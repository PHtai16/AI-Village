"""Restart recovery, snapshot-vs-replay verification, and interrupted-work detection."""

from __future__ import annotations

import json
import sqlite3
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.contracts.artifact import Artifact
from core.contracts.identifiers import RunId, TaskId
from core.contracts.references import ChecklistItem
from core.contracts.run import Run
from core.contracts.session import Session
from core.contracts.task import Task
from core.events.projections import ActivityTimelineProjection, project_activity_timeline
from core.events.replay import (
    RunReplayResult,
    TaskReplayResult,
    replay_run_history,
    replay_task_history,
)
from storage.repositories.sqlite_store import SQLiteRepositoryStore

RecoveryStatus = Literal[
    "VERIFIED_CLEAN",
    "NEEDS_RECONCILIATION",
    "INCOMPLETE_HISTORY",
    "INVALID_HISTORY",
]


class InterruptedWorkAssessment(BaseModel):
    """Explicit post-restart status for work persisted as RUNNING when a process exited."""

    model_config = ConfigDict(extra="forbid")

    entity_type: Literal["task", "run"]
    entity_id: str
    last_persisted_status: str
    observable_process_status: Literal["NOT_APPLICABLE", "UNOBSERVABLE_AFTER_RESTART"]
    reconciliation_required: bool
    reason: str


