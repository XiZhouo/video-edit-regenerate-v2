import dataclasses

import pytest

from business_cutover_activation_contract import (
    BUSINESS_CUTOVER_ACTIVATION_TRANSACTION_KIND,
    BUSINESS_CUTOVER_ACTIVATION_TRANSACTION_PREFIX,
)
from business_cutover_asset_contract import (
    BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_KIND,
    BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_PREFIX,
)
from business_cutover_contract import (
    BUSINESS_CUTOVER_INSTALL_TRANSACTION_KIND,
    BUSINESS_CUTOVER_INSTALL_TRANSACTION_PREFIX,
)
from business_cutover_head_integrity import (
    BUSINESS_CUTOVER_HEAD_HASH_DOMAIN,
    EMPTY_BUSINESS_CUTOVER_HEAD_SHA256,
    BusinessCutoverHeadInput,
    BusinessCutoverHeadIntegrityError,
    BusinessCutoverHeadPointer,
    BusinessCutoverHeadPointerRole,
    _advance_business_cutover_head,
    _issue_initial_business_cutover_head,
    _require_business_cutover_head_chain,
    _require_exact_business_cutover_head_predecessor,
    _state_digest,
    issue_business_cutover_head_chain,
)
from business_cutover_mount_contract import (
    BUSINESS_CUTOVER_MOUNT_TRANSACTION_KIND,
    BUSINESS_CUTOVER_MOUNT_TRANSACTION_PREFIX,
)
from repository_audit_contract import (
    REPOSITORY_AUDIT_TRANSACTION_KIND,
    REPOSITORY_AUDIT_TRANSACTION_PREFIX,
)


CUTOVER_ID = "c" * 64
EPOCH = "e" * 64
CODE = bytes.fromhex("12" * 32)
T_COVERAGE = "2026-07-12T00:00:01.000Z"
T_INSTALL = "2026-07-12T00:00:02.000Z"
T_AUDIT = "2026-07-12T00:00:03.000Z"
T_ACTIVATION = "2026-07-12T00:00:04.000Z"
T_MOUNT = "2026-07-12T00:00:05.000Z"


def _pointer(role, prefix, kind, suffix, occurred_at, byte):
    return BusinessCutoverHeadPointer(
        role=role,
        transaction_id=prefix + suffix,
        receipt_kind=kind,
        occurred_at=occurred_at,
        manifest_sha256=bytes([byte]) * 32,
    )


def _input(**changes):
    cutover_id = changes.get("cutover_id", CUTOVER_ID)
    values = {
        "cutover_id": cutover_id,
        "repository_epoch": EPOCH,
        "runtime_generation": "runtime-cutover-head-test",
        "code_digest": CODE,
        "install": _pointer(
            BusinessCutoverHeadPointerRole.INSTALL,
            BUSINESS_CUTOVER_INSTALL_TRANSACTION_PREFIX,
            BUSINESS_CUTOVER_INSTALL_TRANSACTION_KIND,
            cutover_id,
            T_INSTALL,
            1,
        ),
        "coverage": _pointer(
            BusinessCutoverHeadPointerRole.COVERAGE,
            BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_PREFIX,
            BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_KIND,
            cutover_id,
            T_COVERAGE,
            2,
        ),
        "audit": _pointer(
            BusinessCutoverHeadPointerRole.AUDIT,
            REPOSITORY_AUDIT_TRANSACTION_PREFIX,
            REPOSITORY_AUDIT_TRANSACTION_KIND,
            "a" * 32,
            T_AUDIT,
            3,
        ),
        "activation": _pointer(
            BusinessCutoverHeadPointerRole.ACTIVATION,
            BUSINESS_CUTOVER_ACTIVATION_TRANSACTION_PREFIX,
            BUSINESS_CUTOVER_ACTIVATION_TRANSACTION_KIND,
            cutover_id,
            T_ACTIVATION,
            4,
        ),
        "mount": _pointer(
            BusinessCutoverHeadPointerRole.MOUNT,
            BUSINESS_CUTOVER_MOUNT_TRANSACTION_PREFIX,
            BUSINESS_CUTOVER_MOUNT_TRANSACTION_KIND,
            cutover_id,
            T_MOUNT,
            5,
        ),
    }
    values.update(changes)
    return BusinessCutoverHeadInput(**values)


