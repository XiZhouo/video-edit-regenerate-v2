"""send.dispatch stage 分类单测(依赖注入,无网络/无框架):
transport 错误→requires_runtime / 网关 failed→failed / 成功→候选(+空段自动采纳)。"""

from __future__ import annotations

import hashlib

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "02-mod" / "05-code"))

import dispatch_stages  # noqa: E402


class FakeCtx:
    def __init__(self):
        self.progress = []

    def log(self, *a, **k):
        pass

    def checkpoint(self):
        pass

    def set_progress(self, value, label=""):
        self.progress.append(value)


class FakeClient:
    """A configurable stand-in for _domain.videogen_client."""

    def __init__(self, *, submit_exc=None, submit_resp=None, poll_seq=None):
        self._submit_exc = submit_exc
        self._submit_resp = submit_resp or {"task_id": "T1"}
        self._poll_seq = list(poll_seq or [])

    def submit(self, **kwargs):
        if self._submit_exc:
            raise self._submit_exc
        return self._submit_resp

    def submit_files(self, **kwargs):
        self.last_files = kwargs
        if self._submit_exc:
            raise self._submit_exc
        return self._submit_resp

    def poll(self, task_id):
        return self._poll_seq.pop(0)

    # parse helpers delegate to the real module for fidelity
    task_id_of = staticmethod(dispatch_stages.videogen_client.task_id_of)
    status_of = staticmethod(dispatch_stages.videogen_client.status_of)
    result_url_of = staticmethod(dispatch_stages.videogen_client.result_url_of)
    error_message_of = staticmethod(dispatch_stages.videogen_client.error_message_of)

    def download(self, url):
        return b"video-bytes"


class FakeAssetStore:
    """够真的资产库:put 会真存下来,get 能取回 —— 渲染标注面要读原视频。"""

    def __init__(self):
        self.blobs: dict[str, bytes] = {}

    def put(self, data, ext=".mp4", mime=""):
        digest = hashlib.sha256(data).hexdigest()
        self.blobs[digest] = data
        store = self

        class _Ref:
            def to_dict(self):
                return {"sha256": digest, "ext": ext, "size": len(data),
                        "mime": mime or "video/mp4"}
        return _Ref()

    def get(self, sha256: str) -> bytes:
        return self.blobs[sha256]


BASE_ARGS = {"prompt": "把2改成3", "model": "gemini-omni-flash-preview", "mode": 3,
             "job_id": "job-1", "target_clip_id": "clip-1"}


def _run(client, args=None, **kw):
    return dispatch_stages.run_dispatch_stage(
        FakeCtx(), {**BASE_ARGS, **(args or {})}, client=client,
        sleeper=lambda s: None, now=_fake_now(), **kw)


def _fake_now():
    t = [0.0]

    def now():
        t[0] += 1.0
        return t[0]
    return now


def test_transport_error_is_requires_runtime():
    out = _run(FakeClient(submit_exc=OSError("connection refused")))
    assert out["patches"]["status"] == "requires_runtime"
    assert "RUNTIME" in out["patches"]["lastError"] or "不可达" in out["patches"]["lastError"]


def test_gateway_failed_is_failed():
    client = FakeClient(poll_seq=[{"status": "failed", "error": "内容被拒"}])
    out = _run(client)
    assert out["patches"]["status"] == "failed" and "内容被拒" in out["patches"]["lastError"]


def test_empty_prompt_is_failed():
    out = _run(FakeClient(), args={"prompt": ""})
    assert out["patches"]["status"] == "failed"


def test_success_creates_candidate_and_suggests_adopt_on_empty():
    """stage 只能 patch 本 WorkItem 实体 + 建实体(supervisor 契约),写不了目标 clip。
    所以「空段首结果自动采纳」= 候选带 autoAdoptSuggested + job 带建议提示,
    真正写正片仍走 candidate.adopt —— 不假装 stage 内已自动落地。"""
    client = FakeClient(poll_seq=[
        {"status": "generating", "progress": 0.5},
        {"status": "completed", "data": [{"url": "http://x/out.mp4"}]},
    ])
    store = FakeAssetStore()
    out = _run(client, args={"target_has_video": False, "auto_adopt_enabled": True},
               asset_store=store)
    assert out["patches"]["status"] == "succeeded"
    cand = out["created_entities"][0]
    assert cand["type"] == "candidate"
    assert cand["data"]["adopted"] is False           # 候选制:永不在 stage 里写正本
    assert cand["data"]["autoAdoptSuggested"] is True  # 但标了建议采纳
    assert cand["data"]["videoRef"]["sha256"] in store.blobs
    assert "pendingAdoptHint" in out["patches"]
    # 关键:绝不返回框架不认的自造键(会被 supervisor 静默丢弃)
    assert set(out) <= {"patches", "created_entities", "created_entities_on_conflict",
                        "name", "ai_provenance"}


