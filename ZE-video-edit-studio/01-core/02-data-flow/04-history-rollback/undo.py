"""History & Rollback — UndoController + rollback 工厂.

完整 SPEC 见同目录 SPEC.md.

已实装 (无跨叶子依赖):
  - UndoController(op_log, doc): 双栈; undo / redo / can_undo / can_redo
  - undo() — 取末尾非-rollback op, 同 group_id 连续聚合, 反向 apply, 落 kind=rollback
  - redo() — 从 redo_stack pop 那条 rollback_op, 重新 apply 原正向 patches
  - prepare_undo / prepare_redo — CommitCoordinator 用的纯 producer,只 apply + 产 Operation
  - confirm_deferred — 耐久提交后才推进 redo 栈;未提交/重复/乱序确认拒绝
  - rollback_field(entity, field, target, actor) — 造 kind=rollback Operation (set patch)
  - rollback_entity_to_op(entity, op_log, target_op_id, actor) — 累积反向 patches

依赖:
  - 02-operation: new_operation / Operation / Patch / OpLog
  - 01-document: Document.apply_patch_directly (本叶子加进 document.py)
  - 02-entity: Entity / Actor

设计 (见 SPEC §3-§5):
  - 走 entity CAS (entity.update/delete/restore), 不绕审计
  - 不走 command ACL (undo 是系统级动作)
  - group_id 聚合: 只往回收集"连续 + 同 group_id"
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

# 平铺布局: 注入 entity / operation / document 兄弟目录
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

from entity import Actor, Entity, ZeError  # noqa: E402
from document import (  # noqa: E402
    _HISTORY_REPLAY_CAPABILITY,
    _history_operation_scope,
)
from operation import (  # noqa: E402
    ENTITY_META_FIELD,
    Operation,
    OpLog,
    Patch,
    new_operation,
)
from history_authority import (  # noqa: E402
    _HISTORY_REPLAY_CONSUMER_CAPABILITY,
    _history_oplog_is_bound,
    _history_replay_port,
    confirm_history_source_ticket,
    discard_history_source_ticket,
    history_authority_for_controller,
)


def _canonical_operation_body(operation: Operation | dict[str, Any]) -> bytes:
    """Return one exact, order-independent body for deferred log confirmation."""
    if type(operation) is Operation:
        payload = operation.to_dict()
    elif type(operation) is dict:
        payload = operation
    else:
        raise TypeError("operation body must be an exact Operation or dict")
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise ZeError(
            "validation_failed",
            "deferred history Operation body is not canonical JSON",
        ) from None


def _verify_history_replay_derivation(
    purpose: str,
    source_entries: tuple[dict[str, Any], ...],
    operation: Any,
) -> bool:
    """Pure verifier for the bootstrap-sealed history source contract.

    The runtime authority owns source freshness, single-use claims, and exact
    bodies.  This callback owns only UndoController semantics: an undo reverses
    the selected old-to-new group in new-to-old order; a redo replays one
    committed undo Operation.  Random issuance fields (id/ts) are intentionally
    outside derivation, while every replayed patch and semantic routing field is
    exact.  It performs no mutation and is safe for the protocol layer to retain
    by function identity at bootstrap.
    """
    if (
        purpose not in {"undo", "redo"}
        or type(source_entries) is not tuple
        or not source_entries
        or type(operation) is not Operation
        or operation.kind != "rollback"
        or type(operation.actor) is not dict
        or operation.actor.get("source") != operation.source
    ):
        return False
    try:
        sources = tuple(entry["op"] for entry in source_entries)
        if any(type(source) is not dict for source in sources):
            return False

        if purpose == "undo":
            if operation.command_id != "undo":
                return False
            expected_patches = tuple(
                patch
                for source in reversed(sources)
                for patch in source.get("reverse_patches", ())
            )
            expected_reverse = tuple(
                patch
                for source in sources
                for patch in source.get("patches", ())
            )
            group_id = sources[0].get("group_id", "")
            if group_id:
                if operation.group_id != group_id:
                    return False
            elif not operation.group_id:
                # Ungrouped native operations receive a fresh non-empty replay
                # group id; its random value is not source-derived.
                return False
            expected_trace_id = sources[-1].get("trace_id", "")
        else:
            if (
                len(sources) != 1
                or operation.command_id != "redo"
                or sources[0].get("kind") != "rollback"
                or sources[0].get("command_id") != "undo"
            ):
                return False
            expected_patches = tuple(sources[0].get("reverse_patches", ()))
            expected_reverse = tuple(sources[0].get("patches", ()))
            if operation.group_id != sources[0].get("group_id", ""):
                return False
            expected_trace_id = sources[0].get("trace_id", "")

        actual_patches = tuple(patch.to_dict() for patch in operation.patches)
        actual_reverse = tuple(
            patch.to_dict() for patch in operation.reverse_patches
        )
        return (
            actual_patches == expected_patches
            and actual_reverse == expected_reverse
            and operation.trace_id == expected_trace_id
        )
    except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
        return False


# ==============================================================
# UndoController
# ==============================================================

@dataclass(frozen=True)
class DeferredHistoryRecord:
    """一次已经 apply、但尚未确认提交的 undo / redo 结果.

    ``prepare_undo`` / ``prepare_redo`` 只改 Document 并产生 Operation；它们不保存
    Document、不写 OpLog，也不推进 redo 栈。CommitCoordinator 把 ``operations``
    作为同一事务提交成功后，必须调用 ``confirm_deferred``，控制器才推进历史游标。

    下划线字段是控制器用于拒绝跨实例、乱序和未落日志确认的内部凭据；caller 只消费
    ``token``、``kind`` 和 ``operations``。
    """

    token: str
    kind: Literal["undo", "redo"]
    operations: tuple[Operation, ...]
    _controller_id: str = field(repr=False)
    _base_log_length: int = field(repr=False)
    _redo_stack_snapshot: tuple[str, ...] = field(repr=False)
    _expected_redo_id: str = field(default="", repr=False)
    _invalidate_stale_redo: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if not self.token:
            raise ValueError("DeferredHistoryRecord.token must not be empty")
        if self.kind not in ("undo", "redo"):
            raise ValueError("DeferredHistoryRecord.kind must be 'undo' or 'redo'")
        if not self.operations:
            raise ValueError("DeferredHistoryRecord.operations must not be empty")
        if self.kind == "redo" and not self._expected_redo_id:
            raise ValueError("redo record requires _expected_redo_id")


class UndoController:
    """双栈撤销/重做控制器.

    持有 OpLog (历史 op 源) + Document (反向 apply 目标).
    redo_stack 存已提交 undo 的 rollback_op id；redo 提交确认后才 pop.
    """

    def __init__(self, op_log: OpLog, doc: Any) -> None:
        self._op_log = op_log
        self._doc = doc
        # Installed exactly once by history_authority.bind_history_authority.
        # These are process-local runtime identities, never durable business
        # state.  Keeping them on the exact controller lets Document close the
        # replay graph without importing the neutral authority module.
        self._history_authority_epoch: Any | None = None
        self._history_authority_pid: int | None = None
        # redo_stack 是内存 UI 态,不持久化(SPEC §8 持久化协议):每个进程从空起步,只被本进程
        # undo() push。重启后 OpLog 仍在 → 撤销可从落盘末尾继续(can_undo/undo 直读 OpLog),但 redo
        # 不跨窗口/会话(重启后 redo 无意义)。不从 OpLog 重建 redo_stack —— 跨 session 持久化 redo
        # 是 SPEC 待补项「业务方明确需求时再加」,当前刻意不做,避免重启后"幽灵可重做"。
        self._redo_stack: list[str] = []
        # deferred record 是 CommitCoordinator 持锁期间的短生命周期提交凭据，不是用户历史。
        # 一次只允许一个，避免尚未提交时再次读取旧 OpLog、把同一 group 重复 apply。
        self._controller_id = uuid.uuid4().hex
        self._pending_deferred: dict[str, DeferredHistoryRecord] = {}
        self._pending_operation_bodies: dict[str, tuple[bytes, ...]] = {}
        self._pending_history_tickets: dict[str, Any] = {}

    def _install_canonical_history_authority(
        self,
        *,
        authority_epoch: Any,
        issuer_pid: int,
    ) -> None:
        """Install the process-local canonical history identity once."""

        if (
            self._history_authority_epoch is not None
            or self._history_authority_pid is not None
        ):
            raise ZeError(
                "history_authority_mismatch",
                "canonical history authority may only be installed once",
                context={"reason": "history_controller_rebind_forbidden"},
            )
        if (
            authority_epoch is None
            or type(issuer_pid) is not int
            or issuer_pid != os.getpid()
        ):
            raise ZeError(
                "history_authority_mismatch",
                "canonical history controller identity is invalid",
                context={"reason": "history_controller_candidate_invalid"},
            )
        self._history_authority_epoch = authority_epoch
        self._history_authority_pid = issuer_pid

    def _rollback_canonical_history_authority_install(
        self,
        *,
        authority_epoch: Any,
        issuer_pid: int,
    ) -> bool:
        """Undo only an unpublished bind attempt with the exact identity."""

        if (
            (
                self._history_authority_epoch is None
                or self._history_authority_epoch is authority_epoch
            )
            and self._history_authority_pid in (None, issuer_pid)
        ):
            self._history_authority_epoch = None
            self._history_authority_pid = None
            return True
        return False

    # ---- 状态查询 ----

    def can_undo(self) -> bool:
        """还有未撤销的 native op group 才可撤(与 undo() 同一判据)。

        旧实现只查"有无非 rollback/stage op": 多级 undo 撤到底后 OpLog 里 native op 仍在 →
        can_undo() 恒 True 但 undo() 已无 group 可撤返 None,⌘Z 钮亮着却撤不动(旧 bug)。"""
        entries = self._op_log.entries()
        groups = self._native_groups(entries)
        return len(groups) - self._oplog_undone_depth(entries) > 0

    def _transaction_entries(self) -> tuple[Any, list[dict[str, Any]]]:
        """Use the coordinator's single fresh snapshot for protected replay."""
        authority = history_authority_for_controller(self)
        if authority is None:
            if bool(getattr(self._doc, "_mutation_policies", {})):
                raise ZeError(
                    "history_authority_mismatch",
                    "protected history requires bootstrap-bound canonical authority",
                    context={"reason": "history_authority_not_bound"},
                )
            return None, self._op_log.entries()
        replay_port = _history_replay_port(
            self,
            _capability=_HISTORY_REPLAY_CONSUMER_CAPABILITY,
        )
        if replay_port is None:
            raise ZeError(
                "history_replay_coordinator_required",
                "protected history requires its exact active CommitCoordinator",
                context={"reason": "history_transaction_required"},
            )
        return replay_port, list(replay_port.fresh_entries)

    @staticmethod
    def _native_groups(entries: list[dict]) -> list[list[dict]]:
        """可撤的 native op(跳过 rollback/stage)按"连续同非空 group_id"聚合成 group 列表(旧→新)。

        drag 类同 group_id 的连续 op 聚成一组、一次 undo 全撤;空 group_id 各自单独成组。"""
        groups: list[list[dict]] = []
        for entry in entries:
            op_dict = entry["op"]
            if op_dict.get("kind") in ("rollback", "stage"):
                continue
            gid = op_dict.get("group_id", "")
            if groups and gid and groups[-1][0].get("group_id", "") == gid:
                groups[-1].append(op_dict)
            else:
                groups.append([op_dict])
        return groups

    @staticmethod
    def _indexed_native_groups(
        entries: list[dict[str, Any]],
    ) -> list[list[tuple[int, dict[str, Any]]]]:
        groups: list[list[tuple[int, dict[str, Any]]]] = []
        for index, entry in enumerate(entries):
            op_dict = entry["op"]
            if op_dict.get("kind") in ("rollback", "stage"):
                continue
            gid = op_dict.get("group_id", "")
            if groups and gid and groups[-1][0][1].get("group_id", "") == gid:
                groups[-1].append((index, op_dict))
            else:
                groups.append([(index, op_dict)])
        return groups

    @staticmethod
    def _oplog_undone_depth(entries: list[dict]) -> int:
        """从 OpLog 尾部推导"已撤销未重做的 native group 层数",**跨重启可靠**。

        不能用内存 redo_stack 长度: 它按 SPEC §8 不跨重启、新进程恒从 0 起步,而 OpLog 落盘。
        若重启前已 undo N 次未 redo, 新进程 redo_stack=0 会让 undo() 重新 target 已撤的 group、
        对文档 blind-set 一个已被撤销的旧值(code-review 实测 CRITICAL)。改从 OpLog 尾部数
        undo/redo 自身 rollback 净值(undo +1 / redo −1):遇 native op 即止(分叉时尾部即 native、
        自然归 0);遇其他来源 rollback(operation_group/entity_to_op/field)透明跳过、不 break
        (否则被当栈尾致 depth 归 0、undo() 重放已撤 group —— code-review HIGH)。
        遗留(未修):独立 rollback 撤到 undo 下一步 target 的同一 group 时语义冲突,需单独设计
        (undo LIFO 栈 vs 任意 group 回滚)。见 V3-MIGRATION-GAPS。"""
        depth = 0
        for entry in reversed(entries):
            op = entry["op"]
            kind = op.get("kind")
            if kind == "native":
                break   # 尾部第一个未撤销的 native op: undo 栈尾到此为止
            if kind == "rollback":
                cmd = op.get("command_id")
                if cmd == "undo":
                    depth += 1
                elif cmd == "redo":
                    depth -= 1
                # 其他来源的 rollback(operation_group/entity_to_op/field): 透明跳过 —— break 会把它
                # 当栈尾致 depth 归 0、undo() 重放已撤 group(code-review HIGH);也不计数(撤任意 group)。
            # stage 及其他 kind: 跳过继续
        return max(0, depth)

    def can_redo(self) -> bool:
        return self._redo_is_current()

    def _redo_is_current(self) -> bool:
        """**纯查询(无副作用)**:redo 栈非空、且栈顶 rollback 之后没有新 native 编辑 = 可 redo。

        作废(清栈)只在会改状态的 undo()/redo() 里做(见 _invalidate_stale_redo)。**绝不在这里清栈** ——
        can_redo() 会被 GET /api/history(前端刷新撤销/重做按钮)每次调,只读操作偷偷毁掉 redo 栈会让用户
        没点重做就看见「可重做」翻成 False 且不可逆(code-review CRITICAL)。"""
        return bool(self._redo_stack) and not self._op_log.has_native_after(self._redo_stack[-1])

    @staticmethod
    def _has_native_after_entries(
        entries: list[dict[str, Any]], operation_id: str,
    ) -> bool:
        """Snapshot-local equivalent of ``OpLog.has_native_after``.

        A protected producer must make every history decision from the one
        fresh suffix snapshot issued by its coordinator transaction.  Reading
        OpLog again here would silently mix two heads in one undo/redo action.
        """
        found = False
        for entry in entries:
            operation = entry["op"]
            if found and operation.get("kind") == "native":
                return True
            if operation.get("id") == operation_id:
                found = True
        return False

    def _redo_is_current_entries(self, entries: list[dict[str, Any]]) -> bool:
        return bool(self._redo_stack) and not self._has_native_after_entries(
            entries, self._redo_stack[-1],
        )

    def _invalidate_stale_redo(self) -> None:
        """栈顶 rollback 已被新 native 编辑分叉 → 清空整个 redo 栈(新编辑作废所有待 redo)。
        只在 undo()/redo() 这些会改状态的动作里调,不在 can_redo() 等查询里调。"""
        if self._redo_stack and self._op_log.has_native_after(self._redo_stack[-1]):
            self._redo_stack.clear()

    # ---- deferred commit protocol ----

    def _ensure_no_pending_deferred(self) -> None:
        if self._pending_deferred:
            pending = next(iter(self._pending_deferred.values()))
            raise ZeError(
                "validation_failed",
                "a deferred history mutation is still awaiting commit confirmation",
                context={"token": pending.token, "kind": pending.kind},
            )

    def _register_deferred(
        self,
        *,
        kind: Literal["undo", "redo"],
        operation: Operation,
        base_log_length: int,
        redo_stack_snapshot: tuple[str, ...],
        expected_redo_id: str = "",
        invalidate_stale_redo: bool = False,
        source_ticket: Any = None,
    ) -> DeferredHistoryRecord:
        operation_body = _canonical_operation_body(operation)
        record = DeferredHistoryRecord(
            token=uuid.uuid4().hex,
            kind=kind,
            operations=(operation,),
            _controller_id=self._controller_id,
            _base_log_length=base_log_length,
            _redo_stack_snapshot=redo_stack_snapshot,
            _expected_redo_id=expected_redo_id,
            _invalidate_stale_redo=invalidate_stale_redo,
        )
        self._pending_deferred[record.token] = record
        self._pending_operation_bodies[record.token] = (operation_body,)
        if source_ticket is not None:
            self._pending_history_tickets[record.token] = source_ticket
        return record

    def _require_pending(self, record: DeferredHistoryRecord) -> DeferredHistoryRecord:
        if not isinstance(record, DeferredHistoryRecord):
            raise TypeError("record must be DeferredHistoryRecord")
        if record._controller_id != self._controller_id:
            raise ZeError(
                "validation_failed",
                "deferred history record belongs to another UndoController",
                context={"token": record.token},
            )
        pending = self._pending_deferred.get(record.token)
        if pending is None or pending != record:
            raise ZeError(
                "validation_failed",
                "deferred history record is not pending (already confirmed or discarded)",
                context={"token": record.token, "kind": record.kind},
            )
        return pending

    def confirm_deferred(self, record: DeferredHistoryRecord) -> None:
        """确认 CommitCoordinator 已提交 record，再推进 redo 栈.

        该方法**不写 OpLog**。提交器必须先把 record.operations 原样写入本控制器读取的
        OpLog / CommitJournal；这里核对日志长度和尾部完整 canonical Operation body，防止
        caller 在耐久提交之前误确认、同 id 正文被替换，或并发写入后乱序推进本地历史游标。
        重复确认会明确抛错。
        """
        pending = self._require_pending(record)
        expected_operation_bodies = self._pending_operation_bodies.get(
            pending.token,
        )
        if (
            type(expected_operation_bodies) is not tuple
            or len(expected_operation_bodies) != len(pending.operations)
        ):
            raise ZeError(
                "validation_failed",
                "deferred history Operation body ledger is incomplete",
                context={"token": pending.token, "kind": pending.kind},
            )

        # 所有校验必须先于栈变更，失败时历史状态保持原样。
        entries = self._op_log.entries()
        operation_ids = tuple(operation.id for operation in pending.operations)
        actual_tail_ids = tuple(
            entry["op"].get("id", "") for entry in entries[-len(operation_ids):]
        )
        actual_tail_bodies = tuple(
            _canonical_operation_body(entry["op"])
            for entry in entries[-len(operation_ids):]
        )
        expected_length = pending._base_log_length + len(operation_ids)
        if (
            len(entries) != expected_length
            or actual_tail_ids != operation_ids
            or actual_tail_bodies != expected_operation_bodies
        ):
            raise ZeError(
                "validation_failed",
                "the exact deferred history Operations must be committed to OpLog before confirmation",
                context={
                    "token": pending.token,
                    "expected_log_length": expected_length,
                    "actual_log_length": len(entries),
                    "expected_tail_operation_ids": list(operation_ids),
                    "actual_tail_operation_ids": list(actual_tail_ids),
                },
            )
        if tuple(self._redo_stack) != pending._redo_stack_snapshot:
            raise ZeError(
                "validation_failed",
                "redo stack changed while deferred history commit was pending",
                context={"token": pending.token, "kind": pending.kind},
            )

        source_ticket = self._pending_history_tickets.get(pending.token)
        if source_ticket is not None:
            # This is stronger than the compatibility tail check above: the
            # coordinator has proved the exact source snapshot, replay body,
            # write scope, durable suffix, and canonical runtime binding.
            confirm_history_source_ticket(
                source_ticket,
                controller=self,
                operation=pending.operations[0],
            )

        if pending.kind == "undo":
            if pending._invalidate_stale_redo:
                self._redo_stack.clear()
            self._redo_stack.append(pending.operations[-1].id)
        else:
            if not self._redo_stack or self._redo_stack[-1] != pending._expected_redo_id:
                # snapshot 校验通常已经覆盖；保留显式语义校验，让协议错误更容易定位。
                raise ZeError(
                    "validation_failed",
                    "redo confirmation no longer matches the redo stack head",
                    context={"token": pending.token},
                )
            self._redo_stack.pop()

        del self._pending_deferred[pending.token]
        del self._pending_operation_bodies[pending.token]
        self._pending_history_tickets.pop(pending.token, None)

    def discard_deferred(self, record: DeferredHistoryRecord) -> None:
        """丢弃未确认凭据，不改 OpLog / redo 栈.

        这里只释放控制器的短生命周期凭据；Document 已 apply 的回滚属于通用 mutation
        rollback（W04），必须由 CommitCoordinator 的失败路径负责，不能在这里偷偷 blind-set。
        """
        pending = self._require_pending(record)
        source_ticket = self._pending_history_tickets.get(pending.token)
        if source_ticket is not None:
            # Only a transaction whose canonical head is proven untouched may
            # release the one-shot replay source.  FROZEN/unknown outcomes are
            # deliberately not convertible into a clean discard.
            discard_history_source_ticket(source_ticket, controller=self)
        del self._pending_deferred[pending.token]
        del self._pending_operation_bodies[pending.token]
        self._pending_history_tickets.pop(pending.token, None)

    # ---- undo ----

    def prepare_undo(self, *, actor: Actor) -> DeferredHistoryRecord | None:
        """原子 apply 最近一组 reverse patches，并返回待提交记录.

        不 save、不 append、不 push/clear redo 栈。caller 必须在同一写锁内提交
        ``record.operations``，成功后调用 :meth:`confirm_deferred`；失败并恢复 Document 后调用
        :meth:`discard_deferred`。
        """
        self._ensure_no_pending_deferred()
        return self._prepare_undo(actor=actor)

    def _prepare_undo(self, *, actor: Actor) -> DeferredHistoryRecord | None:
        history_transaction, entries = self._transaction_entries()
        if not entries:
            return None

        redo_snapshot = tuple(self._redo_stack)
        invalidate_stale_redo = (
            bool(self._redo_stack)
            and not self._redo_is_current_entries(entries)
        )

        # 1) 按 native op group 聚合, 撤"倒数第 (redo_depth+1) 个"尚未撤销的 group。
        #    redo_depth 从已提交 OpLog 推导，不把尚未确认的 deferred record 算成历史。
        #    旧实现只取"最末非 rollback op"、不减去已撤层数 → 连按 ⌘Z 永远停在最近一步、更早的
        #    op 永不可达(多级 undo 从未生效);且每次还追加一条 before 值已过时的 rollback、虚增 redo。
        groups = self._indexed_native_groups(entries)
        redo_depth = self._oplog_undone_depth(entries)   # 从 OpLog 尾部推导, 跨重启可靠(不依赖内存 redo_stack)
        target_idx = len(groups) - 1 - redo_depth
        if target_idx < 0:
            return None
        group = groups[target_idx]
        target_entries = [
            operation for _index, operation in reversed(group)
        ]   # 新→旧: 供反向 apply(与旧实现收集顺序一致)
        target_group = group[0][1].get("group_id", "")

        source_ticket = None
        if history_transaction is not None:
            source_ticket = history_transaction.select_history_source(
                controller=self,
                purpose="undo",
                entry_indexes=tuple(index for index, _operation in group),
            )

        # 2) 收集反向 patches(target_entries 新→旧),整组**原子 apply**:任一 patch 失败(如命中已被后续
        #    op 硬删的 entity)→ doc 零变更、不落 rollback、原样抛出(旧实现逐条 apply 中途抛错留半撤销内存态,
        #    下条命令 save 持久化 → doc 与 oplog 分叉)。
        applied_reverse: list[Patch] = []
        for op_dict in target_entries:
            for rp_dict in op_dict.get("reverse_patches", []):
                applied_reverse.append(_patch_from_dict(rp_dict))
        # 3) 收集 forward_patches (给 redo 用): 必须按"旧→新"顺序, 否则 redo 时
        #    后做的 patch 会被先做的 patch 覆盖 (set 用 after 字段不依赖 before).
        forward_patches: list[Patch] = []
        for op_dict in reversed(target_entries):
            for fp_dict in op_dict.get("patches", []):
                forward_patches.append(_patch_from_dict(fp_dict))

        # 4) 先签发完整 rollback Operation，再把同一实例绑定到受保护的 replay。
        # session 会封存 identity + canonical body，producer 不能在落盘前替换或改写。
        rollback_op = new_operation(
            command_id="undo",
            source=actor.source,
            actor=actor.to_dict(),
            patches=applied_reverse,
            reverse_patches=forward_patches,   # 给 redo 用 (旧→新顺序)
            group_id=target_group or uuid.uuid4().hex,
            trace_id=target_entries[0].get("trace_id", ""),
            kind="rollback",
        )
        if history_transaction is not None:
            history_transaction.bind_replay(source_ticket, rollback_op)
        with _history_operation_scope(
            self._doc,
            rollback_op,
            actor=actor,
            controller=self,
            source_ticket=source_ticket,
            _capability=_HISTORY_REPLAY_CAPABILITY,
        ):
            self._doc.apply_patches_atomically(
                rollback_op.patches,
                actor=actor,
                _replay_capability=_HISTORY_REPLAY_CAPABILITY,
            )
        return self._register_deferred(
            kind="undo",
            operation=rollback_op,
            base_log_length=len(entries),
            redo_stack_snapshot=redo_snapshot,
            invalidate_stale_redo=invalidate_stale_redo,
            source_ticket=source_ticket,
        )

    def undo(self, *, actor: Actor, persist: Callable[[], None] | None = None) -> list[Operation] | None:
        """撤销最末非-rollback op (含同 group_id 连续聚合).

        默认兼容路径仍执行 ``apply -> persist -> append -> redo 栈确认``，返回已落地的
        rollback_op 列表；新提交器应改用 :meth:`prepare_undo`，避免控制器自己写存储。
        """
        self._ensure_no_pending_deferred()
        self._invalidate_stale_redo()   # 默认路径保持旧行为；deferred 路径只在 confirm 后清理
        record = self._prepare_undo(actor=actor)
        if record is None:
            return None

        try:
            if persist is not None:
                persist()
            for operation in record.operations:
                self._op_log.append(operation)
        except Exception:
            self.discard_deferred(record)
            raise
        self.confirm_deferred(record)
        return list(record.operations)

    # ---- redo ----

    def prepare_redo(self, *, actor: Actor) -> DeferredHistoryRecord | None:
        """原子 apply redo patches 并返回待提交记录，不提前消费 redo 栈."""
        self._ensure_no_pending_deferred()
        return self._prepare_redo(actor=actor)

    def _prepare_redo(self, *, actor: Actor) -> DeferredHistoryRecord | None:
        history_transaction, entries = self._transaction_entries()
        if not self._redo_is_current_entries(entries):
            return None
        rollback_id = self._redo_stack[-1]   # prepare 阶段只 peek；成功确认后才 pop

        # 找该 rollback_op
        target_entry = None
        target_index = -1
        for index, entry in enumerate(entries):
            if entry["op"].get("id") == rollback_id:
                target_entry = entry
                target_index = index
                break
        if target_entry is None:
            # 不应发生; 防御性
            return None

        op_dict = target_entry["op"]
        source_ticket = None
        if history_transaction is not None:
            source_ticket = history_transaction.select_history_source(
                controller=self,
                purpose="redo",
                entry_index=target_index,
            )
        # 取 reverse_patches: 这正是"原正向 patches"; 整组**原子 apply** = 重做(失败零变更、不落 redo_op、原样抛出)
        applied: list[Patch] = [_patch_from_dict(fp) for fp in op_dict.get("reverse_patches", [])]
        backward: list[Patch] = [_patch_from_dict(rp) for rp in op_dict.get("patches", [])]
        redo_op = new_operation(
            command_id="redo",
            source=actor.source,
            actor=actor.to_dict(),
            patches=applied,
            reverse_patches=backward,
            group_id=op_dict.get("group_id", ""),
            trace_id=op_dict.get("trace_id", ""),
            kind="rollback",
        )
        if history_transaction is not None:
            history_transaction.bind_replay(source_ticket, redo_op)
        with _history_operation_scope(
            self._doc,
            redo_op,
            actor=actor,
            controller=self,
            source_ticket=source_ticket,
            _capability=_HISTORY_REPLAY_CAPABILITY,
        ):
            self._doc.apply_patches_atomically(
                redo_op.patches,
                actor=actor,
                _replay_capability=_HISTORY_REPLAY_CAPABILITY,
            )
        return self._register_deferred(
            kind="redo",
            operation=redo_op,
            base_log_length=len(entries),
            redo_stack_snapshot=tuple(self._redo_stack),
            expected_redo_id=rollback_id,
            source_ticket=source_ticket,
        )

    def redo(self, *, actor: Actor, persist: Callable[[], None] | None = None) -> list[Operation] | None:
        """重做最后一次 undo；默认兼容路径仍自行 persist + append.

        新提交器应使用 :meth:`prepare_redo`；它在提交确认前不会 pop redo 栈。
        """
        self._ensure_no_pending_deferred()
        self._invalidate_stale_redo()   # 默认路径保持旧的「新编辑立即作废 redo」行为
        record = self._prepare_redo(actor=actor)
        if record is None:
            return None

        try:
            if persist is not None:
                persist()
            for operation in record.operations:
                self._op_log.append(operation)
        except Exception:
            self.discard_deferred(record)
            raise
        self.confirm_deferred(record)
        return list(record.operations)


