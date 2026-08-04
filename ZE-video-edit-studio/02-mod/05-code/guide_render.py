"""把画面引导渲染成标记视频或修改画面。

图片、框选、文字、箭头都是生成输入，不进入最终导出。这里仅在 stage 中执行，
不碰 Document；素材读取通过注入的 AssetStore 完成。
"""

from __future__ import annotations

import json
import math
import os
from io import BytesIO
from typing import Any, Mapping, Sequence

from _domain import ffmpeg_ops
from _domain.errors import DomainError, RequiresRuntime
from guide_motion import resolve_guide_geometry


def _pillow():
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RequiresRuntime("缺少 Pillow，无法渲染图片、文字和路径引导") from exc
    return Image, ImageDraw, ImageFont


def _asset_bytes(asset_store, ref: Mapping[str, Any]) -> bytes:
    if asset_store is None:
        raise RequiresRuntime("AssetStore 未注入，无法读取图片素材")
    sha = str(ref.get("sha256") or "")
    if not sha:
        raise DomainError("图片素材缺少 asset_ref")
    try:
        return asset_store.get(sha)
    except Exception:
        try:
            from sdk import AssetRef
            return asset_store.get(AssetRef.from_dict(dict(ref)))
        except Exception as exc:
            raise DomainError("图片素材已不存在或无法读取") from exc


def _font(ImageFont, size: int):
    candidates = (
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for path in candidates:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size=max(12, size))
            except Exception:
                pass
    return ImageFont.load_default()


def _color(value: Any) -> tuple[int, int, int, int]:
    raw = str(value or "#ff3b30").lstrip("#")
    if len(raw) == 6:
        try:
            return tuple(int(raw[i:i + 2], 16) for i in (0, 2, 4)) + (255,)
        except ValueError:
            pass
    return (255, 59, 48, 255)


def _box(guide: Mapping[str, Any], width: int, height: int) -> tuple[int, int, int, int]:
    bbox = guide.get("bbox") or {}
    return (
        int(round(float(bbox.get("x") or 0))),
        int(round(float(bbox.get("y") or 0))),
        int(round(float(bbox.get("w") or 0))),
        int(round(float(bbox.get("h") or 0))),
    )


def _points(guide: Mapping[str, Any]) -> list[tuple[float, float]]:
    raw = guide.get("points")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = []
    out: list[tuple[float, float]] = []
    for point in raw or ():
        if isinstance(point, Mapping):
            out.append((float(point.get("x") or 0), float(point.get("y") or 0)))
        elif isinstance(point, (list, tuple)) and len(point) >= 2:
            out.append((float(point[0]), float(point[1])))
    return out


def _draw_arrow(draw, points: Sequence[tuple[float, float]], *,
                fill: tuple[int, int, int, int], thickness: int) -> None:
    if len(points) < 2:
        return
    draw.line(list(points), fill=fill, width=thickness, joint="curve")
    ax, ay = points[-2]
    bx, by = points[-1]
    angle = math.atan2(by - ay, bx - ax)
    size = max(12, thickness * 3)
    left = (bx - size * math.cos(angle - math.pi / 6),
            by - size * math.sin(angle - math.pi / 6))
    right = (bx - size * math.cos(angle + math.pi / 6),
             by - size * math.sin(angle + math.pi / 6))
    draw.polygon([(bx, by), left, right], fill=fill)


