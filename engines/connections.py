"""Multi-connection BYOK metadata contracts and backend-only credential resolution."""

from __future__ import annotations

from abc import ABC, abstractmethod
import os
import re
import threading
from typing import Any, Literal, Mapping
from urllib.parse import parse_qsl, urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from engines.antigravity.diagnostics import redact_secrets

_SNAKE_ID_RE = re.compile(r"^[a-z0-9_]+$")
_ENV_VAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SENSITIVE_QUERY_KEYS: frozenset[str] = frozenset(
    {"key", "api_key", "apikey", "token", "access_token", "secret", "password"}
)
_PLACEHOLDER_PREFIXES: tuple[str, ...] = ("your_", "replace_", "<", "placeholder")
_RAW_SECRET_PREFIXES: tuple[str, ...] = ("aiza", "sk-", "ghp_")


def _ensure_not_raw_secret(value: str, field_name: str) -> str:
    """Reject strings matching known raw API key or credential formats."""
    cleaned = value.strip()
    lowered = cleaned.lower()
    if redact_secrets(cleaned) != cleaned or lowered.startswith(_RAW_SECRET_PREFIXES):
        raise ValueError(
            f"{field_name} must store a non-secret reference identifier, never a raw credential."
        )
    return cleaned


class EngineConnectionRef(BaseModel):
    """Non-secret connection record separating provider, project scope, and credential reference."""

    model_config = ConfigDict(extra="forbid")

    connection_id: str = Field(min_length=2, max_length=64)
    provider_id: str = Field(min_length=2, max_length=64)
    adapter_id: str | None = None
    display_name: str | None = None
    account_ref: str | None = None
    project_ref: str | None = None
    credential_ref: str | None = None
    endpoint_url: str | None = None
    default_model_id: str | None = None
    enabled: bool = True
    priority: int = Field(default=100, ge=0, le=10000)
    quota_scope_note: Literal[
        "unknown",
        "project_shared",
        "account_shared",
        "connection_scoped",
    ] = "unknown"

    @field_validator("connection_id", "provider_id")
    @classmethod
    def _validate_required_ids(cls, value: str, info: Any) -> str:
        """Validate required snake_case identifiers."""
        cleaned = _ensure_not_raw_secret(value, info.field_name)
        if not _SNAKE_ID_RE.fullmatch(cleaned):
            raise ValueError(f"{info.field_name} must match ^[a-z0-9_]+$.")
        return cleaned

    @field_validator("adapter_id")
    @classmethod
    def _validate_optional_adapter_id(cls, value: str | None) -> str | None:
        """Validate optional snake_case adapter override."""
        if value is None:
            return None
        cleaned = _ensure_not_raw_secret(value, "adapter_id")
        if not _SNAKE_ID_RE.fullmatch(cleaned):
            raise ValueError("adapter_id must match ^[a-z0-9_]+$.")
        return cleaned

    @field_validator("credential_ref", "display_name", "account_ref", "project_ref")
    @classmethod
    def _reject_secret_values(cls, value: str | None, info: Any) -> str | None:
        """Ensure human-facing and reference fields never contain raw keys."""
        if value is None:
            return None
        cleaned = _ensure_not_raw_secret(value, info.field_name)
        if not cleaned:
            raise ValueError(f"{info.field_name} must not be blank when provided.")
        return cleaned

    @field_validator("endpoint_url")
    @classmethod
    def _validate_safe_endpoint_url(cls, value: str | None) -> str | None:
        """Reject HTTP URLs embedding API keys or tokens in query parameters."""
        if value is None:
            return None
        cleaned = _ensure_not_raw_secret(value, "endpoint_url")
        parsed = urlparse(cleaned)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("endpoint_url must be a valid http or https URL.")
        for q_key, _ in parse_qsl(parsed.query, keep_blank_values=True):
            if q_key.lower() in _SENSITIVE_QUERY_KEYS:
                raise ValueError(
                    f"endpoint_url must never embed credential parameter '{q_key}' in URL."
                )
        return cleaned.rstrip("/")

    def to_safe_summary(self) -> dict[str, Any]:
        """Return safe non-secret metadata suitable for logs or diagnostics."""
        masked_ref = (
            f"ref:{self.credential_ref[:4]}***"
            if self.credential_ref and len(self.credential_ref) > 4
            else ("configured" if self.credential_ref else "none")
        )
        return {
            "connection_id": self.connection_id,
            "provider_id": self.provider_id,
            "adapter_id": self.adapter_id,
            "project_ref": self.project_ref,
            "account_ref": self.account_ref,
            "default_model_id": self.default_model_id,
            "enabled": self.enabled,
            "priority": self.priority,
            "quota_scope_note": self.quota_scope_note,
            "credential_ref_summary": masked_ref,
        }


