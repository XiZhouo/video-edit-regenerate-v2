"""导出:真 ffmpeg、真素材、真落库 —— 规划 P1 硬验收的机器版。

断言全部落在**产物文件本身**(ffprobe 读出来的宽高/时长/音轨),
而不是断言我拼的参数长什么样。命令行对不对只有 ffmpeg 说了算。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "02-mod" / "05-code"))

from sdk import Actor, AssetRef, Entity, ZeError  # noqa: E402
import export_stage  # noqa: E402


@pytest.fixture()
def ctx():
    tmp = tempfile.mkdtemp(prefix="ze-ves-exp-")
    keys = ("ZE_DATA_DIR", "ZE_USERS_PATH", "ZE_SYSTEM_AUTH_DIR")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ["ZE_DATA_DIR"] = str(Path(tmp) / "data")
    os.environ["ZE_USERS_PATH"] = str(Path(tmp) / "users.json")
    os.environ["ZE_SYSTEM_AUTH_DIR"] = str(Path(tmp) / "auth")
    try:
        from sdk.bootstrap import bootstrap, SYSTEM_USER_ID
        repo = bootstrap()
        pid = repo.create_project(name="__exp__", owner=SYSTEM_USER_ID, seed=False)
        c = repo.get_or_load(pid)
        c._actor = Actor(user_id=SYSTEM_USER_ID, source="user")
        yield c
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _mkvideo(w: int, h: int, dur: float, color: str, fps: int = 24) -> bytes:
    d = tempfile.mkdtemp()
    p = f"{d}/v.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}:r={fps}:d={dur}",
                    "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                    "-t", str(dur), "-c:v", "libx264", "-c:a", "aac", "-shortest", p],
                   check=True)
    return Path(p).read_bytes()


def _probe(path: str) -> dict:
    d = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
        capture_output=True, text=True).stdout)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    return {"w": int(v["width"]), "h": int(v["height"]),
            "duration": float(d["format"]["duration"]),
            "has_audio": any(s["codec_type"] == "audio" for s in d["streams"])}


def _add(ctx, etype, **data):
    e = Entity(type=etype, id=f"{etype}-{uuid.uuid4().hex[:8]}",
               name=data.get("name", etype), data=data)
    ctx.doc.add(e)
    return e


class _Ctx:
    def log(self, *a, **k):
        pass

    def checkpoint(self):
        pass


def _scene(ctx, *, canvas=(1080, 1920), fps=30):
    """一横一竖两段真素材,接在时间线上。"""
    _add(ctx, "project", name="p", width=canvas[0], height=canvas[1], fps=fps)
    ref_a = ctx.asset_store.put(_mkvideo(1280, 720, 2.0, "red"),
                                mime="video/mp4", ext=".mp4").to_dict()
    ref_b = ctx.asset_store.put(_mkvideo(720, 1280, 2.0, "blue", fps=30),
                                mime="video/mp4", ext=".mp4").to_dict()
    a1 = _add(ctx, "asset", name="横片", assetType="video", assetRef=ref_a, duration=2.0)
    a2 = _add(ctx, "asset", name="竖片", assetType="video", assetRef=ref_b, duration=2.0)
    _add(ctx, "clip", name="第一段", assetId=a1.id, layer=0, visible=True,
         start=0.0, duration=2.0, sourceIn=0.0, xKind="source", fitMode="cover")
    _add(ctx, "clip", name="第二段", assetId=a2.id, layer=0, visible=True,
         start=2.0, duration=2.0, sourceIn=0.0, xKind="source")
    return canvas, fps


def _run_export(ctx, **args):
    """派发 export.render,再把 stage 真跑完并把 patches 落回实体。"""
    from _domain import render as dom_render
    ctx.cmd_reg.dispatch("export.render", args, actor=ctx._actor, source="user",
                         doc=ctx.doc, op_log=ctx.op_log)
    ent = [e for e in ctx.doc.list("export") if not getattr(e, "deleted", False)][-1]

    live = lambda t: [e for e in ctx.doc.list(t)  # noqa: E731
                      if not getattr(e, "deleted", False)]
    clips = [{**c.data, "id": c.id} for c in live("clip")]
    assets = {a.id: a for a in live("asset")}
    for c in clips:
        if not (isinstance(c.get("videoRef"), dict) and c["videoRef"].get("sha256")):
            a = assets.get(c.get("assetId") or "")
            if a is not None:
                c["videoRef"] = a.data.get("assetRef")
    proj = live("project")[0].data
    d = tempfile.mkdtemp()
    payload = {"clips": clips, "canvas": [proj["width"], proj["height"]],
               "fps": proj["fps"], "preset": args.get("preset") or dom_render.DEFAULT_PRESET,
               "start": args.get("start"), "end": args.get("end"),
               "_artifacts": (str(Path(d) / "receipt.txt"),)}
    out = export_stage.run_export_stage(_Ctx(), payload, asset_store=ctx.asset_store)
    if out.get("patches"):
        ent.update(out["patches"], expected_version=ent.version, actor=ctx._actor)
    return ent, out, Path(payload["_artifacts"][0])


def test_export_produces_a_canvas_spec_file_with_audio(ctx):
    """规划 P1 硬验收的核心:一横一竖两段 → 一个 1080×1920 的 mp4,带声,4 秒。"""
    _scene(ctx)
    ent, out, receipt = _run_export(ctx)
    assert out["patches"]["status"] == "succeeded", out
    assert ent.data["status"] == "succeeded"
    ref = ent.data["fileRef"]
    assert ref and ref.get("sha256")

    d = tempfile.mkdtemp()
    p = f"{d}/out.mp4"
    Path(p).write_bytes(ctx.asset_store.get(AssetRef.from_dict(ref)))
    got = _probe(p)
    assert (got["w"], got["h"]) == (1080, 1920), got
    # 容差按物理给:每一刀只能截到整帧,所以最多每刀差一帧。两刀 @24fps ≈ 0.084s。
    # 原来给的 0.4 松到能盖住整整十帧的系统性缩水 —— 那样的容差等于没断言。
    assert abs(got["duration"] - 4.0) < 0.12, got
    assert got["has_audio"], "导出必须带音频"
    assert ent.data["duration"] == pytest.approx(4.0, abs=0.05)
    # 收据必须落地,否则框架判 artifact_output_missing
    assert receipt.is_file() and "sha256:" in receipt.read_text()


def test_changing_the_canvas_changes_the_exported_file(ctx):
    """改画布 = 改输出规格 —— 这是「画布是输出规格不只是坐标系」的证明。"""
    _scene(ctx, canvas=(640, 360), fps=24)
    ent, _out, _r = _run_export(ctx)
    d = tempfile.mkdtemp()
    p = f"{d}/o.mp4"
    Path(p).write_bytes(ctx.asset_store.get(AssetRef.from_dict(ent.data["fileRef"])))
    assert (_probe(p)["w"], _probe(p)["h"]) == (640, 360)


def test_hidden_layer_is_not_in_the_exported_file(ctx):
    """关掉第二段的眼睛 → 成品只有 2 秒(INV-2:看见什么导出什么)。"""
    _scene(ctx, canvas=(640, 360), fps=24)
    second = [c for c in ctx.doc.list("clip") if c.data.get("start") == 2.0][0]
    second.update({"visible": False}, expected_version=second.version, actor=ctx._actor)
    ent, _out, _r = _run_export(ctx)
    d = tempfile.mkdtemp()
    p = f"{d}/o.mp4"
    Path(p).write_bytes(ctx.asset_store.get(AssetRef.from_dict(ent.data["fileRef"])))
    assert abs(_probe(p)["duration"] - 2.0) < 0.1


def test_export_range_can_be_narrowed(ctx):
    _scene(ctx, canvas=(640, 360), fps=24)
    ent, _out, _r = _run_export(ctx, start=1.0, end=3.0)
    d = tempfile.mkdtemp()
    p = f"{d}/o.mp4"
    Path(p).write_bytes(ctx.asset_store.get(AssetRef.from_dict(ent.data["fileRef"])))
    assert abs(_probe(p)["duration"] - 2.0) < 0.1


def test_export_with_nothing_visible_is_refused_with_a_reason(ctx):
    _scene(ctx, canvas=(640, 360), fps=24)
    for c in list(ctx.doc.list("clip")):
        c.update({"visible": False}, expected_version=c.version, actor=ctx._actor)
    with pytest.raises(ZeError) as exc:
        ctx.cmd_reg.dispatch("export.render", {}, actor=ctx._actor, source="user",
                             doc=ctx.doc, op_log=ctx.op_log)
    assert "没有显示中的视频图层" in exc.value.message
