"""Fail-closed tests for P08.01 Artifact Registry versioning and P08.02 Vault preview/diff/integrity."""

from __future__ import annotations

from datetime import datetime, timezone
import io
from pathlib import Path

from docx import Document as DocxDocument
import openpyxl
from PIL import Image
import pymupdf
import pytest

from core.contracts import Run, Task, new_run_id, new_task_id
from core.governance import ApprovalService, PermissionManager, PolicyEvaluator, TrustedAuthContext, build_default_policy_rules
from storage.artifacts import ArtifactVaultService, VaultError
from storage.db import apply_migrations, sqlite_connection
from storage.repositories import SQLiteApprovalRepository, SQLiteRepositoryStore

FAKE_KEY = "sk-" + "VaultSecretTestMaterial1234567890"


def _utc(minute: int = 0) -> datetime:
    return datetime(2026, 11, 27, 19, minute, 0, tzinfo=timezone.utc)


def _setup_vault(tmp_path: Path) -> tuple[SQLiteRepositoryStore, ArtifactVaultService, str, str]:
    db_file = tmp_path / "vault.db"
    with sqlite_connection(db_file) as conn:
        apply_migrations(conn)
    store = SQLiteRepositoryStore(db_file)
    ts = _utc()
    task_id = new_task_id()
    run_id = new_run_id()
    store.tasks.create_task(
        Task(
            task_id=task_id,
            title="Vault Versioning Suite",
            description="Verify P08.01 and P08.02",
            requested_by="operator.alice",
            created_at=ts,
            updated_at=ts,
        )
    )
    store.runs.create_run(Run(run_id=run_id, task_id=task_id, runtime_id="custom.master_worker_v1"))

    approval_svc = ApprovalService(SQLiteApprovalRepository(store), store.events)
    perm_mgr = PermissionManager(
        evaluator=PolicyEvaluator(rules=build_default_policy_rules()),
        journal=store.events,
        approval_service=approval_svc,
    )
    vault = ArtifactVaultService(
        store=store,
        vault_root=tmp_path / "vault_storage",
        permission_manager=perm_mgr,
        journal=store.events,
    )
    return store, vault, task_id, run_id


def test_version_history_blob_deduplication_and_tamper_detection(tmp_path: Path) -> None:
    """Verify lineage across versions, SHA-256 deduplication, private/task scope, and fail-closed tamper guard."""
    store, vault, task_id, run_id = _setup_vault(tmp_path)
    try:
        owner = TrustedAuthContext(
            actor_id="it.author",
            actor_kind="worker_agent",
            auth_source="application_boundary",
            authenticated=True,
            assigned_task_ids=[task_id],
        )
        other_worker = TrustedAuthContext(
            actor_id="qa.other",
            actor_kind="worker_agent",
            auth_source="application_boundary",
            authenticated=True,
            assigned_task_ids=["task_unassigned0001"],
        )

        # Register v1.0.0 and v1.1.0 with identical bytes -> deduplicates single blob file
        v1 = vault.register_artifact(
            auth_context=owner,
            task_id=task_id,
            run_id=run_id,
            owner_agent_id="it.author",
            logical_key="spec_notes",
            relative_path="output/notes.md",
            content_bytes=f"# Spec\nline 1\ntoken={FAKE_KEY}".encode("utf-8"),
            version="1.0.0",
            access_scope="task",
            timestamp=_utc(1),
        )
        v2 = vault.register_artifact(
            auth_context=owner,
            task_id=task_id,
            run_id=run_id,
            owner_agent_id="it.author",
            logical_key="spec_notes",
            relative_path="output/notes.md",
            content_bytes=f"# Spec\nline 1\ntoken={FAKE_KEY}".encode("utf-8"),
            version="1.1.0",
            access_scope="task",
            timestamp=_utc(2),
        )
        v3 = vault.register_artifact(
            auth_context=owner,
            task_id=task_id,
            run_id=run_id,
            owner_agent_id="it.author",
            logical_key="spec_notes",
            relative_path="output/notes.md",
            content_bytes=b"# Spec\nline 1\nline 2 added\n",
            access_scope="task",
            timestamp=_utc(3),
        )
        assert v2.previous_artifact_id == v1.artifact_id
        assert v3.previous_artifact_id == v2.artifact_id
        assert v3.version == "1.2.0"
        assert v1.blob_sha256 == v2.blob_sha256

        # Only 2 physical blobs exist on disk for 3 artifact entries (v1 & v2 deduplicated)
        blobs = [p for p in (tmp_path / "vault_storage" / "blobs").rglob("*") if p.is_file()]
        assert len(blobs) == 2

        history = vault.get_version_history("spec_notes", auth_context=owner, timestamp=_utc(4))
        assert history.version_count == 3
        assert history.latest_artifact_id == v3.artifact_id
        assert [v.version for v in history.versions] == ["1.0.0", "1.1.0", "1.2.0"]

        # Unassigned worker is denied access
        with pytest.raises(VaultError) as denied:
            vault.get_artifact(v3.artifact_id, auth_context=other_worker, timestamp=_utc(5))
        assert denied.value.error_code == "ACCESS_DENIED"

        # Tampering physical blob bytes on disk must fail SHA-256 verification fail-closed
        blob_v3 = tmp_path / "vault_storage" / v3.storage_relpath
        blob_v3.write_bytes(b"TAMPERED")
        with pytest.raises(VaultError) as corrupt:
            vault.read_verified_bytes(v3.artifact_id, auth_context=owner, timestamp=_utc(6))
        assert corrupt.value.error_code == "INTEGRITY_MISMATCH"
    finally:
        store.close()


