"""Unit and Boundary tests for Phase 05 / P05.01 Agent Manifest Schema & Safe YAML Loader."""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest
from pydantic import ValidationError
import yaml

from core.agents import (
    DEFAULT_DEPARTMENTS,
    AgentManifest,
    DepartmentManifest,
    ManifestLoadError,
    is_valid_worker_department,
    load_agent_manifest_file,
    load_department_manifest_file,
    manifest_to_spike_agent_spec,
    validate_agent_manifest,
)
from core.contracts.agent import AgentIdentity
from core.governance import PolicyEvaluator, PolicyRequest, TrustedAuthContext

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS_ROOT = REPO_ROOT / "agents"


def _base_worker_payload() -> dict[str, Any]:
    """Return a clean baseline valid Worker manifest mapping for deterministic mutation tests."""
    return {
        "spec_version": "1.0.0",
        "id": "it.code_reviewer",
        "name": "Quốc Bảo",
        "role": "Senior Code Quality Inspector",
        "system_role": "worker",
        "department": "it",
        "description": "Code reviewer scoped strictly to isolated task inspection.",
        "mission": {
            "purpose": "Review task diffs against architectural rules.",
            "responsibilities": [
                "Inspect diff cleanly inside workspace scope.",
                "Report concrete security and PEP 8 findings.",
            ],
            "non_goals": ["Never bypass independent QA checks."],
        },
        "capabilities": {
            "skills": ["git_diff_analysis", "python_ast_audit"],
            "inputs": ["git_diff", "source_code"],
            "outputs": ["markdown_review_report"],
        },
        "engine": {
            "default_type": "antigravity",
            "provider_id": "antigravity",
            "adapter_id": "antigravity_cli",
            "connection_id": "default_antigravity",
            "model_tier": "code",
            "model_id": None,
            "routing_profile": "code_precision",
            "fallback_policy": "operator_approved_only",
            "parameters": {"temperature": 0.1},
        },
        "memory": {"scopes": ["private", "project"]},
        "skills": {"allowed": ["git_diff_code_review"]},
        "tools": {"allowed": ["tools.files.read", "tools.code.run_linter"]},
        "permissions": {
            "fs_read": ["workspace/{task_id}/inputs/*", "docs/*"],
            "fs_write": ["workspace/{task_id}/outputs/*"],
            "network": "none",
            "requires_approval": ["code.exec_custom"],
        },
        "limits": {"timeout_seconds": 180, "max_retries": 2},
    }


