"""Phase 09 / P09.05 IT & R&D Department: isolated workspace code authoring and sandbox test runs."""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.contracts.identifiers import (
    EventId,
    RunId,
    Sha256Hex,
    TaskId,
    ensure_not_secret,
    validate_aware_datetime,
    validate_safe_relative_path,
)
from core.departments._common import (
    DepartmentBehaviorError,
    ToolInvokerPort,
    append_department_event,
    authorize_and_audit,
    event_actor_id,
    invoke_tool_checked,
)
from core.events.journal import EventJournal
from core.governance.manager import PermissionManager
from core.governance.models import TrustedAuthContext, actor_kind_to_event_source

_PROTECTED_PRODUCTION_TOP_DIRS: frozenset[str] = frozenset(
    {
        "core",
        "engines",
        "agents",
        "storage",
        "tools",
        "skills",
        "api",
        "ui",
        "docs",
        "tests",
        ".git",
        ".runtime",
        ".ssh",
        "secrets",
    }
)
_PROTECTED_PRODUCTION_FILES: frozenset[str] = frozenset(
    {"agents.md", "pyproject.toml", "progress.md", "readme.md", ".env", ".gitignore"}
)


class ItProductionMutationError(DepartmentBehaviorError):
    """Raised fail-closed when IT/R&D attempts to touch production/protected repository paths."""


def _assert_isolated_workspace_relpath(rel_path: str) -> str:
    """Validate safe workspace path strictly outside protected production directories."""
    cleaned = validate_safe_relative_path(rel_path, "relative_path")
    parts = PurePosixPath(cleaned).parts
    if not parts:
        raise ItProductionMutationError("INVALID_WORKSPACE_PATH", "Target path must not be empty.")
    first = parts[0].lower()
    if first in _PROTECTED_PRODUCTION_TOP_DIRS or first in _PROTECTED_PRODUCTION_FILES:
        raise ItProductionMutationError(
            "PRODUCTION_MUTATION_FORBIDDEN",
            (
                f"IT/R&D cannot modify protected production path '{cleaned}'; "
                "all code changes must remain inside isolated workspace/ or sandbox/."
            ),
        )
    if any(seg.lower() in _PROTECTED_PRODUCTION_TOP_DIRS for seg in parts[:1]):
        raise ItProductionMutationError(
            "PRODUCTION_MUTATION_FORBIDDEN",
            f"Protected production path '{cleaned}' is blocked.",
        )
    return cleaned


class WorkspaceFileSpec(BaseModel):
    """One source or test file authored inside an isolated task workspace."""

    model_config = ConfigDict(extra="forbid")

    relative_path: str = Field(min_length=1, max_length=260)
    content: str = Field(max_length=5_000_000)
    overwrite: bool = False

    @field_validator("relative_path")
    @classmethod
    def _validate_rel(cls, value: str) -> str:
        return _assert_isolated_workspace_relpath(value)

    @field_validator("content")
    @classmethod
    def _validate_content(cls, value: str) -> str:
        return ensure_not_secret(value, "content") if value.strip() else value


class WorkspaceCodeAuthoringRequest(BaseModel):
    """Request to write code/test files strictly inside an isolated Task workspace."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    run_id: RunId | None = None
    workspace_root: str = Field(min_length=1, max_length=512)
    files: list[WorkspaceFileSpec] = Field(min_length=1, max_length=32)
    timestamp: datetime

    @field_validator("workspace_root")
    @classmethod
    def _validate_root(cls, value: str) -> str:
        return ensure_not_secret(value, "workspace_root")

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class WrittenWorkspaceFileOutcome(BaseModel):
    """Real metadata captured after writing one workspace file via ``ToolRuntime``."""

    model_config = ConfigDict(extra="forbid")

    relative_path: str
    size_bytes: int = Field(ge=0)
    sha256: Sha256Hex
    overwritten: bool


class WorkspaceCodeAuthoringOutcome(BaseModel):
    """Outcome of isolated workspace project/code creation (always UNVERIFIED until QA)."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    run_id: RunId | None = None
    written_files: list[WrittenWorkspaceFileOutcome]
    business_verification_status: Literal["UNVERIFIED"] = "UNVERIFIED"
    audit_event_id: EventId


class SandboxTestExecutionRequest(BaseModel):
    """Request to run a script or pytest suite strictly in isolated Docker sandbox."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    run_id: RunId | None = None
    workspace_root: str = Field(min_length=1, max_length=512)
    operation: Literal["python_script", "pytest"] = "pytest"
    target_relpath: str = Field(min_length=1, max_length=260)
    args: list[str] = Field(default_factory=list, max_length=16)
    timeout_seconds: int = Field(default=60, ge=1, le=120)
    timestamp: datetime

    @field_validator("workspace_root")
    @classmethod
    def _validate_root(cls, value: str) -> str:
        return ensure_not_secret(value, "workspace_root")

    @field_validator("target_relpath")
    @classmethod
    def _validate_target(cls, value: str) -> str:
        return _assert_isolated_workspace_relpath(value)

    @field_validator("args")
    @classmethod
    def _validate_args(cls, values: list[str]) -> list[str]:
        return [ensure_not_secret(item, "args") for item in values]

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class SandboxTestExecutionOutcome(BaseModel):
    """Measured isolated sandbox execution log without overriding QA authority."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    run_id: RunId | None = None
    operation: Literal["python_script", "pytest"]
    target_relpath: str
    exit_code: int
    exit_state: str
    sandbox_passed: bool
    stdout: str
    stderr: str
    duration_ms: int = Field(ge=0)
    evidence_refs: list[str] = Field(default_factory=list)
    business_verification_status: Literal["UNVERIFIED"] = "UNVERIFIED"
    audit_event_id: EventId


