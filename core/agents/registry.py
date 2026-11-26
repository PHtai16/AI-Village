"""Versioned, Governance-enforced Agent Registry core domain service and contracts."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from core.agents.loader import ManifestLoadError, load_agent_manifest_file
from core.agents.manifest import AgentManifest, validate_agent_manifest
from core.contracts.agent import DEFAULT_DEPARTMENTS, AgentSystemRole
from core.contracts.event import Event
from core.contracts.identifiers import (
    AgentId,
    CapabilityId,
    DepartmentId,
    EventId,
    SemVer,
    Sha256Hex,
    ensure_not_secret,
    new_event_id,
    reject_secret_text,
    validate_aware_datetime,
)
from core.governance.manager import PermissionManager
from core.governance.models import PolicyRequest, TrustedAuthContext
from engines.antigravity.diagnostics import redact_secrets

AgentRegistrationStatus = Literal["DRAFT", "VALIDATED", "ACTIVE", "DISABLED", "REJECTED"]

_NETWORK_RANK: dict[str, int] = {"none": 0, "intranet": 1, "full": 2}


class AgentRegistryError(RuntimeError):
    """Structured fail-closed exception raised by Agent Registry operations."""

    def __init__(self, error_code: str, message: str) -> None:
        cleaned = redact_secrets(message)
        super().__init__(cleaned)
        self.error_code = error_code
        self.message = cleaned


class DuplicateAgentError(AgentRegistryError):
    """Raised when attempting to register an ``agent_id`` that already exists."""


class AgentNotFoundError(AgentRegistryError):
    """Raised when querying or mutating a nonexistent ``agent_id`` or revision."""


class StaleAgentVersionError(AgentRegistryError):
    """Raised when optimistic lock ``expected_record_version`` does not match stored version."""


class AgentStateError(AgentRegistryError):
    """Raised on invalid Registry status transitions (e.g. disabling an already DISABLED Agent)."""


class AgentManifestValidationError(AgentRegistryError):
    """Raised when an Agent Manifest fails schema, provider, department, or tool checks."""


class CapabilityVerificationEntry(BaseModel):
    """Tracks declared capability tags separately from validated and observed runtime proof."""

    model_config = ConfigDict(extra="forbid")

    capability_id: CapabilityId
    declared: bool = True
    validated: bool = False
    observed_in_execution: bool = False


class AgentManifestVersion(BaseModel):
    """Immutable historical snapshot for one registered Agent Manifest revision."""

    model_config = ConfigDict(extra="forbid")

    agent_id: AgentId
    manifest_revision: int = Field(ge=1)
    spec_version: SemVer
    manifest: AgentManifest
    manifest_sha256: Sha256Hex
    change_summary: str = Field(min_length=1, max_length=500)
    changed_by: str = Field(min_length=1, max_length=128)
    changed_at: datetime
    audit_event_id: EventId | None = None

    @field_validator("change_summary", "changed_by")
    @classmethod
    def _validate_safe_labels(cls, value: str, info: object) -> str:
        """Ensure revision labels are non-blank and free of credentials."""
        return ensure_not_secret(value, getattr(info, "field_name", "version_field"))

    @field_validator("changed_at")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        """Require explicit timezone-aware timestamp."""
        return validate_aware_datetime(value, "changed_at")


class AgentRegistryRecord(BaseModel):
    """Authoritative Registry state for a registered AI Agent (distinct from Runtime State)."""

    model_config = ConfigDict(extra="forbid")

    agent_id: AgentId
    registration_status: AgentRegistrationStatus
    active_revision: int = Field(ge=1)
    record_version: int = Field(ge=1)
    manifest: AgentManifest
    manifest_sha256: Sha256Hex
    source_path: str | None = Field(default=None, min_length=1, max_length=260)
    capability_verification: list[CapabilityVerificationEntry] = Field(default_factory=list)
    registered_by: str = Field(min_length=1, max_length=128)
    registered_at: datetime
    updated_by: str = Field(min_length=1, max_length=128)
    updated_at: datetime
    disabled_at: datetime | None = None
    disabled_reason: str | None = None
    last_audit_event_id: EventId | None = None

    @field_validator("registered_by", "updated_by", "source_path")
    @classmethod
    def _validate_optional_safe_labels(cls, value: str | None, info: object) -> str | None:
        """Reject secret patterns or blank strings."""
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "registry_field"))

    @field_validator("disabled_reason")
    @classmethod
    def _validate_disabled_reason(cls, value: str | None) -> str | None:
        """Reject secrets in optional disable rationale."""
        if value is None:
            return None
        cleaned = value.strip()
        reject_secret_text(cleaned, "disabled_reason")
        return cleaned or None

    @field_validator("registered_at", "updated_at", "disabled_at")
    @classmethod
    def _validate_timestamps(cls, value: datetime | None, info: object) -> datetime | None:
        """Require explicit timezone-aware timestamps."""
        if value is None:
            return None
        return validate_aware_datetime(value, getattr(info, "field_name", "registry_ts"))

    def is_eligible_for_assignment(self) -> bool:
        """Return True strictly when Registry status allows task assignment consideration."""
        return self.registration_status == "ACTIVE"


class ManifestChangeAnalysis(BaseModel):
    """Structured security diff analyzing risk-bearing mutations between Manifest revisions."""

    model_config = ConfigDict(extra="forbid")

    changed: bool
    permission_escalation: bool
    department_changed: bool
    connection_or_provider_changed: bool
    limits_increased: bool
    new_unverified_capabilities: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    required_governance_action: str


class AgentSearchQuery(BaseModel):
    """Structured Registry search filter operating exclusively on real registered profiles."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str | None = None
    name_contains: str | None = None
    role_contains: str | None = None
    department: str | None = None
    capability: str | None = None
    input_format: str | None = None
    output_format: str | None = None
    registration_status: AgentRegistrationStatus | None = None
    eligible_only: bool = False
    provider_id: str | None = None
    default_engine_type: str | None = None
    require_validated_capability: bool = False
    require_observed_capability: bool = False


