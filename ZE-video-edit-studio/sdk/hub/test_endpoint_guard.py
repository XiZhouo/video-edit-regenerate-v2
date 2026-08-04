"""用户自填端点 SSRF 护栏测试(P5:安全核心,穷举拒绝面 + DNS rebinding 防护)。"""

from __future__ import annotations

import urllib.parse
import urllib.request

import pytest

from sdk.hub.endpoint_guard import (
    is_safe_registerable_endpoint, assert_resolves_public, guard_outbound_host,
)


# ── 登记期护栏 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("url", [
    "https://api.my-llm.example.com",
    "https://api.my-llm.example.com/v1",
    "https://api.my-llm.example.com:443/v1",
    "https://8.8.8.8",                            # 真公网 IP 字面量
])
def test_accepts_safe_public_endpoints(url):
    ok, reason = is_safe_registerable_endpoint(url)
    assert ok, reason


@pytest.mark.parametrize("url,why", [
    ("", "empty"),
    ("http://api.example.com", "http"),                    # 非 https
    ("ftp://api.example.com", "scheme"),
    ("https://user:pass@api.example.com", "userinfo"),     # 凭据混入
    ("https://api.example.com/?x=1", "query"),
    ("https://api.example.com/#f", "fragment"),
    ("https://api.example.com:8080", "port"),              # 非标端口(常指内网服务)
    ("https://localhost", "localhost"),
    ("https://foo.internal", "internal 后缀"),
    ("https://svc.local", "local 后缀"),
    ("https://127.0.0.1", "环回"),
    ("https://10.0.0.252", "私网 10/8"),                    # 正是原型内网 IP
    ("https://192.168.1.5", "私网 192.168"),
    ("https://172.16.0.9", "私网 172.16/12"),
    ("https://169.254.169.254", "链路本地/云元数据"),        # 云 metadata SSRF 经典目标
    ("https://[::1]", "IPv6 环回"),
    ("https://[fd00::1]", "IPv6 ULA 私网"),
])
def test_rejects_unsafe_endpoints(url, why):
    ok, reason = is_safe_registerable_endpoint(url)
    assert not ok, f"应拒({why}): {url} → {reason}"


# ── 运行时解析核验(DNS rebinding 防护)──────────────────────────────────────
def test_assert_public_ip_literal_passes():
    assert_resolves_public("8.8.8.8")             # 不抛


def test_assert_private_ip_literal_rejected():
    with pytest.raises(ValueError):
        assert_resolves_public("10.0.0.252")


def test_assert_hostname_resolving_public_passes():
    # 注入 resolver:主机名解析到公网 IP → 放行
    fake = lambda name: [(2, 1, 6, "", ("93.184.216.34", 0))]
    assert_resolves_public("api.example.com", _resolver=fake)


def test_assert_hostname_resolving_private_rejected():
    # DNS rebinding:登记时是公网名,解析却指向内网 → 运行时挡下
    fake = lambda name: [(2, 1, 6, "", ("10.0.0.5", 0))]
    with pytest.raises(ValueError):
        assert_resolves_public("evil.example.com", _resolver=fake)


def test_assert_mixed_resolution_any_private_rejected():
    # 多条解析里任一非公网 → 拒(不给攻击者留缝)
    fake = lambda name: [(2, 1, 6, "", ("93.184.216.34", 0)), (2, 1, 6, "", ("127.0.0.1", 0))]
    with pytest.raises(ValueError):
        assert_resolves_public("api.example.com", _resolver=fake)


def test_assert_resolution_failure_rejected():
    def _boom(name): raise OSError("nxdomain")
    with pytest.raises(ValueError):
        assert_resolves_public("nope.example.com", _resolver=_boom)


def test_assert_blocked_hostname_rejected():
    with pytest.raises(ValueError):
        assert_resolves_public("localhost")


# ── guard_outbound_host:官方放行(零解析)/ custom 解析核验 ───────────────────
def test_guard_official_host_skips_resolution():
    # 官方 host 在集合内 → 直接放行,resolver 绝不被调(零成本、零回归)
    calls = []
    guard_outbound_host("api.openai.com", official={"api.openai.com"},
                        _resolver=lambda n: calls.append(n) or [(2, 1, 6, "", ("1.2.3.4", 0))])
    assert calls == []                                    # 官方 host 未解析


