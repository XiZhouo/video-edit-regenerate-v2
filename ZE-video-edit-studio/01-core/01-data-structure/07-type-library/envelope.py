"""envelope — 信封 .ze-pack 导出导入(互通 P0;SPEC §8)。

公共壳(ze.manifest.<domain>.v1):
  schema / source{skill,doc_id,doc_name,exported_at,core_version} / coordinate_space /
  units{time:seconds,length:px,angle:deg} / entity_types{桶名:{schema(resolved 含注解),archetype}}
  / entities[{id,type,name,data}] / assets[{sha256,mime,size,ext}] (+ 域专属块)。
  **version/actor_log/provenance 不出境**(宿主私史,importer 重建)。

打包:zip = manifest.json + blobs/<sha[:2]>/<sha><ext>(与 AssetStore 物理布局逐字节同构
→ 导入即 put 零重排);裸 manifest.json 亦合法(无资产时)。

导入五步(normative):版本闸 → 逐 blob put(sha 校验) → id 冲突换新+重映射 → 重写 $ref 与
graph_node.deps(包外悬挂引用**清除**+warning——**AP-02 红线:信封是拷贝语义,不产生跨文档
活引用**;清除落地=键/元素移除,validator 不容裸 null;必填引用被清的实体降级丢弃+warning)
→ doc.add 自动过 validator(校验即合规线)。整次导入 = 一个 Operation N 个 create patch +
对称 reverse → **一键 undo**。

七域 v1(规划六域 + graph 补遗,包格式须装得下自家全部 7 个 preset):vision(标注互通)/
timeline(一条成片一包)/ scene(一关卡一包)/ layers(锚上内联栈 ⇄ layer 实体 1:1 机械转换)/
text(body 内 asset://<sha> 约定)/ dataset(结果行 blob 同包)/ graph(节点图)。
域只影响推断与 layers 转换,公共壳对全域同形。

失败模式:manifest_version_unsupported / manifest_blob_corrupt / manifest_domain_unresolved /
manifest_type_undeclared / manifest_singleton_conflict / manifest_malformed。
"""

from __future__ import annotations

import copy
import io
import json
import re
import sys
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ENTITY_DIR = Path(__file__).resolve().parent.parent / "02-entity"
_PROTOCOL_DIR = Path(__file__).resolve().parent.parent.parent / "_protocol"
for _directory in (_ENTITY_DIR, _PROTOCOL_DIR):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

from entity import Entity, ZeError  # noqa: E402
from effect_context import assert_external_effect_allowed  # noqa: E402

import type_library as zt  # noqa: E402
from fragments import ARCHETYPES, DOC_PRESETS  # noqa: E402

ENVELOPE_VERSION = 1
# 解压上限(防 zip 炸弹耗尽内存;按声明尺寸先验再读)。容量叶子管 entity/asset 落库面,
# 这里管「解包瞬间」的内存面。已知行为:导入中途校验失败时,步 2 已入库的 blob 成为
# 无引用孤儿——内容寻址 + 引用计数使其可被 AssetStore 回收,不算数据损坏。
MAX_BLOB_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
# 七域 = 规划六域 + graph(补遗:T3 有 node_graph preset,包格式必须能装下自家全部 preset)
DOMAINS = ("vision", "timeline", "scene", "layers", "text", "dataset", "graph")
_SCHEMA_RE = re.compile(r"^ze\.manifest\.([a-z][a-z0-9_]*)\.v(\d+)$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_EXPORT_NAME_RE = re.compile(
    r"^[0-9]{8}-[0-9]{6}-(?:vision|timeline|scene|layers|text|dataset|graph)-[0-9a-f]{6}\.ze-pack$"
)
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

# 域推断:archetype 注解 → 域(按特异度排序;layers 须显式指定——同为 scene 锚,意图不可推)
_ARCHETYPE_DOMAIN = (
    ("annotation", "vision"),
    ("clip", "timeline"),
    ("track", "timeline"),
    ("placed_object", "scene"),
    ("chapter", "text"),
    ("data_query", "dataset"),
    ("graph_node", "graph"),
    ("scene", "scene"),
)


def _archetype_short(schema: dict) -> str | None:
    m = re.match(r"^ze\.archetype\.([a-z][a-z0-9_]*)@\d+$", (schema or {}).get("x-ze-archetype") or "")
    return m.group(1) if m else None


def infer_domain(entity_types: dict[str, dict]) -> str | None:
    shorts = {_archetype_short(d.get("schema") or {}) for d in entity_types.values()}
    for short, domain in _ARCHETYPE_DOMAIN:
        if short in shorts:
            return domain
    return None


# ==============================================================
# 资产收集(递归走 data 找 asset_ref 形状的 dict)
# ==============================================================

def _walk_asset_refs(value: Any, out: dict[str, dict]) -> None:
    if isinstance(value, dict):
        sha = value.get("sha256")
        if isinstance(sha, str) and _SHA_RE.match(sha):
            meta = {"sha256": sha}
            for k in ("mime", "size", "ext"):
                if k in value:
                    meta[k] = value[k]
            out.setdefault(sha, meta)
        for v in value.values():
            _walk_asset_refs(v, out)
    elif isinstance(value, list):
        for v in value:
            _walk_asset_refs(v, out)


# ==============================================================
# layers 域:锚上内联栈 ⇄ layer 实体(1:1 机械转换)
# ==============================================================

def _expand_layers(entities: list[dict], entity_types: dict[str, dict]) -> None:
    """导出侧:scene 锚的 data.layers[] 展开为 layer 实体(order=栈位);锚甩掉内联栈。"""
    extra: list[dict] = []
    for ent in entities:
        decl = entity_types.get(ent["type"]) or {}
        if _archetype_short(decl.get("schema") or {}) != "scene":
            continue
        stack = ent["data"].pop("layers", None) or []
        ent["data"].pop("active_layer", None)
        for i, item in enumerate(stack):
            item = dict(item)
            lid = item.pop("id", None) or uuid.uuid4().hex
            lname = item.pop("name", "")
            item["order"] = i
            extra.append({"id": lid, "type": "layer", "name": lname, "data": item})
    if extra:
        entity_types.setdefault("layer", {
            "schema": zt.archetype("layer"),
            "archetype": "ze.archetype.layer@1",
        })
        entities.extend(extra)


