from __future__ import annotations

from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "02-mod" / "05-code"))

import prompt_optimize
from sdk import CancelledError, ZeError


class _Ctx:
    def __init__(self):
        self.logs = []
        self._cancelled = threading.Event()

    def log(self, level, message, **kwargs):
        self.logs.append((level, message, kwargs))

    def checkpoint(self):
        if self._cancelled.is_set():
            raise CancelledError("cancelled")


def _payload(**overrides):
    base = {
        "optimizer_provider": "cursor_api_key",
        "optimizer_model": "grok-4.5",
        "target_model": "gemini-omni-flash-preview",
        "target_provider": "omni",
        "generation_strategy": "annotated_video",
        "ratio": "16:9",
        "duration": 6,
        "project_context": "海滩夕阳",
        "timeline_facts": [{"start": 0, "duration": 6}],
        "guide_facts": [{
            "kind": "box", "label": "加入海鸥", "interval": [1.0, 5.0],
            "keyframes": [{"time": 0.0}, {"time": 4.0}],
            "keyframe_origin": 1.0,
        }],
        "submission_windows": [{"start": 0.0, "end": 6.0, "full": True}],
        "frame_times": [1.0, 3.0],
        "clips": [],
        "assets_by_id": {},
        "canvas": [1280, 720],
        "fps": 30,
        "user_request": "让海鸥飞过",
    }
    base.update(overrides)
    return base


def test_select_frame_times_prefers_reference_then_keyframes():
    times = prompt_optimize.select_frame_times(
        reference_times=[2.5],
        guide_facts=[{
            "interval": [1.0, 5.0], "keyframe_origin": 1.0,
            "keyframes": [{"time": 0.0}, {"time": 2.0}, {"time": 4.0}],
        }],
        max_frames=3,
    )
    assert times == [2.5, 1.0, 3.0]


def test_select_frame_times_ignores_covered_keyframes():
    times = prompt_optimize.select_frame_times(
        guide_facts=[{
            "interval": [3.0, 5.0],
            "windows": [[3.0, 5.0]],
            "keyframe_origin": 1.0,
            "keyframes": [{"time": 0.0}, {"time": 3.0}],
        }],
    )

    assert times == [4.0]


def test_prepare_visible_context_uses_generated_video_and_cutout_image():
    source_ref = {"sha256": "source", "mime": "video/mp4", "ext": ".mp4"}
    generated_ref = {"sha256": "generated", "mime": "video/mp4", "ext": ".mp4"}
    raw_image = {"sha256": "raw-image", "mime": "image/png", "ext": ".png"}
    cutout_image = {"sha256": "cutout-image", "mime": "image/png", "ext": ".png"}
    clips, guides = prompt_optimize.prepare_visible_context(
        clips=[
            {
                "id": "source-clip", "assetId": "source-asset",
                "start": 0, "duration": 6, "layer": 0, "visible": True,
            },
            {
                "id": "generated-clip", "videoRef": generated_ref,
                "start": 1, "duration": 4, "layer": 2, "visible": True,
            },
        ],
        guides=[
            {
                "id": "covered", "kind": "box", "layer": 1,
                "interval": [1, 5], "visible": True,
            },
            {
                "id": "image-guide", "kind": "image", "layer": 3,
                "asset_id": "image-asset", "interval": [1, 5], "visible": True,
            },
        ],
        assets_by_id={
            "source-asset": {"assetRef": source_ref, "name": "原视频"},
            "image-asset": {
                "assetRef": raw_image, "processedRef": cutout_image, "name": "小船",
            },
        },
        total_duration=6,
    )

    assert clips[0]["videoRef"] == source_ref
    assert clips[1]["videoRef"] == generated_ref
    assert [guide["id"] for guide in guides] == ["image-guide"]
    assert guides[0]["image_ref"] == cutout_image
    assert guides[0]["asset_name"] == "小船"
    assert guides[0]["windows"] == [[1.0, 5.0]]


def test_submission_timing_translates_guide_facts_to_uploaded_video_time():
    guides = prompt_optimize.add_submission_timing(
        guides=[{
            "id": "moving-box",
            "interval": [10.0, 12.0],
            "windows": [[10.0, 12.0]],
            "keyframe_origin": 10.0,
            "keyframes": [{"time": 0.0}, {"time": 2.0}],
        }],
        submission_windows=[{
            "start": 9.0, "end": 13.0, "full": False,
        }],
    )

    assert guides[0]["submitted_intervals"] == [{
        "window": 1, "start": 1.0, "end": 3.0,
    }]
    assert guides[0]["submitted_keyframes"] == [
        {"window": 1, "at": 1.0},
        {"window": 1, "at": 3.0},
    ]


