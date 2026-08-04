"""Adversarial tests for the pure V11 cutover carrier preflight."""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
import inspect
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

import business_cutover_persistability as persistability
import test_business_cutover_asset_integrity as asset_support
import test_business_cutover_mount_integrity as mount_support
from business_cutover_asset_integrity import (
    _issue_business_cutover_asset_stage_plan,
)
from business_cutover_head_integrity import (
    issue_business_cutover_head_chain_from_mount,
)
from business_cutover_integrity import BusinessCutoverPlan
from business_cutover_persistability import (
    BUSINESS_CUTOVER_PERSISTABILITY_PROTOCOL,
    BUSINESS_CUTOVER_PERSISTABILITY_STATUS,
    BusinessCutoverPersistabilityCarrierKind,
    BusinessCutoverPersistabilityError,
    BusinessCutoverPersistabilityPhase,
    _measure_business_cutover_persistable_carrier,
    _require_business_cutover_activation_persistability,
    _require_business_cutover_asset_coverage_persistability,
    _require_business_cutover_asset_stage_persistability,
    _require_business_cutover_head_persistability,
    _require_business_cutover_install_persistability,
    _require_business_cutover_install_closure_persistability,
    _require_business_cutover_mount_persistability,
    _require_repository_audit_persistability,
)
from sqlite_schema import MAX_BUSINESS_SMALL_PAYLOAD_BYTES
from test_business_cutover_integrity import _plan as _cutover_plan
from test_business_cutover_install_closure import _projection as _v12_projection


def test_exact_v11_carrier_boundary_minus_one_boundary_and_plus_one() -> None:
    assert MAX_BUSINESS_SMALL_PAYLOAD_BYTES == 16 * 1024 * 1024
    for actual in (7, 8):
        measured = _measure_business_cutover_persistable_carrier(
            phase=BusinessCutoverPersistabilityPhase.INSTALL,
            carrier=BusinessCutoverPersistabilityCarrierKind.MANIFEST,
            ordinal=0,
            payload=b"x" * actual,
            _limit=8,
        )
        assert measured.actual_bytes == actual
        assert measured.limit_bytes == 8

    with pytest.raises(BusinessCutoverPersistabilityError) as exceeded:
        _measure_business_cutover_persistable_carrier(
            phase=BusinessCutoverPersistabilityPhase.INSTALL,
            carrier=BusinessCutoverPersistabilityCarrierKind.MANIFEST,
            ordinal=0,
            payload=b"x" * 9,
            _limit=8,
        )
    assert exceeded.value.code == (
        "business_cutover_persistability_payload_bytes_exceeded"
    )
    assert exceeded.value.phase == "install"
    assert exceeded.value.carrier == "manifest"
    assert exceeded.value.actual_bytes == 9
    assert exceeded.value.limit_bytes == 8
    assert str(exceeded.value) == exceeded.value.code
    assert exceeded.value.args == (exceeded.value.code,)


@pytest.mark.parametrize("payload", (b"", bytearray(b"x"), True, None))
def test_carrier_rejects_zero_length_and_non_exact_bytes(payload) -> None:
    with pytest.raises(BusinessCutoverPersistabilityError) as invalid:
        _measure_business_cutover_persistable_carrier(
            phase=BusinessCutoverPersistabilityPhase.ASSET_STAGE,
            carrier=BusinessCutoverPersistabilityCarrierKind.MANIFEST,
            ordinal=0,
            payload=payload,
            _limit=8,
        )
    assert invalid.value.code == ("business_cutover_persistability_payload_invalid")
    assert invalid.value.phase == "asset_stage"
    assert invalid.value.actual_bytes == 0
    assert invalid.value.limit_bytes == 8


