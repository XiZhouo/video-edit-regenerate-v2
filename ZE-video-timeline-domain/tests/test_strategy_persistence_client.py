import json

import pytest

from ze_video_domain import ffmpeg_ops, videogen_client
from ze_video_domain.persistence import atomic, task_recovery
from ze_video_domain.strategy import runner


# --- strategy runner ---------------------------------------------------------

def _valid_output():
    return {
        "strategyId": "general-v1",
        "workflow": {"adType": "通用", "hook": "h", "suggestedMode": "text",
                     "suggestedDuration": 5, "assetRoles": [], "riskChecks": [],
                     "promptPlan": []},
        "optimizedPrompt": "最终提示词",
        "notes": [], "warnings": [],
    }


def test_strategies_load_and_list():
    ids = {s["id"] for s in runner.list_strategies()}
    assert {"general-v1", "ad-short", "video-edit-v1"} <= ids


def test_render_user_template():
    strat = runner.load_strategy("general-v1")
    out = runner.render_user_template(strat, {"mode": "text", "ratio": "16:9",
                                              "duration": 8, "requirements": "走进集市"})
    assert "text" in out and "走进集市" in out and "{mode}" not in out


def test_validate_output_rejects_missing_keys():
    ok, errs = runner.validate_output({"strategyId": "x"})
    assert not ok and errs


def test_video_edit_strategy_uses_compact_output_contract():
    strategy = runner.load_strategy("video-edit-v1")
    assert runner.output_schema_for(strategy)["required"] == [
        "optimized_prompt", "warnings", "facts_used",
    ]
    ok, errors = runner.validate_output(
        {"optimized_prompt": "让海鸥沿轨迹飞行", "warnings": [], "facts_used": ["红框轨迹"]},
        strategy,
    )
    assert ok and not errors


def test_user_guide_palette_maps_ui_hex_to_prompt_color():
    assert ffmpeg_ops.annotation_paint({"color": "#000000"})[:2] == ("black", "黑色")
    assert ffmpeg_ops.annotation_paint({"color": "#ffffff"})[:2] == ("white", "白色")
    assert ffmpeg_ops.annotation_paint({"color": "#ff3b30"})[:2] == ("red", "红色")


def test_run_strategy_uses_declared_provider():
    res = runner.run_strategy("general-v1", {"requirements": "x"},
                              providers={"codex-skill": lambda s, f: _valid_output()})
    assert res["provider"] == "codex-skill" and res["output"]["optimizedPrompt"] == "最终提示词"


def test_run_strategy_falls_back_to_gateway_then_skip():
    # codex unavailable (returns None) -> gateway -> ok
    res = runner.run_strategy(
        "general-v1", {"requirements": "x"},
        providers={"codex-skill": lambda s, f: None, "gateway": lambda s, f: _valid_output()})
    assert res["provider"] == "gateway"
    # no providers -> skip (degradation, not failure)
    res2 = runner.run_strategy("general-v1", {"requirements": "x"}, providers={})
    assert res2["provider"] == "skip" and res2["output"] is None and res2["warnings"]


def test_run_strategy_invalid_output_degrades():
    res = runner.run_strategy(
        "general-v1", {}, providers={"codex-skill": lambda s, f: {"bad": 1}})
    assert res["provider"] == "skip"  # invalid output rejected, chain exhausted


# --- persistence -------------------------------------------------------------

def test_atomic_roundtrip(tmp_path):
    p = str(tmp_path / "nested" / "doc.json")
    atomic.write_json_atomic(p, {"a": 1, "中文": True})
    assert atomic.read_json(p) == {"a": 1, "中文": True}
    assert atomic.read_json(str(tmp_path / "missing.json"), default=[]) == []


def test_task_save_scan_cleanup(tmp_path):
    cache = str(tmp_path)
    task_recovery.save_task(cache, {"taskKey": "t1", "status": "dispatched"}, now_ms=1)
    task_recovery.save_task(cache, {"taskKey": "t2", "status": "succeeded"}, now_ms=1)
    task_recovery.save_task(cache, {"taskKey": "t3", "status": "dispatched",
                                    "localRef": "asset://x"}, now_ms=1)
    resumable = {r["taskKey"] for r in task_recovery.scan_resumable(cache)}
    assert resumable == {"t1"}  # t2 terminal, t3 downloaded
    assert task_recovery.is_terminal("failed") and not task_recovery.is_terminal("dispatched")


def test_cleanup_old_tasks_by_mtime(tmp_path):
    cache = str(tmp_path)
    task_recovery.save_task(cache, {"taskKey": "old", "status": "dispatched"}, now_ms=1)
    import os
    path = task_recovery.task_path(cache, "old")
    os.utime(path, (0, 0))  # ancient mtime
    removed = task_recovery.cleanup_old_tasks(cache, now=10_000_000, retention_seconds=100)
    assert "old" in removed


# --- videogen client ---------------------------------------------------------

def test_build_submit_body_clamps_duration():
    body = videogen_client.build_submit_body(model="seedance-2.0", mode="text-to-video",
                                              prompt="p", duration=99)
    assert body["duration"] == 15 and body["model"] == "seedance-2.0"
    # 统一服务的参数表里没有 async(那是旧 jimeng 网关的);不发文档里没有的字段。
    assert "async" not in body


