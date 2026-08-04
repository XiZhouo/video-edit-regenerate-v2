"""type_library — 引用组合 resolver + zt API 本体。

完整 SPEC 见同目录 SPEC.md;内容本体在 fragments.py(纯字面量);词表见 TYPE-REGISTRY.md。

核心裁决(SPEC §5):解析发生在 declare_entity_type **之前**(本模块函数内)——
DocumentRegistry / field_types validator / 编辑器全部零改动。落盘的 data_schema 是
**平铺 resolved dict + x-ze-* 注解**:Document.open 直接建 validator(schema 随文档冻结,
旧项目不依赖库当前版本);注解让门禁/export 机器识别引用(x-* 键 validator 不读,已逐键验证)。

注解词表:
  x-ze-use        字段级 provenance("ze.field.label@1" / "ze.cluster.run@1")
  x-ze-archetype  实体级 provenance("ze.archetype.clip@1")
  x-ze-ext        mod 扩展字段标记(True)
  x-ze-sealed     封印字段名列表(GATE-TYPE-09 比对锚)
  x-ze-collab-unit 原型默认 collaboration_unit(apply_preset 读)

失败模式(mod register 即炸,可读):
  type_fragment_unknown / type_version_mismatch / type_extension_violation / type_seal_violation
"""

from __future__ import annotations

import copy
import difflib
import re
import sys
from pathlib import Path
from typing import Any

# 共享 ZeError: 从兄弟目录 02-entity import(镜像 field_types.py 的注入法)
_ENTITY_DIR = Path(__file__).resolve().parent.parent / "02-entity"
if str(_ENTITY_DIR) not in sys.path:
    sys.path.insert(0, str(_ENTITY_DIR))

from entity import ZeError  # noqa: E402

from fragments import ARCHETYPES, CLUSTERS, DOC_PRESETS, FIELDS  # noqa: E402

# zt.field 的 extra 白名单(纯 cosmetic;约束键出现 → type_extension_violation)
COSMETIC_KEYS = ("label", "x-group", "step", "default")

# narrow / GATE-09 比对时忽略的键(外观与注解,不影响数据合法性)
_APPEARANCE_KEYS = set(COSMETIC_KEYS) | {"x-ze-use", "x-ze-ext"}

_TIERS = {"field": FIELDS, "cluster": CLUSTERS, "archetype": ARCHETYPES, "doc": DOC_PRESETS}
_ID_RE = re.compile(r"^(?:ze\.(field|cluster|archetype|doc)\.)?([a-z][a-z0-9_]*)(?:@(\d+))?$")


# ==============================================================
# id 解析(三写法等价: "bbox_xyxy" | "ze.field.bbox_xyxy" | "…@1")
# ==============================================================

def _parse_id(fid: str, tier: str) -> tuple[str, dict]:
    """解析 id → (short, entry)。tier 不符 / 未知 / 版本不符 → 结构化异常。"""
    m = _ID_RE.match(fid or "")
    if not m:
        raise ZeError("type_fragment_unknown", f"malformed type id {fid!r}",
                      context={"id": fid, "tier": tier})
    id_tier, short, ver = m.group(1), m.group(2), m.group(3)
    if id_tier and id_tier != tier:
        raise ZeError(
            "type_fragment_unknown",
            f"{fid!r} is tier {id_tier!r}, expected {tier!r}",
            context={"id": fid, "tier": tier, "got_tier": id_tier},
        )
    table = _TIERS[tier]
    entry = table.get(short)
    if entry is None:
        hint = difflib.get_close_matches(short, list(table), n=3)
        raise ZeError(
            "type_fragment_unknown",
            f"unknown ze.{tier}.{short!r}" + (f"; 近似候选: {hint}" if hint else ""),
            context={"id": fid, "tier": tier, "candidates": hint},
        )
    if ver is not None and int(ver) != entry["version"]:
        raise ZeError(
            "type_version_mismatch",
            f"ze.{tier}.{short} 当前版本 @{entry['version']},请求 @{ver}",
            context={"id": fid, "current": entry["version"], "requested": int(ver)},
        )
    return short, entry


