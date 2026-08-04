"""图层合成:时间线是一摞图层,上层盖下层,只有可见的参与合成。"""
from __future__ import annotations

from ze_video_domain import layer_compose as lc


def _clip(cid, start, dur, layer, *, visible=True, source_in=0.0):
    return {"id": cid, "start": start, "duration": dur, "layer": layer,
            "visible": visible, "sourceIn": source_in, "assetId": f"a-{cid}"}


def test_only_visible_rows_take_part():
    clips = [_clip("c1", 0, 8, 0), _clip("c2", 0, 8, 1, visible=False)]
    assert [c["id"] for c in lc.visible_layers(clips)] == ["c1"]


def test_layers_sort_bottom_up():
    clips = [_clip("top", 0, 8, 5), _clip("bottom", 0, 8, 0), _clip("mid", 0, 8, 2)]
    assert [c["id"] for c in lc.visible_layers(clips)] == ["bottom", "mid", "top"]


def test_topmost_wins_at_a_moment():
    """上层盖下层 —— 那一刻看到的是最上面那个。"""
    clips = [_clip("orig", 0, 8, 0), _clip("edited", 2, 3, 1)]
    assert lc.topmost_at(clips, 1.0)["id"] == "orig"
    assert lc.topmost_at(clips, 3.0)["id"] == "edited"    # 2–5 被上层盖住
    assert lc.topmost_at(clips, 6.0)["id"] == "orig"
    assert lc.topmost_at(clips, 99.0) is None


def test_hiding_the_top_layer_reveals_the_original():
    """这就是「对比」—— 不需要专门做对比态,关掉眼睛就看到下面的原片。"""
    clips = [_clip("orig", 0, 8, 0), _clip("edited", 2, 3, 1, visible=False)]
    assert lc.topmost_at(clips, 3.0)["id"] == "orig"


# --- 合成计划 -----------------------------------------------------------------

def test_single_layer_span_is_one_cut():
    plan = lc.compose_plan([_clip("c1", 0, 8, 0)], 1.0, 4.0)
    assert plan["valid"] and len(plan["cuts"]) == 1
    cut = plan["cuts"][0]
    assert cut["clip_id"] == "c1"
    assert (cut["source_in"], cut["source_out"]) == (1.0, 4.0)


def test_source_in_offsets_into_the_material():
    """片段在时间线 0 起、但素材从第 10 秒切进来 —— 取的是素材内的 11–14 秒。"""
    plan = lc.compose_plan([_clip("c1", 0, 8, 0, source_in=10.0)], 1.0, 4.0)
    cut = plan["cuts"][0]
    assert (cut["source_in"], cut["source_out"]) == (11.0, 14.0)


def test_upper_layer_splits_the_span_into_three_cuts():
    """原片 0–8,上面盖了一段 3–5 的改过的片子 → 这一段要拼:原片/改过的/原片。"""
    clips = [_clip("orig", 0, 8, 0), _clip("edited", 3, 2, 1)]
    plan = lc.compose_plan(clips, 1.0, 7.0)
    assert plan["valid"]
    assert [(c["clip_id"], c["timeline_start"], c["timeline_end"]) for c in plan["cuts"]] == [
        ("orig", 1.0, 3.0), ("edited", 3.0, 5.0), ("orig", 5.0, 7.0)]


def test_hidden_upper_layer_does_not_split():
    clips = [_clip("orig", 0, 8, 0), _clip("edited", 3, 2, 1, visible=False)]
    plan = lc.compose_plan(clips, 1.0, 7.0)
    assert len(plan["cuts"]) == 1 and plan["cuts"][0]["clip_id"] == "orig"


def test_boundaries_that_do_not_change_the_top_layer_merge():
    """下面那层的起止会落下边界,但最上层始终是同一个 —— 不该因此切出碎段。"""
    clips = [_clip("orig", 0, 8, 5), _clip("under", 3, 2, 0)]   # under 在下面,盖不住
    plan = lc.compose_plan(clips, 1.0, 7.0)
    assert [c["clip_id"] for c in plan["cuts"]] == ["orig"]
    assert (plan["cuts"][0]["timeline_start"], plan["cuts"][0]["timeline_end"]) == (1.0, 7.0)


def test_gap_is_reported_not_silently_dropped():
    """时间线上有洞是允许的,但必须如实报 —— 拼出来会短一截,不能让人以为是连续的。"""
    clips = [_clip("a", 0, 2, 0), _clip("b", 5, 3, 0)]
    plan = lc.compose_plan(clips, 0.0, 8.0)
    assert plan["valid"]
    assert plan["gaps"] == [[2.0, 5.0]]
    assert [c["clip_id"] for c in plan["cuts"]] == ["a", "b"]


