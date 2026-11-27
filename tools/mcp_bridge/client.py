"""Single governed MCP client bridge enforcing server allowlists, sanitized env, timeouts, and secret redaction."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack
import logging
import os
import sys
import tempfile
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, ConfigDict, Field

from core.contracts.identifiers import sanitize_payload
from core.tools.models import ToolExecutionError
from engines.antigravity.diagnostics import redact_secrets

logger = logging.getLogger(__name__)

_SAFE_ENV_KEYS: tuple[str, ...] = (
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
)


class McpServerConfig(BaseModel):
    """Explicit allowlist entry for an approved stdio MCP server."""

    model_config = ConfigDict(extra="forbid")

    server_id: str = Field(min_length=1, max_length=64)
    command: str = Field(min_length=1, max_length=260)
    args: list[str] = Field(default_factory=list, max_length=32)
    env_allowlist: list[str] = Field(default_factory=list, max_length=16)
    timeout_seconds: int = Field(default=30, ge=1, le=300)


class McpInvocationResult(BaseModel):
    """Redacted result envelope returned by GovernedMcpBridge."""

    model_config = ConfigDict(extra="forbid")

    server_id: str
    remote_tool_name: str
    text_content: str
    structured_content: Any | None = None
    is_error: bool = False


McpInvoker = Callable[
    [McpServerConfig, str, dict[str, Any], dict[str, str], str | None],
    McpInvocationResult,
]


def build_default_mcp_servers() -> dict[str, McpServerConfig]:
    """Return the approved Phase 07 Package B open-source MCP server configurations."""
    return {
        "playwright": McpServerConfig(
            server_id="playwright",
            command="npx",
            args=["-y", "@playwright/mcp@latest", "--headless"],
            timeout_seconds=45,
        ),
        "fetch": McpServerConfig(
            server_id="fetch",
            command="uvx",
            args=["mcp-server-fetch", "--transport", "stdio"],
            timeout_seconds=30,
        ),
        "markitdown": McpServerConfig(
            server_id="markitdown",
            command=sys.executable,
            args=["-m", "markitdown_mcp"],
            timeout_seconds=45,
        ),
        "git": McpServerConfig(
            server_id="git",
            command="uvx",
            args=["mcp-server-git", "--transport", "stdio"],
            timeout_seconds=30,
        ),
    }


def build_sanitized_env(
    env_allowlist: Sequence[str],
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a minimal subprocess environment omitting host credentials and API keys."""
    source = os.environ if base_env is None else base_env
    sanitized: dict[str, str] = {}
    for key in _SAFE_ENV_KEYS:
        if key in source and source[key]:
            sanitized[key] = source[key]
    for extra_key in env_allowlist:
        if extra_key in source and source[extra_key]:
            sanitized[extra_key] = source[extra_key]
    return sanitized


def _extract_mcp_text(result: Any) -> str:
    """Extract redacted textual content from an official MCP CallToolResult."""
    chunks: list[str] = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if isinstance(text, str) and text:
            chunks.append(text)
    return redact_secrets("\n".join(chunks))


async def _invoke_stdio_async(
    config: McpServerConfig,
    remote_tool_name: str,
    arguments: dict[str, Any],
    env: dict[str, str],
    cwd: str | None,
) -> McpInvocationResult:
    """Open an isolated stdio MCP session, call one tool, and redact returned content."""
    server_params = StdioServerParameters(
        command=config.command,
        args=list(config.args),
        env=env,
        cwd=cwd,
    )
    async with AsyncExitStack() as stack:
        read_stream, write_stream = await stack.enter_async_context(stdio_client(server_params))
        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await asyncio.wait_for(session.initialize(), timeout=config.timeout_seconds)
        raw_result = await asyncio.wait_for(
            session.call_tool(
                name=remote_tool_name,
                arguments=arguments,
                read_timeout_seconds=float(config.timeout_seconds),
            ),
            timeout=config.timeout_seconds,
        )
    structured = getattr(raw_result, "structuredContent", None)
    safe_structured = sanitize_payload(structured) if structured is not None else None
    return McpInvocationResult(
        server_id=config.server_id,
        remote_tool_name=remote_tool_name,
        text_content=_extract_mcp_text(raw_result),
        structured_content=safe_structured,
        is_error=bool(getattr(raw_result, "isError", False)),
    )


class GovernedMcpBridge:
    """Executes allowlisted MCP tools with strict environment sanitization and timeouts."""

    def __init__(
        self,
        *,
        allowed_servers: Mapping[str, McpServerConfig] | None = None,
        invoker: McpInvoker | None = None,
    ) -> None:
        """Initialize the MCP bridge with an explicit allowlist and optional test invoker."""
        self._servers = (
            dict(allowed_servers) if allowed_servers is not None else build_default_mcp_servers()
        )
        self._invoker = invoker

    @property
    def allowed_server_ids(self) -> tuple[str, ...]:
        """Return configured allowlisted MCP server IDs in sorted order."""
        return tuple(sorted(self._servers))

    def call_tool(
        self,
        *,
        server_id: str,
        remote_tool_name: str,
        arguments: dict[str, Any],
        cwd: str | None = None,
        env_source: Mapping[str, str] | None = None,
    ) -> McpInvocationResult:
        """Invoke an allowlisted MCP server tool fail-closed with redacted outputs."""
        config = self._servers.get(server_id)
        if config is None:
            raise ToolExecutionError(
                "MCP_SERVER_NOT_ALLOWLISTED",
                f"MCP server '{server_id}' is not in the approved allowlist.",
            )

        clean_env = build_sanitized_env(config.env_allowlist, base_env=env_source)
        effective_cwd = cwd or (tempfile.gettempdir() if server_id == "playwright" else None)
        try:
            if self._invoker is not None:
                raw = self._invoker(config, remote_tool_name, dict(arguments), clean_env, effective_cwd)
                return McpInvocationResult(
                    server_id=raw.server_id,
                    remote_tool_name=raw.remote_tool_name,
                    text_content=redact_secrets(raw.text_content),
                    structured_content=(
                        sanitize_payload(raw.structured_content)
                        if raw.structured_content is not None
                        else None
                    ),
                    is_error=raw.is_error,
                )

            coro = _invoke_stdio_async(
                config,
                remote_tool_name,
                dict(arguments),
                clean_env,
                effective_cwd,
            )
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                result = asyncio.run(coro)
            else:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    result = executor.submit(asyncio.run, coro).result(
                        timeout=config.timeout_seconds + 5
                    )
        except TimeoutError as exc:
            raise ToolExecutionError(
                "TIMED_OUT",
                f"MCP tool '{server_id}.{remote_tool_name}' exceeded {config.timeout_seconds}s timeout.",
                retryable=True,
            ) from exc
        except ToolExecutionError:
            raise
        except Exception as exc:
            logger.warning("MCP invocation failed for %s.%s: %s", server_id, remote_tool_name, exc)
            raise ToolExecutionError(
                "MCP_INVOCATION_FAILED",
                f"MCP server '{server_id}' failed executing '{remote_tool_name}': {exc}",
            ) from exc

        if result.is_error:
            raise ToolExecutionError(
                "MCP_REMOTE_ERROR",
                result.text_content or f"MCP tool '{server_id}.{remote_tool_name}' returned error.",
            )
        return result
