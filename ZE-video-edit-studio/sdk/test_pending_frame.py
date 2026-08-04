"""sdk/test_pending_frame.py — 确认拉起帧的安全面单测(A3 变形体 AI 具象化)。

覆盖(红线优先):
  - pending_confirmation_frame **绝不携带 approval_nonce / 冻结 args**(AI 可读的
    SSE 面拿不到批准材料与参数明文);
  - entity_ref 提取(entity_type/entity_id/id 提示位;缺省为 None);
  - PendingStore.peek:只读、不消费(peek 后 reserve 仍可)、过期不可见、
    reserved 后不再对外呈现(防双通道抢确认)。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
CORE_OP = str(Path(__file__).resolve().parent.parent / "01-core" / "03-commands-operations" / "02-operation")
if CORE_OP not in sys.path:
    sys.path.insert(0, CORE_OP)

from operation import PendingStore, new_pending_confirmation  # noqa: E402

from sdk.persistence import pending_confirmation_frame  # noqa: E402


def _pending(**kw):
    base = dict(
        command_id="entity.update",
        args={"entity_type": "clip", "entity_id": "c1", "secret_field": "raw"},
        source="ai",
        actor={"user_id": "feishu:u1"},
        summary="改 clip c1",
    )
    base.update(kw)
    return new_pending_confirmation(**base)


def test_frame_never_leaks_approval_nonce_or_args():
    p = _pending()
    frame = pending_confirmation_frame(p, doc_id="d1")
    flat = repr(frame)
    assert "approval_nonce" not in frame
    assert p.approval_nonce not in flat          # 值也不能以任何键泄露
    assert "args" not in frame
    assert "secret_field" not in flat and "raw" not in flat
    assert frame["type"] == "pending_confirmation"
    assert frame["doc_id"] == "d1"
    assert frame["token"] == p.token
    assert frame["command_id"] == "entity.update"
    assert frame["actor"] == {"user_id": "feishu:u1"}


def test_frame_entity_ref_extraction():
    assert pending_confirmation_frame(_pending(), doc_id="d")["entity_ref"] == {
        "entity_type": "clip", "entity_id": "c1",
    }
    p2 = _pending(args={"id": "x9"})
    assert pending_confirmation_frame(p2, doc_id="d")["entity_ref"] == {
        "entity_type": "", "entity_id": "x9",
    }
    p3 = _pending(args={"foo": 1})
    assert pending_confirmation_frame(p3, doc_id="d")["entity_ref"] is None


def test_peek_is_readonly_and_state_scoped():
    store = PendingStore()
    p = _pending()
    store.put(p)

    seen = store.peek(p.token)
    assert seen is not None and seen.token == p.token
    assert seen.approval_nonce == p.approval_nonce   # 服务端内部可见(端点决定暴露面)

    # peek 不消费:仍可正常 reserve(同 actor + 正确 nonce)
    reservation = store.reserve(
        p.token, actor={"user_id": "feishu:u1"},
        approval_channel="human_http", approval_nonce=p.approval_nonce,
    )
    assert reservation.pending.token == p.token
    # reserved 后不再对外呈现(防双通道抢确认)
    assert store.peek(p.token) is None

    assert store.peek("no-such-token") is None


def test_peek_hides_expired():
    store = PendingStore(pending_ttl_sec=0.05)
    p = _pending()
    store.put(p)
    assert store.peek(p.token) is not None
    time.sleep(0.08)
    assert store.peek(p.token) is None
