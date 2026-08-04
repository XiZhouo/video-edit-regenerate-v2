from __future__ import annotations

import ast
import sqlite3
from pathlib import Path
import sys

import pytest


_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import receipt_namespace_contract as contract_module
import receipt_namespace_integrity as integrity_module
from receipt_namespace_contract import (
    EMPTY_RECEIPT_NAMESPACE_SHA256,
    ReceiptNamespaceEntry,
)
from receipt_namespace_integrity import (
    ReceiptNamespaceIntegrityError,
    _receipt_namespace_sha256,
)


@pytest.mark.parametrize(
    "timestamp",
    (
        "2026-99-99T99:99:99.999Z",
        "0000-00-00T00:00:00.000Z",
        "2025-02-29T00:00:00.000Z",
    ),
)
def test_calendar_invalid_timestamps_are_rejected(timestamp: str) -> None:
    with pytest.raises(ValueError) as rejected:
        ReceiptNamespaceEntry("receipt", "kind", timestamp)
    assert rejected.value.code == "receipt_namespace_committed_at_invalid"


def _rows() -> tuple:
    return (
        ReceiptNamespaceEntry("a:1", "kind-a", "2026-07-12T00:00:00.000Z"),
        ReceiptNamespaceEntry("业务:2", "kind-b", "2026-07-12T00:00:01.000Z"),
    )


def test_empty_and_nonempty_fixed_roots() -> None:
    assert _receipt_namespace_sha256((), expected_count=0) == (
        0,
        EMPTY_RECEIPT_NAMESPACE_SHA256,
    )
    count, root = _receipt_namespace_sha256(iter(_rows()), expected_count=2)
    assert count == 2
    assert root.hex() == "e6507fc8b5d4ef0fd9f8822d5efea4c923fbad6aa9bec0f9101238e823e11d7b"


@pytest.mark.parametrize(
    "rows,count,code",
    (
        ((_rows()[0],), 2, "receipt_namespace_count_mismatch"),
        (_rows(), 1, "receipt_namespace_count_mismatch"),
        ((_rows()[1], _rows()[0]), 2, "receipt_namespace_order_invalid"),
        ((_rows()[0], _rows()[0]), 2, "receipt_namespace_order_invalid"),
    ),
)
def test_count_order_and_uniqueness_are_exact(rows, count, code) -> None:
    with pytest.raises(ReceiptNamespaceIntegrityError) as rejected:
        _receipt_namespace_sha256(iter(rows), expected_count=count)
    assert rejected.value.code == code


def test_every_durable_field_changes_root() -> None:
    root = _receipt_namespace_sha256(_rows(), expected_count=2)[1]
    variants = (
        (
            ReceiptNamespaceEntry("a:1", "other", _rows()[0].committed_at),
            _rows()[1],
        ),
        (
            ReceiptNamespaceEntry("a:1", "kind-a", "2026-07-12T00:00:02.000Z"),
            _rows()[1],
        ),
    )
    assert all(
        _receipt_namespace_sha256(value, expected_count=2)[1] != root
        for value in variants
    )


def test_utf8_sqlite_binary_order_matches_the_canonical_byte_order() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        assert connection.execute("PRAGMA encoding").fetchone()[0] == "UTF-8"
        connection.execute("CREATE TABLE receipts(transaction_id TEXT PRIMARY KEY)")
        identities = ("业务:2", "a:1", "é:3", "z:4")
        connection.executemany(
            "INSERT INTO receipts(transaction_id) VALUES (?)",
            ((value,) for value in identities),
        )
        sqlite_order = tuple(
            row[0]
            for row in connection.execute(
                "SELECT transaction_id FROM receipts "
                "ORDER BY transaction_id COLLATE BINARY"
            )
        )
        assert sqlite_order == tuple(
            sorted(identities, key=lambda value: value.encode("utf-8"))
        )
    finally:
        connection.close()


def test_deep_tamper_is_rebuilt_before_hashing() -> None:
    value = _rows()[0]
    object.__setattr__(value, "committed_at", "not-a-time")
    with pytest.raises(ReceiptNamespaceIntegrityError) as rejected:
        _receipt_namespace_sha256((value,), expected_count=1)
    assert rejected.value.code == "receipt_namespace_entry_changed"


def test_non_iterable_input_has_one_stable_error() -> None:
    with pytest.raises(ReceiptNamespaceIntegrityError) as rejected:
        _receipt_namespace_sha256(None, expected_count=1)
    assert rejected.value.code == "receipt_namespace_entries_invalid"


def test_modules_are_private_pure_python39_leaves() -> None:
    root = Path(__file__).resolve().parent
    for name in ("receipt_namespace_contract.py", "receipt_namespace_integrity.py"):
        source = (root / name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])
            assert type(node).__name__ != "Match"
        assert imported.isdisjoint({"sqlite3", "sqlite_schema", "sqlite_kernel"})
        assert " | " not in source
    assert contract_module.__all__ == ()
    assert integrity_module.__all__ == ()
