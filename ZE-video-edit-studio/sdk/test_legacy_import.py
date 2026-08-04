"""Adversarial tests for the strict legacy scan and frozen payload seam."""

from __future__ import annotations

import copy
import gc
import hashlib
import inspect
import json
import linecache
import os
import pickle
import select
import sys
import threading
import time
import warnings
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

import sdk
import sdk._legacy_import_state as state_module
import sdk.legacy_import as legacy_module
from business_codec import decode_payload_bounded, encode_payload_bounded
from business_snapshot import capture_business_snapshot, verify_business_snapshot
from hash_framing import framed_size, update_hash_frame
from sdk import Document, DocumentRegistry, Entity
from sdk.legacy_import import (
    FrozenLegacyImport,
    LegacyImportError,
    LegacyScanHandle,
    PreparedLegacyPayloadReceipt,
    detach_legacy_runtime_head,
    prepare_legacy_import,
    scan_legacy_project,
)
from sdk.project_topology import ProjectTopologyCoordinator
from sdk.runtime_rehydration import (
    DetachedBusinessHead,
    RuntimeAdmission,
    RuntimeAuthorityFenceReceipt,
    rehydrate_runtime,
    snapshot_registry_declarations,
)


CARD_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string", "required": True}},
}
IMPORT_ID = "0123456789abcdef" * 4


