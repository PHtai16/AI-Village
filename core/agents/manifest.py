"""Static Agent Manifest Pydantic v2 schema and compatibility mappers per AGENT_SPEC.md."""

from __future__ import annotations

from collections.abc import Collection, Sequence
import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.agents.department import is_valid_worker_department, validate_tool_id_syntax
from core.contracts.agent import (
    DEFAULT_DEPARTMENTS,
    RESERVED_PROVIDER_DEPARTMENT_NAMES,
    AgentIdentity,
    AgentSystemRole,
)
from core.contracts.identifiers import (
    AgentId,
    CapabilityId,
    DepartmentId,
    SemVer,
    SnakeId,
    ensure_not_secret,
    reject_secret_text,
    validate_capability_id,
    validate_safe_payload,
    validate_snake_id,
    validate_unique_id_list,
)
from core.contracts.references import ConnectionRef
from core.orchestration.spike_models import SpikeAgentSpec

MemoryScope = Literal["private", "project", "global"]
ModelTier = Literal["pro", "flash", "code"]
FallbackPolicyMode = Literal["none", "same_tier_allowed", "operator_approved_only"]
NetworkPolicyMode = Literal["none", "intranet", "full"]

_ACTION_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_DRIVE_OR_SCHEME_RE = re.compile(r"^(?:[A-Za-z]:|[A-Za-z][A-Za-z0-9+.-]*://)")
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z0-9_.-]*)\}")
_FORBIDDEN_ENGINE_PARAM_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "apikey",
        "raw_key",
        "secret",
        "token",
        "password",
        "credential",
        "authorization",
        "private_key",
        "x-goog-api-key",
    }
)
_FORBIDDEN_WRITE_TOP_DIRS: frozenset[str] = frozenset(
    {
        "core",
        "engines",
        "agents",
        "storage",
        "tools",
        "skills",
        "api",
        "ui",
        "docs",
        "tests",
        ".git",
        ".runtime",
        ".ssh",
        "secrets",
    }
)
_SENSITIVE_PATH_TOKENS: frozenset[str] = frozenset(
    {
        ".env",
        ".ssh",
        "secrets",
        ".runtime",
        ".git",
        "credentials.json",
        "token.json",
        "id_rsa",
        "id_ed25519",
    }
)
_SENSITIVE_EXTENSIONS: tuple[str, ...] = (".pem", ".key", ".cert", ".crt", ".pfx", ".p12")


def _validate_skill_id(value: str) -> str:
    """Validate snake_case skill identifier."""
    return validate_snake_id(value, "skill_id")


def _validate_action_id(value: str) -> str:
    """Validate ``<domain>.<action>`` governance action identifier without wildcards."""
    cleaned = ensure_not_secret(value, "requires_approval")
    if "*" in cleaned or "?" in cleaned:
        raise ValueError(f"Action ID must not contain wildcards: {value!r}.")
    if not _ACTION_ID_RE.fullmatch(cleaned):
        raise ValueError(
            f"Action ID must match '^[a-z0-9_]+\\.[a-z0-9_]+$', got {value!r}."
        )
    return cleaned


def _validate_scope_pattern(raw_scope: str, *, mode: Literal["fs_read", "fs_write"]) -> str:
    """Validate static filesystem scope pattern fail-closed without touching disk."""
    cleaned = ensure_not_secret(raw_scope, mode)
    if cleaned.startswith(("/", "\\", "~")) or _DRIVE_OR_SCHEME_RE.match(cleaned):
        raise ValueError(f"{mode} must be a repository/workspace-relative scope, got {raw_scope!r}.")

    normalized = cleaned.replace("\\", "/")
    if "?" in normalized:
        raise ValueError(f"{mode} must not use single-char wildcard '?': {raw_scope!r}.")

    posix = PurePosixPath(normalized)
    if posix.is_absolute() or ".." in posix.parts or "." in posix.parts:
        raise ValueError(f"{mode} contains disallowed traversal segments: {raw_scope!r}.")

    parts = posix.parts
    if not parts:
        raise ValueError(f"{mode} pattern must not be empty.")
    if parts == ("*",) or parts == ("**",) or (parts[0] in {"*", "**"}):
        raise ValueError(f"{mode} forbids unbounded root wildcards like {raw_scope!r}.")
    if any("**" in seg for seg in parts):
        raise ValueError(f"{mode} forbids recursive wildcard '**': {raw_scope!r}.")

    for seg in parts:
        for match in _PLACEHOLDER_RE.finditer(seg):
            if match.group(1) != "task_id":
                raise ValueError(
                    f"{mode} permits only '{{task_id}}' placeholder, got '{{{match.group(1)}}}'."
                )

    lowered_parts = [p.lower() for p in parts]
    for seg in lowered_parts:
        if (
            seg in _SENSITIVE_PATH_TOKENS
            or seg.startswith(".env")
            or any(seg.endswith(ext) for ext in _SENSITIVE_EXTENSIONS)
        ):
            raise ValueError(f"{mode} forbids sensitive secret or runtime path segment: {raw_scope!r}.")

    top_dir = lowered_parts[0]
    if mode == "fs_write":
        if top_dir in _FORBIDDEN_WRITE_TOP_DIRS or top_dir in {"agents.md", "pyproject.toml"}:
            raise ValueError(
                f"fs_write forbids writing to protected system path '{top_dir}' ({raw_scope!r})."
            )
        if top_dir not in {"workspace", "sandbox"}:
            raise ValueError(
                f"fs_write must stay strictly inside 'workspace/' or 'sandbox/', got {raw_scope!r}."
            )
        if len(parts) < 2 or parts[1] == "*":
            raise ValueError(
                f"fs_write must scope beneath a task or sandbox subpath, got {raw_scope!r}."
            )

    return posix.as_posix()


