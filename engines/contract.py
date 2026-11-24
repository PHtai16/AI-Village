"""Provider-agnostic Universal Engine Contract for AI Village."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
import re
from typing import TYPE_CHECKING, Any, Callable, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from engines.antigravity.diagnostics import redact_secrets

if TYPE_CHECKING:
    from engines.connections import EngineConnectionRef

_SNAKE_ID_RE = re.compile(r"^[a-z0-9_]+$")
_FORBIDDEN_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "apikey",
        "raw_key",
        "secret",
        "token",
        "password",
        "credential",
        "credentials",
        "authorization",
        "x-goog-api-key",
    }
)

CapabilityStatus = Literal["SUPPORTED", "UNSUPPORTED", "UNKNOWN", "NOT_VERIFIED"]
CapabilityName = Literal[
    "text_generation",
    "structured_output",
    "streaming",
    "tool_calling_proposal",
    "native_tool_execution",
    "workspace_access",
    "session_continuation",
    "cancellation",
    "usage_telemetry",
    "model_discovery",
]
EnginePlatformKind = Literal["agent_execution_platform", "model_api"]
EngineExecutionStatus = Literal["SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED", "BLOCKED"]
EngineEventSource = Literal["provider", "adapter", "tool_runtime"]
EngineEventType = Literal[
    "execution_started",
    "text_delta",
    "tool_call_proposed",
    "tool_execution_observed",
    "provider_error",
    "execution_completed",
    "process_cancelled",
    "process_timed_out",
    "adapter_status",
]
EngineTelemetryStatus = Literal[
    "VERIFIED_MEASURED",
    "UNAVAILABLE_FROM_PROVIDER",
    "ESTIMATED_PENDING_AUDIT",
]
EngineTelemetrySource = Literal[
    "provider_cli",
    "provider_api",
    "adapter_measured",
    "unavailable",
]
EngineErrorCode = Literal[
    "AUTH_ERROR",
    "CREDENTIAL_UNAVAILABLE",
    "RATE_LIMITED",
    "QUOTA_EXCEEDED",
    "INVALID_MODEL",
    "UNSUPPORTED_CAPABILITY",
    "TOOL_UNSUPPORTED_BY_PROVIDER",
    "CONNECTION_NOT_FOUND",
    "CONNECTION_DISABLED",
    "DATA_BOUNDARY_VIOLATION",
    "NETWORK_ERROR",
    "TIMED_OUT",
    "INVALID_RESPONSE",
    "PROVIDER_ERROR",
    "CANCELLED",
    "EXECUTION_BLOCKED",
    "UNSUPPORTED_ARGUMENT",
    "PROCESS_LAUNCH_ERROR",
]
EngineErrorSource = Literal[
    "provider",
    "cli",
    "adapter",
    "capability",
    "cancellation",
    "credential",
    "network",
    "validation",
]


def _validate_identifier(value: str | None, field_name: str) -> str | None:
    """Validate optional snake_case identifiers and reject secret-like strings."""
    if value is None:
        return None
    cleaned = value.strip()
    if not _SNAKE_ID_RE.fullmatch(cleaned):
        raise ValueError(f"{field_name} must match ^[a-z0-9_]+$, got {value!r}.")
    if redact_secrets(cleaned) != cleaned or cleaned.startswith("ghp_"):
        raise ValueError(f"{field_name} must never contain a credential or API key.")
    return cleaned


class EngineError(BaseModel):
    """Normalized, secret-redacted error returned by any engine or provider adapter."""

    model_config = ConfigDict(extra="forbid")

    error_code: EngineErrorCode
    error_source: EngineErrorSource
    message: str = Field(min_length=1, max_length=1000)
    retryable: bool = False
    retry_after_seconds: float | None = Field(default=None, ge=0)
    provider_id: str | None = None
    connection_id: str | None = None
    http_status: int | None = None
    exit_code: int | None = None
    redacted_details: str = ""

    @field_validator("message", "redacted_details")
    @classmethod
    def _scrub_secret_text(cls, value: str) -> str:
        """Redact known credential formats from error text."""
        return redact_secrets(value)


class EngineCapabilities(BaseModel):
    """Explicit capability matrix distinguishing model APIs from agent execution platforms."""

    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(min_length=1)
    adapter_id: str = Field(min_length=1)
    platform_kind: EnginePlatformKind
    text_generation: CapabilityStatus = "UNKNOWN"
    structured_output: CapabilityStatus = "UNKNOWN"
    streaming: CapabilityStatus = "UNKNOWN"
    tool_calling_proposal: CapabilityStatus = "UNKNOWN"
    native_tool_execution: CapabilityStatus = "UNSUPPORTED"
    workspace_access: CapabilityStatus = "UNSUPPORTED"
    session_continuation: CapabilityStatus = "UNSUPPORTED"
    cancellation: CapabilityStatus = "UNSUPPORTED"
    usage_telemetry: CapabilityStatus = "UNKNOWN"
    model_discovery: CapabilityStatus = "NOT_VERIFIED"
    requires_authentication: bool = True
    data_residency: Literal["local", "cloud", "cli_managed", "unknown"] = "unknown"
    evidence_notes: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _enforce_model_api_boundaries(self) -> EngineCapabilities:
        """Prevent pure Model APIs from claiming native host tool or workspace execution."""
        if self.platform_kind == "model_api":
            if self.native_tool_execution == "SUPPORTED":
                raise ValueError(
                    "Model APIs must not declare native_tool_execution as SUPPORTED."
                )
            if self.workspace_access == "SUPPORTED":
                raise ValueError(
                    "Model APIs must not declare workspace_access as SUPPORTED."
                )
        return self

    def get_capability(self, name: CapabilityName) -> CapabilityStatus:
        """Return the current verification status of a named capability."""
        return getattr(self, name)

    def to_evidence_map(self) -> dict[str, CapabilityStatus]:
        """Serialize the ten standard engine capabilities into a status dictionary."""
        return {
            "text_generation": self.text_generation,
            "structured_output": self.structured_output,
            "streaming": self.streaming,
            "tool_calling_proposal": self.tool_calling_proposal,
            "native_tool_execution": self.native_tool_execution,
            "workspace_access": self.workspace_access,
            "session_continuation": self.session_continuation,
            "cancellation": self.cancellation,
            "usage_telemetry": self.usage_telemetry,
            "model_discovery": self.model_discovery,
        }


class EngineSessionReference(BaseModel):
    """Session tracking reference keeping AI Village sessions distinct from provider threads."""

    model_config = ConfigDict(extra="forbid")

    session_id: str | None = None
    provider_conversation_id: str | None = None
    binding_source: Literal["none", "provider_cli", "provider_api", "adapter_logical"] = "none"


class EngineToolCallProposal(BaseModel):
    """Structured tool call proposal emitted by a model without local execution."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str | None = None
    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    executed: Literal[False] = False
    source: Literal["model_api_proposal", "provider_cli_observed"] = "model_api_proposal"


