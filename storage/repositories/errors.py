"""Structured repository exceptions and optimistic-concurrency storage envelopes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from core.contracts.run import Run
from core.contracts.task import Task
from engines.antigravity.diagnostics import redact_secrets

RepositoryErrorCode = Literal[
    "ENTITY_NOT_FOUND",
    "DUPLICATE_ENTITY",
    "STALE_STATE_UPDATE",
    "OWNERSHIP_MISMATCH",
    "SECRET_VIOLATION",
    "SCHEMA_COMPATIBILITY_ERROR",
    "VALIDATION_ERROR",
    "PERSISTENCE_FAILED",
    "STORE_CLOSED",
]


class RepositoryError(RuntimeError):
    """Base structured fail-closed exception raised by the persistence repository layer."""

    def __init__(
        self,
        error_code: RepositoryErrorCode,
        message: str,
        *,
        entity_id: str | None = None,
    ) -> None:
        """Initialize repository exception with redacted audit message."""
        cleaned = redact_secrets(message)
        super().__init__(cleaned)
        self.error_code = error_code
        self.message = cleaned
        self.entity_id = entity_id


class EntityNotFoundError(RepositoryError):
    """Raised when a required Task, Run, Session, or metadata record does not exist."""

    def __init__(self, message: str, *, entity_id: str | None = None) -> None:
        super().__init__("ENTITY_NOT_FOUND", message, entity_id=entity_id)


class DuplicateEntityError(RepositoryError):
    """Raised when inserting a record whose primary identity already exists."""

    def __init__(self, message: str, *, entity_id: str | None = None) -> None:
        super().__init__("DUPLICATE_ENTITY", message, entity_id=entity_id)


class StaleStateError(RepositoryError):
    """Raised when an optimistic-concurrency version check fails to prevent lost updates."""

    def __init__(self, message: str, *, entity_id: str | None = None) -> None:
        super().__init__("STALE_STATE_UPDATE", message, entity_id=entity_id)


class OwnershipMismatchError(RepositoryError):
    """Raised when a Run, Session, or Artifact references an incompatible parent entity."""

    def __init__(self, message: str, *, entity_id: str | None = None) -> None:
        super().__init__("OWNERSHIP_MISMATCH", message, entity_id=entity_id)


class SecretViolationError(RepositoryError):
    """Raised when unredacted credential material is detected at a repository boundary."""

    def __init__(self, message: str, *, entity_id: str | None = None) -> None:
        super().__init__("SECRET_VIOLATION", message, entity_id=entity_id)


class SchemaCompatibilityError(RepositoryError):
    """Raised when database schema version or checksum does not match repository expectations."""

    def __init__(self, message: str, *, entity_id: str | None = None) -> None:
        super().__init__("SCHEMA_COMPATIBILITY_ERROR", message, entity_id=entity_id)


class StoredTask(BaseModel):
    """Persisted Task envelope pairing the current snapshot with its revision and initial baseline."""

    model_config = ConfigDict(extra="forbid")

    task: Task
    version: int = Field(ge=1)
    baseline: Task


class StoredRun(BaseModel):
    """Persisted Run envelope pairing the current snapshot with its revision and initial baseline."""

    model_config = ConfigDict(extra="forbid")

    run: Run
    version: int = Field(ge=1)
    baseline: Run
