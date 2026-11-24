"""Independent filesystem and SHA-256 artifact verification for Antigravity tool runs."""

import hashlib
import os
from pathlib import Path
from typing import Sequence

from engines.antigravity.models import (
    AntigravityHandshakeResult,
    ArtifactVerificationResult,
)


def is_symlink_or_junction(path: Path) -> bool:
    """Return True when ``path`` is a symbolic link or Windows NTFS junction."""
    try:
        if path.is_symlink():
            return True
    except OSError:
        return True
    is_junction_fn = getattr(os.path, "isjunction", None)
    if callable(is_junction_fn):
        try:
            if bool(is_junction_fn(path)):
                return True
        except OSError:
            return True
    return False


def has_symlink_or_junction_component(
    path: Path,
    stop_at: Path | None = None,
) -> bool:
    """Inspect ``path`` and its parent chain for symlinks or Windows junctions.

    Args:
        path: Directory or file path inspected without following links.
        stop_at: Optional ancestor directory where traversal stops once reached.

    Returns:
        True if any inspected component is a symlink or junction.
    """
    current = path.absolute()
    stop_abs = stop_at.absolute() if stop_at is not None else None
    visited: set[Path] = set()

    while current not in visited:
        visited.add(current)
        if stop_abs is not None and current == stop_abs:
            break
        if is_symlink_or_junction(current):
            return True
        if current.parent == current:
            break
        current = current.parent
    return False


def compute_sha256_bytes(data: bytes) -> str:
    """Compute the lowercase hexadecimal SHA-256 digest of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def compute_sha256_file(path: Path) -> str:
    """Compute the lowercase hexadecimal SHA-256 digest of a file on disk."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def resolve_within_workspace(workspace_dir: Path, relpath: str) -> Path:
    """Resolve a relative path strictly inside the canonical workspace directory.

    Args:
        workspace_dir: Root directory of the allowed workspace fixture.
        relpath: Relative path within ``workspace_dir``.

    Returns:
        Canonical resolved ``Path`` guaranteed to be inside ``workspace_dir``.

    Raises:
        ValueError: If ``relpath`` is absolute, empty, or escapes ``workspace_dir``.
    """
    if not relpath or not relpath.strip():
        raise ValueError("Relative artifact path must not be empty.")
    candidate_rel = Path(relpath)
    if candidate_rel.is_absolute():
        raise ValueError(f"Path must be relative to workspace, got absolute path: {relpath}")

    canonical_root = workspace_dir.resolve()
    unfollowed = canonical_root / candidate_rel
    if has_symlink_or_junction_component(unfollowed, stop_at=canonical_root):
        raise ValueError(
            f"SYMLINK_ESCAPE_REJECTED: Path '{relpath}' traverses a symlink or junction."
        )
    resolved = unfollowed.resolve()
    try:
        resolved.relative_to(canonical_root)
    except ValueError as exc:
        raise ValueError(
            f"Path '{relpath}' resolves outside workspace '{canonical_root}'."
        ) from exc
    if resolved == canonical_root:
        raise ValueError("Artifact path must reference a file inside workspace, not the workspace root.")
    return resolved


def snapshot_file_hashes(workspace_dir: Path, relpaths: Sequence[str]) -> dict[str, str]:
    """Record pre-execution SHA-256 hashes of protected source files in the workspace.

    Args:
        workspace_dir: Root directory of the workspace fixture.
        relpaths: Relative paths of files that must remain unmodified.

    Returns:
        Mapping from relative path to pre-execution SHA-256 hex digest.

    Raises:
        ValueError: If a relative path escapes the workspace.
        FileNotFoundError: If a protected source file does not exist.
    """
    hashes: dict[str, str] = {}
    for rel in relpaths:
        resolved = resolve_within_workspace(workspace_dir, rel)
        if not resolved.is_file():
            raise FileNotFoundError(f"Protected source file does not exist: {resolved}")
        hashes[rel] = compute_sha256_file(resolved)
    return hashes


