"""Baseline structure, documentation, configuration, security, and UI scaffold checks for Stage A."""

import json
from pathlib import Path
import re
import subprocess
from typing import Any
from urllib.parse import unquote

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

TOP_LEVEL_DIRS = [
    "agents",
    "core",
    "engines",
    "tools",
    "skills",
    "storage",
    "api",
    "ui",
    "sandbox",
    "tests",
    "docs",
    "workspace",
    "output",
    ".runtime",
]

REQUIRED_ROOT_FILES = [
    "README.md",
    "PROGRESS.md",
    "AGENTS.md",
    ".gitignore",
    ".env.example",
]

REQUIRED_STAGE_A_DOCS = [
    "docs/README.md",
    "docs/PROGRESS.md",
    "docs/architecture/SYSTEM_BLUEPRINT.md",
    "docs/architecture/vision.md",
    "docs/architecture/boundaries.md",
    "docs/architecture/reuse_inventory.md",
    "docs/decisions/ADR-0001-temporary-technology-stack.md",
    "docs/reviews/PHASE_01B_AUDIT.md",
    "agents/README.md",
    "core/README.md",
    "engines/README.md",
    "tools/README.md",
    "skills/README.md",
    "storage/README.md",
    "api/README.md",
    "ui/README.md",
    "sandbox/README.md",
    "tests/README.md",
]

SECRET_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z\-_]{35}"),
    re.compile(r"ghp_[0-9A-Za-z]{36}"),
    re.compile(r"sk-[0-9A-Za-z]{48}"),
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]

LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


@pytest.fixture(scope="module")
def tracked_files() -> list[str]:
    """Return all files currently tracked or known to git status excluding ignored files."""
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


@pytest.fixture(scope="module")
def stage_a_markdown_files() -> list[Path]:
    """Collect all authored Stage A markdown files across root, docs, and technical directories."""
    candidates: list[Path] = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "PROGRESS.md",
        REPO_ROOT / "AGENTS.md",
    ]
    for folder in ["agents", "core", "engines", "tools", "skills", "storage", "api", "ui", "sandbox", "tests"]:
        readme_path = REPO_ROOT / folder / "README.md"
        if readme_path.is_file():
            candidates.append(readme_path)
    docs_dir = REPO_ROOT / "docs"
    for md_path in sorted(docs_dir.rglob("*.md")):
        candidates.append(md_path)
    return candidates


def _resolve_doc_link(source_file: Path, target: str) -> Path | None:
    """Resolve a local markdown link target to a repository Path, or None for external URLs."""
    cleaned = target.strip().split("#", 1)[0]
    if not cleaned or cleaned.startswith(("http://", "https://", "mailto:")):
        return None
    if cleaned.startswith("file:///"):
        local_str = unquote(cleaned.removeprefix("file:///"))
        return Path(local_str)
    return (source_file.parent / cleaned).resolve()


def test_top_level_directories_exist_with_clean_boundaries() -> None:
    """Verify all 14 top-level directories exist and contain no premature runtime code."""
    for folder in TOP_LEVEL_DIRS:
        dir_path = REPO_ROOT / folder
        assert dir_path.is_dir(), f"Missing top-level directory: {folder}"

    # Phase 02 authorizes adapter and contract code inside engines/ (antigravity/ and providers/).
    # Phase 03 authorizes core/__init__.py, core/orchestration/*.py, and core/runtime/*.py.
    # Phase 04 authorizes core/contracts/*.py (P04.01), core/tasks/*.py (P04.02),
    # core/events/*.py (P04.03), storage/db/*.py (P04.04), and storage/repositories/*.py (P04.05).
    no_code_dirs = ["agents", "tools", "skills", "api", "sandbox"]
    for folder in no_code_dirs:
        py_files = list((REPO_ROOT / folder).rglob("*.py"))
        js_files = list((REPO_ROOT / folder).rglob("*.js"))
        assert not py_files, f"Unexpected Python files in Stage A folder {folder}: {py_files}"
        assert not js_files, f"Unexpected JS files in Stage A folder {folder}: {js_files}"

    storage_root = REPO_ROOT / "storage"
    assert not list(storage_root.rglob("*.js")), "Unexpected JS files inside storage/."
    allowed_storage_subdirs = {storage_root / "db", storage_root / "repositories"}
    unexpected_storage_py = [
        p.relative_to(storage_root).as_posix()
        for p in storage_root.rglob("*.py")
        if p.relative_to(storage_root).as_posix() != "__init__.py"
        and p.parent not in allowed_storage_subdirs
    ]
    assert not unexpected_storage_py, (
        f"Unexpected Python modules inside storage/: {unexpected_storage_py}"
    )

    core_root = REPO_ROOT / "core"
    assert not list(core_root.rglob("*.js")), "Unexpected JS files inside core/."
    # Phase 05 authorizes core/agents/*.py (P05.01-P05.02) while agents/ stays YAML-only.
    allowed_core_subdirs = {
        core_root / "orchestration",
        core_root / "runtime",
        core_root / "contracts",
        core_root / "tasks",
        core_root / "events",
        core_root / "governance",
        core_root / "agents",
    }
    unexpected_core_py = [
        p.relative_to(core_root).as_posix()
        for p in core_root.rglob("*.py")
        if p.relative_to(core_root).as_posix() != "__init__.py"
        and p.parent not in allowed_core_subdirs
    ]
    assert not unexpected_core_py, f"Unexpected Python modules inside core/: {unexpected_core_py}"

    forbidden_db_dirs = [REPO_ROOT, storage_root, core_root]
    for check_dir in forbidden_db_dirs:
        if check_dir == REPO_ROOT:
            db_files = [
                p for p in REPO_ROOT.iterdir()
                if p.is_file() and p.suffix in {".db", ".sqlite", ".sqlite3"}
            ]
        else:
            db_files = [
                p for p in check_dir.rglob("*")
                if p.is_file() and p.suffix in {".db", ".sqlite", ".sqlite3"}
            ]
        assert not db_files, f"Database file found outside .runtime/: {db_files}"


