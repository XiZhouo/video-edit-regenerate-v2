from __future__ import annotations

from io import BytesIO
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "02-mod" / "05-code"))

import cutout_stage
import generation_contract
import guide_render
import layer_edit_stage
from _domain import ffmpeg_ops, videogen_client


class _Ref:
    def __init__(self, sha: str):
        self.sha = sha

    def to_dict(self):
        return {"sha256": self.sha, "mime": "image/png", "ext": ".png"}


class _Store:
    def __init__(self, blobs=None):
        self.blobs = dict(blobs or {})
        self.puts = []

    def get(self, ref):
        key = ref if isinstance(ref, str) else getattr(ref, "sha256", "")
        return self.blobs[key]

    def put(self, data, **_kwargs):
        key = f"out-{len(self.puts)}"
        self.puts.append(bytes(data))
        self.blobs[key] = bytes(data)
        return _Ref(key)


class _Ctx:
    def log(self, *_args, **_kwargs):
        pass

    def checkpoint(self):
        pass


def _png_bytes() -> bytes:
    image = Image.new("RGBA", (80, 60), "white")
    for y in range(15, 45):
        for x in range(20, 60):
            image.putpixel((x, y), (220, 20, 30, 255))
    out = BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def test_provider_strategy_matrix_is_explicit():
    assert generation_contract.normalize_profile(
        provider="omni", model="gemini-omni-flash-preview",
        strategy="annotated_video",
    )["strategy"] == "annotated_video"
    assert generation_contract.service_mode("seedance", "modified_frames") == "reference-to-video"
    generation_contract.validate_profile(
        "omni", "gemini-omni-flash-preview", "modified_frames")
    assert generation_contract.service_mode("omni", "modified_frames") == "edit"


def test_omni_submission_omits_unsupported_fields_and_limits_input_span():
    request = generation_contract.compile_submission_request(
        provider="omni",
        model="gemini-omni-flash-preview",
        strategy="annotated_video",
        ratio="16:9",
        span_duration=10.0,
    )
    assert request == {"mode": "edit"}
    # 模拟 layer_edit_stage: submit_files(**compile_submission_request(...))
    # 不得靠客户端默认值把 aspect_ratio 补回去。
    fields = dict(videogen_client.build_multipart_fields(
        model="gemini-omni-flash-preview",
        prompt="Edit the input video",
        **request,
    ))
    assert "aspect_ratio" not in fields
    assert "duration" not in fields

    with pytest.raises(ValueError, match="Omni.*10"):
        generation_contract.compile_submission_request(
            provider="omni",
            model="gemini-omni-flash-preview",
            strategy="annotated_video",
            ratio="16:9",
            span_duration=10.01,
        )


def test_seedance_submission_keeps_ratio_and_clamps_output_duration():
    short = generation_contract.compile_submission_request(
        provider="seedance",
        model="seedance-2.0-fast",
        strategy="modified_frames",
        ratio="9:16",
        span_duration=1.2,
    )
    assert short == {
        "mode": "reference-to-video",
        "ratio": "9:16",
        "duration": 4,
    }
    whole_source = generation_contract.compile_submission_request(
        provider="seedance",
        model="seedance-2.0-fast",
        strategy="annotated_video",
        ratio="16:9",
        span_duration=6.08,
    )
    assert whole_source["duration"] == 7

    with pytest.raises(ValueError, match="Seedance.*15"):
        generation_contract.compile_submission_request(
            provider="seedance",
            model="seedance-2.0-fast",
            strategy="modified_frames",
            ratio="9:16",
            span_duration=15.01,
        )


def test_submission_uses_the_full_video_when_it_fits_the_model_window():
    groups = generation_contract.plan_submission_groups(
        provider="seedance",
        total_duration=6.08,
        focus_groups=[{"start": 0.5, "end": 5.5, "numbers": [1]}],
    )

    assert groups == [{
        "start": 0.0,
        "end": 6.08,
        "numbers": [1],
        "full": True,
    }]


def test_long_video_submission_expands_the_focus_to_a_valid_whole_second_span():
    seedance = generation_contract.plan_submission_groups(
        provider="seedance",
        total_duration=30.0,
        focus_groups=[{"start": 9.7, "end": 12.6, "numbers": [1]}],
    )
    omni = generation_contract.plan_submission_groups(
        provider="omni",
        total_duration=30.0,
        focus_groups=[{"start": 9.7, "end": 12.6, "numbers": [1]}],
    )

    assert seedance == [{
        "start": 9.0,
        "end": 13.0,
        "numbers": [1],
        "full": False,
    }]
    assert omni == [{
        "start": 9.0,
        "end": 13.0,
        "numbers": [1],
        "full": False,
    }]
    assert seedance[0]["end"] - seedance[0]["start"] == 4
    assert omni[0]["end"] - omni[0]["start"] == 4


def test_submission_rejects_a_focus_that_cannot_fit_in_one_model_window():
    with pytest.raises(ValueError, match="15"):
        generation_contract.plan_submission_groups(
            provider="seedance",
            total_duration=40.0,
            focus_groups=[{"start": 5.0, "end": 21.0, "numbers": [1]}],
        )


