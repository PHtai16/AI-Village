"""Tests for Phase 10 / P10.03: Skill candidate packages, schemas, AST purity, and fail-closed guards."""

from __future__ import annotations

import ast
from datetime import timedelta
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.agents.hr import HrDepartmentService
from core.agents.hr_models import HrSkillApprovalError, SkillGrantRequest
from core.departments import (
    CompileTaskRetrospectiveRequest,
    PatternAnalysisRequest,
    ProcessDepartmentService,
)
from core.skills import (
    SkillCandidateRequest,
    SkillLifecycleService,
    SkillStateError,
    SkillTestCaseSpec,
    SkillValidationError,
    parse_and_validate_skill_markdown,
)
from core.tools.models import ToolExecutionRecord
from tests.departments.conftest import REPO_ROOT, create_task_and_run, register_worker_agent, request_and_approve, utc
from tests.skills.conftest import SkillFactoryHarness, make_candidate_request


def test_p10_03_ast_purity_and_json_schema_contract() -> None:
    """Ensure core/skills modules stay layer-pure and JSON manifest schema matches SKILL_SPEC.md."""
    forbidden_prefixes = (
        "sqlite3",
        "storage",
        "sqlalchemy",
        "subprocess",
        "tools",
        "api",
        "ui",
        "httpx",
        "urllib",
        "requests",
        "crewai",
    )
    skills_core = REPO_ROOT / "core" / "skills"
    py_files = sorted(skills_core.rglob("*.py"))
    assert py_files
    for py_file in py_files:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(forbidden_prefixes), (
                        f"Forbidden import '{alias.name}' in {py_file}"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(forbidden_prefixes), (
                    f"Forbidden from-import '{node.module}' in {py_file}"
                )

    schema_path = REPO_ROOT / "skills" / "schemas" / "skill_manifest.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    for key in [
        "spec_version",
        "id",
        "name",
        "version",
        "description",
        "category",
        "author",
        "maintainer",
        "status",
        "requirements",
    ]:
        assert key in schema["properties"]
    assert set(schema["properties"]["status"]["enum"]) == {
        "DRAFT",
        "CANDIDATE",
        "UNDER_REVIEW",
        "APPROVED",
        "REJECTED",
        "DEPRECATED",
        "DISABLED",
    }


def test_p10_03_candidate_creation_improvement_link_draft_transition_and_persistence(
    skill_harness: SkillFactoryHarness,
) -> None:
    """Create linked candidate package in candidates/, verify SKILL.md round-trip and restart recovery."""
    h = skill_harness.dept
    t0 = utc(9, 0, 0)
    proc = ProcessDepartmentService(
        permission_manager=h.permissions,
        journal=h.store.events,
        retrospective_repo=h.store.process,
    )
    for idx in range(2):
        tid, rid = create_task_and_run(h, title=f"BOM task {idx}", description="Read BOM", timestamp=t0)
        h.store.tools.record_execution(
            ToolExecutionRecord(
                execution_id=f"tex_bom_{idx}",
                tool_id="tools.files.read",
                task_id=tid,
                run_id=rid,
                actor_id="it.coder",
                action="files.read",
                status="FAILED",
                error_code="BOM_DECODE_FAIL",
                error_message="Unexpected BOM",
                duration_ms=20,
                executed_at=t0 + timedelta(seconds=idx + 1),
            )
        )
        proc.compile_task_retrospective(
            CompileTaskRetrospectiveRequest(task_id=tid, timestamp=t0 + timedelta(seconds=10 + idx)),
            auth_context=h.master,
        )

    pat = proc.analyze_patterns_and_suggest_improvements(
        PatternAnalysisRequest(min_occurrences=2, timestamp=t0 + timedelta(minutes=1)),
        auth_context=h.master,
    )
    imp = pat.created_improvements[0]

    # Create DRAFT first, then submit as CANDIDATE cleanly.
    draft_req = make_candidate_request(
        skill_id="csv_bom_handler",
        version="0.1.0",
        source_improvement_id=imp.improvement_id,
        submit_as_candidate=False,
        scripts={"normalize.py": "def normalize(text: str) -> str:\n    return text.lstrip('\\ufeff')\n"},
        references={"bom_notes.md": "# UTF-8 BOM reference\nCheck leading 0xEFBBBF.\n"},
        minute_offset=2,
    )
    draft_pkg = skill_harness.skills.create_candidate(draft_req, auth_context=h.master)
    assert draft_pkg.lifecycle_status == "DRAFT"
    assert draft_pkg.storage_zone == "candidates"
    assert draft_pkg.approved_for_production is False
    assert draft_pkg.risk_profile.has_executable_scripts is True
    assert draft_pkg.risk_profile.requires_human_approval is True
    assert skill_harness.skills.get_approved_skill("csv_bom_handler") is None

    skill_md_disk = (skill_harness.skills_root / draft_pkg.package_relpath / "SKILL.md").read_text(
        encoding="utf-8"
    )
    fm, sections = parse_and_validate_skill_markdown(skill_md_disk)
    assert fm.id == "csv_bom_handler"
    assert len(sections) == 10
    assert (skill_harness.skills_root / draft_pkg.package_relpath / "tests" / "fixtures.json").is_file()
    assert (skill_harness.skills_root / draft_pkg.package_relpath / "scripts" / "normalize.py").is_file()

    cand_pkg = skill_harness.skills.submit_draft_as_candidate(
        "csv_bom_handler",
        "0.1.0",
        auth_context=h.master,
        timestamp=t0 + timedelta(minutes=5),
    )
    assert cand_pkg.lifecycle_status == "CANDIDATE"

    # Restart store and verify full state survives restart without losing file hashes.
    reopened_store, reopened_skills = skill_harness.reopen()
    try:
        recovered = reopened_skills.get_package("csv_bom_handler", "0.1.0")
        assert recovered is not None
        assert recovered.lifecycle_status == "CANDIDATE"
        assert recovered.package_sha256 == draft_pkg.package_sha256
        assert recovered.source_improvement_id == imp.improvement_id
    finally:
        reopened_store.close()


