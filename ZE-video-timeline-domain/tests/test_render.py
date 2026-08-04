"""导出:真跑 ffmpeg,把两段不同规格的素材合成为一个画布规格的成品。

这是「精修完拿得出去」的证明。断言全部落在**产物文件本身**(ffprobe 读出来的
宽高/时长/有没有音轨),不是断言我拼的命令行长什么样 —— 命令行对不对只有 ffmpeg 说了算。
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from ze_video_domain import render
from ze_video_domain.errors import DomainError


def _make(path: str, w: int, h: int, dur: float, color: str, fps: int = 24) -> str:
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}:r={fps}:d={dur}",
                    "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                    "-t", str(dur), "-c:v", "libx264", "-c:a", "aac", "-shortest",
                    path], check=True)
    return path


def _probe(path: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format",
         "-of", "json", path], capture_output=True, text=True).stdout
    d = json.loads(out)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    return {"w": int(v["width"]), "h": int(v["height"]),
            "duration": float(d["format"]["duration"]), "has_audio": a is not None,
            "vcodec": v["codec_name"]}


@pytest.fixture()
def bed():
    """两段不同规格的素材 + 取字节的回调。"""
    d = tempfile.mkdtemp()
    a = _make(f"{d}/a.mp4", 1280, 720, 2.0, "red", fps=24)     # 横
    b = _make(f"{d}/b.mp4", 720, 1280, 2.0, "blue", fps=30)    # 竖
    blobs = {"AAA": Path(a).read_bytes(), "BBB": Path(b).read_bytes()}
    return {"dir": d, "fetch": lambda ref: blobs[ref["sha256"]]}


def _clip(cid, sha, layer, start, dur, **geom):
    return {"id": cid, "layer": layer, "visible": True, "start": start,
            "duration": dur, "sourceIn": 0.0,
            "videoRef": {"sha256": sha}, **geom}


def test_two_differently_shaped_sources_export_as_one_vertical_file(bed):
    """1280×720 与 720×1280 两段接起来 → 一个 1080×1920 的成品,带音轨。"""
    clips = [_clip("A", "AAA", 0, 0.0, 2.0),
             _clip("B", "BBB", 0, 2.0, 2.0, fitMode="cover")]
    res = render.render_timeline(clips, bed["dir"], fetch_bytes=bed["fetch"],
                                 canvas=(1080, 1920), fps=30)
    p = _probe(res["path"])
    assert (p["w"], p["h"]) == (1080, 1920)
    assert abs(p["duration"] - 4.0) < 0.35, p
    assert p["has_audio"], "导出必须带音频"
    assert res["preset"] == "mp4_high" and res["ext"] == ".mp4"


def test_export_range_can_be_limited_to_part_of_the_timeline(bed):
    clips = [_clip("A", "AAA", 0, 0.0, 2.0), _clip("B", "BBB", 0, 2.0, 2.0)]
    res = render.render_timeline(clips, bed["dir"], fetch_bytes=bed["fetch"],
                                 canvas=(640, 360), fps=24, start=1.0, end=3.0)
    p = _probe(res["path"])
    assert abs(p["duration"] - 2.0) < 0.35, p
    assert (p["w"], p["h"]) == (640, 360)


def test_a_gap_in_the_timeline_becomes_black_and_keeps_total_length(bed):
    """0–2s 有片段、2–3s 空、3–5s 有片段 → 成品 5 秒,不是 4 秒。"""
    clips = [_clip("A", "AAA", 0, 0.0, 2.0), _clip("B", "BBB", 0, 3.0, 2.0)]
    res = render.render_timeline(clips, bed["dir"], fetch_bytes=bed["fetch"],
                                 canvas=(640, 360), fps=24)
    assert abs(_probe(res["path"])["duration"] - 5.0) < 0.4


def test_hidden_layer_is_not_exported(bed):
    """关掉眼睛的层不参与导出(INV-2:看见什么导出什么)。"""
    clips = [_clip("A", "AAA", 0, 0.0, 2.0),
             {**_clip("B", "BBB", 5, 0.0, 2.0), "visible": False}]
    res = render.render_timeline(clips, bed["dir"], fetch_bytes=bed["fetch"],
                                 canvas=(640, 360), fps=24)
    assert abs(_probe(res["path"])["duration"] - 2.0) < 0.35


def test_upper_layer_wins_where_they_overlap(bed):
    """两层重叠处导出的是上面那层的画面(红在下、蓝在上 → 中间应是蓝)。"""
    clips = [_clip("A", "AAA", 0, 0.0, 2.0),
             _clip("B", "BBB", 1, 0.5, 1.0, fitMode="stretch")]
    res = render.render_timeline(clips, bed["dir"], fetch_bytes=bed["fetch"],
                                 canvas=(320, 240), fps=24)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", "1.0", "-i", res["path"],
                          "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True).stdout
    r, g, b = raw[0], raw[1], raw[2]
    assert b > r, f"1.0s 处应是上层的蓝,实测 rgb=({r},{g},{b})"


def test_a_layer_longer_than_its_source_says_so_instead_of_failing_obscurely():
    """图层声称 3s 但素材只有 2s —— 从前会截出没有视频轨的碎片,在 concat 处以
    完全无关的报错炸掉。现在直接说清是哪一层、差多少。"""
    import tempfile as _tf
    d = _tf.mkdtemp()
    a = _make(f"{d}/a.mp4", 640, 360, 2.0, "red")
    blobs = {"AAA": Path(a).read_bytes()}
    clips = [_clip("A", "AAA", 0, 0.0, 4.0)]
    with pytest.raises(DomainError) as exc:
        render.render_timeline(clips, d, fetch_bytes=lambda r: blobs[r["sha256"]],
                               canvas=(640, 360), fps=24)
    msg = str(exc.value)
    assert "图层" in msg and "素材" in msg and "改短" in msg, msg


def test_webm_preset_really_produces_vp9(bed):
    clips = [_clip("A", "AAA", 0, 0.0, 1.0)]
    res = render.render_timeline(clips, bed["dir"], fetch_bytes=bed["fetch"],
                                 canvas=(320, 240), fps=24, preset="webm")
    assert res["ext"] == ".webm"
    assert _probe(res["path"])["vcodec"] == "vp9"


def test_nothing_visible_refuses_with_a_reason(bed):
    clips = [{**_clip("A", "AAA", 0, 0.0, 2.0), "visible": False}]
    with pytest.raises(DomainError) as exc:
        render.render_timeline(clips, bed["dir"], fetch_bytes=bed["fetch"],
                               canvas=(640, 360), fps=24)
    assert "没有可见" in str(exc.value)


def test_missing_canvas_size_refuses_instead_of_guessing(bed):
    clips = [_clip("A", "AAA", 0, 0.0, 2.0)]
    with pytest.raises(DomainError) as exc:
        render.render_timeline(clips, bed["dir"], fetch_bytes=bed["fetch"],
                               canvas=(0, 0), fps=24)
    assert "画布尺寸" in str(exc.value)


# ── 素材探测:导入时把真实规格读出来,而不是让人手填 ──────────────

def test_probe_media_reads_real_spec():
    from ze_video_domain import ffmpeg_ops
    d = tempfile.mkdtemp()
    p = _make(f"{d}/probe.mp4", 1920, 1080, 1.5, "green", fps=30)
    got = ffmpeg_ops.probe_media(p)
    assert (got["width"], got["height"]) == (1920.0, 1080.0)
    assert abs(got["fps"] - 30) < 0.01
    assert abs(got["duration"] - 1.5) < 0.2


def test_probe_media_on_a_broken_file_returns_zeros_instead_of_raising():
    """导入一个怪文件不该阻断建项目 —— 填 0,上层照旧当占位处理。"""
    from ze_video_domain import ffmpeg_ops
    from ze_video_domain.errors import DomainError
    d = tempfile.mkdtemp()
    bad = Path(d) / "bad.mp4"
    bad.write_bytes(b"not a video at all")
    with pytest.raises(DomainError):        # ffprobe 明确失败 → 结构化抛,不静默
        ffmpeg_ops.probe_media(str(bad))