def test_duration_clamped_only_for_seedance():
    # non-Seedance models keep their own duration (not forced into Seedance's 4-15 window)
    veo = videogen_client.build_submit_body(model="veo-3.1", mode="text-to-video",
                                            prompt="p", duration=8)
    assert veo["duration"] == 8
    # veo 只收 4/6/8 三档 —— 7 秒取最近一档,而不是原样发出去被服务拒
    assert videogen_client.build_submit_body(model="veo-3.1", mode="text-to-video",
                                             prompt="p", duration=7)["duration"] in (6, 8)
    kling = videogen_client.build_submit_body(model="kling-v3", mode="text-to-video",
                                              prompt="p", duration=3)
    assert kling["duration"] == 3  # below Seedance min, but valid for kling


def test_parse_helpers():
    assert videogen_client.task_id_of({"task_id": "abc"}) == "abc"
    assert videogen_client.status_of({"status": "completed"}) == "completed"
    assert videogen_client.result_url_of({"data": [{"url": "http://x/v.mp4"}]}) == "http://x/v.mp4"


def test_download_rejects_bad_scheme():
    with pytest.raises(ValueError):
        videogen_client.download("file:///etc/passwd")


def test_submit_raises_on_transport_error(monkeypatch):
    monkeypatch.setenv("ZE_VIDEOGEN_BASE_URL", "http://test.local")

    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(videogen_client, "_urlopen", boom)
    with pytest.raises(OSError):
        videogen_client.submit(model="seedance-2.0", mode="text-to-video", prompt="p")


def test_base_url_unconfigured_raises_connection_error(monkeypatch):
    for var in videogen_client.BASE_URL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ConnectionError):
        videogen_client.base_url()


def test_clamp_duration_bad_input():
    assert videogen_client.clamp_duration("nope") == 10
    assert videogen_client.clamp_duration(3) == 4  # below min


def test_endpoint_paths_match_the_service_contract(monkeypatch):
    """路径钉死。服务 /discover 声明:提交 POST /v1/videos/generations,
    查询 GET /v1/videos/:id —— **查询不带 generations**。

    这条是打真服务才发现的(单测里 _urlopen 是假的,拼错的 URL 永远不会 404)。
    """
    monkeypatch.setenv("ZE_VIDEOGEN_BASE_URL", "http://svc")
    seen = []

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"{}"

    def fake_urlopen(req, timeout=0):
        seen.append((req.get_method(), req.full_url))
        return _Resp()

    monkeypatch.setattr(videogen_client, "_urlopen", fake_urlopen)
    videogen_client.submit(model="m", mode="text-to-video", prompt="p")
    videogen_client.poll("T1")
    assert seen[0] == ("POST", "http://svc/v1/videos/generations")
    assert seen[1] == ("GET", "http://svc/v1/videos/T1")


# 实测真响应(2026-07-26,seedance-2.0-fast 文生视频)。凭空猜形状的代价:
# 任务 completed 却取不到 URL,当场崩在 download 里。这份夹具是真服务给的原文。
_REAL_COMPLETED = {
    "id": "eVmTTbC0jVKev0E3w9G7S", "model": "seedance-2.0-fast", "status": "completed",
    "mode": "text-to-video", "prompt": "一只橘猫…", "source": "direct",
    "created_at": "2026-07-26T03:58:30.376Z", "completed_at": "2026-07-26T04:00:40.396Z",
    "video_url": "http://10.0.0.6:5003/jimeng/2026/07/26/xxx.mp4",
}


def test_result_url_reads_the_real_completed_shape():
    assert videogen_client.status_of(_REAL_COMPLETED) == "completed"
    assert videogen_client.result_url_of(_REAL_COMPLETED).endswith("xxx.mp4")
    assert videogen_client.task_id_of(_REAL_COMPLETED) == "eVmTTbC0jVKev0E3w9G7S"


def test_result_url_still_tolerates_the_legacy_shapes():
    assert videogen_client.result_url_of({"data": [{"url": "http://a/1.mp4"}]}) == "http://a/1.mp4"
    assert videogen_client.result_url_of({"data": {"url": "http://a/2.mp4"}}) == "http://a/2.mp4"
    assert videogen_client.result_url_of({"status": "processing"}) is None


def test_multipart_parts_carry_the_real_mime():
    """服务靠**每个 part 的 Content-Type** 认这是图还是视频。

    一律写 application/octet-stream 会被判成
    `File at index 0 has unrecognizable type. edit requires: image, video.`
    —— 实测 400。这条是花钱打真服务才发现的。
    """
    assert videogen_client.mime_for("/x/a.mp4") == "video/mp4"
    assert videogen_client.mime_for("/x/b.PNG") == "image/png"
    assert videogen_client.mime_for("/x/c.wav") == "audio/wav"

    import os
    import tempfile
    with tempfile.TemporaryDirectory() as work:
        vid = os.path.join(work, "clip.mp4")
        open(vid, "wb").write(b"\x00\x01")
        body = videogen_client.build_multipart(
            [("model", "m"), ("mode", "edit")], [vid], "BOUND")
    text = body.decode("latin-1")
    assert 'name="file[]"; filename="clip.mp4"' in text
    assert "Content-Type: video/mp4" in text
    assert "application/octet-stream" not in text