def _normalize_layer_order(entities: list[dict], entity_types: dict[str, dict]) -> None:
    """导出侧:layer 桶按 order 排序后重写为连续整数(多端并发 reorder 可能留下重复/空洞;
    信封是交换格式,栈位必须无歧义)。稳定排序:order 同值按 id 兜底。"""
    layer_buckets = {name for name, decl in entity_types.items()
                     if _archetype_short(decl.get("schema") or {}) == "layer"}
    for bucket in layer_buckets:
        rows = [e for e in entities if e["type"] == bucket]
        rows.sort(key=lambda e: (e["data"].get("order", 0), e["id"]))
        for i, e in enumerate(rows):
            e["data"]["order"] = i


def _fold_layers(entities: list[dict], entity_types: dict[str, dict],
                 doc: Any, warnings: list[str]) -> None:
    """导入侧:目标 doc 未声明 layer 桶时,layer 实体折回锚的 data.layers(按 order)。"""
    if "layer" in (doc.schema or {}):
        return   # 目标自己收 layer 实体
    layer_ents = [e for e in entities if e["type"] == "layer"]
    if not layer_ents:
        return
    anchors = [e for e in entities
               if _archetype_short((entity_types.get(e["type"]) or {}).get("schema") or {}) == "scene"]
    if len(anchors) != 1:
        warnings.append(f"layers 折回跳过:锚 {len(anchors)} 个(须恰 1),layer 实体被丢弃")
        entities[:] = [e for e in entities if e["type"] != "layer"]
        return
    stack = []
    for e in sorted(layer_ents, key=lambda x: x["data"].get("order", 0)):
        item = dict(e["data"])
        item.pop("order", None)
        item["id"] = e["id"]
        if e.get("name"):
            item["name"] = e["name"]
        stack.append(item)
    anchors[0]["data"]["layers"] = stack
    entities[:] = [e for e in entities if e["type"] != "layer"]
    entity_types.pop("layer", None)


# ==============================================================
# 导出
# ==============================================================

def export_manifest(doc: Any, asset_store: Any, *, skill_id: str = "",
                    core_version: str = "", domain: str | None = None,
                    coordinate_space: str = "pixel_top_left") -> tuple[dict, dict[str, bytes]]:
    """组装 manifest dict + {sha: blob bytes}。缺 blob 留 warning 不中断(meta 仍出境)。"""
    entity_types: dict[str, dict] = {}
    for name, schema in (doc.schema or {}).items():
        entity_types[name] = {
            "schema": copy.deepcopy(schema),
            "archetype": (schema or {}).get("x-ze-archetype"),
        }
    entities: list[dict] = []
    # 只走 schema 声明桶:候选(06-candidate)等框架内部桶不出境;软删/只读源同样不出境
    # (拷贝语义只带活的本档数据)。
    for et in sorted(entity_types):
        for ent in doc.list(et):
            if getattr(ent, "read_only", False):
                continue
            entities.append({
                "id": ent.id, "type": ent.type, "name": ent.name,
                "data": copy.deepcopy(ent.data),
            })

    if domain is None:
        domain = infer_domain(entity_types)
    if domain not in DOMAINS:
        raise ZeError(
            "manifest_domain_unresolved",
            f"域无法推断(实体无库注解),显式传 domain ∈ {DOMAINS}",
            context={"domain": domain},
        )
    if domain == "layers":
        _expand_layers(entities, entity_types)
        _normalize_layer_order(entities, entity_types)

    asset_meta: dict[str, dict] = {}
    for ent in entities:
        _walk_asset_refs(ent["data"], asset_meta)
    blobs: dict[str, bytes] = {}
    warnings: list[str] = []
    for sha, meta in sorted(asset_meta.items()):
        try:
            from asset import AssetRef  # noqa: E402 (叶子路径已注入)
            blobs[sha] = asset_store.get(AssetRef(sha256=sha, ext=meta.get("ext", "")))
        except Exception:
            warnings.append(f"asset {sha[:12]}… 不在本地 store,blob 未入包(meta 保留)")

    manifest = {
        "schema": f"ze.manifest.{domain}.v{ENVELOPE_VERSION}",
        "source": {
            "skill": skill_id,
            "doc_id": getattr(doc, "id", ""),
            "doc_name": getattr(doc, "name", ""),
            "exported_at": time.time(),
            "core_version": core_version,
        },
        "coordinate_space": coordinate_space,
        "units": {"time": "seconds", "length": "px", "angle": "deg"},
        "entity_types": entity_types,
        "entities": entities,
        "assets": [asset_meta[s] for s in sorted(asset_meta)],
    }
    if warnings:
        manifest["warnings"] = warnings
    return manifest, blobs


@dataclass(frozen=True)
class _ExportSnapshot:
    """命令 producer 捕获的不可变导出输入。

    manifest 先序列化为 bytes，blob 冻结为有序 ``(sha, bytes)`` tuple；提交后动作
    每次执行都从同一份值快照重建输入，不再读取仍会变化的 Document/AssetStore。
    """

    manifest_json: bytes
    blobs: tuple[tuple[str, bytes], ...]

    @classmethod
    def capture(cls, manifest: dict, blobs: dict[str, bytes]) -> "_ExportSnapshot":
        manifest_json = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return cls(
            manifest_json=manifest_json,
            blobs=tuple((sha, bytes(data)) for sha, data in sorted(blobs.items())),
        )

    def thaw(self) -> tuple[dict, dict[str, bytes]]:
        return json.loads(self.manifest_json.decode("utf-8")), dict(self.blobs)


