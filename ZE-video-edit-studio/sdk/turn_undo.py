"""§7.4 P0-D 整轮撤销:把一轮 change_set 的可逆 op 规划成补偿正向命令(Form B)。

**为什么 Form B**:apply reverse_patches 必须走 sealed `document.apply_patch_directly(_replay_capability)`
(undo.py 持模块私有 token,普通业务无法构造——SPEC 01-core/02-data-flow/04-history-rollback)。故整轮
撤销**不自建 producer 反做**,而对每个可逆 op 按其 **patches**(变更的 ground truth,跨命令统一)构造
补偿的**语义命令**(entity.add/update/delete),经普通 dispatch(与 entity.update 同路径、CAS 并发安全、
不碰 sealed)执行。补偿是新 op(补偿事务,非历史改写),本身可再撤(§7.4 语义)。

**纯规划**:`plan_turn_compensation(ops)` 只产计划(命令+参数+guard),不触 doc/dispatch;executor 先校
guard(该 op 的效果此刻仍在、未被后续改动覆盖)再 dispatch,guard 不满足或类型不支持 → 跳过并列出
(§7.4「部分可逆时明确列出将撤销和不能撤销的部分」)。ops 传入序须**新→旧**(= OpLog 追加逆序),
补偿按此序 dispatch(先撤最近的改动)。

**v1 scope(保守)**:处理 entity.update(set/name 字段 → 设回 before)与 entity.add(create → delete);
含删除(delete __entity__ 恢复)/soft_delete/restore/dot-path/ancestry 的 op **跳过并列出**(恢复需精确
id+data+position+schema,留后续)。未识别命令一律跳过(绝不猜测性补偿,防误撤)。
"""

from __future__ import annotations

from typing import Any

_META_FIELD = "__entity__"          # lifecycle patch 的 field(create/delete/soft_delete/restore)
# v1 能安全补偿的字段级 patch op(纯值 set / 改名);其余 op 归“不支持”跳过。
_SIMPLE_FIELD_OPS = frozenset({"set", "name"})


def _entity_key(p: dict[str, Any]) -> "tuple[str, str] | None":
    et, eid = p.get("entity_type"), p.get("entity_id")
    return (et, eid) if (et and eid) else None


def _classify_op(op: dict[str, Any]) -> dict[str, Any]:
    """把一条 op 的 patches 归类成补偿计划项(纯逻辑)。返回 plan item(见模块 docstring)。

    识别规则(按 patch 集合):
      - 恰有一条 create __entity__(且无其他实体)→ 本 op 创建了实体 → 补偿 entity.delete。
      - 全是简单字段 set/name(单实体、无 __entity__、无 ancestry/dot-path)→ 补偿 entity.update 设回 before。
      - 其余(delete/soft_delete/restore __entity__、多实体、ancestry、dot-path、未知)→ skip + 原因。
    """
    op_id = op.get("id", "")
    patches = [p for p in op.get("patches", []) if isinstance(p, dict)]
    if not patches:
        return {"op_id": op_id, "action": "skip", "skip_reason": "无 patches(无实体改动)"}

    keys = {_entity_key(p) for p in patches}
    keys.discard(None)
    if len(keys) != 1:
        return {"op_id": op_id, "action": "skip",
                "skip_reason": f"跨 {len(keys)} 个实体的 op,v1 不自动补偿(避免部分补偿留不一致)"}
    et, eid = next(iter(keys))

    meta = [p for p in patches if p.get("field") == _META_FIELD]
    if meta:
        kinds = {p.get("op") for p in meta}
        if kinds == {"create"}:
            # 本 op 新建实体 → 补偿删除。guard:实体此刻仍在(未被后续删)。
            return {"op_id": op_id, "action": "compensate",
                    "command_id": "entity.delete", "args": {"type": et, "id": eid},
                    "guards": [{"kind": "entity_exists", "entity_type": et, "entity_id": eid}],
                    "undoes": "create"}
        return {"op_id": op_id, "action": "skip",
                "skip_reason": f"含生命周期 patch {sorted(kinds)}(删除/软删/恢复),v1 不自动恢复(需精确 id+data+position)"}

    # 无 __entity__ → 纯字段改。要求全是简单 set/name、无 ancestry/dot-path(否则 before 语义复杂)。
    bad = [p for p in patches
           if p.get("op") not in _SIMPLE_FIELD_OPS
           or p.get("ancestry") is not None
           or "." in str(p.get("field") or "")]
    if bad:
        return {"op_id": op_id, "action": "skip",
                "skip_reason": "含 ancestry/dot-path/非 set 字段 patch,v1 不自动补偿"}

    # 每字段设回 before;guard 校 current==after(未被后续改动覆盖才补偿,并发冲突则跳过)。
    revert_fields: dict[str, Any] = {}
    name_before = None
    guards: list[dict[str, Any]] = []
    for p in patches:
        field = p.get("field")
        if p.get("op") == "name":
            name_before = p.get("before")
            guards.append({"kind": "field_unchanged", "entity_type": et, "entity_id": eid,
                           "field": "name", "expect_after": p.get("after")})
        else:
            revert_fields[field] = p.get("before")
            guards.append({"kind": "field_unchanged", "entity_type": et, "entity_id": eid,
                           "field": field, "expect_after": p.get("after")})
    args: dict[str, Any] = {"type": et, "id": eid, "patches": revert_fields}
    if name_before is not None:
        args["name"] = name_before
    return {"op_id": op_id, "action": "compensate", "command_id": "entity.update",
            "args": args, "guards": guards, "undoes": "update"}


def plan_turn_compensation(ops: list) -> dict[str, Any]:
    """把一轮可逆 op 列表(**新→旧**序)规划成补偿计划。纯函数。

    返回 {"plan": [item...], "compensable": [op_id...], "skipped": [{op_id, reason}...]}。
    executor 按 plan 序对 action=='compensate' 的项校 guard 再 dispatch;skip 项列给用户(§7.4)。
    """
    plan: list[dict[str, Any]] = []
    compensable: list[str] = []
    skipped: list[dict[str, Any]] = []
    for op in ops:
        if not isinstance(op, dict):
            continue
        item = _classify_op(op)
        plan.append(item)
        if item["action"] == "compensate":
            compensable.append(item["op_id"])
        else:
            skipped.append({"op_id": item["op_id"], "reason": item.get("skip_reason", "")})
    return {"plan": plan, "compensable": compensable, "skipped": skipped}
