"""Provider 探活测试(ProviderHub 回收 P3:HTTP 健康分类 + GET,SSRF 边界在 caller)。"""

from __future__ import annotations

import urllib.error

import pytest

from sdk.hub import errors, probe
from sdk.hub.probe import (
    classify_http_status, probe_url, ProbeResult, build_probe_request, is_probeable,
    discover_models, parse_discovered_models,
)


@pytest.mark.parametrize("status,ok,code", [
    (200, True, None), (201, True, None), (204, True, None),
    (401, False, errors.CREDENTIAL_EXPIRED), (403, False, errors.CREDENTIAL_EXPIRED),
    (429, False, errors.RATE_LIMITED),
    (500, False, errors.PROVIDER_UNREACHABLE), (503, False, errors.PROVIDER_UNREACHABLE),
    (404, False, errors.REQUEST_INVALID), (400, False, errors.REQUEST_INVALID),
])
def test_classify_http_status(status, ok, code):
    assert classify_http_status(status) == (ok, code)


class _FakeResp:
    def __init__(self, status): self.status = status
    def read(self): return b"{}"
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _FakeRespBody:
    def __init__(self, body, status=200): self.status = status; self._body = body
    def read(self): return self._body.encode("utf-8")
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_probe_url_ok(monkeypatch):
    monkeypatch.setattr(probe, "_guarded_open", lambda req, *, timeout=15: _FakeResp(200))
    r = probe_url("https://api.openai.com/v1/models", headers={"Authorization": "Bearer x"})
    assert r.ok and r.health == "ok" and r.error_code is None
    assert r.latency_ms is not None and r.latency_ms >= 0


def test_probe_url_http_error_maps_code(monkeypatch):
    def _raise(req, timeout=15):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
    monkeypatch.setattr(probe, "_guarded_open", _raise)
    r = probe_url("https://api.openai.com/v1/models")
    assert not r.ok and r.health == "error" and r.error_code == errors.CREDENTIAL_EXPIRED
    assert "401" in r.detail


def test_probe_url_network_failure_is_unreachable(monkeypatch):
    def _raise(req, timeout=15):
        raise urllib.error.URLError("dns fail")
    monkeypatch.setattr(probe, "_guarded_open", _raise)
    r = probe_url("https://api.openai.com/v1/models")
    assert not r.ok and r.error_code == errors.PROVIDER_UNREACHABLE


def test_probe_result_carries_no_secret(monkeypatch):
    # 结果对象只携带 health/detail/latency,无任何 secret 字段(脱敏红线)
    r = ProbeResult(ok=True, health="ok")
    assert set(vars(r)) == {"ok", "health", "error_code", "detail", "latency_ms"}


def test_build_probe_request_openai_bearer():
    url, headers = build_probe_request("openai-compatible", "https://api.openai.com/", "sk-x")
    assert url == "https://api.openai.com/v1/models"          # 去尾斜杠 + test_path
    assert headers == {"Authorization": "Bearer sk-x"}


def test_build_probe_request_anthropic_headers():
    url, headers = build_probe_request("anthropic", "https://api.anthropic.com", "ak")
    assert url == "https://api.anthropic.com/v1/models"
    assert headers["x-api-key"] == "ak" and "anthropic-version" in headers


def test_build_probe_request_gemini_key_in_query():
    url, headers = build_probe_request("gemini", "https://generativelanguage.googleapis.com", "g/k+1")
    assert url.startswith("https://generativelanguage.googleapis.com/v1beta/models?")
    assert "key=g%2Fk%2B1" in url                             # secret URL-quote
    assert headers == {}


def test_build_probe_request_unprobeable_kind_returns_none():
    # 官方 CLI / 内网服务无用户端点 → None(caller 记 not_applicable)
    assert build_probe_request("official-cli", "x", "s") is None
    assert build_probe_request("internal-service", "x", "s") is None


def test_parse_discovered_models_openai_shape():
    payload = {"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}, {"id": "gpt-4o"}]}
    assert parse_discovered_models(payload, "openai-compatible") == ["gpt-4o", "gpt-4o-mini"]


def test_parse_discovered_models_gemini_strips_prefix():
    payload = {"models": [{"name": "models/gemini-3-pro"}, {"name": "models/gemini-flash"}]}
    assert parse_discovered_models(payload, "gemini") == ["gemini-3-pro", "gemini-flash"]


def test_parse_discovered_models_bad_shape_empty():
    assert parse_discovered_models("junk", "openai-compatible") == []
    assert parse_discovered_models({"data": ["x", None]}, "openai-compatible") == []


def test_discover_models_ok(monkeypatch):
    monkeypatch.setattr(probe, "_guarded_open",
                        lambda req, *, timeout=15: _FakeRespBody('{"data":[{"id":"m1"},{"id":"m2"}]}'))
    ids, code = discover_models("https://api.openai.com/v1/models", kind="openai-compatible")
    assert ids == ["m1", "m2"] and code is None


def test_discover_models_http_error_returns_code(monkeypatch):
    def _raise(req, timeout=15):
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)
    monkeypatch.setattr(probe, "_guarded_open", _raise)
    ids, code = discover_models("https://api.openai.com/v1/models", kind="openai-compatible")
    assert ids == [] and code == errors.CREDENTIAL_EXPIRED


def test_discover_models_bad_json_is_request_invalid(monkeypatch):
    monkeypatch.setattr(probe, "_guarded_open", lambda req, *, timeout=15: _FakeRespBody("not json"))
    ids, code = discover_models("https://api.openai.com/v1/models", kind="openai-compatible")
    assert ids == [] and code == errors.REQUEST_INVALID


def test_is_probeable_gates_reveal():
    # caller 用 is_probeable 在 reveal_secret 前判定,不可探活就不解密(最小暴露)
    assert is_probeable("openai-compatible") and is_probeable("anthropic") and is_probeable("gemini")
    assert not is_probeable("official-cli") and not is_probeable("internal-service")
    # 与 build_probe_request 判定一致
    assert (build_probe_request("official-cli", "x", "s") is None) == (not is_probeable("official-cli"))
