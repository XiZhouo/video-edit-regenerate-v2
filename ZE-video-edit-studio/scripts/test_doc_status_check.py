"""doc_status_check 测试:锁'adopted 编辑器不被 prose 说成未做'+ 检测逻辑正确。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import doc_status_check as dsc  # noqa: E402


def test_adopted_classes_nonempty():
    a = dsc._adopted_classes()
    assert "table" in a and "scene3d" in a and len(a) >= 7


def test_stale_detects_table_row_with_backtick_class_and_strict_token():
    # 表格行 + 反引号类名 + 严格未做词 → 命中
    assert dsc.stale_editor_in_line("| `table` | 列表 | ⛏ 待补 |", ["table"]) == "table"
    assert dsc.stale_editor_in_line("| `scene3d` | 3D | pending |", ["scene3d", "table"]) == "scene3d"


def test_stale_ignores_non_table_prose():
    # 非表格行(子功能待补的散文)不误命中
    assert dsc.stale_editor_in_line("`table` 的 marquee 框选待补", ["table"]) is None


def test_stale_ignores_plain_word_without_backticks():
    # 普通词 table 无反引号 → 不命中(避免 "a table of data 待补" 误报)
    assert dsc.stale_editor_in_line("| 一个 table 的说明 | 待补 |", ["table"]) is None


def test_stale_ignores_table_row_without_strict_token():
    assert dsc.stale_editor_in_line("| `table` | 列表 | ✅ 已建 |", ["table"]) is None


def test_stale_ignores_done_marked_row_with_subfeature_note():
    # 行有 ✅(已做标记)+ 子功能 待补 → 不误判编辑器未做(降误报)
    assert dsc.stale_editor_in_line("| `canvas2d` | ✅ 真 | 图层/滤镜待补 |", ["canvas2d"]) is None


def test_first_forward_token():
    assert dsc.first_forward_token("这条 待补") == "待补"
    assert dsc.first_forward_token("styled-unwired 占位") == "styled-unwired"
    assert dsc.first_forward_token("全部已建,无前瞻") is None


def test_real_repo_has_no_stale_editor_status():
    """核心不变量:真实仓库无 adopted 编辑器在 SPEC 表里被说成未做。"""
    res = dsc.run()
    assert res["ok"], f"STALE_EDITOR_STATUS: {res['violations']}"
