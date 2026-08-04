"""Pure adversarial evidence for the v5 repository proof projection."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

import pytest


_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import legacy_repository_contract as contract
import legacy_repository_integrity as integrity_module
from business_codec import BusinessCodecError
from legacy_repository_integrity import (
    LegacyRepositoryIntegrityError,
    LegacyRepositoryIntegrityMember,
    build_legacy_repository_integrity,
)


_EMPTY_DIGEST = hashlib.sha256(b"").digest()


def _live(
    project_id: str = "project-a", **overrides: Any
) -> LegacyRepositoryIntegrityMember:
    values = {
        "project_id": project_id,
        "generation": 1,
        "status": contract.LEGACY_REPOSITORY_MEMBER_LIVE,
        "operation_id": contract.LEGACY_REPOSITORY_LEGACY_ADOPT_OPERATION_ID,
        "source_state_sha256": b"h" * 32,
        "source_state_bytes": 17,
        "catalog_ordinal": 0,
        "import_id": "a" * 64,
        "document_id": "document-a",
        "document_version": 7,
        "storage_version": "v2",
        "business_root_sha256": b"r" * 32,
        "entity_count": 2,
        "row_snapshot_bytes": 19,
    }
    values.update(overrides)
    return LegacyRepositoryIntegrityMember(**values)


def _deleted(
    project_id: str = "project-z", **overrides: Any
) -> LegacyRepositoryIntegrityMember:
    values = {
        "project_id": project_id,
        "generation": 9,
        "status": contract.LEGACY_REPOSITORY_MEMBER_DELETED,
        "operation_id": "1" * 32,
        "source_state_sha256": _EMPTY_DIGEST,
        "source_state_bytes": 0,
        "catalog_ordinal": None,
        "import_id": None,
        "document_id": None,
        "document_version": None,
        "storage_version": None,
        "business_root_sha256": None,
        "entity_count": None,
        "row_snapshot_bytes": None,
    }
    values.update(overrides)
    return LegacyRepositoryIntegrityMember(**values)


def _build(members: tuple[LegacyRepositoryIntegrityMember, ...], **overrides: Any):
    values = {
        "catalog_id": "c" * 64,
        "catalog_seal_sha256": b"s" * 32,
        "repository_epoch": "e" * 64,
        "repository_revision": 11,
        "catalog_revision": 11,
        "runtime_generation": "core-3.11.0+build-a",
        "code_digest": b"d" * 32,
        "members": members,
        "schema_fingerprint": "f" * 64,
    }
    values.update(overrides)
    return build_legacy_repository_integrity(**values)


def test_empty_repository_has_one_deterministic_non_authoritative_identity() -> None:
    first = _build(())
    second = _build(())
    assert first == second
    assert len(first.cutover_id) == 64
    assert first.transaction_id == (
        contract.LEGACY_REPOSITORY_TRANSACTION_PREFIX + first.cutover_id
    )
    assert (
        first.project_count,
        first.live_project_count,
        first.deleted_project_count,
    ) == (
        0,
        0,
        0,
    )
    assert first.source_state_bytes == 0
    assert b'"authority":"legacy"' in first.manifest_bytes
    assert b'"authority_state":"cutover_prepared"' in first.manifest_bytes
    assert b'"cutover_ready":0' in first.manifest_bytes


def test_live_and_deleted_members_bind_full_state_and_catalog_coverage() -> None:
    proof = _build((_live(), _deleted()))
    assert (
        proof.project_count,
        proof.live_project_count,
        proof.deleted_project_count,
    ) == (
        2,
        1,
        1,
    )
    assert proof.source_state_bytes == 17
    assert b'"status":"deleted"' in proof.manifest_bytes
    assert b'"source_state_bytes":0' in proof.manifest_bytes


@pytest.mark.parametrize(
    ("overrides", "code"),
    (
        ({"catalog_id": "C" * 64}, "catalog_id_invalid"),
        ({"catalog_seal_sha256": b"short"}, "catalog_seal_digest_invalid"),
        ({"repository_epoch": "x"}, "repository_epoch_invalid"),
        ({"repository_revision": True}, "repository_revision_invalid"),
        ({"catalog_revision": 10}, "repository_revision_invalid"),
        ({"runtime_generation": ""}, "runtime_generation_invalid"),
        ({"runtime_generation": "界" * 86}, "runtime_generation_invalid"),
        ({"code_digest": bytearray(b"d" * 32)}, "code_digest_invalid"),
        ({"schema_fingerprint": "F" * 64}, "schema_fingerprint_invalid"),
    ),
)
def test_top_level_values_are_exact(overrides: dict[str, Any], code: str) -> None:
    with pytest.raises(LegacyRepositoryIntegrityError) as caught:
        _build((), **overrides)
    assert caught.value.code == code


@pytest.mark.parametrize(
    ("member", "code"),
    (
        (_deleted(source_state_sha256=b"x" * 32), "deleted_member_invalid"),
        (_deleted(source_state_bytes=1), "deleted_member_invalid"),
        (_deleted(operation_id="legacy-adopt"), "deleted_member_invalid"),
        (_deleted(catalog_ordinal=0), "deleted_member_invalid"),
        (_live(source_state_bytes=0), "live_member_invalid"),
        (_live(operation_id="not-an-operation"), "live_member_invalid"),
        (_live(import_id="A" * 64), "live_member_invalid"),
        (_live(business_root_sha256=None), "live_member_invalid"),
        (_live(generation=2), "live_member_invalid"),
    ),
)
def test_member_union_is_fail_closed(
    member: LegacyRepositoryIntegrityMember, code: str
) -> None:
    with pytest.raises(LegacyRepositoryIntegrityError) as caught:
        _build((member,))
    assert caught.value.code == code


def test_members_require_exact_tuple_strict_order_and_dense_catalog_ordinals() -> None:
    with pytest.raises(LegacyRepositoryIntegrityError) as not_tuple:
        _build([_live()])  # type: ignore[arg-type]
    assert not_tuple.value.code == "members_invalid"

    with pytest.raises(LegacyRepositoryIntegrityError) as order:
        _build((_deleted("project-z"), _live("project-a")))
    assert order.value.code == "member_order_invalid"

    with pytest.raises(LegacyRepositoryIntegrityError) as coverage:
        _build((_live(catalog_ordinal=1),))
    assert coverage.value.code == "catalog_coverage_invalid"


def test_every_currentness_or_code_input_changes_the_content_address() -> None:
    baseline = _build((_live(), _deleted())).cutover_id
    variants = (
        _build((_live(generation=4, operation_id="2" * 32), _deleted())).cutover_id,
        _build((_live(source_state_sha256=b"x" * 32), _deleted())).cutover_id,
        _build((_live(), _deleted(generation=10))).cutover_id,
        _build(
            (_live(), _deleted()), repository_revision=12, catalog_revision=12
        ).cutover_id,
        _build((_live(), _deleted()), runtime_generation="other").cutover_id,
        _build((_live(), _deleted()), code_digest=b"x" * 32).cutover_id,
        _build((_live(), _deleted()), catalog_seal_sha256=b"x" * 32).cutover_id,
    )
    assert baseline not in variants
    assert len(set(variants)) == len(variants)


def test_namedtuple_subclasses_and_bool_integers_are_rejected() -> None:
    class MemberSubclass(LegacyRepositoryIntegrityMember):
        pass

    with pytest.raises(LegacyRepositoryIntegrityError) as subclass:
        _build((MemberSubclass(*_live()),))
    assert subclass.value.code == "member_invalid"
    with pytest.raises(LegacyRepositoryIntegrityError) as boolean:
        _build((_live(generation=True),))
    assert boolean.value.code == "member_invalid"


def test_manifest_node_formula_accepts_exact_ten_thousand_deleted_members() -> None:
    assert contract.MAX_LEGACY_REPOSITORY_MANIFEST_NODES == (
        contract.LEGACY_REPOSITORY_MANIFEST_ROOT_NODES
        + contract.LEGACY_REPOSITORY_MANIFEST_MEMBER_NODES
        * contract.MAX_LEGACY_REPOSITORY_PROJECTS
    )
    members = tuple(
        _deleted("project-%05d" % index, generation=index + 1)
        for index in range(contract.MAX_LEGACY_REPOSITORY_PROJECTS)
    )
    proof = _build(members)
    assert proof.project_count == contract.MAX_LEGACY_REPOSITORY_PROJECTS
    assert proof.live_project_count == 0
    assert proof.deleted_project_count == contract.MAX_LEGACY_REPOSITORY_PROJECTS
    assert len(proof.manifest_bytes) <= contract.MAX_LEGACY_REPOSITORY_MANIFEST_BYTES


def test_codec_failures_are_mapped_to_stable_integrity_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_codec(_value: Any, _maximum_bytes: int, _maximum_nodes: int) -> bytes:
        raise BusinessCodecError("business_codec_node_budget_exceeded", "$", "dict")

    monkeypatch.setattr(integrity_module, "encode_payload_bounded", fail_codec)
    with pytest.raises(LegacyRepositoryIntegrityError) as caught:
        _build(())
    assert caught.value.code == "manifest_encoding_failed"
    assert isinstance(caught.value.__cause__, BusinessCodecError)


def test_mixed_repository_proof_golden_vector_is_frozen() -> None:
    proof = _build((_live(), _deleted()))
    assert proof.cutover_id == (
        "4c0083fbf57dfe60a884d9bbdba63219" "ab7035c6454b7e0eee72e6083e5f3d1b"
    )
    assert proof.manifest_sha256.hex() == (
        "5ea6e2eb96efeb5144bc3460c2defe98" "5d0b2d877e0b2686ccba1aa732630032"
    )
    assert proof.source_repository_sha256.hex() == (
        "e20469168cfc21d40c2b7fe17595212e" "2adb064582cc43d13c3b7c1a236da9cc"
    )
    assert proof.catalog_coverage_sha256.hex() == (
        "c166fd7499ba043ba4bf0379bb5098954" "72d73760eb98e814aee9a1134ec5fb3"
    )
    assert proof.proof_sha256.hex() == (
        "81721f64f4365d7fdb17f4fa6c861c2" "a843662e5c0a80a77aa63a1d70366e964"
    )
    assert len(proof.manifest_bytes) == 2182
