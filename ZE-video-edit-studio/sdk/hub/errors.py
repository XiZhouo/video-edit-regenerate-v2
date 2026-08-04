"""ProviderHub 统一错误码(规划 §3.5)—— 一次成型,对齐前后端 + 文案 key。

规划红线:每个错误必须含 reason、fix.action、fix.target、用户文案 key;禁止只给栈或 HTTP
状态码。这里把散在 server(not_bound/not_configured/version_blocked)、model.py
(requires_credential)、aiRoutes.ts(credential_stale/region_blocked/quota_low)的**裂脑**
reason 收敛为 9 类权威码,并给既有码 → 新码的映射,让接线层一次对齐、i18n
`aiRoutes.reason.${code}` 不再静默落空(侦察 risk[4])。
"""

from __future__ import annotations

from typing import Any, Optional


# ── 9 类统一错误码(规划 §3.5)────────────────────────────────────────────────
REQUIRES_CONNECTION = "requires_connection"      # 未连接任何能满足该能力的来源
CREDENTIAL_EXPIRED = "credential_expired"        # 连了但凭证过期/需重新登录
MODEL_UNAVAILABLE = "model_unavailable"          # 指定模型当前不可用(含 pin 失效、版本闸)
CAPABILITY_MISMATCH = "capability_mismatch"      # 连接/模型不具备要求的细能力
QUOTA_BLOCKED = "quota_blocked"                  # 配额/预算不足
PROVIDER_UNREACHABLE = "provider_unreachable"    # provider 网络不可达(真调用时)
RATE_LIMITED = "rate_limited"                    # 被限流
REQUEST_INVALID = "request_invalid"              # 请求本身非法(含服务未配置)
POLICY_DENIED = "policy_denied"                  # 被 entitlement / 权限 / 地区策略拒绝

HUB_ERROR_CODES = (
    REQUIRES_CONNECTION, CREDENTIAL_EXPIRED, MODEL_UNAVAILABLE, CAPABILITY_MISMATCH,
    QUOTA_BLOCKED, PROVIDER_UNREACHABLE, RATE_LIMITED, REQUEST_INVALID, POLICY_DENIED,
)

# 每码的默认 fix 动作 + 文案 key(前端 t(`ai.error.${code}`) 与之对齐;fix.target 由现场补)。
_DEFAULT_FIX_ACTION = {
    REQUIRES_CONNECTION: "connect",
    CREDENTIAL_EXPIRED: "reauth",
    MODEL_UNAVAILABLE: "choose_other_model",
    CAPABILITY_MISMATCH: "connect",
    QUOTA_BLOCKED: "review_quota",
    PROVIDER_UNREACHABLE: "retry",
    RATE_LIMITED: "retry_later",
    REQUEST_INVALID: "fix_request",
    POLICY_DENIED: "review_policy",
}

# 既有码 → 9 类新码(接线层收敛既有分散 reason;侦察给出的映射)。
LEGACY_CODE_MAP = {
    # model.py / 命令层
    "requires_credential": REQUIRES_CONNECTION,
    "not_bound": REQUIRES_CONNECTION,
    "not_configured": REQUEST_INVALID,
    "needs_reauth": CREDENTIAL_EXPIRED,
    "version_blocked": MODEL_UNAVAILABLE,
    "model_scope_denied": POLICY_DENIED,
    # aiRoutes.ts 前端旧枚举
    "credential_stale": CREDENTIAL_EXPIRED,
    "region_blocked": POLICY_DENIED,
    "quota_low": QUOTA_BLOCKED,
    "service_unreachable": PROVIDER_UNREACHABLE,
}


def normalize_code(legacy_or_new: Optional[str]) -> Optional[str]:
    """把任意既有/前端旧 reason 归一到 9 类权威码;已是新码则原样;None → None。"""
    if legacy_or_new is None:
        return None
    if legacy_or_new in HUB_ERROR_CODES:
        return legacy_or_new
    return LEGACY_CODE_MAP.get(legacy_or_new, legacy_or_new)


def i18n_key(code: str) -> str:
    return f"ai.error.{code}"


def make_fix(code: str, **target: Any) -> dict[str, Any]:
    """构造 {action, target...} 修复入口。target 是「一步到正确设置位置」的定位信息
    (provider / capability / model_id / config key 等)。"""
    fix: dict[str, Any] = {"action": _DEFAULT_FIX_ACTION.get(code, "connect")}
    fix.update({k: v for k, v in target.items() if v is not None})
    return fix
