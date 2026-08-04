"""sdk/guided_tour.py — 引导精灵的**逻辑/契约层**(系统 3a:指引契约 + AI 侧).

图景(GUIDED-TOUR.md):引导精灵不是一份独立手写的引导脚本,而是 U0 内核
``ze.tool-understanding.v1`` 的 ``key_flows`` 的**投影 + 呈现覆盖层**。内核的
``key_flow_step`` 已经把每一步的 ``actor``(human/ai/system)、``command_id``、
``semantic_target`` 和 ``confirmation_intent`` 声明清楚了 —— 它是 guided_tour 语义步的
**超集**。本模块只做「投影」:把内核步压成前端 GuideLayer(系统 3b)能渲染的封闭
step 集,**不新造语义、不手写 confirm**。

两条纪律(落到结构,不只注释):
  1. **actor 决定模式**:``ai``/``system`` + 有 ``command_id`` → **代操作(act)**;
     其余 → **指路(guide)**。指路只 say/point/highlight/ask/wait,绝不替用户按下命令。
  2. **confirm 派生,不手写**:act 步的 ``confirm`` 取自
     ``command_semantics.confirmation_intent == "human_required"``;真正的确认闸门仍在
     dispatch(命令 spec 的 ``requires_confirm``),本字段只是给前端的提示。

实例 vs 回退:
  - 有 tool_understanding **实例**(U0 产物)→ 从 ``key_flows`` 逐 flow 派生一条 tour。
  - 无实例 → 从 describe 的 ``ai_flow``(summary + steps)/ 说明书首段派生**兜底** tour,
    保证全系列 ~183 工具无实例也可推(与 copilot_context 的静态回退同一回退源精神)。

target 语法调和:内核 ``semantic_target{kind:tool/entity/control/command}`` 投影为
``tool:``/``entity:``/``control:``/``cmd:`` 前缀串。锚定/视觉类(``region:``/``guide:``/
``vision:``)属于**呈现覆盖层**,由前端在系统 3b 填进每步的 ``presentation`` 预留字段,
本逻辑层只留空位、不臆造。

纯函数、零 IO、不 import sdk barrel(避免与 introspect 循环):调用方喂进 record/ai_flow/
commands,本模块只组装 dict。
"""

from __future__ import annotations

from typing import Any


# 引导步封闭集(前端 GuideLayer 能渲染的全部原语)。
TOUR_STEP_KINDS = ("say", "point", "highlight", "act", "wait", "ask")
# 一条 tour 的整体模式。
TOUR_MODES = ("guide", "act", "mixed")

# 内核 semantic_target.kind → 前端 target 前缀。
_TARGET_PREFIX = {
    "tool": "tool",
    "entity": "entity",
    "control": "control",
    "command": "cmd",
}


def project_semantic_target(semantic_target: Any) -> str:
    """内核 ``{kind, ref}`` → 前端 target 串;未知/缺失回空串(无具体锚点)。"""
    if not isinstance(semantic_target, dict):
        return ""
    kind = str(semantic_target.get("kind", "") or "")
    ref = str(semantic_target.get("ref", "") or "")
    prefix = _TARGET_PREFIX.get(kind)
    if not prefix or not ref:
        return ""
    return f"{prefix}:{ref}"


def _derive_step_kind(step: dict[str, Any], *, has_command: bool) -> str:
    """从内核步派生封闭集里的一个 kind(见模块 docstring 的两条纪律)。"""
    actor = str(step.get("actor", "") or "")
    # 人在此步为某高危命令提供确认(confirmation provider)→ 问用户。
    if step.get("confirmation_for_command_ids"):
        return "ask"
    if has_command and actor in ("ai", "system"):
        return "act"          # 代操作:AI/系统替用户跑命令
    if has_command:
        return "point"        # 指路:命令由人在 UI 亲自按下,精灵指位置
    # 无命令步(纯语境)。
    target_kind = ""
    st = step.get("semantic_target")
    if isinstance(st, dict):
        target_kind = str(st.get("kind", "") or "")
    if actor == "system":
        return "wait"         # 系统在处理,用户等待
    if target_kind == "entity":
        return "highlight"    # 让用户看某个实体(结果/输入)
    if target_kind in ("control", "command"):
        return "point"        # 指向某个 UI 控件
    return "say"              # 工具级/纯叙述


