"""Pure Pydantic v2 contracts and Repository protocol for Phase 07 Tool Registry and Runtime."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Callable, Literal, Protocol, runtime_checkable
import uuid

from jsonschema import Draft202012Validator, SchemaError as JsonSchemaError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.contracts.identifiers import (
    CapabilityId,
    RunId,
    SemVer,
    SessionId,
    TaskId,
    ensure_not_secret,
    sanitize_payload,
    validate_aware_datetime,
    validate_capability_id,
    validate_safe_payload,
    validate_semver,
    validate_tool_id_syntax,
    validate_unique_id_list,
)
from core.governance.models import TrustedAuthContext
from engines.antigravity.diagnostics import redact_secrets

ToolCategory = Literal["files", "code", "browser", "research", "office"]
ToolRiskLevel = Literal["LOW", "MEDIUM", "HIGH"]
ToolExecutionMode = Literal["sandbox_only", "restricted_local", "approved_external"]
ToolStatus = Literal["AVAILABLE", "DEGRADED", "DISABLED"]
ToolResultStatus = Literal["SUCCESS", "FAILED", "DENIED", "APPROVAL_REQUIRED", "TIMEOUT", "CANCELLED"]

_ACTION_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_TEX_ID_RE = re.compile(r"^tex_[a-z0-9][a-z0-9_-]{3,63}$")


def new_tool_execution_id() -> str:
    """Generate a unique canonical Tool execution identifier (`tex_*`)."""
    return f"tex_{uuid.uuid4().hex[:16]}"


def _validate_tex_id(value: str) -> str:
    cleaned = ensure_not_secret(value, "execution_id")
    if not _TEX_ID_RE.fullmatch(cleaned):
        raise ValueError(f"Invalid tool execution_id format: {value!r}")
    return cleaned


class ToolExecutionError(RuntimeError):
    """Structured fail-closed exception raised by concrete tool handlers."""

    def __init__(self, error_code: str, message: str, *, retryable: bool = False) -> None:
        cleaned = redact_secrets(message)[:500]
        super().__init__(cleaned)
        self.error_code = error_code
        self.message = cleaned
        self.retryable = retryable


class ToolCancelledError(ToolExecutionError):
    """Raised when tool execution is cancelled cooperatively."""

    def __init__(self, message: str = "Tool execution cancelled.") -> None:
        super().__init__("CANCELLED", message, retryable=False)


class ToolCostProfile(BaseModel):
    """Transparent tool invocation cost profile without fabricated metrics."""

    model_config = ConfigDict(extra="forbid")

    unit: str = Field(min_length=1, max_length=64)
    estimated_unit_cost_usd: float | None = Field(default=None, ge=0.0)
    notes: str | None = Field(default=None, max_length=256)

    @field_validator("unit", "notes")
    @classmethod
    def _validate_text(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "cost_field"))


class McpToolBinding(BaseModel):
    """Optional binding mapping a canonical Tool ID to an allowlisted MCP server tool."""

    model_config = ConfigDict(extra="forbid")

    server_id: str = Field(min_length=1, max_length=64)
    remote_tool_name: str = Field(min_length=1, max_length=128)
    transport: Literal["stdio"] = "stdio"

    @field_validator("server_id", "remote_tool_name")
    @classmethod
    def _validate_labels(cls, value: str, info: object) -> str:
        return ensure_not_secret(value, getattr(info, "field_name", "mcp_field"))


class ToolManifest(BaseModel):
    """Canonical Tool Manifest contract aligned with TOOL_SPEC.md."""

    model_config = ConfigDict(extra="forbid")

    spec_version: SemVer = "1.0.0"
    id: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=3, max_length=80)
    version: SemVer
    category: ToolCategory
    description: str = Field(min_length=3, max_length=500)
    capabilities: list[CapabilityId] = Field(min_length=1, max_length=32)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    permissions_required: list[str] = Field(min_length=1, max_length=16)
    approval_required: bool = False
    risk_level: ToolRiskLevel
    execution_mode: ToolExecutionMode
    timeout_seconds: int = Field(default=30, ge=1, le=600)
    cost_profile: ToolCostProfile
    status: ToolStatus = "AVAILABLE"
    safe_retry: bool = False
    mcp_binding: McpToolBinding | None = None

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return validate_tool_id_syntax(value)

    @field_validator("spec_version", "version")
    @classmethod
    def _validate_versions(cls, value: str) -> str:
        return validate_semver(value)

    @field_validator("name", "description")
    @classmethod
    def _validate_text(cls, value: str, info: object) -> str:
        return ensure_not_secret(value, getattr(info, "field_name", "manifest_field"))

    @field_validator("capabilities")
    @classmethod
    def _validate_caps(cls, values: list[str]) -> list[str]:
        return validate_unique_id_list(values, validate_capability_id, "capabilities")

    @field_validator("permissions_required")
    @classmethod
    def _validate_permissions(cls, values: list[str]) -> list[str]:
        seen: set[str] = set()
        cleaned: list[str] = []
        for raw in values:
            item = ensure_not_secret(raw, "permissions_required")
            if not _ACTION_ID_RE.fullmatch(item):
                raise ValueError(f"Invalid permission Action ID '{item}'.")
            if item not in seen:
                seen.add(item)
                cleaned.append(item)
        return cleaned

    @field_validator("input_schema", "output_schema")
    @classmethod
    def _validate_json_schema(cls, schema: dict[str, Any], info: object) -> dict[str, Any]:
        field_name = getattr(info, "field_name", "schema")
        validate_safe_payload(schema, field_name)
        try:
            Draft202012Validator.check_schema(schema)
        except JsonSchemaError as exc:
            raise ValueError(f"Invalid JSON Schema in {field_name}: {exc.message}") from exc
        return schema

    @model_validator(mode="after")
    def _validate_category_alignment(self) -> ToolManifest:
        id_category = self.id.split(".")[1]
        if id_category != self.category:
            raise ValueError(
                f"Tool ID '{self.id}' category '{id_category}' does not match manifest category '{self.category}'."
            )
        return self


class ToolExecutionContext(BaseModel):
    """Immutable execution context passed from ToolRuntime into tool handlers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: str
    tool_id: str
    actor_id: str
    actor_kind: str
    task_id: TaskId | None = None
    run_id: RunId | None = None
    session_id: SessionId | None = None
    workspace_root: str | None = None
    cross_task_roots: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(ge=1, le=600)
    approved_by_operator: bool = False
    approval_id: str | None = None

    @field_validator("execution_id")
    @classmethod
    def _validate_exec_id(cls, value: str) -> str:
        return _validate_tex_id(value)


