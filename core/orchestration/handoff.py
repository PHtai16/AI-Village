"""Governed, evidence-based Worker-to-Worker Handoff coordinator for P05.06."""

from __future__ import annotations

from datetime import datetime
from fnmatch import fnmatch
import hashlib
import logging
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal, Sequence

from core.contracts.event import Event, EventSource, sanitize_event_payload
from core.contracts.identifiers import ensure_not_secret, new_event_id, validate_aware_datetime
from core.governance.manager import GovernanceAuditError, PermissionDeniedError, PermissionManager
from core.governance.models import PolicyRequest, TrustedAuthContext
from core.governance.workspace import validate_workspace_path
from core.orchestration.handoff_models import (
    TERMINAL_HANDOFF_STATUSES,
    VALID_HANDOFF_TRANSITIONS,
    DuplicateHandoffConflictError,
    HandoffAcknowledgeRequest,
    HandoffArtifactError,
    HandoffArtifactReference,
    HandoffContextPackage,
    HandoffCreateRequest,
    HandoffDeliveryRequest,
    HandoffDependencyError,
    HandoffError,
    HandoffGovernanceDeniedError,
    HandoffNotFoundError,
    HandoffRejectRequest,
    HandoffRevisionRecord,
    HandoffStateError,
    HandoffValidationError,
    StoredWorkerHandoff,
    WorkerHandoffRecord,
    WorkerHandoffRepository,
    new_handoff_id,
)
from core.orchestration.planning_models import PlanRepository, PlanStepSpec
from core.orchestration.team_models import TeamProposalRepository
from core.orchestration.worker_session_models import WorkerSessionRepository
from core.tasks.transitions import TERMINAL_RUN_STATUSES, TERMINAL_TASK_STATUSES

if TYPE_CHECKING:
    from core.agents.manifest import AgentManifest
    from core.agents.registry import AgentRegistryService
    from core.governance.approvals import ApprovalService

logger = logging.getLogger(__name__)

_LOCAL_ONLY_PROVIDERS: frozenset[str] = frozenset({"ollama", "local_llm", "llama_cpp"})


def _actor_kind_to_event_source(actor_kind: str) -> EventSource:
    """Map workflow/governance ``actor_kind`` into canonical ``EventSource``."""
    mapping: dict[str, EventSource] = {
        "human_operator": "operator",
        "operator": "operator",
        "master_agent": "orchestrator",
        "orchestrator": "orchestrator",
        "runtime_service": "core_runtime",
        "core_runtime": "core_runtime",
        "trusted_verifier": "qa_verifier",
        "qa_verifier": "qa_verifier",
        "worker_agent": "agent_runtime",
    }
    return mapping.get(actor_kind, "governance")


def _to_governance_actor_kind(
    actor_kind: str,
) -> Literal["human_operator", "master_agent", "worker_agent", "runtime_service", "trusted_verifier"]:
    """Map workflow ``ActorTrustKind`` onto Governance ``actor_kind`` literals."""
    mapping = {
        "operator": "human_operator",
        "orchestrator": "master_agent",
        "core_runtime": "runtime_service",
        "qa_verifier": "trusted_verifier",
        "worker_agent": "worker_agent",
        "governance": "runtime_service",
        "human_operator": "human_operator",
        "master_agent": "master_agent",
        "runtime_service": "runtime_service",
        "trusted_verifier": "trusted_verifier",
    }
    return mapping.get(actor_kind, "worker_agent")  # type: ignore[return-value]


def _build_trusted_auth(actor_id: str, actor_kind: str) -> TrustedAuthContext:
    """Build a verified ``TrustedAuthContext`` for Governance handoff authorization."""
    gov_kind = _to_governance_actor_kind(actor_kind)
    auth_source: Literal["application_boundary", "operator_session"] = (
        "operator_session" if gov_kind == "human_operator" else "application_boundary"
    )
    return TrustedAuthContext(
        actor_id=actor_id,
        actor_kind=gov_kind,
        auth_source=auth_source,
        authenticated=True,
    )


def _normalize_posix(raw_path: str) -> str:
    """Normalize a workspace-relative path to clean POSIX style."""
    return PurePosixPath(raw_path.strip().replace("\\", "/")).as_posix().rstrip("/")


def _scope_matches_patterns(
    target_rel: str,
    patterns: Sequence[str],
    *,
    task_id: str,
) -> bool:
    """Return True strictly when ``target_rel`` falls within at least one allowed pattern."""
    if not patterns:
        return False
    normalized = _normalize_posix(target_rel)
    probe_child = f"{normalized}/__probe__.tmp"
    for raw_pattern in patterns:
        expanded = _normalize_posix(raw_pattern.replace("{task_id}", task_id))
        if fnmatch(normalized, expanded) or fnmatch(probe_child, expanded):
            return True
        if "*" not in expanded and (normalized == expanded or normalized.startswith(f"{expanded}/")):
            return True
        if expanded.endswith("/*") and normalized == expanded[:-2]:
            return True
    return False


def _compute_file_sha256(file_path: Path) -> str:
    """Compute lowercase 64-char SHA-256 hex digest over real file bytes on disk."""
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        while chunk := handle.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


