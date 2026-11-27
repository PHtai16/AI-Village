"""Fail-closed tests for P08.05 Backup/Restore, Retention, Trash/Restore, and Safe Bulk Actions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.contracts import Run, Task, new_run_id, new_task_id
from core.governance import (
    ApprovalPolicyContext,
    ApprovalService,
    PermissionManager,
    PolicyEvaluator,
    TrustedAuthContext,
    build_default_policy_rules,
)
from storage.artifacts import ArtifactVaultService, VaultApprovalRequiredError, VaultError
from storage.db import apply_migrations, sqlite_connection
from storage.repositories import SQLiteApprovalRepository, SQLiteRepositoryStore


def _utc(minute: int = 0) -> datetime:
    return datetime(2026, 11, 27, 21, minute, 0, tzinfo=timezone.utc)


def _setup(tmp_path: Path) -> tuple[SQLiteRepositoryStore, ArtifactVaultService, ApprovalService, str, str, str, str]:
    db_file = tmp_path / "bkp_trash.db"
    with sqlite_connection(db_file) as conn:
        apply_migrations(conn)
    store = SQLiteRepositoryStore(db_file)
    ts = _utc()
    t1, t2 = new_task_id(), new_task_id()
    r1, r2 = new_run_id(), new_run_id()
    for tid, title in ((t1, "Owner Task"), (t2, "Consumer Task")):
        store.tasks.create_task(
            Task(
                task_id=tid,
                title=title,
                description="Verify P08.05",
                requested_by="operator.alice",
                created_at=ts,
                updated_at=ts,
            )
        )
    store.runs.create_run(Run(run_id=r1, task_id=t1, runtime_id="custom.master_worker_v1"))
    store.runs.create_run(Run(run_id=r2, task_id=t2, runtime_id="custom.master_worker_v1"))

    approval_svc = ApprovalService(SQLiteApprovalRepository(store), store.events)
    perm_mgr = PermissionManager(
        evaluator=PolicyEvaluator(rules=build_default_policy_rules()),
        journal=store.events,
        approval_service=approval_svc,
    )
    vault = ArtifactVaultService(
        store=store,
        vault_root=tmp_path / "vault",
        permission_manager=perm_mgr,
        journal=store.events,
    )
    return store, vault, approval_svc, t1, r1, t2, r2


def _approve_action(
    approval_svc: ApprovalService,
    requester: TrustedAuthContext,
    approver: TrustedAuthContext,
    pending_exc: VaultApprovalRequiredError,
    ts: datetime,
) -> str:
    apr = approval_svc.create_approval_request(
        requester=requester,
        action=pending_exc.approval_action,
        target_resource=pending_exc.resource,
        task_id=None,
        run_id=None,
        request_fingerprint=pending_exc.request_fingerprint,
        policy_snapshot=ApprovalPolicyContext(
            matched_rule_ids=["gov.require_approval_sensitive_admin_ops"],
            rule_versions={"gov.require_approval_sensitive_admin_ops": "1.0.0"},
            reason_code="APPROVAL_RULE_MATCHED",
        ),
        risk_summary=f"Approve {pending_exc.approval_action}",
        requested_at=ts,
        expires_at=ts + timedelta(minutes=30),
    )
    approval_svc.decide_approval(
        apr.approval_id,
        decider=approver,
        approved=True,
        reason="Operator verified",
        timestamp=ts,
    )
    return apr.approval_id


def test_trash_restore_shared_task_protection_and_approved_purge(tmp_path: Path) -> None:
    """Protect shared blobs/references during bulk trash & purge; allow restore and approved unshared purge."""
    store, vault, approval_svc, t1, r1, t2, _ = _setup(tmp_path)
    try:
        op_req = TrustedAuthContext(
            actor_id="operator.requester",
            actor_kind="human_operator",
            auth_source="operator_session",
            authenticated=True,
        )
        op_dec = TrustedAuthContext(
            actor_id="operator.approver",
            actor_kind="human_operator",
            auth_source="operator_session",
            authenticated=True,
        )
        worker = TrustedAuthContext(
            actor_id="it.worker",
            actor_kind="worker_agent",
            auth_source="application_boundary",
            authenticated=True,
            assigned_task_ids=[t1, t2],
        )

        # Shared bytes between a_shared (referenced by t2) and a_solo (owned only by t1)
        shared = vault.register_artifact(
            auth_context=op_req,
            task_id=t1,
            run_id=r1,
            owner_agent_id="it.worker",
            logical_key="shared_doc",
            relative_path="output/shared.txt",
            content_bytes=b"shared vault payload",
            timestamp=_utc(1),
        )
        vault.attach_task_reference(
            shared.artifact_id,
            referencing_task_id=t2,
            auth_context=op_req,
            timestamp=_utc(2),
        )
        solo = vault.register_artifact(
            auth_context=op_req,
            task_id=t1,
            run_id=r1,
            owner_agent_id="it.worker",
            logical_key="solo_doc",
            relative_path="output/solo.txt",
            content_bytes=b"disposable payload",
            timestamp=_utc(3),
        )

        # Worker is strictly denied trash/purge operations
        with pytest.raises(VaultError) as w_deny:
            vault.trash_artifact(solo.artifact_id, auth_context=worker, reason="cleanup", timestamp=_utc(4))
        assert w_deny.value.error_code == "PERMISSION_DENIED"

        # Safe bulk trash spares `shared` because t2 references it, while trashing `solo`
        bulk = vault.safe_bulk_trash(
            [shared.artifact_id, solo.artifact_id],
            auth_context=op_req,
            reason="Task 1 cleanup",
            timestamp=_utc(5),
            acting_task_id=t1,
        )
        assert bulk.trashed_ids == [solo.artifact_id]
        assert len(bulk.skipped_protected) == 1
        assert bulk.skipped_protected[0]["artifact_id"] == shared.artifact_id
        assert bulk.skipped_protected[0]["referencing_task_ids"] == [t2]

        # Shared artifact bytes remain fully readable for Task 2
        _, shared_bytes = vault.read_verified_bytes(shared.artifact_id, auth_context=worker, timestamp=_utc(6))
        assert shared_bytes == b"shared vault payload"

        # Restore `solo` back to ACTIVE, then re-trash and purge behind single-use Approval
        restored = vault.restore_artifact(solo.artifact_id, auth_context=op_req, timestamp=_utc(7))
        assert restored.lifecycle_status == "ACTIVE"
        vault.trash_artifact(solo.artifact_id, auth_context=op_req, reason="Re-trash", timestamp=_utc(8))

        with pytest.raises(VaultApprovalRequiredError) as need_apr:
            vault.purge_artifact(solo.artifact_id, auth_context=op_req, timestamp=_utc(9))
        apr_id = _approve_action(approval_svc, op_req, op_dec, need_apr.value, _utc(10))

        purged = vault.purge_artifact(solo.artifact_id, auth_context=op_req, approval_id=apr_id, timestamp=_utc(11))
        assert purged.lifecycle_status == "PURGED"
        assert purged.blob_deleted is True

        # Shared artifact cannot be purged even if manually trashed while Task 2 still holds reference
        vault.trash_artifact(shared.artifact_id, auth_context=op_req, reason="Test guard", timestamp=_utc(12))
        with pytest.raises(VaultError) as ref_prot:
            vault.purge_artifact(shared.artifact_id, auth_context=op_req, approval_id=apr_id, timestamp=_utc(13))
        assert ref_prot.value.error_code == "SHARED_REFERENCE_PROTECTED"
    finally:
        store.close()


def test_backup_tamper_detection_approved_restore_and_retention(tmp_path: Path) -> None:
    """Verify atomic SQLite+blob backup, fail-closed tamper check, approved restore, and retention."""
    store, vault, approval_svc, t1, r1, _, _ = _setup(tmp_path)
    try:
        op_req = TrustedAuthContext(
            actor_id="operator.requester",
            actor_kind="human_operator",
            auth_source="operator_session",
            authenticated=True,
        )
        op_dec = TrustedAuthContext(
            actor_id="operator.approver",
            actor_kind="human_operator",
            auth_source="operator_session",
            authenticated=True,
        )

        art = vault.register_artifact(
            auth_context=op_req,
            task_id=t1,
            run_id=r1,
            owner_agent_id="vault.keeper",
            logical_key="important_doc",
            relative_path="output/important.txt",
            content_bytes=b"critical backup bytes",
            timestamp=_utc(1),
        )
        backup = vault.create_backup(auth_context=op_req, timestamp=_utc(2), retention_days=7)
        assert backup.blob_count == 1

        # Test approved clean restore into target verification dir
        with pytest.raises(VaultApprovalRequiredError) as need_apr:
            vault.restore_backup(
                backup.backup_id,
                auth_context=op_req,
                timestamp=_utc(3),
                target_db_path=tmp_path / "restored" / "restored.db",
                target_vault_root=tmp_path / "restored_vault",
            )
        apr_id = _approve_action(approval_svc, op_req, op_dec, need_apr.value, _utc(4))

        report = vault.restore_backup(
            backup.backup_id,
            auth_context=op_req,
            approval_id=apr_id,
            timestamp=_utc(5),
            target_db_path=tmp_path / "restored" / "restored.db",
            target_vault_root=tmp_path / "restored_vault",
        )
        assert report.integrity_ok is True
        assert report.foreign_key_ok is True
        assert report.restored_blob_count == 1
        assert (tmp_path / "restored_vault" / art.storage_relpath).read_bytes() == b"critical backup bytes"

        # Tampering backup blob rejects restore fail-closed
        tampered_blob = tmp_path / "vault" / backup.backup_relpath / art.storage_relpath
        tampered_blob.write_bytes(b"corrupted")
        with pytest.raises(VaultError) as corrupt_exc:
            vault.restore_backup(
                backup.backup_id,
                auth_context=op_req,
                approval_id=apr_id,
                timestamp=_utc(6),
                target_db_path=tmp_path / "restored2" / "restored.db",
                target_vault_root=tmp_path / "restored_vault2",
            )
        assert corrupt_exc.value.error_code == "BACKUP_CORRUPTED"

        # Retention prunes expired backup after retention_days
        pruned = vault.prune_expired_backups(auth_context=op_req, now=_utc(2) + timedelta(days=8))
        assert pruned == [backup.backup_id]
        assert not (tmp_path / "vault" / backup.backup_relpath).exists()
    finally:
        store.close()
