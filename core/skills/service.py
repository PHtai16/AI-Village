"""Phase 10 Skill Factory service managing candidate authoring, IT/QA verification, approval, and rollback."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import shutil
from typing import Any, Literal

from core.agents.hr_models import ApprovedSkillCatalogPort, ApprovedSkillDescriptor
from core.contracts.event import Event, sanitize_event_payload
from core.contracts.identifiers import ensure_not_secret, new_event_id, validate_aware_datetime
from core.departments._common import (
    append_department_event,
    authorize_and_audit,
    event_actor_id,
)
from core.departments.process_models import (
    ProcessImprovementRepository,
    ProcessRetrospectiveRepository,
)
from core.departments.qa import EvidenceVaultPort
from core.events.journal import EventJournal
from core.governance.approvals import ApprovalError
from core.governance.evaluator import compute_request_fingerprint
from core.governance.manager import GovernanceAuditError, PermissionDeniedError, PermissionManager
from core.governance.models import (
    ApprovalPolicyContext,
    PolicyRequest,
    TrustedAuthContext,
    actor_kind_to_event_source,
)
from core.skills.benchmarking import (
    compute_ab_benchmark_metrics,
    evaluate_benchmark_status,
    scrub_benchmark_outcomes,
)
from core.skills.models import (
    VALID_SKILL_TRANSITIONS,
    ActivateSkillRequest,
    BenchmarkStatus,
    ItTechnicalReviewRequest,
    ProposeSkillActivationRequest,
    QaSkillBenchmarkRequest,
    RecordImprovementOutcomeRequest,
    RegisteredToolLookupPort,
    RollbackSkillRequest,
    SkillActivationOutcome,
    SkillActivationProposalRecord,
    SkillActivePointer,
    SkillBenchmarkCaseOutcome,
    SkillBenchmarkError,
    SkillBenchmarkMetrics,
    SkillBenchmarkRecord,
    SkillBenchmarkRunnerPort,
    SkillCandidateRequest,
    SkillFrontmatter,
    SkillGovernanceError,
    SkillImprovementOutcomeRecord,
    SkillIndependenceError,
    SkillIntegrityError,
    SkillItReviewRecord,
    SkillLifecycleRepository,
    SkillLifecycleStatus,
    SkillPackageRecord,
    SkillRollbackOutcome,
    SkillStateError,
    SkillValidationError,
    new_activation_proposal_id,
    new_benchmark_id,
    new_improvement_outcome_id,
    new_it_review_id,
    new_package_id,
)
from core.skills.parser import (
    assess_skill_risk_and_sensitivity,
    compute_directory_file_hashes,
    parse_and_validate_skill_markdown,
    promote_package_to_approved,
    verify_approved_package_integrity,
    write_candidate_package,
)
from engines.antigravity.diagnostics import redact_secrets


class SkillLifecycleService:
    """Orchestrates P10.03-P10.05 Skill packages and exposes verified APPROVED catalog entries."""

    def __init__(
        self,
        *,
        skills_root: Path,
        repo: SkillLifecycleRepository,
        permission_manager: PermissionManager,
        journal: EventJournal,
        tool_lookup: RegisteredToolLookupPort | None = None,
        improvement_repo: ProcessImprovementRepository | None = None,
        retrospective_repo: ProcessRetrospectiveRepository | None = None,
        benchmark_runner: SkillBenchmarkRunnerPort | None = None,
        evidence_vault: EvidenceVaultPort | None = None,
    ) -> None:
        """Bind Skill Factory to disk root, SQLite persistence, Governance, Journal, and ports."""
        self._skills_root = skills_root.resolve()
        self._repo = repo
        self._pm = permission_manager
        self._journal = journal
        self._tool_lookup = tool_lookup
        self._improvement_repo = improvement_repo
        self._retro_repo = retrospective_repo
        self._benchmark_runner = benchmark_runner
        self._evidence_vault = evidence_vault

    @property
    def skills_root(self) -> Path:
        """Return resolved filesystem root for candidates and approved skill packages."""
        return self._skills_root

    def _require_package(self, skill_id: str, version: str) -> SkillPackageRecord:
        pkg = self._repo.get_package(skill_id, version)
        if pkg is None:
            raise SkillStateError(
                "SKILL_PACKAGE_NOT_FOUND",
                f"Skill package '{skill_id}@{version}' does not exist.",
            )
        return pkg

    @staticmethod
    def _assert_transition(
        current: SkillLifecycleStatus,
        target: SkillLifecycleStatus,
    ) -> None:
        if target not in VALID_SKILL_TRANSITIONS.get(current, frozenset()):
            raise SkillStateError(
                "INVALID_SKILL_TRANSITION",
                f"Cannot transition Skill lifecycle state '{current}' -> '{target}'.",
            )

    def _authorize(
        self,
        *,
        auth_context: TrustedAuthContext,
        action: str,
        resource: str,
        timestamp: datetime,
        task_id: str | None = None,
        run_id: str | None = None,
        approval_id: str | None = None,
        parameters: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> Any:
        try:
            return authorize_and_audit(
                self._pm,
                auth_context=auth_context,
                action=action,
                resource=resource,
                timestamp=timestamp,
                task_id=task_id,
                run_id=run_id,
                approval_id=approval_id,
                parameters=parameters,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            if hasattr(exc, "error_code") and hasattr(exc, "message"):
                raise SkillGovernanceError(exc.error_code, exc.message) from exc
            raise

    # ------------------------------------------------------------------
    # P10.03: Skill Candidate Creation, Contract Validation & Versioning
    # ------------------------------------------------------------------

    def create_candidate(
        self,
        request: SkillCandidateRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> SkillPackageRecord:
        """Author and persist a validated Skill candidate package cleanly in ``candidates/``."""
        self._authorize(
            auth_context=auth_context,
            action="skill.propose",
            resource=f"skill:{request.skill_id}:{request.version}",
            timestamp=request.timestamp,
            correlation_id=f"skill-create-{request.skill_id}-{request.version}",
        )

        if self._repo.get_package(request.skill_id, request.version) is not None:
            raise SkillStateError(
                "DUPLICATE_SKILL_VERSION",
                f"Skill '{request.skill_id}@{request.version}' already exists; bump SemVer for changes.",
            )

        case_kinds = {tc.case_kind for tc in request.test_cases}
        if "happy_path" not in case_kinds or "failure_case" not in case_kinds:
            raise SkillValidationError(
                "INCOMPLETE_SKILL_TESTS",
                "Candidate Skill must include at least one 'happy_path' and one 'failure_case' fixture.",
            )

        if request.source_improvement_id is not None and self._improvement_repo is not None:
            imp = self._improvement_repo.get_improvement(request.source_improvement_id)
            if imp is None:
                raise SkillValidationError(
                    "IMPROVEMENT_NOT_FOUND",
                    f"Referenced improvement '{request.source_improvement_id}' does not exist.",
                )

        if self._tool_lookup is not None:
            missing = [
                tid for tid in request.requirements.tools if self._tool_lookup.get(tid) is None
            ]
            if missing:
                raise SkillValidationError(
                    "UNKNOWN_SKILL_TOOL",
                    f"Skill references unregistered tool IDs: {missing}.",
                )

        prior_ptr = self._repo.get_active_pointer(request.skill_id)
        prior_tools: set[str] | None = None
        if prior_ptr is not None and prior_ptr.active_version is not None:
            prior_pkg = self._repo.get_package(request.skill_id, prior_ptr.active_version)
            if prior_pkg is not None:
                prior_tools = set(prior_pkg.frontmatter.requirements.tools)

        risk_profile = assess_skill_risk_and_sensitivity(
            tools=request.requirements.tools,
            dependencies=request.requirements.dependencies,
            scripts=request.scripts,
            risk_level=request.risk_level,
            risks=request.risks,
            modifies_sensitive_workflow=request.modifies_sensitive_workflow,
            prior_approved_tools=prior_tools,
        )

        initial_status: SkillLifecycleStatus = (
            "CANDIDATE" if request.submit_as_candidate else "DRAFT"
        )
        frontmatter = SkillFrontmatter(
            spec_version=request.spec_version,
            id=request.skill_id,
            name=request.name,
            version=request.version,
            description=request.description,
            category=request.category,
            author=request.author,
            maintainer=request.maintainer,
            status=initial_status,
            requirements=request.requirements,
        )

        rel_pkg, manifest_sha, package_sha, file_hashes = write_candidate_package(
            self._skills_root,
            request=request,
            frontmatter=frontmatter,
            risk_profile=risk_profile,
        )

        audit_event = Event(
            event_id=new_event_id(),
            timestamp=request.timestamp,
            event_type="skill.candidate_created",
            source=actor_kind_to_event_source(auth_context.actor_kind),
            actor_id=event_actor_id(auth_context),
            correlation_id=f"skill-cand-{request.skill_id}-{request.version}",
            redacted_payload=sanitize_event_payload(
                {
                    "skill_id": request.skill_id,
                    "version": request.version,
                    "lifecycle_status": initial_status,
                    "source_improvement_id": request.source_improvement_id,
                    "package_relpath": rel_pkg,
                    "package_sha256": package_sha,
                    "risk_level": risk_profile.risk_level,
                    "requires_human_approval": risk_profile.requires_human_approval,
                }
            ),
        )
        record = SkillPackageRecord(
            package_id=new_package_id(),
            skill_id=request.skill_id,
            version=request.version,
            lifecycle_status=initial_status,
            frontmatter=frontmatter,
            contract=request.contract,
            risk_profile=risk_profile,
            test_cases=list(request.test_cases),
            source_improvement_id=request.source_improvement_id,
            storage_zone="candidates",
            package_relpath=rel_pkg,
            manifest_sha256=manifest_sha,
            package_sha256=package_sha,
            file_hashes=file_hashes,
            benchmark_status="NOT_BENCHMARKED",
            approved_for_production=False,
            record_version=1,
            created_by=auth_context.actor_id,
            created_at=request.timestamp,
            updated_by=auth_context.actor_id,
            updated_at=request.timestamp,
            last_audit_event_id=audit_event.event_id,
        )
        try:
            saved = self._repo.insert_package_atomic(package=record, audit_event=audit_event)
        except Exception:
            pkg_dir = (self._skills_root / rel_pkg).resolve()
            if pkg_dir.exists() and self._skills_root in pkg_dir.parents:
                shutil.rmtree(pkg_dir, ignore_errors=True)
            raise

        if (
            initial_status == "CANDIDATE"
            and request.source_improvement_id is not None
            and self._improvement_repo is not None
        ):
            self._improvement_repo.update_improvement_status(
                request.source_improvement_id,
                status="CANDIDATE_CREATED",
                updated_at=request.timestamp,
            )
        return saved

    def register_external_candidate_dir(
        self,
        *,
        relpath: str,
        risk_level: Literal["low", "medium", "high"] = "low",
        risks: list[str],
        test_cases: list[Any],
        contract: Any,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> SkillPackageRecord:
        """Validate an externally authored ``SKILL.md`` candidate while blocking forged APPROVED claims."""
        resolved_dir = (self._skills_root / relpath).resolve()
        if self._skills_root not in resolved_dir.parents:
            raise SkillValidationError("PATH_TRAVERSAL_BLOCKED", "Candidate path escapes skills root.")
        skill_md_path = resolved_dir / "SKILL.md"
        if not skill_md_path.is_file():
            raise SkillValidationError("SKILL_MD_MISSING", f"Missing SKILL.md at '{relpath}'.")
        fm, _ = parse_and_validate_skill_markdown(skill_md_path.read_text(encoding="utf-8"))
        # Security invariant (SKILL_SPEC §5): YAML claiming status='APPROVED' NEVER activates a skill.
        sanitized_req = SkillCandidateRequest(
            skill_id=fm.id,
            name=fm.name,
            version=fm.version,
            spec_version=fm.spec_version,
            description=fm.description,
            category=fm.category,
            author=fm.author,
            maintainer=fm.maintainer,
            requirements=fm.requirements,
            contract=contract,
            risk_level=risk_level,
            risks=risks,
            test_cases=test_cases,
            submit_as_candidate=True,
            timestamp=timestamp,
        )
        return self.create_candidate(sanitized_req, auth_context=auth_context)

    def submit_draft_as_candidate(
        self,
        skill_id: str,
        version: str,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> SkillPackageRecord:
        """Transition a ``DRAFT`` package cleanly into ``CANDIDATE`` state."""
        validate_aware_datetime(timestamp, "timestamp")
        self._authorize(
            auth_context=auth_context,
            action="skill.propose",
            resource=f"skill:{skill_id}:{version}",
            timestamp=timestamp,
        )
        current = self._require_package(skill_id, version)
        self._assert_transition(current.lifecycle_status, "CANDIDATE")
        ev = Event(
            event_id=new_event_id(),
            timestamp=timestamp,
            event_type="skill.candidate_created",
            source=actor_kind_to_event_source(auth_context.actor_kind),
            actor_id=event_actor_id(auth_context),
            correlation_id=f"skill-sub-{skill_id}-{version}",
            redacted_payload=sanitize_event_payload(
                {
                    "skill_id": skill_id,
                    "version": version,
                    "previous_status": current.lifecycle_status,
                    "lifecycle_status": "CANDIDATE",
                }
            ),
        )
        updated = current.model_copy(
            update={
                "lifecycle_status": "CANDIDATE",
                "record_version": current.record_version + 1,
                "updated_by": auth_context.actor_id,
                "updated_at": timestamp,
                "last_audit_event_id": ev.event_id,
            }
        )
        return self._repo.update_package_atomic(
            package=updated,
            expected_record_version=current.record_version,
            audit_event=ev,
        )

    def get_package(self, skill_id: str, version: str) -> SkillPackageRecord | None:
        """Fetch persisted Skill package metadata without exposing unapproved files."""
        return self._repo.get_package(skill_id, version)

    def list_packages(
        self,
        *,
        skill_id: str | None = None,
        lifecycle_status: SkillLifecycleStatus | None = None,
    ) -> list[SkillPackageRecord]:
        """List persisted Skill packages matching filters."""
        return self._repo.list_packages(skill_id=skill_id, lifecycle_status=lifecycle_status)

    def get_active_skill_markdown(self, skill_id: str) -> str:
        """Read selected APPROVED ``SKILL.md`` cleanly (on-demand context minimization + integrity check)."""
        ptr = self._repo.get_active_pointer(skill_id)
        if ptr is None or ptr.active_version is None:
            raise SkillStateError("SKILL_NOT_ACTIVE", f"Skill '{skill_id}' has no active APPROVED version.")
        pkg = self._require_package(skill_id, ptr.active_version)
        if pkg.lifecycle_status != "APPROVED" or pkg.storage_zone != "approved":
            raise SkillIntegrityError(
                "SKILL_NOT_APPROVED",
                f"Active pointer '{skill_id}@{ptr.active_version}' is not in verified APPROVED zone.",
            )
        return verify_approved_package_integrity(
            self._skills_root,
            package_relpath=pkg.package_relpath,
            expected_package_sha256=pkg.package_sha256,
        )

    def _verify_candidate_integrity_on_disk(self, pkg: SkillPackageRecord) -> str:
        """Verify candidate directory matches DB ``package_sha256`` before IT or QA verification."""
        pkg_dir = (self._skills_root / pkg.package_relpath).resolve()
        if self._skills_root not in pkg_dir.parents:
            raise SkillValidationError("PATH_TRAVERSAL_BLOCKED", "Package path escapes skills root.")
        _, actual_sha = compute_directory_file_hashes(pkg_dir)
        if actual_sha != pkg.package_sha256:
            raise SkillIntegrityError(
                "CANDIDATE_INTEGRITY_MISMATCH",
                (
                    f"Skill '{pkg.skill_id}@{pkg.version}' files on disk changed since registration: "
                    f"expected {pkg.package_sha256[:12]}, got {actual_sha[:12]}."
                ),
            )
        return actual_sha

    # ------------------------------------------------------------------
    # P10.04: IT Technical Validation & Independent QA A/B Benchmarking
    # ------------------------------------------------------------------

    def record_it_technical_review(
        self,
        request: ItTechnicalReviewRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> tuple[SkillItReviewRecord, SkillPackageRecord]:
        """Record IT Department tool compatibility and isolated sandbox verification (never QA pass)."""
        self._authorize(
            auth_context=auth_context,
            action="skill.review_technical",
            resource=f"skill:{request.skill_id}:{request.version}",
            task_id=request.task_id,
            run_id=request.run_id,
            timestamp=request.timestamp,
            correlation_id=f"skill-it-{request.skill_id}-{request.version}",
        )
        current = self._require_package(request.skill_id, request.version)
        if current.lifecycle_status not in {"CANDIDATE", "UNDER_REVIEW"}:
            raise SkillStateError(
                "INVALID_SKILL_STATE_FOR_IT_REVIEW",
                (
                    f"IT review requires status 'CANDIDATE' or 'UNDER_REVIEW', got "
                    f"'{current.lifecycle_status}'."
                ),
            )
        self._verify_candidate_integrity_on_disk(current)

        verified_tools: list[str] = []
        missing_tools: list[str] = []
        for tool_id in current.frontmatter.requirements.tools:
            if self._tool_lookup is None or self._tool_lookup.get(tool_id) is not None:
                verified_tools.append(tool_id)
            else:
                missing_tools.append(tool_id)

        scripts_checked = sorted(k for k in current.file_hashes if k.startswith("scripts/"))
        findings = [redact_secrets(f).strip()[:500] for f in request.findings]
        if missing_tools:
            findings.append(f"Missing registered tools in ToolRegistry: {missing_tools}.")
        if not request.sandbox_passed:
            findings.append("IT sandbox verification failed.")
        passed = bool(request.sandbox_passed and not missing_tools)

        combined_logs = "\n".join(
            part for part in (request.sandbox_stdout.strip(), request.sandbox_stderr.strip()) if part
        )
        scrubbed_log = redact_secrets(combined_logs)[:2000]

        review_id = new_it_review_id()
        next_status: SkillLifecycleStatus = (
            "UNDER_REVIEW" if passed else current.lifecycle_status
        )
        ev = Event(
            event_id=new_event_id(),
            timestamp=request.timestamp,
            event_type="skill.technical_reviewed",
            source=actor_kind_to_event_source(auth_context.actor_kind),
            task_id=request.task_id,
            run_id=request.run_id,
            actor_id=event_actor_id(auth_context),
            correlation_id=f"skill-it-{request.skill_id}-{request.version}",
            redacted_payload=sanitize_event_payload(
                {
                    "review_id": review_id,
                    "skill_id": request.skill_id,
                    "version": request.version,
                    "package_sha256": current.package_sha256,
                    "passed": passed,
                    "sandbox_passed": request.sandbox_passed,
                    "missing_tools": missing_tools,
                    "scripts_checked": scripts_checked,
                }
            ),
        )
        review = SkillItReviewRecord(
            review_id=review_id,
            skill_id=request.skill_id,
            version=request.version,
            package_sha256=current.package_sha256,
            reviewer_id=auth_context.actor_id,
            passed=passed,
            registered_tools_verified=verified_tools,
            missing_tools=missing_tools,
            scripts_checked=scripts_checked,
            sandbox_passed=request.sandbox_passed,
            sandbox_log_excerpt=scrubbed_log,
            findings=findings,
            reviewed_at=request.timestamp,
            audit_event_id=ev.event_id,
        )
        updated_pkg = current.model_copy(
            update={
                "lifecycle_status": next_status,
                "it_review_passed": passed,
                "latest_it_review_id": review_id,
                "approved_for_production": False,
                "record_version": current.record_version + 1,
                "updated_by": auth_context.actor_id,
                "updated_at": request.timestamp,
                "last_audit_event_id": ev.event_id,
            }
        )
        return self._repo.insert_it_review_atomic(
            review=review,
            updated_package=updated_pkg,
            expected_record_version=current.record_version,
            audit_event=ev,
        )

    def run_qa_benchmark(
        self,
        request: QaSkillBenchmarkRequest,
        *,
        auth_context: TrustedAuthContext,
        runner: SkillBenchmarkRunnerPort | None = None,
    ) -> tuple[SkillBenchmarkRecord, SkillPackageRecord]:
        """Run independent QA fixture A/B benchmark and store transparent immutable verdict."""
        current = self._require_package(request.skill_id, request.version)
        it_reviews = self._repo.list_it_reviews(request.skill_id, request.version)
        it_reviewers = {r.reviewer_id for r in it_reviews}

        # Enforce QA independence BEFORE recording any verdict (author and IT cannot self-certify).
        if auth_context.actor_kind not in {"trusted_verifier", "human_operator"}:
            raise SkillIndependenceError(
                "QA_VERIFIER_ROLE_REQUIRED",
                (
                    f"Actor '{auth_context.actor_id}' ({auth_context.actor_kind}) cannot run QA "
                    "benchmarks; requires 'trusted_verifier' or 'human_operator'."
                ),
            )
        if auth_context.actor_id in {current.created_by, current.frontmatter.author} or (
            auth_context.actor_id in it_reviewers
        ):
            raise SkillIndependenceError(
                "SELF_BENCHMARK_FORBIDDEN",
                (
                    f"Author/IT reviewer '{auth_context.actor_id}' cannot independently QA-verify "
                    f"'{request.skill_id}@{request.version}'."
                ),
            )

        self._authorize(
            auth_context=auth_context,
            action="skill.benchmark",
            resource=f"skill:{request.skill_id}:{request.version}",
            task_id=request.task_id,
            run_id=request.run_id,
            timestamp=request.timestamp,
            correlation_id=f"skill-bm-{request.skill_id}-{request.version}",
        )

        if current.lifecycle_status not in {"CANDIDATE", "UNDER_REVIEW"}:
            raise SkillStateError(
                "INVALID_SKILL_STATE_FOR_BENCHMARK",
                (
                    f"Benchmarking requires status 'CANDIDATE' or 'UNDER_REVIEW', got "
                    f"'{current.lifecycle_status}'."
                ),
            )
        self._verify_candidate_integrity_on_disk(current)

        active_runner = runner or self._benchmark_runner
        if request.explicit_outcomes:
            raw_outcomes = list(request.explicit_outcomes)
        elif active_runner is not None:
            raw_outcomes = []
            for tc in current.test_cases:
                raw_outcomes.append(
                    active_runner.execute_fixture_arm(
                        package=current, case=tc, arm="A_BASELINE"
                    )
                )
                raw_outcomes.append(
                    active_runner.execute_fixture_arm(
                        package=current, case=tc, arm="B_SKILL"
                    )
                )
        else:
            raise SkillBenchmarkError(
                "MISSING_BENCHMARK_EXECUTION",
                "Supply either a SkillBenchmarkRunnerPort or measured explicit_outcomes for QA fixtures.",
            )

        scrubbed_outcomes = scrub_benchmark_outcomes(raw_outcomes)
        metrics, _, arm_b = compute_ab_benchmark_metrics(current.test_cases, scrubbed_outcomes)
        bm_status, findings = evaluate_benchmark_status(
            metrics=metrics,
            arm_b=arm_b,
            require_improvement=request.require_improvement,
            qualitative_rubric=request.qualitative_rubric,
        )

        bm_id = new_benchmark_id()
        evidence_id: str | None = None
        if self._evidence_vault is not None and request.task_id is not None:
            evd = self._evidence_vault.record_evidence(
                auth_context=auth_context,
                evidence_kind="qa_log",
                task_id=request.task_id,
                run_id=request.run_id,
                content_sha256=current.package_sha256,
                summary=redact_secrets(request.summary)[:500],
                payload={
                    "benchmark_id": bm_id,
                    "skill_id": request.skill_id,
                    "version": request.version,
                    "benchmark_status": bm_status,
                    "metrics": metrics.model_dump(mode="json"),
                    "findings": findings,
                },
                timestamp=request.timestamp,
            )
            evidence_id = str(evd.evidence_id)

        next_lifecycle: SkillLifecycleStatus = (
            "REJECTED" if bm_status == "FAILED" else "UNDER_REVIEW"
        )
        audit_event = Event(
            event_id=new_event_id(),
            timestamp=request.timestamp,
            event_type="skill.benchmark_recorded",
            source="qa_verifier",
            task_id=request.task_id,
            run_id=request.run_id,
            actor_id=event_actor_id(auth_context),
            evidence_id=evidence_id,
            correlation_id=f"skill-bm-{bm_id}",
            redacted_payload=sanitize_event_payload(
                {
                    "benchmark_id": bm_id,
                    "skill_id": request.skill_id,
                    "version": request.version,
                    "package_sha256": current.package_sha256,
                    "benchmark_status": bm_status,
                    "pass_rate_a": metrics.pass_rate_a,
                    "pass_rate_b": metrics.pass_rate_b,
                    "duration_reduction_pct": metrics.duration_reduction_pct,
                    "tool_calls_reduction_pct": metrics.tool_calls_reduction_pct,
                    "findings": findings,
                }
            ),
        )
        bm_record = SkillBenchmarkRecord(
            benchmark_id=bm_id,
            skill_id=request.skill_id,
            version=request.version,
            package_sha256=current.package_sha256,
            qa_actor_id=auth_context.actor_id,
            benchmark_status=bm_status,
            metrics=metrics,
            case_outcomes=scrubbed_outcomes,
            qualitative_rubric=dict(request.qualitative_rubric),
            findings=findings,
            evidence_id=evidence_id,
            summary=redact_secrets(request.summary).strip()[:1000],
            recorded_at=request.timestamp,
            audit_event_id=audit_event.event_id,
        )
        updated_pkg = current.model_copy(
            update={
                "lifecycle_status": next_lifecycle,
                "benchmark_status": bm_status,
                "latest_benchmark_id": bm_id,
                "approved_for_production": False,
                "record_version": current.record_version + 1,
                "updated_by": auth_context.actor_id,
                "updated_at": request.timestamp,
                "last_audit_event_id": audit_event.event_id,
            }
        )
        return self._repo.insert_benchmark_atomic(
            benchmark=bm_record,
            updated_package=updated_pkg,
            expected_record_version=current.record_version,
            audit_event=audit_event,
        )

    def list_it_reviews(self, skill_id: str, version: str) -> list[SkillItReviewRecord]:
        """List IT technical reviews recorded for ``(skill_id, version)``."""
        return self._repo.list_it_reviews(skill_id, version)

    def list_benchmarks(self, skill_id: str, version: str) -> list[SkillBenchmarkRecord]:
        """List immutable QA benchmarks recorded for ``(skill_id, version)``."""
        return self._repo.list_benchmarks(skill_id, version)

    # ------------------------------------------------------------------
    # P10.05: Activation Proposals, Approvals, Outcomes, Disable & Rollback
    # ------------------------------------------------------------------

    @staticmethod
    def _activation_policy_parameters(
        pkg: SkillPackageRecord,
        *,
        requires_human: bool,
        has_permission_delta: bool,
    ) -> dict[str, Any]:
        return {
            "skill_id": pkg.skill_id,
            "version": pkg.version,
            "package_sha256": pkg.package_sha256,
            "requires_human_approval": requires_human,
            "has_scripts": pkg.risk_profile.has_executable_scripts,
            "has_permission_delta": has_permission_delta,
            "sensitive_workflow": pkg.risk_profile.modifies_sensitive_workflow,
            "risk_level": pkg.risk_profile.risk_level,
        }

    def propose_skill_activation(
        self,
        request: ProposeSkillActivationRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> SkillActivationProposalRecord:
        """Propose a benchmark-PASSED Skill for activation, queuing Human Approval when sensitive."""
        self._authorize(
            auth_context=auth_context,
            action="skill.propose",
            resource=f"skill:{request.skill_id}:{request.version}:activate",
            task_id=request.task_id,
            timestamp=request.timestamp,
            correlation_id=f"skill-prop-{request.skill_id}-{request.version}",
        )
        if auth_context.actor_kind not in {"master_agent", "human_operator"}:
            raise SkillGovernanceError(
                "ACTIVATION_PROPOSAL_FORBIDDEN",
                f"Actor kind '{auth_context.actor_kind}' cannot propose production skill activation.",
            )

        pkg = self._require_package(request.skill_id, request.version)
        if pkg.lifecycle_status != "UNDER_REVIEW":
            raise SkillStateError(
                "INVALID_SKILL_STATE_FOR_ACTIVATION",
                f"Activation proposal requires 'UNDER_REVIEW' state, got '{pkg.lifecycle_status}'.",
            )
        self._verify_candidate_integrity_on_disk(pkg)

        benchmarks = self._repo.list_benchmarks(request.skill_id, request.version)
        matching_passed = [
            b
            for b in benchmarks
            if b.benchmark_status == "PASSED" and b.package_sha256 == pkg.package_sha256
        ]
        if not matching_passed:
            raise SkillBenchmarkError(
                "BENCHMARK_PASS_REQUIRED",
                (
                    f"Cannot propose activation for '{request.skill_id}@{request.version}': "
                    f"current benchmark_status is '{pkg.benchmark_status}'."
                ),
            )
        latest_bm = matching_passed[-1]

        open_props = [
            p
            for p in self._repo.list_activation_proposals(skill_id=request.skill_id)
            if p.version == request.version
            and p.proposal_status in {"PENDING_APPROVAL", "PENDING_ACTIVATION"}
        ]
        if open_props:
            raise SkillStateError(
                "DUPLICATE_PENDING_PROPOSAL",
                f"Pending activation proposal '{open_props[0].proposal_id}' already exists.",
            )

        prior_ptr = self._repo.get_active_pointer(request.skill_id)
        prior_tools: set[str] | None = None
        if prior_ptr is not None and prior_ptr.active_version is not None:
            prior_pkg = self._repo.get_package(request.skill_id, prior_ptr.active_version)
            if prior_pkg is not None:
                prior_tools = set(prior_pkg.frontmatter.requirements.tools)

        live_risk = assess_skill_risk_and_sensitivity(
            tools=pkg.frontmatter.requirements.tools,
            dependencies=pkg.frontmatter.requirements.dependencies,
            scripts={k: "present" for k in pkg.file_hashes if k.startswith("scripts/")},
            risk_level=pkg.risk_profile.risk_level,
            risks=pkg.risk_profile.risks,
            modifies_sensitive_workflow=pkg.risk_profile.modifies_sensitive_workflow,
            prior_approved_tools=prior_tools,
        )
        requires_human = live_risk.requires_human_approval
        resource = f"skill:{request.skill_id}:{request.version}"
        params = self._activation_policy_parameters(
            pkg,
            requires_human=requires_human,
            has_permission_delta=live_risk.modifies_permissions_or_tools,
        )
        fingerprint = compute_request_fingerprint(
            action="skill.activate",
            resource=resource,
            task_id=request.task_id,
            run_id=None,
            parameters=params,
        )

        approval_id: str | None = None
        proposal_status: Literal["PENDING_APPROVAL", "PENDING_ACTIVATION"] = "PENDING_ACTIVATION"
        if requires_human:
            if not request.request_human_approval_if_needed or self._pm.approval_service is None:
                raise SkillGovernanceError(
                    "APPROVAL_SERVICE_REQUIRED",
                    "Sensitive skill activation requires ApprovalService to queue Human Operator approval.",
                )
            probe_decision = self._pm.evaluator.evaluate(
                PolicyRequest(
                    auth_context=auth_context,
                    action="skill.activate",
                    resource=resource,
                    task_id=request.task_id,
                    parameters=params,
                    timestamp=request.timestamp,
                )
            )
            apr = self._pm.approval_service.create_approval_request(
                requester=auth_context,
                action="skill.activate",
                target_resource=resource,
                task_id=request.task_id,
                run_id=None,
                request_fingerprint=probe_decision.request_fingerprint,
                policy_snapshot=ApprovalPolicyContext(
                    matched_rule_ids=probe_decision.matched_rule_ids,
                    rule_versions=self._pm.evaluator.policy_snapshot(
                        probe_decision.matched_rule_ids
                    ),
                    reason_code=probe_decision.reason_code,
                ),
                risk_summary=redact_secrets(
                    f"Activate skill {request.skill_id}@{request.version}: "
                    + ("; ".join(live_risk.sensitivity_reasons) or request.rationale)
                )[:500],
                requested_at=request.timestamp,
                correlation_id=f"skill-apr-{request.skill_id}-{request.version}",
            )
            approval_id = apr.approval_id
            proposal_status = "PENDING_APPROVAL"

        prop_id = new_activation_proposal_id()
        ev = Event(
            event_id=new_event_id(),
            timestamp=request.timestamp,
            event_type="skill.activation_proposed",
            source=actor_kind_to_event_source(auth_context.actor_kind),
            task_id=request.task_id,
            actor_id=event_actor_id(auth_context),
            correlation_id=f"skill-prop-{prop_id}",
            redacted_payload=sanitize_event_payload(
                {
                    "proposal_id": prop_id,
                    "skill_id": request.skill_id,
                    "version": request.version,
                    "package_sha256": pkg.package_sha256,
                    "benchmark_id": latest_bm.benchmark_id,
                    "proposal_status": proposal_status,
                    "requires_human_approval": requires_human,
                    "approval_id": approval_id,
                }
            ),
        )
        proposal = SkillActivationProposalRecord(
            proposal_id=prop_id,
            skill_id=request.skill_id,
            version=request.version,
            package_sha256=pkg.package_sha256,
            benchmark_id=latest_bm.benchmark_id,
            task_id=request.task_id,
            proposal_status=proposal_status,
            requires_human_approval=requires_human,
            sensitivity_reasons=live_risk.sensitivity_reasons,
            approval_id=approval_id,
            approval_action="skill.activate",
            approval_resource=resource,
            approval_request_fingerprint=fingerprint,
            rationale=request.rationale,
            proposed_by=auth_context.actor_id,
            proposed_at=request.timestamp,
            audit_event_id=ev.event_id,
        )
        return self._repo.insert_activation_proposal_atomic(proposal=proposal, audit_event=ev)

    def list_pending_skill_approvals(
        self,
        *,
        include_non_sensitive: bool = False,
        skill_id: str | None = None,
    ) -> list[SkillActivationProposalRecord]:
        """Return queued pending skill activation proposals awaiting operator or Master execution."""
        items = self._repo.list_activation_proposals(skill_id=skill_id)
        wanted = (
            {"PENDING_APPROVAL", "PENDING_ACTIVATION"}
            if include_non_sensitive
            else {"PENDING_APPROVAL"}
        )
        return [p for p in items if p.proposal_status in wanted]

    def activate_skill_version(
        self,
        request: ActivateSkillRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> SkillActivationOutcome:
        """Promote a benchmarked & approved Skill into immutable ``approved/`` zone and update active pointer."""
        proposal = self._repo.get_activation_proposal(request.proposal_id)
        if proposal is None:
            raise SkillStateError(
                "ACTIVATION_PROPOSAL_NOT_FOUND",
                f"Skill activation proposal '{request.proposal_id}' does not exist.",
            )
        if proposal.proposal_status not in {"PENDING_APPROVAL", "PENDING_ACTIVATION"}:
            raise SkillStateError(
                "PROPOSAL_ALREADY_SETTLED",
                f"Proposal '{proposal.proposal_id}' is already '{proposal.proposal_status}'.",
            )

        pkg = self._require_package(proposal.skill_id, proposal.version)
        if pkg.lifecycle_status != "UNDER_REVIEW":
            raise SkillStateError(
                "INVALID_SKILL_STATE_FOR_ACTIVATION",
                f"Expected package 'UNDER_REVIEW', got '{pkg.lifecycle_status}'.",
            )
        self._verify_candidate_integrity_on_disk(pkg)
        if pkg.package_sha256 != proposal.package_sha256:
            raise SkillIntegrityError(
                "PROPOSAL_PACKAGE_SHA_MISMATCH",
                f"Package hash changed since proposal '{proposal.proposal_id}'.",
            )

        benchmarks = self._repo.list_benchmarks(pkg.skill_id, pkg.version)
        if not any(
            b.benchmark_id == proposal.benchmark_id
            and b.benchmark_status == "PASSED"
            and b.package_sha256 == proposal.package_sha256
            for b in benchmarks
        ):
            raise SkillBenchmarkError(
                "BENCHMARK_PASS_REQUIRED",
                f"Valid PASSED QA benchmark '{proposal.benchmark_id}' is missing for activation.",
            )

        params = self._activation_policy_parameters(
            pkg,
            requires_human=proposal.requires_human_approval,
            has_permission_delta=pkg.risk_profile.modifies_permissions_or_tools,
        )
        effective_approval_id = request.approval_id or proposal.approval_id
        policy_req = PolicyRequest(
            auth_context=auth_context,
            action="skill.activate",
            resource=proposal.approval_resource,
            task_id=proposal.task_id,
            approval_id=effective_approval_id,
            parameters=params,
            timestamp=request.timestamp,
            correlation_id=f"skill-act-{proposal.proposal_id}",
        )

        def _perform_activation() -> SkillActivationOutcome:
            prior_ptr = self._repo.get_active_pointer(pkg.skill_id)
            prior_active_ver = prior_ptr.active_version if prior_ptr is not None else None
            demoted_prior: SkillPackageRecord | None = None
            if prior_active_ver is not None and prior_active_ver != pkg.version:
                prior_pkg = self._require_package(pkg.skill_id, prior_active_ver)
                demoted_prior = prior_pkg.model_copy(
                    update={
                        "lifecycle_status": "DEPRECATED",
                        "record_version": prior_pkg.record_version + 1,
                        "updated_by": auth_context.actor_id,
                        "updated_at": request.timestamp,
                    }
                )

            rel_approved, approved_manifest_sha, approved_pkg_sha, approved_hashes = (
                promote_package_to_approved(
                    self._skills_root,
                    skill_id=pkg.skill_id,
                    version=pkg.version,
                    expected_package_sha256=pkg.package_sha256,
                    frontmatter=pkg.frontmatter,
                    contract=pkg.contract,
                    risk_profile=pkg.risk_profile,
                )
            )
            act_ev = Event(
                event_id=new_event_id(),
                timestamp=request.timestamp,
                event_type="skill.activated",
                source=actor_kind_to_event_source(auth_context.actor_kind),
                task_id=proposal.task_id,
                actor_id=event_actor_id(auth_context),
                correlation_id=f"skill-act-{pkg.skill_id}-{pkg.version}",
                redacted_payload=sanitize_event_payload(
                    {
                        "skill_id": pkg.skill_id,
                        "version": pkg.version,
                        "proposal_id": proposal.proposal_id,
                        "approved_relpath": rel_approved,
                        "approved_package_sha256": approved_pkg_sha,
                        "previous_approved_version": prior_active_ver,
                        "approval_id": effective_approval_id,
                    }
                ),
            )
            approved_fm = pkg.frontmatter.model_copy(update={"status": "APPROVED"})
            next_pkg = pkg.model_copy(
                update={
                    "lifecycle_status": "APPROVED",
                    "frontmatter": approved_fm,
                    "storage_zone": "approved",
                    "package_relpath": rel_approved,
                    "manifest_sha256": approved_manifest_sha,
                    "package_sha256": approved_pkg_sha,
                    "file_hashes": approved_hashes,
                    "approved_for_production": True,
                    "record_version": pkg.record_version + 1,
                    "updated_by": auth_context.actor_id,
                    "updated_at": request.timestamp,
                    "last_audit_event_id": act_ev.event_id,
                }
            )
            next_prop = proposal.model_copy(
                update={
                    "proposal_status": "ACTIVATED",
                    "approval_id": effective_approval_id,
                    "decided_by": auth_context.actor_id,
                    "decided_at": request.timestamp,
                }
            )
            next_ptr = SkillActivePointer(
                skill_id=pkg.skill_id,
                active_version=pkg.version,
                previous_approved_version=prior_active_ver,
                updated_by=auth_context.actor_id,
                updated_at=request.timestamp,
                last_audit_event_id=act_ev.event_id,
            )
            try:
                saved_pkg, saved_prop, saved_ptr = self._repo.activate_package_atomic(
                    package=next_pkg,
                    expected_record_version=pkg.record_version,
                    proposal=next_prop,
                    pointer=next_ptr,
                    demoted_prior_package=demoted_prior,
                    audit_event=act_ev,
                )
            except Exception:
                dest_dir = (self._skills_root / rel_approved).resolve()
                if dest_dir.exists() and self._skills_root in dest_dir.parents:
                    shutil.rmtree(dest_dir, ignore_errors=True)
                raise
            return SkillActivationOutcome(
                package=saved_pkg,
                proposal=saved_prop,
                active_pointer=saved_ptr,
                audit_event_id=act_ev.event_id,
            )

        try:
            _, outcome = self._pm.dispatch_protected_action(policy_req, _perform_activation)
            return outcome
        except PermissionDeniedError as exc:
            raise SkillGovernanceError(exc.decision.reason_code, exc.decision.message) from exc
        except ApprovalError as exc:
            raise SkillGovernanceError(exc.error_code, exc.message) from exc
        except GovernanceAuditError as exc:
            raise SkillGovernanceError("GOVERNANCE_AUDIT_FAILED", str(exc)) from exc

    def reject_skill(
        self,
        skill_id: str,
        version: str,
        *,
        reason: str,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> SkillPackageRecord:
        """Explicitly reject a ``CANDIDATE`` or ``UNDER_REVIEW`` Skill version and close pending proposals."""
        validate_aware_datetime(timestamp, "timestamp")
        clean_reason = ensure_not_secret(reason, "reason")
        if auth_context.actor_kind not in {"master_agent", "human_operator", "trusted_verifier"}:
            raise SkillGovernanceError(
                "SKILL_REJECT_DENIED",
                f"Actor kind '{auth_context.actor_kind}' cannot reject Skill packages.",
            )
        self._authorize(
            auth_context=auth_context,
            action="skill.propose",
            resource=f"skill:{skill_id}:{version}:reject",
            timestamp=timestamp,
        )
        current = self._require_package(skill_id, version)
        self._assert_transition(current.lifecycle_status, "REJECTED")
        ev = Event(
            event_id=new_event_id(),
            timestamp=timestamp,
            event_type="skill.rejected",
            source=actor_kind_to_event_source(auth_context.actor_kind),
            actor_id=event_actor_id(auth_context),
            correlation_id=f"skill-rej-{skill_id}-{version}",
            redacted_payload=sanitize_event_payload(
                {
                    "skill_id": skill_id,
                    "version": version,
                    "reason": clean_reason,
                    "previous_status": current.lifecycle_status,
                }
            ),
        )
        updated = current.model_copy(
            update={
                "lifecycle_status": "REJECTED",
                "approved_for_production": False,
                "record_version": current.record_version + 1,
                "updated_by": auth_context.actor_id,
                "updated_at": timestamp,
                "last_audit_event_id": ev.event_id,
            }
        )
        saved = self._repo.update_package_atomic(
            package=updated,
            expected_record_version=current.record_version,
            audit_event=ev,
        )
        for prop in self._repo.list_activation_proposals(skill_id=skill_id):
            if prop.version == version and prop.proposal_status in {
                "PENDING_APPROVAL",
                "PENDING_ACTIVATION",
            }:
                close_ev = Event(
                    event_id=new_event_id(),
                    timestamp=timestamp,
                    event_type="skill.rejected",
                    source=actor_kind_to_event_source(auth_context.actor_kind),
                    actor_id=event_actor_id(auth_context),
                    correlation_id=f"skill-prop-rej-{prop.proposal_id}",
                    redacted_payload={"proposal_id": prop.proposal_id, "reason": clean_reason},
                )
                self._repo.update_activation_proposal_atomic(
                    proposal=prop.model_copy(
                        update={
                            "proposal_status": "REJECTED",
                            "decided_by": auth_context.actor_id,
                            "decided_at": timestamp,
                        }
                    ),
                    audit_event=close_ev,
                )
        return saved

    def rollback_skill(
        self,
        request: RollbackSkillRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> SkillRollbackOutcome:
        """Instantly revert an active Skill to a previously verified immutable APPROVED version."""
        self._authorize(
            auth_context=auth_context,
            action="skill.rollback",
            resource=f"skill:{request.skill_id}:rollback",
            timestamp=request.timestamp,
            correlation_id=f"skill-rb-{request.skill_id}",
        )
        ptr = self._repo.get_active_pointer(request.skill_id)
        if ptr is None or ptr.active_version is None:
            raise SkillStateError(
                "NO_ACTIVE_SKILL_VERSION",
                f"Skill '{request.skill_id}' currently has no active version to roll back.",
            )
        from_ver = ptr.active_version
        target_ver = request.target_version or ptr.previous_approved_version
        if target_ver is None or target_ver == from_ver:
            raise SkillStateError(
                "NO_ROLLBACK_TARGET",
                f"Skill '{request.skill_id}' has no distinct prior APPROVED version for rollback.",
            )

        curr_pkg = self._require_package(request.skill_id, from_ver)
        target_pkg = self._require_package(request.skill_id, target_ver)
        if target_pkg.storage_zone != "approved":
            raise SkillIntegrityError(
                "ROLLBACK_TARGET_NOT_APPROVED",
                f"Target rollback version '{request.skill_id}@{target_ver}' was never promoted to approved/.",
            )
        verify_approved_package_integrity(
            self._skills_root,
            package_relpath=target_pkg.package_relpath,
            expected_package_sha256=target_pkg.package_sha256,
        )

        rb_ev = Event(
            event_id=new_event_id(),
            timestamp=request.timestamp,
            event_type="skill.rolled_back",
            source=actor_kind_to_event_source(auth_context.actor_kind),
            actor_id=event_actor_id(auth_context),
            correlation_id=f"skill-rb-{request.skill_id}-{target_ver}",
            redacted_payload=sanitize_event_payload(
                {
                    "skill_id": request.skill_id,
                    "rolled_back_from": from_ver,
                    "restored_version": target_ver,
                    "previous_version_status": request.mark_current_as,
                    "reason": request.reason,
                }
            ),
        )
        demoted_pkg = curr_pkg.model_copy(
            update={
                "lifecycle_status": request.mark_current_as,
                "approved_for_production": False,
                "record_version": curr_pkg.record_version + 1,
                "updated_by": auth_context.actor_id,
                "updated_at": request.timestamp,
                "last_audit_event_id": rb_ev.event_id,
            }
        )
        restored_pkg = target_pkg.model_copy(
            update={
                "lifecycle_status": "APPROVED",
                "approved_for_production": True,
                "record_version": target_pkg.record_version + 1,
                "updated_by": auth_context.actor_id,
                "updated_at": request.timestamp,
                "last_audit_event_id": rb_ev.event_id,
            }
        )
        next_ptr = SkillActivePointer(
            skill_id=request.skill_id,
            active_version=target_ver,
            previous_approved_version=from_ver,
            updated_by=auth_context.actor_id,
            updated_at=request.timestamp,
            last_audit_event_id=rb_ev.event_id,
        )
        saved_ptr = self._repo.rollback_active_pointer_atomic(
            previous_active_package=demoted_pkg,
            restored_package=restored_pkg,
            pointer=next_ptr,
            audit_event=rb_ev,
        )
        return SkillRollbackOutcome(
            skill_id=request.skill_id,
            rolled_back_from_version=from_ver,
            restored_version=target_ver,
            previous_version_status=request.mark_current_as,
            active_pointer=saved_ptr,
            audit_event_id=rb_ev.event_id,
        )

    def disable_skill(
        self,
        skill_id: str,
        version: str,
        *,
        reason: str,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> SkillPackageRecord:
        """Emergency kill-switch disabling a Skill version and clearing active pointer if targeted."""
        validate_aware_datetime(timestamp, "timestamp")
        clean_reason = ensure_not_secret(reason, "reason")
        self._authorize(
            auth_context=auth_context,
            action="skill.rollback",
            resource=f"skill:{skill_id}:{version}:disable",
            timestamp=timestamp,
        )
        curr = self._require_package(skill_id, version)
        self._assert_transition(curr.lifecycle_status, "DISABLED")
        ptr = self._repo.get_active_pointer(skill_id)
        dis_ev = Event(
            event_id=new_event_id(),
            timestamp=timestamp,
            event_type="skill.disabled",
            source=actor_kind_to_event_source(auth_context.actor_kind),
            actor_id=event_actor_id(auth_context),
            correlation_id=f"skill-dis-{skill_id}-{version}",
            redacted_payload=sanitize_event_payload(
                {"skill_id": skill_id, "version": version, "reason": clean_reason}
            ),
        )
        next_pkg = curr.model_copy(
            update={
                "lifecycle_status": "DISABLED",
                "approved_for_production": False,
                "record_version": curr.record_version + 1,
                "updated_by": auth_context.actor_id,
                "updated_at": timestamp,
                "last_audit_event_id": dis_ev.event_id,
            }
        )
        next_ptr: SkillActivePointer | None = None
        if ptr is not None and ptr.active_version == version:
            next_ptr = SkillActivePointer(
                skill_id=skill_id,
                active_version=None,
                previous_approved_version=ptr.previous_approved_version,
                updated_by=auth_context.actor_id,
                updated_at=timestamp,
                last_audit_event_id=dis_ev.event_id,
            )
        return self._repo.disable_active_skill_atomic(
            package=next_pkg,
            expected_record_version=curr.record_version,
            pointer=next_ptr,
            audit_event=dis_ev,
        )

    def record_improvement_outcome(
        self,
        request: RecordImprovementOutcomeRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> SkillImprovementOutcomeRecord:
        """Compare real baseline vs post-activation Task retrospectives cleanly without fake data."""
        self._authorize(
            auth_context=auth_context,
            action="knowledge.write",
            resource=f"skill:{request.skill_id}:{request.version}:outcome",
            timestamp=request.timestamp,
        )
        if auth_context.actor_kind == "worker_agent":
            raise SkillGovernanceError(
                "OUTCOME_RECORDING_DENIED",
                "Worker agents cannot certify production skill improvement outcomes.",
            )
        if self._retro_repo is None:
            raise SkillStateError(
                "RETROSPECTIVE_REPO_UNAVAILABLE",
                "ProcessRetrospectiveRepository is required to compute measured skill outcomes.",
            )
        self._require_package(request.skill_id, request.version)

        overlap = set(request.baseline_task_ids) & set(request.post_activation_task_ids)
        if overlap:
            raise SkillValidationError(
                "OVERLAPPING_OUTCOME_COHORTS",
                f"Baseline and post-activation cohorts share task IDs: {sorted(overlap)}.",
            )

        def _load_cohort(task_ids: list[str], label: str) -> tuple[float, float | None, float]:
            summaries = []
            for tid in task_ids:
                stored = self._retro_repo.get_by_task(tid)
                if stored is None:
                    raise SkillValidationError(
                        "MISSING_TASK_RETROSPECTIVE",
                        f"{label} task '{tid}' lacks a persisted P10.01 retrospective.",
                    )
                summaries.append(stored.summary)
            n = len(summaries)
            err_rate = round(sum(1 for s in summaries if s.errors_observed) / n, 4)
            durs = [s.duration_ms for s in summaries if s.duration_ms is not None]
            avg_dur = round(sum(durs) / len(durs), 2) if durs else None
            avg_calls = round(sum(s.total_tool_calls for s in summaries) / n, 2)
            return err_rate, avg_dur, avg_calls

        b_err, b_dur, b_calls = _load_cohort(request.baseline_task_ids, "Baseline")
        p_err, p_dur, p_calls = _load_cohort(request.post_activation_task_ids, "Post-activation")

        regression = bool(
            p_err > b_err
            or (
                b_dur is not None
                and p_dur is not None
                and p_dur > (b_dur * 1.25 if b_dur > 0 else p_dur)
                and p_calls >= b_calls
            )
            or (p_err == b_err and b_err > 0.0 and p_calls > b_calls)
        )
        outcome_id = new_improvement_outcome_id()
        summary_msg = (
            f"Skill {request.skill_id}@{request.version}: error_rate {b_err}->{p_err}, "
            f"avg_tool_calls {b_calls}->{p_calls}, avg_duration_ms {b_dur}->{p_dur}, "
            f"regression_detected={regression}."
        )
        ev = Event(
            event_id=new_event_id(),
            timestamp=request.timestamp,
            event_type="skill.improvement_outcome_recorded",
            source=actor_kind_to_event_source(auth_context.actor_kind),
            actor_id=event_actor_id(auth_context),
            correlation_id=f"skill-out-{outcome_id}",
            redacted_payload=sanitize_event_payload(
                {
                    "outcome_id": outcome_id,
                    "skill_id": request.skill_id,
                    "version": request.version,
                    "source_improvement_id": request.source_improvement_id,
                    "baseline_error_rate": b_err,
                    "post_error_rate": p_err,
                    "baseline_avg_tool_calls": b_calls,
                    "post_avg_tool_calls": p_calls,
                    "regression_detected": regression,
                }
            ),
        )
        outcome_rec = SkillImprovementOutcomeRecord(
            outcome_id=outcome_id,
            skill_id=request.skill_id,
            version=request.version,
            source_improvement_id=request.source_improvement_id,
            baseline_task_ids=list(request.baseline_task_ids),
            post_activation_task_ids=list(request.post_activation_task_ids),
            baseline_error_rate=b_err,
            post_error_rate=p_err,
            baseline_avg_duration_ms=b_dur,
            post_avg_duration_ms=p_dur,
            baseline_avg_tool_calls=b_calls,
            post_avg_tool_calls=p_calls,
            regression_detected=regression,
            summary=summary_msg,
            recorded_at=request.timestamp,
            audit_event_id=ev.event_id,
        )
        saved = self._repo.insert_improvement_outcome_atomic(
            outcome=outcome_rec, audit_event=ev
        )
        if (
            not regression
            and request.source_improvement_id is not None
            and self._improvement_repo is not None
        ):
            self._improvement_repo.update_improvement_status(
                request.source_improvement_id,
                status="RESOLVED",
                updated_at=request.timestamp,
            )
        return saved

    def get_active_pointer(self, skill_id: str) -> SkillActivePointer | None:
        """Return active pointer record for ``skill_id``."""
        return self._repo.get_active_pointer(skill_id)

    def list_improvement_outcomes(
        self, *, skill_id: str | None = None
    ) -> list[SkillImprovementOutcomeRecord]:
        """List immutable post-activation measured outcomes."""
        return self._repo.list_improvement_outcomes(skill_id=skill_id)

    # ------------------------------------------------------------------
    # ApprovedSkillCatalogPort implementation (consumed cleanly by HR & Process)
    # ------------------------------------------------------------------

    def get_approved_skill(self, skill_id: str) -> ApprovedSkillDescriptor | None:
        """Return ``ApprovedSkillDescriptor`` strictly when 6 approval & disk integrity checks hold."""
        ptr = self._repo.get_active_pointer(skill_id)
        if ptr is None or ptr.active_version is None:
            return None
        pkg = self._repo.get_package(skill_id, ptr.active_version)
        if pkg is None or pkg.lifecycle_status != "APPROVED" or not pkg.approved_for_production:
            return None
        if pkg.storage_zone != "approved":
            return None
        benchmarks = self._repo.list_benchmarks(skill_id, ptr.active_version)
        if not any(b.benchmark_status == "PASSED" for b in benchmarks):
            return None
        try:
            verify_approved_package_integrity(
                self._skills_root,
                package_relpath=pkg.package_relpath,
                expected_package_sha256=pkg.package_sha256,
            )
        except (SkillIntegrityError, SkillValidationError):
            return None
        return ApprovedSkillDescriptor(
            skill_id=pkg.skill_id,
            version=pkg.version,
            status="APPROVED",
            required_capabilities=[],
            summary=pkg.frontmatter.description,
        )

    def list_approved_skills(self) -> list[ApprovedSkillDescriptor]:
        """List all integrity-verified active APPROVED skills deterministically."""
        descriptors: list[ApprovedSkillDescriptor] = []
        for ptr in self._repo.list_active_pointers():
            if ptr.active_version is None:
                continue
            desc = self.get_approved_skill(ptr.skill_id)
            if desc is not None:
                descriptors.append(desc)
        return sorted(descriptors, key=lambda d: d.skill_id)
