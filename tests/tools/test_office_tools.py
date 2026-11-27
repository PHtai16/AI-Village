"""Tests for P07.05 office tools: Word, Excel, PDF, MarkItDown (native + MCP), and table computation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pymupdf

from core.governance import PermissionManager, PolicyEvaluator, TrustedAuthContext, build_default_policy_rules
from core.events import InMemoryEventJournal
from core.tools import ToolInvocationRequest, ToolRegistry, ToolRuntime
from tools.mcp_bridge import GovernedMcpBridge, McpInvocationResult, McpServerConfig
from tools.office import build_office_tools


def _utc(minute: int = 0) -> datetime:
    return datetime(2026, 11, 27, 14, minute, 0, tzinfo=timezone.utc)


def _setup_office(
    tmp_path: Path,
    *,
    mcp_calls: list[tuple[str, str, dict[str, Any]]] | None = None,
) -> tuple[ToolRuntime, Path, TrustedAuthContext]:
    journal = InMemoryEventJournal()
    recorded = mcp_calls if mcp_calls is not None else []

    def _fake_invoker(
        cfg: McpServerConfig,
        tool_name: str,
        args: dict[str, Any],
        _env: dict[str, str],
        _cwd: str | None,
    ) -> McpInvocationResult:
        recorded.append((cfg.server_id, tool_name, args))
        return McpInvocationResult(
            server_id=cfg.server_id,
            remote_tool_name=tool_name,
            text_content="# Converted Via MCP\n\n- Row 1",
        )

    bridge = GovernedMcpBridge(invoker=_fake_invoker)
    reg = ToolRegistry(journal=journal)
    for manifest, handler in build_office_tools(mcp_bridge=bridge):
        reg.register(manifest, handler, registered_at=_utc())

    worker = TrustedAuthContext(
        actor_id="service.doc_worker",
        actor_kind="worker_agent",
        auth_source="application_boundary",
        authenticated=True,
        allowed_tools=sorted(reg.registered_tool_ids()),
    )
    perm_mgr = PermissionManager(
        evaluator=PolicyEvaluator(
            rules=build_default_policy_rules(),
            registered_tools=reg.registered_tool_ids(),
        ),
        journal=journal,
    )
    runtime = ToolRuntime(registry=reg, permission_manager=perm_mgr, journal=journal)
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    return runtime, workspace, worker


def test_docx_excel_pdf_and_markitdown_roundtrips(tmp_path: Path) -> None:
    """Verify real DOCX, XLSX, PDF, and MarkItDown conversions with workspace & overwrite guards."""
    mcp_calls: list[tuple[str, str, dict[str, Any]]] = []
    runtime, workspace, worker = _setup_office(tmp_path, mcp_calls=mcp_calls)

    # 1. DOCX write -> read -> overwrite guard
    w_docx = runtime.invoke(
        ToolInvocationRequest(
            auth_context=worker,
            tool_id="tools.office.write_docx",
            parameters={
                "path": "brief.docx",
                "title": "Project Brief",
                "paragraphs": ["First paragraph.", "Second paragraph."],
                "tables": [[["Metric", "Value"], ["Latency", "12ms"]]],
            },
            workspace_root=str(workspace),
            timestamp=_utc(1),
        )
    )
    assert w_docx.status == "SUCCESS"
    assert w_docx.artifacts[0]["content_type"].endswith("wordprocessingml.document")

    dup_docx = runtime.invoke(
        ToolInvocationRequest(
            auth_context=worker,
            tool_id="tools.office.write_docx",
            parameters={"path": "brief.docx", "paragraphs": ["Overwrite attempt"]},
            workspace_root=str(workspace),
            timestamp=_utc(2),
        )
    )
    assert dup_docx.status == "FAILED"
    assert dup_docx.error_code == "FILE_EXISTS_OVERWRITE_DENIED"

    r_docx = runtime.invoke(
        ToolInvocationRequest(
            auth_context=worker,
            tool_id="tools.office.read_docx",
            parameters={"path": "brief.docx"},
            workspace_root=str(workspace),
            timestamp=_utc(3),
        )
    )
    assert r_docx.status == "SUCCESS"
    assert "First paragraph." in r_docx.data["paragraphs"]
    assert r_docx.data["tables"][0][1] == ["Latency", "12ms"]

    # 2. XLSX write -> read
    w_xlsx = runtime.invoke(
        ToolInvocationRequest(
            auth_context=worker,
            tool_id="tools.office.write_excel",
            parameters={
                "path": "ledger.xlsx",
                "sheets": {
                    "Budget": [["Dept", "Cost"], ["it", 100], ["qa", 50], ["Total", "=SUM(B2:B3)"]]
                },
            },
            workspace_root=str(workspace),
            timestamp=_utc(4),
        )
    )
    assert w_xlsx.status == "SUCCESS"
    assert w_xlsx.data["row_count"] == 4

    r_xlsx = runtime.invoke(
        ToolInvocationRequest(
            auth_context=worker,
            tool_id="tools.office.read_excel",
            parameters={"path": "ledger.xlsx", "sheet_name": "Budget", "data_only": False},
            workspace_root=str(workspace),
            timestamp=_utc(5),
        )
    )
    assert r_xlsx.status == "SUCCESS"
    assert r_xlsx.data["sheets"]["Budget"][3][1] == "=SUM(B2:B3)"

    # 3. PDF read using PyMuPDF generated PDF
    pdf_path = workspace / "spec.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "AI Village Phase 07 Spec")
    doc.save(str(pdf_path))
    doc.close()

    r_pdf = runtime.invoke(
        ToolInvocationRequest(
            auth_context=worker,
            tool_id="tools.office.read_pdf",
            parameters={"path": "spec.pdf"},
            workspace_root=str(workspace),
            timestamp=_utc(6),
        )
    )
    assert r_pdf.status == "SUCCESS"
    assert r_pdf.data["page_count"] == 1
    assert "AI Village Phase 07 Spec" in r_pdf.data["pages"][0]["text"]

    # 4. MarkItDown local conversion on DOCX + MCP delegation test
    md_local = runtime.invoke(
        ToolInvocationRequest(
            auth_context=worker,
            tool_id="tools.office.parse_markdown",
            parameters={"path": "brief.docx"},
            workspace_root=str(workspace),
            timestamp=_utc(7),
        )
    )
    assert md_local.status == "SUCCESS"
    assert md_local.data["via_mcp"] is False
    assert "Project Brief" in md_local.data["markdown"]

    md_mcp = runtime.invoke(
        ToolInvocationRequest(
            auth_context=worker,
            tool_id="tools.office.parse_markdown",
            parameters={"path": "spec.pdf", "use_mcp": True},
            workspace_root=str(workspace),
            timestamp=_utc(8),
        )
    )
    assert md_mcp.status == "SUCCESS"
    assert md_mcp.data["via_mcp"] is True
    assert "Converted Via MCP" in md_mcp.data["markdown"]
    assert mcp_calls[0][0] == "markitdown"
    assert mcp_calls[0][1] == "convert_to_markdown"
    assert mcp_calls[0][2]["uri"].startswith("file:///")

    # Traversal check rejected cleanly
    bad_office = runtime.invoke(
        ToolInvocationRequest(
            auth_context=worker,
            tool_id="tools.office.read_docx",
            parameters={"path": "../outside.docx"},
            workspace_root=str(workspace),
            timestamp=_utc(9),
        )
    )
    assert bad_office.status == "DENIED"


def test_deterministic_compute_table_and_invalid_numeric_guard(tmp_path: Path) -> None:
    """Verify code-backed tabular math (sum/avg/min/max/count/group_by) and non-numeric rejection."""
    runtime, workspace, worker = _setup_office(tmp_path)

    res = runtime.invoke(
        ToolInvocationRequest(
            auth_context=worker,
            tool_id="tools.office.compute_table",
            parameters={
                "rows": [
                    {"dept": "it", "cost": 10.5},
                    {"dept": "it", "cost": 4.5},
                    {"dept": "qa", "cost": 6.0},
                ],
                "group_by": "dept",
                "metrics": [
                    {"column": "cost", "op": "sum"},
                    {"column": "cost", "op": "avg"},
                    {"column": "cost", "op": "count", "alias": "items"},
                ],
            },
            workspace_root=str(workspace),
            timestamp=_utc(1),
        )
    )
    assert res.status == "SUCCESS"
    assert res.data["aggregates"]["it"] == {"sum_cost": 15.0, "avg_cost": 7.5, "items": 2}
    assert res.data["aggregates"]["qa"] == {"sum_cost": 6.0, "avg_cost": 6.0, "items": 1}

    bad = runtime.invoke(
        ToolInvocationRequest(
            auth_context=worker,
            tool_id="tools.office.compute_table",
            parameters={
                "rows": [{"cost": "not_a_number"}],
                "metrics": [{"column": "cost", "op": "sum"}],
            },
            workspace_root=str(workspace),
            timestamp=_utc(2),
        )
    )
    assert bad.status == "FAILED"
    assert bad.error_code == "INVALID_NUMERIC_VALUE"
