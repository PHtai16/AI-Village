"""SQLite persistence repository for Phase 10 Skill Factory, Benchmarks, Proposals, Outcomes, and Rollback."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from core.contracts.event import Event
from core.contracts.identifiers import validate_safe_payload
from core.skills.models import (
    SkillActivationProposalRecord,
    SkillActivePointer,
    SkillBenchmarkRecord,
    SkillImprovementOutcomeRecord,
    SkillItReviewRecord,
    SkillLifecycleStatus,
    SkillPackageRecord,
    SkillProposalStatus,
    SkillStateError,
)
from storage.repositories.errors import RepositoryError
from storage.repositories.sqlite_store import SQLiteRepositoryStore


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class SQLiteSkillsRepository:
    """SQLite implementation of ``SkillLifecycleRepository`` over migration ``0015``."""

    def __init__(self, store: SQLiteRepositoryStore) -> None:
        """Bind skills repository to initialized ``SQLiteRepositoryStore``."""
        self._store = store

    def _append_audit_or_raise(self, conn: sqlite3.Connection, audit_event: Event) -> None:
        res = self._store.events.append_on_connection(conn, audit_event)
        if res.status == "REJECTED" or res.entry is None:
            msg = res.error.message if res.error else "Skill audit event append rejected."
            raise RepositoryError("AUDIT_APPEND_FAILED", msg, entity_id=audit_event.event_id)

    def _insert_package_row(
        self,
        conn: sqlite3.Connection,
        pkg: SkillPackageRecord,
    ) -> None:
        pkg_json = _dumps(pkg.model_dump(mode="json"))
        conn.execute(
            """
            INSERT INTO skill_packages (
                package_id, skill_id, version, lifecycle_status, risk_level,
                has_scripts, requires_human_approval, author, maintainer, category,
                source_improvement_id, storage_zone, package_relpath,
                manifest_sha256, package_sha256, record_json, record_version,
                created_by, created_at, updated_by, updated_at, last_audit_event_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                pkg.package_id,
                pkg.skill_id,
                pkg.version,
                pkg.lifecycle_status,
                pkg.risk_profile.risk_level,
                1 if pkg.risk_profile.has_executable_scripts else 0,
                1 if pkg.risk_profile.requires_human_approval else 0,
                pkg.frontmatter.author,
                pkg.frontmatter.maintainer,
                pkg.frontmatter.category,
                pkg.source_improvement_id,
                pkg.storage_zone,
                pkg.package_relpath,
                pkg.manifest_sha256,
                pkg.package_sha256,
                pkg_json,
                pkg.record_version,
                pkg.created_by,
                pkg.created_at.isoformat(),
                pkg.updated_by,
                pkg.updated_at.isoformat(),
                pkg.last_audit_event_id,
            ),
        )

    def _update_package_row(
        self,
        conn: sqlite3.Connection,
        pkg: SkillPackageRecord,
        *,
        expected_record_version: int,
    ) -> None:
        pkg_json = _dumps(pkg.model_dump(mode="json"))
        cur = conn.execute(
            """
            UPDATE skill_packages
            SET lifecycle_status = ?,
                storage_zone = ?,
                package_relpath = ?,
                manifest_sha256 = ?,
                package_sha256 = ?,
                record_json = ?,
                record_version = ?,
                updated_by = ?,
                updated_at = ?,
                last_audit_event_id = ?
            WHERE skill_id = ? AND version = ? AND record_version = ?;
            """,
            (
                pkg.lifecycle_status,
                pkg.storage_zone,
                pkg.package_relpath,
                pkg.manifest_sha256,
                pkg.package_sha256,
                pkg_json,
                pkg.record_version,
                pkg.updated_by,
                pkg.updated_at.isoformat(),
                pkg.last_audit_event_id,
                pkg.skill_id,
                pkg.version,
                expected_record_version,
            ),
        )
        if cur.rowcount != 1:
            raise SkillStateError(
                "STALE_SKILL_VERSION",
                (
                    f"Optimistic lock failed updating '{pkg.skill_id}@{pkg.version}' "
                    f"(expected record_version={expected_record_version})."
                ),
            )

    def insert_package_atomic(
        self,
        *,
        package: SkillPackageRecord,
        audit_event: Event,
    ) -> SkillPackageRecord:
        """Persist a new Skill package version and audit event atomically."""
        validated = SkillPackageRecord.model_validate_json(package.model_dump_json())
        validate_safe_payload(validated.model_dump(mode="json"), "skill_package")
        with self._store.transaction_scope() as conn:
            self._append_audit_or_raise(conn, audit_event)
            try:
                self._insert_package_row(conn, validated)
            except sqlite3.IntegrityError as exc:
                raise SkillStateError(
                    "DUPLICATE_SKILL_VERSION",
                    f"Skill '{validated.skill_id}' version '{validated.version}' already exists.",
                ) from exc
            except sqlite3.Error as exc:
                raise RepositoryError("PERSISTENCE_FAILED", str(exc), entity_id=validated.package_id) from exc
            return validated

    def update_package_atomic(
        self,
        *,
        package: SkillPackageRecord,
        expected_record_version: int,
        audit_event: Event,
    ) -> SkillPackageRecord:
        """Atomically update a Skill package version with optimistic locking."""
        validated = SkillPackageRecord.model_validate_json(package.model_dump_json())
        validate_safe_payload(validated.model_dump(mode="json"), "skill_package")
        with self._store.transaction_scope() as conn:
            self._append_audit_or_raise(conn, audit_event)
            self._update_package_row(
                conn, validated, expected_record_version=expected_record_version
            )
            return validated

    def get_package(self, skill_id: str, version: str) -> SkillPackageRecord | None:
        """Fetch one persisted ``SkillPackageRecord``."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                "SELECT record_json FROM skill_packages WHERE skill_id = ? AND version = ?;",
                (skill_id, version),
            ).fetchone()
            return SkillPackageRecord.model_validate_json(str(row["record_json"])) if row else None

    def list_packages(
        self,
        *,
        skill_id: str | None = None,
        lifecycle_status: SkillLifecycleStatus | None = None,
    ) -> list[SkillPackageRecord]:
        """List persisted Skill packages ordered chronologically."""
        clauses: list[str] = []
        params: list[object] = []
        if skill_id is not None:
            clauses.append("skill_id = ?")
            params.append(skill_id)
        if lifecycle_status is not None:
            clauses.append("lifecycle_status = ?")
            params.append(lifecycle_status)
        where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                f"SELECT record_json FROM skill_packages {where_sql}ORDER BY created_at ASC, version ASC;",
                tuple(params),
            ).fetchall()
            return [SkillPackageRecord.model_validate_json(str(r["record_json"])) for r in rows]

    def get_active_pointer(self, skill_id: str) -> SkillActivePointer | None:
        """Fetch active pointer record for ``skill_id``."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                "SELECT * FROM skill_active_pointers WHERE skill_id = ?;",
                (skill_id,),
            ).fetchone()
            if row is None:
                return None
            return SkillActivePointer.model_validate(dict(row))

    def list_active_pointers(self) -> list[SkillActivePointer]:
        """List all persisted active skill pointers ordered by ``skill_id``."""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM skill_active_pointers ORDER BY skill_id ASC;"
            ).fetchall()
            return [SkillActivePointer.model_validate(dict(r)) for r in rows]

    def insert_it_review_atomic(
        self,
        *,
        review: SkillItReviewRecord,
        updated_package: SkillPackageRecord,
        expected_record_version: int,
        audit_event: Event,
    ) -> tuple[SkillItReviewRecord, SkillPackageRecord]:
        """Persist IT review and transition package state atomically."""
        val_review = SkillItReviewRecord.model_validate_json(review.model_dump_json())
        val_pkg = SkillPackageRecord.model_validate_json(updated_package.model_dump_json())
        validate_safe_payload(val_review.model_dump(mode="json"), "skill_it_review")
        with self._store.transaction_scope() as conn:
            self._append_audit_or_raise(conn, audit_event)
            conn.execute(
                """
                INSERT INTO skill_it_reviews (
                    review_id, skill_id, version, package_sha256,
                    reviewer_id, passed, review_json, reviewed_at, audit_event_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    val_review.review_id,
                    val_review.skill_id,
                    val_review.version,
                    val_review.package_sha256,
                    val_review.reviewer_id,
                    1 if val_review.passed else 0,
                    _dumps(val_review.model_dump(mode="json")),
                    val_review.reviewed_at.isoformat(),
                    val_review.audit_event_id,
                ),
            )
            self._update_package_row(
                conn, val_pkg, expected_record_version=expected_record_version
            )
            return val_review, val_pkg

    def list_it_reviews(self, skill_id: str, version: str) -> list[SkillItReviewRecord]:
        """List IT reviews ordered chronologically."""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                """
                SELECT review_json FROM skill_it_reviews
                WHERE skill_id = ? AND version = ?
                ORDER BY reviewed_at ASC, review_id ASC;
                """,
                (skill_id, version),
            ).fetchall()
            return [SkillItReviewRecord.model_validate_json(str(r["review_json"])) for r in rows]

    def insert_benchmark_atomic(
        self,
        *,
        benchmark: SkillBenchmarkRecord,
        updated_package: SkillPackageRecord,
        expected_record_version: int,
        audit_event: Event,
    ) -> tuple[SkillBenchmarkRecord, SkillPackageRecord]:
        """Persist immutable QA A/B benchmark record and update package state atomically."""
        val_bm = SkillBenchmarkRecord.model_validate_json(benchmark.model_dump_json())
        val_pkg = SkillPackageRecord.model_validate_json(updated_package.model_dump_json())
        validate_safe_payload(val_bm.model_dump(mode="json"), "skill_benchmark")
        with self._store.transaction_scope() as conn:
            self._append_audit_or_raise(conn, audit_event)
            conn.execute(
                """
                INSERT INTO skill_benchmarks (
                    benchmark_id, skill_id, version, package_sha256,
                    benchmark_status, qa_actor_id, evidence_id,
                    benchmark_json, recorded_at, audit_event_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    val_bm.benchmark_id,
                    val_bm.skill_id,
                    val_bm.version,
                    val_bm.package_sha256,
                    val_bm.benchmark_status,
                    val_bm.qa_actor_id,
                    val_bm.evidence_id,
                    _dumps(val_bm.model_dump(mode="json")),
                    val_bm.recorded_at.isoformat(),
                    val_bm.audit_event_id,
                ),
            )
            self._update_package_row(
                conn, val_pkg, expected_record_version=expected_record_version
            )
            return val_bm, val_pkg

    def list_benchmarks(self, skill_id: str, version: str) -> list[SkillBenchmarkRecord]:
        """List immutable benchmarks recorded for ``(skill_id, version)``."""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                """
                SELECT benchmark_json FROM skill_benchmarks
                WHERE skill_id = ? AND version = ?
                ORDER BY recorded_at ASC, benchmark_id ASC;
                """,
                (skill_id, version),
            ).fetchall()
            return [SkillBenchmarkRecord.model_validate_json(str(r["benchmark_json"])) for r in rows]

    def insert_activation_proposal_atomic(
        self,
        *,
        proposal: SkillActivationProposalRecord,
        audit_event: Event,
    ) -> SkillActivationProposalRecord:
        """Persist Master skill activation proposal atomically."""
        val_prop = SkillActivationProposalRecord.model_validate_json(proposal.model_dump_json())
        validate_safe_payload(val_prop.model_dump(mode="json"), "skill_activation_proposal")
        with self._store.transaction_scope() as conn:
            self._append_audit_or_raise(conn, audit_event)
            conn.execute(
                """
                INSERT INTO skill_activation_proposals (
                    proposal_id, skill_id, version, package_sha256,
                    proposal_status, requires_human_approval, approval_id,
                    proposed_by, proposal_json, proposed_at, decided_by, decided_at, audit_event_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    val_prop.proposal_id,
                    val_prop.skill_id,
                    val_prop.version,
                    val_prop.package_sha256,
                    val_prop.proposal_status,
                    1 if val_prop.requires_human_approval else 0,
                    val_prop.approval_id,
                    val_prop.proposed_by,
                    _dumps(val_prop.model_dump(mode="json")),
                    val_prop.proposed_at.isoformat(),
                    val_prop.decided_by,
                    val_prop.decided_at.isoformat() if val_prop.decided_at else None,
                    val_prop.audit_event_id,
                ),
            )
            return val_prop

    def get_activation_proposal(self, proposal_id: str) -> SkillActivationProposalRecord | None:
        """Fetch an activation proposal by ID."""
        with self._store.open_connection() as conn:
            row = conn.execute(
                "SELECT proposal_json FROM skill_activation_proposals WHERE proposal_id = ?;",
                (proposal_id,),
            ).fetchone()
            return (
                SkillActivationProposalRecord.model_validate_json(str(row["proposal_json"]))
                if row
                else None
            )

    def list_activation_proposals(
        self,
        *,
        skill_id: str | None = None,
        proposal_status: SkillProposalStatus | None = None,
    ) -> list[SkillActivationProposalRecord]:
        """List skill activation proposals ordered chronologically."""
        clauses: list[str] = []
        params: list[object] = []
        if skill_id is not None:
            clauses.append("skill_id = ?")
            params.append(skill_id)
        if proposal_status is not None:
            clauses.append("proposal_status = ?")
            params.append(proposal_status)
        where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        with self._store.open_connection() as conn:
            rows = conn.execute(
                f"SELECT proposal_json FROM skill_activation_proposals {where_sql}ORDER BY proposed_at ASC, proposal_id ASC;",
                tuple(params),
            ).fetchall()
            return [
                SkillActivationProposalRecord.model_validate_json(str(r["proposal_json"]))
                for r in rows
            ]

    def _upsert_pointer(self, conn: sqlite3.Connection, pointer: SkillActivePointer) -> None:
        conn.execute(
            """
            INSERT INTO skill_active_pointers (
                skill_id, active_version, previous_approved_version,
                updated_by, updated_at, last_audit_event_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(skill_id) DO UPDATE SET
                active_version = excluded.active_version,
                previous_approved_version = excluded.previous_approved_version,
                updated_by = excluded.updated_by,
                updated_at = excluded.updated_at,
                last_audit_event_id = excluded.last_audit_event_id;
            """,
            (
                pointer.skill_id,
                pointer.active_version,
                pointer.previous_approved_version,
                pointer.updated_by,
                pointer.updated_at.isoformat(),
                pointer.last_audit_event_id,
            ),
        )

    def activate_package_atomic(
        self,
        *,
        package: SkillPackageRecord,
        expected_record_version: int,
        proposal: SkillActivationProposalRecord,
        pointer: SkillActivePointer,
        demoted_prior_package: SkillPackageRecord | None,
        audit_event: Event,
    ) -> tuple[SkillPackageRecord, SkillActivationProposalRecord, SkillActivePointer]:
        """Promote package to APPROVED, update pointer/prior version, and settle proposal atomically."""
        val_pkg = SkillPackageRecord.model_validate_json(package.model_dump_json())
        val_prop = SkillActivationProposalRecord.model_validate_json(proposal.model_dump_json())
        val_ptr = SkillActivePointer.model_validate_json(pointer.model_dump_json())
        with self._store.transaction_scope() as conn:
            self._append_audit_or_raise(conn, audit_event)
            if demoted_prior_package is not None:
                self._update_package_row(
                    conn,
                    demoted_prior_package,
                    expected_record_version=demoted_prior_package.record_version - 1,
                )
            self._update_package_row(
                conn, val_pkg, expected_record_version=expected_record_version
            )
            conn.execute(
                """
                UPDATE skill_activation_proposals
                SET proposal_status = ?,
                    approval_id = ?,
                    proposal_json = ?,
                    decided_by = ?,
                    decided_at = ?
                WHERE proposal_id = ?;
                """,
                (
                    val_prop.proposal_status,
                    val_prop.approval_id,
                    _dumps(val_prop.model_dump(mode="json")),
                    val_prop.decided_by,
                    val_prop.decided_at.isoformat() if val_prop.decided_at else None,
                    val_prop.proposal_id,
                ),
            )
            self._upsert_pointer(conn, val_ptr)
            return val_pkg, val_prop, val_ptr

    def rollback_active_pointer_atomic(
        self,
        *,
        previous_active_package: SkillPackageRecord,
        restored_package: SkillPackageRecord,
        pointer: SkillActivePointer,
        audit_event: Event,
    ) -> SkillActivePointer:
        """Atomically demote faulty active version and restore prior APPROVED pointer."""
        val_prev = SkillPackageRecord.model_validate_json(previous_active_package.model_dump_json())
        val_rest = SkillPackageRecord.model_validate_json(restored_package.model_dump_json())
        val_ptr = SkillActivePointer.model_validate_json(pointer.model_dump_json())
        with self._store.transaction_scope() as conn:
            self._append_audit_or_raise(conn, audit_event)
            self._update_package_row(
                conn, val_prev, expected_record_version=val_prev.record_version - 1
            )
            self._update_package_row(
                conn, val_rest, expected_record_version=val_rest.record_version - 1
            )
            self._upsert_pointer(conn, val_ptr)
            return val_ptr

    def insert_improvement_outcome_atomic(
        self,
        *,
        outcome: SkillImprovementOutcomeRecord,
        audit_event: Event,
    ) -> SkillImprovementOutcomeRecord:
        """Persist immutable post-activation effectiveness outcome atomically."""
        val_out = SkillImprovementOutcomeRecord.model_validate_json(outcome.model_dump_json())
        validate_safe_payload(val_out.model_dump(mode="json"), "skill_improvement_outcome")
        with self._store.transaction_scope() as conn:
            self._append_audit_or_raise(conn, audit_event)
            conn.execute(
                """
                INSERT INTO skill_improvement_outcomes (
                    outcome_id, skill_id, version, source_improvement_id,
                    regression_detected, outcome_json, recorded_at, audit_event_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    val_out.outcome_id,
                    val_out.skill_id,
                    val_out.version,
                    val_out.source_improvement_id,
                    1 if val_out.regression_detected else 0,
                    _dumps(val_out.model_dump(mode="json")),
                    val_out.recorded_at.isoformat(),
                    val_out.audit_event_id,
                ),
            )
            return val_out

    def list_improvement_outcomes(
        self,
        *,
        skill_id: str | None = None,
    ) -> list[SkillImprovementOutcomeRecord]:
        """List immutable measured post-activation outcomes."""
        clause = "WHERE skill_id = ? " if skill_id is not None else ""
        params = (skill_id,) if skill_id is not None else ()
        with self._store.open_connection() as conn:
            rows = conn.execute(
                f"SELECT outcome_json FROM skill_improvement_outcomes {clause}ORDER BY recorded_at ASC, outcome_id ASC;",
                params,
            ).fetchall()
            return [
                SkillImprovementOutcomeRecord.model_validate_json(str(r["outcome_json"]))
                for r in rows
            ]


__all__ = ["SQLiteSkillsRepository"]
