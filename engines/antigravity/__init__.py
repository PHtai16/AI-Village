"""Antigravity CLI adapter package for Phase 02 execution proof."""

from engines.antigravity.adapter import (
    AntigravityAdapter,
    discover,
    execute_tool_task,
    handshake,
    normalize_result,
    probe,
    stream_task,
)
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
    StreamingProcessOutcome,
    build_telemetry_from_payload,
    run_streaming_process,
    sanitize_stream_text,
)
from engines.antigravity.verification import (
    compute_sha256_bytes,
    compute_sha256_file,
    resolve_within_workspace,
    snapshot_file_hashes,
    verify_artifact,
)

__all__ = [
    "AntigravityAdapter",
    "AntigravityCapabilities",
    "AntigravityDiscoveryResult",
    "AntigravityError",
    "AntigravityHandshakeRequest",
    "AntigravityHandshakeResult",
    "AntigravityStreamEvent",
    "AntigravityStreamParser",
    "AntigravityStreamRequest",
    "AntigravityStreamResult",
    "AntigravityTelemetry",
    "AntigravityToolExecutionRequest",
    "AntigravityToolExecutionResult",
    "ArtifactVerificationResult",
    "StreamingProcessOutcome",
    "build_telemetry_from_payload",
    "compute_sha256_bytes",
    "compute_sha256_file",
    "discover",
    "execute_tool_task",
    "format_command_summary",
    "handshake",
    "normalize_result",
    "probe",
    "redact_secrets",
    "resolve_within_workspace",
    "run_streaming_process",
    "sanitize_stream_text",
    "snapshot_file_hashes",
    "stream_task",
    "verify_artifact",
]
