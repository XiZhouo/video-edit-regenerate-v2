"""sdk/test_notify.py — 外发通知薄驱动单测(阶段A1).

不打真实 CLI:feishu 走 monkeypatch `subprocess.run`。验证 dry-run 短路 / argv 形态(--user-id
/--as bot/无密钥) / 发送失败不抛(记 result) / 校验类错抛 ZeError / lark-cli 定位兜底 /
message_id 解析 / NotifyLog 读写。
"""

from __future__ import annotations

import importlib
import json
import subprocess as sp
import sys
from pathlib import Path

import pytest

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sdk import ZeError  # noqa: E402
# barrel 把 `notify` 函数绑进 sdk 命名空间, 遮蔽了 sdk.notify 子模块;
# 取真实模块用 importlib(绕过包属性遮蔽), 再从模块取函数/类用于断言与 monkeypatch。
nt = importlib.import_module("sdk.notify")
notify = nt.notify
NotifyLog = nt.NotifyLog
NotifyResult = nt.NotifyResult


class FakeConfig:
    def __init__(self, **overrides):
        self._d = {
            "ze.notify.dry_run": True,
            "ze.notify.lark_cli_bin": "/usr/bin/lark-cli",
        }
        self._d.update(overrides)

    def get(self, key, fallback=None):
        return self._d.get(key, fallback)


def _payload(text="你好 <at user_id=\"ou_x\">赵舒雨</at>"):
    return {"text": text}


# ---- dry-run:短路、不 shell、写日志 ----

def test_dry_run_short_circuits_and_logs(tmp_path, monkeypatch):
    called = {"run": 0}
    monkeypatch.setattr(nt.subprocess, "run",
                        lambda *a, **k: called.__setitem__("run", called["run"] + 1))
    log = NotifyLog(path=tmp_path / "n.ndjson")
    res = notify(FakeConfig(), channel="feishu", recipient="ou_abc",
                 payload=_payload(), dry_run=True, notify_log=log)
    assert isinstance(res, NotifyResult)
    assert res.ok and res.status == "dry_run" and res.message_id == ""
    assert called["run"] == 0                      # 绝不调 CLI
    rows = log.read()
    assert len(rows) == 1 and rows[0]["status"] == "dry_run" and rows[0]["recipient"] == "ou_abc"


def test_dry_run_default_from_config(monkeypatch):
    monkeypatch.setattr(nt.subprocess, "run",
                        lambda *a, **k: pytest.fail("不该调 CLI"))
    res = notify(FakeConfig(**{"ze.notify.dry_run": True}), recipient="ou_x", payload=_payload())
    assert res.status == "dry_run"


# ---- 真发:argv 形态 + 无密钥 + message_id ----

