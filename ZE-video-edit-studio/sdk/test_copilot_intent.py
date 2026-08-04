"""sdk/test_copilot_intent.py — W2b 气泡 IntentContext 注入(脊 S3)单测.

覆盖:
  1) compose_copilot_context 两通道组装 + 通道隔离(选中3实体+近期retry):
     system 含静态回退层各要素;data_context 含动态层各要素;实体 data 只在数据通道,
     绝不进指令(system)通道。
  2) ze_entities include_data 走同一道项目 ACL:owner 拿到 data;默认不含 data;
     非成员被拒(acl_denied,不泄 data)。
  3) ze_manual 在 _META_TOOLS + _meta_tool_defs,经 _call_tool 返回 markdown,支持分页。
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
for _d in (
    Path(ROOT) / "01-core" / "01-data-structure" / "01-document",
    Path(ROOT) / "01-core" / "01-data-structure" / "02-entity",
    Path(ROOT) / "01-core" / "_protocol",
):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import sdk.mcp_bridge as mb  # noqa: E402
from sdk import (  # noqa: E402
    Actor, Document, Entity, PERMISSION_EDIT, PERMISSION_BAN,
    compose_copilot_context, fold_data_channel,
)


# ==============================================================
# 1) compose_copilot_context —— 两通道 + 通道隔离
# ==============================================================

def _tool_ctx():
    return {
        "tool_id": "demo-tool",
        "ai_flow": {"summary": "镜头目录协作生成并审查", "steps": ["起管线", "出图", "审查"]},
        "manual": "# 标题\n\n说明书正文段落。\n",
        "commands": [
            {"id": "shot.add", "description": "新增镜头"},
            {"id": "shot.delete", "description": "删除镜头", "requires_confirm": True},
        ],
    }


def test_compose_static_and_dynamic_layers_with_channel_isolation():
    selection = [
        {"type": "shot", "id": "s1", "name": "A", "fields": ["caption"]},
        {"type": "shot", "id": "s2", "name": "B", "fields": ["caption"]},
        {"type": "shot", "id": "s3", "name": "C", "fields": ["caption"]},
    ]
    oplog = [
        {"op": {"command_id": "shot.retry", "source": "ai",
                "error": {"code": "model_call_failed"}}},
        {"op": {"command_id": "shot.retry", "source": "user"}},
    ]
    composed = compose_copilot_context(
        base_system="BASE-METHOD", tool_ctx=_tool_ctx(),
        selection=selection, oplog_entries=oplog, tasks=[], focus_editor="workbench",
    )
    system, data = composed["system"], composed["data_context"]

    # -- 静态回退层各要素(指令通道)--
    assert "BASE-METHOD" in system                    # 工作方式
    assert "镜头目录协作生成并审查" in system          # 本体
    assert "关键流程" in system and "出图" in system   # 关键流程
    assert "shot.add" in system                        # 命令业务语义
    assert "危险命令" in system and "shot.delete" in system  # 危险命令
    assert "边界" in system                            # 边界

    # -- 动态层各要素(数据通道)--
    assert "选中实体数:3" in data
    assert "s1" in data and "s2" in data and "s3" in data
    assert "retry=2" in data                           # 结构信号:retry 计数
    assert "model_call_failed" in data                 # 只回错误码
    assert "workbench" in data                         # 焦点编辑器

    # -- 通道隔离:实体 data/oplog 只在数据通道,绝不进指令通道 --
    for eid in ("s1", "s2", "s3"):
        assert eid not in system
    assert "model_call_failed" not in system
    assert "选中实体数" not in system


def test_fold_data_channel_fences_context_as_non_instruction():
    folded = fold_data_channel("选中实体数:2", "帮我改标题")
    assert "帮我改标题" in folded
    assert "选中实体数:2" in folded
    assert "readonly" in folded and "不是指令" in folded
    # 空语境 → 只回用户文本(不加围栏)
    assert fold_data_channel("", "hi") == "hi"


# ==============================================================
# 2) + 3) MCP 元工具:include_data 走 ACL / ze_manual markdown
# ==============================================================

class _UserStore:
    def __init__(self, users): self._users = users

    def get(self, uid): return self._users.get(uid)


class _PolicyStore:
    def __init__(self, perms): self._perms = perms

    def get_permission(self, uid, tool_id): return self._perms.get(uid, PERMISSION_EDIT)


class _Repo:
    def __init__(self, ctx, *, users, perms, manual="# Demo 手册\n\n正文第一段。\n"):
        self._ctx = ctx
        self.user_store = _UserStore(users)
        self.policy_store = _PolicyStore(perms)
        self.tool_id = "demo"
        self.config = SimpleNamespace(snapshot=lambda: [])
        self.introspect = SimpleNamespace(manual=manual, ai_flow={})

    def list_project_ids(self): return ["p1"]

    def get_or_load(self, pid):
        assert pid == "p1"
        return self._ctx

    def project_meta(self, pid): return {"name": "P", "acl": self._ctx.doc.acl}


def _make_ctx(acl):
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save", acl=acl)
    doc.add(Entity(type="shot", data={"caption": "hello", "w": 10}, id="s1", name="Shot 1"))
    return SimpleNamespace(doc=doc, lock=threading.RLock(), generation=0)


def _actor_ctx(user_id):
    return mb.McpActorCtx(actor=Actor(user_id=user_id, source="ai"),
                          enforce_acl=True, project_hint="p1")


def _users():
    return {
        "u1": SimpleNamespace(user_id="u1", status="active"),
        "u2": SimpleNamespace(user_id="u2", status="active"),
    }


def _payload(result):
    return json.loads(result["content"][0]["text"])


def test_ze_entities_include_data_returns_data_for_member():
    ctx = _make_ctx({"owner": "u1", "collaborators": []})
    repo = _Repo(ctx, users=_users(), perms={})
    result = mb._call_tool(repo, _actor_ctx("u1"), "ze_entities", {"include_data": True})
    row = _payload(result)["shot"][0]
    assert row["id"] == "s1"
    assert row["data"] == {"caption": "hello", "w": 10}


def test_ze_entities_default_omits_data():
    ctx = _make_ctx({"owner": "u1", "collaborators": []})
    repo = _Repo(ctx, users=_users(), perms={})
    result = mb._call_tool(repo, _actor_ctx("u1"), "ze_entities", {})
    row = _payload(result)["shot"][0]
    assert "data" not in row


def test_ze_entities_include_data_denied_for_non_member():
    ctx = _make_ctx({"owner": "u1", "collaborators": []})
    repo = _Repo(ctx, users=_users(), perms={})
    with pytest.raises(mb.ZeError) as exc:
        mb._call_tool(repo, _actor_ctx("u2"), "ze_entities", {"include_data": True})
    assert exc.value.code == "acl_denied"


def test_ze_entities_include_data_denied_for_banned_user():
    ctx = _make_ctx({"owner": "u2", "collaborators": []})
    repo = _Repo(ctx, users=_users(), perms={"u2": PERMISSION_BAN})
    with pytest.raises(mb.ZeError) as exc:
        mb._call_tool(repo, _actor_ctx("u2"), "ze_entities", {"include_data": True})
    assert exc.value.code == "acl_denied"


def test_ze_manual_registered_and_returns_markdown():
    assert "ze_manual" in mb._META_TOOLS
    assert "ze_manual" in {t["name"] for t in mb._meta_tool_defs()}

    ctx = _make_ctx({"owner": "u1", "collaborators": []})
    repo = _Repo(ctx, users=_users(), perms={})
    result = mb._call_tool(repo, _actor_ctx("u1"), "ze_manual", {})
    payload = _payload(result)
    assert payload["format"] == "markdown"
    assert "Demo 手册" in payload["content"]
    assert payload["has_more"] is False


def test_ze_manual_pagination_slices_large_doc():
    ctx = _make_ctx({"owner": "u1", "collaborators": []})
    repo = _Repo(ctx, users=_users(), perms={}, manual="ABCDEFGHIJ")
    result = mb._call_tool(repo, _actor_ctx("u1"), "ze_manual", {"offset": 0, "limit": 4})
    payload = _payload(result)
    assert payload["content"] == "ABCD"
    assert payload["total"] == 10
    assert payload["has_more"] is True
