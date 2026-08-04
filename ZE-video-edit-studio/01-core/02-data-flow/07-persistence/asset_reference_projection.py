"""Schema-aware complete asset-reference multiset for a business snapshot.

Only values reached through a canonical field schema whose effective type is
``asset_ref`` are references.  A 64-character string elsewhere is ordinary
business data and is never guessed to be an asset.  Object properties, array
items and oneOf/anyOf/allOf use the same compiler and validator as the domain
field-types leaf.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from business_codec import BusinessCodecError, decode_payload_bounded
from business_snapshot import (
    BusinessSnapshotError,
    BusinessSnapshotSet,
    MAX_DOCUMENT_SNAPSHOT_BYTES,
    MAX_ENTITY_SNAPSHOT_BYTES,
    MAX_SNAPSHOT_VALUE_NODES,
    verify_business_snapshot,
)
from field_types import canonicalize_entity_schema, validate
from entity import ZeError

from business_transaction_contract import (
    MAX_ATOMIC_ASSET_REFERENCES,
    MAX_SQLITE_INTEGER,
)


ASSET_REFERENCE_PROJECTION_PROTOCOL = "ze.asset-reference-projection.v1"
ASSET_REFERENCE_HASH_DOMAIN = b"ze.asset-reference-multiset.v1"
MAX_ASSET_REFERENCE_ID_BYTES = 1024
MAX_ASSET_REFERENCE_MIME_BYTES = 255
MAX_ASSET_REFERENCE_EXTENSION_BYTES = 32
_LOWER_HEX = frozenset("0123456789abcdef")


class AssetReferenceProjectionError(ValueError):
    """Stable, payload-free failure from schema-aware projection."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, order=True)
class AssetReferenceValue:
    sha256: str
    mime: Optional[str]
    size: Optional[int]
    ext: Optional[str]


@dataclass(frozen=True)
class AssetReferenceOccurrence:
    entity_type: str
    entity_id: str
    entity_deleted: bool
    value_path: str
    reference: AssetReferenceValue


@dataclass(frozen=True)
class AssetReferenceMultiplicity:
    reference: AssetReferenceValue
    count: int


@dataclass(frozen=True)
class AssetReferenceProjection:
    protocol: str
    project_id: str
    generation: int
    document_id: str
    business_root_sha256: bytes = field(repr=False)
    occurrences: Tuple[AssetReferenceOccurrence, ...]
    multiplicities: Tuple[AssetReferenceMultiplicity, ...]
    projection_sha256: bytes = field(repr=False)

    @property
    def reference_count(self) -> int:
        return len(self.occurrences)

    @property
    def unique_reference_count(self) -> int:
        return len(self.multiplicities)


def _fail(code: str) -> None:
    raise AssetReferenceProjectionError(code)


def _frame(hasher: Any, value: bytes) -> None:
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _utf8(value: Any) -> bytes:
    if type(value) is not str:
        _fail("asset_reference_text_invalid")
    invalid_utf8 = False
    try:
        return value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        invalid_utf8 = True
    if invalid_utf8:
        _fail("asset_reference_text_invalid")
    raise AssertionError("unreachable")


def _validate_reference(value: Any) -> AssetReferenceValue:
    invalid = False
    try:
        validate(value, {"type": "asset_ref"}, "$")
    except ZeError:
        invalid = True
    if invalid:
        _fail("asset_reference_value_invalid")
    if type(value) is not dict:
        _fail("asset_reference_value_invalid")
    sha256 = value.get("sha256")
    if (
        type(sha256) is not str
        or len(sha256) != 64
        or any(character not in _LOWER_HEX for character in sha256)
    ):
        _fail("asset_reference_value_invalid")
    mime = value.get("mime") if "mime" in value else None
    size = value.get("size") if "size" in value else None
    ext = value.get("ext") if "ext" in value else None
    if mime is not None:
        if type(mime) is not str:
            _fail("asset_reference_value_invalid")
        mime_bytes = _utf8(mime)
        if len(mime_bytes) > MAX_ASSET_REFERENCE_MIME_BYTES:
            _fail("asset_reference_value_invalid")
    if size is not None and (
        type(size) is not int or not 0 <= size <= MAX_SQLITE_INTEGER
    ):
        _fail("asset_reference_value_invalid")
    if ext is not None:
        if type(ext) is not str:
            _fail("asset_reference_value_invalid")
        ext_bytes = _utf8(ext)
        if (
            len(ext_bytes) > MAX_ASSET_REFERENCE_EXTENSION_BYTES
            or (ext and ("/" in ext or "\\" in ext))
        ):
            _fail("asset_reference_value_invalid")
    return AssetReferenceValue(sha256=sha256, mime=mime, size=size, ext=ext)


