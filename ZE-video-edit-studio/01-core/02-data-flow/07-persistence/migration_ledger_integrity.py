"""Domain-separated, reproducible root for migration registry/ledger identity."""

from __future__ import annotations

import hashlib
from typing import Any, Tuple

from migration_ledger_contract import (
    MAX_MIGRATION_LEDGER_ENTRIES,
    MIGRATION_LEDGER_ROOT_HASH_DOMAIN,
    MigrationLedgerContractError,
    MigrationLedgerEntry,
)


class MigrationLedgerIntegrityError(ValueError):
    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise MigrationLedgerIntegrityError(code)


def _frame(hasher: Any, value: bytes) -> None:
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _canonical_entries(value: Any) -> Tuple[MigrationLedgerEntry, ...]:
    if (
        type(value) is not tuple
        or not value
        or len(value) > MAX_MIGRATION_LEDGER_ENTRIES
    ):
        _fail("migration_ledger_entries_invalid")
    rebuilt = []
    for expected_version, entry in enumerate(value, start=1):
        if type(entry) is not MigrationLedgerEntry:
            _fail("migration_ledger_entry_type_invalid")
        try:
            canonical = MigrationLedgerEntry(
                version=entry.version,
                name=entry.name,
                checksum_scheme=entry.checksum_scheme,
                checksum=entry.checksum,
            )
        except (MigrationLedgerContractError, TypeError):
            _fail("migration_ledger_entry_changed")
        if canonical != entry:
            _fail("migration_ledger_entry_changed")
        if canonical.version != expected_version:
            _fail("migration_ledger_sequence_invalid")
        rebuilt.append(canonical)
    return tuple(rebuilt)


def _migration_ledger_sha256(value: Any) -> bytes:
    """Return the one canonical root for an exact contiguous registry prefix."""

    entries = _canonical_entries(value)
    hasher = hashlib.sha256()
    _frame(hasher, MIGRATION_LEDGER_ROOT_HASH_DOMAIN)
    _frame(hasher, len(entries).to_bytes(8, "big"))
    for entry in entries:
        _frame(hasher, entry.version.to_bytes(8, "big"))
        _frame(hasher, entry.name.encode("utf-8"))
        _frame(hasher, entry.checksum_scheme.encode("ascii"))
        # The durable schema_migrations column stores the canonical lowercase
        # 64-byte hexadecimal text.  Hash that exact persisted representation,
        # not a decoded convenience form with a different wire identity.
        _frame(hasher, entry.checksum.encode("ascii"))
    return hasher.digest()


def _require_migration_ledger_root(value: Any, expected: Any) -> bytes:
    actual = _migration_ledger_sha256(value)
    if type(expected) is not bytes or len(expected) != 32 or actual != expected:
        _fail("migration_ledger_root_mismatch")
    return actual


__all__ = ()
