"""输入引用绑定层 + 逐模式真值表的测试。

这一批测试守的是两件很容易悄悄坏掉的事:
1. **记号的计数域** —— kling 按类型分别计数,同一批里出现两个「1」是对的。
2. **模式维不能被压平** —— 同一个模型的 edit 与 reference 规矩不同,
   拿一个去卡另一个会拦死主用例。第 3、4 两个测试是这条的守门人。
"""
from __future__ import annotations

import pytest

from ze_video_domain import capability_matrix as caps
from ze_video_domain import ref_binding as rb


def _batch():
    """一批中性输入:视频、图、图。角色由提示词决定,不在类型里编码。"""
    return [
        rb.Input("舞蹈", "video", "ref-v1"),
        rb.Input("主角", "image", "ref-i1"),
        rb.Input("街景", "image", "ref-i2"),
    ]


# --- 记号的计数域 -------------------------------------------------------------

def test_kling_numbers_each_kind_separately():
    """kling:视频和图片各自从 1 起 —— 同一批里两个「1」指不同文件。"""
    c = rb.compile_inputs("kling-v3-omni", "reference-to-video",
                          "参考{舞蹈}的节奏,让{主角}在{街景}里入画", _batch())
    assert c.ok, c.errors
    assert c.tokens == {"舞蹈": "<<<video_1>>>", "主角": "<<<image_1>>>", "街景": "<<<image_2>>>"}
    # 第一张图必须是 image_1 而不是 image_2 —— 用平铺序号就会错在这里。
    assert "<<<image_1>>>在<<<image_2>>>" in c.prompt


def test_seedance_numbers_all_inputs_in_one_queue():
    c = rb.compile_inputs("seedance-2.0", "reference-to-video",
                          "参考{舞蹈}的节奏,让{主角}在{街景}里入画", _batch())
    assert c.ok, c.errors
    assert c.tokens == {"舞蹈": "@file_0", "主角": "@file_1", "街景": "@file_2"}


def test_natural_language_model_gets_prose_not_tokens():
    c = rb.compile_inputs("veo-3.1", "reference-to-video", "让{主角}走进画面",
                          [rb.Input("主角", "image", "i1")])
    assert c.ok, c.errors
    assert c.tokens == {}
    assert "「主角」" in c.prompt


# --- 模式维不能被压平(这两条是本层的立身之本)---------------------------------

def test_edit_mode_accepts_a_lone_video():
    """「一个待编辑视频 + 一句话说改哪里」—— 视频编辑最标准的用法,必须放行。

    gemini-omni 的 reference 模式确实要求至少一张图,但那是 reference 的规矩。
    把它套到 edit 上会拦死这条主路径。
    """
    c = rb.compile_inputs("gemini-omni-flash-preview", "edit",
                          "把{原片}里桌上的杯子换成花瓶", [rb.Input("原片", "video", "v1")])
    assert c.ok, f"纯视频编辑被误拦:{c.errors}"
    assert c.files == ["v1"]


def test_reference_mode_rejects_a_lone_video_on_the_same_model():
    """同一个模型、另一个模式 —— 这里纯视频就该被拦。两条一起才证明模式维没被压平。"""
    c = rb.compile_inputs("gemini-omni-flash-preview", "reference-to-video",
                          "参考{片段}", [rb.Input("片段", "video", "v1")])
    assert not c.ok
    assert any("图片" in e for e in c.errors)


def test_edit_slot_does_not_inherit_reference_required_kinds():
    """直接盯住数据本身:edit 槽不得带 reference 的必备类型。"""
    edit_slot = caps.mode_slot("gemini-omni-flash-preview", "edit")
    ref_slot = caps.mode_slot("gemini-omni-flash-preview", "reference-to-video")
    assert ref_slot["required_kinds"] == ["image"]
    assert edit_slot["required_kinds"] == []
    assert edit_slot["min_by_kind"] == {"video": 1}
    assert edit_slot["alt_param"] == "previous_interaction_id"


