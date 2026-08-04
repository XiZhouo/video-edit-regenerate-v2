"""builtin_commands.py — 9 个内置 entity 操作 command.

framework 自带, 业务方可直接 dispatch. mod 端在 02-mod/05-code/commands_register.py
里通过 register_builtin_commands(registry) 一键注册全部内置.

内置 commands (entity.* 命名空间, 业务方不要覆盖):
  - entity.add            args={type, data, name?, id?, schema?, read_only?}
  - entity.batch_add      args={items:[{type, data, name?, id?}], on_conflict?}  # 单 Operation 批量;skip=幂等
  - entity.update         args={type, id, patches, expected_version, name?, ai_provenance?}
  - entity.batch_update   args={type, items:[{id, patches, expected_version}]}  # 单 Operation 批量
  - entity.reorder        args={type, ids:[...]}                         # 同类型 canonical 顺序
  - entity.delete         args={type, id, expected_version}            # 软删
  - entity.restore        args={type, id, expected_version}
  - entity.hard_remove    args={type, id}                                # 物理删
  - entity.cascade_delete args={type, id}                                # 级联软删
"""

from __future__ import annotations

import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

# import 路径已被 command.py 注入, 这里直接 import
_LEAF_DIR = Path(__file__).resolve().parent
_ENTITY_DIR = _LEAF_DIR.parent.parent / "01-data-structure" / "02-entity"
_DOCUMENT_DIR = _LEAF_DIR.parent.parent / "01-data-structure" / "01-document"
_OPERATION_DIR = _LEAF_DIR.parent / "02-operation"
for d in (_ENTITY_DIR, _DOCUMENT_DIR, _OPERATION_DIR):
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))

from entity import Actor, AiProvenance, Entity, ZeError, apply_patches  # noqa: E402
from operation import (  # noqa: E402
    ENTITY_META_FIELD, DOCUMENT_TARGET, Operation, Patch, new_operation, external_api,
)

from command import (  # noqa: E402
    CommandMutation, CommandRegistry, CommandSpec, DeferredAction,
)

_OPEN_ID_PREFIXES = ("ou_", "on_")

# C-flip 就绪:AI 合法写命令显式声明含 "ai" 的**稳定**源集——刻意不复用会被 C-flip 翻动的
# _DEFAULT_SOURCES_ALLOWED,这样将来默认排除 ai 后,这些命令仍显式放行 AI、不被误掐。
# = 当前全集,现在零行为变化。危险命令(entity.hard_remove)仍显式排除 ai;
# document.set_acl 的 AI 收紧留待 C-flip 正式迁移时定(Phase 0 已有 owner-guard 兜底)。
_AI_WRITE_SOURCES = ("user", "ai", "sync", "system")


# ==============================================================
# 工厂: 构造 patches + reverse_patches 装成 Operation
# ==============================================================

def _meta_patch(
    et: str,
    eid: str,
    op: str,
    before: Any,
    after: Any,
    *,
    position: int | None = None,
) -> Patch:
    """field=__entity__ 元 patch."""
    return Patch(entity_type=et, entity_id=eid, field=ENTITY_META_FIELD,
                 op=op, before=before, after=after, position=position)


def _field_value_and_presence(data: dict[str, Any], field_name: str) -> tuple[bool, Any]:
    """Read one Patch path while preserving missing versus explicit JSON null."""
    node: Any = data
    for segment in field_name.split("."):
        if type(node) is not dict or segment not in node:
            return False, None
        node = node[segment]
    return True, node


def _field_parent_is_replayable(data: dict[str, Any], field_name: str) -> bool:
    node: Any = data
    for segment in field_name.split(".")[:-1]:
        if type(node) is not dict or type(node.get(segment)) is not dict:
            return False
        node = node[segment]
    return True


