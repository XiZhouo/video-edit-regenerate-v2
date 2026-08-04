"""ai_interface_check 测试:锁'每个 introspect reader 都 server+CLI 双暴露'不变量。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ai_interface_check as aic  # noqa: E402


def test_reader_symbols_parsed():
    syms = aic.reader_symbols()
    assert "describe_context" in syms and "list_commands" in syms
    assert "IntrospectionMeta" not in syms  # 类不算能力


def test_refs_word_boundary():
    # 真源符号确在 server/CLI 出现(抽一个核心能力)
    assert aic._refs(aic.SERVER, "describe_context")
    assert aic._refs(aic.CLI, "list_commands")
    # 不存在的符号 → False
    assert not aic._refs(aic.SERVER, "definitely_not_a_reader_xyz")


def test_section5_extracted():
    # FUTURE_STATE.md 是 build-v3 自身的方向文档;下游同步过 toolchain 的工具没有它 →
    # 跳过(§5 一致性由 run() 的 `if FUTURE.is_file()` 守,缺文件时本就不校验)。
    if not aic.FUTURE.is_file():
        pytest.skip("FUTURE_STATE.md 仅 build-v3 自身有(下游工具无)")
    sec = aic._section5(aic.FUTURE.read_text(encoding="utf-8"))
    assert "Context.Describe" in sec and len(sec) > 100


def test_settings_read_is_documented_exception():
    assert "settings_read" in aic._SERVER_ALT_ROUTE


def test_real_repo_ai_interface_consistent():
    """核心不变量:每个 reader server+CLI 双暴露(settings_read 为已记录例外),§5 文档与代码一致。"""
    res = aic.run()
    assert res["ok"], f"violations: {res['violations']}"
    # 例外被记录(诚实,非隐藏)
    assert any("settings_read" in n for n in res["exempt_notes"])
