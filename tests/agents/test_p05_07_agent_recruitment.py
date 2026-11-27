"""Unit, Governance, SQLite persistence, and fail-closed tests for Phase 05 / P05.07 Agent Recruitment."""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path
import sqlite3

import pytest

from core.agents import (
    AgentRecruitmentService,
    AgentRegistryService,
    RecruitmentActivationRequest,
    RecruitmentEvaluationRequest,
    RecruitmentGovernanceError,
    RecruitmentProposalRequest,
    RecruitmentRejectRequest,
    RecruitmentStateError,
    RecruitmentValidationError,
    StaleRecruitmentVersionError,
)
from core.events.journal import AppendResult
from core.governance import ApprovalPolicyContext, ApprovalService, PermissionManager, PolicyEvaluator, compute_request_fingerprint
from storage.db import get_latest_migration_version, sqlite_connection
from storage.repositories import SQLiteRepositoryStore
from tests.agents.test_p05_02_agent_registry import (
    _build_registry,
    _init_db,
    _operator_ctx,
    _utc,
    _worker_manifest_dict,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _build_recruitment(
    store: SQLiteRepositoryStore,
) -> tuple[AgentRecruitmentService, AgentRegistryService, ApprovalService, PolicyEvaluator]:
    """Compose AgentRecruitmentService reusing P05.02 Registry and Governance helpers."""
    registry, approvals, evaluator = _build_registry(store)
    pm = PermissionManager(evaluator=evaluator, journal=store.events, approval_service=approvals)
    recruitment = AgentRecruitmentService(
        recruitment_repo=store.recruitment,
        agent_registry=registry,
        permission_manager=pm,
    )
    return recruitment, registry, approvals, evaluator


def test_p05_07_ast_purity_and_migration_triggers(tmp_path: Path) -> None:
    """Verify AST layer purity in core/agents/recruitment*.py and immutable SQLite revision triggers."""
    forbidden = {"sqlite3", "storage", "sqlalchemy", "subprocess", "crewai", "api", "ui", "httpx", "urllib", "requests"}
    for rel in ("core/agents/recruitment_models.py", "core/agents/recruitment.py"):
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden

    db_path = tmp_path / "recruit_triggers.db"
    _init_db(db_path)
    assert get_latest_migration_version() >= 9

    t0 = _utc(10, 0, 0)
    with SQLiteRepositoryStore(db_path) as store:
        recruitment, _, _ , _ = _build_recruitment(store)
        stored = recruitment.propose_candidate(
            RecruitmentProposalRequest(
                candidate_manifest=_worker_manifest_dict(agent_id="qa.recruited_probe"),
                rationale="Need independent probe capability.",
                needed_capabilities=["git_diff_analysis"],
                proposer_id="core.master",
                proposer_kind="orchestrator",
                timestamp=t0,
            )
        )
        revs = store.recruitment.list_revisions(stored.proposal.proposal_id)
        assert len(revs) == 1

    with sqlite_connection(db_path) as conn:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute("UPDATE agent_recruitment_revisions SET reason = 'tampered';")
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute("DELETE FROM agent_recruitment_revisions;")


def test_p05_07_propose_evaluate_activate_and_restart_recovery(tmp_path: Path) -> None:
    """Propose -> independent evaluate PASS -> activate into AgentRegistryService -> persist across restart."""
    db_path = tmp_path / "recruit_happy.db"
    _init_db(db_path)
    t0 = _utc(10, 0, 0)

    with SQLiteRepositoryStore(db_path) as store:
        recruitment, registry, _, _ = _build_recruitment(store)
        proposed = recruitment.propose_candidate(
            RecruitmentProposalRequest(
                candidate_manifest=_worker_manifest_dict(
                    agent_id="qa.security_auditor",
                    department="qa",
                    skills=["security_audit", "git_diff_analysis"],
                ),
                rationale="Missing security_audit skill in active team pool.",
                needed_capabilities=["security_audit"],
                proposer_id="core.master",
                proposer_kind="orchestrator",
                timestamp=t0,
            )
        )
        assert proposed.proposal.status == "PROPOSED"
        assert registry.get_agent("qa.security_auditor") is None

        evaluated = recruitment.evaluate_candidate(
            RecruitmentEvaluationRequest(
                proposal_id=proposed.proposal.proposal_id,
                evaluator_id="qa.lead_verifier",
                evaluator_kind="qa_verifier",
                summary="Manifest, role, permissions, and needed capabilities verified.",
                checks_passed=["capability_match"],
                timestamp=t0 + timedelta(seconds=5),
            )
        )
        assert evaluated.proposal.status == "EVALUATED_PASS"
        assert evaluated.proposal.evaluation is not None and evaluated.proposal.evaluation.passed is True
        assert registry.get_agent("qa.security_auditor") is None

        activated = recruitment.activate_candidate(
            RecruitmentActivationRequest(
                proposal_id=proposed.proposal.proposal_id,
                actor_id="operator.alice",
                actor_kind="operator",
                validated_capabilities=["security_audit"],
                timestamp=t0 + timedelta(seconds=10),
            )
        )
        assert activated.proposal.status == "ACTIVATED"
        assert activated.proposal.activated_manifest_revision == 1
        active_agent = registry.get_agent("qa.security_auditor")
        assert active_agent is not None and active_agent.registration_status == "ACTIVE"

    with SQLiteRepositoryStore(db_path) as reopened:
        reloaded = reopened.recruitment.get_proposal(proposed.proposal.proposal_id)
        assert reloaded is not None and reloaded.proposal.status == "ACTIVATED"
        assert [r.status for r in reopened.recruitment.list_revisions(proposed.proposal.proposal_id)] == [
            "PROPOSED",
            "EVALUATED_PASS",
            "ACTIVATED",
        ]
        reloaded_agent = reopened.agents.get_record("qa.security_auditor")
        assert reloaded_agent is not None and reloaded_agent.registration_status == "ACTIVE"


def test_p05_07_sensitive_grants_require_approval_and_succeed_with_approval(tmp_path: Path) -> None:
    """Candidates requesting tools or elevated network require ApprovalService sign-off before activation."""
    db_path = tmp_path / "recruit_approval.db"
    _init_db(db_path)
    t0 = _utc(11, 0, 0)
    manifest = _worker_manifest_dict(agent_id="it.tool_worker", network="intranet")
    manifest["tools"] = {"allowed": ["tools.files.read_file"]}  # type: ignore[index]

    with SQLiteRepositoryStore(db_path) as store:
        recruitment, registry, approvals, evaluator = _build_recruitment(store)
        proposed = recruitment.propose_candidate(
            RecruitmentProposalRequest(
                candidate_manifest=manifest,
                rationale="Need tool-enabled worker.",
                needed_capabilities=["git_diff_analysis"],
                proposer_id="core.master",
                proposer_kind="orchestrator",
                timestamp=t0,
            )
        )
        recruitment.evaluate_candidate(
            RecruitmentEvaluationRequest(
                proposal_id=proposed.proposal.proposal_id,
                evaluator_id="qa.verifier",
                evaluator_kind="qa_verifier",
                summary="Checks passed pending operator approval for sensitive grants.",
                timestamp=t0 + timedelta(seconds=2),
            )
        )

        with pytest.raises(RecruitmentGovernanceError):
            recruitment.activate_candidate(
                RecruitmentActivationRequest(
                    proposal_id=proposed.proposal.proposal_id,
                    actor_id="operator.alice",
                    actor_kind="operator",
                    timestamp=t0 + timedelta(seconds=4),
                )
            )
        assert registry.get_agent("it.tool_worker") is None

        alice = _operator_ctx("operator.alice")
        bob = _operator_ctx("operator.bob")
        fp = compute_request_fingerprint(
            action="agent.change_permissions",
            resource="agent:it.tool_worker",
            task_id=None,
            run_id=None,
            parameters={
                "target_agent_id": "it.tool_worker",
                "department": "it",
                "initial_status": "ACTIVE",
                "manifest_sha256": proposed.proposal.manifest_sha256,
                "sensitive_initial_grants": True,
            },
        )
        apr = approvals.create_approval_request(
            requester=alice,
            action="agent.change_permissions",
            target_resource="agent:it.tool_worker",
            task_id=None,
            run_id=None,
            request_fingerprint=fp,
            policy_snapshot=ApprovalPolicyContext(
                matched_rule_ids=["gov.require_approval_sensitive_admin_ops"],
                rule_versions=evaluator.policy_snapshot(["gov.require_approval_sensitive_admin_ops"]),
                reason_code="APPROVAL_RULE_MATCHED",
            ),
            risk_summary="Approve sensitive initial tool and intranet grants.",
            requested_at=t0 + timedelta(seconds=5),
        )
        approvals.decide_approval(
            apr.approval_id,
            decider=bob,
            approved=True,
            reason="Verified scoped permissions.",
            timestamp=t0 + timedelta(seconds=6),
        )

        activated = recruitment.activate_candidate(
            RecruitmentActivationRequest(
                proposal_id=proposed.proposal.proposal_id,
                actor_id="operator.alice",
                actor_kind="operator",
                approval_id=apr.approval_id,
                timestamp=t0 + timedelta(seconds=7),
            )
        )
        assert activated.proposal.status == "ACTIVATED"
        assert registry.get_agent("it.tool_worker") is not None


@pytest.mark.parametrize(
    "case_name",
    [
        "master_impersonation",
        "already_registered",
        "duplicate_active_proposal",
        "worker_propose_denied",
        "self_evaluation_forbidden",
        "activate_without_eval",
        "activate_after_eval_fail",
        "activate_after_reject",
    ],
)
def test_p05_07_fail_closed_blocks_invalid_recruitment_cases(tmp_path: Path, case_name: str) -> None:
    """Verify every invalid recruitment identity, permission, evaluation, or state gate fails closed."""
    db_path = tmp_path / f"recruit_{case_name}.db"
    _init_db(db_path)
    t0 = _utc(12, 0, 0)

    with SQLiteRepositoryStore(db_path) as store:
        recruitment, registry, _, _ = _build_recruitment(store)

        if case_name == "master_impersonation":
            bad = _worker_manifest_dict(agent_id="core.master")
            with pytest.raises(RecruitmentValidationError, match="core.master"):
                recruitment.propose_candidate(
                    RecruitmentProposalRequest(
                        candidate_manifest=bad,
                        rationale="Attempt master impersonation.",
                        needed_capabilities=["git_diff_analysis"],
                        proposer_id="core.master",
                        proposer_kind="orchestrator",
                        timestamp=t0,
                    )
                )
            return

        if case_name == "already_registered":
            registry.register_agent(
                _worker_manifest_dict(agent_id="it.existing"),
                auth_context=_operator_ctx(),
                timestamp=t0,
            )
            with pytest.raises(RecruitmentStateError, match="already exists"):
                recruitment.propose_candidate(
                    RecruitmentProposalRequest(
                        candidate_manifest=_worker_manifest_dict(agent_id="it.existing"),
                        rationale="Duplicate candidate.",
                        needed_capabilities=["git_diff_analysis"],
                        proposer_id="core.master",
                        proposer_kind="orchestrator",
                        timestamp=t0 + timedelta(seconds=1),
                    )
                )
            return

        if case_name == "worker_propose_denied":
            with pytest.raises(RecruitmentGovernanceError):
                recruitment.propose_candidate(
                    RecruitmentProposalRequest(
                        candidate_manifest=_worker_manifest_dict(agent_id="it.rogue"),
                        rationale="Worker attempting unauthorized recruitment.",
                        needed_capabilities=["git_diff_analysis"],
                        proposer_id="it.worker_1",
                        proposer_kind="worker_agent",
                        timestamp=t0,
                    )
                )
            return

        proposed = recruitment.propose_candidate(
            RecruitmentProposalRequest(
                candidate_manifest=_worker_manifest_dict(agent_id="it.candidate_x"),
                rationale="Need specialist capability.",
                needed_capabilities=["git_diff_analysis"],
                proposer_id="core.master",
                proposer_kind="orchestrator",
                timestamp=t0,
            )
        )

        if case_name == "duplicate_active_proposal":
            with pytest.raises(RecruitmentStateError, match="already exists"):
                recruitment.propose_candidate(
                    RecruitmentProposalRequest(
                        candidate_manifest=_worker_manifest_dict(agent_id="it.candidate_x"),
                        rationale="Second proposal while active.",
                        needed_capabilities=["git_diff_analysis"],
                        proposer_id="core.master",
                        proposer_kind="orchestrator",
                        timestamp=t0 + timedelta(seconds=1),
                    )
                )
        elif case_name == "self_evaluation_forbidden":
            with pytest.raises(RecruitmentGovernanceError, match="self-evaluate"):
                recruitment.evaluate_candidate(
                    RecruitmentEvaluationRequest(
                        proposal_id=proposed.proposal.proposal_id,
                        evaluator_id="core.master",
                        evaluator_kind="orchestrator",
                        summary="Self-approving own proposal.",
                        timestamp=t0 + timedelta(seconds=1),
                    )
                )
        elif case_name == "activate_without_eval":
            with pytest.raises(RecruitmentStateError, match="EVALUATED_PASS"):
                recruitment.activate_candidate(
                    RecruitmentActivationRequest(
                        proposal_id=proposed.proposal.proposal_id,
                        actor_id="operator.alice",
                        actor_kind="operator",
                        timestamp=t0 + timedelta(seconds=1),
                    )
                )
        elif case_name == "activate_after_eval_fail":
            failed = recruitment.evaluate_candidate(
                RecruitmentEvaluationRequest(
                    proposal_id=proposed.proposal.proposal_id,
                    evaluator_id="qa.verifier",
                    evaluator_kind="qa_verifier",
                    summary="Candidate failed readiness audit.",
                    findings=["Benchmark accuracy below threshold."],
                    timestamp=t0 + timedelta(seconds=1),
                )
            )
            assert failed.proposal.status == "EVALUATED_FAIL"
            with pytest.raises(RecruitmentStateError, match="EVALUATED_PASS"):
                recruitment.activate_candidate(
                    RecruitmentActivationRequest(
                        proposal_id=proposed.proposal.proposal_id,
                        actor_id="operator.alice",
                        actor_kind="operator",
                        timestamp=t0 + timedelta(seconds=2),
                    )
                )
        elif case_name == "activate_after_reject":
            rejected = recruitment.reject_proposal(
                RecruitmentRejectRequest(
                    proposal_id=proposed.proposal.proposal_id,
                    actor_id="operator.alice",
                    actor_kind="operator",
                    reason="Role unnecessary.",
                    timestamp=t0 + timedelta(seconds=1),
                )
            )
            assert rejected.proposal.status == "REJECTED"
            with pytest.raises(RecruitmentStateError, match="EVALUATED_PASS"):
                recruitment.activate_candidate(
                    RecruitmentActivationRequest(
                        proposal_id=proposed.proposal.proposal_id,
                        actor_id="operator.alice",
                        actor_kind="operator",
                        timestamp=t0 + timedelta(seconds=2),
                    )
                )
        assert registry.get_agent("it.candidate_x") is None


def test_p05_07_stale_version_and_event_append_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify optimistic locking and full SQLite rollback when Event Journal append fails."""
    db_path = tmp_path / "recruit_rollback.db"
    _init_db(db_path)
    t0 = _utc(13, 0, 0)

    with SQLiteRepositoryStore(db_path) as store:
        recruitment, _, _ , _ = _build_recruitment(store)
        proposed = recruitment.propose_candidate(
            RecruitmentProposalRequest(
                candidate_manifest=_worker_manifest_dict(agent_id="it.rollback_worker"),
                rationale="Test rollback and optimistic lock.",
                needed_capabilities=["git_diff_analysis"],
                proposer_id="core.master",
                proposer_kind="orchestrator",
                timestamp=t0,
            )
        )
        with pytest.raises(StaleRecruitmentVersionError):
            store.recruitment.transition_proposal_atomic(
                proposal=proposed.proposal.model_copy(update={"status": "REJECTED"}),
                expected_record_version=99,
                revision=store.recruitment.list_revisions(proposed.proposal.proposal_id)[0],
                event=store.events.read_entries()[0].event,
            )

        orig_append = store.events.append_on_connection
        monkeypatch.setattr(
            store.events,
            "append_on_connection",
            lambda conn, event: (
                AppendResult(appended=False, status="REJECTED", entry=None, error=None)
                if event.event_type == "agent.recruitment_proposed"
                else orig_append(conn, event)
            ),
        )
        with pytest.raises(Exception, match="rejected"):
            recruitment.propose_candidate(
                RecruitmentProposalRequest(
                    candidate_manifest=_worker_manifest_dict(agent_id="it.never_persisted"),
                    rationale="Should roll back cleanly.",
                    needed_capabilities=["git_diff_analysis"],
                    proposer_id="core.master",
                    proposer_kind="orchestrator",
                    timestamp=t0 + timedelta(seconds=2),
                )
            )
        assert store.recruitment.list_proposals(candidate_agent_id="it.never_persisted") == []