def test_reference_frame_renders_current_composite_and_visible_guides(
    monkeypatch, tmp_path,
):
    generated_ref = {"sha256": "generated", "mime": "video/mp4", "ext": ".mp4"}
    cutout_ref = {"sha256": "cutout", "mime": "image/png", "ext": ".png"}
    seen_plans = []
    seen_guides = []

    monkeypatch.setattr(prompt_optimize.ffmpeg_ops, "ffmpeg_bin", lambda: "/fake/ffmpeg")

    def fake_compose(plan, work, **kwargs):
        seen_plans.append(plan)
        assert kwargs["canvas"] == (1280, 720)
        out = Path(work) / "composed.mp4"
        out.write_bytes(b"video")
        return str(out)

    def fake_modified_frame(src, out, **kwargs):
        seen_guides.append(kwargs["guides"])
        assert kwargs["global_at"] == 2.0
        assert kwargs["canvas"] == (1280, 720)
        Path(out).write_bytes(b"png")
        return out

    monkeypatch.setattr(prompt_optimize.render, "compose_span", fake_compose)
    monkeypatch.setattr(
        prompt_optimize.guide_render, "render_modified_frame", fake_modified_frame,
    )

    paths, facts, warnings = prompt_optimize.extract_reference_frames(
        frame_times=[2.0],
        clips=[{
            "id": "generated-clip", "videoRef": generated_ref,
            "start": 1, "duration": 4, "layer": 2, "visible": True,
        }],
        assets_by_id={},
        guides=[{
            "id": "image-guide", "kind": "image", "layer": 3,
            "image_ref": cutout_ref, "interval": [1, 5],
            "windows": [[1, 5]], "visible": True,
        }],
        asset_store=object(),
        work=str(tmp_path),
        canvas=(1280, 720),
        fps=30,
    )

    assert len(paths) == 1
    assert facts == [{
        "at": 2.0,
        "file": "reference-0.png",
        "clipId": "generated-clip",
        "assetId": "",
        "composite": True,
    }]
    assert warnings == []
    assert seen_plans[0]["cuts"][0]["video_ref"] == generated_ref
    assert seen_guides[0][0]["image_ref"] == cutout_ref


def test_thin_guide_facts_drops_keyframes_and_bboxes():
    thinned = prompt_optimize.thin_guide_facts([{
        "number": 1,
        "kind": "box",
        "label": "海鸥",
        "color": "#ff3b30",
        "layer": 3,
        "bbox": {"x": 1, "y": 2, "w": 3, "h": 4},
        "interval": [1.0, 5.0],
        "submitted_intervals": [{"window": 1, "start": 1.0, "end": 5.0}],
        "keyframes": [{"time": 0.0, "bboxX": 10}],
        "submitted_keyframes": [{"window": 1, "at": 1.1}],
    }])
    assert thinned == [{
        "number": 1,
        "kind": "box",
        "label": "海鸥",
        "submitted_intervals": [{"window": 1, "start": 1.0, "end": 5.0}],
    }]


def test_prompt_optimize_success_passes_video_for_cursor_and_skips_frames(monkeypatch):
    seen = {}
    frame_calls = []

    monkeypatch.setattr(
        prompt_optimize,
        "prepare_optimizer_videos",
        lambda **_kwargs: (
            ["/tmp/optimizer-video-1.mp4"],
            [{"file": "optimizer-video-1.mp4", "window": 1, "duration": 6.0,
              "localStart": 0.0, "guidesRendered": True}],
        ),
    )

    def track_frames(**kwargs):
        frame_calls.append(kwargs)
        return ([], [], [])

    monkeypatch.setattr(prompt_optimize, "extract_reference_frames", track_frames)

    def fake_call(**kwargs):
        seen.update(kwargs)
        return {
            "optimized_prompt": "Keep the input video unchanged and add a seagull.",
            "warnings": [],
            "facts_used": ["工作区视频", "引导#1"],
        }

    monkeypatch.setattr(prompt_optimize, "user_llm_schema_call", fake_call)
    result = prompt_optimize.run_prompt_optimize_stage(_Ctx(), _payload())

    assert result["patches"]["status"] == "succeeded"
    assert "seagull" in result["patches"]["optimizedPrompt"]
    assert "自主分析视频×1" in result["patches"]["factsUsedJson"]
    assert frame_calls == []
    assert seen["video_paths"] == ["/tmp/optimizer-video-1.mp4"]
    assert seen["image_paths"] == []
    assert seen["timeout"] == 300
    assert isinstance(seen["cancel_event"], threading.Event)
    assert "optimizerProvider" not in result["patches"]