class ProviderConnectionGroup(BaseModel):
    """Validated group of multiple connections registered under one provider."""

    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(min_length=2, max_length=64)
    connections: list[EngineConnectionRef] = Field(default_factory=list)

    @field_validator("provider_id")
    @classmethod
    def _validate_provider_id(cls, value: str) -> str:
        """Ensure provider_id is safe snake_case."""
        cleaned = _ensure_not_raw_secret(value, "provider_id")
        if not _SNAKE_ID_RE.fullmatch(cleaned):
            raise ValueError("provider_id must match ^[a-z0-9_]+$.")
        return cleaned

    @model_validator(mode="after")
    def _validate_group_consistency(self) -> ProviderConnectionGroup:
        """Ensure connection IDs are unique and match the parent provider_id."""
        seen_ids: set[str] = set()
        for conn in self.connections:
            if conn.provider_id != self.provider_id:
                raise ValueError(
                    f"Connection '{conn.connection_id}' has provider_id '{conn.provider_id}', "
                    f"expected '{self.provider_id}'."
                )
            if conn.connection_id in seen_ids:
                raise ValueError(f"Duplicate connection_id '{conn.connection_id}' in group.")
            seen_ids.add(conn.connection_id)
        return self


class CredentialResolver(ABC):
    """Backend boundary resolving opaque credential_ref handles to raw secrets."""

    @abstractmethod
    def is_configured(self, credential_ref: str) -> bool:
        """Return True if a non-placeholder secret exists for ``credential_ref``."""

    @abstractmethod
    def resolve_secret(self, credential_ref: str) -> str | None:
        """Resolve raw credential string for backend transport headers only."""

    def mask_reference(self, credential_ref: str | None) -> str:
        """Return a safe masked indicator without disclosing secret bytes."""
        if not credential_ref:
            return "unconfigured"
        configured = self.is_configured(credential_ref)
        state = "configured" if configured else "missing"
        return f"ref:{credential_ref} ({state})"


