"""Verifiable Artifact metadata contract separating existence claims from QA verification."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.contracts.identifiers import (
    AgentId,
    ArtifactId,
    RunId,
    SemVer,
    Sha256Hex,
    TaskId,
    ensure_not_secret,
    new_artifact_id,
    validate_aware_datetime,
    validate_safe_relative_path,
)

ArtifactVerificationStatus = Literal["UNVERIFIED", "VERIFIED_PASS", "VERIFIED_FAIL"]


class Artifact(BaseModel):
    """Artifact contract storing path/hash references without touching disk or forging QA PASS."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: ArtifactId = Field(default_factory=new_artifact_id)
    task_id: TaskId
    run_id: RunId
    owner_agent_id: AgentId
    relative_path: str = Field(min_length=1, max_length=260)
    storage_ref: str | None = Field(default=None, min_length=1, max_length=260)
    content_type: str | None = Field(default=None, min_length=3, max_length=120)
    size_bytes: int | None = Field(default=None, ge=0)
    sha256: Sha256Hex | None = None
    version: SemVer | None = None
    verification_status: ArtifactVerificationStatus = "UNVERIFIED"
    evidence_reference: str | None = Field(default=None, min_length=1, max_length=256)
    created_at: datetime | None = None

    @field_validator("relative_path")
    @classmethod
    def _validate_relative_path(cls, value: str) -> str:
        """Ensure artifact relative path stays workspace-relative syntactically without disk I/O."""
        return validate_safe_relative_path(value, "relative_path")

    @field_validator("storage_ref", "content_type")
    @classmethod
    def _validate_optional_strings(cls, value: str | None, info: object) -> str | None:
        """Ensure optional metadata fields are non-blank and free of secrets."""
        if value is None:
            return None
        field_name = getattr(info, "field_name", "artifact_field")
        return ensure_not_secret(value, field_name)

    @field_validator("evidence_reference")
    @classmethod
    def _validate_evidence_reference(cls, value: str | None) -> str | None:
        """Ensure QA evidence references stay non-secret and distinct from domain Task/Run IDs."""
        if value is None:
            return None
        cleaned = ensure_not_secret(value, "evidence_reference")
        if cleaned.startswith(("task_", "run_", "sess-")):
            raise ValueError(
                "evidence_reference must point to QA verification evidence, not Task/Run/Session IDs."
            )
        return cleaned

    @field_validator("created_at")
    @classmethod
    def _validate_created_at(cls, value: datetime | None) -> datetime | None:
        """Require timezone-aware timestamp when supplied."""
        if value is None:
            return None
        return validate_aware_datetime(value, "created_at")

    @model_validator(mode="after")
    def _enforce_qa_evidence_before_pass(self) -> Artifact:
        """Never allow VERIFIED_PASS without independent QA evidence_reference and SHA-256."""
        if self.verification_status == "VERIFIED_PASS":
            if self.evidence_reference is None:
                raise ValueError(
                    "Artifact cannot carry verification_status='VERIFIED_PASS' without evidence_reference."
                )
            if self.sha256 is None:
                raise ValueError(
                    "Artifact cannot carry verification_status='VERIFIED_PASS' without a recorded sha256."
                )
        return self
