"""Pure declaration ownership for versioned entity mutation policies.

This module deliberately knows nothing about ``Document``, ``Operation`` or
command dispatch.  Its only job is to turn passive JSON declarations plus a
canonical entity schema into detached, closed-world policy values and stable
errors.  Runtime graph comparison and operation-audit concerns live elsewhere.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


_ENTITY_DIR = Path(__file__).resolve().parent.parent / "02-entity"
if str(_ENTITY_DIR) not in sys.path:
    sys.path.insert(0, str(_ENTITY_DIR))

from entity import ZeError, _clone_json_value, _require_json_string  # noqa: E402


MUTATION_POLICY_SCHEMA = "ze.entity-mutation-policy.v1"
MUTATION_POLICY_LIFECYCLE_ACTIONS = (
    "create", "rename", "soft_delete", "restore", "hard_remove",
)
MUTATION_POLICY_RESERVED_FIELD_NAMES = frozenset({
    "__entity__", "__deleted__", "__restored__",
})
MAX_MUTATION_POLICY_COMMAND_ID_LENGTH = 256
MAX_SQLITE_INTEGER = 2**63 - 1


def mutation_policy_error(reason: str, **context: Any) -> ZeError:
    return ZeError(
        "mutation_policy_invalid",
        "entity mutation_policy declaration is invalid",
        context={"reason": reason, **context},
    )


def own_mutation_policy_command_ids(
    value: Any,
    *,
    path: str,
    allow_empty: bool,
) -> list[str]:
    if type(value) is not list or (not allow_empty and not value):
        raise mutation_policy_error(
            "mutation_policy_command_list_invalid", path=path,
        )
    owned: list[str] = []
    seen: set[str] = set()
    for raw in value:
        try:
            command_id = _require_json_string(raw, non_empty=True)
        except ZeError:
            raise mutation_policy_error(
                "mutation_policy_command_id_invalid", path=path,
            ) from None
        if (
            len(command_id) > MAX_MUTATION_POLICY_COMMAND_ID_LENGTH
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in command_id)
        ):
            raise mutation_policy_error(
                "mutation_policy_command_id_invalid", path=path,
            )
        if command_id in seen:
            raise mutation_policy_error(
                "mutation_policy_command_id_duplicate",
                path=path,
                command_id=command_id,
            )
        seen.add(command_id)
        owned.append(command_id)
    return owned


def own_entity_mutation_policy(
    entity_type: str,
    schema: dict[str, Any],
    value: Any,
) -> dict[str, Any]:
    """Own one opt-in, closed-world entity mutation policy declaration."""
    try:
        raw = _clone_json_value(value)
    except ZeError:
        raise mutation_policy_error(
            "mutation_policy_json_invalid", entity_type=entity_type,
        ) from None
    if type(raw) is not dict or set(raw) not in (
        {"schema", "lifecycle", "fields"},
        {"schema", "lifecycle", "fields", "cardinality"},
    ):
        raise mutation_policy_error(
            "mutation_policy_shape_invalid", entity_type=entity_type,
        )
    if raw.get("schema") != MUTATION_POLICY_SCHEMA:
        raise mutation_policy_error(
            "mutation_policy_schema_unsupported", entity_type=entity_type,
        )

    lifecycle = raw.get("lifecycle")
    if type(lifecycle) is not dict or set(lifecycle) != set(
        MUTATION_POLICY_LIFECYCLE_ACTIONS
    ):
        raise mutation_policy_error(
            "mutation_policy_lifecycle_invalid", entity_type=entity_type,
        )
    owned_lifecycle = {
        action: own_mutation_policy_command_ids(
            lifecycle[action],
            path=f"$.lifecycle.{action}",
            allow_empty=True,
        )
        for action in MUTATION_POLICY_LIFECYCLE_ACTIONS
    }

    fields = raw.get("fields")
    if type(fields) is not dict:
        raise mutation_policy_error(
            "mutation_policy_fields_invalid", entity_type=entity_type,
        )
    schema_properties = schema.get("properties", {})
    if type(schema_properties) is not dict:
        schema_properties = {}
    for schema_field_name in schema_properties:
        if (
            schema_field_name in MUTATION_POLICY_RESERVED_FIELD_NAMES
            or "." in schema_field_name
        ):
            raise mutation_policy_error(
                "mutation_policy_field_reserved",
                entity_type=entity_type,
                field=schema_field_name,
            )
    owned_fields: dict[str, list[str]] = {}
    for field_name, command_ids in fields.items():
        if (
            type(field_name) is not str
            or not field_name
            or field_name not in schema_properties
        ):
            raise mutation_policy_error(
                "mutation_policy_field_undeclared",
                entity_type=entity_type,
                field=field_name if type(field_name) is str else "",
            )
        if (
            field_name in MUTATION_POLICY_RESERVED_FIELD_NAMES
            or "." in field_name
        ):
            raise mutation_policy_error(
                "mutation_policy_field_reserved",
                entity_type=entity_type,
                field=field_name,
            )
        owned_fields[field_name] = own_mutation_policy_command_ids(
            command_ids,
            path=f"$.fields.{field_name}",
            allow_empty=False,
        )

    owned: dict[str, Any] = {
        "schema": MUTATION_POLICY_SCHEMA,
        "lifecycle": owned_lifecycle,
        "fields": owned_fields,
    }
    if "cardinality" in raw:
        cardinality = raw["cardinality"]
        if type(cardinality) is not dict or set(cardinality) != {"max_total"}:
            raise mutation_policy_error(
                "mutation_policy_cardinality_invalid", entity_type=entity_type,
            )
        max_total = cardinality.get("max_total")
        if (
            type(max_total) is not int
            or max_total < 1
            or max_total > MAX_SQLITE_INTEGER
        ):
            raise mutation_policy_error(
                "mutation_policy_max_total_invalid", entity_type=entity_type,
            )
        owned["cardinality"] = {"max_total": max_total}
    return owned


def own_document_mutation_policies(
    value: Any,
    schema: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    try:
        raw = _clone_json_value(value)
    except ZeError:
        raise mutation_policy_error("mutation_policy_map_json_invalid") from None
    if type(raw) is not dict:
        raise mutation_policy_error("mutation_policy_map_invalid")
    owned: dict[str, dict[str, Any]] = {}
    for entity_type, policy in raw.items():
        if type(entity_type) is not str or entity_type not in schema:
            raise mutation_policy_error(
                "mutation_policy_entity_type_undeclared",
                entity_type=entity_type if type(entity_type) is str else "",
            )
        owned[entity_type] = own_entity_mutation_policy(
            entity_type, schema[entity_type], policy,
        )
    return owned


def mutation_policy_field_root(field_name: str) -> str:
    return field_name.split(".", 1)[0]


def mutation_policy_denied(
    *,
    command_id: str,
    entity_type: str,
    entity_id: str,
    action: str,
    field_name: str = "",
    reason: str = "mutation_policy_command_denied",
) -> ZeError:
    context = {
        "reason": reason,
        "command_id": command_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "action": action,
    }
    if field_name:
        context["field"] = field_name
    return ZeError(
        "mutation_policy_denied",
        "command is not authorized for the actual Document mutation",
        context=context,
    )


__all__ = [
    "MUTATION_POLICY_LIFECYCLE_ACTIONS",
    "MUTATION_POLICY_RESERVED_FIELD_NAMES",
    "MUTATION_POLICY_SCHEMA",
    "mutation_policy_denied",
    "mutation_policy_error",
    "mutation_policy_field_root",
    "own_document_mutation_policies",
    "own_entity_mutation_policy",
    "own_mutation_policy_command_ids",
]
