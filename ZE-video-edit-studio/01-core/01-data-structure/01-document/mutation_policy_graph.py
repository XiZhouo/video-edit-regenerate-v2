"""Protected Document graph ownership, snapshots, deltas and authorization.

This data-structure leaf turns mutation-policy declarations into checks against
the actual Document+Entity graph.  It deliberately has no command, Operation,
persistence, or Document-module dependency: Document consumes the mixin and
re-exports the snapshot carrier for compatibility.
"""

from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ENTITY_DIR = _HERE.parent / "02-entity"
_FIELD_TYPES_DIR = _HERE.parent / "04-field-types"
for _directory in (_ENTITY_DIR, _FIELD_TYPES_DIR):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

from entity import (
    Actor,
    AiProvenance,
    Entity,
    ZeError,
    _clone_json_value,
    _no_op_validator,
)
from field_types import is_framework_validator
from mutation_policy_declaration import (
    mutation_policy_denied as _mutation_policy_denied,
    mutation_policy_error as _mutation_policy_error,
    mutation_policy_field_root as _mutation_policy_field_root,
)

_MAX_SQLITE_INTEGER = 2**63 - 1


def _has_exact_string_keys(value: Any, expected: set[str]) -> bool:
    # Check type before membership.  ``set(value)`` and ``all(... key ...)``
    # both add measurable hot-path work, and set construction would hash an
    # attacker-supplied key before proving it is an exact passive string.
    if type(value) is not dict or len(value) != len(expected):
        return False
    for key in value.keys():
        if type(key) is not str or key not in expected:
            return False
    return True


@dataclass(frozen=True)
class _EntityMutationState:
    """Opaque snapshot record that keeps the original Entity identity."""

    entity: Entity = field(repr=False, compare=False)
    state: dict[str, Any] = field(repr=False, compare=False)
    durable_bytes: bytes = field(repr=False)
    policy_protected: bool = field(repr=False)
    schema_authority: Any = field(repr=False, compare=False)
    validator_authority: Any = field(repr=False, compare=False)
    document_owner: Any = field(repr=False, compare=False)
    admission_schema_authority: Any = field(repr=False, compare=False)
    admission_validator_authority: Any = field(repr=False, compare=False)


@dataclass(frozen=True)
class _BucketMutationState:
    """One original collection bucket plus its exact ordered membership."""

    entity_type: str
    bucket: dict[str, Entity] = field(repr=False, compare=False)
    members: tuple[tuple[str, Entity], ...] = field(repr=False, compare=False)


@dataclass(frozen=True)
class _AdmittedEntityMutationState:
    """Standalone authority that must be restored if admission is later removed."""

    entity: Entity = field(repr=False, compare=False)
    schema_authority: Any = field(repr=False, compare=False)
    schema_bytes: bytes = field(repr=False)
    validator_authority: Any = field(repr=False, compare=False)


@dataclass(frozen=True)
class DocumentMutationSnapshot:
    """Opaque, full in-memory checkpoint owned by one Document object.

    This is a framework transaction primitive, not a serialized project or a
    public editing format.  It deliberately retains the original Document,
    collection buckets, and Entity objects so rollback can restore state
    without invalidating references held by editors, caches, or command code.

    Capturing is O(all Document metadata + all Entity state) in both time and
    memory.  That cost is the W04 correctness baseline until every mutation is
    observable through a before-mutation journal hook.
    """

    _owner: Any = field(repr=False, compare=False)
    _document_state: dict[str, Any] = field(repr=False, compare=False)
    _collections: Any = field(repr=False, compare=False)
    _collections_default_factory: Any = field(repr=False, compare=False)
    _buckets: tuple[_BucketMutationState, ...] = field(repr=False, compare=False)
    _entities: tuple[_EntityMutationState, ...] = field(repr=False, compare=False)
    # Internal authority extensions stay out of the long-standing public
    # six-argument constructor.  ``capture_mutation_snapshot`` installs them
    # immediately with object.__setattr__; external legacy constructors retain
    # their exact positional meaning and fail closed if used as authorities.
    _schema_authority: Any = field(
        default=None, init=False, repr=False, compare=False,
    )
    _mutation_policy_authority: Any = field(
        default=None, init=False, repr=False, compare=False,
    )
    _mutation_policy_registry_authority: Any = field(
        default=None, init=False, repr=False, compare=False,
    )
    _mutation_policy_registry_epoch: Any = field(
        default=None, init=False, repr=False, compare=False,
    )
    _mutation_policy_command_authority: Any = field(
        default=None, init=False, repr=False, compare=False,
    )
    _history_op_log_authority: Any = field(
        default=None, init=False, repr=False, compare=False,
    )
    _history_controller_authority: Any = field(
        default=None, init=False, repr=False, compare=False,
    )
    _history_authority_epoch: Any = field(
        default=None, init=False, repr=False, compare=False,
    )
    _admitted_entities: list[_AdmittedEntityMutationState] = field(
        default_factory=list, init=False, repr=False, compare=False,
    )

    @property
    def document_id(self) -> str:
        """Document id at capture time; ownership is still checked by identity."""
        value = self._document_state.get("_id", "")
        return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class _ProtectedEntityFingerprint:
    entity_type: str
    entity_id: str
    entity: Entity = field(repr=False, compare=False)
    durable_bytes: bytes = field(repr=False)
    schema: Any = field(repr=False, compare=False)
    schema_bytes: bytes = field(repr=False)
    validator: Any = field(repr=False, compare=False)
    document_owner: Any = field(repr=False, compare=False)
    admission_schema: Any = field(repr=False, compare=False)
    admission_schema_bytes: bytes = field(repr=False)
    admission_validator: Any = field(repr=False, compare=False)


@dataclass(frozen=True)
class _ProtectedBucketFingerprint:
    entity_type: str
    present: bool
    bucket: Any = field(repr=False, compare=False)
    members: tuple[_ProtectedEntityFingerprint, ...] = field(repr=False)


@dataclass(frozen=True)
class _ProtectedMutationFingerprint:
    schema_authority: Any = field(repr=False, compare=False)
    schema_bytes: bytes = field(repr=False)
    mutation_policy_authority: Any = field(repr=False, compare=False)
    mutation_policy_bytes: bytes = field(repr=False)
    mutation_policy_sealed: bool = field(repr=False)
    mutation_policy_registry_authority: Any = field(
        repr=False, compare=False,
    )
    mutation_policy_registry_epoch: Any = field(repr=False, compare=False)
    mutation_policy_registry_pid: int | None = field(repr=False)
    mutation_policy_command_authority: Any = field(
        repr=False, compare=False,
    )
    mutation_policy_command_items: tuple[tuple[str, Any], ...] = field(
        repr=False, compare=False,
    )
    history_op_log_authority: Any = field(repr=False, compare=False)
    history_controller_authority: Any = field(repr=False, compare=False)
    history_authority_epoch: Any = field(repr=False, compare=False)
    history_authority_pid: int | None = field(repr=False)
    collections: Any = field(repr=False, compare=False)
    collections_default_factory: Any = field(repr=False, compare=False)
    buckets: tuple[_ProtectedBucketFingerprint, ...] = field(repr=False)


def _mutation_fingerprint_bytes(value: Any, *, reason: str) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise ZeError(
            "mutation_policy_authority_invalid",
            "runtime mutation authority cannot be fingerprinted",
            context={"reason": reason, "error_type": type(exc).__name__},
        ) from exc


def _json_values_exact(left: Any, right: Any) -> bool:
    if type(left) is AiProvenance:
        left = left.to_dict()
    if type(right) is AiProvenance:
        right = right.to_dict()
    try:
        return _mutation_fingerprint_bytes(
            left, reason="mutation_policy_comparison_invalid",
        ) == _mutation_fingerprint_bytes(
            right, reason="mutation_policy_comparison_invalid",
        )
    except ZeError:
        return False


def _mutation_policy_referenced_commands(
    policies: dict[str, Any],
) -> tuple[str, ...]:
    referenced: set[str] = set()
    for policy in policies.values():
        for command_ids in policy["lifecycle"].values():
            referenced.update(command_ids)
        for command_ids in policy["fields"].values():
            referenced.update(command_ids)
    return tuple(sorted(referenced))


def _command_authority_items_match(
    left: Any, right: Any,
) -> bool:
    if type(left) is not dict or type(right) is not dict:
        return False
    if tuple(left) != tuple(right):
        return False
    return all(left[key] is right[key] for key in left)


def _mutation_policy_seal_state(
    document: Any,
    *,
    require_current_process: bool,
) -> tuple[Any, Any, int | None, dict[str, Any], bool]:
    policies = object.__getattribute__(document, "_mutation_policies")
    sealed = object.__getattribute__(document, "_mutation_policy_sealed")
    registry = object.__getattribute__(
        document, "_mutation_policy_registry_authority",
    )
    epoch = object.__getattribute__(document, "_mutation_policy_registry_epoch")
    issuer_pid = object.__getattribute__(
        document, "_mutation_policy_registry_pid",
    )
    commands = object.__getattribute__(
        document, "_mutation_policy_command_authorities",
    )
    if type(policies) is not dict or type(sealed) is not bool or type(commands) is not dict:
        raise ZeError(
            "mutation_policy_authority_invalid",
            "runtime mutation policy seal has an invalid shape",
            context={"reason": "mutation_policy_seal_shape_invalid"},
        )
    referenced = _mutation_policy_referenced_commands(policies)
    if any(type(key) is not str or not key for key in commands):
        raise ZeError(
            "mutation_policy_authority_invalid",
            "runtime mutation command bindings are invalid",
            context={"reason": "mutation_policy_command_binding_invalid"},
        )
    if sealed:
        if (
            registry is None
            or epoch is None
            or type(issuer_pid) is not int
            or tuple(commands) != referenced
            or any(value is None for value in commands.values())
        ):
            raise ZeError(
                "mutation_policy_registry_authority_mismatch",
                "sealed mutation policy registry authority is incomplete",
                context={"reason": "mutation_policy_registry_seal_incomplete"},
            )
        if require_current_process and issuer_pid != os.getpid():
            raise ZeError(
                "mutation_policy_registry_authority_mismatch",
                "sealed mutation policy registry belongs to another process",
                context={"reason": "mutation_policy_registry_pid_mismatch"},
            )
    elif (
        registry is not None
        or epoch is not None
        or issuer_pid is not None
        or commands
    ):
        raise ZeError(
            "mutation_policy_registry_authority_mismatch",
            "unsealed mutation policy retained opaque registry authority",
            context={"reason": "mutation_policy_unsealed_authority_present"},
        )
    return registry, epoch, issuer_pid, commands, sealed


