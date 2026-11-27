"""Phase 08 Artifact Registry, Product Vault, Deterministic Preview/Diff, Trash & Backup Service."""

from __future__ import annotations

from datetime import datetime, timedelta
import difflib
import hashlib
import io
import json
import logging
import mimetypes
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Any

from docx import Document as DocxDocument
import openpyxl
from PIL import Image
import pymupdf

from core.contracts.artifact import ArtifactVerificationStatus
from core.contracts.event import Event, EventType
from core.contracts.identifiers import (
    new_artifact_id,
    new_event_id,
    sanitize_payload,
    validate_safe_relative_path,
    validate_semver,
)
from core.events.journal import EventJournal
from core.governance.approvals import ApprovalError
from core.governance.evaluator import compute_request_fingerprint
from core.governance.manager import PermissionManager
from core.governance.models import PolicyDecision, PolicyRequest, TrustedAuthContext
from engines.antigravity.diagnostics import redact_secrets
from storage.artifacts.models import (
    ArtifactAccessScope,
    ArtifactComparisonResult,
    ArtifactPreviewResult,
    ArtifactRegistryEntry,
    ArtifactVersionHistory,
    BulkTrashReport,
    PreviewKind,
    TrashActionResult,
    VaultApprovalRequiredError,
    VaultBackupRecord,
    VaultError,
    VaultRestoreReport,
    new_backup_id,
)
from storage.db.connection import SQLiteConfig, open_sqlite_connection, run_integrity_checks, sqlite_connection
from storage.db.migrations import verify_schema_compatibility
from storage.repositories.sqlite_store import SQLiteRepositoryStore

logger = logging.getLogger(__name__)


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp_vault_")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_name, target)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def _event_actor(auth: TrustedAuthContext) -> str | None:
    if auth.actor_kind in {"worker_agent", "master_agent"} and "." in auth.actor_id:
        return auth.actor_id
    return None


def _row_to_entry(
    row: sqlite3.Row,
    refs_by_artifact: dict[str, list[str]],
) -> ArtifactRegistryEntry:
    aid = str(row["artifact_id"])
    return ArtifactRegistryEntry.model_validate(
        {
            "artifact_id": aid,
            "logical_key": row["logical_key"],
            "version": row["version"],
            "version_number": int(row["version_number"]),
            "previous_artifact_id": row["previous_artifact_id"],
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "owner_agent_id": row["owner_agent_id"],
            "relative_path": row["relative_path"],
            "blob_sha256": row["blob_sha256"],
            "storage_relpath": row["storage_relpath"],
            "size_bytes": int(row["size_bytes"]),
            "content_type": row["content_type"],
            "verification_status": row["verification_status"],
            "evidence_reference": row["evidence_reference"],
            "access_scope": row["access_scope"],
            "department": row["department"],
            "tags": json.loads(str(row["tags_json"])),
            "metadata": json.loads(str(row["metadata_json"])),
            "lifecycle_status": row["lifecycle_status"],
            "trashed_at": row["trashed_at"],
            "trashed_by": row["trashed_by"],
            "trash_reason": row["trash_reason"],
            "purged_at": row["purged_at"],
            "created_at": row["created_at"],
            "referencing_task_ids": refs_by_artifact.get(aid, []),
        }
    )


def _preview_kind_for(content_type: str, relative_path: str) -> PreviewKind:
    lowered_ct = content_type.lower()
    suffix = Path(relative_path).suffix.lower()
    if suffix == ".docx" or "wordprocessingml" in lowered_ct:
        return "DOCX"
    if suffix == ".xlsx" or "spreadsheetml" in lowered_ct:
        return "XLSX"
    if suffix == ".pdf" or lowered_ct == "application/pdf":
        return "PDF"
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"} or lowered_ct.startswith("image/"):
        return "IMAGE"
    if lowered_ct.startswith("text/") or lowered_ct in {"application/json", "application/xml"} or suffix in {
        ".txt",
        ".md",
        ".py",
        ".json",
        ".yaml",
        ".yml",
        ".csv",
        ".html",
        ".sql",
    }:
        return "TEXT"
    return "BINARY_METADATA_ONLY"


def _extract_docx_info(raw: bytes) -> tuple[list[str], list[list[list[str]]]]:
    doc = DocxDocument(io.BytesIO(raw))
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    tables = [[[c.text for c in r.cells] for r in tbl.rows] for tbl in doc.tables]
    return paragraphs, tables


def _extract_xlsx_info(raw: bytes, *, max_rows: int) -> tuple[list[str], dict[str, list[list[Any]]], dict[str, int]]:
    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=False)
    try:
        names = list(wb.sheetnames)
        preview_sheets: dict[str, list[list[Any]]] = {}
        row_counts: dict[str, int] = {}
        for name in names:
            ws = wb[name]
            rows_list: list[list[Any]] = []
            total = 0
            for row in ws.iter_rows(values_only=True):
                total += 1
                if len(rows_list) < max_rows:
                    rows_list.append(
                        [cell if cell is None or isinstance(cell, (bool, int, float, str)) else str(cell) for cell in row]
                    )
            preview_sheets[name] = rows_list
            row_counts[name] = total
        return names, preview_sheets, row_counts
    finally:
        wb.close()