class WorkerHandoffService:
    """Evidence-validated, permission-checked Worker-to-Worker Handoff coordinator."""

    def __init__(
        self,
        *,
        handoff_repo: WorkerHandoffRepository,
        worker_session_repo: WorkerSessionRepository,
        task_repo: Any,
        run_repo: Any,
        metadata_repo: Any,
        plan_repo: PlanRepository,
        team_repo: TeamProposalRepository,
        agent_registry: AgentRegistryService,
        permission_manager: PermissionManager,
        approval_service: ApprovalService | None = None,
    ) -> None:
        """Inject explicit persistence, Registry, and Governance dependencies."""
        self._handoffs = handoff_repo
        self._worker_sessions = worker_session_repo
        self._tasks = task_repo
        self._runs = run_repo
        self._metadata = metadata_repo
        self._plans = plan_repo
        self._teams = team_repo
        self._registry = agent_registry
        self._permissions = permission_manager
        self._approvals = approval_service

    def _validate_and_hash_artifacts(
        self,
        *,
        artifact_ids: Sequence[str],
        task_id: str,
        run_id: str,
        from_agent_id: str,
        to_agent_id: str,
        receiver_manifest: AgentManifest,
        target_step: PlanStepSpec,
        workspace_root: str,
        require_verified_artifacts: bool,
        timestamp: datetime,
    ) -> tuple[list[HandoffArtifactReference], list[str]]:
        """Validate Artifact SQLite ownership, read permissions, on-disk existence, SHA-256, and QA proof."""
        if require_verified_artifacts and not artifact_ids:
            raise HandoffArtifactError(
                "MISSING_ARTIFACT",
                "Evidence-based handoff requires at least one verified Artifact ID.",
            )

        refs: list[HandoffArtifactReference] = []
        evidence_refs: list[str] = []

        for art_id in artifact_ids:
            artifact = self._metadata.get_artifact(art_id)
            if artifact is None:
                raise HandoffArtifactError(
                    "ARTIFACT_NOT_FOUND",
                    f"Artifact '{art_id}' does not exist in Metadata repository.",
                )
            if artifact.task_id != task_id or artifact.run_id != run_id:
                raise HandoffArtifactError(
                    "ARTIFACT_OWNERSHIP_MISMATCH",
                    (
                        f"Artifact '{art_id}' belongs to (task='{artifact.task_id}', run='{artifact.run_id}'), "
                        f"not (task='{task_id}', run='{run_id}')."
                    ),
                )
            if artifact.owner_agent_id != from_agent_id:
                raise HandoffArtifactError(
                    "ARTIFACT_OWNER_MISMATCH",
                    f"Artifact '{art_id}' is owned by '{artifact.owner_agent_id}', not sender '{from_agent_id}'.",
                )

            ws_check = validate_workspace_path(
                workspace_root,
                artifact.relative_path,
                action="files.read",
            )
            if not ws_check.allowed or ws_check.canonical_path is None:
                raise HandoffGovernanceDeniedError("WORKSPACE_VIOLATION", ws_check.message)

            if not _scope_matches_patterns(
                artifact.relative_path,
                receiver_manifest.permissions.fs_read,
                task_id=task_id,
            ):
                raise HandoffGovernanceDeniedError(
                    "UNAUTHORIZED_ARTIFACT_READ",
                    (
                        f"Receiver '{to_agent_id}' permissions.fs_read does not permit reading "
                        f"'{artifact.relative_path}'."
                    ),
                )
            if target_step.allowed_read_paths and not _scope_matches_patterns(
                artifact.relative_path,
                target_step.allowed_read_paths,
                task_id=task_id,
            ):
                raise HandoffGovernanceDeniedError(
                    "UNAUTHORIZED_ARTIFACT_READ",
                    f"Artifact path '{artifact.relative_path}' falls outside target step allowed_read_paths.",
                )

            disk_path = Path(ws_check.canonical_path)
            if not disk_path.exists() or not disk_path.is_file():
                raise HandoffArtifactError(
                    "ARTIFACT_FILE_MISSING",
                    f"Artifact file '{artifact.relative_path}' does not exist on disk.",
                )
            actual_sha256 = _compute_file_sha256(disk_path)
            if artifact.sha256 is None:
                raise HandoffArtifactError(
                    "ARTIFACT_MISSING_SHA256",
                    f"Artifact '{art_id}' has no recorded SHA-256 digest.",
                )
            if actual_sha256 != artifact.sha256:
                raise HandoffArtifactError(
                    "ARTIFACT_SHA256_MISMATCH",
                    (
                        f"Artifact '{art_id}' disk digest '{actual_sha256[:12]}...' mismatches "
                        f"recorded SHA-256 '{artifact.sha256[:12]}...'."
                    ),
                )

            if require_verified_artifacts:
                if artifact.verification_status != "VERIFIED_PASS":
                    raise HandoffArtifactError(
                        "ARTIFACT_UNVERIFIED",
                        (
                            f"Artifact '{art_id}' has verification_status='{artifact.verification_status}'; "
                            "handoff requires independent QA VERIFIED_PASS."
                        ),
                    )
                if artifact.evidence_reference is None:
                    raise HandoffArtifactError(
                        "MISSING_QA_EVIDENCE",
                        f"Artifact '{art_id}' lacks required independent QA evidence_reference.",
                    )

            if artifact.evidence_reference is not None and artifact.evidence_reference not in evidence_refs:
                evidence_refs.append(artifact.evidence_reference)

            refs.append(
                HandoffArtifactReference(
                    artifact_id=artifact.artifact_id,
                    task_id=artifact.task_id,
                    run_id=artifact.run_id,
                    owner_agent_id=artifact.owner_agent_id,
                    relative_path=artifact.relative_path,
                    sha256=actual_sha256,
                    version=artifact.version,
                    verification_status=artifact.verification_status,
                    evidence_reference=artifact.evidence_reference,
                    verified_at_handoff=timestamp,
                    integrity_checked=True,
                )
            )

        return refs, evidence_refs

    def _authorize_handoff_action(
        self,
        *,
        actor_id: str,
        actor_kind: str,
        task_id: str,
        run_id: str,
        handoff_id: str,
        workspace_root: str | None,
        target_path: str | None,
        timestamp: datetime,
        approval_id: str | None,
        correlation_id: str,
        parameters: dict[str, Any],
    ) -> None:
        """Enforce Governance PolicyEvaluator and ApprovalService cleanly for ``agent.handoff``."""
        auth_ctx = _build_trusted_auth(actor_id, actor_kind)
        req = PolicyRequest(
            auth_context=auth_ctx,
            action="agent.handoff",
            resource=f"task:{task_id}/run:{run_id}/handoff:{handoff_id}",
            task_id=task_id,
            run_id=run_id,
            timestamp=timestamp,
            workspace_root=workspace_root,
            target_path=target_path,
            approval_id=approval_id,
            correlation_id=correlation_id,
            parameters=parameters,
        )
        try:
            decision = self._permissions.evaluator.evaluate(req)
            if approval_id is not None or decision.decision == "APPROVAL_REQUIRED":
                self._permissions.dispatch_protected_action(req, lambda: True)
                return
            audited = self._permissions.evaluate_and_audit(req)
            if audited.decision != "ALLOW":
                raise HandoffGovernanceDeniedError(audited.reason_code, audited.message)
        except PermissionDeniedError as exc:
            raise HandoffGovernanceDeniedError(exc.decision.reason_code, exc.decision.message) from exc
        except GovernanceAuditError as exc:
            raise HandoffError("GOVERNANCE_AUDIT_FAILED", str(exc)) from exc
        except HandoffGovernanceDeniedError:
            raise
        except RuntimeError as exc:
            # Handle ApprovalError (APPROVAL_EXPIRED, APPROVAL_REJECTED, etc.) fail-closed
            err_code = getattr(exc, "error_code", "APPROVAL_ERROR")
            raise HandoffGovernanceDeniedError(str(err_code), str(exc)) from exc

    def _validate_receiver_and_routing(
        self,
        *,
        task_id: str,
        stored_team_id: str,
        to_agent_id: str,
        target_step: PlanStepSpec,
        plan_residency: str,
        workspace_root: str,
        target_workspace_scope: str,
    ) -> AgentManifest:
        """Verify receiver is ACTIVE, manifest pin is non-stale, and routing/workspace comply."""
        stored_team = self._teams.get_proposal(stored_team_id)
        if stored_team is None:
            raise HandoffDependencyError("TEAM_PROPOSAL_NOT_FOUND", f"TeamProposal '{stored_team_id}' missing.")
        if stored_team.status == "STALE":
            raise HandoffDependencyError(
                "STALE_MANIFEST_REVISION",
                f"TeamProposal '{stored_team_id}' is marked STALE; handoff blocked.",
            )

        pinned_receiver = next(
            (m for m in stored_team.proposal.pinned_members if m.agent_id == to_agent_id),
            None,
        )
        if pinned_receiver is None:
            raise HandoffDependencyError(
                "UNAUTHORIZED_RECEIVER",
                f"Receiver '{to_agent_id}' is missing from TeamProposal '{stored_team_id}'.",
            )

        live_receiver = self._registry.get_agent(to_agent_id)
        if live_receiver is None or live_receiver.registration_status != "ACTIVE":
            status_label = live_receiver.registration_status if live_receiver else "MISSING"
            raise HandoffDependencyError(
                "DISABLED_RECEIVER",
                f"Receiver '{to_agent_id}' cannot receive handoffs in status '{status_label}'.",
            )
        if (
            live_receiver.active_revision != pinned_receiver.manifest_revision
            or live_receiver.manifest_sha256 != pinned_receiver.manifest_sha256
        ):
            raise HandoffDependencyError(
                "STALE_MANIFEST_REVISION",
                (
                    f"Receiver '{to_agent_id}' manifest drifted from pinned r{pinned_receiver.manifest_revision} "
                    f"to live r{live_receiver.active_revision}."
                ),
            )

        pinned_ver = self._registry.get_manifest_version(
            to_agent_id,
            pinned_receiver.manifest_revision,
        )
        if pinned_ver is None or pinned_ver.manifest.canonical_sha256() != pinned_receiver.manifest_sha256:
            raise HandoffDependencyError(
                "MANIFEST_HASH_DRIFT",
                f"Pinned manifest r{pinned_receiver.manifest_revision} integrity check failed for '{to_agent_id}'.",
            )
        receiver_manifest = pinned_ver.manifest

        ws_check = validate_workspace_path(workspace_root, target_workspace_scope, action="files.read")
        if not ws_check.allowed:
            raise HandoffGovernanceDeniedError("WORKSPACE_VIOLATION", ws_check.message)

        if target_step.required_tools and not (
            set(target_step.required_tools) <= set(receiver_manifest.tools.allowed)
        ):
            raise HandoffGovernanceDeniedError(
                "UNAUTHORIZED_RECEIVER",
                f"Receiver '{to_agent_id}' lacks required tools {target_step.required_tools}.",
            )

        effective_residency = (
            target_step.routing_proposal.data_residency_policy
            if target_step.routing_proposal is not None
            else plan_residency
        )
        receiver_provider = pinned_receiver.provider_id or receiver_manifest.engine.provider_id or receiver_manifest.engine.default_type
        if effective_residency == "local_only" and receiver_provider not in _LOCAL_ONLY_PROVIDERS:
            raise HandoffGovernanceDeniedError(
                "WRONG_PROVIDER_ROUTING",
                (
                    f"Receiver '{to_agent_id}' uses cloud/remote provider '{receiver_provider}', "
                    "violating local_only data residency policy."
                ),
            )
        if target_step.routing_proposal is not None:
            req_prov = target_step.routing_proposal.provider_id
            req_conn = target_step.routing_proposal.connection_id
            if req_prov is not None and receiver_provider != req_prov:
                raise HandoffGovernanceDeniedError(
                    "WRONG_PROVIDER_ROUTING",
                    f"Receiver provider '{receiver_provider}' mismatches target step provider '{req_prov}'.",
                )
            receiver_conn = pinned_receiver.connection_id or receiver_manifest.engine.connection_id
            if req_conn is not None and receiver_conn != req_conn:
                raise HandoffGovernanceDeniedError(
                    "WRONG_PROVIDER_ROUTING",
                    f"Receiver connection '{receiver_conn}' mismatches target step connection '{req_conn}'.",
                )

        return receiver_manifest

    def create_and_validate_handoff(
        self,
        request: HandoffCreateRequest,
    ) -> StoredWorkerHandoff:
        """Validate DAG dependencies, Artifact SHA-256 + QA proof, permissions, and persist READY Handoff."""
        validate_aware_datetime(request.timestamp, "timestamp")

        if request.from_agent_id == request.to_agent_id and not request.allow_self_handoff:
            raise HandoffValidationError(
                "SELF_HANDOFF_FORBIDDEN",
                f"Self-handoff for '{request.from_agent_id}' is disallowed unless explicitly permitted.",
            )

        # 1. Task, Run, Plan, Team ownership checks
        task = self._tasks.get_task(request.task_id)
        if task is None:
            raise HandoffDependencyError("TASK_NOT_FOUND", f"Task '{request.task_id}' does not exist.")
        if task.status in TERMINAL_TASK_STATUSES:
            raise HandoffDependencyError("TASK_TERMINAL", f"Task '{task.task_id}' is already '{task.status}'.")

        run = self._runs.get_run(request.run_id)
        if run is None:
            raise HandoffDependencyError("RUN_NOT_FOUND", f"Run '{request.run_id}' does not exist.")
        if run.task_id != task.task_id:
            raise HandoffDependencyError(
                "WRONG_RUN_TASK_OWNERSHIP",
                f"Run '{run.run_id}' belongs to Task '{run.task_id}', not '{task.task_id}'.",
            )
        if run.status in TERMINAL_RUN_STATUSES:
            raise HandoffDependencyError("RUN_TERMINAL", f"Run '{run.run_id}' is already '{run.status}'.")

        stored_plan = self._plans.get_plan(request.plan_id)
        if stored_plan is None or stored_plan.task_id != task.task_id:
            raise HandoffDependencyError("PLAN_TASK_MISMATCH", f"Plan '{request.plan_id}' missing or owned by another Task.")
        if stored_plan.status != "APPLIED":
            raise HandoffDependencyError("PLAN_NOT_APPLIED", f"Plan '{stored_plan.plan_id}' is '{stored_plan.status}', not APPLIED.")

        stored_team = self._teams.get_proposal(request.team_proposal_id)
        if stored_team is None or stored_team.task_id != task.task_id:
            raise HandoffDependencyError("TEAM_TASK_MISMATCH", f"TeamProposal '{request.team_proposal_id}' missing or foreign.")
        if (
            stored_team.plan_id != stored_plan.plan_id
            or stored_team.plan_revision != stored_plan.active_revision
        ):
            raise HandoffDependencyError("TEAM_PLAN_MISMATCH", "TeamProposal does not match active plan revision.")
        if stored_team.status != "ASSIGNED":
            raise HandoffDependencyError("TEAM_NOT_ASSIGNED", f"TeamProposal status is '{stored_team.status}', expected ASSIGNED.")

        # 2. Step presence & DAG dependency check (forbids missing steps & reverse-DAG)
        step_by_id = {s.step_id: s for s in stored_plan.plan.steps}
        source_step = step_by_id.get(request.source_step_id)
        if source_step is None:
            raise HandoffDependencyError("MISSING_SOURCE_STEP", f"Source step '{request.source_step_id}' not in Plan.")
        target_step = step_by_id.get(request.target_step_id)
        if target_step is None:
            raise HandoffDependencyError("MISSING_TARGET_STEP", f"Target step '{request.target_step_id}' not in Plan.")
        if request.source_step_id not in target_step.depends_on:
            raise HandoffDependencyError(
                "INVALID_STEP_DEPENDENCY",
                (
                    f"Target step '{request.target_step_id}' does not depend on source step "
                    f"'{request.source_step_id}' (reverse or unrelated DAG handoff blocked)."
                ),
            )

        # 3. Sender / Receiver assignment verification
        assign_by_step = {a.step_id: a for a in stored_team.proposal.step_assignments}
        src_assign = assign_by_step.get(request.source_step_id)
        if src_assign is None or src_assign.assigned_agent_id != request.from_agent_id:
            raise HandoffDependencyError(
                "INVALID_SENDER",
                f"Sender '{request.from_agent_id}' is not assigned owner of step '{request.source_step_id}'.",
            )
        tgt_assign = assign_by_step.get(request.target_step_id)
        if tgt_assign is None or tgt_assign.assigned_agent_id != request.to_agent_id:
            raise HandoffDependencyError(
                "UNAUTHORIZED_RECEIVER",
                f"Receiver '{request.to_agent_id}' is not assigned owner of target step '{request.target_step_id}'.",
            )

        # 4. Source Worker Session ownership & completion readiness
        src_stored = self._worker_sessions.get_stored_session(request.from_session_id)
        if src_stored is None:
            raise HandoffDependencyError("SOURCE_SESSION_NOT_FOUND", f"Source session '{request.from_session_id}' not found.")
        if (
            src_stored.binding.task_id != task.task_id
            or src_stored.binding.run_id != run.run_id
            or src_stored.binding.agent_id != request.from_agent_id
            or src_stored.binding.step_id != request.source_step_id
        ):
            raise HandoffDependencyError(
                "SOURCE_SESSION_OWNERSHIP_MISMATCH",
                "Source session does not match (task_id, run_id, from_agent_id, source_step_id).",
            )
        if src_stored.session.status != "CLOSED":
            raise HandoffDependencyError(
                "SOURCE_NOT_READY",
                (
                    f"Source session '{request.from_session_id}' has status '{src_stored.session.status}'; "
                    "step must reach CLOSED cleanly before creating handoff."
                ),
            )

        # 5. Validate receiver status, pinned non-stale manifest, workspace, tools, routing
        receiver_manifest = self._validate_receiver_and_routing(
            task_id=task.task_id,
            stored_team_id=stored_team.proposal_id,
            to_agent_id=request.to_agent_id,
            target_step=target_step,
            plan_residency=stored_plan.plan.routing_proposal.data_residency_policy,
            workspace_root=request.workspace_root,
            target_workspace_scope=request.target_workspace_scope,
        )

        # 6. Validate Artifacts & independent QA evidence on SQLite + Disk
        artifact_refs, derived_evidence = self._validate_and_hash_artifacts(
            artifact_ids=request.artifact_ids,
            task_id=task.task_id,
            run_id=run.run_id,
            from_agent_id=request.from_agent_id,
            to_agent_id=request.to_agent_id,
            receiver_manifest=receiver_manifest,
            target_step=target_step,
            workspace_root=request.workspace_root,
            require_verified_artifacts=request.require_verified_artifacts,
            timestamp=request.timestamp,
        )
        all_evidence = list(dict.fromkeys(list(request.evidence_references) + derived_evidence))

        handoff_id = request.handoff_id or new_handoff_id()
        correlation_id = request.correlation_id or src_stored.binding.correlation_id

        # 7. Governance permission & anti-escalation check
        self._authorize_handoff_action(
            actor_id=request.actor_id,
            actor_kind=request.actor_kind,
            task_id=task.task_id,
            run_id=run.run_id,
            handoff_id=handoff_id,
            workspace_root=request.workspace_root,
            target_path=request.target_workspace_scope,
            timestamp=request.timestamp,
            approval_id=request.approval_id,
            correlation_id=correlation_id,
            parameters={
                "operation": "create_handoff",
                "from_agent_id": request.from_agent_id,
                "to_agent_id": request.to_agent_id,
                "source_step_id": request.source_step_id,
                "target_step_id": request.target_step_id,
                "artifact_ids": list(request.artifact_ids),
                **request.policy_parameters,
            },
        )

        # 8. Prevent duplicate active edge handoff if distinct non-rejected handoff exists
        if request.handoff_id is None:
            existing_edge = self._handoffs.list_handoffs(
                task_id=task.task_id,
                run_id=run.run_id,
                source_step_id=request.source_step_id,
                target_step_id=request.target_step_id,
            )
            non_rejected = [h for h in existing_edge if h.handoff.status != "REJECTED"]
            if non_rejected:
                raise DuplicateHandoffConflictError(
                    "DUPLICATE_STEP_HANDOFF",
                    (
                        f"Active handoff '{non_rejected[0].handoff.handoff_id}' already exists for "
                        f"'{request.source_step_id}' -> '{request.target_step_id}'."
                    ),
                )

        # 9. Build bounded minimal HandoffContextPackage with untrusted-data boundary
        context_pkg = HandoffContextPackage(
            target_step_id=target_step.step_id,
            target_step_goal=target_step.description[:1000],
            constraints=list(target_step.allowed_write_paths),
            source_summary=request.input_context_summary,
            artifact_references=artifact_refs,
            evidence_references=all_evidence,
            open_issues=list(request.open_issues),
            decision_references=list(request.decision_references),
            acceptance_criteria=list(target_step.acceptance_criteria),
            allowed_workspace_scope=request.target_workspace_scope,
        )

        record = WorkerHandoffRecord(
            handoff_id=handoff_id,
            task_id=task.task_id,
            run_id=run.run_id,
            plan_id=stored_plan.plan_id,
            plan_revision=stored_plan.active_revision,
            team_proposal_id=stored_team.proposal_id,
            source_step_id=source_step.step_id,
            target_step_id=target_step.step_id,
            from_agent_id=request.from_agent_id,
            to_agent_id=request.to_agent_id,
            from_session_id=request.from_session_id,
            to_session_id=None,
            artifact_references=artifact_refs,
            evidence_references=all_evidence,
            decision_references=list(request.decision_references),
            input_context_summary=request.input_context_summary,
            expected_next_action=request.expected_next_action,
            acceptance_requirements=list(target_step.acceptance_criteria),
            context_package=context_pkg,
            status="READY",
            created_at=request.timestamp,
            correlation_id=correlation_id,
        )
        content_sha256 = record.compute_content_sha256()
        record = record.model_copy(update={"content_sha256": content_sha256})

        audit_event = Event(
            event_id=new_event_id(),
            timestamp=request.timestamp,
            event_type="agent.handoff",
            source=_actor_kind_to_event_source(request.actor_kind),
            task_id=task.task_id,
            run_id=run.run_id,
            session_id=request.from_session_id,
            actor_id=request.actor_id,
            artifact_id=artifact_refs[0].artifact_id if artifact_refs else None,
            correlation_id=correlation_id,
            redacted_payload=sanitize_event_payload(
                {
                    "handoff_phase": "ready",
                    "handoff_id": record.handoff_id,
                    "from_agent_id": record.from_agent_id,
                    "to_agent_id": record.to_agent_id,
                    "from_session_id": record.from_session_id,
                    "source_step_id": record.source_step_id,
                    "target_step_id": record.target_step_id,
                    "artifact_ids": [a.artifact_id for a in artifact_refs],
                    "evidence_references": all_evidence,
                    "content_sha256": content_sha256,
                }
            ),
        )
        rev_record = HandoffRevisionRecord(
            handoff_id=record.handoff_id,
            task_id=task.task_id,
            run_id=run.run_id,
            record_version=1,
            status="READY",
            actor_id=request.actor_id,
            reason=f"Validated and created handoff '{record.handoff_id}' READY for Worker '{record.to_agent_id}'.",
            content_sha256=content_sha256,
            event_id=audit_event.event_id,
            timestamp=request.timestamp,
        )
        return self._handoffs.create_handoff_atomic(
            handoff=record,
            revision=rev_record,
            event=audit_event,
        )

    def deliver_handoff(
        self,
        request: HandoffDeliveryRequest,
    ) -> StoredWorkerHandoff:
        """Deliver a READY handoff to Worker B after re-verifying receiver & artifact integrity."""
        validate_aware_datetime(request.timestamp, "timestamp")
        stored = self._handoffs.get_handoff(request.handoff_id)
        if stored is None:
            raise HandoffNotFoundError("HANDOFF_NOT_FOUND", f"Handoff '{request.handoff_id}' does not exist.")

        rec = stored.handoff
        # Idempotent delivery when already DELIVERED to the exact same target session (or None)
        if rec.status == "DELIVERED" and rec.to_session_id == request.to_session_id:
            return stored

        if "DELIVERED" not in VALID_HANDOFF_TRANSITIONS[rec.status]:
            raise HandoffStateError(
                "INVALID_HANDOFF_TRANSITION",
                f"Handoff '{rec.handoff_id}' cannot transition {rec.status} -> DELIVERED.",
            )

        stored_plan = self._plans.get_plan(rec.plan_id)
        if stored_plan is None or stored_plan.status != "APPLIED":
            raise HandoffDependencyError("PLAN_NOT_APPLIED", f"Plan '{rec.plan_id}' is not APPLIED.")
        target_step = next((s for s in stored_plan.plan.steps if s.step_id == rec.target_step_id), None)
        if target_step is None:
            raise HandoffDependencyError("MISSING_TARGET_STEP", f"Target step '{rec.target_step_id}' missing.")

        receiver_manifest = self._validate_receiver_and_routing(
            task_id=rec.task_id,
            stored_team_id=rec.team_proposal_id,
            to_agent_id=rec.to_agent_id,
            target_step=target_step,
            plan_residency=stored_plan.plan.routing_proposal.data_residency_policy,
            workspace_root=request.workspace_root,
            target_workspace_scope=rec.context_package.allowed_workspace_scope,
        )

        # Re-verify artifacts right at delivery to detect post-verification modification
        self._validate_and_hash_artifacts(
            artifact_ids=[a.artifact_id for a in rec.artifact_references],
            task_id=rec.task_id,
            run_id=rec.run_id,
            from_agent_id=rec.from_agent_id,
            to_agent_id=rec.to_agent_id,
            receiver_manifest=receiver_manifest,
            target_step=target_step,
            workspace_root=request.workspace_root,
            require_verified_artifacts=request.require_verified_artifacts,
            timestamp=request.timestamp,
        )

        if request.to_session_id is not None:
            tgt_stored = self._worker_sessions.get_stored_session(request.to_session_id)
            if tgt_stored is None:
                raise HandoffDependencyError("TARGET_SESSION_NOT_FOUND", f"Session '{request.to_session_id}' does not exist.")
            if (
                tgt_stored.binding.task_id != rec.task_id
                or tgt_stored.binding.run_id != rec.run_id
                or tgt_stored.binding.agent_id != rec.to_agent_id
                or tgt_stored.binding.step_id != rec.target_step_id
            ):
                raise HandoffDependencyError(
                    "TARGET_SESSION_OWNERSHIP_MISMATCH",
                    "Target session does not match handoff (task_id, run_id, to_agent_id, target_step_id).",
                )
            if tgt_stored.session.status not in {"INITIALIZED", "ACTIVE", "IDLE"}:
                raise HandoffStateError(
                    "TARGET_SESSION_TERMINAL",
                    f"Cannot bind terminal session '{request.to_session_id}' ({tgt_stored.session.status}).",
                )

        corr_id = request.correlation_id or rec.correlation_id
        self._authorize_handoff_action(
            actor_id=request.actor_id,
            actor_kind=request.actor_kind,
            task_id=rec.task_id,
            run_id=rec.run_id,
            handoff_id=rec.handoff_id,
            workspace_root=request.workspace_root,
            target_path=rec.context_package.allowed_workspace_scope,
            timestamp=request.timestamp,
            approval_id=request.approval_id,
            correlation_id=corr_id,
            parameters={
                "operation": "deliver_handoff",
                "from_agent_id": rec.from_agent_id,
                "to_agent_id": rec.to_agent_id,
                "to_session_id": request.to_session_id,
            },
        )

        next_rec = rec.model_copy(
            update={
                "status": "DELIVERED",
                "to_session_id": request.to_session_id,
                "delivered_at": request.timestamp,
            }
        )
        next_ver = stored.record_version + 1
        event = Event(
            event_id=new_event_id(),
            timestamp=request.timestamp,
            event_type="agent.handoff",
            source=_actor_kind_to_event_source(request.actor_kind),
            task_id=rec.task_id,
            run_id=rec.run_id,
            session_id=request.to_session_id or rec.from_session_id,
            actor_id=request.actor_id,
            artifact_id=rec.artifact_references[0].artifact_id if rec.artifact_references else None,
            correlation_id=corr_id,
            redacted_payload=sanitize_event_payload(
                {
                    "handoff_phase": "delivered",
                    "handoff_id": rec.handoff_id,
                    "from_agent_id": rec.from_agent_id,
                    "to_agent_id": rec.to_agent_id,
                    "from_session_id": rec.from_session_id,
                    "to_session_id": request.to_session_id,
                    "source_step_id": rec.source_step_id,
                    "target_step_id": rec.target_step_id,
                }
            ),
        )
        rev = HandoffRevisionRecord(
            handoff_id=rec.handoff_id,
            task_id=rec.task_id,
            run_id=rec.run_id,
            record_version=next_ver,
            status="DELIVERED",
            actor_id=request.actor_id,
            reason=f"Delivered handoff '{rec.handoff_id}' to Worker '{rec.to_agent_id}'.",
            content_sha256=rec.content_sha256 or rec.compute_content_sha256(),
            event_id=event.event_id,
            timestamp=request.timestamp,
        )
        return self._handoffs.transition_handoff_atomic(
            handoff=next_rec,
            expected_record_version=stored.record_version,
            revision=rev,
            event=event,
        )

    def acknowledge_handoff(
        self,
        request: HandoffAcknowledgeRequest,
    ) -> StoredWorkerHandoff:
        """Record explicit Worker B acknowledgment without implying step or Task completion."""
        validate_aware_datetime(request.timestamp, "timestamp")
        stored = self._handoffs.get_handoff(request.handoff_id)
        if stored is None:
            raise HandoffNotFoundError("HANDOFF_NOT_FOUND", f"Handoff '{request.handoff_id}' does not exist.")

        rec = stored.handoff
        if "ACKNOWLEDGED" not in VALID_HANDOFF_TRANSITIONS[rec.status]:
            raise HandoffStateError(
                "INVALID_HANDOFF_TRANSITION",
                f"Handoff '{rec.handoff_id}' in status '{rec.status}' cannot be acknowledged.",
            )

        if request.actor_kind == "worker_agent" and request.actor_id != rec.to_agent_id:
            raise HandoffGovernanceDeniedError(
                "UNAUTHORIZED_ACKNOWLEDGE",
                f"Only receiver '{rec.to_agent_id}' (not '{request.actor_id}') may acknowledge this handoff.",
            )

        resolved_to_session = rec.to_session_id or request.to_session_id
        if rec.to_session_id is not None and request.to_session_id is not None and request.to_session_id != rec.to_session_id:
            raise HandoffStateError(
                "TARGET_SESSION_MISMATCH",
                f"Acknowledge session '{request.to_session_id}' mismatches delivered session '{rec.to_session_id}'.",
            )
        if resolved_to_session is not None:
            tgt_stored = self._worker_sessions.get_stored_session(resolved_to_session)
            if (
                tgt_stored is None
                or tgt_stored.binding.task_id != rec.task_id
                or tgt_stored.binding.run_id != rec.run_id
                or tgt_stored.binding.agent_id != rec.to_agent_id
            ):
                raise HandoffDependencyError(
                    "TARGET_SESSION_OWNERSHIP_MISMATCH",
                    f"Session '{resolved_to_session}' does not belong to receiver '{rec.to_agent_id}'.",
                )

        corr_id = request.correlation_id or rec.correlation_id
        self._authorize_handoff_action(
            actor_id=request.actor_id,
            actor_kind=request.actor_kind,
            task_id=rec.task_id,
            run_id=rec.run_id,
            handoff_id=rec.handoff_id,
            workspace_root=None,
            target_path=None,
            timestamp=request.timestamp,
            approval_id=None,
            correlation_id=corr_id,
            parameters={"operation": "acknowledge_handoff", "to_agent_id": rec.to_agent_id},
        )

        next_rec = rec.model_copy(
            update={
                "status": "ACKNOWLEDGED",
                "to_session_id": resolved_to_session,
                "acknowledged_at": request.timestamp,
            }
        )
        next_ver = stored.record_version + 1
        event = Event(
            event_id=new_event_id(),
            timestamp=request.timestamp,
            event_type="agent.handoff",
            source=_actor_kind_to_event_source(request.actor_kind),
            task_id=rec.task_id,
            run_id=rec.run_id,
            session_id=resolved_to_session or rec.from_session_id,
            actor_id=request.actor_id,
            artifact_id=rec.artifact_references[0].artifact_id if rec.artifact_references else None,
            correlation_id=corr_id,
            redacted_payload=sanitize_event_payload(
                {
                    "handoff_phase": "acknowledged",
                    "handoff_id": rec.handoff_id,
                    "from_agent_id": rec.from_agent_id,
                    "to_agent_id": rec.to_agent_id,
                    "to_session_id": resolved_to_session,
                    "note": ensure_not_secret(request.note, "note"),
                }
            ),
        )
        rev = HandoffRevisionRecord(
            handoff_id=rec.handoff_id,
            task_id=rec.task_id,
            run_id=rec.run_id,
            record_version=next_ver,
            status="ACKNOWLEDGED",
            actor_id=request.actor_id,
            reason=f"Receiver '{rec.to_agent_id}' acknowledged handoff: {request.note}",
            content_sha256=rec.content_sha256 or rec.compute_content_sha256(),
            event_id=event.event_id,
            timestamp=request.timestamp,
        )
        return self._handoffs.transition_handoff_atomic(
            handoff=next_rec,
            expected_record_version=stored.record_version,
            revision=rev,
            event=event,
        )

    def reject_handoff(
        self,
        request: HandoffRejectRequest,
    ) -> StoredWorkerHandoff:
        """Reject a READY or DELIVERED handoff fail-closed with audited rejection rationale."""
        validate_aware_datetime(request.timestamp, "timestamp")
        stored = self._handoffs.get_handoff(request.handoff_id)
        if stored is None:
            raise HandoffNotFoundError("HANDOFF_NOT_FOUND", f"Handoff '{request.handoff_id}' does not exist.")

        rec = stored.handoff
        if rec.status in TERMINAL_HANDOFF_STATUSES or "REJECTED" not in VALID_HANDOFF_TRANSITIONS[rec.status]:
            raise HandoffStateError(
                "INVALID_HANDOFF_TRANSITION",
                f"Handoff '{rec.handoff_id}' in status '{rec.status}' cannot be rejected.",
            )

        if request.actor_kind == "worker_agent" and request.actor_id not in {rec.to_agent_id, rec.from_agent_id}:
            raise HandoffGovernanceDeniedError(
                "UNAUTHORIZED_REJECT",
                f"Worker '{request.actor_id}' is not a participant of handoff '{rec.handoff_id}'.",
            )

        corr_id = request.correlation_id or rec.correlation_id
        self._authorize_handoff_action(
            actor_id=request.actor_id,
            actor_kind=request.actor_kind,
            task_id=rec.task_id,
            run_id=rec.run_id,
            handoff_id=rec.handoff_id,
            workspace_root=None,
            target_path=None,
            timestamp=request.timestamp,
            approval_id=None,
            correlation_id=corr_id,
            parameters={"operation": "reject_handoff"},
        )

        next_rec = rec.model_copy(
            update={
                "status": "REJECTED",
                "rejected_at": request.timestamp,
                "rejection_reason": request.rejection_reason,
            }
        )
        next_ver = stored.record_version + 1
        event = Event(
            event_id=new_event_id(),
            timestamp=request.timestamp,
            event_type="agent.handoff",
            source=_actor_kind_to_event_source(request.actor_kind),
            task_id=rec.task_id,
            run_id=rec.run_id,
            session_id=rec.to_session_id or rec.from_session_id,
            actor_id=request.actor_id,
            artifact_id=rec.artifact_references[0].artifact_id if rec.artifact_references else None,
            correlation_id=corr_id,
            redacted_payload=sanitize_event_payload(
                {
                    "handoff_phase": "rejected",
                    "handoff_id": rec.handoff_id,
                    "from_agent_id": rec.from_agent_id,
                    "to_agent_id": rec.to_agent_id,
                    "rejection_reason": request.rejection_reason,
                }
            ),
        )
        rev = HandoffRevisionRecord(
            handoff_id=rec.handoff_id,
            task_id=rec.task_id,
            run_id=rec.run_id,
            record_version=next_ver,
            status="REJECTED",
            actor_id=request.actor_id,
            reason=request.rejection_reason,
            content_sha256=rec.content_sha256 or rec.compute_content_sha256(),
            event_id=event.event_id,
            timestamp=request.timestamp,
        )
        return self._handoffs.transition_handoff_atomic(
            handoff=next_rec,
            expected_record_version=stored.record_version,
            revision=rev,
            event=event,
        )