def test_all_v11_phase_profiles_measure_the_exact_sealed_carriers() -> None:
    (
        cutover,
        snapshots,
        coverage,
        _coverage_observation,
        audit,
        activation,
        mount,
    ) = mount_support._bundle()
    stage = _issue_business_cutover_asset_stage_plan(
        cutover_plan=cutover,
        business_snapshots=snapshots,
        blob_carriers=(asset_support._carrier(asset_support.BLOB_A),),
    )
    head_chain = issue_business_cutover_head_chain_from_mount(mount.evidence)

    with pytest.raises(BusinessCutoverPersistabilityError) as install_blocked:
        _require_business_cutover_install_persistability(cutover)
    assert install_blocked.value.code == (
        "business_cutover_persistability_member_mapping_projection_unproven"
    )
    assert install_blocked.value.phase == "install"
    assert install_blocked.value.carrier == "member_mapping_projection"
    assert install_blocked.value.actual_bytes == 0
    assert install_blocked.value.limit_bytes == 0
    assert install_blocked.value.args == (install_blocked.value.code,)
    stage_profile = _require_business_cutover_asset_stage_persistability(stage)
    coverage_profile = _require_business_cutover_asset_coverage_persistability(coverage)
    audit_profile = _require_repository_audit_persistability(audit)
    activation_profile = _require_business_cutover_activation_persistability(activation)
    mount_profile = _require_business_cutover_mount_persistability(mount)
    head_profile = _require_business_cutover_head_persistability(head_chain)

    expected_single_carriers = (
        (stage_profile, stage.manifest_bytes),
        (coverage_profile, coverage.manifest_bytes),
        (audit_profile, audit.evidence.manifest_bytes),
        (activation_profile, activation.evidence.manifest_bytes),
        (mount_profile, mount.evidence.manifest_bytes),
    )
    for profile, manifest_bytes in expected_single_carriers:
        assert profile.cutover_id == cutover.cutover_id
        assert len(profile.carriers) == 1
        assert profile.carriers[0].carrier is (
            BusinessCutoverPersistabilityCarrierKind.MANIFEST
        )
        assert profile.carriers[0].actual_bytes == len(manifest_bytes)
        assert profile.carriers[0].limit_bytes == (MAX_BUSINESS_SMALL_PAYLOAD_BYTES)

    assert tuple(value.ordinal for value in head_profile.carriers) == (
        1,
        2,
        3,
        4,
        5,
    )
    assert tuple(value.actual_bytes for value in head_profile.carriers) == tuple(
        len(value.manifest_bytes) for value in head_chain
    )
    assert all(
        value.carrier is BusinessCutoverPersistabilityCarrierKind.PHASE_HEAD_MANIFEST
        for value in head_profile.carriers
    )

    profiles = (
        stage_profile,
        coverage_profile,
        audit_profile,
        activation_profile,
        mount_profile,
        head_profile,
    )
    assert tuple(value.phase for value in profiles) == tuple(
        value
        for value in BusinessCutoverPersistabilityPhase
        if value is not BusinessCutoverPersistabilityPhase.INSTALL
    )
    assert all(
        value.protocol == BUSINESS_CUTOVER_PERSISTABILITY_PROTOCOL
        and value.status == BUSINESS_CUTOVER_PERSISTABILITY_STATUS
        and value.total_carrier_bytes
        == sum(carrier.actual_bytes for carrier in value.carriers)
        for value in profiles
    )
    with pytest.raises(FrozenInstanceError):
        stage_profile.status = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        stage_profile.carriers[0].actual_bytes = 1  # type: ignore[misc]


def test_install_measures_only_its_real_manifest_then_blocks_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _cutover_plan()
    observed = []
    original = persistability._measure_business_cutover_persistable_carrier

    def _record_measurement(**values):
        observed.append(values)
        return original(**values)

    monkeypatch.setattr(
        persistability,
        "_measure_business_cutover_persistable_carrier",
        _record_measurement,
    )
    with pytest.raises(BusinessCutoverPersistabilityError) as blocked:
        _require_business_cutover_install_persistability(plan)
    assert blocked.value.code == (
        "business_cutover_persistability_member_mapping_projection_unproven"
    )
    assert len(observed) == 1
    assert observed[0]["phase"] is BusinessCutoverPersistabilityPhase.INSTALL
    assert observed[0]["carrier"] is (BusinessCutoverPersistabilityCarrierKind.MANIFEST)
    assert observed[0]["payload"] == plan.closure.install_manifest_bytes
    assert plan.closure.member_mapping_bytes not in tuple(
        value["payload"] for value in observed
    )