class ArtifactVaultService:
    """Manages versioned Artifacts, deduplicated blobs, previews, diffs, trash/restore, and backups."""

    def __init__(
        self,
        *,
        store: SQLiteRepositoryStore,
        vault_root: Path | str,
        permission_manager: PermissionManager,
        journal: EventJournal,
    ) -> None:
        """Initialize Vault service bound to SQLiteRepositoryStore, root dir, Governance, and Journal."""
        self._store = store
        self._root = Path(os.path.realpath(vault_root))
        self._blobs_dir = self._root / "blobs"
        self._backups_dir = self._root / "backups"
        self._permissions = permission_manager
        self._journal = journal

    def _authorize(
        self,
        *,
        auth_context: TrustedAuthContext,
        action: str,
        resource: str,
        timestamp: datetime,
        task_id: str | None = None,
        run_id: str | None = None,
        parameters: dict[str, Any] | None = None,
        approval_id: str | None = None,
    ) -> PolicyDecision:
        clean_params = sanitize_payload(parameters or {})
        if not isinstance(clean_params, dict):
            clean_params = {}
        decision = self._permissions.evaluate_and_audit(
            PolicyRequest(
                auth_context=auth_context,
                action=action,
                resource=resource,
                task_id=task_id,
                run_id=run_id,
                parameters=clean_params,
                approval_id=approval_id,
                correlation_id=f"vault-{action}-{abs(hash(resource)) % 100000}",
                timestamp=timestamp,
            )
        )
        if decision.decision in {"DENY", "ERROR"}:
            raise VaultError("PERMISSION_DENIED", decision.message)
        if decision.decision == "APPROVAL_REQUIRED":
            if approval_id is None:
                raise VaultApprovalRequiredError(
                    approval_action=action,
                    resource=resource,
                    request_fingerprint=decision.request_fingerprint,
                    message=decision.message,
                )
        return decision

    def _assert_read_access(self, entry: ArtifactRegistryEntry, auth_context: TrustedAuthContext) -> None:
        if auth_context.actor_kind in {"human_operator", "master_agent", "runtime_service", "trusted_verifier"}:
            return
        if entry.access_scope == "private":
            if entry.owner_agent_id != auth_context.actor_id:
                raise VaultError(
                    "ACCESS_DENIED",
                    f"Private artifact '{entry.artifact_id}' belongs to '{entry.owner_agent_id}'.",
                )
            return
        if entry.access_scope == "global":
            return
        assigned = set(auth_context.assigned_task_ids)
        if (
            entry.owner_agent_id == auth_context.actor_id
            or entry.task_id in assigned
            or bool(assigned & set(entry.referencing_task_ids))
        ):
            return
        raise VaultError(
            "ACCESS_DENIED",
            f"Worker '{auth_context.actor_id}' is not authorized to read artifact '{entry.artifact_id}'.",
        )

    def _canonical_blob_path(self, sha256: str) -> Path:
        self._blobs_dir.mkdir(parents=True, exist_ok=True)
        real_root = Path(os.path.realpath(self._blobs_dir))
        candidate = real_root / sha256[:2] / sha256
        real_target = Path(os.path.realpath(candidate))
        try:
            real_target.relative_to(real_root)
        except ValueError as exc:
            raise VaultError("INVALID_BLOB_PATH", f"Blob path '{real_target}' escaped Vault root.") from exc
        return real_target

    def _load_refs_map(self, conn: sqlite3.Connection, artifact_ids: list[str] | None = None) -> dict[str, list[str]]:
        if artifact_ids is not None and not artifact_ids:
            return {}
        if artifact_ids is None:
            rows = conn.execute(
                "SELECT artifact_id, referencing_task_id FROM artifact_task_references ORDER BY referencing_task_id ASC;"
            ).fetchall()
        else:
            placeholders = ",".join("?" for _ in artifact_ids)
            rows = conn.execute(
                f"SELECT artifact_id, referencing_task_id FROM artifact_task_references "
                f"WHERE artifact_id IN ({placeholders}) ORDER BY referencing_task_id ASC;",
                tuple(artifact_ids),
            ).fetchall()
        refs: dict[str, list[str]] = {}
        for row in rows:
            refs.setdefault(str(row["artifact_id"]), []).append(str(row["referencing_task_id"]))
        return refs

    def _fetch_entry(self, conn: sqlite3.Connection, artifact_id: str) -> ArtifactRegistryEntry | None:
        row = conn.execute(
            """
            SELECT e.*, b.storage_relpath
            FROM artifact_registry_entries e
            JOIN vault_blobs b ON b.sha256 = e.blob_sha256
            WHERE e.artifact_id = ?;
            """,
            (artifact_id,),
        ).fetchone()
        if row is None:
            return None
        refs = self._load_refs_map(conn, [artifact_id])
        return _row_to_entry(row, refs)

    def _emit_event(
        self,
        *,
        event_type: EventType,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
        task_id: str | None = None,
        run_id: str | None = None,
        artifact_id: str | None = None,
        correlation_id: str,
        payload: dict[str, Any],
    ) -> None:
        ev = Event(
            event_id=new_event_id(),
            timestamp=timestamp,
            event_type=event_type,
            source="core_runtime",
            task_id=task_id,
            run_id=run_id,
            actor_id=_event_actor(auth_context),
            artifact_id=artifact_id,
            correlation_id=correlation_id,
            redacted_payload=sanitize_payload(payload),
        )
        res = self._journal.append(ev)
        if res.status == "REJECTED":
            logger.warning("Failed to append %s event: %s", event_type, res.error)

    def register_artifact(
        self,
        *,
        auth_context: TrustedAuthContext,
        task_id: str,
        run_id: str,
        owner_agent_id: str,
        logical_key: str,
        relative_path: str,
        timestamp: datetime,
        source_path: Path | str | None = None,
        content_bytes: bytes | None = None,
        version: str | None = None,
        content_type: str | None = None,
        verification_status: ArtifactVerificationStatus = "UNVERIFIED",
        evidence_reference: str | None = None,
        access_scope: ArtifactAccessScope = "task",
        department: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        artifact_id: str | None = None,
    ) -> ArtifactRegistryEntry:
        """Persist a new content-addressed artifact version with SHA-256 deduplication and lineage."""
        clean_rel = validate_safe_relative_path(relative_path, "relative_path")
        self._authorize(
            auth_context=auth_context,
            action="vault.write",
            resource=f"vault:key:{logical_key}",
            timestamp=timestamp,
            task_id=task_id,
            run_id=run_id,
            parameters={"logical_key": logical_key, "relative_path": clean_rel},
        )

        if (source_path is None) == (content_bytes is None):
            raise VaultError("INVALID_ARTIFACT_SOURCE", "Provide exactly one of source_path or content_bytes.")
        if source_path is not None:
            src = Path(source_path)
            if not src.is_file():
                raise VaultError("SOURCE_FILE_NOT_FOUND", f"Artifact source '{src}' does not exist.")
            raw = src.read_bytes()
        else:
            assert content_bytes is not None
            raw = content_bytes

        digest = _sha256_bytes(raw)
        size_bytes = len(raw)
        resolved_ct = (
            content_type
            or mimetypes.guess_type(clean_rel)[0]
            or "application/octet-stream"
        )
        storage_rel = f"blobs/{digest[:2]}/{digest}"
        blob_path = self._canonical_blob_path(digest)
        if not blob_path.exists():
            _atomic_write_bytes(blob_path, raw)
        elif _sha256_bytes(blob_path.read_bytes()) != digest:
            _atomic_write_bytes(blob_path, raw)

        clean_tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
        clean_meta = sanitize_payload(metadata or {})
        aid = artifact_id or new_artifact_id()
        ts_iso = timestamp.isoformat()

        with self._store.transaction_scope() as conn:
            prev_row = conn.execute(
                """
                SELECT artifact_id, version_number
                FROM artifact_registry_entries
                WHERE logical_key = ?
                ORDER BY version_number DESC
                LIMIT 1;
                """,
                (logical_key,),
            ).fetchone()
            prev_id = str(prev_row["artifact_id"]) if prev_row is not None else None
            next_num = (int(prev_row["version_number"]) + 1) if prev_row is not None else 1
            resolved_ver = validate_semver(version or f"1.{next_num - 1}.0")

            conn.execute(
                """
                INSERT INTO vault_blobs (sha256, storage_relpath, size_bytes, content_type, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(sha256) DO UPDATE SET purged_at = NULL;
                """,
                (digest, storage_rel, size_bytes, resolved_ct, ts_iso),
            )
            try:
                conn.execute(
                    """
                    INSERT INTO artifact_registry_entries (
                        artifact_id, logical_key, version, version_number, previous_artifact_id,
                        task_id, run_id, owner_agent_id, relative_path, blob_sha256,
                        size_bytes, content_type, verification_status, evidence_reference,
                        access_scope, department, tags_json, metadata_json,
                        lifecycle_status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?);
                    """,
                    (
                        aid,
                        logical_key,
                        resolved_ver,
                        next_num,
                        prev_id,
                        task_id,
                        run_id,
                        owner_agent_id,
                        clean_rel,
                        digest,
                        size_bytes,
                        resolved_ct,
                        verification_status,
                        evidence_reference,
                        access_scope,
                        department,
                        _dumps(clean_tags),
                        _dumps(clean_meta),
                        ts_iso,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise VaultError("ARTIFACT_VERSION_CONFLICT", f"Failed to register artifact: {exc}") from exc

            conn.execute(
                """
                INSERT INTO artifact_task_references (
                    artifact_id, referencing_task_id, referencing_run_id, referenced_by, created_at
                ) VALUES (?, ?, ?, ?, ?);
                """,
                (aid, task_id, run_id, owner_agent_id, ts_iso),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO artifacts (
                    artifact_id, task_id, run_id, owner_agent_id, relative_path,
                    storage_ref, content_type, size_bytes, sha256, version,
                    verification_status, evidence_reference, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    aid,
                    task_id,
                    run_id,
                    owner_agent_id,
                    clean_rel,
                    storage_rel,
                    resolved_ct,
                    size_bytes,
                    digest,
                    resolved_ver,
                    verification_status,
                    evidence_reference,
                    ts_iso,
                ),
            )
            task_row = conn.execute(
                "SELECT artifact_ids_json FROM tasks WHERE task_id = ?;",
                (task_id,),
            ).fetchone()
            if task_row is not None:
                existing_ids = json.loads(str(task_row["artifact_ids_json"]))
                if aid not in existing_ids:
                    existing_ids.append(aid)
                    conn.execute(
                        "UPDATE tasks SET artifact_ids_json = ?, version = version + 1 WHERE task_id = ?;",
                        (_dumps(existing_ids), task_id),
                    )
            entry = self._fetch_entry(conn, aid)
            assert entry is not None

        self._emit_event(
            event_type="artifact.version_registered",
            auth_context=auth_context,
            timestamp=timestamp,
            task_id=task_id,
            run_id=run_id,
            artifact_id=aid,
            correlation_id=f"vault-reg-{aid}",
            payload={
                "artifact_id": aid,
                "logical_key": logical_key,
                "version": resolved_ver,
                "version_number": next_num,
                "sha256": digest,
                "size_bytes": size_bytes,
            },
        )
        return entry

    def attach_task_reference(
        self,
        artifact_id: str,
        *,
        referencing_task_id: str,
        referencing_run_id: str | None = None,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> ArtifactRegistryEntry:
        """Link a new Task to an existing ACTIVE artifact version, protecting it from deletion."""
        entry = self.get_artifact(artifact_id, auth_context=auth_context, timestamp=timestamp)
        if entry.lifecycle_status != "ACTIVE":
            raise VaultError(
                "ARTIFACT_UNAVAILABLE",
                f"Cannot reference artifact '{artifact_id}' with status '{entry.lifecycle_status}'.",
            )
        with self._store.transaction_scope() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO artifact_task_references (
                    artifact_id, referencing_task_id, referencing_run_id, referenced_by, created_at
                ) VALUES (?, ?, ?, ?, ?);
                """,
                (artifact_id, referencing_task_id, referencing_run_id, auth_context.actor_id, timestamp.isoformat()),
            )
            updated = self._fetch_entry(conn, artifact_id)
            assert updated is not None
            return updated

    def update_verification_status(
        self,
        artifact_id: str,
        *,
        verification_status: ArtifactVerificationStatus,
        evidence_reference: str | None,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> ArtifactRegistryEntry:
        """Independently update QA verification status and evidence link inside one transaction."""
        self._authorize(
            auth_context=auth_context,
            action="vault.write",
            resource=f"vault:verify:{artifact_id}",
            timestamp=timestamp,
        )
        if auth_context.actor_kind not in {"trusted_verifier", "human_operator"}:
            raise VaultError(
                "INDEPENDENT_VERIFIER_REQUIRED",
                f"Actor kind '{auth_context.actor_kind}' cannot grant Vault QA verification status.",
            )
        if verification_status == "VERIFIED_PASS" and (
            evidence_reference is None or not evidence_reference.strip()
        ):
            raise VaultError(
                "QA_EVIDENCE_REQUIRED",
                "VERIFIED_PASS requires non-empty independent QA evidence_reference.",
            )

        with self._store.transaction_scope() as conn:
            existing = self._fetch_entry(conn, artifact_id)
            if existing is None:
                raise VaultError("ARTIFACT_NOT_FOUND", f"Artifact '{artifact_id}' does not exist.")
            if auth_context.actor_id == existing.owner_agent_id:
                raise VaultError(
                    "SELF_VERIFICATION_FORBIDDEN",
                    f"Artifact producer '{existing.owner_agent_id}' cannot self-verify '{artifact_id}'.",
                )
            conn.execute(
                """
                UPDATE artifact_registry_entries
                SET verification_status = ?, evidence_reference = ?
                WHERE artifact_id = ?;
                """,
                (verification_status, evidence_reference, artifact_id),
            )
            conn.execute(
                """
                UPDATE artifacts
                SET verification_status = ?, evidence_reference = ?
                WHERE artifact_id = ?;
                """,
                (verification_status, evidence_reference, artifact_id),
            )
            updated = self._fetch_entry(conn, artifact_id)
            assert updated is not None

        ev_id_ref = (
            evidence_reference
            if evidence_reference and evidence_reference.startswith("evd_")
            else None
        )
        self._journal.append(
            Event(
                event_id=new_event_id(),
                timestamp=timestamp,
                event_type="qa.verification_recorded",
                source="qa_verifier",
                task_id=updated.task_id,
                run_id=updated.run_id,
                artifact_id=updated.artifact_id,
                evidence_id=ev_id_ref,
                correlation_id=f"qa-ver-{updated.artifact_id}",
                redacted_payload=sanitize_payload(
                    {
                        "artifact_id": updated.artifact_id,
                        "verification_status": verification_status,
                        "evidence_reference": evidence_reference,
                        "verified_by": auth_context.actor_id,
                    }
                ),
            )
        )
        return updated

    def get_artifact(
        self,
        artifact_id: str,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> ArtifactRegistryEntry:
        """Fetch artifact metadata after enforcing governance permission and scope access."""
        self._authorize(
            auth_context=auth_context,
            action="vault.read",
            resource=f"vault:artifact:{artifact_id}",
            timestamp=timestamp,
        )
        with self._store.open_connection() as conn:
            entry = self._fetch_entry(conn, artifact_id)
        if entry is None:
            raise VaultError("ARTIFACT_NOT_FOUND", f"Artifact '{artifact_id}' does not exist.")
        self._assert_read_access(entry, auth_context)
        return entry

    def inspect_artifact_for_delivery(self, artifact_id: str) -> tuple[ArtifactRegistryEntry, Path] | None:
        """Return Vault metadata and canonical blob path for Master delivery verification."""
        with self._store.open_connection() as conn:
            entry = self._fetch_entry(conn, artifact_id)
        if entry is None:
            return None
        return entry, self._canonical_blob_path(entry.blob_sha256)

    def get_version_history(
        self,
        logical_key: str,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> ArtifactVersionHistory:
        """Return ordered version history for a logical artifact key."""
        self._authorize(
            auth_context=auth_context,
            action="vault.read",
            resource=f"vault:key:{logical_key}",
            timestamp=timestamp,
        )
        with self._store.open_connection() as conn:
            rows = conn.execute(
                """
                SELECT e.*, b.storage_relpath
                FROM artifact_registry_entries e
                JOIN vault_blobs b ON b.sha256 = e.blob_sha256
                WHERE e.logical_key = ?
                ORDER BY e.version_number ASC;
                """,
                (logical_key,),
            ).fetchall()
            if not rows:
                raise VaultError("ARTIFACT_NOT_FOUND", f"No versions found for logical_key '{logical_key}'.")
            refs = self._load_refs_map(conn, [str(r["artifact_id"]) for r in rows])
            versions = [_row_to_entry(r, refs) for r in rows]

        for entry in versions:
            self._assert_read_access(entry, auth_context)
        return ArtifactVersionHistory(
            logical_key=logical_key,
            latest_artifact_id=versions[-1].artifact_id,
            version_count=len(versions),
            versions=versions,
        )

    def list_artifacts(
        self,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
        task_id: str | None = None,
        logical_key: str | None = None,
        include_trashed: bool = False,
    ) -> list[ArtifactRegistryEntry]:
        """List accessible artifacts matching optional task_id or logical_key filters."""
        self._authorize(
            auth_context=auth_context,
            action="vault.read",
            resource="vault:artifacts:list",
            timestamp=timestamp,
            task_id=task_id,
        )
        clauses: list[str] = []
        params: list[object] = []
        if task_id is not None:
            clauses.append(
                "(e.task_id = ? OR EXISTS (SELECT 1 FROM artifact_task_references r "
                "WHERE r.artifact_id = e.artifact_id AND r.referencing_task_id = ?))"
            )
            params.extend([task_id, task_id])
        if logical_key is not None:
            clauses.append("e.logical_key = ?")
            params.append(logical_key)
        if include_trashed:
            clauses.append("e.lifecycle_status != 'PURGED'")
        else:
            clauses.append("e.lifecycle_status = 'ACTIVE'")
        where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""

        with self._store.open_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT e.*, b.storage_relpath
                FROM artifact_registry_entries e
                JOIN vault_blobs b ON b.sha256 = e.blob_sha256
                {where_sql}
                ORDER BY e.created_at ASC, e.version_number ASC;
                """,
                tuple(params),
            ).fetchall()
            refs = self._load_refs_map(conn, [str(r["artifact_id"]) for r in rows])
            entries = [_row_to_entry(r, refs) for r in rows]

        visible: list[ArtifactRegistryEntry] = []
        for entry in entries:
            try:
                self._assert_read_access(entry, auth_context)
                visible.append(entry)
            except VaultError:
                continue
        return visible

    def read_verified_bytes(
        self,
        artifact_id: str,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> tuple[ArtifactRegistryEntry, bytes]:
        """Read blob bytes strictly after enforcing permission, ACTIVE status, and SHA-256 verification."""
        entry = self.get_artifact(artifact_id, auth_context=auth_context, timestamp=timestamp)
        if entry.lifecycle_status != "ACTIVE":
            raise VaultError(
                "ARTIFACT_UNAVAILABLE",
                f"Artifact '{artifact_id}' is '{entry.lifecycle_status}' and cannot be downloaded.",
            )
        blob_path = self._canonical_blob_path(entry.blob_sha256)
        if not blob_path.is_file():
            raise VaultError("BLOB_MISSING", f"Backed blob '{entry.blob_sha256}' is missing from Vault.")
        raw = blob_path.read_bytes()
        if _sha256_bytes(raw) != entry.blob_sha256:
            raise VaultError(
                "INTEGRITY_MISMATCH",
                f"Artifact '{artifact_id}' failed SHA-256 integrity verification on read.",
            )
        return entry, raw

    def preview_artifact(
        self,
        artifact_id: str,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
        max_chars: int = 4000,
        max_rows: int = 20,
    ) -> ArtifactPreviewResult:
        """Produce deterministic format-aware preview metadata from verified blob bytes."""
        entry, raw = self.read_verified_bytes(artifact_id, auth_context=auth_context, timestamp=timestamp)
        kind = _preview_kind_for(entry.content_type, entry.relative_path)
        truncated = False
        data: dict[str, Any]

        if kind == "TEXT":
            text = redact_secrets(raw.decode("utf-8", errors="replace"))
            truncated = len(text) > max_chars
            bounded = text[:max_chars]
            data = {"text": bounded, "line_count": len(text.splitlines())}
        elif kind == "DOCX":
            paragraphs, tables = _extract_docx_info(raw)
            joined = redact_secrets("\n".join(paragraphs))
            truncated = len(joined) > max_chars
            data = {
                "paragraph_count": len(paragraphs),
                "table_count": len(tables),
                "paragraphs_preview": [redact_secrets(p) for p in paragraphs[:25]],
                "text_excerpt": joined[:max_chars],
            }
        elif kind == "XLSX":
            names, sheets_preview, row_counts = _extract_xlsx_info(raw, max_rows=max_rows)
            truncated = any(row_counts[n] > max_rows for n in names)
            data = {"sheet_names": names, "row_counts": row_counts, "sheets_preview": sheets_preview}
        elif kind == "PDF":
            with pymupdf.open(stream=raw, filetype="pdf") as pdf:
                page_count = pdf.page_count
                excerpts = [redact_secrets(pdf[i].get_text("text")) for i in range(min(page_count, 5))]
            combined = "\n".join(excerpts)
            truncated = page_count > 5 or len(combined) > max_chars
            data = {"page_count": page_count, "text_excerpt": combined[:max_chars]}
        elif kind == "IMAGE":
            with Image.open(io.BytesIO(raw)) as img:
                width, height = img.size
                img_format = img.format
            data = {"width": width, "height": height, "format": img_format}
        else:
            data = {"message": "Binary format preview returns verified cryptographic metadata only."}

        return ArtifactPreviewResult(
            artifact_id=entry.artifact_id,
            content_type=entry.content_type,
            preview_kind=kind,
            truncated=truncated,
            size_bytes=entry.size_bytes,
            sha256=entry.blob_sha256,
            preview_data=data,
        )

    def compare_artifacts(
        self,
        left_artifact_id: str,
        right_artifact_id: str,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
        max_diff_lines: int = 120,
    ) -> ArtifactComparisonResult:
        """Compare two artifact versions deterministically using byte hash, structure, and unified_diff."""
        left_entry, left_bytes = self.read_verified_bytes(
            left_artifact_id, auth_context=auth_context, timestamp=timestamp
        )
        right_entry, right_bytes = self.read_verified_bytes(
            right_artifact_id, auth_context=auth_context, timestamp=timestamp
        )
        identical = left_entry.blob_sha256 == right_entry.blob_sha256
        size_delta = right_entry.size_bytes - left_entry.size_bytes

        left_kind = _preview_kind_for(left_entry.content_type, left_entry.relative_path)
        right_kind = _preview_kind_for(right_entry.content_type, right_entry.relative_path)
        diff_lines: list[str] = []
        metrics: dict[str, Any] = {}

        if left_kind == "TEXT" and right_kind == "TEXT":
            method = "deterministic_text_diff"
            l_lines = redact_secrets(left_bytes.decode("utf-8", errors="replace")).splitlines()
            r_lines = redact_secrets(right_bytes.decode("utf-8", errors="replace")).splitlines()
            raw_diff = list(
                difflib.unified_diff(
                    l_lines,
                    r_lines,
                    fromfile=f"{left_entry.relative_path}@{left_entry.version}",
                    tofile=f"{right_entry.relative_path}@{right_entry.version}",
                    lineterm="",
                )
            )
            diff_lines = raw_diff[:max_diff_lines]
            metrics = {
                "left_line_count": len(l_lines),
                "right_line_count": len(r_lines),
                "diff_line_count": len(raw_diff),
                "truncated_diff": len(raw_diff) > max_diff_lines,
            }
        elif left_kind == "DOCX" and right_kind == "DOCX":
            method = "deterministic_structural"
            l_para, l_tbl = _extract_docx_info(left_bytes)
            r_para, r_tbl = _extract_docx_info(right_bytes)
            raw_diff = list(
                difflib.unified_diff(
                    [redact_secrets(p) for p in l_para],
                    [redact_secrets(p) for p in r_para],
                    fromfile=f"docx@{left_entry.version}",
                    tofile=f"docx@{right_entry.version}",
                    lineterm="",
                )
            )
            diff_lines = raw_diff[:max_diff_lines]
            metrics = {
                "left_paragraph_count": len(l_para),
                "right_paragraph_count": len(r_para),
                "left_table_count": len(l_tbl),
                "right_table_count": len(r_tbl),
            }
        elif left_kind == "XLSX" and right_kind == "XLSX":
            method = "deterministic_structural"
            l_names, _, l_rows = _extract_xlsx_info(left_bytes, max_rows=5)
            r_names, _, r_rows = _extract_xlsx_info(right_bytes, max_rows=5)
            metrics = {
                "left_sheet_names": l_names,
                "right_sheet_names": r_names,
                "added_sheets": [s for s in r_names if s not in l_names],
                "removed_sheets": [s for s in l_names if s not in r_names],
                "left_row_counts": l_rows,
                "right_row_counts": r_rows,
            }
        else:
            method = "binary_hash_only"
            metrics = {"left_kind": left_kind, "right_kind": right_kind}

        return ArtifactComparisonResult(
            left_artifact_id=left_entry.artifact_id,
            right_artifact_id=right_entry.artifact_id,
            left_version=left_entry.version,
            right_version=right_entry.version,
            identical_bytes=identical,
            left_sha256=left_entry.blob_sha256,
            right_sha256=right_entry.blob_sha256,
            size_delta_bytes=size_delta,
            comparison_method=method,
            summary_metrics=metrics,
            diff_lines=diff_lines,
        )

    def trash_artifact(
        self,
        artifact_id: str,
        *,
        auth_context: TrustedAuthContext,
        reason: str,
        timestamp: datetime,
    ) -> TrashActionResult:
        """Soft-delete an ACTIVE artifact into TRASHED state without deleting its backing blob."""
        self._authorize(
            auth_context=auth_context,
            action="vault.delete",
            resource=f"vault:artifact:{artifact_id}",
            timestamp=timestamp,
            parameters={"artifact_id": artifact_id, "reason": reason},
        )
        with self._store.transaction_scope() as conn:
            entry = self._fetch_entry(conn, artifact_id)
            if entry is None:
                raise VaultError("ARTIFACT_NOT_FOUND", f"Artifact '{artifact_id}' does not exist.")
            if entry.lifecycle_status == "PURGED":
                raise VaultError("ALREADY_PURGED", f"Artifact '{artifact_id}' has already been purged.")
            if entry.lifecycle_status == "TRASHED":
                raise VaultError("ALREADY_TRASHED", f"Artifact '{artifact_id}' is already in trash.")
            conn.execute(
                """
                UPDATE artifact_registry_entries
                SET lifecycle_status = 'TRASHED', trashed_at = ?, trashed_by = ?, trash_reason = ?
                WHERE artifact_id = ?;
                """,
                (timestamp.isoformat(), auth_context.actor_id, redact_secrets(reason), artifact_id),
            )
            other_refs = [t for t in entry.referencing_task_ids if t != entry.task_id]

        self._emit_event(
            event_type="artifact.trashed",
            auth_context=auth_context,
            timestamp=timestamp,
            task_id=entry.task_id,
            run_id=entry.run_id,
            artifact_id=artifact_id,
            correlation_id=f"vault-trash-{artifact_id}",
            payload={
                "artifact_id": artifact_id,
                "reason": redact_secrets(reason),
                "shared_with_other_tasks": bool(other_refs),
            },
        )
        return TrashActionResult(
            artifact_id=artifact_id,
            lifecycle_status="TRASHED",
            shared_with_other_tasks=bool(other_refs),
            referencing_task_ids=entry.referencing_task_ids,
            blob_deleted=False,
            message="Artifact moved to trash cleanly; backing blob retained for restore/shared safety.",
        )

    def restore_artifact(
        self,
        artifact_id: str,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> TrashActionResult:
        """Restore a TRASHED artifact back to ACTIVE after verifying blob SHA-256 integrity."""
        self._authorize(
            auth_context=auth_context,
            action="trash.restore",
            resource=f"vault:artifact:{artifact_id}",
            timestamp=timestamp,
            parameters={"artifact_id": artifact_id},
        )
        with self._store.transaction_scope() as conn:
            entry = self._fetch_entry(conn, artifact_id)
            if entry is None:
                raise VaultError("ARTIFACT_NOT_FOUND", f"Artifact '{artifact_id}' does not exist.")
            if entry.lifecycle_status != "TRASHED":
                raise VaultError(
                    "INVALID_RESTORE_STATE",
                    f"Artifact '{artifact_id}' has lifecycle_status='{entry.lifecycle_status}', not TRASHED.",
                )
            blob_path = self._canonical_blob_path(entry.blob_sha256)
            if not blob_path.is_file() or _sha256_bytes(blob_path.read_bytes()) != entry.blob_sha256:
                raise VaultError("INTEGRITY_MISMATCH", f"Backing blob for '{artifact_id}' failed integrity check.")
            conn.execute(
                """
                UPDATE artifact_registry_entries
                SET lifecycle_status = 'ACTIVE', trashed_at = NULL, trashed_by = NULL, trash_reason = NULL
                WHERE artifact_id = ?;
                """,
                (artifact_id,),
            )

        self._emit_event(
            event_type="artifact.restored",
            auth_context=auth_context,
            timestamp=timestamp,
            task_id=entry.task_id,
            run_id=entry.run_id,
            artifact_id=artifact_id,
            correlation_id=f"vault-restore-{artifact_id}",
            payload={"artifact_id": artifact_id, "sha256": entry.blob_sha256},
        )
        other_refs = [t for t in entry.referencing_task_ids if t != entry.task_id]
        return TrashActionResult(
            artifact_id=artifact_id,
            lifecycle_status="ACTIVE",
            shared_with_other_tasks=bool(other_refs),
            referencing_task_ids=entry.referencing_task_ids,
            blob_deleted=False,
            message="Artifact restored cleanly to ACTIVE.",
        )

    def purge_artifact(
        self,
        artifact_id: str,
        *,
        auth_context: TrustedAuthContext,
        approval_id: str | None = None,
        timestamp: datetime,
    ) -> TrashActionResult:
        """Permanently purge a TRASHED unshared artifact behind single-use Human Approval Gate."""
        params = {"artifact_id": artifact_id}
        resource = f"vault:artifact:{artifact_id}"
        decision = self._permissions.evaluate_and_audit(
            PolicyRequest(
                auth_context=auth_context,
                action="trash.purge",
                resource=resource,
                parameters=params,
                approval_id=approval_id,
                correlation_id=f"vault-purge-{artifact_id}",
                timestamp=timestamp,
            )
        )
        if decision.decision in {"DENY", "ERROR"}:
            raise VaultError("PERMISSION_DENIED", decision.message)

        with self._store.open_connection() as conn:
            entry = self._fetch_entry(conn, artifact_id)
        if entry is None:
            raise VaultError("ARTIFACT_NOT_FOUND", f"Artifact '{artifact_id}' does not exist.")
        if entry.lifecycle_status != "TRASHED":
            raise VaultError(
                "MUST_TRASH_BEFORE_PURGE",
                f"Artifact '{artifact_id}' must be in TRASHED state before purge.",
            )
        other_refs = [t for t in entry.referencing_task_ids if t != entry.task_id]
        if other_refs:
            raise VaultError(
                "SHARED_REFERENCE_PROTECTED",
                f"Artifact '{artifact_id}' is referenced by other tasks {other_refs}; purge blocked.",
            )

        def _do_purge() -> bool:
            with self._store.transaction_scope() as conn:
                conn.execute(
                    """
                    UPDATE artifact_registry_entries
                    SET lifecycle_status = 'PURGED', purged_at = ?
                    WHERE artifact_id = ?;
                    """,
                    (timestamp.isoformat(), artifact_id),
                )
                remaining = conn.execute(
                    """
                    SELECT COUNT(*) AS cnt FROM artifact_registry_entries
                    WHERE blob_sha256 = ? AND lifecycle_status IN ('ACTIVE', 'TRASHED');
                    """,
                    (entry.blob_sha256,),
                ).fetchone()
                no_refs = int(remaining["cnt"]) == 0 if remaining is not None else False
                if no_refs:
                    conn.execute(
                        "UPDATE vault_blobs SET purged_at = ? WHERE sha256 = ?;",
                        (timestamp.isoformat(), entry.blob_sha256),
                    )
                return no_refs

        approval_svc = self._permissions.approval_service
        if decision.decision == "APPROVAL_REQUIRED":
            if approval_id is None or approval_svc is None:
                raise VaultApprovalRequiredError(
                    approval_action="trash.purge",
                    resource=resource,
                    request_fingerprint=decision.request_fingerprint,
                    message=decision.message,
                )
            fingerprint = compute_request_fingerprint(
                action="trash.purge",
                resource=resource,
                task_id=None,
                run_id=None,
                parameters=params,
            )

            try:
                _, should_unlink = approval_svc.verify_and_consume(
                    approval_id,
                    expected_action="trash.purge",
                    expected_resource=resource,
                    expected_task_id=None,
                    expected_run_id=None,
                    expected_fingerprint=fingerprint,
                    timestamp=timestamp,
                    system_deny=decision.system_deny,
                    action_fn=_do_purge,
                    correlation_id=f"vault-purge-{artifact_id}",
                )
            except ApprovalError as exc:
                raise VaultError(exc.error_code, exc.message) from exc
        else:
            should_unlink = _do_purge()

        blob_deleted = False
        if should_unlink:
            blob_path = self._canonical_blob_path(entry.blob_sha256)
            if blob_path.is_file():
                blob_path.unlink()
                blob_deleted = True

        self._emit_event(
            event_type="artifact.purged",
            auth_context=auth_context,
            timestamp=timestamp,
            task_id=entry.task_id,
            run_id=entry.run_id,
            artifact_id=artifact_id,
            correlation_id=f"vault-purge-{artifact_id}",
            payload={"artifact_id": artifact_id, "blob_deleted": blob_deleted},
        )
        return TrashActionResult(
            artifact_id=artifact_id,
            lifecycle_status="PURGED",
            shared_with_other_tasks=False,
            referencing_task_ids=entry.referencing_task_ids,
            blob_deleted=blob_deleted,
            message="Unshared artifact purged cleanly.",
        )

    def safe_bulk_trash(
        self,
        artifact_ids: list[str],
        *,
        auth_context: TrustedAuthContext,
        reason: str,
        timestamp: datetime,
        acting_task_id: str | None = None,
    ) -> BulkTrashReport:
        """Trash unshared artifacts while sparing any artifact referenced by another Task."""
        self._authorize(
            auth_context=auth_context,
            action="vault.delete",
            resource="vault:artifacts:bulk_trash",
            timestamp=timestamp,
            task_id=acting_task_id,
            parameters={"artifact_ids": artifact_ids, "reason": reason},
        )
        trashed_ids: list[str] = []
        skipped_protected: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        for aid in artifact_ids:
            with self._store.open_connection() as conn:
                entry = self._fetch_entry(conn, aid)
            if entry is None:
                errors.append({"artifact_id": aid, "error_code": "ARTIFACT_NOT_FOUND"})
                continue
            if entry.lifecycle_status != "ACTIVE":
                errors.append({"artifact_id": aid, "error_code": f"STATUS_{entry.lifecycle_status}"})
                continue
            owner_scope = acting_task_id or entry.task_id
            other_refs = [t for t in entry.referencing_task_ids if t != owner_scope]
            if other_refs:
                skipped_protected.append(
                    {
                        "artifact_id": aid,
                        "reason_code": "SHARED_REFERENCE_PROTECTED",
                        "referencing_task_ids": other_refs,
                    }
                )
                continue
            self.trash_artifact(aid, auth_context=auth_context, reason=reason, timestamp=timestamp)
            trashed_ids.append(aid)

        return BulkTrashReport(
            requested_ids=artifact_ids,
            trashed_ids=trashed_ids,
            skipped_protected=skipped_protected,
            errors=errors,
        )

    def create_backup(
        self,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
        retention_days: int = 30,
    ) -> VaultBackupRecord:
        """Create an atomic SQLite + Vault blob snapshot backup with per-file SHA-256 manifest."""
        self._authorize(
            auth_context=auth_context,
            action="vault.backup",
            resource="vault:backups",
            timestamp=timestamp,
            parameters={"retention_days": retention_days},
        )
        backup_id = new_backup_id()
        backup_rel = f"backups/{backup_id}"
        backup_dir = self._backups_dir / backup_id
        blobs_bak_dir = backup_dir / "blobs"
        blobs_bak_dir.mkdir(parents=True, exist_ok=True)
        backup_db_path = backup_dir / "ai_village.db"

        dest_conn = open_sqlite_connection(SQLiteConfig(db_path=backup_db_path, journal_mode="DELETE"))
        try:
            with self._store.open_connection() as src_conn:
                src_conn.backup(dest_conn)
        finally:
            dest_conn.close()

        db_sha256 = _sha256_bytes(backup_db_path.read_bytes())

        with self._store.open_connection() as conn:
            blob_rows = conn.execute(
                "SELECT sha256, storage_relpath FROM vault_blobs WHERE purged_at IS NULL ORDER BY sha256 ASC;"
            ).fetchall()

        blob_hashes: dict[str, str] = {}
        for row in blob_rows:
            sha = str(row["sha256"])
            src_blob = self._canonical_blob_path(sha)
            if not src_blob.is_file():
                raise VaultError("BLOB_MISSING", f"Cannot create backup; blob '{sha}' is missing.")
            raw = src_blob.read_bytes()
            if _sha256_bytes(raw) != sha:
                raise VaultError("INTEGRITY_MISMATCH", f"Cannot create backup; blob '{sha}' corrupted.")
            rel = str(row["storage_relpath"])
            _atomic_write_bytes(backup_dir / rel, raw)
            blob_hashes[rel] = sha

        manifest_doc = {
            "backup_id": backup_id,
            "created_at": timestamp.isoformat(),
            "created_by": auth_context.actor_id,
            "retention_days": retention_days,
            "db_sha256": db_sha256,
            "blobs": blob_hashes,
        }
        manifest_bytes = _dumps(manifest_doc).encode("utf-8")
        manifest_sha = _sha256_bytes(manifest_bytes)
        _atomic_write_bytes(backup_dir / "backup_manifest.json", manifest_bytes)

        with self._store.transaction_scope() as conn:
            conn.execute(
                """
                INSERT INTO vault_backups (
                    backup_id, backup_relpath, db_sha256, manifest_sha256,
                    blob_count, created_by, created_at, retention_days
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    backup_id,
                    backup_rel,
                    db_sha256,
                    manifest_sha,
                    len(blob_hashes),
                    auth_context.actor_id,
                    timestamp.isoformat(),
                    retention_days,
                ),
            )

        self._emit_event(
            event_type="vault.backup_created",
            auth_context=auth_context,
            timestamp=timestamp,
            correlation_id=f"vault-bkp-{backup_id}",
            payload={"backup_id": backup_id, "db_sha256": db_sha256, "blob_count": len(blob_hashes)},
        )
        return VaultBackupRecord(
            backup_id=backup_id,
            backup_relpath=backup_rel,
            db_sha256=db_sha256,
            manifest_sha256=manifest_sha,
            blob_count=len(blob_hashes),
            created_by=auth_context.actor_id,
            created_at=timestamp,
            retention_days=retention_days,
        )

    def prune_expired_backups(self, *, auth_context: TrustedAuthContext, now: datetime) -> list[str]:
        """Remove backups whose retention period has elapsed strictly by timestamp comparison."""
        self._authorize(
            auth_context=auth_context,
            action="vault.backup",
            resource="vault:backups:prune",
            timestamp=now,
            parameters={"action": "prune"},
        )
        pruned: list[str] = []
        with self._store.transaction_scope() as conn:
            rows = conn.execute("SELECT backup_id, created_at, retention_days FROM vault_backups;").fetchall()
            for row in rows:
                created_dt = datetime.fromisoformat(str(row["created_at"]))
                if created_dt + timedelta(days=int(row["retention_days"])) <= now:
                    bid = str(row["backup_id"])
                    bak_dir = self._backups_dir / bid
                    if bak_dir.exists():
                        shutil.rmtree(bak_dir)
                    conn.execute("DELETE FROM vault_backups WHERE backup_id = ?;", (bid,))
                    pruned.append(bid)
        return pruned

    def restore_backup(
        self,
        backup_id: str,
        *,
        auth_context: TrustedAuthContext,
        approval_id: str | None = None,
        timestamp: datetime,
        target_db_path: Path | str | None = None,
        target_vault_root: Path | str | None = None,
    ) -> VaultRestoreReport:
        """Verify cryptographic backup integrity fail-closed, consume approval, and restore snapshot."""
        params = {"backup_id": backup_id}
        resource = f"vault:backup:{backup_id}"
        decision = self._permissions.evaluate_and_audit(
            PolicyRequest(
                auth_context=auth_context,
                action="vault.restore",
                resource=resource,
                parameters=params,
                approval_id=approval_id,
                correlation_id=f"vault-restore-bkp-{backup_id}",
                timestamp=timestamp,
            )
        )
        if decision.decision in {"DENY", "ERROR"}:
            raise VaultError("PERMISSION_DENIED", decision.message)

        with self._store.open_connection() as conn:
            row = conn.execute("SELECT * FROM vault_backups WHERE backup_id = ?;", (backup_id,)).fetchone()
        if row is None:
            raise VaultError("BACKUP_NOT_FOUND", f"Backup '{backup_id}' does not exist.")

        backup_dir = self._backups_dir / backup_id
        manifest_path = backup_dir / "backup_manifest.json"
        bak_db_path = backup_dir / "ai_village.db"
        if not manifest_path.is_file() or not bak_db_path.is_file():
            raise VaultError("BACKUP_CORRUPTED", f"Backup '{backup_id}' files are missing.")

        manifest_bytes = manifest_path.read_bytes()
        if _sha256_bytes(manifest_bytes) != str(row["manifest_sha256"]):
            raise VaultError("BACKUP_CORRUPTED", f"Backup '{backup_id}' manifest SHA-256 mismatch.")
        if _sha256_bytes(bak_db_path.read_bytes()) != str(row["db_sha256"]):
            raise VaultError("BACKUP_CORRUPTED", f"Backup '{backup_id}' DB SHA-256 mismatch.")

        manifest_doc = json.loads(manifest_bytes.decode("utf-8"))
        blobs_map: dict[str, str] = manifest_doc.get("blobs", {})
        verified_blobs: dict[str, bytes] = {}
        for rel_path, expected_sha in blobs_map.items():
            blob_file = backup_dir / rel_path
            if not blob_file.is_file():
                raise VaultError("BACKUP_CORRUPTED", f"Backed blob '{rel_path}' missing.")
            raw = blob_file.read_bytes()
            if _sha256_bytes(raw) != expected_sha:
                raise VaultError("BACKUP_CORRUPTED", f"Backed blob '{rel_path}' failed SHA-256 check.")
            verified_blobs[rel_path] = raw

        consumed_approval_row: dict[str, Any] | None = None
        consumed_event_rows: list[dict[str, Any]] = []
        approval_svc = self._permissions.approval_service
        if decision.decision == "APPROVAL_REQUIRED":
            if approval_id is None or approval_svc is None:
                raise VaultApprovalRequiredError(
                    approval_action="vault.restore",
                    resource=resource,
                    request_fingerprint=decision.request_fingerprint,
                    message=decision.message,
                )
            fingerprint = compute_request_fingerprint(
                action="vault.restore",
                resource=resource,
                task_id=None,
                run_id=None,
                parameters=params,
            )
            try:
                approval_svc.verify_and_consume(
                    approval_id,
                    expected_action="vault.restore",
                    expected_resource=resource,
                    expected_task_id=None,
                    expected_run_id=None,
                    expected_fingerprint=fingerprint,
                    timestamp=timestamp,
                    system_deny=decision.system_deny,
                    action_fn=lambda: True,
                    correlation_id=f"vault-restore-bkp-{backup_id}",
                )
            except ApprovalError as exc:
                raise VaultError(exc.error_code, exc.message) from exc
            with self._store.open_connection() as conn:
                app_row = conn.execute(
                    "SELECT * FROM approval_requests WHERE approval_id = ?;",
                    (approval_id,),
                ).fetchone()
                if app_row is not None:
                    consumed_approval_row = dict(app_row)
                    ev_ids = [
                        eid
                        for eid in (
                            consumed_approval_row.get("request_event_id"),
                            consumed_approval_row.get("decision_event_id"),
                            consumed_approval_row.get("consumed_event_id"),
                        )
                        if isinstance(eid, str) and eid
                    ]
                    if ev_ids:
                        placeholders = ",".join("?" for _ in ev_ids)
                        ev_rows = conn.execute(
                            f"SELECT * FROM events WHERE event_id IN ({placeholders}) ORDER BY sequence_number ASC;",
                            tuple(ev_ids),
                        ).fetchall()
                        consumed_event_rows = [dict(r) for r in ev_rows]

        dest_db = Path(target_db_path) if target_db_path is not None else self._store.config.db_path
        dest_vault = Path(target_vault_root) if target_vault_root is not None else self._root
        for rel_path, raw in verified_blobs.items():
            _atomic_write_bytes(dest_vault / rel_path, raw)

        if target_db_path is not None:
            _atomic_write_bytes(dest_db, bak_db_path.read_bytes())
        else:
            live_backup_rows: list[dict[str, Any]] = []
            with sqlite3.connect(str(bak_db_path)) as src_bak:
                with self._store.open_connection() as live_conn:
                    live_backup_rows = [
                        dict(r) for r in live_conn.execute("SELECT * FROM vault_backups;").fetchall()
                    ]
                    live_conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                    src_bak.backup(live_conn)
            if consumed_approval_row is not None or live_backup_rows:
                with self._store.transaction_scope() as conn:
                    for bkp_row in live_backup_rows:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO vault_backups (
                                backup_id, backup_relpath, db_sha256, manifest_sha256,
                                blob_count, created_by, created_at, retention_days
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                            """,
                            (
                                bkp_row["backup_id"],
                                bkp_row["backup_relpath"],
                                bkp_row["db_sha256"],
                                bkp_row["manifest_sha256"],
                                bkp_row["blob_count"],
                                bkp_row["created_by"],
                                bkp_row["created_at"],
                                bkp_row["retention_days"],
                            ),
                        )
                    for ev_row in consumed_event_rows:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO events (
                                event_id, timestamp, event_type, source,
                                task_id, run_id, session_id, actor_id,
                                provider_id, connection_id, model_id, artifact_id,
                                evidence_id, correlation_id, redacted_payload_json, batch_id
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                            """,
                            (
                                ev_row["event_id"],
                                ev_row["timestamp"],
                                ev_row["event_type"],
                                ev_row["source"],
                                ev_row["task_id"],
                                ev_row["run_id"],
                                ev_row["session_id"],
                                ev_row["actor_id"],
                                ev_row["provider_id"],
                                ev_row["connection_id"],
                                ev_row["model_id"],
                                ev_row["artifact_id"],
                                ev_row["evidence_id"],
                                ev_row["correlation_id"],
                                ev_row["redacted_payload_json"],
                                ev_row["batch_id"],
                            ),
                        )
                    if consumed_approval_row is not None:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO approval_requests (
                                approval_id, requester_id, requester_kind, action, target_resource,
                                task_id, run_id, request_fingerprint, policy_snapshot_json, risk_summary,
                                status, requested_at, expires_at, decided_at, decided_by, decision_reason,
                                consumed_at, request_event_id, decision_event_id, consumed_event_id, version
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                            """,
                            (
                                consumed_approval_row["approval_id"],
                                consumed_approval_row["requester_id"],
                                consumed_approval_row["requester_kind"],
                                consumed_approval_row["action"],
                                consumed_approval_row["target_resource"],
                                consumed_approval_row["task_id"],
                                consumed_approval_row["run_id"],
                                consumed_approval_row["request_fingerprint"],
                                consumed_approval_row["policy_snapshot_json"],
                                consumed_approval_row["risk_summary"],
                                consumed_approval_row["status"],
                                consumed_approval_row["requested_at"],
                                consumed_approval_row["expires_at"],
                                consumed_approval_row["decided_at"],
                                consumed_approval_row["decided_by"],
                                consumed_approval_row["decision_reason"],
                                consumed_approval_row["consumed_at"],
                                consumed_approval_row["request_event_id"],
                                consumed_approval_row["decision_event_id"],
                                consumed_approval_row["consumed_event_id"],
                                consumed_approval_row["version"],
                            ),
                        )

        with sqlite_connection(SQLiteConfig(db_path=dest_db, enable_foreign_keys=True)) as check_conn:
            verify_schema_compatibility(check_conn)
            integrity = run_integrity_checks(check_conn)

        self._emit_event(
            event_type="vault.backup_restored",
            auth_context=auth_context,
            timestamp=timestamp,
            correlation_id=f"vault-restore-{backup_id}",
            payload={
                "backup_id": backup_id,
                "restored_blob_count": len(verified_blobs),
                "integrity_ok": integrity.integrity_ok,
                "foreign_key_ok": integrity.foreign_key_ok,
            },
        )
        return VaultRestoreReport(
            backup_id=backup_id,
            restored_db_path=str(dest_db),
            restored_blob_count=len(verified_blobs),
            integrity_ok=integrity.integrity_ok,
            foreign_key_ok=integrity.foreign_key_ok,
            restored_at=timestamp,
        )
