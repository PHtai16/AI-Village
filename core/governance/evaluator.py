"""Deterministic Policy Evaluator enforcing RULE_SPEC precedence, tool allowlists, and routing checks."""

from __future__ import annotations

from fnmatch import fnmatch
import hashlib
import json
import re
from typing import Any, Sequence

from core.governance.models import (
    PolicyDecision,
    PolicyDecisionOutcome,
    PolicyRequest,
    PolicyRule,
)
from core.governance.workspace import validate_workspace_path

_TOOL_ID_RE = re.compile(r"^tools\.[a-z0-9_]+\.[a-z0-9_]+$")

PERMISSION_MATRIX_ACTIONS: frozenset[str] = frozenset(
    {
        "task.read",
        "task.create",
        "task.transition",
        "task.cancel",
        "task.provide_input",
        "task.update_plan",
        "files.read",
        "files.write",
        "files.delete",
        "tool.invoke",
        "tool.configure",
        "code.execute",
        "git.read",
        "git.write",
        "browser.open",
        "browser.interact",
        "browser.screenshot",
        "network.fetch",
        "network.search",
        "office.read",
        "office.write",
        "mcp.invoke",
        "agent.execute",
        "agent.handoff",
        "agent.assign",
        "agent.read",
        "agent.create",
        "agent.recruit",
        "agent.update",
        "agent.disable",
        "agent.enable",
        "agent.change_permissions",
        "agent.change_department",
        "agent.change_connection",
        "meeting.convene",
        "meeting.participate",
        "meeting.decide",
        "master.deliver",
        "runtime.cancel",
        "connection.read",
        "connection.manage",
        "connection.rotate_credential",
        "connection.change_provider",
        "approval.request",
        "approval.decide",
        "trash.restore",
        "trash.purge",
        "vault.read",
        "vault.write",
        "vault.delete",
        "vault.backup",
        "vault.restore",
        "knowledge.read",
        "knowledge.write",
        "settings.update",
        "kanban.transition",
        "skill.propose",
        "skill.review_technical",
        "skill.benchmark",
        "skill.activate",
        "skill.rollback",
    }
)


