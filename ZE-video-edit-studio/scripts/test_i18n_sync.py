"""test_i18n_sync — insert_key(多行/内联/嵌套/幂等)+ fill 编排回归。

跑法(从 scripts/):.venv/bin/python -m pytest test_i18n_sync.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import i18n_keys_lib as lib  # noqa: E402
import i18n_sync as sync  # noqa: E402

ROOT_RE = r"export const zh\b"

ZH = """export const zh = {
  common: {
    save: '保存',
  },
  inlineNs: { a: 'A' },
} as const
"""


def test_insert_leaf_into_multiline_block():
    out = lib.insert_key(ZH, "common.cancel", "TODO:common.cancel", ROOT_RE)
    assert "cancel: 'TODO:common.cancel'," in out
    assert "save: '保存'," in out  # 原内容保留


def test_insert_is_idempotent():
    once = lib.insert_key(ZH, "common.cancel", "x", ROOT_RE)
    twice = lib.insert_key(once, "common.cancel", "x", ROOT_RE)
    assert once == twice


def test_insert_existing_leaf_noop():
    assert lib.insert_key(ZH, "common.save", "y", ROOT_RE) == ZH


def test_insert_new_top_namespace():
    out = lib.insert_key(ZH, "brand.new", "TODO:brand.new", ROOT_RE)
    assert "brand: {" in out
    assert "new: 'TODO:brand.new'," in out


def test_insert_into_inline_block():
    out = lib.insert_key(ZH, "inlineNs.b", "TODO:inlineNs.b", ROOT_RE)
    # 内联块就地扩展,不破行
    assert "{ a: 'A', b: 'TODO:inlineNs.b' }" in out


def test_insert_deep_creates_inline_nested():
    out = lib.insert_key(ZH, "deep.x.y", "v", ROOT_RE)
    assert "deep: {" in out and "y: 'v'," in out


BRACE_STR = """export const zh = {
  share: {
    desc: '所有者:{owner}。添加后可改',
    add: '添加',
  },
} as const
"""


def test_insert_sibling_when_block_has_brace_string():
    """块内 value 含 {owner} 插值时,brace 匹配不被字符串里的 } 带偏(HIGH 回归)。"""
    out = lib.insert_key(BRACE_STR, "share.remove", "TODO:share.remove", ROOT_RE)
    assert "remove: 'TODO:share.remove'," in out
    # 没破坏既有 brace-string value
    assert "desc: '所有者:{owner}。添加后可改'," in out
    # remove 落在 share 块内(在 add 之后、share 闭合之前)
    assert out.index("add: '添加'") < out.index("remove:") < out.index("} as const")


def test_descend_into_string_leaf_raises():
    """把字符串叶子当 namespace 下钻 → 拒绝(不破坏文件)。"""
    import pytest as _pt
    with _pt.raises(ValueError):
        lib.insert_key(BRACE_STR, "share.desc.deeper", "x", ROOT_RE)


def test_fill_skips_conflict_keeps_file_intact(tmp_path, monkeypatch):
    """fill 遇叶子冲突 key → 跳过、记 skipped、文件不被破坏。"""
    _mk_skill(tmp_path)
    # zh.ts 有 editor.run(字符串叶子);用 'editor.run.x'(把叶子当 namespace)
    dumped = {
        "locale_codes": ["zh", "en"], "default_locale": "zh", "meta_codes": ["zh", "en"],
        "framework": {"zh": ["editor.run"], "en": ["editor.run"]},
        "mod": {"zh": ["hint.a"], "en": ["hint.a"]},
    }
    monkeypatch.setattr(lib, "dump", lambda *a, **k: dumped)
    monkeypatch.setattr(lib, "scan_used_keys", lambda *a, **k: {"editor.run.x": ["X.tsx:1"]})
    before = (tmp_path / "05-i18n" / "zh.ts").read_text(encoding="utf-8")
    res = sync.fill(tmp_path)
    assert res["skipped"], "应记录跳过的冲突 key"
    # zh.ts 没被破坏(run 仍是字符串叶子)
    assert (tmp_path / "05-i18n" / "zh.ts").read_text(encoding="utf-8") == before


def test_no_trailing_comma_still_valid():
    txt = "export const zh = {\n  only: 'one'\n} as const\n"
    out = lib.insert_key(txt, "two", "2", ROOT_RE)
    # 末项无逗号时自动补,再加新项
    assert "only: 'one'," in out and "two: '2'," in out


# ── fill 编排(monkeypatch dump/scan,真改 temp 文件)──────────────────────

def _mk_skill(tmp_path: Path):
    (tmp_path / "05-i18n").mkdir(parents=True)
    (tmp_path / "02-mod").mkdir(parents=True)
    (tmp_path / "05-i18n" / "zh.ts").write_text(
        "export const zh = {\n  editor: {\n    run: '运行',\n  },\n} as const\n", encoding="utf-8")
    (tmp_path / "05-i18n" / "en.ts").write_text(
        "export const en: Dict = {\n  editor: {\n    run: 'Run',\n  },\n}\n", encoding="utf-8")
    (tmp_path / "02-mod" / "09-i18n.ts").write_text(
        "export const messages = {\n  zh: {\n    hint: { a: '甲' },\n  },\n  en: {\n    hint: { a: 'A' },\n  },\n}\n",
        encoding="utf-8")


def test_fill_adds_used_undefined_framework_key(tmp_path, monkeypatch):
    _mk_skill(tmp_path)
    dumped = {
        "locale_codes": ["zh", "en"], "default_locale": "zh", "meta_codes": ["zh", "en"],
        "framework": {"zh": ["editor.run"], "en": ["editor.run"]},
        "mod": {"zh": ["hint.a"], "en": ["hint.a"]},
    }
    monkeypatch.setattr(lib, "dump", lambda *a, **k: dumped)
    monkeypatch.setattr(lib, "scan_used_keys", lambda *a, **k: {"editor.newKey": ["X.tsx:1"]})
    res = sync.fill(tmp_path)
    assert res["count"] == 2  # zh + en
    zh = (tmp_path / "05-i18n" / "zh.ts").read_text(encoding="utf-8")
    en = (tmp_path / "05-i18n" / "en.ts").read_text(encoding="utf-8")
    assert "newKey: 'TODO:editor.newKey'," in zh
    assert "newKey: 'TODO:editor.newKey'," in en


def test_fill_routes_mod_key_to_mod_file(tmp_path, monkeypatch):
    _mk_skill(tmp_path)
    dumped = {
        "locale_codes": ["zh", "en"], "default_locale": "zh", "meta_codes": ["zh", "en"],
        "framework": {"zh": ["editor.run"], "en": ["editor.run"]},
        "mod": {"zh": ["hint.a"], "en": ["hint.a"]},
    }
    monkeypatch.setattr(lib, "dump", lambda *a, **k: dumped)
    monkeypatch.setattr(lib, "scan_used_keys", lambda *a, **k: {"mod.hint.b": ["X.tsx:1"]})
    res = sync.fill(tmp_path)
    mod = (tmp_path / "02-mod" / "09-i18n.ts").read_text(encoding="utf-8")
    assert "b: 'TODO:mod.hint.b'" in mod
    # 框架词表不该被业务 key 污染
    assert "TODO" not in (tmp_path / "05-i18n" / "zh.ts").read_text(encoding="utf-8")


def test_fill_aligns_parity_gap(tmp_path, monkeypatch):
    _mk_skill(tmp_path)
    # en 框架缺 editor.extra → fill 应补进 en.ts
    dumped = {
        "locale_codes": ["zh", "en"], "default_locale": "zh", "meta_codes": ["zh", "en"],
        "framework": {"zh": ["editor.run", "editor.extra"], "en": ["editor.run"]},
        "mod": {"zh": ["hint.a"], "en": ["hint.a"]},
    }
    monkeypatch.setattr(lib, "dump", lambda *a, **k: dumped)
    monkeypatch.setattr(lib, "scan_used_keys", lambda *a, **k: {})
    sync.fill(tmp_path)
    en = (tmp_path / "05-i18n" / "en.ts").read_text(encoding="utf-8")
    assert "extra: 'TODO:editor.extra'," in en
