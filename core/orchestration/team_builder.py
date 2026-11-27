"""Capability-matched, manifest-pinned Dynamic Team Builder and Governance enforcement."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime
import logging
from typing import TYPE_CHECKING, Any, Literal

from core.contracts.event import Event
from core.contracts.identifiers import new_event_id, validate_agent_id, validate_aware_datetime
from core.governance.manager import PermissionDeniedError, PermissionManager
from core.governance.models import (
    PolicyDecision,
    PolicyRequest,
    RoutingContext,
    TrustedAuthContext,
    actor_kind_to_event_source as _actor_kind_to_event_source,
)
from core.orchestration.planning_models import PlanNotFoundError, PlanRepository, PlanStepSpec, StoredMasterPlan
from core.orchestration.team_models import (
    AgentStalenessIssue,
    CandidateEvaluation,
    CapabilityVerificationRequirement,
    PinnedAgentAssignment,
    StaleTeamProposalError,
    StepAssignmentEntry,
    StoredTeamProposal,
    TeamApprovalRequiredError,
    TeamAssignmentResult,
    TeamAssignmentStateError,
    TeamBuildRequest,
    TeamGovernanceCheck,
    TeamGovernanceDeniedError,
    TeamProposal,
    TeamProposalNotFoundError,
    TeamProposalRepository,
    TeamProposalRevisionRecord,
    TeamProposalStatus,
    TeamStalenessReport,
    new_team_proposal_id,
)

if TYPE_CHECKING:
    from core.agents.registry import AgentRegistryRecord, AgentRegistryService

logger = logging.getLogger(__name__)

_LEVEL_RANK: dict[CapabilityVerificationRequirement, int] = {
    "declared": 1,
    "validated": 2,
    "observed_in_execution": 3,
}
_DEFAULT_LOCAL_ONLY_PROVIDERS: frozenset[str] = frozenset({"ollama", "local_llm", "llama_cpp"})
_EXACT_SEARCH_MAX_CANDIDATES = 16


def _resolve_team_actor_id(auth_context: TrustedAuthContext) -> str:
    """Resolve canonical AgentId from trusted auth context."""
    try:
        return validate_agent_id(auth_context.actor_id)
    except ValueError:
        if auth_context.actor_kind == "human_operator":
            return "operator.local"
        return "core.master"


def _highest_verification_level(
    record: AgentRegistryRecord,
    capability_id: str,
) -> tuple[int, CapabilityVerificationRequirement | None]:
    """Return (rank, level_name) truthfully from Registry capability verification entries."""
    entry = next(
        (item for item in record.capability_verification if item.capability_id == capability_id),
        None,
    )
    if entry is None:
        return 0, None
    if entry.observed_in_execution:
        return 3, "observed_in_execution"
    if entry.validated:
        return 2, "validated"
    if entry.declared:
        return 1, "declared"
    return 0, None


def _solve_minimal_team_cover(
    *,
    coverable_steps: Sequence[PlanStepSpec],
    step_eligible: dict[str, list[str]],
    agent_departments: dict[str, str],
    max_team_size: int | None,
) -> list[str]:
    """Select deterministic minimal Worker IDs covering ``coverable_steps``."""
    if not coverable_steps:
        return []

    candidate_pool = sorted({aid for step in coverable_steps for aid in step_eligible[step.step_id]})
    agent_step_masks: dict[str, int] = {aid: 0 for aid in candidate_pool}
    for bit_idx, step in enumerate(coverable_steps):
        for aid in step_eligible[step.step_id]:
            agent_step_masks[aid] |= 1 << bit_idx

    full_mask = (1 << len(coverable_steps)) - 1

    # Exact branch-and-bound minimal cardinality cover for realistic candidate sets.
    if len(candidate_pool) <= _EXACT_SEARCH_MAX_CANDIDATES:
        ordered_steps = sorted(
            enumerate(coverable_steps),
            key=lambda pair: (len(step_eligible[pair[1].step_id]), pair[0]),
        )
        best_team: tuple[str, ...] | None = None
        best_key: tuple[int, int, tuple[str, ...]] | None = None

        def _objective(team_ids: Sequence[str]) -> tuple[int, int, tuple[str, ...]]:
            sorted_ids = tuple(sorted(team_ids))
            depts = {agent_departments[aid] for aid in sorted_ids}
            return (len(sorted_ids), len(depts), sorted_ids)

        def _dfs(covered_mask: int, chosen: list[str]) -> None:
            nonlocal best_team, best_key
            if max_team_size is not None and len(chosen) > max_team_size:
                return
            if best_key is not None and len(chosen) > best_key[0]:
                return
            if covered_mask == full_mask:
                cand_key = _objective(chosen)
                if best_key is None or cand_key < best_key:
                    best_key = cand_key
                    best_team = tuple(sorted(chosen))
                return

            next_step: PlanStepSpec | None = None
            for bit_idx, step in ordered_steps:
                if not (covered_mask & (1 << bit_idx)):
                    next_step = step
                    break
            assert next_step is not None

            # Branch first on already-selected agents? If none selected fits, try eligible sorted.
            eligible_for_step = sorted(
                step_eligible[next_step.step_id],
                key=lambda aid: (-agent_step_masks[aid].bit_count(), aid),
            )
            for aid in eligible_for_step:
                chosen.append(aid)
                _dfs(covered_mask | agent_step_masks[aid], chosen)
                chosen.pop()

        _dfs(0, [])
        if best_team is not None:
            return list(best_team)

    # Deterministic greedy set-cover fallback for large pools or capped max_team_size.
    selected: list[str] = []
    remaining_mask = full_mask
    remaining_candidates = list(candidate_pool)
    limit = max_team_size if max_team_size is not None else len(candidate_pool)
    while remaining_mask != 0 and remaining_candidates and len(selected) < limit:
        best_gain = max(
            (agent_step_masks[aid] & remaining_mask).bit_count()
            for aid in remaining_candidates
        )
        if best_gain == 0:
            break
        tied = sorted(
            aid
            for aid in remaining_candidates
            if (agent_step_masks[aid] & remaining_mask).bit_count() == best_gain
        )
        chosen_aid = tied[0]
        selected.append(chosen_aid)
        remaining_mask &= ~agent_step_masks[chosen_aid]
        remaining_candidates.remove(chosen_aid)

    return sorted(selected)


class DynamicTeamBuilderService:
    """Governance-checked, capability-based Team Builder with manifest revision pinning."""

    def __init__(
        self,
        *,
        team_repo: TeamProposalRepository,
        plan_repo: PlanRepository,
        agent_registry: AgentRegistryService,
        permission_manager: PermissionManager,
        allowed_providers: Collection[str] | None = None,
        allowed_models: Collection[str] | None = None,
        allowed_connections: Collection[str] | None = None,
        registered_tools: Collection[str] | None = None,
        local_only_providers: Collection[str] | None = None,
    ) -> None:
        """Initialize Dynamic Team Builder with explicit Registry, Governance, and Storage ports."""
        self._repo = team_repo
        self._plan_repo = plan_repo
        self._registry = agent_registry
        self._pm = permission_manager
        self._allowed_providers = set(allowed_providers) if allowed_providers is not None else None
        self._allowed_models = set(allowed_models) if allowed_models is not None else None
        self._allowed_connections = set(allowed_connections) if allowed_connections is not None else None
        self._registered_tools = set(registered_tools) if registered_tools is not None else None
        self._local_providers = (
            set(local_only_providers)
            if local_only_providers is not None
            else set(_DEFAULT_LOCAL_ONLY_PROVIDERS)
        )

    def _load_registry_split(self) -> tuple[list[AgentRegistryRecord], list[AgentRegistryRecord]]:
        """Load full Registry snapshot and verified ACTIVE-only search subset."""
        from core.agents.registry import AgentSearchQuery

        all_records = self._registry.list_agents(include_disabled=True)
        active_search = self._registry.search_agents(AgentSearchQuery(eligible_only=True))
        return all_records, active_search

    def _authorize_build_or_evaluate(
        self,
        *,
        auth_context: TrustedAuthContext | None,
        task_id: str,
        resource: str,
        action: str,
        timestamp: datetime,
        parameters: dict[str, Any],
        handler: Any | None = None,
        routing: RoutingContext | None = None,
        approval_id: str | None = None,
        correlation_id: str,
    ) -> tuple[PolicyDecision, Any]:
        """Authorize caller via Governance PermissionManager and reject worker escalation."""
        if auth_context is not None and auth_context.actor_kind == "worker_agent":
            raise TeamGovernanceDeniedError(
                "WORKER_ORCHESTRATION_FORBIDDEN",
                f"Worker agent '{auth_context.actor_id}' cannot build or assign Agent teams.",
            )

        req = PolicyRequest(
            auth_context=auth_context,
            action=action,
            resource=resource,
            task_id=task_id,
            routing=routing,
            approval_id=approval_id,
            parameters=parameters,
            timestamp=timestamp,
            correlation_id=correlation_id,
        )
        run_fn = handler if handler is not None else (lambda: True)
        try:
            return self._pm.dispatch_protected_action(req, run_fn)
        except PermissionDeniedError as exc:
            if exc.decision.decision == "APPROVAL_REQUIRED":
                raise TeamApprovalRequiredError(
                    exc.decision.reason_code,
                    exc.decision.message,
                ) from exc
            raise TeamGovernanceDeniedError(
                exc.decision.reason_code,
                exc.decision.message,
            ) from exc

    def _evaluate_candidate_for_step(
        self,
        *,
        record: AgentRegistryRecord,
        step: PlanStepSpec,
        request: TeamBuildRequest,
        plan_residency: str,
    ) -> tuple[bool, list[str], list[str], list[str]]:
        """Evaluate whether ``record`` can execute ``step`` safely without fabricating proof."""
        matched_caps: list[str] = []
        missing_reqs: list[str] = []
        exclusion_reasons: list[str] = []

        if record.manifest.system_role == "orchestrator" or record.agent_id == "core.master":
            exclusion_reasons.append("ORCHESTRATOR_NOT_WORKER")
        if not record.is_eligible_for_assignment():
            exclusion_reasons.append(f"AGENT_NOT_ACTIVE:{record.registration_status}")

        min_rank = _LEVEL_RANK[request.min_capability_level]
        for cap_id in step.required_capabilities:
            rank, level_name = _highest_verification_level(record, cap_id)
            if rank == 0:
                missing_reqs.append(f"missing_capability:{cap_id}")
            elif rank < min_rank:
                missing_reqs.append(
                    f"capability_below_threshold:{cap_id}({level_name}<{request.min_capability_level})"
                )
                exclusion_reasons.append(f"CAPABILITY_VERIFICATION_BELOW_THRESHOLD:{cap_id}")
            else:
                matched_caps.append(cap_id)

        if step.input_formats and not set(step.input_formats) <= set(record.manifest.capabilities.inputs):
            exclusion_reasons.append("FORMAT_MISMATCH:inputs")
        if step.output_formats and not set(step.output_formats) <= set(record.manifest.capabilities.outputs):
            exclusion_reasons.append("FORMAT_MISMATCH:outputs")

        if step.required_tools:
            if self._registered_tools is not None and not set(step.required_tools) <= self._registered_tools:
                exclusion_reasons.append("TOOL_UNREGISTERED")
            if not set(step.required_tools) <= set(record.manifest.tools.allowed):
                exclusion_reasons.append("TOOL_NOT_ALLOWED")

        if step.allowed_write_paths and not record.manifest.permissions.fs_write:
            exclusion_reasons.append("WORKSPACE_SCOPE_EXCEEDED:no_fs_write")

        cand_provider = record.manifest.engine.provider_id or record.manifest.engine.default_type
        effective_allowed_providers: set[str] | None = None
        if self._allowed_providers is not None and request.allowed_providers is not None:
            effective_allowed_providers = self._allowed_providers & set(request.allowed_providers)
        elif self._allowed_providers is not None:
            effective_allowed_providers = self._allowed_providers
        elif request.allowed_providers is not None:
            effective_allowed_providers = set(request.allowed_providers)

        if effective_allowed_providers is not None and cand_provider not in effective_allowed_providers:
            exclusion_reasons.append(f"ROUTING_POLICY_BLOCKED:provider={cand_provider}")

        cand_model = record.manifest.engine.model_id
        effective_allowed_models: set[str] | None = None
        if self._allowed_models is not None and request.allowed_models is not None:
            effective_allowed_models = self._allowed_models & set(request.allowed_models)
        elif self._allowed_models is not None:
            effective_allowed_models = self._allowed_models
        elif request.allowed_models is not None:
            effective_allowed_models = set(request.allowed_models)

        if effective_allowed_models is not None and cand_model is not None and cand_model not in effective_allowed_models:
            exclusion_reasons.append(f"ROUTING_POLICY_BLOCKED:model={cand_model}")

        effective_residency = request.required_data_residency or plan_residency
        if effective_residency == "local_only" and cand_provider not in self._local_providers:
            exclusion_reasons.append(f"DATA_RESIDENCY_BLOCKED:provider={cand_provider}")
        if (
            effective_residency == "strict_approved_only"
            and record.manifest.engine.connection_id is None
        ):
            exclusion_reasons.append("ROUTING_POLICY_BLOCKED:missing_connection_id")

        eligible = not exclusion_reasons and not missing_reqs
        return eligible, matched_caps, missing_reqs, exclusion_reasons

    def _compute_proposal(
        self,
        *,
        stored_plan: StoredMasterPlan,
        request: TeamBuildRequest,
        actor_agent_id: str,
        timestamp: datetime,
        proposal_id: str,
        proposal_revision: int,
        gov_check: TeamGovernanceCheck,
    ) -> TeamProposal:
        """Deterministically evaluate Registry candidates and assemble minimal team proposal."""
        plan = stored_plan.plan
        if stored_plan.status in {"NEEDS_CLARIFICATION", "REJECTED", "SUPERSEDED", "DRAFT"} or not plan.steps:
            raise TeamAssignmentStateError(
                "PLAN_NOT_READY_FOR_TEAM",
                f"Plan '{stored_plan.plan_id}' in status '{stored_plan.status}' cannot form a TeamProposal.",
            )

        all_records, active_search_records = self._load_registry_split()
        active_search_ids = {rec.agent_id for rec in active_search_records}
        records_by_id = {rec.agent_id: rec for rec in all_records}
        agent_departments = {rec.agent_id: rec.manifest.department for rec in all_records}

        ordered_steps = sorted(plan.steps, key=lambda item: (item.order, item.step_id))
        step_eligible: dict[str, list[str]] = {}
        step_block_reasons: dict[str, list[str]] = {}
        step_cap_matched_ids: dict[str, set[str]] = {}

        # Aggregate per-candidate evaluation across plan steps.
        cand_matched_caps: dict[str, set[str]] = {rec.agent_id: set() for rec in all_records}
        cand_missing_reqs: dict[str, set[str]] = {rec.agent_id: set() for rec in all_records}
        cand_exclusions: dict[str, set[str]] = {rec.agent_id: set() for rec in all_records}
        cand_eligible_any_step: dict[str, bool] = {rec.agent_id: False for rec in all_records}

        for step in ordered_steps:
            eligible_ids: list[str] = []
            reasons_for_step: list[str] = []
            cap_matched_for_step: set[str] = set()
            for rec in all_records:
                # Guarantee search_agents(eligible_only=True) agreement.
                eligible, matched_caps, missing_reqs, excl = self._evaluate_candidate_for_step(
                    record=rec,
                    step=step,
                    request=request,
                    plan_residency=plan.routing_proposal.data_residency_policy,
                )
                if eligible and rec.agent_id not in active_search_ids:
                    eligible = False
                    excl.append("AGENT_NOT_IN_ELIGIBLE_SEARCH")

                cand_matched_caps[rec.agent_id].update(matched_caps)
                cand_missing_reqs[rec.agent_id].update(missing_reqs)
                cand_exclusions[rec.agent_id].update(excl)

                if matched_caps:
                    cap_matched_for_step.add(rec.agent_id)
                if eligible:
                    eligible_ids.append(rec.agent_id)
                    cand_eligible_any_step[rec.agent_id] = True
                elif matched_caps or excl:
                    reasons_for_step.extend(excl)

            step_eligible[step.step_id] = sorted(eligible_ids)
            step_block_reasons[step.step_id] = sorted(set(reasons_for_step))
            step_cap_matched_ids[step.step_id] = cap_matched_for_step

        coverable_steps = [step for step in ordered_steps if step_eligible[step.step_id]]
        selected_ids = _solve_minimal_team_cover(
            coverable_steps=coverable_steps,
            step_eligible=step_eligible,
            agent_departments=agent_departments,
            max_team_size=request.max_team_size,
        )
        selected_set = set(selected_ids)

        # Assign each step deterministically to best selected team member (or mark missing/blocked).
        member_steps: dict[str, list[str]] = {aid: [] for aid in selected_ids}
        member_matched_caps: dict[str, set[str]] = {aid: set() for aid in selected_ids}
        step_assignments: list[StepAssignmentEntry] = []
        missing_caps_ordered: list[str] = []
        seen_missing_caps: set[str] = set()
        uncovered_policy_only = 0
        uncovered_missing_cap = 0

        for step in ordered_steps:
            candidates_in_team = [aid for aid in step_eligible[step.step_id] if aid in selected_set]
            if candidates_in_team:
                def _score_member(aid: str) -> tuple[int, str]:
                    rec = records_by_id[aid]
                    total_rank = sum(
                        _highest_verification_level(rec, cap)[0]
                        for cap in step.required_capabilities
                    )
                    return (-total_rank, aid)

                chosen_aid = min(candidates_in_team, key=_score_member)
                chosen_rec = records_by_id[chosen_aid]
                member_steps[chosen_aid].append(step.step_id)
                member_matched_caps[chosen_aid].update(step.required_capabilities)
                step_assignments.append(
                    StepAssignmentEntry(
                        step_id=step.step_id,
                        checklist_item_id=step.checklist_item_id,
                        required_capabilities=list(step.required_capabilities),
                        assigned_agent_id=chosen_aid,
                        pinned_manifest_revision=chosen_rec.active_revision,
                        status="ASSIGNED_IN_PROPOSAL",
                        missing_capabilities=[],
                        reason=(
                            f"Selected ACTIVE '{chosen_aid}' (dept={chosen_rec.manifest.department}, "
                            f"rev={chosen_rec.active_revision}) covering {step.required_capabilities}."
                        ),
                    )
                )
            else:
                # Distinguish missing Registry capability vs Governance/routing/max_team_size block.
                step_missing_caps: list[str] = []
                for cap_id in step.required_capabilities:
                    has_active_holder = any(
                        rec.is_eligible_for_assignment()
                        and rec.manifest.system_role == "worker"
                        and _highest_verification_level(rec, cap_id)[0] >= _LEVEL_RANK[request.min_capability_level]
                        for rec in all_records
                    )
                    if not has_active_holder:
                        step_missing_caps.append(cap_id)
                        if cap_id not in seen_missing_caps:
                            seen_missing_caps.add(cap_id)
                            missing_caps_ordered.append(cap_id)

                if step_missing_caps:
                    uncovered_missing_cap += 1
                    step_assignments.append(
                        StepAssignmentEntry(
                            step_id=step.step_id,
                            checklist_item_id=step.checklist_item_id,
                            required_capabilities=list(step.required_capabilities),
                            assigned_agent_id=None,
                            pinned_manifest_revision=None,
                            status="MISSING_CAPABILITY",
                            missing_capabilities=step_missing_caps,
                            reason=(
                                f"No ACTIVE non-orchestrator Worker satisfies required capabilities "
                                f"{step_missing_caps} at min_level='{request.min_capability_level}'."
                            ),
                        )
                    )
                else:
                    uncovered_policy_only += 1
                    step_assignments.append(
                        StepAssignmentEntry(
                            step_id=step.step_id,
                            checklist_item_id=step.checklist_item_id,
                            required_capabilities=list(step.required_capabilities),
                            assigned_agent_id=None,
                            pinned_manifest_revision=None,
                            status="BLOCKED_GOVERNANCE",
                            missing_capabilities=[],
                            reason=(
                                f"Capable candidates blocked by routing, tool, workspace, or team size constraints: "
                                f"{step_block_reasons[step.step_id] or ['MAX_TEAM_SIZE_EXCEEDED']}."
                            ),
                        )
                    )

        pinned_members: list[PinnedAgentAssignment] = []
        for aid in sorted(aid for aid, sids in member_steps.items() if sids):
            rec = records_by_id[aid]
            matched_list = sorted(member_matched_caps[aid])
            ev_levels: dict[str, CapabilityVerificationRequirement] = {}
            for cap_id in matched_list:
                _, lvl = _highest_verification_level(rec, cap_id)
                if lvl is not None:
                    ev_levels[cap_id] = lvl
            pinned_members.append(
                PinnedAgentAssignment(
                    agent_id=aid,
                    department=rec.manifest.department,
                    manifest_revision=rec.active_revision,
                    manifest_sha256=rec.manifest_sha256,
                    spec_version=rec.manifest.spec_version,
                    engine_id=rec.manifest.engine.default_type,
                    provider_id=rec.manifest.engine.provider_id,
                    connection_id=rec.manifest.engine.connection_id,
                    model_id=rec.manifest.engine.model_id,
                    assigned_step_ids=member_steps[aid],
                    matched_capabilities=matched_list,
                    capability_evidence_levels=ev_levels,
                    selection_reason=(
                        f"Selected into minimal team from department '{rec.manifest.department}' "
                        f"at manifest revision v{rec.active_revision} to execute {len(member_steps[aid])} step(s)."
                    ),
                    quota_remaining=None,
                    estimated_cost_usd=None,
                )
            )

        candidate_evals: list[CandidateEvaluation] = []
        for rec in all_records:
            candidate_evals.append(
                CandidateEvaluation(
                    agent_id=rec.agent_id,
                    department=rec.manifest.department,
                    registration_status=rec.registration_status,
                    manifest_revision=rec.active_revision,
                    eligible=cand_eligible_any_step[rec.agent_id],
                    matched_capabilities=sorted(cand_matched_caps[rec.agent_id]),
                    missing_requirements=sorted(cand_missing_reqs[rec.agent_id]),
                    exclusion_reasons=sorted(cand_exclusions[rec.agent_id]),
                )
            )

        if uncovered_missing_cap == 0 and uncovered_policy_only == 0:
            status: TeamProposalStatus = "READY_FOR_ASSIGNMENT"
            summary = (
                f"Minimal team of {len(pinned_members)} ACTIVE worker(s) selected across "
                f"{sorted({m.department for m in pinned_members})} covering all {len(ordered_steps)} step(s)."
            )
        elif uncovered_policy_only > 0 and not pinned_members and uncovered_missing_cap == 0:
            status = "BLOCKED_GOVERNANCE"
            summary = (
                f"All {len(ordered_steps)} step(s) blocked by routing, tool, workspace, or team size policy."
            )
        elif pinned_members:
            status = "PARTIAL"
            summary = (
                f"Partial coverage: {len(pinned_members)} worker(s) matched subset of steps; "
                f"missing capabilities={missing_caps_ordered}."
            )
        else:
            status = "BLOCKED_MISSING_CAPABILITY"
            summary = (
                f"Blocked: zero steps covered; missing capabilities={missing_caps_ordered}."
            )

        departments_involved = sorted({m.department for m in pinned_members})
        return TeamProposal(
            proposal_id=proposal_id,
            task_id=stored_plan.task_id,
            plan_id=stored_plan.plan_id,
            plan_revision=stored_plan.active_revision,
            proposal_revision=proposal_revision,
            status=status,
            min_capability_level=request.min_capability_level,
            pinned_members=pinned_members,
            step_assignments=step_assignments,
            candidate_evaluations=candidate_evals,
            missing_capabilities=missing_caps_ordered,
            departments_involved=departments_involved,
            selection_summary=summary,
            governance_checks=[gov_check],
            created_by=actor_agent_id,
            created_at=timestamp,
            change_summary=request.change_summary,
        )

    def build_team_proposal(
        self,
        request: TeamBuildRequest,
        *,
        auth_context: TrustedAuthContext | None,
        timestamp: datetime,
        approval_id: str | None = None,
        correlation_id: str | None = None,
    ) -> StoredTeamProposal:
        """Evaluate Registry candidates for an existing Plan and persist TeamProposal v1."""
        validate_aware_datetime(timestamp, "timestamp")
        stored_plan = self._plan_repo.get_plan(request.plan_id)
        if stored_plan is None:
            raise PlanNotFoundError("PLAN_NOT_FOUND", f"Plan '{request.plan_id}' does not exist.")
        if stored_plan.task_id != request.task_id:
            raise TeamAssignmentStateError(
                "PLAN_TASK_MISMATCH",
                f"Plan '{request.plan_id}' belongs to '{stored_plan.task_id}', not '{request.task_id}'.",
            )

        proposal_id = new_team_proposal_id()
        corr_id = correlation_id or f"corr-team-build-{request.task_id}"
        decision, _ = self._authorize_build_or_evaluate(
            auth_context=auth_context,
            task_id=request.task_id,
            resource=f"plan:{request.plan_id}",
            action="agent.read",
            timestamp=timestamp,
            parameters={
                "proposal_id": proposal_id,
                "plan_id": request.plan_id,
                "min_capability_level": request.min_capability_level,
            },
            approval_id=approval_id,
            correlation_id=corr_id,
        )
        assert auth_context is not None
        actor_agent_id = _resolve_team_actor_id(auth_context)
        gov_check = TeamGovernanceCheck(
            action="agent.read",
            outcome="ALLOW",
            reason=decision.message,
        )
        proposal = self._compute_proposal(
            stored_plan=stored_plan,
            request=request,
            actor_agent_id=actor_agent_id,
            timestamp=timestamp,
            proposal_id=proposal_id,
            proposal_revision=1,
            gov_check=gov_check,
        )
        proposal_sha = proposal.compute_sha256()
        source = _actor_kind_to_event_source(auth_context.actor_kind)

        primary_ev = Event(
            event_id=new_event_id(),
            timestamp=timestamp,
            event_type="team.proposal_created",
            source=source,
            task_id=request.task_id,
            actor_id=actor_agent_id,
            correlation_id=corr_id,
            redacted_payload={
                "proposal_id": proposal.proposal_id,
                "plan_id": proposal.plan_id,
                "plan_revision": proposal.plan_revision,
                "status": proposal.status,
                "pinned_agents": [
                    {"agent_id": m.agent_id, "manifest_revision": m.manifest_revision}
                    for m in proposal.pinned_members
                ],
                "departments_involved": proposal.departments_involved,
                "missing_capabilities": proposal.missing_capabilities,
                "proposal_sha256": proposal_sha,
            },
        )
        extra_events: list[Event] = []
        if proposal.missing_capabilities:
            extra_events.append(
                Event(
                    event_id=new_event_id(),
                    timestamp=timestamp,
                    event_type="team.missing_capability",
                    source=source,
                    task_id=request.task_id,
                    actor_id=actor_agent_id,
                    correlation_id=corr_id,
                    redacted_payload={
                        "proposal_id": proposal.proposal_id,
                        "plan_id": proposal.plan_id,
                        "status": proposal.status,
                        "missing_capabilities": proposal.missing_capabilities,
                    },
                )
            )

        stored = StoredTeamProposal(
            proposal_id=proposal.proposal_id,
            task_id=proposal.task_id,
            plan_id=proposal.plan_id,
            plan_revision=proposal.plan_revision,
            active_revision=1,
            record_version=1,
            status=proposal.status,
            proposal=proposal,
            proposal_sha256=proposal_sha,
            created_by=actor_agent_id,
            created_at=timestamp,
            updated_by=actor_agent_id,
            updated_at=timestamp,
            last_audit_event_id=primary_ev.event_id,
        )
        rev_row = TeamProposalRevisionRecord(
            proposal_id=proposal.proposal_id,
            task_id=proposal.task_id,
            proposal_revision=1,
            status=proposal.status,
            proposal=proposal,
            proposal_sha256=proposal_sha,
            change_summary=proposal.change_summary,
            changed_by=actor_agent_id,
            changed_at=timestamp,
            audit_event_id=primary_ev.event_id,
        )
        return self._repo.create_proposal(stored, rev_row, primary_ev, extra_events=extra_events)

    def check_proposal_staleness(
        self,
        proposal_id: str,
        *,
        timestamp: datetime,
        auth_context: TrustedAuthContext | None = None,
        persist_if_stale: bool = True,
        correlation_id: str | None = None,
    ) -> TeamStalenessReport:
        """Compare pinned Agent Manifest revisions and statuses against live Registry."""
        validate_aware_datetime(timestamp, "timestamp")
        stored = self._repo.get_proposal(proposal_id)
        if stored is None:
            raise TeamProposalNotFoundError(
                "TEAM_PROPOSAL_NOT_FOUND",
                f"TeamProposal '{proposal_id}' does not exist.",
            )

        issues: list[AgentStalenessIssue] = []
        for pinned in stored.proposal.pinned_members:
            live = self._registry.get_agent(pinned.agent_id)
            if live is None:
                issues.append(
                    AgentStalenessIssue(
                        agent_id=pinned.agent_id,
                        pinned_revision=pinned.manifest_revision,
                        current_revision=None,
                        current_status=None,
                        issue_code="AGENT_MISSING",
                        detail=f"Pinned agent '{pinned.agent_id}' is no longer present in Registry.",
                    )
                )
                continue
            if live.registration_status != "ACTIVE":
                issues.append(
                    AgentStalenessIssue(
                        agent_id=pinned.agent_id,
                        pinned_revision=pinned.manifest_revision,
                        current_revision=live.active_revision,
                        current_status=live.registration_status,
                        issue_code="AGENT_NOT_ACTIVE",
                        detail=(
                            f"Pinned agent '{pinned.agent_id}' transitioned to status "
                            f"'{live.registration_status}'."
                        ),
                    )
                )
            if live.active_revision != pinned.manifest_revision:
                issues.append(
                    AgentStalenessIssue(
                        agent_id=pinned.agent_id,
                        pinned_revision=pinned.manifest_revision,
                        current_revision=live.active_revision,
                        current_status=live.registration_status,
                        issue_code="MANIFEST_REVISION_DRIFT",
                        detail=(
                            f"Pinned manifest revision v{pinned.manifest_revision} drifted to "
                            f"live revision v{live.active_revision}."
                        ),
                    )
                )
            elif live.manifest_sha256 != pinned.manifest_sha256:
                issues.append(
                    AgentStalenessIssue(
                        agent_id=pinned.agent_id,
                        pinned_revision=pinned.manifest_revision,
                        current_revision=live.active_revision,
                        current_status=live.registration_status,
                        issue_code="MANIFEST_HASH_DRIFT",
                        detail=(
                            f"Pinned manifest SHA-256 '{pinned.manifest_sha256}' differs from "
                            f"live SHA-256 '{live.manifest_sha256}'."
                        ),
                    )
                )

        is_stale = len(issues) > 0
        if is_stale and persist_if_stale and stored.status != "STALE":
            actor_id = (
                _resolve_team_actor_id(auth_context) if auth_context is not None else "core.master"
            )
            source: EventSource = (
                _actor_kind_to_event_source(auth_context.actor_kind)
                if auth_context is not None
                else "core_runtime"
            )
            next_rev = stored.active_revision + 1
            stale_summary = f"Detected Registry drift on {len(issues)} pinned team member(s)"
            stale_proposal = stored.proposal.model_copy(
                update={
                    "proposal_revision": next_rev,
                    "status": "STALE",
                    "change_summary": stale_summary,
                }
            )
            stale_sha = stale_proposal.compute_sha256()
            stale_ev = Event(
                event_id=new_event_id(),
                timestamp=timestamp,
                event_type="team.stale_detected",
                source=source,
                task_id=stored.task_id,
                actor_id=actor_id,
                correlation_id=correlation_id or f"corr-team-stale-{proposal_id}",
                redacted_payload={
                    "proposal_id": proposal_id,
                    "issues": [item.model_dump(mode="json") for item in issues],
                    "previous_status": stored.status,
                },
            )
            updated_stored = StoredTeamProposal(
                proposal_id=proposal_id,
                task_id=stored.task_id,
                plan_id=stored.plan_id,
                plan_revision=stored.plan_revision,
                active_revision=next_rev,
                record_version=stored.record_version + 1,
                status="STALE",
                proposal=stale_proposal,
                proposal_sha256=stale_sha,
                created_by=stored.created_by,
                created_at=stored.created_at,
                updated_by=actor_id,
                updated_at=timestamp,
                last_audit_event_id=stale_ev.event_id,
            )
            rev_row = TeamProposalRevisionRecord(
                proposal_id=proposal_id,
                task_id=stored.task_id,
                proposal_revision=next_rev,
                status="STALE",
                proposal=stale_proposal,
                proposal_sha256=stale_sha,
                change_summary=stale_summary,
                changed_by=actor_id,
                changed_at=timestamp,
                audit_event_id=stale_ev.event_id,
            )
            self._repo.revise_proposal(
                updated_stored,
                rev_row,
                expected_record_version=stored.record_version,
                audit_event=stale_ev,
            )

        return TeamStalenessReport(
            proposal_id=proposal_id,
            task_id=stored.task_id,
            is_stale=is_stale,
            checked_at=timestamp,
            issues=issues,
        )

    def rebuild_team_proposal(
        self,
        proposal_id: str,
        *,
        expected_record_version: int,
        request: TeamBuildRequest,
        auth_context: TrustedAuthContext | None,
        timestamp: datetime,
        approval_id: str | None = None,
        correlation_id: str | None = None,
    ) -> StoredTeamProposal:
        """Create a new immutable TeamProposal revision using live Registry and Plan state."""
        validate_aware_datetime(timestamp, "timestamp")
        current = self._repo.get_proposal(proposal_id)
        if current is None:
            raise TeamProposalNotFoundError(
                "TEAM_PROPOSAL_NOT_FOUND",
                f"TeamProposal '{proposal_id}' does not exist.",
            )

        stored_plan = self._plan_repo.get_plan(request.plan_id)
        if stored_plan is None:
            raise PlanNotFoundError("PLAN_NOT_FOUND", f"Plan '{request.plan_id}' does not exist.")
        if stored_plan.task_id != current.task_id or request.task_id != current.task_id:
            raise TeamAssignmentStateError(
                "PLAN_TASK_MISMATCH",
                f"Rebuild request does not match existing TeamProposal task '{current.task_id}'.",
            )

        corr_id = correlation_id or f"corr-team-rev-{proposal_id}"
        decision, _ = self._authorize_build_or_evaluate(
            auth_context=auth_context,
            task_id=current.task_id,
            resource=f"team:{proposal_id}",
            action="agent.read",
            timestamp=timestamp,
            parameters={
                "proposal_id": proposal_id,
                "expected_record_version": expected_record_version,
                "plan_id": request.plan_id,
            },
            approval_id=approval_id,
            correlation_id=corr_id,
        )
        assert auth_context is not None
        actor_agent_id = _resolve_team_actor_id(auth_context)
        next_rev = current.active_revision + 1
        gov_check = TeamGovernanceCheck(
            action="agent.read",
            outcome="ALLOW",
            reason=decision.message,
        )
        rebuilt = self._compute_proposal(
            stored_plan=stored_plan,
            request=request,
            actor_agent_id=actor_agent_id,
            timestamp=timestamp,
            proposal_id=proposal_id,
            proposal_revision=next_rev,
            gov_check=gov_check,
        )
        rebuilt_sha = rebuilt.compute_sha256()
        rev_ev = Event(
            event_id=new_event_id(),
            timestamp=timestamp,
            event_type="team.proposal_revised",
            source=_actor_kind_to_event_source(auth_context.actor_kind),
            task_id=current.task_id,
            actor_id=actor_agent_id,
            correlation_id=corr_id,
            redacted_payload={
                "proposal_id": proposal_id,
                "previous_revision": current.active_revision,
                "new_revision": next_rev,
                "status": rebuilt.status,
                "pinned_agents": [
                    {"agent_id": m.agent_id, "manifest_revision": m.manifest_revision}
                    for m in rebuilt.pinned_members
                ],
                "change_summary": request.change_summary,
                "proposal_sha256": rebuilt_sha,
            },
        )
        extra_events: list[Event] = []
        if rebuilt.missing_capabilities:
            extra_events.append(
                Event(
                    event_id=new_event_id(),
                    timestamp=timestamp,
                    event_type="team.missing_capability",
                    source=_actor_kind_to_event_source(auth_context.actor_kind),
                    task_id=current.task_id,
                    actor_id=actor_agent_id,
                    correlation_id=corr_id,
                    redacted_payload={
                        "proposal_id": proposal_id,
                        "status": rebuilt.status,
                        "missing_capabilities": rebuilt.missing_capabilities,
                    },
                )
            )

        updated_stored = StoredTeamProposal(
            proposal_id=proposal_id,
            task_id=current.task_id,
            plan_id=rebuilt.plan_id,
            plan_revision=rebuilt.plan_revision,
            active_revision=next_rev,
            record_version=current.record_version + 1,
            status=rebuilt.status,
            proposal=rebuilt,
            proposal_sha256=rebuilt_sha,
            created_by=current.created_by,
            created_at=current.created_at,
            updated_by=actor_agent_id,
            updated_at=timestamp,
            last_audit_event_id=rev_ev.event_id,
        )
        rev_row = TeamProposalRevisionRecord(
            proposal_id=proposal_id,
            task_id=current.task_id,
            proposal_revision=next_rev,
            status=rebuilt.status,
            proposal=rebuilt,
            proposal_sha256=rebuilt_sha,
            change_summary=request.change_summary,
            changed_by=actor_agent_id,
            changed_at=timestamp,
            audit_event_id=rev_ev.event_id,
        )
        return self._repo.revise_proposal(
            updated_stored,
            rev_row,
            expected_record_version=expected_record_version,
            audit_event=rev_ev,
            extra_events=extra_events,
        )

    def apply_team_assignment(
        self,
        proposal_id: str,
        *,
        expected_proposal_record_version: int,
        auth_context: TrustedAuthContext | None,
        timestamp: datetime,
        approval_id: str | None = None,
        correlation_id: str | None = None,
    ) -> TeamAssignmentResult:
        """Assign pinned team onto Task & PENDING ChecklistItems without starting execution."""
        validate_aware_datetime(timestamp, "timestamp")
        stored = self._repo.get_proposal(proposal_id)
        if stored is None:
            raise TeamProposalNotFoundError(
                "TEAM_PROPOSAL_NOT_FOUND",
                f"TeamProposal '{proposal_id}' does not exist.",
            )

        corr_id = correlation_id or f"corr-team-assign-{proposal_id}"
        stored_plan = self._plan_repo.get_plan(stored.plan_id)
        routing_ctx = None
        if stored_plan is not None:
            rp = stored_plan.plan.routing_proposal
            eff_provider = rp.provider_id or rp.engine_id
            routing_ctx = RoutingContext(
                provider_id=eff_provider,
                connection_id=rp.connection_id,
                model_id=rp.model_id,
                data_residency="local_only" if rp.data_residency_policy == "local_only" else "cloud",
                target_residency="local_only" if eff_provider in self._local_providers else "cloud",
                allowed_providers=sorted(self._allowed_providers) if self._allowed_providers else [],
                allowed_connections=sorted(self._allowed_connections) if self._allowed_connections else [],
            )

        def _execute_assignment() -> TeamAssignmentResult:
            latest = self._repo.get_proposal(proposal_id)
            if latest is None:
                raise TeamProposalNotFoundError(
                    "TEAM_PROPOSAL_NOT_FOUND",
                    f"TeamProposal '{proposal_id}' does not exist.",
                )
            if latest.record_version != expected_proposal_record_version:
                raise StaleTeamProposalError(
                    "STALE_PROPOSAL_VERSION",
                    (
                        f"Stale TeamProposal update for '{proposal_id}': expected "
                        f"record_version={expected_proposal_record_version}, found {latest.record_version}."
                    ),
                )
            if latest.status == "STALE":
                raise StaleTeamProposalError(
                    "PROPOSAL_STALE",
                    f"TeamProposal '{proposal_id}' is marked STALE and must be rebuilt before assignment.",
                )
            if latest.status != "READY_FOR_ASSIGNMENT":
                raise TeamAssignmentStateError(
                    "PROPOSAL_NOT_ASSIGNABLE",
                    f"Cannot assign TeamProposal '{proposal_id}' in status '{latest.status}'.",
                )
            latest_plan = self._plan_repo.get_plan(latest.plan_id)
            if latest_plan is not None and latest_plan.active_revision != latest.plan_revision:
                raise StaleTeamProposalError(
                    "PLAN_REVISION_DRIFT",
                    (
                        f"TeamProposal '{proposal_id}' was built for plan revision v{latest.plan_revision}, "
                        f"but live plan '{latest.plan_id}' is at revision v{latest_plan.active_revision}."
                    ),
                )

            staleness = self.check_proposal_staleness(
                proposal_id,
                timestamp=timestamp,
                auth_context=auth_context,
                persist_if_stale=False,
            )
            if staleness.is_stale:
                codes = [item.issue_code for item in staleness.issues]
                raise StaleTeamProposalError(
                    "STALE_MANIFEST_PIN",
                    f"TeamProposal '{proposal_id}' cannot be assigned due to Registry drift: {codes}.",
                )

            assert auth_context is not None
            actor_agent_id = _resolve_team_actor_id(auth_context)
            assigned_agent_ids = sorted({m.agent_id for m in latest.proposal.pinned_members})
            if not assigned_agent_ids or "core.master" in assigned_agent_ids:
                raise TeamAssignmentStateError(
                    "INVALID_TEAM_MEMBERS",
                    "Team assignment must specify non-empty Worker agent IDs excluding core.master.",
                )

            step_agent_map: dict[str, str] = {}
            live_step_chk_by_id = (
                {step.step_id: step.checklist_item_id for step in latest_plan.plan.steps}
                if latest_plan is not None
                else {}
            )
            for entry in latest.proposal.step_assignments:
                chk_id = entry.checklist_item_id or live_step_chk_by_id.get(entry.step_id)
                if chk_id is not None and entry.assigned_agent_id is not None:
                    step_agent_map[chk_id] = entry.assigned_agent_id

            next_rev = latest.active_revision + 1
            assign_summary = (
                f"Assigned pinned team {assigned_agent_ids} to task '{latest.task_id}' "
                f"(no Run started; ChecklistItems remain PENDING)."
            )
            next_checks = list(latest.proposal.governance_checks)
            next_checks.append(
                TeamGovernanceCheck(
                    action="agent.assign",
                    outcome="ALLOW",
                    reason="Authorized via PermissionManager.dispatch_protected_action",
                    approval_request_id=approval_id,
                )
            )
            assigned_proposal = latest.proposal.model_copy(
                update={
                    "proposal_revision": next_rev,
                    "status": "ASSIGNED",
                    "governance_checks": next_checks,
                    "change_summary": assign_summary,
                }
            )
            assigned_sha = assigned_proposal.compute_sha256()

            team_ev = Event(
                event_id=new_event_id(),
                timestamp=timestamp,
                event_type="team.assigned",
                source=_actor_kind_to_event_source(auth_context.actor_kind),
                task_id=latest.task_id,
                actor_id=actor_agent_id,
                correlation_id=corr_id,
                redacted_payload={
                    "proposal_id": proposal_id,
                    "proposal_revision": next_rev,
                    "plan_id": latest.plan_id,
                    "plan_revision": latest.plan_revision,
                    "assigned_agents": [
                        {
                            "agent_id": m.agent_id,
                            "manifest_revision": m.manifest_revision,
                            "manifest_sha256": m.manifest_sha256,
                        }
                        for m in assigned_proposal.pinned_members
                    ],
                    "checklist_assignments": step_agent_map,
                    "proposal_sha256": assigned_sha,
                },
            )
            updated_stored = StoredTeamProposal(
                proposal_id=proposal_id,
                task_id=latest.task_id,
                plan_id=latest.plan_id,
                plan_revision=latest.plan_revision,
                active_revision=next_rev,
                record_version=latest.record_version + 1,
                status="ASSIGNED",
                proposal=assigned_proposal,
                proposal_sha256=assigned_sha,
                created_by=latest.created_by,
                created_at=latest.created_at,
                updated_by=actor_agent_id,
                updated_at=timestamp,
                last_audit_event_id=team_ev.event_id,
            )
            rev_row = TeamProposalRevisionRecord(
                proposal_id=proposal_id,
                task_id=latest.task_id,
                proposal_revision=next_rev,
                status="ASSIGNED",
                proposal=assigned_proposal,
                proposal_sha256=assigned_sha,
                change_summary=assign_summary,
                changed_by=actor_agent_id,
                changed_at=timestamp,
                audit_event_id=team_ev.event_id,
            )
            return self._repo.apply_team_assignment(
                proposal_id=proposal_id,
                expected_proposal_record_version=expected_proposal_record_version,
                updated_proposal=updated_stored,
                new_revision=rev_row,
                assigned_agent_ids=assigned_agent_ids,
                step_agent_map=step_agent_map,
                actor_id=actor_agent_id,
                actor_kind=auth_context.actor_kind,
                timestamp=timestamp,
                team_event=team_ev,
            )

        _, result = self._authorize_build_or_evaluate(
            auth_context=auth_context,
            task_id=stored.task_id,
            resource=f"task:{stored.task_id}",
            action="agent.assign",
            timestamp=timestamp,
            handler=_execute_assignment,
            routing=routing_ctx,
            parameters={
                "proposal_id": proposal_id,
                "task_id": stored.task_id,
                "assigned_agents": [m.agent_id for m in stored.proposal.pinned_members],
                "expected_record_version": expected_proposal_record_version,
            },
            approval_id=approval_id,
            correlation_id=corr_id,
        )
        return result

    def get_proposal(self, proposal_id: str) -> StoredTeamProposal | None:
        """Load active TeamProposal snapshot by ``proposal_id``."""
        return self._repo.get_proposal(proposal_id)

    def get_active_proposal_for_task(self, task_id: str) -> StoredTeamProposal | None:
        """Load latest TeamProposal bound to ``task_id``."""
        return self._repo.get_active_proposal_for_task(task_id)

    def list_proposal_revisions(self, proposal_id: str) -> list[TeamProposalRevisionRecord]:
        """Return immutable TeamProposal revision history ordered ascending."""
        return self._repo.list_proposal_revisions(proposal_id)
