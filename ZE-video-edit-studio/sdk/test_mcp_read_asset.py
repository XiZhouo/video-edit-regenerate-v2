"""ze_read_asset MCP 元工具:三模式 + durable ACL 闸门回归。"""
from __future__ import annotations

import base64
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdk import Actor, AssetStore, Document, Entity, ZeError  # noqa: E402
import sdk.mcp_bridge as mb  # noqa: E402

# 1x1 PNG(真图字节,让 describe 走 image content block)。
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


class _Repo:
    def __init__(self, ctx):
        self.ctx = ctx

    def list_project_ids(self):
        return ["p1"]

    def get_or_load(self, project_id):
        assert project_id == "p1"
        return self.ctx


def _make(tmp_path: Path):
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc_path = tmp_path / "document.json"
    doc.save(doc_path)
    asset_store = AssetStore(tmp_path / "assets")
    ctx = SimpleNamespace(
        doc=doc, doc_path=doc_path, asset_store=asset_store,
        lock=threading.RLock(), generation=1,
    )
    repo = _Repo(ctx)
    actor_ctx = mb.McpActorCtx(
        actor=Actor(user_id="__mcp__", source="ai"),
        enforce_acl=False, project_hint="p1",
    )
    return ctx, repo, actor_ctx


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def test_meta_mode_returns_metadata(tmp_path):
    ctx, repo, actor_ctx = _make(tmp_path)
    ref = ctx.asset_store.put(b"hello world", mime="text/plain", ext=".txt")

    result = mb._call_tool(repo, actor_ctx, "ze_read_asset",
                           {"sha256": ref.sha256, "mode": "meta"})
    body = _payload(result)
    assert result["isError"] is False
    assert body == {"sha256": ref.sha256, "mime": "text/plain",
                    "size": 11, "ext": ".txt"}


def test_text_mode_returns_content(tmp_path):
    ctx, repo, actor_ctx = _make(tmp_path)
    ref = ctx.asset_store.put(b"line1\nline2", mime="text/plain", ext=".txt")

    body = _payload(mb._call_tool(repo, actor_ctx, "ze_read_asset",
                                  {"sha256": ref.sha256, "mode": "text"}))
    assert body["text"] == "line1\nline2"
    assert body["truncated"] is False


def test_text_mode_rejects_binary_mime(tmp_path):
    ctx, repo, actor_ctx = _make(tmp_path)
    ref = ctx.asset_store.put(_PNG, mime="image/png", ext=".png")

    result = mb._call_tool(repo, actor_ctx, "ze_read_asset",
                           {"sha256": ref.sha256, "mode": "text"})
    assert result["isError"] is True
    assert _payload(result)["error"]["code"] == "not_text_asset"


def test_describe_mode_returns_image_content_block(tmp_path):
    ctx, repo, actor_ctx = _make(tmp_path)
    ref = ctx.asset_store.put(_PNG, mime="image/png", ext=".png")

    result = mb._call_tool(repo, actor_ctx, "ze_read_asset",
                           {"sha256": ref.sha256, "mode": "describe"})
    assert result["isError"] is False
    block = result["content"][0]
    assert block["type"] == "image"
    assert block["mimeType"] == "image/png"
    assert base64.b64decode(block["data"]) == _PNG


def test_describe_mode_rejects_non_image(tmp_path):
    ctx, repo, actor_ctx = _make(tmp_path)
    ref = ctx.asset_store.put(b"not an image", mime="text/plain", ext=".txt")

    result = mb._call_tool(repo, actor_ctx, "ze_read_asset",
                           {"sha256": ref.sha256, "mode": "describe"})
    assert result["isError"] is True
    assert _payload(result)["error"]["code"] == "not_viewable_asset"


def test_unreferenced_sha_denied_no_blob(tmp_path):
    """非本地、且不被 canonical Document 引用的 sha → acl_denied(过同一道闸门)。"""
    ctx, repo, actor_ctx = _make(tmp_path)
    bogus = "a" * 64

    with pytest.raises(ZeError) as exc:
        mb._call_tool(repo, actor_ctx, "ze_read_asset",
                      {"sha256": bogus, "mode": "meta"})
    assert exc.value.code == "acl_denied"


def test_shared_blob_denied_until_document_references_it(tmp_path):
    """共享池有 blob 但本项目未引用 → 拒;canonical Document 引用后 → 放行。

    直接验 durable ACL 闸门:local_ref_for miss + 不在 durable_refs → None → acl_denied。
    """
    shared_root = tmp_path / "shared"
    shared = AssetStore(shared_root)
    ref = shared.put(_PNG, mime="image/png", ext=".png")

    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc_path = tmp_path / "document.json"
    doc.save(doc_path)
    # 本地 store 空,fallback 指向共享池 → blob 可达但未被引用。
    local = AssetStore(tmp_path / "assets", fallback=shared)
    ctx = SimpleNamespace(doc=doc, doc_path=doc_path, asset_store=local,
                          lock=threading.RLock(), generation=1)
    repo = _Repo(ctx)
    actor_ctx = mb.McpActorCtx(actor=Actor(user_id="__mcp__", source="ai"),
                              enforce_acl=False, project_hint="p1")

    with pytest.raises(ZeError) as exc:
        mb._call_tool(repo, actor_ctx, "ze_read_asset",
                      {"sha256": ref.sha256, "mode": "meta"})
    assert exc.value.code == "acl_denied"

    # canonical Document 引用该 sha 后闸门放行(durable 重读生效)。
    doc.add(Entity(type="pic", data={"image": {"sha256": ref.sha256, "mime": "image/png",
                                               "size": ref.size, "ext": ".png"}}, id="e1"))
    doc.save(doc_path)
    body = _payload(mb._call_tool(repo, actor_ctx, "ze_read_asset",
                                  {"sha256": ref.sha256, "mode": "meta"}))
    assert body["sha256"] == ref.sha256


def test_read_asset_is_read_meta_tool_no_edit():
    assert "ze_read_asset" in mb._READ_META_TOOLS