# ==============================================================
# realtime_checkpoint: operation group / session rollback
# ==============================================================

@dataclass(frozen=True)
class RollbackConflict:
    """回滚前检测到的后续写冲突."""

    entity_type: str
    entity_id: str
    field: str
    later_operation_id: str
    later_command_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "field": self.field,
            "later_operation_id": self.later_operation_id,
            "later_command_id": self.later_command_id,
        }


@dataclass(frozen=True)
class GroupRollbackResult:
    """operation group/session rollback 的结果."""

    applied: bool
    rollback_op: Operation | None = None
    conflicts: list[RollbackConflict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "rollback_op": self.rollback_op.to_dict() if self.rollback_op else None,
            "conflicts": [c.to_dict() for c in self.conflicts],
        }


def rollback_operation_group(
    op_log: OpLog,
    doc: Any,
    *,
    actor: Actor,
    group_id: str = "",
    session_id: str = "",
    source_filter: tuple[str, ...] = ("user",),
    conflict_policy: str = "abort",
) -> GroupRollbackResult:
    """回滚一个 operation group 或 session 里的操作.

    默认只回滚 source='user' 的操作,这对应"关闭网页前撤回本次人类修改"。
    AI task 的 publish 不会被顺手撤掉;需要撤 AI 结果时应走 candidate reject /
    task result rollback 等显式命令。

    conflict_policy:
      - abort: 有后续同字段修改就不应用任何 patch
      - force: 即使有冲突也应用 reverse_patches
    """
    if not group_id and not session_id:
        raise ZeError(
            "validation_failed",
            "rollback_operation_group requires group_id or session_id",
        )
    if conflict_policy not in ("abort", "force"):
        raise ZeError(
            "validation_failed",
            "conflict_policy must be 'abort' or 'force'",
            context={"conflict_policy": conflict_policy},
        )
    if (
        _history_oplog_is_bound(op_log)
        or bool(getattr(doc, "_mutation_policies", {}))
    ):
        # This compatibility helper owns neither an UndoController source
        # ticket nor the bound CommitCoordinator store port.  Reject before it
        # reads history or mutates Document.  Every bootstrapped project has a
        # canonical history lease even when its Document has no type mutation
        # policies, so the exact bound OpLog is itself sufficient to close this
        # bypass.  Protected runtimes must use prepare_undo/prepare_redo through
        # their exact coordinator.
        raise ZeError(
            "history_replay_coordinator_required",
            "protected group rollback requires its exact active CommitCoordinator",
            context={"reason": "history_transaction_required"},
        )

    entries = op_log.entries()
    selected_indexes: list[int] = []
    selected_ids: set[str] = set()
    for idx, entry in enumerate(entries):
        op = entry["op"]
        if op.get("kind") == "rollback":
            continue
        if source_filter and op.get("source") not in source_filter:
            continue
        if group_id and op.get("group_id") != group_id:
            continue
        if session_id and op.get("session_id") != session_id:
            continue
        selected_indexes.append(idx)
        selected_ids.add(op["id"])

    if not selected_indexes:
        return GroupRollbackResult(applied=False)

    conflicts = _find_later_conflicts(entries, selected_indexes, selected_ids)
    if conflicts and conflict_policy == "abort":
        return GroupRollbackResult(applied=False, conflicts=conflicts)

    # 反向应用: 新 -> 旧,每条 op 内按 reverse_patches 顺序重放。
    reverse_to_apply: list[Patch] = []
    forward_for_redo: list[Patch] = []
    for idx in reversed(selected_indexes):
        op = entries[idx]["op"]
        for rp in op.get("reverse_patches", []):
            reverse_to_apply.append(_patch_from_dict(rp))
    for idx in selected_indexes:
        op = entries[idx]["op"]
        for fp in op.get("patches", []):
            forward_for_redo.append(_patch_from_dict(fp))

    first_op = entries[selected_indexes[0]]["op"]
    rollback_op = new_operation(
        command_id="rollback_operation_group",
        source=actor.source,
        actor=actor.to_dict(),
        patches=reverse_to_apply,
        reverse_patches=forward_for_redo,
        group_id=group_id or first_op.get("group_id", "") or uuid.uuid4().hex,
        trace_id=first_op.get("trace_id", ""),
        kind="rollback",
        document_id=first_op.get("document_id", ""),
        session_id=session_id or first_op.get("session_id", ""),
    )
    # 整组原子 apply:任一 reverse-patch 失败 → doc 零变更、不落 rollback_op、原样抛出(不留半回滚态)。
    with _history_operation_scope(
        doc,
        rollback_op,
        actor=actor,
        _capability=_HISTORY_REPLAY_CAPABILITY,
    ):
        doc.apply_patches_atomically(
            rollback_op.patches,
            actor=actor,
            _replay_capability=_HISTORY_REPLAY_CAPABILITY,
        )
    op_log.append(rollback_op)
    return GroupRollbackResult(
        applied=True,
        rollback_op=rollback_op,
        conflicts=conflicts,
    )