def test_p10_03_fail_closed_validation_yaml_claim_and_hr_isolation(
    skill_harness: SkillFactoryHarness,
) -> None:
    """Reject self-claimed APPROVED YAML, duplicate versions, unknown tools, secrets, and missing tests."""
    h = skill_harness.dept
    t0 = utc(11, 0, 0)

    # 1. Forged YAML status='APPROVED' placed in candidates/ NEVER activates skill or unlocks HR grant!
    forged_dir = skill_harness.skills_root / "candidates" / "forged_skill" / "1.0.0"
    forged_dir.mkdir(parents=True, exist_ok=True)
    req_forged = make_candidate_request(skill_id="forged_skill", version="1.0.0")
    from core.skills import render_skill_markdown
    from core.skills.models import SkillFrontmatter, SkillRiskProfile

    forged_fm = SkillFrontmatter(
        id="forged_skill",
        name="Forged Self-Approved Skill",
        version="1.0.0",
        description="Attempts self-certification via YAML frontmatter.",
        category="office_automation",
        author="worker",
        maintainer="process",
        status="APPROVED",
        requirements=req_forged.requirements,
    )
    forged_rp = SkillRiskProfile(risk_level="low", risks=["None stated"])
    (forged_dir / "SKILL.md").write_text(
        render_skill_markdown(forged_fm, req_forged.contract, risk_profile=forged_rp),
        encoding="utf-8",
    )
    registered = skill_harness.skills.register_external_candidate_dir(
        relpath="candidates/forged_skill/1.0.0",
        risk_level="low",
        risks=["None stated"],
        test_cases=req_forged.test_cases,
        contract=req_forged.contract,
        auth_context=h.master,
        timestamp=t0,
    )
    assert registered.lifecycle_status == "CANDIDATE"
    assert registered.approved_for_production is False
    assert skill_harness.skills.list_approved_skills() == []

    register_worker_agent(
        h,
        agent_id="it.helper",
        department="it",
        skills=["forged_skill"],
        timestamp=t0 + timedelta(seconds=2),
    )
    hr = HrDepartmentService(
        registry=h.agent_registry,
        departments_root=REPO_ROOT / "agents" / "departments",
        skill_catalog=skill_harness.skills,
    )
    rec = h.agent_registry.get_agent("it.helper")
    assert rec is not None
    apr_id = request_and_approve(
        h,
        action="agent.update",
        resource="agent:it.helper",
        parameters={"target_agent_id": "it.helper", "operation": "grant_skills", "skill_ids": ["forged_skill"]},
        requester=h.alice,
        decider=h.bob,
        risk_summary="Grant forged_skill test",
        timestamp=t0 + timedelta(seconds=3),
    )
    with pytest.raises(HrSkillApprovalError):
        hr.grant_skills(
            SkillGrantRequest(
                agent_id="it.helper",
                skill_ids=["forged_skill"],
                expected_record_version=rec.record_version,
                reason="Cannot grant unapproved YAML-claimed skill",
                auth_context=h.alice,
                approval_id=apr_id,
                timestamp=t0 + timedelta(seconds=4),
            )
        )

    # 2. Duplicate version blocked fail-closed.
    with pytest.raises(SkillStateError, match="already exists"):
        skill_harness.skills.create_candidate(req_forged, auth_context=h.master)

    # 3. Missing failure_case test rejected.
    bad_tests = SkillCandidateRequest(
        **{
            **make_candidate_request(skill_id="only_happy", version="1.0.0").model_dump(),
            "test_cases": [
                SkillTestCaseSpec(
                    case_id="only_h1",
                    case_kind="happy_path",
                    description="Happy case one",
                    expected_outcome="Passes cleanly",
                ),
                SkillTestCaseSpec(
                    case_id="only_h2",
                    case_kind="happy_path",
                    description="Happy case two",
                    expected_outcome="Passes cleanly",
                ),
            ],
        }
    )
    with pytest.raises(SkillValidationError, match="failure_case"):
        skill_harness.skills.create_candidate(bad_tests, auth_context=h.master)

    # 4. Unknown tool ID blocked.
    with pytest.raises(SkillValidationError, match="unregistered tool"):
        skill_harness.skills.create_candidate(
            make_candidate_request(skill_id="bad_tool_skill", tools=["tools.files.nonexistent"]),
            auth_context=h.master,
        )

    # 5. Secret exfiltration or .env access inside script rejected.
    with pytest.raises(SkillValidationError, match="SECRET|forbidden"):
        skill_harness.skills.create_candidate(
            make_candidate_request(
                skill_id="exfil_skill",
                scripts={"steal.py": "open('.env').read()\n"},
            ),
            auth_context=h.master,
        )

    # 6. Path traversal script path rejected.
    with pytest.raises(SkillValidationError, match="traversal|relative|escape"):
        skill_harness.skills.create_candidate(
            make_candidate_request(
                skill_id="escape_skill",
                scripts={"../../evil.py": "print('nope')\n"},
            ),
            auth_context=h.master,
        )
