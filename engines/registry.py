"""Deterministic, policy-safe registry for selecting engines and provider connections."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import threading
from typing import Callable

from engines.connections import ConnectionRegistry, EngineConnectionRef
from engines.contract import (
    EngineAdapter,
    EngineError,
    EngineEvent,
    EngineRequest,
    EngineResult,
    EngineTelemetry,
)

logger = logging.getLogger(__name__)


class EngineRegistry:
    """Registry resolving requests to adapters and connections without implicit fallback."""

    def __init__(self, connections: ConnectionRegistry | None = None) -> None:
        """Initialize the registry with an optional ``ConnectionRegistry``."""
        self._lock = threading.RLock()
        self._adapters: dict[str, EngineAdapter] = {}
        self._provider_defaults: dict[str, str] = {}
        self._connections: ConnectionRegistry = connections or ConnectionRegistry()

    @property
    def connections(self) -> ConnectionRegistry:
        """Return the ``ConnectionRegistry`` managed alongside this adapter registry."""
        return self._connections

    def register_adapter(
        self,
        adapter: EngineAdapter,
        *,
        set_default_for_provider: bool = False,
    ) -> None:
        """Register an ``EngineAdapter`` instance by its unique ``adapter_id``."""
        with self._lock:
            if adapter.adapter_id in self._adapters:
                raise ValueError(f"Adapter '{adapter.adapter_id}' is already registered.")
            self._adapters[adapter.adapter_id] = adapter
            if set_default_for_provider or adapter.provider_id not in self._provider_defaults:
                self._provider_defaults[adapter.provider_id] = adapter.adapter_id

    def register_connection(self, connection: EngineConnectionRef) -> None:
        """Delegate connection registration to the attached ``ConnectionRegistry``."""
        self._connections.register(connection)

    def get_adapter(self, adapter_id: str) -> EngineAdapter | None:
        """Return a registered adapter by ``adapter_id``, or None."""
        with self._lock:
            return self._adapters.get(adapter_id)

    def list_adapters(self) -> list[dict[str, str]]:
        """List non-secret metadata descriptors for every registered adapter."""
        with self._lock:
            return [
                {
                    "adapter_id": adapter.adapter_id,
                    "provider_id": adapter.provider_id,
                    "platform_kind": adapter.platform_kind,
                }
                for adapter in sorted(self._adapters.values(), key=lambda item: item.adapter_id)
            ]

    def resolve(
        self,
        request: EngineRequest,
    ) -> tuple[EngineAdapter | None, EngineConnectionRef | None, EngineError | None]:
        """Resolve adapter and connection fail-closed without silent cross-provider fallback."""
        connection: EngineConnectionRef | None = None
        target_provider_id = request.provider_id
        target_adapter_id = request.adapter_id

        if request.connection_id is not None:
            connection = self._connections.get(request.connection_id)
            if connection is None:
                return (
                    None,
                    None,
                    EngineError(
                        error_code="CONNECTION_NOT_FOUND",
                        error_source="credential",
                        message=f"Connection '{request.connection_id}' is not registered.",
                        retryable=False,
                        provider_id=request.provider_id,
                        connection_id=request.connection_id,
                    ),
                )
            if not connection.enabled:
                return (
                    None,
                    connection,
                    EngineError(
                        error_code="CONNECTION_DISABLED",
                        error_source="credential",
                        message=f"Connection '{connection.connection_id}' is disabled by operator policy.",
                        retryable=False,
                        provider_id=connection.provider_id,
                        connection_id=connection.connection_id,
                    ),
                )
            if target_provider_id is not None and target_provider_id != connection.provider_id:
                return (
                    None,
                    connection,
                    EngineError(
                        error_code="CONNECTION_NOT_FOUND",
                        error_source="validation",
                        message=(
                            f"Connection '{connection.connection_id}' belongs to provider "
                            f"'{connection.provider_id}', not requested provider '{target_provider_id}'."
                        ),
                        retryable=False,
                        provider_id=target_provider_id,
                        connection_id=connection.connection_id,
                    ),
                )
            target_provider_id = connection.provider_id
            if target_adapter_id is None:
                target_adapter_id = connection.adapter_id

        with self._lock:
            adapter: EngineAdapter | None = None
            if target_adapter_id is not None:
                adapter = self._adapters.get(target_adapter_id)
                if adapter is None:
                    return (
                        None,
                        connection,
                        EngineError(
                            error_code="EXECUTION_BLOCKED",
                            error_source="capability",
                            message=f"Requested adapter '{target_adapter_id}' is not registered.",
                            retryable=False,
                            provider_id=target_provider_id,
                            connection_id=request.connection_id,
                        ),
                    )
                if target_provider_id is not None and adapter.provider_id != target_provider_id:
                    return (
                        None,
                        connection,
                        EngineError(
                            error_code="EXECUTION_BLOCKED",
                            error_source="validation",
                            message=(
                                f"Adapter '{adapter.adapter_id}' serves provider "
                                f"'{adapter.provider_id}', not '{target_provider_id}'."
                            ),
                            retryable=False,
                            provider_id=target_provider_id,
                            connection_id=request.connection_id,
                        ),
                    )
            elif target_provider_id is not None:
                default_id = self._provider_defaults.get(target_provider_id)
                adapter = self._adapters.get(default_id) if default_id else None
                if adapter is None:
                    return (
                        None,
                        connection,
                        EngineError(
                            error_code="EXECUTION_BLOCKED",
                            error_source="capability",
                            message=f"No adapter registered for provider '{target_provider_id}'.",
                            retryable=False,
                            provider_id=target_provider_id,
                            connection_id=request.connection_id,
                        ),
                    )
            else:
                return (
                    None,
                    connection,
                    EngineError(
                        error_code="EXECUTION_BLOCKED",
                        error_source="validation",
                        message="Request must specify provider_id, adapter_id, or connection_id.",
                        retryable=False,
                    ),
                )

        return adapter, connection, None

    def execute(
        self,
        request: EngineRequest,
        *,
        on_event: Callable[[EngineEvent], None] | None = None,
    ) -> EngineResult:
        """Resolve target adapter/connection and execute ``request`` fail-closed."""
        adapter, connection, resolve_error = self.resolve(request)
        if resolve_error is not None or adapter is None:
            provider_id = (
                connection.provider_id
                if connection is not None
                else (request.provider_id or "unresolved")
            )
            adapter_id = (
                request.adapter_id
                or (connection.adapter_id if connection is not None else None)
                or "unresolved"
            )
            now = datetime.now(timezone.utc)
            return EngineResult(
                request_id=request.request_id,
                provider_id=provider_id,
                adapter_id=adapter_id,
                connection_id=request.connection_id,
                model_id=request.model_id,
                platform_kind="model_api",
                status="BLOCKED",
                response_text=None,
                session_reference=request.session_reference,
                started_at=now,
                finished_at=now,
                duration_ms=0,
                telemetry=EngineTelemetry(
                    duration_ms=0,
                    telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                    telemetry_source="unavailable",
                ),
                events=[],
                error=resolve_error,
                capability_evidence={},
            )

        logger.info(
            "Executing request %s via provider=%s adapter=%s connection=%s",
            request.request_id,
            adapter.provider_id,
            adapter.adapter_id,
            connection.connection_id if connection else None,
        )
        return adapter.execute(request, connection=connection, on_event=on_event)