def test_feishu_send_argv_shape_no_secret(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return sp.CompletedProcess(argv, 0, stdout='{"message_id":"om_123","chat_id":"oc_1"}', stderr="")

    monkeypatch.setattr(nt.subprocess, "run", fake_run)
    res = notify(FakeConfig(**{"ze.notify.dry_run": False}),
                 recipient="ou_abc", payload=_payload(), dry_run=False)
    assert res.ok and res.status == "sent" and res.message_id == "om_123"
    argv = seen["argv"]
    assert argv[0] == "/usr/bin/lark-cli"
    assert argv[1:3] == ["im", "+messages-send"]
    assert "--user-id" in argv and "ou_abc" in argv
    assert "--as" in argv and "bot" in argv
    assert "--text" in argv
    # 密钥/app_secret 绝不出现在 argv
    joined = " ".join(argv)
    assert "secret" not in joined.lower() and "app_id" not in joined.lower()


def test_feishu_send_failure_returns_failed_not_raise(monkeypatch):
    monkeypatch.setattr(nt.time, "sleep", lambda *_: None)
    monkeypatch.setattr(nt.subprocess, "run",
                        lambda argv, **kw: sp.CompletedProcess(argv, 1, stdout="", stderr="boom 9999"))
    res = notify(FakeConfig(**{"ze.notify.dry_run": False}),
                 recipient="ou_x", payload=_payload(), dry_run=False, retries=0)
    assert not res.ok and res.status == "failed" and "boom" in res.error


def test_feishu_idempotency_key_passed(monkeypatch):
    seen = {}
    monkeypatch.setattr(nt.subprocess, "run",
                        lambda argv, **kw: seen.update(argv=argv) or sp.CompletedProcess(argv, 0, stdout="om_9", stderr=""))
    notify(FakeConfig(**{"ze.notify.dry_run": False}), recipient="ou_x",
           payload={"text": "hi", "idempotency_key": "k-1"}, dry_run=False)
    assert "--idempotency-key" in seen["argv"] and "k-1" in seen["argv"]
    assert seen["argv"][-3:] != []  # message_id 走正则回退
    res_mid = nt._parse_message_id("发送成功 om_9")
    assert res_mid == {"message_id": "om_9"}


# ---- 校验类错误抛 ZeError(不是 failed result)----

@pytest.mark.parametrize("recipient", ["", "ou x", "a;b|c", "ou\nx"])
def test_bad_recipient_raises(recipient):
    with pytest.raises(ZeError) as ei:
        notify(FakeConfig(), recipient=recipient, payload=_payload())
    assert ei.value.code == "validation_failed"


def test_empty_text_raises():
    with pytest.raises(ZeError) as ei:
        notify(FakeConfig(), recipient="ou_x", payload={"text": "  "})
    assert ei.value.code == "validation_failed"


def test_unknown_channel_raises():
    with pytest.raises(ZeError) as ei:
        notify(FakeConfig(), channel="sms", recipient="ou_x", payload=_payload())
    assert ei.value.code == "validation_failed"


# ---- lark-cli 定位兜底 ----

def test_resolve_lark_cli_explicit(monkeypatch):
    monkeypatch.setattr(nt.shutil, "which", lambda *_: None)
    # 显式配置(expanduser);此处用绝对路径
    assert nt._resolve_lark_cli(FakeConfig(**{"ze.notify.lark_cli_bin": "/opt/x/lark-cli"})) == "/opt/x/lark-cli"


def test_resolve_lark_cli_path_then_fallback(monkeypatch):
    monkeypatch.setattr(nt.shutil, "which", lambda *_: "/somewhere/lark-cli")
    assert nt._resolve_lark_cli(FakeConfig(**{"ze.notify.lark_cli_bin": ""})) == "/somewhere/lark-cli"


def test_resolve_lark_cli_missing_raises(monkeypatch):
    monkeypatch.setattr(nt.shutil, "which", lambda *_: None)
    monkeypatch.setattr(nt.os, "access", lambda *a, **k: False)
    with pytest.raises(ZeError) as ei:
        nt._resolve_lark_cli(FakeConfig(**{"ze.notify.lark_cli_bin": ""}))
    assert ei.value.code == "notify_failed"


# ---- message_id 解析 ----

def test_parse_message_id_json_and_regex_and_empty():
    assert nt._parse_message_id('{"message_id":"om_a"}') == {"message_id": "om_a"}
    assert nt._parse_message_id('{"data":{"message_id":"om_b"}}') == {"message_id": "om_b"}
    assert nt._parse_message_id("已发送 om_c 完成") == {"message_id": "om_c"}
    assert nt._parse_message_id("") == {}
    assert nt._parse_message_id("no id here") == {}


# ---- NotifyLog 读写 ----

def test_notify_log_append_read_roundtrip(tmp_path):
    log = NotifyLog(path=tmp_path / "audit.ndjson")
    log.append(channel="feishu", recipient="ou_1", status="sent", message_id="om_1")
    log.append(channel="feishu", recipient="ou_2", status="failed", error="x")
    rows = log.read(limit=10)
    assert len(rows) == 2
    assert rows[0]["recipient"] == "ou_2"          # 最新在前
    assert rows[1]["message_id"] == "om_1"


# ==============================================================
# notify.push builtin 命令 + 收件人解析(阶段A2/A3)
# ==============================================================

from sdk import (  # noqa: E402
    ActionStatus,
    Actor,
    CommandMutation,
    CommandRegistry,
    CommitCoordinator,
    CommitFailure,
    CommitReceipt,
    CommitStage,
    CommitStatus,
    Document,
    DurabilityFacts,
    DurabilityState,
    EffectLedger,
    EventStatus,
    UserStore,
    register_builtin_commands,
)
import builtin_commands as bc  # noqa: E402  (叶子,import sdk 后在 path)
from effect_context import producer_effect_phase  # noqa: E402


class RosterConfig:
    def __init__(self, **overrides):
        self._d = {"ze.notify.dry_run": True,
                   "ze.notify.recipients": "shuyu:ou_zhao, alice=ou_alice"}
        self._d.update(overrides)

    def get(self, key, fallback=None):
        return self._d.get(key, fallback)


def test_parse_recipient_roster():
    r = bc.parse_recipient_roster(RosterConfig())
    assert r == {"shuyu": "ou_zhao", "alice": "ou_alice"}
    assert bc.parse_recipient_roster(RosterConfig(**{"ze.notify.recipients": ""})) == {}


def test_resolve_recipient_open_id_passthrough():
    assert bc.resolve_recipient("ou_direct", config=RosterConfig()) == "ou_direct"
    assert bc.resolve_recipient("on_bot", config=RosterConfig()) == "on_bot"


def test_resolve_recipient_roster_key():
    assert bc.resolve_recipient("shuyu", config=RosterConfig()) == "ou_zhao"


def test_resolve_recipient_ze_user_id(tmp_path):
    store = UserStore(path=tmp_path / "users.json")
    user = store.upsert_external_user(provider="feishu", external_id="ou_known", display_name="Known")
    assert bc.resolve_recipient(user.user_id, config=RosterConfig(), user_store=store) == "ou_known"


@pytest.mark.parametrize("bad", ["", "unknown_key"])
def test_resolve_recipient_unresolvable_raises(bad):
    with pytest.raises(ZeError) as ei:
        bc.resolve_recipient(bad, config=RosterConfig())
    assert ei.value.code == "validation_failed"


def _reg_with(config, notify_log=None, effect_ledger=None, user_store=None):
    reg = CommandRegistry()
    reg.config = config                            # type: ignore[attr-defined]
    reg.notify_log = notify_log                    # type: ignore[attr-defined]
    reg.effect_ledger = effect_ledger              # type: ignore[attr-defined]
    reg.user_store = user_store                    # type: ignore[attr-defined]
    register_builtin_commands(reg)
    return reg


def _notify_op(mutation):
    assert isinstance(mutation, CommandMutation)
    assert len(mutation.operations) == 1
    assert len(mutation.after_commit) == 1
    return mutation.operations[0]


def _run_notify_action(mutation, commit_id="commit-notify"):
    _notify_op(mutation)
    return mutation.after_commit[0].run(commit_id)


def test_notify_push_dispatch_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr(nt.subprocess, "run", lambda *a, **k: pytest.fail("dry-run 不该 shell"))
    log = NotifyLog(path=tmp_path / "n.ndjson")
    reg = _reg_with(RosterConfig(), notify_log=log)
    mutation = reg.dispatch(
        "notify.push",
        {"recipient": "shuyu", "message": "审核到了 <at user_id=\"ou_zhao\">赵舒雨</at>"},
        actor=Actor(user_id="u1"), source="user",
        doc=Document(schema_version="1.0.0", collaboration_mode="with_save"),
    )
    op = _notify_op(mutation)
    assert op.command_id == "notify.push" and op.kind == "native"
    assert op.external and op.external["kind"] == "outbound_api"
    assert op.external["status"] == "pending" and op.external["endpoint"] == "feishu/im/messages-send"
    assert op.external["request_id"] == "" and op.external["latency_ms"] == 0
    assert log.read() == []                       # producer 不写 NotifyLog
    _run_notify_action(mutation)
    rows = log.read()
    assert len(rows) == 1 and rows[0]["recipient"] == "ou_zhao" and rows[0]["status"] == "dry_run"


def test_notify_port_itself_rejects_direct_producer_effect(tmp_path):
    log = NotifyLog(path=tmp_path / "producer-forbidden.ndjson")
    with pytest.raises(ZeError) as exc:
        with producer_effect_phase():
            notify(
                RosterConfig(), recipient="ou_x", payload={"text": "hi"},
                dry_run=True, notify_log=log,
            )
    assert exc.value.code == "producer_effect_forbidden"
    assert log.read() == []


def test_notify_push_empty_message_rejected():
    reg = _reg_with(RosterConfig())
    with pytest.raises(ZeError) as ei:
        reg.dispatch("notify.push", {"recipient": "ou_x", "message": "   "},
                     actor=Actor(user_id="u1"), source="user",
                     doc=Document(schema_version="1.0.0", collaboration_mode="with_save"))
    assert ei.value.code == "validation_failed"


def test_notify_push_policy_roster_rejects_non_roster():
    bc._NOTIFY_SENDS.clear()
    reg = _reg_with(RosterConfig(**{"ze.notify.recipient_policy": "roster"}))
    with pytest.raises(ZeError) as ei:
        reg.dispatch("notify.push", {"recipient": "ou_stranger", "message": "hi"},
                     actor=Actor(user_id="u1"), source="user",
                     doc=Document(schema_version="1.0.0", collaboration_mode="with_save"))
    assert ei.value.code == "validation_failed"


def test_notify_push_policy_roster_allows_roster_key():
    bc._NOTIFY_SENDS.clear()
    reg = _reg_with(RosterConfig(**{"ze.notify.recipient_policy": "roster"}))
    mutation = reg.dispatch("notify.push", {"recipient": "shuyu", "message": "hi"},
                            actor=Actor(user_id="u1"), source="user",
                            doc=Document(schema_version="1.0.0", collaboration_mode="with_save"))
    assert _notify_op(mutation).external["status"] == "pending"
    _run_notify_action(mutation)


def test_notify_push_policy_known_users_allows_ze_user(tmp_path):
    bc._NOTIFY_SENDS.clear()
    store = UserStore(path=tmp_path / "users.json")
    user = store.upsert_external_user(provider="feishu", external_id="ou_known")
    log = NotifyLog(path=tmp_path / "n.ndjson")
    reg = _reg_with(
        RosterConfig(**{"ze.notify.recipient_policy": "known_users"}),
        notify_log=log,
        user_store=store,
    )
    mutation = reg.dispatch("notify.push", {"recipient": user.user_id, "message": "hi"},
                            actor=Actor(user_id="u1"), source="user",
                            doc=Document(schema_version="1.0.0", collaboration_mode="with_save"))
    assert _notify_op(mutation).external["status"] == "pending"
    assert log.read() == []
    _run_notify_action(mutation)
    assert log.read()[0]["recipient"] == "ou_known"


def test_notify_push_policy_known_users_rejects_unknown_open_id(tmp_path):
    bc._NOTIFY_SENDS.clear()
    store = UserStore(path=tmp_path / "users.json")
    store.upsert_external_user(provider="feishu", external_id="ou_known")
    reg = _reg_with(
        RosterConfig(**{"ze.notify.recipient_policy": "known_users"}),
        user_store=store,
    )
    with pytest.raises(ZeError) as ei:
        reg.dispatch("notify.push", {"recipient": "ou_stranger", "message": "hi"},
                     actor=Actor(user_id="u1"), source="user",
                     doc=Document(schema_version="1.0.0", collaboration_mode="with_save"))
    assert ei.value.code == "validation_failed"


def test_notify_push_rejects_ze_user_without_feishu_channel(tmp_path):
    bc._NOTIFY_SENDS.clear()
    store = UserStore(path=tmp_path / "users.json")
    user = store.register("local_user", "hunter222")
    reg = _reg_with(RosterConfig(), user_store=store)
    with pytest.raises(ZeError) as ei:
        reg.dispatch("notify.push", {"recipient": user.user_id, "message": "hi"},
                     actor=Actor(user_id="u1"), source="user",
                     doc=Document(schema_version="1.0.0", collaboration_mode="with_save"))
    assert ei.value.code == "validation_failed"


def test_notify_push_rate_limit_trips(monkeypatch):
    bc._NOTIFY_SENDS.clear()
    reg = _reg_with(RosterConfig(**{"ze.notify.rate_limit": 2}))
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    for _ in range(2):
        mutation = reg.dispatch("notify.push", {"recipient": "ou_x", "message": "hi"},
                                actor=Actor(user_id="spammer"), source="user", doc=doc)
        _run_notify_action(mutation)
    pending = reg.dispatch("notify.push", {"recipient": "ou_x", "message": "hi"},
                           actor=Actor(user_id="spammer"), source="user", doc=doc)
    with pytest.raises(ZeError) as ei:
        _run_notify_action(pending)
    assert ei.value.code == "rate_limited"


def test_notify_push_real_send_keeps_operation_pending_and_records_result_in_log(tmp_path, monkeypatch):
    bc._NOTIFY_SENDS.clear()
    monkeypatch.setattr(nt.subprocess, "run",
                        lambda argv, **k: sp.CompletedProcess(argv, 0, stdout='{"message_id":"om_77"}', stderr=""))
    log = NotifyLog(path=tmp_path / "n.ndjson")
    reg = _reg_with(RosterConfig(**{"ze.notify.dry_run": False,
                                    "ze.notify.lark_cli_bin": "/usr/bin/lark-cli"}),
                    notify_log=log)
    mutation = reg.dispatch("notify.push", {"recipient": "ou_x", "message": "hi"},
                            actor=Actor(user_id="u1"), source="user",
                            doc=Document(schema_version="1.0.0", collaboration_mode="with_save"))
    op = _notify_op(mutation)
    assert op.external["status"] == "pending" and op.external["request_id"] == ""
    assert log.read() == []
    result = _run_notify_action(mutation)
    assert result["status"] == "sent" and result["message_id"] == "om_77"
    assert log.read()[0]["message_id"] == "om_77"


def test_notify_push_effect_ledger_replays_explicit_key(tmp_path, monkeypatch):
    bc._NOTIFY_SENDS.clear()
    calls = {"n": 0}

    def fake_run(argv, **k):
        calls["n"] += 1
        return sp.CompletedProcess(argv, 0, stdout=json.dumps({"message_id": f"om_{calls['n']}"}), stderr="")

    monkeypatch.setattr(nt.subprocess, "run", fake_run)
    log = NotifyLog(path=tmp_path / "n.ndjson")
    reg = _reg_with(
        RosterConfig(**{"ze.notify.dry_run": False, "ze.notify.lark_cli_bin": "/usr/bin/lark-cli"}),
        notify_log=log,
        effect_ledger=EffectLedger(tmp_path / "effects.json"),
    )
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    args = {"recipient": "ou_x", "message": "hi", "idempotency_key": "notify-effect-1"}

    mutation1 = reg.dispatch("notify.push", args, actor=Actor(user_id="u1"), source="user", doc=doc)
    mutation2 = reg.dispatch("notify.push", args, actor=Actor(user_id="u1"), source="user", doc=doc)

    assert calls["n"] == 0 and log.read() == []
    first = _run_notify_action(mutation1, "commit-1")
    second = _run_notify_action(mutation2, "commit-2")
    assert calls["n"] == 1
    assert first["message_id"] == "om_1" and second["message_id"] == "om_1"
    assert _notify_op(mutation1).external["status"] == "pending"
    assert _notify_op(mutation2).external["status"] == "pending"
    assert len(log.read()) == 1


# ---- notify.push producer / commit / event / after-commit 故障矩阵 ----

class _CountingEffectLedger:
    def __init__(self):
        self.calls = 0

    def run(self, *, scope, key, effect_payload, action):
        self.calls += 1
        return action()


class _NotifyCommitStore:
    def __init__(self, *, committed):
        self.committed = committed
        self.calls = 0

    def commit(self, intent):
        self.calls += 1
        if self.committed:
            return CommitReceipt.committed(
                commit_id=intent.commit_id,
                document_id=intent.document_id,
                operation_ids=intent.operation_ids,
                trace_id=intent.trace_id,
                save_policy=intent.save_policy,
                document_version=max(1, int(getattr(intent.document, "version", 1))),
            )
        return CommitReceipt.from_failure(
            commit_id=intent.commit_id,
            document_id=intent.document_id,
            operation_ids=intent.operation_ids,
            trace_id=intent.trace_id,
            save_policy=intent.save_policy,
            failure=CommitFailure(
                stage=CommitStage.DOCUMENT,
                code="document_write_rejected",
                message="test store proved no durable write",
                facts=DurabilityFacts(
                    mutation_applied=True,
                    document=DurabilityState.NOT_DURABLE,
                    oplog=DurabilityState.NOT_DURABLE,
                ),
            ),
        )


class _NotifyPublisher:
    def __init__(self, status):
        self.status = status
        self.calls = 0

    def publish(self, payload):
        self.calls += 1
        return self.status


@pytest.mark.parametrize(
    ("store_committed", "event_status", "expected_effect_calls", "expected_action"),
    [
        (False, EventStatus.PUBLISHED, 0, ActionStatus.SKIPPED),
        (True, EventStatus.FAILED, 0, ActionStatus.SKIPPED),
        (True, EventStatus.PENDING, 1, ActionStatus.SUCCEEDED),
        (True, EventStatus.PUBLISHED, 1, ActionStatus.SUCCEEDED),
    ],
)
def test_notify_push_after_commit_fault_matrix(
    tmp_path,
    monkeypatch,
    store_committed,
    event_status,
    expected_effect_calls,
    expected_action,
):
    """store 未提交/事件失败时四种外部效果为 0;只有全绿才各执行一次。"""
    bc._NOTIFY_SENDS.clear()
    provider_calls = {"n": 0}

    def fake_run(argv, **kwargs):
        provider_calls["n"] += 1
        return sp.CompletedProcess(argv, 0, stdout='{"message_id":"om_matrix"}', stderr="")

    monkeypatch.setattr(nt.subprocess, "run", fake_run)
    log = NotifyLog(path=tmp_path / "matrix.ndjson")
    ledger = _CountingEffectLedger()
    config = RosterConfig(**{
        "ze.notify.dry_run": False,
        "ze.notify.lark_cli_bin": "/usr/bin/lark-cli",
        "ze.notify.rate_limit": 10,
    })
    reg = _reg_with(config, notify_log=log, effect_ledger=ledger)
    document = Document(schema_version="1.0.0", collaboration_mode="with_save")
    store = _NotifyCommitStore(committed=store_committed)
    publisher = _NotifyPublisher(event_status)
    coordinator = CommitCoordinator(document, store, publisher=publisher)
    secret_message = "matrix-body-must-not-enter-operation"
    secret_key = "matrix-key-must-not-enter-operation"

    execution = coordinator.execute(lambda: reg.dispatch(
        "notify.push",
        {"recipient": "ou_x", "message": secret_message, "idempotency_key": secret_key},
        actor=Actor(user_id="matrix-user"), source="user", doc=document,
    ))

    assert store.calls == 1
    assert provider_calls["n"] == expected_effect_calls
    assert ledger.calls == expected_effect_calls
    assert len(bc._NOTIFY_SENDS.get("matrix-user", ())) == expected_effect_calls
    assert len(log.read()) == expected_effect_calls
    assert len(execution.actions) == 1 and execution.actions[0].status is expected_action

    operation = execution.operations[0]
    assert operation.external == {
        "kind": "outbound_api",
        "endpoint": "feishu/im/messages-send",
        "request_id": "",
        "status": "pending",
        "latency_ms": 0,
    }
    operation_wire = json.dumps(operation.to_dict(), ensure_ascii=False)
    assert secret_message not in operation_wire and secret_key not in operation_wire

    if not store_committed:
        assert publisher.calls == 0
        assert execution.receipt.status is CommitStatus.ABORTED
    else:
        assert publisher.calls == 1
        assert execution.receipt.status is CommitStatus.COMMITTED
        assert execution.receipt.event_status is event_status


def test_notify_push_action_failure_does_not_rewrite_committed_receipt(tmp_path, monkeypatch):
    bc._NOTIFY_SENDS.clear()
    provider_calls = {"n": 0}

    def failed_notify(config, *, channel, recipient, payload, notify_log, **kwargs):
        provider_calls["n"] += 1
        notify_log.append(channel=channel, recipient=recipient, status="failed", error="provider down")
        return NotifyResult(
            ok=False,
            channel=channel,
            recipient=recipient,
            status="failed",
            error="provider down",
        )

    monkeypatch.setattr(nt, "notify", failed_notify)
    log = NotifyLog(path=tmp_path / "failed.ndjson")
    ledger = _CountingEffectLedger()
    reg = _reg_with(
        RosterConfig(**{"ze.notify.dry_run": False, "ze.notify.rate_limit": 10}),
        notify_log=log,
        effect_ledger=ledger,
    )
    document = Document(schema_version="1.0.0", collaboration_mode="with_save")
    execution = CommitCoordinator(
        document,
        _NotifyCommitStore(committed=True),
        publisher=_NotifyPublisher(EventStatus.PUBLISHED),
    ).execute(lambda: reg.dispatch(
        "notify.push",
        {"recipient": "ou_x", "message": "hi", "idempotency_key": "failed-action"},
        actor=Actor(user_id="failed-user"), source="user", doc=document,
    ))

    assert execution.receipt.status is CommitStatus.COMMITTED
    assert execution.receipt.event_status is EventStatus.PUBLISHED
    assert execution.action_failed is True
    assert execution.actions[0].status is ActionStatus.FAILED
    assert execution.actions[0].code == "notify_failed"
    assert provider_calls["n"] == 1 and ledger.calls == 1
    assert len(bc._NOTIFY_SENDS["failed-user"]) == 1
    assert len(log.read()) == 1
