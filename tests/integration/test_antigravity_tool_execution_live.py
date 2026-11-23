"""Live integration test for P02.02 Antigravity real tool execution proof."""

import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import uuid

import pytest

from engines.antigravity import (
    AntigravityAdapter,
    AntigravityToolExecutionRequest,
    compute_sha256_bytes,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_LIVE = os.environ.get("AI_VILLAGE_RUN_LIVE", "").strip() == "1"


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Live CLI tool execution test runs only when AI_VILLAGE_RUN_LIVE=1.",
)
def test_live_antigravity_tool_execution_and_sha256() -> None:
    """Verify Antigravity reads package.json, writes proof.txt, and passes independent SHA-256."""
    adapter = AntigravityAdapter()
    discovery = adapter.discover()
    assert discovery.found is True, f"Antigravity CLI not found: {discovery.error}"

    workspace_root = REPO_ROOT / "workspace"
    workspace_root.mkdir(exist_ok=True)
    marker = f"mk-{uuid.uuid4().hex[:12]}"
    pkg_payload = {
        "name": "ai-village-p02-02-fixture",
        "version": "0.0.1",
        "private": True,
        "verification_marker": marker,
    }
    expected_proof = (
        f"name={pkg_payload['name']}\n"
        f"verification_marker={marker}\n"
    )
    expected_sha256 = compute_sha256_bytes(expected_proof.encode("utf-8"))

    # Note: the generated verification_marker is NEVER placed inside the prompt.
    prompt = (
        "Read package.json in the current workspace directory without modifying it. "
        "Extract the exact values of 'name' and 'verification_marker'. "
        "Do not run any shell or terminal commands; use only file read and file write tools. "
        "Create a new file named proof.txt in the current workspace directory with "
        "exactly two UTF-8 lines ending with LF newline:\n"
        "name=<name>\n"
        "verification_marker=<verification_marker>\n"
        "Then read proof.txt back to confirm its content and summarize the result."
    )

    with tempfile.TemporaryDirectory(
        dir=str(workspace_root),
        prefix="p02_02_live_",
        ignore_cleanup_errors=True,
    ) as tmp:
        ws = Path(tmp)
        (ws / "package.json").write_text(
            json.dumps(pkg_payload, indent=2) + "\n",
            encoding="utf-8",
        )

        request = AntigravityToolExecutionRequest(
            prompt=prompt,
            workspace_path=str(ws),
            expected_artifact_relpath="proof.txt",
            source_files_to_protect=["package.json"],
            expected_content=expected_proof,
            required_substrings=[pkg_payload["name"], marker],
            timeout_seconds=90,
            require_sandbox=True,
            execution_mode="accept-edits",
        )
        result = adapter.execute_tool_task(request)

    if ws.exists():
        time.sleep(0.5)
        shutil.rmtree(ws, ignore_errors=True)

    assert result.status == "VERIFIED_PASS", f"Tool execution failed: {result.error} | {result.verification.failure_reasons}"
    assert result.verification.cli_success is True
    assert result.verification.artifact_exists is True
    assert result.verification.within_workspace is True
    assert result.verification.content_verified is True
    assert result.verification.hash_verified is True
    assert result.verification.source_integrity_verified is True
    assert result.verification.business_verified is True
    assert result.verification.expected_sha256 == expected_sha256
    assert result.verification.actual_sha256 == expected_sha256
    assert result.cli_result.exit_code == 0
    assert result.cli_result.duration_ms > 0