# --- 记号 / 顺序 / 文件同源 ---------------------------------------------------

def test_files_order_and_tokens_come_from_one_pass():
    """文件顺序即契约,而且必须和记号编号出自同一次遍历。"""
    inputs = _batch()
    c = rb.compile_inputs("seedance-2.0", "reference-to-video",
                          "{舞蹈}{主角}{街景}", inputs)
    assert c.files == [r.ref for r in inputs]
    assert [o["name"] for o in c.order] == [r.name for r in inputs]
    assert [o["index"] for o in c.order] == [0, 1, 2]
    # 记号里的序号必须与 order 的 index 对得上
    for entry in c.order:
        assert c.tokens[entry["name"]] == f"@file_{entry['index']}"


def test_compile_never_emits_files_without_tokens_being_recomputed():
    """换一个模型重编译,文件不变、记号必须跟着变 —— 两者不允许各自演化。"""
    inputs = _batch()
    a = rb.compile_inputs("seedance-2.0", "reference-to-video", "{主角}", inputs)
    b = rb.compile_inputs("kling-v3-omni", "reference-to-video", "{主角}", inputs)
    assert a.files == b.files
    assert a.tokens["主角"] != b.tokens["主角"]


# --- 发送前拦截 ---------------------------------------------------------------

def test_unknown_placeholder_is_blocked():
    c = rb.compile_inputs("seedance-2.0", "reference-to-video", "让{查无此人}入画",
                          [rb.Input("主角", "image", "i1")])
    assert not c.ok
    assert any("查无此人" in e for e in c.errors)


def test_duplicate_input_name_is_blocked():
    dupes = [rb.Input("主角", "image", "a"), rb.Input("主角", "image", "b")]
    c = rb.compile_inputs("seedance-2.0", "reference-to-video", "{主角}", dupes)
    assert not c.ok
    assert any("重复" in e for e in c.errors)


def test_model_that_cannot_take_video_says_so():
    c = rb.compile_inputs("veo-3.1", "reference-to-video", "参考{片段}",
                          [rb.Input("片段", "video", "v1")])
    assert not c.ok
    assert any("只收" in e and "片段" in e for e in c.errors)


def test_kling_total_quota_drops_when_a_video_is_present():
    """kling:不带视频 7 个,带视频降到 4。1 视频 + 4 图 = 5 > 4,必须拦。"""
    inputs = [rb.Input("片段", "video", "v")] + [
        rb.Input(f"图{i}", "image", f"i{i}") for i in range(4)]
    c = rb.compile_inputs("kling-v3-omni", "reference-to-video", "{片段}", inputs)
    assert not c.ok
    assert any("带参考视频时最多 4" in e for e in c.errors)


def test_happyhorse_rejects_undersized_image():
    small = [rb.Input("小图", "image", "i1", pixels=(200, 150))]
    c = rb.compile_inputs("happyhorse-1.0", "reference-to-video", "{小图}", small)
    assert not c.ok
    assert any("300x300" in e for e in c.errors)


def test_text_to_video_takes_no_files():
    c = rb.compile_inputs("seedance-2.0", "text-to-video", "一只猫走过屋顶",
                          [rb.Input("多余", "image", "i1")])
    assert not c.ok
    assert any("不接受输入文件" in e for e in c.errors)


def test_unsupported_mode_is_rejected():
    c = rb.compile_inputs("grok-video", "keyframe-to-video", "x",
                          [rb.Input("a", "image", "1"), rb.Input("b", "image", "2")])
    assert not c.ok
    assert any("不支持" in e for e in c.errors)


def test_registered_but_unmentioned_asset_is_only_a_warning():
    c = rb.compile_inputs("seedance-2.0", "reference-to-video", "{主角}走过来",
                          [rb.Input("主角", "image", "i1"), rb.Input("闲置", "image", "i2")])
    assert c.ok, c.errors                       # 黄,不是红
    assert any("闲置" in w for w in c.warnings)


# --- 「这一步能用哪些模型」= 能力 ∩ 媒体 ∩ 配额 -------------------------------

