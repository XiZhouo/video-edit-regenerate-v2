import pytest

from ze_video_domain import send_compiler


def _ann(n, label, bbox=None):
    return {"number": n, "label": label, "bbox": bbox or {"x": 100, "y": 50, "w": 80, "h": 60}}


# --- naming reference (② / ④) ------------------------------------------------

def test_naming_reference_seedance_tokens_and_order():
    template = "参考{运镜参考}的节奏，让{角色图}走进{场景图}"
    assets = [
        {"name": "运镜参考", "media_type": "video"},
        {"name": "角色图", "media_type": "image"},
        {"name": "场景图", "media_type": "image"},
    ]
    r = send_compiler.compile_naming_reference(template, assets, "seedance-2.0")
    assert "@file_0" in r["prompt"] and "@file_1" in r["prompt"] and "@file_2" in r["prompt"]
    assert [f["name"] for f in r["file_order"]] == ["运镜参考", "角色图", "场景图"]
    assert r["lint_verdict"] == "green"


def test_naming_reference_grok_1indexed():
    r = send_compiler.compile_naming_reference(
        "{角色}在场景里", [{"name": "角色", "media_type": "image"}], "grok-video")
    assert "@Image1" in r["prompt"]


def test_missing_reference_is_red():
    r = send_compiler.compile_naming_reference(
        "用{不存在}生成", [{"name": "角色", "media_type": "image"}], "seedance-2.0")
    assert r["lint"]["missing"] == ["不存在"] and r["lint_verdict"] == "red"


def test_unreferenced_asset_is_yellow():
    r = send_compiler.compile_naming_reference(
        "只用{角色}", [{"name": "角色", "media_type": "image"},
                      {"name": "闲置", "media_type": "image"}], "seedance-2.0")
    assert r["lint"]["unreferenced"] == ["闲置"] and r["lint_verdict"] == "yellow"


def test_incompatible_media_dropped_and_flagged():
    # veo takes only image refs -> a video ref is incompatible
    r = send_compiler.compile_naming_reference(
        "参考{运镜}的节奏", [{"name": "运镜", "media_type": "video"}], "veo-3.1")
    assert r["lint"]["incompatible"] and r["lint_verdict"] == "yellow"


# --- edit modes (①) ----------------------------------------------------------

def test_mode_one_prompt_enumerates_removal():
    prompt = send_compiler.compile_edit_mode_one(
        [_ann(1, "把2改成3"), _ann(2, "移除水印")], 1920, 1080)
    assert "移除全部标注元素" in prompt
    assert "①" in prompt and "②" in prompt
    assert "其余一切与原片保持一致" in prompt


def test_describe_region_grid():
    assert send_compiler.describe_region({"x": 0, "y": 0, "w": 10, "h": 10}, 1920, 1080) == "画面上左"
    assert send_compiler.describe_region({"x": 900, "y": 500, "w": 10, "h": 10}, 1920, 1080) == "画面中央"


# --- top-level dispatcher ----------------------------------------------------

def test_compile_send_mode_one_ok_with_omni():
    out = send_compiler.compile_send(mode=1, model="omni", annotations=[_ann(1, "把2改成3")])
    assert out["capability"]["ok"] is True
    assert out["routing_suggestion"] is None
    assert out["render_surfaces"] == ["ai", "clean"]
    assert out["file_order"][0]["name"] == "__ai_surface_segment__"


def test_compile_send_mode_one_bad_model_suggests_downgrade():
    out = send_compiler.compile_send(mode=1, model="seedance-2.0", annotations=[_ann(1, "x")])
    assert out["capability"]["ok"] is False
    assert out["routing_suggestion"]["suggest_mode"] == 2
    assert out["warnings"]


def test_compile_send_blocks_on_missing_reference():
    out = send_compiler.compile_send(
        mode=2, model="seedance-2.0", template="用{缺失}", assets_in_order=[])
    assert out["blocked"] is True


def test_compile_send_safety_rewrite_applied():
    out = send_compiler.compile_send(
        mode=2, model="seedance-2.0", template="a shot of Einstein",
        assets_in_order=[])
    assert out["safety"]["changed"] and "white-haired" in out["prompt"]


