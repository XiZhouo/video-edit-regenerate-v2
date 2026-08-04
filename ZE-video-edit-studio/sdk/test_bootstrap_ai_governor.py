"""sdk/test_bootstrap_ai_governor.py — P1 接线单测:ze.ai.* config → 运行时 AiGovernor。

不启 server / 不建 project:只验证 (a) seed_framework_config 声明了 ze.ai.* 旋钮、
(b) `_build_ai_governor` 据 config 正确启停,(c) 构造出的 governor 行为符合旋钮(成本 0=不限
映射成 None、强制溯源、安全过滤、人审阈值)—— 即「config 一开,治理真生效」。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sdk import AiGovernor, ConfigRegistry, seed_framework_config, ZeError  # noqa: E402
from sdk.bootstrap import _build_ai_governor  # noqa: E402


def _seeded(**overrides) -> ConfigRegistry:
    reg = ConfigRegistry()
    seed_framework_config(reg)
    if overrides:
        rejected = reg.set_values(overrides)
        assert not rejected, rejected
    return reg


class _Spec:
    """最小 CommandSpec 替身(guard 只读 id/ai_cost_usd/ai_high_risk)。"""

    def __init__(self, *, ai_cost_usd: float = 0.0, ai_high_risk: bool = False):
        self.id = "c"
        self.ai_cost_usd = ai_cost_usd
        self.ai_high_risk = ai_high_risk


# ---- (a) seed 声明 ----

def test_seed_declares_ai_knobs_with_safe_defaults():
    reg = _seeded()
    # 默认:治理关闭(opt-in)、强制溯源、成本不限/不按成本触发人审
    assert reg.get("ze.ai.governance_enabled") is False
    assert reg.get("ze.ai.require_provenance") is True
    assert reg.get("ze.ai.per_call_cap_usd") == 0.0
    assert reg.get("ze.ai.approval_threshold_usd") == 0.0


# ---- (b) 启停 ----

def test_disabled_by_default_returns_none():
    """默认治理关闭 → None → CommandRegistry 不挂 governor、dispatch 行为不变(向后兼容)。"""
    assert _build_ai_governor(_seeded(), None) is None


def test_enabled_builds_governor():
    g = _build_ai_governor(_seeded(**{"ze.ai.governance_enabled": True}), None)
    assert isinstance(g, AiGovernor)


# ---- (c) 旋钮→行为 ----

def test_zero_cap_does_not_block_costly_call():
    """per_call_cap=0 必须映射成 None(不限);否则 0.0 会把任何正成本命令拦死。"""
    g = _build_ai_governor(_seeded(**{
        "ze.ai.governance_enabled": True,
        "ze.ai.require_provenance": False,
    }), None)
    d = g.guard(_Spec(ai_cost_usd=99.0), {}, actor=None)
    assert d.require_confirm is False  # 不限 + 不按成本触发人审 → 直接放行


def test_positive_cap_blocks_over_budget():
    g = _build_ai_governor(_seeded(**{
        "ze.ai.governance_enabled": True,
        "ze.ai.require_provenance": False,
        "ze.ai.per_call_cap_usd": 0.5,
    }), None)
    with pytest.raises(ZeError) as ei:
        g.guard(_Spec(ai_cost_usd=1.0), {}, actor=None)
    assert ei.value.code == "ai_cost_exceeded"


def test_approval_threshold_triggers_confirm():
    g = _build_ai_governor(_seeded(**{
        "ze.ai.governance_enabled": True,
        "ze.ai.require_provenance": False,
        "ze.ai.approval_threshold_usd": 0.2,
    }), None)
    d = g.guard(_Spec(ai_cost_usd=0.3), {}, actor=None)
    assert d.require_confirm is True


def test_require_provenance_default_enforced():
    g = _build_ai_governor(_seeded(**{"ze.ai.governance_enabled": True}), None)
    with pytest.raises(ZeError) as ei:
        g.guard(_Spec(), {}, actor=None)
    assert ei.value.code == "ai_provenance_required"


def test_require_provenance_knob_off():
    g = _build_ai_governor(_seeded(**{
        "ze.ai.governance_enabled": True,
        "ze.ai.require_provenance": False,
    }), None)
    assert g.guard(_Spec(), {}, actor=None).require_confirm is False  # 不带 provenance 也放行


def test_moderator_threaded_from_mod_hook():
    """moderator(安全过滤 callable)经 mod 钩注入 → governor 真生效。"""
    g = _build_ai_governor(_seeded(**{
        "ze.ai.governance_enabled": True,
        "ze.ai.require_provenance": False,
    }), lambda text: "bad" not in text)
    with pytest.raises(ZeError) as ei:
        g.guard(_Spec(), {"p": "very bad"}, actor=None)
    assert ei.value.code == "ai_safety_blocked"
