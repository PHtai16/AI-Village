"""Central bootstrap helper registering all Phase 07 department-independent tools."""

from __future__ import annotations

from datetime import datetime

from core.tools.models import ToolManifest
from core.tools.registry import ToolRegistry
from tools.browser import BrowserRunner, build_browser_tools
from tools.code import CommandRunner, build_code_tools
from tools.files import build_file_tools
from tools.mcp_bridge import GovernedMcpBridge
from tools.office import build_office_tools
from tools.research import HttpFetcher, SearchBackend, build_research_tools


def register_phase07_tools(
    registry: ToolRegistry,
    *,
    mcp_bridge: GovernedMcpBridge | None = None,
    command_runner: CommandRunner | None = None,
    http_fetcher: HttpFetcher | None = None,
    search_backend: SearchBackend | None = None,
    browser_session_runner: BrowserRunner | None = None,
    browser_interact_runner: BrowserRunner | None = None,
    allow_private_urls_for_test: bool = False,
    registered_at: datetime | None = None,
) -> list[ToolManifest]:
    """Register all 15 canonical Phase 07 tools into ``registry`` and return manifests."""
    bridge = mcp_bridge or GovernedMcpBridge()
    bundles = [
        *build_file_tools(),
        *build_office_tools(mcp_bridge=bridge),
        *build_code_tools(mcp_bridge=bridge, command_runner=command_runner),
        *build_research_tools(
            mcp_bridge=bridge,
            http_fetcher=http_fetcher,
            search_backend=search_backend,
            allow_private_for_test=allow_private_urls_for_test,
        ),
        *build_browser_tools(
            mcp_bridge=bridge,
            session_runner=browser_session_runner,
            interact_runner=browser_interact_runner,
            allow_local_test_urls=allow_private_urls_for_test,
        ),
    ]
    registered: list[ToolManifest] = []
    for manifest, handler in bundles:
        registered.append(registry.register(manifest, handler, registered_at=registered_at))
    return registered