def _deterministic_zip_info(name: str) -> zipfile.ZipInfo:
    """固定 zip 元数据，使同一 snapshot 的重跑产生逐字节相同的包。"""
    info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o600 << 16
    return info


def write_pack(manifest: dict, blobs: dict[str, bytes], path: Path) -> Path:
    """确定性 zip 落盘；相同输入重跑到同一路径时内容逐字节相同。"""
    assert_external_effect_allowed("manifest.export.write_pack")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ext_by_sha = {a["sha256"]: a.get("ext", "") for a in manifest.get("assets", [])}
    # 每次尝试独占自己的 partial；同一 action 被 repair/请求线程并发重跑时不会
    # 互相截断临时文件，最终只原子换入同一份确定性内容。
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            manifest_bytes = json.dumps(
                manifest,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            zf.writestr(_deterministic_zip_info("manifest.json"), manifest_bytes)
            for sha, data in sorted(blobs.items()):
                name = f"blobs/{sha[:2]}/{sha}{ext_by_sha.get(sha, '')}"
                zf.writestr(_deterministic_zip_info(name), data)
        tmp.replace(path)   # 原子换入:下载路由不持 doc 锁,绝不能读到半截 zip
    except BaseException:
        # 失败只留下 ActionReceipt，不把半包伪装成可下载件，也不积累 partial 垃圾。
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return path


# ==============================================================
# 导入(五步 normative)
# ==============================================================

def read_pack(source: bytes | str | Path) -> tuple[dict, dict[str, bytes]]:
    """读 .ze-pack(zip)或裸 manifest.json(bytes/路径)→ (manifest, {sha: bytes})。"""
    raw = Path(source).read_bytes() if isinstance(source, (str, Path)) else source
    blobs: dict[str, bytes] = {}
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            try:
                mi = zf.getinfo("manifest.json")
            except KeyError:
                raise ZeError("manifest_malformed", "zip 内缺 manifest.json") from None
            if mi.file_size > MAX_MANIFEST_BYTES:
                raise ZeError("manifest_malformed",
                              f"manifest.json 声明 {mi.file_size}B 超上限(防解压炸弹)")
            manifest = json.loads(zf.read(mi))
            for info in zf.infolist():
                m = re.match(r"^blobs/[0-9a-f]{2}/([0-9a-f]{64})(\.[A-Za-z0-9.]+)?$", info.filename)
                if m:
                    if info.file_size > MAX_BLOB_BYTES:
                        raise ZeError(
                            "manifest_malformed",
                            f"blob {m.group(1)[:12]}… 声明 {info.file_size}B 超上限(防解压炸弹)",
                        )
                    blobs[m.group(1)] = zf.read(info)
    else:
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise ZeError("manifest_malformed", f"既不是 zip 也不是 JSON: {e}") from None
    if not isinstance(manifest, dict):
        raise ZeError("manifest_malformed", "manifest 顶层必须是 object")
    return manifest, blobs


def import_manifest(doc: Any, asset_store: Any, manifest: dict,
                    blobs: dict[str, bytes] | None = None, *,
                    rename: dict[str, str] | None = None,
                    ) -> tuple[list[Entity], list[str]]:
    """五步导入;返回 (新建的 Entity 列表, warnings)。doc.add 已发生(校验即合规线)。

    rename: {包内桶名: 目标桶名}(Pilot B 一行映射:{"image": "source"})。
    本函数只做数据落位;Operation 组装在 register_manifest_commands 的命令层。
    """
    blobs = blobs or {}
    warnings: list[str] = list(manifest.get("warnings") or [])

    # 步 1:版本闸(主版本不符 → 结构化拒绝,不静默降级)
    m = _SCHEMA_RE.match(manifest.get("schema") or "")
    if not m:
        raise ZeError("manifest_malformed",
                      f"schema 字段非法: {manifest.get('schema')!r}")
    domain, ver = m.group(1), int(m.group(2))
    if domain not in DOMAINS or ver != ENVELOPE_VERSION:
        raise ZeError(
            "manifest_version_unsupported",
            f"ze.manifest.{domain}.v{ver} 不受支持(本端支持 v{ENVELOPE_VERSION},域 {DOMAINS})",
            context={"domain": domain, "version": ver},
        )

    entity_types = {k: dict(v) for k, v in (manifest.get("entity_types") or {}).items()}
    entities = [dict(e, data=copy.deepcopy(e.get("data") or {}))
                for e in (manifest.get("entities") or [])]

    # 桶名映射(rename)先行,再做声明检查
    for old, new in (rename or {}).items():
        if old in entity_types:
            entity_types[new] = entity_types.pop(old)
        for ent in entities:
            if ent["type"] == old:
                ent["type"] = new

    # layers 域折回(目标无 layer 桶时)
    if domain == "layers":
        _fold_layers(entities, entity_types, doc, warnings)

    declared = set((doc.schema or {}).keys())
    undeclared = sorted({e["type"] for e in entities} - declared)
    if undeclared:
        raise ZeError(
            "manifest_type_undeclared",
            f"目标 doc 未声明 {undeclared}(用 rename 映射到本端桶名,或先在 mod declare)",
            context={"undeclared": undeclared, "declared": sorted(declared)},
        )

    # 单例锚守恒(粒度法 B:导入后每个 singleton 桶仍恰 ≤1)
    for bucket in {e["type"] for e in entities}:
        short = _archetype_short((doc.schema or {}).get(bucket) or {})
        if short and ARCHETYPES.get(short, {}).get("singleton"):
            incoming = sum(1 for e in entities if e["type"] == bucket)
            existing = len([x for x in doc.list(bucket) if not getattr(x, "deleted", False)]) \
                if hasattr(doc, "list") else 0
            if incoming + existing > 1:
                raise ZeError(
                    "manifest_singleton_conflict",
                    f"{bucket} 是单例锚:导入 {incoming} 个 + 已有 {existing} 个 > 1"
                    "(一作品=一 document;另开项目再导入)",
                    context={"bucket": bucket, "incoming": incoming, "existing": existing},
                )

    # 步 2:逐 blob 入库(put 重算 sha=免费校验;与包名 sha 不符 → 拒)
    declared_assets = {a["sha256"]: a for a in (manifest.get("assets") or [])}
    for sha, data in sorted(blobs.items()):
        ref = asset_store.put(data, ext=declared_assets.get(sha, {}).get("ext", ""))
        if ref.sha256 != sha:
            raise ZeError(
                "manifest_blob_corrupt",
                f"blob {sha[:12]}… 内容 sha={ref.sha256[:12]}… 与包名不符",
                context={"claimed": sha, "actual": ref.sha256},
            )
    from asset import AssetRef  # noqa: E402 (叶子路径已注入)
    for sha in sorted(declared_assets.keys() - blobs.keys()):
        ref = AssetRef(sha256=sha, ext=declared_assets[sha].get("ext", ""))
        if not asset_store.has(ref):
            warnings.append(f"asset {sha[:12]}… 包内无 blob 且本地无,引用将悬空指向缺失资产")

    # 步 3:id 冲突换新 + 重映射表
    remap: dict[str, str] = {}   # "type/oldid" -> newid
    pack_ids = {f'{e["type"]}/{e["id"]}' for e in entities}
    for ent in entities:
        old = ent["id"]
        exists = hasattr(doc, "find") and doc.find(ent["type"], old) is not None
        if exists:
            ent["id"] = uuid.uuid4().hex
            remap[f'{ent["type"]}/{old}'] = ent["id"]

    # 步 4:重写 $ref 与 graph_node.deps;包外悬挂 → 清除 + warning(拷贝语义红线)。
    # deps 裸 id 按**本桶**重映射(不同桶同名裸 id 不串台;$ref 走全 "type/id" 键无此问题)。
    remap_by_bucket: dict[str, dict[str, str]] = {}
    for old, new in remap.items():
        t, oid = old.split("/", 1)
        remap_by_bucket.setdefault(t, {})[oid] = new

    nulled: set[str] = set()   # 清除过悬挂引用的实体(必填 ref 被清 → 步 5 降级丢弃)
    _DANGLING = object()       # 哨兵:validator 不容 None,「置空」落地=键/元素移除

    def _rewrite(value: Any, ent_key: str) -> Any:
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str) and "/" in ref:
                if ref in remap:
                    t = ref.split("/", 1)[0]
                    return {**value, "$ref": f"{t}/{remap[ref]}"}
                # rename 后包内 id 集合按新桶名比对
                if ref not in pack_ids:
                    warnings.append(f"{ent_key}: 包外悬挂引用 {ref} 已清除(信封是拷贝语义)")
                    nulled.add(ent_key)
                    return _DANGLING
                return value
            return {k: out for k, v in value.items()
                    if (out := _rewrite(v, ent_key)) is not _DANGLING}
        if isinstance(value, list):
            return [out for v in value if (out := _rewrite(v, ent_key)) is not _DANGLING]
        return value

    graph_buckets = {b for b in {e["type"] for e in entities}
                     if _archetype_short((doc.schema or {}).get(b) or {}) == "graph_node"}
    for ent in entities:
        ent["data"] = _rewrite(ent["data"], f'{ent["type"]}/{ent["id"]}')
        if ent["type"] in graph_buckets and isinstance(ent["data"].get("deps"), list):
            bucket_map = remap_by_bucket.get(ent["type"], {})
            ent["data"]["deps"] = [bucket_map.get(d, d) for d in ent["data"]["deps"]]

    # 步 5:doc.add(validator 自动注入,校验即合规线)。
    # 唯一的宽恕:必填 ref 被步 4 置 null 的实体降级丢弃 + warning(不拖垮整次导入);
    # 其余 schema_violation 原样抛(真校验失败不吞)。
    created: list[Entity] = []
    for ent in entities:
        key = f'{ent["type"]}/{ent["id"]}'
        e = Entity(type=ent["type"], data=ent["data"], name=ent.get("name", ""), id=ent["id"])
        try:
            doc.add(e)
        except ZeError as exc:
            if exc.code == "schema_violation" and key in nulled:
                warnings.append(f"{key}: 必填引用悬挂被置 null,实体整体丢弃({exc})")
                continue
            raise
        created.append(e)
    return created, warnings


