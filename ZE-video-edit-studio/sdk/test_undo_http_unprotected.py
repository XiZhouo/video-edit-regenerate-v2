"""HTTP 级 undo/redo 回归守护 —— 普通(无 mutation policy)文档必须能撤销/重做。

背景(2026-07-20,core 3.60.1):`_history_operation_scope` 曾只在 protected
(声明过类型级 mutation policy)文档上把 history source ticket 推进到 REPLAYED,
而提交预检 `history_authority.prepare_store` 对所有文档一律要求 REPLAYED ——
于是未声明 policy 的普通文档(绝大多数下游工具)首次 undo/redo 100% 在
preflight 撞 `history_ticket_state_invalid` → HTTP 409。此前测试只覆盖
「空历史」与「protected」两端,唯独漏掉「unprotected + 真历史」,bug 得以带病
上线(verify_all 的 editor-undo 门禁亦为前端-only/skipped)。本测试钉死该组合。
"""

from __future__ import annotations

import asyncio
import importlib
import uuid


def _load_app(tmp_path, monkeypatch):
    monkeypatch.setenv("ZE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ZE_USERS_PATH", str(tmp_path / "users.json"))
    import server as server_mod

    importlib.reload(server_mod)
    return server_mod.app


def test_undo_redo_http_roundtrip_on_unprotected_document(tmp_path, monkeypatch):
    app = _load_app(tmp_path, monkeypatch)
    import httpx

    async def steps():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/api/auth/register",
                             json={"username": "alice", "password": "alice-pass-1"})
            token = r.json()["token"]
            auth = {"Authorization": f"Bearer {token}"}

            intent = uuid.uuid4().hex
            pid = "proj-" + uuid.uuid4().hex[:12]
            r = await c.post(
                "/api/projects",
                headers={**auth, "Idempotency-Key": f"t:{intent}"},
                json={"name": "undo-guard", "project_id": pid,
                      "intent_id": intent, "expected_generation": 0})
            assert r.status_code == 200, r.text

            r = await c.get(f"/api/document?project_id={pid}", headers=auth)
            generation = int(r.json()["meta"]["generation"])
            # 模板 mod 的 item 类型无 mutation policy —— 正是曾经必炸的组合。
            r = await c.post(
                f"/api/command/entity.add?project_id={pid}", headers=auth,
                json={"type": "item", "name": "t1",
                      "expected_generation": generation, "data": {"title": "t1"}})
            assert r.status_code == 200, r.text

            r = await c.post(
                f"/api/undo?project_id={pid}",
                headers={**auth, "Idempotency-Key": "undo-guard-1"},
                json={"expected_generation": generation})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["can_redo"] is True

            r = await c.get(f"/api/document?project_id={pid}", headers=auth)
            assert len(r.json()["entities"].get("item", [])) == 0

            r = await c.post(
                f"/api/redo?project_id={pid}",
                headers={**auth, "Idempotency-Key": "redo-guard-1"},
                json={"expected_generation": generation})
            assert r.status_code == 200, r.text

            r = await c.get(f"/api/document?project_id={pid}", headers=auth)
            items = r.json()["entities"].get("item", [])
            assert len(items) == 1 and items[0]["data"]["title"] == "t1"

    asyncio.run(steps())