def compute_request_fingerprint(
    *,
    action: str,
    resource: str,
    task_id: str | None,
    run_id: str | None,
    parameters: dict[str, Any],
) -> str:
    """Compute canonical SHA-256 fingerprint binding an ApprovalRequest to exact parameters."""
    canonical = {
        "action": action,
        "resource": resource,
        "task_id": task_id,
        "run_id": run_id,
        "parameters": parameters,
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def build_default_policy_rules() -> list[PolicyRule]:
    """Return baseline Governance rules aligned with RULE_SPEC.md without granting implicit escalation."""
    return [
        PolicyRule(
            rule_id="sys.allow_operator_safe_ops",
            version="1.0.0",
            name="Allow Operator Read and Standard Task Operations",
            description="Human operator may read tasks/connections and manage task lifecycle except sensitive gates.",
            category="permission",
            targets=["human_operator"],
            resources=["*"],
            priority=200,
            effect="ALLOW",
            actions=[
                "task.read",
                "task.create",
                "task.transition",
                "task.cancel",
                "task.provide_input",
                "task.update_plan",
                "files.read",
                "files.write",
                "tool.invoke",
                "code.execute",
                "git.read",
                "browser.open",
                "browser.screenshot",
                "network.fetch",
                "network.search",
                "office.read",
                "office.write",
                "mcp.invoke",
                "agent.execute",
                "agent.handoff",
                "agent.assign",
                "agent.read",
                "agent.create",
                "agent.recruit",
                "agent.update",
                "agent.disable",
                "agent.enable",
                "meeting.convene",
                "meeting.participate",
                "meeting.decide",
                "master.deliver",
                "runtime.cancel",
                "connection.read",
                "approval.request",
                "approval.decide",
                "kanban.transition",
                "vault.read",
                "vault.write",
                "vault.delete",
                "vault.backup",
                "trash.restore",
                "knowledge.read",
                "knowledge.write",
                "skill.propose",
                "skill.review_technical",
                "skill.benchmark",
                "skill.activate",
                "skill.rollback",
            ],
        ),
        PolicyRule(
            rule_id="gov.require_approval_sensitive_admin_ops",
            version="1.0.0",
            name="Require Human Approval for Sensitive System/Connection/Destructive Operations",
            description="Deleting files, purging trash, updating settings, or managing connections require explicit approval.",
            category="approval",
            targets=["global"],
            resources=["*"],
            priority=500,
            effect="REQUIRE_APPROVAL",
            actions=[
                "files.delete",
                "git.write",
                "browser.interact",
                "tool.configure",
                "connection.manage",
                "connection.change_provider",
                "agent.change_permissions",
                "agent.change_department",
                "agent.change_connection",
                "trash.purge",
                "vault.restore",
                "settings.update",
            ],
            approval_required=True,
        ),
        PolicyRule(
            rule_id="orch.allow_master_coordination",
            version="1.0.0",
            name="Allow Master Agent Coordination Within Governance Bounds",
            description="Master may create tasks, assign workers, execute runs, cancel owned runs, and request approvals.",
            category="agent_behavior",
            targets=["master_agent"],
            resources=["*"],
            priority=150,
            effect="ALLOW",
            actions=[
                "task.read",
                "task.create",
                "task.transition",
                "task.cancel",
                "task.update_plan",
                "files.read",
                "agent.execute",
                "agent.handoff",
                "agent.assign",
                "agent.recruit",
                "agent.read",
                "meeting.convene",
                "meeting.participate",
                "meeting.decide",
                "master.deliver",
                "runtime.cancel",
                "connection.read",
                "approval.request",
                "kanban.transition",
                "vault.read",
                "vault.write",
                "knowledge.read",
                "knowledge.write",
                "skill.propose",
                "skill.activate",
                "skill.rollback",
            ],
        ),
        PolicyRule(
            rule_id="worker.allow_scoped_execution",
            version="1.0.0",
            name="Allow Worker Scoped File/Tool Execution",
            description="Workers may read/write inside task workspace, invoke allowlisted tools, and request approvals.",
            category="agent_behavior",
            targets=["worker_agent"],
            resources=["*"],
            priority=100,
            effect="ALLOW",
            actions=[
                "task.read",
                "files.read",
                "files.write",
                "tool.invoke",
                "code.execute",
                "git.read",
                "browser.open",
                "browser.screenshot",
                "network.fetch",
                "network.search",
                "office.read",
                "office.write",
                "mcp.invoke",
                "agent.read",
                "agent.handoff",
                "meeting.participate",
                "approval.request",
                "vault.read",
                "vault.write",
                "knowledge.read",
                "knowledge.write",
                "skill.propose",
                "skill.review_technical",
            ],
        ),
        PolicyRule(
            rule_id="runtime.allow_service_execution",
            version="1.0.0",
            name="Allow Runtime Service and Trusted Verifier Actions",
            description="Internal core runtime and trusted verifier services may read/execute/transition within guards.",
            category="permission",
            targets=["runtime_service", "trusted_verifier"],
            resources=["*"],
            priority=120,
            effect="ALLOW",
            actions=[
                "task.read",
                "task.transition",
                "task.cancel",
                "agent.execute",
                "agent.handoff",
                "agent.recruit",
                "agent.read",
                "meeting.participate",
                "runtime.cancel",
                "files.read",
                "vault.read",
                "vault.write",
                "knowledge.read",
                "knowledge.write",
                "skill.propose",
                "skill.benchmark",
            ],
        ),
    ]


def _rule_matches(rule: PolicyRule, request: PolicyRequest) -> bool:
    """Return True if an ACTIVE PolicyRule applies to request actor, action, and resource."""
    if rule.status != "ACTIVE":
        return False
    if request.action not in rule.actions and "*" not in rule.actions:
        return False
    auth = request.auth_context
    assert auth is not None
    target_match = (
        "global" in rule.targets
        or auth.actor_kind in rule.targets
        or auth.actor_id in rule.targets
    )
    if not target_match:
        return False
    return any(fnmatch(request.resource, pat) or pat == "*" for pat in rule.resources)


class PolicyEvaluator:
    """Deterministic Governance evaluator implementing RULE_SPEC conflict precedence."""

    def __init__(
        self,
        *,
        rules: Sequence[PolicyRule] | None = None,
        registered_tools: Sequence[str] | None = None,
    ) -> None:
        """Initialize evaluator with versioned rules and optional registered tool allowlist."""
        self._rules = list(rules) if rules is not None else build_default_policy_rules()
        self._registered_tools = set(registered_tools) if registered_tools is not None else set()

    @property
    def rules(self) -> list[PolicyRule]:
        """Return defensive copy of configured policy rules."""
        return list(self._rules)

    def policy_snapshot(self, matched_rule_ids: Sequence[str] = ()) -> dict[str, str]:
        """Return rule_id -> SemVer map for audit and ApprovalRequest snapshot records."""
        wanted = set(matched_rule_ids)
        return {
            rule.rule_id: rule.version
            for rule in self._rules
            if not wanted or rule.rule_id in wanted
        }

    def evaluate(self, request: PolicyRequest) -> PolicyDecision:
        """Evaluate ``PolicyRequest`` fail-closed without executing external side effects."""
        fingerprint = compute_request_fingerprint(
            action=request.action,
            resource=request.resource,
            task_id=request.task_id,
            run_id=request.run_id,
            parameters=request.parameters,
        )

        def _make_decision(
            outcome: PolicyDecisionOutcome,
            *,
            reason_code: str,
            message: str,
            matched_ids: list[str] | None = None,
            system_deny: bool = False,
        ) -> PolicyDecision:
            ids = matched_ids or []
            return PolicyDecision(
                decision=outcome,
                actor_id=request.auth_context.actor_id if request.auth_context else None,
                action=request.action,
                resource=request.resource,
                matched_rule_ids=ids,
                rule_versions=self.policy_snapshot(ids),
                reason_code=reason_code,
                message=message,
                evaluated_at=request.timestamp,
                correlation_id=request.correlation_id,
                system_deny=system_deny,
                request_fingerprint=fingerprint,
            )

        auth = request.auth_context
        if auth is None or not auth.authenticated:
            return _make_decision(
                "DENY",
                reason_code="MISSING_AUTH_CONTEXT",
                message="Action rejected fail-closed: missing or unauthenticated authorization context.",
                system_deny=True,
            )

        if auth.actor_kind == "human_operator" and auth.auth_source == "agent_claimed":
            return _make_decision(
                "DENY",
                reason_code="UNTRUSTED_OPERATOR_CLAIM",
                message="Agent-claimed operator identity is untrusted and rejected.",
                system_deny=True,
            )

        # Architectural Invariants & System Security (cannot be overridden by Approval).
        if request.action == "connection.rotate_credential":
            return _make_decision(
                "DENY",
                reason_code="CREDENTIAL_ROTATION_FORBIDDEN",
                message="Credential rotation via Policy/Agent path is strictly forbidden.",
                matched_ids=["inv.no_credential_rotation"],
                system_deny=True,
            )

        if auth.actor_kind in {"worker_agent", "master_agent"}:
            if request.action in {"approval.decide", "settings.update", "connection.manage", "tool.configure"}:
                return _make_decision(
                    "DENY",
                    reason_code="PRIVILEGE_ESCALATION_DENIED",
                    message=f"Actor kind '{auth.actor_kind}' cannot execute privileged action '{request.action}'.",
                    matched_ids=["inv.no_agent_privilege_escalation"],
                    system_deny=True,
                )
            target_agent = str(request.parameters.get("target_agent_id", "")).strip()
            if request.action in {
                "agent.update",
                "agent.disable",
                "agent.enable",
                "agent.change_permissions",
                "agent.change_department",
                "agent.change_connection",
            } and (
                target_agent == auth.actor_id
                or request.resource == f"agent:{auth.actor_id}"
            ):
                return _make_decision(
                    "DENY",
                    reason_code="SELF_MODIFICATION_FORBIDDEN",
                    message=f"Agent '{auth.actor_id}' cannot modify or disable its own Registry record.",
                    matched_ids=["inv.no_agent_self_modification"],
                    system_deny=True,
                )
            if request.action in {"agent.change_permissions", "agent.change_connection"}:
                return _make_decision(
                    "DENY",
                    reason_code="PRIVILEGE_ESCALATION_DENIED",
                    message=f"Actor kind '{auth.actor_kind}' cannot alter agent permissions or routing connections.",
                    matched_ids=["inv.no_agent_privilege_escalation"],
                    system_deny=True,
                )
            if request.action == "agent.disable" and (
                target_agent == "core.master" or request.resource == "agent:core.master"
            ):
                return _make_decision(
                    "DENY",
                    reason_code="PRIVILEGE_ESCALATION_DENIED",
                    message="Non-operator actors cannot disable core.master.",
                    matched_ids=["inv.protect_master_identity"],
                    system_deny=True,
                )
            if request.action == "task.transition" and str(request.parameters.get("target_status", "")).upper() == "COMPLETED":
                return _make_decision(
                    "DENY",
                    reason_code="QA_BYPASS_DENIED",
                    message=f"Actor kind '{auth.actor_kind}' cannot bypass independent QA to mark Task COMPLETED.",
                    matched_ids=["inv.qa_independence"],
                    system_deny=True,
                )

        if auth.actor_kind == "worker_agent" and request.action in {
            "agent.assign",
            "agent.create",
            "agent.recruit",
            "agent.update",
            "agent.disable",
            "agent.enable",
            "agent.change_department",
            "meeting.convene",
            "meeting.decide",
            "master.deliver",
            "trash.purge",
            "trash.restore",
            "vault.delete",
            "vault.backup",
            "vault.restore",
            "skill.benchmark",
            "skill.activate",
            "skill.rollback",
        }:
            return _make_decision(
                "DENY",
                reason_code="PRIVILEGE_ESCALATION_DENIED",
                message=f"Worker agent cannot perform '{request.action}'.",
                matched_ids=["inv.worker_least_privilege"],
                system_deny=True,
            )

        if request.action == "agent.handoff" and (
            bool(request.parameters.get("privilege_expansion"))
            or bool(request.parameters.get("escalate_receiver_permissions"))
            or bool(request.parameters.get("bypass_qa"))
            or bool(request.parameters.get("grant_tools"))
        ):
            return _make_decision(
                "DENY",
                reason_code="PRIVILEGE_ESCALATION_DENIED",
                message="Handoff cannot expand receiver permissions, grant tools, or bypass QA.",
                matched_ids=["inv.handoff_no_privilege_escalation"],
                system_deny=True,
            )

        if request.action == "master.deliver" and (
            bool(request.parameters.get("override_qa_unverified"))
            or bool(request.parameters.get("force_verified"))
        ):
            return _make_decision(
                "DENY",
                reason_code="QA_BYPASS_DENIED",
                message="Master delivery cannot mark unverified deliverables as VERIFIED or bypass QA.",
                matched_ids=["inv.master_delivery_no_qa_bypass"],
                system_deny=True,
            )

        if request.action == "skill.activate" and (
            bool(request.parameters.get("requires_human_approval"))
            or bool(request.parameters.get("has_scripts"))
            or bool(request.parameters.get("has_permission_delta"))
            or bool(request.parameters.get("sensitive_workflow"))
            or str(request.parameters.get("risk_level", "low")).lower() in {"medium", "high"}
        ):
            return _make_decision(
                "APPROVAL_REQUIRED",
                reason_code="SENSITIVE_SKILL_APPROVAL_REQUIRED",
                message=(
                    "Skill activation modifies tools/permissions, scripts, dependencies, or sensitive "
                    "workflows and requires explicit Human Operator approval."
                ),
                matched_ids=["gov.require_approval_sensitive_skill_activation"],
            )

        # Workspace boundary check whenever workspace_root or target_path is supplied.
        if request.action in {"files.read", "files.write", "files.delete"} or request.target_path is not None:
            if request.workspace_root is None or request.target_path is None:
                return _make_decision(
                    "DENY",
                    reason_code="MISSING_WORKSPACE_CONTEXT",
                    message="File or path-scoped action requires both workspace_root and target_path.",
                    matched_ids=["sys.workspace_scope_required"],
                    system_deny=True,
                )
            ws_check = validate_workspace_path(
                request.workspace_root,
                request.target_path,
                action=request.action,
                cross_task_roots=request.cross_task_roots,
            )
            if not ws_check.allowed:
                return _make_decision(
                    "DENY",
                    reason_code=ws_check.reason_code,
                    message=ws_check.message,
                    matched_ids=["sys.workspace_isolation"],
                    system_deny=True,
                )

        # Tool allowlist enforcement for tool.invoke.
        if request.action == "tool.invoke":
            if not request.tool_id or not _TOOL_ID_RE.fullmatch(request.tool_id):
                return _make_decision(
                    "DENY",
                    reason_code="UNKNOWN_TOOL_ID",
                    message=f"Invalid or missing canonical Tool ID: {request.tool_id!r}.",
                    matched_ids=["sys.tool_registry_check"],
                    system_deny=True,
                )
            if self._registered_tools and request.tool_id not in self._registered_tools:
                return _make_decision(
                    "DENY",
                    reason_code="UNKNOWN_TOOL_ID",
                    message=f"Tool '{request.tool_id}' is not registered in Tool Registry.",
                    matched_ids=["sys.tool_registry_check"],
                    system_deny=True,
                )
            if request.tool_id not in auth.allowed_tools and "*" not in auth.allowed_tools:
                return _make_decision(
                    "DENY",
                    reason_code="TOOL_NOT_ALLOWED",
                    message=f"Actor '{auth.actor_id}' is not permitted to invoke tool '{request.tool_id}'.",
                    matched_ids=["sys.tool_allowlist"],
                    system_deny=False,
                )

        # Routing & Residency Policy checks.
        if request.routing is not None:
            routing = request.routing
            if routing.data_residency == "local_only" and routing.target_residency != "local_only":
                return _make_decision(
                    "DENY",
                    reason_code="LOCAL_ONLY_VIOLATION",
                    message="Refusing to route local_only workload to external cloud residency.",
                    matched_ids=["sys.local_only_residency"],
                    system_deny=True,
                )
            if routing.allowed_providers and routing.provider_id not in routing.allowed_providers:
                return _make_decision(
                    "DENY",
                    reason_code="PROVIDER_NOT_ALLOWED",
                    message=f"Provider '{routing.provider_id}' is outside allowed_providers.",
                    matched_ids=["sys.routing_provider_allowlist"],
                )
            if (
                routing.connection_id is not None
                and routing.allowed_connections
                and routing.connection_id not in routing.allowed_connections
            ):
                return _make_decision(
                    "DENY",
                    reason_code="CONNECTION_NOT_ALLOWED",
                    message=f"Connection '{routing.connection_id}' is outside allowed_connections.",
                    matched_ids=["sys.routing_connection_allowlist"],
                )
            if (
                routing.budget_limit_usd is not None
                and routing.estimated_cost_usd is not None
                and routing.estimated_cost_usd > routing.budget_limit_usd
            ):
                return _make_decision(
                    "APPROVAL_REQUIRED",
                    reason_code="BUDGET_EXCEEDED",
                    message=(
                        f"Estimated cost ${routing.estimated_cost_usd:.4f} exceeds budget "
                        f"${routing.budget_limit_usd:.4f}; human approval required."
                    ),
                    matched_ids=["gov.budget_threshold"],
                )

        # Enforce actor-level explicit allowed_actions restriction when declared on non-wildcard context.
        if auth.allowed_actions and "*" not in auth.allowed_actions and request.action not in auth.allowed_actions:
            return _make_decision(
                "DENY",
                reason_code="ACTION_NOT_IN_ACTOR_ALLOWLIST",
                message=f"Actor '{auth.actor_id}' does not hold action permission '{request.action}'.",
                matched_ids=["actor.allowed_actions"],
            )

        # Evaluate active configured PolicyRules with strict precedence: DENY > REQUIRE_APPROVAL > ALLOW.
        matched = sorted(
            (rule for rule in self._rules if _rule_matches(rule, request)),
            key=lambda r: r.priority,
            reverse=True,
        )
        matched_ids = [r.rule_id for r in matched]
        deny_rules = [r for r in matched if r.effect == "DENY"]
        if deny_rules:
            return _make_decision(
                "DENY",
                reason_code="DENY_RULE_MATCHED",
                message=f"Action '{request.action}' denied by rule '{deny_rules[0].rule_id}'.",
                matched_ids=matched_ids,
            )

        approval_rules = [r for r in matched if r.effect == "REQUIRE_APPROVAL"]
        if approval_rules:
            return _make_decision(
                "APPROVAL_REQUIRED",
                reason_code="APPROVAL_RULE_MATCHED",
                message=f"Action '{request.action}' requires human approval per '{approval_rules[0].rule_id}'.",
                matched_ids=matched_ids,
            )

        allow_rules = [r for r in matched if r.effect == "ALLOW"]
        if allow_rules:
            return _make_decision(
                "ALLOW",
                reason_code="POLICY_ALLOWED",
                message=f"Action '{request.action}' allowed by rule '{allow_rules[0].rule_id}'.",
                matched_ids=matched_ids,
            )

        # AUDIT_ONLY rules or empty match list never grant implicit access.
        return _make_decision(
            "DENY",
            reason_code="DEFAULT_DENY",
            message=f"No active ALLOW rule permits action '{request.action}' on '{request.resource}'.",
            matched_ids=matched_ids,
        )
