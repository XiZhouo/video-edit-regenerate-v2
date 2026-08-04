import pytest

from ze_video_domain import schema


def test_make_project_defaults():
    p = schema.make_project("p-1", "示例项目")
    assert p["type"] == "project" and p["fps"] == 30.0 and p["version"] == 1
    assert p["width"] == 1920 and p["height"] == 1080


def test_make_asset_validates_type():
    with pytest.raises(ValueError):
        schema.make_asset("a-1", "clip", "movie", "ref://x")  # bad assetType


def test_annotation_degenerate_interval_ok():
    ann = schema.make_annotation(
        "an-1", "标注", bbox={"x": 10, "y": 20, "w": 30, "h": 40},
        interval=[1.5, 1.5], label="把2改成3", number=1)
    assert ann["interval"] == [1.5, 1.5]  # single-frame interval is valid


def test_interval_rejects_reversed():
    with pytest.raises(ValueError):
        schema.make_annotation("an-2", "x", bbox={"x": 0, "y": 0, "w": 1, "h": 1},
                               interval=[5, 1], label="l", number=1)


def test_update_entity_is_immutable_and_bumps_version():
    p = schema.make_project("p-1", "a")
    p2 = schema.update_entity(p, name="b")
    assert p["name"] == "a" and p2["name"] == "b"
    assert p2["version"] == 2 and p["version"] == 1


def test_update_entity_cannot_change_id_or_type():
    p = schema.make_project("p-1", "a")
    with pytest.raises(ValueError):
        schema.update_entity(p, id="p-2")


def test_document_add_and_unique_asset_name():
    doc = schema.new_document(schema.make_project("p-1", "proj"))
    a1 = schema.make_asset("a-1", "运镜参考", "video", "ref://1")
    doc = schema.add_entity(doc, a1)
    assert schema.get_entity(doc, "asset", "a-1")["name"] == "运镜参考"
    dup = schema.make_asset("a-2", "运镜参考", "video", "ref://2")
    with pytest.raises(ValueError):
        schema.add_entity(doc, dup)  # duplicate name rejected


def test_document_put_and_remove():
    doc = schema.new_document(schema.make_project("p-1", "proj"))
    a1 = schema.make_asset("a-1", "x", "image", "ref://1")
    doc = schema.add_entity(doc, a1)
    doc = schema.put_entity(doc, schema.update_entity(a1, ref="ref://2"))
    assert schema.get_entity(doc, "asset", "a-1")["ref"] == "ref://2"
    doc = schema.remove_entity(doc, "asset", "a-1")
    assert schema.get_entity(doc, "asset", "a-1") is None


def test_add_entity_does_not_mutate_input_document():
    doc = schema.new_document(schema.make_project("p-1", "proj"))
    before = dict(doc["assets"])
    schema.add_entity(doc, schema.make_asset("a-1", "x", "image", "r"))
    assert doc["assets"] == before  # original untouched


def test_new_id_allocates_next_free():
    assert schema.new_id("clip", ["clip-0001", "clip-0003"]) == "clip-0002"
    assert schema.new_id("clip", []) == "clip-0001"


def test_candidate_scope_validation():
    with pytest.raises(ValueError):
        schema.make_candidate("c-1", "cand", scope="bogus")
    c = schema.make_candidate("c-1", "cand", scope="span", target_span=[1, 2])
    assert c["scope"] == "span" and c["targetSpan"] == [1.0, 2.0]
