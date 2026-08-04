"""test_undo.py — UndoController + rollback 工厂 + apply_patch_directly 测试.

覆盖:
  - UndoController: undo / redo / can_undo / can_redo / 空场景
  - group_id 聚合: 同 group_id 多 op 一次 undo 反所有
  - rollback_field 工厂: forward/reverse patches 形态
  - rollback_entity_to_op 工厂: 累积反向 patches
  - apply_patch_directly: 6 种 op 分发 (set / delete / create / soft_delete /
    restore / hard_remove / name) + CAS bump version
  - CheckpointManager: create / list / restore / fork
  - realtime_checkpoint group/session rollback conflict detection
"""

from __future__ import annotations

import json
import inspect
import pickle
import sys
from dataclasses import fields, replace
from pathlib import Path

import pytest

# v3 平铺: 注入 兄弟目录
_HERE = Path(__file__).resolve().parent
_CORE = _HERE.parent.parent
_PATHS = [
    _CORE / "01-data-structure" / "01-document",
    _CORE / "01-data-structure" / "02-entity",
    _CORE / "03-commands-operations" / "02-operation",
]
for _p in _PATHS:
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from checkpoint import CheckpointManager, CheckpointMeta
from document import Document, _HISTORY_REPLAY_CAPABILITY
from entity import Actor, Entity, ZeError
from operation import ENTITY_META_FIELD, OpLog, Patch, new_operation
import history_authority as authority_protocol
from undo import (
    DeferredHistoryRecord,
    UndoController,
    _verify_history_replay_derivation,
    rollback_entity_to_op,
    rollback_field,
)
from undo import rollback_operation_group


def _apply_patch(doc, patch, *, actor):
    doc.apply_patch_directly(
        patch,
        actor=actor,
        _replay_capability=_HISTORY_REPLAY_CAPABILITY,
    )


# ==============================================================
# 辅助
# ==============================================================

def _doc() -> Document:
    return Document(schema_version="1.0.0", collaboration_mode="with_save")


def _actor(user_id: str = "u1") -> Actor:
    return Actor(user_id=user_id)


def _make_op(
    *, command_id="cmd", patches, reverse_patches, group_id="",
    kind="native", actor=None, **extra,
):
    return new_operation(
        command_id=command_id,
        source="user",
        actor=(actor or _actor()).to_dict(),
        patches=patches,
        reverse_patches=reverse_patches,
        group_id=group_id,
        kind=kind,
        **extra,
    )


def test_history_replay_derivation_verifier_rejects_forged_undo_and_accepts_exact_group():
    first = _make_op(
        command_id="first",
        patches=(Patch("t", "e1", "x", "set", 0, 1),),
        reverse_patches=(Patch("t", "e1", "x", "set", 1, 0),),
        group_id="g",
        trace_id="trace-first",
    )
    second = _make_op(
        command_id="second",
        patches=(Patch("t", "e1", "x", "set", 1, 2),),
        reverse_patches=(Patch("t", "e1", "x", "set", 2, 1),),
        group_id="g",
        trace_id="trace-second",
    )
    exact = _make_op(
        command_id="undo",
        patches=(
            Patch("t", "e1", "x", "set", 2, 1),
            Patch("t", "e1", "x", "set", 1, 0),
        ),
        reverse_patches=(
            Patch("t", "e1", "x", "set", 0, 1),
            Patch("t", "e1", "x", "set", 1, 2),
        ),
        group_id="g",
        trace_id="trace-second",
        kind="rollback",
    )
    rows = ({"op": first.to_dict()}, {"op": second.to_dict()})
    assert _verify_history_replay_derivation("undo", rows, exact)

    forged = _make_op(
        command_id="undo",
        patches=exact.reverse_patches,
        reverse_patches=exact.patches,
        group_id="g",
        trace_id="trace-second",
        kind="rollback",
    )
    assert not _verify_history_replay_derivation("undo", rows, forged)


def test_history_replay_derivation_verifier_accepts_only_redo_of_committed_undo():
    original = _make_op(
        command_id="native",
        patches=(Patch("t", "e1", "x", "set", 0, 1),),
        reverse_patches=(Patch("t", "e1", "x", "set", 1, 0),),
        group_id="g",
        trace_id="trace",
    )
    undo = _make_op(
        command_id="undo",
        patches=original.reverse_patches,
        reverse_patches=original.patches,
        group_id="g",
        trace_id="trace",
        kind="rollback",
    )
    redo = _make_op(
        command_id="redo",
        patches=undo.reverse_patches,
        reverse_patches=undo.patches,
        group_id="g",
        trace_id="trace",
        kind="rollback",
    )
    assert _verify_history_replay_derivation(
        "redo", ({"op": undo.to_dict()},), redo,
    )
    assert not _verify_history_replay_derivation(
        "redo", ({"op": original.to_dict()},), redo,
    )


# ==============================================================
# apply_patch_directly: 字段级
# ==============================================================

def test_apply_patch_set():
    doc = _doc()
    e = Entity(type="t", data={"x": 1}, id="e1")
    doc.add(e)
    actor = _actor()
    patch = Patch("t", "e1", "x", "set", before=1, after=42)
    _apply_patch(doc, patch, actor=actor)
    assert doc.get("t", "e1").data["x"] == 42
    # CAS bump 了 version
    assert doc.get("t", "e1").version == 2


def test_apply_patch_set_new_field():
    """set 不存在的字段 = 新建字段."""
    doc = _doc()
    doc.add(Entity(type="t", data={}, id="e1"))
    _apply_patch(doc,
        Patch("t", "e1", "newf", "set", None, "hello"), actor=_actor(),
    )
    assert doc.get("t", "e1").data["newf"] == "hello"


def test_apply_patch_delete_field_sets_none():
    doc = _doc()
    doc.add(Entity(type="t", data={"x": 1, "y": 2}, id="e1"))
    _apply_patch(doc,
        Patch("t", "e1", "x", "delete", before=1, after=None),
        actor=_actor(),
    )
    # 字段级 delete: 我们当前实现是 set to None
    assert doc.get("t", "e1").data["x"] is None
    # y 保留
    assert doc.get("t", "e1").data["y"] == 2


def test_apply_patch_name_change():
    doc = _doc()
    doc.add(Entity(type="t", data={}, name="old", id="e1"))
    _apply_patch(doc,
        Patch("t", "e1", "__name__", "name", before="old", after="newname"),
        actor=_actor(),
    )
    assert doc.get("t", "e1").name == "newname"


def test_apply_patch_unknown_op_raises():
    doc = _doc()
    doc.add(Entity(type="t", data={}, id="e1"))
    with pytest.raises(ZeError) as ex:
        _apply_patch(doc,
            Patch("t", "e1", "x", "bogus", None, 1), actor=_actor(),
        )
    assert ex.value.code == "validation_failed"


def test_apply_patch_set_missing_entity_raises():
    doc = _doc()
    with pytest.raises(ZeError) as ex:
        _apply_patch(doc,
            Patch("t", "nope", "x", "set", None, 1), actor=_actor(),
        )
    assert ex.value.code == "entity_not_found"


