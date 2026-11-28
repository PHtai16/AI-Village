"""Deterministic SKILL.md parser, validator, SHA-256 package hasher, and safe disk layout handler."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
from typing import Any

import yaml

from core.contracts.identifiers import ensure_not_secret, reject_secret_text, validate_safe_relative_path
from core.skills.models import (
    SENSITIVE_SKILL_TOOLS,
    SkillCandidateRequest,
    SkillExecutionContract,
    SkillFrontmatter,
    SkillIntegrityError,
    SkillIOField,
    SkillRiskLevel,
    SkillRiskProfile,
    SkillTestCaseSpec,
    SkillValidationError,
)

_FRONTMATTER_RE = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?(.*)$", re.DOTALL)
_FORBIDDEN_SCRIPT_RE = re.compile(
    r"(?:\.env\b|PRIVATE\s+KEY|AIza|sk-[0-9A-Za-z]|agents/|storage/|os\.environ\[)"
)
_SECTION_HEADINGS: list[tuple[str, str]] = [
    ("purpose_and_scope", "## 1. Mục đích và Phạm vi"),
    ("triggers", "## 2. Điều kiện kích hoạt (Triggers)"),
    ("anti_triggers", "## 3. Điều kiện cấm kích hoạt (Anti-triggers)"),
    ("inputs", "## 4. Đầu vào cam kết (Inputs)"),
    ("outputs", "## 5. Đầu ra cam kết (Outputs)"),
    ("execution_steps", "## 6. Quy trình thực thi từng bước (Execution Steps)"),
    ("security_and_permissions", "## 7. Ranh giới an toàn và Quyền hạn"),
    ("definition_of_done", "## 8. Tiêu chí hoàn thành (Definition of Done)"),
    ("evidence_requirements", "## 9. Yêu cầu bằng chứng kiểm chứng (Evidence)"),
    ("error_handling", "## 10. Xử lý sự cố và Khôi phục (Error Handling)"),
]


def assess_skill_risk_and_sensitivity(
    *,
    tools: list[str],
    dependencies: list[str],
    scripts: dict[str, str],
    risk_level: SkillRiskLevel,
    risks: list[str],
    modifies_sensitive_workflow: bool,
    prior_approved_tools: set[str] | None = None,
) -> SkillRiskProfile:
    """Compute honest sensitivity profile deciding whether Human Approval is mandatory."""
    reasons: list[str] = []
    has_scripts = bool(scripts)
    if has_scripts:
        reasons.append("Contains executable automation scripts in scripts/.")
    if dependencies:
        reasons.append(f"Declares external sandbox dependencies: {dependencies}.")

    tool_set = set(tools)
    added_tools = (tool_set - prior_approved_tools) if prior_approved_tools is not None else set()
    elevated_tools = sorted((tool_set & SENSITIVE_SKILL_TOOLS) | added_tools)
    perm_delta = bool(added_tools) or bool(tool_set & SENSITIVE_SKILL_TOOLS)
    if tool_set & SENSITIVE_SKILL_TOOLS:
        reasons.append(f"Requires sensitive tools: {sorted(tool_set & SENSITIVE_SKILL_TOOLS)}.")
    elif added_tools:
        reasons.append(f"Introduces new tool requirements: {sorted(added_tools)}.")

    if modifies_sensitive_workflow:
        reasons.append("Declared to alter a sensitive operational workflow.")
    if risk_level in {"medium", "high"}:
        reasons.append(f"Risk level classified as '{risk_level}'.")

    requires_human = bool(
        has_scripts
        or dependencies
        or (tool_set & SENSITIVE_SKILL_TOOLS)
        or bool(added_tools)
        or modifies_sensitive_workflow
        or risk_level in {"medium", "high"}
    )
    return SkillRiskProfile(
        risk_level=risk_level,
        risks=list(risks),
        modifies_permissions_or_tools=perm_delta,
        modifies_sensitive_workflow=modifies_sensitive_workflow,
        has_executable_scripts=has_scripts,
        requires_human_approval=requires_human,
        sensitivity_reasons=list(dict.fromkeys(reasons or elevated_tools)),
    )


def validate_skill_subpath(relpath: str, *, expected_dir: str) -> str:
    """Validate script/reference relative paths cleanly preventing escapes or hidden files."""
    try:
        cleaned = validate_safe_relative_path(relpath, f"{expected_dir}_path")
    except ValueError as exc:
        raise SkillValidationError("PATH_TRAVERSAL_BLOCKED", str(exc)) from exc
    parts = PurePosixPath(cleaned).parts
    if not parts or any(p.startswith(".") for p in parts):
        raise SkillValidationError(
            "INVALID_SKILL_SUBPATH",
            f"Skill package path '{relpath}' contains hidden or empty segments.",
        )
    if expected_dir == "scripts" and not cleaned.endswith((".py", ".sh", ".ps1")):
        raise SkillValidationError(
            "INVALID_SCRIPT_EXTENSION",
            f"Skill script '{relpath}' must use .py, .sh, or .ps1 extension.",
        )
    return cleaned


def scan_skill_text_security(content: str, *, label: str) -> str:
    """Reject secret-bearing text or privilege-escalation patterns in Skill definitions/scripts."""
    try:
        ensure_not_secret(content, label)
    except ValueError as exc:
        raise SkillValidationError("SECRET_LEAK_FORBIDDEN", str(exc)) from exc
    if _FORBIDDEN_SCRIPT_RE.search(content):
        raise SkillValidationError(
            "SKILL_SECURITY_VIOLATION",
            f"Skill content '{label}' references forbidden credential/production paths.",
        )
    return content


def _render_io_lines(fields: list[SkillIOField]) -> list[str]:
    return [
        f"- `{f.name}` ({f.data_type}, {'required' if f.required else 'optional'}): {f.description}"
        for f in fields
    ]


def render_skill_markdown(
    frontmatter: SkillFrontmatter,
    contract: SkillExecutionContract,
    *,
    risk_profile: SkillRiskProfile,
) -> str:
    """Render deterministic ``SKILL.md`` conforming to ``SKILL_SPEC.md`` §3.3 and §4."""
    fm_dict = frontmatter.model_dump(mode="json")
    yaml_text = yaml.safe_dump(fm_dict, sort_keys=False, allow_unicode=True).strip()
    sections: list[str] = [
        f"---\n{yaml_text}\n---",
        "",
        f"# Kỹ năng: {frontmatter.name}",
        "",
        f"## 1. Mục đích và Phạm vi\n{contract.purpose_and_scope}",
        "",
        "## 2. Điều kiện kích hoạt (Triggers)\n"
        + "\n".join(f"- {item}" for item in contract.triggers),
        "",
        "## 3. Điều kiện cấm kích hoạt (Anti-triggers)\n"
        + "\n".join(f"- {item}" for item in contract.anti_triggers),
        "",
        "## 4. Đầu vào cam kết (Inputs)\n" + "\n".join(_render_io_lines(contract.inputs)),
        "",
        "## 5. Đầu ra cam kết (Outputs)\n" + "\n".join(_render_io_lines(contract.outputs)),
        "",
        "## 6. Quy trình thực thi từng bước (Execution Steps)\n"
        + "\n".join(f"{idx}. {step}" for idx, step in enumerate(contract.execution_steps, start=1)),
        "",
        "## 7. Ranh giới an toàn và Quyền hạn\n"
        + "\n".join(f"- {item}" for item in contract.security_and_permissions)
        + f"\n- Mức rủi ro khai báo: `{risk_profile.risk_level}`",
        "",
        "## 8. Tiêu chí hoàn thành (Definition of Done)\n"
        + "\n".join(f"- {item}" for item in contract.definition_of_done),
        "",
        "## 9. Yêu cầu bằng chứng kiểm chứng (Evidence)\n"
        + "\n".join(f"- {item}" for item in contract.evidence_requirements),
        "",
        "## 10. Xử lý sự cố và Khôi phục (Error Handling)\n"
        + "\n".join(f"- {item}" for item in contract.error_handling),
        "",
    ]
    return "\n".join(sections)


def parse_and_validate_skill_markdown(raw_markdown: str) -> tuple[SkillFrontmatter, dict[str, str]]:
    """Parse ``SKILL.md`` frontmatter and verify all 10 required Execution Contract sections exist."""
    try:
        reject_secret_text(raw_markdown, "SKILL.md")
    except ValueError as exc:
        raise SkillValidationError("SECRET_LEAK_FORBIDDEN", str(exc)) from exc

    match = _FRONTMATTER_RE.match(raw_markdown)
    if match is None:
        raise SkillValidationError(
            "MISSING_FRONTMATTER",
            "SKILL.md must begin with valid YAML frontmatter delimited by '---'.",
        )
    yaml_raw, body = match.group(1), match.group(2)
    try:
        parsed_yaml = yaml.safe_load(yaml_raw)
    except yaml.YAMLError as exc:
        raise SkillValidationError("INVALID_FRONTMATTER_YAML", f"Invalid YAML frontmatter: {exc}") from exc
    if not isinstance(parsed_yaml, dict):
        raise SkillValidationError("INVALID_FRONTMATTER_TYPE", "YAML frontmatter must be a mapping.")

    try:
        frontmatter = SkillFrontmatter.model_validate(parsed_yaml)
    except Exception as exc:
        raise SkillValidationError("INVALID_FRONTMATTER_SCHEMA", f"Frontmatter schema invalid: {exc}") from exc

    section_bodies: dict[str, str] = {}
    for field_name, heading in _SECTION_HEADINGS:
        if heading not in body:
            raise SkillValidationError(
                "MISSING_EXECUTION_CONTRACT_SECTION",
                f"SKILL.md is missing required Execution Contract heading '{heading}'.",
            )
        section_bodies[field_name] = heading

    return frontmatter, section_bodies


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_directory_file_hashes(pkg_dir: Path) -> tuple[dict[str, str], str]:
    """Compute per-file SHA-256 map and canonical aggregate package SHA-256."""
    if not pkg_dir.is_dir():
        raise SkillIntegrityError(
            "SKILL_PACKAGE_DIR_MISSING",
            f"Skill package directory '{pkg_dir}' does not exist.",
        )
    hashes: dict[str, str] = {}
    for path in sorted(p for p in pkg_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(pkg_dir).as_posix()
        hashes[rel] = _sha256_bytes(path.read_bytes())
    if "SKILL.md" not in hashes:
        raise SkillIntegrityError("SKILL_MD_MISSING", f"Missing SKILL.md inside '{pkg_dir}'.")
    aggregate = hashlib.sha256(
        json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return hashes, aggregate


def write_candidate_package(
    skills_root: Path,
    *,
    request: SkillCandidateRequest,
    frontmatter: SkillFrontmatter,
    risk_profile: SkillRiskProfile,
) -> tuple[str, str, str, dict[str, str]]:
    """Materialize validated Skill candidate directory cleanly under ``skills_root/candidates/``."""
    resolved_root = skills_root.resolve()
    rel_pkg = f"candidates/{request.skill_id}/{request.version}"
    pkg_dir = (resolved_root / rel_pkg).resolve()
    if resolved_root not in pkg_dir.parents:
        raise SkillValidationError("PATH_TRAVERSAL_BLOCKED", "Invalid skill package destination path.")

    skill_md_text = render_skill_markdown(frontmatter, request.contract, risk_profile=risk_profile)
    parse_and_validate_skill_markdown(skill_md_text)

    if pkg_dir.exists():
        shutil.rmtree(pkg_dir)
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "tests").mkdir(exist_ok=True)

    skill_md_path = pkg_dir / "SKILL.md"
    skill_md_path.write_text(skill_md_text, encoding="utf-8")

    # Serialize fixtures deterministically into tests/fixtures.json.
    fixtures_payload = {
        "skill_id": request.skill_id,
        "version": request.version,
        "source_improvement_id": request.source_improvement_id,
        "test_cases": [tc.model_dump(mode="json") for tc in request.test_cases],
    }
    (pkg_dir / "tests" / "fixtures.json").write_text(
        json.dumps(fixtures_payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    for script_name, script_body in request.scripts.items():
        clean_name = validate_skill_subpath(script_name, expected_dir="scripts")
        scan_skill_text_security(script_body, label=f"scripts/{clean_name}")
        target = (pkg_dir / "scripts" / clean_name).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(script_body, encoding="utf-8")

    for ref_name, ref_body in request.references.items():
        clean_name = validate_skill_subpath(ref_name, expected_dir="references")
        try:
            reject_secret_text(ref_body, f"references/{clean_name}")
        except ValueError as exc:
            raise SkillValidationError("SECRET_LEAK_FORBIDDEN", str(exc)) from exc
        target = (pkg_dir / "references" / clean_name).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(ref_body, encoding="utf-8")

    file_hashes, package_sha256 = compute_directory_file_hashes(pkg_dir)
    manifest_sha256 = file_hashes["SKILL.md"]
    return rel_pkg, manifest_sha256, package_sha256, file_hashes


def promote_package_to_approved(
    skills_root: Path,
    *,
    skill_id: str,
    version: str,
    expected_package_sha256: str,
    frontmatter: SkillFrontmatter,
    contract: SkillExecutionContract,
    risk_profile: SkillRiskProfile,
) -> tuple[str, str, str, dict[str, str]]:
    """Copy verified candidate into immutable ``skills_root/approved/{skill_id}/{version}/``."""
    resolved_root = skills_root.resolve()
    cand_dir = (resolved_root / "candidates" / skill_id / version).resolve()
    _, cand_sha = compute_directory_file_hashes(cand_dir)
    if cand_sha != expected_package_sha256:
        raise SkillIntegrityError(
            "CANDIDATE_INTEGRITY_MISMATCH",
            (
                f"Candidate package '{skill_id}@{version}' failed integrity verification: "
                f"expected SHA {expected_package_sha256[:12]}, got {cand_sha[:12]}."
            ),
        )

    rel_approved = f"approved/{skill_id}/{version}"
    dest_dir = (resolved_root / rel_approved).resolve()
    if resolved_root not in dest_dir.parents:
        raise SkillValidationError("PATH_TRAVERSAL_BLOCKED", "Invalid approved destination path.")
    if dest_dir.exists():
        raise SkillIntegrityError(
            "APPROVED_VERSION_ALREADY_EXISTS",
            f"Immutable approved package '{rel_approved}' already exists on disk.",
        )

    shutil.copytree(cand_dir, dest_dir)
    # Write APPROVED frontmatter copy inside immutable approved/ directory AFTER verifying candidate integrity.
    approved_fm = frontmatter.model_copy(update={"status": "APPROVED"})
    (dest_dir / "SKILL.md").write_text(
        render_skill_markdown(approved_fm, contract, risk_profile=risk_profile),
        encoding="utf-8",
    )
    file_hashes, approved_sha = compute_directory_file_hashes(dest_dir)
    return rel_approved, file_hashes["SKILL.md"], approved_sha, file_hashes


def verify_approved_package_integrity(
    skills_root: Path,
    *,
    package_relpath: str,
    expected_package_sha256: str,
) -> str:
    """Verify approved package exists on disk, parses cleanly, and matches expected SHA-256."""
    resolved_root = skills_root.resolve()
    pkg_dir = (resolved_root / package_relpath).resolve()
    if resolved_root not in pkg_dir.parents and pkg_dir != resolved_root:
        raise SkillIntegrityError("PATH_TRAVERSAL_BLOCKED", "Approved package path escapes skills root.")
    _, actual_sha = compute_directory_file_hashes(pkg_dir)
    if actual_sha != expected_package_sha256:
        raise SkillIntegrityError(
            "APPROVED_INTEGRITY_MISMATCH",
            (
                f"Approved package at '{package_relpath}' was tampered or corrupted: "
                f"expected {expected_package_sha256[:12]}, got {actual_sha[:12]}."
            ),
        )
    skill_md_raw = (pkg_dir / "SKILL.md").read_text(encoding="utf-8")
    parse_and_validate_skill_markdown(skill_md_raw)
    return skill_md_raw