def _full_id(tier: str, short: str) -> str:
    return f"ze.{tier}.{short}@{_TIERS[tier][short]['version']}"


# ==============================================================
# tighten-only 校验(narrow 与 GATE-TYPE-09 共用一条代码路径)
# ==============================================================

def strip_appearance(schema: dict) -> dict:
    """剔除外观与注解键(narrow 比对 / GATE-TYPE-09 sealed 全等比对共用)。"""
    return {k: v for k, v in schema.items() if k not in _APPEARANCE_KEYS}


def assert_tightens(base: dict, new: dict, *, path: str) -> None:
    """new 必须是 base 的合法收紧(同 type;min 只升 max 只降;enum 子集;required 只能加)。

    不通过抛 type_extension_violation。比对忽略外观键(label/x-group/step/default/注解)。
    """
    b, n = strip_appearance(base), strip_appearance(new)

    def _bad(reason: str) -> ZeError:
        return ZeError("type_extension_violation",
                       f"{path}: {reason}(只紧不松)",
                       context={"path": path, "base": b, "new": n})

    if n.get("type") != b.get("type"):
        raise _bad(f"type 不得改({b.get('type')!r}→{n.get('type')!r})")
    for key in n.keys() | b.keys():
        if key in ("type", "required"):
            continue
        bv, nv = b.get(key), n.get(key)
        if bv == nv:
            continue
        if key in ("minimum", "minLength", "minItems", "exclusiveMinimum"):
            if bv is not None and (nv is None or nv < bv):
                raise _bad(f"{key} 只能升")
        elif key in ("maximum", "maxLength", "maxItems", "exclusiveMaximum"):
            if bv is not None and (nv is None or nv > bv):
                raise _bad(f"{key} 只能降")
        elif key == "enum":
            if bv is not None and (nv is None or not set(nv) <= set(bv)):
                raise _bad("enum 只能取子集")
        elif key == "uniqueItems":
            if not (bv in (None, False) and nv is True):
                raise _bad("uniqueItems 只能 False→True")
        else:
            raise _bad(f"约束键 {key!r} 不得改")
    # required: True→缺省 = 放松,禁止;缺省→True = 收紧,允许
    if b.get("required") is True and n.get("required") is not True:
        raise _bad("required 不得撤销")


# ==============================================================
# T1: field / cluster 解析
# ==============================================================

def _check_extra_cosmetic(extra: dict, *, where: str) -> None:
    bad = [k for k in extra if k not in COSMETIC_KEYS]
    if bad:
        raise ZeError(
            "type_extension_violation",
            f"{where}: 非 cosmetic 键 {bad}(只许 {list(COSMETIC_KEYS)};约束改动走库版本)",
            context={"where": where, "bad_keys": bad},
        )


def field(fid: str, *, label: str | None = None, required: bool | None = None,
          **extra: Any) -> dict:
    """单字段基元 → resolved schema dict(深拷贝+x-ze-use 戳)。

    多字段平铺基元(point2d/size2d/transform2d/time_range)请用 cluster()。
    extra 只许 cosmetic 键(COSMETIC_KEYS)。
    """
    short, entry = _parse_id(fid, "field")
    if len(entry["fields"]) != 1:
        raise ZeError(
            "type_extension_violation",
            f"ze.field.{short} 是平铺组({list(entry['fields'])}),用 zt.cluster() 展开",
            context={"id": fid, "fields": list(entry["fields"])},
        )
    _check_extra_cosmetic(extra, where=f"zt.field({fid!r})")
    schema = copy.deepcopy(next(iter(entry["fields"].values())))
    if label is not None:
        schema["label"] = label
    if required is not None:
        schema["required"] = required
    schema.update(extra)
    schema["x-ze-use"] = _full_id("field", short)
    return schema


