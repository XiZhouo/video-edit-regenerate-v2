"""InvocationReceipt 写侧桥测试(§3.4:receipt_from_sink 构造 + record_from_sink best-effort 幂等记录)。"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sdk.hub import InvocationLog, receipt_from_sink, record_from_sink  # noqa: E402


_SINK = {"route": "gemini-api", "provider": "gemini", "model_id": "gemini3", "trace": ("auto → gemini3",)}


def _fields(**over):
    f = {"idempotency_key": "k1", "feature_id": "foundry.image.generate", "capability": "image",
         "actor_user_id": "u1", "status": "ok", "created_at": 1000.0}
    f.update(over)
    return f


def test_receipt_from_sink_maps_actual_placement():
    r = receipt_from_sink(_SINK, **_fields())
    assert r.route == "gemini-api" and r.provider == "gemini" and r.model_id == "gemini3"
    assert r.trace == ("auto → gemini3",)
    assert r.feature_id == "foundry.image.generate" and r.capability == "image"
    assert r.actor_user_id == "u1" and r.status == "ok" and r.created_at == 1000.0
    assert r.receipt_id == "k1"          # receipt_id 缺省 = idempotency_key


def test_receipt_id_explicit_overrides_default():
    r = receipt_from_sink(_SINK, **_fields(receipt_id="rid-9"))
    assert r.receipt_id == "rid-9" and r.idempotency_key == "k1"


def test_error_code_normalized_only_on_error_status():
    # status=error → error_code 经 9 类权威码归一(legacy 旧串 → 新码)
    r = receipt_from_sink(_SINK, **_fields(status="error", error_code="needs_reauth"))
    assert r.status == "error" and r.error_code == "credential_expired"   # legacy map
    # 已是权威码 → 原样
    r2 = receipt_from_sink(_SINK, **_fields(status="error", error_code="rate_limited"))
    assert r2.error_code == "rate_limited"
    # status=ok → 不动 error_code(通常 None)
    assert receipt_from_sink(_SINK, **_fields(status="ok", error_code=None)).error_code is None


def test_none_sink_yields_minimal_receipt():
    # sink=None(拿不到落位)→ route/provider/model 落空串,仍构造一条最小回执
    r = receipt_from_sink(None, **_fields())
    assert r.route == "" and r.provider == "" and r.model_id == "" and r.trace == ()
    assert r.idempotency_key == "k1"


def test_turn_id_threads_through():
    r = receipt_from_sink(_SINK, **_fields(turn_id="turn-42"))
    assert r.turn_id == "turn-42"


def test_no_secret_fields_in_public_dict():
    # 脱敏红线:public_dict 不含 secret 类键
    r = receipt_from_sink(_SINK, **_fields(params_summary={"n": 2, "size": "1024x1024"}))
    pub = r.public_dict()
    assert not any(k in pub for k in ("secret", "api_key", "token", "prompt", "bytes"))
    assert pub["params_summary"] == {"n": 2, "size": "1024x1024"}


def test_record_from_sink_persists_and_is_idempotent(tmp_path):
    log = InvocationLog(tmp_path / "inv.json")
    r1 = record_from_sink(log, _SINK, **_fields(idempotency_key="dup"))
    r2 = record_from_sink(log, _SINK, **_fields(idempotency_key="dup"))   # 同键 → 不重复记账
    assert r1 is not None and r2 is not None
    assert len(log.list()) == 1                                          # 幂等:只一条
    assert log.recent(1)[0].model_id == "gemini3"


def test_record_from_sink_none_log_is_noop():
    # log 缺失 → best-effort 返 None,不抛
    assert record_from_sink(None, _SINK, **_fields()) is None


def test_record_from_sink_bad_fields_swallowed(tmp_path):
    log = InvocationLog(tmp_path / "inv.json")
    # 缺必填(空 idempotency_key)→ log.record 抛 ValueError,被 best-effort 吞成 None,不阻断
    assert record_from_sink(log, _SINK, **_fields(idempotency_key="")) is None
    assert log.list() == []
