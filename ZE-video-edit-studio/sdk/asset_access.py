"""sdk/asset_access.py — durable、ACL-safe 的资产读闸门(HTTP 与 MCP 共用一份)。

server.py 的 `GET /api/asset/{sha}` 与 mcp_bridge 的 `ze_read_asset` 必须走**同一道**
durable 授权闸门:只放行「本 project 本地 blob」或「被当前 canonical Document 实际引用
的共享池 blob」,绝不开任何绕过 project ACL 的资产读旁路。逻辑住在 sdk 聚合层,server
(FastAPI 层)与 mcp_bridge(sdk 同层)都 import 它,不各写一份 —— 一份判定、一处修复。

安全不变量:
  - 每次读都 `Document.open(ctx.doc_path)` 重读 durable 元数据,不信任进程内缓存的 ACL;
    被撤销的协作者不能靠旧缓存 ACL 继续读(与 server 原逻辑一致)。
  - 引用扫描**故意排除 read_only entity**(legacy overlay):兼容加载不得把外部数据升格
    为 canonical 共享池授权。
"""

from __future__ import annotations

from typing import Any

from . import _paths  # noqa: F401  (注入 sys.path;必须先于 document 叶子 import)

from document import Document  # noqa: E402


def scan_durable_asset_refs(document: Document) -> set[str]:
    """从一份安全打开的 durable generation 扫 canonical entity 的可达 sha 集合。"""
    refs: set[str] = set()

    def _walk(value: Any) -> None:
        if isinstance(value, dict):
            sha = value.get("sha256")
            if isinstance(sha, str) and len(sha) == 64 and all(
                c in "0123456789abcdef" for c in sha
            ):
                refs.add(sha)
            for child in value.values():
                _walk(child)
        elif isinstance(value, list):
            for child in value:
                _walk(child)

    for entity_type in document.types():
        for entity in document.list(entity_type, include_deleted=True):
            if entity.read_only:
                # 兼容加载遗留的 overlay:不得把外部数据当作 canonical 共享池授权。
                continue
            _walk(entity.data)
    return refs


def durable_asset_access(ctx: Any) -> tuple[dict[str, Any], dict[str, Any], set[str]]:
    """重读 durable ACL/config + 可达 sha 集合(不信任 stale Context 缓存)。

    必须在持 `ctx.lock` 下调用:document 文件被另一个 worker 原子替换后,拓扑代际可能
    不变,故这里重读原子替换后的 document 文件取一致快照。
    """
    durable_document = Document.open(ctx.doc_path)
    return (
        durable_document.acl,
        durable_document.config,
        scan_durable_asset_refs(durable_document),
    )


def asset_ref_for_read_locked(ctx: Any, sha256: str, durable_refs: set[str]):
    """仅解析本 project 本地 blob,或被 durable Document 引用的共享池 blob;否则 None。"""
    ref = ctx.asset_store.local_ref_for(sha256)
    if ref is not None:
        return ref
    if sha256 not in durable_refs:
        return None
    return ctx.asset_store.ref_for(sha256)


__all__ = [
    "scan_durable_asset_refs",
    "durable_asset_access",
    "asset_ref_for_read_locked",
]