class ItRdDepartmentService:
    """IT & R&D service managing sandboxed project creation and isolated automated test execution."""

    def __init__(
        self,
        *,
        permission_manager: PermissionManager,
        journal: EventJournal,
        tool_invoker: ToolInvokerPort,
    ) -> None:
        """Bind IT/R&D service to Governance, EventJournal, and ToolRuntime."""
        self._pm = permission_manager
        self._journal = journal
        self._tools = tool_invoker

    def write_isolated_files(
        self,
        request: WorkspaceCodeAuthoringRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> WorkspaceCodeAuthoringOutcome:
        """Write project/code/test files exclusively within isolated task workspace."""
        written: list[WrittenWorkspaceFileOutcome] = []
        for spec in request.files:
            rel = _assert_isolated_workspace_relpath(spec.relative_path)
            authorize_and_audit(
                self._pm,
                auth_context=auth_context,
                action="files.write",
                resource=f"it:workspace:{request.task_id}/{rel}",
                task_id=request.task_id,
                run_id=request.run_id,
                workspace_root=request.workspace_root,
                target_path=rel,
                timestamp=request.timestamp,
                correlation_id=f"it-write-{request.task_id}",
            )
            res = invoke_tool_checked(
                self._tools,
                auth_context=auth_context,
                tool_id="tools.files.write",
                parameters={
                    "path": rel,
                    "content": spec.content,
                    "overwrite": spec.overwrite,
                },
                task_id=request.task_id,
                run_id=request.run_id,
                workspace_root=request.workspace_root,
                target_path=rel,
                timestamp=request.timestamp,
                correlation_id=f"it-write-{request.task_id}",
            )
            written.append(
                WrittenWorkspaceFileOutcome(
                    relative_path=str(res.data.get("path", rel)),
                    size_bytes=int(res.data["size_bytes"]),
                    sha256=str(res.data["sha256"]),
                    overwritten=bool(res.data.get("overwritten", False)),
                )
            )

        ev = append_department_event(
            self._journal,
            event_type="artifact.recorded",
            source=actor_kind_to_event_source(auth_context.actor_kind),
            task_id=request.task_id,
            run_id=request.run_id,
            actor_id=event_actor_id(auth_context),
            timestamp=request.timestamp,
            correlation_id=f"it-write-{request.task_id}",
            payload={
                "department": "it",
                "files_written": [w.relative_path for w in written],
                "sha256_list": [w.sha256 for w in written],
                "business_verification_status": "UNVERIFIED",
            },
        )
        return WorkspaceCodeAuthoringOutcome(
            task_id=request.task_id,
            run_id=request.run_id,
            written_files=written,
            audit_event_id=ev.event_id,
        )

    def run_sandbox_tests(
        self,
        request: SandboxTestExecutionRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> SandboxTestExecutionOutcome:
        """Execute isolated pytest/script via ``tools.code.run_sandbox`` preserving exact exit & logs."""
        target = _assert_isolated_workspace_relpath(request.target_relpath)
        authorize_and_audit(
            self._pm,
            auth_context=auth_context,
            action="code.execute",
            resource=f"it:sandbox:{request.task_id}/{target}",
            task_id=request.task_id,
            run_id=request.run_id,
            workspace_root=request.workspace_root,
            target_path=target,
            timestamp=request.timestamp,
            correlation_id=f"it-test-{request.task_id}",
        )
        params: dict[str, object] = {
            "operation": request.operation,
            "args": list(request.args),
            "timeout_seconds": request.timeout_seconds,
        }
        if request.operation == "python_script":
            params["script_path"] = target
        else:
            params["test_path"] = target

        res = invoke_tool_checked(
            self._tools,
            auth_context=auth_context,
            tool_id="tools.code.run_sandbox",
            parameters=params,
            task_id=request.task_id,
            run_id=request.run_id,
            workspace_root=request.workspace_root,
            target_path=target,
            timestamp=request.timestamp,
            correlation_id=f"it-test-{request.task_id}",
        )

        exit_code = int(res.data["exit_code"])
        exit_state = str(res.data["exit_state"])
        passed = bool(res.data["passed"])
        ev = append_department_event(
            self._journal,
            event_type="tool.call_completed",
            source=actor_kind_to_event_source(auth_context.actor_kind),
            task_id=request.task_id,
            run_id=request.run_id,
            actor_id=event_actor_id(auth_context),
            evidence_id=res.evidence_refs[0] if res.evidence_refs else None,
            timestamp=request.timestamp,
            correlation_id=f"it-test-{request.task_id}",
            payload={
                "department": "it",
                "operation": request.operation,
                "target_relpath": target,
                "exit_code": exit_code,
                "exit_state": exit_state,
                "sandbox_passed": passed,
                "duration_ms": int(res.data["duration_ms"]),
                "business_verification_status": "UNVERIFIED",
            },
        )
        return SandboxTestExecutionOutcome(
            task_id=request.task_id,
            run_id=request.run_id,
            operation=request.operation,
            target_relpath=target,
            exit_code=exit_code,
            exit_state=exit_state,
            sandbox_passed=passed,
            stdout=str(res.data.get("stdout", "")),
            stderr=str(res.data.get("stderr", "")),
            duration_ms=int(res.data["duration_ms"]),
            evidence_refs=list(res.evidence_refs),
            audit_event_id=ev.event_id,
        )
