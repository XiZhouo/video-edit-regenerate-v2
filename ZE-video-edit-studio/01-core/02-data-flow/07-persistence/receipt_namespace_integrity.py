"""Streaming canonical root for all generic transaction receipts."""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Tuple

from receipt_namespace_contract import (
    EMPTY_RECEIPT_NAMESPACE_SHA256,
    MAX_RECEIPT_NAMESPACE_ENTRIES,
    RECEIPT_NAMESPACE_ROOT_HASH_DOMAIN,
    ReceiptNamespaceContractError,
    ReceiptNamespaceEntry,
)


class ReceiptNamespaceIntegrityError(ValueError):
    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise ReceiptNamespaceIntegrityError(code)


def _frame(hasher: Any, value: bytes) -> None:
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _canonical_entry(value: Any) -> ReceiptNamespaceEntry:
    if type(value) is not ReceiptNamespaceEntry:
        _fail("receipt_namespace_entry_type_invalid")
    try:
        rebuilt = ReceiptNamespaceEntry(
            transaction_id=value.transaction_id,
            kind=value.kind,
            committed_at=value.committed_at,
        )
    except (AttributeError, ReceiptNamespaceContractError, TypeError):
        _fail("receipt_namespace_entry_changed")
    if rebuilt != value:
        _fail("receipt_namespace_entry_changed")
    return rebuilt


def _receipt_namespace_sha256(
    values: Iterable[ReceiptNamespaceEntry],
    *,
    expected_count: int,
) -> Tuple[int, bytes]:
    """Hash one BINARY-ordered stream without materialising all receipt rows."""

    if (
        type(expected_count) is not int
        or expected_count < 0
        or expected_count > MAX_RECEIPT_NAMESPACE_ENTRIES
    ):
        _fail("receipt_namespace_count_invalid")
    try:
        iterator = iter(values)
    except TypeError:
        _fail("receipt_namespace_entries_invalid")
    if expected_count == 0:
        try:
            next(iterator)
        except StopIteration:
            return (0, EMPTY_RECEIPT_NAMESPACE_SHA256)
        _fail("receipt_namespace_count_mismatch")

    hasher = hashlib.sha256()
    _frame(hasher, RECEIPT_NAMESPACE_ROOT_HASH_DOMAIN)
    _frame(hasher, expected_count.to_bytes(8, "big"))
    count = 0
    previous_identity = None
    for raw in iterator:
        count += 1
        if count > expected_count:
            _fail("receipt_namespace_count_mismatch")
        entry = _canonical_entry(raw)
        identity = entry.transaction_id.encode("utf-8")
        if previous_identity is not None and identity <= previous_identity:
            _fail("receipt_namespace_order_invalid")
        previous_identity = identity
        _frame(hasher, identity)
        _frame(hasher, entry.kind.encode("utf-8"))
        _frame(hasher, entry.committed_at.encode("ascii"))
    if count != expected_count:
        _fail("receipt_namespace_count_mismatch")
    return (count, hasher.digest())


__all__ = ()
