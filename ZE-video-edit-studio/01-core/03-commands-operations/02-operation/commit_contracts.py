"""Commit Flow 的内部线协议。

这里只定义提交结果怎样表达，不接入 HTTP/MCP，也不执行任何真实存储。
后续 coordinator、storage port 与 event port 必须共同使用这组类型，避免各入口
各自解释「保存成功」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Mapping, TypeVar


class SavePolicy(str, Enum):
    """何时由框架自动形成持久提交；不包含「手动保存」模式。"""

    AUTO_COMMIT = "auto_commit"
    CHECKPOINT_COMMIT = "checkpoint_commit"
    LEGACY = "legacy"


class CommitStatus(str, Enum):
    """一次提交在业务数据层的最终可证明状态。"""

    COMMITTED = "committed"
    DIRTY = "dirty"
    ABORTED = "aborted"
    INDETERMINATE = "indeterminate"


class EventStatus(str, Enum):
    """提交事件的独立状态；不能反向覆盖业务提交状态。"""

    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    PUBLISHED = "published"
    FAILED = "failed"


class CommitStage(str, Enum):
    """提交协调器唯一允许的处理顺序。"""

    VALIDATE = "validate"
    APPLY = "apply"
    DOCUMENT = "document"
    OPLOG = "oplog"
    EVENT = "event"


class DurabilityState(str, Enum):
    """一次存储写入的可证明结果；UNKNOWN 绝不能按失败后重试处理。"""

    NOT_DURABLE = "not_durable"
    DURABLE = "durable"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DurabilityFacts:
    """失败发生时协调器已经确认的事实，而不是依据异常文字作猜测。"""

    mutation_applied: bool = False
    rollback_succeeded: bool = False
    document: DurabilityState = DurabilityState.NOT_DURABLE
    oplog: DurabilityState = DurabilityState.NOT_DURABLE

    def __post_init__(self) -> None:
        if not isinstance(self.document, DurabilityState):
            raise TypeError("document must be DurabilityState")
        if not isinstance(self.oplog, DurabilityState):
            raise TypeError("oplog must be DurabilityState")
        if self.rollback_succeeded and not self.mutation_applied:
            raise ValueError("rollback_succeeded requires mutation_applied")
        if self.document is not DurabilityState.NOT_DURABLE and not self.mutation_applied:
            raise ValueError("document durability requires mutation_applied")
        if self.document is not DurabilityState.NOT_DURABLE and self.rollback_succeeded:
            raise ValueError("a durable or unknown document cannot also be rolled back in memory")
        if self.oplog is not DurabilityState.NOT_DURABLE and self.document is DurabilityState.NOT_DURABLE:
            raise ValueError("oplog durability requires a durable or unknown document")

    @property
    def document_durable(self) -> bool:
        return self.document is DurabilityState.DURABLE

    @property
    def oplog_durable(self) -> bool:
        return self.oplog is DurabilityState.DURABLE

    @property
    def durability_unknown(self) -> bool:
        return DurabilityState.UNKNOWN in (self.document, self.oplog)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mutation_applied": self.mutation_applied,
            "rollback_succeeded": self.rollback_succeeded,
            "document": self.document.value,
            "oplog": self.oplog.value,
        }


@dataclass(frozen=True)
class FailureDisposition:
    """由失败阶段和耐久事实推导出的唯一结果。"""

    status: CommitStatus
    event_status: EventStatus
    retry_safe: bool


def classify_commit_failure(
    stage: CommitStage,
    facts: DurabilityFacts,
    *,
    event_status: EventStatus | None = None,
) -> FailureDisposition:
    """把失败映射为提交结果。

    ``retry_safe`` 只回答「能否自动重放整笔业务命令」。事件投递可以单独重试，
    但永远不因此把已经 committed 的业务命令再执行一次。
    """

    if not isinstance(stage, CommitStage):
        raise TypeError("stage must be CommitStage")
    if not isinstance(facts, DurabilityFacts):
        raise TypeError("facts must be DurabilityFacts")

    # 阶段约束让调用方不能用一组不可能出现的事实“证明”成功。
    if stage is CommitStage.VALIDATE:
        if facts != DurabilityFacts():
            raise ValueError("validate failure cannot have mutation or durability facts")
    elif stage is CommitStage.APPLY:
        if facts.document is not DurabilityState.NOT_DURABLE or facts.oplog is not DurabilityState.NOT_DURABLE:
            raise ValueError("apply failure cannot already be durable")
    elif stage is CommitStage.DOCUMENT:
        if not facts.mutation_applied or facts.oplog is not DurabilityState.NOT_DURABLE:
            raise ValueError("document stage requires an applied mutation and no durable oplog")
    elif stage is CommitStage.OPLOG:
        if facts.document is DurabilityState.NOT_DURABLE:
            raise ValueError("oplog stage requires a durable or unknown document")
    elif stage is CommitStage.EVENT:
        if not facts.document_durable or not facts.oplog_durable:
            raise ValueError("event stage requires durable document and oplog")

    if stage is CommitStage.EVENT:
        if event_status not in (EventStatus.PENDING, EventStatus.FAILED):
            raise ValueError("event failure must be classified as pending or failed")
        return FailureDisposition(
            status=CommitStatus.COMMITTED,
            event_status=event_status,
            retry_safe=False,
        )

    if event_status not in (None, EventStatus.NOT_REQUIRED):
        raise ValueError("event status is only valid at the event stage")

    if facts.document_durable and facts.oplog_durable:
        # 例如 oplog 已落盘、仅确认响应丢失：业务提交已经成立。
        return FailureDisposition(
            status=CommitStatus.COMMITTED,
            event_status=EventStatus.NOT_REQUIRED,
            retry_safe=False,
        )
    if facts.document_durable or facts.durability_unknown:
        # 最危险边界：业务文档已经落盘，审计日志尚未落盘。
        return FailureDisposition(
            status=CommitStatus.INDETERMINATE,
            event_status=EventStatus.NOT_REQUIRED,
            retry_safe=False,
        )
    if facts.mutation_applied and not facts.rollback_succeeded:
        # 仅内存被改且未回滚；不能让同一进程再叠加执行一次。
        return FailureDisposition(
            status=CommitStatus.DIRTY,
            event_status=EventStatus.NOT_REQUIRED,
            retry_safe=False,
        )
    return FailureDisposition(
        status=CommitStatus.ABORTED,
        event_status=EventStatus.NOT_REQUIRED,
        retry_safe=True,
    )


@dataclass(frozen=True)
class CommitFailure:
    """一次失败的可审计描述；状态字段全部由事实推导，不能由 caller 自报。"""

    stage: CommitStage
    code: str
    message: str
    facts: DurabilityFacts = field(default_factory=DurabilityFacts)
    event_status: EventStatus | None = None
    status: CommitStatus = field(init=False)
    retry_safe: bool = field(init=False)

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("failure code must not be empty")
        if not self.message:
            raise ValueError("failure message must not be empty")
        disposition = classify_commit_failure(
            self.stage,
            self.facts,
            event_status=self.event_status,
        )
        object.__setattr__(self, "status", disposition.status)
        object.__setattr__(self, "event_status", disposition.event_status)
        object.__setattr__(self, "retry_safe", disposition.retry_safe)

    def to_dict(self) -> dict[str, Any]:
        assert isinstance(self.event_status, EventStatus)
        return {
            "stage": self.stage.value,
            "code": self.code,
            "message": self.message,
            "facts": self.facts.to_dict(),
            "status": self.status.value,
            "event_status": self.event_status.value,
            "retry_safe": self.retry_safe,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CommitFailure":
        facts_raw = raw.get("facts")
        if not isinstance(facts_raw, Mapping):
            raise ValueError("failure facts must be an object")
        failure = cls(
            stage=_wire_enum(CommitStage, raw.get("stage"), "failure.stage"),
            code=_wire_text(raw.get("code"), "failure.code"),
            message=_wire_text(raw.get("message"), "failure.message"),
            facts=DurabilityFacts(
                mutation_applied=_wire_bool(facts_raw.get("mutation_applied"), "mutation_applied"),
                rollback_succeeded=_wire_bool(facts_raw.get("rollback_succeeded"), "rollback_succeeded"),
                document=_wire_enum(DurabilityState, facts_raw.get("document"), "document"),
                oplog=_wire_enum(DurabilityState, facts_raw.get("oplog"), "oplog"),
            ),
            event_status=_wire_enum(EventStatus, raw.get("event_status"), "failure.event_status"),
        )
        if dict(raw) != failure.to_dict():
            raise ValueError("failure payload is incomplete, inconsistent, or has unknown fields")
        return failure


@dataclass(frozen=True)
class CommitReceipt:
    """一次提交的稳定 wire receipt（固定全键集、枚举只输出 value）。"""

    SCHEMA_VERSION: ClassVar[str] = "ze.commit-receipt/1"

    commit_id: str
    document_id: str
    operation_ids: tuple[str, ...]
    trace_id: str
    save_policy: SavePolicy
    status: CommitStatus
    event_status: EventStatus
    stage: CommitStage
    retry_safe: bool
    document_version: int | None
    document_durable: bool
    oplog_durable: bool
    failure: CommitFailure | None = None

    def __post_init__(self) -> None:
        for name in ("commit_id", "document_id", "trace_id"):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if not isinstance(self.save_policy, SavePolicy):
            raise TypeError("save_policy must be SavePolicy")
        if not isinstance(self.operation_ids, tuple) or any(
            not isinstance(op_id, str) or not op_id for op_id in self.operation_ids
        ):
            raise ValueError("operation_ids must be a tuple of non-empty strings")
        if len(set(self.operation_ids)) != len(self.operation_ids):
            raise ValueError("operation_ids must not contain duplicates")
        if self.document_version is not None and (
            not isinstance(self.document_version, int) or isinstance(self.document_version, bool)
            or self.document_version < 1
        ):
            raise ValueError("document_version must be a positive integer or null")
        if not isinstance(self.document_durable, bool) or not isinstance(self.oplog_durable, bool):
            raise TypeError("durability flags must be boolean")
        if self.oplog_durable and not self.document_durable:
            raise ValueError("oplog_durable requires document_durable")
        if self.document_durable and self.document_version is None:
            raise ValueError("durable document requires document_version")
        if self.failure is not None:
            if (
                self.stage is not self.failure.stage
                or self.status is not self.failure.status
                or self.event_status is not self.failure.event_status
                or self.retry_safe is not self.failure.retry_safe
            ):
                raise ValueError("receipt fields must match the derived failure disposition")
            if self.document_durable is not self.failure.facts.document_durable:
                raise ValueError("receipt document durability must match failure facts")
            if self.oplog_durable is not self.failure.facts.oplog_durable:
                raise ValueError("receipt oplog durability must match failure facts")
        else:
            if self.status is not CommitStatus.COMMITTED:
                raise ValueError("a non-committed receipt requires failure details")
            if self.retry_safe:
                raise ValueError("a committed business command is never safe to replay wholesale")
            if self.event_status is EventStatus.FAILED:
                raise ValueError("failed event status requires failure details")
            if not self.document_durable or not self.oplog_durable:
                raise ValueError("committed receipt requires durable document and oplog")
        if self.status is CommitStatus.COMMITTED and not self.operation_ids:
            raise ValueError("committed receipt requires at least one operation id")

        if self.event_status in (EventStatus.PENDING, EventStatus.PUBLISHED, EventStatus.FAILED):
            if self.status is not CommitStatus.COMMITTED:
                raise ValueError("event progress is only valid for a committed command")
        if self.event_status is EventStatus.PUBLISHED and self.stage is not CommitStage.EVENT:
            raise ValueError("published event requires event stage")

    @classmethod
    def committed(
        cls,
        *,
        commit_id: str,
        document_id: str,
        operation_ids: tuple[str, ...],
        trace_id: str,
        save_policy: SavePolicy,
        document_version: int,
        event_status: EventStatus = EventStatus.NOT_REQUIRED,
    ) -> "CommitReceipt":
        if event_status is EventStatus.FAILED:
            raise ValueError("use from_failure for failed event delivery")
        stage = CommitStage.EVENT if event_status in (EventStatus.PENDING, EventStatus.PUBLISHED) else CommitStage.OPLOG
        return cls(
            commit_id=commit_id,
            document_id=document_id,
            operation_ids=operation_ids,
            trace_id=trace_id,
            save_policy=save_policy,
            status=CommitStatus.COMMITTED,
            event_status=event_status,
            stage=stage,
            retry_safe=False,
            document_version=document_version,
            document_durable=True,
            oplog_durable=True,
        )

    @classmethod
    def from_failure(
        cls,
        *,
        commit_id: str,
        document_id: str,
        operation_ids: tuple[str, ...],
        trace_id: str,
        save_policy: SavePolicy,
        failure: CommitFailure,
        document_version: int | None = None,
    ) -> "CommitReceipt":
        assert isinstance(failure.event_status, EventStatus)
        return cls(
            commit_id=commit_id,
            document_id=document_id,
            operation_ids=operation_ids,
            trace_id=trace_id,
            save_policy=save_policy,
            status=failure.status,
            event_status=failure.event_status,
            stage=failure.stage,
            retry_safe=failure.retry_safe,
            document_version=document_version,
            document_durable=failure.facts.document_durable,
            oplog_durable=failure.facts.oplog_durable,
            failure=failure,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "commit_id": self.commit_id,
            "document_id": self.document_id,
            "operation_ids": list(self.operation_ids),
            "trace_id": self.trace_id,
            "save_policy": self.save_policy.value,
            "status": self.status.value,
            "event_status": self.event_status.value,
            "stage": self.stage.value,
            "retry_safe": self.retry_safe,
            "document_version": self.document_version,
            "document_durable": self.document_durable,
            "oplog_durable": self.oplog_durable,
            "failure": self.failure.to_dict() if self.failure else None,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CommitReceipt":
        if raw.get("schema_version") != cls.SCHEMA_VERSION:
            raise ValueError("unsupported commit receipt schema_version")
        failure_raw = raw.get("failure")
        failure = None
        if failure_raw is not None:
            if not isinstance(failure_raw, Mapping):
                raise ValueError("failure must be an object or null")
            failure = CommitFailure.from_dict(failure_raw)
        receipt = cls(
            commit_id=_wire_text(raw.get("commit_id"), "commit_id"),
            document_id=_wire_text(raw.get("document_id"), "document_id"),
            operation_ids=_wire_text_tuple(raw.get("operation_ids"), "operation_ids"),
            trace_id=_wire_text(raw.get("trace_id"), "trace_id"),
            save_policy=_wire_enum(SavePolicy, raw.get("save_policy"), "save_policy"),
            status=_wire_enum(CommitStatus, raw.get("status"), "status"),
            event_status=_wire_enum(EventStatus, raw.get("event_status"), "event_status"),
            stage=_wire_enum(CommitStage, raw.get("stage"), "stage"),
            retry_safe=_wire_bool(raw.get("retry_safe"), "retry_safe"),
            document_version=_wire_optional_positive_int(raw.get("document_version"), "document_version"),
            document_durable=_wire_bool(raw.get("document_durable"), "document_durable"),
            oplog_durable=_wire_bool(raw.get("oplog_durable"), "oplog_durable"),
            failure=failure,
        )
        if dict(raw) != receipt.to_dict():
            raise ValueError("receipt payload is incomplete, inconsistent, or has unknown fields")
        return receipt

    @classmethod
    def from_json(cls, payload: str) -> "CommitReceipt":
        raw = json.loads(payload)
        if not isinstance(raw, Mapping):
            raise ValueError("commit receipt JSON must contain an object")
        return cls.from_dict(raw)


_E = TypeVar("_E", bound=Enum)


def _wire_enum(enum_type: type[_E], value: Any, field_name: str) -> _E:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field_name}") from exc


def _wire_text(value: Any, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"{field_name} must be a string")
    return value


def _wire_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _wire_text_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{field_name} must be an array of non-empty strings")
    return tuple(value)


def _wire_optional_positive_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer or null")
    return value
