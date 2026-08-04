"""02-mod/05-code/commands_register.py — ZE-video-edit-studio 命令面.

native: annotation.add / annotation.remove / asset.import / send.compile
stage : edit.send(把「可见图层 × 选定区间」合成一段发给 AI,结果作为新图层插最上面)

纪律(与 ②③ 同源):
- 写入全走 Command → Operation(undo/CAS/审计齐)。
- send.compile 纯函数产快照(不派发),send.dispatch 才真发 —— AI/CLI 可先 compile
  审查再 dispatch,审计链完整(c7 慢过程一律 stage)。
- 时间线是一摞图层:新结果作为新图层插最上面,不覆盖任何东西。要就留着、不要就删那一行,
  想看原片把上层的眼睛关掉 —— 图层栈自己就是候选制,不再有 candidate/采纳/回贴策略。
- ffmpeg / 视频服务缺位 → requires_runtime 结构化(native 抛 ZeError;stage 落 patches)。
"""

from __future__ import annotations

import json
import math
import uuid
from typing import Any

from sdk import (
    CommandRegistry,
    Entity,
    ENTITY_META_FIELD,
    Patch,
    StagePlan,
    StageSpec,
    WorkItem,
    WorkList,
    ZeError,
    new_operation,
    register_builtin_commands,
)

from _domain import capability_matrix, ffmpeg_ops, layer_compose, render, send_compiler
from _domain.errors import RequiresRuntime
import cutout_stage
import export_stage
import generation_contract
import layer_edit_stage
import prompt_optimize


# ---- Operation 组装 ---------------------------------------------------------

def _create_op_patches(e: Entity) -> tuple[Patch, Patch]:
    snap = e.to_dict()
    fwd = Patch(entity_type=e.type, entity_id=e.id, field=ENTITY_META_FIELD,
                op="create", before=None, after=snap)
    rev = Patch(entity_type=e.type, entity_id=e.id, field=ENTITY_META_FIELD,
                op="delete", before=snap, after=None)
    return fwd, rev


def _create_op(command_id: str, source: str, actor, doc, entity: Entity):
    doc.add(entity)
    fwd, rev = _create_op_patches(entity)
    return new_operation(command_id=command_id, source=source, actor=actor.to_dict(),
                         patches=[fwd], reverse_patches=[rev])


def _apply_patches(entity: Entity, patches: dict[str, Any], actor) -> tuple[list[Patch], list[Patch]]:
    before = {k: entity.data.get(k) for k in patches}
    entity.update(patches, expected_version=entity.version, actor=actor)
    fwd = [Patch(entity_type=entity.type, entity_id=entity.id, field=k, op="set",
                 before=before[k], after=patches[k]) for k in patches]
    rev = [Patch(entity_type=entity.type, entity_id=entity.id, field=k, op="set",
                 before=patches[k], after=before[k]) for k in patches]
    return fwd, rev


def _live(doc, entity_type: str) -> list[Entity]:
    return [e for e in doc.list(entity_type) if not getattr(e, "deleted", False)]


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _arg_id(args: dict[str, Any]) -> str:
    """取单实体 id:form 动作传 {id},gallery selection 动作传 {ids:[...]} —— 两者都要接。"""
    one = args.get("id")
    if one:
        return str(one)
    ids = args.get("ids") or []
    return str(ids[0]) if ids else ""


def _compact(data: dict[str, Any]) -> dict[str, Any]:
    """Drop None values — the framework rejects None for typed (string) fields."""
    return {k: v for k, v in data.items() if v is not None}


def _annotation_domain_shape(ann: Entity) -> dict[str, Any]:
    """实体形状 → 域层形状。

    实体上标注跟 clip 一样用 start/duration(时间线上的一行);域层要的是
    ``interval=[t0,t1]``。这个函数就是那层适配器 —— 改实体字段名不动域层。
    """
    d = ann.data
    start = float(d.get("start") or 0)
    try:
        points = json.loads(str(d.get("pointsJson") or "[]"))
    except ValueError:
        points = []
    return {
        "id": ann.id,
        "name": ann.name,
        "kind": str(d.get("kind") or "box"),
        "layer": float(d.get("layer") or 0),
        "visible": d.get("visible") is not False,
        "number": int(d.get("number") or 0),
        "label": str(d.get("label") or ""),
        "bbox": {"x": float(d.get("bboxX") or 0), "y": float(d.get("bboxY") or 0),
                 "w": float(d.get("bboxW") or 0), "h": float(d.get("bboxH") or 0)},
        "points": points,
        "asset_id": d.get("refImageAssetId"),
        "color": str(d.get("color") or "#ff3b30"),
        "interval": [start, start + float(d.get("duration") or 0)],
        "keyframe_origin": start,
    }


def _keyframes_for(doc, annotation_id: str) -> list[dict[str, Any]]:
    return sorted(
        (
            {**frame.data, "id": frame.id}
            for frame in _live(doc, "guide_keyframe")
            if str(frame.data.get("parentId") or "") == annotation_id
        ),
        key=lambda item: float(item.get("time") or 0),
    )


def _visible_annotations(doc) -> list[Entity]:
    """这次要参与的标注 —— 关掉眼睛的不算。

    与 clip 的 ``visible`` 同一条判据(``is not False``,老数据没这个字段算显示)。
    发送前的每一处「有哪些标注」都必须过这一层:画面上烧的、提示词里说的、
    界面上显示的,少过一处人就会看到「关掉了却还发出去」。
    """
    return [a for a in _live(doc, "annotation") if a.data.get("visible") is not False]


def _asset_is_referenced(doc, asset_id: str) -> bool:
    """素材仍被画面引导或视频层使用时不能自动回收。"""
    return any(
        str(ann.data.get("refImageAssetId") or "") == asset_id
        for ann in _live(doc, "annotation")
    ) or any(
        str(clip.data.get("assetId") or "") == asset_id
        for clip in _live(doc, "clip")
    )










