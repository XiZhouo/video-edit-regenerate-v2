"""片段几何:一层素材怎么摆进画布。

重点不是"滤镜字符串长什么样",而是两条契约:
  ① 老数据(没有任何几何字段)渲染结果与升级前一致 —— 升级不改画面
  ② canvas_rect_of(界面画手柄用)与 build_filtergraph(实际渲染用)必须同一套算式
     —— 不然人拖的框和渲染出来的位置对不上
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from ze_video_domain import clip_geometry as cg


def _probe(path: str) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
         "stream=width,height", "-of", "csv=p=0", path],
        capture_output=True, text=True).stdout.strip()
    w, h = out.split(",")[:2]
    return int(w), int(h)


def _render(graph: str, src_w: int, src_h: int) -> tuple[int, int]:
    """真跑一遍 ffmpeg,回报输出尺寸 —— 滤镜串是不是合法只有 ffmpeg 说了算。"""
    d = tempfile.mkdtemp()
    src, out = f"{d}/s.mp4", f"{d}/o.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", f"color=c=red:s={src_w}x{src_h}:r=24:d=0.3",
                    "-c:v", "libx264", src], check=True)
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src,
                        "-filter_complex", graph, "-map", "[v]",
                        "-c:v", "libx264", out], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-600:]
    return _probe(out)


# ── ① 默认 = 老行为 ──────────────────────────────────────────────

def test_a_clip_with_no_geometry_fields_is_identity():
    assert cg.is_identity({}) is True
    assert cg.is_identity({"start": 0, "duration": 4}) is True


def test_identity_chain_still_outputs_canvas_size():
    graph = cg.build_filtergraph({}, width=1920, height=1080, fps=30)
    assert "force_original_aspect_ratio=decrease" in graph     # 等比装进去 = 老的 contain
    assert "color=c=black:s=1920x1080" in graph                # 画布底
    assert "overlay=" in graph and "fps=30" in graph and "setsar=1" in graph


def test_unknown_fit_mode_falls_back_to_contain_not_crash():
    g = cg.normalize_geometry({"fitMode": "乱写的"})
    assert g["fitMode"] == "contain"


# ── ② 真跑 ffmpeg:每种 fitMode 都输出画布规格 ──────────────────

@pytest.mark.parametrize("fit", ["contain", "cover", "stretch", "none"])
def test_every_fit_mode_really_renders_to_canvas_size(fit):
    chain = cg.build_filtergraph({"fitMode": fit}, width=640, height=360, fps=24)
    assert _render(chain, 1280, 720) == (640, 360)


def test_crop_and_scale_and_offset_still_render_to_canvas_size():
    chain = cg.build_filtergraph(
        {"fitMode": "cover", "cropL": 0.1, "cropR": 0.1, "cropT": 0.05, "cropB": 0.05,
         "scale": 1.4, "x": 0.08, "y": -0.05},
        width=360, height=640, fps=24)
    assert _render(chain, 1280, 720) == (360, 640)


def test_absurd_crop_is_clamped_instead_of_producing_a_zero_size_frame():
    """两边各裁 80% 会把画面裁没 —— 夹住而不是让 ffmpeg 炸。"""
    chain = cg.build_filtergraph({"cropL": 0.8, "cropR": 0.8},
                                  width=640, height=360, fps=24)
    assert _render(chain, 1280, 720) == (640, 360)


# ── ③ 手柄矩形与渲染同源 ────────────────────────────────────────

def test_contain_rect_matches_letterbox_geometry():
    """1280×720 装进 1080×1920 竖画布:等比缩到宽满,上下留边,居中。"""
    r = cg.canvas_rect_of({}, width=1080, height=1920, src_w=1280, src_h=720)
    assert round(r["width"]) == 1080
    assert round(r["height"]) == 608          # 1080 * 720/1280
    assert round(r["x"]) == 0
    assert round(r["y"]) == round((1920 - 607.5) / 2)


def test_cover_rect_overflows_the_canvas_on_the_long_axis():
    r = cg.canvas_rect_of({"fitMode": "cover"}, width=1080, height=1920,
                          src_w=1280, src_h=720)
    assert round(r["height"]) == 1920
    assert r["width"] > 1080                  # 左右溢出,会被裁掉
    assert r["x"] < 0


def test_offset_moves_the_rect_by_a_fraction_of_the_canvas():
    base = cg.canvas_rect_of({}, width=1000, height=1000, src_w=1000, src_h=1000)
    moved = cg.canvas_rect_of({"x": 0.1, "y": -0.2}, width=1000, height=1000,
                              src_w=1000, src_h=1000)
    assert round(moved["x"] - base["x"]) == 100
    assert round(moved["y"] - base["y"]) == -200


def test_scale_grows_the_rect_about_the_centre():
    base = cg.canvas_rect_of({}, width=800, height=800, src_w=800, src_h=800)
    big = cg.canvas_rect_of({"scale": 2.0}, width=800, height=800, src_w=800, src_h=800)
    assert round(big["width"]) == round(base["width"] * 2)
    assert round(big["x"] + big["width"] / 2) == round(base["x"] + base["width"] / 2)


def test_crop_shrinks_the_source_before_fitting():
    """左右各裁 25% → 源变成 640×720(更竖),装进方画布后应比未裁时窄。"""
    plain = cg.canvas_rect_of({}, width=720, height=720, src_w=1280, src_h=720)
    cropped = cg.canvas_rect_of({"cropL": 0.25, "cropR": 0.25},
                                width=720, height=720, src_w=1280, src_h=720)
    assert cropped["width"] < plain["width"]
    assert round(cropped["height"]) == 720     # 裁后变竖,高度顶满


# ── ④ 交叉验证:界面手柄的矩形 == ffmpeg 真渲染出来的位置 ──────────
#
# 这是 INV-5 的地基。两者算式分开写了两份,一旦漂开,人拖的框和实际改的地方就错位,
# 而这种错位在界面上完全看不出来(框在画布上,画面也在画布上,只是对不齐)。

def _rendered_rect(chain: str, src_w: int, src_h: int,
                   canvas_w: int, canvas_h: int) -> dict:
    """渲染一帧,量出非黑像素的包围盒 —— 素材真正落在画布哪儿。"""
    d = tempfile.mkdtemp()
    src, png = f"{d}/s.mp4", f"{d}/o.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", f"color=c=red:s={src_w}x{src_h}:r=24:d=0.3",
                    "-c:v", "libx264", src], check=True)
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src,
                        "-filter_complex", chain, "-map", "[v]",
                        "-frames:v", "1", png], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-500:]
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", png, "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    xs, ys = [], []
    for y in range(canvas_h):
        for x in range(canvas_w):
            i = (y * canvas_w + x) * 3
            if raw[i] > 60:                      # 红 vs 黑边
                xs.append(x); ys.append(y)
    assert xs, "整幅都是黑的?滤镜把画面弄没了"
    return {"x": min(xs), "y": min(ys),
            "width": max(xs) - min(xs) + 1, "height": max(ys) - min(ys) + 1}


@pytest.mark.parametrize("geom", [
    {},                                              # 默认 contain
    {"fitMode": "cover"},
    {"fitMode": "contain", "scale": 0.5},
    {"fitMode": "contain", "x": 0.15},
    {"fitMode": "contain", "y": -0.1},
    {"fitMode": "contain", "cropL": 0.2, "cropR": 0.2},
])
def test_handle_rect_matches_what_ffmpeg_actually_renders(geom):
    CW, CH, SW, SH = 320, 240, 640, 360
    chain = cg.build_filtergraph(geom, width=CW, height=CH, fps=24)
    want = cg.canvas_rect_of(geom, width=CW, height=CH, src_w=SW, src_h=SH)
    got = _rendered_rect(chain, SW, SH, CW, CH)
    # 期望矩形超出画布的部分渲染时被裁掉,所以先把期望也夹进画布再比
    ex0, ey0 = max(0, want["x"]), max(0, want["y"])
    ex1 = min(CW, want["x"] + want["width"])
    ey1 = min(CH, want["y"] + want["height"])
    for label, a, b in (("x", ex0, got["x"]), ("y", ey0, got["y"]),
                        ("w", ex1 - ex0, got["width"]), ("h", ey1 - ey0, got["height"])):
        assert abs(a - b) <= 2, f"{label}: 手柄说 {a:.1f},实渲 {b} —— {geom}"
