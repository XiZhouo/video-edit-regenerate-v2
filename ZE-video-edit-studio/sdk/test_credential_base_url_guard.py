"""P5b 服务端护栏测试:_credential_base_url 登记闸(官方严格/自定义护栏)+ _guard_ai_outbound 运行时。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _d in (_ROOT, _ROOT / "01-core" / "01-data-structure" / "01-document",
           _ROOT / "01-core" / "01-data-structure" / "02-entity",
           _ROOT / "01-core" / "_protocol"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import server  # noqa: E402
from entity import ZeError  # noqa: E402
from sdk import PROVIDER_OPENAI_API  # noqa: E402


# ── _credential_base_url 登记闸 ───────────────────────────────────────────────
def test_official_host_accepted():
    assert server._credential_base_url(PROVIDER_OPENAI_API, "https://api.openai.com") == "https://api.openai.com"


def test_custom_safe_https_host_accepted():
    # P5:官方 host 之外的安全公网 https 端点 → 登记通过(自由登记)
    url = "https://my-llm.example.com/v1"
    assert server._credential_base_url(PROVIDER_OPENAI_API, url) == url


@pytest.mark.parametrize("url", [
    "http://my-llm.example.com",          # 非 https
    "https://10.0.0.5",                   # 私网 IP 字面量
    "https://127.0.0.1",                  # 环回
    "https://169.254.169.254",            # 云 metadata
    "https://user:pass@my-llm.example.com",   # userinfo
    "https://my-llm.example.com:8080",    # 非标端口
])
def test_custom_unsafe_host_rejected(url):
    with pytest.raises(ZeError) as e:
        server._credential_base_url(PROVIDER_OPENAI_API, url)
    assert e.value.code == "validation_failed"


def test_official_host_with_query_still_rejected():
    # 官方 host 但带 query → 仍按原严格检查拒(官方路径行为不变)
    with pytest.raises(ZeError):
        server._credential_base_url(PROVIDER_OPENAI_API, "https://api.openai.com/?x=1")


def test_empty_base_url_returns_none():
    assert server._credential_base_url(PROVIDER_OPENAI_API, "") is None


# ── _guard_ai_outbound 运行时护栏 ─────────────────────────────────────────────
def test_guard_ai_outbound_official_skips(monkeypatch):
    # 官方 host → 放行(不解析)
    server._guard_ai_outbound(PROVIDER_OPENAI_API, "https://api.openai.com/v1/models")


def test_guard_ai_outbound_private_ip_rejected():
    # custom 私网 IP 字面量 → 拒(不需 DNS)
    with pytest.raises(ZeError) as e:
        server._guard_ai_outbound(PROVIDER_OPENAI_API, "https://10.0.0.9/v1/models")
    assert e.value.code == "validation_failed"
