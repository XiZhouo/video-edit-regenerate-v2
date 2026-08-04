from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from hash_framing import framed_size, update_hash_frame


def test_hash_frames_have_one_stable_golden_digest() -> None:
    hasher = hashlib.sha256()
    update_hash_frame(hasher, b"ze.hash-frame.test.v1")
    update_hash_frame(hasher, b"")
    update_hash_frame(hasher, "世界".encode("utf-8"))
    assert hasher.hexdigest() == (
        "e3184f191cf667a5aff6b9f4652e3fafeb7daf65793273f6ceac6bf16ea85a14"
    )
    assert framed_size(b"") == 8
    assert framed_size(b"abc") == 11


@pytest.mark.parametrize("value", [None, bytearray(b"x"), memoryview(b"x"), "x"])
def test_hash_frames_reject_non_exact_bytes(value) -> None:
    with pytest.raises(TypeError, match="exact bytes"):
        framed_size(value)
    with pytest.raises(TypeError, match="exact bytes"):
        update_hash_frame(hashlib.sha256(), value)


def test_hash_frames_require_a_hash_target() -> None:
    with pytest.raises(TypeError, match="provide update"):
        update_hash_frame(object(), b"x")
