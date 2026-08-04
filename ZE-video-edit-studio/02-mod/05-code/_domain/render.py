"""时间线渲染 —— 把一摞图层在一段时间上合成为一个文件。

发送给 AI 的那一段、和导出的成品,走的是**同一条链**:

    可见图层 × 时间区间 → compose_plan 切刀
      → 逐刀:截段 → 按该层几何摆进画布(clip_geometry)
      → 空档补黑帧(等长,否则时间换算失准)
      → 拼接
      → [仅 AI 面] 烧标注

两者唯一的差别是**要不要烧标注**:AI 面要(模型靠框认位置),导出面绝不能有(INV-2)。
把这件事收在一个函数里,就不会出现"发出去的和导出的不是同一套几何"这种漂移。

本模块编排 ffmpeg 调用,不碰实体、不碰 AssetStore —— 调用方负责把素材字节喂进来
(``fetch_bytes``)与决定产物去哪。
"""
from __future__ import annotations

import os
from typing import Any, Callable, Mapping, Sequence

from . import clip_geometry, ffmpeg_ops, layer_compose
from .errors import DomainError

#: 导出预设。名字 → (容器扩展, 视频编码器, CRF, preset, 音频编码器)
EXPORT_PRESETS: dict[str, dict[str, Any]] = {
    "mp4_high": {"ext": ".mp4", "vcodec": "libx264", "crf": 18,
                 "preset": "medium", "acodec": "aac", "label": "MP4 · 高质量"},
    "mp4_std": {"ext": ".mp4", "vcodec": "libx264", "crf": 23,
                "preset": "veryfast", "acodec": "aac", "label": "MP4 · 标准"},
    "webm": {"ext": ".webm", "vcodec": "libvpx-vp9", "crf": 32,
             "preset": None, "acodec": "libopus", "label": "WebM · VP9"},
}
DEFAULT_PRESET = "mp4_high"


def resolve_preset(name: str | None) -> tuple[str, dict[str, Any]]:
    """预设名 → (规范名, 规格)。认不出的名字退回默认,不报错。"""
    key = str(name or "")
    if key not in EXPORT_PRESETS:
        key = DEFAULT_PRESET
    return key, EXPORT_PRESETS[key]


def build_encode_argv(binary: str, src: str, out: str, preset: Mapping[str, Any]) -> list[str]:
    """把拼好的中间产物按预设编码成交付文件。"""
    argv = [binary, "-y", "-i", src, "-c:v", str(preset["vcodec"]),
            "-crf", str(preset["crf"]), "-pix_fmt", "yuv420p"]
    if preset.get("preset"):
        argv += ["-preset", str(preset["preset"])]
    argv += ["-c:a", str(preset["acodec"]), "-movflags", "+faststart", out]
    return argv


