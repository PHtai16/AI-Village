"""Governed Agent Recruitment workflow: propose, evaluate, and activate only when valid."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from core.agents.manifest import AgentManifest
from core.agents.recruitment_models import (
    TERMINAL_RECRUITMENT_STATUSES,
    VALID_RECRUITMENT_TRANSITIONS,
    RecruitmentActivationRequest,
    RecruitmentEvaluationRequest,
    RecruitmentEvaluationResult,
    RecruitmentGovernanceError,
    RecruitmentNotFoundError,
    RecruitmentProposalRecord,
    RecruitmentProposalRequest,
    RecruitmentRejectRequest,
    RecruitmentRepository,
    RecruitmentRevisionRecord,
    RecruitmentStateError,
    RecruitmentValidationError,
    StoredRecruitmentProposal,
    new_recruitment_id,
)
from core.agents.registry import (
    AgentManifestValidationError,
    AgentRegistryError,
    AgentRegistryService,
    _manifest_has_sensitive_initial_grants,
)
from core.contracts.event import Event, sanitize_event_payload
from core.contracts.identifiers import new_event_id, validate_aware_datetime
from core.governance.manager import GovernanceAuditError, PermissionDeniedError, PermissionManager
from core.governance.models import (
    PolicyRequest,
    TrustedAuthContext,
    actor_kind_to_event_source,
    resolve_trusted_auth_context,
)
from core.governance.workspace import validate_workspace_path

if TYPE_CHECKING:
    from engines.connections import ConnectionRegistry
    from engines.registry import EngineRegistry

logger = logging.getLogger(__name__)


class AgentRecruitmentService:
    """Proposes, evaluates, and gates activation of new Worker Agents via ``AgentRegistryService``."""

    def __init__(
        self,
        *,
        recruitment_repo: RecruitmentRepository,
        agent_registry: AgentRegistryService,
        permission_manager: PermissionManager,
        engine_registry: EngineRegistry | None = None,
        connection_registry: ConnectionRegistry | None = None,
    ) -> None:
        """Inject explicit persistence, Registry, Governance, and optional routing registries."""
        self._repo = recruitment_repo
        self._registry = agent_registry
        self._pm = permission_manager
        self._engines = engine_registry
        self._connections = connection_registry

    def _authorize_recruit(
        self,
        *,
        auth_context: TrustedAuthContext | None = None,
        actor_id: str,
        actor_kind: str,
        candidate_agent_id: str,
        proposal_id: str,
        timestamp: Any,
        correlation_id: str,
        parameters: dict[str, Any],
    ) -> TrustedAuthContext:
        """Enforce Governance ``agent.recruit`` action fail-closed."""
        auth = resolve_trusted_auth_context(
            auth_context=auth_context,
            actor_id=actor_id,
            actor_kind=actor_kind,
        )
        req = PolicyRequest(
            auth_context=auth,
            action="agent.recruit",
            resource=f"agent:{candidate_agent_id}/recruitment:{proposal_id}",
            timestamp=timestamp,
            correlation_id=correlation_id,
            parameters=parameters,
        )
        try:
            decision = self._pm.evaluate_and_audit(req)
        except PermissionDeniedError as exc:
            raise RecruitmentGovernanceError(exc.decision.reason_code, exc.decision.message) from exc
        except GovernanceAuditError as exc:
            raise RecruitmentGovernanceError("GOVERNANCE_AUDIT_FAILED", str(exc)) from exc
        if decision.decision != "ALLOW":
            raise RecruitmentGovernanceError(decision.reason_code, decision.message)
        return auth

    def propose_candidate(
        self,
        request: RecruitmentProposalRequest,
    ) -> StoredRecruitmentProposal:
        """Validate candidate role, tools, and permissions and persist a PROPOSED recruitment record."""
        validate_aware_datetime(request.timestamp, "timestamp")
        try:
            manifest = self._registry.validate_manifest(request.candidate_manifest)
        except AgentManifestValidationError as exc:
            raise RecruitmentValidationError(exc.error_code, exc.message) from exc

        if manifest.id == "core.master" or manifest.system_role != "worker":
            raise RecruitmentValidationError(
                "MASTER_IMPERSONATION_FORBIDDEN",
                "Recruitment can only propose worker agents, never core.master or orchestrator roles.",
            )

        if self._registry.get_agent(manifest.id) is not None:
            raise RecruitmentStateError(
                "AGENT_ALREADY_REGISTERED",
                f"Candidate agent '{manifest.id}' already exists in AgentRegistryService.",
            )

        existing = self._repo.list_proposals(candidate_agent_id=manifest.id)
        active = [p for p in existing if p.proposal.status not in TERMINAL_RECRUITMENT_STATUSES]
        if active:
            raise RecruitmentStateError(
                "DUPLICATE_ACTIVE_RECRUITMENT",
                f"Active recruitment proposal '{active[0].proposal.proposal_id}' already exists for '{manifest.id}'.",
            )

        proposal_id = request.proposal_id or new_recruitment_id()
        corr_id = request.correlation_id or f"corr-recruit-{proposal_id}"
        self._authorize_recruit(
            auth_context=request.auth_context,
            actor_id=request.proposer_id,
            actor_kind=request.proposer_kind,
            candidate_agent_id=manifest.id,
            proposal_id=proposal_id,
            timestamp=request.timestamp,
            correlation_id=corr_id,
            parameters={"operation": "propose_candidate", "department": manifest.department},
        )

        manifest_sha = manifest.canonical_sha256()
        record = RecruitmentProposalRecord(
            proposal_id=proposal_id,
            candidate_agent_id=manifest.id,
            candidate_manifest=manifest,
            manifest_sha256=manifest_sha,
            rationale=request.rationale,
            needed_capabilities=list(request.needed_capabilities),
            task_id=request.task_id,
            proposed_by=request.proposer_id,
            proposed_by_kind=request.proposer_kind,
            status="PROPOSED",
            correlation_id=corr_id,
            created_at=request.timestamp,
        )
        event = Event(
            event_id=new_event_id(),
            timestamp=request.timestamp,
            event_type="agent.recruitment_proposed",
            source=actor_kind_to_event_source(request.proposer_kind),
            task_id=request.task_id,
            actor_id=request.proposer_id,
            correlation_id=corr_id,
            redacted_payload=sanitize_event_payload(
                {
                    "proposal_id": proposal_id,
                    "candidate_agent_id": manifest.id,
                    "department": manifest.department,
                    "manifest_sha256": manifest_sha,
                    "needed_capabilities": list(request.needed_capabilities),
                }
            ),
        )
        revision = RecruitmentRevisionRecord(
            proposal_id=proposal_id,
            candidate_agent_id=manifest.id,
            record_version=1,
            status="PROPOSED",
            actor_id=request.proposer_id,
            reason=f"Proposed recruitment candidate '{manifest.id}' for capabilities {request.needed_capabilities}.",
            manifest_sha256=manifest_sha,
            event_id=event.event_id,
            timestamp=request.timestamp,
        )
        logger.info("Proposed candidate agent '%s' in proposal '%s'.", manifest.id, proposal_id)
        return self._repo.create_proposal_atomic(proposal=record, revision=revision, event=event)

    def _run_evaluation_checks(
        self,
        manifest: AgentManifest,
        expected_sha: str,
        needed_capabilities: list[str],
        workspace_root: str | None,
    ) -> tuple[list[str], list[str]]:
        """Run deterministic validity, capability, tool, routing, and workspace checks."""
        checks_passed: list[str] = []
        findings: list[str] = []

        try:
            validated = self._registry.validate_manifest(manifest)
            if validated.canonical_sha256() != expected_sha:
                findings.append("Candidate manifest hash drifted from proposed snapshot.")
            else:
                checks_passed.append("manifest_schema_and_hash")
        except AgentManifestValidationError as exc:
            findings.append(f"Manifest validation failed: {exc.message}")

        if self._registry.get_agent(manifest.id) is not None:
            findings.append(f"Agent '{manifest.id}' already exists in Registry.")
        else:
            checks_passed.append("unique_agent_id")

        missing_caps = [cap for cap in needed_capabilities if cap not in set(manifest.capabilities.skills)]
        if missing_caps:
            findings.append(f"Candidate lacks needed capabilities: {missing_caps}")
        else:
            checks_passed.append("needed_capabilities_covered")

        if self._engines is not None:
            provider = manifest.engine.provider_id or manifest.engine.default_type
            try:
                self._engines.resolve(provider_id=provider, adapter_id=manifest.engine.adapter_id)
                checks_passed.append("engine_provider_registered")
            except Exception as exc:
                findings.append(f"Engine provider '{provider}' unavailable: {exc}")

        if self._connections is not None and manifest.engine.connection_id is not None:
            conn = self._connections.get(manifest.engine.connection_id)
            if conn is None or not getattr(conn, "enabled", True):
                findings.append(f"Connection '{manifest.engine.connection_id}' missing or disabled.")
            else:
                checks_passed.append("connection_active")

        if workspace_root is not None:
            bad_scopes: list[str] = []
            for scope in (*manifest.permissions.fs_read, *manifest.permissions.fs_write):
                cleaned_scope = scope.rstrip("/*") or "."
                res = validate_workspace_path(workspace_root, cleaned_scope, action="files.read")
                if not res.allowed:
                    bad_scopes.append(scope)
            if bad_scopes:
                findings.append(f"Workspace scope violation: {bad_scopes}")
            else:
                checks_passed.append("workspace_scopes_valid")

        return checks_passed, findings

    def evaluate_candidate(
        self,
        request: RecruitmentEvaluationRequest,
    ) -> StoredRecruitmentProposal:
        """Independently evaluate candidate proposal; fail-closed if findings exist."""
        validate_aware_datetime(request.timestamp, "timestamp")
        stored = self._repo.get_proposal(request.proposal_id)
        if stored is None:
            raise RecruitmentNotFoundError("PROPOSAL_NOT_FOUND", f"Proposal '{request.proposal_id}' not found.")

        prop = stored.proposal
        if prop.status in TERMINAL_RECRUITMENT_STATUSES:
            raise RecruitmentStateError(
                "INVALID_RECRUITMENT_TRANSITION",
                f"Proposal '{prop.proposal_id}' is terminal ('{prop.status}') and cannot be evaluated.",
            )
        if request.evaluator_id == prop.proposed_by:
            raise RecruitmentGovernanceError(
                "SELF_EVALUATION_FORBIDDEN",
                f"Proposer '{prop.proposed_by}' cannot self-evaluate recruitment '{prop.proposal_id}'.",
            )

        corr_id = request.correlation_id or prop.correlation_id
        self._authorize_recruit(
            auth_context=request.auth_context,
            actor_id=request.evaluator_id,
            actor_kind=request.evaluator_kind,
            candidate_agent_id=prop.candidate_agent_id,
            proposal_id=prop.proposal_id,
            timestamp=request.timestamp,
            correlation_id=corr_id,
            parameters={"operation": "evaluate_candidate"},
        )

        auto_checks, auto_findings = self._run_evaluation_checks(
            prop.candidate_manifest,
            prop.manifest_sha256,
            prop.needed_capabilities,
            request.workspace_root,
        )
        merged_checks = list(dict.fromkeys([*auto_checks, *request.checks_passed]))
        merged_findings = list(dict.fromkeys([*auto_findings, *request.findings]))
        passed = len(merged_findings) == 0
        next_status = "EVALUATED_PASS" if passed else "EVALUATED_FAIL"

        eval_result = RecruitmentEvaluationResult(
            passed=passed,
            summary=request.summary,
            checks_passed=merged_checks,
            findings=merged_findings,
            evaluator_id=request.evaluator_id,
            evaluator_kind=request.evaluator_kind,
            manifest_sha256=prop.manifest_sha256,
            evaluated_at=request.timestamp,
        )
        next_prop = prop.model_copy(
            update={
                "status": next_status,
                "evaluation": eval_result,
                "evaluated_at": request.timestamp,
            }
        )
        next_ver = stored.record_version + 1
        event = Event(
            event_id=new_event_id(),
            timestamp=request.timestamp,
            event_type="agent.recruitment_evaluated",
            source=actor_kind_to_event_source(request.evaluator_kind),
            task_id=prop.task_id,
            actor_id=request.evaluator_id,
            correlation_id=corr_id,
            redacted_payload=sanitize_event_payload(
                {
                    "proposal_id": prop.proposal_id,
                    "candidate_agent_id": prop.candidate_agent_id,
                    "passed": passed,
                    "findings": merged_findings,
                }
            ),
        )
        revision = RecruitmentRevisionRecord(
            proposal_id=prop.proposal_id,
            candidate_agent_id=prop.candidate_agent_id,
            record_version=next_ver,
            status=next_status,
            actor_id=request.evaluator_id,
            reason=request.summary,
            manifest_sha256=prop.manifest_sha256,
            event_id=event.event_id,
            timestamp=request.timestamp,
        )
        return self._repo.transition_proposal_atomic(
            proposal=next_prop,
            expected_record_version=stored.record_version,
            revision=revision,
            event=event,
        )

    def activate_candidate(
        self,
        request: RecruitmentActivationRequest,
    ) -> StoredRecruitmentProposal:
        """Activate an EVALUATED_PASS candidate via ``AgentRegistryService.register_agent``."""
        validate_aware_datetime(request.timestamp, "timestamp")
        stored = self._repo.get_proposal(request.proposal_id)
        if stored is None:
            raise RecruitmentNotFoundError("PROPOSAL_NOT_FOUND", f"Proposal '{request.proposal_id}' not found.")

        prop = stored.proposal
        if prop.status != "EVALUATED_PASS" or prop.evaluation is None or not prop.evaluation.passed:
            raise RecruitmentStateError(
                "CANDIDATE_NOT_EVALUATED_PASS",
                f"Cannot activate proposal '{prop.proposal_id}' in status '{prop.status}'; EVALUATED_PASS required.",
            )
        if prop.evaluation.manifest_sha256 != prop.manifest_sha256:
            raise RecruitmentValidationError(
                "MANIFEST_DRIFT_AFTER_EVALUATION",
                "Candidate manifest hash changed after evaluation.",
            )

        corr_id = request.correlation_id or prop.correlation_id
        auth = self._authorize_recruit(
            auth_context=request.auth_context,
            actor_id=request.actor_id,
            actor_kind=request.actor_kind,
            candidate_agent_id=prop.candidate_agent_id,
            proposal_id=prop.proposal_id,
            timestamp=request.timestamp,
            correlation_id=corr_id,
            parameters={
                "operation": "activate_candidate",
                "sensitive_initial_grants": _manifest_has_sensitive_initial_grants(prop.candidate_manifest),
            },
        )
        if auth.actor_kind != "human_operator":
            raise RecruitmentGovernanceError(
                "OPERATOR_ACTIVATION_REQUIRED",
                (
                    f"Only an authenticated human_operator can activate recruited agent "
                    f"'{prop.candidate_agent_id}' into AgentRegistryService."
                ),
            )

        try:
            reg_outcome = self._registry.register_agent(
                prop.candidate_manifest,
                auth_context=auth,
                timestamp=request.timestamp,
                initial_status="ACTIVE",
                validated_capabilities=request.validated_capabilities or prop.needed_capabilities,
                approval_id=request.approval_id,
                correlation_id=corr_id,
            )
        except PermissionDeniedError as exc:
            raise RecruitmentGovernanceError(exc.decision.reason_code, exc.decision.message) from exc
        except AgentRegistryError as exc:
            raise RecruitmentValidationError(exc.error_code, exc.message) from exc
        except RuntimeError as exc:
            err_code = getattr(exc, "error_code", "ACTIVATION_APPROVAL_FAILED")
            raise RecruitmentGovernanceError(str(err_code), str(exc)) from exc

        next_prop = prop.model_copy(
            update={
                "status": "ACTIVATED",
                "activated_manifest_revision": reg_outcome.record.active_revision,
                "activated_at": request.timestamp,
            }
        )
        next_ver = stored.record_version + 1
        event = Event(
            event_id=new_event_id(),
            timestamp=request.timestamp,
            event_type="agent.recruitment_activated",
            source=actor_kind_to_event_source(request.actor_kind),
            task_id=prop.task_id,
            actor_id=request.actor_id,
            correlation_id=corr_id,
            redacted_payload=sanitize_event_payload(
                {
                    "proposal_id": prop.proposal_id,
                    "candidate_agent_id": prop.candidate_agent_id,
                    "activated_manifest_revision": reg_outcome.record.active_revision,
                    "registry_audit_event_id": reg_outcome.audit_event_id,
                }
            ),
        )
        revision = RecruitmentRevisionRecord(
            proposal_id=prop.proposal_id,
            candidate_agent_id=prop.candidate_agent_id,
            record_version=next_ver,
            status="ACTIVATED",
            actor_id=request.actor_id,
            reason=f"Activated candidate '{prop.candidate_agent_id}' as ACTIVE r{reg_outcome.record.active_revision}.",
            manifest_sha256=prop.manifest_sha256,
            event_id=event.event_id,
            timestamp=request.timestamp,
        )
        return self._repo.transition_proposal_atomic(
            proposal=next_prop,
            expected_record_version=stored.record_version,
            revision=revision,
            event=event,
        )

    def reject_proposal(
        self,
        request: RecruitmentRejectRequest,
    ) -> StoredRecruitmentProposal:
        """Reject a non-terminal recruitment proposal with an audited rationale."""
        validate_aware_datetime(request.timestamp, "timestamp")
        stored = self._repo.get_proposal(request.proposal_id)
        if stored is None:
            raise RecruitmentNotFoundError("PROPOSAL_NOT_FOUND", f"Proposal '{request.proposal_id}' not found.")

        prop = stored.proposal
        if prop.status in TERMINAL_RECRUITMENT_STATUSES or "REJECTED" not in VALID_RECRUITMENT_TRANSITIONS[prop.status]:
            raise RecruitmentStateError(
                "INVALID_RECRUITMENT_TRANSITION",
                f"Proposal '{prop.proposal_id}' in status '{prop.status}' cannot be rejected.",
            )

        corr_id = request.correlation_id or prop.correlation_id
        self._authorize_recruit(
            auth_context=request.auth_context,
            actor_id=request.actor_id,
            actor_kind=request.actor_kind,
            candidate_agent_id=prop.candidate_agent_id,
            proposal_id=prop.proposal_id,
            timestamp=request.timestamp,
            correlation_id=corr_id,
            parameters={"operation": "reject_proposal"},
        )

        next_prop = prop.model_copy(
            update={
                "status": "REJECTED",
                "rejection_reason": request.reason,
                "rejected_at": request.timestamp,
            }
        )
        next_ver = stored.record_version + 1
        event = Event(
            event_id=new_event_id(),
            timestamp=request.timestamp,
            event_type="agent.recruitment_rejected",
            source=actor_kind_to_event_source(request.actor_kind),
            task_id=prop.task_id,
            actor_id=request.actor_id,
            correlation_id=corr_id,
            redacted_payload=sanitize_event_payload(
                {
                    "proposal_id": prop.proposal_id,
                    "candidate_agent_id": prop.candidate_agent_id,
                    "reason": request.reason,
                }
            ),
        )
        revision = RecruitmentRevisionRecord(
            proposal_id=prop.proposal_id,
            candidate_agent_id=prop.candidate_agent_id,
            record_version=next_ver,
            status="REJECTED",
            actor_id=request.actor_id,
            reason=request.reason,
            manifest_sha256=prop.manifest_sha256,
            event_id=event.event_id,
            timestamp=request.timestamp,
        )
        return self._repo.transition_proposal_atomic(
            proposal=next_prop,
            expected_record_version=stored.record_version,
            revision=revision,
            event=event,
        )
