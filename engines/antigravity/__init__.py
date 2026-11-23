"""Antigravity CLI adapter package for Phase 02 execution proof."""

from engines.antigravity.adapter import (
    AntigravityAdapter,
    discover,
    execute_tool_task,
    handshake,
    normalize_result,
    probe,
)
from engines.antigravity.diagnostics import format_command_summary, redact_secrets
from engines.antigravity.models import (
    AntigravityCapabilities,
    AntigravityDiscoveryResult,
    AntigravityError,
    AntigravityHandshakeRequest,
    AntigravityHandshakeResult,
    AntigravityTelemetry,
    AntigravityToolExecutionRequest,
    AntigravityToolExecutionResult,
    ArtifactVerificationResult,
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
    "AntigravityTelemetry",
    "AntigravityToolExecutionRequest",
    "AntigravityToolExecutionResult",
    "ArtifactVerificationResult",
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
    "snapshot_file_hashes",
    "verify_artifact",
]
