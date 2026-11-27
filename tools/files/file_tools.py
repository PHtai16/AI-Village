"""P07.01 department-independent file tools with strict workspace isolation and overwrite protection."""

from __future__ import annotations

import hashlib
import mimetypes
import os
from pathlib import Path
import tempfile
from typing import Any

from core.governance.workspace import validate_workspace_path
from core.tools.models import (
    ToolCostProfile,
    ToolExecutionContext,
    ToolExecutionError,
    ToolHandler,
    ToolHandlerOutput,
    ToolManifest,
)

_DEFAULT_MAX_READ_BYTES = 524_288


def _require_validated_path(
    ctx: ToolExecutionContext,
    raw_path: str,
    *,
    action: str,
) -> tuple[Path, Path, str]:
    """Validate workspace boundary fail-closed and return `(real_root, real_target, rel_posix)`."""
    if not ctx.workspace_root:
        raise ToolExecutionError("MISSING_WORKSPACE_CONTEXT", "workspace_root is required.")
    check = validate_workspace_path(
        ctx.workspace_root,
        raw_path,
        action=action,
        cross_task_roots=ctx.cross_task_roots,
    )
    if not check.allowed or check.canonical_path is None:
        raise ToolExecutionError(check.reason_code, check.message)
    real_root = Path(os.path.realpath(ctx.workspace_root))
    real_target = Path(check.canonical_path)
    rel_posix = "." if real_target == real_root else real_target.relative_to(real_root).as_posix()
    return real_root, real_target, rel_posix


