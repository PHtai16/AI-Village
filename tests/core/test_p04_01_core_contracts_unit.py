"""Pure offline unit tests verifying Phase 04 / P04.01 Core Contracts & Data Models."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys

from pydantic import ValidationError
import pytest

from core.contracts import (
    DEFAULT_DEPARTMENTS,
    Agent,
    AgentIdentity,
    AgentRuntimeStateRef,
    Artifact,
    ChecklistItem,
    ConnectionRef,
    Event,
    NotificationRef,
    ProjectRef,
    Run,
    RunErrorRef,
    RunTelemetryRef,
    Session,
    Task,
    connection_ref_from_engine,
    connection_ref_to_execution_routing,
    core_event_from_runtime_event,
    new_artifact_id,
    new_checklist_item_id,
    new_event_id,
    new_evidence_id,
    new_notification_id,
    new_project_id,
    new_run_id,
    new_session_id,
    new_task_id,
    run_telemetry_from_runtime,
    sanitize_event_payload,
)
from core.runtime.contract import (
    AgentRuntimeEvent,
    AgentRuntimeTelemetry,
    ExecutionRouting,
)
from engines.connections import EngineConnectionRef
from engines.contract import EngineEvent, EngineTelemetry

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_SHA256 = "a" * 64
FAKE_GOOGLE_KEY = "AIza" + "FakeKeyMaterial1234567890ABCD"
FAKE_OPENAI_KEY = "sk-" + "FakeOpenAIKeyMaterial1234567890"
FAKE_GITHUB_PAT = "ghp_" + "FakeGitHubToken1234567890ABCD"


@pytest.fixture
def base_utc() -> datetime:
    """Return a fixed timezone-aware UTC timestamp for deterministic tests."""
    return datetime(2026, 11, 24, 13, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def sample_connection() -> ConnectionRef:
    """Return a valid non-secret ConnectionRef instance."""
    return ConnectionRef(
        provider_id="antigravity",
        connection_id="default_antigravity",
        model_id="gemini-2.5-pro",
        project_ref="proj_ai_village",
        credential_ref="env:GEMINI_API_KEY",
    )


@pytest.fixture
def sample_task() -> Task:
    """Return a minimal valid Task without fabricated timestamps or runtime states."""
    return Task(
        task_id="task_1234567890abcdef",
        title="Verify artifact creation",
        description="Produce summary artifact via worker agent.",
        requested_by="operator.local",
    )


def test_task_valid_schema_and_no_fabricated_timestamps(
    sample_task: Task,
    base_utc: datetime,
) -> None:
    """Task validates defaults cleanly and keeps optional timestamps/run links as None."""
    assert sample_task.status == "QUEUED"
    assert sample_task.priority == "NORMAL"
    assert sample_task.project_id is None
    assert sample_task.created_at is None
    assert sample_task.updated_at is None
    assert sample_task.active_run_id is None
    assert not hasattr(sample_task, "transition_to")

    prj_id = new_project_id()
    chk_id = new_checklist_item_id()
    art_id = new_artifact_id()
    run_id = new_run_id()
    updated_task = Task(
        task_id=new_task_id(),
        project_id=prj_id,
        title="Grouped task",
        description="Detailed task brief",
        requested_by="operator",
        status="RUNNING",
        priority="HIGH",
        created_at=base_utc,
        updated_at=base_utc + timedelta(seconds=10),
        assigned_agent_ids=["core.master", "it.artifact_worker"],
        checklist_item_ids=[chk_id],
        active_run_id=run_id,
        artifact_ids=[art_id],
        acceptance_criteria=["Artifact passes independent SHA-256 check"],
        metadata={"source": "unit_test", "attempt": 1},
    )
    assert updated_task.project_id == prj_id
    assert updated_task.active_run_id == run_id


@pytest.mark.parametrize(
    "bad_task_id",
    [
        "",
        "   ",
        "run_1234567890abcdef",
        "art_1234567890abcdef",
        "task_x",
        "TASK_uppercase123",
        FAKE_OPENAI_KEY,
    ],
)
def test_task_invalid_ids_and_terminal_active_run_rejected(
    bad_task_id: str,
    base_utc: datetime,
) -> None:
    """Task rejects wrong domain ID prefixes, secrets, and active_run_id on terminal states."""
    with pytest.raises(ValidationError):
        Task(
            task_id=bad_task_id,
            title="Invalid task",
            description="Should fail",
            requested_by="operator",
        )

    with pytest.raises(ValidationError):
        Task(
            task_id=new_task_id(),
            title="Completed with dangling active run",
            description="Invalid terminal invariant",
            requested_by="operator",
            status="COMPLETED",
            created_at=base_utc,
            updated_at=base_utc,
            active_run_id=new_run_id(),
        )

    with pytest.raises(ValidationError):
        Task(
            task_id=new_task_id(),
            title="Secret in metadata",
            description="Invalid metadata",
            requested_by="operator",
            metadata={"api_key": FAKE_GOOGLE_KEY},
        )


def test_run_links_task_and_separates_multi_run_lifecycle(base_utc: datetime) -> None:
    """One Task can link multiple Runs whose statuses remain distinct from Task.status."""
    task = Task(
        task_id=new_task_id(),
        title="Multi-attempt execution",
        description="Retry creates a new Run rather than duplicating Task.",
        requested_by="operator",
        status="RUNNING",
        created_at=base_utc,
    )
    session_id = new_session_id()
    art_id = new_artifact_id()

    failed_run = Run(
        run_id=new_run_id(),
        task_id=task.task_id,
        runtime_id="custom.master_worker_v1",
        invocation_id="inv-attempt1",
        status="FAILED",
        started_at=base_utc + timedelta(seconds=1),
        finished_at=base_utc + timedelta(seconds=3),
        session_ids=[session_id],
        provider_id="antigravity",
        connection_id="default_antigravity",
        error_reference=RunErrorRef(
            error_code="WORKER_EXECUTION_FAILED",
            stage="worker_execute",
            message="Simulated failure",
            retryable=True,
        ),
        telemetry=RunTelemetryRef(
            total_duration_ms=2000,
            telemetry_status="UNAVAILABLE_FROM_PROVIDER",
            telemetry_source="unavailable",
        ),
    )
    succeeded_run = Run(
        run_id=new_run_id(),
        task_id=task.task_id,
        runtime_id="custom.master_worker_v1",
        invocation_id="inv-attempt2",
        status="SUCCEEDED",
        started_at=base_utc + timedelta(seconds=5),
        finished_at=base_utc + timedelta(seconds=12),
        session_ids=[new_session_id()],
        artifact_ids=[art_id],
        telemetry=RunTelemetryRef(
            total_duration_ms=7000,
            provider_duration_ms=6500,
            engine_call_count=3,
            input_tokens=100,
            output_tokens=20,
            total_tokens=120,
            telemetry_status="VERIFIED_MEASURED",
            telemetry_source="provider_cli",
        ),
    )

    assert failed_run.run_id != succeeded_run.run_id
    assert failed_run.task_id == task.task_id == succeeded_run.task_id
    assert task.status == "RUNNING"
    assert succeeded_run.status == "SUCCEEDED"

    with pytest.raises(ValidationError):
        Run(task_id="evt_1234567890abcdef", runtime_id="custom.master_worker_v1")
    with pytest.raises(ValidationError):
        Run(
            task_id=task.task_id,
            runtime_id="custom.master_worker_v1",
            status="PENDING",
            started_at=base_utc,
        )
    with pytest.raises(ValidationError):
        Run(
            task_id=task.task_id,
            runtime_id="custom.master_worker_v1",
            status="SUCCEEDED",
            started_at=base_utc,
            error_reference=RunErrorRef(error_code="ERR", message="Cannot attach to SUCCEEDED"),
        )
    with pytest.raises(ValidationError):
        RunTelemetryRef(
            telemetry_status="VERIFIED_MEASURED",
            telemetry_source="unavailable",
        )


def test_agent_identity_role_department_and_master_exception(
    sample_connection: ConnectionRef,
    base_utc: datetime,
) -> None:
    """AgentIdentity enforces AGENT_SPEC Master exception and Department!=Provider rule."""
    assert Agent is AgentIdentity
    assert {"hr", "resources", "service", "marketing", "it", "reception", "process", "qa", "vault"} == DEFAULT_DEPARTMENTS

    master = AgentIdentity(
        agent_id="core.master",
        name="Master Coordinator",
        role="Village Orchestrator",
        system_role="orchestrator",
        department="orchestration",
        capability_ids=["task_decomposition", "arbitration"],
        allowed_connections=[sample_connection],
    )
    worker = AgentIdentity(
        agent_id="it.code_reviewer",
        name="Quoc Bao",
        role="Senior Code Inspector",
        system_role="worker",
        department="it",
        capability_ids=["python_ast_audit"],
        allowed_connections=[sample_connection],
    )
    runtime_state = AgentRuntimeStateRef(
        agent_id=worker.agent_id,
        session_id=new_session_id(),
        current_task_id=new_task_id(),
        current_run_id=new_run_id(),
        status="RUNNING",
        active_connection=sample_connection,
        last_active_at=base_utc,
    )
    assert master.system_role == "orchestrator"
    assert runtime_state.agent_id == worker.agent_id

    # Invalid Master/orchestrator/provider combinations
    invalid_cases = [
        {"agent_id": "it.rogue", "system_role": "orchestrator", "department": "orchestration"},
        {"agent_id": "it.rogue", "system_role": "orchestrator", "department": "it"},
        {"agent_id": "core.master", "system_role": "worker", "department": "orchestration"},
        {"agent_id": "it.worker", "system_role": "worker", "department": "orchestration"},
        {"agent_id": "it.worker", "system_role": "worker", "department": "antigravity"},
        {"agent_id": "it.worker", "system_role": "worker", "department": "google_gemini"},
    ]
    for case in invalid_cases:
        with pytest.raises(ValidationError):
            AgentIdentity(
                name="Test Agent",
                role="Test Role",
                **case,  # type: ignore[arg-type]
            )


def test_session_uuid_v4_vs_legacy_spike_and_provider_thread(base_utc: datetime) -> None:
    """Session enforces UUID v4 IDs, supports P03 legacy refs cleanly, and separates provider threads."""
    sid = new_session_id()
    provider_thread = new_session_id()
    sess = Session(
        session_id=sid,
        legacy_session_ref="sess-worker-c779bb03f3",
        agent_id="it.artifact_worker",
        task_id=new_task_id(),
        run_id=new_run_id(),
        invocation_id="inv-03e9d5039fc8",
        provider_id="antigravity",
        connection_id="default_antigravity",
        provider_conversation_id=provider_thread,
        workspace_reference="workspace/task_01",
        status="ACTIVE",
        started_at=base_utc,
        finished_at=base_utc + timedelta(seconds=30),
    )
    assert sess.session_id == sid
    assert sess.provider_conversation_id == provider_thread
    assert sess.legacy_session_ref == "sess-worker-c779bb03f3"

    # Reject legacy sess-* directly inside canonical session_id
    with pytest.raises(ValidationError):
        Session(session_id="sess-worker-c779bb03f3", agent_id="it.artifact_worker")

    # Reject provider thread colliding with logical session_id
    with pytest.raises(ValidationError):
        Session(
            session_id=sid,
            agent_id="it.artifact_worker",
            provider_conversation_id=sid,
        )

    # Reject workspace path traversal and INITIALIZED pre-dated timestamps
    with pytest.raises(ValidationError):
        Session(agent_id="it.artifact_worker", workspace_reference="../outside_escape")
    with pytest.raises(ValidationError):
        Session(agent_id="it.artifact_worker", status="INITIALIZED", started_at=base_utc)


def test_artifact_metadata_sha256_and_independent_qa_boundary(tmp_path: Path) -> None:
    """Artifact rejects path escapes, ignores disk state, and never auto-PASSes on SHA-256."""
    nonexistent_rel = f"nonexistent_dir_{tmp_path.name}/proof.txt"
    art_unverified = Artifact(
        task_id=new_task_id(),
        run_id=new_run_id(),
        owner_agent_id="it.artifact_worker",
        relative_path=nonexistent_rel,
        content_type="text/plain",
        size_bytes=42,
        sha256=SAMPLE_SHA256.upper(),
        version="1.0.0",
    )
    assert art_unverified.sha256 == SAMPLE_SHA256
    assert art_unverified.verification_status == "UNVERIFIED"

    # VERIFIED_PASS requires both independent evidence_reference and sha256
    with pytest.raises(ValidationError):
        Artifact(
            task_id=new_task_id(),
            run_id=new_run_id(),
            owner_agent_id="it.artifact_worker",
            relative_path="out/proof.txt",
            sha256=SAMPLE_SHA256,
            verification_status="VERIFIED_PASS",
        )
    with pytest.raises(ValidationError):
        Artifact(
            task_id=new_task_id(),
            run_id=new_run_id(),
            owner_agent_id="it.artifact_worker",
            relative_path="out/proof.txt",
            verification_status="VERIFIED_PASS",
            evidence_reference=new_evidence_id(),
        )

    verified = Artifact(
        task_id=new_task_id(),
        run_id=new_run_id(),
        owner_agent_id="it.artifact_worker",
        relative_path="out/proof.txt",
        sha256=SAMPLE_SHA256,
        verification_status="VERIFIED_PASS",
        evidence_reference=new_evidence_id(),
    )
    assert verified.verification_status == "VERIFIED_PASS"

    for bad_path in ("../secret.txt", "/etc/passwd", "C:\\Windows\\win.ini", "out/*.txt"):
        with pytest.raises(ValidationError):
            Artifact(
                task_id=new_task_id(),
                run_id=new_run_id(),
                owner_agent_id="it.artifact_worker",
                relative_path=bad_path,
            )


def test_event_references_optional_provider_and_secret_payload_safety(base_utc: datetime) -> None:
    """Event validates full and partial anchors cleanly and blocks/redacts payload secrets."""
    ev_full = Event(
        event_id=new_event_id(),
        timestamp=base_utc,
        event_type="artifact.recorded",
        source="qa_verifier",
        task_id=new_task_id(),
        run_id=new_run_id(),
        session_id=new_session_id(),
        actor_id="qa.independent_verifier",
        provider_id="antigravity",
        connection_id="default_antigravity",
        model_id="gemini-2.5-pro",
        artifact_id=new_artifact_id(),
        evidence_id=new_evidence_id(),
        correlation_id="corr-001",
        redacted_payload={"sha256": SAMPLE_SHA256},
    )
    assert ev_full.provider_id == "antigravity"

    # Valid internal event omitting optional provider/model/artifact/evidence fields
    ev_internal = Event(
        timestamp=base_utc,
        event_type="task.created",
        source="core_runtime",
        task_id=new_task_id(),
        redacted_payload={"priority": "NORMAL"},
    )
    assert ev_internal.provider_id is None
    assert ev_internal.connection_id is None
    assert ev_internal.model_id is None
    assert ev_internal.artifact_id is None
    assert ev_internal.evidence_id is None

    # Completely unattributed Event rejected
    with pytest.raises(ValidationError):
        Event(
            timestamp=base_utc,
            event_type="task.created",
            source="core_runtime",
        )

    # Secret keys or raw tokens in redacted_payload rejected fail-closed
    for bad_payload in (
        {"api_key": "public_key_id"},
        {"auth_token": "something"},
        {"nested": [{"secret": "value"}]},
        {"output": f"Bearer {FAKE_OPENAI_KEY}"},
        {"stdout": FAKE_GOOGLE_KEY},
        {"pat": FAKE_GITHUB_PAT},
    ):
        with pytest.raises(ValidationError):
            Event(
                timestamp=base_utc,
                event_type="runtime.error",
                source="core_runtime",
                task_id=new_task_id(),
                redacted_payload=bad_payload,
            )

    # Usage telemetry token counts remain allowed in payload
    ev_tokens = Event(
        timestamp=base_utc,
        event_type="engine.event_forwarded",
        source="engine_adapter",
        task_id=new_task_id(),
        redacted_payload={"input_tokens": 50, "total_tokens": 75},
    )
    assert ev_tokens.redacted_payload["total_tokens"] == 75

    # sanitize_event_payload scrubs nested secrets so Event validation passes
    cleaned = sanitize_event_payload(
        {"api_key": FAKE_GOOGLE_KEY, "nested": [{"stdout": f"leak={FAKE_OPENAI_KEY}"}]}
    )
    ev_clean = Event(
        timestamp=base_utc,
        event_type="runtime.error",
        source="core_runtime",
        task_id=new_task_id(),
        redacted_payload=cleaned,
    )
    assert ev_clean.redacted_payload["api_key"] == "[REDACTED_KEY_VALUE]"
    assert FAKE_GOOGLE_KEY not in str(ev_clean.redacted_payload)
    assert FAKE_OPENAI_KEY not in str(ev_clean.redacted_payload)


def test_connection_ref_project_ref_checklist_and_notification(
    sample_connection: ConnectionRef,
    base_utc: datetime,
) -> None:
    """ConnectionRef, ProjectRef, ChecklistItem, and NotificationRef uphold safety invariants."""
    safe_summary = sample_connection.to_safe_summary()
    assert safe_summary["credential_ref"] == "env:***"

    for bad_cred in (FAKE_GOOGLE_KEY, FAKE_OPENAI_KEY, FAKE_GITHUB_PAT, "sk-short", "bad ref"):
        with pytest.raises(ValidationError):
            ConnectionRef(
                provider_id="google_gemini",
                connection_id="gemini_primary",
                credential_ref=bad_cred,
            )

    # ProjectRef is strictly organizational grouping with extra="forbid"
    project = ProjectRef(project_id=new_project_id(), name="Release Automation")
    assert project.project_id.startswith("prj_")
    with pytest.raises(ValidationError):
        ProjectRef(name="Bad Runtime", status="RUNNING")  # type: ignore[call-arg]

    # ChecklistItem forbids COMPLETED without evidence and completed_at, and forbids progress_percent
    task_id = new_task_id()
    with pytest.raises(ValidationError):
        ChecklistItem(task_id=task_id, title="Done without proof", status="COMPLETED", order=1)
    with pytest.raises(ValidationError):
        ChecklistItem(
            task_id=task_id,
            title="Done without timestamp",
            status="COMPLETED",
            order=1,
            evidence_reference=new_evidence_id(),
        )
    with pytest.raises(ValidationError):
        ChecklistItem(
            task_id=task_id,
            title="No progress percent",
            order=1,
            progress_percent=50,  # type: ignore[call-arg]
        )

    chk = ChecklistItem(
        task_id=task_id,
        title="Verify output SHA-256",
        assigned_agent_id="qa.verifier",
        status="COMPLETED",
        order=2,
        evidence_reference=new_evidence_id(),
        created_at=base_utc,
        completed_at=base_utc + timedelta(seconds=5),
    )
    assert chk.status == "COMPLETED"

    notif = NotificationRef(
        notification_id=new_notification_id(),
        task_id=task_id,
        run_id=new_run_id(),
        event_id=new_event_id(),
        severity="WARNING",
        notification_type="QA_RESULT",
        created_at=base_utc,
    )
    assert notif.notification_id.startswith("ntf_")


def test_timezone_chronology_unknown_fields_and_nullable_json_roundtrip(
    sample_connection: ConnectionRef,
    base_utc: datetime,
) -> None:
    """All models reject naive timestamps, chronological inversions, extra fields, and preserve nulls."""
    naive = datetime(2026, 11, 24, 13, 0, 0)
    task_id = new_task_id()
    run_id = new_run_id()
    evt_id = new_event_id()

    with pytest.raises(ValidationError):
        Task(task_id=task_id, title="T", description="D", requested_by="op", created_at=naive)
    with pytest.raises(ValidationError):
        Event(timestamp=naive, event_type="task.created", source="core_runtime", task_id=task_id)
    with pytest.raises(ValidationError):
        NotificationRef(
            event_id=evt_id,
            notification_type="SYSTEM_NOTICE",
            created_at=naive,
        )
    with pytest.raises(ValidationError):
        Task(
            task_id=task_id,
            title="T",
            description="D",
            requested_by="op",
            created_at=base_utc,
            updated_at=base_utc - timedelta(seconds=1),
        )
    with pytest.raises(ValidationError):
        Run(
            task_id=task_id,
            runtime_id="custom.master_worker_v1",
            status="FAILED",
            started_at=base_utc,
            finished_at=base_utc - timedelta(seconds=1),
        )

    models = [
        Task(
            task_id=task_id,
            project_id=None,
            title="Roundtrip task",
            description="Preserve nulls cleanly",
            requested_by="operator",
            created_at=base_utc,
        ),
        Run(
            run_id=run_id,
            task_id=task_id,
            runtime_id="custom.master_worker_v1",
            telemetry=RunTelemetryRef(
                total_duration_ms=100,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                estimated_cost_usd=None,
                actual_cost_usd=None,
            ),
        ),
        AgentIdentity(
            agent_id="core.master",
            name="Master",
            role="Orchestrator",
            system_role="orchestrator",
            department="orchestration",
            allowed_connections=[sample_connection],
        ),
        AgentRuntimeStateRef(agent_id="core.master", status="IDLE"),
        Session(agent_id="it.worker"),
        Artifact(
            task_id=task_id,
            run_id=run_id,
            owner_agent_id="it.worker",
            relative_path="out/report.md",
            sha256=None,
            version=None,
        ),
        Event(
            event_id=evt_id,
            timestamp=base_utc,
            event_type="task.created",
            source="core_runtime",
            task_id=task_id,
        ),
        sample_connection,
        ChecklistItem(task_id=task_id, title="Step 1", order=0),
        NotificationRef(
            event_id=evt_id,
            notification_type="TASK_UPDATE",
            created_at=base_utc,
        ),
        ProjectRef(name="Core Contracts"),
    ]

    for instance in models:
        cls = type(instance)
        dumped = instance.model_dump_json()
        restored = cls.model_validate_json(dumped)
        assert restored == instance

        # Unknown extra fields rejected on every model
        with pytest.raises(ValidationError):
            cls.model_validate({**instance.model_dump(), "unexpected_extra_field": "x"})

    # Verify RunTelemetryRef keeps None rather than converting to 0
    restored_run = Run.model_validate_json(models[1].model_dump_json())
    assert restored_run.telemetry is not None
    assert restored_run.telemetry.input_tokens is None
    assert restored_run.telemetry.total_tokens is None
    assert restored_run.telemetry.estimated_cost_usd is None
    assert restored_run.telemetry.actual_cost_usd is None


def test_architecture_boundaries_no_circular_import_or_forbidden_deps() -> None:
    """Verify clean fresh imports, zero CrewAI/UI/storage/DB/network deps in core/contracts/."""
    proc = subprocess.run(
        [sys.executable, "-c", "import core.contracts; print('OK')"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "OK"

    forbidden_prefixes = (
        "crewai",
        "ui",
        "storage",
        "api",
        "sqlite3",
        "sqlalchemy",
        "httpx",
        "urllib",
        "subprocess",
        "engines.antigravity.adapter",
        "engines.antigravity.lifecycle",
        "core.runtime.custom_runtime",
        "core.runtime.application",
    )
    for py_path in sorted((REPO_ROOT / "core" / "contracts").glob("*.py")):
        tree = ast.parse(py_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(forbidden_prefixes), (
                        f"Forbidden import {alias.name!r} in {py_path.name}"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(forbidden_prefixes), (
                    f"Forbidden import {node.module!r} in {py_path.name}"
                )


def test_agent_runtime_compatibility_helpers(base_utc: datetime) -> None:
    """Verify pure bridge helpers interoperate cleanly with AgentRuntime and EngineConnectionRef."""
    eng_conn = EngineConnectionRef(
        connection_id="gemini_primary",
        provider_id="google_gemini",
        default_model_id="gemini-2.5-pro",
        project_ref="billing_scope_a",
        credential_ref="env:GEMINI_API_KEY",
    )
    core_conn = connection_ref_from_engine(eng_conn)
    assert core_conn.credential_ref is None
    routing = connection_ref_to_execution_routing(core_conn)
    assert isinstance(routing, ExecutionRouting)
    assert routing.provider_id == "google_gemini"
    assert routing.connection_id == "gemini_primary"
    assert routing.model_id == "gemini-2.5-pro"

    rt_telem = AgentRuntimeTelemetry(
        total_duration_ms=1200,
        provider_duration_ms=1000,
        engine_call_count=1,
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        telemetry_status="UNAVAILABLE_FROM_PROVIDER",
        telemetry_source="unavailable",
    )
    run_telem = run_telemetry_from_runtime(rt_telem)
    assert run_telem.input_tokens is None
    assert run_telem.total_tokens is None

    engine_ev = EngineEvent(
        event_id="ev-1",
        sequence_number=1,
        timestamp=base_utc,
        event_type="tool_execution_observed",
        source="provider",
        provider_id="antigravity",
        connection_id="default_antigravity",
        status="IN_PROGRESS",
        tool_name="write_file",
        redacted_payload={"file": "proof.txt"},
    )
    spike_rt_ev = AgentRuntimeEvent(
        invocation_id="inv-abc",
        runtime_id="custom.master_worker_v1",
        sequence=1,
        stage="worker_execute",
        actor_id="it.artifact_worker",
        actor_role="worker",
        logical_session_id="sess-worker-123456",
        provider_id="antigravity",
        provider_conversation_id="conv-xyz",
        engine_event=engine_ev,
    )
    mapped_legacy = core_event_from_runtime_event(
        spike_rt_ev,
        task_id=new_task_id(),
        run_id=new_run_id(),
    )
    assert mapped_legacy.session_id is None
    assert mapped_legacy.redacted_payload["legacy_session_ref"] == "sess-worker-123456"
    assert mapped_legacy.timestamp == base_utc

    uuid_sid = new_session_id()
    uuid_rt_ev = spike_rt_ev.model_copy(update={"logical_session_id": uuid_sid})
    mapped_uuid = core_event_from_runtime_event(
        uuid_rt_ev,
        task_id=new_task_id(),
        run_id=new_run_id(),
    )
    assert mapped_uuid.session_id == uuid_sid

    no_ts_rt_ev = spike_rt_ev.model_copy(update={"engine_event": None})
    with pytest.raises(ValueError):
        core_event_from_runtime_event(no_ts_rt_ev, task_id=new_task_id())
