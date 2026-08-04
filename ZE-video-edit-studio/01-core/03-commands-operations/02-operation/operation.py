"""Operation — 数据变更日志单元 (Patch + Operation + new_operation + OpLog).

完整 SPEC 见同目录 SPEC.md.

已实装 (无跨叶子依赖):
  - Patch:六个旧字段 + provenance / position / ancestry (frozen；由 _protocol 中立叶子拥有并在此 re-export)
  - Operation: id / command_id / source / actor / ts / patches / reverse_patches
               / group_id / trace_id / kind / error / document_id / session_id / external
               / pipeline (frozen; optional members omitted from to_dict when absent)
  - new_operation(...) 工厂: 校验 source ∈ SOURCES, 默认 id / ts / trace_id
  - OpLog: 内存 / NDJSON 持久化双模式;append-only + hash chain + file fsync
           + flock 续写 + 未换行崩溃尾物理修复
  - by_entity / by_source 过滤

待补 (真依赖别的叶子):
  - OpLog 分片 / 压缩 / 历史索引        [02-data-flow]
  - 协作同步 (op 推到远端)              [02-data-flow/01-realtime-sync]
  - 跨 skill trace_id 串联              [03-commands-operations/04-cross-skill-call]

已落地的 undo / redo 控制器由 [02-data-flow/04-history-rollback] 消费本叶子的值与日志合同。
"""

from __future__ import annotations

import copy
import hmac
import json
import math
import os
import stat
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# fcntl 仅 Unix 有；Windows（本地开发）优雅降级为 no-op 锁。flock 只保证异常并发下**文件不损坏**，
# SKILL 明令禁止并发写，本地单进程无跨进程并发，降级安全。整树其他 fcntl 点已守，此叶子曾唯一硬 import。
try:
    import fcntl
    _LOCK_EX, _LOCK_SH, _LOCK_UN = fcntl.LOCK_EX, fcntl.LOCK_SH, fcntl.LOCK_UN
except ImportError:  # pragma: no cover - Windows 无 fcntl
    fcntl = None  # type: ignore[assignment]
    _LOCK_EX = _LOCK_SH = _LOCK_UN = 0


def _flock(fileobj: Any, operation: int) -> None:
    """fcntl.flock 的可移植封装：有 fcntl 时加/解锁，无（Windows）时 no-op。"""
    if fcntl is not None:
        fcntl.flock(fileobj.fileno(), operation)

# 共享 ZeError: 从 02-entity 兄弟目录 import (跟 document / asset 一致)
_ENTITY_DIR = Path(__file__).resolve().parent.parent.parent / "01-data-structure" / "02-entity"
_PERSISTENCE_DIR = Path(__file__).resolve().parent.parent.parent / "02-data-flow" / "07-persistence"
_PROTOCOL_DIR = Path(__file__).resolve().parent.parent.parent / "_protocol"
for _directory in (_ENTITY_DIR, _PERSISTENCE_DIR, _PROTOCOL_DIR):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

from entity import ZeError  # noqa: E402
from mutation_patch import (  # noqa: E402
    DOCUMENT_TARGET,
    ENTITY_META_FIELD,
    EXTERNAL_TARGET,
    OPS,
    Patch,
    own_operation_value as _own_operation_value,
)
from operation_record_codec import (  # noqa: E402
    MAX_OPERATION_RECORD_BYTES,
    OperationRecordCodecError,
    ZERO_OPERATION_HASH,
    decode_strict_json,
    encode_operation_chain_entry,
    freeze_operation_body,
    materialize_operation_body,
    _normalize_pipeline_operation_metadata,
    _pipeline_operation_schema,
    _validate_pipeline_operation_metadata,
    operation_body_hash,
    operation_entries_form_chain,
)
from operation_public_projection import (  # noqa: E402
    _project_public_history_entries,
)
from history_authority import (  # noqa: E402
    abort_history_append,
    authorize_history_append,
    consume_history_append,
)


_PIPELINE_AUTHORITY_SCHEMAS = frozenset(
    ("ze.pipeline-start.v2", "ze.pipeline-control.v2")
)
_PIPELINE_AUTHORITY_OPERATION_CAPABILITY = object()
_PIPELINE_AUTHORITY_APPEND_HOOK_CAPABILITY = object()
_PIPELINE_AUTHORITY_APPEND_HOOKS: tuple[Any, Any, Any] | None = None
_LOWER_HEX = frozenset("0123456789abcdef")


def _install_pipeline_authority_append_hooks(
    *,
    claim: Any,
    complete: Any,
    abort: Any,
    _capability: object,
) -> None:
    """Install the private v2 writer guard without importing upward here."""

    global _PIPELINE_AUTHORITY_APPEND_HOOKS
    if _capability is not _PIPELINE_AUTHORITY_APPEND_HOOK_CAPABILITY:
        raise TypeError("invalid Pipeline append-hook capability")
    if not all(callable(value) for value in (claim, complete, abort)):
        raise TypeError("Pipeline append hooks must be callable")
    hooks = (claim, complete, abort)
    if _PIPELINE_AUTHORITY_APPEND_HOOKS is None:
        _PIPELINE_AUTHORITY_APPEND_HOOKS = hooks
        return
    if any(
        current is not candidate
        for current, candidate in zip(_PIPELINE_AUTHORITY_APPEND_HOOKS, hooks)
    ):
        raise RuntimeError("Pipeline append authority hooks are already installed")


def _begin_pipeline_authority_append(
    op_log: Any,
    operation: Any,
) -> tuple[Any, Any, Any] | None:
    """Claim an exact one-shot v2 append authorization before writer I/O."""

    pipeline = (
        object.__getattribute__(operation, "pipeline")
        if type(operation) is Operation
        else None
    )
    try:
        schema = _pipeline_operation_schema(pipeline)
    except OperationRecordCodecError:
        schema = None

    hooks = _PIPELINE_AUTHORITY_APPEND_HOOKS
    if hooks is None and schema in _PIPELINE_AUTHORITY_SCHEMAS:
        try:
            __import__("pipeline_authority_commit")
        except Exception as exc:
            raise ZeError(
                "pipeline_authority_append_required",
                "Pipeline v2 append authority is unavailable",
                context={"reason": "pipeline_authority_append_module_unavailable"},
            ) from exc
        hooks = _PIPELINE_AUTHORITY_APPEND_HOOKS
        if hooks is None:
            raise ZeError(
                "pipeline_authority_append_required",
                "Pipeline v2 append authority is unavailable",
                context={"reason": "pipeline_authority_append_hooks_missing"},
            )
    if hooks is None:
        return None
    claim = hooks[0](op_log, operation)
    if claim is None:
        return None
    return claim, hooks[1], hooks[2]


# ==============================================================
# 常量
# ==============================================================

SOURCES = ("user", "ai", "sync", "system")
KINDS = ("native", "stage", "compose", "rollback")
ZERO_HASH = ZERO_OPERATION_HASH

