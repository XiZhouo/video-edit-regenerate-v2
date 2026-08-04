"""ProviderHub §3.6 选项序列化契约锁 —— 来源类别标签 + param_schema 引用 + 不泄漏内部 kind。

binding_public_dict 是前端 CapabilityModelPicker 的数据契约。§3.6 要求:选项带**连接来源类别**
(订阅/API/组织/本地)而非内部 kind/品牌;模型特有参数由选中 binding 的 param_schema 驱动。本测试
把这份契约钉死,防回流/重构悄悄改字段或泄漏内部 descriptor kind。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import sdk  # noqa: E402,F401
from credentials import PROVIDER_CODEX_CLI, PROVIDER_GEMINI_API  # noqa: E402
from sdk.hub import (  # noqa: E402
    resolve, project_connections, CapabilityRequirement,
    CAP_IMAGE_GENERATE, AUDIENCE_PUBLIC, AUDIENCE_INTERNAL, snapshot_public_dict,
)
from sdk.hub.serialize import (  # noqa: E402
    _source_category, _SOURCE_CATEGORY_BY_KIND, _billing_hint, _BILLING_HINT_BY_CATEGORY,
)

GEN = CapabilityRequirement(feature_id="foundry.image.generate", capability="image",
                            require=(CAP_IMAGE_GENERATE,))

_CATEGORIES = {"subscription", "api", "org", "local"}
_RAW_KINDS = set(_SOURCE_CATEGORY_BY_KIND)   # 内部 kind 值,绝不能出现在 source 里


def _pub(provider, *, caps=("image",)):
    return {"id": f"conn-{provider}", "provider": provider, "label": provider,
            "capabilities": list(caps), "status": "active", "metadata": {},
            "last4": "1234", "has_secret": True}


def _opts(snap):
    return {o["model_id"]: o for o in snapshot_public_dict(snap)["options"]}


def test_every_option_carries_valid_source_category():
    snap = resolve(GEN, project_connections([]), service_configured=True,
                   deploy_audience=AUDIENCE_INTERNAL)
    for o in snapshot_public_dict(snap)["options"]:
        assert o["source"] in _CATEGORIES


def test_source_never_leaks_internal_kind():
    # source 是用户可读**类别**,绝不能是内部 descriptor kind(official-cli/gemini/internal-service…)
    snap = resolve(GEN, project_connections([_pub(PROVIDER_CODEX_CLI)]), deploy_audience=AUDIENCE_PUBLIC)
    for o in snapshot_public_dict(snap)["options"]:
        assert o["source"] not in _RAW_KINDS


def test_codex_source_is_subscription():
    snap = resolve(GEN, project_connections([_pub(PROVIDER_CODEX_CLI)]), deploy_audience=AUDIENCE_PUBLIC)
    assert _opts(snap)["fal-gpt-image-2"]["source"] == "subscription"   # 官方 CLI 设备登录=订阅额度


def test_gemini_source_is_api():
    snap = resolve(GEN, project_connections([_pub(PROVIDER_GEMINI_API)]), deploy_audience=AUDIENCE_PUBLIC)
    assert _opts(snap)["gemini3"]["source"] == "api"                    # 用户自备 API key


def test_param_schema_ref_flows_to_option():
    # §3.6:模型特有参数由 binding 的 param_schema 驱动 → 选项带 param_schema 引用
    snap = resolve(GEN, project_connections([]), service_configured=True,
                   deploy_audience=AUDIENCE_INTERNAL)
    opts = _opts(snap)
    assert opts["gemini3"]["param_schema"] == "foundry.banana"
    assert opts["fal-gpt-image-2"]["param_schema"] == "foundry.gpt"


def test_source_category_fallback_is_api():
    # 拿不到 descriptor(未知 id)→ 兜底 'api',不抛、不泄漏
    assert _source_category("nonexistent-descriptor") == "api"
    assert _source_category("") == "api"


_BILLING_HINTS = set(_BILLING_HINT_BY_CATEGORY.values())


def test_every_option_carries_billing_hint():
    # §3.6:每个选项带「预估计费方式」性质码(非金额)
    snap = resolve(GEN, project_connections([]), service_configured=True,
                   deploy_audience=AUDIENCE_INTERNAL)
    for o in snapshot_public_dict(snap)["options"]:
        assert o["billing_hint"] in _BILLING_HINTS


def test_billing_hint_maps_from_source():
    # 订阅=额度内;用户自备 API=按量
    snap = resolve(GEN, project_connections([_pub(PROVIDER_CODEX_CLI)]), deploy_audience=AUDIENCE_PUBLIC)
    assert _opts(snap)["fal-gpt-image-2"]["billing_hint"] == "subscription_quota"
    snap2 = resolve(GEN, project_connections([_pub(PROVIDER_GEMINI_API)]), deploy_audience=AUDIENCE_PUBLIC)
    assert _opts(snap2)["gemini3"]["billing_hint"] == "metered"


def test_billing_hint_fallback_is_metered_never_free():
    # 未知类别按最保守的按量兜底,绝不误报「本地免费」(不承诺不该承诺的免费)
    assert _billing_hint("unknown-category") == "metered"
    assert _billing_hint("") == "metered"
