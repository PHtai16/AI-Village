"""Secret redaction and safe command summary utilities for Antigravity diagnostics."""

import re
from typing import Sequence

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL),
        "[REDACTED_PRIVATE_KEY]",
    ),
    (re.compile(r"AIza[0-9A-Za-z\-_]{20,}"), "AIza[REDACTED]"),
    (re.compile(r"ghp_[0-9A-Za-z]{20,}"), "ghp_[REDACTED]"),
    (re.compile(r"sk-[0-9A-Za-z\-_]{20,}"), "sk-[REDACTED]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/=]{12,}"), r"\1[REDACTED]"),
    (
        re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\s*[=:]\s*)([^\s,;\"']+)"),
        r"\1[REDACTED]",
    ),
)


def redact_secrets(text: str | None) -> str:
    """Redact common API key, token, password, and private-key patterns from text.

    Args:
        text: Raw diagnostic text such as stdout, stderr, or exception message.

    Returns:
        Secret-redacted string safe for logging and error payloads.
    """
    if not text:
        return ""
    sanitized = text
    for pattern, replacement in _SECRET_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


def format_command_summary(command: Sequence[str]) -> str:
    """Build a compact, non-sensitive summary of a subprocess command invocation.

    Args:
        command: Full argument vector passed to subprocess execution.

    Returns:
        Redacted command string with inline prompt payload replaced by length metadata.
    """
    summarized: list[str] = []
    skip_next_settings = False
    for arg in command:
        if skip_next_settings:
            summarized.append("<redacted_rules>")
            skip_next_settings = False
        elif arg == "--settings":
            summarized.append("--settings")
            skip_next_settings = True
        elif arg.startswith("--settings="):
            summarized.append("--settings=<redacted_rules>")
        elif arg.startswith("--print="):
            prompt_len = len(arg.removeprefix("--print="))
            summarized.append(f"--print=<{prompt_len}_chars>")
        else:
            summarized.append(redact_secrets(arg))
    return " ".join(summarized)
