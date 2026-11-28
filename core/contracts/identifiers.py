"""Validated domain identifiers, timestamp guards, and secret-safety primitives for Core contracts."""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
import re
from typing import Annotated, Any, Callable
import uuid

from pydantic import AfterValidator

from engines.antigravity.diagnostics import redact_secrets

_TASK_ID_RE = re.compile(r"^task_[a-z0-9][a-z0-9_-]{3,63}$")
_RUN_ID_RE = re.compile(r"^run_[a-z0-9][a-z0-9_-]{3,63}$")
_ARTIFACT_ID_RE = re.compile(r"^art_[a-z0-9][a-z0-9_-]{3,63}$")
_EVENT_ID_RE = re.compile(r"^evt_[a-z0-9][a-z0-9_-]{3,63}$")
_CHECKLIST_ITEM_ID_RE = re.compile(r"^chk_[a-z0-9][a-z0-9_-]{3,63}$")
_NOTIFICATION_ID_RE = re.compile(r"^ntf_[a-z0-9][a-z0-9_-]{3,63}$")
_PROJECT_ID_RE = re.compile(r"^prj_[a-z0-9][a-z0-9_-]{2,63}$")
_EVIDENCE_ID_RE = re.compile(r"^evd_[a-z0-9][a-z0-9_-]{3,63}$")
_CONVERSATION_ID_RE = re.compile(r"^conv_[a-z0-9][a-z0-9_-]{3,63}$")
_INTAKE_ID_RE = re.compile(r"^intk_[a-z0-9][a-z0-9_-]{3,63}$")
_REFINEMENT_ID_RE = re.compile(r"^ref_[a-z0-9][a-z0-9_-]{3,63}$")
_TURN_ID_RE = re.compile(r"^turn_[a-z0-9][a-z0-9_-]{3,63}$")
_INTERRUPT_ID_RE = re.compile(r"^intr_[a-z0-9][a-z0-9_-]{3,63}$")
_INCIDENT_ID_RE = re.compile(r"^inc_[a-z0-9][a-z0-9_-]{3,63}$")
_ESCALATION_ID_RE = re.compile(r"^esc_[a-z0-9][a-z0-9_-]{3,63}$")
_QUEUE_ENTRY_ID_RE = re.compile(r"^qent_[a-z0-9][a-z0-9_-]{3,63}$")
_LEASE_ID_RE = re.compile(r"^lease_[a-z0-9][a-z0-9_-]{3,63}$")
_SCHEDULE_ID_RE = re.compile(r"^sched_[a-z0-9][a-z0-9_-]{3,63}$")
_RESOURCE_POLICY_ID_RE = re.compile(r"^rpol_[a-z0-9][a-z0-9_-]{3,63}$")
_RESOURCE_LEDGER_ID_RE = re.compile(r"^rled_[a-z0-9][a-z0-9_-]{3,63}$")
_RESOURCE_ALERT_ID_RE = re.compile(r"^ralt_[a-z0-9][a-z0-9_-]{3,63}$")
_CLEANUP_ID_RE = re.compile(r"^clnp_[a-z0-9][a-z0-9_-]{3,63}$")
_PROCESS_HANDOFF_ID_RE = re.compile(r"^phnd_[a-z0-9][a-z0-9_-]{3,63}$")
_UUID_V4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_LEGACY_SPIKE_SESSION_RE = re.compile(r"^sess-[a-z0-9][a-z0-9_-]{2,63}$")
_AGENT_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_DEPARTMENT_ID_RE = re.compile(r"^[a-z0-9_]+$")
_SNAKE_ID_RE = re.compile(r"^[a-z0-9_]{2,64}$")
_TOOL_ID_RE = re.compile(r"^tools\.[a-z0-9_]+\.[a-z0-9_]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_CREDENTIAL_REF_RE = re.compile(
    r"^(?:env:[A-Z][A-Z0-9_]{2,63}|ref:[a-z0-9_.:/-]{3,128}|[a-z][a-z0-9_.-]{2,63})$"
)
_DRIVE_OR_SCHEME_RE = re.compile(r"^(?:[A-Za-z]:|[A-Za-z][A-Za-z0-9+.-]*://)")

