"""用户自填端点 SSRF 登记护栏(ProviderHub 回收 P5:自由登记任意兼容端点,但不复制原型的裸开放)。

原型允许用户自填**任意** base_url 且**零 SSRF 校验**(裸打用户 URL)——这本身是 SSRF 洞。忠实回收
「登记自定义兼容端点」这个**能力**,但把安全边界做对:

  - 登记期(is_safe_registerable_endpoint,纯函数):强制 https、拒 userinfo/query/fragment、拒非标端口、
    拒私网/环回/链路本地/保留 IP **字面量**、拒 localhost 类主机名。主机名(非 IP)登记期放行,但——
  - 运行时(assert_resolves_public,发请求前调):解析主机名 → **全部**解析 IP 必须公网,挡 DNS rebinding
    (登记时填公网名、解析时指向内网的攻击)。

自由登记 = **向白名单加带护栏的条目**,不是绕过白名单;白名单仍是 SSRF 唯一判据(教义③)。
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse
import urllib.request

# 环回/内网类主机名(即便解析,也不该被登记为外部端点)。
_BLOCKED_HOSTS = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})
_BLOCKED_HOST_SUFFIXES = (".local", ".internal", ".localhost", ".lan", ".home", ".corp")


def _ip_is_public(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    """IP 是否公网可路由(私网/环回/链路本地/保留/多播/未指定 → 非公网)。"""
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified)


def is_safe_registerable_endpoint(url: str) -> "tuple[bool, str]":
    """判定用户自填端点 URL 是否可**安全登记**(SSRF 登记期护栏)。返回 (ok, reason)。

    拒:空/不可解析、非 https、含 userinfo、含 query|fragment、非标端口(非 443)、私网/环回/链路本地/
    保留 IP 字面量、localhost 类主机名。合法主机名(非 IP)登记期放行(运行时须 assert_resolves_public 再核验)。"""
    raw = (url or "").strip()
    if not raw:
        return False, "empty"
    try:
        p = urllib.parse.urlsplit(raw)
    except ValueError:
        return False, "unparseable"
    if p.scheme.lower() != "https":
        return False, "must be https"
    if p.username is not None or p.password is not None:
        return False, "must not contain userinfo"
    if p.query or p.fragment:
        return False, "must not contain query/fragment"
    try:
        port = p.port
    except ValueError:
        return False, "invalid port"
    if port not in (None, 443):
        return False, "port must be 443"
    host = (p.hostname or "").lower()
    if not host:
        return False, "missing host"
    if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_HOST_SUFFIXES):
        return False, "loopback/internal hostname"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True, ""                       # 主机名 → 登记期放行,运行时再解析核验
    return (True, "") if _ip_is_public(ip) else (False, "private/loopback/link-local IP")


def guard_outbound_host(host: str, *, official, _resolver=None) -> None:
    """出站前护栏:**官方/组织可信 host 放行(无解析、无成本)**;非官方(用户自填 custom)host →
    assert_resolves_public(解析核验全公网,防 DNS rebinding)。official=该 provider 的官方 host 集
    (含组织 config 的可信 host,如内网网关)。让官方路径零回归、只对用户自填 host 付解析代价。"""
    h = (host or "").strip().lower()
    if h and h in {str(o).strip().lower() for o in (official or ())}:
        return
    assert_resolves_public(h, _resolver=_resolver)


def assert_resolves_public(host: str, *, _resolver=None) -> None:
    """运行时护栏:解析主机名 → **全部**解析 IP 必须公网,否则抛 ValueError(挡 DNS rebinding)。

    caller 在向用户登记的自定义 host 发出站请求**前**调用。_resolver 便于测试注入(默认 getaddrinfo)。"""
    h = (host or "").strip().lower()
    if not h:
        raise ValueError("missing host")
    if h in _BLOCKED_HOSTS or h.endswith(_BLOCKED_HOST_SUFFIXES):
        raise ValueError("loopback/internal hostname")
    # IP 字面量直接判定(公网→放行;非公网→拒)
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        ip = None
    if ip is not None:
        if not _ip_is_public(ip):
            raise ValueError("non-public IP literal")
        return
    # 主机名 → 解析,全部解析 IP 必须公网
    resolver = _resolver or (lambda name: socket.getaddrinfo(name, None))
    try:
        infos = resolver(h)
    except Exception as e:  # noqa: BLE001 — 解析失败当作不可信
        raise ValueError(f"host resolution failed: {e}") from e
    addrs = {info[4][0] for info in infos} if infos else set()
    if not addrs:
        raise ValueError("host did not resolve")
    for a in addrs:
        if not _ip_is_public(ipaddress.ip_address(a)):
            raise ValueError(f"resolves to non-public IP {a}")


class GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """每一跳重定向都重新过护栏。

    ## 为什么必须有

    上面两道护栏都在**发请求前**跑一次,而 `urllib.request.urlopen` 默认跟随 302 ——
    于是一个登记完全合规的公网 host,只要回一个 `Location: http://169.254.169.254/...`,
    请求就被带进内网,护栏一次都不会再被问到。实测确认过:urlopen 确实跟了过去。

    这不是理论风险:登记面已经放宽到用户自填任意公网 host,而 302 的内容由对面决定。
    「登记时是安全的」和「整条请求链是安全的」是两件事,只做前者等于没做。

    ## 判据

      - 不许 https → http 降级(降级后连传输层保护都没了,还常常是内网入口)
      - 目标 host 必须过 assert_resolves_public(与运行时护栏同一套判据,不另立标准)

    任一不满足就抛 ValueError 中断整条请求,而不是"跳过这一跳继续" —— 被重定向到不该去的
    地方本身就是异常信号,静默降级只会把它藏起来。
    """

    def __init__(self, *, _resolver=None) -> None:
        super().__init__()
        self._resolver = _resolver

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        scheme = (parsed.scheme or "").lower()
        if req.type == "https" and scheme != "https":
            raise ValueError(f"redirect downgrades https to {scheme or '(none)'}: {newurl}")
        if scheme not in ("http", "https"):
            raise ValueError(f"redirect to non-http(s) scheme: {newurl}")
        assert_resolves_public(parsed.hostname or "", _resolver=self._resolver)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def guarded_opener(*, _resolver=None) -> urllib.request.OpenerDirector:
    """出站用的 opener:跟随重定向,但每一跳都过护栏。

    所有对外 HTTP 都该走它,而不是裸 `urllib.request.urlopen` —— 裸调用会静默跟随重定向。
    """
    return urllib.request.build_opener(GuardedRedirectHandler(_resolver=_resolver))
