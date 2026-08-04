"""Exact and adversarial tests for the shared stored-manifest boundary."""

from __future__ import annotations

import copy
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


from business_codec import decode_payload_bounded, encode_payload_bounded
from business_cutover_asset_contract import (
    MAX_BUSINESS_CUTOVER_ASSET_MANIFEST_BYTES,
    MAX_BUSINESS_CUTOVER_ASSET_MANIFEST_NODES,
)
from business_cutover_contract import (
    MAX_BUSINESS_CUTOVER_MANIFEST_BYTES,
    MAX_BUSINESS_CUTOVER_MANIFEST_NODES,
)
from business_cutover_stored_manifest import (
    BusinessCutoverStoredManifestError,
    _parse_stored_coverage_manifest,
    _parse_stored_install_manifest,
)
from test_business_cutover_install_closure import _coverage_for, _v12_plan


def _install_bytes() -> bytes:
    return _v12_plan().install.manifest_bytes


def _coverage_bytes() -> bytes:
    plan = _v12_plan()
    return _coverage_for(plan).manifest_bytes


def _changed(
    payload: bytes,
    path: tuple[object, ...],
    value: object,
    *,
    maximum_bytes: int,
    maximum_nodes: int,
) -> bytes:
    root = copy.deepcopy(decode_payload_bounded(payload, maximum_bytes, maximum_nodes))
    selected = root
    for part in path[:-1]:
        selected = selected[part]
    selected[path[-1]] = value
    return encode_payload_bounded(root, maximum_bytes, maximum_nodes)


def test_exact_install_projection_exposes_full_private_proof() -> None:
    plan = _v12_plan()
    projection = _parse_stored_install_manifest(plan.install.manifest_bytes)

    assert projection.evidence == plan.install
    assert projection.source_admission_sha256 == (plan.closure.source_admission_sha256)
    assert projection.pending_admission_sha256 == (
        plan.closure.pending_admission_sha256
    )
    assert projection.final_admission_sha256 == (plan.closure.final_admission_sha256)
    assert projection.legacy_proof["proof_sha256"] == (
        plan.legacy_proof.proof_sha256.hex()
    )


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("admissions", "source", "revision"), True),
        (("legacy_proof", "project_count"), "1"),
        (("member_mapping", "live_project_count"), []),
        (("runtime", "code_digest"), None),
        (("expected_target", "schema_version"), {}),
        (("transaction",), []),
    ),
)
def test_install_nested_malformed_values_have_one_stable_error(
    path: tuple[object, ...], value: object
) -> None:
    changed = _changed(
        _install_bytes(),
        path,
        value,
        maximum_bytes=MAX_BUSINESS_CUTOVER_MANIFEST_BYTES,
        maximum_nodes=MAX_BUSINESS_CUTOVER_MANIFEST_NODES,
    )

    with pytest.raises(BusinessCutoverStoredManifestError) as caught:
        _parse_stored_install_manifest(changed)
    assert caught.value.code == "cutover_stored_manifest_invalid"


def test_exact_coverage_projection_preserves_root_for_deep_revalidation() -> None:
    plan = _v12_plan()
    coverage = _coverage_for(plan)
    projection = _parse_stored_coverage_manifest(coverage.manifest_bytes)

    assert projection.evidence == coverage.expected_evidence
    assert projection.root["coverage"]["coverage_sha256"] == (
        coverage.coverage.coverage_sha256.hex()
    )
    assert projection.root["repository"]["members"]


@pytest.mark.parametrize("value", (True, "0", [], None))
def test_coverage_untrusted_summary_arithmetic_has_one_stable_error(
    value: object,
) -> None:
    changed = _changed(
        _coverage_bytes(),
        ("observed", "project_row_count"),
        value,
        maximum_bytes=MAX_BUSINESS_CUTOVER_ASSET_MANIFEST_BYTES,
        maximum_nodes=MAX_BUSINESS_CUTOVER_ASSET_MANIFEST_NODES,
    )

    with pytest.raises(BusinessCutoverStoredManifestError) as caught:
        _parse_stored_coverage_manifest(changed)
    assert caught.value.code == "cutover_stored_manifest_invalid"


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("authority", "source_admission_revision"), []),
        (("repository", "project_count"), "1"),
        (("coverage", "reference_count"), None),
        (("limits", "max_blob_bytes"), False),
        (("transaction",), "bad"),
        (("projects",), {}),
    ),
)
def test_coverage_nested_malformed_values_have_one_stable_error(
    path: tuple[object, ...], value: object
) -> None:
    changed = _changed(
        _coverage_bytes(),
        path,
        value,
        maximum_bytes=MAX_BUSINESS_CUTOVER_ASSET_MANIFEST_BYTES,
        maximum_nodes=MAX_BUSINESS_CUTOVER_ASSET_MANIFEST_NODES,
    )

    with pytest.raises(BusinessCutoverStoredManifestError) as caught:
        _parse_stored_coverage_manifest(changed)
    assert caught.value.code == "cutover_stored_manifest_invalid"
