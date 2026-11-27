"""Fail-closed unit & SQLite persistence tests for P07.06 Tool Registry, 10-step Runtime, and Usage Summary."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
from typing import Any

from pydantic import ValidationError
import pytest

from core.contracts import new_run_id, new_task_id
from core.governance import (
    ApprovalService,
    PermissionManager,
    PolicyEvaluator,
    TrustedAuthContext,
    build_default_policy_rules,
    compute_request_fingerprint,
)
from core.tools import (
    ToolCancelledError,
    ToolCostProfile,
    ToolExecutionContext,
    ToolExecutionError,
    ToolHandlerOutput,
    ToolInvocationRequest,
    ToolManifest,
    ToolRegistry,
    ToolRuntime,
)
from storage.db import apply_migrations, sqlite_connection
from storage.repositories import SQLiteApprovalRepository, SQLiteRepositoryStore

REPO_ROOT = Path(__file__).resolve().parents[2]
FAKE_OPENAI_SECRET = "sk-" + "FakeOpenAISecret1234567890ABCD"


def _utc(minute: int = 0) -> datetime:
    return datetime(2026, 11, 27, 12, minute, 0, tzinfo=timezone.utc)


def _worker_ctx(
    *,
    allowed_tools: list[str] | None = None,
    allowed_actions: list[str] | None = None,
) -> TrustedAuthContext:
    return TrustedAuthContext(
        actor_id="it.artifact_worker",
        actor_kind="worker_agent",
        auth_source="application_boundary",
        authenticated=True,
        allowed_tools=allowed_tools if allowed_tools is not None else [],
        allowed_actions=allowed_actions if allowed_actions is not None else [],
    )


def _operator_ctx(actor_id: str = "operator.alice") -> TrustedAuthContext:
    return TrustedAuthContext(
        actor_id=actor_id,
        actor_kind="human_operator",
        auth_source="operator_session",
        authenticated=True,
    )


def _sample_manifest(
    tool_id: str = "tools.files.echo_probe",
    *,
    approval_required: bool = False,
    permissions_required: list[str] | None = None,
    safe_retry: bool = False,
    estimated_cost: float | None = 0.0,
) -> ToolManifest:
    return ToolManifest(
        id=tool_id,
        name="Echo Probe Tool",
        version="1.0.0",
        category="files",
        description="Test manifest exercising governed invocation lifecycle.",
        capabilities=["workspace_inspection"],
        input_schema={
            "type": "object",
            "required": ["path"],
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "note": {"type": "string"},
            },
        },
        output_schema={
            "type": "object",
            "required": ["echo"],
            "additionalProperties": False,
            "properties": {"echo": {"type": "string"}},
        },
        permissions_required=permissions_required or ["files.read"],
        approval_required=approval_required,
        risk_level="HIGH" if approval_required else "LOW",
        execution_mode="restricted_local",
        timeout_seconds=10,
        cost_profile=ToolCostProfile(unit="test_call", estimated_unit_cost_usd=estimated_cost),
        safe_retry=safe_retry,
    )


def _setup_runtime(
    tmp_path: Path,
    registry: ToolRegistry | None = None,
    *,
    store: SQLiteRepositoryStore | None = None,
) -> tuple[SQLiteRepositoryStore, ToolRegistry, ToolRuntime, ApprovalService, str, str]:
    db_file = tmp_path / "tools_runtime.db"
    if store is None:
        with sqlite_connection(db_file) as conn:
            apply_migrations(conn)
        store = SQLiteRepositoryStore(db_file)
    task_id = new_task_id()
    run_id = new_run_id()
    ts = _utc()
    from core.contracts import Run, Task

    store.tasks.create_task(
        Task(
            task_id=task_id,
            title="Tool Runtime Acceptance",
            description="Exercise Phase 07 tools",
            requested_by="operator.alice",
            created_at=ts,
            updated_at=ts,
        )
    )
    store.runs.create_run(Run(run_id=run_id, task_id=task_id, runtime_id="custom.master_worker_v1"))

    reg = registry or ToolRegistry(repository=store.tools, journal=store.events)
    approval_svc = ApprovalService(SQLiteApprovalRepository(store), store.events)
    evaluator = PolicyEvaluator(
        rules=build_default_policy_rules(),
        registered_tools=reg.registered_tool_ids(),
    )
    perm_mgr = PermissionManager(evaluator=evaluator, journal=store.events, approval_service=approval_svc)
    runtime = ToolRuntime(
        registry=reg,
        permission_manager=perm_mgr,
        journal=store.events,
        tool_repo=store.tools,
    )
    return store, reg, runtime, approval_svc, task_id, run_id


def test_core_tools_ast_architecture_purity() -> None:
    """Ensure core/tools/ stays pure without importing storage, subprocess, network, or UI."""
    forbidden_prefixes = (
        "sqlite3",
        "storage",
        "subprocess",
        "httpx",
        "playwright",
        "ddgs",
        "markitdown",
        "api",
        "ui",
    )
    for py_path in sorted((REPO_ROOT / "core" / "tools").glob("*.py")):
        tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(forbidden_prefixes)
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(forbidden_prefixes)


def test_manifest_validation_rejects_bad_ids_mismatch_and_secrets() -> None:
    """ToolManifest rejects malformed Tool ID, category mismatch, bad schema, and secrets."""
    with pytest.raises(ValidationError):
        _sample_manifest(tool_id="invalid_id")

    with pytest.raises(ValidationError):
        ToolManifest(
            id="tools.code.mismatch",
            name="Mismatch",
            version="1.0.0",
            category="files",
            description="Mismatched category.",
            capabilities=["workspace_inspection"],
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            permissions_required=["files.read"],
            risk_level="LOW",
            execution_mode="restricted_local",
            cost_profile=ToolCostProfile(unit="free_local", estimated_unit_cost_usd=0.0),
        )

    with pytest.raises(ValidationError):
        ToolManifest(
            id="tools.files.bad_desc",
            name="Bad Secret",
            version="1.0.0",
            category="files",
            description=f"Uses {FAKE_OPENAI_SECRET} directly",
            capabilities=["workspace_inspection"],
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            permissions_required=["files.read"],
            risk_level="LOW",
            execution_mode="restricted_local",
            cost_profile=ToolCostProfile(unit="free_local"),
        )


def test_runtime_lifecycle_permissions_schemas_disabled_and_untrusted_actor(tmp_path: Path) -> None:
    """Cover unregistered, DISABLED, schema mismatch, missing allowlist, and agent-claimed operator DENY."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    reg = ToolRegistry()
    handler_calls = 0

    def _echo(params: dict[str, Any], _ctx: ToolExecutionContext) -> ToolHandlerOutput:
        nonlocal handler_calls
        handler_calls += 1
        return ToolHandlerOutput(data={"echo": str(params["path"])})

    store, reg, runtime, _, task_id, run_id = _setup_runtime(tmp_path, reg)
    try:
        # Persist tool in store repository too
        reg._repo = store.tools
        reg._journal = store.events
        manifest = _sample_manifest()
        reg.register(manifest, _echo, registered_at=_utc())
        # Refresh evaluator registered_tools set
        runtime._permissions._evaluator._registered_tools = reg.registered_tool_ids()

        # 1. Unregistered tool -> DENIED & recorded cleanly
        unreg = runtime.invoke(
            ToolInvocationRequest(
                auth_context=_worker_ctx(allowed_tools=["tools.files.missing"]),
                tool_id="tools.files.missing",
                parameters={},
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(1),
            )
        )
        assert unreg.status == "DENIED"
        assert unreg.error_code == "TOOL_NOT_FOUND"
        assert unreg.business_verification_status == "UNVERIFIED"

        # 2. Invalid input_schema -> FAILED before handler runs
        bad_input = runtime.invoke(
            ToolInvocationRequest(
                auth_context=_worker_ctx(allowed_tools=[manifest.id]),
                tool_id=manifest.id,
                parameters={"unexpected": 123},
                workspace_root=str(workspace),
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(2),
            )
        )
        assert bad_input.status == "FAILED"
        assert bad_input.error_code == "INVALID_INPUT_SCHEMA"
        assert handler_calls == 0

        # 3. Missing from agent.tools.allowed -> DENIED
        not_allowed = runtime.invoke(
            ToolInvocationRequest(
                auth_context=_worker_ctx(allowed_tools=[]),
                tool_id=manifest.id,
                parameters={"path": "notes.txt"},
                workspace_root=str(workspace),
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(3),
            )
        )
        assert not_allowed.status == "DENIED"
        assert not_allowed.error_code == "TOOL_NOT_ALLOWED"

        # 4. Agent-claimed operator context rejected at TrustedAuthContext boundary
        with pytest.raises(ValidationError):
            TrustedAuthContext(
                actor_id="operator.spoofed",
                actor_kind="human_operator",
                auth_source="agent_claimed",
                authenticated=True,
            )

        # 5. DISABLED tool denied even when allowlisted
        reg.set_status(manifest.id, "DISABLED", updated_at=_utc(4))
        disabled_res = runtime.invoke(
            ToolInvocationRequest(
                auth_context=_worker_ctx(allowed_tools=[manifest.id]),
                tool_id=manifest.id,
                parameters={"path": "notes.txt"},
                workspace_root=str(workspace),
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(5),
            )
        )
        assert disabled_res.status == "DENIED"
        assert disabled_res.error_code == "TOOL_UNAVAILABLE"
        assert handler_calls == 0

        # Re-enable and verify success always leaves business_verification_status == UNVERIFIED
        reg.set_status(manifest.id, "AVAILABLE", updated_at=_utc(6))
        ok_res = runtime.invoke(
            ToolInvocationRequest(
                auth_context=_worker_ctx(allowed_tools=[manifest.id]),
                tool_id=manifest.id,
                parameters={"path": "notes.txt"},
                workspace_root=str(workspace),
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(7),
            )
        )
        assert ok_res.status == "SUCCESS"
        assert ok_res.data == {"echo": "notes.txt"}
        assert ok_res.business_verification_status == "UNVERIFIED"
        assert handler_calls == 1
    finally:
        store.close()