def test_storyboard_scaffold_only_above_threshold():
    assert send_compiler.storyboard_scaffold(4) == []
    windows = send_compiler.storyboard_scaffold(9)
    assert windows[0]["start"] == 0.0 and windows[-1]["end"] == 9.0


def test_compile_send_mode_two_includes_image_step():
    out = send_compiler.compile_send(mode=2, model="seedance-2.0",
                                     annotations=[_ann(1, "把2改成3")])
    assert out["image_step_prompt"] is not None
    assert "不含任何标注元素" in out["image_step_prompt"]  # template 2A
    assert out["render_surfaces"] == ["ai", "clean", "keyframes"]


def test_send_interval_is_annotation_union_plus_padding():
    # 拍板①的招牌语义:区间并集 + 前后 padding = 截段区间
    out = send_compiler.compile_send(
        mode=1, model="omni", padding_sec=0.5, source_duration=8.0,
        annotations=[dict(_ann(1, "a"), interval=[1.2, 4.5]),
                     dict(_ann(2, "b"), interval=[3.0, 6.0])])
    si = out["send_interval"]
    assert si["annotation_union"] == [1.2, 6.0]      # 两标注区间取并
    assert si["start"] == 0.7 and si["end"] == 6.5   # ± padding
    assert si["duration"] == 5.8


def test_send_interval_degenerate_single_frame_stretched_by_padding():
    # 未滚动 = 单帧区间 [ta,ta],必须靠 padding 撑开成可发段(否则段长 0)
    out = send_compiler.compile_send(
        mode=1, model="omni", padding_sec=0.5, source_duration=8.0,
        annotations=[dict(_ann(1, "x"), interval=[3.0, 3.0])])
    si = out["send_interval"]
    assert si["degenerate_union"] is True
    assert si["start"] == 2.5 and si["end"] == 3.5 and si["duration"] == 1.0


def test_send_interval_clamped_to_source_duration():
    out = send_compiler.compile_send(
        mode=1, model="omni", padding_sec=1.0, source_duration=5.0,
        annotations=[dict(_ann(1, "x"), interval=[0.2, 4.9])])
    si = out["send_interval"]
    assert si["start"] == 0.0 and si["end"] == 5.0  # 不越界


def test_send_interval_none_without_intervals():
    out = send_compiler.compile_send(mode=1, model="omni", annotations=[_ann(1, "x")])
    assert out["send_interval"] is None  # 标注没带区间 → 不编造段


def test_compile_send_mode_one_and_three_have_no_image_step():
    m1 = send_compiler.compile_send(mode=1, model="omni", annotations=[_ann(1, "x")])
    assert m1["image_step_prompt"] is None
    m3 = send_compiler.compile_send(mode=3, model="seedance-2.0", annotations=[_ann(1, "x")])
    assert m3["image_step_prompt"] is None  # mode 3 已有参考图,无图步


# --- 完整公式:能力 ∩ 媒体 ∩ 配额(0.2.0 新增拦截)-----------------------------

_REF_ASSETS = [{"name": "改好的关键帧", "media_type": "image", "ref": "img-sha"},
               {"name": "原片段", "media_type": "video", "ref": "vid-sha"}]


def test_mode_two_blocks_a_model_that_cannot_take_the_source_video():
    """veo 会多参考,但模式二必须送原视频而它收不了 —— 从前会把视频当
    「不兼容素材」悄悄剔除后照发,提示词里却还写着「替换原视频中对应区域」。"""
    out = send_compiler.compile_send(mode=2, model="veo-3.1", annotations=[],
                                     assets_in_order=_REF_ASSETS)
    assert out["blocked"] is True
    assert out["capability"]["ok"] is False
    assert "收不下" in out["routing_suggestion"]["reason"]


def test_mode_two_passes_for_video_capable_models():
    for model in ("kling-v3-omni", "seedance-2.0", "gemini-omni-flash-preview"):
        out = send_compiler.compile_send(mode=2, model=model, annotations=[],
                                         assets_in_order=_REF_ASSETS)
        assert out["blocked"] is False, (model, out["warnings"])
        assert out["files"] == ["img-sha", "vid-sha"], model


