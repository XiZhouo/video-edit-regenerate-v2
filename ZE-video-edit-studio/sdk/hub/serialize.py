"""EffectiveCapabilitySnapshot → JSON(GET /api/ai/options 的返回体序列化)。

前端 CapabilityModelPicker 消费这个形状:options 恒含全部候选(available 真/假),不可用项带
9 类错误码 + fix(一步到正确设置位置)+ i18n key。是 /api/ai-routes 的严格超集。
纯函数、无 I/O;错误码经 errors.i18n_key 附文案 key,让前端 t(error_i18n) 对齐。
"""

from __future__ import annotations

from typing import Any

from . import errors
from .descriptors import descriptor_by_id, model_by_id
from .types import ModelBinding, EffectiveCapabilitySnapshot

# descriptor kind → 用户可读的**连接来源类别**(§3.6:订阅/API/组织/本地)。前端按 category 出 i18n 标签
# (ai.source.{category}),不把内部 kind/品牌暴露给用户。official 设备登录=订阅额度;用户填 endpoint 的
# openai/gemini/anthropic=用户自备 API key;内网服务=组织托管;local=本机(local-dev,暂无 descriptor)。
_SOURCE_CATEGORY_BY_KIND = {
    "official-cli": "subscription",
    "openai-compatible": "api",
    "gemini": "api",
    "anthropic": "api",
    "internal-service": "org",
    "local-cli": "local",
    "ollama": "local",
}


def _source_category(descriptor_id: str) -> str:
    """descriptor id → 来源类别(拿不到 descriptor → 'api' 兜底,不泄漏内部 kind)。"""
    d = descriptor_by_id(descriptor_id)
    kind = getattr(d, "kind", "") if d is not None else ""
    return _SOURCE_CATEGORY_BY_KIND.get(kind, "api")


# 来源类别 → **预估计费性质**(§3.6:给「预估计费方式」,但「不承诺不可靠的精确价格」)。
# 这是计费的**性质码**而非金额:订阅=额度内不单独计费;api=按量;组织提供=不向用户直接计费;
# 本地=无外部成本。前端出 i18n 文案 `ai.billing.{hint}`,不显示任何具体价格数字。
_BILLING_HINT_BY_CATEGORY = {
    "subscription": "subscription_quota",   # 订阅额度内
    "api": "metered",                       # 按量计费(用户自备 key)
    "org": "org_provided",                  # 组织托管提供
    "local": "local_free",                  # 本机运行,无外部计费
}


def _billing_hint(category: str) -> str:
    """来源类别 → 计费性质码(未知类别按最保守的按量兜底,不误报「免费」)。"""
    return _BILLING_HINT_BY_CATEGORY.get(category, "metered")


def binding_public_dict(b: ModelBinding) -> dict[str, Any]:
    d: dict[str, Any] = {
        "model_id": b.model_id,
        "model_label": b.model_label,
        "route": b.route,
        "descriptor_id": b.descriptor_id,
        "connection_id": b.connection_id,
        "available": b.available,
        "capabilities": list(b.capabilities),
        "reason": b.reason,
        "fix": b.fix,
        # §3.6 选项含「连接来源标签」:类别码,前端出 i18n 人读标签(订阅·API·组织·本地)。
        "source": _source_category(b.descriptor_id),
    }
    # §3.6 预估计费方式:从来源类别派生的计费性质码(非金额),前端 t(`ai.billing.${hint}`)。
    d["billing_hint"] = _billing_hint(d["source"])
    # §3.6 模型特有参数由选中 binding 的 schema 驱动:带上 param_schema 引用(无则省)。
    m = model_by_id(b.model_id)
    if m is not None and getattr(m, "param_schema_ref", ""):
        d["param_schema"] = m.param_schema_ref
    if b.reason:
        d["reason_i18n"] = errors.i18n_key(b.reason)
    return d


def snapshot_public_dict(snap: EffectiveCapabilitySnapshot) -> dict[str, Any]:
    """序列化快照。`options` 顺序即候选顺序(选择器按此渲染)。"""
    return {
        "feature_id": snap.feature_id,
        "capability": snap.capability,
        "options": [binding_public_dict(o) for o in snap.options],
        "pin": snap.pin,
        "current": snap.current,
        # 用户保存过的选择 + 它为什么用不了(§3.5:保留原值供用户看见,禁用执行并提示更换)。
        # 前端靠这两个字段做到「显示原值 + 置灰 + 提示」,而不是看见一个被悄悄换掉的 current。
        "preference": snap.preference,
        "preference_blocked": snap.preference_blocked,
        "has_available": snap.has_available,
        "trace": list(snap.trace),
    }
