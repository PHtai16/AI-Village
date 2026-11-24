"""Bounded opt-in live benchmark tests comparing CrewAI Flow and Crew with Antigravity CLI."""

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

pytestmark = [pytest.mark.live, pytest.mark.crewai]
pytest.importorskip("crewai")

from engines import AntigravityEngineAdapter, EngineRegistry, compute_sha256_bytes  # noqa: E402
from tests.benchmarks.p03_02.crewai_crew_prototype import run_crewai_crew_benchmark  # noqa: E402
from tests.benchmarks.p03_02.crewai_flow_prototype import run_crewai_flow_benchmark  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_LIVE = os.environ.get("AI_VILLAGE_RUN_LIVE", "").strip() == "1"
logger = logging.getLogger(__name__)


@contextmanager
def _isolated_live_dirs(prefix: str) -> Iterator[tuple[Path, Path, Path, Path, Path]]:
    """Create isolated live workspaces and CrewAI local state dirs inside ``workspace/``."""
    workspace_root = REPO_ROOT / "workspace"
    workspace_root.mkdir(exist_ok=True)
    run_root = Path(tempfile.mkdtemp(dir=str(workspace_root), prefix=prefix))
    master_ws = run_root / "master_ws"
    worker_ws = run_root / "worker_ws"
    flow_db = run_root / "flow_state" / "p03_02_flow.db"
    storage_dir = run_root / "crewai_storage"
    master_ws.mkdir(parents=True)
    worker_ws.mkdir(parents=True)
    try:
        yield run_root, master_ws, worker_ws, flow_db, storage_dir
    finally:
        for _ in range(10):
            if not run_root.exists():
                break
            shutil.rmtree(run_root, ignore_errors=True)
            if run_root.exists():
                time.sleep(0.3)


def _setup_fixture(master_ws: Path, worker_ws: Path) -> tuple[str, str, str]:
    """Create identical P03.01-parity package.json fixture without leaking verification_marker."""
    marker = f"mk-{uuid.uuid4().hex[:12]}"
    pkg = {
        "name": "ai-village-p03-02-benchmark",
        "version": "0.1.0",
        "private": True,
        "verification_marker": marker,
    }
    (worker_ws / "package.json").write_text(json.dumps(pkg, indent=2) + "\n", encoding="utf-8")
    (master_ws / "BRIEF.txt").write_text(
        "Worker workspace contains package.json; produce proof.txt there.\n",
        encoding="utf-8",
    )
    expected_proof = f"name={pkg['name']}\nverification_marker={marker}\n"
    expected_sha = compute_sha256_bytes(expected_proof.encode("utf-8"))
    user_request = (
        "Delegate to an authorized worker to read 'package.json' in its workspace without "
        "modifying 'package.json', extract the exact values of 'name' and "
        "'verification_marker', and create 'proof.txt' in the same workspace containing "
        "exact two UTF-8 LF-terminated lines:\n"
        "name=<name>\n"
        "verification_marker=<verification_marker>"
    )
    return user_request, expected_proof, expected_sha