def test_v12_install_closure_measures_every_real_sql_carrier() -> None:
    _plan, _baseline, closure = _v12_projection()
    profile = _require_business_cutover_install_closure_persistability(closure)

    expected = [
        (
            BusinessCutoverPersistabilityCarrierKind.MANIFEST,
            0,
            len(closure._plan.closure.install_manifest_bytes),
        ),
    ]
    expected.extend(
        (
            BusinessCutoverPersistabilityCarrierKind.MEMBER_MAPPING_CHUNK,
            value.ordinal,
            len(value.chunk_bytes),
        )
        for value in closure.member_mapping.chunks
    )
    expected.extend(
        (
            BusinessCutoverPersistabilityCarrierKind.BASELINE_PROJECTION_CHUNK,
            value.ordinal,
            len(value.chunk_bytes),
        )
        for value in closure.baseline_projection.chunks
    )
    expected.extend(
        (
            BusinessCutoverPersistabilityCarrierKind.BASELINE_INNER_MANIFEST,
            value.member_ordinal,
            len(value.manifest_bytes),
        )
        for value in closure.baseline_receipts
    )
    expected.extend(
        (
            BusinessCutoverPersistabilityCarrierKind.BASELINE_OUTER_MANIFEST,
            value.member_ordinal,
            len(value.baseline_outer_manifest_bytes),
        )
        for value in closure.baseline_receipts
    )
    expected.append((
        BusinessCutoverPersistabilityCarrierKind.INSTALL_CLOSURE_MANIFEST,
        0,
        len(closure.closure.manifest_bytes),
    ))
    assert profile.phase is BusinessCutoverPersistabilityPhase.INSTALL
    assert profile.cutover_id == closure.cutover_id
    assert closure._coverage_plan.cutover_id == closure.cutover_id
    assert closure.closure.coverage_manifest_sha256 == (
        closure._coverage_plan.manifest_sha256
    )
    assert tuple(
        (value.carrier, value.ordinal, value.actual_bytes)
        for value in profile.carriers
    ) == tuple(expected)


def test_v12_persistability_revalidates_coverage_before_measuring_carriers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plan, _baseline, closure = _v12_projection()
    object.__setattr__(
        closure._coverage_plan,
        "manifest_sha256",
        b"t" * 32,
    )

    def _measurement_must_not_run(**_values):
        raise AssertionError("carrier measurement ran before coverage validation")

    monkeypatch.setattr(
        persistability,
        "_measure_business_cutover_persistable_carrier",
        _measurement_must_not_run,
    )
    with pytest.raises(BusinessCutoverPersistabilityError) as invalid:
        _require_business_cutover_install_closure_persistability(closure)
    assert invalid.value.code == "business_cutover_persistability_plan_invalid"
    assert invalid.value.phase == "install"


def test_tampered_plan_is_rejected_before_any_carrier_measurement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _cutover_plan()
    object.__setattr__(
        plan,
        "closure",
        plan.closure._replace(install_manifest_bytes=b"changed"),
    )

    def _measurement_must_not_run(**_values):
        raise AssertionError("carrier measurement ran before seal validation")

    monkeypatch.setattr(
        persistability,
        "_measure_business_cutover_persistable_carrier",
        _measurement_must_not_run,
    )
    with pytest.raises(BusinessCutoverPersistabilityError) as invalid:
        _require_business_cutover_install_persistability(plan)
    assert invalid.value.code == ("business_cutover_persistability_plan_invalid")
    assert invalid.value.phase == "install"
    assert invalid.value.actual_bytes == 0
    assert invalid.value.carrier == ""


def test_unsealed_and_wrong_plan_types_fail_closed() -> None:
    unsealed = object.__new__(BusinessCutoverPlan)
    for value in (unsealed, None, True, object()):
        with pytest.raises(BusinessCutoverPersistabilityError) as invalid:
            _require_business_cutover_install_persistability(value)
        assert invalid.value.code == ("business_cutover_persistability_plan_invalid")

    phase_functions = (
        (
            _require_business_cutover_asset_stage_persistability,
            "asset_stage",
        ),
        (
            _require_business_cutover_asset_coverage_persistability,
            "asset_coverage",
        ),
        (_require_repository_audit_persistability, "audit"),
        (_require_business_cutover_activation_persistability, "activation"),
        (_require_business_cutover_mount_persistability, "mount"),
        (_require_business_cutover_head_persistability, "head"),
    )
    for require, phase in phase_functions:
        with pytest.raises(BusinessCutoverPersistabilityError) as invalid:
            require(None)
        assert invalid.value.code == ("business_cutover_persistability_plan_invalid")
        assert invalid.value.phase == phase


def test_persistability_boundary_has_no_io_ports_or_runtime_authority() -> None:
    source = inspect.getsource(persistability)
    for forbidden in (
        "import sqlite3",
        "from pathlib",
        "import os",
        "import time",
        "datetime.now",
    ):
        assert forbidden not in source
    tree = ast.parse(source)
    forbidden_calls = {"open", "exec", "eval", "compile", "input"}
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in forbidden_calls
    ]
    assert (
        "connection"
        not in inspect.signature(
            _require_business_cutover_install_persistability
        ).parameters
    )
    assert (
        "path"
        not in inspect.signature(
            _require_business_cutover_install_persistability
        ).parameters
    )
    assert persistability.__all__ == ()
