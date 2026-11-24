"""Pydantic v2 data models for the minimal Antigravity CLI adapter."""

from datetime import datetime
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from engines.antigravity.diagnostics import redact_secrets

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")


class AntigravityDiscoveryResult(BaseModel):
    """Result of locating the Antigravity CLI executable on the local host."""

    found: bool
    executable_path: str | None = None
    source: Literal["explicit", "env", "path", "user_home", "not_found"]
    checked_locations: list[str] = Field(default_factory=list)
    error: str | None = None


class AntigravityCapabilities(BaseModel):
    """Probed version and CLI flags reported by the local Antigravity executable."""

    executable_path: str
    version: str | None = None
    supports_print_mode: bool = False
    supports_json_output: bool = False
    supports_stream_json_flag: bool = False
    supports_print_timeout: bool = False
    supports_sandbox_flag: bool = False
    supports_mode_flag: bool = False
    supports_models_subcommand: bool = False
    supports_conversation_flag: bool = False
    supports_settings_flag: bool = False
    probe_exit_code: int | None = None
    error: str | None = None


class AntigravityTelemetry(BaseModel):
    """Measured execution timing and provider token telemetry."""

    duration_ms: int = Field(ge=0)
    provider_duration_seconds: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None
    cache_read_tokens: int | None = None
    total_tokens: int | None = None
    telemetry_status: Literal["VERIFIED_MEASURED", "UNAVAILABLE_FROM_PROVIDER"]


ProcessLifecycleState = Literal[
    "CREATED",
    "STARTING",
    "RUNNING",
    "CANCEL_REQUESTED",
    "CANCELLING",
    "CANCELLED",
    "TIMED_OUT",
    "SUCCEEDED",
    "FAILED",
]


class AntigravityError(BaseModel):
    """Normalized, secret-redacted error returned by the Antigravity adapter."""

    error_code: Literal[
        "EXECUTABLE_NOT_FOUND",
        "UNSUPPORTED_ARGUMENT",
        "UNSUPPORTED_MODE",
        "SANDBOX_UNVERIFIED",
        "AUTH_ERROR",
        "QUOTA_EXCEEDED",
        "RATE_LIMITED",
        "TIMED_OUT",
        "PROVIDER_ERROR",
        "INVALID_RESPONSE",
        "PROCESS_LAUNCH_ERROR",
        "ARTIFACT_MISSING",
        "ARTIFACT_OUTSIDE_WORKSPACE",
        "CONTENT_MISMATCH",
        "HASH_MISMATCH",
        "SOURCE_MODIFIED",
        "SCOPE_VIOLATION",
        "STREAM_PARSE_ERROR",
        "STREAM_INTERRUPTED",
        "MISSING_COMPLETION_EVENT",
        "DUPLICATE_SESSION_ID",
        "INVALID_SESSION_ID",
        "INVALID_WORKSPACE_BOUNDARY",
        "WORKSPACE_OVERLAP",
        "CROSS_SESSION_CONVERSATION_VIOLATION",
        "CROSS_SESSION_ACCESS_DETECTED",
        "SYMLINK_ESCAPE_REJECTED",
        "CANCELLED",
        "EXECUTION_NOT_FOUND",
        "EXECUTION_CONFLICT",
    ]
    message: str
    retryable: bool = False
    error_source: Literal["cli", "provider", "adapter", "capability", "cancellation"] | None = None
    retry_after_seconds: float | None = Field(default=None, ge=0)
    exit_code: int | None = None
    redacted_stderr: str = ""


class AntigravityHandshakeRequest(BaseModel):
    """Validated input request for a single non-interactive Antigravity handshake."""

    prompt: str = Field(min_length=1, max_length=16000)
    workspace_path: str = Field(min_length=1)
    timeout_seconds: int = Field(default=60, ge=5, le=600)
    sandbox: bool = False
    model: str | None = None

    @field_validator("prompt")
    @classmethod
    def _prompt_must_not_be_blank(cls, value: str) -> str:
        """Ensure the prompt contains non-whitespace content."""
        if not value.strip():
            raise ValueError("Prompt must not be empty or whitespace-only.")
        return value


class AntigravityHandshakeResult(BaseModel):
    """Normalized result of a single Antigravity CLI handshake invocation."""

    status: Literal["SUCCEEDED", "FAILED", "TIMED_OUT", "BLOCKED"]
    executable_path: str | None = None
    version: str | None = None
    command_summary: str
    started_at: datetime
    finished_at: datetime
    duration_ms: int = Field(ge=0)
    exit_code: int | None = None
    response_text: str | None = None
    provider_status: str | None = None
    conversation_id: str | None = None
    model_id: str | None = None
    num_turns: int | None = None
    telemetry: AntigravityTelemetry
    error: AntigravityError | None = None
    denied_actions: list[dict[str, Any]] = Field(default_factory=list)
    redacted_stdout: str = ""
    redacted_stderr: str = ""


