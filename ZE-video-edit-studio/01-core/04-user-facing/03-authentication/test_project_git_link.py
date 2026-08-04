"""test_project_git_link.py — 项目级 git 关联 sidecar(R-C17 W1)。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from project_git_link import (
    GIT_LINK_SCHEMA, PROJECT_GIT_LINK_FILE,
    delete_link, link_exists, read_link, write_link,
)
from entity import ZeError


def test_round_trip(tmp_path: Path) -> None:
    rec = write_link(tmp_path, owner="octocat", repo="hello", ref="main",
                     updated_by="feishu:u1", now=123.0)
    assert rec.owner == "octocat" and rec.updated_at == 123.0
    back = read_link(tmp_path)
    assert back is not None
    assert (back.owner, back.repo, back.ref) == ("octocat", "hello", "main")
    assert link_exists(tmp_path) is True
    assert delete_link(tmp_path) is True
    assert read_link(tmp_path) is None
    assert delete_link(tmp_path) is False   # 幂等


def test_invalid_owner_repo_ref_rejected(tmp_path: Path) -> None:
    with pytest.raises(ZeError):
        write_link(tmp_path, owner="bad owner!", repo="r")
    with pytest.raises(ZeError):
        write_link(tmp_path, owner="o", repo="bad/repo")
    with pytest.raises(ZeError):
        write_link(tmp_path, owner="o", repo="r", ref="bad ref\n")
    assert read_link(tmp_path) is None   # 拒绝时不落盘


def test_corrupt_or_partial_reads_as_none(tmp_path: Path) -> None:
    p = tmp_path / PROJECT_GIT_LINK_FILE
    p.write_text("not json", encoding="utf-8")
    assert read_link(tmp_path) is None
    # 残缺即无效(C4):有 schema 但 repo 缺
    p.write_text(json.dumps({"schema": GIT_LINK_SCHEMA, "owner": "o"}),
                 encoding="utf-8")
    assert read_link(tmp_path) is None
    # 错 schema
    p.write_text(json.dumps({"schema": "other", "owner": "o", "repo": "r"}),
                 encoding="utf-8")
    assert read_link(tmp_path) is None


def test_link_exists_never_raises(tmp_path: Path) -> None:
    assert link_exists(tmp_path / "no-such-dir") is False


def test_atomic_write_permissions(tmp_path: Path) -> None:
    write_link(tmp_path, owner="o", repo="r")
    mode = (tmp_path / PROJECT_GIT_LINK_FILE).stat().st_mode & 0o777
    assert mode == 0o600