# ==============================================================
# apply_patch_directly: 元操作
# ==============================================================

def test_apply_patch_create_entity():
    doc = _doc()
    new_e_dict = Entity(type="t", data={"x": 1}, name="created", id="new1").to_dict()
    _apply_patch(doc,
        Patch("t", "new1", ENTITY_META_FIELD, "create", None, new_e_dict),
        actor=_actor(),
    )
    e = doc.get("t", "new1")
    assert e.data["x"] == 1
    assert e.name == "created"


def test_apply_patch_create_already_exists_raises():
    doc = _doc()
    e = Entity(type="t", data={}, id="dup")
    doc.add(e)
    with pytest.raises(ZeError) as ex:
        _apply_patch(doc,
            Patch("t", "dup", ENTITY_META_FIELD, "create", None, e.to_dict()),
            actor=_actor(),
        )
    assert ex.value.code == "validation_failed"


def test_apply_patch_soft_delete():
    doc = _doc()
    e = Entity(type="t", data={}, id="e1")
    doc.add(e)
    _apply_patch(doc,
        Patch("t", "e1", ENTITY_META_FIELD, "soft_delete",
              before=e.to_dict(), after=None),
        actor=_actor(),
    )
    assert doc.get("t", "e1").deleted is True


def test_apply_patch_restore_after_soft_delete():
    doc = _doc()
    e = Entity(type="t", data={}, id="e1")
    doc.add(e)
    e.delete(expected_version=1, actor=_actor())
    assert e.deleted is True
    _apply_patch(doc,
        Patch("t", "e1", ENTITY_META_FIELD, "restore",
              before=e.to_dict(), after=None),
        actor=_actor(),
    )
    assert doc.get("t", "e1").deleted is False


def test_apply_patch_hard_remove():
    doc = _doc()
    doc.add(Entity(type="t", data={}, id="e1"))
    _apply_patch(doc,
        Patch("t", "e1", ENTITY_META_FIELD, "delete",
              before={"id": "e1"}, after=None),
        actor=_actor(),
    )
    assert doc.find("t", "e1") is None


def test_apply_patch_hard_remove_missing_is_idempotent():
    doc = _doc()
    # 不存在; hard_remove 静默
    _apply_patch(doc,
        Patch("t", "ghost", ENTITY_META_FIELD, "delete", None, None),
        actor=_actor(),
    )


def test_apply_patch_meta_op_soft_delete_missing_raises():
    doc = _doc()
    with pytest.raises(ZeError) as ex:
        _apply_patch(doc,
            Patch("t", "nope", ENTITY_META_FIELD, "soft_delete", None, None),
            actor=_actor(),
        )
    assert ex.value.code == "entity_not_found"


def test_apply_patch_meta_unknown_op_raises():
    doc = _doc()
    doc.add(Entity(type="t", data={}, id="e1"))
    with pytest.raises(ZeError) as ex:
        _apply_patch(doc,
            Patch("t", "e1", ENTITY_META_FIELD, "bogus_meta", None, None),
            actor=_actor(),
        )
    assert ex.value.code == "validation_failed"


# ==============================================================
# UndoController: 状态查询
# ==============================================================

def test_can_undo_empty_log_false():
    log = OpLog()
    doc = _doc()
    c = UndoController(log, doc)
    assert c.can_undo() is False
    assert c.can_redo() is False


def test_can_undo_after_op():
    log = OpLog()
    doc = _doc()
    doc.add(Entity(type="t", data={"x": 1}, id="e1"))
    log.append(_make_op(
        patches=[Patch("t", "e1", "x", "set", 1, 2)],
        reverse_patches=[Patch("t", "e1", "x", "set", 2, 1)],
    ))
    c = UndoController(log, doc)
    assert c.can_undo() is True


def test_can_undo_ignores_rollback():
    log = OpLog()
    doc = _doc()
    # 只压一条 kind=rollback 的; 应不算可撤
    log.append(_make_op(
        patches=[], reverse_patches=[], kind="rollback",
    ))
    c = UndoController(log, doc)
    assert c.can_undo() is False


def test_can_undo_ignores_stage():
    """尾部只剩 kind=stage 生命周期 op(空 patches)时 can_undo()=False —
    与 undo() 跳过集合一致,避免 ⌘Z 钮看着能点却撤不动。"""
    log = OpLog()
    doc = _doc()
    log.append(_make_op(command_id="pipeline.pause", patches=[], reverse_patches=[], kind="stage"))
    c = UndoController(log, doc)
    assert c.can_undo() is False


# ==============================================================
# UndoController: undo
# ==============================================================

def test_undo_returns_none_when_empty():
    log = OpLog()
    doc = _doc()
    c = UndoController(log, doc)
    assert c.undo(actor=_actor()) is None


def test_undo_reverts_single_op():
    log = OpLog()
    doc = _doc()
    doc.add(Entity(type="t", data={"x": 1}, id="e1"))
    # 模拟一次用户操作: x 从 1 → 42
    _apply_patch(doc, Patch("t", "e1", "x", "set", 1, 42), actor=_actor())
    log.append(_make_op(
        patches=[Patch("t", "e1", "x", "set", 1, 42)],
        reverse_patches=[Patch("t", "e1", "x", "set", 42, 1)],
    ))
    assert doc.get("t", "e1").data["x"] == 42

    c = UndoController(log, doc)
    result = c.undo(actor=_actor())
    assert result is not None
    assert len(result) == 1
    assert result[0].kind == "rollback"
    # 数据已恢复到 1
    assert doc.get("t", "e1").data["x"] == 1
    # OpLog 末尾多一条 rollback
    assert log.entries()[-1]["op"]["kind"] == "rollback"


def test_undo_skips_stage_ops():
    """kind=stage 生命周期 op(空 patches)被跳过 — ⌘Z 不"撤到"一个 pipeline.pause 空转,
    而是越过它撤下面那条真 mutation。"""
    log = OpLog()
    doc = _doc()
    doc.add(Entity(type="t", data={"x": 1}, id="e1"))
    # 真用户操作: x 1 → 42
    _apply_patch(doc, Patch("t", "e1", "x", "set", 1, 42), actor=_actor())
    log.append(_make_op(
        patches=[Patch("t", "e1", "x", "set", 1, 42)],
        reverse_patches=[Patch("t", "e1", "x", "set", 42, 1)],
    ))
    # 其后一条生命周期 op(如 pipeline.pause): 空 patches, kind=stage
    log.append(_make_op(command_id="pipeline.pause", patches=[], reverse_patches=[], kind="stage"))

    c = UndoController(log, doc)
    result = c.undo(actor=_actor())
    assert result is not None
    assert result[0].kind == "rollback"
    # 跳过 stage、撤的是真 mutation → x 回到 1(不是 no-op 空转)
    assert doc.get("t", "e1").data["x"] == 1


