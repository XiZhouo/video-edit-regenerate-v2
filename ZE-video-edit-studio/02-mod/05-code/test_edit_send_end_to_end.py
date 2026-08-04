"""把 edit.send 整条链跑完一次 —— 合成 → 烧框 → 发 → **结果实体真的落库**。

为什么必须有这个测试:之前所有测试都停在「命令派发成功」,从没让 stage 跑完并把
``created_entities`` 交给 Document 校验。于是 `clip.duration` 的 schema 下限
(minimum 0.2)在真人点第一次发送时才炸 —— 而那时合成、烧框、调服务全已白做。

ffmpeg 与视频服务都替换成假的:这个测试要证的是**契约**(结果实体过不过 schema、
时间换算对不对),不是 ffmpeg 会不会跑。
"""

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
import layer_edit_stage  # noqa: E402
import generation_contract  # noqa: E402
from _domain import ffmpeg_ops  # noqa: E402

FAKE_REF = {"sha256": "b" * 64, "mime": "video/mp4", "ext": ".mp4", "size": 5}


@pytest.fixture()
def ctx():
    tmp = tempfile.mkdtemp(prefix="ze-ves-e2e-")
    keys = ("ZE_DATA_DIR", "ZE_USERS_PATH", "ZE_SYSTEM_AUTH_DIR")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ["ZE_DATA_DIR"] = str(Path(tmp) / "data")
    os.environ["ZE_USERS_PATH"] = str(Path(tmp) / "users.json")
    os.environ["ZE_SYSTEM_AUTH_DIR"] = str(Path(tmp) / "auth")
    try:
        from sdk.bootstrap import bootstrap, SYSTEM_USER_ID
        repo = bootstrap()
        pid = repo.create_project(name="__e2e__", owner=SYSTEM_USER_ID, seed=False)
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


class _FakeClient:
    """服务替身:提交即完成,直接给产物 URL。"""

    def submit_files(self, **kw):
        self.last = kw
        return {"status": "completed", "video_url": "http://x/out.mp4"}

    def poll(self, task_id):
        return {"status": "completed", "video_url": "http://x/out.mp4"}

    @staticmethod
    def download(url):
        return b"FAKEMP4"

    task_id_of = staticmethod(layer_edit_stage.videogen_client.task_id_of)
    result_url_of = staticmethod(layer_edit_stage.videogen_client.result_url_of)
    error_message_of = staticmethod(layer_edit_stage.videogen_client.error_message_of)


class _Ctx:
    def log(self, *a, **k):
        pass

    def checkpoint(self):
        pass


def _fake_ffmpeg(monkeypatch, work_probe=None):
    """ffmpeg 全替成「写个占位文件就算成功」。"""
    def _touch(path):
        Path(path).write_bytes(b"FAKE")
        return path
    monkeypatch.setattr(ffmpeg_ops, "ffmpeg_bin", lambda: "/fake/ffmpeg")
    # 素材是假字节,真 ffprobe 读不了 —— 假一个够长的时长,让「图层比素材长」的
    # 守卫不误伤(守卫本身由域层 test_render 用真文件验)
    monkeypatch.setattr(ffmpeg_ops, "probe_duration", lambda path: 60.0)
    monkeypatch.setattr(ffmpeg_ops, "cut_segment",
                        lambda src, out, start, dur: _touch(out))
    monkeypatch.setattr(ffmpeg_ops, "normalize_segment",
                        lambda src, out, **kw: _touch(out))
    monkeypatch.setattr(ffmpeg_ops, "black_filler",
                        lambda out, dur, **kw: _touch(out))
    monkeypatch.setattr(ffmpeg_ops, "concat_videos",
                        lambda inputs, out: _touch(out))
    def _burn(src, out, anns, **kw):
        if work_probe is not None:
            work_probe.append({"annotations": anns, "kw": kw})
        return _touch(out)
    monkeypatch.setattr(ffmpeg_ops, "burn_annotations", _burn)


def _scene(ctx, *, mark_start, mark_duration, padding=0.5):
    """一层带文件的原片 + 一条标注。返回 (clip, annotation)。"""
    add = lambda t, **d: ctx.doc.add(Entity(  # noqa: E731
        type=t, id=f"{t}-{uuid.uuid4().hex[:8]}", name=d.get("name", t), data=d)) \
        or [e for e in ctx.doc.list(t) if not getattr(e, "deleted", False)][-1]
    # 素材要真进 AssetStore —— stage 会去读它的字节;编个 sha 是读不出来的
    ref = ctx.asset_store.put(b"FAKE-SOURCE-MP4", mime="video/mp4", ext=".mp4").to_dict()
    src = add("asset", name="源", assetType="video", assetRef=ref, duration=8)
    clip = add("clip", name="原片", assetId=src.id, layer=0, visible=True,
               start=0, duration=8, sourceIn=0, xKind="source")
    add("project", name="p", width=1280, height=720, fps=24)
    add("send_profile", name="发送", mode=1, model="gemini-omni-flash-preview",
        paddingSec=padding)
    ann = add("annotation", name="标注", label="这块换成青草地", number=1, visible=True,
              layer=1, bboxX=100, bboxY=100, bboxW=300, bboxH=200,
              start=mark_start, duration=mark_duration)
    return clip, ann