class AntigravityToolExecutionRequest(BaseModel):
    """Validated input request for a sandboxed Antigravity tool execution proof."""

    prompt: str = Field(min_length=1, max_length=16000)
    workspace_path: str = Field(min_length=1)
    expected_artifact_relpath: str = Field(min_length=1)
    source_files_to_protect: list[str] = Field(default_factory=list)
    expected_content: str | None = None
    required_substrings: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=90, ge=5, le=600)
    require_sandbox: bool = True
    execution_mode: Literal["accept-edits", "plan"] = "accept-edits"
    model: str | None = None

    @field_validator("prompt")
    @classmethod
    def _prompt_not_blank(cls, value: str) -> str:
        """Ensure the prompt contains non-whitespace content."""
        if not value.strip():
            raise ValueError("Prompt must not be empty or whitespace-only.")
        return value


class ArtifactVerificationResult(BaseModel):
    """Independent filesystem and SHA-256 verification outcome for a tool artifact."""

    artifact_relpath: str
    artifact_abs_path: str | None = None
    cli_success: bool
    artifact_exists: bool
    within_workspace: bool
    content_verified: bool
    hash_verified: bool
    source_integrity_verified: bool
    business_verified: bool
    file_size_bytes: int | None = None
    expected_sha256: str | None = None
    actual_sha256: str | None = None
    failure_reasons: list[str] = Field(default_factory=list)


class AntigravityToolExecutionResult(BaseModel):
    """Combined CLI execution outcome and independent artifact verification evidence."""

    status: Literal["VERIFIED_PASS", "VERIFIED_FAIL", "BLOCKED", "TIMED_OUT", "FAILED"]
    workspace_fixture_id: str
    cli_result: AntigravityHandshakeResult
    verification: ArtifactVerificationResult
    tool_events_note: str = "NOT OBSERVABLE FROM CURRENT CLI OUTPUT"
    error: AntigravityError | None = None


class AntigravityStreamRequest(BaseModel):
    """Validated input request for a streaming Antigravity CLI invocation."""

    prompt: str = Field(min_length=1, max_length=16000)
    workspace_path: str = Field(min_length=1)
    timeout_seconds: int = Field(default=90, ge=5, le=600)
    require_sandbox: bool = True
    execution_mode: Literal["accept-edits", "plan"] = "accept-edits"
    model: str | None = None
    conversation_id: str | None = None
    denied_read_write_paths: list[str] = Field(default_factory=list)
    deny_shell_commands: bool = False
    max_payload_chars: int = Field(default=512, ge=64, le=4096)
    max_line_bytes: int = Field(default=262144, ge=1024, le=4194304)

    @field_validator("prompt")
    @classmethod
    def _prompt_not_blank(cls, value: str) -> str:
        """Ensure the prompt contains non-whitespace content."""
        if not value.strip():
            raise ValueError("Prompt must not be empty or whitespace-only.")
        return value

    @field_validator("conversation_id")
    @classmethod
    def _conversation_id_not_blank(cls, value: str | None) -> str | None:
        """Reject whitespace-only conversation IDs when explicitly supplied."""
        if value is not None and not value.strip():
            raise ValueError("conversation_id must not be blank when provided.")
        return value.strip() if value is not None else None


class AntigravityStreamEvent(BaseModel):
    """Normalized execution event emitted by either the CLI stream or the adapter."""

    event_id: str = Field(min_length=1)
    sequence_number: int = Field(ge=1)
    timestamp: datetime
    event_type: Literal[
        "process_started",
        "process_exited",
        "process_timed_out",
        "stream_parse_failed",
        "stream_interrupted",
        "execution_started",
        "step_update",
        "text_delta",
        "tool_call_started",
        "tool_call_completed",
        "tool_call_failed",
        "execution_completed",
        "provider_error",
        "unknown_provider_event",
        "execution_cancel_requested",
        "process_cancelled",
        "event_buffer_overflow",
    ]
    source: Literal["provider_cli", "adapter"]
    provider_event_type: str | None = None
    conversation_id: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    status: Literal["STARTED", "IN_PROGRESS", "COMPLETED", "FAILED", "UNKNOWN"]
    redacted_payload: dict[str, Any] = Field(default_factory=dict)
    telemetry: AntigravityTelemetry | None = None
    raw_event_reference: str | None = None


