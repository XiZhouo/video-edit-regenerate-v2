"""project_git_link.py — 项目级 git 仓库关联(R-C17「关联源统一项目级」)。

图景:一个项目 = 0/1 个本地文件夹(浏览器 IndexedDB,物理约束) + 0/1 个 git 仓库
(本模块,服务端 sidecar)。owner/repo/ref 从账号级 credential **降到项目级**;只有
PAT 令牌留账号级(credentials.py 的 github_repo,密封复用,所有项目共用)。AI 面
ze_file 按「当前项目的仓库 + 当前用户的 PAT」取材 —— 修掉「项目 A/B 的 AI 读同
一账号级仓库」的串仓库语义 bug。

存储:项目目录下独立 sidecar `.ze-git-link.json`(与本地文件夹关联同层——都在
Document 之外)。三条结构性理由:
  - duplicate 只拷 durable_entries(document/entities/assets),副本天然「未关联」,
    与本地链接(新 pid=新 IndexedDB 槽)对称;
  - export/.ze-pack 不含 sidecar,分享包绝不泄露仓库绑定;
  - 关联不是可 undo 的业务数据,不进 Operation/oplog(editor-arch 规则 3)。
显式决策(对抗审 M1-M3):
  - sidecar **不存** pat_credential_id —— PAT 按 actor 实时解析(find_for_user),
    避免跨用户泄露凭据 id 与悬挂引用;
  - 并发模型 = last-write-wins(配置级数据,无版本栅栏;文档级才有 generation);
  - topology.replace(现仅 demo-reset)会丢 sidecar —— 新 incarnation 从零关联,
    符合「副本/重生不继承关联」的对称语义。
残缺即无效(对抗审 C4):owner/repo 任一为空的记录按「未关联」处理,绝不与账号级
历史 metadata 交叉拼接。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from github_repo import validate_owner, validate_repo, validate_ref

PROJECT_GIT_LINK_FILE = ".ze-git-link.json"
GIT_LINK_SCHEMA = "ze.project_git_link.v1"


@dataclass(frozen=True)
class GitLinkRecord:
    owner: str
    repo: str
    ref: str = ""
    updated_at: float = 0.0
    updated_by: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner, "repo": self.repo, "ref": self.ref,
            "updated_at": self.updated_at,
        }


def _link_path(project_dir: Path) -> Path:
    return project_dir / PROJECT_GIT_LINK_FILE


def read_link(project_dir: Path) -> GitLinkRecord | None:
    """读 sidecar;缺失/损坏/残缺(owner|repo 空)一律返回 None(fail-safe,绝不拖垮加载)。"""
    try:
        raw = json.loads(_link_path(project_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("schema") != GIT_LINK_SCHEMA:
        return None
    owner = raw.get("owner")
    repo = raw.get("repo")
    if not isinstance(owner, str) or not owner or not isinstance(repo, str) or not repo:
        return None   # 残缺即无效(C4):不给消费者交叉拼接的机会
    ref = raw.get("ref")
    updated_at = raw.get("updated_at")
    return GitLinkRecord(
        owner=owner, repo=repo,
        ref=ref if isinstance(ref, str) else "",
        updated_at=float(updated_at) if isinstance(updated_at, (int, float)) else 0.0,
        updated_by=str(raw.get("updated_by") or ""),
    )


def write_link(
    project_dir: Path, *, owner: str, repo: str, ref: str = "",
    updated_by: str = "", now: float | None = None,
) -> GitLinkRecord:
    """入口校验(复用 github validators, 与 server 侧同一道纵深防御)后原子写。"""
    owner = validate_owner(owner)
    repo = validate_repo(repo)
    ref = validate_ref(ref) if ref else ""
    record = GitLinkRecord(
        owner=owner, repo=repo, ref=ref,
        updated_at=float(now) if now is not None else time.time(),
        updated_by=updated_by,
    )
    payload = {
        "schema": GIT_LINK_SCHEMA,
        "owner": record.owner, "repo": record.repo, "ref": record.ref,
        "updated_at": record.updated_at, "updated_by": record.updated_by,
    }
    target = _link_path(project_dir)
    fd, tmp_name = tempfile.mkstemp(
        prefix=PROJECT_GIT_LINK_FILE + ".", dir=str(project_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False))
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return record


def delete_link(project_dir: Path) -> bool:
    try:
        _link_path(project_dir).unlink()
        return True
    except FileNotFoundError:
        return False


def link_exists(project_dir: Path) -> bool:
    """项目卡徽标专用:纯存在性探针,零解析、任何异常都按 False(热路径安全)。"""
    try:
        return _link_path(project_dir).is_file()
    except Exception:   # noqa: BLE001  一个坏目录不许 500 整个项目列表
        return False