# ==============================================================
# Broker — 自描述包协商层(cross-skill 互通;设计见 plans/cross-skill-interop-broker-design.md)
# 诚实核心 = L0 原始资产 + 窄 L2 整原型精确匹配。L1 单角色收割实际坍缩进 L2(必填闸
# document.py add 注入目标 validator + 簇共享一个 x-ze-use);AP-03:singleton 锚子物不可收进外来锚。
# 全部逻辑在新函数 + 命令层;import_manifest 五步核心零改动(opportunistic 走「过滤+重命名子集→严格导入」)。
# ==============================================================

def _required_field_set(schema: dict) -> set[str]:
    """目标桶必填字段集(双风格:父 required 数组 ∪ 字段级 required:True;镜像 field_types._validate_object)。"""
    req = set((schema or {}).get("required") or [])
    for k, sub in ((schema or {}).get("properties") or {}).items():
        if isinstance(sub, dict) and sub.get("required") is True:
            req.add(k)
    return req


def _required_ref_fields(schema: dict) -> dict[str, str | None]:
    """必填且为 entity_ref 的字段 → 其 to: 约束(可 None)。用于 $ref 闭包判定(设计 §3.3)。"""
    props = (schema or {}).get("properties") or {}
    out: dict[str, str | None] = {}
    for k in _required_field_set(schema):
        sub = props.get(k) or {}
        if isinstance(sub, dict) and sub.get("type") == "entity_ref":
            out[k] = sub.get("to")
    return out