def _build_update_field_patches(
    entity_type: str,
    entity_id: str,
    before_data: dict[str, Any],
    patches_in: dict[str, Any],
    *,
    capture_reversible_ancestry: bool,
    before_provenance: dict[str, AiProvenance] | None = None,
    ai_provenance: dict[str, AiProvenance] | None = None,
    require_provenance_binding: bool = False,
) -> tuple[list[Patch], list[Patch]]:
    """Build one exact ordered program and its true reverse program."""
    before_provenance = before_provenance or {}
    if require_provenance_binding and ai_provenance is not None:
        if type(ai_provenance) is not dict or any(
            type(field_name) is not str
            or not field_name
            or type(value) is not AiProvenance
            for field_name, value in ai_provenance.items()
        ):
            raise ZeError(
                "mutation_operation_provenance_invalid",
                "protected AI provenance must be an exact field map",
            )
        unbound = sorted(set(ai_provenance) - set(patches_in))
        if unbound:
            raise ZeError(
                "mutation_operation_provenance_unbound",
                "protected AI provenance must bind to one exact data patch path",
                context={"fields": unbound},
            )
    current = before_data
    forward: list[Patch] = []
    reverse_in_forward_order: list[Patch] = []
    for field_name, after_value in patches_in.items():
        present, before_value = _field_value_and_presence(current, field_name)
        next_data = apply_patches(current, {field_name: after_value})
        ancestry_envelope: dict[str, Any] | None = None
        if (
            capture_reversible_ancestry
            and "." in field_name
            and not _field_parent_is_replayable(current, field_name)
        ):
            root_field = field_name.split(".", 1)[0]
            before_root_present = root_field in current
            after_root_present = root_field in next_data
            ancestry_envelope = {
                "field": root_field,
                "before": {
                    "present": before_root_present,
                    "value": current.get(root_field),
                },
                "after": {
                    "present": after_root_present,
                    "value": next_data.get(root_field),
                },
            }
        provenance_envelope: dict[str, Any] | None = None
        if type(ai_provenance) is dict and field_name in ai_provenance:
            after_provenance = ai_provenance[field_name]
            if type(after_provenance) is not AiProvenance:
                if require_provenance_binding:
                    raise ZeError(
                        "mutation_operation_provenance_invalid",
                        "protected AI provenance value must be AiProvenance",
                        context={"field": field_name},
                    )
            else:
                old_provenance = before_provenance.get(field_name)
                provenance_envelope = {
                    "field": field_name,
                    "before": {
                        "present": old_provenance is not None,
                        "value": (
                            old_provenance.to_dict()
                            if old_provenance is not None
                            else None
                        ),
                    },
                    "after": {
                        "present": True,
                        "value": after_provenance.to_dict(),
                    },
                }
        forward.append(Patch(
            entity_type=entity_type,
            entity_id=entity_id,
            field=field_name,
            op="set",
            before=before_value,
            after=after_value,
            provenance=provenance_envelope,
            ancestry=ancestry_envelope,
        ))
        reverse_provenance = None
        if provenance_envelope is not None:
            reverse_provenance = {
                "field": provenance_envelope["field"],
                "before": provenance_envelope["after"],
                "after": provenance_envelope["before"],
            }
        reverse_ancestry = None
        if ancestry_envelope is not None:
            reverse_ancestry = {
                "field": ancestry_envelope["field"],
                "before": ancestry_envelope["after"],
                "after": ancestry_envelope["before"],
            }
        reverse_in_forward_order.append(Patch(
            entity_type=entity_type,
            entity_id=entity_id,
            field=field_name,
            op="set" if present else "delete",
            before=after_value,
            after=before_value,
            provenance=reverse_provenance,
            ancestry=reverse_ancestry,
        ))
        current = next_data
    return forward, list(reversed(reverse_in_forward_order))


# Asset 生命周期不能在 producer handler 中写文件。W04 可以回滚 Document
# 内存，却无法让已经 add_ref/release 的 meta 文件随之回滚；而 import、duplicate、
# undo、redo 也并不都经过这些内置 handler。唯一安全权威因此是完整 canonical
# Document 的可达性，AssetStore 只在 GC/维护边界统一 scan + reconcile。


# ==============================================================
# entity.add
# ==============================================================

def _execute_entity_add(args: dict[str, Any], actor: Actor, source: str, doc: Any) -> Operation:
    """创建 entity, 加入 doc, 产生 create → delete reverse."""
    e = Entity(
        type=args["type"],
        data=args.get("data") or {},
        name=args.get("name", ""),
        id=args.get("id"),
        schema=args.get("schema"),
        read_only=args.get("read_only", False),
    )
    doc.add(e)
    snapshot = e.to_dict()
    position = doc._entity_collection_position(e)
    return new_operation(
        command_id="entity.add",
        source=source,
        actor=actor.to_dict(),
        document_id=doc.id,
        patches=[_meta_patch(
            e.type, e.id, "create", before=None, after=snapshot,
            position=position,
        )],
        reverse_patches=[_meta_patch(
            e.type, e.id, "delete", before=snapshot, after=None,
            position=position,
        )],
    )


# ==============================================================
# entity.batch_add — 单 Operation 批量建 entity(AI 在 stage 里一次建 N 个 / 任意批量建)
# ==============================================================

def _execute_entity_batch_add(args: dict[str, Any], actor: Actor, source: str, doc: Any) -> Operation:
    """批量建 entity,单 Operation → 一键撤销(reverse=倒序 delete)。每项自带 type(可混型)。

    args={items:[{type, name?, data?, id?, schema?, read_only?}], on_conflict?}。
    与 entity.add 对称(create→delete 元 patch),只是一次性多个、装进同一 Operation。
    供 PipelineSupervisor 把 stage 输出的 created_entities 一次落库,也供任意"建一批"场景复用。

    on_conflict="error"(默认)|"skip":skip 下已存在同 type+id 的项静默跳过(不产 patch、
    不覆盖)——确定性 id 的幂等落库,根治「存在性检查在 dispatch 时、落库在 stage 完成时」
    的竞态窗口(3.55.0,design D3 自动建帧反哺)。
    """
    items = args.get("items")
    if not isinstance(items, list) or not items:
        raise ZeError("validation_failed", "entity.batch_add items must be a non-empty list",
                      context={"command": "entity.batch_add"})
    on_conflict = args.get("on_conflict", "error")
    if on_conflict not in ("error", "skip"):
        raise ZeError("validation_failed",
                      "entity.batch_add on_conflict must be 'error' or 'skip'",
                      context={"command": "entity.batch_add"})
    fwd: list[Patch] = []
    created: list[tuple[str, str, dict[str, Any], int]] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("type"):
            raise ZeError("validation_failed", "entity.batch_add item needs a type",
                          context={"command": "entity.batch_add"})
        if on_conflict == "skip" and item.get("id") \
                and doc.find(item["type"], item["id"]) is not None:
            continue
        e = Entity(
            type=item["type"], data=item.get("data") or {}, name=item.get("name", ""),
            id=item.get("id"), schema=item.get("schema"), read_only=item.get("read_only", False),
        )
        doc.add(e)
        snap = e.to_dict()
        position = doc._entity_collection_position(e)
        fwd.append(_meta_patch(
            e.type, e.id, "create", before=None, after=snap,
            position=position,
        ))
        created.append((e.type, e.id, snap, position))
    rev = [_meta_patch(
        et, eid, "delete", before=snap, after=None, position=position,
    ) for et, eid, snap, position in reversed(created)]
    return new_operation(
        command_id="entity.batch_add", source=source, actor=actor.to_dict(),
        patches=fwd, reverse_patches=rev,
    )


