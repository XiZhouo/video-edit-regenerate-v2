"""Shared domain-separated SHA-256 length framing for persistence contracts.

Every caller supplies exact bytes.  A frame is ``uint64_be(length) || payload``;
domain labels, counts, manifests and canonical row payloads all use the same
unambiguous grammar.  This module owns no business schema and performs no I/O.
"""

from __future__ import annotations

from typing import Any


MAX_FRAME_LENGTH = 2**64 - 1


def framed_size(payload: bytes) -> int:
    """Return the exact encoded frame size after validating the byte boundary."""
    if type(payload) is not bytes:
        raise TypeError("hash frame payload must be exact bytes")
    if len(payload) > MAX_FRAME_LENGTH:
        raise ValueError("hash frame payload is too large")
    return 8 + len(payload)


def update_hash_frame(hasher: Any, payload: bytes) -> None:
    """Stream one exact length-prefixed frame into a hashlib-compatible hash."""
    size = framed_size(payload) - 8
    update = getattr(hasher, "update", None)
    if not callable(update):
        raise TypeError("hash frame target must provide update(bytes)")
    update(size.to_bytes(8, "big"))
    update(payload)


__all__ = ["MAX_FRAME_LENGTH", "framed_size", "update_hash_frame"]