def cluster(cid: str, *, required: list[str] | None = None) -> dict[str, dict]:
    """平铺组 → {属性名: resolved schema}。

    接受:三具名簇(run/audio/rect)、多字段基元(point2d/size2d/transform2d/time_range)、
    可平铺对象基元(prompt_block → 其 properties)。required 按属性名点名必填。
    """
    tier = "cluster" if cid.startswith("ze.cluster.") or _bare_in(cid, CLUSTERS) else "field"
    short, entry = _parse_id(cid, tier)
    fields = entry["fields"]
    if tier == "field" and len(fields) == 1:
        only = next(iter(fields.values()))
        props = only.get("properties") if isinstance(only, dict) else None
        if only.get("type") == "object" and props:
            fields = props   # 对象基元平铺(prompt_block)
        else:
            raise ZeError(
                "type_extension_violation",
                f"ze.field.{short} 是单字段,用 zt.field()",
                context={"id": cid},
            )
    out: dict[str, dict] = {}
    for name, sub in fields.items():
        if isinstance(sub, dict) and "$use" in sub:
            inner = {k: v for k, v in sub.items() if k != "$use"}
            out[name] = field(sub["$use"], **inner) if not _has_constraint(inner) \
                else _internal_field(sub["$use"], inner)
        else:
            resolved = copy.deepcopy(sub)
            resolved["x-ze-use"] = _full_id(tier, short)
            out[name] = resolved
    for name in (required or []):
        if name not in out:
            raise ZeError(
                "type_extension_violation",
                f"required 点名 {name!r} 不在 ze.{tier}.{short} 的属性 {list(out)} 内",
                context={"id": cid, "required": required},
            )
        out[name]["required"] = True
    return out


def _bare_in(fid: str, table: dict) -> bool:
    m = _ID_RE.match(fid or "")
    return bool(m and not m.group(1) and m.group(2) in table)


def _has_constraint(overrides: dict) -> bool:
    return any(k not in (*COSMETIC_KEYS, "required") for k in overrides)


def _internal_field(fid: str, overrides: dict) -> dict:
    """库内部组合用:允许 required/to 等落点键(mod 面的 zt.field 仍 cosmetic-only)。"""
    short, entry = _parse_id(fid, "field")
    schema = copy.deepcopy(next(iter(entry["fields"].values())))
    schema.update(overrides)
    schema["x-ze-use"] = _full_id("field", short)
    return schema


# ==============================================================
# T2: archetype 解析
# ==============================================================

def _resolve_archetype_props(short: str, entry: dict) -> dict[str, dict]:
    props: dict[str, dict] = {}
    for key, val in entry["fields"].items():
        if isinstance(val, dict) and "$use" in val:
            overrides = {k: v for k, v in val.items() if k != "$use"}
            resolved = _internal_field(val["$use"], overrides)
            _check_pinned(val["$use"], key, where=f"ze.archetype.{short}")
            props[key] = resolved
        elif isinstance(val, dict) and "$cluster" in val:
            req = val.get("required")
            expanded = cluster(val["$cluster"], required=req)
            for name, sub in expanded.items():
                if name in props:
                    raise ZeError(
                        "type_extension_violation",
                        f"ze.archetype.{short}: $cluster {val['$cluster']!r} 展开的 "
                        f"{name!r} 与已有属性冲突",
                        context={"archetype": short, "key": name},
                    )
                props[name] = sub
        else:
            props[key] = copy.deepcopy(val)
    return props


def _check_pinned(fid: str, mounted: str, *, where: str) -> None:
    m = _ID_RE.match(fid)
    entry = FIELDS.get(m.group(2)) if m else None
    pinned = (entry or {}).get("pinned_name")
    if pinned and mounted != pinned and not mounted.startswith("x_"):
        raise ZeError(
            "type_extension_violation",
            f"{where}: {fid} 的落点字段名钉死 {pinned!r},不得挂为 {mounted!r}",
            context={"id": fid, "pinned": pinned, "mounted": mounted},
        )