class ToolInvocationRequest(BaseModel):
    """Caller request to execute a registered tool through Governance and ToolRuntime."""

    model_config = ConfigDict(extra="forbid")

    auth_context: TrustedAuthContext
    tool_id: str = Field(min_length=1, max_length=128)
    parameters: dict[str, Any] = Field(default_factory=dict)
    task_id: TaskId | None = None
    run_id: RunId | None = None
    session_id: SessionId | None = None
    workspace_root: str | None = None
    target_path: str | None = None
    cross_task_roots: list[str] = Field(default_factory=list, max_length=32)
    approval_id: str | None = None
    correlation_id: str | None = Field(default=None, min_length=1, max_length=80)
    timestamp: datetime

    @field_validator("tool_id", "correlation_id")
    @classmethod
    def _validate_strings(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "request_field"))

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class ToolHandlerOutput(BaseModel):
    """Structured output returned by a concrete tool handler before schema verification."""

    model_config = ConfigDict(extra="forbid")

    data: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    untrusted_content: bool = False
    estimated_cost_usd: float | None = Field(default=None, ge=0.0)


ToolHandler = Callable[[dict[str, Any], ToolExecutionContext], ToolHandlerOutput]


class ToolResult(BaseModel):
    """Canonical normalized tool outcome returned by ToolRuntime."""

    model_config = ConfigDict(extra="forbid")

    execution_id: str
    tool_id: str
    status: ToolResultStatus
    data: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    business_verification_status: Literal["UNVERIFIED"] = "UNVERIFIED"
    untrusted_content: bool = False
    duration_ms: int = Field(ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0.0)
    error_code: str | None = None
    error_message: str | None = Field(default=None, max_length=500)
    audit_event_id: str | None = None
    approval_id: str | None = None

    @field_validator("execution_id")
    @classmethod
    def _validate_exec_id(cls, value: str) -> str:
        return _validate_tex_id(value)

    @field_validator("error_message")
    @classmethod
    def _scrub_error(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return redact_secrets(value)[:500]


class ToolExecutionRecord(BaseModel):
    """Secret-sanitized persistent audit record for one tool invocation."""

    model_config = ConfigDict(extra="forbid")

    execution_id: str
    tool_id: str
    task_id: TaskId | None = None
    run_id: RunId | None = None
    session_id: SessionId | None = None
    actor_id: str = Field(min_length=1, max_length=128)
    action: str = Field(min_length=1, max_length=64)
    status: ToolResultStatus
    business_verification_status: Literal["UNVERIFIED"] = "UNVERIFIED"
    duration_ms: int = Field(ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0.0)
    approval_id: str | None = None
    audit_event_id: str | None = None
    redacted_input: dict[str, Any] = Field(default_factory=dict)
    redacted_output: dict[str, Any] = Field(default_factory=dict)
    artifact_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = Field(default=None, max_length=500)
    executed_at: datetime

    @field_validator("execution_id")
    @classmethod
    def _validate_exec_id(cls, value: str) -> str:
        return _validate_tex_id(value)

    @field_validator("redacted_input", "redacted_output")
    @classmethod
    def _sanitize_io(cls, value: dict[str, Any], info: object) -> dict[str, Any]:
        sanitized = sanitize_payload(value)
        if not isinstance(sanitized, dict):
            raise ValueError("Sanitized tool payload must be a JSON object.")
        validate_safe_payload(sanitized, getattr(info, "field_name", "io_payload"))
        return sanitized

    @field_validator("error_message")
    @classmethod
    def _scrub_err(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return redact_secrets(value)[:500]

    @field_validator("executed_at")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "executed_at")


class ToolUsageBucket(BaseModel):
    """Aggregated tool usage metrics for one Tool ID."""

    model_config = ConfigDict(extra="forbid")

    tool_id: str
    call_count: int = Field(ge=0)
    success_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    denied_count: int = Field(ge=0)
    approval_required_count: int = Field(ge=0)
    timeout_count: int = Field(ge=0)
    total_duration_ms: int = Field(ge=0)
    known_cost_usd: float | None = Field(default=None, ge=0.0)
    unknown_cost_calls: int = Field(ge=0)


class ToolUsageSummary(BaseModel):
    """Tool usage aggregation across Task, Run, or Actor without fabricated cost metrics."""

    model_config = ConfigDict(extra="forbid")

    task_id: str | None = None
    run_id: str | None = None
    actor_id: str | None = None
    total_calls: int = Field(ge=0)
    by_status: dict[str, int] = Field(default_factory=dict)
    total_duration_ms: int = Field(ge=0)
    known_cost_usd: float | None = Field(default=None, ge=0.0)
    unknown_cost_calls: int = Field(ge=0)
    by_tool: list[ToolUsageBucket] = Field(default_factory=list)


@runtime_checkable
class ToolRepository(Protocol):
    """Persistence port for Tool Registry manifests and Tool Execution Records."""

    def upsert_tool(
        self,
        manifest: ToolManifest,
        *,
        registered_at: datetime,
        updated_at: datetime,
    ) -> ToolManifest:
        """Persist or update a registered ToolManifest."""

    def get_tool(self, tool_id: str) -> ToolManifest | None:
        """Fetch a persisted ToolManifest by ID."""

    def list_tools(
        self,
        *,
        category: ToolCategory | None = None,
        status: ToolStatus | None = None,
    ) -> list[ToolManifest]:
        """List registered ToolManifest entries optionally filtered by category/status."""

    def set_tool_status(
        self,
        tool_id: str,
        status: ToolStatus,
        *,
        updated_at: datetime,
    ) -> ToolManifest:
        """Transition a registered tool's availability status."""

    def record_execution(self, record: ToolExecutionRecord) -> ToolExecutionRecord:
        """Append an immutable secret-redacted ToolExecutionRecord."""

    def list_executions(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        actor_id: str | None = None,
        tool_id: str | None = None,
        status: ToolResultStatus | None = None,
        limit: int = 200,
    ) -> list[ToolExecutionRecord]:
        """List persisted execution records matching optional filters."""

    def summarize_usage(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        actor_id: str | None = None,
    ) -> ToolUsageSummary:
        """Aggregate usage counts, durations, and null-safe costs."""