def _find_later_conflicts(
    entries: list[dict[str, Any]],
    selected_indexes: list[int],
    selected_ids: set[str],
) -> list[RollbackConflict]:
    """查找 selected op 之后的非 selected 同字段写."""
    affected: dict[tuple[str, str, str], int] = {}
    for idx in selected_indexes:
        op = entries[idx]["op"]
        for p in op.get("patches", []):
            affected[(p["entity_type"], p["entity_id"], p["field"])] = max(
                idx,
                affected.get((p["entity_type"], p["entity_id"], p["field"]), -1),
            )

    conflicts: list[RollbackConflict] = []
    for idx, entry in enumerate(entries):
        op = entry["op"]
        if op.get("id") in selected_ids or op.get("kind") == "rollback":
            continue
        for p in op.get("patches", []):
            key = (p["entity_type"], p["entity_id"], p["field"])
            selected_idx = affected.get(key)
            if selected_idx is not None and idx > selected_idx:
                conflicts.append(
                    RollbackConflict(
                        entity_type=p["entity_type"],
                        entity_id=p["entity_id"],
                        field=p["field"],
                        later_operation_id=op["id"],
                        later_command_id=op.get("command_id", ""),
                    )
                )
    return conflicts


# ==============================================================
# rollback 工厂
# ==============================================================

