"""Framework-independent AgentRuntime Protocol and Pydantic v2 contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Literal, Protocol, runtime_checkable
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from engines.antigravity.diagnostics import redact_secrets
from engines.contract import EngineEvent, EngineResult
from engines.verification import EngineArtifactVerificationResult

_FORBIDDEN_METADATA_FRAGMENTS: tuple[str, ...] = (
    "secret",
    "password",
    "api_key",
    "apikey",
    "token",
    "credential",
    "authorization",
    "raw_key",
    "x-goog-api-key",
)


def _reject_blank_or_secret(value: str, field_name: str) -> str:
    """Validate that a required string is non-blank and free of known secret patterns."""
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be blank.")
    if redact_secrets(cleaned) != cleaned:
        raise ValueError(f"{field_name} must never contain credentials or secret material.")
    return cleaned


class AgentRuntimeInfo(BaseModel):
    """Descriptive capability and boundary metadata reported by an ``AgentRuntime``."""

    model_config = ConfigDict(extra="forbid")

    runtime_id: str = Field(min_length=1, max_length=100)
    runtime_kind: Literal["custom_orchestrator", "fake", "adapter"]
    version: str = Field(min_length=1, max_length=32)
    supports_streaming_events: bool
    supports_independent_qa: bool
    supports_workflow_cancellation: bool
    cancellation_boundary_note: str = Field(min_length=1, max_length=500)


class WorkspaceScope(BaseModel):
    """Explicit bounded workspace and path permissions passed to an ``AgentRuntime``."""

    model_config = ConfigDict(extra="forbid")

    allowed_root_path: str = Field(min_length=1)
    orchestrator_workspace_path: str = Field(min_length=1)
    worker_workspace_path: str = Field(min_length=1)
    permitted_read_paths: list[str] = Field(min_length=1, max_length=20)
    permitted_write_paths: list[str] = Field(min_length=1, max_length=10)

    @field_validator(
        "allowed_root_path",
        "orchestrator_workspace_path",
        "worker_workspace_path",
    )
    @classmethod
    def _validate_dir_paths(cls, value: str) -> str:
        """Reject blank workspace path fields or credential strings."""
        return _reject_blank_or_secret(value, "workspace_path")

    @field_validator("permitted_read_paths", "permitted_write_paths")
    @classmethod
    def _validate_relpath_lists(cls, values: list[str]) -> list[str]:
        """Reject empty or secret-bearing path list entries."""
        return [_reject_blank_or_secret(item, "permitted_path") for item in values]


class ExecutionRouting(BaseModel):
    """Explicit provider, connection, and model routing without silent fallbacks."""

    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(min_length=1, max_length=64)
    connection_id: str | None = None
    model_id: str | None = None

    @field_validator("provider_id")
    @classmethod
    def _validate_provider_id(cls, value: str) -> str:
        """Ensure ``provider_id`` is explicitly specified and non-secret."""
        return _reject_blank_or_secret(value, "provider_id")

    @field_validator("connection_id", "model_id")
    @classmethod
    def _validate_optional_ids(cls, value: str | None) -> str | None:
        """Reject blank or secret-bearing optional routing identifiers."""
        if value is None:
            return None
        return _reject_blank_or_secret(value, "routing_identifier")


class ArtifactAcceptanceSpec(BaseModel):
    """Declarative acceptance contract used for independent disk QA verification."""

    model_config = ConfigDict(extra="forbid")

    artifact_relpath: str = Field(min_length=1, max_length=260)
    expected_content: str | None = None
    required_substrings: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("artifact_relpath")
    @classmethod
    def _validate_artifact_relpath(cls, value: str) -> str:
        """Ensure target artifact relative path is non-blank and secret-free."""
        return _reject_blank_or_secret(value, "artifact_relpath")

    @field_validator("required_substrings")
    @classmethod
    def _validate_substrings(cls, values: list[str]) -> list[str]:
        """Reject empty substrings in QA acceptance checks."""
        if any(not item for item in values):
            raise ValueError("required_substrings must not contain empty strings.")
        return values


class AgentRuntimeRequest(BaseModel):
    """Framework-agnostic execution request sent from callers to ``AgentRuntime``."""

    model_config = ConfigDict(extra="forbid")

    invocation_id: str = Field(
        default_factory=lambda: f"inv-{uuid.uuid4().hex[:12]}",
        min_length=1,
        max_length=80,
    )
    user_objective: str = Field(min_length=1, max_length=8000)
    workspace_scope: WorkspaceScope
    routing: ExecutionRouting
    acceptance: ArtifactAcceptanceSpec
    timeout_seconds: int = Field(default=90, ge=1, le=600)
    orchestrator_session_id: str | None = None
    worker_session_id: str | None = None
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @field_validator("invocation_id", "user_objective")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        """Reject blank invocation IDs or objectives."""
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Required request text field must not be blank.")
        return cleaned

    @field_validator("metadata")
    @classmethod
    def _validate_metadata(
        cls,
        value: dict[str, str | int | float | bool | None],
    ) -> dict[str, str | int | float | bool | None]:
        """Reject credential-bearing metadata keys or secret-like values."""
        for key, item in value.items():
            lowered = key.strip().lower()
            if any(frag in lowered for frag in _FORBIDDEN_METADATA_FRAGMENTS):
                raise ValueError(f"metadata must never contain secret key '{key}'.")
            if isinstance(item, str) and redact_secrets(item) != item:
                raise ValueError(f"metadata value for '{key}' appears to contain a secret.")
        return value


class AgentRuntimeEvent(BaseModel):
    """Normalized execution event carrying role, session, and provider attribution."""

    model_config = ConfigDict(extra="forbid")

    invocation_id: str = Field(min_length=1)
    runtime_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    stage: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    actor_role: Literal["orchestrator", "worker", "qa_verifier", "runtime"]
    logical_session_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    provider_conversation_id: str | None = None
    engine_event: EngineEvent | None = None


class AgentRuntimeArtifactRef(BaseModel):
    """Artifact reference summarizing disk presence, SHA-256 digest, and QA verdict."""

    model_config = ConfigDict(extra="forbid")

    artifact_relpath: str = Field(min_length=1)
    artifact_abs_path: str | None = None
    exists_on_disk: bool
    sha256: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    qa_status: Literal["VERIFIED_PASS", "VERIFIED_FAIL", "UNVERIFIED"]


class AgentRuntimeTelemetry(BaseModel):
    """Honest runtime telemetry preserving unavailable provider metrics as None."""

    model_config = ConfigDict(extra="forbid")

    total_duration_ms: int = Field(ge=0)
    provider_duration_ms: int = Field(default=0, ge=0)
    engine_call_count: int = Field(default=0, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    thinking_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    actual_cost_usd: float | None = Field(default=None, ge=0)
    telemetry_status: Literal["VERIFIED_MEASURED", "UNAVAILABLE_FROM_PROVIDER"]
    telemetry_source: Literal["provider_cli", "provider_api", "unavailable"]


class AgentRuntimeError(BaseModel):
    """Secret-scrubbed error returned by an ``AgentRuntime`` instead of raw exceptions."""

    model_config = ConfigDict(extra="forbid")

    error_code: str = Field(min_length=1, max_length=64)
    stage: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=2000)
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("message")
    @classmethod
    def _scrub_error_message(cls, value: str) -> str:
        """Redact potential credential formats before surfacing errors to callers."""
        return redact_secrets(value)


class AgentRuntimeResult(BaseModel):
    """Normalized ``AgentRuntime`` result keeping technical state distinct from QA truth."""

    model_config = ConfigDict(extra="forbid")

    runtime_id: str = Field(min_length=1)
    invocation_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    status: Literal["DELIVERED", "FAILED", "BLOCKED", "REJECTED_BY_QA"]
    technical_execution_succeeded: bool
    business_verified: bool
    actors_executed: list[str] = Field(default_factory=list)
    orchestrator_session_id: str | None = None
    worker_session_id: str | None = None
    provider_conversation_ids: list[str] = Field(default_factory=list)
    observed_tool_names: list[str] = Field(default_factory=list)
    engine_results: list[EngineResult] = Field(default_factory=list)
    artifact: AgentRuntimeArtifactRef | None = None
    qa_verification: EngineArtifactVerificationResult | None = None
    final_summary: str | None = None
    telemetry: AgentRuntimeTelemetry
    events: list[AgentRuntimeEvent] = Field(default_factory=list)
    error: AgentRuntimeError | None = None
    started_at: datetime
    finished_at: datetime
    duration_ms: int = Field(ge=0)


@runtime_checkable
class AgentRuntime(Protocol):
    """Framework-independent abstraction executed by application and Core workflows."""

    def get_runtime_info(self) -> AgentRuntimeInfo:
        """Return descriptive capabilities and boundary limits of this runtime."""

    def execute(
        self,
        request: AgentRuntimeRequest,
        *,
        on_event: Callable[[AgentRuntimeEvent], None] | None = None,
    ) -> AgentRuntimeResult:
        """Execute one orchestration workflow bounded by ``request`` and QA rules."""