# ==============================================================
# entity.update
# ==============================================================

def _execute_entity_update(args: dict[str, Any], actor: Actor, source: str, doc: Any) -> Operation:
    """改 entity 字段, 每字段产生一个 set patch; reverse 互换 before/after."""
    et, eid = args["type"], args["id"]
    e = doc.get(et, eid)
    expected = args["expected_version"]
    patches_in = args["patches"]                # {field: new_value}

    # 抓 before 快照 (按 patches keys); dot-path key 取**子值** (get_path) → reverse 精确还原子键,
    # 不把整字段写成 stale 值 (P2 改动点2)。顶层 key 仍取整字段。
    fwd, rev = _build_update_field_patches(
        et,
        eid,
        e.data,
        patches_in,
        capture_reversible_ancestry=et in getattr(doc, "_mutation_policies", {}),
        before_provenance={
            field_name: provenance
            for field_name in patches_in
            if (provenance := e.provenance(field_name)) is not None
        },
        ai_provenance=args.get("ai_provenance"),
        require_provenance_binding=et in getattr(doc, "_mutation_policies", {}),
    )
    before_name = e.name   # 改名前抓旧名 (必须在 e.update 之前, 否则拿到的是新名 → 旧 bug 的根源)

    # apply via Entity.update
    e.update(
        patches_in,
        expected_version=expected,
        actor=actor,
        ai_provenance=args.get("ai_provenance"),
        name=args.get("name"),
    )

    # name 改名也进 Operation: 改名时补一对 op="name" 元 patch (before_name 已在 e.update 之前抓)。
    # → undo 重放 rev 还原旧名、redo 重放 fwd 重做新名 (document.apply_patch_directly 的 op=="name" 分支据此回放)。
    new_name = args.get("name")
    if new_name is not None and new_name != before_name:
        fwd.append(_meta_patch(et, eid, "name", before=before_name, after=new_name))
        rev.insert(0, _meta_patch(
            et, eid, "name", before=new_name, after=before_name,
        ))

    return new_operation(
        command_id="entity.update",
        source=source,
        actor=actor.to_dict(),
        patches=fwd,
        reverse_patches=rev,
    )


# ==============================================================
# entity.batch_update — 单 Operation 批量(inspector 多选聚合编辑 / 卡片列表批量写回)
# ==============================================================