def register(registry: CommandRegistry) -> None:
    register_builtin_commands(registry)
    asset_store = getattr(registry, "asset_store", None)
    supervisor = getattr(registry, "pipeline_supervisor", None)
    config = getattr(registry, "config", None)

    registry.usability_cases = [
        {"command": "annotation.add", "entity_type": "annotation", "expect": "ok"},
        {"command": "annotation.remove", "entity_type": "annotation", "expect": "ok"},
        {"command": "reference_frame.add", "entity_type": "reference_frame", "expect": "ok"},
        {"command": "asset.import", "entity_type": "asset", "expect": "reject"},
        {"command": "send.compile", "entity_type": "send_profile", "expect": "reject"},
        # send.dispatch is a stage command (not native); its empty-input guard is
        # covered by test_commands_flow, and the usability gate only tracks natives.
    ]

    def _project(doc) -> Entity | None:
        items = _live(doc, "project")
        return items[0] if items else None

    def _canvas(doc) -> tuple[float, float]:
        proj = _project(doc)
        d = proj.data if proj else {}
        return (float(d.get("width") or 1920), float(d.get("height") or 1080))

    def _ratio(doc) -> str:
        for prof in _live(doc, "send_profile"):
            if prof.data.get("ratio"):
                return str(prof.data["ratio"])
        project = _project(doc)
        pdata = project.data if project is not None else {}
        width = max(0, round(float(pdata.get("width") or 0)))
        height = max(0, round(float(pdata.get("height") or 0)))
        if width > 0 and height > 0:
            divisor = math.gcd(width, height) or 1
            rw, rh = width // divisor, height // divisor
            if rw <= 100 and rh <= 100:
                return f"{rw}:{rh}"
        return "16:9"

    def _is_first_asset(doc, just_added) -> bool:
        """这是不是项目里第一个视频素材 —— 只有它有资格定画布。

        判据用「第一个素材」而不是「宽高为空」:宽高有 schema 默认值、永远不为空,
        判不出来;而一旦人已经导过素材、可能也已经调过画布,后续导入就绝不该
        再悄悄改画幅。
        """
        return not [
            a for a in _live(doc, "asset")
            if a.id != just_added.id and a.data.get("assetType") == "video"
        ]

    def _fps(doc) -> float:
        proj = _project(doc)
        return float((proj.data if proj else {}).get("fps") or 30.0)

    def _padding(doc) -> float:
        """发送区间前后留多少余量。模型光看一秒判断不出运动方向和光照走势,
        前后各留半秒它才知道这块东西在怎么动;单帧标注也靠它撑成可发段。"""
        for prof in _live(doc, "send_profile"):
            if prof.data.get("paddingSec") is not None:
                return max(0.0, float(prof.data["paddingSec"]))
        return 0.5

    def _default_model(doc) -> str:
        """默认模型:发送配置里选的;都没有就取真值表里唯一会视频编辑的那个。"""
        for prof in _live(doc, "send_profile"):
            if prof.data.get("model"):
                return str(prof.data["model"])
        eligible = capability_matrix.models_for_step("edit", required_kinds=("video",))
        return eligible[0] if eligible else ""

    def _need_supervisor() -> None:
        if supervisor is None:
            raise ZeError("pipeline_not_available", "PipelineSupervisor 未注入,无法启动慢任务",
                          context={})
        if asset_store is None:
            raise ZeError("requires_runtime", "AssetStore 未注入,无法读写视频文件", context={})

    def _launch(command_id: str, source: str, actor, work_list: WorkList, stage_plan: StagePlan,
                patches: list[Patch] | None = None, reverse_patches: list[Patch] | None = None):
        """起慢任务。``patches`` 让调用方把「这次发送」本身的建档一起记进同一笔账 ——
        否则界面上任务在跑,却看不到跑的是什么。"""
        start_op = new_operation(command_id=command_id, source=source, actor=actor.to_dict(),
                                 patches=list(patches or []),
                                 reverse_patches=list(reverse_patches or []),
                                 trace_id=work_list.run_id, kind="stage")
        if supervisor is None:
            raise ZeError("pipeline_not_available", "PipelineSupervisor 未注入,无法启动慢任务",
                          context={"command": command_id})
        mutation, _state = supervisor.prepare_start(
            work_list=work_list, stage_plan=stage_plan, trace_id=work_list.run_id,
            start_command_id=command_id, start_operation=start_op)
        return mutation

    # ---- send_profile.set --------------------------------------------------

    @registry.command(
        id="send_profile.set",
        label="更新生成设置",
        kind="native",
        touches=["send_profile"],
        description="更新模型与提交内容；历史项目缺少生成设置时自动补建默认配置。",
        params_schema={"type": "object", "properties": {
            "mode": {"type": "number"},
            "provider": {"type": "string"},
            "strategy": {"type": "string"},
            "model": {"type": "string"},
            "duration": {"type": "number"},
            "ratio": {"type": "string"},
            "paddingSec": {"type": "number"},
            "targetClipId": {"type": "string"},
        }},
    )
    def _send_profile_set(args, actor, source, doc):
        profile = next(iter(_live(doc, "send_profile")), None)
        current = dict(profile.data) if profile is not None else {}
        model = str(
            args.get("model")
            or current.get("model")
            or _default_model(doc)
            or "gemini-omni-flash-preview"
        ).strip()
        provider = args.get("provider") or current.get("provider")
        strategy = args.get("strategy") or current.get("strategy")
        try:
            normalized = generation_contract.normalize_profile(
                provider=provider,
                model=model,
                strategy=strategy,
                legacy_mode=args.get("mode", current.get("mode")),
            )
        except ValueError as exc:
            raise ZeError(
                "validation_failed",
                str(exc),
                context={"provider": provider, "model": model, "strategy": strategy},
            ) from exc

        chosen_strategy = normalized["strategy"]
        data = {
            **current,
            "mode": 2 if chosen_strategy == generation_contract.MODIFIED_FRAMES else 1,
            "provider": normalized["provider"],
            "strategy": chosen_strategy,
            "model": normalized["modelId"],
            "ratio": str(args.get("ratio") or current.get("ratio") or _ratio(doc)),
            "paddingSec": max(
                0.0,
                float(args.get(
                    "paddingSec",
                    current.get("paddingSec") if current.get("paddingSec") is not None else 0.5,
                )),
            ),
        }
        for key in ("duration", "targetClipId"):
            if key in args:
                data[key] = args[key]
        if profile is None:
            profile = Entity(
                type="send_profile",
                id=_new_id("profile"),
                name="生成设置",
                data=_compact(data),
            )
            return _create_op("send_profile.set", source, actor, doc, profile)

        patches = {key: value for key, value in data.items() if profile.data.get(key) != value}
        fwd, rev = _apply_patches(profile, patches, actor) if patches else ([], [])
        return new_operation(
            command_id="send_profile.set",
            source=source,
            actor=actor.to_dict(),
            patches=fwd,
            reverse_patches=rev,
        )

    # ---- annotation.add / remove -------------------------------------------

    @registry.command(id="annotation.add", label="添加画面引导", kind="native", touches=["annotation"],
                      description="在画面上框选、放入素材、添加文字或绘制路径。引导是时间线上的一行:"
                                  "有 start/duration(duration=0 即单帧)、有 visible。"
                                  "编号按现有引导顺延,框选与路径可选黑、白、红三色。",
                      params_schema={"type": "object", "properties": {
                          "kind": {"type": "string", "label": "box/text/image/arrow"},
                          "label": {"type": "string", "label": "指令文字"},
                          "color": {"type": "string", "label": "黑色、白色或红色"},
                          "refImageAssetId": {"type": "string", "label": "图片素材 id"},
                          "pointsJson": {"type": "string", "label": "路径点 JSON"},
                          "targetClipId": {"type": "string"},
                          "bboxX": {"type": "number"}, "bboxY": {"type": "number"},
                          "bboxW": {"type": "number"}, "bboxH": {"type": "number"},
                          "start": {"type": "number", "label": "起始(秒)"},
                          "duration": {"type": "number", "label": "时长(秒)"},
                          "layer": {"type": "number", "label": "层序(缺省压在当前最上层视频之上)"}}})
    def _annotation_add(args, actor, source, doc):
        number = len(_live(doc, "annotation")) + 1
        # 层序:压在「你正看着的那层视频」之上,否则新画的框自己就被盖住、看不见。
        # 调用方给了就听它(AI 精确摆放)。
        if args.get("layer") is not None:
            layer = max(0.0, float(args["layer"]))
        else:
            at = float(args.get("start") or 0)
            clips = [{**c.data, "id": c.id} for c in _live(doc, "clip")]
            top = layer_compose.topmost_at(clips, at)
            layer = float(top.get("layer") or 0) + 1 if top else 0.0
        label = str(args.get("label") or f"画面引导 {number}")
        kind = str(args.get("kind") or "box")
        if kind not in {"box", "text", "image", "arrow"}:
            raise ZeError("validation_failed", f"未知引导方式: {kind}", context={"kind": kind})
        color = str(args.get("color") or "#ff3b30").strip().lower()
        if color not in {paint[2] for paint in ffmpeg_ops.GUIDE_COLORS}:
            raise ZeError("validation_failed", "颜色只能选择黑色、白色或红色",
                          context={"color": color})
        data = _compact({
            "visible": True, "layer": layer,
            "kind": kind, "label": label, "number": number,
            "color": color,
            "targetClipId": args.get("targetClipId"),
            "refImageAssetId": args.get("refImageAssetId"),
            "pointsJson": args.get("pointsJson"),
            "bboxX": float(args.get("bboxX") or 0), "bboxY": float(args.get("bboxY") or 0),
            "bboxW": float(args.get("bboxW") or 0), "bboxH": float(args.get("bboxH") or 0),
            "start": max(0.0, float(args.get("start") or 0)),
            "duration": max(0.0, float(args.get("duration") or 0)),
        })
        ent = Entity(type="annotation", id=_new_id("ann"), name=label, data=data)
        return _create_op("annotation.add", source, actor, doc, ent)

    @registry.command(id="annotation.remove", label="删除画面引导", kind="native",
                      touches=["annotation", "guide_keyframe", "asset"],
                      description="删除画面引导并把剩余编号重排；图片引导独占的素材一并回收。",
                      params_schema={"type": "object", "properties": {"id": {"type": "string"}}})
    def _annotation_remove(args, actor, source, doc):
        if not args.get("id"):
            raise ZeError("validation_failed", "缺少 annotation id", context={})
        target = doc.get("annotation", args["id"])
        if target is None:
            raise ZeError("validation_failed", "画面引导不存在", context={"id": args.get("id")})
        fwd, rev = [], []
        snap = target.to_dict()
        target.delete(expected_version=target.version, actor=actor)
        after = target.to_dict()
        fwd.append(Patch(entity_type="annotation", entity_id=target.id, field=ENTITY_META_FIELD,
                         op="soft_delete", before=snap, after=after))
        rev.insert(0, Patch(entity_type="annotation", entity_id=target.id, field=ENTITY_META_FIELD,
                            op="restore", before=after, after=snap))
        # 位置关键帧是这条引导的一部分；删引导时必须同笔删除，撤销也同笔恢复。
        for frame in _live(doc, "guide_keyframe"):
            if str(frame.data.get("parentId") or "") != target.id:
                continue
            frame_before = frame.to_dict()
            frame.delete(expected_version=frame.version, actor=actor)
            frame_after = frame.to_dict()
            fwd.append(Patch(entity_type="guide_keyframe", entity_id=frame.id,
                             field=ENTITY_META_FIELD, op="soft_delete",
                             before=frame_before, after=frame_after))
            rev.insert(0, Patch(entity_type="guide_keyframe", entity_id=frame.id,
                                field=ENTITY_META_FIELD, op="restore",
                                before=frame_after, after=frame_before))
        # 图片是“导入素材 + 图片引导”一体创建的。删掉最后一个引用它的引导时，
        # 同笔软删孤儿素材，释放项目内唯一名称；若仍被别的引导或视频层引用则保留。
        image_asset_id = (
            str(target.data.get("refImageAssetId") or "")
            if str(target.data.get("kind") or "") == "image" else ""
        )
        if image_asset_id:
            asset = doc.find("asset", image_asset_id)
            if asset is not None and not asset.deleted and not _asset_is_referenced(
                doc, image_asset_id,
            ):
                asset_before = asset.to_dict()
                asset.delete(expected_version=asset.version, actor=actor)
                asset_after = asset.to_dict()
                fwd.append(Patch(entity_type="asset", entity_id=asset.id,
                                 field=ENTITY_META_FIELD, op="soft_delete",
                                 before=asset_before, after=asset_after))
                rev.insert(0, Patch(entity_type="asset", entity_id=asset.id,
                                    field=ENTITY_META_FIELD, op="restore",
                                    before=asset_after, after=asset_before))
        # 剩下的只重排编号。颜色是用户对这条引导的选择，不能随编号改变。
        remaining = sorted((e for e in _live(doc, "annotation") if e.id != target.id),
                           key=lambda e: e.data.get("number") or 0)
        for i, ann in enumerate(remaining, start=1):
            patch = {}
            if ann.data.get("number") != i:
                patch["number"] = i
            if patch:
                f2, r2 = _apply_patches(ann, patch, actor)
                fwd += f2
                rev += r2
        return new_operation(command_id="annotation.remove", source=source,
                             actor=actor.to_dict(), patches=fwd, reverse_patches=rev)

    # ---- guide.keyframe.set / remove --------------------------------------

    @registry.command(
        id="guide.keyframe.set", label="记录当前位置", kind="native",
        touches=["guide_keyframe"],
        description="在当前时刻记录矩形或图片的位置与大小；同一帧再次记录会更新原点。",
        params_schema={"type": "object", "properties": {
            "annotationId": {"type": "string", "label": "画面引导"},
            "at": {"type": "number", "label": "项目时间(秒)"},
            "bboxX": {"type": "number"}, "bboxY": {"type": "number"},
            "bboxW": {"type": "number"}, "bboxH": {"type": "number"},
        }},
    )
    def _guide_keyframe_set(args, actor, source, doc):
        annotation_id = str(args.get("annotationId") or "")
        ann = doc.get("annotation", annotation_id) if annotation_id else None
        if ann is None or getattr(ann, "deleted", False):
            raise ZeError("validation_failed", "画面引导不存在",
                          context={"annotationId": annotation_id})
        kind = str(ann.data.get("kind") or "box")
        if kind not in {"box", "image"}:
            raise ZeError("validation_failed", "位置关键画面目前只支持矩形框和图片",
                          context={"kind": kind})
        fps = max(1.0, _fps(doc))
        start = float(ann.data.get("start") or 0)
        duration = max(0.0, float(ann.data.get("duration") or 0))
        at = round(max(0.0, float(args.get("at") or 0)) * fps) / fps
        eps = 0.5 / fps
        if at < start - eps or at > start + duration + eps:
            raise ZeError("validation_failed", "当前时刻不在这条引导的时间范围内",
                          context={"at": at, "range": [start, start + duration]})
        local_time = round(max(0.0, min(duration, at - start)), 6)
        data = {
            "parentId": ann.id,
            "time": local_time,
            "bboxX": float(args.get("bboxX", ann.data.get("bboxX") or 0)),
            "bboxY": float(args.get("bboxY", ann.data.get("bboxY") or 0)),
            "bboxW": max(0.0, float(args.get("bboxW", ann.data.get("bboxW") or 0))),
            "bboxH": max(0.0, float(args.get("bboxH", ann.data.get("bboxH") or 0))),
            "easing": "linear",
        }
        for frame in _live(doc, "guide_keyframe"):
            if str(frame.data.get("parentId") or "") != ann.id:
                continue
            if abs(float(frame.data.get("time") or 0) - local_time) <= eps:
                fwd, rev = _apply_patches(frame, data, actor)
                return new_operation(command_id="guide.keyframe.set", source=source,
                                     actor=actor.to_dict(), patches=fwd, reverse_patches=rev)
        frame = Entity(
            type="guide_keyframe", id=_new_id("gkf"),
            name=f"{ann.name} · {local_time:.2f}s", data=data,
        )
        return _create_op("guide.keyframe.set", source, actor, doc, frame)

    @registry.command(
        id="guide.keyframe.remove", label="删除当前位置", kind="native",
        touches=["guide_keyframe"],
        description="删除一个位置关键画面。",
        params_schema={"type": "object", "properties": {"id": {"type": "string"}}},
    )
    def _guide_keyframe_remove(args, actor, source, doc):
        frame_id = _arg_id(args)
        frame = doc.get("guide_keyframe", frame_id) if frame_id else None
        if frame is None or getattr(frame, "deleted", False):
            raise ZeError("validation_failed", "位置关键画面不存在", context={"id": frame_id})
        before = frame.to_dict()
        frame.delete(expected_version=frame.version, actor=actor)
        after = frame.to_dict()
        return new_operation(
            command_id="guide.keyframe.remove", source=source, actor=actor.to_dict(),
            patches=[Patch(entity_type="guide_keyframe", entity_id=frame.id,
                           field=ENTITY_META_FIELD, op="soft_delete",
                           before=before, after=after)],
            reverse_patches=[Patch(entity_type="guide_keyframe", entity_id=frame.id,
                                   field=ENTITY_META_FIELD, op="restore",
                                   before=after, after=before)],
        )

    # ---- reference_frame.add / remove -------------------------------------

    @registry.command(id="reference_frame.add", label="加入参考画面", kind="native",
                      touches=["reference_frame"],
                      description="把当前时间点加入修改画面清单；提交时渲染这一刻生效的全部引导。",
                      params_schema={"type": "object", "properties": {
                          "at": {"type": "number", "label": "时间(秒)"},
                          "targetClipId": {"type": "string"},
                          "label": {"type": "string"}}})
    def _reference_frame_add(args, actor, source, doc):
        at = max(0.0, float(args.get("at") or 0))
        # 同一帧只保留一个点，避免一次误触上传重复图片。
        fps = max(1.0, _fps(doc))
        for cue in _live(doc, "reference_frame"):
            if abs(float(cue.data.get("at") or 0) - at) <= 0.5 / fps:
                return new_operation(command_id="reference_frame.add", source=source,
                                     actor=actor.to_dict(), patches=[], reverse_patches=[])
        ent = Entity(
            type="reference_frame",
            id=_new_id("ref"),
            name=str(args.get("label") or f"参考画面 {at:.2f}s"),
            data=_compact({"at": round(at, 3), "enabled": True,
                           "targetClipId": args.get("targetClipId"),
                           "label": args.get("label") or ""}),
        )
        return _create_op("reference_frame.add", source, actor, doc, ent)

    @registry.command(id="reference_frame.remove", label="移除参考画面", kind="native",
                      touches=["reference_frame"],
                      params_schema={"type": "object", "properties": {"id": {"type": "string"}}})
    def _reference_frame_remove(args, actor, source, doc):
        target = doc.get("reference_frame", str(args.get("id") or ""))
        if target is None:
            raise ZeError("validation_failed", "参考画面不存在", context={"id": args.get("id")})
        snap = target.to_dict()
        target.delete(expected_version=target.version, actor=actor)
        after = target.to_dict()
        return new_operation(
            command_id="reference_frame.remove",
            source=source,
            actor=actor.to_dict(),
            patches=[Patch(entity_type="reference_frame", entity_id=target.id,
                           field=ENTITY_META_FIELD, op="soft_delete", before=snap, after=after)],
            reverse_patches=[Patch(entity_type="reference_frame", entity_id=target.id,
                                   field=ENTITY_META_FIELD, op="restore", before=after, after=snap)],
        )

    # ---- asset.import ------------------------------------------------------

    @registry.command(id="asset.import", label="导入素材", kind="native", touches=["asset"],
                      description="登记命名素材(项目内唯一);视频/图片/音频。",
                      params_schema={"type": "object", "properties": {
                          "name": {"type": "string"}, "assetType": {"type": "string"},
                          "sourcePath": {"type": "string"}, "duration": {"type": "number"},
                          "width": {"type": "number"}, "height": {"type": "number"},
                          "fps": {"type": "number"}}})
    def _asset_import(args, actor, source, doc):
        ref = args.get("file") or args.get("assetRef") or args.get("asset_ref")
        has_file = isinstance(ref, dict) and bool(ref.get("sha256"))
        name = str(args.get("name") or "").strip()
        if not name and has_file:
            raw = str(ref.get("name") or ref.get("filename") or "素材")
            name = raw.rsplit("/", 1)[-1].rsplit(".", 1)[0].strip() or "素材"
        if not name:
            raise ZeError("validation_failed", "素材需要名字(项目内唯一,供命名指代)", context={})
        atype = str(args.get("assetType") or "")
        if not atype:
            mime = str((ref or {}).get("mime") or "") if has_file else ""
            atype = ("image" if mime.startswith("image/")
                     else "audio" if mime.startswith("audio/") else "video")
        same_named = [asset for asset in _live(doc, "asset") if asset.name == name]
        reclaimable = [
            asset for asset in same_named
            if atype == "image" and asset.data.get("assetType") == "image"
            and not _asset_is_referenced(doc, asset.id)
        ]
        if len(reclaimable) != len(same_named):
            raise ZeError("validation_failed", f"素材名「{name}」已存在(项目内须唯一)", context={})
        # Web 导入器从浏览器媒体解码器读取规格并随命令提交。同步命令不做文件 IO，
        # 保持 Command producer 纯净；CLI 未给规格时仍可登记为待补全素材。
        spec = {
            "width": float(args.get("width") or 0),
            "height": float(args.get("height") or 0),
            "fps": float(args.get("fps") or 0),
            "duration": float(args.get("duration") or 0),
        }

        data = _compact({"assetType": atype,
                         "assetRef": ref if has_file else None,
                         "sourcePath": args.get("sourcePath"),
                         "duration": float(spec["duration"] or 0),
                         "width": spec["width"] or None,
                         "height": spec["height"] or None,
                         "fps": spec["fps"] or None,
                         "cutoutStatus": "idle" if atype == "image" else None,
                         "placeholder": not (has_file or bool(args.get("sourcePath")))})
        fwd, rev = [], []
        for orphan in reclaimable:
            orphan_before = orphan.to_dict()
            orphan.delete(expected_version=orphan.version, actor=actor)
            orphan_after = orphan.to_dict()
            fwd.append(Patch(entity_type="asset", entity_id=orphan.id,
                             field=ENTITY_META_FIELD, op="soft_delete",
                             before=orphan_before, after=orphan_after))
            rev.append(Patch(entity_type="asset", entity_id=orphan.id,
                             field=ENTITY_META_FIELD, op="restore",
                             before=orphan_after, after=orphan_before))

        ent = Entity(type="asset", id=_new_id("asset"), name=name, data=data)
        doc.add(ent)
        create_fwd, create_rev = _create_op_patches(ent)
        fwd.append(create_fwd)
        rev.insert(0, create_rev)

        # **首个素材定画布**:空项目拖进第一段视频,画布就取它的规格。
        # 否则人得先猜一个画幅,而猜错会让后面的几何与标注坐标全歪(v1 首图定画布同款)。
        # 与导入**同一个 Operation** —— 一次撤销要把「导进来」和「画布被改了」一起退回去,
        # 否则人撤销后素材没了、画幅却留着被改过的样子。
        proj = _project(doc)
        if (proj is not None and atype == "video" and spec["width"] > 0
                and _is_first_asset(doc, ent)):
            patches = {"width": spec["width"], "height": spec["height"]}
            if spec["fps"] > 0:
                patches["fps"] = spec["fps"]
            f2, r2 = _apply_patches(proj, patches, actor)
            fwd += f2
            rev[:0] = r2                        # 后做的先撤
        return new_operation(command_id="asset.import", source=source,
                             actor=actor.to_dict(), patches=fwd, reverse_patches=rev)

    @registry.command(id="asset.cutout", label="图片自动去背景", kind="stage",
                      touches=["asset"],
                      description="为图片素材生成透明版本；失败时保留原图，不阻断后续编辑。",
                      params_schema={"type": "object", "properties": {
                          "id": {"type": "string", "label": "图片素材 id"}}})
    def _asset_cutout(args, actor, source, doc):
        asset_id = _arg_id(args)
        asset = doc.get("asset", asset_id)
        if asset is None or asset.data.get("assetType") != "image":
            raise ZeError("validation_failed", "请选择一张图片素材", context={"id": asset_id})
        ref = asset.data.get("assetRef")
        if not isinstance(ref, dict) or not ref.get("sha256"):
            raise ZeError("validation_failed", "图片素材还没有文件", context={"id": asset_id})
        _need_supervisor()
        run_id = uuid.uuid4().hex[:12]
        item = WorkItem(
            item_id=asset.id,
            entity_type="asset",
            entity_id=asset.id,
            # 处理结果直接以 AssetRef 回写；这里没有额外的文件型审计产物。
            artifacts=(),
            payload={"asset_ref": ref},
        )
        stage = StageSpec(
            id="asset.cutout",
            label="图片自动去背景",
            run=lambda ctx, a: cutout_stage.run_cutout_stage(ctx, a, asset_store=asset_store),
            resource_scopes=("cpu.image",),
        )
        return _launch(
            "asset.cutout", source, actor,
            WorkList(run_id=run_id, name=f"处理图片 · {asset.name}", items=[item]),
            StagePlan.from_single(stage),
        )

    # ---- send.compile (纯,产快照) ------------------------------------------

    @registry.command(id="send.compile", label="编译发送(审查快照)", kind="native",
                      touches=["send_job"],
                      description="按 send_profile + 标注编译出实发提示词/文件序/渲染面快照,"
                                  "落 send_job(status=compiled);不派发。红线阻断(引用缺失/未改写名人)。",
                      params_schema={"type": "object", "properties": {
                          "profileId": {"type": "string", "label": "send_profile id"}}})
    def _send_compile(args, actor, source, doc):
        profile_id = args.get("profileId") or args.get("id")
        if not profile_id:
            raise ZeError("validation_failed", "缺少 send_profile id;先选一张 send_profile", context={})
        profile = doc.get("send_profile", profile_id)
        if profile is None:
            raise ZeError("validation_failed", "找不到发送配置;先选一张 send_profile", context={})
        model = str(profile.data.get("model") or "").strip()
        if not model:
            raise ZeError("validation_failed", "发送配置未选模型;先在发送卡里选模型", context={})
        mode = int(profile.data.get("mode") or 1)
        target_clip = profile.data.get("targetClipId")
        anns = []
        for ann in _visible_annotations(doc):
            if target_clip is not None and ann.data.get("targetClipId") != target_clip:
                continue
            shaped = _annotation_domain_shape(ann)
            shaped["keyframes"] = _keyframes_for(doc, ann.id)
            anns.append(shaped)
        if not anns:
            raise ZeError("validation_failed",
                          "需要至少一个显示中的标注;先在画布框选新建,或把关掉的那几行打开",
                          context={})
        projects = _live(doc, "project")
        proj_data = projects[0].data if projects else {}
        cw = float(proj_data.get("width") or 1920)
        ch = float(proj_data.get("height") or 1080)
        # 截段区间要 clamp 到素材时长:优先目标 clip 的时长,回退项目里第一个视频素材
        src_dur = None
        if target_clip:
            try:
                src_dur = float(doc.get("clip", target_clip).data.get("duration") or 0) or None
            except ZeError:
                src_dur = None
        padding = float(profile.data.get("paddingSec") or 0.5)
        try:
            compiled = send_compiler.compile_send(
                mode=mode, model=model, annotations=anns,
                canvas_w=cw, canvas_h=ch,
                padding_sec=padding, source_duration=src_dur,
                segment_duration=profile.data.get("duration"))
        except ValueError as exc:
            raise ZeError("validation_failed", str(exc), context={"model": model, "mode": mode}) from exc
        interval = compiled.get("send_interval")
        data = _compact({
            "status": "compiled", "mode": mode, "model": compiled["model"],
            "targetClipId": target_clip,  # carry the回贴 destination through to dispatch
            "promptSnapshot": compiled["prompt"],
            "imageStepPromptSnapshot": compiled.get("image_step_prompt"),  # 模式二图步(2A)
            "fileOrderJson": json.dumps(compiled["file_order"], ensure_ascii=False),
            "renderSurfacesJson": json.dumps(compiled["render_surfaces"], ensure_ascii=False),
            # 红线与人可审的编译细节必须落库,否则 dispatch 无从拦、人无从复核
            "blocked": bool(compiled["blocked"]),
            "sendIntervalJson": json.dumps(interval, ensure_ascii=False) if interval else "",
            "safetyDiffJson": json.dumps(compiled["safety"], ensure_ascii=False),
            "compileWarningsJson": json.dumps(compiled["warnings"], ensure_ascii=False),
            "lastError": "; ".join(compiled["warnings"]) if compiled["warnings"] else "",
        })
        ent = Entity(type="send_job", id=_new_id("job"),
                     name=f"发送 · 模式{mode} · {compiled['model']}", data=data)
        op = _create_op("send.compile", source, actor, doc, ent)
        # blocked snapshots are recorded but flagged (red line: missing ref / unrewritten name)
        return op

    # ---- edit.send (stage) —— 这个工具的主动作 --------------------------------

    def _resolve_refs(plan, assets):
        """每一刀都要有真视频文件:生成层自带 videoRef,原片层回到它引用的素材。"""
        for cut in plan["cuts"]:
            if not (isinstance(cut.get("video_ref"), dict) and cut["video_ref"].get("sha256")):
                asset = assets.get(cut.get("asset_id") or "")
                ref = asset.data.get("assetRef") if asset is not None else None
                if not (isinstance(ref, dict) and ref.get("sha256")):
                    raise ZeError("validation_failed",
                                  f"图层「{cut.get('clip_id')}」还没有视频文件;先导入素材",
                                  context={"clip": cut.get("clip_id")})
                cut["video_ref"] = ref
        return plan

    def _guide_lines(guides):
        """把不同画面引导写成人能审、模型能对应的短句。"""
        labels = {"box": "框选区域", "text": "文字说明", "image": "图片素材", "arrow": "移动路径"}
        lines = []
        for idx, guide in enumerate(guides):
            kind = str(guide.get("kind") or "box")
            label = str(guide.get("label") or "").strip()
            color_name = ffmpeg_ops.annotation_paint(guide, idx)[1]
            if kind == "image":
                subject = f"{color_name}边界内的图片素材"
            elif kind == "arrow":
                subject = f"{color_name}箭头指示的移动路径与方向"
            elif kind == "text":
                subject = "画面中的文字说明"
            else:
                subject = f"{color_name}框选区域"
            detail = label or labels.get(kind, "画面引导")
            lines.append(f"{guide['number']}. {subject}: {detail}")
        return lines

    def _generation_profile(doc, args) -> dict[str, str]:
        profile = next(iter(_live(doc, "send_profile")), None)
        pdata = profile.data if profile else {}
        model = str(args.get("model") or pdata.get("model") or _default_model(doc)).strip()
        provider = args.get("provider") or pdata.get("provider")
        strategy = args.get("strategy") or pdata.get("strategy")
        try:
            return generation_contract.normalize_profile(
                provider=provider,
                model=model,
                strategy=strategy,
                legacy_mode=pdata.get("mode"),
            )
        except ValueError as exc:
            raise ZeError("validation_failed", str(exc), context={
                "provider": provider, "model": model, "strategy": strategy,
            }) from exc

    # ---- prompt.optimize (stage) —— 慢调用不占 Document 锁 -------------------

    @registry.command(id="prompt.optimize", label="优化提示词", kind="stage",
                      touches=["prompt_optimization"],
                      description="把用户原话、时间线、画面引导和运动关键帧交给当前用户选择的"
                                  " Codex / Claude / Cursor 账号，生成适配目标视频模型的提示词。"
                                  "失败保留原文，不自动切换其他收费账号。",
                      params_schema={"type": "object", "properties": {
                          "requestId": {"type": "string", "label": "本次请求 id"},
                          "prompt": {"type": "string", "label": "用户原始说明"},
                          "optimizerProvider": {"type": "string",
                                                "label": "codex_cli_profile / claude_cli_profile / cursor_api_key"},
                          "optimizerModel": {"type": "string", "label": "优化模型"},
                          "provider": {"type": "string", "label": "目标视频模型提供方"},
                          "strategy": {"type": "string", "label": "目标提交策略"},
                          "model": {"type": "string", "label": "目标视频模型"}}})
    def _prompt_optimize(args, actor, source, doc):
        selected_provider = str(args.get("optimizerProvider") or "").strip()
        allowed = {"codex_cli_profile", "claude_cli_profile", "cursor_api_key"}
        if selected_provider not in allowed:
            raise ZeError("validation_failed", "请选择用于优化的 AI 账号",
                          context={"allowed": sorted(allowed)})
        selected_model = str(args.get("optimizerModel") or "").strip()
        if selected_provider == "cursor_api_key" and not selected_model:
            raise ZeError("validation_failed", "Cursor 优化需要从账号模型列表选择模型",
                          context={"provider": selected_provider})
        request_id = str(args.get("requestId") or _new_id("opt")).strip()
        if not request_id or any(ch in request_id for ch in ("/", "\\", "\x00")):
            raise ZeError("validation_failed", "requestId 非法", context={})
        # Document.get() is a strict selector and raises entity_not_found when
        # the id is new.  A prompt optimization request is normally new, so
        # duplicate detection must use the nullable lookup.
        if doc.find("prompt_optimization", request_id) is not None:
            raise ZeError("conflict", "这次优化请求已存在", context={"requestId": request_id})

        target = _generation_profile(doc, args)
        clips = [{**clip.data, "id": clip.id, "name": clip.name}
                 for clip in _live(doc, "clip")]
        assets_by_id = {
            asset.id: {**asset.data, "id": asset.id, "name": asset.name}
            for asset in _live(doc, "asset")
        }
        guides = []
        for entity in _visible_annotations(doc):
            shaped = _annotation_domain_shape(entity)
            shaped["keyframes"] = _keyframes_for(doc, entity.id)
            guides.append(shaped)
        if not guides:
            raise ZeError("validation_failed", "至少需要一个显示中的画面引导", context={})
        duration = max(
            (float(clip.get("start") or 0) + float(clip.get("duration") or 0)
             for clip in clips if clip.get("visible") is not False),
            default=0.0,
        )
        clips, guides = prompt_optimize.prepare_visible_context(
            clips=clips,
            guides=guides,
            assets_by_id=assets_by_id,
            total_duration=duration,
        )
        if not guides:
            raise ZeError(
                "validation_failed",
                "所有画面引导都被上面的视频图层盖住了；"
                "把引导移到视频层之上，或隐藏遮挡的图层",
                context={},
            )
        focus_groups = layer_compose.send_groups(
            guides,
            padding_sec=_padding(doc),
            total_duration=duration,
        )
        try:
            submission_windows = generation_contract.plan_submission_groups(
                provider=target["provider"],
                total_duration=duration,
                focus_groups=focus_groups,
            )
        except ValueError as exc:
            raise ZeError(
                "validation_failed",
                str(exc),
                context={"provider": target["provider"], "duration": duration},
            ) from exc
        reference_times = [
            round(float(cue.data.get("at") or 0), 3)
            for cue in _live(doc, "reference_frame")
            if cue.data.get("enabled") is not False
            and any(
                float(window["start"]) <= float(cue.data.get("at") or 0)
                <= float(window["end"])
                for window in submission_windows
            )
        ]
        guides = prompt_optimize.add_submission_timing(
            guides=guides,
            submission_windows=submission_windows,
        )
        frame_times = prompt_optimize.select_frame_times(
            reference_times=reference_times, guide_facts=guides,
        )
        project = _project(doc)
        project_data = project.data if project else {}
        source_prompt = str(args.get("prompt") or "").strip()
        # Shared project entity stores only the request/result draft for collaborators.
        # Optimizer account/model stay in the WorkItem payload (execution audit only).
        entity = Entity(
            type="prompt_optimization",
            id=request_id,
            name=f"提示词优化 · {target['modelId']}",
            data={
                "status": "queued",
                "sourcePrompt": source_prompt,
                "targetModel": target["modelId"],
                "optimizedPrompt": "",
                "warningsJson": "[]",
                "factsUsedJson": "[]",
                "lastError": "",
            },
        )
        doc.add(entity)
        forward, reverse = _create_op_patches(entity)
        item = WorkItem(
            item_id=request_id,
            entity_type="prompt_optimization",
            entity_id=request_id,
            payload={
                "optimizer_provider": selected_provider,
                "optimizer_model": selected_model,
                "target_model": target["modelId"],
                "target_provider": target["provider"],
                "generation_strategy": target["strategy"],
                "ratio": _ratio(doc),
                "duration": duration,
                "project_context": str(project_data.get("globalContext") or ""),
                "user_request": source_prompt,
                "timeline_facts": [
                    {
                        "id": clip["id"], "name": clip["name"],
                        "start": float(clip.get("start") or 0),
                        "duration": float(clip.get("duration") or 0),
                        "layer": float(clip.get("layer") or 0),
                        "visible": clip.get("visible") is not False,
                    }
                    for clip in clips
                ],
                "guide_facts": guides,
                "submission_windows": submission_windows,
                "frame_times": frame_times,
                "clips": clips,
                "assets_by_id": assets_by_id,
                "canvas": list(_canvas(doc)),
                "fps": _fps(doc),
            },
        )
        stage = StageSpec(
            id="prompt.optimize",
            label="优化提示词",
            run=lambda ctx, payload: prompt_optimize.run_prompt_optimize_stage(
                ctx, payload, config=config, asset_store=asset_store),
            cancel_handler=prompt_optimize.cancel_prompt_optimize,
            resource_scopes=("api.llm",),
        )
        return _launch(
            "prompt.optimize", source, actor,
            WorkList(run_id=uuid.uuid4().hex[:12], name="优化提示词", items=[item]),
            StagePlan.from_single(stage),
            patches=[forward], reverse_patches=[reverse],
        )

    @registry.command(id="edit.send", label="发送给 AI 改", kind="stage",
                      touches=["clip", "send_job"],
                      description="原视频符合模型时长要求时完整提交；只有过长时才按画面引导"
                                  "截取并补成模型区间内的整秒长度。把「可见图层 × 提交窗口」"
                                  "合成、加入引导、发给所选视频模型；"
                                  "回来的结果作为新图层插在最上面,不覆盖任何东西。",
                      params_schema={"type": "object", "properties": {
                          "prompt": {"type": "string", "label": "补一句全局的话(可选)"},
                          "provider": {"type": "string", "label": "omni / seedance"},
                          "strategy": {"type": "string",
                                       "label": "annotated_video / modified_frames"},
                          "model": {"type": "string", "label": "模型"},
                          "start": {"type": "number", "label": "手动指定区间起点(可选)"},
                          "end": {"type": "number", "label": "手动指定区间终点(可选)"}}})
    def _edit_send(args, actor, source, doc):
        extra = str(args.get("prompt") or "").strip()
        clips = [{**c.data, "id": c.id} for c in _live(doc, "clip")]
        assets = {a.id: a for a in _live(doc, "asset")}
        all_anns = []
        for entity in _visible_annotations(doc):
            guide = _annotation_domain_shape(entity)
            guide["keyframes"] = _keyframes_for(doc, entity.id)
            asset = assets.get(str(guide.get("asset_id") or ""))
            if asset is not None:
                guide["asset_name"] = asset.name
                ref = asset.data.get("processedRef") or asset.data.get("assetRef")
                if isinstance(ref, dict) and ref.get("sha256"):
                    guide["image_ref"] = ref
            all_anns.append(guide)
        if not all_anns:
            raise ZeError("validation_failed",
                          "没有显示中的画面引导 —— 先框选位置、放入素材、添加文字或绘制路径，"
                          "或者把关掉的那几行打开", context={})

        total = max(
            (
                float(c.get("start") or 0) + float(c.get("duration") or 0)
                for c in clips if c.get("visible") is not False
            ),
            default=0.0,
        )
        if total <= 0:
            raise ZeError("validation_failed", "没有可见图层 —— 至少要有一行没被隐藏",
                          context={})
        padding = _padding(doc)

        # 「看见什么就发什么」:被上面的视频层盖住的时间不算 —— 画面上看不见的框
        # 不该驱动发送、不该烧进画面、也不该出现在提示词里。
        exposure = layer_compose.exposed_windows(clips, all_anns, 0.0, max(total, 0.001))
        exposed = []
        for ann in all_anns:
            wins = exposure.get(ann.get("id"))
            if not wins:
                continue                     # 整段都被盖住 → 这条这次不参与
            first, last = wins[0][0], wins[-1][1]
            exposed.append({**ann, "windows": wins, "interval": [first, last]})
        if not exposed:
            raise ZeError("validation_failed",
                          "所有画面引导都被上面的视频图层盖住了；"
                          "把引导移到视频层之上，或隐藏遮挡的图层", context={})

        profile = _generation_profile(doc, args)
        provider = profile["provider"]
        strategy = profile["strategy"]
        model = profile["modelId"]

        # 原视频在模型时长范围内就完整提交；只有过长才按引导截取并补成整秒。
        if args.get("start") is not None and args.get("end") is not None:
            if float(args["end"]) - float(args["start"]) <= 0.01:
                raise ZeError("validation_failed", "选的这一段是空的；请重新设置时间范围",
                              context={"span": [args["start"], args["end"]]})
            focus_groups = [{"start": float(args["start"]), "end": float(args["end"]),
                             "numbers": []}]
        else:
            focus_groups = layer_compose.send_groups(
                exposed, padding_sec=padding, total_duration=total,
            )
        if not focus_groups:
            raise ZeError("validation_failed", "算不出要生成的时间段；检查画面引导的起止时间",
                          context={})
        try:
            groups = generation_contract.plan_submission_groups(
                provider=provider,
                total_duration=total,
                focus_groups=focus_groups,
            )
        except ValueError as exc:
            raise ZeError(
                "validation_failed",
                str(exc),
                context={"provider": provider, "duration": total},
            ) from exc
        # 两种提交方式仍允许搭配任一模型；这里只执行已经确认的传输硬约束
        # （Omni/Seedance 单次时长与各自请求字段），不替服务猜未验证的能力。

        cues = sorted(
            (r for r in _live(doc, "reference_frame") if r.data.get("enabled") is not False),
            key=lambda item: float(item.data.get("at") or 0),
        )
        canvas = list(_canvas(doc))
        fps = _fps(doc)
        # 同一根数轴:新层不能跟标注撞层序
        base_layer = layer_compose.next_layer(clips + all_anns)

        # 每一组先各自把计划算出来并校验 —— 有一组发不出去就整单拒,
        # 不做「发一半」:人点一下按钮,要么全发出去,要么知道为什么一个都没发。
        prepared = []
        for offset, group in enumerate(groups):
            plan = layer_compose.compose_plan(clips, group["start"], group["end"])
            if not plan["valid"]:
                raise ZeError("validation_failed",
                              f"{group['start']}–{group['end']}s 这一段发不出去:{plan['reason']}",
                              context={"span": [group["start"], group["end"]]})
            span_duration = float(group["end"]) - float(group["start"])
            try:
                generation_contract.compile_submission_request(
                    provider=provider,
                    model=model,
                    strategy=strategy,
                    ratio=_ratio(doc),
                    span_duration=span_duration,
                )
            except (TypeError, ValueError) as exc:
                raise ZeError(
                    "validation_failed",
                    str(exc),
                    context={"span": [group["start"], group["end"]],
                             "provider": provider, "model": model},
                ) from exc
            _resolve_refs(plan, assets)
            anns = layer_compose.annotations_in_span(exposed, group["start"], group["end"])
            lines = _guide_lines(anns)
            prompt = "\n".join(lines + ([""] + [extra] if extra else [])) if lines else extra
            if not prompt.strip():
                raise ZeError("validation_failed",
                              "这一段里的画面引导都没有说明要怎么改；补充文字或整体说明",
                              context={"span": [group["start"], group["end"]]})
            explicit_times = [
                round(float(cue.data.get("at") or 0), 3)
                for cue in cues
                if float(group["start"]) <= float(cue.data.get("at") or 0) <= float(group["end"])
            ]
            motion_times = []
            for ann in anns:
                origin = float(ann.get("keyframe_origin") or ann["interval"][0])
                for frame in ann.get("keyframes") or ():
                    at = round(origin + float(frame.get("time") or 0), 3)
                    if not (float(group["start"]) <= at <= float(group["end"])):
                        continue
                    windows = ann.get("windows") or ()
                    if windows and not any(float(win[0]) <= at <= float(win[1]) for win in windows):
                        continue
                    motion_times.append(at)
            frame_times = sorted(set(explicit_times + motion_times))
            if strategy == generation_contract.MODIFIED_FRAMES and not frame_times:
                raise ZeError(
                    "validation_failed",
                    f"{group['start']}–{group['end']}s 内没有修改画面;"
                    "调整矩形或图片的位置，或手动加入一个参考画面",
                    context={"span": [group["start"], group["end"]]},
                )
            prepared.append((group, plan, anns, prompt, frame_times, base_layer + offset))

        # 运行时缺位放在最后判:先把「你选的这些段有什么毛病」说清楚,
        # 再谈机器跑不跑得动 —— 前者人能立刻改,后者人改不了。
        _need_supervisor()

        run_id = uuid.uuid4().hex[:12]
        items, fwd, rev = [], [], []
        for group, plan, anns, prompt, frame_times, new_layer in prepared:
            tag = ("引导" + "".join(str(n) for n in group["numbers"])) if group["numbers"] else "手动段"
            job = Entity(type="send_job", id=_new_id("job"),
                         name=f"改 {group['start']}s–{group['end']}s · {tag}",
                         data=_compact({
                             "status": "queued",
                             "mode": 2 if strategy == generation_contract.MODIFIED_FRAMES else 1,
                             "provider": provider,
                             "strategy": strategy,
                             "model": model,
                             "promptSnapshot": prompt,
                             "editedFramesJson": json.dumps(frame_times, ensure_ascii=False),
                             "lastError": "",
                         }))
            doc.add(job)
            f, r = _create_op_patches(job)
            fwd.append(f)
            rev.append(r)
            items.append(WorkItem(
                item_id=job.id, entity_type="send_job", entity_id=job.id,
                artifacts=(f"pipeline_artifacts/{run_id}/{job.id}.txt",),
                payload={"plan": plan, "prompt": prompt, "model": model,
                         "provider": provider, "strategy": strategy,
                         "annotations": anns, "reference_times": frame_times,
                         "ratio": _ratio(doc),
                         "canvas": canvas, "fps": fps, "new_layer": new_layer,
                         # 每层的几何:合成时按它把素材摆进画布(INV-5)
                         "clips_by_id": {c["id"]: c for c in clips}}))

        stage = StageSpec(id="edit.send", label="编辑这一段",
                          run=lambda ctx, a: layer_edit_stage.run_edit_stage(
                              ctx, a, asset_store=asset_store),
                          resource_scopes=("cpu.ffmpeg", "api.videogen"))
        name = (f"改 {len(items)} 段" if len(items) > 1
                else items[0].payload and f"改 {groups[0]['start']}s–{groups[0]['end']}s")
        return _launch("edit.send", source, actor,
                       WorkList(run_id=run_id, name=str(name), items=items),
                       StagePlan.from_single(stage),
                       patches=fwd, reverse_patches=rev)

    # ---- export.render (stage) —— 精修完拿出去 --------------------------------

    @registry.command(id="export.render", label="导出", kind="stage",
                      touches=["export"],
                      description="把整条时间线(或指定区间)按画布规格合成、编码,落成可下载的成品。"
                                  "画面引导只参与生成，**不会出现在导出成片中**。"
                                  "隐藏的图层不参与;每层按自己的几何摆进画布。",
                      params_schema={"type": "object", "properties": {
                          "preset": {"type": "string",
                                     "label": "预设(mp4_high / mp4_std / webm)"},
                          "start": {"type": "number", "label": "起点(秒,可选)"},
                          "end": {"type": "number", "label": "终点(秒,可选)"}}})
    def _export_render(args, actor, source, doc):
        clips = [{**c.data, "id": c.id} for c in _live(doc, "clip")]
        visible = [c for c in clips if c.get("visible") is not False
                   and float(c.get("duration") or 0) > 0]
        if not visible:
            raise ZeError("validation_failed",
                          "没有显示中的视频图层 —— 导不出东西;先导入素材或把关掉的那几行打开",
                          context={})

        # 每一层都要有真视频文件(生成层自带 videoRef,原片层回到它引用的素材)
        assets = {a.id: a for a in _live(doc, "asset")}
        for c in visible:
            if not (isinstance(c.get("videoRef"), dict) and c["videoRef"].get("sha256")):
                asset = assets.get(c.get("assetId") or "")
                ref = asset.data.get("assetRef") if asset is not None else None
                if not (isinstance(ref, dict) and ref.get("sha256")):
                    raise ZeError("validation_failed",
                                  f"图层「{c.get('id')}」还没有视频文件;先导入素材",
                                  context={"clip": c.get("id")})
                c["videoRef"] = ref

        cw, ch = _canvas(doc)
        if cw <= 0 or ch <= 0:
            raise ZeError("validation_failed", "先设置画布宽高", context={})

        key, spec = render.resolve_preset(args.get("preset"))
        _need_supervisor()

        run_id = uuid.uuid4().hex[:12]
        ent = Entity(type="export", id=_new_id("export"),
                     name=f"导出 · {int(cw)}×{int(ch)} · {spec['label']}",
                     data=_compact({"status": "queued", "preset": key,
                                    "width": float(cw), "height": float(ch),
                                    "fps": _fps(doc), "lastError": ""}))
        doc.add(ent)
        fwd, rev = _create_op_patches(ent)

        item = WorkItem(item_id=ent.id, entity_type="export", entity_id=ent.id,
                        artifacts=(f"pipeline_artifacts/{run_id}/{ent.id}.txt",),
                        payload={"clips": clips, "canvas": [cw, ch], "fps": _fps(doc),
                                 "preset": key, "start": args.get("start"),
                                 "end": args.get("end")})
        stage = StageSpec(id="export.render", label="导出成品",
                          run=lambda ctx, a: export_stage.run_export_stage(
                              ctx, a, asset_store=asset_store),
                          resource_scopes=("cpu.ffmpeg",))
        return _launch("export.render", source, actor,
                       WorkList(run_id=run_id, name=ent.name, items=[item]),
                       StagePlan.from_single(stage),
                       patches=[fwd], reverse_patches=[rev])