def archetype(aid: str, *, extend: dict[str, Any] | None = None,
              narrow: dict[str, dict] | None = None,
              override_labels: dict[str, str] | None = None,
              seal: bool = False) -> dict:
    """实体原型 → resolved entity schema(declare_entity_type 的 schema 实参)。

    extend: 新字段(键必须 x_ 前缀;值=literal schema 或 {"$use": fid});碰撞→违例。
    narrow: 仅非 sealed 字段、只紧不松(assert_tightens)。
    override_labels: 纯外观改名。seal=True 把 extend 的字段也并进 x-ze-sealed。
    """
    short, entry = _parse_id(aid, "archetype")
    props = _resolve_archetype_props(short, entry)
    sealed = list(entry["sealed"])

    for name, val in (extend or {}).items():
        if name in props:
            raise ZeError(
                "type_extension_violation",
                f"extend {name!r} 与 ze.archetype.{short} 既有字段碰撞",
                context={"archetype": short, "key": name},
            )
        if not name.startswith("x_"):
            raise ZeError(
                "type_extension_violation",
                f"extend 键 {name!r} 必须 x_ 前缀(加性 only;库属字段走库版本)",
                context={"archetype": short, "key": name},
            )
        if isinstance(val, dict) and "$use" in val:
            resolved = field(val["$use"], **{k: v for k, v in val.items() if k != "$use"})
        else:
            resolved = copy.deepcopy(val)
        resolved["x-ze-ext"] = True
        props[name] = resolved
        if seal:
            sealed.append(name)

    for name, tighter in (narrow or {}).items():
        if name not in props:
            raise ZeError(
                "type_extension_violation",
                f"narrow 目标 {name!r} 不在 ze.archetype.{short} 字段内",
                context={"archetype": short, "key": name},
            )
        fake = [k for k in tighter if k.startswith("x-ze-")]
        if fake:
            raise ZeError(
                "type_extension_violation",
                f"narrow {name!r} 不得携带注解键 {fake}(provenance 由库盖戳,不得伪造)",
                context={"archetype": short, "key": name, "bad_keys": fake},
            )
        if name in entry["sealed"]:
            raise ZeError(
                "type_seal_violation",
                f"ze.archetype.{short}.{name} 是 sealed 字段,narrow 也不行",
                context={"archetype": short, "key": name},
            )
        merged = {**props[name], **tighter}
        assert_tightens(props[name], merged, path=f"{short}.{name}")
        props[name] = merged

    for name, text in (override_labels or {}).items():
        if name in props:
            props[name]["label"] = text

    return {
        "type": "object",
        "label": entry["label"],
        "properties": props,
        "x-ze-archetype": _full_id("archetype", short),
        "x-ze-sealed": sealed,
        "x-ze-collab-unit": entry["collab_unit"],
    }


# ==============================================================
# T3: doc preset
# ==============================================================

def doc_preset(pid: str) -> dict:
    """文档组合 preset → {entity_types, collaboration_mode, singleton, layout_hint}。"""
    short, entry = _parse_id(pid, "doc")
    return {
        "id": _full_id("doc", short),
        "entity_types": dict(entry["entity_types"]),
        "collaboration_mode": entry["collaboration_mode"],
        "singleton": list(entry["singleton"]),
        "layout_hint": entry["layout_hint"],
    }


def _rewire_refs(schema: dict, rename: dict[str, str]) -> None:
    """preset 改桶名时,递归重写 entity_ref 的 to: 指向(clip.asset to:asset_item 等)。"""
    if not isinstance(schema, dict):
        return
    if schema.get("type") == "entity_ref" and schema.get("to") in rename:
        schema["to"] = rename[schema["to"]]
    for v in schema.values():
        if isinstance(v, dict):
            _rewire_refs(v, rename)
        elif isinstance(v, list):
            for item in v:
                _rewire_refs(item, rename)