def test_capability_mismatch_now_blocks_dispatch():
    """模式一配 seedance:seedance 不会 edit,必须拦,不能只记一句警告。"""
    out = send_compiler.compile_send(mode=1, model="seedance-2.0",
                                     annotations=[_ann(1, "换成花瓶")])
    assert out["blocked"] is True
    assert out["capability"]["ok"] is False


def test_default_template_numbers_video_as_the_first_video():
    """kling 按类型分别计数:原视频是**第一个视频**,不是「第 2 个文件」。"""
    out = send_compiler.compile_send(mode=2, model="kling-v3-omni", annotations=[],
                                     assets_in_order=_REF_ASSETS)
    assert "<<<image_1>>>" in out["prompt"]
    assert "<<<video_1>>>" in out["prompt"]
    assert "<<<video_2>>>" not in out["prompt"]


def test_compile_send_carries_the_actual_files():
    out = send_compiler.compile_send(mode=3, model="seedance-2.0", annotations=[],
                                     assets_in_order=_REF_ASSETS)
    assert out["files"] == ["img-sha", "vid-sha"]
    assert out["bind_errors"] == []


# ── 标注配色的单一真源 ──────────────────────────────────────────────
# 界面上看到的框色、烧进视频的框色、提示词里说的色名,必须是同一行表里出来的。
# 从前烧红框却说青框,模型对不上;这两条把三处钉在一起。

def test_annotation_color_table_is_the_only_source_of_truth():
    from ze_video_domain import ffmpeg_ops
    for i in range(len(ffmpeg_ops.ANNOTATION_COLORS) + 3):   # 越界要能循环
        row = ffmpeg_ops.ANNOTATION_COLORS[i % len(ffmpeg_ops.ANNOTATION_COLORS)]
        assert ffmpeg_ops.annotation_color(i) == (row[0], row[1])
        assert ffmpeg_ops.annotation_hex(i) == row[2]


def test_burnt_box_color_matches_the_color_word_in_the_prompt():
    """第 N 个标注:drawbox 用的色 与 提示词里说的色名 必须指同一个颜色。"""
    from ze_video_domain import ffmpeg_ops, send_compiler
    anns = [{"number": i + 1, "label": f"改第{i + 1}处",
             "bbox": {"x": 10 * i, "y": 10, "w": 40, "h": 40}, "interval": [0.0, 1.0]}
            for i in range(4)]
    argv = ffmpeg_ops.build_burn_annotations_argv("ffmpeg", "in.mp4", "out.mp4", anns)
    vf = argv[argv.index("-vf") + 1]
    compiled = send_compiler.compile_send(
        mode=1, model="gemini-omni-flash-preview", annotations=anns,
        canvas_w=1920, canvas_h=1080)
    for i in range(len(anns)):
        ff_name, zh_name, hexcode = ffmpeg_ops.ANNOTATION_COLORS[i]
        assert f"color={ff_name}@" in vf, f"第 {i + 1} 框没用 {ff_name} 烧"
        assert zh_name in compiled["prompt"], f"提示词里没提 {zh_name}"
        assert hexcode.startswith("#") and len(hexcode) == 7


