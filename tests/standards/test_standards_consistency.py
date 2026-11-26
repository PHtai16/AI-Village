"""Automated consistency and syntax checks for Phase 01B standards and templates."""

from pathlib import Path
import re
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

SPEC_FILES = [
    REPO_ROOT / "docs" / "standards" / "AGENT_SPEC.md",
    REPO_ROOT / "docs" / "standards" / "DEPARTMENT_SPEC.md",
    REPO_ROOT / "docs" / "standards" / "SKILL_SPEC.md",
    REPO_ROOT / "docs" / "standards" / "RULE_SPEC.md",
    REPO_ROOT / "docs" / "standards" / "TOOL_SPEC.md",
    REPO_ROOT / "docs" / "architecture" / "SYSTEM_BLUEPRINT.md",
]

TEMPLATE_AGENT = REPO_ROOT / "docs" / "templates" / "agent" / "agent.yaml"
TEMPLATE_DEPT = REPO_ROOT / "docs" / "templates" / "department" / "department.yaml"
TEMPLATE_SKILL = REPO_ROOT / "docs" / "templates" / "skill" / "SKILL.md"

TOOL_ID_RE = re.compile(r"^tools\.[a-z0-9_]+\.[a-z0-9_]+$")
ACTION_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
SKILL_ID_RE = re.compile(r"^[a-z0-9_]+$")
AGENT_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
DEPT_ID_RE = re.compile(r"^[a-z0-9_]+$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

SECRET_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z\-_]{35}"),
    re.compile(r"ghp_[0-9A-Za-z]{36}"),
    re.compile(r"sk-[0-9A-Za-z]{48}"),
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]

FORBIDDEN_ENGINE_SECRET_KEYS = {
    "api_key",
    "raw_key",
    "secret",
    "token",
    "password",
    "credential",
}
VALID_FALLBACK_POLICIES = {"none", "same_tier_allowed", "operator_approved_only"}
OPTIONAL_ENGINE_ID_FIELDS = ("provider_id", "adapter_id", "connection_id", "routing_profile")


def _validate_engine_config(engine: dict[str, Any]) -> None:
    """Validate Agent Engine configuration fields and forbid raw credential keys."""
    assert SKILL_ID_RE.match(engine["default_type"]), "default_type must be snake_case identifier"
    assert engine["model_tier"] in {"pro", "flash", "code"}
    for key in engine:
        assert key.lower() not in FORBIDDEN_ENGINE_SECRET_KEYS, (
            f"Agent engine config must never store secret key '{key}'"
        )
    for field in OPTIONAL_ENGINE_ID_FIELDS:
        if field in engine and engine[field] is not None:
            assert SKILL_ID_RE.match(engine[field]), f"Invalid {field}: {engine[field]}"
    if "fallback_policy" in engine and engine["fallback_policy"] is not None:
        assert engine["fallback_policy"] in VALID_FALLBACK_POLICIES


def extract_fenced_blocks(markdown_text: str, language: str) -> list[str]:
    """Extract fenced code blocks for a given language from markdown text."""
    pattern = re.compile(rf"```{language}\s*\n(.*?)\n```", re.DOTALL)
    return pattern.findall(markdown_text)


