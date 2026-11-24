"""Live integration tests for P02.04 Antigravity real session isolation."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import time
import uuid

import pytest

from engines.antigravity import (
    AntigravityAdapter,
    AntigravitySessionConfig,
    AntigravitySessionEvent,
    AntigravitySessionManager,
    compute_sha256_bytes,
    compute_sha256_file,
    snapshot_file_hashes,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_LIVE = os.environ.get("AI_VILLAGE_RUN_LIVE", "").strip() == "1"


def _create_live_session_root(prefix: str) -> tuple[Path, Path, Path, Path]:
    """Create a non-temp session root under ``workspace/p02_04_sessions/``."""
    workspace_root = REPO_ROOT / "workspace"
    workspace_root.mkdir(exist_ok=True)
    sessions_base = workspace_root / "p02_04_sessions"
    sessions_base.mkdir(exist_ok=True)
    run_root = sessions_base / f"{prefix}_{uuid.uuid4().hex[:8]}"
    run_root.mkdir(parents=True, exist_ok=True)
    ws_a = run_root / "session_A"
    ws_b = run_root / "session_B"
    ws_a.mkdir()
    ws_b.mkdir()
    return sessions_base, run_root, ws_a, ws_b


def _cleanup_live_root(sessions_base: Path, run_root: Path) -> None:
    """Safely remove temporary run fixture directories without touching other files."""
    if run_root.exists():
        time.sleep(0.5)
        shutil.rmtree(run_root, ignore_errors=True)
    if sessions_base.exists():
        try:
            if not any(sessions_base.iterdir()):
                sessions_base.rmdir()
        except OSError:
            pass


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Live CLI session isolation tests run only when AI_VILLAGE_RUN_LIVE=1.",
)
def test_live_session_context_isolation_and_continuation() -> None:
    """Verify session A context never leaks to fresh session B, while A can resume."""
    sessions_base, run_root, ws_a, ws_b = _create_live_session_root("ctx")
    marker_a = f"A_MARKER_{uuid.uuid4().hex[:10]}"
    manager = AntigravitySessionManager(adapter=AntigravityAdapter())

    try:
        manager.create_session(
            AntigravitySessionConfig(
                session_id="live_ctx_A",
                workspace_path=str(ws_a),
                allowed_root_path=str(run_root),
                timeout_seconds=60,
            )
        )
        manager.create_session(
            AntigravitySessionConfig(
                session_id="live_ctx_B",
                workspace_path=str(ws_b),
                allowed_root_path=str(run_root),
                timeout_seconds=60,
            )
        )

        turn_a1 = manager.run_turn(
            "live_ctx_A",
            f"Remember this exact token for later in this conversation: {marker_a}. Reply only: ACK_A",
        )
        turn_b1 = manager.run_turn(
            "live_ctx_B",
            "Do you have a secret token starting with A_MARKER_ from an earlier independent "
            "conversation? If not, reply with exactly: NO_TOKEN",
        )

        assert turn_a1.stream_result.status == "SUCCEEDED", f"Turn A1 failed: {turn_a1.stream_result.error}"
        assert "ACK_A" in (turn_a1.stream_result.response_text or "")
        assert turn_a1.provider_conversation_id

        assert turn_b1.stream_result.status == "SUCCEEDED", f"Turn B1 failed: {turn_b1.stream_result.error}"
        assert turn_b1.provider_conversation_id
        assert turn_b1.provider_conversation_id != turn_a1.provider_conversation_id
        assert turn_b1.isolation_error is None
        assert marker_a not in (turn_b1.stream_result.response_text or "")
        assert marker_a not in json.dumps([e.model_dump(mode="json") for e in turn_b1.session_events])

        # Verify explicit --conversation continuation works for A without affecting B.
        turn_a2 = manager.run_turn(
            "live_ctx_A",
            "Repeat the exact A_MARKER_ token you were asked to remember earlier.",
            resume_conversation=True,
        )
        assert turn_a2.stream_result.status == "SUCCEEDED", f"Turn A2 resume failed: {turn_a2.stream_result.error}"
        assert turn_a2.resumed_conversation is True
        assert turn_a2.provider_conversation_id == turn_a1.provider_conversation_id
        assert marker_a in (turn_a2.stream_result.response_text or "")

        manager.close_session("live_ctx_A", cleanup_workspace=True)
        manager.close_session("live_ctx_B", cleanup_workspace=True)
    finally:
        _cleanup_live_root(sessions_base, run_root)


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Live CLI session isolation tests run only when AI_VILLAGE_RUN_LIVE=1.",
)
def test_live_session_workspace_and_cross_access_isolation() -> None:
    """Verify A/B workspace isolation, independent SHA-256s, and blocked cross-access."""
    sessions_base, run_root, ws_a, ws_b = _create_live_session_root("fs")
    a_marker = f"PRIV_A_{uuid.uuid4().hex[:10]}"
    b_marker = f"PRIV_B_{uuid.uuid4().hex[:10]}"
    sentinel_marker = f"SENTINEL_{uuid.uuid4().hex[:10]}"

    (ws_a / "package.json").write_text(
        json.dumps({"name": "ai-village-p02-04-ws-a", "version": "0.0.1", "private": True}, indent=2) + "\n",
        encoding="utf-8",
    )
    (ws_a / "a_private.txt").write_text(f"a_marker={a_marker}\n", encoding="utf-8")
    sentinel_path = ws_a / "a_sentinel.txt"
    sentinel_path.write_text(f"SENTINEL_DATA={sentinel_marker}\n", encoding="utf-8")

    (ws_b / "package.json").write_text(
        json.dumps({"name": "ai-village-p02-04-ws-b", "version": "0.0.1", "private": True}, indent=2) + "\n",
        encoding="utf-8",
    )
    (ws_b / "b_private.txt").write_text(f"b_marker={b_marker}\n", encoding="utf-8")

    hashes_a = snapshot_file_hashes(ws_a, ["package.json", "a_private.txt", "a_sentinel.txt"])
    hashes_b = snapshot_file_hashes(ws_b, ["package.json", "b_private.txt"])
    sentinel_sha_before = compute_sha256_file(sentinel_path)

    expected_a = f"name=ai-village-p02-04-ws-a\na_marker={a_marker}\n"
    expected_b = f"name=ai-village-p02-04-ws-b\nb_marker={b_marker}\n"
    expected_sha_a = compute_sha256_bytes(expected_a.encode("utf-8"))
    expected_sha_b = compute_sha256_bytes(expected_b.encode("utf-8"))

    manager = AntigravitySessionManager(adapter=AntigravityAdapter())

    try:
        manager.create_session(
            AntigravitySessionConfig(
                session_id="live_fs_A",
                workspace_path=str(ws_a),
                allowed_root_path=str(run_root),
            )
        )
        manager.create_session(
            AntigravitySessionConfig(
                session_id="live_fs_B",
                workspace_path=str(ws_b),
                allowed_root_path=str(run_root),
            )
        )

        # Prompts deliberately do NOT contain the random markers stored in files.
        turn_a = manager.run_turn(
            "live_fs_A",
            "Read package.json and a_private.txt in the current workspace without modifying them. "
            "Do not run shell commands. Create proof_A.txt with exactly two UTF-8 LF lines:\n"
            "name=<package_json_name>\n"
            "a_marker=<value_after_equals_in_a_private>\n"
            "Then read proof_A.txt back to confirm.",
        )
        turn_b = manager.run_turn(
            "live_fs_B",
            "Read package.json and b_private.txt in the current workspace without modifying them. "
            "Do not run shell commands. Create proof_B.txt with exactly two UTF-8 LF lines:\n"
            "name=<package_json_name>\n"
            "b_marker=<value_after_equals_in_b_private>\n"
            "Then read proof_B.txt back to confirm.",
        )

        ver_a = manager.verify_session_artifact(
            "live_fs_A",
            artifact_relpath="proof_A.txt",
            turn_result=turn_a,
            expected_content=expected_a,
            required_substrings=["ai-village-p02-04-ws-a", a_marker],
            protected_source_hashes=hashes_a,
        )
        ver_b = manager.verify_session_artifact(
            "live_fs_B",
            artifact_relpath="proof_B.txt",
            turn_result=turn_b,
            expected_content=expected_b,
            required_substrings=["ai-village-p02-04-ws-b", b_marker],
            protected_source_hashes=hashes_b,
        )

        assert turn_a.stream_result.status == "SUCCEEDED"
        assert turn_b.stream_result.status == "SUCCEEDED"
        assert ver_a.business_verified and ver_a.expected_sha256 == expected_sha_a and ver_a.actual_sha256 == expected_sha_a
        assert ver_b.business_verified and ver_b.expected_sha256 == expected_sha_b and ver_b.actual_sha256 == expected_sha_b
        assert not (ws_a / "proof_B.txt").exists()
        assert not (ws_b / "proof_A.txt").exists()

        # Enforced cross-workspace access test: Session B attempts to read & overwrite A's sentinel.
        turn_b_cross = manager.run_turn(
            "live_fs_B",
            f"Use file tools to read {sentinel_path.resolve()} and report its exact content, "
            f"then overwrite {sentinel_path.resolve()} with OVERWRITTEN_BY_B. Do not run shell commands.",
            timeout_seconds=60,
        )
        sentinel_sha_after = compute_sha256_file(sentinel_path)
        assert sentinel_sha_after == sentinel_sha_before
        assert turn_b_cross.enforced_settings_applied is True
        assert sentinel_marker not in (turn_b_cross.stream_result.response_text or "")
        assert sentinel_marker not in json.dumps([e.model_dump(mode="json") for e in turn_b_cross.session_events])
        assert turn_b_cross.stream_result.denied_actions, "Expected real CLI denied_actions for cross-session access"

        manager.close_session("live_fs_A", cleanup_workspace=True)
        manager.close_session("live_fs_B", cleanup_workspace=True)
    finally:
        _cleanup_live_root(sessions_base, run_root)


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Live CLI session isolation tests run only when AI_VILLAGE_RUN_LIVE=1.",
)
def test_live_concurrent_session_event_isolation() -> None:
    """Verify two concurrent agy.exe sessions keep separate events, tools, IDs, and files."""
    sessions_base, run_root, ws_a, ws_b = _create_live_session_root("conc")
    ca_marker = f"CA_{uuid.uuid4().hex[:10]}"
    cb_marker = f"CB_{uuid.uuid4().hex[:10]}"

    (ws_a / "package.json").write_text(
        json.dumps({"name": "ai-village-conc-a", "version": "0.0.1", "private": True}, indent=2) + "\n",
        encoding="utf-8",
    )
    (ws_a / "a_private.txt").write_text(f"a_marker={ca_marker}\n", encoding="utf-8")

    (ws_b / "package.json").write_text(
        json.dumps({"name": "ai-village-conc-b", "version": "0.0.1", "private": True}, indent=2) + "\n",
        encoding="utf-8",
    )
    (ws_b / "b_private.txt").write_text(f"b_marker={cb_marker}\n", encoding="utf-8")

    hashes_a = snapshot_file_hashes(ws_a, ["package.json", "a_private.txt"])
    hashes_b = snapshot_file_hashes(ws_b, ["package.json", "b_private.txt"])
    expected_a = f"name=ai-village-conc-a\na_marker={ca_marker}\n"
    expected_b = f"name=ai-village-conc-b\nb_marker={cb_marker}\n"

    manager = AntigravitySessionManager(adapter=AntigravityAdapter())
    cb_events_a: list[AntigravitySessionEvent] = []
    cb_events_b: list[AntigravitySessionEvent] = []

    try:
        manager.create_session(
            AntigravitySessionConfig(
                session_id="live_conc_A",
                workspace_path=str(ws_a),
                allowed_root_path=str(run_root),
            )
        )
        manager.create_session(
            AntigravitySessionConfig(
                session_id="live_conc_B",
                workspace_path=str(ws_b),
                allowed_root_path=str(run_root),
            )
        )

        prompt_a = (
            "Read package.json and a_private.txt without modifying them. Do not run shell commands. "
            "Create proof_A.txt with two UTF-8 LF lines:\n"
            "name=<package_json_name>\n"
            "a_marker=<value_after_equals_in_a_private>\n"
            "Then read proof_A.txt back."
        )
        prompt_b = (
            "Read package.json and b_private.txt without modifying them. Do not run shell commands. "
            "Create proof_B.txt with two UTF-8 LF lines:\n"
            "name=<package_json_name>\n"
            "b_marker=<value_after_equals_in_b_private>\n"
            "Then read proof_B.txt back."
        )

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_a = pool.submit(manager.run_turn, "live_conc_A", prompt_a, on_event=cb_events_a.append)
            fut_b = pool.submit(manager.run_turn, "live_conc_B", prompt_b, on_event=cb_events_b.append)
            turn_a = fut_a.result()
            turn_b = fut_b.result()

        assert turn_a.stream_result.status == "SUCCEEDED", f"Concurrent A failed: {turn_a.stream_result.error}"
        assert turn_b.stream_result.status == "SUCCEEDED", f"Concurrent B failed: {turn_b.stream_result.error}"
        assert turn_a.isolation_error is None and turn_b.isolation_error is None
        assert turn_a.provider_conversation_id and turn_b.provider_conversation_id
        assert turn_a.provider_conversation_id != turn_b.provider_conversation_id
        assert turn_a.stream_result.completion_event_received is True
        assert turn_b.stream_result.completion_event_received is True
        assert "read_file" in turn_a.stream_result.observed_tool_names
        assert "write_file" in turn_a.stream_result.observed_tool_names
        assert "read_file" in turn_b.stream_result.observed_tool_names
        assert "write_file" in turn_b.stream_result.observed_tool_names

        assert turn_a.session_events == cb_events_a
        assert turn_b.session_events == cb_events_b
        assert [e.event.sequence_number for e in cb_events_a] == list(range(1, len(cb_events_a) + 1))
        assert [e.event.sequence_number for e in cb_events_b] == list(range(1, len(cb_events_b) + 1))
        assert all(e.session_id == "live_conc_A" and e.session_id_source == "adapter_session_context" for e in cb_events_a)
        assert all(e.session_id == "live_conc_B" and e.session_id_source == "adapter_session_context" for e in cb_events_b)

        dump_a = json.dumps([e.model_dump(mode="json") for e in cb_events_a])
        dump_b = json.dumps([e.model_dump(mode="json") for e in cb_events_b])
        assert turn_b.provider_conversation_id not in dump_a
        assert turn_a.provider_conversation_id not in dump_b
        assert cb_marker not in dump_a
        assert ca_marker not in dump_b

        ver_a = manager.verify_session_artifact(
            "live_conc_A",
            artifact_relpath="proof_A.txt",
            turn_result=turn_a,
            expected_content=expected_a,
            required_substrings=["ai-village-conc-a", ca_marker],
            protected_source_hashes=hashes_a,
        )
        ver_b = manager.verify_session_artifact(
            "live_conc_B",
            artifact_relpath="proof_B.txt",
            turn_result=turn_b,
            expected_content=expected_b,
            required_substrings=["ai-village-conc-b", cb_marker],
            protected_source_hashes=hashes_b,
        )
        assert ver_a.business_verified and ver_a.hash_verified
        assert ver_b.business_verified and ver_b.hash_verified

        manager.close_session("live_conc_A", cleanup_workspace=True)
        manager.close_session("live_conc_B", cleanup_workspace=True)
    finally:
        _cleanup_live_root(sessions_base, run_root)
