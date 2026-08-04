"""Neutral immutable Patch carrier shared by data replay and Operation logs.

This leaf has no Document, Entity, persistence or command dependency.  The
operation module re-exports its public names for compatibility; Document may
therefore validate privileged history replay without importing upward into the
command/operation tree.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any


OPS = ("set", "delete", "create", "soft_delete", "restore", "name")
ENTITY_META_FIELD = "__entity__"
DOCUMENT_TARGET = "__document__"
EXTERNAL_TARGET = "__external__"


class _UnsupportedOperationValue:
    __slots__ = ()

    def __deepcopy__(self, _memo: Any) -> "_UnsupportedOperationValue":
        return self


_UNSUPPORTED_OPERATION_VALUE = _UnsupportedOperationValue()


def own_operation_value(value: Any) -> Any:
    """Detach JSON-like input without invoking user-defined copy hooks.

    Exact tuples remain tuples.  The strict codec has historically accepted
    them as JSON arrays and ``json.dumps`` emits the same canonical bytes as a
    list, so retaining them preserves both compatibility and input ownership.
    Dict keys retain the codec's historic scalar-key policy; unsupported keys
    remain safely invalid rather than being normalized into a new authority.
    """
    if value is None or type(value) in (bool, int, float, str):
        return value
    if type(value) is dict:
        return {
            own_operation_value(key): own_operation_value(item)
            for key, item in value.items()
        }
    if type(value) is list:
        return [own_operation_value(item) for item in value]
    if type(value) is tuple:
        return tuple(own_operation_value(item) for item in value)
    return _UNSUPPORTED_OPERATION_VALUE


@dataclass(frozen=True)
class Patch:
    """One immutable and exactly reversible field or lifecycle transition."""

    entity_type: str
    entity_id: str
    field: str
    op: str
    before: Any = None
    after: Any = None
    provenance: dict[str, Any] | None = None
    position: int | None = None
    ancestry: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        for name in ("entity_type", "entity_id", "field", "op"):
            if type(object.__getattribute__(self, name)) is not str:
                raise TypeError(f"Patch.{name} must be an exact string")
        position = object.__getattribute__(self, "position")
        if position is not None and type(position) is not int:
            raise TypeError("Patch.position must be an exact integer or None")
        # Detach every caller-owned mutable value while preserving the normal
        # public-field dataclass surface (__dict__, pickle, reflection, match).
        object.__setattr__(
            self,
            "before",
            own_operation_value(object.__getattribute__(self, "before")),
        )
        object.__setattr__(
            self,
            "after",
            own_operation_value(object.__getattribute__(self, "after")),
        )
        object.__setattr__(
            self,
            "provenance",
            own_operation_value(object.__getattribute__(self, "provenance")),
        )
        object.__setattr__(
            self,
            "ancestry",
            own_operation_value(object.__getattribute__(self, "ancestry")),
        )
        for name in ("provenance", "ancestry"):
            value = object.__getattribute__(self, name)
            if value is not None and type(value) is not dict:
                raise TypeError(f"Patch.{name} must be an exact dict or None")

    def __getattribute__(self, name: str) -> Any:
        if name == "__dict__":
            state = object.__getattribute__(self, "__dict__")
            return {
                key: copy.deepcopy(value)
                for key, value in state.items()
            }
        value = object.__getattribute__(self, name)
        if name in {"before", "after", "provenance", "ancestry"}:
            return copy.deepcopy(value)
        return value

    def __reduce_ex__(self, _protocol: int) -> tuple[Any, tuple[Any, ...]]:
        state = object.__getattribute__(self, "__dict__")
        return (
            type(self),
            tuple(
                copy.deepcopy(state[name])
                for name in (
                    "entity_type", "entity_id", "field", "op", "before",
                    "after", "provenance", "position", "ancestry",
                )
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        before = object.__getattribute__(self, "before")
        after = object.__getattribute__(self, "after")
        provenance = object.__getattribute__(self, "provenance")
        ancestry = object.__getattribute__(self, "ancestry")
        value = {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "field": self.field,
            "op": self.op,
            "before": copy.deepcopy(before),
            "after": copy.deepcopy(after),
        }
        if provenance is not None:
            value["provenance"] = copy.deepcopy(provenance)
        if self.position is not None:
            value["position"] = self.position
        if ancestry is not None:
            value["ancestry"] = copy.deepcopy(ancestry)
        return value


__all__ = [
    "DOCUMENT_TARGET",
    "ENTITY_META_FIELD",
    "EXTERNAL_TARGET",
    "OPS",
    "Patch",
    "own_operation_value",
]
