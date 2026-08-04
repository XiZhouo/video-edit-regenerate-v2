"""supervisor — 活的管线编排器(生命周期 + 文件统计进度 + 占位 + 重试).

PipelineRunner 是同步多 step 引擎;Supervisor 是它缺的那层:服务器常驻单例,持有
正在跑的 run,提供 start / pause / resume / cancel / retry_failed,把进度按 checksum receipt
派生、经 broker→SSE 实时下发。复用 stage.py 的 StageRunner(progress/cancel/checkpoint),
重试语义镜像 PipelineRunner.retry_failed(只重跑 error 项),但"失败"按磁盘标记判,故崩溃安全。

模型:work-list 即 items,每个 item 跑一遍 item_stage(单 stage / item)。
占位 = item 起跑时建的 entity(source='system'),逐项完成经 entity.update(source='ai')填充。
worker 在独立后台线程跑;暂停在项间断点(pause_gate);doc 变更(建占位 / 提交)持
server 的同一把锁,stage 计算在锁外(分钟级 AI 调用不冻 UI)。

边界:
  - progress 节流 / 跨进程队列仍属 [02-data-flow]。
  - 进程重启由 mod 的 stage_resolver 重建可执行 StagePlan；恢复后真正续跑前，
    无 marker 的 suffix 按潜在重放强制 retry_safe，不会猜测执行外部副作用。
"""

from __future__ import annotations

import copy
import json
import os
import stat
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path
from typing import Any, Callable, Iterator

# 注意:平铺 sys.path 下有本地 05-concurrency/queue.py 遮蔽 stdlib queue,
# 故 worker 队列用 collections.deque(append/popleft 在 CPython 原子、多线程安全)。

