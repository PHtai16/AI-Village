"""P06.05-P06.06 Conflict Arbitration, Master Delivery Verification, and Gate D Acceptance Suite."""

from __future__ import annotations

from datetime import timedelta
import hashlib
from pathlib import Path

import pytest

from core.contracts import Artifact, new_artifact_id, new_evidence_id, new_session_id
from core.orchestration import (
    HandoffAcknowledgeRequest,
    HandoffCreateRequest,
    HandoffDeliveryRequest,
    MasterDecisionRequest,
    MasterDeliveryCompileRequest,
    MeetingComplexitySignal,
    MeetingOpenRequest,
    MeetingPermissionError,
    MeetingStateError,
    MeetingTurnRequest,
    WorkerConflictCheckRequest,
    WorkerConflictResolveRequest,
    WorkerExecutionRequest,
)
from engines.contract import EngineRequest, EngineResult, EngineSessionReference, EngineTelemetry
from engines.verification import verify_engine_artifact
from core.tasks.state_machine import complete_run_only, qa_evidence_from_engine_verification
from core.tasks.transitions import ExecutionEvidence, TransitionContext
from storage.repositories import PersistentWorkflowService, recover_run, recover_task
from tests.orchestration.test_p05_05_06_integration_acceptance import (
    AcceptanceFakeAdapter,
    BASE_TIME,
    _master_auth,
    _open_stack,
    _operator_auth,
)
from tests.orchestration.test_p06_01_04_meetings import (
    _attach_phase06_services,
    _setup_two_worker_workflow,
)


def test_p06_05_worker_conflict_detection_resolution_and_handoff_history(tmp_path: Path) -> None:
    """Detect overlapping write/step conflict, require Master decision or ACK handoff to resolve."""
    ctx = _setup_two_worker_workflow(tmp_path, execute_step1=True, step_count=2)
    t0 = BASE_TIME + timedelta(seconds=40)
    shared_file = f"workspace/{ctx['task'].task_id}/shared_spec.txt"

    with ctx["store"]:
        deliv_svc = ctx["delivery_svc"]
        mtg_svc = ctx["meeting_svc"]

        # Disjoint paths and steps -> no conflict
        none_cfl = deliv_svc.detect_file_or_task_conflict(
            WorkerConflictCheckRequest(
                task_id=ctx["task"].task_id,
                run_id=ctx["out_a"].run_id,
                step_a_id="step_01",
                agent_a_id="it.producer_a",
                session_a_id=ctx["out_a"].session_id,
                paths_a=[f"workspace/{ctx['task'].task_id}/a.txt"],
                step_b_id="step_02",
                agent_b_id="qa.reviewer_b",
                session_b_id=new_session_id(),
                paths_b=[f"workspace/{ctx['task'].task_id}/b.txt"],
                timestamp=t0,
            )
        )
        assert none_cfl is None

        # Overlapping write target -> OPEN FILE_WRITE_CONTENTION
        conflict = deliv_svc.detect_file_or_task_conflict(
            WorkerConflictCheckRequest(
                task_id=ctx["task"].task_id,
                run_id=ctx["out_a"].run_id,
                step_a_id="step_01",
                agent_a_id="it.producer_a",
                session_a_id=ctx["out_a"].session_id,
                paths_a=[shared_file],
                step_b_id="step_02",
                agent_b_id="qa.reviewer_b",
                session_b_id=new_session_id(),
                paths_b=[shared_file],
                timestamp=t0 + timedelta(seconds=1),
            )
        )
        assert conflict is not None
        assert conflict.conflict.status == "OPEN"
        assert conflict.conflict.conflict_type == "FILE_WRITE_CONTENTION"

        # Worker cannot arbitrate conflict; Master cannot resolve without proof
        with pytest.raises(MeetingPermissionError):
            deliv_svc.resolve_worker_conflict(
                WorkerConflictResolveRequest(
                    conflict_id=conflict.conflict.conflict_id,
                    actor_id="it.producer_a",
                    actor_kind="worker_agent",
                    resolution_summary="Self-assign lock.",
                    timestamp=t0 + timedelta(seconds=2),
                )
            )
        with pytest.raises(MeetingStateError) as proof_err:
            deliv_svc.resolve_worker_conflict(
                WorkerConflictResolveRequest(
                    conflict_id=conflict.conflict.conflict_id,
                    resolution_summary="Missing decision or handoff ref.",
                    timestamp=t0 + timedelta(seconds=2),
                )
            )
        assert proof_err.value.error_code == "RESOLUTION_PROOF_REQUIRED"

        dec = mtg_svc.record_master_decision(
            MasterDecisionRequest(
                conflict_id=conflict.conflict.conflict_id,
                task_id=ctx["task"].task_id,
                run_id=ctx["out_a"].run_id,
                chosen_option="Serialize shared_spec.txt write to step_01 only.",
                decision_summary="Prevent concurrent write overwrite.",
                user_visible_rationale="Grant write ownership of shared_spec.txt to step_01 before QA inspection.",
                evidence_references=["ev_conflict_log"],
                timestamp=t0 + timedelta(seconds=3),
            )
        )
        resolved = deliv_svc.resolve_worker_conflict(
            WorkerConflictResolveRequest(
                conflict_id=conflict.conflict.conflict_id,
                decision_id=dec.decision_id,
                resolution_summary="Resolved via Master DecisionRecord.",
                timestamp=t0 + timedelta(seconds=4),
            )
        )
        assert resolved.conflict.status == "RESOLVED_DECIDED"
        history = deliv_svc.build_handoff_collaboration_history(
            task_id=ctx["task"].task_id,
            run_id=ctx["out_a"].run_id,
        )
        assert len(history.conflicts) == 1 and history.conflicts[0].status == "RESOLVED_DECIDED"