def _collect_roles(schema: dict) -> list[str]:
    """桶在场的 x-ze-use 角色词汇(field/cluster id;去重排序)。"""
    roles: set[str] = set()
    for sub in ((schema or {}).get("properties") or {}).values():
        if isinstance(sub, dict):
            u = sub.get("x-ze-use")
            if isinstance(u, str):
                roles.add(u)
    return sorted(roles)


def _anchor_bound_archetypes() -> set[str]:
    """从 DOC_PRESETS 派生「锚子物」原型短名:出现在带 singleton 的 preset、非锚本身、非 asset_item
    (asset_item 自含媒体无锚依赖)。设计 §3.4 / AP-03。当前 = {placed_object, layer, clip, track}。"""
    out: set[str] = set()
    for preset in DOC_PRESETS.values():
        singletons = set(preset.get("singleton") or [])
        if not singletons:
            continue
        ent = preset.get("entity_types") or {}
        singleton_archs = {ent[b] for b in singletons if b in ent}
        for bucket, arch_short in ent.items():
            if bucket in singletons or arch_short == "asset_item" or arch_short in singleton_archs:
                continue
            out.add(arch_short)
    return out


def derive_accept_profile(doc: Any) -> dict:
    """运行时从 doc.schema 派生 accept_profile.buckets(不存储;设计 §4)。"""
    buckets: dict[str, dict] = {}
    for bname, schema in (doc.schema or {}).items():
        schema = schema or {}
        short = _archetype_short(schema)
        buckets[bname] = {
            "archetype": schema.get("x-ze-archetype"),
            "required": sorted(_required_field_set(schema)),
            "singleton": bool(ARCHETYPES.get(short, {}).get("singleton")) if short else False,
            "roles": _collect_roles(schema),
        }
    return {"skill": getattr(doc, "name", "") or "", "buckets": buckets}


def compute_ingest_proposal(manifest: dict, doc: Any) -> dict:
    """broker 撮合:包 P × 目标 doc.schema → ingest_proposal(设计 §5)。

    诚实核心:L0 资产永远可收;L2 整原型精确匹配(archetype 全等)且 ① 非锚子物(AP-03)
    ② 目标非 ref 必填 present ③ 必填 ref 闭包全可收(fixpoint,防 §3.3 自毁)。其余进 drop(带理由)。
    纯只读、零副作用——不落库、不动 doc。
    """
    profile = derive_accept_profile(doc)
    tgt_by_arch: dict[str, str] = {}
    for bn, b in profile["buckets"].items():
        if b["archetype"]:
            tgt_by_arch.setdefault(b["archetype"], bn)   # 首个目标桶赢
    anchor_bound = _anchor_bound_archetypes()

    src_types = manifest.get("entity_types") or {}
    entities = manifest.get("entities") or []
    assets = [a.get("sha256") for a in (manifest.get("assets") or []) if a.get("sha256")]

    # 1. 桶级裁定:src 桶 → 目标桶 / 丢弃理由
    bucket_map: dict[str, str] = {}
    bucket_drop: dict[str, str] = {}
    for sb, decl in src_types.items():
        sschema = decl.get("schema") or {"x-ze-archetype": decl.get("archetype")}
        arch = sschema.get("x-ze-archetype") or decl.get("archetype")
        if not arch:
            bucket_drop[sb] = "unannotated_source"
            continue
        # AP-03 先于 undeclared:锚子物在 P0 一律不收(无坐标重基/锚共入),哪怕目标恰好声明了该原型
        if _archetype_short(sschema) in anchor_bound:
            bucket_drop[sb] = "foreign_anchor_child"
            continue
        tgt = tgt_by_arch.get(arch)
        if tgt is None:
            bucket_drop[sb] = "undeclared"
            continue
        bucket_map[sb] = tgt

    # 2. 单例守恒:映射到目标 singleton 桶且 incoming+existing>1 → 该桶整体冲突(设计 §3.4 / AP-03)
    for sb, tb in list(bucket_map.items()):
        if profile["buckets"][tb]["singleton"]:
            incoming = sum(1 for e in entities if e["type"] == sb)
            existing = (len([x for x in doc.list(tb) if not getattr(x, "deleted", False)])
                        if hasattr(doc, "list") else 0)
            if incoming + existing > 1:
                bucket_drop[sb] = "anchor_singleton_conflict"
                del bucket_map[sb]

    # 3. L2 实体候选:目标非 ref 必填 present
    cand: dict[str, dict] = {}     # "sb/id" -> entity
    fail_required: set[str] = set()
    for e in entities:
        sb = e["type"]
        if sb not in bucket_map:
            continue
        tschema = (doc.schema or {}).get(bucket_map[sb]) or {}
        nonref_required = _required_field_set(tschema) - set(_required_ref_fields(tschema))
        if any(f not in (e.get("data") or {}) for f in nonref_required):
            fail_required.add(f'{sb}/{e["id"]}')
            continue
        cand[f'{sb}/{e["id"]}'] = e

    # 4. 必填 ref 闭包 fixpoint:必填 ref 目标须也在 cand(键用包内 src "type/id");否则 unmet,可级联
    changed = True
    while changed:
        changed = False
        for key in list(cand.keys()):
            e = cand[key]
            tschema = (doc.schema or {}).get(bucket_map[e["type"]]) or {}
            ok = True
            for f in _required_ref_fields(tschema):
                val = (e.get("data") or {}).get(f)
                ref = val.get("$ref") if isinstance(val, dict) else None
                if not (isinstance(ref, str) and ref in cand):
                    ok = False
                    break
            if not ok:
                fail_required.add(key)
                del cand[key]
                changed = True

    # 5. L1 收割(对未 L2 接受的实体):携带 when_role 的实体 → into_bucket,角色值落目标角色字段 + supply
    harvest: dict[str, dict] = {}
    for e in entities:
        key = f'{e["type"]}/{e["id"]}'
        if key in cand or key in harvest:
            continue
        sprops = ((src_types.get(e["type"]) or {}).get("schema") or {}).get("properties") or {}
        for rule in (getattr(doc, "harvest_rules", None) or []):
            when_role, into_bucket = rule["when_role"], rule["into_bucket"]
            sf = next((k for k, sub in sprops.items()
                       if isinstance(sub, dict) and sub.get("x-ze-use") == when_role), None)
            if sf is None or sf not in (e.get("data") or {}):
                continue
            tprops = ((doc.schema or {}).get(into_bucket) or {}).get("properties") or {}
            tf = next((k for k, sub in tprops.items()
                       if isinstance(sub, dict) and sub.get("x-ze-use") == when_role), None)
            if tf is None:
                continue
            harvest[key] = {
                "src_bucket": e["type"], "src_id": e["id"], "into_bucket": into_bucket,
                "match": "harvest_rule", "required_closure": [],
                "name": e.get("name", ""),
                "harvest_data": {tf: copy.deepcopy(e["data"][sf]),
                                 **copy.deepcopy(rule.get("supply_required") or {})},
            }
            break

    # 6. accept(L2 archetype_exact + L1 harvest_rule)+ drop(既非 L2 也非收割)
    accept = []
    for e in cand.values():
        tb = bucket_map[e["type"]]
        closure = []
        for f in _required_ref_fields((doc.schema or {}).get(tb) or {}):
            val = (e.get("data") or {}).get(f)
            ref = val.get("$ref") if isinstance(val, dict) else None
            if isinstance(ref, str):
                closure.append(ref)
        accept.append({"src_bucket": e["type"], "src_id": e["id"], "into_bucket": tb,
                       "match": "archetype_exact", "required_closure": sorted(set(closure))})
    accept.extend(harvest.values())

    drop = []
    for e in entities:
        key = f'{e["type"]}/{e["id"]}'
        if key in cand or key in harvest:
            continue
        reason = "required_field_unmet" if key in fail_required else bucket_drop.get(e["type"], "undeclared")
        drop.append({"src_bucket": e["type"], "src_id": e["id"], "reason": reason})

    level = ("L2" if any(a["match"] == "archetype_exact" for a in accept)
             else "L1" if harvest else "L0")
    return {
        "pack_schema": manifest.get("schema", ""),
        "level": level,
        "accept": accept,
        "drop": drop,
        "assets": assets,
        "warnings": list(manifest.get("warnings") or []),
    }