def _identity(value):
    return {
        "cutover_id": value.cutover_id,
        "repository_epoch": value.repository_epoch,
        "runtime_generation": value.runtime_generation,
        "code_digest": value.code_digest,
    }


def _incremental_chain(value):
    identity = _identity(value)
    states = [_issue_initial_business_cutover_head(
        **identity,
        install=value.install,
    )]
    for pointer in value.pointers[1:]:
        states.append(_advance_business_cutover_head(
            states[-1],
            **identity,
            pointer=pointer,
        ))
    return tuple(states)


def test_five_phase_chain_is_canonical_and_row_complete():
    chain = issue_business_cutover_head_chain(_input())

    assert len(chain) == 5
    assert tuple(value.state_revision for value in chain) == (1, 2, 3, 4, 5)
    assert tuple(value.phase for value in chain) == (
        "installed",
        "assets_covered",
        "audited",
        "activated",
        "mounted",
    )
    assert tuple(value.authority_state for value in chain) == (
        "read_only",
        "read_only",
        "read_only",
        "active",
        "active",
    )
    assert tuple(value.updated_at for value in chain) == (
        T_INSTALL,
        T_INSTALL,
        T_AUDIT,
        T_ACTIVATION,
        T_MOUNT,
    )
    assert chain[0].previous_head_sha256 == EMPTY_BUSINESS_CUTOVER_HEAD_SHA256
    assert all(
        right.previous_head_sha256 == left.head_sha256
        for left, right in zip(chain, chain[1:])
    )
    assert _require_business_cutover_head_chain(chain) is chain

    for index, state in enumerate(chain, start=1):
        row = state.row_values()
        assert row["singleton"] == 1
        assert row["state_revision"] == row["phase_revision"] == index
        assert len(state.manifest_bytes) <= 1024 * 1024
        for offset, role in enumerate(BusinessCutoverHeadPointerRole, start=1):
            present = offset <= index
            assert (row[role.value + "_transaction_id"] is not None) is present
            assert (row[role.value + "_manifest_sha256"] is not None) is present


def test_incremental_issuance_is_value_identical_to_frozen_whole_chain():
    value = _input()
    incremental = _incremental_chain(value)
    whole = issue_business_cutover_head_chain(value)

    assert incremental == whole
    assert tuple(state.manifest_bytes for state in incremental) == tuple(
        state.manifest_bytes for state in whole
    )
    assert tuple(state.row_values() for state in incremental) == tuple(
        state.row_values() for state in whole
    )
    assert all(
        _require_exact_business_cutover_head_predecessor(state) is state
        for state in incremental
    )


def test_initial_issuance_accepts_only_the_install_pointer():
    value = _input()
    with pytest.raises(BusinessCutoverHeadIntegrityError) as wrong_role:
        _issue_initial_business_cutover_head(
            **_identity(value),
            install=value.coverage,
        )
    assert wrong_role.value.code == "business_cutover_head_transition_invalid"


@pytest.mark.parametrize(
    "predecessor_index,pointer_name",
    (
        (0, "audit"),
        (1, "install"),
        (2, "coverage"),
        (3, "audit"),
    ),
)
def test_incremental_transition_rejects_skip_and_rollback(
    predecessor_index,
    pointer_name,
):
    value = _input()
    chain = issue_business_cutover_head_chain(value)
    with pytest.raises(BusinessCutoverHeadIntegrityError) as transition:
        _advance_business_cutover_head(
            chain[predecessor_index],
            **_identity(value),
            pointer=getattr(value, pointer_name),
        )
    assert transition.value.code == "business_cutover_head_transition_invalid"


@pytest.mark.parametrize(
    "field,replacement",
    (
        ("cutover_id", "d" * 64),
        ("repository_epoch", "f" * 64),
        ("runtime_generation", "runtime-cutover-head-other"),
        ("code_digest", b"z" * 32),
    ),
)
def test_incremental_transition_rejects_identity_drift(field, replacement):
    value = _input()
    identity = _identity(value)
    identity[field] = replacement
    with pytest.raises(BusinessCutoverHeadIntegrityError) as drift:
        _advance_business_cutover_head(
            issue_business_cutover_head_chain(value)[0],
            **identity,
            pointer=value.coverage,
        )
    assert drift.value.code == "business_cutover_head_identity_transition_invalid"


