"""G1 退出门的**机制半边**:一次配置、多工具共用(规划 §10 Batch 1 G1 / §3.1 单一用户入口)。

G1 要求「两个工具共享一次配置;Codex-only 测试账号只看到真实 Codex 能力」。端到端那半需要第二个
工具真接入(见 spawn 的接线任务),但**机制**这半可以在 resolver 层钉死:同一份用户连接集,喂给
不同 feature_id / capability 的 CapabilityRequirement,各自解析出各自的可用选项——工具之间不需要
各配一次,也不会互相污染。

这里锁的是 ProviderHub 的核心价值主张:**连接是用户级的,能力需求是工具级的**,二者正交。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import sdk  # noqa: E402,F401
from credentials import PROVIDER_CODEX_CLI, PROVIDER_GEMINI_API, STATUS_ACTIVE  # noqa: E402
from sdk.hub import (  # noqa: E402
    resolve, project_connections, CapabilityRequirement,
    CAP_IMAGE_GENERATE, AUDIENCE_PUBLIC, snapshot_public_dict,
)


def _pub(provider, *, caps=("image",), status=STATUS_ACTIVE):
    return {"id": f"conn-{provider}", "provider": provider, "label": provider,
            "capabilities": list(caps), "status": status, "metadata": {},
            "last4": "1234", "has_secret": True}


# 两个「工具」:同为 image 泳道但 feature_id 不同(批量生图 / 另一个消费者),模拟共享配置的两方。
TOOL_A = CapabilityRequirement(feature_id="foundry.image.generate", capability="image",
                               require=(CAP_IMAGE_GENERATE,))
TOOL_B = CapabilityRequirement(feature_id="second.tool.image.generate", capability="image",
                               require=(CAP_IMAGE_GENERATE,))


def _available(snap):
    return {o["model_id"] for o in snapshot_public_dict(snap)["options"] if o["available"]}


def test_one_connection_serves_both_tools():
    """用户配一次(单条 gemini 连接)→ 两个不同 feature_id 的工具都解析到同一批可用模型。"""
    conns = project_connections([_pub(PROVIDER_GEMINI_API)])
    a = resolve(TOOL_A, conns, deploy_audience=AUDIENCE_PUBLIC)
    b = resolve(TOOL_B, conns, deploy_audience=AUDIENCE_PUBLIC)
    assert a.has_available and b.has_available
    assert _available(a) == _available(b) and _available(a)     # 同一份配置,两工具同样可用


def test_codex_only_account_sees_only_codex_capabilities():
    """G1 断言:Codex-only 账号在**每个**工具里都只看到真实 Codex 能力,看不到未连接的 provider。"""
    conns = project_connections([_pub(PROVIDER_CODEX_CLI)])
    for req in (TOOL_A, TOOL_B):
        snap = resolve(req, conns, deploy_audience=AUDIENCE_PUBLIC)
        options = snapshot_public_dict(snap)["options"]
        available = [o for o in options if o["available"]]
        assert available, f"{req.feature_id}: Codex 连接下应有可用模型"
        # 可用项必须都由 codex 路线服务(不可用项没有落位路线,route 为空——所以只看可用项的 route)
        assert all(o["route"] == "codex-cli" for o in available), [o["route"] for o in available]
        # 未连接的 provider 的模型必须**不可用**,且给出可执行原因(不是静默消失也不是假装可用)
        unavailable = {o["model_id"]: o for o in options if not o["available"]}
        assert "gemini3" in unavailable, f"{req.feature_id}: 无 gemini 连接时 gemini3 不该可用"
        assert unavailable["gemini3"]["reason"] and unavailable["gemini3"]["fix"]


def test_tools_do_not_pollute_each_other_pins():
    """工具级选择(pin)互不串:A pin 一个模型,不改变 B 的解析结果。"""
    conns = project_connections([_pub(PROVIDER_GEMINI_API)])
    b_before = resolve(TOOL_B, conns, deploy_audience=AUDIENCE_PUBLIC).current
    a_models = sorted(_available(resolve(TOOL_A, conns, deploy_audience=AUDIENCE_PUBLIC)))
    assert a_models, "前置:A 应有可用模型"
    resolve(TOOL_A, conns, pin=a_models[0], deploy_audience=AUDIENCE_PUBLIC)
    b_after = resolve(TOOL_B, conns, deploy_audience=AUDIENCE_PUBLIC).current
    assert b_before == b_after                                   # B 不受 A 的 pin 影响


def test_adding_a_connection_lifts_all_tools_at_once():
    """再连一个 provider → 两个工具的可用集**同时**扩大(配置是用户级的,不是各工具各存一份)。"""
    only_codex = project_connections([_pub(PROVIDER_CODEX_CLI)])
    both = project_connections([_pub(PROVIDER_CODEX_CLI), _pub(PROVIDER_GEMINI_API)])
    for req in (TOOL_A, TOOL_B):
        before = _available(resolve(req, only_codex, deploy_audience=AUDIENCE_PUBLIC))
        after = _available(resolve(req, both, deploy_audience=AUDIENCE_PUBLIC))
        assert before < after, f"{req.feature_id}: 加连接后可用集应扩大 {sorted(before)} → {sorted(after)}"


def test_no_connection_blocks_both_tools_with_actionable_reason():
    """零连接:两个工具都空态,且每个不可用项都带 9 类错误码 + fix(不是空白失败)。"""
    conns = project_connections([])
    for req in (TOOL_A, TOOL_B):
        snap = resolve(req, conns, service_configured=False, deploy_audience=AUDIENCE_PUBLIC)
        assert not snap.has_available
        for o in snapshot_public_dict(snap)["options"]:
            assert o["reason"], f"{req.feature_id}: 不可用项必须给原因"
            assert o["fix"], f"{req.feature_id}: 不可用项必须给修复入口"