def _run_video_optimizer_cleanup(monkeypatch, tmp_path, provider: str):
    managed_root = tmp_path / "tool-data"
    monkeypatch.setenv("ZE_DATA_DIR", str(managed_root))
    monkeypatch.setattr(prompt_optimize.ffmpeg_ops, "ffmpeg_bin", lambda: "/fake/ffmpeg")

    def boom_frames(**_kwargs):
        raise AssertionError("video-first path must not extract reference frames")

    monkeypatch.setattr(prompt_optimize, "extract_reference_frames", boom_frames)

    def fake_compose(_plan, work, **_kwargs):
        out = Path(work) / "composed.mp4"
        out.write_bytes(b"composed")
        return str(out)

    def fake_burn(_src, out, _guides, **_kwargs):
        Path(out).write_bytes(b"video-with-guides")
        return out

    monkeypatch.setattr(prompt_optimize.render, "compose_span", fake_compose)
    monkeypatch.setattr(prompt_optimize.guide_render, "burn_guides", fake_burn)
    captured = {}

    def fake_call(**kwargs):
        captured.update(kwargs)
        paths = [Path(path) for path in kwargs["video_paths"]]
        assert len(paths) == 1 and paths[0].is_file()
        assert managed_root.resolve() in paths[0].resolve().parents
        return {
            "optimized_prompt": "Add a bird that follows the marked path.",
            "warnings": [],
            "facts_used": ["完整视频", "移动框"],
        }

    monkeypatch.setattr(prompt_optimize, "user_llm_schema_call", fake_call)
    source_ref = {"sha256": "source", "mime": "video/mp4", "ext": ".mp4"}
    payload = _payload(
        optimizer_provider=provider,
        optimizer_model="" if provider in {"codex_cli_profile", "claude_cli_profile"} else "grok-4.5",
        clips=[{
            "id": "source-clip",
            "videoRef": source_ref,
            "start": 0,
            "duration": 6,
            "layer": 0,
            "visible": True,
        }],
        guide_facts=[{
            "id": "moving-box",
            "kind": "box",
            "label": "加入飞行物",
            "layer": 1,
            "visible": True,
            "interval": [1.0, 5.0],
            "windows": [[1.0, 5.0]],
            "keyframes": [{"time": 0.0}, {"time": 4.0}],
            "keyframe_origin": 1.0,
        }],
    )

    result = prompt_optimize.run_prompt_optimize_stage(
        _Ctx(), payload, asset_store=object(),
    )

    assert result["patches"]["status"] == "succeeded"
    assert captured["cwd"] == Path(captured["video_paths"][0]).parent
    assert not Path(captured["video_paths"][0]).exists()
    assert not list((managed_root / "tmp" / "prompt-optimization").glob("opt-*"))


def test_prompt_optimize_codex_passes_managed_video_and_cleans_it(
    monkeypatch, tmp_path,
):
    _run_video_optimizer_cleanup(monkeypatch, tmp_path, "codex_cli_profile")


def test_prompt_optimize_claude_passes_managed_video_and_cleans_it(
    monkeypatch, tmp_path,
):
    _run_video_optimizer_cleanup(monkeypatch, tmp_path, "claude_cli_profile")


def test_prompt_optimize_cursor_passes_managed_video_and_cleans_it(
    monkeypatch, tmp_path,
):
    _run_video_optimizer_cleanup(monkeypatch, tmp_path, "cursor_api_key")


def _stub_cursor_video_prep(monkeypatch):
    monkeypatch.setattr(
        prompt_optimize,
        "prepare_optimizer_videos",
        lambda **_kwargs: (
            ["/tmp/optimizer-video-1.mp4"],
            [{"file": "optimizer-video-1.mp4", "window": 1, "duration": 6.0,
              "localStart": 0.0, "guidesRendered": True}],
        ),
    )
    monkeypatch.setattr(
        prompt_optimize,
        "extract_reference_frames",
        lambda **_kwargs: ([], [], []),
    )