def test_seedance_rejects_a_source_shorter_than_its_minimum_window():
    with pytest.raises(ValueError, match="至少需要 4"):
        generation_contract.plan_submission_groups(
            provider="seedance",
            total_duration=3.5,
            focus_groups=[{"start": 1.0, "end": 2.0, "numbers": [1]}],
        )


def test_multipart_upload_rejects_a_file_over_20mb_before_network(tmp_path, monkeypatch):
    oversized = tmp_path / "too-large.mp4"
    oversized.write_bytes(b"x")
    with oversized.open("r+b") as fh:
        fh.truncate(videogen_client.UPLOAD_MAX_BYTES + 1)
    called = False

    def fail_if_called(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("oversized upload must fail before network")

    monkeypatch.setattr(videogen_client, "_urlopen", fail_if_called)
    with pytest.raises(videogen_client.SubmissionValidationError, match="20MB"):
        videogen_client.submit_files(
            model="seedance-2.0-fast",
            mode="reference-to-video",
            prompt="@file_0",
            file_paths=[str(oversized)],
            ratio="16:9",
            duration=4,
        )
    assert called is False


def test_upload_size_limit_is_per_file_not_whole_request(tmp_path):
    paths = []
    for idx in range(2):
        path = tmp_path / f"part-{idx}.mp4"
        path.write_bytes(b"x")
        with path.open("r+b") as fh:
            fh.truncate(11 * 1024 * 1024)
        paths.append(str(path))
    videogen_client.validate_upload_size(paths)


def test_burned_box_uses_the_color_selected_on_canvas():
    argv = ffmpeg_ops.build_burn_annotations_argv(
        "/fake/ffmpeg", "source.mp4", "out.mp4",
        [{
            "number": 2,
            "color": "#000000",
            "bbox": {"x": 10, "y": 20, "w": 100, "h": 80},
        }],
    )
    assert "color=black@1.0" in argv[argv.index("-vf") + 1]


def test_seedance_modified_frame_prompt_names_every_uploaded_file():
    prompt = generation_contract.decorate_prompt(
        "让物体沿路径移动",
        provider="seedance",
        strategy="modified_frames",
        frame_times=[1.25, 2.5],
    )
    assert "@file_0" in prompt
    assert "@file_1 对应原视频 1.250s" in prompt
    assert "@file_2 对应原视频 2.500s" in prompt


def test_render_overlay_supports_box_text_image_and_arrow(tmp_path):
    store = _Store({"image": _png_bytes()})
    guides = [
        {"kind": "box", "layer": 1, "label": "生成物体", "color": "#ff3b30",
         "bbox": {"x": 10, "y": 10, "w": 60, "h": 40}},
        {"kind": "text", "layer": 2, "label": "从这里出发", "color": "#32d0d8",
         "bbox": {"x": 20, "y": 70, "w": 100, "h": 30}},
        {"kind": "image", "layer": 3, "label": "我的素材", "color": "#ffd60a",
         "image_ref": {"sha256": "image"},
         "bbox": {"x": 120, "y": 20, "w": 70, "h": 70}},
        {"kind": "arrow", "layer": 4, "label": "移动方向", "color": "#32d74b",
         "points": [{"x": 30, "y": 100}, {"x": 180, "y": 110}],
         "bbox": {"x": 30, "y": 100, "w": 150, "h": 10}},
    ]
    path = guide_render.render_overlay_png(
        guides, str(tmp_path / "overlay.png"), asset_store=store, canvas=(240, 140))
    result = Image.open(path).convert("RGBA")
    assert result.size == (240, 140)
    assert result.getbbox() is not None


def test_active_guides_resolve_moving_box_at_requested_time():
    guide = {
        "kind": "box",
        "bbox": {"x": 9, "y": 8, "w": 7, "h": 6},
        "interval": [1.0, 3.0],
        "keyframes": [
            {"time": 0, "bboxX": 0, "bboxY": 10, "bboxW": 100, "bboxH": 50},
            {"time": 2, "bboxX": 200, "bboxY": 110, "bboxW": 140, "bboxH": 70},
        ],
    }
    resolved = guide_render.active_guides([guide], at=2.0, fps=30)
    assert resolved[0]["bbox"] == {"x": 100, "y": 60, "w": 120, "h": 60}


def test_cutout_keeps_original_available_and_writes_processed_ref():
    store = _Store({"source": _png_bytes()})
    result = cutout_stage.run_cutout_stage(
        _Ctx(), {"asset_ref": {"sha256": "source"}}, asset_store=store)
    assert result["patches"]["cutoutStatus"] == "succeeded"
    assert result["patches"]["processedRef"]["sha256"].startswith("out-")
    assert store.puts


def test_modified_frames_sends_clean_video_then_all_edited_frames(monkeypatch, tmp_path):
    captured = {}

    def fake_compose(_store, _plan, work, **_kwargs):
        path = Path(work) / "clean.mp4"
        path.write_bytes(b"clean")
        return str(path)

    def fake_frame(_src, out, **_kwargs):
        Path(out).write_bytes(b"frame")
        return out

    class Client:
        @staticmethod
        def submit_files(**kwargs):
            captured.update(kwargs)
            return {"status": "completed", "url": "memory://result"}

        @staticmethod
        def task_id_of(_resp):
            return ""

        @staticmethod
        def result_url_of(resp):
            return resp.get("url")

        @staticmethod
        def error_message_of(_resp):
            return ""

        @staticmethod
        def download(_url):
            return b"generated-video"

    monkeypatch.setattr(layer_edit_stage, "compose_segment", fake_compose)
    monkeypatch.setattr(layer_edit_stage.ffmpeg_ops, "ffmpeg_bin", lambda: "/fake/ffmpeg")
    monkeypatch.setattr(layer_edit_stage.guide_render, "render_modified_frame", fake_frame)
    store = _Store()
    result = layer_edit_stage.run_edit_stage(
        _Ctx(),
        {
            "plan": {"valid": True, "span": [1.0, 3.0]},
            "prompt": "让物体移动",
            "provider": "seedance",
            "strategy": "modified_frames",
            "model": "seedance-2.0-fast",
            "annotations": [{"kind": "arrow", "label": "向右"}],
            "reference_times": [1.25, 2.5],
            "ratio": "16:9",
            "canvas": [320, 180],
            "fps": 24,
            "new_layer": 3,
        },
        asset_store=store,
        client=Client(),
        sleeper=lambda _seconds: None,
        poll_interval=0,
        poll_deadline=1,
    )
    assert result["patches"]["status"] == "succeeded"
    assert captured["mode"] == "reference-to-video"
    assert captured["ratio"] == "16:9"
    assert captured["duration"] == 4
    assert len(captured["file_paths"]) == 3
    assert Path(captured["file_paths"][0]).name == "clean.mp4"
    assert "@file_2" in captured["prompt"]
    assert "0.250s" in captured["prompt"]
    assert "1.500s" in captured["prompt"]
    assert "2.500s" not in captured["prompt"]


def test_real_ffmpeg_burns_text_image_and_arrow_with_their_time_range(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg unavailable")
    source = tmp_path / "source.mp4"
    subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-f", "lavfi",
         "-i", "color=c=navy:s=320x180:r=24:d=1",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)],
        check=True,
    )
    store = _Store({"image": _png_bytes()})
    guides = [
        {"kind": "text", "label": "这里生成", "color": "#ff3b30", "layer": 1,
         "bbox": {"x": 10, "y": 20, "w": 100, "h": 30}, "interval": [0.1, 0.8]},
        {"kind": "image", "label": "素材", "color": "#ffd60a", "layer": 2,
         "image_ref": {"sha256": "image"},
         "bbox": {"x": 120, "y": 40, "w": 80, "h": 70}, "interval": [0.2, 0.9]},
        {"kind": "arrow", "label": "向右", "color": "#32d74b", "layer": 3,
         "points": [{"x": 40, "y": 140}, {"x": 260, "y": 140}],
         "bbox": {"x": 40, "y": 135, "w": 220, "h": 10}, "interval": [0.0, 1.0]},
    ]
    out = tmp_path / "guided.mp4"
    guide_render.burn_guides(
        str(source), str(out), guides, asset_store=store, work=str(tmp_path),
        canvas=(320, 180), time_offset=0, fps=24,
    )
    assert out.is_file() and out.stat().st_size > 1000


