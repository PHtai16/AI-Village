"""Safe fail-closed YAML loader for Agent and Department manifests."""

from __future__ import annotations

from collections.abc import Collection
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError
import yaml
from yaml.constructor import ConstructorError

from core.agents.department import DepartmentManifest
from core.agents.manifest import AgentManifest, validate_agent_manifest

ManifestLoadErrorCode = Literal[
    "PATH_OUTSIDE_ALLOWED_ROOT",
    "FILE_NOT_FOUND",
    "FILE_TOO_LARGE",
    "INVALID_YAML",
    "DUPLICATE_YAML_KEY",
    "UNSAFE_YAML_TAG",
    "INVALID_MANIFEST_SCHEMA",
    "PATH_ID_MISMATCH",
]

DEFAULT_MAX_MANIFEST_BYTES = 65_536


class ManifestLoadError(RuntimeError):
    """Structured exception raised when YAML manifest loading or validation fails."""

    def __init__(self, error_code: ManifestLoadErrorCode, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


class _StrictNoDuplicateSafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys fail-closed."""


def _construct_mapping_no_duplicates(
    loader: _StrictNoDuplicateSafeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    """Construct mapping while raising ``ManifestLoadError`` on duplicate keys."""
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            exists = key in mapping
        except TypeError as exc:
            raise ManifestLoadError(
                "INVALID_YAML",
                f"Unhashable YAML mapping key encountered: {exc}",
            ) from exc
        if exists:
            raise ManifestLoadError(
                "DUPLICATE_YAML_KEY",
                f"Duplicate YAML key '{key}' is forbidden.",
            )
        value = loader.construct_object(value_node, deep=deep)
        mapping[key] = value
    return mapping


_StrictNoDuplicateSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_no_duplicates,
)


def _resolve_bounded_file(file_path: str | Path, *, allowed_root: str | Path) -> tuple[Path, Path]:
    """Resolve ``file_path`` via ``os.path.realpath`` and verify containment inside ``allowed_root``."""
    raw_path = Path(file_path)
    if any(part == ".." for part in raw_path.parts):
        raise ManifestLoadError(
            "PATH_OUTSIDE_ALLOWED_ROOT",
            f"Path traversal segment '..' rejected: {file_path!r}.",
        )
    try:
        real_root = Path(os.path.realpath(allowed_root))
        candidate = raw_path if raw_path.is_absolute() else Path(allowed_root) / raw_path
        real_target = Path(os.path.realpath(candidate))
        real_target.relative_to(real_root)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ManifestLoadError(
            "PATH_OUTSIDE_ALLOWED_ROOT",
            f"Manifest path '{file_path}' escapes allowed root '{allowed_root}': {exc}",
        ) from exc
    return real_root, real_target


def load_yaml_document(
    file_path: str | Path,
    *,
    allowed_root: str | Path,
    max_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
) -> tuple[dict[str, Any], Path, Path]:
    """Load a bounded YAML mapping safely without executing custom constructors."""
    real_root, real_target = _resolve_bounded_file(file_path, allowed_root=allowed_root)
    if not real_target.is_file():
        raise ManifestLoadError(
            "FILE_NOT_FOUND",
            f"Manifest file does not exist: '{real_target}'.",
        )

    size_bytes = real_target.stat().st_size
    if size_bytes > max_bytes:
        raise ManifestLoadError(
            "FILE_TOO_LARGE",
            f"Manifest file exceeds max limit ({size_bytes} > {max_bytes} bytes).",
        )

    try:
        raw_text = real_target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestLoadError("INVALID_YAML", f"Unable to read UTF-8 YAML manifest: {exc}") from exc

    try:
        parsed = yaml.load(raw_text, Loader=_StrictNoDuplicateSafeLoader)
    except ManifestLoadError:
        raise
    except ConstructorError as exc:
        raise ManifestLoadError(
            "UNSAFE_YAML_TAG",
            f"Unsafe or unsupported YAML tag rejected fail-closed: {exc}",
        ) from exc
    except yaml.YAMLError as exc:
        raise ManifestLoadError("INVALID_YAML", f"Malformed YAML syntax: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ManifestLoadError(
            "INVALID_MANIFEST_SCHEMA",
            f"YAML manifest root must be a mapping, got {type(parsed).__name__}.",
        )
    return parsed, real_root, real_target


def _verify_agent_path_convention(manifest: AgentManifest, rel_path: Path) -> None:
    """Verify manifest ID aligns with canonical ``agents/master/`` or ``agents/roles/<slug>/`` layout."""
    parts = rel_path.parts
    if parts == ("master", "agent.yaml"):
        if manifest.id != "core.master":
            raise ManifestLoadError(
                "PATH_ID_MISMATCH",
                f"Manifest at 'master/agent.yaml' must use id='core.master', got '{manifest.id}'.",
            )
        return

    if len(parts) == 3 and parts[0] == "roles" and parts[2] == "agent.yaml":
        expected_id_slug = manifest.id.replace(".", "_")
        if parts[1] != expected_id_slug:
            raise ManifestLoadError(
                "PATH_ID_MISMATCH",
                (
                    f"Manifest directory '{parts[1]}' under 'roles/' does not match manifest id "
                    f"'{manifest.id}' (expected '{expected_id_slug}')."
                ),
            )


def load_agent_manifest_file(
    file_path: str | Path,
    *,
    allowed_root: str | Path,
    enforce_path_convention: bool = True,
    allowed_departments: Collection[str] | None = None,
    known_providers: Collection[str] | None = None,
    registered_tools: Collection[str] | None = None,
    max_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
) -> AgentManifest:
    """Safely load and validate an ``AgentManifest`` YAML file within ``allowed_root``."""
    raw_data, real_root, real_target = load_yaml_document(
        file_path,
        allowed_root=allowed_root,
        max_bytes=max_bytes,
    )
    try:
        manifest = validate_agent_manifest(
            raw_data,
            allowed_departments=allowed_departments,
            known_providers=known_providers,
            registered_tools=registered_tools,
        )
    except (ValidationError, ValueError) as exc:
        raise ManifestLoadError(
            "INVALID_MANIFEST_SCHEMA",
            f"Manifest failed schema validation: {exc}",
        ) from exc

    if enforce_path_convention:
        rel_path = real_target.relative_to(real_root)
        _verify_agent_path_convention(manifest, rel_path)
    return manifest


def load_department_manifest_file(
    file_path: str | Path,
    *,
    allowed_root: str | Path,
    max_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
) -> DepartmentManifest:
    """Safely load and validate a ``DepartmentManifest`` YAML file within ``allowed_root``."""
    raw_data, _, _ = load_yaml_document(
        file_path,
        allowed_root=allowed_root,
        max_bytes=max_bytes,
    )
    try:
        return DepartmentManifest.model_validate(raw_data)
    except (ValidationError, ValueError) as exc:
        raise ManifestLoadError(
            "INVALID_MANIFEST_SCHEMA",
            f"Department manifest failed schema validation: {exc}",
        ) from exc