_RAW_SECRET_PREFIXES: tuple[str, ...] = (
    "aiza",
    "sk-",
    "ghp_",
    "gho_",
    "github_pat_",
    "glpat-",
    "xoxb-",
    "xoxp-",
    "-----begin",
    "bearer ",
)
_EMBEDDED_SECRET_RE = re.compile(
    r"(?:(?<![A-Za-z0-9])(?:AIza[0-9A-Za-z\-_]{4,}|sk-[0-9A-Za-z\-_]{4,}|ghp_[0-9A-Za-z]{4,}|github_pat_[0-9A-Za-z_]{4,}|xox[bp]-[0-9A-Za-z-]{4,})|(?i:\bbearer\s+[A-Za-z0-9\-._~+/=]{4,})|(?i:\b(?:api[_-]?key|raw[_-]?key|secret|password|token)\s*[=:]\s*[^\s,;\"']+))"
)
_FORBIDDEN_KEY_FRAGMENTS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "raw_key",
    "secret",
    "password",
    "credential",
    "authorization",
    "x-goog-api-key",
    "private_key",
)
_REDACTED_KEY_SENTINEL = "[REDACTED_KEY_VALUE]"


def _is_sensitive_key(key: str) -> bool:
    """Return True if a dictionary key name denotes a credential, secret, or auth token."""
    lowered = key.strip().lower()
    if any(frag in lowered for frag in _FORBIDDEN_KEY_FRAGMENTS):
        return True
    return lowered == "token" or lowered.endswith("_token")


def new_task_id() -> str:
    """Generate a unique canonical Task identifier (`task_*`)."""
    return f"task_{uuid.uuid4().hex[:16]}"


def new_run_id() -> str:
    """Generate a unique canonical Run identifier (`run_*`)."""
    return f"run_{uuid.uuid4().hex[:16]}"


def new_artifact_id() -> str:
    """Generate a unique canonical Artifact identifier (`art_*`)."""
    return f"art_{uuid.uuid4().hex[:16]}"


def new_event_id() -> str:
    """Generate a unique canonical Event identifier (`evt_*`)."""
    return f"evt_{uuid.uuid4().hex[:16]}"


def new_checklist_item_id() -> str:
    """Generate a unique canonical ChecklistItem identifier (`chk_*`)."""
    return f"chk_{uuid.uuid4().hex[:16]}"


def new_notification_id() -> str:
    """Generate a unique canonical NotificationRef identifier (`ntf_*`)."""
    return f"ntf_{uuid.uuid4().hex[:16]}"


def new_project_id() -> str:
    """Generate a unique canonical organizational Project identifier (`prj_*`)."""
    return f"prj_{uuid.uuid4().hex[:12]}"


def new_evidence_id() -> str:
    """Generate a unique canonical Evidence identifier (`evd_*`)."""
    return f"evd_{uuid.uuid4().hex[:16]}"


def new_conversation_id() -> str:
    """Generate a unique canonical Conversation identifier (`conv_*`)."""
    return f"conv_{uuid.uuid4().hex[:16]}"


def new_intake_id() -> str:
    """Generate a unique canonical Reception Intake identifier (`intk_*`)."""
    return f"intk_{uuid.uuid4().hex[:16]}"


def new_refinement_id() -> str:
    """Generate a unique canonical Task Refinement identifier (`ref_*`)."""
    return f"ref_{uuid.uuid4().hex[:16]}"


def new_turn_id() -> str:
    """Generate a unique canonical Conversation Turn identifier (`turn_*`)."""
    return f"turn_{uuid.uuid4().hex[:16]}"


def new_interrupt_id() -> str:
    """Generate a unique canonical Interrupt event identifier (`intr_*`)."""
    return f"intr_{uuid.uuid4().hex[:16]}"


