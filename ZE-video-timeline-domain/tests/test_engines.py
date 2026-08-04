import pytest

from ze_video_domain import (candidate_flow, capability_matrix, clip_math,
                             ffmpeg_ops, safety_rewrite, verify_report)
from ze_video_domain.errors import RequiresRuntime


# --- clip_math ---------------------------------------------------------------

def test_timeline_track_bounds():
    assert clip_math.is_timeline_track(0) and clip_math.is_timeline_track(63)
    assert not clip_math.is_timeline_track(64)
    assert not clip_math.is_timeline_track(True)  # bool rejected
    with pytest.raises(ValueError):
        clip_math.require_timeline_track(-1)


def test_clamp_mark_range_sorts_and_clamps():
    assert clip_math.clamp_mark_range(8, 2, 10) == (2.0, 8.0)
    assert clip_math.clamp_mark_range(-5, 99, 10) == (0.0, 10.0)
    assert clip_math.clamp_mark_range(None, 3, 10) == (None, 3.0)


def test_interval_union_and_pad():
    assert clip_math.interval_union([(2, 4), (1, 3), (3.5, 5)]) == (1.0, 5.0)
    assert clip_math.interval_union([]) is None
    assert clip_math.pad_interval((1.5, 1.5), 0.5, 8.0) == (1.0, 2.0)  # single-frame widened
    assert clip_math.pad_interval((0.2, 7.9), 0.5, 8.0) == (0.0, 8.0)  # clamped


def test_span_anchor_frames_uses_outer_edges():
    anc = clip_math.span_anchor_frames((6.5, 18.5), total_duration=24.0, fps=30.0)
    assert anc["head"] == pytest.approx(6.5 - 1 / 30, abs=1e-2)
    assert anc["tail"] == pytest.approx(18.5 + 1 / 30, abs=1e-2)
    edge = clip_math.span_anchor_frames((0.0, 24.0), 24.0, 30.0)
    assert edge["head"] is None and edge["tail"] is None


def test_safe_export_filename_guards_traversal():
    assert clip_math.safe_export_filename("../../etc/passwd") == "passwd.mp4"
    assert clip_math.safe_export_filename("") == "video.mp4"
    assert clip_math.safe_export_filename("clip.mov") == "clip.mp4"  # forces .mp4 ext


# --- capability_matrix -------------------------------------------------------

def test_alias_resolution():
    assert capability_matrix.canonical_model("omni") == "gemini-omni-flash-preview"
    assert capability_matrix.canonical_model("seedance") == "seedance-2.0"
    with pytest.raises(ValueError):
        capability_matrix.canonical_model("nope")


def test_only_omni_supports_edit():
    edit_models = capability_matrix.models_for_capability("edit")
    assert edit_models == ["gemini-omni-flash-preview"]
    assert capability_matrix.models_for_mode(1) == ["gemini-omni-flash-preview"]


def test_ref_token_per_family():
    assert capability_matrix.ref_token("seedance-2.0", 0) == "@file_0"
    assert capability_matrix.ref_token("grok-video", 0) == "@Image1"
    assert capability_matrix.ref_token("happyhorse-1.0", 1) == "[Image 2]"
    assert capability_matrix.ref_token("kling-v3-omni", 0, "video") == "<<<video_1>>>"
    assert capability_matrix.ref_token("veo-3.1", 0) is None  # natural language


def test_media_acceptance():
    assert capability_matrix.model_accepts_media("seedance-2.0", "audio")
    assert not capability_matrix.model_accepts_media("grok-video", "video")


# --- safety_rewrite ----------------------------------------------------------

def test_rewrite_known_name():
    res = safety_rewrite.rewrite("make Einstein wave hello")
    assert res["changed"] and "white-haired" in res["rewritten"]
    assert not res["blocked"]


def test_suspected_person_is_flagged_for_review_not_blocked():
    # 判断权保留:启发式命中只提示复核,不硬拦(否则任意英文大写双词都发不出去)
    res = safety_rewrite.rewrite("a shot of Taylor Swift dancing")
    assert res["flagged"] and res["review_required"]
    assert not res["blocked"]


def test_capitalised_bigram_does_not_block():
    res = safety_rewrite.rewrite("a Wooden Block in Night City")
    assert not res["blocked"] and res["flagged"]  # 提示但不拦