def test_required_stage_a_files_and_markdown_integrity(stage_a_markdown_files: list[Path]) -> None:
    """Verify root files, Stage A documentation, balanced fences, and local markdown links."""
    for rel_path in [*REQUIRED_ROOT_FILES, *REQUIRED_STAGE_A_DOCS]:
        full_path = REPO_ROOT / rel_path
        assert full_path.is_file(), f"Missing required Stage A file: {rel_path}"

    for md_path in stage_a_markdown_files:
        with md_path.open("r", encoding="utf-8") as handle:
            content = handle.read()
        fence_count = sum(1 for line in content.splitlines() if line.strip().startswith("```"))
        assert fence_count % 2 == 0, f"Unclosed code fence in {md_path}"

        # Strip fenced code blocks before checking inline links
        prose = re.sub(r"```.*?```", "", content, flags=re.DOTALL)
        for match in LINK_RE.findall(prose):
            resolved = _resolve_doc_link(md_path, match)
            if resolved is not None:
                assert resolved.exists(), f"Broken link '{match}' in {md_path}"


def test_env_example_and_gitignore_security_baseline(tracked_files: list[str]) -> None:
    """Validate .env.example placeholders and ensure .gitignore protects private/runtime paths."""
    env_example = REPO_ROOT / ".env.example"
    with env_example.open("r", encoding="utf-8") as handle:
        lines = handle.readlines()

    defined_keys: set[str] = set()
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        assert "=" in line, f"Invalid .env.example line: {line}"
        key, value = line.split("=", 1)
        defined_keys.add(key.strip())
        if any(token in key.upper() for token in ("KEY", "SECRET", "TOKEN", "PASSWORD")):
            assert value.strip() in {"", "your_api_key_here", "placeholder"}, (
                f"Sensitive key '{key}' in .env.example must not contain a real value"
            )

    assert "AI_VILLAGE_DEFAULT_ENGINE" in defined_keys
    assert "AI_VILLAGE_DB_PATH" in defined_keys

    for path_to_ignore in [
        ".env",
        ".runtime/ai_village.db",
        "workspace/task_1/notes.md",
        "output/task_1/deliverable.md",
        "ui/node_modules/pkg/index.js",
        "ui/dist/index.html",
    ]:
        check = subprocess.run(
            ["git", "check-ignore", "-q", path_to_ignore],
            cwd=REPO_ROOT,
            check=False,
        )
        assert check.returncode == 0, f"Expected .gitignore to ignore '{path_to_ignore}'"

    for tracked in tracked_files:
        assert not tracked.startswith(".runtime/"), f".runtime/ file tracked in Git: {tracked}"
        assert tracked != ".env", ".env must never be tracked in Git"
        assert not tracked.endswith((".db", ".sqlite", ".sqlite3")), (
            f"Database file tracked in Git: {tracked}"
        )


def test_templates_not_duplicated_and_no_secret_leaks(stage_a_markdown_files: list[Path]) -> None:
    """Verify templates reside only in docs/templates/ and no secrets appear in repository text."""
    allowed_agent_yamls = {
        "master/agent.yaml",
        "roles/it_artifact_worker/agent.yaml",
    }
    found_agent_yamls = {
        p.relative_to(REPO_ROOT / "agents").as_posix()
        for p in (REPO_ROOT / "agents").rglob("*.yaml")
    }
    unexpected_agent_yamls = found_agent_yamls - allowed_agent_yamls
    assert not unexpected_agent_yamls, (
        f"Unexpected YAML manifests outside P05.01 allowlist in agents/: {unexpected_agent_yamls}"
    )
    assert not list((REPO_ROOT / "skills").rglob("SKILL.md"))

    files_to_scan = [
        *stage_a_markdown_files,
        *(REPO_ROOT / "agents").rglob("*.yaml"),
        REPO_ROOT / ".env.example",
        REPO_ROOT / ".gitignore",
    ]
    ui_src = REPO_ROOT / "ui" / "src"
    if ui_src.is_dir():
        files_to_scan.extend(p for p in ui_src.rglob("*") if p.is_file())

    for path in files_to_scan:
        with path.open("r", encoding="utf-8") as handle:
            content = handle.read()
        for pattern in SECRET_PATTERNS:
            assert not pattern.search(content), f"Potential secret pattern found in {path}"


def test_ui_minimal_scaffold_contract() -> None:
    """Verify minimal React + Vite scaffold in ui/ without 3D or fake runtime claims."""
    ui_dir = REPO_ROOT / "ui"
    pkg_path = ui_dir / "package.json"
    assert pkg_path.is_file()
    assert (ui_dir / "vite.config.js").is_file()
    assert (ui_dir / "index.html").is_file()
    assert (ui_dir / "src" / "main.jsx").is_file()
    app_path = ui_dir / "src" / "App.jsx"
    assert app_path.is_file()

    with pkg_path.open("r", encoding="utf-8") as handle:
        pkg_data: dict[str, Any] = json.load(handle)

    deps = {**pkg_data.get("dependencies", {}), **pkg_data.get("devDependencies", {})}
    assert "react" in deps
    assert "vite" in deps
    assert "three" not in deps
    assert "@react-three/fiber" not in deps
    assert "build" in pkg_data.get("scripts", {})

    with app_path.open("r", encoding="utf-8") as handle:
        app_text = handle.read()
    assert "Runtime chưa được kết nối" in app_text
