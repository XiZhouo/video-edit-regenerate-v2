"""Pure, cold-safe reduction of Pipeline authority from canonical history.

The reducer intentionally accepts a complete, detached OpLog row snapshot and
its matching head.  It does not import or consult PipelineSupervisor, a run
manifest, resolver metadata, marker files, Document, or any runtime grant
registry.  A caller that wants execution authority must separately prove that
these rows came from a fresh OpLog scan and consume a sealed, process-local
permit in a later protocol layer.
"""

from __future__ import annotations

import hashlib
import hmac
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_CORE_DIR = Path(__file__).resolve().parent.parent
_PERSISTENCE_DIR = _CORE_DIR / "02-data-flow" / "07-persistence"
if str(_PERSISTENCE_DIR) not in sys.path:
    sys.path.insert(0, str(_PERSISTENCE_DIR))

from operation_record_codec import (  # noqa: E402
    OperationRecordCodecError,
    ZERO_OPERATION_HASH,
    freeze_operation_body,
    freeze_operation_record,
    materialize_operation_body,
)
from pipeline_authority_codec import (  # noqa: E402
    FrozenPipelineControl,
    FrozenPipelineStart,
    PipelineAuthorityCodecError,
    materialize_pipeline_descriptor,
    parse_pipeline_control,
    parse_pipeline_start,
)


PIPELINE_AUTHORITY_REDUCTION_PROTOCOL = "ze.pipeline-authority-reduction.v1"

_START_SCHEMA = "ze.pipeline-start.v2"
_CONTROL_SCHEMA = "ze.pipeline-control.v2"
# Batch 1 accepted these names as a resource-bounded carrier before strict
# semantics existed.  Their historical dictionaries can never be upgraded by
# reinterpretation into authority; only a newly committed v2 descriptor counts.
_LEGACY_CARRIER_SCHEMAS = frozenset(
    ("ze.pipeline-start.v1", "ze.pipeline-control.v1")
)
_ENTRY_KEYS = frozenset(("op", "prev_hash", "self_hash"))
_LOWER_HEX = frozenset("0123456789abcdef")
_OPERATION_SOURCES = frozenset(("user", "ai", "sync", "system"))

_ACTION_COMMAND_IDS = {
    "pause": "pipeline.pause",
    "interrupt": "pipeline.interrupt",
    "cancel": "pipeline.cancel",
    "discard": "pipeline.discard",
    "resume": "pipeline.resume",
    "retry_failed": "pipeline.retry_failed",
    "rerun_step": "pipeline.rerun_step",
    "run_from_step": "pipeline.run_from_step",
    "set_breakpoint": "pipeline.set_breakpoint",
}
_POSITIVE_ACTIONS = frozenset(
    ("resume", "retry_failed", "rerun_step", "run_from_step")
)
_TERMINAL_ACTIONS = frozenset(("cancel", "discard"))


