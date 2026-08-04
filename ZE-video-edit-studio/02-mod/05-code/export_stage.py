"""export_stage — 「把这一段导出来」的慢 stage。

精修完拿不出去等于没修。这一步把整条时间线(或指定区间)按画布规格合成、编码,
落成一个 asset,人可以下载、也可以交给 ③ 继续装配。

与发送链**共用同一个域层渲染器**(``render.compose_span``),唯一的差别是:
导出**绝不烧标注**(规划 INV-2 —— 标注只属于 AI 面,成品里不该有框)。

纪律与发送链一致:缺 ffmpeg/AssetStore → ``requires_runtime``;素材/几何有问题 →
``failed`` 并说清是哪一层。四条出口都写 artifact 收据(不写框架判「需要修复」)。
"""
from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

from _domain import ffmpeg_ops, render
from _domain.errors import DomainError, RequiresRuntime

RUNTIME_HINT = "需要本地 ffmpeg 合成与编码;确认 ffmpeg 可用"


def _brief(text: str, limit: int = 280) -> str:
    t = " ".join((text or "").split())
    return t[:limit] + ("…" if len(t) > limit else "")


def _write_artifact(args: dict[str, Any], text: str) -> None:
    """产物收据 —— 不写的话框架完工核对会判 artifact_output_missing。"""
    artifacts = args.get("_artifacts") or ()
    if not artifacts:
        return
    path = Path(artifacts[0])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _requires_runtime(args: dict[str, Any], msg: str) -> dict[str, Any]:
    _write_artifact(args, f"requires_runtime: {_brief(msg)}\n")
    return {"patches": {"status": "requires_runtime",
                        "lastError": f"{_brief(msg)} — {RUNTIME_HINT}"}}


def _failed(args: dict[str, Any], msg: str) -> dict[str, Any]:
    _write_artifact(args, f"failed: {_brief(msg)}\n")
    return {"patches": {"status": "failed", "lastError": _brief(msg) or "导出失败"}}


def run_export_stage(ctx, args: dict[str, Any], *, asset_store=None) -> dict[str, Any]:
    """合成整条时间线 → 编码 → 落成 asset。"""
    from sdk import AssetRef

    if asset_store is None or ffmpeg_ops.ffmpeg_bin() is None:
        return _requires_runtime(args, "缺 ffmpeg 或 AssetStore")

    clips = args.get("clips") or []
    canvas = args.get("canvas") or [0, 0]
    fps = float(args.get("fps") or 30.0)
    preset = str(args.get("preset") or render.DEFAULT_PRESET)

    with tempfile.TemporaryDirectory(prefix="ze-ves-export-") as work:
        ctx.log("info", "合成整条时间线", progress_scope="indeterminate")
        try:
            result = render.render_timeline(
                clips, work,
                fetch_bytes=lambda ref: asset_store.get(AssetRef.from_dict(ref)),
                canvas=(int(canvas[0] or 0), int(canvas[1] or 0)), fps=fps,
                start=args.get("start"), end=args.get("end"), preset=preset)
        except RequiresRuntime as exc:
            return _requires_runtime(args, str(exc))
        except (DomainError, OSError, KeyError, ValueError) as exc:
            return _failed(args, str(exc))

        ctx.log("info", "落库", progress_scope="indeterminate")
        data = Path(result["path"]).read_bytes()

    mime = "video/webm" if result["ext"] == ".webm" else "video/mp4"
    file_ref = asset_store.put(data, mime=mime, ext=result["ext"]).to_dict()
    _write_artifact(args, "\n".join([
        f"range: {result['start']}–{result['end']}s ({result['duration']}s)",
        f"canvas: {result['canvas'][0]}x{result['canvas'][1]} @{result['fps']:g}fps",
        f"preset: {result['preset']}",
        f"bytes: {len(data)}",
        f"sha256: {file_ref.get('sha256', '')}",
        "",
    ]))
    return {
        "patches": {
            "status": "succeeded", "lastError": "",
            "fileRef": file_ref, "preset": result["preset"],
            "width": result["canvas"][0], "height": result["canvas"][1],
            "fps": result["fps"], "duration": result["duration"],
        },
    }
