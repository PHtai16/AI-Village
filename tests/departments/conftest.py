"""Shared SQLite, ToolRuntime, Governance, and Vault fixtures for tests/departments/."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from core.agents import AgentRegistryService
from core.governance import (
    ApprovalPolicyContext,
    ApprovalService,
    PermissionDeniedError,
    PermissionManager,
    PolicyEvaluator,
)
from core.governance.models import TrustedAuthContext
from core.tools.registry import ToolRegistry
from core.tools.runtime import ToolRuntime
from storage.artifacts import ArtifactVaultService
from storage.db import apply_migrations, get_latest_migration_version, sqlite_connection
from storage.knowledge import EvidenceAndKnowledgeService
from storage.repositories import PersistentWorkflowService, SQLiteRepositoryStore
from tools.code import CommandRunResult
from tools.registry_bootstrap import register_phase07_tools
from tools.research.research_tools import HttpFetchResponse

REPO_ROOT = Path(__file__).resolve().parents[2]


def utc(hour: int = 10, minute: int = 0, second: int = 0) -> datetime:
    """Return deterministic UTC datetime."""
    return datetime(2026, 11, 27, hour, minute, second, tzinfo=timezone.utc)


def init_db(db_path: Path) -> None:
    """Apply all SQLite schema migrations up to latest version."""
    with sqlite_connection(db_path) as conn:
        res = apply_migrations(conn)
        assert res.current_version == get_latest_migration_version()


def operator_ctx(actor_id: str = "operator.alice") -> TrustedAuthContext:
    """Return authenticated human_operator context."""
    return TrustedAuthContext(
        actor_id=actor_id,
        actor_kind="human_operator",
        auth_source="operator_session",
        authenticated=True,
        allowed_actions=["*"],
        allowed_tools=["*"],
    )


def master_ctx(actor_id: str = "core.master") -> TrustedAuthContext:
    """Return authenticated master_agent context."""
    return TrustedAuthContext(
        actor_id=actor_id,
        actor_kind="master_agent",
        auth_source="application_boundary",
        authenticated=True,
    )


def worker_ctx(
    actor_id: str,
    *,
    allowed_tools: list[str] | None = None,
    assigned_task_ids: list[str] | None = None,
) -> TrustedAuthContext:
    """Return authenticated worker_agent context scoped to tools and assigned tasks."""
    return TrustedAuthContext(
        actor_id=actor_id,
        actor_kind="worker_agent",
        auth_source="application_boundary",
        authenticated=True,
        allowed_tools=allowed_tools or [],
        assigned_task_ids=assigned_task_ids or [],
    )


def verifier_ctx(actor_id: str = "qa.verifier") -> TrustedAuthContext:
    """Return authenticated trusted_verifier context."""
    return TrustedAuthContext(
        actor_id=actor_id,
        actor_kind="trusted_verifier",
        auth_source="application_boundary",
        authenticated=True,
    )


@dataclass
class DepartmentTestHarness:
    """Full SQLite + Governance + ToolRuntime + Vault harness shared by P09.02-P09.09 and Gate F."""

    tmp_path: Path
    db_path: Path
    workspace_root: Path
    vault_root: Path
    store: SQLiteRepositoryStore
    workflows: PersistentWorkflowService
    evaluator: PolicyEvaluator
    approvals: ApprovalService
    permissions: PermissionManager
    tool_registry: ToolRegistry
    tool_runtime: ToolRuntime
    agent_registry: AgentRegistryService
    artifact_vault: ArtifactVaultService
    evidence_vault: EvidenceAndKnowledgeService
    alice: TrustedAuthContext
    bob: TrustedAuthContext
    master: TrustedAuthContext
    qa_verifier: TrustedAuthContext


def make_department_harness(
    tmp_path: Path,
    *,
    sandbox_results: dict[str, CommandRunResult] | None = None,
    search_items: list[dict[str, str]] | None = None,
    fetched_html: str = "<html><body><h1>Verified Release Benchmark</h1><p>Latency reduced 42%.</p></body></html>",
) -> DepartmentTestHarness:
    """Construct initialized SQLite store, real tools, vaults, and registries."""
    db_path = tmp_path / "dept_runtime.db"
    workspace_root = tmp_path / "workspace"
    vault_root = tmp_path / "vault"
    workspace_root.mkdir(parents=True, exist_ok=True)
    vault_root.mkdir(parents=True, exist_ok=True)
    init_db(db_path)

    store = SQLiteRepositoryStore(db_path)
    approvals = ApprovalService(store.approvals, store.events)
    tool_registry = ToolRegistry(repository=store.tools, journal=store.events)

    def _sandbox_runner(cmd: list[str], timeout_s: int, cwd: Path | None) -> CommandRunResult:
        joined = " ".join(cmd)
        if sandbox_results:
            for key, res in sandbox_results.items():
                if key in joined:
                    return res
        return CommandRunResult(exit_code=0, stdout="sandbox ok", stderr="", duration_ms=15)

    def _search_backend(query: str, max_results: int) -> list[dict[str, str]]:
        if search_items is not None:
            return search_items[:max_results]
        return [
            {
                "title": f"Market Insight for {query}",
                "url": "https://example.org/research-summary",
                "snippet": f"Observed analysis for query '{query}'.",
            }
        ][:max_results]

    def _http_fetcher(url: str, timeout_s: int) -> HttpFetchResponse:
        return HttpFetchResponse(
            url=url,
            final_url=url,
            status_code=200,
            text=fetched_html,
        )

    register_phase07_tools(
        tool_registry,
        command_runner=_sandbox_runner,
        search_backend=_search_backend,
        http_fetcher=_http_fetcher,
        allow_private_urls_for_test=True,
        registered_at=utc(8, 0, 0),
    )

    registered_ids = sorted(m.id for m in tool_registry.list_manifests())
    evaluator = PolicyEvaluator(registered_tools=registered_ids)
    permissions = PermissionManager(evaluator=evaluator, journal=store.events, approval_service=approvals)
    agent_registry = AgentRegistryService(
        repository=store.agents,
        permission_manager=permissions,
        known_providers=("antigravity", "google_gemini", "openai", "ollama"),
        registered_tools=registered_ids,
    )
    tool_runtime = ToolRuntime(
        registry=tool_registry,
        permission_manager=permissions,
        journal=store.events,
        tool_repo=store.tools,
        agent_registry=agent_registry,
    )
    artifact_vault = ArtifactVaultService(
        store=store,
        vault_root=vault_root,
        permission_manager=permissions,
        journal=store.events,
    )
    evidence_vault = EvidenceAndKnowledgeService(
        store=store,
        permission_manager=permissions,
        journal=store.events,
        artifact_vault=artifact_vault,
    )
    return DepartmentTestHarness(
        tmp_path=tmp_path,
        db_path=db_path,
        workspace_root=workspace_root,
        vault_root=vault_root,
        store=store,
        workflows=PersistentWorkflowService(store),
        evaluator=evaluator,
        approvals=approvals,
        permissions=permissions,
        tool_registry=tool_registry,
        tool_runtime=tool_runtime,
        agent_registry=agent_registry,
        artifact_vault=artifact_vault,
        evidence_vault=evidence_vault,
        alice=operator_ctx("operator.alice"),
        bob=operator_ctx("operator.bob"),
        master=master_ctx(),
        qa_verifier=verifier_ctx(),
    )


@pytest.fixture
def dept_harness(tmp_path: Path) -> DepartmentTestHarness:
    """Provide clean SQLite + ToolRuntime + Vault harness closed after test."""
    harness = make_department_harness(tmp_path)
    try:
        yield harness
    finally:
        harness.store.close()


def request_and_approve(
    harness: DepartmentTestHarness,
    *,
    action: str,
    resource: str,
    parameters: dict[str, Any],
    requester: TrustedAuthContext,
    decider: TrustedAuthContext,
    task_id: str | None = None,
    run_id: str | None = None,
    risk_summary: str,
    timestamp: datetime,
) -> str:
    """Create and approve an explicit single-use Governance ApprovalRequest."""
    req_probe = harness.evaluator.evaluate(
        __import__("core.governance.models", fromlist=["PolicyRequest"]).PolicyRequest(
            auth_context=requester,
            action=action,
            resource=resource,
            task_id=task_id,
            run_id=run_id,
            parameters=parameters,
            timestamp=timestamp,
        )
    )
    apr = harness.approvals.create_approval_request(
        requester=requester,
        action=action,
        target_resource=resource,
        task_id=task_id,
        run_id=run_id,
        request_fingerprint=req_probe.request_fingerprint,
        policy_snapshot=ApprovalPolicyContext(
            matched_rule_ids=req_probe.matched_rule_ids,
            rule_versions=harness.evaluator.policy_snapshot(req_probe.matched_rule_ids),
            reason_code=req_probe.reason_code,
        ),
        risk_summary=risk_summary,
        requested_at=timestamp,
    )
    harness.approvals.decide_approval(
        apr.approval_id,
        decider=decider,
        approved=True,
        reason=f"Approved: {risk_summary}",
        timestamp=timestamp + timedelta(seconds=1),
    )
    return apr.approval_id


def register_worker_agent(
    harness: DepartmentTestHarness,
    *,
    agent_id: str,
    department: str,
    skills: list[str],
    tools_allowed: list[str] | None = None,
    role: str = "Department Specialist",
    validated_capabilities: list[str] | None = None,
    timestamp: datetime,
) -> Any:
    """Register a valid Worker Agent in AgentRegistryService, handling Approval Gate for sensitive grants."""
    manifest_dict: dict[str, Any] = {
        "spec_version": "1.0.0",
        "id": agent_id,
        "name": f"Agent {agent_id}",
        "role": role,
        "system_role": "worker",
        "department": department,
        "description": f"Registered specialist for {department} tests.",
        "mission": {
            "purpose": f"Execute bounded {department} tasks cleanly.",
            "responsibilities": [f"Perform {department} work inside assigned workspace."],
            "non_goals": ["Never bypass QA or Governance."],
        },
        "capabilities": {
            "skills": skills,
            "inputs": ["task_brief", "text"],
            "outputs": ["workspace_file_artifact", "structured_execution_report"],
        },
        "engine": {
            "default_type": "antigravity",
            "provider_id": "antigravity",
            "adapter_id": "antigravity_cli",
            "connection_id": "default_antigravity",
            "model_tier": "code",
            "model_id": None,
            "routing_profile": "workspace_sandboxed",
            "fallback_policy": "operator_approved_only",
            "parameters": {"temperature": 0.1},
        },
        "memory": {"scopes": ["private", "project"]},
        "skills": {"allowed": []},
        "tools": {"allowed": list(tools_allowed or [])},
        "permissions": {
            "fs_read": ["workspace/{task_id}/*"],
            "fs_write": ["workspace/{task_id}/*"],
            "network": "intranet" if tools_allowed and any("research" in t or "browser" in t for t in tools_allowed) else "none",
            "requires_approval": ["files.delete"],
        },
        "limits": {"timeout_seconds": 180, "max_retries": 1},
    }
    try:
        return harness.agent_registry.register_agent(
            manifest_dict,
            auth_context=harness.alice,
            timestamp=timestamp,
            validated_capabilities=validated_capabilities if validated_capabilities is not None else skills,
        )
    except PermissionDeniedError as err:
        assert err.decision.decision == "APPROVAL_REQUIRED"
        apr = harness.approvals.create_approval_request(
            requester=harness.alice,
            action=err.decision.action,
            target_resource=err.decision.resource,
            task_id=None,
            run_id=None,
            request_fingerprint=err.decision.request_fingerprint,
            policy_snapshot=ApprovalPolicyContext(
                matched_rule_ids=err.decision.matched_rule_ids,
                rule_versions=harness.evaluator.policy_snapshot(err.decision.matched_rule_ids),
                reason_code=err.decision.reason_code,
            ),
            risk_summary=f"Register worker {agent_id} with tools/network",
            requested_at=timestamp,
        )
        harness.approvals.decide_approval(
            apr.approval_id,
            decider=harness.bob,
            approved=True,
            reason=f"Approved registration for {agent_id}",
            timestamp=timestamp + timedelta(seconds=1),
        )
        return harness.agent_registry.register_agent(
            manifest_dict,
            auth_context=harness.alice,
            timestamp=timestamp + timedelta(seconds=2),
            validated_capabilities=validated_capabilities if validated_capabilities is not None else skills,
            approval_id=apr.approval_id,
        )


def create_task_and_run(
    harness: DepartmentTestHarness,
    *,
    title: str,
    description: str,
    timestamp: datetime,
    assigned_agent_ids: list[str] | None = None,
    task_status: str = "RUNNING",
) -> tuple[str, str]:
    """Persist real Task and RUNNING Run records in SQLite for FK-bound operations."""
    from core.contracts import Run, Task, new_run_id, new_task_id

    task_id = new_task_id()
    run_id = new_run_id()
    harness.workflows.create_task(
        Task(
            task_id=task_id,
            title=title,
            description=description,
            requested_by="operator.alice",
            status=task_status,  # type: ignore[arg-type]
            assigned_agent_ids=list(assigned_agent_ids or []),
            created_at=timestamp,
        )
    )
    harness.workflows.create_run(
        Run(
            run_id=run_id,
            task_id=task_id,
            runtime_id="custom.master_worker_v1",
            status="RUNNING",
            started_at=timestamp,
        )
    )
    return task_id, run_id
