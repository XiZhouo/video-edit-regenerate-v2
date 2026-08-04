"""系统 1b「github 仓库关联」定向测试。

覆盖:绑定(token 密封,不落文件、不入 public_dict;owner/repo/ref 可回显)→ 列文件 →
读文件 → 未绑 / 越权拒 → rate-limit / token 失效 / 二进制 / 超大结构化拒 → 路径穿越拒 →
ze_file handler 端到端(token 不入带内、服务端确实用到)。GitHub API 全 mock,不发真网络。
"""

from __future__ import annotations

import base64
import json
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for _p in (str(ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import github_repo  # noqa: E402
from sdk import (  # noqa: E402
    Actor,
    CredentialStore,
    PROVIDER_GITHUB_REPO,
    CREDENTIAL_STATUS_ACTIVE,
    ZeError,
)
import sdk.mcp_bridge as mb  # noqa: E402


def _store(tmp_path: Path) -> CredentialStore:
    return CredentialStore(path=tmp_path / "credentials.json", key_path=tmp_path / "credentials.key")


def _dir_body(entries):
    return json.dumps(entries).encode("utf-8")


def _file_body(data: bytes, *, path="README.md", encoding="base64"):
    return json.dumps({
        "type": "file", "name": Path(path).name, "path": path,
        "size": len(data), "sha": "deadbeef",
        "encoding": encoding,
        "content": base64.b64encode(data).decode("ascii") if encoding == "base64" else "",
    }).encode("utf-8")


# ---- 绑定 + 密封 + public_dict --------------------------------------------

def test_github_binding_seals_token_and_public_view(tmp_path):
    store = _store(tmp_path)
    rec = store.upsert_for_user(
        "feishu:ou_1", provider=PROVIDER_GITHUB_REPO, label="widgets repo",
        secret="ghp_supersecrettoken", metadata={"owner": "acme", "repo": "widgets", "ref": "main"},
    )
    raw = (tmp_path / "credentials.json").read_text(encoding="utf-8")
    assert "ghp_supersecrettoken" not in raw          # token 密封,不落明文
    assert rec.status == CREDENTIAL_STATUS_ACTIVE

    pub = store.list_for_user("feishu:ou_1")[0].public_dict()
    assert "ghp_supersecrettoken" not in json.dumps(pub, ensure_ascii=False)
    assert "secret_ref" not in pub and "owner_user_id" not in pub
    assert pub["has_secret"] is True
    assert pub["metadata"] == {"owner": "acme", "repo": "widgets", "ref": "main"}
    # 服务端可取回原文供只读客户端用
    assert store.reveal_secret("feishu:ou_1", rec.id) == "ghp_supersecrettoken"


def test_github_binding_without_token_is_needs_secret(tmp_path):
    store = _store(tmp_path)
    rec = store.upsert_for_user(
        "feishu:ou_1", provider=PROVIDER_GITHUB_REPO,
        metadata={"owner": "acme", "repo": "widgets"},
    )
    assert rec.status == "needs_secret"


# ---- 越权 -----------------------------------------------------------------

def test_cross_user_reveal_and_get_denied(tmp_path):
    store = _store(tmp_path)
    rec = store.upsert_for_user(
        "feishu:ou_1", provider=PROVIDER_GITHUB_REPO,
        secret="ghp_x", metadata={"owner": "a", "repo": "b"},
    )
    with pytest.raises(ZeError) as e1:
        store.reveal_secret("feishu:ou_2", rec.id)
    assert e1.value.code == "not_found"
    with pytest.raises(ZeError) as e2:
        store.get_for_user("feishu:ou_2", rec.id)
    assert e2.value.code == "not_found"


# ---- 只读客户端(mock GitHub API)-----------------------------------------

def test_client_lists_directory(monkeypatch):
    def http(url, token, timeout=15.0):
        assert "/contents/" in url and token == "tok"
        return 200, {"X-RateLimit-Remaining": "58"}, _dir_body([
            {"name": "README.md", "path": "README.md", "type": "file", "size": 12},
            {"name": "src", "path": "src", "type": "dir", "size": 0},
        ])
    monkeypatch.setattr(github_repo, "_http_get", http)
    out = github_repo.read_path("tok", "acme", "widgets", "", "main")
    assert out["kind"] == "dir"
    assert {e["name"] for e in out["entries"]} == {"README.md", "src"}
    assert out["rate_limit"]["remaining"] == "58"


def test_client_reads_text_file(monkeypatch):
    monkeypatch.setattr(github_repo, "_http_get",
                        lambda u, t, timeout=15.0: (200, {}, _file_body(b"hello world")))
    out = github_repo.read_path("tok", "acme", "widgets", "README.md", "main")
    assert out["kind"] == "file"
    assert out["text"] == "hello world"


def test_client_token_invalid_is_structured(monkeypatch):
    monkeypatch.setattr(github_repo, "_http_get",
                        lambda u, t, timeout=15.0: (401, {}, b'{"message":"Bad credentials"}'))
    with pytest.raises(ZeError) as e:
        github_repo.read_path("bad", "acme", "widgets", "README.md")
    assert e.value.code == "github_unauthorized"


def test_client_private_repo_forbidden(monkeypatch):
    monkeypatch.setattr(github_repo, "_http_get",
                        lambda u, t, timeout=15.0: (403, {"X-RateLimit-Remaining": "42"}, b"{}"))
    with pytest.raises(ZeError) as e:
        github_repo.read_path("tok", "acme", "secret", "README.md")
    assert e.value.code == "github_forbidden"


def test_client_rate_limited_not_silent(monkeypatch):
    monkeypatch.setattr(github_repo, "_http_get", lambda u, t, timeout=15.0: (
        403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1700000000"}, b"{}"))
    with pytest.raises(ZeError) as e:
        github_repo.read_path("tok", "acme", "widgets", "x")
    assert e.value.code == "github_rate_limited"
    assert e.value.context.get("reset") == "1700000000"


def test_client_not_found(monkeypatch):
    monkeypatch.setattr(github_repo, "_http_get",
                        lambda u, t, timeout=15.0: (404, {}, b'{"message":"Not Found"}'))
    with pytest.raises(ZeError) as e:
        github_repo.read_path("tok", "acme", "widgets", "nope.txt")
    assert e.value.code == "github_not_found"


def test_client_binary_refused(monkeypatch):
    monkeypatch.setattr(github_repo, "_http_get",
                        lambda u, t, timeout=15.0: (200, {}, _file_body(b"\xff\xfe\x00\x01", path="logo.png")))
    with pytest.raises(ZeError) as e:
        github_repo.read_path("tok", "acme", "widgets", "logo.png")
    assert e.value.code == "github_binary_file"


def test_client_too_large_refused_without_fetch(monkeypatch):
    # size 超上限 → 结构化拒;encoding='none' 不应触发(先按 size 拒)。
    body = json.dumps({"type": "file", "path": "big.bin", "size": 10_000_000,
                       "sha": "s", "encoding": "none", "content": ""}).encode()
    monkeypatch.setattr(github_repo, "_http_get", lambda u, t, timeout=15.0: (200, {}, body))
    with pytest.raises(ZeError) as e:
        github_repo.read_path("tok", "acme", "widgets", "big.bin")
    assert e.value.code == "github_file_too_large"


def test_normalize_rejects_traversal_and_absolute():
    for bad in ("../etc/passwd", "a/../../b", "/abs/path"):
        norm = None
        try:
            norm = github_repo.normalize_path(bad)
        except ZeError as exc:
            assert exc.code == "validation_failed"
            continue
        # 绝对路径被 lstrip 成相对是允许的;穿越必须被拒
        assert ".." not in norm.split("/")


def test_validators_reject_injection():
    with pytest.raises(ZeError):
        github_repo.validate_owner("acme/../evil")
    with pytest.raises(ZeError):
        github_repo.validate_repo("re po")
    with pytest.raises(ZeError):
        github_repo.validate_ref("main/../secret")


# ---- ze_file handler(系统 2 后端;token 不入带内)-------------------------

def _fake_repo(store):
    return types.SimpleNamespace(credential_store=store)


def _actor_ctx(user_id):
    return mb.McpActorCtx(actor=Actor(user_id=user_id, source="ai"), enforce_acl=True)


def test_ze_file_handler_reads_via_binding(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.upsert_for_user(
        "feishu:ou_1", provider=PROVIDER_GITHUB_REPO,
        secret="ghp_tok", metadata={"owner": "acme", "repo": "widgets", "ref": "main"},
    )
    seen = {}

    def http(url, token, timeout=15.0):
        seen["token"] = token
        seen["url"] = url
        return 200, {}, _dir_body([{"name": "README.md", "path": "README.md", "type": "file", "size": 1}])

    monkeypatch.setattr(github_repo, "_http_get", http)
    res = mb._github_file_result(_fake_repo(store), _actor_ctx("feishu:ou_1"), "proj-t", {"path": ""})
    assert res["isError"] is False
    text = res["content"][0]["text"]
    assert "README.md" in text
    assert "ghp_tok" not in text            # token 绝不入带内响应
    assert seen["token"] == "ghp_tok"       # 服务端确实用绑定 token 调 GitHub
    assert "acme/widgets" in seen["url"]


def test_ze_file_handler_reports_not_bound(tmp_path):
    store = _store(tmp_path)
    res = mb._github_file_result(_fake_repo(store), _actor_ctx("feishu:ou_9"), "proj-t", {"path": "x"})
    assert res["isError"] is True
    assert "github_not_bound" in res["content"][0]["text"]


def test_ze_file_handler_surfaces_upstream_error_without_token(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.upsert_for_user(
        "feishu:ou_1", provider=PROVIDER_GITHUB_REPO,
        secret="ghp_secret", metadata={"owner": "acme", "repo": "widgets"},
    )
    monkeypatch.setattr(github_repo, "_http_get",
                        lambda u, t, timeout=15.0: (401, {}, b'{"message":"Bad credentials"}'))
    res = mb._github_file_result(_fake_repo(store), _actor_ctx("feishu:ou_1"), "proj-t", {"path": "README.md"})
    assert res["isError"] is True
    body = res["content"][0]["text"]
    assert "github_unauthorized" in body
    assert "ghp_secret" not in body