def _execute_entity_batch_update(args: dict[str, Any], actor: Actor, source: str, doc: Any) -> Operation:
    """对同型多 entity 应用字段 patches; 单 Operation → undo 一键回滚.

    args={type, items:[{id, patches, expected_version}]}.
    整体拒绝语义: 先全量预检(存在/可写/未软删/CAS 版本), 任一不过直接抛、doc 零变更;
    apply 途中剩余异常(schema 违例等)→ 已应用项按快照**精确还原 data+version**后原样抛出
    (e.update 回滚会把版本再 bump, 持原版本的并发客户端会被假 CAS 冲突击中 —— 故走快照还原),
    保证 doc 与 OpLog 不出现"半套用且无 Operation"的脱节.
    """
    et = args["type"]
    items = args["items"]
    if not isinstance(items, list) or not items:
        raise ZeError(
            "validation_failed", "entity.batch_update items must be a non-empty list",
            context={"command": "entity.batch_update"},
        )

    # 预检(零变更): 存在性(doc.get 抛 entity_not_found)+ 可写 + 未软删 + CAS 版本
    resolved: list[tuple[Any, dict[str, Any], int]] = []
    for item in items:
        e = doc.get(et, item["id"])
        expected = item["expected_version"]
        if e.read_only:
            raise ZeError(
                "read_only_violation", f"entity {e.id!r} is read-only",
                context={"entity_id": e.id, "entity_type": et},
            )
        if e.deleted:
            raise ZeError(
                "entity_soft_deleted", f"entity {e.id!r} is soft-deleted; restore() first",
                context={"entity_id": e.id, "entity_type": et},
            )
        if e.version != expected:
            raise ZeError(
                "cas_version_mismatch",
                f"entity {e.id!r} expected v{expected} got v{e.version}",
                context={"entity_id": e.id, "entity_type": et,
                         "expected_version": expected, "current_version": e.version},
            )
        resolved.append((e, dict(item["patches"]), expected))

    fwd: list[Patch] = []
    rev: list[Patch] = []
    applied: list[tuple[Any, dict[str, Any], int]] = []   # (entity, before_data 全量, before_version)
    try:
        for e, patches_in, expected in resolved:
            before_data = e.data                # property 返回拷贝 → 即快照
            before_version = e.version
            entity_fwd, entity_rev = _build_update_field_patches(
                et,
                e.id,
                before_data,
                patches_in,
                capture_reversible_ancestry=et in getattr(doc, "_mutation_policies", {}),
                before_provenance={
                    field_name: provenance
                    for field_name in patches_in
                    if (provenance := e.provenance(field_name)) is not None
                },
                ai_provenance=args.get("ai_provenance"),
                require_provenance_binding=et in getattr(doc, "_mutation_policies", {}),
            )
            e.update(patches_in, expected_version=expected, actor=actor,
                     ai_provenance=args.get("ai_provenance"))
            applied.append((e, before_data, before_version))
            # dot-path key 取**子值**(get_path),与单条 entity.update 一致;否则 reverse 把 'box.w' 当扁平
            # key 取 None,undo 会把嵌套子字段写成 None(数据损坏)。顶层 key 仍取整字段。
            fwd.extend(entity_fwd)
            # Each later entity must be undone first; prepend its already-reversed
            # per-entity program to the aggregate reverse sequence.
            rev[0:0] = entity_rev
    except Exception:
        # 任何 apply 失败(不只 ZeError)都精确还原: data+version 回到 before 快照.
        # 刻意触私有态: 公开 update() 会再 bump 版本, 无法表达"回到原版本"的精确语义;
        # builtin_commands 是 framework 内部(与 cascade_delete 触 doc 内部同一信任边界).
        for e, before_data, before_version in reversed(applied):
            e._data = dict(before_data)          # noqa: SLF001
            e._version = before_version          # noqa: SLF001
        raise

    return new_operation(
        command_id="entity.batch_update",
        source=source,
        actor=actor.to_dict(),
        patches=fwd,
        reverse_patches=rev,
    )


# ==============================================================
# entity.reorder (通用拖拽排序:重写 order 字段 → 连续整数归一)
# ==============================================================

_ORDER_FIELD = "order"
_ORDER_INF = float("inf")


def _order_key(entity: Any, idx: int) -> tuple:
    """排序键:有数字 order 用之、无则排到末尾;同值/缺失按插入序(idx)兜底(去重复/空洞歧义)。"""
    val = entity.data.get(_ORDER_FIELD)
    return (val if isinstance(val, (int, float)) and not isinstance(val, bool) else _ORDER_INF, idx)


def _execute_entity_reorder(args: dict[str, Any], actor: Actor, source: str, doc: Any) -> Operation:
    """通用拖拽排序:把 id 移到 before_id 之前(before_id 缺/None → 移到末尾),
    对该 type 的非删实体重写 `order` 为连续整数,仅对 order 真变的实体下 patch。

    args={type, id, before_id?}。复用 entity.batch_update → 单 Operation、逐实体 CAS、一键 undo。
    平的有序集合走本命令即可;树/图等特殊排序语义由 mod 自声明专属命令。
    """
    et = args["type"]
    move_id = args["id"]
    before_id = args.get("before_id")
    doc.get(et, move_id)   # 存在性:不存在抛 entity_not_found

    collection = list(doc.list(et))          # 非删,插入序
    ordered = [e for _, e in sorted(((i, e) for i, e in enumerate(collection)),
                                    key=lambda pair: _order_key(pair[1], pair[0]))]
    ids = [e.id for e in ordered]
    if move_id not in ids:                   # 软删的/跨集合 → 视为无操作
        return new_operation(command_id="entity.reorder", source=source,
                             actor=actor.to_dict(), patches=[], reverse_patches=[])
    ids.remove(move_id)
    if before_id is not None and before_id in ids:
        ids.insert(ids.index(before_id), move_id)
    else:
        ids.append(move_id)                  # before_id 缺/无效 → 末尾

    by_id = {e.id: e for e in ordered}
    items = []
    for new_pos, eid in enumerate(ids):
        ent = by_id[eid]
        cur = ent.data.get(_ORDER_FIELD)
        if cur != new_pos:
            items.append({"id": eid, "patches": {_ORDER_FIELD: new_pos},
                          "expected_version": ent.version})
    if not items:                            # 已在目标位,零变更
        return new_operation(command_id="entity.reorder", source=source,
                             actor=actor.to_dict(), patches=[], reverse_patches=[])
    # 复用 batch_update 落地(逐实体 CAS + 快照还原不变量),再以本命令名重建 Operation 记录
    # (Operation frozen 不可改 command_id;重建只换归属,patches 原样)。
    sub = _execute_entity_batch_update({"type": et, "items": items,
                                        "ai_provenance": args.get("ai_provenance")},
                                       actor, source, doc)
    return new_operation(command_id="entity.reorder", source=source, actor=actor.to_dict(),
                         patches=list(sub.patches), reverse_patches=list(sub.reverse_patches))


