"""导入素材:记录浏览器解码得到的真实规格,首个视频素材定画布。

为什么这两件事要一起测:它们是同一个错误的两半。规格靠人手填 → 填错;
画布靠人先猜 → 猜错。两个数一旦不是素材的真数,几何、烧框、导出全部错位,
而错位在界面上表现为「框画在这儿、出来的视频框在那儿」,极难倒查。

同步命令不做文件 IO；测试用 ffprobe 模拟 Web 导入器读到的规格，再断言实体落库结果。
"""
from __future__ import annotations

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

from sdk import Actor, Entity  # noqa: E402


@pytest.fixture()
def ctx():
    tmp = tempfile.mkdtemp(prefix="ze-ves-imp-")
    keys = ("ZE_DATA_DIR", "ZE_USERS_PATH", "ZE_SYSTEM_AUTH_DIR")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ["ZE_DATA_DIR"] = str(Path(tmp) / "data")
    os.environ["ZE_USERS_PATH"] = str(Path(tmp) / "users.json")
    os.environ["ZE_SYSTEM_AUTH_DIR"] = str(Path(tmp) / "auth")
    try:
        from sdk.bootstrap import bootstrap, SYSTEM_USER_ID
        repo = bootstrap()
        pid = repo.create_project(name="__imp__", owner=SYSTEM_USER_ID, seed=False)
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


def _mkvideo(w: int, h: int, dur: float, fps: int = 24) -> bytes:
    d = tempfile.mkdtemp()
    p = f"{d}/v.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", f"color=c=red:s={w}x{h}:r={fps}:d={dur}",
                    "-c:v", "libx264", p], check=True)
    return Path(p).read_bytes()


def _project(ctx, **data):
    # 画布字段有 schema 下限(16),给不了 0 —— 所以「画布还没定过」判不出来,
    # 只能靠「这是第一个素材」。新建项目就是 schema 默认的那个画幅。
    e = Entity(type="project", id=f"project-{uuid.uuid4().hex[:8]}", name="p",
               data={"width": 1920, "height": 1080, "fps": 30, **data})
    ctx.doc.add(e)
    return e


def _import(ctx, blob: bytes, **args):
    ref = ctx.asset_store.put(blob, mime="video/mp4", ext=".mp4").to_dict()
    with tempfile.NamedTemporaryFile(suffix=".mp4") as fh:
        fh.write(blob)
        fh.flush()
        from _domain import ffmpeg_ops
        spec = ffmpeg_ops.probe_media(fh.name)
    payload = {
        "assetType": "video", "assetRef": ref,
        "width": spec["width"], "height": spec["height"],
        "fps": spec["fps"], "duration": spec["duration"],
        **args,
    }
    _import.last_op = ctx.cmd_reg.dispatch(
        "asset.import", payload,
        actor=ctx._actor, source="user", doc=ctx.doc, op_log=ctx.op_log)
    return [a for a in ctx.doc.list("asset") if not getattr(a, "deleted", False)][-1]


def _live(ctx, t):
    return [e for e in ctx.doc.list(t) if not getattr(e, "deleted", False)]


# ── 导入探测 ───────────────────────────────────────────────────────

def test_import_records_real_spec_from_the_browser_probe(ctx):
    """Web 解码器给出的宽高帧率时长会被原样记录。"""
    asset = _import(ctx, _mkvideo(1280, 720, 1.5, fps=24), name="横片")
    assert (asset.data["width"], asset.data["height"]) == (1280, 720)
    assert abs(asset.data["fps"] - 24) < 0.01
    assert abs(asset.data["duration"] - 1.5) < 0.2
    assert not asset.data.get("placeholder")


def test_explicit_duration_still_wins(ctx):
    """人明确给了时长就听人的 —— 探测是补空,不是覆盖。"""
    asset = _import(ctx, _mkvideo(640, 360, 2.0), name="片", duration=0.75)
    assert abs(asset.data["duration"] - 0.75) < 0.01
    assert (asset.data["width"], asset.data["height"]) == (640, 360)   # 没给的照旧探


def test_placeholder_import_without_file_still_works(ctx):
    """没有文件的占位素材照旧能建 —— 探测不该变成新的门槛。"""
    ctx.cmd_reg.dispatch("asset.import", {"assetType": "video", "name": "待补"},
                         actor=ctx._actor, source="user", doc=ctx.doc, op_log=ctx.op_log)
    asset = _live(ctx, "asset")[-1]
    assert asset.data.get("placeholder") is True
    assert not asset.data.get("width")


# ── 首个素材定画布 ─────────────────────────────────────────────────

def test_first_asset_defines_the_canvas(ctx):
    proj = _project(ctx)
    _import(ctx, _mkvideo(1280, 720, 1.0, fps=24), name="第一段")
    proj = ctx.doc.get("project", proj.id)
    assert (proj.data["width"], proj.data["height"]) == (1280, 720)
    assert abs(proj.data["fps"] - 24) < 0.01


def test_second_asset_does_not_touch_the_canvas(ctx):
    """人可能已经调过画布了 —— 后续导入绝不能悄悄改画幅。"""
    proj = _project(ctx)
    _import(ctx, _mkvideo(1280, 720, 1.0, fps=24), name="第一段")
    proj = ctx.doc.get("project", proj.id)
    before = (proj.data["width"], proj.data["height"], proj.data["fps"])

    _import(ctx, _mkvideo(720, 1280, 1.0, fps=30), name="第二段")
    proj = ctx.doc.get("project", proj.id)
    assert (proj.data["width"], proj.data["height"], proj.data["fps"]) == before


def test_canvas_change_rides_in_the_same_operation(ctx):
    """定画布和导入必须是同一个 Operation —— 一次撤销要把两件事一起退回去。

    否则撤销之后素材没了、画幅却留着被改过的样子,人得手动改回去。
    """
    _project(ctx, width=640, height=480)
    _import(ctx, _mkvideo(1280, 720, 1.0), name="第一段")
    op = _import.last_op

    assert op.command_id == "asset.import"
    assert any(p.entity_type == "project" and p.field == "width" and p.after == 1280
               for p in op.patches), "定画布没跟着导入进同一个 op"
    # 反向补丁要把画布放回 640 —— 且排在删素材之前(后做的先撤)
    rev_canvas = [p for p in op.reverse_patches if p.entity_type == "project"]
    assert rev_canvas and rev_canvas[0].after == 640
    assert op.reverse_patches[-1].op == "delete"


def test_import_still_works_with_no_project_entity(ctx):
    """没有 project 实体时导入不该炸 —— 定画布是加分项,不是前置条件。"""
    asset = _import(ctx, _mkvideo(640, 360, 1.0), name="孤儿")
    assert (asset.data["width"], asset.data["height"]) == (640, 360)