def _merge_reference_metadata(
    left: AssetReferenceValue,
    right: AssetReferenceValue,
) -> AssetReferenceValue:
    if left.sha256 != right.sha256:
        _fail("asset_reference_identity_mismatch")

    def known(first: Any, second: Any) -> Any:
        if first is not None and second is not None and first != second:
            _fail("asset_reference_metadata_conflict")
        return first if first is not None else second

    return AssetReferenceValue(
        sha256=left.sha256,
        mime=known(left.mime, right.mime),
        size=known(left.size, right.size),
        ext=known(left.ext, right.ext),
    )


def _matches(value: Any, schema: Dict[str, Any]) -> bool:
    try:
        validate(value, schema, "$")
    except ZeError:
        return False
    return True


def _pointer(parent: str, member: str) -> str:
    escaped = member.replace("~", "~0").replace("/", "~1")
    pointer = parent + "/" + escaped
    if len(_utf8(pointer)) > MAX_ASSET_REFERENCE_ID_BYTES:
        _fail("asset_reference_path_budget_exceeded")
    return pointer


def _walk_schema(
    value: Any,
    schema: Dict[str, Any],
    path: str,
    output: Dict[str, AssetReferenceValue],
) -> None:
    composite = [name for name in ("oneOf", "anyOf", "allOf") if name in schema]
    if composite:
        if len(composite) != 1:
            _fail("asset_reference_schema_invalid")
        name = composite[0]
        alternatives = schema[name]
        if type(alternatives) is not list:
            _fail("asset_reference_schema_invalid")
        matches = [candidate for candidate in alternatives if _matches(value, candidate)]
        if name == "oneOf" and len(matches) != 1:
            _fail("asset_reference_schema_value_mismatch")
        if name == "anyOf" and not matches:
            _fail("asset_reference_schema_value_mismatch")
        if name == "allOf" and len(matches) != len(alternatives):
            _fail("asset_reference_schema_value_mismatch")
        for candidate in matches:
            _walk_schema(value, candidate, path, output)
        return

    schema_type = schema.get("type")
    if schema_type == "asset_ref":
        reference = _validate_reference(value)
        existing = output.get(path)
        if existing is not None and existing != reference:
            _fail("asset_reference_composite_conflict")
        output[path] = reference
        return
    if schema_type == "array":
        if type(value) is not list:
            _fail("asset_reference_schema_value_mismatch")
        item_schema = schema.get("items") or schema.get("itemSchema")
        if item_schema is None:
            return
        if type(item_schema) is not dict:
            _fail("asset_reference_schema_invalid")
        for index, item in enumerate(value):
            _walk_schema(item, item_schema, _pointer(path, str(index)), output)
        return
    if schema_type == "object":
        if type(value) is not dict:
            _fail("asset_reference_schema_value_mismatch")
        properties = schema.get("properties") or {}
        if type(properties) is not dict:
            _fail("asset_reference_schema_invalid")
        for key in sorted(properties):
            if key in value:
                candidate = properties[key]
                if type(candidate) is not dict:
                    _fail("asset_reference_schema_invalid")
                _walk_schema(value[key], candidate, _pointer(path, key), output)


def _reference_bytes(reference: AssetReferenceValue) -> bytes:
    hasher = hashlib.sha256()
    _frame(hasher, bytes.fromhex(reference.sha256))
    for value in (reference.mime, reference.ext):
        if value is None:
            _frame(hasher, b"\x00")
        else:
            _frame(hasher, b"\x01" + _utf8(value))
    if reference.size is None:
        _frame(hasher, b"\x00")
    else:
        _frame(hasher, b"\x01" + reference.size.to_bytes(8, "big"))
    return hasher.digest()


def _projection_digest(
    project_id: str,
    generation: int,
    document_id: str,
    business_root_sha256: bytes,
    occurrences: Tuple[AssetReferenceOccurrence, ...],
) -> bytes:
    hasher = hashlib.sha256()
    _frame(hasher, ASSET_REFERENCE_HASH_DOMAIN)
    _frame(hasher, _utf8(project_id))
    _frame(hasher, generation.to_bytes(8, "big"))
    _frame(hasher, _utf8(document_id))
    _frame(hasher, business_root_sha256)
    _frame(hasher, len(occurrences).to_bytes(8, "big"))
    for ordinal, occurrence in enumerate(occurrences):
        _frame(hasher, ordinal.to_bytes(8, "big"))
        _frame(hasher, _utf8(occurrence.entity_type))
        _frame(hasher, _utf8(occurrence.entity_id))
        _frame(hasher, b"\x01" if occurrence.entity_deleted else b"\x00")
        _frame(hasher, _utf8(occurrence.value_path))
        _frame(hasher, _reference_bytes(occurrence.reference))
    return hasher.digest()