class AgentExplorerSummary(BaseModel):
    """Read-only Agent Explorer view strictly omitting fabricated live runtime state."""

    model_config = ConfigDict(extra="forbid")

    agent_id: AgentId
    name: str
    role: str
    system_role: AgentSystemRole
    department: DepartmentId
    registration_status: AgentRegistrationStatus
    active_revision: int = Field(ge=1)
    record_version: int = Field(ge=1)
    capabilities: list[CapabilityVerificationEntry]
    engine_preferences: dict[str, str | None]
    permission_summary: dict[str, Any]
    updated_at: datetime
    runtime_state_source: Literal["NOT_OBSERVED_BY_REGISTRY"] = "NOT_OBSERVED_BY_REGISTRY"


class ManifestDriftReport(BaseModel):
    """Comparison between on-disk YAML Manifest SHA-256 and authoritative SQLite Registry."""

    model_config = ConfigDict(extra="forbid")

    agent_id: AgentId
    status: Literal[
        "IN_SYNC",
        "DRIFT_DETECTED",
        "SOURCE_MISSING",
        "UNREGISTERED_MANIFEST",
        "INVALID_MANIFEST",
    ]
    registered_sha256: Sha256Hex | None = None
    file_sha256: Sha256Hex | None = None
    source_path: str | None = None
    message: str


class AgentMutationOutcome(BaseModel):
    """Structured outcome returned by Registry create/update/disable/enable operations."""

    model_config = ConfigDict(extra="forbid")

    record: AgentRegistryRecord
    version: AgentManifestVersion | None = None
    change_analysis: ManifestChangeAnalysis | None = None
    active_run_ids_not_cancelled: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    audit_event_id: EventId | None = None


@runtime_checkable
class AgentRegistryRepository(Protocol):
    """Durable persistence port isolating ``AgentRegistryService`` from SQLite details."""

    def create_record(
        self,
        record: AgentRegistryRecord,
        initial_version: AgentManifestVersion,
        audit_event: Event,
    ) -> AgentRegistryRecord:
        """Atomically persist initial Registry record, version v1, and audit Event."""

    def get_record(self, agent_id: str) -> AgentRegistryRecord | None:
        """Load current Registry record by ``agent_id`` or return None."""

    def list_records(self, *, include_disabled: bool = True) -> list[AgentRegistryRecord]:
        """List stored Registry records deterministically by ``agent_id``."""

    def update_record(
        self,
        record: AgentRegistryRecord,
        *,
        expected_record_version: int,
        new_version: AgentManifestVersion | None,
        audit_event: Event,
    ) -> AgentRegistryRecord:
        """Atomically update Registry record under optimistic locking and append audit Event."""

    def list_versions(self, agent_id: str) -> list[AgentManifestVersion]:
        """Return immutable historical versions for ``agent_id`` in ascending revision order."""

    def get_version(
        self, agent_id: str, manifest_revision: int
    ) -> AgentManifestVersion | None:
        """Return exact historical revision for Run pinning or inspection."""

    def list_active_run_ids_for_agent(self, agent_id: str) -> list[str]:
        """Read-only lookup of non-terminal Runs linked to sessions of ``agent_id``."""


