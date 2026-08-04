"""Document — skill 的数据根.

完整 SPEC 见同目录 SPEC.md.

已实装:
  - 构造 + 必填校验 (schema_version / collaboration_mode) + 可选 save_policy / runtime mutation policies
  - 元属性 property (含 acl / version)
  - acl 字段 (document 级 ACL 占位 dict)
  - _v 文档版本号 (初始 1, 每次 save +1)
  - save(path, expected_version=None) — v2 identity hash generation + document commit point + 409
  - Document.open(path) — SH flock + 同一 dirfd 安全读 v2 / strict-component legacy
  - register_read_only_source(spec) — 调 loader, 灌入 read_only entity
  - read_only_sources() 列名
  - 8 个 entity 方法: add/get/find/list/types/hard_remove/detect_cycles/cascade_delete
  - 完整内存 mutation snapshot / restore / one-shot session（提交失败精确回滚）
  - 类型级 policy 声明、真实 graph delta/cardinality 与受保护 CRUD authority
  - DocumentRegistry — declare / build_document 灌声明；完整 command registry 校验后单向 seal

模块边界:
  - mutation_policy_declaration.py 只拥有 passive policy 语法
  - mutation_policy_graph.py 只拥有 Document graph snapshot/delta/authorization
  - document_mutation_session.py 只拥有 session/candidate/coverage/rollback 生命周期
  - document.py 组合并兼容 re-export，不向上 import Command / Operation

待补 (真有跨叶子依赖):
  - .recovery/wc-<session>/ 工作副本  [跟 checkpoint_commit 行为绑定, 等 02-data-flow]
  - SQLite/Postgres 多进程 cardinality 唯一约束 [02-data-flow/07-persistence]
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
import uuid
import weakref
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

# 让 02-entity / 04-field-types 兄弟目录可 import (v3 平铺布局, 暂用 sys.path 注入)
_ENTITY_DIR = Path(__file__).resolve().parent.parent / "02-entity"
_FIELD_TYPES_DIR = Path(__file__).resolve().parent.parent / "04-field-types"
_PROTOCOL_DIR = Path(__file__).resolve().parent.parent.parent / "_protocol"
for _d in (_ENTITY_DIR, _FIELD_TYPES_DIR, _PROTOCOL_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from entity import (  # noqa: E402
    Actor,
    AiProvenance,
    Entity,
    Validator,
    ZeError,
    _DOCUMENT_AUTHORITY_CAPABILITY,
    _clone_json_value,
    _no_op_validator,
    _require_json_string,
)
from field_types import (  # noqa: E402
    canonicalize_entity_schema,
    is_framework_validator,
    make_validator,
)
from mutation_patch import (  # noqa: E402
    DOCUMENT_TARGET,
    ENTITY_META_FIELD,
    Patch,
)
from entity_file_store import (  # noqa: E402
    MAX_ENTITY_FILE_BYTES,
    MAX_DOCUMENT_VERSION,
    EntityFileStore,
    EntityFileStoreError,
    entity_identity_digest,
    entity_storage_marker,
    is_entity_storage_marker,
    require_legacy_component,
    require_legacy_type_component,
)
from mutation_policy_declaration import (  # noqa: E402
    own_document_mutation_policies as _own_document_mutation_policies,
    own_entity_mutation_policy as _own_entity_mutation_policy,
)
from mutation_policy_graph import (  # noqa: E402
    MutationPolicyGraphMixin,
    DocumentMutationSnapshot,
    _AdmittedEntityMutationState,
    _BucketMutationState,
    _EntityMutationState,
    _ProtectedMutationFingerprint,
    _apply_history_patch_provenance,
    _copy_entity_state,
    _copy_entity_state_mapping,
    _document_matches_mutation_snapshot,
    _history_replay_ancestry_data,
    _json_values_exact,
    _mutation_audit_delete_path,
    _mutation_fingerprint_bytes,
    _protected_fingerprints_match,
    _protected_mutation_fingerprint,
    _restore_entity_state,
)
from document_mutation_session import (  # noqa: E402
    DocumentMutationSession,
    _ATOMIC_DOCUMENT_COMMIT_CAPABILITY,
    _HISTORY_REPLAY_CAPABILITY,
    _DocumentCommitCandidate,
    _DocumentCommitCandidateRecord,
    _VerifiedDocumentCommitCandidate,
    _document_has_exclusive_mutation_session,
    _guard_entity_not_reserved_by_foreign_session,
    _active_history_operation_binding,
    _history_operation_scope,
    _history_replay_scope,
    _history_replay_scope_active,
    _mutation_session_stacks,
    _record_history_policy_coverage,
    _record_session_entity_admission,
    _require_active_history_policy_coverage,
    _reset_mutation_state_after_fork,
    _verify_atomic_document_commit_candidate,
)


# A schema field may opt into replay-time validation when a later deployment
# tightens a durable invariant.  The marker is passive schema data: ordinary
# types retain the historical "replay exactly, validate future edits" rule,
# while opted-in types cannot have an obsolete undo/redo resurrect a value the
# current product can no longer consume.
HISTORY_REPLAY_SCHEMA_GUARD = "x-ze-history-replay-guard"

_VALID_COLLABORATION_MODES = ("live_collab", "with_save", "realtime_checkpoint")
_VALID_SAVE_POLICIES = ("auto_commit", "checkpoint_commit", "legacy")
_MAX_SQLITE_INTEGER = 2**63 - 1
_MAX_LEGACY_SCAN_RAW_ENTITY_BYTES = 1024 * 1024 * 1024
_MAX_LEGACY_SCAN_ENTITY_COUNT = 1_000_000

# Exact module-owned proof for Document.save durability outcomes.  Persistence
# recognizes only this exact type + seal; arbitrary ZeError/code/context values
# remain UNKNOWN.  Strings avoid importing persistence enums back into domain.
_DOCUMENT_WRITE_OUTCOME_SEAL = object()
_WRITE_NOT_DURABLE = "not_durable"
_WRITE_UNKNOWN = "unknown"

# Exact construction authority for values consumers treat as already validated.
# The seal is checked but never retained on the instance, so an authentic value
# does not hand callers a token for forging a second one.
_DOCUMENT_HEAD_VALUE_SEAL = object()

# Exact authority held only by the topology coordinator's staging-root wrapper.
# This prevents a metadata-only head rewrite from becoming a shortcut around
# live Command -> Coordinator -> OpLog/version semantics.
_TOPOLOGY_STAGING_METADATA_CAPABILITY = object()

# Exact authority held only by the framework's strict legacy scanner.  The
# guarded context exposes one unpublished mutable Document while its source SH
# lock remains held, so ordinary runtime/bootstrap code must not obtain it as a
# shortcut around the snapshot/import authority boundary.
_LEGACY_SCAN_DOCUMENT_CAPABILITY = object()

class DocumentWriteOutcomeError(ZeError):
    """Sealed Document.save failure with a framework-proven durability fact."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        context: dict[str, Any],
        outcome: str,
        document_version: int | None = None,
        _seal: object,
    ) -> None:
        if _seal is not _DOCUMENT_WRITE_OUTCOME_SEAL:
            raise TypeError("write outcome requires Document authority")
        if outcome not in (_WRITE_NOT_DURABLE, _WRITE_UNKNOWN):
            raise ValueError("write outcome is invalid")
        if document_version is not None and (
            type(document_version) is not int or document_version < 1
        ):
            raise ValueError("document_version must be an exact positive integer")
        if outcome == _WRITE_NOT_DURABLE and document_version is not None:
            raise ValueError("a non-durable write cannot report a document version")
        super().__init__(code, message, context=context)
        self._document_write_outcome = outcome
        self._document_write_version = document_version
        self._document_write_outcome_seal = _seal


def proven_document_write_outcome(
    exc: BaseException,
) -> tuple[str, int | None] | None:
    """Return an exact sealed save outcome for the persistence adapter."""
    if (
        type(exc) is not DocumentWriteOutcomeError
        or getattr(exc, "_document_write_outcome_seal", None)
        is not _DOCUMENT_WRITE_OUTCOME_SEAL
    ):
        return None
    outcome = getattr(exc, "_document_write_outcome", None)
    version = getattr(exc, "_document_write_version", None)
    if outcome not in (_WRITE_NOT_DURABLE, _WRITE_UNKNOWN):
        return None
    if version is not None and (type(version) is not int or version < 1):
        return None
    if outcome == _WRITE_NOT_DURABLE and version is not None:
        return None
    return outcome, version


def _proven_save_failure(
    exc: ZeError,
    *,
    outcome: str = _WRITE_NOT_DURABLE,
    document_version: int | None = None,
) -> DocumentWriteOutcomeError:
    context = dict(exc.context)
    context["commit_point_reached"] = outcome == _WRITE_UNKNOWN
    return DocumentWriteOutcomeError(
        exc.code,
        exc.message,
        context=context,
        outcome=outcome,
        document_version=document_version,
        _seal=_DOCUMENT_WRITE_OUTCOME_SEAL,
    )


def _require_history_replay_capability(capability: Any) -> None:
    if capability is not _HISTORY_REPLAY_CAPABILITY:
        raise ZeError(
            "history_replay_forbidden",
            "history replay requires framework authority",
            context={"reason": "history_replay_capability_required"},
        )


