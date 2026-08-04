"""稳定身份目录的三条硬约束回归。

这三条错了都不当场报错 —— 墓碑漏了、解析回退了、id 被重发了,系统照常跑,几个月后才
以「权限判给错的人」的形式浮出来。所以每一条都必须有一个会红的测试压着。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_ENTITY = _HERE.parent.parent / "01-data-structure" / "02-entity"
for _d in (_HERE, _ENTITY):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from principal_directory import (  # noqa: E402
    DIRECTORY_SCHEMA, LOGIN_NAME_RE, TOMBSTONE_SCHEMA, ZE_USER_ID_RE,
    PrincipalDirectory, PrincipalDirectoryError, new_ze_user_id,
)


def _dir(tmp_path) -> PrincipalDirectory:
    return PrincipalDirectory(tmp_path / "principals.json", tmp_path / "tombs.json")


def test_new_id_shape_matches_contract():
    for _ in range(50):
        assert ZE_USER_ID_RE.match(new_ze_user_id())


def test_ensure_is_idempotent_and_never_reissues_an_id(tmp_path):
    d = _dir(tmp_path)
    first = d.ensure("a@example.com")
    assert d.ensure("a@example.com") == first
    d.save()
    # 重新载入后仍是同一个 id —— id 只在首次创建时生成
    assert _dir(tmp_path).resolve("a@example.com") == first


def test_resolve_is_fail_closed_not_string_fallback(tmp_path):
    """核心约束:解析不出必须抛。回退成「拿登录名当 id」会让缺失永远不暴露。"""
    d = _dir(tmp_path)
    with pytest.raises(PrincipalDirectoryError):
        d.resolve("nobody@example.com")
    with pytest.raises(PrincipalDirectoryError):
        d.resolve("")
    # resolve_or_none 是给读路径的显式选择,不是 resolve 的默认行为
    assert d.resolve_or_none("nobody@example.com") is None


def test_tombstoned_login_name_can_never_be_reassigned(tmp_path):
    d = _dir(tmp_path)
    uid = d.ensure("old@example.com")
    d.rename(uid, "new@example.com")
    assert d.resolve("new@example.com") == uid          # id 不因改名而变
    assert "old@example.com" in d.tombstoned_login_names
    with pytest.raises(PrincipalDirectoryError):        # 别人不能捡走旧名
        d.ensure("old@example.com")
    with pytest.raises(PrincipalDirectoryError):        # 解析旧名也不能落到新人身上
        d.resolve("old@example.com")


def test_tombstones_survive_reload(tmp_path):
    d = _dir(tmp_path)
    uid = d.ensure("old@example.com")
    d.rename(uid, "new@example.com")
    d.save()
    again = _dir(tmp_path)
    assert "old@example.com" in again.tombstoned_login_names
    with pytest.raises(PrincipalDirectoryError):
        again.ensure("old@example.com")


def test_rename_rejects_taken_login_name(tmp_path):
    d = _dir(tmp_path)
    a = d.ensure("a@example.com")
    d.ensure("b@example.com")
    with pytest.raises(PrincipalDirectoryError):
        d.rename(a, "b@example.com")


def test_non_email_login_name_rejected(tmp_path):
    """合同要求 login_name 是邮箱形。本地遗留的 'tester' 这类 id 不该混进身份目录。"""
    d = _dir(tmp_path)
    for bad in ("tester", "pw-c11fb5a4", "a@b", "", "  "):
        with pytest.raises(PrincipalDirectoryError):
            d.ensure(bad)
        assert not LOGIN_NAME_RE.match(bad)


def test_corrupt_file_refuses_to_load_as_empty(tmp_path):
    """读不出必须拒绝。按空表继续 = 把所有人当新用户重新发 id,历史引用全断。"""
    (tmp_path / "principals.json").write_text(json.dumps({"schema": "something-else"}), encoding="utf-8")
    with pytest.raises(PrincipalDirectoryError):
        _dir(tmp_path)


def test_corrupt_tombstones_refuse_to_load_as_empty(tmp_path):
    (tmp_path / "principals.json").write_text(
        json.dumps({"schema": DIRECTORY_SCHEMA,
                    "authority": {"authority_id": "local-device", "location": "local-device"},
                    "updated_at": "2026-07-26T00:00:00Z", "principals": []}), encoding="utf-8")
    (tmp_path / "tombs.json").write_text(json.dumps({"schema": "nope"}), encoding="utf-8")
    with pytest.raises(PrincipalDirectoryError):
        _dir(tmp_path)


def test_document_passes_the_real_contract_validator(tmp_path):
    """不是自己说符合 schema 就算 —— 过 System 那份合同校验器。"""
    contract_dir = Path.home() / ".claude" / "skills" / "ZE-skill-system" / "contracts" / "product"
    if not (contract_dir / "product_contract.py").exists():
        pytest.skip("ZE-skill-system 合同不在本检出内")
    sys.path.insert(0, str(contract_dir))
    import product_contract  # noqa: E402
    pytest.importorskip("jsonschema", reason="合同校验器要 jsonschema;缺了只能跳过,不能假绿")

    d = _dir(tmp_path)
    d.ensure("a@example.com", display_name="A")
    d.ensure("b@example.com", credential_state="external-only")
    doc = d.document()
    assert doc["schema"] == DIRECTORY_SCHEMA
    product_contract.assert_valid(doc)      # 不合规会抛


def test_saved_files_are_owner_only(tmp_path):
    d = _dir(tmp_path)
    d.ensure("a@example.com")
    d.save()
    for p in (tmp_path / "principals.json", tmp_path / "tombs.json"):
        assert oct(p.stat().st_mode & 0o777) == "0o600"
    assert json.loads((tmp_path / "tombs.json").read_text())["schema"] == TOMBSTONE_SCHEMA


# ── 外部登录商身份(飞书等)──────────────────────────────────────────────────────

def test_外部登录商身份也能拿到稳定id(tmp_path):
    """飞书登录是真实身份,而且是本机与 B 端的管理员身份。

    原来 LOGIN_NAME_RE 只认邮箱形,`feishu:ou_...` 被当成非身份跳过 —— 管理员
    因此拿不到稳定 id,而稳定 id 是要长期当权限主键用的。
    """
    d = _dir(tmp_path)
    uid = d.ensure("feishu:ou_6f32458cfb19a412fcfcc9ca6389c59a", display_name="hupo")
    assert ZE_USER_ID_RE.match(uid), uid
    assert d.resolve("feishu:ou_6f32458cfb19a412fcfcc9ca6389c59a") == uid


def test_本地遗留的非身份仍被拒(tmp_path):
    """放宽别放成筛子:55 个本地账号里 53 个是 'demo'/'tester'/'e2e...' 这类
    非身份,它们进目录就是污染一张长期权限表。"""
    d = _dir(tmp_path)
    for junk in ("demo", "tester", "boss", "pw-abc", "e2e07c1a3e64af6",
                 "codex_e2e_1781750036", "a@b", "feishu:ou", "FEISHU:ou_xxxxx"):
        with pytest.raises(PrincipalDirectoryError):
            d.ensure(junk)


def test_外部登录商身份过合同校验(tmp_path):
    """正则放宽了,合同 schema 的 pattern 必须同步放宽 —— 否则本地能建、
    落盘的文档过不了合同,是最难查的那种半截状态。"""
    contract_dir = Path.home() / ".claude" / "skills" / "ZE-skill-system" / "contracts" / "product"
    if not (contract_dir / "product_contract.py").exists():
        pytest.skip("ZE-skill-system 合同不在本检出内")
    sys.path.insert(0, str(contract_dir))
    import product_contract  # noqa: E402
    pytest.importorskip("jsonschema", reason="合同校验器要 jsonschema;缺了只能跳过,不能假绿")

    d = _dir(tmp_path)
    d.ensure("feishu:ou_6f32458cfb19a412fcfcc9ca6389c59a", display_name="hupo")
    product_contract.assert_valid(d.document())