def analyze_manifest_diff(old: AgentManifest, new: AgentManifest) -> ManifestChangeAnalysis:
    """Analyze security, department, routing, capability, and limit changes between Manifests."""
    reasons: list[str] = []

    new_writes = sorted(set(new.permissions.fs_write) - set(old.permissions.fs_write))
    new_reads = sorted(set(new.permissions.fs_read) - set(old.permissions.fs_read))
    new_tools = sorted(set(new.tools.allowed) - set(old.tools.allowed))
    new_skills = sorted(set(new.skills.allowed) - set(old.skills.allowed))
    dropped_approvals = sorted(
        set(old.permissions.requires_approval) - set(new.permissions.requires_approval)
    )
    new_mem_scopes = sorted(set(new.memory.scopes) - set(old.memory.scopes))
    network_expanded = (
        _NETWORK_RANK[new.permissions.network] > _NETWORK_RANK[old.permissions.network]
    )

    if new_writes:
        reasons.append(f"Added fs_write scopes: {new_writes}")
    if new_reads:
        reasons.append(f"Added fs_read scopes: {new_reads}")
    if new_tools:
        reasons.append(f"Added tools.allowed: {new_tools}")
    if new_skills:
        reasons.append(f"Added skills.allowed: {new_skills}")
    if dropped_approvals:
        reasons.append(f"Removed requires_approval actions: {dropped_approvals}")
    if "global" in new_mem_scopes:
        reasons.append("Added global memory scope")
    if network_expanded:
        reasons.append(
            f"Expanded network policy: {old.permissions.network} -> {new.permissions.network}"
        )

    permission_escalation = any(
        (
            bool(new_writes),
            bool(new_reads),
            bool(new_tools),
            bool(new_skills),
            bool(dropped_approvals),
            "global" in new_mem_scopes,
            network_expanded,
        )
    )

    department_changed = new.department != old.department
    if department_changed:
        reasons.append(f"Changed department: {old.department} -> {new.department}")

    old_engine_routing = (
        old.engine.default_type,
        old.engine.provider_id,
        old.engine.adapter_id,
        old.engine.connection_id,
        old.engine.model_id,
        old.engine.routing_profile,
        old.engine.fallback_policy,
    )
    new_engine_routing = (
        new.engine.default_type,
        new.engine.provider_id,
        new.engine.adapter_id,
        new.engine.connection_id,
        new.engine.model_id,
        new.engine.routing_profile,
        new.engine.fallback_policy,
    )
    connection_or_provider_changed = new_engine_routing != old_engine_routing
    if connection_or_provider_changed:
        reasons.append("Modified engine provider, adapter, connection, model, or routing policy")

    limits_increased = (
        new.limits.timeout_seconds > old.limits.timeout_seconds
        or new.limits.max_retries > old.limits.max_retries
    )
    if limits_increased:
        reasons.append(
            f"Increased execution limits: timeout {old.limits.timeout_seconds}->{new.limits.timeout_seconds}, "
            f"max_retries {old.limits.max_retries}->{new.limits.max_retries}"
        )

    new_caps = sorted(set(new.capabilities.skills) - set(old.capabilities.skills))
    if new_caps:
        reasons.append(f"Declared new unverified capabilities: {new_caps}")

    if permission_escalation or limits_increased:
        required_action = "agent.change_permissions"
    elif department_changed:
        required_action = "agent.change_department"
    elif connection_or_provider_changed:
        required_action = "agent.change_connection"
    else:
        required_action = "agent.update"

    return ManifestChangeAnalysis(
        changed=new.canonical_sha256() != old.canonical_sha256(),
        permission_escalation=permission_escalation,
        department_changed=department_changed,
        connection_or_provider_changed=connection_or_provider_changed,
        limits_increased=limits_increased,
        new_unverified_capabilities=new_caps,
        reasons=reasons,
        required_governance_action=required_action,
    )


def _manifest_has_sensitive_initial_grants(manifest: AgentManifest) -> bool:
    """Return True if a newly active manifest requests elevated network, tools, skills, or global scope."""
    return (
        manifest.permissions.network != "none"
        or bool(manifest.tools.allowed)
        or bool(manifest.skills.allowed)
        or "global" in manifest.memory.scopes
    )


def _build_initial_capability_verification(
    manifest: AgentManifest,
    *,
    validated_capabilities: Collection[str] = (),
) -> list[CapabilityVerificationEntry]:
    """Construct capability verification entries marking declared vs validated accurately."""
    validated_set = set(validated_capabilities)
    return [
        CapabilityVerificationEntry(
            capability_id=cap_id,
            declared=True,
            validated=cap_id in validated_set,
            observed_in_execution=False,
        )
        for cap_id in manifest.capabilities.skills
    ]


