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
    AntigravityTelemetry,
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
    if not payload:
        return AntigravityTelemetry(
            duration_ms=duration_ms,
            telemetry_status="UNAVAILABLE_FROM_PROVIDER",
        )

    provider_duration = payload.get("duration_seconds")
    usage = payload.get("usage")
    if isinstance(usage, dict) and any(
        isinstance(usage.get(k), int)
        for k in ("input_tokens", "output_tokens", "total_tokens")
    ):
        return AntigravityTelemetry(
            duration_ms=duration_ms,
            provider_duration_seconds=float(provider_duration) if isinstance(provider_duration, (int, float)) else None,
            input_tokens=usage.get("input_tokens") if isinstance(usage.get("input_tokens"), int) else None,
            output_tokens=usage.get("output_tokens") if isinstance(usage.get("output_tokens"), int) else None,
            thinking_tokens=usage.get("thinking_tokens") if isinstance(usage.get("thinking_tokens"), int) else None,
            cache_read_tokens=usage.get("cache_read_tokens") if isinstance(usage.get("cache_read_tokens"), int) else None,
            total_tokens=usage.get("total_tokens") if isinstance(usage.get("total_tokens"), int) else None,
            telemetry_status="VERIFIED_MEASURED",
        )

    return AntigravityTelemetry(
        duration_ms=duration_ms,
        provider_duration_seconds=float(provider_duration) if isinstance(provider_duration, (int, float)) else None,
        telemetry_status="UNAVAILABLE_FROM_PROVIDER",
    )


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
        redacted_stdout=redacted_out[:4000],
        redacted_stderr=redacted_err[:2000],
    )


class AntigravityAdapter:
    """Minimal adapter for probing and running non-interactive Antigravity CLI handshakes."""

    def __init__(
        self,
        cli_path: str | None = None,
        runner: RunnerCallable | None = None,
    ) -> None:
        """Initialize the adapter with an optional CLI override and subprocess runner.

        Args:
            cli_path: Optional explicit path to the ``agy`` executable.
            runner: Optional subprocess callable matching ``subprocess.run`` for testing.
        """
        self._cli_path = cli_path
        self._runner: RunnerCallable = runner or subprocess.run

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
            supports_models_subcommand=("models" in help_text),
            probe_exit_code=help_proc.returncode,
            error=redact_secrets(help_proc.stderr) if help_proc.returncode != 0 else None,
        )

    def handshake(self, request: AntigravityHandshakeRequest) -> AntigravityHandshakeResult:
        """Execute a single non-interactive handshake prompt through ``agy``.

        Args:
            request: Validated handshake request specifying prompt and workspace.

        Returns:
            Normalized handshake result including exit code, timing, and telemetry.
        """
        started_at = datetime.now(timezone.utc)
        t0 = time.monotonic()

        discovery = self.discover()
        if not discovery.found or not discovery.executable_path:
            finished_at = datetime.now(timezone.utc)
            duration_ms = int((time.monotonic() - t0) * 1000)
            return AntigravityHandshakeResult(
                status="BLOCKED",
                executable_path=None,
                command_summary="agy (not found)",
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                telemetry=AntigravityTelemetry(
                    duration_ms=duration_ms,
                    telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                ),
                error=AntigravityError(
                    error_code="EXECUTABLE_NOT_FOUND",
                    message=discovery.error or "Antigravity CLI executable not found.",
                    retryable=False,
                ),
            )

        exe = discovery.executable_path
        workspace = Path(request.workspace_path)
        if not workspace.is_dir():
            finished_at = datetime.now(timezone.utc)
            duration_ms = int((time.monotonic() - t0) * 1000)
            return AntigravityHandshakeResult(
                status="FAILED",
                executable_path=exe,
                command_summary="agy (invalid workspace)",
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                telemetry=AntigravityTelemetry(
                    duration_ms=duration_ms,
                    telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                ),
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

        summary = format_command_summary(command)
        logger.info("Starting Antigravity handshake in %s (%s)", workspace, summary)

        try:
            proc = self._runner(
                command,
                cwd=str(workspace),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=request.timeout_seconds + 15,
            )
        except subprocess.TimeoutExpired as exc:
            finished_at = datetime.now(timezone.utc)
            duration_ms = int((time.monotonic() - t0) * 1000)
            stderr_raw = exc.stderr if isinstance(exc.stderr, str) else ""
            stdout_raw = exc.stdout if isinstance(exc.stdout, str) else ""
            return AntigravityHandshakeResult(
                status="TIMED_OUT",
                executable_path=exe,
                version=caps.version,
                command_summary=summary,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                model_id=request.model,
                telemetry=AntigravityTelemetry(
                    duration_ms=duration_ms,
                    telemetry_status="UNAVAILABLE_FROM_PROVIDER",
                ),
                error=AntigravityError(
                    error_code="TIMED_OUT",
                    message=f"Antigravity CLI timed out after {request.timeout_seconds}s.",
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
                version=caps.version,
                command_summary=summary,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                model_id=request.model,
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
            version=caps.version,
            command=command,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            requested_model=request.model,
        )


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