def test_explicit_unrewritable_name_blocks():
    # 确定事实才阻断:调用方把名字映射到空串 = 明示「无可用改写」
    res = safety_rewrite.rewrite("a shot of Taylor Swift dancing",
                                 name_map={"Taylor Swift": ""})
    assert res["blocked"] and res["unrewritable"] == ["taylor swift"]


def test_rewrite_longest_name_first_no_dangling():
    # "albert einstein" must win over the "einstein" substring — no dangling "Albert"
    res = safety_rewrite.rewrite("Albert Einstein explains relativity")
    assert res["changed"] and "white-haired" in res["rewritten"]
    assert "Albert" not in res["rewritten"]
    assert "einstein" not in res["rewritten"].lower()
    assert not res["blocked"] and not res["flagged"]  # fully rewritten -> not flagged


# --- candidate_flow ----------------------------------------------------------

def test_can_adopt_rejects_stale_and_empty():
    ok, _ = candidate_flow.can_adopt({"stale": True, "videoRef": {"sha256": "x"}})
    assert not ok
    ok, _ = candidate_flow.can_adopt({"videoRef": None})
    assert not ok
    ok, _ = candidate_flow.can_adopt({"videoRef": {"sha256": "abc"}})
    assert ok


def test_should_auto_adopt_only_empty_non_regen():
    assert candidate_flow.should_auto_adopt(target_has_video=False, is_regeneration=False)
    assert not candidate_flow.should_auto_adopt(target_has_video=True, is_regeneration=False)
    assert not candidate_flow.should_auto_adopt(target_has_video=False, is_regeneration=True)
    assert not candidate_flow.should_auto_adopt(
        target_has_video=False, is_regeneration=False, auto_adopt_enabled=False)


def test_mark_stale_for_target():
    cands = [
        {"id": "c1", "targetClipId": "clip-1"},
        {"id": "c2", "targetClipId": "clip-2"},
        {"id": "c3", "targetClipId": "clip-1", "stale": True},
        {"id": "c4", "targetClipId": "clip-1", "adopted": True},
    ]
    assert candidate_flow.mark_stale_for_target(cands, target_clip_id="clip-1") == ["c1"]


# --- verify_report -----------------------------------------------------------

def test_non_integrity_red_capped_to_yellow():
    check = verify_report.make_check(verify_report.CHECK_OUT_OF_BBOX, "red", "over threshold")
    assert check["verdict"] == "yellow"  # only integrity checks may be red


def test_duration_drift_may_be_red_and_blocks():
    checks = [
        verify_report.make_check(verify_report.CHECK_ANNOTATION_RESIDUE, "green"),
        verify_report.make_check(verify_report.CHECK_DURATION_DRIFT, "red", "fps mismatch"),
    ]
    report = verify_report.build_report("c-1", checks)
    assert report["verdict"] == "red" and report["adoptionAllowed"] is False


def test_yellow_allows_adoption():
    checks = [verify_report.make_check(verify_report.CHECK_OUT_OF_BBOX, "yellow")]
    report = verify_report.build_report("c-1", checks)
    assert report["verdict"] == "yellow" and report["adoptionAllowed"] is True


# --- ffmpeg_ops (pure argv + degradation) ------------------------------------

def test_argv_builders_are_lists_no_shell():
    assert ffmpeg_ops.build_frame_argv("ffmpeg", "v.mp4", 1.5, "o.png")[:2] == ["ffmpeg", "-y"]
    cut = ffmpeg_ops.build_cut_argv("ffmpeg", "s.mp4", 1.0, 3.0, "o.mp4")
    assert "-ss" in cut and "libx264" in cut
    merge = ffmpeg_ops.build_mask_merge_argv("ffmpeg", "orig.mp4", "cand.mp4", "m.png", "out.mp4")
    assert "alphamerge" in " ".join(merge) and "overlay=0:0" in " ".join(merge)
    assert "0:a?" in merge  # keeps original audio


def test_mask_argv_framing_preserving():
    argv = ffmpeg_ops.build_mask_merge_argv("ffmpeg", "o.mp4", "c.mp4", "m.png", "out.mp4")
    joined = " ".join(argv)
    assert "scale=" not in joined  # no rescale -> framing preserved


def test_require_tool_missing_raises_requires_runtime(monkeypatch):
    monkeypatch.setattr(ffmpeg_ops, "ffmpeg_bin", lambda: None)
    with pytest.raises(RequiresRuntime):
        ffmpeg_ops.require_tool("ffmpeg")