def test_success_no_adopt_suggestion_when_target_has_video():
    client = FakeClient(poll_seq=[{"status": "completed", "data": [{"url": "http://x/out.mp4"}]}])
    out = _run(client, args={"target_has_video": True}, asset_store=FakeAssetStore())
    cand = out["created_entities"][0]
    assert cand["data"]["adopted"] is False
    assert cand["data"].get("autoAdoptSuggested") in (False, None)
    assert "pendingAdoptHint" not in out["patches"]


def test_success_without_asset_store_is_requires_runtime():
    client = FakeClient(poll_seq=[{"status": "completed", "data": [{"url": "http://x/out.mp4"}]}])
    out = _run(client)  # no asset_store -> cannot produce a valid asset_ref
    assert out["patches"]["status"] == "requires_runtime"


def test_poll_transport_error_is_requires_runtime():
    class FlakyPoll(FakeClient):
        def poll(self, task_id):
            raise TimeoutError("poll timed out")
    out = _run(FlakyPoll())
    assert out["patches"]["status"] == "requires_runtime"


def test_download_stores_asset_when_store_present():
    client = FakeClient(poll_seq=[{"status": "completed", "data": [{"url": "http://x/out.mp4"}]}])
    store = FakeAssetStore()
    out = _run(client, asset_store=store)
    assert out["created_entities"][0]["data"]["videoRef"]["sha256"] in store.blobs


# --- 模式一:标注面真的被做出来并发出去(从前这条链整条不存在)-------------------

import os          # noqa: E402
import shutil      # noqa: E402
import subprocess  # noqa: E402
import tempfile    # noqa: E402

import pytest      # noqa: E402

from _domain import ffmpeg_ops  # noqa: E402

_MODE1 = {"mode": 1, "send_interval": {"start": 1.0, "end": 3.0, "duration": 2.0},
          "annotations": [{"number": 1, "label": "把杯子换成花瓶",
                           "bbox": {"x": 40, "y": 30, "w": 120, "h": 90},
                           "interval": [1.0, 3.0]}]}


def _make_test_video(path: str, seconds: float = 5.0) -> None:
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                    "-i", f"testsrc=size=320x240:rate=24:duration={seconds}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", path],
                   capture_output=True, check=True)


def test_mode_one_without_source_video_says_so():
    store = FakeAssetStore()
    out = _run(FakeClient(submit_resp={"id": "t"}), {**_MODE1}, asset_store=store)
    assert out["patches"]["status"] == "failed"
    assert "缺原视频或标注" in out["patches"]["lastError"]


def test_mode_one_without_asset_store_is_requires_runtime():
    args = {**_MODE1, "source_video_ref": {"sha256": "a" * 64}}
    out = _run(FakeClient(submit_resp={"id": "t"}), args, asset_store=None)
    assert out["patches"]["status"] == "requires_runtime"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要本地 ffmpeg")
def test_mode_one_really_renders_a_cut_and_burned_segment():
    """真跑 ffmpeg:截出 2 秒 + 把标注框烧进画面,产物入库。

    不是 mock —— 这一条要证明的正是「那段视频真的被做出来了」,
    用假的 ffmpeg 证明不了任何东西。
    """
    store = FakeAssetStore()
    with tempfile.TemporaryDirectory() as work:
        src = os.path.join(work, "src.mp4")
        _make_test_video(src, 5.0)
        with open(src, "rb") as fh:
            source_ref = store.put(fh.read(), mime="video/mp4").to_dict()

        surface = dispatch_stages.render_ai_surface(
            {**_MODE1, "source_video_ref": source_ref}, store)

        assert surface is not None and surface["sha256"] in store.blobs
        burned = os.path.join(work, "burned.mp4")
        with open(burned, "wb") as fh:
            fh.write(store.get(surface["sha256"]))
        # 真是一段能被 ffprobe 读出来的视频,而且时长就是截出来的 2 秒
        assert abs(ffmpeg_ops.probe_duration(burned) - 2.0) < 0.35
        assert os.path.getsize(burned) > 0
        assert surface["sha256"] != source_ref["sha256"]      # 画面确实被改过


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要本地 ffmpeg")
def test_mode_one_sends_exactly_the_rendered_surface():
    """派发时发出去的就是刚渲染的那一段 —— 不多不少一个文件。"""
    store = FakeAssetStore()
    with tempfile.TemporaryDirectory() as work:
        src = os.path.join(work, "src.mp4")
        _make_test_video(src, 4.0)
        with open(src, "rb") as fh:
            source_ref = store.put(fh.read(), mime="video/mp4").to_dict()

        sent: dict = {}

        class _Recording(FakeClient):
            def submit_files(self, **kw):
                sent.update(kw)
                return {"id": "task-1"}

        client = _Recording(submit_resp={"id": "task-1"},
                            poll_seq=[{"status": "completed", "data": [{"url": "http://x/v.mp4"}]}])
        _run(client, {**_MODE1, "source_video_ref": source_ref}, asset_store=store)

        assert len(sent.get("file_paths") or []) == 1, sent
        assert sent["mode"] == "edit"
