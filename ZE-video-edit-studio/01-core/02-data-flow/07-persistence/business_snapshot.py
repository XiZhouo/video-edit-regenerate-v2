"""Pure, deterministic business-head snapshot contract.

This module is the in-memory boundary between the current ``Document`` graph
and a future persistence adapter.  It deliberately does not know how to scan a
legacy project, open SQLite, write rows, issue receipts, or choose an authority.
Callers capture while holding the project mutation lock, pass the frozen rows to
an adapter, and use :func:`verify_business_snapshot` before trusting rows read
from any persistence medium.

The implementation does not use ``deepcopy`` or dataclass ``asdict`` on live
business values.  A live graph is projected by hand and immediately frozen by
the deterministic codec.  Every SQL-shaped column is then derived from that
same codec-owned value graph.
"""

from __future__ import annotations

import hashlib
import math
import threading
import weakref
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple


# The v3 core intentionally uses flat leaf imports.  Import the concrete domain
# leaves directly; importing ``sdk`` here would create both a cycle and a public
# API promise for this adapter-internal contract.  The core bootstrap/test
# harness owns the flat-leaf import path; a business contract must not perform
# filesystem discovery as an import side effect.
from business_codec import (  # noqa: E402
    BusinessCodecError,
    CODEC_ID,
    CODEC_VERSION,
    MAX_NESTING_DEPTH,
    decode_payload_bounded,
    encode_payload_bounded,
    payload_sha256,
)
from document import Document  # noqa: E402
from entity import (  # noqa: E402
    AiProvenance,
    Entity,
    ZeError as EntityZeError,
    _clone_json_envelope,
    _clone_json_value,
    _no_op_validator,
)
from field_types import canonicalize_entity_schema, is_framework_validator  # noqa: E402
from hash_framing import update_hash_frame as _update_hash_frame  # noqa: E402


SNAPSHOT_FORMAT_VERSION = 1

PROJECT_SCHEMA = "ze.business-snapshot.project.v1"
DOCUMENT_SCHEMA = "ze.business-snapshot.document.v1"
ENTITY_SCHEMA = "ze.business-snapshot.entity.v1"
ENTITY_SET_HASH_DOMAIN = "ze.business-snapshot.entity-set.v1"

MAX_PROJECT_SNAPSHOT_BYTES = 64 * 1024
MAX_DOCUMENT_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_ENTITY_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_ENTITY_COUNT = 1_000_000
MAX_TOTAL_SNAPSHOT_BYTES = 1024 * 1024 * 1024
MAX_SQLITE_INTEGER = 2**63 - 1
# A single envelope may contain rich editor state, but 250k serialized value/key
# nodes already represents orders of magnitude more structure than an ordinary
# mutation while bounding pre-encoding traversal and normalized-copy memory.
MAX_SNAPSHOT_VALUE_NODES = 250_000

_VALID_COLLABORATION_MODES = frozenset(
    ("live_collab", "with_save", "realtime_checkpoint")
)
_VALID_SAVE_POLICIES = frozenset(
    ("auto_commit", "checkpoint_commit", "legacy")
)
_VALID_ACTOR_SOURCES = frozenset(("user", "ai", "sync", "system"))
_LOWER_HEX = frozenset("0123456789abcdef")

_OUTER_KEYS = frozenset(("schema", "identity", "state"))
_PROJECT_IDENTITY_KEYS = frozenset(("project_id",))
_PROJECT_STATE_KEYS = frozenset(("generation", "document", "entities"))
_PROJECT_DOCUMENT_DESCRIPTOR_KEYS = frozenset(
    (
        "document_id",
        "document_version",
        "codec_id",
        "codec_version",
        "snapshot_format_version",
        "snapshot_sha256",
    )
)
_PROJECT_ENTITY_SET_KEYS = frozenset(("count", "set_sha256"))
_DOCUMENT_IDENTITY_KEYS = frozenset(("project_id", "document_id"))
_DOCUMENT_STATE_KEYS = frozenset(
    (
        "document_version",
        "schema_version",
        "collaboration_mode",
        "save_policy",
        "name",
        "data_schema",
        "config",
        "acl",
        "created_at",
        "updated_at",
    )
)
_ENTITY_IDENTITY_KEYS = frozenset(
    ("project_id", "document_id", "entity_type", "entity_id")
)
_ENTITY_STATE_KEYS = frozenset(
    (
        "entity_version",
        "collection_position",
        "deleted",
        "name",
        "data",
        "provenance",
        "actor_log",
        "created_at",
        "updated_at",
    )
)
_PROVENANCE_KEYS = frozenset(
    (
        "actor",
        "model",
        "model_version",
        "prompt_template_id",
        "prompt_args",
        "cost_usd",
        "token_in",
        "token_out",
        "ts",
        "trace_id",
    )
)
_ACTOR_LOG_EVENT_KEYS = frozenset(("version", "actor", "ts", "fields"))
_ACTOR_KEYS = frozenset(("user_id", "device_id", "source"))
_PROJECT_ROW_KEYS = frozenset(
    (
        "project_id",
        "generation",
        "codec_id",
        "codec_version",
        "snapshot_format_version",
        "snapshot_bytes",
        "snapshot_sha256",
    )
)
_DOCUMENT_ROW_KEYS = frozenset(
    (
        "project_id",
        "document_id",
        "document_version",
        "schema_version",
        "collaboration_mode",
        "save_policy",
        "codec_id",
        "codec_version",
        "snapshot_format_version",
        "snapshot_bytes",
        "snapshot_sha256",
    )
)
_ENTITY_ROW_KEYS = frozenset(
    (
        "project_id",
        "document_id",
        "entity_type",
        "entity_id",
        "entity_version",
        "deleted",
        "codec_id",
        "codec_version",
        "snapshot_format_version",
        "snapshot_bytes",
        "snapshot_sha256",
    )
)
_SNAPSHOT_SET_KEYS = frozenset(("project", "document", "entities"))
_DOCUMENT_DOMAIN_STATE_KEYS = frozenset(
    (
        "_id",
        "_name",
        "_schema_version",
        "_collaboration_mode",
        "_save_policy",
        "_schema",
        "_mutation_policies",
        "_mutation_policy_sealed",
        "_mutation_policy_registry_authority",
        "_mutation_policy_registry_epoch",
        "_mutation_policy_registry_pid",
        "_mutation_policy_command_authorities",
        "_history_op_log_authority",
        "_history_controller_authority",
        "_history_authority_epoch",
        "_history_authority_pid",
        "_config",
        "_acl",
        "_harvest_rules",
        "_read_only_sources",
        "_collections",
        "_v",
        "_created_at",
        "_updated_at",
    )
)
_ENTITY_DOMAIN_STATE_KEYS = frozenset(
    (
        "_id",
        "_type",
        "_name",
        "_data",
        "_version",
        "_deleted",
        "_read_only",
        "_schema",
        "_validator",
        "_document_owner",
        "_document_admission_state",
        "_provenance",
        "_actor_log",
        "_created_at",
        "_updated_at",
    )
)


