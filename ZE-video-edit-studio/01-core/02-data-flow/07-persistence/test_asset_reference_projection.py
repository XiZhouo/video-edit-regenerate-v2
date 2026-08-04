"""Schema-aware asset multiset tests: no digest guessing, no lost references."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest


_HERE = Path(__file__).resolve().parent
_DATA_STRUCTURE = _HERE.parents[1] / "01-data-structure"
for _directory in (
    _HERE,
    _DATA_STRUCTURE / "01-document",
    _DATA_STRUCTURE / "02-entity",
    _DATA_STRUCTURE / "04-field-types",
):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

from asset_reference_projection import (  # noqa: E402
    AssetReferenceProjectionError,
    project_asset_references,
    verify_asset_reference_projection,
)
from business_snapshot import capture_business_snapshot  # noqa: E402
from document import Document  # noqa: E402
from entity import Entity  # noqa: E402


SHA = "a" * 64


def _snapshot(
    *,
    conflict: bool = False,
    huge_size: bool = False,
    mime: str = "image/png",
    ext: str = "png",
    entity_id: str = "media-1",
):
    schema = {
        "media": {
            "type": "object",
            "properties": {
                "primary": {"type": "asset_ref"},
                "gallery": {
                    "type": "array",
                    "items": {"type": "asset_ref"},
                },
                "optional": {
                    "oneOf": [{"type": "asset_ref"}, {"type": "null"}],
                },
                "looks_like_hash": {"type": "string"},
                "untyped": {},
            },
        }
    }
    document = Document(
        id="document-asset",
        name="assets",
        schema_version="1",
        collaboration_mode="live_collab",
        save_policy="auto_commit",
        schema=schema,
    )
    primary = {
        "sha256": SHA,
        "mime": mime,
        "size": (2**63 if huge_size else 0),
        "ext": ext,
    }
    gallery = {"sha256": SHA}
    if conflict:
        gallery["mime"] = "image/jpeg"
    entity = Entity(
        type="media",
        id=entity_id,
        name="media",
        data={
            "primary": primary,
            "gallery": [gallery],
            "optional": None,
            "looks_like_hash": "b" * 64,
            "untyped": {"sha256": "c" * 64},
        },
    )
    document.add(entity)
    entity._deleted = True
    document._v = 2
    return capture_business_snapshot("project-asset", 3, document).snapshot


def _snapshot_with_asset_field(field_name: str):
    document = Document(
        id="document-asset-path",
        name="assets",
        schema_version="1",
        collaboration_mode="live_collab",
        save_policy="auto_commit",
        schema={
            "media": {
                "type": "object",
                "properties": {field_name: {"type": "asset_ref"}},
            }
        },
    )
    document.add(
        Entity(
            type="media",
            id="media-path",
            name="media",
            data={field_name: {"sha256": SHA}},
        )
    )
    document._v = 2
    return capture_business_snapshot("project-asset-path", 1, document).snapshot


def test_projection_follows_schema_and_merges_partial_metadata() -> None:
    projection = project_asset_references(_snapshot())
    assert projection.reference_count == 2
    assert projection.unique_reference_count == 1
    assert [item.value_path for item in projection.occurrences] == [
        "/gallery/0",
        "/primary",
    ]
    assert all(item.entity_deleted for item in projection.occurrences)
    # A partial reference and a complete reference to one digest converge to
    # one canonical metadata value.  Empty blobs (size=0) stay valid.
    reference = projection.multiplicities[0].reference
    assert (reference.sha256, reference.mime, reference.size, reference.ext) == (
        SHA,
        "image/png",
        0,
        "png",
    )
    assert projection.multiplicities[0].count == 2
    assert all(item.reference == reference for item in projection.occurrences)


def test_64_hex_strings_outside_asset_ref_schema_are_not_guessed() -> None:
    projection = project_asset_references(_snapshot())
    assert {item.reference.sha256 for item in projection.occurrences} == {SHA}
    assert "b" * 64 not in {item.reference.sha256 for item in projection.occurrences}
    assert "c" * 64 not in {item.reference.sha256 for item in projection.occurrences}


def test_empty_mime_and_extension_remain_legacy_compatible() -> None:
    projection = project_asset_references(_snapshot(mime="", ext=""))
    reference = projection.multiplicities[0].reference
    assert reference.mime == ""
    assert reference.ext == ""


def test_known_metadata_conflict_and_oversized_size_fail_closed() -> None:
    with pytest.raises(AssetReferenceProjectionError) as conflict:
        project_asset_references(_snapshot(conflict=True))
    assert conflict.value.code == "asset_reference_metadata_conflict"
    with pytest.raises(AssetReferenceProjectionError) as size:
        project_asset_references(_snapshot(huge_size=True))
    assert size.value.code == "asset_reference_value_invalid"


@pytest.mark.parametrize(
    "snapshot",
    [
        lambda: _snapshot(mime="m" * 256),
        lambda: _snapshot(ext="x" * 33),
        lambda: _snapshot(entity_id="e" * 1025),
        # JSON Pointer escapes each '~' to '~0'; 513 input bytes become a
        # 1027-byte indexed path including the leading slash.
        lambda: _snapshot_with_asset_field("~" * 513),
    ],
)
def test_v6_indexed_asset_fields_reject_oversized_values(snapshot) -> None:
    with pytest.raises(AssetReferenceProjectionError):
        project_asset_references(snapshot())


def test_projection_digest_and_closed_multiset_detect_tamper() -> None:
    projection = project_asset_references(_snapshot())
    first = dataclasses.replace(projection.occurrences[0], value_path="/changed")
    tampered = dataclasses.replace(
        projection,
        occurrences=(first, *projection.occurrences[1:]),
    )
    with pytest.raises(AssetReferenceProjectionError):
        verify_asset_reference_projection(tampered)
    with pytest.raises(AssetReferenceProjectionError):
        verify_asset_reference_projection(
            dataclasses.replace(projection, projection_sha256=b"x" * 32)
        )


def test_snapshot_error_translation_does_not_retain_lower_exception() -> None:
    with pytest.raises(AssetReferenceProjectionError) as raised:
        project_asset_references(object())
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