def parse_frontmatter(markdown_text: str) -> dict[str, Any]:
    """Parse YAML frontmatter enclosed between leading --- delimiters."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", markdown_text, re.DOTALL)
    if not match:
        raise ValueError("Missing YAML frontmatter block")
    loaded = yaml.safe_load(match.group(1))
    if not isinstance(loaded, dict):
        raise ValueError("Frontmatter must deserialize to a mapping")
    return loaded


def count_backtick_fences(markdown_text: str) -> int:
    """Count top-level triple-backtick fence lines outside nested markdown blocks."""
    count = 0
    for line in markdown_text.splitlines():
        if line.strip().startswith("```"):
            count += 1
    return count


@pytest.fixture
def parsed_agent_template() -> dict[str, Any]:
    """Load and parse the standard agent YAML template."""
    with TEMPLATE_AGENT.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    assert isinstance(data, dict)
    return data


@pytest.fixture
def parsed_department_template() -> dict[str, Any]:
    """Load and parse the standard department YAML template."""
    with TEMPLATE_DEPT.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    assert isinstance(data, dict)
    return data


@pytest.fixture
def parsed_skill_template() -> dict[str, Any]:
    """Load and parse the standard skill SKILL.md template frontmatter."""
    with TEMPLATE_SKILL.open("r", encoding="utf-8") as handle:
        text = handle.read()
    return parse_frontmatter(text)


def test_required_files_exist() -> None:
    """Verify all Phase 01B specification documents and templates exist."""
    for path in [*SPEC_FILES, TEMPLATE_AGENT, TEMPLATE_DEPT, TEMPLATE_SKILL]:
        assert path.is_file(), f"Missing required file: {path}"


def test_markdown_fences_balanced_and_no_secrets() -> None:
    """Ensure every markdown document has even fence counts and no secret tokens."""
    for path in [*SPEC_FILES, TEMPLATE_SKILL]:
        with path.open("r", encoding="utf-8") as handle:
            content = handle.read()
        fence_count = count_backtick_fences(content)
        assert fence_count % 2 == 0, f"Unclosed code fence in {path.name}: {fence_count}"
        for pattern in SECRET_PATTERNS:
            assert not pattern.search(content), f"Potential secret leaked in {path.name}"


def test_agent_template_contract(parsed_agent_template: dict[str, Any]) -> None:
    """Validate agent template fields, types, enums, and cross-spec ID formats."""
    required_keys = {
        "spec_version",
        "id",
        "name",
        "role",
        "system_role",
        "department",
        "description",
        "mission",
        "capabilities",
        "engine",
        "memory",
        "skills",
        "tools",
        "permissions",
        "limits",
    }
    assert required_keys.issubset(parsed_agent_template.keys())
    assert AGENT_ID_RE.match(parsed_agent_template["id"])
    assert parsed_agent_template["system_role"] == "worker"
    assert parsed_agent_template["department"] != "orchestration"
    _validate_engine_config(parsed_agent_template["engine"])
    for skill_id in parsed_agent_template["skills"]["allowed"]:
        assert SKILL_ID_RE.match(skill_id), f"Invalid skill_id: {skill_id}"
    for tool_id in parsed_agent_template["tools"]["allowed"]:
        assert TOOL_ID_RE.match(tool_id), f"Invalid tool_id: {tool_id}"
    for action_id in parsed_agent_template["permissions"]["requires_approval"]:
        assert ACTION_ID_RE.match(action_id), f"Invalid action_id: {action_id}"
        assert not action_id.startswith("tools."), "requires_approval must use Action ID"


def test_department_template_contract(parsed_department_template: dict[str, Any]) -> None:
    """Validate department template fields, status, and tool ID format."""
    required_keys = {
        "spec_version",
        "department_id",
        "name",
        "description",
        "purpose",
        "responsibilities",
        "supported_capabilities",
        "default_tools",
        "status",
        "visual_metadata",
    }
    assert required_keys.issubset(parsed_department_template.keys())
    assert DEPT_ID_RE.match(parsed_department_template["department_id"])
    assert parsed_department_template["status"] == "INACTIVE"
    for cap in parsed_department_template["supported_capabilities"]:
        assert SKILL_ID_RE.match(cap), f"Invalid capability format: {cap}"
    for tool_id in parsed_department_template["default_tools"]:
        assert TOOL_ID_RE.match(tool_id), f"Invalid default_tool ID: {tool_id}"


def test_skill_template_contract(parsed_skill_template: dict[str, Any]) -> None:
    """Validate skill template frontmatter, non-approved status, and sections."""
    required_keys = {
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
    }
    assert required_keys.issubset(parsed_skill_template.keys())
    assert SKILL_ID_RE.match(parsed_skill_template["id"])
    assert SEMVER_RE.match(parsed_skill_template["version"])
    assert parsed_skill_template["status"] != "APPROVED"
    for tool_id in parsed_skill_template["requirements"]["tools"]:
        assert TOOL_ID_RE.match(tool_id), f"Invalid tool_id in skill template: {tool_id}"

    with TEMPLATE_SKILL.open("r", encoding="utf-8") as handle:
        body = handle.read()
    for heading in [
        "Triggers",
        "Anti-triggers",
        "Inputs",
        "Outputs",
        "Execution Steps",
        "Permissions",
        "Definition of Done",
        "Evidence",
        "Error Handling",
    ]:
        assert heading in body, f"Missing contract section '{heading}' in SKILL.md template"


def test_spec_yaml_examples_valid_and_consistent() -> None:
    """Parse and validate every YAML and Skill Markdown example across all 5 specs."""
    agent_text = (REPO_ROOT / "docs" / "standards" / "AGENT_SPEC.md").read_text(encoding="utf-8")
    agent_blocks = extract_fenced_blocks(agent_text, "yaml")
    assert len(agent_blocks) == 1
    agent_ex = yaml.safe_load(agent_blocks[0])
    assert agent_ex["system_role"] == "worker"
    _validate_engine_config(agent_ex["engine"])
    for skill_id in agent_ex["skills"]["allowed"]:
        assert SKILL_ID_RE.match(skill_id)
    for tool_id in agent_ex["tools"]["allowed"]:
        assert TOOL_ID_RE.match(tool_id)
    for action_id in agent_ex["permissions"]["requires_approval"]:
        assert ACTION_ID_RE.match(action_id) and not action_id.startswith("tools.")

    dept_text = (REPO_ROOT / "docs" / "standards" / "DEPARTMENT_SPEC.md").read_text(encoding="utf-8")
    dept_blocks = extract_fenced_blocks(dept_text, "yaml")
    assert len(dept_blocks) == 1
    dept_ex = yaml.safe_load(dept_blocks[0])
    assert DEPT_ID_RE.match(dept_ex["department_id"])
    for tool_id in dept_ex["default_tools"]:
        assert TOOL_ID_RE.match(tool_id)

    skill_text = (REPO_ROOT / "docs" / "standards" / "SKILL_SPEC.md").read_text(encoding="utf-8")
    skill_blocks = extract_fenced_blocks(skill_text, "markdown")
    assert len(skill_blocks) == 2
    for block in skill_blocks:
        fm = parse_frontmatter(block)
        assert SKILL_ID_RE.match(fm["id"])
        assert fm["status"] == "DRAFT", "Design example skills must not claim APPROVED"
        for tool_id in fm["requirements"]["tools"]:
            assert TOOL_ID_RE.match(tool_id)

    rule_text = (REPO_ROOT / "docs" / "standards" / "RULE_SPEC.md").read_text(encoding="utf-8")
    rule_blocks = extract_fenced_blocks(rule_text, "yaml")
    assert len(rule_blocks) == 3
    for block in rule_blocks:
        rule_ex = yaml.safe_load(block)
        assert rule_ex["effect"] in {"ALLOW", "DENY", "REQUIRE_APPROVAL", "AUDIT_ONLY"}
        assert rule_ex["status"] == "DRAFT", "Design example rules must not claim ACTIVE"
        if rule_ex["effect"] == "REQUIRE_APPROVAL":
            assert rule_ex["approval_required"] is True
        for action_id in rule_ex["actions"]:
            assert ACTION_ID_RE.match(action_id)

    tool_text = (REPO_ROOT / "docs" / "standards" / "TOOL_SPEC.md").read_text(encoding="utf-8")
    tool_blocks = extract_fenced_blocks(tool_text, "yaml")
    assert len(tool_blocks) == 3
    for block in tool_blocks:
        tool_ex = yaml.safe_load(block)
        assert TOOL_ID_RE.match(tool_ex["tool_id"])
        assert tool_ex["status"] == "DRAFT", "Design example tools must not claim AVAILABLE"
        for action_id in tool_ex["permissions_required"]:
            assert ACTION_ID_RE.match(action_id)


def test_vision_multi_key_example_and_approval_status() -> None:
    """Validate two-Gemini-connection YAML example and valid vision approval metadata."""
    vision_path = REPO_ROOT / "docs" / "architecture" / "vision.md"
    vision_text = vision_path.read_text(encoding="utf-8")
    assert "APPROVED" in vision_text or "PENDING_USER_APPROVAL" in vision_text
    if "Trạng thái phê duyệt tài liệu `vision.md`:** `APPROVED`" in vision_text:
        assert "Oke duyệt" in vision_text

    yaml_blocks = extract_fenced_blocks(vision_text, "yaml")
    assert len(yaml_blocks) == 1
    cfg = yaml.safe_load(yaml_blocks[0])
    assert cfg["provider"] == "google_gemini"
    connections = cfg["connections"]
    assert len(connections) == 2
    ids = {conn["connection_id"] for conn in connections}
    refs = {conn["credential_ref"] for conn in connections}
    assert ids == {"gemini_primary", "gemini_secondary"}
    assert len(refs) == 2
    for conn in connections:
        for forbidden in FORBIDDEN_ENGINE_SECRET_KEYS:
            assert forbidden not in conn


def test_mission_control_ux_and_roadmap_consistency() -> None:
    """Verify Mission Control 18 components, Stage A completion, and unimplemented Phase 02-17."""
    mc_path = REPO_ROOT / "docs" / "architecture" / "MISSION_CONTROL_UX.md"
    assert mc_path.is_file()
    mc_text = mc_path.read_text(encoding="utf-8")

    expected_components = [
        "mc.task_checklist",
        "mc.kanban",
        "mc.operations_monitor",
        "mc.usage_cost",
        "mc.task_timing",
        "mc.notifications",
        "mc.action_inbox",
        "mc.scheduler",
        "mc.meeting_room",
        "mc.qa_station",
        "mc.artifact_cabinet",
        "mc.comparison_board",
        "mc.activity_journal",
        "mc.engine_status",
        "mc.provider_manager",
        "mc.trash_restore",
        "mc.global_search",
        "mc.settings_governance",
        "mc.master_desk",
    ]
    for comp_id in expected_components:
        assert comp_id in mc_text, f"Missing Mission Control component: {comp_id}"

    progress_text = (REPO_ROOT / "docs" / "PROGRESS.md").read_text(encoding="utf-8")
    assert "PHASE 01C" not in progress_text
    assert "- [x] **P00.01" in progress_text
    assert "**Gate A (`PASS`):**" in progress_text
    assert "MISSION_CONTROL_UX.md" in (
        REPO_ROOT / "docs" / "architecture" / "SYSTEM_BLUEPRINT.md"
    ).read_text(encoding="utf-8")

    # Ensure no Phase 04-17 implementation item beyond P05.02 is prematurely marked DONE
    for line in progress_text.splitlines():
        match = re.match(r"^- \[x\] \*\*P(\d{2}\.\d{2})\b", line)
        if match and match.group(1) not in {"00.01", "00.02", "00.03", "00.04", "02.01", "02.02", "02.03", "02.04", "02.05", "02.06", "03.01", "03.02", "03.03", "04.01", "04.02", "04.03", "04.04", "04.05", "04.06", "04.07", "05.01", "05.02"} and not match.group(1).startswith("01."):
            raise AssertionError(f"Premature DONE marker in roadmap: {line}")
