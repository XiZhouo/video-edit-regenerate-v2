"""Exact durable Operation audit for protected Document mutations.

Dependency direction is intentionally one-way: the command/operation layer
imports the Document graph primitives and the Operation carrier.  The data
root never imports ``Operation`` merely to verify a command result.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


_LEAF_DIR = Path(__file__).resolve().parent
_DOCUMENT_DIR = _LEAF_DIR.parent.parent / "01-data-structure" / "01-document"
_ENTITY_DIR = _LEAF_DIR.parent.parent / "01-data-structure" / "02-entity"
_OPERATION_DIR = _LEAF_DIR.parent / "02-operation"
for _directory in (_DOCUMENT_DIR, _ENTITY_DIR, _OPERATION_DIR):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

from entity import ZeError, _clone_json_value  # noqa: E402
from mutation_policy_graph import (  # noqa: E402
    DocumentMutationSnapshot,
    _MUTATION_AUDIT_MISSING,
    _json_values_exact,
    _mutation_audit_apply_ancestry,
    _mutation_audit_apply_provenance,
    _mutation_audit_delete_path,
    _mutation_audit_document_projection,
    _mutation_audit_empty_bucket_projection,
    _mutation_audit_entity_projection,
    _mutation_audit_get_path,
    _mutation_audit_path_segments,
    _mutation_audit_set_path,
    _mutation_audit_snapshot_projection,
    _mutation_fingerprint_bytes,
    _mutation_operation_patch_error,
)
from operation import ENTITY_META_FIELD, Operation, Patch  # noqa: E402


MutationTouch = tuple[str, str, str, str]


def _canonical_operation_bodies(
    operations: tuple[Operation, ...],
) -> tuple[bytes, ...]:
    """Seal every durable Operation field without truthiness normalization."""
    bodies: list[bytes] = []
    for operation in operations:
        try:
            bodies.append(_mutation_fingerprint_bytes(
                operation.to_dict(),
                reason="mutation_operation_body_invalid",
            ))
        except (AttributeError, TypeError, ValueError, ZeError):
            raise _mutation_operation_patch_error(
                "mutation_operation_body_invalid"
            ) from None
    return tuple(bodies)


def validate_mutation_operation_coverage(
    document: Any,
    snapshot: DocumentMutationSnapshot,
    actual_touches: frozenset[MutationTouch] | None,
    operations: tuple[Operation, ...],
    *,
    reject_duplicate_transitions: bool = True,
) -> None:
    """Prove the durable patch program exactly replays and reverses the delta."""
    if (
        type(snapshot) is not DocumentMutationSnapshot
        or snapshot._owner is not document
    ):
        raise TypeError("snapshot must belong to this Document")
    if actual_touches is not None and type(actual_touches) is not frozenset:
        raise TypeError("actual_touches must be a frozenset or None")
    if type(reject_duplicate_transitions) is not bool:
        raise TypeError("reject_duplicate_transitions must be an exact bool")
    if (
        type(operations) is not tuple
        or not operations
        or any(type(operation) is not Operation for operation in operations)
    ):
        raise TypeError("operations must contain exact Operation values")
    sealed_operation_bodies = _canonical_operation_bodies(operations)

    protected_types = set(document._mutation_policies)

    def collect(attribute: str) -> frozenset[MutationTouch]:
        touches: set[MutationTouch] = set()
        for operation in operations:
            patches = getattr(operation, attribute)
            for patch in patches:
                if type(patch) is not Patch:
                    raise ZeError(
                        "mutation_operation_patch_mismatch",
                        "protected mutation audit contains a non-Patch value",
                    )
                if patch.entity_type not in protected_types:
                    continue
                if type(patch.entity_id) is not str or not patch.entity_id:
                    raise _mutation_operation_patch_error(
                        "mutation_operation_target_invalid"
                    )
                if patch.field == ENTITY_META_FIELD:
                    if patch.op not in {
                        "create", "delete", "soft_delete", "restore", "name",
                    }:
                        raise ZeError(
                            "mutation_operation_patch_mismatch",
                            "protected entity meta patch has an invalid operation",
                            context={"op": patch.op},
                        )
                    action = patch.op
                    field_name = ""
                elif patch.op == "name":
                    _mutation_audit_path_segments(patch.field)
                    action = "name"
                    field_name = ""
                elif patch.op in {"set", "delete"}:
                    _mutation_audit_path_segments(patch.field)
                    action = "field"
                    field_name = patch.field
                else:
                    raise ZeError(
                        "mutation_operation_patch_mismatch",
                        "protected field patch has an invalid operation",
                        context={"op": patch.op},
                    )
                touches.add((
                    patch.entity_type,
                    patch.entity_id,
                    action,
                    field_name,
                ))
        return frozenset(touches)

    reported_forward = collect("patches")
    command_actual_touches = actual_touches
    if actual_touches is None:
        actual_touches = reported_forward
    reverse_action = {
        "create": "delete",
        "delete": "create",
        "soft_delete": "restore",
        "restore": "soft_delete",
        "name": "name",
        "field": "field",
    }
    expected_reverse = frozenset(
        (entity_type, entity_id, reverse_action[action], field_name)
        for entity_type, entity_id, action, field_name in actual_touches
    )
    reported_reverse = collect("reverse_patches")
    if reported_forward != actual_touches or reported_reverse != expected_reverse:
        def render(values: frozenset[MutationTouch]) -> list[str]:
            return sorted(
                f"{entity_type}/{entity_id}:{action}"
                + (f":{field_name}" if field_name else "")
                for entity_type, entity_id, action, field_name in values
            )

        raise ZeError(
            "mutation_operation_patch_mismatch",
            "Operation patches do not exactly cover the protected Document delta",
            context={
                "missing_forward": render(actual_touches - reported_forward),
                "extra_forward": render(reported_forward - actual_touches),
                "missing_reverse": render(expected_reverse - reported_reverse),
                "extra_reverse": render(reported_reverse - expected_reverse),
            },
        )

    if command_actual_touches is not None:
        _validate_lifecycle_envelopes(
            document,
            snapshot,
            command_actual_touches,
            operations,
            protected_types,
        )

    touched_entity_keys = (
        None
        if command_actual_touches is None
        else {
            (entity_type, entity_id)
            for entity_type, entity_id, _action, _field_name
            in command_actual_touches
        }
    )
    before_projection = _mutation_audit_snapshot_projection(
        snapshot, protected_types, touched_entity_keys,
    )
    after_projection = _mutation_audit_document_projection(
        document, protected_types, touched_entity_keys,
    )
    forward_patches = [
        patch for operation in operations for patch in operation.patches
    ]
    reverse_patches = [
        patch
        for operation in reversed(operations)
        for patch in operation.reverse_patches
    ]
    replayed = _apply_patch_sequence(
        before_projection,
        forward_patches,
        protected_types=protected_types,
        reject_duplicate_transitions=reject_duplicate_transitions,
    )
    if not _json_values_exact(replayed, after_projection):
        raise _mutation_operation_patch_error(
            "mutation_operation_forward_replay_mismatch"
        )
    reversed_projection = _apply_patch_sequence(
        after_projection,
        reverse_patches,
        protected_types=protected_types,
        reject_duplicate_transitions=reject_duplicate_transitions,
    )
    if not _json_values_exact(reversed_projection, before_projection):
        raise _mutation_operation_patch_error(
            "mutation_operation_reverse_replay_mismatch"
        )
    if _canonical_operation_bodies(operations) != sealed_operation_bodies:
        raise _mutation_operation_patch_error(
            "mutation_operation_body_changed"
        )


def _validate_lifecycle_envelopes(
    document: Any,
    snapshot: DocumentMutationSnapshot,
    actual_touches: frozenset[MutationTouch],
    operations: tuple[Operation, ...],
    protected_types: set[str],
) -> None:
    """Bind lifecycle Patch payloads to exact before/after Entity envelopes."""
    lifecycle_keys = {
        (entity_type, entity_id)
        for entity_type, entity_id, action, _ in actual_touches
        if action in {"create", "delete", "soft_delete", "restore"}
    }
    if not lifecycle_keys:
        return
    snapshot_payloads: dict[tuple[str, str], dict[str, Any]] = {}
    snapshot_records = {
        id(record.entity): record
        for record in snapshot._entities
        if (record.state.get("_type"), record.state.get("_id"))
        in lifecycle_keys
    }
    for bucket in snapshot._buckets:
        if bucket.entity_type not in protected_types:
            continue
        for entity_id, entity in bucket.members:
            if (bucket.entity_type, entity_id) not in lifecycle_keys:
                continue
            record = snapshot_records.get(id(entity))
            if record is None or record.entity is not entity:
                raise _mutation_operation_patch_error(
                    "mutation_operation_snapshot_inventory_invalid"
                )
            try:
                payload = json.loads(record.durable_bytes.decode("utf-8", "strict"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise _mutation_operation_patch_error(
                    "mutation_operation_snapshot_payload_invalid"
                ) from None
            snapshot_payloads[(bucket.entity_type, entity_id)] = payload
    live_payloads = {
        (entity_type, entity_id): entity.to_dict()
        for entity_type in protected_types
        for entity_id, entity in document._collections.get(entity_type, {}).items()
        if (entity_type, entity_id) in lifecycle_keys
    }
    forward_meta: dict[tuple[str, str, str], list[Patch]] = {}
    reverse_meta: dict[tuple[str, str, str], list[Patch]] = {}
    for operation in operations:
        for patches, target in (
            (operation.patches, forward_meta),
            (operation.reverse_patches, reverse_meta),
        ):
            for patch in patches:
                if (
                    patch.entity_type in protected_types
                    and patch.field == ENTITY_META_FIELD
                    and patch.op in {
                        "create", "delete", "soft_delete", "restore",
                    }
                ):
                    target.setdefault((
                        patch.entity_type, patch.entity_id, patch.op,
                    ), []).append(patch)
    reverse_for_action = {
        "create": "delete",
        "delete": "create",
        "soft_delete": "restore",
        "restore": "soft_delete",
    }
    lifecycle_actions_by_key: dict[tuple[str, str], set[str]] = {}
    for entity_type, entity_id, action, _ in actual_touches:
        if action in reverse_for_action:
            lifecycle_actions_by_key.setdefault(
                (entity_type, entity_id), set(),
            ).add(action)
    replacement_keys = {
        key for key, actions in lifecycle_actions_by_key.items()
        if {"delete", "create"}.issubset(actions)
    }

    # A same-id replacement has two real intermediate states even though its
    # before/after inventory contains the key at both endpoints:
    # old --delete--> missing --create--> new.  Bind all four lifecycle
    # envelopes here; the strict sequence replay below proves their ordering
    # and collection positions in both directions.
    for entity_type, entity_id in replacement_keys:
        before_payload = snapshot_payloads.get((entity_type, entity_id))
        after_payload = live_payloads.get((entity_type, entity_id))
        forward_delete = forward_meta.get((entity_type, entity_id, "delete"), [])
        forward_create = forward_meta.get((entity_type, entity_id, "create"), [])
        reverse_delete = reverse_meta.get((entity_type, entity_id, "delete"), [])
        reverse_create = reverse_meta.get((entity_type, entity_id, "create"), [])
        if (
            before_payload is None
            or after_payload is None
            or any(len(values) != 1 for values in (
                forward_delete, forward_create, reverse_delete, reverse_create,
            ))
        ):
            raise _mutation_operation_patch_error(
                "mutation_operation_lifecycle_envelope_ambiguous"
            )
        old_to_missing = forward_delete[0]
        missing_to_new = forward_create[0]
        new_to_missing = reverse_delete[0]
        missing_to_old = reverse_create[0]
        if not (
            _json_values_exact(old_to_missing.before, before_payload)
            and old_to_missing.after is None
            and missing_to_new.before is None
            and _json_values_exact(missing_to_new.after, after_payload)
            and _json_values_exact(new_to_missing.before, after_payload)
            and new_to_missing.after is None
            and missing_to_old.before is None
            and _json_values_exact(missing_to_old.after, before_payload)
        ):
            raise _mutation_operation_patch_error(
                "mutation_operation_lifecycle_envelope_forged"
            )

    for entity_type, entity_id, action, _ in actual_touches:
        if action not in reverse_for_action:
            continue
        if (entity_type, entity_id) in replacement_keys:
            continue
        before_payload = snapshot_payloads.get((entity_type, entity_id))
        after_payload = live_payloads.get((entity_type, entity_id))
        forward = forward_meta.get((entity_type, entity_id, action), [])
        reverse = reverse_meta.get((
            entity_type, entity_id, reverse_for_action[action],
        ), [])
        if len(forward) != 1 or len(reverse) != 1:
            raise _mutation_operation_patch_error(
                "mutation_operation_lifecycle_envelope_ambiguous"
            )
        forward_patch = forward[0]
        reverse_patch = reverse[0]
        if action == "create":
            exact = (
                before_payload is None
                and after_payload is not None
                and forward_patch.before is None
                and _json_values_exact(forward_patch.after, after_payload)
                and _json_values_exact(reverse_patch.before, after_payload)
                and reverse_patch.after is None
            )
        elif action == "delete":
            exact = (
                before_payload is not None
                and after_payload is None
                and _json_values_exact(forward_patch.before, before_payload)
                and forward_patch.after is None
                and reverse_patch.before is None
                and _json_values_exact(reverse_patch.after, before_payload)
            )
        else:
            exact = (
                before_payload is not None
                and after_payload is not None
                and _json_values_exact(forward_patch.before, before_payload)
                and _json_values_exact(forward_patch.after, after_payload)
                and _json_values_exact(reverse_patch.before, after_payload)
                and _json_values_exact(reverse_patch.after, before_payload)
            )
        if not exact:
            raise _mutation_operation_patch_error(
                "mutation_operation_lifecycle_envelope_forged"
            )


def _apply_patch_sequence(
    start: dict[str, dict[str, Any]],
    patch_sequence: list[Patch],
    *,
    protected_types: set[str],
    reject_duplicate_transitions: bool,
) -> dict[str, dict[str, Any]]:
    """Replay a strict Patch sequence over an immutable business projection."""
    state = _clone_json_value(start)
    if type(state) is not dict:
        raise _mutation_operation_patch_error(
            "mutation_operation_projection_invalid"
        )
    seen_transitions: set[bytes] = set()
    for patch in patch_sequence:
        if patch.entity_type not in protected_types:
            continue
        try:
            signature = _mutation_fingerprint_bytes(
                patch.to_dict(),
                reason="mutation_operation_patch_value_invalid",
            )
        except ZeError:
            raise _mutation_operation_patch_error(
                "mutation_operation_patch_value_invalid"
            ) from None
        if reject_duplicate_transitions and signature in seen_transitions:
            raise _mutation_operation_patch_error(
                "mutation_operation_duplicate_transition"
            )
        seen_transitions.add(signature)

        entity_type = patch.entity_type
        entity_id = patch.entity_id
        bucket = state.setdefault(
            entity_type, _mutation_audit_empty_bucket_projection(),
        )
        if (
            type(bucket) is not dict
            or set(bucket) != {"order", "entities"}
            or type(bucket["order"]) is not list
            or type(bucket["entities"]) is not dict
        ):
            raise _mutation_operation_patch_error(
                "mutation_operation_projection_invalid"
            )
        order = bucket["order"]
        entities = bucket["entities"]
        exists = entity_id in entities

        if patch.field == ENTITY_META_FIELD:
            if patch.op == "create":
                if (
                    patch.before is not None
                    or exists
                    or type(patch.position) is not int
                    or patch.position < 0
                    or patch.position > len(order)
                    or patch.provenance is not None
                    or patch.ancestry is not None
                ):
                    raise _mutation_operation_patch_error(
                        "mutation_operation_create_precondition_failed"
                    )
                entities[entity_id] = _mutation_audit_entity_projection(
                    patch.after,
                    entity_type=entity_type,
                    entity_id=entity_id,
                )
                order.insert(patch.position, entity_id)
                continue
            if patch.op == "delete":
                if (
                    not exists
                    or patch.after is not None
                    or type(patch.position) is not int
                    or patch.position < 0
                    or patch.position >= len(order)
                    or order[patch.position] != entity_id
                    or patch.provenance is not None
                    or patch.ancestry is not None
                ):
                    raise _mutation_operation_patch_error(
                        "mutation_operation_delete_precondition_failed"
                    )
                before_value = _mutation_audit_entity_projection(
                    patch.before,
                    entity_type=entity_type,
                    entity_id=entity_id,
                )
                if not _json_values_exact(entities[entity_id], before_value):
                    raise _mutation_operation_patch_error(
                        "mutation_operation_before_value_mismatch"
                    )
                del entities[entity_id]
                del order[patch.position]
                continue
            if patch.op in {"soft_delete", "restore"}:
                if (
                    not exists
                    or patch.position is not None
                    or patch.provenance is not None
                    or patch.ancestry is not None
                ):
                    raise _mutation_operation_patch_error(
                        "mutation_operation_lifecycle_precondition_failed"
                    )
                before_value = _mutation_audit_entity_projection(
                    patch.before,
                    entity_type=entity_type,
                    entity_id=entity_id,
                )
                after_value = _mutation_audit_entity_projection(
                    patch.after,
                    entity_type=entity_type,
                    entity_id=entity_id,
                )
                if not _json_values_exact(entities[entity_id], before_value):
                    raise _mutation_operation_patch_error(
                        "mutation_operation_before_value_mismatch"
                    )
                expected = _clone_json_value(before_value)
                expected["deleted"] = patch.op == "soft_delete"
                if not _json_values_exact(after_value, expected):
                    raise _mutation_operation_patch_error(
                        "mutation_operation_lifecycle_value_mismatch"
                    )
                entities[entity_id] = after_value
                continue
            if patch.op == "name":
                _apply_name_patch(entities[entity_id], patch)
                continue
            raise _mutation_operation_patch_error(
                "mutation_operation_meta_op_invalid"
            )

        if not exists:
            raise _mutation_operation_patch_error(
                "mutation_operation_field_target_missing"
            )
        if patch.op == "name":
            _apply_name_patch(entities[entity_id], patch)
            continue
        if patch.position is not None:
            raise _mutation_operation_patch_error(
                "mutation_operation_position_invalid"
            )

        current = _mutation_audit_get_path(
            entities[entity_id]["data"], patch.field,
        )
        before_matches = (
            patch.before is None
            if current is _MUTATION_AUDIT_MISSING
            else _json_values_exact(current, patch.before)
        )
        if not before_matches:
            raise _mutation_operation_patch_error(
                "mutation_operation_before_value_mismatch"
            )
        if patch.op == "set":
            try:
                before_data = entities[entity_id]["data"]
                candidate_data = _mutation_audit_set_path(
                    before_data, patch.field, patch.after,
                )
                entities[entity_id]["data"] = _mutation_audit_apply_ancestry(
                    before_data, candidate_data, patch,
                )
            except ZeError:
                raise _mutation_operation_patch_error(
                    "mutation_operation_after_value_invalid"
                ) from None
        elif patch.op == "delete":
            if patch.after is not None:
                raise _mutation_operation_patch_error(
                    "mutation_operation_delete_value_invalid"
                )
            before_data = entities[entity_id]["data"]
            candidate_data = _mutation_audit_delete_path(
                before_data, patch.field,
            )
            entities[entity_id]["data"] = _mutation_audit_apply_ancestry(
                before_data, candidate_data, patch,
            )
        else:
            raise _mutation_operation_patch_error(
                "mutation_operation_field_op_invalid"
            )
        _mutation_audit_apply_provenance(entities[entity_id], patch)
    return state


def _apply_name_patch(entity: dict[str, Any], patch: Patch) -> None:
    if (
        patch.position is not None
        or patch.provenance is not None
        or patch.ancestry is not None
        or type(patch.before) is not str
        or type(patch.after) is not str
        or not _json_values_exact(entity["name"], patch.before)
    ):
        raise _mutation_operation_patch_error(
            "mutation_operation_name_precondition_failed"
        )
    entity["name"] = patch.after


__all__ = ["validate_mutation_operation_coverage"]
