"""Independent workspace artifact and SHA-256 verification for Universal Engine results."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from engines.antigravity.verification import (
    compute_sha256_bytes,
    compute_sha256_file,
    resolve_within_workspace,
    snapshot_file_hashes,
)
from engines.contract import EngineExecutionStatus, EngineResult

logger = logging.getLogger(__name__)


class EngineArtifactVerificationResult(BaseModel):
    """Independent QA filesystem verification outcome paired with an ``EngineResult``."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["VERIFIED_PASS", "VERIFIED_FAIL"]
    engine_execution_status: EngineExecutionStatus
    engine_business_status_before_qa: Literal["UNVERIFIED"] = "UNVERIFIED"
    artifact_relpath: str
    artifact_abs_path: str | None = None
    engine_succeeded: bool
    artifact_exists: bool
    within_workspace: bool
    content_verified: bool
    hash_verified: bool
    source_integrity_verified: bool
    business_verified: bool
    file_size_bytes: int | None = None
    expected_sha256: str | None = None
    actual_sha256: str | None = None
    failure_reasons: list[str] = Field(default_factory=list)


def verify_engine_artifact(
    *,
    workspace_dir: Path,
    artifact_relpath: str,
    engine_result: EngineResult,
    expected_content: str | None = None,
    required_substrings: Sequence[str] = (),
    protected_source_hashes: dict[str, str] | None = None,
) -> EngineArtifactVerificationResult:
    """Verify an artifact on disk independently of any model or CLI self-report.

    Args:
        workspace_dir: Root directory enclosing the isolated task workspace.
        artifact_relpath: Expected artifact path relative to ``workspace_dir``.
        engine_result: Result returned by executing a request through ``EngineRegistry``.
        expected_content: Optional exact UTF-8 text expected inside the artifact.
        required_substrings: Optional substrings required inside the artifact text.
        protected_source_hashes: Pre-execution SHA-256 hashes of source files that must stay intact.

    Returns:
        Structured ``EngineArtifactVerificationResult`` separating engine technical
        completion from independent disk verification.
    """
    failure_reasons: list[str] = []
    engine_succeeded = engine_result.status == "SUCCEEDED"
    if not engine_succeeded:
        failure_reasons.append(
            f"Engine execution did not succeed (status={engine_result.status})."
        )

    expected_sha256: str | None = (
        compute_sha256_bytes(expected_content.encode("utf-8"))
        if expected_content is not None
        else None
    )

    source_integrity_verified = True
    for rel_path, before_sha in (protected_source_hashes or {}).items():
        try:
            src_path = resolve_within_workspace(workspace_dir, rel_path)
        except ValueError as exc:
            source_integrity_verified = False
            failure_reasons.append(str(exc))
            continue
        if not src_path.is_file():
            source_integrity_verified = False
            failure_reasons.append(f"Protected source file missing after execution: {rel_path}")
            continue
        try:
            after_sha = compute_sha256_file(src_path)
        except OSError as exc:
            source_integrity_verified = False
            failure_reasons.append(f"Unable to hash protected source '{rel_path}': {exc}")
            continue
        if after_sha != before_sha:
            source_integrity_verified = False
            failure_reasons.append(
                f"Protected source '{rel_path}' modified (before={before_sha}, after={after_sha})."
            )

    try:
        artifact_path = resolve_within_workspace(workspace_dir, artifact_relpath)
        within_workspace = True
    except ValueError as exc:
        logger.warning("Artifact boundary check failed for %s: %s", artifact_relpath, exc)
        failure_reasons.append(str(exc))
        return EngineArtifactVerificationResult(
            status="VERIFIED_FAIL",
            engine_execution_status=engine_result.status,
            engine_business_status_before_qa=engine_result.business_verification_status,
            artifact_relpath=artifact_relpath,
            artifact_abs_path=None,
            engine_succeeded=engine_succeeded,
            artifact_exists=False,
            within_workspace=False,
            content_verified=False,
            hash_verified=False,
            source_integrity_verified=source_integrity_verified,
            business_verified=False,
            expected_sha256=expected_sha256,
            failure_reasons=failure_reasons,
        )

    if not artifact_path.is_file():
        failure_reasons.append(f"Expected artifact not found: {artifact_relpath}")
        return EngineArtifactVerificationResult(
            status="VERIFIED_FAIL",
            engine_execution_status=engine_result.status,
            engine_business_status_before_qa=engine_result.business_verification_status,
            artifact_relpath=artifact_relpath,
            artifact_abs_path=str(artifact_path),
            engine_succeeded=engine_succeeded,
            artifact_exists=False,
            within_workspace=within_workspace,
            content_verified=False,
            hash_verified=False,
            source_integrity_verified=source_integrity_verified,
            business_verified=False,
            expected_sha256=expected_sha256,
            failure_reasons=failure_reasons,
        )

    try:
        raw_bytes = artifact_path.read_bytes()
    except OSError as exc:
        failure_reasons.append(f"Failed reading artifact '{artifact_relpath}': {exc}")
        return EngineArtifactVerificationResult(
            status="VERIFIED_FAIL",
            engine_execution_status=engine_result.status,
            engine_business_status_before_qa=engine_result.business_verification_status,
            artifact_relpath=artifact_relpath,
            artifact_abs_path=str(artifact_path),
            engine_succeeded=engine_succeeded,
            artifact_exists=True,
            within_workspace=within_workspace,
            content_verified=False,
            hash_verified=False,
            source_integrity_verified=source_integrity_verified,
            business_verified=False,
            expected_sha256=expected_sha256,
            failure_reasons=failure_reasons,
        )

    actual_sha256 = compute_sha256_bytes(raw_bytes)
    file_size_bytes = len(raw_bytes)

    try:
        actual_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        failure_reasons.append(f"Artifact is not valid UTF-8 text: {exc}")
        return EngineArtifactVerificationResult(
            status="VERIFIED_FAIL",
            engine_execution_status=engine_result.status,
            engine_business_status_before_qa=engine_result.business_verification_status,
            artifact_relpath=artifact_relpath,
            artifact_abs_path=str(artifact_path),
            engine_succeeded=engine_succeeded,
            artifact_exists=True,
            within_workspace=within_workspace,
            content_verified=False,
            hash_verified=False,
            source_integrity_verified=source_integrity_verified,
            business_verified=False,
            file_size_bytes=file_size_bytes,
            expected_sha256=expected_sha256,
            actual_sha256=actual_sha256,
            failure_reasons=failure_reasons,
        )

    content_verified = True
    if expected_content is not None and actual_text != expected_content:
        content_verified = False
        failure_reasons.append("Artifact content does not match expected format/content.")
    for snippet in required_substrings:
        if snippet not in actual_text:
            content_verified = False
            failure_reasons.append(f"Artifact missing required substring: {snippet}")

    if expected_sha256 is not None:
        hash_verified = actual_sha256 == expected_sha256
        if not hash_verified:
            failure_reasons.append(
                f"SHA-256 mismatch (expected={expected_sha256}, actual={actual_sha256})."
            )
    else:
        hash_verified = True

    business_verified = (
        engine_succeeded
        and within_workspace
        and content_verified
        and hash_verified
        and source_integrity_verified
    )
    return EngineArtifactVerificationResult(
        status="VERIFIED_PASS" if business_verified else "VERIFIED_FAIL",
        engine_execution_status=engine_result.status,
        engine_business_status_before_qa=engine_result.business_verification_status,
        artifact_relpath=artifact_relpath,
        artifact_abs_path=str(artifact_path),
        engine_succeeded=engine_succeeded,
        artifact_exists=True,
        within_workspace=within_workspace,
        content_verified=content_verified,
        hash_verified=hash_verified,
        source_integrity_verified=source_integrity_verified,
        business_verified=business_verified,
        file_size_bytes=file_size_bytes,
        expected_sha256=expected_sha256,
        actual_sha256=actual_sha256,
        failure_reasons=failure_reasons,
    )


__all__ = [
    "EngineArtifactVerificationResult",
    "compute_sha256_bytes",
    "compute_sha256_file",
    "snapshot_file_hashes",
    "verify_engine_artifact",
]