# ==============================================================
# entity.delete (软删)
# ==============================================================

def _execute_entity_delete(args: dict[str, Any], actor: Actor, source: str, doc: Any) -> Operation:
    et, eid = args["type"], args["id"]
    e = doc.get(et, eid)
    if e.deleted:
        e._guard_writable()
        e._guard_version(args["expected_version"])
        return new_operation(
            command_id="entity.delete", source=source, actor=actor.to_dict(),
            patches=[], reverse_patches=[],
        )
    before_snapshot = e.to_dict()
    e.delete(expected_version=args["expected_version"], actor=actor)
    after_snapshot = e.to_dict()
    return new_operation(
        command_id="entity.delete",
        source=source,
        actor=actor.to_dict(),
        patches=[_meta_patch(et, eid, "soft_delete", before_snapshot, after_snapshot)],
        reverse_patches=[_meta_patch(et, eid, "restore", after_snapshot, before_snapshot)],
    )


# ==============================================================
# entity.restore
# ==============================================================

def _execute_entity_restore(args: dict[str, Any], actor: Actor, source: str, doc: Any) -> Operation:
    et, eid = args["type"], args["id"]
    e = doc.get(et, eid)
    before_snapshot = e.to_dict()
    e.restore(expected_version=args["expected_version"], actor=actor)
    after_snapshot = e.to_dict()
    if before_snapshot == after_snapshot:
        return new_operation(
            command_id="entity.restore", source=source, actor=actor.to_dict(),
            patches=[], reverse_patches=[],
        )
    return new_operation(
        command_id="entity.restore",
        source=source,
        actor=actor.to_dict(),
        patches=[_meta_patch(et, eid, "restore", before_snapshot, after_snapshot)],
        reverse_patches=[_meta_patch(et, eid, "soft_delete", after_snapshot, before_snapshot)],
    )


# ==============================================================
# entity.hard_remove
# ==============================================================

def _execute_entity_hard_remove(args: dict[str, Any], actor: Actor, source: str, doc: Any) -> Operation:
    """物理删. reverse 恢复整 entity (用删除时的 snapshot)."""
    et, eid = args["type"], args["id"]
    e = doc.get(et, eid)                         # 不存在抛 entity_not_found
    snapshot = e.to_dict()
    position = doc._entity_collection_position(e)
    doc.hard_remove(et, eid)
    return new_operation(
        command_id="entity.hard_remove",
        source=source,
        actor=actor.to_dict(),
        patches=[_meta_patch(
            et, eid, "delete", snapshot, None, position=position,
        )],
        reverse_patches=[_meta_patch(
            et, eid, "create", None, snapshot, position=position,
        )],
    )


# ==============================================================
# entity.cascade_delete
# ==============================================================

def _execute_entity_cascade_delete(args: dict[str, Any], actor: Actor, source: str, doc: Any) -> Operation:
    """级联软删. patches 含所有被删 entity 的 soft_delete."""
    et, eid = args["type"], args["id"]
    before_snapshots = {
        (member_type, member.id): member.to_dict()
        for member_type in doc.types()
        for member in doc.list(member_type, include_deleted=True)
    }
    removed = doc.cascade_delete(et, eid, actor=actor)

    patches: list[Patch] = []
    reverse_patches: list[Patch] = []
    for (rt, rid) in removed:
        e = doc.find(rt, rid)
        if e is None:
            continue
        before_snapshot = before_snapshots[(rt, rid)]
        after_snapshot = e.to_dict()
        patches.append(_meta_patch(rt, rid, "soft_delete", before_snapshot, after_snapshot))
        reverse_patches.insert(
            0, _meta_patch(rt, rid, "restore", after_snapshot, before_snapshot),
        )

    return new_operation(
        command_id="entity.cascade_delete",
        source=source,
        actor=actor.to_dict(),
        patches=patches,
        reverse_patches=reverse_patches,
    )


# ==============================================================
# 注册入口
# ==============================================================

# ==============================================================
# document.rename — 文档级元数据变更(经 Operation/__document__ 通道)
#
# v3 红线:一律单 Document。document.new/open/save_as/close/delete 属多文档/工作台层,
# 不在单 skill 实现;document.snapshot 属 04-history-rollback(未实装)。
# 故 framework 内置的 document.* 仅 rename(改当前文档名)—— 唯一不破坏单文档红线的。
# ==============================================================

def _required_document_expected_version(args: dict[str, Any]) -> int:
    value = args.get("expected_version")
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ZeError(
            "validation_failed",
            "document metadata commands require a positive expected_version",
            context={"field": "expected_version"},
        )
    return value


# 框架保留的本机全权/运维身份:CLI(__system__,scripts/ze_command.py)与 MCP loopback
# 全权(__mcp__,sdk/mcp_bridge._should_use_local_actor)。它们按设计绕过项目 ACL(等同持有
# 机器 shell 的运维),user_id 永不等于真实 owner —— owner-only 门若不豁免它们,接管无主/
# 孤儿项目、部署预置这类运维路径会被误伤。真实用户注册禁 "__" 前缀(auth._validate_username),
# 故 token/HTTP 通道的远程用户无法伪造成这两个身份来套用豁免。
_ACL_ADMIN_USER_IDS = frozenset({"__system__", "__mcp__"})


