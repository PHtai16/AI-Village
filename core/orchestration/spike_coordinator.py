"""Fail-closed coordinator for the P03.01 Master -> Worker -> QA -> Master spike."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any, Callable, Sequence
import uuid

from pydantic import ValidationError

from core.orchestration.spike_models import (
    AttributedSpikeEvent,
    MasterPlan,
    MasterSynthesis,
    MasterWorkerSpikeResult,
    OrchestrationError,
    SpikeAgentSpec,
    WorkerExecutionRecord,
    WorkerHandoff,
    _validate_clean_relpath,
    build_default_spike_roster,
    parse_strict_json_object,
)
from engines.antigravity.diagnostics import redact_secrets
from engines.antigravity.sessions import (
    SessionIsolationError,
    paths_overlap,
    validate_session_workspace,
)
from engines.antigravity.verification import (
    compute_sha256_file,
    resolve_within_workspace,
    snapshot_file_hashes,
)
from engines.contract import (
    EngineEvent,
    EngineRequest,
    EngineResult,
    EngineSessionReference,
)
from engines.registry import EngineRegistry
from engines.verification import EngineArtifactVerificationResult, verify_engine_artifact

logger = logging.getLogger(__name__)


@dataclass
class _SpikeEventCollector:
    """Thread-safe sequential wrapper attributing ``EngineEvent`` instances to a run stage."""

    downstream: Callable[[AttributedSpikeEvent], None] | None = None
    items: list[AttributedSpikeEvent] = field(default_factory=list)

    def make_callback(
        self,
        *,
        stage: str,
        agent_id: str,
        role: str,
        logical_session_id: str,
    ) -> Callable[[EngineEvent], None]:
        """Return an ``on_event`` callback binding stage and agent attribution."""

        def _handle(event: EngineEvent) -> None:
            conv_id_raw = event.redacted_payload.get("conversation_id")
            conv_id = str(conv_id_raw) if isinstance(conv_id_raw, str) and conv_id_raw else None
            attributed = AttributedSpikeEvent(
                sequence=len(self.items) + 1,
                stage=stage,  # type: ignore[arg-type]
                agent_id=agent_id,
                role=role,  # type: ignore[arg-type]
                logical_session_id=logical_session_id,
                provider_conversation_id=conv_id,
                engine_event=event,
            )
            self.items.append(attributed)
            if self.downstream is not None:
                self.downstream(attributed)

        return _handle

    def backfill_conversation_id(self, *, stage: str, conversation_id: str | None) -> None:
        """Populate ``provider_conversation_id`` on collected stage events after completion."""
        if not conversation_id:
            return
        for idx, item in enumerate(self.items):
            if item.stage == stage and item.provider_conversation_id is None:
                self.items[idx] = item.model_copy(
                    update={"provider_conversation_id": conversation_id}
                )


def _snapshot_workspace_tree(workspace_dir: Path) -> dict[str, str]:
    """Hash every regular file inside ``workspace_dir`` relative to its root."""
    root = workspace_dir.resolve()
    hashes: dict[str, str] = {}
    if not root.is_dir():
        return hashes
    for file_path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel_posix = file_path.relative_to(root).as_posix()
        hashes[rel_posix] = compute_sha256_file(file_path)
    return hashes


def _elapsed_ms(started_at: datetime, finished_at: datetime) -> int:
    """Compute non-negative integer duration in milliseconds."""
    return max(0, int((finished_at - started_at).total_seconds() * 1000))


def _build_master_plan_prompt(
    *,
    user_request: str,
    master_spec: SpikeAgentSpec,
    permitted_read_relpaths: Sequence[str],
    permitted_write_relpaths: Sequence[str],
    expected_artifact_relpath: str,
) -> str:
    """Construct the planning prompt for ``core.master`` requiring strict JSON output."""
    schema_example = {
        "schema_version": "1.0",
        "task_summary": "concise summary of the task",
        "target_worker_id": master_spec.allowed_handoff_targets[0],
        "worker_objective": "clear step-by-step objective for the worker without shell usage",
        "allowed_read_paths": list(permitted_read_relpaths),
        "allowed_write_paths": list(permitted_write_relpaths),
        "forbidden_actions": [
            "Execute shell or terminal commands",
            "Modify or overwrite protected read-only files",
            "Access paths outside the worker workspace",
        ],
        "acceptance_criteria": [
            "All protected input files stay unmodified",
            f"Artifact {expected_artifact_relpath} exists with accurate extracted values",
        ],
        "artifact_expectation": {
            "required_substrings": ["name="],
            "expected_line_count": 2,
            "must_preserve_inputs": True,
        },
    }
    return (
        f"You are {master_spec.agent_id} (system_role={master_spec.system_role}, "
        f"department={master_spec.department}).\n"
        "Analyze the user request below and produce ONLY a delegation plan as a single raw JSON object.\n"
        "Constraints:\n"
        "- Do NOT run any shell or terminal commands.\n"
        "- Do NOT read, create, or modify any files.\n"
        f"- Choose target_worker_id strictly from: {list(master_spec.allowed_handoff_targets)}.\n"
        f"- Choose allowed_read_paths strictly as a subset of: {list(permitted_read_relpaths)}.\n"
        f"- Choose allowed_write_paths strictly matching: {list(permitted_write_relpaths)} "
        f"(must include '{expected_artifact_relpath}').\n"
        "- Return ONLY valid JSON with NO markdown code fences and NO extra commentary.\n"
        "- Do NOT add any keys outside this exact JSON schema:\n"
        f"{json.dumps(schema_example, indent=2)}\n\n"
        f"User Request:\n{user_request}"
    )


def _build_worker_prompt(handoff: WorkerHandoff) -> str:
    """Construct an explicit bounded execution prompt from a validated ``WorkerHandoff``."""
    return (
        f"You are {handoff.to_agent_id} executing handoff_id={handoff.handoff_id}.\n"
        f"Task Summary: {handoff.plan.task_summary}\n"
        f"Objective: {handoff.plan.worker_objective}\n\n"
        "Strict Operating Boundaries:\n"
        f"- You may ONLY read workspace-relative paths: {handoff.plan.allowed_read_paths}\n"
        f"- You may ONLY create or write workspace-relative paths: {handoff.plan.allowed_write_paths}\n"
        "- Do NOT modify protected read-only source files.\n"
        "- Do NOT run any shell, terminal, PowerShell, or external network commands.\n"
        "- Use ONLY file read and file write tools in the current workspace.\n"
        "- Forbidden actions: "
        + "; ".join(handoff.plan.forbidden_actions or ["None beyond workspace bounds"])
        + "\n"
        "- Acceptance criteria: "
        + "; ".join(handoff.plan.acceptance_criteria)
        + "\n\n"
        "After writing the artifact, read it back to verify its exact contents and summarize."
    )


def _build_master_synthesis_prompt(
    *,
    user_request: str,
    master_spec: SpikeAgentSpec,
    handoff: WorkerHandoff,
    worker_result: EngineResult,
    qa_verification: EngineArtifactVerificationResult,
) -> str:
    """Construct the post-QA synthesis prompt grounded in verified filesystem facts."""
    worker_excerpt = redact_secrets((worker_result.response_text or "")[:600])
    qa_facts = {
        "qa_status": qa_verification.status,
        "engine_execution_status": qa_verification.engine_execution_status,
        "artifact_relpath": qa_verification.artifact_relpath,
        "artifact_exists": qa_verification.artifact_exists,
        "within_workspace": qa_verification.within_workspace,
        "content_verified": qa_verification.content_verified,
        "hash_verified": qa_verification.hash_verified,
        "source_integrity_verified": qa_verification.source_integrity_verified,
        "file_size_bytes": qa_verification.file_size_bytes,
        "actual_sha256": qa_verification.actual_sha256,
        "failure_reasons": qa_verification.failure_reasons,
    }
    schema_example = {
        "schema_version": "1.0",
        "delivery_status": "DELIVERED" if qa_verification.status == "VERIFIED_PASS" else "REJECTED",
        "qa_status_observed": qa_verification.status,
        "worker_status_observed": worker_result.status,
        "artifact_relpath": qa_verification.artifact_relpath,
        "artifact_sha256": qa_verification.actual_sha256,
        "final_summary": "concise factual summary grounded strictly in QA results",
        "verified_facts": ["factual statement"],
        "unverified_or_failed_reasons": qa_verification.failure_reasons,
    }
    return (
        f"You are {master_spec.agent_id} performing post-QA final synthesis.\n"
        "Rules:\n"
        "- Do NOT run any shell commands or edit any files.\n"
        "- Your JSON MUST match the independent QA verification record truthfully.\n"
        "- NEVER set delivery_status='DELIVERED' unless qa_status == 'VERIFIED_PASS'.\n"
        "- When qa_status == 'VERIFIED_PASS', set qa_status_observed='VERIFIED_PASS', "
        "delivery_status='DELIVERED', and artifact_sha256 to the exact actual_sha256 below.\n"
        "- When qa_status == 'VERIFIED_FAIL', set qa_status_observed='VERIFIED_FAIL' and "
        "delivery_status='REJECTED'.\n"
        "- Return ONLY a single valid JSON object with NO markdown fences and NO prose.\n"
        "- Do NOT add extra keys beyond this exact schema:\n"
        f"{json.dumps(schema_example, indent=2)}\n\n"
        f"Original User Request:\n{user_request}\n\n"
        f"Worker Handoff ID: {handoff.handoff_id} (worker={handoff.to_agent_id}, "
        f"observed_tools={worker_result.observed_tool_names})\n"
        f"Worker Self-Report Excerpt (UNTRUSTED unless QA passed): {worker_excerpt!r}\n\n"
        f"Independent QA Verification Record (AUTHORITATIVE):\n{json.dumps(qa_facts, indent=2)}"
    )


def _validate_policy_paths(
    *,
    worker_ws: Path,
    planned_reads: Sequence[str],
    planned_writes: Sequence[str],
    permitted_reads: Sequence[str],
    permitted_writes: Sequence[str],
    expected_artifact_relpath: str,
) -> list[str]:
    """Validate that planned read/write paths stay strictly inside permitted policy bounds."""
    violations: list[str] = []
    allowed_read_set = set(permitted_reads)
    allowed_write_set = set(permitted_writes)
    resolved_read_map = {resolve_within_workspace(worker_ws, p): p for p in permitted_reads}
    resolved_write_map = {resolve_within_workspace(worker_ws, p): p for p in permitted_writes}

    for rel in planned_reads:
        try:
            target = resolve_within_workspace(worker_ws, rel)
        except ValueError as exc:
            violations.append(f"Invalid allowed_read_paths entry '{rel}': {exc}")
            continue
        if rel not in allowed_read_set or target not in resolved_read_map:
            violations.append(f"Unauthorized read path planned by Master: '{rel}'.")

    seen_writes: set[str] = set()
    for rel in planned_writes:
        try:
            target = resolve_within_workspace(worker_ws, rel)
        except ValueError as exc:
            violations.append(f"Invalid allowed_write_paths entry '{rel}': {exc}")
            continue
        if rel not in allowed_write_set or target not in resolved_write_map:
            violations.append(f"Unauthorized write path planned by Master: '{rel}'.")
        if target in resolved_read_map:
            violations.append(f"Planned write path overlaps protected read path: '{rel}'.")
        seen_writes.add(rel)

    if expected_artifact_relpath not in seen_writes:
        violations.append(
            f"MasterPlan omitted required target artifact '{expected_artifact_relpath}' "
            "from allowed_write_paths."
        )
    return violations


def validate_master_plan_policy(
    *,
    plan: MasterPlan,
    master_spec: SpikeAgentSpec,
    roster: dict[str, SpikeAgentSpec],
    worker_ws: Path,
    permitted_reads: Sequence[str],
    permitted_writes: Sequence[str],
    expected_artifact_relpath: str,
) -> tuple[SpikeAgentSpec | None, OrchestrationError | None]:
    """Validate authorization and workspace boundaries for a parsed ``MasterPlan``.

    Returns:
        Tuple of ``(worker_spec, None)`` when approved or ``(None, OrchestrationError)``
        failing closed on the first detected authorization or scope violation.
    """
    violations: list[str] = []
    target_worker = roster.get(plan.target_worker_id)
    if plan.target_worker_id not in master_spec.allowed_handoff_targets:
        violations.append(
            f"Master selected unauthorized target_worker_id '{plan.target_worker_id}'."
        )
    elif target_worker is None:
        violations.append(
            f"Planned target_worker_id '{plan.target_worker_id}' does not exist in roster."
        )
    elif target_worker.system_role != "worker":
        violations.append(
            f"Target '{plan.target_worker_id}' has role '{target_worker.system_role}', expected 'worker'."
        )
    elif not target_worker.allow_workspace_write:
        violations.append(f"Worker '{plan.target_worker_id}' lacks workspace write role.")
    elif target_worker.allow_shell:
        violations.append(f"Worker '{plan.target_worker_id}' violates no-shell spike policy.")

    for field_name, text_val in (
        ("task_summary", plan.task_summary),
        ("worker_objective", plan.worker_objective),
    ):
        if redact_secrets(text_val) != text_val:
            violations.append(
                f"MasterPlan.{field_name} contains sensitive credential-like material."
            )

    violations.extend(
        _validate_policy_paths(
            worker_ws=worker_ws,
            planned_reads=plan.allowed_read_paths,
            planned_writes=plan.allowed_write_paths,
            permitted_reads=permitted_reads,
            permitted_writes=permitted_writes,
            expected_artifact_relpath=expected_artifact_relpath,
        )
    )
    if violations:
        return None, OrchestrationError(
            error_code="POLICY_VIOLATION",
            stage="handoff_policy",
            message="; ".join(violations),
            details={"target_worker_id": plan.target_worker_id, "violations": violations},
        )
    return target_worker, None


def _validate_synthesis_truth(
    *,
    synthesis: MasterSynthesis,
    worker_result: EngineResult,
    qa_verification: EngineArtifactVerificationResult,
) -> OrchestrationError | None:
    """Enforce that Master synthesis matches QA truth and cannot deliver unverified output."""
    mismatches: list[str] = []
    normalized_art = synthesis.artifact_relpath.replace("\\", "/").strip()
    expected_art = qa_verification.artifact_relpath.replace("\\", "/").strip()
    if normalized_art != expected_art:
        mismatches.append(
            f"Synthesis artifact_relpath '{normalized_art}' != QA '{expected_art}'."
        )
    if synthesis.worker_status_observed != worker_result.status:
        mismatches.append(
            f"Synthesis worker_status_observed '{synthesis.worker_status_observed}' != "
            f"actual '{worker_result.status}'."
        )
    if synthesis.qa_status_observed != qa_verification.status:
        mismatches.append(
            f"Synthesis qa_status_observed '{synthesis.qa_status_observed}' != "
            f"QA '{qa_verification.status}'."
        )

    if qa_verification.status == "VERIFIED_PASS":
        if synthesis.delivery_status != "DELIVERED":
            mismatches.append(
                f"QA passed but Master reported delivery_status='{synthesis.delivery_status}'."
            )
        expected_sha = (qa_verification.actual_sha256 or "").strip().lower()
        observed_sha = (synthesis.artifact_sha256 or "").strip().lower()
        if not expected_sha or observed_sha != expected_sha:
            mismatches.append(
                f"Synthesis artifact_sha256 '{observed_sha}' != QA '{expected_sha}'."
            )
    elif synthesis.delivery_status == "DELIVERED":
        mismatches.append(
            "Master attempted to mark delivery_status='DELIVERED' despite QA VERIFIED_FAIL."
        )

    if mismatches:
        return OrchestrationError(
            error_code="SYNTHESIS_TRUTH_MISMATCH",
            stage="master_synthesize",
            message="; ".join(mismatches),
            details={"mismatches": mismatches},
        )
    return None


def _preflight_validate(
    *,
    master_workspace_dir: Path,
    worker_workspace_dir: Path,
    allowed_root: Path,
    allow_temp_workspace: bool = False,
    roster: dict[str, SpikeAgentSpec],
    master_agent_id: str,
    permitted_read_relpaths: Sequence[str],
    permitted_write_relpaths: Sequence[str],
    expected_artifact_relpath: str,
    master_session_id: str,
    worker_session_id: str,
) -> tuple[Path, Path, SpikeAgentSpec, list[str], list[str], str] | OrchestrationError:
    """Validate workspaces, non-overlapping bounds, roster specs, and session IDs."""
    try:
        master_ws = validate_session_workspace(
            master_workspace_dir,
            allowed_root,
            allow_temp_workspace=allow_temp_workspace,
        )
        worker_ws = validate_session_workspace(
            worker_workspace_dir,
            allowed_root,
            allow_temp_workspace=allow_temp_workspace,
        )
        if paths_overlap(master_ws, worker_ws):
            raise SessionIsolationError(
                "INVALID_WORKSPACE_BOUNDARY",
                f"Master workspace '{master_ws}' and Worker workspace '{worker_ws}' overlap.",
            )
    except SessionIsolationError as exc:
        return OrchestrationError(
            error_code="INVALID_WORKSPACE_ISOLATION",
            stage="preflight",
            message=str(exc),
        )

    if not master_session_id.strip() or not worker_session_id.strip():
        return OrchestrationError(
            error_code="SESSION_ISOLATION_VIOLATION",
            stage="preflight",
            message="Master and Worker logical session IDs must be non-blank.",
        )
    if master_session_id.strip() == worker_session_id.strip():
        return OrchestrationError(
            error_code="SESSION_ISOLATION_VIOLATION",
            stage="preflight",
            message=f"Master and Worker must use distinct session IDs (got '{master_session_id}').",
        )

    master_spec = roster.get(master_agent_id)
    if master_spec is None or master_spec.system_role != "orchestrator":
        return OrchestrationError(
            error_code="INVALID_AGENT_SPEC",
            stage="preflight",
            message=f"Roster lacks valid orchestrator spec for '{master_agent_id}'.",
        )

    try:
        clean_reads = [_validate_clean_relpath(p, "permitted_read_relpaths") for p in permitted_read_relpaths]
        clean_writes = [_validate_clean_relpath(p, "permitted_write_relpaths") for p in permitted_write_relpaths]
        clean_expected = _validate_clean_relpath(expected_artifact_relpath, "expected_artifact_relpath")
    except ValueError as exc:
        return OrchestrationError(
            error_code="POLICY_VIOLATION",
            stage="preflight",
            message=str(exc),
        )

    if not clean_writes or clean_expected not in clean_writes:
        return OrchestrationError(
            error_code="POLICY_VIOLATION",
            stage="preflight",
            message=f"expected_artifact_relpath '{clean_expected}' must appear in permitted_write_relpaths.",
        )
    if set(clean_reads) & set(clean_writes):
        return OrchestrationError(
            error_code="POLICY_VIOLATION",
            stage="preflight",
            message="permitted_read_relpaths and permitted_write_relpaths must be disjoint.",
        )
    return master_ws, worker_ws, master_spec, clean_reads, clean_writes, clean_expected


def _execute_stage(
    *,
    registry: EngineRegistry,
    collector: _SpikeEventCollector,
    stage: str,
    agent_spec: SpikeAgentSpec,
    session_id: str,
    workspace_dir: Path,
    denied_dir: Path,
    prompt: str,
    provider_id: str,
    connection_id: str | None,
    model_id: str | None,
    timeout_seconds: int,
    capabilities_required: list[str],
    correlation_id: str,
) -> EngineResult:
    """Invoke ``EngineRegistry.execute`` for one orchestrator or worker stage."""
    request = EngineRequest(
        provider_id=provider_id,
        connection_id=connection_id,
        model_id=model_id,
        prompt=prompt,
        workspace_path=str(workspace_dir),
        timeout_seconds=timeout_seconds,
        capabilities_required=capabilities_required,  # type: ignore[arg-type]
        execution_mode=agent_spec.execution_mode,
        denied_paths=[str(denied_dir)],
        deny_shell_commands=not agent_spec.allow_shell,
        session_reference=EngineSessionReference(
            session_id=session_id,
            binding_source="adapter_logical",
        ),
        metadata={
            "correlation_id": correlation_id,
            "agent_id": agent_spec.agent_id,
            "system_role": agent_spec.system_role,
            "stage": stage,
        },
    )
    callback = collector.make_callback(
        stage=stage,
        agent_id=agent_spec.agent_id,
        role=agent_spec.system_role,
        logical_session_id=session_id,
    )
    result = registry.execute(request, on_event=callback)
    conv_id = result.session_reference.provider_conversation_id if result.session_reference else None
    collector.backfill_conversation_id(stage=stage, conversation_id=conv_id)
    return result


def run_master_worker_spike(
    *,
    registry: EngineRegistry,
    user_request: str,
    master_workspace_dir: Path,
    worker_workspace_dir: Path,
    allowed_root: Path,
    expected_artifact_relpath: str = "proof.txt",
    permitted_read_relpaths: Sequence[str] = ("package.json",),
    permitted_write_relpaths: Sequence[str] = ("proof.txt",),
    expected_artifact_content: str | None = None,
    required_artifact_substrings: Sequence[str] = (),
    provider_id: str = "antigravity",
    connection_id: str | None = None,
    model_id: str | None = None,
    timeout_seconds: int = 90,
    roster: dict[str, SpikeAgentSpec] | None = None,
    on_event: Callable[[AttributedSpikeEvent], None] | None = None,
    master_session_id: str | None = None,
    worker_session_id: str | None = None,
    allow_temp_workspace: bool = False,
) -> MasterWorkerSpikeResult:
    """Execute one fail-closed Master -> Worker -> Independent QA -> Master collaboration spike.

    Args:
        registry: Configured ``EngineRegistry`` used by both Master and Worker.
        user_request: Plain-text user objective passed to ``core.master``.
        master_workspace_dir: Isolated directory assigned solely to Master.
        worker_workspace_dir: Disjoint isolated directory assigned solely to Worker.
        allowed_root: Parent directory constraining valid task workspaces.
        expected_artifact_relpath: Expected workspace-relative artifact verified by QA.
        permitted_read_relpaths: Relative input paths Master is permitted to delegate for reading.
        permitted_write_relpaths: Relative output paths Master is permitted to delegate for writing.
        expected_artifact_content: Optional exact UTF-8 text verified independently by QA.
        required_artifact_substrings: Substrings checked independently by QA inside the artifact.
        provider_id: Engine provider registered in ``registry``.
        connection_id: Optional connection identifier in ``registry``.
        model_id: Optional model identifier forwarded to ``EngineRequest``.
        timeout_seconds: Per-turn execution timeout in seconds.
        roster: Optional agent specification override dictionary.
        on_event: Optional callback receiving attributed streaming events in real time.
        master_session_id: Optional explicit logical session ID for Master.
        worker_session_id: Optional explicit logical session ID for Worker.

    Returns:
        Auditable ``MasterWorkerSpikeResult`` recording plans, handoffs, telemetry,
        independent QA outcomes, and final Master synthesis.
    """
    started_at = datetime.now(timezone.utc)
    correlation_id = f"corr-{uuid.uuid4().hex[:12]}"
    master_agent_id = "core.master"
    m_session = (master_session_id or f"sess-master-{uuid.uuid4().hex[:10]}").strip()
    w_session = (worker_session_id or f"sess-worker-{uuid.uuid4().hex[:10]}").strip()
    effective_roster = roster if roster is not None else build_default_spike_roster()

    def _finish(
        *,
        status: str,
        error: OrchestrationError | None = None,
        worker_agent_id: str | None = None,
        master_plan_result: EngineResult | None = None,
        master_plan: MasterPlan | None = None,
        worker_handoff: WorkerHandoff | None = None,
        worker_execution: WorkerExecutionRecord | None = None,
        qa_verification: EngineArtifactVerificationResult | None = None,
        master_synthesis_result: EngineResult | None = None,
        master_synthesis: MasterSynthesis | None = None,
        master_plan_conv_id: str | None = None,
        worker_conv_id: str | None = None,
        master_synth_conv_id: str | None = None,
        master_ws_str: str = str(master_workspace_dir),
        worker_ws_str: str = str(worker_workspace_dir),
        events: list[AttributedSpikeEvent] | None = None,
    ) -> MasterWorkerSpikeResult:
        finished_at = datetime.now(timezone.utc)
        if error is not None:
            logger.warning(
                "Spike %s ended status=%s stage=%s code=%s",
                correlation_id,
                status,
                error.stage,
                error.error_code,
            )
        return MasterWorkerSpikeResult(
            status=status,  # type: ignore[arg-type]
            correlation_id=correlation_id,
            master_agent_id=master_agent_id,
            worker_agent_id=worker_agent_id,
            master_session_id=m_session,
            worker_session_id=w_session if worker_handoff is not None else None,
            master_plan_conversation_id=master_plan_conv_id,
            worker_conversation_id=worker_conv_id,
            master_synthesis_conversation_id=master_synth_conv_id,
            master_workspace_path=master_ws_str,
            worker_workspace_path=worker_ws_str,
            user_request=user_request,
            master_plan_result=master_plan_result,
            master_plan=master_plan,
            worker_handoff=worker_handoff,
            worker_execution=worker_execution,
            qa_verification=qa_verification,
            master_synthesis_result=master_synthesis_result,
            master_synthesis=master_synthesis,
            attributed_events=events or [],
            error=error,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=_elapsed_ms(started_at, finished_at),
        )

    preflight = _preflight_validate(
        master_workspace_dir=master_workspace_dir,
        worker_workspace_dir=worker_workspace_dir,
        allowed_root=allowed_root,
        allow_temp_workspace=allow_temp_workspace,
        roster=effective_roster,
        master_agent_id=master_agent_id,
        permitted_read_relpaths=permitted_read_relpaths,
        permitted_write_relpaths=permitted_write_relpaths,
        expected_artifact_relpath=expected_artifact_relpath,
        master_session_id=m_session,
        worker_session_id=w_session,
    )
    if isinstance(preflight, OrchestrationError):
        early_status = (
            "BLOCKED"
            if preflight.error_code
            in {"INVALID_WORKSPACE_ISOLATION", "SESSION_ISOLATION_VIOLATION", "POLICY_VIOLATION"}
            else "FAILED"
        )
        return _finish(status=early_status, error=preflight)

    master_ws, worker_ws, master_spec, clean_reads, clean_writes, clean_expected = preflight
    collector = _SpikeEventCollector(downstream=on_event)
    pre_master_worker_tree = _snapshot_workspace_tree(worker_ws)

    logger.info(
        "Starting P03.01 spike %s master_session=%s worker_session=%s",
        correlation_id,
        m_session,
        w_session,
    )

    plan_prompt = _build_master_plan_prompt(
        user_request=user_request,
        master_spec=master_spec,
        permitted_read_relpaths=clean_reads,
        permitted_write_relpaths=clean_writes,
        expected_artifact_relpath=clean_expected,
    )
    master_plan_result = _execute_stage(
        registry=registry,
        collector=collector,
        stage="master_plan",
        agent_spec=master_spec,
        session_id=m_session,
        workspace_dir=master_ws,
        denied_dir=worker_ws,
        prompt=plan_prompt,
        provider_id=provider_id,
        connection_id=connection_id,
        model_id=model_id,
        timeout_seconds=timeout_seconds,
        capabilities_required=["text_generation", "streaming"],
        correlation_id=correlation_id,
    )
    master_plan_conv_id = (
        master_plan_result.session_reference.provider_conversation_id
        if master_plan_result.session_reference
        else None
    )

    post_master_worker_tree = _snapshot_workspace_tree(worker_ws)
    if post_master_worker_tree != pre_master_worker_tree:
        return _finish(
            status="BLOCKED",
            error=OrchestrationError(
                error_code="POLICY_VIOLATION",
                stage="master_plan",
                message="Master stage mutated worker workspace directly; failing closed.",
            ),
            master_plan_result=master_plan_result,
            master_plan_conv_id=master_plan_conv_id,
            master_ws_str=str(master_ws),
            worker_ws_str=str(worker_ws),
            events=collector.items,
        )

    if master_plan_result.status != "SUCCEEDED":
        err_msg = (
            master_plan_result.error.message
            if master_plan_result.error
            else f"Master planning returned status={master_plan_result.status}."
        )
        return _finish(
            status="BLOCKED" if master_plan_result.status == "BLOCKED" else "FAILED",
            error=OrchestrationError(
                error_code="MASTER_PLAN_EXECUTION_FAILED",
                stage="master_plan",
                message=err_msg,
                details={"engine_status": master_plan_result.status},
            ),
            master_plan_result=master_plan_result,
            master_plan_conv_id=master_plan_conv_id,
            master_ws_str=str(master_ws),
            worker_ws_str=str(worker_ws),
            events=collector.items,
        )

    try:
        plan_payload = parse_strict_json_object(master_plan_result.response_text)
        master_plan = MasterPlan.model_validate(plan_payload)
    except (ValueError, ValidationError) as exc:
        return _finish(
            status="FAILED",
            error=OrchestrationError(
                error_code="MASTER_PLAN_PARSE_FAILED",
                stage="master_plan",
                message=f"Master plan output violated strict JSON schema: {exc}",
            ),
            master_plan_result=master_plan_result,
            master_plan_conv_id=master_plan_conv_id,
            master_ws_str=str(master_ws),
            worker_ws_str=str(worker_ws),
            events=collector.items,
        )

    worker_spec, policy_err = validate_master_plan_policy(
        plan=master_plan,
        master_spec=master_spec,
        roster=effective_roster,
        worker_ws=worker_ws,
        permitted_reads=clean_reads,
        permitted_writes=clean_writes,
        expected_artifact_relpath=clean_expected,
    )
    if policy_err is not None or worker_spec is None:
        return _finish(
            status="BLOCKED",
            error=policy_err,
            master_plan_result=master_plan_result,
            master_plan=master_plan,
            master_plan_conv_id=master_plan_conv_id,
            master_ws_str=str(master_ws),
            worker_ws_str=str(worker_ws),
            events=collector.items,
        )

    handoff = WorkerHandoff(
        correlation_id=correlation_id,
        from_agent_id=master_spec.agent_id,
        to_agent_id=worker_spec.agent_id,
        worker_session_id=w_session,
        worker_workspace_path=str(worker_ws),
        denied_paths=[str(master_ws)],
        deny_shell_commands=True,
        plan=master_plan,
        worker_instructions=master_plan.worker_objective,
    )
    existing_protected = [
        rel
        for rel in sorted(set(clean_reads) | set(master_plan.allowed_read_paths))
        if (worker_ws / rel).is_file()
    ]
    protected_hashes = snapshot_file_hashes(worker_ws, existing_protected)
    pre_worker_master_tree = _snapshot_workspace_tree(master_ws)

    worker_result = _execute_stage(
        registry=registry,
        collector=collector,
        stage="worker_execute",
        agent_spec=worker_spec,
        session_id=w_session,
        workspace_dir=worker_ws,
        denied_dir=master_ws,
        prompt=_build_worker_prompt(handoff),
        provider_id=provider_id,
        connection_id=connection_id,
        model_id=model_id,
        timeout_seconds=timeout_seconds,
        capabilities_required=["streaming", "native_tool_execution", "workspace_access"],
        correlation_id=correlation_id,
    )
    worker_conv_id = (
        worker_result.session_reference.provider_conversation_id
        if worker_result.session_reference
        else None
    )
    worker_record = WorkerExecutionRecord(
        worker_agent_id=worker_spec.agent_id,
        worker_session_id=w_session,
        handoff_id=handoff.handoff_id,
        engine_result=worker_result,
        artifact_relpath=clean_expected,
        protected_source_hashes=protected_hashes,
    )

    post_worker_master_tree = _snapshot_workspace_tree(master_ws)
    if post_worker_master_tree != pre_worker_master_tree:
        return _finish(
            status="BLOCKED",
            error=OrchestrationError(
                error_code="SESSION_ISOLATION_VIOLATION",
                stage="worker_execute",
                message="Worker stage mutated Master workspace directly; failing closed.",
            ),
            worker_agent_id=worker_spec.agent_id,
            master_plan_result=master_plan_result,
            master_plan=master_plan,
            worker_handoff=handoff,
            worker_execution=worker_record,
            master_plan_conv_id=master_plan_conv_id,
            worker_conv_id=worker_conv_id,
            master_ws_str=str(master_ws),
            worker_ws_str=str(worker_ws),
            events=collector.items,
        )

    returned_worker_sid = (
        worker_result.session_reference.session_id if worker_result.session_reference else None
    )
    if returned_worker_sid == m_session or (
        master_plan_conv_id and worker_conv_id and master_plan_conv_id == worker_conv_id
    ):
        return _finish(
            status="BLOCKED",
            error=OrchestrationError(
                error_code="SESSION_ISOLATION_VIOLATION",
                stage="worker_execute",
                message="Provider returned colliding Master and Worker session/conversation IDs.",
            ),
            worker_agent_id=worker_spec.agent_id,
            master_plan_result=master_plan_result,
            master_plan=master_plan,
            worker_handoff=handoff,
            worker_execution=worker_record,
            master_plan_conv_id=master_plan_conv_id,
            worker_conv_id=worker_conv_id,
            master_ws_str=str(master_ws),
            worker_ws_str=str(worker_ws),
            events=collector.items,
        )

    effective_substrings = tuple(required_artifact_substrings)
    qa_verification = verify_engine_artifact(
        workspace_dir=worker_ws,
        artifact_relpath=clean_expected,
        engine_result=worker_result,
        expected_content=expected_artifact_content,
        required_substrings=effective_substrings,
        protected_source_hashes=protected_hashes,
    )

    if worker_result.status != "SUCCEEDED":
        err_msg = (
            worker_result.error.message
            if worker_result.error
            else f"Worker execution returned status={worker_result.status}."
        )
        return _finish(
            status="BLOCKED" if worker_result.status == "BLOCKED" else "FAILED",
            error=OrchestrationError(
                error_code="WORKER_EXECUTION_FAILED",
                stage="worker_execute",
                message=err_msg,
                details={"engine_status": worker_result.status},
            ),
            worker_agent_id=worker_spec.agent_id,
            master_plan_result=master_plan_result,
            master_plan=master_plan,
            worker_handoff=handoff,
            worker_execution=worker_record,
            qa_verification=qa_verification,
            master_plan_conv_id=master_plan_conv_id,
            worker_conv_id=worker_conv_id,
            master_ws_str=str(master_ws),
            worker_ws_str=str(worker_ws),
            events=collector.items,
        )

    synthesis_prompt = _build_master_synthesis_prompt(
        user_request=user_request,
        master_spec=master_spec,
        handoff=handoff,
        worker_result=worker_result,
        qa_verification=qa_verification,
    )
    master_synth_result = _execute_stage(
        registry=registry,
        collector=collector,
        stage="master_synthesize",
        agent_spec=master_spec,
        session_id=m_session,
        workspace_dir=master_ws,
        denied_dir=worker_ws,
        prompt=synthesis_prompt,
        provider_id=provider_id,
        connection_id=connection_id,
        model_id=model_id,
        timeout_seconds=timeout_seconds,
        capabilities_required=["text_generation", "streaming"],
        correlation_id=correlation_id,
    )
    master_synth_conv_id = (
        master_synth_result.session_reference.provider_conversation_id
        if master_synth_result.session_reference
        else None
    )

    if master_synth_conv_id and worker_conv_id and master_synth_conv_id == worker_conv_id:
        return _finish(
            status="BLOCKED",
            error=OrchestrationError(
                error_code="SESSION_ISOLATION_VIOLATION",
                stage="master_synthesize",
                message="Master synthesis provider conversation collided with Worker conversation.",
            ),
            worker_agent_id=worker_spec.agent_id,
            master_plan_result=master_plan_result,
            master_plan=master_plan,
            worker_handoff=handoff,
            worker_execution=worker_record,
            qa_verification=qa_verification,
            master_synthesis_result=master_synth_result,
            master_plan_conv_id=master_plan_conv_id,
            worker_conv_id=worker_conv_id,
            master_synth_conv_id=master_synth_conv_id,
            master_ws_str=str(master_ws),
            worker_ws_str=str(worker_ws),
            events=collector.items,
        )

    if master_synth_result.status != "SUCCEEDED":
        err_msg = (
            master_synth_result.error.message
            if master_synth_result.error
            else f"Master synthesis returned status={master_synth_result.status}."
        )
        return _finish(
            status="FAILED",
            error=OrchestrationError(
                error_code="MASTER_SYNTHESIS_EXECUTION_FAILED",
                stage="master_synthesize",
                message=err_msg,
                details={"engine_status": master_synth_result.status},
            ),
            worker_agent_id=worker_spec.agent_id,
            master_plan_result=master_plan_result,
            master_plan=master_plan,
            worker_handoff=handoff,
            worker_execution=worker_record,
            qa_verification=qa_verification,
            master_synthesis_result=master_synth_result,
            master_plan_conv_id=master_plan_conv_id,
            worker_conv_id=worker_conv_id,
            master_synth_conv_id=master_synth_conv_id,
            master_ws_str=str(master_ws),
            worker_ws_str=str(worker_ws),
            events=collector.items,
        )

    try:
        synth_payload = parse_strict_json_object(master_synth_result.response_text)
        master_synthesis = MasterSynthesis.model_validate(synth_payload)
    except (ValueError, ValidationError) as exc:
        return _finish(
            status="FAILED",
            error=OrchestrationError(
                error_code="MASTER_SYNTHESIS_PARSE_FAILED",
                stage="master_synthesize",
                message=f"Master synthesis output violated strict JSON schema: {exc}",
            ),
            worker_agent_id=worker_spec.agent_id,
            master_plan_result=master_plan_result,
            master_plan=master_plan,
            worker_handoff=handoff,
            worker_execution=worker_record,
            qa_verification=qa_verification,
            master_synthesis_result=master_synth_result,
            master_plan_conv_id=master_plan_conv_id,
            worker_conv_id=worker_conv_id,
            master_synth_conv_id=master_synth_conv_id,
            master_ws_str=str(master_ws),
            worker_ws_str=str(worker_ws),
            events=collector.items,
        )

    truth_err = _validate_synthesis_truth(
        synthesis=master_synthesis,
        worker_result=worker_result,
        qa_verification=qa_verification,
    )
    if truth_err is not None:
        return _finish(
            status="FAILED",
            error=truth_err,
            worker_agent_id=worker_spec.agent_id,
            master_plan_result=master_plan_result,
            master_plan=master_plan,
            worker_handoff=handoff,
            worker_execution=worker_record,
            qa_verification=qa_verification,
            master_synthesis_result=master_synth_result,
            master_synthesis=master_synthesis,
            master_plan_conv_id=master_plan_conv_id,
            worker_conv_id=worker_conv_id,
            master_synth_conv_id=master_synth_conv_id,
            master_ws_str=str(master_ws),
            worker_ws_str=str(worker_ws),
            events=collector.items,
        )

    if qa_verification.status == "VERIFIED_FAIL":
        return _finish(
            status="REJECTED_BY_QA",
            error=OrchestrationError(
                error_code="QA_VERIFICATION_FAILED",
                stage="qa_verify",
                message="; ".join(qa_verification.failure_reasons) or "Independent QA failed.",
            ),
            worker_agent_id=worker_spec.agent_id,
            master_plan_result=master_plan_result,
            master_plan=master_plan,
            worker_handoff=handoff,
            worker_execution=worker_record,
            qa_verification=qa_verification,
            master_synthesis_result=master_synth_result,
            master_synthesis=master_synthesis,
            master_plan_conv_id=master_plan_conv_id,
            worker_conv_id=worker_conv_id,
            master_synth_conv_id=master_synth_conv_id,
            master_ws_str=str(master_ws),
            worker_ws_str=str(worker_ws),
            events=collector.items,
        )

    return _finish(
        status="DELIVERED",
        error=None,
        worker_agent_id=worker_spec.agent_id,
        master_plan_result=master_plan_result,
        master_plan=master_plan,
        worker_handoff=handoff,
        worker_execution=worker_record,
        qa_verification=qa_verification,
        master_synthesis_result=master_synth_result,
        master_synthesis=master_synthesis,
        master_plan_conv_id=master_plan_conv_id,
        worker_conv_id=worker_conv_id,
        master_synth_conv_id=master_synth_conv_id,
        master_ws_str=str(master_ws),
        worker_ws_str=str(worker_ws),
        events=collector.items,
    )
