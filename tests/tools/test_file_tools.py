"""Fail-closed tests for P07.01 workspace file tools (`tools.files.*`)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from core.contracts import Run, Task, new_run_id, new_task_id
from core.governance import (
    ApprovalPolicyContext,
    ApprovalService,
    PermissionManager,
    PolicyEvaluator,
    TrustedAuthContext,
    build_default_policy_rules,
)
from core.tools import ToolInvocationRequest, ToolRegistry, ToolRuntime
from storage.db import apply_migrations, sqlite_connection
from storage.repositories import SQLiteApprovalRepository, SQLiteRepositoryStore
from tools.files import build_file_tools, sha256_bytes


def _utc(minute: int = 0) -> datetime:
    return datetime(2026, 11, 27, 13, minute, 0, tzinfo=timezone.utc)


def _setup(tmp_path: Path) -> tuple[SQLiteRepositoryStore, ToolRuntime, ApprovalService, Path, str, str]:
    db_file = tmp_path / "files.db"
    with sqlite_connection(db_file) as conn:
        apply_migrations(conn)
    store = SQLiteRepositoryStore(db_file)
    task_id = new_task_id()
    run_id = new_run_id()
    ts = _utc()
    store.tasks.create_task(
        Task(
            task_id=task_id,
            title="File Tool Suite",
            description="Test P07.01 boundaries",
            requested_by="operator.alice",
            created_at=ts,
            updated_at=ts,
        )
    )
    store.runs.create_run(Run(run_id=run_id, task_id=task_id, runtime_id="custom.master_worker_v1"))

    reg = ToolRegistry(repository=store.tools, journal=store.events)
    for manifest, handler in build_file_tools():
        reg.register(manifest, handler, registered_at=ts)

    approval_svc = ApprovalService(SQLiteApprovalRepository(store), store.events)
    perm_mgr = PermissionManager(
        evaluator=PolicyEvaluator(
            rules=build_default_policy_rules(),
            registered_tools=reg.registered_tool_ids(),
        ),
        journal=store.events,
        approval_service=approval_svc,
    )
    runtime = ToolRuntime(
        registry=reg,
        permission_manager=perm_mgr,
        journal=store.events,
        tool_repo=store.tools,
    )
    workspace = tmp_path / "workspace" / task_id
    workspace.mkdir(parents=True)
    return store, runtime, approval_svc, workspace, task_id, run_id


def test_file_write_read_list_overwrite_protection_and_sha256(tmp_path: Path) -> None:
    """Verify atomic write, SHA-256 checksums, overwrite block, and filtered directory listing."""
    store, runtime, _, workspace, task_id, run_id = _setup(tmp_path)
    try:
        worker = TrustedAuthContext(
            actor_id="it.artifact_worker",
            actor_kind="worker_agent",
            auth_source="application_boundary",
            authenticated=True,
            allowed_tools=["tools.files.write", "tools.files.read", "tools.files.list"],
        )

        # Write new file
        res_write = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id="tools.files.write",
                parameters={"path": "docs/report.txt", "content": "hello ai village"},
                workspace_root=str(workspace),
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(1),
            )
        )
        assert res_write.status == "SUCCESS"
        expected_sha = sha256_bytes(b"hello ai village")
        assert res_write.data["sha256"] == expected_sha
        assert res_write.data["overwritten"] is False
        assert res_write.artifacts[0]["sha256"] == expected_sha

        # Accidental overwrite blocked when overwrite omitted or False
        res_dup = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id="tools.files.write",
                parameters={"path": "docs/report.txt", "content": "replaced"},
                workspace_root=str(workspace),
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(2),
            )
        )
        assert res_dup.status == "FAILED"
        assert res_dup.error_code == "FILE_EXISTS_OVERWRITE_DENIED"
        assert (workspace / "docs" / "report.txt").read_text(encoding="utf-8") == "hello ai village"

        # Explicit overwrite=True updates file cleanly
        res_over = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id="tools.files.write",
                parameters={"path": "docs/report.txt", "content": "v2", "overwrite": True},
                workspace_root=str(workspace),
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(3),
            )
        )
        assert res_over.status == "SUCCESS"
        assert res_over.data["overwritten"] is True

        # Read returns exact updated content and SHA-256
        res_read = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id="tools.files.read",
                parameters={"path": "docs/report.txt"},
                workspace_root=str(workspace),
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(4),
            )
        )
        assert res_read.status == "SUCCESS"
        assert res_read.data["content"] == "v2"
        assert res_read.data["sha256"] == sha256_bytes(b"v2")

        # Sensitive .env file placed manually must never be listed
        (workspace / ".env").write_text("SECRET=123", encoding="utf-8")
        res_list = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id="tools.files.list",
                parameters={"path": ".", "recursive": True},
                workspace_root=str(workspace),
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(5),
            )
        )
        assert res_list.status == "SUCCESS"
        paths = [e["path"] for e in res_list.data["entries"]]
        assert "docs/report.txt" in paths
        assert ".env" not in paths
    finally:
        store.close()


def test_file_tools_fail_closed_boundaries_and_delete_approval(tmp_path: Path) -> None:
    """Reject traversal, secret files, cross-task leaks, missing workspace, and enforce approval on delete."""
    store, runtime, approval_svc, workspace, task_id, run_id = _setup(tmp_path)
    other_ws = tmp_path / "workspace" / "task_other"
    other_ws.mkdir(parents=True)
    (other_ws / "other.txt").write_text("classified", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("outside", encoding="utf-8")

    try:
        worker = TrustedAuthContext(
            actor_id="it.artifact_worker",
            actor_kind="worker_agent",
            auth_source="application_boundary",
            authenticated=True,
            allowed_tools=["tools.files.read", "tools.files.write", "tools.files.delete"],
        )

        # Path traversal blocked
        trav = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id="tools.files.read",
                parameters={"path": "../../outside.txt"},
                workspace_root=str(workspace),
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(1),
            )
        )
        assert trav.status == "DENIED"
        assert trav.error_code == "PATH_TRAVERSAL_DENIED"

        # Secret file read blocked
        (workspace / ".env").write_text("TOKEN=1", encoding="utf-8")
        sec = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id="tools.files.read",
                parameters={"path": ".env"},
                workspace_root=str(workspace),
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(2),
            )
        )
        assert sec.status == "DENIED"
        assert sec.error_code == "SECRET_FILE_ACCESS_DENIED"

        # Cross-task isolation blocked even with absolute target
        cross = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id="tools.files.read",
                parameters={"path": str(other_ws / "other.txt")},
                workspace_root=str(workspace),
                cross_task_roots=[str(other_ws)],
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(3),
            )
        )
        assert cross.status == "DENIED"
        assert cross.error_code == "CROSS_TASK_ACCESS_DENIED"

        # Missing workspace_root denied fail-closed
        no_ws = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id="tools.files.read",
                parameters={"path": "notes.txt"},
                workspace_root=None,
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(4),
            )
        )
        assert no_ws.status == "DENIED"
        assert no_ws.error_code == "MISSING_WORKSPACE_CONTEXT"

        # Delete requires human approval first
        victim = workspace / "temp.txt"
        victim.write_text("trash", encoding="utf-8")
        del_req = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id="tools.files.delete",
                parameters={"path": "temp.txt"},
                workspace_root=str(workspace),
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(5),
            )
        )
        assert del_req.status == "APPROVAL_REQUIRED"
        assert victim.exists()

        apr = approval_svc.create_approval_request(
            requester=worker,
            action=del_req.data["approval_action"],
            target_resource=del_req.data["resource"],
            task_id=task_id,
            run_id=run_id,
            request_fingerprint=del_req.data["request_fingerprint"],
            policy_snapshot=ApprovalPolicyContext(
                matched_rule_ids=["gov.require_approval_sensitive_admin_ops"],
                rule_versions={"gov.require_approval_sensitive_admin_ops": "1.0.0"},
                reason_code="APPROVAL_RULE_MATCHED",
            ),
            risk_summary="Approve single file delete",
            requested_at=_utc(6),
            expires_at=_utc(30),
        )
        approval_svc.decide_approval(
            apr.approval_id,
            decider=TrustedAuthContext(
                actor_id="operator.alice",
                actor_kind="human_operator",
                auth_source="operator_session",
                authenticated=True,
            ),
            approved=True,
            reason="Confirmed delete",
            timestamp=_utc(7),
        )
        del_ok = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id="tools.files.delete",
                parameters={"path": "temp.txt"},
                workspace_root=str(workspace),
                task_id=task_id,
                run_id=run_id,
                approval_id=apr.approval_id,
                timestamp=_utc(8),
            )
        )
        assert del_ok.status == "SUCCESS"
        assert del_ok.data["deleted"] is True
        assert del_ok.data["prior_sha256"] == sha256_bytes(b"trash")
        assert not victim.exists()
    finally:
        store.close()
