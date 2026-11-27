"""Phase 08 Evidence Vault and Permission-First Knowledge Retrieval Service."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import sqlite3
from typing import Any

from core.contracts.event import Event, EventType
from core.contracts.identifiers import new_event_id, sanitize_payload
from core.events.journal import EventJournal
from core.governance.manager import PermissionManager
from core.governance.models import PolicyRequest, TrustedAuthContext
from engines.antigravity.diagnostics import redact_secrets
from storage.artifacts.models import ArtifactAccessScope, ArtifactRegistryEntry, VaultError
from storage.artifacts.vault import ArtifactVaultService
from storage.knowledge.models import (
    EvidenceKind,
    EvidenceRecord,
    KnowledgeApprovalStatus,
    KnowledgeCategory,
    KnowledgeEntry,
    KnowledgeSearchHit,
    KnowledgeSearchQuery,
    KnowledgeSearchResult,
    QaInspectionBundle,
    new_knowledge_id,
)
from storage.repositories.sqlite_store import SQLiteRepositoryStore


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _event_actor(auth: TrustedAuthContext) -> str | None:
    if auth.actor_kind in {"worker_agent", "master_agent"} and "." in auth.actor_id:
        return auth.actor_id
    return None


def _row_to_evidence(row: sqlite3.Row) -> EvidenceRecord:
    return EvidenceRecord.model_validate(
        {
            "evidence_id": row["evidence_id"],
            "evidence_kind": row["evidence_kind"],
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "artifact_id": row["artifact_id"],
            "recorded_by": row["recorded_by"],
            "source_url": row["source_url"],
            "retrieved_at": row["retrieved_at"],
            "untrusted_source": bool(int(row["untrusted_source"])),
            "content_sha256": row["content_sha256"],
            "summary": row["summary"],
            "redacted_payload": json.loads(str(row["redacted_payload_json"])),
            "created_at": row["created_at"],
        }
    )


def _row_to_knowledge(row: sqlite3.Row) -> KnowledgeEntry:
    return KnowledgeEntry.model_validate(
        {
            "knowledge_id": row["knowledge_id"],
            "title": row["title"],
            "category": row["category"],
            "version": row["version"],
            "access_scope": row["access_scope"],
            "owner_agent_id": row["owner_agent_id"],
            "department": row["department"],
            "task_id": row["task_id"],
            "project_id": row["project_id"],
            "source_artifact_id": row["source_artifact_id"],
            "source_evidence_id": row["source_evidence_id"],
            "tags": json.loads(str(row["tags_json"])),
            "metadata": json.loads(str(row["metadata_json"])),
            "content_markdown": row["content_markdown"],
            "content_sha256": row["content_sha256"],
            "approval_status": row["approval_status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
    )


def _can_access_knowledge(entry: KnowledgeEntry, auth: TrustedAuthContext) -> bool:
    """Enforce scope-based access control strictly BEFORE ranking search results."""
    if auth.actor_kind in {"human_operator", "master_agent", "runtime_service", "trusted_verifier"}:
        return True
    if entry.access_scope == "private":
        return entry.owner_agent_id == auth.actor_id
    if entry.access_scope == "global":
        return entry.approval_status == "APPROVED" or entry.owner_agent_id == auth.actor_id
    if entry.owner_agent_id == auth.actor_id:
        return True
    return entry.task_id is not None and entry.task_id in set(auth.assigned_task_ids)


class EvidenceAndKnowledgeService:
    """Manages immutable QA/research Evidence Vault and permission-filtered Knowledge retrieval."""

    def __init__(
        self,
        *,
        store: SQLiteRepositoryStore,
        permission_manager: PermissionManager,
        journal: EventJournal,
        artifact_vault: ArtifactVaultService | None = None,
    ) -> None:
        """Bind service to SQLite store, Governance PermissionManager, Journal, and ArtifactVault."""
        self._store = store
        self._permissions = permission_manager
        self._journal = journal
        self._vault = artifact_vault

    def _authorize(
        self,
        *,
        auth_context: TrustedAuthContext,
        action: str,
        resource: str,
        timestamp: datetime,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        decision = self._permissions.evaluate_and_audit(
            PolicyRequest(
                auth_context=auth_context,
                action=action,
                resource=resource,
                task_id=task_id,
                run_id=run_id,
                parameters={},
                correlation_id=f"knw-{action}-{abs(hash(resource)) % 100000}",
                timestamp=timestamp,
            )
        )
        if decision.decision != "ALLOW":
            raise VaultError("PERMISSION_DENIED", decision.message)

    def _emit_event(
        self,
        *,
        event_type: EventType,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
        task_id: str | None,
        run_id: str | None,
        evidence_id: str | None = None,
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
            evidence_id=evidence_id,
            artifact_id=artifact_id,
            correlation_id=correlation_id,
            redacted_payload=sanitize_payload(payload),
        )
        self._journal.append(ev)

    def record_evidence(
        self,
        *,
        auth_context: TrustedAuthContext,
        evidence_kind: EvidenceKind,
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
    ) -> EvidenceRecord:
        """Persist an immutable secret-redacted EvidenceRecord for QA/research/traceability."""
        self._authorize(
            auth_context=auth_context,
            action="vault.write",
            resource=f"evidence:task:{task_id}",
            timestamp=timestamp,
            task_id=task_id,
            run_id=run_id,
        )
        record = EvidenceRecord(
            evidence_id=evidence_id or EvidenceRecord.model_fields["evidence_id"].default_factory(),  # type: ignore[misc]
            evidence_kind=evidence_kind,
            task_id=task_id,
            run_id=run_id,
            artifact_id=artifact_id,
            recorded_by=auth_context.actor_id,
            source_url=source_url,
            retrieved_at=retrieved_at,
            untrusted_source=untrusted_source,
            content_sha256=content_sha256,
            summary=summary,
            redacted_payload=payload or {},
            created_at=timestamp,
        )
        with self._store.transaction_scope() as conn:
            conn.execute(
                """
                INSERT INTO evidence_records (
                    evidence_id, evidence_kind, task_id, run_id, artifact_id,
                    recorded_by, source_url, retrieved_at, untrusted_source,
                    content_sha256, summary, redacted_payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    record.evidence_id,
                    record.evidence_kind,
                    record.task_id,
                    record.run_id,
                    record.artifact_id,
                    record.recorded_by,
                    record.source_url,
                    record.retrieved_at.isoformat() if record.retrieved_at else None,
                    1 if record.untrusted_source else 0,
                    record.content_sha256,
                    record.summary,
                    _dumps(record.redacted_payload),
                    record.created_at.isoformat(),
                ),
            )
        self._emit_event(
            event_type="evidence.recorded",
            auth_context=auth_context,
            timestamp=timestamp,
            task_id=task_id,
            run_id=run_id,
            evidence_id=record.evidence_id,
            artifact_id=artifact_id,
            correlation_id=f"evd-{record.evidence_id}",
            payload={
                "evidence_id": record.evidence_id,
                "evidence_kind": record.evidence_kind,
                "untrusted_source": record.untrusted_source,
                "source_url": record.source_url,
            },
        )
        return record

    def list_evidence(
        self,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
        task_id: str | None = None,
        run_id: str | None = None,
        artifact_id: str | None = None,
        evidence_kind: EvidenceKind | None = None,
    ) -> list[EvidenceRecord]:
        """List stored EvidenceRecords after enforcing permission and Task scope."""
        self._authorize(
            auth_context=auth_context,
            action="vault.read",
            resource=f"evidence:list:{task_id or 'all'}",
            timestamp=timestamp,
            task_id=task_id,
            run_id=run_id,
        )
        if auth_context.actor_kind == "worker_agent":
            assigned = set(auth_context.assigned_task_ids)
            if task_id is not None and task_id not in assigned:
                raise VaultError("ACCESS_DENIED", f"Worker cannot read evidence for unassigned task '{task_id}'.")

        clauses: list[str] = []
        params: list[object] = []
        for col, val in (
            ("task_id", task_id),
            ("run_id", run_id),
            ("artifact_id", artifact_id),
            ("evidence_kind", evidence_kind),
        ):
            if val is not None:
                clauses.append(f"{col} = ?")
                params.append(val)
        where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM evidence_records {where_sql}ORDER BY created_at ASC, evidence_id ASC;",
                tuple(params),
            ).fetchall()
        records = [_row_to_evidence(r) for r in rows]
        if auth_context.actor_kind == "worker_agent":
            assigned = set(auth_context.assigned_task_ids)
            records = [r for r in records if r.task_id in assigned or r.recorded_by == auth_context.actor_id]
        return records

    def get_qa_inspection_bundle(
        self,
        task_id: str,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
        run_id: str | None = None,
    ) -> QaInspectionBundle:
        """Assemble artifacts, immutable evidence, and tool execution traces for QA Inspection Station."""
        evidence = self.list_evidence(
            auth_context=auth_context,
            timestamp=timestamp,
            task_id=task_id,
            run_id=run_id,
        )
        artifacts: list[ArtifactRegistryEntry] = []
        if self._vault is not None:
            artifacts = self._vault.list_artifacts(
                auth_context=auth_context,
                timestamp=timestamp,
                task_id=task_id,
                include_trashed=False,
            )
            if run_id is not None:
                artifacts = [a for a in artifacts if a.run_id == run_id]
        tool_execs = self._store.tools.list_executions(task_id=task_id, run_id=run_id, limit=500)
        return QaInspectionBundle(
            task_id=task_id,
            run_id=run_id,
            evidence_records=evidence,
            artifacts=artifacts,
            tool_executions=tool_execs,
            has_verified_pass_artifact=any(a.verification_status == "VERIFIED_PASS" for a in artifacts),
        )

    def record_knowledge(
        self,
        *,
        auth_context: TrustedAuthContext,
        title: str,
        category: KnowledgeCategory,
        content_markdown: str,
        timestamp: datetime,
        version: str = "1.0.0",
        access_scope: ArtifactAccessScope = "task",
        department: str | None = None,
        task_id: str | None = None,
        project_id: str | None = None,
        source_artifact_id: str | None = None,
        source_evidence_id: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        approval_status: KnowledgeApprovalStatus = "APPROVED",
        knowledge_id: str | None = None,
    ) -> KnowledgeEntry:
        """Persist a secret-redacted KnowledgeEntry linked to optional Task, Artifact, or Evidence."""
        self._authorize(
            auth_context=auth_context,
            action="knowledge.write",
            resource=f"knowledge:scope:{access_scope}",
            timestamp=timestamp,
            task_id=task_id,
        )
        cleaned_md = redact_secrets(content_markdown).strip()
        content_sha = hashlib.sha256(cleaned_md.encode("utf-8")).hexdigest()
        entry = KnowledgeEntry(
            knowledge_id=knowledge_id or new_knowledge_id(),
            title=title,
            category=category,
            version=version,
            access_scope=access_scope,
            owner_agent_id=auth_context.actor_id,
            department=department,
            task_id=task_id,
            project_id=project_id,
            source_artifact_id=source_artifact_id,
            source_evidence_id=source_evidence_id,
            tags=tags or [],
            metadata=metadata or {},
            content_markdown=cleaned_md,
            content_sha256=content_sha,
            approval_status=approval_status,
            created_at=timestamp,
            updated_at=timestamp,
        )
        with self._store.transaction_scope() as conn:
            conn.execute(
                """
                INSERT INTO knowledge_entries (
                    knowledge_id, title, category, version, access_scope,
                    owner_agent_id, department, task_id, project_id,
                    source_artifact_id, source_evidence_id, tags_json, metadata_json,
                    content_markdown, content_sha256, approval_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    entry.knowledge_id,
                    entry.title,
                    entry.category,
                    entry.version,
                    entry.access_scope,
                    entry.owner_agent_id,
                    entry.department,
                    entry.task_id,
                    entry.project_id,
                    entry.source_artifact_id,
                    entry.source_evidence_id,
                    _dumps(entry.tags),
                    _dumps(entry.metadata),
                    entry.content_markdown,
                    entry.content_sha256,
                    entry.approval_status,
                    entry.created_at.isoformat(),
                    entry.updated_at.isoformat(),
                ),
            )
        self._emit_event(
            event_type="knowledge.recorded",
            auth_context=auth_context,
            timestamp=timestamp,
            task_id=task_id,
            run_id=None,
            artifact_id=source_artifact_id,
            evidence_id=source_evidence_id,
            correlation_id=f"knw-{entry.knowledge_id}",
            payload={
                "knowledge_id": entry.knowledge_id,
                "category": entry.category,
                "access_scope": entry.access_scope,
                "approval_status": entry.approval_status,
                "content_sha256": entry.content_sha256,
            },
        )
        return entry

    def search_knowledge(
        self,
        query: KnowledgeSearchQuery,
        *,
        auth_context: TrustedAuthContext,
        timestamp: datetime,
    ) -> KnowledgeSearchResult:
        """Retrieve Knowledge entries by applying permission filters BEFORE metadata/keyword scoring."""
        self._authorize(
            auth_context=auth_context,
            action="knowledge.read",
            resource="knowledge:search",
            timestamp=timestamp,
            task_id=query.task_id,
        )
        clauses: list[str] = []
        params: list[object] = []
        for col, val in (
            ("category", query.category),
            ("department", query.department),
            ("task_id", query.task_id),
            ("approval_status", query.approval_status),
        ):
            if val is not None:
                clauses.append(f"{col} = ?")
                params.append(val)
        where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM knowledge_entries {where_sql}ORDER BY updated_at DESC, knowledge_id ASC;",
                tuple(params),
            ).fetchall()

        authorized = [
            entry for row in rows if _can_access_knowledge((entry := _row_to_knowledge(row)), auth_context)
        ]

        wanted_tags = {t.strip().lower() for t in query.tags if t.strip()}
        if wanted_tags:
            authorized = [
                e for e in authorized if wanted_tags.issubset({t.lower() for t in e.tags})
            ]

        terms = [tok.lower() for tok in query.query_text.split() if tok.strip()]
        hits: list[KnowledgeSearchHit] = []
        for entry in authorized:
            if not terms:
                hits.append(KnowledgeSearchHit(entry=entry, score=1.0, matched_fields=["metadata"]))
                continue
            title_l = entry.title.lower()
            tags_l = " ".join(entry.tags).lower()
            meta_l = _dumps(entry.metadata).lower()
            body_l = entry.content_markdown.lower()

            score = 0.0
            matched: set[str] = set()
            all_terms_present = True
            for term in terms:
                term_hit = False
                if term in title_l:
                    score += 3.0
                    matched.add("title")
                    term_hit = True
                if term in tags_l:
                    score += 2.5
                    matched.add("tags")
                    term_hit = True
                if term in meta_l:
                    score += 1.5
                    matched.add("metadata")
                    term_hit = True
                if term in body_l:
                    score += 1.0
                    matched.add("content_markdown")
                    term_hit = True
                if not term_hit:
                    all_terms_present = False
            if all_terms_present and score > 0.0:
                hits.append(
                    KnowledgeSearchHit(
                        entry=entry,
                        score=score,
                        matched_fields=sorted(matched),
                    )
                )

        hits.sort(key=lambda h: (-h.score, h.entry.title))
        bounded = hits[: query.limit]
        return KnowledgeSearchResult(
            query_text=query.query_text,
            total_hits=len(hits),
            hits=bounded,
        )
