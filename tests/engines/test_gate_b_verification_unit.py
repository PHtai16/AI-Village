"""Unit tests for independent Gate B artifact verification through the engine contract."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from engines import (
    EngineConnectionRef,
    EngineRegistry,
    EngineRequest,
    EngineResult,
    EngineTelemetry,
    EnvCredentialResolver,
    GoogleGeminiAdapter,
    compute_sha256_bytes,
    snapshot_file_hashes,
    verify_engine_artifact,
)


@pytest.fixture
def succeeded_engine_result() -> EngineResult:
    """Return a clean technical SUCCEEDED result whose business QA status starts UNVERIFIED."""
    now = datetime.now(timezone.utc)
    return EngineResult(
        request_id="req-unit-gate-b",
        provider_id="antigravity",
        adapter_id="antigravity_cli",
        platform_kind="agent_execution_platform",
        status="SUCCEEDED",
        response_text="Created proof.txt",
        observed_tool_names=["read_file", "write_file"],
        started_at=now,
        finished_at=now,
        duration_ms=120,
        telemetry=EngineTelemetry(
            duration_ms=120,
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            telemetry_status="VERIFIED_MEASURED",
            telemetry_source="provider_cli",
        ),
    )


def test_verify_engine_artifact_happy_path(
    tmp_path: Path,
    succeeded_engine_result: EngineResult,
) -> None:
    """Verify exact artifact content, matching SHA-256, and untouched protected source pass."""
    pkg = tmp_path / "package.json"
    pkg.write_text('{"name":"demo","verification_marker":"mk-123"}\n', encoding="utf-8")
    source_hashes = snapshot_file_hashes(tmp_path, ["package.json"])

    expected = "name=demo\nverification_marker=mk-123\n"
    proof = tmp_path / "proof.txt"
    proof.write_bytes(expected.encode("utf-8"))

    res = verify_engine_artifact(
        workspace_dir=tmp_path,
        artifact_relpath="proof.txt",
        engine_result=succeeded_engine_result,
        expected_content=expected,
        required_substrings=["name=demo", "verification_marker=mk-123"],
        protected_source_hashes=source_hashes,
    )
    assert succeeded_engine_result.business_verification_status == "UNVERIFIED"
    assert res.status == "VERIFIED_PASS"
    assert res.engine_business_status_before_qa == "UNVERIFIED"
    assert res.business_verified is True
    assert res.content_verified is True
    assert res.hash_verified is True
    assert res.source_integrity_verified is True
    assert res.actual_sha256 == compute_sha256_bytes(expected.encode("utf-8"))
    assert res.failure_reasons == []


def test_verify_engine_artifact_detects_wrong_content_and_hash_even_when_engine_succeeded(
    tmp_path: Path,
    succeeded_engine_result: EngineResult,
) -> None:
    """Engine technical SUCCEEDED must turn into VERIFIED_FAIL when proof.txt is wrong."""
    pkg = tmp_path / "package.json"
    pkg.write_text('{"name":"demo","verification_marker":"mk-real"}\n', encoding="utf-8")
    source_hashes = snapshot_file_hashes(tmp_path, ["package.json"])

    expected = "name=demo\nverification_marker=mk-real\n"
    (tmp_path / "proof.txt").write_text("name=demo\nverification_marker=mk-fake\n", encoding="utf-8")

    res = verify_engine_artifact(
        workspace_dir=tmp_path,
        artifact_relpath="proof.txt",
        engine_result=succeeded_engine_result,
        expected_content=expected,
        protected_source_hashes=source_hashes,
    )
    assert res.status == "VERIFIED_FAIL"
    assert res.engine_succeeded is True
    assert res.content_verified is False
    assert res.hash_verified is False
    assert res.business_verified is False
    assert res.actual_sha256 != res.expected_sha256
    assert any("SHA-256 mismatch" in reason for reason in res.failure_reasons)


def test_verify_engine_artifact_rejects_missing_tampered_source_and_traversal(
    tmp_path: Path,
    succeeded_engine_result: EngineResult,
) -> None:
    """Detect missing output, modified protected source file, and path traversal attempts."""
    pkg = tmp_path / "package.json"
    pkg.write_text('{"name":"demo"}\n', encoding="utf-8")
    source_hashes = snapshot_file_hashes(tmp_path, ["package.json"])

    missing_res = verify_engine_artifact(
        workspace_dir=tmp_path,
        artifact_relpath="proof.txt",
        engine_result=succeeded_engine_result,
        expected_content="name=demo\n",
        protected_source_hashes=source_hashes,
    )
    assert missing_res.status == "VERIFIED_FAIL"
    assert missing_res.artifact_exists is False

    # Mutate protected source file and supply a matching proof file.
    (tmp_path / "proof.txt").write_text("name=demo\n", encoding="utf-8")
    pkg.write_text('{"name":"tampered"}\n', encoding="utf-8")
    tampered_src = verify_engine_artifact(
        workspace_dir=tmp_path,
        artifact_relpath="proof.txt",
        engine_result=succeeded_engine_result,
        expected_content="name=demo\n",
        protected_source_hashes=source_hashes,
    )
    assert tampered_src.status == "VERIFIED_FAIL"
    assert tampered_src.source_integrity_verified is False

    traversal = verify_engine_artifact(
        workspace_dir=tmp_path,
        artifact_relpath="../escape.txt",
        engine_result=succeeded_engine_result,
    )
    assert traversal.status == "VERIFIED_FAIL"
    assert traversal.within_workspace is False


def test_verify_engine_artifact_blocks_failed_and_model_api_results(tmp_path: Path) -> None:
    """Failed or BLOCKED EngineResult stays VERIFIED_FAIL even if proof.txt exists on disk."""
    (tmp_path / "proof.txt").write_text("stale content\n", encoding="utf-8")

    registry = EngineRegistry()
    registry.register_adapter(
        GoogleGeminiAdapter(
            credential_resolver=EnvCredentialResolver(environ={}),
        )
    )
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
    blocked_res = registry.execute(
        EngineRequest(
            connection_id="gemini_primary",
            prompt="Create proof.txt",
            workspace_path=str(tmp_path),
            capabilities_required=["native_tool_execution", "workspace_access"],
            execution_mode="accept-edits",
        )
    )
    assert blocked_res.status == "BLOCKED"
    assert blocked_res.error is not None
    assert blocked_res.error.error_code == "TOOL_UNSUPPORTED_BY_PROVIDER"

    qa_res = verify_engine_artifact(
        workspace_dir=tmp_path,
        artifact_relpath="proof.txt",
        engine_result=blocked_res,
        expected_content="stale content\n",
    )
    assert qa_res.status == "VERIFIED_FAIL"
    assert qa_res.engine_succeeded is False
    assert qa_res.business_verified is False