def _validate_projection(value: Any) -> AssetReferenceProjection:
    if type(value) is not AssetReferenceProjection:
        _fail("asset_reference_projection_type_invalid")
    if value.protocol != ASSET_REFERENCE_PROJECTION_PROTOCOL:
        _fail("asset_reference_projection_protocol_invalid")
    _utf8(value.project_id)
    if len(_utf8(value.document_id)) > MAX_ASSET_REFERENCE_ID_BYTES:
        _fail("asset_reference_projection_identity_invalid")
    if type(value.generation) is not int or value.generation < 1:
        _fail("asset_reference_projection_identity_invalid")
    if type(value.business_root_sha256) is not bytes or len(value.business_root_sha256) != 32:
        _fail("asset_reference_projection_digest_invalid")
    if type(value.occurrences) is not tuple or len(value.occurrences) > MAX_ATOMIC_ASSET_REFERENCES:
        _fail("asset_reference_projection_occurrences_invalid")
    if type(value.multiplicities) is not tuple:
        _fail("asset_reference_projection_multiplicities_invalid")

    previous_key = None
    counts: Dict[AssetReferenceValue, int] = {}
    sha_metadata: Dict[str, AssetReferenceValue] = {}
    for occurrence in value.occurrences:
        if type(occurrence) is not AssetReferenceOccurrence:
            _fail("asset_reference_occurrence_invalid")
        if (
            not 1 <= len(_utf8(occurrence.entity_type)) <= MAX_ASSET_REFERENCE_ID_BYTES
            or not 1 <= len(_utf8(occurrence.entity_id)) <= MAX_ASSET_REFERENCE_ID_BYTES
            or not 1 <= len(_utf8(occurrence.value_path)) <= MAX_ASSET_REFERENCE_ID_BYTES
        ):
            _fail("asset_reference_occurrence_identity_invalid")
        if type(occurrence.entity_deleted) is not bool:
            _fail("asset_reference_occurrence_invalid")
        reference = _validate_reference(
            {
                "sha256": occurrence.reference.sha256,
                **({"mime": occurrence.reference.mime} if occurrence.reference.mime is not None else {}),
                **({"size": occurrence.reference.size} if occurrence.reference.size is not None else {}),
                **({"ext": occurrence.reference.ext} if occurrence.reference.ext is not None else {}),
            }
        ) if type(occurrence.reference) is AssetReferenceValue else None
        if reference is None or reference != occurrence.reference:
            _fail("asset_reference_occurrence_invalid")
        key = (
            occurrence.entity_type,
            occurrence.entity_id,
            occurrence.value_path,
        )
        if previous_key is not None and key <= previous_key:
            _fail("asset_reference_occurrence_order_invalid")
        previous_key = key
        existing = sha_metadata.get(reference.sha256)
        if existing is not None and existing != reference:
            _fail("asset_reference_metadata_conflict")
        sha_metadata[reference.sha256] = reference
        counts[reference] = counts.get(reference, 0) + 1

    expected = tuple(
        AssetReferenceMultiplicity(reference=reference, count=count)
        for reference, count in sorted(counts.items())
    )
    if value.multiplicities != expected:
        _fail("asset_reference_multiplicity_mismatch")
    digest = _projection_digest(
        value.project_id,
        value.generation,
        value.document_id,
        value.business_root_sha256,
        value.occurrences,
    )
    if type(value.projection_sha256) is not bytes or len(value.projection_sha256) != 32:
        _fail("asset_reference_projection_digest_invalid")
    if not hmac.compare_digest(digest, value.projection_sha256):
        _fail("asset_reference_projection_digest_mismatch")
    return value