def new_incident_id() -> str:
    """Generate a unique canonical Interrupt Incident identifier (`inc_*`)."""
    return f"inc_{uuid.uuid4().hex[:16]}"


def new_escalation_id() -> str:
    """Generate a unique canonical Interrupt Escalation identifier (`esc_*`)."""
    return f"esc_{uuid.uuid4().hex[:16]}"


def new_queue_entry_id() -> str:
    """Generate a unique canonical Scheduler Queue Entry identifier (`qent_*`)."""
    return f"qent_{uuid.uuid4().hex[:16]}"


def new_lease_id() -> str:
    """Generate a unique canonical Execution Lease identifier (`lease_*`)."""
    return f"lease_{uuid.uuid4().hex[:16]}"


def new_schedule_id() -> str:
    """Generate a unique canonical Scheduled Task identifier (`sched_*`)."""
    return f"sched_{uuid.uuid4().hex[:16]}"


def new_resource_policy_id() -> str:
    """Generate a resource budget policy identifier with prefix ``rpol_``."""
    return f"rpol_{uuid.uuid4().hex[:16]}"


def new_resource_ledger_id() -> str:
    """Generate a resource ledger entry identifier with prefix ``rled_``."""
    return f"rled_{uuid.uuid4().hex[:16]}"


def new_resource_alert_id() -> str:
    """Generate a resource threshold alert identifier with prefix ``ralt_``."""
    return f"ralt_{uuid.uuid4().hex[:16]}"


def new_cleanup_id() -> str:
    """Generate a post-task cleanup identifier with prefix ``clnp_``."""
    return f"clnp_{uuid.uuid4().hex[:16]}"


def new_process_handoff_id() -> str:
    """Generate a process learning handoff identifier with prefix ``phnd_``."""
    return f"phnd_{uuid.uuid4().hex[:16]}"


def new_session_id() -> str:
    """Generate a unique canonical UUID v4 logical Session identifier."""
    return str(uuid.uuid4())


def reject_secret_text(value: str, field_name: str) -> str:
    """Reject credential patterns in arbitrary string fields (allowing empty strings)."""
    lowered = value.strip().lower()
    if (
        redact_secrets(value) != value
        or lowered.startswith(_RAW_SECRET_PREFIXES)
        or _EMBEDDED_SECRET_RE.search(value) is not None
    ):
        raise ValueError(f"{field_name} must never contain credentials or secret material.")
    return value


def ensure_not_secret(value: str, field_name: str) -> str:
    """Validate that a required string is non-blank and free of credential material."""
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be blank.")
    reject_secret_text(cleaned, field_name)
    return cleaned


def _validate_pattern(value: str, pattern: re.Pattern[str], field_name: str, desc: str) -> str:
    """Validate a required identifier against secret checks and a canonical regex."""
    cleaned = ensure_not_secret(value, field_name)
    if not pattern.fullmatch(cleaned):
        raise ValueError(f"{field_name} must match {desc}, got {value!r}.")
    return cleaned


def validate_task_id(value: str) -> str:
    """Validate canonical `task_*` Task identifier."""
    return _validate_pattern(value, _TASK_ID_RE, "task_id", "^task_[a-z0-9][a-z0-9_-]{3,63}$")


def validate_run_id(value: str) -> str:
    """Validate canonical `run_*` Run identifier."""
    return _validate_pattern(value, _RUN_ID_RE, "run_id", "^run_[a-z0-9][a-z0-9_-]{3,63}$")


def validate_artifact_id(value: str) -> str:
    """Validate canonical `art_*` Artifact identifier."""
    return _validate_pattern(
        value, _ARTIFACT_ID_RE, "artifact_id", "^art_[a-z0-9][a-z0-9_-]{3,63}$"
    )


def validate_event_id(value: str) -> str:
    """Validate canonical `evt_*` Event identifier."""
    return _validate_pattern(value, _EVENT_ID_RE, "event_id", "^evt_[a-z0-9][a-z0-9_-]{3,63}$")


