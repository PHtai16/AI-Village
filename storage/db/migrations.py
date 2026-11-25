"""Versioned, transactional SQLite schema migration runner."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import logging
from pathlib import Path
import re
import sqlite3
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from storage.db.connection import sqlite_transaction

logger = logging.getLogger(__name__)

DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATION_FILE_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

MigrationErrorCode = Literal[
    "INVALID_MIGRATION_FILE",
    "CHECKSUM_MISMATCH",
    "DOWNGRADE_FORBIDDEN",
    "SCHEMA_VERSION_AHEAD",
    "MIGRATION_EXECUTION_FAILED",
    "MISSING_MIGRATION_VERSION",
]


class MigrationError(RuntimeError):
    """Structured fail-closed exception raised during schema validation or migration."""

    def __init__(
        self,
        error_code: MigrationErrorCode,
        message: str,
        *,
        version: int | None = None,
        migration_name: str | None = None,
    ) -> None:
        """Initialize structured migration audit metadata."""
        super().__init__(message)
        self.error_code = error_code
        self.version = version
        self.migration_name = migration_name
        self.message = message


class MigrationSpec(BaseModel):
    """Discovered SQL migration script metadata with SHA-256 integrity digest."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=128)
    path: Path
    checksum: str = Field(min_length=64, max_length=64)
    sql: str


class MigrationRecord(BaseModel):
    """Applied migration entry recorded in SQLite ``schema_migrations``."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=128)
    checksum: str = Field(min_length=64, max_length=64)
    applied_at: str = Field(min_length=1, max_length=64)


class MigrationRunResult(BaseModel):
    """Structured audit summary returned by ``apply_migrations``."""

    model_config = ConfigDict(extra="forbid")

    previous_version: int = Field(ge=0)
    current_version: int = Field(ge=0)
    applied_versions: list[int] = Field(default_factory=list)
    skipped_versions: list[int] = Field(default_factory=list)


def split_sql_statements(sql: str) -> list[str]:
    """Split SQL script into executable statements without using ``executescript()``.

    Uses ``sqlite3.complete_statement()`` so multi-statement ``CREATE TRIGGER ... BEGIN ... END;``
    blocks stay intact while preserving explicit SQLite transaction boundaries.
    """
    statements: list[str] = []
    buffer: list[str] = []
    for line in sql.splitlines(keepends=True):
        stripped = line.strip()
        if not buffer and (not stripped or stripped.startswith("--")):
            continue
        buffer.append(line)
        candidate = "".join(buffer)
        if sqlite3.complete_statement(candidate):
            cleaned = candidate.strip()
            if cleaned and not all(
                not ln.strip() or ln.strip().startswith("--")
                for ln in cleaned.splitlines()
            ):
                statements.append(cleaned)
            buffer.clear()

    leftover_lines = [
        ln.strip() for ln in buffer if ln.strip() and not ln.strip().startswith("--")
    ]
    if leftover_lines:
        raise MigrationError(
            "INVALID_MIGRATION_FILE",
            f"Incomplete SQL statement found at end of migration: {leftover_lines[0][:120]}",
        )
    return statements


def discover_migrations(migrations_dir: Path | None = None) -> list[MigrationSpec]:
    """Load and validate versioned ``NNNN_*.sql`` migration scripts in ascending order."""
    directory = migrations_dir if migrations_dir is not None else DEFAULT_MIGRATIONS_DIR
    if not directory.is_dir():
        raise MigrationError(
            "MISSING_MIGRATION_VERSION",
            f"Migrations directory does not exist: {directory}",
        )

    specs: list[MigrationSpec] = []
    seen_versions: dict[int, Path] = {}
    for file_path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_FILE_RE.fullmatch(file_path.name)
        if match is None:
            raise MigrationError(
                "INVALID_MIGRATION_FILE",
                f"Migration filename '{file_path.name}' must match NNNN_snake_name.sql.",
                migration_name=file_path.name,
            )
        version = int(match.group(1))
        name = match.group(2)
        if version < 1:
            raise MigrationError(
                "INVALID_MIGRATION_FILE",
                f"Migration version must be >= 1, got {version} in '{file_path.name}'.",
                version=version,
                migration_name=file_path.name,
            )
        if version in seen_versions:
            raise MigrationError(
                "INVALID_MIGRATION_FILE",
                f"Duplicate migration version {version}: '{seen_versions[version].name}' and '{file_path.name}'.",
                version=version,
                migration_name=file_path.name,
            )
        seen_versions[version] = file_path

        sql_text = file_path.read_text(encoding="utf-8")
        if not sql_text.strip():
            raise MigrationError(
                "INVALID_MIGRATION_FILE",
                f"Migration file '{file_path.name}' is empty.",
                version=version,
                migration_name=file_path.name,
            )
        split_sql_statements(sql_text)
        checksum = hashlib.sha256(sql_text.encode("utf-8")).hexdigest()
        specs.append(
            MigrationSpec(
                version=version,
                name=name,
                path=file_path,
                checksum=checksum,
                sql=sql_text,
            )
        )

    specs.sort(key=lambda spec: spec.version)
    expected_version = 1
    for spec in specs:
        if spec.version != expected_version:
            raise MigrationError(
                "MISSING_MIGRATION_VERSION",
                f"Expected contiguous migration version {expected_version}, found {spec.version}.",
                version=spec.version,
                migration_name=spec.name,
            )
        expected_version += 1

    return specs


def ensure_migrations_table(conn: sqlite3.Connection) -> None:
    """Create the ``schema_migrations`` version table if missing."""
    with sqlite_transaction(conn, immediate=True):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY CHECK (version >= 1),
                name TEXT NOT NULL CHECK (length(trim(name)) > 0),
                checksum TEXT NOT NULL CHECK (length(checksum) = 64),
                applied_at TEXT NOT NULL CHECK (length(trim(applied_at)) > 0)
            );
            """
        )


