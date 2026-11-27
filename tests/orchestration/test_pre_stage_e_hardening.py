"""Regression and security hardening tests verifying Pre-Stage E audit fixes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
from pathlib import Path
import sqlite3
import threading
from typing import Any

import pytest

from core.agents import (
    AgentRecruitmentService,
    RecruitmentActivationRequest,
    RecruitmentEvaluationRequest,
    RecruitmentGovernanceError,
    RecruitmentProposalRequest,
)
from core.contracts import (
    Artifact,
    Task,
    new_artifact_id,
    new_evidence_id,
    new_session_id,
    new_task_id,
)
from core.governance.models import resolve_trusted_auth_context
from core.orchestration import (
    HandoffAcknowledgeRequest,
    HandoffCreateRequest,
    HandoffDeliveryRequest,
    HandoffGovernanceDeniedError,
    MasterDeliveryCompileRequest,
    MeetingPermissionError,
    MeetingStateError,
    PlanStepSpec,
    StructuredUserBrief,
    TeamBuildRequest,
    WorkerConflictCheckRequest,
    WorkerConflictResolveRequest,
    WorkerExecutionRequest,
    WorkerPreflightBlockedError,
)
from engines.contract import EngineRequest, EngineResult, EngineSessionReference, EngineTelemetry
from storage.db import sqlite_connection
from storage.repositories import recover_task
from tests.orchestration.test_p05_05_06_integration_acceptance import (
    AcceptanceFakeAdapter,
    BASE_TIME,
    _bootstrap_db,
    _master_auth,
    _open_stack,
    _operator_auth,
    _worker_manifest,
)
from tests.orchestration.test_p06_01_04_meetings import (
    _attach_phase06_services,
    _setup_two_worker_workflow,
)


def test_operator_spoofing_without_auth_context_denied_fail_closed(tmp_path: Path) -> None:
    """Reject raw actor_kind='operator' across services when verified auth_context is absent."""
    ctx = _setup_two_worker_workflow(tmp_path, execute_step1=True, step_count=2)
    t0 = BASE_TIME + timedelta(seconds=40)
    store = ctx["store"]

    # Unknown or agent-claimed trust labels resolve unauthenticated fail-closed.
    claimed = resolve_trusted_auth_context(
        auth_context=None,
        actor_id="rogue.agent",
        actor_kind="llm_self_report",
    )
    assert claimed.authenticated is False and claimed.auth_source == "agent_claimed"

    with store:
        recruitment = AgentRecruitmentService(
            recruitment_repo=store.recruitment,
            agent_registry=ctx["registry"],
            permission_manager=ctx["pm"],
        )
        candidate = _worker_manifest(
            agent_id="it.spoof_target",
            department="it",
            skills=["code_generation"],
        )
        prop = recruitment.propose_candidate(
            RecruitmentProposalRequest(
                candidate_manifest=candidate,
                rationale="Test fail-closed operator spoof protection.",
                needed_capabilities=["code_generation"],
                proposer_id="core.master",
                proposer_kind="orchestrator",
                timestamp=t0,
            )
        )
        recruitment.evaluate_candidate(
            RecruitmentEvaluationRequest(
                proposal_id=prop.proposal.proposal_id,
                evaluator_id="qa.verifier",
                evaluator_kind="qa_verifier",
                summary="Checks passed.",
                workspace_root=str(ctx["ws_root"]),
                timestamp=t0 + timedelta(seconds=1),
            )
        )
        with pytest.raises(RecruitmentGovernanceError, match="missing or unauthenticated"):
            recruitment.activate_candidate(
                RecruitmentActivationRequest(
                    proposal_id=prop.proposal.proposal_id,
                    actor_id="operator.spoofed",
                    actor_kind="operator",
                    timestamp=t0 + timedelta(seconds=2),
                )
            )

        with pytest.raises(MeetingPermissionError, match="missing or unauthenticated"):
            ctx["delivery_svc"].compile_master_delivery(
                MasterDeliveryCompileRequest(
                    task_id=ctx["task"].task_id,
                    run_id=ctx["out_a"].run_id,
                    actor_id="operator.spoofed",
                    actor_kind="operator",
                    workspace_root=str(ctx["ws_root"]),
                    artifact_ids=[],
                    summary="Attempt delivery without auth_context.",
                    timestamp=t0 + timedelta(seconds=3),
                )
            )

        with pytest.raises(HandoffGovernanceDeniedError, match="missing or unauthenticated"):
            ctx["handoff_svc"].create_and_validate_handoff(
                HandoffCreateRequest(
                    task_id=ctx["task"].task_id,
                    run_id=ctx["out_a"].run_id,
                    plan_id=ctx["plan"].plan_id,
                    team_proposal_id=ctx["team"].proposal_id,
                    source_step_id="step_01",
                    target_step_id="step_02",
                    from_agent_id="it.producer_a",
                    to_agent_id="qa.reviewer_b",
                    from_session_id=ctx["out_a"].session_id,
                    actor_id="operator.spoofed",
                    actor_kind="operator",
                    workspace_root=str(ctx["ws_root"]),
                    target_workspace_scope=f"workspace/{ctx['task'].task_id}",
                    require_verified_artifacts=False,
                    input_context_summary="Spoofed handoff test.",
                    expected_next_action="Inspect.",
                    timestamp=t0 + timedelta(seconds=4),
                )
            )

        with pytest.raises(WorkerPreflightBlockedError, match="missing or unauthenticated"):
            ctx["session_svc"].terminate_session(
                ctx["out_a"].session_id,
                actor_id="operator.spoofed",
                actor_kind="operator",
                reason="Spoofed cancel.",
                timestamp=t0 + timedelta(seconds=5),
            )

        deny_events = [
            e
            for e in store.events.read_entries()
            if e.event.event_type == "governance.decision_recorded"
            and e.event.redacted_payload.get("reason_code") == "MISSING_AUTH_CONTEXT"
        ]
        assert len(deny_events) >= 4


def test_parallel_worker_steps_fresh_task_and_shared_run_accumulate_telemetry(tmp_path: Path) -> None:
    """Concurrent workers on same Task/Run serialize Run metadata cleanly and sum telemetry."""
    db_path, ws_root = _bootstrap_db(tmp_path)
    barrier = threading.Barrier(2, timeout=5.0)
    fake = AcceptanceFakeAdapter(ws_root)

    def _parallel_handler(req: EngineRequest, conn: Any, on_event: Any) -> EngineResult:
        barrier.wait()
        step_id = str(req.metadata.get("step_id", ""))
        now = BASE_TIME + timedelta(seconds=20)
        tokens_in = 100 if step_id == "step_01" else 140
        tokens_out = 50 if step_id == "step_01" else 60
        dur = 120 if step_id == "step_01" else 180
        return EngineResult(
            request_id=req.request_id,
            provider_id="antigravity",
            adapter_id="antigravity_cli",
            connection_id=req.connection_id,
            model_id=req.model_id,
            platform_kind="agent_execution_platform",
            status="SUCCEEDED",
            business_verification_status="UNVERIFIED",
            response_text=f"Completed {step_id}",
            session_reference=EngineSessionReference(
                session_id=req.session_reference.session_id if req.session_reference else None,
                provider_conversation_id=f"par_{step_id}_{req.request_id[-6:]}",
                binding_source="adapter_logical",
            ),
            started_at=now,
            finished_at=now + timedelta(seconds=1),
            duration_ms=dur,
            telemetry=EngineTelemetry(
                duration_ms=dur,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                total_tokens=tokens_in + tokens_out,
                telemetry_status="VERIFIED_MEASURED",
                telemetry_source="provider_cli",
            ),
        )

    fake.custom_handler = _parallel_handler
    stack = _attach_phase06_services(_open_stack(db_path, ws_root, adapter=fake))
    store = stack["store"]
    op_auth = _operator_auth()
    m_auth = _master_auth()

    stack["registry"].register_agent(
        _worker_manifest(agent_id="it.w1", department="it", skills=["code_generation"]),
        auth_context=op_auth,
        timestamp=BASE_TIME,
    )
    stack["registry"].register_agent(
        _worker_manifest(agent_id="qa.w2", department="qa", skills=["artifact_qa_inspection"]),
        auth_context=op_auth,
        timestamp=BASE_TIME,
    )
    task = store.tasks.create_task(
        Task(
            task_id=new_task_id(),
            title="Parallel Worker Steps",
            description="Two independent steps executing concurrently on one Task and Run.",
            requested_by="operator.local",
            status="QUEUED",
            created_at=BASE_TIME,
        )
    ).task
    steps = [
        PlanStepSpec(
            step_id="step_01",
            order=0,
            title="Parallel Step 1",
            description="Run worker 1.",
            required_capabilities=["code_generation"],
            expected_outputs=[f"workspace/{task.task_id}/o1.txt"],
            allowed_read_paths=[f"workspace/{task.task_id}"],
            allowed_write_paths=[f"workspace/{task.task_id}"],
            acceptance_criteria=["Done 1"],
        ),
        PlanStepSpec(
            step_id="step_02",
            order=1,
            title="Parallel Step 2",
            description="Run worker 2.",
            required_capabilities=["artifact_qa_inspection"],
            expected_outputs=[f"workspace/{task.task_id}/o2.txt"],
            allowed_read_paths=[f"workspace/{task.task_id}"],
            allowed_write_paths=[f"workspace/{task.task_id}"],
            acceptance_criteria=["Done 2"],
        ),
    ]
    brief = StructuredUserBrief(
        task_id=task.task_id,
        title=task.title,
        objective=task.description,
        requested_by="operator.local",
        required_outputs=[f"workspace/{task.task_id}/o1.txt"],
        acceptance_criteria=["Verified"],
        preferred_engine_id="antigravity",
        preferred_provider_id="antigravity",
        preferred_connection_id="ag_shared",
        preferred_model_id="gemini-2.5-flash",
    )
    plan_res = stack["planner"].create_plan(brief=brief, steps=steps, auth_context=m_auth, timestamp=BASE_TIME + timedelta(seconds=1))
    plan, _ = stack["planner"].apply_plan_to_task(
        plan_res.stored_plan.plan_id,
        expected_plan_record_version=plan_res.stored_plan.record_version,
        auth_context=m_auth,
        timestamp=BASE_TIME + timedelta(seconds=2),
    )
    tp = stack["team_builder"].build_team_proposal(
        TeamBuildRequest(task_id=task.task_id, plan_id=plan.plan_id),
        auth_context=m_auth,
        timestamp=BASE_TIME + timedelta(seconds=3),
    )
    assigned = stack["team_builder"].apply_team_assignment(
        tp.proposal_id,
        expected_proposal_record_version=tp.record_version,
        auth_context=m_auth,
        timestamp=BASE_TIME + timedelta(seconds=4),
    )

    def _run_step(step_id: str, agent_id: str) -> Any:
        return stack["session_svc"].execute_worker_step(
            WorkerExecutionRequest(
                task_id=task.task_id,
                plan_id=plan.plan_id,
                team_proposal_id=assigned.stored_proposal.proposal_id,
                step_id=step_id,
                agent_id=agent_id,
                actor_id="core.master",
                actor_kind="orchestrator",
                workspace_root=str(ws_root),
                workspace_scope=f"workspace/{task.task_id}",
                timestamp=BASE_TIME + timedelta(seconds=5),
            )
        )

    with store:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(_run_step, "step_01", "it.w1")
            f2 = pool.submit(_run_step, "step_02", "qa.w2")
            out1 = f1.result()
            out2 = f2.result()

        assert out1.status == "CLOSED" and out2.status == "CLOSED"
        assert out1.run_id == out2.run_id and out1.run_id is not None
        runs = store.runs.list_runs_for_task(task.task_id)
        assert len(runs) == 1
        shared_run = runs[0]
        assert set(shared_run.session_ids) == {out1.session_id, out2.session_id}
        assert shared_run.telemetry is not None
        assert shared_run.telemetry.engine_call_count == 2
        assert shared_run.telemetry.total_duration_ms == 300
        assert shared_run.telemetry.input_tokens == 240
        assert shared_run.telemetry.output_tokens == 110
        assert shared_run.telemetry.total_tokens == 350
        assert shared_run.telemetry.telemetry_status == "VERIFIED_MEASURED"
        assert shared_run.telemetry.telemetry_source == "provider_cli"


def test_worker_long_exception_transitions_cleanly_to_failed(tmp_path: Path) -> None:
    """Long exception strings (>500 chars) truncate cleanly instead of failing transition_session."""
    ctx = _setup_two_worker_workflow(tmp_path, execute_step1=False, step_count=1)
    ctx["adapter"].custom_handler = lambda req, conn, on_ev: (_ for _ in ()).throw(
        RuntimeError("ADAPTER_ERR:" + ("Z" * 680))
    )

    with ctx["store"]:
        outcome = ctx["session_svc"].execute_worker_step(
            WorkerExecutionRequest(
                task_id=ctx["task"].task_id,
                plan_id=ctx["plan"].plan_id,
                team_proposal_id=ctx["team"].proposal_id,
                step_id="step_01",
                agent_id="it.producer_a",
                actor_id="core.master",
                actor_kind="orchestrator",
                workspace_root=str(ctx["ws_root"]),
                workspace_scope=f"workspace/{ctx['task'].task_id}",
                timestamp=BASE_TIME + timedelta(seconds=20),
            )
        )
        assert outcome.status == "FAILED"
        assert outcome.error_code == "ENGINE_EXECUTION_ERROR"
        assert outcome.error_message is not None and len(outcome.error_message) <= 500
        assert outcome.session_id is not None
        stored = ctx["store"].worker_sessions.get_stored_session(outcome.session_id)
        assert stored is not None and stored.session.status == "FAILED"
        transitions = ctx["store"].worker_sessions.list_transitions(outcome.session_id)
        assert all(len(t.reason) <= 500 for t in transitions)


def test_recover_task_mid_task_after_handoff_with_multiple_artifacts(tmp_path: Path) -> None:
    """Mid-task restart recovery matches multi-artifact handoffs without false INVALID_HISTORY."""
    ctx = _setup_two_worker_workflow(tmp_path, execute_step1=True, step_count=2)
    t0 = BASE_TIME + timedelta(seconds=40)
    store = ctx["store"]
    task_id = ctx["task"].task_id
    run_id = ctx["out_a"].run_id

    art_records: list[Artifact] = []
    with store:
        for idx in (1, 2):
            rel = f"workspace/{task_id}/handoff_part_{idx}.txt"
            target = ctx["ws_root"] / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            data = f"handoff-body-{idx}\n".encode("utf-8")
            target.write_bytes(data)
            art_records.append(
                store.metadata.save_artifact(
                    Artifact(
                        artifact_id=new_artifact_id(),
                        task_id=task_id,
                        run_id=run_id,
                        owner_agent_id="it.producer_a",
                        relative_path=rel,
                        sha256=hashlib.sha256(data).hexdigest(),
                        size_bytes=len(data),
                        verification_status="VERIFIED_PASS",
                        evidence_reference=new_evidence_id(),
                        created_at=t0 + timedelta(seconds=idx),
                    )
                )
            )

        hoff = ctx["handoff_svc"].create_and_validate_handoff(
            HandoffCreateRequest(
                task_id=task_id,
                run_id=run_id,
                plan_id=ctx["plan"].plan_id,
                team_proposal_id=ctx["team"].proposal_id,
                source_step_id="step_01",
                target_step_id="step_02",
                from_agent_id="it.producer_a",
                to_agent_id="qa.reviewer_b",
                from_session_id=ctx["out_a"].session_id,
                actor_id="core.master",
                workspace_root=str(ctx["ws_root"]),
                target_workspace_scope=f"workspace/{task_id}",
                artifact_ids=[a.artifact_id for a in art_records],
                input_context_summary="Prepared two verified handoff artifacts.",
                expected_next_action="Inspect both artifacts in step_02.",
                timestamp=t0 + timedelta(seconds=3),
            )
        )
        ctx["handoff_svc"].deliver_handoff(
            HandoffDeliveryRequest(
                handoff_id=hoff.handoff.handoff_id,
                actor_id="core.master",
                workspace_root=str(ctx["ws_root"]),
                timestamp=t0 + timedelta(seconds=4),
            )
        )
        ctx["handoff_svc"].acknowledge_handoff(
            HandoffAcknowledgeRequest(
                handoff_id=hoff.handoff.handoff_id,
                actor_id="qa.reviewer_b",
                actor_kind="worker_agent",
                timestamp=t0 + timedelta(seconds=5),
            )
        )

        rec = recover_task(store, task_id)
        assert rec.recovery_status == "NEEDS_RECONCILIATION"
        assert rec.snapshot_matches_replay is True
        assert not rec.inconsistencies
        for art in art_records:
            assert art.artifact_id in rec.replay_result.artifact_ids

    # Removing an artifact row breaks recovery fail-closed as INVALID_HISTORY.
    with sqlite_connection(ctx["db_path"]) as raw_conn:
        raw_conn.execute("DELETE FROM artifacts WHERE artifact_id = ?;", (art_records[1].artifact_id,))
    with store.__class__(ctx["db_path"]) as reopened:
        broken_rec = recover_task(reopened, task_id)
        assert broken_rec.recovery_status == "INVALID_HISTORY"
        assert broken_rec.snapshot_matches_replay is False


def test_resolve_worker_conflict_rejects_mismatched_handoff_agents_or_run(tmp_path: Path) -> None:
    """Unrelated ACKNOWLEDGED handoff cannot resolve a conflict between different workers."""
    ctx = _setup_two_worker_workflow(tmp_path, execute_step1=True, step_count=2)
    t0 = BASE_TIME + timedelta(seconds=40)
    store = ctx["store"]
    task_id = ctx["task"].task_id
    run_id = ctx["out_a"].run_id

    with store:
        unrelated_hoff = ctx["handoff_svc"].create_and_validate_handoff(
            HandoffCreateRequest(
                task_id=task_id,
                run_id=run_id,
                plan_id=ctx["plan"].plan_id,
                team_proposal_id=ctx["team"].proposal_id,
                source_step_id="step_01",
                target_step_id="step_02",
                from_agent_id="it.producer_a",
                to_agent_id="qa.reviewer_b",
                from_session_id=ctx["out_a"].session_id,
                actor_id="core.master",
                workspace_root=str(ctx["ws_root"]),
                target_workspace_scope=f"workspace/{task_id}",
                require_verified_artifacts=False,
                input_context_summary="Unrelated handoff edge.",
                expected_next_action="Inspect.",
                timestamp=t0 + timedelta(seconds=1),
            )
        )
        ctx["handoff_svc"].deliver_handoff(
            HandoffDeliveryRequest(
                handoff_id=unrelated_hoff.handoff.handoff_id,
                actor_id="core.master",
                workspace_root=str(ctx["ws_root"]),
                require_verified_artifacts=False,
                timestamp=t0 + timedelta(seconds=2),
            )
        )
        ctx["handoff_svc"].acknowledge_handoff(
            HandoffAcknowledgeRequest(
                handoff_id=unrelated_hoff.handoff.handoff_id,
                actor_id="qa.reviewer_b",
                actor_kind="worker_agent",
                timestamp=t0 + timedelta(seconds=3),
            )
        )

        cfl = ctx["delivery_svc"].detect_file_or_task_conflict(
            WorkerConflictCheckRequest(
                task_id=task_id,
                run_id=run_id,
                step_a_id="step_01",
                agent_a_id="mkt.bystander",
                session_a_id=new_session_id(),
                paths_a=[f"workspace/{task_id}/shared.txt"],
                step_b_id="step_02",
                agent_b_id="it.producer_a",
                session_b_id=ctx["out_a"].session_id,
                paths_b=[f"workspace/{task_id}/shared.txt"],
                timestamp=t0 + timedelta(seconds=4),
            )
        )
        assert cfl is not None
        with pytest.raises(MeetingStateError, match="does not serialize conflict agents"):
            ctx["delivery_svc"].resolve_worker_conflict(
                WorkerConflictResolveRequest(
                    conflict_id=cfl.conflict.conflict_id,
                    handoff_id=unrelated_hoff.handoff.handoff_id,
                    resolution_summary="Attempt bypass with mismatched worker handoff.",
                    timestamp=t0 + timedelta(seconds=5),
                )
            )


def test_recruitment_master_cannot_activate_and_sqlite_pool_minimizes_connects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Master cannot bypass human_operator activation, and warmed store avoids extra sqlite3.connect."""
    ctx = _setup_two_worker_workflow(tmp_path, execute_step1=True, step_count=2)
    t0 = BASE_TIME + timedelta(seconds=40)
    store = ctx["store"]

    with store:
        recruitment = AgentRecruitmentService(
            recruitment_repo=store.recruitment,
            agent_registry=ctx["registry"],
            permission_manager=ctx["pm"],
        )
        cand = _worker_manifest(
            agent_id="it.master_blocked",
            department="it",
            skills=["code_generation"],
        )
        prop = recruitment.propose_candidate(
            RecruitmentProposalRequest(
                candidate_manifest=cand,
                rationale="Verify Master cannot self-activate candidates.",
                needed_capabilities=["code_generation"],
                proposer_id="core.master",
                proposer_kind="orchestrator",
                timestamp=t0,
            )
        )
        recruitment.evaluate_candidate(
            RecruitmentEvaluationRequest(
                proposal_id=prop.proposal.proposal_id,
                evaluator_id="qa.verifier",
                evaluator_kind="qa_verifier",
                summary="Checks passed.",
                workspace_root=str(ctx["ws_root"]),
                timestamp=t0 + timedelta(seconds=1),
            )
        )
        with pytest.raises(RecruitmentGovernanceError, match="human_operator"):
            recruitment.activate_candidate(
                RecruitmentActivationRequest(
                    proposal_id=prop.proposal.proposal_id,
                    actor_id="core.master",
                    actor_kind="orchestrator",
                    timestamp=t0 + timedelta(seconds=2),
                )
            )

        # Warm thread-local connection once before measuring connect churn
        _ = store.tasks.get_task(ctx["task"].task_id)
        orig_connect = sqlite3.connect
        connect_calls = 0

        def _counting_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            nonlocal connect_calls
            connect_calls += 1
            return orig_connect(*args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", _counting_connect)
        ctx["handoff_svc"].create_and_validate_handoff(
            HandoffCreateRequest(
                task_id=ctx["task"].task_id,
                run_id=ctx["out_a"].run_id,
                plan_id=ctx["plan"].plan_id,
                team_proposal_id=ctx["team"].proposal_id,
                source_step_id="step_01",
                target_step_id="step_02",
                from_agent_id="it.producer_a",
                to_agent_id="qa.reviewer_b",
                from_session_id=ctx["out_a"].session_id,
                actor_id="core.master",
                workspace_root=str(ctx["ws_root"]),
                target_workspace_scope=f"workspace/{ctx['task'].task_id}",
                require_verified_artifacts=False,
                input_context_summary="Verify pooled SQLite reuse.",
                expected_next_action="Inspect.",
                timestamp=t0 + timedelta(seconds=3),
            )
        )
        assert connect_calls == 0

    assert len(store._pooled_conns) == 0
    ctx["db_path"].unlink()
    assert not ctx["db_path"].exists()
