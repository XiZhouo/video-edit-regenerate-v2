"""Explicit full audits for a mounted SQLite business repository.

The transaction runtime owns an indexed fast fence.  This separate leaf owns
the intentionally history-wide work: exact schema identity (through the fast
fence), SQLite page integrity, all foreign keys, and the complete reserved
business receipt namespace.  Startup and periodic callers are named
separately so a transaction path cannot accidentally disguise a full audit as
ordinary admission.  Production callers must additionally hold the external
repository/maintenance lease for the whole call.  Repeated inode samples and
the final fresh profile fence detect ordinary replacement races, but no finite
path sampling can defeat a malicious A→B→A swap that ignores that lease.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from typing import NamedTuple

from receipt_namespace_contract import ReceiptNamespaceEntry
from receipt_namespace_integrity import (
    ReceiptNamespaceIntegrityError,
    _receipt_namespace_sha256,
)
from sqlite_business_runtime import (
    BusinessRuntimeAccess,
    BusinessRuntimeProfile,
    _canonical_utc,
    _lower_hex,
    _validate_access,
    _validate_business_runtime_connection,
)
from sqlite_kernel import SQLiteKernelError, SqliteDatabase, _OwnedConnection
from sqlite_schema import (
    INSTALL_CLOSURE_MIGRATION,
    MIGRATIONS,
    PRODUCTION_AUTHORITY_WORKER_MIGRATION,
    _expected_migration_ledger_sha256,
)
from sqlite_schema import BUSINESS_TRANSACTION_KIND, BUSINESS_TRANSACTION_PREFIX


def _fail(code: str, message: str) -> None:
    raise SQLiteKernelError(code, message)


def _verify_relational_integrity(connection: _OwnedConnection) -> None:
    integrity_rows = connection.execute("PRAGMA integrity_check(1)").fetchall()
    if len(integrity_rows) != 1 or tuple(integrity_rows[0]) != ("ok",):
        _fail(
            "sqlite_business_integrity_failed",
            "SQLite integrity_check did not prove a healthy database",
        )
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        _fail(
            "sqlite_business_foreign_key_violation",
            "business runtime contains a foreign-key violation",
        )


def _verify_business_receipt_namespace(connection: _OwnedConnection) -> None:
    """Stream every reserved receipt and prove its exact immutable commit."""

    cursor = connection.execute(
        """
        SELECT tr.transaction_id, tr.kind, tr.committed_at, bc.commit_id,
               bc.committed_at AS commit_committed_at
        FROM transaction_receipts AS tr
        LEFT JOIN business_commits AS bc
          ON bc.transaction_id = tr.transaction_id
         AND bc.receipt_kind = tr.kind
        WHERE substr(CAST(tr.transaction_id AS TEXT), 1, ?) = ?
           OR CAST(tr.kind AS TEXT) = ?
        """,
        (
            len(BUSINESS_TRANSACTION_PREFIX),
            BUSINESS_TRANSACTION_PREFIX,
            BUSINESS_TRANSACTION_KIND,
        ),
    )
    for row in cursor:
        transaction_id = row["transaction_id"]
        commit_id = row["commit_id"]
        if (
            type(transaction_id) is not str
            or not transaction_id.startswith(BUSINESS_TRANSACTION_PREFIX)
            or not _lower_hex(
                transaction_id[len(BUSINESS_TRANSACTION_PREFIX) :], 32
            )
            or type(row["kind"]) is not str
            or row["kind"] != BUSINESS_TRANSACTION_KIND
            or not _canonical_utc(row["committed_at"])
            or not _lower_hex(commit_id, 32)
            or transaction_id != BUSINESS_TRANSACTION_PREFIX + commit_id
            or type(row["commit_committed_at"]) is not str
            or row["commit_committed_at"] != row["committed_at"]
        ):
            _fail(
                "sqlite_business_receipt_namespace_invalid",
                "reserved business receipt namespace is not one-to-one",
            )

    # The FK normally proves this direction.  Keep an explicit anti-join in
    # the full audit so an offline write that bypassed foreign_keys cannot turn
    # a missing receipt into a healthy business commit.
    missing = connection.execute(
        """
        SELECT 1
        FROM business_commits AS bc
        LEFT JOIN transaction_receipts AS tr
          ON tr.transaction_id = bc.transaction_id
         AND tr.kind = bc.receipt_kind
        WHERE tr.transaction_id IS NULL
        LIMIT 1
        """
    ).fetchone()
    if missing is not None:
        _fail(
            "sqlite_business_receipt_namespace_invalid",
            "business commits and reserved receipts are not one-to-one",
        )


def _read_receipt_namespace_identity(
    connection: _OwnedConnection,
    *,
    excluded_transaction_ids: tuple[str, ...] = (),
) -> tuple:
    """Stream the one canonical identity of every generic receipt row."""

    if (
        type(excluded_transaction_ids) is not tuple
        or any(type(value) is not str or not value for value in excluded_transaction_ids)
    ):
        raise TypeError("excluded_transaction_ids must be unique exact strings")
    excluded = frozenset(excluded_transaction_ids)
    if len(excluded) != len(excluded_transaction_ids):
        raise TypeError("excluded_transaction_ids must be unique exact strings")

    count_row = connection.execute(
        "SELECT count(*) AS receipt_count FROM transaction_receipts"
    ).fetchone()
    if count_row is None or type(count_row["receipt_count"]) is not int:
        _fail(
            "sqlite_receipt_namespace_unreadable",
            "receipt namespace count is unreadable",
        )
    expected_count = count_row["receipt_count"] - len(excluded_transaction_ids)
    if expected_count < 0:
        _fail(
            "sqlite_receipt_namespace_invalid",
            "excluded receipt identities exceed the stored namespace",
        )
    seen_excluded = 0
    cursor = connection.execute(
        "SELECT transaction_id, kind, committed_at "
        "FROM transaction_receipts ORDER BY transaction_id COLLATE BINARY"
    )

    def entries():
        nonlocal seen_excluded
        for row in cursor:
            if row["transaction_id"] in excluded:
                seen_excluded += 1
                continue
            try:
                yield ReceiptNamespaceEntry(
                    transaction_id=row["transaction_id"],
                    kind=row["kind"],
                    committed_at=row["committed_at"],
                )
            except (TypeError, ValueError) as exc:
                raise ReceiptNamespaceIntegrityError(
                    "receipt_namespace_entry_changed"
                ) from exc

    try:
        identity = _receipt_namespace_sha256(
            entries(),
            expected_count=expected_count,
        )
    except ReceiptNamespaceIntegrityError as exc:
        _fail(
            "sqlite_receipt_namespace_invalid",
            "receipt namespace canonical identity failed: " + exc.code,
        )
    if seen_excluded != len(excluded_transaction_ids):
        _fail(
            "sqlite_receipt_namespace_invalid",
            "excluded receipt identities are not an exact stored subset",
        )
    return identity


def _verify_business_revision_history(
    connection: _OwnedConnection,
    profile: BusinessRuntimeProfile,
) -> None:
    """Prove the global stream and every live current projection are closed."""

    # Every durable business commit, including the cutover baseline, is one
    # atomic write.  The forward foreign keys prove that an atomic receipt
    # points at a real commit; this anti-join proves the converse.  Without it
    # an offline writer that bypassed triggers/foreign_keys could delete the
    # zero-control baseline receipt while leaving a superficially continuous
    # commit and current-head stream behind.
    # Historical schema-v7 binaries have no outer atomic-receipt table.  Their
    # compatibility reader/writer is audited against the frozen v7 closure;
    # the reverse requirement begins exactly where schema-v8 introduced the
    # table and its production authority protocol.
    if MIGRATIONS[-1].version >= PRODUCTION_AUTHORITY_WORKER_MIGRATION.version:
        commit_without_atomic_receipt = connection.execute(
            """
            SELECT 1
            FROM business_commits AS business_commit
            LEFT JOIN atomic_business_write_receipts AS atomic_receipt
              ON atomic_receipt.commit_id = business_commit.commit_id
             AND atomic_receipt.transaction_id = business_commit.transaction_id
             AND atomic_receipt.receipt_kind = business_commit.receipt_kind
             AND atomic_receipt.committed_at = business_commit.committed_at
            WHERE atomic_receipt.commit_id IS NULL
            LIMIT 1
            """
        ).fetchone()
        if commit_without_atomic_receipt is not None:
            _fail(
                "sqlite_business_atomic_receipt_missing",
                "a durable business commit has no exact atomic-write receipt",
            )

    aggregate = connection.execute(
        "SELECT count(*) AS commit_count, "
        "min(repository_revision) AS first_revision, "
        "max(repository_revision) AS last_revision "
        "FROM business_commits"
    ).fetchone()
    if aggregate is None or type(aggregate["commit_count"]) is not int:
        _fail(
            "sqlite_business_revision_history_invalid",
            "business revision history aggregate is unreadable",
        )
    commit_count = aggregate["commit_count"]

    # A V12 empty install has no baseline commit but deliberately reserves
    # repository revision 1 as the mounted runtime's bootstrap high-water.
    # Its first post-mount business commit is therefore revision 2.  This is
    # the only legal hole in the global stream; non-empty installs and legacy
    # test/history profiles still start at revision 1.  Derive the exception
    # from the immutable closure row instead of inferring it from the observed
    # commit sequence, otherwise a forged history beginning at 2 would be
    # indistinguishable from a real empty install.
    closure_rows = []
    if MIGRATIONS[-1].version >= INSTALL_CLOSURE_MIGRATION.version:
        closure_rows = connection.execute(
            """
            SELECT closure.cutover_id,
                   closure.install_transaction_id,
                   closure.install_manifest_sha256,
                   closure.source_admission_revision,
                   closure.coverage_transaction_id,
                   closure.coverage_manifest_sha256,
                   closure.committed_at,
                   closure.project_count,
                   closure.live_project_count,
                   closure.deleted_project_count,
                   closure.baseline_commit_count,
                   install.cutover_id AS install_cutover_id,
                   install.transaction_id AS installed_transaction_id,
                   install.repository_epoch AS install_repository_epoch,
                   install.runtime_generation AS install_runtime_generation,
                   install.code_digest AS install_code_digest,
                   install.manifest_sha256 AS installed_manifest_sha256,
                   install.committed_at AS install_committed_at,
                   head.singleton AS head_singleton,
                   head.state_revision AS head_state_revision,
                   head.phase_revision AS head_phase_revision,
                   head.phase AS head_phase,
                   head.authority_state AS head_authority_state,
                   head.repository_epoch AS head_repository_epoch,
                   head.runtime_generation AS head_runtime_generation,
                   head.code_digest AS head_code_digest,
                   head.install_transaction_id AS head_install_transaction_id,
                   head.install_manifest_sha256 AS head_install_manifest_sha256,
                   head.coverage_transaction_id AS head_coverage_transaction_id,
                   head.coverage_manifest_sha256 AS head_coverage_manifest_sha256,
                   head.updated_at AS head_updated_at,
                   (SELECT count(*) FROM business_cutover_head)
                       AS cutover_head_count,
                   (SELECT count(*) FROM repository_audit_certificates)
                       AS audit_certificate_count,
                   (SELECT count(*) FROM repository_audit_head)
                       AS audit_head_count,
                   (SELECT count(*) FROM business_cutover_activation_receipts)
                       AS activation_count,
                   (SELECT count(*) FROM business_cutover_mount_receipts)
                       AS mount_count
            FROM business_cutover_install_closures AS closure
            LEFT JOIN business_cutover_install_receipts AS install
              ON install.cutover_id = closure.cutover_id AND
                 install.transaction_id = closure.install_transaction_id AND
                 install.manifest_sha256 = closure.install_manifest_sha256
            LEFT JOIN business_cutover_head AS head
              ON head.singleton = 1 AND
                 head.cutover_id = closure.cutover_id
            """
        ).fetchall()
    if (
        MIGRATIONS[-1].version >= INSTALL_CLOSURE_MIGRATION.version
        and len(closure_rows) != 1
    ):
        _fail(
            "sqlite_business_revision_history_invalid",
            "current business history has no exact singleton cutover closure",
        )
    revision_origin = 1
    baseline_commit_count = 0
    if closure_rows:
        closure = closure_rows[0]
        baseline_commit_count = closure["baseline_commit_count"]
        # The V12 install writer deliberately writes the semantic closure as
        # its final DML.  The canonical phase-1 head therefore belongs to a
        # later, independently recoverable transaction.  A freshly committed
        # install is healthy while that head is still absent, but only in the
        # narrow read-only runtime/head prefix below.  Treating every missing
        # head as corruption made the real writer's success state impossible
        # to full audit; accepting later phase suffixes would hide a torn
        # transition.  This shared prerequisite does not by itself prove the
        # three scheduler genesis heads: the global cutover auditor performs
        # that deeper semantic closure before the state can authorize anything.
        initial_head_required = (
            closure["cutover_head_count"] == 0
            and closure["head_singleton"] is None
            and closure["head_state_revision"] is None
            and closure["head_phase_revision"] is None
            and closure["head_phase"] is None
            and closure["head_authority_state"] is None
            and closure["head_repository_epoch"] is None
            and closure["head_runtime_generation"] is None
            and closure["head_code_digest"] is None
            and closure["head_install_transaction_id"] is None
            and closure["head_install_manifest_sha256"] is None
            and closure["head_coverage_transaction_id"] is None
            and closure["head_coverage_manifest_sha256"] is None
            and closure["head_updated_at"] is None
            and closure["audit_certificate_count"] == 0
            and closure["audit_head_count"] == 0
            and closure["activation_count"] == 0
            and closure["mount_count"] == 0
            and profile.authority_state.value == "read_only"
            and profile.state_revision == 1
        )
        headed_cutover = (
            closure["cutover_head_count"] == 1
            and closure["head_singleton"] == 1
            and type(closure["head_phase_revision"]) is int
            and closure["head_state_revision"]
            == closure["head_phase_revision"]
            and (
                closure["head_phase_revision"],
                closure["head_phase"],
                closure["head_authority_state"],
            )
            in (
                (1, "installed", "read_only"),
                (2, "assets_covered", "read_only"),
                (3, "audited", "read_only"),
                (4, "activated", "active"),
                (5, "mounted", "active"),
            )
            and closure["head_repository_epoch"] == profile.repository_epoch
            and closure["head_runtime_generation"]
            == profile.runtime_generation
            and closure["head_code_digest"] == profile.code_digest
            and closure["head_install_transaction_id"]
            == closure["install_transaction_id"]
            and closure["head_install_manifest_sha256"]
            == closure["install_manifest_sha256"]
            and (
                closure["head_phase_revision"] != 1
                or (
                    closure["head_coverage_transaction_id"] is None
                    and closure["head_coverage_manifest_sha256"] is None
                )
            )
            and (
                closure["head_phase_revision"] < 2
                or (
                    closure["head_coverage_transaction_id"]
                    == closure["coverage_transaction_id"]
                    and closure["head_coverage_manifest_sha256"]
                    == closure["coverage_manifest_sha256"]
                )
            )
            and _canonical_utc(closure["head_updated_at"])
            and closure["head_updated_at"] >= closure["committed_at"]
        )
        if (
            type(closure["cutover_id"]) is not str
            or closure["cutover_id"] != profile.cutover_id
            or type(closure["install_transaction_id"]) is not str
            or type(closure["install_manifest_sha256"]) is not bytes
            or len(closure["install_manifest_sha256"]) != 32
            or type(closure["source_admission_revision"]) is not int
            or closure["source_admission_revision"]
            != profile.source_admission_revision
            or type(closure["coverage_transaction_id"]) is not str
            or type(closure["coverage_manifest_sha256"]) is not bytes
            or len(closure["coverage_manifest_sha256"]) != 32
            or not _canonical_utc(closure["committed_at"])
            or type(closure["project_count"]) is not int
            or type(closure["live_project_count"]) is not int
            or type(closure["deleted_project_count"]) is not int
            or type(baseline_commit_count) is not int
            or baseline_commit_count < 0
            or baseline_commit_count != closure["live_project_count"]
            or closure["project_count"]
            != closure["live_project_count"] + closure["deleted_project_count"]
            or closure["install_cutover_id"] != profile.cutover_id
            or closure["installed_transaction_id"]
            != closure["install_transaction_id"]
            or closure["installed_manifest_sha256"]
            != closure["install_manifest_sha256"]
            or closure["install_repository_epoch"] != profile.repository_epoch
            or closure["install_runtime_generation"]
            != profile.runtime_generation
            or closure["install_code_digest"] != profile.code_digest
            or closure["install_committed_at"] != closure["committed_at"]
            or not (initial_head_required or headed_cutover)
            or profile.authority != "sqlite"
        ):
            _fail(
                "sqlite_business_revision_history_invalid",
                "business cutover closure cannot define the revision origin",
            )

        foreign_lineage = connection.execute(
            """
            SELECT 1
            FROM business_commits
            WHERE cutover_id != ? OR repository_epoch != ? OR
                  source_admission_revision != ?
            LIMIT 1
            """,
            (
                profile.cutover_id,
                profile.repository_epoch,
                profile.source_admission_revision,
            ),
        ).fetchone()
        baseline = connection.execute(
            """
            SELECT count(*) AS commit_count,
                   min(repository_revision) AS first_revision,
                   max(repository_revision) AS last_revision
            FROM business_commits
            WHERE cutover_id = ? AND commit_kind = 'baseline' AND
                  committed_at = ?
            """,
            (profile.cutover_id, closure["committed_at"]),
        ).fetchone()
        if (
            foreign_lineage is not None
            or baseline is None
            or type(baseline["commit_count"]) is not int
            or baseline["commit_count"] != baseline_commit_count
            or (
                baseline_commit_count == 0
                and (
                    baseline["first_revision"] is not None
                    or baseline["last_revision"] is not None
                )
            )
            or (
                baseline_commit_count > 0
                and (
                    baseline["first_revision"] != 1
                    or baseline["last_revision"] != baseline_commit_count
                )
            )
        ):
            _fail(
                "sqlite_business_revision_history_invalid",
                "business cutover baseline does not define one exact revision prefix",
            )
        revision_origin = 2 if baseline_commit_count == 0 else 1

    if commit_count == 0:
        if (
            aggregate["first_revision"] is not None
            or aggregate["last_revision"] is not None
            or profile.repository_revision != 1
            or baseline_commit_count != 0
        ):
            _fail(
                "sqlite_business_revision_history_invalid",
                "empty business history does not have the canonical revision floor",
            )
    elif (
        type(aggregate["first_revision"]) is not int
        or type(aggregate["last_revision"]) is not int
        or commit_count < baseline_commit_count
        or aggregate["first_revision"] != revision_origin
        or aggregate["last_revision"] != commit_count + revision_origin - 1
        or profile.repository_revision != aggregate["last_revision"]
    ):
        _fail(
            "sqlite_business_revision_history_invalid",
            "business repository revisions are not one continuous global stream",
        )

    project_gap = connection.execute(
        """
        SELECT 1
        FROM (
            SELECT project_id, generation, count(*) AS revision_count,
                   min(project_revision) AS first_revision,
                   max(project_revision) AS last_revision
            FROM business_commits
            GROUP BY project_id, generation
        ) AS project_stream
        WHERE first_revision != 1 OR last_revision != revision_count
        LIMIT 1
        """
    ).fetchone()
    head_gap = connection.execute(
        """
        SELECT 1
        FROM (
            SELECT project_id, generation, document_id,
                   count(*) AS revision_count,
                   min(head_revision) AS first_revision,
                   max(head_revision) AS last_revision
            FROM business_commits
            GROUP BY project_id, generation, document_id
        ) AS head_stream
        WHERE first_revision != 1 OR last_revision != revision_count
        LIMIT 1
        """
    ).fetchone()
    if project_gap is not None or head_gap is not None:
        _fail(
            "sqlite_business_local_revision_history_invalid",
            "project or document-head revisions are not one continuous local stream",
        )

    live_project_without_head = connection.execute(
        """
        SELECT 1
        FROM repository_projects AS repository_project
        WHERE repository_project.status = 'live'
          AND NOT EXISTS (
              SELECT 1
              FROM document_commit_heads AS document_head
              JOIN business_commits AS business_commit
                ON business_commit.commit_id = document_head.commit_id
              WHERE document_head.project_id = repository_project.project_id
                AND document_head.generation = repository_project.generation
                AND document_head.status = 'live'
                AND business_commit.project_revision =
                    repository_project.project_revision
                AND business_commit.head_revision = document_head.head_revision
                AND business_commit.after_root_sha256 =
                    repository_project.current_snapshot_sha256
                AND business_commit.after_root_sha256 =
                    document_head.business_root_sha256
          )
        LIMIT 1
        """
    ).fetchone()
    live_head_without_project = connection.execute(
        """
        SELECT 1
        FROM document_commit_heads AS document_head
        WHERE document_head.status = 'live'
          AND NOT EXISTS (
              SELECT 1
              FROM repository_projects AS repository_project
              WHERE repository_project.project_id = document_head.project_id
                AND repository_project.generation = document_head.generation
                AND repository_project.status = 'live'
          )
        LIMIT 1
        """
    ).fetchone()
    if live_project_without_head is not None or live_head_without_project is not None:
        _fail(
            "sqlite_business_current_projection_invalid",
            "live project and document-head projections are not mutually closed",
        )

    stale_live_project = connection.execute(
        """
        SELECT 1
        FROM repository_projects AS repository_project
        WHERE repository_project.status = 'live'
          AND (
              repository_project.project_revision != (
                  SELECT max(project_revision)
                  FROM business_commits
                  WHERE project_id = repository_project.project_id
                    AND generation = repository_project.generation
              ) OR NOT EXISTS (
                  SELECT 1
                  FROM business_commits AS newest_project_commit
                  WHERE newest_project_commit.project_id =
                            repository_project.project_id
                    AND newest_project_commit.generation =
                            repository_project.generation
                    AND newest_project_commit.project_revision =
                            repository_project.project_revision
                    AND newest_project_commit.after_root_sha256 =
                            repository_project.current_snapshot_sha256
              )
          )
        LIMIT 1
        """
    ).fetchone()
    stale_live_head = connection.execute(
        """
        SELECT 1
        FROM document_commit_heads AS document_head
        WHERE document_head.status = 'live'
          AND (
              document_head.head_revision != (
                  SELECT max(head_revision)
                  FROM business_commits
                  WHERE project_id = document_head.project_id
                    AND generation = document_head.generation
                    AND document_id = document_head.document_id
              ) OR NOT EXISTS (
                  SELECT 1
                  FROM business_commits AS newest_head_commit
                  WHERE newest_head_commit.commit_id = document_head.commit_id
                    AND newest_head_commit.project_id = document_head.project_id
                    AND newest_head_commit.generation = document_head.generation
                    AND newest_head_commit.document_id = document_head.document_id
                    AND newest_head_commit.head_revision =
                            document_head.head_revision
                    AND newest_head_commit.after_root_sha256 =
                            document_head.business_root_sha256
              )
          )
        LIMIT 1
        """
    ).fetchone()
    if stale_live_project is not None or stale_live_head is not None:
        _fail(
            "sqlite_business_current_projection_stale",
            "a live current projection is behind its local durable stream",
        )

    if commit_count == 0:
        live_current = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM repository_projects "
            "WHERE status = 'live' LIMIT 1) OR "
            "EXISTS(SELECT 1 FROM document_commit_heads "
            "WHERE status = 'live' LIMIT 1)"
        ).fetchone()
        if live_current is None or int(live_current[0]) != 0:
            _fail(
                "sqlite_business_current_projection_invalid",
                "empty business history cannot publish a live current projection",
            )
        return

    newest_closed = connection.execute(
        """
        SELECT 1
        FROM business_commits AS newest_commit
        JOIN repository_projects AS newest_project
          ON newest_project.project_id = newest_commit.project_id
         AND newest_project.generation = newest_commit.generation
         AND newest_project.status = 'live'
         AND newest_project.project_revision = newest_commit.project_revision
         AND newest_project.current_snapshot_sha256 =
                newest_commit.after_root_sha256
        JOIN document_commit_heads AS newest_head
          ON newest_head.commit_id = newest_commit.commit_id
         AND newest_head.status = 'live'
         AND newest_head.head_revision = newest_commit.head_revision
         AND newest_head.business_root_sha256 = newest_commit.after_root_sha256
        WHERE newest_commit.repository_revision = ?
        LIMIT 1
        """,
        (profile.repository_revision,),
    ).fetchone()
    if newest_closed is None:
        _fail(
            "sqlite_business_current_projection_invalid",
            "newest global revision is not represented by a closed current head",
        )


def _main_storage_identity(
    database: SqliteDatabase,
    *,
    readonly: bool = False,
) -> tuple:
    """Sample the owner-controlled main-file inode around one full audit."""

    if type(readonly) is not bool:
        raise TypeError("readonly must be an exact bool")
    if readonly:
        database._validate_readonly_storage()
    else:
        database._validate_storage_files()
    try:
        info = os.lstat(database.path)
    except OSError as exc:
        raise SQLiteKernelError(
            "sqlite_business_storage_identity_unreadable",
            "business database identity could not be inspected",
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        _fail(
            "sqlite_business_storage_identity_unsafe",
            "business database is not one pinned regular file",
        )
    return (info.st_dev, info.st_ino)


def _require_main_storage_identity(
    database: SqliteDatabase,
    expected: tuple,
    *,
    readonly: bool = False,
) -> None:
    if _main_storage_identity(database, readonly=readonly) != expected:
        _fail(
            "sqlite_business_storage_identity_changed",
            "business database path changed during the full audit",
        )


class BusinessRuntimeFullAuditObservation(NamedTuple):
    """All reusable audit facts read from one SQLite transaction snapshot."""

    profile: BusinessRuntimeProfile
    user_version: int
    schema_fingerprint: str
    migration_ledger_count: int
    migration_ledger_sha256: bytes
    receipt_namespace_count: int
    receipt_namespace_sha256: bytes


def _audit_business_runtime_connection(
    database: SqliteDatabase,
    connection: _OwnedConnection,
    *,
    access: BusinessRuntimeAccess,
) -> BusinessRuntimeFullAuditObservation:
    """Run every history-wide check and return its roots in the same snapshot.

    The future certificate transaction must call this helper from its own
    already-open ``BEGIN IMMEDIATE`` transaction and persist the stamped
    certificate before leaving that transaction.  Reading these roots again
    after this helper returns would create a cross-snapshot proof.
    """

    if not connection.in_transaction:
        raise TypeError("full audit connection must already own a transaction")
    profile = _validate_business_runtime_connection(
        database,
        connection,
        access=access,
    )
    _verify_relational_integrity(connection)
    _verify_business_receipt_namespace(connection)
    receipt_count, receipt_root = _read_receipt_namespace_identity(connection)
    _verify_business_revision_history(connection, profile)
    fingerprint_row = connection.execute(
        "SELECT value FROM kernel_metadata WHERE key='schema_fingerprint'"
    ).fetchone()
    if (
        fingerprint_row is None
        or type(fingerprint_row["value"]) is not str
        or len(fingerprint_row["value"]) != 64
    ):
        _fail(
            "sqlite_schema_fingerprint_invalid",
            "schema fingerprint is unavailable inside the audit snapshot",
        )
    return BusinessRuntimeFullAuditObservation(
        profile=profile,
        user_version=MIGRATIONS[-1].version,
        schema_fingerprint=fingerprint_row["value"],
        migration_ledger_count=MIGRATIONS[-1].version,
        migration_ledger_sha256=_expected_migration_ledger_sha256(),
        receipt_namespace_count=receipt_count,
        receipt_namespace_sha256=receipt_root,
    )


def _run_business_runtime_full_audit(
    database: SqliteDatabase,
    *,
    access: BusinessRuntimeAccess,
    purpose: str,
) -> BusinessRuntimeProfile:
    if not isinstance(database, SqliteDatabase):
        raise TypeError("database must be a SqliteDatabase")
    access = _validate_access(access)
    if purpose not in ("startup", "periodic"):
        raise ValueError("full audit purpose must be startup or periodic")

    connection = None
    storage_identity = _main_storage_identity(database)
    try:
        connection, _filesystem = database._open_unvalidated()
        _require_main_storage_identity(database, storage_identity)
        connection.execute("BEGIN")
        observation = _audit_business_runtime_connection(
            database,
            connection,
            access=access,
        )
        profile = observation.profile
        _require_main_storage_identity(database, storage_identity)
    except SQLiteKernelError:
        raise
    except sqlite3.Error as exc:
        raise SQLiteKernelError(
            "sqlite_business_runtime_unreadable",
            "business runtime full audit could not inspect structural state",
        ) from exc
    finally:
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
    database._validate_storage_files()
    _require_main_storage_identity(database, storage_identity)
    # Path inode sampling alone is vulnerable to an A→B→A replacement while
    # SQLite opens B.  Reopen the final path and re-prove the exact fast
    # profile before publishing the history-wide result.  Under the trusted
    # writer model the repository epoch/cutover/runtime tuple is the persistent
    # identity token; a malicious offline clone of all data and digests remains
    # outside this certificate's stated guarantee.
    publish_connection = None
    try:
        publish_connection, _filesystem = database._open_unvalidated()
        _require_main_storage_identity(database, storage_identity)
        publish_connection.execute("BEGIN")
        published_profile = _validate_business_runtime_connection(
            database,
            publish_connection,
            access=access,
        )
        if published_profile != profile:
            _fail(
                "sqlite_business_full_audit_publish_changed",
                "business runtime changed between full-audit and publish fences",
            )
        _require_main_storage_identity(database, storage_identity)
    except SQLiteKernelError:
        raise
    except sqlite3.Error as exc:
        raise SQLiteKernelError(
            "sqlite_business_runtime_unreadable",
            "business runtime publish fence could not be inspected",
        ) from exc
    finally:
        if publish_connection is not None:
            if publish_connection.in_transaction:
                publish_connection.rollback()
            publish_connection.close()
    return profile


def startup_business_runtime_full_audit(
    database: SqliteDatabase,
    *,
    access: BusinessRuntimeAccess = BusinessRuntimeAccess.READER,
) -> BusinessRuntimeProfile:
    """Run the mandatory history-wide audit before mounting a repository."""

    return _run_business_runtime_full_audit(
        database,
        access=access,
        purpose="startup",
    )


def periodic_business_runtime_full_audit(
    database: SqliteDatabase,
    *,
    access: BusinessRuntimeAccess = BusinessRuntimeAccess.READER,
) -> BusinessRuntimeProfile:
    """Run the same history-wide proof from an explicit periodic auditor."""

    return _run_business_runtime_full_audit(
        database,
        access=access,
        purpose="periodic",
    )


# Private core leaf.  Production mounting will import the exact startup or
# periodic name deliberately rather than exposing it through the stable SDK.
__all__ = ()
