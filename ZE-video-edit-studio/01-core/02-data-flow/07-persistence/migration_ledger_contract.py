"""Pure canonical identity contract for the immutable SQLite migration ledger.

The durable ``applied_at`` timestamp is audited separately and deliberately
excluded: two repositories created at different times must have the same
target-ledger identity when version, name, checksum scheme and checksum are
identical.  This module performs no SQLite or registry I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


MIGRATION_LEDGER_ROOT_PROTOCOL = "ze.sqlite-migration-ledger-root.v1"
MIGRATION_LEDGER_ROOT_HASH_DOMAIN = b"ze.sqlite.migration-ledger.v1"
MAX_MIGRATION_LEDGER_ENTRIES = 4096
MAX_MIGRATION_LEDGER_TEXT_BYTES = 1024

_LOWER_HEX = frozenset("0123456789abcdef")
_CHECKSUM_SCHEMES = frozenset(("legacy-whitespace-v1", "exact-utf8-v2"))


class MigrationLedgerContractError(ValueError):
    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise MigrationLedgerContractError(code)


def _text(value: Any, code: str) -> str:
    if type(value) is not str or not value:
        _fail(code)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        _fail(code)
        raise AssertionError("unreachable")
    if len(encoded) > MAX_MIGRATION_LEDGER_TEXT_BYTES:
        _fail(code)
    return value


@dataclass(frozen=True)
class MigrationLedgerEntry:
    version: int
    name: str
    checksum_scheme: str
    checksum: str

    def __post_init__(self) -> None:
        if (
            type(self.version) is not int
            or self.version < 1
            or self.version > 2**63 - 1
        ):
            _fail("migration_ledger_version_invalid")
        _text(self.name, "migration_ledger_name_invalid")
        if (
            type(self.checksum_scheme) is not str
            or self.checksum_scheme not in _CHECKSUM_SCHEMES
        ):
            _fail("migration_ledger_checksum_scheme_invalid")
        if (
            type(self.checksum) is not str
            or len(self.checksum) != 64
            or any(character not in _LOWER_HEX for character in self.checksum)
        ):
            _fail("migration_ledger_checksum_invalid")


__all__ = ()