class EngineTelemetry(BaseModel):
    """Honest runtime telemetry leaving unavailable provider metrics as None."""

    model_config = ConfigDict(extra="forbid")

    duration_ms: int = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    thinking_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    provider_duration_seconds: float | None = Field(default=None, ge=0)
    provider_reported_usage: dict[str, Any] | None = None
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    actual_cost_usd: float | None = Field(default=None, ge=0)
    telemetry_status: EngineTelemetryStatus
    telemetry_source: EngineTelemetrySource


class EngineEvent(BaseModel):
    """Normalized execution event with explicit attribution of provider versus adapter source."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    sequence_number: int = Field(ge=1)
    timestamp: datetime
    event_type: EngineEventType
    source: EngineEventSource
    provider_id: str = Field(min_length=1)
    connection_id: str | None = None
    status: Literal[
        "STARTED",
        "IN_PROGRESS",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "TIMED_OUT",
        "UNKNOWN",
    ]
    text_delta: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    redacted_payload: dict[str, Any] = Field(default_factory=dict)
    telemetry: EngineTelemetry | None = None


class EngineRequest(BaseModel):
    """Provider-neutral execution request carrying non-secret metadata only."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(default_factory=lambda: f"req-{uuid.uuid4().hex[:12]}")
    provider_id: str | None = None
    adapter_id: str | None = None
    connection_id: str | None = None
    model_id: str | None = None
    prompt: str = Field(min_length=1, max_length=32000)
    context: str | None = None
    workspace_path: str | None = None
    timeout_seconds: int = Field(default=60, ge=1, le=600)
    capabilities_required: list[CapabilityName] = Field(
        default_factory=lambda: ["text_generation"]
    )
    execution_mode: Literal["text", "structured", "accept-edits", "plan"] = "text"
    data_locality: Literal["default", "local_only"] = "default"
    response_schema: dict[str, Any] | None = None
    tool_declarations: list[dict[str, Any]] = Field(default_factory=list)
    session_reference: EngineSessionReference | None = None
    denied_paths: list[str] = Field(default_factory=list)
    deny_shell_commands: bool = False
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @field_validator("provider_id", "adapter_id", "connection_id")
    @classmethod
    def _validate_snake_ids(cls, value: str | None, info: Any) -> str | None:
        """Validate snake_case provider, adapter, and connection identifiers."""
        return _validate_identifier(value, info.field_name)

    @field_validator("prompt")
    @classmethod
    def _prompt_not_blank(cls, value: str) -> str:
        """Reject empty or whitespace-only prompts."""
        if not value.strip():
            raise ValueError("Prompt must not be empty or whitespace-only.")
        return value

    @field_validator("metadata")
    @classmethod
    def _validate_safe_metadata(
        cls,
        value: dict[str, str | int | float | bool | None],
    ) -> dict[str, str | int | float | bool | None]:
        """Reject secret-bearing keys or credential strings in request metadata."""
        for key, item in value.items():
            normalized_key = key.strip().lower()
            if normalized_key in _FORBIDDEN_METADATA_KEYS or any(
                frag in normalized_key for frag in ("secret", "password", "api_key", "token")
            ):
                raise ValueError(f"metadata must never contain secret key '{key}'.")
            if isinstance(item, str) and redact_secrets(item) != item:
                raise ValueError(f"metadata value for '{key}' appears to contain a secret.")
        return value


