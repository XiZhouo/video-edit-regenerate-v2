"""Asset — 大文件内容寻址存储 (AssetStore + AssetRef).

完整 SPEC 见同目录 SPEC.md.

已实装 (无跨叶子依赖):
  - AssetRef: sha256 / mime / size / ext (frozen dataclass) + to_dict/from_dict
  - AssetStore:
      put(bytes) / put_file(path)               — sha256 dedup + atomic
      get(ref)                                   — checksum 防篡改
      has(ref)
      add_ref(ref) / release(ref) / ref_count(ref) — legacy 派生引用观测
      scan_document_ref_counts(doc)              — 全 Document 多重集可达性
      reconcile_document_refs(doc)               — 用可达性重建本地 refs 观测
      gc(doc)                                     — 对账后只清不可达 blob
      all_refs()
      scan_entity_refs(entity) [static]          — 扫 entity.data 找 AssetRef
  - ZeError 共享: 从 entity 模块 import (跟 document 一致)

待补 (真依赖别的叶子):
  - ChunkedUploader (分片上传)              [12-file-search / commands-operations]
  - 真 asset_ref 字段类型校验               [04-field-types]
  - 多进程锁 / 数据库事务 participant / orphan sweeper [W06 persistence]
  - 跨项目/账号全局配额账本 / 分布式后端   [W06+]
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import stat
import sys
from collections import Counter
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

# 共享 ZeError: 从 02-entity 兄弟目录 import (v3 平铺布局, 暂用 sys.path 注入)
_ENTITY_DIR = Path(__file__).resolve().parent.parent / "02-entity"
_PROTOCOL_DIR = Path(__file__).resolve().parent.parent.parent / "_protocol"
for _dependency_dir in (_ENTITY_DIR, _PROTOCOL_DIR):
    if str(_dependency_dir) not in sys.path:
        sys.path.insert(0, str(_dependency_dir))

from entity import ZeError  # noqa: E402
from effect_context import assert_external_effect_allowed  # noqa: E402


# ==============================================================
# AssetRef 值类型
# ==============================================================

@dataclass(frozen=True)
class AssetRef:
    """指向 AssetStore 里某 blob 的不可变引用.

    sha256: 64 个 hex 字符 (内容唯一 ID)
    mime: e.g. "image/png" / "video/mp4" / "application/pdf"
    size: 字节数
    ext: 包前导点, e.g. ".png" / ".mp4" / "" (无 ext)
    """
    sha256: str
    mime: str = "application/octet-stream"
    size: int = 0
    ext: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AssetRef":
        return cls(
            sha256=d["sha256"],
            mime=d.get("mime", "application/octet-stream"),
            size=int(d.get("size", 0)),
            ext=d.get("ext", ""),
        )


# ==============================================================
# AssetStore
# ==============================================================

class AssetStore:
    """内容寻址 blob 存储 + Document 可达性对账.

    物理布局:
        <root>/blobs/<sha[:2]>/<sha><ext>      — blob (内容寻址, 不可变)
        <root>/meta/<sha>.json                  — meta {mime, size, refs, ext}
    """

    def __init__(self, root: str | os.PathLike, *, fallback: "AssetStore | None" = None) -> None:
        """fallback(P3):本地缺 blob 时只读委托的共享池(Repository 级)。

        读路径(get/has/ref_for)本地缺失 → 查 fallback(blob 不可变 + sha 自校验 → 跨层安全)。
        写/派生观测/GC(put/add_ref/release/reconcile/gc/ref_count)**仅本地** —— per-project 语义不变,
        共享内容的生命周期由共享池自己管。is_local/promote 显式区分本地与共享。
        """
        self.root = Path(root)
        self.blobs_dir = self.root / "blobs"
        self.meta_dir = self.root / "meta"
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        self.fallback = fallback

    # ---- put / get ----

    def put(
        self,
        data: bytes,
        *,
        mime: str = "application/octet-stream",
        ext: str = "",
    ) -> AssetRef:
        """写 blob (sha256 dedup); 已存在不重写; meta 已存在保留 refs.

        安全:ext 直接进 blob 文件名 (blobs/<sha[:2]>/<sha><ext>),严校防路径穿越
        (核心组件,任何 caller 都防,不只 HTTP 层)。
        """
        if ext and (not ext.startswith(".") or "/" in ext or "\\" in ext or ".." in ext or len(ext) > 17):
            raise ZeError(
                "validation_failed",
                f"unsafe asset ext {ext!r} (须 .字母数字, 无路径分隔 / ..)",
                context={"ext": ext},
            )
        sha = hashlib.sha256(data).hexdigest()
        ref = AssetRef(sha256=sha, mime=mime, size=len(data), ext=ext)
        blob_path = self._blob_path(sha, ext)
        meta_path = self._meta_path(sha)

        # blob: dedup, 已存在跳过
        if not blob_path.exists():
            blob_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = blob_path.with_suffix(blob_path.suffix + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, blob_path)

        # meta: 已存在保留 refs (不覆盖), 新建则 refs=0
        if not meta_path.exists():
            self._write_meta(sha, {"mime": mime, "size": len(data), "refs": 0, "ext": ext})

        return ref

    def put_file(
        self,
        path: str | os.PathLike,
        *,
        mime: str | None = None,
    ) -> AssetRef:
        """读完整文件 + 推断 mime / ext."""
        assert_external_effect_allowed("AssetStore.put_file")
        p = Path(path)
        data = p.read_bytes()
        if mime is None:
            guessed, _ = mimetypes.guess_type(str(p))
            mime = guessed or "application/octet-stream"
        ext = p.suffix  # 含前导点 (空字符串如果无后缀)
        return self.put(data, mime=mime, ext=ext)

    def get(self, ref: AssetRef) -> bytes:
        """读 blob + checksum 防篡改. 本地缺失且有 fallback → 委托共享池(其自带 sha 校验)."""
        blob_path = self._find_blob(ref)
        if blob_path is None or not blob_path.exists():
            if self.fallback is not None:
                return self.fallback.get(ref)
            raise ZeError(
                "asset_not_found",
                f"asset {ref.sha256[:8]}... missing",
                context={"sha256": ref.sha256},
            )
        data = blob_path.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != ref.sha256:
            raise ZeError(
                "asset_corrupted",
                f"checksum mismatch for asset {ref.sha256[:8]}...",
                context={"expected": ref.sha256, "actual": actual, "sha256": ref.sha256},
            )
        return data

    def has(self, ref: AssetRef) -> bool:
        blob_path = self._find_blob(ref)
        if blob_path is not None and blob_path.exists():
            return True
        return self.fallback.has(ref) if self.fallback is not None else False

    def is_local(self, ref: AssetRef) -> bool:
        """blob 是否在本 store 本地(不含 fallback 共享池)。HTTP 越权护栏据此区分。"""
        blob_path = self._find_blob(ref)
        return blob_path is not None and blob_path.exists()

    def promote(self, ref: AssetRef) -> AssetRef:
        """把 blob 复制去重进共享池(fallback);无 fallback 配置 → 报错。返回共享池 ref(同 sha)。

        已知限制(非热路径,mod 管理动作):并发 promote 同一 blob 时,共享池 put 的 meta 写
        非原子(blob 走 os.replace 原子、meta 单独写)→ refs 计数可能竞争。当前 promote 不写
        共享 refs(只落 blob+meta 基线 refs=0),竞争影响有限;若未来加共享引用计数需补锁。
        """
        assert_external_effect_allowed("AssetStore.promote")
        if self.fallback is None:
            raise ZeError(
                "asset_no_shared_pool",
                "no shared asset store configured (promote 需 fallback 共享池)",
                context={"sha256": ref.sha256},
            )
        data = self.get(ref)   # 本地优先;已在共享则原样校验
        return self.fallback.put(data, mime=ref.mime, ext=ref.ext)

    # ---- legacy 引用观测 ----

    def add_ref(self, ref: AssetRef) -> int:
        """+1 派生 refs 观测;返回新值.meta 不存在时抛 asset_not_found.

        ``refs`` 不再是 GC 权威。这个兼容 API 只供旧调用方与诊断使用；安全回收
        始终以 ``reconcile_document_refs`` 扫描得到的 Document 可达性为准。
        """
        assert_external_effect_allowed("AssetStore.add_ref")
        meta = self._read_meta(ref.sha256)
        meta["refs"] = int(meta.get("refs", 0)) + 1
        self._write_meta(ref.sha256, meta)
        return int(meta["refs"])

    def release(self, ref: AssetRef) -> int:
        """-1 派生 refs 观测(下限 0,idempotent);meta 不存在时返回 0."""
        assert_external_effect_allowed("AssetStore.release")
        meta_path = self._meta_path(ref.sha256)
        if not meta_path.exists():
            return 0
        meta = json.loads(meta_path.read_text())
        meta["refs"] = max(0, int(meta.get("refs", 0)) - 1)
        self._write_meta(ref.sha256, meta)
        return int(meta["refs"])

    def ref_count(self, ref: AssetRef) -> int:
        if not self._meta_path(ref.sha256).exists():
            return 0
        meta = json.loads(self._meta_path(ref.sha256).read_text())
        return int(meta.get("refs", 0))

    # ---- Document 可达性 / GC ----

    @staticmethod
    def scan_document_ref_counts(doc: Any) -> Counter[str]:
        """扫描完整 Document，返回 ``sha256 -> 槽位数`` 的多重集。

        GC 的唯一安全权威是当前 canonical Document 的完整可达性，而不是命令
        handler 曾经增减过几次计数。这里故意包含软删 entity（仍可 restore）与
        read_only source/overlay；同一 sha 出现在两个字段或两个 entity 时计两次。

        ``doc`` 采用 Document 的最小只读协议 ``types()`` +
        ``list(type, include_deleted=True)``，这样 asset 叶子不反向 import Document。
        缺少该协议会显式拒绝，不能把“不知道”误判成“零引用”。
        """
        if doc is None or not callable(getattr(doc, "types", None)) or not callable(
            getattr(doc, "list", None)
        ):
            raise ZeError(
                "asset_reachability_required",
                "asset reachability requires a canonical Document",
                context={"required": ["types()", "list(type, include_deleted=True)"]},
            )

        counts: Counter[str] = Counter()
        for entity_type in doc.types():
            for entity in doc.list(entity_type, include_deleted=True):
                counts.update(ref.sha256 for ref in AssetStore.scan_entity_refs(entity))
        return counts

    def reconcile_document_refs(self, doc: Any) -> dict[str, int]:
        """按完整 Document 可达性重建所有**本地** meta 的 ``refs`` 观测。

        返回每个本地 sha 的最终槽位数。Document 引用了 fallback/shared blob 时
        不会在本地凭空创建 meta；本方法只修复当前 store 自己拥有的生命周期账。
        扫描与全部 meta 解析都先完成，随后才开始写，避免半次扫描就把未知状态写
        成 0。多进程互斥与数据库事务 participant 属 W06，本地 W05 先建立正确权威。
        """
        assert_external_effect_allowed("AssetStore.reconcile_document_refs")
        reachable = self.scan_document_ref_counts(doc)

        # 先完整读取/解析；任一损坏都在任何 meta 写入前失败，GC 也不会开始删除。
        loaded: list[tuple[str, dict[str, Any]]] = []
        for meta_file in sorted(self.meta_dir.glob("*.json")):
            loaded.append((meta_file.stem, json.loads(meta_file.read_text())))

        local_counts: dict[str, int] = {}
        for sha, meta in loaded:
            target = int(reachable.get(sha, 0))
            local_counts[sha] = target
            if int(meta.get("refs", 0)) != target:
                meta["refs"] = target
                self._write_meta(sha, meta)
        return local_counts

    def gc(self, doc: Any | None = None) -> list[str]:
        """对账 canonical Document 后，清理本地不可达 blob 与 meta。

        没有 Document 时显式 fail-closed；legacy ``refs=0`` 绝不能单独授权删除。
        有 Document 时先全量扫描并把 meta.refs 修正成派生观测，之后只删除可达
        槽位数为 0 的本地项。返回被清理的 sha256 列表。
        """
        assert_external_effect_allowed("AssetStore.gc")
        if doc is None:
            raise ZeError(
                "asset_reachability_required",
                "AssetStore.gc requires a canonical Document; no files were removed",
                context={"operation": "gc"},
            )

        local_counts = self.reconcile_document_refs(doc)
        removed: list[str] = []
        for meta_file in sorted(self.meta_dir.glob("*.json")):
            sha = meta_file.stem
            if local_counts.get(sha, 0) != 0:
                continue
            meta = json.loads(meta_file.read_text())
            ext = meta.get("ext", "")
            blob_path = self._blob_path(sha, ext)
            if blob_path.exists():
                blob_path.unlink()
            meta_file.unlink()
            removed.append(sha)
        # 清空的 blobs/<sha[:2]>/ 分片目录
        for shard in self.blobs_dir.iterdir():
            if shard.is_dir() and not any(shard.iterdir()):
                shard.rmdir()
        return removed

    # ---- 辅助 ----

    def all_refs(self) -> list[AssetRef]:
        """列全部已知 AssetRef (含 refs=0 pending GC)."""
        out: list[AssetRef] = []
        for meta_file in sorted(self.meta_dir.glob("*.json")):
            meta = json.loads(meta_file.read_text())
            out.append(AssetRef(
                sha256=meta_file.stem,
                mime=meta.get("mime", "application/octet-stream"),
                size=int(meta.get("size", 0)),
                ext=meta.get("ext", ""),
            ))
        return out

    def total_size(self) -> int:
        """Return verified bytes occupied by unique local blobs.

        Capacity admission must not trust a caller-editable ``meta.size``.  Each
        metadata row therefore has to name a canonical sha/ext pair whose local
        target exists as a regular, non-symlink file and whose ``lstat`` size
        exactly matches the declared size.  Content checksums remain the read
        path's responsibility (``get``); hashing the whole store before every
        upload would make quota checks O(total blob bytes).  Any structural or
        accounting mismatch fails closed instead of authorizing more storage.
        """
        total = 0
        for meta_file in sorted(self.meta_dir.glob("*.json")):
            sha = meta_file.stem
            if len(sha) != 64 or any(ch not in "0123456789abcdef" for ch in sha):
                raise ZeError(
                    "asset_corrupted", "asset metadata filename is not a canonical sha256",
                    context={"sha256": sha},
                )
            try:
                meta = json.loads(meta_file.read_text())
                if not isinstance(meta, dict):
                    raise TypeError("metadata root must be an object")
                declared_size = meta["size"]
                if isinstance(declared_size, bool) or not isinstance(declared_size, int):
                    raise TypeError("metadata size must be an integer")
                ext = meta.get("ext", "")
                if not isinstance(ext, str) or (
                    ext
                    and (
                        not ext.startswith(".")
                        or "/" in ext
                        or "\\" in ext
                        or ".." in ext
                        or len(ext) > 17
                    )
                ):
                    raise ValueError("metadata ext is unsafe")
                if declared_size < 0:
                    raise ValueError("metadata size is negative")
                blob_path = self._blob_path(sha, ext)
                blob_stat = blob_path.lstat()
                if not stat.S_ISREG(blob_stat.st_mode) or blob_path.is_symlink():
                    raise ValueError("blob is not a regular local file")
                if blob_stat.st_size != declared_size:
                    raise ValueError("metadata size does not match blob size")
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ZeError(
                    "asset_corrupted",
                    "asset metadata and local blob accounting disagree",
                    context={"sha256": sha, "reason": type(exc).__name__},
                ) from exc
            total += declared_size
        return total

    def local_ref_for(self, sha256: str) -> AssetRef | None:
        """仅从**本地** meta 重建 AssetRef;不存在/损坏返 None(不碰 fallback 共享池)。

        越权护栏(server.py)靠它做「本地优先、不引用就不查共享池」—— 避免对未引用的共享 sha
        触发共享池 meta stat,杜绝「404 但时间侧信道泄露存在性」。
        """
        meta_path = self._meta_path(sha256)
        if not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            return None   # meta 损坏(崩溃中途写)→ 当不存在,GET 返 404 而非 500
        return AssetRef(
            sha256=sha256,
            mime=meta.get("mime", "application/octet-stream"),
            size=int(meta.get("size", 0)),
            ext=meta.get("ext", ""),
        )

    def ref_for(self, sha256: str) -> AssetRef | None:
        """按 sha256 从 meta 重建 AssetRef(mime/size/ext);本地无则委托共享池;都无返 None。

        给 HTTP GET /api/asset/{sha256} 用 —— 前端只持 sha,服务端据 meta 补 mime 再返 blob。
        """
        local = self.local_ref_for(sha256)
        if local is not None:
            return local
        return self.fallback.ref_for(sha256) if self.fallback is not None else None

    @staticmethod
    def scan_entity_refs(entity: Any) -> list[AssetRef]:
        """递归扫 entity.data 找 sha256 字典 -> [AssetRef]."""
        out: list[AssetRef] = []

        def walk(v: Any) -> None:
            if isinstance(v, dict):
                sha = v.get("sha256")
                if isinstance(sha, str) and len(sha) == 64:
                    out.append(AssetRef.from_dict(v))
                # 始终继续递归子值:一个 dict 既可能本身是 AssetRef、又可能内嵌别的 ref
                # (如 {sha256, thumbnail:{sha256}})。不递归会漏掉兄弟/嵌套引用 → 越权护栏少算。
                for x in v.values():
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)

        walk(entity.data)
        return out

    @staticmethod
    def scan_doc_refs(doc: Any) -> set[str]:
        """兼容集合视图：完整多重集扫描的 sha key（跨项目读取护栏用）。"""
        return set(AssetStore.scan_document_ref_counts(doc))

    # ---- 内部 ----

    def _blob_path(self, sha: str, ext: str) -> Path:
        return self.blobs_dir / sha[:2] / f"{sha}{ext}"

    def _meta_path(self, sha: str) -> Path:
        return self.meta_dir / f"{sha}.json"

    def _find_blob(self, ref: AssetRef) -> Path | None:
        """从 ref 找 blob 路径. ref.ext 准, 直接拼; 否则查 meta 拿 ext."""
        # 优先用 ref 自带 ext
        candidate = self._blob_path(ref.sha256, ref.ext)
        if candidate.exists():
            return candidate
        # 退路: 查 meta 拿 ext
        meta_path = self._meta_path(ref.sha256)
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            return self._blob_path(ref.sha256, meta.get("ext", ""))
        return None

    def _read_meta(self, sha: str) -> dict[str, Any]:
        path = self._meta_path(sha)
        if not path.exists():
            raise ZeError(
                "asset_not_found",
                f"meta missing for asset {sha[:8]}...",
                context={"sha256": sha},
            )
        return json.loads(path.read_text())

    def _write_meta(self, sha: str, meta: dict[str, Any]) -> None:
        path = self._meta_path(sha)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False))
        os.replace(tmp, path)
