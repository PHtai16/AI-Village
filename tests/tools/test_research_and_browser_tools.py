"""Fail-closed SSRF tests for P07.04 research tools and real headless Chromium tests for P07.03."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from typing import Any

import pytest

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
from core.tools.models import ToolExecutionError
from storage.db import apply_migrations, sqlite_connection
from storage.repositories import SQLiteApprovalRepository, SQLiteRepositoryStore
from tools.browser import build_browser_tools
from tools.files import sha256_bytes
from tools.mcp_bridge import GovernedMcpBridge, McpInvocationResult, McpServerConfig
from tools.research import HttpFetchResponse, UNTRUSTED_DATA_LABEL, build_research_tools, validate_public_http_url

FAKE_BEARER = "Bearer SuperSecretTokenValue123456"


def _utc(minute: int = 0) -> datetime:
    return datetime(2026, 11, 27, 16, minute, 0, tzinfo=timezone.utc)


class _DemoHttpHandler(BaseHTTPRequestHandler):
    """Minimal local HTTP page with interactive DOM form for real Playwright smoke testing."""

    def do_GET(self) -> None:
        html = (
            "<!doctype html><html><head><title>AI Village Gate</title></head><body>"
            "<h1 id='greeting'>Waiting</h1>"
            "<input id='user_name' type='text' />"
            "<button id='apply_btn' onclick=\"document.getElementById('greeting').innerText = "
            "'Hello ' + document.getElementById('user_name').value\">Apply</button>"
            "</body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def log_message(self, format: str, *args: object) -> None:
        return


@contextmanager
def _local_http_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DemoHttpHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/"
    finally:
        server.shutdown()
        server.server_close()


def _setup_web_runtime(
    tmp_path: Path,
    *,
    allow_private_for_test: bool = False,
    http_fetcher: Any = None,
    search_backend: Any = None,
    resolver: Any = None,
    mcp_bridge: GovernedMcpBridge | None = None,
) -> tuple[SQLiteRepositoryStore, ToolRuntime, ApprovalService, Path, str, str]:
    db_file = tmp_path / "web_tools.db"
    with sqlite_connection(db_file) as conn:
        apply_migrations(conn)
    store = SQLiteRepositoryStore(db_file)
    ts = _utc()
    task_id = new_task_id()
    run_id = new_run_id()
    store.tasks.create_task(
        Task(
            task_id=task_id,
            title="Web and Browser Tools",
            description="Verify P07.03 and P07.04",
            requested_by="operator.alice",
            created_at=ts,
            updated_at=ts,
        )
    )
    store.runs.create_run(Run(run_id=run_id, task_id=task_id, runtime_id="custom.master_worker_v1"))

    reg = ToolRegistry(repository=store.tools, journal=store.events)
    for manifest, handler in build_research_tools(
        mcp_bridge=mcp_bridge,
        http_fetcher=http_fetcher,
        search_backend=search_backend,
        resolver=resolver,
        allow_private_for_test=allow_private_for_test,
    ):
        reg.register(manifest, handler, registered_at=ts)
    for manifest, handler in build_browser_tools(
        mcp_bridge=mcp_bridge,
        resolver=resolver,
        allow_local_test_urls=allow_private_for_test,
    ):
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
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    return store, runtime, approval_svc, ws, task_id, run_id


def test_ssrf_url_guard_blocks_loopback_metadata_rfc1918_and_dns_rebinding() -> None:
    """Block all internal/local/file/credential URLs and DNS-rebinding private resolutions."""
    blocked_urls = [
        "http://localhost/admin",
        "http://127.0.0.1:8080/health",
        "http://10.0.0.5/internal",
        "http://172.16.4.10/api",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]:8000/",
        "http://service.local/data",
        "file:///etc/passwd",
        "http://user:pass@example.com/",
    ]
    for target in blocked_urls:
        with pytest.raises(ToolExecutionError) as exc_info:
            validate_public_http_url(target)
        assert exc_info.value.error_code == "SSRF_BLOCKED", target

    # DNS resolution pointing to private IP must also fail closed
    with pytest.raises(ToolExecutionError) as dns_exc:
        validate_public_http_url("https://rebind.example.com/", resolver=lambda _h: ["127.0.0.1"])
    assert dns_exc.value.error_code == "SSRF_BLOCKED"

    assert validate_public_http_url(
        "https://example.com/docs",
        resolver=lambda _h: ["93.184.216.34"],
    ) == "https://example.com/docs"


def test_research_fetch_modes_search_and_untrusted_markers(tmp_path: Path) -> None:
    """Verify web_fetch (httpx, jina_reader, mcp_fetch), redirect SSRF block, and web_search citations."""
    seen_fetch_urls: list[str] = []

    def _stub_fetch(url: str, _timeout: int) -> HttpFetchResponse:
        seen_fetch_urls.append(url)
        if "redirect-local" in url:
            return HttpFetchResponse(
                url=url,
                final_url="http://127.0.0.1:9000/secret",
                status_code=200,
                text="escaped",
            )
        return HttpFetchResponse(
            url=url,
            final_url=url,
            status_code=200,
            text=f"# Example Doc\nAuth header: {FAKE_BEARER}",
        )

    def _stub_search(_query: str, _max_res: int) -> list[dict[str, Any]]:
        return [
            {"title": "Public Result", "url": "https://docs.python.org/3/", "snippet": "Python docs"},
            {"title": "Blocked Internal", "url": "http://127.0.0.1/admin", "snippet": "hidden"},
        ]

    bridge = GovernedMcpBridge(
        invoker=lambda cfg, name, args, _e, _c: McpInvocationResult(
            server_id=cfg.server_id,
            remote_tool_name=name,
            text_content=f"MCP fetched content for {args['url']}",
        )
    )
    store, runtime, _, _, task_id, run_id = _setup_web_runtime(
        tmp_path,
        http_fetcher=_stub_fetch,
        search_backend=_stub_search,
        resolver=lambda _h: ["93.184.216.34"],
        mcp_bridge=bridge,
    )
    try:
        worker = TrustedAuthContext(
            actor_id="marketing.researcher",
            actor_kind="worker_agent",
            auth_source="application_boundary",
            authenticated=True,
            allowed_tools=["tools.research.web_fetch", "tools.research.web_search"],
        )

        r_http = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id="tools.research.web_fetch",
                parameters={"url": "https://example.com/guide", "mode": "httpx"},
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(1),
            )
        )
        assert r_http.status == "SUCCESS"
        assert r_http.untrusted_content is True
        assert r_http.estimated_cost_usd is None
        assert r_http.evidence_refs and r_http.evidence_refs[0].startswith("evd_")
        assert r_http.data["content_trust_level"] == UNTRUSTED_DATA_LABEL
        assert "SuperSecretTokenValue" not in r_http.data["content"]

        r_jina = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id="tools.research.web_fetch",
                parameters={"url": "https://example.com/guide", "mode": "jina_reader"},
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(2),
            )
        )
        assert r_jina.status == "SUCCESS"
        assert seen_fetch_urls[-1] == "https://r.jina.ai/https://example.com/guide"

        r_mcp = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id="tools.research.web_fetch",
                parameters={"url": "https://example.com/guide", "mode": "mcp_fetch"},
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(3),
            )
        )
        assert r_mcp.status == "SUCCESS"
        assert r_mcp.data["mode"] == "mcp_fetch"

        r_redir = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id="tools.research.web_fetch",
                parameters={"url": "https://example.com/redirect-local"},
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(4),
            )
        )
        assert r_redir.status == "FAILED"
        assert r_redir.error_code == "SSRF_BLOCKED"

        r_search = runtime.invoke(
            ToolInvocationRequest(
                auth_context=worker,
                tool_id="tools.research.web_search",
                parameters={"query": "python 3.13 pydantic", "max_results": 5},
                task_id=task_id,
                run_id=run_id,
                timestamp=_utc(5),
            )
        )
        assert r_search.status == "SUCCESS"
        assert r_search.untrusted_content is True
        assert len(r_search.data["results"]) == 1
        assert r_search.data["results"][0]["url"] == "https://docs.python.org/3/"
    finally:
        store.close()


def test_real_chromium_browser_session_screenshot_and_approved_interact(tmp_path: Path) -> None:
    """Launch real headless Playwright Chromium against ephemeral HTTP server and verify screenshots/approvals."""
    store, runtime, approval_svc, ws, task_id, run_id = _setup_web_runtime(
        tmp_path,
        allow_private_for_test=True,
    )
    try:
        worker = TrustedAuthContext(
            actor_id="qa.browser_verifier",
            actor_kind="worker_agent",
            auth_source="application_boundary",
            authenticated=True,
            allowed_tools=["tools.browser.session", "tools.browser.interact"],
        )

        with _local_http_server() as base_url:
            # 1. Real headless Chromium extract_text + PNG screenshot
            shot = runtime.invoke(
                ToolInvocationRequest(
                    auth_context=worker,
                    tool_id="tools.browser.session",
                    parameters={
                        "url": base_url,
                        "action": "extract_text",
                        "screenshot_path": "evidence/home.png",
                    },
                    workspace_root=str(ws),
                    task_id=task_id,
                    run_id=run_id,
                    timestamp=_utc(1),
                )
            )
            assert shot.status == "SUCCESS", shot.error_message
            assert shot.untrusted_content is True
            assert shot.data["title"] == "AI Village Gate"
            assert "Waiting" in shot.data["extracted_text"]
            png_file = ws / "evidence" / "home.png"
            assert png_file.is_file()
            raw_png = png_file.read_bytes()
            assert raw_png.startswith(b"\x89PNG")
            assert shot.data["screenshot_sha256"] == sha256_bytes(raw_png)
            assert len(shot.data["action_log"]) >= 2
            assert shot.evidence_refs and shot.evidence_refs[0].startswith("evd_")

            # 2. Overwrite guard prevents silently overwriting existing screenshot
            dup = runtime.invoke(
                ToolInvocationRequest(
                    auth_context=worker,
                    tool_id="tools.browser.session",
                    parameters={
                        "url": base_url,
                        "action": "screenshot",
                        "screenshot_path": "evidence/home.png",
                    },
                    workspace_root=str(ws),
                    task_id=task_id,
                    run_id=run_id,
                    timestamp=_utc(2),
                )
            )
            assert dup.status == "FAILED"
            assert dup.error_code == "FILE_EXISTS_OVERWRITE_DENIED"

            # 3. tools.browser.interact blocked until human operator approves
            steps = [
                {"action": "fill", "selector": "#user_name", "value": "Operator"},
                {"action": "click", "selector": "#apply_btn"},
            ]
            unapproved = runtime.invoke(
                ToolInvocationRequest(
                    auth_context=worker,
                    tool_id="tools.browser.interact",
                    parameters={
                        "url": base_url,
                        "steps": steps,
                        "screenshot_path": "evidence/after_click.png",
                    },
                    workspace_root=str(ws),
                    task_id=task_id,
                    run_id=run_id,
                    timestamp=_utc(3),
                )
            )
            assert unapproved.status == "APPROVAL_REQUIRED"

            apr = approval_svc.create_approval_request(
                requester=worker,
                action=unapproved.data["approval_action"],
                target_resource=unapproved.data["resource"],
                task_id=task_id,
                run_id=run_id,
                request_fingerprint=unapproved.data["request_fingerprint"],
                policy_snapshot=ApprovalPolicyContext(
                    matched_rule_ids=["gov.require_approval_sensitive_admin_ops"],
                    rule_versions={"gov.require_approval_sensitive_admin_ops": "1.0.0"},
                    reason_code="APPROVAL_RULE_MATCHED",
                ),
                risk_summary="Approve Chromium form interaction",
                requested_at=_utc(4),
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
                reason="Verified safe local test form",
                timestamp=_utc(5),
            )

            approved = runtime.invoke(
                ToolInvocationRequest(
                    auth_context=worker,
                    tool_id="tools.browser.interact",
                    parameters={
                        "url": base_url,
                        "steps": steps,
                        "screenshot_path": "evidence/after_click.png",
                    },
                    workspace_root=str(ws),
                    task_id=task_id,
                    run_id=run_id,
                    approval_id=apr.approval_id,
                    timestamp=_utc(6),
                )
            )
            assert approved.status == "SUCCESS", approved.error_message
            assert approved.data["steps_executed"] == 2
            assert "Hello Operator" in approved.data["extracted_text"]
            assert (ws / "evidence" / "after_click.png").is_file()

            # Replay rejected fail-closed
            replay = runtime.invoke(
                ToolInvocationRequest(
                    auth_context=worker,
                    tool_id="tools.browser.interact",
                    parameters={
                        "url": base_url,
                        "steps": steps,
                        "screenshot_path": "evidence/after_click.png",
                    },
                    workspace_root=str(ws),
                    task_id=task_id,
                    run_id=run_id,
                    approval_id=apr.approval_id,
                    timestamp=_utc(7),
                )
            )
            assert replay.status == "DENIED"
            assert replay.error_code == "APPROVAL_ALREADY_CONSUMED"
    finally:
        store.close()