class PipelineAuthorityReductionError(RuntimeError):
    """Stable, payload-free failure while reducing durable authority."""

    __slots__ = ("code", "reason", "context")

    def __init__(self, code: str, reason: str, message: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        if type(reason) is not str or not reason:
            raise TypeError("reason must be a non-empty exact string")
        if type(message) is not str or not message:
            raise TypeError("message must be a non-empty exact string")
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.context = {"reason": reason}


def _fail(code: str, reason: str, message: str) -> None:
    raise PipelineAuthorityReductionError(code, reason, message)


class _DetachedDictView:
    """Hide an object's live attribute mapping behind a detached snapshot.

    The managed core still supports Python versions whose ``dataclass`` does
    not accept ``slots=True``.  These reducer values therefore retain their
    ordinary instance dictionary for compatibility, but callers can never use
    ``value.__dict__`` (or ``vars(value)``) as a back door around ``frozen``.
    Later authority issuers must consume a private sealed reduction, not these
    public projection objects.
    """

    __slots__ = ()

    def __getattribute__(self, name: str) -> Any:
        if type(name) is str and name == "__dict__":
            live = object.__getattribute__(self, "__dict__")
            return live.copy()
        return object.__getattribute__(self, name)


@dataclass(frozen=True)
class PipelinePlaceholderBindingFact(_DetachedDictView):
    item_id: str
    entity_type: str
    entity_id: str
    operation_id: str


@dataclass(frozen=True)
class PipelineWorkItemBindingFact(_DetachedDictView):
    """Exact durable WorkList item-to-entity binding.

    A set of item ids plus a separate set of targets cannot prove that item A
    belongs to entity A.  Placeholder bindings are also intentionally partial,
    so later delegated checks consume this complete bounded mapping instead of
    joining independent sets.
    """

    item_id: str
    entity_type: str
    entity_id: str


@dataclass(frozen=True)
class PipelineDelegatedTargetFact(_DetachedDictView):
    entity_type: str
    entity_id: str
    fields: tuple[str, ...]
    lifecycle: tuple[str, ...]


@dataclass(frozen=True)
class PipelineDelegatedCreationFact(_DetachedDictView):
    entity_type: str
    fields: tuple[str, ...]
    max_per_operation: int
    max_per_run: int


@dataclass(frozen=True)
class ReducedPipelineAuthority(_DetachedDictView):
    """One run's authority facts derived only from a verified history head.

    ``reconciliation_pending`` is deliberately sticky across later restrictive
    controls.  A positive control can be activated only by a later same-process
    sealed after-commit permit; a cold caller never turns this result directly
    into execution authority.
    """

    protocol: str
    document_id: str
    run_id: str
    pipeline_id: str
    start_command_id: str
    start_operation_id: str
    start_operation_hash: str
    start_descriptor_digest: str
    initial_operation_ids: tuple[str, ...] = field(repr=False)
    root_actor: tuple[str, str, str]
    root_source: str
    root_command_digest: str
    document_policy_digest: str
    worker_actor: tuple[str, str, str]
    worker_source: str
    work_item_ids: tuple[str, ...] = field(repr=False)
    work_item_bindings: tuple[PipelineWorkItemBindingFact, ...] = field(
        repr=False
    )
    allowed_stage_ids: tuple[str, ...] = field(repr=False)
    placeholder_bindings: tuple[PipelinePlaceholderBindingFact, ...] = field(
        repr=False
    )
    delegated_targets: tuple[PipelineDelegatedTargetFact, ...] = field(repr=False)
    delegated_creation_scopes: tuple[PipelineDelegatedCreationFact, ...] = field(
        repr=False
    )
    active_breakpoint_stage_ids: tuple[str, ...] = field(repr=False)
    generation: int
    state: str
    reconciliation_pending: bool
    terminal_action: str | None
    last_control_operation_id: str | None
    last_control_hash: str | None
    control_operation_ids: tuple[str, ...] = field(repr=False)
    history_head_count: int
    history_head_hash: str

    @property
    def terminal(self) -> bool:
        return self.state == "terminal"

    @property
    def reconcile_required(self) -> bool:
        return self.reconciliation_pending


@dataclass(frozen=True)
class _OwnedOperationRow(_DetachedDictView):
    index: int
    operation_id: str | None
    command_id: str | None
    document_id: str | None
    source: str | None
    actor_identity: tuple[str, str, str] | None
    pipeline_schema: str | None
    created_entity_refs: tuple[tuple[str, str], ...]
    entity_add_fact: tuple[str, str] | None
    self_hash: str


@dataclass(frozen=True)
class _StartRow(_DetachedDictView):
    row: _OwnedOperationRow
    descriptor: FrozenPipelineStart


@dataclass(frozen=True)
class _ControlRow(_DetachedDictView):
    row: _OwnedOperationRow
    descriptor: FrozenPipelineControl


@dataclass(frozen=True)
class _StartAttestationFacts(_DetachedDictView):
    root_actor: tuple[str, str, str]
    root_source: str
    root_command_digest: str
    document_policy_digest: str
    worker_actor: tuple[str, str, str]
    worker_source: str
    work_item_ids: tuple[str, ...]
    work_item_bindings: tuple[PipelineWorkItemBindingFact, ...]
    allowed_stage_ids: tuple[str, ...]
    placeholder_bindings: tuple[PipelinePlaceholderBindingFact, ...]
    delegated_targets: tuple[PipelineDelegatedTargetFact, ...]
    delegated_creation_scopes: tuple[PipelineDelegatedCreationFact, ...]


def _exact_nonempty_text(value: Any) -> bool:
    return type(value) is str and bool(value)


def _hash_text(value: Any) -> str | None:
    if (
        type(value) is str
        and len(value) == 64
        and all(character in _LOWER_HEX for character in value)
    ):
        return value
    return None


def _digest_hex(value: Any) -> str:
    if type(value) is bytes and len(value) == 32:
        return value.hex()
    if _hash_text(value) is not None:
        return value
    _fail(
        "pipeline_start_digest_mismatch",
        "start_descriptor_digest_invalid",
        "Pipeline Start descriptor digest is invalid",
    )
    raise AssertionError("unreachable")


def _strict_operation(
    body: dict[str, Any],
    *,
    code: str,
    reason: str,
    message: str,
) -> None:
    try:
        freeze_operation_record(body)
    except OperationRecordCodecError:
        _fail(code, reason, message)


def _actor_payload_identity(actor: Any) -> tuple[str, str, str] | None:
    if type(actor) is not dict or frozenset(actor) != frozenset(
        ("user_id", "device_id", "source")
    ):
        return None
    values = (actor["user_id"], actor["device_id"], actor["source"])
    if any(type(value) is not str for value in values):
        return None
    if values[2] not in _OPERATION_SOURCES:
        return None
    return values


def _actor_identity(body: dict[str, Any]) -> tuple[str, str, str] | None:
    return _actor_payload_identity(body.get("actor"))


def _json_exact_equal(left: Any, right: Any) -> bool:
    """Compare two already validated JSON graphs without Python coercion.

    Python deliberately considers ``False == 0`` and ``1 == 1.0``.  Durable
    reverse snapshots are identity evidence, so those values must remain
    different even though ordinary application equality considers them equal.
    """

    if type(left) is not type(right):
        return False
    if type(left) is dict:
        if frozenset(left) != frozenset(right):
            return False
        return all(_json_exact_equal(left[key], right[key]) for key in left)
    if type(left) is list:
        return len(left) == len(right) and all(
            _json_exact_equal(left_value, right_value)
            for left_value, right_value in zip(left, right)
        )
    return bool(left == right)


def _shallow_exact_string_key_snapshot(
    value: Any,
    *,
    invalid_reason: str,
    invalid_message: str,
) -> dict[str, Any] | None:
    """Read one exact dict without touching hooks on untrusted key objects.

    ``dict.get`` is not inert when an exact dict contains a hostile non-string
    key whose cached hash collides with the requested string: Python may invoke
    that key's equality hook.  Iteration plus an exact-string check avoids all
    caller comparison hooks and gives the rest of the reducer one shallow,
    stable member snapshot.  Nested authority data is still deeply owned by
    the strict codec immediately afterwards.
    """

    if type(value) is not dict:
        return None
    changed = False
    invalid = False
    snapshot: dict[str, Any] = {}
    try:
        for key, member in value.items():
            if type(key) is not str:
                invalid = True
                break
            snapshot[key] = member
    except RuntimeError:
        changed = True
    if changed:
        _fail(
            "pipeline_authority_unavailable",
            "history_entry_changed",
            "Pipeline history changed while it was being reduced",
        )
    if invalid:
        _fail(
            "pipeline_authority_unavailable",
            invalid_reason,
            invalid_message,
        )
    return snapshot


def _preflight_v2_authority_descriptor(body_input: Any) -> dict[str, Any] | None:
    """Own a strict v2 descriptor before the Operation codec deep-copies it.

    The generic Operation writer intentionally still rejects v2 in Batch 2.
    A cold reducer nevertheless needs to read already prepared test/history
    rows, and must apply the narrower 4 MiB/64 KiB authority budget before the
    legacy 16 MiB Operation copier can traverse the caller-owned descriptor.
    """

    body_snapshot = _shallow_exact_string_key_snapshot(
        body_input,
        invalid_reason="history_operation_body_invalid",
        invalid_message="Pipeline history contains an invalid Operation body",
    )
    if body_snapshot is None:
        return None
    pipeline = body_snapshot.get("pipeline")
    if type(pipeline) is not dict:
        return None

    pipeline_snapshot = _shallow_exact_string_key_snapshot(
        pipeline,
        invalid_reason="pipeline_descriptor_shape_invalid",
        invalid_message="Pipeline history contains an invalid authority descriptor",
    )
    assert pipeline_snapshot is not None
    schema = pipeline_snapshot.get("schema")
    if type(schema) is not str:
        return None
    if schema not in (_START_SCHEMA, _CONTROL_SCHEMA):
        return None

    changed = False
    invalid = False
    frozen: FrozenPipelineStart | FrozenPipelineControl | None = None
    try:
        frozen = (
            parse_pipeline_start(pipeline)
            if schema == _START_SCHEMA
            else parse_pipeline_control(pipeline)
        )
    except PipelineAuthorityCodecError:
        invalid = True
    except RuntimeError:
        changed = True
    if changed:
        _fail(
            "pipeline_authority_unavailable",
            "history_entry_changed",
            "Pipeline history changed while it was being reduced",
        )
    if invalid or frozen is None:
        if schema == _START_SCHEMA:
            _fail(
                "pipeline_start_identity_mismatch",
                "pipeline_start_descriptor_invalid",
                "Pipeline history contains an invalid Start descriptor",
            )
        _fail(
            "pipeline_control_invalid",
            "pipeline_control_descriptor_invalid",
            "Pipeline history contains an invalid Control descriptor",
        )

    invalid = False
    try:
        owned = materialize_pipeline_descriptor(frozen)
    except PipelineAuthorityCodecError:
        invalid = True
        owned = None
    if invalid or type(owned) is not dict:
        if schema == _START_SCHEMA:
            _fail(
                "pipeline_start_identity_mismatch",
                "pipeline_start_descriptor_invalid",
                "Pipeline history contains an invalid Start descriptor",
            )
        _fail(
            "pipeline_control_invalid",
            "pipeline_control_descriptor_invalid",
            "Pipeline history contains an invalid Control descriptor",
        )
    return owned


def _authority_envelope(
    body: dict[str, Any],
    *,
    code: str,
    reason: str,
    message: str,
) -> None:
    """Validate the narrow Operation envelope reserved for authority facts."""

    # v2 is intentionally not accepted by the general Operation carrier until
    # Batch 3 installs the mandatory pre-store sealed hook.  Validate today's
    # complete Operation envelope after removing only the independently parsed
    # authority descriptor; do not create an early writable-v2 window here.
    envelope = dict(body)
    envelope.pop("pipeline", None)
    _strict_operation(envelope, code=code, reason=reason, message=message)
    actor_identity = _actor_identity(body)
    if (
        body.get("kind") != "stage"
        or body.get("error") is not None
        or "external" in body
        or body.get("patches") != []
        or body.get("reverse_patches") != []
        or actor_identity is None
    ):
        _fail(code, reason, message)


def _project_exact_entity_add(body: dict[str, Any]) -> tuple[str, str] | None:
    """Project today's one-entity add shape without retaining its snapshot."""

    if (
        body.get("command_id") != "entity.add"
        or body.get("kind") != "native"
        or body.get("error") is not None
        or "external" in body
        or "pipeline" in body
    ):
        return None
    forward = body.get("patches")
    reverse = body.get("reverse_patches")
    if type(forward) is not list or type(reverse) is not list:
        return None
    if len(forward) != 1 or len(reverse) != 1:
        return None
    create = forward[0]
    delete = reverse[0]
    required_keys = frozenset(
        ("entity_type", "entity_id", "field", "op", "before", "after")
    )
    allowed_keys = required_keys | frozenset(("position",))
    if (
        type(create) is not dict
        or type(delete) is not dict
        or not required_keys.issubset(create)
        or not required_keys.issubset(delete)
        or not frozenset(create).issubset(allowed_keys)
        or not frozenset(delete).issubset(allowed_keys)
    ):
        return None
    entity_type = create["entity_type"]
    entity_id = create["entity_id"]
    snapshot = create["after"]
    if (
        type(entity_type) is not str
        or type(entity_id) is not str
        or create["field"] != "__entity__"
        or create["op"] != "create"
        or create["before"] is not None
        or type(snapshot) is not dict
        or snapshot.get("id") != entity_id
        or snapshot.get("type") != entity_type
        or delete["entity_type"] != entity_type
        or delete["entity_id"] != entity_id
        or delete["field"] != "__entity__"
        or delete["op"] != "delete"
        or not _json_exact_equal(delete["before"], snapshot)
        or delete["after"] is not None
        or type(create.get("position")) is not int
        or create["position"] < 0
        or type(delete.get("position")) is not int
        or delete["position"] != create["position"]
    ):
        return None
    return entity_type, entity_id


def _owned_history_rows(
    entries: Any,
    *,
    head_count: Any,
    head_hash: Any,
) -> tuple[
    tuple[_OwnedOperationRow, ...],
    tuple[_StartRow, ...],
    tuple[_ControlRow, ...],
]:
    """Own and verify each body once before retaining compact reduction facts."""

    if type(entries) is not tuple:
        _fail(
            "pipeline_authority_unavailable",
            "history_snapshot_not_detached",
            "Pipeline authority requires one detached complete history snapshot",
        )
    if type(head_count) is not int or head_count < 0 or head_count != len(entries):
        _fail(
            "pipeline_authority_unavailable",
            "history_head_count_mismatch",
            "Pipeline history rows do not match the supplied canonical head",
        )
    canonical_head_hash = _hash_text(head_hash)
    if canonical_head_hash is None:
        _fail(
            "pipeline_authority_unavailable",
            "history_head_hash_invalid",
            "Pipeline history head hash is invalid",
        )

    rows: list[_OwnedOperationRow] = []
    starts: list[_StartRow] = []
    controls: list[_ControlRow] = []
    # Every v2 Start turns its contiguous future closure members into new
    # authority rows.  Validate those rows with the current strict Operation
    # envelope without retroactively applying it to unrelated legacy history.
    strict_closure_positions: dict[int, list[str]] = {}
    previous_hash = ZERO_OPERATION_HASH

    for index, entry in enumerate(entries):
        entry_snapshot = _shallow_exact_string_key_snapshot(
            entry,
            invalid_reason="history_entry_shape_invalid",
            invalid_message="Pipeline history contains an invalid chain entry",
        )
        if entry_snapshot is None or frozenset(entry_snapshot) != _ENTRY_KEYS:
            _fail(
                "pipeline_authority_unavailable",
                "history_entry_shape_invalid",
                "Pipeline history contains an invalid chain entry",
            )
        body_input = entry_snapshot["op"]
        entry_previous_hash = entry_snapshot["prev_hash"]
        entry_self_hash = entry_snapshot["self_hash"]

        canonical_previous_hash = _hash_text(entry_previous_hash)
        if canonical_previous_hash is None:
            _fail(
                "pipeline_authority_unavailable",
                "history_previous_hash_invalid",
                "Pipeline history contains an invalid previous hash",
            )
        if canonical_previous_hash != previous_hash:
            _fail(
                "pipeline_authority_unavailable",
                "history_chain_discontinuous",
                "Pipeline history hash chain is discontinuous",
            )
        canonical_self_hash = _hash_text(entry_self_hash)
        if canonical_self_hash is None:
            _fail(
                "pipeline_authority_unavailable",
                "history_self_hash_invalid",
                "Pipeline history contains an invalid self hash",
            )

        owned_pipeline = _preflight_v2_authority_descriptor(body_input)
        body_for_freeze = body_input
        if owned_pipeline is not None:
            copy_changed = False
            try:
                body_for_freeze = dict(body_input)
            except RuntimeError:
                copy_changed = True
                body_for_freeze = None
            if copy_changed:
                _fail(
                    "pipeline_authority_unavailable",
                    "history_entry_changed",
                    "Pipeline history changed while it was being reduced",
                )
            body_for_freeze["pipeline"] = owned_pipeline
        try:
            frozen_body = freeze_operation_body(body_for_freeze)
            body = materialize_operation_body(frozen_body)
        except OperationRecordCodecError:
            _fail(
                "pipeline_authority_unavailable",
                "history_operation_body_invalid",
                "Pipeline history contains an invalid Operation body",
            )
        if index in strict_closure_positions:
            _strict_operation(
                body,
                code="pipeline_start_prefix_incomplete",
                reason="pipeline_start_member_operation_invalid",
                message="Pipeline Start closure contains an invalid Operation",
            )
        computed_hash = hashlib.sha256(
            previous_hash.encode("ascii") + frozen_body.body_bytes
        ).hexdigest()
        if not hmac.compare_digest(canonical_self_hash, computed_hash):
            _fail(
                "pipeline_authority_unavailable",
                "history_self_hash_mismatch",
                "Pipeline history Operation hash does not match its body",
            )

        operation_id = body.get("id") if type(body.get("id")) is str else None
        command_id = (
            body.get("command_id") if type(body.get("command_id")) is str else None
        )
        document_id = (
            body.get("document_id") if type(body.get("document_id")) is str else None
        )
        source = body.get("source") if type(body.get("source")) is str else None
        actor_identity = _actor_identity(body)
        patches = body.get("patches")
        created_entity_refs: tuple[tuple[str, str], ...] = ()
        if type(patches) is list:
            created_entity_refs = tuple(
                (patch["entity_type"], patch["entity_id"])
                for patch in patches
                if (
                    type(patch) is dict
                    and patch.get("op") == "create"
                    and patch.get("field") == "__entity__"
                    and type(patch.get("entity_type")) is str
                    and type(patch.get("entity_id")) is str
                )
            )
        entity_add_fact = _project_exact_entity_add(body)
        pipeline = body.get("pipeline")
        pipeline_schema: str | None = None
        if pipeline is not None:
            if type(pipeline) is not dict or type(pipeline.get("schema")) is not str:
                _fail(
                    "pipeline_authority_unavailable",
                    "pipeline_descriptor_shape_invalid",
                    "Pipeline history contains an invalid authority descriptor",
                )
            pipeline_schema = pipeline["schema"]

        row = _OwnedOperationRow(
            index=index,
            operation_id=operation_id,
            command_id=command_id,
            document_id=document_id,
            source=source,
            actor_identity=actor_identity,
            pipeline_schema=pipeline_schema,
            created_entity_refs=created_entity_refs,
            entity_add_fact=entity_add_fact,
            self_hash=canonical_self_hash,
        )
        rows.append(row)

        if pipeline_schema == _START_SCHEMA:
            try:
                start = parse_pipeline_start(pipeline)
            except PipelineAuthorityCodecError:
                _fail(
                    "pipeline_start_identity_mismatch",
                    "pipeline_start_descriptor_invalid",
                    "Pipeline history contains an invalid Start descriptor",
                )
            _authority_envelope(
                body,
                code="pipeline_start_identity_mismatch",
                reason="pipeline_start_operation_envelope_invalid",
                message="Pipeline Start uses an invalid Operation envelope",
            )
            for offset, expected_id in enumerate(start.initial_operation_ids[1:], 1):
                strict_closure_positions.setdefault(index + offset, []).append(
                    expected_id
                )
            starts.append(_StartRow(row=row, descriptor=start))
        elif pipeline_schema == _CONTROL_SCHEMA:
            try:
                control = parse_pipeline_control(pipeline)
            except PipelineAuthorityCodecError:
                _fail(
                    "pipeline_control_invalid",
                    "pipeline_control_descriptor_invalid",
                    "Pipeline history contains an invalid Control descriptor",
                )
            _authority_envelope(
                body,
                code="pipeline_control_invalid",
                reason="pipeline_control_operation_envelope_invalid",
                message="Pipeline Control uses an invalid Operation envelope",
            )
            controls.append(_ControlRow(row=row, descriptor=control))
        elif pipeline_schema in _LEGACY_CARRIER_SCHEMAS:
            # Permanent carrier-only history.  In particular, a run represented
            # only by v1 must reduce to pipeline_start_missing, never authority.
            pass
        elif pipeline_schema is not None:
            _fail(
                "pipeline_authority_unavailable",
                "pipeline_descriptor_schema_unknown",
                "Pipeline history contains an unknown authority descriptor",
            )

        previous_hash = canonical_self_hash

    actual_head_hash = previous_hash if rows else ZERO_OPERATION_HASH
    if not hmac.compare_digest(actual_head_hash, canonical_head_hash):
        _fail(
            "pipeline_authority_unavailable",
            "history_head_hash_mismatch",
            "Pipeline history rows do not end at the supplied canonical head",
        )
    return tuple(rows), tuple(starts), tuple(controls)


def _select_start(
    starts: tuple[_StartRow, ...],
    *,
    document_id: str,
    run_id: str,
    pipeline_id: str,
) -> _StartRow:
    candidates = tuple(row for row in starts if row.descriptor.run_id == run_id)
    if not candidates:
        _fail(
            "pipeline_start_missing",
            "pipeline_start_not_found",
            "No durable Pipeline Start exists for this run",
        )
    if len(candidates) != 1:
        _fail(
            "pipeline_start_ambiguous",
            "pipeline_start_duplicate",
            "More than one durable Pipeline Start exists for this run",
        )
    selected = candidates[0]
    start = selected.descriptor
    if start.document_id != document_id or start.pipeline_id != pipeline_id:
        _fail(
            "pipeline_start_identity_mismatch",
            "pipeline_start_target_mismatch",
            "Pipeline Start does not bind the requested document and pipeline",
        )
    for other in starts:
        if other is selected:
            continue
        if other.descriptor.start_operation_id == start.start_operation_id:
            _fail(
                "pipeline_start_ambiguous",
                "pipeline_start_operation_reused",
                "Pipeline Start operation identity is reused by another run",
            )
    return selected


def _verify_start_and_prefix(
    rows: tuple[_OwnedOperationRow, ...],
    selected: _StartRow,
) -> int:
    """Return the exclusive end of the exact Start closure prefix."""

    row = selected.row
    start = selected.descriptor
    if (
        row.operation_id != start.start_operation_id
        or row.document_id != start.document_id
        or row.command_id != start.start_command_id
    ):
        _fail(
            "pipeline_start_identity_mismatch",
            "pipeline_start_operation_mismatch",
            "Pipeline Start Operation does not match its durable descriptor",
        )

    initial_ids = start.initial_operation_ids
    if not initial_ids or initial_ids[0] != start.start_operation_id:
        _fail(
            "pipeline_start_prefix_incomplete",
            "pipeline_start_not_prefix_head",
            "Pipeline Start is not the first member of its initial closure",
        )
    closure_end = row.index + len(initial_ids)
    if closure_end > len(rows):
        _fail(
            "pipeline_start_prefix_incomplete",
            "pipeline_start_prefix_truncated",
            "Pipeline Start initial closure is truncated",
        )

    for offset, expected_id in enumerate(initial_ids):
        member = rows[row.index + offset]
        if member.operation_id != expected_id:
            _fail(
                "pipeline_start_prefix_incomplete",
                "pipeline_start_prefix_discontinuous",
                "Pipeline Start initial Operations are not one continuous prefix",
            )
        if member.document_id != start.document_id:
            _fail(
                "pipeline_start_identity_mismatch",
                "pipeline_start_member_document_mismatch",
                "Pipeline Start initial closure crosses a document identity",
            )
        if (
            member.source != row.source
            or member.actor_identity is None
            or member.actor_identity != row.actor_identity
        ):
            _fail(
                "pipeline_start_identity_mismatch",
                "pipeline_start_member_actor_mismatch",
                "Pipeline Start initial closure crosses its business actor",
            )
        if offset > 0 and member.pipeline_schema is not None:
            _fail(
                "pipeline_start_prefix_incomplete",
                "pipeline_start_nested_authority_descriptor",
                "Pipeline Start initial closure contains another authority descriptor",
            )

    expected_positions = {
        operation_id: row.index + offset
        for offset, operation_id in enumerate(initial_ids)
    }
    for member in rows:
        expected_position = expected_positions.get(member.operation_id)
        if expected_position is not None and member.index != expected_position:
            _fail(
                "pipeline_start_prefix_incomplete",
                "pipeline_start_member_identity_reused",
                "Pipeline Start initial Operation identity is reused outside its closure",
            )
    return closure_end


def _verify_placeholder_bindings(
    rows: tuple[_OwnedOperationRow, ...],
    selected: _StartRow,
    closure_end: int,
) -> dict[str, Any]:
    """Bind every declared placeholder to one exact durable entity.add row."""

    try:
        descriptor = materialize_pipeline_descriptor(selected.descriptor)
    except PipelineAuthorityCodecError:
        _fail(
            "pipeline_start_digest_mismatch",
            "pipeline_start_materialization_failed",
            "Pipeline Start descriptor cannot be rematerialized",
        )
    work_items = descriptor["work_list"]["items"]
    bindings = descriptor["placeholder_bindings"]
    item_by_id = {item["item_id"]: item for item in work_items}
    item_order = {item["item_id"]: index for index, item in enumerate(work_items)}
    previous_item_index = -1
    bound_operation_ids: set[str] = set()
    bound_targets: set[tuple[str, str]] = set()

    closure_by_id = {
        row.operation_id: row
        for row in rows[selected.row.index + 1:closure_end]
        if row.operation_id is not None
    }
    for binding in bindings:
        item = item_by_id.get(binding["item_id"])
        item_index = item_order.get(binding["item_id"])
        target = (binding["entity_type"], binding["entity_id"])
        operation_id = binding["operation_id"]
        operation_row = closure_by_id.get(operation_id)
        if (
            item is None
            or item_index is None
            or item_index <= previous_item_index
            or item["entity_type"] != target[0]
            or item["entity_id"] != target[1]
            or operation_id not in selected.descriptor.initial_operation_ids[1:]
            or operation_row is None
            or operation_row.command_id != "entity.add"
            or operation_row.entity_add_fact != target
            or operation_id in bound_operation_ids
            or target in bound_targets
        ):
            _fail(
                "pipeline_start_identity_mismatch",
                "pipeline_placeholder_binding_mismatch",
                "Pipeline placeholder binding does not match its durable add Operation",
            )
        previous_item_index = item_index
        bound_operation_ids.add(operation_id)
        bound_targets.add(target)

    work_targets = {
        (item["entity_type"], item["entity_id"])
        for item in work_items
    }
    created_work_targets: set[tuple[str, str]] = set()
    for row in rows[selected.row.index + 1:closure_end]:
        for target in row.created_entity_refs:
            if target in work_targets:
                if target in created_work_targets:
                    _fail(
                        "pipeline_start_identity_mismatch",
                        "pipeline_placeholder_created_twice",
                        "Pipeline placeholder target is created more than once",
                    )
                created_work_targets.add(target)
    if created_work_targets != bound_targets:
        _fail(
            "pipeline_start_identity_mismatch",
            "pipeline_placeholder_binding_incomplete",
            "Pipeline placeholder creation and descriptor bindings differ",
        )
    return descriptor


def _control_is_positive(control: FrozenPipelineControl) -> bool:
    return control.action in _POSITIVE_ACTIONS or (
        control.action == "set_breakpoint" and not control.parameters["halt"]
    )


def _verify_control_scope(
    control: FrozenPipelineControl,
    *,
    item_ids: frozenset[str],
    stage_ids: frozenset[str],
) -> None:
    parameters = control.parameters
    action = control.action
    valid = True
    if action == "retry_failed":
        requested = parameters["item_ids"]
        valid = len(requested) == len(set(requested)) and all(
            item_id in item_ids for item_id in requested
        )
    elif action in ("rerun_step", "run_from_step"):
        item_id = parameters["item_id"]
        valid = parameters["stage_id"] in stage_ids and (
            item_id is None or item_id in item_ids
        )
    elif action == "set_breakpoint":
        valid = parameters["stage_id"] in stage_ids
    if not valid:
        _fail(
            "pipeline_control_invalid",
            "pipeline_control_scope_invalid",
            "Pipeline Control references an item or stage outside the durable Start",
        )


def _verify_control_transition(
    control: FrozenPipelineControl,
    *,
    state: str,
) -> None:
    positive = _control_is_positive(control)
    # A later explicitly committed and sealed positive Control is the planned
    # way to continue after cold reconciliation.  Historical uncertainty stays
    # sticky, but it does not make the run permanently impossible to resume.
    # Batch 3, not this pure reducer, proves the new action's same-process seal.
    if positive and state not in (
        "paused", "interrupted", "reconcile_required"
    ):
        _fail(
            "pipeline_control_invalid",
            "pipeline_control_positive_transition_unsafe",
            "Pipeline positive Control cannot advance the current durable state",
        )


def _start_attestation_facts(
    payload: dict[str, Any],
    selected: _StartRow,
) -> _StartAttestationFacts:
    """Project the bounded facts later grant layers need, not raw descriptors."""

    invalid = False
    try:
        delegation = payload["delegation"]
        root_actor = selected.row.actor_identity
        root_source = selected.row.source
        worker_actor = _actor_payload_identity(delegation["worker_actor"])
        facts = _StartAttestationFacts(
            root_actor=root_actor,  # type: ignore[arg-type]
            root_source=root_source,  # type: ignore[arg-type]
            root_command_digest=delegation["root_command_digest"],
            document_policy_digest=delegation["document_policy_digest"],
            worker_actor=worker_actor,  # type: ignore[arg-type]
            worker_source=delegation["worker_source"],
            work_item_ids=tuple(
                item["item_id"] for item in payload["work_list"]["items"]
            ),
            work_item_bindings=tuple(
                PipelineWorkItemBindingFact(
                    item_id=item["item_id"],
                    entity_type=item["entity_type"],
                    entity_id=item["entity_id"],
                )
                for item in payload["work_list"]["items"]
            ),
            allowed_stage_ids=tuple(stage["id"] for stage in payload["stages"]),
            placeholder_bindings=tuple(
                PipelinePlaceholderBindingFact(
                    item_id=binding["item_id"],
                    entity_type=binding["entity_type"],
                    entity_id=binding["entity_id"],
                    operation_id=binding["operation_id"],
                )
                for binding in payload["placeholder_bindings"]
            ),
            delegated_targets=tuple(
                PipelineDelegatedTargetFact(
                    entity_type=target["entity_type"],
                    entity_id=target["entity_id"],
                    fields=tuple(target["fields"]),
                    lifecycle=tuple(target["lifecycle"]),
                )
                for target in delegation["targets"]
            ),
            delegated_creation_scopes=tuple(
                PipelineDelegatedCreationFact(
                    entity_type=scope["entity_type"],
                    fields=tuple(scope["fields"]),
                    max_per_operation=scope["max_per_operation"],
                    max_per_run=scope["max_per_run"],
                )
                for scope in delegation["creation_scopes"]
            ),
        )
        invalid = (
            root_actor is None
            or root_source is None
            or worker_actor is None
            or type(facts.root_command_digest) is not str
            or _hash_text(facts.root_command_digest) is None
            or type(facts.document_policy_digest) is not str
            or _hash_text(facts.document_policy_digest) is None
            or type(facts.worker_source) is not str
            or facts.worker_source not in _OPERATION_SOURCES
        )
    except (KeyError, TypeError):
        invalid = True
        facts = None
    if invalid or facts is None:
        _fail(
            "pipeline_start_identity_mismatch",
            "pipeline_start_attestation_invalid",
            "Pipeline Start cannot produce a bounded authority attestation",
        )
    return facts


def _control_next_state(
    control: FrozenPipelineControl,
    *,
    state: str,
) -> tuple[str, str | None]:
    action = control.action
    if action in _TERMINAL_ACTIONS:
        return "terminal", action
    if action == "pause":
        return "paused", None
    if action == "interrupt":
        return "interrupted", None
    if action == "set_breakpoint":
        # Arming a halt-after boundary is a monotonic restriction, not an
        # immediate pause.  Clearing it also leaves the actual run state alone;
        # the caller records that permission expansion separately as sticky
        # reconciliation uncertainty.
        return state, None
    if action in _POSITIVE_ACTIONS:
        return "reconcile_required", None
    _fail(
        "pipeline_control_invalid",
        "pipeline_control_action_unknown",
        "Pipeline Control contains an unsupported action",
    )
    raise AssertionError("unreachable")


def reduce_fresh_pipeline_history(
    entries: tuple[dict[str, Any], ...],
    *,
    head_count: int,
    head_hash: str,
    document_id: str,
    run_id: str,
    pipeline_id: str,
) -> ReducedPipelineAuthority:
    """Reduce one run from a complete canonical OpLog snapshot.

    ``entries``, ``head_count`` and ``head_hash`` must describe one atomic,
    freshly scanned history view (for example ``OpLog._canonical_history_snapshot``).
    The function independently rechecks the complete hash chain and exact head
    before interpreting any Start or Control descriptor.
    """

    if not all(
        _exact_nonempty_text(value)
        for value in (document_id, run_id, pipeline_id)
    ):
        raise TypeError("document_id, run_id and pipeline_id must be non-empty strings")

    rows, starts, controls = _owned_history_rows(
        entries,
        head_count=head_count,
        head_hash=head_hash,
    )
    selected = _select_start(
        starts,
        document_id=document_id,
        run_id=run_id,
        pipeline_id=pipeline_id,
    )
    closure_end = _verify_start_and_prefix(rows, selected)
    start_payload = _verify_placeholder_bindings(rows, selected, closure_end)
    start = selected.descriptor
    attestation = _start_attestation_facts(start_payload, selected)
    item_ids = frozenset(
        item["item_id"] for item in start_payload["work_list"]["items"]
    )
    stage_ids = frozenset(stage["id"] for stage in start_payload["stages"])

    relevant_controls: list[_ControlRow] = []
    for control_row in controls:
        control = control_row.descriptor
        same_run = control.run_id == start.run_id
        same_start = control.start_operation_id == start.start_operation_id
        if not same_run and not same_start:
            continue
        if not same_run or not same_start:
            _fail(
                "pipeline_control_invalid",
                "pipeline_control_binding_conflict",
                "Pipeline Control conflicts with the selected Start identity",
            )
        relevant_controls.append(control_row)

    generation = 0
    state = "paused"
    reconciliation_pending = False
    terminal_action: str | None = None
    active_breakpoint_stage_ids: set[str] = set()
    last_control_row: _ControlRow | None = None
    control_operation_ids: list[str] = []
    authority_operation_ids = set(start.initial_operation_ids)
    operation_id_counts: dict[str, int] = {}
    for row in rows:
        if row.operation_id is not None:
            operation_id_counts[row.operation_id] = (
                operation_id_counts.get(row.operation_id, 0) + 1
            )

    for control_row in relevant_controls:
        row = control_row.row
        control = control_row.descriptor
        if row.index < closure_end:
            _fail(
                "pipeline_control_invalid",
                "pipeline_control_precedes_start_closure",
                "Pipeline Control appears before the complete Start closure",
            )
        if state == "terminal":
            _fail(
                "pipeline_control_invalid",
                "pipeline_control_after_terminal",
                "Pipeline Control appears after a terminal control",
            )
        if (
            control.control_operation_id in authority_operation_ids
            or operation_id_counts.get(control.control_operation_id) != 1
        ):
            _fail(
                "pipeline_control_invalid",
                "pipeline_control_operation_identity_reused",
                "Pipeline Control Operation identity is not globally unique",
            )
        if (
            row.operation_id != control.control_operation_id
            or row.document_id != start.document_id
            or control.document_id != start.document_id
            or control.pipeline_id != start.pipeline_id
        ):
            _fail(
                "pipeline_control_invalid",
                "pipeline_control_identity_mismatch",
                "Pipeline Control Operation does not match the selected authority",
            )
        expected_command_id = _ACTION_COMMAND_IDS.get(control.action)
        if expected_command_id is None or row.command_id != expected_command_id:
            _fail(
                "pipeline_control_invalid",
                "pipeline_control_command_mismatch",
                "Pipeline Control action does not match its business command",
            )
        if not hmac.compare_digest(
            control.start_operation_hash,
            selected.row.self_hash,
        ):
            _fail(
                "pipeline_start_digest_mismatch",
                "pipeline_start_self_hash_mismatch",
                "Pipeline Control does not bind the canonical Start self hash",
            )
        if (
            control.expected_generation != generation
            or control.generation != generation + 1
            or control.expected_state != state
        ):
            _fail(
                "pipeline_control_invalid",
                "pipeline_control_transition_invalid",
                "Pipeline Control generation or expected state is stale",
            )
        _verify_control_scope(control, item_ids=item_ids, stage_ids=stage_ids)
        _verify_control_transition(
            control,
            state=state,
        )

        if control.action == "set_breakpoint":
            stage_id = control.parameters["stage_id"]
            if control.parameters["halt"]:
                active_breakpoint_stage_ids.add(stage_id)
            else:
                active_breakpoint_stage_ids.discard(stage_id)

        next_state, next_terminal_action = _control_next_state(
            control,
            state=state,
        )
        # Keep external-effect uncertainty separate from the latest restrictive
        # authority state.  A later pause can make future writes paused, but it
        # cannot prove whether an earlier resume already launched work.  Only a
        # terminal tombstone safely dominates that uncertainty.
        if (
            control.action == "set_breakpoint"
            and not control.parameters["halt"]
        ):
            reconciliation_pending = True
        elif next_state == "reconcile_required":
            reconciliation_pending = True
        elif next_state == "terminal":
            reconciliation_pending = False
            active_breakpoint_stage_ids.clear()
        state = next_state
        terminal_action = next_terminal_action
        generation = control.generation
        control_operation_ids.append(control.control_operation_id)
        authority_operation_ids.add(control.control_operation_id)
        last_control_row = control_row

    digest = _digest_hex(start.descriptor_digest)
    return ReducedPipelineAuthority(
        protocol=PIPELINE_AUTHORITY_REDUCTION_PROTOCOL,
        document_id=start.document_id,
        run_id=start.run_id,
        pipeline_id=start.pipeline_id,
        start_command_id=start.start_command_id,
        start_operation_id=start.start_operation_id,
        start_operation_hash=selected.row.self_hash,
        start_descriptor_digest=digest,
        initial_operation_ids=start.initial_operation_ids,
        root_actor=attestation.root_actor,
        root_source=attestation.root_source,
        root_command_digest=attestation.root_command_digest,
        document_policy_digest=attestation.document_policy_digest,
        worker_actor=attestation.worker_actor,
        worker_source=attestation.worker_source,
        work_item_ids=attestation.work_item_ids,
        work_item_bindings=attestation.work_item_bindings,
        allowed_stage_ids=attestation.allowed_stage_ids,
        placeholder_bindings=attestation.placeholder_bindings,
        delegated_targets=attestation.delegated_targets,
        delegated_creation_scopes=attestation.delegated_creation_scopes,
        active_breakpoint_stage_ids=tuple(sorted(active_breakpoint_stage_ids)),
        generation=generation,
        state=state,
        reconciliation_pending=reconciliation_pending,
        terminal_action=terminal_action,
        last_control_operation_id=(
            last_control_row.descriptor.control_operation_id
            if last_control_row is not None
            else None
        ),
        last_control_hash=(
            last_control_row.row.self_hash if last_control_row is not None else None
        ),
        control_operation_ids=tuple(control_operation_ids),
        history_head_count=head_count,
        history_head_hash=head_hash,
    )


__all__ = (
    "PIPELINE_AUTHORITY_REDUCTION_PROTOCOL",
    "PipelineDelegatedCreationFact",
    "PipelineDelegatedTargetFact",
    "PipelineAuthorityReductionError",
    "PipelinePlaceholderBindingFact",
    "PipelineWorkItemBindingFact",
    "ReducedPipelineAuthority",
    "reduce_fresh_pipeline_history",
)
