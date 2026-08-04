"""Local ffmpeg / ffprobe operations for the video domain.

Ported from ZE-video-generation-timeline's ``ffmpeg_ops.py`` and extended with
the spatiotemporal **mask_merge** (the video port of S2D change-extract).

Discipline (kept from the source):
- All subprocess calls use **list argv**, never a shell string.
- ``build_*_argv`` are PURE constructors — tests assert the argv without running
  ffmpeg.
- A missing binary raises ``RequiresRuntime`` (structured), so the stage layer
  can surface "install ffmpeg" instead of a hard failure.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Any, Mapping, Sequence

from . import clip_geometry
from .errors import DomainError, RequiresRuntime

_ERR_TAIL = 800


# --- binary discovery --------------------------------------------------------

def ffmpeg_bin() -> str | None:
    return os.environ.get("ZE_FFMPEG_BIN") or shutil.which("ffmpeg")


def ffprobe_bin() -> str | None:
    return os.environ.get("ZE_FFPROBE_BIN") or shutil.which("ffprobe")


def require_tool(name: str) -> str:
    resolver = {"ffmpeg": ffmpeg_bin, "ffprobe": ffprobe_bin}.get(name)
    binary = resolver() if resolver else shutil.which(name)
    if not binary:
        raise RequiresRuntime(f"{name} 不可用；请安装 {name} 或设置 ZE_{name.upper()}_BIN",
                              context={"tool": name})
    return binary


def _run(argv: Sequence[str], *, what: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(list(argv), capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-_ERR_TAIL:]
        raise DomainError(f"{what} 失败: {tail}", context={"argv": list(argv)})
    return proc


# --- pure argv builders ------------------------------------------------------

def build_probe_argv(binary: str, path: str) -> list[str]:
    return [binary, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path]


def build_frame_argv(binary: str, video: str, at: float, out_png: str) -> list[str]:
    """Extract a single frame at ``at`` seconds (frame-accurate: -ss before -i)."""
    return [binary, "-y", "-ss", f"{float(at):.3f}", "-i", video,
            "-frames:v", "1", out_png]


def build_cut_argv(binary: str, src: str, start: float,
                   duration: float | None, out: str) -> list[str]:
    """截段/subclip with exact cut (re-encode libx264). duration=None → to end."""
    argv = [binary, "-y", "-ss", f"{float(start):.3f}", "-i", src]
    if duration is not None:
        argv += ["-t", f"{float(duration):.3f}"]
    argv += ["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", out]
    return argv


def build_concat_demuxer_argv(binary: str, list_file: str, out: str) -> list[str]:
    """拼合/concat fast path (stream copy, no re-encode)."""
    return [binary, "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", out]


def build_concat_filter_argv(binary: str, inputs: Sequence[str], out: str) -> list[str]:
    """concat fallback (re-encode; handles heterogeneous encodings)."""
    argv = [binary, "-y"]
    for path in inputs:
        argv += ["-i", path]
    n = len(inputs)
    streams = "".join(f"[{i}:v][{i}:a]" for i in range(n))
    argv += ["-filter_complex", f"{streams}concat=n={n}:v=1:a=1[v][a]",
             "-map", "[v]", "-map", "[a]", out]
    return argv


# 标注框的配色。**这是唯一真相** —— 提示词里说「②青色矩形框」时,视频里那一框
# 就必须真的是青色。从前提示词写死「红色」而那个视频根本没被画出来过;真画出来之后,
# 「两处各写一遍颜色」是下一个必然的坑,所以编译器直接引用这张表。
# 第三列是界面色(时间线条 / 画布框):人看到的那一框、烧进视频的那一框、
# 提示词里说的那个色名,三者同一行 —— 分开写就是同一个坑再挖一遍。
ANNOTATION_COLORS: tuple[tuple[str, str, str], ...] = (
    ("red", "红色", "#ff3b30"), ("cyan", "青色", "#32d0d8"),
    ("yellow", "黄色", "#ffd60a"), ("magenta", "品红色", "#ff2d95"),
    ("lime", "亮绿色", "#32d74b"), ("orange", "橙色", "#ff9f0a"),
    ("blue", "蓝色", "#0a84ff"), ("white", "白色", "#f2f2f7"),
)

# User-selectable guide palette.  Keep this smaller than the legacy automatic
# annotation palette: the editor intentionally exposes only black/white/red.
GUIDE_COLORS: tuple[tuple[str, str, str], ...] = (
    ("black", "黑色", "#000000"),
    ("white", "白色", "#ffffff"),
    ("red", "红色", "#ff3b30"),
)


def annotation_paint(ann: Mapping[str, Any], fallback_index0: int = 0) -> tuple[str, str, str]:
    """Resolve the guide's persisted UI color to ffmpeg/name/hex consistently."""
    raw = str(ann.get("color") or "").strip().lower()
    for paint in GUIDE_COLORS:
        if raw == paint[2]:
            return paint
    return ANNOTATION_COLORS[fallback_index0 % len(ANNOTATION_COLORS)]


