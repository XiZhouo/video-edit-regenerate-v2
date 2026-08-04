"""test_asset.py — AssetStore 行为测试.

覆盖:
  - AssetRef: 构造 / to_dict / from_dict / frozen
  - put / get / has: round-trip / 二进制保真 / 不存在 / dedup / checksum
  - put_file: 推 mime + ext / 二进制保真
  - 引用计数: add_ref / release / ref_count / release idempotent at 0
  - gc: refs=0 清 / 有 ref 留 / 空分片目录清 / 返回 sha 列表
  - all_refs: 列全部 (含 refs=0)
  - scan_entity_refs: 扫 entity.data 嵌套 / 多 ref / 无 ref / list 内嵌
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from asset import AssetRef, AssetStore

_DOCUMENT_DIR = Path(__file__).resolve().parent.parent / "01-document"
if str(_DOCUMENT_DIR) not in sys.path:
    sys.path.insert(0, str(_DOCUMENT_DIR))
from document import Document
from effect_context import producer_effect_phase
from entity import Actor, Entity, ZeError


def _doc() -> Document:
    return Document(schema_version="1.0.0", collaboration_mode="with_save")


# ==============================================================
# AssetRef
# ==============================================================

def test_asset_ref_construct():
    ref = AssetRef(sha256="abc", mime="image/png", size=100, ext=".png")
    assert ref.sha256 == "abc"
    assert ref.mime == "image/png"
    assert ref.size == 100
    assert ref.ext == ".png"


def test_asset_ref_defaults():
    ref = AssetRef(sha256="abc")
    assert ref.mime == "application/octet-stream"
    assert ref.size == 0
    assert ref.ext == ""


def test_asset_ref_frozen():
    ref = AssetRef(sha256="abc")
    with pytest.raises(Exception):
        ref.sha256 = "xyz"  # type: ignore[misc]


def test_asset_ref_to_dict():
    ref = AssetRef(sha256="abc", mime="image/png", size=100, ext=".png")
    d = ref.to_dict()
    assert d == {"sha256": "abc", "mime": "image/png", "size": 100, "ext": ".png"}


def test_asset_ref_from_dict_roundtrip():
    ref = AssetRef(sha256="abc", mime="image/png", size=100, ext=".png")
    restored = AssetRef.from_dict(ref.to_dict())
    assert restored == ref


def test_asset_ref_from_dict_tolerates_missing():
    """缺字段 fallback 默认值."""
    ref = AssetRef.from_dict({"sha256": "abc"})
    assert ref.mime == "application/octet-stream"
    assert ref.size == 0
    assert ref.ext == ""


# ==============================================================
# put / get / has
# ==============================================================

def test_put_returns_correct_ref(tmp_path: Path):
    store = AssetStore(tmp_path)
    data = b"hello world"
    expected_sha = hashlib.sha256(data).hexdigest()
    ref = store.put(data, mime="text/plain", ext=".txt")
    assert ref.sha256 == expected_sha
    assert ref.mime == "text/plain"
    assert ref.size == 11
    assert ref.ext == ".txt"


def test_put_get_roundtrip(tmp_path: Path):
    store = AssetStore(tmp_path)
    data = b"\x00\x01\x02 binary \xff\xfe"
    ref = store.put(data, mime="application/octet-stream")
    assert store.get(ref) == data


def test_get_missing_raises(tmp_path: Path):
    store = AssetStore(tmp_path)
    fake_ref = AssetRef(sha256="0" * 64)
    with pytest.raises(ZeError) as exc:
        store.get(fake_ref)
    assert exc.value.code == "asset_not_found"


def test_has_existing(tmp_path: Path):
    store = AssetStore(tmp_path)
    ref = store.put(b"hello", ext=".txt")
    assert store.has(ref) is True


def test_has_missing(tmp_path: Path):
    store = AssetStore(tmp_path)
    assert store.has(AssetRef(sha256="0" * 64)) is False


def test_physical_layout(tmp_path: Path):
    """blob 在 blobs/<sha[:2]>/<sha><ext>; meta 在 meta/<sha>.json."""
    store = AssetStore(tmp_path)
    ref = store.put(b"hello", mime="text/plain", ext=".txt")
    blob_path = tmp_path / "blobs" / ref.sha256[:2] / f"{ref.sha256}.txt"
    meta_path = tmp_path / "meta" / f"{ref.sha256}.json"
    assert blob_path.exists()
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text())
    assert meta == {"mime": "text/plain", "size": 5, "refs": 0, "ext": ".txt"}


# ==============================================================
# dedup
# ==============================================================

def test_put_dedup_same_bytes(tmp_path: Path):
    """同 bytes 两次 put → 同 sha, blob 只存一份."""
    store = AssetStore(tmp_path)
    data = b"hello"
    ref1 = store.put(data)
    ref2 = store.put(data)
    assert ref1.sha256 == ref2.sha256
    # blob 文件还在
    assert (tmp_path / "blobs" / ref1.sha256[:2] / ref1.sha256).exists()
    # 数量: blobs/<sha[:2]>/ 下只有 1 个文件
    shard_dir = tmp_path / "blobs" / ref1.sha256[:2]
    assert len(list(shard_dir.iterdir())) == 1


def test_put_dedup_preserves_existing_refs(tmp_path: Path):
    """已有 refs > 0 的 blob 再 put 不应重置 refs=0."""
    store = AssetStore(tmp_path)
    ref = store.put(b"hello")
    store.add_ref(ref)
    store.add_ref(ref)
    assert store.ref_count(ref) == 2
    # 第二次 put 同 bytes
    store.put(b"hello")
    assert store.ref_count(ref) == 2  # 保持


# ==============================================================
# checksum 防篡改
# ==============================================================

def test_get_detects_corruption(tmp_path: Path):
    """blob 被篡改 -> get 抛 asset_corrupted."""
    store = AssetStore(tmp_path)
    ref = store.put(b"hello", ext=".txt")
    # 篡改 blob
    blob_path = tmp_path / "blobs" / ref.sha256[:2] / f"{ref.sha256}.txt"
    blob_path.write_bytes(b"TAMPERED")
    with pytest.raises(ZeError) as exc:
        store.get(ref)
    assert exc.value.code == "asset_corrupted"


# ==============================================================
# put_file
# ==============================================================

def test_put_file_infers_mime_and_ext(tmp_path: Path):
    """put_file 推 mime + ext."""
    src = tmp_path / "src.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n fake png")
    store = AssetStore(tmp_path / "store")
    ref = store.put_file(src)
    assert ref.mime == "image/png"
    assert ref.ext == ".png"


def test_put_file_explicit_mime(tmp_path: Path):
    """caller 传 mime 覆盖自动推断."""
    src = tmp_path / "data.bin"
    src.write_bytes(b"bytes")
    store = AssetStore(tmp_path / "store")
    ref = store.put_file(src, mime="application/x-custom")
    assert ref.mime == "application/x-custom"


def test_put_file_unknown_extension_fallback(tmp_path: Path):
    """未知扩展名 mime 回退 application/octet-stream."""
    src = tmp_path / "thing.qqzzx"  # 几乎不可能被系统 mimetypes 识别
    src.write_bytes(b"weird")
    store = AssetStore(tmp_path / "store")
    ref = store.put_file(src)
    assert ref.mime == "application/octet-stream"
    assert ref.ext == ".qqzzx"


def test_put_file_no_extension(tmp_path: Path):
    """无 ext 的文件 ext=''."""
    src = tmp_path / "README"
    src.write_bytes(b"text")
    store = AssetStore(tmp_path / "store")
    ref = store.put_file(src)
    assert ref.ext == ""


def test_put_file_roundtrip_bytes(tmp_path: Path):
    """put_file 后 get 拿回原字节."""
    src = tmp_path / "src.bin"
    payload = bytes(range(256))  # 0..255 全字节
    src.write_bytes(payload)
    store = AssetStore(tmp_path / "store")
    ref = store.put_file(src)
    assert store.get(ref) == payload


# ==============================================================
# 引用计数
# ==============================================================

def test_ref_count_starts_at_zero(tmp_path: Path):
    store = AssetStore(tmp_path)
    ref = store.put(b"x")
    assert store.ref_count(ref) == 0


def test_add_ref_increments(tmp_path: Path):
    store = AssetStore(tmp_path)
    ref = store.put(b"x")
    assert store.add_ref(ref) == 1
    assert store.add_ref(ref) == 2
    assert store.add_ref(ref) == 3
    assert store.ref_count(ref) == 3


def test_release_decrements(tmp_path: Path):
    store = AssetStore(tmp_path)
    ref = store.put(b"x")
    store.add_ref(ref)
    store.add_ref(ref)
    assert store.release(ref) == 1
    assert store.release(ref) == 0


def test_release_idempotent_at_zero(tmp_path: Path):
    """refs=0 时 release 保持 0, 不抛错."""
    store = AssetStore(tmp_path)
    ref = store.put(b"x")
    assert store.release(ref) == 0
    assert store.release(ref) == 0
    assert store.release(ref) == 0


def test_release_unknown_ref_returns_zero(tmp_path: Path):
    """release 不存在的 ref -> 返回 0, 不抛错 (idempotent)."""
    store = AssetStore(tmp_path)
    fake = AssetRef(sha256="0" * 64)
    assert store.release(fake) == 0


def test_add_ref_unknown_raises(tmp_path: Path):
    """add_ref 必须先 put (meta 必存在); 否则抛 asset_not_found."""
    store = AssetStore(tmp_path)
    fake = AssetRef(sha256="0" * 64)
    with pytest.raises(ZeError) as exc:
        store.add_ref(fake)
    assert exc.value.code == "asset_not_found"


# ==============================================================
# GC
# ==============================================================

def test_gc_clears_refs_zero(tmp_path: Path):
    store = AssetStore(tmp_path)
    ref = store.put(b"x")
    removed = store.gc(_doc())
    assert removed == [ref.sha256]
    assert not store.has(ref)


def test_gc_ignores_stale_positive_legacy_ref_count(tmp_path: Path):
    """Legacy refs 不是回收授权；Document 不可达时先归零再清理。"""
    store = AssetStore(tmp_path)
    ref = store.put(b"x")
    store.add_ref(ref)
    removed = store.gc(_doc())
    assert removed == [ref.sha256]
    assert not store.has(ref)


def test_gc_multiple(tmp_path: Path):
    """多个 asset, 部分 refs=0, 部分有 refs."""
    store = AssetStore(tmp_path)
    keep = store.put(b"keep")
    drop1 = store.put(b"drop1")
    drop2 = store.put(b"drop2")
    store.add_ref(keep)
    doc = _doc()
    doc.add(Entity(type="obj", id="keep", data={"asset": keep.to_dict()}))
    removed = set(store.gc(doc))
    assert removed == {drop1.sha256, drop2.sha256}
    assert store.has(keep)
    assert not store.has(drop1)
    assert not store.has(drop2)


def test_gc_cleans_empty_shard_dirs(tmp_path: Path):
    """gc 后空的 blobs/<sha[:2]>/ 目录应清掉."""
    store = AssetStore(tmp_path)
    ref = store.put(b"x", ext=".txt")
    shard_dir = tmp_path / "blobs" / ref.sha256[:2]
    assert shard_dir.exists()
    store.gc(_doc())
    assert not shard_dir.exists()


def test_gc_meta_file_removed(tmp_path: Path):
    """gc 后 meta 文件也应删."""
    store = AssetStore(tmp_path)
    ref = store.put(b"x")
    meta_path = tmp_path / "meta" / f"{ref.sha256}.json"
    assert meta_path.exists()
    store.gc(_doc())
    assert not meta_path.exists()


def test_gc_without_document_fails_closed_and_keeps_blob(tmp_path: Path):
    """未知可达性不能当成零引用；无 Document 时文件与 meta 一字不动。"""
    store = AssetStore(tmp_path)
    ref = store.put(b"must-stay")
    meta_path = tmp_path / "meta" / f"{ref.sha256}.json"
    before = meta_path.read_bytes()

    with pytest.raises(ZeError) as exc:
        store.gc()

    assert exc.value.code == "asset_reachability_required"
    assert store.has(ref)
    assert meta_path.read_bytes() == before


def test_gc_reconciles_stale_zero_before_deciding(tmp_path: Path):
    """producer 不维护 refs 也安全：Document 真可达的 stale-zero blob 会先对账并保留。"""
    store = AssetStore(tmp_path)
    ref = store.put(b"reachable")
    doc = _doc()
    doc.add(Entity(type="obj", id="e1", data={"asset": ref.to_dict()}))
    assert store.ref_count(ref) == 0

    assert store.gc(doc) == []

    assert store.has(ref)
    assert store.ref_count(ref) == 1


def test_reconcile_uses_full_document_multiset(tmp_path: Path):
    """同 sha 多槽位、软删可恢复项、read_only overlay 都属于可达集合。"""
    store = AssetStore(tmp_path)
    ref = store.put(b"shared")
    doc = _doc()
    doc.add(Entity(
        type="obj", id="duplicate-slots",
        data={"hero": ref.to_dict(), "thumbnail": {"source": ref.to_dict()}},
    ))
    soft = Entity(type="obj", id="soft-deleted", data={"asset": ref.to_dict()})
    doc.add(soft)
    soft.delete(expected_version=soft.version, actor=Actor(user_id="u1"))
    doc.add(Entity(
        type="overlay", id="read-only-source", read_only=True,
        data={"asset": ref.to_dict()},
    ))

    counts = AssetStore.scan_document_ref_counts(doc)
    reconciled = store.reconcile_document_refs(doc)

    assert counts == {ref.sha256: 4}
    assert reconciled == {ref.sha256: 4}
    assert store.ref_count(ref) == 4
    assert store.gc(doc) == []


def test_reconcile_tracks_direct_update_duplicate_and_hard_remove(tmp_path: Path):
    """按终态扫描，不依赖 mutation 入口；import/duplicate/undo 类直改也能重新收敛。"""
    store = AssetStore(tmp_path)
    old = store.put(b"old")
    new = store.put(b"new")
    doc = _doc()
    original = Entity(type="obj", id="original", data={"asset": old.to_dict()})
    doc.add(original)
    # duplicate/import 可能不经 builtin handler；可达性仍看 Document 终态。
    doc.add(Entity(type="obj", id="duplicate", data={"asset": old.to_dict()}))
    assert store.reconcile_document_refs(doc) == {new.sha256: 0, old.sha256: 2}

    original.update(
        {"asset": new.to_dict()}, expected_version=original.version,
        actor=Actor(user_id="u1"),
    )
    doc.hard_remove("obj", "duplicate")

    assert store.reconcile_document_refs(doc) == {new.sha256: 1, old.sha256: 0}
    assert set(store.gc(doc)) == {old.sha256}
    assert store.has(new)


def test_reconcile_scan_failure_writes_nothing(tmp_path: Path):
    """Document 扫描不完整时 fail-closed，不把旧 refs 擅自改成 0。"""
    store = AssetStore(tmp_path)
    ref = store.put(b"safe")
    store.add_ref(ref)
    meta_path = tmp_path / "meta" / f"{ref.sha256}.json"
    before = meta_path.read_bytes()

    class BrokenDocument:
        def types(self):
            return ["obj"]

        def list(self, entity_type, *, include_deleted=False):
            raise RuntimeError("canonical snapshot unavailable")

    with pytest.raises(RuntimeError, match="snapshot unavailable"):
        store.gc(BrokenDocument())

    assert store.has(ref)
    assert meta_path.read_bytes() == before


def test_effectful_asset_maintenance_is_forbidden_in_producer(tmp_path: Path):
    """可回滚 producer 不能读任意文件、改引用账、promote 或 GC。"""
    shared = AssetStore(tmp_path / "shared")
    store = AssetStore(tmp_path / "local", fallback=shared)
    ref = store.put(b"local")
    source = tmp_path / "upload.bin"
    source.write_bytes(b"upload")
    doc = _doc()

    effects = [
        ("AssetStore.put_file", lambda: store.put_file(source)),
        ("AssetStore.promote", lambda: store.promote(ref)),
        ("AssetStore.add_ref", lambda: store.add_ref(ref)),
        ("AssetStore.release", lambda: store.release(ref)),
        ("AssetStore.reconcile_document_refs", lambda: store.reconcile_document_refs(doc)),
        ("AssetStore.gc", lambda: store.gc(doc)),
    ]
    for effect_name, call in effects:
        with producer_effect_phase(), pytest.raises(ZeError) as exc:
            call()
        assert exc.value.code == "producer_effect_forbidden"
        assert exc.value.context["effect"] == effect_name

    assert store.has(ref)
    assert shared.has(ref) is False
    assert store.ref_count(ref) == 0


def test_content_addressed_put_is_the_only_producer_prepare_exception(tmp_path: Path):
    """put(bytes) 只会留可去重 orphan，不会让 Document rollback 后形成误删/误保权威。"""
    store = AssetStore(tmp_path)
    with producer_effect_phase():
        ref = store.put(b"prepared-immutable-content")

    assert store.has(ref)
    assert store.ref_count(ref) == 0
    # 若业务提交失败，这只是不可达 orphan；后续 canonical GC 可安全回收。
    assert store.gc(_doc()) == [ref.sha256]


# ==============================================================
# all_refs
# ==============================================================

def test_all_refs_empty(tmp_path: Path):
    store = AssetStore(tmp_path)
    assert store.all_refs() == []


def test_all_refs_includes_zero_refs(tmp_path: Path):
    """all_refs 应包含 refs=0 pending GC 的."""
    store = AssetStore(tmp_path)
    ref1 = store.put(b"a")
    ref2 = store.put(b"b")
    store.add_ref(ref1)
    all_refs = store.all_refs()
    assert len(all_refs) == 2
    assert {r.sha256 for r in all_refs} == {ref1.sha256, ref2.sha256}


def test_all_refs_returns_complete_meta(tmp_path: Path):
    """all_refs 的 AssetRef 应含完整 mime / size / ext."""
    store = AssetStore(tmp_path)
    store.put(b"hello", mime="text/plain", ext=".txt")
    refs = store.all_refs()
    assert refs[0].mime == "text/plain"
    assert refs[0].size == 5
    assert refs[0].ext == ".txt"


def test_total_size_counts_unique_local_blobs_not_references(tmp_path: Path):
    store = AssetStore(tmp_path)
    first = store.put(b"abc", mime="text/plain", ext=".txt")
    store.put(b"abc", mime="text/plain", ext=".txt")
    store.put(b"12345", mime="text/plain", ext=".txt")
    store.add_ref(first)
    store.add_ref(first)
    assert store.total_size() == 8


def test_total_size_fails_closed_on_invalid_meta_size(tmp_path: Path):
    store = AssetStore(tmp_path)
    ref = store.put(b"abc")
    meta = store.meta_dir / f"{ref.sha256}.json"
    meta.write_text('{"mime":"x","size":"not-an-int","refs":0,"ext":""}')
    with pytest.raises(ZeError) as exc:
        store.total_size()
    assert exc.value.code == "asset_corrupted"


@pytest.mark.parametrize("declared_size", [2, 4])
def test_total_size_fails_closed_when_meta_size_disagrees_with_blob(
    tmp_path: Path, declared_size: int,
):
    store = AssetStore(tmp_path)
    ref = store.put(b"abc")
    meta_path = store.meta_dir / f"{ref.sha256}.json"
    meta = json.loads(meta_path.read_text())
    meta["size"] = declared_size
    meta_path.write_text(json.dumps(meta))

    with pytest.raises(ZeError) as exc:
        store.total_size()
    assert exc.value.code == "asset_corrupted"


def test_total_size_fails_closed_when_blob_is_missing(tmp_path: Path):
    store = AssetStore(tmp_path)
    ref = store.put(b"abc", ext=".txt")
    (store.blobs_dir / ref.sha256[:2] / f"{ref.sha256}.txt").unlink()

    with pytest.raises(ZeError) as exc:
        store.total_size()
    assert exc.value.code == "asset_corrupted"


def test_total_size_rejects_symlink_blob(tmp_path: Path):
    store = AssetStore(tmp_path)
    ref = store.put(b"abc")
    blob = store.blobs_dir / ref.sha256[:2] / ref.sha256
    blob.unlink()
    target = tmp_path / "outside.bin"
    target.write_bytes(b"abc")
    blob.symlink_to(target)

    with pytest.raises(ZeError) as exc:
        store.total_size()
    assert exc.value.code == "asset_corrupted"


def test_total_size_rejects_noncanonical_meta_filename(tmp_path: Path):
    store = AssetStore(tmp_path)
    (store.meta_dir / "not-a-sha.json").write_text(
        '{"mime":"x","size":0,"refs":0,"ext":""}'
    )

    with pytest.raises(ZeError) as exc:
        store.total_size()
    assert exc.value.code == "asset_corrupted"


# ==============================================================
# scan_entity_refs
# ==============================================================

def test_scan_entity_refs_finds_top_level(tmp_path: Path):
    """entity.data 顶层 dict 含 sha256 -> 识别为 AssetRef."""
    e = Entity(
        type="obj",
        data={"image": {"sha256": "a" * 64, "mime": "image/png", "size": 100, "ext": ".png"}},
    )
    refs = AssetStore.scan_entity_refs(e)
    assert len(refs) == 1
    assert refs[0].sha256 == "a" * 64
    assert refs[0].mime == "image/png"


def test_scan_entity_refs_nested(tmp_path: Path):
    """嵌套在 list / dict 里的 AssetRef 也能扫到."""
    e = Entity(
        type="obj",
        data={
            "layers": [
                {"image": {"sha256": "a" * 64, "mime": "image/png"}},
                {"image": {"sha256": "b" * 64, "mime": "image/jpeg"}},
            ],
            "thumbnail": {"sha256": "c" * 64},
        },
    )
    refs = AssetStore.scan_entity_refs(e)
    assert {r.sha256 for r in refs} == {"a" * 64, "b" * 64, "c" * 64}


def test_scan_entity_refs_no_refs(tmp_path: Path):
    """无 AssetRef 字典返回空 list."""
    e = Entity(type="obj", data={"name": "x", "count": 42})
    assert AssetStore.scan_entity_refs(e) == []


def test_scan_entity_refs_ignores_short_sha(tmp_path: Path):
    """sha256 字段不足 64 字符不识别为 AssetRef (避免误判普通字段)."""
    e = Entity(
        type="obj",
        data={"fake": {"sha256": "tooshort"}},
    )
    assert AssetStore.scan_entity_refs(e) == []


def test_scan_entity_refs_walks_into_non_ref_dict(tmp_path: Path):
    """非 ref dict 仍递归 (e.g. wrapper 里嵌真 ref)."""
    e = Entity(
        type="obj",
        data={
            "config": {
                "nested": {
                    "image": {"sha256": "a" * 64, "mime": "image/png"},
                },
            },
        },
    )
    refs = AssetStore.scan_entity_refs(e)
    assert len(refs) == 1
    assert refs[0].sha256 == "a" * 64


# ==============================================================
# 跟 Document cascade_delete on_asset_unref 集成 (smoke test)
# ==============================================================

def test_cascade_release_hook_pattern(tmp_path: Path):
    """模拟 caller 把 store.release 接进 cascade_delete on_asset_unref."""
    store = AssetStore(tmp_path)
    img_data = b"\x89PNG fake"
    img_ref = store.put(img_data, mime="image/png", ext=".png")
    store.add_ref(img_ref)
    assert store.ref_count(img_ref) == 1

    # 模拟 cascade_delete 调 hook
    e = Entity(type="obj", data={"image": img_ref.to_dict()})

    def on_unref(d: dict) -> None:
        store.release(AssetRef.from_dict(d))

    # 找 e.data 里的 ref dict 然后调 hook (这是 document.cascade_delete 内部行为)
    for v in e.data.values():
        if isinstance(v, dict) and "sha256" in v:
            on_unref(v)

    assert store.ref_count(img_ref) == 0
    removed = store.gc(_doc())
    assert img_ref.sha256 in removed


# ==============================================================
# 共享池 fallback(P3)
# ==============================================================

def test_fallback_none_is_regression(tmp_path):
    """无 fallback → 行为与改前一致:本地缺失 get 抛 asset_not_found、has False。"""
    store = AssetStore(tmp_path / "p")
    ghost = AssetRef(sha256="0" * 64, mime="image/png", size=1, ext=".png")
    assert store.has(ghost) is False
    with pytest.raises(ZeError) as ei:
        store.get(ghost)
    assert ei.value.code == "asset_not_found"


def test_fallback_read_delegation(tmp_path):
    """本地缺、共享有 → get/has/ref_for 经 fallback 解析(blob 不可变 + sha 自校验)。"""
    shared = AssetStore(tmp_path / "shared")
    local = AssetStore(tmp_path / "p", fallback=shared)
    ref = shared.put(b"shared-blob", mime="image/png", ext=".png")
    # 本地没有,但经 fallback 可读
    assert local.is_local(ref) is False
    assert local.has(ref) is True
    assert local.get(ref) == b"shared-blob"
    assert local.ref_for(ref.sha256) is not None


def test_fallback_writes_stay_local(tmp_path):
    """put/add_ref/release/gc 只动本地 —— per-project 引用语义不碰共享池。"""
    shared = AssetStore(tmp_path / "shared")
    local = AssetStore(tmp_path / "p", fallback=shared)
    ref = local.put(b"local-blob", mime="image/png", ext=".png")
    assert local.is_local(ref) is True
    assert shared.has(ref) is False              # 写没漏进共享池
    local.add_ref(ref)
    assert local.ref_count(ref) == 1
    assert shared.ref_count(ref) == 0            # 引用计数不串台


def test_promote_copies_into_shared(tmp_path):
    shared = AssetStore(tmp_path / "shared")
    local = AssetStore(tmp_path / "p", fallback=shared)
    ref = local.put(b"promote-me", mime="image/png", ext=".png")
    assert shared.has(ref) is False
    out = local.promote(ref)
    assert out.sha256 == ref.sha256
    assert shared.has(ref) is True
    assert shared.get(ref) == b"promote-me"


def test_promote_without_fallback_errors(tmp_path):
    store = AssetStore(tmp_path / "p")
    ref = store.put(b"x", mime="image/png", ext=".png")
    with pytest.raises(ZeError) as ei:
        store.promote(ref)
    assert ei.value.code == "asset_no_shared_pool"


def test_scan_doc_refs_and_cross_project_guard(tmp_path):
    """scan_doc_refs 收齐 doc 内所有引用 sha;跨项目护栏 = is_local OR referenced。"""
    shared = AssetStore(tmp_path / "shared")
    shared_ref = shared.put(b"shared", mime="image/png", ext=".png")

    # 鸭子类型 doc:只需 types() + list(type, include_deleted=)
    class _E:
        def __init__(self, data): self.data = data
    class _Doc:
        def __init__(self, m): self._m = m
        def types(self): return list(self._m)
        def list(self, et, include_deleted=False): return self._m[et]

    referencing = _Doc({"img": [_E({"thumb": {"sha256": shared_ref.sha256, "ext": ".png"}})]})
    other = _Doc({"img": [_E({"note": "no refs here"})]})

    assert AssetStore.scan_doc_refs(referencing) == {shared_ref.sha256}
    assert AssetStore.scan_doc_refs(other) == set()

    # 护栏判定:共享 blob(非本地)在引用方放行、在未引用方拦下
    proj_a = AssetStore(tmp_path / "a", fallback=shared)
    assert proj_a.is_local(shared_ref) is False
    allow_a = proj_a.is_local(shared_ref) or shared_ref.sha256 in AssetStore.scan_doc_refs(referencing)
    block_b = proj_a.is_local(shared_ref) or shared_ref.sha256 in AssetStore.scan_doc_refs(other)
    assert allow_a is True and block_b is False


def test_scan_entity_refs_recurses_past_top_level_sha():
    """dict 同时是 AssetRef 又内嵌别的 ref → 两个都要扫到(护栏不漏算)。"""
    A, B = "a" * 64, "b" * 64
    class _E:
        data = {"sha256": A, "mime": "image/png", "thumbnail": {"sha256": B, "mime": "image/png"}}
    shas = {r.sha256 for r in AssetStore.scan_entity_refs(_E())}
    assert shas == {A, B}


def test_local_ref_for_does_not_touch_fallback(tmp_path):
    """local_ref_for 只看本地 → 共享 sha 返 None(护栏据此本地优先、不泄露共享存在性)。"""
    shared = AssetStore(tmp_path / "shared")
    local = AssetStore(tmp_path / "p", fallback=shared)
    sref = shared.put(b"shared")
    assert local.local_ref_for(sref.sha256) is None    # 不委托
    assert local.ref_for(sref.sha256) is not None       # ref_for 才委托


def test_scan_doc_refs_includes_soft_deleted(tmp_path):
    """scan_doc_refs 必须用 include_deleted=True —— 软删实体仍引用的共享 blob 不能被误拒。"""
    sha = "c" * 64
    seen_flag = {}
    class _E:
        def __init__(self, d): self.data = d
    class _Doc:
        def types(self): return ["img"]
        def list(self, et, include_deleted=False):
            seen_flag["v"] = include_deleted
            return [_E({"sha256": sha, "mime": "image/png"})]
    assert AssetStore.scan_doc_refs(_Doc()) == {sha}
    assert seen_flag["v"] is True
