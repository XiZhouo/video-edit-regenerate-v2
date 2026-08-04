from __future__ import annotations

import json
import time

import pytest


def test_idempotency_store_replays_first_success(tmp_path):
    from sdk import IdempotencyStore

    store = IdempotencyStore(tmp_path / "idem.json")
    calls = {"n": 0}

    def action():
        calls["n"] += 1
        return {"ok": True, "value": calls["n"]}

    first = store.run(scope="s", key="k-1", request_payload={"a": 1}, action=action)
    second = store.run(scope="s", key="k-1", request_payload={"a": 1}, action=action)

    assert first == {"ok": True, "value": 1}
    assert second == first
    assert calls["n"] == 1


def test_idempotency_store_rejects_same_key_different_body(tmp_path):
    from sdk import IdempotencyStore, ZeError

    store = IdempotencyStore(tmp_path / "idem.json")
    store.run(scope="s", key="k-1", request_payload={"a": 1}, action=lambda: {"ok": True})

    with pytest.raises(ZeError) as ei:
        store.run(scope="s", key="k-1", request_payload={"a": 2}, action=lambda: {"ok": False})
    assert ei.value.code == "idempotency_key_reused"


def test_idempotency_store_removes_record_when_action_fails(tmp_path):
    from sdk import IdempotencyStore, ZeError

    store = IdempotencyStore(tmp_path / "idem.json")
    with pytest.raises(ZeError):
        store.run(
            scope="s",
            key="k-1",
            request_payload={"a": 1},
            action=lambda: (_ for _ in ()).throw(ZeError("boom", "boom")),
        )

    assert store.run(scope="s", key="k-1", request_payload={"a": 1}, action=lambda: {"ok": True}) == {"ok": True}


def test_idempotency_store_can_freeze_only_a_named_uncertain_failure(tmp_path):
    from sdk import IdempotencyStore, ZeError

    store = IdempotencyStore(tmp_path / "idem.json")
    calls = {"n": 0}

    def uncertain_action():
        calls["n"] += 1
        raise ZeError("config_durability_unknown", "rename durability is unknown")

    with pytest.raises(ZeError) as first:
        store.run(
            scope="config.put",
            key="cfg-1",
            request_payload={"updates": {"x": 1}},
            action=uncertain_action,
            outcome_unknown_codes=("config_durability_unknown",),
        )
    assert first.value.code == "config_durability_unknown"

    with pytest.raises(ZeError) as replay:
        store.run(
            scope="config.put",
            key="cfg-1",
            request_payload={"updates": {"x": 1}},
            action=lambda: calls.__setitem__("n", calls["n"] + 1) or {"ok": True},
            outcome_unknown_codes=("config_durability_unknown",),
        )
    assert replay.value.code == "idempotency_outcome_unknown"
    assert calls["n"] == 1


def test_typed_history_unknown_creates_tombstone_and_never_replays(tmp_path):
    from sdk import IdempotencyStore, ZeError
    from history_authority import HistoryAuthorityError

    path = tmp_path / "idem.json"
    store = IdempotencyStore(path)
    calls = {"n": 0}

    def uncertain_action():
        calls["n"] += 1
        raise HistoryAuthorityError(
            "history_append_outcome_indeterminate",
            "append acknowledgement lost",
        )

    classifier = lambda exc: (
        isinstance(exc, HistoryAuthorityError) and exc.outcome_unknown
    )
    with pytest.raises(HistoryAuthorityError):
        store.run(
            scope="command.write",
            key="history-unknown",
            request_payload={"x": 1},
            action=uncertain_action,
            outcome_unknown_classifier=classifier,
        )

    record = next(iter(json.loads(path.read_text(encoding="utf-8"))["records"].values()))
    assert record["status"] == "outcome_unknown"
    assert record["error_code"] == "history_authority_mismatch"

    with pytest.raises(ZeError) as replay:
        store.run(
            scope="command.write",
            key="history-unknown",
            request_payload={"x": 1},
            action=lambda: calls.__setitem__("n", calls["n"] + 1) or {"ok": True},
            outcome_unknown_classifier=classifier,
        )

    assert replay.value.code == "idempotency_outcome_unknown"
    assert calls["n"] == 1


def test_preflight_history_mismatch_releases_idempotency_key_for_reload(tmp_path):
    from sdk import IdempotencyStore
    from history_authority import HistoryAuthorityError

    path = tmp_path / "idem.json"
    store = IdempotencyStore(path)
    calls = {"n": 0}
    classifier = lambda exc: (
        isinstance(exc, HistoryAuthorityError) and exc.outcome_unknown
    )

    def preflight_failure():
        calls["n"] += 1
        raise HistoryAuthorityError(
            "history_head_changed_before_store",
            "reload required",
        )

    with pytest.raises(HistoryAuthorityError) as caught:
        store.run(
            scope="command.write",
            key="history-preflight",
            request_payload={"x": 1},
            action=preflight_failure,
            outcome_unknown_classifier=classifier,
        )

    assert caught.value.outcome_unknown is False
    assert json.loads(path.read_text(encoding="utf-8"))["records"] == {}
    assert store.run(
        scope="command.write",
        key="history-preflight",
        request_payload={"x": 1},
        action=lambda: calls.__setitem__("n", calls["n"] + 1) or {"ok": True},
        outcome_unknown_classifier=classifier,
    ) == {"ok": True}
    assert calls["n"] == 2


