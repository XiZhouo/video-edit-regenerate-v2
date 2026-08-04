"""InvocationLog(规划 §3.4)—— AI 调用回执的独立幂等日志。

**独立于 Operation 撤销栈**(editor-arch:执行元数据非数据变更,不进 OpLog)。落盘到 project/
skill 数据目录的 `invocations.json`(与 `oplog.ndjson` 物理分离)。幂等:同 `idempotency_key`
重复 record 只留一条(重连/重放不重复记账)。供「当前工具最近调用的实际 provider/model」展示
(§2.2 line 153)+ Commerce 脱敏计费事件(§3.4 line 429/514)。

设计:纯本地文件 + RLock + 原子写(临时文件 replace,同 IdempotencyStore 手法);零网络、不
import server/store。receipt 由 caller 构造(`sdk.hub.InvocationReceipt`),本模块只负责幂等
持久化 + 查询。receipts 存**有序 list**(非 dict)—— 保证 `sort_keys=True` 的确定性输出不打乱
记录顺序(recent/list 依赖插入序)。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional

from . import errors
from .types import InvocationReceipt

_SCHEMA_VERSION = 1


class InvocationLog:
    """AI 调用回执的独立幂等日志(不进 Operation 栈)。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def record(self, receipt: InvocationReceipt) -> InvocationReceipt:
        """幂等追加。同 `idempotency_key` 已存在 → 返回**既有**回执(不重复记账、不覆盖)。"""
        key = (receipt.idempotency_key or "").strip()
        if not key:
            raise ValueError("InvocationReceipt.idempotency_key 不能为空(幂等去重键)")
        with self._lock:
            state = self._load_unlocked()
            receipts: list[dict[str, Any]] = state["receipts"]
            for existing in receipts:
                if existing.get("idempotency_key") == key:
                    return _from_dict(existing)          # 幂等命中:返回既有
            receipts.append(receipt.public_dict())
            self._save_unlocked(state)
            return receipt

    def list(self, *, limit: Optional[int] = None, user_id: Optional[str] = None) -> list[InvocationReceipt]:
        """按记录顺序返回回执;`user_id` 过滤归属;`limit` 取最新 limit 条。"""
        with self._lock:
            state = self._load_unlocked()
        items = state["receipts"]
        if user_id is not None:
            items = [r for r in items if r.get("actor_user_id") == user_id]
        if limit is not None and limit >= 0:
            items = items[-limit:]
        return [_from_dict(r) for r in items]

    def recent(self, n: int = 10, *, user_id: Optional[str] = None) -> list[InvocationReceipt]:
        """最近 n 条(最新在末尾)。供「当前工具最近调用的实际 provider/model」展示。"""
        items = self.list(user_id=user_id)
        return items[-n:] if n >= 0 else items

    # ── 持久化(原子写;读损坏不静默吞成空)──────────────────────────────────
    def _load_unlocked(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema_version": _SCHEMA_VERSION, "receipts": []}
        if not isinstance(raw, dict):
            raise ValueError(f"InvocationLog 文件不是 JSON 对象: {self._path}")
        raw.setdefault("schema_version", _SCHEMA_VERSION)
        receipts = raw.setdefault("receipts", [])
        if not isinstance(receipts, list):
            raise ValueError(f"InvocationLog receipts 必须是 list: {self._path}")
        return raw

    def _save_unlocked(self, state: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f"{self._path.name}.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)


def receipt_from_sink(
    sink: Optional[dict[str, Any]], *,
    idempotency_key: str,
    feature_id: str,
    capability: str,
    actor_user_id: Optional[str],
    status: str,
    created_at: float,
    receipt_id: str = "",
    turn_id: Optional[str] = None,
    connection_id: Optional[str] = None,
    latency_ms: Optional[int] = None,
    error_code: Optional[str] = None,
    cost_ref: Optional[dict[str, Any]] = None,
    params_summary: Optional[dict[str, Any]] = None,
    result_summary: Optional[dict[str, Any]] = None,
    provenance: Optional[str] = None,
) -> InvocationReceipt:
    """从 image_call/llm_call 的 receipt_sink(实际落位:route/provider/model_id/trace)+ caller 显式字段
    构造一条 InvocationReceipt(§3.4 写侧桥:让派发处「记录接线」收敛成一次调用,不各自手搓)。

    纯函数、无 I/O、不取时间(created_at 由 caller 传,便于测试与幂等)。receipt_id 缺省 = idempotency_key。
    status='error' 时把 error_code 经 9 类权威码归一(errors.normalize_code);sink=None 时 route/provider/
    model 落空串(仍可记一条「调用发生过」的最小回执)。**脱敏红线**:本函数不塞 secret/完整 prompt/bytes——
    caller 只应传摘要类字段(params_summary/result_summary),与 InvocationReceipt 契约一致。"""
    s = sink or {}
    normalized_err = errors.normalize_code(error_code) if status == "error" else error_code
    return InvocationReceipt(
        receipt_id=receipt_id or idempotency_key,
        idempotency_key=idempotency_key,
        feature_id=feature_id,
        capability=capability,
        provider=str(s.get("provider") or ""),
        model_id=str(s.get("model_id") or ""),
        route=str(s.get("route") or ""),
        status=status,
        created_at=created_at,
        connection_id=connection_id,
        turn_id=turn_id,
        latency_ms=latency_ms,
        params_summary=params_summary,
        result_summary=result_summary,
        error_code=normalized_err,
        cost_ref=cost_ref,
        provenance=provenance,
        actor_user_id=actor_user_id,
        trace=tuple(s.get("trace") or ()),
    )


def record_from_sink(log: "InvocationLog | None", sink: Optional[dict[str, Any]],
                     **fields: Any) -> "InvocationReceipt | None":
    """**best-effort** 记录桥:构造 receipt(receipt_from_sink)→ log.record(幂等)。绝不抛——
    记账失败/日志缺失/字段不全都只返 None,不影响主生成流程(与 turn/change_set 记录同纪律)。
    fields 转发给 receipt_from_sink(至少需 idempotency_key/feature_id/capability/status/created_at)。"""
    if log is None:
        return None
    try:
        return log.record(receipt_from_sink(sink, **fields))
    except Exception:   # noqa: BLE001 — 记账 best-effort,不阻断出图/回答
        return None


def _from_dict(d: dict[str, Any]) -> InvocationReceipt:
    """public_dict → InvocationReceipt(public_dict 丢弃的 None 字段回落默认)。"""
    return InvocationReceipt(
        receipt_id=d["receipt_id"],
        idempotency_key=d["idempotency_key"],
        feature_id=d["feature_id"],
        capability=d["capability"],
        provider=d["provider"],
        model_id=d["model_id"],
        route=d["route"],
        status=d["status"],
        created_at=d["created_at"],
        connection_id=d.get("connection_id"),
        turn_id=d.get("turn_id"),
        latency_ms=d.get("latency_ms"),
        params_summary=d.get("params_summary"),
        result_summary=d.get("result_summary"),
        error_code=d.get("error_code"),
        cost_ref=d.get("cost_ref"),
        provenance=d.get("provenance"),
        actor_user_id=d.get("actor_user_id"),
        trace=tuple(d.get("trace") or ()),
    )
