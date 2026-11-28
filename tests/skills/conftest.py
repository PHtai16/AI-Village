"""Shared fixtures and builders for Phase 10 Skill Factory tests (P10.03-P10.05)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from core.skills import (
    SkillCandidateRequest,
    SkillExecutionContract,
    SkillIOField,
    SkillLifecycleService,
    SkillRequirements,
    SkillTestCaseSpec,
)
from core.tools.registry import ToolRegistry
from storage.repositories import SQLiteRepositoryStore
from tests.departments.conftest import DepartmentTestHarness, make_department_harness, utc
from tools.registry_bootstrap import register_phase07_tools


@dataclass
class SkillFactoryHarness:
    """Bundled Phase 10 Skill Factory test environment backed by real SQLite and ToolRegistry."""

    dept: DepartmentTestHarness
    skills_root: Path
    tool_registry: ToolRegistry
    skills: SkillLifecycleService

    def reopen(self) -> tuple[SQLiteRepositoryStore, SkillLifecycleService]:
        """Close current store and open a fresh SkillLifecycleService on the same DB/disk root."""
        from core.governance import ApprovalService, PermissionManager, PolicyEvaluator

        db_path = self.dept.db_path
        self.dept.store.close()
        new_store = SQLiteRepositoryStore(db_path)
        pm = PermissionManager(
            evaluator=PolicyEvaluator(),
            journal=new_store.events,
            approval_service=ApprovalService(new_store.approvals, new_store.events),
        )
        svc = SkillLifecycleService(
            skills_root=self.skills_root,
            repo=new_store.skills,
            permission_manager=pm,
            journal=new_store.events,
            tool_lookup=self.tool_registry,
            improvement_repo=new_store.process,
            retrospective_repo=new_store.process,
        )
        return new_store, svc


@pytest.fixture
def skill_harness(tmp_path: Path) -> SkillFactoryHarness:
    """Create isolated Skills Factory harness with all 17 canonical Phase 07 tools registered."""
    dept = make_department_harness(tmp_path)
    skills_root = tmp_path / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    tool_reg = ToolRegistry(journal=dept.store.events)
    register_phase07_tools(tool_reg, registered_at=utc(8, 0, 0))
    svc = SkillLifecycleService(
        skills_root=skills_root,
        repo=dept.store.skills,
        permission_manager=dept.permissions,
        journal=dept.store.events,
        tool_lookup=tool_reg,
        improvement_repo=dept.store.process,
        retrospective_repo=dept.store.process,
    )
    return SkillFactoryHarness(
        dept=dept,
        skills_root=skills_root,
        tool_registry=tool_reg,
        skills=svc,
    )


def make_candidate_request(
    *,
    skill_id: str = "csv_bom_cleanup",
    version: str = "1.0.0",
    name: str = "CSV UTF-8 BOM Cleanup",
    maintainer: str = "process",
    tools: list[str] | None = None,
    dependencies: list[str] | None = None,
    risk_level: str = "low",
    risks: list[str] | None = None,
    modifies_sensitive_workflow: bool = False,
    scripts: dict[str, str] | None = None,
    references: dict[str, str] | None = None,
    source_improvement_id: str | None = None,
    submit_as_candidate: bool = True,
    minute_offset: int = 0,
) -> SkillCandidateRequest:
    """Build a valid ``SkillCandidateRequest`` adhering cleanly to ``SKILL_SPEC.md``."""
    return SkillCandidateRequest(
        skill_id=skill_id,
        name=name,
        version=version,
        description=f"Standardized repeatable procedure for {name}.",
        category="office_automation",
        author="process_discovery",
        maintainer=maintainer,  # type: ignore[arg-type]
        source_improvement_id=source_improvement_id,
        requirements=SkillRequirements(
            tools=tools if tools is not None else ["tools.files.read"],
            dependencies=dependencies if dependencies is not None else [],
            min_engine_tier="flash",
        ),
        contract=SkillExecutionContract(
            purpose_and_scope="Normalize UTF-8 BOM files deterministically inside workspace/{task_id}/.",
            triggers=["Input file ends with UTF-8 BOM decoding warning."],
            anti_triggers=["Single small ASCII note under 20 lines readable directly."],
            inputs=[
                SkillIOField(
                    name="input_relpath",
                    data_type="workspace_relative_path",
                    description="Input file inside task workspace",
                    required=True,
                )
            ],
            outputs=[
                SkillIOField(
                    name="normalized_summary",
                    data_type="markdown_report",
                    description="Normalized output verification summary",
                    required=True,
                )
            ],
            execution_steps=[
                "Verify file path stays strictly inside workspace/{task_id}/.",
                "Inspect byte header with allowed file tools and record verification hash.",
            ],
            security_and_permissions=[
                "Read-only inside workspace/{task_id}/ unless operator explicitly approves write.",
                "Never read .env or credentials outside task scope.",
            ],
            definition_of_done=[
                "Output verified cleanly with SHA-256 checksum recorded.",
            ],
            evidence_requirements=[
                "SHA-256 digest of output artifact plus redacted tool execution log.",
            ],
            error_handling=[
                "Stop and report exact parse failure without guessing missing content.",
            ],
        ),
        risk_level=risk_level,  # type: ignore[arg-type]
        risks=risks or ["Malformed BOM encoding may require fallback charset rejection."],
        modifies_sensitive_workflow=modifies_sensitive_workflow,
        test_cases=[
            SkillTestCaseSpec(
                case_id="case_happy_utf8_bom",
                case_kind="happy_path",
                description="Cleanly handles standard UTF-8 BOM CSV input inside workspace.",
                expected_outcome="Reads header cleanly in 1 tool pass.",
                input_payload={"encoding": "utf-8-sig", "rows": 12},
            ),
            SkillTestCaseSpec(
                case_id="case_fail_missing_file",
                case_kind="failure_case",
                description="Returns fail-closed FILE_NOT_FOUND when input is absent.",
                expected_outcome="Fails cleanly without fabricated data.",
                input_payload={"missing": True},
            ),
        ],
        scripts=scripts or {},
        references=references or {},
        submit_as_candidate=submit_as_candidate,
        timestamp=utc(10, 0, 0) + timedelta(minutes=minute_offset),
    )