def verify_artifact(
    *,
    workspace_dir: Path,
    artifact_relpath: str,
    cli_result: AntigravityHandshakeResult,
    expected_content: str | None = None,
    required_substrings: Sequence[str] = (),
    protected_source_hashes: dict[str, str] | None = None,
) -> ArtifactVerificationResult:
    """Independently inspect the workspace filesystem and verify artifact integrity.

    Args:
        workspace_dir: Root directory of the workspace fixture.
        artifact_relpath: Expected artifact path relative to ``workspace_dir``.
        cli_result: Normalized result returned by the CLI subprocess invocation.
        expected_content: Optional exact UTF-8 content used to compute expected SHA-256.
        required_substrings: Optional substrings that must appear in the artifact text.
        protected_source_hashes: Pre-execution SHA-256 map of source files that must not change.

    Returns:
        Structured ``ArtifactVerificationResult`` separating CLI, existence, content,
        hash, source integrity, and overall business verification flags.
    """
    failure_reasons: list[str] = []
    cli_success = cli_result.status == "SUCCEEDED" and cli_result.exit_code == 0
    if not cli_success:
        failure_reasons.append(
            f"CLI did not succeed (status={cli_result.status}, exit_code={cli_result.exit_code})."
        )

    expected_sha256: str | None = (
        compute_sha256_bytes(expected_content.encode("utf-8"))
        if expected_content is not None
        else None
    )

    source_integrity_verified = True
    for rel, before_sha in (protected_source_hashes or {}).items():
        try:
            src_path = resolve_within_workspace(workspace_dir, rel)
        except ValueError as exc:
            source_integrity_verified = False
            failure_reasons.append(str(exc))
            continue
        if not src_path.is_file():
            source_integrity_verified = False
            failure_reasons.append(f"Protected source file missing after execution: {rel}")
            continue
        after_sha = compute_sha256_file(src_path)
        if after_sha != before_sha:
            source_integrity_verified = False
            failure_reasons.append(
                f"Protected source file '{rel}' was modified (before={before_sha}, after={after_sha})."
            )

    try:
        artifact_path = resolve_within_workspace(workspace_dir, artifact_relpath)
        within_workspace = True
    except ValueError as exc:
        within_workspace = False
        failure_reasons.append(str(exc))
        return ArtifactVerificationResult(
            artifact_relpath=artifact_relpath,
            artifact_abs_path=None,
            cli_success=cli_success,
            artifact_exists=False,
            within_workspace=False,
            content_verified=False,
            hash_verified=False,
            source_integrity_verified=source_integrity_verified,
            business_verified=False,
            expected_sha256=expected_sha256,
            failure_reasons=failure_reasons,
        )

    artifact_exists = artifact_path.is_file()
    if not artifact_exists:
        failure_reasons.append(f"Expected artifact not found: {artifact_relpath}")
        return ArtifactVerificationResult(
            artifact_relpath=artifact_relpath,
            artifact_abs_path=str(artifact_path),
            cli_success=cli_success,
            artifact_exists=False,
            within_workspace=within_workspace,
            content_verified=False,
            hash_verified=False,
            source_integrity_verified=source_integrity_verified,
            business_verified=False,
            expected_sha256=expected_sha256,
            failure_reasons=failure_reasons,
        )

    raw_bytes = artifact_path.read_bytes()
    actual_sha256 = compute_sha256_bytes(raw_bytes)
    file_size_bytes = len(raw_bytes)

    try:
        actual_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        failure_reasons.append(f"Artifact is not valid UTF-8 text: {exc}")
        return ArtifactVerificationResult(
            artifact_relpath=artifact_relpath,
            artifact_abs_path=str(artifact_path),
            cli_success=cli_success,
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
        cli_success
        and artifact_exists
        and within_workspace
        and content_verified
        and hash_verified
        and source_integrity_verified
    )

    return ArtifactVerificationResult(
        artifact_relpath=artifact_relpath,
        artifact_abs_path=str(artifact_path),
        cli_success=cli_success,
        artifact_exists=artifact_exists,
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