def test_undo_group_id_aggregates():
    """同 group_id 多 op 一次 undo 反所有."""
    log = OpLog()
    doc = _doc()
    doc.add(Entity(type="t", data={"x": 0, "y": 0}, id="e1"))
    # 3 个 op 同 group_id
    g = "drag-1"
    _apply_patch(doc, Patch("t", "e1", "x", "set", 0, 1), actor=_actor())
    log.append(_make_op(
        patches=[Patch("t", "e1", "x", "set", 0, 1)],
        reverse_patches=[Patch("t", "e1", "x", "set", 1, 0)],
        group_id=g,
    ))
    _apply_patch(doc, Patch("t", "e1", "x", "set", 1, 2), actor=_actor())
    log.append(_make_op(
        patches=[Patch("t", "e1", "x", "set", 1, 2)],
        reverse_patches=[Patch("t", "e1", "x", "set", 2, 1)],
        group_id=g,
    ))
    _apply_patch(doc, Patch("t", "e1", "x", "set", 2, 3), actor=_actor())
    log.append(_make_op(
        patches=[Patch("t", "e1", "x", "set", 2, 3)],
        reverse_patches=[Patch("t", "e1", "x", "set", 3, 2)],
        group_id=g,
    ))
    assert doc.get("t", "e1").data["x"] == 3

    c = UndoController(log, doc)
    c.undo(actor=_actor())
    # x 应当回到 0 (反 3 个 op)
    assert doc.get("t", "e1").data["x"] == 0


def test_undo_group_id_stops_at_different_group():
    """连续聚合: 中间换 group_id 即停."""
    log = OpLog()
    doc = _doc()
    doc.add(Entity(type="t", data={"x": 0}, id="e1"))
    # 不同 group_id: 不应聚合最早那个
    _apply_patch(doc, Patch("t", "e1", "x", "set", 0, 1), actor=_actor())
    log.append(_make_op(
        patches=[Patch("t", "e1", "x", "set", 0, 1)],
        reverse_patches=[Patch("t", "e1", "x", "set", 1, 0)],
        group_id="other",
    ))
    # 接两个 group_id="drag"
    _apply_patch(doc, Patch("t", "e1", "x", "set", 1, 2), actor=_actor())
    log.append(_make_op(
        patches=[Patch("t", "e1", "x", "set", 1, 2)],
        reverse_patches=[Patch("t", "e1", "x", "set", 2, 1)],
        group_id="drag",
    ))
    _apply_patch(doc, Patch("t", "e1", "x", "set", 2, 3), actor=_actor())
    log.append(_make_op(
        patches=[Patch("t", "e1", "x", "set", 2, 3)],
        reverse_patches=[Patch("t", "e1", "x", "set", 3, 2)],
        group_id="drag",
    ))

    c = UndoController(log, doc)
    c.undo(actor=_actor())
    # 应只反 drag 这两个; x 回到 1, 不到 0
    assert doc.get("t", "e1").data["x"] == 1


def test_undo_empty_group_id_does_not_aggregate():
    """group_id 空: 单 op undo."""
    log = OpLog()
    doc = _doc()
    doc.add(Entity(type="t", data={"x": 0}, id="e1"))
    _apply_patch(doc, Patch("t", "e1", "x", "set", 0, 1), actor=_actor())
    log.append(_make_op(
        patches=[Patch("t", "e1", "x", "set", 0, 1)],
        reverse_patches=[Patch("t", "e1", "x", "set", 1, 0)],
        group_id="",
    ))
    _apply_patch(doc, Patch("t", "e1", "x", "set", 1, 2), actor=_actor())
    log.append(_make_op(
        patches=[Patch("t", "e1", "x", "set", 1, 2)],
        reverse_patches=[Patch("t", "e1", "x", "set", 2, 1)],
        group_id="",
    ))

    c = UndoController(log, doc)
    c.undo(actor=_actor())
    # 只反最末; x 回到 1
    assert doc.get("t", "e1").data["x"] == 1


def test_undo_skips_existing_rollback_entries():
    """OpLog 已有 kind=rollback 的不应被当作可撤的 target."""
    log = OpLog()
    doc = _doc()
    doc.add(Entity(type="t", data={"x": 0}, id="e1"))
    _apply_patch(doc, Patch("t", "e1", "x", "set", 0, 7), actor=_actor())
    # 用户 op
    log.append(_make_op(
        patches=[Patch("t", "e1", "x", "set", 0, 7)],
        reverse_patches=[Patch("t", "e1", "x", "set", 7, 0)],
    ))
    # 模拟之前已有的 rollback (例如其他 actor 撤过)
    log.append(_make_op(
        patches=[], reverse_patches=[], kind="rollback",
    ))

    c = UndoController(log, doc)
    result = c.undo(actor=_actor())
    # 应跳过末尾那条 rollback, 找到用户 op, 撤之
    assert result is not None
    assert doc.get("t", "e1").data["x"] == 0


def _append_edit(log, doc, before, after, group_id=""):
    """辅助: 施加一次 x set 编辑并记 native op(供多级 undo 测试建栈)。"""
    _apply_patch(doc, Patch("t", "e1", "x", "set", before, after), actor=_actor())
    log.append(_make_op(
        patches=[Patch("t", "e1", "x", "set", before, after)],
        reverse_patches=[Patch("t", "e1", "x", "set", after, before)],
        group_id=group_id,
    ))


def test_undo_multi_level_reverts_step_by_step():
    """连按 undo 应逐步退栈到底(旧 bug: 永远停在最近一步, 更早的 op 不可达)。"""
    log = OpLog()
    doc = _doc()
    doc.add(Entity(type="t", data={"x": 0}, id="e1"))
    _append_edit(log, doc, 0, 1)
    _append_edit(log, doc, 1, 2)
    _append_edit(log, doc, 2, 3)
    assert doc.get("t", "e1").data["x"] == 3

    c = UndoController(log, doc)
    c.undo(actor=_actor())
    assert doc.get("t", "e1").data["x"] == 2   # 退一步
    c.undo(actor=_actor())
    assert doc.get("t", "e1").data["x"] == 1   # 退两步(旧 bug 会卡在 2)
    c.undo(actor=_actor())
    assert doc.get("t", "e1").data["x"] == 0   # 退三步到底
    assert c.can_undo() is False               # 撤到底, ⌘Z 钮应灭(旧 bug 恒 True)
    assert c.undo(actor=_actor()) is None       # 已无 group 可撤


def test_undo_redo_interleave_multi_level():
    """undo/redo 交替: 全撤 → 重做一步 → 撤掉刚重做的 → 再全部重做。"""
    log = OpLog()
    doc = _doc()
    doc.add(Entity(type="t", data={"x": 0}, id="e1"))
    _append_edit(log, doc, 0, 1)
    _append_edit(log, doc, 1, 2)

    c = UndoController(log, doc)
    c.undo(actor=_actor()); c.undo(actor=_actor())
    assert doc.get("t", "e1").data["x"] == 0   # 全撤
    assert c.can_redo() is True
    c.redo(actor=_actor())
    assert doc.get("t", "e1").data["x"] == 1   # 重做第一步
    c.undo(actor=_actor())                      # redo 后再 undo 撤掉刚重做的
    assert doc.get("t", "e1").data["x"] == 0
    c.redo(actor=_actor()); c.redo(actor=_actor())
    assert doc.get("t", "e1").data["x"] == 2   # 全部重做


