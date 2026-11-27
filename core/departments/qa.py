"""Phase 09 / P09.08 Operations & QA Department: independent verification with strict Evidence-First gates."""

from __future__ import annotations

from datetime import datetime
import hashlib
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.contracts.artifact import ArtifactVerificationStatus
from core.contracts.identifiers import (
    AgentId,
    ArtifactId,
    EventId,
    EvidenceId,
    RunId,
    Sha256Hex,
    TaskId,
    ensure_not_secret,
    validate_aware_datetime,
    validate_safe_relative_path,
)
from core.departments._common import (
    DepartmentBehaviorError,
    ToolInvokerPort,
    append_department_event,
    authorize_and_audit,
    invoke_tool_checked,
)
from core.events.journal import EventJournal
from core.governance.manager import PermissionManager
from core.governance.models import TrustedAuthContext

QaVerdictStatus = Literal["PASS", "FAIL", "UNVERIFIED"]


class QaIndependenceError(DepartmentBehaviorError):
    """Raised when artifact producer or non-verifier attempts self-certification."""


class QaEvidenceRequiredError(DepartmentBehaviorError):
    """Raised fail-closed when PASS is requested without concrete verification proof."""


@runtime_checkable
class EvidenceVaultPort(Protocol):
    """Structural interface decoupled from ``storage.knowledge`` matching ``EvidenceAndKnowledgeService``."""

    def record_evidence(
        self,
        *,
        auth_context: TrustedAuthContext,
        evidence_kind: str,
        task_id: str,
        summary: str,
        timestamp: datetime,
        run_id: str | None = None,
        artifact_id: str | None = None,
        source_url: str | None = None,
        retrieved_at: datetime | None = None,
        untrusted_source: bool = False,
        content_sha256: str | None = None,
        payload: dict[str, Any] | None = None,
        evidence_id: str | None = None,
    ) -> Any:
        """Persist immutable evidence record."""

    def get_qa_inspection_bundle(
        self,
        task_id: str,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
        run_id: str | None = None,
    ) -> Any:
        """Assemble artifacts, evidence records, and tool traces."""


