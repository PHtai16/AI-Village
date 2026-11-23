"""Live integration test for P02.01 Antigravity CLI handshake."""

import os
from pathlib import Path
import tempfile

import pytest

from engines.antigravity import AntigravityAdapter, AntigravityHandshakeRequest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_LIVE = os.environ.get("AI_VILLAGE_RUN_LIVE", "").strip() == "1"


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Live CLI integration test runs only when AI_VILLAGE_RUN_LIVE=1.",
)
def test_live_antigravity_cli_handshake() -> None:
    """Execute a real non-interactive handshake through the Antigravity CLI adapter."""
    adapter = AntigravityAdapter()
    discovery = adapter.discover()
    assert discovery.found is True, f"Antigravity CLI not found: {discovery.error}"

    caps = adapter.probe()
    assert caps.version is not None
    assert caps.supports_print_mode is True
    assert caps.supports_json_output is True

    workspace_root = REPO_ROOT / "workspace"
    workspace_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(workspace_root), prefix="p02_01_live_") as tmp:
        request = AntigravityHandshakeRequest(
            prompt="Reply with exact text: AI_VILLAGE_HANDSHAKE_OK",
            workspace_path=tmp,
            timeout_seconds=60,
        )
        result = adapter.handshake(request)

    assert result.status == "SUCCEEDED", f"Handshake failed: {result.error}"
    assert result.exit_code == 0
    assert result.response_text is not None
    assert "AI_VILLAGE_HANDSHAKE_OK" in result.response_text
    assert result.conversation_id is not None
    assert result.duration_ms > 0