def test_undo_multi_then_new_edit_discards_redo():
    """多级 undo 后来一次新编辑: redo 全部作废, 新编辑成为可撤的新栈顶。"""
    log = OpLog()
    doc = _doc()
    doc.add(Entity(type="t", data={"x": 0}, id="e1"))
    _append_edit(log, doc, 0, 1)
    _append_edit(log, doc, 1, 2)

    c = UndoController(log, doc)
    c.undo(actor=_actor()); c.undo(actor=_actor())   # x→0, redo 栈深 2
    assert doc.get("t", "e1").data["x"] == 0
    _append_edit(log, doc, 0, 9)                      # 新编辑分叉
    assert c.can_redo() is False                       # 新编辑作废 redo
    c.undo(actor=_actor())                             # 撤的是新编辑, 不是某个旧状态
    assert doc.get("t", "e1").data["x"] == 0


def test_undo_multi_level_survives_process_restart(tmp_path):
    """跨进程重启: OpLog 落盘、redo_stack 内存态按 SPEC §8 清零, undo 仍从 OpLog 推导正确撤销深度。

    旧 bug(code-review CRITICAL): 重启后新 controller redo_depth=len(redo_stack)=0 → undo() 重新
    target 已撤的最末 group → 对文档 blind set 一个已被撤销的旧值(损坏), 既非再撤一级也非 no-op。"""
    oplog_path = tmp_path / "oplog.ndjson"
    doc_path = tmp_path / "document.json"

    # 进程1: 3 独立编辑 x 0→1→2→3 + undo 2 次(落盘状态 x=1, OpLog 含 R1/R2)
    log = OpLog(oplog_path)
    doc = _doc()
    doc.add(Entity(type="t", data={"x": 0}, id="e1"))
    for b, a in [(0, 1), (1, 2), (2, 3)]:
        _apply_patch(doc, Patch("t", "e1", "x", "set", b, a), actor=_actor())
        log.append(_make_op(
            patches=[Patch("t", "e1", "x", "set", b, a)],
            reverse_patches=[Patch("t", "e1", "x", "set", a, b)],
        ))
    c1 = UndoController(log, doc)
    c1.undo(actor=_actor()); c1.undo(actor=_actor())
    assert doc.get("t", "e1").data["x"] == 1
    doc.save(doc_path)

    # 进程2"重启": OpLog 从盘载回(含 R1/R2) + doc 载回(x=1) + 新 UndoController(redo_stack 空)
    log2 = OpLog(oplog_path)
    doc2 = Document.open(doc_path)
    c2 = UndoController(log2, doc2)
    assert c2.can_undo() is True                      # 还有 op1 一个 group 可撤
    c2.undo(actor=_actor())
    assert doc2.get("t", "e1").data["x"] == 0         # 撤到底 x=0(旧 bug 会 blind set 到 x=2)
    assert c2.can_undo() is False                     # 三个 group 全撤完


def test_undo_ignores_independent_rollback_in_oplog_tail():
    """undo 与 rollback_operation_group 共用同一 op_log:独立来源的 rollback(撤无关 entity)落到
    OpLog 尾部后, 再 undo 仍撤下一个 undo 栈层, 不被非 undo/redo 的 rollback 当作栈尾
    (code-review HIGH: 旧实现遇它 break → depth 归 0 → undo 重放已撤 group)。"""
    log = OpLog()
    doc = _doc()
    doc.add(Entity(type="t", data={"x": 0}, id="e1"))
    doc.add(Entity(type="t", data={"y": 0}, id="e2"))
    # e2 先有一次 session s1 的编辑(与 e1 的 undo 栈无关)
    _apply_patch(doc, Patch("t", "e2", "y", "set", 0, 5), actor=_actor())
    log.append(_make_op(command_id="ai.publish", session_id="s1",
                        patches=[Patch("t", "e2", "y", "set", 0, 5)],
                        reverse_patches=[Patch("t", "e2", "y", "set", 5, 0)]))
    # e1 两次独立编辑 x 0→1→2
    _append_edit(log, doc, 0, 1)
    _append_edit(log, doc, 1, 2)

    c = UndoController(log, doc)
    c.undo(actor=_actor())                            # 撤 e1.x 1→2, x=1
    assert doc.get("t", "e1").data["x"] == 1
    # 独立来源 rollback: 撤 e2 的 session s1(append kind=rollback, command_id=rollback_operation_group)
    rollback_operation_group(log, doc, actor=_actor(), session_id="s1")
    assert doc.get("t", "e2").data["y"] == 0
    # 再 undo: 应撤 e1.x 0→1(x→0), 不被 e2 的独立 rollback 当作 undo 栈尾
    c.undo(actor=_actor())
    assert doc.get("t", "e1").data["x"] == 0          # 旧 break 版会重放 e1 g2(x 仍 1)、g1 没撤


# ==============================================================
# UndoController: redo
# ==============================================================

def test_redo_returns_none_when_empty():
    log = OpLog()
    doc = _doc()
    c = UndoController(log, doc)
    assert c.redo(actor=_actor()) is None


def test_undo_redo_round_trip():
    """undo 后 redo 应恢复原数据."""
    log = OpLog()
    doc = _doc()
    doc.add(Entity(type="t", data={"x": 0}, id="e1"))
    _apply_patch(doc, Patch("t", "e1", "x", "set", 0, 42), actor=_actor())
    log.append(_make_op(
        patches=[Patch("t", "e1", "x", "set", 0, 42)],
        reverse_patches=[Patch("t", "e1", "x", "set", 42, 0)],
    ))
    assert doc.get("t", "e1").data["x"] == 42

    c = UndoController(log, doc)
    c.undo(actor=_actor())
    assert doc.get("t", "e1").data["x"] == 0
    assert c.can_redo() is True

    c.redo(actor=_actor())
    # redo 恢复原值 42
    assert doc.get("t", "e1").data["x"] == 42


def test_redo_blocked_after_new_native_op_no_data_loss():
    """undo 后做新正向 op → redo 被作废、不静默覆盖新编辑(簇 C-2 数据丢失)。"""
    log = OpLog()
    doc = _doc()
    doc.add(Entity(type="t", data={"x": 1}, id="e1"))
    # native A: x 1→2
    _apply_patch(doc, Patch("t", "e1", "x", "set", 1, 2), actor=_actor())
    log.append(_make_op(command_id="entity.update",
                        patches=[Patch("t", "e1", "x", "set", 1, 2)],
                        reverse_patches=[Patch("t", "e1", "x", "set", 2, 1)]))
    c = UndoController(log, doc)
    c.undo(actor=_actor())                              # x → 1
    assert c.can_redo() is True
    # 新正向 B 经 dispatch 直接进共享 op_log(不经 controller):x 1→5
    _apply_patch(doc, Patch("t", "e1", "x", "set", 1, 5), actor=_actor())
    log.append(_make_op(command_id="entity.update",
                        patches=[Patch("t", "e1", "x", "set", 1, 5)],
                        reverse_patches=[Patch("t", "e1", "x", "set", 5, 1)]))
    # 新编辑分叉历史 → redo 作废:can_redo False、redo None、x 仍 5(不被 A 的 forward 复活成 2)
    assert c.can_redo() is False
    assert c.redo(actor=_actor()) is None
    assert doc.get("t", "e1").data["x"] == 5