class AgentMission(BaseModel):
    """Static Mission section specifying Agent purpose, duties, and strict non-goals."""

    model_config = ConfigDict(extra="forbid")

    purpose: str = Field(min_length=3, max_length=1000)
    responsibilities: list[str] = Field(min_length=1, max_length=32)
    non_goals: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("purpose")
    @classmethod
    def _validate_purpose(cls, value: str) -> str:
        """Ensure purpose is non-blank and secret-free."""
        return ensure_not_secret(value, "mission.purpose")

    @field_validator("responsibilities", "non_goals")
    @classmethod
    def _validate_string_lists(cls, values: list[str], info: object) -> list[str]:
        """Deduplicate and validate non-blank mission items."""
        field_name = getattr(info, "field_name", "mission")
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in values:
            item = ensure_not_secret(raw, f"mission.{field_name}")
            if item in seen:
                raise ValueError(f"Duplicate item in mission.{field_name}: {item!r}.")
            seen.add(item)
            cleaned.append(item)
        return cleaned


class AgentCapabilities(BaseModel):
    """Static capability tags and declared I/O formats (declared only, not verified execution)."""

    model_config = ConfigDict(extra="forbid")

    skills: list[CapabilityId] = Field(min_length=1, max_length=64)
    inputs: list[str] = Field(min_length=1, max_length=32)
    outputs: list[str] = Field(min_length=1, max_length=32)

    @field_validator("skills")
    @classmethod
    def _validate_skills(cls, values: list[str]) -> list[str]:
        """Validate deduplicated snake_case capability tags."""
        return validate_unique_id_list(values, validate_capability_id, "capabilities.skills")

    @field_validator("inputs", "outputs")
    @classmethod
    def _validate_formats(cls, values: list[str], info: object) -> list[str]:
        """Validate deduplicated snake_case input/output format tokens."""
        field_name = getattr(info, "field_name", "formats")
        return validate_unique_id_list(
            values,
            lambda raw: validate_snake_id(raw, f"capabilities.{field_name}"),
            f"capabilities.{field_name}",
        )


class AgentEngineConfig(BaseModel):
    """Advisory Engine and routing configuration (never holds secrets or runtime permissions)."""

    model_config = ConfigDict(extra="forbid")

    default_type: SnakeId
    provider_id: SnakeId | None = None
    adapter_id: SnakeId | None = None
    connection_id: SnakeId | None = None
    model_tier: ModelTier
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    routing_profile: SnakeId | None = None
    fallback_policy: FallbackPolicyMode | None = None
    parameters: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @field_validator("model_id")
    @classmethod
    def _validate_model_id(cls, value: str | None) -> str | None:
        """Ensure optional model ID is non-blank and secret-free."""
        if value is None:
            return None
        return ensure_not_secret(value, "engine.model_id")

    @field_validator("parameters")
    @classmethod
    def _validate_parameters(
        cls,
        value: dict[str, str | int | float | bool | None],
    ) -> dict[str, str | int | float | bool | None]:
        """Reject secret keys and credential strings inside static engine parameters."""
        for key in value:
            if key.strip().lower() in _FORBIDDEN_ENGINE_PARAM_KEYS:
                raise ValueError(
                    f"engine.parameters forbids credential key '{key}'."
                )
        validate_safe_payload(value, "engine.parameters")
        return value


