"""AI 助手 Turn 协议内核(规划 §7.1 P0-A「Turn 协议先行」)。

先定义**稳定对象 + 状态机**,后接派发/SSE/撤销(P0-B..E)。一「轮」= 用户一次请求触发的完整
助手交互:计划 → 调工具 → 改实体 → (必要时)等确认 → 完成。所有工具调用与实体改动都归到
**同一 turn_id**,让整轮可追溯、可撤销、可断线重连。

设计红线(§7.1):
- `turn_id` 贯通浏览器请求 → agent runner → MCP → dispatch → OpLog → SSE → InvocationReceipt →
  前端卡片(本内核是其起点与真源;InvocationReceipt.turn_id 已预留)。
- **重连可恢复**:get_turn(turn_id) 载回完整事件流与状态。
- **重复事件幂等**:同 event_id 二次 append 为 no-op(断线重放不重记)。
- **不同 turn 不串**:事件按 turn_id 分桶,seq 各自单调。
- 只是**协议 + 存储**,零网络、零派发接线(那些在 P0-B..E);纯数据 + 本地文件,可离线单测。

状态机(§7.1):
    created → planning → acting → awaiting_confirmation → acting → completed
                            └──────────────────────────────────→ cancelled / failed
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ── Turn 状态 ────────────────────────────────────────────────────────────────
CREATED = "created"
PLANNING = "planning"
ACTING = "acting"
AWAITING_CONFIRMATION = "awaiting_confirmation"
COMPLETED = "completed"
CANCELLED = "cancelled"
FAILED = "failed"

_TERMINAL = frozenset({COMPLETED, CANCELLED, FAILED})
# 合法转移;终态无出边。cancel/fail 可从任一非终态发生(§7.4:取消随时释放悬挂确认)。
_TRANSITIONS: dict[str, frozenset[str]] = {
    CREATED: frozenset({PLANNING, CANCELLED, FAILED}),
    PLANNING: frozenset({ACTING, AWAITING_CONFIRMATION, COMPLETED, CANCELLED, FAILED}),
    ACTING: frozenset({ACTING, AWAITING_CONFIRMATION, COMPLETED, CANCELLED, FAILED}),
    AWAITING_CONFIRMATION: frozenset({ACTING, CANCELLED, FAILED}),
    COMPLETED: frozenset(),
    CANCELLED: frozenset(),
    FAILED: frozenset(),
}


def can_transition(src: str, dst: str) -> bool:
    return dst in _TRANSITIONS.get(src, frozenset())


def is_terminal(state: str) -> bool:
    return state in _TERMINAL


# ── 事件种类(§7.1 稳定对象)─────────────────────────────────────────────────
EV_MESSAGE = "assistant_message"          # 用户/助手一条消息
EV_TOOL_CALL = "assistant_tool_call"      # 一次工具调用
EV_TOOL_RESULT = "assistant_tool_result"  # 工具调用结果
EV_CHANGE_SET = "assistant_change_set"    # 本轮聚合的可逆实体改动(整轮撤销单位)
EV_CONFIRMATION = "assistant_confirmation"  # 人审确认(nonce/描述/影响/状态)
EV_TURN_END = "assistant_turn_end"        # 轮结束(outcome)
# §7.3 W1-4:一轮内的阶段推进(理解/读取/调用工具/等待/完成)。过程卡此前只有「完成」一个
# 可渲染态 —— 前四段没有事件、没有文案、没有渲染点,于是用户从发出到拿到回话之间看不到
# 任何东西在动,只有一个转圈。它是**呈现**事件:不进 Operation 栈、不参与撤销。
EV_STAGE = "assistant_stage"              # {stage, tool?} 一轮内的阶段推进

_EVENT_KINDS = frozenset({
    EV_MESSAGE, EV_TOOL_CALL, EV_TOOL_RESULT, EV_CHANGE_SET, EV_CONFIRMATION, EV_TURN_END,
    EV_STAGE,
})

# 过程卡的五段(§7.3 原文「正在理解、读取、调用工具、等待、完成」)。
# 封闭集:前端按它取 i18n 文案,越界的阶段名一律拒 —— 否则会渲染出裸英文。
STAGE_UNDERSTANDING = "understanding"   # 收到这一轮,正在理解要做什么
STAGE_READING = "reading"               # 读工具语境(选中/oplog/说明书/能力面)
STAGE_TOOL = "tool"                     # 调用工具(带工具名)
STAGE_WAITING = "waiting"               # 等模型出结果
STAGE_DONE = "done"                     # 本轮收束
STAGES = (STAGE_UNDERSTANDING, STAGE_READING, STAGE_TOOL, STAGE_WAITING, STAGE_DONE)


def stage_frame(turn_id: str, stage: str, doc_id: str, tool: str = "") -> dict:
    """阶段帧(走既有 /api/events SSE 广播,不新开通道 —— 与 guide_step 同规矩)。

    为什么必须走 SSE:/api/ai/chat 是一次 POST,结果回来时这一轮已经结束了,
    响应体里带阶段等于事后才告诉用户「刚才在读取」。要让过程卡真的动,只能推。
    """
    if stage not in STAGES:
        raise ValueError(f"unknown assistant stage: {stage}")
    return {"type": "assistant_stage", "doc_id": doc_id, "turn_id": turn_id,
            "stage": stage, "tool": tool}


def changeset_from_oplog_rows(rows: list) -> Optional[dict[str, Any]]:
    """把一段 OpLog 条目窗口聚合成一条 change_set 载荷(§7.1 P0-D 整轮撤销的可逆单位)。

    只收 source=='ai' 且无 error 的 op(失败 op 非可逆改动;user/system op 不属本轮 AI 改动)。
    返回 {operation_ids, command_ids, count};窗口内无合格 AI op → None(caller 不记空 change_set)。
    纯函数(不碰存储/全局),供 server 的 turn change_set 记录与单测共用同一口径。

    rows 每条形如 OpLog.entries() 的 `{"op": {"id","source","command_id","error",...}}`。
    operation_ids 保持窗口内出现序(新旧序即 OpLog 追加序,整轮撤销按此逆序反做)。"""
    ops = [e["op"] for e in rows
           if isinstance(e, dict) and isinstance(e.get("op"), dict)
           and e["op"].get("source") == "ai" and not e["op"].get("error")]
    operation_ids = [o["id"] for o in ops if o.get("id")]
    if not operation_ids:
        return None
    command_ids = sorted({o.get("command_id", "") for o in ops if o.get("command_id")})
    return {"operation_ids": operation_ids, "command_ids": command_ids,
            "count": len(operation_ids)}


# Operation.external.kind 值,标记该 op 产生了不可撤销的外部效果(operation.py external_ai/external_api)。
_IRREVERSIBLE_EXTERNAL_KINDS = frozenset({"ai_generation", "outbound_api"})


def split_changeset_reversibility(operation_ids: list, oplog_rows: list,
                                  receipts: Optional[list] = None) -> dict[str, Any]:
    """把一轮 change_set 的 operation_ids 按可逆性拆两组(§7.4 整轮撤销:只撤可逆实体改动,
    显式列出不可逆外部效果)。纯函数,供 server 整轮撤销与单测共用同一口径。

    - reversible:纯本地实体改动的 op(无 external 信封)—— 可经反向 patch 整轮撤销;保持传入序
      (= change_set 窗口序 = OpLog 追加序),caller 按此逆序反做。
    - irreversible:带 external 信封(kind ∈ ai_generation/outbound_api)的 op —— 外部效果
      (已花额度的生成 / 已发出的调用)不可撤,列出展示、不参与 revert(避免「实体撤了但外部
      效果已发生」的不一致态,§7.4「已发生的付费调用不进入普通 undo」)。
    - external_effects:本轮 InvocationReceipt(轮级,按 turn_id 已由 caller 过滤)脱敏摘要——
      「已发生的付费调用」展示源。receipt 无 operation_id,不映射具体 op,仅轮级列出。

    Operation.external 当前极少被填(operation.py:389 注释:只有真带外部效果的 op 才有此键),
    故 receipt 是付费调用的**主展示源**,external 信封是**精确到 op 的可逆性判定源**,二者互补
    (§7.4「明确列出将撤销和不能撤销的部分」)。"""
    by_id: dict[str, dict[str, Any]] = {}
    for e in oplog_rows:
        if isinstance(e, dict) and isinstance(e.get("op"), dict):
            oid = e["op"].get("id")
            if oid:
                by_id[oid] = e["op"]
    reversible: list[str] = []
    irreversible: list[dict[str, Any]] = []
    for oid in operation_ids:
        op = by_id.get(oid)
        ext = op.get("external") if isinstance(op, dict) else None
        kind = ext.get("kind") if isinstance(ext, dict) else None
        if kind in _IRREVERSIBLE_EXTERNAL_KINDS:
            irreversible.append({"operation_id": oid, "kind": kind,
                                 "command_id": op.get("command_id", "") if op else ""})
        else:
            reversible.append(oid)   # 无 op(窗口外)也归可逆:caller revert 时找不到 op 自然跳过
    external_effects: list[dict[str, Any]] = []
    for r in (receipts or []):
        if not isinstance(r, dict):
            continue
        external_effects.append({
            "receipt_id": r.get("receipt_id", ""), "provider": r.get("provider", ""),
            "model_id": r.get("model_id", ""), "capability": r.get("capability", ""),
            "status": r.get("status", ""), "cost_ref": r.get("cost_ref"),
        })
    return {"reversible": reversible, "irreversible": irreversible,
            "external_effects": external_effects,
            "reversible_count": len(reversible), "irreversible_count": len(irreversible)}


def changeset_entities_from_oplog_rows(rows: list) -> list:
    """抽本轮 AI op 触及的实体+字段(§7.3 变更卡「按本轮聚合、定位到实体/字段」的数据源)。

    从每条 source=='ai' 无 error 的 op 的 patches 收集 distinct (entity_type, entity_id) 及其
    被改字段集;返回 [{entity_type, entity_id, fields:[...], lifecycle:bool}](按首次出现序,fields
    排序稳定)。lifecycle=该实体有 __entity__ patch(add/delete/soft_delete/restore 等整体生灭)。
    纯函数,供 change_set 记录与单测共用;/api/ai/turns 审计与 P0-C 变更卡直接读,免 op_log 回查。"""
    order: list[tuple[str, str]] = []
    fields_by: dict[tuple[str, str], set[str]] = {}
    lifecycle_by: dict[tuple[str, str], bool] = {}
    for e in rows:
        if not (isinstance(e, dict) and isinstance(e.get("op"), dict)):
            continue
        op = e["op"]
        if op.get("source") != "ai" or op.get("error"):
            continue
        for p in op.get("patches", []):
            if not isinstance(p, dict):
                continue
            et, eid, field = p.get("entity_type"), p.get("entity_id"), p.get("field")
            if not et or not eid:
                continue
            key = (et, eid)
            if key not in fields_by:
                fields_by[key] = set()
                lifecycle_by[key] = False
                order.append(key)
            if field == "__entity__":
                lifecycle_by[key] = True          # 整体生灭(add/delete/…),非某字段改
            elif isinstance(field, str) and field:
                fields_by[key].add(field)
    return [{"entity_type": et, "entity_id": eid,
             "fields": sorted(fields_by[(et, eid)]), "lifecycle": lifecycle_by[(et, eid)]}
            for (et, eid) in order]


# EV_CONFIRMATION 载荷绝不携带的键(安全红线:approval_nonce/冻结 args 只经浏览器 cookie 端点下发,
# AI 子进程与 turn 记录都拿不到;与 persistence.pending_confirmation_frame「nonce 绝不进广播」同源)。
_CONFIRMATION_FORBIDDEN_KEYS = frozenset({"approval_nonce", "nonce", "args", "approval"})


def confirmation_event_from_pending(pending_public: dict, *, status: str = "pending") -> dict[str, Any]:
    """把一条 PendingConfirmation 的**脱敏公开数据**(persistence.pending_confirmation_frame 的形状:
    token/command_id/source/summary/entity_ref/…)聚合成一条 EV_CONFIRMATION 载荷,记进 Turn
    (§7.1 assistant_confirmation:人审确认事件,供 P0-C 确认卡渲染 + 全事件按 turn_id 可追溯)。

    定死 EV_CONFIRMATION 的规范载荷形状:
      { "token": str,            # 定位符;批准/取消经它回 server(GET /api/command/pending-approval 取 nonce)
        "command_id": str,       # 哪个命令待确认
        "source": str,           # 发起者(通常 'ai')
        "summary": str,          # 要做什么(人读描述,脱敏)
        "entity_ref": {...}|None,# 影响锚点(定位目标实体)
        "status": str }          # pending | approved | cancelled | released

    **安全红线**:载荷**绝不含** approval_nonce/nonce/冻结 args——批准材料只经浏览器 cookie 专属端点下发
    (与 pending_confirmation_frame「nonce 绝不进广播」同一裁定)。纯函数、无 I/O,供 server 记录与单测共用;
    非 dict 入参 → 空壳(token='' status=status),不抛。"""
    p = pending_public if isinstance(pending_public, dict) else {}
    ref = p.get("entity_ref")
    return {
        "token": str(p.get("token", "") or ""),
        "command_id": str(p.get("command_id", "") or ""),
        "source": str(p.get("source", "") or ""),
        "summary": str(p.get("summary", "") or ""),
        "entity_ref": ref if isinstance(ref, dict) and ref else None,
        "status": status,
    }


@dataclass(frozen=True)
class TurnEvent:
    """turn 事件流的一条。`kind` ∈ 六种;`data` 是该种的载荷(脱敏:摘要非完整 prompt/bytes,
    绝无 secret)。`event_id` 是幂等键(断线重放去重);`seq` 是本 turn 内单调序。"""
    event_id: str
    turn_id: str
    kind: str
    seq: int
    created_at: float
    data: dict[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        return {"event_id": self.event_id, "turn_id": self.turn_id, "kind": self.kind,
                "seq": self.seq, "created_at": self.created_at, "data": self.data}


@dataclass(frozen=True)
class AssistantTurn:
    """一轮助手交互(§7.1)。state 走状态机;events 是有序事件流(工具调用/改动/确认都归此轮)。"""
    turn_id: str
    actor_user_id: str
    feature_id: str
    state: str
    created_at: float
    updated_at: float
    events: tuple[TurnEvent, ...] = ()
    project_id: str = ""            # 本轮操作的项目(P0-D 整轮撤销 load 正确 ctx 用;旧记录缺 → "")

    def public_dict(self) -> dict[str, Any]:
        return {"turn_id": self.turn_id, "actor_user_id": self.actor_user_id,
                "feature_id": self.feature_id, "state": self.state, "project_id": self.project_id,
                "created_at": self.created_at, "updated_at": self.updated_at,
                "events": [e.public_dict() for e in self.events]}


class TurnError(Exception):
    """非法转移 / 未知 turn / 非法事件种类。"""


class TurnStore:
    """助手 turn 的持久化协议存储(§7.1)。断线可恢复(get_turn 载回全量)+ 事件幂等(同 event_id
    no-op)+ turn 不串(按 turn_id 分桶)。落 assistant_turns.json,与 Operation OpLog 分离
    (turn 是助手会话协议,不是文档撤销栈——整轮撤销在 P0-D 另经 change_set 聚合驱动)。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def create_turn(self, *, actor_user_id: str, feature_id: str, project_id: str = "",
                    turn_id: Optional[str] = None) -> AssistantTurn:
        """开一轮(state=created)。turn_id 可由 caller 传(浏览器请求带来的稳定 id),缺省生成。
        project_id 记本轮操作的项目(P0-D 整轮撤销要 load 该项目 ctx 才能反做)。
        同 turn_id 已存在 → 返回既有(幂等,断线重连不重开)。"""
        tid = (turn_id or uuid.uuid4().hex).strip()
        if not tid:
            raise TurnError("turn_id 不能为空")
        now = time.time()
        with self._lock:
            state = self._load_unlocked()
            if tid in state["turns"]:
                return _turn_from_dict(state["turns"][tid])
            rec = {"turn_id": tid, "actor_user_id": actor_user_id, "feature_id": feature_id,
                   "project_id": project_id,
                   "state": CREATED, "created_at": now, "updated_at": now, "events": []}
            state["turns"][tid] = rec
            self._save_unlocked(state)
            return _turn_from_dict(rec)

    def append_event(self, turn_id: str, kind: str, data: Optional[dict[str, Any]] = None,
                     *, event_id: Optional[str] = None) -> TurnEvent:
        """给一轮追加事件(幂等:同 event_id 已存在 → 返回既有,不重追加)。seq 本 turn 内单调。"""
        if kind not in _EVENT_KINDS:
            raise TurnError(f"未知事件种类: {kind!r}")
        eid = (event_id or uuid.uuid4().hex).strip()
        with self._lock:
            state = self._load_unlocked()
            rec = state["turns"].get(turn_id)
            if rec is None:
                raise TurnError(f"未知 turn: {turn_id!r}")
            for e in rec["events"]:
                if e.get("event_id") == eid:
                    return _event_from_dict(e)                  # 幂等命中
            seq = len(rec["events"])
            ev = {"event_id": eid, "turn_id": turn_id, "kind": kind, "seq": seq,
                  "created_at": time.time(), "data": dict(data or {})}
            rec["events"].append(ev)
            rec["updated_at"] = ev["created_at"]
            self._save_unlocked(state)
            return _event_from_dict(ev)

    def transition(self, turn_id: str, new_state: str) -> AssistantTurn:
        """推进状态;非法转移抛 TurnError(终态无出边)。cancel/fail 释放悬挂确认由 caller 处理。"""
        with self._lock:
            state = self._load_unlocked()
            rec = state["turns"].get(turn_id)
            if rec is None:
                raise TurnError(f"未知 turn: {turn_id!r}")
            cur = rec["state"]
            if new_state == cur:
                return _turn_from_dict(rec)
            if not can_transition(cur, new_state):
                raise TurnError(f"非法状态转移: {cur} → {new_state}")
            rec["state"] = new_state
            rec["updated_at"] = time.time()
            self._save_unlocked(state)
            return _turn_from_dict(rec)

    def get_turn(self, turn_id: str) -> Optional[AssistantTurn]:
        """载回一轮完整状态 + 事件流(断线重连恢复用)。"""
        with self._lock:
            state = self._load_unlocked()
        rec = state["turns"].get(turn_id)
        return _turn_from_dict(rec) if rec is not None else None

    def recent(self, n: int = 20, *, actor_user_id: Optional[str] = None) -> list[AssistantTurn]:
        """最近 n 轮(按 created_at,最新在后);actor 过滤归属。"""
        with self._lock:
            state = self._load_unlocked()
        turns = sorted(state["turns"].values(), key=lambda r: r.get("created_at", 0.0))
        if actor_user_id is not None:
            turns = [r for r in turns if r.get("actor_user_id") == actor_user_id]
        turns = turns[-n:] if n >= 0 else turns
        return [_turn_from_dict(r) for r in turns]

    # ── 持久化(原子写)────────────────────────────────────────────────────
    def _load_unlocked(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema_version": 1, "turns": {}}
        if not isinstance(raw, dict) or not isinstance(raw.get("turns"), dict):
            raise TurnError(f"assistant_turns 文件损坏: {self._path}")
        raw.setdefault("schema_version", 1)
        return raw

    def _save_unlocked(self, state: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f"{self._path.name}.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)


def _event_from_dict(d: dict[str, Any]) -> TurnEvent:
    return TurnEvent(event_id=d["event_id"], turn_id=d["turn_id"], kind=d["kind"],
                     seq=d["seq"], created_at=d["created_at"], data=dict(d.get("data") or {}))


def _turn_from_dict(d: dict[str, Any]) -> AssistantTurn:
    return AssistantTurn(
        turn_id=d["turn_id"], actor_user_id=d.get("actor_user_id", ""),
        feature_id=d.get("feature_id", ""), state=d["state"],
        created_at=d["created_at"], updated_at=d["updated_at"],
        events=tuple(_event_from_dict(e) for e in d.get("events", [])),
        project_id=d.get("project_id", ""),   # 旧记录无此键 → "" 向后兼容
    )