def _canonical_history_authority_state(
    document: Any,
    *,
    require_current_process: bool,
) -> tuple[Any, Any, Any, int | None]:
    """Return one coherent process-local canonical-history binding.

    The Document leaf deliberately does not import OpLog or UndoController.
    It consumes only their exact opaque identities and a tiny private protocol,
    preserving the one-way dependency from history into Document.
    """
    op_log = object.__getattribute__(document, "_history_op_log_authority")
    controller = object.__getattribute__(
        document, "_history_controller_authority",
    )
    epoch = object.__getattribute__(document, "_history_authority_epoch")
    issuer_pid = object.__getattribute__(document, "_history_authority_pid")
    if op_log is None and controller is None and epoch is None and issuer_pid is None:
        return None, None, None, None
    if (
        op_log is None
        or controller is None
        or epoch is None
        or type(issuer_pid) is not int
        or getattr(controller, "_op_log", None) is not op_log
        or getattr(controller, "_doc", None) is not document
        or getattr(controller, "_history_authority_epoch", None) is not epoch
        or getattr(controller, "_history_authority_pid", None) != issuer_pid
    ):
        raise ZeError(
            "history_authority_mismatch",
            "canonical history authority is incomplete or internally inconsistent",
            context={"reason": "history_authority_binding_invalid"},
        )
    if require_current_process and issuer_pid != os.getpid():
        raise ZeError(
            "history_authority_mismatch",
            "canonical history authority belongs to another process",
            context={"reason": "history_authority_pid_mismatch"},
        )
    return op_log, controller, epoch, issuer_pid


_MUTATION_AUDIT_ENTITY_KEYS = frozenset({
    "id", "type", "name", "version", "deleted", "read_only", "data",
    "provenance", "actor_log", "created_at", "updated_at",
})
_MUTATION_AUDIT_MISSING = object()


def _mutation_operation_patch_error(reason: str) -> ZeError:
    return ZeError(
        "mutation_operation_patch_mismatch",
        "Operation patches cannot exactly replay the protected Document delta",
        context={"reason": reason},
    )


def _mutation_audit_entity_projection(
    value: Any,
    *,
    entity_type: str,
    entity_id: str,
) -> dict[str, Any]:
    """Own one exact durable Entity envelope for patch-program replay."""
    try:
        owned = _clone_json_value(value)
    except ZeError:
        raise _mutation_operation_patch_error(
            "mutation_operation_entity_envelope_invalid"
        ) from None
    if (
        type(owned) is not dict
        or frozenset(owned) != _MUTATION_AUDIT_ENTITY_KEYS
        or owned.get("id") != entity_id
        or owned.get("type") != entity_type
        or type(owned.get("id")) is not str
        or type(owned.get("type")) is not str
        or type(owned.get("name")) is not str
        or type(owned.get("version")) is not int
        or owned["version"] < 1
        or type(owned.get("deleted")) is not bool
        or type(owned.get("read_only")) is not bool
        or type(owned.get("data")) is not dict
        or type(owned.get("provenance")) is not dict
        or type(owned.get("actor_log")) is not list
        or type(owned.get("created_at")) not in (int, float)
        or type(owned.get("updated_at")) not in (int, float)
        or not math.isfinite(float(owned["created_at"]))
        or not math.isfinite(float(owned["updated_at"]))
        or owned["updated_at"] < owned["created_at"]
    ):
        raise _mutation_operation_patch_error(
            "mutation_operation_entity_envelope_invalid"
        )
    # Version/timestamps/actor_log are independently authorized against the
    # real snapshot delta.  Field Patch programs intentionally model only the
    # reversible business graph; requiring them to synthesize framework clock
    # and audit bookkeeping would make every ordinary field patch incomplete.
    return {
        "id": owned["id"],
        "type": owned["type"],
        "name": owned["name"],
        "deleted": owned["deleted"],
        "read_only": owned["read_only"],
        "data": owned["data"],
        "provenance": owned["provenance"],
    }


def _mutation_audit_empty_bucket_projection() -> dict[str, Any]:
    return {"order": [], "entities": {}}


def _mutation_audit_snapshot_projection(
    snapshot: DocumentMutationSnapshot,
    protected_types: set[str],
    entity_keys: set[tuple[str, str]] | None = None,
) -> dict[str, dict[str, Any]]:
    projection = {
        entity_type: _mutation_audit_empty_bucket_projection()
        for entity_type in protected_types
    }
    records = {
        id(record.entity): record
        for record in snapshot._entities
        if entity_keys is None or (
            record.state.get("_type"), record.state.get("_id")
        ) in entity_keys
    }
    for bucket in snapshot._buckets:
        if bucket.entity_type not in protected_types:
            continue
        target = projection.setdefault(
            bucket.entity_type, _mutation_audit_empty_bucket_projection(),
        )
        if target["order"] or target["entities"]:
            raise _mutation_operation_patch_error(
                "mutation_operation_snapshot_inventory_invalid"
            )
        for entity_id, entity in bucket.members:
            target["order"].append(entity_id)
            if (
                entity_keys is not None
                and (bucket.entity_type, entity_id) not in entity_keys
            ):
                continue
            record = records.get(id(entity))
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
            target["entities"][entity_id] = _mutation_audit_entity_projection(
                payload,
                entity_type=bucket.entity_type,
                entity_id=entity_id,
            )
    return projection


def _mutation_audit_document_projection(
    document: Any,
    protected_types: set[str],
    entity_keys: set[tuple[str, str]] | None = None,
) -> dict[str, dict[str, Any]]:
    projection = {
        entity_type: _mutation_audit_empty_bucket_projection()
        for entity_type in protected_types
    }
    collections = object.__getattribute__(document, "_collections")
    for entity_type in protected_types:
        bucket = collections.get(entity_type)
        if bucket is None:
            continue
        if type(bucket) is not dict:
            raise _mutation_operation_patch_error(
                "mutation_operation_document_inventory_invalid"
            )
        target = projection[entity_type]
        for entity_id, entity in bucket.items():
            if type(entity_id) is not str or type(entity) is not Entity:
                raise _mutation_operation_patch_error(
                    "mutation_operation_document_inventory_invalid"
                )
            target["order"].append(entity_id)
            if (
                entity_keys is not None
                and (entity_type, entity_id) not in entity_keys
            ):
                continue
            target["entities"][entity_id] = _mutation_audit_entity_projection(
                entity.to_dict(),
                entity_type=entity_type,
                entity_id=entity_id,
            )
    return projection


def _mutation_audit_provenance_state(value: Any) -> tuple[bool, Any]:
    if (
        type(value) is not dict
        or set(value) != {"present", "value"}
        or type(value.get("present")) is not bool
    ):
        raise _mutation_operation_patch_error(
            "mutation_operation_provenance_invalid"
        )
    if not value["present"]:
        if value["value"] is not None:
            raise _mutation_operation_patch_error(
                "mutation_operation_provenance_invalid"
            )
        return False, None
    payload = value["value"]
    if type(payload) is not dict:
        raise _mutation_operation_patch_error(
            "mutation_operation_provenance_invalid"
        )
    try:
        restored = AiProvenance(**payload)
        owned = restored.to_dict()
    except Exception:
        raise _mutation_operation_patch_error(
            "mutation_operation_provenance_invalid"
        ) from None
    if not _json_values_exact(owned, payload):
        raise _mutation_operation_patch_error(
            "mutation_operation_provenance_invalid"
        )
    return True, owned


def _mutation_audit_json_endpoint(value: Any) -> tuple[bool, Any]:
    if (
        type(value) is not dict
        or set(value) != {"present", "value"}
        or type(value.get("present")) is not bool
    ):
        raise _mutation_operation_patch_error(
            "mutation_operation_ancestry_invalid"
        )
    if not value["present"]:
        if value["value"] is not None:
            raise _mutation_operation_patch_error(
                "mutation_operation_ancestry_invalid"
            )
        return False, None
    try:
        return True, _clone_json_value(value["value"])
    except ZeError:
        raise _mutation_operation_patch_error(
            "mutation_operation_ancestry_invalid"
        ) from None


