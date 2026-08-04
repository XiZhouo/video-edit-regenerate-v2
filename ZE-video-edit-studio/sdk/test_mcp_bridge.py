from __future__ import annotations

import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import sdk.mcp_bridge as mb  # noqa: E402


class FakeRequest:
    def __init__(self, *, host: str | None = "127.0.0.1", headers: dict | None = None,
                 query: dict | None = None, cookies: dict | None = None):
        self.client = SimpleNamespace(host=host) if host is not None else None
        self.headers = headers or {}
        self.query_params = query or {}
        self.cookies = cookies or {}


class FakeRepo:
    user_store = object()
    session_mgr = object()


def _clear_mcp_env(monkeypatch):
    for name in ("ZE_MCP_REQUIRE_TOKEN", "ZE_MCP_ALLOW_LOOPBACK_TRUST", "ZE_MCP_BIND",
                 "ZE_SERVER_MODE"):
        monkeypatch.delenv(name, raising=False)


def test_mcp_requires_token_by_default_even_on_loopback(monkeypatch):
    _clear_mcp_env(monkeypatch)
    assert mb.allow_loopback_trust() is False
    assert mb.require_token_mode() is True
    with pytest.raises(mb.ZeError) as exc:
        mb._resolve_http_actor_context(FakeRequest(host="127.0.0.1"), FakeRepo())
    assert exc.value.code == "auth_required"


def test_mcp_loopback_trust_requires_explicit_opt_out(monkeypatch):
    _clear_mcp_env(monkeypatch)
    monkeypatch.setenv("ZE_MCP_ALLOW_LOOPBACK_TRUST", "1")
    assert mb.allow_loopback_trust() is True
    assert mb.require_token_mode() is False
    ctx = mb._resolve_http_actor_context(FakeRequest(host="127.0.0.1"), FakeRepo())
    assert ctx.actor.user_id == mb.MCP_LOCAL_USER_ID
    assert ctx.enforce_acl is False


def test_mcp_generation_query_is_parsed_and_invalid_values_fail_closed(monkeypatch):
    _clear_mcp_env(monkeypatch)
    monkeypatch.setenv("ZE_MCP_ALLOW_LOOPBACK_TRUST", "1")
    ctx = mb._resolve_http_actor_context(FakeRequest(
        host="127.0.0.1", query={"project": "p1", "generation": "7"},
    ), FakeRepo())
    assert ctx.project_hint == "p1"
    assert ctx.project_generation == 7

    with pytest.raises(mb.ZeError) as invalid:
        mb._resolve_http_actor_context(FakeRequest(
            host="127.0.0.1", query={"project": "p1", "generation": "not-an-int"},
        ), FakeRepo())
    assert invalid.value.code == "validation_failed"


def test_mcp_proxy_headers_disable_loopback_trust(monkeypatch):
    _clear_mcp_env(monkeypatch)
    monkeypatch.setenv("ZE_MCP_ALLOW_LOOPBACK_TRUST", "1")
    with pytest.raises(mb.ZeError) as exc:
        mb._resolve_http_actor_context(
            FakeRequest(host="127.0.0.1", headers={"x-forwarded-for": "203.0.113.10"}),
            FakeRepo(),
        )
    assert exc.value.code == "auth_required"


def test_mcp_public_bind_forces_token_even_with_opt_out(monkeypatch):
    _clear_mcp_env(monkeypatch)
    monkeypatch.setenv("ZE_MCP_ALLOW_LOOPBACK_TRUST", "1")
    monkeypatch.setenv("ZE_MCP_BIND", "0.0.0.0")
    assert mb.require_token_mode() is True
    with pytest.raises(mb.ZeError):
        mb._resolve_http_actor_context(FakeRequest(host="127.0.0.1"), FakeRepo())


def test_mcp_legacy_require_token_false_is_explicit_loopback_opt_out(monkeypatch):
    _clear_mcp_env(monkeypatch)
    monkeypatch.setenv("ZE_MCP_REQUIRE_TOKEN", "0")
    assert mb.allow_loopback_trust() is True
    assert mb.require_token_mode() is False


