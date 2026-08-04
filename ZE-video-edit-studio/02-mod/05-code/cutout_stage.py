"""图片素材自动去背景 stage。失败时保留原图，用户仍可继续编辑。"""

from __future__ import annotations

from io import BytesIO
from typing import Any


def _source_bytes(asset_store, ref: dict) -> bytes:
    try:
        return asset_store.get(ref["sha256"])
    except Exception:
        from sdk import AssetRef
        return asset_store.get(AssetRef.from_dict(ref))


def _has_useful_alpha(img) -> bool:
    alpha = img.getchannel("A")
    low, high = alpha.getextrema()
    return low < 250 and high > 10 and low != high


def _fallback_knockout(img):
    px = img.load()
    width, height = img.size
    sx = min(2, max(0, width - 1))
    sy = min(2, max(0, height - 1))
    samples = [
        px[sx, sy][:3],
        px[max(0, width - 1 - sx), sy][:3],
        px[sx, max(0, height - 1 - sy)][:3],
        px[max(0, width - 1 - sx), max(0, height - 1 - sy)][:3],
    ]
    background = tuple(sum(c[i] for c in samples) // len(samples) for i in range(3))
    out = img.copy()
    target = out.load()
    for y in range(height):
        for x in range(width):
            r, g, b, a = target[x, y]
            distance = abs(r - background[0]) + abs(g - background[1]) + abs(b - background[2])
            if distance < 114:
                target[x, y] = (r, g, b, 0)
    return out


def run_cutout_stage(ctx, args: dict[str, Any], *, asset_store=None) -> dict[str, Any]:
    if asset_store is None:
        return {"patches": {"cutoutStatus": "requires_runtime",
                            "cutoutError": "AssetStore 未注入，已保留原图"}}
    ref = args.get("asset_ref")
    if not isinstance(ref, dict) or not ref.get("sha256"):
        return {"patches": {"cutoutStatus": "failed", "cutoutError": "图片素材缺少文件"}}
    try:
        from PIL import Image
    except ImportError:
        return {"patches": {"cutoutStatus": "requires_runtime",
                            "cutoutError": "缺少 Pillow，已保留原图"}}

    try:
        raw = _source_bytes(asset_store, ref)
        image = Image.open(BytesIO(raw)).convert("RGBA")
        method = "passthrough_alpha"
        if not _has_useful_alpha(image):
            try:
                from rembg import remove
                image = Image.open(BytesIO(remove(raw))).convert("RGBA")
                method = "rembg"
            except Exception:
                image = _fallback_knockout(image)
                method = "corner_knockout"
        bbox = image.getbbox()
        if bbox:
            image = image.crop(bbox)
        buf = BytesIO()
        image.save(buf, "PNG")
        out_ref = asset_store.put(buf.getvalue(), mime="image/png", ext=".png").to_dict()
        ctx.log("info", f"图片去背景完成: {method}", progress_scope="indeterminate")
        return {
            "patches": {
                "processedRef": out_ref,
                "cutoutStatus": "succeeded",
                "cutoutMethod": method,
                "cutoutError": "",
                "width": float(image.width),
                "height": float(image.height),
            },
        }
    except Exception as exc:
        return {"patches": {"cutoutStatus": "failed",
                            "cutoutError": f"自动去背景失败，已保留原图: {exc}"}}
