"""Pure row contract for the complete generic transaction receipt namespace."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


RECEIPT_NAMESPACE_ROOT_PROTOCOL = "ze.sqlite-receipt-namespace-root.v1"
RECEIPT_NAMESPACE_ROOT_HASH_DOMAIN = b"ze.sqlite.receipt-namespace.v1"
EMPTY_RECEIPT_NAMESPACE_SHA256 = b"\0" * 32
MAX_RECEIPT_NAMESPACE_ENTRIES = 10_000_000
MAX_RECEIPT_NAMESPACE_TEXT_BYTES = 1024

class ReceiptNamespaceContractError(ValueError):
    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise ReceiptNamespaceContractError(code)


def _text(value: Any, code: str) -> str:
    if type(value) is not str or not value:
        _fail(code)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        _fail(code)
        raise AssertionError("unreachable")
    if len(encoded) > MAX_RECEIPT_NAMESPACE_TEXT_BYTES:
        _fail(code)
    return value


@dataclass(frozen=True)
class ReceiptNamespaceEntry:
    transaction_id: str
    kind: str
    committed_at: str

    def __post_init__(self) -> None:
        _text(self.transaction_id, "receipt_namespace_transaction_id_invalid")
        _text(self.kind, "receipt_namespace_kind_invalid")
        if type(self.committed_at) is not str or len(self.committed_at) != 24:
            _fail("receipt_namespace_committed_at_invalid")
        try:
            parsed = datetime.strptime(
                self.committed_at,
                "%Y-%m-%dT%H:%M:%S.%fZ",
            )
        except (TypeError, ValueError):
            _fail("receipt_namespace_committed_at_invalid")
            raise AssertionError("unreachable")
        canonical = parsed.strftime("%Y-%m-%dT%H:%M:%S.") + (
            "%03dZ" % (parsed.microsecond // 1000)
        )
        if canonical != self.committed_at:
            _fail("receipt_namespace_committed_at_invalid")


__all__ = ()