def _run_pipeline(ctx, monkeypatch, probe=None):
    """按 edit.send 的同一条路径造 payload,再把 stage 真跑完、结果实体落库。

    payload 用的是命令层调的**同一批域层函数**(send_groups → compose_plan →
    exposed_windows),所以「段太短」这类问题在这里同样会现形;而落库那一步会让
    Document 拿 schema 校验结果实体 —— 从前所有测试都停在「命令派发成功」,
    这一步从没跑过,于是 clip.duration 的下限在真人点第一次发送时才炸。

    先真派发一次 edit.send(证明命令层自己也能走通、落得下 send_job),再跑 stage。
    框架用 spec_from_file_location 载 mod、模块不进 sys.modules,拦不到它的
    WorkItem,所以 payload 在这里按同一套域层函数重建。
    """
    _fake_ffmpeg(monkeypatch, probe)
    from _domain import layer_compose

    ctx.cmd_reg.dispatch("edit.send", {"prompt": "其余不动"},
                         actor=ctx._actor, source="user",
                         doc=ctx.doc, op_log=ctx.op_log)
    jobs = [e for e in ctx.doc.list("send_job") if not getattr(e, "deleted", False)]
    assert jobs, "edit.send 必须落 send_job"

    live = lambda t: [e for e in ctx.doc.list(t) if not getattr(e, "deleted", False)]  # noqa: E731
    clips = [{**c.data, "id": c.id} for c in live("clip")]
    proj = live("project")[0].data
    total = max((c["start"] + c["duration"] for c in clips), default=0.0)
    marks = []
    for a in live("annotation"):
        if a.data.get("visible") is False:
            continue
        st = float(a.data.get("start") or 0)
        marks.append({"id": a.id, "layer": float(a.data.get("layer") or 0),
                      "visible": True, "number": int(a.data.get("number") or 0),
                      "label": str(a.data.get("label") or ""),
                      "bbox": {"x": a.data.get("bboxX", 0), "y": a.data.get("bboxY", 0),
                               "w": a.data.get("bboxW", 0), "h": a.data.get("bboxH", 0)},
                      "interval": [st, st + float(a.data.get("duration") or 0)]})
    padding = float(live("send_profile")[0].data.get("paddingSec", 0.5))
    exposure = layer_compose.exposed_windows(clips, marks, 0.0, max(total, 0.001))
    exposed = [{**m, "windows": exposure[m["id"]],
                "interval": [exposure[m["id"]][0][0], exposure[m["id"]][-1][1]]}
               for m in marks if m["id"] in exposure]
    focus_groups = layer_compose.send_groups(
        exposed, padding_sec=padding, total_duration=total,
    )
    groups = generation_contract.plan_submission_groups(
        provider="omni",
        total_duration=total,
        focus_groups=focus_groups,
    )
    assert groups, "算不出发送段"

    base = layer_compose.next_layer(clips + exposed)
    assets = {a.id: a for a in live("asset")}
    results = []
    for i, g in enumerate(groups):
        plan = layer_compose.compose_plan(clips, g["start"], g["end"])
        assert plan["valid"], plan["reason"]
        for cut in plan["cuts"]:
            if not (isinstance(cut.get("video_ref"), dict) and cut["video_ref"].get("sha256")):
                cut["video_ref"] = assets[cut["asset_id"]].data["assetRef"]
        anns = layer_compose.annotations_in_span(exposed, g["start"], g["end"])
        payload = {"plan": plan, "prompt": "1. 这块换成青草地\n\n其余不动",
                   "model": "gemini-omni-flash-preview", "annotations": anns,
                   "ratio": "16:9", "canvas": [proj["width"], proj["height"]],
                   "fps": proj["fps"], "new_layer": base + i,
                   # 框架会把 WorkItem.artifacts 以 _artifacts 传进 stage;
                   # stage 必须真把它写出来,否则框架判 artifact_output_missing
                   "_artifacts": (str(Path(ctx.data_dir) / "pipeline_artifacts"
                                      / f"e2e-{i}.txt"),)}
        out = layer_edit_stage.run_edit_stage(
            _Ctx(), payload, asset_store=ctx.asset_store, client=_FakeClient(),
            sleeper=lambda s: None, poll_interval=0.0, poll_deadline=1.0)
        assert out.get("patches", {}).get("status") == "succeeded", out
        for spec in out.get("created_entities") or []:
            # **这一步就是从前缺的那一步** —— 结果实体必须过 schema
            ctx.doc.add(Entity(type=spec["type"], id=spec["id"],
                               name=spec["name"], data=spec["data"]))
        receipt = Path(payload["_artifacts"][0])
        assert receipt.is_file(), (
            f"stage 没写声明的产物 {receipt} —— 框架会判 artifact_output_missing:"
            "结果视频拿回来了、图层也落了,任务却报红")
        body = receipt.read_text()
        assert "result_sha256:" in body and "span:" in body, body
        results.append(out)
    return results


