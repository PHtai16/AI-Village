"""Persistent repository interfaces, SQLite implementations, transactional workflow service, and restart recovery."""

from storage.repositories.agent_registry_repo import SQLiteAgentRegistryRepository
from storage.repositories.approval_repo import SQLiteApprovalRepository
from storage.repositories.errors import (
    DuplicateEntityError,
    EntityNotFoundError,
    OwnershipMismatchError,
    RepositoryError,
    RepositoryErrorCode,
    SchemaCompatibilityError,
    SecretViolationError,
    StaleStateError,
    StoredRun,
    StoredTask,
)
from storage.repositories.event_journal import SQLiteEventJournal
from storage.repositories.interfaces import (
    MetadataRepository,
    RunRepository,
    SessionRepository,
    TaskRepository,
)
from storage.repositories.lifecycle_repo import (
    ExecutionRecord,
    IdempotencyConflictError,
    IdempotencyRecord,
    SQLiteLifecyclePersistenceAdapter,
    SQLiteLifecycleRepository,
)
from storage.repositories.planning_repo import SQLitePlanRepository
from storage.repositories.recovery import (
    InterruptedWorkAssessment,
    RecoveryStatus,
    RunRecoveryReport,
    TaskRecoveryReport,
    recover_run,
    recover_task,
)
from storage.repositories.service import (
    PairedOperation,
    PersistentTransitionOutcome,
    PersistentWorkflowService,
)
from storage.repositories.sqlite_store import (
    SQLiteMetadataRepository,
    SQLiteRepositoryStore,
    SQLiteRunRepository,
    SQLiteSessionRepository,
    SQLiteTaskRepository,
)

__all__ = [
    "DuplicateEntityError",
    "EntityNotFoundError",
    "ExecutionRecord",
    "IdempotencyConflictError",
    "IdempotencyRecord",
    "InterruptedWorkAssessment",
    "MetadataRepository",
    "OwnershipMismatchError",
    "PairedOperation",
    "PersistentTransitionOutcome",
    "PersistentWorkflowService",
    "RecoveryStatus",
    "RepositoryError",
    "RepositoryErrorCode",
    "RunRecoveryReport",
    "RunRepository",
    "SQLiteAgentRegistryRepository",
    "SQLiteApprovalRepository",
    "SQLiteEventJournal",
    "SQLiteLifecyclePersistenceAdapter",
    "SQLiteLifecycleRepository",
    "SQLiteMetadataRepository",
    "SQLitePlanRepository",
    "SQLiteRepositoryStore",
    "SQLiteRunRepository",
    "SQLiteSessionRepository",
    "SQLiteTaskRepository",
    "SchemaCompatibilityError",
    "SecretViolationError",
    "SessionRepository",
    "StaleStateError",
    "StoredRun",
    "StoredTask",
    "TaskRecoveryReport",
    "TaskRepository",
    "recover_run",
    "recover_task",
]