def test_can_redo_is_pure_does_not_clear_stack():
    """can_redo() 是纯查询:即便判 redo 已失效也不清栈。GET /api/history 反复 poll 不能毁状态(C-2 CRITICAL)。"""
    log = OpLog()
    doc = _doc()
    doc.add(Entity(type="t", data={"x": 1}, id="e1"))
    _apply_patch(doc, Patch("t", "e1", "x", "set", 1, 2), actor=_actor())
    log.append(_make_op(command_id="entity.update",
                        patches=[Patch("t", "e1", "x", "set", 1, 2)],
                        reverse_patches=[Patch("t", "e1", "x", "set", 2, 1)]))
    c = UndoController(log, doc)
    c.undo(actor=_actor())
    _apply_patch(doc, Patch("t", "e1", "x", "set", 1, 5), actor=_actor())   # 新 native 编辑使 redo 失效
    log.append(_make_op(command_id="entity.update",
                        patches=[Patch("t", "e1", "x", "set", 1, 5)],
                        reverse_patches=[Patch("t", "e1", "x", "set", 5, 1)]))
    stack_before = list(c._redo_stack)
    assert c.can_redo() is False           # 正确判失效
    assert c.can_redo() is False           # 反复 poll 稳定
    assert c._redo_stack == stack_before   # 纯查询:没清栈(失效清理留给 undo()/redo() 这些状态变更)


def test_undo_redo_aggregated_group():
    """聚合 undo 之后 redo 也应聚合 (用同一个 rollback_op 的 reverse 重做)."""
    log = OpLog()
    doc = _doc()
    doc.add(Entity(type="t", data={"x": 0}, id="e1"))
    g = "drag-1"
    for v_from, v_to in [(0, 1), (1, 2), (2, 3)]:
        _apply_patch(doc,
            Patch("t", "e1", "x", "set", v_from, v_to), actor=_actor(),
        )
        log.append(_make_op(
            patches=[Patch("t", "e1", "x", "set", v_from, v_to)],
            reverse_patches=[Patch("t", "e1", "x", "set", v_to, v_from)],
            group_id=g,
        ))

    c = UndoController(log, doc)
    c.undo(actor=_actor())
    assert doc.get("t", "e1").data["x"] == 0
    c.redo(actor=_actor())
    assert doc.get("t", "e1").data["x"] == 3


# ==============================================================
# UndoController: CommitCoordinator deferred record
# ==============================================================

def test_deferred_history_record_keeps_legacy_dataclass_surface():
    operation = _make_op(patches=[], reverse_patches=[])
    expected_fields = (
        "token",
        "kind",
        "operations",
        "_controller_id",
        "_base_log_length",
        "_redo_stack_snapshot",
        "_expected_redo_id",
        "_invalidate_stale_redo",
    )

    assert tuple(item.name for item in fields(DeferredHistoryRecord)) == expected_fields
    assert tuple(inspect.signature(DeferredHistoryRecord).parameters) == expected_fields

    # The pre-hardening positional constructor remains valid; the canonical
    # body seal belongs to UndoController and does not widen this value type.
    record = DeferredHistoryRecord(
        "token",
        "undo",
        (operation,),
        "controller",
        0,
        (),
        "",
        False,
    )
    changed = replace(record, token="replacement")
    restored = pickle.loads(pickle.dumps(record))

    assert changed.token == "replacement"
    assert changed.operations == record.operations
    assert restored == record


def test_prepare_undo_only_applies_and_produces_operation_until_confirmed():
    """deferred undo 不偷写存储、不提前制造可 redo 历史；未落日志时确认会被拒绝。"""
    log = OpLog()
    doc = _doc()
    doc.add(Entity(type="t", data={"x": 0}, id="e1"))
    _append_edit(log, doc, 0, 1)
    c = UndoController(log, doc)
    before_log = log.entries()

    record = c.prepare_undo(actor=_actor())

    assert record is not None
    assert record.kind == "undo"
    assert len(record.operations) == 1
    assert record.operations[0].command_id == "undo"
    assert doc.get("t", "e1").data["x"] == 0       # producer 只 apply
    assert log.entries() == before_log                 # 没 append
    assert c._redo_stack == []                         # 没 push
    with pytest.raises(ZeError, match="committed to OpLog"):
        c.confirm_deferred(record)                     # 未提交不能假确认
    assert c._redo_stack == []

    # 模拟 CommitCoordinator 的 durable commit；确认只推进控制器游标，不重复 append。
    log.append(record.operations[0])
    c.confirm_deferred(record)
    assert c._redo_stack == [record.operations[0].id]
    assert c.can_redo() is True
    assert len(log) == len(before_log) + 1
    with pytest.raises(ZeError, match="already confirmed or discarded"):
        c.confirm_deferred(record)                     # 重复确认明确拒绝


def _replace_deferred_operation(operation, field):
    changes = {
        "reverse_patches": {"reverse_patches": ()},
        "actor": {
            "actor": {
                "user_id": "forged",
                "device_id": "default",
                "source": "user",
            },
        },
        "source": {"source": "ai"},
        "kind": {"kind": "native"},
        "command_id": {"command_id": "Forged.History"},
        "group_id": {"group_id": "forged-group"},
        "trace_id": {"trace_id": "forged-trace"},
    }[field]
    return replace(operation, **changes)


@pytest.mark.parametrize("phase", ("undo", "redo"))
@pytest.mark.parametrize(
    "field",
    (
        "reverse_patches",
        "actor",
        "source",
        "kind",
        "command_id",
        "group_id",
        "trace_id",
    ),
)
def test_confirm_deferred_rejects_same_id_with_changed_canonical_body(
    phase, field,
):
    log = OpLog()
    doc = _doc()
    doc.add(Entity(type="t", data={"x": 0}, id="e1"))
    _append_edit(log, doc, 0, 1)
    controller = UndoController(log, doc)

    if phase == "redo":
        undo_record = controller.prepare_undo(actor=_actor())
        assert undo_record is not None
        log.append(undo_record.operations[0])
        controller.confirm_deferred(undo_record)
        assert controller.can_redo() is True

    prepare = (
        controller.prepare_undo
        if phase == "undo"
        else controller.prepare_redo
    )
    record = prepare(actor=_actor())
    assert record is not None
    before_redo = tuple(controller._redo_stack)
    tampered = _replace_deferred_operation(record.operations[0], field)
    assert tampered.id == record.operations[0].id
    log.append(tampered)
    entries_after_injection = log.entries()

    with pytest.raises(ZeError, match="exact deferred history Operations"):
        controller.confirm_deferred(record)

    assert tuple(controller._redo_stack) == before_redo
    assert record.token in controller._pending_deferred
    assert record.token in controller._pending_operation_bodies
    assert log.entries() == entries_after_injection
    controller.discard_deferred(record)
    assert controller._pending_deferred == {}
    assert controller._pending_operation_bodies == {}