def validate_checklist_item_id(value: str) -> str:
    """Validate canonical `chk_*` ChecklistItem identifier."""
    return _validate_pattern(
        value, _CHECKLIST_ITEM_ID_RE, "checklist_item_id", "^chk_[a-z0-9][a-z0-9_-]{3,63}$"
    )


def validate_notification_id(value: str) -> str:
    """Validate canonical `ntf_*` NotificationRef identifier."""
    return _validate_pattern(
        value, _NOTIFICATION_ID_RE, "notification_id", "^ntf_[a-z0-9][a-z0-9_-]{3,63}$"
    )


def validate_project_id(value: str) -> str:
    """Validate canonical `prj_*` Project grouping identifier."""
    return _validate_pattern(
        value, _PROJECT_ID_RE, "project_id", "^prj_[a-z0-9][a-z0-9_-]{2,63}$"
    )


def validate_evidence_id(value: str) -> str:
    """Validate canonical `evd_*` Evidence identifier."""
    return _validate_pattern(
        value, _EVIDENCE_ID_RE, "evidence_id", "^evd_[a-z0-9][a-z0-9_-]{3,63}$"
    )


def validate_conversation_id(value: str) -> str:
    """Validate canonical `conv_*` Conversation identifier."""
    return _validate_pattern(
        value, _CONVERSATION_ID_RE, "conversation_id", "^conv_[a-z0-9][a-z0-9_-]{3,63}$"
    )


def validate_intake_id(value: str) -> str:
    """Validate canonical `intk_*` Reception Intake identifier."""
    return _validate_pattern(
        value, _INTAKE_ID_RE, "intake_id", "^intk_[a-z0-9][a-z0-9_-]{3,63}$"
    )


def validate_refinement_id(value: str) -> str:
    """Validate canonical `ref_*` Task Refinement identifier."""
    return _validate_pattern(
        value, _REFINEMENT_ID_RE, "refinement_id", "^ref_[a-z0-9][a-z0-9_-]{3,63}$"
    )


def validate_turn_id(value: str) -> str:
    """Validate canonical `turn_*` Conversation Turn identifier."""
    return _validate_pattern(
        value, _TURN_ID_RE, "turn_id", "^turn_[a-z0-9][a-z0-9_-]{3,63}$"
    )


def validate_interrupt_id(value: str) -> str:
    """Validate canonical `intr_*` Interrupt event identifier."""
    return _validate_pattern(
        value, _INTERRUPT_ID_RE, "interrupt_id", "^intr_[a-z0-9][a-z0-9_-]{3,63}$"
    )


def validate_incident_id(value: str) -> str:
    """Validate canonical `inc_*` Interrupt Incident identifier."""
    return _validate_pattern(
        value, _INCIDENT_ID_RE, "incident_id", "^inc_[a-z0-9][a-z0-9_-]{3,63}$"
    )


def validate_escalation_id(value: str) -> str:
    """Validate canonical `esc_*` Interrupt Escalation identifier."""
    return _validate_pattern(
        value, _ESCALATION_ID_RE, "escalation_id", "^esc_[a-z0-9][a-z0-9_-]{3,63}$"
    )


def validate_queue_entry_id(value: str) -> str:
    """Validate canonical `qent_*` Scheduler Queue Entry identifier."""
    return _validate_pattern(
        value, _QUEUE_ENTRY_ID_RE, "queue_entry_id", "^qent_[a-z0-9][a-z0-9_-]{3,63}$"
    )


def validate_lease_id(value: str) -> str:
    """Validate canonical `lease_*` Execution Lease identifier."""
    return _validate_pattern(
        value, _LEASE_ID_RE, "lease_id", "^lease_[a-z0-9][a-z0-9_-]{3,63}$"
    )


def validate_schedule_id(value: str) -> str:
    """Validate canonical `sched_*` Scheduled Task identifier."""
    return _validate_pattern(
        value, _SCHEDULE_ID_RE, "schedule_id", "^sched_[a-z0-9][a-z0-9_-]{3,63}$"
    )


