"""Unit, Persistence, Governance, and End-to-End Acceptance tests for Phase 05 / P05.02 Agent Registry."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest

from core.agents import (
    AgentExplorerSummary,
    AgentManifest,
    AgentManifestValidationError,
    AgentNotFoundError,
    AgentRegistryService,
    AgentSearchQuery,
    AgentStateError,
    DuplicateAgentError,
    StaleAgentVersionError,
    load_agent_manifest_file,
)
from core.contracts import (
    Artifact,
    Event,
    Run,
    Session,
    Task,
    new_artifact_id,
    new_run_id,
    new_session_id,
    new_task_id,
)
from core.events.journal import AppendResult, JournalError
from core.governance import (
    ApprovalError,
    ApprovalPolicyContext,
    ApprovalService,
    GovernanceAuditError,
    PermissionDeniedError,
    PermissionManager,
    PolicyEvaluator,
)
from core.governance.models import TrustedAuthContext
from storage.db import apply_migrations, get_latest_migration_version, sqlite_connection
from storage.repositories import PersistentWorkflowService, SQLiteRepositoryStore

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS_ROOT = REPO_ROOT / "agents"


def _utc(hour: int = 10, minute: int = 0, second: int = 0) -> datetime:
    """Return deterministic UTC datetime."""
    return datetime(2026, 11, 26, hour, minute, second, tzinfo=timezone.utc)


def _init_db(db_path: Path) -> None:
    """Apply latest SQLite schema migrations including 0004_agent_registry.sql."""
    with sqlite_connection(db_path) as conn:
        res = apply_migrations(conn)
        assert res.current_version == get_latest_migration_version()
        assert res.current_version >= 4


def _operator_ctx(actor_id: str = "operator.alice") -> TrustedAuthContext:
    """Return trusted human operator context."""
    return TrustedAuthContext(
        actor_id=actor_id,
        actor_kind="human_operator",
        auth_source="operator_session",
        authenticated=True,
    )


def _master_ctx() -> TrustedAuthContext:
    """Return trusted Master Agent context."""
    return TrustedAuthContext(
        actor_id="core.master",
        actor_kind="master_agent",
        auth_source="application_boundary",
        authenticated=True,
    )


def _worker_ctx(actor_id: str = "it.artifact_worker") -> TrustedAuthContext:
    """Return trusted Worker Agent context."""
    return TrustedAuthContext(
        actor_id=actor_id,
        actor_kind="worker_agent",
        auth_source="application_boundary",
        authenticated=True,
    )


def _build_registry(
    store: SQLiteRepositoryStore,
    *,
    known_providers: tuple[str, ...] = ("antigravity", "google_gemini", "ollama"),
) -> tuple[AgentRegistryService, ApprovalService, PolicyEvaluator]:
    """Compose real SQLite-backed Governance + ApprovalService + AgentRegistryService."""
    evaluator = PolicyEvaluator()
    approvals = ApprovalService(store.approvals, store.events)
    pm = PermissionManager(evaluator=evaluator, journal=store.events, approval_service=approvals)
    registry = AgentRegistryService(
        repository=store.agents,
        permission_manager=pm,
        known_providers=known_providers,
    )
    return registry, approvals, evaluator


def _worker_manifest_dict(
    *,
    agent_id: str = "it.code_reviewer",
    department: str = "it",
    skills: list[str] | None = None,
    provider_id: str = "antigravity",
    connection_id: str = "default_antigravity",
    network: str = "none",
    timeout_seconds: int = 180,
) -> dict[str, object]:
    """Return valid non-secret Worker manifest dictionary for Registry scenarios."""
    return {
        "spec_version": "1.0.0",
        "id": agent_id,
        "name": f"Agent {agent_id}",
        "role": "Specialized Reviewer",
        "system_role": "worker",
        "department": department,
        "description": "Verified specialist manifest for Registry testing.",
        "mission": {
            "purpose": "Perform scoped technical tasks.",
            "responsibilities": ["Execute task cleanly within workspace."],
            "non_goals": ["Never bypass QA or Governance."],
        },
        "capabilities": {
            "skills": skills or ["git_diff_analysis"],
            "inputs": ["git_diff", "source_code"],
            "outputs": ["markdown_review_report"],
        },
        "engine": {
            "default_type": provider_id,
            "provider_id": provider_id,
            "adapter_id": "antigravity_cli",
            "connection_id": connection_id,
            "model_tier": "code",
            "model_id": None,
            "routing_profile": "code_precision",
            "fallback_policy": "operator_approved_only",
            "parameters": {"temperature": 0.1},
        },
        "memory": {"scopes": ["private", "project"]},
        "skills": {"allowed": []},
        "tools": {"allowed": []},
        "permissions": {
            "fs_read": ["workspace/{task_id}/inputs/*"],
            "fs_write": ["workspace/{task_id}/outputs/*"],
            "network": network,
            "requires_approval": ["code.exec_custom"],
        },
        "limits": {"timeout_seconds": timeout_seconds, "max_retries": 2},
    }


def test_p05_02_registration_guards_duplicates_unknown_provider_and_sensitive_approval(
    tmp_path: Path,
) -> None:
    """Verify criteria 1-6, 17-22: create, duplicate, invalid, auth, escalation, sensitive approval."""
    db_path = tmp_path / "reg_guards.db"
    _init_db(db_path)
    t0 = _utc(9, 0, 0)

    alice = _operator_ctx("operator.alice")
    bob = _operator_ctx("operator.bob")
    worker = _worker_ctx("it.code_reviewer")

    with SQLiteRepositoryStore(db_path) as store:
        registry, approvals, evaluator = _build_registry(store)

        # 1. Register valid Agent & 2. Reject duplicate ID.
        outcome = registry.register_agent(
            _worker_manifest_dict(),
            auth_context=alice,
            timestamp=t0,
        )
        assert outcome.record.agent_id == "it.code_reviewer"
        assert outcome.record.registration_status == "ACTIVE"
        assert outcome.record.active_revision == 1
        assert outcome.record.record_version == 1

        with pytest.raises(DuplicateAgentError) as dup_err:
            registry.register_agent(
                _worker_manifest_dict(),
                auth_context=alice,
                timestamp=t0 + timedelta(seconds=1),
            )
        assert dup_err.value.error_code == "DUPLICATE_AGENT_ID"

        # 3. Invalid manifest rejected.
        bad_manifest = _worker_manifest_dict(agent_id="qa.bad")
        bad_manifest["department"] = "invalid_dept"  # type: ignore[index]
        with pytest.raises(AgentManifestValidationError):
            registry.register_agent(bad_manifest, auth_context=alice, timestamp=t0)

        # 17. Unknown provider rejected fail-closed.
        with pytest.raises(AgentManifestValidationError, match="Unknown engine.provider_id"):
            registry.register_agent(
                _worker_manifest_dict(agent_id="qa.unknown_prov", provider_id="mystery_llm"),
                auth_context=alice,
                timestamp=t0,
            )

        # 4. Unauthorized registration (Worker / Master / missing auth rejected).
        with pytest.raises(PermissionDeniedError) as w_err:
            registry.register_agent(
                _worker_manifest_dict(agent_id="qa.rogue"),
                auth_context=worker,
                timestamp=t0,
            )
        assert w_err.value.decision.reason_code == "PRIVILEGE_ESCALATION_DENIED"

        with pytest.raises(PermissionDeniedError) as none_err:
            registry.register_agent(
                _worker_manifest_dict(agent_id="qa.noauth"),
                auth_context=None,
                timestamp=t0,
            )
        assert none_err.value.decision.reason_code == "MISSING_AUTH_CONTEXT"

        # 20. Sensitive initial ACTIVE registration (network='intranet') requires Approval Gate!
        sensitive_dict = _worker_manifest_dict(agent_id="research.net_worker", department="qa", network="intranet")
        sensitive_manifest = AgentManifest.model_validate(sensitive_dict)
        with pytest.raises(PermissionDeniedError) as sens_err:
            registry.register_agent(
                sensitive_manifest,
                auth_context=alice,
                timestamp=t0 + timedelta(seconds=5),
            )
        assert sens_err.value.decision.decision == "APPROVAL_REQUIRED"
        fp = sens_err.value.decision.request_fingerprint

        # Approve, then register successfully and verify single-use consumption!
        req = approvals.create_approval_request(
            requester=alice,
            action="agent.change_permissions",
            target_resource="agent:research.net_worker",
            task_id=None,
            run_id=None,
            request_fingerprint=fp,
            policy_snapshot=ApprovalPolicyContext(
                matched_rule_ids=["gov.require_approval_sensitive_admin_ops"],
                rule_versions=evaluator.policy_snapshot(["gov.require_approval_sensitive_admin_ops"]),
                reason_code="APPROVAL_RULE_MATCHED",
            ),
            risk_summary="Register worker with intranet network access",
            requested_at=t0 + timedelta(seconds=6),
        )
        approvals.decide_approval(
            req.approval_id,
            decider=bob,
            approved=True,
            reason="Verified bounded intranet scope",
            timestamp=t0 + timedelta(seconds=7),
        )
        ok_sens = registry.register_agent(
            sensitive_manifest,
            auth_context=alice,
            timestamp=t0 + timedelta(seconds=8),
            approval_id=req.approval_id,
        )
        assert ok_sens.record.registration_status == "ACTIVE"
        consumed_row = store.approvals.get_approval(req.approval_id)
        assert consumed_row is not None and consumed_row.status == "CONSUMED"


def test_p05_02_update_versioning_optimistic_lock_self_mod_and_escalation_approval(
    tmp_path: Path,
) -> None:
    """Verify criteria 5-9, 18-22: update, stale conflict, version history, self-mod block, approval gate."""
    db_path = tmp_path / "reg_updates.db"
    _init_db(db_path)
    t0 = _utc(10, 0, 0)

    alice = _operator_ctx("operator.alice")
    bob = _operator_ctx("operator.bob")

    with SQLiteRepositoryStore(db_path) as store:
        registry, approvals, evaluator = _build_registry(store)
        v1 = registry.register_agent(
            _worker_manifest_dict(),
            auth_context=alice,
            timestamp=t0,
        ).record

        # 7. Non-escalating description/skill metadata update works directly for human operator.
        upd_payload = _worker_manifest_dict(skills=["git_diff_analysis", "python_ast_audit"])
        v2_out = registry.update_agent(
            "it.code_reviewer",
            upd_payload,
            expected_record_version=1,
            auth_context=alice,
            timestamp=t0 + timedelta(seconds=10),
            change_summary="Added python_ast_audit declared skill",
        )
        assert v2_out.record.active_revision == 2
        assert v2_out.record.record_version == 2
        # New capability stays unvalidated/unobserved by default!
        new_cap = [c for c in v2_out.record.capability_verification if c.capability_id == "python_ast_audit"][0]
        assert new_cap.declared is True and new_cap.validated is False and new_cap.observed_in_execution is False

        # 8. Stale expected_record_version rejected cleanly without overwriting v2.
        with pytest.raises(StaleAgentVersionError) as stale_err:
            registry.update_agent(
                "it.code_reviewer",
                upd_payload,
                expected_record_version=1,
                auth_context=alice,
                timestamp=t0 + timedelta(seconds=11),
                change_summary="Stale overwrite attempt",
            )
        assert stale_err.value.error_code == "STALE_RECORD_VERSION"

        # 19. Agent cannot self-modify!
        with pytest.raises(PermissionDeniedError) as self_err:
            registry.update_agent(
                "it.code_reviewer",
                _worker_manifest_dict(timeout_seconds=300),
                expected_record_version=2,
                auth_context=_worker_ctx("it.code_reviewer"),
                timestamp=t0 + timedelta(seconds=12),
                change_summary="Worker trying to raise own timeout",
            )
        assert self_err.value.decision.reason_code == "SELF_MODIFICATION_FORBIDDEN"

        # 5 & 20 & 21. Timeout increase / permission escalation triggers Approval Gate; rejection blocks execution.
        esc_payload = _worker_manifest_dict(
            skills=["git_diff_analysis", "python_ast_audit"],
            timeout_seconds=300,
        )
        with pytest.raises(PermissionDeniedError) as esc_err:
            registry.update_agent(
                "it.code_reviewer",
                esc_payload,
                expected_record_version=2,
                auth_context=alice,
                timestamp=t0 + timedelta(seconds=15),
                change_summary="Increase timeout limit",
            )
        assert esc_err.value.decision.decision == "APPROVAL_REQUIRED"

        rejected_apr = approvals.create_approval_request(
            requester=alice,
            action="agent.change_permissions",
            target_resource="agent:it.code_reviewer",
            task_id=None,
            run_id=None,
            request_fingerprint=esc_err.value.decision.request_fingerprint,
            policy_snapshot=ApprovalPolicyContext(
                matched_rule_ids=["gov.require_approval_sensitive_admin_ops"],
                rule_versions=evaluator.policy_snapshot(["gov.require_approval_sensitive_admin_ops"]),
                reason_code="APPROVAL_RULE_MATCHED",
            ),
            risk_summary="Increase execution timeout limit",
            requested_at=t0 + timedelta(seconds=16),
        )
        approvals.decide_approval(
            rejected_apr.approval_id,
            decider=bob,
            approved=False,
            reason="Rejected higher timeout",
            timestamp=t0 + timedelta(seconds=17),
        )
        with pytest.raises(ApprovalError) as rej_err:
            registry.update_agent(
                "it.code_reviewer",
                esc_payload,
                expected_record_version=2,
                auth_context=alice,
                timestamp=t0 + timedelta(seconds=18),
                change_summary="Increase timeout limit",
                approval_id=rejected_apr.approval_id,
            )
        assert rej_err.value.error_code == "APPROVAL_REJECTED"

        # 9 & 25. Version history remains intact and DB triggers block direct UPDATE/DELETE.
        versions = registry.get_version_history("it.code_reviewer")
        assert [v.manifest_revision for v in versions] == [1, 2]
        assert versions[0].manifest.capabilities.skills == ["git_diff_analysis"]
        assert versions[1].manifest.capabilities.skills == ["git_diff_analysis", "python_ast_audit"]
        assert registry.get_manifest_version("it.code_reviewer", 1) == versions[0]

    with sqlite_connection(db_path) as conn:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute(
                "UPDATE agent_manifest_versions SET change_summary = 'tampered' WHERE agent_id = 'it.code_reviewer';"
            )
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute("DELETE FROM agent_manifest_versions WHERE agent_id = 'it.code_reviewer';")


def test_p05_02_disable_active_run_preservation_master_protection_and_rollback(
    tmp_path: Path,
) -> None:
    """Verify criteria 6, 10-12, 23-24: disable twice, active runs preserved, Master protected, audit rollback."""
    db_path = tmp_path / "reg_disable.db"
    _init_db(db_path)
    t0 = _utc(11, 0, 0)

    alice = _operator_ctx("operator.alice")
    bob = _operator_ctx("operator.bob")

    with SQLiteRepositoryStore(db_path) as store:
        registry, approvals, evaluator = _build_registry(store)
        master_m = load_agent_manifest_file(AGENTS_ROOT / "master" / "agent.yaml", allowed_root=AGENTS_ROOT)
        worker_m = load_agent_manifest_file(
            AGENTS_ROOT / "roles" / "it_artifact_worker" / "agent.yaml", allowed_root=AGENTS_ROOT
        )

        m_rec = registry.register_agent(master_m, auth_context=alice, timestamp=t0).record
        w_rec = registry.register_agent(worker_m, auth_context=alice, timestamp=t0 + timedelta(seconds=1)).record

        # Seed active Session/Run/Artifact for it.artifact_worker so we prove disable leaves active work intact.
        svc = PersistentWorkflowService(store)
        task = Task(
            task_id=new_task_id(),
            title="Active worker run check",
            description="Ensure disabling agent preserves active runs and artifacts.",
            requested_by="operator.alice",
            status="QUEUED",
            created_at=t0,
        )
        svc.create_task(task)
        run = Run(
            run_id=new_run_id(),
            task_id=task.task_id,
            runtime_id="custom.master_worker_v1",
            status="RUNNING",
            started_at=t0 + timedelta(seconds=2),
        )
        svc.create_run(run)
        sess = Session(
            session_id=new_session_id(),
            agent_id="it.artifact_worker",
            task_id=task.task_id,
            run_id=run.run_id,
            provider_id="antigravity",
            connection_id="default_antigravity",
            provider_conversation_id="conv-stay-running",
            status="ACTIVE",
            started_at=t0 + timedelta(seconds=2),
        )
        store.sessions.save_session(sess)
        art = Artifact(
            artifact_id=new_artifact_id(),
            task_id=task.task_id,
            run_id=run.run_id,
            owner_agent_id="it.artifact_worker",
            relative_path="outputs/partial.txt",
            verification_status="UNVERIFIED",
        )
        store.metadata.save_artifact(art)

        # 6. core.master protected from Worker, self-disable, and unapproved operator disable.
        with pytest.raises(PermissionDeniedError):
            registry.disable_agent(
                "core.master",
                expected_record_version=m_rec.record_version,
                auth_context=_worker_ctx("it.artifact_worker"),
                timestamp=t0 + timedelta(seconds=5),
                reason="Worker trying to kill Master",
            )
        with pytest.raises(PermissionDeniedError) as master_self_err:
            registry.disable_agent(
                "core.master",
                expected_record_version=m_rec.record_version,
                auth_context=_master_ctx(),
                timestamp=t0 + timedelta(seconds=5),
                reason="Master self disable",
            )
        assert master_self_err.value.decision.reason_code == "SELF_MODIFICATION_FORBIDDEN"

        with pytest.raises(PermissionDeniedError) as m_op_err:
            registry.disable_agent(
                "core.master",
                expected_record_version=m_rec.record_version,
                auth_context=alice,
                timestamp=t0 + timedelta(seconds=5),
                reason="Operator disabling Master without approval",
            )
        assert m_op_err.value.decision.decision == "APPROVAL_REQUIRED"

        # 10 & 12. Disable worker -> keeps active Run RUNNING & artifacts untouched!
        dis_out = registry.disable_agent(
            "it.artifact_worker",
            expected_record_version=w_rec.record_version,
            auth_context=alice,
            timestamp=t0 + timedelta(seconds=10),
            reason="Offboard role after active work",
        )
        assert dis_out.record.registration_status == "DISABLED"
        assert dis_out.active_run_ids_not_cancelled == [run.run_id]
        assert store.runs.get_run(run.run_id).status == "RUNNING"  # type: ignore[union-attr]
        assert store.metadata.get_artifact(art.artifact_id) is not None
        assert store.sessions.get_session(sess.session_id).status == "ACTIVE"  # type: ignore[union-attr]

        # 11. Disable twice rejected fail-closed.
        with pytest.raises(AgentStateError) as dbl_err:
            registry.disable_agent(
                "it.artifact_worker",
                expected_record_version=dis_out.record.record_version,
                auth_context=alice,
                timestamp=t0 + timedelta(seconds=11),
                reason="Duplicate disable",
            )
        assert dbl_err.value.error_code == "ALREADY_DISABLED"

        # 12. Eligible search excludes disabled worker, while non-eligible lookup sees it.
        assert (
            registry.search_agents(
                AgentSearchQuery(capability="workspace_artifact_authoring", eligible_only=True)
            )
            == []
        )

        # Re-enable restores eligibility cleanly.
        en_out = registry.enable_agent(
            "it.artifact_worker",
            expected_record_version=dis_out.record.record_version,
            auth_context=alice,
            timestamp=t0 + timedelta(seconds=12),
            reason="Reactivated by operator",
        )
        assert en_out.record.registration_status == "ACTIVE"
        assert (
            len(
                registry.search_agents(
                    AgentSearchQuery(capability="workspace_artifact_authoring", eligible_only=True)
                )
            )
            == 1
        )

        # 24. Event append failure triggers full rollback (neither governance journal nor SQLite mutates).
        class _BrokenJournal:
            def append(self, event: Event) -> object:
                raise RuntimeError("disk full during governance audit")

            def read_entries(self, **kwargs: object) -> list[object]:  # pragma: no cover
                return []

        broken_pm = PermissionManager(
            evaluator=PolicyEvaluator(),
            journal=_BrokenJournal(),  # type: ignore[arg-type]
        )
        broken_reg = AgentRegistryService(
            repository=store.agents,
            permission_manager=broken_pm,
        )
        with pytest.raises(GovernanceAuditError):
            broken_reg.register_agent(
                _worker_manifest_dict(agent_id="qa.rollback_test", department="qa"),
                auth_context=alice,
                timestamp=t0 + timedelta(seconds=20),
            )
        assert store.agents.get_record("qa.rollback_test") is None


def test_p05_02_section_ix_end_to_end_acceptance_and_drift_and_explorer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execute complete Section IX 21-step Offline Acceptance Scenario with real SQLite + restart."""
    # Guarantee zero Antigravity CLI or subprocess invocations during all Registry steps.
    def _forbid_subprocess(*args: object, **kwargs: object) -> None:
        raise AssertionError("Agent Registry must never spawn Antigravity CLI or runtime subprocesses!")

    monkeypatch.setattr(subprocess, "Popen", _forbid_subprocess)
    monkeypatch.setattr(subprocess, "run", _forbid_subprocess)

    # Copy real YAML manifests into tmp agents dir so we can also test disk drift safely without touching git repo!
    ws_agents = tmp_path / "agents"
    shutil.copytree(AGENTS_ROOT / "master", ws_agents / "master")
    shutil.copytree(AGENTS_ROOT / "roles", ws_agents / "roles")

    db_path = tmp_path / "acceptance_p0502.db"
    _init_db(db_path)
    t0 = _utc(14, 0, 0)
    alice = _operator_ctx("operator.alice")
    worker_actor = _worker_ctx("it.artifact_worker")

    # Steps 1-14 inside Session 1
    with SQLiteRepositoryStore(db_path) as store1:
        registry1, _, _ = _build_registry(store1)

        # 1-2. Load and validate Master manifest.
        master_m = load_agent_manifest_file(ws_agents / "master" / "agent.yaml", allowed_root=ws_agents)
        assert master_m.to_agent_identity().agent_id == "core.master"

        # 3-4. Load and validate Worker manifest.
        worker_m = load_agent_manifest_file(
            ws_agents / "roles" / "it_artifact_worker" / "agent.yaml", allowed_root=ws_agents
        )
        assert worker_m.to_agent_identity().agent_id == "it.artifact_worker"

        # 5-6. Register both Agents under authorized Governance.
        m_reg = registry1.register_agent(
            master_m,
            auth_context=alice,
            timestamp=t0,
            source_path="agents/master/agent.yaml",
        ).record
        w_reg = registry1.register_agent(
            worker_m,
            auth_context=alice,
            timestamp=t0 + timedelta(seconds=1),
            source_path="agents/roles/it_artifact_worker/agent.yaml",
            validated_capabilities=["workspace_artifact_authoring"],
        ).record

        # 7. Verify Worker cannot mutate core.master.
        with pytest.raises(PermissionDeniedError):
            registry1.update_agent(
                "core.master",
                master_m,
                expected_record_version=m_reg.record_version,
                auth_context=worker_actor,
                timestamp=t0 + timedelta(seconds=2),
                change_summary="Unauthorized master edit",
            )

        # 8. Search Worker by capability, department, input/output, and distinction of declared vs observed.
        found = registry1.search_agents(
            AgentSearchQuery(
                department="it",
                capability="workspace_artifact_authoring",
                input_format="task_brief",
                output_format="workspace_file_artifact",
                eligible_only=True,
                require_validated_capability=True,
            )
        )
        assert [r.agent_id for r in found] == ["it.artifact_worker"]
        assert (
            registry1.search_agents(
                AgentSearchQuery(
                    capability="workspace_artifact_authoring",
                    require_observed_capability=True,
                )
            )
            == []
        )

        # 9. Update Worker Manifest revision (adding non-escalating skill tag).
        w_v2_data = worker_m.model_dump(mode="json")
        w_v2_data["capabilities"]["skills"] = ["workspace_artifact_authoring", "structured_report_writing"]
        w_v2 = registry1.update_agent(
            "it.artifact_worker",
            w_v2_data,
            expected_record_version=w_reg.record_version,
            auth_context=alice,
            timestamp=t0 + timedelta(seconds=5),
            change_summary="Declare structured_report_writing capability",
        ).record
        assert w_v2.active_revision == 2

        # 10. Old version remains queryable.
        old_v1 = registry1.get_manifest_version("it.artifact_worker", 1)
        assert old_v1 is not None
        assert old_v1.manifest.capabilities.skills == ["workspace_artifact_authoring"]

        # 11. Stale version update blocked.
        with pytest.raises(StaleAgentVersionError):
            registry1.update_agent(
                "it.artifact_worker",
                w_v2_data,
                expected_record_version=1,
                auth_context=alice,
                timestamp=t0 + timedelta(seconds=6),
                change_summary="Stale retry",
            )

        # Verify manifest drift detection (v2 in SQLite vs v1 on disk -> DRIFT_DETECTED; master -> IN_SYNC).
        assert (
            registry1.detect_manifest_drift(
                "core.master", ws_agents / "master" / "agent.yaml", allowed_root=ws_agents
            ).status
            == "IN_SYNC"
        )
        drift = registry1.detect_manifest_drift(
            "it.artifact_worker",
            ws_agents / "roles" / "it_artifact_worker" / "agent.yaml",
            allowed_root=ws_agents,
        )
        assert drift.status == "DRIFT_DETECTED"

        # Verify Explorer Summary contains zero fabricated runtime state.
        summaries = registry1.export_explorer_summaries()
        assert all(isinstance(s, AgentExplorerSummary) for s in summaries)
        assert all(s.runtime_state_source == "NOT_OBSERVED_BY_REGISTRY" for s in summaries)

        # 12-13. Disable Worker & confirm excluded from eligible assignments.
        dis_res = registry1.disable_agent(
            "it.artifact_worker",
            expected_record_version=w_v2.record_version,
            auth_context=alice,
            timestamp=t0 + timedelta(seconds=10),
            reason="Paused for maintenance",
        )
        assert dis_res.record.registration_status == "DISABLED"
        assert (
            registry1.search_agents(
                AgentSearchQuery(capability="workspace_artifact_authoring", eligible_only=True)
            )
            == []
        )

    # 14-21. Close & reopen fresh SQLiteRepositoryStore after simulated restart!
    with SQLiteRepositoryStore(db_path) as store2:
        registry2, _, _ = _build_registry(store2)

        # 16. Reload Master and Worker.
        reloaded_master = registry2.get_agent("core.master")
        reloaded_worker = registry2.get_agent("it.artifact_worker")
        assert reloaded_master is not None and reloaded_master.registration_status == "ACTIVE"
        assert reloaded_worker is not None

        # 17-18. Version history and disabled status preserved across restart!
        assert reloaded_worker.registration_status == "DISABLED"
        assert reloaded_worker.disabled_reason == "Paused for maintenance"
        assert [v.manifest_revision for v in registry2.get_version_history("it.artifact_worker")] == [1, 2]

        # 19-20. Read Event Journal & verify Governance + Registry audit trail!
        worker_events = store2.events.read_entries(
            correlation_id="corr-agent-reg-it.artifact_worker"
        )
        upd_events = store2.events.read_entries(correlation_id="corr-agent-upd-it.artifact_worker")
        dis_events = store2.events.read_entries(correlation_id="corr-agent-dis-it.artifact_worker")
        master_upd_denied = [
            e.event
            for e in store2.events.read_entries(correlation_id="corr-agent-upd-core.master")
            if e.event.event_type == "governance.decision_recorded"
        ]
        assert any(e.event.event_type == "agent.registered" for e in worker_events)
        assert any(e.event.event_type == "agent.updated" for e in upd_events)
        assert any(e.event.event_type == "agent.disabled" for e in dis_events)
        assert any(ev.redacted_payload.get("decision") == "DENY" for ev in master_upd_denied)

        # 21. Verify zero Runs or Sessions were fabricated by Registry operations.
        assert store2.runs.list_runs_for_task("task_nonexistent") == []
        assert store2.sessions.list_sessions() == []
        with sqlite_connection(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM runs;").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM sessions;").fetchone()[0] == 0

        # Also check provider/engine/name/role search filters & credential safety.
        by_provider = registry2.search_agents(
            AgentSearchQuery(
                provider_id="antigravity",
                default_engine_type="antigravity",
                name_contains="Master",
                role_contains="Orchestration",
                registration_status="ACTIVE",
            )
        )
        assert [r.agent_id for r in by_provider] == ["core.master"]
        assert reloaded_master.manifest.to_agent_identity().allowed_connections[0].credential_ref is None


