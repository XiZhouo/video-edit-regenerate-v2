"""命令语义单测(真 bootstrap,隔离数据目录):annotation 增删+编号重排 /
asset 命名唯一 / send.compile 产快照 / candidate.adopt stale 拒采 /
verify+mask_merge requires_runtime 降级 / send.dispatch 空输入拒。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "02-mod" / "05-code"))

from sdk import Actor, Entity, ZeError  # noqa: E402
import commands_register  # noqa: E402  (ensures import path is valid)
import document_register  # noqa: E402

FAKE_REF = {"sha256": "a" * 64, "mime": "video/mp4", "ext": ".mp4", "size": 3}


@pytest.fixture(scope="module")
def ctx():
    tmp = tempfile.mkdtemp(prefix="ze-ves-cmdtest-")
    # ZE_SYSTEM_AUTH_DIR 也必须隔离:不隔离就读写**真凭据库**,于是本机跑过服务(.venv,
    # 有 cryptography)留下的 sealed-v2 密钥,会让没有该后端的解释器整批 setup 直接炸
    # ——「昨天还绿的测试今天全红,而代码一行没动」。测试要自带沙盒,不看机器脸色。
    keys = ("ZE_DATA_DIR", "ZE_USERS_PATH", "ZE_SYSTEM_AUTH_DIR")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ["ZE_DATA_DIR"] = str(Path(tmp) / "data")
    os.environ["ZE_USERS_PATH"] = str(Path(tmp) / "users.json")
    os.environ["ZE_SYSTEM_AUTH_DIR"] = str(Path(tmp) / "auth")
    try:
        from sdk.bootstrap import bootstrap, SYSTEM_USER_ID
        repo = bootstrap()
        pid = repo.create_project(name="__cmdtest__", owner=SYSTEM_USER_ID, seed=False)
        context = repo.get_or_load(pid)
        context._test_actor = Actor(user_id=SYSTEM_USER_ID, source="user")
        yield context
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        for key, val in saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


def _dispatch(ctx, cmd, args):
    return ctx.cmd_reg.dispatch(cmd, args, actor=ctx._test_actor, source="user",
                                doc=ctx.doc, op_log=ctx.op_log)


def _add(ctx, etype, **data):
    e = Entity(type=etype, id=f"{etype}-{uuid.uuid4().hex[:8]}", name=data.get("name", etype),
               data=data)
    ctx.doc.add(e)
    return e


def test_send_profile_set_creates_missing_legacy_profile(ctx):
    """老项目没有 send_profile 时，第一次选模型必须创建配置，而不是回退 Omni。"""
    from sdk.bootstrap import bootstrap, SYSTEM_USER_ID

    repo = bootstrap()
    pid = repo.create_project(
        name=f"__legacy-profile-{uuid.uuid4().hex[:8]}__",
        owner=SYSTEM_USER_ID,
        seed=False,
    )
    legacy = repo.get_or_load(pid)
    actor = Actor(user_id=SYSTEM_USER_ID, source="user")
    legacy.doc.add(Entity(
        type="project",
        id=f"project-{uuid.uuid4().hex[:8]}",
        name="竖版历史项目",
        data={"name": "竖版历史项目", "width": 720, "height": 1280, "fps": 30},
    ))

    assert [
        item for item in legacy.doc.list("send_profile")
        if not getattr(item, "deleted", False)
    ] == []

    legacy.cmd_reg.dispatch(
        "send_profile.set",
        {
            "model": "seedance-2.0",
            "provider": "seedance",
            "strategy": "annotated_video",
            "mode": 1,
        },
        actor=actor,
        source="user",
        doc=legacy.doc,
        op_log=legacy.op_log,
    )

    profiles = [
        item for item in legacy.doc.list("send_profile")
        if not getattr(item, "deleted", False)
    ]
    assert len(profiles) == 1
    assert profiles[0].data["model"] == "seedance-2.0"
    assert profiles[0].data["provider"] == "seedance"
    assert profiles[0].data["ratio"] == "9:16"

    first_id = profiles[0].id
    legacy.cmd_reg.dispatch(
        "send_profile.set",
        {
            "model": "seedance-2.0-fast",
            "provider": "seedance",
            "strategy": "modified_frames",
            "mode": 2,
        },
        actor=actor,
        source="user",
        doc=legacy.doc,
        op_log=legacy.op_log,
    )
    profiles = [
        item for item in legacy.doc.list("send_profile")
        if not getattr(item, "deleted", False)
    ]
    assert [item.id for item in profiles] == [first_id]
    assert profiles[0].data["model"] == "seedance-2.0-fast"
    assert profiles[0].data["strategy"] == "modified_frames"


def test_prompt_optimize_accepts_a_new_request_id(ctx):
    """新请求不能被严格 selector 误报为 entity_not_found。"""
    clip = _fresh_edit_scene(ctx, "prompt-opt")
    _add(
        ctx, "annotation", name="提示词优化测试引导", kind="box",
        label="加入移动目标", visible=True,
        bboxX=10, bboxY=10, bboxW=40, bboxH=40, start=0.0, duration=1.0,
        targetClipId=clip.id,
    )
    request_id = f"opt-test-{uuid.uuid4().hex[:8]}"
    _dispatch(ctx, "prompt.optimize", {
        "requestId": request_id,
        "prompt": "让目标沿框选轨迹移动",
        "optimizerProvider": "codex_cli_profile",
        "optimizerModel": "",
        "provider": "omni",
        "strategy": "annotated_video",
        "model": "gemini-omni-flash-preview",
    })
    assert ctx.doc.find("prompt_optimization", request_id) is not None

    with pytest.raises(ZeError) as exc:
        _dispatch(ctx, "prompt.optimize", {
            "requestId": request_id,
            "prompt": "重复请求",
            "optimizerProvider": "codex_cli_profile",
            "optimizerModel": "",
            "provider": "omni",
            "strategy": "annotated_video",
            "model": "gemini-omni-flash-preview",
        })
    assert exc.value.code == "conflict"


# --- annotation ---------------------------------------------------------------

def _one(ctx, etype):
    return next(e for e in ctx.doc.list(etype) if not getattr(e, "deleted", False))


def test_annotation_add_auto_numbers(ctx):
    # 真断言:连加三条,编号必须严格递增(把自动编号删成常量则此测必红)
    before = len([e for e in ctx.doc.list("annotation") if not getattr(e, "deleted", False)])
    nums = []
    for i in range(3):
        _dispatch(ctx, "annotation.add", {"label": f"标注{i}", "bboxX": 10, "bboxW": 20,
                                          "start": 1.0, "duration": 1.0})
        anns = [e for e in ctx.doc.list("annotation") if not getattr(e, "deleted", False)]
        nums.append(anns[-1].data["number"])
    assert nums == [before + 1, before + 2, before + 3], nums
    assert anns[-1].data["label"] == "标注2"


def test_annotation_remove_renumbers(ctx):
    a = _dispatch(ctx, "annotation.add", {"label": "A"})  # noqa: F841
    b_op = _dispatch(ctx, "annotation.add", {"label": "B"})  # noqa: F841
    anns = sorted((e for e in ctx.doc.list("annotation") if not getattr(e, "deleted", False)),
                  key=lambda e: e.data.get("number") or 0)
    first = anns[0]
    _dispatch(ctx, "annotation.remove", {"id": first.id})
    remaining = sorted((e for e in ctx.doc.list("annotation") if not getattr(e, "deleted", False)),
                       key=lambda e: e.data.get("number") or 0)
    numbers = [e.data.get("number") for e in remaining]
    assert numbers == list(range(1, len(remaining) + 1))  # contiguous from 1


def test_guide_keyframe_set_uses_local_time_and_upserts_same_frame(ctx):
    ann = _add(
        ctx, "annotation", name="移动框", kind="box", label="让物体沿框移动",
        start=1.0, duration=3.0, bboxX=10, bboxY=20, bboxW=100, bboxH=80,
    )
    _dispatch(ctx, "guide.keyframe.set", {
        "annotationId": ann.id, "at": 2.0,
        "bboxX": 30, "bboxY": 40, "bboxW": 120, "bboxH": 90,
    })
    frames = [
        e for e in ctx.doc.list("guide_keyframe")
        if not getattr(e, "deleted", False) and e.data.get("parentId") == ann.id
    ]
    assert len(frames) == 1
    assert frames[0].data["time"] == pytest.approx(1.0)

    _dispatch(ctx, "guide.keyframe.set", {
        "annotationId": ann.id, "at": 2.01,
        "bboxX": 50, "bboxY": 60, "bboxW": 140, "bboxH": 110,
    })
    frames = [
        e for e in ctx.doc.list("guide_keyframe")
        if not getattr(e, "deleted", False) and e.data.get("parentId") == ann.id
    ]
    assert len(frames) == 1
    assert frames[0].data["bboxX"] == 50


def test_guide_keyframe_rejects_text_and_outside_guide_range(ctx):
    text = _add(
        ctx, "annotation", name="文字", kind="text", label="文字",
        start=1.0, duration=1.0, bboxX=0, bboxY=0, bboxW=20, bboxH=10,
    )
    with pytest.raises(ZeError):
        _dispatch(ctx, "guide.keyframe.set", {"annotationId": text.id, "at": 1.5})

    box = _add(
        ctx, "annotation", name="框", kind="box", label="框",
        start=2.0, duration=1.0, bboxX=0, bboxY=0, bboxW=20, bboxH=10,
    )
    with pytest.raises(ZeError):
        _dispatch(ctx, "guide.keyframe.set", {"annotationId": box.id, "at": 3.5})


def test_remove_guide_also_removes_its_keyframes(ctx):
    ann = _add(
        ctx, "annotation", name="待删除图片", kind="image", label="素材",
        start=0.0, duration=2.0, bboxX=0, bboxY=0, bboxW=100, bboxH=100,
    )
    _dispatch(ctx, "guide.keyframe.set", {
        "annotationId": ann.id, "at": 0.5,
        "bboxX": 10, "bboxY": 20, "bboxW": 80, "bboxH": 80,
    })
    frame = next(
        e for e in ctx.doc.list("guide_keyframe")
        if not getattr(e, "deleted", False) and e.data.get("parentId") == ann.id
    )
    _dispatch(ctx, "annotation.remove", {"id": ann.id})
    assert frame.deleted is True


# --- asset --------------------------------------------------------------------

def test_asset_import_requires_unique_name(ctx):
    _dispatch(ctx, "asset.import", {"name": "运镜参考", "assetType": "video"})
    with pytest.raises(ZeError) as exc:
        _dispatch(ctx, "asset.import", {"name": "运镜参考", "assetType": "video"})
    assert exc.value.code == "validation_failed"


def test_removing_an_image_guide_releases_its_orphaned_asset_name(ctx):
    name = f"可重新导入-{uuid.uuid4().hex[:8]}"
    asset = _add(ctx, "asset", name=name, assetType="image")
    guide = _add(
        ctx, "annotation", name=name, kind="image", label=name,
        refImageAssetId=asset.id, visible=True,
        bboxX=10, bboxY=10, bboxW=100, bboxH=100, start=0, duration=1,
    )

    _dispatch(ctx, "annotation.remove", {"id": guide.id})

    assert asset.deleted is True
    _dispatch(ctx, "asset.import", {"name": name, "assetType": "image"})


def test_removing_one_image_guide_keeps_an_asset_used_by_another(ctx):
    asset = _add(ctx, "asset", name=f"共享图片-{uuid.uuid4().hex[:8]}", assetType="image")
    first = _add(
        ctx, "annotation", name="共享图片一", kind="image", label="共享图片一",
        refImageAssetId=asset.id, visible=True,
        bboxX=10, bboxY=10, bboxW=100, bboxH=100, start=0, duration=1,
    )
    _add(
        ctx, "annotation", name="共享图片二", kind="image", label="共享图片二",
        refImageAssetId=asset.id, visible=True,
        bboxX=20, bboxY=20, bboxW=100, bboxH=100, start=1, duration=1,
    )

    _dispatch(ctx, "annotation.remove", {"id": first.id})

    assert asset.deleted is False


def test_importing_an_image_reclaims_a_legacy_orphan_with_the_same_name(ctx):
    name = f"历史孤儿-{uuid.uuid4().hex[:8]}"
    orphan = _add(ctx, "asset", name=name, assetType="image")

    _dispatch(ctx, "asset.import", {"name": name, "assetType": "image"})

    assert orphan.deleted is True
    live_named = [asset for asset in ctx.doc.list("asset") if asset.name == name]
    assert len(live_named) == 1
    assert live_named[0].id != orphan.id


def test_asset_import_needs_name(ctx):
    with pytest.raises(ZeError):
        _dispatch(ctx, "asset.import", {"assetType": "video"})


# --- send.compile -------------------------------------------------------------

def test_send_compile_mode_one_produces_snapshot(ctx):
    clip = _add(ctx, "clip", name="c", trackId="t", start=0, duration=8, xKind="source")
    _add(ctx, "annotation", name="标注", label="把2改成3", number=1,
         targetClipId=clip.id, bboxX=800, bboxY=440, bboxW=180, bboxH=160,
         start=1.2, duration=3.3, visible=True)
    profile = _add(ctx, "send_profile", name="p", mode=1,
                   model="gemini-omni-flash-preview", duration=4.3,
                   targetClipId=clip.id, repasteStrategy="full_segment")
    _dispatch(ctx, "send.compile", {"profileId": profile.id})
    jobs = [e for e in ctx.doc.list("send_job") if not getattr(e, "deleted", False)]
    assert jobs, "send_job should be created"
    job = jobs[-1]
    assert job.data["status"] == "compiled"
    assert "移除全部标注元素" in job.data["promptSnapshot"]
    assert job.data["mode"] == 1
    assert job.data.get("targetClipId") == clip.id  # 回贴去处 threaded from profile


def test_send_compile_rejects_missing_profile(ctx):
    with pytest.raises(ZeError):
        _dispatch(ctx, "send.compile", {"profileId": "nope"})


def test_send_compile_mode_two_includes_image_step(ctx):
    clip = _add(ctx, "clip", name="c2m", trackId="t", start=0, duration=8, xKind="source")
    _add(ctx, "annotation", name="a2", label="把产品换成红色款", number=1, targetClipId=clip.id,
         bboxX=100, bboxY=100, bboxW=50, bboxH=50, start=1, duration=1, visible=True)
    profile = _add(ctx, "send_profile", name="p2", mode=2, model="seedance-2.0",
                   targetClipId=clip.id, duration=3)
    _dispatch(ctx, "send.compile", {"profileId": profile.id})
    jobs = [e for e in ctx.doc.list("send_job") if not getattr(e, "deleted", False)]
    job = jobs[-1]
    assert job.data["mode"] == 2
    assert "不含任何标注元素" in (job.data.get("imageStepPromptSnapshot") or "")  # 图步 2A


def test_send_compile_mode_three_no_image_step(ctx):
    clip = _add(ctx, "clip", name="c3m", trackId="t", start=0, duration=8, xKind="source")
    _add(ctx, "annotation", name="a3", label="用参考图替换", number=1, targetClipId=clip.id,
         bboxX=0, bboxY=0, bboxW=10, bboxH=10, start=0, duration=1, visible=True)
    profile = _add(ctx, "send_profile", name="p3", mode=3, model="seedance-2.0",
                   targetClipId=clip.id, duration=3)
    _dispatch(ctx, "send.compile", {"profileId": profile.id})
    jobs = [e for e in ctx.doc.list("send_job") if not getattr(e, "deleted", False)]
    job = jobs[-1]
    assert job.data["mode"] == 3 and not job.data.get("imageStepPromptSnapshot")


# --- candidate.adopt ----------------------------------------------------------

def test_demo_seed_builds(ctx):
    # the demo bootstrap must run without raising (all entities pass schema)
    class _Doc:
        def __init__(self):
            self.items = []

        def add(self, e):
            self.items.append(e)

    d = _Doc()
    document_register._seed(d)
    types = {e.type for e in d.items}
    assert {"project", "asset", "clip", "annotation", "send_profile"} <= types
    assert "track" not in types          # 一行 = 一个片段,轨道实体已并进图层
    # 第 0 层是原片,默认可见 —— 时间线是一摞图层,不是一堆候选
    clip = next(e for e in d.items if e.type == "clip")
    assert clip.data["layer"] == 0 and clip.data["visible"] is True


def test_demo_media_ships_and_seeds_two_real_layers(tmp_path, monkeypatch):
    """demo 打开必须能看见东西。

    没有这一条,seed 退回占位素材时门禁照样全绿,而人打开是一块灰屏
    (「一个真的都没有」)—— 所以这里断言两个真视频文件在、两层都拿到真 ref。
    """
    assert document_register.DEMO_MEDIA.joinpath("source.mp4").is_file()
    assert document_register.DEMO_MEDIA.joinpath("edited.mp4").is_file()

    monkeypatch.setenv("ZE_DATA_DIR", str(tmp_path))

    class _Doc:
        def __init__(self):
            self.items = []

        def add(self, e):
            self.items.append(e)

    d = _Doc()
    document_register._seed(d)
    clips = sorted((e for e in d.items if e.type == "clip"), key=lambda e: e.data["layer"])
    assert [c.data["layer"] for c in clips] == [0, 1]
    # 第 0 层经素材引用拿到文件,第 1 层(AI 结果)自带正片
    asset = next(e for e in d.items if e.type == "asset")
    assert asset.data["assetRef"]["sha256"] and asset.data["placeholder"] is False
    assert clips[1].data["videoRef"]["sha256"] != asset.data["assetRef"]["sha256"]
    # 灌进去的确实是共享池,不是别处
    assert (tmp_path / "shared_assets" / "blobs").is_dir()


# --- 审查补测:红线阻断 / 区间落库 / profile 派发 / 机检红拒采 ------------------

def test_edit_send_needs_a_prompt(ctx):
    with pytest.raises(ZeError) as exc:
        _dispatch(ctx, "edit.send", {"start": 0, "end": 2, "prompt": "   "})
    assert exc.value.code == "validation_failed"


def test_edit_send_rejects_empty_span(ctx):
    with pytest.raises(ZeError) as exc:
        _dispatch(ctx, "edit.send", {"start": 3, "end": 3, "prompt": "换个颜色"})
    assert "这一段是空的" in exc.value.message


def test_edit_send_rejects_when_every_layer_is_hidden(ctx):
    """把所有行的眼睛关掉 = 这次不发任何东西 —— 必须挡住,不能发个空的出去。"""
    doc = ctx.doc
    kept = [(c, c.data.get("visible")) for c in doc.list("clip")
            if not getattr(c, "deleted", False)]
    for clip, _old in kept:
        clip.update({"visible": False}, expected_version=clip.version, actor=ctx._test_actor)
    try:
        with pytest.raises(ZeError) as exc:
            _dispatch(ctx, "edit.send", {"start": 0, "end": 2, "prompt": "换个颜色"})
        assert "没有可见图层" in exc.value.message
    finally:
        for clip, old in kept:
            clip.update({"visible": bool(old)}, expected_version=clip.version,
                        actor=ctx._test_actor)


def test_edit_send_accepts_seedance_annotated_video(ctx):
    """Seedance 的标记视频方式是明确开放组合，不再被旧 edit-only 规则误拦。"""
    src = _add(ctx, "asset", name="编辑源片", assetType="video", assetRef=FAKE_REF, duration=8)
    clip = _add(ctx, "clip", name="原片L", assetId=src.id, layer=0, visible=True,
                start=0, duration=8, sourceIn=0, xKind="source")
    assert clip.data["visible"] is True
    _dispatch(ctx, "edit.send", {
        "start": 1, "end": 3, "prompt": "换个颜色",
        "provider": "seedance", "strategy": "annotated_video",
        "model": "seedance-2.0",
    })
    jobs = [e for e in ctx.doc.list("send_job") if not getattr(e, "deleted", False)]
    assert jobs[-1].data["provider"] == "seedance"
    assert jobs[-1].data["strategy"] == "annotated_video"


def test_edit_send_accepts_omni_modified_frames_without_local_capability_rejection(ctx):
    """产品允许任意模型搭配任意提交方式；真实能力由服务调用结果回答。"""
    src = _add(ctx, "asset", name="Omni 修改画面源片", assetType="video",
               assetRef=FAKE_REF, duration=4)
    clip = _add(ctx, "clip", name="Omni 原片", assetId=src.id, layer=0, visible=True,
                start=0, duration=4, sourceIn=0, xKind="source")
    _add(ctx, "annotation", name="修改区域", kind="box", label="按参考画面修改",
         number=99, layer=1, visible=True, targetClipId=clip.id,
         bboxX=10, bboxY=10, bboxW=100, bboxH=100, start=0, duration=2)
    _add(ctx, "reference_frame", name="参考画面", at=1.0, enabled=True)
    _dispatch(ctx, "edit.send", {
        "start": 0, "end": 2, "prompt": "按参考画面修改",
        "provider": "omni", "strategy": "modified_frames",
        "model": "gemini-omni-flash-preview",
    })
    jobs = [e for e in ctx.doc.list("send_job") if not getattr(e, "deleted", False)]
    assert jobs[-1].data["provider"] == "omni"
    assert jobs[-1].data["strategy"] == "modified_frames"


@pytest.mark.parametrize(
    ("provider", "model", "seconds", "message"),
    [
        ("omni", "gemini-omni-flash-preview", 10.01, "Omni 单次最多处理 10 秒"),
        ("seedance", "seedance-2.0-fast", 15.01, "Seedance 单次最多生成 15 秒"),
    ],
)
def test_edit_send_rejects_provider_overlong_span_before_launch(
    ctx, provider, model, seconds, message,
):
    src = _add(
        ctx, "asset", name=f"{provider} 长片",
        assetType="video", assetRef=FAKE_REF, duration=seconds,
    )
    clip = _add(
        ctx, "clip", name=f"{provider} 长片层", assetId=src.id, layer=0, visible=True,
        start=0, duration=seconds, sourceIn=0, xKind="source",
    )
    _add(
        ctx, "annotation", name=f"{provider} 长引导", kind="box", label="修改这一段",
        number=101, layer=20, visible=True, targetClipId=clip.id,
        bboxX=10, bboxY=10, bboxW=100, bboxH=100, start=0, duration=seconds,
    )
    with pytest.raises(ZeError) as exc:
        _dispatch(ctx, "edit.send", {
            "start": 0, "end": seconds, "prompt": "修改这一段",
            "provider": provider, "strategy": "annotated_video", "model": model,
        })
    assert message in exc.value.message


def test_modified_frames_uses_position_keyframe_times_without_manual_references(ctx):
    clip = _fresh_edit_scene(ctx, "moving-reference")
    for cue in ctx.doc.list("reference_frame"):
        if not getattr(cue, "deleted", False) and cue.data.get("enabled") is not False:
            cue.update({"enabled": False}, expected_version=cue.version, actor=ctx._test_actor)
    ann = _add(
        ctx, "annotation", name="移动区域", kind="box", label="让物体沿区域移动",
        number=1, layer=1, visible=True, targetClipId=clip.id,
        bboxX=10, bboxY=10, bboxW=100, bboxH=100, start=1.0, duration=3.0,
    )
    for at, x in ((1.5, 20), (3.0, 200)):
        _dispatch(ctx, "guide.keyframe.set", {
            "annotationId": ann.id, "at": at,
            "bboxX": x, "bboxY": 30, "bboxW": 100, "bboxH": 100,
        })
    _dispatch(ctx, "edit.send", {
        "provider": "seedance", "strategy": "modified_frames",
        "model": "seedance-2.0-fast",
    })
    jobs = [e for e in ctx.doc.list("send_job") if not getattr(e, "deleted", False)]
    assert json.loads(jobs[-1].data["editedFramesJson"]) == [1.5, 3.0]


def test_send_compile_persists_interval_and_blocked_flag(ctx):
    clip = _add(ctx, "clip", name="ci", trackId="t", start=0, duration=8, xKind="source")
    _add(ctx, "annotation", name="ai", label="改数字", number=1, targetClipId=clip.id,
         bboxX=100, bboxY=100, bboxW=50, bboxH=50, start=1.2, duration=3.3, visible=True)
    profile = _add(ctx, "send_profile", name="pi", mode=1,
                   model="gemini-omni-flash-preview", targetClipId=clip.id, paddingSec=0.5)
    _dispatch(ctx, "send.compile", {"profileId": profile.id})
    job = [e for e in ctx.doc.list("send_job") if not getattr(e, "deleted", False)][-1]
    interval = json.loads(job.data["sendIntervalJson"])
    assert interval["annotation_union"] == [1.2, 4.5]
    assert interval["start"] == 0.7 and interval["end"] == 5.0   # ± padding
    assert job.data["blocked"] is False



# ── 标注即图层:眼睛关掉 = 三处一起消失 ──────────────────────────────

def _fresh_edit_scene(ctx, name):
    """一个能真发的场景:只有一层带文件的原片可见。

    **必须把之前测试留下的 clip 也关掉**:它们不少没有 assetRef,而 compose_plan 选
    最上层时同 layer 之间按 id 排序(uuid 随机)—— 不隔离的话这个测试会随机撞上
    「图层还没有视频文件」而红,是个真随机的假红。
    """
    for e in list(ctx.doc.list("clip")) + list(ctx.doc.list("annotation")):
        if not getattr(e, "deleted", False) and e.data.get("visible") is not False:
            e.update({"visible": False}, expected_version=e.version, actor=ctx._test_actor)
    src = _add(ctx, "asset", name=f"素材{name}", assetType="video",
               assetRef=FAKE_REF, duration=8)
    return _add(ctx, "clip", name=f"层{name}", assetId=src.id, layer=0, visible=True,
                start=0, duration=8, sourceIn=0, xKind="source")


def test_hidden_annotation_is_excluded_from_what_gets_sent(ctx):
    """关掉眼睛的标注,不进「这次要发的标注」这份清单。

    这是「标注是图层」在发送侧的兑现点:画面上烧的、提示词里说的,都从这份清单来。
    """
    import commands_register as cr
    _fresh_edit_scene(ctx, "hid")
    _add(ctx, "annotation", name="看得见的", label="把地板换成草地", number=1,
         visible=True, bboxX=10, bboxY=10, bboxW=100, bboxH=100, start=0.0, duration=4.0)
    _add(ctx, "annotation", name="关掉的", label="这句不该发出去", number=2,
         visible=False, bboxX=20, bboxY=20, bboxW=100, bboxH=100, start=0.0, duration=4.0)

    labels = [a.data.get("label") for a in cr._visible_annotations(ctx.doc)]
    assert "把地板换成草地" in labels
    assert "这句不该发出去" not in labels


def test_hidden_annotation_also_drops_out_of_send_compile(ctx):
    """两条发送路径必须同一行为 —— 只改一条就是「报红在无辜一侧」。"""
    clip = _fresh_edit_scene(ctx, "hid2")
    _add(ctx, "annotation", name="关掉的2", label="不该编译进去", number=1, visible=False,
         targetClipId=clip.id, bboxX=10, bboxY=10, bboxW=100, bboxH=100,
         start=0.0, duration=4.0)
    profile = _add(ctx, "send_profile", name="ph", mode=1,
                   model="gemini-omni-flash-preview", targetClipId=clip.id)
    with pytest.raises(ZeError) as exc:
        _dispatch(ctx, "send.compile", {"profileId": profile.id})
    assert "显示中的标注" in exc.value.message


def test_annotation_labels_actually_reach_the_model(ctx):
    """标注文字必须真的进提示词 —— 从前一个字都没进,模型只看到几个彩框。"""
    from _domain import ffmpeg_ops
    clip = _fresh_edit_scene(ctx, "lbl")
    _add(ctx, "annotation", name="A", label="把木地板换成青草地", number=1, visible=True,
         color="#000000",
         bboxX=10, bboxY=10, bboxW=100, bboxH=100, start=0.0, duration=4.0,
         targetClipId=clip.id)
    _dispatch(ctx, "edit.send", {"start": 0.0, "end": 4.0,
                                 "prompt": "其余保持不动", "model": ""})
    job = [e for e in ctx.doc.list("send_job") if not getattr(e, "deleted", False)][-1]
    snap = job.data["promptSnapshot"]
    assert "把木地板换成青草地" in snap                      # 标注文字进去了
    assert ffmpeg_ops.annotation_paint({"color": "#000000"})[1] in snap  # 按用户选色认框
    assert "其余保持不动" in snap                            # 人写的那句也还在
    assert snap.index("把木地板换成青草地") < snap.index("其余保持不动")


def test_user_guide_colors_survive_renumber_after_delete(ctx):
    """颜色属于用户画的引导，不应随删除后的编号重排而改变。"""
    for a in list(ctx.doc.list("annotation")):
        if not getattr(a, "deleted", False):
            _dispatch(ctx, "annotation.remove", {"id": a.id})
    ids = []
    colors = ["#000000", "#ffffff", "#ff3b30"]
    for i, color in enumerate(colors):
        _dispatch(ctx, "annotation.add", {
            "label": f"第{i}条", "bboxW": 20, "bboxH": 20, "color": color,
        })
        ids.append([e for e in ctx.doc.list("annotation")
                    if not getattr(e, "deleted", False)][-1].id)
    _dispatch(ctx, "annotation.remove", {"id": ids[0]})
    rest = sorted((e for e in ctx.doc.list("annotation") if not getattr(e, "deleted", False)),
                  key=lambda e: e.data["number"])
    assert [e.data["number"] for e in rest] == [1, 2]
    assert [e.data["color"] for e in rest] == ["#ffffff", "#ff3b30"]


def test_annotation_add_rejects_colors_outside_the_visible_palette(ctx):
    with pytest.raises(ZeError) as exc:
        _dispatch(ctx, "annotation.add", {
            "label": "非法颜色", "bboxW": 20, "bboxH": 20, "color": "#32d0d8",
        })
    assert "黑色、白色或红色" in exc.value.message


# ── 发送范围从标注来,不用人另外框 ─────────────────────────────────

def test_send_range_comes_from_the_annotation_not_from_a_manual_span(ctx):
    """原视频在 Omni 限制内时，即使只标 1 秒也完整提交。"""
    clip = _fresh_edit_scene(ctx, "derive")     # 一层 0–8s 的原片
    _add(ctx, "annotation", name="一秒", label="把这块换成青草地", number=1, visible=True,
         bboxX=100, bboxY=100, bboxW=200, bboxH=200, start=3.0, duration=1.0,
         targetClipId=clip.id)
    before = len([e for e in ctx.doc.list("send_job") if not getattr(e, "deleted", False)])
    _dispatch(ctx, "edit.send", {})              # 不给 start/end,也不给 prompt
    jobs = [e for e in ctx.doc.list("send_job") if not getattr(e, "deleted", False)]
    assert len(jobs) == before + 1, "一条标注 → 一次发送"
    job = jobs[-1]
    assert "0.0" in job.name and "8.0" in job.name, job.name
    # 提示词是从标注文字自动生成的,人一个字都不用打
    assert "把这块换成青草地" in job.data["promptSnapshot"]


def test_far_apart_annotations_share_the_full_video_when_it_fits(ctx):
    """原视频只有 8 秒且符合 Omni 限制，两条远隔引导也只完整提交一次。"""
    clip = _fresh_edit_scene(ctx, "two")
    _add(ctx, "annotation", name="前", label="前面这块改掉", number=1, visible=True,
         bboxX=10, bboxY=10, bboxW=100, bboxH=100, start=1.0, duration=1.0,
         targetClipId=clip.id)
    _add(ctx, "annotation", name="后", label="后面这块改掉", number=2, visible=True,
         bboxX=20, bboxY=20, bboxW=100, bboxH=100, start=6.0, duration=1.0,
         targetClipId=clip.id)
    before = len([e for e in ctx.doc.list("send_job") if not getattr(e, "deleted", False)])
    _dispatch(ctx, "edit.send", {})
    jobs = [e for e in ctx.doc.list("send_job") if not getattr(e, "deleted", False)]
    assert len(jobs) == before + 1
    assert "0.0" in jobs[-1].name and "8.0" in jobs[-1].name
    assert "前面这块改掉" in jobs[-1].data["promptSnapshot"]
    assert "后面这块改掉" in jobs[-1].data["promptSnapshot"]


def test_nearby_annotations_stay_in_one_send(ctx):
    """[3,4] 与 [5,6] 加完余量挨上了 → 它们在同一处,合成一段发。"""
    clip = _fresh_edit_scene(ctx, "near")
    for n, s0 in ((1, 3.0), (2, 5.0)):
        _add(ctx, "annotation", name=f"n{n}", label=f"第{n}处改掉", number=n, visible=True,
             bboxX=10 * n, bboxY=10, bboxW=100, bboxH=100, start=s0, duration=1.0,
             targetClipId=clip.id)
    before = len([e for e in ctx.doc.list("send_job") if not getattr(e, "deleted", False)])
    _dispatch(ctx, "edit.send", {})
    jobs = [e for e in ctx.doc.list("send_job") if not getattr(e, "deleted", False)]
    assert len(jobs) == before + 1
    snap = jobs[-1].data["promptSnapshot"]
    assert "第1处改掉" in snap and "第2处改掉" in snap


def test_send_without_any_visible_annotation_is_refused_with_a_reason(ctx):
    _fresh_edit_scene(ctx, "none")               # 只有图层,没有显示中的标注
    with pytest.raises(ZeError) as exc:
        _dispatch(ctx, "edit.send", {})
    assert "没有显示中的画面引导" in exc.value.message


# ── 看见什么就发什么:被上面的视频层盖住的标注不生效 ──────────────────

def test_annotation_covered_by_an_upper_video_layer_is_not_sent(ctx):
    """标注在第 0 层,一段视频在第 5 层整段盖住它 → 画面上看不见,发不出去。"""
    _fresh_edit_scene(ctx, "cover")               # 一层 0–8s 的原片(layer 0)
    src2 = _add(ctx, "asset", name="盖住的素材", assetType="video",
                assetRef=FAKE_REF, duration=8)
    _add(ctx, "clip", name="盖住的层", assetId=src2.id, layer=5, visible=True,
         start=0, duration=8, sourceIn=0, xKind="gen")
    _add(ctx, "annotation", name="被盖住", label="这块改掉", number=1, visible=True,
         layer=1, bboxX=10, bboxY=10, bboxW=100, bboxH=100, start=1.0, duration=2.0)
    with pytest.raises(ZeError) as exc:
        _dispatch(ctx, "edit.send", {})
    assert "盖住" in exc.value.message


def test_lifting_the_annotation_above_the_cover_makes_it_sendable(ctx):
    """同样的遮挡,把标注层序抬到盖住那层之上 → 露出来了,能发。"""
    _fresh_edit_scene(ctx, "lift")
    src2 = _add(ctx, "asset", name="盖住的素材2", assetType="video",
                assetRef=FAKE_REF, duration=8)
    _add(ctx, "clip", name="盖住的层2", assetId=src2.id, layer=5, visible=True,
         start=0, duration=8, sourceIn=0, xKind="gen")
    _add(ctx, "annotation", name="抬上去", label="这块改掉", number=1, visible=True,
         layer=6, bboxX=10, bboxY=10, bboxW=100, bboxH=100, start=1.0, duration=2.0)
    before = len([e for e in ctx.doc.list("send_job") if not getattr(e, "deleted", False)])
    _dispatch(ctx, "edit.send", {})
    jobs = [e for e in ctx.doc.list("send_job") if not getattr(e, "deleted", False)]
    assert len(jobs) == before + 1
    assert "这块改掉" in jobs[-1].data["promptSnapshot"]


def test_partially_covered_annotation_only_sends_the_exposed_part(ctx):
    """原视频符合限制时完整提交，但遮挡关系仍决定引导在哪些时段可见。"""
    _fresh_edit_scene(ctx, "part")                # 原片 layer 0, 0–8s
    src2 = _add(ctx, "asset", name="中段素材", assetType="video",
                assetRef=FAKE_REF, duration=8)
    _add(ctx, "clip", name="中段盖住", assetId=src2.id, layer=5, visible=True,
         start=3.0, duration=2.0, sourceIn=0, xKind="gen")   # 盖住 3–5s
    _add(ctx, "annotation", name="跨过遮挡", label="这一大块改掉", number=1, visible=True,
         layer=1, bboxX=10, bboxY=10, bboxW=100, bboxH=100, start=2.0, duration=4.0)
    before = len([e for e in ctx.doc.list("send_job") if not getattr(e, "deleted", False)])
    _dispatch(ctx, "edit.send", {})
    jobs = [e for e in ctx.doc.list("send_job") if not getattr(e, "deleted", False)]
    new = jobs[before:]
    assert len(new) == 1, [j.name for j in new]
    assert "0.0" in new[0].name and "8.0" in new[0].name


def test_new_annotation_lands_above_the_layer_you_are_looking_at(ctx):
    """新画的框默认压在当前最上层视频之上 —— 否则它自己就被盖住、看不见。"""
    _fresh_edit_scene(ctx, "above")
    src2 = _add(ctx, "asset", name="上层素材", assetType="video",
                assetRef=FAKE_REF, duration=8)
    _add(ctx, "clip", name="上层", assetId=src2.id, layer=7, visible=True,
         start=0, duration=8, sourceIn=0, xKind="gen")
    _dispatch(ctx, "annotation.add", {"label": "新的", "bboxW": 50, "bboxH": 50,
                                      "start": 2.0, "duration": 1.0})
    ann = [e for e in ctx.doc.list("annotation") if not getattr(e, "deleted", False)][-1]
    assert ann.data["layer"] == 8.0, ann.data