def test_real_ffmpeg_burns_a_box_at_interpolated_positions(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe unavailable")
    source = tmp_path / "source.mp4"
    subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-f", "lavfi",
         "-i", "color=c=navy:s=320x180:r=20:d=1",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)],
        check=True,
    )
    guide = {
        "kind": "box", "label": "", "color": "#ff3b30", "layer": 1,
        "bbox": {"x": 20, "y": 50, "w": 40, "h": 40}, "interval": [0.0, 1.0],
        "keyframe_origin": 0.0,
        "keyframes": [
            {"time": 0.0, "bboxX": 20, "bboxY": 50, "bboxW": 40, "bboxH": 40},
            {"time": 0.8, "bboxX": 240, "bboxY": 50, "bboxW": 40, "bboxH": 40},
        ],
    }
    out = tmp_path / "moving.mp4"
    guide_render.burn_guides(
        str(source), str(out), [guide], asset_store=_Store(), work=str(tmp_path),
        canvas=(320, 180), time_offset=0, fps=20,
    )
    early = tmp_path / "early.png"
    late = tmp_path / "late.png"
    for at, frame in ((0.04, early), (0.76, late)):
        subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-ss", str(at), "-i", str(out),
             "-frames:v", "1", str(frame)],
            check=True,
        )

    def has_red_near(path, x0):
        image = Image.open(path).convert("RGB")
        return any(
            r > 150 and r > g * 1.4 and r > b * 1.4
            for x in range(max(0, x0 - 8), min(image.width, x0 + 9))
            for y in range(42, 98)
            for r, g, b in [image.getpixel((x, y))]
        )

    # ffmpeg 在 20fps 下会取到最接近的下一帧：约 0.05s 与 0.80s。
    assert has_red_near(early, 34)
    assert has_red_near(late, 240)
    assert not has_red_near(late, 20)