def sha256_bytes(data: bytes) -> str:
    """Compute lowercase hexadecimal SHA-256 digest of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def _handle_read(params: dict[str, Any], ctx: ToolExecutionContext) -> ToolHandlerOutput:
    _, target, rel_posix = _require_validated_path(ctx, str(params["path"]), action="files.read")
    if not target.exists() or not target.is_file():
        raise ToolExecutionError("FILE_NOT_FOUND", f"File '{rel_posix}' does not exist.")
    raw = target.read_bytes()
    max_bytes = int(params.get("max_bytes", _DEFAULT_MAX_READ_BYTES))
    if len(raw) > max_bytes:
        raise ToolExecutionError(
            "FILE_TOO_LARGE",
            f"File '{rel_posix}' ({len(raw)} bytes) exceeds max_bytes={max_bytes}.",
        )
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolExecutionError("BINARY_OR_NON_UTF8", f"File '{rel_posix}' is not valid UTF-8.") from exc
    return ToolHandlerOutput(
        data={
            "path": rel_posix,
            "content": content,
            "size_bytes": len(raw),
            "sha256": sha256_bytes(raw),
        }
    )


def _handle_write(params: dict[str, Any], ctx: ToolExecutionContext) -> ToolHandlerOutput:
    _, target, rel_posix = _require_validated_path(ctx, str(params["path"]), action="files.write")
    overwrite = bool(params.get("overwrite", False))
    existed = target.exists()
    if existed and target.is_dir():
        raise ToolExecutionError("TARGET_IS_DIRECTORY", f"Target '{rel_posix}' is a directory.")
    if existed and not overwrite:
        raise ToolExecutionError(
            "FILE_EXISTS_OVERWRITE_DENIED",
            f"File '{rel_posix}' already exists; pass overwrite=true to replace it intentionally.",
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    payload = str(params["content"]).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp_write_")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.replace(tmp_name, target)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise

    digest = sha256_bytes(payload)
    content_type = mimetypes.guess_type(target.name)[0] or "text/plain"
    return ToolHandlerOutput(
        data={
            "path": rel_posix,
            "size_bytes": len(payload),
            "sha256": digest,
            "overwritten": existed,
        },
        artifacts=[
            {
                "path": rel_posix,
                "sha256": digest,
                "size_bytes": len(payload),
                "content_type": content_type,
            }
        ],
    )


def _handle_list(params: dict[str, Any], ctx: ToolExecutionContext) -> ToolHandlerOutput:
    real_root, target, rel_posix = _require_validated_path(
        ctx,
        str(params.get("path", ".")),
        action="files.read",
    )
    if not target.exists() or not target.is_dir():
        raise ToolExecutionError("DIRECTORY_NOT_FOUND", f"Directory '{rel_posix}' does not exist.")

    recursive = bool(params.get("recursive", False))
    max_entries = int(params.get("max_entries", 200))
    iterator = sorted(target.rglob("*")) if recursive else sorted(target.iterdir())
    entries: list[dict[str, Any]] = []
    truncated = False

    for candidate in iterator:
        check = validate_workspace_path(
            real_root,
            candidate,
            action="files.read",
            cross_task_roots=ctx.cross_task_roots,
        )
        if not check.allowed or check.canonical_path is None:
            continue
        if len(entries) >= max_entries:
            truncated = True
            break
        real_entry = Path(check.canonical_path)
        entry_rel = real_entry.relative_to(real_root).as_posix()
        entries.append(
            {
                "path": entry_rel,
                "kind": "dir" if real_entry.is_dir() else "file",
                "size_bytes": real_entry.stat().st_size if real_entry.is_file() else 0,
            }
        )

    return ToolHandlerOutput(
        data={"path": rel_posix, "entries": entries, "truncated": truncated}
    )


def _handle_delete(params: dict[str, Any], ctx: ToolExecutionContext) -> ToolHandlerOutput:
    _, target, rel_posix = _require_validated_path(ctx, str(params["path"]), action="files.delete")
    if not target.exists():
        raise ToolExecutionError("FILE_NOT_FOUND", f"File '{rel_posix}' does not exist.")
    if not target.is_file():
        raise ToolExecutionError("TARGET_NOT_FILE", f"Target '{rel_posix}' is not a regular file.")
    prior_digest = sha256_bytes(target.read_bytes())
    target.unlink()
    return ToolHandlerOutput(
        data={"path": rel_posix, "deleted": True, "prior_sha256": prior_digest}
    )


def build_file_tools() -> list[tuple[ToolManifest, ToolHandler]]:
    """Return canonical manifests and handlers for P07.01 workspace file tools."""
    cost = ToolCostProfile(unit="free_local", estimated_unit_cost_usd=0.0)
    return [
        (
            ToolManifest(
                id="tools.files.read",
                name="Read Workspace File",
                version="1.0.0",
                category="files",
                description="Read UTF-8 text files strictly within the authorized task workspace.",
                capabilities=["file_reading", "workspace_inspection"],
                input_schema={
                    "type": "object",
                    "required": ["path"],
                    "additionalProperties": False,
                    "properties": {
                        "path": {"type": "string", "minLength": 1, "maxLength": 260},
                        "max_bytes": {"type": "integer", "minimum": 1, "maximum": 5_242_880},
                    },
                },
                output_schema={
                    "type": "object",
                    "required": ["path", "content", "size_bytes", "sha256"],
                    "additionalProperties": False,
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                        "size_bytes": {"type": "integer"},
                        "sha256": {"type": "string"},
                    },
                },
                permissions_required=["files.read"],
                approval_required=False,
                risk_level="LOW",
                execution_mode="restricted_local",
                timeout_seconds=15,
                cost_profile=cost,
                safe_retry=True,
            ),
            _handle_read,
        ),
        (
            ToolManifest(
                id="tools.files.write",
                name="Write Workspace File",
                version="1.0.0",
                category="files",
                description="Atomically write a UTF-8 file inside workspace, blocking accidental overwrite.",
                capabilities=["file_writing"],
                input_schema={
                    "type": "object",
                    "required": ["path", "content"],
                    "additionalProperties": False,
                    "properties": {
                        "path": {"type": "string", "minLength": 1, "maxLength": 260},
                        "content": {"type": "string", "maxLength": 5_242_880},
                        "overwrite": {"type": "boolean"},
                    },
                },
                output_schema={
                    "type": "object",
                    "required": ["path", "size_bytes", "sha256", "overwritten"],
                    "additionalProperties": False,
                    "properties": {
                        "path": {"type": "string"},
                        "size_bytes": {"type": "integer"},
                        "sha256": {"type": "string"},
                        "overwritten": {"type": "boolean"},
                    },
                },
                permissions_required=["files.write"],
                approval_required=False,
                risk_level="MEDIUM",
                execution_mode="restricted_local",
                timeout_seconds=15,
                cost_profile=cost,
                safe_retry=False,
            ),
            _handle_write,
        ),
        (
            ToolManifest(
                id="tools.files.list",
                name="List Workspace Directory",
                version="1.0.0",
                category="files",
                description="List visible files and folders within task workspace excluding sensitive paths.",
                capabilities=["workspace_inspection"],
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "path": {"type": "string", "minLength": 1, "maxLength": 260},
                        "recursive": {"type": "boolean"},
                        "max_entries": {"type": "integer", "minimum": 1, "maximum": 1000},
                    },
                },
                output_schema={
                    "type": "object",
                    "required": ["path", "entries", "truncated"],
                    "additionalProperties": False,
                    "properties": {
                        "path": {"type": "string"},
                        "entries": {"type": "array"},
                        "truncated": {"type": "boolean"},
                    },
                },
                permissions_required=["files.read"],
                approval_required=False,
                risk_level="LOW",
                execution_mode="restricted_local",
                timeout_seconds=15,
                cost_profile=cost,
                safe_retry=True,
            ),
            _handle_list,
        ),
        (
            ToolManifest(
                id="tools.files.delete",
                name="Delete Workspace File",
                version="1.0.0",
                category="files",
                description="Delete a single regular file inside task workspace under mandatory human approval.",
                capabilities=["file_writing"],
                input_schema={
                    "type": "object",
                    "required": ["path"],
                    "additionalProperties": False,
                    "properties": {
                        "path": {"type": "string", "minLength": 1, "maxLength": 260},
                    },
                },
                output_schema={
                    "type": "object",
                    "required": ["path", "deleted", "prior_sha256"],
                    "additionalProperties": False,
                    "properties": {
                        "path": {"type": "string"},
                        "deleted": {"type": "boolean"},
                        "prior_sha256": {"type": "string"},
                    },
                },
                permissions_required=["files.delete"],
                approval_required=True,
                risk_level="HIGH",
                execution_mode="restricted_local",
                timeout_seconds=15,
                cost_profile=cost,
                safe_retry=False,
            ),
            _handle_delete,
        ),
    ]