def test_p05_02_sqlite_inner_event_append_rejection_rolls_back_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 24b: Even if Policy audit passed, SQLite inner event rejection rolls back Registry tables."""
    db_path = tmp_path / "reg_inner_rollback.db"
    _init_db(db_path)
    t0 = _utc(15, 0, 0)
    alice = _operator_ctx("operator.alice")

    with SQLiteRepositoryStore(db_path) as store:
        registry, _, _ = _build_registry(store)

        def _reject_append_on_conn(conn: sqlite3.Connection, event: Event) -> AppendResult:
            if event.event_type == "agent.registered":
                return AppendResult(
                    status="REJECTED",
                    appended=False,
                    error=JournalError(
                        error_code="UNSAFE_PAYLOAD",
                        message="Injected inner append_on_connection rejection",
                    ),
                )
            return original_append(conn, event)

        original_append = store.events.append_on_connection
        monkeypatch.setattr(store.events, "append_on_connection", _reject_append_on_conn)

        with pytest.raises(Exception, match="Injected inner append_on_connection rejection"):
            registry.register_agent(
                _worker_manifest_dict(agent_id="qa.inner_rollback", department="qa"),
                auth_context=alice,
                timestamp=t0,
            )

        assert store.agents.get_record("qa.inner_rollback") is None
        with sqlite_connection(db_path) as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM agent_manifest_versions WHERE agent_id = 'qa.inner_rollback';"
                ).fetchone()[0]
                == 0
            )
