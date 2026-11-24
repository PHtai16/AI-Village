"""Minimal Antigravity CLI adapter implementing discovery, probe, and handshake."""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any, Callable

from engines.antigravity.diagnostics import format_command_summary, redact_secrets
from engines.antigravity.models import (
    AntigravityCapabilities,
    AntigravityDiscoveryResult,
    AntigravityError,
    AntigravityHandshakeRequest,
    AntigravityHandshakeResult,
    AntigravityStreamEvent,
    AntigravityStreamRequest,
    AntigravityStreamResult,
    AntigravityTelemetry,
    AntigravityToolExecutionRequest,
    AntigravityToolExecutionResult,
    ArtifactVerificationResult,
)
from engines.antigravity.streaming import (
    AntigravityStreamParser,
    PopenFactory,
    build_telemetry_from_payload,
    run_streaming_process,
)
from engines.antigravity.sessions import build_ephemeral_deny_settings
from engines.antigravity.verification import (
    resolve_within_workspace,
    snapshot_file_hashes,
    verify_artifact,
)

logger = logging.getLogger(__name__)

_VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.]+)?)\b")
RunnerCallable = Callable[..., subprocess.CompletedProcess[str]]


def _default_user_cli_path() -> Path:
    """Return the standard per-user installation path for the Antigravity CLI."""
    binary_name = "agy.exe" if os.name == "nt" else "agy"
    return Path.home() / ".gemini" / "bin" / binary_name


def discover(cli_path: str | None = None) -> AntigravityDiscoveryResult:
    """Locate the Antigravity CLI executable without fabricating missing binaries.

    Args:
        cli_path: Optional explicit path to the ``agy`` executable.

    Returns:
        Structured discovery result with the resolved path and checked locations.
    """
    checked: list[str] = []

    if cli_path:
        explicit = Path(cli_path)
        checked.append(str(explicit))
        if explicit.is_file():
            return AntigravityDiscoveryResult(
                found=True,
                executable_path=str(explicit.resolve()),
                source="explicit",
                checked_locations=checked,
            )
        return AntigravityDiscoveryResult(
            found=False,
            source="not_found",
            checked_locations=checked,
            error=f"Explicit Antigravity CLI path does not exist: {explicit}",
        )

    env_path = os.environ.get("ANTIGRAVITY_CLI_PATH", "").strip()
    if env_path:
        candidate = Path(env_path)
        checked.append(f"env:ANTIGRAVITY_CLI_PATH={candidate}")
        if candidate.is_file():
            return AntigravityDiscoveryResult(
                found=True,
                executable_path=str(candidate.resolve()),
                source="env",
                checked_locations=checked,
            )
        return AntigravityDiscoveryResult(
            found=False,
            source="not_found",
            checked_locations=checked,
            error=f"ANTIGRAVITY_CLI_PATH does not exist: {candidate}",
        )

    which_path = shutil.which("agy")
    checked.append("PATH:agy")
    if which_path:
        return AntigravityDiscoveryResult(
            found=True,
            executable_path=str(Path(which_path).resolve()),
            source="path",
            checked_locations=checked,
        )

    home_candidate = _default_user_cli_path()
    checked.append(str(home_candidate))
    if home_candidate.is_file():
        return AntigravityDiscoveryResult(
            found=True,
            executable_path=str(home_candidate.resolve()),
            source="user_home",
            checked_locations=checked,
        )

    return AntigravityDiscoveryResult(
        found=False,
        source="not_found",
        checked_locations=checked,
        error="Antigravity CLI executable (agy) was not found in configured locations.",
    )


def _extract_version(output: str) -> str | None:
    """Parse a semantic version string from ``agy --version`` output."""
    match = _VERSION_RE.search(output.strip())
    return match.group(1) if match else None


def _classify_cli_error(exit_code: int | None, stdout: str, stderr: str) -> AntigravityError:
    """Map raw CLI failure output into a normalized, secret-redacted AntigravityError."""
    redacted_err = redact_secrets(stderr)
    redacted_out = redact_secrets(stdout)
    combined = f"{redacted_err}\n{redacted_out}".strip()
    lowered = combined.lower()

    if any(tok in lowered for tok in ("unknown flag", "unknown shorthand", "ignored", "flag provided but not defined")):
        code = "UNSUPPORTED_ARGUMENT"
        retryable = False
    elif any(tok in lowered for tok in ("unauthenticated", "unauthorized", "not logged in", "auth", "invalid api key")):
        code = "AUTH_ERROR"
        retryable = False
    elif any(tok in lowered for tok in ("quota", "billing", "insufficient")):
        code = "QUOTA_EXCEEDED"
        retryable = False
    elif any(tok in lowered for tok in ("rate limit", "too many requests", "429")):
        code = "RATE_LIMITED"
        retryable = True
    else:
        code = "PROVIDER_ERROR"
        retryable = False

    summary_message = combined.splitlines()[0] if combined else f"Antigravity CLI exited with code {exit_code}."
    return AntigravityError(
        error_code=code,
        message=summary_message[:500],
        retryable=retryable,
        exit_code=exit_code,
        redacted_stderr=redacted_err[:2000],
    )


