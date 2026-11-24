"""Opt-in live integration tests for P02.06 Universal Engine Contract (Tests A-E)."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Iterator

import pytest

from engines import (
    AntigravityEngineAdapter,
    EngineConnectionRef,
    EngineRegistry,
    EngineRequest,
    EngineResult,
    EnvCredentialResolver,
    GoogleGeminiAdapter,
    ProviderConnectionGroup,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_LIVE = os.environ.get("AI_VILLAGE_RUN_LIVE", "").strip() == "1"


def _resolve_live_gemini_ref() -> tuple[str, str] | None:
    """Return configured (credential_ref, env_var) for Gemini only if non-placeholder exists."""
    resolver = EnvCredentialResolver(
        ref_to_env={
            "env:GEMINI_API_KEY": "GEMINI_API_KEY",
            "env:GOOGLE_API_KEY": "GOOGLE_API_KEY",
        }
    )
    for env_name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        ref = f"env:{env_name}"
        if resolver.is_configured(ref):
            return ref, env_name
    return None


def _run_neutral_task(registry: EngineRegistry, request: EngineRequest) -> EngineResult:
    """Application helper invoking any configured provider purely through ``EngineRegistry``."""
    return registry.execute(request)


@contextmanager
def _isolated_workspace_dir(prefix: str) -> Iterator[str]:
    """Create a temporary workspace under ``workspace/`` and clean up with brief Windows retry."""
    workspace_root = REPO_ROOT / "workspace"
    workspace_root.mkdir(exist_ok=True)
    tmp_path = Path(tempfile.mkdtemp(dir=str(workspace_root), prefix=prefix))
    try:
        yield str(tmp_path)
    finally:
        for _ in range(10):
            if not tmp_path.exists():
                break
            shutil.rmtree(tmp_path, ignore_errors=True)
            if tmp_path.exists():
                time.sleep(0.3)


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Live provider contract tests run only when AI_VILLAGE_RUN_LIVE=1.",
)
def test_live_antigravity_through_common_contract() -> None:
    """TEST A: Invoke real Antigravity CLI through Universal Engine Contract and EngineRegistry."""
    with _isolated_workspace_dir("p02_06_live_agy_") as tmp_dir:
        registry = EngineRegistry()
        registry.register_adapter(AntigravityEngineAdapter(default_workspace_path=tmp_dir))

        request = EngineRequest(
            provider_id="antigravity",
            prompt="Reply with exact text: AI_VILLAGE_P02_06_AGY_OK",
            timeout_seconds=60,
            execution_mode="text",
        )
        result = _run_neutral_task(registry, request)

    assert result.status == "SUCCEEDED", f"Antigravity common contract failed: {result.error}"
    assert result.provider_id == "antigravity"
    assert result.adapter_id == "antigravity_cli"
    assert result.platform_kind == "agent_execution_platform"
    assert result.response_text is not None
    assert "AI_VILLAGE_P02_06_AGY_OK" in result.response_text
    assert result.duration_ms > 0
    assert result.capability_evidence["native_tool_execution"] == "SUPPORTED"


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Live provider contract tests run only when AI_VILLAGE_RUN_LIVE=1.",
)
def test_live_alternative_api_provider_gemini() -> None:
    """TEST B: Call real Google Gemini API via the shared EngineRequest contract."""
    detected = _resolve_live_gemini_ref()
    if detected is None:
        pytest.skip(
            "BLOCKED: No authorized Google Gemini credential (GEMINI_API_KEY / GOOGLE_API_KEY) "
            "is configured in environment."
        )
    cred_ref, _ = detected
    model_id = os.environ.get("AI_VILLAGE_GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"

    registry = EngineRegistry()
    registry.register_adapter(GoogleGeminiAdapter(credential_resolver=EnvCredentialResolver()))
    registry.register_connection(
        EngineConnectionRef(
            connection_id="gemini_live_primary",
            provider_id="google_gemini",
            adapter_id="gemini_api",
            project_ref="operator_configured_project",
            credential_ref=cred_ref,
            default_model_id=model_id,
            enabled=True,
        )
    )

    result = _run_neutral_task(
        registry,
        EngineRequest(
            connection_id="gemini_live_primary",
            prompt="Reply with exact text: AI_VILLAGE_P02_06_GEMINI_OK",
            timeout_seconds=30,
        ),
    )
    assert result.status == "SUCCEEDED", f"Gemini live call failed: {result.error}"
    assert result.provider_id == "google_gemini"
    assert result.connection_id == "gemini_live_primary"
    assert result.response_text is not None
    assert "AI_VILLAGE_P02_06_GEMINI_OK" in result.response_text
    assert result.duration_ms > 0
    assert result.observed_tool_names == []


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Live provider contract tests run only when AI_VILLAGE_RUN_LIVE=1.",
)
def test_live_provider_switching_without_core_changes() -> None:
    """TEST C: Switch between Antigravity and Google Gemini using one shared caller."""
    detected = _resolve_live_gemini_ref()
    if detected is None:
        pytest.skip(
            "BLOCKED: Live provider switching requires an authorized GEMINI_API_KEY / GOOGLE_API_KEY."
        )
    cred_ref, _ = detected
    model_id = os.environ.get("AI_VILLAGE_GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"

    with _isolated_workspace_dir("p02_06_switch_") as tmp_dir:
        registry = EngineRegistry()
        registry.register_adapter(AntigravityEngineAdapter(default_workspace_path=tmp_dir))
        registry.register_adapter(GoogleGeminiAdapter(credential_resolver=EnvCredentialResolver()))
        registry.register_connection(
            EngineConnectionRef(
                connection_id="gemini_live_primary",
                provider_id="google_gemini",
                adapter_id="gemini_api",
                credential_ref=cred_ref,
                default_model_id=model_id,
                enabled=True,
            )
        )

        res_agy = _run_neutral_task(
            registry,
            EngineRequest(
                provider_id="antigravity",
                prompt="Reply with exact token: SWITCH_AGY_OK",
                timeout_seconds=60,
            ),
        )
        res_gem = _run_neutral_task(
            registry,
            EngineRequest(
                connection_id="gemini_live_primary",
                prompt="Reply with exact token: SWITCH_GEMINI_OK",
                timeout_seconds=30,
            ),
        )

    assert res_agy.status == "SUCCEEDED" and res_gem.status == "SUCCEEDED"
    assert res_agy.provider_id == "antigravity"
    assert res_gem.provider_id == "google_gemini"


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Live provider contract tests run only when AI_VILLAGE_RUN_LIVE=1.",
)
def test_live_unsupported_capability_fail_closed() -> None:
    """TEST D: Native file/workspace request to pure Model API fails closed with zero side effects."""
    with _isolated_workspace_dir("p02_06_cap_") as tmp_dir:
        registry = EngineRegistry()
        registry.register_adapter(GoogleGeminiAdapter())
        registry.register_connection(
            EngineConnectionRef(
                connection_id="gemini_primary",
                provider_id="google_gemini",
                adapter_id="gemini_api",
                project_ref="google_project_a",
                credential_ref="env:GEMINI_API_KEY",
                default_model_id="gemini-2.5-flash",
                enabled=True,
            )
        )

        result = _run_neutral_task(
            registry,
            EngineRequest(
                connection_id="gemini_primary",
                prompt="Create proof.txt in workspace",
                workspace_path=tmp_dir,
                capabilities_required=["native_tool_execution", "workspace_access"],
                execution_mode="accept-edits",
            ),
        )
        created_files = list(Path(tmp_dir).iterdir())

    assert result.status == "BLOCKED"
    assert result.error is not None
    assert result.error.error_code == "TOOL_UNSUPPORTED_BY_PROVIDER"
    assert result.observed_tool_names == []
    assert created_files == []


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Live provider contract tests run only when AI_VILLAGE_RUN_LIVE=1.",
)
def test_live_multi_connection_metadata_isolation() -> None:
    """TEST E: Multi-connection Gemini fixture isolates refs, prevents quota assumptions, hides keys."""
    group = ProviderConnectionGroup(
        provider_id="google_gemini",
        connections=[
            EngineConnectionRef(
                connection_id="gemini_primary",
                provider_id="google_gemini",
                adapter_id="gemini_api",
                project_ref="google_project_a",
                credential_ref="secret_ref_primary",
                default_model_id="gemini-2.5-flash",
                enabled=True,
                priority=1,
                quota_scope_note="project_shared",
            ),
            EngineConnectionRef(
                connection_id="gemini_secondary",
                provider_id="google_gemini",
                adapter_id="gemini_api",
                project_ref="google_project_a",
                credential_ref="secret_ref_secondary",
                default_model_id="gemini-2.5-flash",
                enabled=False,
                priority=2,
                quota_scope_note="project_shared",
            ),
        ],
    )
    registry = EngineRegistry()
    registry.register_adapter(GoogleGeminiAdapter())
    registry.connections.register_group(group)

    assert registry.connections.has_shared_project_scope("gemini_primary", "gemini_secondary") is True
    assert registry.connections.has_independent_quota_proof("gemini_primary", "gemini_secondary") is False

    disabled_res = _run_neutral_task(
        registry,
        EngineRequest(connection_id="gemini_secondary", prompt="Hello"),
    )
    assert disabled_res.status == "BLOCKED"
    assert disabled_res.error is not None
    assert disabled_res.error.error_code == "CONNECTION_DISABLED"
    assert "secret_ref_secondary" not in repr(disabled_res)
