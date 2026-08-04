"""Pure canonical integrity projection for one v5 repository proof.

Inputs must come from one strict, durable repository snapshot while the global
legacy mutation barrier remains held.  This leaf validates exact scalar
shapes, canonical ordering and quotas, then computes domain-separated hashes.
It performs no filesystem, SQLite or SDK I/O and cannot change authority.
"""

from __future__ import annotations

import hashlib
from typing import Any, NamedTuple, Optional, Tuple

from business_codec import BusinessCodecError, encode_payload_bounded
from hash_framing import update_hash_frame
from legacy_import_contract import MAX_LEGACY_ENTITIES
from legacy_repository_contract import (
    LEGACY_REPOSITORY_AUTHORITY,
    LEGACY_REPOSITORY_AUTHORITY_STATE,
    LEGACY_REPOSITORY_CUTOVER_READY,
    LEGACY_REPOSITORY_LEGACY_ADOPT_OPERATION_ID,
    LEGACY_REPOSITORY_MANIFEST_SCHEMA,
    LEGACY_REPOSITORY_MEMBER_DELETED,
    LEGACY_REPOSITORY_MEMBER_LIVE,
    LEGACY_REPOSITORY_PROOF_PROTOCOL,
    LEGACY_REPOSITORY_RECEIPT_PROTOCOL,
    LEGACY_REPOSITORY_SELECTION,
    LEGACY_REPOSITORY_STATUS,
    LEGACY_REPOSITORY_TARGET_SCHEMA_VERSION,
    LEGACY_REPOSITORY_TRANSACTION_KIND,
    LEGACY_REPOSITORY_TRANSACTION_PREFIX,
    MAX_LEGACY_REPOSITORY_MANIFEST_BYTES,
    MAX_LEGACY_REPOSITORY_MANIFEST_NODES,
    MAX_LEGACY_REPOSITORY_PROJECTS,
    MAX_LEGACY_REPOSITORY_PROJECT_SOURCE_STATE_BYTES,
    MAX_LEGACY_REPOSITORY_RUNTIME_GENERATION_BYTES,
    MAX_LEGACY_REPOSITORY_SOURCE_STATE_BYTES,
    MAX_LEGACY_REPOSITORY_TARGET_SNAPSHOT_BYTES,
)


LEGACY_REPOSITORY_SOURCE_HASH_DOMAIN = b"ze.legacy-repository.source.v1"
LEGACY_REPOSITORY_CATALOG_HASH_DOMAIN = b"ze.legacy-repository.catalog.v1"
LEGACY_REPOSITORY_ID_HASH_DOMAIN = b"ze.legacy-repository.id.v1"
LEGACY_REPOSITORY_MANIFEST_HASH_DOMAIN = LEGACY_REPOSITORY_MANIFEST_SCHEMA.encode(
    "ascii"
)
LEGACY_REPOSITORY_PROOF_HASH_DOMAIN = LEGACY_REPOSITORY_PROOF_PROTOCOL.encode("ascii")

_LOWER_HEX = frozenset("0123456789abcdef")
_MAX_SQLITE_INTEGER = 2**63 - 1
_STORAGE_VERSIONS = frozenset(("legacy", "v2"))
_EMPTY_SOURCE_SHA256 = hashlib.sha256(b"").digest()


