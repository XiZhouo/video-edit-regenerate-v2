"""HTTP command/confirm/history all consume the W03 CommitCoordinator boundary."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

# v3 平铺:注入兄弟目录 + 仓库根(server.py 在根)
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _d in (
    _ROOT,
    _ROOT / "01-core" / "01-data-structure" / "01-document",
    _ROOT / "01-core" / "01-data-structure" / "02-entity",
    _ROOT / "01-core" / "03-commands-operations" / "02-operation",
    _ROOT / "01-core" / "02-data-flow" / "04-history-rollback",
    _ROOT / "01-core" / "_protocol",
):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import server  # noqa: E402
from sdk import BrokerEventPublisher, CommitCoordinator, FileCommitStore  # noqa: E402
from command import CommandRegistry, CommandSpec  # noqa: E402
from document import Document, DocumentRegistry  # noqa: E402
from entity import Actor, Entity, ZeError  # noqa: E402
from operation import OpLog, Patch, new_operation  # noqa: E402
from sdk.idempotency import IdempotencyStore  # noqa: E402
from sdk.persistence import commit_execution_payload, execute_confirmation  # noqa: E402
from sdk.test_commit_surface_matrix import assert_commit_order  # noqa: E402
from history_authority import (  # noqa: E402
    HistoryAuthorityError,
    _issue_history_bind_capability,
    bind_history_authority,
)
from undo import UndoController, _verify_history_replay_derivation  # noqa: E402


_TEST_HISTORY_BOOTSTRAP_OWNER = object()


async def _anext(iterator):
    """Python 3.9-compatible async-next helper (builtins.anext starts in 3.10)."""
    return await iterator.__anext__()


def test_server_usage_label_consumes_sealed_registration_projection():
    registry = CommandRegistry()
    spec = CommandSpec(
        id="sealed.label",
        label="Original label",
        execute=lambda **kwargs: new_operation(
            command_id="sealed.label",
            source=kwargs["source"],
            actor=kwargs["actor"].to_dict(),
            patches=(),
            reverse_patches=(),
        ),
    )
    registry.register(spec)
    spec.label = "Forged label"
    projection = registry.registered_declaration("sealed.label")
    projection["label"] = "Forged projection"

    assert server._command_label(
        SimpleNamespace(cmd_reg=registry), "sealed.label",
    ) == "Original label"


def test_http_history_redacts_pipeline_authority_body():
    secret = "private-history-work-list"
    row = {
        "op": {
            "id": "1" * 32,
            "command_id": "pipeline.start",
            "pipeline": {
                "schema": "ze.pipeline-start.v1",
                "work_list": {"prompt": secret},
            },
        },
        "prev_hash": "0" * 64,
        "self_hash": "2" * 64,
    }
    ctx = SimpleNamespace(
        lock=threading.RLock(),
        op_log=SimpleNamespace(entries=lambda: [row]),
        undo=SimpleNamespace(can_undo=lambda: False, can_redo=lambda: False),
        doc=SimpleNamespace(version=3),
        generation=7,
    )

    payload = server.get_history(ctx)
    assert "pipeline" not in payload["entries"][0]["op"]
    assert payload["entries"][0]["canonical_body_redacted"] is True
    assert payload["entries"][0]["redacted_fields"] == ["op.pipeline"]
    assert secret not in repr(payload)
    assert payload["project_generation"] == 7
    assert "pipeline" in row["op"]


def _command_surface(tmp_path, monkeypatch, *, requires_confirm: bool = False):
    """Real registry + coordinator with every commit side effect observable."""
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
    commit = CommitCoordinator(
        doc,
        FileCommitStore(doc_path, oplog),
        publisher=BrokerEventPublisher(broker),
        lock=lock,
    )
    ctx = SimpleNamespace(
        doc=doc, doc_path=doc_path, op_log=oplog, broker=broker,
        cmd_reg=reg, lock=lock, commit=commit,
    )
    return events, doc, reg, ctx


def _run_http_wrapper(
    tmp_path, monkeypatch, events, *, scope: str, body: dict, action,
):
    """Run the real HTTP idempotency wrapper and observe durable success recording."""
    store = IdempotencyStore(tmp_path / f"{scope.replace('.', '-')}-idempotency.json")
    real_save = store._save_unlocked

    def save_with_success_event(state):
        succeeded = any(
            record.get("status") == "succeeded"
            for record in state.get("records", {}).values()
        )
        result = real_save(state)
        if succeeded:
            events.append("idempotency_success")
        return result

    store._save_unlocked = save_with_success_event
    monkeypatch.setattr(server._repo, "idempotency_store", store)
    request = SimpleNamespace(
        method="POST",
        headers={"Idempotency-Key": f"w00-{scope}"},
        url=SimpleNamespace(path=f"/api/{scope}"),
    )
    return server._run_idempotent(
        request,
        actor=Actor(user_id="u"),
        scope=scope,
        project_id="p1",
        body=body,
        action=action,
    )


def test_coordinator_saves_doc_before_appending_oplog(tmp_path, monkeypatch):
    """FileCommitStore is the sole HTTP durability seam: Document then OpLog."""
    order: list[str] = []

    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    assert type(doc) is Document
    real_save = Document.save

    def observed_save(self, path):
        if self is doc:
            order.append("save")
        return real_save(self, path)

    monkeypatch.setattr(Document, "save", observed_save)
    doc.add(Entity(type="t", data={"x": 1}, id="e1"))
    oplog = OpLog(tmp_path / "oplog.ndjson")
    op = new_operation(command_id="entity.add", source="user",
                       actor={"user_id": "u"}, patches=[], reverse_patches=[])

    real_append = oplog.append
    oplog.append = lambda o: order.append("append") or real_append(o)

    execution = CommitCoordinator(
        doc, FileCommitStore(tmp_path / "document.json", oplog),
    ).execute(lambda: op)

    assert order == ["save", "append"]
    assert len(oplog) == 1                              # op 确实记进了 OpLog
    assert (tmp_path / "document.json").exists()        # 文档确实落盘
    assert execution.receipt is not None
    assert execution.receipt.status.value == "committed"


def test_coordinator_legacy_live_collab_converges_to_safe_auto_commit(tmp_path):
    """旧 live_collab 不能再形成“只落 OpLog、Document 未落盘”的非法提交。"""
    doc = Document(schema_version="1.0.0", collaboration_mode="live_collab")
    oplog = OpLog(tmp_path / "oplog.ndjson")
    op = new_operation(command_id="entity.add", source="user",
                       actor={"user_id": "u"}, patches=[], reverse_patches=[])
    execution = CommitCoordinator(
        doc, FileCommitStore(tmp_path / "document.json", oplog),
    ).execute(lambda: op)
    assert len(oplog) == 1
    assert (tmp_path / "document.json").exists()
    assert execution.receipt is not None
    assert execution.receipt.status.value == "committed"
    assert execution.receipt.save_policy.value == "auto_commit"


def test_coordinator_explicit_auto_commit_saves_live_collab(tmp_path):
    """保存策略与协作拓扑解耦：live_collab 显式 auto_commit 也必须耐久。"""
    doc = Document(
        schema_version="1.0.0", collaboration_mode="live_collab",
        save_policy="auto_commit",
    )
    oplog = OpLog(tmp_path / "oplog.ndjson")
    op = new_operation(command_id="entity.add", source="user",
                       actor={"user_id": "u"}, patches=[], reverse_patches=[])

    execution = CommitCoordinator(
        doc, FileCommitStore(tmp_path / "document.json", oplog),
    ).execute(lambda: op)

    assert (tmp_path / "document.json").exists()
    assert execution.receipt is not None
    assert execution.receipt.status.value == "committed"
    assert execution.receipt.document_durable is True


def test_http_command_commits_before_publish(tmp_path, monkeypatch):
    """HTTP command must not publish a collaborator-visible event before durability."""
    events, doc, reg, ctx = _command_surface(tmp_path, monkeypatch)
    def action():
        execution = ctx.commit.execute(
            lambda: reg.dispatch(
                "baseline.write", {"x": 2}, actor=Actor(user_id="u"), source="user",
                doc=doc, op_log=None,
            )
        )
        result = commit_execution_payload(execution, document=doc)
        events.append("action_return")
        return result

    response = _run_http_wrapper(
        tmp_path, monkeypatch, events,
        scope="command.baseline.write", body={"x": 2}, action=action,
    )
    events.append("return")
    assert response["persistence"]["status"] == "committed"
    assert response["persistence"]["event_status"] == "published"
    assert_commit_order("http_command", events)


def test_http_confirm_commits_before_publish(tmp_path, monkeypatch):
    """HTTP confirm has the same target order and its pending phase stays side-effect free."""
    events, doc, reg, ctx = _command_surface(
        tmp_path, monkeypatch, requires_confirm=True,
    )
    actor = Actor(user_id="u")
    pending_execution = ctx.commit.execute(
        lambda: reg.dispatch(
            "baseline.write", {"x": 2}, actor=actor, source="user", doc=doc, op_log=None,
        )
    )
    pending = pending_execution.pending_confirmation
    assert pending is not None
    assert events == []
    def action():
        execution = execute_confirmation(
            registry=reg,
            coordinator=ctx.commit,
            token=pending.token,
            actor=actor,
            document=doc,
            approval_channel="human_http",
            approval_nonce=pending.approval_nonce,
        )
        result = commit_execution_payload(execution, document=doc)
        events.append("action_return")
        return result

    response = _run_http_wrapper(
        tmp_path, monkeypatch, events,
        scope="command.confirm", body={"token": pending.token}, action=action,
    )
    events.append("return")
    assert response["persistence"]["status"] == "committed"
    assert response["persistence"]["event_status"] == "published"
    assert_commit_order("http_confirm", events)


def test_command_pending_and_confirm_routes_return_project_generation(tmp_path, monkeypatch):
    events, _doc, _reg, ctx = _command_surface(
        tmp_path, monkeypatch, requires_confirm=True,
    )
    ctx.generation = 11
    ctx.lifecycle_guard = nullcontext
    actor = Actor(user_id="u")

    monkeypatch.setattr(
        server, "_idempotency_context_for",
        lambda _pid, _actor, _request: (ctx, actor),
    )
    monkeypatch.setattr(server, "_require_edit_ctx", lambda _ctx, _actor, _request: actor)
    monkeypatch.setattr(server, "credential_context", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(server._usage, "emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        server,
        "_run_idempotent",
        lambda _request, **kwargs: kwargs["action"](),
    )

    class FakeRequest:
        headers = {}
        query_params = {}

        def __init__(self, body):
            self._body = body

        async def json(self):
            return self._body

    pending = asyncio.run(server.post_command(
        "baseline.write", FakeRequest({"x": 2, "expected_generation": 11}),
        project_id="p", actor=actor,
    ))
    assert pending["project_generation"] == 11
    token = pending["pending"]["token"]
    approval_nonce = pending["pending"]["approval_nonce"]

    confirmed = asyncio.run(server.post_command_confirm(
        FakeRequest({
            "token": token, "approval_nonce": approval_nonce,
            "expected_generation": 11,
        }),
        project_id="p",
        actor=actor,
    ))
    assert confirmed["project_generation"] == 11
    assert confirmed["persistence"]["status"] == "committed"
    assert events == ["mutate", "save", "append", "publish"]


def test_queued_http_command_revalidates_acl_under_mutation_lock_after_revoke(
    tmp_path, monkeypatch,
):
    """A request queued on the writer lock cannot spend an obsolete ACL grant.

    The instrumented lock is a deterministic barrier, not a timing sleep.  With
    the historical check-before-lock ordering the writer passes while still a
    collaborator, announces its lock attempt, and commits after the owner has
    removed it.  The fixed ordering announces the same attempt first, then
    observes the new ACL after the lock is released and fails before producer,
    save, OpLog, or event publication.
    """
    events, doc, _reg, ctx = _command_surface(tmp_path, monkeypatch)
    doc.set_acl(
        {"owner": "owner", "collaborators": ["removed"]},
        expected_version=doc.version,
    )
    ctx.generation = 1
    ctx.lifecycle_guard = nullcontext

    writer_attempted_lock = threading.Event()

    class BarrierRLock:
        def __init__(self):
            self._lock = threading.RLock()

        def __enter__(self):
            if threading.current_thread().name == "revoked-writer":
                writer_attempted_lock.set()
            self._lock.acquire()
            return self

        def __exit__(self, exc_type, exc, tb):
            self._lock.release()

    lock = BarrierRLock()
    ctx.lock = lock
    ctx.commit.lock = lock

    actor = Actor(user_id="removed")
    monkeypatch.setattr(
        server, "_idempotency_context_for",
        lambda _pid, _actor, _request: (ctx, actor),
    )
    # This test is about document ACL ordering, not global account policy.
    monkeypatch.setattr(server, "_require_edit", lambda _actor: SimpleNamespace())
    monkeypatch.setattr(server, "credential_context", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(server._usage, "emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        server,
        "_run_idempotent",
        lambda _request, **kwargs: kwargs["action"](),
    )

    class FakeRequest:
        headers = {}
        query_params = {}
        cookies = {}

        async def json(self):
            return {"x": 99, "expected_generation": 1}

    result: dict[str, object] = {}

    def queued_writer() -> None:
        try:
            result["value"] = asyncio.run(server.post_command(
                "baseline.write", FakeRequest(), project_id="p", actor=actor,
            ))
        except BaseException as exc:  # captured for assertion in the test thread
            result["error"] = exc

    with lock:
        writer = threading.Thread(target=queued_writer, name="revoked-writer")
        writer.start()
        assert writer_attempted_lock.wait(2), "writer never reached the mutation lock barrier"

        def revoke_collaborator():
            before = doc.set_acl(
                {"owner": "owner", "collaborators": []},
                expected_version=doc.version,
            )
            after = doc.acl
            return new_operation(
                command_id="document.set_acl",
                source="user",
                actor={"user_id": "owner"},
                patches=[Patch("__document__", doc.id, "acl", "set", before, after)],
                reverse_patches=[Patch("__document__", doc.id, "acl", "set", after, before)],
            )

        owner_commit = ctx.commit.execute(revoke_collaborator)
        assert owner_commit.is_committed
        baseline_events = list(events)
        baseline_op_ids = [entry["op"]["id"] for entry in ctx.op_log.entries()]
        baseline_doc_version = doc.version

    writer.join(2)
    assert not writer.is_alive()
    assert isinstance(result.get("error"), ZeError)
    assert result["error"].code == "acl_denied"
    assert "value" not in result
    assert doc.get("t", "e1").data["x"] == 1
    assert doc.version == baseline_doc_version
    assert [entry["op"]["id"] for entry in ctx.op_log.entries()] == baseline_op_ids
    assert events == baseline_events


def test_asset_disposition_forces_download_for_scriptable_types():
    """存储型 XSS 防护:可内嵌脚本的类型(html/svg/xhtml/xml)强制 attachment 下载;
    图片/视频/音频/pdf/纯文本仍 inline 显示(不破坏正常 <img>/<video> 资产)。"""
    for mime in ("image/png", "image/jpeg", "video/mp4", "audio/mpeg",
                 "application/pdf", "text/plain", "image/webp; charset=utf-8"):
        assert server._asset_disposition(mime) == "inline", mime
    for mime in ("text/html", "image/svg+xml", "image/svg", "application/xhtml+xml",
                 "text/xml", "application/xml", "application/octet-stream",
                 "application/javascript", "TEXT/HTML", "Image/SVG+XML"):
        assert server._asset_disposition(mime) == "attachment", mime


def test_project_generation_envelope_overrides_untrusted_payload_without_mutation():
    payload = {"type": "entity_change", "project_generation": 1}
    fenced = server._with_project_generation(payload, SimpleNamespace(generation=7))
    assert fenced["project_generation"] == 7
    assert payload["project_generation"] == 1


def test_native_transport_promotes_explicit_query_token_over_shared_cookie(monkeypatch):
    """EventSource/media must follow the local account, not a stale SSO cookie."""
    captured = {}

    def fake_resolve_actor(**kwargs):
        captured.update(kwargs)
        return Actor(user_id="local")

    monkeypatch.setattr(server, "resolve_actor", fake_resolve_actor)
    request = SimpleNamespace(
        headers={}, cookies={server.SESSION_COOKIE_NAME: "shared-other-account"},
    )
    actor = server._get_transport_actor(request, "local-session-token")
    assert actor.user_id == "local"
    assert captured["authorization"] == "Bearer local-session-token"
    assert captured["cookie_token"] == "shared-other-account"
    assert captured["query_token"] is None

    request.headers = {"authorization": "Bearer header-session-token"}
    server._get_transport_actor(request, "local-session-token")
    assert captured["authorization"] == "Bearer header-session-token"


def test_acl_access_rejects_string_collaborator_substring():
    assert not server._can_access(
        {"owner": "owner", "collaborators": "alice"},
        "ali",
    )


def test_shared_asset_resolution_uses_durable_refs_not_cached_document():
    sha = "c" * 64
    shared_ref = SimpleNamespace(sha256=sha)
    calls: list[str] = []
    store = SimpleNamespace(
        local_ref_for=lambda _sha: None,
        ref_for=lambda value: calls.append(value) or shared_ref,
    )
    # ``doc`` deliberately advertises a stale reference. The helper's explicit
    # durable set is the only authority and must prevent fallback probing.
    ctx = SimpleNamespace(
        asset_store=store,
        doc=SimpleNamespace(types=lambda: ["x"], list=lambda *_a, **_k: []),
    )
    assert server._asset_ref_for_read_locked(ctx, sha, set()) is None
    assert calls == []
    assert server._asset_ref_for_read_locked(ctx, sha, {sha}) is shared_ref
    assert calls == [sha]


def test_sse_generation_probe_runs_blocking_lease_off_event_loop_thread():
    caller_thread = threading.get_ident()
    guard_threads: list[int] = []

    @contextmanager
    def lifecycle_guard():
        guard_threads.append(threading.get_ident())
        yield

    ctx = SimpleNamespace(generation=4, lifecycle_guard=lifecycle_guard)
    assert asyncio.run(server._probe_project_generation_async(ctx)) == 4
    assert guard_threads and all(thread_id != caller_thread for thread_id in guard_threads)


def test_sse_access_probe_reads_canonical_saved_document_revision(tmp_path, monkeypatch):
    """A healthy Document.save file must not be mistaken for cross-process drift."""
    doc = Document(
        id="project-1", schema_version="1.0.0", collaboration_mode="live_collab",
        acl={"owner": "u", "collaborators": []},
    )
    doc_path = tmp_path / "document.json"
    doc.save(doc_path)
    ctx = SimpleNamespace(doc=doc, doc_path=doc_path)
    request = SimpleNamespace(headers={}, cookies={}, query_params={})
    monkeypatch.setattr(server, "_stream_actor", lambda _request: Actor(user_id="u"))
    monkeypatch.setattr(server, "_require_tool_access", lambda _actor: object())

    # The on-disk ``_v`` written by Document.save exactly matches the in-memory
    # Document revision, so an ordinary SSE probe remains live.
    server._assert_stream_access_locked(ctx, request)

    raw = json.loads(doc_path.read_text(encoding="utf-8"))
    raw["_v"] += 1
    doc_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ZeError, match="project context changed") as exc_info:
        server._assert_stream_access_locked(ctx, request)
    assert exc_info.value.code == "project_generation_stale"


def test_sse_runtime_snapshot_retries_until_broker_cursor_window_is_stable(monkeypatch):
    cursors = iter((10, 11, 11, 11))

    class FakeBroker:
        def stats(self):
            return {"last_seq": next(cursors), "stream_id": "epoch"}

    pipeline_snapshots = iter(([{"run_id": "old"}], [{"run_id": "new"}]))
    entity_snapshots = iter(([{"phase": "old"}], [{"phase": "new"}]))
    ctx = SimpleNamespace(
        lifecycle_guard=nullcontext,
        lock=threading.RLock(),
        doc=SimpleNamespace(id="p"),
        broker=FakeBroker(),
        supervisor=SimpleNamespace(
            snapshot=lambda: next(pipeline_snapshots),
            entity_status_snapshot=lambda: next(entity_snapshots),
        ),
    )
    monkeypatch.setattr(server, "_assert_stream_access_locked", lambda *_args: None)
    _doc_id, pipeline, entity, cursor = server._sse_initial_snapshot(
        ctx, SimpleNamespace(),
    )
    assert cursor == 11
    assert pipeline["runs"] == [{"run_id": "new"}]
    assert entity["statuses"] == [{"phase": "new"}]
    assert entity["replace"] is True


def test_sse_initial_and_ordinary_frames_carry_project_generation(monkeypatch):
    class FakeBroker:
        callback = None
        unsubscribed = False

        def subscribe(self, *, callback, **_kwargs):
            self.callback = callback
            return SimpleNamespace(
                replay_gap=False,
                unsubscribe=lambda: setattr(self, "unsubscribed", True),
            )

        def stats(self):
            return {"last_seq": 1, "stream_id": "epoch-1"}

    class FakeRequest:
        query_params = {}
        headers = {}
        cookies = {}

        async def is_disconnected(self):
            return False

    @contextmanager
    def lifecycle_guard():
        yield

    broker = FakeBroker()
    supervisor = SimpleNamespace(snapshot=lambda: [], entity_status_snapshot=lambda: [])
    ctx = SimpleNamespace(
        project_id="project-1",
        generation=6,
        lifecycle_guard=lifecycle_guard,
        lock=threading.RLock(),
        doc=SimpleNamespace(id="project-1", version=1),
        broker=broker,
        supervisor=supervisor,
    )
    monkeypatch.setattr(server, "_assert_stream_access_locked", lambda *_args: None)

    async def scenario():
        response = await server.events(
            FakeRequest(), since_seq=0, event_epoch=None, ctx=ctx,
            actor=Actor(user_id="u"),
        )
        assert broker.callback is not None
        broker.callback({
            "type": "entity_change", "doc_id": "project-1", "op_id": "op-1",
            "changes": [], "seq": 1, "document_version": 1,
        })
        iterator = response.body_iterator
        frames = [await _anext(iterator) for _ in range(5)]
        await iterator.aclose()
        return frames

    frames = asyncio.run(scenario())
    payloads = [json.loads(frame.removeprefix("data: ").strip())
                for frame in frames if frame.startswith("data: ")]
    assert [payload["type"] for payload in payloads] == [
        "project_generation", "entity_change", "pipeline_status", "entity_status",
    ]
    assert all(payload["project_generation"] == 6 for payload in payloads)
    assert broker.unsubscribed is True


def test_sse_replay_gap_is_explicit_and_terminal():
    class FakeBroker:
        def subscribe(self, *, callback, **_kwargs):
            return SimpleNamespace(
                replay_gap=True, gap_reason="replay_buffer_gap", unsubscribe=lambda: None,
            )

        def stats(self):
            return {"last_seq": 9, "stream_id": "epoch-gap"}

    class FakeRequest:
        async def is_disconnected(self):
            return False

    ctx = SimpleNamespace(generation=3, broker=FakeBroker(), doc=SimpleNamespace(id="p"))

    async def scenario():
        response = await server.events(
            FakeRequest(), since_seq=1, event_epoch="epoch-gap", ctx=ctx,
            actor=Actor(user_id="u"),
        )
        iterator = response.body_iterator
        frames = [await _anext(iterator), await _anext(iterator)]
        with pytest.raises(StopAsyncIteration):
            await _anext(iterator)
        return frames

    frames = asyncio.run(scenario())
    payload = json.loads(frames[1].removeprefix("data: ").strip())
    assert payload == {
        "type": "resync_required",
        "reason": "replay_buffer_gap",
        "event_cursor": 9,
        "project_generation": 3,
        "event_epoch": "epoch-gap",
    }


def test_sse_acl_revocation_control_precedes_and_suppresses_queued_event(monkeypatch):
    class FakeBroker:
        callback = None

        def subscribe(self, *, callback, **_kwargs):
            self.callback = callback
            return SimpleNamespace(replay_gap=False, unsubscribe=lambda: None)

        def stats(self):
            return {"last_seq": 1, "stream_id": "epoch-acl"}

    class FakeRequest:
        async def is_disconnected(self):
            return False

    broker = FakeBroker()
    ctx = SimpleNamespace(generation=5, broker=broker, doc=SimpleNamespace(id="p"))
    monkeypatch.setattr(
        server, "_sse_initial_snapshot",
        lambda _ctx, _request: (
            "p", {"type": "pipeline_status"}, {"type": "entity_status"}, 0,
        ),
    )

    async def denied_probe(_ctx, _request=None):
        raise ZeError("acl_denied", "removed")

    monkeypatch.setattr(server, "_probe_project_generation_async", denied_probe)

    async def scenario():
        response = await server.events(
            FakeRequest(), since_seq=1, event_epoch="epoch-acl", ctx=ctx,
            actor=Actor(user_id="removed"),
        )
        broker.callback({
            "type": "entity_change", "seq": 1, "document_version": 2,
            "doc_id": "p", "changes": [],
        })
        iterator = response.body_iterator
        frames = [await _anext(iterator) for _ in range(2)]
        await iterator.aclose()
        return frames

    frames = asyncio.run(scenario())
    payloads = [
        json.loads(frame.removeprefix("data: ").strip())
        for frame in frames if frame.startswith("data: ")
    ]
    assert [payload["type"] for payload in payloads] == ["access_revoked"]
    assert all(payload["type"] != "entity_change" for payload in payloads)


def test_sse_queue_overflow_emits_resync_without_partial_replay():
    class FakeBroker:
        callback = None

        def subscribe(self, *, callback, **_kwargs):
            self.callback = callback
            return SimpleNamespace(replay_gap=False, unsubscribe=lambda: None)

        def stats(self):
            return {"last_seq": 300, "stream_id": "epoch-overflow"}

    class FakeRequest:
        async def is_disconnected(self):
            return False

    broker = FakeBroker()
    ctx = SimpleNamespace(generation=2, broker=broker, doc=SimpleNamespace(id="p"))

    async def scenario():
        response = await server.events(
            FakeRequest(), since_seq=1, event_epoch="epoch-overflow", ctx=ctx,
            actor=Actor(user_id="u"),
        )
        for seq in range(1, 258):
            broker.callback({"type": "entity_change", "seq": seq, "changes": []})
        iterator = response.body_iterator
        frames = [await _anext(iterator), await _anext(iterator)]
        await iterator.aclose()
        return frames

    frames = asyncio.run(scenario())
    payload = json.loads(frames[1].removeprefix("data: ").strip())
    assert payload["type"] == "resync_required"
    assert payload["reason"] == "queue_overflow"


def test_asset_upload_reads_body_before_acquiring_generation_lease():
    events: list[str] = []

    class FakeRequest:
        query_params = {"ext": ".png", "expected_generation": "1"}
        headers = {"content-type": "image/png"}
        cookies = {server.GUEST_COOKIE_NAME: "guest-1"}

        async def stream(self):
            events.append("body")
            yield b"png"

    @contextmanager
    def lifecycle_guard():
        events.append("lease-enter")
        yield
        events.append("lease-exit")

    ref = SimpleNamespace(to_dict=lambda: {"sha256": "a" * 64})
    asset_store = SimpleNamespace(
        local_ref_for=lambda _sha: None,
        total_size=lambda: 0,
        put=lambda data, **_kwargs: events.append(f"put:{data.decode()}") or ref,
    )
    ctx = SimpleNamespace(
        project_id="demo-1",
        generation=1,
        lifecycle_guard=lifecycle_guard,
        lock=threading.RLock(),
        asset_store=asset_store,
        doc=SimpleNamespace(
            config={"project_kind": "demo_session"},
            acl={"owner": "guest-1", "collaborators": []},
        ),
    )

    result = asyncio.run(server.put_asset(FakeRequest(), ctx=ctx, actor=Actor(user_id="")))
    assert result == {"sha256": "a" * 64, "project_generation": 1}
    assert events == ["body", "lease-enter", "put:png", "lease-exit"]


def test_manifest_download_is_generation_fenced(tmp_path):
    name = "20260710-120000-art-abcdef.ze-pack"
    exports = tmp_path / "exports"
    exports.mkdir()
    (exports / name).write_bytes(b"zip")
    ctx = SimpleNamespace(
        generation=4,
        doc=SimpleNamespace(id="p"),
        data_dir=tmp_path,
    )
    request = SimpleNamespace(
        query_params={"expected_generation": "4"}, headers={},
    )
    response = server.get_manifest_file(name, request, ctx=ctx)
    assert response.body == b"zip"
    assert response.headers["x-project-generation"] == "4"
    assert response.headers["cache-control"] == "no-store"

    stale = SimpleNamespace(
        query_params={"expected_generation": "3"}, headers={},
    )
    with pytest.raises(ZeError) as captured:
        server.get_manifest_file(name, stale, ctx=ctx)
    assert captured.value.code == "project_generation_stale"


def test_project_export_download_is_fixed_generation_fenced_and_nosniff(tmp_path):
    name = "20260713-120000-image-to-3d-0123456789ab.json"
    exports = tmp_path / "exports"
    exports.mkdir()
    (exports / name).write_bytes(b'{"schema":"example.v1"}\n')
    ctx = SimpleNamespace(generation=5, doc=SimpleNamespace(id="p"), data_dir=tmp_path)
    request = SimpleNamespace(query_params={"expected_generation": "5"}, headers={})
    route = next(
        item for item in server.app.routes
        if getattr(item, "path", "") == "/api/export/file/{name}"
    )
    assert any(
        dependency.call is server._require_member_leased
        for dependency in route.dependant.dependencies
    )

    response = server.get_project_export_file(name, request, ctx=ctx)

    assert response.body == b'{"schema":"example.v1"}\n'
    assert response.media_type == "application/json"
    assert response.headers["content-disposition"] == f'attachment; filename="{name}"'
    assert response.headers["x-project-generation"] == "5"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"

    stale = SimpleNamespace(query_params={"expected_generation": "4"}, headers={})
    with pytest.raises(ZeError) as captured:
        server.get_project_export_file(name, stale, ctx=ctx)
    assert captured.value.code == "project_generation_stale"


def test_project_export_download_rejects_names_and_non_regular_files_as_one_404(tmp_path):
    exports = tmp_path / "exports"
    exports.mkdir()
    ctx = SimpleNamespace(generation=2, doc=SimpleNamespace(id="p"), data_dir=tmp_path)
    request = SimpleNamespace(query_params={"expected_generation": "2"}, headers={})

    invalid_names = [
        "../20260713-120000-x-0123456789ab.json",
        "/20260713-120000-x-0123456789ab.json",
        "20260713-120000-x%2fy-0123456789ab.json",
        "20260713-120000-x-0123456789ab.json.extra",
        "20260713-120000-x-0123456789ab.ze-pack",
        f"20260713-120000-{'x' * 300}-0123456789ab.json",
    ]
    baseline = server.get_project_export_file(invalid_names[0], request, ctx=ctx)
    assert baseline.status_code == 404
    for invalid in invalid_names[1:]:
        response = server.get_project_export_file(invalid, request, ctx=ctx)
        assert response.status_code == 404
        assert response.body == baseline.body

    outside = tmp_path / "outside.json"
    outside.write_text("secret", encoding="utf-8")
    symlink_name = "20260713-120000-symlink-0123456789ab.json"
    (exports / symlink_name).symlink_to(outside)
    directory_name = "20260713-120000-directory-0123456789ab.json"
    (exports / directory_name).mkdir()
    empty_name = "20260713-120000-empty-0123456789ab.json"
    (exports / empty_name).touch()
    oversized_name = "20260713-120000-oversized-0123456789ab.json"
    with open(exports / oversized_name, "wb") as oversized:
        oversized.truncate(server._MAX_PROJECT_EXPORT_BYTES + 1)
    hardlink_name = "20260713-120000-hardlink-0123456789ab.json"
    hardlink_source = tmp_path / "hardlink-source.json"
    hardlink_source.write_bytes(b'{"secret":true}')
    os.link(hardlink_source, exports / hardlink_name)
    special_names = [symlink_name, directory_name, empty_name, oversized_name, hardlink_name]
    if hasattr(os, "mkfifo"):
        fifo_name = "20260713-120000-fifo-0123456789ab.json"
        os.mkfifo(exports / fifo_name)
        special_names.append(fifo_name)
    for special in special_names:
        response = server.get_project_export_file(special, request, ctx=ctx)
        assert response.status_code == 404
        assert response.body == baseline.body


def test_project_export_download_rejects_a_file_changed_during_read(tmp_path, monkeypatch):
    name = "20260713-120000-racing-0123456789ab.json"
    exports = tmp_path / "exports"
    exports.mkdir()
    (exports / name).write_bytes(b'{"stable_size":true}')
    ctx = SimpleNamespace(generation=2, doc=SimpleNamespace(id="p"), data_dir=tmp_path)
    request = SimpleNamespace(query_params={"expected_generation": "2"}, headers={})

    real_fstat = os.fstat
    calls = 0

    def changed_after_read(fd):
        nonlocal calls
        calls += 1
        info = real_fstat(fd)
        if calls != 3:
            return info
        return SimpleNamespace(
            st_mode=info.st_mode,
            st_size=info.st_size,
            st_dev=info.st_dev,
            st_ino=info.st_ino,
            st_mtime_ns=info.st_mtime_ns + 1,
            st_nlink=info.st_nlink,
            st_uid=info.st_uid,
        )

    monkeypatch.setattr(server.os, "fstat", changed_after_read)
    response = server.get_project_export_file(name, request, ctx=ctx)

    assert calls == 3
    assert response.status_code == 404
    assert response.body == server._project_export_not_found().body


def test_undo_route_persists_before_appending_no_double_save(tmp_path, monkeypatch):
    """The real HTTP history helper submits deferred undo through one coordinator."""
    order: list[str] = []

    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    assert type(doc) is Document
    real_apply_patches_atomically = Document.apply_patches_atomically
    real_save = Document.save

    def observed_apply_patches_atomically(
        self, patches, *, actor, _replay_capability=None,
    ):
        if self is doc:
            order.append("mutate")
        return real_apply_patches_atomically(
            self, patches, actor=actor,
            _replay_capability=_replay_capability,
        )

    def observed_save(self, path):
        if self is doc:
            order.append("save")
        return real_save(self, path)

    monkeypatch.setattr(
        Document, "apply_patches_atomically", observed_apply_patches_atomically,
    )
    monkeypatch.setattr(Document, "save", observed_save)
    doc.add(Entity(type="t", data={"x": 1}, id="e1"))
    doc.get("t", "e1").update({"x": 2}, expected_version=1, actor=Actor(user_id="u"))
    oplog = OpLog(tmp_path / "oplog.ndjson")
    oplog.append(new_operation(
        command_id="entity.update", source="user", actor={"user_id": "u"}, kind="native",
        patches=[Patch("t", "e1", "x", "set", 1, 2)],
        reverse_patches=[Patch("t", "e1", "x", "set", 2, 1)]))
    undo_ctl = UndoController(oplog, doc)

    real_append = oplog.append
    oplog.append = lambda o: order.append("append") or real_append(o)
    broker = SimpleNamespace(
        publish_change=lambda payload: order.append("publish")
    )
    lock = threading.RLock()
    ctx = SimpleNamespace(
        doc=doc, doc_path=tmp_path / "document.json", op_log=oplog,
        broker=broker, undo=undo_ctl, lock=lock,
    )
    ctx.commit = CommitCoordinator(
        doc, FileCommitStore(ctx.doc_path, oplog),
        publisher=BrokerEventPublisher(broker), lock=lock,
    )

    result = server._history_commit(ctx, actor=Actor(user_id="u"), kind="undo")

    order.append("return")

    assert_commit_order("undo", order)
    assert doc.get("t", "e1").data["x"] == 1            # 撤销真生效
    assert len(result["ops"]) == 1
    assert len(result["changes"]) == 1
    assert result["persistence"]["status"] == "committed"
    assert "can_undo" in result and "can_redo" in result


def test_redo_route_persists_before_append_before_publish(tmp_path, monkeypatch):
    """redo 与 undo 共用同一 durability-before-visibility contract."""
    events: list[str] = []
    observe = {"enabled": False}

    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    assert type(doc) is Document
    real_apply_patches_atomically = Document.apply_patches_atomically
    real_save = Document.save

    def observed_apply_patches_atomically(
        self, patches, *, actor, _replay_capability=None,
    ):
        if self is doc and observe["enabled"]:
            events.append("mutate")
        return real_apply_patches_atomically(
            self, patches, actor=actor,
            _replay_capability=_replay_capability,
        )

    def observed_save(self, path):
        if self is doc and observe["enabled"]:
            events.append("save")
        return real_save(self, path)

    monkeypatch.setattr(
        Document, "apply_patches_atomically", observed_apply_patches_atomically,
    )
    monkeypatch.setattr(Document, "save", observed_save)
    doc.add(Entity(type="t", data={"x": 1}, id="e1"))
    doc.get("t", "e1").update({"x": 2}, expected_version=1, actor=Actor(user_id="u"))
    oplog = OpLog(tmp_path / "oplog.ndjson")
    oplog.append(new_operation(
        command_id="entity.update", source="user", actor={"user_id": "u"}, kind="native",
        patches=[Patch("t", "e1", "x", "set", 1, 2)],
        reverse_patches=[Patch("t", "e1", "x", "set", 2, 1)],
    ))
    undo_ctl = UndoController(oplog, doc)
    seed_lock = threading.RLock()
    seed_broker = SimpleNamespace(publish_change=lambda payload: None)
    seed_ctx = SimpleNamespace(
        doc=doc, doc_path=tmp_path / "document.json", op_log=oplog,
        broker=seed_broker, undo=undo_ctl, lock=seed_lock,
    )
    seed_ctx.commit = CommitCoordinator(
        doc, FileCommitStore(seed_ctx.doc_path, oplog),
        publisher=BrokerEventPublisher(seed_broker), lock=seed_lock,
    )
    server._history_commit(seed_ctx, actor=Actor(user_id="u"), kind="undo")
    assert doc.get("t", "e1").data["x"] == 1

    observe["enabled"] = True
    real_append = oplog.append
    oplog.append = lambda op: events.append("append") or real_append(op)
    broker = SimpleNamespace(publish_change=lambda payload: events.append("publish"))
    lock = threading.RLock()
    ctx = SimpleNamespace(
        doc=doc, doc_path=tmp_path / "document.json", op_log=oplog,
        broker=broker, undo=undo_ctl, lock=lock,
    )
    ctx.commit = CommitCoordinator(
        doc, FileCommitStore(ctx.doc_path, oplog),
        publisher=BrokerEventPublisher(broker), lock=lock,
    )

    result = server._history_commit(ctx, actor=Actor(user_id="u"), kind="redo")
    events.append("return")

    assert_commit_order("redo", events)
    assert doc.get("t", "e1").data["x"] == 2
    assert len(result["ops"]) == 1
    assert len(result["changes"]) == 1
    assert result["persistence"]["status"] == "committed"


class _HistoryStoreMustNotRun:
    def __init__(self):
        self.calls = 0

    def commit(self, _intent):
        self.calls += 1
        raise AssertionError("invalid history must be rejected before store")


def _protected_invalid_history_context(kind):
    registry = DocumentRegistry()
    registry.declare_entity_type(
        name="t",
        schema={
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "additionalProperties": False,
        },
        mutation_policy={
            "schema": "ze.entity-mutation-policy.v1",
            "lifecycle": {
                "create": [],
                "rename": [],
                "soft_delete": [],
                "restore": [],
                "hard_remove": [],
            },
            "fields": {"x": ["Business.Update"]},
        },
    )
    registry.set_collaboration_mode("with_save")
    document = registry.build_document(schema_version="1.0", id="history-doc")
    value = 2 if kind == "undo" else 1
    entity = document.add(Entity(type="t", id="e1", data={"x": value}))
    commands = CommandRegistry()
    commands.register(CommandSpec(
        id="Business.Update",
        label="business update",
        execute=lambda **_kwargs: None,
        mutation_authority=True,
    ))
    document._validate_mutation_policy_commands(commands)

    op_log = OpLog()
    native = new_operation(
        command_id="Business.Update",
        source="user",
        actor=Actor(user_id="u").to_dict(),
        patches=(
            Patch(
                "t", "e1", "x", "set",
                999 if kind == "undo" else 1,
                2,
            ),
        ),
        reverse_patches=(Patch("t", "e1", "x", "set", 2, 1),),
        group_id="group-1",
    )
    op_log.append(native)
    controller = UndoController(op_log, document)
    if kind == "redo":
        malformed_undo = new_operation(
            command_id="undo",
            source="user",
            actor=Actor(user_id="u").to_dict(),
            patches=(Patch("t", "e1", "x", "set", 2, 1),),
            reverse_patches=(Patch("t", "e1", "x", "set", 999, 2),),
            group_id="group-1",
            kind="rollback",
        )
        op_log.append(malformed_undo)
        controller._redo_stack.append(malformed_undo.id)

    store = _HistoryStoreMustNotRun()
    coordinator = CommitCoordinator(document, store)
    authority = bind_history_authority(
        _issue_history_bind_capability(
            bootstrap_owner=_TEST_HISTORY_BOOTSTRAP_OWNER,
        ),
        bootstrap_owner=_TEST_HISTORY_BOOTSTRAP_OWNER,
        document=document,
        op_log=op_log,
        undo_controller=controller,
        coordinator=coordinator,
        store=store,
        project_id=document.id,
        generation=1,
        runtime_revision="test-server-history",
        replay_verifier=_verify_history_replay_derivation,
    )
    context = SimpleNamespace(
        doc=document,
        undo=controller,
        commit=coordinator,
        history_authority=authority,
    )
    return context, entity, op_log, store


@pytest.mark.parametrize("kind", ("undo", "redo"))
def test_history_commit_discards_deferred_ledger_when_validation_raises(kind):
    ctx, entity, op_log, store = _protected_invalid_history_context(kind)
    before_entity = entity.to_dict()
    before_entries = op_log.entries()
    before_redo = tuple(ctx.undo._redo_stack)

    # Retry proves the first rejected request did not leave the controller in
    # its "another deferred mutation is pending" poison state.
    for _attempt in range(2):
        with pytest.raises(ZeError) as captured:
            server._history_commit(ctx, actor=Actor(user_id="u"), kind=kind)

        assert captured.value.code == "mutation_operation_patch_mismatch"
        assert store.calls == 0
        assert entity.to_dict() == before_entity
        assert op_log.entries() == before_entries
        assert tuple(ctx.undo._redo_stack) == before_redo
        assert ctx.undo._pending_deferred == {}
        assert ctx.undo._pending_operation_bodies == {}


def test_history_commit_keeps_frozen_deferred_ledger_for_repair():
    registry = DocumentRegistry()
    registry.declare_entity_type(
        name="t",
        schema={
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "additionalProperties": False,
        },
        mutation_policy={
            "schema": "ze.entity-mutation-policy.v1",
            "lifecycle": {
                "create": [], "rename": [], "soft_delete": [],
                "restore": [], "hard_remove": [],
            },
            "fields": {"x": ["Business.Update"]},
        },
    )
    registry.set_collaboration_mode("with_save")
    document = registry.build_document(schema_version="1.0", id="frozen-history")
    entity = document.add(Entity(type="t", id="e1", data={"x": 2}))
    commands = CommandRegistry()
    commands.register(CommandSpec(
        id="Business.Update",
        label="business update",
        execute=lambda **_kwargs: None,
        mutation_authority=True,
    ))
    document._validate_mutation_policy_commands(commands)
    op_log = OpLog()
    op_log.append(new_operation(
        command_id="Business.Update",
        source="user",
        actor=Actor(user_id="u").to_dict(),
        patches=(Patch("t", "e1", "x", "set", 1, 2),),
        reverse_patches=(Patch("t", "e1", "x", "set", 2, 1),),
    ))
    controller = UndoController(op_log, document)

    class AppendThenFailStore:
        def __init__(self):
            self.op_log = op_log

        def commit(self, intent):
            for operation in intent.operations:
                self.op_log.append(operation)
            raise RuntimeError("injected after canonical append")

    store = AppendThenFailStore()
    coordinator = CommitCoordinator(document, store)
    authority = bind_history_authority(
        _issue_history_bind_capability(
            bootstrap_owner=_TEST_HISTORY_BOOTSTRAP_OWNER,
        ),
        bootstrap_owner=_TEST_HISTORY_BOOTSTRAP_OWNER,
        document=document,
        op_log=op_log,
        undo_controller=controller,
        coordinator=coordinator,
        store=store,
        project_id=document.id,
        generation=1,
        runtime_revision="test-server-frozen-history",
        replay_verifier=_verify_history_replay_derivation,
    )
    context = SimpleNamespace(
        doc=document,
        undo=controller,
        commit=coordinator,
        history_authority=authority,
    )

    with pytest.raises(HistoryAuthorityError) as captured:
        server._history_commit(context, actor=Actor(user_id="u"), kind="undo")

    assert captured.value.code == "history_authority_mismatch"
    assert captured.value.outcome_unknown is True
    assert authority.state == "FROZEN"
    assert entity.data["x"] == 1
    assert len(op_log) == 2
    assert len(controller._pending_deferred) == 1
    assert len(controller._pending_operation_bodies) == 1


@pytest.mark.parametrize(
    "path",
    (
        "/api/command/example.write",
        "/api/command/confirm",
        "/api/undo",
    ),
)
def test_history_unknown_global_handler_is_safe_for_all_write_entries(path):
    error = HistoryAuthorityError(
        "history_append_outcome_indeterminate",
        "private persistence detail",
    ).with_commit_facts({
        "phase": "oplog",
        "outcome": "indeterminate",
        "commit_id": "c" * 32,
        "retry_safe": False,
        "document_durable": True,
        "oplog_durable": False,
    })
    request = SimpleNamespace(
        method="POST",
        url=SimpleNamespace(path=path),
    )

    response = asyncio.run(server._history_authority_error_handler(request, error))
    body = json.loads(response.body)

    assert response.status_code == 409
    assert body["error"]["code"] == "history_authority_mismatch"
    assert "do not retry" in body["error"]["message"]
    assert "reload" not in body["error"]["message"]
    assert body["error"]["context"] == {
        "reason": "history_append_outcome_indeterminate",
        "outcome_unknown": True,
        "durability_phase": "post_write_verification",
        "commit_facts": error.commit_facts,
    }


def test_preflight_history_handler_requires_reload_but_is_not_unknown():
    error = HistoryAuthorityError(
        "history_head_changed_before_store",
        "private persistence detail",
    )
    request = SimpleNamespace(
        method="POST",
        url=SimpleNamespace(path="/api/command/example.write"),
    )

    response = asyncio.run(server._history_authority_error_handler(request, error))
    body = json.loads(response.body)

    assert response.status_code == 409
    assert "reload" in body["error"]["message"]
    assert body["error"]["context"] == {
        "reason": "history_head_changed_before_store",
        "outcome_unknown": False,
        "durability_phase": "preflight",
    }


def test_suffix_verification_unknown_tombstones_same_idempotency_key(
    tmp_path,
    monkeypatch,
):
    document = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        id="suffix-verification-project",
    )
    op_log = OpLog(tmp_path / "oplog.ndjson")
    commit_store = FileCommitStore(tmp_path / "document.json", op_log)
    coordinator = CommitCoordinator(document, commit_store)
    controller = UndoController(op_log, document)
    authority = bind_history_authority(
        _issue_history_bind_capability(
            bootstrap_owner=_TEST_HISTORY_BOOTSTRAP_OWNER,
        ),
        bootstrap_owner=_TEST_HISTORY_BOOTSTRAP_OWNER,
        document=document,
        op_log=op_log,
        undo_controller=controller,
        coordinator=coordinator,
        store=commit_store,
        project_id=document.id,
        generation=1,
        runtime_revision="test-suffix-idempotency",
        replay_verifier=_verify_history_replay_derivation,
    )

    def fail_suffix(_start_count):
        raise OSError("injected post-append suffix probe failure")

    monkeypatch.setattr(
        op_log, "_canonical_history_suffix_snapshot", fail_suffix,
    )
    idempotency_store = IdempotencyStore(tmp_path / "idempotency.json")
    monkeypatch.setattr(server._repo, "idempotency_store", idempotency_store)
    request = SimpleNamespace(
        method="POST",
        headers={"Idempotency-Key": "suffix-verification-unknown"},
        url=SimpleNamespace(path="/api/command/suffix.verify"),
    )
    actor = Actor(user_id="u")
    calls = 0

    def action():
        nonlocal calls
        calls += 1
        operation = new_operation(
            command_id="suffix.verify",
            source="user",
            actor=actor.to_dict(),
            patches=(),
            reverse_patches=(),
        )
        coordinator.execute(lambda: operation)
        return {"unexpected": True}

    def run():
        return server._run_idempotent(
            request,
            actor=actor,
            scope="command:suffix.verify",
            project_id=document.id,
            body={"value": 1},
            action=action,
            outcome_unknown_classifier=server._history_outcome_unknown,
        )

    with pytest.raises(HistoryAuthorityError) as first:
        run()

    assert first.value.reason == "history_suffix_verification_failed"
    assert first.value.outcome_unknown is True
    assert first.value.commit_facts == {
        "phase": "oplog",
        "outcome": "committed",
        "commit_id": first.value.commit_facts["commit_id"],
        "retry_safe": False,
        "document_durable": True,
        "oplog_durable": True,
    }
    assert len(first.value.commit_facts["commit_id"]) == 32
    assert authority.state == "FROZEN"
    assert len(op_log) == 1

    with pytest.raises(ZeError) as replay:
        run()

    assert replay.value.code == "idempotency_outcome_unknown"
    assert calls == 1
    ledger = json.loads(idempotency_store.path.read_text(encoding="utf-8"))
    records = tuple(ledger["records"].values())
    assert len(records) == 1
    assert records[0]["status"] == "outcome_unknown"
    assert records[0]["error_type"] == "HistoryAuthorityError"
