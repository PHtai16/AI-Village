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
from core.events.journal import EventJournal
from core.governance.manager import PermissionManager
from core.governance.models import PolicyRequest, TrustedAuthContext, actor_kind_to_event_source
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
