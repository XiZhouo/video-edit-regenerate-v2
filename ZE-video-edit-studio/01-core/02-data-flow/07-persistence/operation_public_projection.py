"""Private, allocation-aware projection of canonical Operation history.

Pipeline Start/Control descriptors are durable authority material, not public
transport payloads.  These helpers copy exact canonical JSON objects one field
at a time and skip ``op.pipeline`` before allocation.  Hash-chain fields remain
unchanged and receive an explicit marker because they bind the unredacted body.
"""

from __future__ import annotations

import copy
from typing import Any


def _project_public_operation_body(
    body: Any,
) -> tuple[dict[str, Any], bool]:
    if type(body) is not dict:
        raise TypeError("operation body must be an exact dict")
    redacted = "pipeline" in body
    projected = {
        key: copy.deepcopy(value)
        for key, value in body.items()
        if key != "pipeline"
    }
    return projected, redacted


def _project_public_history_entries(entries: Any) -> list[dict[str, Any]]:
    if type(entries) not in (list, tuple):
        raise TypeError("history entries must be an exact list or tuple")
    projected: list[dict[str, Any]] = []
    for entry in entries:
        if type(entry) is not dict or type(entry.get("op")) is not dict:
            raise TypeError("history entry must contain an exact Operation dict")
        public_operation, redacted = _project_public_operation_body(entry["op"])
        item = {
            key: copy.deepcopy(value)
            for key, value in entry.items()
            if key not in {
                "op",
                "canonical_body_redacted",
                "redacted_fields",
            }
        }
        item["op"] = public_operation
        if redacted:
            item["canonical_body_redacted"] = True
            item["redacted_fields"] = ["op.pipeline"]
        projected.append(item)
    return projected


__all__: list[str] = []
