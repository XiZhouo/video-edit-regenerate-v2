"""Pure adversarial tests for the private V5c cutover contract and issuer."""

from __future__ import annotations

import hashlib
import inspect
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional, Tuple

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

from asset_reference_projection import project_asset_references
from business_baseline_contract import digest_business_baseline_manifest
from business_cutover_contract import (
    REPOSITORY_ADMISSION_HASH_DOMAIN,
    BusinessCutoverInstallReconcileStatus,
    BusinessCutoverJoinReconcileStatus,
    BusinessCutoverRetryAction,
    digest_business_cutover_admission,
)
from business_cutover_integrity import (
    BusinessCutoverIntegrityError,
    BusinessCutoverPlan,
    _classify_business_cutover_install,
    _classify_business_cutover_join,
    _issue_business_cutover_plan,
    _require_business_cutover_plan,
)
from business_snapshot import (
    BusinessSnapshotSet,
    capture_business_snapshot,
)
from document import Document
from entity import Entity
from legacy_repository_contract import (
    LEGACY_REPOSITORY_LEGACY_ADOPT_OPERATION_ID,
    LEGACY_REPOSITORY_MEMBER_DELETED,
    LEGACY_REPOSITORY_MEMBER_LIVE,
)
from legacy_repository_integrity import (
    LegacyRepositoryIntegrityMember,
    build_legacy_repository_integrity,
)


CATALOG_SEAL = b"s" * 32
LEGACY_SCHEMA_FINGERPRINT = "f" * 64
TARGET_SCHEMA_FINGERPRINT = "8" * 64
RUNTIME_GENERATION = "core-3.15.0+cutover-a"
CODE_DIGEST = hashlib.sha256(b"cutover-code-a").digest()
EMPTY_DIGEST = hashlib.sha256(b"").digest()


def _snapshot(
    *,
    project_id: str = "project-a",
    generation: int = 1,
    document_id: str = "document-a",
    document_version: int = 7,
) -> BusinessSnapshotSet:
    document = Document(
        id=document_id,
        name="cutover",
        schema_version="1",
        collaboration_mode="live_collab",
        save_policy="auto_commit",
        schema={
            "media": {
                "type": "object",
                "properties": {"asset": {"type": "asset_ref"}},
            },
            "item": {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
            },
        },
    )
    media = Entity(
        type="media",
        id="media-1",
        data={"asset": {"sha256": "a" * 64}},
    )
    item = Entity(type="item", id="item-1", data={"value": 9})
    document.add(media)
    document.add(item)
    for ordinal, entity in enumerate((media, item), start=1):
        entity._version = ordinal
        entity._created_at = ordinal
        entity._updated_at = ordinal
    document._v = document_version
    document._created_at = 1
    document._updated_at = 3
    return capture_business_snapshot(project_id, generation, document).snapshot


def _live(
    snapshot: Optional[BusinessSnapshotSet] = None,
    **overrides: Any,
) -> LegacyRepositoryIntegrityMember:
    selected = _snapshot() if snapshot is None else snapshot
    values = {
        "project_id": selected.project.project_id,
        "generation": selected.project.generation,
        "status": LEGACY_REPOSITORY_MEMBER_LIVE,
        "operation_id": LEGACY_REPOSITORY_LEGACY_ADOPT_OPERATION_ID,
        "source_state_sha256": b"h" * 32,
        "source_state_bytes": 17,
        "catalog_ordinal": 0,
        "import_id": "a" * 64,
        "document_id": selected.document.document_id,
        "document_version": selected.document.document_version,
        "storage_version": "v2",
        "business_root_sha256": selected.project.snapshot_sha256,
        "entity_count": len(selected.entities),
        "row_snapshot_bytes": 19,
    }
    values.update(overrides)
    return LegacyRepositoryIntegrityMember(**values)