def test_color_does_not_drift_when_annotations_are_filtered_out():
    """选段排除了 ①、或 ① 关了眼睛之后,剩下那几个框的颜色不许跟着变。

    这是「两处各写一遍颜色」的第三种犯法:界面按 number 上色,烧框按位序上色。
    只要有过滤,人看着青条、视频里那框是红的、模型按「红色框」找 —— 全错开。
    """
    from ze_video_domain import ffmpeg_ops, send_compiler
    three = [{"number": n, "label": f"第{n}处",
              "bbox": {"x": 10 * n, "y": 10, "w": 40, "h": 40},
              "interval": [float(n), float(n) + 1.0]} for n in (1, 2, 3)]

    def burnt(anns):
        argv = ffmpeg_ops.build_burn_annotations_argv("ffmpeg", "i.mp4", "o.mp4", anns)
        vf = argv[argv.index("-vf") + 1]
        return [seg.split("=")[1].split("@")[0]
                for seg in vf.split(":") if seg.startswith("color=")]

    assert burnt(three) == ["red", "cyan", "yellow"]
    # 只发 ②③(① 被选段排除或关了眼睛)—— 颜色必须还是青、黄,不许变成红、青
    assert burnt(three[1:]) == ["cyan", "yellow"]
    # 只发 ③ —— 还是黄
    assert burnt(three[2:]) == ["yellow"]
    # 界面色跟着编号走,与上面逐条对上
    assert [ffmpeg_ops.annotation_hex(a["number"] - 1) for a in three[1:]] == \
        [ffmpeg_ops.ANNOTATION_COLORS[1][2], ffmpeg_ops.ANNOTATION_COLORS[2][2]]
    # 提示词里说的色名同样不漂
    prompt = send_compiler.compile_send(
        mode=1, model="gemini-omni-flash-preview", annotations=three[1:],
        canvas_w=1920, canvas_h=1080)["prompt"]
    assert "青色" in prompt and "黄色" in prompt
    assert "红色" not in prompt


def test_single_frame_annotation_burns_one_frame_not_the_whole_segment():
    """界面上叫「单帧」,视频里就得是一帧 —— 不加 enable 会让它整段常亮。"""
    from ze_video_domain import ffmpeg_ops
    ann = [{"number": 1, "label": "这一下", "bbox": {"x": 0, "y": 0, "w": 50, "h": 50},
            "interval": [2.0, 2.0]}]
    argv = ffmpeg_ops.build_burn_annotations_argv("ffmpeg", "i.mp4", "o.mp4", ann, fps=30)
    vf = argv[argv.index("-vf") + 1]
    assert "enable=" in vf, "单帧标注不能没有 enable,否则整段都画着这个框"
    assert "between(t,2.000,2.033)" in vf


def test_bbox_is_drawn_relative_to_the_real_frame_not_canvas_pixels():
    """项目画布 1920×1080、素材 1280×720 时,框不许按画布像素画到画面外。"""
    from ze_video_domain import ffmpeg_ops
    ann = [{"number": 1, "label": "右下角那块",
            "bbox": {"x": 1440, "y": 810, "w": 480, "h": 270}, "interval": [0.0, 1.0]}]
    argv = ffmpeg_ops.build_burn_annotations_argv(
        "ffmpeg", "i.mp4", "o.mp4", ann, canvas_w=1920, canvas_h=1080)
    vf = argv[argv.index("-vf") + 1]
    assert "x=iw*0.750000" in vf and "y=ih*0.750000" in vf
    assert "w=iw*0.250000" in vf and "h=ih*0.250000" in vf
    # 不给画布尺寸时退回像素(向后兼容,老调用点不炸)
    vf2 = ffmpeg_ops.build_burn_annotations_argv("ffmpeg", "i.mp4", "o.mp4", ann)
    vf2 = vf2[vf2.index("-vf") + 1]
    assert "x=1440:y=810:w=480:h=270" in vf2


def test_burn_draws_one_box_per_exposed_window_not_across_the_cover():
    """标注被中间那层视频盖住的时间,视频里就不该画这个框。"""
    from ze_video_domain import ffmpeg_ops
    ann = [{"number": 1, "label": "改这块", "bbox": {"x": 0, "y": 0, "w": 50, "h": 50},
            "interval": [1.0, 6.0], "windows": [[1.0, 2.0], [5.0, 6.0]]}]
    argv = ffmpeg_ops.build_burn_annotations_argv("ffmpeg", "i.mp4", "o.mp4", ann,
                                                 canvas_w=1920, canvas_h=1080)
    vf = argv[argv.index("-vf") + 1]
    assert vf.count("drawbox=") == 2, vf
    assert "between(t,1.000,2.000)" in vf
    assert "between(t,5.000,6.000)" in vf
    assert "between(t,1.000,6.000)" not in vf     # 不许跨过被盖住的那 3 秒
