"""sdk/lark_client 单测：纯逻辑（校验/解析/定位），不碰网络、不真调 lark-cli。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 叶子 sibling import（同 test_eval_suite）
import lark_client as lc  # noqa: E402


def test_send_rejects_missing_recipient():
    try:
        lc.send(text="hi")
        assert False, "应拒空收件人"
    except Exception as e:
        assert "chat_id" in str(e) or "user_id" in str(e)


def test_send_rejects_empty_body():
    try:
        lc.send(text="   ", chat_id="oc_abc")
        assert False, "应拒空正文"
    except Exception as e:
        assert "正文" in str(e)


def test_fetch_doc_rejects_illegal_token():
    try:
        lc.fetch_doc("bad token!")
        assert False, "应拒非法 token"
    except Exception as e:
        assert "token" in str(e)


def test_parse_message_id():
    assert lc._parse_message_id('{"message_id":"om_abc"}') == "om_abc"
    assert lc._parse_message_id('{"data":{"message_id":"om_d"}}') == "om_d"
    assert lc._parse_message_id("noise om_xyz noise") == "om_xyz"
    assert lc._parse_message_id("") == ""


def test_resolve_lark_cli_explicit(tmp_path):
    fake = tmp_path / "lark-cli"
    fake.write_text("#!/bin/sh\n")
    os.chmod(fake, 0o755)

    class _Cfg:
        def get(self, key, default=None):
            return str(fake) if key == "ze.notify.lark_cli_bin" else default

    assert lc.resolve_lark_cli(_Cfg()) == str(fake)


def test_resolve_lark_cli_missing_raises(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _x: None)
    monkeypatch.setattr(lc, "_LARK_CLI_FALLBACKS", ("/no/such/lark-cli-xyz",))

    class _Cfg:
        def get(self, key, default=None):
            return default

    try:
        lc.resolve_lark_cli(_Cfg())
        assert False, "无 lark-cli 应抛"
    except Exception as e:
        assert "lark-cli" in str(e)