class EngineResult(BaseModel):
    """Normalized execution outcome separating technical completion from business verification."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    adapter_id: str = Field(min_length=1)
    connection_id: str | None = None
    model_id: str | None = None
    platform_kind: EnginePlatformKind
    status: EngineExecutionStatus
    business_verification_status: Literal["UNVERIFIED"] = "UNVERIFIED"
    response_text: str | None = None
    structured_output: dict[str, Any] | list[Any] | None = None
    tool_call_proposals: list[EngineToolCallProposal] = Field(default_factory=list)
    observed_tool_names: list[str] = Field(default_factory=list)
    session_reference: EngineSessionReference | None = None
    started_at: datetime
    finished_at: datetime
    duration_ms: int = Field(ge=0)
    telemetry: EngineTelemetry
    events: list[EngineEvent] = Field(default_factory=list)
    error: EngineError | None = None
    capability_evidence: dict[str, CapabilityStatus] = Field(default_factory=dict)


class EngineAdapter(ABC):
    """Abstract base contract implemented by every AI Engine or Model API adapter."""

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Return the normalized provider identifier served by this adapter."""

    @property
    @abstractmethod
    def adapter_id(self) -> str:
        """Return the unique adapter identifier registered in ``EngineRegistry``."""

    @property
    @abstractmethod
    def platform_kind(self) -> EnginePlatformKind:
        """Classify whether this adapter runs an Agent Execution Platform or Model API."""

    @abstractmethod
    def get_capabilities(
        self,
        connection: EngineConnectionRef | None = None,
    ) -> EngineCapabilities:
        """Inspect and report truthful capability statuses for the adapter."""

    @abstractmethod
    def execute(
        self,
        request: EngineRequest,
        *,
        connection: EngineConnectionRef | None = None,
        on_event: Callable[[EngineEvent], None] | None = None,
    ) -> EngineResult:
        """Execute a provider-neutral request and return a normalized ``EngineResult``."""

    def required_capabilities_for_request(self, request: EngineRequest) -> list[CapabilityName]:
        """Determine the ordered deduplicated capabilities required by ``request``."""
        ordered: list[CapabilityName] = []
        seen: set[CapabilityName] = set()

        def _add(cap: CapabilityName) -> None:
            if cap not in seen:
                seen.add(cap)
                ordered.append(cap)

        for cap in request.capabilities_required:
            _add(cap)
        if request.execution_mode == "structured" or request.response_schema is not None:
            _add("structured_output")
        if request.execution_mode in ("accept-edits", "plan"):
            _add("native_tool_execution")
            _add("workspace_access")
        if request.tool_declarations:
            _add("tool_calling_proposal")
        if request.session_reference and request.session_reference.provider_conversation_id:
            _add("session_continuation")
        return ordered

    def validate_request_capabilities(
        self,
        request: EngineRequest,
        capabilities: EngineCapabilities,
        connection_id: str | None = None,
    ) -> EngineError | None:
        """Validate data locality and capability requirements fail-closed before calling out."""
        if request.data_locality == "local_only" and capabilities.data_residency != "local":
            return EngineError(
                error_code="DATA_BOUNDARY_VIOLATION",
                error_source="capability",
                message=(
                    f"Provider '{self.provider_id}' has residency '{capabilities.data_residency}', "
                    "which cannot satisfy local_only data boundary."
                ),
                retryable=False,
                provider_id=self.provider_id,
                connection_id=connection_id,
            )

        for cap in self.required_capabilities_for_request(request):
            status = capabilities.get_capability(cap)
            if status == "SUPPORTED":
                continue
            if cap in ("native_tool_execution", "workspace_access") and (
                capabilities.platform_kind == "model_api"
            ):
                return EngineError(
                    error_code="TOOL_UNSUPPORTED_BY_PROVIDER",
                    error_source="capability",
                    message=(
                        f"Model API provider '{self.provider_id}' does not support '{cap}' "
                        f"(status={status}); local tools require an Agent Execution Platform or Tool Runtime."
                    ),
                    retryable=False,
                    provider_id=self.provider_id,
                    connection_id=connection_id,
                )
            return EngineError(
                error_code="UNSUPPORTED_CAPABILITY",
                error_source="capability",
                message=(
                    f"Provider '{self.provider_id}' does not satisfy required capability "
                    f"'{cap}' (status={status})."
                ),
                retryable=False,
                provider_id=self.provider_id,
                connection_id=connection_id,
            )
        return None

    def build_blocked_result(
        self,
        request: EngineRequest,
        error: EngineError,
        *,
        capabilities: EngineCapabilities | None = None,
        connection_id: str | None = None,
        model_id: str | None = None,
    ) -> EngineResult:
        """Build a standardized fail-closed ``BLOCKED`` result without invoking external I/O."""
        now = datetime.now(timezone.utc)
        return EngineResult(
            request_id=request.request_id,
            provider_id=self.provider_id,
            adapter_id=self.adapter_id,
            connection_id=connection_id or request.connection_id,
            model_id=model_id or request.model_id,
            platform_kind=self.platform_kind,
            status="BLOCKED",
            response_text=None,
            session_reference=request.session_reference,
            started_at=now,
            finished_at=now,
            duration_ms=0,
            telemetry=EngineTelemetry(
                duration_ms=0,
                telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                telemetry_source="unavailable",
            ),
            events=[],
            error=error,
            capability_evidence=capabilities.to_evidence_map() if capabilities else {},
        )
