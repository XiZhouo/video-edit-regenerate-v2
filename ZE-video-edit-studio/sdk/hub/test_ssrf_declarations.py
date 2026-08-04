"""§5.3 SSRF 边界声明健全性(ProviderHub 硬门覆盖之一)。

endpoint_policy 是「SSRF 边界的唯一权威」(types.py:67 base_url 允许清单)。本测试诚实地钉死**声明**
层不变量:用户可自填 base_url 的 provider(openai-compatible/gemini/anthropic)必须声明非空
base_url_allowlist,且清单只含具体**外网 host**(无通配符、无 scheme/path、无内网/本机地址);
official-cli 走设备登录无用户 endpoint、internal-service 走服务端 config_key —— 都不该有用户 allowlist。

注:这是**声明**层守卫(防新增 descriptor 时 allowlist 缺失/写错)。对用户自填 base_url 的**运行时**
执行校验须消费同一 endpoint_policy 权威(§4.4);运行时校验若接线,应另有针对性测试,不由本文件冒充。
"""

from __future__ import annotations

from sdk.hub import all_provider_descriptors

# 用户可填 base_url 的 provider kind(须 allowlist 兜 SSRF)。
_USER_ENDPOINT_KINDS = {"openai-compatible", "gemini", "anthropic"}
# 内网/本机地址前缀(allowlist 出现即 SSRF 隐患)。
_INTERNAL_MARKERS = ("localhost", "127.", "0.0.0.0", "10.", "192.168.", "172.16.", "169.254.", "::1")


def test_user_endpoint_descriptors_declare_allowlist():
    for d in all_provider_descriptors():
        if d.kind in _USER_ENDPOINT_KINDS:
            allow = d.endpoint_policy.get("base_url_allowlist")
            assert allow and all(isinstance(h, str) and h for h in allow), \
                f"{d.id}: 用户可配 endpoint 的 provider 必须声明非空 base_url_allowlist(SSRF 边界唯一权威)"


def test_allowlist_hosts_are_specific_external():
    for d in all_provider_descriptors():
        for h in (d.endpoint_policy.get("base_url_allowlist") or ()):
            assert "*" not in h, f"{d.id}: allowlist 不得含通配符: {h!r}"
            assert "://" not in h and "/" not in h, f"{d.id}: allowlist 应为纯 host,不含 scheme/path: {h!r}"
            assert not any(h == m or h.startswith(m) for m in _INTERNAL_MARKERS), \
                f"{d.id}: allowlist 不得含内网/本机地址(SSRF): {h!r}"


def test_cli_and_internal_descriptors_have_no_user_allowlist():
    for d in all_provider_descriptors():
        if d.kind == "official-cli":
            assert d.endpoint_policy.get("official_login") is True, f"{d.id}: official-cli 应走设备登录"
            assert "base_url_allowlist" not in d.endpoint_policy, \
                f"{d.id}: official-cli 无用户 endpoint,不应有 allowlist"
        if d.kind == "internal-service":
            assert "config_key" in d.endpoint_policy, f"{d.id}: internal-service 应由服务端 config_key 配"
            assert d.endpoint_policy.get("audience") == "internal", \
                f"{d.id}: internal-service 应标 audience=internal(public 隐藏)"


def test_every_descriptor_declares_a_recognized_kind():
    known = _USER_ENDPOINT_KINDS | {"official-cli", "internal-service"}
    for d in all_provider_descriptors():
        assert d.kind in known, f"{d.id}: 未识别 kind {d.kind!r}(新增 kind 须补 SSRF 声明规则)"