def test_approval_gate_single_use_and_replay_protection(tmp_path: Path) -> None:
    """Tools requiring human approval pause at APPROVAL_REQUIRED, execute once, then reject replay."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    exec_count = 0

    def _sensitive(params: dict[str, Any], ctx: ToolExecutionContext) -> ToolHandlerOutput:
        nonlocal exec_count
        exec_count += 1
        assert ctx.approved_by_operator is True
        return ToolHandlerOutput(data={"echo": str(params["path"])})

    store, reg, runtime, approval_svc, task_id, run_id = _setup_runtime(tmp_path)
    try:
        manifest = _sample_manifest(
            tool_id="tools.files.sensitive_probe",
            approval_required=True,
            permissions_required=["files.read"],
        )
        reg.register(manifest, _sensitive, registered_at=_utc())
        runtime._permissions._evaluator._registered_tools = reg.registered_tool_ids()
        worker = _worker_ctx(allowed_tools=[manifest.id])

        first = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id=manifest.id,
                parameters={"path": "item.txt"},
                workspace_root=str(workspace),
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(1),
            )
        )
        assert first.status == "APPROVAL_REQUIRED"
        assert first.error_code == "APPROVAL_REQUIRED"
        assert exec_count == 0

        from core.governance import ApprovalPolicyContext

        apr = approval_svc.create_approval_request(
            requester=worker,
            action=first.data["approval_action"],
            target_resource=first.data["resource"],
            task_id=task_id,
            run_id=run_id,
            request_fingerprint=first.data["request_fingerprint"],
            policy_snapshot=ApprovalPolicyContext(
                matched_rule_ids=["test"],
                rule_versions={"test": "1.0.0"},
                reason_code="APPROVAL_REQUIRED",
            ),
            risk_summary="Approve sensitive tool invocation",
            requested_at=_utc(2),
            expires_at=_utc(30),
        )
        approval_svc.decide_approval(
            apr.approval_id,
            decider=_operator_ctx(),
            approved=True,
            reason="Verified scope",
            timestamp=_utc(3),
        )

        executed = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id=manifest.id,
                parameters={"path": "item.txt"},
                workspace_root=str(workspace),
                task_id=task_id,
                run_id=run_id,
                approval_id=apr.approval_id,
                timestamp=_utc(4),
            )
        )
        assert executed.status == "SUCCESS"
        assert executed.approval_id == apr.approval_id
        assert exec_count == 1

        replay = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id=manifest.id,
                parameters={"path": "item.txt"},
                workspace_root=str(workspace),
                task_id=task_id,
                run_id=run_id,
                approval_id=apr.approval_id,
                timestamp=_utc(5),
            )
        )
        assert replay.status == "DENIED"
        assert replay.error_code == "APPROVAL_ALREADY_CONSUMED"
        assert exec_count == 1
    finally:
        store.close()


def test_secret_redaction_immutability_timeout_output_invalid_and_usage_summary(tmp_path: Path) -> None:
    """Verify SQLite redaction/immutability, timeout/output errors, and null-safe ToolUsageSummary."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store, reg, runtime, _, task_id, run_id = _setup_runtime(tmp_path)
    try:
        # Tool 1: leaks secret in output + error, plus a known free cost
        m_redact = _sample_manifest("tools.files.secret_probe", estimated_cost=0.0)

        def _leaky(params: dict[str, Any], _ctx: ToolExecutionContext) -> ToolHandlerOutput:
            mode = params.get("note", "ok")
            if mode == "timeout":
                raise TimeoutError(f"Timed out with token={FAKE_OPENAI_SECRET}")
            if mode == "bad_output":
                return ToolHandlerOutput(data={"wrong_key": "value"})
            return ToolHandlerOutput(data={"echo": f"key={FAKE_OPENAI_SECRET}"})

        # Tool 2: unknown cost (None)
        m_unknown_cost = _sample_manifest("tools.files.unknown_cost_probe", estimated_cost=None)

        def _ok_unknown(_params: dict[str, Any], _ctx: ToolExecutionContext) -> ToolHandlerOutput:
            return ToolHandlerOutput(data={"echo": "clean"}, estimated_cost_usd=None)

        reg.register(m_redact, _leaky, registered_at=_utc())
        reg.register(m_unknown_cost, _ok_unknown, registered_at=_utc())
        runtime._permissions._evaluator._registered_tools = reg.registered_tool_ids()
        worker = _worker_ctx(allowed_tools=[m_redact.id, m_unknown_cost.id])

        r1 = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id=m_redact.id,
                parameters={"path": "a.txt", "note": f"api_key={FAKE_OPENAI_SECRET}"},
                workspace_root=str(workspace),
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(1),
            )
        )
        assert r1.status == "SUCCESS"

        r2 = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id=m_redact.id,
                parameters={"path": "a.txt", "note": "timeout"},
                workspace_root=str(workspace),
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(2),
            )
        )
        assert r2.status == "TIMEOUT"
        assert r2.error_code == "TIMED_OUT"
        assert FAKE_OPENAI_SECRET not in (r2.error_message or "")

        r3 = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id=m_redact.id,
                parameters={"path": "a.txt", "note": "bad_output"},
                workspace_root=str(workspace),
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(3),
            )
        )
        assert r3.status == "FAILED"
        assert r3.error_code == "OUTPUT_INVALID"
        assert r3.data == {}

        r4 = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id=m_unknown_cost.id,
                parameters={"path": "b.txt"},
                workspace_root=str(workspace),
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(4),
            )
        )
        assert r4.status == "SUCCESS"
        assert r4.estimated_cost_usd is None

        # Verify zero secret leakage in SQLite tool_executions and events
        with store.open_connection() as conn:
            rows = conn.execute("SELECT * FROM tool_executions;").fetchall()
            assert len(rows) == 4
            for row in rows:
                assert FAKE_OPENAI_SECRET not in str(row["redacted_input_json"])
                assert FAKE_OPENAI_SECRET not in str(row["redacted_output_json"])
                assert FAKE_OPENAI_SECRET not in str(row["error_message"] or "")
                assert row["business_verification_status"] == "UNVERIFIED"

            # Verify immutable update/delete triggers
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE tool_executions SET status = 'DENIED';")
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM tool_executions;")

        for entry in store.events.read_entries(task_id=task_id):
            assert FAKE_OPENAI_SECRET not in entry.event.model_dump_json()

        # Verify ToolUsageSummary per-tool and null-safe cost aggregation
        summary_all = runtime.summarize_usage(task_id=task_id, run_id=run_id)
        assert summary_all.total_calls == 4
        assert summary_all.by_status == {"FAILED": 1, "SUCCESS": 2, "TIMEOUT": 1}
        assert summary_all.known_cost_usd == 0.0
        assert summary_all.unknown_cost_calls == 3

        summary_unknown_only = runtime.summarize_usage(actor_id="it.artifact_worker")
        by_tool = {b.tool_id: b for b in summary_unknown_only.by_tool}
        assert by_tool[m_unknown_cost.id].known_cost_usd is None
        assert by_tool[m_unknown_cost.id].unknown_cost_calls == 1
    finally:
        store.close()


