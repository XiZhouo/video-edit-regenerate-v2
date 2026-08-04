"""work_list — 管线工作清单(占位 / 文件统计进度 / 精确重试的共同脊柱).

一个 WorkItem 与「将产出的 entity + 期望产物文件路径」1:1。这让三件事统一:
  - 占位在开始就出现 :  每个 item → 一个占位 entity(supervisor 起跑时建)
  - 进度按 receipt 统计: done 数 = commit 身份 + artifacts checksum 均有效的 item 数
  - 精确重试         :  只重跑 error 标记的 item(retry_failed)

artifacts 路径是相对 data_dir 的白名单;stage 写 UUID attempt 路径，Supervisor 在
Document commit 后 promote，并由结构化 receipt 判 done(不靠裸文件或内存百分比)。

待补(跨叶子):
  - 并发 fan_out                 [caller / supervisor 线程池]
  - DAG(非线性 item 依赖)       [按需扩展]
"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any


_RECOVERY_IDENTITY_FIELDS = "recovery_identity_fields"
_SEED_DATA = "seed_data"
_MAX_RECOVERY_IDENTITY_FIELDS = 64


def _manifest_mapping(value: Any, *, label: str) -> dict[str, Any]:
    """Own one untrusted JSON object without invoking comparison hooks on bad keys."""
    if type(value) is not dict:
        raise TypeError(f"{label} 必须是精确 dict")
    snapshot: dict[str, Any] = {}
    try:
        for key, member in value.items():
            if type(key) is not str:
                raise TypeError(f"{label} 的 key 必须是精确 str")
            snapshot[key] = member
    except RuntimeError as exc:
        raise ValueError(f"{label} 在读取时发生变化") from exc
    return snapshot


def _immutable_container(*_args: Any, **_kwargs: Any) -> None:
    raise TypeError("WorkItem payload is an immutable owned snapshot")


class _FrozenDict(dict):
    """JSON-compatible read view whose nested containers cannot be edited in place."""

    __setitem__ = _immutable_container
    __delitem__ = _immutable_container
    clear = _immutable_container
    pop = _immutable_container
    popitem = _immutable_container
    setdefault = _immutable_container
    update = _immutable_container
    __ior__ = _immutable_container

    def __deepcopy__(self, _memo: dict[int, Any]) -> "_FrozenDict":
        return self


class _FrozenList(list):
    """List-shaped JSON value that preserves equality/encoding compatibility."""

    __setitem__ = _immutable_container
    __delitem__ = _immutable_container
    append = _immutable_container
    clear = _immutable_container
    extend = _immutable_container
    insert = _immutable_container
    pop = _immutable_container
    remove = _immutable_container
    reverse = _immutable_container
    sort = _immutable_container
    __iadd__ = _immutable_container
    __imul__ = _immutable_container

    def __deepcopy__(self, _memo: dict[int, Any]) -> "_FrozenList":
        return self


def _freeze_owned(value: Any, active: set[int] | None = None) -> Any:
    """Recursively freeze JSON containers after the caller graph has been copied."""
    active = set() if active is None else active
    if type(value) is dict:
        identity = id(value)
        if identity in active:
            raise ValueError("WorkItem.payload must not contain container cycles")
        active.add(identity)
        try:
            entries: list[tuple[str, Any]] = []
            for key, member in value.items():
                if type(key) is not str:
                    raise ValueError("WorkItem.payload object keys must be exact strings")
                entries.append((key, _freeze_owned(member, active)))
            return _FrozenDict(entries)
        finally:
            active.remove(identity)
    if type(value) is list:
        identity = id(value)
        if identity in active:
            raise ValueError("WorkItem.payload must not contain container cycles")
        active.add(identity)
        try:
            return _FrozenList(_freeze_owned(member, active) for member in value)
        finally:
            active.remove(identity)
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError("WorkItem.payload must contain exact JSON values")


def _thaw_owned(value: Any) -> Any:
    """Return a detached mutable carrier for stages and manifest serialization."""
    if isinstance(value, dict):
        return {copy.deepcopy(key): _thaw_owned(member) for key, member in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_owned(member) for member in value]
    return copy.deepcopy(value)


_WORK_ITEM_IDENTITY_FIELD_INDEX = {
    "item_id": 0,
    "entity_type": 1,
    "entity_id": 2,
    "artifacts": 3,
}


def _validate_recovery_identity(payload: Any) -> None:
    """Validate the declarative durable-entity binding used by cold recovery.

    The manifest remains untrusted.  A resolver may require this exact declaration and the
    Supervisor then compares the named seed members with the already committed Document entity.
    Validation lives on WorkItem so malformed declarations fail both at initial start and while
    decoding a recovered manifest.
    """
    if type(payload) is not dict or _RECOVERY_IDENTITY_FIELDS not in payload:
        return
    fields = payload[_RECOVERY_IDENTITY_FIELDS]
    seed = payload.get(_SEED_DATA)
    if (
        type(fields) is not list
        or not fields
        or len(fields) > _MAX_RECOVERY_IDENTITY_FIELDS
        or type(seed) is not dict
    ):
        raise ValueError(
            "WorkItem recovery_identity_fields requires a non-empty bounded list and seed_data"
        )
    if any(
        type(field) is not str
        or not field
        or len(field) > 256
        or "\x00" in field
        or field not in seed
        for field in fields
    ):
        raise ValueError(
            "WorkItem recovery_identity_fields must name bounded seed_data members"
        )
    if len(set(fields)) != len(fields):
        raise ValueError("WorkItem recovery_identity_fields must not contain duplicates")


@dataclass(frozen=True)
class WorkItem:
    """一个工作项 = 一个将产出 / 填充的 entity + 它的期望产物文件。

    item_id     : 稳定 id(默认 == entity_id;重试 / resume 据此定位)
    entity_type : 目标 entity 类型
    entity_id   : 目标 entity id(占位实体 + 最终填充都打它)
    artifacts   : 期望最终文件路径白名单(相对 data_dir);receipt+checksum 全有效 = done
    payload     : 交给 stage 的参数(+ 可选 name / seed_data 给占位实体)
                  可选 recovery_identity_fields 列出 seed_data 中恢复时不可换绑的顶层字段；
                  Supervisor 会把它们与已提交 Document entity 逐项比较。
    """
    item_id: str
    entity_type: str
    entity_id: str
    artifacts: tuple[str, ...] = ()
    payload: dict[str, Any] = field(default_factory=dict)
    _payload_snapshot: bytes = field(init=False, repr=False, compare=False)
    _identity_snapshot: tuple[Any, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        item_id = object.__getattribute__(self, "item_id")
        entity_type = object.__getattribute__(self, "entity_type")
        entity_id = object.__getattribute__(self, "entity_id")
        artifacts_input = object.__getattribute__(self, "artifacts")
        # 安全:item_id 会进 .pipelines/<run>/status/<item_id>.* 标记文件名,
        # 含路径分隔符 / null 会逃出 status/ 目录 → 拒绝(error_marker / stage 标记共用)。
        if (
            type(item_id) is not str
            or not item_id
            or item_id in (".", "..")
            or any(c in item_id for c in ("/", "\\", "\x00"))
        ):
            raise ValueError(f"WorkItem.item_id 不能含路径分隔符 / null: {item_id!r}")
        if type(entity_type) is not str or not entity_type:
            raise ValueError("WorkItem.entity_type 必须是非空精确 str")
        if type(entity_id) is not str or not entity_id:
            raise ValueError("WorkItem.entity_id 必须是非空精确 str")
        if type(artifacts_input) not in (list, tuple):
            raise TypeError("WorkItem.artifacts 必须是 list[str] 或 tuple[str, ...]")
        # 安全:artifacts 必须是 data_dir 内的相对路径,拒绝绝对路径 / .. 穿越
        # (supervisor 会把它们拼到 data_dir 下 mkdir/写文件)。
        normalized: list[str] = []
        for a in artifacts_input:
            if type(a) is not str or not a or a == "." or "\\" in a or "\x00" in a:
                raise ValueError(f"WorkItem.artifacts 必须是非空 POSIX 相对路径,拒绝 {a!r}")
            p = PurePosixPath(a)
            windows = PureWindowsPath(a)
            if p.is_absolute() or windows.is_absolute() or windows.drive or ".." in p.parts:
                raise ValueError(f"WorkItem.artifacts 必须是 data_dir 内相对路径,拒绝 {a!r}")
            normalized.append(p.as_posix())
        if len(set(normalized)) != len(normalized):
            raise ValueError("WorkItem.artifacts 目标路径不得重复")
        try:
            payload_input = object.__getattribute__(self, "payload")
            if type(payload_input) is _FrozenDict:
                # ``dataclasses.replace`` reads the public JSON-compatible view.  Accept only
                # this framework-owned wrapper and immediately detach it; arbitrary mapping
                # subclasses remain outside the exact JSON input domain.
                payload_input = _thaw_owned(payload_input)
            if type(payload_input) is not dict:
                raise ValueError("WorkItem.payload must be an object")
            detached_payload = _thaw_owned(_freeze_owned(payload_input))
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("WorkItem.payload must be a deeply ownable value") from exc
        if type(detached_payload) is not dict:
            raise ValueError("WorkItem.payload must be an object")
        _validate_recovery_identity(detached_payload)
        owned_payload = _freeze_owned(detached_payload)
        if type(owned_payload) is not _FrozenDict:
            raise ValueError("WorkItem.payload must be an object")
        # The public frozen view preserves the old mapping/list read API.  Execution and
        # serialization both recover from one canonical immutable byte snapshot, so bypassing a
        # dict/list subclass mutator cannot rewrite the approved payload behind their backs.
        try:
            snapshot = json.dumps(
                detached_payload,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8", "strict")
        except (
            TypeError,
            ValueError,
            OverflowError,
            UnicodeError,
            RecursionError,
        ) as exc:
            raise ValueError("WorkItem.payload must have a canonical UTF-8 JSON form") from exc
        object.__setattr__(self, "payload", owned_payload)
        object.__setattr__(self, "artifacts", tuple(normalized))
        object.__setattr__(self, "_payload_snapshot", snapshot)
        object.__setattr__(self, "_identity_snapshot", (
            item_id, entity_type, entity_id, tuple(normalized),
        ))

    def payload_copy(self) -> dict[str, Any]:
        """Return one detached payload from the construction-time authority snapshot."""
        snapshot = object.__getattribute__(self, "_payload_snapshot")
        if type(snapshot) is not bytes:
            raise ValueError("WorkItem payload snapshot is invalid")
        try:
            payload = json.loads(snapshot.decode("utf-8", "strict"))
        except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise ValueError("WorkItem payload snapshot is invalid") from exc
        if type(payload) is not dict:
            raise ValueError("WorkItem payload snapshot is invalid")
        _validate_recovery_identity(payload)
        return payload

    def __reduce__(self) -> tuple[Any, tuple[dict[str, Any]]]:
        """Keep process-transfer compatibility without unpickling frozen containers in place."""
        return (type(self).from_dict, (self.to_dict(),))

    def __getattribute__(self, name: str) -> Any:
        identity_index = _WORK_ITEM_IDENTITY_FIELD_INDEX.get(name)
        if identity_index is not None:
            state = object.__getattribute__(self, "__dict__")
            snapshot = state.get("_identity_snapshot")
            if type(snapshot) is tuple and len(snapshot) == 4:
                return snapshot[identity_index]
        if name == "payload":
            state = object.__getattribute__(self, "__dict__")
            if type(state.get("_payload_snapshot")) is bytes:
                # Rebuild a read view from the same canonical bytes used by execution and
                # serialization.  A deliberate write to the public dataclass field, or a
                # base-class list/dict mutator on an earlier view, cannot create a second truth.
                payload = object.__getattribute__(self, "payload_copy")()
                frozen = _freeze_owned(payload)
                if type(frozen) is not _FrozenDict:
                    raise ValueError("WorkItem payload snapshot is invalid")
                return frozen
        if name == "__dict__":
            return dict(object.__getattribute__(self, "__dict__"))
        return object.__getattribute__(self, name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "artifacts": list(self.artifacts),
            "payload": self.payload_copy(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WorkItem":
        manifest = _manifest_mapping(d, label="WorkItem manifest")
        allowed = frozenset(
            ("item_id", "entity_type", "entity_id", "artifacts", "payload")
        )
        required = frozenset(("item_id", "entity_type", "entity_id"))
        if not required.issubset(manifest) or not frozenset(manifest).issubset(allowed):
            raise ValueError("WorkItem manifest 字段不完整或包含未知字段")
        artifacts = manifest["artifacts"] if "artifacts" in manifest else []
        payload = manifest["payload"] if "payload" in manifest else {}
        if type(artifacts) is not list:
            raise TypeError("WorkItem manifest artifacts 必须是精确 list")
        if type(payload) is not dict:
            raise TypeError("WorkItem manifest payload 必须是精确 dict")
        return cls(
            item_id=manifest["item_id"],
            entity_type=manifest["entity_type"],
            entity_id=manifest["entity_id"],
            artifacts=artifacts,
            payload=payload,
        )


@dataclass(frozen=True)
class WorkList:
    """一次 pipeline run 的工作清单。"""
    run_id: str
    name: str = ""
    items: list[WorkItem] = field(default_factory=list)
    _run_id_snapshot: str = field(init=False, repr=False, compare=False)
    _name_snapshot: str = field(init=False, repr=False, compare=False)
    _items_snapshot: tuple[WorkItem, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            type(self.run_id) is not str
            or not self.run_id
            or self.run_id in (".", "..")
            or any(c in self.run_id for c in ("/", "\\", "\x00"))
        ):
            raise ValueError(f"WorkList.run_id 必须是安全的非空目录名: {self.run_id!r}")
        if type(self.name) is not str:
            raise TypeError("WorkList.name 必须是精确 str")
        if type(self.items) not in (list, tuple):
            raise TypeError("WorkList.items 必须是 list[WorkItem] 或 tuple[WorkItem, ...]")
        if any(type(item) is not WorkItem for item in self.items):
            raise TypeError("WorkList.items 只能包含精确 WorkItem")

        # Own both the public immutable read view and a separate serialization snapshot.  The
        # caller's list and WorkItem objects can therefore be discarded or tampered with without
        # changing what the Supervisor executes or what a manifest records.
        owned_items = tuple(WorkItem.from_dict(item.to_dict()) for item in self.items)
        snapshot_items = tuple(WorkItem.from_dict(item.to_dict()) for item in owned_items)

        item_ids = [item.item_id for item in owned_items]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("WorkList item_id 必须跨 item 唯一")
        targets = [(item.entity_type, item.entity_id) for item in owned_items]
        if len(set(targets)) != len(targets):
            raise ValueError("WorkList entity target 必须跨 item 唯一")
        artifacts = [
            PurePosixPath(path).as_posix()
            for item in owned_items
            for path in item.artifacts
        ]
        if len(set(artifacts)) != len(artifacts):
            raise ValueError("WorkList artifact target 必须跨 item 唯一")
        object.__setattr__(self, "items", owned_items)
        object.__setattr__(self, "_run_id_snapshot", self.run_id)
        object.__setattr__(self, "_name_snapshot", self.name)
        object.__setattr__(self, "_items_snapshot", snapshot_items)

    def target_keys(self) -> list[tuple[str, str]]:
        return [(it.entity_type, it.entity_id) for it in self._items_snapshot]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self._run_id_snapshot,
            "name": self._name_snapshot,
            "items": [it.to_dict() for it in self._items_snapshot],
        }

    def __reduce__(self) -> tuple[Any, tuple[dict[str, Any]]]:
        return (type(self).from_dict, (self.to_dict(),))

    def __getattribute__(self, name: str) -> Any:
        state = object.__getattribute__(self, "__dict__")
        snapshot_name = {
            "run_id": "_run_id_snapshot",
            "name": "_name_snapshot",
            "items": "_items_snapshot",
        }.get(name)
        if snapshot_name is not None and snapshot_name in state:
            return state[snapshot_name]
        if name == "__dict__":
            return dict(object.__getattribute__(self, "__dict__"))
        return object.__getattribute__(self, name)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WorkList":
        manifest = _manifest_mapping(d, label="WorkList manifest")
        if "run_id" not in manifest:
            raise ValueError("WorkList manifest 缺少 run_id")
        items = manifest["items"] if "items" in manifest else []
        if type(items) is not list:
            raise TypeError("WorkList manifest items 必须是精确 list")
        if any(type(item) is not dict for item in items):
            raise TypeError("WorkList manifest items 只能包含精确 dict")
        return cls(
            run_id=manifest["run_id"],
            name=manifest.get("name", ""),
            items=[WorkItem.from_dict(item) for item in items],
        )
