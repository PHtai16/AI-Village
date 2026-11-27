"""Fail-closed tests for P07.02 isolated code sandbox, Git inspection, and GovernedMcpBridge."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
from typing import Any

import pytest

from core.events import InMemoryEventJournal
from core.governance import PermissionManager, PolicyEvaluator, TrustedAuthContext, build_default_policy_rules
from core.tools import ToolInvocationRequest, ToolRegistry, ToolRuntime
from core.tools.models import ToolExecutionError
from tools.code import CommandRunResult, DEFAULT_SANDBOX_IMAGE, build_code_tools, build_docker_sandbox_command
from tools.mcp_bridge import GovernedMcpBridge, McpInvocationResult, McpServerConfig

FAKE_TOKEN = "ghp_" + "FakeSecretTokenValue1234567890"


def _utc(minute: int = 0) -> datetime:
    return datetime(2026, 11, 27, 15, minute, 0, tzinfo=timezone.utc)


def _setup_code(
    tmp_path: Path,
    *,
    command_runner: Any = None,
    docker_binary_checker: Any = None,
    mcp_bridge: GovernedMcpBridge | None = None,
) -> tuple[ToolRuntime, Path, TrustedAuthContext]:
    journal = InMemoryEventJournal()
    reg = ToolRegistry(journal=journal)
    for manifest, handler in build_code_tools(
        mcp_bridge=mcp_bridge,
        command_runner=command_runner,
        docker_binary_checker=docker_binary_checker,
    ):
        reg.register(manifest, handler, registered_at=_utc())

    worker = TrustedAuthContext(
        actor_id="it.coder",
        actor_kind="worker_agent",
        auth_source="application_boundary",
        authenticated=True,
        allowed_tools=sorted(reg.registered_tool_ids()),
    )
    perm_mgr = PermissionManager(
        evaluator=PolicyEvaluator(
            rules=build_default_policy_rules(),
            registered_tools=reg.registered_tool_ids(),
        ),
        journal=journal,
    )
    runtime = ToolRuntime(registry=reg, permission_manager=perm_mgr, journal=journal)
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    return runtime, workspace, worker


def test_docker_sandbox_command_hardening_and_execution_states(tmp_path: Path) -> None:
    """Verify hardened Docker CLI flags, truncation, secret redaction, timeout, and fail-closed absence."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    script = workspace / "run.py"
    script.write_text("print('ok')\n", encoding="utf-8")

    cmd = build_docker_sandbox_command(
        real_workspace=workspace,
        operation="python_script",
        rel_target="run.py",
        extra_args=["--verbose"],
    )
    assert cmd[:4] == ["docker", "run", "--rm", "--network=none"]
    assert "--read-only" in cmd
    assert "--cap-drop=ALL" in cmd
    assert "--security-opt=no-new-privileges" in cmd
    assert "--pids-limit=64" in cmd
    assert any(part.endswith(":/workspace:ro") for part in cmd)
    assert DEFAULT_SANDBOX_IMAGE in cmd

    captured_cmds: list[list[str]] = []

    def _ok_and_redact(c: list[str], _t: int, _cwd: Path | None) -> CommandRunResult:
        captured_cmds.append(c)
        return CommandRunResult(
            exit_code=0,
            stdout=("A" * 500) + f" token={FAKE_TOKEN}",
            stderr="",
            duration_ms=42,
        )

    runtime, _, worker = _setup_code(tmp_path, command_runner=_ok_and_redact)
    res_ok = runtime.invoke(
        ToolInvocationRequest(
            auth_context=worker,
            tool_id="tools.code.run_sandbox",
            parameters={"operation": "python_script", "script_path": "run.py", "max_output_chars": 256},
            workspace_root=str(workspace),
            timestamp=_utc(1),
        )
    )
    assert res_ok.status == "SUCCESS"
    assert res_ok.business_verification_status == "UNVERIFIED"
    assert res_ok.data["passed"] is True
    assert res_ok.data["truncated"] is True
    assert len(res_ok.data["stdout"]) == 256
    assert FAKE_TOKEN not in res_ok.data["stdout"]

    # Non-zero exit code reports exit_state='NON_ZERO_EXIT' and redacted stderr
    def _nonzero(_c: list[str], _t: int, _cwd: Path | None) -> CommandRunResult:
        return CommandRunResult(
            exit_code=2,
            stdout="",
            stderr=f"AssertionError token={FAKE_TOKEN}",
            duration_ms=19,
        )

    rt_fail, _, worker2 = _setup_code(tmp_path, command_runner=_nonzero)
    res_nz = rt_fail.invoke(
        ToolInvocationRequest(
            auth_context=worker2,
            tool_id="tools.code.run_sandbox",
            parameters={"operation": "pytest", "test_path": "run.py"},
            workspace_root=str(workspace),
            timestamp=_utc(2),
        )
    )
    assert res_nz.status == "SUCCESS"
    assert res_nz.data["exit_code"] == 2
    assert res_nz.data["passed"] is False
    assert res_nz.data["exit_state"] == "NON_ZERO_EXIT"
    assert FAKE_TOKEN not in res_nz.data["stderr"]

    # Timeout raises TimeoutError -> status TIMEOUT
    def _timeout(_c: list[str], _t: int, _cwd: Path | None) -> CommandRunResult:
        raise subprocess.TimeoutExpired(cmd="docker", timeout=1)

    rt_to, _, worker3 = _setup_code(tmp_path, command_runner=_timeout)
    res_to = rt_to.invoke(
        ToolInvocationRequest(
            auth_context=worker3,
            tool_id="tools.code.run_sandbox",
            parameters={"operation": "python_script", "script_path": "run.py", "timeout_seconds": 1},
            workspace_root=str(workspace),
            timestamp=_utc(3),
        )
    )
    assert res_to.status == "TIMEOUT"
    assert res_to.error_code == "TIMED_OUT"

    # Missing Docker binary / stopped daemon MUST fail closed (never fall back to host execution)
    rt_no_docker, _, worker4 = _setup_code(tmp_path, docker_binary_checker=lambda: None)
    res_no_docker = rt_no_docker.invoke(
        ToolInvocationRequest(
            auth_context=worker4,
            tool_id="tools.code.run_sandbox",
            parameters={"operation": "python_script", "script_path": "run.py"},
            workspace_root=str(workspace),
            timestamp=_utc(4),
        )
    )
    assert res_no_docker.status == "FAILED"
    assert res_no_docker.error_code == "SANDBOX_UNAVAILABLE"

    def _daemon_down(_c: list[str], _t: int, _cwd: Path | None) -> CommandRunResult:
        return CommandRunResult(
            exit_code=125,
            stdout="",
            stderr="docker: error during connect: npipe:////./pipe/dockerDesktopLinuxEngine",
            duration_ms=5,
        )

    rt_daemon_down, _, worker5 = _setup_code(tmp_path, command_runner=_daemon_down)
    res_down = rt_daemon_down.invoke(
        ToolInvocationRequest(
            auth_context=worker5,
            tool_id="tools.code.run_sandbox",
            parameters={"operation": "python_script", "script_path": "run.py"},
            workspace_root=str(workspace),
            timestamp=_utc(5),
        )
    )
    assert res_down.status == "FAILED"
    assert res_down.error_code == "SANDBOX_UNAVAILABLE"