def _require_document_acl_owner(actor: Actor, doc: Any) -> None:
    """set_acl 是所有权/共享变更 —— 只许文档 owner。收口进命令层让 HTTP/MCP/CLI 三通道共用同一门
    (此前 owner-only 只在 server HTTP 层,协作者+EDIT 经 MCP 可绕过自设为 owner,安全漏洞)。

    空 owner(无主公共项目)对普通用户一律拒:否则任何可访问者都能把自己设成独占 owner。
    仅本机全权/运维保留身份豁免(_ACL_ADMIN_USER_IDS),用于接管无主项目/部署预置。
    """
    uid = getattr(actor, "user_id", "")
    if uid in _ACL_ADMIN_USER_IDS:
        return
    owner = (getattr(doc, "acl", None) or {}).get("owner", "")
    if not owner or uid != owner:
        raise ZeError("acl_denied", "only the owner can change sharing")


def _execute_document_set_acl(args: dict[str, Any], actor: Actor, source: str, doc: Any) -> Operation:
    """改文档 ACL(owner/collaborators),产 __document__ 级 set patch。

    args={acl, expected_version}。owner-only 语义校验在命令层(_require_document_acl_owner,
    HTTP/MCP/CLI 共用);Document 本身只存 acl dict。
    """
    _require_document_acl_owner(actor, doc)
    before = doc.set_acl(
        args["acl"], expected_version=_required_document_expected_version(args),
    )
    after = doc.acl
    did = doc.id
    return new_operation(
        command_id="document.set_acl",
        source=source,
        actor=actor.to_dict(),
        patches=[Patch(entity_type=DOCUMENT_TARGET, entity_id=did, field="acl",
                       op="set", before=before, after=after)],
        reverse_patches=[Patch(entity_type=DOCUMENT_TARGET, entity_id=did, field="acl",
                               op="set", before=after, after=before)],
    )


def _execute_document_rename(args: dict[str, Any], actor: Actor, source: str, doc: Any) -> Operation:
    """改当前文档名,产 __document__ 级 set patch(走标准 Operation/OpLog/广播)。"""
    before = doc.rename(
        args["name"], expected_version=_required_document_expected_version(args),
    )
    after = doc.name
    did = doc.id
    return new_operation(
        command_id="document.rename",
        source=source,
        actor=actor.to_dict(),
        patches=[Patch(entity_type=DOCUMENT_TARGET, entity_id=did, field="name",
                       op="set", before=before, after=after)],
        reverse_patches=[Patch(entity_type=DOCUMENT_TARGET, entity_id=did, field="name",
                               op="set", before=after, after=before)],
    )


# ==============================================================
# notify.push — 框架自带「推送指定飞书账号」命令(机制在 sdk/notify.py 薄驱动)
# ==============================================================
# 收件人模型:open_id 直填(ou_/on_ 前缀)或 ze.notify.recipients 名册 key 解析。
# producer 只解析参数、执行纯校验并返回状态为 pending 的意图 Operation。
# 滑动窗口额度、EffectLedger、真正 provider 与 NotifyLog 全部放在 DeferredAction:
# 只有 Document + OpLog 耐久且 canonical event 被接受后才可执行。动作失败只记
# ActionReceipt,不能把已成立的业务提交改写为失败。


def parse_recipient_roster(config: Any) -> dict[str, str]:
    """解析 ze.notify.recipients(逗号分隔 `key:open_id` 或 `key=open_id`)→ {key: open_id}。"""
    raw = ""
    if config is not None:
        raw = str(config.get("ze.notify.recipients", "") or "")
    roster: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        sep = ":" if ":" in pair else ("=" if "=" in pair else "")
        if not sep:
            continue
        key, _, oid = pair.partition(sep)
        key, oid = key.strip(), oid.strip()
        if key and oid:
            roster[key] = oid
    return roster


def _feishu_external_id(user: Any) -> str:
    if getattr(user, "status", "") == "disabled":
        return ""
    if getattr(user, "auth_provider", "") != "feishu":
        return ""
    oid = str(getattr(user, "external_id", "") or "").strip()
    return oid if oid.startswith(_OPEN_ID_PREFIXES) else ""


def _known_user_for_recipient(recipient_raw: str, resolved: str, user_store: Any) -> bool:
    if user_store is None:
        return False
    try:
        users = user_store.list_users()
    except Exception:  # noqa: BLE001 - policy fallback: unknown, not allowed
        return False
    for user in users:
        oid = _feishu_external_id(user)
        if not oid:
            continue
        if recipient_raw == getattr(user, "user_id", "") or recipient_raw == oid or resolved == oid:
            return True
    return False


