"""sdk/introspect.py — AI 最低接口的只读 reader 层(三接口元件的 AI 面).

图景 `ZE-skill-system/modules/ai-native-flow-components.md` §6:每个 ZE work skill
必须让**别的 AI 不读前端源码**就能理解、操作它。本模块把"理解"侧(只读 introspection)
收成一组纯 reader:server 路由与 `scripts/ze_command.py` CLI 共用同一 reader,
保证 API 与 CLI **同形返回**(图景"AI 的左手")。

红线:reader 只读 —— 返回 dict、**不产生 Operation、不写 OpLog、不改 canonical**。
写仍走命令总线(`Command.Run` = `CommandRegistry.dispatch`)。

鸭子类型:本模块**不 import 任何叶子类**(避免与 sdk barrel 循环),只操作传入对象。
"""

from __future__ import annotations

from typing import Any

from ._operation_projection import public_oplog_entries
from .guided_tour import compile_guided_tours, guided_tour_summaries


# ==============================================================
# IntrospectionMeta — mod 经 02-mod/05-code/ai_flow_register.py 声明
# ==============================================================

class IntrospectionMeta:
    """skill 级 introspection 声明(ai_flow / manual / 编辑器映射).

    mod 的 `ai_flow_register.register(meta)` 调本类 setter 填;缺省则各 reader
    优雅降级(空 ai_flow / 空 manual / 空 editors)。挂在 Repository(skill 级单例)。

    编辑器映射是 04-layout.config.ts(TS,Python 读不到)的 Python 镜像:只声明
    Editor.List 需要的几个字段(slot / editor_class / bind / read_sources / view_state),
    可运行命令由 core 计算(touches ∩ bind),不必 mod 重复声明。
    """

    def __init__(self) -> None:
        self._ai_flow: dict[str, Any] = {}
        self._manual: str = ""
        self._editors: list[dict[str, Any]] = []
        self._pipelines: list[dict[str, Any]] = []
        self._tool_understanding: dict[str, Any] | None = None

    def set_ai_flow(self, flow: dict[str, Any]) -> None:
        self._ai_flow = dict(flow or {})

    def set_tool_understanding(self, record: dict[str, Any] | None) -> None:
        """声明 U0 ``ze.tool-understanding.v1`` 实例(可选;缺省走 guided_tour 回退派生)。

        guided_tour(系统 3a)以此为**首选源**投影引导步;无实例时各 reader 从
        ai_flow/说明书兜底,保证全系列可推。
        """
        self._tool_understanding = dict(record) if isinstance(record, dict) else None

    def set_manual(self, markdown: str = "") -> None:
        self._manual = str(markdown or "")

    def declare_editors(self, editors: list[dict[str, Any]]) -> None:
        self._editors = [dict(e) for e in (editors or [])]

    def declare_pipelines(self, pipelines: list[dict[str, Any]]) -> None:
        self._pipelines = [dict(p) for p in (pipelines or [])]

    @property
    def ai_flow(self) -> dict[str, Any]:
        return dict(self._ai_flow)

    @property
    def manual(self) -> str:
        return self._manual

    @property
    def editors(self) -> list[dict[str, Any]]:
        return [dict(e) for e in self._editors]

    @property
    def pipelines(self) -> list[dict[str, Any]]:
        return [dict(p) for p in self._pipelines]

    @property
    def tool_understanding(self) -> dict[str, Any] | None:
        return dict(self._tool_understanding) if self._tool_understanding else None


# editor class 是否需 mod 注入重渲染器(core 不打包重库;见 FUTURE_STATE §3 / §5)
_RENDERER_REQUIRED = {"scene3d", "chart"}


# ==============================================================
# 命令(Command.List / Command.Describe)
# ==============================================================

def _command_value(spec: Any, name: str, default: Any) -> Any:
    if type(spec) is dict:
        return spec.get(name, default)
    return getattr(spec, name, default)


def _command_brief(spec: Any) -> dict[str, Any]:
    return {
        "id": _command_value(spec, "id", ""),
        "label": _command_value(spec, "label", ""),
        "kind": _command_value(spec, "kind", "native"),
        "phase": _command_value(spec, "phase", 1),
        "touches": list(_command_value(spec, "touches", []) or []),
        # AI 通道声明('image'/'llm'/…):随命令下发,前端据此对入口按钮做路线门控
        # (未声明=""→加性零影响,见 command.py requires_route)。
        "requires_route": _command_value(spec, "requires_route", ""),
        "sources_allowed": list(
            _command_value(spec, "sources_allowed", []) or []
        ),
        "shortcut": _command_value(spec, "shortcut", ""),
        "description": _command_value(spec, "description", "") or "",
        "outputs_schema": dict(
            _command_value(spec, "outputs_schema", {}) or {}
        ),
        # 操作台地基:管线 StartConsole 等 schema 驱动参数表单消费(与 outputs_schema 同款下发)。
        "params_schema": dict(_command_value(spec, "params_schema", {}) or {}),
        "ui_ports": dict(_command_value(spec, "ui_ports", {}) or {}),
        # 两段式确认:AI/UI 提前知该命令 dispatch 后返回 PendingConfirmation 而非直接执行。
        "requires_confirm": bool(
            _command_value(spec, "requires_confirm", False)
        ),
    }


