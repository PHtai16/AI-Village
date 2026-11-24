"""Opt-in live integration test for the P03.01 Master -> Worker -> QA -> Master spike."""

from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Iterator
import uuid

import pytest

from core.orchestration import AttributedSpikeEvent, run_master_worker_spike
from engines import (
    AntigravityEngineAdapter,
    EngineRegistry,
    compute_sha256_bytes,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_LIVE = os.environ.get("AI_VILLAGE_RUN_LIVE", "").strip() == "1"
logger = logging.getLogger(__name__)


@contextmanager
def _isolated_spike_workspaces() -> Iterator[tuple[Path, Path, Path]]:
    """Create disjoint master and worker directories inside ``workspace/`` with retry cleanup."""
    workspace_root = REPO_ROOT / "workspace"
    workspace_root.mkdir(exist_ok=True)
    run_root = Path(tempfile.mkdtemp(dir=str(workspace_root), prefix="p03_01_spike_"))
    master_ws = run_root / "master_ws"
    worker_ws = run_root / "worker_ws"
    master_ws.mkdir(parents=True)
    worker_ws.mkdir(parents=True)
    try:
        yield run_root, master_ws, worker_ws
    finally:
        for _ in range(10):
            if not run_root.exists():
                break
            shutil.rmtree(run_root, ignore_errors=True)
            if run_root.exists():
                time.sleep(0.3)


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="P03.01 Master -> Worker live spike runs only when AI_VILLAGE_RUN_LIVE=1.",
)
def test_live_master_worker_integration_spike() -> None:
    """Prove real Master AI -> Handoff -> Worker AI -> Independent QA -> Master Synthesis."""
    with _isolated_spike_workspaces() as (run_root, master_ws, worker_ws):
        marker = f"mk-{uuid.uuid4().hex[:12]}"
        pkg_payload = {
            "name": "ai-village-p03-01-spike",
            "version": "0.1.0",
            "private": True,
            "verification_marker": marker,
        }
        (worker_ws / "package.json").write_text(
            json.dumps(pkg_payload, indent=2) + "\n",
            encoding="utf-8",
        )
        (master_ws / "BRIEF.txt").write_text(
            "Worker workspace contains package.json; produce proof.txt there.\n",
            encoding="utf-8",
        )
        expected_proof = (
            f"name={pkg_payload['name']}\n"
            f"verification_marker={marker}\n"
        )
        expected_sha256 = compute_sha256_bytes(expected_proof.encode("utf-8"))

        # The hidden verification_marker value is NEVER placed in user_request or master_ws.
        user_request = (
            "Delegate to an authorized worker to read 'package.json' in its workspace without "
            "modifying 'package.json', extract the exact values of 'name' and "
            "'verification_marker', and create 'proof.txt' in the same workspace containing "
            "exact two UTF-8 LF-terminated lines:\n"
            "name=<name>\n"
            "verification_marker=<verification_marker>"
        )

        registry = EngineRegistry()
        registry.register_adapter(AntigravityEngineAdapter())
        streamed_events: list[AttributedSpikeEvent] = []

        result = run_master_worker_spike(
            registry=registry,
            user_request=user_request,
            master_workspace_dir=master_ws,
            worker_workspace_dir=worker_ws,
            allowed_root=run_root,
            expected_artifact_relpath="proof.txt",
            permitted_read_relpaths=("package.json",),
            permitted_write_relpaths=("proof.txt",),
            expected_artifact_content=expected_proof,
            required_artifact_substrings=(f"name={pkg_payload['name']}",),
            provider_id="antigravity",
            timeout_seconds=120,
            on_event=streamed_events.append,
        )

        assert result.status == "DELIVERED", (
            f"P03.01 live spike failed: status={result.status}, error={result.error}"
        )
        assert result.master_plan is not None
        assert result.master_plan.target_worker_id == "it.artifact_worker"
        assert result.worker_handoff is not None
        assert result.worker_execution is not None
        assert result.worker_execution.engine_result.status == "SUCCEEDED"
        assert result.worker_execution.engine_result.business_verification_status == "UNVERIFIED"
        assert "read_file" in result.worker_execution.engine_result.observed_tool_names
        assert "write_file" in result.worker_execution.engine_result.observed_tool_names
        assert result.qa_verification is not None
        assert result.qa_verification.status == "VERIFIED_PASS"
        assert result.qa_verification.actual_sha256 == expected_sha256
        assert result.qa_verification.source_integrity_verified is True
        assert result.master_synthesis is not None
        assert result.master_synthesis.delivery_status == "DELIVERED"
        assert result.master_synthesis.qa_status_observed == "VERIFIED_PASS"
        assert result.master_synthesis.artifact_sha256 == expected_sha256
        assert result.master_session_id != result.worker_session_id
        if result.master_plan_conversation_id and result.worker_conversation_id:
            assert result.master_plan_conversation_id != result.worker_conversation_id

        worker_provider_tools = [
            ev
            for ev in result.attributed_events
            if ev.stage == "worker_execute"
            and ev.engine_event.event_type == "tool_execution_observed"
            and ev.engine_event.source == "provider"
        ]
        assert worker_provider_tools, "Expected real provider tool execution events from Worker."

        evidence = {
            "correlation_id": result.correlation_id,
            "status": result.status,
            "master_session_id": result.master_session_id,
            "worker_session_id": result.worker_session_id,
            "master_plan_conversation_id": result.master_plan_conversation_id,
            "worker_conversation_id": result.worker_conversation_id,
            "master_synthesis_conversation_id": result.master_synthesis_conversation_id,
            "master_plan_duration_ms": (
                result.master_plan_result.duration_ms if result.master_plan_result else None
            ),
            "master_plan_telemetry": (
                result.master_plan_result.telemetry.model_dump()
                if result.master_plan_result
                else None
            ),
            "worker_duration_ms": result.worker_execution.engine_result.duration_ms,
            "worker_observed_tools": result.worker_execution.engine_result.observed_tool_names,
            "worker_telemetry": result.worker_execution.engine_result.telemetry.model_dump(),
            "qa_status": result.qa_verification.status,
            "qa_actual_sha256": result.qa_verification.actual_sha256,
            "qa_expected_sha256": expected_sha256,
            "qa_source_integrity_verified": result.qa_verification.source_integrity_verified,
            "master_synthesis_duration_ms": (
                result.master_synthesis_result.duration_ms if result.master_synthesis_result else None
            ),
            "master_synthesis_telemetry": (
                result.master_synthesis_result.telemetry.model_dump()
                if result.master_synthesis_result
                else None
            ),
            "total_duration_ms": result.duration_ms,
            "attributed_event_count": len(result.attributed_events),
        }
        logger.info("P03_01_LIVE_EVIDENCE %s", json.dumps(evidence, sort_keys=True))
