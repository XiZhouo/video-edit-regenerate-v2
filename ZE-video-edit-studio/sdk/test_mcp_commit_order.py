"""W03 ordering tests for the two real MCP mutation surfaces."""
from __future__ import annotations

import json
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdk import (  # noqa: E402
    Actor, BrokerEventPublisher, CommandRegistry, CommandSpec, CommitCoordinator,
    Document, Entity, FileCommitStore, IdempotencyStore, OpLog, Patch, new_operation,
)
import sdk.mcp_bridge as mb  # noqa: E402
from history_authority import HistoryAuthorityError  # noqa: E402
from sdk.test_commit_surface_matrix import assert_commit_order  # noqa: E402


class _Repo:
    def __init__(self, ctx):
        self.ctx = ctx

    def list_project_ids(self):
        return ["p1"]

    def project_exists(self, project_id):
        return project_id == "p1"

    def get_or_load(self, project_id):
        assert project_id == "p1"
        return self.ctx


def _surface(tmp_path: Path, monkeypatch, *, requires_confirm: bool = False):
    events: list[str] = []

    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    assert type(doc) is Document
    real_save = Document.save

    def observed_save(self, path):
        if self is doc:
            events.append("save")
        return real_save(self, path)

    monkeypatch.setattr(Document, "save", observed_save)
    doc.add(Entity(type="t", data={"x": 1}, id="e1"))
    broker = SimpleNamespace(publish_change=lambda payload: events.append("publish"))
    reg = CommandRegistry()

    def execute(args, actor, source, doc):
        events.append("mutate")
        ent = doc.get("t", "e1")
        before = ent.data["x"]
        after = int(args.get("x", before + 1))
        ent.update({"x": after}, expected_version=ent.version, actor=actor)
        return new_operation(
            command_id="baseline.write", source=source, actor=actor.to_dict(),
            patches=[Patch("t", "e1", "x", "set", before, after)],
            reverse_patches=[Patch("t", "e1", "x", "set", after, before)],
        )

    reg.register(CommandSpec(
        id="baseline.write", label="baseline", requires_confirm=requires_confirm,
        execute=execute,
    ))
    oplog = OpLog(tmp_path / "oplog.ndjson")
    real_append = oplog.append
    oplog.append = lambda op: events.append("append") or real_append(op)
    lock = threading.RLock()
    doc_path = tmp_path / "document.json"
    ctx = SimpleNamespace(
        doc=doc, doc_path=doc_path, op_log=oplog,
        cmd_reg=reg, lock=lock, generation=1,
    )
    ctx.commit = CommitCoordinator(
        doc, FileCommitStore(doc_path, oplog),
        publisher=BrokerEventPublisher(broker), lock=lock,
    )
    repo = _Repo(ctx)
    actor_ctx = mb.McpActorCtx(
        actor=Actor(user_id="__mcp__", source="ai"),
        enforce_acl=False,
        project_hint="p1",
    )
    return events, repo, actor_ctx


def _text_payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def test_mcp_coordinator_respects_explicit_auto_commit_for_live_collab(tmp_path):
    doc = Document(
        schema_version="1.0.0", collaboration_mode="live_collab",
        save_policy="auto_commit",
    )
    oplog = OpLog(tmp_path / "oplog.ndjson")
    op = new_operation(
        command_id="baseline.write", source="ai", actor={"user_id": "u"},
        patches=[], reverse_patches=[],
    )

    execution = CommitCoordinator(
        doc, FileCommitStore(tmp_path / "document.json", oplog),
    ).execute(lambda: op)

    assert (tmp_path / "document.json").exists()
    assert execution.receipt is not None
    assert execution.receipt.status.value == "committed"
    assert execution.receipt.document_durable is True


def test_mcp_command_commits_before_publish(tmp_path, monkeypatch):
    events, repo, actor_ctx = _surface(tmp_path, monkeypatch)

    result = mb._call_tool(repo, actor_ctx, "baseline-write", {"x": 2})
    events.append("return")

    assert result["isError"] is False
    payload = _text_payload(result)
    assert payload["op"]["command_id"] == "baseline.write"
    assert payload["persistence"]["status"] == "committed"
    assert payload["persistence"]["event_status"] == "published"
    assert_commit_order("mcp_command", events)