def compose_span(plan: Mapping[str, Any], work: str, *,
                 fetch_bytes: Callable[[Mapping[str, Any]], bytes],
                 clips_by_id: Mapping[str, Mapping[str, Any]],
                 canvas: tuple[int, int], fps: float = 30.0) -> str:
    """按合成计划把这一段拼出来 —— 逐刀截、按几何摆进画布、空档补黑帧、拼接。

    ``plan`` 来自 :func:`layer_compose.compose_plan`;``clips_by_id`` 用来取每一刀
    所属图层的几何。产出是画布规格的一个文件路径(在 ``work`` 目录下)。

    两件必须做的事,少一件后续都会错:

    * **空档填平**:``plan["gaps"]`` 是没有任何可见图层盖住的时间。只把几刀首尾相接
      会把洞压缩掉,拼出来比时间线上那一段短;而烧标注是「减掉这一段起点」的线性换算,
      于是洞之后每个框都在错的时刻出现。填平后两者等长,换算才成立。
    * **统一规格**:两个来源不同的片段直接 concat 会挂;而且只有都落到画布规格,
      标注 bbox 的占比换算才在同一个坐标系里(INV-5)。
    """
    width, height = int(canvas[0] or 0), int(canvas[1] or 0)
    if width <= 0 or height <= 0:
        raise DomainError("缺画布尺寸,合成不了;先给项目设置画布宽高")

    ordered: list[tuple[float, str, Any]] = []
    for cut in plan.get("cuts") or ():
        ordered.append((float(cut["timeline_start"]), "cut", cut))
    for gap in plan.get("gaps") or ():
        ordered.append((float(gap[0]), "gap", gap))
    ordered.sort(key=lambda item: item[0])

    pieces: list[str] = []
    for idx, (_at, kind, payload) in enumerate(ordered):
        out = os.path.join(work, f"piece_{idx}.mp4")
        if kind == "gap":
            ffmpeg_ops.black_filler(out, float(payload[1]) - float(payload[0]),
                                    width=width, height=height, fps=fps)
            pieces.append(out)
            continue

        cut = payload
        ref = cut.get("video_ref")
        if not (isinstance(ref, dict) and ref.get("sha256")):
            raise DomainError(f"图层「{cut.get('clip_id')}」还没有视频文件,拼不出这一段")
        src = os.path.join(work, f"src_{idx}.mp4")
        with open(src, "wb") as fh:
            fh.write(fetch_bytes(ref))
        # 截之前先确认素材真有这么长。图层的 duration 是文档里写的,素材是磁盘上的 ——
        # 两者不一致时(图层声称 3s、素材只有 2s),ffmpeg 会安静地截出一个没有视频轨的
        # 碎片,然后在后面的 concat 里以完全无关的报错炸掉。宁可在这里说人话。
        src_in, src_out = float(cut["source_in"]), float(cut["source_out"])
        have = ffmpeg_ops.probe_duration(src)
        # 要的比素材有的多 → 直接说清楚。不许悄悄截短:那样导出的成品会比时间线短,
        # 而人完全看不出来哪儿少了(单刀时更隐蔽,连 concat 都不会报错)。
        if have > 0 and src_out > have + 0.05:
            raise DomainError(
                f"图层「{cut.get('clip_id')}」要取素材的 {src_in:.2f}–{src_out:.2f}s,"
                f"但这段素材只有 {have:.2f}s —— 图层比素材长,先把它的时长改短")
        cutout = os.path.join(work, f"cut_{idx}.mp4")
        ffmpeg_ops.cut_segment(src, cutout, src_in, src_out - src_in)
        ffmpeg_ops.normalize_segment(cutout, out, width=width, height=height, fps=fps,
                                     clip=clips_by_id.get(str(cut.get("clip_id")), {}))
        pieces.append(out)

    if not pieces:
        raise DomainError("这一段没有任何可拼的图层")
    if len(pieces) == 1:
        return pieces[0]
    merged = os.path.join(work, "composed.mp4")
    ffmpeg_ops.concat_videos(pieces, merged)
    return merged


def render_timeline(clips: Sequence[Mapping[str, Any]], work: str, *,
                    fetch_bytes: Callable[[Mapping[str, Any]], bytes],
                    canvas: tuple[int, int], fps: float = 30.0,
                    start: float | None = None, end: float | None = None,
                    preset: str | None = None,
                    out_path: str | None = None) -> dict[str, Any]:
    """导出:把整条时间线(或指定区间)合成并编码成交付文件。

    **不烧任何标注**(INV-2:导出的是给人看的成品,标注只属于 AI 面)。
    区间缺省 = 从 0 到最后一个可见图层的结束时间。

    返回 ``{"path", "start", "end", "duration", "preset", "ext"}``。
    """
    live = layer_compose.visible_layers(clips)
    if not live:
        raise DomainError("没有可见的视频图层,导不出东西")
    t0 = 0.0 if start is None else max(0.0, float(start))
    total = max(float(c.get("start") or 0) + float(c.get("duration") or 0) for c in live)
    t1 = total if end is None else min(total, float(end))
    if t1 - t0 <= 0.01:
        raise DomainError("导出区间是空的")

    plan = layer_compose.compose_plan(clips, t0, t1)
    if not plan.get("valid"):
        raise DomainError(plan.get("reason") or "这一段合成不出来")

    by_id = {str(c.get("id")): c for c in clips}
    composed = compose_span(plan, work, fetch_bytes=fetch_bytes, clips_by_id=by_id,
                            canvas=canvas, fps=fps)

    key, spec = resolve_preset(preset)
    out = out_path or os.path.join(work, f"export{spec['ext']}")
    binary = ffmpeg_ops.require_tool("ffmpeg")
    ffmpeg_ops._run(build_encode_argv(binary, composed, out, spec),  # noqa: SLF001
                    what="ffmpeg export encode")
    return {"path": out, "start": round(t0, 2), "end": round(t1, 2),
            "duration": round(t1 - t0, 2), "preset": key, "ext": spec["ext"],
            "canvas": [int(canvas[0]), int(canvas[1])], "fps": float(fps)}