def _tour_mode(steps: list[dict[str, Any]]) -> str:
    kinds = {s["kind"] for s in steps}
    has_act = "act" in kinds
    has_non_act = any(k != "act" for k in kinds)
    if has_act and has_non_act:
        return "mixed"
    if has_act:
        return "act"
    return "guide"


def _make_tour(*, tour_id: str, title: str, trigger: str, when: str,
               steps: list[dict[str, Any]], source: str) -> dict[str, Any]:
    return {
        "id": tour_id,
        "title": title,
        "trigger": trigger,
        "when": when,
        "mode": _tour_mode(steps) if steps else "guide",
        "source": source,
        "steps": steps,
        "step_count": len(steps),
    }


def _step(*, step_id: str, kind: str, actor: str, say: str, target: str,
          command_id: str | None, confirm: bool, expected_effect: str) -> dict[str, Any]:
    return {
        "id": step_id,
        "kind": kind,
        "actor": actor,
        "mode": "act" if kind == "act" else "guide",
        "say": say,
        "target": target,
        "command_id": command_id,
        "confirm": bool(confirm),
        "expected_effect": expected_effect,
        # 呈现覆盖层(region:/guide:/vision:)由前端在系统 3b 填;逻辑层只留空位。
        "presentation": {},
    }


# ==============================================================
# 实例派生(有 tool_understanding 时)
# ==============================================================

def _tours_from_instance(record: dict[str, Any]) -> list[dict[str, Any]]:
    command_by_id = {
        str(c.get("command_id", "") or ""): c
        for c in (record.get("command_semantics") or [])
        if isinstance(c, dict)
    }
    tours: list[dict[str, Any]] = []
    for key_flow in (record.get("key_flows") or []):
        if not isinstance(key_flow, dict):
            continue
        flow_id = str(key_flow.get("id", "") or "")
        steps: list[dict[str, Any]] = []
        for index, raw in enumerate(key_flow.get("steps") or []):
            if not isinstance(raw, dict):
                continue
            command_id = raw.get("command_id")
            command_id = str(command_id) if command_id else None
            command = command_by_id.get(command_id) if command_id else None
            # confirm 派生,不手写:命令语义 confirmation_intent == human_required。
            confirm = bool(
                command
                and str(command.get("confirmation_intent", "")) == "human_required"
            )
            kind = _derive_step_kind(raw, has_command=command_id is not None)
            steps.append(_step(
                step_id=str(raw.get("id", "") or f"{flow_id}.s{index}"),
                kind=kind,
                actor=str(raw.get("actor", "") or ""),
                say=str(raw.get("intent", "") or ""),
                target=project_semantic_target(raw.get("semantic_target")),
                command_id=command_id,
                confirm=confirm,
                expected_effect=str(raw.get("expected_effect", "") or ""),
            ))
        tours.append(_make_tour(
            tour_id=flow_id,
            title=str(key_flow.get("title", "") or ""),
            trigger=str(key_flow.get("user_goal", "") or ""),
            when=str(key_flow.get("when_to_use", "") or ""),
            steps=steps,
            source="instance",
        ))
    return tours


# ==============================================================
# 回退派生(无实例:ai_flow + 说明书)
# ==============================================================

def _first_manual_paragraph(manual: str, *, cap: int = 400) -> str:
    if not manual:
        return ""
    para: list[str] = []
    for raw in manual.splitlines():
        line = raw.strip()
        if not line:
            if para:
                break
            continue
        if line.startswith("#") and not para:
            para.append(line.lstrip("# ").strip())
            continue
        para.append(line)
    text = " ".join(p for p in para if p).strip()
    return text[:cap]


def _tours_from_fallback(ai_flow: dict[str, Any], manual: str) -> list[dict[str, Any]]:
    ai_flow = ai_flow or {}
    summary = str(ai_flow.get("summary", "") or "").strip()
    raw_steps = [str(s).strip() for s in (ai_flow.get("steps") or []) if str(s).strip()]
    title = summary or _first_manual_paragraph(manual) or "工具引导"
    steps: list[dict[str, Any]] = []
    if raw_steps:
        for index, text in enumerate(raw_steps):
            steps.append(_step(
                step_id=f"overview.s{index}", kind="say", actor="human",
                say=text, target="", command_id=None, confirm=False,
                expected_effect="",
            ))
    else:
        body = summary or _first_manual_paragraph(manual)
        if not body:
            return []
        steps.append(_step(
            step_id="overview.s0", kind="say", actor="human",
            say=body, target="", command_id=None, confirm=False,
            expected_effect="",
        ))
    return [_make_tour(
        tour_id="overview", title=title, trigger=summary, when="",
        steps=steps, source="fallback",
    )]