@runtime_checkable
class ArtifactVaultQaPort(Protocol):
    """Structural interface matching ``ArtifactVaultService`` read + verification methods."""

    def read_verified_bytes(
        self,
        artifact_id: str,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> tuple[Any, bytes]:
        """Verify SHA-256 and read blob bytes."""

    def update_verification_status(
        self,
        artifact_id: str,
        *,
        verification_status: ArtifactVerificationStatus,
        evidence_reference: str | None,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> Any:
        """Update Vault and metadata artifact QA status atomically."""


class QaVerificationRequest(BaseModel):
    """Request for independent QA verification of Task deliverables."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    run_id: RunId | None = None
    producer_agent_id: AgentId
    artifact_id: ArtifactId | None = None
    target_relpath: str | None = Field(default=None, max_length=260)
    workspace_root: str | None = Field(default=None, max_length=512)
    expected_sha256: Sha256Hex | None = None
    sandbox_passed: bool | None = None
    verified_checks: list[str] = Field(default_factory=list, max_length=32)
    existing_evidence_ids: list[str] = Field(default_factory=list, max_length=32)
    defect_findings: list[str] = Field(default_factory=list, max_length=32)
    summary: str = Field(min_length=3, max_length=1000)
    require_pass_verdict: bool = False
    timestamp: datetime

    @field_validator("target_relpath")
    @classmethod
    def _validate_rel(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_safe_relative_path(value, "target_relpath")

    @field_validator("summary", "workspace_root")
    @classmethod
    def _validate_text(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return ensure_not_secret(value, getattr(info, "field_name", "qa_field"))

    @field_validator("verified_checks", "existing_evidence_ids", "defect_findings")
    @classmethod
    def _validate_lists(cls, values: list[str], info: object) -> list[str]:
        field_name = getattr(info, "field_name", "qa_list")
        return [ensure_not_secret(v, field_name) for v in values]

    @field_validator("timestamp")
    @classmethod
    def _validate_ts(cls, value: datetime) -> datetime:
        return validate_aware_datetime(value, "timestamp")


class QaVerificationVerdict(BaseModel):
    """Independent QA verification verdict backed by immutable Evidence and SHA checks."""

    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    run_id: RunId | None = None
    artifact_id: ArtifactId | None = None
    status: QaVerdictStatus
    verification_status: ArtifactVerificationStatus
    verified_by: str
    observed_sha256: Sha256Hex | None = None
    integrity_matched: bool | None = None
    defects: list[str] = Field(default_factory=list)
    evidence_id: EvidenceId | None = None
    audit_event_id: EventId


class QaDepartmentService:
    """Operations & QA service enforcing verifier independence and Evidence-First certification."""

    def __init__(
        self,
        *,
        permission_manager: PermissionManager,
        journal: EventJournal,
        evidence_vault: EvidenceVaultPort,
        artifact_vault: ArtifactVaultQaPort | None = None,
        tool_invoker: ToolInvokerPort | None = None,
    ) -> None:
        """Bind QA service to Governance, EventJournal, EvidenceVault, and optional Vault/Tools."""
        self._pm = permission_manager
        self._journal = journal
        self._evidence = evidence_vault
        self._vault = artifact_vault
        self._tools = tool_invoker

    def inspect_bundle(
        self,
        task_id: str,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
        run_id: str | None = None,
    ) -> Any:
        """Return Evidence + Artifact + Tool Execution bundle for independent QA review."""
        authorize_and_audit(
            self._pm,
            auth_context=auth_context,
            action="vault.read",
            resource=f"qa:bundle:{task_id}",
            task_id=task_id,
            run_id=run_id,
            timestamp=timestamp,
            correlation_id=f"qa-bundle-{task_id}",
        )
        return self._evidence.get_qa_inspection_bundle(
            task_id,
            auth_context=auth_context,
            timestamp=timestamp,
            run_id=run_id,
        )

    def verify_deliverables(
        self,
        request: QaVerificationRequest,
        *,
        auth_context: TrustedAuthContext,
    ) -> QaVerificationVerdict:
        """Verify artifacts and test proofs independently; deny PASS without valid evidence."""
        authorize_and_audit(
            self._pm,
            auth_context=auth_context,
            action="vault.write",
            resource=f"qa:verify:{request.task_id}",
            task_id=request.task_id,
            run_id=request.run_id,
            timestamp=request.timestamp,
            correlation_id=f"qa-ver-{request.task_id}",
        )
        if auth_context.actor_kind not in {"trusted_verifier", "human_operator"}:
            raise QaIndependenceError(
                "INDEPENDENT_VERIFIER_REQUIRED",
                f"Actor kind '{auth_context.actor_kind}' cannot perform independent QA certification.",
            )
        if auth_context.actor_id == request.producer_agent_id:
            raise QaIndependenceError(
                "SELF_VERIFICATION_FORBIDDEN",
                f"Producer agent '{request.producer_agent_id}' cannot verify its own output.",
            )

        defects = list(request.defect_findings)
        observed_sha: str | None = None
        integrity_matched: bool | None = None

        if request.artifact_id is not None and self._vault is not None:
            try:
                entry, raw_bytes = self._vault.read_verified_bytes(
                    request.artifact_id,
                    auth_context=auth_context,
                    timestamp=request.timestamp,
                )
                observed_sha = hashlib.sha256(raw_bytes).hexdigest()
                if entry.owner_agent_id == auth_context.actor_id:
                    raise QaIndependenceError(
                        "SELF_VERIFICATION_FORBIDDEN",
                        f"Vault artifact owner '{entry.owner_agent_id}' cannot self-verify.",
                    )
                if request.expected_sha256 is not None and observed_sha != request.expected_sha256:
                    integrity_matched = False
                    defects.append(
                        f"Vault artifact SHA-256 mismatch: expected {request.expected_sha256[:12]}, got {observed_sha[:12]}."
                    )
                else:
                    integrity_matched = True
            except QaIndependenceError:
                raise
            except Exception as exc:
                integrity_matched = False
                defects.append(f"Vault integrity check failed: {exc}")
        elif request.target_relpath is not None and request.workspace_root is not None:
            try:
                read_res = invoke_tool_checked(
                    self._tools,
                    auth_context=auth_context,
                    tool_id="tools.files.read",
                    parameters={"path": request.target_relpath},
                    task_id=request.task_id,
                    run_id=request.run_id,
                    workspace_root=request.workspace_root,
                    target_path=request.target_relpath,
                    timestamp=request.timestamp,
                    correlation_id=f"qa-read-{request.task_id}",
                )
                observed_sha = str(
                    read_res.data.get("sha256")
                    or hashlib.sha256(str(read_res.data.get("content", "")).encode("utf-8")).hexdigest()
                )
                if request.expected_sha256 is not None and observed_sha != request.expected_sha256:
                    integrity_matched = False
                    defects.append(
                        f"Workspace file SHA-256 mismatch: expected {request.expected_sha256[:12]}, got {observed_sha[:12]}."
                    )
                else:
                    integrity_matched = True
            except Exception as exc:
                integrity_matched = False
                defects.append(f"Workspace file inspection failed: {exc}")

        if request.sandbox_passed is False:
            defects.append("Isolated sandbox test suite failed with non-zero exit code.")

        has_concrete_proof = bool(
            request.sandbox_passed is True
            or request.verified_checks
            or request.existing_evidence_ids
        )

        if defects:
            status: QaVerdictStatus = "FAIL"
            ver_status: ArtifactVerificationStatus = "VERIFIED_FAIL"
        elif not has_concrete_proof:
            if request.require_pass_verdict:
                raise QaEvidenceRequiredError(
                    "QA_EVIDENCE_REQUIRED",
                    "Refusing VERIFIED_PASS: no sandbox test pass, verified check, or existing evidence supplied.",
                )
            status = "UNVERIFIED"
            ver_status = "UNVERIFIED"
            defects.append("Missing concrete verification proof; marking UNVERIFIED instead of PASS.")
        else:
            status = "PASS"
            ver_status = "VERIFIED_PASS"

        evd_record = self._evidence.record_evidence(
            auth_context=auth_context,
            evidence_kind="qa_log",
            task_id=request.task_id,
            run_id=request.run_id,
            artifact_id=request.artifact_id,
            content_sha256=observed_sha,
            summary=request.summary,
            payload={
                "qa_verdict": status,
                "verification_status": ver_status,
                "defects": defects,
                "verified_checks": list(request.verified_checks),
                "linked_evidence_ids": list(request.existing_evidence_ids),
                "integrity_matched": integrity_matched,
            },
            timestamp=request.timestamp,
        )
        ev_id = str(evd_record.evidence_id)

        if request.artifact_id is not None and self._vault is not None and ver_status != "UNVERIFIED":
            self._vault.update_verification_status(
                request.artifact_id,
                verification_status=ver_status,
                evidence_reference=ev_id,
                auth_context=auth_context,
                timestamp=request.timestamp,
            )

        audit_ev = append_department_event(
            self._journal,
            event_type="qa.verification_recorded",
            source="qa_verifier",
            task_id=request.task_id,
            run_id=request.run_id,
            artifact_id=request.artifact_id,
            evidence_id=ev_id,
            timestamp=request.timestamp,
            correlation_id=f"qa-verdict-{request.task_id}",
            payload={
                "department": "qa",
                "verification_status": ver_status,
                "qa_verdict": status,
                "defects": defects,
                "observed_sha256": observed_sha,
                "verified_by": auth_context.actor_id,
            },
        )
        return QaVerificationVerdict(
            task_id=request.task_id,
            run_id=request.run_id,
            artifact_id=request.artifact_id,
            status=status,
            verification_status=ver_status,
            verified_by=auth_context.actor_id,
            observed_sha256=observed_sha,
            integrity_matched=integrity_matched,
            defects=defects,
            evidence_id=ev_id,
            audit_event_id=audit_ev.event_id,
        )
