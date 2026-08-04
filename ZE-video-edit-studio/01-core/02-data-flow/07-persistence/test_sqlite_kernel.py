"""Adversarial evidence for the standalone W06.1 SQLite kernel.

These tests intentionally exercise the real filesystem, independent SQLite
connections and spawned processes.  Synchronisation uses barriers/events;
there are no timing sleeps that can accidentally turn a race into a pass.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import warnings
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from typing import Any, List, Tuple

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import legacy_catalog_contract as catalog_contract
import legacy_import_contract as import_contract
import legacy_repository_contract as repository_contract
import sqlite_kernel as kernel


def _local_probe(path: Path) -> kernel.FilesystemInfo:
    return kernel.FilesystemInfo("test-local", str(path), True)


def _network_probe(path: Path) -> kernel.FilesystemInfo:
    return kernel.FilesystemInfo("nfs", str(path), False, "network filesystem")


def _unknown_probe(path: Path) -> kernel.FilesystemInfo:
    return kernel.FilesystemInfo("unknown", str(path), False, "not proven local")


def _noop(_uow: kernel.UnitOfWork) -> None:
    return None


def _database(tmp_path: Path, *, busy_timeout_ms: int = 5000) -> kernel.SqliteDatabase:
    path = tmp_path.resolve() / "private" / "repository.sqlite3"
    return kernel.SqliteDatabase(
        path,
        settings=kernel.SQLiteSettings(busy_timeout_ms=busy_timeout_ms),
        _test_filesystem_probe=_local_probe,
    )


_SNAPSHOT = b"{}"
_SNAPSHOT_SHA256 = b"s" * 32
_NOW = "2026-07-11T00:00:00.000Z"
_SCHEMA_FINGERPRINT_V4 = kernel._expected_schema_fingerprint(4)
_SCHEMA_FINGERPRINT_V5 = kernel._expected_schema_fingerprint(5)
_SCHEMA_FINGERPRINT_V7 = (
    "9c448f57ab122034c7576c8ae4f872a721ec7d4abb14c790e9c689c11f17bffa"
)
_SCHEMA_FINGERPRINT_V8 = (
    "0d96ffa4e88ac3c3d3fd1ba1b6f09f11a3a2d7a0e1861f5781bf0856d7fce771"
)
_SCHEMA_FINGERPRINT_V9 = (
    "ccb5b4d248c2c429c0a99a5811816521cf4563de3bee457db826b4e4d4090ddf"
)
_SCHEMA_FINGERPRINT_V10 = (
    "4ef1cb22745c3982b00077762a741371adbc17ca057bf908eb18a1642a45bc5e"
)
_SCHEMA_FINGERPRINT_V11 = (
    "d92e9a6ac8c58ef6ed598a4c53bc972239f93b8e8c2a4d8a7e54369e3dc1c74f"
)
_SCHEMA_FINGERPRINT_V12 = (
    "4c265275dcf7d90e4ee77fda3b3a1749646e5e55783ce17fdff40e737195b226"
)
_SCHEMA_FINGERPRINT_V13 = (
    "28ea7b266fdce716b14b4002c3e5431d"
    "8984f51195aba838f392fa0060b25527"
)


def _raw_database(database: kernel.SqliteDatabase) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database.path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _insert_project(
    connection: sqlite3.Connection,
    project_id: Any,
    *,
    generation: Any = 1,
    codec_id: Any = "strict-json",
    codec_version: Any = 1,
    snapshot_format_version: Any = 1,
    snapshot_bytes: Any = _SNAPSHOT,
    snapshot_sha256: Any = _SNAPSHOT_SHA256,
) -> None:
    connection.execute(
        """
        INSERT INTO projects(
            repository_singleton, project_id, generation,
            codec_id, codec_version, snapshot_format_version,
            snapshot_bytes, snapshot_sha256
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            generation,
            codec_id,
            codec_version,
            snapshot_format_version,
            snapshot_bytes,
            snapshot_sha256,
        ),
    )


def _insert_document(
    connection: sqlite3.Connection,
    project_id: Any,
    document_id: Any,
    *,
    document_version: Any = 1,
    schema_version: Any = "1.0.0",
    collaboration_mode: Any = "live_collab",
    save_policy: Any = None,
    codec_id: Any = "strict-json",
    codec_version: Any = 1,
    snapshot_format_version: Any = 1,
    snapshot_bytes: Any = _SNAPSHOT,
    snapshot_sha256: Any = _SNAPSHOT_SHA256,
) -> None:
    connection.execute(
        """
        INSERT INTO documents(
            project_id, document_id, document_version, schema_version,
            collaboration_mode, save_policy, codec_id, codec_version,
            snapshot_format_version, snapshot_bytes, snapshot_sha256
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            document_id,
            document_version,
            schema_version,
            collaboration_mode,
            save_policy,
            codec_id,
            codec_version,
            snapshot_format_version,
            snapshot_bytes,
            snapshot_sha256,
        ),
    )


def _insert_entity(
    connection: sqlite3.Connection,
    project_id: Any,
    document_id: Any,
    entity_type: Any,
    entity_id: Any,
    *,
    entity_version: Any = 1,
    deleted: Any = 0,
    codec_id: Any = "strict-json",
    codec_version: Any = 1,
    snapshot_format_version: Any = 1,
    snapshot_bytes: Any = _SNAPSHOT,
    snapshot_sha256: Any = _SNAPSHOT_SHA256,
) -> None:
    connection.execute(
        """
        INSERT INTO entities(
            project_id, document_id, entity_type, entity_id,
            entity_version, deleted, codec_id, codec_version,
            snapshot_format_version, snapshot_bytes, snapshot_sha256
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            document_id,
            entity_type,
            entity_id,
            entity_version,
            deleted,
            codec_id,
            codec_version,
            snapshot_format_version,
            snapshot_bytes,
            snapshot_sha256,
        ),
    )


def _insert_import_batch(
    connection: sqlite3.Connection,
    import_id: Any = "a" * 64,
    **overrides: Any,
) -> None:
    values = {
        "transaction_id": import_contract.SQLITE_IMPORT_TRANSACTION_PREFIX + import_id,
        "receipt_kind": import_contract.SQLITE_IMPORT_TRANSACTION_KIND,
        "receipt_protocol": import_contract.SQLITE_IMPORT_RECEIPT_PROTOCOL,
        "status": import_contract.SQLITE_IMPORT_TERMINAL_STATUS,
        "manifest_schema": import_contract.LEGACY_MANIFEST_SCHEMA,
        "batch_protocol": import_contract.LEGACY_BATCH_PROTOCOL,
        "payload_protocol": import_contract.LEGACY_PAYLOAD_PROTOCOL,
        "selection": "explicit_projects",
        "project_count": 1,
        "entity_count": 1,
        "row_snapshot_bytes": 100,
        "framed_payload_bytes": 200,
        "manifest_bytes": b"{}",
        "manifest_sha256": b"m" * 32,
        "payload_sha256": b"p" * 32,
        "batch_sha256": b"b" * 32,
    }
    values.update(overrides)
    connection.execute(
        """
        INSERT INTO legacy_import_batches(
            import_id, transaction_id, receipt_kind, receipt_protocol, status,
            manifest_schema, batch_protocol, payload_protocol, selection,
            project_count, entity_count, row_snapshot_bytes,
            framed_payload_bytes, manifest_bytes, manifest_sha256,
            payload_sha256, batch_sha256
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            import_id,
            values["transaction_id"],
            values["receipt_kind"],
            values["receipt_protocol"],
            values["status"],
            values["manifest_schema"],
            values["batch_protocol"],
            values["payload_protocol"],
            values["selection"],
            values["project_count"],
            values["entity_count"],
            values["row_snapshot_bytes"],
            values["framed_payload_bytes"],
            values["manifest_bytes"],
            values["manifest_sha256"],
            values["payload_sha256"],
            values["batch_sha256"],
        ),
    )


def _insert_import_kernel_receipt(
    connection: sqlite3.Connection,
    import_id: str = "a" * 64,
    *,
    transaction_id: Any = None,
    kind: Any = import_contract.SQLITE_IMPORT_TRANSACTION_KIND,
) -> None:
    actual_transaction_id = (
        import_contract.SQLITE_IMPORT_TRANSACTION_PREFIX + import_id
        if transaction_id is None
        else transaction_id
    )
    connection.execute(
        """
        INSERT INTO transaction_receipts(transaction_id, kind, committed_at)
        VALUES (?, ?, '2026-07-11T00:00:00.000Z')
        """,
        (actual_transaction_id, kind),
    )


def _insert_import_project(
    connection: sqlite3.Connection,
    import_id: Any = "a" * 64,
    **overrides: Any,
) -> None:
    values = {
        "ordinal": 0,
        "project_id": "project-a",
        "generation": 1,
        "document_id": "document-a",
        "document_version": 1,
        "storage_version": "v2",
        "source_head_sha256": b"h" * 32,
        "source_head_bytes": 100,
        "business_root_sha256": b"r" * 32,
        "entity_count": 1,
        "row_snapshot_bytes": 100,
    }
    values.update(overrides)
    connection.execute(
        """
        INSERT INTO legacy_import_projects(
            import_id, ordinal, project_id, generation,
            document_id, document_version, storage_version,
            source_head_sha256, source_head_bytes, business_root_sha256,
            entity_count, row_snapshot_bytes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            import_id,
            values["ordinal"],
            values["project_id"],
            values["generation"],
            values["document_id"],
            values["document_version"],
            values["storage_version"],
            values["source_head_sha256"],
            values["source_head_bytes"],
            values["business_root_sha256"],
            values["entity_count"],
            values["row_snapshot_bytes"],
        ),
    )


def _insert_import_state_marker(
    connection: sqlite3.Connection,
    value: Any = import_contract.SQLITE_IMPORT_STATE_TERMINAL_SHADOW,
) -> None:
    connection.execute(
        "INSERT INTO kernel_metadata(key, value) VALUES (?, ?)",
        (import_contract.SQLITE_IMPORT_STATE_KEY, value),
    )


def _insert_catalog_state_marker(
    connection: sqlite3.Connection,
    value: Any = catalog_contract.SQLITE_CATALOG_STATE_HISTORICAL_FENCED,
) -> None:
    connection.execute(
        "INSERT INTO kernel_metadata(key, value) VALUES (?, ?)",
        (catalog_contract.SQLITE_CATALOG_STATE_KEY, value),
    )


def _insert_catalog_receipt(
    connection: sqlite3.Connection,
    catalog_id: str = "c" * 64,
    *,
    transaction_id: Any = None,
    kind: Any = catalog_contract.LEGACY_CATALOG_TRANSACTION_KIND,
    committed_at: Any = "2026-07-11T00:00:00.000Z",
) -> None:
    actual_transaction_id = (
        catalog_contract.LEGACY_CATALOG_TRANSACTION_PREFIX + catalog_id
        if transaction_id is None
        else transaction_id
    )
    connection.execute(
        "INSERT INTO transaction_receipts(transaction_id, kind, committed_at) "
        "VALUES (?, ?, ?)",
        (actual_transaction_id, kind, committed_at),
    )


def _insert_catalog_seal(
    connection: sqlite3.Connection,
    *,
    catalog_id: str = "c" * 64,
    import_id: Any = "a" * 64,
    **overrides: Any,
) -> None:
    nonempty = import_id is not None
    values = {
        "transaction_id": catalog_contract.LEGACY_CATALOG_TRANSACTION_PREFIX
        + catalog_id,
        "receipt_kind": catalog_contract.LEGACY_CATALOG_TRANSACTION_KIND,
        "receipt_protocol": catalog_contract.LEGACY_CATALOG_RECEIPT_PROTOCOL,
        "seal_protocol": catalog_contract.LEGACY_CATALOG_SEAL_PROTOCOL,
        "manifest_schema": catalog_contract.LEGACY_CATALOG_MANIFEST_SCHEMA,
        "status": catalog_contract.LEGACY_CATALOG_STATUS,
        "selection": catalog_contract.LEGACY_CATALOG_SELECTION,
        "project_count": 1 if nonempty else 0,
        "entity_count": 1 if nonempty else 0,
        "source_head_bytes": 100 if nonempty else 0,
        "row_snapshot_bytes": 6 if nonempty else 0,
        "manifest_bytes": b"{}",
        "manifest_sha256": b"m" * 32,
        "source_catalog_sha256": b"s" * 32,
        "target_coverage_sha256": b"t" * 32,
        "seal_sha256": b"z" * 32,
        "target_schema_version": catalog_contract.LEGACY_CATALOG_TARGET_SCHEMA_VERSION,
        "schema_fingerprint": _SCHEMA_FINGERPRINT_V4,
        "cutover_ready": catalog_contract.LEGACY_CATALOG_CUTOVER_READY,
    }
    values.update(overrides)
    connection.execute(
        """
        INSERT INTO legacy_catalog_seals(
            repository_singleton, catalog_id, transaction_id, receipt_kind,
            receipt_protocol, import_id, seal_protocol, manifest_schema,
            status, selection, project_count, entity_count, source_head_bytes,
            row_snapshot_bytes, manifest_bytes, manifest_sha256,
            source_catalog_sha256, target_coverage_sha256, seal_sha256,
            target_schema_version, schema_fingerprint, cutover_ready
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            catalog_id,
            values["transaction_id"],
            values["receipt_kind"],
            values["receipt_protocol"],
            import_id,
            values["seal_protocol"],
            values["manifest_schema"],
            values["status"],
            values["selection"],
            values["project_count"],
            values["entity_count"],
            values["source_head_bytes"],
            values["row_snapshot_bytes"],
            values["manifest_bytes"],
            values["manifest_sha256"],
            values["source_catalog_sha256"],
            values["target_coverage_sha256"],
            values["seal_sha256"],
            values["target_schema_version"],
            values["schema_fingerprint"],
            values["cutover_ready"],
        ),
    )


def _insert_catalog_member(
    connection: sqlite3.Connection,
    *,
    catalog_id: str = "c" * 64,
    import_id: str = "a" * 64,
    **overrides: Any,
) -> None:
    values = {
        "ordinal": 0,
        "project_id": "project-a",
        "generation": 1,
        "document_id": "document-a",
        "document_version": 1,
        "storage_version": "v2",
        "source_head_sha256": b"h" * 32,
        "source_head_bytes": 100,
        "business_root_sha256": b"r" * 32,
        "entity_count": 1,
        "row_snapshot_bytes": 6,
    }
    values.update(overrides)
    connection.execute(
        """
        INSERT INTO legacy_catalog_members(
            catalog_id, ordinal, import_id, project_id, generation,
            document_id, document_version, storage_version,
            source_head_sha256, source_head_bytes, business_root_sha256,
            entity_count, row_snapshot_bytes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            catalog_id,
            values["ordinal"],
            import_id,
            values["project_id"],
            values["generation"],
            values["document_id"],
            values["document_version"],
            values["storage_version"],
            values["source_head_sha256"],
            values["source_head_bytes"],
            values["business_root_sha256"],
            values["entity_count"],
            values["row_snapshot_bytes"],
        ),
    )


def _insert_historical_catalog(
    connection: sqlite3.Connection, *, empty: bool = False, **seal_overrides: Any
) -> None:
    _insert_catalog_receipt(connection)
    _insert_catalog_seal(
        connection,
        import_id=None if empty else "a" * 64,
        **seal_overrides,
    )
    if not empty:
        _insert_catalog_member(connection)
    _insert_catalog_state_marker(connection)