def test_guard_custom_host_resolved_public_passes():
    guard_outbound_host("my-llm.example.com", official={"api.openai.com"},
                        _resolver=lambda n: [(2, 1, 6, "", ("93.184.216.34", 0))])


def test_guard_custom_host_resolving_private_rejected():
    with pytest.raises(ValueError):
        guard_outbound_host("evil.example.com", official={"api.openai.com"},
                            _resolver=lambda n: [(2, 1, 6, "", ("10.0.0.5", 0))])


def test_guard_custom_private_ip_literal_rejected_without_dns():
    # 私网 IP 字面量 → 直接拒(不解析),即便不在官方集
    with pytest.raises(ValueError):
        guard_outbound_host("169.254.169.254", official={"api.openai.com"})


# ---------------------------------------------------------------------------
# 重定向逐跳复检(2026-07-25 补:登记期/发请求前的护栏对重定向后的地址一次都问不到)
# ---------------------------------------------------------------------------

def _handler(resolver=None):
    from sdk.hub import GuardedRedirectHandler
    return GuardedRedirectHandler(_resolver=resolver)


def _redirect(handler, from_url: str, to_url: str):
    # 用真 Request,不手搓替身:替身缺一个方法就会让"放行"那条测试假红,
    # 而假红最容易被当成"护栏太严"顺手放宽。
    req = urllib.request.Request(from_url, method="GET")
    return handler.redirect_request(req, None, 302, "Found", {}, to_url)


@pytest.mark.parametrize("target", [
    "http://169.254.169.254/latest/meta-data/",   # 云元数据端点 —— 最经典的 SSRF 目标
    "http://127.0.0.1:8080/admin",
    "http://10.0.0.252:12018/",
    "http://192.168.1.1/",
    "https://169.254.169.254/",                   # 换成 https 也一样是内网
])
def test_redirect_into_internal_addresses_is_blocked(target):
    """护栏在发请求前只跑一次,而 302 的内容由对面决定 —— 每一跳都得重新问。"""
    with pytest.raises(ValueError):
        _redirect(_handler(), "https://api.example.com/v1/models", target)


def test_redirect_may_not_downgrade_https_to_http():
    """降级后连传输层保护都没了,而且常常就是内网入口。"""
    with pytest.raises(ValueError, match="downgrade"):
        _redirect(_handler(), "https://api.example.com/v1", "http://api.example.com/v1")


def test_redirect_to_non_http_scheme_is_blocked():
    with pytest.raises(ValueError, match="non-http"):
        _redirect(_handler(), "http://api.example.com/v1", "ftp://api.example.com/x")


def test_redirect_to_a_public_host_still_works():
    """不能因为怕就把重定向砍掉 —— 合规跳转必须照常放行。"""
    handler = _handler(resolver=lambda name: [(2, 1, 6, "", ("93.184.216.34", 443))])
    out = _redirect(handler, "https://api.example.com/v1", "https://cdn.example.com/v1")
    assert out is not None and out.full_url == "https://cdn.example.com/v1"


def test_redirect_host_that_resolves_internal_is_blocked():
    """DNS rebinding 的重定向版:跳转目标是个公网名字,却解析到内网。"""
    handler = _handler(resolver=lambda name: [(2, 1, 6, "", ("10.1.2.3", 443))])
    with pytest.raises(ValueError, match="non-public"):
        _redirect(handler, "https://api.example.com/v1", "https://evil.example.com/v1")


def test_outbound_callers_do_not_use_bare_urlopen():
    """出站入口只能有一个。裸 urlopen 会静默跟随重定向,把上面这些判据全绕过去。

    这条不是文体洁癖:probe/model 里任何一处漏网的 urlopen,都让整套护栏在那条路径上失效。
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    offenders = []
    for rel in ("sdk/hub/probe.py", "sdk/model.py"):
        text = (root / rel).read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "urllib.request.urlopen(" in line and "guarded" not in line:
                offenders.append(f"{rel}:{lineno}")
    assert not offenders, f"这些出站点绕过了重定向护栏:{offenders}"
