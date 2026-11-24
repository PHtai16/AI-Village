"""Strict schemas and fail-closed JSON parsers for the P03.01 Master -> Worker spike."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from engines.antigravity.diagnostics import redact_secrets
from engines.contract import EngineEvent, EngineResult
from engines.verification import EngineArtifactVerificationResult

_FENCE_RE = re.compile(r"^```(?:json|JSON)?\s*\r?\n(?P<body>.*)\r?\n```\s*$", re.DOTALL)
_AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_DRIVE_OR_URL_RE = re.compile(r"^[A-Za-z]:")


def parse_strict_json_object(raw_text: str | None) -> dict[str, Any]:
    """Parse strict JSON object output from an LLM response without prose recovery.

    A single clean fenced ``json`` block without surrounding text is unwrapped;
    surrounding commentary, broken syntax, or non-object JSON values fail closed.

    Args:
        raw_text: Raw model response text returned by an engine adapter.

    Returns:
        Parsed top-level dictionary ready for Pydantic schema validation.

    Raises:
        ValueError: If text is missing, wrapped with prose, invalid JSON, or not an object.
    """
    if raw_text is None or not raw_text.strip():
        raise ValueError("Model response text is empty; expected a strict JSON object.")

    candidate = raw_text.strip()
    fence_match = _FENCE_RE.fullmatch(candidate)
    if fence_match is not None:
        candidate = fence_match.group("body").strip()

    if not candidate.startswith("{") or not candidate.endswith("}"):
        raise ValueError("Model response must be a single JSON object without prose.")

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON object syntax: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError(f"Expected top-level JSON object, got {type(parsed).__name__}.")
    return parsed


def _validate_clean_relpath(raw_path: str, field_name: str) -> str:
    """Validate that a planned path string is a clean non-escaping relative path."""
    cleaned = raw_path.strip()
    if not cleaned:
        raise ValueError(f"{field_name} entries must not be blank.")
    if "*" in cleaned or "?" in cleaned:
        raise ValueError(f"{field_name} must declare explicit paths, not wildcards: {raw_path!r}")
    if cleaned.startswith(("/", "\\")) or _DRIVE_OR_URL_RE.match(cleaned):
        raise ValueError(f"{field_name} must be workspace-relative, got {raw_path!r}")
    normalized = cleaned.replace("\\", "/")
    posix = PurePosixPath(normalized)
    if posix.is_absolute() or ".." in posix.parts or any(part == "." for part in posix.parts):
        raise ValueError(f"{field_name} contains disallowed traversal segments: {raw_path!r}")
    return Path(*posix.parts).as_posix()


class OrchestrationError(BaseModel):
    """Normalized, secret-scrubbed failure record for a Master -> Worker spike stage."""

    model_config = ConfigDict(extra="forbid")

    error_code: Literal[
        "INVALID_AGENT_SPEC",
        "INVALID_WORKSPACE_ISOLATION",
        "MASTER_PLAN_EXECUTION_FAILED",
        "MASTER_PLAN_PARSE_FAILED",
        "POLICY_VIOLATION",
        "WORKER_EXECUTION_FAILED",
        "SESSION_ISOLATION_VIOLATION",
        "QA_VERIFICATION_FAILED",
        "MASTER_SYNTHESIS_EXECUTION_FAILED",
        "MASTER_SYNTHESIS_PARSE_FAILED",
        "SYNTHESIS_TRUTH_MISMATCH",
    ]
    stage: Literal[
        "preflight",
        "master_plan",
        "handoff_policy",
        "worker_execute",
        "qa_verify",
        "master_synthesize",
    ]
    message: str = Field(min_length=1, max_length=2000)
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("message")
    @classmethod
    def _scrub_message(cls, value: str) -> str:
        """Redact potential credentials from orchestration error messages."""
        return redact_secrets(value)


class SpikeAgentSpec(BaseModel):
    """Explicit role and boundary contract for a participant in the P03.01 spike."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=3, max_length=100)
    department: Literal["orchestration", "it"]
    system_role: Literal["orchestrator", "worker"]
    execution_mode: Literal["plan", "accept-edits"]
    allow_workspace_write: bool
    allow_shell: bool = False
    allowed_handoff_targets: list[str] = Field(default_factory=list)

    @field_validator("agent_id")
    @classmethod
    def _validate_agent_id(cls, value: str) -> str:
        """Ensure identifiers follow ``<domain>.<name>`` conventions."""
        cleaned = value.strip()
        if not _AGENT_ID_RE.fullmatch(cleaned):
            raise ValueError(f"Invalid agent_id format: {value!r}")
        return cleaned

    @model_validator(mode="after")
    def _validate_role_boundaries(self) -> SpikeAgentSpec:
        """Enforce P03.01 invariants between Master orchestrator and Worker roles."""
        if self.system_role == "orchestrator":
            if self.department != "orchestration":
                raise ValueError("Orchestrator agent must belong to department 'orchestration'.")
            if self.allow_workspace_write:
                raise ValueError("Master orchestrator must not hold workspace write permission.")
            if self.execution_mode != "plan":
                raise ValueError("Master orchestrator must run in 'plan' execution mode.")
        if self.system_role == "worker" and self.allowed_handoff_targets:
            raise ValueError("Spike workers must not define downstream handoff targets.")
        return self