def apply_preset(registry: Any, pid: str, *, rename: dict[str, str] | None = None,
                 extend: dict[str, dict] | None = None) -> list[str]:
    """把 preset 逐 declare 进 DocumentRegistry + set_collaboration_mode,返回声明的 type 名。

    rename: {默认桶名: mod 桶名}(同时重写库内 entity_ref to: 指向)。
    extend: {默认桶名: zt.archetype 的 extend 实参}。
    最小胶水:不做任何 registry 之外的事(singleton 约束由信封导入/门禁层把守)。
    """
    short, entry = _parse_id(pid, "doc")
    rename = rename or {}
    for label, mapping in (("rename", rename), ("extend", extend or {})):
        unknown = [k for k in mapping if k not in entry["entity_types"]]
        if unknown:
            raise ZeError(
                "type_extension_violation",
                f"{label} 的桶 {unknown} 不在 ze.doc.{short} 的 {list(entry['entity_types'])} 内",
                context={"preset": short, "unknown": unknown},
            )
    declared: list[str] = []
    for bucket, aid in entry["entity_types"].items():
        final = rename.get(bucket, bucket)
        schema = archetype(aid, extend=(extend or {}).get(bucket))
        if rename:
            _rewire_refs(schema, rename)
        registry.declare_entity_type(
            name=final, schema=schema,
            collaboration_unit=bool(schema.get("x-ze-collab-unit")),
        )
        declared.append(final)
    registry.set_collaboration_mode(entry["collaboration_mode"])
    return declared


# ==============================================================
# JSON 镜像入口 + 机器词表
# ==============================================================

def resolve(node: dict | str) -> Any:
    """JSON 镜像(docs/AI/信封次形态)——与 zt.field/archetype 同一条校验代码路径。

    形如 {"$use": "ze.archetype.clip@1", "extend": {...}, "override_labels": {...}}
    或 {"$use": "ze.field.label@1", "label": "..."};裸 string 视同 {"$use": s}。
    裸短名歧义(scene 同时是 archetype 与 doc)→ 必须用全 id。
    """
    if isinstance(node, str):
        node = {"$use": node}
    fid = node.get("$use", "")
    m = _ID_RE.match(fid or "")
    if not m:
        raise ZeError("type_fragment_unknown", f"malformed $use {fid!r}",
                      context={"node": node})
    tier = m.group(1)
    if tier is None:
        short = m.group(2)
        hits = [t for t, table in _TIERS.items() if short in table]
        if len(hits) != 1:
            raise ZeError(
                "type_fragment_unknown",
                f"裸短名 {short!r} 命中 {hits or '0 个 tier'},请用全 id(ze.<tier>.<name>)",
                context={"id": fid, "hits": hits},
            )
        tier = hits[0]
    rest = {k: v for k, v in node.items() if k != "$use"}
    if tier == "field":
        return field(fid, **rest)
    if tier == "cluster":
        return cluster(fid, **rest)
    if tier == "archetype":
        ext = rest.pop("extend", None)
        if ext:
            ext = {k: (resolve(v) if isinstance(v, (str, dict)) and
                       (isinstance(v, str) or "$use" in v) else v)
                   for k, v in ext.items()}
        return archetype(fid, extend=ext, **rest)
    return doc_preset(fid)


def resolve_type_schema(node: dict | str) -> Any:
    """barrel 跨模块出口(server / scripts / envelope 用);等价 resolve()。"""
    return resolve(node)


def list_fragments(tier: str | None = None) -> list[dict]:
    """机器词表(GATE-TYPE-07 注册表双向核对用)。"""
    out = []
    for t, table in _TIERS.items():
        if tier and t != tier:
            continue
        for short, entry in sorted(table.items()):
            out.append({
                "id": f"ze.{t}.{short}",
                "tier": t,
                "version": entry["version"],
                "label": entry["label"],
                "closed": bool(entry.get("closed")),
                "pinned_name": entry.get("pinned_name"),
                "sealed": list(entry.get("sealed", [])),
                "singleton": bool(entry.get("singleton")),
                "collab": entry.get("collab_unit"),
            })
    return out