def test_incremental_transition_rejects_pointer_and_time_tamper():
    value = _input()
    initial = issue_business_cutover_head_chain(value)[0]
    tampered = dataclasses.replace(value.coverage)
    object.__setattr__(tampered, "occurred_at", "not-a-time")

    with pytest.raises(BusinessCutoverHeadIntegrityError) as pointer:
        _advance_business_cutover_head(
            initial,
            **_identity(value),
            pointer=tampered,
        )
    assert pointer.value.code == "business_cutover_head_pointer_invalid"

    late_coverage = dataclasses.replace(value.coverage, occurred_at=T_MOUNT)
    with pytest.raises(BusinessCutoverHeadIntegrityError) as chronology:
        _advance_business_cutover_head(
            initial,
            **_identity(value),
            pointer=late_coverage,
        )
    assert chronology.value.code == "business_cutover_head_chronology_invalid"


def test_incremental_transition_rejects_digest_consistent_wrong_predecessor():
    value = _input()
    legitimate = issue_business_cutover_head_chain(value)[1]
    wrong_previous = bytes.fromhex("99" * 32)
    wrong_head = _state_digest(
        cutover_id=legitimate.cutover_id,
        repository_epoch=legitimate.repository_epoch,
        runtime_generation=legitimate.runtime_generation,
        code_digest=legitimate.code_digest,
        state_revision=legitimate.state_revision,
        phase_revision=legitimate.phase_revision,
        phase=legitimate.phase,
        authority_state=legitimate.authority_state,
        pointers=legitimate.pointers,
        previous_head_sha256=wrong_previous,
        updated_at=legitimate.updated_at,
    )
    forged = dataclasses.replace(
        legitimate,
        previous_head_sha256=wrong_previous,
        head_sha256=wrong_head,
    )

    with pytest.raises(BusinessCutoverHeadIntegrityError) as predecessor:
        _advance_business_cutover_head(
            forged,
            **_identity(value),
            pointer=value.audit,
        )
    assert predecessor.value.code == "business_cutover_head_predecessor_invalid"


def test_incremental_retry_is_idempotent_but_applied_pointer_cannot_replay():
    value = _input()
    initial = issue_business_cutover_head_chain(value)[0]
    first = _advance_business_cutover_head(
        initial,
        **_identity(value),
        pointer=value.coverage,
    )
    retry = _advance_business_cutover_head(
        initial,
        **_identity(value),
        pointer=value.coverage,
    )
    assert retry == first

    with pytest.raises(BusinessCutoverHeadIntegrityError) as replay:
        _advance_business_cutover_head(
            first,
            **_identity(value),
            pointer=value.coverage,
        )
    assert replay.value.code == "business_cutover_head_transition_invalid"


def test_final_head_cannot_advance_or_replay():
    value = _input()
    final = issue_business_cutover_head_chain(value)[-1]
    with pytest.raises(BusinessCutoverHeadIntegrityError) as final_transition:
        _advance_business_cutover_head(
            final,
            **_identity(value),
            pointer=value.mount,
        )
    assert final_transition.value.code == "business_cutover_head_transition_invalid"


def test_chain_is_deterministic_and_has_frozen_vectors():
    first = issue_business_cutover_head_chain(_input())
    second = issue_business_cutover_head_chain(_input())
    assert first == second
    assert BUSINESS_CUTOVER_HEAD_HASH_DOMAIN == b"ze.business-cutover.phase-head.v1"
    assert tuple(value.head_sha256.hex() for value in first) == (
        "eb6fd5e8b8cba4432fc27bcc2c6bec32f512ea48e0b480f2ec744c8d1e566ae3",
        "07ae72b411e29f95c97c4919f4de9f9686db02858d06e3d887d44d57bf608896",
        "bf83741b76b276732e488961ddc0b705ec9d6c66ef03e2d1ea0b316aa93bd3db",
        "e108ef005ef9dd7065f2240e5aae431b2283c8fa251a005702cf143343ddee43",
        "2c96b65bfd640466ed21ffa0216bf12f6f52b6292b51fc5d4c4734bc186559a1",
    )