def _build_telemetry(duration_ms: int, payload: dict[str, Any] | None) -> AntigravityTelemetry:
    """Construct telemetry from verified CLI JSON usage fields without fabricating data."""
    return build_telemetry_from_payload(duration_ms, payload)


def normalize_result(
    *,
    executable_path: str | None,
    version: str | None,
    command: list[str],
    started_at: datetime,
    finished_at: datetime,
    duration_ms: int,
    exit_code: int | None,
    stdout: str,
    stderr: str,
    requested_model: str | None = None,
) -> AntigravityHandshakeResult:
    """Parse and normalize raw ``agy --output-format json`` subprocess output.

    Args:
        executable_path: Resolved path to ``agy``.
        version: Detected CLI version string if available.
        command: Exact argument vector invoked.
        started_at: UTC timestamp before subprocess start.
        finished_at: UTC timestamp after subprocess exit.
        duration_ms: Wall-clock duration in milliseconds.
        exit_code: Process exit code.
        stdout: Raw stdout captured from subprocess.
        stderr: Raw stderr captured from subprocess.
        requested_model: Explicit model passed on CLI if any.

    Returns:
        Normalized ``AntigravityHandshakeResult`` with redacted diagnostics.
    """
    summary = format_command_summary(command)
    redacted_out = redact_secrets(stdout)
    redacted_err = redact_secrets(stderr)

    if exit_code != 0:
        err = _classify_cli_error(exit_code, stdout, stderr)
        return AntigravityHandshakeResult(
            status="FAILED",
            executable_path=executable_path,
            version=version,
            command_summary=summary,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            exit_code=exit_code,
            model_id=requested_model,
            telemetry=_build_telemetry(duration_ms, None),
            error=err,
            redacted_stdout=redacted_out[:4000],
            redacted_stderr=redacted_err[:2000],
        )

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return AntigravityHandshakeResult(
            status="FAILED",
            executable_path=executable_path,
            version=version,
            command_summary=summary,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            exit_code=exit_code,
            model_id=requested_model,
            telemetry=_build_telemetry(duration_ms, None),
            error=AntigravityError(
                error_code="INVALID_RESPONSE",
                message=f"Failed to parse JSON output from Antigravity CLI: {exc.msg}",
                retryable=False,
                exit_code=exit_code,
                redacted_stderr=redacted_err[:2000],
            ),
            redacted_stdout=redacted_out[:4000],
            redacted_stderr=redacted_err[:2000],
        )

    if not isinstance(payload, dict):
        return AntigravityHandshakeResult(
            status="FAILED",
            executable_path=executable_path,
            version=version,
            command_summary=summary,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            exit_code=exit_code,
            model_id=requested_model,
            telemetry=_build_telemetry(duration_ms, None),
            error=AntigravityError(
                error_code="INVALID_RESPONSE",
                message="Antigravity CLI JSON output must be an object.",
                retryable=False,
                exit_code=exit_code,
                redacted_stderr=redacted_err[:2000],
            ),
            redacted_stdout=redacted_out[:4000],
            redacted_stderr=redacted_err[:2000],
        )

    provider_status = payload.get("status")
    response_text = payload.get("response")
    conversation_id = payload.get("conversation_id")
    num_turns = payload.get("num_turns")
    raw_denied = payload.get("denied_actions")
    denied_actions = (
        [item for item in raw_denied if isinstance(item, dict)]
        if isinstance(raw_denied, list)
        else []
    )
    telemetry = _build_telemetry(duration_ms, payload)

    if provider_status != "SUCCESS" or not isinstance(response_text, str):
        return AntigravityHandshakeResult(
            status="FAILED",
            executable_path=executable_path,
            version=version,
            command_summary=summary,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            exit_code=exit_code,
            response_text=redact_secrets(response_text) if isinstance(response_text, str) else None,
            provider_status=str(provider_status) if provider_status is not None else None,
            conversation_id=str(conversation_id) if isinstance(conversation_id, str) else None,
            model_id=requested_model,
            num_turns=num_turns if isinstance(num_turns, int) else None,
            telemetry=telemetry,
            error=AntigravityError(
                error_code="PROVIDER_ERROR",
                message=f"Antigravity CLI reported non-success status: {provider_status}",
                retryable=False,
                exit_code=exit_code,
                redacted_stderr=redacted_err[:2000],
            ),
            denied_actions=denied_actions,
            redacted_stdout=redacted_out[:4000],
            redacted_stderr=redacted_err[:2000],
        )

    return AntigravityHandshakeResult(
        status="SUCCEEDED",
        executable_path=executable_path,
        version=version,
        command_summary=summary,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=duration_ms,
        exit_code=exit_code,
        response_text=redact_secrets(response_text),
        provider_status=provider_status,
        conversation_id=conversation_id if isinstance(conversation_id, str) else None,
        model_id=requested_model,
        num_turns=num_turns if isinstance(num_turns, int) else None,
        telemetry=telemetry,
        denied_actions=denied_actions,
        redacted_stdout=redacted_out[:4000],
        redacted_stderr=redacted_err[:2000],
    )