def _mutation_audit_apply_ancestry(
    before_data: dict[str, Any],
    candidate_data: dict[str, Any],
    patch: Any,
) -> dict[str, Any]:
    envelope = patch.ancestry
    if envelope is None:
        return candidate_data
    root_field = _mutation_policy_field_root(patch.field)
    if (
        "." not in patch.field
        or type(envelope) is not dict
        or set(envelope) != {"field", "before", "after"}
        or envelope.get("field") != root_field
    ):
        raise _mutation_operation_patch_error(
            "mutation_operation_ancestry_invalid"
        )
    before_present, before_value = _mutation_audit_json_endpoint(
        envelope["before"]
    )
    after_present, after_value = _mutation_audit_json_endpoint(
        envelope["after"]
    )
    current_present = root_field in before_data
    if current_present is not before_present or (
        current_present
        and not _json_values_exact(before_data[root_field], before_value)
    ):
        raise _mutation_operation_patch_error(
            "mutation_operation_ancestry_precondition_failed"
        )
    result = _clone_json_value(before_data)
    if type(result) is not dict:
        raise _mutation_operation_patch_error(
            "mutation_operation_data_invalid"
        )
    if after_present:
        result[root_field] = after_value
    else:
        result.pop(root_field, None)
    resulting_leaf = _mutation_audit_get_path(result, patch.field)
    if patch.op == "set":
        if (
            resulting_leaf is _MUTATION_AUDIT_MISSING
            or not _json_values_exact(resulting_leaf, patch.after)
        ):
            raise _mutation_operation_patch_error(
                "mutation_operation_ancestry_result_mismatch"
            )
    elif patch.op == "delete":
        if patch.after is not None or resulting_leaf is not _MUTATION_AUDIT_MISSING:
            raise _mutation_operation_patch_error(
                "mutation_operation_ancestry_result_mismatch"
            )
    else:
        raise _mutation_operation_patch_error(
            "mutation_operation_ancestry_invalid"
        )
    return result


def _mutation_audit_apply_provenance(
    entity: dict[str, Any],
    patch: Any,
) -> None:
    envelope = patch.provenance
    if envelope is None:
        return
    if (
        type(envelope) is not dict
        or set(envelope) != {"field", "before", "after"}
        or envelope.get("field") != patch.field
        or type(entity.get("provenance")) is not dict
    ):
        raise _mutation_operation_patch_error(
            "mutation_operation_provenance_invalid"
        )
    before_present, before_value = _mutation_audit_provenance_state(
        envelope["before"]
    )
    after_present, after_value = _mutation_audit_provenance_state(
        envelope["after"]
    )
    provenance = entity["provenance"]
    current_present = patch.field in provenance
    if current_present is not before_present or (
        current_present
        and not _json_values_exact(provenance[patch.field], before_value)
    ):
        raise _mutation_operation_patch_error(
            "mutation_operation_provenance_precondition_failed"
        )
    if after_present:
        provenance[patch.field] = after_value
    else:
        provenance.pop(patch.field, None)

def _apply_history_patch_provenance(entity: Entity, patch: Any) -> None:
    """Apply one validated reversible provenance endpoint to a live Entity."""
    envelope = patch.provenance
    if envelope is None:
        return
    if (
        type(envelope) is not dict
        or set(envelope) != {"field", "before", "after"}
        or envelope.get("field") != patch.field
    ):
        raise ZeError(
            "validation_failed",
            "history replay provenance envelope is invalid",
            context={"reason": "history_replay_provenance_invalid"},
        )
    before_present, before_value = _mutation_audit_provenance_state(
        envelope["before"]
    )
    after_present, after_value = _mutation_audit_provenance_state(
        envelope["after"]
    )
    current = entity.provenance(patch.field)
    current_present = current is not None
    if current_present is not before_present or (
        current_present
        and not _json_values_exact(current.to_dict(), before_value)
    ):
        raise ZeError(
            "validation_failed",
            "history replay provenance precondition does not match the Entity",
            context={"reason": "history_replay_provenance_precondition_failed"},
        )
    candidate = dict(object.__getattribute__(entity, "_provenance"))
    if after_present:
        candidate[patch.field] = AiProvenance(**after_value)
    else:
        candidate.pop(patch.field, None)
    entity._provenance = candidate
    # Re-run the complete durable-envelope clone budget before the enclosing
    # replay transaction is allowed to publish this private state change.
    entity.to_dict()


def _history_replay_ancestry_data(
    entity: Entity,
    patch: Any,
) -> dict[str, Any] | None:
    if patch.ancestry is None:
        return None
    before_data = entity.data
    current = _mutation_audit_get_path(before_data, patch.field)
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
        candidate_data = _mutation_audit_set_path(
            before_data, patch.field, patch.after,
        )
    elif patch.op == "delete" and patch.after is None:
        candidate_data = _mutation_audit_delete_path(
            before_data, patch.field,
        )
    else:
        raise _mutation_operation_patch_error(
            "mutation_operation_ancestry_invalid"
        )
    return _mutation_audit_apply_ancestry(
        before_data, candidate_data, patch,
    )


def _mutation_audit_path_segments(field_name: Any) -> tuple[str, ...]:
    if type(field_name) is not str or not field_name:
        raise _mutation_operation_patch_error("mutation_operation_field_invalid")
    segments = tuple(field_name.split("."))
    if any(not segment for segment in segments):
        raise _mutation_operation_patch_error("mutation_operation_field_invalid")
    return segments


def _mutation_audit_get_path(data: dict[str, Any], field_name: str) -> Any:
    node: Any = data
    for segment in _mutation_audit_path_segments(field_name):
        if type(node) is not dict or segment not in node:
            return _MUTATION_AUDIT_MISSING
        node = node[segment]
    return node


def _mutation_audit_set_path(
    data: dict[str, Any], field_name: str, value: Any,
) -> dict[str, Any]:
    segments = _mutation_audit_path_segments(field_name)
    owned_value = _clone_json_value(value)
    root = _clone_json_value(data)
    if type(root) is not dict:
        raise _mutation_operation_patch_error("mutation_operation_data_invalid")
    node = root
    for segment in segments[:-1]:
        child = node.get(segment)
        if type(child) is not dict:
            child = {}
        else:
            child = _clone_json_value(child)
        node[segment] = child
        node = child
    node[segments[-1]] = owned_value
    return root


def _mutation_audit_delete_path(
    data: dict[str, Any], field_name: str,
) -> dict[str, Any]:
    segments = _mutation_audit_path_segments(field_name)
    root = _clone_json_value(data)
    if type(root) is not dict:
        raise _mutation_operation_patch_error("mutation_operation_data_invalid")
    node: Any = root
    for segment in segments[:-1]:
        if type(node) is not dict or type(node.get(segment)) is not dict:
            return root
        node[segment] = _clone_json_value(node[segment])
        node = node[segment]
    if type(node) is dict:
        node.pop(segments[-1], None)
    return root


def _protected_mutation_fingerprint(document: "Document") -> _ProtectedMutationFingerprint:
    policies = object.__getattribute__(document, "_mutation_policies")
    schema = object.__getattribute__(document, "_schema")
    (
        registry_authority,
        registry_epoch,
        registry_pid,
        command_authority,
        sealed,
    ) = _mutation_policy_seal_state(
        document, require_current_process=True,
    )
    (
        history_op_log,
        history_controller,
        history_epoch,
        history_pid,
    ) = _canonical_history_authority_state(
        document, require_current_process=True,
    )
    collections = object.__getattribute__(document, "_collections")
    if type(policies) is not dict or type(schema) is not dict or type(sealed) is not bool:
        raise ZeError(
            "mutation_policy_authority_invalid",
            "runtime mutation declaration authority must be exact objects",
            context={"reason": "mutation_policy_authority_shape_invalid"},
        )
    buckets: list[_ProtectedBucketFingerprint] = []
    for entity_type in sorted(policies):
        present = entity_type in collections
        bucket = collections[entity_type] if present else None
        members: list[_ProtectedEntityFingerprint] = []
        if present:
            if type(bucket) is not dict:
                raise ZeError(
                    "mutation_policy_authority_invalid",
                    "protected collection bucket must be an exact dict",
                    context={
                        "reason": "mutation_policy_bucket_invalid",
                        "entity_type": entity_type,
                    },
                )
            for entity_id, entity in bucket.items():
                if type(entity_id) is not str or type(entity) is not Entity:
                    raise ZeError(
                        "mutation_policy_authority_invalid",
                        "protected collection membership is invalid",
                        context={
                            "reason": "mutation_policy_collection_identity_invalid",
                            "entity_type": entity_type,
                        },
                    )
                state = object.__getattribute__(entity, "__dict__")
                admission = state.get("_document_admission_state")
                if admission is None:
                    admission_schema = None
                    admission_schema_bytes = b"null"
                    admission_validator = None
                elif (
                    type(admission) is tuple
                    and len(admission) == 2
                    and type(admission[0]) is dict
                    and callable(admission[1])
                ):
                    admission_schema = admission[0]
                    admission_schema_bytes = _mutation_fingerprint_bytes(
                        admission_schema,
                        reason="mutation_policy_admission_schema_invalid",
                    )
                    admission_validator = admission[1]
                else:
                    raise ZeError(
                        "mutation_policy_authority_invalid",
                        "protected Entity admission authority is invalid",
                        context={
                            "reason": "mutation_policy_entity_authority_invalid",
                            "entity_type": entity_type,
                            "entity_id": entity_id,
                        },
                    )
                entity_schema = state.get("_schema")
                if type(entity_schema) is not dict:
                    raise ZeError(
                        "mutation_policy_authority_invalid",
                        "protected Entity schema authority is invalid",
                        context={
                            "reason": "mutation_policy_entity_authority_invalid",
                            "entity_type": entity_type,
                            "entity_id": entity_id,
                        },
                    )
                members.append(_ProtectedEntityFingerprint(
                    entity_type=entity_type,
                    entity_id=entity_id,
                    entity=entity,
                    durable_bytes=_mutation_fingerprint_bytes(
                        entity.to_dict(), reason="mutation_policy_entity_state_invalid",
                    ),
                    schema=entity_schema,
                    schema_bytes=_mutation_fingerprint_bytes(
                        entity_schema, reason="mutation_policy_entity_schema_invalid",
                    ),
                    validator=state.get("_validator"),
                    document_owner=state.get("_document_owner"),
                    admission_schema=admission_schema,
                    admission_schema_bytes=admission_schema_bytes,
                    admission_validator=admission_validator,
                ))
        buckets.append(_ProtectedBucketFingerprint(
            entity_type=entity_type,
            present=present,
            bucket=bucket,
            members=tuple(members),
        ))
    return _ProtectedMutationFingerprint(
        schema_authority=schema,
        schema_bytes=_mutation_fingerprint_bytes(
            schema, reason="mutation_policy_schema_authority_invalid",
        ),
        mutation_policy_authority=policies,
        mutation_policy_bytes=_mutation_fingerprint_bytes(
            policies, reason="mutation_policy_authority_value_invalid",
        ),
        mutation_policy_sealed=sealed,
        mutation_policy_registry_authority=registry_authority,
        mutation_policy_registry_epoch=registry_epoch,
        mutation_policy_registry_pid=registry_pid,
        mutation_policy_command_authority=command_authority,
        mutation_policy_command_items=tuple(command_authority.items()),
        history_op_log_authority=history_op_log,
        history_controller_authority=history_controller,
        history_authority_epoch=history_epoch,
        history_authority_pid=history_pid,
        collections=collections,
        collections_default_factory=getattr(collections, "default_factory", None),
        buckets=tuple(buckets),
    )


