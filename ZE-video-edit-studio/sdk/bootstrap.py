"""sdk/bootstrap.py — server.py 与 scripts/ze_command.py 共用的启动序列.

把 02-mod/05-code 的 register() 声明装成可运行的运行时上下文。框架层, 零业务逻辑——
业务全在被 importlib 载入的 mod 里。

**多项目仓库(账号功能)**:数据目录布局 = `<data_dir>/projects/<project_id>/{document.json,
entities/, assets/}`。`bootstrap()` 返回 `Repository`(全局 UserStore + SessionManager +
按 project_id 懒加载的 `{project_id: Context}` 缓存),不再在启动时建单个 doc。

每个 project 一套**完整独立**的运行时单例(doc / op_log / broker / supervisor /
asset_store / lock / **cmd_reg**)。cmd_reg 也 per-project:mod 的 commands_register 在
register 时把 supervisor 闭包进 batch_generate 命令,共享 cmd_reg 会绑错 project 的
supervisor;每 project 各自 register 一次(mod 纯声明, 毫秒级), 不碰 handler 契约。

账号身份:全局 UserStore(~/.claude/ze_users.json)+ SessionManager(.data/sessions.json)。每个 HTTP
请求由 server 用 `resolve_actor` 解析出 actor;CLI / AI 管线用保留账号 `__system__`。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import threading
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import _paths  # noqa: F401  (注入 sys.path; 必须先于下面的叶子 import)

from sdk import (  # noqa: E402
    Actor, AiGovernor, AssetStore, CommandRegistry, Document, DocumentRegistry, OpLog,
    RealtimeBroker, PipelineSupervisor, register_pipeline_commands, UserStore, SessionManager,
    PolicyStore, AuditLog, NotifyLog, default_system_auth_dir, default_sessions_path,
    ZeError, UndoController, ConfigRegistry, seed_framework_config, IntrospectionMeta, GateRegistry,
    register_manifest_commands, materialize_read_only_sources,
    instantiate_scenario, load_scenario_manifest, scenario_config,
    EffectLedger, IdempotencyStore, CredentialStore,
    credential_context, current_credential_context,
    BrokerEventPublisher, CommitCoordinator, FileCommitStore,
)
from ._contract import INTERFACE_VERSION  # noqa: E402  (信封 source.core_version)
from .hub import InvocationLog  # noqa: E402  (ProviderHub §3.4 调用回执独立幂等日志)
from .assistant_turn import TurnStore  # noqa: E402  (AI 助手 Turn 协议内核 §7.1 P0-A)
from .hub import PreferenceStore, CapabilityLaneStore, DiscoveredModelStore  # noqa: E402  (§3.4 用户偏好 + P4 发现分层)
from .project_topology import (  # noqa: E402
    ProjectHandle, ProjectTopologyCoordinator, TopologyCommit,
    _REPOSITORY_ADMISSION_BOOTSTRAP_CAPABILITY,
)
from document import (  # noqa: E402
    DocumentMetadata,
    open_document_head,
    read_document_metadata,
    validate_document_acl,
)
from media_ticket import MediaTicketSigner  # noqa: E402
import project_git_link  # noqa: E402  (R-C17 项目级 git 关联 sidecar)
from history_authority import (  # noqa: E402
    HistoryAuthority,
    _HISTORY_RETIRE_CAPABILITY,
    _issue_history_bind_capability,
    bind_history_authority,
    retire_history_authority,
)
from undo import _verify_history_replay_derivation  # noqa: E402

ROOT = _paths.ROOT
MOD_CODE = ROOT / "02-mod" / "05-code"
SCHEMA_VERSION = "1.0"
# 保留系统账号:CLI / AI 管线提交用(非空 user_id 过 enforce_login;不经 HTTP ACL)。
SYSTEM_USER_ID = "__system__"
_HISTORY_BOOTSTRAP_OWNER = object()


def _open_project_topology_for_repository(
    projects_root: Path,
    *,
    validate: Callable[[Path], None],
) -> ProjectTopologyCoordinator:
    """Open v5 topology, installing admission only for one proven v4 store.

    The ordinary constructor remains fail-closed when an existing control root
    has no admission record.  Repository bootstrap is the sole production
    caller allowed to retry through the private stop-the-world migration seam,
    and only for that exact pre-v5 diagnosis.  Malformed records, an existing
    install marker, pending/sqlite authority, and every other recovery state
    propagate unchanged.
    """

    try:
        return ProjectTopologyCoordinator(projects_root)
    except ZeError as exc:
        if (
            exc.code != "project_recovery_required"
            or exc.context not in (
                {"reason": "admission_missing"},
                {"reason": "admission_legacy_bootstrap_resume_required"},
            )
        ):
            raise
    return ProjectTopologyCoordinator._bootstrap_existing_legacy_repository_admission(
        projects_root,
        validate=validate,
        _capability=_REPOSITORY_ADMISSION_BOOTSTRAP_CAPABILITY,
    )


def _read_tool_id(root: Path) -> str:
    """deploy.json.skill → deploy.json.name → 目录名。"""
    try:
        raw = json.loads((root / "deploy.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        raw = {}
    if isinstance(raw, dict):
        for key in ("skill", "name"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return root.name


def _demo_sandbox_id(user_id: str, template_id: str) -> str:
    """确定性沙盒 project id:`__demo__<user_hash>__<template_id>`。

    同用户同模板永远同一沙盒 —— 再次打开 = 删旧建新 = 重置(天然满足「重置」语义,无需
    额外重置接口)。user_id 取稳定短哈希避免把账号名直接写进路径。template_id 在注册时已
    校验为 path-safe slug,故拼接后仍过路径穿越护栏。
    """
    h = hashlib.sha1(user_id.encode("utf-8")).hexdigest()[:10]
    return f"__demo__{h}__{template_id}"


@dataclass
class Context:
    """单个 project 的运行时单例集合(每 project 一套, 互相隔离)。"""
    doc: Document
    cmd_reg: CommandRegistry
    op_log: OpLog
    broker: RealtimeBroker
    data_dir: Path                    # = projects/<project_id>/
    doc_path: Path
    actor: Actor                      # supervisor 提交 AI 产物用的系统账号
    lock: "threading.RLock"
    supervisor: PipelineSupervisor
    asset_store: AssetStore
    undo: UndoController
    commit: CommitCoordinator             # 所有 canonical mutation 的唯一提交入口
    history_authority: HistoryAuthority   # 强持进程内 opaque canonical-history lease
    generation: int                       # 项目目录代际；delete/reset 后旧 Context 永久失效
    lifecycle_guard: Callable[[], Any]    # 跨进程共享 lease + generation fence
    schema_diagnosis: dict | None = None  # R-C18:装载时若发现 schema 损坏, 只读诊断报告(加性;None=无损坏)


def data_dir() -> Path:
    env = os.environ.get("ZE_DATA_DIR")
    return Path(env).expanduser().resolve() if env else (ROOT / ".data")


def _load_mod(filename: str):
    """按文件路径 importlib 载入 mod 模块 (不用固定包名, 保持框架对 clone 通用)。

    多文件 mod 支持:把 MOD_CODE(02-mod/05-code)注入 sys.path,使 mod 文件可 `import 同级模块`
    (如 commands_register `import cleanup_ops`)。不注入则 spec_from_file_location 载入的模块没有
    包上下文、同级 import 必 ModuleNotFoundError。加性:单文件 mod 不受影响;幂等(已在则不重复插)。
    """
    path = MOD_CODE / filename
    if str(MOD_CODE) not in sys.path:
        sys.path.insert(0, str(MOD_CODE))
    spec = importlib.util.spec_from_file_location(f"_ze_mod_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法载入 mod 模块: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_mod_optional(filename: str):
    """可选 mod 模块:文件不存在返回 None(config_register.py 对老 mod 可缺省)。"""
    if not (MOD_CODE / filename).exists():
        return None
    return _load_mod(filename)


def _build_ai_governor(config: "ConfigRegistry", moderator: "Callable[[str], bool] | None"):
    """据 ze.ai.* config 构造 AiGovernor(05-ai-coworker);治理未启用 → None。

    返回 None 时 CommandRegistry 不挂 governor、source='ai' 的 dispatch 行为完全不变(向后兼容)——
    所以 governance_enabled 默认 False 是安全 opt-in,不影响存量 skill。

    成本旋钮语义对齐 AiGovernor:其 None 才表示「该项关闭」,而 config 的 0 = 不限/不触发,
    因此 `0 → None` 映射(直接传 0.0 会把上限/阈值卡成 0、拦掉任何正成本命令)。moderator
    是 callable,config 装不下 → 经可选 mod ai_governor_register.py 注入(缺省 → 不做安全过滤)。
    """
    if not bool(config.get("ze.ai.governance_enabled", False)):
        return None
    cap = float(config.get("ze.ai.per_call_cap_usd", 0.0) or 0.0)
    thr = float(config.get("ze.ai.approval_threshold_usd", 0.0) or 0.0)
    return AiGovernor(
        per_call_cap_usd=cap if cap > 0 else None,
        approval_threshold_usd=thr if thr > 0 else None,
        moderator=moderator,
        require_provenance=bool(config.get("ze.ai.require_provenance", True)),
    )


# ==============================================================
# per-project Context 构建
# ==============================================================

def build_project_context(project_id: str, *, projects_root: Path,
                          config: "ConfigRegistry | None" = None,
                          shared_asset_store: "AssetStore | None" = None,
                          gate_registry: "GateRegistry | None" = None,
                          stage_resolver: "Callable[[dict], Any] | None" = None,
                          ai_governor: "AiGovernor | None" = None,
                          notify_log: "NotifyLog | None" = None,
                          user_store: "UserStore | None" = None,
                          credential_store: "CredentialStore | None" = None,
                          policy_store: "PolicyStore | None" = None,
                          tool_id: str = "",
                          effect_ledger: "EffectLedger | None" = None,
                          invocation_log: "InvocationLog | None" = None,
                          turn_store: "TurnStore | None" = None,
                          project_handle: "ProjectHandle | None" = None,
                          lifecycle_guard: "Callable[[], Any] | None" = None) -> Context:
    """为单个已存在的 project 建一套完整运行时单例。

    要求 `projects_root/<project_id>/document.json` 已存在(否则 project_not_found)。
    每 project 独立:broker / op_log / lock / cmd_reg(各自 register mod)/ supervisor /
    asset_store。enforce_login=True —— dispatch 兜底拦空 user_id(server Depends 是第一道)。

    config(skill 级单例)挂到 cmd_reg(同 asset_store 的接法),让 mod 的 stage 经
    `getattr(registry, "config", None)` 读 `ze.llm.*`/`ze.image.*` 调 `sdk.llm_call`/
    `sdk.image_call`(深化点 #1:模型调用驱动的接缝)。
    """
    proj_dir = project_handle.path if project_handle is not None else projects_root / project_id
    generation = project_handle.generation if project_handle is not None else 1
    project_guard = lifecycle_guard or nullcontext
    doc_path = proj_dir / "document.json"
    if not doc_path.exists():
        raise ZeError(
            "project_not_found",
            f"project {project_id!r} has no document.json",
            context={"project_id": project_id},
        )

    broker = RealtimeBroker()
    # OpLog 落盘到 project 目录(NDJSON + hash chain);重启从盘载回 → 操作日志/undo 历史不丢。
    op_log = OpLog(proj_dir / "oplog.ndjson")
    # project 共享锁覆盖 producer→store→event→after-commit action。RLock 只允许同一
    # 协议阶段的辅助函数复用锁；CommitCoordinator 会硬拒绝 producer 内嵌套 execute。
    lock = threading.RLock()
    # AI 管线产物提交用系统账号(source='ai';user_id 非空过 enforce_login)。
    sys_actor = Actor(user_id=SYSTEM_USER_ID, source="ai")

    doc = Document.open(doc_path)
    if doc.id != project_id:
        raise ZeError(
            "project_recovery_required",
            "document identity disagrees with its project namespace",
            context={"project_id": project_id, "reason": "document_id_mismatch"},
        )
    # per-project 资产库;读路径 fallback 到 Repository 级共享池(P3:多项目共享 LoRA/style-ref)。
    asset_store = AssetStore(proj_dir / "assets", fallback=shared_asset_store)

    # P1 只读源物化(互通第二级):mod 声明只在 create_project 的 register 里跑过,
    # Document.open 不恢复源注册 → 每次装载重读声明并刷新物化(会话视图,跟随源 pack 现状)。
    # blob 灌共享池(有则;多项目共享一份),实体 read_only=True 由 Document 机器保证。
    _ro_reg = DocumentRegistry()
    _load_mod("document_register.py").register(_ro_reg)
    # Policies are deployment/runtime authority and intentionally absent from
    # document.json.  Reinstall the current detached declarations on every
    # open; this changes no business bytes or Document version.  The later
    # command-registry check closes referenced ids before recovery starts.
    doc._install_mutation_policy_authority(_ro_reg.mutation_policies)

    # W02 内建策略迁移：新工具的 save_policy 由 mod 单一声明；旧项目缺字段或仍为
    # legacy 时，在任何只读源物化/业务迁移前升级并单独落盘。这样当前 mod 选择
    # checkpoint_commit 时旧项目也会采用，而不是永久停在兼容默认。
    if _ro_reg.save_policy and doc.adopt_save_policy(_ro_reg.save_policy):
        doc.save(doc_path)

    if _ro_reg.read_only_sources:
        materialize_read_only_sources(
            doc, _ro_reg.read_only_sources, base_dir=ROOT,
            asset_store=shared_asset_store or asset_store,
        )

    # on-open 文档迁移(框架机制, 零业务;mod 经 document_register.declare_open_migration 声明):
    # 与只读源物化同源(共用 _ro_reg 声明)。每个钩子 fn(doc)->bool 就地把持久化项目升级到当前
    # 声明(旧字段归一 / 补迁移默认 / 按刷新 schema 重挂校验器);返回 True 表示改了 doc。任一钩子
    # 改动 → 落盘一次(只读物化是会话视图不落盘,迁移是持久升级须落盘)。无声明 → 空循环不 save,
    # 该 skill 装载路径零行为变化。此处在 cmd_reg/supervisor 建立前、Context 未入缓存被共享,故迁移
    # 独占 doc、无需 lock;后续 dispatch 用刷新后的校验器(真项目/demo 首次装载即迁移,再来命中缓存)。
    if _ro_reg.open_migrations:
        _migrated = False
        for _migrate in _ro_reg.open_migrations:
            if _migrate(doc):
                _migrated = True
        if _migrated:
            # A legacy File migration still runs before command/supervisor
            # construction, but it must never persist a graph that already
            # violates the deployment's runtime singleton contract.  The next
            # migration-authority batch will add exact delta authorization and
            # full rollback; this pre-save check closes the immediate durable
            # cardinality window without claiming that broader work is done.
            doc._validate_mutation_policy_cardinality()
            doc.save(doc_path)

    # R-C18 结构诊断:on-open 迁移(处理已知漂移)之后, 对迁移没盖住的**未知损坏**做一次
    # 只读扫描——不 raise、不改 doc、不 fail-closed(全 try/except 兜底, 诊断器崩不影响开门)。
    # 有损坏则落盘 schema_doctor.ndjson(算子可查)并 stash 到 Context, 前端把"要修但不说哪坏"
    # 的死胡同换成逐处诊断卡 + "帮我改"。迁移管已知/诊断管未知, 分工不重叠。
    _schema_diagnosis = _diagnose_schema_on_open(doc, proj_dir)

    # per-project cmd_reg:mod 的 batch_generate 闭包要绑本 project 的 supervisor。
    # ai_governor(skill 级、无状态)挂上后,source='ai' 命令经 provenance/safety/cost/approval 治理;
    # 为 None(治理未启用)时 dispatch 行为不变(向后兼容)。
    cmd_reg = CommandRegistry(enforce_login=True, ai_governor=ai_governor)

    # W03 唯一提交边界：当前用诚实报告部分失败的文件 adapter；W06 换事务 store 时
    # HTTP/MCP/CLI/Pipeline/History 均不再改调用形状。
    commit_store = FileCommitStore(doc_path, op_log)
    commit = CommitCoordinator(
        document=doc,
        store=commit_store,
        publisher=BrokerEventPublisher(broker),
        lock=lock,
        lifecycle_guard=project_guard,
    )

    def _save() -> None:
        with project_guard():
            doc.save(doc_path)

    supervisor = PipelineSupervisor(
        doc=doc, cmd_reg=cmd_reg, op_log=op_log, broker=broker,
        data_dir=proj_dir, actor=sys_actor, lock=lock, save_fn=_save,
        gate_registry=gate_registry,   # skill 级共享资源闸门(P2c-B;mod 经 resource_register 注册合同)
        stage_resolver=stage_resolver, # 重启续跑钩子(P2c-C;mod 经 stage_resolver.resolve 重建计划)
        credential_context_getter=current_credential_context,
        credential_context_factory=credential_context,
        commit_coordinator=commit,
        lifecycle_guard=project_guard,
    )
    cmd_reg.pipeline_supervisor = supervisor          # type: ignore[attr-defined]
    cmd_reg.asset_store = asset_store                 # type: ignore[attr-defined]
    cmd_reg.config = config                           # type: ignore[attr-defined]  (stage 读模型路线)
    cmd_reg.user_store = user_store                   # type: ignore[attr-defined]  (notify.push 解析 ZE 用户)
    cmd_reg.credential_store = credential_store       # type: ignore[attr-defined]  (用户绑定外部账号)
    cmd_reg.policy_store = policy_store               # type: ignore[attr-defined]  (模型/工具权限策略)
    cmd_reg.tool_id = tool_id                         # type: ignore[attr-defined]  (当前 ZE 工具 id)
    cmd_reg.notify_log = notify_log                   # type: ignore[attr-defined]  (notify.push stage 留痕)
    cmd_reg.effect_ledger = effect_ledger             # type: ignore[attr-defined]  (外部副作用显式幂等)
    cmd_reg.invocation_log = invocation_log           # type: ignore[attr-defined]  (AI 调用回执记录,生成派发处)
    cmd_reg.turn_store = turn_store                    # type: ignore[attr-defined]  (AI 助手 Turn 协议 §7.1)
    register_pipeline_commands(cmd_reg, supervisor)   # framework 4 个生命周期 command
    # 信封 P0:manifest.export/import(闭包持本 project 的 asset_store + exports/ 目录)。
    register_manifest_commands(cmd_reg, asset_store=asset_store,
                               exports_dir=proj_dir / "exports",
                               skill_id=ROOT.name, core_version=INTERFACE_VERSION)
    _load_mod("commands_register.py").register(cmd_reg)  # mod 业务命令(本 project 闭包)
    # Runtime policy declarations name business commands, not Python callables.
    # Resolve the complete framework+mod registry before any recovery worker can
    # consume a manifest.  Missing command ids fail activation instead of
    # leaving a policy that can never authorize its promised path.
    doc._validate_mutation_policy_commands(cmd_reg)
    doc._validate_mutation_policy_cardinality()

    # UndoController:undo/can_undo 直读落盘 OpLog(撤销跨重启可用);redo_stack 内存态不跨重启
    # (SPEC §8,重启后 redo 无意义);doc 为反向 apply 目标。绑定发生在 Context 发布和
    # recovery 之前；任何后来临时构造的 log/controller 都不能冒充本 project 的 canonical 历史。
    undo = UndoController(op_log, doc)
    history_authority = bind_history_authority(
        _issue_history_bind_capability(
            bootstrap_owner=_HISTORY_BOOTSTRAP_OWNER,
        ),
        bootstrap_owner=_HISTORY_BOOTSTRAP_OWNER,
        document=doc,
        op_log=op_log,
        undo_controller=undo,
        coordinator=commit,
        store=commit_store,
        project_id=project_id,
        generation=generation,
        runtime_revision=INTERFACE_VERSION,
        replay_verifier=_verify_history_replay_derivation,
    )
    try:
        # 重启续跑(P2c-C):重建上次未完成的 run(无 stage_resolver → no-op)
        supervisor.recover()
    except BaseException as recovery_error:
        # The authority is already path-published but Context is not yet owned
        # by Repository, so normal eviction cleanup cannot see it.  Stop all
        # possible recovery workers first; only a proved stop may unlock the
        # canonical path.  Keep the recovery failure as the primary exception
        # and chain cleanup failure as evidence when cleanup itself is unsafe.
        try:
            stopped = supervisor.shutdown()
        except BaseException as shutdown_error:
            raise recovery_error from shutdown_error
        if not stopped:
            cleanup_error = ZeError(
                "project_recovery_required",
                "failed project recovery left active workers",
                context={
                    "project_id": project_id,
                    "reason": "bootstrap_recovery_shutdown_failed",
                },
            )
            raise recovery_error from cleanup_error
        try:
            retire_history_authority(
                history_authority,
                _capability=_HISTORY_RETIRE_CAPABILITY,
            )
        except BaseException as retirement_error:
            raise recovery_error from retirement_error
        raise

    return Context(
        doc=doc, cmd_reg=cmd_reg, op_log=op_log, broker=broker,
        data_dir=proj_dir, doc_path=doc_path, actor=sys_actor, lock=lock,
        supervisor=supervisor, asset_store=asset_store, undo=undo, commit=commit,
        history_authority=history_authority,
        generation=generation, lifecycle_guard=project_guard,
        schema_diagnosis=_schema_diagnosis,
    )


def _diagnose_schema_on_open(doc: Any, proj_dir: Path) -> dict | None:
    """R-C18:只读诊断装载后的 doc 结构; 有损坏落盘 ndjson 并返回报告。永不 raise、不改 doc。"""
    try:
        from .schema_doctor import diagnose_document
        report = diagnose_document(doc, redact=True, trigger="open")
        if report.get("ok"):
            return None
        try:
            import json as _json
            line = _json.dumps(report, ensure_ascii=False)
            with (proj_dir / "schema_doctor.ndjson").open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass   # 落盘失败不影响返回内存报告
        return report
    except Exception:   # noqa: BLE001  诊断器任何异常都不许拖垮项目加载
        return None


# ==============================================================
# 存量迁移:旧单文档布局 → projects/ 多项目布局
# ==============================================================

def _migrate_legacy_layout(dd: Path, topology: ProjectTopologyCoordinator) -> None:
    """把旧根布局作为一次 journaled create 导入，成功后才清旧源。

    `projects/` 目录存在不再被当作“迁移已完成”的证据；document/entities/assets 先完整复制到
    本操作唯一 staging，Document.open 验证、fsync、原子发布后，journal 才清理旧源。锁序固定
    为 repository EX → legacy content EX；第二个 bootstrap 会在锁内看到 head 已被清理并正常 no-op。
    """
    with topology.legacy_migration_guard():
        entered_head = False
        try:
            with open_document_head(
                dd / "document.json",
                exclusive=True,
                missing_ok=True,
            ) as head:
                entered_head = True
                if head is None:
                    return
                pid = head.metadata.id
                try:
                    migrated_target = topology.path_for(pid)
                except ZeError:
                    raise ZeError(
                        "project_recovery_required",
                        "legacy document identity cannot enter the project namespace",
                        context={"reason": "legacy_project_id_invalid"},
                    ) from None
                if migrated_target.exists():
                    raise ZeError(
                        "project_recovery_required",
                        "legacy source and migrated target both exist",
                        context={"project_id": pid},
                    )

                # Snapshot cleanup names and keep the same content EX lock
                # through safe copy, target publication and source cleanup.
                sources = topology.present_durable_source_paths(dd)

                def _build(staging: Path) -> None:
                    topology.copy_durable_source(dd, staging)

                topology.create(
                    pid,
                    build=_build,
                    validate=lambda root: Document.open(root / "document.json"),
                    kind="legacy_migrate",
                    cleanup_sources=sources,
                )
        except ZeError as exc:
            if entered_head:
                raise
            # A failure before the context yields is not necessarily corrupt
            # legacy content.  Lock contention and an unsupported storage
            # platform are availability/capability facts and must retain their
            # stable 503 diagnostics; only unsafe/invalid head structure is
            # projected to the migration recovery boundary below.
            if exc.code in (
                "document_lock_unavailable",
                "document_storage_unsupported",
            ):
                raise
            raise ZeError(
                "project_recovery_required",
                "legacy document cannot be validated for migration",
                context={"layout": "legacy"},
            ) from exc


# ==============================================================
# Repository — 多项目仓库管理器(bootstrap 返回它)
# ==============================================================

class Repository:
    """全局 UserStore + SessionManager + 按 project_id 懒加载的 Context 缓存。

    server.py 顶层 `_repo = bootstrap()`。每个带 project_id 的路由 `_repo.get_or_load(pid)`
    拿该 project 的 Context。列项目用轻量 `project_meta`(只读 document.json, 不 load Context)。
    """

    def __init__(self, *, data_dir: Path, users_path: str | Path | None = None) -> None:
        self.data_dir = data_dir
        self.projects_root = data_dir / "projects"
        self.tool_id = _read_tool_id(ROOT)
        self.auth_dir = default_system_auth_dir()
        self.user_store = UserStore(path=users_path)     # 默认 ZE-system auth/users.json(全 ZE 共享)
        self.session_mgr = SessionManager(
            path=os.environ.get("ZE_SESSIONS_PATH") or default_sessions_path(),
        )
        self.policy_store = PolicyStore(path=os.environ.get("ZE_POLICIES_PATH"))
        self.auth_audit = AuditLog(path=os.environ.get("ZE_AUTH_AUDIT_PATH"))
        self.credential_store = CredentialStore()
        # Browser-native media URLs receive only a short, asset-scoped ticket.
        # The signer derives from the same durable host key as CredentialStore,
        # so separate workers share verification state.  Key load/create errors
        # intentionally abort bootstrap; a random per-process fallback would
        # create intermittent authorization and make forged-worker boundaries.
        self.media_tickets = MediaTicketSigner(tool_id=self.tool_id)
        self.notify_log = NotifyLog(path=os.environ.get("ZE_NOTIFY_LOG_PATH"))
        self.idempotency_store = IdempotencyStore(data_dir / "idempotency.json")
        self.effect_ledger = EffectLedger(data_dir / "effects.json")
        # AI 调用回执独立日志(ProviderHub §3.4):skill 级单例(计费/审计跨 project),落
        # invocations.json,与 project 的 oplog.ndjson(Operation 栈)物理分离。消费方经
        # _repo.invocation_log 读「最近调用的实际 provider/model」+ 记账;记录接线在生成派发处。
        self.invocation_log = InvocationLog(data_dir / "invocations.json")
        # AI 助手 Turn 协议存储(§7.1 P0-A):skill 级单例,落 assistant_turns.json,与 OpLog 分离
        # (turn 是助手会话协议,整轮撤销 P0-D 另经 change_set 聚合驱动)。P0-B..E 经 cmd_reg.turn_store 用。
        self.turn_store = TurnStore(data_dir / "assistant_turns.json")
        # §3.4 FeaturePreference:用户私有默认模型(resolver preference 私有覆盖读它;不进项目文档、无 secret)。
        self.preference_store = PreferenceStore(data_dir / "ai_preferences.json")
        # §3.4 CapabilityLane:用户对某能力的全局模型优先序(resolver 五级优先第三层)。
        self.lane_store = CapabilityLaneStore(data_dir / "ai_lanes.json")
        # P4 /models 动态发现结果的独立分层落库(source=discovered/caps=[],不进 canonical、不喂 resolver)。
        self.discovered_model_store = DiscoveredModelStore(data_dir / "ai_discovered_models.json")
        self._contexts: dict[str, Context] = {}
        self._reg_lock = threading.RLock()               # 只保护 _contexts dict 的 get/put
        self._current_schema: dict | None = None         # memoize 当前 mod 声明的 data_schema(demo 会话刷新用)
        validate_project = lambda root: Document.open(root / "document.json")
        self.topology = _open_project_topology_for_repository(
            self.projects_root,
            validate=validate_project,
        )
        self.topology.initialize(validate=validate_project)
        _migrate_legacy_layout(data_dir, self.topology)

        # Repository 级内容寻址共享池(P3):多 project 只读共享同一份 blob(LoRA/style-ref 等);
        # 各 project 的 asset_store 读路径 fallback 到此。新空目录、零数据迁移(旧 assets/ 不动)。
        self.shared_asset_store = AssetStore(data_dir / "shared_assets")

        # Repository 级共享资源闸门(P2c-B):GPU/CPU 池等机器级硬上限跨 project 共享同一组槽位;
        # mod 经可选 resource_register.py 注册 ResourceContract(缺省 → 无闸门、不限流)。
        self.gate_registry = GateRegistry()
        _res = _load_mod_optional("resource_register.py")
        if _res is not None and hasattr(_res, "register"):
            _res.register(self.gate_registry)

        # 重启续跑钩子(P2c-C):可选 mod stage_resolver.py 的 resolve(manifest)->StagePlan|None;
        # 缺省 → None → supervisor.recover() no-op(框架自身无此 mod,零影响)。
        _sr = _load_mod_optional("stage_resolver.py")
        self.stage_resolver = getattr(_sr, "resolve", None) if _sr is not None else None

        # ---- 配置中枢(skill 级单例;镜像 doc_reg/cmd_reg 的 seed→mod-register→load 三步)----
        # 框架旋钮(只读展示)+ 通用配置组(LLM 路线 / 图片服务)→ mod 业务旋钮 → .data/config.json 覆盖值。
        self.config = ConfigRegistry()
        seed_framework_config(self.config)
        _mod = _load_mod_optional("config_register.py")
        if _mod is not None and hasattr(_mod, "register"):
            _mod.register(self.config)
        self.config_path = data_dir / "config.json"       # 值落 .data(sync/git 都不碰)
        self.config.load_values(self.config_path)
        self._config_lock = threading.RLock()             # 保护 config 值的并发读改写

        # ---- AI 同事治理(P1 接线;skill 级单例、无状态)----
        # 据 ze.ai.* config 构造 AiGovernor 挂到每 project 的 cmd_reg(get_or_load 透传);
        # 治理未启用(默认)→ None → dispatch 行为不变。moderator(安全过滤 callable)经可选
        # mod ai_governor_register.py 的 `moderator(text)->bool` 注入(缺省 → 不做安全过滤)。
        # 与 gate_registry/stage_resolver 一样在 __init__ 一次构造:ze.ai.* 旋钮在此读取,改
        # 治理启停/阈值经 config 热改后需重启进程才生效(治理是部署级策略,非逐请求开关)。
        _agm = _load_mod_optional("ai_governor_register.py")
        _mod_fn = getattr(_agm, "moderator", None) if _agm is not None else None
        _moderator = _mod_fn if callable(_mod_fn) else None   # 防呆:错配的非 callable 不静默漏进 governor
        self.ai_governor = _build_ai_governor(self.config, _moderator)

        # ---- AI 最低接口声明(skill 级;mod 经 ai_flow_register 声明 ai_flow/manual/编辑器映射)----
        # 缺省优雅降级:无 ai_flow_register.py 时 Context.Describe 返空 ai_flow/manual/editors。
        self.introspect = IntrospectionMeta()
        _ai = _load_mod_optional("ai_flow_register.py")
        if _ai is not None and hasattr(_ai, "register"):
            _ai.register(self.introspect)

        # ---- demo 模板元数据缓存(剔除 bootstrap 函数引用,可直接 JSON 序列化)----
        # 启动时算一次:/api/demo-templates(picker 热路径)每次直接返回缓存,不重复
        # _load_mod+register。open_demo_template(冷路径,显式打开)仍重建注册表取活 bootstrap。
        _demo_reg = DocumentRegistry()
        _load_mod("document_register.py").register(_demo_reg)
        self._demo_templates_meta = [
            {"id": t["id"], "name": t["name"], "description": t["description"],
             "category": t["category"], "business_scene": t["business_scene"]}
            for t in _demo_reg.demo_templates
        ]

    def save_config(
        self,
        updates: dict,
        *,
        expected_version: int,
    ) -> list:
        """用调用方读到的 revision 原子校验并持久化一批配置 override。

        ConfigRegistry 负责 candidate→耐久文件→内存 swap 的完整边界；这里不再把
        ``set_values`` 与 ``save_values`` 拼成两个可能分叉的步骤。ops 层异常不依赖
        core 的 ZeError，为了让 HTTP/MCP 等 surface 得到同一个稳定错误信封，在门面
        处只转换带 ``config_*`` 稳定 code 的已知配置错误。该生产门面不允许省略
        ``expected_version``；无需 CAS 的启动迁移直接使用 ConfigRegistry 的专用入口。
        """
        try:
            with self._config_lock:
                return self.config.update_values(
                    self.config_path,
                    updates,
                    expected_version=expected_version,
                )
        except Exception as exc:
            code = getattr(exc, "code", "")
            if isinstance(code, str) and code.startswith("config_"):
                raise ZeError(
                    code,
                    str(exc) or code,
                    context=dict(getattr(exc, "context", {}) or {}),
                ) from exc
            raise

    def _project_dir(self, project_id: str) -> Path:
        """project_id → live namespace path；路径/控制命名空间校验由 topology 唯一拥有。"""
        return self.topology.path_for(project_id)

    # ---- 项目枚举 / 元信息(轻量, 不 load Context) ----

    def list_project_ids(self) -> list[str]:
        return self.topology.list_live_ids()

    @staticmethod
    def _project_meta_from_handle(handle: ProjectHandle) -> dict:
        project_id = handle.project_id
        metadata = read_document_metadata(handle.path / "document.json")
        if metadata.id != project_id:
            raise ZeError(
                "project_recovery_required",
                "project metadata identity disagrees with its namespace",
                context={"project_id": project_id, "reason": "document_id_mismatch"},
            )
        acl = validate_document_acl(
            metadata.acl,
            error_code="project_recovery_required",
            context={"project_id": project_id},
        )
        config = metadata.config
        return {
            "id": project_id,
            "name": metadata.name or project_id,
            "acl": acl,
            "generation": handle.generation,
            "document_version": metadata.version,
            "updated_at": metadata.updated_at,
            "config": config,
            # R-C17 项目卡 git 徽标:纯存在性探针(零解析)+ 内部全兜底 —— 一个坏
            # sidecar 不许 500 整个项目列表(此回调跑在 snapshot_live 逐项目热路径上)。
            "git_linked": project_git_link.link_exists(handle.path),
        }

    def list_project_metas(self) -> list[dict]:
        """Return one topology-stable catalog snapshot, including generation."""
        return self.topology.snapshot_live(self._project_meta_from_handle)

    def project_meta(self, project_id: str) -> dict:
        """在 live generation lease 下读元信息；损坏时 fail closed，绝不退化为公共 ACL。"""
        with self.topology.open_live(project_id) as handle:
            return self._project_meta_from_handle(handle)

    def project_exists(self, project_id: str) -> bool:
        try:
            with self.topology.open_live(project_id):
                return True
        except ZeError:
            return False

    # ---- 项目级 git 关联(R-C17;sidecar 在 Document 之外, 读写走 live lease) ----

    def read_project_git_link(self, project_id: str) -> dict | None:
        with self.topology.open_live(project_id) as handle:
            record = project_git_link.read_link(handle.path)
            return record.to_public_dict() if record else None

    def set_project_git_link(
        self, project_id: str, *, owner: str, repo: str, ref: str = "",
        actor_user_id: str = "",
    ) -> dict:
        with self.topology.open_live(project_id) as handle:
            record = project_git_link.write_link(
                handle.path, owner=owner, repo=repo, ref=ref,
                updated_by=actor_user_id,
            )
            return record.to_public_dict()

    def clear_project_git_link(self, project_id: str) -> bool:
        with self.topology.open_live(project_id) as handle:
            return project_git_link.delete_link(handle.path)

    # ---- Context 懒加载 ----

    @staticmethod
    def _assert_context_head_current(
        project_id: str,
        ctx: Context,
        metadata: DocumentMetadata,
    ) -> None:
        cached_id = ctx.doc.id
        cached_version = int(ctx.doc.version)
        if metadata.id != project_id or cached_id != project_id:
            raise ZeError(
                "project_recovery_required",
                "project context identity disagrees with its durable head",
                context={
                    "project_id": project_id,
                    "reason": "document_id_mismatch",
                },
            )
        if metadata.version != cached_version:
            # Do not auto-reload here.  FileCommitStore intentionally commits
            # Document before OpLog, so a same-generation head advance can be a
            # recoverable half-commit window.  Rebuilding would bless new
            # Document + old history; SQLite authority will make reload safe.
            raise ZeError(
                "commit_repair_required",
                "project context no longer matches the durable commit head",
                context={
                    "project_id": project_id,
                    "cached_document_version": cached_version,
                    "durable_document_version": metadata.version,
                    "reason": "same_generation_document_drift",
                },
            )

    def _retire_unpublished_context(self, project_id: str, ctx: Context) -> None:
        """Stop a cache-miss Context that failed its final durable-head fence."""
        try:
            stopped = ctx.supervisor.shutdown(join_timeout=self.topology.lock_timeout)
        except BaseException as exc:
            raise ZeError(
                "project_recovery_required",
                "an unpublished project context could not be retired safely",
                context={
                    "project_id": project_id,
                    "reason": "unpublished_context_retirement_failed",
                },
            ) from exc
        if not stopped:
            raise ZeError(
                "project_recovery_required",
                "an unpublished project context still has active workers",
                context={
                    "project_id": project_id,
                    "reason": "unpublished_context_retirement_failed",
                },
            )
        try:
            retire_history_authority(
                ctx.history_authority,
                _capability=_HISTORY_RETIRE_CAPABILITY,
            )
        except BaseException as exc:
            raise ZeError(
                "project_recovery_required",
                "an unpublished project history authority could not be retired",
                context={
                    "project_id": project_id,
                    "reason": "unpublished_history_retirement_failed",
                },
            ) from exc

    @staticmethod
    def _retire_evicted_history(
        project_id: str,
        evicted: dict[str, Context | None],
    ) -> None:
        """Release canonical path authority only after topology commit wins."""
        ctx = evicted.get("ctx")
        if ctx is None:
            return
        try:
            retire_history_authority(
                ctx.history_authority,
                _capability=_HISTORY_RETIRE_CAPABILITY,
            )
        except BaseException as exc:
            raise ZeError(
                "project_recovery_required",
                "evicted project history authority could not be retired",
                context={
                    "project_id": project_id,
                    "reason": "evicted_history_retirement_failed",
                },
            ) from exc

    def get_or_load(self, project_id: str) -> Context:
        """Return a generation/current-head-consistent local Context.

        Same-generation durable drift is deliberately availability-fail-closed:
        the file adapter cannot prove Document+OpLog atomicity or safely retire
        active request leases, so this process reports ``commit_repair_required``
        and retains (but never returns) the old cache entry.  Automatic reload
        remains unavailable until the transactional SQLite authority lands.
        """
        with self.topology.open_live(project_id) as handle:
            while True:
                with self._reg_lock:
                    ctx = self._contexts.get(project_id)
                    if ctx is not None and ctx.generation != handle.generation:
                        self._contexts.pop(project_id, None)
                        ctx = None
                    if ctx is None:
                        guard = lambda _handle=handle: self.topology.activity(_handle)
                        ctx = build_project_context(
                            project_id, projects_root=self.projects_root,
                            config=self.config,
                            shared_asset_store=self.shared_asset_store,
                            gate_registry=self.gate_registry,
                            stage_resolver=self.stage_resolver,
                            ai_governor=self.ai_governor,
                            notify_log=self.notify_log,
                            user_store=self.user_store,
                            credential_store=self.credential_store,
                            policy_store=self.policy_store,
                            tool_id=self.tool_id,
                            effect_ledger=self.effect_ledger,
                            invocation_log=self.invocation_log,
                            turn_store=self.turn_store,
                            project_handle=handle,
                            lifecycle_guard=guard,
                        )
                        # The build can perform substantial recovery/registry
                        # work after Document.open.  Another process may commit
                        # a newer same-generation head during that interval.  A
                        # newly built Context is not publishable until one final
                        # durable id/version fence succeeds; on failure it is
                        # stopped and never enters the cache.
                        try:
                            with ctx.lock:
                                metadata = read_document_metadata(
                                    handle.path / "document.json",
                                )
                                self._assert_context_head_current(
                                    project_id, ctx, metadata,
                                )
                        except BaseException as original_error:
                            try:
                                self._retire_unpublished_context(project_id, ctx)
                            except BaseException as retirement_error:
                                raise retirement_error from original_error
                            raise
                        self._contexts[project_id] = ctx
                        return ctx

                # Never hold registry -> Context locks: a legitimate local
                # commit owns ctx.lock while Document.save advances both disk
                # and ctx.doc.  Wait for that coherent point, then re-check the
                # cache mapping after releasing ctx.lock.
                with ctx.lock:
                    metadata = read_document_metadata(handle.path / "document.json")
                    self._assert_context_head_current(project_id, ctx, metadata)
                with self._reg_lock:
                    if self._contexts.get(project_id) is not ctx:
                        continue
                    if ctx.generation != handle.generation:
                        continue
                    return ctx

    def _quiesce_cached_context(
        self,
        project_id: str,
        evicted: dict[str, Context | None],
        timeout: float,
    ) -> bool:
        """Called only after topology state became quiescing, so no new load can enter."""
        with self._reg_lock:
            ctx = self._contexts.pop(project_id, None)
        evicted["ctx"] = ctx
        if ctx is None:
            return True
        stopped = ctx.supervisor.shutdown(join_timeout=timeout)
        if not stopped:
            with self._reg_lock:
                self._contexts.setdefault(project_id, ctx)
        return stopped

    def _restore_evicted_context_if_current(
        self,
        project_id: str,
        evicted: dict[str, Context | None],
    ) -> None:
        ctx = evicted.get("ctx")
        if ctx is None:
            return
        try:
            with self.topology.open_live(project_id) as handle:
                if handle.generation != ctx.generation:
                    return
        except ZeError:
            return
        with self._reg_lock:
            self._contexts.setdefault(project_id, ctx)

    # ---- 新建 project ----

    def create_project(
        self, *, name: str, owner: str, seed: bool = False,
        project_id: str | None = None,
        intent_id: str = "",
        return_commit: bool = False,
    ) -> str | TopologyCommit:
        """新建 project:owner=me + 可选 demo seed。返回 project_id。

        owner 写进 acl;collaborators 初始空。seed=True 跑 mod 声明的 demo bootstrap
        (把示例数据灌进**该用户自己**的 project, 不破坏隔离)。
        """
        doc_reg = DocumentRegistry()
        _load_mod("document_register.py").register(doc_reg)
        pid = project_id or uuid.uuid4().hex[:12]

        def _build(staging: Path) -> None:
            config = {"project_create_intent_id": intent_id} if intent_id else None
            doc = doc_reg.build_document(
                schema_version=SCHEMA_VERSION,
                id=pid,
                name=name,
                config=config,
                acl={"owner": owner, "collaborators": []},
            )
            if seed and doc_reg.demo:
                doc_reg.demo["bootstrap"](doc)
            doc._validate_mutation_policy_cardinality()
            doc.save(staging / "document.json")

        commit = self.topology.create(
            pid,
            build=_build,
            validate=lambda root: Document.open(root / "document.json"),
            operation_id=intent_id or None,
            principal_id=owner,
        )
        return commit if return_commit else pid

    def current_data_schema(self) -> dict:
        """当前代码声明的 data_schema(从 mod 注册表现取,memoize)。给 demo 会话刷新 schema 用 ——
        demo 文档把 schema 固化进 document.json,部署新代码后旧 demo 会话仍用旧 schema(如新增模型 enum
        渲不成下拉)。/api/document 对 demo 会话用它覆盖 doc.schema:渲染规则随代码更新、实体数据保留不动
        (不重置、不丢用户在示例里的调试)。普通项目仍用各自固化 schema(尊重 schema_version 迁移语义)。"""
        if self._current_schema is None:
            dr = DocumentRegistry()
            _load_mod("document_register.py").register(dr)
            self._current_schema = dr.build_document(schema_version=SCHEMA_VERSION, name="_schema_probe").schema
        return self._current_schema

    # ---- demo 模板(一等文件系统能力:多模板 catalog + 隐藏可重置沙盒)----

    def demo_templates(self) -> list[dict]:
        """列出 mod 声明的 demo 模板元数据(启动时缓存,剔除 bootstrap 函数引用)。"""
        return [dict(t) for t in self._demo_templates_meta]

    def demo_sandbox_id(self, actor_user_id: str, template_id: str) -> str:
        """Return the deterministic hidden project id without mutating storage."""
        return _demo_sandbox_id(actor_user_id, template_id)

    def project_intent_status(
        self,
        project_id: str,
        intent_id: str,
        *,
        kind: str | None = None,
        principal_id: str | None = None,
    ) -> dict[str, Any]:
        """Reconcile a lost topology response against durable project evidence."""
        return self.topology.intent_status(
            project_id, intent_id, kind=kind, principal_id=principal_id,
        )

    def demo_template_content_version(self, template_id: str) -> str:
        """demo 模板内容版本 = scenario manifest 文件内容哈希(无 scenario → "bootstrap")。
        给 demo.open 幂等 scope 用:内容变(如新增物体参考图)→ 版本变 → 重开缓存未命中 → 删旧建新拿
        新内容;内容不变仍幂等(不抖)。修「访客第一次开 demo 后被幂等冻结在旧内容上,后续部署的新内容
        (scenario 增减实体/参考图)永远进不来,刷新回登录页重进还是命中同一缓存的旧会话」。
        scenario_path 不在公开的 _demo_templates_meta(已剥),故按 open_demo_template 同法从注册表现取。"""
        import hashlib
        doc_reg = DocumentRegistry()
        _load_mod("document_register.py").register(doc_reg)
        tpl = next((t for t in doc_reg.demo_templates if t["id"] == template_id), None)
        sp = (tpl or {}).get("scenario_path") or ""
        if sp and Path(sp).exists():
            return hashlib.sha256(Path(sp).read_bytes()).hexdigest()[:16]
        return "bootstrap"

    def open_demo_template(
        self,
        *,
        actor_user_id: str,
        template_id: str,
        intent_id: str = "",
        expected_generation: int | None = None,
        return_commit: bool = False,
    ) -> str | TopologyCommit:
        """打开(或重置)一个 demo 模板:确定性隐藏沙盒,删旧建新 = 重置。返回 project_id。

        沙盒 config 写 `project_kind=demo_session`(从普通项目列表过滤;另存为时剥除变普通
        项目)。模板两源:bootstrap(doc) 兜底 / scenario_path 数据化(manifest + $asset 真资产,
        缺资产显式报错)。owner = 操作者(灌进该用户自己的隐藏沙盒,不破坏多账号隔离)。
        """
        doc_reg = DocumentRegistry()
        _load_mod("document_register.py").register(doc_reg)
        tpl = next((t for t in doc_reg.demo_templates if t["id"] == template_id), None)
        if tpl is None:
            raise ZeError("not_found", f"demo template {template_id!r} 不存在",
                          context={"template_id": template_id})

        sandbox_id = _demo_sandbox_id(actor_user_id, template_id)

        # 数据化模板:先在锁外读 manifest(只读 I/O,不碰共享态)。
        scenario_path = tpl.get("scenario_path") or ""
        manifest = None
        scenario_dir = None
        extra_config: dict = {}
        if scenario_path:
            manifest = load_scenario_manifest(scenario_path)
            extra_config = scenario_config(manifest)
            scenario_dir = Path(scenario_path).parent

        config = {
            **extra_config,
            "project_kind": "demo_session",
            "demo_template_id": template_id,
            "demo_reset_intent_id": intent_id,
            "demo_reset_at": datetime.now(timezone.utc).isoformat(),
        }

        def _build(staging: Path) -> None:
            doc = doc_reg.build_document(
                schema_version=SCHEMA_VERSION,
                id=sandbox_id,
                name=tpl.get("name") or template_id,
                config=config,
                acl={"owner": actor_user_id, "collaborators": []},
            )
            if scenario_path:
                asset_store = AssetStore(staging / "assets", fallback=self.shared_asset_store)
                instantiate_scenario(doc, manifest, scenario_dir=scenario_dir, asset_store=asset_store)
            elif tpl.get("bootstrap"):
                tpl["bootstrap"](doc)
            doc._validate_mutation_policy_cardinality()
            doc.save(staging / "document.json")

        def _verify_existing(live: Path) -> None:
            metadata = read_document_metadata(live / "document.json")
            if metadata.id != sandbox_id:
                raise ZeError(
                    "project_recovery_required",
                    "existing demo sandbox identity is invalid",
                    context={"project_id": sandbox_id, "reason": "document_id_mismatch"},
                )
            current_config = metadata.config
            current_acl = validate_document_acl(
                metadata.acl,
                error_code="project_recovery_required",
                context={"project_id": sandbox_id},
            )
            if (
                current_config.get("project_kind") != "demo_session"
                or current_config.get("demo_template_id") != template_id
                or current_acl.get("owner") != actor_user_id
            ):
                raise ZeError(
                    "project_topology_conflict",
                    "deterministic demo id belongs to a different project",
                    context={"project_id": sandbox_id},
                )

        evicted: dict[str, Context | None] = {}
        try:
            commit = self.topology.replace(
                sandbox_id,
                build=_build,
                validate=lambda root: Document.open(root / "document.json"),
                verify_existing=_verify_existing,
                quiesce=lambda timeout: self._quiesce_cached_context(
                    sandbox_id, evicted, timeout,
                ),
                operation_id=intent_id or None,
                expected_generation=expected_generation,
                principal_id=actor_user_id,
            )
        except BaseException:
            self._restore_evicted_context_if_current(sandbox_id, evicted)
            raise
        self._retire_evicted_history(sandbox_id, evicted)
        return commit if return_commit else sandbox_id

    # ---- 复制 / 删除 project(header-v2 文件切换器,SPEC §2.3.5.9/5.10)----

    def duplicate_project(self, src_project_id: str, *, owner: str,
                          name: str | None = None,
                          project_id: str | None = None,
                          intent_id: str = "",
                          expected_generation: int | None = None,
                          verify_source: Callable[[Path], None] | None = None,
                          principal_id: str = "",
                          return_commit: bool = False) -> str | TopologyCommit:
        """复制 project 的 canonical document/entities/assets，并重写身份与 ACL。

        oplog、pipeline attempt/status、exports、临时文件都不是副本业务数据，不进入新项目。
        源/目标在跨进程生命周期锁下形成一个完整快照，目标只在最终 rename 后可见。
        """
        pid = project_id or uuid.uuid4().hex[:12]

        def _rewrite(staging: Path) -> None:
            metadata = read_document_metadata(staging / "document.json")
            if metadata.id != src_project_id:
                raise ZeError(
                    "project_recovery_required",
                    "duplicate source identity disagrees with its namespace",
                    context={
                        "project_id": src_project_id,
                        "reason": "document_id_mismatch",
                    },
                )
            duplicate_name = name if name else f"{metadata.name or src_project_id} 副本"
            # 另存为普通项目:源是 demo 沙盒 → 剥掉 demo 元数据,副本变普通可见项目
            # (用户保留 demo 编辑成果的唯一途径;真图随目录 copy 带走)。
            cfg = metadata.config
            if cfg.get("project_kind") == "demo_session":
                for k in ("project_kind", "demo_template_id", "demo_reset_at"):
                    cfg.pop(k, None)
            cfg.pop("demo_reset_intent_id", None)
            if intent_id:
                cfg["project_duplicate_intent_id"] = intent_id
                cfg["project_duplicate_source_id"] = src_project_id
            self.topology._rewrite_staged_document_metadata(
                staging,
                expected_version=metadata.version,
                id=pid,
                name=duplicate_name,
                acl={"owner": owner, "collaborators": []},
                config=cfg,
            )

        commit = self.topology.duplicate(
            src_project_id,
            pid,
            rewrite=_rewrite,
            validate=lambda root: Document.open(root / "document.json"),
            operation_id=intent_id or None,
            expected_generation=expected_generation,
            verify_source=verify_source,
            principal_id=principal_id or owner,
        )
        return commit if return_commit else pid

    def delete_project(self, project_id: str, *, intent_id: str = "",
                       expected_generation: int | None = None,
                       verify_existing: Callable[[Path], None] | None = None,
                       principal_id: str = "",
                       return_commit: bool = False) -> TopologyCommit | None:
        """逻辑删除 project：quiesce 后 live→唯一 trash，物理 GC 只是提交后清理。

        owner-only 与手打全名红线由 server 层校验(SPEC §2.3.5.9);本方法只做路径护栏 + 有序删除。
        """
        evicted: dict[str, Context | None] = {}
        try:
            commit = self.topology.delete(
                project_id,
                quiesce=lambda timeout: self._quiesce_cached_context(
                    project_id, evicted, timeout,
                ),
                operation_id=intent_id or None,
                expected_generation=expected_generation,
                verify_existing=verify_existing,
                principal_id=principal_id,
            )
        except BaseException:
            self._restore_evicted_context_if_current(project_id, evicted)
            raise
        self._retire_evicted_history(project_id, evicted)
        return commit if return_commit else None


def bootstrap() -> Repository:
    """启动入口:建多项目仓库管理器(不在启动时 load 任何 project, 空仓库合法)。

    用户表默认全局 ~/.claude/ze_users.json(跨 skill 共享账号);$ZE_USERS_PATH 可覆盖(测试隔离)。
    """
    return Repository(data_dir=data_dir(), users_path=os.environ.get("ZE_USERS_PATH"))
