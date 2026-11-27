"""Phase 09 / P09.01 HR Department domain service for roles, skills, recruitment, and Explorer metadata."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import logging
from pathlib import Path
from typing import Any

from core.agents.department import DepartmentManifest, is_valid_worker_department
from core.agents.hr_models import (
    ApprovedSkillCatalogPort,
    CapabilityGapAssessment,
    CapabilityGapRequest,
    DepartmentTransferRequest,
    FailClosedEmptySkillCatalog,
    HrAgentExplorerItem,
    HrAgentRuntimeProjection,
    HrExplorerCatalog,
    HrExplorerQuery,
    HrRecruitmentDraftRequest,
    HrSkillApprovalError,
    HrStateError,
    HrValidationError,
    RoleChangeRequest,
    RuntimeStateReader,
    SkillGrantRequest,
    SkillRevokeRequest,
)
from core.agents.loader import load_department_manifests
from core.agents.manifest import AgentManifest, AgentSkillsConfig
from core.agents.recruitment import AgentRecruitmentService
from core.agents.recruitment_models import (
    TERMINAL_RECRUITMENT_STATUSES,
    RecruitmentProposalRequest,
    StoredRecruitmentProposal,
)
from core.agents.registry import (
    AgentManifestValidationError,
    AgentManifestVersion,
    AgentMutationOutcome,
    AgentNotFoundError,
    AgentRegistryRecord,
    AgentRegistryService,
)
from core.governance.models import TrustedAuthContext

logger = logging.getLogger(__name__)


def _is_authenticated_operator(auth_context: TrustedAuthContext | None) -> bool:
    """Return True only when caller holds an authenticated human_operator context."""
    return (
        auth_context is not None
        and auth_context.authenticated
        and auth_context.actor_kind == "human_operator"
    )


def _normalize_runtime_observation(
    raw: Any,
    *,
    task_id: str | None,
) -> HrAgentRuntimeProjection:
    """Map real or duck-typed runtime snapshot without fabricating unverified RUNNING status."""
    if raw is None:
        return HrAgentRuntimeProjection(runtime_state_source="NOT_OBSERVED", task_id=task_id)

    return HrAgentRuntimeProjection(
        runtime_state_source="RUNTIME_OBSERVER",
        task_id=getattr(raw, "task_id", task_id),
        session_id=getattr(raw, "session_id", None),
        session_status=getattr(raw, "session_status", None),
        session_initialized=bool(getattr(raw, "session_initialized", False)),
        session_active=bool(getattr(raw, "session_active", False)),
        observable_running=bool(getattr(raw, "observable_running", False)),
        run_id=getattr(raw, "run_id", None),
        run_status=getattr(raw, "run_status", None),
        is_assigned_to_task=bool(getattr(raw, "is_assigned_to_task", False)),
        assigned_step_ids=list(getattr(raw, "assigned_step_ids", []) or ()),
        reconciliation_required=bool(getattr(raw, "reconciliation_required", False)),
        reconciliation_reason=getattr(raw, "reconciliation_reason", None),
    )


class HrDepartmentService:
    """Orchestrates HR lifecycle, governed role/skill updates, recruitment, and Explorer metadata."""

    def __init__(
        self,
        *,
        registry: AgentRegistryService,
        recruitment_service: AgentRecruitmentService | None = None,
        departments: Mapping[str, DepartmentManifest] | None = None,
        departments_root: Path | None = None,
        skill_catalog: ApprovedSkillCatalogPort | None = None,
        runtime_reader: RuntimeStateReader | None = None,
    ) -> None:
        """Bind HR service to Registry, optional Recruitment, Department metadata, and read-only ports."""
        self._registry = registry
        self._recruitment = recruitment_service
        if departments is not None:
            self._departments: dict[str, DepartmentManifest] = dict(departments)
        elif departments_root is not None:
            self._departments = load_department_manifests(departments_root)
        else:
            self._departments = {}
        self._skill_catalog: ApprovedSkillCatalogPort = (
            skill_catalog if skill_catalog is not None else FailClosedEmptySkillCatalog()
        )
        self._runtime_reader = runtime_reader

    def _require_registered_record(self, agent_id: str) -> AgentRegistryRecord:
        """Load AgentRegistryRecord or fail closed with AgentNotFoundError."""
        record = self._registry.get_agent(agent_id)
        if record is None:
            raise AgentNotFoundError("AGENT_NOT_FOUND", f"Agent '{agent_id}' is not registered.")
        return record

    @staticmethod
    def _guard_immutable_master(record: AgentRegistryRecord, *, operation: str) -> None:
        """Refuse role/department reassignment on core.master or orchestrator role."""
        if record.agent_id == "core.master" or record.manifest.system_role == "orchestrator":
            raise HrValidationError(
                "MASTER_IDENTITY_IMMUTABLE",
                f"Operation '{operation}' cannot modify immutable orchestrator identity '{record.agent_id}'.",
            )

    def _validate_target_department(self, department: str, *, require_active: bool = True) -> None:
        """Ensure target department is a valid functional worker department (and ACTIVE when catalog loaded)."""
        if not is_valid_worker_department(department, custom_departments=self._departments.keys()):
            raise HrValidationError(
                "INVALID_DEPARTMENT",
                f"Department '{department}' is not a valid functional Worker department.",
            )
        if self._departments:
            dept_manifest = self._departments.get(department)
            if dept_manifest is None:
                raise HrValidationError(
                    "UNKNOWN_DEPARTMENT",
                    f"Department '{department}' is not present in loaded department catalog.",
                )
            if require_active and dept_manifest.status != "ACTIVE":
                raise HrValidationError(
                    "INACTIVE_DEPARTMENT",
                    f"Department '{department}' has status '{dept_manifest.status}'; ACTIVE required.",
                )

    def _require_approved_skills(
        self,
        skill_ids: list[str],
        *,
        declared_capabilities: set[str] | None = None,
    ) -> None:
        """Fail closed unless every requested skill exists with status='APPROVED' and prerequisite caps."""
        for skill_id in skill_ids:
            descriptor = self._skill_catalog.get_approved_skill(skill_id)
            if descriptor is None or descriptor.status != "APPROVED":
                raise HrSkillApprovalError(
                    "UNAPPROVED_SKILL",
                    (
                        f"Skill '{skill_id}' is not APPROVED in ApprovedSkillCatalogPort; "
                        "refusing fail-closed before Phase 10 verification."
                    ),
                )
            if declared_capabilities is not None and descriptor.required_capabilities:
                missing_caps = [
                    cap for cap in descriptor.required_capabilities if cap not in declared_capabilities
                ]
                if missing_caps:
                    raise HrValidationError(
                        "MISSING_REQUIRED_CAPABILITY",
                        (
                            f"Agent lacks prerequisite capabilities {missing_caps} "
                            f"required by approved skill '{skill_id}'."
                        ),
                    )

    # ------------------------------------------------------------------
    # Agent lifecycle delegations (no shadow registry or history state)
    # ------------------------------------------------------------------

    def list_agents(self, *, include_disabled: bool = True) -> list[AgentRegistryRecord]:
        """List authoritative Registry records via ``AgentRegistryService``."""
        return self._registry.list_agents(include_disabled=include_disabled)

    def get_agent(self, agent_id: str) -> AgentRegistryRecord | None:
        """Retrieve authoritative Registry record for ``agent_id`` or None."""
        return self._registry.get_agent(agent_id)

    def get_version_history(self, agent_id: str) -> list[AgentManifestVersion]:
        """Return immutable version history from ``AgentRegistryService``."""
        return self._registry.get_version_history(agent_id)

    def disable_agent(
        self,
        agent_id: str,
        *,
        expected_record_version: int,
        auth_context: TrustedAuthContext | None,
        timestamp: datetime,
        reason: str,
        approval_id: str | None = None,
        correlation_id: str | None = None,
    ) -> AgentMutationOutcome:
        """Disable an Agent for future work while preserving active Runs and audit history."""
        return self._registry.disable_agent(
            agent_id,
            expected_record_version=expected_record_version,
            auth_context=auth_context,
            timestamp=timestamp,
            reason=reason,
            approval_id=approval_id,
            correlation_id=correlation_id,
        )

    def enable_agent(
        self,
        agent_id: str,
        *,
        expected_record_version: int,
        auth_context: TrustedAuthContext | None,
        timestamp: datetime,
        reason: str,
        approval_id: str | None = None,
        correlation_id: str | None = None,
    ) -> AgentMutationOutcome:
        """Re-enable a DISABLED/VALIDATED/DRAFT Agent under Governance rules."""
        return self._registry.enable_agent(
            agent_id,
            expected_record_version=expected_record_version,
            auth_context=auth_context,
            timestamp=timestamp,
            reason=reason,
            approval_id=approval_id,
            correlation_id=correlation_id,
        )

    # ------------------------------------------------------------------
    # Governed role, department transfer, and skill management
    # ------------------------------------------------------------------

    def change_role(self, request: RoleChangeRequest) -> AgentMutationOutcome:
        """Update a Worker Agent's role, mission, capabilities, and optional department under Governance."""
        current = self._require_registered_record(request.agent_id)
        self._guard_immutable_master(current, operation="change_role")

        target_dept = request.new_department or current.manifest.department
        if target_dept != current.manifest.department:
            self._validate_target_department(target_dept, require_active=True)

        updates: dict[str, Any] = {
            "role": request.new_role,
            "department": target_dept,
        }
        if request.new_description is not None:
            updates["description"] = request.new_description
        if request.new_mission is not None:
            updates["mission"] = request.new_mission
        if request.new_capabilities is not None:
            updates["capabilities"] = request.new_capabilities

        updated_manifest = current.manifest.model_copy(update=updates)
        if (
            updated_manifest.canonical_sha256() == current.manifest_sha256
            and _is_authenticated_operator(request.auth_context)
        ):
            raise HrStateError(
                "NO_ROLE_CHANGE",
                f"Role change request produced no manifest differences for '{request.agent_id}'.",
            )

        logger.info(
            "HR updating role for agent '%s' (%s -> %s, dept %s -> %s).",
            request.agent_id,
            current.manifest.role,
            updated_manifest.role,
            current.manifest.department,
            updated_manifest.department,
        )
        return self._registry.update_agent(
            request.agent_id,
            updated_manifest,
            expected_record_version=request.expected_record_version,
            auth_context=request.auth_context,
            timestamp=request.timestamp,
            change_summary=request.change_summary,
            validated_capabilities=request.validated_capabilities or None,
            approval_id=request.approval_id,
            correlation_id=request.correlation_id or f"corr-hr-role-{request.agent_id}",
        )

    def transfer_department(self, request: DepartmentTransferRequest) -> AgentMutationOutcome:
        """Transfer a Worker Agent to another active Department via ``agent.change_department`` approval."""
        current = self._require_registered_record(request.agent_id)
        self._guard_immutable_master(current, operation="transfer_department")
        self._validate_target_department(request.target_department, require_active=True)

        if (
            request.target_department == current.manifest.department
            and _is_authenticated_operator(request.auth_context)
        ):
            raise HrStateError(
                "NO_DEPARTMENT_CHANGE",
                f"Agent '{request.agent_id}' is already assigned to department '{request.target_department}'.",
            )

        updates: dict[str, Any] = {"department": request.target_department}
        if request.new_role is not None:
            updates["role"] = request.new_role
        if request.new_description is not None:
            updates["description"] = request.new_description
        if request.new_mission is not None:
            updates["mission"] = request.new_mission
        if request.new_capabilities is not None:
            updates["capabilities"] = request.new_capabilities

        updated_manifest = current.manifest.model_copy(update=updates)
        logger.info(
            "HR transferring agent '%s' department %s -> %s.",
            request.agent_id,
            current.manifest.department,
            request.target_department,
        )
        return self._registry.update_agent(
            request.agent_id,
            updated_manifest,
            expected_record_version=request.expected_record_version,
            auth_context=request.auth_context,
            timestamp=request.timestamp,
            change_summary=request.reason,
            validated_capabilities=request.validated_capabilities or None,
            approval_id=request.approval_id,
            correlation_id=request.correlation_id or f"corr-hr-dept-{request.agent_id}",
        )

    def grant_skills(self, request: SkillGrantRequest) -> AgentMutationOutcome:
        """Grant catalog-APPROVED skills into ``skills.allowed`` under Approval Gate."""
        current = self._require_registered_record(request.agent_id)
        self._guard_immutable_master(current, operation="grant_skills")
        self._require_approved_skills(
            request.skill_ids,
            declared_capabilities=set(current.manifest.capabilities.skills),
        )

        existing = list(current.manifest.skills.allowed)
        missing_to_add = [skill for skill in request.skill_ids if skill not in set(existing)]
        if not missing_to_add and _is_authenticated_operator(request.auth_context):
            raise HrStateError(
                "SKILLS_ALREADY_GRANTED",
                f"All requested skills {request.skill_ids} are already granted to '{request.agent_id}'.",
            )

        next_allowed = list(dict.fromkeys([*existing, *request.skill_ids]))
        updated_manifest = current.manifest.model_copy(
            update={"skills": AgentSkillsConfig(allowed=next_allowed)}
        )
        logger.info(
            "HR granting skills %s to agent '%s'.",
            request.skill_ids,
            request.agent_id,
        )
        return self._registry.update_agent(
            request.agent_id,
            updated_manifest,
            expected_record_version=request.expected_record_version,
            auth_context=request.auth_context,
            timestamp=request.timestamp,
            change_summary=request.reason,
            approval_id=request.approval_id,
            correlation_id=request.correlation_id or f"corr-hr-skill-add-{request.agent_id}",
        )

    def revoke_skills(self, request: SkillRevokeRequest) -> AgentMutationOutcome:
        """Revoke specified skills from ``skills.allowed`` creating an audited manifest revision."""
        current = self._require_registered_record(request.agent_id)
        self._guard_immutable_master(current, operation="revoke_skills")

        existing = list(current.manifest.skills.allowed)
        missing = [skill for skill in request.skill_ids if skill not in set(existing)]
        if missing and _is_authenticated_operator(request.auth_context):
            raise HrStateError(
                "SKILL_NOT_GRANTED",
                f"Cannot revoke skills {missing} not currently granted to '{request.agent_id}'.",
            )

        revoke_set = set(request.skill_ids)
        next_allowed = [skill for skill in existing if skill not in revoke_set]
        updated_manifest = current.manifest.model_copy(
            update={"skills": AgentSkillsConfig(allowed=next_allowed)}
        )
        logger.info(
            "HR revoking skills %s from agent '%s'.",
            request.skill_ids,
            request.agent_id,
        )
        return self._registry.update_agent(
            request.agent_id,
            updated_manifest,
            expected_record_version=request.expected_record_version,
            auth_context=request.auth_context,
            timestamp=request.timestamp,
            change_summary=request.reason,
            approval_id=request.approval_id,
            correlation_id=request.correlation_id or f"corr-hr-skill-rev-{request.agent_id}",
        )

    # ------------------------------------------------------------------
    # Capability gap assessment and recruitment proposal intake
    # ------------------------------------------------------------------

    def assess_capability_gap(self, request: CapabilityGapRequest) -> CapabilityGapAssessment:
        """Compare required capabilities and tools against ACTIVE Worker Agents."""
        dept_manifest: DepartmentManifest | None = None
        if request.department is not None:
            self._validate_target_department(request.department, require_active=False)
            dept_manifest = self._departments.get(request.department)

        active_workers = [
            rec
            for rec in self._registry.list_agents(include_disabled=False)
            if rec.registration_status == "ACTIVE"
            and rec.manifest.system_role == "worker"
            and (request.department is None or rec.manifest.department == request.department)
        ]

        required_cap_set = set(request.required_capabilities)
        required_tool_set = set(request.required_tools)
        covered_union: set[str] = set()
        eligible_agent_ids: list[str] = []

        for rec in active_workers:
            if request.include_unvalidated:
                agent_caps = {c.capability_id for c in rec.capability_verification if c.declared}
            else:
                agent_caps = {c.capability_id for c in rec.capability_verification if c.validated}
            covered_union.update(agent_caps & required_cap_set)

            agent_tools = set(rec.manifest.tools.allowed)
            if required_cap_set <= agent_caps and required_tool_set <= agent_tools:
                eligible_agent_ids.append(rec.agent_id)

        covered_caps = [cap for cap in request.required_capabilities if cap in covered_union]
        missing_caps = [cap for cap in request.required_capabilities if cap not in covered_union]
        eligible_agent_ids.sort()

        return CapabilityGapAssessment(
            department=request.department,
            required_capabilities=list(request.required_capabilities),
            required_tools=list(request.required_tools),
            covered_capabilities=covered_caps,
            missing_capabilities=missing_caps,
            eligible_agent_ids=eligible_agent_ids,
            recommended_recruitment=bool(missing_caps or not eligible_agent_ids),
            department_supported_capabilities=(
                list(dept_manifest.supported_capabilities) if dept_manifest is not None else []
            ),
            department_advisory_tools=(
                list(dept_manifest.default_tools) if dept_manifest is not None else []
            ),
        )

    def propose_recruitment(
        self,
        request: HrRecruitmentDraftRequest,
    ) -> StoredRecruitmentProposal:
        """Validate HR department/skill preconditions and delegate candidate intake to ``AgentRecruitmentService``."""
        if self._recruitment is None:
            raise HrStateError(
                "RECRUITMENT_SERVICE_UNAVAILABLE",
                "AgentRecruitmentService is not configured on HrDepartmentService.",
            )

        manifest_for_checks: AgentManifest | None = None
        if isinstance(request.candidate_manifest, AgentManifest):
            manifest_for_checks = request.candidate_manifest
        else:
            try:
                manifest_for_checks = self._registry.validate_manifest(request.candidate_manifest)
            except AgentManifestValidationError:
                manifest_for_checks = None

        if manifest_for_checks is not None:
            if manifest_for_checks.id != "core.master" and manifest_for_checks.system_role == "worker":
                self._validate_target_department(manifest_for_checks.department, require_active=True)
            if manifest_for_checks.skills.allowed:
                self._require_approved_skills(
                    list(manifest_for_checks.skills.allowed),
                    declared_capabilities=set(manifest_for_checks.capabilities.skills),
                )

        return self._recruitment.propose_candidate(
            RecruitmentProposalRequest(
                proposal_id=request.proposal_id,
                candidate_manifest=request.candidate_manifest,
                rationale=request.rationale,
                needed_capabilities=list(request.needed_capabilities),
                task_id=request.task_id,
                proposer_id=request.proposer_id,
                proposer_kind=request.proposer_kind,
                auth_context=request.auth_context,
                timestamp=request.timestamp,
                correlation_id=request.correlation_id,
            )
        )

    # ------------------------------------------------------------------
    # Truthful Agent status & capability metadata export for Explorer
    # ------------------------------------------------------------------

    def _build_explorer_item(
        self,
        rec: AgentRegistryRecord,
        *,
        task_id: str | None,
        agents_root: Path | None,
        open_proposal_ids_by_agent: dict[str, list[str]],
    ) -> HrAgentExplorerItem:
        """Assemble unified read-only Explorer item joining Registry, Department, and optional Runtime."""
        dept_manifest = self._departments.get(rec.manifest.department)
        drift_status: str | None = None
        if agents_root is not None and rec.source_path is not None:
            drift = self._registry.detect_manifest_drift(
                rec.agent_id,
                rec.source_path,
                allowed_root=agents_root,
                enforce_path_convention=True,
            )
            drift_status = drift.status

        raw_runtime = (
            self._runtime_reader.get_worker_runtime_state(rec.agent_id, task_id=task_id)
            if self._runtime_reader is not None
            else None
        )
        runtime_projection = _normalize_runtime_observation(raw_runtime, task_id=task_id)

        return HrAgentExplorerItem(
            agent_id=rec.agent_id,
            name=rec.manifest.name,
            role=rec.manifest.role,
            system_role=rec.manifest.system_role,
            department=rec.manifest.department,
            department_name=dept_manifest.name if dept_manifest is not None else None,
            department_zone_id=(
                dept_manifest.visual_metadata.zone_id if dept_manifest is not None else None
            ),
            department_status=dept_manifest.status if dept_manifest is not None else None,
            registration_status=rec.registration_status,
            active_revision=rec.active_revision,
            record_version=rec.record_version,
            manifest_sha256=rec.manifest_sha256,
            source_path=rec.source_path,
            drift_status=drift_status,
            capabilities=list(rec.capability_verification),
            declared_skills=list(rec.manifest.capabilities.skills),
            declared_inputs=list(rec.manifest.capabilities.inputs),
            declared_outputs=list(rec.manifest.capabilities.outputs),
            skills_allowed=list(rec.manifest.skills.allowed),
            tools_allowed=list(rec.manifest.tools.allowed),
            department_advisory_tools=(
                list(dept_manifest.default_tools) if dept_manifest is not None else []
            ),
            engine_preferences={
                "default_type": rec.manifest.engine.default_type,
                "provider_id": rec.manifest.engine.provider_id,
                "adapter_id": rec.manifest.engine.adapter_id,
                "connection_id": rec.manifest.engine.connection_id,
                "model_tier": rec.manifest.engine.model_tier,
                "model_id": rec.manifest.engine.model_id,
                "routing_profile": rec.manifest.engine.routing_profile,
                "fallback_policy": rec.manifest.engine.fallback_policy,
            },
            permission_summary={
                "fs_read": list(rec.manifest.permissions.fs_read),
                "fs_write": list(rec.manifest.permissions.fs_write),
                "network": rec.manifest.permissions.network,
                "requires_approval": list(rec.manifest.permissions.requires_approval),
            },
            open_recruitment_proposal_ids=list(open_proposal_ids_by_agent.get(rec.agent_id, ())),
            updated_at=rec.updated_at,
            last_audit_event_id=rec.last_audit_event_id,
            runtime=runtime_projection,
        )

    def _open_recruitments(self) -> tuple[list[StoredRecruitmentProposal], dict[str, list[str]]]:
        """Collect non-terminal recruitment proposals indexed by candidate_agent_id."""
        if self._recruitment is None:
            return [], {}
        all_props = self._recruitment.list_proposals()
        open_props = [
            prop for prop in all_props if prop.proposal.status not in TERMINAL_RECRUITMENT_STATUSES
        ]
        by_agent: dict[str, list[str]] = {}
        for prop in open_props:
            by_agent.setdefault(prop.proposal.candidate_agent_id, []).append(
                prop.proposal.proposal_id
            )
        return open_props, by_agent

    def get_explorer_agent(
        self,
        agent_id: str,
        *,
        task_id: str | None = None,
        agents_root: Path | None = None,
    ) -> HrAgentExplorerItem:
        """Return enriched HR Explorer view for a single registered Agent."""
        rec = self._require_registered_record(agent_id)
        _, open_by_agent = self._open_recruitments()
        return self._build_explorer_item(
            rec,
            task_id=task_id,
            agents_root=agents_root,
            open_proposal_ids_by_agent=open_by_agent,
        )

    def export_explorer_catalog(
        self,
        *,
        query: HrExplorerQuery | None = None,
        agents_root: Path | None = None,
    ) -> HrExplorerCatalog:
        """Export unified read-only HR metadata catalog for Agent Explorer."""
        effective_query = query if query is not None else HrExplorerQuery()
        open_props, open_by_agent = self._open_recruitments()

        items: list[HrAgentExplorerItem] = []
        for rec in self._registry.list_agents(include_disabled=effective_query.include_disabled):
            if (
                effective_query.department is not None
                and rec.manifest.department != effective_query.department
            ):
                continue
            if (
                effective_query.registration_status is not None
                and rec.registration_status != effective_query.registration_status
            ):
                continue
            items.append(
                self._build_explorer_item(
                    rec,
                    task_id=effective_query.task_id,
                    agents_root=agents_root,
                    open_proposal_ids_by_agent=open_by_agent,
                )
            )

        approved_skills = [d.skill_id for d in self._skill_catalog.list_approved_skills()]
        return HrExplorerCatalog(
            generated_for_task_id=effective_query.task_id,
            agents=items,
            departments=[self._departments[k] for k in sorted(self._departments)],
            open_recruitments=open_props,
            approved_skills_available=approved_skills,
        )
