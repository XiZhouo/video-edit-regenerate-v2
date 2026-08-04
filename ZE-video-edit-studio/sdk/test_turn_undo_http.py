"""§7.4 P0-D 整轮撤销**执行**端点真 HTTP 集成:建实体→AI 改字段→POST undo→实体字段回退。

真 bootstrap app(含 turn_store)+ 真 CommitCoordinator dispatch,钉死「补偿正向命令确实把实体
改回本轮前值」与「被后续改动覆盖 → conflict 跳过不误撤」。"""

from __future__ import annotations

import asyncio
import importlib
import uuid


def _load_app(tmp_path, monkeypatch):
    monkeypatch.setenv("ZE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ZE_USERS_PATH", str(tmp_path / "users.json"))
    import server as server_mod
    importlib.reload(server_mod)
    return server_mod, server_mod.app


def _item(doc_json):
    items = doc_json["entities"].get("item", [])
    return items[0] if items else None


def test_ai_turn_undo_reverts_field_and_skips_conflict(tmp_path, monkeypatch):
    server_mod, app = _load_app(tmp_path, monkeypatch)
    import httpx
    from sdk.assistant_turn import EV_CHANGE_SET

    async def steps():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/api/auth/register",
                             json={"username": "alice", "password": "alice-pass-1"})
            token = r.json()["token"]
            uid = r.json()["user"]["id"]
            auth = {"Authorization": f"Bearer {token}"}

            intent = uuid.uuid4().hex
            pid = "proj-" + uuid.uuid4().hex[:12]
            r = await c.post("/api/projects", headers={**auth, "Idempotency-Key": f"t:{intent}"},
                             json={"name": "undo-turn", "project_id": pid,
                                   "intent_id": intent, "expected_generation": 0})
            assert r.status_code == 200, r.text

            async def gen():
                d = (await c.get(f"/api/document?project_id={pid}", headers=auth)).json()
                return int(d["meta"]["generation"]), d

            g0, _ = await gen()
            r = await c.post(f"/api/command/entity.add?project_id={pid}", headers=auth,
                             json={"type": "item", "id": "it1", "name": "t1",
                                   "data": {"title": "orig"}, "expected_generation": g0})
            assert r.status_code == 200, r.text

            g1, d1 = await gen()
            ver = int(_item(d1)["version"])
            r = await c.post(f"/api/command/entity.update?project_id={pid}", headers=auth,
                             json={"type": "item", "id": "it1", "patches": {"title": "ai-改了"},
                                   "expected_version": ver, "expected_generation": g1})
            assert r.status_code == 200, r.text

            d2 = (await c.get(f"/api/document?project_id={pid}", headers=auth)).json()
            assert _item(d2)["data"]["title"] == "ai-改了"

            # 取那条 entity.update op 的 id(它就是本轮 AI 的可逆改动)
            ctx = server_mod._repo.get_or_load(pid)
            upd_ids = [e["op"]["id"] for e in ctx.op_log.entries()
                       if e["op"].get("command_id") == "entity.update"]
            assert upd_ids, "应有一条 entity.update op"
            op_id = upd_ids[-1]

            # 手工构一轮 Turn + change_set(模拟 AI 一轮把 title 改了;真流程由 /api/ai/chat 记)
            t = server_mod._repo.turn_store.create_turn(
                actor_user_id=uid, feature_id="assistant.chat", project_id=pid)
            server_mod._repo.turn_store.append_event(
                t.turn_id, EV_CHANGE_SET,
                {"operation_ids": [op_id], "reversible": [op_id], "irreversible": []})

            # 预览:should 显示 will_revert 含该 op(现值仍是 ai-改了,未被覆盖)
            pv = (await c.get(f"/api/ai/turn/{t.turn_id}/undo-preview", headers=auth)).json()
            assert [w["op_id"] for w in pv["will_revert"]] == [op_id]

            # 执行整轮撤销:title 应回退到 orig
            r = await c.post(f"/api/ai/turn/{t.turn_id}/undo", headers=auth, json={})
            assert r.status_code == 200, r.text
            assert r.json()["reverted"] == [op_id]
            d3 = (await c.get(f"/api/document?project_id={pid}", headers=auth)).json()
            assert _item(d3)["data"]["title"] == "orig", "补偿正向命令应把字段改回本轮前值"

            # 冲突分支:再改一轮 + 之后手动改该字段 → undo 该轮应 conflict 跳过(不覆盖后续改动)
            g3, d3b = await gen()
            ver3 = int(_item(d3b)["version"])
            r = await c.post(f"/api/command/entity.update?project_id={pid}", headers=auth,
                             json={"type": "item", "id": "it1", "patches": {"title": "二轮AI"},
                                   "expected_version": ver3, "expected_generation": g3})
            assert r.status_code == 200, r.text
            op2 = [e["op"]["id"] for e in server_mod._repo.get_or_load(pid).op_log.entries()
                   if e["op"].get("command_id") == "entity.update"][-1]
            t2 = server_mod._repo.turn_store.create_turn(
                actor_user_id=uid, feature_id="assistant.chat", project_id=pid)
            server_mod._repo.turn_store.append_event(
                t2.turn_id, EV_CHANGE_SET,
                {"operation_ids": [op2], "reversible": [op2], "irreversible": []})
            # 用户之后手动把 title 改成别的(覆盖了二轮 AI 的改动)
            g4, d4 = await gen()
            ver4 = int(_item(d4)["version"])
            await c.post(f"/api/command/entity.update?project_id={pid}", headers=auth,
                         json={"type": "item", "id": "it1", "patches": {"title": "人工最终"},
                               "expected_version": ver4, "expected_generation": g4})
            # 撤二轮:现值是「人工最终」≠ 二轮 after「二轮AI」→ conflict,不撤
            r = await c.post(f"/api/ai/turn/{t2.turn_id}/undo", headers=auth, json={})
            assert r.status_code == 200, r.text
            assert r.json()["reverted"] == []
            assert [conf["op_id"] for conf in r.json()["conflict"]] == [op2]
            d5 = (await c.get(f"/api/document?project_id={pid}", headers=auth)).json()
            assert _item(d5)["data"]["title"] == "人工最终", "冲突时不覆盖用户后续改动"

    asyncio.run(steps())