def test_discard_unconfirmed_undo_does_not_pollute_history_state():
    """提交失败路径只释放 deferred 凭据；OpLog/redo 栈保持 prepare 前状态。

    Document mutation 的恢复属于 W04 通用 mutation rollback，本切片刻意不假装处理。
    """
    log = OpLog()
    doc = _doc()
    doc.add(Entity(type="t", data={"x": 0}, id="e1"))
    _append_edit(log, doc, 0, 1)
    c = UndoController(log, doc)
    before_log = log.entries()
    before_stack = list(c._redo_stack)

    record = c.prepare_undo(actor=_actor())
    assert record is not None
    c.discard_deferred(record)

    assert log.entries() == before_log
    assert c._redo_stack == before_stack
    with pytest.raises(ZeError, match="already confirmed or discarded"):
        c.confirm_deferred(record)


def test_prepare_rejects_second_history_mutation_while_commit_is_pending():
    """同一锁内尚有未决提交时不能再次按旧日志重复 apply 同一 group。"""
    log = OpLog()
    doc = _doc()
    doc.add(Entity(type="t", data={"x": 0}, id="e1"))
    _append_edit(log, doc, 0, 1)
    c = UndoController(log, doc)

    record = c.prepare_undo(actor=_actor())
    assert record is not None
    with pytest.raises(ZeError, match="awaiting commit confirmation"):
        c.prepare_undo(actor=_actor())
    c.discard_deferred(record)


def test_prepare_redo_peeks_stack_and_consumes_it_only_after_confirm():
    """redo producer 不提前 pop；外部提交成功并确认后才消费对应 undo。"""
    log = OpLog()
    doc = _doc()
    doc.add(Entity(type="t", data={"x": 0}, id="e1"))
    _append_edit(log, doc, 0, 7)
    c = UndoController(log, doc)
    c.undo(actor=_actor())
    assert doc.get("t", "e1").data["x"] == 0
    stack_before = list(c._redo_stack)
    log_length_before = len(log)

    record = c.prepare_redo(actor=_actor())

    assert record is not None
    assert record.kind == "redo"
    assert record.operations[0].command_id == "redo"
    assert doc.get("t", "e1").data["x"] == 7
    assert c._redo_stack == stack_before               # prepare 只 peek
    assert len(log) == log_length_before               # prepare 不 append

    log.append(record.operations[0])
    c.confirm_deferred(record)
    assert c._redo_stack == []                         # durable 后才 pop
    assert c.can_redo() is False


def test_deferred_undo_clears_stale_redo_only_after_confirm():
    """新 native 分叉后的旧 redo，在 deferred undo 提交前仍不被 prepare 偷偷清理。"""
    log = OpLog()
    doc = _doc()
    doc.add(Entity(type="t", data={"x": 0}, id="e1"))
    _append_edit(log, doc, 0, 1)
    c = UndoController(log, doc)
    c.undo(actor=_actor())
    stale_redo_id = c._redo_stack[-1]
    _append_edit(log, doc, 0, 9)                       # 分叉，旧 redo 已失效但查询不清栈
    assert c.can_redo() is False

    record = c.prepare_undo(actor=_actor())            # 撤分叉出来的新编辑
    assert record is not None
    assert c._redo_stack == [stale_redo_id]            # prepare 未污染历史状态
    log.append(record.operations[0])
    c.confirm_deferred(record)
    assert c._redo_stack == [record.operations[0].id]  # confirm 才清旧栈并 push 新 undo


# ==============================================================
# rollback_field 工厂
# ==============================================================

def test_rollback_field_factory():
    e = Entity(type="t", data={"score": 100}, id="e1")
    actor = _actor()
    op = rollback_field(e, "score", target_value=50, actor=actor)
    assert op.kind == "rollback"
    assert op.command_id == "rollback_field"
    assert len(op.patches) == 1
    p = op.patches[0]
    assert p.field == "score"
    assert p.op == "set"
    assert p.before == 100
    assert p.after == 50
    # reverse: redo 时设回 100
    rp = op.reverse_patches[0]
    assert rp.after == 100
    assert rp.before == 50


def test_rollback_field_factory_group_id():
    e = Entity(type="t", data={"x": 1}, id="e1")
    op = rollback_field(e, "x", 0, actor=_actor(), group_id="g1")
    assert op.group_id == "g1"


# ==============================================================
# rollback_entity_to_op 工厂
# ==============================================================

def test_rollback_entity_to_op():
    """造 rollback op: 累积 target_op 之后所有影响该 entity 的反向 patches."""
    log = OpLog()
    e = Entity(type="t", data={"x": 0, "y": 0}, id="e1")
    target = _make_op(
        patches=[Patch("t", "e1", "x", "set", 0, 1)],
        reverse_patches=[Patch("t", "e1", "x", "set", 1, 0)],
    )
    log.append(target)
    # 之后两次操作影响 e1
    log.append(_make_op(
        patches=[Patch("t", "e1", "x", "set", 1, 2)],
        reverse_patches=[Patch("t", "e1", "x", "set", 2, 1)],
    ))
    log.append(_make_op(
        patches=[Patch("t", "e1", "y", "set", 0, 99)],
        reverse_patches=[Patch("t", "e1", "y", "set", 99, 0)],
    ))
    # 另一个 entity 的 op (不应混入)
    log.append(_make_op(
        patches=[Patch("t", "other", "z", "set", 0, 1)],
        reverse_patches=[Patch("t", "other", "z", "set", 1, 0)],
    ))

    op = rollback_entity_to_op(e, log, target.id, actor=_actor())
    assert op.kind == "rollback"
    # 应含 2 个反向 patch (y 撤回 + x 从 2 撤到 1), 不含 other
    assert len(op.patches) == 2
    for p in op.patches:
        assert p.entity_type == "t"
        assert p.entity_id == "e1"


def test_rollback_entity_to_op_missing_target_raises():
    log = OpLog()
    e = Entity(type="t", data={}, id="e1")
    with pytest.raises(ZeError) as ex:
        rollback_entity_to_op(e, log, "nonexistent_id", actor=_actor())
    assert ex.value.code == "validation_failed"


# ==============================================================
# CheckpointManager
# ==============================================================

def test_checkpoint_meta_construct():
    meta = CheckpointMeta(
        name="v1", created_at=1.0, actor={"user_id": "u1"},
        op_log_position=5, summary={"entity_count": 10},
    )
    assert meta.name == "v1"
    assert meta.summary == {"entity_count": 10}