def test_empty_span_and_no_visible_layer_are_refused():
    assert not lc.compose_plan([_clip("c1", 0, 8, 0)], 3.0, 3.0)["valid"]
    hidden = [_clip("c1", 0, 8, 0, visible=False)]
    out = lc.compose_plan(hidden, 1.0, 4.0)
    assert not out["valid"] and "可见图层" in out["reason"]


# --- 新图层落在最上面 -----------------------------------------------------------

def test_new_layer_goes_on_top():
    """AI 回来的结果插最上层 —— 播放时自动看到最新的。"""
    assert lc.next_layer([]) == 0
    assert lc.next_layer([_clip("a", 0, 8, 0), _clip("b", 0, 8, 3)]) == 4
    # 隐藏的层也算进层序,否则新层会和它撞在一起
    assert lc.next_layer([_clip("a", 0, 8, 7, visible=False)]) == 8


# --- 区间内的标注 ---------------------------------------------------------------

def test_annotations_overlapping_the_span_are_picked_in_number_order():
    anns = [{"number": 2, "interval": [3.0, 4.0]},
            {"number": 1, "interval": [1.0, 2.0]},
            {"number": 9, "interval": [20.0, 21.0]}]
    got = lc.annotations_in_span(anns, 0.5, 5.0)
    assert [a["number"] for a in got] == [1, 2]


def test_single_frame_annotation_still_counts():
    anns = [{"number": 1, "interval": [2.0, 2.0]}]
    assert len(lc.annotations_in_span(anns, 1.0, 3.0)) == 1


# ── 一次发几段:各自加余量,加完挨着的才并 ─────────────────────────────

def _ann(n, a, b):
    return {"number": n, "label": f"第{n}处", "interval": [a, b],
            "bbox": {"x": 0, "y": 0, "w": 10, "h": 10}}


def test_far_apart_annotations_send_separately_not_as_one_long_span():
    """[1,2] 和 [25,26] 不许被吞成 [0.5,26.5] —— 那是 23 秒白发。"""
    groups = lc.send_groups([_ann(1, 1.0, 2.0), _ann(2, 25.0, 26.0)],
                                       padding_sec=0.5, total_duration=30.0)
    assert len(groups) == 2
    assert (groups[0]["start"], groups[0]["end"]) == (0.5, 2.5)
    assert (groups[1]["start"], groups[1]["end"]) == (24.5, 26.5)
    assert [g["numbers"] for g in groups] == [[1], [2]]


def test_nearby_annotations_merge_into_one_send():
    """[3,4] 与 [5,6] 加完余量首尾相接 → 它们在同一处,并成一段发。"""
    groups = lc.send_groups([_ann(1, 3.0, 4.0), _ann(2, 5.0, 6.0)],
                                       padding_sec=0.5, total_duration=10.0)
    assert len(groups) == 1
    assert (groups[0]["start"], groups[0]["end"]) == (2.5, 6.5)
    assert groups[0]["numbers"] == [1, 2]


def test_single_frame_annotation_is_stretched_open_by_padding():
    groups = lc.send_groups([_ann(1, 3.4, 3.4)],
                                       padding_sec=0.5, total_duration=10.0)
    assert (groups[0]["start"], groups[0]["end"]) == (2.9, 3.9)


def test_group_is_clamped_to_the_timeline_not_negative():
    groups = lc.send_groups([_ann(1, 0.2, 1.0)],
                                       padding_sec=0.5, total_duration=4.0)
    assert (groups[0]["start"], groups[0]["end"]) == (0.0, 1.5)


def test_no_annotations_means_nothing_to_send():
    assert lc.send_groups([], padding_sec=0.5, total_duration=10.0) == []


# ── 标注也受层序管:被上面的视频层盖住就不生效 ────────────────────────

def _vc(cid, layer, start, dur, visible=True):
    return {"id": cid, "layer": layer, "visible": visible, "start": start,
            "duration": dur, "sourceIn": 0.0, "videoRef": {"sha256": "x" * 64}}


def _mark(mid, layer, a, b, visible=True):
    return {"id": mid, "layer": layer, "visible": visible, "number": 1,
            "label": "改这块", "interval": [a, b],
            "bbox": {"x": 0, "y": 0, "w": 10, "h": 10}}