def list_applied_migrations(conn: sqlite3.Connection) -> list[MigrationRecord]:
    """Return applied migrations ordered by version, or empty list if uninitialized."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations';"
    ).fetchone()
    if exists is None:
        return []
    rows = conn.execute(
        "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version ASC;"
    ).fetchall()
    return [
        MigrationRecord(
            version=int(row["version"]),
            name=str(row["name"]),
            checksum=str(row["checksum"]),
            applied_at=str(row["applied_at"]),
        )
        for row in rows
    ]


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied schema version, or ``0`` if no migrations exist."""
    applied = list_applied_migrations(conn)
    return applied[-1].version if applied else 0


def get_latest_migration_version(migrations_dir: Path | None = None) -> int:
    """Return the highest known migration version bundled on disk."""
    specs = discover_migrations(migrations_dir)
    return specs[-1].version if specs else 0


def verify_schema_compatibility(
    conn: sqlite3.Connection,
    *,
    expected_version: int | None = None,
    migrations_dir: Path | None = None,
) -> int:
    """Verify applied database migrations match code specs and reject mismatched schemas."""
    specs = discover_migrations(migrations_dir)
    spec_by_version = {spec.version: spec for spec in specs}
    max_known = specs[-1].version if specs else 0
    applied = list_applied_migrations(conn)
    current_version = applied[-1].version if applied else 0

    if current_version > max_known:
        raise MigrationError(
            "SCHEMA_VERSION_AHEAD",
            f"Database schema version {current_version} is newer than known migrations (max {max_known}).",
            version=current_version,
        )

    for record in applied:
        spec = spec_by_version.get(record.version)
        if spec is None:
            raise MigrationError(
                "MISSING_MIGRATION_VERSION",
                f"Applied migration version {record.version} ({record.name}) is missing from disk.",
                version=record.version,
                migration_name=record.name,
            )
        if spec.checksum != record.checksum or spec.name != record.name:
            raise MigrationError(
                "CHECKSUM_MISMATCH",
                (
                    f"Migration v{record.version} ({record.name}) differs from applied checksum; "
                    "refusing to mutate or bypass schema history."
                ),
                version=record.version,
                migration_name=record.name,
            )

    if expected_version is not None and current_version != expected_version:
        if current_version > expected_version:
            raise MigrationError(
                "SCHEMA_VERSION_AHEAD",
                f"Database schema version {current_version} exceeds expected version {expected_version}.",
                version=current_version,
            )
        raise MigrationError(
            "MISSING_MIGRATION_VERSION",
            f"Database schema version {current_version} does not match expected version {expected_version}.",
            version=current_version,
        )

    return current_version


def apply_migrations(
    conn: sqlite3.Connection,
    *,
    migrations_dir: Path | None = None,
    target_version: int | None = None,
) -> MigrationRunResult:
    """Apply pending SQLite migrations atomically in ascending version order."""
    specs = discover_migrations(migrations_dir)
    max_known = specs[-1].version if specs else 0

    if target_version is not None and target_version > max_known:
        raise MigrationError(
            "MISSING_MIGRATION_VERSION",
            f"Requested target_version {target_version} exceeds latest migration {max_known}.",
            version=target_version,
        )

    ensure_migrations_table(conn)
    previous_version = verify_schema_compatibility(conn, migrations_dir=migrations_dir)

    if target_version is not None and target_version < previous_version:
        raise MigrationError(
            "DOWNGRADE_FORBIDDEN",
            f"Refusing downgrade from schema version {previous_version} to {target_version}.",
            version=target_version,
        )

    applied_records = {rec.version: rec for rec in list_applied_migrations(conn)}
    applied_versions: list[int] = []
    skipped_versions: list[int] = []

    for spec in specs:
        if target_version is not None and spec.version > target_version:
            break
        if spec.version in applied_records:
            skipped_versions.append(spec.version)
            continue

        statements = split_sql_statements(spec.sql)
        applied_at = datetime.now(timezone.utc).isoformat()
        try:
            with sqlite_transaction(conn, immediate=True):
                for stmt in statements:
                    conn.execute(stmt)
                conn.execute(
                    """
                    INSERT INTO schema_migrations (version, name, checksum, applied_at)
                    VALUES (?, ?, ?, ?);
                    """,
                    (spec.version, spec.name, spec.checksum, applied_at),
                )
        except sqlite3.Error as exc:
            logger.error(
                "Migration v%d (%s) failed and rolled back cleanly: %s",
                spec.version,
                spec.name,
                exc,
            )
            raise MigrationError(
                "MIGRATION_EXECUTION_FAILED",
                f"Migration v{spec.version} ({spec.name}) failed and was rolled back: {exc}",
                version=spec.version,
                migration_name=spec.name,
            ) from exc

        applied_versions.append(spec.version)
        logger.info("Applied SQLite migration v%d (%s)", spec.version, spec.name)

    current_version = get_schema_version(conn)
    return MigrationRunResult(
        previous_version=previous_version,
        current_version=current_version,
        applied_versions=applied_versions,
        skipped_versions=skipped_versions,
    )
