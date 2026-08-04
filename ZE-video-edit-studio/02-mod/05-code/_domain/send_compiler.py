"""send-compiler (V4 + V6): the shared prompt/dispatch compiler.

One implementation serves ① (edit modes), ② (segment generation) and ④ (batch
naming reference). Single-send is the 1×1 special case of batch. The compiler is
a PURE function: given annotations / assets / model / mode it returns a
*compiled snapshot* (the exact prompt, file order, resolved reference tokens,
lint results, render surfaces, capability routing, safety diff). It never
dispatches — that is send.dispatch's job — so AI/CLI can compile-then-review
before spending money (auditable snapshot in send_job).

Covers:
- V4 capability routing: is the model able to serve the mode? If not, emit an
  explicit routing suggestion (never silently switch model/mode).
- V6 naming reference: ``{素材名}`` → per-model token (@file_0 / @Image1 /
  [Image 1] / <<<image_1>>> / natural language) + file order + reference lint
  (missing = red/block, unreferenced = yellow, incompatible media = drop+warn).
- V6 edit templates: mode 1/2A/2B/3 prompt bodies compiled from annotations,
  with explicit per-item annotation removal (root-cause fix for arrow residue).
- Safety rewrite pass (real-person name → appearance).
- Timestamped storyboard form for segments > 6s (Seedance long-prompt optimum).
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from . import capability_matrix as caps
from . import clip_math
from . import ffmpeg_ops
from . import ref_binding
from . import safety_rewrite

# 占位符只有一处真相 —— 绑定层拥有它,这里只是引用。
_PLACEHOLDER = ref_binding.PLACEHOLDER

STORYBOARD_THRESHOLD_SEC = 6.0


# --- region description ------------------------------------------------------

def describe_region(bbox: Mapping[str, float], canvas_w: float, canvas_h: float) -> str:
    """Map a bbox to a colloquial 9-grid position phrase (画面左上 / 中央 …)."""
    cx = (bbox["x"] + bbox["w"] / 2) / max(canvas_w, 1)
    cy = (bbox["y"] + bbox["h"] / 2) / max(canvas_h, 1)
    col = "左" if cx < 1 / 3 else ("右" if cx > 2 / 3 else "中")
    row = "上" if cy < 1 / 3 else ("下" if cy > 2 / 3 else "中")
    if row == "中" and col == "中":
        return "画面中央"
    return f"画面{row}{col}".replace("中中", "中")


# --- naming reference (② / ④ core) ------------------------------------------

def _asset_pixels(asset: Mapping[str, Any]) -> tuple[int, int] | None:
    px = asset.get("pixels")
    if isinstance(px, (list, tuple)) and len(px) == 2:
        return int(px[0]), int(px[1])
    return None


def compile_naming_reference(template: str, assets_in_order: Sequence[Mapping[str, Any]],
                             model: str, *,
                             mode: str = "reference-to-video") -> dict[str, Any]:
    """把 ``{素材名}`` 模板编译成目标模型认的提示词 + **实发文件**。

    ``assets_in_order`` 是有序素材,每项 ``{"name", "media_type"}``,可选 ``"ref"``
    (真实文件引用)与 ``"pixels"``。文件顺序 = 素材在模板里**首次出现**的顺序
    —— 按位置绑定的模型(Seedance/Grok 等)靠的就是这个顺序。

    记号与文件由 :func:`ref_binding.compile_inputs` **一次编译同时产出**,
    这里不再自己算序号 —— 两处各算一遍正是「记号指向错误文件」的根源。
    """
    canonical = caps.canonical_model(model)
    by_name = {a["name"]: a for a in assets_in_order}
    referenced = [m.group(1) for m in _PLACEHOLDER.finditer(template)]

    lint: dict[str, Any] = {"missing": [], "unreferenced": [], "incompatible": []}
    ordered: list[ref_binding.Input] = []
    seen: set[str] = set()
    dropped: set[str] = set()

    for name in referenced:
        if name not in by_name:
            if name not in lint["missing"]:
                lint["missing"].append(name)          # 红:引用了没登记的素材
            continue
        asset = by_name[name]
        media = asset["media_type"]
        # 按**本模式**判准入(不是按模型)—— 同一模型不同模式收的东西可以不一样。
        if not caps.mode_accepts_media(canonical, mode, media):
            if name not in [d["name"] for d in lint["incompatible"]]:
                lint["incompatible"].append({"name": name, "media_type": media})
            dropped.add(name)
            continue
        if name not in seen:
            seen.add(name)
            ordered.append(ref_binding.Input(name=name, kind=media, ref=asset.get("ref"),
                                             pixels=_asset_pixels(asset)))

    # 不兼容的素材先就地说明,免得绑定层把它当成「引用了不存在的输入」再报一次。
    working = _PLACEHOLDER.sub(
        lambda m: f"（已剔除不兼容素材「{m.group(1)}」）" if m.group(1) in dropped else m.group(0),
        template)

    compiled = ref_binding.compile_inputs(canonical, mode, working, ordered)

    references = [{"name": o["name"], "index": o["index"],
                   "token": compiled.tokens.get(o["name"]) or f"「{o['name']}」",
                   "media_type": o["media_type"]} for o in compiled.order]

    for name in by_name:
        if name not in referenced and name not in lint["unreferenced"]:
            lint["unreferenced"].append(name)         # 黄:登记了但没被用到

    verdict = "red" if lint["missing"] else (
        "yellow" if (lint["unreferenced"] or lint["incompatible"]) else "green")

    return {
        "prompt": compiled.prompt,
        "file_order": compiled.order,
        "files": compiled.files,          # 实发文件 —— 与记号同源,顺序即契约
        "references": references,
        "lint": lint,
        "lint_verdict": verdict,
        "syntax_family": caps.syntax_family(canonical),
        "errors": compiled.errors,        # 发送前拦截(非空即不得发送)
        "warnings": compiled.warnings,
    }


def storyboard_scaffold(duration_sec: float, fps: float = 30.0) -> list[dict[str, float]]:
    """Timestamped storyboard segment scaffold for long (>6s) prompts.

    Returns ~3s windows covering the whole duration; the strategy layer or human
    fills the description of each. First window is the Hook, last is the close.
    """
    if duration_sec <= STORYBOARD_THRESHOLD_SEC:
        return []
    windows: list[dict[str, float]] = []
    t = 0.0
    step = 3.0
    while t < duration_sec:
        windows.append({"start": round(t, 2), "end": round(min(t + step, duration_sec), 2)})
        t += step
    return windows


# --- edit templates (① core) ------------------------------------------------

def _annotation_inventory(annotations: Sequence[Mapping[str, Any]],
                          canvas_w: float, canvas_h: float) -> tuple[str, str, str]:
    """Build (inventory, task, removal) text blocks from annotation entities."""
    inv_lines, task_lines, removal_lines = [], [], []
    for idx, ann in enumerate(sorted(annotations, key=lambda a: a.get("number", 0))):
        n = ann.get("number")
        label = ann.get("label", "")
        region = describe_region(ann["bbox"], canvas_w, canvas_h)
        # 颜色取自 ffmpeg_ops 的调色板 —— 视频里真画的就是这个颜色。
        # 两处各写一遍颜色 = 提示词说红框、画面上是青框,模型照着描述找不到东西。
        # 档位按标注自己的编号取(color_slot),与烧框、与界面条色同一个锚。
        color = ffmpeg_ops.annotation_paint(ann, ffmpeg_ops.color_slot(ann, idx))[1]
        inv_lines.append(f"{_circled(n)}{color}矩形框 — 位于{region}，标注文字「{label}」")
        task_lines.append(f"{_circled(n)}{label}")
        removal_lines.append(f"{_circled(n)}{color}矩形框与说明文字「{label}」")
    return ("；".join(inv_lines), "\n".join(task_lines), "、".join(removal_lines))


def _circled(n: Any) -> str:
    circled = "①②③④⑤⑥⑦⑧⑨⑩"
    if isinstance(n, int) and 1 <= n <= len(circled):
        return circled[n - 1]
    return f"({n})"


def compile_edit_mode_one(annotations: Sequence[Mapping[str, Any]],
                          canvas_w: float, canvas_h: float) -> str:
    """模板一 · 标注入视频 (edit protocol, gemini-omni)."""
    inv, task, removal = _annotation_inventory(annotations, canvas_w, canvas_h)
    return (
        "【输入】一段带临时编辑标注的视频。画面内标注元素清单：\n"
        f"{inv}\n\n"
        "【任务】执行上述标注要求的修改：\n"
        f"{task}\n\n"
        "【硬性要求】\n"
        f"1. 输出视频必须移除全部标注元素：{removal}。\n"
        "2. 除标注要求的修改外，其余一切与原片保持一致：人物与动作、场景与物件、"
        "镜头运动与节奏、构图、光影、色调、时长、音频。\n"
        "3. 不得修改标注区域之外的任何内容；不新增文字、字幕、水印、logo。"
    )


def compile_edit_mode_two_image(annotations: Sequence[Mapping[str, Any]],
                                canvas_w: float, canvas_h: float) -> str:
    """模板二A · 图步 (send to image service)."""
    inv, _, _ = _annotation_inventory(annotations, canvas_w, canvas_h)
    return (
        f"输入图为视频关键帧，画面内标注：{inv}。\n"
        "输出：已执行全部标注修改、且不含任何标注元素的同尺寸画面；\n"
        "修改处之外逐像素保持原样；不新增文字/水印。"
    )


def compile_edit_mode_two_video(ref_image_token: str, source_video_token: str) -> str:
    """模板二B · 视频步 (send to video model; ref image → replace region)."""
    return (
        f"{ref_image_token}为目标画面状态（已完成修改的关键帧）。\n"
        f"以参考图为准替换{source_video_token}中对应区域的内容；\n"
        "镜头运动、节奏、人物动作、场景、光影、时长、音频等其余一切与原视频完全一致；\n"
        "不得出现任何标注、红框、说明文字、箭头、水印。"
    )


# --- top-level dispatcher ----------------------------------------------------

def compile_send(*, mode: int, model: str,
                 annotations: Sequence[Mapping[str, Any]] | None = None,
                 assets_in_order: Sequence[Mapping[str, Any]] | None = None,
                 template: str | None = None,
                 canvas_w: float = 1920, canvas_h: float = 1080,
                 segment_duration: float | None = None,
                 padding_sec: float = 0.5,
                 source_duration: float | None = None,
                 name_map: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Compile a full send snapshot (the payload frozen into a send_job).

    - mode 1: uses ``annotations`` → template one (edit protocol).
    - mode 2/3: uses ``assets_in_order`` + optional ``template`` (naming ref) or
      falls back to template two-B/three.
    Returns a dict with ``prompt``, ``file_order``, ``references``, ``lint``,
    ``capability``, ``routing_suggestion``, ``safety``, ``render_surfaces``,
    ``warnings``, ``blocked``.
    """
    annotations = list(annotations or [])
    canonical = caps.canonical_model(model)
    required_cap = caps.MODE_CAPABILITY[mode]

    # 「这一步能不能用这个模型」= 能力 ∩ 这一步必须上传的媒体类型 ∩ 配额。
    # 只看能力会高估:veo 会多参考,但模式二必须把原视频送上去,而 veo 收不了视频 ——
    # 从前它会一路编译通过、把视频当「不兼容素材」悄悄剔除,提示词里却还写着
    # 「替换原视频中对应区域」,发出去必然不对。
    asset_kinds = sorted({a["media_type"] for a in (assets_in_order or [])})
    eligible = caps.models_for_mode(mode, extra_kinds=asset_kinds)
    capability_ok = canonical in eligible

    routing_suggestion = None
    warnings: list[str] = []
    if not capability_ok:
        # 明确的降级建议 —— 绝不静默换模型/换模式(V4 纪律)
        suggest_mode = 2 if mode == 1 else mode
        alt_models = (caps.models_for_mode(2, extra_kinds=asset_kinds) if mode == 1
                      else eligible)
        if not caps.model_supports(canonical, required_cap):
            reason = f"{canonical} 不支持 {required_cap}（模式 {mode} 所需能力）"
        else:
            need = "/".join(ref_binding.zh_kind(k) for k in caps.MODE_INPUT_KINDS[mode])
            reason = (f"{canonical} 会 {required_cap}，但模式 {mode} 必须送 {need}，"
                      f"它收不下 —— 换用能收的模型")
        routing_suggestion = {
            "reason": reason,
            "suggest_mode": suggest_mode,
            "candidate_models": alt_models,
        }
        warnings.append(reason)

    render_surfaces = _render_surfaces_for_mode(mode)

    # build the prompt body
    files: list[Any] = []
    bind_errors: list[str] = []
    if mode == 1:
        prompt = compile_edit_mode_one(annotations, canvas_w, canvas_h)
        # 模式一发的是「烧了标注框的视频段」。这个渲染面由调用方在派发前产出并登记,
        # 编译期只占位;``files`` 保持空 = 诚实地表示「编译器手上没有文件」。
        file_order: list[dict[str, Any]] = [{"name": "__ai_surface_segment__",
                                             "media_type": "video", "index": 0}]
        references: list[dict[str, Any]] = []
        lint = {"missing": [], "unreferenced": [], "incompatible": []}
        lint_verdict = "green"
    else:
        if template is not None:
            nr = compile_naming_reference(template, assets_in_order or [], canonical,
                                          mode=required_cap)
            prompt = nr["prompt"]
            file_order = nr["file_order"]
            references = nr["references"]
            lint = nr["lint"]
            lint_verdict = nr["lint_verdict"]
            files = nr["files"]
            bind_errors = nr["errors"]
        else:
            # 默认模板 二B / 三:第一张参考图 + 原视频。
            # 记号不在这里手算 —— 走同一个绑定层,否则按类型计数的模型(kling)
            # 会把「第一个视频」算成 video_2(它在文件队列里排第 2)。
            assets_in_order = list(assets_in_order or [])
            first_img = next((a for a in assets_in_order if a["media_type"] == "image"), None)
            src = next((a for a in assets_in_order if a["media_type"] == "video"), None)
            default_inputs: list[ref_binding.Input] = []
            if first_img:
                default_inputs.append(ref_binding.Input(
                    name=first_img.get("name", "参考图"), kind="image",
                    ref=first_img.get("ref"), pixels=_asset_pixels(first_img)))
            if src:
                default_inputs.append(ref_binding.Input(
                    name=src.get("name", "原视频"), kind="video", ref=src.get("ref")))
            img_slot = ("{%s}" % first_img["name"]) if first_img else "参考图"
            src_slot = ("{%s}" % src["name"]) if src else "原视频"
            nr = compile_naming_reference(
                compile_edit_mode_two_video(img_slot, src_slot),
                [{"name": r.name, "media_type": r.kind, "ref": r.ref} for r in default_inputs],
                canonical, mode=required_cap)
            prompt = nr["prompt"]
            file_order = nr["file_order"]
            references = nr["references"]
            lint = nr["lint"]
            lint_verdict = nr["lint_verdict"]
            files = nr["files"]
            bind_errors = nr["errors"]

    # mode 2 (拆参考图) is a two-step chain: image step first (send annotation frame
    # to the image service to produce a clean modified keyframe), then the video step
    # (the ``prompt`` above). Compile the image-step (template 2A) prompt too.
    image_step_prompt = None
    if mode == 2 and annotations:
        image_step_prompt = compile_edit_mode_two_image(annotations, canvas_w, canvas_h)

    # V3 发送区间:参与标注的区间取并集 + 前后 padding,clamp 到素材时长。
    # 未滚动的单帧区间 [ta,ta] 正是靠 padding 撑开成可发段(拍板①的区间语义在此落地)。
    send_interval = None
    intervals = [tuple(a["interval"]) for a in annotations
                 if isinstance(a.get("interval"), (list, tuple)) and len(a["interval"]) == 2]
    union = clip_math.interval_union(intervals) if intervals else None
    if union is not None:
        total = float(source_duration) if source_duration else max(union[1] + padding_sec, union[1])
        t0, t1 = clip_math.pad_interval(union, padding_sec, total)
        send_interval = {
            "start": t0, "end": t1,
            "duration": clip_math.round2(t1 - t0),
            "annotation_union": [union[0], union[1]],
            "padding_sec": clip_math.round2(padding_sec),
            "degenerate_union": union[0] == union[1],  # 单帧区间靠 padding 撑开
        }

    # storyboard hint for long generation segments
    storyboard = storyboard_scaffold(segment_duration, ) if segment_duration else []
    if storyboard:
        warnings.append(f"段时长 {segment_duration}s > {STORYBOARD_THRESHOLD_SEC}s：建议按时间戳分镜组织提示词")

    # safety rewrite pass over the final prompt
    safety = safety_rewrite.rewrite(prompt, name_map=name_map)
    if safety["changed"]:
        prompt = safety["rewritten"]

    # 能力不匹配现在是**拦**,不再只是警告 —— 之前 mode 1 配 seedance 能一路发到服务,
    # 而 seedance 根本不会 edit,钱花了才知道。
    blocked = (bool(lint["missing"]) or bool(safety["blocked"])
               or bool(bind_errors) or not capability_ok)
    if lint["missing"]:
        warnings.append(f"引用不存在的素材：{lint['missing']}（红线，禁止派发）")
    for err in bind_errors:
        warnings.append(f"输入绑定不通过：{err}（红线，禁止派发）")
    if lint["incompatible"]:
        warnings.append(f"模型不兼容素材已剔除：{[c['name'] for c in lint['incompatible']]}")
    if safety.get("unrewritable"):
        warnings.append(f"名字无可用改写：{safety['unrewritable']}（红线，禁止派发；补外观描述后重编译）")
    elif safety["flagged"]:
        warnings.append(f"疑似真人/名人引用：{safety['flagged']}（黄，请人工复核；已知名已自动改写）")

    return {
        "mode": mode,
        "model": canonical,
        "prompt": prompt,
        "file_order": file_order,
        "files": files,                          # 实发文件,与记号同源(模式一由调用方补渲染面)
        "bind_errors": bind_errors,              # 输入绑定的发送前拦截理由
        "references": references,
        "lint": lint,
        "lint_verdict": lint_verdict,
        "capability": {"required": required_cap, "ok": capability_ok},
        "routing_suggestion": routing_suggestion,
        "safety": safety,
        "render_surfaces": render_surfaces,
        "storyboard": storyboard,
        "image_step_prompt": image_step_prompt,  # mode 2 图步 (2A);其余模式为 None
        "send_interval": send_interval,          # V3 截段区间(标注区间并集 + padding)
        "warnings": warnings,
        "blocked": blocked,
    }


def _render_surfaces_for_mode(mode: int) -> list[str]:
    """Which V5 render surfaces a mode needs to produce before dispatch."""
    if mode == 1:
        return ["ai", "clean"]          # burn annotations into segment; clean for repaste
    if mode == 2:
        return ["ai", "clean", "keyframes"]  # image step needs annotation frame + clean
    return ["clean"]                     # mode 3: already has reference image