def annotation_color(index0: int) -> tuple[str, str]:
    """第 ``index0`` 个标注的 (ffmpeg 颜色名, 中文名)。超出调色板就循环。"""
    name, zh, _hex = ANNOTATION_COLORS[index0 % len(ANNOTATION_COLORS)]
    return name, zh


def annotation_hex(index0: int) -> str:
    """同一个标注在**界面上**的颜色(时间线上那一条 / 画布上那个框)。"""
    return ANNOTATION_COLORS[index0 % len(ANNOTATION_COLORS)][2]


def color_slot(ann: Mapping[str, Any], fallback_index0: int = 0) -> int:
    """这条标注该用色表第几档 —— **锚在它自己的编号上,不是本次发送里的位序**。

    位序会随过滤漂移:选段排除了 ①、或者 ① 关了眼睛,剩下的 ②③ 位序就变成 0、1,
    于是烧出来的框色、提示词里的色名、界面上那一条的颜色三者当场错开 ——
    人看着青条,视频里那框是红的,模型按「红色框」去找。编号不随过滤变,所以锚它。
    """
    n = ann.get("number")
    if isinstance(n, (int, float)) and int(n) >= 1:
        return int(n) - 1
    return fallback_index0


def build_black_filler_argv(binary: str, out: str, duration: float, *,
                            width: int, height: int, fps: float = 30.0) -> list[str]:
    """一段黑帧,用来把时间线上的**空档**填平。

    为什么需要它:``compose_plan`` 会如实报出 gaps(那一段没有任何可见图层),而拼接
    只是把几刀首尾接起来 —— 洞被压缩掉,拼出来的段比时间线上那一段短。烧标注用的是
    「减掉这一段起点」的线性换算,于是洞之后的每个标注窗口整体偏移,框在错的时刻出现。
    填平之后「发出去的这一段」与「时间线上的这一段」等长,换算才成立。
    """
    return [binary, "-y", "-f", "lavfi", "-i",
            f"color=c=black:s={int(width)}x{int(height)}:r={float(fps):g}:d={max(0.04, float(duration)):.3f}",
            "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t", f"{max(0.04, float(duration)):.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", out]


