"""InvocationLog / InvocationReceipt 测试(规划 §3.4:回执幂等入独立日志,不进 Operation 栈)。

覆盖:record/list 往返、幂等去重(同 key 只一条 + 返既有)、插入序稳定(sort_keys 不打乱)、
user_id 过滤、recent(n)、落盘重载、public_dict 脱敏丢 None + 往返、空 key 拒绝、损坏结构拒绝、
与 Operation(OpLog)物理分离。离线零 I/O 依赖(纯本地文件)。
"""

from __future__ import annotations

import json

import pytest

from sdk.hub import InvocationLog, InvocationReceipt


def _receipt(key: str, *, user: str = "u1", model: str = "gemini3", status: str = "ok",
             created_at: float = 100.0, **kw) -> InvocationReceipt:
    return InvocationReceipt(
        receipt_id=f"rc-{key}",
        idempotency_key=key,
        feature_id="foundry.image.generate",
        capability="image",
        provider="gemini",
        model_id=model,
        route="service",
        status=status,
        created_at=created_at,
        actor_user_id=user,
        **kw,
    )


def test_record_and_list_roundtrip(tmp_path):
    log = InvocationLog(tmp_path / "invocations.json")
    r = log.record(_receipt("k1"))
    assert r.idempotency_key == "k1"
    got = log.list()
    assert len(got) == 1
    assert got[0].receipt_id == "rc-k1"
    assert got[0].model_id == "gemini3"
    assert got[0].actor_user_id == "u1"


def test_trace_roundtrips_through_log(tmp_path):
    # §3.5 第8步:解析轨迹 trace 作纯数据承载,record→落盘→list 往返不丢
    log = InvocationLog(tmp_path / "invocations.json")
    tr = ("feature=foundry.image.generate audience=internal", "auto → gemini3")
    log.record(_receipt("k-trace", trace=tr))
    got = log.list()[0]
    assert got.trace == tr
    assert got.public_dict()["trace"] == list(tr)


def test_empty_trace_dropped_from_public_dict(tmp_path):
    # 空轨迹走 None-drop,载荷不含 trace 键(保持紧凑,向后兼容旧回执)
    r = _receipt("k-notrace")
    assert r.trace == ()
    assert "trace" not in r.public_dict()
    # 反序列化缺 trace 键 → 回落空 tuple(旧数据兼容)
    log = InvocationLog(tmp_path / "invocations.json")
    log.record(r)
    assert log.list()[0].trace == ()


def test_idempotent_same_key_single_entry(tmp_path):
    log = InvocationLog(tmp_path / "invocations.json")
    first = log.record(_receipt("dup", model="gemini3"))
    # 同 key 二次 record(哪怕字段不同)→ 不新增、返回既有(不重复记账、不覆盖)
    second = log.record(_receipt("dup", model="openai-15"))
    assert len(log.list()) == 1
    assert first.model_id == "gemini3"
    assert second.model_id == "gemini3"        # 返既有,非新值
    assert log.list()[0].model_id == "gemini3"


def test_insertion_order_stable_across_reload(tmp_path):
    path = tmp_path / "invocations.json"
    log = InvocationLog(path)
    # 乱序 key(字母序会打乱),验证 sort_keys=True 不改记录顺序
    for k in ("zebra", "alpha", "mike"):
        log.record(_receipt(k))
    keys = [r.idempotency_key for r in log.list()]
    assert keys == ["zebra", "alpha", "mike"]
    # 落盘重载后顺序不变
    keys2 = [r.idempotency_key for r in InvocationLog(path).list()]
    assert keys2 == ["zebra", "alpha", "mike"]


def test_user_filter_and_recent(tmp_path):
    log = InvocationLog(tmp_path / "invocations.json")
    log.record(_receipt("a", user="u1"))
    log.record(_receipt("b", user="u2"))
    log.record(_receipt("c", user="u1"))
    assert [r.idempotency_key for r in log.list(user_id="u1")] == ["a", "c"]
    assert [r.idempotency_key for r in log.list(user_id="u2")] == ["b"]
    assert [r.idempotency_key for r in log.recent(2)] == ["b", "c"]
    assert [r.idempotency_key for r in log.recent(1, user_id="u1")] == ["c"]


def test_limit_takes_latest(tmp_path):
    log = InvocationLog(tmp_path / "invocations.json")
    for k in ("a", "b", "c", "d"):
        log.record(_receipt(k))
    assert [r.idempotency_key for r in log.list(limit=2)] == ["c", "d"]


def test_public_dict_drops_none_and_roundtrips(tmp_path):
    r = _receipt("k", latency_ms=None, error_code=None,
                 params_summary={"size": "1024x1024", "n": 1},
                 result_summary={"mime": "image/jpeg", "bytes": 310000})
    pub = r.public_dict()
    assert "latency_ms" not in pub          # None 丢弃
    assert "error_code" not in pub
    assert pub["params_summary"] == {"size": "1024x1024", "n": 1}
    # 往返:record 存 public_dict,list 还原
    log = InvocationLog(tmp_path / "invocations.json")
    log.record(r)
    back = log.list()[0]
    assert back.params_summary == {"size": "1024x1024", "n": 1}
    assert back.result_summary["mime"] == "image/jpeg"
    assert back.latency_ms is None


def test_receipt_carries_no_secret_fields():
    # 脱敏红线:receipt 结构里没有任何 secret/token/密文字段(按构造即无)
    r = _receipt("k", connection_id="conn-123")
    pub = r.public_dict()
    for forbidden in ("secret", "api_key", "token", "password", "reveal"):
        assert not any(forbidden in str(k).lower() for k in pub), forbidden
    assert pub["connection_id"] == "conn-123"    # 连接引用(非密文)保留


def test_error_receipt_records_error_code(tmp_path):
    log = InvocationLog(tmp_path / "invocations.json")
    log.record(_receipt("err", status="error", error_code="provider_unreachable"))
    got = log.list()[0]
    assert got.status == "error"
    assert got.error_code == "provider_unreachable"


def test_empty_idempotency_key_rejected(tmp_path):
    log = InvocationLog(tmp_path / "invocations.json")
    with pytest.raises(ValueError):
        log.record(_receipt("   "))            # 空白 key


def test_corrupt_receipts_shape_rejected(tmp_path):
    path = tmp_path / "invocations.json"
    path.write_text(json.dumps({"schema_version": 1, "receipts": {"not": "a list"}}), encoding="utf-8")
    with pytest.raises(ValueError):
        InvocationLog(path).list()


def test_separate_file_from_oplog(tmp_path):
    # 独立日志:回执落 invocations.json,与 Operation 的 oplog.ndjson 物理分离(不共享文件)
    log = InvocationLog(tmp_path / "invocations.json")
    log.record(_receipt("k"))
    assert (tmp_path / "invocations.json").exists()
    assert not (tmp_path / "oplog.ndjson").exists()