def test_server_mode_hard_refuses_loopback_trust_opt_out(monkeypatch):
    """部署态(ZE_SERVER_MODE=1)硬拒 loopback-trust:忽略 ALLOW_LOOPBACK_TRUST,仍强制 token。"""
    _clear_mcp_env(monkeypatch)
    monkeypatch.setenv("ZE_SERVER_MODE", "1")
    monkeypatch.setenv("ZE_MCP_ALLOW_LOOPBACK_TRUST", "1")     # 部署态该 env 被忽略
    assert mb.allow_loopback_trust() is False
    assert mb.require_token_mode() is True
    with pytest.raises(mb.ZeError) as exc:
        mb._resolve_http_actor_context(FakeRequest(host="127.0.0.1"), FakeRepo())
    assert exc.value.code == "auth_required"


def test_server_mode_hard_refuses_legacy_require_token_false(monkeypatch):
    """部署态也忽略 legacy ZE_MCP_REQUIRE_TOKEN=0 opt-out,仍强制 token。"""
    _clear_mcp_env(monkeypatch)
    monkeypatch.setenv("ZE_SERVER_MODE", "1")
    monkeypatch.setenv("ZE_MCP_REQUIRE_TOKEN", "0")
    assert mb.allow_loopback_trust() is False
    assert mb.require_token_mode() is True
    with pytest.raises(mb.ZeError):
        mb._resolve_http_actor_context(FakeRequest(host="127.0.0.1"), FakeRepo())


def test_non_server_mode_local_dev_behavior_unchanged(monkeypatch):
    """非部署态(本地开发)行为不变:显式 opt-out 仍给本机全权 __mcp__(消融对照)。"""
    _clear_mcp_env(monkeypatch)                                # 无 ZE_SERVER_MODE
    monkeypatch.setenv("ZE_MCP_ALLOW_LOOPBACK_TRUST", "1")
    assert mb.allow_loopback_trust() is True
    assert mb.require_token_mode() is False
    ctx = mb._resolve_http_actor_context(FakeRequest(host="127.0.0.1"), FakeRepo())
    assert ctx.actor.user_id == mb.MCP_LOCAL_USER_ID
    assert ctx.enforce_acl is False


def test_mcp_pipeline_authority_error_keeps_machine_replay_facts():
    from pipeline_authority_commit import PipelineAuthorityCommitError

    error = PipelineAuthorityCommitError(
        "pipeline_authority_unavailable",
        "pipeline_authority_fresh_scan_failed",
        "private durable verifier detail",
        outcome_unknown=True,
        durability_phase="post_write_verification",
    ).with_commit_facts({"retry_safe": False, "document_durable": True})

    body = mb._commit_authority_error_body(error)
    assert body["code"] == "pipeline_authority_unavailable"
    assert body["context"] == {
        "reason": "pipeline_authority_fresh_scan_failed",
        "outcome_unknown": True,
        "durability_phase": "post_write_verification",
        "commit_facts": {"retry_safe": False, "document_durable": True},
    }


# ==============================================================
# R-C17: ze_file 项目级消费(修跨项目串仓库) — _github_file_result 单元
# ==============================================================

class _FakeCred:
    def __init__(self):
        self.id = "cred_1"
        self.metadata = {"owner": "legacy-owner", "repo": "legacy-repo", "ref": "dev"}


class _FakeStore:
    def __init__(self, cred=None):
        self._cred = cred
        self.revealed = None

    def find_for_user(self, user_id, providers=()):
        return self._cred

    def reveal_secret(self, user_id, cred_id):
        self.revealed = cred_id
        return "tok-secret"


class _LinkRepo:
    """repo 假体:按 project_id 返回项目级链接;记录 github_read_path 收到的参数。"""
    def __init__(self, links, cred=None):
        self._links = links
        self.credential_store = _FakeStore(cred)

    def read_project_git_link(self, pid):
        return self._links.get(pid)


def _actor_ctx():
    return SimpleNamespace(actor=SimpleNamespace(user_id="u1"))


def _patch_github_read(monkeypatch, calls):
    def fake_read(token, owner, repo, path, ref):
        calls.append({"owner": owner, "repo": repo, "ref": ref, "path": path})
        return {"ok": True, "repo": f"{owner}/{repo}"}
    monkeypatch.setattr(mb, "github_read_path", fake_read)


