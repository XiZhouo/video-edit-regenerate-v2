"""按轮释放挂起确认的归属链(§7.4 P0-E,W1-5 / 用户 D1 方案 b)。

【为什么要有这个测试】归属靠 `turn_id` 从 chat 端点一路传到 pending:
    /api/ai/chat 建 turn → mcp_url ?turn= → McpActorCtx.turn_id
      → cmd_reg.dispatch(turn_id=) → new_pending_confirmation(turn_id=)
        → PendingStore.tokens_for_turn → 取消端点逐个 cancel

这条链**中间断任何一跳都不会报错**,只会让 tokens_for_turn 恒返空 ——
「取消整轮释放挂起确认」变成一个安静的空操作,而 nonce 还活着、高危命令仍可被批准。
自查时实测到的正是这个:字段加了、枚举写了,但 dispatch 从没传过 turn_id。
所以下面锁的不是「函数能跑」,是**归属真的落到了 pending 上、且只释放该释放的**。
"""

from __future__ import annotations

import sys
from pathlib import Path

MOD = str(Path(__file__).resolve().parent)
if MOD not in sys.path:
    sys.path.insert(0, MOD)

import pytest  # noqa: E402

from operation import PendingStore, new_pending_confirmation  # noqa: E402


ALICE = {"user_id": "u-alice"}
BOB = {"user_id": "u-bob"}


def _put(store: PendingStore, *, turn_id: str, actor=ALICE, command_id="danger.do"):
    pending = new_pending_confirmation(
        command_id=command_id, args={}, source="ai", actor=actor, turn_id=turn_id,
    )
    store.put(pending)
    return pending


def test_turn_id_survives_the_factory_and_the_store() -> None:
    """归属得真的存进去 —— 工厂接了参数但没写进 dataclass,是最容易漏且最难发现的一种。"""
    store = PendingStore()
    p = _put(store, turn_id="turn-1")
    assert p.turn_id == "turn-1"
    assert p.to_dict()["turn_id"] == "turn-1"
    # peek 走的是 _copy_pending:那里漏一个字段就会把归属copy掉,而且不报错
    assert store.peek(p.token).turn_id == "turn-1"


def test_lists_only_this_turn() -> None:
    store = PendingStore()
    a1 = _put(store, turn_id="turn-1")
    a2 = _put(store, turn_id="turn-1")
    other = _put(store, turn_id="turn-2")
    got = set(store.tokens_for_turn("turn-1", actor=ALICE))
    assert got == {a1.token, a2.token}
    assert other.token not in got


def test_never_crosses_actors() -> None:
    """与 cancel() 同规矩:不给跨人释放开旁路。并发两人各有一轮时这条最要紧。"""
    store = PendingStore()
    mine = _put(store, turn_id="turn-1", actor=ALICE)
    theirs = _put(store, turn_id="turn-1", actor=BOB)
    assert store.tokens_for_turn("turn-1", actor=ALICE) == [mine.token]
    assert store.tokens_for_turn("turn-1", actor=BOB) == [theirs.token]


def test_empty_turn_id_belongs_to_no_turn() -> None:
    """用户在网页直接点的高危命令不属于任何一轮 —— 取消某轮不该把它顺手清掉。"""
    store = PendingStore()
    _put(store, turn_id="")
    assert store.tokens_for_turn("", actor=ALICE) == []
    assert store.tokens_for_turn("turn-1", actor=ALICE) == []


def test_reserved_is_left_alone() -> None:
    """reserved = 正在执行。整轮取消顺手清它会把一次执行到一半的高危命令状态搞乱。"""
    store = PendingStore()
    running = _put(store, turn_id="turn-1")
    idle = _put(store, turn_id="turn-1")
    # reserve 要 approval_channel == "human_http" + 匹配的 nonce —— 这串硬校验
    # 就是「AI 拿不到 nonce、只有网页人工通道能取执行权」那条红线的实现本体
    # (operation.py 里 hmac.compare_digest 那段)。渠道名写错会被直接拒。
    store.reserve(running.token, actor=ALICE,
                  approval_channel="human_http", approval_nonce=running.approval_nonce)
    assert store.tokens_for_turn("turn-1", actor=ALICE) == [idle.token]


def test_release_actually_releases() -> None:
    """端到端:枚举 → cancel → 该轮再无挂起。这是「取消了」这句话为真的判据。"""
    store = PendingStore()
    _put(store, turn_id="turn-1")
    _put(store, turn_id="turn-1")
    kept = _put(store, turn_id="turn-2")
    for token in store.tokens_for_turn("turn-1", actor=ALICE):
        assert store.cancel(token, actor=ALICE) is True
    assert store.tokens_for_turn("turn-1", actor=ALICE) == []
    assert store.state(kept.token) == "pending"      # 别人那轮不受影响


def test_dispatch_passes_turn_id_through() -> None:
    """链上最容易断、且断了最安静的一跳:dispatch 收了 turn_id 却没往工厂传。

    不起真 server —— 只证明 dispatch 的确认分支把 turn_id 交给了 pending。
    """
    core = Path(__file__).resolve().parents[1] / "01-command" / "command.py"
    src = core.read_text(encoding="utf-8")
    assert "turn_id: str = \"\"" in src, "dispatch 签名缺 turn_id 形参"
    # 工厂调用处必须把它传下去(只有签名没传 = 参数是摆设)
    idx = src.find("pending = new_pending_confirmation(")
    assert idx > 0, "找不到 pending 创建处"
    # 括号配对取整个调用 —— 找第一个 ")" 会停在内层 _render_confirm(...) 上,
    # 于是把「传了」误判成「没传」(第一版就是这么自己骗自己的)。
    start = src.index("(", idx)
    depth, j = 0, start
    while j < len(src):
        if src[j] == "(":
            depth += 1
        elif src[j] == ")":
            depth -= 1
            if depth == 0:
                break
        j += 1
    call = src[start: j + 1]
    assert "turn_id=turn_id" in call, "dispatch 未把 turn_id 传给 new_pending_confirmation"


@pytest.mark.parametrize("hop_marker,path,needle", [
    ("mcp URL 带 turn", "server.py", '"turn": turn_id'),
    ("McpActorCtx 收 turn", "sdk/mcp_bridge.py", 'turn_id: str = ""'),
    ("dispatch 收 turn", "sdk/mcp_bridge.py", "turn_id=actor_ctx.turn_id"),
])
def test_each_hop_present(hop_marker: str, path: str, needle: str) -> None:
    """逐跳锁死。链子断在哪一环都不报错,所以只能逐环钉。"""
    root = Path(__file__).resolve().parents[3]
    text = (root / path).read_text(encoding="utf-8")
    assert needle in text, f"{hop_marker} 这一跳断了({path} 里找不到 {needle!r})"