class BusinessSnapshotError(ValueError):
    """Stable, non-payload-bearing failure at the business snapshot boundary."""

    def __init__(self, code: str, path: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty string")
        if type(path) is not str or not path:
            raise TypeError("path must be a non-empty string")
        self.code = code
        self.path = path
        # ``path`` may eventually contain business identifiers.  Keep it as a
        # structured attribute for trusted diagnostics, not in exception text.
        super().__init__(code)


@dataclass(frozen=True)
class ProjectSnapshotRow:
    project_id: str
    generation: int
    codec_id: str
    codec_version: int
    snapshot_format_version: int
    snapshot_bytes: bytes = field(repr=False)
    snapshot_sha256: bytes = field(repr=False)


@dataclass(frozen=True)
class DocumentSnapshotRow:
    project_id: str
    document_id: str
    document_version: int
    schema_version: str
    collaboration_mode: str
    save_policy: str | None
    codec_id: str
    codec_version: int
    snapshot_format_version: int
    snapshot_bytes: bytes = field(repr=False)
    snapshot_sha256: bytes = field(repr=False)


@dataclass(frozen=True)
class EntitySnapshotRow:
    project_id: str
    document_id: str
    entity_type: str
    entity_id: str
    entity_version: int
    deleted: int
    codec_id: str
    codec_version: int
    snapshot_format_version: int
    snapshot_bytes: bytes = field(repr=False)
    snapshot_sha256: bytes = field(repr=False)


@dataclass(frozen=True)
class EntityKey:
    entity_type: str
    entity_id: str


@dataclass(frozen=True)
class BusinessSnapshotSet:
    project: ProjectSnapshotRow
    document: DocumentSnapshotRow
    entities: Tuple[EntitySnapshotRow, ...]


@dataclass(frozen=True)
class CaptureResult:
    snapshot: BusinessSnapshotSet
    excluded_read_only: Tuple[EntityKey, ...]


@dataclass(frozen=True, init=False, eq=False)
class RestoredBusinessHead:
    project_id: str
    generation: int
    document: Document = field(repr=False)
    # A restored head proves canonical persisted business state only.  Runtime
    # declarations/ports (harvest rules, read-only sources, etc.) are excluded
    # by format and must be installed by the single runtime rehydration seam.
    requires_runtime_rehydration: bool


@dataclass
class _RestoredBusinessHeadRecord:
    snapshot: BusinessSnapshotSet
    state: str = "detached"


def _build_restored_head_ledger():
    """Keep restore authority off the publicly inspectable carrier itself."""
    lock = threading.RLock()
    records: "weakref.WeakKeyDictionary[RestoredBusinessHead, _RestoredBusinessHeadRecord]" = (
        weakref.WeakKeyDictionary()
    )

    def issue(
        head: RestoredBusinessHead,
        snapshot: BusinessSnapshotSet,
    ) -> None:
        with lock:
            records[head] = _RestoredBusinessHeadRecord(snapshot=snapshot)

    def consume(value: Any) -> BusinessSnapshotSet:
        if type(value) is not RestoredBusinessHead:
            _fail("business_snapshot_restored_head_invalid", "$.restore_head")
        with lock:
            record = records.get(value)
            if record is None or record.state != "detached":
                _fail("business_snapshot_restored_head_invalid", "$.restore_head")
            expected = record.snapshot
        if (
            getattr(value, "project_id", None) != expected.project.project_id
            or getattr(value, "generation", None) != expected.project.generation
            or type(getattr(value, "document", None)) is not Document
            or getattr(value, "document", None).id != expected.document.document_id
            or getattr(value, "requires_runtime_rehydration", None) is not True
        ):
            _fail("business_snapshot_restored_head_invalid", "$.restore_head")

        # Restore returns a mutable Document because runtime declarations still
        # need installation.  Re-root it immediately before handoff so public
        # mutation between restore and rehydration cannot be mislabeled a no-op.
        current = capture_business_snapshot(
            expected.project.project_id,
            expected.project.generation,
            value.document,
        )
        if current.excluded_read_only or current.snapshot != expected:
            _fail("business_snapshot_restored_head_changed", "$.restore_head")
        with lock:
            record = records.get(value)
            if record is None or record.state != "detached" or record.snapshot != expected:
                _fail("business_snapshot_restored_head_invalid", "$.restore_head")
            record.state = "consumed"
        return expected

    return issue, consume


_issue_restored_business_head, _consume_restored_business_head = (
    _build_restored_head_ledger()
)


def _fail(code: str, path: str) -> None:
    raise BusinessSnapshotError(code, path)


def _require_exact_dict(value: Any, path: str) -> Dict[str, Any]:
    if type(value) is not dict:
        _fail("business_snapshot_value_invalid", path)
    return value


def _require_exact_list(value: Any, path: str) -> List[Any]:
    if type(value) is not list:
        _fail("business_snapshot_value_invalid", path)
    return value


def _require_exact_keys(value: Dict[str, Any], keys: frozenset[str], path: str) -> None:
    if len(value) != len(keys):
        _fail("business_snapshot_shape_invalid", path)
    # Capture also applies this helper to exact built-in dicts owned by a live
    # domain graph.  Do not build a set before checking key type: doing so could
    # execute an extension object's hostile ``__hash__`` hook.
    for key in value.keys():
        if type(key) is not str or key not in keys:
            _fail("business_snapshot_shape_invalid", path)


def _require_domain_state_inventory(
    value: Any,
    keys: frozenset[str],
    path: str,
) -> Dict[str, Any]:
    if type(value) is not dict or len(value) != len(keys):
        _fail("business_snapshot_domain_state_invalid", path)
    for key in value.keys():
        if type(key) is not str or key not in keys:
            _fail("business_snapshot_domain_state_invalid", path)
    return value


def _require_utf8_string(
    value: Any,
    path: str,
    *,
    nonempty: bool = False,
) -> str:
    if type(value) is not str or (nonempty and not value):
        _fail("business_snapshot_value_invalid", path)
    invalid = False
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        invalid = True
    if invalid:
        _fail("business_snapshot_value_invalid", path)
    return value


def _require_string_shape(value: Any, path: str, *, nonempty: bool = False) -> str:
    """Check an untrusted row scalar without allocating a UTF-8 copy.

    A row-side string is validated before trust only for exact type/emptiness;
    after bounded payload decoding it must equal the payload-side UTF-8 string.
    """
    if type(value) is not str or (nonempty and not value):
        _fail("business_snapshot_value_invalid", path)
    return value


def _require_bounded_utf8_string(
    value: Any,
    path: str,
    maximum_bytes: int,
    *,
    nonempty: bool = True,
) -> Tuple[str, int]:
    """Validate one live string without allocating beyond its row budget."""
    if (
        type(value) is not str
        or (nonempty and not value)
        or len(value) > maximum_bytes
    ):
        _fail("business_snapshot_value_invalid", path)
    invalid = False
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        invalid = True
        encoded = b""
    if invalid or len(encoded) > maximum_bytes:
        _fail("business_snapshot_value_invalid", path)
    return value, len(encoded)


def _require_project_id(value: Any, path: str) -> str:
    if type(value) is not str or not value or len(value) > 160:
        _fail("business_snapshot_project_id_invalid", path)
    if (
        value in (".", "..")
        or value.startswith(".ze-")
        or any(character in value for character in ("/", "\\", "\x00", "\n", "\r", "\t"))
    ):
        _fail("business_snapshot_project_id_invalid", path)
    return _require_utf8_string(value, path, nonempty=True)


def _require_sqlite_positive_integer(value: Any, path: str) -> int:
    if type(value) is not int or value < 1 or value > MAX_SQLITE_INTEGER:
        _fail("business_snapshot_integer_invalid", path)
    return value


def _require_entity_count(value: Any, path: str) -> int:
    # An editor with no business entities is a valid head, so this aggregate
    # (unlike SQL version columns) deliberately includes zero.
    if type(value) is not int or value < 0 or value > MAX_ENTITY_COUNT:
        _fail("business_snapshot_integer_invalid", path)
    return value


def _require_nonnegative_integer(value: Any, path: str) -> int:
    # Provenance counters are payload values rather than SQLite INTEGER
    # columns.  Their numeric ceiling is therefore the deterministic codec's
    # domain, not SQLite's signed-64-bit storage range.
    if type(value) is not int or value < 0:
        _fail("business_snapshot_integer_invalid", path)
    return value


def _require_nonnegative_number(value: Any, path: str) -> int | float:
    if type(value) is int:
        if value < 0:
            _fail("business_snapshot_number_invalid", path)
        return value
    if type(value) is float:
        if not math.isfinite(value) or value < 0:
            _fail("business_snapshot_number_invalid", path)
        return value
    _fail("business_snapshot_number_invalid", path)
    raise AssertionError("unreachable")  # for static type checkers


def _require_lower_hex_digest(value: Any, path: str) -> str:
    digest = _require_utf8_string(value, path)
    if len(digest) != 64 or any(character not in _LOWER_HEX for character in digest):
        _fail("business_snapshot_digest_invalid", path)
    return digest


def _encode_envelope(value: Dict[str, Any], path: str, maximum: int) -> Tuple[bytes, Dict[str, Any], bytes]:
    encode_failure_code = ""
    try:
        payload = encode_payload_bounded(value, maximum, MAX_SNAPSHOT_VALUE_NODES)
    except BusinessCodecError as exc:
        encode_failure_code = exc.code
        payload = b""
    if encode_failure_code:
        # Raise after leaving the handler: ``from None`` hides display but still
        # retains ``__context__`` and its traceback locals (including payload).
        if encode_failure_code in (
            "business_codec_payload_too_large",
            "business_codec_node_limit_exceeded",
        ):
            raise BusinessSnapshotError(
                "business_snapshot_payload_size_invalid", path
            )
        raise BusinessSnapshotError("business_snapshot_codec_invalid", path)
    if not payload or len(payload) > maximum:
        _fail("business_snapshot_payload_size_invalid", path)
    decode_failure_code = ""
    try:
        frozen = decode_payload_bounded(
            payload, maximum, MAX_SNAPSHOT_VALUE_NODES
        )
    except BusinessCodecError as exc:
        decode_failure_code = exc.code
        frozen = None
    if decode_failure_code:
        # Encoding and immediate decoding must be a closed invariant.  Keep a
        # stable business-layer failure if a future codec violates it.
        if decode_failure_code in (
            "business_codec_payload_too_large",
            "business_codec_node_limit_exceeded",
        ):
            raise BusinessSnapshotError(
                "business_snapshot_payload_size_invalid", path
            )
        raise BusinessSnapshotError("business_snapshot_codec_invalid", path)
    if type(frozen) is not dict:
        _fail("business_snapshot_shape_invalid", path)
    return payload, frozen, payload_sha256(payload)


def _decode_row_payload(payload: Any, path: str, maximum: int) -> Dict[str, Any]:
    if type(payload) is not bytes or not payload or len(payload) > maximum:
        _fail("business_snapshot_payload_size_invalid", path)
    decode_failure_code = ""
    try:
        decoded = decode_payload_bounded(
            payload, maximum, MAX_SNAPSHOT_VALUE_NODES
        )
    except BusinessCodecError as exc:
        decode_failure_code = exc.code
        decoded = None
    if decode_failure_code:
        if decode_failure_code in (
            "business_codec_payload_too_large",
            "business_codec_node_limit_exceeded",
        ):
            raise BusinessSnapshotError(
                "business_snapshot_payload_size_invalid", path
            )
        raise BusinessSnapshotError("business_snapshot_payload_invalid", path)
    if type(decoded) is not dict:
        _fail("business_snapshot_shape_invalid", path)
    return decoded


def _verify_and_decode_row_payload(
    payload: Any,
    digest: Any,
    path: str,
    maximum: int,
) -> Dict[str, Any]:
    # Hash shape/recomputation deliberately precedes JSON interpretation.  A
    # corrupt row is not handed to the decoder merely because its bytes happen
    # to look like JSON.
    if type(payload) is not bytes or not payload or len(payload) > maximum:
        _fail("business_snapshot_payload_size_invalid", path + ".snapshot_bytes")
    _verify_hash(payload, digest, path + ".snapshot_sha256")
    return _decode_row_payload(payload, path + ".snapshot_bytes", maximum)


def _verify_hash(payload: bytes, digest: Any, path: str) -> None:
    if type(digest) is not bytes or len(digest) != 32:
        _fail("business_snapshot_hash_invalid", path)
    if payload_sha256(payload) != digest:
        _fail("business_snapshot_hash_invalid", path)


def _verify_codec_columns(row: Any, path: str) -> None:
    # Type checks precede all value operations: row fields are untrusted and a
    # hostile object could execute comparison hooks such as ``__ne__``.
    if type(row.codec_id) is not str or row.codec_id != CODEC_ID:
        _fail("business_snapshot_codec_metadata_invalid", path + ".codec_id")
    if type(row.codec_version) is not int or row.codec_version != CODEC_VERSION:
        _fail("business_snapshot_codec_metadata_invalid", path + ".codec_version")
    if (
        type(row.snapshot_format_version) is not int
        or row.snapshot_format_version != SNAPSHOT_FORMAT_VERSION
    ):
        _fail(
            "business_snapshot_codec_metadata_invalid",
            path + ".snapshot_format_version",
        )


def _preflight_passive_row(
    row: Any,
    expected_type: type,
    expected_keys: frozenset[str],
    path: str,
    maximum: int,
) -> int:
    """Check a passive carrier and byte budget without interpreting payload."""
    if type(row) is not expected_type:
        _fail("business_snapshot_row_type_invalid", path)
    state = object.__getattribute__(row, "__dict__")
    if type(state) is not dict:
        _fail("business_snapshot_row_shape_invalid", path)
    _require_exact_keys(state, expected_keys, path)
    payload = state["snapshot_bytes"]
    if type(payload) is not bytes or not payload or len(payload) > maximum:
        _fail("business_snapshot_payload_size_invalid", path + ".snapshot_bytes")
    return len(payload)


def _verify_outer_envelope(
    payload: Dict[str, Any],
    schema: str,
    identity_keys: frozenset[str],
    state_keys: frozenset[str],
    path: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    _require_exact_keys(payload, _OUTER_KEYS, path)
    if type(payload["schema"]) is not str or payload["schema"] != schema:
        _fail("business_snapshot_schema_invalid", path + ".schema")
    identity = _require_exact_dict(payload["identity"], path + ".identity")
    state = _require_exact_dict(payload["state"], path + ".state")
    _require_exact_keys(identity, identity_keys, path + ".identity")
    _require_exact_keys(state, state_keys, path + ".state")
    return identity, state


def _verify_domain_json_value(value: Any, path: str) -> None:
    """Prove one persisted value fits the Entity/Document ownership domain.

    The deterministic codec permits 128 container levels so an enclosing
    snapshot can wrap a 120-level business value.  Decoding therefore proves
    only the wire-format domain.  Restore constructors and public getters use
    the stricter domain clone (120 levels and 250,000 expanded value/key
    nodes), so verification must run the same no-hook clone before declaring a
    row restorable.  The detached result is deliberately discarded.
    """
    clone_failed = False
    try:
        _clone_json_value(value)
    except EntityZeError:
        clone_failed = True
    if clone_failed:
        _fail("business_snapshot_domain_value_invalid", path)


def _verify_domain_json_envelope(value: Any, path: str) -> None:
    """Apply the domain's per-field depth reset and shared envelope budget."""
    clone_failed = False
    try:
        _clone_json_envelope(value)
    except EntityZeError:
        clone_failed = True
    if clone_failed:
        _fail("business_snapshot_domain_value_invalid", path)


def _verify_timestamps(state: Dict[str, Any], path: str) -> None:
    created_at = _require_nonnegative_number(state["created_at"], path + ".created_at")
    updated_at = _require_nonnegative_number(state["updated_at"], path + ".updated_at")
    if created_at > updated_at:
        _fail("business_snapshot_timestamp_invalid", path)


def _verify_data_schema(value: Any, path: str) -> Dict[str, Any]:
    schema = _require_exact_dict(value, path)
    for entity_type, entity_schema in schema.items():
        _require_utf8_string(entity_type, path, nonempty=True)
        if entity_type == "__document__":
            _fail("business_snapshot_entity_type_invalid", path)
        _require_exact_dict(entity_schema, path)
        invalid = False
        try:
            canonical = canonicalize_entity_schema(entity_schema)
        except EntityZeError:
            invalid = True
            canonical = None
        if invalid or canonical != entity_schema:
            _fail("business_snapshot_schema_invalid", path)
    return schema


def _verify_acl(value: Any, path: str) -> Dict[str, Any]:
    acl = _require_exact_dict(value, path)
    if "owner" in acl:
        _require_utf8_string(acl["owner"], path + ".owner")
    if "collaborators" in acl:
        collaborators = _require_exact_list(acl["collaborators"], path + ".collaborators")
        seen = set()
        for collaborator in collaborators:
            member = _require_utf8_string(collaborator, path + ".collaborators")
            if member in seen:
                _fail("business_snapshot_acl_invalid", path + ".collaborators")
            seen.add(member)
    return acl


def _verify_provenance(value: Any, path: str) -> Dict[str, Any]:
    provenance = _require_exact_dict(value, path)
    for field_name, raw in provenance.items():
        _require_utf8_string(field_name, path)
        item = _require_exact_dict(raw, path)
        _require_exact_keys(item, _PROVENANCE_KEYS, path)
        for member in (
            "actor",
            "model",
            "model_version",
            "prompt_template_id",
            "trace_id",
        ):
            _require_utf8_string(item[member], path + "." + member)
        prompt_args = _require_exact_dict(
            item["prompt_args"], path + ".prompt_args"
        )
        _verify_domain_json_value(prompt_args, path + ".prompt_args")
        _require_nonnegative_number(item["cost_usd"], path + ".cost_usd")
        _require_nonnegative_integer(item["token_in"], path + ".token_in")
        _require_nonnegative_integer(item["token_out"], path + ".token_out")
        _require_nonnegative_number(item["ts"], path + ".ts")
        # AiProvenance then owns one ten-field envelope.  Its depth resets per
        # field while one expanded-node budget covers the whole item; the
        # prompt_args clone above alone would miss that cumulative boundary.
        _verify_domain_json_envelope(item, path + ".item")
    return provenance


def _verify_actor_log(value: Any, entity_version: int, path: str) -> List[Any]:
    actor_log = _require_exact_list(value, path)
    previous_version = 0
    for index, raw in enumerate(actor_log):
        event_path = "%s[%d]" % (path, index)
        event = _require_exact_dict(raw, event_path)
        _require_exact_keys(event, _ACTOR_LOG_EVENT_KEYS, event_path)
        version = _require_sqlite_positive_integer(
            event["version"], event_path + ".version"
        )
        if version <= previous_version or version > entity_version:
            _fail("business_snapshot_actor_log_invalid", event_path + ".version")
        previous_version = version
        actor = _require_exact_dict(event["actor"], event_path + ".actor")
        _require_exact_keys(actor, _ACTOR_KEYS, event_path + ".actor")
        _require_utf8_string(actor["user_id"], event_path + ".actor.user_id")
        _require_utf8_string(actor["device_id"], event_path + ".actor.device_id")
        source = _require_utf8_string(actor["source"], event_path + ".actor.source")
        if source not in _VALID_ACTOR_SOURCES:
            _fail("business_snapshot_actor_log_invalid", event_path + ".actor.source")
        _require_nonnegative_number(event["ts"], event_path + ".ts")
        fields = _require_exact_list(event["fields"], event_path + ".fields")
        for field_name in fields:
            _require_utf8_string(field_name, event_path + ".fields")
    # Entity.actor_log() and legacy restoration own the complete list as one
    # value, not one event at a time.
    _verify_domain_json_value(actor_log, path)
    return actor_log


def _entity_descriptor(row: EntitySnapshotRow) -> Dict[str, Any]:
    return {
        "codec_id": row.codec_id,
        "codec_version": row.codec_version,
        "deleted": bool(row.deleted),
        "entity_id": row.entity_id,
        "entity_type": row.entity_type,
        "entity_version": row.entity_version,
        "snapshot_format_version": row.snapshot_format_version,
        "snapshot_sha256": row.snapshot_sha256.hex(),
    }


def _entity_set_digest(rows: Tuple[EntitySnapshotRow, ...]) -> str:
    """Return the v1 domain-separated, length-framed streaming set root."""
    hasher = hashlib.sha256()
    _update_hash_frame(hasher, ENTITY_SET_HASH_DOMAIN.encode("utf-8"))
    count_bytes = len(rows).to_bytes(8, "big")
    # The count is itself an eight-byte value and is framed like every other
    # component; the intentional double length makes the grammar unambiguous.
    _update_hash_frame(hasher, count_bytes)
    for row in rows:
        encode_failure_code = ""
        try:
            payload = encode_payload_bounded(
                _entity_descriptor(row),
                MAX_ENTITY_SNAPSHOT_BYTES,
                MAX_SNAPSHOT_VALUE_NODES,
            )
        except BusinessCodecError as exc:
            encode_failure_code = exc.code
            payload = b""
        if encode_failure_code:
            if encode_failure_code in (
                "business_codec_payload_too_large",
                "business_codec_node_limit_exceeded",
            ):
                raise BusinessSnapshotError(
                    "business_snapshot_payload_size_invalid",
                    "$.project.state.entities",
                )
            raise BusinessSnapshotError(
                "business_snapshot_codec_invalid", "$.project.state.entities"
            )
        _update_hash_frame(hasher, payload)
    return hasher.hexdigest()


def _project_document_descriptor(row: DocumentSnapshotRow) -> Dict[str, Any]:
    return {
        "document_id": row.document_id,
        "document_version": row.document_version,
        "codec_id": row.codec_id,
        "codec_version": row.codec_version,
        "snapshot_format_version": row.snapshot_format_version,
        "snapshot_sha256": row.snapshot_sha256.hex(),
    }


def _copy_provenance_for_capture(value: Any, path: str) -> Dict[str, Any]:
    source = _require_exact_dict(value, path)
    # Each map entry serializes at least: field key + item object + ten item
    # keys + ten item values.  Reject impossible node budgets before allocating
    # one projected dict per provenance record.
    if len(source) * 22 > MAX_SNAPSHOT_VALUE_NODES:
        _fail("business_snapshot_payload_size_invalid", path)
    copied: Dict[str, Any] = {}
    field_name_bytes = 0
    for field_name, raw in source.items():
        _field_name, encoded_size = _require_bounded_utf8_string(
            field_name,
            path,
            MAX_ENTITY_SNAPSHOT_BYTES,
            nonempty=False,
        )
        field_name_bytes += encoded_size
        if field_name_bytes > MAX_ENTITY_SNAPSHOT_BYTES:
            _fail("business_snapshot_payload_size_invalid", path)
        if type(raw) is not AiProvenance:
            _fail("business_snapshot_provenance_invalid", path)
        provenance_state = _require_domain_state_inventory(
            object.__getattribute__(raw, "__dict__"),
            _PROVENANCE_KEYS,
            path,
        )
        copied[field_name] = {
            "actor": provenance_state["actor"],
            "model": provenance_state["model"],
            "model_version": provenance_state["model_version"],
            "prompt_template_id": provenance_state["prompt_template_id"],
            "prompt_args": provenance_state["prompt_args"],
            "cost_usd": provenance_state["cost_usd"],
            "token_in": provenance_state["token_in"],
            "token_out": provenance_state["token_out"],
            "ts": provenance_state["ts"],
            "trace_id": provenance_state["trace_id"],
        }
    return copied


def _consume_schema_utf8(value: str, budget: List[int], path: str) -> None:
    # UTF-8 byte length is at least codepoint count.  Reject on that cheap bound
    # before allocating a bounded encoding for the exact count.
    if len(value) > budget[1]:
        _fail("business_snapshot_schema_authority_mismatch", path)
    invalid = False
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        invalid = True
        encoded = b""
    if invalid or len(encoded) > budget[1]:
        _fail("business_snapshot_schema_authority_mismatch", path)
    budget[1] -= len(encoded)


def _compare_schema_value(
    candidate: Any,
    expected: Any,
    *,
    path: str,
    depth: int,
    active: set[int],
    budget: List[int],
) -> None:
    """Bounded exact JSON comparison against codec-verified Document schema."""
    if budget[0] <= 0:
        _fail("business_snapshot_schema_authority_mismatch", path)
    budget[0] -= 1
    if type(candidate) is not type(expected):
        _fail("business_snapshot_schema_authority_mismatch", path)

    if candidate is None:
        return
    if type(candidate) is bool or type(candidate) is int:
        if candidate != expected:
            _fail("business_snapshot_schema_authority_mismatch", path)
        return
    if type(candidate) is float:
        if candidate != expected or (
            candidate == 0.0
            and math.copysign(1.0, candidate) != math.copysign(1.0, expected)
        ):
            _fail("business_snapshot_schema_authority_mismatch", path)
        return
    if type(candidate) is str:
        if len(candidate) != len(expected):
            _fail("business_snapshot_schema_authority_mismatch", path)
        _consume_schema_utf8(candidate, budget, path)
        if candidate != expected:
            _fail("business_snapshot_schema_authority_mismatch", path)
        return

    if type(candidate) not in (list, dict) or depth >= MAX_NESTING_DEPTH:
        _fail("business_snapshot_schema_authority_mismatch", path)
    identity = id(candidate)
    if identity in active:
        _fail("business_snapshot_schema_authority_mismatch", path)
    active.add(identity)
    try:
        if type(candidate) is list:
            if len(candidate) != len(expected):
                _fail("business_snapshot_schema_authority_mismatch", path)
            for index, (child, expected_child) in enumerate(zip(candidate, expected)):
                _compare_schema_value(
                    child,
                    expected_child,
                    path="%s[%d]" % (path, index),
                    depth=depth + 1,
                    active=active,
                    budget=budget,
                )
            return

        if len(candidate) != len(expected):
            _fail("business_snapshot_schema_authority_mismatch", path)
        for key, child in candidate.items():
            if type(key) is not str or budget[0] <= 0:
                _fail("business_snapshot_schema_authority_mismatch", path)
            budget[0] -= 1
            _consume_schema_utf8(key, budget, path)
            if key not in expected:
                _fail("business_snapshot_schema_authority_mismatch", path)
            _compare_schema_value(
                child,
                expected[key],
                path=path,
                depth=depth + 1,
                active=active,
                budget=budget,
            )
    finally:
        active.remove(identity)


def _require_schema_authority(value: Any, expected: Dict[str, Any], path: str) -> None:
    _compare_schema_value(
        value,
        expected,
        path=path,
        depth=0,
        active=set(),
        # A decoded Document schema already fits this byte ceiling.  Separate
        # node/UTF-8 counters keep a hostile live mirror within the same order
        # of resource budget without first serializing it.
        budget=[MAX_SNAPSHOT_VALUE_NODES, MAX_DOCUMENT_SNAPSHOT_BYTES],
    )


def _capture_entity_row(
    project_id: str,
    document_id: str,
    bucket_type: str,
    bucket_id: str,
    entity: Any,
    document_owner: Document,
    expected_schema: Dict[str, Any],
    expects_framework_validator: bool,
    collection_position: int,
    path: str,
) -> Tuple[EntitySnapshotRow | None, EntityKey | None]:
    if type(entity) is not Entity:
        _fail("business_snapshot_domain_type_invalid", path)
    state = _require_domain_state_inventory(
        object.__getattribute__(entity, "__dict__"),
        _ENTITY_DOMAIN_STATE_KEYS,
        path,
    )
    if state["_document_owner"] is not document_owner:
        _fail("business_snapshot_document_owner_mismatch", path + ".document_owner")
    admission_state = state["_document_admission_state"]
    if (
        type(admission_state) is not tuple
        or len(admission_state) != 2
        or type(admission_state[0]) is not dict
        or not callable(admission_state[1])
    ):
        _fail("business_snapshot_domain_state_invalid", path + ".document_admission_state")

    entity_type = state.get("_type")
    entity_id = state.get("_id")
    _require_string_shape(entity_type, path + ".entity_type", nonempty=True)
    _require_string_shape(entity_id, path + ".entity_id", nonempty=True)
    if entity_type == "__document__":
        _fail("business_snapshot_entity_type_invalid", path + ".entity_type")
    if entity_type != bucket_type or entity_id != bucket_id:
        _fail("business_snapshot_collection_identity_invalid", path)

    read_only = state.get("_read_only")
    if type(read_only) is not bool:
        _fail("business_snapshot_domain_state_invalid", path + ".read_only")
    if read_only:
        return None, EntityKey(entity_type=entity_type, entity_id=entity_id)

    _require_schema_authority(state["_schema"], expected_schema, path + ".schema")
    validator = state["_validator"]
    if expects_framework_validator:
        if not is_framework_validator(validator, expected_schema):
            _fail(
                "business_snapshot_validator_authority_mismatch",
                path + ".validator",
            )
    elif validator is not _no_op_validator:
        _fail(
            "business_snapshot_validator_authority_mismatch",
            path + ".validator",
        )

    provenance = _copy_provenance_for_capture(
        state.get("_provenance"), path + ".provenance"
    )
    actor_log = state["_actor_log"]
    envelope = {
        "schema": ENTITY_SCHEMA,
        "identity": {
            "project_id": project_id,
            "document_id": document_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
        },
        "state": {
            "entity_version": state.get("_version"),
            "collection_position": collection_position,
            "deleted": state.get("_deleted"),
            "name": state.get("_name"),
            "data": state.get("_data"),
            "provenance": provenance,
            "actor_log": actor_log,
            "created_at": state.get("_created_at"),
            "updated_at": state.get("_updated_at"),
        },
    }
    payload, frozen, digest = _encode_envelope(
        envelope, path, MAX_ENTITY_SNAPSHOT_BYTES
    )
    identity = frozen["identity"]
    frozen_state = frozen["state"]
    row = EntitySnapshotRow(
        project_id=identity["project_id"],
        document_id=identity["document_id"],
        entity_type=identity["entity_type"],
        entity_id=identity["entity_id"],
        entity_version=frozen_state["entity_version"],
        deleted=1 if frozen_state["deleted"] is True else 0,
        codec_id=CODEC_ID,
        codec_version=CODEC_VERSION,
        snapshot_format_version=SNAPSHOT_FORMAT_VERSION,
        snapshot_bytes=payload,
        snapshot_sha256=digest,
    )
    return row, None


def capture_business_snapshot(
    project_id: str,
    generation: int,
    document: Document,
) -> CaptureResult:
    """Project one already-locked live ``Document`` into canonical rows.

    The function intentionally acquires no lock of its own.  The caller must
    hold the same lifecycle/commit lock used for Document mutation so all rows
    describe one business head.
    """
    project_id = _require_project_id(project_id, "$.project_id")
    generation = _require_sqlite_positive_integer(generation, "$.generation")
    if type(document) is not Document:
        _fail("business_snapshot_domain_type_invalid", "$.document")
    document_state = _require_domain_state_inventory(
        object.__getattribute__(document, "__dict__"),
        _DOCUMENT_DOMAIN_STATE_KEYS,
        "$.document",
    )

    document_id, _document_id_bytes = _require_bounded_utf8_string(
        document_state["_id"],
        "$.document.document_id",
        MAX_DOCUMENT_SNAPSHOT_BYTES,
    )
    document_envelope = {
        "schema": DOCUMENT_SCHEMA,
        "identity": {
            "project_id": project_id,
            "document_id": document_id,
        },
        "state": {
            "document_version": document_state.get("_v"),
            "schema_version": document_state.get("_schema_version"),
            "collaboration_mode": document_state.get("_collaboration_mode"),
            "save_policy": document_state.get("_save_policy"),
            "name": document_state.get("_name"),
            "data_schema": document_state.get("_schema"),
            "config": document_state.get("_config"),
            "acl": document_state.get("_acl"),
            "created_at": document_state.get("_created_at"),
            "updated_at": document_state.get("_updated_at"),
        },
    }
    document_payload, frozen_document, document_digest = _encode_envelope(
        document_envelope, "$.document", MAX_DOCUMENT_SNAPSHOT_BYTES
    )
    document_identity = frozen_document["identity"]
    frozen_document_state = frozen_document["state"]
    document_row = DocumentSnapshotRow(
        project_id=document_identity["project_id"],
        document_id=document_identity["document_id"],
        document_version=frozen_document_state["document_version"],
        schema_version=frozen_document_state["schema_version"],
        collaboration_mode=frozen_document_state["collaboration_mode"],
        save_policy=frozen_document_state["save_policy"],
        codec_id=CODEC_ID,
        codec_version=CODEC_VERSION,
        snapshot_format_version=SNAPSHOT_FORMAT_VERSION,
        snapshot_bytes=document_payload,
        snapshot_sha256=document_digest,
    )
    # Establish Document schema authority before reading any Entity schema.
    # This also makes malformed live metadata fail before a large topology walk.
    _verified_document_row, verified_document_payload = _verify_document_row(
        document_row
    )
    authority_data_schema = verified_document_payload["state"]["data_schema"]

    collections = document_state["_collections"]
    if type(collections) is not defaultdict or collections.default_factory is not dict:
        _fail("business_snapshot_domain_state_invalid", "$.document.collections")
    if len(collections) > MAX_ENTITY_COUNT:
        _fail("business_snapshot_entity_count_exceeded", "$.document.collections")
    entity_rows: List[EntitySnapshotRow] = []
    excluded: List[EntityKey] = []
    scan_entry_count = 0
    payload_bytes = len(document_payload)
    scan_identity_bytes = 0
    for bucket_index, (bucket_type, bucket) in enumerate(collections.items()):
        bucket_path = "$.document.collections[%d]" % bucket_index
        bucket_type, bucket_type_bytes = _require_bounded_utf8_string(
            bucket_type,
            bucket_path + ".entity_type",
            MAX_ENTITY_SNAPSHOT_BYTES,
        )
        if bucket_type == "__document__":
            _fail("business_snapshot_entity_type_invalid", bucket_path + ".entity_type")
        if type(bucket) is not dict:
            _fail("business_snapshot_domain_state_invalid", bucket_path)
        if not bucket:
            scan_identity_bytes += bucket_type_bytes
            if scan_identity_bytes > MAX_TOTAL_SNAPSHOT_BYTES:
                _fail(
                    "business_snapshot_total_size_exceeded",
                    "$.document.collections",
                )
        if scan_entry_count + len(bucket) > MAX_ENTITY_COUNT:
            _fail("business_snapshot_entity_count_exceeded", "$.document.collections")
        collection_position = 0
        for entity_index, (bucket_id, entity) in enumerate(bucket.items()):
            entity_path = "%s.entities[%d]" % (bucket_path, entity_index)
            scan_entry_count += 1
            if scan_entry_count > MAX_ENTITY_COUNT:
                _fail("business_snapshot_entity_count_exceeded", "$.document.collections")
            bucket_id, bucket_id_bytes = _require_bounded_utf8_string(
                bucket_id,
                entity_path + ".entity_id",
                MAX_ENTITY_SNAPSHOT_BYTES,
            )
            # Count every materialized identity, including read-only overlays;
            # otherwise an Entity-empty canonical set could still allocate an
            # unbounded diagnostics tuple.
            scan_identity_bytes += bucket_type_bytes + bucket_id_bytes
            if scan_identity_bytes > MAX_TOTAL_SNAPSHOT_BYTES:
                _fail("business_snapshot_total_size_exceeded", "$.document.collections")

            if bucket_type in authority_data_schema:
                expected_schema = authority_data_schema[bucket_type]
                expects_framework_validator = bool(expected_schema)
            else:
                expected_schema = {}
                expects_framework_validator = False
            row, diagnostic = _capture_entity_row(
                project_id,
                document_id,
                bucket_type,
                bucket_id,
                entity,
                document,
                expected_schema,
                expects_framework_validator,
                collection_position,
                entity_path,
            )
            if row is not None:
                entity_rows.append(row)
                collection_position += 1
                payload_bytes += len(row.snapshot_bytes)
                if payload_bytes > MAX_TOTAL_SNAPSHOT_BYTES:
                    _fail("business_snapshot_total_size_exceeded", "$.entities")
            if diagnostic is not None:
                excluded.append(diagnostic)

    if len(entity_rows) > MAX_ENTITY_COUNT:
        _fail("business_snapshot_entity_count_exceeded", "$.entities")
    entity_rows.sort(key=lambda row: (row.entity_type, row.entity_id))
    excluded.sort(key=lambda key: (key.entity_type, key.entity_id))
    canonical_entities = tuple(entity_rows)

    project_envelope = {
        "schema": PROJECT_SCHEMA,
        "identity": {"project_id": project_id},
        "state": {
            "generation": generation,
            "document": _project_document_descriptor(document_row),
            "entities": {
                "count": len(canonical_entities),
                "set_sha256": _entity_set_digest(canonical_entities),
            },
        },
    }
    project_payload, frozen_project, project_digest = _encode_envelope(
        project_envelope, "$.project", MAX_PROJECT_SNAPSHOT_BYTES
    )
    payload_bytes += len(project_payload)
    if payload_bytes > MAX_TOTAL_SNAPSHOT_BYTES:
        _fail("business_snapshot_total_size_exceeded", "$")
    project_row = ProjectSnapshotRow(
        project_id=frozen_project["identity"]["project_id"],
        generation=frozen_project["state"]["generation"],
        codec_id=CODEC_ID,
        codec_version=CODEC_VERSION,
        snapshot_format_version=SNAPSHOT_FORMAT_VERSION,
        snapshot_bytes=project_payload,
        snapshot_sha256=project_digest,
    )
    snapshot = BusinessSnapshotSet(
        project=project_row,
        document=document_row,
        entities=canonical_entities,
    )
    # Capture and persisted-row verification deliberately share one semantic
    # contract; live-object corruption cannot bypass checks merely because it
    # originated in memory rather than a database.
    verify_business_snapshot(snapshot)
    return CaptureResult(snapshot=snapshot, excluded_read_only=tuple(excluded))


def _verify_project_row(row: Any) -> Tuple[ProjectSnapshotRow, Dict[str, Any]]:
    path = "$.project"
    if type(row) is not ProjectSnapshotRow:
        _fail("business_snapshot_row_type_invalid", path)
    _verify_codec_columns(row, path)
    decoded = _verify_and_decode_row_payload(
        row.snapshot_bytes,
        row.snapshot_sha256,
        path,
        MAX_PROJECT_SNAPSHOT_BYTES,
    )
    identity, state = _verify_outer_envelope(
        decoded,
        PROJECT_SCHEMA,
        _PROJECT_IDENTITY_KEYS,
        _PROJECT_STATE_KEYS,
        path,
    )
    payload_project_id = _require_project_id(
        identity["project_id"], path + ".identity.project_id"
    )
    payload_generation = _require_sqlite_positive_integer(
        state["generation"], path + ".state.generation"
    )

    # As for Document/Entity rows, canonical payload validation owns the
    # bounded UTF-8 work.  The passive SQL-side scalar is inspected only after
    # that boundary and then bound by exact equality.
    _require_string_shape(row.project_id, path + ".project_id", nonempty=True)
    _require_sqlite_positive_integer(row.generation, path + ".generation")
    if (
        payload_project_id != row.project_id
        or payload_generation != row.generation
    ):
        _fail("business_snapshot_column_mismatch", path)

    document = _require_exact_dict(state["document"], path + ".state.document")
    _require_exact_keys(
        document, _PROJECT_DOCUMENT_DESCRIPTOR_KEYS, path + ".state.document"
    )
    _require_utf8_string(
        document["document_id"], path + ".state.document.document_id", nonempty=True
    )
    _require_sqlite_positive_integer(
        document["document_version"], path + ".state.document.document_version"
    )
    if type(document["codec_id"]) is not str or document["codec_id"] != CODEC_ID:
        _fail("business_snapshot_codec_metadata_invalid", path + ".state.document")
    if type(document["codec_version"]) is not int or document["codec_version"] != CODEC_VERSION:
        _fail("business_snapshot_codec_metadata_invalid", path + ".state.document")
    if (
        type(document["snapshot_format_version"]) is not int
        or document["snapshot_format_version"] != SNAPSHOT_FORMAT_VERSION
    ):
        _fail("business_snapshot_codec_metadata_invalid", path + ".state.document")
    _require_lower_hex_digest(
        document["snapshot_sha256"], path + ".state.document.snapshot_sha256"
    )

    entities = _require_exact_dict(state["entities"], path + ".state.entities")
    _require_exact_keys(entities, _PROJECT_ENTITY_SET_KEYS, path + ".state.entities")
    _require_entity_count(entities["count"], path + ".state.entities.count")
    _require_lower_hex_digest(
        entities["set_sha256"], path + ".state.entities.set_sha256"
    )
    return row, decoded


def _verify_document_row(row: Any) -> Tuple[DocumentSnapshotRow, Dict[str, Any]]:
    path = "$.document"
    if type(row) is not DocumentSnapshotRow:
        _fail("business_snapshot_row_type_invalid", path)
    _verify_codec_columns(row, path)
    decoded = _verify_and_decode_row_payload(
        row.snapshot_bytes,
        row.snapshot_sha256,
        path,
        MAX_DOCUMENT_SNAPSHOT_BYTES,
    )
    identity, state = _verify_outer_envelope(
        decoded,
        DOCUMENT_SCHEMA,
        _DOCUMENT_IDENTITY_KEYS,
        _DOCUMENT_STATE_KEYS,
        path,
    )
    _require_project_id(identity["project_id"], path + ".identity.project_id")
    _require_utf8_string(
        identity["document_id"], path + ".identity.document_id", nonempty=True
    )
    version = _require_sqlite_positive_integer(
        state["document_version"], path + ".state.document_version"
    )
    schema_version = _require_utf8_string(
        state["schema_version"], path + ".state.schema_version", nonempty=True
    )
    collaboration_mode = _require_utf8_string(
        state["collaboration_mode"], path + ".state.collaboration_mode"
    )
    if collaboration_mode not in _VALID_COLLABORATION_MODES:
        _fail("business_snapshot_collaboration_mode_invalid", path + ".state.collaboration_mode")
    save_policy = state["save_policy"]
    if save_policy is not None:
        save_policy = _require_utf8_string(save_policy, path + ".state.save_policy")
        if save_policy not in _VALID_SAVE_POLICIES:
            _fail("business_snapshot_save_policy_invalid", path + ".state.save_policy")
    _require_utf8_string(state["name"], path + ".state.name")
    data_schema = _verify_data_schema(
        state["data_schema"], path + ".state.data_schema"
    )
    _verify_domain_json_value(data_schema, path + ".state.data_schema")
    config = _require_exact_dict(state["config"], path + ".state.config")
    _verify_domain_json_value(config, path + ".state.config")
    acl = _verify_acl(state["acl"], path + ".state.acl")
    _verify_domain_json_value(acl, path + ".state.acl")
    _verify_timestamps(state, path + ".state")

    # Only now inspect unbounded row-side text.  Exact type/emptiness checks do
    # not allocate UTF-8 copies; equality with the already-validated payload
    # transfers its UTF-8 and value-domain facts to the SQL columns.
    _require_project_id(row.project_id, path + ".project_id")
    _require_string_shape(row.document_id, path + ".document_id", nonempty=True)
    _require_sqlite_positive_integer(row.document_version, path + ".document_version")
    _require_string_shape(row.schema_version, path + ".schema_version", nonempty=True)
    mode = _require_string_shape(row.collaboration_mode, path + ".collaboration_mode")
    if mode not in _VALID_COLLABORATION_MODES:
        _fail("business_snapshot_collaboration_mode_invalid", path + ".collaboration_mode")
    if row.save_policy is not None:
        policy = _require_string_shape(row.save_policy, path + ".save_policy")
        if policy not in _VALID_SAVE_POLICIES:
            _fail("business_snapshot_save_policy_invalid", path + ".save_policy")
    if (
        identity["project_id"] != row.project_id
        or identity["document_id"] != row.document_id
        or version != row.document_version
        or schema_version != row.schema_version
        or collaboration_mode != row.collaboration_mode
        or save_policy != row.save_policy
    ):
        _fail("business_snapshot_column_mismatch", path)
    return row, decoded


def _verify_entity_row(row: Any, index: int) -> Tuple[EntitySnapshotRow, Dict[str, Any]]:
    path = "$.entities[%d]" % index
    if type(row) is not EntitySnapshotRow:
        _fail("business_snapshot_row_type_invalid", path)
    _verify_codec_columns(row, path)
    decoded = _verify_and_decode_row_payload(
        row.snapshot_bytes,
        row.snapshot_sha256,
        path,
        MAX_ENTITY_SNAPSHOT_BYTES,
    )
    identity, state = _verify_outer_envelope(
        decoded,
        ENTITY_SCHEMA,
        _ENTITY_IDENTITY_KEYS,
        _ENTITY_STATE_KEYS,
        path,
    )
    _require_project_id(identity["project_id"], path + ".identity.project_id")
    _require_utf8_string(
        identity["document_id"], path + ".identity.document_id", nonempty=True
    )
    payload_entity_type = _require_utf8_string(
        identity["entity_type"], path + ".identity.entity_type", nonempty=True
    )
    if payload_entity_type == "__document__":
        _fail("business_snapshot_entity_type_invalid", path + ".identity.entity_type")
    _require_utf8_string(
        identity["entity_id"], path + ".identity.entity_id", nonempty=True
    )
    version = _require_sqlite_positive_integer(
        state["entity_version"], path + ".state.entity_version"
    )
    position = state["collection_position"]
    if (
        type(position) is not int
        or position < 0
        or position >= MAX_ENTITY_COUNT
    ):
        _fail(
            "business_snapshot_collection_position_invalid",
            path + ".state.collection_position",
        )
    if type(state["deleted"]) is not bool:
        _fail("business_snapshot_deleted_invalid", path + ".state.deleted")
    _require_utf8_string(state["name"], path + ".state.name")
    data = _require_exact_dict(state["data"], path + ".state.data")
    _verify_domain_json_value(data, path + ".state.data")
    _verify_provenance(state["provenance"], path + ".state.provenance")
    _verify_actor_log(state["actor_log"], version, path + ".state.actor_log")
    _verify_timestamps(state, path + ".state")

    _require_project_id(row.project_id, path + ".project_id")
    entity_type = _require_string_shape(
        row.entity_type, path + ".entity_type", nonempty=True
    )
    if entity_type == "__document__":
        _fail("business_snapshot_entity_type_invalid", path + ".entity_type")
    _require_string_shape(row.document_id, path + ".document_id", nonempty=True)
    _require_string_shape(row.entity_id, path + ".entity_id", nonempty=True)
    _require_sqlite_positive_integer(row.entity_version, path + ".entity_version")
    if type(row.deleted) is not int or row.deleted not in (0, 1):
        _fail("business_snapshot_deleted_invalid", path + ".deleted")
    if (
        identity["project_id"] != row.project_id
        or identity["document_id"] != row.document_id
        or identity["entity_type"] != row.entity_type
        or identity["entity_id"] != row.entity_id
        or version != row.entity_version
        or int(state["deleted"]) != row.deleted
    ):
        _fail("business_snapshot_column_mismatch", path)
    return row, decoded


def verify_business_snapshot(snapshot: BusinessSnapshotSet) -> BusinessSnapshotSet:
    """Validate canonical bytes, SQL columns, topology, ordering and root binding."""
    if type(snapshot) is not BusinessSnapshotSet:
        _fail("business_snapshot_type_invalid", "$")
    snapshot_state = object.__getattribute__(snapshot, "__dict__")
    if type(snapshot_state) is not dict:
        _fail("business_snapshot_shape_invalid", "$")
    _require_exact_keys(snapshot_state, _SNAPSHOT_SET_KEYS, "$")
    if type(snapshot.entities) is not tuple:
        _fail("business_snapshot_tuple_type_invalid", "$.entities")
    if len(snapshot.entities) > MAX_ENTITY_COUNT:
        _fail("business_snapshot_entity_count_exceeded", "$.entities")

    # Complete the aggregate resource preflight before decoding the first row.
    # Without this pass, a hostile set could force nearly 1 GiB of repeated JSON
    # work before the running total finally crossed its declared ceiling.
    total_bytes = _preflight_passive_row(
        snapshot.project,
        ProjectSnapshotRow,
        _PROJECT_ROW_KEYS,
        "$.project",
        MAX_PROJECT_SNAPSHOT_BYTES,
    )
    total_bytes += _preflight_passive_row(
        snapshot.document,
        DocumentSnapshotRow,
        _DOCUMENT_ROW_KEYS,
        "$.document",
        MAX_DOCUMENT_SNAPSHOT_BYTES,
    )
    if total_bytes > MAX_TOTAL_SNAPSHOT_BYTES:
        _fail("business_snapshot_total_size_exceeded", "$")
    project_row, project_payload = _verify_project_row(snapshot.project)
    document_row, _document_payload = _verify_document_row(snapshot.document)

    # Reject O(1) aggregate/topology contradictions before interpreting any
    # Entity payload.  The set hash still waits until every leaf is trusted.
    if document_row.project_id != project_row.project_id:
        _fail("business_snapshot_topology_mismatch", "$.document.project_id")
    project_state = project_payload["state"]
    if project_state["document"] != _project_document_descriptor(document_row):
        _fail("business_snapshot_root_mismatch", "$.project.state.document")
    if project_state["entities"]["count"] != len(snapshot.entities):
        _fail("business_snapshot_root_mismatch", "$.project.state.entities")

    # Only a root-consistent tuple earns an O(n) Entity carrier/aggregate scan.
    # Finish that resource preflight before decoding the first Entity payload.
    for index, row in enumerate(snapshot.entities):
        total_bytes += _preflight_passive_row(
            row,
            EntitySnapshotRow,
            _ENTITY_ROW_KEYS,
            "$.entities[%d]" % index,
            MAX_ENTITY_SNAPSHOT_BYTES,
        )
        if total_bytes > MAX_TOTAL_SNAPSHOT_BYTES:
            _fail("business_snapshot_total_size_exceeded", "$.entities")

    previous_key: Tuple[str, str] | None = None
    position_type: str | None = None
    positions: set[int] = set()
    for index, candidate in enumerate(snapshot.entities):
        row, entity_payload = _verify_entity_row(candidate, index)
        key = (row.entity_type, row.entity_id)
        if previous_key is not None and key == previous_key:
            _fail("business_snapshot_entity_duplicate", "$.entities")
        if previous_key is not None and key < previous_key:
            _fail("business_snapshot_entity_order_invalid", "$.entities")
        previous_key = key
        if (
            row.project_id != project_row.project_id
            or row.document_id != document_row.document_id
        ):
            _fail("business_snapshot_topology_mismatch", "$.entities[%d]" % index)
        if position_type != row.entity_type:
            if positions and (
                min(positions) != 0 or max(positions) != len(positions) - 1
            ):
                _fail("business_snapshot_collection_position_invalid", "$.entities")
            position_type = row.entity_type
            positions = set()
        position = entity_payload["state"]["collection_position"]
        if position in positions:
            _fail("business_snapshot_collection_position_invalid", "$.entities")
        positions.add(position)

    if positions and (
        min(positions) != 0 or max(positions) != len(positions) - 1
    ):
        _fail("business_snapshot_collection_position_invalid", "$.entities")

    if (
        project_state["entities"]["set_sha256"]
        != _entity_set_digest(snapshot.entities)
    ):
        _fail("business_snapshot_root_mismatch", "$.project.state.entities")
    return snapshot


def _restore_provenance(value: Dict[str, Any]) -> Dict[str, AiProvenance]:
    restored: Dict[str, AiProvenance] = {}
    for field_name, item in value.items():
        restored[field_name] = AiProvenance(
            actor=item["actor"],
            model=item["model"],
            model_version=item["model_version"],
            prompt_template_id=item["prompt_template_id"],
            prompt_args=item["prompt_args"],
            cost_usd=item["cost_usd"],
            token_in=item["token_in"],
            token_out=item["token_out"],
            ts=item["ts"],
            trace_id=item["trace_id"],
        )
    return restored


def _restore_actor_log(value: List[Any]) -> List[Dict[str, Any]]:
    return [
        {
            "version": event["version"],
            "actor": {
                "user_id": event["actor"]["user_id"],
                "device_id": event["actor"]["device_id"],
                "source": event["actor"]["source"],
            },
            "ts": event["ts"],
            "fields": list(event["fields"]),
        }
        for event in value
    ]


def restore_business_snapshot(snapshot: BusinessSnapshotSet) -> RestoredBusinessHead:
    """Rebuild a detached business head, then prove a byte-exact recapture.

    Full verification happens before the first domain object is constructed.
    Restoration intentionally bypasses ``Document.open``, ``Entity.from_dict``
    and ``Document.add``: those entry points respectively interpret legacy
    files or perform new-business-mutation semantics.
    """
    verify_business_snapshot(snapshot)

    # Rows are now trusted canonical payloads.  Decode them again rather than
    # retaining a hidden verifier cache; the returned graph must own its values.
    document_payload = _decode_row_payload(
        snapshot.document.snapshot_bytes,
        "$.document.snapshot_bytes",
        MAX_DOCUMENT_SNAPSHOT_BYTES,
    )
    document_state = document_payload["state"]
    restore_failed = False
    try:
        document = Document(
            schema_version=document_state["schema_version"],
            collaboration_mode=document_state["collaboration_mode"],
            name=document_state["name"],
            id=document_payload["identity"]["document_id"],
            schema=document_state["data_schema"],
            config=document_state["config"],
            acl=document_state["acl"],
            save_policy=document_state["save_policy"],
        )
        document._v = document_state["document_version"]
        document._created_at = document_state["created_at"]
        document._updated_at = document_state["updated_at"]

        restoration_entries = []
        for index, row in enumerate(snapshot.entities):
            payload = _decode_row_payload(
                row.snapshot_bytes,
                "$.entities[%d].snapshot_bytes" % index,
                MAX_ENTITY_SNAPSHOT_BYTES,
            )
            restoration_entries.append(
                (
                    row.entity_type,
                    payload["state"]["collection_position"],
                    index,
                    row,
                )
            )
        restoration_entries.sort(key=lambda item: (item[0], item[1]))

        for _entity_type, _collection_position, index, row in restoration_entries:
            payload = _decode_row_payload(
                row.snapshot_bytes,
                "$.entities[%d].snapshot_bytes" % index,
                MAX_ENTITY_SNAPSHOT_BYTES,
            )
            identity = payload["identity"]
            state = payload["state"]
            entity = Entity(
                type=identity["entity_type"],
                id=identity["entity_id"],
                name=state["name"],
                data=state["data"],
                read_only=False,
            )
            entity._version = state["entity_version"]
            entity._deleted = state["deleted"]
            entity._read_only = False
            entity._provenance = _restore_provenance(state["provenance"])
            entity._actor_log = _restore_actor_log(state["actor_log"])
            entity._created_at = state["created_at"]
            entity._updated_at = state["updated_at"]

            document._add_entity_internal(entity)
    except BusinessSnapshotError:
        raise
    except Exception:
        restore_failed = True
    if restore_failed:
        # Domain exceptions may embed historical values in their messages and
        # traceback locals.  Raise outside the handler so the public error does
        # not retain them through ``__context__``.
        raise BusinessSnapshotError(
            "business_snapshot_restore_failed", "$.restore"
        )

    recaptured = capture_business_snapshot(
        snapshot.project.project_id,
        snapshot.project.generation,
        document,
    )
    if recaptured.excluded_read_only or recaptured.snapshot != snapshot:
        _fail("business_snapshot_restore_mismatch", "$.restore")
    restored = object.__new__(RestoredBusinessHead)
    object.__setattr__(
        restored, "project_id", snapshot.project.project_id,
    )
    object.__setattr__(
        restored, "generation", snapshot.project.generation,
    )
    object.__setattr__(restored, "document", document)
    object.__setattr__(restored, "requires_runtime_rehydration", True)
    _issue_restored_business_head(restored, snapshot)
    return restored


__all__ = [
    "BusinessSnapshotError",
    "BusinessSnapshotSet",
    "CaptureResult",
    "DOCUMENT_SCHEMA",
    "DocumentSnapshotRow",
    "ENTITY_SCHEMA",
    "ENTITY_SET_HASH_DOMAIN",
    "EntityKey",
    "EntitySnapshotRow",
    "MAX_DOCUMENT_SNAPSHOT_BYTES",
    "MAX_ENTITY_COUNT",
    "MAX_ENTITY_SNAPSHOT_BYTES",
    "MAX_PROJECT_SNAPSHOT_BYTES",
    "MAX_SQLITE_INTEGER",
    "MAX_SNAPSHOT_VALUE_NODES",
    "MAX_TOTAL_SNAPSHOT_BYTES",
    "PROJECT_SCHEMA",
    "ProjectSnapshotRow",
    "RestoredBusinessHead",
    "SNAPSHOT_FORMAT_VERSION",
    "capture_business_snapshot",
    "restore_business_snapshot",
    "verify_business_snapshot",
]