# 持久化 OpLog 是本机可信边界的一部分。上限保持为内部实现细节，既不扩大公开 API，
# 也让调用方不能用配置把防内存/磁盘耗尽护栏关闭。
_MAX_OPLOG_BYTES = 256 * 1024 * 1024
_MAX_OPLOG_RECORD_BYTES = MAX_OPERATION_RECORD_BYTES


class _StrictJsonError(ValueError):
    """Strict NDJSON decoder 的内部哨兵；不得携带日志里的敏感值。"""


def _strict_json_record(raw: bytes) -> Any:
    """Compatibility wrapper over the persistence-owned single codec."""
    try:
        decoded = decode_strict_json(raw, maximum_bytes=_MAX_OPLOG_RECORD_BYTES)
        # Reject a hand-resigned unknown/oversized Pipeline descriptor before
        # retaining this row in the loaded history.  Full legacy Operation
        # semantics intentionally remain outside this compatibility reader.
        if type(decoded) is dict and type(decoded.get("op")) is dict:
            _validate_pipeline_operation_metadata(decoded["op"])
        return decoded
    except OperationRecordCodecError:
        raise _StrictJsonError("invalid strict JSON record") from None


# ==============================================================
# Operation
# ==============================================================

@dataclass(frozen=True)
class Operation:
    """完整变更记录. 由 Command.execute() 产生, 落 OpLog.

    构造请用 new_operation(...) 工厂 (自动填默认值 + 校验).
    """
    id: str
    command_id: str
    source: str
    actor: dict[str, Any]
    ts: float
    patches: tuple[Patch, ...]
    reverse_patches: tuple[Patch, ...]
    group_id: str = ""
    trace_id: str = ""
    kind: str = "native"
    error: dict[str, Any] | None = None
    document_id: str = ""
    session_id: str = ""
    # 外部副作用 provenance 信封(AI 生成 / 出网调用 / 探测);None = 无外部效果(纯本地变更)。
    external: dict[str, Any] | None = None
    # Pipeline Start/Control 的耐久业务描述符；None 时完全省略以保持旧 hash。
    pipeline: dict[str, Any] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        for name in (
            "id", "command_id", "source", "group_id", "trace_id", "kind",
            "document_id", "session_id",
        ):
            if type(object.__getattribute__(self, name)) is not str:
                raise TypeError(f"Operation.{name} must be an exact string")
        ts = object.__getattribute__(self, "ts")
        if type(ts) not in (int, float) or not math.isfinite(ts):
            raise TypeError("Operation.ts must be a finite exact number")

        actor = _own_operation_value(object.__getattribute__(self, "actor"))
        if type(actor) is not dict:
            raise TypeError("Operation.actor must be an exact dict")
        object.__setattr__(
            self,
            "actor",
            actor,
        )

        patches = object.__getattribute__(self, "patches")
        reverse_patches = object.__getattribute__(self, "reverse_patches")
        for name, values in (
            ("patches", patches),
            ("reverse_patches", reverse_patches),
        ):
            if type(values) not in (list, tuple):
                raise TypeError(f"Operation.{name} must be an exact list or tuple")
            if any(type(value) is not Patch for value in values):
                raise TypeError(f"Operation.{name} must contain exact Patch values")
        object.__setattr__(
            self,
            "patches",
            tuple(patches),
        )
        object.__setattr__(
            self,
            "reverse_patches",
            tuple(reverse_patches),
        )

        error = _own_operation_value(object.__getattribute__(self, "error"))
        external = _own_operation_value(object.__getattribute__(self, "external"))
        for name, value in (("error", error), ("external", external)):
            if value is not None and type(value) is not dict:
                raise TypeError(f"Operation.{name} must be an exact dict or None")
        object.__setattr__(
            self,
            "error",
            error,
        )
        object.__setattr__(
            self,
            "external",
            external,
        )
        pipeline_input = object.__getattribute__(self, "pipeline")
        if pipeline_input is None:
            pipeline = None
        else:
            # v2 is an authority-bearing durable fact.  General construction is
            # permanently closed even though the lower record codec can now
            # encode v2.  Only the private reserved-id factory below may attach
            # one after the ordinary Operation envelope has been validated.
            try:
                pipeline_schema = _pipeline_operation_schema(pipeline_input)
            except OperationRecordCodecError:
                raise TypeError(
                    "Operation.pipeline must be a valid known Pipeline descriptor"
                ) from None
            if pipeline_schema in _PIPELINE_AUTHORITY_SCHEMAS:
                raise TypeError(
                    "Pipeline v2 descriptors require the sealed authority factory"
                )
            try:
                pipeline = _normalize_pipeline_operation_metadata(pipeline_input)
            except OperationRecordCodecError:
                raise TypeError(
                    "Operation.pipeline must be a valid known Pipeline descriptor"
                ) from None
        object.__setattr__(self, "pipeline", pipeline)

    def __getattribute__(self, name: str) -> Any:
        if name == "__dict__":
            state = object.__getattribute__(self, "__dict__")
            return {
                key: copy.deepcopy(value)
                for key, value in state.items()
            }
        value = object.__getattribute__(self, name)
        if name in {"actor", "error", "external", "pipeline"}:
            return copy.deepcopy(value)
        return value

    def __reduce_ex__(self, _protocol: int) -> tuple[Any, tuple[Any, ...]]:
        state = object.__getattribute__(self, "__dict__")
        names = [
            "id", "command_id", "source", "actor", "ts", "patches",
            "reverse_patches", "group_id", "trace_id", "kind", "error",
            "document_id", "session_id", "external",
        ]
        # Keep the historical 14-argument pickle shape when the additive
        # carrier is absent; old runtimes can still unpickle those values.
        if state["pipeline"] is not None:
            names.append("pipeline")
        return (
            type(self),
            tuple(
                copy.deepcopy(state[name])
                for name in names
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return self._operation_dict(include_pipeline=True)

    def _public_operation_payload(self) -> dict[str, Any]:
        """Return the transport body without ever copying Pipeline authority."""
        return self._operation_dict(include_pipeline=False)

    def _operation_dict(self, *, include_pipeline: bool) -> dict[str, Any]:
        operation_id = object.__getattribute__(self, "id")
        command_id = object.__getattribute__(self, "command_id")
        source = object.__getattribute__(self, "source")
        actor = object.__getattribute__(self, "actor")
        ts = object.__getattribute__(self, "ts")
        patches = object.__getattribute__(self, "patches")
        reverse_patches = object.__getattribute__(self, "reverse_patches")
        group_id = object.__getattribute__(self, "group_id")
        trace_id = object.__getattribute__(self, "trace_id")
        kind = object.__getattribute__(self, "kind")
        error = object.__getattribute__(self, "error")
        document_id = object.__getattribute__(self, "document_id")
        session_id = object.__getattribute__(self, "session_id")
        external = object.__getattribute__(self, "external")
        pipeline = (
            object.__getattribute__(self, "pipeline")
            if include_pipeline
            else None
        )
        d: dict[str, Any] = {
            "id": operation_id,
            "command_id": command_id,
            "source": source,
            "actor": copy.deepcopy(actor),
            "ts": ts,
            "patches": [p.to_dict() for p in patches],
            "reverse_patches": [p.to_dict() for p in reverse_patches],
            "group_id": group_id,
            "trace_id": trace_id,
            "kind": kind,
            "error": copy.deepcopy(error) if error is not None else None,
        }
        # document/session 是 realtime_checkpoint 新协议字段。空值省略以保持历史
        # oplog hash body 不变; 一旦调用方显式传入,它们就成为审计维度。
        if document_id != "":
            d["document_id"] = document_id
        if session_id != "":
            d["session_id"] = session_id
        # external 为 None 时**整键省略** —— 历史 oplog 的 self_hash 不含此键,加键会改 hash 断链;
        # 省略保证既有持久化 oplog verify_chain() 不变(只有真带外部效果的新 op 才出现 external)。
        if external is not None:
            d["external"] = copy.deepcopy(external)
        # pipeline 与 external 分属不同所有权；只在真有描述符时进入
        # canonical body，旧 Operation 的 bytes/self_hash 因此逐字不变。
        if pipeline is not None:
            d["pipeline"] = copy.deepcopy(pipeline)
        return d


def new_operation(
    *,
    command_id: str,
    source: str,
    actor: dict[str, Any],
    patches: list[Patch] | tuple[Patch, ...],
    reverse_patches: list[Patch] | tuple[Patch, ...],
    group_id: str = "",
    trace_id: str = "",
    kind: str = "native",
    error: dict[str, Any] | None = None,
    document_id: str = "",
    session_id: str = "",
    external: dict[str, Any] | None = None,
    pipeline: dict[str, Any] | None = None,
) -> Operation:
    """Operation 工厂. 校验 source / kind; 自动填 id / ts / trace_id."""
    if type(source) is not str:
        raise ZeError(
            "validation_failed",
            "source must be an exact string",
            context={"field": "source"},
        )
    if source not in SOURCES:
        raise ZeError(
            "validation_failed",
            f"unknown source {source!r}; expected one of {SOURCES}",
            context={"source": source},
        )
    if type(kind) is not str:
        raise ZeError(
            "validation_failed",
            "kind must be an exact string",
            context={"field": "kind"},
        )
    if kind not in KINDS:
        raise ZeError(
            "validation_failed",
            f"unknown kind {kind!r}; expected one of {KINDS}",
            context={"kind": kind},
        )
    return Operation(
        id=uuid.uuid4().hex,
        command_id=command_id,
        source=source,
        actor=actor,
        ts=time.time(),
        patches=patches,
        reverse_patches=reverse_patches,
        group_id=group_id,
        trace_id=trace_id or uuid.uuid4().hex,
        kind=kind,
        error=error,
        document_id=document_id,
        session_id=session_id,
        external=external,
        pipeline=pipeline,
    )


def _new_reserved_pipeline_authority_operation(
    *,
    operation_id: str,
    command_id: str,
    source: str,
    actor: dict[str, Any],
    document_id: str,
    pipeline: dict[str, Any],
    group_id: str = "",
    trace_id: str = "",
    session_id: str = "",
    _capability: object,
) -> Operation:
    """Construct one v2 authority Operation behind the private exact seal.

    The public constructor remains unable to create or reconstruct v2 values.
    ``pipeline_authority_commit`` reserves the id before building the descriptor,
    calls this seam once, and immediately binds the returned exact object in its
    process-local seal ledger.
    """

    if _capability is not _PIPELINE_AUTHORITY_OPERATION_CAPABILITY:
        raise TypeError("invalid Pipeline authority construction capability")
    if (
        type(operation_id) is not str
        or len(operation_id) != 32
        or any(character not in _LOWER_HEX for character in operation_id)
    ):
        raise TypeError("operation_id must be 32 lowercase hexadecimal characters")
    try:
        pipeline_schema = _pipeline_operation_schema(pipeline)
    except OperationRecordCodecError:
        pipeline_schema = None
    if type(pipeline) is not dict or pipeline_schema not in _PIPELINE_AUTHORITY_SCHEMAS:
        raise TypeError("pipeline must be an exact v2 authority descriptor")
    if type(source) is not str or source not in SOURCES:
        raise ZeError(
            "validation_failed",
            f"unknown source {source!r}; expected one of {SOURCES}",
            context={"source": source},
        )

    try:
        owned_pipeline = _normalize_pipeline_operation_metadata(pipeline)
    except OperationRecordCodecError:
        raise TypeError("pipeline must be a valid v2 authority descriptor") from None

    operation = Operation(
        id=operation_id,
        command_id=command_id,
        source=source,
        actor=actor,
        ts=time.time(),
        patches=(),
        reverse_patches=(),
        group_id=group_id,
        trace_id=trace_id or uuid.uuid4().hex,
        kind="stage",
        error=None,
        document_id=document_id,
        session_id=session_id,
        external=None,
        pipeline=None,
    )
    # Frozen dataclass mutation is restricted to this private construction seam;
    # every public path above still rejects v2 before normalization.
    object.__setattr__(operation, "pipeline", owned_pipeline)
    return operation


# ==============================================================
# 外部副作用 provenance 信封(语言中立;什么字段算业务由 mod 决定)
# ==============================================================

def external_ai(
    prov: Any,
    *,
    target_ref: dict[str, str] | None = None,
    status: str = "ok",
    latency_ms: int = 0,
) -> dict[str, Any]:
    """AI 生成类外部效果信封。prov = AiProvenance(复用其 to_dict:model/cost/token/trace)。

    target_ref 指向被监视的 __external__ 实体({entity_type,entity_id})。
    """
    env: dict[str, Any] = {
        "kind": "ai_generation",
        "provenance": prov.to_dict() if hasattr(prov, "to_dict") else dict(prov),
        "status": status,
        "latency_ms": latency_ms,
    }
    if target_ref:
        env["target_ref"] = dict(target_ref)
    return env


def external_api(
    endpoint: str,
    *,
    target_ref: dict[str, str] | None = None,
    request_id: str = "",
    status: str = "ok",
    latency_ms: int = 0,
) -> dict[str, Any]:
    """出网调用类外部效果信封。endpoint 只记 host/path(**不放密钥**)。"""
    env: dict[str, Any] = {
        "kind": "outbound_api",
        "endpoint": endpoint,
        "request_id": request_id,
        "status": status,
        "latency_ms": latency_ms,
    }
    if target_ref:
        env["target_ref"] = dict(target_ref)
    return env


# ==============================================================
# 命令确认门控(两段式:dispatch 返回待确认 → confirm 才执行)
# ==============================================================

@dataclass(frozen=True)
class PendingConfirmation:
    """一次"需确认"命令的冻结快照。dispatch 命中 requires_confirm 时返回它(零变更:
    不 append、不广播)。只有具备一次性 nonce 的网页人工通道可 reserve/confirm；AI transport
    只能发起或取消本人 pending，不能批准。args 冻结 → 确认时不可改参。
    """
    token: str
    command_id: str
    args: dict[str, Any]
    source: str
    actor: dict[str, Any]
    ts: float
    summary: str = ""
    trace_id: str = ""
    approval_nonce: str = ""
    # §7.4 P0-E(用户 D1 拍板方案 b,2026-07-26):这条 pending 属于哪一轮助手对话。
    # 为什么必须落在这里而不是另建登记表:取消一轮时要把该轮**全部**挂起释放,
    # 而并发两轮的确认在时间上会交织 —— 按时间窗归属会认错人。归属只有内嵌才确定。
    # 空串 = 非助手轮发起(用户在网页直接点的高危命令),不参与按轮释放。
    turn_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "command_id": self.command_id,
            "args": copy.deepcopy(self.args),
            "source": self.source,
            "actor": copy.deepcopy(self.actor),
            "ts": self.ts,
            "summary": self.summary,
            "trace_id": self.trace_id,
            "approval_nonce": self.approval_nonce,
            "turn_id": self.turn_id,
        }

    def to_public_dict(self, *, include_approval: bool = False) -> dict[str, Any]:
        """Transport-safe projection; frozen command args never leave the state machine."""
        out = {
            "token": self.token,
            "command_id": self.command_id,
            "source": self.source,
            "ts": self.ts,
            "summary": self.summary,
            "trace_id": self.trace_id,
            "approval_required": True,
        }
        if include_approval:
            out["approval_nonce"] = self.approval_nonce
        return out


def new_pending_confirmation(
    *,
    command_id: str,
    args: dict[str, Any],
    source: str,
    actor: dict[str, Any],
    summary: str = "",
    trace_id: str = "",
    turn_id: str = "",
) -> PendingConfirmation:
    """PendingConfirmation 工厂:自动填 token / ts / trace_id。

    turn_id:发起它的那一轮助手对话(§7.4 P0-E 按轮释放要它)。用户在网页直接点高危命令时
    为空 —— 那不属于任何一轮,取消某轮不该把它顺手清掉。"""
    return PendingConfirmation(
        token=uuid.uuid4().hex,
        command_id=command_id,
        args=copy.deepcopy(args),
        source=source,
        actor=copy.deepcopy(actor),
        ts=time.time(),
        summary=summary,
        trace_id=trace_id or uuid.uuid4().hex,
        approval_nonce=uuid.uuid4().hex,
        turn_id=turn_id,
    )


@dataclass(frozen=True)
class ConfirmationReservation:
    """一次确认执行权的不可伪造租约。

    token 本身只定位请求；reservation_id 才证明 caller 是当前唯一执行者。提交结果
    明确前记录仍保留在 PendingStore，因而失败保存不会把确认意图提前吃掉。
    """

    token: str
    reservation_id: str
    pending: PendingConfirmation


@dataclass
class _PendingEntry:
    pending: PendingConfirmation
    state: str = "pending"       # pending | reserved | frozen
    reservation_id: str = ""
    frozen_code: str = ""
    frozen_message: str = ""


def _confirmation_actor_id(actor: Any) -> str:
    if isinstance(actor, dict):
        value = actor.get("user_id")
    else:
        value = getattr(actor, "user_id", None)
    return value if isinstance(value, str) else ""


def _copy_pending(value: PendingConfirmation) -> PendingConfirmation:
    return PendingConfirmation(
        token=value.token,
        command_id=value.command_id,
        args=copy.deepcopy(value.args),
        source=value.source,
        actor=copy.deepcopy(value.actor),
        ts=value.ts,
        summary=value.summary,
        trace_id=value.trace_id,
        approval_nonce=value.approval_nonce,
        # 漏了这行 = 一次 copy 就把归属丢掉,按轮释放会静默失效(而且不报错)。
        turn_id=value.turn_id,
    )


class PendingStore:
    """单进程确认状态机；W06 可在不改调用形状的前提下换成数据库实现。

    合法路径只有 ``put → reserve → finalize``：committed 消费，已证明回滚的
    aborted 释放供重试，dirty/indeterminate 冻结待修复。任何 caller 都不能在
    producer 开始前 pop token。
    """

    def __init__(
        self,
        *,
        max_entries: int = 4096,
        max_per_actor: int = 128,
        pending_ttl_sec: float = 900.0,
    ) -> None:
        if max_entries <= 0 or max_per_actor <= 0 or pending_ttl_sec <= 0:
            raise ValueError("confirmation capacity and ttl must be positive")
        self._entries: dict[str, _PendingEntry] = {}
        self._lock = threading.RLock()
        self._max_entries = int(max_entries)
        self._max_per_actor = int(max_per_actor)
        self._pending_ttl_sec = float(pending_ttl_sec)

    def _prune_expired_pending(self, now: float) -> None:
        expired = [
            token for token, entry in self._entries.items()
            if entry.state == "pending" and now - entry.pending.ts > self._pending_ttl_sec
        ]
        for token in expired:
            self._entries.pop(token, None)

    def put(self, pending: PendingConfirmation) -> None:
        if not isinstance(pending, PendingConfirmation):
            raise TypeError("pending must be PendingConfirmation")
        with self._lock:
            self._prune_expired_pending(time.time())
            if pending.token in self._entries:
                raise ZeError(
                    "confirmation_token_collision",
                    "confirmation token already exists",
                    context={"token": pending.token},
                )
            actor_id = _confirmation_actor_id(pending.actor)
            actor_count = sum(
                1 for entry in self._entries.values()
                if _confirmation_actor_id(entry.pending.actor) == actor_id
            )
            if len(self._entries) >= self._max_entries or actor_count >= self._max_per_actor:
                raise ZeError(
                    "confirmation_capacity_exceeded",
                    "too many pending confirmations",
                    context={"actor": actor_id},
                )
            self._entries[pending.token] = _PendingEntry(_copy_pending(pending))

    def peek(self, token: str) -> PendingConfirmation | None:
        """只读查看一条 pending(不取执行权、不消费、不改状态)。

        供网页人工批准 UI 拉取展示/批准材料(经 cookie 专属端点);过期即不可见。
        只暴露 pending 态——已 reserved/frozen 的请求不再对外呈现,避免双通道抢确认。
        """
        with self._lock:
            self._prune_expired_pending(time.time())
            entry = self._entries.get(token)
            if entry is None or entry.state != "pending":
                return None
            return _copy_pending(entry.pending)

    def reserve(
        self,
        token: str,
        *,
        actor: Any,
        approval_channel: str,
        approval_nonce: str,
    ) -> ConfirmationReservation:
        """原子取得唯一执行权，但不消费 token。"""
        with self._lock:
            self._prune_expired_pending(time.time())
            entry = self._entries.get(token)
            if entry is None:
                raise ZeError(
                    "confirm_token_unknown",
                    f"unknown or already-consumed confirm token {token!r}",
                    context={"token": token},
                )
            expected_actor = _confirmation_actor_id(entry.pending.actor)
            actual_actor = _confirmation_actor_id(actor)
            if not expected_actor or actual_actor != expected_actor:
                raise ZeError(
                    "confirmation_actor_mismatch",
                    "confirmation token belongs to another actor",
                    context={"token": token},
                )
            if (
                approval_channel != "human_http"
                or not approval_nonce
                or not hmac.compare_digest(approval_nonce, entry.pending.approval_nonce)
            ):
                raise ZeError(
                    "confirmation_human_required",
                    "confirmation requires an authenticated human approval nonce",
                    context={"token": token},
                )
            if entry.state == "frozen":
                raise ZeError(
                    "confirmation_repair_required",
                    entry.frozen_message or "confirmation outcome requires repair",
                    context={
                        "token": token,
                        "cause_code": entry.frozen_code or "confirmation_outcome_unknown",
                    },
                )
            if entry.state == "reserved":
                raise ZeError(
                    "confirmation_in_progress",
                    "confirmation token is already being executed",
                    context={"token": token},
                )
            reservation_id = uuid.uuid4().hex
            entry.state = "reserved"
            entry.reservation_id = reservation_id
            return ConfirmationReservation(
                token=token,
                reservation_id=reservation_id,
                pending=_copy_pending(entry.pending),
            )

    def pending_for(self, reservation: ConfirmationReservation) -> PendingConfirmation:
        with self._lock:
            entry = self._require_reservation(reservation)
            return _copy_pending(entry.pending)

    def release(self, reservation: ConfirmationReservation) -> None:
        """仅用于已证明存储未触碰/producer 已成功回滚的失败。"""
        with self._lock:
            entry = self._require_reservation(reservation)
            entry.state = "pending"
            entry.reservation_id = ""

    def consume(self, reservation: ConfirmationReservation) -> None:
        with self._lock:
            self._require_reservation(reservation)
            self._entries.pop(reservation.token, None)

    def freeze(
        self,
        reservation: ConfirmationReservation,
        *,
        code: str,
        message: str,
    ) -> None:
        with self._lock:
            entry = self._require_reservation(reservation)
            entry.state = "frozen"
            entry.reservation_id = ""
            entry.frozen_code = code or "confirmation_outcome_unknown"
            entry.frozen_message = message or "confirmation outcome requires repair"

    def finalize(self, reservation: ConfirmationReservation, execution: Any) -> str:
        """按 CommitExecution 的耐久事实消费、释放或冻结，返回最终动作名。"""
        receipt = getattr(execution, "receipt", None)
        status = getattr(getattr(receipt, "status", None), "value", "")
        if status == "committed":
            self.consume(reservation)
            return "consumed"
        failure = getattr(receipt, "failure", None)
        facts = getattr(failure, "facts", None)
        if status == "aborted" and bool(getattr(facts, "rollback_succeeded", False)):
            self.release(reservation)
            return "released"
        code = str(getattr(failure, "code", "") or f"commit_{status or 'outcome_missing'}")
        message = str(
            getattr(failure, "message", "")
            or "confirmation commit outcome is not safe to replay"
        )
        self.freeze(reservation, code=code, message=message)
        return "frozen"

    def cancel(self, token: str, *, actor: Any) -> bool:
        """只有创建者可取消 pending；reserved/frozen 都不能伪装成未执行。"""
        with self._lock:
            entry = self._entries.get(token)
            if entry is None:
                return False
            if _confirmation_actor_id(actor) != _confirmation_actor_id(entry.pending.actor):
                raise ZeError(
                    "confirmation_actor_mismatch",
                    "confirmation token belongs to another actor",
                    context={"token": token},
                )
            if entry.state == "reserved":
                raise ZeError(
                    "confirmation_in_progress",
                    "confirmation token is already being executed",
                    context={"token": token},
                )
            if entry.state == "frozen":
                raise ZeError(
                    "confirmation_repair_required",
                    entry.frozen_message or "confirmation outcome requires repair",
                    context={"token": token, "cause_code": entry.frozen_code},
                )
            self._entries.pop(token, None)
            return True

    def tokens_for_turn(self, turn_id: str, *, actor: Any) -> list[str]:
        """列出某一轮里**本人**发起、且仍可取消(state == pending)的挂起 token。

        §7.4 P0-E(用户 D1 拍板方案 b)。只读:不改状态,给调用方去逐个 cancel。
        - 只认 state=="pending":reserved/frozen 正在执行或需修复,取消整轮不该动它们
          (那会把一次执行到一半的高危命令的状态搞乱),留给各自的既有路径处理。
        - 只认同一 actor:与 cancel() 同规矩,不给跨人释放开旁路。
        - turn_id 为空一律不匹配:空 = 非助手轮发起的,不属于任何一轮。
        """
        if not turn_id:
            return []
        want = _confirmation_actor_id(actor)
        with self._lock:
            self._prune_expired_pending(time.time())
            return [
                token for token, entry in self._entries.items()
                if entry.state == "pending"
                and entry.pending.turn_id == turn_id
                and _confirmation_actor_id(entry.pending.actor) == want
            ]

    def state(self, token: str) -> str | None:
        """只读诊断；不暴露冻结 args/actor。"""
        with self._lock:
            entry = self._entries.get(token)
            return entry.state if entry is not None else None

    def _require_reservation(self, reservation: ConfirmationReservation) -> _PendingEntry:
        if not isinstance(reservation, ConfirmationReservation):
            raise TypeError("reservation must be ConfirmationReservation")
        entry = self._entries.get(reservation.token)
        if (
            entry is None
            or entry.state != "reserved"
            or entry.reservation_id != reservation.reservation_id
        ):
            raise ZeError(
                "confirmation_reservation_stale",
                "confirmation reservation is stale or no longer owns the token",
                context={"token": reservation.token},
            )
        return entry

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


# ==============================================================
# OpLog (内存版 append-only + hash chain)
# ==============================================================

class OpLog:
    """Append-only 日志, 每条 entry 带 prev_hash + self_hash 链.

    任何篡改 → self_hash 错 → verify_chain() 返 False.

    持久化(可选): 传 path 则:
      - 构造时从 path 载回已有 entries(NDJSON, 一行一 entry)并续上 tail_hash;
        载回后 verify_chain() 若失败 → 抛 oplog_corrupt(防带病续写)。
      - 每次 append 多写一行 NDJSON(追加, fsync 落盘)。
    path=None(默认) → 纯内存, 行为不变(向后兼容, 既有测试不破)。
    """

    def __init__(self, path: "str | Path | None" = None) -> None:
        self._entries: list[dict[str, Any]] = []
        self._tail_hash = ZERO_HASH
        self._path = Path(path) if path else None
        self._lock = threading.RLock()
        self._truncated_tail_dropped = False   # _load 软恢复掉一个截断末行时置 True(供 boot 上报)
        self._last_persisted_stat: tuple[int, int, int, int, int] | None = None
        if self._path is not None:
            try:
                self._load()
            except FileNotFoundError:
                # path 或 parent 尚不存在 = 合法的首次启动；首次 append 才以 0600 创建。
                pass

    def _unsafe_path(self, reason: str) -> ZeError:
        del reason
        return ZeError(
            "oplog_path_unsafe",
            "oplog path must be a private, singly-linked regular file",
        )

    def _size_limit(self, scope: str, observed: int, limit: int) -> ZeError:
        del scope, observed, limit
        return ZeError(
            "oplog_limit_exceeded",
            "oplog storage limit exceeded",
        )

    def _entry_invalid(self, reason: str) -> ZeError:
        del reason
        return ZeError(
            "oplog_entry_invalid",
            "operation cannot be encoded as a safe oplog record",
        )

    def _open_secure(self, *, create: bool):
        """以 parent dirfd + O_NOFOLLOW 打开最终文件，避免 path check/open 竞态。

        O_NONBLOCK 保证攻击者把最终路径换成 FIFO 时不会卡住进程；打开后只信 fstat，
        regular 且 nlink==1 才可用。direct parent 自身也用 O_NOFOLLOW 打开，因此 parent
        symlink 在创建和载回两条路径都会 fail closed。
        """
        assert self._path is not None
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory = getattr(os, "O_DIRECTORY", 0)
        if not nofollow or not directory:
            raise self._unsafe_path("secure_open_unsupported")

        if create:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise self._unsafe_path("parent_unavailable") from None

        parent_flags = os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)
        try:
            parent_fd = os.open(self._path.parent, parent_flags)
        except FileNotFoundError:
            if not create:
                raise
            raise self._unsafe_path("parent_disappeared") from None
        except OSError:
            raise self._unsafe_path("parent_not_real_directory") from None

        fd = -1
        try:
            flags = (
                os.O_RDWR
                | os.O_APPEND
                | os.O_NONBLOCK
                | nofollow
                | getattr(os, "O_CLOEXEC", 0)
            )
            if create:
                flags |= os.O_CREAT
            try:
                fd = os.open(self._path.name, flags, 0o600, dir_fd=parent_fd)
            except FileNotFoundError:
                if not create:
                    raise
                # Darwin 可在两个 O_CREAT|O_NOFOLLOW 首创者竞争时给输家短暂 ENOENT；
                # 固定 parent dirfd 上重试一次，不放宽任何路径约束。
                try:
                    fd = os.open(self._path.name, flags, 0o600, dir_fd=parent_fd)
                except OSError:
                    raise self._unsafe_path("target_create_failed") from None
            except OSError:
                raise self._unsafe_path("target_not_safe_file") from None

            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise self._unsafe_path("target_not_regular")
            if info.st_nlink != 1:
                raise self._unsafe_path("target_not_singly_linked")
            if info.st_uid != os.geteuid():
                raise self._unsafe_path("target_not_owned")
            if stat.S_IMODE(info.st_mode) != 0o600:
                try:
                    os.fchmod(fd, 0o600)
                except OSError:
                    raise self._unsafe_path("permissions_not_private") from None
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                ):
                    raise self._unsafe_path("permissions_not_private")

            stream = open(fd, "r+b", closefd=True)
            fd = -1
            return stream
        finally:
            if fd >= 0:
                os.close(fd)
            os.close(parent_fd)

    def _load(self) -> None:
        """从 NDJSON 文件载回 entries + 续 tail_hash + 验链(损坏即抛, 不带病续写)。

        **末行截断软恢复**: LF 是 record 提交标记。在 LOCK_EX 内先验证全部完整
        record 的 hash chain，再把唯一未换行尾片物理 truncate + fsync。任何已换行
        坏 JSON/UTF-8/hash 都 fail closed；未带 LF 的字节则统一视为未提交尾片。
        """
        self._truncated_tail_dropped = False
        with self._open_secure(create=False) as f:
            _flock(f, _LOCK_EX)
            try:
                loaded, repaired, _ = self._scan_and_repair_locked(f)
                persisted_stat = self._stat_signature(os.fstat(f.fileno()))
            finally:
                _flock(f, _LOCK_UN)
        self._entries = loaded
        self._tail_hash = loaded[-1]["self_hash"] if loaded else ZERO_HASH
        self._truncated_tail_dropped = repaired
        self._last_persisted_stat = persisted_stat

    def append(self, op: Operation) -> dict[str, Any]:
        """追加 op 到链尾; 返回 entry 深拷贝 (caller mutate 不影响内部).

        **write-ahead**: 持久化模式先落盘成功、再改内存。否则 _persist_line 失败
        (磁盘满/权限)会留下"内存有、盘上没"→ 下次重启从盘载回缺这条, hash chain
        断裂 → _load 抛 oplog_corrupt → 服务拒启动。先盘后内存保证盘 ⊇ 内存的链一致。

        **持久化模式下 self_hash 在 _persist_line 的 flock 内基于文件真实尾算**(跨进程防链分叉,
        见该方法);纯内存模式用内存 tail 算。
        """
        with self._lock:
            pipeline_append = _begin_pipeline_authority_append(self, op)
            history_permit = None
            try:
                history_permit = authorize_history_append(self, op)
                try:
                    body = op.to_dict()
                except (
                    TypeError,
                    ValueError,
                    OverflowError,
                    UnicodeError,
                    RecursionError,
                    RuntimeError,
                ):
                    raise self._entry_invalid("operation_projection_failed") from None
                if self._path is not None:
                    # 持久化 helper 在发布文件 stat 快路前一并更新内存链，避免
                    # fsync 后、外层 append 前的异步异常留下“stat 新、内存旧”。
                    result = copy.deepcopy(self._persist_line(body))
                else:
                    # 从这一行起不再读取 caller-owned 嵌套对象。否则 hash body 与完整 entry 的
                    # 两次 JSON 遍历之间被并发 mutate，会写出自哈希不匹配的内存 record。
                    body = self._freeze_body(body)
                    prev_hash = self._tail_hash
                    entry, _ = self._encode_entry(prev_hash, body)
                    self._entries.append(entry)
                    self._tail_hash = entry["self_hash"]
                    result = copy.deepcopy(entry)
                consume_history_append(
                    history_permit,
                    op_log=self,
                    operation=op,
                )
                if pipeline_append is not None:
                    claim, complete, _abort = pipeline_append
                    complete(claim, op_log=self, operation=op)
                return result
            except BaseException:
                try:
                    abort_history_append(
                        history_permit,
                        op_log=self,
                        operation=op,
                    )
                finally:
                    if pipeline_append is not None:
                        claim, _complete, abort = pipeline_append
                        abort(claim, op_log=self, operation=op)
                raise

    def _freeze_body(self, body: dict[str, Any]) -> dict[str, Any]:
        """委托唯一 codec 抓取 caller 快照并返回 OpLog 私有 JSON tree。"""
        try:
            frozen = freeze_operation_body(
                body,
                maximum_bytes=_MAX_OPLOG_RECORD_BYTES,
                json_dumps=json.dumps,
            )
            return materialize_operation_body(frozen)
        except OperationRecordCodecError:
            raise self._entry_invalid("not_finite_strict_json") from None

    def _persist_line(self, body: dict[str, Any]) -> dict[str, Any]:
        """flock 内:基于**文件真实尾 hash** 算 self_hash + 追加一行 NDJSON + fsync,返回 entry(含 prev/self_hash)。

        跨进程防链分叉:CLI 与 server 两进程各持自己的内存 tail,若都用内存 tail 算 prev_hash 会双双
        chain 到同一 H0 → 文件里出现两条同 prev_hash 的 entry → hash 链分叉 → 下次 _load 抛 oplog_corrupt
        → 该 project 全 API 500。改为在 fcntl.flock(LOCK_EX) 内读文件真实尾行的 self_hash 当 prev_hash 重算,
        保证文件 hash 链永远连续。**单写者快路**:文件字节数自本进程上次落盘未变 → 无其他写者 → 直接用内存
        tail(免读全文件、避免每次 append O(n))。快路同时比对 dev/inode/size/
        mtime/ctime，任一外部变化都回退到全量验链。并发写(SKILL 明令禁止)时输家的内存视图会暂时落后文件、
        重启 _load 追上;flock 只保证异常并发下**文件不损坏**,非支持高频并发。
        """
        with self._open_secure(create=True) as f:
            _flock(f, _LOCK_EX)
            try:
                before = self._stat_signature(os.fstat(f.fileno()))
                if before[2] > _MAX_OPLOG_BYTES:
                    raise self._size_limit("file", before[2], _MAX_OPLOG_BYTES)
                if self._last_persisted_stat is not None and before == self._last_persisted_stat:
                    loaded = self._entries
                    repaired = False
                else:
                    loaded, repaired, _ = self._scan_and_repair_locked(f)
                prev_hash = loaded[-1]["self_hash"] if loaded else ZERO_HASH
                # 先 durable repair，再冻结新 body；因此新 entry 非法也不会让已确认的尾修复丢失。
                frozen_body = self._freeze_body(body)
                entry, line = self._encode_entry(prev_hash, frozen_body)
                f.seek(0, os.SEEK_END)
                current_size = os.fstat(f.fileno()).st_size
                projected_size = current_size + len(line)
                if projected_size > _MAX_OPLOG_BYTES:
                    raise self._size_limit("file", projected_size, _MAX_OPLOG_BYTES)
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
                if repaired:
                    self._truncated_tail_dropped = True
                # Publish order is a crash/async-exception fence.  The cached
                # stat is the permission to take the next append's fast path,
                # so it must become visible only after memory contains the
                # durable entry and its matching tail hash.
                self._entries = [*loaded, entry]
                self._tail_hash = entry["self_hash"]
                self._last_persisted_stat = self._stat_signature(os.fstat(f.fileno()))
                return entry
            finally:
                _flock(f, _LOCK_UN)

    def _scan_and_repair_locked(self, f) -> tuple[list[dict[str, Any]], bool, int]:
        """LOCK_EX 内验证完整 record，并物理删掉唯一允许的未换行尾片。"""
        file_size = os.fstat(f.fileno()).st_size
        if file_size > _MAX_OPLOG_BYTES:
            raise self._size_limit("file", file_size, _MAX_OPLOG_BYTES)
        f.seek(0)
        loaded: list[dict[str, Any]] = []
        complete_end = 0
        fragment_start: int | None = None
        line_index = 0
        scanned_bytes = 0
        while True:
            record_start = f.tell()
            # 单行有界读取，恶意单 record 不会先分配整个文件或整个超长行。
            record = f.readline(_MAX_OPLOG_RECORD_BYTES + 1)
            if not record:
                break
            scanned_bytes += len(record)
            if scanned_bytes > _MAX_OPLOG_BYTES:
                raise self._size_limit("file", scanned_bytes, _MAX_OPLOG_BYTES)
            if len(record) > _MAX_OPLOG_RECORD_BYTES:
                raise self._size_limit("record", len(record), _MAX_OPLOG_RECORD_BYTES)
            if not record.endswith(b"\n"):
                fragment_start = record_start
                break
            try:
                entry = _strict_json_record(record[:-1])
            except _StrictJsonError:
                raise self._corrupt(
                    "oplog complete line is not valid strict UTF-8 JSON",
                    line_index,
                ) from None
            loaded.append(entry)
            complete_end = f.tell()
            line_index += 1
        if not self._entries_form_chain(loaded):
            raise self._corrupt("oplog hash chain broken")

        repaired = fragment_start is not None
        if repaired:
            complete_end = fragment_start
            os.ftruncate(f.fileno(), complete_end)
            f.flush()
            os.fsync(f.fileno())
            # 修复本身已经持久化；即使随后新 record 写入失败，也不能丢掉此事实。
            self._truncated_tail_dropped = True
        return loaded, repaired, complete_end if repaired else file_size

    def _encode_entry(
        self,
        prev_hash: str,
        body: dict[str, Any],
    ) -> tuple[dict[str, Any], bytes]:
        """由唯一 codec 生成历史格式不变的有限 JSON record。"""
        try:
            return encode_operation_chain_entry(
                prev_hash,
                body,
                maximum_bytes=_MAX_OPLOG_RECORD_BYTES,
                json_dumps=json.dumps,
            )
        except OperationRecordCodecError:
            raise self._entry_invalid("not_finite_strict_json") from None

    @staticmethod
    def _stat_signature(stat_result: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_size,
            stat_result.st_mtime_ns,
            stat_result.st_ctime_ns,
        )

    @classmethod
    def _entries_form_chain(cls, entries: list[Any]) -> bool:
        return operation_entries_form_chain(entries, json_dumps=json.dumps)

    def _corrupt(self, message: str, line_index: int | None = None) -> ZeError:
        del line_index
        return ZeError("oplog_corrupt", message)

    def verify_chain(self) -> bool:
        """全链校验(**校验的是本实例内存视图 self._entries**): 任何 prev_hash 不接或 self_hash 算错都返 False.

        多实例注意:_persist_line 的 flock 保证文件链连续；本实例在下一次 append 发现文件 stat 变化时
        会先重载完整链再续写。但纯查询不会主动刷新其他实例刚写入的记录，因此 verify_chain() 只证明
        当前内存视图自洽，fresh OpLog(path) 才是文件完整性的权威判据。"""
        with self._lock:
            return self._entries_form_chain(self._entries)

    def entries(self) -> list[dict[str, Any]]:
        """全部 entries (deep copy 防外部 mutate)."""
        return copy.deepcopy(self._entries)

    def _public_history_snapshot(
        self,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        source: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Filter/limit first, then copy a redacted transport snapshot.

        This private SDK seam prevents a small history query from first
        deepcopying every 4 MiB Start descriptor in the canonical OpLog.
        Canonical storage and internal audit continue to use ``entries()``.
        """
        with self._lock:
            if entity_type and entity_id:
                selected = [
                    entry
                    for entry in self._entries
                    if any(
                        patch["entity_type"] == entity_type
                        and patch["entity_id"] == entity_id
                        for patch in entry["op"]["patches"]
                    )
                ]
            elif source:
                selected = [
                    entry
                    for entry in self._entries
                    if entry["op"]["source"] == source
                ]
            else:
                selected = self._entries
            if limit is not None and limit > 0:
                selected = selected[-limit:]
            return _project_public_history_entries(selected)

    def _canonical_history_snapshot(
        self,
    ) -> tuple[tuple[dict[str, Any], ...], int, str]:
        """Return one fresh, atomic chain view for runtime history authority.

        Persistent logs refresh from the file while holding the instance lock;
        the file scan itself takes its existing cross-process flock.  The
        returned rows are detached and the count/tail pair describes exactly
        those rows.  This private protocol is consumed by the runtime assembler
        and commit coordinator, never by editor/mod code.
        """
        rows, count, tail_hash, _path_identity, _stat_signature = (
            self._canonical_history_rows_snapshot()
        )
        return rows, count, tail_hash

    def _canonical_history_head_probe(
        self,
    ) -> tuple[
        int,
        str,
        str,
        tuple[int, int, int, int, int] | None,
        tuple[int, int, int, int, int] | None,
    ]:
        """Return an O(1) cached-head/fresh-file-stat probe under one lock.

        ``cached_stat == observed_stat`` is the permission to trust cached
        count/tail without rescanning history.  A mismatch is *not* repaired by
        this fast port: the runtime authority must freeze because another writer
        changed canonical history outside its exact coordinator.  In-memory
        logs use ``None == None`` and retain their constant-time behavior.
        """
        with self._lock:
            path_identity = str(self._path.resolve()) if self._path is not None else ""
            observed_stat: tuple[int, int, int, int, int] | None = None
            if self._path is not None:
                try:
                    stream = self._open_secure(create=False)
                except FileNotFoundError:
                    stream = None
                if stream is not None:
                    with stream:
                        _flock(stream, _LOCK_SH)
                        try:
                            observed_stat = self._stat_signature(
                                os.fstat(stream.fileno())
                            )
                        finally:
                            _flock(stream, _LOCK_UN)
            return (
                len(self._entries),
                self._tail_hash,
                path_identity,
                self._last_persisted_stat,
                observed_stat,
            )

    def _canonical_history_rows_snapshot(
        self,
    ) -> tuple[
        tuple[dict[str, Any], ...],
        int,
        str,
        str,
        tuple[int, int, int, int, int] | None,
    ]:
        """Return one fully verified rows/head/stat view.

        This is intentionally the expensive history-consumer port.  Ordinary
        commits use :meth:`_canonical_history_head_probe`; undo/redo calls this
        once to obtain source rows, and durable confirmation requests only its
        newly appended suffix.
        """
        with self._lock:
            if self._path is not None:
                try:
                    self._load()
                except FileNotFoundError:
                    # A never-created persistent log is the canonical empty
                    # head.  Deletion of a previously observed file is not.
                    if self._last_persisted_stat is not None or self._entries:
                        raise
            return (
                tuple(copy.deepcopy(self._entries)),
                len(self._entries),
                self._tail_hash,
                str(self._path.resolve()) if self._path is not None else "",
                self._last_persisted_stat,
            )

    def _canonical_history_suffix_snapshot(
        self,
        start_count: int,
    ) -> tuple[
        tuple[dict[str, Any], ...],
        int,
        str,
        str,
        tuple[int, int, int, int, int] | None,
    ]:
        """Return the just-published suffix behind one O(1) file-stat fence.

        The append path has already encoded, fsynced, and published each new
        entry into this exact ``OpLog`` instance while holding ``self._lock``.
        Durable confirmation therefore needs only those private in-memory rows
        plus a fresh stat of the securely opened file.  It must not rescan the
        historical prefix on every commit.  A writer outside this instance is
        represented by a different returned stat and is rejected by the
        history authority's exact-head comparison.
        """
        if type(start_count) is not int or start_count < 0:
            raise TypeError("start_count must be a non-negative int")
        with self._lock:
            observed_stat: tuple[int, int, int, int, int] | None = None
            if self._path is not None:
                with self._open_secure(create=False) as stream:
                    _flock(stream, _LOCK_SH)
                    try:
                        observed_stat = self._stat_signature(
                            os.fstat(stream.fileno())
                        )
                    finally:
                        _flock(stream, _LOCK_UN)
            if start_count > len(self._entries):
                raise ZeError(
                    "history_authority_mismatch",
                    "history suffix starts after the canonical head",
                    context={"reason": "history_suffix_start_invalid"},
                )
            return (
                tuple(copy.deepcopy(self._entries[start_count:])),
                len(self._entries),
                self._tail_hash,
                str(self._path.resolve()) if self._path is not None else "",
                observed_stat,
            )

    def has_native_after(self, op_id: str) -> bool:
        """id==op_id 那条之后是否还有 kind=='native' 的正向 op(undo 判 redo 是否被新编辑分叉用)。

        直扫内部 _entries、**不 deepcopy** —— can_redo() 会在 GET /api/history 每次调,避免 O(n) 拷贝开销。"""
        seen = False
        for e in self._entries:
            if seen and e["op"].get("kind") == "native":
                return True
            if e["op"].get("id") == op_id:
                seen = True
        return False

    def by_entity(self, entity_type: str, entity_id: str) -> list[dict[str, Any]]:
        """过滤影响某 entity 的 entries (任意 patch 命中即包含)."""
        out: list[dict[str, Any]] = []
        for e in self._entries:
            for p in e["op"]["patches"]:
                if p["entity_type"] == entity_type and p["entity_id"] == entity_id:
                    out.append(copy.deepcopy(e))
                    break
        return out

    def by_source(self, source: str) -> list[dict[str, Any]]:
        return [copy.deepcopy(e) for e in self._entries if e["op"]["source"] == source]

    def by_document(self, document_id: str) -> list[dict[str, Any]]:
        """过滤某个 Document 的 entries.

        旧日志没有 document_id,自然不会命中;这保持向后兼容。
        """
        return [
            copy.deepcopy(e)
            for e in self._entries
            if e["op"].get("document_id") == document_id
        ]

    def by_session(self, session_id: str) -> list[dict[str, Any]]:
        """过滤某个浏览器/AI session 的 entries."""
        return [
            copy.deepcopy(e)
            for e in self._entries
            if e["op"].get("session_id") == session_id
        ]

    def by_group(self, group_id: str) -> list[dict[str, Any]]:
        """过滤某个 operation group 的 entries."""
        return [
            copy.deepcopy(e)
            for e in self._entries
            if e["op"].get("group_id") == group_id
        ]

    def __len__(self) -> int:
        return len(self._entries)

    @staticmethod
    def _hash(prev_hash: str, body: dict[str, Any]) -> str:
        """sha256(prev_hash + json(body, sort_keys=True)).

        sort_keys=True 保证字段顺序不影响 hash.
        """
        # 只保留兼容入口；算法与 transaction record 共用一个 pure codec。
        return operation_body_hash(prev_hash, body, json_dumps=json.dumps)
