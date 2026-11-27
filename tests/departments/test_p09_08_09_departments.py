"""Unit, Persistence, Governance, Replay, and End-to-End tests for Phase 09 P09.08 (QA) and P09.09 (Vault)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from core.departments import (
    QaDepartmentService,
    QaEvidenceRequiredError,
    QaIndependenceError,
    QaVerificationRequest,
    VaultDepartmentError,
    VaultDepartmentService,
    VaultReuseRequest,
    VaultSubmissionRequest,
)
from core.events.replay import validate_journal_entries
from storage.artifacts import VaultError
from tests.departments.conftest import (
    DepartmentTestHarness,
    create_task_and_run,
    register_worker_agent,
    utc,
    worker_ctx,
)


def test_p09_08_qa_independence_evidence_gates_tamper_detection_and_replay(
    dept_harness: DepartmentTestHarness,
) -> None:
    """Verify P09.08 forbids self-PASS, blocks missing evidence, detects disk tamper, and passes Event replay."""
    t0 = utc(9, 0, 0)
    task_id, run_id = create_task_and_run(
        dept_harness,
        title="Verify release artifact",
        description="Run independent QA over produced workspace deliverable.",
        timestamp=t0,
        assigned_agent_ids=["it.developer"],
    )
    register_worker_agent(
        dept_harness,
        agent_id="it.developer",
        department="it",
        skills=["file_writing"],
        tools_allowed=["tools.files.write", "tools.files.read"],
        timestamp=t0,
    )
    it_worker = worker_ctx(
        "it.developer",
        allowed_tools=["tools.files.write", "tools.files.read"],
        assigned_task_ids=[task_id],
    )

    qa = QaDepartmentService(
        permission_manager=dept_harness.permissions,
        journal=dept_harness.store.events,
        evidence_vault=dept_harness.evidence_vault,
        artifact_vault=dept_harness.artifact_vault,
        tool_invoker=dept_harness.tool_runtime,
    )
    vault_dept = VaultDepartmentService(
        permission_manager=dept_harness.permissions,
        journal=dept_harness.store.events,
        artifact_vault=dept_harness.artifact_vault,
        knowledge_vault=dept_harness.evidence_vault,
    )

    rel = f"workspace/{task_id}/module_output.py"
    disk_file = dept_harness.tmp_path / rel
    disk_file.parent.mkdir(parents=True, exist_ok=True)
    raw = b"def health() -> str:\n    return 'ok'\n"
    disk_file.write_bytes(raw)

    sub = vault_dept.submit_document(
        VaultSubmissionRequest(
            task_id=task_id,
            run_id=run_id,
            owner_agent_id="it.developer",
            logical_key="module_output_py",
            relative_path=rel,
            content_bytes=raw,
            department="it",
            tags=["python", "release"],
            metadata={"suite": "unit"},
            timestamp=t0 + timedelta(seconds=1),
        ),
        auth_context=it_worker,
    )
    art_id = sub.artifact.artifact_id
    assert sub.artifact.verification_status == "UNVERIFIED"

    # 1. Producer / worker agent cannot self-certify or issue QA PASS.
    with pytest.raises(QaIndependenceError) as worker_err:
        qa.verify_deliverables(
            QaVerificationRequest(
                task_id=task_id,
                run_id=run_id,
                producer_agent_id="it.developer",
                artifact_id=art_id,
                sandbox_passed=True,
                summary="Worker trying to approve output",
                timestamp=t0 + timedelta(seconds=2),
            ),
            auth_context=it_worker,
        )
    assert worker_err.value.error_code == "INDEPENDENT_VERIFIER_REQUIRED"

    with pytest.raises(QaIndependenceError) as same_id_err:
        qa.verify_deliverables(
            QaVerificationRequest(
                task_id=task_id,
                run_id=run_id,
                producer_agent_id="qa.verifier",
                artifact_id=art_id,
                sandbox_passed=True,
                summary="Same actor verifying own artifact",
                timestamp=t0 + timedelta(seconds=3),
            ),
            auth_context=dept_harness.qa_verifier,
        )
    assert same_id_err.value.error_code == "SELF_VERIFICATION_FORBIDDEN"

    # 2. Missing proof -> refuses PASS fail-closed when require_pass_verdict=True, or emits UNVERIFIED.
    with pytest.raises(QaEvidenceRequiredError) as no_proof_err:
        qa.verify_deliverables(
            QaVerificationRequest(
                task_id=task_id,
                run_id=run_id,
                producer_agent_id="it.developer",
                artifact_id=art_id,
                require_pass_verdict=True,
                summary="Claim pass without test proof",
                timestamp=t0 + timedelta(seconds=4),
            ),
            auth_context=dept_harness.qa_verifier,
        )
    assert no_proof_err.value.error_code == "QA_EVIDENCE_REQUIRED"

    unver = qa.verify_deliverables(
        QaVerificationRequest(
            task_id=task_id,
            run_id=run_id,
            producer_agent_id="it.developer",
            artifact_id=art_id,
            require_pass_verdict=False,
            summary="No test run observed yet",
            timestamp=t0 + timedelta(seconds=5),
        ),
        auth_context=dept_harness.qa_verifier,
    )
    assert unver.status == "UNVERIFIED"
    assert unver.verification_status == "UNVERIFIED"
    assert any("Missing concrete verification proof" in d for d in unver.defects)

    # 3. Real sandbox failure -> emits FAIL / VERIFIED_FAIL cleanly.
    failed = qa.verify_deliverables(
        QaVerificationRequest(
            task_id=task_id,
            run_id=run_id,
            producer_agent_id="it.developer",
            artifact_id=art_id,
            sandbox_passed=False,
            summary="Sandbox pytest suite failed",
            timestamp=t0 + timedelta(seconds=6),
        ),
        auth_context=dept_harness.qa_verifier,
    )
    assert failed.status == "FAIL"
    assert failed.verification_status == "VERIFIED_FAIL"
    assert dept_harness.artifact_vault.get_artifact(
        art_id, auth_context=dept_harness.qa_verifier, timestamp=t0 + timedelta(seconds=6)
    ).verification_status == "VERIFIED_FAIL"

    # 4. Valid independent proof -> VERIFIED_PASS with immutable EvidenceRecord + replay PASS.
    passed = qa.verify_deliverables(
        QaVerificationRequest(
            task_id=task_id,
            run_id=run_id,
            producer_agent_id="it.developer",
            artifact_id=art_id,
            sandbox_passed=True,
            verified_checks=["pytest_all_passed", "sha256_verified"],
            require_pass_verdict=True,
            summary="All sandbox tests passed and blob SHA-256 verified.",
            timestamp=t0 + timedelta(seconds=7),
        ),
        auth_context=dept_harness.qa_verifier,
    )
    assert passed.status == "PASS"
    assert passed.verification_status == "VERIFIED_PASS"
    assert passed.integrity_matched is True
    assert passed.evidence_id is not None and passed.evidence_id.startswith("evd_")

    bundle = qa.inspect_bundle(task_id, auth_context=dept_harness.qa_verifier, timestamp=t0 + timedelta(seconds=8))
    assert bundle.has_verified_pass_artifact is True

    # Validate qa.verification_recorded event passes replay structural QA checks.
    task_entries = dept_harness.store.events.read_entries(task_id=task_id)
    replay_issues = validate_journal_entries(
        task_entries,
        expected_task_id=task_id,
        require_contiguous_sequences=False,
    )
    assert replay_issues == []

    # 5. Tampered SHA expectation on workspace file -> FAIL & VERIFIED_FAIL.
    tamper_fail = qa.verify_deliverables(
        QaVerificationRequest(
            task_id=task_id,
            run_id=run_id,
            producer_agent_id="it.developer",
            target_relpath=rel,
            workspace_root=str(dept_harness.tmp_path),
            expected_sha256="0" * 64,
            sandbox_passed=True,
            summary="Detect unexpected disk content digest",
            timestamp=t0 + timedelta(seconds=9),
        ),
        auth_context=dept_harness.alice,
    )
    assert tamper_fail.status == "FAIL"
    assert tamper_fail.integrity_matched is False
    assert any("mismatch" in d.lower() for d in tamper_fail.defects)


def test_p09_09_vault_version_lineage_knowledge_index_and_safe_cross_task_reuse(
    dept_harness: DepartmentTestHarness,
) -> None:
    """Verify P09.09 submission, SemVer lineage, diff preview, knowledge indexing, and cross-task reuse."""
    t0 = utc(10, 0, 0)
    task_old, run_old = create_task_and_run(
        dept_harness,
        title="Task 1 Spec Authoring",
        description="Produce v1 and v2 API specification artifacts.",
        timestamp=t0,
        assigned_agent_ids=["vault.archivist"],
    )
    task_new, run_new = create_task_and_run(
        dept_harness,
        title="Task 2 Consumer",
        description="Reuse verified v2 artifact safely.",
        timestamp=t0 + timedelta(seconds=10),
        assigned_agent_ids=["vault.archivist"],
    )
    archivist = worker_ctx("vault.archivist", assigned_task_ids=[task_old, task_new])
    vault_dept = VaultDepartmentService(
        permission_manager=dept_harness.permissions,
        journal=dept_harness.store.events,
        artifact_vault=dept_harness.artifact_vault,
        knowledge_vault=dept_harness.evidence_vault,
    )

    # Reject claiming VERIFIED_PASS at submission without evidence_reference.
    with pytest.raises(VaultDepartmentError) as no_ev_err:
        vault_dept.submit_document(
            VaultSubmissionRequest(
                task_id=task_old,
                run_id=run_old,
                owner_agent_id="vault.archivist",
                logical_key="api_spec_md",
                relative_path="outputs/api_spec.md",
                content_bytes=b"# Spec v1\n",
                verification_status="VERIFIED_PASS",
                timestamp=t0,
            ),
            auth_context=archivist,
        )
    assert no_ev_err.value.error_code == "QA_EVIDENCE_REQUIRED"

    # Submit v1.0.0 and v1.1.0 with Knowledge summary indexing.
    v1_out = vault_dept.submit_document(
        VaultSubmissionRequest(
            task_id=task_old,
            run_id=run_old,
            owner_agent_id="vault.archivist",
            logical_key="api_spec_md",
            relative_path="outputs/api_spec.md",
            content_bytes=b"# API Spec\nEndpoint: /v1/health\n",
            department="vault",
            access_scope="project",
            tags=["spec", "openapi"],
            metadata={"owner_team": "platform"},
            timestamp=t0 + timedelta(seconds=1),
        ),
        auth_context=archivist,
    )
    evd = dept_harness.evidence_vault.record_evidence(
        auth_context=dept_harness.qa_verifier,
        evidence_kind="qa_log",
        task_id=task_old,
        run_id=run_old,
        summary="Independent QA signed v1.1.0 specification.",
        timestamp=t0 + timedelta(seconds=2),
    )
    v2_out = vault_dept.submit_document(
        VaultSubmissionRequest(
            task_id=task_old,
            run_id=run_old,
            owner_agent_id="vault.archivist",
            logical_key="api_spec_md",
            relative_path="outputs/api_spec.md",
            content_bytes=b"# API Spec\nEndpoint: /v1/health\nEndpoint: /v1/tasks\n",
            verification_status="VERIFIED_PASS",
            evidence_reference=evd.evidence_id,
            source_evidence_id=evd.evidence_id,
            department="vault",
            access_scope="project",
            tags=["spec", "openapi"],
            metadata={"owner_team": "platform"},
            knowledge_title="Platform API Spec v1.1.0",
            knowledge_category="architecture",
            knowledge_summary_markdown="# Summary\nDefines `/v1/health` and `/v1/tasks`.",
            timestamp=t0 + timedelta(seconds=3),
        ),
        auth_context=archivist,
    )
    assert v1_out.artifact.version == "1.0.0"
    assert v2_out.artifact.version == "1.1.0"
    assert v2_out.artifact.previous_artifact_id == v1_out.artifact.artifact_id
    assert v2_out.knowledge_entry is not None
    assert v2_out.knowledge_entry.source_artifact_id == v2_out.artifact.artifact_id

    history, preview = vault_dept.get_version_history_with_preview(
        "api_spec_md", auth_context=archivist, timestamp=t0 + timedelta(seconds=4)
    )
    assert history.version_count == 2
    assert "/v1/tasks" in preview.preview_data["text"]

    cmp_res = vault_dept.compare_versions(
        v1_out.artifact.artifact_id,
        v2_out.artifact.artifact_id,
        auth_context=archivist,
        timestamp=t0 + timedelta(seconds=5),
    )
    assert cmp_res.identical_bytes is False
    assert cmp_res.llm_generated is False
    assert any("+/v1/tasks" in line or "+Endpoint: /v1/tasks" in line for line in cmp_res.diff_lines)

    # Cross-task reuse into Task 2 attaches reference protecting shared artifact from accidental deletion.
    reused = vault_dept.reuse_artifact_in_task(
        VaultReuseRequest(
            artifact_id=v2_out.artifact.artifact_id,
            target_task_id=task_new,
            target_run_id=run_new,
            timestamp=t0 + timedelta(seconds=11),
        ),
        auth_context=archivist,
    )
    assert b"/v1/tasks" in reused.content_bytes
    assert task_new in reused.artifact.referencing_task_ids

    # Trying safe bulk trash on shared artifact from Task 1 rejects deleting shared reference!
    bulk = dept_harness.artifact_vault.safe_bulk_trash(
        [v2_out.artifact.artifact_id],
        auth_context=dept_harness.alice,
        reason="Cleanup old task artifacts",
        timestamp=t0 + timedelta(seconds=12),
    )
    assert v2_out.artifact.artifact_id in [s["artifact_id"] for s in bulk.skipped_protected]

    # Unauthorized unassigned worker cannot read or reuse private/unassigned task artifact.
    rogue = worker_ctx("marketing.rogue", assigned_task_ids=[])
    with pytest.raises(VaultError, match="not authorized"):
        vault_dept.reuse_artifact_in_task(
            VaultReuseRequest(
                artifact_id=v1_out.artifact.artifact_id,
                target_task_id=task_new,
                timestamp=t0 + timedelta(seconds=13),
            ),
            auth_context=rogue,
        )
