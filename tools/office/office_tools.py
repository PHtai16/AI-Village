"""P07.05 office tools for Word, Excel, PDF, Markdown conversion, and deterministic table math."""

from __future__ import annotations

from collections import defaultdict
import os
from pathlib import Path
import tempfile
from typing import Any

from docx import Document as DocxDocument
from markitdown import MarkItDown
import openpyxl
import pymupdf
from pypdf import PdfReader

from core.tools.models import (
    McpToolBinding,
    ToolCostProfile,
    ToolExecutionContext,
    ToolExecutionError,
    ToolHandler,
    ToolHandlerOutput,
    ToolManifest,
)
from tools.files.file_tools import _require_validated_path, sha256_bytes
from tools.mcp_bridge.client import GovernedMcpBridge

_DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _atomic_write_via_temp(target: Path, writer: Any) -> bytes:
    """Invoke ``writer(tmp_path)`` then atomically replace ``target`` and return written bytes."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp_office_", suffix=target.suffix)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        writer(tmp_path)
        data = tmp_path.read_bytes()
        os.replace(tmp_path, target)
        return data
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def _check_overwrite(target: Path, rel_posix: str, overwrite: bool) -> bool:
    existed = target.exists()
    if existed and target.is_dir():
        raise ToolExecutionError("TARGET_IS_DIRECTORY", f"Target '{rel_posix}' is a directory.")
    if existed and not overwrite:
        raise ToolExecutionError(
            "FILE_EXISTS_OVERWRITE_DENIED",
            f"File '{rel_posix}' already exists; pass overwrite=true to replace it.",
        )
    return existed


def _json_cell(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _handle_read_docx(params: dict[str, Any], ctx: ToolExecutionContext) -> ToolHandlerOutput:
    _, target, rel_posix = _require_validated_path(ctx, str(params["path"]), action="files.read")
    if not target.is_file():
        raise ToolExecutionError("FILE_NOT_FOUND", f"DOCX file '{rel_posix}' does not exist.")
    digest = sha256_bytes(target.read_bytes())
    doc = DocxDocument(str(target))
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    tables = [[[cell.text for cell in row.cells] for row in table.rows] for table in doc.tables]
    return ToolHandlerOutput(
        data={"path": rel_posix, "paragraphs": paragraphs, "tables": tables, "sha256": digest}
    )


def _handle_write_docx(params: dict[str, Any], ctx: ToolExecutionContext) -> ToolHandlerOutput:
    _, target, rel_posix = _require_validated_path(ctx, str(params["path"]), action="files.write")
    existed = _check_overwrite(target, rel_posix, bool(params.get("overwrite", False)))
    title = params.get("title")
    paragraphs = [str(item) for item in params.get("paragraphs", [])]
    tables = params.get("tables", [])

    def _write(dest: Path) -> None:
        doc = DocxDocument()
        if isinstance(title, str) and title.strip():
            doc.add_heading(title.strip(), level=1)
        for text in paragraphs:
            doc.add_paragraph(text)
        for tbl_rows in tables:
            if not tbl_rows:
                continue
            cols = max(len(r) for r in tbl_rows)
            if cols <= 0:
                continue
            table = doc.add_table(rows=len(tbl_rows), cols=cols)
            for r_idx, row_vals in enumerate(tbl_rows):
                for c_idx, cell_val in enumerate(row_vals):
                    table.cell(r_idx, c_idx).text = str(cell_val)
        doc.save(str(dest))

    payload = _atomic_write_via_temp(target, _write)
    digest = sha256_bytes(payload)
    return ToolHandlerOutput(
        data={
            "path": rel_posix,
            "paragraph_count": len(paragraphs),
            "table_count": len(tables),
            "size_bytes": len(payload),
            "sha256": digest,
            "overwritten": existed,
        },
        artifacts=[
            {
                "path": rel_posix,
                "sha256": digest,
                "size_bytes": len(payload),
                "content_type": _DOCX_CONTENT_TYPE,
            }
        ],
    )


def _handle_read_excel(params: dict[str, Any], ctx: ToolExecutionContext) -> ToolHandlerOutput:
    _, target, rel_posix = _require_validated_path(ctx, str(params["path"]), action="files.read")
    if not target.is_file():
        raise ToolExecutionError("FILE_NOT_FOUND", f"Excel file '{rel_posix}' does not exist.")
    digest = sha256_bytes(target.read_bytes())
    data_only = bool(params.get("data_only", True))
    max_rows = int(params.get("max_rows", 500))
    req_sheet = params.get("sheet_name")

    wb = openpyxl.load_workbook(str(target), read_only=True, data_only=data_only)
    try:
        sheet_names = list(wb.sheetnames)
        if isinstance(req_sheet, str) and req_sheet not in wb.sheetnames:
            raise ToolExecutionError("SHEET_NOT_FOUND", f"Sheet '{req_sheet}' does not exist.")
        selected = [req_sheet] if isinstance(req_sheet, str) else sheet_names
        sheets_data: dict[str, list[list[Any]]] = {}
        for name in selected:
            ws = wb[name]
            rows_out: list[list[Any]] = []
            for idx, row in enumerate(ws.iter_rows(values_only=True)):
                if idx >= max_rows:
                    break
                rows_out.append([_json_cell(cell) for cell in row])
            sheets_data[name] = rows_out
    finally:
        wb.close()

    return ToolHandlerOutput(
        data={
            "path": rel_posix,
            "sheet_names": sheet_names,
            "sheets": sheets_data,
            "sha256": digest,
        }
    )


def _handle_write_excel(params: dict[str, Any], ctx: ToolExecutionContext) -> ToolHandlerOutput:
    _, target, rel_posix = _require_validated_path(ctx, str(params["path"]), action="files.write")
    existed = _check_overwrite(target, rel_posix, bool(params.get("overwrite", False)))
    sheets: dict[str, list[list[Any]]] = params["sheets"]
    if not sheets:
        raise ToolExecutionError("EMPTY_WORKBOOK", "At least one worksheet is required.")

    total_rows = 0

    def _write(dest: Path) -> None:
        nonlocal total_rows
        wb = openpyxl.Workbook()
        default_ws = wb.active
        first = True
        for sheet_name, rows in sheets.items():
            ws = default_ws if first and default_ws is not None else wb.create_sheet(title=sheet_name)
            ws.title = sheet_name
            first = False
            for row in rows:
                ws.append(list(row))
                total_rows += 1
        wb.save(str(dest))
        wb.close()

    payload = _atomic_write_via_temp(target, _write)
    digest = sha256_bytes(payload)
    return ToolHandlerOutput(
        data={
            "path": rel_posix,
            "sheet_names": list(sheets.keys()),
            "row_count": total_rows,
            "size_bytes": len(payload),
            "sha256": digest,
            "overwritten": existed,
        },
        artifacts=[
            {
                "path": rel_posix,
                "sha256": digest,
                "size_bytes": len(payload),
                "content_type": _XLSX_CONTENT_TYPE,
            }
        ],
    )


def _handle_read_pdf(params: dict[str, Any], ctx: ToolExecutionContext) -> ToolHandlerOutput:
    _, target, rel_posix = _require_validated_path(ctx, str(params["path"]), action="files.read")
    if not target.is_file():
        raise ToolExecutionError("FILE_NOT_FOUND", f"PDF file '{rel_posix}' does not exist.")
    raw = target.read_bytes()
    digest = sha256_bytes(raw)
    max_pages = int(params.get("max_pages", 50))
    pages: list[dict[str, Any]] = []
    meta: dict[str, str] = {}

    try:
        with pymupdf.open(str(target)) as doc:
            page_count = doc.page_count
            for k, v in (doc.metadata or {}).items():
                if v is not None and str(v).strip():
                    meta[str(k)] = str(v)
            for idx in range(min(page_count, max_pages)):
                pages.append({"page_number": idx + 1, "text": doc[idx].get_text("text")})
    except Exception:
        reader = PdfReader(str(target))
        page_count = len(reader.pages)
        for idx in range(min(page_count, max_pages)):
            pages.append({"page_number": idx + 1, "text": reader.pages[idx].extract_text() or ""})

    return ToolHandlerOutput(
        data={
            "path": rel_posix,
            "page_count": page_count,
            "pages": pages,
            "metadata": meta,
            "sha256": digest,
        }
    )


def _make_parse_markdown_handler(mcp_bridge: GovernedMcpBridge | None) -> ToolHandler:
    def _handle_parse_markdown(params: dict[str, Any], ctx: ToolExecutionContext) -> ToolHandlerOutput:
        _, target, rel_posix = _require_validated_path(ctx, str(params["path"]), action="files.read")
        if not target.is_file():
            raise ToolExecutionError("FILE_NOT_FOUND", f"Document '{rel_posix}' does not exist.")
        digest = sha256_bytes(target.read_bytes())
        max_chars = int(params.get("max_chars", 50_000))
        use_mcp = bool(params.get("use_mcp", False))

        if use_mcp:
            if mcp_bridge is None:
                raise ToolExecutionError("MCP_BRIDGE_UNAVAILABLE", "GovernedMcpBridge is not configured.")
            mcp_res = mcp_bridge.call_tool(
                server_id="markitdown",
                remote_tool_name="convert_to_markdown",
                arguments={"uri": target.resolve().as_uri()},
            )
            text = mcp_res.text_content
        else:
            conv = MarkItDown().convert(target)
            text = conv.text_content or ""

        truncated = len(text) > max_chars
        bounded = text[:max_chars]
        return ToolHandlerOutput(
            data={
                "path": rel_posix,
                "markdown": bounded,
                "char_count": len(bounded),
                "truncated": truncated,
                "sha256": digest,
                "via_mcp": use_mcp,
            }
        )

    return _handle_parse_markdown


def _aggregate_group(group_rows: list[dict[str, Any]], metrics: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for metric in metrics:
        col = str(metric["column"])
        op = str(metric["op"])
        alias = str(metric.get("alias") or f"{op}_{col}")
        if op == "count":
            result[alias] = sum(1 for row in group_rows if col in row and row[col] is not None)
            continue
        nums: list[float] = []
        for row in group_rows:
            val = row.get(col)
            if val is None:
                continue
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ToolExecutionError(
                    "INVALID_NUMERIC_VALUE",
                    f"Column '{col}' value {val!r} is not numeric for '{op}'.",
                )
            nums.append(float(val))
        if not nums:
            result[alias] = None
        elif op == "sum":
            result[alias] = round(sum(nums), 6)
        elif op == "avg":
            result[alias] = round(sum(nums) / len(nums), 6)
        elif op == "min":
            result[alias] = min(nums)
        elif op == "max":
            result[alias] = max(nums)
    return result


def _handle_compute_table(params: dict[str, Any], _ctx: ToolExecutionContext) -> ToolHandlerOutput:
    rows: list[dict[str, Any]] = params["rows"]
    metrics: list[dict[str, Any]] = params["metrics"]
    group_by = params.get("group_by")

    if isinstance(group_by, str) and group_by.strip():
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[str(row.get(group_by, "<null>"))].append(row)
        aggregates: dict[str, Any] = {
            key: _aggregate_group(grp, metrics) for key, grp in sorted(grouped.items())
        }
    else:
        aggregates = _aggregate_group(rows, metrics)

    return ToolHandlerOutput(
        data={
            "row_count": len(rows),
            "group_by": group_by,
            "aggregates": aggregates,
        }
    )


def build_office_tools(*, mcp_bridge: GovernedMcpBridge | None = None) -> list[tuple[ToolManifest, ToolHandler]]:
    """Return canonical manifests and handlers for P07.05 office document and calculation tools."""
    cost = ToolCostProfile(unit="free_local", estimated_unit_cost_usd=0.0)
    return [
        (
            ToolManifest(
                id="tools.office.read_docx",
                name="Read Word Document",
                version="1.0.0",
                category="office",
                description="Extract paragraphs and tables from a DOCX file inside task workspace.",
                capabilities=["office_reading", "docx_processing"],
                input_schema={
                    "type": "object",
                    "required": ["path"],
                    "additionalProperties": False,
                    "properties": {"path": {"type": "string", "minLength": 1}},
                },
                output_schema={
                    "type": "object",
                    "required": ["path", "paragraphs", "tables", "sha256"],
                    "additionalProperties": False,
                    "properties": {
                        "path": {"type": "string"},
                        "paragraphs": {"type": "array"},
                        "tables": {"type": "array"},
                        "sha256": {"type": "string"},
                    },
                },
                permissions_required=["office.read", "files.read"],
                approval_required=False,
                risk_level="LOW",
                execution_mode="restricted_local",
                timeout_seconds=30,
                cost_profile=cost,
                safe_retry=True,
            ),
            _handle_read_docx,
        ),
        (
            ToolManifest(
                id="tools.office.write_docx",
                name="Write Word Document",
                version="1.0.0",
                category="office",
                description="Create a DOCX file with heading, paragraphs, and tables inside workspace.",
                capabilities=["office_writing", "docx_processing"],
                input_schema={
                    "type": "object",
                    "required": ["paragraphs", "path"],
                    "additionalProperties": False,
                    "properties": {
                        "path": {"type": "string", "minLength": 1},
                        "title": {"type": "string"},
                        "paragraphs": {"type": "array", "items": {"type": "string"}},
                        "tables": {"type": "array"},
                        "overwrite": {"type": "boolean"},
                    },
                },
                output_schema={
                    "type": "object",
                    "required": ["path", "paragraph_count", "table_count", "size_bytes", "sha256", "overwritten"],
                    "additionalProperties": False,
                    "properties": {
                        "path": {"type": "string"},
                        "paragraph_count": {"type": "integer"},
                        "table_count": {"type": "integer"},
                        "size_bytes": {"type": "integer"},
                        "sha256": {"type": "string"},
                        "overwritten": {"type": "boolean"},
                    },
                },
                permissions_required=["office.write", "files.write"],
                approval_required=False,
                risk_level="MEDIUM",
                execution_mode="restricted_local",
                timeout_seconds=30,
                cost_profile=cost,
                safe_retry=False,
            ),
            _handle_write_docx,
        ),
        (
            ToolManifest(
                id="tools.office.read_excel",
                name="Read Excel Workbook",
                version="1.0.0",
                category="office",
                description="Read sheets and cell values from an XLSX workbook inside task workspace.",
                capabilities=["office_reading", "xlsx_processing"],
                input_schema={
                    "type": "object",
                    "required": ["path"],
                    "additionalProperties": False,
                    "properties": {
                        "path": {"type": "string", "minLength": 1},
                        "sheet_name": {"type": "string"},
                        "data_only": {"type": "boolean"},
                        "max_rows": {"type": "integer", "minimum": 1, "maximum": 5000},
                    },
                },
                output_schema={
                    "type": "object",
                    "required": ["path", "sheet_names", "sheets", "sha256"],
                    "additionalProperties": False,
                    "properties": {
                        "path": {"type": "string"},
                        "sheet_names": {"type": "array"},
                        "sheets": {"type": "object"},
                        "sha256": {"type": "string"},
                    },
                },
                permissions_required=["office.read", "files.read"],
                approval_required=False,
                risk_level="LOW",
                execution_mode="restricted_local",
                timeout_seconds=30,
                cost_profile=cost,
                safe_retry=True,
            ),
            _handle_read_excel,
        ),
        (
            ToolManifest(
                id="tools.office.write_excel",
                name="Write Excel Workbook",
                version="1.0.0",
                category="office",
                description="Create an XLSX workbook with named sheets, rows, and formulas inside workspace.",
                capabilities=["office_writing", "xlsx_processing"],
                input_schema={
                    "type": "object",
                    "required": ["path", "sheets"],
                    "additionalProperties": False,
                    "properties": {
                        "path": {"type": "string", "minLength": 1},
                        "sheets": {"type": "object"},
                        "overwrite": {"type": "boolean"},
                    },
                },
                output_schema={
                    "type": "object",
                    "required": ["path", "sheet_names", "row_count", "size_bytes", "sha256", "overwritten"],
                    "additionalProperties": False,
                    "properties": {
                        "path": {"type": "string"},
                        "sheet_names": {"type": "array"},
                        "row_count": {"type": "integer"},
                        "size_bytes": {"type": "integer"},
                        "sha256": {"type": "string"},
                        "overwritten": {"type": "boolean"},
                    },
                },
                permissions_required=["office.write", "files.write"],
                approval_required=False,
                risk_level="MEDIUM",
                execution_mode="restricted_local",
                timeout_seconds=30,
                cost_profile=cost,
                safe_retry=False,
            ),
            _handle_write_excel,
        ),
        (
            ToolManifest(
                id="tools.office.read_pdf",
                name="Read PDF Document",
                version="1.0.0",
                category="office",
                description="Extract per-page text and metadata from a workspace PDF via PyMuPDF/pypdf.",
                capabilities=["office_reading", "pdf_processing"],
                input_schema={
                    "type": "object",
                    "required": ["path"],
                    "additionalProperties": False,
                    "properties": {
                        "path": {"type": "string", "minLength": 1},
                        "max_pages": {"type": "integer", "minimum": 1, "maximum": 200},
                    },
                },
                output_schema={
                    "type": "object",
                    "required": ["path", "page_count", "pages", "metadata", "sha256"],
                    "additionalProperties": False,
                    "properties": {
                        "path": {"type": "string"},
                        "page_count": {"type": "integer"},
                        "pages": {"type": "array"},
                        "metadata": {"type": "object"},
                        "sha256": {"type": "string"},
                    },
                },
                permissions_required=["office.read", "files.read"],
                approval_required=False,
                risk_level="LOW",
                execution_mode="restricted_local",
                timeout_seconds=30,
                cost_profile=cost,
                safe_retry=True,
            ),
            _handle_read_pdf,
        ),
        (
            ToolManifest(
                id="tools.office.parse_markdown",
                name="Convert Document to Markdown",
                version="1.0.0",
                category="office",
                description="Convert workspace PDF, DOCX, XLSX, or HTML documents to Markdown via MarkItDown/MCP.",
                capabilities=["office_reading", "markdown_conversion"],
                input_schema={
                    "type": "object",
                    "required": ["path"],
                    "additionalProperties": False,
                    "properties": {
                        "path": {"type": "string", "minLength": 1},
                        "max_chars": {"type": "integer", "minimum": 100, "maximum": 200_000},
                        "use_mcp": {"type": "boolean"},
                    },
                },
                output_schema={
                    "type": "object",
                    "required": ["path", "markdown", "char_count", "truncated", "sha256", "via_mcp"],
                    "additionalProperties": False,
                    "properties": {
                        "path": {"type": "string"},
                        "markdown": {"type": "string"},
                        "char_count": {"type": "integer"},
                        "truncated": {"type": "boolean"},
                        "sha256": {"type": "string"},
                        "via_mcp": {"type": "boolean"},
                    },
                },
                permissions_required=["office.read", "files.read"],
                approval_required=False,
                risk_level="LOW",
                execution_mode="restricted_local",
                timeout_seconds=45,
                cost_profile=cost,
                safe_retry=True,
                mcp_binding=McpToolBinding(server_id="markitdown", remote_tool_name="convert_to_markdown"),
            ),
            _make_parse_markdown_handler(mcp_bridge),
        ),
        (
            ToolManifest(
                id="tools.office.compute_table",
                name="Compute Deterministic Table Summary",
                version="1.0.0",
                category="office",
                description="Perform deterministic sum, avg, min, max, count, and group_by calculations via Python.",
                capabilities=["tabular_computation", "deterministic_analytics"],
                input_schema={
                    "type": "object",
                    "required": ["rows", "metrics"],
                    "additionalProperties": False,
                    "properties": {
                        "rows": {"type": "array", "items": {"type": "object"}},
                        "metrics": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "required": ["column", "op"],
                                "additionalProperties": False,
                                "properties": {
                                    "column": {"type": "string", "minLength": 1},
                                    "op": {"type": "string", "enum": ["sum", "avg", "min", "max", "count"]},
                                    "alias": {"type": "string", "minLength": 1},
                                },
                            },
                        },
                        "group_by": {"type": ["string", "null"]},
                    },
                },
                output_schema={
                    "type": "object",
                    "required": ["row_count", "group_by", "aggregates"],
                    "additionalProperties": False,
                    "properties": {
                        "row_count": {"type": "integer"},
                        "group_by": {"type": ["string", "null"]},
                        "aggregates": {"type": "object"},
                    },
                },
                permissions_required=["office.read"],
                approval_required=False,
                risk_level="LOW",
                execution_mode="restricted_local",
                timeout_seconds=15,
                cost_profile=cost,
                safe_retry=True,
            ),
            _handle_compute_table,
        ),
    ]
