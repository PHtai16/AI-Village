"""Structured Multi-Agent Meetings, Critique Limits, and Master Decision Records (P06.01-P06.04)."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import TYPE_CHECKING, Any

from core.contracts.event import Event, sanitize_event_payload
from core.contracts.identifiers import ensure_not_secret, new_event_id, validate_aware_datetime
from core.events.journal import EventJournal
from core.governance.manager import GovernanceAuditError, PermissionDeniedError, PermissionManager
from core.governance.models import PolicyRequest
from core.orchestration.meeting_models import (
    VALID_MEETING_TRANSITIONS,
    CollaborationRepository,
    DecisionSubstituteQaForbiddenError,
    FakeConsensusRejectedError,
    MasterDecisionRecord,
    MasterDecisionRequest,
    MeetingComplexitySignal,
    MeetingEvidenceError,
    MeetingNecessityOutcome,
    MeetingNotFoundError,
    MeetingOpenRequest,
    MeetingParticipant,
    MeetingPermissionError,
    MeetingRecord,
    MeetingStateError,
    MeetingTurnLimitExceededError,
    MeetingTurnRecord,
    MeetingTurnRequest,
    StoredMeeting,
    actor_kind_to_event_source,
    build_trusted_auth,
    new_decision_id,
    new_meeting_id,
)
from core.orchestration.planning_models import PlanRepository
from core.orchestration.team_models import TeamProposalRepository
from core.tasks.transitions import TERMINAL_RUN_STATUSES, TERMINAL_TASK_STATUSES

if TYPE_CHECKING:
    from core.agents.registry import AgentRegistryService

logger = logging.getLogger(__name__)


class MeetingCoordinatorService:
    """Coordinates P06.01-P06.04 real Multi-Agent meetings, critique limits, and Master decisions."""

    def __init__(
        self,
        *,
        collaboration_repo: CollaborationRepository,
        task_repo: Any,
        run_repo: Any,
        plan_repo: PlanRepository,
        team_repo: TeamProposalRepository,
        agent_registry: AgentRegistryService,
        permission_manager: PermissionManager,
        event_journal: EventJournal,
    ) -> None:
        """Inject explicit storage ports, Registry, Governance, and EventJournal."""
        self._collab = collaboration_repo
        self._tasks = task_repo
        self._runs = run_repo
        self._plans = plan_repo
        self._teams = team_repo
        self._registry = agent_registry
        self._permissions = permission_manager
        self._events = event_journal

    def _authorize(
        self,
        *,
        actor_id: str,
        actor_kind: str,
        action: str,
        resource: str,
        task_id: str | None,
        run_id: str | None,
        timestamp: datetime,
        correlation_id: str,
        parameters: dict[str, Any] | None = None,
    ) -> None:
        """Enforce Governance action authorization fail-closed."""
        req = PolicyRequest(
            auth_context=build_trusted_auth(actor_id, actor_kind),
            action=action,
            resource=resource,
            task_id=task_id,
            run_id=run_id,
            timestamp=timestamp,
            correlation_id=correlation_id,
            parameters=parameters or {},
        )
        try:
            decision = self._permissions.evaluate_and_audit(req)
        except PermissionDeniedError as exc:
            raise MeetingPermissionError(exc.decision.reason_code, exc.decision.message) from exc
        except GovernanceAuditError as exc:
            raise MeetingPermissionError("GOVERNANCE_AUDIT_FAILED", str(exc)) from exc
        if decision.decision != "ALLOW":
            raise MeetingPermissionError(decision.reason_code, decision.message)

    def evaluate_meeting_necessity(
        self,
        *,
        signals: MeetingComplexitySignal,
        team_proposal_id: str,
        actor_id: str = "core.master",
        actor_kind: str = "orchestrator",
        timestamp: datetime,
        correlation_id: str | None = None,
    ) -> MeetingNecessityOutcome:
        """Select only relevant ACTIVE participants, or skip meeting cleanly for simple tasks (P06.01)."""
        validate_aware_datetime(timestamp, "timestamp")
        task = self._tasks.get_task(signals.task_id)
        if task is None:
            raise MeetingNotFoundError("TASK_NOT_FOUND", f"Task '{signals.task_id}' does not exist.")
        stored_team = self._teams.get_proposal(team_proposal_id)
        if stored_team is None or stored_team.task_id != task.task_id:
            raise MeetingNotFoundError("TEAM_NOT_FOUND", f"TeamProposal '{team_proposal_id}' missing or foreign.")

        corr_id = correlation_id or f"corr-mtg-eval-{task.task_id}"
        self._authorize(
            actor_id=actor_id,
            actor_kind=actor_kind,
            action="meeting.convene",
            resource=f"task:{task.task_id}/meeting:necessity",
            task_id=task.task_id,
            run_id=signals.run_id,
            timestamp=timestamp,
            correlation_id=corr_id,
        )

        meeting_required = (
            signals.has_technical_disagreement
            or signals.has_cross_step_conflict
            or signals.has_high_risk_tradeoff
            or signals.qa_failure_requires_triage
            or signals.step_count > 2
        )
        if not meeting_required:
            skip_ev = Event(
                event_id=new_event_id(),
                timestamp=timestamp,
                event_type="meeting.skipped",
                source=actor_kind_to_event_source(actor_kind),
                task_id=task.task_id,
                run_id=signals.run_id,
                actor_id=actor_id,
                correlation_id=corr_id,
                redacted_payload=sanitize_event_payload(
                    {"reason": signals.reason, "step_count": signals.step_count}
                ),
            )
            res = self._events.append(skip_ev)
            if not res.appended:
                raise MeetingStateError("EVENT_APPEND_FAILED", "Failed to record meeting.skipped event.")
            return MeetingNecessityOutcome(
                meeting_required=False,
                reason=signals.reason,
                eligible_participants=[],
                event_id=skip_ev.event_id,
            )

        wanted_steps = set(signals.involved_step_ids)
        eligible: list[str] = ["core.master"]
        for assign in stored_team.proposal.step_assignments:
            if wanted_steps and assign.step_id not in wanted_steps:
                continue
            live = self._registry.get_agent(assign.assigned_agent_id)
            if live is not None and live.registration_status == "ACTIVE":
                if assign.assigned_agent_id not in eligible:
                    eligible.append(assign.assigned_agent_id)

        return MeetingNecessityOutcome(
            meeting_required=True,
            reason=signals.reason,
            eligible_participants=eligible,
            event_id=None,
        )

    def open_meeting(self, request: MeetingOpenRequest) -> StoredMeeting:
        """Open a structured Multi-Agent meeting strictly with relevant ACTIVE team members."""
        validate_aware_datetime(request.timestamp, "timestamp")
        meeting_id = request.meeting_id or new_meeting_id()
        corr_id = request.correlation_id or f"corr-{meeting_id}"

        self._authorize(
            actor_id=request.actor_id,
            actor_kind=request.actor_kind,
            action="meeting.convene",
            resource=f"task:{request.task_id}/meeting:{meeting_id}",
            task_id=request.task_id,
            run_id=request.run_id,
            timestamp=request.timestamp,
            correlation_id=corr_id,
            parameters={"agenda": ensure_not_secret(request.agenda, "agenda")},
        )

        task = self._tasks.get_task(request.task_id)
        if task is None or task.status in TERMINAL_TASK_STATUSES:
            raise MeetingStateError("INVALID_TASK_FOR_MEETING", f"Task '{request.task_id}' missing or terminal.")
        run = self._runs.get_run(request.run_id)
        if run is None or run.task_id != task.task_id or run.status in TERMINAL_RUN_STATUSES:
            raise MeetingStateError("INVALID_RUN_FOR_MEETING", f"Run '{request.run_id}' missing, foreign, or terminal.")
        stored_plan = self._plans.get_plan(request.plan_id)
        if stored_plan is None or stored_plan.task_id != task.task_id or stored_plan.status != "APPLIED":
            raise MeetingStateError("PLAN_NOT_APPLIED", f"Plan '{request.plan_id}' missing or not APPLIED.")
        stored_team = self._teams.get_proposal(request.team_proposal_id)
        if stored_team is None or stored_team.task_id != task.task_id or stored_team.status != "ASSIGNED":
            raise MeetingStateError("TEAM_NOT_ASSIGNED", f"TeamProposal '{request.team_proposal_id}' not ASSIGNED.")

        assigns_by_agent: dict[str, list[str]] = {}
        for entry in stored_team.proposal.step_assignments:
            if request.involved_step_ids and entry.step_id not in request.involved_step_ids:
                continue
            assigns_by_agent.setdefault(entry.assigned_agent_id, []).append(entry.step_id)

        participants: list[MeetingParticipant] = [
            MeetingParticipant(agent_id="core.master", role_in_meeting="chair", step_ids=[])
        ]
        seen_ids = {"core.master"}
        for agent_id in request.participant_agent_ids:
            if agent_id in seen_ids:
                continue
            if agent_id not in assigns_by_agent:
                raise MeetingStateError(
                    "UNRELATED_PARTICIPANT",
                    f"Agent '{agent_id}' is not assigned to the relevant steps in TeamProposal '{request.team_proposal_id}'.",
                )
            live = self._registry.get_agent(agent_id)
            if live is None or live.registration_status != "ACTIVE":
                raise MeetingStateError(
                    "INACTIVE_PARTICIPANT",
                    f"Participant '{agent_id}' must be ACTIVE in AgentRegistryService.",
                )
            role_kind = "verifier" if live.manifest.department == "qa" else "contributor"
            participants.append(
                MeetingParticipant(
                    agent_id=agent_id,
                    role_in_meeting=role_kind,
                    step_ids=assigns_by_agent[agent_id],
                )
            )
            seen_ids.add(agent_id)

        if len(participants) < 2:
            raise MeetingStateError(
                "INSUFFICIENT_PARTICIPANTS",
                "A Multi-Agent meeting requires Master chair plus at least one assigned worker.",
            )

        meeting = MeetingRecord(
            meeting_id=meeting_id,
            task_id=task.task_id,
            run_id=run.run_id,
            plan_id=stored_plan.plan_id,
            team_proposal_id=stored_team.proposal_id,
            agenda=ensure_not_secret(request.agenda, "agenda"),
            chair_id="core.master",
            participants=participants,
            max_rounds=request.max_rounds,
            max_turns=request.max_turns,
            turns=[],
            decision_ids=[],
            status="OPEN",
            correlation_id=corr_id,
            created_at=request.timestamp,
        )
        event = Event(
            event_id=new_event_id(),
            timestamp=request.timestamp,
            event_type="meeting.opened",
            source=actor_kind_to_event_source(request.actor_kind),
            task_id=task.task_id,
            run_id=run.run_id,
            actor_id=request.actor_id,
            correlation_id=corr_id,
            redacted_payload=sanitize_event_payload(
                {
                    "meeting_id": meeting_id,
                    "agenda": meeting.agenda,
                    "participants": [p.agent_id for p in participants],
                    "max_rounds": meeting.max_rounds,
                    "max_turns": meeting.max_turns,
                }
            ),
        )
        return self._collab.create_meeting_atomic(meeting=meeting, event=event)

    def submit_turn(self, request: MeetingTurnRequest) -> StoredMeeting:
        """Record a structured critique turn while enforcing round/turn caps and anti-fake-consensus (P06.02-P06.03)."""
        validate_aware_datetime(request.timestamp, "timestamp")
        stored = self._collab.get_meeting(request.meeting_id)
        if stored is None:
            raise MeetingNotFoundError("MEETING_NOT_FOUND", f"Meeting '{request.meeting_id}' not found.")

        mtg = stored.meeting
        if mtg.status != "OPEN":
            raise MeetingTurnLimitExceededError(
                "MEETING_NOT_OPEN",
                f"Meeting '{mtg.meeting_id}' has status '{mtg.status}' and accepts no more turns.",
            )

        participant_ids = {p.agent_id for p in mtg.participants}
        if request.speaker_id not in participant_ids:
            raise MeetingPermissionError(
                "SPEAKER_NOT_INVITED",
                f"Speaker '{request.speaker_id}' is not an invited participant of '{mtg.meeting_id}'.",
            )
        if request.round_number > mtg.max_rounds or len(mtg.turns) >= mtg.max_turns:
            raise MeetingTurnLimitExceededError(
                "MEETING_TURN_LIMIT_EXCEEDED",
                f"Meeting '{mtg.meeting_id}' exceeded round/turn cap ({mtg.max_rounds}/{mtg.max_turns}).",
            )

        corr_id = request.correlation_id or mtg.correlation_id
        self._authorize(
            actor_id=request.speaker_id,
            actor_kind=request.speaker_kind,
            action="meeting.participate",
            resource=f"task:{mtg.task_id}/meeting:{mtg.meeting_id}",
            task_id=mtg.task_id,
            run_id=mtg.run_id,
            timestamp=request.timestamp,
            correlation_id=corr_id,
        )

        normalized_stmt = " ".join(request.statement.lower().split())
        if any(" ".join(t.statement.lower().split()) == normalized_stmt for t in mtg.turns):
            raise FakeConsensusRejectedError(
                "ECHO_CHAMBER_DUPLICATE_TURN",
                "Duplicate verbatim statement rejected as non-substantive echo-chamber consensus.",
            )

        if request.consensus_claimed:
            speakers_so_far = {t.speaker_id for t in mtg.turns} | {request.speaker_id}
            if len(speakers_so_far) < 2:
                raise FakeConsensusRejectedError(
                    "SINGLE_SPEAKER_CONSENSUS_FORBIDDEN",
                    "Cannot claim consensus with fewer than two participating speakers.",
                )
            targeted_ids = {t.target_turn_id for t in mtg.turns if t.target_turn_id is not None}
            if request.target_turn_id is not None:
                targeted_ids.add(request.target_turn_id)
            unresolved_objections = [
                t
                for t in mtg.turns
                if t.turn_kind == "OBJECTION"
                and t.turn_id not in targeted_ids
                and request.turn_kind != "EVIDENCE"
            ]
            if unresolved_objections:
                raise FakeConsensusRejectedError(
                    "UNRESOLVED_OBJECTION_CONSENSUS_FORBIDDEN",
                    "Cannot claim consensus while prior OBJECTION turns remain unanswered with counter-evidence.",
                )

        try:
            turn = MeetingTurnRecord(
                meeting_id=mtg.meeting_id,
                round_number=request.round_number,
                turn_index=len(mtg.turns) + 1,
                speaker_id=request.speaker_id,
                speaker_kind=request.speaker_kind,
                turn_kind=request.turn_kind,
                statement=request.statement,
                target_turn_id=request.target_turn_id,
                evidence_references=list(request.evidence_references),
                uncertainty_notes=list(request.uncertainty_notes),
                action_items=list(request.action_items),
                consensus_claimed=request.consensus_claimed,
                timestamp=request.timestamp,
            )
        except ValueError as exc:
            raise MeetingEvidenceError("INVALID_MEETING_TURN_EVIDENCE", str(exc)) from exc

        next_turns = [*mtg.turns, turn]
        next_status = "NEEDS_MASTER_DECISION" if len(next_turns) >= mtg.max_turns else "OPEN"
        next_mtg = mtg.model_copy(update={"turns": next_turns, "status": next_status})

        event = Event(
            event_id=new_event_id(),
            timestamp=request.timestamp,
            event_type="meeting.turn_recorded",
            source=actor_kind_to_event_source(request.speaker_kind),
            task_id=mtg.task_id,
            run_id=mtg.run_id,
            actor_id=request.speaker_id,
            correlation_id=corr_id,
            redacted_payload=sanitize_event_payload(
                {
                    "meeting_id": mtg.meeting_id,
                    "turn_id": turn.turn_id,
                    "round_number": turn.round_number,
                    "turn_index": turn.turn_index,
                    "turn_kind": turn.turn_kind,
                    "meeting_status": next_status,
                }
            ),
        )
        return self._collab.update_meeting_atomic(
            meeting=next_mtg,
            expected_record_version=stored.record_version,
            event=event,
        )

    def record_master_decision(self, request: MasterDecisionRequest) -> MasterDecisionRecord:
        """Persist a Master DecisionRecord with user-visible rationale; never replaces QA (P06.04)."""
        validate_aware_datetime(request.timestamp, "timestamp")
        if request.substitute_qa:
            raise DecisionSubstituteQaForbiddenError(
                "DECISION_CANNOT_REPLACE_QA",
                "Master DecisionRecord can never substitute for independent QA verification.",
            )

        decision_id = request.decision_id or new_decision_id()
        corr_id = request.correlation_id or f"corr-{decision_id}"
        self._authorize(
            actor_id=request.actor_id,
            actor_kind=request.actor_kind,
            action="meeting.decide",
            resource=f"task:{request.task_id}/decision:{decision_id}",
            task_id=request.task_id,
            run_id=request.run_id,
            timestamp=request.timestamp,
            correlation_id=corr_id,
        )

        if self._tasks.get_task(request.task_id) is None:
            raise MeetingNotFoundError("TASK_NOT_FOUND", f"Task '{request.task_id}' does not exist.")

        updated_meeting: MeetingRecord | None = None
        expected_ver: int | None = None
        acknowledged_dissent: list[str] = []
        run_id = request.run_id

        if request.meeting_id is not None:
            stored = self._collab.get_meeting(request.meeting_id)
            if stored is None or stored.meeting.task_id != request.task_id:
                raise MeetingNotFoundError("MEETING_NOT_FOUND", f"Meeting '{request.meeting_id}' missing or foreign.")
            mtg = stored.meeting
            if "DECIDED" not in VALID_MEETING_TRANSITIONS[mtg.status]:
                raise MeetingStateError("INVALID_DECISION_STATE", f"Cannot decide meeting in status '{mtg.status}'.")
            expected_ver = stored.record_version
            run_id = run_id or mtg.run_id
            acknowledged_dissent = [
                f"{t.speaker_id} ({t.turn_id}): {t.statement[:120]}"
                for t in mtg.turns
                if t.turn_kind == "OBJECTION"
            ]
            target_status = "CLOSED" if request.close_meeting else "DECIDED"
            updated_meeting = mtg.model_copy(
                update={
                    "status": target_status,
                    "decision_ids": [*mtg.decision_ids, decision_id],
                    "decided_at": request.timestamp,
                    "closed_at": request.timestamp if request.close_meeting else mtg.closed_at,
                }
            )

        decision = MasterDecisionRecord(
            decision_id=decision_id,
            meeting_id=request.meeting_id,
            conflict_id=request.conflict_id,
            task_id=request.task_id,
            run_id=run_id,
            decided_by=request.actor_id,
            chosen_option=request.chosen_option,
            decision_summary=request.decision_summary,
            user_visible_rationale=request.user_visible_rationale,
            acknowledged_dissent=acknowledged_dissent,
            evidence_references=list(request.evidence_references),
            action_items=list(request.action_items),
            can_substitute_qa=False,
            timestamp=request.timestamp,
        )
        events = [
            Event(
                event_id=new_event_id(),
                timestamp=request.timestamp,
                event_type="meeting.decision_recorded",
                source=actor_kind_to_event_source(request.actor_kind),
                task_id=request.task_id,
                run_id=run_id,
                actor_id=request.actor_id,
                correlation_id=corr_id,
                redacted_payload=sanitize_event_payload(
                    {
                        "decision_id": decision_id,
                        "meeting_id": request.meeting_id,
                        "chosen_option": decision.chosen_option,
                        "user_visible_rationale": decision.user_visible_rationale,
                    }
                ),
            )
        ]
        if updated_meeting is not None and updated_meeting.status == "CLOSED":
            events.append(
                Event(
                    event_id=new_event_id(),
                    timestamp=request.timestamp,
                    event_type="meeting.closed",
                    source=actor_kind_to_event_source(request.actor_kind),
                    task_id=request.task_id,
                    run_id=run_id,
                    actor_id=request.actor_id,
                    correlation_id=corr_id,
                    redacted_payload={"meeting_id": updated_meeting.meeting_id, "decision_id": decision_id},
                )
            )
        return self._collab.record_decision_atomic(
            decision=decision,
            updated_meeting=updated_meeting,
            expected_meeting_version=expected_ver,
            events=events,
        )

    def get_meeting_timeline(
        self,
        task_id: str,
    ) -> tuple[list[StoredMeeting], list[MasterDecisionRecord]]:
        """Return persisted meetings and Master decisions for a Task cleanly (empty if skipped)."""
        return self._collab.list_meetings(task_id=task_id), self._collab.list_decisions(task_id=task_id)
