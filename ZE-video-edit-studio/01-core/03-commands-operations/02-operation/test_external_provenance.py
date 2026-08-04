"""test_external_provenance — Operation.external 信封 + PendingStore(P1 §6#6)。

覆盖:
  - 无 external 的 op:to_dict() **不含** external 键(历史 oplog hash chain 不变 → back-compat)
  - 有 external 的 op:to_dict()['external'] 形态正确,OpLog append + verify_chain 通过
  - external_ai / external_api 信封字段 + target_ref
  - PendingStore put/take/cancel + 单次性 + confirm_token_unknown
"""
from __future__ import annotations

import pytest

from entity import AiProvenance, ZeError
from operation import (
    EXTERNAL_TARGET, OpLog, new_operation, external_ai, external_api,
    PendingStore, new_pending_confirmation,
)


def test_op_without_external_omits_key_and_chain_holds():
    op = new_operation(command_id="x", source="user", actor={"user_id": "u"},
                       patches=[], reverse_patches=[])
    d = op.to_dict()
    assert "external" not in d, "external must be omitted when None (preserves historical oplog hashes)"
    log = OpLog()
    log.append(op)
    assert log.verify_chain()


def test_op_with_external_round_trips_and_chains():
    prov = AiProvenance(actor="u", model="flux", cost_usd=0.01, token_in=10, token_out=5)
    op = new_operation(
        command_id="gen", source="ai", actor={"user_id": "u"},
        patches=[], reverse_patches=[],
        external=external_ai(prov, target_ref={"entity_type": EXTERNAL_TARGET, "entity_id": "host-1"}),
    )
    d = op.to_dict()
    assert d["external"]["kind"] == "ai_generation"
    assert d["external"]["provenance"]["model"] == "flux"
    assert d["external"]["target_ref"] == {"entity_type": "__external__", "entity_id": "host-1"}
    log = OpLog()
    log.append(op)
    assert log.verify_chain()


def test_external_api_envelope():
    env = external_api("https://api.example/gen", request_id="r1",
                       target_ref={"entity_type": EXTERNAL_TARGET, "entity_id": "ep"}, latency_ms=42)
    assert env["kind"] == "outbound_api"
    assert env["endpoint"] == "https://api.example/gen"
    assert env["request_id"] == "r1"
    assert env["latency_ms"] == 42
    assert env["target_ref"]["entity_id"] == "ep"


def test_external_ai_accepts_plain_dict_provenance():
    # duck-typed: a plain mapping (no to_dict) is also accepted
    env = external_ai({"model": "m"})
    assert env["provenance"] == {"model": "m"}


def test_pending_store_reserve_consume_is_single_use():
    store = PendingStore()
    p = new_pending_confirmation(command_id="c", args={"a": 1}, source="user", actor={"user_id": "u"})
    store.put(p)
    assert len(store) == 1
    reservation = store.reserve(
        p.token,
        actor={"user_id": "u"},
        approval_channel="human_http",
        approval_nonce=p.approval_nonce,
    )
    got = store.pending_for(reservation)
    assert got.command_id == "c" and got.args == {"a": 1}
    store.consume(reservation)
    assert len(store) == 0
    with pytest.raises(ZeError) as ei:
        store.reserve(
            p.token,
            actor={"user_id": "u"},
            approval_channel="human_http",
            approval_nonce=p.approval_nonce,
        )
    assert ei.value.code == "confirm_token_unknown"


def test_pending_store_cancel():
    store = PendingStore()
    p = new_pending_confirmation(command_id="c", args={}, source="user", actor={"user_id": "u"})
    store.put(p)
    actor = {"user_id": "u"}
    assert store.cancel(p.token, actor=actor) is True
    assert store.cancel(p.token, actor=actor) is False
