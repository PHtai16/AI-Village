"""Fail-closed tests for P08.03 Evidence Vault and P08.04 Permission-Filtered Knowledge Search."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3

import pytest

from core.contracts import Run, Task, new_run_id, new_task_id
from core.governance import PermissionManager, PolicyEvaluator, TrustedAuthContext, build_default_policy_rules
from storage.artifacts import ArtifactVaultService, VaultError
from storage.db import apply_migrations, sqlite_connection
from storage.knowledge import EvidenceAndKnowledgeService, KnowledgeSearchQuery
from storage.repositories import SQLiteRepositoryStore

FAKE_TOKEN = "ghp_" + "FakeKnowledgeLeakToken1234567890"


def _utc(minute: int = 0) -> datetime:
    return datetime(2026, 11, 27, 20, minute, 0, tzinfo=timezone.utc)


def _setup(tmp_path: Path) -> tuple[SQLiteRepositoryStore, ArtifactVaultService, EvidenceAndKnowledgeService, str, str, str, str]:
    db_file = tmp_path / "evd_knw.db"
    with sqlite_connection(db_file) as conn:
        apply_migrations(conn)
    store = SQLiteRepositoryStore(db_file)
    ts = _utc()
    t1, t2 = new_task_id(), new_task_id()
    r1, r2 = new_run_id(), new_run_id()
    for tid, title in ((t1, "Task One"), (t2, "Task Two")):
        store.tasks.create_task(
            Task(
                task_id=tid,
                title=title,
                description="Knowledge boundary test",
                requested_by="operator.alice",
                created_at=ts,
                updated_at=ts,
            )
        )
    store.runs.create_run(Run(run_id=r1, task_id=t1, runtime_id="custom.master_worker_v1"))
    store.runs.create_run(Run(run_id=r2, task_id=t2, runtime_id="custom.master_worker_v1"))

    perm_mgr = PermissionManager(evaluator=PolicyEvaluator(rules=build_default_policy_rules()), journal=store.events)
    vault = ArtifactVaultService(
        store=store,
        vault_root=tmp_path / "vault",
        permission_manager=perm_mgr,
        journal=store.events,
    )
    ek_svc = EvidenceAndKnowledgeService(
        store=store,
        permission_manager=perm_mgr,
        journal=store.events,
        artifact_vault=vault,
    )
    return store, vault, ek_svc, t1, r1, t2, r2


def test_evidence_immutability_redaction_and_qa_inspection_bundle(tmp_path: Path) -> None:
    """Evidence records are immutable in SQLite, redact secrets, and feed QA Inspection Bundles."""
    store, vault, ek_svc, t1, r1, _, _ = _setup(tmp_path)
    try:
        qa_worker = TrustedAuthContext(
            actor_id="qa.inspector",
            actor_kind="worker_agent",
            auth_source="application_boundary",
            authenticated=True,
            assigned_task_ids=[t1],
        )
        art = vault.register_artifact(
            auth_context=qa_worker,
            task_id=t1,
            run_id=r1,
            owner_agent_id="qa.inspector",
            logical_key="qa_target",
            relative_path="output/check.txt",
            content_bytes=b"ok",
            timestamp=_utc(1),
        )
        evd = ek_svc.record_evidence(
            auth_context=qa_worker,
            evidence_kind="qa_log",
            task_id=t1,
            run_id=r1,
            artifact_id=art.artifact_id,
            source_url="https://docs.python.org/3/",
            retrieved_at=_utc(1),
            untrusted_source=True,
            content_sha256="a" * 64,
            summary=f"Verified output cleanly despite token={FAKE_TOKEN}",
            payload={"secret": FAKE_TOKEN, "check": "sha256_ok"},
            timestamp=_utc(2),
        )
        assert FAKE_TOKEN not in evd.summary
        assert evd.redacted_payload["secret"] == "[REDACTED_KEY_VALUE]"
        assert evd.untrusted_source is True

        bundle = ek_svc.get_qa_inspection_bundle(t1, auth_context=qa_worker, timestamp=_utc(3), run_id=r1)
        assert len(bundle.evidence_records) == 1
        assert len(bundle.artifacts) == 1
        assert bundle.has_verified_pass_artifact is False

        # Immutable triggers reject UPDATE / DELETE on evidence_records
        with store.open_connection() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE evidence_records SET summary = 'forged';")
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM evidence_records;")
    finally:
        store.close()


def test_knowledge_search_filters_permissions_before_ranking(tmp_path: Path) -> None:
    """Metadata keyword search never leaks private or unassigned task knowledge."""
    store, _, ek_svc, t1, _, t2, _ = _setup(tmp_path)
    try:
        w1 = TrustedAuthContext(
            actor_id="it.worker_one",
            actor_kind="worker_agent",
            auth_source="application_boundary",
            authenticated=True,
            assigned_task_ids=[t1],
        )
        w2 = TrustedAuthContext(
            actor_id="marketing.worker_two",
            actor_kind="worker_agent",
            auth_source="application_boundary",
            authenticated=True,
            assigned_task_ids=[t2],
        )

        k_global = ek_svc.record_knowledge(
            auth_context=w1,
            title="Shared Architecture Standard",
            category="standard",
            access_scope="global",
            department="it",
            tags=["security", "sandbox"],
            content_markdown=f"Always run untrusted code in --network=none.\nKey={FAKE_TOKEN}",
            timestamp=_utc(1),
        )
        k_t1 = ek_svc.record_knowledge(
            auth_context=w1,
            title="Task One Confidential Plan",
            category="research_finding",
            access_scope="task",
            task_id=t1,
            tags=["sandbox"],
            content_markdown="Sandbox benchmark measurements for Task One.",
            timestamp=_utc(2),
        )
        ek_svc.record_knowledge(
            auth_context=w1,
            title="Worker One Private Note",
            category="playbook",
            access_scope="private",
            task_id=t1,
            tags=["sandbox"],
            content_markdown="Private scratch note about sandbox.",
            timestamp=_utc(3),
        )

        # w2 assigned ONLY to t2 searches for "sandbox": sees ONLY k_global, NEVER k_t1 or w1's private note
        res_w2 = ek_svc.search_knowledge(
            KnowledgeSearchQuery(query_text="sandbox", tags=["sandbox"]),
            auth_context=w2,
            timestamp=_utc(4),
        )
        assert res_w2.search_mode == "metadata_keyword"
        assert res_w2.semantic_search_enabled is False
        assert [h.entry.knowledge_id for h in res_w2.hits] == [k_global.knowledge_id]
        assert FAKE_TOKEN not in res_w2.hits[0].entry.content_markdown

        # w1 sees all 3 owned/assigned entries
        res_w1 = ek_svc.search_knowledge(
            KnowledgeSearchQuery(query_text="sandbox"),
            auth_context=w1,
            timestamp=_utc(5),
        )
        assert len(res_w1.hits) == 3

        # Evidence list for unassigned task also blocked
        with pytest.raises(VaultError) as ev_denied:
            ek_svc.list_evidence(auth_context=w2, timestamp=_utc(6), task_id=t1)
        assert ev_denied.value.error_code == "ACCESS_DENIED"
        _ = k_t1
    finally:
        store.close()