# 兄弟叶子 + _protocol 注入(平铺布局)
_HERE = Path(__file__).resolve().parent
_PROTOCOL_DIR = _HERE.parent.parent / "_protocol"
_ENTITY_DIR = _HERE.parent.parent / "01-data-structure" / "02-entity"
_PERSISTENCE_DIR = _HERE.parent.parent / "02-data-flow" / "07-persistence"
_COMMAND_DIR = _HERE.parent / "01-command"
_OPERATION_DIR = _HERE.parent / "02-operation"
for _d in (_PROTOCOL_DIR, _ENTITY_DIR, _PERSISTENCE_DIR, _COMMAND_DIR, _OPERATION_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from entity import Actor, ZeError  # noqa: E402
from stage import StageRunner, StageSpec, _thaw_stage_json  # noqa: E402
from stage_plan import StagePlan  # noqa: E402
from work_list import WorkItem, WorkList  # noqa: E402
import artifact_scan as scan  # noqa: E402
from run_status import EntityRunStatus, StageStatus  # noqa: E402
from change_set import (  # noqa: E402
    affected_entities, to_broadcast_payload, to_status_payload, to_pipeline_status_payload,
)
from ports import MutationBatch  # noqa: E402
from command import (  # noqa: E402
    CommandMutation,
    DeferredAction,
    _active_command_root_snapshot,
)
from operation import Operation, new_operation  # noqa: E402
from pipeline_authority_commit import (  # noqa: E402
    issue_sealed_pipeline_start_operation,
)
from history_authority import HistoryAuthorityError  # noqa: E402


SNAPSHOT_VALUE_LIMIT = 64_000
_MAX_RUN_MANIFEST_BYTES = 8 * 1024 * 1024
_RECOVERY_IDENTITY_FIELDS = "recovery_identity_fields"
_SEED_DATA = "seed_data"


def _canonical_identity_bytes(value: Any) -> bytes:
    """Canonical JSON equality that keeps bool/int/float and nested shapes distinct."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _recovery_identity_matches(item: WorkItem, entity: Any) -> bool:
    """Bind untrusted manifest seed members to an already committed Document entity."""
    try:
        payload = item.payload_copy()
    except (TypeError, ValueError, RecursionError):
        return False
    fields = payload.get(_RECOVERY_IDENTITY_FIELDS)
    if fields is None:
        return True
    if entity is None:
        return False
    seed = payload.get(_SEED_DATA)
    if type(fields) is not list or type(seed) is not dict:
        return False
    data = entity.data
    try:
        return all(
            field in data
            and _canonical_identity_bytes(data[field]) == _canonical_identity_bytes(seed[field])
            for field in fields
        )
    except (KeyError, TypeError, ValueError):
        return False


class _RecoveryRejected(Exception):
    """Internal fixed-code rejection; raw parser/resolver errors never reach the client."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _read_recovery_manifest(rd: Path, mp: Path) -> dict[str, Any]:
    """Read run.json from one fixed real directory/file inode without following links."""
    if not hasattr(os, "O_NOFOLLOW"):
        raise _RecoveryRejected("pipeline_manifest_unsupported_platform")
    directory_fd = -1
    file_fd = -1
    try:
        directory_fd = os.open(
            rd,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        directory_info = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != os.geteuid():
            raise _RecoveryRejected("pipeline_manifest_invalid")
        file_fd = os.open(
            mp.name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory_fd,
        )
        before = os.fstat(file_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or before.st_size <= 0
            or before.st_size > _MAX_RUN_MANIFEST_BYTES
        ):
            raise _RecoveryRejected("pipeline_manifest_invalid")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(file_fd, min(1024 * 1024, remaining))
            if not chunk:
                raise _RecoveryRejected("pipeline_manifest_invalid")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(file_fd)
        if (
            after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_nlink != 1
            or after.st_uid != os.geteuid()
        ):
            raise _RecoveryRejected("pipeline_manifest_invalid")
        parsed = json.loads(b"".join(chunks).decode("utf-8"))
        if type(parsed) is not dict:
            raise _RecoveryRejected("pipeline_manifest_invalid")
        return parsed
    except _RecoveryRejected:
        raise
    except (OSError, TypeError, ValueError, UnicodeError):
        raise _RecoveryRejected("pipeline_manifest_invalid") from None
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if directory_fd >= 0:
            os.close(directory_fd)


class _RunState:
    """一次 run 的运行时状态(内存)。"""

    def __init__(self, *, work_list: WorkList, stage_plan: StagePlan, trace_id: str,
                 pipeline_id: str = "", start_command_id: str = "",
                 credential_actor: Any = None, credential_store: Any = None,
                 credential_policy_store: Any = None, credential_tool_id: str = "") -> None:
        self.run_id = work_list.run_id
        self.pipeline_id = pipeline_id or work_list.run_id
        self.start_command_id = start_command_id
        self.credential_actor = credential_actor
        self.credential_store = credential_store
        self.credential_policy_store = credential_policy_store
        self.credential_tool_id = credential_tool_id
        self.name = work_list.name
        self.work_list = work_list
        self.stage_plan = stage_plan
        self.item_stage = stage_plan[0]   # 单 stage 路径直接用;多 stage 路径走 stage_plan
        self.trace_id = trace_id
        self.status = "running"  # running|paused|interrupting|interrupted|cancelling|cancelled|success|error
        self.started_at = time.time()
        self.ended_at = 0.0
        self.log_lines: list[str] = []
        self.log_events: list[dict[str, Any]] = []
        self.step_states: dict[str, dict[str, Any]] = {}
        self.progress_broadcasts: dict[str, tuple[float, float | None]] = {}
        self.progress_logs: dict[str, tuple[float, float | None]] = {}
        self.last_log_broadcast = 0.0
        # worker 池(P2c):共享队列 + N 线程抽干 + 屏障 finalize。concurrency=1 = 旧单线程顺序。
        self.concurrency = 1
        self.max_retries = 0   # item 失败自动重试上限(F3;0 = 关闭 = 现行为)
        self.queue: "deque[WorkItem]" = deque()   # 线程安全 work-queue(append/popleft 原子)
        self.threads: list[threading.Thread] = []
        self.worker_lock = threading.Lock()   # 护 active_workers 计数 + pool_generation(finalize 屏障)
        self.active_workers = 0
        self.pool_generation = 0   # 每次 _spawn +1;worker 带出生代,过期代不碰新池计数/不抢新 item
        self.pause_gate = threading.Event()
        self.pause_gate.set()  # set = 放行;clear = 暂停
        # 「关闭」断点:运行时动态标 halt-after 的 stage_id 集(叠加 StageSpec.pause_after 静态声明)。
        # 空集 + 全 pause_after=False → 断点逻辑全程不触发,与现状字节等价(back-compat by tests)。
        self.breakpoints: set[str] = set()
        self.cancel_event = threading.Event()
        self.interrupt_event = threading.Event()
        self.stage_runner = StageRunner()
        self.claimed: dict[str, int] = {}                  # item_id → 起跑时 entity 版本(CAS 用)
        self.item_status: dict[str, EntityRunStatus] = {}  # item_id → 当前运行态
        # 显式占位身份只来自 prepare_start 实际新建。不从 payload/内容猜，
        # 并由 run manifest 耐久化，以便重启后仍能保持同一呈现语义。
        self.placeholder_item_ids: set[str] = set()
        self.retry_attempts: dict[str, int] = {}           # item_id → 已用自动重试次数(F3;手动 retry_failed 不看)
        # 这些 item 的下一个未完成 stage 可能已在旧进程/中断前开始过。
        # resume 对它们不是普通的「继续第一次执行」，而是潜在重放，必须经 retry_safe 闸门。
        self.replay_guard_items: set[str] = set()
        # pre-write canonical-history mismatch 只能由一个新 Context 恢复后显式重试。
        # live worker 不会把 item 放进这里；recover() 读取到安全的 reload marker
        # 才授予一次进程内许可。outcome-unknown marker 永远不会进入此集合。
        self.history_reload_ready_items: set[str] = set()


class PipelineSupervisor:
    """活的管线编排器。服务器常驻单例,由 bootstrap 创建并注入到 CommandRegistry。"""

    def __init__(
        self, *,
        doc: Any,
        cmd_reg: Any,
        op_log: Any,
        broker: Any,
        data_dir: Path,
        actor: Any,
        lock: threading.Lock,
        save_fn: Callable[[], None] | None = None,
        gate_registry: Any = None,
        stage_resolver: Callable[[dict], Any] | None = None,
        credential_context_getter: Callable[[], Any] | None = None,
        credential_context_factory: Callable[..., Any] | None = None,
        commit_coordinator: Any = None,
        allow_legacy_test_adapter: bool = False,
        lifecycle_guard: Callable[[], Any] | None = None,
    ) -> None:
        if commit_coordinator is None and not allow_legacy_test_adapter:
            raise ZeError(
                "commit_coordinator_required",
                "PipelineSupervisor requires CommitCoordinator; "
                "allow_legacy_test_adapter is test-only",
            )
        self._doc = doc
        self._cmd_reg = cmd_reg
        self._op_log = op_log
        self._broker = broker
        self._data_dir = Path(data_dir)
        self._actor = actor
        self._lock = lock                 # 与 server.py 的 dispatch+save 共享,串行化 doc 变更
        self._save_fn = save_fn
        # 资源闸门登记处(P2c-B,skill 级共享;None = 不限流)。stage 进资源前按 resource_scopes acquire。
        self._gate_registry = gate_registry
        # 重建钩子(P2c-C):mod 给 (run_manifest)->StagePlan|None,据持久化 manifest 重建可执行计划
        # (run callable 不可序列化故必须 mod 提供);None / 返 None → 该 run 不可重建、跳过。
        self._stage_resolver = stage_resolver
        self._credential_context_getter = credential_context_getter
        self._credential_context_factory = credential_context_factory
        # 生产路径唯一 canonical 提交入口。None 仅在 caller 显式开启 test adapter 时允许。
        self._commit_coordinator = commit_coordinator
        self._lifecycle_guard = lifecycle_guard or nullcontext
        self._runs: dict[str, _RunState] = {}
        # A bad durable manifest must not disappear into a ghost queued entity.  These views are
        # re-derived on every process start from the still-present run directory, expose only a
        # fixed code (never parser text/path/stack), and deliberately offer no unsafe retry action.
        self._recovery_failures: dict[str, dict[str, Any]] = {}
        # 实体级「已有成功产出」台账:任何 run 的 item 落 done 时记录 (entity_type, entity_id)→ts。
        # 用途:并发 run 对同一实体后写回失败(CAS 冲突)时,error 占位据此判定「已被别人的成功
        # 取代」→ 不再盖在卡片上(run 详情视图仍保留真实 error)。worker 线程写/SSE 线程读,
        # 单键赋值原子,与 item_status 同一并发模式,不另加锁。进程生命周期内不清理——与
        # _runs 同模式(每 item 一条 (str,str)→float,量级远小于 run 状态本身)。
        self._entity_done_at: dict[tuple[str, str], float] = {}

    @contextmanager
    def _credential_scope(self, st: _RunState) -> Iterator[None]:
        """Run stage code under the user credential context captured at start()."""
        factory = self._credential_context_factory
        if factory is None or st.credential_actor is None or st.credential_store is None:
            yield
            return
        with factory(
            st.credential_actor,
            st.credential_store,
            st.credential_policy_store,
            st.credential_tool_id,
        ):
            yield

    @contextmanager
    def _acquire_scopes(self, stage: Any) -> Iterator[None]:
        """进 stage 资源前,按声明的 resource_scopes 逐个 acquire 对应硬上限闸门。

        无 registry / 无 scopes / scope 未注册合同 → 不限流(优雅降级,acquire 在锁外阻塞等槽)。
        - **按 scope 字典序 acquire**:多 scope 时各 stage 取序一致 → 杜绝 AB-BA 死锁。
        - 容量类(CapacityGate)用 `stage.resource_costs[scope]`(缺省 1)做加权准入;槽位类忽略 cost。
        只 acquire 包住 stage.run(资源调用),不包提交(commit 走 self._lock,二者不嵌套)。
        """
        reg = self._gate_registry
        scopes = getattr(stage, "resource_scopes", ())
        if reg is None or not scopes:
            yield
            return
        costs = getattr(stage, "resource_costs", None) or {}
        known = set(reg.scopes())
        with ExitStack() as stack:
            for sc in sorted(scopes):
                if sc not in known:
                    continue   # 声明了 scope 但 mod 未注册合同 = 视为不限流
                gate = reg.get(sc)
                if gate.contract.kind == "capacity":
                    stack.enter_context(gate.acquire(cost=int(costs.get(sc, 1))))
                else:
                    stack.enter_context(gate.acquire())
            yield

    # ============================================================
    # 生命周期
    # ============================================================

    def validate_lifecycle(
        self,
        command_id: str,
        args: dict[str, Any],
    ) -> tuple[str, ...] | None:
        """生命周期命令的纯读预检，并冻结 retry 的精确 item scope。"""
        run_id = args.get("pipeline_id")
        if not isinstance(run_id, str) or not run_id:
            raise ZeError("validation_failed", "pipeline_id is required")
        if command_id == "pipeline.retry_failed":
            # Lookup, failed-phase snapshot and replay checks share the exact
            # same lock as the later action.  A concurrent run replacement can
            # therefore never change what ``None`` meant during preparation.
            with self._lock:
                st = self._runs.get(run_id)
                if st is None:
                    raise ZeError(
                        "pipeline_not_found",
                        f"pipeline run {run_id!r} not found",
                        context={"pipeline_id": run_id},
                    )
                retry_scope = self._resolve_retry_scope_locked(
                    st,
                    args.get("item_ids"),
                )
                wanted = set(retry_scope)
                self._require_replay_safe(st, wanted)
                if self._any_alive(st):
                    return retry_scope
                failed = self._failed_items(st, wanted)
                self._require_stage_replay_safe(
                    st,
                    [(item, self._resume_stage_index(st, item)) for item in failed],
                    trigger="retry_failed",
                )
                return retry_scope
        st = self._runs.get(run_id)
        if st is None:
            raise ZeError(
                "pipeline_not_found",
                f"pipeline run {run_id!r} not found",
                context={"pipeline_id": run_id},
            )

        if command_id in (
            "pipeline.rerun_step", "pipeline.run_from_step", "pipeline.set_breakpoint",
        ):
            stage_id = args.get("stage_id")
            if not isinstance(stage_id, str) or not stage_id:
                raise ZeError("validation_failed", "stage_id is required")
            try:
                st.stage_plan.index_of(stage_id)
            except ValueError as exc:
                raise ZeError(
                    "pipeline_stage_not_found",
                    f"pipeline stage {stage_id!r} not found",
                    context={"pipeline_id": run_id, "stage_id": stage_id},
                ) from exc

        if command_id in ("pipeline.rerun_step", "pipeline.run_from_step"):
            item_id = args.get("item_id")
            self._require_replay_safe(st, {item_id} if item_id else None)
            if not self._any_alive(st):
                start_idx = st.stage_plan.index_of(args["stage_id"])
                items = [
                    item for item in st.work_list.items
                    if item_id is None or item.item_id == item_id
                ]
                self._require_stage_replay_safe(
                    st,
                    [(item, start_idx) for item in items],
                    trigger=command_id.removeprefix("pipeline."),
                )
        elif command_id == "pipeline.resume" and not self._any_alive(st):
            self._require_resume_stage_replay_safe(st)
        return None

    def _require_replay_safe(
        self,
        st: _RunState,
        item_ids: set[str] | None = None,
    ) -> None:
        """不允许任何入口重放结果不确定的 mutation。"""
        unsafe: list[dict[str, str]] = []
        reload_required: list[dict[str, str]] = []
        for item in st.work_list.items:
            if item_ids is not None and item.item_id not in item_ids:
                continue
            status = st.item_status.get(item.item_id)
            error = status.error if status is not None else None
            if not error and self._item_phase(st, item) == "error":
                error = scan.read_json_file(
                    scan.error_marker_path(self._data_dir, st.run_id, item.item_id)
                )
            code = str((error or {}).get("code") or "")
            if code in (
                "commit_dirty", "commit_indeterminate",
                "pipeline_completion_repair_required", "commit_repair_required",
            ):
                unsafe.append({"item_id": item.item_id, "code": code})
            elif (
                code == "history_authority_mismatch"
                and item.item_id not in st.history_reload_ready_items
            ):
                reload_required.append({"item_id": item.item_id, "code": code})
        if unsafe:
            raise ZeError(
                "commit_repair_required",
                "pipeline replay is blocked until the uncertain commit is repaired",
                context={
                    "run_id": st.run_id,
                    "items": unsafe,
                    "repair_required": True,
                    "outcome_unknown": True,
                    "retry_safe": False,
                },
            )
        if reload_required:
            raise ZeError(
                "history_authority_mismatch",
                "pipeline replay requires a freshly reloaded project context",
                context={
                    "run_id": st.run_id,
                    "items": reload_required,
                    "reload_required": True,
                    "outcome_unknown": False,
                    "retry_safe": False,
                },
            )

    @staticmethod
    def _stage_declares_replay_safe(stage: Any) -> bool:
        """只有显式 ``retry_safe=False`` 才禁止重放。

        StageSpec 与现有 mod 的默认值一直是 True；对没有该字段的旧 resolver
        stage 也按 True 处理，避免升级后把纯本地旧管线整批禁用。新的外部/
        不幂等副作用 stage 必须显式声明 False。
        """
        return getattr(stage, "retry_safe", True) is not False

    def _resume_stage_index(self, st: _RunState, item: WorkItem) -> int:
        """返回 item 下次真正会进入的 stage，而不是笼统地检查整条 plan。"""
        if st.stage_plan.is_single:
            return 0
        return scan.completed_stage_count(item, self._data_dir, st.run_id, st.stage_plan)

    def _unsafe_stage_replays(
        self,
        st: _RunState,
        targets: list[tuple[WorkItem, int]],
    ) -> list[dict[str, Any]]:
        """列出本次执行路径的 unsafe suffix；已完成前缀不参与判定。"""
        unsafe: list[dict[str, Any]] = []
        for item, start_idx in targets:
            for idx in range(max(0, start_idx), len(st.stage_plan)):
                stage = st.stage_plan[idx]
                if self._stage_declares_replay_safe(stage):
                    continue
                unsafe.append({
                    "item_id": item.item_id,
                    "stage_id": stage.id,
                    "stage_index": idx,
                })
        return unsafe

    def _require_stage_replay_safe(
        self,
        st: _RunState,
        targets: list[tuple[WorkItem, int]],
        *,
        trigger: str,
    ) -> None:
        unsafe = self._unsafe_stage_replays(st, targets)
        if not unsafe:
            return
        raise ZeError(
            "pipeline_replay_unsafe",
            "pipeline replay includes a stage declared retry_safe=False",
            context={"run_id": st.run_id, "trigger": trigger, "stages": unsafe},
        )

    def _require_resume_stage_replay_safe(self, st: _RunState) -> None:
        """只检查确实可能重放的 recovered/interrupted item。

        普通 breakpoint pause 的后续 stage 尚未开始，它的 resume 仍是首次执行，
        不应因 retry_safe=False 被拦住。
        """
        guarded = [
            item for item in self._pending_items(st)
            if item.item_id in st.replay_guard_items
        ]
        self._require_stage_replay_safe(
            st,
            [(item, self._resume_stage_index(st, item)) for item in guarded],
            trigger="resume",
        )

    def _failed_items(
        self,
        st: _RunState,
        wanted: set[str] | None,
    ) -> list[WorkItem]:
        return [
            item for item in st.work_list.items
            if self._item_phase(st, item) == "error"
            and (wanted is None or item.item_id in wanted)
        ]

    def _resolve_retry_scope_locked(
        self,
        st: _RunState,
        item_ids: list[str] | None,
    ) -> tuple[str, ...]:
        """Freeze one exact retry scope while the Supervisor lock is held.

        ``None`` means "all items that are failed *now*" and is expanded to a
        sorted tuple immediately.  An explicit empty list never inherits that
        meaning.  This prevents a deferred lifecycle action from widening a
        durable request after the failed set changes.
        """
        if item_ids is None:
            requested: tuple[str, ...] | None = None
        else:
            if type(item_ids) is not list or any(
                type(item_id) is not str or not item_id
                for item_id in item_ids
            ):
                raise ZeError(
                    "pipeline_retry_scope_invalid",
                    "pipeline retry item_ids must be an exact list of non-empty strings",
                    context={"run_id": st.run_id},
                )
            if not item_ids:
                raise ZeError(
                    "pipeline_retry_scope_empty",
                    "pipeline retry scope is empty",
                    context={"run_id": st.run_id},
                )
            if len(set(item_ids)) != len(item_ids):
                raise ZeError(
                    "pipeline_retry_scope_invalid",
                    "pipeline retry item_ids must not contain duplicates",
                    context={"run_id": st.run_id},
                )
            requested = tuple(sorted(item_ids))

        failed_ids = tuple(sorted(
            item.item_id
            for item in st.work_list.items
            if self._item_phase(st, item) == "error"
        ))
        if requested is None:
            resolved = failed_ids
        else:
            failed_set = set(failed_ids)
            not_failed = [item_id for item_id in requested if item_id not in failed_set]
            if not_failed:
                raise ZeError(
                    "pipeline_retry_scope_invalid",
                    "pipeline retry scope contains an item that is not failed",
                    context={"run_id": st.run_id, "item_ids": not_failed},
                )
            resolved = requested
        if not resolved:
            raise ZeError(
                "pipeline_retry_scope_empty",
                "pipeline retry scope contains no failed items",
                context={"run_id": st.run_id},
            )
        return resolved

    def start(self, *, work_list: WorkList, item_stage: StageSpec | None = None,
              stage_plan: StagePlan | None = None, trace_id: str = "",
              concurrency: int = 1, max_retries: int = 0, pipeline_id: str = "",
              start_command_id: str = "") -> _RunState:
        """直接起一个 run；Document mutation 先提交，运行态副作用后执行。

        mod 的命令 handler 不应在 producer 中调用本方法，而应调用
        :meth:`prepare_start`，把 start Operation 与占位 Operations 合成同一笔
        ``CommandMutation``。本方法保留给 framework/叶子直接调用。
        """
        if self._commit_coordinator is not None:
            prepared: dict[str, Any] = {}

            def _produce_start() -> CommandMutation:
                mutation, state = self.prepare_start(
                    work_list=work_list,
                    item_stage=item_stage,
                    stage_plan=stage_plan,
                    trace_id=trace_id,
                    concurrency=concurrency,
                    max_retries=max_retries,
                    pipeline_id=pipeline_id,
                    start_command_id=start_command_id,
                )
                prepared["state"] = state
                return mutation

            execution = self._commit_coordinator.execute(
                _produce_start,
                trace_id=trace_id or work_list.run_id,
            )
            st = prepared["state"]
            if not execution.is_committed:
                status = execution.receipt.status.value if execution.receipt else "aborted"
                raise ZeError(
                    f"commit_{status}",
                    f"pipeline start commit ended as {status}",
                    context={
                        "receipt": execution.receipt.to_dict() if execution.receipt else None,
                    },
                )
            if not execution.actions or any(
                action.status.value != "succeeded" for action in execution.actions
            ):
                raise ZeError(
                    "pipeline_start_action_failed",
                    "pipeline start was committed but its after-commit action did not complete",
                    context={"actions": [action.to_dict() for action in execution.actions]},
                )
            return st

        # 明名的 legacy test adapter：老叶子未注入 coordinator 时仍保证
        # save→append→publish→activate；production bootstrap 永远不会走这里。
        mutation, st = self.prepare_start(
            work_list=work_list,
            item_stage=item_stage,
            stage_plan=stage_plan,
            trace_id=trace_id,
            concurrency=concurrency,
            max_retries=max_retries,
            pipeline_id=pipeline_id,
            start_command_id=start_command_id,
        )
        self._commit_mutation(
            lambda: self._mutation_product(list(mutation.operations)),
            trace_id=st.trace_id,
        )
        for action in mutation.after_commit:
            action.run("legacy-test-commit")
        return st

    def prepare_start(
        self,
        *,
        work_list: WorkList,
        item_stage: StageSpec | None = None,
        stage_plan: StagePlan | None = None,
        trace_id: str = "",
        concurrency: int = 1,
        max_retries: int = 0,
        pipeline_id: str = "",
        start_command_id: str = "",
        parameters: dict[str, Any] | None = None,
        start_operation: Any | None = None,
    ) -> tuple[CommandMutation, _RunState]:
        """准备占位 mutation；不登记 run、不写 manifest、不广播、不起线程。

        单 stage(旧):传 item_stage → 内部包成 StagePlan.from_single,走原行为(产物判 done)。
        多 stage(P2b):传 stage_plan → 每 item 顺序过各 stage,stage 标记派生 done、续跑安全。
        二者必传其一、不可同传。

        concurrency(P2c):并发 worker 数(item 间并行 fan-out);默认 1 = 旧单线程顺序、行为不变。
        max_retries(F3):item 失败自动重试上限;默认 0 = 不自动重试(现行为),超限才落 error。
        """
        if stage_plan is None and item_stage is None:
            raise ZeError("pipeline_start_requires_stage", "需传 item_stage 或 stage_plan 其一")
        if stage_plan is not None and item_stage is not None:
            raise ZeError("pipeline_start_ambiguous_stage", "item_stage 与 stage_plan 不可同传")
        protected_document = bool(self._doc.mutation_policies)
        if protected_document and start_operation is not None:
            raise ZeError(
                "pipeline_start_operation_forbidden",
                "protected Pipeline Start must be issued by the sealed framework factory",
                context={"run_id": work_list.run_id},
            )
        root_snapshot = None
        try:
            root_snapshot = _active_command_root_snapshot(
                registry=self._cmd_reg,
                document=self._doc,
            )
        except ZeError as exc:
            if (
                protected_document
                or exc.code != "pipeline_start_authority_required"
                or exc.context.get("reason") != "active_root_dispatch_missing"
            ):
                raise
        if protected_document:
            if root_snapshot is None or not root_snapshot.protected:
                raise ZeError(
                    "pipeline_start_authority_required",
                    "protected Pipeline Start requires an active policy-authorized business command",
                    context={"run_id": work_list.run_id},
                )
            if start_command_id and start_command_id != root_snapshot.command_id:
                raise ZeError(
                    "pipeline_start_identity_mismatch",
                    "Pipeline start_command_id differs from the active business root",
                    context={
                        "run_id": work_list.run_id,
                        "start_command_id": start_command_id,
                        "root_command_id": root_snapshot.command_id,
                    },
                )
            start_command_id = root_snapshot.command_id
            if (
                type(pipeline_id) is not str
                or not pipeline_id
                or pipeline_id in (".", "..")
                or any(character in pipeline_id for character in ("/", "\\", "\x00"))
            ):
                raise ZeError(
                    "pipeline_start_identity_mismatch",
                    "protected Pipeline Start requires an explicit safe pipeline_id",
                    context={"run_id": work_list.run_id},
                )
            if type(concurrency) is not int or not 1 <= concurrency <= 64:
                raise ZeError(
                    "pipeline_start_identity_mismatch",
                    "Pipeline concurrency must be an exact integer from 1 to 64",
                    context={"run_id": work_list.run_id},
                )
            if type(max_retries) is not int or not 0 <= max_retries <= 100:
                raise ZeError(
                    "pipeline_start_identity_mismatch",
                    "Pipeline max_retries must be an exact integer from 0 to 100",
                    context={"run_id": work_list.run_id},
                )
            if parameters is not None and type(parameters) is not dict:
                raise ZeError(
                    "pipeline_start_identity_mismatch",
                    "Pipeline execution parameters must be an exact object",
                    context={"run_id": work_list.run_id},
                )
        if work_list.run_id in self._runs:
            raise ZeError(
                "pipeline_run_exists",
                f"pipeline run {work_list.run_id!r} already exists",
                context={"run_id": work_list.run_id},
            )
        plan = stage_plan if stage_plan is not None else StagePlan.from_single(item_stage)
        cred_ctx = self._credential_context_getter() if self._credential_context_getter else None
        st = _RunState(
            work_list=work_list, stage_plan=plan, trace_id=trace_id or work_list.run_id,
            pipeline_id=pipeline_id, start_command_id=start_command_id,
            credential_actor=getattr(cred_ctx, "actor", None),
            credential_store=getattr(cred_ctx, "store", None),
            credential_policy_store=getattr(cred_ctx, "policy_store", None),
            credential_tool_id=getattr(cred_ctx, "tool_id", ""),
        )
        st.concurrency = max(1, int(concurrency))
        st.max_retries = max(0, int(max_retries))

        placeholder_operations: list[tuple[WorkItem, Operation]] = []
        if root_snapshot is not None:
            placeholder_actor = Actor(**root_snapshot.actor)
            placeholder_source = root_snapshot.source
        else:
            placeholder_actor = self._actor
            placeholder_source = "system"

        with self._lock:
            seeded_ops: list[Operation] = []
            for item in work_list.items:
                payload = item.payload_copy()
                ent = self._doc.find(item.entity_type, item.entity_id)
                if ent is None:
                    seeded_operation = self._cmd_reg.dispatch(
                        "entity.add",
                        {
                            "type": item.entity_type,
                            "id": item.entity_id,
                            "name": payload.get("name", "生成中…"),
                            "data": dict(payload.get("seed_data") or {}),
                        },
                        actor=placeholder_actor,
                        source=placeholder_source,
                        doc=self._doc,
                        op_log=None,
                    )
                    if type(seeded_operation) is not Operation:
                        raise ZeError(
                            "pipeline_start_prefix_incomplete",
                            "placeholder creation did not return one exact Operation",
                            context={"item_id": item.item_id},
                        )
                    seeded_ops.append(seeded_operation)
                    placeholder_operations.append((item, seeded_operation))
                    # dispatch 成功就是本次 prepare 的新建事实；不依赖某个
                    # CommandRegistry adapter 是否立即将 Operation 投影回 doc。
                    st.placeholder_item_ids.add(item.item_id)
                    ent = self._doc.find(item.entity_type, item.entity_id)
                if not _recovery_identity_matches(item, ent):
                    raise ZeError(
                        "pipeline_recovery_identity_mismatch",
                        "pipeline recovery identity does not match the durable entity",
                        context={"item_id": item.item_id},
                    )
                st.claimed[item.item_id] = ent.version if ent else 1
                st.item_status[item.item_id] = EntityRunStatus(
                    entity_type=item.entity_type, entity_id=item.entity_id,
                    phase="pending", run_id=st.run_id, updated_at=time.time(),
                    placeholder=item.item_id in st.placeholder_item_ids,
                )

        operations: list[Operation] = []
        if protected_document:
            actor_payload = root_snapshot.actor
            for _item, placeholder_operation in placeholder_operations:
                if (
                    placeholder_operation.document_id != self._doc.id
                    or placeholder_operation.source != root_snapshot.source
                    or placeholder_operation.actor != actor_payload
                ):
                    raise ZeError(
                        "pipeline_start_identity_mismatch",
                        "placeholder Operation identity differs from the active business root",
                        context={"run_id": st.run_id},
                    )
            policies = self._doc.mutation_policies
            targets: list[dict[str, Any]] = []
            for item in sorted(
                work_list.items,
                key=lambda value: (value.entity_type, value.entity_id),
            ):
                policy = policies.get(item.entity_type)
                if type(policy) is not dict:
                    raise ZeError(
                        "pipeline_start_authority_required",
                        "Pipeline target has no protected mutation policy",
                        context={
                            "run_id": st.run_id,
                            "entity_type": item.entity_type,
                        },
                    )
                fields = sorted(
                    field_name
                    for field_name, command_ids in policy.get("fields", {}).items()
                    if start_command_id in command_ids
                )
                lifecycle = sorted(
                    action
                    for action, command_ids in policy.get("lifecycle", {}).items()
                    if start_command_id in command_ids
                )
                targets.append({
                    "entity_type": item.entity_type,
                    "entity_id": item.entity_id,
                    "fields": fields,
                    "lifecycle": lifecycle,
                })
            start_operation = issue_sealed_pipeline_start_operation(
                document=self._doc,
                run_id=st.run_id,
                pipeline_id=st.pipeline_id,
                start_command_id=start_command_id,
                initial_operations=tuple(seeded_ops),
                work_list=work_list.to_dict(),
                stages=plan.to_meta(),
                placeholder_bindings=[
                    {
                        "item_id": item.item_id,
                        "entity_type": item.entity_type,
                        "entity_id": item.entity_id,
                        "operation_id": operation.id,
                    }
                    for item, operation in placeholder_operations
                ],
                concurrency=st.concurrency,
                max_retries=st.max_retries,
                parameters={} if parameters is None else parameters,
                worker_actor=self._actor.to_dict(),
                worker_source="ai",
                targets=targets,
                creation_scopes=None,
                trace_id=st.trace_id,
                _root_authority=root_snapshot,
            )
        elif start_operation is None:
            if root_snapshot is not None:
                legacy_start_command_id = root_snapshot.command_id
                legacy_start_source = root_snapshot.source
                legacy_start_actor = root_snapshot.actor
                legacy_document_id = root_snapshot.document_id
            else:
                legacy_start_command_id = start_command_id or "pipeline.start"
                legacy_start_source = "system"
                legacy_start_actor = self._actor.to_dict()
                legacy_document_id = self._doc.id
            start_operation = new_operation(
                command_id=legacy_start_command_id,
                source=legacy_start_source,
                actor=legacy_start_actor,
                patches=[],
                reverse_patches=[],
                trace_id=st.trace_id,
                kind="stage",
                document_id=legacy_document_id,
            )
        if type(start_operation) is not Operation:
            raise ZeError(
                "pipeline_start_prefix_incomplete",
                "Pipeline Start must be one exact Operation",
                context={"run_id": st.run_id},
            )
        operations.append(start_operation)
        operations.extend(seeded_ops)

        # Batch 3 deliberately stops protected v2 starts at durable authority
        # attestation.  Registering the run, writing an executable manifest, or
        # spawning a worker here would let external effects begin before the
        # Batch 5 grant/lifecycle registry exists.  Legacy/unprotected starts
        # retain their existing after-commit activation path.
        actions: tuple[DeferredAction, ...] = ()
        if not protected_document:
            actions = (DeferredAction(
                id=f"pipeline.start:{st.run_id}",
                name="pipeline.start",
                run=lambda _commit_id: self._activate_prepared_start(st),
            ),)
        return CommandMutation(tuple(operations), actions), st

    def _activate_prepared_start(self, st: _RunState) -> None:
        """提交后登记运行态、写 manifest、广播并起 worker；失败不会伪装成提交失败。"""
        if st.run_id in self._runs:
            if self._runs[st.run_id] is st:
                return
            raise ZeError(
                "pipeline_run_exists",
                f"pipeline run {st.run_id!r} already exists",
                context={"run_id": st.run_id},
            )
        self._recovery_failures.pop(st.run_id, None)
        self._write_manifest(st)
        self._log(st, "info", "pipeline started", source="pipeline")
        self._runs[st.run_id] = st
        self._broadcast_entity(st, st.work_list.items)
        self._broadcast_pipeline()
        self._spawn(st, st.work_list.items)

    def recover(self) -> list[str]:
        """进程重启后扫 .pipelines/*/run.json 重建未在内存的 run(P2c-C)。

        run callable 不可序列化 → 用 mod 注入的 `stage_resolver(manifest)->StagePlan|None` 重建
        可执行计划;无 resolver / 返 None / manifest 损坏 → 跳过该 run(不强行注册半残)。
        item 相位 + 终态从磁盘派生;有 pending 的标 paused(等 resume 续跑,复用既有续跑机制)。
        返回重建的 run_id 列表。bootstrap 每 project 调一次(不自动跑,等用户/系统 resume)。
        """
        pdir = self._data_dir / scan.PIPELINES_DIR
        previous_failures = self._recovery_failures
        if not pdir.exists() or self._stage_resolver is None:
            if previous_failures:
                with self._lock:
                    self._recovery_failures = {}
                self._broadcast_pipeline()
            return []
        recovered: list[str] = []
        next_failures: dict[str, dict[str, Any]] = {}

        def remember_failure(run_id: str, code: str) -> None:
            prior = previous_failures.get(run_id)
            if prior is not None and prior.get("recovery_error") == {"code": code}:
                next_failures[run_id] = prior
            else:
                next_failures[run_id] = self._recovery_failure_view(run_id, code)

        for rd in sorted(pdir.iterdir()):
            mp = rd / "run.json"
            # rd.name 进 scan.run_dir 路径 → 拒绝可疑名(分隔符/隐藏),防越界
            if (rd.is_symlink() or not rd.is_dir()
                    or rd.name.startswith(".") or "/" in rd.name or "\\" in rd.name):
                continue
            # A live in-memory run remains authoritative for this process.  Re-reading a later
            # damaged disk manifest must not append a second snapshot with the same public id.
            if rd.name in self._runs:
                continue
            try:
                rid = self._recover_one(rd, mp)
            except _RecoveryRejected as exc:
                remember_failure(rd.name, exc.code)
                rid = None
            except Exception:
                remember_failure(rd.name, "pipeline_manifest_invalid")
                rid = None   # 单个 run 损坏不拖累其它 run，但必须留下可观察的 repair 状态。
            if rid is not None:
                recovered.append(rid)
        failures_changed = next_failures != previous_failures
        with self._lock:
            self._recovery_failures = next_failures
        if recovered or failures_changed:
            self._broadcast_pipeline()
        return recovered

    def _recover_one(self, rd: Path, mp: Path) -> str | None:
        """重建单个 run(供 recover 调,异常由 caller 兜底跳过)。成功返 run_id、否则 None。"""
        manifest = _read_recovery_manifest(rd, mp)
        # run_id 是路径/注册/receipt 的共同身份。缺失或与目录不等不能由框架猜回，否则
        # 被删/换的 manifest 身份会在 resolver 前被静默修成合法值。
        if type(manifest) is not dict or manifest.get("run_id") != rd.name:
            raise _RecoveryRejected("pipeline_run_identity_mismatch")
        run_id = manifest["run_id"]
        if run_id in self._runs:
            return None   # 本进程已起的 run,不重复
        plan = self._stage_resolver(manifest)
        if plan is None:
            raise _RecoveryRejected("pipeline_recovery_unrecognized")
        work_list = WorkList.from_dict(manifest)
        st = _RunState(
            work_list=work_list, stage_plan=plan, trace_id=run_id,
            pipeline_id=str(manifest.get("pipeline_id") or run_id),
            start_command_id=str(manifest.get("start_command_id") or ""),
        )
        st.concurrency = max(1, int(manifest.get("concurrency", 1)))
        st.max_retries = max(0, int(manifest.get("max_retries", 0)))
        # 旧 manifest 没有该键时 fail-open 为普通实体。坏 shape / 未知 item id
        # 同样不得凭猜测升格为占位实体。
        raw_placeholder_ids = manifest.get("placeholder_item_ids")
        if isinstance(raw_placeholder_ids, list):
            known_item_ids = {item.item_id for item in work_list.items}
            st.placeholder_item_ids = {
                item_id for item_id in raw_placeholder_ids
                if type(item_id) is str and item_id in known_item_ids
            }
        # 全程持锁:disk 派生相位/计数 + 状态 + 注册 _runs 一致(防 recover 若被并发调用时半注册)
        with self._lock:
            # run.json 可被损坏或篡改。resolver 只证明执行声明的结构是它认识的；若 item
            # 进一步声明 recovery_identity_fields，还必须把 manifest seed 与已经提交的
            # Document entity 逐字段合取。任何不一致都在修复 marker/启动 worker 前拒绝，
            # 避免用另一份输入继续执行、却把结果挂在原 provenance 上。
            if any(
                not _recovery_identity_matches(
                    item, self._doc.find(item.entity_type, item.entity_id),
                )
                for item in work_list.items
            ):
                raise _RecoveryRejected("pipeline_recovery_identity_mismatch")
            for item in work_list.items:
                # commit checkpoint 已存在 = 业务 commit 明确成功；先幂等补 promote/done，
                # 再派生相位。没有 checkpoint 的 commit→checkpoint 崩溃窗保持 pending，
                # 由 replay_guard 对 retry_safe=False fail-closed。
                if plan.is_single:
                    completion_repaired = scan.repair_item_completion(
                        item, self._data_dir, run_id, plan[0].id,
                    )
                else:
                    for stage_id in plan.stage_ids:
                        scan.repair_stage_completion(item, self._data_dir, run_id, stage_id)
                    completion_repaired = scan.stage_item_done(
                        item, self._data_dir, run_id, plan,
                    )
                if completion_repaired:
                    persisted_error = scan.read_json_file(
                        scan.error_marker_path(self._data_dir, run_id, item.item_id),
                    )
                    if persisted_error.get("code") == "pipeline_completion_repair_required":
                        self._clear_error_marker(run_id, item.item_id)
                phase = self._item_phase(st, item)
                ent = self._doc.find(item.entity_type, item.entity_id)
                # 占位 entity 正常在 document.json 持久存在;缺失(崩在建占位前)→ claimed=1,
                # 续跑时 entity.update 命不中 → CAS/缺失报错、item 落 error(安全失败,不误写)。
                st.claimed[item.item_id] = ent.version if ent else 1
                # error 项带上磁盘 marker 内容:占位有失败详情,且 supersede 按
                # error.code 判(CAS 类才掩蔽),重启后不丢判据。
                err = None
                if phase == "error":
                    err = scan.read_json_file(
                        scan.error_marker_path(self._data_dir, run_id, item.item_id))
                    if self._is_history_reload_marker(err):
                        # A new supervisor/Context is the reload boundary.  The
                        # pre-write marker proves no durable mutation is in
                        # doubt, so one explicit retry may now proceed.  The
                        # outcome-unknown repair marker never reaches this set.
                        st.history_reload_ready_items.add(item.item_id)
                st.item_status[item.item_id] = EntityRunStatus(
                    entity_type=item.entity_type, entity_id=item.entity_id,
                    phase=phase, progress=1.0 if phase == "done" else None,
                    run_id=run_id, updated_at=time.time(), error=err,
                    placeholder=item.item_id in st.placeholder_item_ids,
                )
                if phase == "done":   # 重启后 supersede 台账从磁盘相位重建,不丢判据
                    self._entity_done_at[(item.entity_type, item.entity_id)] = time.time()
                elif phase == "pending":
                    # 没有 done/error marker 无法区分「从未开始」与「旧进程做了
                    # 外部副作用后崩溃」；恢复后继续必须按潜在重放守住。
                    st.replay_guard_items.add(item.item_id)
            counts = self._counts(st)
            if counts["total"] and counts["done"] == counts["total"]:
                st.status = "success"
            elif counts["error"] > 0 and counts["pending"] == 0:
                st.status = "error"
            else:
                st.status = "paused"     # 还有 pending → 暂停态,resume 续跑
                st.pause_gate.clear()
            self._runs[run_id] = st
        return run_id

    def _recovery_failure_view(self, run_id: str, code: str) -> dict[str, Any]:
        """Expose one safe, non-actionable pipeline view for a durable run needing repair."""
        return {
            "id": run_id,
            "pipeline_id": run_id,
            "start_command_id": "",
            "name": run_id,
            "stage": "recovery_required",
            "progress": None,
            "status": "error",
            "started_at": time.time(),
            "ended_at": None,
            "log_lines": ["recovery_required"],
            "can_pause": False,
            "can_interrupt": False,
            "can_resume": False,
            "can_cancel": False,
            "failed_count": 0,
            "detail_version": 1,
            "log_events": [],
            "active_items": [],
            "artifacts": [],
            "step_states": [],
            "breakpoints": [],
            "stage_count": 0,
            "recovery_required": True,
            "recovery_error": {"code": code},
        }

    def pause(self, run_id: str) -> None:
        st = self._runs.get(run_id)
        if not st:
            return
        st.pause_gate.clear()
        if st.status == "running":
            st.status = "paused"
            self._log(st, "info", "pipeline paused", source="pipeline")
        self._broadcast_pipeline()

    def resume(self, run_id: str) -> None:
        st = self._runs.get(run_id)
        if not st:
            return
        # 全程持锁:pause_gate.set 与判活/起线程原子,杜绝「set 后 worker 抢先 finalize、
        # 判活才看到死、再对已终态 run 重起一轮」的 TOCTOU 双开。
        with self._lock:
            if not self._any_alive(st):
                # 在改 status/gate、清事件或起 worker 前拦截；被拒绝时 run
                # 仍完整停在 paused/interrupted，不伪造已 resume 的状态。
                self._require_resume_stage_replay_safe(st)
            st.interrupt_event.clear()
            st.cancel_event.clear()
            st.pause_gate.set()
            if st.status in ("paused", "interrupted", "interrupting"):
                st.status = "running"
                self._log(st, "info", "pipeline resumed", source="pipeline")
            # 已终态(success/error/cancelled)不重起;否则池退出且有 pending → 续跑一轮
            if st.status not in ("running", "cancelling") or self._any_alive(st):
                self._broadcast_pipeline()
                return
            pending = self._pending_items(st)
            if pending:
                self._spawn(st, pending)
                st.replay_guard_items.difference_update(item.item_id for item in pending)
        self._broadcast_pipeline()

    def cancel(self, run_id: str) -> None:
        st = self._runs.get(run_id)
        if not st:
            return
        st.cancel_event.set()
        st.pause_gate.set()  # 解除暂停阻塞,让 worker 看到 cancel
        for tid in st.stage_runner.active():
            st.stage_runner.cancel(tid)
        if st.status in ("running", "paused", "interrupting", "interrupted"):
            st.status = "cancelling"
            self._log(st, "warn", "pipeline cancelling", source="pipeline")
        self._broadcast_pipeline()

    def shutdown(self, *, join_timeout: float = 5.0) -> bool:
        """请求全部 worker 停止，并返回是否已经可靠 quiesced。

        给「删除 / 重置 project」前调用:让背景 worker 不会在项目目录被 rmtree 之后又走
        `_commit_stage → _maybe_save`(取 ctx.lock 写 document.json)把目录重建出来(orphan)。
        对每个 run 发 cancel(置 cancel_event + 解暂停 + 取消在跑 stage),再逐个 join worker
        线程。**绝不能在持 ctx.lock 时调** —— worker 的 `with self._lock: _maybe_save` 会与之死锁;
        join 在锁外做,让 worker 跑完最后一次 save 自然退出。bounded:个别卡在不可中断长 stage 的
        worker 即使是 daemon 也不能在超时后假装安全：返回 False，由拓扑层保持 live 不动并报
        project_busy。幂等:无 run / 线程已退则返回 True。
        """
        run_ids = list(self._runs.keys())
        for run_id in run_ids:
            self.cancel(run_id)
        deadline = time.time() + max(0.0, join_timeout)
        for run_id in run_ids:
            st = self._runs.get(run_id)
            if not st:
                continue
            for t in list(st.threads):
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                if t.is_alive():
                    t.join(timeout=remaining)
        return not any(
            thread.is_alive()
            for run_id in run_ids
            for thread in list(self._runs[run_id].threads if run_id in self._runs else ())
        )

    def interrupt(self, run_id: str) -> None:
        """请求中断当前 stage;已完成 step 保留,未完成 step 可 resume。"""
        st = self._runs.get(run_id)
        if not st:
            return
        st.interrupt_event.set()
        st.pause_gate.set()
        for tid in st.stage_runner.active():
            st.stage_runner.interrupt(tid)
        if st.status in ("running", "paused"):
            st.status = "interrupting"
            self._log(st, "warn", "pipeline interrupt requested", source="pipeline")
        self._broadcast_pipeline()

    def set_breakpoint(self, run_id: str, *, stage_id: str, halt: bool) -> None:
        """「关闭/开启」某 stage 的 halt-after 断点(运行时动态切;halt=False 即开启=移除断点)。
        只改断点集,不动当前执行;下次 item 跑到该 stage 边界时生效。继续 = 调 resume()。"""
        st = self._runs.get(run_id)
        if not st:
            return
        with self._lock:
            if halt:
                st.breakpoints.add(stage_id)
            else:
                st.breakpoints.discard(stage_id)
        self._log(st, "info", f"breakpoint {'set' if halt else 'cleared'}", source="pipeline",
                  stage_id=stage_id)
        self._broadcast_pipeline()

    def retry_failed(self, run_id: str, item_ids: list[str] | None = None) -> None:
        """只重跑 error 标记的 item(可选限定 item_ids)。复用 worker,清错误标记 + 重抢版本。

        持锁做"判活+决定重跑+起线程",避免两个并发请求都过 is_alive() 闸而双开 worker。
        """
        with self._lock:
            st = self._runs.get(run_id)
            if not st:
                return
            retry_scope = self._resolve_retry_scope_locked(st, item_ids)
            wanted = set(retry_scope)
            self._require_replay_safe(st, wanted)
            if self._any_alive(st):
                return
            failed = self._failed_items(st, wanted)
            if tuple(sorted(item.item_id for item in failed)) != retry_scope:
                raise ZeError(
                    "pipeline_retry_scope_invalid",
                    "pipeline retry scope changed before execution",
                    context={"run_id": st.run_id},
                )
            self._require_stage_replay_safe(
                st,
                [(item, self._resume_stage_index(st, item)) for item in failed],
                trigger="retry_failed",
            )
            self._log(st, "info", f"retrying {len(failed)} failed item(s)", source="pipeline")
            for it in failed:
                st.history_reload_ready_items.discard(it.item_id)
                self._clear_error_marker(st.run_id, it.item_id)
                ent = self._doc.find(it.entity_type, it.entity_id)
                st.claimed[it.item_id] = ent.version if ent else 1
                st.item_status[it.item_id] = EntityRunStatus(
                    entity_type=it.entity_type, entity_id=it.entity_id,
                    phase="pending", run_id=st.run_id, updated_at=time.time(),
                    placeholder=it.item_id in st.placeholder_item_ids,
                )
            st.cancel_event.clear()
            st.interrupt_event.clear()
            st.pause_gate.set()
            st.status = "running"
            self._spawn(st, failed)   # 起线程仍在锁内:杜绝双开
        self._broadcast_entity(st, failed)
        self._broadcast_pipeline()

    def rerun_step(self, run_id: str, *, stage_id: str,
                   item_id: str | None = None) -> None:
        """重跑指定 stage,并把该 stage 及下游标 stale。"""
        self._run_from_step(run_id, stage_id=stage_id, item_id=item_id, mode="rerun_step")

    def run_from_step(self, run_id: str, *, stage_id: str,
                      item_id: str | None = None) -> None:
        """从指定 stage 跑到最后,上游 snapshot/产物保持不变。"""
        self._run_from_step(run_id, stage_id=stage_id, item_id=item_id, mode="run_from_step")

    def _run_from_step(self, run_id: str, *, stage_id: str,
                       item_id: str | None, mode: str) -> None:
        st = self._runs.get(run_id)
        if not st:
            return
        with self._lock:
            self._require_replay_safe(st, {item_id} if item_id else None)
            if self._any_alive(st):
                return
            try:
                st.stage_plan.index_of(stage_id)
            except ValueError:
                self._log(st, "error", f"unknown stage: {stage_id}", source="pipeline",
                          stage_id=stage_id, code="unknown_stage")
                self._broadcast_pipeline()
                return
            items = [it for it in st.work_list.items if item_id is None or it.item_id == item_id]
            if not items:
                return
            start_idx = st.stage_plan.index_of(stage_id)
            self._require_stage_replay_safe(
                st,
                [(item, start_idx) for item in items],
                trigger=mode,
            )
            for it in items:
                st.history_reload_ready_items.discard(it.item_id)
                scan.clear_stage_markers_from(self._data_dir, st.run_id, it.item_id,
                                              st.stage_plan, stage_id)
                self._clear_error_marker(st.run_id, it.item_id)
                ent = self._doc.find(it.entity_type, it.entity_id)
                st.claimed[it.item_id] = ent.version if ent else 1
                self._set_item_staged(st, it, "pending",
                                      stage_index=st.stage_plan.index_of(stage_id))
                self._mark_step_stale(st, it, stage_id)
            st.cancel_event.clear()
            st.interrupt_event.clear()
            st.pause_gate.set()
            st.status = "running"
            self._log(st, "info", f"{mode}: {stage_id} ({len(items)} item(s))",
                      source="pipeline", stage_id=stage_id)
            self._spawn(st, items)
        self._broadcast_entity(st, items)
        self._broadcast_pipeline()

    # ============================================================
    # 快照(SSE 连接 / 重连时拉,从内存 + 磁盘派生)
    # ============================================================

    def snapshot(self) -> list[dict[str, Any]]:
        """全部 run 的 PipelineRun 形态视图(footer pipeline 面板)。

        list() 拷贝防与 worker 线程并发改 _runs 时迭代报错(SSE 连接线程也会调本方法)。
        """
        return [
            *[self._run_view(st) for st in list(self._runs.values())],
            *[copy.deepcopy(view) for view in list(self._recovery_failures.values())],
        ]

    def entity_status_snapshot(self) -> list[dict[str, Any]]:
        """全部 run 里 pending/running/error 的 item 运行态(done 不需占位,省略)。

        error 项若已被成功产出取代(_error_superseded)→ 跳过:不让「输掉 CAS 的
        重复生成」的红色错误盖在一张其实已经生成好的卡上。
        """
        out: list[dict[str, Any]] = []
        for st in list(self._runs.values()):
            for s in list(st.item_status.values()):
                if s.phase == "done":
                    continue
                if s.phase == "error" and self._error_superseded(s):
                    continue
                out.append(s.to_dict())
        return out

    def _error_superseded(self, s: EntityRunStatus) -> bool:
        """error 占位是否已被成功产出取代。只掩蔽 CAS 冲突类失败(其语义本就是
        「写回时别人已经写过了」),且该实体确有成功产出;其它失败(生图真错/超时)
        永远可见 —— 实体曾成功过不代表这次新失败不需要用户知道。"""
        if (s.error or {}).get("code") != "cas_version_mismatch":
            return False
        return (s.entity_type, s.entity_id) in self._entity_done_at

    def active_entity_ids(self) -> set[tuple[str, str]]:
        """活跃(还会推进)run 里 pending/running item 的 (entity_type, entity_id) 集合。

        供圈选类命令(批量生成/重生成)排除「已在生成中」的实体:同一实体被并发 run
        重复圈进,后写回者必然 CAS 冲突且白花一次生成。终态与 interrupted run 不算
        (interrupted ≈ 用户放弃,重圈另开新 run 更符合意图;万一 resume 撞版本,由
        CAS 安全失败 + supersede 占位兜底)。error item 不算:瞬时错误的自动重试会重抢
        版本;CAS 冲突错误刻意不自动重试(用户编辑赢),停在 error 等手动 retry_failed
        (手动路径重抢版本=用户明示要 AI 结果)——两种情况都无需把 error 计入活跃集。
        """
        active_status = ("running", "paused", "interrupting", "cancelling")
        out: set[tuple[str, str]] = set()
        for st in list(self._runs.values()):
            if st.status not in active_status:
                continue
            for s in list(st.item_status.values()):
                if s.phase in ("pending", "running"):
                    out.add((s.entity_type, s.entity_id))
        return out

    # ============================================================
    # worker
    # ============================================================

    def _spawn(self, st: _RunState, items: list[WorkItem]) -> None:
        """装载共享队列 + 起 N=concurrency 个 worker(默认 1 = 旧单线程顺序)。

        N 不超过 item 数(空 list 仍起 1 个让它走 finalize)。同一 run 内的 worker 共享
        队列(work-stealing 负载均衡)、pause_gate / cancel_event / stage_runner。
        """
        # 清掉上一轮残留(cancel 时 worker 可能未抽干)→ 本轮队列只含 items。
        st.queue.clear()
        st.queue.extend(items)
        n = max(1, min(st.concurrency, len(items) or 1))
        # 代号 + 计数在 worker_lock 内一起立(早退 worker 在 start 前不会先减):
        # 上一代残留 worker(若有)带旧 gen,_on_worker_done 会按 gen 判出而不碰本代计数。
        with st.worker_lock:
            st.pool_generation += 1
            gen = st.pool_generation
            st.active_workers = n
        st.threads = []
        for _ in range(n):
            t = threading.Thread(target=self._run_worker, args=(st, gen), daemon=True)
            st.threads.append(t)
            t.start()

    def _any_alive(self, st: _RunState) -> bool:
        return any(t.is_alive() for t in st.threads)

    def _run_worker(self, st: _RunState, gen: int) -> None:
        """worker:从共享队列拉 item 逐个处理;单/多 stage 各走对应 per-item 处理器。

        concurrency=1 时单 worker FIFO 抽干 == 旧「for item in items」逐字等价(同序、同
        pause/cancel 断点)。N 个 worker 并行抽干同队列:item 间独立,共享 doc 变更走 self._lock,
        item_status 按 item_id 分键写、磁盘标记按 item 路径分文件 → 无串台。
        gen 过期(被新一轮 _spawn 顶替)→ 立即停拉,不偷新一代的 item。
        """
        single = st.stage_plan.is_single
        try:
            while True:
                if st.cancel_event.is_set() or st.pool_generation != gen:
                    break
                st.pause_gate.wait()
                if st.cancel_event.is_set() or st.pool_generation != gen:
                    break
                try:
                    item = st.queue.popleft()   # 原子;多 worker 并发 pop 安全
                except IndexError:
                    break
                try:
                    # The lease covers the complete stage, artifact promotion,
                    # business commit, and receipt write. Delete/reset owns the
                    # exclusive side and therefore cannot cut the directory away.
                    with self._lifecycle_guard():
                        if single:
                            self._process_item_single(st, item)
                        else:
                            self._process_item_staged(st, item)
                except HistoryAuthorityError as exc:
                    # Commit helpers normally project this typed failure at the
                    # narrow boundary.  Keep a worker-level backstop because a
                    # future history consumer inside item processing must never
                    # kill the daemon thread without a durable item diagnosis.
                    self._record_history_authority_failure(st, item, exc)
                    self._broadcast_entity(st, [item])
                    self._broadcast_pipeline()
                except ZeError as exc:
                    if exc.code not in (
                        "project_busy",
                        "project_not_found",
                        "project_generation_stale",
                        "project_recovery_required",
                    ):
                        raise
                    st.cancel_event.set()
                    st.status = "cancelled"
                    break
        finally:
            self._on_worker_done(st, gen)

    def _on_worker_done(self, st: _RunState, gen: int) -> None:
        """worker 退出:**本代最后一个** worker 收尾 finalize(屏障,杜绝并发/过期 worker 误 finalize)。"""
        with st.worker_lock:
            if gen != st.pool_generation:
                return   # 过期代 worker:绝不碰新一代池的计数
            st.active_workers -= 1
            last = st.active_workers == 0
        if last:
            self._finalize(st)
            self._broadcast_pipeline()

    def _process_item_single(self, st: _RunState, item: WorkItem) -> None:
        """单 stage:一个 item 跑一遍 item_stage(per-item 处理,本体与 P2b 前逐字一致)。"""
        repaired = scan.repair_item_completion(
            item, self._data_dir, st.run_id, st.item_stage.id,
        )
        self._clear_repaired_completion_error(st, item, repaired)
        phase = scan.scan_item(item, self._data_dir, st.run_id, st.item_stage.id)
        if phase == "done":
            self._set_item(st, item, "done")
            self._broadcast_entity(st, [item])   # 续跑跳过已产出项:也要清前端占位
            return
        if phase == "error":
            err = self._completion_or_stage_error(st, item)
            self._set_item(st, item, "error", error=err)
            return  # completion 证据不确定时禁止重放

        # 起跑只证明任务已进入 stage，不证明已经完成了 0% 工作。只有业务
        # stage 主动调用 set_progress() 后才有可展示的数值。
        self._set_item(st, item, "running")
        self._log(st, "info", "item started", source="pipeline", item_id=item.item_id,
                  stage_id=st.item_stage.id)
        self._broadcast_entity(st, [item])
        try:
            attempt = scan.begin_attempt(
                item, self._data_dir, st.run_id, st.item_stage.id, single_stage=True,
            )
        except (scan.ArtifactProtocolError, OSError) as error:
            err = {
                "code": getattr(error, "code", "artifact_attempt_failed"),
                "message": str(error),
                "trace_id": st.trace_id,
            }
            self._write_error_marker(st.run_id, item.item_id, err)
            self._set_item(st, item, "error", error=err)
            self._log(st, "error", err["message"], source="pipeline",
                      item_id=item.item_id, stage_id=st.item_stage.id, code=err["code"])
            self._broadcast_entity(st, [item])
            self._broadcast_pipeline()
            return

        args = {
            **item.payload_copy(),
            "_item_id": item.item_id,
            "_data_dir": str(self._data_dir),
            # Stage 只写 attempt；读取上游/既有最终文件请显式用 _final_artifacts。
            "_artifacts": [str(path) for path in attempt.staging_paths],
            "_final_artifacts": [str(self._data_dir / path) for path in item.artifacts],
            "_attempt_number": attempt.number,
            "_attempt_nonce": attempt.nonce,
        }

        def _on_prog(ctx: Any, _item: WorkItem = item) -> None:
            self._set_item(st, _item, "running", progress=ctx.progress, stage_label=ctx.label)
            self._log_progress(st, _item.item_id, ctx.stage_id, ctx.progress, ctx.label)
            if self._should_broadcast_progress(st, _item.item_id, ctx.progress):
                self._broadcast_entity(st, [_item])

        def _on_log(ctx: Any, level: str, message: str,
                    extra: dict[str, Any], _item: WorkItem = item) -> None:
            extra = self._clean_stage_log_extra(extra)
            self._log(st, level, message, source="stage", item_id=_item.item_id,
                      stage_id=ctx.stage_id, **extra)
            self._maybe_broadcast_pipeline_log(st)

        with self._acquire_scopes(st.item_stage):
            with self._credential_scope(st):
                result = st.stage_runner.run(
                    st.item_stage, args, task_id=item.item_id,
                    on_progress=_on_prog, on_log=_on_log, trace_id=st.trace_id,
                )

        if st.cancel_event.is_set():
            self._set_item(st, item, "pending")
            self._log(st, "warn", "stage result ignored after cancellation", source="pipeline",
                      item_id=item.item_id, stage_id=st.item_stage.id, code="cancelled")
            self._broadcast_entity(st, [item])
            self._broadcast_pipeline()
            return

        if result.status == "ok":
            self._commit_ok(st, item, result.output, attempt)
        elif result.status == "cancelled":
            self._set_item(st, item, "pending")
        else:
            err = result.error or {"code": "stage_error", "message": "unknown", "trace_id": st.trace_id}
            self._write_error_marker(st.run_id, item.item_id, err)
            self._set_item(st, item, "error", error=err)
            self._log(st, "error", err.get("message", "stage error"), source="stage",
                      item_id=item.item_id, stage_id=st.item_stage.id, code=err.get("code"))

        cur = st.item_status.get(item.item_id)
        if cur is not None and cur.phase == "error":   # stage 错 / _commit_ok 冲突皆经此统一口(F3)
            self._maybe_retry(st, item, stage_id=st.item_stage.id)
        self._broadcast_entity(st, [item])
        self._broadcast_pipeline()

    # ---- 多 stage 路径(P2b) ----

    def _process_item_staged(self, st: _RunState, item: WorkItem) -> None:
        """多 stage:一个 item 顺序过 stage_plan 各 stage(per-item 处理,本体与 P2b 一致)。

        stage receipt 派生续跑点(崩溃/暂停安全)。语义:取消→pending、错误→标记+留给 retry、
        暂停→段间断点。每 stage 先提交 entity.update(逐 stage CAS)，再 promote artifact
        并写 checksum receipt。
        """
        plan = st.stage_plan
        repaired = False
        for stage_id in plan.stage_ids:
            repaired = (
                scan.repair_stage_completion(item, self._data_dir, st.run_id, stage_id)
                or repaired
            )
        self._clear_repaired_completion_error(
            st, item, repaired and scan.stage_item_done(item, self._data_dir, st.run_id, plan),
        )
        phase = scan.scan_item_staged(item, self._data_dir, st.run_id, plan)
        if phase == "done":
            self._set_item_staged(st, item, "done", stage_index=len(plan))
            self._broadcast_entity(st, [item])   # 续跑跳过已完成项:清前端占位
            return
        if phase == "error":
            err = self._completion_or_stage_error(st, item)
            self._set_item_staged(
                st, item, "error",
                stage_index=scan.completed_stage_count(
                    item, self._data_dir, st.run_id, plan,
                ),
                error=err,
            )
            return

        start_idx = scan.completed_stage_count(item, self._data_dir, st.run_id, plan)
        failed = cancelled = False
        for idx in range(start_idx, len(plan)):
            if st.cancel_event.is_set():
                cancelled = True
                break
            st.pause_gate.wait()
            if st.cancel_event.is_set():
                cancelled = True
                break

            stage = plan[idx]
            try:
                attempt = scan.begin_attempt(
                    item, self._data_dir, st.run_id, stage.id, single_stage=False,
                )
            except (scan.ArtifactProtocolError, OSError) as error:
                err = {
                    "code": getattr(error, "code", "artifact_attempt_failed"),
                    "message": str(error),
                    "trace_id": st.trace_id,
                }
                self._write_error_marker(st.run_id, item.item_id, err)
                self._set_item_staged(st, item, "error", stage_index=idx, error=err)
                self._log(st, "error", err["message"], source="pipeline",
                          item_id=item.item_id, stage_id=stage.id, code=err["code"])
                failed = True
                break
            # stage 开始本身不是进度测量；没有 callback 时保持 indeterminate。
            self._set_item_staged(st, item, "running", stage_index=idx,
                                  stage_label=stage.label)
            self._log(st, "info", "stage started", source="pipeline", item_id=item.item_id,
                      stage_id=stage.id)
            self._broadcast_entity(st, [item])

            args = {
                **item.payload_copy(),
                "_item_id": item.item_id,
                "_data_dir": str(self._data_dir),
                "_artifacts": [str(path) for path in attempt.staging_paths],
                "_final_artifacts": [str(self._data_dir / path) for path in item.artifacts],
                "_attempt_number": attempt.number,
                "_attempt_nonce": attempt.nonce,
                "_stage_id": stage.id,
                "_stage_index": idx,
                "_stage_count": len(plan),
            }
            self._begin_step(st, item, stage, args, idx)

            def _on_prog(ctx: Any, _item: WorkItem = item, _idx: int = idx) -> None:
                self._set_item_staged(st, _item, "running", stage_index=_idx,
                                      stage_progress=ctx.progress, stage_label=ctx.label)
                self._set_step_progress(st, _item.item_id, ctx.stage_id, ctx.progress)
                key = f"{_item.item_id}::{ctx.stage_id}"
                self._log_progress(st, key, ctx.stage_id, ctx.progress, ctx.label,
                                   item_id=_item.item_id)
                if self._should_broadcast_progress(st, key, ctx.progress):
                    self._broadcast_entity(st, [_item])

            def _on_log(ctx: Any, level: str, message: str,
                        extra: dict[str, Any], _item: WorkItem = item) -> None:
                extra = self._clean_stage_log_extra(extra)
                self._log(st, level, message, source="stage", item_id=_item.item_id,
                          stage_id=ctx.stage_id, **extra)
                self._maybe_broadcast_pipeline_log(st)

            with self._acquire_scopes(stage):
                with self._credential_scope(st):
                    result = st.stage_runner.run(
                        stage, args, task_id=f"{item.item_id}::{stage.id}",
                        on_progress=_on_prog, on_log=_on_log, trace_id=st.trace_id,
                    )

            if st.cancel_event.is_set():
                cancelled = True
                self._set_item_staged(st, item, "pending", stage_index=idx)
                self._end_step(st, item, stage, "pending", result.output,
                               error={"code": "cancelled", "message": "cancelled",
                                      "trace_id": st.trace_id})
                self._log(st, "warn", "stage result ignored after cancellation",
                          source="pipeline", item_id=item.item_id, stage_id=stage.id,
                          code="cancelled")
                break

            if result.status == "ok":
                commit = self._commit_stage(st, item, result.output, attempt)
                if not commit.get("ok"):
                    err = commit.get("error") or {
                        "code": "commit_error", "message": "commit failed", "trace_id": st.trace_id,
                    }
                    self._write_error_marker(st.run_id, item.item_id, err)
                    self._set_item_staged(st, item, "error", stage_index=idx, error=err)
                    self._end_step(st, item, stage, "error", result.output,
                                   error=err, commit=commit)
                    self._log(st, "error", f"commit failed: {err.get('code', 'unknown')}",
                              source="pipeline", item_id=item.item_id, stage_id=stage.id,
                              code=err.get("code"))
                    failed = True
                    break
                scan.clear_stage_stale(self._data_dir, st.run_id, item.item_id, stage.id)
                self._end_step(st, item, stage, "done", result.output, commit=commit)
                self._log(st, "info", "stage completed", source="pipeline",
                          item_id=item.item_id, stage_id=stage.id)
                # 「关闭」断点:本 stage 完成后若标 halt(静态 pause_after 或运行时 breakpoint)且非末 stage →
                # 清 pause_gate + 转 paused;**不 break** —— 循环走到下一 stage 边界的 pause_gate.wait()
                # 处 park 本 worker(持 item),与既有 pause 完全同语义(resume → set gate → 续跑下一 stage)。
                if (stage.pause_after or stage.id in st.breakpoints) and idx + 1 < len(plan) \
                        and not st.cancel_event.is_set():
                    with self._lock:
                        st.pause_gate.clear()
                        if st.status == "running":
                            st.status = "paused"
                    self._log(st, "info", "halted at breakpoint", source="pipeline",
                              item_id=item.item_id, stage_id=stage.id)
                    self._broadcast_pipeline()
            elif result.status == "cancelled":
                self._set_item_staged(st, item, "pending", stage_index=idx)
                self._end_step(st, item, stage, "pending", result.output,
                               error=result.error)
                self._log(st, "warn", "stage cancelled", source="pipeline",
                          item_id=item.item_id, stage_id=stage.id, code="cancelled")
                cancelled = True
                break
            elif result.status == "interrupted":
                err = result.error or {"code": "interrupted", "message": "interrupted",
                                       "trace_id": st.trace_id}
                self._set_item_staged(st, item, "interrupted", stage_index=idx, error=err)
                self._end_step(st, item, stage, "interrupted", result.output, error=err)
                self._log(st, "warn", "stage interrupted", source="pipeline",
                          item_id=item.item_id, stage_id=stage.id, code="interrupted")
                # 该 stage 已真正进入 run；resume 会从无 done marker 的当前
                # stage 重新开始，因此不能按普通暂停边界处理。
                st.replay_guard_items.add(item.item_id)
                st.status = "interrupted"
                cancelled = True
                break
            else:
                err = result.error or {"code": "stage_error", "message": "unknown",
                                       "trace_id": st.trace_id}
                self._write_error_marker(st.run_id, item.item_id, err)
                self._set_item_staged(st, item, "error", stage_index=idx, error=err)
                self._end_step(st, item, stage, "error", result.output, error=err)
                self._log(st, "error", err.get("message", "stage error"), source="stage",
                          item_id=item.item_id, stage_id=stage.id, code=err.get("code"))
                failed = True
                break

            self._broadcast_entity(st, [item])
            self._broadcast_pipeline()

        if failed:
            # 失败 stage 的自动重试(F3):已完成 stage 标记保留 → 重跑只从失败 stage 接。
            self._maybe_retry(st, item, stage_id=stage.id, stage_index=idx)
        if not failed and not cancelled:
            if not scan.stage_item_done(item, self._data_dir, st.run_id, plan):
                err = {
                    "code": "pipeline_completion_repair_required",
                    "message": "stage receipts do not cover every declared WorkItem artifact",
                    "trace_id": st.trace_id,
                    "context": {"artifact_code": "artifact_coverage_incomplete"},
                }
                self._write_error_marker(st.run_id, item.item_id, err)
                self._set_item_staged(
                    st, item, "error", stage_index=max(0, len(plan) - 1), error=err,
                )
                self._log(st, "error", err["message"], source="pipeline",
                          item_id=item.item_id, code=err["code"])
            else:
                self._clear_error_marker(st.run_id, item.item_id)
                self._set_item_staged(st, item, "done", stage_index=len(plan))
                self._log(st, "info", "item completed", source="pipeline", item_id=item.item_id)
        self._broadcast_entity(st, [item])
        self._broadcast_pipeline()

    def _commit_stage(
        self,
        st: _RunState,
        item: WorkItem,
        output: dict[str, Any],
        attempt: scan.ArtifactAttempt,
    ) -> dict[str, Any]:
        """单 stage 成功后视情提交 entity.update(有 patches/name/provenance 才提交)。

        成功返回 {ok, operation_id, patches} 并刷新 claimed 版本(供下个 stage 的 CAS);
        版本冲突返回 {ok:false,error}。
        """
        has_update = bool(output.get("patches") or output.get("name") or output.get("ai_provenance"))
        has_created = bool(output.get("created_entities"))
        try:
            with self._lock:
                produced: dict[str, Any] = {}

                def _produce_stage_commit():
                    op = None
                    ops = []
                    if has_update:
                        op = self._cmd_reg.dispatch(
                            "entity.update",
                            {
                                "type": item.entity_type, "id": item.entity_id,
                                "patches": dict(output.get("patches") or {}),
                                "expected_version": st.claimed.get(item.item_id),
                                "name": output.get("name"),
                                "ai_provenance": output.get("ai_provenance"),
                            },
                            actor=self._actor, source="ai", doc=self._doc, op_log=None,
                        )
                        ops.append(op)
                    created_op = self._dispatch_created(output)
                    if created_op is not None:
                        ops.append(created_op)
                    produced["primary"] = op
                    return self._mutation_product(ops)

                identity = self._commit_mutation(_produce_stage_commit, trace_id=st.trace_id)
                # claimed 是 pipeline 运行态 CAS 游标，不属于 Document mutation
                # session。只有整批已经 committed 后才能推进；否则 created_entities
                # 等后续 dispatch 失败时 Document 虽会回滚，claimed 却会虚增一版。
                if has_update:
                    ent = self._doc.find(item.entity_type, item.entity_id)
                    if ent is not None:
                        st.claimed[item.item_id] = ent.version
                op = produced.get("primary")
            scan.finalize_attempt(
                attempt,
                commit_id=identity["commit_id"],
                commit_kind=identity["commit_kind"],
                operation_ids=identity["operation_ids"],
                require_all=False,
            )
            return {
                "ok": True,
                "operation_id": getattr(op, "id", "") if op is not None else "",
                "operation_ids": list(identity["operation_ids"]),
                "commit_id": identity["commit_id"],
                "patches": dict(output.get("patches") or {}),
            }
        except HistoryAuthorityError as e:
            return {"ok": False, "error": self._history_authority_failure(st, e)}
        except ZeError as e:
            return {
                "ok": False,
                "error": {"code": e.code, "message": str(e), "trace_id": st.trace_id},
            }
        except (scan.ArtifactProtocolError, OSError) as e:
            return {"ok": False, "error": self._artifact_completion_error(st, e)}

    def _build_stages(self, plan: StagePlan, stage_index: int, item_phase: str,
                      stage_progress: float | None) -> tuple[StageStatus, ...]:
        """按当前 stage_index + item 相位,派生每 stage 的 StageStatus(done/running/error/pending)。"""
        out: list[StageStatus] = []
        # item 全完成时 stage_index==len(plan) → 所有 idx 落 idx<stage_index 分支(全 done);
        # idx==stage_index 分支只在「当前正在/卡在某 stage」时命中。
        for idx, s in enumerate(plan.stages):
            if idx < stage_index:
                ph, prog = "done", 1.0
            elif idx == stage_index:
                if item_phase == "done":
                    ph, prog = "done", 1.0
                elif item_phase == "error":
                    ph, prog = "error", stage_progress
                else:
                    ph, prog = "running", stage_progress
            else:
                ph, prog = "pending", None
            out.append(StageStatus(stage_id=s.id, label=s.label, phase=ph, progress=prog))
        return tuple(out)

    def _set_item_staged(self, st: _RunState, item: WorkItem, phase: str, *,
                         stage_index: int, stage_progress: float | None = None,
                         stage_label: str = "", error: dict[str, Any] | None = None) -> None:
        plan = st.stage_plan
        total = len(plan)
        previous = st.item_status.get(item.item_id)
        if phase == "done":
            stage_progress = 1.0
        elif (
            stage_progress is None
            and phase in ("error", "interrupted")
            and previous is not None
            and previous.stage_index == stage_index
            and 0 <= stage_index < len(previous.stages)
        ):
            # 失败/中断不抹掉业务 stage 已经真实报告过的最后进度。
            stage_progress = previous.stages[stage_index].progress
        stages = self._build_stages(plan, stage_index, phase, stage_progress)
        if phase == "done" or not total:
            overall: float | None = 1.0
        else:
            unfinished = [stage for stage in stages if stage.phase != "done"]
            # 已完成 stage 数仍由 stages / stage_summary 单独表达。只要任一
            # 未完成 stage 没有真实 callback，就不能用等分 stage 数伪造总百分比。
            if any(stage.progress is None for stage in unfinished):
                overall = None
            else:
                overall = sum(
                    1.0 if stage.phase == "done" else float(stage.progress)
                    for stage in stages
                ) / total
        st.item_status[item.item_id] = EntityRunStatus(
            entity_type=item.entity_type, entity_id=item.entity_id,
            phase=phase, progress=overall, run_id=st.run_id,
            stage_label=stage_label, error=error, updated_at=time.time(),
            stages=stages, stage_index=stage_index, eta_sec=None,
            placeholder=item.item_id in st.placeholder_item_ids,
        )
        if phase == "done":
            self._entity_done_at[(item.entity_type, item.entity_id)] = time.time()

    def _dispatch_created(self, output: dict[str, Any]):
        """stage 输出含 created_entities → 经命令通道单 Operation 批量建实体(undo/审计齐)。须在 self._lock 内调。

        返回批量建实体的 Operation(无 created_entities 时返 None);**op_log=None**,append 交由 caller 的
        _save_then_append 在 doc.save 之后统一做(approach B:先落盘、再记 OpLog,保证 doc ⊇ oplog)。
        created_entities = [{type, name?, data?, id?}, ...];走通用 entity.batch_add,
        让 stage 不仅能"改已存在实体"、也能"建 N 个新实体"(非专用能力)。
        """
        created = output.get("created_entities")
        if not created:
            return None
        args: dict[str, Any] = {"items": list(created)}
        # stage 可声明 created_entities_on_conflict="skip":确定性 id 幂等落库
        # (透传给 entity.batch_add on_conflict;3.55.0,design D3 自动建帧反哺)。
        mode = output.get("created_entities_on_conflict")
        if mode is not None:
            args["on_conflict"] = mode
        return self._cmd_reg.dispatch(
            "entity.batch_add", args,
            actor=self._actor, source="ai", doc=self._doc, op_log=None,
        )

    @staticmethod
    def _artifact_completion_error(st: _RunState, error: BaseException) -> dict[str, Any]:
        """业务 commit 之后的文件收尾失败不能当作普通可重试 stage 错误。"""
        artifact_code = getattr(error, "code", "artifact_finalize_failed")
        return {
            "code": "pipeline_completion_repair_required",
            "message": str(error) or "artifact completion requires repair",
            "trace_id": st.trace_id,
            "context": {"artifact_code": artifact_code},
        }

    @staticmethod
    def _history_authority_failure(
        st: _RunState,
        error: HistoryAuthorityError,
    ) -> dict[str, Any]:
        """Project a typed history failure into a durable pipeline marker.

        Classification deliberately consumes ``outcome_unknown`` from the
        protocol type.  The human ``reason`` remains diagnostic metadata and
        never decides whether replay is legal.
        """
        outcome_unknown = error.outcome_unknown is True
        context: dict[str, Any] = {
            "reason": error.reason,
            "durability_phase": error.durability_phase,
            "outcome_unknown": outcome_unknown,
            "retry_safe": False,
        }
        if outcome_unknown:
            context.update({"repair_required": True, "reload_required": False})
            if error.commit_facts:
                context["commit_facts"] = copy.deepcopy(error.commit_facts)
            code = "commit_repair_required"
            message = (
                "pipeline commit outcome is unknown; repair and reconcile "
                "before any replay"
            )
        else:
            context.update({"repair_required": False, "reload_required": True})
            code = "history_authority_mismatch"
            message = "canonical history changed; reload the project before retrying"
        return {
            "code": code,
            "message": message,
            "trace_id": st.trace_id,
            "context": context,
        }

    @staticmethod
    def _is_history_reload_marker(error: dict[str, Any]) -> bool:
        """Recognize only the exact safe pre-write marker issued above."""
        context = error.get("context")
        return (
            error.get("code") == "history_authority_mismatch"
            and type(context) is dict
            and context.get("outcome_unknown") is False
            and context.get("reload_required") is True
            and context.get("repair_required") is False
            and context.get("retry_safe") is False
        )

    def _record_history_authority_failure(
        self,
        st: _RunState,
        item: WorkItem,
        error: HistoryAuthorityError,
        *,
        stage_index: int | None = None,
    ) -> dict[str, Any]:
        """Persist and expose one typed history failure without replaying it."""
        projected = self._history_authority_failure(st, error)
        st.history_reload_ready_items.discard(item.item_id)
        self._write_error_marker(st.run_id, item.item_id, projected)
        if st.stage_plan.is_single:
            self._set_item(st, item, "error", error=projected)
        else:
            index = (
                self._resume_stage_index(st, item)
                if stage_index is None
                else stage_index
            )
            self._set_item_staged(
                st, item, "error", stage_index=index, error=projected,
            )
        self._log(
            st,
            "error",
            projected["message"],
            source="pipeline",
            item_id=item.item_id,
            code=projected["code"],
            durability_phase=error.durability_phase,
            outcome_unknown=error.outcome_unknown,
        )
        return projected

    def _clear_repaired_completion_error(
        self, st: _RunState, item: WorkItem, repaired: bool,
    ) -> None:
        """A valid repaired receipt is authoritative over its stale error marker."""
        if not repaired:
            return
        persisted = scan.read_json_file(
            scan.error_marker_path(self._data_dir, st.run_id, item.item_id),
        )
        if persisted.get("code") == "pipeline_completion_repair_required":
            self._clear_error_marker(st.run_id, item.item_id)

    def _completion_or_stage_error(
        self, st: _RunState, item: WorkItem,
    ) -> dict[str, Any]:
        """Materialize invalid completion evidence as a replay-blocking error."""
        marker = scan.error_marker_path(self._data_dir, st.run_id, item.item_id)
        persisted = scan.read_json_file(marker)
        if persisted.get("code"):
            return persisted
        err = {
            "code": "pipeline_completion_repair_required",
            "message": "pipeline completion evidence is present but invalid",
            "trace_id": st.trace_id,
            "context": {"artifact_code": "artifact_receipt_invalid"},
        }
        self._write_error_marker(st.run_id, item.item_id, err)
        return err

    def _commit_ok(
        self,
        st: _RunState,
        item: WorkItem,
        output: dict[str, Any],
        attempt: scan.ArtifactAttempt,
    ) -> None:
        """成功项:经 command 通道(source='ai')提交 entity.update(有改才提)+ created_entities 批量建 + 落盘。
        版本冲突 → 标 error 不覆盖。"""
        try:
            with self._lock:
                def _produce_item_commit():
                    ops = []
                    if output.get("patches") or output.get("name") or output.get("ai_provenance"):
                        ops.append(self._cmd_reg.dispatch(
                            "entity.update",
                            {
                                "type": item.entity_type, "id": item.entity_id,
                                "patches": dict(output.get("patches") or {}),
                                "expected_version": st.claimed.get(item.item_id),
                                "name": output.get("name"),
                                "ai_provenance": output.get("ai_provenance"),
                            },
                            actor=self._actor, source="ai", doc=self._doc, op_log=None,
                        ))
                    created_op = self._dispatch_created(output)
                    if created_op is not None:
                        ops.append(created_op)
                    return self._mutation_product(ops)

                identity = self._commit_mutation(_produce_item_commit, trace_id=st.trace_id)
                # 锁内先记「已成功产出」:并发输家在同一把锁内 CAS 失败,出锁标 error/广播时
                # 台账必已可见 → supersede 判定无 race 窗口(_set_item done 的记录是兜底重复)。
                self._entity_done_at[(item.entity_type, item.entity_id)] = time.time()
            scan.finalize_attempt(
                attempt,
                commit_id=identity["commit_id"],
                commit_kind=identity["commit_kind"],
                operation_ids=identity["operation_ids"],
                require_all=True,
            )
            self._clear_error_marker(st.run_id, item.item_id)
            self._set_item(st, item, "done")
            self._log(st, "info", "item completed", source="pipeline", item_id=item.item_id)
        except HistoryAuthorityError as e:
            self._record_history_authority_failure(st, item, e)
        except ZeError as e:
            err = {"code": e.code, "message": str(e), "trace_id": st.trace_id}
            self._write_error_marker(st.run_id, item.item_id, err)
            self._set_item(st, item, "error", error=err)
            self._log(st, "error", f"commit failed: {e.code}", source="pipeline",
                      item_id=item.item_id, code=e.code)
        except (scan.ArtifactProtocolError, OSError) as e:
            err = self._artifact_completion_error(st, e)
            self._write_error_marker(st.run_id, item.item_id, err)
            self._set_item(st, item, "error", error=err)
            self._log(st, "error", "artifact completion requires repair", source="pipeline",
                      item_id=item.item_id, code=err["code"])

    def _maybe_retry(self, st: _RunState, item: WorkItem, *,
                     stage_id: str | None = None, stage_index: int | None = None) -> bool:
        """item 落 error 后的自动重试(F3):attempts < max_retries 且未 cancel → 清标记 + 重置 pending + 重新入队。

        error 标记已由错误路径写下,重试即清(崩在清后重跑前 → recover 派生 pending 续跑,安全);
        超限 → 返 False、标记保留 = 现行为(留给手动 retry_failed,它不看本计数)。
        staged 路径不清 stage done 标记 → 重跑经 completed_stage_count 只从失败 stage 接。
        重新入队走本代共享队列:worker 下轮循环照常过 cancel_event / pause_gate / 代号检查。
        """
        if st.max_retries <= 0 or st.cancel_event.is_set():
            return False
        cur = st.item_status.get(item.item_id)
        if cur is not None and (cur.error or {}).get("code") == "cas_version_mismatch":
            # AI 重试不覆盖用户编辑(editor-architecture 规则12):CAS 冲突 = 生成期间用户改了
            # 该实体;旧行为「重抢当前版本重跑」会让 AI 产出盖掉用户刚才的编辑。冲突类不自动
            # 重试:用户编辑赢,error 占位留给 _error_superseded 掩蔽/手动 retry_failed
            # (手动重试 = 用户明示"要 AI 结果",那时重抢版本才正当)。码与掩蔽逻辑同源。
            self._log(st, "warn", "cas conflict: auto-retry skipped (user edit wins)",
                      source="pipeline", item_id=item.item_id, stage_id=stage_id,
                      code="auto_retry_skipped_cas")
            return False
        if cur is not None and (cur.error or {}).get("code") == "history_authority_mismatch":
            # Producer 前失败没有未知业务写入，但当前 Context 的 authority 已经
            # 失效；同一 worker 自动重试只会再次撞旧权威。等待 reload 后显式 retry。
            self._log(
                st,
                "error",
                "canonical history reload required: auto-retry skipped",
                source="pipeline",
                item_id=item.item_id,
                stage_id=stage_id,
                code="auto_retry_skipped_history_reload",
            )
            return False
        if cur is not None and (cur.error or {}).get("code") in (
            "commit_dirty", "commit_indeterminate", "pipeline_completion_repair_required",
            "commit_repair_required",
        ):
            # 整笔命令的内存/耐久结果不清楚，自动重跑会叠加业务修改。只能先对账或由
            # W04 mutation session 完整回滚；不能沿用普通 stage retry。
            self._log(st, "error", "commit outcome unsafe: auto-retry skipped",
                      source="pipeline", item_id=item.item_id, stage_id=stage_id,
                      code="auto_retry_skipped_commit_outcome")
            return False
        n = st.retry_attempts.get(item.item_id, 0) + 1
        if n > st.max_retries:
            return False
        start_idx = stage_index if stage_index is not None else 0
        unsafe = self._unsafe_stage_replays(st, [(item, start_idx)])
        if unsafe:
            # 自动路径没有同步 caller 可接 ZeError，因此把同一稳定码
            # 落到 item/error marker 与结构化日志。保留原失败作为 cause，
            # 且不清 marker、不重抢版本、不入队。
            previous = dict(cur.error) if cur is not None and cur.error else None
            err: dict[str, Any] = {
                "code": "pipeline_replay_unsafe",
                "message": "automatic retry includes a stage declared retry_safe=False",
                "trace_id": st.trace_id,
                "context": {"run_id": st.run_id, "trigger": "auto_retry", "stages": unsafe},
            }
            if previous is not None:
                err["cause"] = previous
            self._write_error_marker(st.run_id, item.item_id, err)
            if stage_index is None:
                self._set_item(st, item, "error", error=err)
            else:
                self._set_item_staged(
                    st, item, "error", stage_index=stage_index, error=err,
                )
            self._log(
                st,
                "error",
                "automatic retry blocked by retry_safe=False",
                source="pipeline",
                item_id=item.item_id,
                stage_id=stage_id,
                code="pipeline_replay_unsafe",
                unsafe_stages=unsafe,
            )
            return False
        st.retry_attempts[item.item_id] = n
        self._log(st, "warn", f"retry {n}/{st.max_retries}", source="pipeline",
                  item_id=item.item_id, stage_id=stage_id, code="auto_retry")
        self._clear_error_marker(st.run_id, item.item_id)
        with self._lock:   # 重抢版本(镜像 retry_failed):commit 冲突类失败重试才有机会过 CAS
            ent = self._doc.find(item.entity_type, item.entity_id)
            st.claimed[item.item_id] = ent.version if ent else 1
        if stage_index is None:
            self._set_item(st, item, "pending")
        else:
            self._set_item_staged(st, item, "pending", stage_index=stage_index)
        st.queue.append(item)
        return True

    def _finalize(self, st: _RunState) -> None:
        counts = self._counts(st)
        if st.interrupt_event.is_set() or st.status in ("interrupting", "interrupted"):
            st.status = "interrupted"
        elif st.cancel_event.is_set() and counts["pending"] > 0:
            st.status = "cancelled"
        elif counts["error"] > 0:
            st.status = "error"
        elif counts["done"] == counts["total"]:
            st.status = "success"
        elif not st.pause_gate.is_set():
            st.status = "paused"   # 还有 pending 但被暂停,等 resume
        else:
            # worker 异常退出(非取消/非暂停/未全完成)→ 标 error,不留 running 僵尸 run
            st.status = "error"
        if st.status in ("cancelled", "success", "error"):
            st.ended_at = time.time()

    # ============================================================
    # 状态 / 广播
    # ============================================================

    def _set_item(self, st: _RunState, item: WorkItem, phase: str,
                  *, progress: float | None = None, stage_label: str = "",
                  error: dict[str, Any] | None = None) -> None:
        previous = st.item_status.get(item.item_id)
        if phase == "done":
            progress = 1.0
        elif phase in ("error", "interrupted") and progress is None and previous is not None:
            # 保留业务 callback 的最后真值；pending/retry 则仍明确重置为 unknown。
            progress = previous.progress
        st.item_status[item.item_id] = EntityRunStatus(
            entity_type=item.entity_type, entity_id=item.entity_id,
            phase=phase, progress=progress, run_id=st.run_id,
            stage_label=stage_label, error=error, updated_at=time.time(),
            placeholder=item.item_id in st.placeholder_item_ids,
        )
        if phase == "done":
            self._entity_done_at[(item.entity_type, item.entity_id)] = time.time()

    def _broadcast_entity(self, st: _RunState, items: list[WorkItem]) -> None:
        statuses = []
        for it in items:
            s = st.item_status.get(it.item_id)
            if s is None:
                continue
            d = s.to_dict()
            # 被取代的 error(CAS 冲突且实体已被别的 item 成功产出)→ 改发 done:前端按
            # 实体键删占位,不把重复生成输掉 CAS 的红色错误盖在已生成好的卡上。
            # run 详情视图仍保留真实 error。
            if s.phase == "error" and self._error_superseded(s):
                d["phase"] = "done"
                d["error"] = None
            statuses.append(d)
        if not statuses:
            return
        payload = to_status_payload(
            statuses, doc_id=getattr(self._doc, "id", None),
            run_id=st.run_id, trace_id=st.trace_id,
        )
        self._publish(payload)

    def _broadcast_pipeline(self) -> None:
        payload = to_pipeline_status_payload(self.snapshot(), doc_id=getattr(self._doc, "id", None))
        self._publish(payload)

    def _publish(self, payload: dict[str, Any]) -> None:
        try:
            self._broker.publish_change(payload)
        except Exception:
            import traceback
            traceback.print_exc()  # AP-R4: 广播错不影响 run

    def _log(self, st: _RunState, level: str, message: str, *, source: str = "pipeline",
             item_id: str | None = None, stage_id: str | None = None,
             code: str | None = None, artifact_path: str | None = None, **extra: Any) -> None:
        """写结构化执行日志,同时派生旧 log_lines 以保持 footer/CLI 兼容。"""
        event: dict[str, Any] = {
            "ts": time.time(),
            "level": str(level or "info").lower(),
            "source": source,
            "run_id": st.run_id,
            "trace_id": st.trace_id,
            "message": str(message),
        }
        optional = {
            "item_id": item_id,
            "stage_id": stage_id,
            "code": code,
            "artifact_path": artifact_path,
        }
        for k, v in {**optional, **extra}.items():
            if v is not None and v != "":
                event[k] = v
        st.log_events.append(event)
        st.log_lines.append(self._format_log_line(event))
        # SSE 里只发尾部,内存也做上限,避免长跑无限增长。
        if len(st.log_events) > 500:
            del st.log_events[:-500]
        if len(st.log_lines) > 500:
            del st.log_lines[:-500]

    def _clean_stage_log_extra(self, extra: dict[str, Any]) -> dict[str, Any]:
        reserved = {
            "source", "run_id", "trace_id", "item_id", "stage_id",
            "level", "message", "ts",
        }
        return {k: v for k, v in dict(extra or {}).items() if k not in reserved}

    def _maybe_broadcast_pipeline_log(self, st: _RunState) -> None:
        now = time.time()
        if now - st.last_log_broadcast < 0.5:
            return
        st.last_log_broadcast = now
        self._broadcast_pipeline()

    def _format_log_line(self, event: dict[str, Any]) -> str:
        prefix = f"[{event.get('level', 'info')}]"
        loc = ""
        if event.get("item_id") and event.get("stage_id"):
            loc = f" {event['item_id']}@{event['stage_id']}:"
        elif event.get("item_id"):
            loc = f" {event['item_id']}:"
        elif event.get("stage_id"):
            loc = f" @{event['stage_id']}:"
        return f"{prefix}{loc} {event.get('message', '')}".strip()

    def _should_broadcast_progress(self, st: _RunState, key: str,
                                   progress: float | None) -> bool:
        now = time.time()
        prev = st.progress_broadcasts.get(key)
        if prev is None:
            st.progress_broadcasts[key] = (now, progress)
            return True
        prev_t, prev_p = prev
        force_done = progress is not None and progress >= 1.0
        enough_time = now - prev_t >= 0.25
        enough_delta = (
            progress is not None and prev_p is not None
            and abs(progress - prev_p) >= 0.1
        )
        if force_done or enough_time or enough_delta:
            st.progress_broadcasts[key] = (now, progress)
            return True
        return False

    def _log_progress(self, st: _RunState, key: str, stage_id: str,
                      progress: float | None, label: str,
                      *, item_id: str | None = None) -> None:
        if progress is None:
            return
        now = time.time()
        prev = st.progress_logs.get(key)
        force_done = progress >= 1.0
        if prev is not None:
            prev_t, prev_p = prev
            if not force_done and now - prev_t < 2.0 and prev_p is not None and progress - prev_p < 0.25:
                return
        st.progress_logs[key] = (now, progress)
        pct = int(round(progress * 100))
        msg = label or f"progress {pct}%"
        self._log(st, "info", msg, source="progress", item_id=item_id or key,
                  stage_id=stage_id, progress=progress)
        self._maybe_broadcast_pipeline_log(st)

    # ============================================================
    # Pipeline notebook step state / snapshots
    # ============================================================

    def _step_key(self, item_id: str, stage_id: str) -> str:
        return f"{item_id}::{stage_id}"

    def _stage_command_id(self, stage: StageSpec) -> str:
        return getattr(stage, "command_id", "") or stage.id

    def _stage_ports(self, stage: StageSpec, direction: str) -> list[dict[str, Any]]:
        raw = getattr(stage, f"{direction}_ports", ()) or ()
        if raw:
            # StageSpec owns tuple-backed immutable JSON.  ``dict(p)`` would perform a
            # linear lookup for every key and leave nested extension bags in private wrapper
            # types.  The controlled thaw is linear and returns detached plain JSON.
            return [_thaw_stage_json(port) for port in raw]
        command_id = self._stage_command_id(stage)
        try:
            declaration = getattr(
                self._cmd_reg, "registered_declaration", None,
            )
            if callable(declaration):
                spec = declaration(command_id)
                ui_ports = dict(spec.get("ui_ports", {}) or {})
            else:
                # Compatibility for narrow fake registries in old tests.
                spec = self._cmd_reg.get(command_id)
                ui_ports = dict(getattr(spec, "ui_ports", {}) or {})
            raw = ui_ports.get("inputs" if direction == "input" else "outputs") or []
            if isinstance(raw, list):
                return [dict(p) for p in raw if isinstance(p, dict)]
        except Exception:
            return []
        return []

    def _get_path(self, obj: Any, path: str | None) -> Any:
        if not path:
            return obj
        cur = obj
        for part in path.replace("/", ".").split("."):
            if part == "":
                continue
            if isinstance(cur, dict):
                cur = cur.get(part)
            elif isinstance(cur, list) and part.isdigit():
                idx = int(part)
                cur = cur[idx] if 0 <= idx < len(cur) else None
            else:
                return None
        return cur

    def _port_value(self, port: dict[str, Any], *, item: WorkItem,
                    args: dict[str, Any], stage_output: dict[str, Any] | None = None,
                    commit: dict[str, Any] | None = None) -> Any:
        source = str(port.get("source") or "args")
        field = port.get("field")
        path = str(port.get("path") or field or "") or None
        if source == "args":
            value = self._get_path(args, path)
        elif source == "work_item_payload":
            value = self._get_path(item.payload_copy(), path)
        elif source == "entity_field":
            ent = self._doc.find(item.entity_type, item.entity_id)
            value = self._get_path(getattr(ent, "data", {}) if ent else {}, path)
        elif source == "config":
            value = self._get_path(args.get("_config", {}), path)
        elif source == "stage_output":
            value = self._get_path(stage_output or {}, path)
        elif source == "operation_patch":
            value = self._get_path((commit or {}).get("patches", {}), path)
        elif source == "artifact":
            rel = str(port.get("path") or port.get("field") or "")
            if not rel and item.artifacts:
                rel = item.artifacts[0]
            p = self._data_dir / rel if rel else None
            return {"kind": "artifact", "path": rel, "present": bool(p and p.exists())}
        else:
            value = None
        return self._summarize_port_value(port, value)

    def _summarize_port_value(self, port: dict[str, Any], value: Any) -> Any:
        if port.get("secret"):
            return {"configured": value is not None}
        kind = str(port.get("kind") or "json")
        if isinstance(value, dict) and isinstance(value.get("sha256"), str):
            return {"kind": "asset" if kind in ("image", "mask", "asset") else kind, "ref": dict(value)}
        if kind in ("image", "mask", "asset") and isinstance(value, str):
            return {"kind": kind, "url": value}
        return self._bounded_json_value(value)

    def _bounded_json_value(self, value: Any) -> Any:
        try:
            encoded = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            return {"truncated": True, "summary": repr(value)[:240]}
        if len(encoded) <= SNAPSHOT_VALUE_LIMIT:
            try:
                return json.loads(encoded)
            except Exception:
                return value
        return {
            "truncated": True,
            "summary": f"{type(value).__name__} snapshot exceeded {SNAPSHOT_VALUE_LIMIT} chars",
        }

    def _collect_port_snapshot(self, ports: list[dict[str, Any]], *, item: WorkItem,
                               args: dict[str, Any], stage_output: dict[str, Any] | None = None,
                               commit: dict[str, Any] | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for p in ports:
            pid = str(p.get("id") or p.get("field") or p.get("path") or "")
            if not pid:
                continue
            out[pid] = self._port_value(p, item=item, args=args,
                                        stage_output=stage_output, commit=commit)
        return out

    def _write_snapshot_file(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        safe = self._bounded_json_value(payload)
        scan.atomic_write_json(path, safe)

    def _begin_step(self, st: _RunState, item: WorkItem, stage: StageSpec,
                    args: dict[str, Any], stage_index: int) -> None:
        now = time.time()
        ports = self._stage_ports(stage, "input")
        inputs = self._collect_port_snapshot(ports, item=item, args=args)
        key = self._step_key(item.item_id, stage.id)
        state = {
            "item_id": item.item_id,
            "entity_type": item.entity_type,
            "entity_id": item.entity_id,
            "stage_id": stage.id,
            "stage_label": stage.label,
            "stage_index": stage_index,
            "command_id": self._stage_command_id(stage),
            "phase": "running",
            "progress": None,
            "started_at": now,
            "input_snapshot": inputs,
            "artifact_refs": [],
        }
        st.step_states[key] = state
        payload = {
            "stage_id": stage.id,
            "command_id": self._stage_command_id(stage),
            "phase": "running",
            "started_at": now,
            "inputs": inputs,
        }
        self._write_snapshot_file(
            scan.stage_input_path(self._data_dir, st.run_id, item.item_id, stage.id),
            payload,
        )

    def _set_step_progress(self, st: _RunState, item_id: str, stage_id: str,
                           progress: float | None) -> None:
        key = self._step_key(item_id, stage_id)
        if key in st.step_states:
            st.step_states[key]["progress"] = progress

    def _end_step(self, st: _RunState, item: WorkItem, stage: StageSpec, phase: str,
                  output: dict[str, Any], *, error: dict[str, Any] | None = None,
                  commit: dict[str, Any] | None = None) -> None:
        now = time.time()
        key = self._step_key(item.item_id, stage.id)
        ports = self._stage_ports(stage, "output")
        outputs = self._collect_port_snapshot(
            ports, item=item, args=item.payload_copy(), stage_output=output, commit=commit,
        )
        operations = [
            str(operation_id)
            for operation_id in ((commit or {}).get("operation_ids") or ())
            if operation_id
        ]
        if not operations:
            operation_id = str((commit or {}).get("operation_id") or "")
            operations = [operation_id] if operation_id else []
        state = dict(st.step_states.get(key, {}))
        state.update({
            "item_id": item.item_id,
            "entity_type": item.entity_type,
            "entity_id": item.entity_id,
            "stage_id": stage.id,
            "stage_label": stage.label,
            "command_id": self._stage_command_id(stage),
            "phase": phase,
            "progress": 1.0 if phase == "done" else state.get("progress"),
            "ended_at": now,
            "output_snapshot": outputs,
            "operation_ids": operations,
            "error": error,
        })
        st.step_states[key] = state
        payload = {
            "stage_id": stage.id,
            "command_id": self._stage_command_id(stage),
            "phase": phase,
            "started_at": state.get("started_at"),
            "ended_at": now,
            "inputs": state.get("input_snapshot") or {},
            "outputs": outputs,
            "operation_ids": operations,
            "error": error,
        }
        self._write_snapshot_file(
            scan.stage_result_path(self._data_dir, st.run_id, item.item_id, stage.id),
            payload,
        )

    def _mark_step_stale(self, st: _RunState, item: WorkItem, stage_id: str) -> None:
        for stage in st.stage_plan.stages[st.stage_plan.index_of(stage_id):]:
            key = self._step_key(item.item_id, stage.id)
            prev = dict(st.step_states.get(key, {}))
            prev.update({
                "item_id": item.item_id,
                "entity_type": item.entity_type,
                "entity_id": item.entity_id,
                "stage_id": stage.id,
                "stage_label": stage.label,
                "command_id": self._stage_command_id(stage),
                "phase": "stale",
            })
            st.step_states[key] = prev

    def _derive_step_states(self, st: _RunState) -> list[dict[str, Any]]:
        states: list[dict[str, Any]] = []
        for item in st.work_list.items:
            item_phase = scan.scan_item(
                item, self._data_dir, st.run_id, st.item_stage.id,
            ) if st.stage_plan.is_single else ""
            completed = 1 if item_phase == "done" and st.stage_plan.is_single else (
                scan.completed_stage_count(item, self._data_dir, st.run_id, st.stage_plan)
                if not st.stage_plan.is_single else 0
            )
            live = st.item_status.get(item.item_id)
            err = scan.read_json_file(scan.error_marker_path(self._data_dir, st.run_id, item.item_id))
            for idx, stage in enumerate(st.stage_plan.stages):
                key = self._step_key(item.item_id, stage.id)
                in_snap = scan.read_json_file(
                    scan.stage_input_path(self._data_dir, st.run_id, item.item_id, stage.id)
                )
                out_snap = scan.read_json_file(
                    scan.stage_result_path(self._data_dir, st.run_id, item.item_id, stage.id)
                )
                stale = scan.stage_stale_marker_path(
                    self._data_dir, st.run_id, item.item_id, stage.id,
                ).exists()
                if stale:
                    phase = "stale"
                elif idx < completed:
                    phase = "done"
                elif err and idx == completed:
                    phase = "error"
                elif live and live.stage_index == idx and live.phase in ("running", "interrupted", "error"):
                    phase = live.phase
                else:
                    phase = "pending"
                base = {
                    "item_id": item.item_id,
                    "entity_type": item.entity_type,
                    "entity_id": item.entity_id,
                    "stage_id": stage.id,
                    "stage_label": stage.label,
                    "stage_index": idx,
                    "command_id": self._stage_command_id(stage),
                    "phase": phase,
                    "progress": 1.0 if phase == "done" else None,
                    "input_snapshot": in_snap.get("inputs") or {},
                    "output_snapshot": out_snap.get("outputs") or {},
                    "operation_ids": out_snap.get("operation_ids") or [],
                    "error": (out_snap.get("error") or err or None),
                }
                live_state = st.step_states.get(key)
                if live_state:
                    base.update({k: v for k, v in live_state.items() if v is not None})
                states.append(base)
        return states

    def _active_items(self, st: _RunState) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in st.work_list.items:
            status = st.item_status.get(item.item_id)
            if status is None or status.phase == "done":
                continue
            out.append({
                "item_id": item.item_id,
                "entity_type": item.entity_type,
                "entity_id": item.entity_id,
                "phase": status.phase,
                "progress": status.progress,
                "stage_label": status.stage_label,
                "stage_index": status.stage_index,
                "error": status.error,
            })
        return out

    def _artifact_refs(self, st: _RunState, *, limit: int = 40) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        for item in st.work_list.items:
            for rel in item.artifacts:
                p = self._data_dir / rel
                refs.append({
                    "item_id": item.item_id,
                    "path": rel,
                    "present": p.exists() and p.is_file(),
                })
        return refs[-limit:]

    def _run_view(self, st: _RunState) -> dict[str, Any]:
        counts = self._counts(st)
        total = counts["total"]
        view: dict[str, Any] = {
            "id": st.run_id,
            "pipeline_id": st.pipeline_id,
            "start_command_id": st.start_command_id,
            "name": st.name or st.run_id,
            "stage": f"{counts['done']}/{total}",
            "progress": self._run_progress(st, counts),
            "status": st.status,
            "started_at": st.started_at,
            "ended_at": st.ended_at or None,
            "log_lines": st.log_lines[-20:],
            "can_pause": st.status == "running",
            "can_interrupt": st.status in ("running", "paused"),
            "can_resume": st.status in ("paused", "interrupted"),
            "can_cancel": st.status in ("running", "paused", "interrupting", "interrupted", "cancelling"),
            "failed_count": counts["error"],
            "detail_version": 1,
            "log_events": st.log_events[-80:],
            "active_items": self._active_items(st),
            "artifacts": self._artifact_refs(st),
            "step_states": self._derive_step_states(st),
            # M1 每步「关闭」断点:运行时 halt-after 集(pipeline.set_breakpoint 切的就是这个,
            # 前端 toggle 据此显 armed 态、round-trip 精确)。静态 StageSpec.pause_after 不并入——
            # 它是 mod 声明的永久断点、非可切控件,混入会让 toggle 显"未设"却仍 halt(撒谎)。
            "breakpoints": sorted(st.breakpoints),
            # stage 总数:>1 才在每步显断点 toggle、且末 stage 不显(halt-after 末 stage 是 no-op)。
            "stage_count": len(st.stage_plan.stages),
        }
        # P2d 可观测(加性):ETA/吞吐(样本不足时省略键)+ 多 stage 每 stage 完成统计。
        elapsed = max(0.0, time.time() - st.started_at)
        eta, throughput = scan.eta_throughput(counts["done"], counts["pending"], elapsed)
        if eta is not None:
            view["eta_sec"] = eta
        if throughput is not None:
            view["throughput"] = throughput
        if not st.stage_plan.is_single:
            view["stages"] = scan.stage_summary(st.work_list, self._data_dir, st.stage_plan)
        return view

    def _run_progress(self, st: _RunState, counts: dict[str, int]) -> float | None:
        """汇总真实 item 进度；任何未知未完成工作都会使 run 保持 unknown。

        ``stage`` / ``stages`` 已独立表达完成项和完成阶段计数，因此不能再把
        ``done / total`` 冒充工作量百分比。只有每个尚未完成的 item 都有业务
        callback 真值时才求平均；全部完成（含空 run）固定为 1.0。
        """
        total = counts["total"]
        if not total or counts["done"] == total:
            return 1.0

        progress_values: list[float] = []
        for item in st.work_list.items:
            phase = self._item_phase(st, item)
            if phase == "done":
                progress_values.append(1.0)
                continue
            # 失败意味着仍有未完成工作；即使 stage 计算曾到 100%，run 也尚未
            # 完成 commit/receipt，不能显示成完整百分比。
            if phase == "error":
                return None
            status = st.item_status.get(item.item_id)
            if status is None or status.progress is None:
                return None
            progress_values.append(status.progress)
        return sum(progress_values) / total

    # ============================================================
    # 杂项
    # ============================================================

    def _item_phase(self, st: _RunState, item: WorkItem) -> str:
        """item 相位：单 stage 看 item receipt，多 stage 看连续 stage receipt。"""
        if st.stage_plan.is_single:
            return scan.scan_item(item, self._data_dir, st.run_id, st.item_stage.id)
        return scan.scan_item_staged(item, self._data_dir, st.run_id, st.stage_plan)

    def _counts(self, st: _RunState) -> dict[str, int]:
        """各相位 item 数：两条路径都只认校验通过的 completion receipt。"""
        if st.stage_plan.is_single:
            return scan.scan_counts(st.work_list, self._data_dir, st.item_stage.id)
        done = err = 0
        for it in st.work_list.items:
            ph = self._item_phase(st, it)
            if ph == "done":
                done += 1
            elif ph == "error":
                err += 1
        total = len(st.work_list.items)
        return {"done": done, "error": err, "pending": total - done - err, "total": total}

    def _pending_items(self, st: _RunState) -> list[WorkItem]:
        return [it for it in st.work_list.items
                if self._item_phase(st, it) == "pending"]

    @staticmethod
    def _mutation_product(ops: list[Any]):
        clean = tuple(op for op in ops if op is not None)
        if not clean:
            return None
        if len(clean) == 1:
            return clean[0]
        return MutationBatch(clean)

    def _commit_mutation(self, producer: Callable[[], Any], *, trace_id: str = "") -> dict[str, Any]:
        """生产路径走 CommitCoordinator；兼容路径仍严格 save→append→publish。

        返回 artifact receipt 所需的 ``commit_id/operation_ids``。纯文件 stage 没有
        Document mutation，使用带前缀的本地 completion id，operation_ids 为空。
        非 committed receipt 作为明确 ZeError 抛给 pipeline 状态机，
        `commit_indeterminate/dirty` 会被自动重试护栏拦下，绝不重放整笔 stage。
        """
        if self._commit_coordinator is not None:
            execution = self._commit_coordinator.execute(producer, trace_id=trace_id)
            if execution.receipt is not None and not execution.is_committed:
                status = execution.receipt.status.value
                raise ZeError(
                    f"commit_{status}",
                    f"pipeline mutation commit ended as {status}",
                    context={"receipt": execution.receipt.to_dict()},
                )
            if execution.receipt is None:
                return {
                    "commit_id": f"artifact-only:{uuid.uuid4().hex}",
                    "commit_kind": "artifact_only",
                    "operation_ids": [],
                }
            return {
                "commit_id": execution.receipt.commit_id,
                "commit_kind": "document",
                "operation_ids": [operation.id for operation in execution.operations],
            }

        # 同步前下游/叶子测试兼容 adapter。CommandRegistry 已是纯执行器，所以这里
        # 在耐久完成后显式构造 canonical event；生产 bootstrap 永远注入 coordinator。
        produced = producer()
        if produced is None:
            return {
                "commit_id": f"artifact-only:{uuid.uuid4().hex}",
                "commit_kind": "artifact_only",
                "operation_ids": [],
            }
        actions = ()
        if isinstance(produced, CommandMutation):
            operations = produced.operations
            actions = produced.after_commit
        elif isinstance(produced, MutationBatch):
            operations = produced.operations
        else:
            operations = (produced,)
        self._save_then_append(operations)
        for operation in operations:
            payload = to_broadcast_payload(
                operation,
                affected_entities(operation, self._doc),
                doc_id=getattr(self._doc, "id", None),
            )
            self._publish(payload)
        commit_id = f"legacy-file:{uuid.uuid4().hex}"
        for action in actions:
            action.run(commit_id)
        return {
            "commit_id": commit_id,
            "commit_kind": "document",
            "operation_ids": [operation.id for operation in operations],
        }

    def _save_then_append(self, ops) -> None:
        """W03 前兼容 adapter：**先落盘、再按序 append OpLog**。须在 self._lock 内调。

        producer 须让 dispatch 以 op_log=None 调用。崩在 save 与 append 之间 → 文档已存、只少若干
        OpLog 记录(数据不丢、那些 op 不可 undo),而非旧序「先 append 后 save」崩在中间 = OpLog 有、
        document.json 没存(reboot 文档落后于 OpLog、undo 那些 op 把 reverse 应用到没 forward 的文档 =
        数据损坏)。保证 doc ⊇ oplog。ops 内 None(无 update / 无 created_entities)自动跳过。"""
        self._maybe_save()
        for op in ops:
            if op is not None:
                self._op_log.append(op)

    def _maybe_save(self) -> None:
        policy_flag = getattr(self._doc, "should_auto_commit", None)
        # 兼容 W02 之前的 duck-type 文档：旧 with_save 仍保存；真正 Document 一律走
        # should_auto_commit，协作模式不再承担保存开关。
        should_save = (
            bool(policy_flag)
            if policy_flag is not None
            else getattr(self._doc, "collaboration_mode", "") == "with_save"
        )
        if self._save_fn and should_save:
            self._save_fn()

    def _write_manifest(self, st: _RunState) -> None:
        d = scan.run_dir(self._data_dir, st.run_id)
        d.mkdir(parents=True, exist_ok=True)
        # work_list.to_dict() 的超集 + 重建所需的 stage 身份(不含 run callable)+ 并发度。
        # 旧 manifest(只有 work_list 键)仍能被 WorkList.from_dict 读 → 向后兼容。
        manifest = {
            **st.work_list.to_dict(),
            "pipeline_id": st.pipeline_id,
            "start_command_id": st.start_command_id,
            "stages": st.stage_plan.to_meta(),
            "concurrency": st.concurrency,
            "max_retries": st.max_retries,
            "placeholder_item_ids": sorted(st.placeholder_item_ids),
        }
        scan.atomic_write_json(scan.manifest_path(self._data_dir, st.run_id), manifest)

    def _write_error_marker(self, run_id: str, item_id: str, err: dict[str, Any]) -> None:
        p = scan.error_marker_path(self._data_dir, run_id, item_id)
        scan.atomic_write_json(p, err)

    def _clear_error_marker(self, run_id: str, item_id: str) -> None:
        p = scan.error_marker_path(self._data_dir, run_id, item_id)
        if p.exists():
            p.unlink()