def rollback_field(
    entity: Entity,
    field: str,
    target_value: Any,
    *,
    actor: Actor,
    group_id: str = "",
) -> Operation:
    """造一个 rollback Operation: 把 entity.data[field] 设回 target_value.

    forward patches: set field = target_value (撤回到目标值)
    reverse_patches: set field = 当前值 (redo 时用, 重新设回 redo 前那个值)

    实际 apply 留给 caller; 这里只造 Operation.
    """
    current_value = entity.get(field)
    forward = Patch(
        entity_type=entity.type,
        entity_id=entity.id,
        field=field,
        op="set",
        before=current_value,
        after=target_value,
    )
    reverse = Patch(
        entity_type=entity.type,
        entity_id=entity.id,
        field=field,
        op="set",
        before=target_value,
        after=current_value,
    )
    return new_operation(
        command_id="rollback_field",
        source="user",
        actor=actor.to_dict(),
        patches=[forward],
        reverse_patches=[reverse],
        group_id=group_id,
        kind="rollback",
    )


def rollback_entity_to_op(
    entity: Entity,
    op_log: OpLog,
    target_op_id: str,
    *,
    actor: Actor,
) -> Operation:
    """造 rollback Operation: 把 entity 回到 target_op_id 后的状态.

    实施: 找 op_log 里 target_op_id (含)之后所有影响该 entity 的 patches,
         累积 reverse_patches (即从新到旧拼起来).
    target_op_id 不存在 → 抛 validation_failed.
    """
    entries = op_log.entries()
    found = False
    affecting: list[dict] = []   # target 之后影响该 entity 的 entries (从旧到新)

    for entry in entries:
        op_dict = entry["op"]
        if op_dict.get("id") == target_op_id:
            found = True
            # target_op_id 本身不撤; 撤它后面的
            continue
        if not found:
            continue
        # 只算影响该 entity 的
        touches = any(
            p["entity_type"] == entity.type and p["entity_id"] == entity.id
            for p in op_dict.get("patches", [])
        )
        if touches:
            affecting.append(op_dict)

    if not found:
        raise ZeError(
            "validation_failed",
            f"target_op_id {target_op_id!r} not found in op_log",
            context={"target_op_id": target_op_id},
        )

    # forward_patches (op.patches): 新→旧, 顺序 apply 即从 entity 当前状态撤回到 target
    forward_patches: list[Patch] = []
    for op_dict in reversed(affecting):
        for rp_dict in op_dict.get("reverse_patches", []):
            patch = _patch_from_dict(rp_dict)
            if (patch.entity_type == entity.type
                    and patch.entity_id == entity.id):
                forward_patches.append(patch)
    # reverse_for_redo (op.reverse_patches): 必须旧→新 (跟 UndoController.undo 同因);
    # redo 时 controller 会按这个顺序 apply, set patch 用 after 字段, 顺序错被覆盖.
    reverse_for_redo: list[Patch] = []
    for op_dict in affecting:
        for fp_dict in op_dict.get("patches", []):
            patch = _patch_from_dict(fp_dict)
            if (patch.entity_type == entity.type
                    and patch.entity_id == entity.id):
                reverse_for_redo.append(patch)

    return new_operation(
        command_id="rollback_entity_to_op",
        source="user",
        actor=actor.to_dict(),
        patches=forward_patches,
        reverse_patches=reverse_for_redo,
        kind="rollback",
    )


# ==============================================================
# 辅助: 从 dict 恢复 Patch
# ==============================================================

def _patch_from_dict(d: dict[str, Any]) -> Patch:
    return Patch(
        entity_type=d["entity_type"],
        entity_id=d["entity_id"],
        field=d["field"],
        op=d["op"],
        before=d.get("before"),
        after=d.get("after"),
        provenance=d.get("provenance"),
        position=d.get("position"),
        ancestry=d.get("ancestry"),
    )


__all__ = [
    "DeferredHistoryRecord",
    "UndoController",
    "RollbackConflict",
    "GroupRollbackResult",
    "rollback_operation_group",
    "rollback_field",
    "rollback_entity_to_op",
]