def list_commands(cmd_reg: Any) -> list[dict[str, Any]]:
    """Command.List — 列出所有注册命令(brief)。"""
    declarations = getattr(cmd_reg, "registered_declarations", None)
    if callable(declarations):
        specs = declarations()
    else:
        # Duck-typed compatibility for test/fake registries.  Production
        # CommandRegistry always provides the registration-time projection.
        specs = cmd_reg.list()
    return [
        _command_brief(spec)
        for spec in sorted(
            specs, key=lambda value: _command_value(value, "id", ""),
        )
    ]


def describe_command(cmd_reg: Any, command_id: str) -> dict[str, Any]:
    """Command.Describe — 单命令完整声明(含 params_schema / scope / acl)。"""
    declaration = getattr(cmd_reg, "registered_declaration", None)
    if callable(declaration):
        spec = declaration(command_id)
    else:
        # 不存在抛 unknown_command(由 caller 转 HTTP/CLI 错)
        spec = cmd_reg.get(command_id)
    out = _command_brief(spec)
    out.update({
        "conflict_strategy": _command_value(spec, "conflict_strategy", ""),
        "scheduling": _command_value(spec, "scheduling", ""),
        "acl": dict(_command_value(spec, "acl", {}) or {}),
        "params_schema": dict(_command_value(spec, "params_schema", {}) or {}),
        "outputs_schema": dict(
            _command_value(spec, "outputs_schema", {}) or {}
        ),
        "ui_ports": dict(_command_value(spec, "ui_ports", {}) or {}),
        "has_poll": bool(_command_value(spec, "has_poll", False))
        if type(spec) is dict
        else getattr(spec, "poll", None) is not None,
        "confirm_template": _command_value(spec, "confirm_template", "") or "",
    })
    return out


# ==============================================================
# 实体(Entity.List / Entity.Select)
# ==============================================================

def _doc_entity_types(doc: Any) -> list[str]:
    return sorted(set(doc.schema.keys()) | set(doc.types()))


def list_entities(doc: Any, entity_type: str | None = None,
                  *, include_data: bool = False) -> dict[str, list[dict[str, Any]]]:
    """Entity.List — 轻量列实体(id/name/version/deleted)。

    ``include_data=True`` 时额外回实体的完整业务 ``data``(单实体读的通道)。
    **权限不在此处判**:reader 只投影 doc,ACL/owner 校验由调用方(server `_require_member`
    / mcp `_member_context`)在**取到本 doc 之前**完成 —— 拿到 doc 即已过项目级 ACL,
    include_data 复用同一道门,绝不新开旁路。
    """
    types = [entity_type] if entity_type else _doc_entity_types(doc)
    out: dict[str, list[dict[str, Any]]] = {}
    for et in types:
        rows: list[dict[str, Any]] = []
        try:
            entities = doc.list(et)
        except Exception:
            entities = []
        for e in entities:
            d = e.to_dict()
            row = {
                "id": d.get("id"),
                "name": d.get("name", ""),
                "version": d.get("version", d.get("_v", 1)),
                "deleted": bool(d.get("deleted", False)),
            }
            if include_data:
                row["data"] = d.get("data", {})
            rows.append(row)
        out[et] = rows
    return out


def entity_select(doc: Any, entity_type: str, entity_id: str) -> dict[str, Any]:
    """Entity.Select — 只校验存在(view-state 概念;不改 canonical / 不落 op)。

    用 doc.get(type,id)(O(1));不存在抛 entity_not_found → 捕获为 exists=False。
    """
    try:
        doc.get(entity_type, entity_id)
        exists = True
    except Exception:
        exists = False
    return {"selected": {"type": entity_type, "id": entity_id}, "exists": exists}


# ==============================================================
# 编辑器(Editor.List)
# ==============================================================

def list_editors(introspect: IntrospectionMeta, cmd_reg: Any) -> list[dict[str, Any]]:
    """Editor.List — mod 声明的编辑器映射 + core 算可运行命令 + renderer 注入需求。"""
    cmds = list_commands(cmd_reg)
    out: list[dict[str, Any]] = []
    for ed in introspect.editors:
        bind = list(ed.get("bind", []) or [])
        bind_set = set(bind)
        runnable = sorted(c["id"] for c in cmds if bind_set & set(c["touches"]))
        ec = ed.get("editor_class", "")
        out.append({
            "slot": ed.get("slot", ""),
            "editor_class": ec,
            "bind": bind,
            "read_sources": list(ed.get("read_sources", bind) or bind),
            "view_state": dict(ed.get("view_state", {}) or {}),
            "needs_renderer": ec in _RENDERER_REQUIRED,
            "runnable_commands": runnable,
        })
    return out


def list_pipelines(introspect: IntrospectionMeta) -> list[dict[str, Any]]:
    """Pipeline.StaticMeta — mod 声明的固定 pipeline/stage/cell 静态信息。"""
    return introspect.pipelines