def test_format_previews_and_deterministic_comparisons_no_llm(tmp_path: Path) -> None:
    """Verify TEXT/DOCX/XLSX/PDF/IMAGE/BINARY previews and deterministic comparisons without LLM."""
    store, vault, task_id, run_id = _setup_vault(tmp_path)
    try:
        op = TrustedAuthContext(
            actor_id="operator.alice",
            actor_kind="human_operator",
            auth_source="operator_session",
            authenticated=True,
        )

        # Build two DOCX versions
        def _docx_bytes(lines: list[str]) -> bytes:
            doc = DocxDocument()
            for line in lines:
                doc.add_paragraph(line)
            buf = io.BytesIO()
            doc.save(buf)
            return buf.getvalue()

        d1 = vault.register_artifact(
            auth_context=op,
            task_id=task_id,
            run_id=run_id,
            owner_agent_id="service.writer",
            logical_key="brief_doc",
            relative_path="output/brief.docx",
            content_bytes=_docx_bytes(["Intro", f"Secret={FAKE_KEY}"]),
            version="1.0.0",
            timestamp=_utc(1),
        )
        d2 = vault.register_artifact(
            auth_context=op,
            task_id=task_id,
            run_id=run_id,
            owner_agent_id="service.writer",
            logical_key="brief_doc",
            relative_path="output/brief.docx",
            content_bytes=_docx_bytes(["Intro", "Updated body", "Conclusion"]),
            version="2.0.0",
            timestamp=_utc(2),
        )

        p_docx = vault.preview_artifact(d1.artifact_id, auth_context=op, timestamp=_utc(3))
        assert p_docx.preview_kind == "DOCX"
        assert p_docx.preview_data["paragraph_count"] == 2
        assert FAKE_KEY not in p_docx.preview_data["text_excerpt"]

        cmp_docx = vault.compare_artifacts(d1.artifact_id, d2.artifact_id, auth_context=op, timestamp=_utc(4))
        assert cmp_docx.llm_generated is False
        assert cmp_docx.comparison_method == "deterministic_structural"
        assert cmp_docx.identical_bytes is False
        assert cmp_docx.summary_metrics["right_paragraph_count"] == 3
        assert any("+Updated body" in line for line in cmp_docx.diff_lines)

        # Build two XLSX versions
        def _xlsx_bytes(sheets: dict[str, list[list[object]]]) -> bytes:
            wb = openpyxl.Workbook()
            first = wb.active
            assert first is not None
            first_used = False
            for name, rows in sheets.items():
                ws = first if not first_used else wb.create_sheet(title=name)
                ws.title = name
                first_used = True
                for r in rows:
                    ws.append(r)
            buf = io.BytesIO()
            wb.save(buf)
            wb.close()
            return buf.getvalue()

        x1 = vault.register_artifact(
            auth_context=op,
            task_id=task_id,
            run_id=run_id,
            owner_agent_id="resources.analyst",
            logical_key="budget_xlsx",
            relative_path="output/budget.xlsx",
            content_bytes=_xlsx_bytes({"Q1": [["Item", "Cost"], ["A", 10]]}),
            version="1.0.0",
            timestamp=_utc(5),
        )
        x2 = vault.register_artifact(
            auth_context=op,
            task_id=task_id,
            run_id=run_id,
            owner_agent_id="resources.analyst",
            logical_key="budget_xlsx",
            relative_path="output/budget.xlsx",
            content_bytes=_xlsx_bytes({"Q1": [["Item", "Cost"], ["A", 10]], "Q2": [["Item", "Cost"]]}),
            version="1.1.0",
            timestamp=_utc(6),
        )
        p_xlsx = vault.preview_artifact(x1.artifact_id, auth_context=op, timestamp=_utc(7))
        assert p_xlsx.preview_kind == "XLSX"
        assert p_xlsx.preview_data["sheet_names"] == ["Q1"]

        cmp_xlsx = vault.compare_artifacts(x1.artifact_id, x2.artifact_id, auth_context=op, timestamp=_utc(8))
        assert cmp_xlsx.comparison_method == "deterministic_structural"
        assert cmp_xlsx.summary_metrics["added_sheets"] == ["Q2"]

        # PDF, PNG, and Binary metadata-only previews
        pdf_doc = pymupdf.open()
        pdf_doc.new_page().insert_text((72, 72), "Vault PDF Excerpt")
        pdf_bytes = pdf_doc.tobytes()
        pdf_doc.close()
        a_pdf = vault.register_artifact(
            auth_context=op,
            task_id=task_id,
            run_id=run_id,
            owner_agent_id="qa.reviewer",
            logical_key="report_pdf",
            relative_path="output/report.pdf",
            content_bytes=pdf_bytes,
            timestamp=_utc(9),
        )
        assert vault.preview_artifact(a_pdf.artifact_id, auth_context=op, timestamp=_utc(10)).preview_kind == "PDF"

        img_buf = io.BytesIO()
        Image.new("RGB", (32, 16), color="blue").save(img_buf, format="PNG")
        a_img = vault.register_artifact(
            auth_context=op,
            task_id=task_id,
            run_id=run_id,
            owner_agent_id="qa.reviewer",
            logical_key="shot_png",
            relative_path="output/shot.png",
            content_bytes=img_buf.getvalue(),
            timestamp=_utc(11),
        )
        p_img = vault.preview_artifact(a_img.artifact_id, auth_context=op, timestamp=_utc(12))
        assert p_img.preview_kind == "IMAGE"
        assert p_img.preview_data["width"] == 32 and p_img.preview_data["height"] == 16

        a_bin = vault.register_artifact(
            auth_context=op,
            task_id=task_id,
            run_id=run_id,
            owner_agent_id="it.builder",
            logical_key="bin_blob",
            relative_path="output/archive.bin",
            content_bytes=b"\x00\x01\x02\x03",
            content_type="application/octet-stream",
            timestamp=_utc(13),
        )
        p_bin = vault.preview_artifact(a_bin.artifact_id, auth_context=op, timestamp=_utc(14))
        assert p_bin.preview_kind == "BINARY_METADATA_ONLY"
    finally:
        store.close()
