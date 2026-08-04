"""test_resolver.py — 4 策略 resolver 测试.

覆盖:
  - reject: current == base → 返 incoming; != → 抛 ZeError
  - last-write-wins: 永远返 incoming
  - three-way-merge: 5 个分支
  - crdt: 传 crdt_merge / 不传抛 NotImplementedError
  - 别名兼容: last_write_wins / three_way_merge
  - 未知 strategy 抛 validation_failed
"""

from __future__ import annotations

import pytest

from resolver import resolve, ALL_STRATEGIES
from entity import ZeError


# ==============================================================
# reject
# ==============================================================

def test_reject_passes_when_unchanged():
    """current == base → return incoming."""
    assert resolve(strategy="reject", base="X", current="X", incoming="Y") == "Y"


def test_reject_raises_when_changed():
    """current != base → 抛 cas_version_mismatch."""
    with pytest.raises(ZeError) as exc:
        resolve(strategy="reject", base="X", current="A", incoming="Y")
    assert exc.value.code == "cas_version_mismatch"
    assert exc.value.context["base"] == "X"
    assert exc.value.context["current"] == "A"


def test_reject_passes_when_unchanged_dict():
    """复合 value 也支持; 用 == 比较."""
    base = {"a": 1, "b": 2}
    cur = {"a": 1, "b": 2}
    inc = {"a": 1, "b": 3}
    assert resolve(strategy="reject", base=base, current=cur, incoming=inc) == inc


# ==============================================================
# last-write-wins
# ==============================================================

def test_lww_returns_incoming():
    assert resolve(strategy="last-write-wins",
                   base="X", current="A", incoming="Y") == "Y"


def test_lww_alias_underscore():
    """下划线变体也接受."""
    assert resolve(strategy="last_write_wins",
                   base="X", current="A", incoming="Y") == "Y"


def test_lww_with_unchanged_current():
    """current == base 仍返 incoming."""
    assert resolve(strategy="last-write-wins",
                   base="X", current="X", incoming="Y") == "Y"


def test_lww_with_none_incoming():
    """LWW 不区分 None / value."""
    assert resolve(strategy="last-write-wins",
                   base="X", current="A", incoming=None) is None


# ==============================================================
# three-way-merge
# ==============================================================

def test_three_way_both_unchanged():
    """双方都没改 → base."""
    assert resolve(strategy="three-way-merge",
                   base="X", current="X", incoming="X") == "X"


def test_three_way_only_current_changed():
    """只 current 改了 (A 边改了, B 边没动) → current."""
    assert resolve(strategy="three-way-merge",
                   base="X", current="A", incoming="X") == "A"


def test_three_way_only_incoming_changed():
    """只 incoming 改了 (B 边改了, A 边没动) → incoming."""
    assert resolve(strategy="three-way-merge",
                   base="X", current="X", incoming="Y") == "Y"


def test_three_way_both_same_value():
    """双方都改且值相同 → current (碰巧改一样, 不算冲突)."""
    assert resolve(strategy="three-way-merge",
                   base="X", current="Y", incoming="Y") == "Y"


def test_three_way_both_different_raises():
    """双方都改且值不同 → 抛."""
    with pytest.raises(ZeError) as exc:
        resolve(strategy="three-way-merge",
                base="X", current="A", incoming="B")
    assert exc.value.code == "cas_version_mismatch"
    assert "both sides changed" in str(exc.value.args[0])


def test_three_way_alias_underscore():
    assert resolve(strategy="three_way_merge",
                   base="X", current="X", incoming="Y") == "Y"


def test_three_way_with_dicts():
    """复合 value 用 == 比较."""
    base = {"a": 1}
    cur = {"a": 1, "b": 2}      # A 加了 b
    inc = {"a": 1}              # B 没动
    assert resolve(strategy="three-way-merge",
                   base=base, current=cur, incoming=inc) == cur


# ==============================================================
# crdt
# ==============================================================

def test_crdt_without_merge_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        resolve(strategy="crdt", base="X", current="A", incoming="Y")


def test_crdt_with_custom_merge():
    """传 crdt_merge fn 就用 caller 提供的."""
    merge_fn = lambda c, i: c | i   # set union
    result = resolve(
        strategy="crdt",
        base=set(),
        current={"a", "b"},
        incoming={"a", "c"},
        crdt_merge=merge_fn,
    )
    assert result == {"a", "b", "c"}


def test_crdt_merge_takes_current_and_incoming():
    """crdt_merge 应该收到 (current, incoming), 不含 base."""
    seen = []
    def merge_fn(c, i):
        seen.append((c, i))
        return c
    resolve(strategy="crdt", base="X", current="A", incoming="B", crdt_merge=merge_fn)
    assert seen == [("A", "B")]


def test_crdt_merge_can_return_anything():
    """crdt_merge 可返回任意 value."""
    assert resolve(strategy="crdt", base=1, current=2, incoming=3,
                   crdt_merge=lambda c, i: c + i) == 5


# ==============================================================
# 未知 strategy
# ==============================================================

def test_unknown_strategy_raises():
    with pytest.raises(ZeError) as exc:
        resolve(strategy="bogus", base="X", current="X", incoming="Y")
    assert exc.value.code == "validation_failed"
    assert "bogus" in str(exc.value.args[0])


def test_typo_strategy_raises():
    """常见拼错也抛错 (而不是 silent fail)."""
    with pytest.raises(ZeError):
        resolve(strategy="LWW", base="X", current="X", incoming="Y")    # 大小写敏感


# ==============================================================
# ALL_STRATEGIES 常量
# ==============================================================

def test_all_strategies_constant():
    """SPEC §2 4 个策略全部在 ALL_STRATEGIES 里."""
    assert "reject" in ALL_STRATEGIES
    assert "last-write-wins" in ALL_STRATEGIES
    assert "three-way-merge" in ALL_STRATEGIES
    assert "crdt" in ALL_STRATEGIES
    assert len(ALL_STRATEGIES) == 4


# ==============================================================
# 纯函数性质 (无副作用)
# ==============================================================

def test_resolve_is_pure_no_side_effect():
    """resolve 是纯函数: 输入不变."""
    base = {"a": 1}
    cur = {"a": 1, "b": 2}
    inc = {"a": 1}
    base_before = dict(base)
    cur_before = dict(cur)
    inc_before = dict(inc)

    resolve(strategy="three-way-merge", base=base, current=cur, incoming=inc)

    assert base == base_before
    assert cur == cur_before
    assert inc == inc_before


def test_resolve_can_be_called_repeatedly():
    """同输入应该同输出 (可重入)."""
    args = dict(strategy="three-way-merge", base="X", current="A", incoming="X")
    a = resolve(**args)
    b = resolve(**args)
    c = resolve(**args)
    assert a == b == c == "A"
