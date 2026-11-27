"""Core Governance package: Policy Evaluator, Workspace Security, Human Approval Gate, and Audit."""

from core.governance.approvals import ApprovalError, ApprovalRepository, ApprovalService
from core.governance.evaluator import (
    PERMISSION_MATRIX_ACTIONS,
    PolicyEvaluator,
    build_default_policy_rules,
    compute_request_fingerprint,
)
from core.governance.manager import (
    GovernanceAuditError,
    PermissionDeniedError,
    PermissionManager,
)
from core.governance.models import (
    ActorKind,
    ApprovalDecision,
    ApprovalPolicyContext,
    ApprovalRequest,
    ApprovalStatus,
    AuthSource,
    PolicyDecision,
    PolicyDecisionOutcome,
    PolicyRequest,
    PolicyRule,
    RoutingContext,
    RuleCategory,
    RuleEffect,
    RuleStatus,
    TrustedAuthContext,
    actor_kind_to_event_source,
    new_approval_id,
    resolve_trusted_auth_context,
)
from core.governance.workspace import WorkspaceAccessCheck, validate_workspace_path

__all__ = [
    "ActorKind",
    "ApprovalDecision",
    "ApprovalError",
    "ApprovalPolicyContext",
    "ApprovalRepository",
    "ApprovalRequest",
    "ApprovalStatus",
    "ApprovalService",
    "AuthSource",
    "GovernanceAuditError",
    "PERMISSION_MATRIX_ACTIONS",
    "PermissionDeniedError",
    "PermissionManager",
    "PolicyDecision",
    "PolicyDecisionOutcome",
    "PolicyEvaluator",
    "PolicyRequest",
    "PolicyRule",
    "RoutingContext",
    "RuleCategory",
    "RuleEffect",
    "RuleStatus",
    "TrustedAuthContext",
    "WorkspaceAccessCheck",
    "actor_kind_to_event_source",
    "build_default_policy_rules",
    "compute_request_fingerprint",
    "new_approval_id",
    "resolve_trusted_auth_context",
    "validate_workspace_path",
]