def build_default_spike_roster() -> dict[str, SpikeAgentSpec]:
    """Build the default two-agent roster for the P03.01 integration spike.

    Returns:
        Dictionary mapping ``core.master`` and ``it.artifact_worker`` to their specs.
    """
    return {
        "core.master": SpikeAgentSpec(
            agent_id="core.master",
            department="orchestration",
            system_role="orchestrator",
            execution_mode="plan",
            allow_workspace_write=False,
            allow_shell=False,
            allowed_handoff_targets=["it.artifact_worker"],
        ),
        "it.artifact_worker": SpikeAgentSpec(
            agent_id="it.artifact_worker",
            department="it",
            system_role="worker",
            execution_mode="accept-edits",
            allow_workspace_write=True,
            allow_shell=False,
            allowed_handoff_targets=[],
        ),
    }


class ArtifactExpectation(BaseModel):
    """Advisory verification expectations planned by Master prior to handoff."""

    model_config = ConfigDict(extra="forbid")

    required_substrings: list[str] = Field(default_factory=list, max_length=20)
    expected_line_count: int | None = Field(default=None, ge=1, le=200)
    must_preserve_inputs: bool = True

    @field_validator("required_substrings")
    @classmethod
    def _validate_substrings(cls, values: list[str]) -> list[str]:
        """Reject empty substring entries."""
        cleaned = [item for item in values if item]
        if len(cleaned) != len(values):
            raise ValueError("required_substrings must not contain empty strings.")
        return cleaned