def _insert_repository_proof(
    connection: sqlite3.Connection,
    *,
    live: bool,
    deleted: bool = False,
    manifest_bytes: bytes = b"{}",
    declared_project_count: Any = None,
    omit_members: bool = False,
    live_generation: int = 1,
    live_operation_id: str = "legacy-adopt",
) -> None:
    cutover_id = "f" * 64
    transaction_id = (
        repository_contract.LEGACY_REPOSITORY_TRANSACTION_PREFIX + cutover_id
    )
    live_count = int(live)
    deleted_count = int(deleted)
    project_count = (
        live_count + deleted_count
        if declared_project_count is None
        else declared_project_count
    )
    source_state_bytes = 100 if live else 0
    connection.execute(
        "INSERT INTO transaction_receipts(transaction_id, kind, committed_at) "
        "VALUES (?, ?, '2026-07-11T00:00:00.000Z')",
        (transaction_id, repository_contract.LEGACY_REPOSITORY_TRANSACTION_KIND),
    )
    connection.execute(
        """
        INSERT INTO legacy_repository_proof_control(
            singleton, authority, authority_state, repository_epoch,
            repository_revision, catalog_revision, cutover_id,
            runtime_generation, code_digest, cutover_ready
        ) VALUES (1, 'legacy', 'cutover_prepared', ?, 11, 11, ?, ?, ?, 0)
        """,
        ("e" * 64, cutover_id, "runtime-a", b"d" * 32),
    )
    connection.execute(
        """
        INSERT INTO legacy_repository_seals(
            repository_singleton, cutover_id, transaction_id, receipt_kind,
            receipt_protocol, catalog_id, catalog_seal_sha256,
            proof_protocol, manifest_schema, status, selection,
            repository_epoch, repository_revision, catalog_revision,
            runtime_generation, code_digest, project_count,
            live_project_count, deleted_project_count, source_state_bytes,
            manifest_bytes, manifest_sha256, source_repository_sha256,
            catalog_coverage_sha256, proof_sha256, target_schema_version,
            schema_fingerprint, cutover_ready
        ) VALUES (
            1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 11, 11, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, 5, ?, 0
        )
        """,
        (
            cutover_id,
            transaction_id,
            repository_contract.LEGACY_REPOSITORY_TRANSACTION_KIND,
            repository_contract.LEGACY_REPOSITORY_RECEIPT_PROTOCOL,
            "c" * 64,
            b"z" * 32,
            repository_contract.LEGACY_REPOSITORY_PROOF_PROTOCOL,
            repository_contract.LEGACY_REPOSITORY_MANIFEST_SCHEMA,
            repository_contract.LEGACY_REPOSITORY_STATUS,
            repository_contract.LEGACY_REPOSITORY_SELECTION,
            "e" * 64,
            "runtime-a",
            b"d" * 32,
            project_count,
            live_count,
            deleted_count,
            source_state_bytes,
            manifest_bytes,
            b"m" * 32,
            b"s" * 32,
            b"v" * 32,
            b"p" * 32,
            _SCHEMA_FINGERPRINT_V5,
        ),
    )
    ordinal = 0
    if live and not omit_members:
        connection.execute(
            """
            INSERT INTO legacy_repository_members(
                cutover_id, ordinal, project_id, generation, status,
                operation_id, source_state_sha256, source_state_bytes,
                catalog_id, catalog_ordinal, import_id, document_id,
                document_version, storage_version, business_root_sha256,
                entity_count, row_snapshot_bytes
            ) VALUES (?, ?, 'project-a', ?, 'live', ?, ?, 100,
                      ?, 0, ?, 'document-a', 1, 'v2', ?, 1, 6)
            """,
            (
                cutover_id,
                ordinal,
                live_generation,
                live_operation_id,
                b"h" * 32,
                "c" * 64,
                "a" * 64,
                b"r" * 32,
            ),
        )
        ordinal += 1
    if deleted and not omit_members:
        connection.execute(
            """
            INSERT INTO legacy_repository_members(
                cutover_id, ordinal, project_id, generation, status,
                operation_id, source_state_sha256, source_state_bytes
            ) VALUES (?, ?, 'project-z', 9, 'deleted', ?, ?, 0)
            """,
            (
                cutover_id,
                ordinal,
                "1" * 32,
                __import__("hashlib").sha256(b"").digest(),
            ),
        )


def _insert_terminal_shadow_batch(
    connection: sqlite3.Connection,
    *,
    import_id: str = "a" * 64,
    project_id: str = "project-a",
    document_id: str = "document-a",
    generation: int = 1,
    document_version: int = 1,
    project_snapshot: bytes = _SNAPSHOT,
    document_snapshot: bytes = _SNAPSHOT,
    entity_snapshots: Tuple[bytes, ...] = (_SNAPSHOT,),
    source_head_bytes: int = 100,
) -> None:
    project_digest = b"r" * 32
    row_snapshot_bytes = (
        len(project_snapshot)
        + len(document_snapshot)
        + sum(len(value) for value in entity_snapshots)
    )
    _insert_import_kernel_receipt(connection, import_id)
    _insert_import_batch(
        connection,
        import_id,
        project_count=1,
        entity_count=len(entity_snapshots),
        row_snapshot_bytes=row_snapshot_bytes,
        framed_payload_bytes=row_snapshot_bytes + 101,
    )
    _insert_import_project(
        connection,
        import_id,
        project_id=project_id,
        generation=generation,
        document_id=document_id,
        document_version=document_version,
        business_root_sha256=project_digest,
        source_head_bytes=source_head_bytes,
        entity_count=len(entity_snapshots),
        row_snapshot_bytes=row_snapshot_bytes,
    )
    _insert_project(
        connection,
        project_id,
        generation=generation,
        snapshot_bytes=project_snapshot,
        snapshot_sha256=project_digest,
    )
    _insert_document(
        connection,
        project_id,
        document_id,
        document_version=document_version,
        snapshot_bytes=document_snapshot,
        snapshot_sha256=b"d" * 32,
    )
    for index, snapshot in enumerate(entity_snapshots):
        _insert_entity(
            connection,
            project_id,
            document_id,
            "item",
            "entity-%d" % index,
            snapshot_bytes=snapshot,
            snapshot_sha256=bytes([97 + index % 26]) * 32,
        )


_INITIALIZE_SCRIPT = r"""
import json
import sys
from pathlib import Path
import sqlite_kernel as kernel

def local_probe(path):
    return kernel.FilesystemInfo("test-local", str(path), True)

sys.stdout.write("READY\n")
sys.stdout.flush()
if sys.stdin.readline().strip() != "GO":
    raise SystemExit(94)
database = kernel.SqliteDatabase(sys.argv[1], _test_filesystem_probe=local_probe)
try:
    profile = database.initialize()
    result = ["ok", profile.application_id, profile.user_version]
except BaseException as exc:
    result = ["error", getattr(exc, "code", type(exc).__name__), str(exc)]
sys.stdout.write(json.dumps(result) + "\n")
sys.stdout.flush()
"""


_COMMIT_SCRIPT = r"""
import json
import sys
import sqlite_kernel as kernel

def local_probe(path):
    return kernel.FilesystemInfo("test-local", str(path), True)

def noop(_uow):
    return None

sys.stdout.write("READY\n")
sys.stdout.flush()
if sys.stdin.readline().strip() != "GO":
    raise SystemExit(94)
database = kernel.SqliteDatabase(sys.argv[1], _test_filesystem_probe=local_probe)
try:
    result = database.commit(sys.argv[2], "concurrency-test", noop)
    payload = ["ok", result.receipt.transaction_id]
except BaseException as exc:
    payload = ["error", getattr(exc, "code", type(exc).__name__)]
sys.stdout.write(json.dumps(payload) + "\n")
sys.stdout.flush()
"""


_CRASH_SCRIPT = r"""
import os
import sys
import sqlite_kernel as kernel

def local_probe(path):
    return kernel.FilesystemInfo("test-local", str(path), True)

def noop(_uow):
    return None

database = kernel.SqliteDatabase(sys.argv[1], _test_filesystem_probe=local_probe)
wanted = kernel.CrashPoint(sys.argv[3])

def crash_hook(point):
    if point is wanted:
        sys.stdout.write("BOUNDARY\n")
        sys.stdout.flush()
        if sys.stdin.readline().strip() != "GO":
            os._exit(96)
        os._exit(91)

database.commit(
    sys.argv[2],
    "crash-test",
    noop,
    _test_crash_hook=crash_hook,
)
os._exit(95)
"""