def test_annotation_under_a_video_layer_is_covered_and_does_not_count():
    """标注在第 0 层,一段视频在第 1 层盖住它 → 画面上看不见,发送时也不存在。"""
    clips = [_vc("原片", 0, 0.0, 8.0), _vc("生成层", 1, 2.0, 3.0)]
    marks = [_mark("m1", 0, 1.0, 6.0)]
    win = lc.exposed_windows(clips, marks, 0.0, 8.0)
    # 2–5s 被第 1 层盖住 → 只剩 1–2s 与 5–6s 两小段
    assert win["m1"] == [[1.0, 2.0], [5.0, 6.0]]


def test_annotation_above_the_covering_layer_stays_exposed():
    """同样的遮挡关系,把标注放到第 2 层(比生成层还高)→ 整段都露着。"""
    clips = [_vc("原片", 0, 0.0, 8.0), _vc("生成层", 1, 2.0, 3.0)]
    marks = [_mark("m1", 2, 1.0, 6.0)]
    assert lc.exposed_windows(clips, marks, 0.0, 8.0)["m1"] == [[1.0, 6.0]]


def test_fully_covered_annotation_disappears_entirely():
    clips = [_vc("原片", 0, 0.0, 8.0), _vc("盖住的", 5, 0.0, 8.0)]
    marks = [_mark("m1", 1, 1.0, 6.0)]
    assert "m1" not in lc.exposed_windows(clips, marks, 0.0, 8.0)


def test_hiding_the_covering_layer_brings_the_annotation_back():
    """关掉上面那层的眼睛,底下的标注就露出来了 —— 与画面一致。"""
    clips = [_vc("原片", 0, 0.0, 8.0), _vc("生成层", 1, 2.0, 3.0, visible=False)]
    marks = [_mark("m1", 0, 1.0, 6.0)]
    assert lc.exposed_windows(clips, marks, 0.0, 8.0)["m1"] == [[1.0, 6.0]]


def test_annotations_do_not_cover_each_other():
    """标注只是个框,不是满屏图像 —— 两条互不遮挡,都露着。"""
    clips = [_vc("原片", 0, 0.0, 8.0)]
    marks = [_mark("m1", 1, 1.0, 4.0), _mark("m2", 2, 1.0, 4.0)]
    win = lc.exposed_windows(clips, marks, 0.0, 8.0)
    assert win["m1"] == [[1.0, 4.0]] and win["m2"] == [[1.0, 4.0]]


def test_next_layer_counts_annotations_too():
    """同一根数轴 —— 新层不能跟标注撞层序。"""
    stack = [_vc("c", 0, 0.0, 4.0), _mark("m", 3, 1.0, 2.0)]
    assert lc.next_layer(stack) == 4


def test_send_groups_splits_on_windows_not_just_the_outer_interval():
    """一条标注被中间的视频层打断成两截露出来 → 两处独立的活,发两次。"""
    ann = [{"number": 1, "label": "改这块", "interval": [2.0, 6.0],
            "windows": [[2.0, 3.0], [5.0, 6.0]],
            "bbox": {"x": 0, "y": 0, "w": 10, "h": 10}}]
    groups = lc.send_groups(ann, padding_sec=0.5, total_duration=8.0)
    assert len(groups) == 2, groups
    assert (groups[0]["start"], groups[0]["end"]) == (1.5, 3.5)
    assert (groups[1]["start"], groups[1]["end"]) == (4.5, 6.5)


def test_every_group_is_at_least_min_span_long():
    """太短的段发不出去 —— clip.duration 的 schema 下限是 0.2,0.17 秒的结果层
    会在整条链最后一步被拒(合成、烧框、调服务全白做)。所以每段都撑到下限。"""
    tiny = [{"number": 1, "label": "一瞬", "interval": [1.0, 1.05],
             "bbox": {"x": 0, "y": 0, "w": 10, "h": 10}}]
    g = lc.send_groups(tiny, padding_sec=0.0, total_duration=10.0, min_span=0.5)[0]
    assert round(g["end"] - g["start"], 2) == 0.5
    assert g["start"] < 1.0 < g["end"]                     # 居中撑开


def test_min_span_at_the_very_end_of_the_timeline_is_pushed_back_not_past_the_end():
    tiny = [{"number": 1, "label": "贴边", "interval": [3.98, 4.0],
             "bbox": {"x": 0, "y": 0, "w": 10, "h": 10}}]
    g = lc.send_groups(tiny, padding_sec=0.0, total_duration=4.0, min_span=0.5)[0]
    assert g["end"] == 4.0 and g["start"] == 3.5
