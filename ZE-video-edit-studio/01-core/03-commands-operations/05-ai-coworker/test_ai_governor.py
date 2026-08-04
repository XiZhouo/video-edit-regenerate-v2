"""AiGovernor 单测 — source='ai' 命令治理(组合既有原语)。

跑法:从仓根用 audit 注入路径,或 `pytest --import-mode=importlib`。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 叶子路径注入(与 audit_gates 同款,便于独立 pytest)
_CORE = Path(__file__).resolve().parents[2]
for rel in ("01-data-structure/02-entity", "01-data-structure/01-document",
            "03-commands-operations/01-command", "03-commands-operations/02-operation",
            "03-commands-operations/05-ai-coworker"):
    p = str((_CORE / rel).resolve())
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest  # noqa: E402
from ai_governor import AiGovernor, GuardDecision  # noqa: E402
from command import CommandRegistry, CommandSpec  # noqa: E402
from document import Document  # noqa: E402
from entity import Actor, ZeError  # noqa: E402
from operation import new_operation, PendingConfirmation  # noqa: E402


def _reg(governor, **spec_extra):
    reg = CommandRegistry(ai_governor=governor)
    reg.register(CommandSpec(
        id="c", label="x", sources_allowed=("user", "ai"),
        execute=lambda **kw: new_operation(
            command_id="c", source=kw["source"], actor=kw["actor"].to_dict(),
            patches=[], reverse_patches=[]),
        **spec_extra))
    return reg, Document(schema_version="1.0.0", collaboration_mode="with_save")


def test_provenance_required():
    reg, doc = _reg(AiGovernor(require_provenance=True))
    with pytest.raises(ZeError) as ei:
        reg.dispatch("c", {}, actor=Actor(user_id="u"), source="ai", doc=doc)
    assert ei.value.code == "ai_provenance_required"
    # 带 provenance 放行
    op = reg.dispatch("c", {"provenance": {"actor": "u", "model": "m"}},
                      actor=Actor(user_id="u"), source="ai", doc=doc)
    assert op.command_id == "c"


def test_safety_blocks():
    reg, doc = _reg(AiGovernor(moderator=lambda t: "bad" not in t, require_provenance=False))
    with pytest.raises(ZeError) as ei:
        reg.dispatch("c", {"p": "very bad"}, actor=Actor(user_id="u"), source="ai", doc=doc)
    assert ei.value.code == "ai_safety_blocked"
    assert reg.dispatch("c", {"p": "fine"}, actor=Actor(user_id="u"), source="ai", doc=doc)


def test_cost_cap():
    reg, doc = _reg(AiGovernor(per_call_cap_usd=0.1, require_provenance=False), ai_cost_usd=0.5)
    with pytest.raises(ZeError) as ei:
        reg.dispatch("c", {}, actor=Actor(user_id="u"), source="ai", doc=doc)
    assert ei.value.code == "ai_cost_exceeded"
    # args.cost_usd 覆盖 spec 估算 → 低于 cap 放行
    assert reg.dispatch("c", {"cost_usd": 0.01}, actor=Actor(user_id="u"), source="ai", doc=doc)


def test_high_risk_needs_approval():
    reg, doc = _reg(AiGovernor(require_provenance=False), ai_high_risk=True)
    r = reg.dispatch("c", {}, actor=Actor(user_id="u"), source="ai", doc=doc)
    assert isinstance(r, PendingConfirmation)


def test_approval_threshold():
    reg, doc = _reg(AiGovernor(approval_threshold_usd=0.2, require_provenance=False), ai_cost_usd=0.3)
    r = reg.dispatch("c", {}, actor=Actor(user_id="u"), source="ai", doc=doc)
    assert isinstance(r, PendingConfirmation)


def test_user_source_unaffected():
    """governor 只治 source='ai';user 源完全不受影响(高风险 spec 也直接执行)。"""
    reg, doc = _reg(AiGovernor(require_provenance=True), ai_high_risk=True)
    op = reg.dispatch("c", {}, actor=Actor(user_id="u"), source="user", doc=doc)
    assert op.command_id == "c"


def test_no_governor_backward_compat():
    """不挂 governor → source='ai' 行为与历史一致(无额外治理)。"""
    reg = CommandRegistry()  # 无 governor
    reg.register(CommandSpec(
        id="c", label="x", sources_allowed=("user", "ai"),
        execute=lambda **kw: new_operation(
            command_id="c", source=kw["source"], actor=kw["actor"].to_dict(),
            patches=[], reverse_patches=[])))
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    op = reg.dispatch("c", {}, actor=Actor(user_id="u"), source="ai", doc=doc)
    assert op.command_id == "c"


def test_guard_decision_shape():
    d = GuardDecision(require_confirm=True)
    assert d.require_confirm is True
