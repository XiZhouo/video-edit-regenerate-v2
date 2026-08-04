"""Atomic JSON persistence — .tmp → replace (crash-safe).

seedance-site-v2 used a plain non-atomic write for its task cache (a torn-write
risk under crash); its project/download writes were atomic. This module promotes
the atomic discipline to everything: one helper for all project + task JSON so a
kill mid-write never leaves a half-file.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any


def write_json_atomic(path: str, payload: Any) -> None:
    """Write ``payload`` as JSON to ``path`` atomically (tmp in same dir → replace)."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)  # atomic rename on POSIX
    except BaseException:
        _silent_remove(tmp)
        raise


def read_json(path: str, default: Any = None) -> Any:
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _silent_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