# ==============================================================
# 编译入口 + 摘要 + 查找
# ==============================================================

def compile_guided_tours(
    *,
    tool_understanding: dict[str, Any] | None = None,
    ai_flow: dict[str, Any] | None = None,
    manual: str = "",
    commands: list[dict[str, Any]] | None = None,  # noqa: ARG001  (回退不消费;签名对齐 seam)
) -> list[dict[str, Any]]:
    """key_flows → guided_tour。有实例走实例派生,否则从 ai_flow/说明书回退派生。"""
    if isinstance(tool_understanding, dict) and tool_understanding.get("key_flows"):
        return _tours_from_instance(tool_understanding)
    return _tours_from_fallback(ai_flow or {}, manual or "")


def guided_tour_summaries(tours: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """ze_describe 用的轻量摘要(不含 steps 明细,明细经 ze_guide 取)。"""
    return [{
        "id": t["id"],
        "title": t["title"],
        "trigger": t["trigger"],
        "when": t["when"],
        "mode": t["mode"],
        "step_count": t["step_count"],
    } for t in tours]


def find_tour(tours: list[dict[str, Any]], tour_id: str) -> dict[str, Any] | None:
    for t in tours:
        if t["id"] == tour_id:
            return t
    return None


def normalize_adhoc_steps(
    raw_steps: list[Any],
    *,
    confirm_lookup: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """把 AI 直接传的 ad-hoc steps 规约成封闭集步(kind 越界一律拒)。

    ad-hoc act 步的 ``confirm`` 从命令注册表的 ``requires_confirm`` 派生(``confirm_lookup``),
    与实例派生一样绝不手写;真正的闸门仍在 dispatch。
    """
    confirm_lookup = confirm_lookup or {}
    out: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_steps or []):
        if not isinstance(raw, dict):
            raise ValueError("ze_guide step 须为对象")
        kind = str(raw.get("kind", "") or "")
        if kind not in TOUR_STEP_KINDS:
            raise ValueError(f"未知 guided_tour step kind: {kind!r}")
        command_id = raw.get("command_id")
        command_id = str(command_id) if command_id else None
        if kind == "act" and not command_id:
            raise ValueError("act 步必须带 command_id")
        confirm = bool(command_id and confirm_lookup.get(command_id, False))
        step = _step(
            step_id=str(raw.get("id", "") or f"adhoc.s{index}"),
            kind=kind,
            actor=str(raw.get("actor", "") or ("ai" if kind == "act" else "human")),
            say=str(raw.get("say", "") or raw.get("intent", "") or ""),
            target=str(raw.get("target", "") or ""),
            command_id=command_id,
            confirm=confirm,
            expected_effect=str(raw.get("expected_effect", "") or ""),
        )
        # ad-hoc 允许作者带 args(逻辑层步不带具体参数;仅 ad-hoc 直调时用)。
        if isinstance(raw.get("args"), dict):
            step["args"] = dict(raw["args"])
        out.append(step)
    return out


def adhoc_tour_mode(steps: list[dict[str, Any]]) -> str:
    return _tour_mode(steps) if steps else "guide"


# ==============================================================
# SSE 帧(per-run 流式;复用现有广播通道,前端在系统 3b 消费)
# ==============================================================

def guide_step_frame(run_id: str, index: int, total: int,
                     step: dict[str, Any], doc_id: str) -> dict[str, Any]:
    """``guide_step`` 广播帧:per-run 一步一帧(无 ``changes``,只匹配 doc 级订阅)。"""
    return {
        "type": "guide_step",
        "doc_id": doc_id,
        "runId": run_id,
        "index": index,
        "total": total,
        "step": step,
    }


def guide_end_frame(run_id: str, status: str, doc_id: str) -> dict[str, Any]:
    """``guide_end`` 广播帧:一轮引导收尾。"""
    return {
        "type": "guide_end",
        "doc_id": doc_id,
        "runId": run_id,
        "status": status,
    }


__all__ = [
    "TOUR_STEP_KINDS",
    "TOUR_MODES",
    "project_semantic_target",
    "compile_guided_tours",
    "guided_tour_summaries",
    "find_tour",
    "normalize_adhoc_steps",
    "adhoc_tour_mode",
    "guide_step_frame",
    "guide_end_frame",
]
