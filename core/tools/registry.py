"""Central Tool Registry managing manifests, handlers, status lifecycle, and capability discovery."""

from __future__ import annotations

from datetime import datetime, timezone
import logging

from core.contracts.event import Event
from core.contracts.identifiers import new_event_id
from core.events.journal import EventJournal
from core.tools.models import (
    ToolCategory,
    ToolExecutionError,
    ToolHandler,
    ToolManifest,
    ToolRepository,
    ToolStatus,
)

logger = logging.getLogger(__name__)


class ToolRegistry:
    """In-memory and optionally SQLite-backed Registry separating declarations from runtime calls."""

    def __init__(
        self,
        *,
        repository: ToolRepository | None = None,
        journal: EventJournal | None = None,
    ) -> None:
        """Initialize an empty ToolRegistry bound to optional repository and EventJournal."""
        self._repo = repository
        self._journal = journal
        self._manifests: dict[str, ToolManifest] = {}
        self._handlers: dict[str, ToolHandler] = {}

    def register(
        self,
        manifest: ToolManifest,
        handler: ToolHandler,
        *,
        registered_at: datetime | None = None,
    ) -> ToolManifest:
        """Validate, register, persist, and audit a canonical ToolManifest with its handler."""
        ts = registered_at or datetime.now(timezone.utc)
        if ts.tzinfo is None or ts.utcoffset() is None:
            raise ToolExecutionError("INVALID_TIMESTAMP", "registered_at must be timezone-aware.")

        stored_manifest = manifest
        if self._repo is not None:
            stored_manifest = self._repo.upsert_tool(manifest, registered_at=ts, updated_at=ts)

        self._manifests[stored_manifest.id] = stored_manifest
        self._handlers[stored_manifest.id] = handler

        if self._journal is not None:
            self._journal.append(
                Event(
                    event_id=new_event_id(),
                    timestamp=ts,
                    event_type="tool.registered",
                    source="tool_runtime",
                    correlation_id=f"toolreg-{stored_manifest.id}",
                    redacted_payload={
                        "tool_id": stored_manifest.id,
                        "version": stored_manifest.version,
                        "category": stored_manifest.category,
                        "status": stored_manifest.status,
                        "risk_level": stored_manifest.risk_level,
                        "execution_mode": stored_manifest.execution_mode,
                        "mcp_server_id": stored_manifest.mcp_binding.server_id if stored_manifest.mcp_binding else None,
                    },
                )
            )
        logger.info("Registered tool %s v%s (%s)", stored_manifest.id, stored_manifest.version, stored_manifest.status)
        return stored_manifest

    def get(self, tool_id: str) -> ToolManifest | None:
        """Return latest registered manifest from durable repository if present, else memory."""
        if self._repo is not None:
            found = self._repo.get_tool(tool_id)
            if found is not None:
                self._manifests[tool_id] = found
                return found
        return self._manifests.get(tool_id)

    def get_handler(self, tool_id: str) -> ToolHandler | None:
        """Return bound Python handler for ``tool_id`` if registered in this runtime."""
        return self._handlers.get(tool_id)

    def list_manifests(
        self,
        *,
        category: ToolCategory | None = None,
        capability: str | None = None,
        status: ToolStatus | None = None,
    ) -> list[ToolManifest]:
        """List registered tools matching optional category, capability, or status filters."""
        if self._repo is not None:
            items = self._repo.list_tools(category=category, status=status)
        else:
            items = sorted(self._manifests.values(), key=lambda m: m.id)
            if category is not None:
                items = [m for m in items if m.category == category]
            if status is not None:
                items = [m for m in items if m.status == status]
        if capability is not None:
            items = [m for m in items if capability in m.capabilities]
        return items

    def set_status(
        self,
        tool_id: str,
        status: ToolStatus,
        *,
        updated_at: datetime | None = None,
    ) -> ToolManifest:
        """Update availability status (`AVAILABLE`, `DEGRADED`, `DISABLED`) in Registry."""
        ts = updated_at or datetime.now(timezone.utc)
        existing = self.get(tool_id)
        if existing is None:
            raise ToolExecutionError("TOOL_NOT_FOUND", f"Tool '{tool_id}' is not registered.")
        if self._repo is not None:
            updated = self._repo.set_tool_status(tool_id, status, updated_at=ts)
        else:
            updated = existing.model_copy(update={"status": status})
        self._manifests[tool_id] = updated
        logger.info("Updated tool %s status to %s", tool_id, status)
        return updated

    def registered_tool_ids(self) -> set[str]:
        """Return all registered Tool IDs for Governance evaluator synchronization."""
        if self._repo is not None:
            return {m.id for m in self._repo.list_tools()} | set(self._manifests.keys())
        return set(self._manifests.keys())