def resolve_recipient(recipient: str, *, config: Any, user_store: Any | None = None) -> str:
    """把 recipient(open_id / 名册 key / ZE user_id)解析成 open_id。无法解析抛 ZeError。"""
    recipient = str(recipient or "").strip()
    if not recipient:
        raise ZeError("validation_failed", "缺少收件人", context={})
    if recipient.startswith(_OPEN_ID_PREFIXES):
        return recipient
    roster = parse_recipient_roster(config)
    if recipient in roster:
        return roster[recipient]
    if user_store is not None:
        try:
            user = user_store.get(recipient)
        except Exception:  # noqa: BLE001 - fall through to normal validation error
            user = None
        if user is not None:
            oid = _feishu_external_id(user)
            if oid:
                return oid
            raise ZeError("validation_failed",
                          f"收件人 {recipient!r} 不是可发送飞书消息的 ZE 用户",
                          context={"recipient": recipient})
    raise ZeError("validation_failed",
                  f"无法解析收件人 {recipient!r}(非 open_id、非 ze.notify.recipients 名册 key、非可发送 ZE 用户)",
                  context={"recipient": recipient, "roster_keys": list(roster)})


# 防滥发:每发起人滑动窗口限流(进程内;跨进程严格限流留运维层)。
_NOTIFY_RATE_LOCK = threading.Lock()
_NOTIFY_SENDS: dict[str, deque] = {}


def _check_notify_rate(user_id: str, limit_per_min: int) -> None:
    if limit_per_min <= 0:
        return
    now = time.time()
    key = user_id or "__anon__"
    with _NOTIFY_RATE_LOCK:
        dq = _NOTIFY_SENDS.setdefault(key, deque())
        while dq and now - dq[0] > 60.0:
            dq.popleft()
        if len(dq) >= limit_per_min:
            raise ZeError("rate_limited",
                          f"通知发送过于频繁(>{limit_per_min}/分钟),稍后再试", context={})
        dq.append(now)


def _enforce_recipient_policy(
    recipient_raw: str,
    resolved: str,
    policy: str,
    config: Any,
    user_store: Any | None = None,
) -> None:
    """收件人白名单策略:防把框架当任意飞书轰炸器。open_id 仅来自登录/名册,故 known/any 放行合法 open_id。"""
    policy = (policy or "roster_or_known").strip()
    if policy == "any":
        return
    roster = parse_recipient_roster(config)
    in_roster = recipient_raw in roster or resolved in set(roster.values())
    if policy == "roster" and not in_roster:
        raise ZeError("validation_failed",
                      "收件人不在 ze.notify.recipients 名册(recipient_policy=roster)",
                      context={"recipient": recipient_raw})
        return
    known = _known_user_for_recipient(recipient_raw, resolved, user_store)
    direct_open_id = recipient_raw.startswith(_OPEN_ID_PREFIXES) and resolved == recipient_raw
    if policy == "roster_or_known" and (in_roster or known or direct_open_id):
        return
    if policy == "known_users" and not (in_roster or known):
        raise ZeError("validation_failed",
                      "收件人不在 ZE-system 已知用户或 ze.notify.recipients 名册",
                      context={"recipient": recipient_raw})


def _register_notify_push(registry: CommandRegistry) -> None:
    """注册 notify.push;config/notify_log 在 execute 时经 getattr(registry) 懒读(适配注册早于挂载)。"""

    def _execute_notify_push(
        args: dict[str, Any], actor: Actor, source: str, doc: Any,
    ) -> CommandMutation:
        # 延迟 import:避免与 sdk barrel 循环。available_channels 是纯读校验;
        # _notify 在下方 closure 中只会由 CommitCoordinator 执行。
        from sdk.notify import available_channels, notify as _notify

        config = getattr(registry, "config", None)
        effect_ledger = getattr(registry, "effect_ledger", None)
        notify_log = getattr(registry, "notify_log", None)
        user_store = getattr(registry, "user_store", None)
        channel = str(args.get("channel") or "feishu")
        allowed_channels = available_channels()
        if channel not in allowed_channels:
            raise ZeError("validation_failed", f"未知通知渠道: {channel!r}",
                          context={"channel": channel, "allowed": allowed_channels})
        message = str(args.get("message") or "")
        if not message.strip():
            raise ZeError("validation_failed", "通知正文为空", context={})
        recipient_raw = str(args.get("recipient", ""))
        recipient = resolve_recipient(recipient_raw, config=config, user_store=user_store)
        policy = str((config.get("ze.notify.recipient_policy", "roster_or_known") if config else "")
                     or "roster_or_known")
        _enforce_recipient_policy(recipient_raw, recipient, policy, config, user_store=user_store)
        try:
            limit = int((config.get("ze.notify.rate_limit", 30) if config else 30) or 30)
        except (TypeError, ValueError):
            limit = 30
        payload: dict[str, Any] = {"text": message}
        idem = str(args.get("idempotency_key") or "")
        template = str(args.get("template") or "")
        if idem:
            payload["idempotency_key"] = idem

        actor_user_id = str(getattr(actor, "user_id", "") or "")

        # Operation 审计的是“已接受外发意图”,不是“已发送”。request_id / 延迟等
        # provider 结果此时不存在,绝不伪造;message、idempotency key 与任何凭证也不进 OpLog。
        op = new_operation(
            command_id="notify.push",
            source=source,
            actor=actor.to_dict(),
            patches=[],
            reverse_patches=[],
            external=external_api(f"{channel}/im/messages-send", status="pending"),
        )

        def _send_once() -> dict[str, Any]:
            _check_notify_rate(actor_user_id, limit)
            res = _notify(config, channel=channel, recipient=recipient, payload=payload,
                          timeout=15, notify_log=notify_log,
                          template=template,
                          actor_user_id=actor_user_id, source="command")
            if not res.ok:
                raise ZeError("notify_failed", res.error or "发送失败",
                              context={"recipient": recipient, "channel": channel})
            return res.to_dict()

        def _send_after_commit(_commit_id: str) -> dict[str, Any]:
            # 额度、ledger 文件、provider 和 NotifyLog 都在这条 after-commit 边界内。
            # EffectLedger 只保存 request hash + 无密钥响应,不会持久化 message。
            if effect_ledger is not None and idem:
                return effect_ledger.run(
                    scope=f"notify.push.{channel}",
                    key=idem,
                    effect_payload={
                        "channel": channel,
                        "recipient": recipient,
                        "message": message,
                        "template": template,
                    },
                    action=_send_once,
                )
            return _send_once()

        return CommandMutation(
            operations=(op,),
            after_commit=(DeferredAction(
                id=f"notify.push:{op.id}",
                name="notify.push.send",
                run=_send_after_commit,
            ),),
        )

    registry.register(CommandSpec(
        id="notify.push",
        label="推送消息(飞书)",
        kind="native",
        description="向指定账号(open_id 或 ze.notify.recipients 名册 key)发一条外发通知;"
                    "Operation 只记 pending 意图;提交与事件接受后才发送并写 NotifyLog。",
        touches=[],
        params_schema={
            "type": "object",
            "properties": {
                "recipient": {
                    "type": "string",
                    "description": "ZE user_id(推荐)、open_id(ou_/on_) 或名册 key",
                    "x-ze-widget": "user-picker",
                    "x-ze-channel": "feishu",
                    "x-ze-raw-fallback": True,
                },
                "message": {"type": "string", "description": "预渲染正文(可含 <at user_id=…> 提及)"},
                "channel": {"type": "string", "description": "通知渠道,默认 feishu"},
                "template": {"type": "string", "description": "模板标识(仅留痕用)"},
                "idempotency_key": {"type": "string"},
            },
            "required": ["recipient", "message"],
        },
        execute=_execute_notify_push,
    ))


