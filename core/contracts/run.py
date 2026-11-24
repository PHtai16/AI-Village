"""Execution Run, Telemetry reference, and Error reference contracts separate from Task."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.contracts.identifiers import (
    ArtifactId,
    RunId,
    SessionId,
    SnakeId,
    TaskId,
    ensure_not_secret,
    new_run_id,
    reject_secret_text,
    validate_artifact_id,
    validate_aware_datetime,
    validate_chronological,
    validate_session_id,
    validate_unique_id_list,
)

RunStatus = Literal[
    "PENDING",
    "RUNNING",
    "WAITING_INPUT",
    "WAITING_APPROVAL",
    "BLOCKED",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "TIMED_OUT",
]
RunTelemetryStatus = Literal[
    "VERIFIED_MEASURED",
    "UNAVAILABLE_FROM_PROVIDER",
    "ESTIMATED_PENDING_AUDIT",
]
RunTelemetrySource = Literal["provider_cli", "provider_api", "adapter_measured", "unavailable"]


class RunErrorRef(BaseModel):
    """Normalized, secret-scrubbed failure reference attached to a failed or blocked Run."""

    model_config = ConfigDict(extra="forbid")

    error_code: str = Field(min_length=1, max_length=64)
    stage: str | None = Field(default=None, min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=2000)
    retryable: bool = False

    @field_validator("error_code", "stage")
    @classmethod
    def _validate_labels(cls, value: str | None, info: object) -> str | None:
        """Ensure error code and optional stage labels are non-blank and secret-free."""
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "error_field"))

    @field_validator("message")
    @classmethod
    def _validate_message(cls, value: str) -> str:
        """Reject secret-bearing strings inside RunErrorRef messages."""
        return ensure_not_secret(value, "message")


class RunTelemetryRef(BaseModel):
    """Measured or explicitly unavailable telemetry snapshot attached to a Run."""

    model_config = ConfigDict(extra="forbid")

    total_duration_ms: int | None = Field(default=None, ge=0)
    provider_duration_ms: int | None = Field(default=None, ge=0)
    engine_call_count: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    thinking_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    actual_cost_usd: float | None = Field(default=None, ge=0)
    telemetry_status: RunTelemetryStatus = "UNAVAILABLE_FROM_PROVIDER"
    telemetry_source: RunTelemetrySource = "unavailable"

    @model_validator(mode="after")
    def _validate_source_truthfulness(self) -> RunTelemetryRef:
        """Forbid claiming VERIFIED_MEASURED when telemetry_source is 'unavailable'."""
        if self.telemetry_status == "VERIFIED_MEASURED" and self.telemetry_source == "unavailable":
            raise ValueError(
                "telemetry_source cannot be 'unavailable' when telemetry_status is 'VERIFIED_MEASURED'."
            )
        return self


class Run(BaseModel):
    """Single execution attempt belonging to one Task without owning Task lifecycle state."""

    model_config = ConfigDict(extra="forbid")

    run_id: RunId = Field(default_factory=new_run_id)
    task_id: TaskId
    runtime_id: str = Field(min_length=1, max_length=100)
    invocation_id: str | None = Field(default=None, min_length=1, max_length=80)
    correlation_id: str | None = Field(default=None, min_length=1, max_length=80)
    status: RunStatus = "PENDING"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    session_ids: list[SessionId] = Field(default_factory=list, max_length=50)
    provider_id: SnakeId | None = None
    connection_id: SnakeId | None = None
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    artifact_ids: list[ArtifactId] = Field(default_factory=list, max_length=200)
    error_reference: RunErrorRef | None = None
    telemetry: RunTelemetryRef | None = None

    @field_validator("runtime_id", "correlation_id", "model_id")
    @classmethod
    def _validate_non_secret_labels(cls, value: str | None, info: object) -> str | None:
        """Ensure runtime, correlation, and model identifiers are non-blank and safe."""
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "run_field"))

    @field_validator("invocation_id")
    @classmethod
    def _validate_invocation_id(cls, value: str | None) -> str | None:
        """Keep AgentRuntime invocation_id distinct from domain Task/Run/Artifact/Event IDs."""
        if value is None:
            return None
        cleaned = ensure_not_secret(value, "invocation_id")
        if cleaned.startswith(("task_", "run_", "art_", "evt_")):
            raise ValueError(
                "invocation_id must not masquerade as a domain Task, Run, Artifact, or Event ID."
            )
        return cleaned

    @field_validator("session_ids")
    @classmethod
    def _validate_session_ids(cls, values: list[str]) -> list[str]:
        """Validate canonical UUID v4 sessions without duplicates."""
        return validate_unique_id_list(values, validate_session_id, "session_ids")

    @field_validator("artifact_ids")
    @classmethod
    def _validate_artifact_ids(cls, values: list[str]) -> list[str]:
        """Validate canonical `art_*` artifact references without duplicates."""
        return validate_unique_id_list(values, validate_artifact_id, "artifact_ids")

    @field_validator("started_at", "finished_at")
    @classmethod
    def _validate_timestamps(cls, value: datetime | None, info: object) -> datetime | None:
        """Enforce timezone-aware timestamps when supplied."""
        if value is None:
            return None
        return validate_aware_datetime(value, getattr(info, "field_name", "timestamp"))

    @model_validator(mode="after")
    def _validate_run_timing_and_error_consistency(self) -> Run:
        """Prevent fabricated timing on PENDING runs or errors attached to SUCCEEDED runs."""
        if self.status == "PENDING" and (self.started_at is not None or self.finished_at is not None):
            raise ValueError("PENDING Run must not pre-populate started_at or finished_at.")
        validate_chronological(self.started_at, self.finished_at, "started_at", "finished_at")
        if self.status == "SUCCEEDED" and self.error_reference is not None:
            raise ValueError("SUCCEEDED Run must not carry an error_reference.")
        _ = reject_secret_text
        return self