def validate_resource_policy_id(value: str) -> str:
    """Validate canonical `rpol_*` Resource Budget Policy identifier."""
    return _validate_pattern(
        value, _RESOURCE_POLICY_ID_RE, "policy_id", "^rpol_[a-z0-9][a-z0-9_-]{3,63}$"
    )


def validate_resource_ledger_id(value: str) -> str:
    """Validate canonical `rled_*` Resource Ledger Entry identifier."""
    return _validate_pattern(
        value, _RESOURCE_LEDGER_ID_RE, "ledger_entry_id", "^rled_[a-z0-9][a-z0-9_-]{3,63}$"
    )


def validate_resource_alert_id(value: str) -> str:
    """Validate canonical `ralt_*` Resource Threshold Alert identifier."""
    return _validate_pattern(
        value, _RESOURCE_ALERT_ID_RE, "alert_id", "^ralt_[a-z0-9][a-z0-9_-]{3,63}$"
    )


def validate_cleanup_id(value: str) -> str:
    """Validate canonical `clnp_*` Post-Task Cleanup identifier."""
    return _validate_pattern(
        value, _CLEANUP_ID_RE, "cleanup_id", "^clnp_[a-z0-9][a-z0-9_-]{3,63}$"
    )


def validate_process_handoff_id(value: str) -> str:
    """Validate canonical `phnd_*` Process Learning Handoff identifier."""
    return _validate_pattern(
        value, _PROCESS_HANDOFF_ID_RE, "handoff_id", "^phnd_[a-z0-9][a-z0-9_-]{3,63}$"
    )


def validate_session_id(value: str) -> str:
    """Validate strict lowercase UUID v4 logical Session ID per AGENT_SPEC.md Section 3.2."""
    cleaned = ensure_not_secret(value, "session_id").lower()
    if _LEGACY_SPIKE_SESSION_RE.fullmatch(cleaned):
        raise ValueError(
            "session_id must be a canonical UUID v4; store P03 spike 'sess-*' IDs in legacy_session_ref."
        )
    if not _UUID_V4_RE.fullmatch(cleaned):
        raise ValueError(f"session_id must be a valid UUID v4 string, got {value!r}.")
    return cleaned


def validate_legacy_session_ref(value: str) -> str:
    """Validate P03.01-P03.03 spike logical session reference (`sess-*`)."""
    return _validate_pattern(
        value,
        _LEGACY_SPIKE_SESSION_RE,
        "legacy_session_ref",
        "^sess-[a-z0-9][a-z0-9_-]{2,63}$",
    )


def validate_agent_id(value: str) -> str:
    """Validate `<department>.<role>` Agent identifier per AGENT_SPEC.md."""
    return _validate_pattern(
        value, _AGENT_ID_RE, "agent_id", "^[a-z0-9_]+\\.[a-z0-9_]+$"
    )


def validate_department_id(value: str) -> str:
    """Validate department identifier per DEPARTMENT_SPEC.md."""
    return _validate_pattern(value, _DEPARTMENT_ID_RE, "department", "^[a-z0-9_]+$")


def validate_snake_id(value: str, field_name: str = "identifier") -> str:
    """Validate normalized snake_case identifier (2..64 chars)."""
    return _validate_pattern(value, _SNAKE_ID_RE, field_name, "^[a-z0-9_]{2,64}$")


def validate_capability_id(value: str) -> str:
    """Validate snake_case capability tag."""
    return _validate_pattern(value, _DEPARTMENT_ID_RE, "capability_id", "^[a-z0-9_]+$")


def validate_tool_id_syntax(value: str) -> str:
    """Validate canonical ``tools.<category>.<name>`` syntax without secrets."""
    return _validate_pattern(
        value,
        _TOOL_ID_RE,
        "tool_id",
        "^tools\\.[a-z0-9_]+\\.[a-z0-9_]+$",
    )


