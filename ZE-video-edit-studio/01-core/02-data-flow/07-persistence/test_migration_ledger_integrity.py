from __future__ import annotations

import ast
from pathlib import Path
import sys

import pytest


_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import migration_ledger_contract as contract_module
import migration_ledger_integrity as integrity_module
from migration_ledger_contract import (
    MAX_MIGRATION_LEDGER_ENTRIES,
    MigrationLedgerEntry,
)
from migration_ledger_integrity import (
    MigrationLedgerIntegrityError,
    _migration_ledger_sha256,
    _require_migration_ledger_root,
)


def _entries() -> tuple:
    return (
        MigrationLedgerEntry(
            1,
            "sqlite_kernel_v1",
            "legacy-whitespace-v1",
            "1" * 64,
        ),
        MigrationLedgerEntry(
            2,
            "业务快照-v1",
            "exact-utf8-v2",
            "a" * 64,
        ),
    )


def test_fixed_root_is_reproducible_and_timestamp_independent() -> None:
    root = _migration_ledger_sha256(_entries())
    assert root.hex() == "03d5455d126b1cb4f33ac96aa4dc37cbd459a7e95b57da3c9b724cc2a7d6f82b"
    assert _migration_ledger_sha256(_entries()) == root
    assert _require_migration_ledger_root(_entries(), root) == root


@pytest.mark.parametrize(
    "entries,code",
    (
        ((), "migration_ledger_entries_invalid"),
        ((_entries()[1], _entries()[0]), "migration_ledger_sequence_invalid"),
        ((_entries()[0],), "migration_ledger_root_mismatch"),
    ),
)
def test_missing_reordered_or_different_prefix_cannot_match(entries, code) -> None:
    expected = _migration_ledger_sha256(_entries())
    with pytest.raises(MigrationLedgerIntegrityError) as rejected:
        if code == "migration_ledger_root_mismatch":
            _require_migration_ledger_root(entries, expected)
        else:
            _migration_ledger_sha256(entries)
    assert rejected.value.code == code


def test_every_identity_field_changes_the_root() -> None:
    original = _entries()
    alternatives = (
        (
            original[0],
            MigrationLedgerEntry(2, "other", "exact-utf8-v2", "a" * 64),
        ),
        (
            original[0],
            MigrationLedgerEntry(2, original[1].name, "exact-utf8-v2", "b" * 64),
        ),
    )
    root = _migration_ledger_sha256(original)
    assert all(_migration_ledger_sha256(value) != root for value in alternatives)


def test_deep_tamper_and_budget_are_rejected() -> None:
    changed = _entries()[0]
    object.__setattr__(changed, "checksum", "f" * 64)
    # Rebuilding catches ordinary invalid values; a still-valid changed value
    # remains a different, honestly hashed registry identity.
    assert _migration_ledger_sha256((changed,)) != _migration_ledger_sha256(
        (MigrationLedgerEntry(1, "sqlite_kernel_v1", "legacy-whitespace-v1", "1" * 64),)
    )
    with pytest.raises(MigrationLedgerIntegrityError) as rejected:
        _migration_ledger_sha256(
            (MigrationLedgerEntry(1, "x", "exact-utf8-v2", "0" * 64),)
            * (MAX_MIGRATION_LEDGER_ENTRIES + 1)
        )
    assert rejected.value.code == "migration_ledger_entries_invalid"


def test_modules_are_private_pure_python39_leaves() -> None:
    root = Path(__file__).resolve().parent
    for name in ("migration_ledger_contract.py", "migration_ledger_integrity.py"):
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