def _fork_json(probe) -> dict[str, Any]:
    """Run one in-memory carrier probe across a real fork without pickling."""
    read_fd, write_fd = os.pipe()
    with warnings.catch_warnings():
        # One test intentionally forks while this ledger's RLock is held by a
        # sibling thread to prove the child callback never touches that lock.
        warnings.simplefilter("ignore", DeprecationWarning)
        child_pid = os.fork()
    if child_pid == 0:  # pragma: no branch - the parent asserts child output
        os.close(read_fd)
        try:
            result = probe()
        except BaseException as exc:  # keep child failures inspectable by parent
            result = {
                "fatal": type(exc).__name__,
                "code": getattr(exc, "code", ""),
                "reason": getattr(exc, "reason", ""),
            }
        payload = json.dumps(result, sort_keys=True).encode("utf-8")
        try:
            os.write(write_fd, payload)
        finally:
            os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    chunks: list[bytes] = []
    deadline = time.monotonic() + 10.0
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select(
                (read_fd,), (), (), max(0.0, remaining),
            )[0]:
                os.kill(child_pid, 9)
                os.waitpid(child_pid, 0)
                pytest.fail("fork probe timed out")
            chunk = os.read(read_fd, 65_536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(read_fd)
    waited_pid, status = os.waitpid(child_pid, 0)
    assert waited_pid == child_pid
    assert os.waitstatus_to_exitcode(status) == 0
    assert chunks
    result = json.loads(b"".join(chunks).decode("utf-8"))
    assert type(result) is dict
    return result


def _consume_prepared_for_test(
    package: FrozenLegacyImport,
    receipt: PreparedLegacyPayloadReceipt,
    *,
    _capability: Any = None,
) -> tuple[BusinessSnapshotSet, ...]:
    """Exercise the explicit lane terminal in tests; production has no shortcut."""
    reservation, plan = legacy_module._reserve_prepared_legacy_import(
        package,
        receipt,
        _attempt_token=object(),
        _capability=_capability,
    )
    legacy_module._finish_prepared_legacy_import(
        reservation,
        outcome="consumed",
        _capability=_capability,
    )
    return plan.snapshots


def _document(project_id: str, title: str = "hello") -> Document:
    document = Document(
        id=project_id,
        schema_version="1",
        collaboration_mode="live_collab",
        save_policy="auto_commit",
        schema={"card": CARD_SCHEMA},
    )
    document.add(Entity(type="card", id="row", data={"title": title}))
    return document


def _topology(tmp_path: Path) -> ProjectTopologyCoordinator:
    topology = ProjectTopologyCoordinator(tmp_path / "projects", lock_timeout=2.0)
    topology.initialize(validate=lambda _root: None)
    return topology


def _create_live_project(
    topology: ProjectTopologyCoordinator,
    project_id: str,
    *,
    title: str = "hello",
) -> tuple[int, Document]:
    built: dict[str, Document] = {}

    def build(root: Path) -> None:
        document = _document(project_id, title)
        document.save(Path(root) / "document.json")
        built["document"] = document

    commit = topology.create(
        project_id,
        build=build,
        validate=lambda _root: None,
    )
    return commit.generation, built["document"]


def _convert_to_raw_legacy(
    topology: ProjectTopologyCoordinator,
    project_id: str,
    document: Document,
    *,
    read_only: bool = False,
) -> None:
    root = topology.path_for(project_id)
    document_path = root / "document.json"
    head = json.loads(document_path.read_text(encoding="utf-8"))
    head.pop("entity_storage")
    document_path.write_text(
        json.dumps(head, ensure_ascii=False),
        encoding="utf-8",
    )
    document_path.chmod(0o600)

    entity_payload = document.get("card", "row").to_dict()
    entity_payload["read_only"] = read_only
    raw_type = root / "entities" / "card"
    raw_type.mkdir(exist_ok=True)
    raw_entity = raw_type / "row.json"
    raw_entity.write_text(
        json.dumps(entity_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    raw_entity.chmod(0o644)


def _deterministic_handle(
    project_id: str,
    *,
    generation: int,
    title: str,
    storage_version: str = "v2",
) -> tuple[LegacyScanHandle, Any]:
    document = _document(project_id, title)
    document._created_at = 10.0
    document._updated_at = 20.0
    document._v = 7
    entity = document.get("card", "row")
    entity._created_at = 11.0
    entity._updated_at = 19.0
    entity._version = 3
    snapshot = capture_business_snapshot(
        project_id,
        generation,
        document,
    ).snapshot
    source = ("source-head:" + project_id).encode("utf-8")
    handle = state_module._issue_scan_handle(
        snapshot,
        source_head_sha256=hashlib.sha256(source).digest(),
        source_head_bytes=len(source),
        storage_version=storage_version,
    )
    return handle, snapshot


def _framed_digest(domain: bytes, *parts: bytes) -> bytes:
    hasher = hashlib.sha256()
    update_hash_frame(hasher, domain)
    for part in parts:
        update_hash_frame(hasher, part)
    return hasher.digest()


def _manual_payload_digest(snapshots: tuple[Any, ...]) -> tuple[bytes, int]:
    hasher = hashlib.sha256()
    size = 0
    update_hash_frame(hasher, state_module._PAYLOAD_DOMAIN)
    size += framed_size(state_module._PAYLOAD_DOMAIN)
    count = len(snapshots).to_bytes(8, "big")
    update_hash_frame(hasher, count)
    size += framed_size(count)
    for snapshot in snapshots:
        for payload in (
            snapshot.project.snapshot_bytes,
            snapshot.document.snapshot_bytes,
        ):
            update_hash_frame(hasher, payload)
            size += framed_size(payload)
        entity_count = len(snapshot.entities).to_bytes(8, "big")
        update_hash_frame(hasher, entity_count)
        size += framed_size(entity_count)
        for row in snapshot.entities:
            update_hash_frame(hasher, row.snapshot_bytes)
            size += framed_size(row.snapshot_bytes)
    return hasher.digest(), size


def _current_fence(request: Any) -> RuntimeAuthorityFenceReceipt:
    return RuntimeAuthorityFenceReceipt(
        request_identity=request.request_identity,
        outcome="current",
        protocol=request.protocol,
        project_id=request.project_id,
        generation=request.generation,
        business_root=request.business_root,
        document_version=request.document_version,
        declaration_digest=request.declaration_digest,
        runtime_generation=request.runtime_generation,
        authority_revision="legacy-file-revision-1",
    )


def _declarations():
    registry = DocumentRegistry()
    registry.declare_entity_type(
        name="card",
        schema=CARD_SCHEMA,
        collaboration_unit=True,
    )
    registry.set_collaboration_mode("live_collab")
    registry.set_save_policy("auto_commit")
    return snapshot_registry_declarations(
        registry,
        schema_version="1",
        runtime_generation="legacy-import-test",
    )


def test_v2_scan_binds_topology_inner_head_and_capture_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology = _topology(tmp_path)
    generation, document = _create_live_project(
        topology,
        "项目-甲",
        title="世界",
    )
    active = {"topology": False, "document": False}
    real_topology_open = ProjectTopologyCoordinator.open_live_scan_source
    real_document_open = legacy_module._open_document_for_legacy_scan
    real_capture = legacy_module.capture_business_snapshot
    real_issue = legacy_module._issue_scan_handle

    @contextmanager
    def observed_topology(self, project_id, expected_generation):
        with real_topology_open(self, project_id, expected_generation) as source:
            active["topology"] = True
            try:
                yield source
            finally:
                active["topology"] = False

    @contextmanager
    def observed_document(*args, **kwargs):
        with real_document_open(*args, **kwargs) as opened:
            assert active["topology"] is True
            active["document"] = True
            try:
                yield opened
            finally:
                active["document"] = False

    def observed_capture(*args, **kwargs):
        assert active == {"topology": True, "document": True}
        return real_capture(*args, **kwargs)

    def observed_issue(*args, **kwargs):
        # Capture and verification require both locks.  Carrier issuance waits
        # until the topology context's post-yield fences have also succeeded.
        assert active == {"topology": False, "document": False}
        return real_issue(*args, **kwargs)

    monkeypatch.setattr(
        ProjectTopologyCoordinator,
        "open_live_scan_source",
        observed_topology,
    )
    monkeypatch.setattr(
        legacy_module,
        "_open_document_for_legacy_scan",
        observed_document,
    )
    monkeypatch.setattr(
        legacy_module,
        "capture_business_snapshot",
        observed_capture,
    )
    monkeypatch.setattr(legacy_module, "_issue_scan_handle", observed_issue)

    handle = scan_legacy_project(topology, "项目-甲", generation)
    assert active == {"topology": False, "document": False}
    assert type(handle) is LegacyScanHandle
    assert handle.project_id == "项目-甲"
    assert handle.generation == generation
    assert handle.document_id == document.id
    assert handle.document_version == document.version
    assert handle.storage_version == "v2"
    assert handle.entity_count == 1
    source_head = topology.path_for("项目-甲") / "document.json"
    assert handle.source_head_sha256 == hashlib.sha256(
        source_head.read_bytes()
    ).digest()


def test_raw_legacy_scan_and_excluded_read_only_rejection(tmp_path: Path) -> None:
    topology = _topology(tmp_path)
    generation, document = _create_live_project(topology, "raw-project")
    _convert_to_raw_legacy(topology, "raw-project", document)

    handle = scan_legacy_project(topology, "raw-project", generation)
    assert handle.storage_version == "legacy"
    assert handle.entity_count == 1

    other_generation, other = _create_live_project(topology, "read-only-raw")
    _convert_to_raw_legacy(
        topology,
        "read-only-raw",
        other,
        read_only=True,
    )
    with pytest.raises(LegacyImportError) as captured:
        scan_legacy_project(topology, "read-only-raw", other_generation)
    assert captured.value.code == "legacy_import_scan_invalid"
    assert captured.value.reason == "read_only_entities_excluded"


def test_scan_requires_exact_live_topology_and_path_free_errors(
    tmp_path: Path,
) -> None:
    with pytest.raises(LegacyImportError) as wrong_authority:
        scan_legacy_project(object(), "secret-project", 1)  # type: ignore[arg-type]
    assert wrong_authority.value.reason == "topology_authority_required"
    assert "secret-project" not in str(wrong_authority.value)

    topology = _topology(tmp_path)
    with pytest.raises(LegacyImportError) as oversized:
        scan_legacy_project(topology, "界" * 1_000_000, 1)
    assert oversized.value.reason == "project_id_invalid"
    validation_source = inspect.getsource(legacy_module._validate_scan_request)
    assert validation_source.index("_validate_project_id") < (
        validation_source.index('.encode("utf-8"')
    )

    generation, _document_value = _create_live_project(
        topology,
        "secret-project",
    )
    with pytest.raises(LegacyImportError) as stale:
        scan_legacy_project(topology, "secret-project", generation + 1)
    assert stale.value.code == "legacy_import_scan_failed"
    assert stale.value.reason == "project_generation_stale"
    assert stale.value.args == ("legacy_import_scan_failed",)
    assert "secret-project" not in str(stale.value)


def test_manifest_virtual_payload_golden_sorting_and_closed_shape() -> None:
    alpha, alpha_snapshot = _deterministic_handle(
        "阿尔法",
        generation=2,
        title="世界",
        storage_version="legacy",
    )
    beta, beta_snapshot = _deterministic_handle(
        "beta",
        generation=3,
        title="hello",
    )
    package, receipt = prepare_legacy_import(IMPORT_ID, (alpha, beta))

    manifest = decode_payload_bounded(
        package.manifest_bytes,
        state_module.MAX_LEGACY_MANIFEST_BYTES,
        state_module.MAX_LEGACY_MANIFEST_NODES,
    )
    assert type(manifest) is dict
    assert set(manifest) == {
        "schema",
        "protocol",
        "payload_protocol",
        "import_id",
        "selection",
        "project_count",
        "entity_count",
        "row_snapshot_bytes",
        "framed_payload_bytes",
        "payload_sha256",
        "projects",
    }
    assert manifest["schema"] == state_module.LEGACY_MANIFEST_SCHEMA
    assert manifest["protocol"] == state_module.LEGACY_BATCH_PROTOCOL
    assert manifest["payload_protocol"] == state_module.LEGACY_PAYLOAD_PROTOCOL
    assert manifest["selection"] == "explicit_projects"
    assert [row["project_id"] for row in manifest["projects"]] == [
        "beta",
        "阿尔法",
    ]
    assert all(set(row) == {
        "project_id",
        "generation",
        "document_id",
        "document_version",
        "storage_version",
        "source_head_sha256",
        "source_head_bytes",
        "business_root_sha256",
        "entity_count",
        "row_snapshot_bytes",
    } for row in manifest["projects"])

    payload_sha256, framed_payload_bytes = _manual_payload_digest(
        (beta_snapshot, alpha_snapshot),
    )
    assert package.payload_sha256 == payload_sha256
    assert package.framed_payload_bytes == framed_payload_bytes
    assert manifest["payload_sha256"] == payload_sha256.hex()
    assert package.manifest_sha256 == _framed_digest(
        state_module._MANIFEST_HASH_DOMAIN,
        package.manifest_bytes,
    )
    assert package.batch_sha256 == _framed_digest(
        state_module._BATCH_HASH_DOMAIN,
        package.manifest_bytes,
        package.payload_sha256,
    )
    assert receipt.status == "prepared_verified_payload"
    assert not hasattr(receipt, "outcome")
    assert not hasattr(receipt, "committed")
    assert not hasattr(receipt, "authority")
    assert receipt.batch_sha256 == package.batch_sha256
    assert receipt.manifest_sha256 == package.manifest_sha256
    assert receipt.payload_sha256 == package.payload_sha256

    # Fixed values catch accidental framing/domain/schema changes across Python
    # runtimes.  They are populated from the independently checked vector above.
    assert package.payload_sha256.hex() == (
        "93f638979f5ee1f54884225169684ef539c950e981c3d8b62a79840a64c3c9ba"
    )
    assert package.manifest_sha256.hex() == (
        "45f87e568c840ac2138684064c295e0f392d434c7c52f29bee095f5adf50c222"
    )
    assert package.batch_sha256.hex() == (
        "beb4b63f65f4417a211a6af116dc7dfdebc9377a85c145f3f3b445177b467272"
    )


def test_verified_import_plan_is_complete_atomic_and_immutable() -> None:
    alpha, alpha_snapshot = _deterministic_handle(
        "plan-alpha",
        generation=4,
        title="alpha",
        storage_version="legacy",
    )
    beta, beta_snapshot = _deterministic_handle(
        "plan-beta",
        generation=9,
        title="beta",
    )
    package, receipt = prepare_legacy_import(IMPORT_ID, (beta, alpha))
    reservation, plan = legacy_module._reserve_prepared_legacy_import(
        package,
        receipt,
        _attempt_token=object(),
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )

    assert type(plan) is state_module.VerifiedLegacyImportPlan
    assert plan.plan_protocol == state_module.LEGACY_IMPORT_PLAN_PROTOCOL
    assert plan.import_id == IMPORT_ID
    assert plan.transaction_id == "legacy-import:" + IMPORT_ID
    assert plan.receipt_kind == "legacy_import_v1"
    assert plan.receipt_protocol == "ze.legacy-import.sqlite-receipt.v1"
    assert plan.status == "imported"
    assert plan.manifest_schema == state_module.LEGACY_MANIFEST_SCHEMA
    assert plan.batch_protocol == state_module.LEGACY_BATCH_PROTOCOL
    assert plan.payload_protocol == state_module.LEGACY_PAYLOAD_PROTOCOL
    assert plan.selection == "explicit_projects"
    assert plan.project_count == 2
    assert plan.entity_count == 2
    assert plan.row_snapshot_bytes == package.row_snapshot_bytes
    assert plan.framed_payload_bytes == package.framed_payload_bytes
    assert plan.manifest_bytes == package.manifest_bytes
    assert plan.manifest_sha256 == package.manifest_sha256
    assert plan.payload_sha256 == package.payload_sha256
    assert plan.batch_sha256 == package.batch_sha256
    assert [project.ordinal for project in plan.projects] == [0, 1]
    assert [project.project_id for project in plan.projects] == [
        "plan-alpha",
        "plan-beta",
    ]
    assert plan.snapshots == (alpha_snapshot, beta_snapshot)

    manifest = decode_payload_bounded(
        plan.manifest_bytes,
        state_module.MAX_LEGACY_MANIFEST_BYTES,
        state_module.MAX_LEGACY_MANIFEST_NODES,
    )
    assert manifest["schema"] == plan.manifest_schema
    assert manifest["protocol"] == plan.batch_protocol
    assert manifest["payload_protocol"] == plan.payload_protocol
    assert manifest["import_id"] == plan.import_id
    assert manifest["selection"] == plan.selection
    assert manifest["project_count"] == plan.project_count
    assert manifest["entity_count"] == plan.entity_count
    assert manifest["row_snapshot_bytes"] == plan.row_snapshot_bytes
    assert manifest["framed_payload_bytes"] == plan.framed_payload_bytes
    assert manifest["payload_sha256"] == plan.payload_sha256.hex()
    for descriptor, manifest_row in zip(plan.projects, manifest["projects"]):
        assert type(descriptor) is state_module.VerifiedLegacyProjectPlan
        assert descriptor.project_id == descriptor.snapshot.project.project_id
        assert descriptor.generation == descriptor.snapshot.project.generation
        assert descriptor.document_id == descriptor.snapshot.document.document_id
        assert (
            descriptor.document_version
            == descriptor.snapshot.document.document_version
        )
        assert descriptor.business_root_sha256 == (
            descriptor.snapshot.project.snapshot_sha256
        )
        assert descriptor.entity_count == len(descriptor.snapshot.entities)
        assert descriptor.project_row.project_id == descriptor.project_id
        assert descriptor.project_row.generation == descriptor.generation
        assert descriptor.project_row.snapshot_sha256 == (
            descriptor.business_root_sha256
        )
        assert descriptor.document_row.document_id == descriptor.document_id
        assert descriptor.document_row.document_version == (
            descriptor.document_version
        )
        assert len(descriptor.entity_rows) == descriptor.entity_count
        assert not hasattr(descriptor.project_row, "__dict__")
        assert not hasattr(descriptor.document_row, "__dict__")
        assert all(not hasattr(row, "__dict__") for row in descriptor.entity_rows)
        assert descriptor.project_id == manifest_row["project_id"]
        assert descriptor.generation == manifest_row["generation"]
        assert descriptor.document_id == manifest_row["document_id"]
        assert descriptor.document_version == manifest_row["document_version"]
        assert descriptor.storage_version == manifest_row["storage_version"]
        assert descriptor.source_head_sha256.hex() == (
            manifest_row["source_head_sha256"]
        )
        assert descriptor.source_head_bytes == manifest_row["source_head_bytes"]
        assert descriptor.business_root_sha256.hex() == (
            manifest_row["business_root_sha256"]
        )
        assert descriptor.entity_count == manifest_row["entity_count"]
        assert descriptor.row_snapshot_bytes == manifest_row["row_snapshot_bytes"]

    for forbidden in (
        "package", "receipt", "reservation", "record", "committed_at",
        "outcome", "authority",
    ):
        assert not hasattr(plan, forbidden)
    assert "VerifiedLegacyImportPlan" not in legacy_module.__all__
    assert "VerifiedLegacyImportPlan" not in sdk.__all__
    assert not hasattr(sdk, "VerifiedLegacyImportPlan")
    assert not hasattr(plan, "__dict__")
    assert not hasattr(plan.projects[0], "__dict__")
    with pytest.raises(AttributeError):
        plan.selection = "catalog"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        plan.projects[0].ordinal = 5  # type: ignore[misc]
    with pytest.raises(AttributeError):
        plan.projects[0].project_row.generation = 5  # type: ignore[misc]
    with pytest.raises(TypeError):
        state_module.VerifiedLegacyImportPlan()
    with pytest.raises(TypeError):
        copy.copy(plan)
    with pytest.raises(TypeError):
        pickle.dumps(plan)
    with pytest.raises(TypeError):
        pickle.dumps(plan.projects)

    first_manifest = plan.manifest_bytes
    first_snapshots = plan.snapshots

    legacy_module._finish_prepared_legacy_import(
        reservation,
        outcome="release",
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )
    retried, retried_plan = legacy_module._reserve_prepared_legacy_import(
        package,
        receipt,
        _attempt_token=object(),
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )
    assert retried_plan is not plan
    assert retried_plan.manifest_bytes == first_manifest
    assert retried_plan.snapshots == first_snapshots
    legacy_module._finish_prepared_legacy_import(
        retried,
        outcome="consumed",
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )


@pytest.mark.parametrize(
    "changed_field",
    ("manifest", "nested_project", "bool_alias"),
)
def test_verified_import_plan_rejects_record_change_during_projection(
    monkeypatch: pytest.MonkeyPatch,
    changed_field: str,
) -> None:
    handle, _snapshot = _deterministic_handle(
        "plan-race",
        generation=1,
        title="race",
    )
    package, receipt = prepare_legacy_import(IMPORT_ID, (handle,))
    record = state_module._LEDGER.prepared_package(package)
    real_verify = state_module._verify_prepared_projection

    def verify_then_change(source):
        plan = real_verify(source)
        if changed_field == "manifest":
            record.manifest_bytes += b" "
        elif changed_field == "nested_project":
            record.projects[0].snapshot.project.__dict__["generation"] = 2
        else:
            # bool compares equal to integer 1 in Python; exact-type checking
            # must still reject it during the final sealed-record comparison.
            record.entity_count = True
        return plan

    monkeypatch.setattr(
        state_module,
        "_verify_prepared_projection",
        verify_then_change,
    )
    with pytest.raises(LegacyImportError) as changed:
        legacy_module._reserve_prepared_legacy_import(
            package,
            receipt,
            _attempt_token=object(),
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    assert changed.value.code == "legacy_import_payload_invalid"
    assert changed.value.reason == "prepared_record_changed"
    with pytest.raises(LegacyImportError) as quarantined:
        legacy_module._reserve_prepared_legacy_import(
            package,
            receipt,
            _attempt_token=object(),
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    assert quarantined.value.reason == "adapter_lane_unavailable"


def test_verified_row_views_have_bounded_payload_free_repr_and_no_pickle() -> None:
    secret = "SUPER_SECRET_VALUE"
    handle, _snapshot = _deterministic_handle(
        "safe-row-repr",
        generation=1,
        title=secret,
    )
    package, receipt = prepare_legacy_import(IMPORT_ID, (handle,))
    reservation, plan = legacy_module._reserve_prepared_legacy_import(
        package,
        receipt,
        _attempt_token=object(),
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )
    project = plan.projects[0]
    rows = (
        project.project_row,
        project.document_row,
        *project.entity_rows,
    )
    for row in rows:
        rendered = repr(row)
        assert secret not in rendered
        assert "snapshot_bytes" not in rendered
        assert "snapshot_sha256" not in rendered
        assert len(rendered) < 1_000
        with pytest.raises(TypeError):
            pickle.dumps(row)
    assert repr(project.entity_rows) == "_EntityRowIntents(count=1)"
    with pytest.raises(TypeError):
        pickle.dumps(project.entity_rows)

    huge_secret = "SUPER_SECRET_VALUE" * 100_000
    huge_document_row = state_module._DocumentRowIntent(
        huge_secret,
        huge_secret,
        1,
        huge_secret,
        huge_secret,
        huge_secret,
        huge_secret,
        1,
        1,
        b"secret bytes",
        b"x" * 32,
    )
    huge_entity_row = state_module._EntityRowIntent(
        huge_secret,
        huge_secret,
        huge_secret,
        huge_secret,
        1,
        0,
        huge_secret,
        1,
        1,
        b"secret bytes",
        b"x" * 32,
    )
    assert secret not in repr(huge_document_row)
    assert secret not in repr(huge_entity_row)
    assert len(repr(huge_document_row)) < 200
    assert len(repr(huge_entity_row)) < 200
    legacy_module._finish_prepared_legacy_import(
        reservation,
        outcome="consumed",
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )


def test_verified_import_plan_is_graph_isolated_and_exact_pair_bound() -> None:
    handle, _snapshot = _deterministic_handle(
        "plan-isolated",
        generation=3,
        title="isolated",
    )
    package, receipt = prepare_legacy_import(IMPORT_ID, (handle,))
    record = state_module._LEDGER.prepared_package(package)
    original = record.projects[0].snapshot
    reservation, plan = legacy_module._reserve_prepared_legacy_import(
        package,
        receipt,
        _attempt_token=object(),
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )

    detached = plan.projects[0].snapshot
    assert detached is not original
    assert detached.project is not original.project
    assert detached.document is not original.document
    assert detached.entities[0] is not original.entities[0]
    intended_project_row = plan.projects[0].project_row
    detached.project.__dict__["generation"] = 700
    detached.project.__dict__["snapshot_bytes"] = b"corrupt detached row"
    assert plan.projects[0].generation == 3
    assert plan.projects[0].snapshot.project.generation == 3
    assert intended_project_row.generation == 3
    assert intended_project_row.snapshot_bytes != b"corrupt detached row"
    with pytest.raises(AttributeError):
        intended_project_row.generation = 700  # type: ignore[misc]

    legacy_module._verify_prepared_legacy_import_plan_binding(
        reservation,
        plan,
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )
    forged = object.__new__(state_module.VerifiedLegacyImportPlan)
    with pytest.raises(LegacyImportError) as forged_property:
        _ = forged.import_id
    assert forged_property.value.reason == "verified_plan_invalid"
    with pytest.raises(LegacyImportError) as forged_binding:
        legacy_module._verify_prepared_legacy_import_plan_binding(
            reservation,
            forged,
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    assert forged_binding.value.reason == "verified_plan_binding_invalid"

    # Even a direct mutation of the old dataclass graph cannot alter the
    # isolated plan; the pair verifier additionally refuses stale source state.
    original.project.__dict__["generation"] = 701
    assert plan.projects[0].generation == 3
    assert plan.projects[0].snapshot.project.generation == 3
    with pytest.raises(LegacyImportError) as changed_binding:
        legacy_module._verify_prepared_legacy_import_plan_binding(
            reservation,
            plan,
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    assert changed_binding.value.reason == "prepared_record_changed"
    legacy_module._finish_prepared_legacy_import(
        reservation,
        outcome="quarantine",
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )


def test_bound_plan_recovery_is_explicit_exact_and_keeps_reserve_single_winner() -> None:
    handle, _snapshot = _deterministic_handle(
        "plan-recovery",
        generation=1,
        title="recovery",
    )
    package, receipt = prepare_legacy_import(IMPORT_ID, (handle,))
    attempt_token = object()
    reservation, plan = legacy_module._reserve_prepared_legacy_import(
        package,
        receipt,
        _attempt_token=attempt_token,
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )
    with pytest.raises(LegacyImportError) as duplicate_reserve:
        legacy_module._reserve_prepared_legacy_import(
            package,
            receipt,
            _attempt_token=attempt_token,
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    assert duplicate_reserve.value.reason == "adapter_lane_unavailable"

    recovered_reservation, recovered_plan = (
        legacy_module._recover_prepared_legacy_import(
            package,
            receipt,
            _attempt_token=attempt_token,
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    )
    assert recovered_reservation is reservation
    assert recovered_plan is plan
    with pytest.raises(LegacyImportError) as wrong_attempt:
        legacy_module._recover_prepared_legacy_import(
            package,
            receipt,
            _attempt_token=object(),
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    assert wrong_attempt.value.reason == "adapter_recovery_unavailable"
    recovery_results: list[str] = []
    recovery_lock = threading.Lock()

    def foreign_recovery() -> None:
        try:
            legacy_module._recover_prepared_legacy_import(
                package,
                receipt,
                _attempt_token=object(),
                _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
            )
            result = "stolen"
        except LegacyImportError:
            result = "closed"
        with recovery_lock:
            recovery_results.append(result)

    recovery_threads = [threading.Thread(target=foreign_recovery) for _ in range(8)]
    for thread in recovery_threads:
        thread.start()
    for thread in recovery_threads:
        thread.join(5)
        assert not thread.is_alive()
    assert recovery_results == ["closed"] * 8
    with pytest.raises(LegacyImportError) as wrong_capability:
        legacy_module._recover_prepared_legacy_import(
            package,
            receipt,
            _attempt_token=attempt_token,
            _capability=object(),
        )
    assert wrong_capability.value.reason == "adapter_capability_required"

    legacy_module._finish_prepared_legacy_import(
        reservation,
        outcome="release",
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )
    with pytest.raises(LegacyImportError) as no_active_reserve:
        legacy_module._recover_prepared_legacy_import(
            package,
            receipt,
            _attempt_token=attempt_token,
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    assert no_active_reserve.value.reason == "adapter_recovery_unavailable"


@pytest.mark.parametrize(
    "interrupted_frame, expect_bound_plan",
    (
        ("ledger_reserve", False),
        ("state_reserve", False),
        ("facade_reserve", True),
    ),
)
def test_exact_attempt_recovers_reserve_return_edge_acknowledgement_loss(
    interrupted_frame: str,
    expect_bound_plan: bool,
) -> None:
    handle, _snapshot = _deterministic_handle(
        "reserve-return-edge-" + interrupted_frame,
        generation=1,
        title="return-edge",
    )
    package, receipt = prepare_legacy_import(
        hashlib.sha256(interrupted_frame.encode("ascii")).hexdigest(),
        (handle,),
    )
    attempt_token = object()
    interrupted_code = {
        "ledger_reserve": state_module._LegacyImportLedger.reserve_prepared.__code__,
        "state_reserve": state_module._reserve_prepared_import.__code__,
        "facade_reserve": legacy_module._reserve_prepared_legacy_import.__code__,
    }[interrupted_frame]

    def interrupt(frame, event, _arg):
        if event == "return" and frame.f_code is interrupted_code:
            sys.settrace(None)
            raise KeyboardInterrupt("injected reserve acknowledgement loss")
        return interrupt

    sys.settrace(interrupt)
    try:
        with pytest.raises(KeyboardInterrupt):
            legacy_module._reserve_prepared_legacy_import(
                package,
                receipt,
                _attempt_token=attempt_token,
                _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
            )
    finally:
        sys.settrace(None)

    record = state_module._LEDGER.prepared_package(package)
    registered = state_module._LEDGER._prepared_reservations[id(record)]
    assert registered[1] is record
    assert (registered[3] is not None) is expect_bound_plan

    reservation, plan = legacy_module._recover_prepared_legacy_import(
        package,
        receipt,
        _attempt_token=attempt_token,
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )
    assert reservation is registered[0]
    if expect_bound_plan:
        assert plan is registered[3]
    else:
        assert state_module._LEDGER._prepared_reservations[id(record)][3] is plan
    legacy_module._verify_prepared_legacy_import_plan_binding(
        reservation,
        plan,
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )
    legacy_module._finish_prepared_legacy_import(
        reservation,
        outcome="consumed",
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )


@pytest.mark.parametrize(
    "cleanup_outcome, expected_state, recovery_reason",
    (
        ("release", "available", "adapter_recovery_unavailable"),
        ("quarantine", "quarantined", "prepared_record_changed"),
    ),
)
def test_exact_attempt_finishes_interrupted_reserve_cleanup_without_handle(
    monkeypatch: pytest.MonkeyPatch,
    cleanup_outcome: str,
    expected_state: str,
    recovery_reason: str,
) -> None:
    handle, _snapshot = _deterministic_handle(
        "reserve-cleanup-" + cleanup_outcome,
        generation=1,
        title="cleanup",
    )
    package, receipt = prepare_legacy_import(
        hashlib.sha256(cleanup_outcome.encode("ascii")).hexdigest(),
        (handle,),
    )
    attempt_token = object()

    def fail_projection(_reservation):
        if cleanup_outcome == "release":
            raise KeyboardInterrupt("cancel projection")
        raise LegacyImportError(
            "legacy_import_payload_invalid",
            "injected_projection_failure",
        )

    monkeypatch.setattr(
        legacy_module,
        "_verified_prepared_plan",
        fail_projection,
    )
    finish_code = state_module._LegacyImportLedger.finish_prepared.__code__

    def interrupt_cleanup(frame, event, _arg):
        if (
            event == "line"
            and frame.f_code is finish_code
            and "record.adapter_state = next_state"
            in linecache.getline(frame.f_code.co_filename, frame.f_lineno)
        ):
            sys.settrace(None)
            raise KeyboardInterrupt("interrupt reserve cleanup")
        return interrupt_cleanup

    sys.settrace(interrupt_cleanup)
    try:
        with pytest.raises(KeyboardInterrupt):
            legacy_module._reserve_prepared_legacy_import(
                package,
                receipt,
                _attempt_token=attempt_token,
                _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
            )
    finally:
        sys.settrace(None)

    record = state_module._LEDGER.prepared_package(package)
    registered = state_module._LEDGER._prepared_reservations[id(record)]
    assert registered[2] == cleanup_outcome
    assert registered[3] is None

    with pytest.raises(LegacyImportError) as recovered_cleanup:
        legacy_module._recover_prepared_legacy_import(
            package,
            receipt,
            _attempt_token=attempt_token,
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    assert recovered_cleanup.value.reason == recovery_reason
    assert record.adapter_state == expected_state
    assert record.adapter_token is None
    assert id(record) not in state_module._LEDGER._prepared_reservations

    if cleanup_outcome == "release":
        monkeypatch.undo()
        reservation, _plan = legacy_module._reserve_prepared_legacy_import(
            package,
            receipt,
            _attempt_token=object(),
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
        legacy_module._finish_prepared_legacy_import(
            reservation,
            outcome="consumed",
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )


def test_importer_ready_source_constraints_fail_before_plan_issuance() -> None:
    def shared_document_handle(project_id: str) -> LegacyScanHandle:
        document = _document("shared-document")
        snapshot = capture_business_snapshot(project_id, 1, document).snapshot
        source = ("source:" + project_id).encode("ascii")
        return state_module._issue_scan_handle(
            snapshot,
            source_head_sha256=hashlib.sha256(source).digest(),
            source_head_bytes=len(source),
            storage_version="v2",
        )

    first = shared_document_handle("source-project-a")
    second = shared_document_handle("source-project-b")
    with pytest.raises(LegacyImportError) as duplicate_document:
        prepare_legacy_import(IMPORT_ID, (first, second))
    assert duplicate_document.value.code == "legacy_import_prepare_invalid"
    assert duplicate_document.value.reason == "duplicate_document"
    assert prepare_legacy_import("a" * 64, (first,))[0].project_count == 1
    assert prepare_legacy_import("b" * 64, (second,))[0].project_count == 1

    document = _document("empty-source")
    snapshot = capture_business_snapshot("empty-source", 1, document).snapshot
    with pytest.raises(LegacyImportError) as empty_source:
        state_module._issue_scan_handle(
            snapshot,
            source_head_sha256=hashlib.sha256(b"").digest(),
            source_head_bytes=0,
            storage_version="v2",
        )
    assert empty_source.value.code == "legacy_import_payload_invalid"
    assert empty_source.value.reason == "project_record_invalid"


@pytest.mark.parametrize("pending_quarantine", (False, True))
def test_exact_recovery_quarantines_changed_source_without_reservation_handle(
    pending_quarantine: bool,
) -> None:
    handle, _snapshot = _deterministic_handle(
        "recovery-quarantine",
        generation=1,
        title="recovery-quarantine",
    )
    package, receipt = prepare_legacy_import(IMPORT_ID, (handle,))
    attempt_token = object()
    reservation, plan = legacy_module._reserve_prepared_legacy_import(
        package,
        receipt,
        _attempt_token=attempt_token,
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )
    record = reservation.record
    record_identity = id(record)
    record.manifest_bytes += b"changed"
    if pending_quarantine:
        with state_module._LEDGER._lock:
            state_module._LEDGER._prepared_reservations[record_identity] = (
                reservation,
                record,
                "quarantine",
                plan,
            )
    del reservation, plan
    gc.collect()

    with pytest.raises(LegacyImportError) as changed:
        legacy_module._recover_prepared_legacy_import(
            package,
            receipt,
            _attempt_token=attempt_token,
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    assert changed.value.reason == "prepared_record_changed"
    assert record.adapter_state == "quarantined"
    assert record.adapter_token is None
    assert record_identity not in state_module._LEDGER._prepared_reservations

    with pytest.raises(LegacyImportError) as no_second_recovery:
        legacy_module._recover_prepared_legacy_import(
            package,
            receipt,
            _attempt_token=attempt_token,
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    assert no_second_recovery.value.reason == "adapter_recovery_unavailable"
    with pytest.raises(LegacyImportError) as terminal:
        legacy_module._reserve_prepared_legacy_import(
            package,
            receipt,
            _attempt_token=object(),
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    assert terminal.value.reason == "adapter_lane_unavailable"


def test_manifest_node_budget_covers_the_documented_10k_project_batch() -> None:
    handle, _snapshot = _deterministic_handle(
        "template",
        generation=1,
        title="template",
    )
    package, _receipt = prepare_legacy_import(IMPORT_ID, (handle,))
    manifest = decode_payload_bounded(
        package.manifest_bytes,
        state_module.MAX_LEGACY_MANIFEST_BYTES,
        state_module.MAX_LEGACY_MANIFEST_NODES,
    )
    row = manifest["projects"][0]
    manifest["projects"] = [dict(row) for _index in range(10_000)]
    manifest["project_count"] = 10_000
    encoded = encode_payload_bounded(
        manifest,
        state_module.MAX_LEGACY_MANIFEST_BYTES,
        state_module.MAX_LEGACY_MANIFEST_NODES,
    )
    assert len(encoded) < state_module.MAX_LEGACY_MANIFEST_BYTES


def test_carriers_are_exact_facade_identity_and_expose_no_domain_graph() -> None:
    handle, _snapshot = _deterministic_handle(
        "carrier",
        generation=1,
        title="carrier",
    )
    package, receipt = prepare_legacy_import(IMPORT_ID, (handle,))

    assert LegacyScanHandle is state_module.LegacyScanHandle
    assert FrozenLegacyImport is state_module.FrozenLegacyImport
    assert PreparedLegacyPayloadReceipt is state_module.PreparedLegacyPayloadReceipt
    assert LegacyImportError is state_module.LegacyImportError
    for value in (handle, package, receipt):
        assert type(value).__module__ == "sdk.legacy_import"
        assert type(value).__qualname__ == type(value).__name__
        for forbidden in ("document", "snapshot", "payload", "entities"):
            assert not hasattr(value, forbidden)
        with pytest.raises(TypeError):
            copy.copy(value)
        with pytest.raises(TypeError):
            copy.deepcopy(value)

    assert legacy_module.__all__ == [
        "FrozenLegacyImport",
        "LegacyImportError",
        "LegacyScanHandle",
        "PreparedLegacyPayloadReceipt",
        "detach_legacy_runtime_head",
        "prepare_legacy_import",
        "scan_legacy_project",
    ]
    assert not hasattr(sdk, "scan_legacy_project")


def test_forged_subclass_receipt_pair_and_adapter_capability_are_rejected() -> None:
    handle, _snapshot = _deterministic_handle(
        "adapter",
        generation=1,
        title="adapter",
    )
    package, receipt = prepare_legacy_import(IMPORT_ID, (handle,))

    with pytest.raises(TypeError):
        LegacyScanHandle()
    forged_handle = object.__new__(LegacyScanHandle)
    with pytest.raises(LegacyImportError):
        _ = forged_handle.project_id

    class ForgedReceipt(PreparedLegacyPayloadReceipt):
        pass

    forged_receipt = object.__new__(ForgedReceipt)
    with pytest.raises(LegacyImportError):
        _consume_prepared_for_test(
            package,
            forged_receipt,
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    with pytest.raises(LegacyImportError) as capability_error:
        _consume_prepared_for_test(
            package,
            receipt,
            _capability=object(),
        )
    assert capability_error.value.reason == "adapter_capability_required"

    snapshots = _consume_prepared_for_test(
        package,
        receipt,
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )
    assert type(snapshots) is tuple and len(snapshots) == 1
    assert verify_business_snapshot(snapshots[0]) is snapshots[0]
    with pytest.raises(LegacyImportError) as consumed:
        _consume_prepared_for_test(
            package,
            receipt,
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    assert consumed.value.reason == "adapter_lane_unavailable"


def test_mismatched_prepared_receipt_does_not_consume_package() -> None:
    first, _ = _deterministic_handle("first", generation=1, title="first")
    second, _ = _deterministic_handle("second", generation=1, title="second")
    first_package, first_receipt = prepare_legacy_import("1" * 64, (first,))
    _second_package, second_receipt = prepare_legacy_import("2" * 64, (second,))

    with pytest.raises(LegacyImportError) as mismatch:
        _consume_prepared_for_test(
            first_package,
            second_receipt,
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    assert mismatch.value.reason == "prepared_pair_invalid"
    snapshots = _consume_prepared_for_test(
        first_package,
        first_receipt,
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )
    assert snapshots[0].project.project_id == "first"


def test_prepared_tamper_is_reverified_then_quarantined() -> None:
    handle, _ = _deterministic_handle("tamper", generation=1, title="tamper")
    package, receipt = prepare_legacy_import(IMPORT_ID, (handle,))
    record = state_module._LEDGER.prepared_package(package)
    record.manifest_bytes += b" "

    with pytest.raises(LegacyImportError) as tampered:
        _consume_prepared_for_test(
            package,
            receipt,
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    assert tampered.value.reason == "prepared_record_tampered"
    with pytest.raises(LegacyImportError) as quarantined:
        _consume_prepared_for_test(
            package,
            receipt,
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    assert quarantined.value.reason == "adapter_lane_unavailable"


def test_batch_build_failure_releases_every_payload_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, _ = _deterministic_handle("one", generation=1, title="one")
    second, _ = _deterministic_handle("two", generation=1, title="two")
    real_builder = state_module._build_prepared_record

    def fail_build(*_args, **_kwargs):
        raise LegacyImportError("legacy_import_manifest_invalid", "injected")

    monkeypatch.setattr(state_module, "_build_prepared_record", fail_build)
    with pytest.raises(LegacyImportError):
        prepare_legacy_import(IMPORT_ID, (second, first))
    monkeypatch.setattr(state_module, "_build_prepared_record", real_builder)

    package, receipt = prepare_legacy_import(IMPORT_ID, (second, first))
    assert package.project_count == 2
    assert receipt.project_count == 2


def test_second_ledger_insert_failure_is_atomic_and_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, _ = _deterministic_handle("atomic-one", generation=1, title="one")
    second, _ = _deterministic_handle("atomic-two", generation=1, title="two")
    real_receipts = state_module._LEDGER._receipts

    class FailingReceiptLedger(weakref.WeakKeyDictionary):
        def __setitem__(self, _key, _value):
            raise MemoryError("injected second ledger insert failure")

    monkeypatch.setattr(
        state_module._LEDGER,
        "_receipts",
        FailingReceiptLedger(),
    )
    with pytest.raises(LegacyImportError) as failed:
        prepare_legacy_import(IMPORT_ID, (first, second))
    assert failed.value.reason == "internal_failure"
    monkeypatch.setattr(state_module._LEDGER, "_receipts", real_receipts)

    package, receipt = prepare_legacy_import(IMPORT_ID, (first, second))
    assert package.project_count == 2
    assert receipt.status == "prepared_verified_payload"


def test_duplicate_and_budget_failures_consume_zero_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle, _ = _deterministic_handle("budget", generation=1, title="budget")
    with pytest.raises(LegacyImportError) as invalid_id:
        prepare_legacy_import("A" * 64, (handle,))
    assert invalid_id.value.reason == "import_id_invalid"
    with pytest.raises(LegacyImportError) as list_input:
        prepare_legacy_import(IMPORT_ID, [handle])  # type: ignore[arg-type]
    assert list_input.value.reason == "handles_must_be_nonempty_tuple"
    with pytest.raises(LegacyImportError) as duplicate:
        prepare_legacy_import(IMPORT_ID, (handle, handle))
    assert duplicate.value.reason == "duplicate_handle"

    original_budget = state_module.MAX_LEGACY_ROW_SNAPSHOT_BYTES
    monkeypatch.setattr(
        state_module,
        "MAX_LEGACY_ROW_SNAPSHOT_BYTES",
        handle.row_snapshot_bytes - 1,
    )
    with pytest.raises(LegacyImportError) as over_budget:
        prepare_legacy_import(IMPORT_ID, (handle,))
    assert over_budget.value.reason == "row_snapshot_bytes_exceeded"
    monkeypatch.setattr(
        state_module,
        "MAX_LEGACY_ROW_SNAPSHOT_BYTES",
        original_budget,
    )

    package, _receipt = prepare_legacy_import(IMPORT_ID, (handle,))
    assert package.project_count == 1


def test_payload_and_runtime_lanes_are_independent_and_each_one_shot() -> None:
    handle, _ = _deterministic_handle("lanes", generation=4, title="lanes")
    package, receipt = prepare_legacy_import(IMPORT_ID, (handle,))
    with pytest.raises(LegacyImportError) as payload_twice:
        prepare_legacy_import("f" * 64, (handle,))
    assert payload_twice.value.reason == "payload_lane_unavailable"

    detached = detach_legacy_runtime_head(handle)
    assert type(detached) is DetachedBusinessHead
    assert detached.project_id == "lanes"
    assert detached.generation == 4
    with pytest.raises(LegacyImportError) as runtime_twice:
        detach_legacy_runtime_head(handle)
    assert runtime_twice.value.reason == "runtime_lane_unavailable"

    snapshots = _consume_prepared_for_test(
        package,
        receipt,
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )
    assert snapshots[0].project.project_id == "lanes"


@pytest.mark.parametrize("lane", ("payload", "runtime"))
def test_same_lane_concurrency_has_exactly_one_winner(lane: str) -> None:
    handle, _ = _deterministic_handle(
        "concurrent-" + lane,
        generation=1,
        title=lane,
    )
    barrier = threading.Barrier(8)
    results: list[str] = []
    kept: list[Any] = []
    result_lock = threading.Lock()

    def worker(index: int) -> None:
        barrier.wait()
        try:
            if lane == "payload":
                value = prepare_legacy_import(format(index, "064x"), (handle,))
            else:
                value = detach_legacy_runtime_head(handle)
            outcome = "success"
            with result_lock:
                kept.append(value)
        except LegacyImportError:
            outcome = "closed"
        with result_lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    assert results.count("success") == 1
    assert results.count("closed") == 7


def test_private_adapter_lane_concurrency_has_exactly_one_winner() -> None:
    handle, _ = _deterministic_handle(
        "concurrent-adapter",
        generation=1,
        title="adapter",
    )
    package, receipt = prepare_legacy_import(IMPORT_ID, (handle,))
    barrier = threading.Barrier(8)
    results: list[str] = []
    result_lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            _consume_prepared_for_test(
                package,
                receipt,
                _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
            )
            outcome = "success"
        except LegacyImportError:
            outcome = "closed"
        with result_lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    assert results.count("success") == 1
    assert results.count("closed") == 7


def test_fork_resets_locked_ledger_and_rejects_every_inherited_carrier() -> None:
    handle, _snapshot = _deterministic_handle(
        "fork-parent",
        generation=7,
        title="parent",
    )
    package, receipt = prepare_legacy_import(IMPORT_ID, (handle,))
    parent_pid = os.getpid()
    parent_epoch = state_module._LEDGER._issuer_epoch
    parent_lock = state_module._LEDGER._lock
    parent_scans = state_module._LEDGER._scans

    scan_record = state_module._LEDGER.scan(handle)
    prepared_record = state_module._LEDGER.prepared_package(package)
    assert (scan_record.issuer_pid, scan_record.issuer_epoch) == (
        parent_pid,
        parent_epoch,
    )
    assert (prepared_record.issuer_pid, prepared_record.issuer_epoch) == (
        parent_pid,
        parent_epoch,
    )

    locked = threading.Event()
    release = threading.Event()

    def hold_inherited_lock() -> None:
        with parent_lock:
            locked.set()
            assert release.wait(15)

    holder = threading.Thread(target=hold_inherited_lock)
    holder.start()
    assert locked.wait(5)

    def child_probe() -> dict[str, Any]:
        def diagnosed(operation) -> list[str]:
            try:
                operation()
            except LegacyImportError as exc:
                return [exc.code, exc.reason]
            return ["accepted", "accepted"]

        inherited = {
            "handle": diagnosed(lambda: handle.project_id),
            "package": diagnosed(lambda: package.import_id),
            "receipt": diagnosed(lambda: receipt.status),
            "runtime": diagnosed(lambda: detach_legacy_runtime_head(handle)),
            "adapter": diagnosed(lambda: _consume_prepared_for_test(
                package,
                receipt,
                _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
            )),
        }
        fresh_handle, _fresh_snapshot = _deterministic_handle(
            "fork-child",
            generation=1,
            title="child",
        )
        fresh_record = state_module._LEDGER.scan(fresh_handle)
        fresh_package, fresh_receipt = prepare_legacy_import(
            "a" * 64,
            (fresh_handle,),
        )
        fresh_snapshots = _consume_prepared_for_test(
            fresh_package,
            fresh_receipt,
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
        return {
            "pid": os.getpid(),
            "epoch": state_module._LEDGER._issuer_epoch,
            "lock_replaced": state_module._LEDGER._lock is not parent_lock,
            "scans_replaced": state_module._LEDGER._scans is not parent_scans,
            "fresh_issuer": [fresh_record.issuer_pid, fresh_record.issuer_epoch],
            "fresh_project": fresh_snapshots[0].project.project_id,
            "inherited": inherited,
        }

    try:
        child = _fork_json(child_probe)
    finally:
        release.set()
        holder.join(5)
    assert not holder.is_alive()
    assert child.get("fatal") is None
    assert child["pid"] != parent_pid
    assert child["epoch"] == parent_epoch + 1
    assert child["lock_replaced"] is True
    assert child["scans_replaced"] is True
    assert child["fresh_issuer"] == [child["pid"], child["epoch"]]
    assert child["fresh_project"] == "fork-child"
    assert child["inherited"] == {
        "adapter": ["legacy_import_receipt_invalid", "prepared_pair_invalid"],
        "handle": ["legacy_import_handle_invalid", "scan_handle_invalid"],
        "package": ["legacy_import_payload_invalid", "prepared_package_invalid"],
        "receipt": ["legacy_import_receipt_invalid", "prepared_receipt_invalid"],
        "runtime": ["legacy_import_handle_invalid", "scan_handle_invalid"],
    }

    # The child's reset mutated only its copy.  The parent's carriers and lock
    # state remain valid and both independent lanes can still finish.
    assert handle.project_id == "fork-parent"
    assert package.import_id == IMPORT_ID
    assert receipt.status == "prepared_verified_payload"
    assert detach_legacy_runtime_head(handle).project_id == "fork-parent"
    assert _consume_prepared_for_test(
        package,
        receipt,
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )[0].project.project_id == "fork-parent"


def test_inherited_prepared_reservation_cannot_verify_or_finish_in_child() -> None:
    handle, _snapshot = _deterministic_handle(
        "fork-reservation",
        generation=1,
        title="reserved",
    )
    package, receipt = prepare_legacy_import(IMPORT_ID, (handle,))
    reservation, plan = legacy_module._reserve_prepared_legacy_import(
        package,
        receipt,
        _attempt_token=object(),
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )
    assert plan.snapshots[0].project.project_id == "fork-reservation"

    def child_probe() -> dict[str, Any]:
        outcomes: dict[str, list[str]] = {}
        for label, operation in {
            "plan": lambda: plan.import_id,
            "verify": lambda: state_module._verified_prepared_plan(
                reservation,
            ),
            "finish": lambda: legacy_module._finish_prepared_legacy_import(
                reservation,
                outcome="release",
                _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
            ),
        }.items():
            try:
                operation()
            except LegacyImportError as exc:
                outcomes[label] = [exc.code, exc.reason]
            else:
                outcomes[label] = ["accepted", "accepted"]
        return {"outcomes": outcomes}

    child = _fork_json(child_probe)
    assert child == {
        "outcomes": {
            "finish": [
                "legacy_import_payload_invalid",
                "adapter_reservation_lost",
            ],
            "plan": [
                "legacy_import_payload_invalid",
                "verified_plan_invalid",
            ],
            "verify": [
                "legacy_import_payload_invalid",
                "adapter_reservation_lost",
            ],
        },
    }

    # The parent retains the active reservation and may explicitly release it.
    legacy_module._finish_prepared_legacy_import(
        reservation,
        outcome="release",
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )
    retried, retried_plan = legacy_module._reserve_prepared_legacy_import(
        package,
        receipt,
        _attempt_token=object(),
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )
    assert retried_plan is not plan
    assert retried_plan.snapshots == plan.snapshots
    legacy_module._finish_prepared_legacy_import(
        retried,
        outcome="consumed",
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )


def test_inherited_scan_lane_reservations_cannot_verify_abort_or_finish() -> None:
    handle, _snapshot = _deterministic_handle(
        "fork-scan-reservations",
        generation=1,
        title="reserved",
    )
    payload_reservation = state_module._LEDGER.reserve_payload_batch((handle,))
    runtime_reservation = state_module._reserve_runtime_handle(handle)

    def child_probe() -> dict[str, Any]:
        outcomes: dict[str, list[str]] = {}
        for label, operation in {
            "payload_verify": lambda: state_module._LEDGER.payload_reservation_records(
                payload_reservation,
            ),
            "runtime_verify": lambda: state_module._verified_runtime_snapshot(
                runtime_reservation,
            ),
            "runtime_finish": lambda: state_module._finish_runtime_handle(
                runtime_reservation,
            ),
        }.items():
            try:
                operation()
            except LegacyImportError as exc:
                outcomes[label] = [exc.code, exc.reason]
            else:
                outcomes[label] = ["accepted", "accepted"]

        # Abort is intentionally idempotent for exception cleanup, but an
        # inherited token must be ignored rather than mutating stale records.
        state_module._LEDGER.abort_payload_batch(payload_reservation)
        state_module._abort_runtime_handle(runtime_reservation)
        return {
            "outcomes": outcomes,
            "payload_state": payload_reservation.records[0].payload_state,
            "runtime_state": runtime_reservation.record.runtime_state,
        }

    child = _fork_json(child_probe)
    assert child == {
        "outcomes": {
            "payload_verify": [
                "legacy_import_prepare_failed",
                "payload_reservation_lost",
            ],
            "runtime_finish": [
                "legacy_import_runtime_failed",
                "runtime_reservation_lost",
            ],
            "runtime_verify": [
                "legacy_import_runtime_failed",
                "runtime_reservation_lost",
            ],
        },
        "payload_state": "reserved",
        "runtime_state": "reserved",
    }

    state_module._LEDGER.abort_payload_batch(payload_reservation)
    state_module._abort_runtime_handle(runtime_reservation)
    package, receipt = prepare_legacy_import(IMPORT_ID, (handle,))
    assert package.project_count == receipt.project_count == 1
    assert detach_legacy_runtime_head(handle).project_id == "fork-scan-reservations"


def test_prepared_reservation_finish_matrix_is_identity_bound_and_explicit() -> None:
    handle, _snapshot = _deterministic_handle(
        "reservation-matrix",
        generation=1,
        title="matrix",
    )
    package, receipt = prepare_legacy_import(IMPORT_ID, (handle,))
    reservation, plan = legacy_module._reserve_prepared_legacy_import(
        package,
        receipt,
        _attempt_token=object(),
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )
    assert plan.snapshots[0].project.project_id == "reservation-matrix"

    with pytest.raises(LegacyImportError) as concurrently_reserved:
        legacy_module._reserve_prepared_legacy_import(
            package,
            receipt,
            _attempt_token=object(),
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    assert concurrently_reserved.value.reason == "adapter_lane_unavailable"

    cloned = copy.copy(reservation)
    assert cloned is not reservation and cloned.token is reservation.token
    with pytest.raises(LegacyImportError) as cloned_reservation:
        state_module._verified_prepared_plan(cloned)
    assert cloned_reservation.value.reason == "adapter_reservation_lost"

    forged = state_module._PreparedReservation(
        package=reservation.package,
        receipt=reservation.receipt,
        record=reservation.record,
        token=object(),
        attempt_token=reservation.attempt_token,
        issuer_pid=reservation.issuer_pid,
        issuer_epoch=reservation.issuer_epoch,
    )
    with pytest.raises(LegacyImportError) as wrong_token_verify:
        state_module._verified_prepared_plan(forged)
    assert wrong_token_verify.value.reason == "adapter_reservation_lost"
    with pytest.raises(LegacyImportError) as wrong_token_finish:
        legacy_module._finish_prepared_legacy_import(
            forged,
            outcome="release",
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    assert wrong_token_finish.value.reason == "adapter_reservation_lost"

    uninitialized = object.__new__(state_module._PreparedReservation)
    with pytest.raises(LegacyImportError) as uninitialized_verify:
        state_module._verified_prepared_plan(uninitialized)
    assert uninitialized_verify.value.reason == "adapter_reservation_lost"
    with pytest.raises(LegacyImportError) as uninitialized_finish:
        legacy_module._finish_prepared_legacy_import(
            uninitialized,
            outcome="release",
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    assert uninitialized_finish.value.reason == "adapter_reservation_lost"

    with pytest.raises(LegacyImportError) as wrong_capability:
        legacy_module._finish_prepared_legacy_import(
            reservation,
            outcome="release",
            _capability=object(),
        )
    assert wrong_capability.value.reason == "adapter_capability_required"
    with pytest.raises(LegacyImportError) as wrong_outcome:
        legacy_module._finish_prepared_legacy_import(
            reservation,
            outcome="commit",
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    assert wrong_outcome.value.reason == "adapter_outcome_invalid"

    # Failed forged/capability/outcome attempts leave the real token active.
    legacy_module._finish_prepared_legacy_import(
        reservation,
        outcome="release",
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )
    consumed, _plan = legacy_module._reserve_prepared_legacy_import(
        package,
        receipt,
        _attempt_token=object(),
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )
    legacy_module._finish_prepared_legacy_import(
        consumed,
        outcome="consumed",
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )
    # The exact same terminal acknowledgement is idempotent; a caller that
    # loses the return edge after the active registry pop may retry safely.
    legacy_module._finish_prepared_legacy_import(
        consumed,
        outcome="consumed",
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )
    with pytest.raises(LegacyImportError) as conflicting_finish:
        legacy_module._finish_prepared_legacy_import(
            consumed,
            outcome="quarantine",
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    assert conflicting_finish.value.reason == "adapter_outcome_conflict"
    with pytest.raises(LegacyImportError) as consumed_again:
        legacy_module._reserve_prepared_legacy_import(
            package,
            receipt,
            _attempt_token=object(),
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    assert consumed_again.value.reason == "adapter_lane_unavailable"

    quarantine_handle, _ = _deterministic_handle(
        "reservation-quarantine",
        generation=1,
        title="quarantine",
    )
    quarantine_package, quarantine_receipt = prepare_legacy_import(
        "b" * 64,
        (quarantine_handle,),
    )
    quarantined, _plan = legacy_module._reserve_prepared_legacy_import(
        quarantine_package,
        quarantine_receipt,
        _attempt_token=object(),
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )
    legacy_module._finish_prepared_legacy_import(
        quarantined,
        outcome="quarantine",
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )
    with pytest.raises(LegacyImportError) as quarantined_again:
        legacy_module._reserve_prepared_legacy_import(
            quarantine_package,
            quarantine_receipt,
            _attempt_token=object(),
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    assert quarantined_again.value.reason == "adapter_lane_unavailable"


def test_prepared_reservation_registry_survives_caller_reference_gc() -> None:
    handle, _snapshot = _deterministic_handle(
        "reservation-gc",
        generation=1,
        title="gc",
    )
    package, receipt = prepare_legacy_import(IMPORT_ID, (handle,))
    reservation, issued_plan = legacy_module._reserve_prepared_legacy_import(
        package,
        receipt,
        _attempt_token=object(),
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )
    reservation_id = id(reservation)
    record_id = id(reservation.record)
    del reservation
    gc.collect()

    registered = state_module._LEDGER._prepared_reservations[record_id]
    held_reservation, record, pending_outcome, held_plan = registered
    assert id(held_reservation) == reservation_id
    assert pending_outcome is None
    assert held_plan is issued_plan
    assert record.adapter_state == "reserved"
    assert record.adapter_token is held_reservation.token
    with pytest.raises(LegacyImportError) as still_reserved:
        legacy_module._reserve_prepared_legacy_import(
            package,
            receipt,
            _attempt_token=object(),
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    assert still_reserved.value.reason == "adapter_lane_unavailable"

    legacy_module._finish_prepared_legacy_import(
        held_reservation,
        outcome="release",
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )
    assert record_id not in state_module._LEDGER._prepared_reservations
    assert _consume_prepared_for_test(
        package,
        receipt,
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )[0].project.project_id == "reservation-gc"


@pytest.mark.parametrize(
    "interrupt_line",
    (
        "record.adapter_state = next_state",
        "record.adapter_token = None",
        "self._prepared_reservations.pop(record_identity, None)",
        "__return__",
    ),
)
def test_prepared_finish_async_interrupt_is_same_outcome_retryable(
    interrupt_line: str,
) -> None:
    case_id = hashlib.sha256(interrupt_line.encode("utf-8")).hexdigest()
    handle, _snapshot = _deterministic_handle(
        "reservation-interrupt-" + case_id[:12],
        generation=1,
        title="interrupt",
    )
    package, receipt = prepare_legacy_import(
        case_id,
        (handle,),
    )
    reservation, _plan = legacy_module._reserve_prepared_legacy_import(
        package,
        receipt,
        _attempt_token=object(),
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )
    finish_code = state_module._LegacyImportLedger.finish_prepared.__code__

    def interrupt(frame, event, _arg):
        if (
            event == "return"
            and frame.f_code is finish_code
            and interrupt_line == "__return__"
        ):
            sys.settrace(None)
            raise KeyboardInterrupt("injected finish acknowledgement loss")
        if (
            event == "line"
            and frame.f_code is finish_code
            and interrupt_line
            in linecache.getline(frame.f_code.co_filename, frame.f_lineno)
        ):
            sys.settrace(None)
            raise KeyboardInterrupt("injected async finish interruption")
        return interrupt

    sys.settrace(interrupt)
    try:
        with pytest.raises(KeyboardInterrupt):
            legacy_module._finish_prepared_legacy_import(
                reservation,
                outcome="release",
                _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
            )
    finally:
        sys.settrace(None)

    registered = state_module._LEDGER._prepared_reservations.get(
        id(reservation.record)
    )
    if interrupt_line == "__return__":
        assert registered is None
        assert state_module._LEDGER._prepared_resolutions[reservation] == "release"
    else:
        assert registered is not None
        assert registered[0] is reservation
        assert registered[1] is reservation.record
        assert registered[2] == "release"
    with pytest.raises(LegacyImportError) as conflicting_retry:
        legacy_module._finish_prepared_legacy_import(
            reservation,
            outcome="quarantine",
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    assert conflicting_retry.value.reason == "adapter_outcome_conflict"

    legacy_module._finish_prepared_legacy_import(
        reservation,
        outcome="release",
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )
    retried, _retried_plan = legacy_module._reserve_prepared_legacy_import(
        package,
        receipt,
        _attempt_token=object(),
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )
    legacy_module._finish_prepared_legacy_import(
        retried,
        outcome="consumed",
        _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
    )


def test_scan_detach_and_noop_fence_produce_runtime_admission(
    tmp_path: Path,
) -> None:
    topology = _topology(tmp_path)
    generation, _document_value = _create_live_project(topology, "runtime")
    handle = scan_legacy_project(topology, "runtime", generation)
    detached = detach_legacy_runtime_head(handle)

    admission = rehydrate_runtime(
        detached,
        _declarations(),
        authority_fence=_current_fence,
    )
    assert type(admission) is RuntimeAdmission
    assert admission.project_id == "runtime"
    assert admission.generation == generation
    assert admission.authority_proof == "canonical_noop"
    assert admission.authority_revision == "legacy-file-revision-1"
    assert admission.document.get("card", "row").data == {"title": "hello"}


def test_private_adapter_seam_has_exactly_one_private_sqlite_consumer() -> None:
    assert not hasattr(legacy_module, "_consume_prepared_legacy_import")
    sdk_root = Path(legacy_module.__file__).parent
    private_adapter = sdk_root / "_legacy_import_sqlite_adapter.py"
    sensitive = (
        "_reserve_prepared_legacy_import",
        "_recover_prepared_legacy_import",
        "_verify_prepared_legacy_import_plan_binding",
        "_finish_prepared_legacy_import",
        "_LEGACY_IMPORT_ADAPTER_CAPABILITY",
    )
    consumers = []
    for path in sdk_root.glob("*.py"):
        if path.name == "legacy_import.py" or path.name.startswith("test_"):
            continue
        source = path.read_text(encoding="utf-8")
        if any(name in source for name in sensitive):
            consumers.append(path.resolve())
    assert consumers == [private_adapter.resolve()]