class AgentRegistryService:
    """Governance-enforced Agent Registry coordinating YAML validation, versioning, and audit."""

    def __init__(
        self,
        *,
        repository: AgentRegistryRepository,
        permission_manager: PermissionManager,
        allowed_departments: Collection[str] | None = None,
        known_providers: Collection[str] | None = None,
        registered_tools: Collection[str] | None = None,
    ) -> None:
        """Bind Registry service to durable repository, Governance manager, and allowlists."""
        self._repo = repository
        self._pm = permission_manager
        self._allowed_departments = (
            set(allowed_departments) if allowed_departments is not None else set(DEFAULT_DEPARTMENTS)
        )
        self._known_providers = set(known_providers) if known_providers is not None else None
        self._registered_tools = set(registered_tools) if registered_tools is not None else None

    def validate_manifest(self, manifest_or_data: AgentManifest | dict[str, Any]) -> AgentManifest:
        """Validate manifest against schema, department, provider, and tool allowlists."""
        raw = (
            manifest_or_data.model_dump(mode="python")
            if isinstance(manifest_or_data, AgentManifest)
            else manifest_or_data
        )
        try:
            return validate_agent_manifest(
                raw,
                allowed_departments=self._allowed_departments,
                known_providers=self._known_providers,
                registered_tools=self._registered_tools,
            )
        except (ValidationError, ValueError) as exc:
            raise AgentManifestValidationError("INVALID_MANIFEST", str(exc)) from exc

    def register_agent(
        self,
        manifest_or_data: AgentManifest | dict[str, Any],
        *,
        auth_context: TrustedAuthContext | None,
        timestamp: datetime,
        initial_status: Literal["DRAFT", "VALIDATED", "ACTIVE"] = "ACTIVE",
        source_path: str | None = None,
        validated_capabilities: Collection[str] = (),
        approval_id: str | None = None,
        correlation_id: str | None = None,
    ) -> AgentMutationOutcome:
        """Validate, authorize, and atomically register an Agent without launching any Runtime."""
        validate_aware_datetime(timestamp, "timestamp")
        manifest = self.validate_manifest(manifest_or_data)

        requires_sensitive_approval = (
            initial_status == "ACTIVE" and _manifest_has_sensitive_initial_grants(manifest)
        )
        governance_action = "agent.change_permissions" if requires_sensitive_approval else "agent.create"
        manifest_sha = manifest.canonical_sha256()

        policy_req = PolicyRequest(
            auth_context=auth_context,
            action=governance_action,
            resource=f"agent:{manifest.id}",
            approval_id=approval_id,
            parameters={
                "target_agent_id": manifest.id,
                "department": manifest.department,
                "initial_status": initial_status,
                "manifest_sha256": manifest_sha,
                "sensitive_initial_grants": requires_sensitive_approval,
            },
            timestamp=timestamp,
            correlation_id=correlation_id or f"corr-agent-reg-{manifest.id}",
        )

        def _persist_registration() -> AgentMutationOutcome:
            if self._repo.get_record(manifest.id) is not None:
                raise DuplicateAgentError(
                    "DUPLICATE_AGENT_ID",
                    f"Agent '{manifest.id}' is already registered.",
                )

            assert auth_context is not None
            audit_ev = Event(
                event_id=new_event_id(),
                timestamp=timestamp,
                event_type="agent.registered",
                source="governance",
                actor_id=manifest.id,
                provider_id=manifest.engine.provider_id,
                connection_id=manifest.engine.connection_id,
                model_id=manifest.engine.model_id,
                correlation_id=policy_req.correlation_id,
                redacted_payload={
                    "target_agent_id": manifest.id,
                    "department": manifest.department,
                    "registration_status": initial_status,
                    "manifest_revision": 1,
                    "manifest_sha256": manifest_sha,
                    "registered_by": auth_context.actor_id,
                },
            )
            version_row = AgentManifestVersion(
                agent_id=manifest.id,
                manifest_revision=1,
                spec_version=manifest.spec_version,
                manifest=manifest,
                manifest_sha256=manifest_sha,
                change_summary="Initial validated manifest registration",
                changed_by=auth_context.actor_id,
                changed_at=timestamp,
                audit_event_id=audit_ev.event_id,
            )
            record = AgentRegistryRecord(
                agent_id=manifest.id,
                registration_status=initial_status,
                active_revision=1,
                record_version=1,
                manifest=manifest,
                manifest_sha256=manifest_sha,
                source_path=source_path,
                capability_verification=_build_initial_capability_verification(
                    manifest, validated_capabilities=validated_capabilities
                ),
                registered_by=auth_context.actor_id,
                registered_at=timestamp,
                updated_by=auth_context.actor_id,
                updated_at=timestamp,
                last_audit_event_id=audit_ev.event_id,
            )
            saved = self._repo.create_record(record, version_row, audit_ev)
            return AgentMutationOutcome(
                record=saved,
                version=version_row,
                audit_event_id=audit_ev.event_id,
            )

        _, outcome = self._pm.dispatch_protected_action(policy_req, _persist_registration)
        return outcome

    def get_agent(self, agent_id: str) -> AgentRegistryRecord | None:
        """Fetch current Registry record by ``agent_id`` (returns None if unregistered)."""
        return self._repo.get_record(agent_id)

    def list_agents(self, *, include_disabled: bool = True) -> list[AgentRegistryRecord]:
        """Return deterministically ordered Agent Registry records."""
        return self._repo.list_records(include_disabled=include_disabled)

    def search_agents(self, query: AgentSearchQuery) -> list[AgentRegistryRecord]:
        """Filter real registered Agents deterministically by role, department, and capability."""
        records = self._repo.list_records(include_disabled=not query.eligible_only)
        matched: list[AgentRegistryRecord] = []
        for rec in records:
            if query.eligible_only and not rec.is_eligible_for_assignment():
                continue
            if query.agent_id is not None and rec.agent_id != query.agent_id:
                continue
            if (
                query.registration_status is not None
                and rec.registration_status != query.registration_status
            ):
                continue
            if query.department is not None and rec.manifest.department != query.department:
                continue
            if (
                query.name_contains is not None
                and query.name_contains.lower() not in rec.manifest.name.lower()
            ):
                continue
            if (
                query.role_contains is not None
                and query.role_contains.lower() not in rec.manifest.role.lower()
            ):
                continue
            if (
                query.input_format is not None
                and query.input_format not in rec.manifest.capabilities.inputs
            ):
                continue
            if (
                query.output_format is not None
                and query.output_format not in rec.manifest.capabilities.outputs
            ):
                continue
            if (
                query.provider_id is not None
                and rec.manifest.engine.provider_id != query.provider_id
            ):
                continue
            if (
                query.default_engine_type is not None
                and rec.manifest.engine.default_type != query.default_engine_type
            ):
                continue
            if query.capability is not None:
                cap_entry = next(
                    (
                        item
                        for item in rec.capability_verification
                        if item.capability_id == query.capability
                    ),
                    None,
                )
                if cap_entry is None:
                    continue
                if query.require_validated_capability and not cap_entry.validated:
                    continue
                if query.require_observed_capability and not cap_entry.observed_in_execution:
                    continue
            matched.append(rec)
        return matched

    def update_agent(
        self,
        agent_id: str,
        new_manifest_or_data: AgentManifest | dict[str, Any],
        *,
        expected_record_version: int,
        auth_context: TrustedAuthContext | None,
        timestamp: datetime,
        change_summary: str,
        source_path: str | None = None,
        validated_capabilities: Collection[str] | None = None,
        approval_id: str | None = None,
        correlation_id: str | None = None,
    ) -> AgentMutationOutcome:
        """Create a new immutable manifest revision under Governance and optimistic locking."""
        validate_aware_datetime(timestamp, "timestamp")
        ensure_not_secret(change_summary, "change_summary")
        new_manifest = self.validate_manifest(new_manifest_or_data)
        if new_manifest.id != agent_id:
            raise AgentManifestValidationError(
                "MANIFEST_ID_MISMATCH",
                f"Manifest id '{new_manifest.id}' does not match target '{agent_id}'.",
            )

        current = self._repo.get_record(agent_id)
        if current is None:
            raise AgentNotFoundError("AGENT_NOT_FOUND", f"Agent '{agent_id}' is not registered.")

        diff = analyze_manifest_diff(current.manifest, new_manifest)
        new_sha = new_manifest.canonical_sha256()

        policy_req = PolicyRequest(
            auth_context=auth_context,
            action=diff.required_governance_action,
            resource=f"agent:{agent_id}",
            approval_id=approval_id,
            parameters={
                "target_agent_id": agent_id,
                "expected_record_version": expected_record_version,
                "new_manifest_sha256": new_sha,
                "permission_escalation": diff.permission_escalation,
                "department_changed": diff.department_changed,
                "connection_or_provider_changed": diff.connection_or_provider_changed,
                "limits_increased": diff.limits_increased,
            },
            timestamp=timestamp,
            correlation_id=correlation_id or f"corr-agent-upd-{agent_id}",
        )

        def _persist_update() -> AgentMutationOutcome:
            latest = self._repo.get_record(agent_id)
            if latest is None:
                raise AgentNotFoundError("AGENT_NOT_FOUND", f"Agent '{agent_id}' is not registered.")
            if latest.record_version != expected_record_version:
                raise StaleAgentVersionError(
                    "STALE_RECORD_VERSION",
                    (
                        f"Stale update for '{agent_id}': expected record_version={expected_record_version}, "
                        f"found {latest.record_version}."
                    ),
                )

            assert auth_context is not None
            next_revision = latest.active_revision + 1
            prior_ver_map = {
                entry.capability_id: entry for entry in latest.capability_verification
            }
            explicit_validated = set(validated_capabilities) if validated_capabilities is not None else set()
            next_cap_ver = [
                CapabilityVerificationEntry(
                    capability_id=cap_id,
                    declared=True,
                    validated=(
                        True
                        if cap_id in explicit_validated
                        else (
                            prior_ver_map[cap_id].validated
                            if cap_id in prior_ver_map
                            else False
                        )
                    ),
                    observed_in_execution=(
                        prior_ver_map[cap_id].observed_in_execution
                        if cap_id in prior_ver_map
                        else False
                    ),
                )
                for cap_id in new_manifest.capabilities.skills
            ]

            audit_ev = Event(
                event_id=new_event_id(),
                timestamp=timestamp,
                event_type="agent.updated",
                source="governance",
                actor_id=agent_id,
                provider_id=new_manifest.engine.provider_id,
                connection_id=new_manifest.engine.connection_id,
                model_id=new_manifest.engine.model_id,
                correlation_id=policy_req.correlation_id,
                redacted_payload={
                    "target_agent_id": agent_id,
                    "previous_revision": latest.active_revision,
                    "new_revision": next_revision,
                    "manifest_sha256": new_sha,
                    "change_summary": change_summary,
                    "permission_escalation": diff.permission_escalation,
                    "new_unverified_capabilities": diff.new_unverified_capabilities,
                    "updated_by": auth_context.actor_id,
                },
            )
            new_ver_row = AgentManifestVersion(
                agent_id=agent_id,
                manifest_revision=next_revision,
                spec_version=new_manifest.spec_version,
                manifest=new_manifest,
                manifest_sha256=new_sha,
                change_summary=change_summary,
                changed_by=auth_context.actor_id,
                changed_at=timestamp,
                audit_event_id=audit_ev.event_id,
            )
            updated_record = latest.model_copy(
                update={
                    "active_revision": next_revision,
                    "manifest": new_manifest,
                    "manifest_sha256": new_sha,
                    "source_path": source_path if source_path is not None else latest.source_path,
                    "capability_verification": next_cap_ver,
                    "updated_by": auth_context.actor_id,
                    "updated_at": timestamp,
                    "last_audit_event_id": audit_ev.event_id,
                }
            )
            saved = self._repo.update_record(
                updated_record,
                expected_record_version=expected_record_version,
                new_version=new_ver_row,
                audit_event=audit_ev,
            )
            active_runs = self._repo.list_active_run_ids_for_agent(agent_id)
            warnings: list[str] = []
            if active_runs:
                warnings.append(
                    f"Active runs {active_runs} keep pinned manifest revision v{latest.active_revision}."
                )
            return AgentMutationOutcome(
                record=saved,
                version=new_ver_row,
                change_analysis=diff,
                active_run_ids_not_cancelled=active_runs,
                warnings=warnings,
                audit_event_id=audit_ev.event_id,
            )

        _, outcome = self._pm.dispatch_protected_action(policy_req, _persist_update)
        return outcome

    def disable_agent(
        self,
        agent_id: str,
        *,
        expected_record_version: int,
        auth_context: TrustedAuthContext | None,
        timestamp: datetime,
        reason: str,
        approval_id: str | None = None,
        correlation_id: str | None = None,
    ) -> AgentMutationOutcome:
        """Disable Agent for future assignments without deleting history or cancelling active Runs."""
        validate_aware_datetime(timestamp, "timestamp")
        cleaned_reason = ensure_not_secret(reason, "reason")

        current = self._repo.get_record(agent_id)
        if current is None:
            raise AgentNotFoundError("AGENT_NOT_FOUND", f"Agent '{agent_id}' is not registered.")

        # Disabling core.master requires operator approval gate even for human_operator.
        governance_action = (
            "agent.change_permissions" if agent_id == "core.master" else "agent.disable"
        )
        policy_req = PolicyRequest(
            auth_context=auth_context,
            action=governance_action,
            resource=f"agent:{agent_id}",
            approval_id=approval_id,
            parameters={
                "target_agent_id": agent_id,
                "expected_record_version": expected_record_version,
                "reason": cleaned_reason,
            },
            timestamp=timestamp,
            correlation_id=correlation_id or f"corr-agent-dis-{agent_id}",
        )

        def _persist_disable() -> AgentMutationOutcome:
            latest = self._repo.get_record(agent_id)
            if latest is None:
                raise AgentNotFoundError("AGENT_NOT_FOUND", f"Agent '{agent_id}' is not registered.")
            if latest.registration_status == "DISABLED":
                raise AgentStateError(
                    "ALREADY_DISABLED",
                    f"Agent '{agent_id}' is already DISABLED.",
                )
            if latest.record_version != expected_record_version:
                raise StaleAgentVersionError(
                    "STALE_RECORD_VERSION",
                    (
                        f"Stale disable for '{agent_id}': expected record_version={expected_record_version}, "
                        f"found {latest.record_version}."
                    ),
                )

            assert auth_context is not None
            active_runs = self._repo.list_active_run_ids_for_agent(agent_id)
            warnings: list[str] = []
            if active_runs:
                warnings.append(
                    f"Agent '{agent_id}' has active runs {active_runs}; disable prevents new "
                    "assignments only and leaves running executions untouched."
                )

            audit_ev = Event(
                event_id=new_event_id(),
                timestamp=timestamp,
                event_type="agent.disabled",
                source="governance",
                actor_id=agent_id,
                correlation_id=policy_req.correlation_id,
                redacted_payload={
                    "target_agent_id": agent_id,
                    "previous_status": latest.registration_status,
                    "new_status": "DISABLED",
                    "reason": cleaned_reason,
                    "active_runs_untouched": active_runs,
                    "disabled_by": auth_context.actor_id,
                },
            )
            updated = latest.model_copy(
                update={
                    "registration_status": "DISABLED",
                    "updated_by": auth_context.actor_id,
                    "updated_at": timestamp,
                    "disabled_at": timestamp,
                    "disabled_reason": cleaned_reason,
                    "last_audit_event_id": audit_ev.event_id,
                }
            )
            saved = self._repo.update_record(
                updated,
                expected_record_version=expected_record_version,
                new_version=None,
                audit_event=audit_ev,
            )
            return AgentMutationOutcome(
                record=saved,
                active_run_ids_not_cancelled=active_runs,
                warnings=warnings,
                audit_event_id=audit_ev.event_id,
            )

        _, outcome = self._pm.dispatch_protected_action(policy_req, _persist_disable)
        return outcome

    def enable_agent(
        self,
        agent_id: str,
        *,
        expected_record_version: int,
        auth_context: TrustedAuthContext | None,
        timestamp: datetime,
        reason: str,
        approval_id: str | None = None,
        correlation_id: str | None = None,
    ) -> AgentMutationOutcome:
        """Re-enable a DISABLED/VALIDATED/DRAFT Agent to ACTIVE under Governance."""
        validate_aware_datetime(timestamp, "timestamp")
        cleaned_reason = ensure_not_secret(reason, "reason")

        current = self._repo.get_record(agent_id)
        if current is None:
            raise AgentNotFoundError("AGENT_NOT_FOUND", f"Agent '{agent_id}' is not registered.")

        sensitive = _manifest_has_sensitive_initial_grants(current.manifest)
        governance_action = "agent.change_permissions" if sensitive else "agent.enable"
        policy_req = PolicyRequest(
            auth_context=auth_context,
            action=governance_action,
            resource=f"agent:{agent_id}",
            approval_id=approval_id,
            parameters={
                "target_agent_id": agent_id,
                "expected_record_version": expected_record_version,
                "reason": cleaned_reason,
                "sensitive_grants": sensitive,
            },
            timestamp=timestamp,
            correlation_id=correlation_id or f"corr-agent-en-{agent_id}",
        )

        def _persist_enable() -> AgentMutationOutcome:
            latest = self._repo.get_record(agent_id)
            if latest is None:
                raise AgentNotFoundError("AGENT_NOT_FOUND", f"Agent '{agent_id}' is not registered.")
            if latest.registration_status == "ACTIVE":
                raise AgentStateError(
                    "INVALID_STATUS_TRANSITION",
                    f"Agent '{agent_id}' is already ACTIVE.",
                )
            if latest.registration_status == "REJECTED":
                raise AgentStateError(
                    "INVALID_STATUS_TRANSITION",
                    f"REJECTED Agent '{agent_id}' cannot be enabled without registering a valid revision.",
                )
            if latest.record_version != expected_record_version:
                raise StaleAgentVersionError(
                    "STALE_RECORD_VERSION",
                    (
                        f"Stale enable for '{agent_id}': expected record_version={expected_record_version}, "
                        f"found {latest.record_version}."
                    ),
                )

            assert auth_context is not None
            audit_ev = Event(
                event_id=new_event_id(),
                timestamp=timestamp,
                event_type="agent.enabled",
                source="governance",
                actor_id=agent_id,
                correlation_id=policy_req.correlation_id,
                redacted_payload={
                    "target_agent_id": agent_id,
                    "previous_status": latest.registration_status,
                    "new_status": "ACTIVE",
                    "reason": cleaned_reason,
                    "enabled_by": auth_context.actor_id,
                },
            )
            updated = latest.model_copy(
                update={
                    "registration_status": "ACTIVE",
                    "updated_by": auth_context.actor_id,
                    "updated_at": timestamp,
                    "disabled_at": None,
                    "disabled_reason": None,
                    "last_audit_event_id": audit_ev.event_id,
                }
            )
            saved = self._repo.update_record(
                updated,
                expected_record_version=expected_record_version,
                new_version=None,
                audit_event=audit_ev,
            )
            return AgentMutationOutcome(
                record=saved,
                audit_event_id=audit_ev.event_id,
            )

        _, outcome = self._pm.dispatch_protected_action(policy_req, _persist_enable)
        return outcome

    def get_version_history(self, agent_id: str) -> list[AgentManifestVersion]:
        """Return all immutable manifest revisions recorded for ``agent_id``."""
        if self._repo.get_record(agent_id) is None:
            raise AgentNotFoundError("AGENT_NOT_FOUND", f"Agent '{agent_id}' is not registered.")
        return self._repo.list_versions(agent_id)

    def get_manifest_version(
        self, agent_id: str, manifest_revision: int
    ) -> AgentManifestVersion | None:
        """Fetch an exact manifest revision snapshot for Run pinning or inspection."""
        return self._repo.get_version(agent_id, manifest_revision)

    def export_explorer_summaries(
        self, *, include_disabled: bool = True
    ) -> list[AgentExplorerSummary]:
        """Return safe read-only Agent Explorer summaries without fabricating live runtime state."""
        summaries: list[AgentExplorerSummary] = []
        for rec in self._repo.list_records(include_disabled=include_disabled):
            summaries.append(
                AgentExplorerSummary(
                    agent_id=rec.agent_id,
                    name=rec.manifest.name,
                    role=rec.manifest.role,
                    system_role=rec.manifest.system_role,
                    department=rec.manifest.department,
                    registration_status=rec.registration_status,
                    active_revision=rec.active_revision,
                    record_version=rec.record_version,
                    capabilities=list(rec.capability_verification),
                    engine_preferences={
                        "default_type": rec.manifest.engine.default_type,
                        "provider_id": rec.manifest.engine.provider_id,
                        "adapter_id": rec.manifest.engine.adapter_id,
                        "connection_id": rec.manifest.engine.connection_id,
                        "model_tier": rec.manifest.engine.model_tier,
                        "model_id": rec.manifest.engine.model_id,
                        "routing_profile": rec.manifest.engine.routing_profile,
                        "fallback_policy": rec.manifest.engine.fallback_policy,
                    },
                    permission_summary={
                        "fs_read": list(rec.manifest.permissions.fs_read),
                        "fs_write": list(rec.manifest.permissions.fs_write),
                        "network": rec.manifest.permissions.network,
                        "requires_approval": list(rec.manifest.permissions.requires_approval),
                    },
                    updated_at=rec.updated_at,
                )
            )
        return summaries

    def detect_manifest_drift(
        self,
        agent_id: str,
        file_path: str | Path,
        *,
        allowed_root: str | Path,
        enforce_path_convention: bool = True,
    ) -> ManifestDriftReport:
        """Compare on-disk YAML Manifest hash against SQLite Registry without silent mutation."""
        rec = self._repo.get_record(agent_id)
        try:
            disk_manifest = load_agent_manifest_file(
                file_path,
                allowed_root=allowed_root,
                enforce_path_convention=enforce_path_convention,
                allowed_departments=self._allowed_departments,
                known_providers=self._known_providers,
                registered_tools=self._registered_tools,
            )
        except ManifestLoadError as exc:
            if exc.error_code == "FILE_NOT_FOUND":
                return ManifestDriftReport(
                    agent_id=agent_id,
                    status="SOURCE_MISSING",
                    registered_sha256=rec.manifest_sha256 if rec else None,
                    source_path=str(file_path),
                    message=exc.message,
                )
            return ManifestDriftReport(
                agent_id=agent_id,
                status="INVALID_MANIFEST",
                registered_sha256=rec.manifest_sha256 if rec else None,
                source_path=str(file_path),
                message=exc.message,
            )

        file_sha = disk_manifest.canonical_sha256()
        if rec is None:
            return ManifestDriftReport(
                agent_id=disk_manifest.id,
                status="UNREGISTERED_MANIFEST",
                registered_sha256=None,
                file_sha256=file_sha,
                source_path=str(file_path),
                message=f"Manifest '{disk_manifest.id}' on disk has not been registered in SQLite.",
            )

        if disk_manifest.id != agent_id:
            return ManifestDriftReport(
                agent_id=agent_id,
                status="INVALID_MANIFEST",
                registered_sha256=rec.manifest_sha256,
                file_sha256=file_sha,
                source_path=str(file_path),
                message=f"Disk manifest id '{disk_manifest.id}' does not match '{agent_id}'.",
            )

        if file_sha != rec.manifest_sha256:
            return ManifestDriftReport(
                agent_id=agent_id,
                status="DRIFT_DETECTED",
                registered_sha256=rec.manifest_sha256,
                file_sha256=file_sha,
                source_path=str(file_path),
                message=(
                    f"Manifest on disk differs from active SQLite revision v{rec.active_revision}; "
                    "explicit governed update required."
                ),
            )

        return ManifestDriftReport(
            agent_id=agent_id,
            status="IN_SYNC",
            registered_sha256=rec.manifest_sha256,
            file_sha256=file_sha,
            source_path=str(file_path),
            message=f"Disk YAML matches SQLite Registry revision v{rec.active_revision}.",
        )
