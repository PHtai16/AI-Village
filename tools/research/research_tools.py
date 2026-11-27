"""P07.04 research tools with strict SSRF protection, source timestamps, and Untrusted Data markers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import ipaddress
import socket
from typing import Any
from urllib.parse import urljoin, urlsplit

from ddgs import DDGS
import httpx

from core.contracts.identifiers import new_evidence_id
from core.tools.models import (
    McpToolBinding,
    ToolCostProfile,
    ToolExecutionContext,
    ToolExecutionError,
    ToolHandler,
    ToolHandlerOutput,
    ToolManifest,
)
from engines.antigravity.diagnostics import redact_secrets
from tools.mcp_bridge.client import GovernedMcpBridge

UNTRUSTED_DATA_LABEL = "UNTRUSTED_EXTERNAL_DATA"
_BLOCKED_HOST_SUFFIXES: tuple[str, ...] = (".local", ".internal", ".localhost", ".home.arpa")
_MAX_REDIRECT_HOPS = 5


@dataclass(frozen=True)
class HttpFetchResponse:
    """Normalized HTTP response returned by default httpx fetcher or test stub."""

    url: str
    final_url: str
    status_code: int
    text: str


HttpFetcher = Callable[[str, int], HttpFetchResponse]
SearchBackend = Callable[[str, int], list[dict[str, Any]]]
HostResolver = Callable[[str], list[str]]


def _default_host_resolver(hostname: str) -> list[str]:
    infos = socket.getaddrinfo(hostname, None)
    ips: list[str] = []
    for item in infos:
        addr = item[4][0]
        if isinstance(addr, str):
            ips.append(addr.split("%", 1)[0])
    return ips


def _is_non_public_ip(ip_str: str) -> bool:
    ip_obj = ipaddress.ip_address(ip_str.split("%", 1)[0])
    return (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
        or not ip_obj.is_global
    )


def validate_public_http_url(
    url: str,
    *,
    allow_private_for_test: bool = False,
    resolver: HostResolver | None = None,
) -> str:
    """Validate URL scheme and reject SSRF targets targeting loopback, metadata, or private networks."""
    raw = url.strip()
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ToolExecutionError(
            "SSRF_BLOCKED",
            f"Unsupported URL scheme '{parsed.scheme}'; only public http/https URLs are allowed.",
        )
    if parsed.username is not None or parsed.password is not None:
        raise ToolExecutionError("SSRF_BLOCKED", "Embedded URL credentials are forbidden.")
    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    if not hostname:
        raise ToolExecutionError("INVALID_URL", "URL must contain a valid hostname.")

    if allow_private_for_test:
        return raw

    if hostname == "localhost" or hostname.endswith(_BLOCKED_HOST_SUFFIXES):
        raise ToolExecutionError("SSRF_BLOCKED", f"Private or local host '{hostname}' is blocked.")

    try:
        ip_literal = ipaddress.ip_address(hostname)
    except ValueError:
        ip_literal = None

    if ip_literal is not None:
        if _is_non_public_ip(str(ip_literal)):
            raise ToolExecutionError("SSRF_BLOCKED", f"Non-public IP target '{hostname}' is blocked.")
        return raw

    resolve_fn = resolver or _default_host_resolver
    try:
        resolved_ips = resolve_fn(hostname)
    except OSError:
        resolved_ips = []

    for ip_str in resolved_ips:
        try:
            if _is_non_public_ip(ip_str):
                raise ToolExecutionError(
                    "SSRF_BLOCKED",
                    f"Host '{hostname}' resolved to non-public address '{ip_str}'.",
                )
        except ValueError:
            continue

    return raw


def _default_httpx_fetch(
    url: str,
    timeout_seconds: int,
    *,
    allow_private_for_test: bool = False,
    resolver: HostResolver | None = None,
) -> HttpFetchResponse:
    """Fetch URL with hop-by-hop redirect SSRF validation."""
    current = url
    with httpx.Client(timeout=float(timeout_seconds), follow_redirects=False) as client:
        for _ in range(_MAX_REDIRECT_HOPS + 1):
            resp = client.get(current, headers={"User-Agent": "AI-Village-ResearchTool/1.0"})
            if resp.is_redirect and "location" in resp.headers:
                next_url = urljoin(current, resp.headers["location"])
                current = validate_public_http_url(
                    next_url,
                    allow_private_for_test=allow_private_for_test,
                    resolver=resolver,
                )
                continue
            return HttpFetchResponse(
                url=url,
                final_url=str(resp.url),
                status_code=int(resp.status_code),
                text=resp.text,
            )
    raise ToolExecutionError("TOO_MANY_REDIRECTS", f"Exceeded {_MAX_REDIRECT_HOPS} HTTP redirects.")


def _default_ddgs_search(query: str, max_results: int) -> list[dict[str, Any]]:
    raw = DDGS().text(query, max_results=max_results)
    normalized: list[dict[str, Any]] = []
    for item in raw:
        normalized.append(
            {
                "title": str(item.get("title", "")).strip(),
                "url": str(item.get("href") or item.get("url") or "").strip(),
                "snippet": str(item.get("body") or item.get("snippet") or "").strip(),
            }
        )
    return normalized


def _make_web_fetch_handler(
    *,
    mcp_bridge: GovernedMcpBridge | None = None,
    http_fetcher: HttpFetcher | None = None,
    resolver: HostResolver | None = None,
    allow_private_for_test: bool = False,
) -> ToolHandler:
    def _handle_web_fetch(params: dict[str, Any], ctx: ToolExecutionContext) -> ToolHandlerOutput:
        mode = str(params.get("mode", "httpx"))
        max_chars = int(params.get("max_chars", 50_000))
        validated_url = validate_public_http_url(
            str(params["url"]),
            allow_private_for_test=allow_private_for_test,
            resolver=resolver,
        )
        retrieved_at = datetime.now(timezone.utc).isoformat()

        if mode == "mcp_fetch":
            if mcp_bridge is None:
                raise ToolExecutionError("MCP_BRIDGE_UNAVAILABLE", "GovernedMcpBridge is not configured.")
            mcp_res = mcp_bridge.call_tool(
                server_id="fetch",
                remote_tool_name="fetch",
                arguments={"url": validated_url, "max_length": max_chars},
            )
            raw_text = mcp_res.text_content
            final_url = validated_url
            http_status = 200
        else:
            target_url = (
                f"https://r.jina.ai/{validated_url}"
                if mode == "jina_reader"
                else validated_url
            )
            try:
                if http_fetcher is not None:
                    resp = http_fetcher(target_url, ctx.timeout_seconds)
                else:
                    resp = _default_httpx_fetch(
                        target_url,
                        ctx.timeout_seconds,
                        allow_private_for_test=allow_private_for_test,
                        resolver=resolver,
                    )
            except TimeoutError as exc:
                raise TimeoutError(f"Fetch timed out for '{validated_url}'.") from exc
            except httpx.TimeoutException as exc:
                raise TimeoutError(f"HTTP fetch timed out for '{validated_url}'.") from exc
            except ToolExecutionError:
                raise
            except Exception as exc:
                raise ToolExecutionError("FETCH_FAILED", f"HTTP fetch failed: {exc}") from exc

            validate_public_http_url(
                resp.final_url,
                allow_private_for_test=allow_private_for_test,
                resolver=resolver,
            )
            raw_text = resp.text
            final_url = resp.final_url
            http_status = resp.status_code

        cleaned = redact_secrets(raw_text)
        truncated = len(cleaned) > max_chars
        bounded = cleaned[:max_chars]
        content_sha256 = hashlib.sha256(bounded.encode("utf-8")).hexdigest()
        evd_id = new_evidence_id()

        return ToolHandlerOutput(
            data={
                "url": validated_url,
                "final_url": final_url,
                "mode": mode,
                "http_status": http_status,
                "retrieved_at": retrieved_at,
                "content": bounded,
                "truncated": truncated,
                "content_sha256": content_sha256,
                "content_trust_level": UNTRUSTED_DATA_LABEL,
            },
            evidence_refs=[evd_id],
            untrusted_content=True,
            estimated_cost_usd=None,
        )

    return _handle_web_fetch


def _make_web_search_handler(
    *,
    search_backend: SearchBackend | None = None,
    allow_private_for_test: bool = False,
) -> ToolHandler:
    backend = search_backend or _default_ddgs_search

    def _handle_web_search(params: dict[str, Any], _ctx: ToolExecutionContext) -> ToolHandlerOutput:
        query = str(params["query"]).strip()
        max_results = int(params.get("max_results", 5))
        retrieved_at = datetime.now(timezone.utc).isoformat()
        try:
            raw_items = backend(query, max_results)
        except Exception as exc:
            raise ToolExecutionError("SEARCH_FAILED", f"Web search failed: {exc}") from exc

        cleaned_results: list[dict[str, str]] = []
        for item in raw_items[:max_results]:
            raw_url = str(item.get("url", "")).strip()
            if not raw_url:
                continue
            try:
                safe_url = validate_public_http_url(
                    raw_url,
                    allow_private_for_test=allow_private_for_test,
                )
            except ToolExecutionError:
                continue
            cleaned_results.append(
                {
                    "title": redact_secrets(str(item.get("title", "")))[:250],
                    "url": safe_url,
                    "snippet": redact_secrets(str(item.get("snippet", "")))[:800],
                }
            )

        evd_id = new_evidence_id()
        return ToolHandlerOutput(
            data={
                "query": query,
                "retrieved_at": retrieved_at,
                "results": cleaned_results,
                "content_trust_level": UNTRUSTED_DATA_LABEL,
            },
            evidence_refs=[evd_id],
            untrusted_content=True,
            estimated_cost_usd=None,
        )

    return _handle_web_search


def build_research_tools(
    *,
    mcp_bridge: GovernedMcpBridge | None = None,
    http_fetcher: HttpFetcher | None = None,
    search_backend: SearchBackend | None = None,
    resolver: HostResolver | None = None,
    allow_private_for_test: bool = False,
) -> list[tuple[ToolManifest, ToolHandler]]:
    """Return canonical manifests and handlers for P07.04 SSRF-guarded external research tools."""
    external_cost = ToolCostProfile(
        unit="external_call",
        estimated_unit_cost_usd=None,
        notes="Tool transport fee unavailable from free external/MCP endpoints",
    )
    return [
        (
            ToolManifest(
                id="tools.research.web_fetch",
                name="Fetch Public Web Resource",
                version="1.0.0",
                category="research",
                description="Retrieve public HTTP/HTTPS content via httpx, Jina Reader, or mcp-server-fetch with SSRF blocking.",
                capabilities=["web_research", "source_verification"],
                input_schema={
                    "type": "object",
                    "required": ["url"],
                    "additionalProperties": False,
                    "properties": {
                        "url": {"type": "string", "minLength": 8, "maxLength": 2048},
                        "mode": {"type": "string", "enum": ["httpx", "jina_reader", "mcp_fetch"]},
                        "max_chars": {"type": "integer", "minimum": 100, "maximum": 200_000},
                    },
                },
                output_schema={
                    "type": "object",
                    "required": [
                        "url",
                        "final_url",
                        "mode",
                        "http_status",
                        "retrieved_at",
                        "content",
                        "truncated",
                        "content_sha256",
                        "content_trust_level",
                    ],
                    "additionalProperties": False,
                    "properties": {
                        "url": {"type": "string"},
                        "final_url": {"type": "string"},
                        "mode": {"type": "string"},
                        "http_status": {"type": "integer"},
                        "retrieved_at": {"type": "string"},
                        "content": {"type": "string"},
                        "truncated": {"type": "boolean"},
                        "content_sha256": {"type": "string"},
                        "content_trust_level": {"type": "string"},
                    },
                },
                permissions_required=["network.fetch"],
                approval_required=False,
                risk_level="MEDIUM",
                execution_mode="approved_external",
                timeout_seconds=45,
                cost_profile=external_cost,
                safe_retry=True,
                mcp_binding=McpToolBinding(server_id="fetch", remote_tool_name="fetch"),
            ),
            _make_web_fetch_handler(
                mcp_bridge=mcp_bridge,
                http_fetcher=http_fetcher,
                resolver=resolver,
                allow_private_for_test=allow_private_for_test,
            ),
        ),
        (
            ToolManifest(
                id="tools.research.web_search",
                name="Search Public Web",
                version="1.0.0",
                category="research",
                description="Query public web search via DuckDuckGo (ddgs) returning untrusted source citations.",
                capabilities=["web_research", "search_discovery"],
                input_schema={
                    "type": "object",
                    "required": ["query"],
                    "additionalProperties": False,
                    "properties": {
                        "query": {"type": "string", "minLength": 2, "maxLength": 500},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                },
                output_schema={
                    "type": "object",
                    "required": ["query", "retrieved_at", "results", "content_trust_level"],
                    "additionalProperties": False,
                    "properties": {
                        "query": {"type": "string"},
                        "retrieved_at": {"type": "string"},
                        "results": {"type": "array"},
                        "content_trust_level": {"type": "string"},
                    },
                },
                permissions_required=["network.search"],
                approval_required=False,
                risk_level="MEDIUM",
                execution_mode="approved_external",
                timeout_seconds=30,
                cost_profile=external_cost,
                safe_retry=True,
            ),
            _make_web_search_handler(
                search_backend=search_backend,
                allow_private_for_test=allow_private_for_test,
            ),
        ),
    ]
