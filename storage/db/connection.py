"""SQLite connection management, explicit transaction control, and integrity diagnostics."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import logging
import os
from pathlib import Path
import sqlite3
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)

DEFAULT_RUNTIME_DB_PATH = Path(".runtime") / "ai_village.db"
JournalMode = Literal["WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY"]


class SQLiteConfig(BaseModel):
    """Configuration for opening SQLite connections without creating files at import time."""

    model_config = ConfigDict(extra="forbid")

    db_path: Path
    timeout_seconds: float = Field(default=10.0, gt=0.0, le=300.0)
    busy_timeout_ms: int = Field(default=10000, ge=0, le=300000)
    journal_mode: JournalMode = "WAL"
    enable_foreign_keys: Literal[True] = True

    @field_validator("db_path", mode="before")
    @classmethod
    def _coerce_path(cls, value: Path | str) -> Path:
        """Accept ``Path`` or string path while rejecting empty values."""
        if isinstance(value, str):
            cleaned = value.strip()
            if not cleaned:
                raise ValueError("db_path must not be blank.")
            return Path(cleaned)
        return value

    @property
    def is_in_memory(self) -> bool:
        """Return True when this configuration targets SQLite's in-memory database."""
        return str(self.db_path) == ":memory:"


class SQLiteIntegrityReport(BaseModel):
    """Structured result of ``PRAGMA integrity_check`` and ``PRAGMA foreign_key_check``."""

    model_config = ConfigDict(extra="forbid")

    integrity_ok: bool
    integrity_messages: list[str]
    foreign_key_ok: bool
    foreign_key_violations: list[dict[str, Any]]
    foreign_keys_enabled: bool
    schema_version: int = Field(ge=0)


def resolve_database_path(
    db_path: Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve runtime SQLite path from explicit argument, environment, or `.runtime/` default.

    Does not touch the filesystem on call or module import.
    """
    if db_path is not None:
        raw = str(db_path).strip()
        if not raw:
            raise ValueError("db_path must not be blank.")
        return Path(raw)

    source_env = env if env is not None else os.environ
    env_value = source_env.get("AI_VILLAGE_DB_PATH", "").strip()
    if env_value:
        return Path(env_value)
    return DEFAULT_RUNTIME_DB_PATH


def _coerce_config(config: SQLiteConfig | Path | str) -> SQLiteConfig:
    """Normalize a path or ``SQLiteConfig`` instance into ``SQLiteConfig``."""
    if isinstance(config, SQLiteConfig):
        return config
    return SQLiteConfig(db_path=resolve_database_path(config))


def open_sqlite_connection(config: SQLiteConfig | Path | str) -> sqlite3.Connection:
    """Open a configured SQLite connection with manual transactions and ``foreign_keys=ON``."""
    cfg = _coerce_config(config)
    if not cfg.is_in_memory:
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)

    target = ":memory:" if cfg.is_in_memory else str(cfg.db_path)
    conn = sqlite3.connect(
        target,
        timeout=cfg.timeout_seconds,
        isolation_level=None,
        check_same_thread=False,
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {cfg.busy_timeout_ms};")
        conn.execute("PRAGMA foreign_keys = ON;")
        fk_row = conn.execute("PRAGMA foreign_keys;").fetchone()
        if fk_row is None or int(fk_row[0]) != 1:
            raise RuntimeError("Failed to enable SQLite PRAGMA foreign_keys=ON.")

        if not cfg.is_in_memory:
            conn.execute(f"PRAGMA journal_mode = {cfg.journal_mode};")
            conn.execute("PRAGMA synchronous = FULL;")
    except Exception:
        conn.close()
        raise

    logger.debug("Opened SQLite connection to %s (journal_mode=%s)", target, cfg.journal_mode)
    return conn


@contextmanager
def sqlite_connection(config: SQLiteConfig | Path | str) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection and guarantee rollback-on-uncommitted + close on exit."""
    conn = open_sqlite_connection(config)
    try:
        yield conn
    finally:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK;")
            except sqlite3.Error as exc:
                logger.warning("Rollback during SQLite connection close failed: %s", exc)
        conn.close()


@contextmanager
def sqlite_transaction(
    conn: sqlite3.Connection,
    *,
    immediate: bool = True,
) -> Iterator[sqlite3.Connection]:
    """Execute a block inside an explicit SQLite transaction or nested SAVEPOINT."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE;" if immediate else "BEGIN;")
        try:
            yield conn
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK;")
            raise
        else:
            if conn.in_transaction:
                conn.execute("COMMIT;")
        return

    savepoint = f"sp_{uuid.uuid4().hex[:12]}"
    conn.execute(f"SAVEPOINT {savepoint};")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint};")
            conn.execute(f"RELEASE SAVEPOINT {savepoint};")
        raise
    else:
        if conn.in_transaction:
            conn.execute(f"RELEASE SAVEPOINT {savepoint};")


def run_integrity_checks(conn: sqlite3.Connection) -> SQLiteIntegrityReport:
    """Run ``PRAGMA integrity_check`` and ``PRAGMA foreign_key_check`` on an open connection."""
    fk_row = conn.execute("PRAGMA foreign_keys;").fetchone()
    fk_enabled = bool(fk_row is not None and int(fk_row[0]) == 1)

    integrity_rows = conn.execute("PRAGMA integrity_check;").fetchall()
    messages = [str(row[0]) for row in integrity_rows]
    integrity_ok = messages == ["ok"]

    fk_rows = conn.execute("PRAGMA foreign_key_check;").fetchall()
    violations = [
        {
            "table": row[0],
            "rowid": row[1],
            "parent": row[2],
            "fkid": row[3],
        }
        for row in fk_rows
    ]

    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations';"
    ).fetchone()
    version = 0
    if table_exists is not None:
        ver_row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations;").fetchone()
        version = int(ver_row[0]) if ver_row is not None else 0

    return SQLiteIntegrityReport(
        integrity_ok=integrity_ok,
        integrity_messages=messages,
        foreign_key_ok=len(violations) == 0,
        foreign_key_violations=violations,
        foreign_keys_enabled=fk_enabled,
        schema_version=version,
    )
