"""Protocol interfaces isolating Core/Application callers from concrete SQLite internals."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.contracts.artifact import Artifact
from core.contracts.references import ChecklistItem, ConnectionRef, NotificationRef, ProjectRef
from core.contracts.run import Run
from core.contracts.session import Session
from core.contracts.task import Task, TaskStatus
from storage.repositories.errors import StoredRun, StoredTask


@runtime_checkable
class TaskRepository(Protocol):
    """Controlled persistence contract for Task aggregates without raw history bypass."""

    def create_task(self, task: Task) -> StoredTask:
        """Persist a new Task along with its immutable initial replay baseline."""

    def get_task(self, task_id: str) -> Task | None:
        """Fetch the latest Task snapshot, or None if not found."""

    def get_stored_task(self, task_id: str) -> StoredTask | None:
        """Fetch the latest Task snapshot, optimistic version, and initial baseline."""

    def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        project_id: str | None = None,
    ) -> list[Task]:
        """List persisted Task snapshots filtered by optional status or project grouping."""

    def save_task_state(self, task: Task, *, expected_version: int) -> StoredTask:
        """Persist a Task state change using optimistic version locking."""


@runtime_checkable
class RunRepository(Protocol):
    """Controlled persistence contract for Run execution records."""

    def create_run(self, run: Run) -> StoredRun:
        """Persist a new Run bound to an existing Task with its initial baseline."""

    def get_run(self, run_id: str) -> Run | None:
        """Fetch the latest Run snapshot, or None if not found."""

    def get_stored_run(self, run_id: str) -> StoredRun | None:
        """Fetch the latest Run snapshot, optimistic version, and initial baseline."""

    def list_runs_for_task(self, task_id: str) -> list[Run]:
        """List all Runs belonging to a parent Task."""

    def save_run_state(self, run: Run, *, expected_version: int) -> StoredRun:
        """Persist a Run state change using optimistic version locking."""


@runtime_checkable
class SessionRepository(Protocol):
    """Persistence contract for logical AI Village Sessions distinct from provider threads."""

    def save_session(self, session: Session) -> Session:
        """Create or update a logical Session record without altering its UUID v4 identity."""

    def get_session(self, session_id: str) -> Session | None:
        """Fetch a logical Session by UUID v4 identifier."""

    def list_sessions(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> list[Session]:
        """List Sessions optionally filtered by parent Task or Run."""


@runtime_checkable
class MetadataRepository(Protocol):
    """Persistence contract for Project, Checklist, Artifact, Notification, and Connection metadata."""

    def save_project(self, project: ProjectRef) -> ProjectRef:
        """Persist an organizational ProjectRef."""

    def get_project(self, project_id: str) -> ProjectRef | None:
        """Fetch an organizational ProjectRef by identifier."""

    def save_checklist_item(self, item: ChecklistItem) -> ChecklistItem:
        """Persist a ChecklistItem tied to an existing Task."""

    def list_checklist_items(self, task_id: str) -> list[ChecklistItem]:
        """List ordered ChecklistItems belonging to a Task."""

    def save_artifact(self, artifact: Artifact) -> Artifact:
        """Persist Artifact metadata without modifying verification truth."""

    def get_artifact(self, artifact_id: str) -> Artifact | None:
        """Fetch Artifact metadata by identifier."""

    def list_artifacts(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> list[Artifact]:
        """List Artifact metadata filtered by Task or Run."""

    def save_notification(self, notification: NotificationRef) -> NotificationRef:
        """Persist a NotificationRef anchored to an existing Event."""

    def list_notifications(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> list[NotificationRef]:
        """List NotificationRef records filtered by Task or Run."""

    def save_connection(self, connection: ConnectionRef) -> ConnectionRef:
        """Persist non-secret ConnectionRef metadata."""

    def get_connection(self, provider_id: str, connection_id: str) -> ConnectionRef | None:
        """Fetch ConnectionRef metadata by provider and connection ID."""

    def list_connections(self, *, provider_id: str | None = None) -> list[ConnectionRef]:
        """List stored non-secret ConnectionRef metadata."""