def project_asset_references(snapshot: BusinessSnapshotSet) -> AssetReferenceProjection:
    """Verify one full snapshot and derive every schema-declared asset occurrence."""
    invalid_snapshot = False
    try:
        verified = verify_business_snapshot(snapshot)
        document_payload = decode_payload_bounded(
            verified.document.snapshot_bytes,
            MAX_DOCUMENT_SNAPSHOT_BYTES,
            MAX_SNAPSHOT_VALUE_NODES,
        )
    except (BusinessSnapshotError, BusinessCodecError):
        invalid_snapshot = True
        verified = None
        document_payload = None
    if invalid_snapshot:
        _fail("asset_reference_snapshot_invalid")
    assert verified is not None and document_payload is not None
    data_schema = document_payload["state"]["data_schema"]
    if type(data_schema) is not dict:
        _fail("asset_reference_schema_invalid")
    canonical_schemas: Dict[str, Dict[str, Any]] = {}
    invalid_schema = False
    try:
        for entity_type, schema in data_schema.items():
            if type(entity_type) is not str:
                _fail("asset_reference_schema_invalid")
            canonical_schemas[entity_type] = canonicalize_entity_schema(schema)
    except ZeError:
        invalid_schema = True
    if invalid_schema:
        _fail("asset_reference_schema_invalid")

    occurrences: List[AssetReferenceOccurrence] = []
    for row in verified.entities:
        schema = canonical_schemas.get(row.entity_type)
        if not schema:
            continue
        invalid_entity = False
        try:
            payload = decode_payload_bounded(
                row.snapshot_bytes,
                MAX_ENTITY_SNAPSHOT_BYTES,
                MAX_SNAPSHOT_VALUE_NODES,
            )
        except BusinessCodecError:
            invalid_entity = True
            payload = None
        if invalid_entity:
            _fail("asset_reference_snapshot_invalid")
        assert payload is not None
        by_path: Dict[str, AssetReferenceValue] = {}
        entity_data = payload["state"]["data"]
        if not _matches(entity_data, schema):
            _fail("asset_reference_schema_value_mismatch")
        _walk_schema(entity_data, schema, "", by_path)
        for path, reference in sorted(by_path.items()):
            occurrences.append(
                AssetReferenceOccurrence(
                    entity_type=row.entity_type,
                    entity_id=row.entity_id,
                    entity_deleted=bool(row.deleted),
                    value_path=path or "/",
                    reference=reference,
                )
            )
            if len(occurrences) > MAX_ATOMIC_ASSET_REFERENCES:
                _fail("asset_reference_count_exceeded")
    merged_by_sha: Dict[str, AssetReferenceValue] = {}
    for occurrence in occurrences:
        existing = merged_by_sha.get(occurrence.reference.sha256)
        merged_by_sha[occurrence.reference.sha256] = (
            occurrence.reference
            if existing is None
            else _merge_reference_metadata(existing, occurrence.reference)
        )
    normalized_occurrences = [
        AssetReferenceOccurrence(
            entity_type=occurrence.entity_type,
            entity_id=occurrence.entity_id,
            entity_deleted=occurrence.entity_deleted,
            value_path=occurrence.value_path,
            reference=merged_by_sha[occurrence.reference.sha256],
        )
        for occurrence in occurrences
    ]
    normalized_occurrences.sort(
        key=lambda item: (item.entity_type, item.entity_id, item.value_path)
    )
    frozen_occurrences = tuple(normalized_occurrences)
    counts: Dict[AssetReferenceValue, int] = {}
    sha_metadata: Dict[str, AssetReferenceValue] = {}
    for occurrence in frozen_occurrences:
        reference = occurrence.reference
        existing = sha_metadata.get(reference.sha256)
        sha_metadata[reference.sha256] = (
            reference
            if existing is None
            else _merge_reference_metadata(existing, reference)
        )
        counts[reference] = counts.get(reference, 0) + 1
    multiplicities = tuple(
        AssetReferenceMultiplicity(reference=reference, count=count)
        for reference, count in sorted(counts.items())
    )
    projection = AssetReferenceProjection(
        protocol=ASSET_REFERENCE_PROJECTION_PROTOCOL,
        project_id=verified.project.project_id,
        generation=verified.project.generation,
        document_id=verified.document.document_id,
        business_root_sha256=verified.project.snapshot_sha256,
        occurrences=frozen_occurrences,
        multiplicities=multiplicities,
        projection_sha256=_projection_digest(
            verified.project.project_id,
            verified.project.generation,
            verified.document.document_id,
            verified.project.snapshot_sha256,
            frozen_occurrences,
        ),
    )
    return _validate_projection(projection)


def verify_asset_reference_projection(value: Any) -> AssetReferenceProjection:
    return _validate_projection(value)


__all__ = [
    "ASSET_REFERENCE_HASH_DOMAIN",
    "ASSET_REFERENCE_PROJECTION_PROTOCOL",
    "AssetReferenceMultiplicity",
    "AssetReferenceOccurrence",
    "AssetReferenceProjection",
    "AssetReferenceProjectionError",
    "AssetReferenceValue",
    "MAX_ASSET_REFERENCE_EXTENSION_BYTES",
    "MAX_ASSET_REFERENCE_ID_BYTES",
    "MAX_ASSET_REFERENCE_MIME_BYTES",
    "project_asset_references",
    "verify_asset_reference_projection",
]