class AgentMemoryConfig(BaseModel):
    """Permitted memory tiers for the Agent (always includes 'private')."""

    model_config = ConfigDict(extra="forbid")

    scopes: list[MemoryScope] = Field(min_length=1, max_length=3)

    @field_validator("scopes")
    @classmethod
    def _validate_scopes(cls, values: list[MemoryScope]) -> list[MemoryScope]:
        """Deduplicate memory scopes and require 'private'."""
        if len(set(values)) != len(values):
            raise ValueError("memory.scopes must not contain duplicates.")
        if "private" not in values:
            raise ValueError("memory.scopes must include at least 'private'.")
        return values


class AgentSkillsConfig(BaseModel):
    """Approved skill IDs referenced by static manifest."""

    model_config = ConfigDict(extra="forbid")

    allowed: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("allowed")
    @classmethod
    def _validate_allowed_skills(cls, values: list[str]) -> list[str]:
        """Validate deduplicated skill identifiers without wildcards."""
        return validate_unique_id_list(values, _validate_skill_id, "skills.allowed")


class AgentToolsConfig(BaseModel):
    """Requested Tool IDs referenced by static manifest (subject to Policy + Tool Registry)."""

    model_config = ConfigDict(extra="forbid")

    allowed: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("allowed")
    @classmethod
    def _validate_allowed_tools(cls, values: list[str]) -> list[str]:
        """Validate canonical ``tools.<category>.<name>`` identifiers without wildcards."""
        return validate_unique_id_list(values, validate_tool_id_syntax, "tools.allowed")


class AgentPermissionsConfig(BaseModel):
    """Declared permission bounds checked independently by Governance before execution."""

    model_config = ConfigDict(extra="forbid")

    fs_read: list[str] = Field(default_factory=list, max_length=64)
    fs_write: list[str] = Field(default_factory=list, max_length=32)
    network: NetworkPolicyMode
    requires_approval: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("fs_read")
    @classmethod
    def _validate_fs_read(cls, values: list[str]) -> list[str]:
        """Validate deduplicated safe read path patterns."""
        return validate_unique_id_list(
            values,
            lambda item: _validate_scope_pattern(item, mode="fs_read"),
            "permissions.fs_read",
        )

    @field_validator("fs_write")
    @classmethod
    def _validate_fs_write(cls, values: list[str]) -> list[str]:
        """Validate deduplicated sandboxed write path patterns."""
        return validate_unique_id_list(
            values,
            lambda item: _validate_scope_pattern(item, mode="fs_write"),
            "permissions.fs_write",
        )

    @field_validator("requires_approval")
    @classmethod
    def _validate_requires_approval(cls, values: list[str]) -> list[str]:
        """Validate deduplicated ``<domain>.<action>`` entries."""
        return validate_unique_id_list(values, _validate_action_id, "permissions.requires_approval")


class AgentLimitsConfig(BaseModel):
    """Resource safety boundaries enforced on Agent invocations."""

    model_config = ConfigDict(extra="forbid")

    timeout_seconds: int = Field(ge=1, le=3600)
    max_retries: int = Field(ge=0, le=5)