class AntigravityStreamResult(BaseModel):
    """Normalized final outcome and ordered event trace of a streaming CLI run."""

    status: Literal["SUCCEEDED", "FAILED", "TIMED_OUT", "BLOCKED", "CANCELLED"]
    execution_id: str | None = None
    executable_path: str | None = None
    version: str | None = None
    command_summary: str
    started_at: datetime
    first_event_at: datetime | None = None
    finished_at: datetime
    duration_ms: int = Field(ge=0)
    exit_code: int | None = None
    pid: int | None = None
    provider_status: str | None = None
    conversation_id: str | None = None
    model_id: str | None = None
    num_turns: int | None = None
    response_text: str | None = None
    telemetry: AntigravityTelemetry
    events: list[AntigravityStreamEvent] = Field(default_factory=list)
    events_dropped_count: int = Field(default=0, ge=0)
    observed_tool_names: list[str] = Field(default_factory=list)
    completion_event_received: bool = False
    parse_error_count: int = Field(default=0, ge=0)
    process_tree_terminated: bool = False
    termination_mechanism: Literal[
        "none",
        "win32_job_object",
        "terminate_only",
        "kill_fallback",
    ] = "none"
    denied_actions: list[dict[str, Any]] = Field(default_factory=list)
    error: AntigravityError | None = None
    redacted_stderr: str = ""


class AntigravitySessionConfig(BaseModel):
    """Configuration binding a logical AI Village session to an isolated workspace."""

    session_id: str = Field(min_length=3, max_length=64)
    workspace_path: str = Field(min_length=1)
    allowed_root_path: str = Field(min_length=1)
    allow_temp_workspace: bool = False
    deny_shell_commands: bool = True
    require_sandbox: bool = True
    execution_mode: Literal["accept-edits", "plan"] = "accept-edits"
    timeout_seconds: int = Field(default=90, ge=5, le=600)
    model: str | None = None
    max_payload_chars: int = Field(default=512, ge=64, le=4096)
    max_line_bytes: int = Field(default=262144, ge=1024, le=4194304)

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, value: str) -> str:
        """Validate logical session ID format and block secret-like or traversal values."""
        if not _SESSION_ID_RE.fullmatch(value) or ".." in value:
            raise ValueError(
                "session_id must match ^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$ without '..'."
            )
        if redact_secrets(value) != value:
            raise ValueError("session_id must not resemble an API key or secret.")
        return value

    @field_validator("workspace_path", "allowed_root_path")
    @classmethod
    def _path_not_blank(cls, value: str) -> str:
        """Reject whitespace-only workspace or root path strings."""
        if not value.strip():
            raise ValueError("Path must not be empty or whitespace-only.")
        return value


class SessionExecutionContext(BaseModel):
    """Runtime session context separating AI Village identity from provider conversation IDs."""

    session_id: str
    workspace_id: str
    canonical_workspace_path: str
    allowed_root_path: str
    provider_conversation_id: str | None = None
    conversation_binding_source: Literal["none", "provider_cli"] = "none"
    created_at: datetime
    turn_count: int = Field(default=0, ge=0)
    closed: bool = False


class AntigravitySessionEvent(BaseModel):
    """Streaming event wrapped with explicit AI Village adapter session attribution."""

    session_id: str
    session_id_source: Literal["adapter_session_context"] = "adapter_session_context"
    execution_id: str | None = None
    provider_conversation_id: str | None = None
    turn_index: int = Field(ge=1)
    event: AntigravityStreamEvent


class AntigravitySessionTurnResult(BaseModel):
    """Outcome of one isolated session turn pairing stream evidence with session metadata."""

    session_id: str
    execution_id: str | None = None
    turn_index: int = Field(ge=1)
    provider_conversation_id: str | None = None
    resumed_conversation: bool = False
    enforced_deny_rules_count: int = Field(default=0, ge=0)
    enforced_settings_applied: bool = False
    stream_result: AntigravityStreamResult
    session_events: list[AntigravitySessionEvent] = Field(default_factory=list)
    isolation_error: AntigravityError | None = None


class AntigravityExecutionStatus(BaseModel):
    """Live snapshot of an owned CLI execution lifecycle without fake progress percentages."""

    execution_id: str
    session_id: str | None = None
    state: ProcessLifecycleState
    is_terminal: bool
    process_running: bool
    active_tool_name: str | None = None
    pid: int | None = None
    conversation_id: str | None = None
    command_summary: str
    created_at: datetime
    started_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    finished_at: datetime | None = None
    elapsed_ms: int = Field(default=0, ge=0)
    exit_code: int | None = None
    events_received: int = Field(default=0, ge=0)
    events_dropped: int = Field(default=0, ge=0)
    termination_race_note: str | None = None


class AntigravityCancelOutcome(BaseModel):
    """Structured outcome returned by an idempotent cancellation request."""

    execution_id: str
    accepted: bool
    already_terminal: bool
    idempotent_repeat: bool
    state_at_request: ProcessLifecycleState
    final_state: ProcessLifecycleState | None = None
    cancel_requested_at: datetime
    message: str