def register_builtin_commands(registry: CommandRegistry) -> None:
    """一键注册全部内置 command(9 个 entity.* + document.rename / document.set_acl + notify.push)。"""
    registry.register(CommandSpec(
        id="entity.add",
        label="新增 entity",
        kind="native",
        sources_allowed=_AI_WRITE_SOURCES,
        execute=_execute_entity_add,
    ))
    registry.register(CommandSpec(
        id="entity.batch_add",
        label="批量新增 entity",
        kind="native",
        description="一次建 N 个 entity(每项自带 type,可混型);单 Operation 一键撤销。供 stage 建实体/批量建复用。",
        sources_allowed=_AI_WRITE_SOURCES,
        execute=_execute_entity_batch_add,
    ))
    registry.register(CommandSpec(
        id="entity.update",
        label="更新 entity",
        kind="native",
        sources_allowed=_AI_WRITE_SOURCES,
        execute=_execute_entity_update,
    ))
    registry.register(CommandSpec(
        id="entity.batch_update",
        label="批量更新 entity",
        kind="native",
        description="对同型多个 entity 应用字段 patches;单 Operation 一键 undo;任一项版本不匹配整体拒绝。",
        sources_allowed=_AI_WRITE_SOURCES,
        execute=_execute_entity_batch_update,
    ))
    registry.register(CommandSpec(
        id="entity.reorder",
        label="拖拽排序 entity",
        kind="native",
        description="把 id 移到 before_id 之前(缺则末尾),重写该 type 的 order 字段为连续整数;"
                    "复用 batch_update 单 Operation 一键 undo。平的有序列表通用;树/图特殊语义 mod 自声明。",
        sources_allowed=_AI_WRITE_SOURCES,
        execute=_execute_entity_reorder,
    ))
    registry.register(CommandSpec(
        id="entity.delete",
        label="软删 entity",
        kind="native",
        sources_allowed=_AI_WRITE_SOURCES,
        execute=_execute_entity_delete,
    ))
    registry.register(CommandSpec(
        id="entity.restore",
        label="恢复 entity",
        kind="native",
        sources_allowed=_AI_WRITE_SOURCES,
        execute=_execute_entity_restore,
    ))
    registry.register(CommandSpec(
        id="entity.hard_remove",
        label="硬删 entity",
        kind="native",
        sources_allowed=("user", "system"),       # 拒绝 ai / sync 直接硬删
        execute=_execute_entity_hard_remove,
    ))
    registry.register(CommandSpec(
        id="entity.cascade_delete",
        label="级联软删 entity",
        kind="native",
        sources_allowed=_AI_WRITE_SOURCES,
        execute=_execute_entity_cascade_delete,
    ))
    registry.register(CommandSpec(
        id="document.rename",
        label="重命名文档",
        kind="native",
        sources_allowed=_AI_WRITE_SOURCES,
        execute=_execute_document_rename,
    ))
    registry.register(CommandSpec(
        id="document.set_acl",
        label="修改文档权限",
        kind="native",
        execute=_execute_document_set_acl,
    ))
    _register_notify_push(registry)
