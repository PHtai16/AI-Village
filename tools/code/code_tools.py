"""P07.02 isolated Docker sandbox code execution and read-only Git inspection tools."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
from time import perf_counter
from typing import Any

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
from tools.files.file_tools import _require_validated_path
from tools.mcp_bridge.client import GovernedMcpBridge, build_sanitized_env

DEFAULT_SANDBOX_IMAGE = "ai-village-sandbox:py312"


@dataclass(frozen=True)
class CommandRunResult:
    """Captured subprocess execution metrics returned by host or injected runner."""

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


CommandRunner = Callable[[list[str], int, Path | None], CommandRunResult]


def _default_command_runner(cmd: list[str], timeout_seconds: int, cwd: Path | None) -> CommandRunResult:
    started = perf_counter()
    env = build_sanitized_env([])
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    elapsed_ms = max(0, int((perf_counter() - started) * 1000))
    return CommandRunResult(
        exit_code=int(proc.returncode),
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        duration_ms=elapsed_ms,
    )


def build_docker_sandbox_command(
    *,
    real_workspace: Path,
    operation: str,
    rel_target: str,
    extra_args: list[str],
    sandbox_image: str = DEFAULT_SANDBOX_IMAGE,
) -> list[str]:
    """Build hardened non-networked, read-only Docker CLI arguments without host fallback."""
    inner_cmd = ["python", rel_target, *extra_args] if operation == "python_script" else [
        "pytest",
        rel_target,
        *extra_args,
    ]
    return [
        "docker",
        "run",
        "--rm",
        "--network=none",
        "--cpus=1",
        "--memory=512m",
        "--pids-limit=64",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
        "-v",
        f"{real_workspace}:/workspace:ro",
        "-w",
        "/workspace",
        sandbox_image,
        *inner_cmd,
    ]


def _looks_like_docker_daemon_failure(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(
        hint in lowered
        for hint in (
            "cannot connect to the docker daemon",
            "dockerdesktoplinuxengine",
            "error during connect",
            "is the docker daemon running",
            "unable to find image",
        )
    )


def _truncate_and_redact(text: str, max_chars: int) -> tuple[str, bool]:
    cleaned = redact_secrets(text)
    if len(cleaned) <= max_chars:
        return cleaned, False
    return cleaned[:max_chars], True


def _make_run_sandbox_handler(
    *,
    command_runner: CommandRunner | None = None,
    docker_binary_checker: Callable[[], str | None] | None = None,
    sandbox_image: str = DEFAULT_SANDBOX_IMAGE,
) -> ToolHandler:
    runner = command_runner or _default_command_runner
    which_docker = docker_binary_checker or (lambda: shutil.which("docker"))

    def _handle_run_sandbox(params: dict[str, Any], ctx: ToolExecutionContext) -> ToolHandlerOutput:
        operation = str(params["operation"])
        raw_target = params.get("script_path") if operation == "python_script" else params.get("test_path")
        if not isinstance(raw_target, str) or not raw_target.strip():
            raise ToolExecutionError(
                "INVALID_SANDBOX_TARGET",
                f"Operation '{operation}' requires a valid script_path or test_path inside workspace.",
            )

        real_root, target, rel_posix = _require_validated_path(ctx, raw_target, action="files.read")
        if not target.exists():
            raise ToolExecutionError("TARGET_NOT_FOUND", f"Target '{rel_posix}' does not exist in workspace.")
        if operation == "python_script" and not target.is_file():
            raise ToolExecutionError("TARGET_NOT_FILE", f"Script '{rel_posix}' must be a regular file.")

        extra_args = [str(arg) for arg in params.get("args", [])]
        for arg in extra_args:
            if "\x00" in arg:
                raise ToolExecutionError("INVALID_ARGUMENT", "Sandbox arguments must not contain NUL bytes.")

        if command_runner is None and which_docker() is None:
            raise ToolExecutionError(
                "SANDBOX_UNAVAILABLE",
                "Docker CLI is not installed; refusing fail-closed to execute code on host.",
            )

        effective_timeout = min(int(params.get("timeout_seconds", ctx.timeout_seconds)), ctx.timeout_seconds)
        max_chars = int(params.get("max_output_chars", 8000))
        docker_cmd = build_docker_sandbox_command(
            real_workspace=real_root,
            operation=operation,
            rel_target=rel_posix,
            extra_args=extra_args,
            sandbox_image=sandbox_image,
        )

        try:
            run_res = runner(docker_cmd, effective_timeout, None)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"Sandbox execution exceeded timeout of {effective_timeout}s."
            ) from exc
        except FileNotFoundError as exc:
            raise ToolExecutionError(
                "SANDBOX_UNAVAILABLE",
                "Docker executable was not found; refusing to run code on host.",
            ) from exc

        if run_res.exit_code == 125 and _looks_like_docker_daemon_failure(run_res.stderr):
            raise ToolExecutionError(
                "SANDBOX_UNAVAILABLE",
                f"Docker sandbox daemon unavailable: {redact_secrets(run_res.stderr)[:300]}",
            )
        if run_res.exit_code == 1 and _looks_like_docker_daemon_failure(run_res.stderr):
            raise ToolExecutionError(
                "SANDBOX_UNAVAILABLE",
                f"Docker sandbox failed to start: {redact_secrets(run_res.stderr)[:300]}",
            )

        out_clean, out_trunc = _truncate_and_redact(run_res.stdout, max_chars)
        err_clean, err_trunc = _truncate_and_redact(run_res.stderr, max_chars)
        return ToolHandlerOutput(
            data={
                "operation": operation,
                "target_path": rel_posix,
                "exit_code": run_res.exit_code,
                "exit_state": "SUCCESS" if run_res.exit_code == 0 else "NON_ZERO_EXIT",
                "passed": run_res.exit_code == 0,
                "stdout": out_clean,
                "stderr": err_clean,
                "truncated": out_trunc or err_trunc,
                "duration_ms": run_res.duration_ms,
                "timed_out": False,
                "sandbox_image": sandbox_image,
            },
            evidence_refs=[new_evidence_id()],
        )

    return _handle_run_sandbox


def _make_git_inspect_handler(
    *,
    mcp_bridge: GovernedMcpBridge | None = None,
    command_runner: CommandRunner | None = None,
) -> ToolHandler:
    runner = command_runner or _default_command_runner

    def _handle_git_inspect(params: dict[str, Any], ctx: ToolExecutionContext) -> ToolHandlerOutput:
        if not ctx.workspace_root:
            raise ToolExecutionError("MISSING_WORKSPACE_CONTEXT", "workspace_root is required.")
        real_root = Path(os.path.realpath(ctx.workspace_root))
        operation = str(params["operation"])
        max_commits = int(params.get("max_commits", 10))
        max_chars = int(params.get("max_output_chars", 8000))
        use_mcp = bool(params.get("use_mcp", False))

        rel_target: str | None = None
        if isinstance(params.get("path"), str) and params["path"].strip():
            raw_path = params["path"].strip()
            if raw_path.startswith("-"):
                raise ToolExecutionError("INVALID_GIT_PATH", "Git inspection path must not start with '-'.")
            _, _, rel_target = _require_validated_path(ctx, raw_path, action="files.read")

        if use_mcp:
            if mcp_bridge is None:
                raise ToolExecutionError("MCP_BRIDGE_UNAVAILABLE", "GovernedMcpBridge is not configured.")
            if operation == "status":
                remote_name = "git_status"
                args: dict[str, Any] = {"repo_path": str(real_root)}
            elif operation == "log":
                remote_name = "git_log"
                args = {"repo_path": str(real_root), "max_count": max_commits}
            else:
                remote_name = "git_diff_unstaged"
                args = {"repo_path": str(real_root)}
            mcp_res = mcp_bridge.call_tool(
                server_id="git",
                remote_tool_name=remote_name,
                arguments=args,
                cwd=str(real_root),
            )
            out_clean, out_trunc = _truncate_and_redact(mcp_res.text_content, max_chars)
            return ToolHandlerOutput(
                data={
                    "operation": operation,
                    "exit_code": 0,
                    "output": out_clean,
                    "truncated": out_trunc,
                    "via_mcp": True,
                }
            )

        if operation == "status":
            cmd = ["git", "-C", str(real_root), "status", "--short", "--branch"]
        elif operation == "diff":
            cmd = ["git", "-C", str(real_root), "diff"]
        else:
            cmd = ["git", "-C", str(real_root), "log", f"-n{max_commits}", "--oneline"]
        if rel_target and rel_target != ".":
            cmd.extend(["--", rel_target])

        try:
            res = runner(cmd, ctx.timeout_seconds, real_root)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"git {operation} exceeded {ctx.timeout_seconds}s timeout.") from exc
        if res.exit_code != 0:
            raise ToolExecutionError(
                "GIT_INSPECT_FAILED",
                f"git {operation} exited {res.exit_code}: {res.stderr or res.stdout}",
            )
        out_clean, out_trunc = _truncate_and_redact(res.stdout, max_chars)
        return ToolHandlerOutput(
            data={
                "operation": operation,
                "exit_code": res.exit_code,
                "output": out_clean,
                "truncated": out_trunc,
                "via_mcp": False,
            }
        )

    return _handle_git_inspect


def build_code_tools(
    *,
    mcp_bridge: GovernedMcpBridge | None = None,
    command_runner: CommandRunner | None = None,
    docker_binary_checker: Callable[[], str | None] | None = None,
    sandbox_image: str = DEFAULT_SANDBOX_IMAGE,
) -> list[tuple[ToolManifest, ToolHandler]]:
    """Return canonical manifests and handlers for P07.02 isolated code execution and Git inspection."""
    cost = ToolCostProfile(unit="free_local", estimated_unit_cost_usd=0.0)
    return [
        (
            ToolManifest(
                id="tools.code.run_sandbox",
                name="Run Code or Tests in Docker Sandbox",
                version="1.0.0",
                category="code",
                description="Execute a workspace Python script or pytest suite strictly inside an isolated Docker container.",
                capabilities=["automated_testing", "code_execution", "sandbox_verification"],
                input_schema={
                    "type": "object",
                    "required": ["operation"],
                    "additionalProperties": False,
                    "properties": {
                        "operation": {"type": "string", "enum": ["python_script", "pytest"]},
                        "script_path": {"type": "string", "minLength": 1},
                        "test_path": {"type": "string", "minLength": 1},
                        "args": {"type": "array", "maxItems": 16, "items": {"type": "string", "maxLength": 128}},
                        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120},
                        "max_output_chars": {"type": "integer", "minimum": 256, "maximum": 20000},
                    },
                },
                output_schema={
                    "type": "object",
                    "required": [
                        "operation",
                        "target_path",
                        "exit_code",
                        "exit_state",
                        "passed",
                        "stdout",
                        "stderr",
                        "truncated",
                        "duration_ms",
                        "timed_out",
                        "sandbox_image",
                    ],
                    "additionalProperties": False,
                    "properties": {
                        "operation": {"type": "string"},
                        "target_path": {"type": "string"},
                        "exit_code": {"type": "integer"},
                        "exit_state": {"type": "string"},
                        "passed": {"type": "boolean"},
                        "stdout": {"type": "string"},
                        "stderr": {"type": "string"},
                        "truncated": {"type": "boolean"},
                        "duration_ms": {"type": "integer"},
                        "timed_out": {"type": "boolean"},
                        "sandbox_image": {"type": "string"},
                    },
                },
                permissions_required=["code.execute", "files.read"],
                approval_required=False,
                risk_level="HIGH",
                execution_mode="sandbox_only",
                timeout_seconds=120,
                cost_profile=cost,
                safe_retry=True,
            ),
            _make_run_sandbox_handler(
                command_runner=command_runner,
                docker_binary_checker=docker_binary_checker,
                sandbox_image=sandbox_image,
            ),
        ),
        (
            ToolManifest(
                id="tools.code.git_inspect",
                name="Read-Only Git Inspector",
                version="1.0.0",
                category="code",
                description="Inspect read-only Git status, diff, or commit log via Git or mcp-server-git.",
                capabilities=["git_diff_analysis", "repository_inspection"],
                input_schema={
                    "type": "object",
                    "required": ["operation"],
                    "additionalProperties": False,
                    "properties": {
                        "operation": {"type": "string", "enum": ["status", "diff", "log"]},
                        "path": {"type": "string", "minLength": 1},
                        "max_commits": {"type": "integer", "minimum": 1, "maximum": 50},
                        "max_output_chars": {"type": "integer", "minimum": 256, "maximum": 20000},
                        "use_mcp": {"type": "boolean"},
                    },
                },
                output_schema={
                    "type": "object",
                    "required": ["operation", "exit_code", "output", "truncated", "via_mcp"],
                    "additionalProperties": False,
                    "properties": {
                        "operation": {"type": "string"},
                        "exit_code": {"type": "integer"},
                        "output": {"type": "string"},
                        "truncated": {"type": "boolean"},
                        "via_mcp": {"type": "boolean"},
                    },
                },
                permissions_required=["git.read"],
                approval_required=False,
                risk_level="LOW",
                execution_mode="restricted_local",
                timeout_seconds=30,
                cost_profile=cost,
                safe_retry=True,
                mcp_binding=McpToolBinding(server_id="git", remote_tool_name="git_status"),
            ),
            _make_git_inspect_handler(mcp_bridge=mcp_bridge, command_runner=command_runner),
        ),
    ]