class TaskRecoveryReport(BaseModel):
    """Structured restart recovery assessment combining persisted state, baseline, replay, and timeline."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    snapshot_task: Task | None = None
    snapshot_version: int | None = Field(default=None, ge=1)
    baseline_task: Task | None = None
    replay_result: TaskReplayResult
    runs: list[Run] = Field(default_factory=list)
    run_replays: dict[str, RunReplayResult] = Field(default_factory=dict)
    sessions: list[Session] = Field(default_factory=list)
    checklist_items: list[ChecklistItem] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    timeline: ActivityTimelineProjection
    snapshot_matches_replay: bool
    inconsistencies: list[str] = Field(default_factory=list)
    interrupted_work: list[InterruptedWorkAssessment] = Field(default_factory=list)
    recovery_status: RecoveryStatus


class RunRecoveryReport(BaseModel):
    """Structured restart recovery report for a single persisted Run."""

    model_config = ConfigDict(extra="forbid")

    run_id: RunId
    snapshot_run: Run | None = None
    snapshot_version: int | None = Field(default=None, ge=1)
    baseline_run: Run | None = None
    replay_result: RunReplayResult
    sessions: list[Session] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    timeline: ActivityTimelineProjection
    snapshot_matches_replay: bool
    inconsistencies: list[str] = Field(default_factory=list)
    interrupted_work: list[InterruptedWorkAssessment] = Field(default_factory=list)
    recovery_status: RecoveryStatus


def _parse_optional_task_baseline(raw_row: dict[str, object] | None, inconsistencies: list[str]) -> Task | None:
    """Safely parse ``baseline_json`` from raw Task row without crashing on missing/broken baseline."""
    if raw_row is None:
        inconsistencies.append("Task snapshot row is missing from SQLite.")
        return None
    raw_baseline = raw_row.get("baseline_json")
    if not isinstance(raw_baseline, str) or not raw_baseline.strip():
        inconsistencies.append("Stored Task baseline_json is empty or missing.")
        return None
    try:
        return Task.model_validate_json(raw_baseline)
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        inconsistencies.append(f"Stored Task baseline_json is invalid: {exc}")
        return None


def _parse_optional_run_baseline(raw_row: dict[str, object] | None, inconsistencies: list[str]) -> Run | None:
    """Safely parse ``baseline_json`` from raw Run row without crashing on missing/broken baseline."""
    if raw_row is None:
        inconsistencies.append("Run snapshot row is missing from SQLite.")
        return None
    raw_baseline = raw_row.get("baseline_json")
    if not isinstance(raw_baseline, str) or not raw_baseline.strip():
        inconsistencies.append("Stored Run baseline_json is empty or missing.")
        return None
    try:
        return Run.model_validate_json(raw_baseline)
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        inconsistencies.append(f"Stored Run baseline_json is invalid: {exc}")
        return None


def _list_cross_task_referenced_artifact_ids(
    store: SQLiteRepositoryStore,
    task_id: str,
) -> set[str]:
    """Return cross-task Vault artifact IDs reused by ``task_id`` while authored by another Task."""
    try:
        with store.open_connection() as conn:
            rows = conn.execute(
                """
                SELECT r.artifact_id
                FROM artifact_task_references r
                JOIN artifact_registry_entries e ON e.artifact_id = r.artifact_id
                WHERE r.referencing_task_id = ? AND e.task_id != ?;
                """,
                (task_id, task_id),
            ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return set()
        raise
    return {str(row["artifact_id"]) for row in rows}


def recover_task(
    store: SQLiteRepositoryStore,
    task_id: str,
    *,
    require_contiguous_sequences: bool = False,
) -> TaskRecoveryReport:
    """Rehydrate a Task after restart, replay its history from stored baseline, and flag interrupted RUNNING work."""
    inconsistencies: list[str] = []
    raw_row = store.tasks.get_raw_task_row(task_id)
    baseline_task = _parse_optional_task_baseline(raw_row, inconsistencies)

    snapshot_task: Task | None = None
    snapshot_version: int | None = None
    if raw_row is not None:
        snapshot_version = int(raw_row["version"])
        try:
            snapshot_task = store.tasks.get_task(task_id)
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            inconsistencies.append(f"Stored Task snapshot failed validation: {exc}")

    entries = store.events.read_entries(task_id=task_id)
    timeline = project_activity_timeline(store.events, task_id=task_id)
    replay_result = replay_task_history(
        entries,
        task_id=task_id,
        baseline_task=baseline_task,
        require_contiguous_sequences=require_contiguous_sequences,
    )

    runs = store.runs.list_runs_for_task(task_id)
    sessions = store.sessions.list_sessions(task_id=task_id)
    checklist_items = store.metadata.list_checklist_items(task_id)
    artifacts = store.metadata.list_artifacts(task_id=task_id)
    cross_task_refs = _list_cross_task_referenced_artifact_ids(store, task_id)

    run_replays: dict[str, RunReplayResult] = {}
    for run in runs:
        run_raw = store.runs.get_raw_run_row(run.run_id)
        run_baseline = _parse_optional_run_baseline(run_raw, inconsistencies)
        run_entries = store.events.read_entries(run_id=run.run_id)
        run_replay = replay_run_history(run_entries, run_id=run.run_id, baseline_run=run_baseline)
        run_replays[run.run_id] = run_replay
        if run_replay.status != "VALID":
            inconsistencies.append(
                f"Run '{run.run_id}' replay status is {run_replay.status}."
            )
        elif run_replay.replayed_status != run.status:
            inconsistencies.append(
                f"Run '{run.run_id}' snapshot status '{run.status}' disagrees with replayed status '{run_replay.replayed_status}'."
            )

    snapshot_matches_replay = False
    if snapshot_task is not None and replay_result.status == "VALID":
        replayed = replay_result.reconstructed_task
        if replayed is None:
            inconsistencies.append("Replay returned VALID without reconstructed_task.")
        else:
            if snapshot_task.status != replayed.status:
                inconsistencies.append(
                    f"Task snapshot status '{snapshot_task.status}' differs from replayed '{replayed.status}'."
                )
            if snapshot_task.active_run_id != replayed.active_run_id:
                inconsistencies.append(
                    f"Task snapshot active_run_id '{snapshot_task.active_run_id}' differs from replayed '{replayed.active_run_id}'."
                )
            if snapshot_task.status == "COMPLETED":
                missing_arts = set(replay_result.artifact_ids) - (
                    set(snapshot_task.artifact_ids) | cross_task_refs
                )
                if missing_arts:
                    inconsistencies.append(
                        f"Task snapshot artifact_ids omits replayed artifacts: {sorted(missing_arts)}."
                    )
            else:
                known_arts = (
                    set(snapshot_task.artifact_ids)
                    | {a.artifact_id for a in artifacts}
                    | cross_task_refs
                )
                missing_arts = set(replay_result.artifact_ids) - known_arts
                if missing_arts:
                    inconsistencies.append(
                        f"Persisted artifacts missing replayed references: {sorted(missing_arts)}."
                    )
            snapshot_matches_replay = len(inconsistencies) == 0

    interrupted: list[InterruptedWorkAssessment] = []
    if snapshot_task is not None and snapshot_task.status == "RUNNING":
        interrupted.append(
            InterruptedWorkAssessment(
                entity_type="task",
                entity_id=snapshot_task.task_id,
                last_persisted_status=snapshot_task.status,
                observable_process_status="UNOBSERVABLE_AFTER_RESTART",
                reconciliation_required=True,
                reason=(
                    "Task was persisted as RUNNING before process restart; live worker state "
                    "cannot be assumed active or auto-completed without P04.06 reconciliation."
                ),
            )
        )
    for run in runs:
        if run.status == "RUNNING":
            interrupted.append(
                InterruptedWorkAssessment(
                    entity_type="run",
                    entity_id=run.run_id,
                    last_persisted_status=run.status,
                    observable_process_status="UNOBSERVABLE_AFTER_RESTART",
                    reconciliation_required=True,
                    reason=(
                        "Run was persisted as RUNNING before restart; no live CLI process handle "
                        "is observable from storage alone."
                    ),
                )
            )

    if replay_result.status == "INVALID_HISTORY" or any(
        rr.status == "INVALID_HISTORY" for rr in run_replays.values()
    ):
        recovery_status: RecoveryStatus = "INVALID_HISTORY"
    elif (
        replay_result.status == "INCOMPLETE_HISTORY"
        or snapshot_task is None
        or baseline_task is None
        or any(rr.status == "INCOMPLETE_HISTORY" for rr in run_replays.values())
    ):
        recovery_status = "INCOMPLETE_HISTORY"
    elif inconsistencies:
        recovery_status = "INVALID_HISTORY"
    elif interrupted:
        recovery_status = "NEEDS_RECONCILIATION"
    else:
        recovery_status = "VERIFIED_CLEAN"

    return TaskRecoveryReport(
        task_id=task_id,
        snapshot_task=snapshot_task,
        snapshot_version=snapshot_version,
        baseline_task=baseline_task,
        replay_result=replay_result,
        runs=runs,
        run_replays=run_replays,
        sessions=sessions,
        checklist_items=checklist_items,
        artifacts=artifacts,
        timeline=timeline,
        snapshot_matches_replay=snapshot_matches_replay,
        inconsistencies=inconsistencies,
        interrupted_work=interrupted,
        recovery_status=recovery_status,
    )


def recover_run(
    store: SQLiteRepositoryStore,
    run_id: str,
    *,
    require_contiguous_sequences: bool = False,
) -> RunRecoveryReport:
    """Rehydrate a single Run after restart, replay its Events, and detect interrupted RUNNING state."""
    inconsistencies: list[str] = []
    raw_row = store.runs.get_raw_run_row(run_id)
    baseline_run = _parse_optional_run_baseline(raw_row, inconsistencies)

    snapshot_run: Run | None = None
    snapshot_version: int | None = None
    if raw_row is not None:
        snapshot_version = int(raw_row["version"])
        try:
            snapshot_run = store.runs.get_run(run_id)
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            inconsistencies.append(f"Stored Run snapshot failed validation: {exc}")

    entries = store.events.read_entries(run_id=run_id)
    timeline = project_activity_timeline(store.events, run_id=run_id)
    replay_result = replay_run_history(
        entries,
        run_id=run_id,
        baseline_run=baseline_run,
        require_contiguous_sequences=require_contiguous_sequences,
    )
    sessions = store.sessions.list_sessions(run_id=run_id)
    artifacts = store.metadata.list_artifacts(run_id=run_id)

    snapshot_matches_replay = False
    if snapshot_run is not None and replay_result.status == "VALID":
        replayed = replay_result.reconstructed_run
        if replayed is None:
            inconsistencies.append("Run replay returned VALID without reconstructed_run.")
        else:
            if snapshot_run.status != replayed.status:
                inconsistencies.append(
                    f"Run snapshot status '{snapshot_run.status}' differs from replayed '{replayed.status}'."
                )
            snapshot_matches_replay = len(inconsistencies) == 0

    interrupted: list[InterruptedWorkAssessment] = []
    if snapshot_run is not None and snapshot_run.status == "RUNNING":
        interrupted.append(
            InterruptedWorkAssessment(
                entity_type="run",
                entity_id=snapshot_run.run_id,
                last_persisted_status=snapshot_run.status,
                observable_process_status="UNOBSERVABLE_AFTER_RESTART",
                reconciliation_required=True,
                reason=(
                    "Run was persisted as RUNNING before restart; process state is unobservable "
                    "without reconciliation."
                ),
            )
        )

    if replay_result.status == "INVALID_HISTORY":
        recovery_status: RecoveryStatus = "INVALID_HISTORY"
    elif replay_result.status == "INCOMPLETE_HISTORY" or snapshot_run is None or baseline_run is None:
        recovery_status = "INCOMPLETE_HISTORY"
    elif inconsistencies:
        recovery_status = "INVALID_HISTORY"
    elif interrupted:
        recovery_status = "NEEDS_RECONCILIATION"
    else:
        recovery_status = "VERIFIED_CLEAN"

    return RunRecoveryReport(
        run_id=run_id,
        snapshot_run=snapshot_run,
        snapshot_version=snapshot_version,
        baseline_run=baseline_run,
        replay_result=replay_result,
        sessions=sessions,
        artifacts=artifacts,
        timeline=timeline,
        snapshot_matches_replay=snapshot_matches_replay,
        inconsistencies=inconsistencies,
        interrupted_work=interrupted,
        recovery_status=recovery_status,
    )
