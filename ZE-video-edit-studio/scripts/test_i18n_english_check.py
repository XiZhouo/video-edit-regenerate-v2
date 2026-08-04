"""test_i18n_english_check — chrome 区裸英文门禁回归测试(反向门禁)。

跑法(从 scripts/):python -m pytest test_i18n_english_check.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import i18n_english_check as ie  # noqa: E402


def test_default_scan_clean():
    """整个 04-user-facing(含 editors)应零裸英文(门禁基线;editors 3 处已迁完)。"""
    res = ie.run()
    assert res["ok"], f"裸英文:{res['violations']}"


def test_actually_scans_files():
    res = ie.run()
    assert res["scanned_files"] >= 10


def test_detects_jsx_text_english(tmp_path):
    f = tmp_path / "X.tsx"
    f.write_text("export const X = () => <span>Recent Ops</span>\n", encoding="utf-8")
    vio = ie.check_file(f)
    assert any(v["kind"] == "text" for v in vio)


def test_detects_title_literal(tmp_path):
    f = tmp_path / "Y.tsx"
    f.write_text('const b = <button title="Pause">x</button>\n', encoding="utf-8")
    vio = ie.check_file(f)
    assert any(v["kind"] == "attr" for v in vio)


def test_detects_aria_and_placeholder(tmp_path):
    f = tmp_path / "Z.tsx"
    f.write_text(
        'const a = <i aria-label="Retry failed" />\n'
        "const p = <input placeholder='Search here' />\n",
        encoding="utf-8",
    )
    assert len(ie.check_file(f)) >= 2


def test_detects_brace_string_literal(tmp_path):
    f = tmp_path / "W.tsx"
    f.write_text("const x = <i title={'Hide log'} />\n", encoding="utf-8")
    assert ie.check_file(f)


def test_allows_t_call_text(tmp_path):
    """`{t('ns.key')}` 文本节点不算裸英文。"""
    f = tmp_path / "T.tsx"
    f.write_text("const x = <span>{t('footer.recentOps')}</span>\n", encoding="utf-8")
    assert not ie.check_file(f)


def test_allows_t_call_attr(tmp_path):
    f = tmp_path / "TA.tsx"
    f.write_text("const x = <i title={t('footer.pause')} />\n", encoding="utf-8")
    assert not ie.check_file(f)


def test_ignores_arrow_and_generics(tmp_path):
    """`=> Promise<void>` 等纯 TS 不是 JSX 文本节点,不误报。"""
    f = tmp_path / "G.tsx"
    f.write_text("type F = { login: () => Promise<void> }\n", encoding="utf-8")
    assert not ie.check_file(f)


def test_exempts_snake_case_identifier(tmp_path):
    """snake_case 标识符(doc_id)是数据边界,literally 透传,不算可译 chrome。"""
    f = tmp_path / "I.tsx"
    f.write_text("const x = <span>doc_id</span>\n", encoding="utf-8")
    assert not ie.check_file(f)


def test_allows_geometric_symbols(tmp_path):
    f = tmp_path / "S.tsx"
    f.write_text("const x = <span>▶ ⚠ — # ↶ ↷</span>\n", encoding="utf-8")
    assert not ie.check_file(f)


def test_allows_units(tmp_path):
    """单位 / 缩写(ms、px、id)在 allowlist,不算 prose 英文。"""
    f = tmp_path / "U.tsx"
    f.write_text('const x = <span title="120 ms">x</span>\n', encoding="utf-8")
    assert not ie.check_file(f)


def test_ignores_block_comment_english(tmp_path):
    f = tmp_path / "B.tsx"
    f.write_text("/* Recent Ops here */\nconst x = 1\n", encoding="utf-8")
    assert not ie.check_file(f)


def test_scans_whole_user_facing_including_editors():
    """新策略:扫整个 01-core/04-user-facing(含 editors);旧的「只扫 header/footer/auth」已废。"""
    assert any("04-user-facing" in r for r in ie.SCAN_ROOTS)


def test_generic_text_node_not_flagged(tmp_path):
    """TS 泛型被误读成 JSX 文本节点(`): Record<...>`)识别出 TS 形状 → _TS_SHAPE_RE 跳过,不误报。"""
    f = tmp_path / "G2.tsx"
    f.write_text(
        "function f(): Record<string, Foo> { return parse() as Bar<Baz> }\n",
        encoding="utf-8",
    )
    assert not ie.check_file(f)


def test_prose_with_punctuation_still_flagged(tmp_path):
    """含 `&` / `()` / `[]` 的真 chrome 文案不被 TS 形状过滤误放(修 code review HIGH 的盲区)。"""
    for src in (
        "const a = <button>Save & Exit</button>\n",
        "const b = <span>Filter by group</span>\n",
    ):
        f = tmp_path / "P.tsx"
        f.write_text(src, encoding="utf-8")
        assert ie.check_file(f), f"应捕获裸英文:{src!r}"


def test_block_comment_preserves_line_numbers(tmp_path):
    """多行块注释不移动后续违规行号(修复『变更位置』错位:旧实现吃掉换行会让行号整体上移)。"""
    f = tmp_path / "L.tsx"
    f.write_text(
        "/* aaa\nbbb\nccc */\nconst x = <span>Recent Ops</span>\n",
        encoding="utf-8",
    )
    vio = ie.check_file(f)
    assert vio and vio[0]["line"] == 4
