"""type_registry_check 测试:词表双向钉死(GATE-TYPE-07)+ mod 防漂移(GATE-TYPE-09)逻辑正确。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import type_registry_check as trc  # noqa: E402


def test_row_regex_parses_registry_rows():
    m = trc._ROW_RE.match("| `ze.field.label` | @1 | 语义名 | — |")
    assert m and m.group(1) == "ze.field.label" and m.group(2) == "1"
    m2 = trc._ROW_RE.match("| `ze.archetype.scene` | @1 | 场景锚 | sealed=4 collab=true singleton |")
    assert m2 and set(m2.group(3).split()) == {"sealed=4", "collab=true", "singleton"}
    assert trc._ROW_RE.match("| ze.field.label | @1 | 无反引号 | — |") is None


def test_expected_tokens_reconstruction():
    f = {"tier": "archetype", "closed": False, "pinned_name": None,
         "sealed": ["a", "b"], "singleton": True, "collab": True}
    assert trc._expected_tokens(f) == {"sealed=2", "collab=true", "singleton"}
    g = {"tier": "field", "closed": True, "pinned_name": "status",
         "sealed": [], "singleton": False, "collab": None}
    assert trc._expected_tokens(g) == {"closed", "pinned=status"}


def test_real_repo_registry_in_sync():
    """GATE-TYPE-07 对真仓必须绿(注册表 ⇄ list_fragments 双向一致)。"""
    problems: list[str] = []
    status = trc.check_registry_doc(problems)
    assert status == "checked"
    assert problems == [], problems


def test_real_repo_mod_no_drift():
    """GATE-TYPE-09 对真仓必须绿(demo mod 当前为手写逃生舱或合法组合)。"""
    problems: list[str] = []
    status = trc.check_mod_drift(problems)
    assert status.startswith("checked") or status.startswith("skipped")
    assert problems == [], problems


def test_drift_detection_on_tampered_schema():
    """漂移检测:手改 resolved schema(放松 sealed 字段)必须被抓。"""
    lib = trc._load_lib()
    if lib is None:
        return
    sys.path.insert(0, str(trc.LEAF.parent / "01-document"))
    from document import DocumentRegistry  # noqa: E402

    # 合法组合 → 0 问题
    reg = DocumentRegistry()
    reg.declare_entity_type(name="clip", schema=lib.archetype("clip"), collaboration_unit=True)

    # 篡改:track(sealed)的 minimum 改掉 + 加无前缀字段
    bad = lib.archetype("clip")
    bad["properties"]["track"]["minimum"] = -99
    bad["properties"]["speaker"] = {"type": "string"}
    reg2 = DocumentRegistry()
    reg2.declare_entity_type(name="clip", schema=bad, collaboration_unit=True)

    problems: list[str] = []
    trc._check_registry_entity_types(reg2, lib, problems)
    kinds = {p.split(":")[0] for p in problems}
    assert "MOD_SEALED_FIELD_DRIFT" in kinds, problems
    assert "MOD_FIELD_NOT_X_PREFIXED" in kinds, problems

    # 合法组合零问题
    problems2: list[str] = []
    trc._check_registry_entity_types(reg, lib, problems2)
    assert problems2 == [], problems2
