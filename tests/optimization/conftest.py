"""Shared SQLite, Governance, and Runtime fixtures for tests/optimization/."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from tests.departments.conftest import DepartmentTestHarness, make_department_harness


@pytest.fixture
def dept_harness(tmp_path: Path) -> Iterator[DepartmentTestHarness]:
    """Provide initialized SQLite store, real tools, vaults, and registries for optimization tests."""
    harness = make_department_harness(tmp_path)
    try:
        yield harness
    finally:
        harness.store.close()
