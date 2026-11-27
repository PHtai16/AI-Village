"""P07.03 governed Playwright & Playwright-MCP browser automation tools with screenshot evidence."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

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
from tools.files.file_tools import _require_validated_path, sha256_bytes
from tools.mcp_bridge.client import GovernedMcpBridge
from tools.office.office_tools import _atomic_write_via_temp, _check_overwrite
from tools.research.research_tools import UNTRUSTED_DATA_LABEL, HostResolver, validate_public_http_url


@dataclass(frozen=True)
class BrowserRunResult:
    """Captured browser session result from Playwright or injected test runner."""

    final_url: str
    title: str
    extracted_text: str
    screenshot_bytes: bytes | None
    action_log: list[dict[str, Any]]


BrowserRunner = Callable[[dict[str, Any], ToolExecutionContext], BrowserRunResult]


def _default_playwright_session_runner(
    params: dict[str, Any],
    ctx: ToolExecutionContext,
    *,
    allow_local_test_urls: bool = False,
) -> BrowserRunResult:
    url = str(params["url"])
    action = str(params.get("action", "open"))
    timeout_ms = int(ctx.timeout_seconds * 1000)
    action_log: list[dict[str, Any]] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            page = context.new_page()
            response = page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            final_url = page.url or url
            validate_public_http_url(final_url, allow_private_for_test=allow_local_test_urls)
            action_log.append(
                {
                    "step": 1,
                    "action": "navigate",
                    "url": final_url,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "status": response.status if response is not None else 200,
                }
            )
            title = page.title() or ""
            extracted_text = ""
            if action == "extract_text":
                extracted_text = page.inner_text("body")[:20_000]
                action_log.append(
                    {
                        "step": 2,
                        "action": "extract_text",
                        "url": final_url,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "status": "OK",
                    }
                )
            screenshot_bytes: bytes | None = None
            if action == "screenshot" or params.get("screenshot_path"):
                screenshot_bytes = page.screenshot(type="png", full_page=True)
                action_log.append(
                    {
                        "step": len(action_log) + 1,
                        "action": "screenshot",
                        "url": final_url,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "status": "OK",
                    }
                )
            return BrowserRunResult(
                final_url=final_url,
                title=title,
                extracted_text=extracted_text,
                screenshot_bytes=screenshot_bytes,
                action_log=action_log,
            )
        except PlaywrightTimeoutError as exc:
            raise TimeoutError(f"Browser navigation exceeded {ctx.timeout_seconds}s timeout.") from exc
        finally:
            browser.close()


def _default_playwright_interact_runner(
    params: dict[str, Any],
    ctx: ToolExecutionContext,
    *,
    allow_local_test_urls: bool = False,
) -> BrowserRunResult:
    url = str(params["url"])
    steps: list[dict[str, Any]] = params["steps"]
    timeout_ms = int(ctx.timeout_seconds * 1000)
    action_log: list[dict[str, Any]] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            page = context.new_page()
            response = page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            final_url = page.url or url
            validate_public_http_url(final_url, allow_private_for_test=allow_local_test_urls)
            action_log.append(
                {
                    "step": 1,
                    "action": "navigate",
                    "url": final_url,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "status": response.status if response is not None else 200,
                }
            )
            for idx, step in enumerate(steps, start=2):
                op = str(step["action"])
                selector = str(step["selector"])
                if op == "click":
                    page.locator(selector).first.click(timeout=timeout_ms)
                elif op == "fill":
                    page.locator(selector).first.fill(str(step.get("value", "")), timeout=timeout_ms)
                elif op == "submit":
                    page.locator(selector).first.press("Enter", timeout=timeout_ms)
                final_url = page.url or final_url
                validate_public_http_url(final_url, allow_private_for_test=allow_local_test_urls)
                action_log.append(
                    {
                        "step": idx,
                        "action": op,
                        "selector": selector,
                        "url": final_url,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "status": "OK",
                    }
                )
            screenshot_bytes = (
                page.screenshot(type="png", full_page=True) if params.get("screenshot_path") else None
            )
            return BrowserRunResult(
                final_url=final_url,
                title=page.title() or "",
                extracted_text=page.inner_text("body")[:10_000],
                screenshot_bytes=screenshot_bytes,
                action_log=action_log,
            )
        except PlaywrightTimeoutError as exc:
            raise TimeoutError(f"Browser interaction exceeded {ctx.timeout_seconds}s timeout.") from exc
        finally:
            browser.close()


def _save_screenshot_if_present(
    *,
    ctx: ToolExecutionContext,
    screenshot_rel_arg: Any,
    overwrite: bool,
    screenshot_bytes: bytes | None,
) -> tuple[str | None, str | None, list[dict[str, Any]]]:
    if not isinstance(screenshot_rel_arg, str) or not screenshot_rel_arg.strip():
        return None, None, []
    if screenshot_bytes is None:
        raise ToolExecutionError("SCREENSHOT_MISSING", "Browser runner produced no PNG screenshot bytes.")
    _, target, rel_posix = _require_validated_path(ctx, screenshot_rel_arg, action="files.write")
    _check_overwrite(target, rel_posix, overwrite)
    written = _atomic_write_via_temp(target, lambda dest: dest.write_bytes(screenshot_bytes))
    digest = sha256_bytes(written)
    artifact = {
        "path": rel_posix,
        "sha256": digest,
        "size_bytes": len(written),
        "content_type": "image/png",
    }
    return rel_posix, digest, [artifact]


def _make_session_handler(
    *,
    mcp_bridge: GovernedMcpBridge | None = None,
    session_runner: BrowserRunner | None = None,
    resolver: HostResolver | None = None,
    allow_local_test_urls: bool = False,
) -> ToolHandler:
    def _handle_session(params: dict[str, Any], ctx: ToolExecutionContext) -> ToolHandlerOutput:
        validated_url = validate_public_http_url(
            str(params["url"]),
            allow_private_for_test=allow_local_test_urls,
            resolver=resolver,
        )
        action = str(params.get("action", "open"))
        screenshot_arg = params.get("screenshot_path")
        overwrite = bool(params.get("overwrite", False))
        use_mcp = bool(params.get("use_mcp", False))

        if action == "screenshot" and (not isinstance(screenshot_arg, str) or not screenshot_arg.strip()):
            raise ToolExecutionError(
                "MISSING_SCREENSHOT_PATH",
                "action='screenshot' requires workspace screenshot_path.",
            )
        if isinstance(screenshot_arg, str) and screenshot_arg.strip():
            _, target_preview, rel_preview = _require_validated_path(ctx, screenshot_arg, action="files.write")
            _check_overwrite(target_preview, rel_preview, overwrite)

        if use_mcp:
            if mcp_bridge is None:
                raise ToolExecutionError("MCP_BRIDGE_UNAVAILABLE", "GovernedMcpBridge is not configured.")
            mcp_res = mcp_bridge.call_tool(
                server_id="playwright",
                remote_tool_name="browser_navigate",
                arguments={"url": validated_url},
            )
            evd_id = new_evidence_id()
            return ToolHandlerOutput(
                data={
                    "url": validated_url,
                    "final_url": validated_url,
                    "action": action,
                    "title": "",
                    "extracted_text": redact_secrets(mcp_res.text_content)[:20_000],
                    "screenshot_path": None,
                    "screenshot_sha256": None,
                    "action_log": [
                        {
                            "step": 1,
                            "action": "mcp_browser_navigate",
                            "url": validated_url,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "status": "OK",
                        }
                    ],
                    "via_mcp": True,
                    "content_trust_level": UNTRUSTED_DATA_LABEL,
                },
                evidence_refs=[evd_id],
                untrusted_content=True,
                estimated_cost_usd=None,
            )

        res = (
            session_runner(dict(params), ctx)
            if session_runner is not None
            else _default_playwright_session_runner(
                params,
                ctx,
                allow_local_test_urls=allow_local_test_urls,
            )
        )
        saved_rel, saved_sha, artifacts = _save_screenshot_if_present(
            ctx=ctx,
            screenshot_rel_arg=screenshot_arg,
            overwrite=overwrite,
            screenshot_bytes=res.screenshot_bytes,
        )
        evd_id = new_evidence_id()
        return ToolHandlerOutput(
            data={
                "url": validated_url,
                "final_url": res.final_url,
                "action": action,
                "title": redact_secrets(res.title),
                "extracted_text": redact_secrets(res.extracted_text),
                "screenshot_path": saved_rel,
                "screenshot_sha256": saved_sha,
                "action_log": res.action_log,
                "via_mcp": False,
                "content_trust_level": UNTRUSTED_DATA_LABEL,
            },
            artifacts=artifacts,
            evidence_refs=[evd_id],
            untrusted_content=True,
            estimated_cost_usd=None,
        )

    return _handle_session


def _make_interact_handler(
    *,
    interact_runner: BrowserRunner | None = None,
    resolver: HostResolver | None = None,
    allow_local_test_urls: bool = False,
) -> ToolHandler:
    def _handle_interact(params: dict[str, Any], ctx: ToolExecutionContext) -> ToolHandlerOutput:
        validated_url = validate_public_http_url(
            str(params["url"]),
            allow_private_for_test=allow_local_test_urls,
            resolver=resolver,
        )
        screenshot_arg = params.get("screenshot_path")
        overwrite = bool(params.get("overwrite", False))
        if isinstance(screenshot_arg, str) and screenshot_arg.strip():
            _, target_preview, rel_preview = _require_validated_path(ctx, screenshot_arg, action="files.write")
            _check_overwrite(target_preview, rel_preview, overwrite)

        res = (
            interact_runner(dict(params), ctx)
            if interact_runner is not None
            else _default_playwright_interact_runner(
                params,
                ctx,
                allow_local_test_urls=allow_local_test_urls,
            )
        )
        saved_rel, saved_sha, artifacts = _save_screenshot_if_present(
            ctx=ctx,
            screenshot_rel_arg=screenshot_arg,
            overwrite=overwrite,
            screenshot_bytes=res.screenshot_bytes,
        )
        evd_id = new_evidence_id()
        return ToolHandlerOutput(
            data={
                "url": validated_url,
                "final_url": res.final_url,
                "title": redact_secrets(res.title),
                "extracted_text": redact_secrets(res.extracted_text),
                "steps_executed": len(params["steps"]),
                "screenshot_path": saved_rel,
                "screenshot_sha256": saved_sha,
                "action_log": res.action_log,
                "content_trust_level": UNTRUSTED_DATA_LABEL,
            },
            artifacts=artifacts,
            evidence_refs=[evd_id],
            untrusted_content=True,
            estimated_cost_usd=None,
        )

    return _handle_interact


def build_browser_tools(
    *,
    mcp_bridge: GovernedMcpBridge | None = None,
    session_runner: BrowserRunner | None = None,
    interact_runner: BrowserRunner | None = None,
    resolver: HostResolver | None = None,
    allow_local_test_urls: bool = False,
) -> list[tuple[ToolManifest, ToolHandler]]:
    """Return canonical manifests and handlers for P07.03 governed Playwright browser tools."""
    cost = ToolCostProfile(
        unit="external_call",
        estimated_unit_cost_usd=None,
        notes="Local headless Chromium execution; no external API fee",
    )
    return [
        (
            ToolManifest(
                id="tools.browser.session",
                name="Open Web Page and Capture Screenshot",
                version="1.0.0",
                category="browser",
                description="Open public web URLs via Playwright or @playwright/mcp, extract text, and capture PNG evidence.",
                capabilities=["browser_inspection", "screenshot_capture"],
                input_schema={
                    "type": "object",
                    "required": ["url"],
                    "additionalProperties": False,
                    "properties": {
                        "url": {"type": "string", "minLength": 8, "maxLength": 2048},
                        "action": {"type": "string", "enum": ["open", "screenshot", "extract_text"]},
                        "screenshot_path": {"type": "string", "minLength": 1, "maxLength": 260},
                        "overwrite": {"type": "boolean"},
                        "use_mcp": {"type": "boolean"},
                    },
                },
                output_schema={
                    "type": "object",
                    "required": [
                        "url",
                        "final_url",
                        "action",
                        "title",
                        "extracted_text",
                        "screenshot_path",
                        "screenshot_sha256",
                        "action_log",
                        "via_mcp",
                        "content_trust_level",
                    ],
                    "additionalProperties": False,
                    "properties": {
                        "url": {"type": "string"},
                        "final_url": {"type": "string"},
                        "action": {"type": "string"},
                        "title": {"type": "string"},
                        "extracted_text": {"type": "string"},
                        "screenshot_path": {"type": ["string", "null"]},
                        "screenshot_sha256": {"type": ["string", "null"]},
                        "action_log": {"type": "array"},
                        "via_mcp": {"type": "boolean"},
                        "content_trust_level": {"type": "string"},
                    },
                },
                permissions_required=["browser.open", "browser.screenshot"],
                approval_required=False,
                risk_level="MEDIUM",
                execution_mode="approved_external",
                timeout_seconds=60,
                cost_profile=cost,
                safe_retry=True,
                mcp_binding=McpToolBinding(server_id="playwright", remote_tool_name="browser_navigate"),
            ),
            _make_session_handler(
                mcp_bridge=mcp_bridge,
                session_runner=session_runner,
                resolver=resolver,
                allow_local_test_urls=allow_local_test_urls,
            ),
        ),
        (
            ToolManifest(
                id="tools.browser.interact",
                name="Interact With Web Page (Approval Required)",
                version="1.0.0",
                category="browser",
                description="Perform mutating UI interactions (click, fill, submit) in Chromium under mandatory Approval Gate.",
                capabilities=["browser_automation", "screenshot_capture"],
                input_schema={
                    "type": "object",
                    "required": ["url", "steps"],
                    "additionalProperties": False,
                    "properties": {
                        "url": {"type": "string", "minLength": 8, "maxLength": 2048},
                        "steps": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 20,
                            "items": {
                                "type": "object",
                                "required": ["action", "selector"],
                                "additionalProperties": False,
                                "properties": {
                                    "action": {"type": "string", "enum": ["click", "fill", "submit"]},
                                    "selector": {"type": "string", "minLength": 1, "maxLength": 200},
                                    "value": {"type": "string", "maxLength": 2000},
                                },
                            },
                        },
                        "screenshot_path": {"type": "string", "minLength": 1, "maxLength": 260},
                        "overwrite": {"type": "boolean"},
                    },
                },
                output_schema={
                    "type": "object",
                    "required": [
                        "url",
                        "final_url",
                        "title",
                        "extracted_text",
                        "steps_executed",
                        "screenshot_path",
                        "screenshot_sha256",
                        "action_log",
                        "content_trust_level",
                    ],
                    "additionalProperties": False,
                    "properties": {
                        "url": {"type": "string"},
                        "final_url": {"type": "string"},
                        "title": {"type": "string"},
                        "extracted_text": {"type": "string"},
                        "steps_executed": {"type": "integer"},
                        "screenshot_path": {"type": ["string", "null"]},
                        "screenshot_sha256": {"type": ["string", "null"]},
                        "action_log": {"type": "array"},
                        "content_trust_level": {"type": "string"},
                    },
                },
                permissions_required=["browser.interact", "browser.open"],
                approval_required=True,
                risk_level="HIGH",
                execution_mode="approved_external",
                timeout_seconds=60,
                cost_profile=cost,
                safe_retry=False,
            ),
            _make_interact_handler(
                interact_runner=interact_runner,
                resolver=resolver,
                allow_local_test_urls=allow_local_test_urls,
            ),
        ),
    ]