class AgentManifest(BaseModel):
    """Validated Static Agent Manifest matching AGENT_SPEC.md Section 3.1.

    This contract is purely declarative: loading or validating an ``AgentManifest``
    never registers the Agent, grants runtime permissions, or starts an Engine.
    """

    model_config = ConfigDict(extra="forbid")

    spec_version: SemVer
    id: AgentId
    name: str = Field(min_length=3, max_length=64)
    role: str = Field(min_length=3, max_length=64)
    system_role: AgentSystemRole
    department: DepartmentId
    description: str = Field(min_length=3, max_length=1000)
    mission: AgentMission
    capabilities: AgentCapabilities
    engine: AgentEngineConfig
    memory: AgentMemoryConfig
    skills: AgentSkillsConfig = Field(default_factory=AgentSkillsConfig)
    tools: AgentToolsConfig
    permissions: AgentPermissionsConfig
    limits: AgentLimitsConfig

    @field_validator("name", "role", "description")
    @classmethod
    def _validate_labels(cls, value: str, info: object) -> str:
        """Ensure identity text fields are non-blank and free of secrets."""
        return ensure_not_secret(value, getattr(info, "field_name", "manifest"))

    @model_validator(mode="after")
    def _enforce_static_manifest_invariants(self) -> AgentManifest:
        """Enforce Master/Worker separation, provider separation, and AgentIdentity compatibility."""
        if self.department in RESERVED_PROVIDER_DEPARTMENT_NAMES:
            raise ValueError(
                f"department '{self.department}' names an AI Provider, not an organizational domain."
            )

        is_master_id = self.id == "core.master"
        is_orch_role = self.system_role == "orchestrator"
        is_orch_dept = self.department == "orchestration"

        if is_orch_role and not (is_master_id and is_orch_dept):
            raise ValueError(
                "Only id='core.master' with department='orchestration' may use system_role='orchestrator'."
            )
        if is_master_id and not (is_orch_role and is_orch_dept):
            raise ValueError(
                "id='core.master' must set system_role='orchestrator' and department='orchestration'."
            )
        if is_orch_dept and not (is_master_id and is_orch_role):
            raise ValueError(
                "department='orchestration' is reserved exclusively for id='core.master'."
            )

        if is_master_id and self.permissions.fs_write:
            raise ValueError(
                "Master manifest (core.master) must not declare permissions.fs_write entries."
            )

        # Verify strict compatibility with core.contracts.agent.AgentIdentity.
        self.to_agent_identity()
        return self

    def to_agent_identity(self) -> AgentIdentity:
        """Map static manifest fields to core ``AgentIdentity`` without secret exposure."""
        connections: list[ConnectionRef] = []
        if self.engine.provider_id is not None and self.engine.connection_id is not None:
            connections.append(
                ConnectionRef(
                    provider_id=self.engine.provider_id,
                    connection_id=self.engine.connection_id,
                    model_id=self.engine.model_id,
                )
            )
        return AgentIdentity(
            agent_id=self.id,
            name=self.name,
            role=self.role,
            system_role=self.system_role,
            department=self.department,
            description=self.description,
            capability_ids=list(self.capabilities.skills),
            allowed_connections=connections,
        )

    def canonical_json(self) -> str:
        """Return deterministic key-sorted JSON serialization for hashing and persistence."""
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def canonical_sha256(self) -> str:
        """Return lowercase 64-char SHA-256 hex digest over canonical manifest JSON."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def validate_agent_manifest(
    raw_data: dict[str, Any],
    *,
    allowed_departments: Collection[str] | None = None,
    known_providers: Collection[str] | None = None,
    registered_tools: Collection[str] | None = None,
) -> AgentManifest:
    """Validate raw manifest mapping against schema, department, provider, and tool allowlists."""
    manifest = AgentManifest.model_validate(raw_data)

    if manifest.system_role == "worker":
        custom_depts = (
            set(allowed_departments) if allowed_departments is not None else set(DEFAULT_DEPARTMENTS)
        )
        if not is_valid_worker_department(manifest.department, custom_departments=custom_depts):
            raise ValueError(
                f"Unknown or invalid worker department '{manifest.department}'. "
                f"Expected one of {sorted(custom_depts)}."
            )

    if known_providers is not None:
        allowed_prov = set(known_providers)
        if manifest.engine.provider_id is not None and manifest.engine.provider_id not in allowed_prov:
            raise ValueError(
                f"Unknown engine.provider_id '{manifest.engine.provider_id}'; allowed={sorted(allowed_prov)}."
            )

    if registered_tools is not None:
        known_tools = set(registered_tools)
        unknown_tools = [t for t in manifest.tools.allowed if t not in known_tools]
        if unknown_tools:
            raise ValueError(
                f"Manifest references unregistered Tool IDs: {unknown_tools}."
            )

    return manifest


def manifest_to_spike_agent_spec(
    manifest: AgentManifest,
    *,
    allowed_handoff_targets: Sequence[str] = (),
) -> SpikeAgentSpec:
    """Pure compatibility adapter converting ``AgentManifest`` to P03.01 ``SpikeAgentSpec``.

    Args:
        manifest: Validated static manifest.
        allowed_handoff_targets: Explicit downstream worker targets for Master only.

    Returns:
        Validated ``SpikeAgentSpec`` preserving P03.01 role and sandbox boundaries.
    """
    if manifest.department not in {"orchestration", "it"}:
        raise ValueError(
            f"SpikeAgentSpec only accepts department 'orchestration' or 'it', got '{manifest.department}'."
        )
    if manifest.system_role == "orchestrator":
        return SpikeAgentSpec(
            agent_id=manifest.id,
            department="orchestration",
            system_role="orchestrator",
            execution_mode="plan",
            allow_workspace_write=False,
            allow_shell=False,
            allowed_handoff_targets=list(allowed_handoff_targets),
        )
    return SpikeAgentSpec(
        agent_id=manifest.id,
        department="it",
        system_role="worker",
        execution_mode="accept-edits",
        allow_workspace_write=bool(manifest.permissions.fs_write),
        allow_shell=False,
        allowed_handoff_targets=[],
    )


_ = reject_secret_text
