"""Unit, Governance, SQLite persistence, and failure-injection tests for P06.01-P06.04."""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from core.agents.loader import load_agent_manifest_file
from core.contracts import Task, new_task_id
from core.governance.approvals import ApprovalService
from core.governance.evaluator import PolicyEvaluator
from core.governance.manager import PermissionManager
from core.orchestration import (
    CollaborationDeliveryService,
    DecisionSubstituteQaForbiddenError,
    FakeConsensusRejectedError,
    MasterDecisionRequest,
    MeetingComplexitySignal,
    MeetingCoordinatorService,
    MeetingEvidenceError,
    MeetingOpenRequest,
    MeetingPermissionError,
    MeetingStateError,
    MeetingTurnLimitExceededError,
    MeetingTurnRequest,
    PlanStepSpec,
    StructuredUserBrief,
    TeamBuildRequest,
    WorkerExecutionRequest,
)
from storage.db import sqlite_connection
from tests.orchestration.test_p05_05_06_integration_acceptance import (
    AcceptanceFakeAdapter,
    BASE_TIME,
    REPO_ROOT,
    _bootstrap_db,
    _master_auth,
    _open_stack,
    _operator_auth,
    _worker_manifest,
)

AGENTS_ROOT = REPO_ROOT / "agents"


def _attach_phase06_services(stack: dict[str, Any]) -> dict[str, Any]:
    """Attach MeetingCoordinatorService and CollaborationDeliveryService to existing acceptance stack."""
    store = stack["store"]
    approvals = ApprovalService(repository=store.approvals, journal=store.events)
    pm = PermissionManager(evaluator=PolicyEvaluator(), journal=store.events, approval_service=approvals)
    meeting_svc = MeetingCoordinatorService(
        collaboration_repo=store.collaboration,
        task_repo=store.tasks,
        run_repo=store.runs,
        plan_repo=store.plans,
        team_repo=store.teams,
        agent_registry=stack["registry"],
        permission_manager=pm,
        event_journal=store.events,
    )
    delivery_svc = CollaborationDeliveryService(
        collaboration_repo=store.collaboration,
        handoff_repo=store.handoffs,
        task_repo=store.tasks,
        run_repo=store.runs,
        metadata_repo=store.metadata,
        permission_manager=pm,
    )
    return {**stack, "meeting_svc": meeting_svc, "delivery_svc": delivery_svc, "pm": pm}