class EnvCredentialResolver(CredentialResolver):
    """Temporary backend resolver mapping ``credential_ref`` handles to environment variables."""

    def __init__(
        self,
        *,
        ref_to_env: Mapping[str, str] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Initialize the resolver with explicit reference-to-env mappings.

        Args:
            ref_to_env: Mapping from opaque ``credential_ref`` strings to env variable names.
            environ: Optional mapping overriding ``os.environ`` for isolated testing.
        """
        self._ref_to_env: dict[str, str] = {}
        if ref_to_env:
            for ref_key, env_name in ref_to_env.items():
                if not _ENV_VAR_NAME_RE.fullmatch(env_name):
                    raise ValueError(
                        f"Environment variable name '{env_name}' is invalid for ref '{ref_key}'."
                    )
                self._ref_to_env[ref_key] = env_name
        self._environ: Mapping[str, str] = os.environ if environ is None else environ

    def _lookup_env_name(self, credential_ref: str) -> str | None:
        """Resolve the configured environment variable name for ``credential_ref``."""
        if not credential_ref:
            return None
        if credential_ref in self._ref_to_env:
            return self._ref_to_env[credential_ref]
        if credential_ref.startswith("env:"):
            candidate = credential_ref.removeprefix("env:")
            if _ENV_VAR_NAME_RE.fullmatch(candidate):
                return candidate
        return None

    @staticmethod
    def _is_usable_secret(raw_value: str | None) -> str | None:
        """Return cleaned secret string when non-empty and not a placeholder."""
        if raw_value is None:
            return None
        cleaned = raw_value.strip()
        if not cleaned:
            return None
        lowered = cleaned.lower()
        if lowered.startswith(_PLACEHOLDER_PREFIXES):
            return None
        return cleaned

    def is_configured(self, credential_ref: str) -> bool:
        """Return True if the backing environment variable holds a non-placeholder value."""
        return self.resolve_secret(credential_ref) is not None

    def resolve_secret(self, credential_ref: str) -> str | None:
        """Read secret value from the configured backend mapping for adapter calls."""
        env_name = self._lookup_env_name(credential_ref)
        if env_name is None:
            return None
        return self._is_usable_secret(self._environ.get(env_name))

    def __repr__(self) -> str:
        """Return a safe representation concealing secret values."""
        return f"EnvCredentialResolver(refs={sorted(self._ref_to_env.keys())})"


class ConnectionRegistry:
    """Thread-safe in-memory registry of multi-provider connection metadata."""

    def __init__(self) -> None:
        """Initialize an empty connection registry guarded by an ``RLock``."""
        self._lock = threading.RLock()
        self._connections: dict[str, EngineConnectionRef] = {}

    def register(self, connection: EngineConnectionRef) -> None:
        """Register or raise on duplicate ``connection_id``."""
        with self._lock:
            if connection.connection_id in self._connections:
                raise ValueError(
                    f"Connection '{connection.connection_id}' is already registered."
                )
            self._connections[connection.connection_id] = connection

    def register_group(self, group: ProviderConnectionGroup) -> None:
        """Register all connections in a validated ``ProviderConnectionGroup``."""
        with self._lock:
            for conn in group.connections:
                if conn.connection_id in self._connections:
                    raise ValueError(
                        f"Connection '{conn.connection_id}' is already registered."
                    )
            for conn in group.connections:
                self._connections[conn.connection_id] = conn

    def get(self, connection_id: str) -> EngineConnectionRef | None:
        """Retrieve a registered connection by ``connection_id``."""
        with self._lock:
            return self._connections.get(connection_id)

    def list_connections(
        self,
        *,
        provider_id: str | None = None,
        enabled_only: bool = False,
    ) -> list[EngineConnectionRef]:
        """List registered connections sorted by priority ascending then connection_id."""
        with self._lock:
            items = [
                conn
                for conn in self._connections.values()
                if (provider_id is None or conn.provider_id == provider_id)
                and (not enabled_only or conn.enabled)
            ]
        return sorted(items, key=lambda item: (item.priority, item.connection_id))

    def has_shared_project_scope(self, conn_id_a: str, conn_id_b: str) -> bool:
        """Return True if both connections belong to the same provider and non-null project."""
        with self._lock:
            conn_a = self._connections.get(conn_id_a)
            conn_b = self._connections.get(conn_id_b)
        if conn_a is None or conn_b is None:
            return False
        if conn_a.provider_id != conn_b.provider_id:
            return False
        return (
            conn_a.project_ref is not None
            and conn_b.project_ref is not None
            and conn_a.project_ref == conn_b.project_ref
        )

    def has_independent_quota_proof(self, conn_id_a: str, conn_id_b: str) -> bool:
        """Return False unless independent quota isolation is verified from provider billing."""
        with self._lock:
            _ = self._connections.get(conn_id_a)
            _ = self._connections.get(conn_id_b)
        return False
