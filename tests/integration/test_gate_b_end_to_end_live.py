"""Opt-in Gate B end-to-end live integration tests through the Universal Engine Contract."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Iterator
import uuid

import pytest

from engines import (
    AntigravityEngineAdapter,
    EngineConnectionRef,
    EngineEvent,
    EngineRegistry,
    EngineRequest,
    EngineResult,
    GoogleGeminiAdapter,
    compute_sha256_bytes,
    snapshot_file_hashes,
    verify_engine_artifact,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_LIVE = os.environ.get("AI_VILLAGE_RUN_LIVE", "").strip() == "1"


@contextmanager
def _isolated_workspace_dir(prefix: str) -> Iterator[Path]:
    """Create a temporary workspace inside ``workspace/`` and clean up with Windows retry."""
    workspace_root = REPO_ROOT / "workspace"
    workspace_root.mkdir(exist_ok=True)
    tmp_path = Path(tempfile.mkdtemp(dir=str(workspace_root), prefix=prefix))
    try:
        yield tmp_path
    finally:
        for _ in range(10):
            if not tmp_path.exists():
                break
            shutil.rmtree(tmp_path, ignore_errors=True)
            if tmp_path.exists():
                time.sleep(0.3)


def _build_gate_b_registry(workspace_dir: Path) -> EngineRegistry:
    """Create an ``EngineRegistry`` hosting both Antigravity primary and optional Gemini API."""
    registry = EngineRegistry()
    registry.register_adapter(
        AntigravityEngineAdapter(default_workspace_path=str(workspace_dir))
    )
    registry.register_adapter(GoogleGeminiAdapter())
    registry.register_connection(
        EngineConnectionRef(
            connection_id="gemini_primary",
            provider_id="google_gemini",
            adapter_id="gemini_api",
            project_ref="google_project_a",
            credential_ref="env:GEMINI_API_KEY",
            default_model_id="gemini-2.5-flash",
            enabled=True,
        )
    )
    return registry


def _execute_neutral_request(
    registry: EngineRegistry,
    request: EngineRequest,
    *,
    on_event: list[EngineEvent],
) -> EngineResult:
    """Application entrypoint dispatching solely through the provider-neutral contract."""
    return registry.execute(request, on_event=on_event.append)


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Gate B end-to-end live integration test runs only when AI_VILLAGE_RUN_LIVE=1.",
)
def test_live_gate_b_end_to_end_antigravity_through_contract() -> None:
    """Prove Application -> EngineRegistry -> AntigravityEngineAdapter -> agy.exe -> SHA-256."""
    with _isolated_workspace_dir("gate_b_e2e_") as ws_path:
        marker = f"mk-{uuid.uuid4().hex[:12]}"
        pkg_payload = {
            "name": "ai-village-gate-b-fixture",
            "version": "0.0.1",
            "private": True,
            "verification_marker": marker,
        }
        (ws_path / "package.json").write_text(
            json.dumps(pkg_payload, indent=2) + "\n",
            encoding="utf-8",
        )
        protected_hashes = snapshot_file_hashes(ws_path, ["package.json"])

        expected_proof = (
            f"name={pkg_payload['name']}\n"
            f"verification_marker={marker}\n"
        )
        expected_sha256 = compute_sha256_bytes(expected_proof.encode("utf-8"))

        # The hidden verification_marker is NEVER placed in the prompt.
        prompt = (
            "Read package.json in the current workspace directory without modifying it. "
            "Extract the exact values of 'name' and 'verification_marker'. "
            "Do not run any shell or terminal commands; use only file read and file write tools. "
            "Create a new file named proof.txt in the current workspace directory with "
            "exactly two UTF-8 lines ending with LF newline:\n"
            "name=<name>\n"
            "verification_marker=<verification_marker>\n"
            "Then read proof.txt back to verify its content and summarize."
        )

        registry = _build_gate_b_registry(ws_path)
        streamed_events: list[EngineEvent] = []
        request = EngineRequest(
            provider_id="antigravity",
            prompt=prompt,
            workspace_path=str(ws_path),
            timeout_seconds=90,
            capabilities_required=[
                "streaming",
                "native_tool_execution",
                "workspace_access",
            ],
            execution_mode="accept-edits",
        )
        engine_result = _execute_neutral_request(
            registry,
            request,
            on_event=streamed_events,
        )

        assert engine_result.status == "SUCCEEDED", f"Gate B execution failed: {engine_result.error}"
        assert engine_result.business_verification_status == "UNVERIFIED"
        assert engine_result.provider_id == "antigravity"
        assert engine_result.adapter_id == "antigravity_cli"
        assert engine_result.platform_kind == "agent_execution_platform"
        assert engine_result.duration_ms > 0
        assert engine_result.telemetry.telemetry_status == "VERIFIED_MEASURED"
        assert engine_result.telemetry.telemetry_source == "provider_cli"

        tool_events = [
            ev
            for ev in streamed_events
            if ev.event_type == "tool_execution_observed" and ev.source == "provider"
        ]
        assert tool_events, "Expected real incremental provider tool_execution_observed events."
        assert "read_file" in engine_result.observed_tool_names
        assert "write_file" in engine_result.observed_tool_names

        verification = verify_engine_artifact(
            workspace_dir=ws_path,
            artifact_relpath="proof.txt",
            engine_result=engine_result,
            expected_content=expected_proof,
            required_substrings=[f"name={pkg_payload['name']}", f"verification_marker={marker}"],
            protected_source_hashes=protected_hashes,
        )
        assert verification.status == "VERIFIED_PASS", (
            f"Independent verification failed: {verification.failure_reasons}"
        )
        assert verification.business_verified is True
        assert verification.content_verified is True
        assert verification.hash_verified is True
        assert verification.source_integrity_verified is True
        assert verification.actual_sha256 == expected_sha256


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Gate B end-to-end live integration test runs only when AI_VILLAGE_RUN_LIVE=1.",
)
def test_live_gate_b_gemini_tool_rejection_and_tamper_detection() -> None:
    """Verify Model API rejects native file tools fail-closed and tamper detection fails QA."""
    with _isolated_workspace_dir("gate_b_guard_") as ws_path:
        (ws_path / "package.json").write_text(
            '{"name":"ai-village-gate-b-fixture","verification_marker":"mk-guard"}\n',
            encoding="utf-8",
        )
        protected_hashes = snapshot_file_hashes(ws_path, ["package.json"])
        registry = _build_gate_b_registry(ws_path)
        events: list[EngineEvent] = []

        gemini_res = _execute_neutral_request(
            registry,
            EngineRequest(
                connection_id="gemini_primary",
                prompt="Read package.json and write proof.txt",
                workspace_path=str(ws_path),
                capabilities_required=["native_tool_execution", "workspace_access"],
                execution_mode="accept-edits",
            ),
            on_event=events,
        )
        assert gemini_res.status == "BLOCKED"
        assert gemini_res.provider_id == "google_gemini"
        assert gemini_res.error is not None
        assert gemini_res.error.error_code == "TOOL_UNSUPPORTED_BY_PROVIDER"
        assert gemini_res.observed_tool_names == []
        assert not (ws_path / "proof.txt").exists()

        # Verify independent QA detects wrong file content / SHA-256 even on SUCCEEDED result.
        (ws_path / "proof.txt").write_text(
            "name=ai-village-gate-b-fixture\nverification_marker=WRONG\n",
            encoding="utf-8",
        )
        synthetic_succeeded = EngineResult(
            request_id="req-tamper-check",
            provider_id="antigravity",
            adapter_id="antigravity_cli",
            platform_kind="agent_execution_platform",
            status="SUCCEEDED",
            observed_tool_names=["read_file", "write_file"],
            started_at=gemini_res.started_at,
            finished_at=gemini_res.finished_at,
            duration_ms=10,
            telemetry=gemini_res.telemetry,
        )
        tamper_qa = verify_engine_artifact(
            workspace_dir=ws_path,
            artifact_relpath="proof.txt",
            engine_result=synthetic_succeeded,
            expected_content="name=ai-village-gate-b-fixture\nverification_marker=mk-guard\n",
            protected_source_hashes=protected_hashes,
        )
        assert tamper_qa.status == "VERIFIED_FAIL"
        assert tamper_qa.content_verified is False
        assert tamper_qa.hash_verified is False
        assert tamper_qa.business_verified is False
