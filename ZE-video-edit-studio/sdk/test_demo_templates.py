"""demo 模板系统测试(一等文件系统能力:多模板 catalog + 隐藏可重置沙盒 + $asset 真资产)。

护:
  - DocumentRegistry 多模板注册 + set_demo_project 向后兼容(转 id="default")。
  - Repository.open_demo_template:确定性沙盒、删旧建新=重置、demo_session config。
  - server:/api/demo-templates 列举、沙盒从 /api/projects 过滤、另存为剥 demo 元数据变普通项目。
  - demo_scenario.instantiate_scenario:$asset → 真 asset_ref、缺资产报错、路径穿越护栏。
"""

from __future__ import annotations

import json
import hashlib

import pytest


def _repo(tmp_path, monkeypatch):
    monkeypatch.setenv("ZE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ZE_USERS_PATH", str(tmp_path / "users.json"))
    from sdk.bootstrap import bootstrap
    return bootstrap()


def _first_template_id(repo):
    """mod 无关:取 mod 声明的第一个 demo 模板 id。

    这些 Repository/endpoint 测试随 core 层 Skill.Sync 下发到下游工具,各工具的 mod 声明的
    模板各不相同(build-v3=default / finalizer=brass-key…)。故断言**框架不变量**(沙盒语义/
    过滤/隔离),不写死某个 mod 的模板 id 或 seed 内容 —— 否则下游必失败(已知同步摩擦)。
    """
    tpls = repo.demo_templates()
    assert tpls, "mod 须声明至少一个 demo 模板"
    return tpls[0]["id"]


# ============================================================
# DocumentRegistry 多模板注册
# ============================================================

def test_set_demo_project_registers_default_template():
    from sdk import DocumentRegistry
    reg = DocumentRegistry()
    reg.declare_entity_type(name="item", schema={"type": "object", "properties": {}})
    reg.set_collaboration_mode("with_save")
    reg.set_demo_project(name="入门", bootstrap=lambda doc: None)
    ids = [t["id"] for t in reg.demo_templates]
    assert ids == ["default"]
    assert reg.demo is not None             # 老 .demo 访问器仍可用(create_project seed 路径)


def test_register_demo_template_multi_and_validation():
    from sdk import DocumentRegistry, ZeError
    reg = DocumentRegistry()
    reg.register_demo_template(id="alpha", name="A", bootstrap=lambda d: None)
    reg.register_demo_template(id="beta", name="B", scenario_path="/abs/scenarios/beta/manifest.json")
    assert [t["id"] for t in reg.demo_templates] == ["alpha", "beta"]
    # 重复 id
    with pytest.raises(ZeError):
        reg.register_demo_template(id="alpha", name="dup", bootstrap=lambda d: None)
    # 非法 slug(含 /)
    with pytest.raises(ZeError):
        reg.register_demo_template(id="bad/id", name="x", bootstrap=lambda d: None)
    # 既无 bootstrap 也无 scenario_path
    with pytest.raises(ZeError):
        reg.register_demo_template(id="empty", name="x")


def test_register_demo_catalog_resolves_scenarios(tmp_path):
    from sdk import DocumentRegistry
    demo = tmp_path / "demo"
    (demo / "scenarios" / "a").mkdir(parents=True)
    (demo / "catalog.json").write_text(json.dumps({"templates": [
        {"id": "a", "name": "场景A", "description": "d", "category": "c",
         "business_scene": "bs", "scenario": "scenarios/a/manifest.json"},
    ]}), encoding="utf-8")
    reg = DocumentRegistry()
    reg.register_demo_catalog(demo / "catalog.json")
    tpls = reg.demo_templates
    assert [t["id"] for t in tpls] == ["a"]
    # scenario 解析为绝对路径,落在 catalog 目录内
    assert tpls[0]["scenario_path"] == str((demo / "scenarios" / "a" / "manifest.json").resolve())
    assert tpls[0]["business_scene"] == "bs"


def test_register_demo_catalog_blocks_path_traversal(tmp_path):
    from sdk import DocumentRegistry, ZeError
    demo = tmp_path / "demo"
    demo.mkdir(parents=True)
    (demo / "catalog.json").write_text(json.dumps({"templates": [
        {"id": "evil", "name": "x", "scenario": "../../../etc/passwd"},
    ]}), encoding="utf-8")
    reg = DocumentRegistry()
    with pytest.raises(ZeError) as ei:
        reg.register_demo_catalog(demo / "catalog.json")
    assert ei.value.code == "validation_failed"