def _setup_two_worker_workflow(
    tmp_path: Path,
    *,
    execute_step1: bool = True,
    step_count: int = 2,
    adapter: AcceptanceFakeAdapter | None = None,
) -> dict[str, Any]:
    """Bootstrap SQLite, Master, IT Worker A, QA Worker B, Applied Plan, Assigned Team, and Run."""
    db_path, ws_root = _bootstrap_db(tmp_path)
    stack = _attach_phase06_services(_open_stack(db_path, ws_root, adapter=adapter))
    reg = stack["registry"]
    op_auth = _operator_auth()
    reg.register_agent(
        load_agent_manifest_file(AGENTS_ROOT / "master" / "agent.yaml", allowed_root=AGENTS_ROOT),
        auth_context=op_auth,
        timestamp=BASE_TIME,
        source_path="agents/master/agent.yaml",
    )
    reg.register_agent(
        _worker_manifest(agent_id="it.producer_a", department="it", skills=["code_generation"]),
        auth_context=op_auth,
        timestamp=BASE_TIME + timedelta(seconds=1),
        validated_capabilities=["code_generation"],
    )
    reg.register_agent(
        _worker_manifest(agent_id="qa.reviewer_b", department="qa", skills=["artifact_qa_inspection"]),
        auth_context=op_auth,
        timestamp=BASE_TIME + timedelta(seconds=2),
        validated_capabilities=["artifact_qa_inspection"],
    )
    reg.register_agent(
        _worker_manifest(agent_id="mkt.bystander", department="marketing", skills=["copywriting"]),
        auth_context=op_auth,
        timestamp=BASE_TIME + timedelta(seconds=2),
        validated_capabilities=["copywriting"],
    )

    store = stack["store"]
    task = store.tasks.create_task(
        Task(
            task_id=new_task_id(),
            title="Phase 06 Multi-Agent Collaboration Task",
            description="Producer implements output, QA critiques or verifies, Master arbitrates and delivers.",
            requested_by="operator.local",
            status="QUEUED",
            created_at=BASE_TIME + timedelta(seconds=3),
        )
    ).task

    steps = [
        PlanStepSpec(
            step_id="step_01",
            order=0,
            title="Author primary artifact",
            description="Produce target file for inspection.",
            required_capabilities=["code_generation"],
            expected_outputs=[f"workspace/{task.task_id}/it.producer_a_step_01.txt"],
            allowed_read_paths=[f"workspace/{task.task_id}"],
            allowed_write_paths=[f"workspace/{task.task_id}"],
            acceptance_criteria=["Artifact passes QA inspection"],
        )
    ]
    if step_count >= 2:
        steps.append(
            PlanStepSpec(
                step_id="step_02",
                order=1,
                title="Independent QA and downstream summary",
                description="Inspect upstream work and produce final delivery artifact.",
                depends_on=["step_01"],
                required_capabilities=["artifact_qa_inspection"],
                expected_outputs=[f"workspace/{task.task_id}/qa.reviewer_b_step_02.txt"],
                allowed_read_paths=[f"workspace/{task.task_id}"],
                allowed_write_paths=[f"workspace/{task.task_id}"],
                acceptance_criteria=["QA verified final deliverable"],
            )
        )

    brief = StructuredUserBrief(
        task_id=task.task_id,
        title=task.title,
        objective=task.description,
        requested_by="operator.local",
        required_outputs=[steps[-1].expected_outputs[0]],
        acceptance_criteria=["Verified final deliverable"],
        preferred_engine_id="antigravity",
        preferred_provider_id="antigravity",
        preferred_connection_id="ag_shared",
        preferred_model_id="gemini-2.5-flash",
    )
    m_auth = _master_auth()
    plan_res = stack["planner"].create_plan(brief=brief, steps=steps, auth_context=m_auth, timestamp=BASE_TIME + timedelta(seconds=4))
    plan, _ = stack["planner"].apply_plan_to_task(
        plan_res.stored_plan.plan_id,
        expected_plan_record_version=plan_res.stored_plan.record_version,
        auth_context=m_auth,
        timestamp=BASE_TIME + timedelta(seconds=5),
    )
    prop = stack["team_builder"].build_team_proposal(
        TeamBuildRequest(task_id=task.task_id, plan_id=plan.plan_id),
        auth_context=m_auth,
        timestamp=BASE_TIME + timedelta(seconds=6),
    )
    assigned = stack["team_builder"].apply_team_assignment(
        prop.proposal_id,
        expected_proposal_record_version=prop.record_version,
        auth_context=m_auth,
        timestamp=BASE_TIME + timedelta(seconds=7),
    )

    out_a = None
    if execute_step1:
        out_a = stack["session_svc"].execute_worker_step(
            WorkerExecutionRequest(
                task_id=task.task_id,
                plan_id=plan.plan_id,
                team_proposal_id=assigned.stored_proposal.proposal_id,
                step_id="step_01",
                agent_id="it.producer_a",
                actor_id="core.master",
                actor_kind="orchestrator",
                workspace_root=str(ws_root),
                workspace_scope=f"workspace/{task.task_id}",
                timestamp=BASE_TIME + timedelta(seconds=8),
            )
        )

    return {
        **stack,
        "db_path": db_path,
        "ws_root": ws_root,
        "task": task,
        "plan": plan,
        "team": assigned.stored_proposal,
        "out_a": out_a,
    }


def test_p06_ast_purity_and_simple_task_skips_meeting(tmp_path: Path) -> None:
    """Verify core/orchestration Phase 06 AST purity and that simple tasks skip fake meetings."""
    forbidden = {"sqlite3", "storage", "sqlalchemy", "subprocess", "crewai", "api", "ui", "httpx", "urllib", "requests"}
    for rel in (
        "core/orchestration/meeting_models.py",
        "core/orchestration/meetings.py",
        "core/orchestration/delivery.py",
    ):
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden

    ctx = _setup_two_worker_workflow(tmp_path, execute_step1=True, step_count=1)
    with ctx["store"]:
        outcome = ctx["meeting_svc"].evaluate_meeting_necessity(
            signals=MeetingComplexitySignal(
                task_id=ctx["task"].task_id,
                run_id=ctx["out_a"].run_id,
                step_count=1,
                reason="Single unambiguous worker step needs no meeting.",
            ),
            team_proposal_id=ctx["team"].proposal_id,
            timestamp=BASE_TIME + timedelta(seconds=30),
        )
        assert outcome.meeting_required is False
        assert outcome.event_id is not None
        assert outcome.eligible_participants == []
        meetings, decisions = ctx["meeting_svc"].get_meeting_timeline(ctx["task"].task_id)
        assert meetings == [] and decisions == []
        events = [e.event.event_type for e in ctx["store"].events.read_entries(task_id=ctx["task"].task_id)]
        assert "meeting.skipped" in events