def validate_document_acl(
    value: Any,
    *,
    missing: bool = False,
    error_code: str = "validation_failed",
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate the shared document ACL shape without fail-open coercion.

    Missing ACL is retained as the legacy public-document representation.  A
    present value, however, must be an object; ``null``/``[]`` must never be
    collapsed to ``{}``.  The server's authorization model consumes only
    ``owner`` and ``collaborators`` while other framework-specific ACL keys
    remain forward-compatible.
    """
    base_context = {"field": "acl", **dict(context or {})}
    if missing:
        return {}
    if type(value) is not dict:
        raise ZeError(
            error_code,
            "document acl must be an object",
            context={**base_context, "got_type": "unsupported"},
        )

    owner = value.get("owner") if "owner" in value else ""
    if type(owner) is not str:
        raise ZeError(
            error_code,
            "document acl owner must be a string",
            context={**base_context, "member": "owner"},
        )
    collaborators = value.get("collaborators") if "collaborators" in value else []
    if (
        type(collaborators) is not list
        or any(type(item) is not str for item in collaborators)
    ):
        raise ZeError(
            error_code,
            "document acl collaborators must be a list of strings",
            context={**base_context, "member": "collaborators"},
        )
    if len(set(collaborators)) != len(collaborators):
        raise ZeError(
            error_code,
            "document acl collaborators must not contain duplicates",
            context={**base_context, "member": "collaborators"},
        )

    clone_failed = False
    try:
        cloned = _clone_json_value(value)
    except ZeError:
        clone_failed = True
        cloned = None
    if clone_failed or type(cloned) is not dict:
        # Raise outside the clone handler so a rejected nested ACL value is not
        # retained as exception context/traceback locals at this trust boundary.
        raise ZeError(
            error_code,
            "document acl must contain only strict JSON values",
            context={**base_context, "reason": "acl_value_invalid"},
        )
    return cloned


def _storage_error(exc: EntityFileStoreError) -> ZeError:
    """Project one path-free storage diagnostic into the public error model."""
    messages = {
        "document_storage_unsupported": "document storage is unsupported on this filesystem",
        "document_storage_path_unsafe": "document storage contains an unsafe filesystem entry",
        "document_storage_layout_invalid": "document storage layout is invalid",
        "document_storage_mapping_collision": "document storage identity mapping collided",
        "document_lock_unavailable": "document storage lock is unavailable",
        "entity_file_missing": "a referenced entity file is missing",
        "entity_file_identity_mismatch": "an entity file does not match its indexed identity",
        "project_recovery_required": "document storage requires recovery",
        "document_durability_unknown": "document visibility changed but durability is unknown",
        "document_write_failed": "document storage write failed",
    }
    return ZeError(
        exc.code,
        messages.get(exc.code, "document storage failed"),
        context={
            "reason": exc.reason,
            "commit_point_reached": exc.committed,
        },
    )


def _storage_write_error(
    exc: EntityFileStoreError,
    *,
    document_version: int | None = None,
) -> DocumentWriteOutcomeError:
    projected = _storage_error(exc)
    return _proven_save_failure(
        projected,
        outcome=_WRITE_UNKNOWN if exc.committed else _WRITE_NOT_DURABLE,
        document_version=document_version if exc.committed else None,
    )


def _layout_error(reason: str, *, code: str = "document_storage_layout_invalid") -> ZeError:
    return ZeError(
        code,
        "document storage layout is invalid",
        context={"reason": reason},
    )


def _decode_persisted_json(payload: bytes, *, member: str) -> dict[str, Any]:
    """Decode strict JSON without accepting duplicate keys or NaN constants."""

    def preflight(text: str) -> None:
        # Bound the parser's aggregate value graph and nesting before json.loads
        # allocates it.  This lexical pass deliberately counts object keys as
        # nodes, matching the strict Entity JSON ownership budget.
        nodes = 0
        depth = 0
        index = 0
        length = len(text)
        while index < length:
            character = text[index]
            if character in "[{":
                nodes += 1
                depth += 1
                if depth > 120:
                    raise ValueError("json_depth_exceeded")
                index += 1
            elif character in "]}":
                depth -= 1
                index += 1
            elif character == '"':
                nodes += 1
                index += 1
                while index < length:
                    if text[index] == "\\":
                        index += 2
                    elif text[index] == '"':
                        index += 1
                        break
                    else:
                        index += 1
            elif character in "-0123456789":
                nodes += 1
                index += 1
                while index < length and text[index] in "0123456789.eE+-":
                    index += 1
            elif character in "tfn":
                nodes += 1
                index += 1
                while index < length and text[index].isalpha():
                    index += 1
            else:
                index += 1
            if nodes > 250_000 or depth < 0:
                raise ValueError("json_resource_limit_exceeded")
        if depth != 0:
            raise ValueError("json_container_unbalanced")

    def reject_constant(_value: str) -> None:
        raise ValueError("non_finite_number")

    def exact_integer(token: str) -> int:
        digits = token[1:] if token.startswith("-") else token
        if len(digits) > 4300:
            raise ValueError("integer_too_large")
        return int(token)

    def finite_float(token: str) -> float:
        value = float(token)
        if not math.isfinite(value):
            raise ValueError("non_finite_number")
        return value

    def exact_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result

    try:
        text = payload.decode("utf-8", "strict")
        preflight(text)
        value = json.loads(
            text,
            object_pairs_hook=exact_object,
            parse_constant=reject_constant,
            parse_int=exact_integer,
            parse_float=finite_float,
        )
        # Validate every accepted field, including forward-compatible unknown
        # members, so ignored metadata cannot carry non-UTF8 surrogate strings
        # or an aliased/non-strict JSON graph into later code.
        value = _clone_json_value(value)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
        ZeError,
    ):
        raise _layout_error(
            "%s_json_invalid" % member,
            code="project_recovery_required",
        ) from None
    if type(value) is not dict:
        raise _layout_error(
            "%s_json_not_object" % member,
            code="project_recovery_required",
        )
    return value


def _encode_persisted_json(value: dict[str, Any], *, member: str) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise ZeError(
            "validation_failed",
            "document state cannot be encoded as strict JSON",
            context={"reason": "%s_json_invalid" % member},
        ) from None


def _encode_validated_fragment(value: Any, *, member: str) -> bytes:
    """Encode one already-validated metadata member as compact owned bytes.

    ``DocumentMetadata`` is a hot, entity-free view.  Keeping the complete
    ``document.json`` payload there would force every nested getter to parse
    unrelated collections and forward-compatible extensions.  A one-member
    object lets the existing strict decoder own/detach any JSON value while the
    fragment remains immutable and substantially bounded by the requested
    member rather than by the complete head.
    """
    try:
        return json.dumps(
            {"value": value},
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise ZeError(
            "validation_failed",
            "document metadata fragment cannot be encoded as strict JSON",
            context={"reason": "%s_fragment_invalid" % member},
        ) from None


def _decode_validated_fragment(payload: bytes, *, member: str) -> Any:
    """Return a detached value from one module-produced metadata fragment."""
    envelope = _decode_persisted_json(payload, member=member)
    if tuple(envelope) != ("value",):
        raise _layout_error(
            "%s_fragment_invalid" % member,
            code="project_recovery_required",
        )
    return envelope["value"]


def _strict_collections_index(value: Any) -> dict[str, list[str]]:
    def invalid(reason: str) -> ZeError:
        return _layout_error(reason, code="project_recovery_required")

    if type(value) is not dict:
        raise invalid("collections_not_object")
    result: dict[str, list[str]] = {}
    identity_count = 0
    for entity_type, raw_ids in value.items():
        if type(entity_type) is not str or not entity_type or entity_type == "__document__":
            raise invalid("collection_type_invalid")
        try:
            entity_type.encode("utf-8", "strict")
        except UnicodeEncodeError:
            raise invalid("collection_type_invalid") from None
        if type(raw_ids) is not list:
            raise invalid("collection_ids_not_array")
        ids: list[str] = []
        seen: set[str] = set()
        for entity_id in raw_ids:
            if type(entity_id) is not str or not entity_id:
                raise invalid("collection_id_invalid")
            try:
                entity_id.encode("utf-8", "strict")
            except UnicodeEncodeError:
                raise invalid("collection_id_invalid") from None
            if entity_id in seen:
                raise invalid("collection_identity_duplicate")
            seen.add(entity_id)
            ids.append(entity_id)
            identity_count += 1
            if identity_count > 2_000_000:
                raise invalid("collection_identity_limit_exceeded")
        result[entity_type] = ids
    return result


def _persisted_document_version(raw: dict[str, Any]) -> int:
    version = raw.get("_v", 1)
    if type(version) is not int or version < 1 or version > MAX_DOCUMENT_VERSION:
        raise _layout_error("document_version_invalid", code="project_recovery_required")
    return version


def _validate_persisted_read_only_sources(value: Any) -> list[dict[str, Any]]:
    """Validate ignored source declarations instead of making them a side door.

    A loader is executable process state and is deliberately never restored by
    ``Document.open``.  The durable declaration is nevertheless metadata, so it
    must share the same strict acceptance boundary as every field that *is*
    consumed.  Unknown per-source members remain forward-compatible strict JSON.
    """
    if type(value) is not list:
        raise ValueError("document_read_only_sources_invalid")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in value:
        if type(source) is not dict:
            raise ValueError("document_read_only_source_invalid")
        try:
            name = _require_json_string(source["name"], non_empty=True)
            entity_type = _require_json_string(
                source["entity_type"], non_empty=True,
            )
            if "loader_path" in source:
                _require_json_string(source["loader_path"], non_empty=True)
            if "schema" in source and type(source["schema"]) is not dict:
                raise ValueError("document_read_only_source_schema_invalid")
        except (KeyError, ZeError):
            raise ValueError("document_read_only_source_invalid") from None
        if entity_type == "__document__" or name in seen:
            raise ValueError("document_read_only_source_invalid")
        seen.add(name)
        cloned = _clone_json_value(source)
        assert type(cloned) is dict
        result.append(cloned)
    return result


@dataclass(frozen=True, init=False)
class DocumentMetadata:
    """Detached, entity-free view of one validated authoritative head.

    Every scalar is immutable.  Each validated nested member has its own compact
    immutable fragment bytes and is decoded afresh on access, so a caller cannot
    mutate authorization/config/index state by alias and a small ACL lookup does
    not repeatedly parse an unrelated multi-megabyte collections index.  Exact
    complete-head bytes belong to ``DocumentHead`` only.  Construct these values
    only through the module's validated readers.
    """

    id: str
    name: str
    schema: str | None
    schema_version: str
    collaboration_mode: str
    save_policy: str | None
    created_at: float
    updated_at: float
    version: int
    storage_version: str
    _data_schema_fragment: bytes = field(repr=False)
    _config_fragment: bytes = field(repr=False)
    _acl_fragment: bytes = field(repr=False)
    _collections_fragment: bytes = field(repr=False)
    _entity_storage_fragment: bytes = field(repr=False)
    _read_only_sources_fragment: bytes = field(repr=False)

    def __init__(
        self,
        *,
        id: str,
        name: str,
        schema: str | None,
        schema_version: str,
        collaboration_mode: str,
        save_policy: str | None,
        created_at: float,
        updated_at: float,
        version: int,
        storage_version: str,
        _data_schema_fragment: bytes,
        _config_fragment: bytes,
        _acl_fragment: bytes,
        _collections_fragment: bytes,
        _entity_storage_fragment: bytes,
        _read_only_sources_fragment: bytes,
        _seal: object,
    ) -> None:
        if _seal is not _DOCUMENT_HEAD_VALUE_SEAL:
            raise TypeError("DocumentMetadata requires validated head authority")
        fragments = (
            _data_schema_fragment,
            _config_fragment,
            _acl_fragment,
            _collections_fragment,
            _entity_storage_fragment,
            _read_only_sources_fragment,
        )
        if any(type(fragment) is not bytes for fragment in fragments):
            raise TypeError("DocumentMetadata fragments must be exact bytes")
        object.__setattr__(self, "id", id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "schema", schema)
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "collaboration_mode", collaboration_mode)
        object.__setattr__(self, "save_policy", save_policy)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "storage_version", storage_version)
        object.__setattr__(
            self, "_data_schema_fragment", _data_schema_fragment,
        )
        object.__setattr__(self, "_config_fragment", _config_fragment)
        object.__setattr__(self, "_acl_fragment", _acl_fragment)
        object.__setattr__(self, "_collections_fragment", _collections_fragment)
        object.__setattr__(
            self, "_entity_storage_fragment", _entity_storage_fragment,
        )
        object.__setattr__(
            self, "_read_only_sources_fragment", _read_only_sources_fragment,
        )

    @property
    def data_schema(self) -> dict[str, Any]:
        value = _decode_validated_fragment(
            self._data_schema_fragment,
            member="document_data_schema",
        )
        assert type(value) is dict
        return value

    @property
    def config(self) -> dict[str, Any]:
        value = _decode_validated_fragment(
            self._config_fragment,
            member="document_config",
        )
        assert type(value) is dict
        return value

    @property
    def acl(self) -> dict[str, Any]:
        value = _decode_validated_fragment(
            self._acl_fragment,
            member="document_acl",
        )
        assert type(value) is dict
        return value

    @property
    def collections(self) -> dict[str, list[str]]:
        value = _decode_validated_fragment(
            self._collections_fragment,
            member="document_collections",
        )
        assert type(value) is dict
        return value

    @property
    def entity_storage(self) -> dict[str, Any] | None:
        value = _decode_validated_fragment(
            self._entity_storage_fragment,
            member="document_entity_storage",
        )
        assert value is None or type(value) is dict
        return value

    @property
    def read_only_sources(self) -> list[dict[str, Any]]:
        value = _decode_validated_fragment(
            self._read_only_sources_fragment,
            member="document_read_only_sources",
        )
        assert type(value) is list
        return value

    @property
    def effective_save_policy(self) -> str:
        return (
            "checkpoint_commit"
            if self.save_policy == "checkpoint_commit"
            else "auto_commit"
        )


@dataclass(frozen=True, init=False)
class DocumentHead:
    """One parsed head whose exact bytes were read while the context lock held."""

    metadata: DocumentMetadata
    payload: bytes

    def __init__(
        self,
        *,
        metadata: DocumentMetadata,
        payload: bytes,
        _seal: object,
    ) -> None:
        if _seal is not _DOCUMENT_HEAD_VALUE_SEAL:
            raise TypeError("DocumentHead requires validated head authority")
        object.__setattr__(self, "metadata", metadata)
        object.__setattr__(self, "payload", payload)

    @property
    def envelope(self) -> dict[str, Any]:
        """Return a fresh strict-JSON graph, including unknown head fields."""
        return _decode_persisted_json(self.payload, member="document")


@dataclass(frozen=True)
class _ParsedDocumentHead:
    """Internal parsed authority; never yielded across the storage boundary."""

    payload: bytes = field(repr=False)
    envelope: dict[str, Any] = field(repr=False)
    metadata: DocumentMetadata
    collections: dict[str, list[str]] = field(repr=False)
    data_schema: dict[str, Any] = field(repr=False)
    config: dict[str, Any] = field(repr=False)
    acl: dict[str, Any] = field(repr=False)


def _parse_document_head(payload: bytes) -> _ParsedDocumentHead:
    """Strictly parse every durable head field without reading one Entity.

    This is the only semantic parser for ``Document.open``, metadata readers,
    metadata rewrites and durable-head save CAS.  Keeping one truth here prevents
    an authorization/topology reader from accepting a head the full domain
    object would reject (or vice versa).
    """
    raw = _decode_persisted_json(payload, member="document")
    document_version = _persisted_document_version(raw)
    collections = _strict_collections_index(
        raw["collections"] if "collections" in raw else {}
    )
    if "entity_storage" in raw:
        if not is_entity_storage_marker(raw["entity_storage"]):
            raise _layout_error("entity_storage_marker_invalid")
        storage_version = "v2"
        entity_storage_value: dict[str, Any] | None = raw["entity_storage"]
    else:
        storage_version = "legacy"
        entity_storage_value = None
        # The legacy index is also a filesystem mapping.  Metadata-only
        # consumers must reject path/reserved identities at the head boundary,
        # before Entity I/O, or they would accept a head that Document.open
        # rejects later while resolving the same collections index.
        try:
            for entity_type, entity_ids in collections.items():
                require_legacy_type_component(entity_type)
                for entity_id in entity_ids:
                    require_legacy_component(entity_id)
        except EntityFileStoreError as exc:
            raise _storage_error(exc) from None

    try:
        durable_id = _require_json_string(raw["id"], non_empty=True)
    except (KeyError, ZeError):
        # Keep the established recovery diagnosis while making it the shared
        # truth for full opens and metadata-only consumers too.
        raise _layout_error(
            "durable_head_identity_mismatch",
            code="project_recovery_required",
        ) from None

    try:
        schema_marker = raw.get("schema") if "schema" in raw else None
        if "schema" in raw and schema_marker != "ze-document":
            raise ValueError("document_schema_marker_invalid")
        if "save_policy" in raw and raw["save_policy"] is None:
            raise ValueError("document_save_policy_invalid")
        acl = validate_document_acl(
            raw.get("acl"),
            missing="acl" not in raw,
            error_code="project_recovery_required",
        )
        raw_data_schema = raw["data_schema"] if "data_schema" in raw else {}
        durable_schema = _own_document_schema(raw_data_schema)
        if "config" in raw and type(raw["config"]) is not dict:
            raise ValueError("document_config_invalid")
        durable_config = _clone_json_value(raw.get("config", {}))
        assert type(durable_config) is dict
        read_only_sources = _validate_persisted_read_only_sources(
            raw.get("read_only_sources", [])
        )

        prototype = Document(
            schema_version=raw["schema_version"],
            collaboration_mode=raw["collaboration_mode"],
            name=raw.get("name", ""),
            id=durable_id,
            schema=durable_schema,
            config=durable_config,
            acl=acl,
            save_policy=raw.get("save_policy"),
        )
        timestamps: dict[str, float | None] = {}
        for member in ("created_at", "updated_at"):
            if member not in raw:
                timestamps[member] = None
                continue
            value = raw[member]
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("document_timestamp_invalid")
            timestamps[member] = float(value)
        created_at = timestamps["created_at"]
        updated_at = timestamps["updated_at"]
        # Historical heads could omit one or both timestamps.  Preserve their
        # acceptance without injecting wall-clock state into a pure read: one
        # missing endpoint inherits the present endpoint; both missing map to
        # the deterministic legacy epoch pair.
        if created_at is None and updated_at is None:
            created_at = updated_at = 0.0
        elif created_at is None:
            created_at = updated_at
        elif updated_at is None:
            updated_at = created_at
        assert created_at is not None and updated_at is not None
        prototype._created_at = created_at
        prototype._updated_at = updated_at
        if prototype._created_at > prototype._updated_at:
            raise ValueError("document_timestamp_order_invalid")
        prototype._v = document_version
        metadata_fragments = {
            "data_schema": _encode_validated_fragment(
                # Metadata exposes the durable representation, while the full
                # Document consumes the canonicalized schema below.
                raw_data_schema,
                member="document_data_schema",
            ),
            "config": _encode_validated_fragment(
                durable_config,
                member="document_config",
            ),
            "acl": _encode_validated_fragment(
                acl,
                member="document_acl",
            ),
            "collections": _encode_validated_fragment(
                collections,
                member="document_collections",
            ),
            "entity_storage": _encode_validated_fragment(
                entity_storage_value,
                member="document_entity_storage",
            ),
            "read_only_sources": _encode_validated_fragment(
                read_only_sources,
                member="document_read_only_sources",
            ),
        }
    except (KeyError, TypeError, ValueError, ZeError):
        raise _layout_error(
            "document_metadata_invalid",
            code="project_recovery_required",
        ) from None

    metadata = DocumentMetadata(
        id=prototype.id,
        name=prototype.name,
        schema=schema_marker,
        schema_version=prototype.schema_version,
        collaboration_mode=prototype.collaboration_mode,
        save_policy=prototype.save_policy,
        created_at=prototype.created_at,
        updated_at=prototype.updated_at,
        version=document_version,
        storage_version=storage_version,
        _data_schema_fragment=metadata_fragments["data_schema"],
        _config_fragment=metadata_fragments["config"],
        _acl_fragment=metadata_fragments["acl"],
        _collections_fragment=metadata_fragments["collections"],
        _entity_storage_fragment=metadata_fragments["entity_storage"],
        _read_only_sources_fragment=metadata_fragments["read_only_sources"],
        _seal=_DOCUMENT_HEAD_VALUE_SEAL,
    )
    return _ParsedDocumentHead(
        payload=payload,
        envelope=raw,
        metadata=metadata,
        collections=collections,
        data_schema=prototype.schema,
        config=prototype.config,
        acl=prototype.acl,
    )


def _restore_persisted_document(
    document_class: type["Document"],
    head: _ParsedDocumentHead,
    *,
    loaded_entities: list[tuple[str, str, bytes]],
) -> "Document":
    """Build one domain object while its caller still holds the root fd."""
    try:
        doc = document_class(
            schema_version=head.metadata.schema_version,
            collaboration_mode=head.metadata.collaboration_mode,
            name=head.metadata.name,
            id=head.metadata.id,
            schema=head.data_schema,
            config=head.config,
            acl=head.acl,
            save_policy=head.metadata.save_policy,
        )
        doc._created_at = head.metadata.created_at
        doc._updated_at = head.metadata.updated_at
        doc._v = head.metadata.version
    except (KeyError, TypeError, ValueError, ZeError):
        raise _layout_error(
            "document_metadata_invalid",
            code="project_recovery_required",
        ) from None

    for entity_type, entity_id, entity_payload in loaded_entities:
        schema = doc._schema.get(entity_type)
        ent_raw = _decode_persisted_json(entity_payload, member="entity")
        try:
            ent = Entity.from_dict(ent_raw, schema=schema)
        except ZeError:
            raise _layout_error(
                "entity_payload_invalid",
                code="project_recovery_required",
            ) from None
        if ent.type != entity_type or ent.id != entity_id:
            raise ZeError(
                "entity_file_identity_mismatch",
                "an entity file does not match its indexed identity",
                context={"reason": "payload_identity_mismatch"},
            )
        if head.metadata.storage_version == "v2" and ent.read_only:
            raise _layout_error(
                "v2_read_only_entity_persisted",
                code="project_recovery_required",
            )
        # Restore the Document's validator authority without revalidating
        # historical data; future updates are strict.
        doc._apply_validator_authority(ent, validate_current=False)
        doc._insert_entity_preserving_overlay_order(ent)
    return doc


def _load_persisted_document_from_locked_store(
    document_class: type["Document"],
    store: EntityFileStore,
    head: _ParsedDocumentHead,
    *,
    maximum_raw_entity_bytes: int | None = None,
    maximum_entity_count: int | None = None,
) -> "Document":
    """Load every indexed Entity from one already-locked held root fd.

    ``Document.open`` leaves the aggregate limit unset to preserve its existing
    compatibility contract.  The strict legacy scanner supplies a private
    bounded limit because snapshot row limits apply only after this raw source
    graph has been loaded.
    """
    if maximum_raw_entity_bytes is not None and (
        type(maximum_raw_entity_bytes) is not int
        or maximum_raw_entity_bytes < 0
    ):
        raise TypeError(
            "maximum_raw_entity_bytes must be an exact non-negative integer"
        )
    if maximum_entity_count is not None and (
        type(maximum_entity_count) is not int or maximum_entity_count < 0
    ):
        raise TypeError("maximum_entity_count must be an exact non-negative integer")
    entity_count = sum(len(ids) for ids in head.collections.values())
    if maximum_entity_count is not None and entity_count > maximum_entity_count:
        raise _layout_error("legacy_scan_entity_count_exceeded")

    loaded_entities: list[tuple[str, str, bytes]] = []
    legacy_inodes: dict[tuple[int, int], str] = {}
    raw_entity_bytes = 0
    for entity_type, ids in head.collections.items():
        for entity_id in ids:
            remaining = (
                None
                if maximum_raw_entity_bytes is None
                else maximum_raw_entity_bytes - raw_entity_bytes
            )
            try:
                if head.metadata.storage_version == "v2":
                    if remaining is None:
                        entity_payload = store.read_v2_entity(
                            head.metadata.version,
                            entity_type,
                            entity_id,
                        )
                    else:
                        entity_payload = store.read_v2_entity(
                            head.metadata.version,
                            entity_type,
                            entity_id,
                            _maximum_bytes=remaining,
                        )
                else:
                    if remaining is None:
                        entity_payload, inode = store.read_legacy_entity(
                            entity_type,
                            entity_id,
                        )
                    else:
                        entity_payload, inode = store.read_legacy_entity(
                            entity_type,
                            entity_id,
                            _maximum_bytes=remaining,
                        )
            except EntityFileStoreError as exc:
                if (
                    remaining is not None
                    and remaining < MAX_ENTITY_FILE_BYTES
                    and exc.code == "document_storage_layout_invalid"
                    and exc.reason == "file_size_invalid"
                ):
                    raise _layout_error(
                        "legacy_scan_raw_entity_bytes_exceeded"
                    ) from None
                raise
            if head.metadata.storage_version != "v2":
                digest = entity_identity_digest(entity_type, entity_id)
                previous = legacy_inodes.get(inode)
                if previous is not None and previous != digest:
                    raise EntityFileStoreError(
                        "document_storage_mapping_collision",
                        "legacy_identity_mapping_collision",
                    )
                legacy_inodes[inode] = digest

            if maximum_raw_entity_bytes is not None:
                raw_entity_bytes += len(entity_payload)
                if raw_entity_bytes > maximum_raw_entity_bytes:
                    raise _layout_error("legacy_scan_raw_entity_bytes_exceeded")
            loaded_entities.append((entity_type, entity_id, entity_payload))

    return _restore_persisted_document(
        document_class,
        head,
        loaded_entities=loaded_entities,
    )


def _legacy_document_scan_forbidden(reason: str) -> ZeError:
    return ZeError(
        "legacy_document_scan_forbidden",
        "legacy document scan requires framework authority",
        context={"reason": reason},
    )


@contextmanager
def _open_document_for_legacy_scan(
    path: str | os.PathLike[str],
    *,
    _expected_root_identity: tuple[int, int],
    _capability: Any,
) -> Iterator[tuple[DocumentHead, "Document"]]:
    """Yield one exact unpublished Document while its source SH stays held.

    The topology owner first pins the intended live root and passes its exact
    ``(st_dev, st_ino)``.  This context re-proves that identity on the
    ``EntityFileStore`` root fd, then uses that single fd and one shared content
    lock for the exact head, every indexed Entity, full reconstruction and the
    caller's snapshot capture inside the yield.
    """
    if _capability is not _LEGACY_SCAN_DOCUMENT_CAPABILITY:
        raise _legacy_document_scan_forbidden("scan_capability_required")
    if (
        type(_expected_root_identity) is not tuple
        or len(_expected_root_identity) != 2
        or any(
            type(member) is not int or member < 0
            for member in _expected_root_identity
        )
    ):
        raise _legacy_document_scan_forbidden("scan_root_identity_required")

    try:
        store = EntityFileStore(
            path,
            create_parent=False,
            _expected_root_identity=_expected_root_identity,
        )
    except EntityFileStoreError as exc:
        raise _storage_error(exc) from None
    with store:
        try:
            store.acquire_shared_lock()
            parsed = _parse_document_head(store.read_document())
            document = _load_persisted_document_from_locked_store(
                Document,
                store,
                parsed,
                maximum_raw_entity_bytes=_MAX_LEGACY_SCAN_RAW_ENTITY_BYTES,
                maximum_entity_count=_MAX_LEGACY_SCAN_ENTITY_COUNT,
            )
        except EntityFileStoreError as exc:
            raise _storage_error(exc) from None

        exported = DocumentHead(
            metadata=parsed.metadata,
            payload=parsed.payload,
            _seal=_DOCUMENT_HEAD_VALUE_SEAL,
        )
        # Keep only the exact immutable head bytes/metadata and reconstructed
        # Document alive while the scanner performs its potentially long capture.
        del parsed
        yield exported, document


@contextmanager
def open_document_head(
    path: str | os.PathLike[str],
    *,
    exclusive: bool = False,
    missing_ok: bool = False,
    _expected_root_identity: tuple[int, int] | None = None,
) -> Iterator[DocumentHead | None]:
    """Yield one validated entity-free head while its project lock stays held.

    Callers that copy a live project use the default shared lock and keep their
    verify/copy/publish sequence inside this context.  Destructive legacy-root
    migration uses ``exclusive=True, missing_ok=True`` so a second initializer
    observes the first initializer's cleanup as an authoritative no-op *after*
    it acquires the same lock.  Required metadata reads keep ``missing_ok=False``
    and fail closed when no head exists.  A topology-owned caller may privately
    pass its pinned root identity; the store rechecks it before content locking.
    """
    if type(exclusive) is not bool or type(missing_ok) is not bool:
        raise ZeError(
            "validation_failed",
            "document head lock options must be exact booleans",
            context={"reason": "document_head_lock_option_invalid"},
        )
    try:
        store = EntityFileStore(
            path,
            create_parent=False,
            _expected_root_identity=_expected_root_identity,
        )
    except EntityFileStoreError as exc:
        raise _storage_error(exc) from None
    with store:
        try:
            if exclusive:
                store.acquire_exclusive_lock()
            else:
                store.acquire_shared_lock()
            payload = store.read_document_if_exists()
        except EntityFileStoreError as exc:
            raise _storage_error(exc) from None
        if payload is None:
            if not missing_ok:
                raise _layout_error(
                    "document_head_missing",
                    code="project_recovery_required",
                )
            yield None
            return
        parsed = _parse_document_head(payload)
        exported = DocumentHead(
            metadata=parsed.metadata,
            payload=payload,
            _seal=_DOCUMENT_HEAD_VALUE_SEAL,
        )
        # The context can intentionally span a large topology copy.  Keep only
        # the exact bytes + compact metadata fragments alive across that yield,
        # not the parser's complete envelope and normalized working graphs.
        del parsed
        yield exported


def read_document_metadata(path: str | os.PathLike[str]) -> DocumentMetadata:
    """Read one detached metadata snapshot under a short shared head lock."""
    with open_document_head(path) as head:
        assert head is not None
        return head.metadata


_METADATA_UNSET = object()


def _prepare_metadata_rewrite(
    *,
    expected_version: Any,
    id: Any,
    name: Any,
    acl: Any,
    config: Any,
) -> tuple[int, dict[str, Any]]:
    if (
        type(expected_version) is not int
        or expected_version < 1
        or expected_version > MAX_DOCUMENT_VERSION
    ):
        raise ZeError(
            "validation_failed",
            "expected_version must be an exact positive durable integer",
            context={"reason": "expected_version_invalid"},
        )
    updates: dict[str, Any] = {}
    if id is not _METADATA_UNSET:
        try:
            updates["id"] = _require_json_string(id, non_empty=True)
        except ZeError:
            raise ZeError(
                "validation_failed",
                "document id must be an exact non-empty UTF-8 string",
                context={"reason": "document_id_invalid"},
            ) from None
    if name is not _METADATA_UNSET:
        try:
            updates["name"] = _require_json_string(name)
        except ZeError:
            raise ZeError(
                "validation_failed",
                "document name must be an exact UTF-8 string",
                context={"reason": "document_name_invalid"},
            ) from None
    if acl is not _METADATA_UNSET:
        updates["acl"] = validate_document_acl(acl)
    if config is not _METADATA_UNSET:
        clone_failed = False
        try:
            cloned_config = _clone_json_value(config)
        except ZeError:
            clone_failed = True
            cloned_config = None
        if clone_failed or type(cloned_config) is not dict:
            raise ZeError(
                "validation_failed",
                "document config must be an exact strict-JSON object",
                context={"reason": "document_config_invalid"},
            )
        updates["config"] = cloned_config
    if not updates:
        raise ZeError(
            "validation_failed",
            "metadata rewrite requires at least one allowed field",
            context={"reason": "document_metadata_rewrite_empty"},
        )
    return expected_version, updates


def _rewrite_document_metadata_for_topology_staging(
    path: str | os.PathLike[str],
    *,
    expected_version: int,
    id: Any = _METADATA_UNSET,
    name: Any = _METADATA_UNSET,
    acl: Any = _METADATA_UNSET,
    config: Any = _METADATA_UNSET,
    _capability: Any = None,
    _expected_root_identity: tuple[int, int] | None = None,
) -> DocumentMetadata:
    """CAS-rewrite metadata in a topology-proven unpublished staging root.

    The complete operation holds the target's exclusive head lock.  It keeps
    ``_v``, timestamps, storage marker, collections and unknown members exactly
    as values, then validates the complete rewritten envelope through the same
    parser as ``Document.open`` before the atomic/fsynced head commit.  This is
    intentionally private: the topology coordinator's sole wrapper must
    first prove the target belongs to its staging root, then pass the exact
    module capability plus that opened root's ``(st_dev, st_ino)``.  The store
    rechecks the pin on its own held root fd before taking the content lock, so
    a path substitution cannot redirect this authority.  Live user edits must
    use normal commands and ``Document.save`` so versions, OpLog and publication
    move together.

    Every expected failure is sealed with the same durability fact used by
    ``Document.save``: anything before the head rename is NOT_DURABLE, while a
    directory-fsync failure after visible rename is UNKNOWN and never returned
    as a successful rewrite.
    """
    if _capability is not _TOPOLOGY_STAGING_METADATA_CAPABILITY:
        forbidden = ZeError(
            "topology_metadata_rewrite_forbidden",
            "metadata-only rewrite requires topology staging authority",
            context={"reason": "topology_staging_capability_required"},
        )
        raise _proven_save_failure(forbidden) from None
    if (
        type(_expected_root_identity) is not tuple
        or len(_expected_root_identity) != 2
        or any(
            type(member) is not int or member < 0
            for member in _expected_root_identity
        )
    ):
        forbidden = ZeError(
            "topology_metadata_rewrite_forbidden",
            "metadata-only rewrite requires a pinned topology staging root",
            context={"reason": "topology_staging_root_identity_required"},
        )
        raise _proven_save_failure(forbidden) from None
    try:
        expected, updates = _prepare_metadata_rewrite(
            expected_version=expected_version,
            id=id,
            name=name,
            acl=acl,
            config=config,
        )
    except ZeError as exc:
        raise _proven_save_failure(exc) from None

    try:
        store = EntityFileStore(
            path,
            create_parent=False,
            _expected_root_identity=_expected_root_identity,
        )
    except EntityFileStoreError as exc:
        raise _storage_write_error(exc) from None
    with store:
        try:
            store.acquire_exclusive_lock()
            current = _parse_document_head(store.read_document())
            if current.metadata.version != expected:
                raise ZeError(
                    "version_conflict",
                    "durable document version does not match the expected version",
                    context={
                        "current": current.metadata.version,
                        "expected": expected,
                        "reason": "durable_head_advanced",
                    },
                )
            rewritten = current.envelope
            for member, value in updates.items():
                rewritten[member] = value
            payload = _encode_persisted_json(rewritten, member="document")
            # Prove the exact candidate bytes are a head Document.open accepts
            # before the only authoritative commit point can be crossed.
            _parse_document_head(payload)
        except EntityFileStoreError as exc:
            raise _storage_write_error(exc) from None
        except DocumentWriteOutcomeError:
            raise
        except ZeError as exc:
            raise _proven_save_failure(exc) from None

        try:
            store.commit_document(payload)
        except EntityFileStoreError as exc:
            raise _storage_write_error(
                exc,
                document_version=current.metadata.version if exc.committed else None,
            ) from None
        except BaseException as exc:
            # An extension/monkeypatch that violates EntityFileStore's typed
            # contract gives no proof whether rename ran.  Never invent success.
            unknown = ZeError(
                "document_durability_unknown",
                "document metadata write durability is unknown",
                context={"reason": "unclassified_document_commit_failure"},
            )
            raise _proven_save_failure(
                unknown,
                outcome=_WRITE_UNKNOWN,
                document_version=current.metadata.version,
            ) from exc

        # Re-parse the exact bytes only after commit success; the returned view
        # therefore describes precisely the payload accepted at the commit point.
        return _parse_document_head(payload).metadata


@dataclass
class ReadOnlySourceSpec:
    """Declarative read-only data source declaration.

    M1: 仅作为 spec 记录, 不调用 loader.
    M2: register 时调 loader() 把 entity 灌进 doc.
    """
    name: str
    entity_type: str
    loader: Callable[[], Iterable[dict[str, Any]]]
    schema: dict[str, Any] = field(default_factory=dict)


def _clone_read_only_value(value: Any) -> Any:
    """Copy one strict JSON value graph without invoking user deepcopy hooks.

    Read-only loaders are extension code.  Their rows may reuse mutable buffers,
    so a shallow ``dict`` copy is not an isolation boundary; ``copy.deepcopy``
    is also unsuitable because arbitrary nested objects can execute custom
    ``__deepcopy__`` methods.  Exact built-in JSON types give every materialized
    Entity a detached value graph and a small, deterministic acceptance domain.
    """
    clone_failed = False
    try:
        cloned = _clone_json_value(value)
    except ZeError:
        clone_failed = True
        cloned = None
    if clone_failed:
        # Preserve the established source-specific diagnostic while sharing the
        # exact Entity/Document JSON acceptance and resource domain.
        raise ZeError(
            "validation_failed",
            "read-only source value must be strict JSON data",
            context={"reason": "read_only_source_value_invalid"},
        )
    return cloned


def _has_exact_string_keys(value: Any, expected: set[str]) -> bool:
    """Check one passive dict shape without hashing an untrusted key first."""
    if type(value) is not dict or len(value) != len(expected):
        return False
    for key in value.keys():
        if type(key) is not str or key not in expected:
            return False
    return True


def _document_schema_error(reason: str) -> ZeError:
    return ZeError(
        "validation_failed",
        "Document schema must map valid entity types to exact JSON objects",
        context={"reason": reason},
    )


def _own_entity_type_schema(name: Any, schema: Any) -> tuple[str, dict[str, Any]]:
    """Own one registry declaration under the Document schema contract."""
    invalid_name = False
    try:
        owned_name = _require_json_string(name, non_empty=True)
    except ZeError:
        invalid_name = True
        owned_name = ""
    if invalid_name or owned_name == "__document__":
        raise _document_schema_error("document_schema_entity_type_invalid")
    if type(schema) is not dict:
        raise _document_schema_error("document_schema_type_value_invalid")
    return owned_name, canonicalize_entity_schema(schema)


def _own_document_schema(value: Any) -> dict[str, dict[str, Any]]:
    """Own and semantically validate the complete entity-type schema map."""
    cloned = _clone_json_value(value)
    if type(cloned) is not dict:
        raise _document_schema_error("document_schema_invalid")
    canonical: dict[str, dict[str, Any]] = {}
    for entity_type, entity_schema in cloned.items():
        # The strict clone already establishes exact builtin strings and valid
        # UTF-8.  Requiring non-empty/non-reserved names and exact dict values
        # here closes falsey []/0/''/None ambiguities.
        if not entity_type or entity_type == "__document__":
            raise _document_schema_error("document_schema_entity_type_invalid")
        if type(entity_schema) is not dict:
            raise _document_schema_error("document_schema_type_value_invalid")
        canonical[entity_type] = canonicalize_entity_schema(entity_schema)
    # Flat-to-object normalization adds wrapper nodes.  Re-apply one shared
    # Document schema budget to the final in-memory authority, not only to each
    # source type independently.
    normalized = _clone_json_value(canonical)
    assert type(normalized) is dict
    return normalized


def _copy_read_only_sources_state(
    sources: dict[str, ReadOnlySourceSpec],
) -> dict[str, ReadOnlySourceSpec]:
    """Copy source declarations while retaining callable loader identity.

    A loader is executable extension state, not business data.  Deep-copying a
    callable object can run arbitrary hooks or fail on locks/file handles, so
    mutation checkpoints copy the declaration and JSON schema but keep the
    exact loader object.  Rollback therefore restores the same callable port.
    """
    copied: dict[str, ReadOnlySourceSpec] = {}
    for key, spec in sources.items():
        if type(key) is not str or type(spec) is not ReadOnlySourceSpec:
            raise TypeError("read-only source registry is invalid")
        spec_state = object.__getattribute__(spec, "__dict__")
        if not _has_exact_string_keys(
            spec_state, {"name", "entity_type", "loader", "schema"},
        ):
            raise TypeError("read-only source declaration shape is invalid")
        try:
            owned_key = _require_json_string(key, non_empty=True)
            owned_name = _require_json_string(spec_state["name"], non_empty=True)
            owned_entity_type = _require_json_string(
                spec_state["entity_type"], non_empty=True,
            )
        except ZeError as exc:
            raise TypeError("read-only source declaration strings are invalid") from exc
        if (
            owned_key != owned_name
            or owned_entity_type == "__document__"
            or not callable(spec_state["loader"])
            or type(spec_state["schema"]) is not dict
        ):
            raise TypeError("read-only source declaration state is invalid")
        owned_schema = _clone_json_value(spec_state["schema"])
        if type(owned_schema) is not dict:
            raise TypeError("read-only source declaration schema is invalid")
        copied[key] = ReadOnlySourceSpec(
            name=owned_name,
            entity_type=owned_entity_type,
            loader=spec_state["loader"],
            schema=owned_schema,
        )
    return copied


def _copy_document_state(state: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy Document metadata without executing source loader hooks."""
    copied: dict[str, Any] = {}
    for key, value in state.items():
        if key in {
            "_mutation_policy_registry_authority",
            "_mutation_policy_registry_epoch",
            "_history_op_log_authority",
            "_history_controller_authority",
            "_history_authority_epoch",
        }:
            # Process-local opaque authority is never serialized or copied.
            copied[key] = value
            continue
        if key == "_mutation_policy_command_authorities":
            if (
                type(value) is not dict
                or any(type(command_id) is not str or not command_id for command_id in value)
                or any(binding is None for binding in value.values())
            ):
                raise TypeError("Document mutation command authority is invalid")
            # Copy the container for rollback comparison while retaining every
            # exact opaque registration attestation by identity.
            copied[key] = dict(value)
            continue
        if key == "_read_only_sources":
            if type(value) is not dict:
                raise TypeError("Document._read_only_sources must be a dict")
            copied[key] = _copy_read_only_sources_state(value)
            continue
        # Every other current Document field is strict JSON/scalar state.  A
        # future executable port must be explicitly classified here rather than
        # gaining an implicit deepcopy hook during rollback capture.
        copied[key] = _clone_json_value(value)
    return copied



class Document(MutationPolicyGraphMixin):
    """Top-level data container (M1: 元数据骨架).

    Document 持有 skill 的全部数据 (entity-collections + 元数据 + 只读源声明).
    M1 只实装元数据部分; entity 相关方法在 02-entity 叶子完成后回头补 (M2).
    """

    def __init__(
        self,
        *,
        schema_version: str,
        collaboration_mode: str,
        name: str = "",
        id: str | None = None,
        schema: dict[str, Any] | None = None,
        mutation_policies: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
        acl: dict[str, Any] | None = None,
        harvest_rules: list[dict[str, Any]] | None = None,
        save_policy: str | None = None,
    ) -> None:
        scalar_failed = False
        try:
            owned_schema_version = _require_json_string(
                schema_version, non_empty=True,
            )
            owned_name = _require_json_string(name)
            owned_id = (
                None if id is None else _require_json_string(id, non_empty=True)
            )
        except ZeError:
            scalar_failed = True
            owned_schema_version = ""
            owned_name = ""
            owned_id = None
        if scalar_failed:
            raise ZeError(
                "validation_failed",
                "Document identity and metadata strings must be exact UTF-8 values",
                context={"reason": "document_scalar_invalid"},
            )
        if (
            type(collaboration_mode) is not str
            or collaboration_mode not in _VALID_COLLABORATION_MODES
        ):
            raise ZeError(
                "validation_failed",
                f"Document.collaboration_mode must be one of {_VALID_COLLABORATION_MODES}",
                context={"reason": "document_collaboration_mode_invalid"},
            )
        if save_policy is not None and (
            type(save_policy) is not str or save_policy not in _VALID_SAVE_POLICIES
        ):
            raise ZeError(
                "validation_failed",
                f"Document.save_policy must be one of {_VALID_SAVE_POLICIES}",
                context={"reason": "document_save_policy_invalid"},
            )
        self._id = owned_id if owned_id is not None else uuid.uuid4().hex
        self._name = owned_name
        self._schema_version = owned_schema_version
        self._collaboration_mode = collaboration_mode
        # 声明值与生效值刻意分开：None 表示旧 document.json 从未声明过。
        # save() 不能把 None 自动写成兼容结果，否则一次普通保存就会偷偷迁移旧文档。
        self._save_policy = save_policy
        cloned_schema = _own_document_schema(schema if schema is not None else {})
        cloned_config = _clone_json_value(config if config is not None else {})
        cloned_harvest_rules = _clone_json_value(
            harvest_rules if harvest_rules is not None else []
        )
        if type(cloned_config) is not dict:
            raise ZeError(
                "validation_failed",
                "Document.config must be an exact JSON object",
                context={"reason": "document_config_invalid"},
            )
        if type(cloned_harvest_rules) is not list or any(
            type(rule) is not dict for rule in cloned_harvest_rules
        ):
            raise ZeError(
                "validation_failed",
                "Document.harvest_rules must be a JSON list of objects",
                context={"reason": "document_harvest_rules_invalid"},
            )
        self._schema = cloned_schema
        # Runtime declaration authority.  It deliberately does not enter
        # document.json or business snapshots: a policy deployment changes
        # what future commands may do, not the persisted project facts.
        self._mutation_policies = _own_document_mutation_policies(
            mutation_policies if mutation_policies is not None else {},
            cloned_schema,
        )
        # Bootstrap may seed/open canonical entities before the full command
        # registry exists.  Successful registry validation seals the runtime;
        # from that point every protected live mutation needs an authorized
        # command segment or coordinator-owned history replay scope.
        self._mutation_policy_sealed = False
        self._mutation_policy_registry_authority = None
        self._mutation_policy_registry_epoch = None
        self._mutation_policy_registry_pid = None
        self._mutation_policy_command_authorities: dict[str, Any] = {}
        # Process-local canonical history authority.  Business snapshots carry
        # none of these identities; bootstrap/rehydration must bind the exact
        # OpLog + UndoController pair loaded for this project.
        self._history_op_log_authority = None
        self._history_controller_authority = None
        self._history_authority_epoch = None
        self._history_authority_pid = None
        self._config = cloned_config
        self._acl = validate_document_acl(acl, missing=acl is None)
        # broker 机会式收割规则(mod 声明,会话内只读;不进 document.json —— 每次 boot 由 register 重灌)
        self._harvest_rules = cloned_harvest_rules
        self._read_only_sources: dict[str, ReadOnlySourceSpec] = {}
        # entity-collections: entity_type → {entity_id → Entity}
        self._collections: dict[str, dict[str, Entity]] = defaultdict(dict)
        self._v = 1
        self._created_at = time.time()
        self._updated_at = self._created_at

    # ---- meta (M1) ----

    @property
    def id(self) -> str: return self._id
    @property
    def name(self) -> str: return self._name
    @property
    def schema_version(self) -> str: return self._schema_version
    @property
    def collaboration_mode(self) -> str: return self._collaboration_mode
    @property
    def save_policy(self) -> str | None:
        """显式声明的保存策略；旧文档未声明时为 None。"""
        return self._save_policy
    @property
    def effective_save_policy(self) -> str:
        """当前真正生效的保存策略（包含旧 collaboration_mode 兼容映射）。

        历史运行时曾让 ``live_collab`` / ``realtime_checkpoint`` 在成功 mutation 后只写
        OpLog、不写 Document；重启会留下无法安全回放/撤销的分叉。W01 已把这种状态定义为
        非法耐久事实，因此缺失策略或显式 ``legacy`` 都安全收敛到 ``auto_commit``。
        声明字段本身仍保持 None/legacy，普通 save 不会伪造一次 schema 迁移。
        """
        if self._save_policy == "checkpoint_commit":
            return "checkpoint_commit"
        return "auto_commit"
    @property
    def auto_commit_enabled(self) -> bool:
        """是否应在成功 mutation 后自动提交 canonical（只读策略判断）。"""
        return self.effective_save_policy in ("auto_commit", "checkpoint_commit")
    @property
    def should_auto_commit(self) -> bool:
        """供提交协调器消费的语义化只读别名。"""
        return self.auto_commit_enabled

    def adopt_save_policy(self, policy: str) -> bool:
        """把缺失/legacy 声明升级为 mod 当前策略；幂等返回是否发生变化。

        只供 bootstrap 的版本化装载迁移使用。已经显式声明 auto/checkpoint 的文档
        不被代码升级静默覆盖；策略切换需单独的产品迁移。
        """
        if type(policy) is not str or policy not in ("auto_commit", "checkpoint_commit"):
            raise ZeError(
                "validation_failed",
                "adopted save_policy must be auto_commit or checkpoint_commit",
                context={"reason": "document_save_policy_invalid"},
            )
        if self._save_policy not in (None, "legacy"):
            return False
        self._save_policy = policy
        return True

    @property
    def schema(self) -> dict[str, Any]: return _clone_json_value(self._schema)
    @property
    def mutation_policies(self) -> dict[str, Any]:
        return _clone_json_value(self._mutation_policies)
    @property
    def config(self) -> dict[str, Any]: return _clone_json_value(self._config)
    @property
    def acl(self) -> dict[str, Any]:
        # ACL is a security boundary, not a read-only convention.  A shallow
        # ``dict`` copy still exposes the internal collaborators list (and any
        # future nested rule object), letting a consumer mutate authorization
        # without document.set_acl, CAS, OpLog, persistence, or SSE.  Return an
        # isolated graph so every effective ACL change must use the command path.
        return _clone_json_value(self._acl)
    @property
    def harvest_rules(self) -> list[dict[str, Any]]:
        return _clone_json_value(self._harvest_rules)
    @property
    def version(self) -> int: return self._v
    @property
    def created_at(self) -> float: return self._created_at
    @property
    def updated_at(self) -> float: return self._updated_at

    # ---- meta 变更(仅供 document.* command handler 调用,经 Operation 通道)----

    def meta_dict(self) -> dict[str, Any]:
        """文档元数据快照 —— 给文档级变更广播(entity_type=__document__)用。"""
        return {
            "id": self._id,
            "name": self._name,
            "schema_version": self._schema_version,
            "collaboration_mode": self._collaboration_mode,
            "save_policy": self._save_policy,
            "effective_save_policy": self.effective_save_policy,
            "auto_commit_enabled": self.auto_commit_enabled,
            "version": self._v,
            "acl": _clone_json_value(self._acl),
        }

    def _guard_document_version(self, expected_version: Any) -> None:
        if (
            type(self._v) is not int
            or self._v < 1
            or self._v > _MAX_SQLITE_INTEGER
        ):
            raise ZeError(
                "validation_failed",
                "document version state is outside the durable integer domain",
                context={"reason": "document_version_state_invalid"},
            )
        if expected_version is None:
            return
        if (
            type(expected_version) is not int
            or expected_version < 1
            or expected_version > _MAX_SQLITE_INTEGER
        ):
            raise ZeError(
                "validation_failed",
                "expected_version must be an exact SQLite positive integer",
                context={"reason": "expected_version_invalid"},
            )
        if expected_version != self._v:
            raise ZeError(
                "version_conflict",
                f"document version {self._v} does not match the expected version",
                context={"current": self._v, "expected": expected_version},
            )

    def _next_document_version(self) -> int:
        self._guard_document_version(None)
        if self._v == _MAX_SQLITE_INTEGER:
            raise ZeError(
                "version_overflow",
                "document version has reached the durable integer ceiling",
                context={"reason": "document_version_exhausted"},
            )
        return self._v + 1

    def rename(self, new_name: str, *, expected_version: int | None = None) -> str:
        """改文档名,返回旧名(handler 据此构造 Patch before/after)。

        仅供 `document.rename` command handler 调用 —— 不直接落盘（持久化由提交入口按
        save_policy 统一处理）。空名抛 validation_failed;版本不符抛 version_conflict。
        """
        self._guard_document_version(expected_version)
        scalar_failed = False
        try:
            nm = _require_json_string(new_name).strip()
        except ZeError:
            scalar_failed = True
            nm = ""
        if scalar_failed or not nm:
            raise ZeError(
                "validation_failed",
                "document name must be a non-empty string",
                context={"reason": "document_name_invalid"},
            )
        old = self._name
        self._name = nm
        self._updated_at = time.time()
        return old

    def set_acl(self, new_acl: dict[str, Any], *, expected_version: int | None = None) -> dict[str, Any]:
        """改文档 ACL(owner/collaborators),返回旧 ACL(handler 据此构造 Patch before/after)。

        仅供 `document.set_acl` command handler 调用。owner 语义由 server 层解释 ——
        Document 只存 acl dict,不判断"谁是 owner"。版本不符抛 version_conflict。
        """
        self._guard_document_version(expected_version)
        validated_acl = validate_document_acl(new_acl)
        old = _clone_json_value(self._acl)
        self._acl = validated_acl
        self._updated_at = time.time()
        return old

    # ---- framework mutation transaction ----

    def capture_mutation_snapshot(self) -> DocumentMutationSnapshot:
        """Capture one complete public rollback checkpoint."""
        return self._capture_mutation_snapshot(
            include_policy_fingerprints=True,
        )

    def _capture_mutation_snapshot(
        self,
        *,
        include_policy_fingerprints: bool,
    ) -> DocumentMutationSnapshot:
        """Capture every in-memory field needed to undo one mutation attempt.

        The snapshot is intentionally opaque and bound to this exact Document
        object.  Document business metadata is deep-copied; executable source
        loaders and Entity validators retain exact object identity and are never
        deep-copied.  Collection topology is captured separately with references
        to the original buckets and Entity objects, while every Entity data/state
        field is copied.  Consequently a restore removes newly added entities,
        resurrects hard-removed entities, and preserves pre-mutation identities.

        Complexity is O(total Document + Entity state).  Callers must capture
        inside the same mutation lock used by the commit coordinator and keep
        the snapshot only for the lifetime of that commit attempt.
        """
        if type(include_policy_fingerprints) is not bool:
            raise TypeError("include_policy_fingerprints must be an exact bool")
        try:
            live_state = object.__getattribute__(self, "__dict__")
            if type(live_state) is not dict:
                raise TypeError("Document state mapping is invalid")
            state_without_collections: dict[str, Any] = {}
            for key, value in live_state.items():
                if type(key) is not str:
                    raise TypeError("Document state keys must be exact strings")
                if key != "_collections":
                    state_without_collections[key] = value
            document_state = _copy_document_state(state_without_collections)
            collections = self._collections
            policy_types = set(self._mutation_policies)
            fingerprint_types = (
                policy_types if include_policy_fingerprints else set()
            )
            default_factory = getattr(collections, "default_factory", None)
            buckets: list[_BucketMutationState] = []
            entities: list[_EntityMutationState] = []
            seen_entities: set[int] = set()
            for entity_type, bucket in collections.items():
                members = tuple(bucket.items())
                buckets.append(_BucketMutationState(
                    entity_type=entity_type,
                    bucket=bucket,
                    members=members,
                ))
                for _, entity in members:
                    if (
                        type(entity) is not Entity
                        or object.__getattribute__(entity, "_document_owner") is not self
                    ):
                        raise TypeError("collection Entity owner is invalid")
                    identity = id(entity)
                    if identity in seen_entities:
                        continue
                    seen_entities.add(identity)
                    entity_state = object.__getattribute__(entity, "__dict__")
                    admission = entity_state.get("_document_admission_state")
                    if (
                        type(admission) is not tuple
                        or len(admission) != 2
                        or type(admission[0]) is not dict
                        or not callable(admission[1])
                    ):
                        raise TypeError("collection Entity admission authority is invalid")
                    entities.append(_EntityMutationState(
                        entity=entity,
                        state=_copy_entity_state(entity),
                        # Runtime durable-envelope fingerprints are an opt-in
                        # mutation-policy cost.  Legacy/unprotected Documents
                        # retain the original full rollback snapshot without a
                        # second Entity.to_dict()+JSON pass for every entity.
                        durable_bytes=(
                            _mutation_fingerprint_bytes(
                                entity.to_dict(),
                                reason="mutation_policy_entity_state_invalid",
                            )
                            if entity_type in fingerprint_types
                            else b""
                        ),
                        policy_protected=entity_type in policy_types,
                        schema_authority=entity_state.get("_schema"),
                        validator_authority=entity_state.get("_validator"),
                        document_owner=entity_state.get("_document_owner"),
                        admission_schema_authority=admission[0],
                        admission_validator_authority=admission[1],
                    ))
        except Exception as exc:
            raise ZeError(
                "mutation_snapshot_failed",
                "could not capture the complete in-memory Document state",
                context={
                    "document_id": self._id,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            ) from exc

        snapshot = DocumentMutationSnapshot(
            _owner=self,
            _document_state=document_state,
            _collections=collections,
            _collections_default_factory=default_factory,
            _buckets=tuple(buckets),
            _entities=tuple(entities),
        )
        object.__setattr__(snapshot, "_schema_authority", self._schema)
        object.__setattr__(
            snapshot, "_mutation_policy_authority", self._mutation_policies,
        )
        object.__setattr__(
            snapshot,
            "_mutation_policy_registry_authority",
            self._mutation_policy_registry_authority,
        )
        object.__setattr__(
            snapshot,
            "_mutation_policy_registry_epoch",
            self._mutation_policy_registry_epoch,
        )
        object.__setattr__(
            snapshot,
            "_mutation_policy_command_authority",
            self._mutation_policy_command_authorities,
        )
        object.__setattr__(
            snapshot, "_history_op_log_authority", self._history_op_log_authority,
        )
        object.__setattr__(
            snapshot,
            "_history_controller_authority",
            self._history_controller_authority,
        )
        object.__setattr__(
            snapshot, "_history_authority_epoch", self._history_authority_epoch,
        )
        return snapshot

    def restore_mutation_snapshot(self, snapshot: DocumentMutationSnapshot) -> None:
        """Restore a full snapshot in place without replacing this Document.

        Ownership uses object identity, not ``document_id``: two loaded objects
        with the same id are separate in-memory worlds and may not consume each
        other's rollback token.
        """
        if not isinstance(snapshot, DocumentMutationSnapshot):
            raise ZeError(
                "validation_failed",
                "snapshot must be a DocumentMutationSnapshot",
                context={"got": type(snapshot).__name__},
            )
        if snapshot._owner is not self:
            raise ZeError(
                "mutation_snapshot_owner_mismatch",
                "a mutation snapshot can only be restored on its original Document object",
                context={
                    "snapshot_document_id": snapshot.document_id,
                    "target_document_id": self._id,
                },
            )

        # Perform every operation that can realistically fail before touching
        # live state.  Applying the prepared dicts below is then only clear/update.
        try:
            document_state = _copy_document_state(snapshot._document_state)
            restored_schema = document_state.get("_schema")
            restored_policies = document_state.get("_mutation_policies")
            restored_command_authorities = document_state.get(
                "_mutation_policy_command_authorities",
            )
            if (
                type(snapshot._schema_authority) is not dict
                or type(snapshot._mutation_policy_authority) is not dict
                or type(snapshot._mutation_policy_command_authority) is not dict
                or type(restored_schema) is not dict
                or type(restored_policies) is not dict
                or type(restored_command_authorities) is not dict
                or document_state.get("_mutation_policy_registry_authority")
                is not snapshot._mutation_policy_registry_authority
                or document_state.get("_mutation_policy_registry_epoch")
                is not snapshot._mutation_policy_registry_epoch
                or document_state.get("_history_op_log_authority")
                is not snapshot._history_op_log_authority
                or document_state.get("_history_controller_authority")
                is not snapshot._history_controller_authority
                or document_state.get("_history_authority_epoch")
                is not snapshot._history_authority_epoch
            ):
                raise TypeError("captured declaration authority is invalid")
            entity_states: list[tuple[Entity, dict[str, Any]]] = []
            snapshot_entity_ids = {id(record.entity) for record in snapshot._entities}
            for record in snapshot._entities:
                restored = _copy_entity_state_mapping(record.state)
                if restored["_document_owner"] is not self:
                    raise TypeError("captured Entity owner is invalid")
                current_owner = object.__getattribute__(
                    record.entity, "_document_owner",
                )
                if type(current_owner) is Document and current_owner is not self:
                    raise TypeError("captured Entity moved to a foreign Document")
                if record.policy_protected and (
                    type(record.schema_authority) is not dict
                    or not callable(record.validator_authority)
                    or record.document_owner is not self
                    or type(record.admission_schema_authority) is not dict
                    or not callable(record.admission_validator_authority)
                ):
                    raise TypeError("captured Entity authority is invalid")
                entity_states.append((record.entity, restored))
            new_entity_origins: list[
                tuple[Entity, _AdmittedEntityMutationState]
            ] = []
            admitted_by_identity = {
                id(record.entity): record for record in snapshot._admitted_entities
            }
            seen_current: set[int] = set()
            live_collections = object.__getattribute__(self, "__dict__").get(
                "_collections"
            )
            current_candidates: list[Any] = []
            if hasattr(live_collections, "values"):
                for bucket in live_collections.values():
                    if type(bucket) is dict:
                        current_candidates.extend(bucket.values())
            current_candidates.extend(
                record.entity for record in snapshot._admitted_entities
            )
            for entity in current_candidates:
                identity = id(entity)
                if identity in seen_current or identity in snapshot_entity_ids:
                    continue
                seen_current.add(identity)
                if type(entity) is not Entity:
                    raise TypeError("session-created Entity is invalid")
                admitted = admitted_by_identity.get(identity)
                current_owner = object.__getattribute__(
                    entity, "_document_owner"
                )
                if admitted is None:
                    if current_owner is None:
                        # A raw standalone object may have been inserted directly
                        # into a bucket.  Restoring bucket topology removes it;
                        # there is no Document authority to unwind.
                        continue
                    if current_owner is self:
                        schema, validator = entity._prepare_document_unbind(
                            self, capability=_DOCUMENT_AUTHORITY_CAPABILITY,
                        )
                        admitted = _AdmittedEntityMutationState(
                            entity=entity,
                            schema_authority=schema,
                            schema_bytes=_mutation_fingerprint_bytes(
                                schema,
                                reason="mutation_entity_admission_schema_invalid",
                            ),
                            validator_authority=validator,
                        )
                    else:
                        raise TypeError("session-created Entity owner is invalid")
                if current_owner is None:
                    new_entity_origins.append((entity, admitted))
                    continue
                if current_owner is not self:
                    raise TypeError("session-created Entity owner is invalid")
                # Do not trust the live admission tuple here: failed extension
                # code may have damaged or removed it.  The session journal is
                # the rollback authority and is sufficient unless the Entity has
                # genuinely moved under a foreign Document owner.
                new_entity_origins.append((entity, admitted))
        except Exception as exc:
            raise ZeError(
                "mutation_restore_failed",
                "could not prepare the captured Document state for restore",
                context={
                    "document_id": snapshot.document_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            ) from exc

        try:
            # Restore declaration values into the exact authority objects that
            # existed at capture time.  Replacing them with deep-copy objects
            # would make a caught inner-command failure poison its still-active
            # outer command; retaining mutated values would preserve an in-place
            # policy/schema escalation.  Exact dicts make clear/update infallible.
            snapshot._schema_authority.clear()
            snapshot._schema_authority.update(restored_schema)
            snapshot._mutation_policy_authority.clear()
            snapshot._mutation_policy_authority.update(restored_policies)
            snapshot._mutation_policy_command_authority.clear()
            snapshot._mutation_policy_command_authority.update(
                restored_command_authorities,
            )
            document_state["_schema"] = snapshot._schema_authority
            document_state["_mutation_policies"] = snapshot._mutation_policy_authority
            document_state["_mutation_policy_registry_authority"] = (
                snapshot._mutation_policy_registry_authority
            )
            document_state["_mutation_policy_registry_epoch"] = (
                snapshot._mutation_policy_registry_epoch
            )
            document_state["_mutation_policy_command_authorities"] = (
                snapshot._mutation_policy_command_authority
            )
            document_state["_history_op_log_authority"] = (
                snapshot._history_op_log_authority
            )
            document_state["_history_controller_authority"] = (
                snapshot._history_controller_authority
            )
            document_state["_history_authority_epoch"] = (
                snapshot._history_authority_epoch
            )

            # Admission is externally observable on an Entity reference.  Undo
            # it before removing session-created members so rollback does not
            # leave owner-bound ghosts that cannot enter another Document.
            for entity, admitted in new_entity_origins:
                restored_admission_schema = json.loads(
                    admitted.schema_bytes.decode("utf-8", "strict")
                )
                admitted.schema_authority.clear()
                admitted.schema_authority.update(restored_admission_schema)
                entity._schema = admitted.schema_authority
                entity._validator = admitted.validator_authority
                entity._document_owner = None
                entity._document_admission_state = None

            for record, (entity, restored) in zip(
                snapshot._entities, entity_states,
            ):
                if record.entity is not entity:
                    raise ZeError(
                        "mutation_restore_failed",
                        "captured Entity inventory changed during restore",
                        context={"reason": "mutation_entity_inventory_invalid"},
                    )
                if not record.policy_protected:
                    # Policy-opt-out keeps the established snapshot semantics:
                    # restore the detached complete state directly.  Exact
                    # schema/admission object identity is policy authority and
                    # should not add an O(all entities) rollback tax here.
                    entity.__dict__.clear()
                    entity.__dict__.update(restored)
                    continue
                record.schema_authority.clear()
                record.schema_authority.update(restored["_schema"])
                admission = restored["_document_admission_state"]
                if type(admission) is not tuple or len(admission) != 2:
                    raise ZeError(
                        "mutation_restore_failed",
                        "captured Entity admission authority is invalid",
                        context={"reason": "mutation_admission_authority_invalid"},
                    )
                record.admission_schema_authority.clear()
                record.admission_schema_authority.update(admission[0])
                restored["_schema"] = record.schema_authority
                restored["_validator"] = record.validator_authority
                restored["_document_owner"] = record.document_owner
                restored["_document_admission_state"] = (
                    record.admission_schema_authority,
                    record.admission_validator_authority,
                )
                entity.__dict__.clear()
                entity.__dict__.update(restored)

            # Restore original bucket objects first.  Clearing them removes
            # session-created entities; re-adding members resurrects the exact
            # original objects, including entities hard-removed in the session.
            for record in snapshot._buckets:
                record.bucket.clear()
                record.bucket.update(record.members)

            collections = snapshot._collections
            collections.clear()
            if hasattr(collections, "default_factory"):
                collections.default_factory = snapshot._collections_default_factory
            for record in snapshot._buckets:
                collections[record.entity_type] = record.bucket

            self.__dict__.clear()
            self.__dict__.update(document_state)
            self.__dict__["_collections"] = collections
        except Exception as exc:
            raise ZeError(
                "mutation_restore_failed",
                "could not restore the captured Document state",
                context={
                    "document_id": snapshot.document_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            ) from exc

    def begin_mutation_session(
        self,
        *,
        exclusive: bool = False,
    ) -> "DocumentMutationSession":
        """Start a one-shot commit/rollback guard for a framework mutation.

        ``exclusive=True`` is reserved for unpublished-head bootstrap work.  It
        rejects any outer session and prevents a later session from acquiring a
        rollback token that could invalidate an already-issued runtime head.
        """
        if type(exclusive) is not bool:
            raise TypeError("exclusive must be an exact bool")
        return DocumentMutationSession(self, exclusive=exclusive)

    # ---- runtime mutation-policy authority ----

    def _install_mutation_policy_authority(self, value: Any) -> None:
        """Install detached code/runtime policy without changing business data."""
        if (
            self._mutation_policy_sealed
            or self._mutation_policy_registry_authority is not None
            or self._mutation_policy_registry_epoch is not None
            or self._mutation_policy_registry_pid is not None
            or self._mutation_policy_command_authorities
            or self._history_op_log_authority is not None
            or self._history_controller_authority is not None
            or self._history_authority_epoch is not None
            or self._history_authority_pid is not None
        ):
            raise ZeError(
                "mutation_policy_runtime_install_forbidden",
                "sealed mutation policy authority cannot be replaced in place",
                context={"document_id": self._id},
            )
        self._mutation_policies = _own_document_mutation_policies(value, self._schema)

    def _guard_protected_mutation_entry(self, entity_type: str) -> None:
        """Require a live runtime authority before touching a protected type."""
        if (
            not self._mutation_policy_sealed
            or entity_type not in self._mutation_policies
        ):
            return
        if _history_replay_scope_active(self):
            _require_active_history_policy_coverage(self)
            return
        stack = _mutation_session_stacks().get(id(self), ())
        command_authority = any(
            candidate.active
            and candidate._policy_command_authority_active
            for candidate in reversed(stack)
        )
        coordinator_coverage = any(
            candidate.active
            and candidate._policy_coverage_active
            and candidate._policy_coverage_enforced
            for candidate in reversed(stack)
        )
        if command_authority and coordinator_coverage:
            return
        raise ZeError(
            "mutation_policy_coordinator_required",
            "protected mutation requires coordinator coverage and command authority",
            context={
                "document_id": self._id,
                "entity_type": entity_type,
                "command_authority": command_authority,
                "coordinator_coverage": coordinator_coverage,
            },
        )

    def _guard_owned_entity_mutation(self, entity: Entity) -> None:
        if (
            type(entity) is not Entity
            or object.__getattribute__(entity, "_document_owner") is not self
        ):
            raise ZeError(
                "validation_failed",
                "Entity mutation authority does not match its Document owner",
                context={"reason": "entity_document_owner_mismatch"},
            )
        self._guard_protected_mutation_entry(entity.type)


    # ---- read-only sources ----

    def register_read_only_source(self, spec: ReadOnlySourceSpec) -> None:
        """原子记录 spec + 物化只读 entities，绝不覆盖 canonical 数据.

        loader 返回 iterable[dict]; 每个 dict 形如 {"id": ..., "name": ..., "data": {...}}
        或直接 data dict (没 id 字段就自动生成). 整个 dict 视为 data 的备选.

        loader、构造、Document schema 校验或 identity 冲突任一步失败时，source 与整批
        materialization 都保持调用前状态。尤其禁止同 `(type, id)` 的只读 overlay
        覆盖 canonical Entity；否则后续持久化排除 read_only 时会静默丢业务行。

        Document 对应 entity type 的 schema 是唯一权威；仅当 Document 没声明该类型
        时，才用 ``spec.schema`` 作为物化输入的兜底校验合同。调用方须与其他 Document
        mutation 共用同一把锁；本原子性不包含 loader 自己产生的外部副作用。
        """
        if type(spec) is not ReadOnlySourceSpec:
            raise ZeError(
                "validation_failed",
                "spec must be ReadOnlySourceSpec",
                context={"got": type(spec).__name__},
            )
        spec_state = object.__getattribute__(spec, "__dict__")
        if not _has_exact_string_keys(
            spec_state, {"name", "entity_type", "loader", "schema"},
        ):
            raise ZeError(
                "validation_failed",
                "read-only source declaration has an invalid shape",
                context={"reason": "read_only_source_declaration_invalid"},
            )

        invalid_name = False
        try:
            source_name = _require_json_string(
                spec_state["name"], non_empty=True,
            )
        except ZeError:
            invalid_name = True
            source_name = ""
        if invalid_name or not source_name.strip():
            raise ZeError(
                "validation_failed",
                "read-only source name must be a non-empty string",
                context={"reason": "read_only_source_name_invalid"},
            )

        invalid_entity_type = False
        try:
            source_entity_type = _require_json_string(
                spec_state["entity_type"], non_empty=True,
            )
        except ZeError:
            invalid_entity_type = True
            source_entity_type = ""
        if invalid_entity_type or not source_entity_type.strip() or source_entity_type == "__document__":
            raise ZeError(
                "validation_failed",
                "read-only source entity_type must be a non-empty, non-reserved string",
                context={
                    "reason": "read_only_source_entity_type_invalid",
                    "source": source_name,
                },
            )
        source_loader = spec_state["loader"]
        if not callable(source_loader):
            raise ZeError(
                "validation_failed",
                "read-only source loader must be callable",
                context={"reason": "read_only_source_loader_invalid", "source": source_name},
            )
        source_schema_input = spec_state["schema"]
        if type(source_schema_input) is not dict:
            raise ZeError(
                "validation_failed",
                "read-only source schema must be a dict",
                context={"reason": "read_only_source_schema_invalid", "source": source_name},
            )
        if source_name in self._read_only_sources:
            raise ZeError(
                "validation_failed",
                f"read-only source {source_name!r} already registered",
                context={"name": source_name},
            )

        # 完整 materialize + validate 后才动 Document，避免 loader/validator 中途
        # 失败留下“source 已注册但只有半批实体”的不可恢复会话状态。逐条消费
        # iterable，既不额外保留原始批次，也兼容复用同一个 dict 缓冲区的流式 loader。
        prepared: list[Entity] = []
        seen: set[tuple[str, str]] = set()
        source_schema = canonicalize_entity_schema(
            _clone_read_only_value(source_schema_input)
        )
        document_schema = self._schema.get(source_entity_type) if self._schema else None
        effective_schema = _clone_read_only_value(
            document_schema if document_schema is not None else source_schema
        )
        effective_validator = make_validator(effective_schema) if effective_schema else None
        for index, raw in enumerate(source_loader()):
            if type(raw) is not dict:
                raise ZeError(
                    "validation_failed",
                    "read-only source loader rows must be dict objects",
                    context={
                        "reason": "read_only_source_row_invalid",
                        "source": source_name,
                        "row_index": index,
                        "got": type(raw).__name__,
                    },
                )
            frozen_raw = _clone_read_only_value(raw)
            raw_id = frozen_raw.get("id")
            if raw_id is not None and (type(raw_id) is not str or not raw_id):
                raise ZeError(
                    "validation_failed",
                    "read-only source row id must be null/missing or a non-empty string",
                    context={
                        "reason": "read_only_source_row_invalid",
                        "source": source_name,
                        "row_index": index,
                        "member": "id",
                    },
                )
            raw_name = frozen_raw.get("name", "")
            if type(raw_name) is not str:
                raise ZeError(
                    "validation_failed",
                    "read-only source row name must be a string",
                    context={
                        "reason": "read_only_source_row_invalid",
                        "source": source_name,
                        "row_index": index,
                        "member": "name",
                    },
                )
            if "data" in frozen_raw and type(frozen_raw["data"]) is not dict:
                raise ZeError(
                    "validation_failed",
                    "read-only source row data must be an object",
                    context={
                        "reason": "read_only_source_row_invalid",
                        "source": source_name,
                        "row_index": index,
                        "member": "data",
                    },
                )
            ent = Entity(
                type=source_entity_type,
                data=frozen_raw.get("data", frozen_raw),
                name=raw_name,
                id=raw_id,
                schema=effective_schema,
                validator=effective_validator,
                read_only=True,
            )
            ent._bind_document_authority(
                self,
                effective_schema,
                (
                    effective_validator
                    if effective_validator is not None
                    else _no_op_validator
                ),
                validate_current=False,
                capability=_DOCUMENT_AUTHORITY_CAPABILITY,
            )
            key = (ent.type, ent.id)
            if key in seen or ent.id in self._collections.get(ent.type, {}):
                raise ZeError(
                    "validation_failed",
                    "read-only source identity conflicts with an existing entity",
                    context={
                        "reason": "read_only_source_identity_conflict",
                        "source": source_name,
                        "entity_type": ent.type,
                        "entity_id": ent.id,
                    },
                )
            seen.add(key)
            prepared.append(ent)

        # Store a detached declaration so caller mutation cannot rename the
        # already-indexed source or rewrite its persisted schema metadata.
        registered_spec = ReadOnlySourceSpec(
            name=source_name,
            entity_type=source_entity_type,
            loader=source_loader,
            schema=source_schema,
        )
        self._read_only_sources[registered_spec.name] = registered_spec
        for ent in prepared:
            self._collections[ent.type][ent.id] = ent

    def read_only_sources(self) -> list[str]:
        return sorted(self._read_only_sources.keys())

    def unregister_read_only_source(self, name: str) -> None:
        """撤销源注册(只摘 spec;桶内实体的购销归 caller——物化器的刷新语义自己管)。

        不存在时静默(idempotent)。给「同一 doc 实例重物化」用:register 对重名抛错是
        防误注册,刷新流先 unregister 再 register。
        """
        self._read_only_sources.pop(name, None)

    # ---- entity 集成 ----

    def _insert_entity_preserving_overlay_order(self, entity: Entity) -> int:
        """Insert one Entity while keeping the public list order deterministic.

        Canonical entities are durable business state; read-only entities are a
        boot-time overlay.  If an overlay was materialized before a later local
        add, a plain dict append would interleave the two segments and the next
        restart (canonical load first, overlay materialization second) would
        change ``Document.list`` order.  Rebuild the exact existing bucket in
        place so external bucket references remain valid and the invariant is
        always ``canonical insertion order + overlay materialization order``.
        """
        bucket = self._collections[entity.type]
        if entity.read_only:
            position = len(bucket)
            bucket[entity.id] = entity
            return position
        first_overlay_position = next(
            (
                index for index, item in enumerate(bucket.values())
                if item.read_only
            ),
            None,
        )
        if first_overlay_position is None:
            position = len(bucket)
            bucket[entity.id] = entity
            return position
        overlays = [
            (entity_id, item)
            for entity_id, item in bucket.items()
            if item.read_only
        ]
        canonical = [
            (entity_id, item)
            for entity_id, item in bucket.items()
            if not item.read_only
        ]
        bucket.clear()
        bucket.update(canonical)
        bucket[entity.id] = entity
        bucket.update(overlays)
        return first_overlay_position

    def _entity_collection_position(self, entity: Entity) -> int:
        """Return the insertion position captured by this Entity's admission."""
        if type(entity) is not Entity:
            raise TypeError("entity must be an exact Entity")
        bucket = self._collections.get(entity.type)
        if type(bucket) is not dict or bucket.get(entity.id) is not entity:
            raise ZeError(
                "entity_not_found",
                "Entity is not an exact member of this Document",
                context={"entity_type": entity.type, "entity_id": entity.id},
            )
        if next(reversed(bucket)) == entity.id:
            return len(bucket) - 1
        for position, (entity_id, member) in enumerate(bucket.items()):
            if entity_id == entity.id and member is entity:
                return position
        raise ZeError(
            "entity_not_found",
            "Entity position is not present in this Document",
            context={"entity_type": entity.type, "entity_id": entity.id},
        )

    def _apply_validator_authority(
        self,
        entity: Entity,
        *,
        validate_current: bool,
    ) -> None:
        """Replace standalone validation policy with this Document's policy.

        A custom validator is meaningful while an Entity is standalone.  Once
        the Entity enters a Document, the Document's type schema is the sole
        authority: a non-empty schema receives the exact framework validator;
        a missing/empty schema receives the exact no-op.  Restore paths attach
        without revalidating historical data, while public add validates now.
        """
        if type(self) is not Document:
            raise ZeError(
                "validation_failed",
                "validator authority requires an exact Document owner",
                context={"reason": "document_owner_type_invalid"},
            )
        type_schema = self._schema.get(entity.type, {})
        if type_schema:
            validator = make_validator(type_schema)
        else:
            validator = _no_op_validator
        entity._bind_document_authority(
            self,
            type_schema,
            validator,
            validate_current=validate_current,
            capability=_DOCUMENT_AUTHORITY_CAPABILITY,
        )

    def add(self, entity: Entity) -> Entity:
        """加 entity 到 collections.

        - id 冲突抛 validation_failed
        - 无条件用 Document 对应 type 的 schema 接管 validator authority；非空 schema
          立即校验当前 data，空/缺 schema 则清除 standalone custom validator 为 exact no-op.
        """
        if type(entity) is not Entity:
            raise ZeError(
                "validation_failed",
                "document accepts only exact Entity values",
                context={"reason": "entity_instance_invalid"},
            )
        self._guard_protected_mutation_entry(entity.type)
        if entity.type == "__document__":
            raise ZeError(
                "validation_failed",
                "entity_type '__document__' is reserved (document-meta patch sentinel)",
                context={"entity_type": entity.type},
            )
        _guard_entity_not_reserved_by_foreign_session(self, entity)
        bucket = self._collections.get(entity.type)
        if bucket is not None and entity.id in bucket:
            raise ZeError(
                "validation_failed",
                f"实体 id {entity.id!r} 在类型 {entity.type!r} 下已存在",
                context={"entity_id": entity.id, "entity_type": entity.type},
            )
        self._apply_validator_authority(entity, validate_current=True)
        self._insert_entity_preserving_overlay_order(entity)
        _record_session_entity_admission(self, entity)
        self._updated_at = time.time()
        return entity

    def _add_entity_internal(self, entity: Entity) -> None:
        """Restore/bootstrap insert that preserves metadata but rejects overwrite.

        Unlike :meth:`add`, this internal path does not advance Document
        ``updated_at``.  Checkpoint restore consumes it while rebuilding a past
        snapshot.  It must still enforce reserved type and identity uniqueness;
        internal callers are not allowed to regain the old overwrite behavior.
        """
        if type(entity) is not Entity:
            raise ZeError(
                "validation_failed",
                "document accepts only exact Entity values",
                context={"reason": "entity_instance_invalid"},
            )
        if entity.type == "__document__":
            raise ZeError(
                "validation_failed",
                "entity_type '__document__' is reserved",
                context={
                    "reason": "entity_type_reserved",
                    "entity_type": entity.type,
                },
            )
        _guard_entity_not_reserved_by_foreign_session(self, entity)
        if entity.id in self._collections.get(entity.type, {}):
            raise ZeError(
                "validation_failed",
                "internal entity identity conflicts with an existing entity",
                context={
                    "reason": "entity_identity_conflict",
                    "entity_type": entity.type,
                    "entity_id": entity.id,
                },
            )
        self._apply_validator_authority(entity, validate_current=False)
        self._insert_entity_preserving_overlay_order(entity)
        _record_session_entity_admission(self, entity)

    def get(self, entity_type: str, entity_id: str) -> Entity:
        """返回 entity. 不存在抛 entity_not_found."""
        bucket = self._collections.get(entity_type, {})
        if entity_id not in bucket:
            raise ZeError(
                "entity_not_found",
                f"{entity_type}/{entity_id}",
                context={"entity_type": entity_type, "entity_id": entity_id},
            )
        return bucket[entity_id]

    def find(self, entity_type: str, entity_id: str) -> Entity | None:
        return self._collections.get(entity_type, {}).get(entity_id)

    def list(self, entity_type: str, *, include_deleted: bool = False) -> list[Entity]:
        """列出某类型的全部 entity. 默认过滤软删."""
        return [
            e for e in self._collections.get(entity_type, {}).values()
            if include_deleted or not e.deleted
        ]

    def types(self) -> list[str]:
        """返回当前有 entity 的 entity_type 名清单 (sorted)."""
        return sorted(t for t, bucket in self._collections.items() if bucket)

    def hard_remove(self, entity_type: str, entity_id: str) -> None:
        """物理删除 (不查 read_only). 不存在时静默 (idempotent)."""
        self._guard_protected_mutation_entry(entity_type)
        bucket = self._collections.get(entity_type, {})
        if entity_id in bucket:
            bucket[entity_id]._unbind_document_authority(
                self, capability=_DOCUMENT_AUTHORITY_CAPABILITY,
            )
            del bucket[entity_id]
            self._updated_at = time.time()

    # ---- 框架内部 hook: undo / sync 用; 不走 command ACL ----

    def apply_patch_directly(
        self,
        patch: Any,
        *,
        actor: Actor,
        _replay_capability: Any = None,
    ) -> None:
        """Apply one privileged replay patch with atomic final invariants."""
        _require_history_replay_capability(_replay_capability)
        policy_coverage = _require_active_history_policy_coverage(self)
        if (
            policy_coverage is not None
            and _active_history_operation_binding(self) is None
        ):
            raise ZeError(
                "mutation_operation_ledger_mismatch",
                "protected history replay requires its issued Operation",
                context={"reason": "history_operation_missing"},
            )
        snapshot = self.capture_mutation_snapshot()
        coverage_before = _protected_mutation_fingerprint(self)
        try:
            with _history_replay_scope(self):
                self._apply_patch_directly_unchecked(
                    patch,
                    actor=actor,
                    _replay_capability=_HISTORY_REPLAY_CAPABILITY,
                )
            self._validate_mutation_policy_cardinality()
            self._validate_guarded_history_replay((patch,))
            _record_history_policy_coverage(
                self,
                coverage_before,
                (patch,),
                actor=actor,
            )
        except BaseException as original_error:
            try:
                self.restore_mutation_snapshot(snapshot)
            except BaseException as rollback_error:
                raise ZeError(
                    "mutation_rollback_failed",
                    "history replay failed and Document rollback also failed",
                    context={
                        "original_error_type": type(original_error).__name__,
                        "original_error_code": getattr(original_error, "code", None),
                        "rollback_error_type": type(rollback_error).__name__,
                        "rollback_error_code": getattr(rollback_error, "code", None),
                    },
                ) from original_error
            raise

    def _apply_patch_directly_unchecked(
        self,
        patch: Any,
        *,
        actor: Actor,
        _replay_capability: Any = None,
    ) -> None:
        """框架内部用: 把 Patch 应用到 entity / doc.

        - 不走 command ACL (避免 undo 撞 sources_allowed)
        - 走 entity / doc 的 CAS (expected_version=当前 version), 落 actor_log
        - 不调 OpLog.append (caller 自己负责造 rollback_op 落)

        Patch.field 约定:
          - "__entity__"        → 元操作 (op: create / soft_delete / restore / delete)
          - 任意字段名 + op=name → entity.name 改名 (caller 把名字放 patch.after)
          - 其他字段名 + op=set/delete → 字段级变更

        见 04-history-rollback SPEC §4.
        """
        _require_history_replay_capability(_replay_capability)

        if type(patch) is not Patch or any(
            type(value) is not str or not value
            for value in (
                patch.entity_type,
                patch.entity_id,
                patch.field,
                patch.op,
            )
        ):
            raise ZeError(
                "validation_failed",
                "history replay requires an exact, well-shaped Patch",
                context={"reason": "history_replay_patch_invalid"},
            )

        op = patch.op
        et = patch.entity_type
        eid = patch.entity_id
        field_name = patch.field
        if patch.position is not None and (
            type(patch.position) is not int or patch.position < 0
        ):
            raise ZeError(
                "validation_failed",
                "history replay Patch position is invalid",
                context={"reason": "history_replay_position_invalid"},
            )

        # 重放不校验: undo / redo / sync 重放 reverse-patch 只是忠实还原早前状态,
        # 该状态写入时已校验(或是 validator 存在前的历史脏值). 若在此再跑真 validator,
        # 撤销一条"把历史脏值改成合法值"的编辑时, reverse-patch 会重放旧脏值而被拒
        # (schema_violation 中止 undo). 前向编辑(command → entity.update)仍走真 validator.
        # 见 04-history-rollback SPEC §4; 恢复 attach_validator (1.105.0) 之前的重放语义.

        # 文档级 patch(__document__):undo/redo 重放 doc-meta 变更(当前仅 rename)
        if et == DOCUMENT_TARGET:
            if (
                patch.position is not None
                or patch.provenance is not None
                or patch.ancestry is not None
            ):
                raise ZeError(
                    "validation_failed",
                    "document replay Patch cannot carry entity-only envelopes",
                    context={"reason": "history_replay_patch_envelope_invalid"},
                )
            if field_name == "name" and op == "set":
                self.rename(patch.after)   # 不传 expected_version:undo 系统自管版本栅栏
                return
            if field_name == "acl" and op == "set":
                self.set_acl(patch.after)  # 不传 expected_version:undo 系统自管版本栅栏
                return
            raise ZeError(
                "validation_failed",
                f"unsupported __document__ patch (field={field_name!r}, op={op!r})",
                context={"field": field_name, "op": op},
            )

        if field_name == ENTITY_META_FIELD:
            # 元操作
            if op == "create":
                if patch.provenance is not None or patch.ancestry is not None or (
                    et in self._mutation_policies and patch.position is None
                ):
                    raise ZeError(
                        "validation_failed",
                        "protected replay create requires one exact position",
                        context={"reason": "history_replay_position_invalid"},
                    )
                new_entity = Entity.from_dict(patch.after)
                if new_entity.type != et or new_entity.id != eid:
                    raise ZeError(
                        "validation_failed",
                        "replay create payload identity does not match its Patch target",
                        context={"reason": "history_replay_create_identity_mismatch"},
                    )
                # Replay restores already-historical state.  Internal admission
                # binds today's Document authority without immediately rejecting
                # a value that predates it; future public updates are validated.
                self._add_entity_internal(new_entity)
                if patch.position is not None:
                    bucket = self._collections[et]
                    current_position = tuple(bucket).index(eid)
                    item = bucket.pop(eid)
                    remaining = list(bucket.items())
                    if patch.position > len(remaining):
                        raise ZeError(
                            "validation_failed",
                            "history replay create position is outside the bucket",
                            context={"reason": "history_replay_position_invalid"},
                        )
                    remaining.insert(patch.position, (eid, item))
                    bucket.clear()
                    bucket.update(remaining)
                    if tuple(bucket).index(eid) != patch.position:
                        raise ZeError(
                            "validation_failed",
                            "history replay could not restore collection order",
                            context={"reason": "history_replay_position_invalid"},
                        )
                return
            if op == "delete":
                if patch.provenance is not None or patch.ancestry is not None or (
                    et in self._mutation_policies and patch.position is None
                ):
                    raise ZeError(
                        "validation_failed",
                        "protected replay delete requires one exact position",
                        context={"reason": "history_replay_position_invalid"},
                    )
                existing = self.find(et, eid)
                if existing is not None and patch.position is not None:
                    current_position = tuple(self._collections[et]).index(eid)
                    if current_position != patch.position:
                        raise ZeError(
                            "validation_failed",
                            "history replay delete position does not match the bucket",
                            context={"reason": "history_replay_position_mismatch"},
                        )
                if (
                    existing is not None
                    and et in self._mutation_policies
                    and not _json_values_exact(existing.to_dict(), patch.before)
                ):
                    raise ZeError(
                        "validation_failed",
                        "protected replay delete payload does not match the Entity",
                        context={"reason": "history_replay_entity_precondition_failed"},
                    )
                # hard_remove: 不依赖当前 entity 存在 (idempotent)
                self.hard_remove(et, eid)
                return
            if (
                patch.position is not None
                or patch.provenance is not None
                or patch.ancestry is not None
            ):
                raise ZeError(
                    "validation_failed",
                    "non-structural meta replay cannot carry Patch envelopes",
                    context={"reason": "history_replay_patch_envelope_invalid"},
                )
            entity = self.get(et, eid)   # 不存在抛 entity_not_found
            if op == "soft_delete":
                entity.delete(expected_version=entity.version, actor=actor)
                return
            if op == "restore":
                entity.restore(expected_version=entity.version, actor=actor)
                return
            if op == "name":
                # 改名重放 (Patch 契约: name 是 __entity__ 元操作)。entity.update 走空 patches 只改名。
                entity._update_for_replay(
                    {}, name=patch.after,
                    expected_version=entity.version, actor=actor,
                )
                return
            raise ZeError(
                "validation_failed",
                f"unknown meta op {op!r} for __entity__ field",
                context={"op": op, "entity_type": et, "entity_id": eid},
            )

        # 字段级
        entity = self.get(et, eid)
        if patch.position is not None:
            raise ZeError(
                "validation_failed",
                "field replay cannot carry collection position",
                context={"reason": "history_replay_position_invalid"},
            )
        ancestry_data = _history_replay_ancestry_data(entity, patch)
        if op == "set":
            entity._update_for_replay(
                {field_name: patch.after},
                expected_version=entity.version,
                actor=actor,
            )
            if ancestry_data is not None:
                entity._data = ancestry_data
                entity.to_dict()
            _apply_history_patch_provenance(entity, patch)
            return
        if op == "delete":
            # Policy opt-in uses the Operation contract's true field removal
            # semantics so reverse replay can distinguish missing from explicit
            # JSON null.  Legacy documents retain the historical set-to-None
            # behavior until they explicitly adopt the versioned policy contract.
            entity._update_for_replay(
                {field_name: None},
                expected_version=entity.version,
                actor=actor,
            )
            if et in self._mutation_policies:
                entity._data = _mutation_audit_delete_path(
                    entity._data, field_name,
                )
            if ancestry_data is not None:
                entity._data = ancestry_data
                entity.to_dict()
            _apply_history_patch_provenance(entity, patch)
            return
        if op == "name":
            if patch.provenance is not None or patch.ancestry is not None:
                raise ZeError(
                    "validation_failed",
                    "name replay cannot carry field provenance",
                    context={"reason": "history_replay_provenance_invalid"},
                )
            entity._update_for_replay(
                {},
                name=patch.after,
                expected_version=entity.version,
                actor=actor,
            )
            return
        raise ZeError(
            "validation_failed",
            f"unknown patch op {op!r} for field {field_name!r}",
            context={"op": op, "field": field_name,
                     "entity_type": et, "entity_id": eid},
        )

    def _validate_guarded_history_replay(self, patches: Iterable[Patch]) -> None:
        """Revalidate final replay targets for schema fields that opt in.

        Replay normally preserves pre-schema history exactly.  A product that
        tightens a field used by a hard runtime boundary may mark that field
        with ``x-ze-history-replay-guard: true``.  We then validate each final
        target once, after the whole replay batch, so a legacy undo/redo cannot
        resurrect an unrenderable value.  A multi-patch transaction may still
        pass through a temporarily invalid intermediate value as long as its
        final state is valid.
        """
        guarded_types: dict[str, dict[str, Any]] = {}
        targets: set[tuple[str, str]] = set()
        for patch in patches:
            if type(patch) is not Patch or patch.entity_type == DOCUMENT_TARGET:
                continue
            entity_type = patch.entity_type
            type_schema = self._schema.get(entity_type)
            if type(type_schema) is not dict:
                continue
            properties = type_schema.get("properties")
            if type(properties) is not dict:
                continue
            guarded_properties = {
                field_name: field_schema
                for field_name, field_schema in properties.items()
                if type(field_schema) is dict
                and field_schema.get(HISTORY_REPLAY_SCHEMA_GUARD) is True
            }
            if not guarded_properties:
                continue
            guarded_schema: dict[str, Any] = {
                "type": "object",
                "properties": _clone_json_value(guarded_properties),
            }
            root_required = type_schema.get("required")
            if type(root_required) is list:
                guarded_required = [
                    field_name for field_name in root_required
                    if field_name in guarded_properties
                ]
                if guarded_required:
                    guarded_schema["required"] = guarded_required
            guarded_types[entity_type] = guarded_schema
            targets.add((entity_type, patch.entity_id))

        validators = {
            entity_type: make_validator(schema)
            for entity_type, schema in guarded_types.items()
        }
        for entity_type, entity_id in sorted(targets):
            entity = self.find(entity_type, entity_id)
            if entity is None:
                continue
            guarded_properties = guarded_types[entity_type]["properties"]
            guarded_data = {
                field_name: entity.data[field_name]
                for field_name in guarded_properties
                if field_name in entity.data
            }
            validators[entity_type](
                guarded_data,
                _clone_json_value(guarded_types[entity_type]),
                f"${entity_type}",
            )

    def apply_patches_atomically(
        self,
        patches: list[Any],
        *,
        actor: Actor,
        _replay_capability: Any = None,
    ) -> None:
        """按序 apply 多条 patch;任一失败则精确回滚到调用前状态(全有或全无)。

        undo / redo / rollback 的循环用:单条 apply_patch_directly 已保证单 patch 的 CAS + actor_log,
        但一组多 patch 中途失败(如 reverse-patch 命中已被后续 op 硬删的 entity → entity_not_found)会
        留下"半套用"的内存态,下一条命令的 save 会把它持久化 → doc 与 OpLog 分叉。本方法复用
        Document 唯一的 full mutation snapshot，精确恢复原 Entity/bucket 对象身份、插入顺序、
        空桶、文档时间/版本与全部元数据，不再维护第二套局部回滚逻辑。
        """
        _require_history_replay_capability(_replay_capability)
        policy_coverage = _require_active_history_policy_coverage(self)
        if (
            policy_coverage is not None
            and _active_history_operation_binding(self) is None
        ):
            raise ZeError(
                "mutation_operation_ledger_mismatch",
                "protected history replay requires its issued Operation",
                context={"reason": "history_operation_missing"},
            )
        if type(patches) not in (list, tuple) or any(
            type(patch) is not Patch for patch in patches
        ):
            raise ZeError(
                "validation_failed",
                "history replay batch requires exact Patch values",
                context={"reason": "history_replay_patch_invalid"},
            )

        snapshot = self.capture_mutation_snapshot()
        coverage_before = _protected_mutation_fingerprint(self)
        try:
            with _history_replay_scope(self):
                for p in patches:
                    self._apply_patch_directly_unchecked(
                        p,
                        actor=actor,
                        _replay_capability=_HISTORY_REPLAY_CAPABILITY,
                    )
            self._validate_mutation_policy_cardinality()
            self._validate_guarded_history_replay(tuple(patches))
            _record_history_policy_coverage(
                self,
                coverage_before,
                tuple(patches),
                actor=actor,
            )
        except BaseException as original_error:
            try:
                self.restore_mutation_snapshot(snapshot)
            except BaseException as rollback_error:
                raise ZeError(
                    "mutation_rollback_failed",
                    "patch batch failed and Document rollback also failed",
                    context={
                        "original_error_type": type(original_error).__name__,
                        "original_error_code": getattr(original_error, "code", None),
                        "rollback_error_type": type(rollback_error).__name__,
                        "rollback_error_code": getattr(rollback_error, "code", None),
                    },
                ) from original_error
            raise

    # ---- 引用图 ----

    def detect_cycles(
        self,
        *,
        edge_lookup: Callable[[Entity], list[tuple[str, str]]] | None = None,
    ) -> list[list[str]]:
        """检测引用环. 返回 [[type/id, ...]] 形式的环列表.

        edge_lookup(entity) 返回 [(target_type, target_id), ...]; 默认扫 entity.data
        里 {"$ref": "type/id"} 字典.
        """
        if edge_lookup is None:
            edge_lookup = _default_ref_extractor

        adj: dict[str, list[str]] = {}
        for et, bucket in self._collections.items():
            for e in bucket.values():
                key = f"{et}/{e.id}"
                adj[key] = [f"{t}/{i}" for t, i in edge_lookup(e)]

        cycles: list[list[str]] = []
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {k: WHITE for k in adj}
        stack: list[str] = []

        def dfs(node: str) -> None:
            color[node] = GRAY
            stack.append(node)
            for nb in adj.get(node, []):
                if nb not in color:
                    continue
                if color[nb] == GRAY:
                    if nb in stack:
                        idx = stack.index(nb)
                        cycles.append(stack[idx:] + [nb])
                elif color[nb] == WHITE:
                    dfs(nb)
            stack.pop()
            color[node] = BLACK

        for n in list(color.keys()):
            if color[n] == WHITE:
                dfs(n)
        return cycles

    def cascade_delete(
        self,
        entity_type: str,
        entity_id: str,
        *,
        actor: Actor,
        edge_lookup: Callable[[Entity], list[tuple[str, str]]] | None = None,
        on_asset_unref: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[tuple[str, str]]:
        """BFS 软删 entity + 所有引用子孙. 返回被删除的 [(type, id)] 列表.

        edge_lookup 默认扫 $ref 字典; on_asset_unref 在遇到 asset 引用时调 (M2 hook,
        03-asset 完成后由 caller 传真 asset_store unref callback).
        """
        if edge_lookup is None:
            edge_lookup = _default_ref_extractor

        queue: list[tuple[str, str]] = [(entity_type, entity_id)]
        visited: set[tuple[str, str]] = set()
        removed: list[tuple[str, str]] = []
        while queue:
            cur = queue.pop(0)
            if cur in visited:
                continue
            visited.add(cur)
            et, eid = cur
            e = self.find(et, eid)
            if e is None or e.deleted:
                continue
            # asset_unref hook: data 内 {"sha256": ...} 字典视为 asset_ref
            if on_asset_unref:
                for v in e.data.values():
                    if isinstance(v, dict) and "sha256" in v:
                        on_asset_unref(v)
            for ref_type, ref_id in edge_lookup(e):
                queue.append((ref_type, ref_id))
            # read_only entity 不软删 (跳过)
            if not e.read_only:
                e.delete(expected_version=e.version, actor=actor)
                removed.append(cur)
        self._updated_at = time.time()
        return removed

    # ---- persistence ----

    def save(
        self,
        path: str | os.PathLike,
        *,
        expected_version: int | None = None,
    ) -> None:
        """Persist one complete identity-addressed generation.

        Business identities are hashed, never used as paths.  Every entity in
        ``new_v`` is durable before ``document.json`` is atomically replaced;
        that final replace is the only authoritative commit point.  Legacy raw
        entity layouts remain readable, but the next successful save migrates
        them and removes the old tree only after the new head commits.
        """
        if _document_has_exclusive_mutation_session(self):
            raise _proven_save_failure(ZeError(
                "document_save_forbidden",
                "Document.save is forbidden during an exclusive runtime mutation",
                context={
                    "reason": "exclusive_mutation_session_active",
                    "document_id": self.id,
                },
            ))
        # Pure in-memory validation remains side-effect-free (including parent
        # and lock-file creation).  The same checks are repeated under the
        # writer lock before the durable CAS and version allocation.
        try:
            self._guard_document_version(expected_version)
            self._next_document_version()
        except ZeError as exc:
            raise _proven_save_failure(exc) from None
        try:
            store = EntityFileStore(path, create_parent=True)
        except EntityFileStoreError as exc:
            raise _storage_write_error(exc) from None
        with store:
            try:
                # This exclusive lock covers the full storage transaction:
                # durable-head CAS, generation preparation, document commit and
                # unreachable-generation cleanup.  A reader holds the matching
                # shared lock through its final Entity reconstruction.
                store.acquire_exclusive_lock()
                self._guard_document_version(expected_version)
                durable_payload = store.read_document_if_exists()
                if durable_payload is None:
                    if self._v != 1:
                        raise _layout_error(
                            "durable_head_missing",
                            code="project_recovery_required",
                        )
                else:
                    durable_head = _parse_document_head(durable_payload)
                    durable_version = durable_head.metadata.version
                    if durable_version != self._v:
                        raise ZeError(
                            "version_conflict",
                            "durable document version does not match the in-memory version",
                            context={
                                "current": durable_version,
                                "expected": self._v,
                                "reason": "durable_head_advanced",
                            },
                        )
                    if durable_head.metadata.id != self._id:
                        raise _layout_error(
                            "durable_head_identity_mismatch",
                            code="project_recovery_required",
                        )
                new_v = self._next_document_version()
            except EntityFileStoreError as exc:
                raise _storage_write_error(exc) from None
            except DocumentWriteOutcomeError:
                raise
            except ZeError as exc:
                raise _proven_save_failure(exc) from None

            # read_only source 是会话 overlay，不是当前项目拥有的 canonical 数据。
            # 它仍可参与 get/list 与编辑器展示，但任何无关用户保存都不得把外部源快照
            # 偷偷写进 document.json/entities。旧版本曾落过的只读行会在本次 save 的
            # orphan cleanup 中清掉；source 声明本身仍作为可重物化元数据保留。
            try:
                collections_index: dict[str, list[str]] = {}
                payloads: dict[str, bytes] = {}
                digest_owners: dict[str, tuple[str, str]] = {}
                for entity_type, bucket in self._collections.items():
                    canonical_bucket: dict[str, Entity] = {}
                    for entity_id, entity in bucket.items():
                        if entity.read_only:
                            continue
                        durable = entity.to_dict()
                        if durable.get("type") != entity_type or durable.get("id") != entity_id:
                            raise ZeError(
                                "entity_file_identity_mismatch",
                                "an in-memory entity does not match its collection identity",
                                context={"reason": "memory_identity_mismatch"},
                            )
                        try:
                            digest = entity_identity_digest(entity_type, entity_id)
                        except EntityFileStoreError as exc:
                            raise _storage_write_error(exc) from None
                        owner = digest_owners.get(digest)
                        if owner is not None and owner != (entity_type, entity_id):
                            raise ZeError(
                                "document_storage_mapping_collision",
                                "document storage identity mapping collided",
                                context={"reason": "entity_identity_digest_collision"},
                            )
                        digest_owners[digest] = (entity_type, entity_id)
                        payloads[digest] = _encode_persisted_json(durable, member="entity")
                        canonical_bucket[entity_id] = entity
                    if canonical_bucket:
                        collections_index[entity_type] = list(canonical_bucket.keys())
            except DocumentWriteOutcomeError:
                raise
            except ZeError as exc:
                raise _proven_save_failure(exc) from None

            previous_updated_at = self._updated_at
            try:
                store.audit_before_save()
                store.write_generation(new_v, payloads)
            except EntityFileStoreError as exc:
                raise _storage_write_error(exc) from None

            try:
                self._updated_at = time.time()
                out = {
                    "schema": "ze-document",
                    "schema_version": self._schema_version,
                    "id": self._id,
                    "name": self._name,
                    "collaboration_mode": self._collaboration_mode,
                    "data_schema": self._schema,
                    "config": self._config,
                    "acl": self._acl,
                    "collections": collections_index,
                    "read_only_sources": [
                        {"name": s.name, "entity_type": s.entity_type}
                        for s in self._read_only_sources.values()
                    ],
                    "entity_storage": entity_storage_marker(),
                    "created_at": self._created_at,
                    "updated_at": self._updated_at,
                    "_v": new_v,
                }
                # 旧文档缺 save_policy 时必须继续缺失；普通 save 不是迁移操作。
                if self._save_policy is not None:
                    out["save_policy"] = self._save_policy
                document_payload = _encode_persisted_json(out, member="document")
            except ZeError as exc:
                self._updated_at = previous_updated_at
                raise _proven_save_failure(exc) from None
            except BaseException:
                self._updated_at = previous_updated_at
                raise
            try:
                store.commit_document(document_payload)
            except EntityFileStoreError as exc:
                if exc.committed:
                    # rename 已切换 visible head。即使目录 fsync 无法确认，内存
                    # 版本也必须与磁盘可见版本对齐，避免上层错误回滚后重用旧 CAS。
                    self._v = new_v
                else:
                    self._updated_at = previous_updated_at
                raise _storage_write_error(
                    exc,
                    document_version=new_v if exc.committed else None,
                ) from None
            except BaseException:
                # The concrete store reports every post-rename failure as a
                # typed committed error.  An untyped extension/test failure has
                # no commit proof, so retain the old in-memory timestamp.
                self._updated_at = previous_updated_at
                raise

            self._v = new_v
            # Garbage is non-authoritative.  Cleanup failure leaves a complete
            # current generation and is deliberately not reported as save
            # failure after the single commit point has succeeded.
            store.cleanup_after_commit(new_v)

    @classmethod
    def open(cls, path: str | os.PathLike) -> "Document":
        """Load either the v2 hashed generation or one safe legacy raw tree."""
        try:
            store = EntityFileStore(path, create_parent=False)
        except EntityFileStoreError as exc:
            raise _storage_error(exc) from None
        with store:
            try:
                store.acquire_shared_lock()
                head = _parse_document_head(store.read_document())
                return _load_persisted_document_from_locked_store(
                    cls,
                    store,
                    head,
                )
            except EntityFileStoreError as exc:
                raise _storage_error(exc) from None


# ==============================================================
# DocumentMutationSession — framework one-shot rollback guard
# ==============================================================



# ==============================================================
# DocumentRegistry — mod 端注册 API
# ==============================================================

class DocumentRegistry:
    """Mod 端 register() 函数收集声明用.

    使用流程 (见 02-mod/05-code/document_register.py + SPEC §7):
      framework 启动时:
        registry = DocumentRegistry()
        mod_document_register.register(registry)     # mod 端调 declare_xxx 灌声明
        doc = registry.build_document()              # framework 把声明装成 Document
        if registry.demo:
            registry.demo["bootstrap"](doc)           # 跑 demo bootstrap
    """

    def __init__(self) -> None:
        self._entity_types: dict[str, dict[str, Any]] = {}
        self._mutation_policies: dict[str, dict[str, Any]] = {}
        self._collaboration_mode: str | None = None
        self._save_policy: str | None = None
        self._read_only_sources: list[dict[str, Any]] = []
        self._open_migrations: list["Callable[[Document], bool]"] = []
        self._harvest_rules: list[dict[str, Any]] = []
        self._demo: dict[str, Any] | None = None
        # 多模板 demo catalog(一等文件系统能力):每条 = 一个业务场景的可实例化模板。
        # 老的 set_demo_project 仍可用(转登记为 id="default"),.demo 访问器不变(向后兼容)。
        self._demo_templates: list[dict[str, Any]] = []

    # ---- declare_xxx (mod 端调用) ----

    def declare_entity_type(
        self,
        *,
        name: str,
        schema: dict[str, Any],
        collaboration_unit: bool = False,
        mutation_policy: dict[str, Any] | None = None,
    ) -> None:
        owned_name, owned_schema = _own_entity_type_schema(name, schema)
        if type(collaboration_unit) is not bool:
            raise ZeError(
                "validation_failed",
                "collaboration_unit must be an exact bool",
                context={"name": owned_name},
            )
        if owned_name in self._entity_types:
            raise ZeError(
                "validation_failed",
                f"entity_type {owned_name!r} already declared",
                context={"name": owned_name},
            )
        owned_policy = None
        if mutation_policy is not None:
            owned_policy = _own_entity_mutation_policy(
                owned_name, owned_schema, mutation_policy,
            )
        # Publish the declaration only after every fallible ownership/parser
        # step succeeds.  A caller that catches an invalid policy must not be
        # left with the same type silently registered without protection.
        self._entity_types[owned_name] = {
            "schema": owned_schema,
            "collaboration_unit": collaboration_unit,
        }
        if owned_policy is not None:
            self._mutation_policies[owned_name] = owned_policy

    def set_collaboration_mode(self, mode: str) -> None:
        if mode not in _VALID_COLLABORATION_MODES:
            raise ZeError(
                "validation_failed",
                f"collaboration_mode must be one of {_VALID_COLLABORATION_MODES}",
                context={"got": repr(mode)},
            )
        self._collaboration_mode = mode

    def set_save_policy(self, policy: str) -> None:
        """声明保存策略；不提供 manual-save 模式。"""
        if policy not in _VALID_SAVE_POLICIES:
            raise ZeError(
                "validation_failed",
                f"save_policy must be one of {_VALID_SAVE_POLICIES}",
                context={"got": repr(policy)},
            )
        self._save_policy = policy

    def declare_read_only_source(
        self,
        *,
        name: str,
        entity_type: str,
        loader_path: str,
    ) -> None:
        if any(s["name"] == name for s in self._read_only_sources):
            raise ZeError(
                "validation_failed",
                f"read-only source {name!r} already declared",
                context={"name": name},
            )
        self._read_only_sources.append({
            "name": name,
            "entity_type": entity_type,
            "loader_path": loader_path,
        })

    def declare_open_migration(
        self,
        fn: "Callable[[Document], bool]",
    ) -> None:
        """声明一个 on-open 文档变换钩子(框架机制, 零业务)。

        `fn(doc) -> bool`:文档从存储载入后(见 bootstrap.build_project_context, 紧随
        Document.open + 只读源物化), 框架依次对每个声明的钩子调 `fn(doc)`。mod 借此就地升级
        持久化项目 —— 把旧字段归一到当前声明的 schema、补迁移默认值、按刷新后的 schema 重挂
        校验器等(editor-architecture Reader 语义:外部存储 → 当前 Document)。返回 True 表示
        改动了 doc, 框架据此落盘一次(多个钩子的返回值取或)。

        与只读源物化同源:每次装载都重跑(声明随代码现取, Document.open 不恢复源注册), 故钩子
        须**幂等** —— 已是当前态时返回 False、不重复写盘。框架不解释 fn 做什么(schema 迁移 /
        数据回填皆可), 全是 mod 策略。缺省(无声明)→ 该 skill 装载路径零行为变化(向后兼容)。
        """
        self._open_migrations.append(fn)

    def declare_harvest_rule(
        self,
        *,
        when_role: str,
        into_bucket: str,
        supply_required: dict[str, Any] | None = None,
    ) -> None:
        """声明 broker 机会式收割规则(L1):凡携带 `when_role`(库字段角色 id)的实体 → 收进
        `into_bucket`,缺的标量必填由 `supply_required` 常量补齐。合法性在 build_document 校验
        (when_role 词汇 / into_bucket 须声明 / 角色+supply 须覆盖目标必填 / 目标无必填 entity_ref)。"""
        self._harvest_rules.append({
            "when_role": when_role,
            "into_bucket": into_bucket,
            "supply_required": dict(supply_required or {}),
        })

    def set_demo_project(
        self,
        *,
        name: str,
        bootstrap: Callable[["Document"], Any],
    ) -> None:
        self._demo = {"name": name, "bootstrap": bootstrap}
        # 兼容路径:同时登记为 "default" 模板,纳入多模板 catalog(demo_templates)。
        # 老 .demo 访问器与 create_project(seed) 路径不变 —— 单 demo 工具零改动即可被
        # 新 /api/demo-templates 列出。已显式 register 过 default 则不重复(避免冲突)。
        if not any(t["id"] == "default" for t in self._demo_templates):
            self.register_demo_template(id="default", name=name, bootstrap=bootstrap)

    def register_demo_template(
        self,
        *,
        id: str,
        name: str,
        description: str = "",
        category: str = "",
        business_scene: str = "",
        scenario_path: str = "",
        bootstrap: Callable[["Document"], Any] | None = None,
        manual_hints: dict[str, Any] | None = None,
        walkthrough: list[Any] | None = None,
    ) -> None:
        """注册一个 demo 模板(多模板 catalog 的一条)。

        - `id` 须是 path-safe slug([A-Za-z0-9_-]):它会拼进沙盒项目 id
          `__demo__<user_hash>__<id>`,带分隔符会被路径穿越护栏拦掉。
        - `bootstrap` 与 `scenario_path` 二选一(至少给一个);两者都给时 scenario_path
          优先(数据化模板),bootstrap 兜底。实例化见 demo_scenario.instantiate_scenario。
        - 函数引用(bootstrap)留在内存;API 元数据序列化前须剔除(见 server demo-templates)。
        """
        if not id or not all(c.isalnum() or c in "-_" for c in id):
            raise ZeError(
                "validation_failed",
                f"demo template id 须为 slug [A-Za-z0-9_-]: {id!r}",
                context={"id": id},
            )
        if any(t["id"] == id for t in self._demo_templates):
            raise ZeError(
                "validation_failed",
                f"demo template {id!r} already registered",
                context={"id": id},
            )
        if bootstrap is None and not scenario_path:
            raise ZeError(
                "validation_failed",
                f"demo template {id!r} 须给 bootstrap 或 scenario_path(至少一个)",
                context={"id": id},
            )
        self._demo_templates.append({
            "id": id,
            "name": name,
            "description": description,
            "category": category,
            "business_scene": business_scene,
            "scenario_path": scenario_path,
            "bootstrap": bootstrap,
            "manual_hints": dict(manual_hints) if manual_hints else None,
            "walkthrough": list(walkthrough) if walkthrough else None,
        })

    def register_demo_catalog(self, catalog_path: str | os.PathLike) -> None:
        """读 demo/catalog.json(模板清单)逐条注册;scenario 相对 catalog 目录解析为绝对路径。

        catalog.json 形如::

            {"templates": [
              {"id": "...", "name": "...", "description": "...", "category": "...",
               "business_scene": "...", "scenario": "scenarios/<id>/manifest.json"},
              ...]}

        数据化优先(新工具走这条,不把 demo 内容写死 Python)。catalog 缺失/损坏直接抛,
        不静默吞 —— demo 是产品能力,缺内容应显式失败而非装作没 demo。
        """
        cat = Path(catalog_path)
        raw = json.loads(cat.read_text(encoding="utf-8"))
        base = cat.resolve().parent
        for item in raw.get("templates", []):
            scen = item.get("scenario") or item.get("scenario_path") or ""
            scenario_path = ""
            if scen:
                # 穿越护栏:scenario 必须落在 catalog 所在目录内(对标 demo_scenario 的 $asset 护栏),
                # 杜绝恶意/手误 catalog 用 ../ 引出目录外文件(纵深防御:catalog 当前是可信源)。
                target = (base / scen).resolve()
                if target != base and base not in target.parents:
                    raise ZeError(
                        "validation_failed",
                        f"scenario 路径越出 catalog 目录: {scen!r}",
                        context={"scenario": scen},
                    )
                scenario_path = str(target)
            self.register_demo_template(
                id=item["id"],
                name=item.get("name", item["id"]),
                description=item.get("description", ""),
                category=item.get("category", ""),
                business_scene=item.get("business_scene", ""),
                scenario_path=scenario_path,
                manual_hints=item.get("manual_hints"),
                walkthrough=item.get("walkthrough"),
            )

    # ---- accessors (framework 用) ----

    @property
    def entity_types(self) -> dict[str, dict[str, Any]]:
        return _clone_json_value(self._entity_types)

    @property
    def mutation_policies(self) -> dict[str, dict[str, Any]]:
        return _clone_json_value(self._mutation_policies)

    @property
    def collaboration_mode(self) -> str | None:
        return self._collaboration_mode

    @property
    def save_policy(self) -> str | None:
        return self._save_policy

    @property
    def read_only_sources(self) -> list[dict[str, Any]]:
        return [dict(s) for s in self._read_only_sources]

    @property
    def open_migrations(self) -> "list[Callable[[Document], bool]]":
        """已声明的 on-open 文档变换钩子(build_project_context 装载后依次调用)。"""
        return list(self._open_migrations)

    @property
    def demo(self) -> dict[str, Any] | None:
        return dict(self._demo) if self._demo else None

    @property
    def demo_templates(self) -> list[dict[str, Any]]:
        """已注册的 demo 模板列表(含 bootstrap 函数引用;API 层须剔除函数后再序列化)。"""
        return [dict(t) for t in self._demo_templates]

    # ---- build_document (framework 启动时调) ----

    def build_document(
        self,
        *,
        schema_version: str,
        name: str = "",
        id: str | None = None,
        config: dict[str, Any] | None = None,
        acl: dict[str, Any] | None = None,
    ) -> Document:
        """根据已收集声明造一个 Document.

        必须先 set_collaboration_mode + 至少 declare 1 个 entity_type, 否则抛 ZeError.
        """
        if self._collaboration_mode is None:
            raise ZeError(
                "validation_failed",
                "collaboration_mode not set; call registry.set_collaboration_mode() first",
            )
        if not self._entity_types:
            raise ZeError(
                "validation_failed",
                "no entity_type declared; call registry.declare_entity_type() at least once",
            )
        schema = {k: v["schema"] for k, v in self._entity_types.items()}
        # 收割规则 fail-fast 校验(下游真把关:非法 rule 让 skill 启动即炸,不带病上线)
        for rule in self._harvest_rules:
            validate_harvest_rule(rule, schema)
        return Document(
            schema_version=schema_version,
            collaboration_mode=self._collaboration_mode,
            name=name,
            id=id,
            schema=schema,
            mutation_policies=self._mutation_policies,
            config=config,
            acl=acl,
            harvest_rules=self._harvest_rules,
            save_policy=self._save_policy,
        )


# ==============================================================
# broker 收割规则校验(build_document fail-fast + GATE-TYPE-11 共用单一真源)
# ==============================================================

def validate_harvest_rule(rule: dict[str, Any], schema: dict[str, Any]) -> None:
    """校验一条 broker 收割规则(L1)对当前文档 schema 合法;非法抛 validation_failed。

    ① when_role 须是库字段/簇角色 id(ze.field.<x>@N / ze.cluster.<x>@N);
    ② into_bucket 须已声明;
    ③ into_bucket 须有字段携带 when_role(x-ze-use==when_role)—— 收割值的落点;
    ④ into_bucket 必填集须被 {该角色字段名} ∪ supply_required.keys() 全覆盖;
    ⑤ into_bucket 任何必填字段都不是 entity_ref(常量/角色补不出活引用 —— 实施诚实修正:
       设计举的 bbox→annotation 因 annotation.target 是必填 ref 被本条拒)。
    """
    import re
    when_role = rule.get("when_role")
    into_bucket = rule.get("into_bucket")
    supply = rule.get("supply_required") or {}
    if not (isinstance(when_role, str)
            and re.match(r"^ze\.(field|cluster)\.[a-z][a-z0-9_]*@\d+$", when_role)):
        raise ZeError("validation_failed",
                      f"harvest_rule.when_role 须是 ze.field/cluster.<x>@N: {when_role!r}",
                      context={"when_role": when_role})
    tschema = schema.get(into_bucket)
    if not isinstance(tschema, dict):
        raise ZeError("validation_failed",
                      f"harvest_rule.into_bucket {into_bucket!r} 未声明",
                      context={"into_bucket": into_bucket})
    props = tschema.get("properties") or {}
    required = set(tschema.get("required") or [])
    for k, sub in props.items():
        if isinstance(sub, dict) and sub.get("required") is True:
            required.add(k)
    role_field = next((k for k, sub in props.items()
                       if isinstance(sub, dict) and sub.get("x-ze-use") == when_role), None)
    if role_field is None:
        raise ZeError("validation_failed",
                      f"into_bucket {into_bucket!r} 无字段携带角色 {when_role}(收割值无落点)",
                      context={"into_bucket": into_bucket, "when_role": when_role})
    ref_required = sorted(k for k in required
                          if isinstance(props.get(k), dict) and props[k].get("type") == "entity_ref")
    if ref_required:
        raise ZeError("validation_failed",
                      f"into_bucket {into_bucket!r} 含必填 entity_ref {ref_required} —— 常量补不出活引用,不可收割",
                      context={"into_bucket": into_bucket, "ref_required": ref_required})
    missing = sorted(required - ({role_field} | set(supply.keys())))
    if missing:
        raise ZeError("validation_failed",
                      f"harvest_rule 未覆盖 into_bucket {into_bucket!r} 必填 {missing}"
                      f"(角色落 {role_field}、supply 给 {sorted(supply)})",
                      context={"into_bucket": into_bucket, "missing": missing})


# ==============================================================
# 引用图扫描器 (default edge_lookup for detect_cycles / cascade_delete)
# ==============================================================

def _default_ref_extractor(entity: Entity) -> list[tuple[str, str]]:
    """默认 edge_lookup: 引用边 = {"$ref":"type/id"} 字典(递归) ∪ x-ze-ref 标注的裸 id 字段.

    P2 决定2(正向边):mod 的跨实体引用多是**裸字符串 id**(spriteId/parentId/cameraId),
    此前对引用图隐形。给 schema 字段加 `x-ze-ref:<targetType>` 标注后,本扫描器把该字段的
    非空串值当一条边 `(targetType, 值)` → detect_cycles / cascade_delete 自动覆盖,**数据形态不变**。
    (反向清空——删 X 时清空指向 X 的字段——需反向引用索引,是另一机制,不在本扫描器。)
    v1 只扫**顶层** schema 属性的 x-ze-ref(嵌套/数组元素 ref 留 ROADMAP)。
    """
    data = entity.data
    out: list[tuple[str, str]] = []

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            ref = v.get("$ref")
            if isinstance(ref, str) and "/" in ref:
                t, i = ref.split("/", 1)
                out.append((t, i))
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(data)

    # x-ze-ref 标注的裸 id 字段(顶层) → 正向边。schema 私有属,extractor 在 core 内访问。
    schema = getattr(entity, "_schema", None)
    if isinstance(schema, dict):
        props = schema.get("properties") if "properties" in schema else schema
        if isinstance(props, dict) and isinstance(data, dict):
            for field, sub in props.items():
                if not isinstance(sub, dict):
                    continue
                ref_type = sub.get("x-ze-ref")
                if isinstance(ref_type, str) and ref_type:
                    val = data.get(field)
                    if isinstance(val, str) and val:
                        out.append((ref_type, val))

    return out