class LegacyRepositoryIntegrityError(ValueError):
    """Stable, payload-free validation failure at the pure proof boundary."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


class LegacyRepositoryIntegrityMember(NamedTuple):
    """One canonical live descriptor or deleted generation high-water."""

    project_id: str
    generation: int
    status: str
    operation_id: str
    source_state_sha256: bytes
    source_state_bytes: int
    catalog_ordinal: Optional[int]
    import_id: Optional[str]
    document_id: Optional[str]
    document_version: Optional[int]
    storage_version: Optional[str]
    business_root_sha256: Optional[bytes]
    entity_count: Optional[int]
    row_snapshot_bytes: Optional[int]


class LegacyRepositoryIntegrity(NamedTuple):
    """Complete content-addressed projection persisted by a future sealer."""

    cutover_id: str
    transaction_id: str
    catalog_id: str
    repository_epoch: str
    repository_revision: int
    catalog_revision: int
    runtime_generation: str
    code_digest: bytes
    project_count: int
    live_project_count: int
    deleted_project_count: int
    source_state_bytes: int
    manifest_bytes: bytes
    manifest_sha256: bytes
    source_repository_sha256: bytes
    catalog_coverage_sha256: bytes
    proof_sha256: bytes


def _fail(code: str) -> None:
    raise LegacyRepositoryIntegrityError(code)


def _is_hex(value: Any, length: int) -> bool:
    return (
        type(value) is str
        and len(value) == length
        and all(character in _LOWER_HEX for character in value)
    )


def _utf8(value: str, code: str) -> bytes:
    try:
        return value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        _fail(code)
    raise AssertionError("unreachable")


def _frame(hasher: Any, value: bytes) -> None:
    update_hash_frame(hasher, value)


def _integer(hasher: Any, value: int) -> None:
    _frame(hasher, value.to_bytes(8, "big"))


def _digest(domain: bytes, *parts: bytes) -> bytes:
    hasher = hashlib.sha256()
    _frame(hasher, domain)
    for part in parts:
        _frame(hasher, part)
    return hasher.digest()


def _operation_id_valid(value: Any, *, allow_legacy_adopt: bool) -> bool:
    return _is_hex(value, 32) or (
        allow_legacy_adopt
        and type(value) is str
        and value == LEGACY_REPOSITORY_LEGACY_ADOPT_OPERATION_ID
    )


def _validate_member(value: Any) -> LegacyRepositoryIntegrityMember:
    if type(value) is not LegacyRepositoryIntegrityMember:
        _fail("member_invalid")
    if (
        type(value.project_id) is not str
        or not 1 <= len(value.project_id) <= 160
        or type(value.generation) is not int
        or not 1 <= value.generation <= _MAX_SQLITE_INTEGER
        or type(value.status) is not str
        or value.status
        not in (LEGACY_REPOSITORY_MEMBER_LIVE, LEGACY_REPOSITORY_MEMBER_DELETED)
        or type(value.source_state_sha256) is not bytes
        or len(value.source_state_sha256) != 32
        or type(value.source_state_bytes) is not int
        or not 0
        <= value.source_state_bytes
        <= MAX_LEGACY_REPOSITORY_PROJECT_SOURCE_STATE_BYTES
    ):
        _fail("member_invalid")
    _utf8(value.project_id, "member_invalid")

    live_values = (
        value.catalog_ordinal,
        value.import_id,
        value.document_id,
        value.document_version,
        value.storage_version,
        value.business_root_sha256,
        value.entity_count,
        value.row_snapshot_bytes,
    )
    if value.status == LEGACY_REPOSITORY_MEMBER_DELETED:
        if (
            not _operation_id_valid(value.operation_id, allow_legacy_adopt=False)
            or value.source_state_bytes != 0
            or value.source_state_sha256 != _EMPTY_SOURCE_SHA256
            or any(item is not None for item in live_values)
        ):
            _fail("deleted_member_invalid")
        return value

    if (
        not _operation_id_valid(value.operation_id, allow_legacy_adopt=True)
        or (
            value.operation_id == LEGACY_REPOSITORY_LEGACY_ADOPT_OPERATION_ID
            and value.generation != 1
        )
        or value.source_state_bytes < 1
        or type(value.catalog_ordinal) is not int
        or not 0 <= value.catalog_ordinal < MAX_LEGACY_REPOSITORY_PROJECTS
        or not _is_hex(value.import_id, 64)
        or type(value.document_id) is not str
        or not value.document_id
        or type(value.document_version) is not int
        or not 1 <= value.document_version <= _MAX_SQLITE_INTEGER
        or type(value.storage_version) is not str
        or value.storage_version not in _STORAGE_VERSIONS
        or type(value.business_root_sha256) is not bytes
        or len(value.business_root_sha256) != 32
        or type(value.entity_count) is not int
        or not 0 <= value.entity_count <= MAX_LEGACY_ENTITIES
        or type(value.row_snapshot_bytes) is not int
        or not 1
        <= value.row_snapshot_bytes
        <= MAX_LEGACY_REPOSITORY_TARGET_SNAPSHOT_BYTES
    ):
        _fail("live_member_invalid")
    _utf8(value.document_id, "live_member_invalid")
    return value


def _verified_members(
    members: Any,
) -> Tuple[Tuple[LegacyRepositoryIntegrityMember, ...], int, int, int]:
    if type(members) is not tuple:
        _fail("members_invalid")
    if len(members) > MAX_LEGACY_REPOSITORY_PROJECTS:
        _fail("project_count_exceeded")
    verified = tuple(_validate_member(member) for member in members)
    previous_project_id: Optional[str] = None
    live_count = 0
    deleted_count = 0
    source_state_bytes = 0
    catalog_ordinals = set()
    document_ids = set()
    for member in verified:
        if previous_project_id is not None and member.project_id <= previous_project_id:
            _fail("member_order_invalid")
        previous_project_id = member.project_id
        if (
            source_state_bytes + member.source_state_bytes
            > MAX_LEGACY_REPOSITORY_SOURCE_STATE_BYTES
        ):
            _fail("source_state_bytes_exceeded")
        source_state_bytes += member.source_state_bytes
        if member.status == LEGACY_REPOSITORY_MEMBER_LIVE:
            if (
                member.catalog_ordinal in catalog_ordinals
                or member.document_id in document_ids
            ):
                _fail("live_member_duplicate")
            catalog_ordinals.add(member.catalog_ordinal)
            document_ids.add(member.document_id)
            live_count += 1
        else:
            deleted_count += 1
    if catalog_ordinals != set(range(live_count)):
        _fail("catalog_coverage_invalid")
    return verified, live_count, deleted_count, source_state_bytes


def _source_digest(
    members: Tuple[LegacyRepositoryIntegrityMember, ...],
    repository_epoch: str,
    repository_revision: int,
) -> bytes:
    hasher = hashlib.sha256()
    _frame(hasher, LEGACY_REPOSITORY_SOURCE_HASH_DOMAIN)
    _frame(hasher, repository_epoch.encode("ascii"))
    _integer(hasher, repository_revision)
    _integer(hasher, len(members))
    for ordinal, member in enumerate(members):
        _integer(hasher, ordinal)
        _frame(hasher, _utf8(member.project_id, "member_invalid"))
        _integer(hasher, member.generation)
        _frame(hasher, member.status.encode("ascii"))
        _frame(hasher, member.operation_id.encode("ascii"))
        _frame(hasher, member.source_state_sha256)
        _integer(hasher, member.source_state_bytes)
    return hasher.digest()


def _catalog_digest(
    catalog_id: str,
    catalog_seal_sha256: bytes,
    members: Tuple[LegacyRepositoryIntegrityMember, ...],
) -> bytes:
    live = tuple(
        member for member in members if member.status == LEGACY_REPOSITORY_MEMBER_LIVE
    )
    hasher = hashlib.sha256()
    _frame(hasher, LEGACY_REPOSITORY_CATALOG_HASH_DOMAIN)
    _frame(hasher, catalog_id.encode("ascii"))
    _frame(hasher, catalog_seal_sha256)
    _integer(hasher, len(live))
    for member in sorted(live, key=lambda item: item.catalog_ordinal):
        assert member.catalog_ordinal is not None
        assert member.import_id is not None
        assert member.document_id is not None
        assert member.document_version is not None
        assert member.storage_version is not None
        assert member.business_root_sha256 is not None
        assert member.entity_count is not None
        assert member.row_snapshot_bytes is not None
        _integer(hasher, member.catalog_ordinal)
        _frame(hasher, _utf8(member.project_id, "member_invalid"))
        _integer(hasher, member.generation)
        _frame(hasher, member.import_id.encode("ascii"))
        _frame(hasher, _utf8(member.document_id, "live_member_invalid"))
        _integer(hasher, member.document_version)
        _frame(hasher, member.storage_version.encode("ascii"))
        _frame(hasher, member.source_state_sha256)
        _integer(hasher, member.source_state_bytes)
        _frame(hasher, member.business_root_sha256)
        _integer(hasher, member.entity_count)
        _integer(hasher, member.row_snapshot_bytes)
    return hasher.digest()


def _member_manifest(
    member: LegacyRepositoryIntegrityMember, ordinal: int
) -> dict[str, Any]:
    return {
        "ordinal": ordinal,
        "project_id": member.project_id,
        "generation": member.generation,
        "status": member.status,
        "operation_id": member.operation_id,
        "source_state_sha256": member.source_state_sha256.hex(),
        "source_state_bytes": member.source_state_bytes,
        "catalog_ordinal": member.catalog_ordinal,
        "import_id": member.import_id,
        "document_id": member.document_id,
        "document_version": member.document_version,
        "storage_version": member.storage_version,
        "business_root_sha256": (
            None
            if member.business_root_sha256 is None
            else member.business_root_sha256.hex()
        ),
        "entity_count": member.entity_count,
        "row_snapshot_bytes": member.row_snapshot_bytes,
    }


def build_legacy_repository_integrity(
    *,
    catalog_id: str,
    catalog_seal_sha256: bytes,
    repository_epoch: str,
    repository_revision: int,
    catalog_revision: int,
    runtime_generation: str,
    code_digest: bytes,
    members: Tuple[LegacyRepositoryIntegrityMember, ...],
    schema_fingerprint: str,
) -> LegacyRepositoryIntegrity:
    """Build a non-authoritative, content-addressed v5 repository proof."""

    if not _is_hex(catalog_id, 64):
        _fail("catalog_id_invalid")
    if type(catalog_seal_sha256) is not bytes or len(catalog_seal_sha256) != 32:
        _fail("catalog_seal_digest_invalid")
    if not _is_hex(repository_epoch, 64):
        _fail("repository_epoch_invalid")
    if (
        type(repository_revision) is not int
        or not 1 <= repository_revision <= _MAX_SQLITE_INTEGER
        or type(catalog_revision) is not int
        or catalog_revision != repository_revision
    ):
        _fail("repository_revision_invalid")
    if type(runtime_generation) is not str or not runtime_generation:
        _fail("runtime_generation_invalid")
    runtime_bytes = _utf8(runtime_generation, "runtime_generation_invalid")
    if len(runtime_bytes) > MAX_LEGACY_REPOSITORY_RUNTIME_GENERATION_BYTES:
        _fail("runtime_generation_invalid")
    if type(code_digest) is not bytes or len(code_digest) != 32:
        _fail("code_digest_invalid")
    if not _is_hex(schema_fingerprint, 64):
        _fail("schema_fingerprint_invalid")

    verified, live_count, deleted_count, source_state_bytes = _verified_members(members)
    source_repository_sha256 = _source_digest(
        verified, repository_epoch, repository_revision
    )
    catalog_coverage_sha256 = _catalog_digest(catalog_id, catalog_seal_sha256, verified)
    cutover_id = _digest(
        LEGACY_REPOSITORY_ID_HASH_DOMAIN,
        catalog_id.encode("ascii"),
        catalog_seal_sha256,
        repository_epoch.encode("ascii"),
        repository_revision.to_bytes(8, "big"),
        catalog_revision.to_bytes(8, "big"),
        runtime_bytes,
        code_digest,
        source_repository_sha256,
        catalog_coverage_sha256,
        len(verified).to_bytes(8, "big"),
        live_count.to_bytes(8, "big"),
        deleted_count.to_bytes(8, "big"),
        source_state_bytes.to_bytes(8, "big"),
        LEGACY_REPOSITORY_TARGET_SCHEMA_VERSION.to_bytes(8, "big"),
        schema_fingerprint.encode("ascii"),
    ).hex()
    transaction_id = LEGACY_REPOSITORY_TRANSACTION_PREFIX + cutover_id
    try:
        manifest_bytes = encode_payload_bounded(
            {
                "schema": LEGACY_REPOSITORY_MANIFEST_SCHEMA,
                "protocol": LEGACY_REPOSITORY_PROOF_PROTOCOL,
                "receipt_protocol": LEGACY_REPOSITORY_RECEIPT_PROTOCOL,
                "receipt_kind": LEGACY_REPOSITORY_TRANSACTION_KIND,
                "cutover_id": cutover_id,
                "catalog_id": catalog_id,
                "catalog_seal_sha256": catalog_seal_sha256.hex(),
                "repository_epoch": repository_epoch,
                "repository_revision": repository_revision,
                "catalog_revision": catalog_revision,
                "runtime_generation": runtime_generation,
                "code_digest": code_digest.hex(),
                "authority": LEGACY_REPOSITORY_AUTHORITY,
                "authority_state": LEGACY_REPOSITORY_AUTHORITY_STATE,
                "status": LEGACY_REPOSITORY_STATUS,
                "selection": LEGACY_REPOSITORY_SELECTION,
                "cutover_ready": LEGACY_REPOSITORY_CUTOVER_READY,
                "target_schema_version": LEGACY_REPOSITORY_TARGET_SCHEMA_VERSION,
                "schema_fingerprint": schema_fingerprint,
                "project_count": len(verified),
                "live_project_count": live_count,
                "deleted_project_count": deleted_count,
                "source_state_bytes": source_state_bytes,
                "source_repository_sha256": source_repository_sha256.hex(),
                "catalog_coverage_sha256": catalog_coverage_sha256.hex(),
                "members": [
                    _member_manifest(member, ordinal)
                    for ordinal, member in enumerate(verified)
                ],
            },
            MAX_LEGACY_REPOSITORY_MANIFEST_BYTES,
            MAX_LEGACY_REPOSITORY_MANIFEST_NODES,
        )
    except BusinessCodecError as exc:
        raise LegacyRepositoryIntegrityError("manifest_encoding_failed") from exc
    manifest_sha256 = _digest(LEGACY_REPOSITORY_MANIFEST_HASH_DOMAIN, manifest_bytes)
    proof_sha256 = _digest(
        LEGACY_REPOSITORY_PROOF_HASH_DOMAIN,
        cutover_id.encode("ascii"),
        transaction_id.encode("ascii"),
        manifest_bytes,
        manifest_sha256,
        source_repository_sha256,
        catalog_coverage_sha256,
        code_digest,
        schema_fingerprint.encode("ascii"),
    )
    return LegacyRepositoryIntegrity(
        cutover_id=cutover_id,
        transaction_id=transaction_id,
        catalog_id=catalog_id,
        repository_epoch=repository_epoch,
        repository_revision=repository_revision,
        catalog_revision=catalog_revision,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
        project_count=len(verified),
        live_project_count=live_count,
        deleted_project_count=deleted_count,
        source_state_bytes=source_state_bytes,
        manifest_bytes=manifest_bytes,
        manifest_sha256=manifest_sha256,
        source_repository_sha256=source_repository_sha256,
        catalog_coverage_sha256=catalog_coverage_sha256,
        proof_sha256=proof_sha256,
    )


__all__ = [
    "LEGACY_REPOSITORY_CATALOG_HASH_DOMAIN",
    "LEGACY_REPOSITORY_ID_HASH_DOMAIN",
    "LEGACY_REPOSITORY_MANIFEST_HASH_DOMAIN",
    "LEGACY_REPOSITORY_PROOF_HASH_DOMAIN",
    "LEGACY_REPOSITORY_SOURCE_HASH_DOMAIN",
    "LegacyRepositoryIntegrity",
    "LegacyRepositoryIntegrityError",
    "LegacyRepositoryIntegrityMember",
    "build_legacy_repository_integrity",
]
