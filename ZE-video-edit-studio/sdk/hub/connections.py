"""AccessConnection 读投影(规划 §3.3:连接权威复用 System CredentialStore,不建第二套明文)。

纯函数、离线可测:输入是 CredentialRecord.public_dict() 形状的 dict 列表(接线层用
credential_store.list_for_user(user).public_dict() 取,本层不碰 store、不 reveal_secret),
输出 AccessConnection。`kind`/`audience` 是**派生值**(由 descriptor 反查 + metadata 推,不落盘),
规避严格 store schema 加列 = 存量全拒载的雷(侦察 risk[2])。绝无 secret 出口(risk[1])。
"""

from __future__ import annotations

from typing import Any, Iterable

from .types import AccessConnection
from .descriptors import descriptor_for_provider

# descriptor.kind → §3.2 连接 kind 词表(official_cli/api_key/compatible_api/local_runtime/managed_gateway)
_KIND_MAP = {
    "official-cli": "official_cli",
    "openai-compatible": "api_key",
    "anthropic": "api_key",
    "gemini": "api_key",
    "internal-service": "managed_gateway",
    "video-task": "api_key",
}


def _derive_kind(provider: str) -> str:
    desc = descriptor_for_provider(provider)
    if desc is None:
        return "api_key"      # 未登记 provider(hupo_gateway/github 等)默认 api_key
    return _KIND_MAP.get(desc.kind, "api_key")


def access_connection_from_public(public: dict[str, Any]) -> AccessConnection:
    """从一条 public_dict(绝不含 secret)投影一个 AccessConnection。

    public_dict() 字段:id/provider/label/capabilities/status/metadata/last4/has_secret/时间戳。
    这里只取判可用所需的 public 面;audience 本片恒 default(持久化延后,见 decisionsNeeded)。"""
    provider = str(public.get("provider") or "")
    # 与 /api/ai-routes 同规:记录 capabilities 为空时回退 descriptor 粗能力(存量绑定
    # 未显式写 caps 时仍按 provider 默认能力可用),否则原样取记录声明的 caps。
    caps = tuple(str(c) for c in (public.get("capabilities") or ()))
    if not caps:
        desc = descriptor_for_provider(provider)
        caps = tuple(desc.capabilities) if desc else ()
    return AccessConnection(
        connection_id=str(public.get("id") or ""),
        provider=provider,
        kind=_derive_kind(provider),
        status=str(public.get("status") or ""),
        capabilities=caps,
        has_secret=bool(public.get("has_secret")),
        last4=str(public.get("last4") or ""),
        audience="default",
        metadata=dict(public.get("metadata") or {}),
    )


def project_connections(publics: Iterable[dict[str, Any]]) -> tuple[AccessConnection, ...]:
    """批量投影。输入是 [record.public_dict(), ...];顺序保留。"""
    return tuple(access_connection_from_public(p) for p in publics)
