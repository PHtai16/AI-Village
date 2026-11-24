"""Session isolation, permission path rules, and workspace boundary management."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from pathlib import Path
import shutil
import tempfile
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Sequence
import uuid

from engines.antigravity.models import (
    AntigravityCancelOutcome,
    AntigravityError,
    AntigravityExecutionStatus,
    AntigravityHandshakeResult,
    AntigravitySessionConfig,
    AntigravitySessionEvent,
    AntigravitySessionTurnResult,
    AntigravityStreamEvent,
    AntigravityStreamRequest,
    ArtifactVerificationResult,
    SessionExecutionContext,
)
from engines.antigravity.verification import (
    has_symlink_or_junction_component,
    is_symlink_or_junction,
    verify_artifact,
)

if TYPE_CHECKING:
    from engines.antigravity.adapter import AntigravityAdapter
    from engines.antigravity.lifecycle import AntigravityProcessManager

logger = logging.getLogger(__name__)


class SessionIsolationError(ValueError):
    """Domain validation error carrying a structured session isolation error code."""

    def __init__(self, error_code: str, message: str) -> None:
        """Initialize the error with a standardized isolation code and message."""
        super().__init__(f"{error_code}: {message}")
        self.error_code = error_code


@dataclass
class _ManagedSession:
    """Internal mutable holder guarded by ``AntigravitySessionManager`` lock."""

    config: AntigravitySessionConfig
    context: SessionExecutionContext
    canonical_workspace: Path
    canonical_root: Path


@dataclass
class _ActiveSessionTurn:
    """Lifecycle and event state associated with one started session turn."""

    execution_id: str
    session_id: str
    turn_index: int
    target_conv_id: str | None
    resume_conversation: bool
    deny_rules_count: int
    settings_applied: bool
    session_events: list[AntigravitySessionEvent] = field(default_factory=list)
    finalized: bool = False
    turn_result: AntigravitySessionTurnResult | None = None


def normalize_path_for_agy_permission(path: Path) -> str:
    """Normalize a host filesystem path into Antigravity permission rule format.

    Antigravity strips Windows drive letters and converts backslashes to forward
    slashes before matching ``read_file(...)`` and ``write_file(...)`` rules.

    Args:
        path: Host path to normalize.

    Returns:
        Normalized POSIX-style rule target without a drive letter.
    """
    resolved = str(path.resolve()).removeprefix("\\\\?\\")
    if len(resolved) >= 2 and resolved[1] == ":":
        resolved = resolved[2:]
    normalized = resolved.replace("\\", "/")
    return normalized or "/"


def build_ephemeral_deny_settings(
    denied_paths: Sequence[Path],
    *,
    deny_shell_commands: bool = True,
) -> dict[str, Any]:
    """Build an ephemeral ``agy --settings`` JSON object blocking sibling paths and shells.

    Args:
        denied_paths: Host files or directories to block from ``read_file`` and ``write_file``.
        deny_shell_commands: When True, adds ``command(*)`` to the deny list.

    Returns:
        Dictionary matching ``permissions.deny`` configuration schema.
    """
    rules: list[str] = []
    seen: set[str] = set()

    for candidate in denied_paths:
        target = normalize_path_for_agy_permission(candidate)
        for rule in (f"read_file({target})", f"write_file({target})"):
            if rule not in seen:
                seen.add(rule)
                rules.append(rule)

    if deny_shell_commands and "command(*)" not in seen:
        rules.append("command(*)")

    return {"permissions": {"deny": rules}}


def paths_overlap(path_a: Path, path_b: Path) -> bool:
    """Return True when two canonical paths are identical or nested inside one another."""
    canon_a = path_a.resolve()
    canon_b = path_b.resolve()
    return canon_a == canon_b or canon_a.is_relative_to(canon_b) or canon_b.is_relative_to(canon_a)


def validate_session_workspace(
    workspace_dir: Path,
    allowed_root: Path,
    *,
    allow_temp_workspace: bool = False,
) -> Path:
    """Validate that a session workspace sits safely inside ``allowed_root`` without escapes.

    Args:
        workspace_dir: Dedicated directory assigned to the logical session.
        allowed_root: Parent root enclosing authorized session workspaces.
        allow_temp_workspace: Reject OS temp directories by default because CLI built-in
            permissions treat OS temp folders as shared workspace-accessible dirs.

    Returns:
        Canonical resolved ``Path`` for ``workspace_dir``.

    Raises:
        SessionIsolationError: With code ``INVALID_WORKSPACE_BOUNDARY`` or
            ``SYMLINK_ESCAPE_REJECTED`` if any boundary check fails.
    """
    if not allowed_root.is_dir():
        raise SessionIsolationError(
            "INVALID_WORKSPACE_BOUNDARY",
            f"Allowed root directory does not exist: {allowed_root}",
        )
    if is_symlink_or_junction(allowed_root.absolute()):
        raise SessionIsolationError(
            "SYMLINK_ESCAPE_REJECTED",
            f"Allowed root must not be a symlink or junction: {allowed_root}",
        )
    if not workspace_dir.exists() or not workspace_dir.is_dir():
        raise SessionIsolationError(
            "INVALID_WORKSPACE_BOUNDARY",
            f"Session workspace directory does not exist: {workspace_dir}",
        )
    if has_symlink_or_junction_component(workspace_dir, stop_at=allowed_root):
        raise SessionIsolationError(
            "SYMLINK_ESCAPE_REJECTED",
            f"Session workspace contains a symlink or junction component: {workspace_dir}",
        )

    canonical_root = allowed_root.resolve()
    canonical_ws = workspace_dir.resolve()
    if not canonical_ws.is_relative_to(canonical_root):
        raise SessionIsolationError(
            "INVALID_WORKSPACE_BOUNDARY",
            f"Workspace '{canonical_ws}' resolves outside allowed root '{canonical_root}'.",
        )
    if canonical_ws == canonical_root:
        raise SessionIsolationError(
            "INVALID_WORKSPACE_BOUNDARY",
            "Session workspace must be a dedicated subdirectory, not the allowed root itself.",
        )

    if not allow_temp_workspace:
        temp_root = Path(tempfile.gettempdir()).resolve()
        if canonical_ws.is_relative_to(temp_root):
            raise SessionIsolationError(
                "INVALID_WORKSPACE_BOUNDARY",
                "OS temp directories are rejected for isolated Antigravity sessions because "
                "built-in permissions share temp space across workspaces.",
            )

    return canonical_ws


def _safe_workspace_id(canonical_ws: Path, canonical_root: Path) -> str:
    """Derive a non-sensitive relative workspace identifier inside ``canonical_root``."""
    try:
        return canonical_ws.relative_to(canonical_root).as_posix()
    except ValueError:
        return canonical_ws.name


class AntigravitySessionManager:
    """Thread-safe manager isolating workspaces, conversation bindings, and event streams."""

    def __init__(
        self,
        adapter: AntigravityAdapter | None = None,
        process_manager: AntigravityProcessManager | None = None,
    ) -> None:
        """Initialize the session manager around an adapter and lifecycle manager."""
        if adapter is None:
            from engines.antigravity.adapter import AntigravityAdapter as _AdapterCls

            self._adapter: AntigravityAdapter = _AdapterCls()
        else:
            self._adapter = adapter

        if process_manager is None:
            from engines.antigravity.lifecycle import AntigravityProcessManager as _ProcMgrCls

            self._process_manager: AntigravityProcessManager = _ProcMgrCls(adapter=self._adapter)
        else:
            self._process_manager = process_manager

        self._lock = threading.RLock()
        self._sessions: dict[str, _ManagedSession] = {}
        self._all_session_ids: set[str] = set()
        self._conversation_owners: dict[str, str] = {}
        self._turns: dict[str, _ActiveSessionTurn] = {}

    @property
    def process_manager(self) -> AntigravityProcessManager:
        """Return the underlying process lifecycle manager."""
        return self._process_manager

    def create_session(self, config: AntigravitySessionConfig) -> SessionExecutionContext:
        """Register a new logical session bound to an exclusive workspace directory."""
        canonical_ws = validate_session_workspace(
            Path(config.workspace_path),
            Path(config.allowed_root_path),
            allow_temp_workspace=config.allow_temp_workspace,
        )
        canonical_root = Path(config.allowed_root_path).resolve()

        with self._lock:
            if config.session_id in self._all_session_ids:
                raise SessionIsolationError(
                    "DUPLICATE_SESSION_ID",
                    f"Session ID '{config.session_id}' has already been registered.",
                )

            for other_id, other in self._sessions.items():
                if not other.context.closed and paths_overlap(canonical_ws, other.canonical_workspace):
                    raise SessionIsolationError(
                        "WORKSPACE_OVERLAP",
                        f"Workspace '{canonical_ws}' overlaps active session '{other_id}'.",
                    )

            context = SessionExecutionContext(
                session_id=config.session_id,
                workspace_id=_safe_workspace_id(canonical_ws, canonical_root),
                canonical_workspace_path=str(canonical_ws),
                allowed_root_path=str(canonical_root),
                provider_conversation_id=None,
                conversation_binding_source="none",
                created_at=datetime.now(timezone.utc),
                turn_count=0,
                closed=False,
            )
            self._all_session_ids.add(config.session_id)
            self._sessions[config.session_id] = _ManagedSession(
                config=config,
                context=context,
                canonical_workspace=canonical_ws,
                canonical_root=canonical_root,
            )
            return context.model_copy(deep=True)

    def get_session(self, session_id: str) -> SessionExecutionContext:
        """Return a snapshot of the session context for ``session_id``."""
        with self._lock:
            managed = self._sessions.get(session_id)
            if managed is None:
                raise SessionIsolationError(
                    "INVALID_SESSION_ID",
                    f"Unknown session ID: {session_id}",
                )
            return managed.context.model_copy(deep=True)

    def list_sessions(self) -> list[SessionExecutionContext]:
        """Return snapshots of all active or closed registered sessions."""
        with self._lock:
            return [entry.context.model_copy(deep=True) for entry in self._sessions.values()]

    def close_session(
        self,
        session_id: str,
        *,
        cleanup_workspace: bool = False,
        cancel_active: bool = False,
    ) -> None:
        """Close a session, optionally cancelling active runs before workspace removal."""
        with self._lock:
            managed = self._sessions.get(session_id)
            if managed is None:
                raise SessionIsolationError(
                    "INVALID_SESSION_ID",
                    f"Unknown session ID: {session_id}",
                )
            active_ids = self._process_manager.active_execution_ids(session_id)
            if active_ids and not cancel_active:
                raise SessionIsolationError(
                    "EXECUTION_CONFLICT",
                    f"Cannot close session '{session_id}' while active executions {active_ids} are running.",
                )
            managed.context.closed = True
            ws_to_remove = managed.canonical_workspace
            allowed_root = managed.canonical_root
            allow_temp = managed.config.allow_temp_workspace

        if active_ids and cancel_active:
            for exec_id in active_ids:
                self._process_manager.request_cancel(
                    exec_id,
                    owner_token=session_id,
                    wait=True,
                    wait_timeout=10.0,
                )
                try:
                    self.await_turn(exec_id, timeout=5.0)
                except TimeoutError:
                    pass
            if self._process_manager.has_active_execution(session_id):
                raise SessionIsolationError(
                    "EXECUTION_CONFLICT",
                    f"Session '{session_id}' still has running executions after cancellation.",
                )

        if cleanup_workspace and ws_to_remove.exists():
            validate_session_workspace(
                ws_to_remove,
                allowed_root,
                allow_temp_workspace=allow_temp,
            )
            for attempt in range(3):
                if not ws_to_remove.exists():
                    break
                shutil.rmtree(ws_to_remove, ignore_errors=True)
                if ws_to_remove.exists() and attempt < 2:
                    time.sleep(0.35)

    def start_turn(
        self,
        session_id: str,
        prompt: str,
        *,
        resume_conversation: bool = False,
        extra_denied_paths: Sequence[Path] = (),
        on_event: Callable[[AntigravitySessionEvent], None] | None = None,
        timeout_seconds: int | None = None,
        hard_timeout_seconds: float | None = None,
        grace_seconds: float = 3.0,
        max_events: int = 10000,
    ) -> str:
        """Start an isolated CLI turn asynchronously under ``session_id`` and return execution ID."""
        exec_id = f"exec-{uuid.uuid4().hex[:12]}"
        with self._lock:
            managed = self._sessions.get(session_id)
            if managed is None:
                raise SessionIsolationError(
                    "INVALID_SESSION_ID",
                    f"Unknown session ID: {session_id}",
                )
            if managed.context.closed:
                raise SessionIsolationError(
                    "INVALID_SESSION_ID",
                    f"Session '{session_id}' is already closed.",
                )

            cfg = managed.config
            target_conv_id: str | None = None
            if resume_conversation:
                target_conv_id = managed.context.provider_conversation_id
                if not target_conv_id:
                    raise SessionIsolationError(
                        "CROSS_SESSION_CONVERSATION_VIOLATION",
                        f"Cannot resume session '{session_id}' without a verified conversation_id.",
                    )

            managed.context.turn_count += 1
            turn_index = managed.context.turn_count

            sibling_paths: list[Path] = [
                other.canonical_workspace
                for other_id, other in self._sessions.items()
                if other_id != session_id and not other.context.closed
            ]
            denied_paths = [str(path) for path in sibling_paths]
            for extra in extra_denied_paths:
                denied_paths.append(str(Path(extra).resolve()))

            effective_timeout = timeout_seconds if timeout_seconds is not None else cfg.timeout_seconds
            settings_preview = (
                build_ephemeral_deny_settings(
                    [Path(p) for p in denied_paths],
                    deny_shell_commands=cfg.deny_shell_commands,
                )
                if (denied_paths or cfg.deny_shell_commands)
                else None
            )
            deny_rules_count = (
                len(settings_preview["permissions"]["deny"])
                if settings_preview is not None
                else 0
            )
            settings_applied = settings_preview is not None

            turn_record = _ActiveSessionTurn(
                execution_id=exec_id,
                session_id=session_id,
                turn_index=turn_index,
                target_conv_id=target_conv_id,
                resume_conversation=bool(resume_conversation and target_conv_id),
                deny_rules_count=deny_rules_count,
                settings_applied=settings_applied,
            )
            self._turns[exec_id] = turn_record

        def _forward_event(stream_event: AntigravityStreamEvent) -> None:
            wrapped = AntigravitySessionEvent(
                session_id=session_id,
                session_id_source="adapter_session_context",
                execution_id=exec_id,
                provider_conversation_id=stream_event.conversation_id or target_conv_id,
                turn_index=turn_index,
                event=stream_event,
            )
            with self._lock:
                turn_record.session_events.append(wrapped)
            if on_event is not None:
                on_event(wrapped)

        request = AntigravityStreamRequest(
            prompt=prompt,
            workspace_path=str(managed.canonical_workspace),
            timeout_seconds=effective_timeout,
            require_sandbox=cfg.require_sandbox,
            execution_mode=cfg.execution_mode,
            model=cfg.model,
            conversation_id=target_conv_id,
            denied_read_write_paths=denied_paths,
            deny_shell_commands=cfg.deny_shell_commands,
            max_payload_chars=cfg.max_payload_chars,
            max_line_bytes=cfg.max_line_bytes,
        )
        return self._process_manager.start_execution(
            request,
            execution_id=exec_id,
            session_id=session_id,
            owner_token=session_id,
            on_event=_forward_event,
            hard_timeout_seconds=hard_timeout_seconds,
            grace_seconds=grace_seconds,
            max_events=max_events,
        )

    def get_turn_status(self, execution_id: str) -> AntigravityExecutionStatus:
        """Return live lifecycle status for a started session turn."""
        return self._process_manager.get_execution_status(execution_id)

    def cancel_turn(
        self,
        execution_id: str,
        *,
        wait: bool = False,
        wait_timeout: float = 10.0,
    ) -> AntigravityCancelOutcome:
        """Request cancellation scoped exclusively to ``execution_id``'s owning session."""
        from engines.antigravity.lifecycle import ProcessLifecycleError

        with self._lock:
            turn_record = self._turns.get(execution_id)
            if turn_record is None:
                raise ProcessLifecycleError(
                    "EXECUTION_NOT_FOUND",
                    f"Unknown session turn execution_id: {execution_id}",
                )
            owner_session = turn_record.session_id

        return self._process_manager.request_cancel(
            execution_id,
            owner_token=owner_session,
            wait=wait,
            wait_timeout=wait_timeout,
        )

    def await_turn(
        self,
        execution_id: str,
        *,
        timeout: float | None = None,
    ) -> AntigravitySessionTurnResult:
        """Await a started turn, finalize conversation ownership, and return normalized result."""
        from engines.antigravity.lifecycle import ProcessLifecycleError

        with self._lock:
            turn_record = self._turns.get(execution_id)
            if turn_record is None:
                raise ProcessLifecycleError(
                    "EXECUTION_NOT_FOUND",
                    f"Unknown session turn execution_id: {execution_id}",
                )
            if turn_record.finalized and turn_record.turn_result is not None:
                return turn_record.turn_result

        stream_result = self._process_manager.await_result(execution_id, timeout=timeout)

        with self._lock:
            if turn_record.finalized and turn_record.turn_result is not None:
                return turn_record.turn_result

            managed = self._sessions[turn_record.session_id]
            isolation_error: AntigravityError | None = None
            observed_conv = stream_result.conversation_id
            if observed_conv:
                owner = self._conversation_owners.get(observed_conv)
                if owner is not None and owner != turn_record.session_id:
                    isolation_error = AntigravityError(
                        error_code="CROSS_SESSION_CONVERSATION_VIOLATION",
                        message=(
                            f"Provider conversation_id '{observed_conv}' belongs to session "
                            f"'{owner}', not '{turn_record.session_id}'."
                        ),
                        retryable=False,
                        error_source="adapter",
                    )
                elif (
                    turn_record.resume_conversation
                    and turn_record.target_conv_id
                    and observed_conv != turn_record.target_conv_id
                ):
                    isolation_error = AntigravityError(
                        error_code="CROSS_SESSION_CONVERSATION_VIOLATION",
                        message=(
                            f"Session '{turn_record.session_id}' expected resumed conversation "
                            f"'{turn_record.target_conv_id}', but provider returned '{observed_conv}'."
                        ),
                        retryable=False,
                        error_source="adapter",
                    )
                else:
                    self._conversation_owners[observed_conv] = turn_record.session_id
                    managed.context.provider_conversation_id = observed_conv
                    managed.context.conversation_binding_source = "provider_cli"

            turn_result = AntigravitySessionTurnResult(
                session_id=turn_record.session_id,
                execution_id=execution_id,
                turn_index=turn_record.turn_index,
                provider_conversation_id=observed_conv,
                resumed_conversation=turn_record.resume_conversation,
                enforced_deny_rules_count=turn_record.deny_rules_count,
                enforced_settings_applied=turn_record.settings_applied and stream_result.status != "BLOCKED",
                stream_result=stream_result,
                session_events=list(turn_record.session_events),
                isolation_error=isolation_error,
            )
            turn_record.finalized = True
            turn_record.turn_result = turn_result
            return turn_result

    def run_turn(
        self,
        session_id: str,
        prompt: str,
        *,
        resume_conversation: bool = False,
        extra_denied_paths: Sequence[Path] = (),
        on_event: Callable[[AntigravitySessionEvent], None] | None = None,
        timeout_seconds: int | None = None,
    ) -> AntigravitySessionTurnResult:
        """Execute one isolated CLI turn synchronously under ``session_id``."""
        exec_id = self.start_turn(
            session_id,
            prompt,
            resume_conversation=resume_conversation,
            extra_denied_paths=extra_denied_paths,
            on_event=on_event,
            timeout_seconds=timeout_seconds,
        )
        return self.await_turn(exec_id)

    def verify_session_artifact(
        self,
        session_id: str,
        *,
        artifact_relpath: str,
        turn_result: AntigravitySessionTurnResult,
        expected_content: str | None = None,
        required_substrings: Sequence[str] = (),
        protected_source_hashes: dict[str, str] | None = None,
    ) -> ArtifactVerificationResult:
        """Independently verify an output artifact inside ``session_id``'s workspace."""
        with self._lock:
            managed = self._sessions.get(session_id)
            if managed is None:
                raise SessionIsolationError(
                    "INVALID_SESSION_ID",
                    f"Unknown session ID: {session_id}",
                )
            workspace_dir = managed.canonical_workspace

        stream_res = turn_result.stream_result
        cli_status = "SUCCEEDED" if stream_res.status == "SUCCEEDED" else "FAILED"
        cli_proxy = AntigravityHandshakeResult(
            status=cli_status,  # type: ignore[arg-type]
            executable_path=stream_res.executable_path,
            version=stream_res.version,
            command_summary=stream_res.command_summary,
            started_at=stream_res.started_at,
            finished_at=stream_res.finished_at,
            duration_ms=stream_res.duration_ms,
            exit_code=stream_res.exit_code,
            response_text=stream_res.response_text,
            provider_status=stream_res.provider_status,
            conversation_id=stream_res.conversation_id,
            model_id=stream_res.model_id,
            num_turns=stream_res.num_turns,
            telemetry=stream_res.telemetry,
            error=stream_res.error,
            denied_actions=stream_res.denied_actions,
            redacted_stderr=stream_res.redacted_stderr,
        )
        return verify_artifact(
            workspace_dir=workspace_dir,
            artifact_relpath=artifact_relpath,
            cli_result=cli_proxy,
            expected_content=expected_content,
            required_substrings=required_substrings,
            protected_source_hashes=protected_source_hashes,
        )