def test_rejected_expired_tampered_approval_cancellation_and_no_blind_retry(tmp_path: Path) -> None:
    """Cover TOOL_SPEC scenarios 5 (rejected/expired/tampered approval), 7 (cancelled), and 12 (safe_retry=False)."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    exec_count = 0

    def _probe(params: dict[str, Any], _ctx: ToolExecutionContext) -> ToolHandlerOutput:
        nonlocal exec_count
        exec_count += 1
        note = params.get("note", "")
        if note == "cancel_mid":
            raise ToolCancelledError("Worker session cancelled mid-step.")
        if note == "partial_fail":
            raise ToolExecutionError("PARTIAL_EXECUTION", "Write interrupted half-way.")
        return ToolHandlerOutput(data={"echo": str(params["path"])})

    store, reg, runtime, approval_svc, task_id, run_id = _setup_runtime(tmp_path)
    try:
        m_sens = _sample_manifest(
            "tools.files.cancel_approval_probe",
            approval_required=True,
            safe_retry=False,
        )
        m_plain = _sample_manifest("tools.files.cancel_plain_probe", safe_retry=False)
        reg.register(m_sens, _probe, registered_at=_utc())
        reg.register(m_plain, _probe, registered_at=_utc())
        runtime._permissions._evaluator._registered_tools = reg.registered_tool_ids()
        worker = _worker_ctx(allowed_tools=[m_sens.id, m_plain.id])
        approver = _operator_ctx()

        from core.governance import ApprovalError, ApprovalPolicyContext

        pending = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id=m_sens.id,
                parameters={"path": "item.txt"},
                workspace_root=str(workspace),
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(1),
            )
        )
        assert pending.status == "APPROVAL_REQUIRED"
        snap = ApprovalPolicyContext(
            matched_rule_ids=["test"],
            rule_versions={"test": "1.0.0"},
            reason_code="APPROVAL_REQUIRED",
        )

        # Scenario 5a: Rejected approval never executes handler
        apr_rej = approval_svc.create_approval_request(
            requester=worker,
            action=pending.data["approval_action"],
            target_resource=pending.data["resource"],
            task_id=task_id,
            run_id=run_id,
            request_fingerprint=pending.data["request_fingerprint"],
            policy_snapshot=snap,
            risk_summary="Test reject",
            requested_at=_utc(2),
            expires_at=_utc(30),
        )
        approval_svc.decide_approval(
            apr_rej.approval_id,
            decider=approver,
            approved=False,
            reason="Operator denied",
            timestamp=_utc(3),
        )
        res_rej = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id=m_sens.id,
                parameters={"path": "item.txt"},
                workspace_root=str(workspace),
                task_id=task_id,
                run_id=run_id,
                approval_id=apr_rej.approval_id,
                timestamp=_utc(4),
            )
        )
        assert res_rej.status == "DENIED"
        assert res_rej.error_code == "APPROVAL_REJECTED"

        # Scenario 5b: Expired approval blocked fail-closed
        apr_exp = approval_svc.create_approval_request(
            requester=worker,
            action=pending.data["approval_action"],
            target_resource=pending.data["resource"],
            task_id=task_id,
            run_id=run_id,
            request_fingerprint=pending.data["request_fingerprint"],
            policy_snapshot=snap,
            risk_summary="Test expire",
            requested_at=_utc(2),
            expires_at=_utc(10),
        )
        with pytest.raises(ApprovalError) as exp_err:
            approval_svc.decide_approval(
                apr_exp.approval_id,
                decider=approver,
                approved=True,
                reason="Too late",
                timestamp=_utc(15),
            )
        assert exp_err.value.error_code == "APPROVAL_EXPIRED"

        # Scenario 5c: Parameter tampering after approval rejected by fingerprint guard
        apr_ok = approval_svc.create_approval_request(
            requester=worker,
            action=pending.data["approval_action"],
            target_resource=pending.data["resource"],
            task_id=task_id,
            run_id=run_id,
            request_fingerprint=pending.data["request_fingerprint"],
            policy_snapshot=snap,
            risk_summary="Test tamper",
            requested_at=_utc(2),
            expires_at=_utc(40),
        )
        approval_svc.decide_approval(
            apr_ok.approval_id,
            decider=approver,
            approved=True,
            reason="Only item.txt approved",
            timestamp=_utc(3),
        )
        res_tamper = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id=m_sens.id,
                parameters={"path": "item.txt", "note": "tampered"},
                workspace_root=str(workspace),
                task_id=task_id,
                run_id=run_id,
                approval_id=apr_ok.approval_id,
                timestamp=_utc(5),
            )
        )
        assert res_tamper.status == "DENIED"
        assert res_tamper.error_code == "APPROVAL_FINGERPRINT_MISMATCH"
        assert exec_count == 0

        # Scenario 7a: Pre-launch cooperative cancellation
        res_pre_cancel = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id=m_plain.id,
                parameters={"path": "item.txt"},
                workspace_root=str(workspace),
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(6),
            ),
            is_cancelled=lambda: True,
        )
        assert res_pre_cancel.status == "CANCELLED"
        assert res_pre_cancel.error_code == "CANCELLED"
        assert exec_count == 0

        # Scenario 7b: Mid-execution cancellation
        res_mid_cancel = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id=m_plain.id,
                parameters={"path": "item.txt", "note": "cancel_mid"},
                workspace_root=str(workspace),
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(7),
            )
        )
        assert res_mid_cancel.status == "CANCELLED"
        assert res_mid_cancel.error_code == "CANCELLED"
        assert exec_count == 1

        # Scenario 12: Non-idempotent failure executes exactly once with no retry
        res_partial = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id=m_plain.id,
                parameters={"path": "item.txt", "note": "partial_fail"},
                workspace_root=str(workspace),
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(8),
            )
        )
        assert res_partial.status == "FAILED"
        assert res_partial.error_code == "PARTIAL_EXECUTION"
        assert exec_count == 2
        partial_rows = store.tools.list_executions(
            task_id=task_id,
            run_id=run_id,
            tool_id=m_plain.id,
            status="FAILED",
        )
        assert len(partial_rows) == 1
        cancelled_rows = store.tools.list_executions(
            task_id=task_id,
            run_id=run_id,
            tool_id=m_plain.id,
            status="CANCELLED",
        )
        assert len(cancelled_rows) == 2
    finally:
        store.close()