def test_checkpoint_manager_create_list_restore(tmp_path):
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="live_collab",
        save_policy="auto_commit",
    )
    doc.add(Entity(type="item", id="e1", name="One", data={"title": "v1"}))
    mgr = CheckpointManager(tmp_path)

    meta = mgr.create(doc, "session-open", actor=_actor(), op_log_position=3,
                      reason="session_open", session_id="s1")
    assert meta.name == "session-open"
    assert meta.document_id == doc.id
    assert meta.reason == "session_open"
    assert meta.session_id == "s1"
    assert meta.summary["entity_count"] == 1
    assert [m.name for m in mgr.list()] == ["session-open"]

    # mutate current doc after snapshot; restore must return the old state
    doc.get("item", "e1").update({"title": "v2"}, expected_version=1, actor=_actor())
    restored = mgr.restore("session-open")
    assert restored.get("item", "e1").get("title") == "v1"
    assert restored.id == doc.id
    assert restored.collaboration_mode == "live_collab"
    assert restored.save_policy == "auto_commit"
    assert restored.should_auto_commit is True


def test_checkpoint_restore_keeps_legacy_snapshot_without_save_policy(tmp_path):
    """W02 前生成的 checkpoint 缺字段时仍可恢复，且不做隐式迁移。"""
    doc = _doc()
    mgr = CheckpointManager(tmp_path)
    mgr.create(doc, "legacy", actor=_actor())

    snapshot_path = tmp_path / "checkpoints" / "legacy.json"
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    payload["snapshot"].pop("save_policy", None)
    snapshot_path.write_text(json.dumps(payload), encoding="utf-8")

    restored = mgr.restore("legacy")
    assert restored.save_policy is None
    assert restored.effective_save_policy == "auto_commit"


def test_checkpoint_manager_fork(tmp_path):
    doc = _doc()
    doc.add(Entity(type="item", id="e1", data={"x": 1}))
    mgr = CheckpointManager(tmp_path)
    mgr.create(doc, "v1", actor=_actor())
    forked = mgr.fork("v1", "v2")
    assert forked.name == "v2"
    assert forked.reason == "fork:v1"
    assert {m.name for m in mgr.list()} == {"v1", "v2"}


def test_checkpoint_manager_requires_doc_dir():
    mgr = CheckpointManager()
    with pytest.raises(ZeError) as exc:
        mgr.list()
    assert exc.value.code == "validation_failed"


# ==============================================================
# realtime_checkpoint group/session rollback
# ==============================================================

def test_rollback_operation_group_applies_selected_session_only():
    doc = _doc()
    doc.add(Entity(type="item", id="e1", data={"title": "v1"}))
    log = OpLog()

    # Simulate command handler already applied the user edit.
    doc.get("item", "e1").update({"title": "v2"}, expected_version=1, actor=_actor())
    log.append(_make_op(
        command_id="item.update",
        patches=[Patch("item", "e1", "title", "set", "v1", "v2")],
        reverse_patches=[Patch("item", "e1", "title", "set", "v2", "v1")],
        group_id="g1",
        session_id="s1",
        document_id=doc.id,
    ))

    result = rollback_operation_group(log, doc, actor=_actor("u2"), session_id="s1")
    assert result.applied is True
    assert result.conflicts == []
    assert doc.get("item", "e1").get("title") == "v1"
    assert log.entries()[-1]["op"]["command_id"] == "rollback_operation_group"


def test_rollback_operation_group_conflicts_on_later_same_field_change():
    doc = _doc()
    doc.add(Entity(type="item", id="e1", data={"title": "v1"}))
    log = OpLog()

    doc.get("item", "e1").update({"title": "v2"}, expected_version=1, actor=_actor("u1"))
    op1 = _make_op(
        command_id="item.update",
        patches=[Patch("item", "e1", "title", "set", "v1", "v2")],
        reverse_patches=[Patch("item", "e1", "title", "set", "v2", "v1")],
        group_id="g1",
        session_id="s1",
    )
    log.append(op1)

    doc.get("item", "e1").update({"title": "v3"}, expected_version=2, actor=_actor("u2"))
    log.append(_make_op(
        command_id="item.update",
        patches=[Patch("item", "e1", "title", "set", "v2", "v3")],
        reverse_patches=[Patch("item", "e1", "title", "set", "v3", "v2")],
        actor=_actor("u2"),
    ))

    result = rollback_operation_group(log, doc, actor=_actor("u1"), session_id="s1")
    assert result.applied is False
    assert len(result.conflicts) == 1
    assert doc.get("item", "e1").get("title") == "v3"


def test_rollback_operation_group_rejects_exact_bound_oplog_before_reading():
    """A bootstrap history lease closes the legacy rollback bypass by itself.

    Mutation policies are intentionally absent: every bootstrapped project is
    protected by its canonical OpLog authority, so policy presence cannot be
    the guard.  Rejection must precede even ``entries()`` and must not freeze
    the otherwise healthy authority.
    """

    class _Owner:
        pass

    class _Store:
        def __init__(self, op_log):
            self.op_log = op_log

    class _Coordinator:
        def __init__(self, document, store):
            self.document = document
            self.store = store

    doc = _doc()
    doc.add(Entity(type="item", id="e1", data={"x": 1}))
    assert not bool(getattr(doc, "_mutation_policies", {}))
    log = OpLog()
    doc.get("item", "e1").update(
        {"x": 2}, expected_version=1, actor=_actor(),
    )
    log.append(_make_op(
        command_id="item.update",
        patches=[Patch("item", "e1", "x", "set", 1, 2)],
        reverse_patches=[Patch("item", "e1", "x", "set", 2, 1)],
        group_id="g1",
        session_id="s1",
        document_id=doc.id,
    ))
    controller = UndoController(log, doc)
    store = _Store(log)
    coordinator = _Coordinator(doc, store)
    owner = _Owner()
    capability = authority_protocol._issue_history_bind_capability(
        bootstrap_owner=owner,
    )
    authority = authority_protocol.bind_history_authority(
        capability,
        bootstrap_owner=owner,
        document=doc,
        op_log=log,
        undo_controller=controller,
        coordinator=coordinator,
        store=store,
        project_id=doc.id,
        generation=1,
        runtime_revision="test-bound-group-rollback",
        replay_verifier=lambda *_args: True,
    )
    original_entries = log.entries
    log_before = original_entries()

    def _forbidden_entries():
        pytest.fail("bound rollback helper read canonical OpLog before rejecting")

    log.entries = _forbidden_entries
    try:
        with pytest.raises(ZeError) as caught:
            rollback_operation_group(
                log, doc, actor=_actor("u2"), session_id="s1",
            )
    finally:
        log.entries = original_entries

    assert caught.value.code == "history_replay_coordinator_required"
    assert caught.value.context == {"reason": "history_transaction_required"}
    assert doc.get("item", "e1").data["x"] == 2
    assert log.entries() == log_before
    assert authority.state == "ACTIVE"
    authority_protocol.retire_history_authority(
        authority,
        _capability=authority_protocol._HISTORY_RETIRE_CAPABILITY,
    )


# ==============================================================
# 跨重启 undo/redo:undo 从落盘 OpLog 末尾继续可用;redo_stack 内存态不跨重启(SPEC §8)
# ==============================================================

