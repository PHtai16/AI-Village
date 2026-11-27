"""Verification of the 9-department advisory tool/MCP matrix and end-to-end execution of all 17 tools."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import threading
from typing import Any

import pymupdf

from core.agents.loader import load_department_manifest_file
from core.contracts import DEFAULT_DEPARTMENTS, Run, Task, new_run_id, new_task_id
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
from tools import register_phase07_tools
from tools.code import CommandRunResult
from tools.mcp_bridge import GovernedMcpBridge, McpInvocationResult

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS_ROOT = REPO_ROOT / "agents"


def _utc(minute: int = 0) -> datetime:
    return datetime(2026, 11, 27, 17, minute, 0, tzinfo=timezone.utc)


class _MatrixHttpHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = (
            "<html><head><title>Matrix Page</title></head>"
            "<body><button id='btn' onclick=\"document.title='Clicked'\">Click</button></body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@contextmanager
def _serve_local() -> Any:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _MatrixHttpHandler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/"
    finally:
        srv.shutdown()
        srv.server_close()


def test_nine_department_manifests_and_advisory_only_permissions(tmp_path: Path) -> None:
    """All 9 department YAMLs load safely, align with ToolRegistry, and NEVER grant tools implicitly."""
    reg = ToolRegistry()
    registered = register_phase07_tools(reg, registered_at=_utc())
    assert len(registered) == 17
    registered_ids = reg.registered_tool_ids()

    loaded_depts = {}
    zone_ids: set[str] = set()
    for dept_id in sorted(DEFAULT_DEPARTMENTS):
        yaml_path = AGENTS_ROOT / "departments" / dept_id / "department.yaml"
        assert yaml_path.is_file(), f"Missing department manifest for '{dept_id}'"
        manifest = load_department_manifest_file(yaml_path, allowed_root=AGENTS_ROOT)
        assert manifest.department_id == dept_id
        assert manifest.status == "ACTIVE"
        assert set(manifest.default_tools).issubset(registered_ids), (
            f"Department '{dept_id}' references unregistered default_tools: "
            f"{set(manifest.default_tools) - registered_ids}"
        )
        assert manifest.visual_metadata.zone_id not in zone_ids
        zone_ids.add(manifest.visual_metadata.zone_id)
        loaded_depts[dept_id] = manifest

    assert set(loaded_depts) == set(DEFAULT_DEPARTMENTS)

    # Critical governance invariant: department.default_tools is advisory only;
    # a worker in `it` cannot run `tools.code.git_inspect` unless it is in `allowed_tools`.
    db_file = tmp_path / "dept_guard.db"
    with sqlite_connection(db_file) as conn:
        apply_migrations(conn)
    with SQLiteRepositoryStore(db_file) as store:
        reg_store = ToolRegistry(repository=store.tools, journal=store.events)
        register_phase07_tools(reg_store, registered_at=_utc())
        perm_mgr = PermissionManager(
            evaluator=PolicyEvaluator(
                rules=build_default_policy_rules(),
                registered_tools=reg_store.registered_tool_ids(),
            ),
            journal=store.events,
        )
        runtime = ToolRuntime(
            registry=reg_store,
            permission_manager=perm_mgr,
            journal=store.events,
            tool_repo=store.tools,
        )
        restricted_it_worker = TrustedAuthContext(
            actor_id="it.limited_worker",
            actor_kind="worker_agent",
            auth_source="application_boundary",
            authenticated=True,
            allowed_tools=["tools.files.read"],  # Notice: omits `tools.code.git_inspect` from `it` default_tools
        )
        denied = runtime.invoke(
            ToolInvocationRequest(
                auth_context=restricted_it_worker,
                tool_id="tools.code.git_inspect",
                parameters={"operation": "status"},
                workspace_root=str(tmp_path),
                timestamp=_utc(1),
            )
        )
        assert denied.status == "DENIED"
        assert denied.error_code == "TOOL_NOT_ALLOWED"


def test_every_single_phase07_tool_executes_end_to_end(tmp_path: Path) -> None:
    """Exercise all 17 canonical Phase 07 tools through ToolRuntime and verify usage summary."""
    db_file = tmp_path / "all17.db"
    with sqlite_connection(db_file) as conn:
        apply_migrations(conn)

    with SQLiteRepositoryStore(db_file) as store:
        ts = _utc()
        task_id = new_task_id()
        run_id = new_run_id()
        store.tasks.create_task(
            Task(
                task_id=task_id,
                title="All 17 Tools Smoke",
                description="Execute every registered Phase 07 tool",
                requested_by="operator.alice",
                created_at=ts,
                updated_at=ts,
            )
        )
        store.runs.create_run(Run(run_id=run_id, task_id=task_id, runtime_id="custom.master_worker_v1"))

        ws = tmp_path / "ws"
        ws.mkdir()
        subprocess.run(["git", "init"], cwd=ws, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "qa@example.com"], cwd=ws, check=True)
        subprocess.run(["git", "config", "user.name", "QA"], cwd=ws, check=True)
        (ws / "script.py").write_text("print('hi')\n", encoding="utf-8")
        pdf = pymupdf.open()
        pdf.new_page().insert_text((72, 72), "Sample PDF")
        pdf.save(str(ws / "sample.pdf"))
        pdf.close()

        reg = ToolRegistry(repository=store.tools, journal=store.events)
        manifests = register_phase07_tools(
            reg,
            mcp_bridge=GovernedMcpBridge(
                invoker=lambda cfg, name, _a, _e, _c: McpInvocationResult(
                    server_id=cfg.server_id,
                    remote_tool_name=name,
                    text_content="ok",
                )
            ),
            command_runner=lambda _cmd, _t, _cwd: CommandRunResult(
                exit_code=0,
                stdout="hi\n",
                stderr="",
                duration_ms=5,
            ),
            search_backend=lambda _q, _m: [
                {"title": "Result", "url": "https://example.org", "snippet": "Snippet"}
            ],
            allow_private_urls_for_test=True,
            registered_at=ts,
        )
        all_ids = [m.id for m in manifests]
        assert len(all_ids) == 17

        approval_svc = ApprovalService(SQLiteApprovalRepository(store), store.events)
        perm_mgr = PermissionManager(
            evaluator=PolicyEvaluator(
                rules=build_default_policy_rules(),
                registered_tools=set(all_ids),
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
        worker = TrustedAuthContext(
            actor_id="qa.all_tools_tester",
            actor_kind="worker_agent",
            auth_source="application_boundary",
            authenticated=True,
            allowed_tools=all_ids,
        )
        approver = TrustedAuthContext(
            actor_id="operator.alice",
            actor_kind="human_operator",
            auth_source="operator_session",
            authenticated=True,
        )

        def _call(tool_id: str, params: dict[str, Any], minute: int, approval_id: str | None = None) -> Any:
            return runtime.invoke(
                ToolInvocationRequest(
                    auth_context=worker,
                    tool_id=tool_id,
                    parameters=params,
                    workspace_root=str(ws),
                    task_id=task_id,
                    run_id=run_id,
                    approval_id=approval_id,
                    timestamp=_utc(minute),
                )
            )

        def _approve(pending_res: Any, minute: int) -> str:
            assert pending_res.status == "APPROVAL_REQUIRED"
            apr = approval_svc.create_approval_request(
                requester=worker,
                action=pending_res.data["approval_action"],
                target_resource=pending_res.data["resource"],
                task_id=task_id,
                run_id=run_id,
                request_fingerprint=pending_res.data["request_fingerprint"],
                policy_snapshot=ApprovalPolicyContext(
                    matched_rule_ids=["gov.require_approval_sensitive_admin_ops"],
                    rule_versions={"gov.require_approval_sensitive_admin_ops": "1.0.0"},
                    reason_code="APPROVAL_RULE_MATCHED",
                ),
                risk_summary="Approve matrix test call",
                requested_at=_utc(minute),
                expires_at=_utc(55),
            )
            approval_svc.decide_approval(
                apr.approval_id,
                decider=approver,
                approved=True,
                reason="OK",
                timestamp=_utc(minute),
            )
            return apr.approval_id

        executed_ok: set[str] = set()
        with _serve_local() as base_url:
            calls: list[tuple[str, dict[str, Any]]] = [
                ("tools.files.write", {"path": "note.txt", "content": "alpha"}),
                ("tools.files.read", {"path": "note.txt"}),
                ("tools.files.list", {"path": "."}),
                ("tools.office.write_docx", {"path": "doc.docx", "paragraphs": ["p1"]}),
                ("tools.office.read_docx", {"path": "doc.docx"}),
                ("tools.office.write_excel", {"path": "sheet.xlsx", "sheets": {"S1": [["A", 1]]}}),
                ("tools.office.read_excel", {"path": "sheet.xlsx"}),
                ("tools.office.read_pdf", {"path": "sample.pdf"}),
                ("tools.office.parse_markdown", {"path": "doc.docx"}),
                (
                    "tools.office.compute_table",
                    {"rows": [{"v": 10}, {"v": 20}], "metrics": [{"column": "v", "op": "sum"}]},
                ),
                ("tools.code.run_sandbox", {"operation": "python_script", "script_path": "script.py"}),
                ("tools.code.git_inspect", {"operation": "status"}),
                ("tools.research.web_fetch", {"url": base_url, "mode": "httpx"}),
                ("tools.research.web_search", {"query": "ai village"}),
                ("tools.browser.session", {"url": base_url, "action": "screenshot", "screenshot_path": "s.png"}),
            ]
            for idx, (tid, params) in enumerate(calls, start=1):
                res = _call(tid, params, idx)
                assert res.status == "SUCCESS", f"{tid} failed: {res.error_code} {res.error_message}"
                executed_ok.add(tid)

            # Approval-gated tools
            del_req = _call("tools.files.delete", {"path": "note.txt"}, 20)
            del_ok = _call("tools.files.delete", {"path": "note.txt"}, 22, approval_id=_approve(del_req, 21))
            assert del_ok.status == "SUCCESS"
            executed_ok.add("tools.files.delete")

            int_params = {"url": base_url, "steps": [{"action": "click", "selector": "#btn"}]}
            int_req = _call("tools.browser.interact", int_params, 23)
            int_ok = _call("tools.browser.interact", int_params, 25, approval_id=_approve(int_req, 24))
            assert int_ok.status == "SUCCESS"
            executed_ok.add("tools.browser.interact")

        assert executed_ok == set(all_ids)
        summary = runtime.summarize_usage(task_id=task_id, run_id=run_id)
        assert summary.total_calls == 19  # 15 direct + 2 pending + 2 approved
        assert summary.by_status["SUCCESS"] == 17
        assert summary.by_status["APPROVAL_REQUIRED"] == 2