def test_capability_alone_overcounts_mode_two():
    """会多参考的有 10 个,但模式二必须送原视频 —— 真答案是 5。"""
    assert len(caps.models_for_capability("reference-to-video")) == 10
    mode_two = caps.models_for_mode(2)
    assert len(mode_two) == 5
    assert "veo-3.1" not in mode_two          # 会 ref,但收不了视频
    assert "kling-v3-omni" in mode_two


def test_mode_three_widens_or_narrows_with_the_actual_inputs():
    assert len(caps.models_for_mode(3)) == 10                       # 纯图参照
    assert len(caps.models_for_mode(3, extra_kinds=("video",))) == 5  # 参照里含视频


def test_only_omni_serves_mode_one():
    assert caps.models_for_mode(1) == ["gemini-omni-flash-preview"]


def test_keyframe_models_are_the_seven():
    kf = caps.models_for_step("keyframe-to-video", required_kinds=("image",))
    assert kf == ["kling-v3", "kling-v3-omni", "seedance-2.0", "seedance-2.0-fast",
                  "seedance-2.0-mini", "veo-3.1", "veo-3.1-fast"]
    for excluded in ("grok-video", "happyhorse-1.0", "happyhorse-1.1",
                     "gemini-omni-flash-preview"):
        assert excluded not in kf


def test_audio_inputs_are_seedance_only():
    assert caps.models_for_step("reference-to-video", required_kinds=("audio",)) == [
        "seedance-2.0", "seedance-2.0-fast", "seedance-2.0-mini"]


# --- 真值表本身的完整性 --------------------------------------------------------

def test_matrix_shape_and_mode_coverage():
    assert caps.MATRIX["schema"] == "ze.video_capability_matrix.v1"
    assert len(caps.MODELS) == 11
    counts = {mode: sum(1 for m in caps.MODELS if caps.mode_slot(m, mode)) for mode in caps.MODES}
    assert counts == {"text-to-video": 11, "image-to-video": 11, "keyframe-to-video": 7,
                      "reference-to-video": 10, "edit": 1}


def test_no_model_has_unrecognised_reference_syntax():
    """服务加了新记号写法而我们还不认识 —— 宁可红,不要猜一个错的记号出去。"""
    flagged = [m for m in caps.MODELS if caps.model_grammar(m).get("unrecognised_syntax")]
    assert flagged == [], f"这些模型的记号写法没被解析出来:{flagged}"


def test_duration_bounds_absent_for_omni_present_for_seedance():
    assert caps.duration_bounds("gemini-omni-flash-preview") is None
    assert caps.duration_bounds("seedance-2.0") == (4.0, 15.0)
    assert caps.duration_options("veo-3.1") == [4.0, 6.0, 8.0]
    assert caps.duration_options("seedance-2.0") is None


def test_ratio_support_is_per_model():
    assert caps.supports_ratio("seedance-2.0", "21:9")
    assert not caps.supports_ratio("gemini-omni-flash-preview", "21:9")


@pytest.mark.parametrize("model,mode,expected", [
    ("gemini-omni-flash-preview", "keyframe-to-video", None),   # 服务明说不支持
    ("kling-v3", "reference-to-video", None),                   # 唯一不会多参考的
])
def test_absent_modes_have_no_slot(model, mode, expected):
    assert caps.mode_slot(model, mode) is expected


# --- 版本只有一处真相 ----------------------------------------------------------

def test_version_mirror_agrees_with_domain_version():
    """根目录 VERSION 只是镜像;它和 DOMAIN_VERSION 分叉过一次
    (域层升 0.2.0,同步器仍按 VERSION 写 0.1.0 的锁),这里守住不再发生。"""
    import pathlib

    from ze_video_domain import DOMAIN_VERSION

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    mirror = (repo_root / "VERSION").read_text(encoding="utf-8").strip()
    assert mirror == DOMAIN_VERSION, f"VERSION={mirror} 与 DOMAIN_VERSION={DOMAIN_VERSION} 不一致"
