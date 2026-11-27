"""End-to-end Gate E acceptance test verifying knowledge/artifact reuse, real DOCX/XLSX API download, and trash safety."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
from pathlib import Path

from docx import Document as DocxDocument
from fastapi import Request
from fastapi.testclient import TestClient
import openpyxl

from api import create_gate_e_artifact_app
from core.contracts import Run, Task, new_run_id, new_task_id
from core.governance import (
    ApprovalService,
    PermissionManager,
    PolicyEvaluator,
    TrustedAuthContext,
    build_default_policy_rules,
)
from core.tools import ToolInvocationRequest, ToolRegistry, ToolRuntime
from storage.artifacts import ArtifactVaultService
from storage.db import apply_migrations, sqlite_connection
from storage.knowledge import EvidenceAndKnowledgeService, KnowledgeSearchQuery
from storage.repositories import SQLiteApprovalRepository, SQLiteRepositoryStore
from tools.office import build_office_tools


def _utc(minute: int = 0) -> datetime:
    return datetime(2026, 11, 27, 22, minute, 0, tzinfo=timezone.utc)


def test_gate_e_end_to_end_reuse_real_office_download_and_trash_protection(tmp_path: Path) -> None:
    """Verify all Gate E acceptance criteria without fake diffs or mocked downloads."""
    db_file = tmp_path / "gate_e.db"
    with sqlite_connection(db_file) as conn:
        apply_migrations(conn)

    with SQLiteRepositoryStore(db_file) as store:
        ts = _utc()
        t1, t2 = new_task_id(), new_task_id()
        r1, r2 = new_run_id(), new_run_id()
        for tid, title in ((t1, "Research Baseline Task"), (t2, "Deliverable Compilation Task")):
            store.tasks.create_task(
                Task(
                    task_id=tid,
                    title=title,
                    description="Gate E verification task",
                    requested_by="operator.alice",
                    created_at=ts,
                    updated_at=ts,
                )
            )
        store.runs.create_run(Run(run_id=r1, task_id=t1, runtime_id="custom.master_worker_v1"))
        store.runs.create_run(Run(run_id=r2, task_id=t2, runtime_id="custom.master_worker_v1"))

        reg = ToolRegistry(repository=store.tools, journal=store.events)
        for manifest, handler in build_office_tools():
            reg.register(manifest, handler, registered_at=ts)

        approval_svc = ApprovalService(SQLiteApprovalRepository(store), store.events)
        perm_mgr = PermissionManager(
            evaluator=PolicyEvaluator(
                rules=build_default_policy_rules(),
                registered_tools=reg.registered_tool_ids(),
            ),
            journal=store.events,
            approval_service=approval_svc,
        )
        tool_runtime = ToolRuntime(
            registry=reg,
            permission_manager=perm_mgr,
            journal=store.events,
            tool_repo=store.tools,
        )
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

        op = TrustedAuthContext(
            actor_id="operator.alice",
            actor_kind="human_operator",
            auth_source="operator_session",
            authenticated=True,
        )
        w1 = TrustedAuthContext(
            actor_id="it.research_worker",
            actor_kind="worker_agent",
            auth_source="application_boundary",
            authenticated=True,
            allowed_tools=sorted(reg.registered_tool_ids()),
            assigned_task_ids=[t1],
        )
        w2 = TrustedAuthContext(
            actor_id="service.office_worker",
            actor_kind="worker_agent",
            auth_source="application_boundary",
            authenticated=True,
            allowed_tools=sorted(reg.registered_tool_ids()),
            assigned_task_ids=[t2],
        )

        # 1. Task 1 records baseline spec artifact v1.0.0, QA evidence, and reusable global knowledge
        art_v1 = vault.register_artifact(
            auth_context=w1,
            task_id=t1,
            run_id=r1,
            owner_agent_id="it.research_worker",
            logical_key="baseline_guidelines",
            relative_path="output/guidelines.md",
            content_bytes=b"# Baseline Guidelines\nAlways validate SHA-256 checksums.",
            version="1.0.0",
            verification_status="VERIFIED_PASS",
            evidence_reference="evd_baseline0001",
            access_scope="global",
            timestamp=_utc(1),
        )
        art_v1_solo = vault.register_artifact(
            auth_context=w1,
            task_id=t1,
            run_id=r1,
            owner_agent_id="it.research_worker",
            logical_key="scratch_draft",
            relative_path="output/draft.txt",
            content_bytes=b"temporary scratch note",
            version="1.0.0",
            timestamp=_utc(2),
        )
        ek_svc.record_knowledge(
            auth_context=w1,
            title="SHA-256 Delivery Checklist",
            category="standard",
            access_scope="global",
            department="vault",
            source_artifact_id=art_v1.artifact_id,
            tags=["gate_e", "sha256"],
            content_markdown="Deliverables must include verified DOCX and XLSX checksums.",
            timestamp=_utc(3),
        )

        # 2. Task 2 finds prior knowledge and attaches exact v1.0.0 artifact reference
        knw_hits = ek_svc.search_knowledge(
            KnowledgeSearchQuery(query_text="sha256 checksums", tags=["gate_e"]),
            auth_context=w2,
            timestamp=_utc(4),
        )
        assert knw_hits.total_hits == 1
        reused_aid = knw_hits.hits[0].entry.source_artifact_id
        assert reused_aid == art_v1.artifact_id
        vault.attach_task_reference(
            reused_aid,
            referencing_task_id=t2,
            referencing_run_id=r2,
            auth_context=w2,
            timestamp=_utc(5),
        )
        history = vault.get_version_history("baseline_guidelines", auth_context=w2, timestamp=_utc(6))
        assert history.versions[0].version == "1.0.0"

        # 3. Task 2 runs Phase 07 tools to generate real DOCX and XLSX on workspace disk
        ws2 = tmp_path / "workspace" / t2
        ws2.mkdir(parents=True)
        docx_res = tool_runtime.invoke(
            ToolInvocationRequest(
                auth_context=w2,
                tool_id="tools.office.write_docx",
                parameters={
                    "path": "deliverables/summary.docx",
                    "title": "Gate E Acceptance Summary",
                    "paragraphs": ["Reused guidelines v1.0.0 cleanly."],
                },
                workspace_root=str(ws2),
                task_id=t2,
                run_id=r2,
                timestamp=_utc(7),
            )
        )
        xlsx_res = tool_runtime.invoke(
            ToolInvocationRequest(
                auth_context=w2,
                tool_id="tools.office.write_excel",
                parameters={
                    "path": "deliverables/metrics.xlsx",
                    "sheets": {"Metrics": [["Check", "Status"], ["SHA256", "OK"]]},
                },
                workspace_root=str(ws2),
                task_id=t2,
                run_id=r2,
                timestamp=_utc(8),
            )
        )
        assert docx_res.status == "SUCCESS"
        assert xlsx_res.status == "SUCCESS"

        v_docx = vault.register_artifact(
            auth_context=w2,
            task_id=t2,
            run_id=r2,
            owner_agent_id="service.office_worker",
            logical_key="gate_e_docx",
            relative_path="output/summary.docx",
            source_path=ws2 / "deliverables" / "summary.docx",
            version="1.0.0",
            timestamp=_utc(9),
        )
        v_xlsx = vault.register_artifact(
            auth_context=w2,
            task_id=t2,
            run_id=r2,
            owner_agent_id="service.office_worker",
            logical_key="gate_e_xlsx",
            relative_path="output/metrics.xlsx",
            source_path=ws2 / "deliverables" / "metrics.xlsx",
            version="1.0.0",
            timestamp=_utc(10),
        )

        # 4. Download DOCX and XLSX via real FastAPI TestClient endpoints and parse real bytes
        def _resolve_header_auth(request: Request) -> TrustedAuthContext:
            actor = request.headers.get("X-Actor-ID", "")
            if actor == w2.actor_id:
                return w2
            if actor == w1.actor_id:
                return w1
            if actor == op.actor_id:
                return op
            return TrustedAuthContext(
                actor_id="anonymous",
                actor_kind="worker_agent",
                auth_source="application_boundary",
                authenticated=False,
            )

        app = create_gate_e_artifact_app(vault, auth_resolver=_resolve_header_auth)
        client = TestClient(app)

        # Missing auth -> 401; unassigned w1 reading private t2 docx -> 403
        assert client.get(f"/api/v1/artifacts/{v_docx.artifact_id}/download").status_code == 401
        assert client.get(
            f"/api/v1/artifacts/{v_docx.artifact_id}/download",
            headers={"X-Actor-ID": w1.actor_id},
        ).status_code == 403

        dl_docx = client.get(
            f"/api/v1/artifacts/{v_docx.artifact_id}/download",
            headers={"X-Actor-ID": w2.actor_id},
        )
        assert dl_docx.status_code == 200
        assert dl_docx.headers["X-Artifact-SHA256"] == hashlib.sha256(dl_docx.content).hexdigest()
        parsed_doc = DocxDocument(io.BytesIO(dl_docx.content))
        assert any("Gate E Acceptance Summary" in p.text for p in parsed_doc.paragraphs)

        dl_xlsx = client.get(
            f"/api/v1/artifacts/{v_xlsx.artifact_id}/download",
            headers={"X-Actor-ID": w2.actor_id},
        )
        assert dl_xlsx.status_code == 200
        assert dl_xlsx.headers["X-Artifact-SHA256"] == hashlib.sha256(dl_xlsx.content).hexdigest()
        wb = openpyxl.load_workbook(io.BytesIO(dl_xlsx.content), read_only=True)
        assert wb.sheetnames == ["Metrics"]
        wb.close()

        # 5. Bulk trash from Task 1 spares Task 2's shared artifact, while solo trash + restore recovers cleanly
        bulk_rep = vault.safe_bulk_trash(
            [art_v1.artifact_id, art_v1_solo.artifact_id],
            auth_context=op,
            reason="Archive Task 1 outputs",
            timestamp=_utc(11),
            acting_task_id=t1,
        )
        assert bulk_rep.trashed_ids == [art_v1_solo.artifact_id]
        assert bulk_rep.skipped_protected[0]["artifact_id"] == art_v1.artifact_id

        # Shared v1 still downloads over API for Task 2, trashed solo returns 410, then restores to 200
        assert (
            client.get(
                f"/api/v1/artifacts/{art_v1.artifact_id}/download",
                headers={"X-Actor-ID": w2.actor_id},
            ).status_code
            == 200
        )
        assert (
            client.get(
                f"/api/v1/artifacts/{art_v1_solo.artifact_id}/download",
                headers={"X-Actor-ID": op.actor_id},
            ).status_code
            == 410
        )
        vault.restore_artifact(art_v1_solo.artifact_id, auth_context=op, timestamp=_utc(12))
        assert (
            client.get(
                f"/api/v1/artifacts/{art_v1_solo.artifact_id}/download",
                headers={"X-Actor-ID": op.actor_id},
            ).status_code
            == 200
        )
