"""Canonical workspace path validation blocking traversal, cross-task leaks, symlinks, and secrets."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Sequence

from pydantic import BaseModel, ConfigDict

_SENSITIVE_FILENAMES: frozenset[str] = frozenset(
    {
        ".env",
        "credentials.json",
        "token.json",
        "id_rsa",
        "id_ed25519",
    }
)
_SENSITIVE_SUFFIXES: tuple[str, ...] = (".pem", ".key", ".cert", ".crt", ".pfx", ".p12", ".secrets")
_PROTECTED_TOP_DIRS: frozenset[str] = frozenset(
    {
        "core",
        "engines",
        "agents",
        "storage",
        ".git",
        ".runtime",
        "secrets",
        ".ssh",
    }
)
_PROTECTED_FILES: frozenset[str] = frozenset({"agents.md", "pyproject.toml"})


class WorkspaceAccessCheck(BaseModel):
    """Structured outcome of canonical workspace path verification."""

    model_config = ConfigDict(extra="forbid")

    allowed: bool
    canonical_path: str | None = None
    reason_code: str
    message: str


def _is_sensitive_path(path_obj: Path) -> bool:
    """Return True if any path segment names secret material or restricted runtime stores."""
    parts_lower = [part.lower() for part in path_obj.parts]
    name_lower = path_obj.name.lower()
    if name_lower in _SENSITIVE_FILENAMES or name_lower.startswith(".env."):
        return True
    if any(name_lower.endswith(sfx) for sfx in _SENSITIVE_SUFFIXES):
        return True
    if any(seg in {".ssh", "secrets", ".runtime"} for seg in parts_lower):
        return True
    return False


def _is_within_root(candidate: Path, root: Path) -> bool:
    """Return True iff ``candidate`` equals or resides strictly inside ``root``."""
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def validate_workspace_path(
    workspace_root: str | Path,
    target_path: str | Path,
    *,
    action: str = "files.read",
    cross_task_roots: Sequence[str | Path] = (),
    protected_relpaths: Sequence[str] = (),
) -> WorkspaceAccessCheck:
    """Validate a workspace path lexically and via ``os.path.realpath`` fail-closed."""
    try:
        raw_root = Path(workspace_root)
        real_root = Path(os.path.realpath(raw_root))
        raw_target_str = str(target_path).strip()
        if not raw_target_str:
            return WorkspaceAccessCheck(
                allowed=False,
                reason_code="INVALID_PATH",
                message="Target path must not be blank.",
            )

        lexical_candidate = Path(raw_target_str)
        if any(part == ".." for part in lexical_candidate.parts):
            return WorkspaceAccessCheck(
                allowed=False,
                reason_code="PATH_TRAVERSAL_DENIED",
                message=f"Path traversal segment '..' rejected in {raw_target_str!r}.",
            )

        if not lexical_candidate.is_absolute():
            joined_lexical = raw_root / lexical_candidate
        else:
            joined_lexical = lexical_candidate

        # Resolve symlinks/junctions strictly via os.path.realpath.
        real_target = Path(os.path.realpath(joined_lexical))
    except (OSError, ValueError, RuntimeError) as exc:
        return WorkspaceAccessCheck(
            allowed=False,
            reason_code="UNVERIFIABLE_PATH_DENIED",
            message=f"Unable to canonicalize path safely: {exc}",
        )

    if _is_sensitive_path(lexical_candidate) or _is_sensitive_path(real_target):
        return WorkspaceAccessCheck(
            allowed=False,
            canonical_path=str(real_target),
            reason_code="SECRET_FILE_ACCESS_DENIED",
            message=f"Access to sensitive secret or runtime path is forbidden: {raw_target_str!r}.",
        )

    for cross_root in cross_task_roots:
        try:
            real_cross = Path(os.path.realpath(cross_root))
            if _is_within_root(real_target, real_cross) and real_cross != real_root:
                return WorkspaceAccessCheck(
                    allowed=False,
                    canonical_path=str(real_target),
                    reason_code="CROSS_TASK_ACCESS_DENIED",
                    message=f"Target {real_target} resolves inside another Task workspace ({real_cross}).",
                )
        except (OSError, ValueError):
            return WorkspaceAccessCheck(
                allowed=False,
                reason_code="UNVERIFIABLE_PATH_DENIED",
                message="Failed to verify cross-task isolation boundary.",
            )

    if not _is_within_root(real_target, real_root):
        return WorkspaceAccessCheck(
            allowed=False,
            canonical_path=str(real_target),
            reason_code="PATH_OUTSIDE_WORKSPACE",
            message=(
                f"Canonical path '{real_target}' escapes allowed workspace root '{real_root}'."
            ),
        )

    rel_parts = real_target.relative_to(real_root).parts
    rel_posix = PurePosixPath(*rel_parts).as_posix().lower() if rel_parts else ""
    if rel_parts:
        first_seg = rel_parts[0].lower()
        if action in {"files.write", "files.delete"} and (
            first_seg in _PROTECTED_TOP_DIRS or rel_posix in _PROTECTED_FILES
        ):
            return WorkspaceAccessCheck(
                allowed=False,
                canonical_path=str(real_target),
                reason_code="PROTECTED_PATH_DENIED",
                message=f"Modifying protected system file or directory '{rel_posix}' is forbidden.",
            )

    for prot in protected_relpaths:
        norm_prot = PurePosixPath(prot.replace("\\", "/")).as_posix().lower()
        if rel_posix == norm_prot and action in {"files.write", "files.delete"}:
            return WorkspaceAccessCheck(
                allowed=False,
                canonical_path=str(real_target),
                reason_code="PROTECTED_PATH_DENIED",
                message=f"Target '{rel_posix}' is explicitly protected against modification.",
            )

    return WorkspaceAccessCheck(
        allowed=True,
        canonical_path=str(real_target),
        reason_code="WORKSPACE_PATH_ALLOWED",
        message="Canonical path stays strictly within workspace boundaries.",
    )
