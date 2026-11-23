"""Antigravity CLI adapter package for Phase 02 execution proof."""

from engines.antigravity.adapter import (
    AntigravityAdapter,
    discover,
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
)

__all__ = [
    "AntigravityAdapter",
    "AntigravityCapabilities",
    "AntigravityDiscoveryResult",
    "AntigravityError",
    "AntigravityHandshakeRequest",
    "AntigravityHandshakeResult",
    "AntigravityTelemetry",
    "discover",
    "format_command_summary",
    "handshake",
    "normalize_result",
    "probe",
    "redact_secrets",
]
