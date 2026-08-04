"""SDK adapter for allocation-aware public Operation/history projection.

The durable Operation body may contain framework authority material that is
needed for replay but must not be copied into HTTP, MCP, CLI, or browser
responses.  The neutral core leaf owns the redaction rule; this adapter selects
the Operation/OpLog fast seams and supplies a duck-typed fallback for tests and
non-canonical readers.  It never mutates the canonical value.
"""

from __future__ import annotations

from typing import Any

from operation_public_projection import (
    _project_public_history_entries,
    _project_public_operation_body,
)


def public_operation_payload(operation: Any) -> dict[str, Any]:
    """Return one detached public Operation body without authority metadata."""
    fast_projector = getattr(operation, "_public_operation_payload", None)
    if callable(fast_projector):
        body = fast_projector()
    elif hasattr(operation, "to_dict"):
        body = operation.to_dict()
        body, _redacted = _project_public_operation_body(body)
    elif type(operation) is dict:
        body, _redacted = _project_public_operation_body(operation)
    else:
        raise TypeError("operation must be an Operation-like value or exact dict")
    if type(body) is not dict:
        raise TypeError("operation projection must produce an exact dict")
    body.pop("pipeline", None)
    return body


def public_history_entries(entries: Any) -> list[dict[str, Any]]:
    """Return detached hash-chain entries with each Operation safely projected."""
    return _project_public_history_entries(entries)


def public_oplog_entries(
    op_log: Any,
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
    source: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Read a filtered public snapshot without first copying private bodies."""
    private_reader = getattr(op_log, "_public_history_snapshot", None)
    if callable(private_reader):
        return private_reader(
            entity_type=entity_type,
            entity_id=entity_id,
            source=source,
            limit=limit,
        )
    if entity_type and entity_id:
        rows = op_log.by_entity(entity_type, entity_id)
    elif source:
        rows = op_log.by_source(source)
    else:
        rows = op_log.entries()
    if limit is not None and limit > 0:
        rows = rows[-limit:]
    return public_history_entries(rows)


__all__: list[str] = []