@pytest.mark.parametrize(
    "field,replacement",
    (
        ("cutover_id", "d" * 64),
        ("repository_epoch", "f" * 64),
        ("runtime_generation", "runtime-cutover-head-other"),
        ("code_digest", b"z" * 32),
    ),
)
def test_identity_changes_every_head(field, replacement):
    original = issue_business_cutover_head_chain(_input())
    changed = issue_business_cutover_head_chain(_input(**{field: replacement}))
    assert tuple(value.head_sha256 for value in changed) != tuple(
        value.head_sha256 for value in original
    )


@pytest.mark.parametrize("role", tuple(BusinessCutoverHeadPointerRole))
def test_each_pointer_changes_its_phase_and_every_descendant(role):
    original_input = _input()
    original = issue_business_cutover_head_chain(original_input)
    pointer = getattr(original_input, role.value)
    changed_pointer = dataclasses.replace(
        pointer,
        manifest_sha256=bytes([pointer.manifest_sha256[0] + 16]) * 32,
    )
    changed = issue_business_cutover_head_chain(
        dataclasses.replace(original_input, **{role.value: changed_pointer})
    )
    first_phase = tuple(BusinessCutoverHeadPointerRole).index(role)
    assert tuple(value.head_sha256 for value in changed[:first_phase]) == tuple(
        value.head_sha256 for value in original[:first_phase]
    )
    assert all(
        left.head_sha256 != right.head_sha256
        for left, right in zip(original[first_phase:], changed[first_phase:])
    )


def test_digest_and_predecessor_tamper_cannot_construct_a_state():
    chain = issue_business_cutover_head_chain(_input())
    with pytest.raises(BusinessCutoverHeadIntegrityError) as digest:
        dataclasses.replace(chain[-1], head_sha256=b"x" * 32)
    assert digest.value.code == "business_cutover_head_digest_mismatch"

    with pytest.raises(BusinessCutoverHeadIntegrityError) as predecessor:
        dataclasses.replace(chain[-1], previous_head_sha256=b"y" * 32)
    assert predecessor.value.code == "business_cutover_head_digest_mismatch"


def test_wrong_order_wrong_identity_and_reverse_chronology_are_rejected():
    base = _input()
    with pytest.raises(BusinessCutoverHeadIntegrityError) as order:
        BusinessCutoverHeadInput(
            cutover_id=base.cutover_id,
            repository_epoch=base.repository_epoch,
            runtime_generation=base.runtime_generation,
            code_digest=base.code_digest,
            install=base.coverage,
            coverage=base.install,
            audit=base.audit,
            activation=base.activation,
            mount=base.mount,
        )
    assert order.value.code == "business_cutover_head_pointer_order_invalid"

    with pytest.raises(BusinessCutoverHeadIntegrityError) as identity:
        _input(install=dataclasses.replace(
            base.install,
            transaction_id=BUSINESS_CUTOVER_INSTALL_TRANSACTION_PREFIX + "d" * 64,
        ))
    assert identity.value.code == "business_cutover_head_cutover_binding_invalid"

    with pytest.raises(BusinessCutoverHeadIntegrityError) as chronology:
        _input(coverage=dataclasses.replace(base.coverage, occurred_at=T_MOUNT))
    assert chronology.value.code == "business_cutover_head_chronology_invalid"


def test_chain_require_rejects_reordering_and_partial_input():
    chain = issue_business_cutover_head_chain(_input())
    with pytest.raises(BusinessCutoverHeadIntegrityError) as reordered:
        _require_business_cutover_head_chain((chain[1], chain[0], *chain[2:]))
    assert reordered.value.code == "business_cutover_head_chain_invalid"

    with pytest.raises(BusinessCutoverHeadIntegrityError) as partial:
        _require_business_cutover_head_chain(chain[:-1])
    assert partial.value.code == "business_cutover_head_chain_invalid"


@pytest.mark.parametrize(
    "value",
    (
        None,
        [],
        "not-a-chain",
    ),
)
def test_chain_input_is_exact_tuple(value):
    with pytest.raises(BusinessCutoverHeadIntegrityError):
        _require_business_cutover_head_chain(value)
