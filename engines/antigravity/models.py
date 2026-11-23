"""Pydantic v2 data models for the minimal Antigravity CLI adapter."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


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
    supports_models_subcommand: bool = False
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


class AntigravityError(BaseModel):
    """Normalized, secret-redacted error returned by the Antigravity adapter."""

    error_code: Literal[
        "EXECUTABLE_NOT_FOUND",
        "UNSUPPORTED_ARGUMENT",
        "AUTH_ERROR",
        "QUOTA_EXCEEDED",
        "RATE_LIMITED",
        "TIMED_OUT",
        "PROVIDER_ERROR",
        "INVALID_RESPONSE",
        "PROCESS_LAUNCH_ERROR",
    ]
    message: str
    retryable: bool = False
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
    redacted_stdout: str = ""
    redacted_stderr: str = ""
