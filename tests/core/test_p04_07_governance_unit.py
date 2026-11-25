"""Unit tests for Phase 04 / P04.07 Governance Evaluator, Workspace Guards, and Permission Manager."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from core.contracts import Event, new_run_id, new_task_id
from core.events import AppendResult, InMemoryEventJournal, JournalError
from core.governance import (
    ApprovalPolicyContext,
    ApprovalService,
    GovernanceAuditError,
    PERMISSION_MATRIX_ACTIONS,
    PermissionDeniedError,
    PermissionManager,
    PolicyEvaluator,
    PolicyRequest,
    PolicyRule,
    RoutingContext,
    TrustedAuthContext,
    build_default_policy_rules,
    compute_request_fingerprint,
    validate_workspace_path,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _utc(hour: int = 12, minute: int = 0) -> datetime:
    """Return deterministic UTC timestamp."""
    return datetime(2026, 11, 25, hour, minute, 0, tzinfo=timezone.utc)


def _human_ctx(actor_id: str = "operator.alice", **kwargs: object) -> TrustedAuthContext:
    """Build trusted human operator context."""
    return TrustedAuthContext(
        actor_id=actor_id,
        actor_kind="human_operator",
        auth_source="operator_session",
        authenticated=True,
        **kwargs,  # type: ignore[arg-type]
    )


def _worker_ctx(
    actor_id: str = "dev.backend",
    allowed_tools: list[str] | None = None,
    allowed_actions: list[str] | None = None,
) -> TrustedAuthContext:
    """Build trusted worker agent context."""
    return TrustedAuthContext(
        actor_id=actor_id,
        actor_kind="worker_agent",
        auth_source="application_boundary",
        authenticated=True,
        allowed_tools=allowed_tools if allowed_tools is not None else [],
        allowed_actions=allowed_actions if allowed_actions is not None else [],
    )


def _master_ctx(actor_id: str = "core.master") -> TrustedAuthContext:
    """Build trusted master agent context."""
    return TrustedAuthContext(
        actor_id=actor_id,
        actor_kind="master_agent",
        auth_source="application_boundary",
        authenticated=True,
    )


def test_core_governance_module_architecture_boundaries_via_ast() -> None:
    """Verify core/governance modules stay pure and never import storage, subprocess, or UI."""
    forbidden_prefixes = (
        "sqlite3",
        "storage",
        "sqlalchemy",
        "subprocess",
        "crewai",
        "api",
        "ui",
        "httpx",
        "urllib",
        "requests",
    )
    gov_dir = REPO_ROOT / "core" / "governance"
    for py_file in sorted(gov_dir.rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(forbidden_prefixes), (
                        f"Forbidden import '{alias.name}' in {py_file}"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                assert not node.module.startswith(forbidden_prefixes), (
                    f"Forbidden import-from '{node.module}' in {py_file}"
                )


def test_rule_precedence_explicit_allow_deny_approval_and_audit_only() -> None:
    """Enforce DENY > REQUIRE_APPROVAL > ALLOW and ensure AUDIT_ONLY never grants access."""
    worker = _worker_ctx()
    ts = _utc()

    allow_rule = PolicyRule(
        rule_id="test.allow_task_read",
        name="Allow Read",
        description="Allow reading task",
        category="permission",
        targets=["worker_agent"],
        priority=900,
        effect="ALLOW",
        actions=["task.read"],
    )
    deny_low_prio = PolicyRule(
        rule_id="test.deny_task_read",
        name="Deny Read",
        description="Explicit deny overrides higher-priority allow",
        category="permission",
        targets=["worker_agent"],
        priority=10,
        effect="DENY",
        actions=["task.read"],
    )
    approval_low_prio = PolicyRule(
        rule_id="test.approval_task_read",
        name="Require Approval Read",
        description="Require approval overrides allow",
        category="approval",
        targets=["worker_agent"],
        priority=20,
        effect="REQUIRE_APPROVAL",
        actions=["task.read"],
        approval_required=True,
    )
    audit_only_rule = PolicyRule(
        rule_id="test.audit_only_read",
        name="Audit Read",
        description="Audit only never grants implicit ALLOW",
        category="permission",
        targets=["worker_agent"],
        priority=999,
        effect="AUDIT_ONLY",
        actions=["task.read"],
    )

    req = PolicyRequest(
        auth_context=worker,
        action="task.read",
        resource="task:tsk_01",
        timestamp=ts,
    )

    # Explicit ALLOW works when sole matching rule.
    allow_dec = PolicyEvaluator(rules=[allow_rule]).evaluate(req)
    assert allow_dec.decision == "ALLOW"
    assert allow_dec.reason_code == "POLICY_ALLOWED"

    # DENY overrides higher-priority ALLOW.
    deny_dec = PolicyEvaluator(rules=[allow_rule, approval_low_prio, deny_low_prio]).evaluate(req)
    assert deny_dec.decision == "DENY"
    assert deny_dec.reason_code == "DENY_RULE_MATCHED"

    # REQUIRE_APPROVAL overrides higher-priority ALLOW.
    appr_dec = PolicyEvaluator(rules=[allow_rule, approval_low_prio]).evaluate(req)
    assert appr_dec.decision == "APPROVAL_REQUIRED"
    assert appr_dec.reason_code == "APPROVAL_RULE_MATCHED"

    # AUDIT_ONLY without ALLOW fails closed with DEFAULT_DENY.
    audit_dec = PolicyEvaluator(rules=[audit_only_rule]).evaluate(req)
    assert audit_dec.decision == "DENY"
    assert audit_dec.reason_code == "DEFAULT_DENY"

    # Empty ruleset fails closed with DEFAULT_DENY.
    empty_dec = PolicyEvaluator(rules=[]).evaluate(req)
    assert empty_dec.decision == "DENY"
    assert empty_dec.reason_code == "DEFAULT_DENY"


def test_missing_or_untrusted_auth_context_rejected_fail_closed() -> None:
    """Reject missing auth_context, unauthenticated callers, and agent_claimed operator claims."""
    evaluator = PolicyEvaluator()
    ts = _utc()

    no_auth = evaluator.evaluate(
        PolicyRequest(auth_context=None, action="task.read", resource="task:tsk_01", timestamp=ts)
    )
    assert no_auth.decision == "DENY"
    assert no_auth.reason_code == "MISSING_AUTH_CONTEXT"
    assert no_auth.system_deny is True

    unauth = evaluator.evaluate(
        PolicyRequest(
            auth_context=TrustedAuthContext(
                actor_id="operator.bob",
                actor_kind="human_operator",
                auth_source="operator_session",
                authenticated=False,
            ),
            action="task.read",
            resource="task:tsk_01",
            timestamp=ts,
        )
    )
    assert unauth.decision == "DENY"
    assert unauth.reason_code == "MISSING_AUTH_CONTEXT"

    # Pydantic model itself forbids agent_claimed human_operator.
    with pytest.raises(ValidationError, match="human_operator"):
        TrustedAuthContext(
            actor_id="operator.spoof",
            actor_kind="human_operator",
            auth_source="agent_claimed",
            authenticated=True,
        )

    # Even if constructed bypassing Pydantic validators, PolicyEvaluator rejects fail-closed.
    spoofed = TrustedAuthContext.model_construct(
        actor_id="operator.spoof",
        actor_kind="human_operator",
        auth_source="agent_claimed",
        authenticated=True,
        allowed_actions=[],
        allowed_tools=[],
        assigned_task_ids=[],
    )
    spoof_dec = evaluator.evaluate(
        PolicyRequest.model_construct(
            auth_context=spoofed,
            action="task.read",
            resource="task:tsk_01",
            task_id=None,
            run_id=None,
            workspace_root=None,
            target_path=None,
            cross_task_roots=[],
            tool_id=None,
            routing=None,
            approval_id=None,
            parameters={},
            timestamp=ts,
            correlation_id=None,
        )
    )
    assert spoof_dec.decision == "DENY"
    assert spoof_dec.reason_code == "UNTRUSTED_OPERATOR_CLAIM"
    assert spoof_dec.system_deny is True


def test_agent_and_master_privilege_escalation_and_qa_bypass_denied() -> None:
    """Prevent worker_agent and master_agent from self-escalating or bypassing QA."""
    evaluator = PolicyEvaluator()
    ts = _utc()
    worker = _worker_ctx()
    master = _master_ctx()

    for actor in (worker, master):
        for priv_action in ("approval.decide", "settings.update", "connection.manage", "tool.configure"):
            dec = evaluator.evaluate(
                PolicyRequest(
                    auth_context=actor,
                    action=priv_action,
                    resource="system:config",
                    timestamp=ts,
                )
            )
            assert dec.decision == "DENY"
            assert dec.reason_code == "PRIVILEGE_ESCALATION_DENIED"
            assert dec.system_deny is True

        qa_bypass = evaluator.evaluate(
            PolicyRequest(
                auth_context=actor,
                action="task.transition",
                resource="task:tsk_01",
                task_id=new_task_id(),
                parameters={"target_status": "COMPLETED"},
                timestamp=ts,
            )
        )
        assert qa_bypass.decision == "DENY"
        assert qa_bypass.reason_code == "QA_BYPASS_DENIED"
        assert qa_bypass.system_deny is True

    # Worker cannot assign agents or purge trash, nor cancel/transition tasks unauthorized.
    for denied_worker_action in ("agent.assign", "trash.purge", "task.cancel", "kanban.transition"):
        dec = evaluator.evaluate(
            PolicyRequest(
                auth_context=worker,
                action=denied_worker_action,
                resource="task:tsk_01",
                timestamp=ts,
            )
        )
        assert dec.decision == "DENY"


def test_workspace_isolation_traversal_cross_task_secrets_and_symlink_blocked(
    tmp_path: Path,
) -> None:
    """Block path traversal, external paths, cross-task leaks, secret files, protected files, and symlinks."""
    ws_task_a = tmp_path / "task_a"
    ws_task_b = tmp_path / "task_b"
    outside_dir = tmp_path / "outside"
    ws_task_a.mkdir()
    ws_task_b.mkdir()
    outside_dir.mkdir()

    safe_file = ws_task_a / "src" / "main.py"
    safe_file.parent.mkdir()
    safe_file.write_text("print('ok')\n", encoding="utf-8")

    secret_outside = outside_dir / "escape.txt"
    secret_outside.write_text("external", encoding="utf-8")
    (ws_task_b / "notes.md").write_text("task b only", encoding="utf-8")

    evaluator = PolicyEvaluator()
    worker = _worker_ctx()
    ts = _utc()

    # Valid file read/write inside task workspace succeeds.
    ok_ws = evaluator.evaluate(
        PolicyRequest(
            auth_context=worker,
            action="files.write",
            resource="workspace:src/main.py",
            workspace_root=str(ws_task_a),
            target_path="src/main.py",
            cross_task_roots=[str(ws_task_b)],
            timestamp=ts,
        )
    )
    assert ok_ws.decision == "ALLOW"

    # 1) Path traversal with '..' blocked.
    trav = validate_workspace_path(ws_task_a, "../outside/escape.txt")
    assert not trav.allowed
    assert trav.reason_code == "PATH_TRAVERSAL_DENIED"

    # 2) Direct outside path blocked.
    outside = validate_workspace_path(ws_task_a, secret_outside)
    assert not outside.allowed
    assert outside.reason_code == "PATH_OUTSIDE_WORKSPACE"

    # 3) Cross-task path blocked even when explicitly tracked.
    cross = validate_workspace_path(
        ws_task_a, ws_task_b / "notes.md", cross_task_roots=[ws_task_b]
    )
    assert not cross.allowed
    assert cross.reason_code == "CROSS_TASK_ACCESS_DENIED"

    # 4) Secret filenames & runtime paths blocked.
    for secret_rel in (
        ".env",
        ".env.production",
        "config/credentials.json",
        "token.json",
        "certs/private.pem",
        "keys/id_rsa",
        ".ssh/config",
        "secrets/api.yaml",
        ".runtime/state.db",
    ):
        sec_res = validate_workspace_path(ws_task_a, secret_rel)
        assert not sec_res.allowed
        assert sec_res.reason_code == "SECRET_FILE_ACCESS_DENIED"

    # 5) Protected system directories/files blocked for write/delete.
    for prot_rel in ("core/tasks/state_machine.py", "AGENTS.md", "pyproject.toml", ".git/config"):
        prot_res = validate_workspace_path(ws_task_a, prot_rel, action="files.write")
        assert not prot_res.allowed
        assert prot_res.reason_code == "PROTECTED_PATH_DENIED"

    # 6) Missing workspace context denied fail-closed.
    missing_ws = evaluator.evaluate(
        PolicyRequest(
            auth_context=worker,
            action="files.read",
            resource="workspace:main.py",
            timestamp=ts,
        )
    )
    assert missing_ws.decision == "DENY"
    assert missing_ws.reason_code == "MISSING_WORKSPACE_CONTEXT"

    # 7) Real symlink / Windows directory junction escaping workspace_root is blocked by realpath!
    link_path = ws_task_a / "escaped_link"
    created_real_link = False
    try:
        link_path.symlink_to(outside_dir, target_is_directory=True)
        created_real_link = True
    except OSError:
        if sys.platform == "win32":
            proc = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link_path), str(outside_dir)],
                capture_output=True,
                text=True,
                check=False,
            )
            created_real_link = proc.returncode == 0
    assert created_real_link, "Failed to create real symlink or directory junction in tmp_path"

    link_check = evaluator.evaluate(
        PolicyRequest(
            auth_context=worker,
            action="files.read",
            resource="workspace:escaped_link/escape.txt",
            workspace_root=str(ws_task_a),
            target_path="escaped_link/escape.txt",
            timestamp=ts,
        )
    )
    assert link_check.decision == "DENY"
    assert link_check.reason_code == "PATH_OUTSIDE_WORKSPACE"
    assert link_check.system_deny is True


def test_tool_allowlist_unknown_tool_and_routing_policies() -> None:
    """Enforce Tool Registry allowlist, residency/local_only, budget threshold, and unknown quota=None."""
    evaluator = PolicyEvaluator(
        registered_tools=["tools.filesystem.read_file", "tools.terminal.exec"]
    )
    ts = _utc()
    worker = _worker_ctx(allowed_tools=["tools.filesystem.read_file"])

    # Registered and allowlisted tool passes.
    ok_tool = evaluator.evaluate(
        PolicyRequest(
            auth_context=worker,
            action="tool.invoke",
            resource="tool:tools.filesystem.read_file",
            tool_id="tools.filesystem.read_file",
            timestamp=ts,
        )
    )
    assert ok_tool.decision == "ALLOW"

    # Unregistered tool ID denied even if worker claims it in allowed_tools.
    stealth_worker = _worker_ctx(allowed_tools=["tools.unknown.hack", "tools.filesystem.read_file"])
    unknown_tool = evaluator.evaluate(
        PolicyRequest(
            auth_context=stealth_worker,
            action="tool.invoke",
            resource="tool:tools.unknown.hack",
            tool_id="tools.unknown.hack",
            timestamp=ts,
        )
    )
    assert unknown_tool.decision == "DENY"
    assert unknown_tool.reason_code == "UNKNOWN_TOOL_ID"

    # Registered tool NOT in worker's allowlist denied.
    not_allowed = evaluator.evaluate(
        PolicyRequest(
            auth_context=worker,
            action="tool.invoke",
            resource="tool:tools.terminal.exec",
            tool_id="tools.terminal.exec",
            timestamp=ts,
        )
    )
    assert not_allowed.decision == "DENY"
    assert not_allowed.reason_code == "TOOL_NOT_ALLOWED"

    # Credential rotation strictly forbidden.
    human = _human_ctx()
    cred_rot = evaluator.evaluate(
        PolicyRequest(
            auth_context=human,
            action="connection.rotate_credential",
            resource="connection:conn_01",
            timestamp=ts,
        )
    )
    assert cred_rot.decision == "DENY"
    assert cred_rot.reason_code == "CREDENTIAL_ROTATION_FORBIDDEN"
    assert cred_rot.system_deny is True

    # local_only violation denied.
    local_violation = evaluator.evaluate(
        PolicyRequest(
            auth_context=human,
            action="agent.execute",
            resource="provider:cloud_llm",
            routing=RoutingContext(
                provider_id="cloud_llm",
                data_residency="local_only",
                target_residency="cloud",
                quota_remaining=None,
            ),
            timestamp=ts,
        )
    )
    assert local_violation.decision == "DENY"
    assert local_violation.reason_code == "LOCAL_ONLY_VIOLATION"
    assert local_violation.system_deny is True

    # Unknown quota remains None and allows permitted local_only routing.
    local_ok_req = PolicyRequest(
        auth_context=human,
        action="agent.execute",
        resource="provider:local_ollama",
        routing=RoutingContext(
            provider_id="local_ollama",
            connection_id="conn_local",
            data_residency="local_only",
            target_residency="local_only",
            allowed_providers=["local_ollama"],
            allowed_connections=["conn_local"],
            estimated_cost_usd=0.0,
            budget_limit_usd=1.0,
            quota_remaining=None,
        ),
        timestamp=ts,
    )
    assert local_ok_req.routing is not None and local_ok_req.routing.quota_remaining is None
    assert evaluator.evaluate(local_ok_req).decision == "ALLOW"

    # Budget exceeded triggers APPROVAL_REQUIRED.
    over_budget = evaluator.evaluate(
        PolicyRequest(
            auth_context=human,
            action="agent.execute",
            resource="provider:openai",
            routing=RoutingContext(
                provider_id="openai",
                estimated_cost_usd=15.0,
                budget_limit_usd=5.0,
                quota_remaining=None,
            ),
            timestamp=ts,
        )
    )
    assert over_budget.decision == "APPROVAL_REQUIRED"
    assert over_budget.reason_code == "BUDGET_EXCEEDED"

    # Action outside actor's allowed_actions denied.
    narrow_human = _human_ctx(allowed_actions=["task.read"])
    narrow_dec = evaluator.evaluate(
        PolicyRequest(
            auth_context=narrow_human,
            action="task.create",
            resource="task:new",
            timestamp=ts,
        )
    )
    assert narrow_dec.decision == "DENY"
    assert narrow_dec.reason_code == "ACTION_NOT_IN_ACTOR_ALLOWLIST"


def test_secret_rejection_and_fail_closed_audit_failure() -> None:
    """Reject credential patterns in Governance inputs and block actions if audit journal fails."""
    ts = _utc()
    with pytest.raises(ValidationError):
        PolicyRequest(
            auth_context=_human_ctx(),
            action="task.create",
            resource="task:t1",
            parameters={"api_key": "sk-live-secret1234567890abcdef"},
            timestamp=ts,
        )

    class _FailingJournal:
        def append(self, event: Event) -> AppendResult:
            return AppendResult(
                status="REJECTED",
                appended=False,
                error=JournalError(error_code="UNSAFE_PAYLOAD", message="simulated journal rejection"),
            )

        def read_entries(self, **kwargs: object) -> list[object]:  # pragma: no cover - unused
            return []

    executed: list[str] = []
    pm = PermissionManager(
        evaluator=PolicyEvaluator(rules=build_default_policy_rules()),
        journal=_FailingJournal(),  # type: ignore[arg-type]
    )
    with pytest.raises(GovernanceAuditError, match="simulated journal rejection"):
        pm.dispatch_protected_action(
            PolicyRequest(
                auth_context=_human_ctx(),
                action="task.read",
                resource="task:t1",
                timestamp=ts,
            ),
            lambda: executed.append("should_not_run"),
        )
    assert executed == []


def test_permission_matrix_and_unapproved_destructive_action_blocked() -> None:
    """Confirm sensitive Mission Control/admin operations require approval before running."""
    journal = InMemoryEventJournal()
    evaluator = PolicyEvaluator()
    pm = PermissionManager(evaluator=evaluator, journal=journal)
    ts = _utc()
    operator = _human_ctx()

    # Sanity check Permission Matrix completeness.
    assert {
        "kanban.transition",
        "files.delete",
        "connection.manage",
        "connection.change_provider",
        "settings.update",
        "trash.purge",
    }.issubset(PERMISSION_MATRIX_ACTIONS)

    side_effect: list[str] = []
    for gated_action in ("files.delete", "connection.manage", "settings.update", "trash.purge"):
        with pytest.raises(PermissionDeniedError) as exc_info:
            pm.dispatch_protected_action(
                PolicyRequest(
                    auth_context=operator,
                    action=gated_action,
                    resource=f"resource:{gated_action}",
                    workspace_root="." if gated_action == "files.delete" else None,
                    target_path="scratch/tmp.txt" if gated_action == "files.delete" else None,
                    timestamp=ts,
                ),
                lambda: side_effect.append(gated_action),
            )
        assert exc_info.value.decision.decision == "APPROVAL_REQUIRED"
    assert side_effect == []
    assert len(journal.read_entries()) == 4