def render_overlay_png(guides: Sequence[Mapping[str, Any]], out_path: str, *,
                       asset_store, canvas: tuple[int, int]) -> str:
    Image, ImageDraw, ImageFont = _pillow()
    width, height = int(canvas[0]), int(canvas[1])
    if width <= 0 or height <= 0:
        raise DomainError("画布尺寸无效，无法渲染引导")
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    thickness = max(3, round(min(width, height) * 0.006))
    font = _font(ImageFont, round(min(width, height) * 0.035))

    for guide in sorted(guides, key=lambda item: float(item.get("layer") or 0)):
        kind = str(guide.get("kind") or "box")
        color = _color(guide.get("color"))
        x, y, w, h = _box(guide, width, height)
        label = str(guide.get("label") or "").strip()

        if kind == "image":
            ref = guide.get("image_ref")
            if isinstance(ref, Mapping) and ref.get("sha256") and w > 0 and h > 0:
                try:
                    material = Image.open(BytesIO(_asset_bytes(asset_store, ref))).convert("RGBA")
                    material.thumbnail((max(1, w), max(1, h)), Image.Resampling.LANCZOS)
                    px = x + (w - material.width) // 2
                    py = y + (h - material.height) // 2
                    overlay.alpha_composite(material, (px, py))
                except Exception as exc:
                    raise DomainError(f"图片素材渲染失败: {exc}") from exc
        elif kind == "arrow":
            _draw_arrow(draw, _points(guide), fill=color, thickness=thickness)
        elif kind == "text":
            if label:
                pad = max(6, thickness)
                bounds = draw.textbbox((x, y), label, font=font)
                tw, th = bounds[2] - bounds[0], bounds[3] - bounds[1]
                draw.rounded_rectangle(
                    (x - pad, y - pad, x + tw + pad, y + th + pad),
                    radius=pad, fill=(12, 17, 24, 220), outline=color, width=max(2, thickness // 2),
                )
                draw.text((x, y), label, font=font, fill=(255, 255, 255, 255))
        else:
            if w > 0 and h > 0:
                draw.rectangle((x, y, x + w, y + h), outline=color, width=thickness)

        # 框和图片旁边的说明文字。文字引导本身已经画过，不重复。
        if label and kind in {"box", "image", "arrow"}:
            tx = min(max(4, x + w + thickness), max(4, width - 240))
            ty = min(max(4, y), max(4, height - 48))
            pad = max(5, thickness // 2)
            bounds = draw.textbbox((tx, ty), label, font=font)
            tw, th = bounds[2] - bounds[0], bounds[3] - bounds[1]
            draw.rounded_rectangle(
                (tx - pad, ty - pad, min(width - 2, tx + tw + pad), ty + th + pad),
                radius=pad, fill=(12, 17, 24, 220), outline=color, width=max(2, thickness // 2),
            )
            draw.text((tx, ty), label, font=font, fill=(255, 255, 255, 255))

    overlay.save(out_path, "PNG")
    return out_path


def active_guides(guides: Sequence[Mapping[str, Any]], at: float, fps: float) -> list[Mapping[str, Any]]:
    eps = 0.5 / max(1.0, float(fps))
    out = []
    for guide in guides:
        windows = guide.get("windows")
        if not windows:
            interval = guide.get("interval")
            windows = [interval] if isinstance(interval, (list, tuple)) and len(interval) == 2 else []
        if not windows:
            active = dict(guide)
            frames = guide.get("keyframes") or ()
            if frames and str(guide.get("kind") or "box") in {"box", "image"}:
                origin = float(guide.get("keyframe_origin")
                               if guide.get("keyframe_origin") is not None
                               else (guide.get("interval") or [0])[0])
                active["bbox"] = resolve_guide_geometry(
                    guide.get("bbox") or {}, frames, at - origin)
            out.append(active)
            continue
        if any(float(win[0]) - eps <= at <= float(win[1]) + eps for win in windows):
            active = dict(guide)
            frames = guide.get("keyframes") or ()
            if frames and str(guide.get("kind") or "box") in {"box", "image"}:
                origin = float(guide.get("keyframe_origin")
                               if guide.get("keyframe_origin") is not None
                               else (guide.get("interval") or [0])[0])
                active["bbox"] = resolve_guide_geometry(
                    guide.get("bbox") or {}, frames, at - origin)
            out.append(active)
    return out


def burn_guides(src: str, out: str, guides: Sequence[Mapping[str, Any]], *,
                asset_store, work: str, canvas: tuple[int, int],
                time_offset: float, fps: float) -> str:
    """每条引导渲染为独立透明图，再按自身时间窗叠到视频。"""
    binary = ffmpeg_ops.require_tool("ffmpeg")
    has_motion = any(
        item.get("keyframes") and str(item.get("kind") or "box") in {"box", "image"}
        for item in guides
    )
    if has_motion:
        duration = ffmpeg_ops.probe_duration(src)
        if duration <= 0:
            raise DomainError("无法读取提交片段时长，不能渲染位置变化")
        frame_dir = os.path.join(work, "motion_overlays")
        os.makedirs(frame_dir, exist_ok=True)
        frame_count = max(1, int(math.ceil(duration * max(1.0, fps))))
        for index in range(frame_count):
            local_at = index / max(1.0, fps)
            render_overlay_png(
                active_guides(guides, time_offset + local_at, fps),
                os.path.join(frame_dir, f"overlay_{index:06d}.png"),
                asset_store=asset_store,
                canvas=canvas,
            )
        pattern = os.path.join(frame_dir, "overlay_%06d.png")
        argv = [
            binary, "-y", "-i", src,
            "-framerate", f"{max(1.0, fps):g}", "-start_number", "0", "-i", pattern,
            "-filter_complex", "[0:v][1:v]overlay=0:0:format=auto:shortest=1[v]",
            "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy", "-shortest", out,
        ]
        ffmpeg_ops._run(argv, what="ffmpeg burn moving guides")  # noqa: SLF001
        return out

    materialised: list[tuple[str, list[tuple[float, float]]]] = []
    for idx, guide in enumerate(guides):
        path = os.path.join(work, f"guide_{idx}.png")
        render_overlay_png([guide], path, asset_store=asset_store, canvas=canvas)
        windows = guide.get("windows")
        if not windows:
            interval = guide.get("interval")
            windows = [interval] if isinstance(interval, (list, tuple)) and len(interval) == 2 else []
        normalized = []
        for win in windows or ():
            t0 = max(0.0, float(win[0]) - time_offset)
            t1 = max(t0, float(win[1]) - time_offset)
            if t1 <= t0:
                t1 = t0 + 1.0 / max(1.0, fps)
            normalized.append((t0, t1))
        materialised.append((path, normalized))

    if not materialised:
        raise ValueError("没有可渲染的画面引导")

    argv = [binary, "-y", "-i", src]
    for path, _windows in materialised:
        argv += ["-loop", "1", "-i", path]
    chain = []
    previous = "[0:v]"
    for idx, (_path, windows) in enumerate(materialised, start=1):
        target = f"[v{idx}]"
        enable = ""
        if windows:
            expr = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in windows)
            enable = f":enable='{expr}'"
        chain.append(
            f"{previous}[{idx}:v]overlay=0:0:format=auto:shortest=1:eof_action=pass"
            f"{enable}{target}"
        )
        previous = target
    argv += [
        "-filter_complex", ";".join(chain),
        "-map", previous, "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy", "-shortest", out,
    ]
    ffmpeg_ops._run(argv, what="ffmpeg burn guides")  # noqa: SLF001
    return out


def render_modified_frame(src: str, out: str, *, at_local: float,
                          guides: Sequence[Mapping[str, Any]], global_at: float,
                          asset_store, work: str, canvas: tuple[int, int], fps: float) -> str:
    Image, _ImageDraw, _ImageFont = _pillow()
    binary = ffmpeg_ops.require_tool("ffmpeg")
    clean = os.path.join(work, f"clean_{global_at:.3f}.png")
    ffmpeg_ops._run(
        ffmpeg_ops.build_frame_argv(binary, src, max(0.0, at_local), clean),
        what="ffmpeg extract reference frame",
    )
    overlay_path = os.path.join(work, f"overlay_{global_at:.3f}.png")
    render_overlay_png(active_guides(guides, global_at, fps), overlay_path,
                       asset_store=asset_store, canvas=canvas)
    base = Image.open(clean).convert("RGBA")
    overlay = Image.open(overlay_path).convert("RGBA")
    if overlay.size != base.size:
        overlay = overlay.resize(base.size, Image.Resampling.LANCZOS)
    base.alpha_composite(overlay)
    base.convert("RGB").save(out, "PNG")
    return out