def _deleted(**overrides: Any) -> LegacyRepositoryIntegrityMember:
    values = {
        "project_id": "project-z",
        "generation": 9,
        "status": LEGACY_REPOSITORY_MEMBER_DELETED,
        "operation_id": "1" * 32,
        "source_state_sha256": EMPTY_DIGEST,
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


def _proof(
    members: tuple[LegacyRepositoryIntegrityMember, ...],
    **overrides: Any,
):
    values = {
        "catalog_id": "c" * 64,
        "catalog_seal_sha256": CATALOG_SEAL,
        "repository_epoch": "e" * 64,
        "repository_revision": 11,
        "catalog_revision": 11,
        "runtime_generation": RUNTIME_GENERATION,
        "code_digest": CODE_DIGEST,
        "members": members,
        "schema_fingerprint": LEGACY_SCHEMA_FINGERPRINT,
    }
    values.update(overrides)
    return build_legacy_repository_integrity(**values)


def _plan(
    *,
    members: Optional[Tuple[LegacyRepositoryIntegrityMember, ...]] = None,
    snapshots: Optional[Tuple[BusinessSnapshotSet, ...]] = None,
    proof: Any = None,
    runtime_generation: str = RUNTIME_GENERATION,
    code_digest: bytes = CODE_DIGEST,
    expected_target_schema_version: int = 8,
):
    source_snapshot = _snapshot()
    selected_snapshots = (source_snapshot,) if snapshots is None else snapshots
    selected = (
        (_live(source_snapshot), _deleted())
        if members is None
        else members
    )
    exact_proof = _proof(selected) if proof is None else proof
    return _issue_business_cutover_plan(
        legacy_integrity=exact_proof,
        legacy_members=selected,
        catalog_seal_sha256=CATALOG_SEAL,
        legacy_schema_fingerprint=LEGACY_SCHEMA_FINGERPRINT,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
        expected_target_schema_version=expected_target_schema_version,
        expected_target_schema_fingerprint=TARGET_SCHEMA_FINGERPRINT,
        business_snapshots=selected_snapshots,
    )


def test_mixed_plan_derives_only_contiguous_admissions_and_five_state_facts() -> None:
    plan = _plan()
    assert _require_business_cutover_plan(plan) is plan
    assert (
        plan.source_admission.authority_state,
        plan.source_admission.revision,
        plan.source_admission.cutover_id,
    ) == ("legacy", 11, "")
    assert (
        plan.pending_admission.authority_state,
        plan.pending_admission.revision,
        plan.pending_admission.cutover_id,
    ) == ("cutover_pending", 12, plan.cutover_id)
    assert (
        plan.final_admission.authority_state,
        plan.final_admission.revision,
        plan.final_admission.cutover_id,
    ) == ("sqlite", 13, plan.cutover_id)

    assert plan.install.authority_state == "read_only"
    assert plan.install.runtime.state_revision == 1
    assert plan.join.authority_state == "active"
    assert plan.join.runtime.state_revision == 2
    assert plan.install.runtime.source_admission_revision == 11
    assert plan.join.runtime.repository_revision == 1
    assert plan.join.install_manifest_sha256 == plan.install.manifest_sha256
    assert plan.join.final_admission_sha256 == (
        plan.closure.final_admission_sha256
    )
    assert b'"status":"installed_read_only"' in plan.install.manifest_bytes
    assert b'"status":"joined_active"' in plan.join.manifest_bytes


def test_admission_digest_is_byte_exact_with_topology_v1_algorithm() -> None:
    plan = _plan()
    for admission, observed in (
        (plan.source_admission, plan.closure.source_admission_sha256),
        (plan.pending_admission, plan.closure.pending_admission_sha256),
        (plan.final_admission, plan.closure.final_admission_sha256),
    ):
        payload = json.dumps(
            admission.canonical_value(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        expected = hashlib.sha256(REPOSITORY_ADMISSION_HASH_DOMAIN + payload).digest()
        assert observed == expected == digest_business_cutover_admission(admission)
    assert len({
        plan.closure.source_admission_sha256,
        plan.closure.pending_admission_sha256,
        plan.closure.final_admission_sha256,
    }) == 3


def test_live_and_deleted_mapping_is_complete_and_baseline_is_rebuildable() -> None:
    plan = _plan()
    snapshot = plan._business_snapshots[0]
    projection = project_asset_references(snapshot)
    live, deleted = plan.members
    assert (live.ordinal, live.status, live.baseline_repository_revision) == (
        0,
        "live",
        1,
    )
    assert live.baseline_commit_id is not None and len(live.baseline_commit_id) == 32
    assert live.baseline_manifest_bytes is not None
    assert live.baseline_manifest_sha256 == digest_business_baseline_manifest(
        live.baseline_manifest_bytes
    )
    assert b'"authority_state":"read_only"' in live.baseline_manifest_bytes
    assert b'"repository_revision":1' in live.baseline_manifest_bytes
    assert live.document_snapshot_sha256 == snapshot.document.snapshot_sha256
    assert live.save_policy == snapshot.document.save_policy
    assert live.asset_projection_sha256 == projection.projection_sha256
    assert live.asset_reference_count == projection.reference_count == 1
    assert live.unique_asset_reference_count == projection.unique_reference_count == 1
    assert deleted.ordinal == 1 and deleted.status == "deleted"
    assert deleted.baseline_commit_id is None
    assert deleted.document_id is None
    assert deleted.business_root_sha256 is None
    assert plan.legacy_proof.project_count == 2
    assert plan.legacy_proof.live_project_count == 1
    assert plan.legacy_proof.deleted_project_count == 1


def test_empty_and_deleted_only_repositories_have_closed_runtime_floor() -> None:
    empty = _plan(members=(), snapshots=(), proof=_proof(()))
    assert empty.members == ()
    assert empty.install.runtime.repository_revision == 1
    assert empty.install.runtime.state_revision == 1

    deleted_member = _deleted(project_id="project-only")
    deleted = _plan(
        members=(deleted_member,),
        snapshots=(),
        proof=_proof((deleted_member,)),
    )
    assert len(deleted.members) == 1
    assert deleted.members[0].status == "deleted"
    assert deleted.install.runtime.repository_revision == 1


@pytest.mark.parametrize("snapshots", ((), (_snapshot(), _snapshot())))
def test_snapshot_mapping_rejects_missing_or_extra_heads(
    snapshots: Tuple[BusinessSnapshotSet, ...],
) -> None:
    with pytest.raises(BusinessCutoverIntegrityError) as caught:
        _plan(snapshots=snapshots)
    assert caught.value.code == "business_cutover_snapshot_count_mismatch"


def test_snapshot_is_the_only_baseline_authority_not_caller_filled_x_y_or_999() -> None:
    source_snapshot = _snapshot()
    source_members = (_live(source_snapshot), _deleted())
    source_proof = _proof(source_members)

    wrong_identity = _snapshot(document_id="other-document")
    with pytest.raises(BusinessCutoverIntegrityError) as identity:
        _plan(
            members=source_members,
            proof=source_proof,
            snapshots=(wrong_identity,),
        )
    assert identity.value.code == "business_cutover_snapshot_source_mismatch"

    tampered_root = replace(
        source_snapshot,
        project=replace(source_snapshot.project, snapshot_sha256=b"x" * 32),
    )
    with pytest.raises(BusinessCutoverIntegrityError) as root:
        _plan(
            members=source_members,
            proof=source_proof,
            snapshots=(tampered_root,),
        )
    assert root.value.code == "business_cutover_snapshot_invalid"

    tampered_document = replace(
        source_snapshot,
        document=replace(source_snapshot.document, snapshot_sha256=b"y" * 32),
    )
    with pytest.raises(BusinessCutoverIntegrityError) as document:
        _plan(
            members=source_members,
            proof=source_proof,
            snapshots=(tampered_document,),
        )
    assert document.value.code == "business_cutover_snapshot_invalid"

    false_count_members = (
        _live(source_snapshot, entity_count=999),
        _deleted(),
    )
    with pytest.raises(BusinessCutoverIntegrityError) as count:
        _plan(
            members=false_count_members,
            proof=_proof(false_count_members),
            snapshots=(source_snapshot,),
        )
    assert count.value.code == "business_cutover_snapshot_source_mismatch"

    signature = inspect.signature(_issue_business_cutover_plan)
    assert "live_baselines" not in signature.parameters
    assert "business_snapshots" in signature.parameters


def test_legacy_proof_runtime_and_schema_inputs_are_rebuilt_not_trusted() -> None:
    members = (_live(), _deleted())
    proof = _proof(members)
    changed = proof._replace(proof_sha256=b"x" * 32)
    with pytest.raises(BusinessCutoverIntegrityError) as tampered:
        _plan(members=members, proof=changed)
    assert tampered.value.code == "business_cutover_legacy_proof_changed"

    with pytest.raises(BusinessCutoverIntegrityError) as runtime:
        _plan(runtime_generation="other-runtime")
    assert runtime.value.code == "business_cutover_runtime_identity_mismatch"

    with pytest.raises(BusinessCutoverIntegrityError) as target:
        _plan(expected_target_schema_version=True)
    assert target.value.code == (
        "business_cutover_expected_target_schema_version_invalid"
    )


def test_revision_exhaustion_cannot_fabricate_noncontiguous_successors() -> None:
    proof = _proof(
        (_live(), _deleted()),
        repository_revision=2**63 - 1,
        catalog_revision=2**63 - 1,
    )
    with pytest.raises(BusinessCutoverIntegrityError) as caught:
        _plan(proof=proof)
    assert caught.value.code == "business_cutover_admission_revision_not_contiguous"


def test_plan_is_issuer_stamped_immutable_and_fully_reverified() -> None:
    plan = _plan()
    with pytest.raises(AttributeError):
        plan.cutover_id = "0" * 64

    forged = object.__new__(BusinessCutoverPlan)
    with pytest.raises(BusinessCutoverIntegrityError) as invalid:
        _require_business_cutover_plan(forged)
    assert invalid.value.code == "business_cutover_plan_invalid"

    changed = plan.closure._replace(install_manifest_sha256=b"x" * 32)
    object.__setattr__(plan, "closure", changed)
    with pytest.raises(BusinessCutoverIntegrityError) as tampered:
        _require_business_cutover_plan(plan)
    assert tampered.value.code == "business_cutover_plan_changed"


def test_expected_target_is_rebuilt_but_never_claimed_as_current_registry() -> None:
    plan = _plan()
    assert plan.expected_target_schema_version == 8
    assert plan.expected_target_schema_fingerprint == TARGET_SCHEMA_FINGERPRINT
    assert b'"expected_target"' in plan.install.manifest_bytes
    assert b'"target"' not in plan.install.manifest_bytes
    object.__setattr__(plan, "expected_target_schema_fingerprint", "9" * 64)
    with pytest.raises(BusinessCutoverIntegrityError) as changed:
        _require_business_cutover_plan(plan)
    assert changed.value.code == "business_cutover_plan_changed"


def test_install_and_join_fresh_classification_has_exact_retry_semantics() -> None:
    plan = _plan()
    absent = _classify_business_cutover_install(
        plan, plan.pending_admission, None
    )
    assert absent.status is BusinessCutoverInstallReconcileStatus.NOT_INSTALLED
    assert absent.retry_action is BusinessCutoverRetryAction.APPLY_EXACT

    installed = _classify_business_cutover_install(
        plan, plan.pending_admission, plan.install
    )
    assert installed.status is (
        BusinessCutoverInstallReconcileStatus.INSTALLED_READ_ONLY
    )
    assert installed.retry_action is BusinessCutoverRetryAction.RETURN_EXACT
    assert installed.evidence == plan.install

    installed_after_file_flip = _classify_business_cutover_install(
        plan, plan.final_admission, plan.install
    )
    assert installed_after_file_flip.status is (
        BusinessCutoverInstallReconcileStatus.INSTALLED_READ_ONLY
    )

    not_joined = _classify_business_cutover_join(
        plan, plan.final_admission, plan.install, None
    )
    assert not_joined.status is BusinessCutoverJoinReconcileStatus.NOT_JOINED
    assert not_joined.retry_action is BusinessCutoverRetryAction.APPLY_EXACT
    joined = _classify_business_cutover_join(
        plan, plan.final_admission, plan.install, plan.join
    )
    assert joined.status is BusinessCutoverJoinReconcileStatus.JOINED_ACTIVE
    assert joined.retry_action is BusinessCutoverRetryAction.RETURN_EXACT
    assert joined.evidence == plan.join

    install_unknown = _classify_business_cutover_install(
        plan, plan.pending_admission, None, indeterminate=True
    )
    join_unknown = _classify_business_cutover_join(
        plan,
        plan.final_admission,
        plan.install,
        None,
        indeterminate=True,
    )
    assert install_unknown.retry_action is (
        BusinessCutoverRetryAction.RECONCILE_REQUIRED
    )
    assert join_unknown.retry_action is BusinessCutoverRetryAction.RECONCILE_REQUIRED


def test_classification_never_applies_before_pending_or_joins_before_final() -> None:
    plan = _plan()
    source_absent = _classify_business_cutover_install(
        plan, plan.source_admission, None
    )
    assert source_absent.status is BusinessCutoverInstallReconcileStatus.CONFLICT
    assert source_absent.retry_action is BusinessCutoverRetryAction.STOP
    source_with_install = _classify_business_cutover_install(
        plan, plan.source_admission, plan.install
    )
    assert source_with_install.status is BusinessCutoverInstallReconcileStatus.CORRUPT

    final_absent = _classify_business_cutover_install(
        plan, plan.final_admission, None
    )
    assert final_absent.status is BusinessCutoverInstallReconcileStatus.CORRUPT
    assert final_absent.retry_action is BusinessCutoverRetryAction.STOP

    for current, observed_join in (
        (plan.source_admission, None),
        (plan.pending_admission, plan.join),
    ):
        blocked = _classify_business_cutover_join(
            plan, current, plan.install, observed_join
        )
        assert blocked.status is BusinessCutoverJoinReconcileStatus.CONFLICT
        assert blocked.retry_action is BusinessCutoverRetryAction.STOP

    missing_install = _classify_business_cutover_join(
        plan, plan.final_admission, None, None
    )
    assert missing_install.status is BusinessCutoverJoinReconcileStatus.CORRUPT
    assert missing_install.retry_action is BusinessCutoverRetryAction.STOP


def test_classification_separates_other_identity_from_same_identity_tamper() -> None:
    plan = _plan()
    other_members = (_live(source_state_sha256=b"x" * 32), _deleted())
    other = _plan(
        members=other_members,
        proof=_proof(other_members),
    )
    conflict = _classify_business_cutover_install(
        plan, plan.pending_admission, other.install
    )
    assert conflict.status is BusinessCutoverInstallReconcileStatus.CONFLICT
    assert conflict.retry_action is BusinessCutoverRetryAction.STOP
    join_install_conflict = _classify_business_cutover_join(
        plan, plan.final_admission, other.install, None
    )
    assert join_install_conflict.status is BusinessCutoverJoinReconcileStatus.CONFLICT

    corrupted_install = replace(
        plan.install,
        manifest_bytes=plan.install.manifest_bytes + b" ",
    )
    corrupt = _classify_business_cutover_install(
        plan, plan.pending_admission, corrupted_install
    )
    assert corrupt.status is BusinessCutoverInstallReconcileStatus.CORRUPT
    assert corrupt.retry_action is BusinessCutoverRetryAction.STOP
    join_install_corrupt = _classify_business_cutover_join(
        plan, plan.final_admission, corrupted_install, None
    )
    assert join_install_corrupt.status is BusinessCutoverJoinReconcileStatus.CORRUPT

    corrupted_join = replace(
        plan.join,
        manifest_bytes=plan.join.manifest_bytes + b" ",
    )
    join_corrupt = _classify_business_cutover_join(
        plan, plan.final_admission, plan.install, corrupted_join
    )
    assert join_corrupt.status is BusinessCutoverJoinReconcileStatus.CORRUPT

    with pytest.raises(BusinessCutoverIntegrityError) as wrong_type:
        _classify_business_cutover_install(
            plan, plan.pending_admission, {}  # type: ignore[arg-type]
        )
    assert wrong_type.value.code == "business_cutover_install_evidence_type_invalid"


def test_exact_tuple_and_exact_member_types_are_required() -> None:
    snapshot = _snapshot()
    members = (_live(snapshot), _deleted())
    proof = _proof(members)
    with pytest.raises(BusinessCutoverIntegrityError) as member_list:
        _issue_business_cutover_plan(
            legacy_integrity=proof,
            legacy_members=list(members),  # type: ignore[arg-type]
            catalog_seal_sha256=CATALOG_SEAL,
            legacy_schema_fingerprint=LEGACY_SCHEMA_FINGERPRINT,
            runtime_generation=RUNTIME_GENERATION,
            code_digest=CODE_DIGEST,
            expected_target_schema_version=8,
            expected_target_schema_fingerprint=TARGET_SCHEMA_FINGERPRINT,
            business_snapshots=(snapshot,),
        )
    assert member_list.value.code == "business_cutover_legacy_members_invalid"

    with pytest.raises(BusinessCutoverIntegrityError) as snapshot_list:
        _issue_business_cutover_plan(
            legacy_integrity=proof,
            legacy_members=members,
            catalog_seal_sha256=CATALOG_SEAL,
            legacy_schema_fingerprint=LEGACY_SCHEMA_FINGERPRINT,
            runtime_generation=RUNTIME_GENERATION,
            code_digest=CODE_DIGEST,
            expected_target_schema_version=8,
            expected_target_schema_fingerprint=TARGET_SCHEMA_FINGERPRINT,
            business_snapshots=[snapshot],  # type: ignore[arg-type]
        )
    assert snapshot_list.value.code == "business_cutover_snapshots_invalid"


def test_private_modules_have_no_storage_path_or_callback_capability() -> None:
    contract_source = (_HERE / "business_cutover_contract.py").read_text(
        encoding="utf-8"
    )
    integrity_source = (_HERE / "business_cutover_integrity.py").read_text(
        encoding="utf-8"
    )
    combined = contract_source + integrity_source
    for forbidden in (
        "import sqlite3",
        "from pathlib",
        "import pathlib",
        "Callable",
        "Connection",
        "cursor",
        "open(",
    ):
        assert forbidden not in combined
    assert "__all__ = ()" in contract_source
    assert "__all__ = ()" in integrity_source