def filter_manifest_to_proposal(manifest: dict, blobs: dict[str, bytes] | None,
                                proposal: dict) -> tuple[dict, dict[str, bytes]]:
    """据 proposal.accept 产「已重命名+过滤」子集 manifest + blob 子集,可直接交严格 import_manifest
    (五步核心零改动复用)。重写 ent.type 与 data 内 $ref 的 type 段为目标桶名;assets 限 accept 闭包。"""
    blobs = blobs or {}
    accept = proposal.get("accept") or []
    arch = [a for a in accept if a.get("match") == "archetype_exact"]   # L2:整桶重命名 + 复制
    harv = [a for a in accept if a.get("match") == "harvest_rule"]      # L1:角色值新建实体
    bmap = {a["src_bucket"]: a["into_bucket"] for a in arch}
    keep_ids = {f'{a["src_bucket"]}/{a["src_id"]}' for a in arch}

    src_types = manifest.get("entity_types") or {}
    new_types = {tb: copy.deepcopy(src_types[sb]) for sb, tb in bmap.items() if sb in src_types}
    for a in harv:   # 收割落点桶补最小 entity_types 占位(import 据 doc.schema 校验,占位仅占键)
        new_types.setdefault(a["into_bucket"], {"schema": {}, "archetype": None})

    def _rew_ref(value: Any) -> Any:
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str) and "/" in ref:
                t, i = ref.split("/", 1)
                if t in bmap:
                    return {**{k: _rew_ref(v) for k, v in value.items()}, "$ref": f"{bmap[t]}/{i}"}
            return {k: _rew_ref(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_rew_ref(v) for v in value]
        return value

    new_entities = []
    for e in manifest.get("entities") or []:
        if f'{e["type"]}/{e["id"]}' not in keep_ids:
            continue
        new_entities.append({"id": e["id"], "type": bmap[e["type"]], "name": e.get("name", ""),
                             "data": _rew_ref(copy.deepcopy(e.get("data") or {}))})
    seen_keys = {(e["type"], e["id"]) for e in new_entities}
    for a in harv:   # 收割实体:新建,只带角色值 + supply 常量,落 into_bucket(不带源其他字段)
        eid = a["src_id"]
        if (a["into_bucket"], eid) in seen_keys:   # 与 L2/其他收割撞 (桶,id) → 换新 id
            eid = uuid.uuid4().hex                  # 收割物是叶子资产、无入边 $ref,换 id 安全(防 import 自撞硬错)
        seen_keys.add((a["into_bucket"], eid))
        new_entities.append({"id": eid, "type": a["into_bucket"], "name": a.get("name", ""),
                             "data": copy.deepcopy(a.get("harvest_data") or {})})

    used: dict[str, dict] = {}
    for e in new_entities:
        _walk_asset_refs(e["data"], used)
    new_assets = [a for a in (manifest.get("assets") or []) if a.get("sha256") in used]
    new_blobs = {sha: b for sha, b in blobs.items() if sha in used}

    new_manifest = {
        **{k: manifest[k] for k in ("schema", "source", "coordinate_space", "units") if k in manifest},
        "entity_types": new_types,
        "entities": new_entities,
        "assets": new_assets,
    }
    return new_manifest, new_blobs


def proposal_for_pack(source: bytes | str | Path, doc: Any) -> dict:
    """读一个 .ze-pack(或裸 manifest bytes/路径)+ 目标 doc → ingest_proposal(纯只读)。

    P1 活源回报的撮合原语(设计 §7):让「只读源」据提案决定从对方导出目录收哪些桶 —— 一个工具能开
    活面板看「别的工具产出里我能用的子集」。下游真接线(declare_read_only_source 指向对方导出 + 据此
    过滤)仍是逐 skill 集成,需真实消费者对;本函数把框架侧撮合原语备齐。"""
    manifest, _ = read_pack(source)
    return compute_ingest_proposal(manifest, doc)


# ==============================================================
# P1 只读源物化(互通第二级:直读对方导出;SPEC §8)
# ==============================================================

def make_pack_loader(path: str | Path, entity_type: str, *,
                     asset_store: Any = None,
                     rename: dict[str, str] | None = None):
    """造 ReadOnlySourceSpec.loader:读 .ze-pack(或目录内**最新**一包)出指定桶的实体 dict。

    - path 指目录 → 按文件名取最新 .ze-pack(导出名带时间戳,字典序即时间序);
    - 包/目录缺失 → 返回空(工具照常启动,源为空视图——跨 skill 的导出可能尚未发生);
    - blobs 灌 asset_store(内容寻址幂等;传共享池则多项目共享一份);
    - rename 同 import_manifest(包桶名→本端桶名)。
    """
    p = Path(path)

    def _resolve() -> Path | None:
        if p.is_dir():
            packs = sorted(p.glob("*.ze-pack"))
            return packs[-1] if packs else None
        return p if p.is_file() else None

    def _loader():
        f = _resolve()
        if f is None:
            return []
        manifest, blobs = read_pack(f)
        if asset_store is not None:
            for data in blobs.values():     # 内容寻址幂等,顺序无关
                asset_store.put(data)
        out = []
        ren = rename or {}
        for ent in manifest.get("entities") or []:
            et = ren.get(ent.get("type"), ent.get("type"))
            if et == entity_type:
                out.append({"id": ent.get("id"), "name": ent.get("name", ""),
                            "data": copy.deepcopy(ent.get("data") or {})})
        return out

    return _loader


def materialize_read_only_sources(doc: Any, declared: list[dict], *,
                                  base_dir: Path, asset_store: Any = None) -> list[str]:
    """把 DocumentRegistry 声明的只读源物化进已打开的 doc(bootstrap 每次装载调=会话视图)。

    刷新购销:源**拥有其桶的只读面**——先物理清掉桶内既有 read_only 实体(上次物化的持久化
    残影,含已从源消失的过期项),再经 loader 灌当前包内容 → 视图永远跟随源文件现状。
    据此每桶至多一个源(两个源喂同一桶时,后者的购销会吞掉前者刚灌的实体——结构化拒绝,
    不静默丢数据)。幂等:同一 doc 实例重物化先 unregister 再 register(将来项目上下文
    缓存复用 doc 也安全);purge 先于全部 register,源处理顺序不影响结果。
    """
    from document import ReadOnlySourceSpec  # noqa: E402 (叶子路径已注入)
    buckets = [s["entity_type"] for s in declared]
    dup = sorted({b for b in buckets if buckets.count(b) > 1})
    if dup:
        raise ZeError(
            "validation_failed",
            f"只读源桶冲突:{dup} 被多个源声明(每桶至多一个源——源拥有其桶的只读面)",
            context={"duplicated_entity_types": dup},
        )
    # 对 Document 内存态提供整个刷新批次的原子边界。loader/AssetStore 的外部副作用
    # 不在此快照内，仍须保持幂等；但任何源失败都不会留下“旧面已清、新面只灌一半”的 doc。
    session = doc.begin_mutation_session()
    try:
        # 阶段 1:全桶购销(在任何 register 之前,源间顺序不影响结果)。必须包含
        # soft-deleted 残影，否则隐藏的旧 overlay 会和同 id 新行冲突。
        for et in set(buckets):
            for e in list(doc.list(et, include_deleted=True)):
                if getattr(e, "read_only", False):
                    doc.hard_remove(et, e.id)
        # 阶段 2:逐源注册(unregister 先行 → 同 doc 实例重物化幂等)
        done: list[str] = []
        for spec in declared:
            et = spec["entity_type"]
            lp = Path(spec["loader_path"])
            if not lp.is_absolute():
                lp = Path(base_dir) / lp
            doc.unregister_read_only_source(spec["name"])
            doc.register_read_only_source(ReadOnlySourceSpec(
                name=spec["name"], entity_type=et,
                loader=make_pack_loader(lp, et, asset_store=asset_store),
            ))
            done.append(spec["name"])
    except BaseException as original_error:
        try:
            session.rollback()
        except BaseException as rollback_error:
            # Keep the triggering loader/validation failure as the direct
            # causal fact; expose rollback failure in stable, serializable
            # context instead of burying it two levels down __context__.
            raise ZeError(
                "mutation_rollback_failed",
                "read-only source refresh failed and Document rollback also failed",
                context={
                    "original_error_type": type(original_error).__name__,
                    "original_error_code": getattr(original_error, "code", None),
                    "rollback_error_type": type(rollback_error).__name__,
                    "rollback_error_code": getattr(rollback_error, "code", None),
                    "restore_error_code": getattr(
                        rollback_error, "context", {}
                    ).get("restore_error_code"),
                },
            ) from original_error
        raise
    else:
        session.commit()
    return done


# ==============================================================
# 命令接线(bootstrap.build_project_context 调;镜像 register_pipeline_commands 闭包注入)
# ==============================================================

def register_manifest_commands(registry: Any, *, asset_store: Any, exports_dir: Path,
                               skill_id: str = "", core_version: str = "") -> None:
    """注册 manifest.export / manifest.import 两个 framework command。"""
    from command import (  # noqa: E402 (叶子路径已注入)
        CommandMutation,
        CommandSpec,
        DeferredAction,
    )
    from operation import Patch, new_operation, ENTITY_META_FIELD, external_api  # noqa: E402

    def _export(args: dict[str, Any], actor: Any, source: str, doc: Any):
        manifest, blobs = export_manifest(
            doc, asset_store, skill_id=skill_id, core_version=core_version,
            domain=(args or {}).get("domain"),
        )
        domain = manifest["schema"].split(".")[2]
        snapshot = _ExportSnapshot.capture(manifest, blobs)
        # producer 只预分配白名单文件名；真正落盘只能由提交后动作完成。
        fname = f"{time.strftime('%Y%m%d-%H%M%S')}-{domain}-{uuid.uuid4().hex[:6]}.ze-pack"
        if _EXPORT_NAME_RE.fullmatch(fname) is None:  # 防未来 domain/格式改动意外放开路径字符
            raise ZeError(
                "manifest_export_name_invalid",
                "生成的导出文件名未通过框架白名单",
                context={"domain": domain},
            )
        output_path = Path(exports_dir) / fname
        operation = new_operation(
            command_id="manifest.export", source=source, actor=actor.to_dict(),
            patches=[], reverse_patches=[],   # 零 patch:导出不改文档
            external=external_api(
                "manifest.export",
                target_ref={"file": fname, "schema": manifest["schema"],
                            "entities": str(len(manifest["entities"])),
                            "assets": str(len(blobs))},
                # 这里仅记录“已计划”；Operation 落库时文件还不存在，不能伪称已导出。
                status="pending",
            ),
        )

        def _write_after_commit(_commit_id: str) -> Path:
            frozen_manifest, frozen_blobs = snapshot.thaw()
            return write_pack(frozen_manifest, frozen_blobs, output_path)

        return CommandMutation(
            operations=(operation,),
            after_commit=(DeferredAction(
                id=f"manifest.export.write:{operation.id}",
                name="manifest.export.write_pack",
                run=_write_after_commit,
            ),),
        )

    def _read_pack_arg(args: dict[str, Any], *, source: str) -> bytes:
        # path 是宿主内部文件能力，不是交互参数。即使同时给了 asset，也先拒绝越权 path，
        # 避免调用者靠参数优先级把任意服务器路径偷偷带过边界。
        if args.get("path") is not None and source != "system":
            raise ZeError(
                "manifest_path_forbidden",
                "args.path 仅允许 source=system；user/ai 请先上传并传 args.asset",
                context={"source": source},
            )
        if args.get("asset") is not None:
            from asset import AssetRef  # noqa: E402
            a = args["asset"]
            if (
                not isinstance(a, dict)
                or not isinstance(a.get("sha256"), str)
                or _SHA_RE.fullmatch(a["sha256"]) is None
                or not isinstance(a.get("ext", ""), str)
            ):
                raise ZeError(
                    "manifest_asset_invalid",
                    "args.asset 必须是含合法 sha256 的 asset_ref",
                )
            return asset_store.get(AssetRef(sha256=a["sha256"], ext=a.get("ext", "")))
        if args.get("path") is not None:
            return Path(args["path"]).read_bytes()
        raise ZeError("validation_failed", "需要 args.asset(asset_ref)或 args.path")

    def _preview(args: dict[str, Any], actor: Any, source: str, doc: Any):
        manifest, _ = read_pack(_read_pack_arg(args or {}, source=source))
        proposal = compute_ingest_proposal(manifest, doc)
        return new_operation(
            command_id="manifest.preview", source=source, actor=actor.to_dict(),
            patches=[], reverse_patches=[],   # 零 patch:只读预览
            external=external_api(
                "manifest.preview",
                target_ref={"schema": manifest.get("schema", ""),
                            "level": proposal["level"],
                            "accept": str(len(proposal["accept"])),
                            "drop": str(len(proposal["drop"])),
                            "proposal": json.dumps(proposal, ensure_ascii=False)},
            ),
        )

    def _import(args: dict[str, Any], actor: Any, source: str, doc: Any):
        args = args or {}
        manifest, blobs = read_pack(_read_pack_arg(args, source=source))
        extra: list[str] = []
        if (args.get("mode") or "strict") == "opportunistic":
            # 算提案 → 过滤+重命名子集 → 严格 import_manifest 跑子集(五步核心零改动)
            proposal = compute_ingest_proposal(manifest, doc)
            manifest, blobs = filter_manifest_to_proposal(manifest, blobs, proposal)
            extra = list(proposal.get("warnings") or []) + [
                f"opportunistic[{proposal['level']}]: 收 {len(proposal['accept'])} / 丢 {len(proposal['drop'])}"]
            created, warnings = import_manifest(doc, asset_store, manifest, blobs)
        else:
            created, warnings = import_manifest(
                doc, asset_store, manifest, blobs, rename=args.get("rename"),
            )
        warnings = extra + warnings
        patches, reverses = [], []
        for e in created:
            snap = e.to_dict()
            patches.append(Patch(e.type, e.id, ENTITY_META_FIELD, "create", None, snap))
            reverses.append(Patch(e.type, e.id, ENTITY_META_FIELD, "delete", snap, None))
        return new_operation(
            command_id="manifest.import", source=source, actor=actor.to_dict(),
            patches=patches, reverse_patches=reverses,   # 单 Operation → 整次导入一键 undo
            external=external_api(
                "manifest.import",
                target_ref={"schema": manifest.get("schema", ""),
                            "created": str(len(created)),
                            "warnings": str(len(warnings))},
            ),
        )

    registry.register(CommandSpec(
        id="manifest.export", label="导出 .ze-pack",
        sources_allowed=("user", "ai", "system"), execute=_export,
    ))
    registry.register(CommandSpec(
        id="manifest.preview", label="预览 .ze-pack 可收子集",
        sources_allowed=("user", "ai", "system"), execute=_preview,
    ))
    registry.register(CommandSpec(
        id="manifest.import", label="导入 .ze-pack",
        sources_allowed=("user", "system"), execute=_import,
    ))