def _protected_fingerprints_match(
    left: _ProtectedMutationFingerprint,
    right: _ProtectedMutationFingerprint,
) -> bool:
    if (
        left.schema_authority is not right.schema_authority
        or left.schema_bytes != right.schema_bytes
        or left.mutation_policy_authority is not right.mutation_policy_authority
        or left.mutation_policy_bytes != right.mutation_policy_bytes
        or left.mutation_policy_sealed is not right.mutation_policy_sealed
        or left.mutation_policy_registry_authority
        is not right.mutation_policy_registry_authority
        or left.mutation_policy_registry_epoch
        is not right.mutation_policy_registry_epoch
        or left.mutation_policy_registry_pid != right.mutation_policy_registry_pid
        or left.mutation_policy_command_authority
        is not right.mutation_policy_command_authority
        or len(left.mutation_policy_command_items)
        != len(right.mutation_policy_command_items)
        or left.history_op_log_authority is not right.history_op_log_authority
        or left.history_controller_authority
        is not right.history_controller_authority
        or left.history_authority_epoch is not right.history_authority_epoch
        or left.history_authority_pid != right.history_authority_pid
        or left.collections is not right.collections
        or left.collections_default_factory is not right.collections_default_factory
        or len(left.buckets) != len(right.buckets)
    ):
        return False
    for (left_id, left_binding), (right_id, right_binding) in zip(
        left.mutation_policy_command_items,
        right.mutation_policy_command_items,
    ):
        if left_id != right_id or left_binding is not right_binding:
            return False
    for before_bucket, after_bucket in zip(left.buckets, right.buckets):
        if (
            before_bucket.entity_type != after_bucket.entity_type
            or before_bucket.present is not after_bucket.present
            or before_bucket.bucket is not after_bucket.bucket
            or len(before_bucket.members) != len(after_bucket.members)
        ):
            return False
        for before, after in zip(before_bucket.members, after_bucket.members):
            if (
                before.entity_type != after.entity_type
                or before.entity_id != after.entity_id
                or before.entity is not after.entity
                or before.durable_bytes != after.durable_bytes
                or before.schema is not after.schema
                or before.schema_bytes != after.schema_bytes
                or before.validator is not after.validator
                or before.document_owner is not after.document_owner
                or before.admission_schema is not after.admission_schema
                or before.admission_schema_bytes != after.admission_schema_bytes
                or before.admission_validator is not after.admission_validator
            ):
                return False
    return True


def _copy_entity_state_mapping(state: dict[str, Any]) -> dict[str, Any]:
    """Copy Entity state without ever invoking a validator deepcopy hook."""
    expected = {
        "_id", "_type", "_name", "_data", "_version", "_deleted",
        "_read_only", "_schema", "_validator", "_provenance",
        "_document_owner", "_document_admission_state", "_actor_log",
        "_created_at", "_updated_at",
    }
    if not _has_exact_string_keys(state, expected):
        raise TypeError("Entity state shape is invalid")
    copied: dict[str, Any] = {}
    for key, value in state.items():
        if key in ("_validator", "_document_owner"):
            copied[key] = value
            continue
        if key == "_document_admission_state":
            if value is None:
                copied[key] = None
                continue
            if (
                type(value) is not tuple
                or len(value) != 2
                or type(value[0]) is not dict
                or not callable(value[1])
            ):
                raise TypeError("Entity admission state is invalid")
            copied[key] = (_clone_json_value(value[0]), value[1])
            continue
        if key == "_provenance":
            if type(value) is not dict:
                raise TypeError("Entity provenance state is invalid")
            restored_provenance: dict[str, AiProvenance] = {}
            for field_name, provenance in value.items():
                if type(field_name) is not str or type(provenance) is not AiProvenance:
                    raise TypeError("Entity provenance entry is invalid")
                provenance_state = object.__getattribute__(provenance, "__dict__")
                provenance_keys = {
                    "actor", "model", "model_version", "prompt_template_id",
                    "prompt_args", "cost_usd", "token_in", "token_out", "ts",
                    "trace_id",
                }
                if not _has_exact_string_keys(provenance_state, provenance_keys):
                    raise TypeError("AiProvenance state shape is invalid")
                cloned_provenance = _clone_json_value({
                    key: provenance_state[key] for key in provenance_keys
                })
                restored_provenance[field_name] = AiProvenance(**cloned_provenance)
            copied[key] = restored_provenance
            continue
        copied[key] = _clone_json_value(value)
    return copied


def _copy_entity_state(entity: Entity) -> dict[str, Any]:
    """Copy an Entity's complete state while preserving its validator object."""
    return _copy_entity_state_mapping(entity.__dict__)


def _entity_matches_mutation_record(record: _EntityMutationState) -> bool:
    state = object.__getattribute__(record.entity, "__dict__")
    if set(state) != set(record.state):
        return False
    if record.durable_bytes:
        try:
            if _mutation_fingerprint_bytes(
                record.entity.to_dict(), reason="mutation_policy_entity_state_invalid",
            ) != record.durable_bytes:
                return False
        except Exception:
            return False
    else:
        # Unprotected snapshots deliberately skip the extra durable-envelope
        # serialization at capture time.  A preflight callback still gets the
        # same read-only guarantee when one actually ran: compare the complete
        # owned Entity state directly, normalizing provenance to JSON payloads.
        for key in (
            "_id", "_type", "_name", "_data", "_version", "_deleted",
            "_read_only", "_actor_log", "_created_at", "_updated_at",
        ):
            if not _json_values_exact(state.get(key), record.state.get(key)):
                return False
        try:
            current_provenance = {
                field_name: AiProvenance.to_dict(provenance)
                for field_name, provenance in state.get("_provenance", {}).items()
                if type(field_name) is str and type(provenance) is AiProvenance
            }
            captured_provenance = {
                field_name: AiProvenance.to_dict(provenance)
                for field_name, provenance in record.state.get("_provenance", {}).items()
                if type(field_name) is str and type(provenance) is AiProvenance
            }
            if (
                len(current_provenance) != len(state.get("_provenance", {}))
                or len(captured_provenance)
                != len(record.state.get("_provenance", {}))
                or not _json_values_exact(
                    current_provenance, captured_provenance,
                )
            ):
                return False
        except Exception:
            return False
    for key, before in record.state.items():
        after = state.get(key)
        if key == "_schema":
            if (
                after is not record.schema_authority
                or not _json_values_exact(after, before)
            ):
                return False
        elif key == "_validator":
            if after is not record.validator_authority:
                return False
        elif key == "_document_owner":
            if after is not record.document_owner:
                return False
        elif key == "_document_admission_state":
            if (
                type(after) is not tuple
                or len(after) != 2
                or after[0] is not record.admission_schema_authority
                or not _json_values_exact(after[0], before[0])
                or after[1] is not record.admission_validator_authority
            ):
                return False
        elif key not in {
            "_id", "_type", "_name", "_data", "_version", "_deleted",
            "_read_only", "_provenance", "_actor_log", "_created_at",
            "_updated_at",
        } and after != before:
            return False
    return True


def _document_matches_mutation_snapshot(
    document: "Document",
    snapshot: DocumentMutationSnapshot,
) -> bool:
    if snapshot._owner is not document:
        return False
    state = object.__getattribute__(document, "__dict__")
    if set(state) != set(snapshot._document_state) | {"_collections"}:
        return False
    if (
        state.get("_schema") is not snapshot._schema_authority
        or state.get("_mutation_policies") is not snapshot._mutation_policy_authority
        or state.get("_mutation_policy_registry_authority")
        is not snapshot._mutation_policy_registry_authority
        or state.get("_mutation_policy_registry_epoch")
        is not snapshot._mutation_policy_registry_epoch
        or state.get("_mutation_policy_command_authorities")
        is not snapshot._mutation_policy_command_authority
        or state.get("_history_op_log_authority")
        is not snapshot._history_op_log_authority
        or state.get("_history_controller_authority")
        is not snapshot._history_controller_authority
        or state.get("_history_authority_epoch")
        is not snapshot._history_authority_epoch
    ):
        return False
    for key, before in snapshot._document_state.items():
        after = state.get(key)
        if key in {
            "_mutation_policy_registry_authority",
            "_mutation_policy_registry_epoch",
            "_history_op_log_authority",
            "_history_controller_authority",
            "_history_authority_epoch",
        }:
            if after is not before:
                return False
        elif key == "_mutation_policy_command_authorities":
            if not _command_authority_items_match(after, before):
                return False
        elif key == "_read_only_sources":
            if type(after) is not dict or type(before) is not dict:
                return False
            if tuple(after) != tuple(before):
                return False
            for source_name in before:
                before_spec = before[source_name]
                after_spec = after[source_name]
                if (
                    after_spec.name != before_spec.name
                    or after_spec.entity_type != before_spec.entity_type
                    or after_spec.loader is not before_spec.loader
                    or not _json_values_exact(after_spec.schema, before_spec.schema)
                ):
                    return False
        elif not _json_values_exact(after, before):
            return False
    collections = state.get("_collections")
    if collections is not snapshot._collections:
        return False
    if getattr(collections, "default_factory", None) is not snapshot._collections_default_factory:
        return False
    current_buckets = tuple(collections.items())
    if len(current_buckets) != len(snapshot._buckets):
        return False
    for (entity_type, bucket), record in zip(current_buckets, snapshot._buckets):
        if entity_type != record.entity_type or bucket is not record.bucket:
            return False
        current_members = tuple(bucket.items())
        if len(current_members) != len(record.members):
            return False
        for (current_key, current_entity), (before_key, before_entity) in zip(
            current_members, record.members,
        ):
            if current_key != before_key or current_entity is not before_entity:
                return False
    return all(_entity_matches_mutation_record(record) for record in snapshot._entities)