def build_normalize_argv(binary: str, src: str, out: str, *,
                         width: int, height: int, fps: float = 30.0,
                         clip: Mapping[str, Any] | None = None) -> list[str]:
    """把一段按**它自己的几何**摆进画布,产出画布规格的一段。

    两个来源不同的片段(1280×720@24 与 1920×1080@30)直接 concat 会挂 ——
    demuxer 那条路要求编码参数一致,filter 那条路分辨率不同也接不起来。
    统一到画布规格再拼,顺带保证标注的占比换算落在同一个坐标系里(INV-5)。

    ``clip`` 给了就按它的 fitMode/x/y/scale/crop 摆;不给等价于默认 contain
    居中留边 —— 也就是升级前的行为,老项目画面不变。
    """
    graph = clip_geometry.build_filtergraph(clip or {}, width=int(width),
                                            height=int(height), fps=fps,
                                            in_label="0:v", out_label="vout")
    return [binary, "-y", "-i", src, "-filter_complex", graph,
            "-map", "[vout]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "44100", "-ac", "2", out]


def black_filler(out: str, duration: float, *, width: int, height: int,
                 fps: float = 30.0) -> str:
    binary = require_tool("ffmpeg")
    _run(build_black_filler_argv(binary, out, duration, width=width, height=height, fps=fps),
         what="ffmpeg black filler")
    return out


def normalize_segment(src: str, out: str, *, width: int, height: int,
                      fps: float = 30.0, clip: Mapping[str, Any] | None = None) -> str:
    binary = require_tool("ffmpeg")
    _run(build_normalize_argv(binary, src, out, width=width, height=height,
                              fps=fps, clip=clip),
         what="ffmpeg normalize")
    return out


def build_burn_annotations_argv(binary: str, src: str, out: str,
                                annotations: Sequence[Mapping[str, Any]], *,
                                thickness: int = 6,
                                time_offset: float = 0.0,
                                fps: float = 30.0,
                                canvas_w: float = 0.0,
                                canvas_h: float = 0.0) -> list[str]:
    """把标注框烧进视频,产出模式一要发的那个「AI 面」。

    这一步从前**根本不存在** —— 提示词里描述着「一段带临时编辑标注的视频」,
    而那个视频从没被制作出来,派发时发的是零文件。

    每个标注一条 drawbox,颜色按 :func:`annotation_paint` 取(与界面和提示词同源);
    有时间区间的用 ``enable='between(t,..)'`` 只在那一段出现。
    ``time_offset`` 是截段的起点 —— 标注区间是**素材坐标系**的时间,
    烧到截好的段上要减掉它。
    """
    filters: list[str] = []
    for idx, ann in enumerate(sorted(annotations, key=lambda a: a.get("number", 0))):
        bbox = ann.get("bbox") or {}
        x, y = float(bbox.get("x", 0)), float(bbox.get("y", 0))
        w, h = float(bbox.get("w", 0)), float(bbox.get("h", 0))
        if w <= 0 or h <= 0:
            continue
        color = annotation_paint(ann, color_slot(ann, idx))[0]
        # bbox 存的是**画布坐标系**的像素,而这里要画在合成出来的那一段上 ——
        # 两者尺寸可以不一样(项目 1920×1080、素材 1280×720 是常态)。
        # 所以换成画面占比,用 ffmpeg 的 iw/ih 表达式让它自己按真实分辨率落位;
        # 直接写像素的话框会整块跑到画面外,而 drawbox 不会报错,人只看到「AI 没听懂」。
        if canvas_w > 0 and canvas_h > 0:
            geom = (f"x=iw*{x / canvas_w:.6f}:y=ih*{y / canvas_h:.6f}"
                    f":w=iw*{w / canvas_w:.6f}:h=ih*{h / canvas_h:.6f}")
        else:
            geom = f"x={int(x)}:y={int(y)}:w={int(w)}:h={int(h)}"
        base = f"drawbox={geom}:color={color}@1.0:t={thickness}"
        # 这条标注在这一段里**露出来**的那些小窗口。上面压着的视频层会盖住它 ——
        # 画面上看不见的时间,视频里也不该画这个框(「看见什么就发什么」)。
        # 调用方给了 windows 就按它;没给就退回 interval(单条连续窗口)。
        windows = ann.get("windows")
        if not (isinstance(windows, (list, tuple)) and windows):
            interval = ann.get("interval")
            windows = [interval] if (isinstance(interval, (list, tuple))
                                     and len(interval) == 2) else []
        if not windows:
            filters.append(base)          # 没有时间信息 → 整段都画
            continue
        for win in windows:
            t0 = max(0.0, float(win[0]) - time_offset)
            t1 = max(t0, float(win[1]) - time_offset)
            # 单帧(t1==t0)撑成一帧宽,而不是不加 enable —— 不加就是整段常亮:
            # 界面上那个框只在一瞬出现、视频里却自始至终都在,模型会以为整段都要改。
            if t1 <= t0:
                t1 = t0 + 1.0 / max(1.0, float(fps))
            filters.append(f"{base}:enable='between(t,{t0:.3f},{t1:.3f})'")
    if not filters:
        raise ValueError("没有可烧录的标注(缺 bbox 宽高)")
    return [binary, "-y", "-i", src, "-vf", ",".join(filters),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "copy", out]


def burn_annotations(src: str, out: str, annotations: Sequence[Mapping[str, Any]], *,
                     thickness: int = 6, time_offset: float = 0.0,
                     fps: float = 30.0,
                     canvas_w: float = 0.0, canvas_h: float = 0.0) -> str:
    """执行烧录;缺 ffmpeg → RequiresRuntime(由 require_tool 抛)。"""
    binary = require_tool("ffmpeg")
    _run(build_burn_annotations_argv(binary, src, out, annotations,
                                     thickness=thickness, time_offset=time_offset, fps=fps,
                                     canvas_w=canvas_w, canvas_h=canvas_h),
         what="ffmpeg burn annotations")
    return out


def build_feather_mask_argv(binary: str, width: int, height: int,
                            bbox: dict, feather: float, out_png: str) -> list[str]:
    """Build a white-on-black feathered alpha mask (white=candidate region).

    ``bbox`` = {x,y,w,h}; ``feather`` = gaussian sigma in px. The mask is a full
    frame; the feathered white rect marks where the candidate replaces the
    original, everything else stays original.
    """
    x, y = int(bbox["x"]), int(bbox["y"])
    w, h = int(bbox["w"]), int(bbox["h"])
    vf = (f"drawbox=x={x}:y={y}:w={w}:h={h}:color=white@1.0:t=fill,"
          f"gblur=sigma={float(feather):.2f}")
    return [binary, "-y", "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}",
            "-vf", vf, "-frames:v", "1", out_png]


def build_mask_merge_argv(binary: str, original: str, candidate: str,
                          mask_png: str, out: str) -> list[str]:
    """时空蒙版合成 / spatiotemporal mask merge (framing-preserving).

    Composites ``candidate`` over ``original`` using ``mask_png`` as alpha:
    inside the feathered mask = candidate pixels, outside = original pixels
    byte-for-byte. Overlay is at native geometry (0:0, no scale/recenter) so the
    source framing is untouched — the root-cause fix for "邻格8误改". Audio is
    taken from the original (mask-merge audio rule).
    """
    return [binary, "-y", "-i", original, "-i", candidate, "-loop", "1", "-i", mask_png,
            "-filter_complex",
            "[1:v][2:v]alphamerge[ca];[0:v][ca]overlay=0:0:format=auto[v]",
            "-map", "[v]", "-map", "0:a?", "-c:a", "copy", "-shortest", out]


# --- executors (degrade to RequiresRuntime) ----------------------------------

def build_probe_media_argv(binary: str, path: str) -> list[str]:
    """一次问清:宽、高、帧率、时长。导入素材时用 —— 让人手填规格是错误之源。"""
    return [binary, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate:format=duration",
            "-of", "json", path]


def probe_media(path: str) -> dict[str, float]:
    """素材的真实规格 ``{width, height, fps, duration}``;读不出来的项为 0。

    读不出不抛 —— 导入一个怪文件不该阻断建项目,填 0 让上层照旧当占位处理。
    """
    import json as _json

    binary = require_tool("ffprobe")
    proc = _run(build_probe_media_argv(binary, path), what="ffprobe media")
    try:
        d = _json.loads(proc.stdout or "{}")
    except ValueError:
        return {"width": 0.0, "height": 0.0, "fps": 0.0, "duration": 0.0}
    streams = d.get("streams") or [{}]
    v = streams[0] if streams else {}
    rate = str(v.get("r_frame_rate") or "0/1")
    try:
        num, _, den = rate.partition("/")
        fps = float(num) / float(den or 1)
    except (TypeError, ValueError, ZeroDivisionError):
        fps = 0.0
    try:
        dur = float((d.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    return {"width": float(v.get("width") or 0), "height": float(v.get("height") or 0),
            "fps": round(fps, 3), "duration": round(dur, 3)}


def probe_duration(path: str) -> float:
    binary = require_tool("ffprobe")
    proc = _run(build_probe_argv(binary, path), what="ffprobe duration")
    try:
        return float((proc.stdout or "0").strip())
    except (TypeError, ValueError):
        return 0.0


def extract_frame_png(video: str, at: float) -> bytes:
    binary = require_tool("ffmpeg")
    fd, tmp = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        _run(build_frame_argv(binary, video, at, tmp), what="ffmpeg extract frame")
        with open(tmp, "rb") as fh:
            return fh.read()
    finally:
        _cleanup(tmp)


def cut_segment(src: str, out: str, start: float, duration: float | None) -> str:
    binary = require_tool("ffmpeg")
    _run(build_cut_argv(binary, src, start, duration, out), what="ffmpeg cut")
    return out


def concat_videos(inputs: Sequence[str], out: str) -> str:
    """Try stream-copy demuxer, fall back to filter re-encode on failure."""
    binary = require_tool("ffmpeg")
    fd, list_file = tempfile.mkstemp(suffix=".concat.txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for path in inputs:
                escaped = str(path).replace("'", "'\\''")
                fh.write(f"file '{escaped}'\n")
        try:
            _run(build_concat_demuxer_argv(binary, list_file, out), what="ffmpeg concat copy")
        except DomainError:
            _run(build_concat_filter_argv(binary, inputs, out), what="ffmpeg concat filter")
        return out
    finally:
        _cleanup(list_file)


def mask_merge(original: str, candidate: str, out: str, *, width: int, height: int,
               bbox: dict, feather: float = 6.0) -> str:
    """Feather-bbox composite candidate over original; outside-bbox = original."""
    binary = require_tool("ffmpeg")
    fd, mask_png = tempfile.mkstemp(suffix=".mask.png")
    os.close(fd)
    try:
        _run(build_feather_mask_argv(binary, width, height, bbox, feather, mask_png),
             what="ffmpeg mask build")
        _run(build_mask_merge_argv(binary, original, candidate, mask_png, out),
             what="ffmpeg mask merge")
        return out
    finally:
        _cleanup(mask_png)


def _cleanup(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
