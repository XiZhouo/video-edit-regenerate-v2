"""本工具私有 Cursor 凭据库 —— 永不写入 Hub credentials.json。

Codex / Claude / 飞书登录仍走 Hub；仅 cursor_api_key 落在本服务 ZE_DATA_DIR。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from credentials import PROVIDER_CURSOR_API, CredentialRecord, CredentialStore

_APP_ROOT = Path(__file__).resolve().parents[2]
_LOCK = threading.Lock()
_STORE: CredentialStore | None = None


def cursor_data_dir() -> Path:
    env = os.environ.get("ZE_DATA_DIR")
    return Path(env).expanduser().resolve() if env else (_APP_ROOT / ".data")


def cursor_credentials_path() -> Path:
    return cursor_data_dir() / "cursor-credentials.json"


def cursor_credential_key_path() -> Path:
    return cursor_data_dir() / "cursor-credentials.key"


def cursor_credential_store() -> CredentialStore:
    """懒加载单例；文件落在本服务 .data，与 Hub 完全隔离。"""
    global _STORE
    with _LOCK:
        if _STORE is None:
            root = cursor_data_dir()
            root.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(root, 0o700)
            except OSError:
                pass
            _STORE = CredentialStore(
                path=cursor_credentials_path(),
                key_path=cursor_credential_key_path(),
            )
        return _STORE


def is_cursor_provider(provider: str) -> bool:
    return str(provider or "").strip().lower() == PROVIDER_CURSOR_API


def store_for_provider(provider: str, hub_store: CredentialStore) -> CredentialStore:
    return cursor_credential_store() if is_cursor_provider(provider) else hub_store


def list_merged_for_user(
    hub_store: CredentialStore,
    owner_user_id: str,
) -> list[CredentialRecord]:
    """Hub 非 cursor 条目 + 本工具 Cursor 私有库条目。"""
    hub_rows = [
        r for r in hub_store.list_for_user(owner_user_id)
        if r.provider != PROVIDER_CURSOR_API
    ]
    cursor_rows = cursor_credential_store().list_for_user(owner_user_id)
    return sorted(
        hub_rows + cursor_rows,
        key=lambda r: (r.provider, r.label, r.id),
    )


def resolve_store_for_credential(
    hub_store: CredentialStore,
    owner_user_id: str,
    credential_id: str,
) -> tuple[CredentialStore, CredentialRecord]:
    """按 credential_id 定位所在 store（先 Cursor 私有库，再 Hub）。"""
    from sdk import ZeError

    cursor_store = cursor_credential_store()
    try:
        return cursor_store, cursor_store.get_for_user(owner_user_id, credential_id)
    except ZeError as exc:
        if getattr(exc, "code", "") != "not_found":
            raise
    return hub_store, hub_store.get_for_user(owner_user_id, credential_id)


def reset_cursor_credential_store_for_tests() -> None:
    """测试用：丢掉进程内单例（不删磁盘文件）。"""
    global _STORE
    with _LOCK:
        _STORE = None


__all__ = [
    "PROVIDER_CURSOR_API",
    "cursor_credential_store",
    "cursor_credentials_path",
    "cursor_credential_key_path",
    "cursor_data_dir",
    "is_cursor_provider",
    "store_for_provider",
    "list_merged_for_user",
    "resolve_store_for_credential",
    "reset_cursor_credential_store_for_tests",
]