class MasterPlan(BaseModel):
    """Structured task decomposition and permission scope generated by Master AI."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    task_summary: str = Field(min_length=1, max_length=500)
    target_worker_id: str = Field(min_length=3, max_length=100)
    worker_objective: str = Field(min_length=1, max_length=2000)
    allowed_read_paths: list[str] = Field(min_length=1, max_length=10)
    allowed_write_paths: list[str] = Field(min_length=1, max_length=5)
    forbidden_actions: list[str] = Field(default_factory=list, max_length=15)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=10)
    artifact_expectation: ArtifactExpectation = Field(default_factory=ArtifactExpectation)

    @field_validator("task_summary", "target_worker_id", "worker_objective")
    @classmethod
    def _require_non_blank_text(cls, value: str) -> str:
        """Reject whitespace-only text fields."""
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Text field must not be blank.")
        return cleaned

    @field_validator("allowed_read_paths")
    @classmethod
    def _validate_read_paths(cls, values: list[str]) -> list[str]:
        """Validate every planned read path stays strictly workspace-relative."""
        return [_validate_clean_relpath(item, "allowed_read_paths") for item in values]

    @field_validator("allowed_write_paths")
    @classmethod
    def _validate_write_paths(cls, values: list[str]) -> list[str]:
        """Validate every planned write path stays strictly workspace-relative."""
        return [_validate_clean_relpath(item, "allowed_write_paths") for item in values]

    @field_validator("forbidden_actions", "acceptance_criteria")
    @classmethod
    def _validate_non_blank_list_items(cls, values: list[str]) -> list[str]:
        """Reject blank entries inside policy or acceptance lists."""
        cleaned = [item.strip() for item in values]
        if any(not item for item in cleaned):
            raise ValueError("List entries must not be empty or whitespace-only.")
        return cleaned


class WorkerHandoff(BaseModel):
    """Policy-verified delegation envelope passed from Master to Worker."""

    model_config = ConfigDict(extra="forbid")

    handoff_id: str = Field(default_factory=lambda: f"hoff-{uuid.uuid4().hex[:12]}")
    correlation_id: str = Field(min_length=1)
    from_agent_id: str = Field(min_length=3)
    to_agent_id: str = Field(min_length=3)
    worker_session_id: str = Field(min_length=1)
    worker_workspace_path: str = Field(min_length=1)
    denied_paths: list[str] = Field(default_factory=list)
    deny_shell_commands: Literal[True] = True
    plan: MasterPlan
    worker_instructions: str = Field(min_length=1, max_length=8000)


class WorkerExecutionRecord(BaseModel):
    """Recorded output and context of the delegated Worker engine invocation."""

    model_config = ConfigDict(extra="forbid")

    worker_agent_id: str = Field(min_length=3)
    worker_session_id: str = Field(min_length=1)
    handoff_id: str = Field(min_length=1)
    engine_result: EngineResult
    artifact_relpath: str = Field(min_length=1)
    protected_source_hashes: dict[str, str] = Field(default_factory=dict)


class MasterSynthesis(BaseModel):
    """Structured delivery decision emitted by Master AI after independent QA."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    delivery_status: Literal["DELIVERED", "REJECTED", "FAILED"]
    qa_status_observed: Literal["VERIFIED_PASS", "VERIFIED_FAIL", "NOT_RUN"]
    worker_status_observed: str = Field(min_length=1, max_length=64)
    artifact_relpath: str = Field(min_length=1, max_length=260)
    artifact_sha256: str | None = None
    final_summary: str = Field(min_length=1, max_length=2000)
    verified_facts: list[str] = Field(default_factory=list, max_length=20)
    unverified_or_failed_reasons: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("final_summary", "worker_status_observed", "artifact_relpath")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        """Ensure required synthesis strings are non-blank."""
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Synthesis text field must not be blank.")
        return cleaned

    @field_validator("artifact_sha256")
    @classmethod
    def _normalize_optional_sha256(cls, value: str | None) -> str | None:
        """Normalize non-None SHA-256 digests to stripped lowercase hex or None."""
        if value is None:
            return None
        cleaned = value.strip().lower()
        return cleaned or None


class AttributedSpikeEvent(BaseModel):
    """Normalized ``EngineEvent`` attributed to its orchestrator or worker context."""

    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    stage: Literal["master_plan", "worker_execute", "master_synthesize"]
    agent_id: str = Field(min_length=3)
    role: Literal["orchestrator", "worker"]
    logical_session_id: str = Field(min_length=1)
    provider_conversation_id: str | None = None
    engine_event: EngineEvent


class MasterWorkerSpikeResult(BaseModel):
    """Full auditable record of one Master -> Worker -> QA -> Master execution."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["DELIVERED", "FAILED", "BLOCKED", "REJECTED_BY_QA"]
    correlation_id: str = Field(min_length=1)
    master_agent_id: str = Field(min_length=3)
    worker_agent_id: str | None = None
    master_session_id: str = Field(min_length=1)
    worker_session_id: str | None = None
    master_plan_conversation_id: str | None = None
    worker_conversation_id: str | None = None
    master_synthesis_conversation_id: str | None = None
    master_workspace_path: str = Field(min_length=1)
    worker_workspace_path: str = Field(min_length=1)
    user_request: str = Field(min_length=1)
    master_plan_result: EngineResult | None = None
    master_plan: MasterPlan | None = None
    worker_handoff: WorkerHandoff | None = None
    worker_execution: WorkerExecutionRecord | None = None
    qa_verification: EngineArtifactVerificationResult | None = None
    master_synthesis_result: EngineResult | None = None
    master_synthesis: MasterSynthesis | None = None
    attributed_events: list[AttributedSpikeEvent] = Field(default_factory=list)
    error: OrchestrationError | None = None
    started_at: datetime
    finished_at: datetime
    duration_ms: int = Field(ge=0)