class AntigravityAdapter:
    """Minimal adapter for probing and running non-interactive Antigravity CLI handshakes."""

    def __init__(
        self,
        cli_path: str | None = None,
        runner: RunnerCallable | None = None,
        popen_factory: PopenFactory | None = None,
    ) -> None:
        """Initialize the adapter with optional CLI, subprocess runner, and Popen overrides.

        Args:
            cli_path: Optional explicit path to the ``agy`` executable.
            runner: Optional subprocess callable matching ``subprocess.run`` for testing.
            popen_factory: Optional process spawner matching ``subprocess.Popen`` for streaming.
        """
        self._cli_path = cli_path
        self._runner: RunnerCallable = runner or subprocess.run
        self._popen_factory: PopenFactory = popen_factory or subprocess.Popen

    def discover(self) -> AntigravityDiscoveryResult:
        """Locate the Antigravity CLI executable."""
        return discover(self._cli_path)

    def probe(self, timeout_seconds: int = 15) -> AntigravityCapabilities:
        """Inspect CLI version and supported flags via ``--version`` and ``--help``.

        Args:
            timeout_seconds: Subprocess timeout for each probe invocation.

        Returns:
            Detected capability flags and version metadata.
        """
        discovery = self.discover()
        if not discovery.found or not discovery.executable_path:
            return AntigravityCapabilities(
                executable_path="",
                error=discovery.error or "Antigravity CLI executable not found.",
            )

        exe = discovery.executable_path
        try:
            ver_proc = self._runner(
                [exe, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
            )
            help_proc = self._runner(
                [exe, "--help"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            msg = redact_secrets(str(exc))
            logger.warning("Antigravity probe failed for %s: %s", exe, msg)
            return AntigravityCapabilities(executable_path=exe, error=msg)

        version_output = f"{ver_proc.stdout}\n{ver_proc.stderr}"
        version = _extract_version(version_output) if ver_proc.returncode == 0 else None
        help_text = (
            f"{help_proc.stdout}\n{help_proc.stderr}"
            if help_proc.returncode == 0
            else ""
        )

        return AntigravityCapabilities(
            executable_path=exe,
            version=version,
            supports_print_mode=("--print" in help_text),
            supports_json_output=("--output-format" in help_text and "json" in help_text),
            supports_stream_json_flag=("stream-json" in help_text),
            supports_print_timeout=("--print-timeout" in help_text),
            supports_sandbox_flag=("--sandbox" in help_text),
            supports_mode_flag=("--mode" in help_text),
            supports_models_subcommand=("models" in help_text),
            supports_conversation_flag=("--conversation" in help_text),
            supports_settings_flag=("--settings" in help_text),
            probe_exit_code=help_proc.returncode,
            error=redact_secrets(help_proc.stderr) if help_proc.returncode != 0 else None,
        )

    def _run_cli_command(
        self,
        *,
        exe: str,
        version: str | None,
        command: list[str],
        workspace: Path,
        timeout_seconds: int,
        requested_model: str | None,
    ) -> AntigravityHandshakeResult:
        """Execute a prepared ``agy`` command inside ``workspace`` and normalize output."""
        started_at = datetime.now(timezone.utc)
        t0 = time.monotonic()
        summary = format_command_summary(command)
        logger.info("Running Antigravity CLI in %s (%s)", workspace, summary)

        try:
            proc = self._runner(
                command,
                cwd=str(workspace),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds + 15,
            )
        except subprocess.TimeoutExpired as exc:
            finished_at = datetime.now(timezone.utc)
            duration_ms = int((time.monotonic() - t0) * 1000)
            stderr_raw = exc.stderr if isinstance(exc.stderr, str) else ""
            stdout_raw = exc.stdout if isinstance(exc.stdout, str) else ""
            return AntigravityHandshakeResult(
                status="TIMED_OUT",
                executable_path=exe,
                version=version,
                command_summary=summary,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                model_id=requested_model,
                telemetry=AntigravityTelemetry(
                    duration_ms=duration_ms,
                    telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                ),
                error=AntigravityError(
                    error_code="TIMED_OUT",
                    message=f"Antigravity CLI timed out after {timeout_seconds}s.",
                    retryable=False,
                    redacted_stderr=redact_secrets(stderr_raw)[:2000],
                ),
                redacted_stdout=redact_secrets(stdout_raw)[:4000],
                redacted_stderr=redact_secrets(stderr_raw)[:2000],
            )
        except (OSError, subprocess.SubprocessError) as exc:
            finished_at = datetime.now(timezone.utc)
            duration_ms = int((time.monotonic() - t0) * 1000)
            msg = redact_secrets(str(exc))
            return AntigravityHandshakeResult(
                status="FAILED",
                executable_path=exe,
                version=version,
                command_summary=summary,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                model_id=requested_model,
                telemetry=AntigravityTelemetry(
                    duration_ms=duration_ms,
                    telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                ),
                error=AntigravityError(
                    error_code="PROCESS_LAUNCH_ERROR",
                    message=msg,
                    retryable=False,
                ),
            )

        finished_at = datetime.now(timezone.utc)
        duration_ms = int((time.monotonic() - t0) * 1000)
        return normalize_result(
            executable_path=exe,
            version=version,
            command=command,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            requested_model=requested_model,
        )

    @staticmethod
    def _preflight_failure_cli_result(
        *,
        status: str,
        executable_path: str | None,
        version: str | None,
        command_summary: str,
        error: AntigravityError,
    ) -> AntigravityHandshakeResult:
        """Build a normalized preflight failure result when CLI cannot be invoked."""
        now = datetime.now(timezone.utc)
        return AntigravityHandshakeResult(
            status=status,  # type: ignore[arg-type]
            executable_path=executable_path,
            version=version,
            command_summary=command_summary,
            started_at=now,
            finished_at=now,
            duration_ms=0,
            telemetry=AntigravityTelemetry(
                duration_ms=0,
                telemetry_status="UNAVAILABLE_FROM_PROVIDER",
            ),
            error=error,
        )

    def handshake(self, request: AntigravityHandshakeRequest) -> AntigravityHandshakeResult:
        """Execute a single non-interactive handshake prompt through ``agy``.

        Args:
            request: Validated handshake request specifying prompt and workspace.

        Returns:
            Normalized handshake result including exit code, timing, and telemetry.
        """
        discovery = self.discover()
        if not discovery.found or not discovery.executable_path:
            return self._preflight_failure_cli_result(
                status="BLOCKED",
                executable_path=None,
                version=None,
                command_summary="agy (not found)",
                error=AntigravityError(
                    error_code="EXECUTABLE_NOT_FOUND",
                    message=discovery.error or "Antigravity CLI executable not found.",
                    retryable=False,
                ),
            )

        exe = discovery.executable_path
        workspace = Path(request.workspace_path)
        if not workspace.is_dir():
            return self._preflight_failure_cli_result(
                status="FAILED",
                executable_path=exe,
                version=None,
                command_summary="agy (invalid workspace)",
                error=AntigravityError(
                    error_code="PROCESS_LAUNCH_ERROR",
                    message=f"Workspace directory does not exist: {workspace}",
                    retryable=False,
                ),
            )

        caps = self.probe(timeout_seconds=min(15, request.timeout_seconds))
        command: list[str] = [
            exe,
            "--output-format",
            "json",
            f"--print-timeout={request.timeout_seconds}s",
        ]
        if request.sandbox:
            command.append("--sandbox")
        if request.model:
            command.extend(["--model", request.model])
        command.append(f"--print={request.prompt}")

        return self._run_cli_command(
            exe=exe,
            version=caps.version,
            command=command,
            workspace=workspace,
            timeout_seconds=request.timeout_seconds,
            requested_model=request.model,
        )

    def execute_tool_task(
        self,
        request: AntigravityToolExecutionRequest,
    ) -> AntigravityToolExecutionResult:
        """Execute a sandboxed file-tool prompt and verify the resulting artifact on disk.

        Args:
            request: Validated tool execution request with workspace and artifact rules.

        Returns:
            ``AntigravityToolExecutionResult`` pairing CLI outcome with independent
            filesystem and SHA-256 verification.
        """
        workspace = Path(request.workspace_path)
        fixture_id = workspace.name or str(workspace)

        def _fail_early(
            *,
            exec_status: str,
            cli_status: str,
            exe_path: str | None,
            version: str | None,
            summary: str,
            err: AntigravityError,
            within_ws: bool = True,
        ) -> AntigravityToolExecutionResult:
            cli_res = self._preflight_failure_cli_result(
                status=cli_status,
                executable_path=exe_path,
                version=version,
                command_summary=summary,
                error=err,
            )
            verification = ArtifactVerificationResult(
                artifact_relpath=request.expected_artifact_relpath,
                cli_success=False,
                artifact_exists=False,
                within_workspace=within_ws,
                content_verified=False,
                hash_verified=False,
                source_integrity_verified=False,
                business_verified=False,
                failure_reasons=[err.message],
            )
            return AntigravityToolExecutionResult(
                status=exec_status,  # type: ignore[arg-type]
                workspace_fixture_id=fixture_id,
                cli_result=cli_res,
                verification=verification,
                error=err,
            )

        discovery = self.discover()
        if not discovery.found or not discovery.executable_path:
            return _fail_early(
                exec_status="BLOCKED",
                cli_status="BLOCKED",
                exe_path=None,
                version=None,
                summary="agy (not found)",
                err=AntigravityError(
                    error_code="EXECUTABLE_NOT_FOUND",
                    message=discovery.error or "Antigravity CLI executable not found.",
                    retryable=False,
                ),
            )

        exe = discovery.executable_path
        if not workspace.is_dir():
            return _fail_early(
                exec_status="FAILED",
                cli_status="FAILED",
                exe_path=exe,
                version=None,
                summary="agy (invalid workspace)",
                err=AntigravityError(
                    error_code="PROCESS_LAUNCH_ERROR",
                    message=f"Workspace directory does not exist: {workspace}",
                    retryable=False,
                ),
                within_ws=False,
            )

        try:
            resolve_within_workspace(workspace, request.expected_artifact_relpath)
        except ValueError as exc:
            return _fail_early(
                exec_status="FAILED",
                cli_status="FAILED",
                exe_path=exe,
                version=None,
                summary="agy (artifact path outside workspace)",
                err=AntigravityError(
                    error_code="ARTIFACT_OUTSIDE_WORKSPACE",
                    message=str(exc),
                    retryable=False,
                ),
                within_ws=False,
            )

        try:
            protected_hashes = snapshot_file_hashes(workspace, request.source_files_to_protect)
        except ValueError as exc:
            return _fail_early(
                exec_status="FAILED",
                cli_status="FAILED",
                exe_path=exe,
                version=None,
                summary="agy (protected source outside workspace)",
                err=AntigravityError(
                    error_code="ARTIFACT_OUTSIDE_WORKSPACE",
                    message=str(exc),
                    retryable=False,
                ),
                within_ws=False,
            )
        except FileNotFoundError as exc:
            return _fail_early(
                exec_status="FAILED",
                cli_status="FAILED",
                exe_path=exe,
                version=None,
                summary="agy (missing protected source file)",
                err=AntigravityError(
                    error_code="PROCESS_LAUNCH_ERROR",
                    message=str(exc),
                    retryable=False,
                ),
            )

        caps = self.probe(timeout_seconds=min(15, request.timeout_seconds))
        if request.require_sandbox and not caps.supports_sandbox_flag:
            return _fail_early(
                exec_status="BLOCKED",
                cli_status="BLOCKED",
                exe_path=exe,
                version=caps.version,
                summary="agy (sandbox flag unsupported)",
                err=AntigravityError(
                    error_code="SANDBOX_UNVERIFIED",
                    message="Antigravity CLI does not advertise --sandbox support.",
                    retryable=False,
                ),
            )

        if not (caps.supports_print_mode and caps.supports_json_output and caps.supports_mode_flag):
            return _fail_early(
                exec_status="BLOCKED",
                cli_status="BLOCKED",
                exe_path=exe,
                version=caps.version,
                summary="agy (unsupported print/json/mode flags)",
                err=AntigravityError(
                    error_code="UNSUPPORTED_MODE",
                    message="Antigravity CLI lacks required --print, --output-format json, or --mode support.",
                    retryable=False,
                ),
            )

        command: list[str] = [exe]
        if request.require_sandbox:
            command.append("--sandbox")
        command.extend(
            [
                "--mode",
                request.execution_mode,
                "--output-format",
                "json",
                f"--print-timeout={request.timeout_seconds}s",
            ]
        )
        if request.model:
            command.extend(["--model", request.model])
        command.append(f"--print={request.prompt}")

        cli_result = self._run_cli_command(
            exe=exe,
            version=caps.version,
            command=command,
            workspace=workspace,
            timeout_seconds=request.timeout_seconds,
            requested_model=request.model,
        )

        verification = verify_artifact(
            workspace_dir=workspace,
            artifact_relpath=request.expected_artifact_relpath,
            cli_result=cli_result,
            expected_content=request.expected_content,
            required_substrings=request.required_substrings,
            protected_source_hashes=protected_hashes,
        )

        if verification.business_verified:
            return AntigravityToolExecutionResult(
                status="VERIFIED_PASS",
                workspace_fixture_id=fixture_id,
                cli_result=cli_result,
                verification=verification,
            )

        if cli_result.status == "TIMED_OUT":
            return AntigravityToolExecutionResult(
                status="TIMED_OUT",
                workspace_fixture_id=fixture_id,
                cli_result=cli_result,
                verification=verification,
                error=cli_result.error,
            )

        if cli_result.denied_actions:
            err = AntigravityError(
                error_code="SCOPE_VIOLATION",
                message=f"CLI denied actions during execution: {cli_result.denied_actions}",
                retryable=False,
                exit_code=cli_result.exit_code,
                redacted_stderr=cli_result.redacted_stderr,
            )
            return AntigravityToolExecutionResult(
                status="VERIFIED_FAIL",
                workspace_fixture_id=fixture_id,
                cli_result=cli_result,
                verification=verification,
                error=err,
            )

        if cli_result.error is not None:
            return AntigravityToolExecutionResult(
                status="FAILED",
                workspace_fixture_id=fixture_id,
                cli_result=cli_result,
                verification=verification,
                error=cli_result.error,
            )

        err = self._classify_verification_failure(verification)
        return AntigravityToolExecutionResult(
            status="VERIFIED_FAIL",
            workspace_fixture_id=fixture_id,
            cli_result=cli_result,
            verification=verification,
            error=err,
        )

    @staticmethod
    def _stream_preflight_failure(
        *,
        status: str,
        executable_path: str | None,
        version: str | None,
        command_summary: str,
        model_id: str | None,
        error: AntigravityError,
    ) -> AntigravityStreamResult:
        """Build a normalized preflight failure result for ``stream_task``."""
        now = datetime.now(timezone.utc)
        return AntigravityStreamResult(
            status=status,  # type: ignore[arg-type]
            executable_path=executable_path,
            version=version,
            command_summary=command_summary,
            started_at=now,
            finished_at=now,
            duration_ms=0,
            model_id=model_id,
            telemetry=AntigravityTelemetry(
                duration_ms=0,
                telemetry_status="UNAVAILABLE_FROM_PROVIDER",
            ),
            error=error,
        )

    def stream_task(
        self,
        request: AntigravityStreamRequest,
        *,
        on_event: Callable[[AntigravityStreamEvent], None] | None = None,
    ) -> AntigravityStreamResult:
        """Execute ``agy --output-format stream-json`` and stream normalized events.

        Args:
            request: Validated streaming request specifying prompt, workspace, and caps.
            on_event: Optional callback invoked as each normalized event arrives.

        Returns:
            ``AntigravityStreamResult`` containing ordered events, telemetry, and outcome.
        """
        discovery = self.discover()
        if not discovery.found or not discovery.executable_path:
            return self._stream_preflight_failure(
                status="BLOCKED",
                executable_path=None,
                version=None,
                command_summary="agy (not found)",
                model_id=request.model,
                error=AntigravityError(
                    error_code="EXECUTABLE_NOT_FOUND",
                    message=discovery.error or "Antigravity CLI executable not found.",
                    retryable=False,
                ),
            )

        exe = discovery.executable_path
        workspace = Path(request.workspace_path)
        if not workspace.is_dir():
            return self._stream_preflight_failure(
                status="FAILED",
                executable_path=exe,
                version=None,
                command_summary="agy (invalid workspace)",
                model_id=request.model,
                error=AntigravityError(
                    error_code="PROCESS_LAUNCH_ERROR",
                    message=f"Workspace directory does not exist: {workspace}",
                    retryable=False,
                ),
            )

        caps = self.probe(timeout_seconds=min(15, request.timeout_seconds))
        if request.require_sandbox and not caps.supports_sandbox_flag:
            return self._stream_preflight_failure(
                status="BLOCKED",
                executable_path=exe,
                version=caps.version,
                command_summary="agy (sandbox flag unsupported)",
                model_id=request.model,
                error=AntigravityError(
                    error_code="SANDBOX_UNVERIFIED",
                    message="Antigravity CLI does not advertise --sandbox support.",
                    retryable=False,
                ),
            )

        if not (caps.supports_print_mode and caps.supports_stream_json_flag and caps.supports_mode_flag):
            return self._stream_preflight_failure(
                status="BLOCKED",
                executable_path=exe,
                version=caps.version,
                command_summary="agy (unsupported print/stream-json/mode flags)",
                model_id=request.model,
                error=AntigravityError(
                    error_code="UNSUPPORTED_MODE",
                    message="Antigravity CLI lacks required --print, stream-json, or --mode support.",
                    retryable=False,
                ),
            )

        if request.conversation_id and not caps.supports_conversation_flag:
            return self._stream_preflight_failure(
                status="BLOCKED",
                executable_path=exe,
                version=caps.version,
                command_summary="agy (unsupported --conversation flag)",
                model_id=request.model,
                error=AntigravityError(
                    error_code="UNSUPPORTED_MODE",
                    message="Antigravity CLI lacks required --conversation support.",
                    retryable=False,
                ),
            )

        requires_settings = bool(request.denied_read_write_paths or request.deny_shell_commands)
        if requires_settings and not caps.supports_settings_flag:
            return self._stream_preflight_failure(
                status="BLOCKED",
                executable_path=exe,
                version=caps.version,
                command_summary="agy (unsupported --settings flag)",
                model_id=request.model,
                error=AntigravityError(
                    error_code="UNSUPPORTED_MODE",
                    message="Antigravity CLI lacks required --settings support for session isolation.",
                    retryable=False,
                ),
            )

        command: list[str] = [exe]
        if request.require_sandbox:
            command.append("--sandbox")
        command.extend(
            [
                "--mode",
                request.execution_mode,
                "--output-format",
                "stream-json",
                f"--print-timeout={request.timeout_seconds}s",
            ]
        )
        if request.model:
            command.extend(["--model", request.model])
        if request.conversation_id:
            command.extend(["--conversation", request.conversation_id])
        if requires_settings:
            settings_payload = build_ephemeral_deny_settings(
                [Path(denied) for denied in request.denied_read_write_paths],
                deny_shell_commands=request.deny_shell_commands,
            )
            command.extend(["--settings", json.dumps(settings_payload, separators=(",", ":"))])
        command.append(f"--print={request.prompt}")

        parser = AntigravityStreamParser(
            max_payload_chars=request.max_payload_chars,
            max_line_bytes=request.max_line_bytes,
        )
        outcome = run_streaming_process(
            command=command,
            workspace=workspace,
            timeout_seconds=request.timeout_seconds,
            parser=parser,
            on_event=on_event,
            popen_factory=self._popen_factory,
        )
        events = list(outcome.events)
        telemetry = (
            parser.final_telemetry.model_copy(update={"duration_ms": outcome.duration_ms})
            if parser.final_telemetry is not None
            else AntigravityTelemetry(
                duration_ms=outcome.duration_ms,
                telemetry_status="UNAVAILABLE_FROM_PROVIDER",
            )
        )

        error: AntigravityError | None = None
        if outcome.launch_error is not None:
            status = "FAILED"
            error = AntigravityError(
                error_code="PROCESS_LAUNCH_ERROR",
                message=outcome.launch_error[:500],
                retryable=False,
                redacted_stderr=outcome.redacted_stderr,
            )
        elif outcome.timed_out:
            status = "TIMED_OUT"
            error = AntigravityError(
                error_code="TIMED_OUT",
                message=f"Antigravity CLI streaming timed out after {request.timeout_seconds}s.",
                retryable=False,
                exit_code=outcome.exit_code,
                redacted_stderr=outcome.redacted_stderr,
            )
        elif outcome.exit_code != 0:
            status = "FAILED"
            error = _classify_cli_error(
                outcome.exit_code,
                outcome.redacted_stdout_diagnostics,
                outcome.redacted_stderr,
            )
        elif not parser.completion_event_received:
            status = "FAILED"
            if parser.final_provider_status is not None and parser.final_provider_status != "SUCCESS":
                error = AntigravityError(
                    error_code="PROVIDER_ERROR",
                    message=f"Antigravity CLI stream reported non-success status: {parser.final_provider_status}",
                    retryable=False,
                    exit_code=outcome.exit_code,
                    redacted_stderr=outcome.redacted_stderr,
                )
            else:
                interrupted = parser.make_adapter_event(
                    event_type="stream_interrupted",
                    status="FAILED",
                    timestamp=outcome.finished_at,
                    redacted_payload={
                        "reason": "missing_completion_event",
                        "exit_code": outcome.exit_code,
                    },
                )
                events.append(interrupted)
                if on_event is not None:
                    on_event(interrupted)
                error = AntigravityError(
                    error_code="MISSING_COMPLETION_EVENT",
                    message="Antigravity CLI exited cleanly without emitting a terminal completion event.",
                    retryable=False,
                    exit_code=outcome.exit_code,
                    redacted_stderr=outcome.redacted_stderr,
                )
        else:
            status = "SUCCEEDED"

        return AntigravityStreamResult(
            status=status,  # type: ignore[arg-type]
            executable_path=exe,
            version=caps.version,
            command_summary=format_command_summary(command),
            started_at=outcome.started_at,
            first_event_at=outcome.first_event_at,
            finished_at=outcome.finished_at,
            duration_ms=outcome.duration_ms,
            exit_code=outcome.exit_code,
            provider_status=parser.final_provider_status,
            conversation_id=parser.conversation_id,
            model_id=request.model,
            num_turns=parser.final_num_turns,
            response_text=parser.final_response_text,
            telemetry=telemetry,
            events=events,
            observed_tool_names=list(parser.observed_tool_names),
            completion_event_received=parser.completion_event_received,
            parse_error_count=parser.parse_error_count,
            denied_actions=list(parser.final_denied_actions),
            error=error,
            redacted_stderr=outcome.redacted_stderr,
        )

    @staticmethod
    def _classify_verification_failure(verification: ArtifactVerificationResult) -> AntigravityError:
        """Map failed filesystem/hash verification checks to a specific AntigravityError."""
        reason = "; ".join(verification.failure_reasons) or "Artifact verification failed."
        if not verification.source_integrity_verified:
            code = "SOURCE_MODIFIED"
        elif not verification.within_workspace:
            code = "ARTIFACT_OUTSIDE_WORKSPACE"
        elif not verification.artifact_exists:
            code = "ARTIFACT_MISSING"
        elif not verification.content_verified:
            code = "CONTENT_MISMATCH"
        else:
            code = "HASH_MISMATCH"
        return AntigravityError(error_code=code, message=reason[:500], retryable=False)


def probe(cli_path: str | None = None, timeout_seconds: int = 15) -> AntigravityCapabilities:
    """Convenience wrapper to probe the local Antigravity CLI."""
    return AntigravityAdapter(cli_path=cli_path).probe(timeout_seconds=timeout_seconds)


def handshake(
    prompt: str,
    workspace_path: str,
    *,
    cli_path: str | None = None,
    timeout_seconds: int = 60,
    sandbox: bool = False,
    model: str | None = None,
) -> AntigravityHandshakeResult:
    """Convenience wrapper to execute a single Antigravity CLI handshake."""
    request = AntigravityHandshakeRequest(
        prompt=prompt,
        workspace_path=workspace_path,
        timeout_seconds=timeout_seconds,
        sandbox=sandbox,
        model=model,
    )
    return AntigravityAdapter(cli_path=cli_path).handshake(request)


def execute_tool_task(
    prompt: str,
    workspace_path: str,
    expected_artifact_relpath: str,
    *,
    cli_path: str | None = None,
    source_files_to_protect: list[str] | None = None,
    expected_content: str | None = None,
    required_substrings: list[str] | None = None,
    timeout_seconds: int = 90,
    require_sandbox: bool = True,
    execution_mode: str = "accept-edits",
    model: str | None = None,
) -> AntigravityToolExecutionResult:
    """Convenience wrapper to run a sandboxed Antigravity file tool execution proof."""
    request = AntigravityToolExecutionRequest(
        prompt=prompt,
        workspace_path=workspace_path,
        expected_artifact_relpath=expected_artifact_relpath,
        source_files_to_protect=source_files_to_protect or [],
        expected_content=expected_content,
        required_substrings=required_substrings or [],
        timeout_seconds=timeout_seconds,
        require_sandbox=require_sandbox,
        execution_mode=execution_mode,  # type: ignore[arg-type]
        model=model,
    )
    return AntigravityAdapter(cli_path=cli_path).execute_tool_task(request)


def stream_task(
    prompt: str,
    workspace_path: str,
    *,
    cli_path: str | None = None,
    timeout_seconds: int = 90,
    require_sandbox: bool = True,
    execution_mode: str = "accept-edits",
    model: str | None = None,
    max_payload_chars: int = 512,
    on_event: Callable[[AntigravityStreamEvent], None] | None = None,
) -> AntigravityStreamResult:
    """Convenience wrapper to stream normalized events from a single Antigravity CLI turn."""
    request = AntigravityStreamRequest(
        prompt=prompt,
        workspace_path=workspace_path,
        timeout_seconds=timeout_seconds,
        require_sandbox=require_sandbox,
        execution_mode=execution_mode,  # type: ignore[arg-type]
        model=model,
        max_payload_chars=max_payload_chars,
    )
    return AntigravityAdapter(cli_path=cli_path).stream_task(request, on_event=on_event)
