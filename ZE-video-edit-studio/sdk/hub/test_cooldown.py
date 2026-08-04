"""模型健康冷却测试(§3.5 步5:近期失败模型解析期临时置灰)。"""
from __future__ import annotations
from sdk.hub.cooldown import cooldown_reasons


def _r(model="gemini3", status="error", code="rate_limited", ts=1000.0):
    d = {"model_id": model, "status": status, "created_at": ts}
    if code is not None:
        d["error_code"] = code
    return d


def test_min_errors_threshold():
    # 1 次 < 阈值2 → 不冷却;2 次 → 冷却
    assert cooldown_reasons([_r(ts=1000)], now=1001, min_errors=2) == {}
    assert cooldown_reasons([_r(ts=1000), _r(ts=1001)], now=1002, min_errors=2) == {"gemini3": "rate_limited"}


def test_window_expiry():
    assert cooldown_reasons([_r(ts=1000), _r(ts=1001)], now=99999, window_sec=300) == {}


def test_recovered_if_ok_in_window():
    rs = [_r(ts=1000), _r(ts=1001), _r(status="ok", code=None, ts=1002)]
    assert cooldown_reasons(rs, now=1003, window_sec=300) == {}   # 窗口内成功过 → 已恢复


def test_only_transient_codes_cool():
    # request_invalid(调用方问题)不冷却整个模型
    rs = [_r(code="request_invalid", ts=1000), _r(code="request_invalid", ts=1001)]
    assert cooldown_reasons(rs, now=1002) == {}
    # quota_blocked / provider_unreachable / credential_expired 冷却
    for code in ("quota_blocked", "provider_unreachable", "credential_expired"):
        rs2 = [_r(code=code, ts=1000), _r(code=code, ts=1001)]
        assert cooldown_reasons(rs2, now=1002) == {"gemini3": code}


def test_latest_code_shown():
    rs = [_r(code="rate_limited", ts=1000), _r(code="provider_unreachable", ts=1001)]
    assert cooldown_reasons(rs, now=1002) == {"gemini3": "provider_unreachable"}   # 取最近


def test_tolerates_junk():
    assert cooldown_reasons(["x", None, {}, _r()], now=1001, min_errors=1) == {"gemini3": "rate_limited"}