def _setup_doc_with_item():
    doc = _doc()
    doc.add(Entity(type="item", id="e1", name="x", data={"title": "v1"}))
    return doc


def test_redo_does_not_survive_restart(tmp_path):
    """SPEC §8:redo_stack 是内存 UI 态,不持久化。重启后 redo 无意义 —— 新 controller 的
    redo_stack 为空、can_redo() False(撤销仍可从 OpLog 末尾继续,redo 不跨窗口/会话)。"""
    p = tmp_path / "oplog.ndjson"
    # 第一进程:改 entity + 落 oplog + undo(本进程内 redo 可用)
    doc = _setup_doc_with_item()
    log = OpLog(p)
    doc.get("item", "e1").update({"title": "v2"}, expected_version=doc.get("item", "e1").version, actor=_actor())
    log.append(_make_op(command_id="entity.update",
                        patches=[Patch("item", "e1", "title", "set", "v1", "v2")],
                        reverse_patches=[Patch("item", "e1", "title", "set", "v2", "v1")]))
    ctl = UndoController(log, doc)
    ctl.undo(actor=_actor())
    assert doc.get("item", "e1").get("title") == "v1"
    assert ctl.can_redo()                          # 同进程内 redo 可用

    # 第二进程(重启):新 OpLog 从盘载回 + 新 controller —— redo 不跨重启
    log2 = OpLog(p)
    assert log2.verify_chain()
    doc2 = _setup_doc_with_item()                  # title v1(undo 后状态)
    ctl2 = UndoController(log2, doc2)
    assert ctl2._redo_stack == [], "redo_stack 不应跨重启重建"
    assert not ctl2.can_redo()


def test_undo_still_works_after_restart_from_oplog(tmp_path):
    """撤销跨重启仍可用:OpLog 持久化,重启后新 controller 仍能从落盘日志末尾撤销。"""
    p = tmp_path / "oplog.ndjson"
    # 第一进程:改 entity + 落 oplog(不在本进程 undo)
    doc = _setup_doc_with_item()
    log = OpLog(p)
    doc.get("item", "e1").update({"title": "v2"}, expected_version=doc.get("item", "e1").version, actor=_actor())
    log.append(_make_op(command_id="entity.update",
                        patches=[Patch("item", "e1", "title", "set", "v1", "v2")],
                        reverse_patches=[Patch("item", "e1", "title", "set", "v2", "v1")]))

    # 第二进程(重启):doc 从持久态(v2)载回 + 新 controller → 仍能撤销到 v1
    log2 = OpLog(p)
    doc2 = _setup_doc_with_item()
    doc2.get("item", "e1").update({"title": "v2"}, expected_version=doc2.get("item", "e1").version, actor=_actor())
    ctl2 = UndoController(log2, doc2)
    assert ctl2.can_undo()
    ctl2.undo(actor=_actor())
    assert doc2.get("item", "e1").get("title") == "v1", "撤销应从落盘 OpLog 末尾生效"


def test_redo_stack_empty_when_no_undo(tmp_path):
    """没 undo 过 → 新 controller 的 redo_stack 为空。"""
    p = tmp_path / "oplog.ndjson"
    log = OpLog(p)
    log.append(_make_op(command_id="entity.update",
                        patches=[Patch("item", "e1", "title", "set", "a", "b")],
                        reverse_patches=[Patch("item", "e1", "title", "set", "b", "a")]))
    ctl = UndoController(OpLog(p), _setup_doc_with_item())
    assert not ctl.can_redo()
    assert ctl._redo_stack == []


def test_redo_stack_starts_empty_each_process(tmp_path):
    """每个进程的 redo_stack 从空起步(不从 OpLog 重建),即使上个进程 undo 过。"""
    p = tmp_path / "oplog.ndjson"
    doc = _setup_doc_with_item()
    log = OpLog(p)
    log.append(_make_op(command_id="entity.update",
                        patches=[Patch("item", "e1", "title", "set", "v1", "v2")],
                        reverse_patches=[Patch("item", "e1", "title", "set", "v2", "v1")]))
    doc.get("item", "e1").update({"title": "v2"}, expected_version=doc.get("item", "e1").version, actor=_actor())
    ctl = UndoController(log, doc)
    ctl.undo(actor=_actor())
    assert ctl._redo_stack                          # 本进程 undo 后非空
    # 新进程(重启):redo_stack 空
    ctl2 = UndoController(OpLog(p), doc)
    assert ctl2._redo_stack == []


# ==============================================================
# A3: undo 循环原子性 + A1③: undo persist 顺序
# ==============================================================

def test_undo_atomic_rollback_when_reverse_patch_unreachable():
    """A3 回归:undo 一组的反向 patch 中途命中已被后续硬删的 entity → 整组零变更、不落 rollback_op。

    旧逐条 apply 会先把 e1 反向、再在 e2 处抛错,留下半撤销的 e1 内存态 + 无 rollback → doc 与 oplog 分叉。"""
    doc = _doc()
    doc.add(Entity(type="t", data={"x": 1}, id="e1"))
    doc.add(Entity(type="t", data={"x": 1}, id="e2"))
    doc.get("t", "e1").update({"x": 2}, expected_version=1, actor=_actor())
    doc.get("t", "e2").update({"x": 2}, expected_version=1, actor=_actor())
    oplog = OpLog()
    oplog.append(_make_op(
        patches=[Patch("t", "e1", "x", "set", 1, 2), Patch("t", "e2", "x", "set", 1, 2)],
        reverse_patches=[Patch("t", "e1", "x", "set", 2, 1), Patch("t", "e2", "x", "set", 2, 1)],
    ))
    ctl = UndoController(oplog, doc)

    doc.hard_remove("t", "e2")                        # 后续硬删 → undo 反向命中不存在的 e2

    with pytest.raises(ZeError) as ei:
        ctl.undo(actor=_actor())
    assert ei.value.code == "entity_not_found"
    assert doc.get("t", "e1").data["x"] == 2          # e1 未被半撤销(原子回滚)
    assert len(oplog) == 1                            # 没落 rollback_op(仍只有 op1)


def test_undo_persists_before_appending():
    """A1③:undo 传 persist 时,先 persist() 落盘、再 append rollback_op(approach B,保证 doc ⊇ oplog)。"""
    doc = _doc()
    doc.add(Entity(type="t", data={"x": 1}, id="e1"))
    doc.get("t", "e1").update({"x": 2}, expected_version=1, actor=_actor())
    oplog = OpLog()
    oplog.append(_make_op(
        patches=[Patch("t", "e1", "x", "set", 1, 2)],
        reverse_patches=[Patch("t", "e1", "x", "set", 2, 1)],
    ))
    ctl = UndoController(oplog, doc)
    order: list[str] = []
    real_append = oplog.append
    oplog.append = lambda op: order.append("append") or real_append(op)
    ctl.undo(actor=_actor(), persist=lambda: order.append("persist"))
    assert order == ["persist", "append"]             # 先落盘、再记 rollback
    assert doc.get("t", "e1").data["x"] == 1           # 反向已 apply
