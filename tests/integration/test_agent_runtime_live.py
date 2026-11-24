"""Opt-in live smoke test verifying Application -> AgentRuntime -> CustomAgentRuntime -> Antigravity CLI."""

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

from core.runtime import (
    AgentRuntime,
    AgentRuntimeEvent,
    AgentRuntimeRequest,
    AgentWorkflowRunner,
    ArtifactAcceptanceSpec,
    CustomAgentRuntime,
    ExecutionRouting,
    WorkspaceScope,
)
from engines import AntigravityEngineAdapter, EngineRegistry, compute_sha256_bytes

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_LIVE = os.environ.get("AI_VILLAGE_RUN_LIVE", "").strip() == "1"
logger = logging.getLogger(__name__)


@contextmanager
def _isolated_runtime_workspaces() -> Iterator[tuple[Path, Path, Path]]:
    """Create disjoint master and worker directories inside ``workspace/`` with retry cleanup."""
    workspace_root = REPO_ROOT / "workspace"
    workspace_root.mkdir(exist_ok=True)
    run_root = Path(tempfile.mkdtemp(dir=str(workspace_root), prefix="p03_03_runtime_"))
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


def _compose_custom_runtime_runner() -> AgentWorkflowRunner:
    """Composition root binding AntigravityEngineAdapter into AgentWorkflowRunner."""
    registry = EngineRegistry()
    registry.register_adapter(AntigravityEngineAdapter())
    runtime: AgentRuntime = CustomAgentRuntime(registry=registry)
    return AgentWorkflowRunner(runtime)


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="P03.03 live smoke test runs only when AI_VILLAGE_RUN_LIVE=1.",
)
def test_live_agent_runtime_custom_abstraction_smoke() -> None:
    """Prove Application -> AgentRuntime -> CustomAgentRuntime -> agy.exe -> QA SHA-256."""
    with _isolated_runtime_workspaces() as (run_root, master_ws, worker_ws):
        marker = f"mk-{uuid.uuid4().hex[:12]}"
        pkg_payload = {
            "name": "ai-village-p03-03-runtime",
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

        # Caller invokes only AgentWorkflowRunner using AgentRuntimeRequest without concrete runtime imports.
        runner = _compose_custom_runtime_runner()
        assert runner.runtime_info.runtime_kind == "custom_orchestrator"
        assert runner.runtime_info.supports_independent_qa is True

        request = AgentRuntimeRequest(
            user_objective=(
                "Delegate to an authorized worker to read 'package.json' in its workspace "
                "without modifying 'package.json', extract exact values of 'name' and "
                "'verification_marker', and create 'proof.txt' containing two UTF-8 LF lines:\n"
                "name=<name>\n"
                "verification_marker=<verification_marker>"
            ),
            workspace_scope=WorkspaceScope(
                allowed_root_path=str(run_root),
                orchestrator_workspace_path=str(master_ws),
                worker_workspace_path=str(worker_ws),
                permitted_read_paths=["package.json"],
                permitted_write_paths=["proof.txt"],
            ),
            routing=ExecutionRouting(provider_id="antigravity"),
            acceptance=ArtifactAcceptanceSpec(
                artifact_relpath="proof.txt",
                expected_content=expected_proof,
                required_substrings=[f"name={pkg_payload['name']}"],
            ),
            timeout_seconds=120,
        )

        streamed_events: list[AgentRuntimeEvent] = []
        result = runner.run(request, on_event=streamed_events.append)

        assert result.status == "DELIVERED", (
            f"P03.03 live smoke failed: status={result.status}, error={result.error}"
        )
        assert result.technical_execution_succeeded is True
        assert result.business_verified is True
        assert result.actors_executed == ["core.master", "it.artifact_worker"]
        assert result.orchestrator_session_id != result.worker_session_id
        assert len(set(result.provider_conversation_ids)) == len(result.provider_conversation_ids)
        assert "read_file" in result.observed_tool_names
        assert "write_file" in result.observed_tool_names
        assert result.qa_verification is not None
        assert result.qa_verification.status == "VERIFIED_PASS"
        assert result.qa_verification.actual_sha256 == expected_sha256
        assert result.qa_verification.source_integrity_verified is True
        assert result.artifact is not None
        assert result.artifact.sha256 == expected_sha256
        assert result.telemetry.telemetry_status == "VERIFIED_MEASURED"
        assert len(result.events) > 0
        assert len(streamed_events) == len(result.events)

        evidence = {
            "runtime_id": result.runtime_id,
            "invocation_id": result.invocation_id,
            "correlation_id": result.correlation_id,
            "status": result.status,
            "actors_executed": result.actors_executed,
            "orchestrator_session_id": result.orchestrator_session_id,
            "worker_session_id": result.worker_session_id,
            "provider_conversation_ids": result.provider_conversation_ids,
            "observed_tool_names": result.observed_tool_names,
            "qa_status": result.qa_verification.status,
            "qa_expected_sha256": expected_sha256,
            "qa_actual_sha256": result.qa_verification.actual_sha256,
            "qa_source_integrity_verified": result.qa_verification.source_integrity_verified,
            "telemetry": result.telemetry.model_dump(),
            "event_count": len(result.events),
            "duration_ms": result.duration_ms,
        }
        logger.info("P03_03_LIVE_EVIDENCE %s", json.dumps(evidence, sort_keys=True))