def test_mcp_long_task_cannot_write_after_project_generation_changes(
    tmp_path, monkeypatch,
):
    events, repo, actor_ctx = _surface(tmp_path, monkeypatch)
    actor_ctx.project_generation = 1
    repo.ctx.generation = 2

    with pytest.raises(mb.ZeError) as captured:
        mb._call_tool(repo, actor_ctx, "baseline-write", {"x": 2})

    assert captured.value.code == "project_generation_stale"
    assert events == []
    assert repo.ctx.doc.get("t", "e1").data["x"] == 1


def test_mcp_resource_reads_hold_generation_lease(tmp_path, monkeypatch):
    _, repo, actor_ctx = _surface(tmp_path, monkeypatch)
    events: list[str] = []

    @contextmanager
    def lifecycle_guard():
        events.append("lease_enter")
        try:
            yield
        finally:
            events.append("lease_exit")

    repo.ctx.lifecycle_guard = lifecycle_guard
    response = mb.handle_mcp_message(repo, {
        "jsonrpc": "2.0", "id": 1, "method": "resources/read",
        "params": {"uri": "ze://entities"},
    }, actor_ctx)

    assert response is not None and "result" in response
    assert events == ["lease_enter", "lease_exit"]


def test_mcp_ai_cannot_self_approve_pending_command(tmp_path, monkeypatch):
    events, repo, actor_ctx = _surface(
        tmp_path, monkeypatch, requires_confirm=True,
    )

    pending_result = mb._call_tool(repo, actor_ctx, "baseline-write", {"x": 2})
    pending = _text_payload(pending_result)["pending"]
    assert events == []

    assert "approval_nonce" not in pending
    assert "args" not in pending
    assert "ze_confirm" not in {tool["name"] for tool in mb._meta_tool_defs()}
    direct = mb._call_tool(repo, actor_ctx, "ze_confirm", {"token": pending["token"]})
    direct_payload = _text_payload(direct)
    assert direct["isError"] is True
    assert direct_payload["error"] == "unknown_tool"
    assert events == []


def test_mcp_explicit_business_key_replays_without_second_mutation(
    tmp_path, monkeypatch,
):
    events, repo, actor_ctx = _surface(tmp_path, monkeypatch)
    repo.idempotency_store = IdempotencyStore(tmp_path / "mcp-idempotency.json")

    first = mb._call_tool(
        repo, actor_ctx, "baseline-write",
        {"x": 2, "_idempotency_key": "mcp-write-1"},
    )
    first_events = list(events)
    second = mb._call_tool(
        repo, actor_ctx, "baseline-write",
        {"x": 2, "_idempotency_key": "mcp-write-1"},
    )

    assert second == first
    assert events == first_events
    assert _text_payload(first)["persistence"]["status"] == "committed"


def test_mcp_history_unknown_tombstones_key_and_projects_safe_error(
    tmp_path, monkeypatch,
):
    _, repo, actor_ctx = _surface(tmp_path, monkeypatch)
    repo.idempotency_store = IdempotencyStore(tmp_path / "mcp-history-unknown.json")
    calls = {"n": 0}

    def uncertain_action():
        calls["n"] += 1
        raise HistoryAuthorityError(
            "history_append_outcome_indeterminate",
            "private post-write detail",
        )

    with pytest.raises(HistoryAuthorityError):
        mb._run_idempotent_write(
            repo,
            actor_ctx,
            project_id="p1",
            tool_name="baseline-write",
            key="mcp-history-1",
            arguments={"x": 2},
            action=uncertain_action,
        )
    with pytest.raises(mb.ZeError) as replay:
        mb._run_idempotent_write(
            repo,
            actor_ctx,
            project_id="p1",
            tool_name="baseline-write",
            key="mcp-history-1",
            arguments={"x": 2},
            action=lambda: calls.__setitem__("n", calls["n"] + 1) or {},
        )

    assert replay.value.code == "idempotency_outcome_unknown"
    assert calls["n"] == 1

    monkeypatch.setattr(
        mb,
        "_call_tool",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            HistoryAuthorityError(
                "history_append_outcome_indeterminate",
                "private post-write detail",
            )
        ),
    )
    response = mb.handle_mcp_message(repo, {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "baseline-write", "arguments": {}},
    }, actor_ctx)
    payload = _text_payload(response["result"])

    assert response["id"] == 7
    assert response["result"]["isError"] is True
    assert payload["error"]["code"] == "history_authority_mismatch"
    assert "do not retry" in payload["error"]["message"]
    assert "private" not in payload["error"]["message"]


