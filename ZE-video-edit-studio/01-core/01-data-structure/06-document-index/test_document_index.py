from __future__ import annotations

import pytest

from document_index import DocumentIndex, DocumentIndexEntry


def test_document_index_add_list_remove():
    idx = DocumentIndex(kind="seek_find_object_project", title="Batch")
    e1 = DocumentIndexEntry(document_id="level-0450", kind="seek_find_level", label="0450")
    e2 = DocumentIndexEntry(document_id="level-0451", kind="seek_find_level", label="0451",
                            status="running")
    idx.add(e2)
    idx.add(e1)

    assert [e.document_id for e in idx.list()] == ["level-0450", "level-0451"]
    assert idx.get("level-0451").status == "running"
    assert idx.remove("level-0450").label == "0450"
    assert idx.get("level-0450") is None


def test_document_index_rejects_duplicate_add():
    idx = DocumentIndex(kind="k", title="T")
    idx.add(DocumentIndexEntry(document_id="d1", kind="doc", label="Doc"))
    with pytest.raises(ValueError):
        idx.add(DocumentIndexEntry(document_id="d1", kind="doc", label="Doc again"))


def test_document_index_upsert_and_aggregate_status():
    idx = DocumentIndex(kind="k", title="T")
    idx.upsert(DocumentIndexEntry(document_id="d1", kind="doc", label="Doc",
                                  status="empty"))
    idx.upsert(DocumentIndexEntry(document_id="d1", kind="doc", label="Doc",
                                  status="ready"))
    idx.set_aggregate_status({"ready": 1})
    assert idx.get("d1").status == "ready"
    assert idx.aggregate_status == {"ready": 1}


def test_document_index_hides_archived_by_default():
    idx = DocumentIndex(kind="k", title="T")
    idx.add(DocumentIndexEntry(document_id="d1", kind="doc", label="A",
                               status="archived"))
    idx.add(DocumentIndexEntry(document_id="d2", kind="doc", label="B"))
    assert [e.document_id for e in idx.list()] == ["d2"]
    assert {e.document_id for e in idx.list(include_archived=True)} == {"d1", "d2"}


def test_document_index_save_open_roundtrip(tmp_path):
    idx = DocumentIndex(kind="seek_find_object_project", title="Batch",
                        index_id="idx1", aggregate_status={"running": 1})
    idx.add(DocumentIndexEntry(
        document_id="level-0450",
        kind="seek_find_level",
        label="0450",
        thumbnail_asset_id="asset1",
        source_path="0450",
        meta={"objects": 60},
    ))
    path = tmp_path / "index.json"
    idx.save(path)

    reopened = DocumentIndex.open(path)
    assert reopened.index_id == "idx1"
    assert reopened.kind == "seek_find_object_project"
    assert reopened.aggregate_status == {"running": 1}
    assert reopened.get("level-0450").meta == {"objects": 60}
