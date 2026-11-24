"""Framework-independent AgentRuntime abstraction and CustomAgentRuntime adapter."""

from __future__ import annotations

from core.runtime.application import AgentWorkflowRunner, execute_agent_workflow
from core.runtime.contract import (
    AgentRuntime,
    AgentRuntimeArtifactRef,
    AgentRuntimeError,
    AgentRuntimeEvent,
    AgentRuntimeInfo,
    AgentRuntimeRequest,
    AgentRuntimeResult,
    AgentRuntimeTelemetry,
    ArtifactAcceptanceSpec,
    ExecutionRouting,
    WorkspaceScope,
)
from core.runtime.custom_runtime import CustomAgentRuntime

__all__ = [
    "AgentRuntime",
    "AgentRuntimeArtifactRef",
    "AgentRuntimeError",
    "AgentRuntimeEvent",
    "AgentRuntimeInfo",
    "AgentRuntimeRequest",
    "AgentRuntimeResult",
    "AgentRuntimeTelemetry",
    "AgentWorkflowRunner",
    "ArtifactAcceptanceSpec",
    "CustomAgentRuntime",
    "ExecutionRouting",
    "WorkspaceScope",
    "execute_agent_workflow",
]