def test_prompt_optimize_failure_keeps_structured_entity_error(monkeypatch):
    def fail(**_kwargs):
        raise ZeError("requires_credential", "请先绑定账号", context={})

    _stub_cursor_video_prep(monkeypatch)
    monkeypatch.setattr(prompt_optimize, "user_llm_schema_call", fail)
    result = prompt_optimize.run_prompt_optimize_stage(_Ctx(), _payload())

    assert result["patches"]["status"] == "failed"
    assert "绑定账号" in result["patches"]["lastError"]
    assert "optimizedPrompt" not in result["patches"]


def test_prompt_optimize_rejects_schema_mismatch_without_overwriting(monkeypatch):
    _stub_cursor_video_prep(monkeypatch)
    monkeypatch.setattr(
        prompt_optimize,
        "user_llm_schema_call",
        lambda **_kwargs: {"warnings": [], "facts_used": []},
    )

    result = prompt_optimize.run_prompt_optimize_stage(_Ctx(), _payload())

    assert result["patches"]["status"] == "failed"
    assert "结构化契约" in result["patches"]["lastError"]
    assert "optimizedPrompt" not in result["patches"]


def test_prompt_optimize_timeout_does_not_fallback_or_overwrite(monkeypatch):
    calls = []

    def timeout(**kwargs):
        calls.append((kwargs["provider"], kwargs["model"]))
        raise TimeoutError("provider timed out")

    _stub_cursor_video_prep(monkeypatch)
    monkeypatch.setattr(prompt_optimize, "user_llm_schema_call", timeout)
    result = prompt_optimize.run_prompt_optimize_stage(_Ctx(), _payload())

    assert calls == [("cursor_api_key", "grok-4.5")]
    assert result["patches"]["status"] == "failed"
    assert "TimeoutError" in result["patches"]["lastError"]
    assert "optimizedPrompt" not in result["patches"]


def test_prompt_optimize_model_unavailable_keeps_original(monkeypatch):
    def unavailable(**_kwargs):
        raise ZeError("model_call_failed", "所选模型不可用", context={})

    _stub_cursor_video_prep(monkeypatch)
    monkeypatch.setattr(prompt_optimize, "user_llm_schema_call", unavailable)
    result = prompt_optimize.run_prompt_optimize_stage(_Ctx(), _payload())

    assert result["patches"]["status"] == "failed"
    assert "模型不可用" in result["patches"]["lastError"]
    assert "optimizedPrompt" not in result["patches"]


def test_prompt_optimize_cancel_handler_sets_active_event():
    ctx = _Ctx()
    event = threading.Event()
    prompt_optimize._ACTIVE_CANCEL[id(ctx)] = event
    prompt_optimize.cancel_prompt_optimize(ctx)
    assert event.is_set()
    prompt_optimize._ACTIVE_CANCEL.pop(id(ctx), None)


def test_seedance_and_omni_render_distinct_target_facts():
    omni = _payload(
        video_facts=[{
            "file": "optimizer-video-1.mp4", "window": 1, "duration": 6.0,
            "localStart": 0.0, "guidesRendered": True,
            "timelineStart": 0.0, "timelineEnd": 6.0, "composite": True,
        }],
        guide_facts=[{
            "number": 1, "kind": "box", "label": "加入海鸥",
            "color": "#ff3b30", "layer": 3,
            "bbox": {"x": 1, "y": 2, "w": 3, "h": 4},
            "interval": [1.0, 5.0],
            "submitted_intervals": [{"window": 1, "start": 1.0, "end": 5.0}],
            "keyframes": [{"time": 0.0, "bboxX": 1744}],
            "submitted_keyframes": [{"window": 1, "at": 1.1}],
        }],
    )
    seedance = {
        **omni,
        "target_model": "seedance-2.0",
        "target_provider": "seedance",
    }

    _, omni_prompt = prompt_optimize.render_prompt(omni)
    _, seedance_prompt = prompt_optimize.render_prompt(seedance)

    assert "gemini-omni-flash-preview" in omni_prompt
    assert "目标提供方：omni" in omni_prompt
    assert "seedance-2.0" in seedance_prompt
    assert "目标提供方：seedance" in seedance_prompt
    assert "引导薄契约" in omni_prompt
    assert "工作区视频" in omni_prompt
    assert "optimizer-video-1.mp4" in omni_prompt
    assert "bboxX" not in omni_prompt
    assert "1744" not in omni_prompt
    assert "代表帧事实" not in omni_prompt
    assert omni_prompt != seedance_prompt