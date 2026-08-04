"""principal_directory.py — 稳定身份目录:login_name(邮箱) ↔ ze_user_id(`zeu_`+32hex)。

为什么要有它:现在 `user_id` 就是登录名本身。一旦把权限记录、ACL、审计都以登录名为键,
改一次登录名,这些引用全断;更糟的是它不当场报错,而是几个月后悄悄把权限判给错的人。
稳定 id 把「你是谁」和「你怎么登录」拆开:登录名可换,id 一经分配永不改变。

**本模块是加性的。** 它不改登录路径、不改 session、不改现有 ACL 行为 —— 只在旁边多建
一张映射表(治理规范迁移四步的第 1 步)。业务 API 从旧 ACL 切到稳定 id 是后续独立的一步,
必须等服务端授权器回归通过之后再做。

三条硬约束(违反即返工):
  1. **墓碑集**:历史里出现过的登录名永不再分配给别人。否则旧审计记录会解析到另一个人身上。
  2. **fail-closed**:解析不出必须抛,禁止退回「把字符串当 id 用」—— 那正是本模块要消灭的东西。
  3. **id 不可改写**:`ze_user_id` 只在首次创建时生成;改登录名走 rename(旧名进墓碑),id 不动。

落盘两份(都在 `default_system_auth_dir()`,随 ZE_SYSTEM_AUTH_DIR 按系列隔离):
  - `principals.json`        —— 严格符合合同 `ze.principal-directory.v1`
  - `principal_tombstones.json` —— 退役登录名。合同 schema 是 additionalProperties:false,
    塞不进去,也不该塞:墓碑是运行时约束,不是对外目录的一部分。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from auth import default_system_auth_dir  # noqa: E402  (同目录平铺布局)

DIRECTORY_SCHEMA = "ze.principal-directory.v1"
TOMBSTONE_SCHEMA = "ze.principal-tombstones.v1"

ZE_USER_ID_RE = re.compile(r"^zeu_[0-9a-f]{32}$")
# 与合同 schema 的 login_name pattern 逐字一致(改这里必须同步改 schema,反之亦然)。
#
# 两种形状都是真实身份:
#   邮箱          本地口令登录(espalierlin1997@gmail.com)
#   登录商:主体    外部身份提供方(feishu:ou_6f32458cfb19a412fcfcc9ca6389c59a)
#
# 原来只认邮箱,于是**飞书登录的管理员被当成垃圾跳过、拿不到稳定 id** —— 而稳定 id
# 是要长期当权限主键用的。同时这条正则还得挡住本地遗留的 'tester'/'demo'/'e2e...'
# 这类非身份(实测 55 个账号里 53 个是这种),它们进目录就是污染。
# 冒号形要求登录商全小写、主体 ≥4 字符,正好把两者分开。
LOGIN_NAME_RE = re.compile(
    r"^(?:[^\s@:]+@[^\s@:]+\.[^\s@:]+|[a-z][a-z0-9_-]*:[A-Za-z0-9._-]{4,})$")

STATUS_VALUES = ("active", "disabled")
CREDENTIAL_STATES = ("password-set", "admin-reset-required", "external-only")


class PrincipalDirectoryError(ValueError):
    """目录约束被违反。故意用异常而不是返回 None —— 解析失败必须能拦住调用方。"""


def new_ze_user_id() -> str:
    """`zeu_` + 32 位十六进制随机值(治理规范 §身份格式)。"""
    return "zeu_" + secrets.token_hex(16)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def default_principals_path() -> Path:
    return default_system_auth_dir() / "principals.json"


def default_tombstones_path() -> Path:
    return default_system_auth_dir() / "principal_tombstones.json"


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    """先写临时文件再 rename —— 半截文件比没有文件更坏(身份表读不出=全员拒绝登录)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class PrincipalDirectory:
    """稳定身份目录的运行时读写面。

    单进程内直接用;跨进程并发写不在本片范围(与 users.json 现状同级别,不新增保证)。
    """

    def __init__(self, path: Path | str | None = None, tombstones_path: Path | str | None = None,
                 *, authority_id: str = "local-device", location: str = "local-device") -> None:
        self.path = Path(path) if path else default_principals_path()
        self.tombstones_path = Path(tombstones_path) if tombstones_path else default_tombstones_path()
        self._authority_id = authority_id
        self._location = location
        self._principals: list[dict[str, Any]] = []
        self._tombstones: list[dict[str, Any]] = []
        self._load()

    # ---------- 载入 / 落盘 ----------

    def _load(self) -> None:
        if self.path.exists():
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            if doc.get("schema") != DIRECTORY_SCHEMA:
                raise PrincipalDirectoryError(
                    f"{self.path} 不是 {DIRECTORY_SCHEMA}(读到 {doc.get('schema')!r});"
                    "拒绝按空表继续 —— 那会把所有人当成新用户重新发 id。")
            self._principals = list(doc.get("principals") or [])
            auth = doc.get("authority") or {}
            self._authority_id = auth.get("authority_id", self._authority_id)
            self._location = auth.get("location", self._location)
        if self.tombstones_path.exists():
            tdoc = json.loads(self.tombstones_path.read_text(encoding="utf-8"))
            if tdoc.get("schema") != TOMBSTONE_SCHEMA:
                raise PrincipalDirectoryError(
                    f"{self.tombstones_path} 不是 {TOMBSTONE_SCHEMA};拒绝按空墓碑集继续 —— "
                    "墓碑集读空等于允许把退役登录名重新发给别人。")
            self._tombstones = list(tdoc.get("tombstones") or [])

    def document(self) -> dict[str, Any]:
        """导出严格符合 `ze.principal-directory.v1` 的文档(可直接过 product_contract.assert_valid)。"""
        return {
            "schema": DIRECTORY_SCHEMA,
            "authority": {"authority_id": self._authority_id, "location": self._location},
            "updated_at": _now(),
            "principals": [dict(p) for p in self._principals],
        }

    def save(self) -> None:
        _write_atomic(self.path, self.document())
        _write_atomic(self.tombstones_path, {
            "schema": TOMBSTONE_SCHEMA,
            "updated_at": _now(),
            "tombstones": [dict(t) for t in self._tombstones],
        })

    # ---------- 查询 ----------

    @property
    def principals(self) -> list[dict[str, Any]]:
        return [dict(p) for p in self._principals]

    @property
    def tombstoned_login_names(self) -> set[str]:
        return {str(t.get("login_name", "")) for t in self._tombstones}

    def _by_login(self, login_name: str) -> dict[str, Any] | None:
        return next((p for p in self._principals if p["login_name"] == login_name), None)

    def _by_id(self, ze_user_id: str) -> dict[str, Any] | None:
        return next((p for p in self._principals if p["ze_user_id"] == ze_user_id), None)

    def resolve(self, login_name: str) -> str:
        """登录名 → 稳定 id。**fail-closed**:解析不出就抛,绝不回退成「拿登录名当 id」。

        回退是这套东西里最危险的一行代码:它让「没迁移的账号」和「已迁移的账号」表现一致,
        于是缺失永远不会暴露,直到某天权限判给了另一个人。
        """
        name = str(login_name or "").strip()
        if not name:
            raise PrincipalDirectoryError("空登录名无法解析为稳定身份")
        found = self._by_login(name)
        if found is None:
            if name in self.tombstoned_login_names:
                raise PrincipalDirectoryError(
                    f"登录名 {name!r} 已退役(在墓碑集里),不对应任何在册身份")
            raise PrincipalDirectoryError(f"登录名 {name!r} 不在身份目录里")
        return str(found["ze_user_id"])

    def resolve_or_none(self, login_name: str) -> str | None:
        """给「查得到就用、查不到走别的分支」的读路径用。写路径与鉴权路径一律用 resolve()。"""
        try:
            return self.resolve(login_name)
        except PrincipalDirectoryError:
            return None

    # ---------- 变更 ----------

    def ensure(self, login_name: str, *, display_name: str = "",
               credential_state: str = "password-set", status: str = "active") -> str:
        """取该登录名的稳定 id;不存在则新发一个。已存在的 id 原样返回,永不重发。"""
        name = str(login_name or "").strip()
        if not LOGIN_NAME_RE.match(name):
            raise PrincipalDirectoryError(
                f"登录名 {name!r} 不符合合同要求的邮箱形状;身份目录不接受非邮箱登录名")
        if credential_state not in CREDENTIAL_STATES:
            raise PrincipalDirectoryError(f"未知 credential_state: {credential_state!r}")
        if status not in STATUS_VALUES:
            raise PrincipalDirectoryError(f"未知 status: {status!r}")
        existing = self._by_login(name)
        if existing is not None:
            return str(existing["ze_user_id"])
        if name in self.tombstoned_login_names:
            raise PrincipalDirectoryError(
                f"登录名 {name!r} 在墓碑集里,不能再分配给任何人 —— "
                "历史记录里它指向的是原来那个人")
        now = _now()
        record = {
            "ze_user_id": new_ze_user_id(),
            "login_name": name,
            "login_semantics": "identifier-only",
            "status": status,
            "credential_state": credential_state,
            "created_at": now,
            "updated_at": now,
        }
        if display_name:
            record["display_name"] = display_name
        self._principals.append(record)
        return str(record["ze_user_id"])

    def rename(self, ze_user_id: str, new_login_name: str) -> None:
        """改登录名:旧名进墓碑,`ze_user_id` 不动 —— 这正是稳定 id 存在的理由。"""
        name = str(new_login_name or "").strip()
        if not LOGIN_NAME_RE.match(name):
            raise PrincipalDirectoryError(f"登录名 {name!r} 不符合合同要求的邮箱形状")
        record = self._by_id(ze_user_id)
        if record is None:
            raise PrincipalDirectoryError(f"{ze_user_id!r} 不在身份目录里")
        if name in self.tombstoned_login_names:
            raise PrincipalDirectoryError(f"登录名 {name!r} 在墓碑集里,不能再分配")
        clash = self._by_login(name)
        if clash is not None and clash["ze_user_id"] != ze_user_id:
            raise PrincipalDirectoryError(f"登录名 {name!r} 已被 {clash['ze_user_id']} 占用")
        old = str(record["login_name"])
        if old == name:
            return
        record["login_name"] = name
        record["updated_at"] = _now()
        self._tombstones.append({"login_name": old, "ze_user_id": ze_user_id, "retired_at": _now()})

    def set_status(self, ze_user_id: str, status: str) -> None:
        if status not in STATUS_VALUES:
            raise PrincipalDirectoryError(f"未知 status: {status!r}")
        record = self._by_id(ze_user_id)
        if record is None:
            raise PrincipalDirectoryError(f"{ze_user_id!r} 不在身份目录里")
        record["status"] = status
        record["updated_at"] = _now()


__all__ = [
    "PrincipalDirectory", "PrincipalDirectoryError", "new_ze_user_id",
    "default_principals_path", "default_tombstones_path",
    "DIRECTORY_SCHEMA", "TOMBSTONE_SCHEMA", "ZE_USER_ID_RE", "LOGIN_NAME_RE",
]