def test_p06_06_master_delivery_enforces_not_verified_without_qa_and_blocks_bypass(tmp_path: Path) -> None:
    """Force NOT_VERIFIED whenever artifact is missing, unverified, or tampered; deny QA bypass."""
    ctx = _setup_two_worker_workflow(tmp_path, execute_step1=True, step_count=1)
    t0 = BASE_TIME + timedelta(seconds=40)
    store = ctx["store"]

    with store:
        deliv_svc = ctx["delivery_svc"]
        rel_path = f"workspace/{ctx['task'].task_id}/it.producer_a_step_01.txt"
        content = (ctx["ws_root"] / rel_path).read_bytes()
        unverified_art = store.metadata.save_artifact(
            Artifact(
                artifact_id=new_artifact_id(),
                task_id=ctx["task"].task_id,
                run_id=ctx["out_a"].run_id,
                owner_agent_id="it.producer_a",
                relative_path=rel_path,
                sha256=hashlib.sha256(content).hexdigest(),
                verification_status="UNVERIFIED",
                created_at=t0,
            )
        )

        # Unverified artifact -> compiles as NOT_VERIFIED with explicit blocker
        unverified_delivery = deliv_svc.compile_master_delivery(
            MasterDeliveryCompileRequest(
                task_id=ctx["task"].task_id,
                run_id=ctx["out_a"].run_id,
                workspace_root=str(ctx["ws_root"]),
                artifact_ids=[unverified_art.artifact_id],
                known_limitations=["QA verification still pending."],
                summary="Draft delivery compiled prior to QA verification.",
                timestamp=t0 + timedelta(seconds=1),
            )
        )
        assert unverified_delivery.delivery_verification_status == "NOT_VERIFIED"
        assert unverified_delivery.qa_verified is False
        assert any("UNVERIFIED" in b for b in unverified_delivery.verification_blockers)

        # Attempting override_qa_unverified=True blocked by Governance inv.master_delivery_no_qa_bypass
        with pytest.raises(MeetingPermissionError, match="QA"):
            deliv_svc.compile_master_delivery(
                MasterDeliveryCompileRequest(
                    task_id=ctx["task"].task_id,
                    run_id=ctx["out_a"].run_id,
                    workspace_root=str(ctx["ws_root"]),
                    artifact_ids=[unverified_art.artifact_id],
                    override_qa_unverified=True,
                    summary="Illegal override.",
                    timestamp=t0 + timedelta(seconds=2),
                )
            )