def _restore_entity_state(entity: Entity, state: dict[str, Any]) -> None:
    """Restore ``entity.__dict__`` in place so external references stay valid."""
    restored = _copy_entity_state_mapping(state)
    entity.__dict__.clear()
    entity.__dict__.update(restored)

class MutationPolicyGraphMixin:
    """Document-side runtime policy validation without command dependencies."""

    def _validate_mutation_policy_commands(self, registry: Any) -> None:
        """Fail activation when a declaration names a command that is absent."""
        has = getattr(registry, "has", None)
        is_authority = getattr(registry, "is_mutation_authority", None)
        seal_registry = getattr(registry, "_seal_mutation_policy_registry", None)
        if (
            not callable(has)
            or not callable(is_authority)
            or not callable(seal_registry)
        ):
            raise TypeError(
                "registry must expose the framework mutation-policy seal protocol"
            )
        if (
            type(self._mutation_policy_sealed) is not bool
            or self._mutation_policy_sealed
            or self._mutation_policy_registry_authority is not None
            or self._mutation_policy_registry_epoch is not None
            or self._mutation_policy_registry_pid is not None
            or type(self._mutation_policy_command_authorities) is not dict
            or self._mutation_policy_command_authorities
        ):
            raise ZeError(
                "mutation_policy_registry_authority_mismatch",
                "runtime mutation policy may only be sealed from pristine state",
                context={"reason": "mutation_policy_reseal_forbidden"},
            )
        all_referenced: set[str] = set()
        for entity_type, policy in self._mutation_policies.items():
            referenced: set[str] = set()
            for command_ids in policy["lifecycle"].values():
                referenced.update(command_ids)
            for command_ids in policy["fields"].values():
                referenced.update(command_ids)
            all_referenced.update(referenced)
            for command_id in sorted(referenced):
                if not has(command_id):
                    raise _mutation_policy_error(
                        "mutation_policy_command_unregistered",
                        entity_type=entity_type,
                        command_id=command_id,
                    )
                if not is_authority(command_id):
                    raise _mutation_policy_error(
                        "mutation_policy_command_not_authority",
                        entity_type=entity_type,
                        command_id=command_id,
                    )
        sealed = seal_registry(tuple(sorted(all_referenced)))
        if (
            type(sealed) is not tuple
            or len(sealed) != 3
            or sealed[0] is None
            or type(sealed[1]) is not int
            or sealed[1] != os.getpid()
            or type(sealed[2]) is not dict
            or tuple(sealed[2]) != tuple(sorted(all_referenced))
            or any(value is None for value in sealed[2].values())
        ):
            raise ZeError(
                "mutation_policy_registry_authority_mismatch",
                "command registry returned an invalid mutation-policy seal",
                context={"reason": "mutation_policy_registry_seal_invalid"},
            )
        # Install only after every declaration and opaque binding validated.
        self._mutation_policy_registry_authority = registry
        self._mutation_policy_registry_epoch = sealed[0]
        self._mutation_policy_registry_pid = sealed[1]
        self._mutation_policy_command_authorities = sealed[2]
        self._mutation_policy_sealed = True

    def _mutation_policy_dispatch_required(self) -> bool:
        """O(1) opt-in check used before any root-dispatch callback."""
        policies = object.__getattribute__(self, "_mutation_policies")
        return type(policies) is not dict or bool(policies)

    def _require_mutation_policy_live_seal(self) -> None:
        """Require the process-local opaque seal for protected runtime work."""
        if not self._mutation_policy_dispatch_required():
            return
        _mutation_policy_seal_state(self, require_current_process=True)
        if not self._mutation_policy_sealed:
            raise ZeError(
                "mutation_policy_registry_authority_mismatch",
                "protected runtime mutation policy is not sealed",
                context={"reason": "mutation_policy_registry_not_sealed"},
            )

    def _install_canonical_history_authority(
        self,
        *,
        op_log: Any,
        controller: Any,
        authority_epoch: Any,
        issuer_pid: int,
    ) -> None:
        """Bind the one real runtime OpLog/controller pair exactly once.

        OpLog and UndoController live in the history leaf.  Document stores only
        opaque identities and validates their tiny private protocol, so this
        closes forged-history replay without reversing the dependency graph.
        """
        if (
            self._history_op_log_authority is not None
            or self._history_controller_authority is not None
            or self._history_authority_epoch is not None
            or self._history_authority_pid is not None
        ):
            raise ZeError(
                "history_authority_mismatch",
                "canonical history authority may only be installed once",
                context={"reason": "history_authority_rebind_forbidden"},
            )
        if (
            op_log is None
            or controller is None
            or authority_epoch is None
            or type(issuer_pid) is not int
            or issuer_pid != os.getpid()
            or getattr(controller, "_op_log", None) is not op_log
            or getattr(controller, "_doc", None) is not self
            or getattr(controller, "_history_authority_epoch", None)
            is not authority_epoch
            or getattr(controller, "_history_authority_pid", None) != issuer_pid
        ):
            raise ZeError(
                "history_authority_mismatch",
                "canonical history authority candidate is invalid",
                context={"reason": "history_authority_candidate_invalid"},
            )
        self._require_mutation_policy_live_seal()
        self._history_op_log_authority = op_log
        self._history_controller_authority = controller
        self._history_authority_epoch = authority_epoch
        self._history_authority_pid = issuer_pid

    def _rollback_canonical_history_authority_install(
        self,
        *,
        op_log: Any,
        controller: Any,
        authority_epoch: Any,
        issuer_pid: int,
    ) -> bool:
        """Clear only a partial, unpublished install of one exact candidate."""

        identities_match = (
            (
                self._history_op_log_authority is None
                or self._history_op_log_authority is op_log
            )
            and (
                self._history_controller_authority is None
                or self._history_controller_authority is controller
            )
            and (
                self._history_authority_epoch is None
                or self._history_authority_epoch is authority_epoch
            )
            and self._history_authority_pid in (None, issuer_pid)
        )
        if not identities_match:
            return False
        self._history_op_log_authority = None
        self._history_controller_authority = None
        self._history_authority_epoch = None
        self._history_authority_pid = None
        return True

    def _require_canonical_history_authority(
        self,
        *,
        op_log: Any,
        controller: Any,
        authority_epoch: Any,
        issuer_pid: int,
    ) -> None:
        """Validate one history segment before it can read or replay history."""
        if not self._mutation_policy_dispatch_required():
            return
        self._require_mutation_policy_live_seal()
        current = _canonical_history_authority_state(
            self, require_current_process=True,
        )
        if current[0] is None:
            raise ZeError(
                "history_authority_mismatch",
                "protected history replay has no canonical runtime authority",
                context={"reason": "history_authority_not_bound"},
            )
        if (
            current[0] is not op_log
            or current[1] is not controller
            or current[2] is not authority_epoch
            or current[3] != issuer_pid
        ):
            raise ZeError(
                "history_authority_mismatch",
                "history replay did not originate from the canonical runtime authority",
                context={"reason": "history_authority_identity_mismatch"},
            )

    def _require_mutation_policy_dispatch_authority(
        self,
        *,
        registry_authority: Any,
        registry_epoch: Any,
        registry_pid: int,
        command_id: str,
        command_attestation: Any,
    ) -> None:
        """Bind one root dispatch to this Document's exact registry seal."""
        if not self._mutation_policy_dispatch_required():
            return
        if (
            self._mutation_policy_sealed is False
            and registry_authority is self._mutation_policy_registry_authority
            and registry_epoch is self._mutation_policy_registry_epoch
            and registry_pid == self._mutation_policy_registry_pid
        ):
            raise ZeError(
                "mutation_policy_authority_mutated",
                "command changed the sealed runtime mutation authority",
                context={"command_id": command_id},
            )
        self._require_mutation_policy_live_seal()
        if type(command_id) is not str or not command_id:
            raise TypeError("command_id must be a non-empty string")
        if (
            registry_authority is not self._mutation_policy_registry_authority
            or registry_epoch is not self._mutation_policy_registry_epoch
            or type(registry_pid) is not int
            or registry_pid != self._mutation_policy_registry_pid
            or registry_pid != os.getpid()
        ):
            raise ZeError(
                "mutation_policy_registry_authority_mismatch",
                "root dispatch used another command registry generation",
                context={
                    "command_id": command_id,
                    "reason": "dispatch_registry_generation_mismatch",
                },
            )
        expected = self._mutation_policy_command_authorities.get(command_id)
        if expected is not None and command_attestation is not expected:
            raise ZeError(
                "mutation_policy_registry_authority_mismatch",
                "root dispatch command binding differs from the sealed authority",
                context={
                    "command_id": command_id,
                    "reason": "dispatch_command_binding_mismatch",
                },
            )

    def _validate_mutation_policy_cardinality(self) -> None:
        """Validate final canonical cardinality for every protected type."""
        for entity_type, policy in self._mutation_policies.items():
            cardinality = policy.get("cardinality")
            if cardinality is None:
                continue
            max_total = cardinality["max_total"]
            total = sum(
                1 for entity in self._collections.get(entity_type, {}).values()
                if not entity.read_only
            )
            if total > max_total:
                raise ZeError(
                    "mutation_cardinality_violation",
                    "canonical entity cardinality exceeds its runtime declaration",
                    context={
                        "reason": "mutation_policy_max_total_exceeded",
                        "entity_type": entity_type,
                        "max_total": max_total,
                        "actual_total": total,
                    },
                )

    @staticmethod
    def _require_mutation_policy_permission(
        policy: dict[str, Any],
        *,
        command_id: str,
        entity_type: str,
        entity_id: str,
        action: str,
        field_name: str = "",
    ) -> None:
        if action == "field":
            allowed = policy["fields"].get(field_name, ())
        else:
            allowed = policy["lifecycle"][action]
        if command_id not in allowed:
            raise _mutation_policy_denied(
                command_id=command_id,
                entity_type=entity_type,
                entity_id=entity_id,
                action=action,
                field_name=field_name,
            )

    @staticmethod
    def _validate_mutation_entity_envelope(
        entity: Entity,
        *,
        command_id: str,
        entity_type: str,
        entity_id: str,
        action: str,
    ) -> dict[str, Any]:
        try:
            state = object.__getattribute__(entity, "__dict__")
            if (
                type(state.get("_id")) is not str
                or not state["_id"]
                or type(state.get("_type")) is not str
                or not state["_type"]
                or type(state.get("_name")) is not str
                or type(state.get("_version")) is not int
                or not (1 <= state["_version"] <= _MAX_SQLITE_INTEGER)
                or type(state.get("_deleted")) is not bool
                or type(state.get("_read_only")) is not bool
                or type(state.get("_created_at")) not in (int, float)
                or type(state.get("_created_at")) is bool
                or type(state.get("_updated_at")) not in (int, float)
                or type(state.get("_updated_at")) is bool
                or not math.isfinite(float(state["_created_at"]))
                or not math.isfinite(float(state["_updated_at"]))
                or state["_created_at"] < 0
                or state["_updated_at"] < state["_created_at"]
                or type(state.get("_data")) is not dict
                or type(state.get("_provenance")) is not dict
                or type(state.get("_actor_log")) is not list
            ):
                raise TypeError("Entity durable scalar envelope is invalid")
            actor_log = state["_actor_log"]
            previous_timestamp = float(state["_created_at"])
            for index, entry in enumerate(actor_log, start=2):
                if (
                    type(entry) is not dict
                    or set(entry) != {"version", "actor", "ts", "fields"}
                    or type(entry.get("version")) is not int
                    or not (1 <= entry["version"] <= _MAX_SQLITE_INTEGER)
                    or type(entry.get("ts")) not in (int, float)
                    or type(entry.get("ts")) is bool
                    or not math.isfinite(float(entry["ts"]))
                    or entry["ts"] < 0
                    or type(entry.get("actor")) is not dict
                    or set(entry["actor"]) != {"user_id", "device_id", "source"}
                    or type(entry.get("fields")) is not list
                    or any(
                        type(field_name) is not str or not field_name
                        for field_name in entry["fields"]
                    )
                    or entry["fields"] != sorted(entry["fields"])
                    or len(entry["fields"]) != len(set(entry["fields"]))
                    or entry["version"] != index
                    or float(entry["ts"]) < previous_timestamp
                ):
                    raise TypeError("Entity actor log envelope is invalid")
                Actor(**entry["actor"])
                previous_timestamp = float(entry["ts"])
            if actor_log:
                if (
                    len(actor_log) != state["_version"] - 1
                    or not _json_values_exact(
                        state["_updated_at"], actor_log[-1]["ts"],
                    )
                ):
                    raise TypeError("Entity actor log continuity is invalid")
            elif (
                state["_version"] != 1
                or not _json_values_exact(
                    state["_updated_at"], state["_created_at"],
                )
            ):
                raise TypeError("Entity empty actor log continuity is invalid")
            payload = entity.to_dict()
            _mutation_fingerprint_bytes(
                payload, reason="mutation_policy_entity_state_invalid",
            )
        except Exception as exc:
            raise _mutation_policy_denied(
                command_id=command_id,
                entity_type=entity_type,
                entity_id=entity_id,
                action=action,
                reason="mutation_policy_final_envelope_invalid",
            ) from exc
        return payload

    def _authorize_created_entity(
        self,
        entity: Entity,
        policy: dict[str, Any],
        *,
        command_id: str,
    ) -> None:
        entity_type = entity.type
        entity_id = entity.id
        self._validate_mutation_entity_envelope(
            entity,
            command_id=command_id,
            entity_type=entity_type,
            entity_id=entity_id,
            action="create",
        )
        self._require_mutation_policy_permission(
            policy,
            command_id=command_id,
            entity_type=entity_type,
            entity_id=entity_id,
            action="create",
        )
        state = object.__getattribute__(entity, "__dict__")
        expected_state_keys = {
            "_id", "_type", "_name", "_data", "_version", "_deleted",
            "_read_only", "_schema", "_validator", "_provenance",
            "_document_owner", "_document_admission_state", "_actor_log",
            "_created_at", "_updated_at",
        }
        type_schema = self._schema.get(entity_type, {})
        admission = state.get("_document_admission_state")
        validator = state.get("_validator")
        authority_valid = (
            set(state) == expected_state_keys
            and state.get("_id") == entity_id
            and state.get("_type") == entity_type
            and state.get("_document_owner") is self
            and type(admission) is tuple
            and len(admission) == 2
            and type(admission[0]) is dict
            and callable(admission[1])
            and type(state.get("_schema")) is dict
            and _json_values_exact(state.get("_schema"), type_schema)
            and (
                is_framework_validator(validator, type_schema)
                if type_schema else validator is _no_op_validator
            )
        )
        if (
            not authority_valid
            or entity.read_only
            or entity.deleted
            or entity.version != 1
            or state.get("_actor_log") != []
            or state.get("_provenance") != {}
            or type(state.get("_created_at")) not in (int, float)
            or type(state.get("_created_at")) is bool
            or not math.isfinite(float(state.get("_created_at", math.nan)))
            or not _json_values_exact(
                state.get("_created_at"), state.get("_updated_at"),
            )
        ):
            raise _mutation_policy_denied(
                command_id=command_id,
                entity_type=entity_type,
                entity_id=entity_id,
                action="create",
                reason="mutation_policy_create_envelope_invalid",
            )
        try:
            validator(
                _clone_json_value(state["_data"]),
                _clone_json_value(type_schema),
                f"${entity_type}",
            )
        except Exception as exc:
            raise _mutation_policy_denied(
                command_id=command_id,
                entity_type=entity_type,
                entity_id=entity_id,
                action="create",
                reason="mutation_policy_final_schema_invalid",
            ) from exc
        if entity.name:
            self._require_mutation_policy_permission(
                policy,
                command_id=command_id,
                entity_type=entity_type,
                entity_id=entity_id,
                action="rename",
            )
        for field_name in sorted(state["_data"]):
            self._require_mutation_policy_permission(
                policy,
                command_id=command_id,
                entity_type=entity_type,
                entity_id=entity_id,
                action="field",
                field_name=field_name,
            )

    def _authorize_existing_entity(
        self,
        record: _EntityMutationState,
        entity: Entity,
        policy: dict[str, Any],
        *,
        command_id: str,
        expected_actor: dict[str, Any],
        expected_source: str,
    ) -> set[tuple[str, str, str, str]]:
        before = record.state
        after = object.__getattribute__(entity, "__dict__")
        entity_type = before["_type"]
        entity_id = before["_id"]
        if _entity_matches_mutation_record(record):
            return set()
        if before["_read_only"]:
            raise _mutation_policy_denied(
                command_id=command_id,
                entity_type=entity_type,
                entity_id=entity_id,
                action="field",
                reason="mutation_policy_read_only_entity_mutated",
            )
        self._validate_mutation_entity_envelope(
            entity,
            command_id=command_id,
            entity_type=entity_type,
            entity_id=entity_id,
            action="field",
        )
        if (
            set(after) != set(before)
            or not _json_values_exact(after.get("_id"), before.get("_id"))
            or not _json_values_exact(after.get("_type"), before.get("_type"))
            or not _json_values_exact(
                after.get("_read_only"), before.get("_read_only"),
            )
            or not _json_values_exact(
                after.get("_created_at"), before.get("_created_at"),
            )
            or after.get("_schema") is not record.schema_authority
            or not _json_values_exact(
                after.get("_schema"), before.get("_schema"),
            )
            or after.get("_validator") is not record.validator_authority
            or after.get("_document_owner") is not record.document_owner
            or type(after.get("_document_admission_state")) is not tuple
            or len(after.get("_document_admission_state")) != 2
            or after["_document_admission_state"][0]
            is not record.admission_schema_authority
            or not _json_values_exact(
                after["_document_admission_state"][0],
                before["_document_admission_state"][0],
            )
            or after["_document_admission_state"][1]
            is not record.admission_validator_authority
        ):
            raise _mutation_policy_denied(
                command_id=command_id,
                entity_type=entity_type,
                entity_id=entity_id,
                action="field",
                reason="mutation_policy_entity_authority_mutated",
            )

        before_log = before["_actor_log"]
        after_log = after["_actor_log"]
        if (
            type(after_log) is not list
            or len(after_log) < len(before_log)
            or any(
                not _json_values_exact(after_entry, before_entry)
                for after_entry, before_entry in zip(
                    after_log[:len(before_log)], before_log,
                )
            )
        ):
            raise _mutation_policy_denied(
                command_id=command_id,
                entity_type=entity_type,
                entity_id=entity_id,
                action="field",
                reason="mutation_policy_actor_log_invalid",
            )
        appended = after_log[len(before_log):]
        if (
            type(after["_version"]) is not int
            or type(before["_version"]) is not int
            or after["_version"] - before["_version"] != len(appended)
        ):
            raise _mutation_policy_denied(
                command_id=command_id,
                entity_type=entity_type,
                entity_id=entity_id,
                action="field",
                reason="mutation_policy_version_delta_invalid",
            )

        actor_fields: set[str] = set()
        actor_field_paths: set[str] = set()
        lifecycle_actions: list[str] = []
        expected_version = before["_version"]
        expected_deleted = before["_deleted"]
        for entry in appended:
            expected_version += 1
            if (
                type(entry) is not dict
                or set(entry) != {"version", "actor", "ts", "fields"}
                or type(entry.get("version")) is not int
                or entry.get("version") != expected_version
                or type(entry.get("fields")) is not list
                or type(entry.get("actor")) is not dict
                or set(entry.get("actor"))
                != {"user_id", "device_id", "source"}
            ):
                raise _mutation_policy_denied(
                    command_id=command_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    action="field",
                    reason="mutation_policy_actor_log_invalid",
                )
            try:
                Actor(**entry["actor"])
            except Exception:
                raise _mutation_policy_denied(
                    command_id=command_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    action="field",
                    reason="mutation_policy_actor_log_invalid",
                ) from None
            if (
                not _json_values_exact(entry["actor"], expected_actor)
                or entry["actor"].get("source") != expected_source
            ):
                raise _mutation_policy_denied(
                    command_id=command_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    action="field",
                    reason="mutation_policy_actor_identity_mismatch",
                )
            fields = entry["fields"]
            if (
                any(type(field_name) is not str or not field_name for field_name in fields)
                or fields != sorted(fields)
                or len(fields) != len(set(fields))
            ):
                raise _mutation_policy_denied(
                    command_id=command_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    action="field",
                    reason="mutation_policy_actor_log_invalid",
                )
            if fields == ["__deleted__"]:
                lifecycle_actions.append("soft_delete")
                expected_deleted = True
                continue
            if fields == ["__restored__"]:
                lifecycle_actions.append("restore")
                expected_deleted = False
                continue
            for raw_field in fields:
                if type(raw_field) is not str or not raw_field:
                    raise _mutation_policy_denied(
                        command_id=command_id,
                        entity_type=entity_type,
                        entity_id=entity_id,
                        action="field",
                        reason="mutation_policy_actor_log_invalid",
                    )
                actor_field_paths.add(raw_field)
                actor_fields.add(_mutation_policy_field_root(raw_field))
        if appended:
            timestamps = [entry.get("ts") for entry in appended]
            if any(
                type(timestamp) not in (int, float)
                or type(timestamp) is bool
                or not math.isfinite(float(timestamp))
                for timestamp in timestamps
            ) or (
                float(timestamps[0]) < float(before["_updated_at"])
                or any(
                    float(current) < float(previous)
                    for previous, current in zip(timestamps, timestamps[1:])
                )
            ) or not _json_values_exact(after["_updated_at"], timestamps[-1]):
                raise _mutation_policy_denied(
                    command_id=command_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    action="field",
                    reason="mutation_policy_updated_at_invalid",
                )
        elif not _json_values_exact(after["_updated_at"], before["_updated_at"]):
            raise _mutation_policy_denied(
                command_id=command_id,
                entity_type=entity_type,
                entity_id=entity_id,
                action="field",
                reason="mutation_policy_updated_at_invalid",
            )
        if type(after["_deleted"]) is not bool or expected_deleted is not after["_deleted"]:
            raise _mutation_policy_denied(
                command_id=command_id,
                entity_type=entity_type,
                entity_id=entity_id,
                action="field",
                reason="mutation_policy_deleted_delta_invalid",
            )

        missing = object()
        before_data = before["_data"]
        after_data = after["_data"]
        changed_data_fields = {
            key for key in set(before_data) | set(after_data)
            if (
                before_data.get(key, missing) is missing
                or after_data.get(key, missing) is missing
                or not _json_values_exact(before_data[key], after_data[key])
            )
        }
        before_provenance = before["_provenance"]
        after_provenance = after["_provenance"]
        changed_provenance_paths = {
            key for key in set(before_provenance) | set(after_provenance)
            if (
                before_provenance.get(key, missing) is missing
                or after_provenance.get(key, missing) is missing
                or not _json_values_exact(
                    before_provenance[key], after_provenance[key],
                )
            )
        }
        if (
            not changed_data_fields.issubset(actor_fields)
            or not changed_provenance_paths.issubset(actor_field_paths)
        ):
            raise _mutation_policy_denied(
                command_id=command_id,
                entity_type=entity_type,
                entity_id=entity_id,
                action="field",
                reason="mutation_policy_unlogged_delta",
            )
        changed_fields = changed_data_fields | {
            _mutation_policy_field_root(field_name)
            for field_name in changed_provenance_paths
        }
        changed_fields.update(actor_fields)
        for field_name in sorted(changed_fields):
            self._require_mutation_policy_permission(
                policy,
                command_id=command_id,
                entity_type=entity_type,
                entity_id=entity_id,
                action="field",
                field_name=field_name,
            )
        for action in lifecycle_actions:
            self._require_mutation_policy_permission(
                policy,
                command_id=command_id,
                entity_type=entity_type,
                entity_id=entity_id,
                action=action,
            )
        renamed = not _json_values_exact(before["_name"], after["_name"])
        if renamed:
            self._require_mutation_policy_permission(
                policy,
                command_id=command_id,
                entity_type=entity_type,
                entity_id=entity_id,
                action="rename",
            )
        if appended and not (changed_fields or lifecycle_actions or renamed):
            raise _mutation_policy_denied(
                command_id=command_id,
                entity_type=entity_type,
                entity_id=entity_id,
                action="field",
                reason="mutation_policy_metadata_only_delta",
            )
        if not appended and any((changed_fields, lifecycle_actions, renamed)):
            raise _mutation_policy_denied(
                command_id=command_id,
                entity_type=entity_type,
                entity_id=entity_id,
                action="field",
                reason="mutation_policy_unlogged_delta",
            )
        type_schema = self._schema.get(entity_type, {})
        validator = after.get("_validator")
        if not (
            is_framework_validator(validator, type_schema)
            if type_schema else validator is _no_op_validator
        ):
            raise _mutation_policy_denied(
                command_id=command_id,
                entity_type=entity_type,
                entity_id=entity_id,
                action="field",
                reason="mutation_policy_entity_authority_mutated",
            )
        try:
            validator(
                _clone_json_value(after["_data"]),
                _clone_json_value(type_schema),
                f"${entity_type}",
            )
        except Exception as exc:
            raise _mutation_policy_denied(
                command_id=command_id,
                entity_type=entity_type,
                entity_id=entity_id,
                action="field",
                reason="mutation_policy_final_schema_invalid",
            ) from exc
        touches = {
            (entity_type, entity_id, "field", field_name)
            for field_name in actor_field_paths
        }
        touches.update(
            (entity_type, entity_id, action, "")
            for action in lifecycle_actions
        )
        if renamed:
            touches.add((entity_type, entity_id, "name", ""))
        return touches

    def _authorize_command_mutation(
        self,
        snapshot: DocumentMutationSnapshot,
        *,
        command_id: str,
        actor_payload: dict[str, Any],
        source: str,
    ) -> frozenset[tuple[str, str, str, str]]:
        """Authorize the actual graph delta captured by one command session."""
        if type(command_id) is not str or not command_id:
            raise TypeError("command_id must be a non-empty string")
        if type(actor_payload) is not dict or type(source) is not str or not source:
            raise TypeError("actor/source must be exact command authority values")
        try:
            expected_actor = Actor(**actor_payload).to_dict()
        except Exception:
            raise TypeError("actor_payload must be one exact Actor payload") from None
        if not _json_values_exact(expected_actor, actor_payload):
            raise TypeError("actor_payload must be one exact Actor payload")
        if snapshot._owner is not self:
            raise ZeError(
                "mutation_snapshot_owner_mismatch",
                "mutation policy snapshot belongs to another Document",
            )
        self._require_mutation_policy_live_seal()
        if (
            self._schema is not snapshot._schema_authority
            or self._mutation_policies is not snapshot._mutation_policy_authority
            or self._mutation_policy_registry_authority
            is not snapshot._mutation_policy_registry_authority
            or self._mutation_policy_registry_epoch
            is not snapshot._mutation_policy_registry_epoch
            or self._mutation_policy_command_authorities
            is not snapshot._mutation_policy_command_authority
            or self._history_op_log_authority
            is not snapshot._history_op_log_authority
            or self._history_controller_authority
            is not snapshot._history_controller_authority
            or self._history_authority_epoch
            is not snapshot._history_authority_epoch
            or self._history_authority_pid
            != snapshot._document_state.get("_history_authority_pid")
            or not _command_authority_items_match(
                self._mutation_policy_command_authorities,
                snapshot._document_state.get(
                    "_mutation_policy_command_authorities",
                ),
            )
            or self._mutation_policy_registry_pid
            != snapshot._document_state.get("_mutation_policy_registry_pid")
            or type(self._mutation_policy_sealed) is not bool
            or type(snapshot._document_state.get("_mutation_policy_sealed")) is not bool
            or self._mutation_policy_sealed
            is not snapshot._document_state.get("_mutation_policy_sealed")
            or not _json_values_exact(
                self._schema, snapshot._document_state.get("_schema"),
            )
            or not _json_values_exact(
                self._mutation_policies,
                snapshot._document_state.get("_mutation_policies"),
            )
        ):
            raise ZeError(
                "mutation_policy_authority_mutated",
                "command replaced runtime declaration authority",
                context={"command_id": command_id},
            )
        if not self._mutation_policies:
            return frozenset()

        actual_touches: set[tuple[str, str, str, str]] = set()

        if (
            self._collections is not snapshot._collections
            or getattr(self._collections, "default_factory", None)
            is not snapshot._collections_default_factory
        ):
            raise _mutation_policy_denied(
                command_id=command_id,
                entity_type="*",
                entity_id="",
                action="field",
                reason="mutation_policy_collection_authority_mutated",
            )
        for bucket_type, bucket in self._collections.items():
            if type(bucket_type) is not str or type(bucket) is not dict:
                raise _mutation_policy_denied(
                    command_id=command_id,
                    entity_type=bucket_type if type(bucket_type) is str else "*",
                    entity_id="",
                    action="field",
                    reason="mutation_policy_collection_identity_invalid",
                )
            for member_id, member in bucket.items():
                if (
                    type(member_id) is not str
                    or type(member) is not Entity
                    or member.id != member_id
                    or member.type != bucket_type
                    or object.__getattribute__(member, "_document_owner") is not self
                ):
                    raise _mutation_policy_denied(
                        command_id=command_id,
                        entity_type=bucket_type,
                        entity_id=member_id if type(member_id) is str else "",
                        action="field",
                        reason="mutation_policy_collection_identity_invalid",
                    )
        snapshot_buckets = {
            bucket.entity_type: bucket for bucket in snapshot._buckets
        }
        for entity_type in self._mutation_policies:
            before_bucket = snapshot_buckets.get(entity_type)
            after_present = entity_type in self._collections
            after_bucket = self._collections[entity_type] if after_present else None
            if after_present and type(after_bucket) is not dict:
                raise _mutation_policy_denied(
                    command_id=command_id,
                    entity_type=entity_type,
                    entity_id="",
                    action="field",
                    reason="mutation_policy_collection_authority_mutated",
                )
            if before_bucket is not None and after_bucket is not before_bucket.bucket:
                raise _mutation_policy_denied(
                    command_id=command_id,
                    entity_type=entity_type,
                    entity_id="",
                    action="field",
                    reason="mutation_policy_collection_authority_mutated",
                )
            if before_bucket is None:
                if after_present and not after_bucket:
                    raise _mutation_policy_denied(
                        command_id=command_id,
                        entity_type=entity_type,
                        entity_id="",
                        action="field",
                        reason="mutation_policy_empty_bucket_created",
                    )
                continue
            before_members = before_bucket.members
            after_members = tuple(after_bucket.items())
            if any(
                type(key) is not str or type(entity) is not Entity
                for key, entity in after_members
            ):
                raise _mutation_policy_denied(
                    command_id=command_id,
                    entity_type=entity_type,
                    entity_id="",
                    action="field",
                    reason="mutation_policy_collection_identity_invalid",
                )
            after_by_key = dict(after_members)
            before_by_key = dict(before_members)
            surviving_before = tuple(
                key for key, entity in before_members
                if after_by_key.get(key) is entity
            )
            surviving_after = tuple(
                key for key, entity in after_members
                if before_by_key.get(key) is entity
            )
            if surviving_before != surviving_after:
                raise _mutation_policy_denied(
                    command_id=command_id,
                    entity_type=entity_type,
                    entity_id="",
                    action="field",
                    reason="mutation_policy_collection_order_mutated",
                )
            old_canonical_ids = {
                id(entity) for _, entity in before_members if not entity.read_only
            }
            saw_overlay = False
            saw_new_canonical = False
            for _, entity in after_members:
                if entity.read_only:
                    saw_overlay = True
                    continue
                if saw_overlay:
                    raise _mutation_policy_denied(
                        command_id=command_id,
                        entity_type=entity_type,
                        entity_id=entity.id,
                        action="create",
                        reason="mutation_policy_collection_order_mutated",
                    )
                if id(entity) not in old_canonical_ids:
                    saw_new_canonical = True
                elif saw_new_canonical:
                    raise _mutation_policy_denied(
                        command_id=command_id,
                        entity_type=entity_type,
                        entity_id=entity.id,
                        action="create",
                        reason="mutation_policy_collection_order_mutated",
                    )

        before: dict[tuple[str, str], _EntityMutationState] = {}
        records_by_identity = {
            id(record.entity): record for record in snapshot._entities
        }
        for bucket in snapshot._buckets:
            for key, entity in bucket.members:
                record = records_by_identity.get(id(entity))
                if record is None or record.entity is not entity:
                    raise ZeError(
                        "mutation_snapshot_invalid",
                        "mutation snapshot entity inventory is incomplete",
                    )
                before[(bucket.entity_type, key)] = record
        after = {
            (entity_type, key): entity
            for entity_type, bucket in self._collections.items()
            for key, entity in bucket.items()
        }
        protected_keys = {
            key for key in set(before) | set(after)
            if key[0] in self._mutation_policies
        }
        for entity_type, entity_id in sorted(protected_keys):
            policy = self._mutation_policies[entity_type]
            before_record = before.get((entity_type, entity_id))
            after_entity = after.get((entity_type, entity_id))
            if after_entity is not None and (
                type(after_entity) is not Entity
                or after_entity.type != entity_type
                or after_entity.id != entity_id
            ):
                raise _mutation_policy_denied(
                    command_id=command_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    action="field",
                    reason="mutation_policy_collection_identity_invalid",
                )
            if before_record is None:
                if after_entity is None:
                    raise ZeError(
                        "mutation_snapshot_invalid",
                        "protected delta inventory lost both entity endpoints",
                    )
                self._authorize_created_entity(
                    after_entity, policy, command_id=command_id,
                )
                actual_touches.add((entity_type, entity_id, "create", ""))
                continue
            if after_entity is None or after_entity is not before_record.entity:
                current_state = object.__getattribute__(before_record.entity, "__dict__")
                self._validate_mutation_entity_envelope(
                    before_record.entity,
                    command_id=command_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    action="hard_remove",
                )
                admission = before_record.state["_document_admission_state"]
                if set(current_state) != set(before_record.state):
                    raise _mutation_policy_denied(
                        command_id=command_id,
                        entity_type=entity_type,
                        entity_id=entity_id,
                        action="hard_remove",
                        reason="mutation_policy_removed_entity_mutated",
                    )
                for key in (
                    "_id", "_type", "_name", "_data", "_version", "_deleted",
                    "_read_only", "_provenance", "_actor_log", "_created_at", "_updated_at",
                ):
                    current_value = current_state.get(key)
                    before_value = before_record.state.get(key)
                    if key == "_provenance":
                        current_value = {
                            field_name: value.to_dict()
                            for field_name, value in current_value.items()
                        }
                        before_value = {
                            field_name: value.to_dict()
                            for field_name, value in before_value.items()
                        }
                    if not _json_values_exact(current_value, before_value):
                        raise _mutation_policy_denied(
                            command_id=command_id,
                            entity_type=entity_type,
                            entity_id=entity_id,
                            action="hard_remove",
                            reason="mutation_policy_removed_entity_mutated",
                        )
                if before_record.state["_read_only"]:
                    raise _mutation_policy_denied(
                        command_id=command_id,
                        entity_type=entity_type,
                        entity_id=entity_id,
                        action="hard_remove",
                        reason="mutation_policy_read_only_entity_mutated",
                    )
                if (
                    current_state.get("_document_owner") is not None
                    or current_state.get("_document_admission_state") is not None
                    or current_state.get("_schema")
                    is not before_record.admission_schema_authority
                    or not _json_values_exact(
                        current_state.get("_schema"), admission[0],
                    )
                    or current_state.get("_validator")
                    is not before_record.admission_validator_authority
                ):
                    raise _mutation_policy_denied(
                        command_id=command_id,
                        entity_type=entity_type,
                        entity_id=entity_id,
                        action="hard_remove",
                        reason="mutation_policy_hard_remove_authority_invalid",
                    )
                self._require_mutation_policy_permission(
                    policy,
                    command_id=command_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    action="hard_remove",
                )
                actual_touches.add((entity_type, entity_id, "delete", ""))
                if after_entity is not None:
                    self._authorize_created_entity(
                        after_entity, policy, command_id=command_id,
                    )
                    actual_touches.add((entity_type, entity_id, "create", ""))
                continue
            actual_touches.update(self._authorize_existing_entity(
                before_record,
                after_entity,
                policy,
                command_id=command_id,
                expected_actor=expected_actor,
                expected_source=source,
            ))
        for admitted in snapshot._admitted_entities:
            entity = admitted.entity
            if self._collections.get(entity.type, {}).get(entity.id) is entity:
                continue
            state = object.__getattribute__(entity, "__dict__")
            if not (
                state.get("_document_owner") is None
                and state.get("_document_admission_state") is None
                and state.get("_schema") is admitted.schema_authority
                and _mutation_fingerprint_bytes(
                    state.get("_schema"),
                    reason="mutation_entity_admission_schema_invalid",
                ) == admitted.schema_bytes
                and state.get("_validator") is admitted.validator_authority
            ):
                raise _mutation_policy_denied(
                    command_id=command_id,
                    entity_type=entity.type,
                    entity_id=entity.id,
                    action="create",
                    reason="mutation_policy_admitted_entity_orphaned",
                )
        self._validate_mutation_policy_cardinality()
        if actual_touches and expected_actor["source"] != source:
            raise ZeError(
                "mutation_actor_source_mismatch",
                "dispatch source must equal the exact Actor source",
                context={"command_id": command_id, "source": source},
            )
        return frozenset(actual_touches)