def test_demo_templates_meta_drops_bootstrap(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    metas = repo.demo_templates()
    assert metas                              # mod 声明了至少一个模板
    for t in metas:
        assert "bootstrap" not in t          # API 元数据不含函数引用
        assert set(t) == {"id", "name", "description", "category", "business_scene"}


def test_demo_template_content_version_stable_and_unknown(tmp_path, monkeypatch):
    """内容版本:同模板稳定(幂等不抖)、未知模板回退 "bootstrap"。
    scenario 模板→16 位 hex;bootstrap 模板→"bootstrap"。给 demo.open 幂等 scope 防冻结用。"""
    repo = _repo(tmp_path, monkeypatch)
    tid = _first_template_id(repo)
    v1 = repo.demo_template_content_version(tid)
    v2 = repo.demo_template_content_version(tid)
    assert v1 and v1 == v2                                  # 稳定:内容不变 → 版本不变(幂等)
    assert v1 == "bootstrap" or (len(v1) == 16 and all(c in "0123456789abcdef" for c in v1))
    assert repo.demo_template_content_version("__nope__") == "bootstrap"  # 未知模板不报错、回退


# ============================================================
# Repository.open_demo_template — 沙盒 + 重置
# ============================================================

def test_open_demo_template_builds_sandbox(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    tid = _first_template_id(repo)
    pid = repo.open_demo_template(actor_user_id="alice", template_id=tid)
    assert pid.startswith("__demo__")
    ctx = repo.get_or_load(pid)
    cfg = ctx.doc.config
    assert cfg.get("project_kind") == "demo_session"   # 沙盒语义(框架不变量,mod 无关)
    assert cfg.get("demo_template_id") == tid
    assert "demo_reset_at" in cfg
    assert ctx.doc.acl.get("owner") == "alice"          # owner = 操作者(隔离)


def test_open_demo_template_is_deterministic_and_resets(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    tid = _first_template_id(repo)
    pid1 = repo.open_demo_template(actor_user_id="alice", template_id=tid)
    # 往沙盒目录塞个脏标记文件,模拟"被编辑过"
    marker = tmp_path / "data" / "projects" / pid1 / "marker.txt"
    marker.write_text("dirty")
    pid2 = repo.open_demo_template(actor_user_id="alice", template_id=tid)
    assert pid2 == pid1                       # 同用户同模板 → 同一确定性沙盒
    assert not marker.exists()                # 删旧建新 = 重置(脏标记没了)


def test_open_demo_template_per_user_isolated(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    tid = _first_template_id(repo)
    a = repo.open_demo_template(actor_user_id="alice", template_id=tid)
    b = repo.open_demo_template(actor_user_id="bob", template_id=tid)
    assert a != b                             # 不同用户各自隐藏沙盒


def test_open_demo_template_unknown_raises(tmp_path, monkeypatch):
    from sdk import ZeError
    repo = _repo(tmp_path, monkeypatch)
    with pytest.raises(ZeError) as ei:
        repo.open_demo_template(actor_user_id="alice", template_id="does-not-exist")
    assert ei.value.code == "not_found"


def test_open_demo_template_failed_reset_preserves_old_sandbox(tmp_path, monkeypatch):
    """destroy-before-validate 根治:重置时若"建新沙盒"阶段失败,旧沙盒(含用户上次编辑)原封不动,
    不留半建 `.new` 残留 —— 验证先建临时目录、成功才换上的原子语义。"""
    from sdk import bootstrap as _bs
    repo = _repo(tmp_path, monkeypatch)
    tid = _first_template_id(repo)
    pid = repo.open_demo_template(actor_user_id="alice", template_id=tid)
    proj = tmp_path / "data" / "projects" / pid
    marker = proj / "marker.txt"
    marker.write_text("keep me")                       # 模拟用户上次的 demo 编辑产物
    old_doc_text = (proj / "document.json").read_text()

    # 让下一次重置在"建新沙盒"阶段失败(staging 存盘抛错)
    def boom_save(self, *a, **k):
        raise RuntimeError("simulated build failure during reset")
    monkeypatch.setattr(_bs.Document, "save", boom_save)

    with pytest.raises(RuntimeError):
        repo.open_demo_template(actor_user_id="alice", template_id=tid)

    # 旧沙盒原封不动 + 无 `.new` 残留
    assert proj.exists()
    assert marker.read_text() == "keep me"
    assert (proj / "document.json").read_text() == old_doc_text
    assert not (proj.parent / (proj.name + ".new")).exists()


def test_duplicate_demo_sandbox_strips_demo_meta(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    sandbox = repo.open_demo_template(actor_user_id="alice", template_id=_first_template_id(repo))
    copy = repo.duplicate_project(sandbox, owner="alice", name="我的留存")
    raw = json.loads((tmp_path / "data" / "projects" / copy / "document.json").read_text())
    cfg = raw.get("config") or {}
    assert "project_kind" not in cfg          # 另存为后变普通项目
    assert "demo_template_id" not in cfg
    assert raw["name"] == "我的留存"


# ============================================================
# server 端点:列举 / 过滤 / 另存为
# ============================================================

def _load_app(tmp_path, monkeypatch):
    monkeypatch.setenv("ZE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ZE_USERS_PATH", str(tmp_path / "users.json"))
    import importlib
    import server as server_mod
    importlib.reload(server_mod)
    return server_mod.app


def _run_scenario(app, steps):
    """单 asyncio 事件循环顺序打 ASGI app(排 05-concurrency 的 queue 遮蔽雷,见 test_bootstrap_projects)。"""
    import asyncio
    import sys

    shadow_paths = [p for p in sys.path if p.replace("\\", "/").endswith("05-concurrency")]
    saved_queue = sys.modules.get("queue")
    leaf_queue = saved_queue is not None and "05-concurrency" in str(
        getattr(saved_queue, "__file__", ""))
    try:
        for p in shadow_paths:
            sys.path.remove(p)
        if leaf_queue:
            sys.modules.pop("queue", None)
        import httpx

        async def _main():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                return await steps(client)

        return asyncio.run(_main())
    finally:
        for p in shadow_paths:
            if p not in sys.path:
                sys.path.insert(0, p)
        if leaf_queue and saved_queue is not None:
            sys.modules["queue"] = saved_queue


def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def _demo_intent(tag: str) -> str:
    return hashlib.sha256(tag.encode("utf-8")).hexdigest()[:32]


def _demo_request(tag: str, token: str = "", *, generation: int = 0) -> tuple[dict, dict]:
    intent = _demo_intent(tag)
    headers = {"Idempotency-Key": f"test:demo:{tag}:{intent}"}
    if token:
        headers.update(_auth(token))
    return {"intent_id": intent, "expected_generation": generation}, headers


def test_endpoint_demo_list_open_and_hidden_from_projects(tmp_path, monkeypatch):
    app = _load_app(tmp_path, monkeypatch)

    async def steps(client):
        r = await client.post("/api/auth/register",
                              json={"username": "alice", "password": "alice-pass-1"})
        tok = r.json()["token"]

        # 列示例模板(mod 无关:取第一个)
        r = await client.get("/api/demo-templates", headers=_auth(tok))
        assert r.status_code == 200
        tpls = r.json()["templates"]
        assert tpls
        tid = tpls[0]["id"]
        assert tpls[0]["sandbox_generation"] == 0
        assert tpls[0]["sandbox_project_id"].startswith("__demo__")

        # 打开示例 → 拿到隐藏沙盒
        body, headers = _demo_request("logged-open", tok)
        r = await client.post(f"/api/demo-templates/{tid}/open", json=body, headers=headers)
        assert r.status_code == 200
        assert r.json()["kind"] == "replace"
        assert r.json()["generation"] == r.json()["project_generation"] == 1
        sandbox = r.json()["project_id"]
        assert sandbox.startswith("__demo__")
        listed_after_open = await client.get("/api/demo-templates", headers=_auth(tok))
        current_tpl = next(t for t in listed_after_open.json()["templates"] if t["id"] == tid)
        assert current_tpl["sandbox_project_id"] == sandbox
        assert current_tpl["sandbox_generation"] == 1

        # 沙盒不出现在普通项目列表
        r = await client.get("/api/projects", headers=_auth(tok))
        assert all(p["id"] != sandbox for p in r.json()["projects"])

        # 未知模板 → 404
        body, headers = _demo_request("logged-missing", tok)
        r = await client.post("/api/demo-templates/nope/open", json=body, headers=headers)
        assert r.status_code == 404

        # 文档 meta 暴露 config(前端据此识别 demo 态)
        r = await client.get(f"/api/document?project_id={sandbox}", headers=_auth(tok))
        assert r.status_code == 200
        assert r.json()["meta"]["config"].get("project_kind") == "demo_session"
        assert isinstance(r.json()["event_cursor"], int)
        assert len(r.json()["event_epoch"]) == 32
        first_generation = r.json()["meta"]["generation"]

        # 同一 sandbox id 原地 reset 必须推进 topology generation；前端 SSE
        # 用这个事实识别“ID 没变但 Context 已换代”。
        reset_body, reset_headers = _demo_request("logged-reset", tok, generation=first_generation)
        reset = await client.post(
            f"/api/demo-templates/{tid}/open",
            json=reset_body,
            headers=reset_headers,
        )
        assert reset.status_code == 200
        assert reset.json()["project_id"] == sandbox
        assert reset.json()["kind"] == "replace"
        assert reset.json()["generation"] == first_generation + 1
        listed_after_reset = await client.get("/api/demo-templates", headers=_auth(tok))
        current_tpl = next(t for t in listed_after_reset.json()["templates"] if t["id"] == tid)
        assert current_tpl["sandbox_generation"] == 2
        current = await client.get(f"/api/document?project_id={sandbox}", headers=_auth(tok))
        assert current.json()["meta"]["generation"] == first_generation + 1
        assert current.json()["meta"]["config"]["demo_reset_intent_id"] == reset_body["intent_id"]

    _run_scenario(app, steps)


def test_endpoint_demo_guest_no_login_can_operate(tmp_path, monkeypatch):
    """demo 沙盒「人人可操作、只是不保存」:匿名(无 token)能列举/打开/读/写 demo,但碰真项目仍 401。

    框架不变量(mod 无关):不写死命令 id —— 用 /api/undo(mod 无关写通道)证「写权限闸对 demo 放行」。
    """
    app = _load_app(tmp_path, monkeypatch)

    async def steps(client):
        # ① 匿名列示例(此前 401)
        r = await client.get("/api/demo-templates")
        assert r.status_code == 200, r.text
        tpls = r.json()["templates"]
        assert tpls
        tid = tpls[0]["id"]

        # ② 匿名打开 demo → 落到保留 guest 沙盒(此前 401)
        body, headers = _demo_request("guest-open")
        r = await client.post(f"/api/demo-templates/{tid}/open", json=body, headers=headers)
        assert r.status_code == 200, r.text
        sandbox = r.json()["project_id"]
        assert sandbox.startswith("__demo__")

        # ③ 匿名读 demo 文档 / 命令(_require_member 对 demo 放行)
        r = await client.get(f"/api/document?project_id={sandbox}")
        assert r.status_code == 200, r.text
        assert r.json()["meta"]["config"].get("project_kind") == "demo_session"
        generation = r.json()["meta"]["generation"]
        r = await client.get(f"/api/commands?project_id={sandbox}")
        assert r.status_code == 200, r.text

        # ④ 匿名走写通道(undo)→ 200 而非 401(_require_edit_ctx 对 demo 放行;空轨返回空 ops)
        r = await client.post(
            f"/api/undo?project_id={sandbox}", json={"expected_generation": generation},
        )
        assert r.status_code == 200, r.text
        assert "ops" in r.json()
        assert r.json()["project_generation"] == generation
        r = await client.post(
            f"/api/redo?project_id={sandbox}", json={"expected_generation": generation},
        )
        assert r.status_code == 200, r.text
        assert r.json()["project_generation"] == generation

        # ⑤ demo 沙盒禁改共享:匿名派 document.set_acl → acl_denied,绝不静默放行
        r = await client.post(
            f"/api/command/document.set_acl?project_id={sandbox}",
            json={
                "owner": "__demo_guest__", "collaborators": [],
                "expected_generation": generation,
            })
        assert r.status_code != 200, r.text

        # ⑥ 作用域:匿名碰真(非 demo)项目仍 401(guest 只在 demo doc 上有效)。
        # 真项目经 repo 直建(绕开「新用户默认只读」的权限策略,本测只验访问边界)。
        import server as server_mod
        real_pid = server_mod._repo.create_project(name="真项目", owner="someone", seed=False)
        r = await client.get(f"/api/document?project_id={real_pid}")        # 无 token
        assert r.status_code in (401, 403), r.text

    _run_scenario(app, steps)


def test_generation_fence_precedes_idempotent_replay_after_demo_reset(
    tmp_path, monkeypatch,
):
    app = _load_app(tmp_path, monkeypatch)

    async def steps(client):
        tid = (await client.get("/api/demo-templates")).json()["templates"][0]["id"]
        body, headers = _demo_request("generation-fence-open")
        opened = await client.post(
            f"/api/demo-templates/{tid}/open", json=body, headers=headers,
        )
        assert opened.status_code == 200
        sandbox = opened.json()["project_id"]
        assert opened.json()["generation"] == 1

        replay_headers = {"Idempotency-Key": "demo-generation-fence-undo"}
        first = await client.post(
            f"/api/undo?project_id={sandbox}",
            json={"expected_generation": 1}, headers=replay_headers,
        )
        assert first.status_code == 200

        reset_body, reset_headers = _demo_request(
            "generation-fence-reset", generation=1,
        )
        reset = await client.post(
            f"/api/demo-templates/{tid}/open",
            json=reset_body, headers=reset_headers,
        )
        assert reset.status_code == 200
        assert reset.json()["generation"] == 2

        # Same key/body has a proven G1 success, but it must not be returned as
        # if it belonged to the new G2 context.
        stale = await client.post(
            f"/api/undo?project_id={sandbox}",
            json={"expected_generation": 1}, headers=replay_headers,
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "project_generation_stale"

        # Changing only expected_generation changes the idempotency request
        # hash; the old key cannot silently become a fresh G2 action.
        reused = await client.post(
            f"/api/undo?project_id={sandbox}",
            json={"expected_generation": 2}, headers=replay_headers,
        )
        assert reused.status_code == 409
        assert reused.json()["error"]["code"] == "idempotency_key_reused"

    _run_scenario(app, steps)


def test_demo_reset_stale_retry_reconciles_committed_outcome_when_ledger_is_lost(
    tmp_path, monkeypatch,
):
    app = _load_app(tmp_path, monkeypatch)

    async def steps(client):
        template_id = (await client.get("/api/demo-templates")).json()["templates"][0]["id"]
        first_body, first_headers = _demo_request("lost-ledger-first")
        first = await client.post(
            f"/api/demo-templates/{template_id}/open",
            json=first_body, headers=first_headers,
        )
        assert first.status_code == 200

        reset_body, reset_headers = _demo_request(
            "lost-ledger-reset", generation=1,
        )
        reset = await client.post(
            f"/api/demo-templates/{template_id}/open",
            json=reset_body, headers=reset_headers,
        )
        assert reset.status_code == 200
        assert reset.json()["generation"] == 2

        import server as server_mod
        from sdk import IdempotencyStore
        server_mod._repo.idempotency_store = IdempotencyStore(
            tmp_path / "lost-demo-ledger.json",
        )
        stale_retry = await client.post(
            f"/api/demo-templates/{template_id}/open",
            json=reset_body, headers=reset_headers,
        )
        assert stale_retry.status_code == 409
        error = stale_retry.json()["error"]
        assert error["mutation_outcome"] == "committed_proof_via_reconcile"
        assert error["context"]["proof_status"] == "committed"
        assert error["context"]["proof_generation"] == 2

    _run_scenario(app, steps)


def test_demo_list_returns_stable_non_reversible_guest_scope(tmp_path, monkeypatch):
    app = _load_app(tmp_path, monkeypatch)

    async def steps(client):
        first = await client.get("/api/demo-templates")
        assert first.status_code == 200
        guest_id = client.cookies.get("ze_guest")
        scope = first.json()["guest_scope"]
        assert guest_id and scope
        assert len(scope) == 64
        assert all(char in "0123456789abcdef" for char in scope)
        assert guest_id not in first.text

        repeated = await client.get("/api/demo-templates")
        assert repeated.json()["guest_scope"] == scope
        assert client.cookies.get("ze_guest") == guest_id

        client.cookies.clear()
        rotated = await client.get("/api/demo-templates")
        assert rotated.json()["guest_scope"] != scope
        assert client.cookies.get("ze_guest") != guest_id

    _run_scenario(app, steps)


def test_demo_mutation_establishes_guest_before_write_and_reconciles(tmp_path, monkeypatch):
    app = _load_app(tmp_path, monkeypatch)

    async def steps(client):
        import server as server_mod

        template_id = server_mod._repo.demo_templates()[0]["id"]
        body, headers = _demo_request("guest-direct-post")

        # A direct first POST only establishes the HttpOnly identity.  No
        # sandbox may be created in the same response.
        first = await client.post(
            f"/api/demo-templates/{template_id}/open", json=body, headers=headers,
        )
        assert first.status_code == 409
        assert first.json()["error"]["code"] == "guest_session_required"
        assert client.cookies.get("ze_guest")
        assert server_mod._repo.list_project_ids() == []

        second = await client.post(
            f"/api/demo-templates/{template_id}/open", json=body, headers=headers,
        )
        assert second.status_code == 200
        sandbox = second.json()["project_id"]
        proof = await client.get(
            f"/api/demo-templates/{template_id}/intents/{body['intent_id']}",
        )
        assert proof.status_code == 200
        assert proof.json()["status"] == "committed"
        assert proof.json()["project_id"] == sandbox
        assert proof.json()["proof_revision"] == 2
        assert proof.json()["principal_scoped"] is True
        assert proof.json()["intent_reserved"] is True
        assert "principal_bound" not in proof.json()

        unsent_intent = _demo_intent("guest-unsent-reset")
        untouched = await client.get(
            f"/api/demo-templates/{template_id}/intents/{unsent_intent}?kind=replace",
        )
        assert untouched.status_code == 200
        assert untouched.json()["status"] == "not_committed"
        assert untouched.json()["proof_revision"] == 2
        assert untouched.json()["principal_scoped"] is True
        assert untouched.json()["intent_reserved"] is False

    _run_scenario(app, steps)


def test_endpoint_demo_guest_per_browser_isolated(tmp_path, monkeypatch):
    """并发匿名访客互不干扰:每浏览器一个随机 ze_guest cookie → 各自不可枚举的沙盒。

    同一 cookie 复用 → 同一沙盒(重开即重置);清掉 cookie(模拟换浏览器)→ 全新沙盒。
    """
    app = _load_app(tmp_path, monkeypatch)

    async def steps(client):
        tid = (await client.get("/api/demo-templates")).json()["templates"][0]["id"]

        # 浏览器 A:首开 → 落 ze_guest cookie + 沙盒 A
        body, headers = _demo_request("browser-a-open")
        r = await client.post(f"/api/demo-templates/{tid}/open", json=body, headers=headers)
        assert r.status_code == 200, r.text
        sandbox_a = r.json()["project_id"]
        guest_a = client.cookies.get("ze_guest")
        assert "ze_guest" in r.cookies or client.cookies.get("ze_guest"), "应下发 ze_guest cookie"

        # 同浏览器再开 → 同一确定性沙盒(cookie 不变)
        body, headers = _demo_request("browser-a-reset", generation=1)
        r = await client.post(f"/api/demo-templates/{tid}/open", json=body, headers=headers)
        assert r.json()["project_id"] == sandbox_a

        # 换浏览器(清 cookie)→ 全新随机沙盒,与 A 不同 → 互不可见/不可重置
        client.cookies.clear()
        # 先经只读列表建立稳定 guest；mutation 不负责同时签发身份。
        assert (await client.get("/api/demo-templates")).status_code == 200
        body, headers = _demo_request("browser-b-open")
        r = await client.post(f"/api/demo-templates/{tid}/open", json=body, headers=headers)
        sandbox_b = r.json()["project_id"]
        assert sandbox_b != sandbox_a, "不同访客须落不同沙盒"

        # Browser B knows A's opaque project id but not A's HttpOnly principal.
        # Both reads and writes must fail at the server ACL, not rely on secrecy.
        denied_read = await client.get(f"/api/document?project_id={sandbox_a}")
        assert denied_read.status_code == 403
        denied_write = await client.post(
            f"/api/undo?project_id={sandbox_a}",
            json={"expected_generation": 2},
        )
        assert denied_write.status_code == 403

        client.cookies.clear()
        client.cookies.set("ze_guest", guest_a)
        allowed_again = await client.get(f"/api/document?project_id={sandbox_a}")
        assert allowed_again.status_code == 200

    _run_scenario(app, steps)


def test_demo_intent_is_durably_bound_to_one_guest_principal(tmp_path, monkeypatch):
    app = _load_app(tmp_path, monkeypatch)

    async def steps(client):
        template_id = (await client.get("/api/demo-templates")).json()["templates"][0]["id"]
        shared_intent = _demo_intent("cross-guest-intent")
        body = {"intent_id": shared_intent, "expected_generation": 0}
        opened = await client.post(
            f"/api/demo-templates/{template_id}/open",
            json=body,
            headers={"Idempotency-Key": "cross-guest-a"},
        )
        assert opened.status_code == 200

        client.cookies.clear()
        assert (await client.get("/api/demo-templates")).status_code == 200
        foreign_open = await client.post(
            f"/api/demo-templates/{template_id}/open",
            json=body,
            headers={"Idempotency-Key": "cross-guest-b"},
        )
        assert foreign_open.status_code == 403
        assert foreign_open.json()["error"]["code"] == "acl_denied"
        foreign_status = await client.get(
            f"/api/demo-templates/{template_id}/intents/{shared_intent}",
        )
        assert foreign_status.status_code == 403

    _run_scenario(app, steps)


# ============================================================
# demo_scenario.instantiate_scenario — $asset 真资产
# ============================================================

def _scenario_doc():
    from sdk import DocumentRegistry
    reg = DocumentRegistry()
    reg.declare_entity_type(
        name="shot",
        schema={"type": "object", "properties": {
            "image": {"type": "object", "label": "图"},
            "caption": {"type": "string", "label": "说明"},
        }},
    )
    reg.set_collaboration_mode("with_save")
    return reg.build_document(schema_version="1.0")


def test_instantiate_scenario_resolves_real_asset(tmp_path):
    from sdk import AssetStore, instantiate_scenario
    scen = tmp_path / "scenarios" / "demo-a"
    (scen / "assets").mkdir(parents=True)
    png = scen / "assets" / "home.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n-fake-but-real-bytes")
    manifest = {"entities": [
        {"type": "shot", "id": "s1", "name": "首页",
         "data": {"caption": "改版前", "image": {"$asset": "assets/home.png"}}},
    ]}
    doc = _scenario_doc()
    store = AssetStore(tmp_path / "assets-store")
    instantiate_scenario(doc, manifest, scenario_dir=scen, asset_store=store)

    ent = doc.get("shot", "s1")
    ref = ent.data["image"]
    assert isinstance(ref, dict) and "sha256" in ref      # 解析成真 asset_ref
    from sdk import AssetRef
    assert store.has(AssetRef.from_dict(ref))             # blob 真进了库
    assert ent.data["caption"] == "改版前"                  # 非资产字段原样


def test_instantiate_scenario_missing_asset_raises(tmp_path):
    from sdk import AssetStore, ZeError, instantiate_scenario
    scen = tmp_path / "scenarios" / "demo-b"
    scen.mkdir(parents=True)
    manifest = {"entities": [
        {"type": "shot", "id": "s1", "data": {"image": {"$asset": "assets/missing.png"}}},
    ]}
    doc = _scenario_doc()
    store = AssetStore(tmp_path / "store")
    with pytest.raises(ZeError) as ei:
        instantiate_scenario(doc, manifest, scenario_dir=scen, asset_store=store)
    assert ei.value.code == "asset_missing"


def test_instantiate_scenario_path_traversal_raises(tmp_path):
    from sdk import AssetStore, ZeError, instantiate_scenario
    scen = tmp_path / "scenarios" / "demo-c"
    scen.mkdir(parents=True)
    (tmp_path / "secret.png").write_bytes(b"x")           # scenario 目录外
    manifest = {"entities": [
        {"type": "shot", "id": "s1", "data": {"image": {"$asset": "../secret.png"}}},
    ]}
    doc = _scenario_doc()
    store = AssetStore(tmp_path / "store")
    with pytest.raises(ZeError) as ei:
        instantiate_scenario(doc, manifest, scenario_dir=scen, asset_store=store)
    assert ei.value.code == "validation_failed"


def test_instantiate_scenario_absolute_path_rejected(tmp_path):
    from sdk import AssetStore, ZeError, instantiate_scenario
    scen = tmp_path / "scenarios" / "demo-d"
    scen.mkdir(parents=True)
    manifest = {"entities": [
        {"type": "shot", "id": "s1", "data": {"image": {"$asset": "/etc/hosts"}}},
    ]}
    doc = _scenario_doc()
    store = AssetStore(tmp_path / "store")
    with pytest.raises(ZeError) as ei:
        instantiate_scenario(doc, manifest, scenario_dir=scen, asset_store=store)
    assert ei.value.code == "validation_failed"