@pytest.mark.skipif(
    not RUN_LIVE,
    reason="P03.02 CrewAI live benchmarks run only when AI_VILLAGE_RUN_LIVE=1.",
)
def test_live_p03_02_crewai_flow_benchmark() -> None:
    """Option B live run: CrewAI Flow -> EngineRegistry -> agy.exe -> independent QA SHA-256."""
    with _isolated_live_dirs("p03_02_flow_") as (run_root, master_ws, worker_ws, flow_db, storage_dir):
        user_request, expected_proof, expected_sha = _setup_fixture(master_ws, worker_ws)
        registry = EngineRegistry()
        registry.register_adapter(AntigravityEngineAdapter())

        outcome, flow = run_crewai_flow_benchmark(
            registry=registry,
            user_request=user_request,
            master_workspace_dir=master_ws,
            worker_workspace_dir=worker_ws,
            allowed_root=run_root,
            persistence_db_path=flow_db,
            crewai_storage_dir=storage_dir,
            expected_artifact_content=expected_proof,
            required_artifact_substrings=("name=ai-village-p03-02-benchmark",),
            provider_id="antigravity",
            timeout_seconds=120,
        )
        assert outcome.status == "DELIVERED", (
            f"Option B live benchmark failed: status={outcome.status}, error={outcome.error}"
        )
        assert outcome.qa_verification is not None
        assert outcome.qa_verification.status == "VERIFIED_PASS"
        assert outcome.qa_verification.actual_sha256 == expected_sha
        assert outcome.qa_verification.source_integrity_verified is True
        assert outcome.timing.provider_call_count == 3
        assert outcome.timing.extra_reasoning_calls == 0
        assert "read_file" in outcome.observed_tool_names
        assert "write_file" in outcome.observed_tool_names
        assert len(set(outcome.provider_conversation_ids)) == len(outcome.provider_conversation_ids)
        assert flow_db.is_file()
        assert flow.state.status == "DELIVERED"

        summary_payload = {
            "option": outcome.option,
            "status": outcome.status,
            "correlation_id": outcome.correlation_id,
            "master_session_id": outcome.master_session_id,
            "worker_session_id": outcome.worker_session_id,
            "provider_conversation_ids": outcome.provider_conversation_ids,
            "observed_tool_names": outcome.observed_tool_names,
            "qa_status": outcome.qa_verification.status if outcome.qa_verification else None,
            "qa_actual_sha256": outcome.qa_verification.actual_sha256 if outcome.qa_verification else None,
            "qa_expected_sha256": expected_sha,
            "qa_source_integrity_verified": outcome.qa_verification.source_integrity_verified if outcome.qa_verification else None,
            "timing": outcome.timing.model_dump(),
            "notes": outcome.notes,
        }
        logger.info("P03_02_LIVE_FLOW_EVIDENCE %s", json.dumps(summary_payload, sort_keys=True))


@pytest.mark.skipif(
    not RUN_LIVE,
    reason="P03.02 CrewAI live benchmarks run only when AI_VILLAGE_RUN_LIVE=1.",
)
def test_live_p03_02_crewai_crew_benchmark() -> None:
    """Option C live run: CrewAI Crew -> BaseLLM bridge -> EngineRegistry -> agy.exe -> QA."""
    with _isolated_live_dirs("p03_02_crew_") as (run_root, master_ws, worker_ws, _flow_db, storage_dir):
        user_request, expected_proof, expected_sha = _setup_fixture(master_ws, worker_ws)
        registry = EngineRegistry()
        registry.register_adapter(AntigravityEngineAdapter())

        outcome = run_crewai_crew_benchmark(
            registry=registry,
            user_request=user_request,
            master_workspace_dir=master_ws,
            worker_workspace_dir=worker_ws,
            allowed_root=run_root,
            crewai_storage_dir=storage_dir,
            expected_artifact_content=expected_proof,
            required_artifact_substrings=("name=ai-village-p03-02-benchmark",),
            provider_id="antigravity",
            timeout_seconds=120,
            max_calls_per_stage=1,
        )
        summary_payload = {
            "option": outcome.option,
            "status": outcome.status,
            "correlation_id": outcome.correlation_id,
            "master_session_id": outcome.master_session_id,
            "worker_session_id": outcome.worker_session_id,
            "provider_conversation_ids": outcome.provider_conversation_ids,
            "observed_tool_names": outcome.observed_tool_names,
            "qa_status": outcome.qa_verification.status if outcome.qa_verification else None,
            "qa_actual_sha256": outcome.qa_verification.actual_sha256 if outcome.qa_verification else None,
            "qa_expected_sha256": expected_sha,
            "qa_source_integrity_verified": outcome.qa_verification.source_integrity_verified if outcome.qa_verification else None,
            "framework_token_usage": outcome.framework_token_usage,
            "timing": outcome.timing.model_dump(),
            "notes": outcome.notes,
            "error": outcome.error.model_dump() if outcome.error else None,
        }
        logger.info("P03_02_LIVE_CREW_EVIDENCE %s", json.dumps(summary_payload, sort_keys=True))

        assert outcome.timing.provider_call_count <= 3
        assert outcome.timing.provider_call_count >= 1
        assert all(res.provider_id == "antigravity" for res in outcome.engine_results)
        assert outcome.status == "DELIVERED", (
            f"Option C live benchmark failed: status={outcome.status}, error={outcome.error}"
        )
        assert outcome.qa_verification is not None
        assert outcome.qa_verification.status == "VERIFIED_PASS"
        assert outcome.qa_verification.actual_sha256 == expected_sha
        assert outcome.notes.get("crew_tool_events") == 0