def test_real_git_inspect_and_governed_mcp_bridge(tmp_path: Path) -> None:
    """Test real read-only Git CLI inspection in temp repo plus GovernedMcpBridge security guards."""
    seen_envs: list[dict[str, str]] = []

    def _mock_invoker(
        cfg: McpServerConfig,
        tool_name: str,
        args: dict[str, Any],
        clean_env: dict[str, str],
        _cwd: str | None,
    ) -> McpInvocationResult:
        seen_envs.append(clean_env)
        return McpInvocationResult(
            server_id=cfg.server_id,
            remote_tool_name=tool_name,
            text_content=f"Clean repo status with leaked sk-FakeOpenAISecretKey999999999999",
        )

    bridge = GovernedMcpBridge(invoker=_mock_invoker)
    runtime, repo_ws, worker = _setup_code(tmp_path, mcp_bridge=bridge)

    # Create real git repo inside workspace
    subprocess.run(["git", "init"], cwd=repo_ws, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=repo_ws, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=repo_ws, check=True)
    sample = repo_ws / "app.py"
    sample.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo_ws, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_ws, check=True, capture_output=True)
    sample.write_text("x = 2\n", encoding="utf-8")

    st = runtime.invoke(
        ToolInvocationRequest(
            auth_context=worker,
            tool_id="tools.code.git_inspect",
            parameters={"operation": "status"},
            workspace_root=str(repo_ws),
            timestamp=_utc(1),
        )
    )
    assert st.status == "SUCCESS"
    assert "app.py" in st.data["output"]

    lg = runtime.invoke(
        ToolInvocationRequest(
            auth_context=worker,
            tool_id="tools.code.git_inspect",
            parameters={"operation": "log", "max_commits": 5},
            workspace_root=str(repo_ws),
            timestamp=_utc(2),
        )
    )
    assert lg.status == "SUCCESS"
    assert "initial commit" in lg.data["output"]

    df = runtime.invoke(
        ToolInvocationRequest(
            auth_context=worker,
            tool_id="tools.code.git_inspect",
            parameters={"operation": "diff", "path": "app.py"},
            workspace_root=str(repo_ws),
            timestamp=_utc(3),
        )
    )
    assert df.status == "SUCCESS"
    assert "-x = 1" in df.data["output"] and "+x = 2" in df.data["output"]

    # Reject dangerous flag injection
    bad_flag = runtime.invoke(
        ToolInvocationRequest(
            auth_context=worker,
            tool_id="tools.code.git_inspect",
            parameters={"operation": "diff", "path": "--upload-pack=evil"},
            workspace_root=str(repo_ws),
            timestamp=_utc(4),
        )
    )
    assert bad_flag.status == "FAILED"
    assert bad_flag.error_code == "INVALID_GIT_PATH"

    # MCP mode + env secret stripping + output redaction
    mcp_git = runtime.invoke(
        ToolInvocationRequest(
            auth_context=worker,
            tool_id="tools.code.git_inspect",
            parameters={"operation": "status", "use_mcp": True},
            workspace_root=str(repo_ws),
            timestamp=_utc(5),
        )
    )
    assert mcp_git.status == "SUCCESS"
    assert mcp_git.data["via_mcp"] is True
    assert "FakeOpenAISecretKey" not in mcp_git.data["output"]

    # Direct GovernedMcpBridge guard checks
    with pytest.raises(ToolExecutionError) as bad_srv:
        bridge.call_tool(server_id="unapproved_evil", remote_tool_name="run", arguments={})
    assert bad_srv.value.error_code == "MCP_SERVER_NOT_ALLOWLISTED"

    res_bridge = bridge.call_tool(
        server_id="git",
        remote_tool_name="git_status",
        arguments={"repo_path": str(repo_ws)},
        env_source={
            "PATH": "C:\\Windows\\System32",
            "GEMINI_API_KEY": "AIzaSecretHostKey1234567890",
            "GITHUB_TOKEN": FAKE_TOKEN,
        },
    )
    assert "GEMINI_API_KEY" not in seen_envs[-1]
    assert "GITHUB_TOKEN" not in seen_envs[-1]
    assert "PATH" in seen_envs[-1]
    assert "FakeOpenAISecretKey" not in res_bridge.text_content


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("AI_VILLAGE_RUN_LIVE", "").strip() != "1",
    reason="Live Docker sandbox test runs only when AI_VILLAGE_RUN_LIVE=1 and Docker Desktop is running.",
)
def test_docker_sandbox_live_execution(tmp_path: Path) -> None:
    """Live Docker container smoke test (requires running Docker Desktop and sandbox image)."""
    runtime, ws, worker = _setup_code(tmp_path)
    script = ws / "ok.py"
    script.write_text("print('sandbox_live_ok')\n", encoding="utf-8")
    res = runtime.invoke(
        ToolInvocationRequest(
            auth_context=worker,
            tool_id="tools.code.run_sandbox",
            parameters={"operation": "python_script", "script_path": "ok.py"},
            workspace_root=str(ws),
            timestamp=_utc(1),
        )
    )
    assert res.status == "SUCCESS", res.error_message
    assert "sandbox_live_ok" in res.data["stdout"]
