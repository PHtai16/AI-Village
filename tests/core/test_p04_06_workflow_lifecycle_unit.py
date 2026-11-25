"""Unit tests for Phase 04 / P04.06 fallback policy, lifecycle guards, and boundaries."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.contracts import Run, Task, new_run_id, new_task_id
from core.tasks import (
    FallbackEvaluationRequest,
    FallbackPolicy,
    FallbackTarget,
    HandleTerminationOutcome,
    IdempotentSubmissionRequest,
    TransitionContext,
    WorkflowLifecycleController,
    compute_submission_fingerprint,
    evaluate_fallback,
)
from core.tasks.transitions import ExecutionEvidence
from storage.db import apply_migrations, sqlite_connection
from storage.repositories import PersistentWorkflowService, SQLiteLifecyclePersistenceAdapter, SQLiteRepositoryStore

REPO_ROOT = Path(__file__).resolve().parents[2]


class _FakeHandle:
    """Deterministic in-memory ``OwnedExecutionHandle`` for unit boundary tests."""

    def __init__(
        self,
        execution_id: str,
        owner_token: str,
        *,
        running: bool = True,
        confirm_stop: bool = True,
        already_succeeded: bool = False,
        pid: int | None = 424242,
    ) -> None:
        self._execution_id = execution_id
        self._owner_token = owner_token
        self._running = running
        self._confirm_stop = confirm_stop
        self._already_succeeded = already_succeeded
        self._pid = pid
        self.cancel_calls = 0

    @property
    def execution_id(self) -> str:
        return self._execution_id

    @property
    def owner_token(self) -> str:
        return self._owner_token

    @property
    def pid(self) -> int | None:
        return self._pid

    def is_running(self) -> bool:
        return self._running

    def request_cancel(
        self,
        *,
        owner_token: str,
        wait: bool = True,
        timeout_seconds: float = 5.0,
    ) -> HandleTerminationOutcome:
        assert owner_token == self._owner_token
        self.cancel_calls += 1
        if self._already_succeeded:
            self._running = False
            return HandleTerminationOutcome(
                execution_id=self._execution_id,
                confirmed_stopped=True,
                already_terminal=True,
                terminal_status="SUCCEEDED",
                exit_code=0,
                mechanism="already_completed",
            )
        if self._confirm_stop and wait:
            self._running = False
            return HandleTerminationOutcome(
                execution_id=self._execution_id,
                confirmed_stopped=True,
                already_terminal=False,
                terminal_status="CANCELLED",
                exit_code=1,
                mechanism="fake_stop",
            )
        return HandleTerminationOutcome(
            execution_id=self._execution_id,
            confirmed_stopped=False,
            already_terminal=False,
            terminal_status="STILL_RUNNING",
            mechanism="fake_still_running",
        )

    def wait_for_exit(self, *, timeout_seconds: float = 5.0) -> HandleTerminationOutcome:
        return HandleTerminationOutcome(
            execution_id=self._execution_id,
            confirmed_stopped=not self._running,
            already_terminal=not self._running,
            terminal_status="STILL_RUNNING" if self._running else "SUCCEEDED",
        )


def test_core_tasks_zero_sqlite_storage_or_subprocess_imports() -> None:
    """All modules in core/tasks/ must remain pure from sqlite3, storage, and subprocess."""
    forbidden = ("sqlite3", "storage", "sqlalchemy", "subprocess", "crewai", "api", "ui")
    for py_file in (REPO_ROOT / "core" / "tasks").glob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(forbidden), f"{alias.name} forbidden in {py_file}"
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(forbidden), f"{node.module} forbidden in {py_file}"


def test_fallback_policy_blocks_key_rotation_budget_residency_and_exhausted_retries() -> None:
    """Fallback evaluator enforces retry cap, capability check, no key rotation, and approval gates."""
    t0 = datetime(2026, 11, 25, 16, 0, 0, tzinfo=timezone.utc)
    tid, rid = new_task_id(), new_run_id()
    local_target = FallbackTarget(
        provider_id="ollama",
        connection_id="local_ollama",
        model_id="qwen2.5",
        required_capabilities=["text_generation"],
        data_residency="local_only",
        billing_scope="local_free",
        estimated_cost_usd=0.0,
    )
    cloud_target = FallbackTarget(
        provider_id="google_gemini",
        connection_id="gemini_primary",
        model_id="gemini-2.5-pro",
        required_capabilities=["text_generation"],
        data_residency="cloud",
        billing_scope="local_free",
        estimated_cost_usd=0.05,
    )
    same_provider_alt_key = FallbackTarget(
        provider_id="google_gemini",
        connection_id="gemini_secondary",
        model_id="gemini-2.5-pro",
        required_capabilities=["text_generation"],
        data_residency="cloud",
        billing_scope="local_free",
    )

    policy = FallbackPolicy(
        fallback_allowed=True,
        max_retries=2,
        allowed_targets=[cloud_target],
        allow_provider_switch=True,
        allow_data_residency_change=False,
        allow_billing_scope_change=False,
        max_budget_usd=0.10,
        require_idempotent_operation=True,
    )

    # 1. Explicit key rotation attempt rejected.
    rot = evaluate_fallback(
        policy,
        FallbackEvaluationRequest(
            task_id=tid,
            run_id=rid,
            current_target=local_target,
            candidate_target=local_target,
            attempt_number=1,
            operation_is_idempotent=True,
            failure_reason_code="RATE_LIMITED",
            available_target_capabilities=["text_generation"],
            is_key_rotation_attempt=True,
            requested_at=t0,
        ),
    )
    assert rot.decision == "DENY" and rot.reason_code == "KEY_ROTATION_FORBIDDEN"

    # 2. Implicit same-provider connection rotation on quota/rate limit rejected.
    key_swap = evaluate_fallback(
        FallbackPolicy(
            fallback_allowed=True,
            max_retries=2,
            allowed_targets=[same_provider_alt_key],
            allow_provider_switch=True,
        ),
        FallbackEvaluationRequest(
            task_id=tid,
            run_id=rid,
            current_target=cloud_target,
            candidate_target=same_provider_alt_key,
            attempt_number=1,
            operation_is_idempotent=True,
            failure_reason_code="QUOTA_EXCEEDED",
            available_target_capabilities=["text_generation"],
            requested_at=t0,
        ),
    )
    assert key_swap.decision == "DENY" and key_swap.reason_code == "KEY_ROTATION_FORBIDDEN"

    # 3. Retry budget exhausted rejected.
    exhausted = evaluate_fallback(
        policy,
        FallbackEvaluationRequest(
            task_id=tid,
            run_id=rid,
            current_target=local_target,
            candidate_target=local_target,
            attempt_number=3,
            operation_is_idempotent=True,
            failure_reason_code="NETWORK_ERROR",
            available_target_capabilities=["text_generation"],
            requested_at=t0,
        ),
    )
    assert exhausted.decision == "DENY" and exhausted.reason_code == "RETRY_BUDGET_EXHAUSTED"

    # 4. Non-idempotent side-effecting op cannot retry.
    non_idem = evaluate_fallback(
        policy,
        FallbackEvaluationRequest(
            task_id=tid,
            run_id=rid,
            current_target=local_target,
            candidate_target=local_target,
            attempt_number=1,
            operation_is_idempotent=False,
            failure_reason_code="NETWORK_ERROR",
            available_target_capabilities=["text_generation"],
            requested_at=t0,
        ),
    )
    assert non_idem.decision == "DENY" and non_idem.reason_code == "NON_IDEMPOTENT_OPERATION"

    # 5. Unconfigured fallback target rejected.
    unconfigured = evaluate_fallback(
        policy,
        FallbackEvaluationRequest(
            task_id=tid,
            run_id=rid,
            current_target=local_target,
            candidate_target=same_provider_alt_key,
            attempt_number=1,
            operation_is_idempotent=True,
            failure_reason_code="PROVIDER_ERROR",
            available_target_capabilities=["text_generation"],
            requested_at=t0,
        ),
    )
    assert unconfigured.decision == "DENY" and unconfigured.reason_code == "TARGET_NOT_CONFIGURED"

    # 6. Local-only -> cloud requires explicit human approval.
    residency = evaluate_fallback(
        policy,
        FallbackEvaluationRequest(
            task_id=tid,
            run_id=rid,
            current_target=local_target,
            candidate_target=cloud_target,
            attempt_number=1,
            operation_is_idempotent=True,
            failure_reason_code="PROVIDER_ERROR",
            available_target_capabilities=["text_generation"],
            requested_at=t0,
        ),
    )
    assert residency.decision == "APPROVAL_REQUIRED"

    # 7. Missing capability rejected.
    missing_cap = evaluate_fallback(
        FallbackPolicy(
            fallback_allowed=True,
            max_retries=2,
            allowed_targets=[cloud_target],
            allow_provider_switch=True,
            allow_data_residency_change=True,
        ),
        FallbackEvaluationRequest(
            task_id=tid,
            run_id=rid,
            current_target=local_target,
            candidate_target=cloud_target,
            attempt_number=1,
            operation_is_idempotent=True,
            failure_reason_code="PROVIDER_ERROR",
            available_target_capabilities=[],
            requested_at=t0,
        ),
    )
    assert missing_cap.decision == "DENY" and missing_cap.reason_code == "UNSUPPORTED_CAPABILITY"

    # 8. Budget overrun requires approval.
    over_budget = evaluate_fallback(
        FallbackPolicy(
            fallback_allowed=True,
            max_retries=2,
            allowed_targets=[cloud_target.model_copy(update={"estimated_cost_usd": 1.5})],
            allow_provider_switch=True,
            allow_data_residency_change=True,
            max_budget_usd=0.5,
        ),
        FallbackEvaluationRequest(
            task_id=tid,
            run_id=rid,
            current_target=local_target,
            candidate_target=cloud_target.model_copy(update={"estimated_cost_usd": 1.5}),
            attempt_number=1,
            operation_is_idempotent=True,
            failure_reason_code="PROVIDER_ERROR",
            available_target_capabilities=["text_generation"],
            requested_at=t0,
        ),
    )
    assert over_budget.decision == "APPROVAL_REQUIRED" and over_budget.reason_code == "BUDGET_EXCEEDED_REQUIRES_APPROVAL"

    # 9. Valid configured target allowed cleanly.
    allowed = evaluate_fallback(
        FallbackPolicy(
            fallback_allowed=True,
            max_retries=2,
            allowed_targets=[cloud_target],
            allow_provider_switch=True,
            allow_data_residency_change=True,
            max_budget_usd=1.0,
        ),
        FallbackEvaluationRequest(
            task_id=tid,
            run_id=rid,
            current_target=local_target,
            candidate_target=cloud_target,
            attempt_number=1,
            operation_is_idempotent=True,
            failure_reason_code="PROVIDER_ERROR",
            available_target_capabilities=["text_generation"],
            requested_at=t0,
        ),
    )
    assert allowed.decision == "ALLOW_FALLBACK"


def test_cancellation_never_fabricates_cancelled_when_process_unconfirmed(tmp_path: Path) -> None:
    """If handle does not confirm termination or completed first, controller never fakes Run.CANCELLED."""
    db_path = tmp_path / "cancel_unit.db"
    with sqlite_connection(db_path) as conn:
        apply_migrations(conn)

    t0 = datetime(2026, 11, 25, 16, 10, 0, tzinfo=timezone.utc)
    task = Task(
        task_id=new_task_id(),
        title="Unit cancel truth",
        description="Verify no fabricated CANCELLED status.",
        requested_by="operator.local",
        status="QUEUED",
        created_at=t0,
    )
    with SQLiteRepositoryStore(db_path) as store:
        svc = PersistentWorkflowService(store)
        svc.create_task(task)
        svc.apply_task_transition(
            task.task_id,
            "ASSIGNED",
            TransitionContext(
                actor_id="core.master",
                actor_kind="orchestrator",
                timestamp=t0 + timedelta(seconds=1),
                reason="Assign",
                assigned_agent_ids=["it.worker"],
            ),
        )
        run = Run(
            run_id=new_run_id(),
            task_id=task.task_id,
            runtime_id="custom.master_worker_v1",
            status="PENDING",
        )
        svc.create_run(run)
        svc.apply_paired_transition(
            "start",
            task.task_id,
            run.run_id,
            TransitionContext(
                actor_id="core.master",
                actor_kind="core_runtime",
                timestamp=t0 + timedelta(seconds=2),
                reason="Start",
                execution_evidence=ExecutionEvidence(
                    source="unit_test_fixture",
                    run_id=run.run_id,
                    reason="Start",
                ),
            ),
        )

        port = SQLiteLifecyclePersistenceAdapter(store)
        ctrl = WorkflowLifecycleController(port)

        # Unconfirmed stop returns CANCEL_FAILED and keeps Run RUNNING.
        stubborn = _FakeHandle("exec-stubborn", "tok-1", running=True, confirm_stop=False)
        ctrl.register_owned_execution(task.task_id, run.run_id, stubborn, timestamp=t0 + timedelta(seconds=2))
        res_fail = ctrl.request_cancel(
            task.task_id,
            run.run_id,
            actor_id="operator.local",
            actor_kind="operator",
            timestamp=t0 + timedelta(seconds=3),
            reason="Cancel attempt",
            wait=True,
        )
        assert res_fail.status == "CANCEL_FAILED"
        assert res_fail.cancel_confirmed is False
        assert store.runs.get_run(run.run_id).status == "RUNNING"  # type: ignore[union-attr]

        # Wrong owner token rejected.
        wrong_owner = ctrl.request_cancel(
            task.task_id,
            run.run_id,
            actor_id="operator.local",
            actor_kind="operator",
            timestamp=t0 + timedelta(seconds=4),
            reason="Wrong token",
            owner_token="wrong-token",
        )
        assert wrong_owner.status == "OWNERSHIP_REJECTED"

        # If execution already succeeded in a race, return ALREADY_TERMINAL without forging CANCELLED.
        raced = _FakeHandle("exec-raced", "tok-2", running=False, already_succeeded=True)
        ctrl.register_owned_execution(task.task_id, run.run_id, raced, timestamp=t0 + timedelta(seconds=4))
        res_race = ctrl.request_cancel(
            task.task_id,
            run.run_id,
            actor_id="operator.local",
            actor_kind="operator",
            timestamp=t0 + timedelta(seconds=5),
            reason="Late cancel",
            owner_token="tok-2",
        )
        assert res_race.status == "ALREADY_TERMINAL"
        assert res_race.race_outcome == "execution_completed_before_cancel"
        assert store.runs.get_run(run.run_id).status == "RUNNING"  # type: ignore[union-attr]


def test_submission_fingerprint_stability() -> None:
    """Identical canonical payload yields identical SHA-256 fingerprint while differing payload changes it."""
    t0 = datetime(2026, 11, 25, 16, 20, 0, tzinfo=timezone.utc)
    tid = new_task_id()
    run1 = Run(run_id=new_run_id(), task_id=tid, runtime_id="custom.master_worker_v1")
    run2 = Run(run_id=new_run_id(), task_id=tid, runtime_id="custom.master_worker_v1")
    req1 = IdempotentSubmissionRequest(
        scope="run_submit",
        idempotency_key="idem-key-001",
        task_id=tid,
        run=run1,
        payload={"b": 2, "a": 1},
        submitted_at=t0,
    )
    req2 = IdempotentSubmissionRequest(
        scope="run_submit",
        idempotency_key="idem-key-001",
        task_id=tid,
        run=run2,
        payload={"a": 1, "b": 2},
        submitted_at=t0 + timedelta(seconds=5),
    )
    req3 = req2.model_copy(update={"payload": {"a": 1, "b": 99}})
    assert compute_submission_fingerprint(req1) == compute_submission_fingerprint(req2)
    assert compute_submission_fingerprint(req1) != compute_submission_fingerprint(req3)