# ==============================================================
# 任务 / 管线(Task.List / Task.Status)
# ==============================================================

def list_tasks(supervisor: Any) -> list[dict[str, Any]]:
    """Task.List — 异步执行(管线 run)快照。"""
    return list(supervisor.snapshot())


def task_status(supervisor: Any, run_id: str) -> dict[str, Any]:
    """Task.Status — 单 run 状态 + 其 entity 级 item 状态。"""
    run = next((r for r in supervisor.snapshot() if r.get("id") == run_id), None)
    items = [s for s in supervisor.entity_status_snapshot() if s.get("run_id") == run_id]
    return {"run": run, "items": items, "found": run is not None}


# ==============================================================
# 操作日志(OperationLog.Query)
# ==============================================================

def query_oplog(op_log: Any, *, entity_type: str | None = None,
                entity_id: str | None = None, source: str | None = None,
                limit: int | None = None) -> dict[str, Any]:
    """OperationLog.Query — 过滤审计轨迹 + 链完整性。

    entity_type+entity_id → 按实体;否则 source → 按来源;否则全量。limit 取末 N 条。
    ``verified`` 是服务端对完整 canonical history 的校验；若 entry 标记
    ``canonical_body_redacted``，其公开 ``op`` 不能用于重算 ``self_hash``。
    """
    rows = public_oplog_entries(
        op_log,
        entity_type=entity_type,
        entity_id=entity_id,
        source=source,
        limit=limit,
    )
    return {"entries": rows, "count": len(rows),
            "total": len(op_log), "verified": op_log.verify_chain()}


# ==============================================================
# 配置 / 说明书(Settings.Read / Help.ReadManual)
# ==============================================================

def settings_read(config: Any) -> dict[str, Any]:
    """Settings.Read — 运行配置(secret 已脱敏,复用 ConfigRegistry.snapshot)。"""
    return {"items": config.snapshot() if config is not None else []}


def read_manual(introspect: IntrospectionMeta, *, offset: int = 0,
                limit: int | None = None) -> dict[str, Any]:
    """Help.ReadManual — mod 声明的人类说明书(markdown;AI/人共读)。

    大文档支持字符分页:``offset``/``limit`` 切片,回 ``total``/``has_more``,
    AI 可分次读全文(不传则全文,向后兼容)。
    """
    content = introspect.manual
    total = len(content)
    try:
        start = max(0, int(offset or 0))
    except (TypeError, ValueError):
        start = 0
    if limit is not None:
        try:
            span = int(limit)
        except (TypeError, ValueError):
            span = 0
        chunk = content[start:start + span] if span > 0 else content[start:]
    else:
        chunk = content[start:]
    return {"format": "markdown", "content": chunk, "offset": start,
            "length": len(chunk), "total": total,
            "has_more": start + len(chunk) < total}


# ==============================================================
# Context.Describe — 对象图总览(聚合上面所有,别的 AI 理解本元件的入口)
# ==============================================================

def describe_context(ctx: Any, *, introspect: IntrospectionMeta,
                     config: Any | None = None) -> dict[str, Any]:
    """Context.Describe — 返回对象图:Document/Entity/Editor/Command/Task/ai_flow。"""
    doc = ctx.doc
    entity_types: list[dict[str, Any]] = []
    for et in _doc_entity_types(doc):
        try:
            count = len(doc.list(et))
        except Exception:
            count = 0
        entity_types.append({"type": et, "schema": doc.schema.get(et, {}), "count": count})
    commands = list_commands(ctx.cmd_reg)
    # guided_tour(系统 3a):key_flows 投影;有 U0 实例走实例派生,否则从 ai_flow/说明书回退。
    tours = compile_guided_tours(
        tool_understanding=introspect.tool_understanding,
        ai_flow=introspect.ai_flow,
        manual=introspect.manual,
        commands=commands,
    )
    return {
        "document": {
            "id": doc.id, "name": doc.name,
            "schema_version": doc.schema_version,
            "collaboration_mode": doc.collaboration_mode,
            "save_policy": doc.save_policy,
            "effective_save_policy": doc.effective_save_policy,
            "auto_commit_enabled": doc.auto_commit_enabled,
            "version": doc.version, "acl": doc.acl,
        },
        "entity_types": entity_types,
        "editors": list_editors(introspect, ctx.cmd_reg),
        "commands": commands,
        "guided_tours": guided_tour_summaries(tours),
        "pipelines": list_pipelines(introspect),
        "tasks": list_tasks(ctx.supervisor),
        "operation_log": {"count": len(ctx.op_log), "verified": ctx.op_log.verify_chain()},
        "ai_flow": introspect.ai_flow,
        "manual_available": bool(introspect.manual),
        "settings": (config.snapshot() if config is not None else []),
    }


__all__ = [
    "IntrospectionMeta",
    "describe_context",
    "list_commands", "describe_command",
    "list_entities", "entity_select",
    "list_editors",
    "list_pipelines",
    "list_tasks", "task_status",
    "query_oplog",
    "settings_read", "read_manual",
]