def test_gate_d_end_to_end_two_agents_meeting_handoff_qa_fix_and_delivery(tmp_path: Path) -> None:
    """Gate D E2E: 2-Agent real meeting, QA catches defect, defect fixed + verified, handoff, Master delivery, restart."""
    adapter_holder: list[AcceptanceFakeAdapter] = []

    def custom_exec(req: EngineRequest, conn, on_event):
        agent_id = str(req.metadata.get("agent_id", ""))
        step_id = str(req.metadata.get("step_id", ""))
        task_id = str(req.metadata.get("task_id", ""))
        ws = adapter_holder[0]._ws_root
        out = ws / "workspace" / task_id / f"{agent_id}_{step_id}.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        # First pass of step_01 intentionally writes defective content caught by QA
        if step_id == "step_01":
            out.write_text("DEFECTIVE_DRAFT_MISSING_HEADER\n", encoding="utf-8")
        else:
            out.write_text("VERIFIED_FINAL_REPORT_OK\n", encoding="utf-8")
        now = BASE_TIME + timedelta(seconds=15)
        return EngineResult(
            request_id=req.request_id,
            provider_id="antigravity",
            adapter_id="antigravity_cli",
            connection_id=req.connection_id,
            model_id=req.model_id,
            platform_kind="agent_execution_platform",
            status="SUCCEEDED",
            business_verification_status="UNVERIFIED",
            response_text=f"Executed {step_id}",
            session_reference=EngineSessionReference(
                session_id=req.session_reference.session_id if req.session_reference else None,
                provider_conversation_id=f"gate_d_{agent_id}_{step_id}_{req.request_id[-6:]}",
                binding_source="adapter_logical",
            ),
            started_at=now,
            finished_at=now + timedelta(seconds=1),
            duration_ms=1000,
            telemetry=EngineTelemetry(
                duration_ms=1000,
                telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                telemetry_source="unavailable",
            ),
        )

    fake = AcceptanceFakeAdapter(tmp_path / "ws")
    fake.custom_handler = custom_exec
    adapter_holder.append(fake)

    ctx = _setup_two_worker_workflow(tmp_path, execute_step1=True, step_count=2, adapter=fake)
    t0 = BASE_TIME + timedelta(seconds=30)
    store = ctx["store"]
    task_id = ctx["task"].task_id
    run_id = ctx["out_a"].run_id
    step1_rel = f"workspace/{task_id}/it.producer_a_step_01.txt"

    with store:
        # 1. Independent QA inspects step_01 initial output -> VERIFIED_FAIL
        fail_qa = verify_engine_artifact(
            workspace_dir=ctx["ws_root"],
            artifact_relpath=step1_rel,
            engine_result=ctx["out_a"].engine_result,
            required_substrings=["APPROVED_SPEC_HEADER"],
        )
        assert fail_qa.status == "VERIFIED_FAIL"

        # 2. Complexity triage -> opens real 2-Agent + Master meeting
        mtg = ctx["meeting_svc"].open_meeting(
            MeetingOpenRequest(
                task_id=task_id,
                run_id=run_id,
                plan_id=ctx["plan"].plan_id,
                team_proposal_id=ctx["team"].proposal_id,
                agenda="Fix QA defect on step_01 before handoff to step_02",
                participant_agent_ids=["it.producer_a", "qa.reviewer_b"],
                timestamp=t0,
            )
        )
        t1 = ctx["meeting_svc"].submit_turn(
            MeetingTurnRequest(
                meeting_id=mtg.meeting.meeting_id,
                round_number=1,
                speaker_id="it.producer_a",
                turn_kind="PROPOSAL",
                statement="Delivered step_01 file for QA check.",
                evidence_references=["ev_initial_draft"],
                timestamp=t0 + timedelta(seconds=1),
            )
        )
        t2 = ctx["meeting_svc"].submit_turn(
            MeetingTurnRequest(
                meeting_id=mtg.meeting.meeting_id,
                round_number=1,
                speaker_id="qa.reviewer_b",
                turn_kind="OBJECTION",
                target_turn_id=t1.meeting.turns[0].turn_id,
                statement="Missing APPROVED_SPEC_HEADER required by acceptance criteria.",
                evidence_references=["ev_qa_fail_missing_header"],
                timestamp=t0 + timedelta(seconds=2),
            )
        )
        ctx["meeting_svc"].submit_turn(
            MeetingTurnRequest(
                meeting_id=mtg.meeting.meeting_id,
                round_number=2,
                speaker_id="it.producer_a",
                turn_kind="EVIDENCE",
                target_turn_id=t2.meeting.turns[1].turn_id,
                statement="Agreed to add APPROVED_SPEC_HEADER and re-verify before handoff.",
                evidence_references=["ev_fix_committed"],
                consensus_claimed=True,
                timestamp=t0 + timedelta(seconds=3),
            )
        )
        decision = ctx["meeting_svc"].record_master_decision(
            MasterDecisionRequest(
                meeting_id=mtg.meeting.meeting_id,
                task_id=task_id,
                run_id=run_id,
                chosen_option="Fix step_01 artifact header and re-run QA verification",
                decision_summary="Block handoff until independent QA passes step_01.",
                user_visible_rationale="QA caught missing header; defect must be repaired and re-hashed prior to delivery.",
                evidence_references=["ev_qa_fail_missing_header", "ev_fix_committed"],
                action_items=["Write APPROVED_SPEC_HEADER", "Verify SHA-256"],
                timestamp=t0 + timedelta(seconds=4),
            )
        )

        # 3. Producer fixes defect on disk -> QA re-verifies -> VERIFIED_PASS
        fixed_bytes = b"APPROVED_SPEC_HEADER\nVERIFIED_PRODUCER_CONTENT\n"
        (ctx["ws_root"] / step1_rel).write_bytes(fixed_bytes)
        pass_qa = verify_engine_artifact(
            workspace_dir=ctx["ws_root"],
            artifact_relpath=step1_rel,
            engine_result=ctx["out_a"].engine_result,
            required_substrings=["APPROVED_SPEC_HEADER"],
        )
        assert pass_qa.status == "VERIFIED_PASS"
        art_1 = store.metadata.save_artifact(
            Artifact(
                artifact_id=new_artifact_id(),
                task_id=task_id,
                run_id=run_id,
                owner_agent_id="it.producer_a",
                relative_path=step1_rel,
                sha256=pass_qa.actual_sha256,
                verification_status="VERIFIED_PASS",
                evidence_reference=new_evidence_id(),
                created_at=t0 + timedelta(seconds=5),
            )
        )

        # 4. Identified Handoff A -> B (READY -> DELIVERED -> ACKNOWLEDGED)
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
                artifact_ids=[art_1.artifact_id],
                decision_references=[decision.decision_id],
                input_context_summary="Step 1 defect fixed and verified by QA.",
                expected_next_action="Run step_02 downstream QA deliverable.",
                timestamp=t0 + timedelta(seconds=6),
            )
        )
        assert hoff.handoff.handoff_id.startswith("hoff_")
        ctx["handoff_svc"].deliver_handoff(
            HandoffDeliveryRequest(
                handoff_id=hoff.handoff.handoff_id,
                actor_id="core.master",
                workspace_root=str(ctx["ws_root"]),
                timestamp=t0 + timedelta(seconds=7),
            )
        )
        ctx["handoff_svc"].acknowledge_handoff(
            HandoffAcknowledgeRequest(
                handoff_id=hoff.handoff.handoff_id,
                actor_id="qa.reviewer_b",
                actor_kind="worker_agent",
                timestamp=t0 + timedelta(seconds=8),
            )
        )

        # 5. Worker B executes step_02 strictly after ACKNOWLEDGED handoff + QA verifies final artifact
        out_b = ctx["session_svc"].execute_worker_step(
            WorkerExecutionRequest(
                task_id=task_id,
                plan_id=ctx["plan"].plan_id,
                team_proposal_id=ctx["team"].proposal_id,
                step_id="step_02",
                agent_id="qa.reviewer_b",
                actor_id="core.master",
                actor_kind="orchestrator",
                workspace_root=str(ctx["ws_root"]),
                workspace_scope=f"workspace/{task_id}",
                require_acknowledged_handoffs=True,
                timestamp=t0 + timedelta(seconds=9),
            )
        )
        assert out_b.status == "CLOSED" and out_b.engine_result is not None
        assert out_b.engine_result.status == "SUCCEEDED"
        step2_rel = f"workspace/{task_id}/qa.reviewer_b_step_02.txt"
        qa2 = verify_engine_artifact(
            workspace_dir=ctx["ws_root"],
            artifact_relpath=step2_rel,
            engine_result=out_b.engine_result,
            required_substrings=["VERIFIED_FINAL_REPORT_OK"],
        )
        assert qa2.status == "VERIFIED_PASS"
        art_2 = store.metadata.save_artifact(
            Artifact(
                artifact_id=new_artifact_id(),
                task_id=task_id,
                run_id=run_id,
                owner_agent_id="qa.reviewer_b",
                relative_path=step2_rel,
                sha256=qa2.actual_sha256,
                verification_status="VERIFIED_PASS",
                evidence_reference=new_evidence_id(),
                created_at=t0 + timedelta(seconds=10),
            )
        )

        # 6. Master compiles final verified delivery summary
        delivery = ctx["delivery_svc"].compile_master_delivery(
            MasterDeliveryCompileRequest(
                task_id=task_id,
                run_id=run_id,
                workspace_root=str(ctx["ws_root"]),
                artifact_ids=[art_1.artifact_id, art_2.artifact_id],
                known_limitations=["Executed offline via AcceptanceFakeAdapter (AI_VILLAGE_RUN_LIVE unset)."],
                summary="Both artifacts verified on disk with QA evidence after meeting triage.",
                timestamp=t0 + timedelta(seconds=11),
            )
        )
        assert delivery.delivery_verification_status == "VERIFIED"
        assert delivery.qa_verified is True
        assert delivery.meeting_ids == [mtg.meeting.meeting_id]
        assert decision.decision_id in delivery.decision_ids
        assert hoff.handoff.handoff_id in delivery.handoff_ids
        assert all(a.download_ref.startswith(f"artifact://{task_id}/") for a in delivery.artifacts)

        # Complete Run and mark checklist + Task COMPLETED strictly via independent QA proof
        wf_svc = PersistentWorkflowService(store)
        ev_id = art_2.evidence_reference
        assert ev_id is not None
        for chk in store.metadata.list_checklist_items(task_id):
            store.metadata.save_checklist_item(
                chk.model_copy(
                    update={
                        "status": "COMPLETED",
                        "evidence_reference": ev_id,
                        "completed_at": t0 + timedelta(seconds=12),
                    }
                )
            )
        stored_task = store.tasks.get_stored_task(task_id)
        stored_run = store.runs.get_stored_run(run_id)
        assert stored_task is not None and stored_run is not None
        pair_res = complete_run_only(
            stored_task.task,
            stored_run.run,
            TransitionContext(
                actor_id="core.master",
                actor_kind="core_runtime",
                timestamp=t0 + timedelta(seconds=12),
                reason="Both workers finished steps after QA fix and verified handoff.",
                execution_evidence=ExecutionEvidence(
                    source="unit_test_fixture",
                    run_id=run_id,
                    reason="Step 1 and Step 2 sessions closed CLEAN.",
                ),
            ),
        )
        assert pair_res.accepted and pair_res.task and pair_res.run and pair_res.events
        store.runs.save_run_state(pair_res.run, expected_version=stored_run.version)
        store.tasks.save_task_state(pair_res.task, expected_version=stored_task.version)
        for p_ev in pair_res.events:
            store.events.append(p_ev)

        qa_ev = qa_evidence_from_engine_verification(
            qa2,
            evidence_id=ev_id,
            verifier_actor_id="qa.independent_auditor",
            verified_at=t0 + timedelta(seconds=13),
            artifact_ids=[art_1.artifact_id, art_2.artifact_id],
        )
        complete_res = wf_svc.apply_task_transition(
            task_id,
            "COMPLETED",
            TransitionContext(
                actor_id="qa.independent_auditor",
                actor_kind="qa_verifier",
                timestamp=t0 + timedelta(seconds=13),
                reason="Independent QA verified all final deliverables after fix and meeting.",
                qa_evidence=qa_ev,
                checklist_items=store.metadata.list_checklist_items(task_id),
                artifacts=store.metadata.list_artifacts(task_id=task_id),
            ),
        )
        assert complete_res.accepted and complete_res.task is not None and complete_res.task.status == "COMPLETED"

    # 7. Restart persistence & replay verification across reopened SQLiteRepositoryStore
    reopened = _attach_phase06_services(_open_stack(ctx["db_path"], ctx["ws_root"]))
    with reopened["store"] as s2:
        t_rec = recover_task(s2, task_id)
        r_rec = recover_run(s2, run_id)
        assert t_rec.recovery_status == "VERIFIED_CLEAN" and t_rec.snapshot_matches_replay is True
        assert r_rec.recovery_status == "VERIFIED_CLEAN" and r_rec.snapshot_matches_replay is True
        deliveries = s2.collaboration.list_deliveries(task_id=task_id)
        assert len(deliveries) == 1 and deliveries[0].delivery_verification_status == "VERIFIED"
        collab_hist = reopened["delivery_svc"].build_handoff_collaboration_history(task_id=task_id, run_id=run_id)
        assert len(collab_hist.handoffs) == 1 and len(collab_hist.handoffs[0].revisions) == 3