def test_p06_01_04_structured_debate_turn_limits_and_decision_restart(tmp_path: Path) -> None:
    """Verify scoped participants, evidence-backed turns, turn cap arbitration, immutable decision & restart."""
    ctx = _setup_two_worker_workflow(tmp_path, execute_step1=True, step_count=2)
    t0 = BASE_TIME + timedelta(seconds=30)

    with ctx["store"]:
        mtg_svc: MeetingCoordinatorService = ctx["meeting_svc"]
        necessity = mtg_svc.evaluate_meeting_necessity(
            signals=MeetingComplexitySignal(
                task_id=ctx["task"].task_id,
                run_id=ctx["out_a"].run_id,
                step_count=2,
                qa_failure_requires_triage=True,
                reason="QA detected defect requiring structured producer/QA triage.",
            ),
            team_proposal_id=ctx["team"].proposal_id,
            timestamp=t0,
        )
        assert necessity.meeting_required is True
        assert "mkt.bystander" not in necessity.eligible_participants

        # Unrelated department rejected from convening
        with pytest.raises(MeetingStateError, match="not assigned"):
            mtg_svc.open_meeting(
                MeetingOpenRequest(
                    task_id=ctx["task"].task_id,
                    run_id=ctx["out_a"].run_id,
                    plan_id=ctx["plan"].plan_id,
                    team_proposal_id=ctx["team"].proposal_id,
                    agenda="Triage output defect",
                    participant_agent_ids=["it.producer_a", "mkt.bystander"],
                    max_rounds=2,
                    max_turns=3,
                    timestamp=t0 + timedelta(seconds=1),
                )
            )

        opened = mtg_svc.open_meeting(
            MeetingOpenRequest(
                task_id=ctx["task"].task_id,
                run_id=ctx["out_a"].run_id,
                plan_id=ctx["plan"].plan_id,
                team_proposal_id=ctx["team"].proposal_id,
                agenda="Resolve QA checksum defect on step_01 output",
                participant_agent_ids=["it.producer_a", "qa.reviewer_b"],
                max_rounds=2,
                max_turns=3,
                timestamp=t0 + timedelta(seconds=2),
            )
        )
        mtg_id = opened.meeting.meeting_id

        # Turn 1: Proposal
        after_t1 = mtg_svc.submit_turn(
            MeetingTurnRequest(
                meeting_id=mtg_id,
                round_number=1,
                speaker_id="it.producer_a",
                turn_kind="PROPOSAL",
                statement="Retain existing step_01 text formatting.",
                evidence_references=["ev_step01_draft"],
                timestamp=t0 + timedelta(seconds=3),
            )
        )
        # Turn 2: Evidence-backed objection
        after_t2 = mtg_svc.submit_turn(
            MeetingTurnRequest(
                meeting_id=mtg_id,
                round_number=1,
                speaker_id="qa.reviewer_b",
                turn_kind="OBJECTION",
                target_turn_id=after_t1.meeting.turns[0].turn_id,
                statement="File fails UTF-8 LF expected marker check.",
                evidence_references=["ev_qa_fail_digest"],
                uncertainty_notes=["Verify downstream consumer encoding expectations."],
                timestamp=t0 + timedelta(seconds=4),
            )
        )
        # Turn 3 hits max_turns=3 -> auto-transitions NEEDS_MASTER_DECISION
        after_t3 = mtg_svc.submit_turn(
            MeetingTurnRequest(
                meeting_id=mtg_id,
                round_number=2,
                speaker_id="it.producer_a",
                turn_kind="EVIDENCE",
                target_turn_id=after_t2.meeting.turns[1].turn_id,
                statement="Patch generator to enforce UTF-8 LF marker and re-run QA.",
                evidence_references=["ev_patch_plan_v2"],
                action_items=["Update step_01 output and re-run QA"],
                timestamp=t0 + timedelta(seconds=5),
            )
        )
        assert after_t3.meeting.status == "NEEDS_MASTER_DECISION"

        # 4th turn blocked by turn limit (no infinite debate loop)
        with pytest.raises(MeetingTurnLimitExceededError):
            mtg_svc.submit_turn(
                MeetingTurnRequest(
                    meeting_id=mtg_id,
                    round_number=2,
                    speaker_id="qa.reviewer_b",
                    turn_kind="ACTION_ITEM",
                    action_items=["Extra unapproved loop"],
                    statement="Try extra turn beyond limit.",
                    timestamp=t0 + timedelta(seconds=6),
                )
            )

        decision = mtg_svc.record_master_decision(
            MasterDecisionRequest(
                meeting_id=mtg_id,
                task_id=ctx["task"].task_id,
                run_id=ctx["out_a"].run_id,
                chosen_option="Apply UTF-8 LF fix and re-run independent QA",
                decision_summary="Producer must patch file and pass QA before handoff.",
                user_visible_rationale="QA evidence ev_qa_fail_digest confirmed mismatch; re-verification required.",
                evidence_references=["ev_qa_fail_digest", "ev_patch_plan_v2"],
                action_items=["Patch step_01 artifact", "Re-verify with QA"],
                close_meeting=True,
                timestamp=t0 + timedelta(seconds=7),
            )
        )
        assert decision.can_substitute_qa is False
        assert len(decision.acknowledged_dissent) == 1

    # Immutable DB trigger check + restart persistence
    with sqlite_connection(ctx["db_path"]) as conn:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute("UPDATE meeting_decisions SET decision_json = '{}';")
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute("DELETE FROM meeting_decisions;")

    reopened = _attach_phase06_services(_open_stack(ctx["db_path"], ctx["ws_root"]))
    with reopened["store"]:
        mtgs, decs = reopened["meeting_svc"].get_meeting_timeline(ctx["task"].task_id)
        assert len(mtgs) == 1 and mtgs[0].meeting.status == "CLOSED"
        assert len(mtgs[0].meeting.turns) == 3
        assert len(decs) == 1 and "ev_qa_fail_digest" in decs[0].user_visible_rationale


