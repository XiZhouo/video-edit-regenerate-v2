"""test_type_library — GATE-TYPE-01..06(叶内门禁;07/09 在 scripts/type_registry_check.py)。

覆盖:
  01 id 格式(注册表全量正则 + 三写法等价 + 畸形拒绝)
  02 fragment 自验(每条 example 灌真 make_validator;含 13 原型全量)
  03 深拷贝(改返回值不污染库)
  04 extend 加性 only(非 x_ / 碰撞 / 约束键 三负例)
  05 seal 与 tighten-only(正负例)
  06 provenance 注解必在(x-ze-use / x-ze-archetype / x-ze-ext / x-ze-sealed)
  附:pinned_name / apply_preset(rename 重写 to:) / closed 枚举 / 失败模式码
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

# 跨叶子依赖注入(镜像叶子自注入法;02-entity 由 type_library 自身注入)。
# 自身目录也注入:verify_all 的 glob 注入之外、standalone pytest 也能跑。
_HERE = Path(__file__).resolve().parent
_DS = _HERE.parent
for _d in (_HERE, _DS / "04-field-types", _DS / "01-document"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import type_library as zt  # noqa: E402
from fragments import ARCHETYPES, CLUSTERS, DOC_PRESETS, FIELDS  # noqa: E402
from entity import ZeError  # noqa: E402
from field_types import make_validator  # noqa: E402
from document import DocumentRegistry  # noqa: E402


# ==============================================================
# GATE-TYPE-01 — id 格式
# ==============================================================

_ID_OK = re.compile(r"^ze\.(field|cluster|archetype|doc)\.[a-z][a-z0-9_]*$")


def test_gate_01_registry_ids_wellformed():
    frags = zt.list_fragments()
    assert len(frags) == len(FIELDS) + len(CLUSTERS) + len(ARCHETYPES) + len(DOC_PRESETS)
    for f in frags:
        assert _ID_OK.match(f["id"]), f["id"]
        assert isinstance(f["version"], int) and f["version"] >= 1


def test_gate_01_three_spellings_equivalent():
    a = zt.field("label")
    b = zt.field("ze.field.label")
    c = zt.field("ze.field.label@1")
    assert a == b == c


@pytest.mark.parametrize("bad", ["", "Label", "ze.field.", "ze.bogus.label",
                                 "ze.field.label@x", "ze.archetype.label"])
def test_gate_01_malformed_rejected(bad):
    with pytest.raises(ZeError) as ei:
        zt.resolve(bad)
    assert ei.value.code == "type_fragment_unknown"


def test_gate_01_unknown_gives_candidates():
    with pytest.raises(ZeError) as ei:
        zt.field("bbox_xyx")
    assert "bbox_xyxy" in (ei.value.context or {}).get("candidates", [])


def test_gate_01_version_mismatch():
    with pytest.raises(ZeError) as ei:
        zt.field("label@9")
    assert ei.value.code == "type_version_mismatch"


# ==============================================================
# GATE-TYPE-02 — fragment 自验(example 灌真 validator)
# ==============================================================

def _strip_required(schema: dict) -> dict:
    return {k: {kk: vv for kk, vv in s.items() if kk != "required"}
            for k, s in schema.items()}


@pytest.mark.parametrize("short", sorted(FIELDS))
def test_gate_02_field_examples_validate(short):
    entry = FIELDS[short]
    sch = {"type": "object", "properties": _strip_required(entry["fields"])}
    make_validator(sch)(entry["example"], sch, "$")


@pytest.mark.parametrize("short", sorted(CLUSTERS))
def test_gate_02_cluster_examples_validate(short):
    sch = {"type": "object", "properties": _strip_required(zt.cluster(short))}
    make_validator(sch)(CLUSTERS[short]["example"], sch, "$")


@pytest.mark.parametrize("short", sorted(ARCHETYPES))
def test_gate_02_archetype_examples_validate(short):
    schema = zt.archetype(short)
    make_validator(schema)(ARCHETYPES[short]["example"], schema, "$")


_WRONG_VALUE = {"string": 123, "integer": "x", "number": "x", "boolean": "x",
                "array": {}, "object": [], "asset_ref": "x", "entity_ref": "x"}


@pytest.mark.parametrize("short", sorted(ARCHETYPES))
def test_gate_02_archetype_rejects_bad_data(short):
    """灌坏数据必须 schema_violation(缺必填,或对首个字段灌错型——chapter 等无必填原型)。"""
    schema = zt.archetype(short)
    required = [k for k, s in schema["properties"].items() if s.get("required") is True]
    if required:
        bad = {}
    else:
        name, sub = next(iter(schema["properties"].items()))
        bad = {name: _WRONG_VALUE[sub["type"]]}
    with pytest.raises(ZeError) as ei:
        make_validator(schema)(bad, schema, "$")
    assert ei.value.code == "schema_violation"


def test_gate_02_closed_enum_rejects_outside_value():
    schema = zt.archetype("review")
    bad = dict(ARCHETYPES["review"]["example"], status="maybe")
    with pytest.raises(ZeError):
        make_validator(schema)(bad, schema, "$")


# ==============================================================
# GATE-TYPE-03 — 深拷贝
# ==============================================================

def test_gate_03_field_deep_copy():
    a = zt.field("tags")
    a["items"]["minLength"] = 99
    assert zt.field("tags")["items"]["minLength"] == 1


def test_gate_03_archetype_deep_copy():
    s1 = zt.archetype("layer")
    s1["properties"]["kind"]["enum"] = ["hacked"]
    s1["x-ze-sealed"].append("hacked")
    s2 = zt.archetype("layer")
    assert "enum" not in s2["properties"]["kind"]
    assert "hacked" not in s2["x-ze-sealed"]


def test_gate_03_cluster_deep_copy():
    c = zt.cluster("rect")
    c["w"]["minimum"] = -1
    assert zt.cluster("rect")["w"]["minimum"] == 0


# ==============================================================
# GATE-TYPE-04 — extend 加性 only
# ==============================================================

def test_gate_04_extend_requires_x_prefix():
    with pytest.raises(ZeError) as ei:
        zt.archetype("clip", extend={"speaker": {"type": "string"}})
    assert ei.value.code == "type_extension_violation"


def test_gate_04_extend_library_field_collision_rejected():
    """extend 既有库属字段名(track)= 重定义企图,被加性 only 拦下;x_ 新名放行。"""
    with pytest.raises(ZeError) as ei:
        zt.archetype("clip", extend={"track": {"type": "integer"}})
    assert ei.value.code == "type_extension_violation"
    s = zt.archetype("clip", extend={"x_note": {"type": "string"}})
    assert s["properties"]["x_note"]["x-ze-ext"] is True


def test_gate_04_field_extra_constraint_rejected():
    with pytest.raises(ZeError) as ei:
        zt.field("label", minimum=3)
    assert ei.value.code == "type_extension_violation"


def test_gate_04_field_cosmetic_allowed():
    s = zt.field("confidence", label="可信度", **{"x-group": "评估", "step": 0.01})
    assert s["label"] == "可信度" and s["x-group"] == "评估"
    assert s["minimum"] == 0 and s["maximum"] == 1   # 约束原样


def test_gate_04_extend_with_use_reference():
    s = zt.archetype("review", extend={"x_severity": {"$use": "class_id", "label": "严重度"}})
    prop = s["properties"]["x_severity"]
    assert prop["x-ze-ext"] is True
    assert prop["x-ze-use"] == "ze.field.class_id@1"
    assert prop["label"] == "严重度"


# ==============================================================
# GATE-TYPE-05 — seal 与 tighten-only
# ==============================================================

def test_gate_05_narrow_sealed_rejected():
    with pytest.raises(ZeError) as ei:
        zt.archetype("clip", narrow={"track": {"maximum": 4}})
    assert ei.value.code == "type_seal_violation"


def test_gate_05_narrow_loosen_rejected():
    with pytest.raises(ZeError) as ei:
        zt.archetype("clip", narrow={"source_in": {"minimum": -5}})
    assert ei.value.code == "type_extension_violation"


def test_gate_05_narrow_type_change_rejected():
    with pytest.raises(ZeError) as ei:
        zt.archetype("clip", narrow={"source_in": {"type": "string"}})
    assert ei.value.code == "type_extension_violation"


def test_gate_05_narrow_enum_subset_ok_superset_rejected():
    s = zt.archetype("data_query", narrow={"dialect": {"enum": ["bigquery"]}})
    assert s["properties"]["dialect"]["enum"] == ["bigquery"]
    with pytest.raises(ZeError):
        zt.archetype("data_query", narrow={"dialect": {"enum": ["bigquery", "oracle"]}})


def test_gate_05_narrow_legal_tighten_ok():
    s = zt.archetype("clip", narrow={"source_in": {"minimum": 1}})
    assert s["properties"]["source_in"]["minimum"] == 1


def test_gate_05_narrow_unknown_field_rejected():
    with pytest.raises(ZeError) as ei:
        zt.archetype("clip", narrow={"bogus": {"minimum": 1}})
    assert ei.value.code == "type_extension_violation"


# ==============================================================
# GATE-TYPE-06 — provenance 注解必在
# ==============================================================

def test_gate_06_field_provenance():
    assert zt.field("label")["x-ze-use"] == "ze.field.label@1"
    for name, sub in zt.cluster("run").items():
        assert "x-ze-use" in sub, name
    # 簇内 $use 字段记最深来源
    assert zt.cluster("run")["status"]["x-ze-use"] == "ze.field.run_status@1"


@pytest.mark.parametrize("short", sorted(ARCHETYPES))
def test_gate_06_archetype_provenance(short):
    s = zt.archetype(short)
    assert s["x-ze-archetype"] == f"ze.archetype.{short}@{ARCHETYPES[short]['version']}"
    assert s["x-ze-sealed"] == list(ARCHETYPES[short]["sealed"])
    assert s["x-ze-collab-unit"] == ARCHETYPES[short]["collab_unit"]
    # sealed 字段必须真实存在
    for name in s["x-ze-sealed"]:
        assert name in s["properties"], f"{short} sealed {name!r} 不在 properties"


def test_gate_06_validator_ignores_annotations():
    """x-ze-* 注解对 validator 不可见(零改动承重事实)。"""
    schema = zt.archetype("placed_object")
    make_validator(schema)(ARCHETYPES["placed_object"]["example"], schema, "$")


# ==============================================================
# 附:pinned / preset / 失败模式
# ==============================================================

def test_pinned_status_name_enforced():
    frags = {f["id"]: f for f in zt.list_fragments("field")}
    assert frags["ze.field.workflow_status"]["pinned_name"] == "status"
    assert frags["ze.field.run_status"]["pinned_name"] == "status"
    assert frags["ze.field.workflow_status"]["closed"] is True


def test_apply_preset_declares_and_sets_mode():
    for pid, entry in DOC_PRESETS.items():
        reg = DocumentRegistry()
        names = zt.apply_preset(reg, pid)
        assert set(names) == set(entry["entity_types"])
        assert reg.collaboration_mode == entry["collaboration_mode"]
        doc = reg.build_document(schema_version="1.0.0", name="t")
        assert doc is not None


def test_apply_preset_rename_rewires_entity_ref_to():
    reg = DocumentRegistry()
    zt.apply_preset(reg, "timeline", rename={"asset_item": "footage"})
    clip_schema = reg.entity_types["clip"]["schema"]
    assert clip_schema["properties"]["asset"]["to"] == "footage"
    assert "footage" in reg.entity_types and "asset_item" not in reg.entity_types


def test_apply_preset_unknown_rename_rejected():
    reg = DocumentRegistry()
    with pytest.raises(ZeError) as ei:
        zt.apply_preset(reg, "scene", rename={"bogus": "x"})
    assert ei.value.code == "type_extension_violation"


def test_apply_preset_collab_units_follow_archetype():
    reg = DocumentRegistry()
    zt.apply_preset(reg, "review_queue")
    assert reg.entity_types["task"]["collaboration_unit"] is False     # supervisor 单写者
    assert reg.entity_types["review"]["collaboration_unit"] is True


def test_resolve_bare_ambiguous_short_rejected():
    with pytest.raises(ZeError) as ei:
        zt.resolve("scene")   # archetype 与 doc 双命中
    assert ei.value.code == "type_fragment_unknown"
    assert sorted((ei.value.context or {}).get("hits", [])) == ["archetype", "doc"]


def test_cluster_on_single_field_rejected():
    with pytest.raises(ZeError):
        zt.cluster("label")


def test_field_on_flat_group_redirects():
    with pytest.raises(ZeError) as ei:
        zt.field("point2d")
    assert "cluster" in str(ei.value)


def test_cluster_flattens_prompt_block():
    props = zt.cluster("prompt_block", required=["prompt"])
    assert set(props) == {"prompt", "negative_prompt"}
    assert props["prompt"]["required"] is True


def test_cluster_required_unknown_rejected():
    with pytest.raises(ZeError):
        zt.cluster("rect", required=["bogus"])


def test_end_to_end_compose_edit_validate():
    """七步验收的前三步(compose→validate→真 doc.add):mod 零手写库属字段。"""
    from entity import Entity
    reg = DocumentRegistry()
    zt.apply_preset(reg, "scene", extend={"placed_object": {"x_hint": {"type": "string"}}})
    doc = reg.build_document(schema_version="1.0.0", name="关卡 1")
    doc.add(Entity(type="scene", name="关卡 1",
                   data={"width": 1920, "height": 1080, "status": "draft"}))
    doc.add(Entity(type="placed_object", name="苹果",
                   data={"x": 10, "y": 20, "w": 50, "h": 50, "label": "apple",
                         "x_hint": "树后"}))
    with pytest.raises(ZeError) as ei:
        doc.add(Entity(type="placed_object", name="坏数据",
                       data={"x": 10, "y": 20, "w": -1, "h": 50}))
    assert ei.value.code == "schema_violation"


def test_erratum_clip_asset_optional_for_timeline_add_flow():
    """勘误回归:timeline 双击空轨新增产 {track,start,duration} 无 asset,必须过校验。"""
    schema = zt.archetype("clip")
    assert schema["properties"]["asset"].get("required") is not True
    make_validator(schema)({"track": 0, "start": 0, "duration": 2}, schema, "$")


def test_erratum_placed_object_has_color():
    """勘误回归:标注框标记色(box 迁移承重)。"""
    schema = zt.archetype("placed_object")
    color = schema["properties"]["color"]
    assert color["format"] == "color" and color["x-ze-use"] == "ze.field.color_hex@1"
    assert "color" not in schema["x-ze-sealed"]