def test_core_agents_architecture_boundaries_via_ast() -> None:
    """Keep core/agents free of SQLite, storage, subprocess, CrewAI, UI, and network clients."""
    forbidden = (
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
    agents_core_dir = REPO_ROOT / "core" / "agents"
    for py_file in sorted(agents_core_dir.rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(forbidden), (
                        f"Forbidden import '{alias.name}' in {py_file}"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                assert not node.module.startswith(forbidden), (
                    f"Forbidden import-from '{node.module}' in {py_file}"
                )


def test_01_valid_master_manifest_on_disk() -> None:
    """1. Load and validate official Master manifest agents/master/agent.yaml."""
    manifest = load_agent_manifest_file(
        AGENTS_ROOT / "master" / "agent.yaml",
        allowed_root=AGENTS_ROOT,
        enforce_path_convention=True,
    )
    assert manifest.id == "core.master"
    assert manifest.system_role == "orchestrator"
    assert manifest.department == "orchestration"
    assert manifest.permissions.fs_write == []
    assert manifest.tools.allowed == []
    assert manifest.skills.allowed == []
    identity = manifest.to_agent_identity()
    assert isinstance(identity, AgentIdentity)
    assert identity.agent_id == "core.master"
    assert identity.system_role == "orchestrator"


def test_02_valid_worker_manifest_on_disk_and_templates() -> None:
    """2. Load and validate official Worker manifest plus standard Agent/Department templates."""
    worker = load_agent_manifest_file(
        AGENTS_ROOT / "roles" / "it_artifact_worker" / "agent.yaml",
        allowed_root=AGENTS_ROOT,
        enforce_path_convention=True,
    )
    assert worker.id == "it.artifact_worker"
    assert worker.system_role == "worker"
    assert worker.department == "it"
    assert worker.capabilities.skills == ["workspace_artifact_authoring"]

    # Validate reference templates under docs/templates/ as well.
    tmpl_agent = load_agent_manifest_file(
        REPO_ROOT / "docs" / "templates" / "agent" / "agent.yaml",
        allowed_root=REPO_ROOT / "docs" / "templates",
        enforce_path_convention=False,
    )
    assert tmpl_agent.id == "it.example_worker"

    tmpl_dept = load_department_manifest_file(
        REPO_ROOT / "docs" / "templates" / "department" / "department.yaml",
        allowed_root=REPO_ROOT / "docs" / "templates",
    )
    assert tmpl_dept.department_id == "example_dept"
    assert tmpl_dept.status == "INACTIVE"
    assert set(DEFAULT_DEPARTMENTS) == {
        "hr",
        "resources",
        "service",
        "marketing",
        "it",
        "reception",
        "process",
        "qa",
        "vault",
    }


def test_03_04_05_identity_master_impersonation_and_department_invariants() -> None:
    """3-5. Reject invalid Agent IDs, Worker spoofing Master, and Master using wrong department."""
    # 3. Invalid Agent ID
    for bad_id in ("it-worker", "single_token", "UPPER.Case", "it.worker.extra", ""):
        payload = _base_worker_payload()
        payload["id"] = bad_id
        with pytest.raises((ValidationError, ValueError)):
            validate_agent_manifest(payload)

    # 4. Worker spoofing Master system_role="orchestrator" or department="orchestration"
    spoof_role = _base_worker_payload()
    spoof_role["system_role"] = "orchestrator"
    with pytest.raises(ValidationError, match="core.master"):
        validate_agent_manifest(spoof_role)

    spoof_dept = _base_worker_payload()
    spoof_dept["department"] = "orchestration"
    with pytest.raises(ValidationError, match="core.master"):
        validate_agent_manifest(spoof_dept)

    # 5. Master using wrong department or declaring fs_write
    bad_master_dept = _base_worker_payload()
    bad_master_dept["id"] = "core.master"
    bad_master_dept["system_role"] = "orchestrator"
    bad_master_dept["department"] = "it"
    bad_master_dept["permissions"]["fs_write"] = []
    with pytest.raises(ValidationError, match="orchestration"):
        validate_agent_manifest(bad_master_dept)

    bad_master_write = _base_worker_payload()
    bad_master_write["id"] = "core.master"
    bad_master_write["system_role"] = "orchestrator"
    bad_master_write["department"] = "orchestration"
    with pytest.raises(ValidationError, match="fs_write"):
        validate_agent_manifest(bad_master_write)


def test_06_07_08_capabilities_unknown_department_and_tool_ids() -> None:
    """6-8. Reject invalid capabilities, unknown/provider departments, and invalid Tool IDs."""
    # 6. Invalid capability
    bad_cap = _base_worker_payload()
    bad_cap["capabilities"]["skills"] = ["bad-capability-tag"]
    with pytest.raises(ValidationError):
        validate_agent_manifest(bad_cap)

    dup_cap = _base_worker_payload()
    dup_cap["capabilities"]["skills"] = ["git_diff_analysis", "git_diff_analysis"]
    with pytest.raises(ValidationError, match="duplicate"):
        validate_agent_manifest(dup_cap)

    # 7. Unknown department & reserved AI provider name used as department
    unk_dept = _base_worker_payload()
    unk_dept["department"] = "finance_rogue"
    with pytest.raises(ValueError, match="Unknown or invalid worker department"):
        validate_agent_manifest(unk_dept)

    prov_dept = _base_worker_payload()
    prov_dept["department"] = "openai"
    with pytest.raises(ValidationError, match="Provider"):
        validate_agent_manifest(prov_dept)
    assert is_valid_worker_department("openai") is False
    assert is_valid_worker_department("orchestration") is False
    assert is_valid_worker_department("it") is True

    with pytest.raises(ValidationError, match="orchestration"):
        DepartmentManifest.model_validate(
            {
                "spec_version": "1.0.0",
                "department_id": "orchestration",
                "name": "Bad Dept",
                "description": "Invalid",
                "purpose": "Invalid",
                "responsibilities": ["None"],
                "supported_capabilities": ["cap_a"],
                "default_tools": [],
                "status": "ACTIVE",
                "visual_metadata": {
                    "color_code": "#2563EB",
                    "icon": "folder",
                    "zone_id": "zone_bad",
                },
            }
        )

    # 8. Invalid Tool IDs & unregistered tools check
    for bad_tool in ("tools.*", "files.read", "tools.files.READ", "tools.files"):
        bad_tools_payload = _base_worker_payload()
        bad_tools_payload["tools"]["allowed"] = [bad_tool]
        with pytest.raises(ValidationError):
            validate_agent_manifest(bad_tools_payload)

    with pytest.raises(ValueError, match="unregistered Tool IDs"):
        validate_agent_manifest(
            _base_worker_payload(),
            registered_tools=["tools.files.read"],
        )


def test_09_10_unsafe_filesystem_scopes_and_raw_api_keys() -> None:
    """9-10. Reject unsafe filesystem write/read patterns and raw credentials anywhere in Manifest."""
    # 9. Unsafe filesystem scopes
    unsafe_read_cases = [
        "../secret.txt",
        "/etc/passwd",
        "C:\\Windows\\System32\\config",
        "*",
        "**/*",
        "workspace/.env",
        "secrets/keys.json",
        "certs/private.pem",
        "workspace/{unknown_var}/out",
    ]
    for scope in unsafe_read_cases:
        p = _base_worker_payload()
        p["permissions"]["fs_read"] = [scope]
        with pytest.raises(ValidationError):
            validate_agent_manifest(p)

    unsafe_write_cases = [
        "workspace/*",
        "workspace/**",
        "core/tasks/*",
        "agents/master/*",
        "AGENTS.md",
        ".git/config",
        "storage/db/*",
        "workspace/{task_id}/.env",
    ]
    for scope in unsafe_write_cases:
        p = _base_worker_payload()
        p["permissions"]["fs_write"] = [scope]
        with pytest.raises(ValidationError):
            validate_agent_manifest(p)

    # 10. Raw API keys or credential keys inside Engine parameters or text fields
    secret_param = _base_worker_payload()
    secret_param["engine"]["parameters"] = {"api_key": "sk-testsecretvalue1234567890"}
    with pytest.raises(ValidationError):
        validate_agent_manifest(secret_param)

    embedded_secret = _base_worker_payload()
    embedded_secret["description"] = "Worker initialized with AIzaSyDummyRawGoogleApiKey1234567890"
    with pytest.raises(ValidationError):
        validate_agent_manifest(embedded_secret)

    token_param = _base_worker_payload()
    token_param["engine"]["parameters"] = {"custom_token": "anything"}
    with pytest.raises(ValidationError):
        validate_agent_manifest(token_param)


def test_11_12_13_16_routing_fallback_memory_limits_and_unknown_fields() -> None:
    """11-13, 16. Reject invalid routing profiles, fallback policies, memory scopes, limits, and extra fields."""
    # 11. Invalid routing_profile (must be snake_case, no uppercase/hyphens/secrets)
    bad_routing = _base_worker_payload()
    bad_routing["engine"]["routing_profile"] = "Bad-Profile!"
    with pytest.raises(ValidationError):
        validate_agent_manifest(bad_routing)

    # Known provider mismatch fails when known_providers is configured
    with pytest.raises(ValueError, match="Unknown engine.provider_id"):
        validate_agent_manifest(_base_worker_payload(), known_providers=["ollama"])

    # 12. Invalid fallback_policy
    bad_fb = _base_worker_payload()
    bad_fb["engine"]["fallback_policy"] = "auto_upgrade_expensive_tier"
    with pytest.raises(ValidationError):
        validate_agent_manifest(bad_fb)

    # 13. Invalid memory.scopes (missing 'private' or invalid scope)
    no_priv = _base_worker_payload()
    no_priv["memory"]["scopes"] = ["project"]
    with pytest.raises(ValidationError, match="private"):
        validate_agent_manifest(no_priv)

    bad_mem = _base_worker_payload()
    bad_mem["memory"]["scopes"] = ["private", "host_root"]
    with pytest.raises(ValidationError):
        validate_agent_manifest(bad_mem)

    # Limits out of bounds
    bad_limits = _base_worker_payload()
    bad_limits["limits"]["timeout_seconds"] = 999999
    with pytest.raises(ValidationError):
        validate_agent_manifest(bad_limits)

    # 16. Unknown extra fields forbidden (including premature 3D UI fields)
    extra_top = _base_worker_payload()
    extra_top["avatar_3d_mesh"] = "robot.glb"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        validate_agent_manifest(extra_top)


def test_14_15_yaml_security_duplicate_keys_unsafe_tags_size_and_symlink_escape(
    tmp_path: Path,
) -> None:
    """14-15. Block duplicate YAML keys, unsafe object tags, oversized files, and symlink/junction escapes."""
    allowed_dir = tmp_path / "agents"
    outside_dir = tmp_path / "outside"
    (allowed_dir / "roles" / "it_code_reviewer").mkdir(parents=True)
    outside_dir.mkdir()

    # 14. Duplicate key detection at root and nested level
    dup_yaml = allowed_dir / "roles" / "it_code_reviewer" / "agent.yaml"
    dup_yaml.write_text(
        'spec_version: "1.0.0"\nid: "it.code_reviewer"\nid: "it.spoofed"\n',
        encoding="utf-8",
    )
    with pytest.raises(ManifestLoadError) as dup_err:
        load_agent_manifest_file(dup_yaml, allowed_root=allowed_dir)
    assert dup_err.value.error_code == "DUPLICATE_YAML_KEY"

    # 15. Unsafe YAML tag (python/object/apply execution vector)
    unsafe_yaml = allowed_dir / "roles" / "it_code_reviewer" / "unsafe.yaml"
    unsafe_yaml.write_text(
        'spec_version: !!python/object/apply:os.system ["echo pwned"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ManifestLoadError) as tag_err:
        load_agent_manifest_file(
            unsafe_yaml, allowed_root=allowed_dir, enforce_path_convention=False
        )
    assert tag_err.value.error_code == "UNSAFE_YAML_TAG"

    # File too large rejected fail-closed
    huge_yaml = allowed_dir / "roles" / "it_code_reviewer" / "huge.yaml"
    huge_yaml.write_text("x: " + ("a" * 512), encoding="utf-8")
    with pytest.raises(ManifestLoadError) as size_err:
        load_agent_manifest_file(
            huge_yaml,
            allowed_root=allowed_dir,
            enforce_path_convention=False,
            max_bytes=128,
        )
    assert size_err.value.error_code == "FILE_TOO_LARGE"

    # Directory name vs Manifest ID mismatch rejected when convention enforced
    wrong_dir = allowed_dir / "roles" / "wrong_slug"
    wrong_dir.mkdir(parents=True)
    wrong_file = wrong_dir / "agent.yaml"

    wrong_file.write_text(yaml.safe_dump(_base_worker_payload()), encoding="utf-8")
    with pytest.raises(ManifestLoadError) as mismatch_err:
        load_agent_manifest_file(wrong_file, allowed_root=allowed_dir, enforce_path_convention=True)
    assert mismatch_err.value.error_code == "PATH_ID_MISMATCH"

    # Traversal and real symlink / NTFS junction escape blocked
    with pytest.raises(ManifestLoadError) as trav_err:
        load_agent_manifest_file("../outside/evil.yaml", allowed_root=allowed_dir)
    assert trav_err.value.error_code == "PATH_OUTSIDE_ALLOWED_ROOT"

    escaped_dir = outside_dir / "evil_role"
    escaped_dir.mkdir()
    (escaped_dir / "agent.yaml").write_text(yaml.safe_dump(_base_worker_payload()), encoding="utf-8")
    link_dir = allowed_dir / "roles" / "escaped_role"
    link_created = False
    try:
        link_dir.symlink_to(escaped_dir, target_is_directory=True)
        link_created = True
    except OSError:
        if sys.platform == "win32":
            proc = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link_dir), str(escaped_dir)],
                capture_output=True,
                text=True,
                check=False,
            )
            link_created = proc.returncode == 0
    assert link_created, "Expected real symlink or NTFS junction creation in tmp_path"
    with pytest.raises(ManifestLoadError) as sym_err:
        load_agent_manifest_file(link_dir / "agent.yaml", allowed_root=allowed_dir)
    assert sym_err.value.error_code == "PATH_OUTSIDE_ALLOWED_ROOT"


def test_17_18_19_20_roundtrip_mapping_no_self_privilege_and_no_engine_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """17-20. Verify JSON round-trip, AgentIdentity/Spike mapping, zero self-granted rights, and zero Engine calls."""
    # 20. Forbid subprocess invocation during manifest loading & validation.
    def _fail_subprocess(*args: object, **kwargs: object) -> None:
        raise AssertionError("Loading or validating a Manifest must never start any CLI/Engine process!")

    monkeypatch.setattr(subprocess, "Popen", _fail_subprocess)
    monkeypatch.setattr(subprocess, "run", _fail_subprocess)

    master = load_agent_manifest_file(AGENTS_ROOT / "master" / "agent.yaml", allowed_root=AGENTS_ROOT)
    worker = load_agent_manifest_file(
        AGENTS_ROOT / "roles" / "it_artifact_worker" / "agent.yaml", allowed_root=AGENTS_ROOT
    )

    # 17. Canonical JSON round-trip determinism
    worker_rt = AgentManifest.model_validate_json(worker.canonical_json())
    assert worker_rt == worker
    assert worker_rt.canonical_sha256() == worker.canonical_sha256()

    # 18. Mapping to core.contracts.agent.AgentIdentity & SpikeAgentSpec
    w_ident = worker.to_agent_identity()
    assert w_ident.agent_id == "it.artifact_worker"
    assert w_ident.department == "it"
    assert w_ident.capability_ids == ["workspace_artifact_authoring"]
    assert w_ident.allowed_connections[0].credential_ref is None

    spike_master = manifest_to_spike_agent_spec(
        master, allowed_handoff_targets=[worker.id]
    )
    assert spike_master.execution_mode == "plan"
    assert spike_master.allow_workspace_write is False

    spike_worker = manifest_to_spike_agent_spec(worker)
    assert spike_worker.execution_mode == "accept-edits"
    assert spike_worker.allow_workspace_write is True

    # 19. Manifest declaration never bypasses PolicyEvaluator Governance checks.
    evaluator = PolicyEvaluator()
    ts = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)
    claimed_worker_ctx = TrustedAuthContext(
        actor_id=worker.id,
        actor_kind="worker_agent",
        auth_source="application_boundary",
        authenticated=True,
        allowed_actions=[],
        allowed_tools=[],
    )
    for forbidden_action in ("approval.decide", "settings.update", "agent.assign", "files.delete"):
        dec = evaluator.evaluate(
            PolicyRequest(
                auth_context=claimed_worker_ctx,
                action=forbidden_action,
                resource="system:control",
                workspace_root="." if forbidden_action == "files.delete" else None,
                target_path="workspace/tmp.txt" if forbidden_action == "files.delete" else None,
                timestamp=ts,
            )
        )
        assert dec.decision != "ALLOW"