@pytest.mark.parametrize(
    "case_name",
    [
        "worker_cannot_convene",
        "worker_cannot_decide",
        "objection_without_evidence",
        "single_speaker_fake_consensus",
        "echo_chamber_duplicate",
        "unresolved_objection_consensus",
        "uninvited_speaker",
        "substitute_qa_forbidden",
    ],
)
def test_p06_fail_closed_anti_fake_consensus_and_governance(tmp_path: Path, case_name: str) -> None:
    """Block unprivileged meeting actions, empty objections, fake consensus, and QA substitution."""
    ctx = _setup_two_worker_workflow(tmp_path, execute_step1=True, step_count=2)
    t0 = BASE_TIME + timedelta(seconds=40)

    with ctx["store"]:
        mtg_svc: MeetingCoordinatorService = ctx["meeting_svc"]

        if case_name == "worker_cannot_convene":
            with pytest.raises(MeetingPermissionError):
                mtg_svc.open_meeting(
                    MeetingOpenRequest(
                        task_id=ctx["task"].task_id,
                        run_id=ctx["out_a"].run_id,
                        plan_id=ctx["plan"].plan_id,
                        team_proposal_id=ctx["team"].proposal_id,
                        agenda="Worker trying to chair meeting",
                        participant_agent_ids=["it.producer_a", "qa.reviewer_b"],
                        actor_id="it.producer_a",
                        actor_kind="worker_agent",
                        timestamp=t0,
                    )
                )
            return

        opened = mtg_svc.open_meeting(
            MeetingOpenRequest(
                task_id=ctx["task"].task_id,
                run_id=ctx["out_a"].run_id,
                plan_id=ctx["plan"].plan_id,
                team_proposal_id=ctx["team"].proposal_id,
                agenda="Validate structured debate guards",
                participant_agent_ids=["it.producer_a", "qa.reviewer_b"],
                max_turns=6,
                timestamp=t0,
            )
        )
        mtg_id = opened.meeting.meeting_id

        if case_name == "worker_cannot_decide":
            with pytest.raises(MeetingPermissionError):
                mtg_svc.record_master_decision(
                    MasterDecisionRequest(
                        meeting_id=mtg_id,
                        task_id=ctx["task"].task_id,
                        actor_id="it.producer_a",
                        actor_kind="worker_agent",
                        chosen_option="Approve self",
                        decision_summary="Worker usurping Master.",
                        user_visible_rationale="Workers may never issue Master DecisionRecords.",
                        evidence_references=["ev_1"],
                        timestamp=t0 + timedelta(seconds=1),
                    )
                )
        elif case_name == "objection_without_evidence":
            with pytest.raises(MeetingEvidenceError):
                mtg_svc.submit_turn(
                    MeetingTurnRequest(
                        meeting_id=mtg_id,
                        round_number=1,
                        speaker_id="qa.reviewer_b",
                        turn_kind="OBJECTION",
                        statement="I disagree without proof.",
                        evidence_references=[],
                        timestamp=t0 + timedelta(seconds=1),
                    )
                )
        elif case_name == "single_speaker_fake_consensus":
            with pytest.raises(FakeConsensusRejectedError, match="fewer than two"):
                mtg_svc.submit_turn(
                    MeetingTurnRequest(
                        meeting_id=mtg_id,
                        round_number=1,
                        speaker_id="it.producer_a",
                        turn_kind="PROPOSAL",
                        statement="Single speaker claims everyone agrees.",
                        consensus_claimed=True,
                        timestamp=t0 + timedelta(seconds=1),
                    )
                )
        elif case_name == "echo_chamber_duplicate":
            mtg_svc.submit_turn(
                MeetingTurnRequest(
                    meeting_id=mtg_id,
                    round_number=1,
                    speaker_id="it.producer_a",
                    turn_kind="PROPOSAL",
                    statement="Use deterministic SHA-256 outputs.",
                    timestamp=t0 + timedelta(seconds=1),
                )
            )
            with pytest.raises(FakeConsensusRejectedError, match="Duplicate"):
                mtg_svc.submit_turn(
                    MeetingTurnRequest(
                        meeting_id=mtg_id,
                        round_number=1,
                        speaker_id="qa.reviewer_b",
                        turn_kind="PROPOSAL",
                        statement="Use deterministic   sha-256 outputs.",
                        consensus_claimed=True,
                        timestamp=t0 + timedelta(seconds=2),
                    )
                )
        elif case_name == "unresolved_objection_consensus":
            mtg_svc.submit_turn(
                MeetingTurnRequest(
                    meeting_id=mtg_id,
                    round_number=1,
                    speaker_id="qa.reviewer_b",
                    turn_kind="OBJECTION",
                    statement="Missing SHA checksum check.",
                    evidence_references=["ev_missing_sha"],
                    timestamp=t0 + timedelta(seconds=1),
                )
            )
            with pytest.raises(FakeConsensusRejectedError, match="OBJECTION"):
                mtg_svc.submit_turn(
                    MeetingTurnRequest(
                        meeting_id=mtg_id,
                        round_number=1,
                        speaker_id="it.producer_a",
                        turn_kind="PROPOSAL",
                        statement="Ignore objection and claim consensus.",
                        consensus_claimed=True,
                        timestamp=t0 + timedelta(seconds=2),
                    )
                )
        elif case_name == "uninvited_speaker":
            with pytest.raises(MeetingPermissionError, match="not an invited"):
                mtg_svc.submit_turn(
                    MeetingTurnRequest(
                        meeting_id=mtg_id,
                        round_number=1,
                        speaker_id="mkt.bystander",
                        turn_kind="PROPOSAL",
                        statement="Uninvited marketing interjection.",
                        timestamp=t0 + timedelta(seconds=1),
                    )
                )
        elif case_name == "substitute_qa_forbidden":
            with pytest.raises(DecisionSubstituteQaForbiddenError):
                mtg_svc.record_master_decision(
                    MasterDecisionRequest(
                        meeting_id=mtg_id,
                        task_id=ctx["task"].task_id,
                        chosen_option="Skip QA",
                        decision_summary="Try overriding QA.",
                        user_visible_rationale="Attempting illegal QA substitution.",
                        evidence_references=["ev_x"],
                        substitute_qa=True,
                        timestamp=t0 + timedelta(seconds=1),
                    )
                )
