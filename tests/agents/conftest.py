"""Shared pytest fixtures and Governance approval helpers for tests/agents/."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.agents import (
    AgentRecruitmentService,
    AgentRegistryService,
    ApprovedSkillDescriptor,
    HrDepartmentService,
    StaticApprovedSkillCatalog,
)
from core.governance import (
    ApprovalPolicyContext,
    ApprovalService,
    PermissionDeniedError,
    PermissionManager,
    PolicyEvaluator,
)
from core.governance.models import TrustedAuthContext
from storage.repositories import SQLiteRepositoryStore
from tests.agents.test_p05_02_agent_registry import (
    _build_registry,
    _init_db,
    _master_ctx,
    _operator_ctx,
    _utc,
    _worker_ctx,
    _worker_manifest_dict,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS_ROOT = REPO_ROOT / "agents"
DEPARTMENTS_ROOT = AGENTS_ROOT / "departments"


@dataclass
class HrTestHarness:
    """Bundled real SQLite Registry, Recruitment, Governance, and HR test stack."""

    db_path: Path
    store: SQLiteRepositoryStore
    registry: AgentRegistryService
    recruitment: AgentRecruitmentService
    approvals: ApprovalService
    evaluator: PolicyEvaluator
    permission_manager: PermissionManager
    hr: HrDepartmentService
    alice: TrustedAuthContext
    bob: TrustedAuthContext
    master_auth: TrustedAuthContext


def approve_protected_operation(
    harness: HrTestHarness,
    *,
    action_fn: Callable[[str | None], Any],
    requester: TrustedAuthContext,
    decider: TrustedAuthContext,
    risk_summary: str,
    timestamp: datetime,
) -> tuple[str, Any]:
    """Trigger APPROVAL_REQUIRED, record explicit human approval, and execute single-use call."""
    with pytest.raises(PermissionDeniedError) as err:
        action_fn(None)
    assert err.value.decision.decision == "APPROVAL_REQUIRED"
    req = harness.approvals.create_approval_request(
        requester=requester,
        action=err.value.decision.action,
        target_resource=err.value.decision.resource,
        task_id=None,
        run_id=None,
        request_fingerprint=err.value.decision.request_fingerprint,
        policy_snapshot=ApprovalPolicyContext(
            matched_rule_ids=err.value.decision.matched_rule_ids,
            rule_versions=harness.evaluator.policy_snapshot(err.value.decision.matched_rule_ids),
            reason_code=err.value.decision.reason_code,
        ),
        risk_summary=risk_summary,
        requested_at=timestamp,
    )
    harness.approvals.decide_approval(
        req.approval_id,
        decider=decider,
        approved=True,
        reason=f"Approved: {risk_summary}",
        timestamp=timestamp + timedelta(seconds=1),
    )
    return req.approval_id, action_fn(req.approval_id)


def make_hr_harness(
    tmp_path: Path,
    *,
    db_name: str = "hr_test.db",
    approved_skills: tuple[ApprovedSkillDescriptor, ...] = (
        ApprovedSkillDescriptor(
            skill_id="repo_audit",
            version="1.0.0",
            required_capabilities=["workspace_artifact_authoring"],
            summary="Approved repository structure verification skill.",
        ),
        ApprovedSkillDescriptor(
            skill_id="security_audit_pack",
            version="1.0.0",
            required_capabilities=["security_audit"],
            summary="Approved QA security audit workflow.",
        ),
    ),
) -> HrTestHarness:
    """Create initialized SQLite store, Registry, Recruitment, Governance, and HR service."""
    db_path = tmp_path / db_name
    _init_db(db_path)
    store = SQLiteRepositoryStore(db_path)
    registry, approvals, evaluator = _build_registry(store)
    pm = PermissionManager(evaluator=evaluator, journal=store.events, approval_service=approvals)
    recruitment = AgentRecruitmentService(
        recruitment_repo=store.recruitment,
        agent_registry=registry,
        permission_manager=pm,
    )
    catalog = StaticApprovedSkillCatalog(approved_skills)
    hr = HrDepartmentService(
        registry=registry,
        recruitment_service=recruitment,
        departments_root=DEPARTMENTS_ROOT,
        skill_catalog=catalog,
    )
    return HrTestHarness(
        db_path=db_path,
        store=store,
        registry=registry,
        recruitment=recruitment,
        approvals=approvals,
        evaluator=evaluator,
        permission_manager=pm,
        hr=hr,
        alice=_operator_ctx("operator.alice"),
        bob=_operator_ctx("operator.bob"),
        master_auth=_master_ctx(),
    )


@pytest.fixture
def hr_harness(tmp_path: Path) -> HrTestHarness:
    """Provide a real SQLite-backed HR harness closed cleanly after test completion."""
    harness = make_hr_harness(tmp_path)
    try:
        yield harness
    finally:
        harness.store.close()


__all__ = [
    "AGENTS_ROOT",
    "DEPARTMENTS_ROOT",
    "HrTestHarness",
    "REPO_ROOT",
    "_master_ctx",
    "_operator_ctx",
    "_utc",
    "_worker_ctx",
    "_worker_manifest_dict",
    "approve_protected_operation",
    "hr_harness",
    "make_hr_harness",
]