def _start_subprocess(script: str, *arguments: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", script, *arguments],
        cwd=str(Path(kernel.__file__).resolve().parent),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def _await_ready(process: subprocess.Popen[str], marker: str = "READY") -> None:
    assert process.stdout is not None
    observed = process.stdout.readline().strip()
    if observed != marker:
        _stdout, stderr = process.communicate(timeout=5)
        pytest.fail("worker did not reach %s: %r %s" % (marker, observed, stderr))


def _release_and_collect(process: subprocess.Popen[str]) -> Tuple[int, str, str]:
    assert process.stdin is not None
    process.stdin.write("GO\n")
    process.stdin.flush()
    try:
        stdout, stderr = process.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
        pytest.fail("SQLite evidence subprocess did not terminate: %s" % stderr)
    return process.returncode, stdout, stderr


def _release_all_and_collect(
    processes: List[subprocess.Popen[str]],
) -> List[Tuple[int, str, str]]:
    for process in processes:
        assert process.stdin is not None
        process.stdin.write("GO\n")
        process.stdin.flush()
    collected = []
    for process in processes:
        try:
            stdout, stderr = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            for candidate in processes:
                if candidate.poll() is None:
                    candidate.kill()
            for candidate in processes:
                candidate.wait(timeout=5)
            pytest.fail("concurrent SQLite evidence subprocess did not terminate")
        collected.append((process.returncode, stdout, stderr))
    return collected


def test_fixed_connection_profile_and_row_factory(tmp_path: Path) -> None:
    database = _database(tmp_path, busy_timeout_ms=733)
    profile = database.initialize()

    assert profile.encoding == "UTF-8"
    assert profile.journal_mode == "wal"
    assert profile.synchronous == 2
    assert profile.foreign_keys == 1
    assert profile.trusted_schema == 0
    assert profile.busy_timeout_ms == 733
    assert profile.application_id == kernel.APPLICATION_ID
    assert profile.user_version == kernel.MIGRATIONS[-1].version

    # A second, independently opened connection must receive the same profile.
    second = database.inspect_profile()
    assert second == profile
    with database._owned_connection_for_test() as owned:
        row = owned.execute("SELECT 7 AS value").fetchone()
        assert isinstance(row, sqlite3.Row)
        assert row["value"] == 7
        if os.uname().sysname == "Darwin":
            assert owned.execute("PRAGMA fullfsync").fetchone()[0] == 1
            assert owned.execute("PRAGMA checkpoint_fullfsync").fetchone()[0] == 1


def test_utf16_database_is_rejected_by_the_fixed_profile(tmp_path: Path) -> None:
    path = tmp_path / "utf16.sqlite3"
    raw = sqlite3.connect(str(path), isolation_level=None)
    raw.execute("PRAGMA encoding = 'UTF-16'")
    raw.execute("CREATE TABLE seed(value TEXT)")
    raw.close()

    connection = sqlite3.connect(str(path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    owned = kernel._OwnedConnection(connection)
    try:
        with pytest.raises(kernel.SQLiteKernelError) as rejected:
            kernel._configure_connection(owned, 5000)
        assert rejected.value.code == "sqlite_profile_mismatch"
    finally:
        owned.close()


def test_busy_timeout_settings_are_bounded_and_canonical() -> None:
    assert kernel.SQLiteSettings.from_env({}).busy_timeout_ms == 5000
    assert kernel.SQLiteSettings.from_env(
        {kernel.BUSY_TIMEOUT_ENV: "37"}
    ).busy_timeout_ms == 37
    for invalid in ("", " 5", "05", "+5", "1.0", "abc"):
        with pytest.raises(kernel.SQLiteKernelError) as caught:
            kernel.SQLiteSettings.from_env({kernel.BUSY_TIMEOUT_ENV: invalid})
        assert caught.value.code == "sqlite_busy_timeout_invalid"
    for invalid_value in (True, 0, -1, kernel.MAX_BUSY_TIMEOUT_MS + 1):
        with pytest.raises(kernel.SQLiteKernelError) as caught:
            kernel.SQLiteSettings(busy_timeout_ms=invalid_value)
        assert caught.value.code == "sqlite_busy_timeout_invalid"


def test_fresh_current_schema_is_empty_and_authority_remains_legacy(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    profile = database.initialize()
    assert profile.user_version == kernel.MIGRATIONS[-1].version == 13
    with database._owned_connection_for_test() as owned:
        names = {
            row["name"]
            for row in owned.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        assert names == {
            "schema_migrations",
            "kernel_metadata",
            "kernel_repository",
            "transaction_receipts",
            "projects",
            "documents",
            "entities",
            "legacy_import_batches",
            "legacy_import_projects",
            "legacy_catalog_seals",
            "legacy_catalog_members",
            "legacy_repository_proof_control",
            "legacy_repository_seals",
            "legacy_repository_members",
            *kernel.BUSINESS_TRANSACTION_TABLES,
            *kernel.PRODUCTION_RUNTIME_TABLES,
        }
        marker = owned.execute(
            "SELECT singleton, authority FROM kernel_repository"
        ).fetchone()
        assert tuple(marker) == (1, "legacy")
        receipt_columns = [
            row["name"]
            for row in owned.execute("PRAGMA table_info(transaction_receipts)").fetchall()
        ]
        assert receipt_columns == ["transaction_id", "kind", "committed_at"]
        ledger = [
            (row["version"], row["name"], row["checksum"])
            for row in owned.execute(
                "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        assert ledger == [
            (1, "sqlite_kernel_v1", kernel.KERNEL_MIGRATION.checksum),
            (
                2,
                "business_snapshot_schema_v1",
                kernel.BUSINESS_SNAPSHOT_MIGRATION.checksum,
            ),
            (
                3,
                "legacy_import_ledger_v1",
                kernel.LEGACY_IMPORT_LEDGER_MIGRATION.checksum,
            ),
            (
                4,
                "legacy_catalog_seal_v1",
                kernel.MIGRATIONS[3].checksum,
            ),
            (
                5,
                "legacy_repository_proof_v1",
                kernel.LEGACY_REPOSITORY_PROOF_MIGRATION.checksum,
            ),
            (
                6,
                "business_transaction_substrate_v1",
                kernel.BUSINESS_TRANSACTION_MIGRATION.checksum,
            ),
            (
                7,
                "business_local_revision_separation_v1",
                kernel.LOCAL_REVISION_MIGRATION.checksum,
            ),
            (
                8,
                "production_authority_worker_runtime_v1",
                kernel.PRODUCTION_AUTHORITY_WORKER_MIGRATION.checksum,
            ),
            (
                9,
                "worker_payload_projection_sidecars_v1",
                kernel.WORKER_PAYLOAD_PROJECTION_MIGRATION.checksum,
            ),
            (
                10,
                "runtime_timestamp_dispatch_hardening_v1",
                kernel.RUNTIME_SCHEMA_HARDENING_MIGRATION.checksum,
            ),
            (
                11,
                "mount_chronology_state_closure_v1",
                kernel.MIGRATIONS[10].checksum,
            ),
                (
                    12,
                    "cutover_install_closure_v1",
                    kernel.INSTALL_CLOSURE_MIGRATION.checksum,
                ),
                (
                    13,
                    "direct_predecessor_phase_bindings_v1",
                    kernel.MIGRATIONS[12].checksum,
                ),
        ]
        for table_name in (
            *kernel.BUSINESS_TABLES,
            *kernel.IMPORT_LEDGER_TABLES,
            *kernel.CATALOG_LEDGER_TABLES,
            *kernel.REPOSITORY_PROOF_TABLES,
            *kernel.BUSINESS_TRANSACTION_TABLES,
            *kernel.PRODUCTION_RUNTIME_TABLES,
        ):
            assert owned.execute(
                "SELECT count(*) FROM %s" % table_name
            ).fetchone()[0] == 0


def test_current_profile_fingerprint_preserves_v10_v9_v8_and_v7_history(
    tmp_path: Path,
) -> None:
    assert kernel._expected_schema_fingerprint(7) == _SCHEMA_FINGERPRINT_V7
    assert kernel._expected_schema_fingerprint(8) == _SCHEMA_FINGERPRINT_V8
    assert kernel._expected_schema_fingerprint(9) == _SCHEMA_FINGERPRINT_V9
    assert kernel._expected_schema_fingerprint(10) == _SCHEMA_FINGERPRINT_V10
    assert kernel._expected_schema_fingerprint(11) == _SCHEMA_FINGERPRINT_V11
    assert kernel._expected_schema_fingerprint(12) == _SCHEMA_FINGERPRINT_V12
    assert kernel._expected_schema_fingerprint(13) == _SCHEMA_FINGERPRINT_V13
    assert _SCHEMA_FINGERPRINT_V7 != _SCHEMA_FINGERPRINT_V8
    assert _SCHEMA_FINGERPRINT_V8 != _SCHEMA_FINGERPRINT_V9
    assert _SCHEMA_FINGERPRINT_V9 != _SCHEMA_FINGERPRINT_V10
    assert _SCHEMA_FINGERPRINT_V10 != _SCHEMA_FINGERPRINT_V11
    assert _SCHEMA_FINGERPRINT_V11 != _SCHEMA_FINGERPRINT_V12
    assert _SCHEMA_FINGERPRINT_V12 != _SCHEMA_FINGERPRINT_V13
    assert kernel._expected_schema_fingerprint() == _SCHEMA_FINGERPRINT_V13

    database = _database(tmp_path)
    profile = database.initialize()
    assert profile.user_version == 13
    with database._owned_connection_for_test() as owned:
        metadata = {
            row["key"]: row["value"]
            for row in owned.execute(
                "SELECT key, value FROM kernel_metadata "
                "WHERE key IN ('schema_fingerprint', 'kernel_schema_version')"
            ).fetchall()
        }
    assert metadata == {
        "schema_fingerprint": _SCHEMA_FINGERPRINT_V13,
        "kernel_schema_version": "13",
    }


def test_published_migration_checksums_are_frozen_and_preserve_literal_whitespace() -> None:
    assert kernel.KERNEL_MIGRATION.checksum == (
        "390305f196309ed13d84a09428b5dbec"
        "b28e300d4222c2f980e3b11902a33a26"
    )
    assert kernel.BUSINESS_SNAPSHOT_MIGRATION.checksum == (
        "e90a68e8c36513213618817e39d398a7"
        "944fbb39fc365e4abd946546164fb0fa"
    )
    assert kernel.LEGACY_IMPORT_LEDGER_MIGRATION.checksum == (
        "b5fc2a8d86f45db33d6771c9bf29e8f6"
        "f14fbced22e598340c2ff19cf1d1d590"
    )
    assert kernel.LEGACY_CATALOG_SEAL_MIGRATION.checksum == (
        "71cdb236889b5e5194c7e3f9d48c4cb7"
        "400941c324dd8c1f5bfd46e1c521adc9"
    )
    assert kernel.LEGACY_REPOSITORY_PROOF_MIGRATION.checksum == (
        "b7d543c2594a1fdc78fe18949432e2ba"
        "f6df4c447caa57317c6361ba63313eff"
        )
    assert kernel.BUSINESS_TRANSACTION_MIGRATION.checksum == (
        "41fe4ba13a35bacfa75af648381c0dad"
        "c4d256a8872c65a1bbf0138b64252254"
    )
    assert kernel.LOCAL_REVISION_MIGRATION.checksum == (
        "d96606b2d6b47ee42e53dd216e7c711e"
        "00c877573814c4cc73526004e8aa0656"
    )
    assert kernel.PRODUCTION_AUTHORITY_WORKER_MIGRATION.checksum == (
        "f6c8a8e1f2aa633a52da52a04e6b58a"
        "d11230f5093e4b883e7b63674bb8ad80b"
    )
    assert kernel.WORKER_PAYLOAD_PROJECTION_MIGRATION.checksum == (
        "23018e6b59b8d3ad235e3110abd122ba"
        "12584cee272cff0614730f4d4be0dba0"
    )
    assert kernel.RUNTIME_SCHEMA_HARDENING_MIGRATION.checksum == (
        "1be88f2630f37ca45d1e84ea7547b78e"
        "bc9da48385774d7e279b74721af81baa"
    )
    assert kernel.MIGRATIONS[10].checksum == (
        "48de0b2513175f8575a7127e54f68492"
        "2db4eb687739e0ebbab93a0a524f54cb"
    )
    assert kernel.INSTALL_CLOSURE_MIGRATION.checksum == (
        "aa61960d2bd4e10a41d1ef51767bf4f4"
        "809f5fab26cae5ebef3bfa30fccf35a2"
    )
    two_spaces = kernel.Migration(
        version=99,
        name="literal-whitespace",
        statements=("CREATE TABLE sample(value TEXT DEFAULT 'a  b')",),
    )
    one_space = kernel.Migration(
        version=99,
        name="literal-whitespace",
        statements=("CREATE TABLE sample(value TEXT DEFAULT 'a b')",),
    )
    assert two_spaces.checksum != one_space.checksum


def test_business_tables_are_strict_with_exact_columns_and_primary_keys(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    expected = {
        "projects": [
            ("repository_singleton", "INTEGER", 1, 0),
            ("project_id", "TEXT", 1, 1),
            ("generation", "INTEGER", 1, 0),
            ("codec_id", "TEXT", 1, 0),
            ("codec_version", "INTEGER", 1, 0),
            ("snapshot_format_version", "INTEGER", 1, 0),
            ("snapshot_bytes", "BLOB", 1, 0),
            ("snapshot_sha256", "BLOB", 1, 0),
        ],
        "documents": [
            ("project_id", "TEXT", 1, 1),
            ("document_id", "TEXT", 1, 0),
            ("document_version", "INTEGER", 1, 0),
            ("schema_version", "TEXT", 1, 0),
            ("collaboration_mode", "TEXT", 1, 0),
            ("save_policy", "TEXT", 0, 0),
            ("codec_id", "TEXT", 1, 0),
            ("codec_version", "INTEGER", 1, 0),
            ("snapshot_format_version", "INTEGER", 1, 0),
            ("snapshot_bytes", "BLOB", 1, 0),
            ("snapshot_sha256", "BLOB", 1, 0),
        ],
        "entities": [
            ("project_id", "TEXT", 1, 1),
            ("document_id", "TEXT", 1, 0),
            ("entity_type", "TEXT", 1, 2),
            ("entity_id", "TEXT", 1, 3),
            ("entity_version", "INTEGER", 1, 0),
            ("deleted", "INTEGER", 1, 0),
            ("codec_id", "TEXT", 1, 0),
            ("codec_version", "INTEGER", 1, 0),
            ("snapshot_format_version", "INTEGER", 1, 0),
            ("snapshot_bytes", "BLOB", 1, 0),
            ("snapshot_sha256", "BLOB", 1, 0),
        ],
    }
    connection = _raw_database(database)
    try:
        strict = {
            row["name"]: row["strict"]
            for row in connection.execute("PRAGMA table_list").fetchall()
        }
        for table_name, wanted in expected.items():
            assert strict[table_name] == 1
            actual = [
                (row["name"], row["type"], row["notnull"], row["pk"])
                for row in connection.execute(
                    "PRAGMA table_info(%s)" % table_name
                ).fetchall()
            ]
            assert actual == wanted
    finally:
        connection.close()


def test_import_ledger_is_strict_private_and_has_exact_relational_shape(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    expected = {
        "legacy_import_batches": [
            ("import_id", "TEXT", 1, 1),
            ("transaction_id", "TEXT", 1, 0),
            ("receipt_kind", "TEXT", 1, 0),
            ("receipt_protocol", "TEXT", 1, 0),
            ("status", "TEXT", 1, 0),
            ("manifest_schema", "TEXT", 1, 0),
            ("batch_protocol", "TEXT", 1, 0),
            ("payload_protocol", "TEXT", 1, 0),
            ("selection", "TEXT", 1, 0),
            ("project_count", "INTEGER", 1, 0),
            ("entity_count", "INTEGER", 1, 0),
            ("row_snapshot_bytes", "INTEGER", 1, 0),
            ("framed_payload_bytes", "INTEGER", 1, 0),
            ("manifest_bytes", "BLOB", 1, 0),
            ("manifest_sha256", "BLOB", 1, 0),
            ("payload_sha256", "BLOB", 1, 0),
            ("batch_sha256", "BLOB", 1, 0),
        ],
        "legacy_import_projects": [
            ("import_id", "TEXT", 1, 1),
            ("ordinal", "INTEGER", 1, 0),
            ("project_id", "TEXT", 1, 2),
            ("generation", "INTEGER", 1, 0),
            ("document_id", "TEXT", 1, 0),
            ("document_version", "INTEGER", 1, 0),
            ("storage_version", "TEXT", 1, 0),
            ("source_head_sha256", "BLOB", 1, 0),
            ("source_head_bytes", "INTEGER", 1, 0),
            ("business_root_sha256", "BLOB", 1, 0),
            ("entity_count", "INTEGER", 1, 0),
            ("row_snapshot_bytes", "INTEGER", 1, 0),
        ],
    }
    with database._owned_connection_for_test() as owned:
        strict = {
            row["name"]: row["strict"]
            for row in owned.execute("PRAGMA table_list").fetchall()
        }
        for table_name, wanted in expected.items():
            assert strict[table_name] == 1
            assert [
                (row["name"], row["type"], row["notnull"], row["pk"])
                for row in owned.execute(
                    "PRAGMA table_info(%s)" % table_name
                ).fetchall()
            ] == wanted

        receipt_index = owned.execute(
            "PRAGMA index_list(transaction_receipts)"
        ).fetchall()
        matching = [
            row for row in receipt_index
            if row["name"] == "transaction_receipts_identity_kind"
        ]
        assert len(matching) == 1 and matching[0]["unique"] == 1
        assert [
            row["name"]
            for row in owned.execute(
                "PRAGMA index_info(transaction_receipts_identity_kind)"
            ).fetchall()
        ] == ["transaction_id", "kind"]
        batch_index_columns = {
            tuple(
                column["name"]
                for column in owned.execute(
                    "PRAGMA index_info(%s)" % row["name"]
                ).fetchall()
            )
            for row in owned.execute(
                "PRAGMA index_list(legacy_import_batches)"
            ).fetchall()
        }
        assert not any(
            digest in columns
            for digest in (
                "manifest_sha256", "payload_sha256", "batch_sha256",
            )
            for columns in batch_index_columns
        )

        batch_foreign_keys = owned.execute(
            "PRAGMA foreign_key_list(legacy_import_batches)"
        ).fetchall()
        assert [
            (row["table"], row["from"], row["to"], row["on_update"], row["on_delete"])
            for row in batch_foreign_keys
        ] == [
            ("transaction_receipts", "transaction_id", "transaction_id", "RESTRICT", "RESTRICT"),
            ("transaction_receipts", "receipt_kind", "kind", "RESTRICT", "RESTRICT"),
        ]
        project_foreign_keys = owned.execute(
            "PRAGMA foreign_key_list(legacy_import_projects)"
        ).fetchall()
        assert [
            (row["table"], row["from"], row["to"], row["on_update"], row["on_delete"])
            for row in project_foreign_keys
        ] == [
            ("legacy_import_batches", "import_id", "import_id", "RESTRICT", "RESTRICT"),
        ]
        ddl = {
            row["name"]: row["sql"]
            for row in owned.execute(
                "SELECT name, sql FROM sqlite_schema WHERE name IN (?, ?) ORDER BY name",
                kernel.IMPORT_LEDGER_TABLES,
            ).fetchall()
        }
        assert all("DEFERRABLE INITIALLY DEFERRED" in sql for sql in ddl.values())

    assert set(kernel.IMPORT_LEDGER_TABLES).issubset(kernel.PRIVATE_TABLES)


def test_import_ledger_deferred_binding_and_revision_scoped_uniqueness(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    connection = _raw_database(database)
    first = "a" * 64
    second = "b" * 64
    try:
        connection.execute("BEGIN")
        _insert_import_project(connection, first)
        _insert_import_batch(connection, first)
        _insert_import_kernel_receipt(connection, first)
        connection.execute("COMMIT")

        # A later revision may describe the same project/document under a new
        # import id; DDL must not permanently forbid the future adapter policy.
        connection.execute("BEGIN")
        _insert_import_project(connection, second)
        _insert_import_batch(connection, second)
        _insert_import_kernel_receipt(connection, second)
        connection.execute("COMMIT")

        with pytest.raises(sqlite3.IntegrityError):
            _insert_import_project(
                connection,
                first,
                ordinal=0,
                project_id="project-other",
                document_id="document-other",
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_import_project(
                connection,
                first,
                ordinal=1,
                project_id="project-other",
                document_id="document-a",
            )

        connection.execute("BEGIN")
        _insert_import_project(connection, "c" * 64)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("COMMIT")
        connection.execute("ROLLBACK")

        wrong_kind = "d" * 64
        connection.execute("BEGIN")
        _insert_import_batch(connection, wrong_kind)
        _insert_import_kernel_receipt(
            connection, wrong_kind, kind="ordinary-kind",
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("COMMIT")
        connection.execute("ROLLBACK")
    finally:
        connection.close()


@pytest.mark.parametrize(
    "overrides",
    (
        {"transaction_id": "ordinary-id"},
        {"receipt_kind": "ordinary-kind"},
        {"receipt_protocol": "wrong"},
        {"status": "prepared"},
        {"manifest_schema": "wrong"},
        {"batch_protocol": "wrong"},
        {"payload_protocol": "wrong"},
        {"selection": "catalog"},
        {"project_count": 0},
        {"project_count": import_contract.MAX_LEGACY_PROJECTS + 1},
        {"entity_count": -1},
        {"entity_count": import_contract.MAX_LEGACY_ENTITIES + 1},
        {"row_snapshot_bytes": 0},
        {"framed_payload_bytes": 100},
        {"manifest_bytes": b""},
        {"manifest_bytes": "{}"},
        {"manifest_sha256": b"short"},
        {"payload_sha256": b"short"},
        {"batch_sha256": b"short"},
    ),
)
def test_import_batch_checks_reject_invalid_values(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    database = _database(tmp_path)
    database.initialize()
    connection = _raw_database(database)
    try:
        _insert_import_kernel_receipt(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_import_batch(connection, **overrides)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "import_id", ("a" * 63, "A" * 64, "g" * 64, "a" * 64 + "\x00")
)
def test_import_id_must_be_exact_lower_hex(tmp_path: Path, import_id: str) -> None:
    database = _database(tmp_path)
    database.initialize()
    connection = _raw_database(database)
    try:
        _insert_import_kernel_receipt(connection, import_id)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_import_batch(connection, import_id)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "overrides",
    (
        {"ordinal": -1},
        {"ordinal": import_contract.MAX_LEGACY_PROJECTS},
        {"project_id": ""},
        {"project_id": "p" * 161},
        {"generation": 0},
        {"document_id": ""},
        {"document_version": 0},
        {"storage_version": "future"},
        {"source_head_sha256": b"short"},
        {"source_head_bytes": 0},
        {"business_root_sha256": b"short"},
        {"entity_count": -1},
        {"row_snapshot_bytes": 0},
    ),
)
def test_import_project_checks_reject_invalid_values(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    database = _database(tmp_path)
    database.initialize()
    connection = _raw_database(database)
    try:
        _insert_import_kernel_receipt(connection)
        _insert_import_batch(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_import_project(connection, **overrides)
    finally:
        connection.close()


def test_business_foreign_keys_are_composite_deferred_and_cascade(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    connection = _raw_database(database)
    try:
        foreign_keys = {}
        for table_name in ("projects", "documents", "entities"):
            foreign_keys[table_name] = [
                (
                    row["id"],
                    row["seq"],
                    row["table"],
                    row["from"],
                    row["to"],
                    row["on_update"],
                    row["on_delete"],
                )
                for row in connection.execute(
                    "PRAGMA foreign_key_list(%s)" % table_name
                ).fetchall()
            ]
        assert foreign_keys == {
            "projects": [
                (
                    0,
                    0,
                    "kernel_repository",
                    "repository_singleton",
                    "singleton",
                    "RESTRICT",
                    "RESTRICT",
                )
            ],
            "documents": [
                (0, 0, "projects", "project_id", "project_id", "RESTRICT", "CASCADE")
            ],
            "entities": [
                (0, 0, "documents", "project_id", "project_id", "RESTRICT", "CASCADE"),
                (0, 1, "documents", "document_id", "document_id", "RESTRICT", "CASCADE"),
            ],
        }

        # The composite entity FK is deferred: child-before-parent is legal
        # inside one transaction, but the pair must be complete at COMMIT.
        _insert_project(connection, "p-deferred")
        connection.execute("BEGIN")
        _insert_entity(connection, "p-deferred", "d-deferred", "item", "e-1")
        _insert_document(connection, "p-deferred", "d-deferred")
        connection.execute("COMMIT")

        _insert_project(connection, "p-broken")
        connection.execute("BEGIN")
        _insert_entity(connection, "p-broken", "missing-document", "item", "e-2")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("COMMIT")
        connection.execute("ROLLBACK")

        # Document deletion cascades to entities without deleting the project.
        connection.execute(
            "DELETE FROM documents WHERE project_id='p-deferred'"
        )
        assert connection.execute(
            "SELECT count(*) FROM projects WHERE project_id='p-deferred'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM entities WHERE project_id='p-deferred'"
        ).fetchone()[0] == 0

        # Recreate the chain, then prove the project cascade reaches both levels.
        _insert_document(connection, "p-deferred", "d-deferred-2")
        _insert_entity(connection, "p-deferred", "d-deferred-2", "item", "e-3")
        connection.execute("DELETE FROM projects WHERE project_id='p-deferred'")
        assert connection.execute(
            "SELECT count(*) FROM documents WHERE project_id='p-deferred'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM entities WHERE project_id='p-deferred'"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_business_tables_enforce_checks_strict_types_and_primary_keys(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    connection = _raw_database(database)
    try:
        for invalid_project in (
            lambda: _insert_project(connection, "", generation=1),
            lambda: _insert_project(connection, "p-generation-zero", generation=0),
            lambda: _insert_project(connection, "p-generation-text", generation="bad"),
            lambda: _insert_project(connection, "p-empty-codec", codec_id=""),
            lambda: _insert_project(connection, "p-empty-payload", snapshot_bytes=b""),
            lambda: _insert_project(connection, "p-short-sha", snapshot_sha256=b"short"),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                invalid_project()
        _insert_project(connection, "p-valid")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_project(connection, "p-valid")

        for invalid_document in (
            lambda: _insert_document(connection, "p-valid", "", document_version=1),
            lambda: _insert_document(connection, "p-valid", "d-zero", document_version=0),
            lambda: _insert_document(connection, "p-valid", "d-text", document_version="bad"),
            lambda: _insert_document(
                connection, "p-valid", "d-mode", collaboration_mode="invalid"
            ),
            lambda: _insert_document(
                connection, "p-valid", "d-policy", save_policy="manual"
            ),
            lambda: _insert_document(
                connection, "p-valid", "d-text-payload", snapshot_bytes="{}"
            ),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                invalid_document()
        _insert_document(connection, "p-valid", "d-valid")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_document(connection, "p-valid", "d-other")
        _insert_project(connection, "p-second")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_document(connection, "p-second", "d-valid")

        for invalid_entity in (
            lambda: _insert_entity(connection, "p-valid", "d-valid", "", "e"),
            lambda: _insert_entity(connection, "p-valid", "d-valid", "item", ""),
            lambda: _insert_entity(
                connection,
                "p-valid",
                "d-valid",
                "item",
                "e-zero",
                entity_version=0,
            ),
            lambda: _insert_entity(
                connection, "p-valid", "d-valid", "item", "e-deleted", deleted=2
            ),
            lambda: _insert_entity(
                connection, "p-valid", "d-valid", "item", "e-text", deleted="bad"
            ),
            lambda: _insert_entity(
                connection,
                "p-valid",
                "d-valid",
                "item",
                "e-sha",
                snapshot_sha256=b"short",
            ),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                invalid_entity()
        _insert_entity(connection, "p-valid", "d-valid", "item", "e-valid")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_entity(connection, "p-valid", "d-valid", "item", "e-valid")
    finally:
        connection.close()


def test_real_v1_to_v4_upgrade_preserves_receipt_and_legacy_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    all_migrations = kernel.MIGRATIONS
    monkeypatch.setattr(kernel, "MIGRATIONS", (kernel.KERNEL_MIGRATION,))
    v1_profile = database.initialize()
    assert v1_profile.user_version == 1
    old_receipt = database.commit("v1-receipt", "upgrade-test", _noop).receipt
    with database._owned_connection_for_test() as owned:
        before_marker = tuple(
            owned.execute(
                "SELECT authority, created_at FROM kernel_repository WHERE singleton=1"
            ).fetchone()
        )

    monkeypatch.setattr(kernel, "MIGRATIONS", all_migrations)
    upgraded = database.initialize()
    assert upgraded.user_version == 13
    reconciled = database.reconcile("v1-receipt")
    assert reconciled.status is kernel.ReconcileStatus.COMMITTED
    assert reconciled.receipt == old_receipt
    with database._owned_connection_for_test() as owned:
        after_marker = tuple(
            owned.execute(
                "SELECT authority, created_at FROM kernel_repository WHERE singleton=1"
            ).fetchone()
        )
        assert after_marker == before_marker
        assert after_marker[0] == "legacy"
        assert [
            row["version"]
            for row in owned.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
            ] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]
        for table_name in (*kernel.BUSINESS_TABLES, *kernel.IMPORT_LEDGER_TABLES):
            assert owned.execute(
                "SELECT count(*) FROM %s" % table_name
            ).fetchone()[0] == 0


def test_real_v2_to_v4_upgrade_preserves_receipt_and_legacy_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    all_migrations = kernel.MIGRATIONS
    monkeypatch.setattr(kernel, "MIGRATIONS", all_migrations[:2])
    assert database.initialize().user_version == 2
    old_receipt = database.commit("v2-receipt", "upgrade-test", _noop).receipt
    with database._owned_connection_for_test() as owned:
        before_marker = tuple(owned.execute(
            "SELECT authority, created_at FROM kernel_repository WHERE singleton=1"
        ).fetchone())

    monkeypatch.setattr(kernel, "MIGRATIONS", all_migrations)
    assert database.initialize().user_version == 13
    reconciled = database.reconcile("v2-receipt")
    assert reconciled.status is kernel.ReconcileStatus.COMMITTED
    assert reconciled.receipt == old_receipt
    with database._owned_connection_for_test() as owned:
        assert tuple(owned.execute(
            "SELECT authority, created_at FROM kernel_repository WHERE singleton=1"
        ).fetchone()) == before_marker
        assert [
            row["version"]
            for row in owned.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
            ] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]


def test_real_v3_generic_namespace_cannot_preoccupy_private_catalog_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    all_migrations = kernel.MIGRATIONS
    monkeypatch.setattr(kernel, "MIGRATIONS", all_migrations[:3])
    database = _database(tmp_path)
    assert database.initialize().user_version == 3

    private_transaction_id = (
        catalog_contract.LEGACY_CATALOG_TRANSACTION_PREFIX + "a" * 64
    )
    calls: List[int] = []
    with pytest.raises(kernel.SQLiteKernelError) as private_id:
        database.commit(
            private_transaction_id,
            "ordinary-kind",
            lambda _uow: calls.append(1),
        )
    assert private_id.value.code == "sqlite_transaction_id_invalid"
    with pytest.raises(kernel.SQLiteKernelError) as private_kind:
        database.commit(
            "ordinary-private-kind",
            catalog_contract.LEGACY_CATALOG_TRANSACTION_KIND,
            lambda _uow: calls.append(1),
        )
    assert private_kind.value.code == "sqlite_kind_invalid"
    assert calls == []

    # The old public-looking spellings remain ordinary generic identities.
    # They do not collide with the new underscore-prefixed private namespace.
    old_lookalike = "legacy-catalog-seal:" + "b" * 64
    ordinary = database.commit(
        old_lookalike,
        "legacy_catalog_seal_v1",
        _noop,
    ).receipt

    monkeypatch.setattr(kernel, "MIGRATIONS", all_migrations)
    assert database.initialize().user_version == 13
    assert database.reconcile(old_lookalike).receipt == ordinary
    with database._owned_connection_for_test() as owned:
        assert owned.execute(
            "SELECT count(*) FROM transaction_receipts WHERE transaction_id=?",
            (private_transaction_id,),
        ).fetchone()[0] == 0
        assert owned.execute(
            "SELECT count(*) FROM legacy_catalog_seals"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("dirty_state", ("business", "reserved_receipt"))
def test_dirty_v2_blocks_v3_and_rolls_back_every_schema_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dirty_state: str,
) -> None:
    database = _database(tmp_path)
    all_migrations = kernel.MIGRATIONS
    monkeypatch.setattr(kernel, "MIGRATIONS", all_migrations[:2])
    assert database.initialize().user_version == 2
    raw = _raw_database(database)
    try:
        if dirty_state == "business":
            _insert_project(raw, "dirty-v2-project")
        else:
            _insert_import_kernel_receipt(raw)
    finally:
        raw.close()

    monkeypatch.setattr(kernel, "MIGRATIONS", all_migrations)
    with pytest.raises(kernel.SQLiteKernelError) as caught:
        database.initialize()
    assert caught.value.code == "sqlite_legacy_import_migration_dirty"

    raw = sqlite3.connect(str(database.path))
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 2
        assert raw.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,)]
        names = {
            row[0]
            for row in raw.execute(
                "SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        assert names.isdisjoint({
            "legacy_import_batches",
            "legacy_import_projects",
            "transaction_receipts_identity_kind",
        })
        metadata = dict(raw.execute(
            "SELECT key, value FROM kernel_metadata"
        ).fetchall())
        assert metadata["kernel_schema_version"] == "2"
    finally:
        raw.close()


def test_v1_fingerprint_tamper_blocks_v3_without_advancing_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    all_migrations = kernel.MIGRATIONS
    monkeypatch.setattr(kernel, "MIGRATIONS", (kernel.KERNEL_MIGRATION,))
    assert database.initialize().user_version == 1
    raw = sqlite3.connect(str(database.path), isolation_level=None)
    try:
        raw.execute(
            "UPDATE kernel_metadata SET value=? WHERE key='schema_fingerprint'",
            ("0" * 64,),
        )
    finally:
        raw.close()

    monkeypatch.setattr(kernel, "MIGRATIONS", all_migrations)
    with pytest.raises(kernel.SQLiteKernelError) as caught:
        database.initialize()
    assert caught.value.code == "sqlite_kernel_metadata_drift"

    raw = sqlite3.connect(str(database.path))
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 1
        names = {
            row[0]
            for row in raw.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        assert names == {
            "schema_migrations",
            "kernel_metadata",
            "kernel_repository",
            "transaction_receipts",
        }
        assert raw.execute(
            "SELECT count(*) FROM schema_migrations WHERE version=2"
        ).fetchone()[0] == 0
        assert raw.execute(
            "SELECT count(*) FROM schema_migrations WHERE version=3"
        ).fetchone()[0] == 0
    finally:
        raw.close()


def test_v1_actual_ddl_drift_blocks_v3_without_creating_business_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    all_migrations = kernel.MIGRATIONS
    monkeypatch.setattr(kernel, "MIGRATIONS", (kernel.KERNEL_MIGRATION,))
    assert database.initialize().user_version == 1
    raw = sqlite3.connect(str(database.path), isolation_level=None)
    try:
        raw.execute("CREATE TABLE injected_drift(value TEXT)")
    finally:
        raw.close()

    monkeypatch.setattr(kernel, "MIGRATIONS", all_migrations)
    with pytest.raises(kernel.SQLiteKernelError) as caught:
        database.initialize()
    assert caught.value.code == "sqlite_schema_drift"

    raw = sqlite3.connect(str(database.path))
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 1
        names = {
            row[0]
            for row in raw.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        assert "injected_drift" in names
        assert names.isdisjoint({
            "projects", "documents", "entities",
            "legacy_import_batches", "legacy_import_projects",
        })
        assert raw.execute(
            "SELECT count(*) FROM schema_migrations WHERE version=2"
        ).fetchone()[0] == 0
        assert raw.execute(
            "SELECT count(*) FROM schema_migrations WHERE version=3"
        ).fetchone()[0] == 0
    finally:
        raw.close()


def test_old_v1_registry_fails_closed_when_opening_v3_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    database.initialize()
    monkeypatch.setattr(kernel, "MIGRATIONS", (kernel.KERNEL_MIGRATION,))

    with pytest.raises(kernel.SQLiteKernelError) as initialize_error:
        database.initialize()
    assert initialize_error.value.code == "sqlite_schema_version_unsupported"
    with pytest.raises(kernel.SQLiteKernelError) as inspect_error:
        database.inspect_profile()
    assert inspect_error.value.code == "sqlite_schema_version_unsupported"


def test_v7_registry_remains_an_exact_historical_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    all_migrations = kernel.MIGRATIONS
    monkeypatch.setattr(kernel, "MIGRATIONS", all_migrations[:7])
    database = _database(tmp_path)
    assert database.initialize().user_version == 7
    with database._owned_connection_for_test() as owned:
        assert owned.execute(
            "SELECT value FROM kernel_metadata WHERE key='schema_fingerprint'"
        ).fetchone()[0] == _SCHEMA_FINGERPRINT_V7
        production_objects = owned.execute(
            "SELECT count(*) FROM sqlite_schema WHERE name IN (%s)"
            % ",".join("?" for _table in kernel.PRODUCTION_RUNTIME_TABLES),
            kernel.PRODUCTION_RUNTIME_TABLES,
        ).fetchone()[0]
        assert production_objects == 0

    monkeypatch.setattr(kernel, "MIGRATIONS", all_migrations)
    assert database.initialize().user_version == 13


def test_old_v7_registry_fails_closed_when_opening_current_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    all_migrations = kernel.MIGRATIONS
    database = _database(tmp_path)
    assert database.initialize().user_version == 13
    monkeypatch.setattr(kernel, "MIGRATIONS", all_migrations[:7])

    with pytest.raises(kernel.SQLiteKernelError) as initialize_error:
        database.initialize()
    assert initialize_error.value.code == "sqlite_schema_version_unsupported"
    with pytest.raises(kernel.SQLiteKernelError) as inspect_error:
        database.inspect_profile()
    assert inspect_error.value.code == "sqlite_schema_version_unsupported"
    callback_calls: List[int] = []
    with pytest.raises(kernel.SQLiteKernelError) as commit_error:
        database.commit(
            "old-v7-binary",
            "ordinary",
            lambda _uow: callback_calls.append(1),
        )
    assert commit_error.value.code == "sqlite_schema_version_unsupported"
    assert callback_calls == []


def test_old_v8_registry_fails_closed_before_callback_on_v9_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    all_migrations = kernel.MIGRATIONS
    monkeypatch.setattr(kernel, "MIGRATIONS", all_migrations[:9])
    database = _database(tmp_path)
    assert database.initialize().user_version == 9
    monkeypatch.setattr(kernel, "MIGRATIONS", all_migrations[:8])

    with pytest.raises(kernel.SQLiteKernelError) as initialize_error:
        database.initialize()
    assert initialize_error.value.code == "sqlite_schema_version_unsupported"
    with pytest.raises(kernel.SQLiteKernelError) as inspect_error:
        database.inspect_profile()
    assert inspect_error.value.code == "sqlite_schema_version_unsupported"
    callback_calls: List[int] = []
    with pytest.raises(kernel.SQLiteKernelError) as commit_error:
        database.commit(
            "old-v8-binary",
            "ordinary",
            lambda _uow: callback_calls.append(1),
        )
    assert commit_error.value.code == "sqlite_schema_version_unsupported"
    assert callback_calls == []


def test_initialize_detects_migration_checksum_tampering(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.initialize()
    raw = sqlite3.connect(str(database.path))
    try:
        raw.execute("UPDATE schema_migrations SET checksum=? WHERE version=1", ("0" * 64,))
        raw.commit()
    finally:
        raw.close()

    with pytest.raises(kernel.SQLiteKernelError) as caught:
        database.initialize()
    assert caught.value.code == "sqlite_migration_checksum_mismatch"


def test_initialize_detects_application_id_mismatch(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.path.parent.mkdir(mode=0o700, parents=True)
    raw = sqlite3.connect(str(database.path))
    try:
        raw.execute("PRAGMA application_id = 123456")
        raw.commit()
    finally:
        raw.close()

    with pytest.raises(kernel.SQLiteKernelError) as caught:
        database.initialize()
    assert caught.value.code == "sqlite_application_id_mismatch"


def test_initialize_detects_user_version_mismatch(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.initialize()
    raw = sqlite3.connect(str(database.path))
    try:
        raw.execute("PRAGMA user_version = 99")
        raw.commit()
    finally:
        raw.close()

    with pytest.raises(kernel.SQLiteKernelError) as caught:
        database.initialize()
    assert caught.value.code == "sqlite_schema_version_unsupported"


def test_initialize_detects_schema_and_authority_drift(tmp_path: Path) -> None:
    schema_database = _database(tmp_path / "schema")
    schema_database.initialize()
    raw = sqlite3.connect(str(schema_database.path))
    try:
        raw.execute("CREATE TABLE injected(value TEXT)")
        raw.commit()
    finally:
        raw.close()
    with pytest.raises(kernel.SQLiteKernelError) as caught:
        schema_database.initialize()
    assert caught.value.code == "sqlite_schema_drift"

    authority_database = _database(tmp_path / "authority")
    authority_database.initialize()
    raw = sqlite3.connect(str(authority_database.path))
    try:
        raw.execute("UPDATE kernel_repository SET authority='sqlite' WHERE singleton=1")
        raw.commit()
    finally:
        raw.close()
    with pytest.raises(kernel.SQLiteKernelError) as caught:
        authority_database.initialize()
    assert caught.value.code == "sqlite_authority_marker_invalid"


def test_failed_migration_rolls_back_ddl_ledger_and_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = kernel.MIGRATIONS[0]
    broken = kernel.Migration(
        version=1,
        name="broken-v1",
        statements=original.statements
        + ("CREATE TABLE half_applied(value TEXT)", "CREATE TABL invalid(value TEXT)"),
    )
    monkeypatch.setattr(kernel, "MIGRATIONS", (broken,))
    database = _database(tmp_path)

    with pytest.raises(sqlite3.Error):
        database.initialize()

    raw = sqlite3.connect(str(database.path))
    try:
        assert raw.execute("PRAGMA application_id").fetchone()[0] == 0
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 0
        assert raw.execute(
            "SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall() == []
    finally:
        raw.close()


def test_concurrent_initialize_is_process_safe(tmp_path: Path) -> None:
    database = _database(tmp_path)
    worker_count = 4
    processes = [
        _start_subprocess(_INITIALIZE_SCRIPT, str(database.path))
        for _index in range(worker_count)
    ]
    for process in processes:
        _await_ready(process)
    collected = _release_all_and_collect(processes)

    assert all(returncode == 0 and not stderr for returncode, _stdout, stderr in collected)
    observed = [json.loads(stdout.strip()) for _code, stdout, _stderr in collected]
    assert observed == [
        ["ok", kernel.APPLICATION_ID, kernel.MIGRATIONS[-1].version]
    ] * worker_count
    database.inspect_profile()


def test_concurrent_processes_upgrade_one_v1_database_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    all_migrations = kernel.MIGRATIONS
    monkeypatch.setattr(kernel, "MIGRATIONS", (kernel.KERNEL_MIGRATION,))
    assert database.initialize().user_version == 1
    monkeypatch.setattr(kernel, "MIGRATIONS", all_migrations)

    worker_count = 4
    processes = [
        _start_subprocess(_INITIALIZE_SCRIPT, str(database.path))
        for _index in range(worker_count)
    ]
    for process in processes:
        _await_ready(process)
    collected = _release_all_and_collect(processes)

    assert all(returncode == 0 and not stderr for returncode, _stdout, stderr in collected)
    observed = [json.loads(stdout.strip()) for _code, stdout, _stderr in collected]
    assert observed == [["ok", kernel.APPLICATION_ID, 13]] * worker_count
    with database._owned_connection_for_test() as owned:
        rows = owned.execute(
            "SELECT version, count(*) AS count FROM schema_migrations "
            "GROUP BY version ORDER BY version"
        ).fetchall()
        assert [(row["version"], row["count"]) for row in rows] == [
            (1, 1), (2, 1), (3, 1), (4, 1), (5, 1), (6, 1), (7, 1),
                (8, 1), (9, 1), (10, 1), (11, 1), (12, 1), (13, 1),
            ]


def test_uow_forbids_kernel_transaction_schema_and_pragma_controls(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    forbidden = (
        "PRAGMA user_version = 9",
        "COMMIT",
        "CREATE TABLE escaped(value TEXT)",
        "ATTACH DATABASE ':memory:' AS escaped",
        "UPDATE kernel_repository SET authority='sqlite' WHERE singleton=1",
        "DELETE FROM transaction_receipts",
    )
    for index, statement in enumerate(forbidden):
        transaction_id = "forbidden-%d" % index
        with pytest.raises(kernel.SQLiteKernelError) as caught:
            database.commit(
                transaction_id,
                "uow-test",
                lambda uow, sql=statement: uow.execute(sql),
            )
        assert caught.value.code == "sqlite_uow_statement_forbidden"
        assert database.reconcile(transaction_id).status is kernel.ReconcileStatus.NOT_COMMITTED


@pytest.mark.parametrize(
    ("transaction_id", "kind", "expected_code"),
    (
        (
            import_contract.SQLITE_IMPORT_TRANSACTION_PREFIX + "a" * 64,
            "ordinary-kind",
            "sqlite_transaction_namespace_reserved",
        ),
        (
            "ordinary-id",
            import_contract.SQLITE_IMPORT_TRANSACTION_KIND,
            "sqlite_transaction_namespace_reserved",
        ),
        (
            import_contract.SQLITE_IMPORT_TRANSACTION_PREFIX + "b" * 64,
            import_contract.SQLITE_IMPORT_TRANSACTION_KIND,
            "sqlite_transaction_namespace_reserved",
        ),
        (
            catalog_contract.LEGACY_CATALOG_TRANSACTION_PREFIX + "c" * 64,
            "ordinary-kind",
            "sqlite_transaction_id_invalid",
        ),
        (
            "ordinary-catalog-id",
            catalog_contract.LEGACY_CATALOG_TRANSACTION_KIND,
            "sqlite_kind_invalid",
        ),
        (
            catalog_contract.LEGACY_CATALOG_TRANSACTION_PREFIX + "d" * 64,
            catalog_contract.LEGACY_CATALOG_TRANSACTION_KIND,
            "sqlite_transaction_id_invalid",
        ),
    ),
)
def test_generic_commit_rejects_legacy_evidence_transaction_namespaces(
    tmp_path: Path, transaction_id: str, kind: str, expected_code: str
) -> None:
    database = _database(tmp_path)
    database.initialize()
    callback_calls: List[int] = []
    with pytest.raises(kernel.SQLiteKernelError) as caught:
        database.commit(
            transaction_id,
            kind,
            lambda _uow: callback_calls.append(1),
        )
    assert caught.value.code == expected_code
    assert callback_calls == []
    if transaction_id.startswith(catalog_contract.LEGACY_CATALOG_TRANSACTION_PREFIX):
        with pytest.raises(kernel.SQLiteKernelError) as invalid:
            database.reconcile(transaction_id)
        assert invalid.value.code == "sqlite_transaction_id_invalid"
    else:
        reconciled = database.reconcile(transaction_id)
    if transaction_id.startswith(import_contract.SQLITE_IMPORT_TRANSACTION_PREFIX):
        assert reconciled.status is kernel.ReconcileStatus.INDETERMINATE
        assert reconciled.error_code == "sqlite_transaction_namespace_reserved"
    elif not transaction_id.startswith(
        catalog_contract.LEGACY_CATALOG_TRANSACTION_PREFIX
    ):
        assert reconciled.status is kernel.ReconcileStatus.NOT_COMMITTED


def test_identity_str_subclass_cannot_override_reserved_namespace_checks(
    tmp_path: Path,
) -> None:
    class ForgedIdentity(str):
        def startswith(self, _prefix: object, *args: object) -> bool:
            del args
            return False

    class ForgedKind(str):
        def __eq__(self, _other: object) -> bool:
            return False

    database = _database(tmp_path)
    database.initialize()
    raw_id = import_contract.SQLITE_IMPORT_TRANSACTION_PREFIX + "e" * 64
    callback_calls: List[int] = []
    with pytest.raises(kernel.SQLiteKernelError) as caught:
        database.commit(
            ForgedIdentity(raw_id),
            "ordinary-kind",
            lambda _uow: callback_calls.append(1),
        )
    assert caught.value.code == "sqlite_transaction_id_invalid"
    with pytest.raises(kernel.SQLiteKernelError) as kind_caught:
        database.commit(
            "ordinary-id",
            ForgedKind(import_contract.SQLITE_IMPORT_TRANSACTION_KIND),
            lambda _uow: callback_calls.append(1),
        )
    assert kind_caught.value.code == "sqlite_kind_invalid"
    assert callback_calls == []
    with database._owned_connection_for_test() as owned:
        assert owned.execute(
            "SELECT count(*) FROM transaction_receipts WHERE transaction_id=?",
            (raw_id,),
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("table_name", "statement"),
    (
        (
            "projects",
            """
            INSERT INTO projects(
                repository_singleton, project_id, generation,
                codec_id, codec_version, snapshot_format_version,
                snapshot_bytes, snapshot_sha256
            ) VALUES (1, 'p', 1, 'json', 1, 1, X'7B7D', zeroblob(32))
            """,
        ),
        (
            "documents",
            """
            INSERT INTO documents(
                project_id, document_id, document_version, schema_version,
                collaboration_mode, save_policy, codec_id, codec_version,
                snapshot_format_version, snapshot_bytes, snapshot_sha256
            ) VALUES (
                'p', 'd', 1, '1.0.0', 'live_collab', NULL,
                'json', 1, 1, X'7B7D', zeroblob(32)
            )
            """,
        ),
        (
            "entities",
            """
            INSERT INTO entities(
                project_id, document_id, entity_type, entity_id,
                entity_version, deleted, codec_id, codec_version,
                snapshot_format_version, snapshot_bytes, snapshot_sha256
            ) VALUES (
                'p', 'd', 'item', 'e', 1, 0,
                'json', 1, 1, X'7B7D', zeroblob(32)
            )
            """,
        ),
        (
            "legacy_import_batches",
            """
            INSERT INTO legacy_import_batches(
                import_id, transaction_id, receipt_kind, receipt_protocol,
                status, manifest_schema, batch_protocol, payload_protocol,
                selection, project_count, entity_count, row_snapshot_bytes,
                framed_payload_bytes, manifest_bytes, manifest_sha256,
                payload_sha256, batch_sha256
            ) VALUES (
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                'legacy-import:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                'legacy_import_v1', 'ze.legacy-import.sqlite-receipt.v1',
                'imported', 'ze.legacy-import.manifest.v1',
                'ze.legacy-import.batch.v1', 'ze.legacy-import.payload.v1',
                'explicit_projects', 1, 0, 2, 10, X'7B7D',
                zeroblob(32), zeroblob(32), zeroblob(32)
            )
            """,
        ),
        (
            "legacy_import_projects",
            """
            INSERT INTO legacy_import_projects(
                import_id, ordinal, project_id, generation,
                document_id, document_version, storage_version,
                source_head_sha256, source_head_bytes, business_root_sha256,
                entity_count, row_snapshot_bytes
            ) VALUES (
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                0, 'p', 1, 'd', 1, 'v2', zeroblob(32), 2,
                zeroblob(32), 0, 4
            )
            """,
        ),
    ),
)
def test_ordinary_uow_cannot_write_private_business_or_import_tables(
    tmp_path: Path, table_name: str, statement: str
) -> None:
    database = _database(tmp_path)
    database.initialize()
    transaction_id = "business-private-%s" % table_name

    with pytest.raises(kernel.SQLiteKernelError) as caught:
        database.commit(
            transaction_id,
            "uow-test",
            lambda uow: uow.execute(statement),
        )
    assert caught.value.code == "sqlite_uow_statement_forbidden"
    assert database.reconcile(transaction_id).status is kernel.ReconcileStatus.NOT_COMMITTED
    with database._owned_connection_for_test() as owned:
        assert owned.execute("SELECT count(*) FROM %s" % table_name).fetchone()[0] == 0


def test_callback_cannot_swallow_denied_sql_and_mint_a_receipt(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.initialize()

    def swallow_denial(uow: kernel.UnitOfWork) -> str:
        denied = 0
        for statement in ("DELETE FROM projects", "PRAGMA user_version"):
            try:
                uow.execute(statement)
            except sqlite3.DatabaseError:
                denied += 1
        assert denied == 2
        return "pretend-success"

    with pytest.raises(kernel.SQLiteKernelError) as caught:
        database.commit("swallowed-denial", "uow-test", swallow_denial)
    assert caught.value.code == "sqlite_uow_statement_forbidden"
    assert (
        database.reconcile("swallowed-denial").status
        is kernel.ReconcileStatus.NOT_COMMITTED
    )


def test_terminal_shadow_accepts_disjoint_batches_and_keeps_legacy_authority(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    ordinary = database.commit("ordinary-before-import", "test", _noop).receipt
    raw = _raw_database(database)
    try:
        raw.execute("BEGIN")
        _insert_terminal_shadow_batch(raw)
        _insert_terminal_shadow_batch(
            raw,
            import_id="b" * 64,
            project_id="project-b",
            document_id="document-b",
            generation=7,
            document_version=9,
            entity_snapshots=(),
        )
        _insert_import_state_marker(raw)
        raw.execute("COMMIT")
    finally:
        raw.close()

    assert database.inspect_profile().user_version == 13
    assert database.initialize().user_version == 13
    assert database.reconcile(ordinary.transaction_id).receipt == ordinary
    with database._owned_connection_for_test() as owned:
        assert owned.execute(
            "SELECT authority FROM kernel_repository WHERE singleton=1"
        ).fetchone()[0] == "legacy"
        assert owned.execute(
            "SELECT value FROM kernel_metadata WHERE key=?",
            (import_contract.SQLITE_IMPORT_STATE_KEY,),
        ).fetchone()[0] == import_contract.SQLITE_IMPORT_STATE_TERMINAL_SHADOW

    # Terminal shadow is append-only through its private importer; the generic
    # callback boundary closes before any ordinary callback can inspect it.
    callback_calls: List[int] = []
    with pytest.raises(kernel.SQLiteKernelError) as closed:
        database.commit(
            "ordinary-after-import",
            "test",
            lambda _uow: callback_calls.append(1),
        )
    assert closed.value.code == "sqlite_terminal_shadow_read_only"
    assert callback_calls == []
    assert (
        database.reconcile("ordinary-after-import").status
        is kernel.ReconcileStatus.NOT_COMMITTED
    )


def test_terminal_shadow_global_open_does_not_hash_snapshot_or_payload_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    database.initialize()
    large_document_snapshot = b"x" * (2 * 1024 * 1024)
    raw = _raw_database(database)
    try:
        raw.execute("BEGIN")
        _insert_terminal_shadow_batch(
            raw,
            document_snapshot=large_document_snapshot,
        )
        _insert_import_state_marker(raw)
        raw.execute("COMMIT")
    finally:
        raw.close()

    real_sha256 = kernel.hashlib.sha256
    initial_sizes: List[int] = []

    def recording_sha256(value: bytes = b"") -> Any:
        initial_sizes.append(len(value))
        return real_sha256(value)

    monkeypatch.setattr(kernel.hashlib, "sha256", recording_sha256)
    assert database.inspect_profile().user_version == 13
    # Schema fingerprints remain checked.  No snapshot/manifest/payload blob is
    # selected into Python or passed to SHA-256 by the global state verifier.
    assert initial_sizes
    assert max(initial_sizes) < len(large_document_snapshot)


def test_historical_empty_catalog_is_closed_and_keeps_legacy_authority(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    ordinary = database.commit("before-empty-catalog", "test", _noop).receipt
    raw = _raw_database(database)
    try:
        raw.execute("BEGIN")
        _insert_historical_catalog(raw, empty=True)
        raw.execute("COMMIT")
    finally:
        raw.close()

    assert database.inspect_profile().user_version == 13
    assert database.initialize().user_version == 13
    assert database.reconcile(ordinary.transaction_id).receipt == ordinary
    catalog_transaction_id = (
        catalog_contract.LEGACY_CATALOG_TRANSACTION_PREFIX + "c" * 64
    )
    with pytest.raises(kernel.SQLiteKernelError) as private_identity:
        database.reconcile(catalog_transaction_id)
    assert private_identity.value.code == "sqlite_transaction_id_invalid"
    with database._owned_connection_for_test() as owned:
        assert owned.execute(
            "SELECT authority FROM kernel_repository WHERE singleton=1"
        ).fetchone()[0] == "legacy"
        assert owned.execute("SELECT count(*) FROM legacy_catalog_members").fetchone()[0] == 0

    calls: List[int] = []
    with pytest.raises(kernel.SQLiteKernelError) as closed:
        database.commit(
            "after-empty-catalog",
            "test",
            lambda _uow: calls.append(1),
        )
    assert closed.value.code == "sqlite_terminal_shadow_read_only"
    assert calls == []


def test_historical_nonempty_catalog_closes_one_import_batch(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    raw = _raw_database(database)
    try:
        raw.execute("BEGIN")
        _insert_terminal_shadow_batch(raw)
        _insert_import_state_marker(raw)
        _insert_historical_catalog(raw)
        raw.execute("COMMIT")
    finally:
        raw.close()

    assert database.inspect_profile().user_version == 13
    with database._owned_connection_for_test() as owned:
        assert owned.execute(
            "SELECT authority FROM kernel_repository WHERE singleton=1"
        ).fetchone()[0] == "legacy"
        assert owned.execute("SELECT project_count FROM legacy_catalog_seals").fetchone()[0] == 1
        assert owned.execute("SELECT count(*) FROM legacy_catalog_members").fetchone()[0] == 1


def test_historical_catalog_accepts_exact_per_project_source_head_limit(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    source_limit = catalog_contract.MAX_LEGACY_CATALOG_PROJECT_SOURCE_HEAD_BYTES
    raw = _raw_database(database)
    try:
        raw.execute("BEGIN")
        _insert_terminal_shadow_batch(raw, source_head_bytes=source_limit)
        _insert_import_state_marker(raw)
        _insert_catalog_receipt(raw)
        _insert_catalog_seal(raw, source_head_bytes=source_limit)
        _insert_catalog_member(raw, source_head_bytes=source_limit)
        _insert_catalog_state_marker(raw)
        raw.execute("COMMIT")
    finally:
        raw.close()

    assert database.inspect_profile().user_version == 13


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        ("catalog-marker-only", "sqlite_catalog_state_invalid"),
        ("both-markers-no-seal", "sqlite_catalog_state_invalid"),
        ("seal-without-marker", "sqlite_catalog_state_unexpected"),
        ("unsupported-catalog-marker", "sqlite_catalog_state_invalid"),
        ("catalog-receipt-without-marker", "sqlite_catalog_state_unexpected"),
    ),
)
def test_catalog_marker_combinations_fail_closed(
    tmp_path: Path, mutation: str, expected_code: str
) -> None:
    database = _database(tmp_path)
    database.initialize()
    raw = _raw_database(database)
    try:
        if mutation == "catalog-marker-only":
            _insert_catalog_state_marker(raw)
        elif mutation == "both-markers-no-seal":
            raw.execute("BEGIN")
            _insert_terminal_shadow_batch(raw)
            _insert_import_state_marker(raw)
            _insert_catalog_state_marker(raw)
            raw.execute("COMMIT")
        elif mutation == "seal-without-marker":
            raw.execute("BEGIN")
            _insert_catalog_receipt(raw)
            _insert_catalog_seal(raw, import_id=None)
            raw.execute("COMMIT")
        elif mutation == "unsupported-catalog-marker":
            _insert_catalog_state_marker(raw, "future-catalog-state")
        elif mutation == "catalog-receipt-without-marker":
            _insert_catalog_receipt(raw)
        else:  # pragma: no cover
            raise AssertionError(mutation)
    finally:
        raw.close()

    with pytest.raises(kernel.SQLiteKernelError) as caught:
        database.inspect_profile()
    assert caught.value.code == expected_code


@pytest.mark.parametrize(
    "mutation",
    (
        "missing-receipt",
        "extra-reserved-receipt",
        "missing-member",
        "member-field",
        "source-aggregate",
        "schema-fingerprint",
        "schema-version",
        "digest-shape",
        "committed-at",
        "project-source-limit",
    ),
)
def test_historical_nonempty_catalog_tamper_matrix_fails_closed(
    tmp_path: Path, mutation: str
) -> None:
    database = _database(tmp_path)
    database.initialize()
    raw = _raw_database(database)
    try:
        if mutation in ("missing-receipt", "member-field"):
            raw.execute("PRAGMA foreign_keys = OFF")
        if mutation in ("schema-version", "digest-shape", "project-source-limit"):
            raw.execute("PRAGMA ignore_check_constraints = ON")
        raw.execute("BEGIN")
        project_source_bytes = (
            catalog_contract.MAX_LEGACY_CATALOG_PROJECT_SOURCE_HEAD_BYTES + 1
            if mutation == "project-source-limit"
            else 100
        )
        _insert_terminal_shadow_batch(raw, source_head_bytes=project_source_bytes)
        _insert_import_state_marker(raw)
        if mutation != "missing-receipt":
            _insert_catalog_receipt(
                raw,
                committed_at=(
                    "not-canonical"
                    if mutation == "committed-at"
                    else "2026-07-11T00:00:00.000Z"
                ),
            )
        _insert_catalog_seal(
            raw,
            source_head_bytes=(
                101
                if mutation == "source-aggregate"
                else project_source_bytes
            ),
            schema_fingerprint=(
                "d" * 64
                if mutation == "schema-fingerprint"
                else _SCHEMA_FINGERPRINT_V4
            ),
            target_schema_version=3 if mutation == "schema-version" else 4,
            manifest_sha256=b"x" * (31 if mutation == "digest-shape" else 32),
        )
        if mutation != "missing-member":
            _insert_catalog_member(
                raw,
                generation=2 if mutation == "member-field" else 1,
                source_head_bytes=project_source_bytes,
            )
        _insert_catalog_state_marker(raw)
        if mutation == "extra-reserved-receipt":
            _insert_catalog_receipt(
                raw,
                "d" * 64,
                transaction_id="ordinary-catalog-kind",
            )
        raw.execute("COMMIT")
    finally:
        raw.close()

    with pytest.raises(kernel.SQLiteKernelError) as caught:
        database.inspect_profile()
    assert caught.value.code == "sqlite_catalog_state_invalid"


def test_historical_catalog_rejects_extra_import_batch(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    raw = _raw_database(database)
    try:
        raw.execute("BEGIN")
        _insert_terminal_shadow_batch(raw)
        _insert_terminal_shadow_batch(
            raw,
            import_id="b" * 64,
            project_id="project-b",
            document_id="document-b",
            entity_snapshots=(),
        )
        _insert_import_state_marker(raw)
        _insert_historical_catalog(raw)
        raw.execute("COMMIT")
    finally:
        raw.close()

    with pytest.raises(kernel.SQLiteKernelError) as caught:
        database.inspect_profile()
    assert caught.value.code == "sqlite_catalog_state_invalid"


def test_historical_catalog_global_open_does_not_hash_manifest_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    database.initialize()
    manifest = b"x" * (2 * 1024 * 1024)
    raw = _raw_database(database)
    try:
        raw.execute("BEGIN")
        _insert_historical_catalog(raw, empty=True, manifest_bytes=manifest)
        raw.execute("COMMIT")
    finally:
        raw.close()

    real_sha256 = kernel.hashlib.sha256
    initial_sizes: List[int] = []

    def recording_sha256(value: bytes = b"") -> Any:
        initial_sizes.append(len(value))
        return real_sha256(value)

    monkeypatch.setattr(kernel.hashlib, "sha256", recording_sha256)
    assert database.inspect_profile().user_version == 13
    assert initial_sizes
    assert max(initial_sizes) < len(manifest)


@pytest.mark.parametrize(
    "mutation",
    (
        "marker-only",
        "unsupported-marker",
        "extra-reserved-receipt",
        "missing-descriptor",
        "ordinal-gap",
        "batch-aggregate",
        "extra-business-project",
        "generation",
        "document-version",
        "business-root",
        "entity-count",
        "row-bytes",
        "foreign-key",
    ),
)
def test_terminal_shadow_half_states_fail_closed(
    tmp_path: Path, mutation: str
) -> None:
    database = _database(tmp_path)
    database.initialize()
    raw = _raw_database(database)
    try:
        if mutation == "marker-only":
            _insert_import_state_marker(raw)
        elif mutation == "unsupported-marker":
            _insert_import_state_marker(raw, "future-state")
        else:
            raw.execute("BEGIN")
            _insert_terminal_shadow_batch(raw)
            _insert_import_state_marker(raw)
            raw.execute("COMMIT")
            if mutation == "extra-reserved-receipt":
                raw.execute(
                    "INSERT INTO transaction_receipts VALUES (?, ?, ?)",
                    (
                        import_contract.SQLITE_IMPORT_TRANSACTION_PREFIX + "b" * 64,
                        import_contract.SQLITE_IMPORT_TRANSACTION_KIND,
                        "2026-07-11T00:00:00.000Z",
                    ),
                )
            elif mutation == "missing-descriptor":
                raw.execute("DELETE FROM legacy_import_projects")
            elif mutation == "ordinal-gap":
                raw.execute("UPDATE legacy_import_projects SET ordinal=1")
            elif mutation == "batch-aggregate":
                raw.execute(
                    "UPDATE legacy_import_batches SET project_count=2"
                )
            elif mutation == "extra-business-project":
                _insert_project(raw, "project-extra")
            elif mutation == "generation":
                raw.execute("UPDATE projects SET generation=2")
            elif mutation == "document-version":
                raw.execute("UPDATE documents SET document_version=2")
            elif mutation == "business-root":
                raw.execute(
                    "UPDATE legacy_import_projects "
                    "SET business_root_sha256=?",
                    (b"z" * 32,),
                )
            elif mutation == "entity-count":
                raw.execute(
                    "UPDATE legacy_import_projects SET entity_count=0"
                )
                raw.execute(
                    "UPDATE legacy_import_batches SET entity_count=0"
                )
            elif mutation == "row-bytes":
                raw.execute(
                    "UPDATE legacy_import_projects SET row_snapshot_bytes=7"
                )
                raw.execute(
                    "UPDATE legacy_import_batches "
                    "SET row_snapshot_bytes=7, framed_payload_bytes=200"
                )
            elif mutation == "foreign-key":
                raw.execute("PRAGMA foreign_keys = OFF")
                raw.execute(
                    "UPDATE entities SET document_id='orphan-document'"
                )
            else:  # pragma: no cover - closed parameter inventory above.
                raise AssertionError(mutation)
    finally:
        raw.close()

    with pytest.raises(kernel.SQLiteKernelError) as inspect_error:
        database.inspect_profile()
    assert inspect_error.value.code == "sqlite_import_state_invalid"
    with pytest.raises(kernel.SQLiteKernelError) as initialize_error:
        database.initialize()
    assert initialize_error.value.code == "sqlite_import_state_invalid"


def test_inactive_business_rows_fail_closed_before_any_kernel_work(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    raw = _raw_database(database)
    try:
        _insert_project(raw, "unexpected-business-row")
        assert raw.execute(
            "SELECT authority FROM kernel_repository WHERE singleton=1"
        ).fetchone()[0] == "legacy"
    finally:
        raw.close()

    with pytest.raises(kernel.SQLiteKernelError) as initialize_error:
        database.initialize()
    assert initialize_error.value.code == "sqlite_business_state_unexpected"
    with pytest.raises(kernel.SQLiteKernelError) as inspect_error:
        database.inspect_profile()
    assert inspect_error.value.code == "sqlite_business_state_unexpected"

    callback_calls: List[int] = []
    with pytest.raises(kernel.SQLiteKernelError) as commit_error:
        database.commit(
            "business-state-blocked",
            "uow-test",
            lambda _uow: callback_calls.append(1),
        )
    assert commit_error.value.code == "sqlite_business_state_unexpected"
    assert callback_calls == []
    reconciled = database.reconcile("business-state-blocked")
    assert reconciled.status is kernel.ReconcileStatus.INDETERMINATE
    assert reconciled.error_code == "sqlite_business_state_unexpected"
    raw = _raw_database(database)
    try:
        assert raw.execute(
            "SELECT count(*) FROM transaction_receipts "
            "WHERE transaction_id='business-state-blocked'"
        ).fetchone()[0] == 0
    finally:
        raw.close()


@pytest.mark.parametrize("dirty_state", ("reserved_receipt", "ledger_rows"))
def test_inactive_import_state_fails_closed_before_any_kernel_work(
    tmp_path: Path, dirty_state: str
) -> None:
    database = _database(tmp_path)
    database.initialize()
    raw = _raw_database(database)
    try:
        _insert_import_kernel_receipt(raw)
        if dirty_state == "ledger_rows":
            _insert_import_batch(raw)
            _insert_import_project(raw)
    finally:
        raw.close()

    with pytest.raises(kernel.SQLiteKernelError) as initialize_error:
        database.initialize()
    assert initialize_error.value.code == "sqlite_import_state_unexpected"
    with pytest.raises(kernel.SQLiteKernelError) as inspect_error:
        database.inspect_profile()
    assert inspect_error.value.code == "sqlite_import_state_unexpected"

    callback_calls: List[int] = []
    with pytest.raises(kernel.SQLiteKernelError) as commit_error:
        database.commit(
            "import-state-blocked",
            "uow-test",
            lambda _uow: callback_calls.append(1),
        )
    assert commit_error.value.code == "sqlite_import_state_unexpected"
    assert callback_calls == []
    reconciled = database.reconcile("import-state-blocked")
    assert reconciled.status is kernel.ReconcileStatus.INDETERMINATE
    assert reconciled.error_code == "sqlite_import_state_unexpected"


def test_uow_does_not_expose_connection_or_transaction_controls(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.initialize()
    retained: List[kernel.UnitOfWork] = []

    def inspect_surface(uow: kernel.UnitOfWork) -> Tuple[bool, bool, bool, bool]:
        retained.append(uow)
        assert not hasattr(uow, "__dict__")
        with pytest.raises(AttributeError):
            object.__getattribute__(uow, "_UnitOfWork__connection")
        assert uow.execute.__self__ is uow
        return tuple(
            hasattr(uow, name)
            for name in ("connection", "commit", "rollback", "close")
        )

    result = database.commit("surface-test", "uow-test", inspect_surface)
    assert result.value == (False, False, False, False)
    with pytest.raises(TypeError):
        kernel.UnitOfWork()
    with pytest.raises(kernel.SQLiteKernelError) as revoked:
        retained[0].execute("SELECT 1")
    assert revoked.value.code == "sqlite_uow_capability_lost"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is unavailable")
def test_fork_resets_locked_uow_ledger_without_inheriting_active_handle(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    locked = threading.Event()
    release = threading.Event()
    handles: List[kernel.UnitOfWork] = []
    errors: List[BaseException] = []

    def callback(uow: kernel.UnitOfWork) -> None:
        handles.append(uow)
        with kernel._hold_uow_ledger_lock_for_test():
            locked.set()
            if not release.wait(10):
                raise RuntimeError("parent UoW ledger lock test timed out")
        assert uow.execute("SELECT 1").fetchone()[0] == 1

    def writer() -> None:
        try:
            database.commit("fork-uow-ledger", "uow-test", callback)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=writer)
    worker.start()
    assert locked.wait(10)
    read_fd, write_fd = os.pipe()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            handles[0].execute("SELECT 1")
        except BaseException as exc:
            payload = getattr(exc, "code", type(exc).__name__).encode("ascii")
        else:
            payload = b"unexpected-success"
        os.write(write_fd, payload)
        os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    payload = os.read(read_fd, 256).decode("ascii")
    os.close(read_fd)
    waited_pid, status = os.waitpid(pid, 0)
    assert waited_pid == pid
    assert os.waitstatus_to_exitcode(status) == 0
    assert payload == "sqlite_uow_capability_lost"
    release.set()
    worker.join(10)
    assert not worker.is_alive()
    assert errors == []
    assert database.reconcile("fork-uow-ledger").status is kernel.ReconcileStatus.COMMITTED


def test_statement_result_is_materialized_and_exposes_no_connection(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.initialize()

    def query(uow: kernel.UnitOfWork) -> Any:
        statement = uow.execute("SELECT 17 AS value")
        assert not hasattr(statement, "connection")
        assert not hasattr(statement, "cursor")
        assert not hasattr(statement, "execute")
        return statement

    committed = database.commit("materialized-result", "uow-test", query)
    statement = committed.value
    assert statement.fetchone()["value"] == 17
    assert statement.fetchall()[0]["value"] == 17


def test_callback_error_rolls_back_and_creates_no_receipt(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.initialize()

    def fail(_uow: kernel.UnitOfWork) -> None:
        raise ValueError("producer failed")

    with pytest.raises(ValueError, match="producer failed"):
        database.commit("rollback-test", "uow-test", fail)
    assert database.reconcile("rollback-test").status is kernel.ReconcileStatus.NOT_COMMITTED


def test_commit_ack_loss_reconciles_committed_without_replaying_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    database.initialize()
    original_commit = kernel._OwnedConnection.commit
    original_barrier = kernel.SqliteDatabase._finalize_committed_storage
    callback_calls: List[int] = []
    barrier_calls: List[int] = []

    def commit_then_lose_ack(connection: Any) -> None:
        original_commit(connection)
        raise sqlite3.OperationalError("simulated commit acknowledgement loss")

    def record_barrier(target: Any) -> None:
        barrier_calls.append(1)
        original_barrier(target)

    monkeypatch.setattr(kernel._OwnedConnection, "commit", commit_then_lose_ack)
    monkeypatch.setattr(
        kernel.SqliteDatabase, "_finalize_committed_storage", record_barrier
    )
    result = database.commit(
        "ack-lost-committed",
        "reconcile-test",
        lambda _uow: callback_calls.append(1),
    )

    assert result.recovered_after_ack_loss is True
    assert result.receipt.transaction_id == "ack-lost-committed"
    assert callback_calls == [1]
    assert barrier_calls == [1]


def test_commit_never_reports_ack_loss_recovery_when_durability_barrier_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    database.initialize()
    original_commit = kernel._OwnedConnection.commit

    def commit_then_lose_ack(connection: Any) -> None:
        original_commit(connection)
        raise sqlite3.OperationalError("simulated commit acknowledgement loss")

    def fail_barrier(_database: Any) -> None:
        raise kernel.SQLiteKernelError(
            "sqlite_file_stat_failed", "simulated durability barrier failure"
        )

    monkeypatch.setattr(kernel._OwnedConnection, "commit", commit_then_lose_ack)
    monkeypatch.setattr(
        kernel.SqliteDatabase, "_finalize_committed_storage", fail_barrier
    )
    with pytest.raises(kernel.SQLiteKernelError) as caught:
        database.commit("ack-lost-unsafe", "reconcile-test", _noop)
    assert caught.value.code == "sqlite_commit_indeterminate"


def test_positive_reconcile_cannot_bypass_post_commit_durability_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    database.initialize()

    def fail_barrier(_database: Any) -> None:
        raise kernel.SQLiteKernelError(
            "sqlite_directory_sync_failed", "simulated directory fsync failure"
        )

    monkeypatch.setattr(
        kernel.SqliteDatabase, "_finalize_committed_storage", fail_barrier
    )
    with pytest.raises(kernel.SQLiteKernelError) as caught:
        database.commit("barrier-failed-visible", "reconcile-test", _noop)
    assert caught.value.code == "sqlite_commit_indeterminate"
    reconciled = database.reconcile("barrier-failed-visible")
    assert reconciled.status is kernel.ReconcileStatus.INDETERMINATE
    assert reconciled.error_code == "sqlite_directory_sync_failed"


def test_post_commit_barrier_async_interrupt_is_reconciled_then_propagated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    database.initialize()
    original_barrier = kernel.SqliteDatabase._finalize_committed_storage
    barrier_calls = []

    def interrupt_first_barrier(target: Any) -> None:
        barrier_calls.append(1)
        if len(barrier_calls) == 1:
            raise KeyboardInterrupt()
        original_barrier(target)

    monkeypatch.setattr(
        kernel.SqliteDatabase,
        "_finalize_committed_storage",
        interrupt_first_barrier,
    )
    with pytest.raises(KeyboardInterrupt):
        database.commit("barrier-async-visible", "reconcile-test", _noop)
    assert barrier_calls == [1, 1]
    assert database.reconcile("barrier-async-visible").status is (
        kernel.ReconcileStatus.COMMITTED
    )


def test_post_commit_transient_barrier_error_recovers_only_after_fresh_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    database.initialize()
    original_barrier = kernel.SqliteDatabase._finalize_committed_storage
    barrier_calls = []

    def fail_first_barrier(target: Any) -> None:
        barrier_calls.append(1)
        if len(barrier_calls) == 1:
            raise kernel.SQLiteKernelError(
                "sqlite_directory_sync_failed", "simulated transient fsync failure"
            )
        original_barrier(target)

    monkeypatch.setattr(
        kernel.SqliteDatabase,
        "_finalize_committed_storage",
        fail_first_barrier,
    )
    result = database.commit("barrier-transient", "reconcile-test", _noop)
    assert result.recovered_after_ack_loss
    assert barrier_calls == [1, 1]


def test_fresh_reconcile_async_interrupt_is_never_wrapped_by_prior_barrier_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    database.initialize()
    barrier_calls = []

    def fail_then_interrupt(_target: Any) -> None:
        barrier_calls.append(1)
        if len(barrier_calls) == 1:
            raise kernel.SQLiteKernelError(
                "sqlite_directory_sync_failed", "first barrier failed"
            )
        raise KeyboardInterrupt()

    monkeypatch.setattr(
        kernel.SqliteDatabase, "_finalize_committed_storage", fail_then_interrupt
    )
    with pytest.raises(KeyboardInterrupt):
        database.commit("barrier-reconcile-async", "reconcile-test", _noop)
    assert barrier_calls == [1, 1]


def test_commit_async_interrupt_after_commit_is_classified_then_propagated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    database.initialize()
    original_commit = kernel._OwnedConnection.commit

    def commit_then_interrupt(connection: Any) -> None:
        original_commit(connection)
        raise KeyboardInterrupt()

    monkeypatch.setattr(kernel._OwnedConnection, "commit", commit_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        database.commit("kernel-async-committed", "reconcile-test", _noop)
    assert database.reconcile("kernel-async-committed").status is (
        kernel.ReconcileStatus.COMMITTED
    )


def test_commit_async_interrupt_before_commit_is_classified_then_propagated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    database.initialize()

    def interrupt_before_commit(_connection: Any) -> None:
        raise KeyboardInterrupt()

    monkeypatch.setattr(kernel._OwnedConnection, "commit", interrupt_before_commit)
    with pytest.raises(KeyboardInterrupt):
        database.commit("kernel-async-aborted", "reconcile-test", _noop)
    assert database.reconcile("kernel-async-aborted").status is (
        kernel.ReconcileStatus.NOT_COMMITTED
    )


def test_commit_async_interrupt_preserved_after_indeterminate_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    database.initialize()
    original_commit = kernel._OwnedConnection.commit

    def commit_then_interrupt(connection: Any) -> None:
        original_commit(connection)
        raise KeyboardInterrupt()

    def fail_barrier(_database: Any) -> None:
        raise kernel.SQLiteKernelError(
            "sqlite_directory_sync_failed", "simulated directory fsync failure"
        )

    monkeypatch.setattr(kernel._OwnedConnection, "commit", commit_then_interrupt)
    monkeypatch.setattr(
        kernel.SqliteDatabase, "_finalize_committed_storage", fail_barrier
    )
    with pytest.raises(KeyboardInterrupt):
        database.commit("kernel-async-indeterminate", "reconcile-test", _noop)


def test_commit_failure_reconciles_not_committed_without_replaying_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    database.initialize()
    callback_calls: List[int] = []

    def fail_before_commit(_connection: Any) -> None:
        raise sqlite3.OperationalError("simulated commit failure")

    monkeypatch.setattr(kernel._OwnedConnection, "commit", fail_before_commit)
    with pytest.raises(kernel.SQLiteKernelError) as caught:
        database.commit(
            "ack-lost-aborted",
            "reconcile-test",
            lambda _uow: callback_calls.append(1),
        )
    assert caught.value.code == "sqlite_transaction_not_committed"
    assert callback_calls == [1]
    assert (
        database.reconcile("ack-lost-aborted").status
        is kernel.ReconcileStatus.NOT_COMMITTED
    )


def test_receipt_type_corruption_reconciles_as_indeterminate(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.initialize()
    database.commit("corrupt-receipt", "reconcile-test", _noop)
    raw = sqlite3.connect(str(database.path))
    try:
        raw.execute(
            "UPDATE transaction_receipts SET kind=? WHERE transaction_id=?",
            (sqlite3.Binary(b"not-text"), "corrupt-receipt"),
        )
        raw.commit()
    finally:
        raw.close()

    reconciled = database.reconcile("corrupt-receipt")
    assert reconciled.status is kernel.ReconcileStatus.INDETERMINATE
    assert reconciled.receipt is None
    assert reconciled.error_code == "sqlite_receipt_corrupt"


@pytest.mark.parametrize("second_kind", ["first-kind", "different-kind"])
def test_repeated_transaction_id_is_conflict_before_callback(
    tmp_path: Path, second_kind: str
) -> None:
    database = _database(tmp_path)
    database.initialize()
    first = database.commit("unique-id", "first-kind", _noop)
    callback_calls: List[int] = []

    with pytest.raises(kernel.SQLiteKernelError) as caught:
        database.commit(
            "unique-id",
            second_kind,
            lambda _uow: callback_calls.append(1),
        )
    assert caught.value.code == "sqlite_transaction_id_conflict"
    assert callback_calls == []
    reconciled = database.reconcile("unique-id")
    assert reconciled.status is kernel.ReconcileStatus.COMMITTED
    assert reconciled.receipt == first.receipt


def test_concurrent_distinct_commits_are_serialized_without_loss(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.initialize()
    transaction_ids = ("parallel-a", "parallel-b", "parallel-c")
    processes = [
        _start_subprocess(_COMMIT_SCRIPT, str(database.path), transaction_id)
        for transaction_id in transaction_ids
    ]
    for process in processes:
        _await_ready(process)
    collected = _release_all_and_collect(processes)
    assert all(returncode == 0 and not stderr for returncode, _stdout, stderr in collected)
    observed = [json.loads(stdout.strip()) for _code, stdout, _stderr in collected]
    assert sorted(observed) == sorted(["ok", value] for value in transaction_ids)
    for transaction_id in transaction_ids:
        assert database.reconcile(transaction_id).status is kernel.ReconcileStatus.COMMITTED


def test_concurrent_same_transaction_id_has_one_winner(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.initialize()
    processes = [
        _start_subprocess(_COMMIT_SCRIPT, str(database.path), "same-id")
        for _index in range(2)
    ]
    for process in processes:
        _await_ready(process)
    collected = _release_all_and_collect(processes)
    assert all(returncode == 0 and not stderr for returncode, _stdout, stderr in collected)
    observed = [json.loads(stdout.strip()) for _code, stdout, _stderr in collected]
    assert sum(item[0] == "ok" for item in observed) == 1
    assert sum(
        item == ["error", "sqlite_transaction_id_conflict"] for item in observed
    ) == 1
    assert database.reconcile("same-id").status is kernel.ReconcileStatus.COMMITTED


@pytest.mark.parametrize(
    ("crash_point", "expected_status"),
    [
        (kernel.CrashPoint.BEFORE_COMMIT, kernel.ReconcileStatus.NOT_COMMITTED),
        (
            kernel.CrashPoint.AFTER_COMMIT_BEFORE_ACK,
            kernel.ReconcileStatus.COMMITTED,
        ),
    ],
)
def test_process_crash_reconciles_from_fresh_connection(
    tmp_path: Path,
    crash_point: kernel.CrashPoint,
    expected_status: kernel.ReconcileStatus,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    transaction_id = "crash-%s" % crash_point.value
    process = _start_subprocess(
        _CRASH_SCRIPT,
        str(database.path),
        transaction_id,
        crash_point.value,
    )
    _await_ready(process, marker="BOUNDARY")
    returncode, stdout, stderr = _release_and_collect(process)
    assert returncode == 91, (stdout, stderr)

    reconciled = database.reconcile(transaction_id)
    assert reconciled.status is expected_status
    if expected_status is kernel.ReconcileStatus.COMMITTED:
        assert reconciled.receipt is not None
        assert reconciled.receipt.transaction_id == transaction_id
    else:
        assert reconciled.receipt is None


def test_storage_permissions_are_private(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.path.parent.mkdir(mode=0o755, parents=True)
    os.chmod(str(database.path.parent), 0o755)
    database.initialize()
    assert stat.S_IMODE(os.stat(str(database.path.parent)).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(str(database.path)).st_mode) == 0o600
    bootstrap_lock = kernel._initialization_lock_path(database.path)
    bootstrap_info = os.lstat(str(bootstrap_lock))
    assert stat.S_ISREG(bootstrap_info.st_mode)
    assert stat.S_IMODE(bootstrap_info.st_mode) == 0o600
    assert bootstrap_info.st_nlink == 1
    with database._owned_connection_for_test():
        for candidate in kernel._storage_paths(database.path):
            if candidate.exists():
                assert stat.S_IMODE(os.lstat(str(candidate)).st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="directory fsync evidence is POSIX-only")
def test_creation_and_initialization_fsync_regular_files_and_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    original_fsync = kernel.os.fsync
    synced_types: List[str] = []

    def recording_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        if stat.S_ISDIR(mode):
            synced_types.append("directory")
        elif stat.S_ISREG(mode):
            synced_types.append("file")
        original_fsync(descriptor)

    monkeypatch.setattr(kernel.os, "fsync", recording_fsync)
    database.initialize()

    assert "file" in synced_types
    assert "directory" in synced_types


def test_network_and_unknown_filesystems_fail_closed(tmp_path: Path) -> None:
    for name, probe in (("network", _network_probe), ("unknown", _unknown_probe)):
        database = kernel.SqliteDatabase(
            tmp_path.resolve() / name / "repository.sqlite3",
            _test_filesystem_probe=probe,
        )
        with pytest.raises(kernel.SQLiteKernelError) as caught:
            database.initialize()
        assert caught.value.code == "sqlite_filesystem_unsupported"


def test_only_explicit_initialize_may_create_database_or_directory(
    tmp_path: Path,
) -> None:
    path = tmp_path.resolve() / "absent" / "repository.sqlite3"
    database = kernel.SqliteDatabase(path, _test_filesystem_probe=_local_probe)

    reconciled = database.reconcile("never-created")
    assert reconciled.status is kernel.ReconcileStatus.INDETERMINATE
    assert reconciled.error_code == "sqlite_database_missing"
    with pytest.raises(kernel.SQLiteKernelError) as inspect_error:
        database.inspect_profile()
    assert inspect_error.value.code == "sqlite_database_missing"
    with pytest.raises(kernel.SQLiteKernelError) as commit_error:
        database.commit("missing-commit", "probe", _noop)
    assert commit_error.value.code == "sqlite_database_missing"
    assert not path.parent.exists()

    database.initialize()
    assert path.is_file()


def test_platform_filesystem_probes_classify_local_network_and_unc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    linux_mountinfo = "\n".join(
        (
            "31 20 0:1 / / rw,relatime - ext4 /dev/disk rw",
            "32 20 0:2 / /network rw,relatime - nfs server:/share rw",
        )
    )

    def fake_read_text(_self: Path, **_kwargs: Any) -> str:
        return linux_mountinfo

    monkeypatch.setattr(kernel.Path, "read_text", fake_read_text)
    assert kernel._probe_linux_mountinfo(Path("/local/project")).is_local is True
    network = kernel._probe_linux_mountinfo(Path("/network/project"))
    assert network.filesystem == "nfs"
    assert network.is_local is False

    mount_output = "\n".join(
        (
            "/dev/disk3s1 on / (apfs, local, journaled)",
            "server:/share on /Volumes/share (nfs, nodev)",
        )
    )
    monkeypatch.setattr(
        kernel.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["mount"], 0, stdout=mount_output, stderr=""
        ),
    )
    assert kernel._probe_darwin_mount(Path("/Users/example")).is_local is True
    darwin_network = kernel._probe_darwin_mount(Path("/Volumes/share/project"))
    assert darwin_network.filesystem == "nfs"
    assert darwin_network.is_local is False

    unc = kernel._probe_windows_drive(PureWindowsPath("//server/share/repository"))
    assert unc.filesystem == "unc"
    assert unc.is_local is False
    fake_ctypes = SimpleNamespace(
        windll=SimpleNamespace(
            kernel32=SimpleNamespace(GetDriveTypeW=lambda _root: 3)
        )
    )
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
    fixed = kernel._probe_windows_drive(PureWindowsPath("C:/repository"))
    assert fixed.filesystem == "fixed"
    assert fixed.is_local is True


def test_symlink_special_file_and_hardlink_paths_fail_closed(tmp_path: Path) -> None:
    root = tmp_path.resolve()

    real_parent = root / "real-parent"
    real_parent.mkdir()
    linked_parent = root / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(kernel.SQLiteKernelError) as caught:
        kernel.SqliteDatabase(
            linked_parent / "db.sqlite3", _test_filesystem_probe=_local_probe
        ).initialize()
    assert caught.value.code == "sqlite_path_symlink_unsafe"

    if hasattr(os, "mkfifo"):
        fifo_path = root / "fifo.sqlite3"
        os.mkfifo(str(fifo_path), 0o600)
        with pytest.raises(kernel.SQLiteKernelError) as caught:
            kernel.SqliteDatabase(fifo_path, _test_filesystem_probe=_local_probe).initialize()
        assert caught.value.code == "sqlite_file_type_unsafe"

    database = kernel.SqliteDatabase(
        root / "hardlink" / "db.sqlite3", _test_filesystem_probe=_local_probe
    )
    database.initialize()
    alias = database.path.with_name("alias.sqlite3")
    try:
        os.link(str(database.path), str(alias))
    except (AttributeError, NotImplementedError, OSError):
        pytest.skip("hard links are unavailable on this filesystem")
    with pytest.raises(kernel.SQLiteKernelError) as caught:
        database.inspect_profile()
    assert caught.value.code == "sqlite_file_hardlink_unsafe"


def test_sidecar_symlink_fails_closed_before_connection_open(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.initialize()
    target = database.path.parent / "not-a-sidecar"
    target.write_bytes(b"")
    wal = Path(str(database.path) + "-wal")
    for sidecar in (wal, Path(str(database.path) + "-shm")):
        if os.path.lexists(str(sidecar)):
            sidecar.unlink()
    wal.symlink_to(target)
    with pytest.raises(kernel.SQLiteKernelError) as caught:
        database.inspect_profile()
    assert caught.value.code in {"sqlite_path_symlink_unsafe", "sqlite_file_type_unsafe"}


def test_connection_rejects_cross_thread_use(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.initialize()
    observed: List[str] = []
    with database._owned_connection_for_test() as owned:
        worker = threading.Thread(
            target=lambda: _capture_owned_connection_error(owned, observed)
        )
        worker.start()
        worker.join(10)
        assert not worker.is_alive()
        assert observed == ["sqlite_connection_owner_mismatch"]
        assert owned.execute("SELECT 1").fetchone()[0] == 1


def _capture_owned_connection_error(
    connection: Any, observed: List[str]
) -> None:
    try:
        connection.execute("SELECT 1")
    except BaseException as exc:
        observed.append(getattr(exc, "code", type(exc).__name__))


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is unavailable")
def test_connection_rejects_post_fork_use(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.initialize()
    read_fd, write_fd = os.pipe()
    with database._owned_connection_for_test() as owned:
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                owned.execute("SELECT 1")
            except BaseException as exc:
                os.write(write_fd, getattr(exc, "code", type(exc).__name__).encode())
                os._exit(0)
            os._exit(1)
        os.close(write_fd)
        payload = os.read(read_fd, 256).decode()
        _, status_code = os.waitpid(pid, 0)
        os.close(read_fd)
        assert os.waitstatus_to_exitcode(status_code) == 0
        assert payload == "sqlite_connection_owner_mismatch"
        assert owned.execute("SELECT 1").fetchone()[0] == 1


def test_writer_busy_timeout_returns_stable_error_without_releasing_lock(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path, busy_timeout_ms=25)
    database.initialize()
    observed: List[BaseException] = []
    entered = threading.Event()

    def contender() -> None:
        entered.set()
        try:
            database.commit("busy-writer", "busy-test", _noop)
        except BaseException as exc:
            observed.append(exc)

    with database._owned_connection_for_test() as holder:
        holder.execute("BEGIN IMMEDIATE")
        worker = threading.Thread(target=contender)
        worker.start()
        assert entered.wait(5)
        worker.join(5)
        assert not worker.is_alive(), "bounded busy_timeout did not release the contender"
        holder.rollback()

    assert len(observed) == 1
    assert isinstance(observed[0], kernel.SQLiteKernelError)
    assert observed[0].code == "sqlite_busy"
    assert database.reconcile("busy-writer").status is kernel.ReconcileStatus.NOT_COMMITTED


def test_v5_empty_repository_proof_is_closed_but_never_authoritative(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    raw = _raw_database(database)
    try:
        raw.execute("BEGIN")
        _insert_historical_catalog(raw, empty=True)
        _insert_repository_proof(raw, live=False)
        raw.execute("COMMIT")
    finally:
        raw.close()

    assert database.inspect_profile().user_version == 13
    with database._owned_connection_for_test() as owned:
        assert tuple(
            owned.execute(
                "SELECT authority, authority_state, cutover_ready "
                "FROM legacy_repository_proof_control"
            ).fetchone()
        ) == ("legacy", "cutover_prepared", 0)
        assert owned.execute(
            "SELECT cutover_ready FROM legacy_repository_seals"
        ).fetchone()[0] == 0
    with pytest.raises(kernel.SQLiteKernelError) as closed:
        database.commit("after-proof", "test", _noop)
    assert closed.value.code == "sqlite_terminal_shadow_read_only"


def test_v5_repository_proof_covers_live_catalog_and_deleted_high_water(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    raw = _raw_database(database)
    try:
        raw.execute("BEGIN")
        _insert_terminal_shadow_batch(raw)
        _insert_import_state_marker(raw)
        _insert_historical_catalog(raw)
        _insert_repository_proof(raw, live=True, deleted=True)
        raw.execute("COMMIT")
    finally:
        raw.close()

    assert database.inspect_profile().user_version == 13
    with database._owned_connection_for_test() as owned:
        assert [
            tuple(row)
            for row in owned.execute(
                "SELECT project_id, generation, status, catalog_id "
                "FROM legacy_repository_members ORDER BY ordinal"
            ).fetchall()
        ] == [
            ("project-a", 1, "live", "c" * 64),
            ("project-z", 9, "deleted", None),
        ]


def test_v5_repository_proof_missing_member_fails_structural_open(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    raw = _raw_database(database)
    try:
        raw.execute("BEGIN")
        _insert_historical_catalog(raw, empty=True)
        _insert_repository_proof(raw, live=False, deleted=True, omit_members=True)
        raw.execute("COMMIT")
    finally:
        raw.close()

    with pytest.raises(kernel.SQLiteKernelError) as caught:
        database.inspect_profile()
    assert caught.value.code == "sqlite_repository_state_invalid"


def test_v5_structural_open_does_not_validate_manifest_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    database.initialize()
    manifest = b"x" * (2 * 1024 * 1024)
    raw = _raw_database(database)
    try:
        raw.execute("BEGIN")
        _insert_historical_catalog(raw, empty=True)
        _insert_repository_proof(raw, live=False, manifest_bytes=manifest)
        raw.execute("COMMIT")
    finally:
        raw.close()

    real_sha256 = kernel.hashlib.sha256
    initial_sizes: List[int] = []

    def recording_sha256(value: bytes = b"") -> Any:
        initial_sizes.append(len(value))
        return real_sha256(value)

    monkeypatch.setattr(kernel.hashlib, "sha256", recording_sha256)
    assert database.inspect_profile().user_version == 13
    assert max(initial_sizes, default=0) < len(manifest)
    with database._owned_connection_for_test() as owned:
        assert owned.execute(
            "SELECT cutover_ready FROM legacy_repository_proof_control"
        ).fetchone()[0] == 0


def test_v4_generic_namespace_cannot_preoccupy_private_repository_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    all_migrations = kernel.MIGRATIONS
    monkeypatch.setattr(kernel, "MIGRATIONS", all_migrations[:4])
    database = _database(tmp_path)
    assert database.initialize().user_version == 4

    private_id = repository_contract.LEGACY_REPOSITORY_TRANSACTION_PREFIX + "a" * 64
    with pytest.raises(kernel.SQLiteKernelError) as invalid_id:
        database.commit(private_id, "ordinary", _noop)
    assert invalid_id.value.code == "sqlite_transaction_id_invalid"
    with pytest.raises(kernel.SQLiteKernelError) as invalid_kind:
        database.commit(
            "ordinary-repository-kind",
            repository_contract.LEGACY_REPOSITORY_TRANSACTION_KIND,
            _noop,
        )
    assert invalid_kind.value.code == "sqlite_kind_invalid"

    lookalike = "legacy-repository-seal:" + "b" * 64
    ordinary = database.commit(
        lookalike, "legacy_repository_seal_v1", _noop
    ).receipt
    monkeypatch.setattr(kernel, "MIGRATIONS", all_migrations)
    assert database.initialize().user_version == 13
    assert database.reconcile(lookalike).receipt == ordinary
    with database._owned_connection_for_test() as owned:
        assert owned.execute(
            "SELECT count(*) FROM legacy_repository_proof_control"
        ).fetchone()[0] == 0


def test_v4_and_v5_schema_fingerprints_are_distinct_and_registry_cached() -> None:
    kernel._expected_schema_fingerprint_for_migrations.cache_clear()
    v4 = kernel._expected_schema_fingerprint(4)
    v5 = kernel._expected_schema_fingerprint(5)
    assert v4 == _SCHEMA_FINGERPRINT_V4
    assert v5 == _SCHEMA_FINGERPRINT_V5
    assert v4 != v5
    assert kernel._expected_schema_fingerprint(4) == v4
    info = kernel._expected_schema_fingerprint_for_migrations.cache_info()
    assert (info.misses, info.hits) == (2, 1)


def test_v5_kernel_rejects_legacy_adopt_above_generation_one(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    raw = _raw_database(database)
    try:
        raw.execute("PRAGMA ignore_check_constraints = ON")
        raw.execute("BEGIN")
        _insert_terminal_shadow_batch(raw, generation=2)
        _insert_import_state_marker(raw)
        _insert_catalog_receipt(raw)
        _insert_catalog_seal(raw)
        _insert_catalog_member(raw, generation=2)
        _insert_catalog_state_marker(raw)
        _insert_repository_proof(
            raw,
            live=True,
            live_generation=2,
            live_operation_id="legacy-adopt",
        )
        raw.execute("COMMIT")
        raw.execute("PRAGMA ignore_check_constraints = OFF")
    finally:
        raw.close()

    with pytest.raises(kernel.SQLiteKernelError) as caught:
        database.inspect_profile()
    assert caught.value.code == "sqlite_repository_state_invalid"


@pytest.mark.parametrize(
    "state", ("empty", "terminal", "catalog-empty", "catalog-live")
)
def test_real_v4_four_states_upgrade_to_inert_v5_without_authority_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    all_migrations = kernel.MIGRATIONS
    monkeypatch.setattr(kernel, "MIGRATIONS", all_migrations[:4])
    database = _database(tmp_path)
    assert database.initialize().user_version == 4
    raw = _raw_database(database)
    try:
        raw.execute("BEGIN")
        if state in ("terminal", "catalog-live"):
            _insert_terminal_shadow_batch(raw)
            _insert_import_state_marker(raw)
        if state == "catalog-empty":
            _insert_historical_catalog(raw, empty=True)
        elif state == "catalog-live":
            _insert_historical_catalog(raw)
        raw.execute("COMMIT")
    finally:
        raw.close()

    monkeypatch.setattr(kernel, "MIGRATIONS", all_migrations)
    assert database.initialize().user_version == 13
    with database._owned_connection_for_test() as owned:
        assert owned.execute(
            "SELECT authority FROM kernel_repository WHERE singleton=1"
        ).fetchone()[0] == "legacy"
        assert all(
            owned.execute("SELECT count(*) FROM %s" % table).fetchone()[0] == 0
            for table in kernel.REPOSITORY_PROOF_TABLES
        )
        assert all(
            owned.execute("SELECT count(*) FROM %s" % table).fetchone()[0] == 0
            for table in kernel.BUSINESS_TRANSACTION_TABLES
        )


@pytest.mark.parametrize(
    ("transaction_id", "kind"),
    (
        (kernel.BUSINESS_TRANSACTION_PREFIX + "a" * 32, "ordinary"),
        ("ordinary-business-kind", kernel.BUSINESS_TRANSACTION_KIND),
        (
            kernel.BUSINESS_TRANSACTION_PREFIX + "b" * 32,
            kernel.BUSINESS_TRANSACTION_KIND,
        ),
    ),
)
def test_generic_kernel_reserves_business_receipts_before_callback_and_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transaction_id: str,
    kind: str,
) -> None:
    database = _database(tmp_path)
    opened: List[int] = []
    callback_calls: List[int] = []

    def forbidden_open(*_args: Any, **_kwargs: Any) -> Any:
        opened.append(1)
        raise AssertionError("reserved identity must fail before database I/O")

    monkeypatch.setattr(database, "_open_unvalidated", forbidden_open)
    with pytest.raises(kernel.SQLiteKernelError) as caught:
        database.commit(
            transaction_id,
            kind,
            lambda _uow: callback_calls.append(1),
        )
    assert caught.value.code == "sqlite_transaction_namespace_reserved"
    assert opened == []
    assert callback_calls == []


def test_generic_reconcile_never_upgrades_business_receipt_existence_to_commit(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    transaction_id = kernel.BUSINESS_TRANSACTION_PREFIX + "c" * 32
    reconciled = database.reconcile(transaction_id)
    assert reconciled.status is kernel.ReconcileStatus.INDETERMINATE
    assert reconciled.error_code == "sqlite_transaction_namespace_reserved"
    assert not database.path.exists()


@pytest.mark.parametrize("reserved", ("prefix", "kind"))
def test_v5_reserved_business_receipt_blocks_v6_without_partial_ddl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reserved: str,
) -> None:
    all_migrations = kernel.MIGRATIONS
    monkeypatch.setattr(kernel, "MIGRATIONS", all_migrations[:5])
    database = _database(tmp_path)
    assert database.initialize().user_version == 5
    raw = _raw_database(database)
    try:
        raw.execute(
            "INSERT INTO transaction_receipts(transaction_id, kind, committed_at) "
            "VALUES (?, ?, ?)",
            (
                kernel.BUSINESS_TRANSACTION_PREFIX + "d" * 32
                if reserved == "prefix"
                else "ordinary-before-v6",
                kernel.BUSINESS_TRANSACTION_KIND
                if reserved == "kind"
                else "ordinary",
                _NOW,
            ),
        )
    finally:
        raw.close()

    monkeypatch.setattr(kernel, "MIGRATIONS", all_migrations)
    with pytest.raises(kernel.SQLiteKernelError) as caught:
        database.initialize()
    assert caught.value.code == "sqlite_business_migration_dirty"
    raw = _raw_database(database)
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 5
        assert raw.execute(
            "SELECT count(*) FROM schema_migrations WHERE version=6"
        ).fetchone()[0] == 0
        objects = {
            row[0]
            for row in raw.execute(
                "SELECT name FROM sqlite_schema WHERE name IN "
                "('repository_runtime_control', "
                "'projects_transaction_identity', "
                "'documents_transaction_identity')"
            ).fetchall()
        }
        assert objects == set()
    finally:
        raw.close()


def test_v6_dirty_local_revision_upgrade_fails_before_v7_ddl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    all_migrations = kernel.MIGRATIONS
    monkeypatch.setattr(kernel, "MIGRATIONS", all_migrations[:6])
    database = _database(tmp_path)
    assert database.initialize().user_version == 6

    project_bytes = b"dirty-v6-project"
    project_sha256 = kernel.hashlib.sha256(project_bytes).digest()
    raw = _raw_database(database)
    try:
        raw.execute("BEGIN")
        raw.execute(
            "UPDATE kernel_repository SET authority='sqlite' WHERE singleton=1"
        )
        raw.execute(
            """
            INSERT INTO repository_runtime_control(
                authority, authority_state, repository_epoch, cutover_id,
                source_admission_revision, repository_revision,
                state_revision, runtime_generation, code_digest,
                schema_version, updated_at
            ) VALUES ('sqlite', 'active', ?, ?, 1, 1, 1,
                      'runtime-dirty-v6', ?, 6, ?)
            """,
            ("e" * 64, "f" * 64, b"c" * 32, _NOW),
        )
        raw.execute(
            """
            INSERT INTO projects(
                project_id, generation, codec_id, codec_version,
                snapshot_format_version, snapshot_bytes, snapshot_sha256
            ) VALUES ('dirty-project', 1, 'json', 1, 1, ?, ?)
            """,
            (project_bytes, project_sha256),
        )
        raw.execute(
            """
            INSERT INTO repository_projects(
                project_id, generation, project_revision, status,
                current_snapshot_sha256, created_at, updated_at
            ) VALUES ('dirty-project', 1, 1, 'live', ?, ?, ?)
            """,
            (project_sha256, _NOW, _NOW),
        )
        raw.execute("COMMIT")
    finally:
        raw.close()

    monkeypatch.setattr(kernel, "MIGRATIONS", all_migrations)
    owned, _filesystem = database._open_unvalidated()
    try:
        owned.execute("BEGIN EXCLUSIVE")
        with pytest.raises(kernel.SQLiteKernelError) as caught:
            database._apply_migrations(owned, from_version=6)
        assert caught.value.code == (
            "sqlite_local_revision_migration_precondition_failed"
        )
        owned.rollback()
    finally:
        owned.close()

    raw = _raw_database(database)
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 6
        assert raw.execute(
            "SELECT count(*) FROM schema_migrations WHERE version=7"
        ).fetchone()[0] == 0
        columns = {
            row[1]
            for row in raw.execute(
                "PRAGMA table_info(business_commits)"
            ).fetchall()
        }
        assert columns.isdisjoint({"project_revision", "head_revision"})
        assert raw.execute(
            "SELECT count(*) FROM sqlite_schema "
            "WHERE name='schema_v7_local_revision_preflight'"
        ).fetchone()[0] == 0
    finally:
        raw.close()


@pytest.mark.parametrize(
    "statement",
    (
        "SELECT * FROM repository_runtime_control",
        "INSERT INTO asset_blobs(asset_sha256, content_key, byte_length, "
        "status, state_revision, created_at, updated_at) "
        "VALUES (zeroblob(32), 'invalid', 0, 'staged', 1, 'x', 'x')",
        "UPDATE event_outbox SET status='pending'",
        "DELETE FROM document_asset_refs",
    ),
)
def test_generic_uow_cannot_read_or_write_v6_private_tables(
    tmp_path: Path,
    statement: str,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    with pytest.raises(kernel.SQLiteKernelError) as caught:
        database.commit(
            "business-private-uow-" + str(abs(hash(statement))),
            "test",
            lambda uow: uow.execute(statement),
        )
    assert caught.value.code == "sqlite_uow_statement_forbidden"


def test_current_production_runtime_tables_are_private_and_ordinary_state_is_empty(
    tmp_path: Path,
) -> None:
    assert len(kernel.PRODUCTION_RUNTIME_V8_TABLES) == 25
    assert len(kernel.WORKER_PROJECTION_SIDECAR_TABLES) == 3
    assert kernel.PRODUCTION_RUNTIME_TABLES[:28] == (
        *kernel.PRODUCTION_RUNTIME_V8_TABLES,
        *kernel.WORKER_PROJECTION_SIDECAR_TABLES,
    )
    assert len(kernel.CUTOVER_INSTALL_CLOSURE_TABLES) == 4
    assert len(kernel.DIRECT_PREDECESSOR_BINDING_TABLES) == 4
    assert len(kernel.PRODUCTION_RUNTIME_TABLES) == 36
    assert set(kernel.PRODUCTION_RUNTIME_TABLES).issubset(kernel.PRIVATE_TABLES)
    database = _database(tmp_path)
    database.initialize()
    ordinary = database.commit("v9-ordinary-receipt", "ordinary", _noop).receipt
    assert database.reconcile(ordinary.transaction_id).receipt == ordinary
    assert database.inspect_profile().user_version == 13
    with database._owned_connection_for_test() as owned:
        assert all(
            owned.execute("SELECT count(*) FROM %s" % table).fetchone()[0] == 0
            for table in kernel.PRODUCTION_RUNTIME_TABLES
        )


@pytest.mark.parametrize("table_name", kernel.PRODUCTION_RUNTIME_TABLES)
def test_generic_uow_cannot_read_or_write_any_current_production_runtime_table(
    tmp_path: Path,
    table_name: str,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    for access, statement in (
        ("read", "SELECT count(*) FROM %s" % table_name),
        ("write", "DELETE FROM %s" % table_name),
    ):
        transaction_id = "v8-private-%s-%s" % (
            access,
            table_name.replace("_", "-"),
        )
        with pytest.raises(kernel.SQLiteKernelError) as caught:
            database.commit(
                transaction_id,
                "ordinary",
                lambda uow, sql=statement: uow.execute(sql),
            )
        assert caught.value.code == "sqlite_uow_statement_forbidden"
        assert database.reconcile(transaction_id).status is (
            kernel.ReconcileStatus.NOT_COMMITTED
        )


@pytest.mark.parametrize(
    "ordinary_receipt",
    (False, True),
    ids=("legacy-empty", "ordinary-receipt"),
)
def test_inactive_v8_runtime_pollution_fails_closed(
    tmp_path: Path,
    ordinary_receipt: bool,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    if ordinary_receipt:
        database.commit("ordinary-before-v8-pollution", "ordinary", _noop)
    raw = _raw_database(database)
    try:
        raw.execute(
            "INSERT INTO worker_ledger_head("
            "singleton, state_revision, sequence, previous_tail_sha256, "
            "tail_sha256, updated_at) VALUES (1, 1, 0, zeroblob(32), "
            "zeroblob(32), ?)",
            (_NOW,),
        )
    finally:
        raw.close()

    with pytest.raises(kernel.SQLiteKernelError) as inspected:
        database.inspect_profile()
    assert inspected.value.code == "sqlite_production_runtime_state_unexpected"
    with pytest.raises(kernel.SQLiteKernelError) as initialized:
        database.initialize()
    assert initialized.value.code == "sqlite_production_runtime_state_unexpected"


def test_unmounted_v6_business_row_pollution_fails_closed(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    raw = _raw_database(database)
    try:
        raw.execute(
            """
            INSERT INTO asset_blobs(
                asset_sha256, content_key, media_type, byte_length, status,
                state_revision, created_at, updated_at
            ) VALUES (?, ?, 'application/octet-stream', 1, 'available', 1, ?, ?)
            """,
            (b"a" * 32, "sha256/61/" + (b"a" * 32).hex(), _NOW, _NOW),
        )
    finally:
        raw.close()

    with pytest.raises(kernel.SQLiteKernelError) as inspected:
        database.inspect_profile()
    assert inspected.value.code == "sqlite_business_state_unexpected"
    with pytest.raises(kernel.SQLiteKernelError) as initialized:
        database.initialize()
    assert initialized.value.code == "sqlite_business_state_unexpected"