def test_a_one_second_mark_runs_the_whole_chain_and_the_result_clip_passes_schema(
        ctx, monkeypatch):
    """一条 1 秒标注 → 整条链跑完 → 结果图层落库(schema 通过)。"""
    _scene(ctx, mark_start=3.0, mark_duration=1.0)
    results = _run_pipeline(ctx, monkeypatch)
    assert len(results) == 1
    clips = [e for e in ctx.doc.list("clip") if not getattr(e, "deleted", False)]
    gen = [c for c in clips if c.data.get("xKind") == "gen"]
    assert len(gen) == 1, "结果必须落成一个新图层"
    g = gen[0].data
    assert g["start"] == 0 and round(g["duration"], 2) == 8.0
    assert g["visible"] is True and g["videoRef"]["sha256"]
    assert g["layer"] > 1, "新图层要插在最上面"


def test_a_single_frame_mark_still_produces_a_schema_valid_result_clip(
        ctx, monkeypatch):
    """单帧标注(duration=0)—— 从前这条会产出 0.17s 的结果层,被 schema 拒在最后一步。"""
    _scene(ctx, mark_start=3.4, mark_duration=0.0)
    _run_pipeline(ctx, monkeypatch)
    gen = [c for c in ctx.doc.list("clip")
           if not getattr(c, "deleted", False) and c.data.get("xKind") == "gen"]
    assert len(gen) == 1
    dur = gen[0].data["duration"]
    assert dur >= 0.2, f"结果层时长 {dur} 低于 clip.duration 的 schema 下限"


def test_a_mark_at_the_very_end_of_the_timeline_also_survives(ctx, monkeypatch):
    """贴着片尾的标注:余量往右撑不动,得往左撑,不能撑出一个超短段。"""
    _scene(ctx, mark_start=7.98, mark_duration=0.0)
    _run_pipeline(ctx, monkeypatch)
    gen = [c for c in ctx.doc.list("clip")
           if not getattr(c, "deleted", False) and c.data.get("xKind") == "gen"]
    assert gen[0].data["duration"] >= 0.2


def test_burn_receives_the_exposed_windows_and_the_segment_offset(ctx, monkeypatch):
    """烧框拿到的是「露出来的窗口」+ 这一段的起点偏移,不是原始区间。"""
    probe: list = []
    _scene(ctx, mark_start=3.0, mark_duration=1.0)
    _run_pipeline(ctx, monkeypatch, probe)
    assert probe, "burn_annotations 必须被调到"
    call = probe[0]
    assert call["kw"]["time_offset"] == 0             # 原视频符合 Omni 要求，整段提交
    assert call["kw"]["canvas_w"] == 1280 and call["kw"]["canvas_h"] == 720
    ann = call["annotations"][0]
    assert ann["windows"] == [[3.0, 4.0]]             # 没被遮挡 → 一整段露着


def test_zero_padding_single_frame_mark_still_passes_schema(ctx, monkeypatch):
    """余量调成 0(paddingSec 允许为 0)+ 单帧标注 —— 只有「最短段地板」兜着。

    这正是那次真机报错的形态:结果层时长 0.17s,被 clip.duration 的 schema 下限
    (minimum 0.2)在整条链的最后一步拒掉,而合成、烧框、调服务全已白做。
    """
    _scene(ctx, mark_start=3.4, mark_duration=0.0, padding=0.0)
    _run_pipeline(ctx, monkeypatch)
    gen = [c for c in ctx.doc.list("clip")
           if not getattr(c, "deleted", False) and c.data.get("xKind") == "gen"]
    assert len(gen) == 1
    assert gen[0].data["duration"] >= 0.2, gen[0].data


def test_zero_padding_very_short_mark_also_passes_schema(ctx, monkeypatch):
    """余量 0 + 一条 0.05 秒的标注 —— 同样不许产出发不出去的段。"""
    _scene(ctx, mark_start=2.0, mark_duration=0.05, padding=0.0)
    _run_pipeline(ctx, monkeypatch)
    gen = [c for c in ctx.doc.list("clip")
           if not getattr(c, "deleted", False) and c.data.get("xKind") == "gen"]
    assert gen[0].data["duration"] >= 0.2, gen[0].data


def test_result_layer_is_named_after_the_mark_not_the_machine_prompt(ctx, monkeypatch):
    """结果图层的名字要是人写的那句话,不是自动拼的「1. 画面中的红色矩形框:…」。"""
    _scene(ctx, mark_start=3.0, mark_duration=1.0)
    _run_pipeline(ctx, monkeypatch)
    gen = [c for c in ctx.doc.list("clip")
           if not getattr(c, "deleted", False) and c.data.get("xKind") == "gen"]
    assert gen[0].name == "这块换成青草地", gen[0].name
    assert "矩形框" not in gen[0].name
