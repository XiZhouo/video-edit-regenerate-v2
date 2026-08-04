"""模型健康冷却(ProviderHub §3.5 步5:解析期施加健康/冷却状态)。

resolver 是纯函数、无 I/O;冷却**状态**由接线层从 InvocationReceipt(用户近期实际调用回执)算出后传入
——某模型近期连续失败(rate_limited / provider_unreachable / quota_blocked)→ 进冷却,resolver 解析期把它
**临时置灰**(available=False + 该错误码),不等到真调用才失败。冷却是软的:窗口过了自然恢复;pin 该模型
仍硬阻断提示(不静默换)。

`cooldown_reasons(receipts, now, ...)` 纯函数:近期回执 → {model_id: error_code}(进冷却的模型 → 最近错误码)。
"""

from __future__ import annotations

from typing import Any, Iterable

# 触发冷却的错误码(瞬态/额度类;request_invalid 等调用方问题不冷却整个模型)。
_COOLDOWN_CODES = frozenset({"rate_limited", "provider_unreachable", "quota_blocked", "credential_expired"})


def cooldown_reasons(receipts: Iterable[dict[str, Any]], *, now: float,
                     window_sec: float = 300.0, min_errors: int = 2) -> dict[str, str]:
    """近期回执 → 进冷却的 {model_id: 最近错误码}。

    规则:窗口内(created_at >= now-window_sec)某模型 status=error 且 error_code ∈ 冷却码,累计 >= min_errors
    次 → 进冷却。取该模型窗口内**最近一次**冷却类错误的 error_code 作展示原因。窗口内有过成功(ok)则不冷却
    (说明已恢复)。非 dict / 缺字段跳过。"""
    lo = now - window_sec
    err_count: dict[str, int] = {}
    last_code: dict[str, tuple[float, str]] = {}   # model → (created_at, code) 最近一次冷却错误
    recovered: set[str] = set()
    for r in receipts:
        if not isinstance(r, dict):
            continue
        ts = r.get("created_at")
        if not isinstance(ts, (int, float)) or ts < lo:
            continue
        model = str(r.get("model_id") or "")
        if not model:
            continue
        if r.get("status") == "ok":
            recovered.add(model)                    # 窗口内成功过 → 视为已恢复,不冷却
            continue
        code = str(r.get("error_code") or "")
        if code in _COOLDOWN_CODES:
            err_count[model] = err_count.get(model, 0) + 1
            prev = last_code.get(model)
            if prev is None or ts >= prev[0]:
                last_code[model] = (ts, code)
    return {m: last_code[m][1] for m, n in err_count.items()
            if n >= min_errors and m not in recovered and m in last_code}