def test_typed_pipeline_reconcile_failure_creates_permanent_tombstone(tmp_path):
    from pipeline_authority_commit import PipelineAuthorityCommitError
    from sdk import IdempotencyStore, ZeError
    from sdk.error_projection import commit_outcome_unknown

    path = tmp_path / "idem.json"
    store = IdempotencyStore(path)
    calls = {"n": 0}

    def post_durable_failure():
        calls["n"] += 1
        raise PipelineAuthorityCommitError(
            "pipeline_authority_unavailable",
            "pipeline_authority_fresh_scan_failed",
            "durable history needs repair",
            outcome_unknown=True,
            durability_phase="post_write_verification",
        )

    with pytest.raises(PipelineAuthorityCommitError):
        store.run(
            scope="command.pipeline.start",
            key="pipeline-unknown",
            request_payload={"run_id": "run-1"},
            action=post_durable_failure,
            outcome_unknown_classifier=commit_outcome_unknown,
        )

    record = next(iter(json.loads(path.read_text(encoding="utf-8"))["records"].values()))
    assert record["status"] == "outcome_unknown"
    assert record["error_code"] == "pipeline_authority_unavailable"

    with pytest.raises(ZeError) as replay:
        store.run(
            scope="command.pipeline.start",
            key="pipeline-unknown",
            request_payload={"run_id": "run-1"},
            action=lambda: calls.__setitem__("n", calls["n"] + 1) or {"ok": True},
            outcome_unknown_classifier=commit_outcome_unknown,
        )
    assert replay.value.code == "idempotency_outcome_unknown"
    assert calls["n"] == 1


def test_idempotency_store_prunes_old_records(tmp_path):
    from sdk import IdempotencyStore

    path = tmp_path / "idem.json"
    store = IdempotencyStore(path, in_progress_ttl_sec=30, retention_sec=30)
    store.run(scope="s", key="k-1", request_payload={"a": 1}, action=lambda: {"value": 1})
    raw = json.loads(path.read_text(encoding="utf-8"))
    for rec in raw["records"].values():
        rec["updated_at"] = time.time() - 60
    path.write_text(json.dumps(raw), encoding="utf-8")

    calls = {"n": 0}

    def action():
        calls["n"] += 1
        return {"value": calls["n"]}

    assert store.run(scope="s", key="k-1", request_payload={"a": 1}, action=action) == {"value": 1}
    assert calls["n"] == 1


def test_stale_in_progress_becomes_unknown_and_never_replays(tmp_path):
    from sdk import IdempotencyStore, ZeError

    path = tmp_path / "idem.json"
    store = IdempotencyStore(path, in_progress_ttl_sec=30)
    request_hash = __import__("sdk").canonical_request_hash({"a": 1})
    record_id = store._record_id("s", "k-lost")
    path.write_text(json.dumps({
        "schema_version": 1,
        "records": {
            record_id: {
                "schema_version": 1,
                "scope": "s",
                "key": "k-lost",
                "request_hash": request_hash,
                "status": "in_progress",
                "created_at": time.time() - 120,
                "updated_at": time.time() - 120,
            },
        },
    }), encoding="utf-8")
    calls = {"n": 0}

    with pytest.raises(ZeError) as exc:
        store.run(
            scope="s", key="k-lost", request_payload={"a": 1},
            action=lambda: calls.__setitem__("n", calls["n"] + 1) or {"ok": True},
        )

    assert exc.value.code == "idempotency_outcome_unknown"
    assert calls["n"] == 0
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["records"][record_id]["status"] == "outcome_unknown"


def test_idempotency_store_caps_record_count(tmp_path):
    from sdk import IdempotencyStore

    path = tmp_path / "idem.json"
    store = IdempotencyStore(path, max_records=2)
    for i in range(3):
        store.run(scope="s", key=f"k-{i}", request_payload={"i": i}, action=lambda i=i: {"value": i})

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert len(raw["records"]) == 2


def test_effect_ledger_replays_explicit_external_effect(tmp_path):
    from sdk import EffectLedger

    ledger = EffectLedger(tmp_path / "effects.json")
    calls = {"n": 0}

    def action():
        calls["n"] += 1
        return {"ok": True, "message_id": f"m-{calls['n']}"}

    first = ledger.run(scope="notify.push.feishu", key="effect-1",
                       effect_payload={"recipient": "ou_x", "message": "hi"},
                       action=action)
    second = ledger.run(scope="notify.push.feishu", key="effect-1",
                        effect_payload={"recipient": "ou_x", "message": "hi"},
                        action=action)

    assert second == first
    assert calls["n"] == 1


def test_effect_ledger_rejects_same_key_different_effect_payload(tmp_path):
    from sdk import EffectLedger, ZeError

    ledger = EffectLedger(tmp_path / "effects.json")
    ledger.run(scope="notify.push.feishu", key="effect-1",
               effect_payload={"recipient": "ou_x", "message": "hi"},
               action=lambda: {"ok": True})

    with pytest.raises(ZeError) as ei:
        ledger.run(scope="notify.push.feishu", key="effect-1",
                   effect_payload={"recipient": "ou_x", "message": "bye"},
                   action=lambda: {"ok": True})
    assert ei.value.code == "idempotency_key_reused"


def test_effect_ledger_failure_is_unknown_not_replay_permission(tmp_path):
    from sdk import EffectLedger, ZeError

    ledger = EffectLedger(tmp_path / "effects.json")
    calls = {"n": 0}

    def uncertain_send():
        calls["n"] += 1
        raise RuntimeError("provider response lost after send")

    with pytest.raises(RuntimeError):
        ledger.run(
            scope="notify.push.feishu", key="effect-unknown",
            effect_payload={"recipient": "ou_x", "message": "hi"},
            action=uncertain_send,
        )
    with pytest.raises(ZeError) as exc:
        ledger.run(
            scope="notify.push.feishu", key="effect-unknown",
            effect_payload={"recipient": "ou_x", "message": "hi"},
            action=lambda: {"ok": True},
        )

    assert exc.value.code == "idempotency_outcome_unknown"
    assert calls["n"] == 1
