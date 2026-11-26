"""Static Department Manifest contract aligned with DEPARTMENT_SPEC.md."""

from __future__ import annotations

from collections.abc import Collection
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.contracts.agent import DEFAULT_DEPARTMENTS, RESERVED_PROVIDER_DEPARTMENT_NAMES
from core.contracts.identifiers import (
    CapabilityId,
    DepartmentId,
    SemVer,
    ensure_not_secret,
    reject_secret_text,
    validate_capability_id,
    validate_unique_id_list,
)

DepartmentStatus = Literal["ACTIVE", "INACTIVE", "DEPRECATED"]

_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_ICON_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_ZONE_RE = re.compile(r"^[a-z0-9_]{2,64}$")
_TOOL_ID_RE = re.compile(r"^tools\.[a-z0-9_]+\.[a-z0-9_]+$")


def validate_tool_id_syntax(value: str) -> str:
    """Validate canonical ``tools.<category>.<name>`` syntax without secrets."""
    cleaned = ensure_not_secret(value, "tool_id")
    if not _TOOL_ID_RE.fullmatch(cleaned):
        raise ValueError(
            f"tool_id must match '^tools\\.[a-z0-9_]+\\.[a-z0-9_]+$', got {value!r}."
        )
    return cleaned


class DepartmentVisualMetadata(BaseModel):
    """UI presentation metadata for 2D/3D department rendering without runtime logic."""

    model_config = ConfigDict(extra="forbid")

    color_code: str
    icon: str
    zone_id: str

    @field_validator("color_code")
    @classmethod
    def _validate_color(cls, value: str) -> str:
        """Require canonical 6-digit hexadecimal color code."""
        cleaned = ensure_not_secret(value, "color_code")
        if not _HEX_COLOR_RE.fullmatch(cleaned):
            raise ValueError(f"color_code must match '^#[0-9A-Fa-f]{{6}}$', got {value!r}.")
        return cleaned

    @field_validator("icon")
    @classmethod
    def _validate_icon(cls, value: str) -> str:
        """Validate normalized icon identifier."""
        cleaned = ensure_not_secret(value, "icon")
        if not _ICON_RE.fullmatch(cleaned):
            raise ValueError(f"icon must be kebab/snake identifier, got {value!r}.")
        return cleaned

    @field_validator("zone_id")
    @classmethod
    def _validate_zone(cls, value: str) -> str:
        """Validate normalized snake_case visual zone identifier."""
        cleaned = ensure_not_secret(value, "zone_id")
        if not _ZONE_RE.fullmatch(cleaned):
            raise ValueError(f"zone_id must match '^[a-z0-9_]{{2,64}}$', got {value!r}.")
        return cleaned


class DepartmentManifest(BaseModel):
    """Static functional Department specification compliant with DEPARTMENT_SPEC.md.

    Note: ``default_tools`` expresses advisory capability recommendations only and
    never grants execution permissions to any Agent.
    """

    model_config = ConfigDict(extra="forbid")

    spec_version: SemVer
    department_id: DepartmentId
    name: str = Field(min_length=3, max_length=64)
    description: str = Field(min_length=1, max_length=1000)
    purpose: str = Field(min_length=1, max_length=1000)
    responsibilities: list[str] = Field(min_length=1, max_length=32)
    supported_capabilities: list[CapabilityId] = Field(min_length=1, max_length=64)
    default_tools: list[str] = Field(default_factory=list, max_length=64)
    status: DepartmentStatus
    visual_metadata: DepartmentVisualMetadata

    @field_validator("name", "description", "purpose")
    @classmethod
    def _validate_text_fields(cls, value: str, info: object) -> str:
        """Ensure required descriptive fields are non-blank and free of secrets."""
        return ensure_not_secret(value, getattr(info, "field_name", "department_text"))

    @field_validator("responsibilities")
    @classmethod
    def _validate_responsibilities(cls, values: list[str]) -> list[str]:
        """Validate non-empty unique department responsibility entries."""
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in values:
            item = ensure_not_secret(raw, "responsibilities")
            if item in seen:
                raise ValueError(f"Duplicate responsibility entry: {item!r}.")
            seen.add(item)
            cleaned.append(item)
        return cleaned

    @field_validator("supported_capabilities")
    @classmethod
    def _validate_capabilities(cls, values: list[str]) -> list[str]:
        """Validate deduplicated snake_case capability tags."""
        return validate_unique_id_list(
            values, validate_capability_id, "supported_capabilities"
        )

    @field_validator("default_tools")
    @classmethod
    def _validate_default_tools(cls, values: list[str]) -> list[str]:
        """Validate advisory Tool IDs strictly by syntax without granting rights."""
        return validate_unique_id_list(values, validate_tool_id_syntax, "default_tools")

    @model_validator(mode="after")
    def _enforce_department_invariants(self) -> DepartmentManifest:
        """Keep functional departments distinct from AI Providers and Master orchestration."""
        if self.department_id == "orchestration":
            raise ValueError(
                "'orchestration' is reserved exclusively for core.master, not a functional Department."
            )
        if self.department_id in RESERVED_PROVIDER_DEPARTMENT_NAMES:
            raise ValueError(
                f"department_id '{self.department_id}' collides with a reserved AI Provider name."
            )
        return self


def is_valid_worker_department(
    department: str,
    *,
    custom_departments: Collection[str] = (),
) -> bool:
    """Return True if ``department`` names a valid default or registered Worker department."""
    if department == "orchestration" or department in RESERVED_PROVIDER_DEPARTMENT_NAMES:
        return False
    allowed = set(DEFAULT_DEPARTMENTS) | set(custom_departments)
    return department in allowed