def test_mcp_command_schema_advertises_business_idempotency_key(
    tmp_path, monkeypatch,
):
    _, repo, _ = _surface(tmp_path, monkeypatch)
    tool = next(row for row in mb._command_tools(repo.ctx.cmd_reg)
                if row["name"] == "baseline-write")
    key_schema = tool["inputSchema"]["properties"]["_idempotency_key"]
    assert key_schema["type"] == "string"
    assert key_schema["maxLength"] == 200


# --- ze_guide 引导驱动(系统 3a):act 步过确认闸门 + 帧流式 ------------------

def _attach_guide_broker(repo) -> list[dict]:
    frames: list[dict] = []
    repo.ctx.broker = SimpleNamespace(publish_change=lambda p: frames.append(p))
    return frames


def test_ze_guide_reversible_act_dispatches_and_streams_frames(tmp_path, monkeypatch):
    _events, repo, actor_ctx = _surface(tmp_path, monkeypatch)  # requires_confirm=False
    frames = _attach_guide_broker(repo)

    result = mb._call_tool(repo, actor_ctx, "ze_guide", {"steps": [
        {"kind": "say", "say": "先看这里"},
        {"kind": "act", "command_id": "baseline.write", "args": {"x": 5}},
    ]})
    payload = _text_payload(result)

    assert result["isError"] is False
    assert payload["total"] == 2 and payload["status"] == "completed"
    # 可逆 act 真跑:文档被改。
    assert repo.ctx.doc.get("t", "e1").data["x"] == 5
    assert payload["acts"][0]["command_id"] == "baseline.write"
    assert payload["acts"][0]["pending"] is False
    # per-step guide_step + 收尾 guide_end,复用广播通道。
    kinds = [f["type"] for f in frames]
    assert kinds.count("guide_step") == 2 and kinds.count("guide_end") == 1
    assert all(f.get("runId") == payload["runId"] for f in frames)


def test_ze_guide_high_risk_act_goes_pending_not_self_approved(tmp_path, monkeypatch):
    events, repo, actor_ctx = _surface(tmp_path, monkeypatch, requires_confirm=True)
    frames = _attach_guide_broker(repo)

    result = mb._call_tool(repo, actor_ctx, "ze_guide", {"steps": [
        {"kind": "act", "command_id": "baseline.write", "args": {"x": 9}},
    ]})
    payload = _text_payload(result)

    # 高危命令进待批准:未 mutate/append/publish(commit 通道未触发)。
    assert events == []
    assert repo.ctx.doc.get("t", "e1").data["x"] == 1
    assert payload["acts"][0]["pending"] is True
    assert "pending" in payload["acts"][0]["result"]
    # 帧仍流式(引导本身不受阻);3.59.0 起 pending 时加播确认拉起帧(通知网页拉起批准 UI)。
    assert [f["type"] for f in frames] == ["guide_step", "pending_confirmation", "guide_end"]
    pending_frame = frames[1]
    # 红线:广播帧绝不携带批准材料与冻结参数(AI 可读面拿不到 nonce,不能自批)。
    assert "approval_nonce" not in pending_frame
    assert "args" not in pending_frame
    assert pending_frame["command_id"] == "baseline.write"


def test_ze_guide_rejects_unknown_step_kind(tmp_path, monkeypatch):
    _events, repo, actor_ctx = _surface(tmp_path, monkeypatch)
    _attach_guide_broker(repo)
    with pytest.raises(mb.ZeError) as exc:
        mb._call_tool(repo, actor_ctx, "ze_guide", {"steps": [{"kind": "frobnicate"}]})
    assert exc.value.code == "validation_failed"


def test_ze_guide_requires_tour_id_or_steps(tmp_path, monkeypatch):
    _events, repo, actor_ctx = _surface(tmp_path, monkeypatch)
    _attach_guide_broker(repo)
    with pytest.raises(mb.ZeError) as exc:
        mb._call_tool(repo, actor_ctx, "ze_guide", {})
    assert exc.value.code == "validation_failed"


def test_ze_guide_is_advertised_but_confirm_tool_is_not():
    names = {tool["name"] for tool in mb._meta_tool_defs()}
    assert "ze_guide" in names
    assert "ze_confirm" not in names
