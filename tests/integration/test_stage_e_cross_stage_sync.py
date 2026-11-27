"""Cross-stage synchronization regression tests across Stages A-E."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.agents import analyze_manifest_diff, load_agent_manifest_file, load_department_manifests
from core.contracts import Event, Run, Task, new_event_id, new_run_id, new_task_id
from core.events.replay import replay_run_history, replay_task_history, validate_journal_entries
from core.governance import (
    ApprovalPolicyContext,
    ApprovalService,
    PermissionManager,
    PolicyEvaluator,
    PolicyRequest,
    PolicyRule,
    TrustedAuthContext,
    build_default_policy_rules,
)
from core.orchestration import (
    CollaborationDeliveryService,
    DynamicTeamBuilderService,
    MasterDeliveryCompileRequest,
    TeamBuildRequest,
    WorkerExecutionRequest,
)
from core.tools import ToolInvocationRequest, ToolRegistry, ToolRuntime
from engines.connections import EngineConnectionRef
from engines.contract import EngineResult, EngineSessionReference, EngineTelemetry
from storage.artifacts import ArtifactVaultService, VaultApprovalRequiredError, VaultError
from storage.db import apply_migrations, sqlite_connection
from storage.repositories import SQLiteApprovalRepository, SQLiteRepositoryStore, recover_task
from tests.orchestration.test_p05_05_worker_session_lifecycle import (
    BASE_TIME,
    FakeConfigurableEngineAdapter,
    _master_auth,
    _operator_auth,
    _seed_planned_assigned_task,
    _setup_environment,
)
from tests.storage.test_p08_05_backup_trash_and_bulk import _approve_action
from tools.registry_bootstrap import register_phase07_tools

REPO_ROOT = Path(__file__).resolve().parents[2]


def _utc(minute: int = 0) -> datetime:
    return datetime(2026, 11, 27, 22, minute, 0, tzinfo=timezone.utc)


def _open_sync_stack(
    tmp_path: Path,
    *,
    rules: list[PolicyRule] | None = None,
) -> dict[str, object]:
    db_file = tmp_path / "sync.db"
    with sqlite_connection(db_file) as conn:
        apply_migrations(conn)
    store = SQLiteRepositoryStore(db_file)
    approvals = ApprovalService(SQLiteApprovalRepository(store), store.events)
    pm = PermissionManager(
        evaluator=PolicyEvaluator(rules=rules if rules is not None else build_default_policy_rules()),
        journal=store.events,
        approval_service=approvals,
    )
    vault = ArtifactVaultService(
        store=store,
        vault_root=tmp_path / "vault",
        permission_manager=pm,
        journal=store.events,
    )
    tool_registry = ToolRegistry()
    register_phase07_tools(tool_registry)
    tool_runtime = ToolRuntime(
        registry=tool_registry,
        permission_manager=pm,
        journal=store.events,
        tool_repo=store.tools,
    )
    return {
        "db_file": db_file,
        "store": store,
        "approvals": approvals,
        "pm": pm,
        "vault": vault,
        "tool_registry": tool_registry,
        "tool_runtime": tool_runtime,
    }


def test_a1_a2_vault_artifact_syncs_task_snapshot_and_tool_replay_observes_tools(tmp_path: Path) -> None:
    """A1 + A2: Completed Tasks with Vault artifacts recover CLEAN and Replay captures tool_id."""
    stack = _open_sync_stack(tmp_path)
    store: SQLiteRepositoryStore = stack["store"]  # type: ignore[assignment]
    vault: ArtifactVaultService = stack["vault"]  # type: ignore[assignment]
    tool_runtime: ToolRuntime = stack["tool_runtime"]  # type: ignore[assignment]
    t0 = _utc(0)
    op_ctx = TrustedAuthContext(
        actor_id="operator.alice",
        actor_kind="human_operator",
        auth_source="operator_session",
        authenticated=True,
        allowed_tools=["tools.files.list"],
    )
    try:
        task = Task(
            task_id=new_task_id(),
            title="Vault & Tool Replay Sync",
            description="Ensure Vault artifacts and ToolRuntime events replay cleanly.",
            requested_by="operator.alice",
            status="QUEUED",
            priority="NORMAL",
            created_at=t0,
            updated_at=t0,
        )
        store.tasks.create_task(task)
        run = Run(
            run_id=new_run_id(),
            task_id=task.task_id,
            runtime_id="custom.master_worker_v1",
            status="PENDING",
        )
        store.runs.create_run(run)

        store.events.append(
            Event(
                event_id=new_event_id(),
                timestamp=t0 + timedelta(seconds=1),
                event_type="run.started",
                source="core_runtime",
                task_id=task.task_id,
                run_id=run.run_id,
                correlation_id="corr-a1a2",
                redacted_payload={"previous_status": "PENDING", "new_status": "RUNNING"},
            )
        )
        store.runs.save_run_state(
            run.model_copy(update={"status": "RUNNING", "started_at": t0 + timedelta(seconds=1)}),
            expected_version=1,
        )
        store.events.append(
            Event(
                event_id=new_event_id(),
                timestamp=t0 + timedelta(seconds=2),
                event_type="task.status_changed",
                source="core_runtime",
                task_id=task.task_id,
                run_id=run.run_id,
                correlation_id="corr-a1a2",
                redacted_payload={"previous_status": "QUEUED", "new_status": "ASSIGNED"},
            )
        )
        store.tasks.save_task_state(
            task.model_copy(update={"status": "ASSIGNED", "updated_at": t0 + timedelta(seconds=2)}),
            expected_version=1,
        )
        store.events.append(
            Event(
                event_id=new_event_id(),
                timestamp=t0 + timedelta(seconds=3),
                event_type="task.status_changed",
                source="core_runtime",
                task_id=task.task_id,
                run_id=run.run_id,
                correlation_id="corr-a1a2",
                redacted_payload={"previous_status": "ASSIGNED", "new_status": "RUNNING"},
            )
        )
        store.tasks.save_task_state(
            task.model_copy(
                update={"status": "RUNNING", "active_run_id": run.run_id, "updated_at": t0 + timedelta(seconds=3)}
            ),
            expected_version=2,
        )

        # Register Vault artifact -> Task snapshot must receive artifact_id and bump version.
        entry = vault.register_artifact(
            auth_context=op_ctx,
            task_id=task.task_id,
            run_id=run.run_id,
            owner_agent_id="it.artifact_worker",
            logical_key="sync/report.txt",
            relative_path="outputs/report.txt",
            content_bytes=b"verified vault deliverable",
            verification_status="VERIFIED_PASS",
            evidence_reference="evd_sync_a1",
            timestamp=t0 + timedelta(seconds=4),
        )
        refreshed = store.tasks.get_stored_task(task.task_id)
        assert refreshed is not None
        assert entry.artifact_id in refreshed.task.artifact_ids
        assert refreshed.version == 4

        # Invoke real ToolRuntime tools -> Replay must capture tool_id into observed_tool_names.
        ws_dir = tmp_path / "ws"
        (ws_dir / "outputs").mkdir(parents=True)
        (ws_dir / "outputs" / "note.txt").write_text("tool note", encoding="utf-8")
        tool_res = tool_runtime.invoke(
            ToolInvocationRequest(
                auth_context=op_ctx,
                tool_id="tools.files.list",
                parameters={"path": "outputs"},
                task_id=task.task_id,
                run_id=run.run_id,
                workspace_root=str(ws_dir),
                timestamp=t0 + timedelta(seconds=5),
            )
        )
        assert tool_res.status == "SUCCESS"

        store.events.append(
            Event(
                event_id=new_event_id(),
                timestamp=t0 + timedelta(seconds=6),
                event_type="run.completed",
                source="core_runtime",
                task_id=task.task_id,
                run_id=run.run_id,
                correlation_id="corr-a1a2",
                redacted_payload={"previous_status": "RUNNING", "new_status": "SUCCEEDED"},
            )
        )
        store.runs.save_run_state(
            run.model_copy(
                update={
                    "status": "SUCCEEDED",
                    "started_at": t0 + timedelta(seconds=1),
                    "finished_at": t0 + timedelta(seconds=6),
                }
            ),
            expected_version=2,
        )
        store.events.append(
            Event(
                event_id=new_event_id(),
                timestamp=t0 + timedelta(seconds=7),
                event_type="task.status_changed",
                source="qa_verifier",
                task_id=task.task_id,
                run_id=run.run_id,
                evidence_id="evd_sync_a1",
                correlation_id="corr-a1a2",
                redacted_payload={"previous_status": "RUNNING", "new_status": "COMPLETED"},
            )
        )
        store.tasks.save_task_state(
            refreshed.task.model_copy(
                update={
                    "status": "COMPLETED",
                    "active_run_id": None,
                    "updated_at": t0 + timedelta(seconds=7),
                }
            ),
            expected_version=refreshed.version,
        )

        report = recover_task(store, task.task_id)
        assert report.recovery_status == "VERIFIED_CLEAN"
        assert report.inconsistencies == []
        assert report.snapshot_matches_replay is True
        assert entry.artifact_id in report.replay_result.artifact_ids
        assert report.replay_result.observed_tool_names == ["tools.files.list"]

        run_replay = replay_run_history(store.events.read_entries(run_id=run.run_id), run_id=run.run_id)
        assert run_replay.observed_tool_names == ["tools.files.list"]
    finally:
        store.close()


def test_a3_purge_under_custom_allow_and_inplace_restore_approval_non_replay(tmp_path: Path) -> None:
    """A3: Custom ALLOW purge actually mutates DB/blob; in-place restore keeps approval CONSUMED."""
    # Part 1: Custom ALLOW rule without APPROVAL_REQUIRED for trash.purge.
    custom_rules = [
        rule
        for rule in build_default_policy_rules()
        if rule.rule_id != "gov.require_approval_sensitive_admin_ops"
    ]
    custom_rules.append(
        PolicyRule(
            rule_id="custom.allow_operator_purge",
            version="1.0.0",
            name="Allow Operator Direct Purge",
            description="Test custom ALLOW rule bypassing approval gate for trash.purge.",
            category="permission",
            targets=["human_operator"],
            resources=["*"],
            priority=50,
            effect="ALLOW",
            actions=["trash.purge"],
        )
    )
    allow_dir = tmp_path / "allow_purge"
    allow_stack = _open_sync_stack(allow_dir, rules=custom_rules)
    allow_store: SQLiteRepositoryStore = allow_stack["store"]  # type: ignore[assignment]
    allow_vault: ArtifactVaultService = allow_stack["vault"]  # type: ignore[assignment]
    op_ctx = TrustedAuthContext(
        actor_id="operator.alice",
        actor_kind="human_operator",
        auth_source="operator_session",
        authenticated=True,
        allowed_tools=["tools.files.list"],
    )
    t0 = _utc(0)
    try:
        task = Task(
            task_id=new_task_id(),
            title="Custom Allow Purge",
            description="Verify ALLOW purge mutates DB and unlinks blob.",
            requested_by="operator.alice",
            created_at=t0,
            updated_at=t0,
        )
        allow_store.tasks.create_task(task)
        run = Run(run_id=new_run_id(), task_id=task.task_id, runtime_id="custom.master_worker_v1")
        allow_store.runs.create_run(run)
        entry = allow_vault.register_artifact(
            auth_context=op_ctx,
            task_id=task.task_id,
            run_id=run.run_id,
            owner_agent_id="vault.keeper",
            logical_key="disposable/temp.txt",
            relative_path="outputs/temp.txt",
            content_bytes=b"temporary allow purge payload",
            timestamp=t0 + timedelta(seconds=1),
        )
        blob_file = allow_dir / "vault" / entry.storage_relpath
        assert blob_file.is_file()

        allow_vault.trash_artifact(entry.artifact_id, auth_context=op_ctx, reason="Cleanup", timestamp=t0 + timedelta(seconds=2))
        purged = allow_vault.purge_artifact(entry.artifact_id, auth_context=op_ctx, timestamp=t0 + timedelta(seconds=3))
        assert purged.lifecycle_status == "PURGED"
        assert purged.blob_deleted is True
        assert not blob_file.exists()
        reloaded = allow_vault.get_artifact(entry.artifact_id, auth_context=op_ctx, timestamp=t0 + timedelta(seconds=4))
        assert reloaded.lifecycle_status == "PURGED"
    finally:
        allow_store.close()

    # Part 2: In-place live restore (`target_db_path=None`) must NOT revert CONSUMED approval.
    restore_dir = tmp_path / "inplace_restore"
    rest_stack = _open_sync_stack(restore_dir)
    rest_store: SQLiteRepositoryStore = rest_stack["store"]  # type: ignore[assignment]
    rest_vault: ArtifactVaultService = rest_stack["vault"]  # type: ignore[assignment]
    rest_approvals: ApprovalService = rest_stack["approvals"]  # type: ignore[assignment]
    op_dec = TrustedAuthContext(
        actor_id="operator.approver",
        actor_kind="human_operator",
        auth_source="operator_session",
        authenticated=True,
    )
    try:
        task2 = Task(
            task_id=new_task_id(),
            title="Live Restore Approval Safety",
            description="Ensure single-use restore approval stays CONSUMED after in-place restore.",
            requested_by="operator.alice",
            created_at=t0,
            updated_at=t0,
        )
        rest_store.tasks.create_task(task2)
        run2 = Run(run_id=new_run_id(), task_id=task2.task_id, runtime_id="custom.master_worker_v1")
        rest_store.runs.create_run(run2)
        rest_vault.register_artifact(
            auth_context=op_ctx,
            task_id=task2.task_id,
            run_id=run2.run_id,
            owner_agent_id="vault.keeper",
            logical_key="important/live.txt",
            relative_path="outputs/live.txt",
            content_bytes=b"critical state before backup",
            timestamp=t0 + timedelta(seconds=1),
        )
        backup = rest_vault.create_backup(auth_context=op_ctx, timestamp=t0 + timedelta(seconds=2), retention_days=7)

        with pytest.raises(VaultApprovalRequiredError) as need_apr:
            rest_vault.restore_backup(
                backup.backup_id,
                auth_context=op_ctx,
                timestamp=t0 + timedelta(seconds=3),
            )
        apr_id = _approve_action(rest_approvals, op_ctx, op_dec, need_apr.value, t0 + timedelta(seconds=4))

        report = rest_vault.restore_backup(
            backup.backup_id,
            auth_context=op_ctx,
            approval_id=apr_id,
            timestamp=t0 + timedelta(seconds=5),
        )
        assert report.integrity_ok is True
        assert report.foreign_key_ok is True

        stored_apr = rest_store.approvals.get_approval(apr_id)
        assert stored_apr is not None
        assert stored_apr.status == "CONSUMED"
        assert validate_journal_entries(rest_store.events.read_entries(), require_contiguous_sequences=True) == []

        with pytest.raises(VaultError) as replay_err:
            rest_vault.restore_backup(
                backup.backup_id,
                auth_context=op_ctx,
                approval_id=apr_id,
                timestamp=t0 + timedelta(seconds=6),
            )
        assert replay_err.value.error_code == "APPROVAL_ALREADY_CONSUMED"
    finally:
        rest_store.close()


def test_a4_master_delivery_verifies_vault_blobs_and_blocks_trashed_artifacts(tmp_path: Path) -> None:
    """A4: CollaborationDeliveryService verifies ACTIVE Vault blobs, uses API download_ref, blocks TRASHED."""
    stack = _open_sync_stack(tmp_path)
    store: SQLiteRepositoryStore = stack["store"]  # type: ignore[assignment]
    vault: ArtifactVaultService = stack["vault"]  # type: ignore[assignment]
    pm: PermissionManager = stack["pm"]  # type: ignore[assignment]
    deliv_svc = CollaborationDeliveryService(
        collaboration_repo=store.collaboration,
        handoff_repo=store.handoffs,
        task_repo=store.tasks,
        run_repo=store.runs,
        metadata_repo=store.metadata,
        permission_manager=pm,
        artifact_vault=vault,
    )
    t0 = _utc(0)
    op_ctx = TrustedAuthContext(
        actor_id="operator.alice",
        actor_kind="human_operator",
        auth_source="operator_session",
        authenticated=True,
        allowed_tools=["tools.files.list"],
    )
    master_ctx = _master_auth()
    ws_root = tmp_path / "ws"
    ws_root.mkdir(parents=True)
    try:
        task = Task(
            task_id=new_task_id(),
            title="Vault Delivery Verification",
            description="Check Vault blob integrity and trash gating during Master delivery.",
            requested_by="operator.alice",
            created_at=t0,
            updated_at=t0,
        )
        store.tasks.create_task(task)
        run = Run(run_id=new_run_id(), task_id=task.task_id, runtime_id="custom.master_worker_v1")
        store.runs.create_run(run)

        # Register Vault-only artifact without creating a duplicate file under ws_root.
        vault_entry = vault.register_artifact(
            auth_context=op_ctx,
            task_id=task.task_id,
            run_id=run.run_id,
            owner_agent_id="it.artifact_worker",
            logical_key="deliverables/summary.txt",
            relative_path=f"workspace/{task.task_id}/summary.txt",
            content_bytes=b"VERIFIED_VAULT_CONTENT\n",
            verification_status="VERIFIED_PASS",
            evidence_reference="evd_qa_vault_01",
            timestamp=t0 + timedelta(seconds=1),
        )

        verified_delivery = deliv_svc.compile_master_delivery(
            MasterDeliveryCompileRequest(
                task_id=task.task_id,
                run_id=run.run_id,
                workspace_root=str(ws_root),
                artifact_ids=[vault_entry.artifact_id],
                summary="Verified Vault-backed deliverable ready.",
                auth_context=master_ctx,
                timestamp=t0 + timedelta(seconds=2),
            )
        )
        assert verified_delivery.delivery_verification_status == "VERIFIED"
        assert verified_delivery.qa_verified is True
        assert verified_delivery.artifacts[0].integrity_matched is True
        assert verified_delivery.artifacts[0].download_ref == f"/api/v1/artifacts/{vault_entry.artifact_id}/download"

        # Trashing the Vault artifact must force NOT_VERIFIED even though legacy artifacts table still has row.
        vault.trash_artifact(
            vault_entry.artifact_id,
            auth_context=op_ctx,
            reason="Superseded draft",
            timestamp=t0 + timedelta(seconds=3),
        )
        trashed_delivery = deliv_svc.compile_master_delivery(
            MasterDeliveryCompileRequest(
                task_id=task.task_id,
                run_id=run.run_id,
                workspace_root=str(ws_root),
                artifact_ids=[vault_entry.artifact_id],
                summary="Attempt delivery after trash.",
                auth_context=master_ctx,
                timestamp=t0 + timedelta(seconds=4),
            )
        )
        assert trashed_delivery.delivery_verification_status == "NOT_VERIFIED"
        assert trashed_delivery.qa_verified is False
        assert any("TRASHED" in b for b in trashed_delivery.verification_blockers)
    finally:
        store.close()


def test_b6_agent_allowlist_worker_sessions_and_department_validation_end_to_end(tmp_path: Path) -> None:
    """B6: ToolRuntime enforces tools.allowed, WorkerSession executes tools+vault, TeamBuilder validates depts."""
    env = _setup_environment(tmp_path)
    store: SQLiteRepositoryStore = env["store"]
    pm: PermissionManager = env["pm"]
    registry = env["registry"]
    ws_root: Path = env["ws_root"]
    op_auth = _operator_auth()
    master_auth = _master_auth()
    agents_root = REPO_ROOT / "agents"
    depts_root = agents_root / "departments"

    try:
        # Check real manifests on disk align cleanly with AgentRegistryService & Test contracts.
        master_manifest = load_agent_manifest_file(agents_root / "master" / "agent.yaml", allowed_root=agents_root)
        assert master_manifest.tools.allowed == []
        worker_manifest = load_agent_manifest_file(
            agents_root / "roles" / "it_artifact_worker" / "agent.yaml",
            allowed_root=agents_root,
        )
        assert worker_manifest.tools.allowed == []

        dept_map = load_department_manifests(depts_root)
        assert len(dept_map) == 9
        assert all(m.status == "ACTIVE" for m in dept_map.values())

        vault = ArtifactVaultService(
            store=store,
            vault_root=tmp_path / "vault",
            permission_manager=pm,
            journal=store.events,
        )
        tool_registry = ToolRegistry()
        register_phase07_tools(tool_registry)
        tool_runtime = ToolRuntime(
            registry=tool_registry,
            permission_manager=pm,
            journal=store.events,
            tool_repo=store.tools,
            agent_registry=registry,
        )

        # Register real default_antigravity connection referenced by it_artifact_worker manifest.
        env["eng_registry"].register_connection(
            EngineConnectionRef(
                connection_id="default_antigravity",
                provider_id="antigravity",
                adapter_id="antigravity_cli",
                credential_ref="env:ANTIGRAVITY_API_KEY",
                default_model_id="gemini-2.5-flash",
                enabled=True,
            )
        )

        # Register real it.artifact_worker baseline (tools.allowed=[]) cleanly without approval.
        reg_outcome = registry.register_agent(
            worker_manifest,
            auth_context=op_auth,
            timestamp=BASE_TIME + timedelta(seconds=1),
            source_path="agents/roles/it_artifact_worker/agent.yaml",
            validated_capabilities=worker_manifest.capabilities.skills,
        )

        probe_task = store.tasks.create_task(
            Task(
                task_id=new_task_id(),
                title="Allowlist Probe Task",
                description="Real persisted task for ToolRuntime allowlist verification.",
                requested_by="operator.local",
                status="QUEUED",
                created_at=BASE_TIME + timedelta(seconds=1),
            )
        ).task

        # Before granting tools via Human Approval Gate, ToolRuntime denies tools.files.list.
        pre_grant_deny = tool_runtime.invoke(
            ToolInvocationRequest(
                auth_context=TrustedAuthContext(
                    actor_id="it.artifact_worker",
                    actor_kind="worker_agent",
                    auth_source="application_boundary",
                    authenticated=True,
                    allowed_tools=[],
                    assigned_task_ids=[probe_task.task_id],
                ),
                tool_id="tools.files.list",
                parameters={"path": "."},
                task_id=probe_task.task_id,
                workspace_root=str(ws_root),
                timestamp=BASE_TIME + timedelta(seconds=2),
            )
        )
        assert pre_grant_deny.status == "DENIED"
        assert pre_grant_deny.error_code == "TOOL_NOT_IN_AGENT_ALLOWLIST"

        # Grant file tools to it.artifact_worker via Human Approval Gate (agent.change_permissions).
        granted_manifest = worker_manifest.model_copy(
            update={
                "tools": worker_manifest.tools.model_copy(
                    update={"allowed": ["tools.files.read", "tools.files.write", "tools.files.list"]}
                )
            }
        )
        diff = analyze_manifest_diff(reg_outcome.record.manifest, granted_manifest)
        upd_dec = pm.evaluator.evaluate(
            PolicyRequest(
                auth_context=op_auth,
                action=diff.required_governance_action,
                resource="agent:it.artifact_worker",
                parameters={
                    "target_agent_id": "it.artifact_worker",
                    "expected_record_version": reg_outcome.record.record_version,
                    "new_manifest_sha256": granted_manifest.canonical_sha256(),
                    "permission_escalation": diff.permission_escalation,
                    "department_changed": diff.department_changed,
                    "connection_or_provider_changed": diff.connection_or_provider_changed,
                    "limits_increased": diff.limits_increased,
                },
                timestamp=BASE_TIME + timedelta(seconds=2),
                correlation_id="corr-grant-it-worker-tools",
            )
        )
        upd_req = env["approvals"].create_approval_request(
            requester=master_auth,
            action=diff.required_governance_action,
            target_resource="agent:it.artifact_worker",
            task_id=None,
            run_id=None,
            request_fingerprint=upd_dec.request_fingerprint,
            policy_snapshot=ApprovalPolicyContext(
                matched_rule_ids=upd_dec.matched_rule_ids,
                rule_versions=upd_dec.rule_versions,
                reason_code=upd_dec.reason_code,
            ),
            risk_summary="Grant scoped file tools to it.artifact_worker",
            requested_at=BASE_TIME + timedelta(seconds=2),
        )
        env["approvals"].decide_approval(
            upd_req.approval_id,
            decider=op_auth,
            approved=True,
            reason="Verified scoped workspace file tools",
            timestamp=BASE_TIME + timedelta(seconds=2),
        )
        registry.update_agent(
            "it.artifact_worker",
            granted_manifest,
            expected_record_version=reg_outcome.record.record_version,
            auth_context=op_auth,
            timestamp=BASE_TIME + timedelta(seconds=2),
            change_summary="Grant scoped workspace file tools",
            approval_id=upd_req.approval_id,
        )

        # ToolRuntime enforces tools.allowed from AgentRegistryService (deny unlisted tool).
        denied_res = tool_runtime.invoke(
            ToolInvocationRequest(
                auth_context=TrustedAuthContext(
                    actor_id="it.artifact_worker",
                    actor_kind="worker_agent",
                    auth_source="application_boundary",
                    authenticated=True,
                    allowed_tools=[],
                    assigned_task_ids=[probe_task.task_id],
                ),
                tool_id="tools.code.git_inspect",
                parameters={"operation": "status"},
                task_id=probe_task.task_id,
                workspace_root=str(ws_root),
                timestamp=BASE_TIME + timedelta(seconds=2),
            )
        )
        assert denied_res.status == "DENIED"
        assert denied_res.error_code == "TOOL_NOT_IN_AGENT_ALLOWLIST"

        # Seed Task + Plan + Team via DynamicTeamBuilderService wired to real 9 department YAMLs.
        team_builder = DynamicTeamBuilderService(
            team_repo=store.teams,
            plan_repo=store.plans,
            agent_registry=registry,
            permission_manager=pm,
            allowed_providers={"antigravity"},
            allowed_models={"gemini-2.5-flash"},
            departments_root=depts_root,
        )
        env["team_builder"] = team_builder
        task = store.tasks.create_task(
            Task(
                task_id=new_task_id(),
                title="Integrated Worker Tool & Vault Session",
                description="Execute Worker session invoking governed tools and registering Vault artifact.",
                requested_by="operator.local",
                status="QUEUED",
                created_at=BASE_TIME + timedelta(seconds=3),
            )
        ).task
        task_ws = ws_root / "workspace" / task.task_id / "outputs"
        task_ws.mkdir(parents=True, exist_ok=True)
        task_ws_root = ws_root / "workspace" / task.task_id

        from core.orchestration import PlanStepSpec, StructuredUserBrief
        from core.orchestration.planning_models import PlanRoutingProposal

        plan_out = env["planner"].create_plan(
            brief=StructuredUserBrief(
                task_id=task.task_id,
                title=task.title,
                objective=task.description,
                requested_by="operator.local",
                required_outputs=[f"workspace/{task.task_id}/outputs/final.txt"],
                acceptance_criteria=["Tool write/read and Vault registration succeed cleanly."],
                preferred_engine_id="antigravity",
                preferred_provider_id="antigravity",
                preferred_connection_id="ag_shared",
                preferred_model_id="gemini-2.5-flash",
            ),
            steps=[
                PlanStepSpec(
                    step_id="step_01",
                    order=0,
                    title="Write and register artifact",
                    description="Invoke file tools and Vault inside active worker session.",
                    required_capabilities=["workspace_artifact_authoring"],
                    required_tools=["tools.files.write", "tools.files.read"],
                    expected_outputs=[f"workspace/{task.task_id}/outputs/final.txt"],
                    allowed_read_paths=[f"workspace/{task.task_id}"],
                    allowed_write_paths=[f"workspace/{task.task_id}/outputs"],
                    acceptance_criteria=["Verified deliverable created."],
                    routing_proposal=PlanRoutingProposal(
                        engine_id="antigravity",
                        provider_id="antigravity",
                        connection_id="default_antigravity",
                        model_id="gemini-2.5-flash",
                        data_residency_policy="cloud_allowed",
                        routing_reason="Isolated sandbox execution.",
                    ),
                )
            ],
            auth_context=master_auth,
            timestamp=BASE_TIME + timedelta(seconds=4),
        )
        assert plan_out.stored_plan is not None
        applied_plan, _ = env["planner"].apply_plan_to_task(
            plan_out.stored_plan.plan_id,
            expected_plan_record_version=plan_out.stored_plan.record_version,
            auth_context=master_auth,
            timestamp=BASE_TIME + timedelta(seconds=5),
        )
        prop = team_builder.build_team_proposal(
            TeamBuildRequest(task_id=task.task_id, plan_id=applied_plan.plan_id),
            auth_context=master_auth,
            timestamp=BASE_TIME + timedelta(seconds=6),
        )
        assert prop.status == "READY_FOR_ASSIGNMENT"
        assert prop.proposal.step_assignments[0].status == "ASSIGNED_IN_PROPOSAL"

        # Verify INACTIVE department blocks candidate selection when injected.
        inactive_depts = dict(dept_map)
        inactive_depts["it"] = dept_map["it"].model_copy(update={"status": "INACTIVE"})
        blocked_builder = DynamicTeamBuilderService(
            team_repo=store.teams,
            plan_repo=store.plans,
            agent_registry=registry,
            permission_manager=pm,
            allowed_providers={"antigravity"},
            allowed_models={"gemini-2.5-flash"},
            department_manifests=inactive_depts,
        )
        blocked_prop = blocked_builder.build_team_proposal(
            TeamBuildRequest(task_id=task.task_id, plan_id=applied_plan.plan_id),
            auth_context=master_auth,
            timestamp=BASE_TIME + timedelta(seconds=7),
        )
        assert blocked_prop.status == "BLOCKED_GOVERNANCE"
        cand_eval = next(c for c in blocked_prop.proposal.candidate_evaluations if c.agent_id == "it.artifact_worker")
        assert "DEPARTMENT_INACTIVE_OR_UNKNOWN:it" in cand_eval.exclusion_reasons

        assign_res = team_builder.apply_team_assignment(
            prop.proposal_id,
            expected_proposal_record_version=prop.record_version,
            auth_context=master_auth,
            timestamp=BASE_TIME + timedelta(seconds=8),
        )

        # Wire WorkerSessionLifecycleService with ToolRuntime & ArtifactVaultService.
        observed_handles: dict[str, object] = {}

        def _session_exec(req, conn, on_event):
            sess_id = req.session_reference.session_id
            w_res = env["worker_service"].invoke_session_tool(
                sess_id,
                tool_id="tools.files.write",
                parameters={"path": "outputs/final.txt", "content": "SESSION_TOOL_PAYLOAD"},
                workspace_root=str(task_ws_root),
                timestamp=BASE_TIME + timedelta(seconds=11),
            )
            r_res = env["worker_service"].invoke_session_tool(
                sess_id,
                tool_id="tools.files.read",
                parameters={"path": "outputs/final.txt"},
                workspace_root=str(task_ws_root),
                timestamp=BASE_TIME + timedelta(seconds=12),
            )
            b_res = env["worker_service"].invoke_session_tool(
                sess_id,
                tool_id="tools.code.git_inspect",
                parameters={"operation": "status"},
                workspace_root=str(task_ws_root),
                timestamp=BASE_TIME + timedelta(seconds=13),
            )
            art_entry = env["worker_service"].register_session_artifact(
                sess_id,
                logical_key="worker/final.txt",
                relative_path="outputs/final.txt",
                source_path=task_ws_root / "outputs" / "final.txt",
                timestamp=BASE_TIME + timedelta(seconds=14),
            )
            observed_handles["write_status"] = w_res.status
            observed_handles["read_content"] = r_res.data.get("content")
            observed_handles["blocked_code"] = b_res.error_code
            observed_handles["artifact_id"] = art_entry.artifact_id
            now_exec = BASE_TIME + timedelta(seconds=15)
            return EngineResult(
                request_id=req.request_id,
                provider_id="antigravity",
                adapter_id="antigravity_cli",
                connection_id=req.connection_id,
                model_id=req.model_id,
                platform_kind="agent_execution_platform",
                status="SUCCEEDED",
                business_verification_status="UNVERIFIED",
                response_text="Completed via governed tools & vault.",
                session_reference=EngineSessionReference(
                    session_id=sess_id,
                    provider_conversation_id=f"conv_sync_{req.request_id[-6:]}",
                    binding_source="adapter_logical",
                ),
                started_at=now_exec,
                finished_at=now_exec + timedelta(seconds=1),
                duration_ms=1000,
                telemetry=EngineTelemetry(
                    duration_ms=1000,
                    telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                    telemetry_source="unavailable",
                ),
            )

        env["adapter"]._handler = _session_exec
        env["worker_service"]._tool_runtime = tool_runtime
        env["worker_service"]._artifact_vault = vault

        outcome = env["worker_service"].execute_worker_step(
            WorkerExecutionRequest(
                task_id=task.task_id,
                plan_id=applied_plan.plan_id,
                team_proposal_id=assign_res.stored_proposal.proposal_id,
                step_id="step_01",
                agent_id="it.artifact_worker",
                actor_id="core.master",
                workspace_root=str(ws_root),
                workspace_scope=f"workspace/{task.task_id}/outputs",
                additional_context="Execute governed tool & vault workflow.",
                auth_context=master_auth,
                timestamp=BASE_TIME + timedelta(seconds=10),
            )
        )
        assert outcome.status == "CLOSED", (
            outcome.error_code,
            outcome.error_message,
            [(c.check_name, c.code, c.message) for c in outcome.preflight.checks if not c.passed],
        )
        assert observed_handles["write_status"] == "SUCCESS"
        assert observed_handles["read_content"] == "SESSION_TOOL_PAYLOAD"
        assert observed_handles["blocked_code"] == "TOOL_NOT_IN_AGENT_ALLOWLIST"

        replay = replay_task_history(store.events.read_entries(task_id=task.task_id), task_id=task.task_id)
        assert "tools.files.write" in replay.observed_tool_names
        assert "tools.files.read" in replay.observed_tool_names
        assert observed_handles["artifact_id"] in replay.artifact_ids
        assert observed_handles["artifact_id"] in store.tasks.get_task(task.task_id).artifact_ids
    finally:
        store.close()