def validate_sha256_hex(value: str) -> str:
    """Normalize and validate 64-character hexadecimal SHA-256 digest."""
    cleaned = ensure_not_secret(value, "sha256").lower()
    if not _SHA256_RE.fullmatch(cleaned):
        raise ValueError(f"sha256 must be a 64-char hex digest, got {value!r}.")
    return cleaned


def validate_semver(value: str) -> str:
    """Validate semantic version string (e.g., `1.0.0`)."""
    return _validate_pattern(value, _SEMVER_RE, "version", "SemVer MAJOR.MINOR.PATCH")


def validate_credential_ref(value: str) -> str:
    """Validate non-secret backend credential reference handle."""
    cleaned = ensure_not_secret(value, "credential_ref")
    if any(ch.isspace() for ch in cleaned) or "=" in cleaned:
        raise ValueError("credential_ref must be a clean reference handle without spaces or '='.")
    if not _CREDENTIAL_REF_RE.fullmatch(cleaned):
        raise ValueError(
            "credential_ref must use an opaque handle format such as 'env:VAR_NAME' or 'ref:scope/name'."
        )
    return cleaned


def validate_aware_datetime(value: datetime, field_name: str) -> datetime:
    """Require timezone-aware datetime stamps; reject naive datetimes."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone information (timezone-aware UTC/ISO 8601).")
    return value


def validate_chronological(
    earlier: datetime | None,
    later: datetime | None,
    earlier_name: str,
    later_name: str,
) -> None:
    """Verify two optional timestamps obey existence dependency and chronological ordering."""
    if later is None:
        return
    if earlier is None:
        raise ValueError(f"{later_name} cannot be set without {earlier_name}.")
    if later < earlier:
        raise ValueError(f"{later_name} ({later.isoformat()}) precedes {earlier_name} ({earlier.isoformat()}).")


def validate_safe_relative_path(raw_path: str, field_name: str) -> str:
    """Validate clean workspace-relative POSIX paths purely syntactically without disk I/O."""
    cleaned = ensure_not_secret(raw_path, field_name)
    if "*" in cleaned or "?" in cleaned:
        raise ValueError(f"{field_name} must not contain wildcard characters: {raw_path!r}")
    if cleaned.startswith(("/", "\\")) or _DRIVE_OR_SCHEME_RE.match(cleaned):
        raise ValueError(f"{field_name} must be a relative path, got {raw_path!r}")
    normalized = cleaned.replace("\\", "/")
    posix = PurePosixPath(normalized)
    if posix.is_absolute() or ".." in posix.parts or "." in posix.parts:
        raise ValueError(f"{field_name} contains invalid traversal segments: {raw_path!r}")
    return posix.as_posix()


def validate_unique_id_list(
    values: list[str],
    item_validator: Callable[[str], str],
    field_name: str,
) -> list[str]:
    """Validate every identifier in a list and reject duplicate entries."""
    validated: list[str] = []
    seen: set[str] = set()
    for raw in values:
        item = item_validator(raw)
        if item in seen:
            raise ValueError(f"{field_name} contains duplicate identifier '{item}'.")
        seen.add(item)
        validated.append(item)
    return validated


def validate_safe_metadata(
    value: dict[str, str | int | float | bool | None],
    field_name: str = "metadata",
) -> dict[str, str | int | float | bool | None]:
    """Reject forbidden secret key names or credential values in flat metadata maps."""
    for key, item in value.items():
        if _is_sensitive_key(key):
            raise ValueError(f"{field_name} must never contain sensitive key '{key}'.")
        if isinstance(item, str):
            reject_secret_text(item, f"{field_name}[{key}]")
    return value


def validate_safe_payload(value: Any, path: str = "redacted_payload") -> Any:
    """Recursively reject secret keys and credential patterns inside arbitrary event payloads."""
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} keys must be strings, got {type(key).__name__}.")
            if _is_sensitive_key(key):
                if item == _REDACTED_KEY_SENTINEL:
                    continue
                raise ValueError(f"{path}.{key} is forbidden because the key implies secret material.")
            validate_safe_payload(item, f"{path}.{key}")
        return value
    if isinstance(value, list):
        for idx, item in enumerate(value):
            validate_safe_payload(item, f"{path}[{idx}]")
        return value
    if isinstance(value, str):
        reject_secret_text(value, path)
    return value


def sanitize_payload(value: Any) -> Any:
    """Return a deep-copied payload with secret keys and token strings redacted."""
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if _is_sensitive_key(key_str):
                sanitized[key_str] = _REDACTED_KEY_SENTINEL
            else:
                sanitized[key_str] = sanitize_payload(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    if isinstance(value, str):
        scrubbed = redact_secrets(value)
        scrubbed = _EMBEDDED_SECRET_RE.sub("[REDACTED_SECRET]", scrubbed)
        if scrubbed.strip().lower().startswith(_RAW_SECRET_PREFIXES):
            return "[REDACTED_SECRET]"
        return scrubbed
    return value


def scrub_secret_text(value: str) -> str:
    """Redact credentials cleanly so scrubbed text contains zero secret prefixes or values."""
    scrubbed = str(sanitize_payload(value))
    scrubbed = re.sub(
        r"(?i)(?:aiza|sk-|ghp_|gho_|github_pat_|glpat-|xox[bp]-|bearer\s+)\[REDACTED(?:_SECRET)?\]",
        "[REDACTED_SECRET]",
        scrubbed,
    )
    return scrubbed


TaskId = Annotated[str, AfterValidator(validate_task_id)]
RunId = Annotated[str, AfterValidator(validate_run_id)]
ArtifactId = Annotated[str, AfterValidator(validate_artifact_id)]
EventId = Annotated[str, AfterValidator(validate_event_id)]
ChecklistItemId = Annotated[str, AfterValidator(validate_checklist_item_id)]
NotificationId = Annotated[str, AfterValidator(validate_notification_id)]
ProjectId = Annotated[str, AfterValidator(validate_project_id)]
EvidenceId = Annotated[str, AfterValidator(validate_evidence_id)]
ConversationId = Annotated[str, AfterValidator(validate_conversation_id)]
IntakeId = Annotated[str, AfterValidator(validate_intake_id)]
RefinementId = Annotated[str, AfterValidator(validate_refinement_id)]
TurnId = Annotated[str, AfterValidator(validate_turn_id)]
InterruptId = Annotated[str, AfterValidator(validate_interrupt_id)]
IncidentId = Annotated[str, AfterValidator(validate_incident_id)]
EscalationId = Annotated[str, AfterValidator(validate_escalation_id)]
QueueEntryId = Annotated[str, AfterValidator(validate_queue_entry_id)]
LeaseId = Annotated[str, AfterValidator(validate_lease_id)]
ScheduleId = Annotated[str, AfterValidator(validate_schedule_id)]
ResourcePolicyId = Annotated[str, AfterValidator(validate_resource_policy_id)]
ResourceLedgerId = Annotated[str, AfterValidator(validate_resource_ledger_id)]
ResourceAlertId = Annotated[str, AfterValidator(validate_resource_alert_id)]
CleanupId = Annotated[str, AfterValidator(validate_cleanup_id)]
ProcessHandoffId = Annotated[str, AfterValidator(validate_process_handoff_id)]
SessionId = Annotated[str, AfterValidator(validate_session_id)]
LegacySpikeSessionRef = Annotated[str, AfterValidator(validate_legacy_session_ref)]
AgentId = Annotated[str, AfterValidator(validate_agent_id)]
DepartmentId = Annotated[str, AfterValidator(validate_department_id)]
SnakeId = Annotated[str, AfterValidator(validate_snake_id)]
CapabilityId = Annotated[str, AfterValidator(validate_capability_id)]
Sha256Hex = Annotated[str, AfterValidator(validate_sha256_hex)]
SemVer = Annotated[str, AfterValidator(validate_semver)]
CredentialRefHandle = Annotated[str, AfterValidator(validate_credential_ref)]