def test_ze_file_uses_project_link_not_account(monkeypatch):
    """两个项目各自链接 → 各读各的仓库(串仓库 bug 修复的直接断言)。"""
    calls: list = []
    _patch_github_read(monkeypatch, calls)
    repo = _LinkRepo({
        "proj-a": {"owner": "team-a", "repo": "repo-a", "ref": ""},
        "proj-b": {"owner": "team-b", "repo": "repo-b", "ref": "main"},
    }, cred=_FakeCred())
    mb._github_file_result(repo, _actor_ctx(), "proj-a", {"path": "README.md"})
    mb._github_file_result(repo, _actor_ctx(), "proj-b", {"path": "README.md"})
    assert calls[0]["owner"] == "team-a" and calls[0]["repo"] == "repo-a"
    assert calls[1]["owner"] == "team-b" and calls[1]["repo"] == "repo-b"


def test_ze_file_legacy_fallback_when_project_unlinked(monkeypatch):
    """项目未关联 → 整组回退账号级历史绑定(迁移期 fail-safe)。"""
    calls: list = []
    _patch_github_read(monkeypatch, calls)
    repo = _LinkRepo({}, cred=_FakeCred())
    result = mb._github_file_result(repo, _actor_ctx(), "proj-x", {"path": "a.md"})
    assert not result.get("isError")
    assert calls[0] == {"owner": "legacy-owner", "repo": "legacy-repo",
                       "ref": "dev", "path": "a.md"}


def test_ze_file_pat_not_bound_is_distinct_error(monkeypatch):
    """项目已关联但无 PAT → github_pat_not_bound(新错误码, 与未关联区分)。"""
    calls: list = []
    _patch_github_read(monkeypatch, calls)
    repo = _LinkRepo({"proj-a": {"owner": "o", "repo": "r", "ref": ""}}, cred=None)
    result = mb._github_file_result(repo, _actor_ctx(), "proj-a", {"path": "a"})
    assert result.get("isError")
    assert "github_pat_not_bound" in str(result)
    assert not calls


def test_ze_file_unlinked_and_no_cred_is_not_bound(monkeypatch):
    calls: list = []
    _patch_github_read(monkeypatch, calls)
    repo = _LinkRepo({}, cred=None)
    result = mb._github_file_result(repo, _actor_ctx(), "proj-a", {"path": "a"})
    assert result.get("isError") and "github_not_bound" in str(result)


def test_ze_file_link_read_crash_degrades_to_fallback(monkeypatch):
    """链接读取抛异常 → 按未关联处理(回退账号级), 绝不拖垮 ze_file。"""
    calls: list = []
    _patch_github_read(monkeypatch, calls)

    class _Boom(_LinkRepo):
        def read_project_git_link(self, pid):
            raise RuntimeError("disk error")

    repo = _Boom({}, cred=_FakeCred())
    result = mb._github_file_result(repo, _actor_ctx(), "proj-a", {"path": "a"})
    assert not result.get("isError")
    assert calls[0]["owner"] == "legacy-owner"


def test_gate_link_project_scope():
    """GATE-LINK-PROJECT-SCOPE(R-C17 防回归):关联源必须项目域。

    断言范围**只限本模块 _github_file_result**(对抗审:不得对全仓 META_REPO_OWNER
    做面断言,server 的 PAT metadata 校验是合法留存):
    ① 形参携带 project_id;② 函数体调用 read_project_git_link(项目链接为权威);
    ③ 账号级 metadata 读取仅存在于标注 legacy fallback 的回退分支;
    ④ project_git_link 模块与三条 /api/projects/{id}/git-link 路由在册。
    """
    import ast
    import inspect
    from pathlib import Path

    src = inspect.getsource(mb)
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_github_file_result")
    assert "project_id" in [a.arg for a in fn.args.args], "ze_file 必须携带项目维度"
    attr_calls = {c.func.attr for c in ast.walk(fn)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}
    assert "read_project_git_link" in attr_calls, "项目链接必须是 owner/repo 权威来源"
    fn_src = ast.get_source_segment(src, fn) or ""
    assert "legacy fallback" in fn_src, "账号级回退分支必须显式标注 legacy fallback"

    import project_git_link  # noqa: F401  模块在册(sdk _paths 可达)

    server_src = (Path(__file__).resolve().parent.parent / "server.py").read_text(
        encoding="utf-8")
    assert server_src.count('"/api/projects/{project_id}/git-link"') >= 3, \
        "GET/PUT/DELETE 三条项目级 git-link 路由必须在册"
