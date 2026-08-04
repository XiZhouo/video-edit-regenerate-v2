"""Crash, concurrency, fencing, and canonical-copy gates for project topology.

These tests deliberately use real child processes for ``flock`` and crash
boundaries.  Fault injection is limited to the file-compatibility coordinator;
no Repository or HTTP behavior is mocked into the topology contract.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import threading
from typing import Any, Callable

import pytest

import sdk.project_topology as topology


CREATE_CRASH_EXIT = 81
DUPLICATE_CRASH_EXIT = 82
FIRST_REPLACE_CRASH_EXIT = 83
ADMISSION_BEFORE_PREPARED_CRASH_EXIT = 90
ADMISSION_PREPARED_CRASH_EXIT = 91
ADMISSION_RECORD_CRASH_EXIT = 92
ADMISSION_COMMITTED_CRASH_EXIT = 93
CUTOVER_FREEZE_CRASH_EXIT = 94
CUTOVER_RUNTIME_GENERATION = "runtime-cutover-test-v1"
CUTOVER_CODE_DIGEST = hashlib.sha256(
    b"ze.project-topology.cutover-freeze.test.v1"
).digest()


class _GenerationInt(int):
    pass


def _write_project(root: Path, project_id: str, value: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "entities").mkdir(exist_ok=True)
    (root / "assets").mkdir(exist_ok=True)
    (root / "document.json").write_text(
        json.dumps({
            "schema": "ze-document",
            "schema_version": "1.0.0",
            "id": project_id,
            "name": project_id,
            "collaboration_mode": "with_save",
            "data_schema": {},
            "config": {},
            "acl": {},
            "collections": {},
            "created_at": 1.0,
            "updated_at": 1.0,
            "_v": 1,
            # Topology tests keep two unknown members to prove metadata-only
            # consumers and rewrites preserve forward-compatible envelopes.
            "project_id": project_id,
            "value": value,
        }),
        encoding="utf-8",
    )
    (root / "entities" / "item.json").write_text(
        json.dumps({"id": "item", "value": value}),
        encoding="utf-8",
    )
    (root / "assets" / "blob.bin").write_bytes(value.encode("utf-8"))


def _build(project_id: str, value: str) -> Callable[[Path], None]:
    return lambda root: _write_project(root, project_id, value)


def _validate(root: Path) -> None:
    payload = json.loads((Path(root) / "document.json").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert isinstance(payload.get("project_id"), str)
    assert isinstance(payload.get("value"), str)


def _rewrite(project_id: str, value: str) -> Callable[[Path], None]:
    def rewrite(root: Path) -> None:
        document_path = Path(root) / "document.json"
        payload = json.loads(document_path.read_text(encoding="utf-8"))
        payload.update({"id": project_id, "project_id": project_id, "value": value})
        document_path.write_text(json.dumps(payload), encoding="utf-8")

    return rewrite


def _new_coordinator(tmp_path: Path) -> topology.ProjectTopologyCoordinator:
    coordinator = topology.ProjectTopologyCoordinator(tmp_path / "projects", lock_timeout=2.0)
    coordinator.initialize(validate=_validate)
    return coordinator


def _install_cutover_freeze(
    coordinator: topology.ProjectTopologyCoordinator,
    admission: topology._RepositoryAdmission,
) -> topology._RepositoryCutoverFreeze:
    return coordinator._install_repository_cutover_freeze(
        admission,
        runtime_generation=CUTOVER_RUNTIME_GENERATION,
        code_digest=CUTOVER_CODE_DIGEST,
        _capability=topology._REPOSITORY_CUTOVER_FREEZE_CAPABILITY,
    )


def _fork_context() -> Any:
    if topology.fcntl is None or "fork" not in mp.get_all_start_methods():
        pytest.skip("project topology process probes require Unix flock and fork")
    return mp.get_context("fork")


def _join(process: Any, expected_exit: int = 0) -> None:
    process.join(10)
    if process.is_alive():
        process.terminate()
        process.join(5)
        pytest.fail("topology probe child process did not terminate")
    assert process.exitcode == expected_exit


def _hold_document_content_lock(
    document_path: str,
    ready: Any,
    release: Any,
) -> None:
    from document import open_document_head

    with open_document_head(Path(document_path), exclusive=True):
        ready.set()
        if not release.wait(10):
            raise TimeoutError("content-lock test release timed out")


def _same_target_worker(
    projects_root: str,
    kind: str,
    label: str,
    barrier: Any,
    results: Any,
) -> None:
    coordinator = topology.ProjectTopologyCoordinator(Path(projects_root), lock_timeout=3.0)
    try:
        barrier.wait(5)
        if kind == "create":
            commit = coordinator.create(
                "same",
                build=_build("same", label),
                validate=_validate,
                operation_id=label * 32,
            )
        else:
            commit = coordinator.duplicate(
                "source",
                "copy",
                rewrite=_rewrite("copy", label),
                validate=_validate,
                operation_id=label * 32,
            )
        results.put(("ok", label, commit.generation))
    except BaseException as exc:  # process probe must report stable framework errors
        results.put((getattr(exc, "code", type(exc).__name__), label, str(exc)))


def _crash_create_prepared(projects_root: str, operation_id: str) -> None:
    coordinator = topology.ProjectTopologyCoordinator(Path(projects_root), lock_timeout=3.0)

    def crash_build(root: Path) -> None:
        _write_project(root, "same", "crashed")
        os._exit(CREATE_CRASH_EXIT)

    coordinator.create(
        "same",
        build=crash_build,
        validate=_validate,
        operation_id=operation_id,
    )


def _crash_duplicate_prepared(projects_root: str, operation_id: str) -> None:
    coordinator = topology.ProjectTopologyCoordinator(Path(projects_root), lock_timeout=3.0)

    def crash_rewrite(_root: Path) -> None:
        os._exit(DUPLICATE_CRASH_EXIT)

    coordinator.duplicate(
        "source",
        "copy",
        rewrite=crash_rewrite,
        validate=_validate,
        operation_id=operation_id,
    )


def _crash_first_replace_after_promote(projects_root: str, operation_id: str) -> None:
    coordinator = topology.ProjectTopologyCoordinator(Path(projects_root), lock_timeout=3.0)
    live = coordinator.path_for("first")
    real_rename = topology.os.rename

    def crash_rename(source: Any, target: Any, *args: Any, **kwargs: Any) -> Any:
        source_path = Path(source)
        target_path = Path(target)
        result = real_rename(source, target, *args, **kwargs)
        if source_path.parent == coordinator.staging_root and target_path == live:
            os._exit(FIRST_REPLACE_CRASH_EXIT)
        return result

    topology.os.rename = crash_rename
    # ``os`` is a process-global module, so replacing its attribute also reaches
    # EntityFileStore's platform capability probe.  Preserve the wrapper's
    # dir-fd capability while this child injects the topology crash.
    topology.os.supports_dir_fd.add(crash_rename)
    coordinator.replace(
        "first",
        build=_build("first", "new"),
        validate=_validate,
        operation_id=operation_id,
    )


def _lock_worker(
    lock_path: str,
    exclusive: bool,
    timeout: float,
    release: Any,
    results: Any,
) -> None:
    try:
        with topology._path_lock(Path(lock_path), exclusive=exclusive, timeout=timeout):
            results.put("acquired")
            if release is not None:
                release.wait(5)
    except topology._PathLockTimeout:
        results.put("timeout")
    except BaseException as exc:
        results.put(f"error:{getattr(exc, 'code', type(exc).__name__)}")


def _activity_barrier_worker(
    projects_root: str,
    ready: Any,
    start: Any,
    release: Any,
    results: Any,
    timeout: float,
) -> None:
    coordinator = topology.ProjectTopologyCoordinator(
        Path(projects_root),
        lock_timeout=timeout,
    )
    try:
        with coordinator.open_live("project") as opened:
            handle = opened
        ready.set()
        if not start.wait(5):
            raise TimeoutError("activity barrier start timed out")
        with coordinator.activity(handle):
            results.put("acquired")
            if release is not None and not release.wait(5):
                raise TimeoutError("activity barrier release timed out")
    except BaseException as exc:
        results.put(getattr(exc, "code", type(exc).__name__))


def _nested_live_activity_worker(
    projects_root: str,
    ready: Any,
    start: Any,
    release: Any,
    results: Any,
    timeout: float,
) -> None:
    coordinator = topology.ProjectTopologyCoordinator(
        Path(projects_root),
        lock_timeout=timeout,
    )
    try:
        if not start.wait(5):
            raise TimeoutError("nested activity start timed out")
        with coordinator.open_live("project") as handle:
            with coordinator.activity(handle):
                results.put("acquired")
                ready.set()
                if release is not None and not release.wait(5):
                    raise TimeoutError("nested activity release timed out")
    except BaseException as exc:
        results.put(getattr(exc, "code", type(exc).__name__))
        ready.set()


def _fork_inherited_lock_probe(lock_path: str, results: Any) -> None:
    inherited_entries = getattr(topology._HELD_LOCKS, "entries", None)
    results.put((
        "registry",
        len(inherited_entries or {}),
        len(topology._HELD_LOCK_STREAMS),
    ))
    try:
        with topology._path_lock(
            Path(lock_path),
            exclusive=True,
            timeout=0.2,
        ):
            results.put("acquired")
    except topology._PathLockTimeout:
        results.put("timeout")
    except BaseException as exc:
        results.put(f"error:{getattr(exc, 'code', type(exc).__name__)}")


def _scan_blocked_mutation_worker(
    projects_root: str,
    kind: str,
    start: Any,
    results: Any,
) -> None:
    coordinator = topology.ProjectTopologyCoordinator(
        Path(projects_root),
        lock_timeout=0.2,
    )
    try:
        if not start.wait(5):
            raise TimeoutError("scan-blocked mutation start timed out")
        if kind == "replace":
            coordinator.replace(
                "project",
                build=_build("project", "replacement"),
                validate=_validate,
                expected_generation=1,
            )
        else:
            coordinator.delete("project", expected_generation=1)
        results.put("committed")
    except BaseException as exc:
        results.put(getattr(exc, "code", type(exc).__name__))


def _admission_reject_worker(projects_root: str, results: Any) -> None:
    coordinator = topology.ProjectTopologyCoordinator(
        Path(projects_root),
        lock_timeout=0.5,
    )
    try:
        coordinator.list_live_ids()
        results.put("admitted")
    except BaseException as exc:
        results.put(getattr(exc, "code", type(exc).__name__))


def _crash_repository_admission_install(projects_root: str, edge: str) -> None:
    real_atomic_json = topology._atomic_json

    def crash_after_write(
        path: Path,
        value: dict[str, Any],
        *,
        mode: int = 0o600,
    ) -> bool:
        phase = value.get("phase")
        if edge == "before_prepared" and phase == "prepared":
            os._exit(ADMISSION_BEFORE_PREPARED_CRASH_EXIT)
        result = real_atomic_json(path, value, mode=mode)
        if edge == "prepared" and phase == "prepared":
            os._exit(ADMISSION_PREPARED_CRASH_EXIT)
        if edge == "record" and value.get("schema") == topology.REPOSITORY_ADMISSION_SCHEMA:
            os._exit(ADMISSION_RECORD_CRASH_EXIT)
        if edge == "committed" and phase == "committed":
            os._exit(ADMISSION_COMMITTED_CRASH_EXIT)
        return result

    topology._atomic_json = crash_after_write
    topology.ProjectTopologyCoordinator(Path(projects_root), lock_timeout=2.0)


def _bootstrap_repository_admission_worker(
    projects_root: str,
    start: Any,
    results: Any,
) -> None:
    try:
        if not start.wait(5):
            raise TimeoutError("bootstrap admission start timed out")
        coordinator = (
            topology.ProjectTopologyCoordinator
            ._bootstrap_existing_legacy_repository_admission(
                Path(projects_root),
                validate=_validate,
                lock_timeout=3.0,
                _capability=topology._REPOSITORY_ADMISSION_BOOTSTRAP_CAPABILITY,
            )
        )
        admission = coordinator._read_repository_admission()
        assert admission is not None
        results.put(("ok", admission.authority_epoch, admission.revision))
    except BaseException as exc:
        results.put((getattr(exc, "code", type(exc).__name__), "", 0))


def _crash_after_repository_cutover_freeze(projects_root: str) -> None:
    coordinator = topology.ProjectTopologyCoordinator(
        Path(projects_root),
        lock_timeout=2.0,
    )
    with coordinator._open_strict_repository_migration(
        _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
    ) as snapshot:
        _install_cutover_freeze(coordinator, snapshot.admission)
        os._exit(CUTOVER_FREEZE_CRASH_EXIT)


@pytest.mark.parametrize("kind", ("create", "duplicate"))
def test_same_target_cross_process_has_exactly_one_winner(tmp_path: Path, kind: str) -> None:
    ctx = _fork_context()
    coordinator = _new_coordinator(tmp_path)
    if kind == "duplicate":
        coordinator.create("source", build=_build("source", "seed"), validate=_validate)

    barrier = ctx.Barrier(2)
    results = ctx.Queue()
    processes = [
        ctx.Process(
            target=_same_target_worker,
            args=(str(coordinator.projects_root), kind, label, barrier, results),
        )
        for label in ("a", "b")
    ]
    for process in processes:
        process.start()
    for process in processes:
        _join(process)

    outcomes = [results.get(timeout=2) for _ in processes]
    assert sorted(outcome[0] for outcome in outcomes) == ["ok", "project_topology_conflict"]
    target = coordinator.path_for("same" if kind == "create" else "copy")
    payload = json.loads((target / "document.json").read_text(encoding="utf-8"))
    winner = next(outcome[1] for outcome in outcomes if outcome[0] == "ok")
    assert payload["value"] == winner
    assert not list(coordinator.journal_root.iterdir())
    assert not list(coordinator.staging_root.iterdir())


def test_prepared_create_journal_fences_target_and_same_opid_evidence(tmp_path: Path) -> None:
    ctx = _fork_context()
    coordinator = _new_coordinator(tmp_path)
    operation_id = "a" * 32
    process = ctx.Process(
        target=_crash_create_prepared,
        args=(str(coordinator.projects_root), operation_id),
    )
    process.start()
    _join(process, CREATE_CRASH_EXIT)

    journal_path = coordinator._journal_path(operation_id)
    stage = coordinator.staging_root / operation_id
    journal_before = journal_path.read_bytes()
    staged_document_before = (stage / "document.json").read_bytes()

    with pytest.raises(topology.ZeError) as captured:
        coordinator.create(
            "same",
            build=_build("same", "retry"),
            validate=_validate,
            operation_id=operation_id,
        )
    assert captured.value.code == "project_busy"
    assert captured.value.context["operation_id"] == operation_id
    assert journal_path.read_bytes() == journal_before
    assert (stage / "document.json").read_bytes() == staged_document_before
    assert not coordinator.path_for("same").exists()

    recovered = topology.ProjectTopologyCoordinator(coordinator.projects_root)
    recovered.initialize(validate=_validate)
    assert not journal_path.exists()
    assert not stage.exists()
    assert recovered.intent_status(
        "same", operation_id, kind="create",
    )["status"] == "not_committed"
    commit = recovered.create(
        "same", build=_build("same", "winner"), validate=_validate,
        operation_id=operation_id,
    )
    assert commit.generation == 1
    assert recovered.intent_status(
        "same", operation_id, kind="create",
    )["status"] == "committed"


def test_prepared_duplicate_journal_fences_target_until_recovery(tmp_path: Path) -> None:
    ctx = _fork_context()
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("source", build=_build("source", "seed"), validate=_validate)
    operation_id = "b" * 32
    process = ctx.Process(
        target=_crash_duplicate_prepared,
        args=(str(coordinator.projects_root), operation_id),
    )
    process.start()
    _join(process, DUPLICATE_CRASH_EXIT)

    journal_path = coordinator._journal_path(operation_id)
    journal_before = journal_path.read_bytes()
    with pytest.raises(topology.ZeError) as captured:
        coordinator.duplicate(
            "source",
            "copy",
            rewrite=_rewrite("copy", "retry"),
            validate=_validate,
            operation_id="c" * 32,
        )
    assert captured.value.code == "project_busy"
    assert captured.value.context["operation_id"] == operation_id
    assert journal_path.read_bytes() == journal_before
    assert not coordinator.path_for("copy").exists()

    recovered = topology.ProjectTopologyCoordinator(coordinator.projects_root)
    recovered.initialize(validate=_validate)
    assert not journal_path.exists()
    assert recovered.intent_status(
        "copy", operation_id, kind="duplicate",
    )["status"] == "not_committed"
    commit = recovered.duplicate(
        "source",
        "copy",
        rewrite=_rewrite("copy", "winner"),
        validate=_validate,
        operation_id=operation_id,
    )
    assert commit.generation == 1


def test_source_parked_failure_is_unknown_and_same_opid_cannot_overwrite_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("project", build=_build("project", "old"), validate=_validate)
    live = coordinator.path_for("project")
    operation_id = "d" * 32
    real_fsync_directory = topology._fsync_directory

    def fail_after_source_parked(path: Path) -> bool:
        candidate = Path(path)
        if (
            candidate == coordinator.projects_root
            and not live.exists()
            and any(coordinator.trash_root.glob("*.backup"))
        ):
            raise OSError("injected directory fsync failure after source park")
        return real_fsync_directory(candidate)

    monkeypatch.setattr(topology, "_fsync_directory", fail_after_source_parked)
    with pytest.raises(topology.ZeError) as captured:
        coordinator.replace(
            "project",
            build=_build("project", "new"),
            validate=_validate,
            operation_id=operation_id,
        )
    assert captured.value.code == "project_topology_durability_unknown"
    assert captured.value.context["operation_id"] == operation_id
    assert not live.exists()
    state = coordinator._read_state("project")
    assert state and state["status"] == "quiescing"
    journal_path = coordinator._journal_path(operation_id)
    stage = coordinator.staging_root / operation_id
    backup = coordinator.trash_root / f"{operation_id}.backup"
    assert journal_path.is_file() and stage.is_dir() and backup.is_dir()

    monkeypatch.setattr(topology, "_fsync_directory", real_fsync_directory)
    evidence_before = (
        journal_path.read_bytes(),
        (stage / "document.json").read_bytes(),
        (backup / "document.json").read_bytes(),
    )
    with pytest.raises(topology.ZeError) as retry:
        coordinator.replace(
            "project",
            build=_build("project", "retry"),
            validate=_validate,
            operation_id=operation_id,
        )
    assert retry.value.code == "project_busy"
    assert retry.value.context["operation_id"] == operation_id
    assert evidence_before == (
        journal_path.read_bytes(),
        (stage / "document.json").read_bytes(),
        (backup / "document.json").read_bytes(),
    )

    recovered = topology.ProjectTopologyCoordinator(coordinator.projects_root)
    recovered.initialize(validate=_validate)
    with recovered.open_live("project") as handle:
        assert handle.generation == 2
    assert json.loads((live / "document.json").read_text(encoding="utf-8"))["value"] == "new"
    assert not journal_path.exists()
    assert not stage.exists()
    assert not backup.exists()
    assert recovered.intent_status(
        "project", operation_id, kind="replace",
    )["status"] == "committed"


def test_verify_existing_failure_restores_old_live_state_without_restart(tmp_path: Path) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("project", build=_build("project", "old"), validate=_validate)
    state_before = coordinator._read_state("project")
    marker_before = coordinator._read_marker(coordinator.path_for("project"))

    def reject_existing(_root: Path) -> None:
        raise topology.ZeError("verification_failed", "injected verification rejection")

    with pytest.raises(topology.ZeError) as captured:
        coordinator.replace(
            "project",
            build=_build("project", "new"),
            validate=_validate,
            verify_existing=reject_existing,
            operation_id="e" * 32,
        )
    assert captured.value.code == "verification_failed"
    assert coordinator._read_state("project") == state_before
    assert coordinator._read_marker(coordinator.path_for("project")) == marker_before
    assert json.loads(
        (coordinator.path_for("project") / "document.json").read_text(encoding="utf-8")
    )["value"] == "old"
    assert not list(coordinator.journal_root.iterdir())
    assert not list(coordinator.staging_root.iterdir())
    assert not list(coordinator.trash_root.iterdir())
    with coordinator.open_live("project") as handle:
        assert handle.generation == 1


def test_delete_holds_one_content_snapshot_from_verification_through_rename(
    tmp_path: Path,
) -> None:
    from document import read_document_metadata

    coordinator = _new_coordinator(tmp_path)
    coordinator.create("source", build=_build("source", "v1"), validate=_validate)
    live = coordinator.path_for("source")
    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    release = ctx.Event()
    holder = ctx.Process(
        target=_hold_document_content_lock,
        args=(str(live / "document.json"), ready, release),
    )
    holder.start()
    assert ready.wait(10)

    verified = []
    outcomes = []

    def verify(root: Path) -> None:
        verified.append(read_document_metadata(root / "document.json").name)

    def remove() -> None:
        try:
            outcomes.append(coordinator.delete("source", verify_existing=verify))
        except BaseException as exc:
            outcomes.append(exc)

    remover = threading.Thread(target=remove)
    remover.start()
    remover.join(0.2)
    blocked_on_content = remover.is_alive()
    still_live_while_blocked = live.exists()
    release.set()
    remover.join(10)
    holder.join(10)
    if holder.is_alive():
        holder.terminate()
        holder.join(5)

    assert blocked_on_content is True
    assert still_live_while_blocked is True
    assert holder.exitcode == 0
    assert not remover.is_alive()
    assert verified == ["source"]
    assert len(outcomes) == 1 and isinstance(outcomes[0], topology.TopologyCommit)
    assert not live.exists()


def test_first_replace_recovers_after_crash_immediately_after_promote(tmp_path: Path) -> None:
    ctx = _fork_context()
    coordinator = _new_coordinator(tmp_path)
    operation_id = "f" * 32
    process = ctx.Process(
        target=_crash_first_replace_after_promote,
        args=(str(coordinator.projects_root), operation_id),
    )
    process.start()
    _join(process, FIRST_REPLACE_CRASH_EXIT)

    live = coordinator.path_for("first")
    assert json.loads((live / "document.json").read_text(encoding="utf-8"))["value"] == "new"
    state = coordinator._read_state("first")
    assert state and state["status"] == "quiescing"
    assert coordinator._journal_path(operation_id).is_file()

    recovered = topology.ProjectTopologyCoordinator(coordinator.projects_root)
    recovered.initialize(validate=_validate)
    with recovered.open_live("first") as handle:
        assert handle.generation == 1
    assert recovered._read_state("first")["status"] == "live"
    assert recovered.intent_status(
        "first", operation_id, kind="replace",
    )["status"] == "committed"
    assert not list(recovered.journal_root.iterdir())
    assert not list(recovered.staging_root.iterdir())
    assert not list(recovered.trash_root.iterdir())


def test_delete_commits_tombstone_before_gc_and_recovery_finishes_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("project", build=_build("project", "old"), validate=_validate)
    with coordinator.open_live("project") as old_handle:
        pass
    operation_id = "1" * 32
    real_remove_tree_durable = topology._remove_tree_durable
    observed: dict[str, Any] = {}

    def fail_first_trash_gc(path: Path) -> None:
        candidate = Path(path)
        if candidate == coordinator.trash_root / operation_id:
            state = coordinator._read_state("project")
            journal = json.loads(
                coordinator._journal_path(operation_id).read_text(encoding="utf-8")
            )
            observed.update({"state": state, "journal": journal})
            assert state and state["status"] == "deleted"
            assert state["operation_id"] == operation_id
            assert journal["phase"] == "committed"
            raise OSError("injected GC failure")
        real_remove_tree_durable(candidate)

    monkeypatch.setattr(topology, "_remove_tree_durable", fail_first_trash_gc)
    commit = coordinator.delete("project", operation_id=operation_id)
    assert commit.cleanup_pending is True
    assert observed["state"]["generation"] == 2
    assert not coordinator.path_for("project").exists()
    assert (coordinator.trash_root / operation_id).is_dir()
    pending_journal = json.loads(
        coordinator._journal_path(operation_id).read_text(encoding="utf-8")
    )
    assert pending_journal["phase"] == "cleanup_pending"
    with pytest.raises(topology.ZeError) as stale:
        with coordinator.activity(old_handle):
            pass
    assert stale.value.code == "project_not_found"

    monkeypatch.setattr(topology, "_remove_tree_durable", real_remove_tree_durable)
    recovered = topology.ProjectTopologyCoordinator(coordinator.projects_root)
    recovered.initialize(validate=_validate)
    assert recovered.list_live_ids() == []
    assert recovered._read_state("project")["status"] == "deleted"
    assert recovered.intent_status(
        "project", operation_id, kind="delete",
    )["status"] == "deleted"
    assert not coordinator._journal_path(operation_id).exists()
    assert not (coordinator.trash_root / operation_id).exists()


def test_generation_fencing_rejects_handle_from_replaced_generation(tmp_path: Path) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("project", build=_build("project", "old"), validate=_validate)
    with coordinator.open_live("project") as old_handle:
        pass

    commit = coordinator.replace(
        "project",
        build=_build("project", "new"),
        validate=_validate,
    )
    assert commit.generation == 2
    with pytest.raises(topology.ZeError) as captured:
        with coordinator.activity(old_handle):
            pass
    assert captured.value.code == "project_generation_stale"
    assert captured.value.context == {
        "project_id": "project",
        "expected_generation": 1,
        "current_generation": 2,
    }
    with coordinator.open_live("project") as current_handle:
        assert current_handle.generation == 2
        with coordinator.activity(current_handle):
            pass


@pytest.mark.parametrize(
    "invalid_generation",
    (True, False, 0, -1, 2**63, 1.0, _GenerationInt(1)),
)
def test_live_scan_source_requires_exact_bounded_generation(
    tmp_path: Path,
    invalid_generation: Any,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("project", build=_build("project", "v1"), validate=_validate)

    with pytest.raises(topology.ZeError) as captured:
        with coordinator.open_live_scan_source(
            "project",
            invalid_generation,
        ):
            pass
    assert captured.value.code == "validation_failed"
    assert captured.value.context == {"reason": "expected_generation_invalid"}


def test_live_scan_source_is_sealed_and_binds_generation_root_and_head(
    tmp_path: Path,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("project", build=_build("project", "v1"), validate=_validate)

    with coordinator.open_live_scan_source("project", 1) as source:
        root_info = coordinator.path_for("project").lstat()
        assert type(source) is topology._LiveScanSource
        assert source.project_id == "project"
        assert source.generation == 1
        assert source.document_path == coordinator.path_for("project") / "document.json"
        assert source.root_identity == (root_info.st_dev, root_info.st_ino)
        assert source.head.metadata.id == "project"
        assert source.head.metadata.version == 1
        assert "_LiveScanSource" not in topology.__all__

        with pytest.raises(TypeError, match="framework-issued authority"):
            topology._LiveScanSource(
                project_id=source.project_id,
                generation=source.generation,
                document_path=source.document_path,
                root_identity=source.root_identity,
                head=source.head,
            )


def test_live_scan_source_rejects_stale_generation(tmp_path: Path) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("project", build=_build("project", "v1"), validate=_validate)
    coordinator.replace(
        "project",
        build=_build("project", "v2"),
        validate=_validate,
        expected_generation=1,
    )

    with pytest.raises(topology.ZeError) as captured:
        with coordinator.open_live_scan_source("project", 1):
            pass
    assert captured.value.code == "project_generation_stale"
    assert captured.value.context == {
        "project_id": "project",
        "expected_generation": 1,
        "current_generation": 2,
    }


def test_live_scan_source_rejects_unsafe_root_without_path_details(
    tmp_path: Path,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("project", build=_build("project", "v1"), validate=_validate)
    root = coordinator.path_for("project")
    original_mode = root.stat().st_mode & 0o777
    root.chmod(original_mode | 0o022)
    try:
        with pytest.raises(topology.ZeError) as captured:
            with coordinator.open_live_scan_source("project", 1):
                pass
    finally:
        root.chmod(original_mode)
    assert captured.value.code == "project_recovery_required"
    assert captured.value.context == {"reason": "live_scan_root_unsafe"}
    assert str(root) not in str(captured.value)


def test_live_scan_source_rechecks_root_permissions_before_release(
    tmp_path: Path,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("project", build=_build("project", "v1"), validate=_validate)
    root = coordinator.path_for("project")
    original_mode = root.stat().st_mode & 0o777
    try:
        with pytest.raises(topology.ZeError) as captured:
            with coordinator.open_live_scan_source("project", 1):
                root.chmod(original_mode | 0o022)
        assert captured.value.code == "project_recovery_required"
        assert captured.value.context == {"reason": "live_scan_root_unsafe"}
    finally:
        root.chmod(original_mode)


def test_live_scan_source_rejects_root_inode_swap_before_yield(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("project", build=_build("project", "kept"), validate=_validate)
    coordinator.create("victim", build=_build("victim", "victim"), validate=_validate)
    live = coordinator.path_for("project")
    victim = coordinator.path_for("victim")
    parked = coordinator.projects_root / "project.pinned-original"
    live_before = (live / "document.json").read_bytes()
    victim_before = (victim / "document.json").read_bytes()
    real_assert_named = topology._assert_live_scan_root_named
    swapped = False

    def swap_before_first_named_check(root: Path, identity: tuple[int, int]) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            os.rename(live, parked)
            os.rename(victim, live)
            try:
                real_assert_named(root, identity)
            finally:
                os.rename(live, victim)
                os.rename(parked, live)
            return
        real_assert_named(root, identity)

    monkeypatch.setattr(
        topology,
        "_assert_live_scan_root_named",
        swap_before_first_named_check,
    )
    with pytest.raises(topology.ZeError) as captured:
        with coordinator.open_live_scan_source("project", 1):
            pytest.fail("inode-swapped source reached scanner")
    assert captured.value.code == "project_recovery_required"
    assert captured.value.context == {
        "reason": "live_scan_root_identity_mismatch",
    }
    assert (live / "document.json").read_bytes() == live_before
    assert (victim / "document.json").read_bytes() == victim_before


@pytest.mark.parametrize("kind", ("replace", "delete"))
def test_live_scan_source_blocks_topology_cutover_until_release(
    tmp_path: Path,
    kind: str,
) -> None:
    ctx = _fork_context()
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("project", build=_build("project", "v1"), validate=_validate)
    start = ctx.Event()
    results = ctx.Queue()
    process = ctx.Process(
        target=_scan_blocked_mutation_worker,
        args=(str(coordinator.projects_root), kind, start, results),
    )
    process.start()

    with coordinator.open_live_scan_source("project", 1) as source:
        assert source.head.metadata.id == "project"
        start.set()
        assert results.get(timeout=5) == "project_busy"
        _join(process)
        with coordinator.open_live("project") as handle:
            assert handle.generation == 1

    if kind == "replace":
        committed = coordinator.replace(
            "project",
            build=_build("project", "replacement"),
            validate=_validate,
            expected_generation=1,
        )
    else:
        committed = coordinator.delete("project", expected_generation=1)
    assert committed.generation == 2


def test_live_scan_source_exception_closes_root_and_releases_all_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("project", build=_build("project", "v1"), validate=_validate)
    real_open_root = topology._open_live_scan_root
    observed: dict[str, int] = {}

    def observe_root(root: Path) -> tuple[int, tuple[int, int]]:
        descriptor, identity = real_open_root(root)
        observed["descriptor"] = descriptor
        return descriptor, identity

    monkeypatch.setattr(topology, "_open_live_scan_root", observe_root)
    with pytest.raises(RuntimeError, match="scanner failed"):
        with coordinator.open_live_scan_source("project", 1):
            raise RuntimeError("scanner failed")

    with pytest.raises(OSError):
        os.fstat(observed["descriptor"])
    committed = coordinator.replace(
        "project",
        build=_build("project", "v2"),
        validate=_validate,
        expected_generation=1,
    )
    assert committed.generation == 2


def test_shared_locks_coexist_and_exclusive_waits_for_all_readers(tmp_path: Path) -> None:
    ctx = _fork_context()
    lock_path = tmp_path / "stable.lock"
    release_reader = ctx.Event()
    reader_results = ctx.Queue()
    reader = ctx.Process(
        target=_lock_worker,
        args=(str(lock_path), False, 2.0, release_reader, reader_results),
    )
    reader.start()
    assert reader_results.get(timeout=3) == "acquired"

    peer_results = ctx.Queue()
    peer = ctx.Process(
        target=_lock_worker,
        args=(str(lock_path), False, 1.0, None, peer_results),
    )
    peer.start()
    assert peer_results.get(timeout=3) == "acquired"
    _join(peer)

    writer_results = ctx.Queue()
    blocked_writer = ctx.Process(
        target=_lock_worker,
        args=(str(lock_path), True, 0.2, None, writer_results),
    )
    blocked_writer.start()
    assert writer_results.get(timeout=3) == "timeout"
    _join(blocked_writer)

    release_reader.set()
    _join(reader)
    final_results = ctx.Queue()
    writer = ctx.Process(
        target=_lock_worker,
        args=(str(lock_path), True, 1.0, None, final_results),
    )
    writer.start()
    assert final_results.get(timeout=3) == "acquired"
    _join(writer)

    with topology._path_lock(lock_path, exclusive=False, timeout=1.0):
        with topology._path_lock(lock_path, exclusive=False, timeout=1.0):
            pass
        with pytest.raises(topology.ZeError) as upgrade:
            with topology._path_lock(lock_path, exclusive=True, timeout=1.0):
                pass
    assert upgrade.value.code == "project_lock_order_invalid"


def test_strict_catalog_migration_supports_empty_deleted_and_canonical_catalogs(
    tmp_path: Path,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    with coordinator.open_strict_catalog_migration() as empty:
        assert empty == ()
        assert isinstance(empty, tuple)

    coordinator.create("zeta", build=_build("zeta", "z"), validate=_validate)
    coordinator.create("alpha", build=_build("alpha", "a"), validate=_validate)
    with coordinator.open_strict_catalog_migration() as handles:
        assert isinstance(handles, tuple)
        assert [handle.project_id for handle in handles] == ["alpha", "zeta"]
        assert [handle.generation for handle in handles] == [1, 1]

    coordinator.delete("alpha", expected_generation=1)
    with coordinator.open_strict_catalog_migration() as remaining:
        assert [handle.project_id for handle in remaining] == ["zeta"]


def test_strict_catalog_accepts_only_the_exact_historical_adoption_sentinel(
    tmp_path: Path,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.create(
        "project",
        build=_build("project", "v1"),
        validate=_validate,
        operation_id="a" * 32,
    )
    root = coordinator.path_for("project")
    # Existing deployments legitimately persisted this pre-journal sentinel.
    # Accept it as history without rewriting marker/state outside a journal.
    coordinator._write_marker(root, "project", 1, "legacy-adopt")
    coordinator._write_state("project", 1, "live", "legacy-adopt")
    with coordinator.open_strict_catalog_migration() as handles:
        assert [(item.project_id, item.generation) for item in handles] == [
            ("project", 1),
        ]


def test_catalog_migration_barrier_drains_and_blocks_cross_process_activity(
    tmp_path: Path,
) -> None:
    ctx = _fork_context()
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("project", build=_build("project", "v1"), validate=_validate)

    # An activity that entered first holds barrier SH even though it no longer
    # owns repo SH.  Migration EX must time out until that activity releases.
    ready = ctx.Event()
    start = ctx.Event()
    release = ctx.Event()
    results = ctx.Queue()
    holder = ctx.Process(
        target=_activity_barrier_worker,
        args=(
            str(coordinator.projects_root), ready, start, release, results, 2.0,
        ),
    )
    holder.start()
    assert ready.wait(5)
    start.set()
    assert results.get(timeout=5) == "acquired"
    impatient = topology.ProjectTopologyCoordinator(
        coordinator.projects_root,
        lock_timeout=0.2,
    )
    with pytest.raises(topology.ZeError) as blocked_migration:
        with impatient.open_strict_catalog_migration():
            pytest.fail("migration crossed an active repository barrier")
    assert blocked_migration.value.code == "project_busy"
    release.set()
    _join(holder)

    # Once migration owns barrier EX, a process that already captured a handle
    # cannot enter activity.  Preparing the handle before the context isolates
    # this assertion from the separate repo EX lock.
    ready = ctx.Event()
    start = ctx.Event()
    results = ctx.Queue()
    blocked = ctx.Process(
        target=_activity_barrier_worker,
        args=(
            str(coordinator.projects_root), ready, start, None, results, 0.2,
        ),
    )
    blocked.start()
    assert ready.wait(5)
    with coordinator.open_strict_catalog_migration() as handles:
        assert [handle.project_id for handle in handles] == ["project"]
        start.set()
        assert results.get(timeout=5) == "project_busy"
        _join(blocked)

    with coordinator.open_live("project") as handle:
        with coordinator.activity(handle):
            pass


def test_nested_open_live_activity_and_strict_migration_keep_global_lock_order(
    tmp_path: Path,
) -> None:
    ctx = _fork_context()
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("project", build=_build("project", "v1"), validate=_validate)

    # This is the production nesting used by Repository.get_or_load:
    # open_live owns barrier SH -> repo SH -> project SH, then activity only
    # re-enters barrier/project.  A strict writer must wait at barrier EX,
    # never own barrier EX while waiting for this reader's repo SH.
    ready = ctx.Event()
    start = ctx.Event()
    release = ctx.Event()
    results = ctx.Queue()
    reader = ctx.Process(
        target=_nested_live_activity_worker,
        args=(
            str(coordinator.projects_root), ready, start, release, results, 2.0,
        ),
    )
    reader.start()
    start.set()
    assert ready.wait(5)
    assert results.get(timeout=5) == "acquired"
    impatient = topology.ProjectTopologyCoordinator(
        coordinator.projects_root,
        lock_timeout=0.2,
    )
    with pytest.raises(topology.ZeError) as blocked:
        with impatient.open_strict_catalog_migration():
            pytest.fail("strict migration crossed nested live activity")
    assert blocked.value.code == "project_busy"
    release.set()
    _join(reader)
    with coordinator.open_strict_catalog_migration() as handles:
        assert [handle.project_id for handle in handles] == ["project"]

    # The reverse order also terminates by timeout: strict owns barrier EX, so
    # open_live cannot first grab repo SH and form the old inversion.
    ready = ctx.Event()
    start = ctx.Event()
    results = ctx.Queue()
    blocked_reader = ctx.Process(
        target=_nested_live_activity_worker,
        args=(
            str(coordinator.projects_root), ready, start, None, results, 0.2,
        ),
    )
    blocked_reader.start()
    with coordinator.open_strict_catalog_migration():
        start.set()
        assert ready.wait(5)
        assert results.get(timeout=5) == "project_busy"
        _join(blocked_reader)


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    (
        ("malformed_state", None),
        ("state_extra_member", "catalog_state_shape_invalid"),
        ("state_wrong_path", "catalog_state_path_mismatch"),
        ("state_quiescing", "catalog_state_not_terminal"),
        ("state_recovery", "catalog_state_not_terminal"),
        ("operation_noncanonical", "catalog_state_operation_invalid"),
        ("legacy_sentinel_generation", "catalog_state_operation_invalid"),
        ("legacy_sentinel_status", "catalog_state_operation_invalid"),
        ("pending_journal", "catalog_journal_pending"),
        ("staging_entry", "catalog_staging_not_empty"),
        ("trash_entry", "catalog_trash_not_empty"),
        ("orphan_root", "catalog_orphan_root"),
        ("missing_live_root", "catalog_live_root_missing"),
        ("marker_generation", "catalog_marker_mismatch"),
        ("marker_operation", "catalog_marker_mismatch"),
        ("document_identity", None),
    ),
)
def test_strict_catalog_migration_rejects_every_unclosed_topology_state(
    tmp_path: Path,
    case: str,
    expected_reason: str | None,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    commit = coordinator.create(
        "project",
        build=_build("project", "v1"),
        validate=_validate,
        operation_id="a" * 32,
    )
    assert commit.generation == 1
    state_path = coordinator._state_path("project")
    root = coordinator.path_for("project")

    if case == "malformed_state":
        state_path.write_text("{", encoding="utf-8")
    elif case == "state_extra_member":
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["unexpected"] = True
        state_path.write_text(json.dumps(state), encoding="utf-8")
    elif case == "state_wrong_path":
        (coordinator.state_root / "wrong.json").write_bytes(state_path.read_bytes())
    elif case == "state_quiescing":
        coordinator._write_state("project", 1, "quiescing", "a" * 32)
    elif case == "state_recovery":
        coordinator._write_state("project", 1, "recovery_required", "a" * 32)
    elif case == "operation_noncanonical":
        coordinator._write_state("project", 1, "live", "same")
        marker_path = root / topology.PROJECT_MARKER
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["operation_id"] = "same"
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
    elif case == "legacy_sentinel_generation":
        coordinator._write_state("project", 2, "live", "legacy-adopt")
        coordinator._write_marker(root, "project", 2, "legacy-adopt")
    elif case == "legacy_sentinel_status":
        coordinator._write_state("project", 1, "deleted", "legacy-adopt")
    elif case == "pending_journal":
        (coordinator.journal_root / ("b" * 32 + ".json")).write_text(
            "{}", encoding="utf-8",
        )
    elif case == "staging_entry":
        (coordinator.staging_root / "orphan").mkdir()
    elif case == "trash_entry":
        (coordinator.trash_root / "orphan").mkdir()
    elif case == "orphan_root":
        _write_project(coordinator.projects_root / "orphan", "orphan", "v1")
    elif case == "missing_live_root":
        os.rename(root, tmp_path / "parked-project")
    elif case in ("marker_generation", "marker_operation"):
        marker_path = root / topology.PROJECT_MARKER
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if case == "marker_generation":
            marker["generation"] = 2
        else:
            marker["operation_id"] = "b" * 32
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
    elif case == "document_identity":
        document_path = root / "document.json"
        document = json.loads(document_path.read_text(encoding="utf-8"))
        document["id"] = "other"
        document_path.write_text(json.dumps(document), encoding="utf-8")
    else:  # pragma: no cover - parameter table is closed above
        raise AssertionError(case)

    with pytest.raises(topology.ZeError) as captured:
        with coordinator.open_strict_catalog_migration():
            pytest.fail("unclosed catalog reached the migration caller")
    assert captured.value.code == "project_recovery_required"
    if expected_reason is not None:
        assert captured.value.context["reason"] == expected_reason


def test_fork_child_drops_inherited_lock_registry_without_unlocking_parent(
    tmp_path: Path,
) -> None:
    ctx = _fork_context()
    lock_path = tmp_path / "fork-held.lock"
    results = ctx.Queue()

    with topology._path_lock(lock_path, exclusive=False, timeout=1.0):
        inherited = getattr(topology._HELD_LOCKS, "entries", {})
        assert str(lock_path.absolute()) in inherited
        process = ctx.Process(
            target=_fork_inherited_lock_probe,
            args=(str(lock_path), results),
        )
        process.start()
        assert results.get(timeout=5) == ("registry", 0, 0)
        # A fresh EX in the child still times out: closing the child's copied
        # descriptor did not issue LOCK_UN against the parent's open file.
        assert results.get(timeout=5) == "timeout"
        _join(process)

    with topology._path_lock(lock_path, exclusive=True, timeout=1.0):
        pass


def test_repository_admission_is_exact_owner_safe_and_missing_fails_closed(
    tmp_path: Path,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    path = coordinator.repository_admission_path
    value = json.loads(path.read_text(encoding="utf-8"))
    assert set(value) == {
        "schema", "authority_state", "authority_epoch", "revision", "cutover_id",
    }
    assert value["schema"] == topology.REPOSITORY_ADMISSION_SCHEMA
    assert value["authority_state"] == "legacy"
    assert len(value["authority_epoch"]) == 64
    assert value["revision"] == 1
    assert value["cutover_id"] == ""
    info = path.lstat()
    assert info.st_nlink == 1
    assert info.st_mode & 0o077 == 0

    path.unlink()
    with pytest.raises(topology.ZeError) as missing:
        coordinator.list_live_ids()
    assert missing.value.code == "project_recovery_required"
    assert missing.value.context["reason"] == "admission_missing"
    with pytest.raises(topology.ZeError) as reopened:
        topology.ProjectTopologyCoordinator(coordinator.projects_root)
    assert reopened.value.context["reason"] == "admission_missing"


def test_private_bootstrap_installs_only_a_closed_pre_v5_legacy_repository(
    tmp_path: Path,
) -> None:
    original = _new_coordinator(tmp_path)
    original.create(
        "project", build=_build("project", "v4"), validate=_validate,
        operation_id="a" * 32,
    )
    # Simulate a valid v4 repository: topology catalog exists, but neither v5
    # admission artifact has ever been installed.
    original.repository_admission_path.unlink()
    original._repository_admission_install_path.unlink()

    with pytest.raises(topology.ZeError) as ordinary:
        topology.ProjectTopologyCoordinator(original.projects_root)
    assert ordinary.value.context["reason"] == "admission_missing"
    with pytest.raises(topology.ZeError) as forbidden:
        topology.ProjectTopologyCoordinator._bootstrap_existing_legacy_repository_admission(
            original.projects_root,
            validate=_validate,
        )
    assert forbidden.value.code == "project_topology_forbidden"

    upgraded = topology.ProjectTopologyCoordinator._bootstrap_existing_legacy_repository_admission(
        original.projects_root,
        validate=_validate,
        _capability=topology._REPOSITORY_ADMISSION_BOOTSTRAP_CAPABILITY,
    )
    admission = upgraded._read_repository_admission()
    assert admission is not None
    assert admission.authority_state == "legacy"
    assert admission.revision == 1
    install_marker = upgraded._read_repository_admission_install_marker()
    assert install_marker is not None
    assert install_marker.phase == "committed"
    assert install_marker.authority_epoch == admission.authority_epoch
    assert upgraded.list_live_ids() == ["project"]


def test_private_bootstrap_concurrent_workers_converge_to_one_exact_install(
    tmp_path: Path,
) -> None:
    ctx = _fork_context()
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("project", build=_build("project", "v4"), validate=_validate)
    coordinator.repository_admission_path.unlink()
    coordinator._repository_admission_install_path.unlink()

    start = ctx.Event()
    results = ctx.Queue()
    workers = [
        ctx.Process(
            target=_bootstrap_repository_admission_worker,
            args=(str(coordinator.projects_root), start, results),
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    start.set()
    outcomes = [results.get(timeout=10) for _ in workers]
    for worker in workers:
        _join(worker)
    assert [outcome[0] for outcome in outcomes] == ["ok", "ok"]
    assert outcomes[0][1:] == outcomes[1][1:]
    assert outcomes[0][2] == 1

    reopened = topology.ProjectTopologyCoordinator(coordinator.projects_root)
    admission = reopened._read_repository_admission()
    marker = reopened._read_repository_admission_install_marker()
    assert admission is not None and admission.authority_epoch == outcomes[0][1]
    assert marker is not None and marker.phase == "committed"
    assert marker.install_mode == "legacy_bootstrap"


def test_private_bootstrap_never_overwrites_installed_pending_or_malformed_state(
    tmp_path: Path,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    with coordinator._open_strict_repository_migration(
        _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
    ) as snapshot:
        _install_cutover_freeze(coordinator, snapshot.admission)
        pending = coordinator._compare_and_swap_repository_admission(
            snapshot.admission,
            target_state="cutover_pending",
            cutover_id="8" * 64,
            _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
        )
    before = coordinator.repository_admission_path.read_bytes()
    with pytest.raises(topology.ZeError) as already_installed:
        topology.ProjectTopologyCoordinator._bootstrap_existing_legacy_repository_admission(
            coordinator.projects_root,
            validate=_validate,
            _capability=topology._REPOSITORY_ADMISSION_BOOTSTRAP_CAPABILITY,
        )
    assert already_installed.value.code == "project_topology_conflict"
    assert coordinator.repository_admission_path.read_bytes() == before
    assert coordinator._read_repository_admission() == pending

    # Deleting the record does not make an installed repository look pre-v5:
    # the durable install sentinel permanently closes bootstrap resurrection.
    coordinator.repository_admission_path.unlink()
    with pytest.raises(topology.ZeError) as removed:
        topology.ProjectTopologyCoordinator._bootstrap_existing_legacy_repository_admission(
            coordinator.projects_root,
            validate=_validate,
            _capability=topology._REPOSITORY_ADMISSION_BOOTSTRAP_CAPABILITY,
        )
    assert removed.value.context["reason"] == "admission_missing"

    # A malformed installed record is likewise diagnosis-only, never replaced.
    other = _new_coordinator(tmp_path / "other")
    other.repository_admission_path.write_text("{", encoding="utf-8")
    malformed_before = other.repository_admission_path.read_bytes()
    with pytest.raises(topology.ZeError) as malformed:
        topology.ProjectTopologyCoordinator._bootstrap_existing_legacy_repository_admission(
            other.projects_root,
            validate=_validate,
            _capability=topology._REPOSITORY_ADMISSION_BOOTSTRAP_CAPABILITY,
        )
    assert malformed.value.context["reason"] == "admission_json_invalid"
    assert other.repository_admission_path.read_bytes() == malformed_before


def test_private_bootstrap_recovers_pre_v5_pending_journal_before_install(
    tmp_path: Path,
) -> None:
    ctx = _fork_context()
    coordinator = _new_coordinator(tmp_path)
    operation_id = "7" * 32
    crashed = ctx.Process(
        target=_crash_create_prepared,
        args=(str(coordinator.projects_root), operation_id),
    )
    crashed.start()
    _join(crashed, expected_exit=CREATE_CRASH_EXIT)
    assert list(coordinator.journal_root.iterdir())
    assert list(coordinator.staging_root.iterdir())

    # Strip only v5 artifacts to model the exact recoverable v4 rollout input.
    coordinator.repository_admission_path.unlink()
    coordinator._repository_admission_install_path.unlink()
    upgraded = topology.ProjectTopologyCoordinator._bootstrap_existing_legacy_repository_admission(
        coordinator.projects_root,
        validate=_validate,
        _capability=topology._REPOSITORY_ADMISSION_BOOTSTRAP_CAPABILITY,
    )
    assert not list(upgraded.journal_root.iterdir())
    assert not list(upgraded.staging_root.iterdir())
    assert not list(upgraded.trash_root.iterdir())
    marker = upgraded._read_repository_admission_install_marker()
    assert marker is not None and marker.phase == "committed"
    assert marker.install_mode == "legacy_bootstrap"


def test_private_bootstrap_keeps_unrecoverable_v4_state_prepared_not_committed(
    tmp_path: Path,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.repository_admission_path.unlink()
    coordinator._repository_admission_install_path.unlink()
    bad_journal = coordinator.journal_root / ("6" * 32 + ".json")
    bad_journal.write_text("{}", encoding="utf-8")

    with pytest.raises(topology.ZeError) as failed:
        topology.ProjectTopologyCoordinator._bootstrap_existing_legacy_repository_admission(
            coordinator.projects_root,
            validate=_validate,
            _capability=topology._REPOSITORY_ADMISSION_BOOTSTRAP_CAPABILITY,
        )
    assert failed.value.code == "project_recovery_required"
    marker = coordinator._read_repository_admission_install_marker()
    admission = coordinator._read_repository_admission()
    assert marker is not None and marker.phase == "prepared"
    assert marker.install_mode == "legacy_bootstrap"
    assert admission is not None and admission.authority_state == "legacy"
    with pytest.raises(topology.ZeError) as ordinary:
        topology.ProjectTopologyCoordinator(coordinator.projects_root)
    assert ordinary.value.context["reason"] == (
        "admission_legacy_bootstrap_resume_required"
    )


@pytest.mark.parametrize("control_name", ("staging", "trash"))
def test_private_bootstrap_never_traverses_unsafe_namespace_control(
    tmp_path: Path,
    control_name: str,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.repository_admission_path.unlink()
    coordinator._repository_admission_install_path.unlink()
    control = (
        coordinator.staging_root
        if control_name == "staging"
        else coordinator.trash_root
    )
    control.rmdir()
    outside = tmp_path / f"outside-{control_name}"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("must-survive", encoding="utf-8")
    nested = outside / "nested"
    nested.mkdir()
    (nested / "payload.txt").write_text("untouched", encoding="utf-8")
    try:
        control.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(topology.ZeError) as unsafe:
        topology.ProjectTopologyCoordinator._bootstrap_existing_legacy_repository_admission(
            coordinator.projects_root,
            validate=_validate,
            _capability=topology._REPOSITORY_ADMISSION_BOOTSTRAP_CAPABILITY,
        )
    assert unsafe.value.code == "project_recovery_required"
    assert unsafe.value.context["reason"] == "admission_namespace_control_unsafe"
    assert sentinel.read_text(encoding="utf-8") == "must-survive"
    assert (nested / "payload.txt").read_text(encoding="utf-8") == "untouched"
    assert not coordinator.repository_admission_path.exists()
    assert not coordinator._repository_admission_install_path.exists()


def test_repository_admission_revision_exhaustion_never_writes_overflow(
    tmp_path: Path,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    initial = coordinator._read_repository_admission()
    assert initial is not None
    maximum = topology._RepositoryAdmission(
        authority_state="legacy",
        authority_epoch=initial.authority_epoch,
        revision=2**63 - 1,
        cutover_id="",
    )
    coordinator._write_repository_admission(maximum)
    before = coordinator.repository_admission_path.read_bytes()
    with pytest.raises(topology.ZeError) as exhausted:
        coordinator._compare_and_swap_repository_admission(
            maximum,
            target_state="cutover_pending",
            cutover_id="5" * 64,
            _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
        )
    assert exhausted.value.code == "project_topology_conflict"
    assert coordinator.repository_admission_path.read_bytes() == before
    assert coordinator._read_repository_admission() == maximum


@pytest.mark.parametrize(
    ("edge", "exit_code"),
    (
        ("before_prepared", ADMISSION_BEFORE_PREPARED_CRASH_EXIT),
        ("prepared", ADMISSION_PREPARED_CRASH_EXIT),
        ("record", ADMISSION_RECORD_CRASH_EXIT),
        ("committed", ADMISSION_COMMITTED_CRASH_EXIT),
    ),
)
def test_repository_admission_install_recovers_every_process_crash_edge(
    tmp_path: Path,
    edge: str,
    exit_code: int,
) -> None:
    ctx = _fork_context()
    projects_root = tmp_path / edge / "projects"
    process = ctx.Process(
        target=_crash_repository_admission_install,
        args=(str(projects_root), edge),
    )
    process.start()
    _join(process, expected_exit=exit_code)

    if edge == "before_prepared":
        with pytest.raises(topology.ZeError) as ordinary:
            topology.ProjectTopologyCoordinator(projects_root, lock_timeout=2.0)
        assert ordinary.value.context["reason"] == "admission_missing"
        recovered = (
            topology.ProjectTopologyCoordinator
            ._bootstrap_existing_legacy_repository_admission(
                projects_root,
                validate=_validate,
                lock_timeout=2.0,
                _capability=topology._REPOSITORY_ADMISSION_BOOTSTRAP_CAPABILITY,
            )
        )
    else:
        recovered = topology.ProjectTopologyCoordinator(
            projects_root,
            lock_timeout=2.0,
        )
    admission = recovered._read_repository_admission()
    marker = recovered._read_repository_admission_install_marker()
    assert admission is not None
    assert admission.authority_state == "legacy"
    assert admission.revision == 1
    assert marker is not None
    assert marker.phase == "committed"
    assert marker.install_mode == (
        "legacy_bootstrap" if edge == "before_prepared" else "fresh"
    )
    assert marker.authority_epoch == admission.authority_epoch
    assert marker.initial_record_sha256 == topology._repository_admission_digest(
        admission,
    )


@pytest.mark.parametrize("edge", ("prepared", "record", "committed"))
def test_repository_admission_install_recovers_every_ack_loss_edge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    edge: str,
) -> None:
    projects_root = tmp_path / edge / "projects"
    real_atomic_json = topology._atomic_json
    injected = False

    def lose_return_edge(
        path: Path,
        value: dict[str, Any],
        *,
        mode: int = 0o600,
    ) -> bool:
        nonlocal injected
        result = real_atomic_json(path, value, mode=mode)
        phase = value.get("phase")
        selected = (
            (edge == "prepared" and phase == "prepared")
            or (
                edge == "record"
                and value.get("schema") == topology.REPOSITORY_ADMISSION_SCHEMA
            )
            or (edge == "committed" and phase == "committed")
        )
        if selected and not injected:
            injected = True
            raise OSError("injected admission install acknowledgement loss")
        return result

    monkeypatch.setattr(topology, "_atomic_json", lose_return_edge)
    with pytest.raises(OSError):
        topology.ProjectTopologyCoordinator(projects_root, lock_timeout=2.0)
    monkeypatch.setattr(topology, "_atomic_json", real_atomic_json)

    recovered = topology.ProjectTopologyCoordinator(projects_root, lock_timeout=2.0)
    admission = recovered._read_repository_admission()
    marker = recovered._read_repository_admission_install_marker()
    assert admission is not None
    assert marker is not None and marker.phase == "committed"
    assert marker.authority_epoch == admission.authority_epoch


@pytest.mark.parametrize(
    ("case", "reason"),
    (
        ("malformed", "admission_json_invalid"),
        ("duplicate", "admission_json_invalid"),
        ("extra", "admission_shape_invalid"),
        ("wrong_type", "admission_value_invalid"),
        ("unsafe_mode", "admission_inode_unsafe"),
        ("symlink", "admission_path_unsafe"),
        ("hardlink", "admission_inode_unsafe"),
    ),
)
def test_repository_admission_rejects_malformed_and_unsafe_carriers(
    tmp_path: Path,
    case: str,
    reason: str,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    path = coordinator.repository_admission_path
    raw = json.loads(path.read_text(encoding="utf-8"))
    if case == "malformed":
        path.write_text("{", encoding="utf-8")
    elif case == "duplicate":
        path.write_text(
            json.dumps(raw)[:-1] + ',"revision":1}',
            encoding="utf-8",
        )
    elif case == "extra":
        raw["extra"] = True
        path.write_text(json.dumps(raw), encoding="utf-8")
    elif case == "wrong_type":
        raw["revision"] = True
        path.write_text(json.dumps(raw), encoding="utf-8")
    elif case == "unsafe_mode":
        path.chmod(0o666)
    elif case == "symlink":
        outside = tmp_path / "outside-admission.json"
        outside.write_text(json.dumps(raw), encoding="utf-8")
        path.unlink()
        try:
            path.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are unavailable on this platform")
    elif case == "hardlink":
        outside = tmp_path / "outside-admission.json"
        outside.write_text(json.dumps(raw), encoding="utf-8")
        outside.chmod(0o600)
        path.unlink()
        try:
            os.link(outside, path)
        except (OSError, NotImplementedError):
            pytest.skip("hard links are unavailable on this platform")
    else:  # pragma: no cover - parameter table is closed above
        raise AssertionError(case)

    with pytest.raises(topology.ZeError) as captured:
        coordinator.list_live_ids()
    assert captured.value.code == "project_recovery_required"
    assert captured.value.context["reason"] == reason


def test_cutover_freeze_is_canonical_exact_retry_and_blocks_legacy_leases(
    tmp_path: Path,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("project", build=_build("project", "v1"), validate=_validate)
    initial = coordinator._read_repository_admission()
    assert initial is not None
    with pytest.raises(topology.ZeError) as escaped:
        _install_cutover_freeze(coordinator, initial)
    assert escaped.value.code == "project_topology_forbidden"
    with coordinator.open_live("project") as handle:
        captured_handle = handle
    with coordinator._open_strict_repository_migration(
        _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
    ) as snapshot:
        marker = _install_cutover_freeze(coordinator, snapshot.admission)
        assert _install_cutover_freeze(coordinator, snapshot.admission) == marker
        assert coordinator._revalidate_strict_repository_snapshot(
            snapshot,
            _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
        ) == snapshot
        with coordinator.open_live_scan_source("project", 1) as source:
            assert source.project_id == "project"
            assert source.generation == 1
        with pytest.raises(topology.ZeError) as different_identity:
            coordinator._install_repository_cutover_freeze(
                snapshot.admission,
                runtime_generation="other-runtime",
                code_digest=hashlib.sha256(b"other-code").digest(),
                _capability=topology._REPOSITORY_CUTOVER_FREEZE_CAPABILITY,
            )
        assert different_identity.value.code == "project_topology_conflict"

    path = coordinator._repository_cutover_freeze_path
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert set(raw) == {
        "schema", "authority_epoch", "source_admission_revision",
        "runtime_generation", "code_digest", "freeze_sha256",
    }
    assert raw["schema"] == topology._REPOSITORY_CUTOVER_FREEZE_SCHEMA
    assert raw["authority_epoch"] == marker.authority_epoch
    assert raw["source_admission_revision"] == marker.source_admission_revision
    assert raw["runtime_generation"] == marker.runtime_generation
    assert raw["code_digest"] == marker.code_digest.hex()
    assert raw["freeze_sha256"] == marker.freeze_sha256
    info = path.lstat()
    assert info.st_nlink == 1
    assert info.st_mode & 0o077 == 0

    callback_called = False

    def forbidden_callback(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal callback_called
        callback_called = True
        raise AssertionError("legacy callback crossed durable source freeze")

    def enter(context: Any) -> None:
        with context:
            pass

    blocked_copy_target = tmp_path / "freeze-blocked-copy"
    entries = (
        lambda: coordinator.list_live_ids(),
        lambda: coordinator.snapshot_live(forbidden_callback),
        lambda: coordinator.initialize(validate=forbidden_callback),
        lambda: enter(coordinator.legacy_migration_guard()),
        lambda: enter(coordinator.open_live("project")),
        lambda: enter(coordinator.open_live_scan_source("project", 1)),
        lambda: enter(coordinator.activity(captured_handle)),
        lambda: coordinator.intent_status("project", "1" * 32),
        lambda: coordinator.create(
            "new", build=forbidden_callback, validate=forbidden_callback,
        ),
        lambda: coordinator.duplicate(
            "project", "copy", rewrite=forbidden_callback,
            validate=forbidden_callback,
        ),
        lambda: coordinator.replace(
            "project", build=forbidden_callback, validate=forbidden_callback,
        ),
        lambda: coordinator.delete(
            "project", verify_existing=forbidden_callback,
        ),
        lambda: coordinator.copy_durable_source(
            coordinator.path_for("project"),
            blocked_copy_target,
        ),
        lambda: coordinator.read_durable_source_file(
            coordinator.path_for("project"),
            "document.json",
        ),
        lambda: coordinator.present_durable_source_paths(
            coordinator.path_for("project"),
        ),
        lambda: enter(coordinator.open_strict_catalog_migration()),
    )
    for invoke in entries:
        with pytest.raises(topology.ZeError) as captured:
            invoke()
        assert captured.value.code == "project_authority_moved"
        assert captured.value.context["authority_state"] == "legacy_frozen"
    assert callback_called is False
    assert not blocked_copy_target.exists()

    # The private cutover recovery seam deliberately bypasses the ordinary
    # lease gate while retaining the same EX -> EX source snapshot.
    with coordinator._open_strict_repository_migration(
        _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
    ) as recovered:
        assert recovered.admission == snapshot.admission
        assert [value.project_id for value in recovered.states] == ["project"]
        assert coordinator._read_repository_cutover_freeze() == marker


def test_cutover_freeze_ack_loss_requires_exact_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    real_atomic_json = topology._atomic_json
    injected = False

    def lose_ack(path: Path, value: dict[str, Any], *, mode: int = 0o600) -> bool:
        nonlocal injected
        result = real_atomic_json(path, value, mode=mode)
        if path == coordinator._repository_cutover_freeze_path and not injected:
            injected = True
            raise OSError("injected cutover-freeze acknowledgement loss")
        return result

    with coordinator._open_strict_repository_migration(
        _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
    ) as snapshot:
        monkeypatch.setattr(topology, "_atomic_json", lose_ack)
        with pytest.raises(topology.ZeError) as unknown:
            _install_cutover_freeze(coordinator, snapshot.admission)
        assert unknown.value.code == "project_topology_durability_unknown"
        monkeypatch.setattr(topology, "_atomic_json", real_atomic_json)
        marker = _install_cutover_freeze(coordinator, snapshot.admission)
    assert coordinator._read_repository_cutover_freeze() == marker


@pytest.mark.parametrize(
    ("case", "reason"),
    (
        ("malformed", "cutover_freeze_json_invalid"),
        ("duplicate", "cutover_freeze_json_invalid"),
        ("extra", "cutover_freeze_shape_invalid"),
        ("wrong_digest", "cutover_freeze_binding_invalid"),
        ("unsafe_mode", "cutover_freeze_inode_unsafe"),
        ("symlink", "cutover_freeze_path_unsafe"),
        ("hardlink", "cutover_freeze_inode_unsafe"),
    ),
)
def test_cutover_freeze_rejects_malformed_and_unsafe_carriers(
    tmp_path: Path,
    case: str,
    reason: str,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    with coordinator._open_strict_repository_migration(
        _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
    ) as snapshot:
        _install_cutover_freeze(coordinator, snapshot.admission)
    path = coordinator._repository_cutover_freeze_path
    raw = json.loads(path.read_text(encoding="utf-8"))
    if case == "malformed":
        path.write_text("{", encoding="utf-8")
    elif case == "duplicate":
        path.write_text(
            json.dumps(raw)[:-1] + ',"runtime_generation":"duplicate"}',
            encoding="utf-8",
        )
    elif case == "extra":
        raw["extra"] = True
        path.write_text(json.dumps(raw), encoding="utf-8")
    elif case == "wrong_digest":
        raw["freeze_sha256"] = "0" * 64
        path.write_text(json.dumps(raw), encoding="utf-8")
    elif case == "unsafe_mode":
        path.chmod(0o666)
    elif case == "symlink":
        outside = tmp_path / "outside-freeze.json"
        outside.write_text(json.dumps(raw), encoding="utf-8")
        path.unlink()
        try:
            path.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are unavailable on this platform")
    elif case == "hardlink":
        outside = tmp_path / "outside-freeze.json"
        outside.write_text(json.dumps(raw), encoding="utf-8")
        outside.chmod(0o600)
        path.unlink()
        try:
            os.link(outside, path)
        except (OSError, NotImplementedError):
            pytest.skip("hard links are unavailable on this platform")
    else:  # pragma: no cover - parameter table is closed above
        raise AssertionError(case)

    with pytest.raises(topology.ZeError) as captured:
        coordinator.list_live_ids()
    assert captured.value.code == "project_recovery_required"
    assert captured.value.context["reason"] == reason


def test_cutover_freeze_survives_process_death_and_strict_resume(
    tmp_path: Path,
) -> None:
    ctx = _fork_context()
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("project", build=_build("project", "v1"), validate=_validate)
    process = ctx.Process(
        target=_crash_after_repository_cutover_freeze,
        args=(str(coordinator.projects_root),),
    )
    process.start()
    _join(process, expected_exit=CUTOVER_FREEZE_CRASH_EXIT)

    reopened = topology.ProjectTopologyCoordinator(
        coordinator.projects_root,
        lock_timeout=2.0,
    )
    with pytest.raises(topology.ZeError) as ordinary:
        reopened.list_live_ids()
    assert ordinary.value.code == "project_authority_moved"
    assert ordinary.value.context["authority_state"] == "legacy_frozen"
    with reopened._open_strict_repository_migration(
        _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
    ) as snapshot:
        marker = _install_cutover_freeze(reopened, snapshot.admission)
        assert marker.source_admission_revision == snapshot.admission.revision
        assert [value.project_id for value in snapshot.states] == ["project"]
        with reopened.open_live_scan_source("project", 1) as source:
            assert source.project_id == "project"


def test_repository_admission_cas_requires_durable_cutover_freeze(
    tmp_path: Path,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    with coordinator._open_strict_repository_migration(
        _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
    ) as snapshot:
        initial = snapshot.admission
    before = coordinator.repository_admission_path.read_bytes()
    with pytest.raises(topology.ZeError) as missing:
        coordinator._compare_and_swap_repository_admission(
            initial,
            target_state="cutover_pending",
            cutover_id="e" * 64,
            _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
        )
    assert missing.value.code == "project_recovery_required"
    assert missing.value.context["reason"] == "cutover_freeze_missing"
    assert coordinator.repository_admission_path.read_bytes() == before
    assert coordinator._read_repository_admission() == initial


@pytest.mark.parametrize("edge", ("pending", "sqlite"))
def test_repository_admission_cas_rejects_canonical_freeze_mismatch(
    tmp_path: Path,
    edge: str,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    with coordinator._open_strict_repository_migration(
        _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
    ) as snapshot:
        initial = snapshot.admission
        marker = _install_cutover_freeze(coordinator, initial)
        current = initial
        if edge == "sqlite":
            current = coordinator._compare_and_swap_repository_admission(
                initial,
                target_state="cutover_pending",
                cutover_id="a" * 64,
                _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
            )
        wrong_source_revision = marker.source_admission_revision + 1
        wrong = topology._RepositoryCutoverFreeze(
            authority_epoch=marker.authority_epoch,
            source_admission_revision=wrong_source_revision,
            runtime_generation=marker.runtime_generation,
            code_digest=marker.code_digest,
            freeze_sha256=topology._repository_cutover_freeze_digest(
                authority_epoch=marker.authority_epoch,
                source_admission_revision=wrong_source_revision,
                runtime_generation=marker.runtime_generation,
                code_digest=marker.code_digest,
            ),
        )
        topology._atomic_json(
            coordinator._repository_cutover_freeze_path,
            topology._cutover_freeze_value(wrong),
            mode=0o600,
        )
        with pytest.raises(topology.ZeError) as mismatched:
            coordinator._compare_and_swap_repository_admission(
                current,
                target_state=(
                    "cutover_pending" if edge == "pending" else "sqlite"
                ),
                cutover_id="a" * 64,
                _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
            )
        assert mismatched.value.code == "project_recovery_required"
        assert mismatched.value.context["reason"] == (
            "cutover_freeze_admission_mismatch"
        )
    assert coordinator._read_repository_admission() == current


def test_strict_repository_snapshot_is_path_free_complete_and_content_bound(
    tmp_path: Path,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    with coordinator._open_strict_repository_migration(
        _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
    ) as empty:
        assert empty.states == ()
        assert empty.admission.authority_state == "legacy"

    coordinator.create(
        "zeta", build=_build("zeta", "live"), validate=_validate,
        operation_id="a" * 32,
    )
    coordinator.create(
        "alpha", build=_build("alpha", "deleted"), validate=_validate,
        operation_id="b" * 32,
    )
    coordinator.delete(
        "alpha", expected_generation=1, operation_id="c" * 32,
    )
    with coordinator._open_strict_repository_migration(
        _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
    ) as snapshot:
        assert [item.project_id for item in snapshot.states] == ["alpha", "zeta"]
        deleted, live = snapshot.states
        assert (
            deleted.generation,
            deleted.status,
            deleted.operation_id,
            deleted.source_bytes,
            deleted.source_sha256,
        ) == (2, "deleted", "c" * 32, b"", __import__("hashlib").sha256(b"").digest())
        expected_source = (coordinator.path_for("zeta") / "document.json").read_bytes()
        assert live.status == "live"
        assert live.source_bytes == expected_source
        assert live.source_sha256 == __import__("hashlib").sha256(expected_source).digest()
        assert len(live.source_sha256) == 32
        assert not any(
            isinstance(value, (Path, dict, list))
            for descriptor in snapshot.states
            for value in (
                descriptor.project_id,
                descriptor.generation,
                descriptor.status,
                descriptor.operation_id,
                descriptor.source_bytes,
                descriptor.source_sha256,
            )
        )
    with coordinator.open_strict_catalog_migration() as legacy_compatible:
        assert [item.project_id for item in legacy_compatible] == ["zeta"]


def test_strict_repository_revalidate_requires_live_outer_ex_lease(
    tmp_path: Path,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.create(
        "project", build=_build("project", "v1"), validate=_validate,
    )
    with coordinator._open_strict_repository_migration(
        _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
    ) as snapshot:
        assert coordinator._revalidate_strict_repository_snapshot(
            snapshot,
            _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
        ) == snapshot

    with pytest.raises(topology.ZeError) as escaped:
        coordinator._revalidate_strict_repository_snapshot(
            snapshot,
            _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
        )
    assert escaped.value.code == "project_topology_forbidden"

    with coordinator._open_strict_repository_migration(
        _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
    ) as current:
        document_path = coordinator.path_for("project") / "document.json"
        document_path.write_bytes(document_path.read_bytes() + b"\n")
        with pytest.raises(topology.ZeError) as changed:
            coordinator._revalidate_strict_repository_snapshot(
                current,
                _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
            )
        assert changed.value.code == "project_topology_conflict"


def test_repository_admission_cas_is_irreversible_and_ack_loss_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("project", build=_build("project", "v1"), validate=_validate)
    with coordinator._open_strict_repository_migration(
        _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
    ) as snapshot:
        initial = snapshot.admission
        freeze = _install_cutover_freeze(coordinator, initial)
    assert freeze.source_admission_revision == initial.revision
    assert freeze.authority_epoch == initial.authority_epoch

    cutover_id = "d" * 64
    real_atomic_json = topology._atomic_json
    injected = False

    def lose_ack(path: Path, value: dict[str, Any], *, mode: int = 0o600) -> bool:
        nonlocal injected
        result = real_atomic_json(path, value, mode=mode)
        if path == coordinator.repository_admission_path and not injected:
            injected = True
            raise OSError("injected return-edge loss")
        return result

    monkeypatch.setattr(topology, "_atomic_json", lose_ack)
    with pytest.raises(topology.ZeError) as unknown:
        coordinator._compare_and_swap_repository_admission(
            initial,
            target_state="cutover_pending",
            cutover_id=cutover_id,
            _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
        )
    assert unknown.value.code == "project_topology_durability_unknown"
    pending = coordinator._compare_and_swap_repository_admission(
        initial,
        target_state="cutover_pending",
        cutover_id=cutover_id,
        _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
    )
    assert pending.revision == initial.revision + 1
    assert pending.authority_epoch == initial.authority_epoch

    with pytest.raises(topology.ZeError) as irreversible_pending:
        coordinator._compare_and_swap_repository_admission(
            pending,
            target_state="legacy",
            cutover_id=cutover_id,
            _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
        )
    assert irreversible_pending.value.code == "project_topology_conflict"

    sqlite = coordinator._compare_and_swap_repository_admission(
        pending,
        target_state="sqlite",
        cutover_id=cutover_id,
        _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
    )
    assert sqlite.authority_state == "sqlite"
    assert sqlite.authority_epoch == initial.authority_epoch
    assert sqlite.revision == initial.revision + 2
    with coordinator._open_sqlite_repository_authority(
        repository_epoch=initial.authority_epoch,
        cutover_id=cutover_id,
        source_admission_revision=initial.revision,
        runtime_generation=CUTOVER_RUNTIME_GENERATION,
        code_digest=CUTOVER_CODE_DIGEST,
        _capability=topology._REPOSITORY_SQLITE_AUTHORITY_CAPABILITY,
    ) as leased:
        assert leased == sqlite
    with pytest.raises(topology.ZeError) as mismatched_lease:
        with coordinator._open_sqlite_repository_authority(
            repository_epoch=initial.authority_epoch,
            cutover_id=cutover_id,
            source_admission_revision=initial.revision + 1,
            runtime_generation=CUTOVER_RUNTIME_GENERATION,
            code_digest=CUTOVER_CODE_DIGEST,
            _capability=topology._REPOSITORY_SQLITE_AUTHORITY_CAPABILITY,
        ):
            pass
    assert mismatched_lease.value.code == "project_recovery_required"
    with pytest.raises(topology.ZeError) as mismatched_identity:
        with coordinator._open_sqlite_repository_authority(
            repository_epoch=initial.authority_epoch,
            cutover_id=cutover_id,
            source_admission_revision=initial.revision,
            runtime_generation="other-runtime",
            code_digest=CUTOVER_CODE_DIGEST,
            _capability=topology._REPOSITORY_SQLITE_AUTHORITY_CAPABILITY,
        ):
            pass
    assert mismatched_identity.value.code == "project_recovery_required"
    assert mismatched_identity.value.context["reason"] == (
        "sqlite_cutover_freeze_mismatch"
    )
    with pytest.raises(topology.ZeError) as irreversible:
        coordinator._compare_and_swap_repository_admission(
            sqlite,
            target_state="legacy",
            cutover_id=cutover_id,
            _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
        )
    assert irreversible.value.code == "project_topology_conflict"
    coordinator._repository_cutover_freeze_path.unlink()
    with pytest.raises(topology.ZeError) as missing_freeze:
        with coordinator._open_sqlite_repository_authority(
            repository_epoch=initial.authority_epoch,
            cutover_id=cutover_id,
            source_admission_revision=initial.revision,
            runtime_generation=CUTOVER_RUNTIME_GENERATION,
            code_digest=CUTOVER_CODE_DIGEST,
            _capability=topology._REPOSITORY_SQLITE_AUTHORITY_CAPABILITY,
        ):
            pass
    assert missing_freeze.value.code == "project_recovery_required"
    assert missing_freeze.value.context["reason"] == "cutover_freeze_missing"


def test_pending_admission_blocks_every_ordinary_file_repository_entry(
    tmp_path: Path,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("project", build=_build("project", "v1"), validate=_validate)
    with coordinator.open_live("project") as handle:
        captured_handle = handle
    with coordinator._open_strict_repository_migration(
        _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
    ) as snapshot:
        _install_cutover_freeze(coordinator, snapshot.admission)
        coordinator._compare_and_swap_repository_admission(
            snapshot.admission,
            target_state="cutover_pending",
            cutover_id="f" * 64,
            _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
        )

    callback_called = False

    def forbidden_callback(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal callback_called
        callback_called = True
        raise AssertionError("legacy callback crossed repository admission")

    def enter(context: Any) -> None:
        with context:
            pass

    blocked_copy_target = tmp_path / "blocked-copy"
    entries = (
        lambda: coordinator.list_live_ids(),
        lambda: coordinator.snapshot_live(forbidden_callback),
        lambda: coordinator.initialize(validate=forbidden_callback),
        lambda: enter(coordinator.legacy_migration_guard()),
        lambda: enter(coordinator.open_live("project")),
        lambda: enter(coordinator.open_live_scan_source("project", 1)),
        lambda: enter(coordinator.activity(captured_handle)),
        lambda: coordinator.intent_status("project", "1" * 32),
        lambda: coordinator.create(
            "new", build=forbidden_callback, validate=forbidden_callback,
        ),
        lambda: coordinator.duplicate(
            "project", "copy", rewrite=forbidden_callback,
            validate=forbidden_callback,
        ),
        lambda: coordinator.replace(
            "project", build=forbidden_callback, validate=forbidden_callback,
        ),
        lambda: coordinator.delete("project", verify_existing=forbidden_callback),
        lambda: coordinator.copy_durable_source(
            coordinator.path_for("project"),
            blocked_copy_target,
        ),
        lambda: coordinator.read_durable_source_file(
            coordinator.path_for("project"),
            "document.json",
        ),
        lambda: coordinator.present_durable_source_paths(
            coordinator.path_for("project"),
        ),
        lambda: enter(coordinator.open_strict_catalog_migration()),
    )
    for invoke in entries:
        with pytest.raises(topology.ZeError) as captured:
            invoke()
        assert captured.value.code == "project_authority_moved"
        assert captured.value.context["authority_state"] == "cutover_pending"
    assert callback_called is False
    assert not blocked_copy_target.exists()


def test_admission_transition_ex_drains_activity_and_forks_reject_pending(
    tmp_path: Path,
) -> None:
    ctx = _fork_context()
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("project", build=_build("project", "v1"), validate=_validate)
    with coordinator._open_strict_repository_migration(
        _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
    ) as snapshot:
        initial = snapshot.admission

    ready = ctx.Event()
    start = ctx.Event()
    release = ctx.Event()
    results = ctx.Queue()
    holder = ctx.Process(
        target=_activity_barrier_worker,
        args=(str(coordinator.projects_root), ready, start, release, results, 2.0),
    )
    holder.start()
    assert ready.wait(5)
    start.set()
    assert results.get(timeout=5) == "acquired"
    impatient = topology.ProjectTopologyCoordinator(
        coordinator.projects_root,
        lock_timeout=0.2,
    )
    with pytest.raises(topology.ZeError) as blocked:
        impatient._compare_and_swap_repository_admission(
            initial,
            target_state="cutover_pending",
            cutover_id="9" * 64,
            _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
        )
    assert blocked.value.code == "project_busy"
    release.set()
    _join(holder)

    with impatient._open_strict_repository_migration(
        _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
    ) as snapshot:
        assert snapshot.admission == initial
        _install_cutover_freeze(impatient, snapshot.admission)
        impatient._compare_and_swap_repository_admission(
            initial,
            target_state="cutover_pending",
            cutover_id="9" * 64,
            _capability=topology._REPOSITORY_ADMISSION_CAPABILITY,
        )
    child_results = ctx.Queue()
    child = ctx.Process(
        target=_admission_reject_worker,
        args=(str(coordinator.projects_root), child_results),
    )
    child.start()
    assert child_results.get(timeout=5) == "project_authority_moved"
    _join(child)


@pytest.mark.filterwarnings(
    "ignore:This process .* is multi-threaded, use of fork\\(\\) may lead to deadlocks in the child:DeprecationWarning"
)
def test_fork_waits_until_every_open_lock_fd_is_registered_for_child_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fork_context()
    if not hasattr(os, "fork"):
        pytest.skip("direct fork is unavailable")

    lock_path = tmp_path / "fork-registration-window.lock"
    raw_opened = threading.Event()
    allow_fdopen = threading.Event()
    fork_waiting = threading.Event()
    lock_acquired = threading.Event()
    release_lock = threading.Event()
    captured: dict[str, Any] = {}
    main_thread_id = threading.get_ident()
    real_guard = topology._HELD_LOCK_STREAMS_GUARD
    real_fdopen = topology.os.fdopen

    class ObservedGuard:
        def acquire(self, *args: Any, **kwargs: Any) -> bool:
            if (
                threading.get_ident() == main_thread_id
                and raw_opened.is_set()
            ):
                fork_waiting.set()
            return real_guard.acquire(*args, **kwargs)

        def release(self) -> None:
            real_guard.release()

        def __enter__(self) -> "ObservedGuard":
            self.acquire()
            return self

        def __exit__(self, *_args: Any) -> None:
            self.release()

    observed_guard = ObservedGuard()
    monkeypatch.setattr(
        topology,
        "_HELD_LOCK_STREAMS_GUARD",
        observed_guard,
    )

    def paused_fdopen(fd: int, *args: Any, **kwargs: Any) -> Any:
        captured["fd"] = fd
        raw_opened.set()
        if not allow_fdopen.wait(5):
            raise TimeoutError("fd registration test timed out")
        stream = real_fdopen(fd, *args, **kwargs)
        captured["stream"] = stream
        return stream

    monkeypatch.setattr(topology.os, "fdopen", paused_fdopen)

    def hold_path_lock() -> None:
        with topology._path_lock(lock_path, exclusive=False, timeout=2.0):
            lock_acquired.set()
            if not release_lock.wait(5):
                raise TimeoutError("fork registration holder timed out")

    holder = threading.Thread(target=hold_path_lock)
    holder.start()
    assert raw_opened.wait(5)

    def release_open_after_fork_reaches_before_callback() -> None:
        assert fork_waiting.wait(5)
        allow_fdopen.set()

    releaser = threading.Thread(
        target=release_open_after_fork_reaches_before_callback,
    )
    releaser.start()
    child_pid = os.fork()
    if child_pid == 0:  # pragma: no cover - assertion is reported by exit code
        stream = captured.get("stream")
        clean = bool(
            stream is not None
            and stream.closed
            and not getattr(topology._HELD_LOCKS, "entries", {})
            and not topology._HELD_LOCK_STREAMS
        )
        os._exit(0 if clean else 91)

    waited_pid, status = os.waitpid(child_pid, 0)
    assert waited_pid == child_pid
    assert os.waitstatus_to_exitcode(status) == 0
    assert lock_acquired.wait(5)
    release_lock.set()
    holder.join(5)
    releaser.join(5)
    assert not holder.is_alive()
    assert not releaser.is_alive()


def test_symlink_in_staging_is_rejected_without_following_target(tmp_path: Path) -> None:
    coordinator = _new_coordinator(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("do-not-touch", encoding="utf-8")

    def build_with_symlink(root: Path) -> None:
        _write_project(root, "unsafe", "value")
        try:
            (root / "entities" / "escape").symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(topology.ZeError) as captured:
        coordinator.create("unsafe", build=build_with_symlink, validate=_validate)
    assert captured.value.code == "project_topology_write_failed"
    assert outside.read_text(encoding="utf-8") == "do-not-touch"
    assert not coordinator.path_for("unsafe").exists()
    assert not list(coordinator.journal_root.iterdir())
    assert not list(coordinator.staging_root.iterdir())


def test_initialize_freezes_symlink_project_root_without_following_it(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    coordinator = topology.ProjectTopologyCoordinator(projects_root)
    outside = tmp_path / "outside-project"
    _write_project(outside, "evil", "outside")
    try:
        (projects_root / "evil").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")

    coordinator.initialize(validate=_validate)
    state = coordinator._read_state("evil")
    assert state and state["status"] == "recovery_required"
    assert coordinator.list_live_ids() == []
    assert json.loads((outside / "document.json").read_text(encoding="utf-8"))["value"] == "outside"


def test_control_directory_and_lock_symlinks_are_rejected(tmp_path: Path) -> None:
    projects_root = tmp_path / "data" / "projects"
    projects_root.mkdir(parents=True)
    outside_dir = tmp_path / "outside-control"
    outside_dir.mkdir()
    try:
        (projects_root / ".ze-staging").symlink_to(outside_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(topology.ZeError) as unsafe_control:
        topology.ProjectTopologyCoordinator(projects_root)
    assert unsafe_control.value.code == "project_topology_unavailable"

    (projects_root / ".ze-staging").unlink()
    coordinator = topology.ProjectTopologyCoordinator(projects_root)
    outside_lock = tmp_path / "outside.lock"
    outside_lock.write_text("sentinel", encoding="utf-8")
    coordinator.repo_lock.symlink_to(outside_lock)
    with pytest.raises(topology.ZeError) as unsafe_lock:
        coordinator.initialize(validate=_validate)
    assert unsafe_lock.value.code == "project_topology_unavailable"
    assert outside_lock.read_text(encoding="utf-8") == "sentinel"


def test_legacy_document_symlink_is_frozen_before_validation(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    project = projects_root / "legacy"
    project.mkdir(parents=True)
    outside = tmp_path / "outside-document.json"
    outside.write_text(json.dumps({"project_id": "legacy", "value": "outside"}), encoding="utf-8")
    try:
        (project / "document.json").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")

    validated = False

    def validator(_root: Path) -> None:
        nonlocal validated
        validated = True

    coordinator = topology.ProjectTopologyCoordinator(projects_root)
    coordinator.initialize(validate=validator)
    assert validated is False
    assert coordinator._read_state("legacy")["status"] == "recovery_required"
    assert json.loads(outside.read_text(encoding="utf-8"))["value"] == "outside"


def test_duplicate_copies_only_canonical_entries_and_writes_target_marker(tmp_path: Path) -> None:
    coordinator = _new_coordinator(tmp_path)
    source_commit = coordinator.create(
        "source",
        build=_build("source", "seed"),
        validate=_validate,
    )
    source = coordinator.path_for("source")
    (source / "oplog.ndjson").write_text("runtime-only\n", encoding="utf-8")
    (source / ".pipelines").mkdir()
    (source / ".pipelines" / "run.json").write_text("{}", encoding="utf-8")
    (source / "volatile.tmp").write_text("ignore", encoding="utf-8")

    duplicate_commit = coordinator.duplicate(
        "source",
        "copy",
        rewrite=_rewrite("copy", "copied"),
        validate=_validate,
    )
    target = coordinator.path_for("copy")
    copied_head = json.loads((target / "document.json").read_text(encoding="utf-8"))
    assert copied_head["project_id"] == "copy"
    assert copied_head["id"] == "copy"
    assert copied_head["value"] == "copied"
    assert copied_head["_v"] == 1
    assert json.loads((target / "entities" / "item.json").read_text(encoding="utf-8")) == {
        "id": "item",
        "value": "seed",
    }
    assert (target / "assets" / "blob.bin").read_bytes() == b"seed"
    assert not (target / "oplog.ndjson").exists()
    assert not (target / ".pipelines").exists()
    assert not (target / "volatile.tmp").exists()
    marker = coordinator._read_marker(target)
    assert marker == {
        "schema": topology.MARKER_SCHEMA,
        "project_id": "copy",
        "generation": duplicate_commit.generation,
        "operation_id": duplicate_commit.operation_id,
    }
    assert marker["operation_id"] != source_commit.operation_id


def test_topology_metadata_rewrite_wrapper_rejects_live_project_path(tmp_path: Path) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("source", build=_build("source", "seed"), validate=_validate)
    live = coordinator.path_for("source")
    before = (live / "document.json").read_bytes()

    with pytest.raises(topology.ZeError) as captured:
        coordinator._rewrite_staged_document_metadata(
            live,
            expected_version=1,
            id="forbidden",
            name="forbidden",
            acl={"owner": "alice", "collaborators": []},
            config={},
        )
    assert captured.value.code == "topology_metadata_rewrite_forbidden"
    assert captured.value.context == {"reason": "topology_staging_path_required"}
    assert (live / "document.json").read_bytes() == before


def test_create_never_publishes_marker_for_mismatched_document_identity(
    tmp_path: Path,
) -> None:
    coordinator = _new_coordinator(tmp_path)

    with pytest.raises(topology.ZeError) as captured:
        coordinator.create(
            "expected",
            build=_build("different", "seed"),
            validate=_validate,
        )
    assert captured.value.code == "project_recovery_required"
    assert captured.value.context == {
        "project_id": "expected",
        "reason": "document_id_mismatch",
    }
    assert not coordinator.path_for("expected").exists()
    assert coordinator.list_live_ids() == []


@pytest.mark.parametrize("operation", ["duplicate", "replace", "delete"])
def test_topology_mutations_reject_mismatched_live_document_identity(
    tmp_path: Path,
    operation: str,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("source", build=_build("source", "seed"), validate=_validate)
    live = coordinator.path_for("source")
    payload = json.loads((live / "document.json").read_text(encoding="utf-8"))
    payload["id"] = "different"
    (live / "document.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(topology.ZeError) as captured:
        if operation == "duplicate":
            coordinator.duplicate(
                "source",
                "copy",
                rewrite=_rewrite("copy", "copied"),
                validate=_validate,
            )
        elif operation == "replace":
            coordinator.replace(
                "source",
                build=_build("source", "replacement"),
                validate=_validate,
            )
        else:
            coordinator.delete("source")
    assert captured.value.code == "project_recovery_required"
    assert captured.value.context == {
        "project_id": "source",
        "reason": "document_id_mismatch",
    }
    assert live.exists()
    assert not coordinator.path_for("copy").exists()


def test_initialize_freezes_marker_document_identity_mismatch(tmp_path: Path) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("source", build=_build("source", "seed"), validate=_validate)
    live = coordinator.path_for("source")
    payload = json.loads((live / "document.json").read_text(encoding="utf-8"))
    payload["id"] = "different"
    (live / "document.json").write_text(json.dumps(payload), encoding="utf-8")

    recovered = topology.ProjectTopologyCoordinator(
        coordinator.projects_root,
        lock_timeout=2.0,
    )
    recovered.initialize(validate=_validate)

    assert recovered.list_live_ids() == []
    with pytest.raises(topology.ZeError) as captured:
        with recovered.open_live("source"):
            pass
    assert captured.value.code == "project_recovery_required"


def test_staging_rewrite_binds_path_proof_to_opened_root_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("source", build=_build("source", "seed"), validate=_validate)
    coordinator.create("victim", build=_build("victim", "kept"), validate=_validate)
    victim = coordinator.path_for("victim")
    victim_before = (victim / "document.json").read_bytes()
    real_rewrite = topology._rewrite_document_metadata_for_topology_staging

    def replace_after_topology_check(path: Path, **kwargs: Any):
        stage = Path(path).parent
        parked = stage.with_name(f"{stage.name}.pinned-original")
        os.rename(stage, parked)
        os.rename(victim, stage)
        try:
            return real_rewrite(path, **kwargs)
        finally:
            os.rename(stage, victim)
            os.rename(parked, stage)

    monkeypatch.setattr(
        topology,
        "_rewrite_document_metadata_for_topology_staging",
        replace_after_topology_check,
    )

    def rewrite(stage: Path) -> None:
        coordinator._rewrite_staged_document_metadata(
            stage,
            expected_version=1,
            id="copy",
            name="copy",
            acl={"owner": "alice", "collaborators": []},
            config={},
        )

    with pytest.raises(topology.ZeError) as captured:
        coordinator.duplicate(
            "source",
            "copy",
            rewrite=rewrite,
            validate=_validate,
        )
    assert captured.value.code == "document_storage_path_unsafe"
    assert captured.value.context["reason"] == "project_directory_identity_mismatch"
    assert captured.value.context["commit_point_reached"] is False
    assert not coordinator.path_for("copy").exists()
    assert (victim / "document.json").read_bytes() == victim_before
    with coordinator.open_live("victim") as handle:
        assert handle.project_id == "victim"


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink", "fifo", "writable"])
def test_duplicate_durable_copy_rejects_unsafe_nested_inode(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    """The primitive shared with legacy migration rejects every unsafe inode class."""
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("source", build=_build("source", "seed"), validate=_validate)
    source = coordinator.path_for("source")
    unsafe = source / "assets" / f"unsafe-{unsafe_kind}"
    outside = tmp_path / f"outside-{unsafe_kind}.bin"
    outside.write_bytes(b"outside")

    try:
        if unsafe_kind == "symlink":
            unsafe.symlink_to(outside)
        elif unsafe_kind == "hardlink":
            os.link(outside, unsafe)
        elif unsafe_kind == "fifo":
            if not hasattr(os, "mkfifo"):
                pytest.skip("FIFOs are unavailable on this platform")
            os.mkfifo(unsafe)
        else:
            unsafe.write_bytes(b"writable")
            unsafe.chmod(0o666)
    except (OSError, NotImplementedError):
        pytest.skip(f"{unsafe_kind} filesystem probe is unavailable")

    with pytest.raises(topology.ZeError) as captured:
        coordinator.duplicate(
            "source", "copy", rewrite=_rewrite("copy", "copy"), validate=_validate,
        )
    assert captured.value.code == "project_topology_write_failed"
    assert captured.value.context == {"reason": "unsafe_durable_source_entry"}
    assert unsafe.name not in str(captured.value)
    assert unsafe.name not in json.dumps(captured.value.context)
    assert source.exists()
    assert unsafe.exists() or unsafe.is_symlink()
    assert outside.read_bytes() == b"outside"
    assert not coordinator.path_for("copy").exists()
    assert list(coordinator.staging_root.iterdir()) == []


def test_duplicate_copy_preserves_posix_backslash_filename(tmp_path: Path) -> None:
    """A backslash is data, not a path separator, on the supported POSIX boundary."""
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("source", build=_build("source", "seed"), validate=_validate)
    source_file = coordinator.path_for("source") / "assets" / "legacy\\name.bin"
    source_file.write_bytes(b"legacy-posix-name")

    coordinator.duplicate(
        "source", "copy", rewrite=_rewrite("copy", "copy"), validate=_validate,
    )

    copied = coordinator.path_for("copy") / "assets" / "legacy\\name.bin"
    assert copied.read_bytes() == b"legacy-posix-name"


@pytest.mark.parametrize(
    ("budget_name", "limit", "reason"),
    [
        ("_COPY_MAX_DEPTH", 1, "durable_source_depth_exceeded"),
        ("_COPY_MAX_ENTRIES", 1, "durable_source_entries_exceeded"),
        ("_COPY_MAX_TOTAL_BYTES", 1, "durable_source_bytes_exceeded"),
    ],
)
def test_duplicate_copy_budget_exhaustion_never_publishes_partial_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    budget_name: str,
    limit: int,
    reason: str,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("source", build=_build("source", "seed"), validate=_validate)
    monkeypatch.setattr(topology, budget_name, limit)

    with pytest.raises(topology.ZeError) as captured:
        coordinator.duplicate(
            "source", "copy", rewrite=_rewrite("copy", "copy"), validate=_validate,
        )
    assert captured.value.code == "project_topology_write_failed"
    assert captured.value.context == {"reason": reason}
    assert coordinator.path_for("source").exists()
    assert not coordinator.path_for("copy").exists()
    assert list(coordinator.staging_root.iterdir()) == []


def test_initialize_freezes_state_only_contradictions_without_blocking_healthy_project(
    tmp_path: Path,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("healthy", build=_build("healthy", "ok"), validate=_validate)
    coordinator._write_state("stuck", 1, "quiescing", "a" * 32)
    coordinator._write_state("missing-live", 1, "live", "b" * 32)

    recovered = topology.ProjectTopologyCoordinator(coordinator.projects_root)
    recovered.initialize(validate=_validate)
    assert recovered._read_state("stuck")["status"] == "recovery_required"
    assert recovered._read_state("missing-live")["status"] == "recovery_required"
    assert recovered.list_live_ids() == ["healthy"]


def test_replace_recovers_ready_stage_over_deleted_tombstone(tmp_path: Path) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("demo", build=_build("demo", "old"), validate=_validate)
    deleted = coordinator.delete("demo", operation_id="c" * 32)
    operation_id = "d" * 32
    generation = deleted.generation + 1
    stage = coordinator.staging_root / operation_id
    stage.mkdir()
    _write_project(stage, "demo", "reset")
    coordinator._write_marker(stage, "demo", generation, operation_id)
    topology._fsync_tree(stage)
    journal = coordinator._new_journal(
        operation_id,
        "replace",
        target_id="demo",
        old_generation=deleted.generation,
        new_generation=generation,
        old_status="deleted",
        old_operation_id=deleted.operation_id,
    )
    journal["phase"] = "ready"
    coordinator._claim_journal(journal)
    coordinator._write_state("demo", deleted.generation, "quiescing", operation_id)

    recovered = topology.ProjectTopologyCoordinator(coordinator.projects_root)
    recovered.initialize(validate=_validate)
    assert recovered._read_state("demo") == {
        "schema": topology.STATE_SCHEMA,
        "project_id": "demo",
        "generation": generation,
        "status": "live",
        "operation_id": operation_id,
    }
    assert json.loads(
        (recovered.path_for("demo") / "document.json").read_text(encoding="utf-8")
    )["value"] == "reset"
    assert not coordinator._journal_path(operation_id).exists()


def test_replace_committed_journal_failure_returns_cleanup_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("project", build=_build("project", "old"), validate=_validate)
    operation_id = "e" * 32
    real_write_journal = coordinator._write_journal

    def fail_committed(journal: dict[str, Any]) -> None:
        if journal.get("operation_id") == operation_id and journal.get("phase") == "committed":
            raise OSError("injected committed-journal failure")
        real_write_journal(journal)

    monkeypatch.setattr(coordinator, "_write_journal", fail_committed)
    commit = coordinator.replace(
        "project",
        build=_build("project", "new"),
        validate=_validate,
        operation_id=operation_id,
    )
    assert commit.cleanup_pending is True
    assert coordinator._read_state("project")["generation"] == 2
    assert coordinator._read_marker(coordinator.path_for("project"))["operation_id"] == operation_id
    assert json.loads(
        coordinator._journal_path(operation_id).read_text(encoding="utf-8")
    )["phase"] == "promoted"


def test_recovery_cleanup_failure_does_not_freeze_proven_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    committed = coordinator.create(
        "project", build=_build("project", "value"), validate=_validate,
    )
    operation_id = committed.operation_id
    journal = coordinator._new_journal(
        operation_id,
        "create",
        target_id="project",
        old_generation=0,
        new_generation=committed.generation,
    )
    journal["phase"] = "committed"
    coordinator._write_journal(journal)
    real_finish = topology.ProjectTopologyCoordinator._finish_journal

    def fail_finish(self: topology.ProjectTopologyCoordinator, opid: str) -> None:
        if opid == operation_id:
            raise OSError("injected cleanup acknowledgement failure")
        real_finish(self, opid)

    monkeypatch.setattr(topology.ProjectTopologyCoordinator, "_finish_journal", fail_finish)
    recovered = topology.ProjectTopologyCoordinator(coordinator.projects_root)
    recovered.initialize(validate=_validate)

    assert recovered._read_state("project")["status"] == "live"
    with recovered.open_live("project") as handle:
        assert handle.generation == committed.generation
    assert json.loads(
        recovered._journal_path(operation_id).read_text(encoding="utf-8")
    )["phase"] == "cleanup_pending"


def test_kind_aware_unissued_intents_distinguish_baseline_from_conflict(
    tmp_path: Path,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("live", build=_build("live", "old"), validate=_validate)

    assert coordinator.intent_status(
        "new-create", "1" * 32, kind="create",
    )["status"] == "not_committed"
    assert coordinator.intent_status(
        "new-copy", "2" * 32, kind="duplicate",
    )["status"] == "not_committed"
    assert coordinator.intent_status(
        "live", "3" * 32, kind="delete",
    )["status"] == "not_committed"
    assert coordinator.intent_status(
        "live", "4" * 32, kind="replace",
    )["status"] == "not_committed"

    # A live namespace is the expected precondition for delete/replace, but it
    # is an unrelated occupant for create/duplicate.
    assert coordinator.intent_status(
        "live", "5" * 32, kind="create",
    )["status"] == "conflict"
    assert coordinator.intent_status(
        "live", "6" * 32, kind="duplicate",
    )["status"] == "conflict"


def test_committed_outcomes_survive_later_generations_for_every_public_kind(
    tmp_path: Path,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    create_id = "a" * 32
    replace_id = "b" * 32
    delete_id = "c" * 32
    recreate_id = "d" * 32
    source_id = "e" * 32
    duplicate_id = "f" * 32
    copy_replace_id = "1" * 32

    coordinator.create(
        "project", build=_build("project", "v1"), validate=_validate,
        operation_id=create_id,
    )
    coordinator.replace(
        "project", build=_build("project", "v2"), validate=_validate,
        operation_id=replace_id,
    )
    coordinator.delete("project", operation_id=delete_id)
    coordinator.replace(
        "project", build=_build("project", "v4"), validate=_validate,
        operation_id=recreate_id,
    )

    coordinator.create(
        "source", build=_build("source", "seed"), validate=_validate,
        operation_id=source_id,
    )
    coordinator.duplicate(
        "source", "copy", rewrite=_rewrite("copy", "copy-v1"),
        validate=_validate, operation_id=duplicate_id,
    )
    coordinator.replace(
        "copy", build=_build("copy", "copy-v2"), validate=_validate,
        operation_id=copy_replace_id,
    )

    expected = (
        ("project", create_id, "create", "committed", 1),
        ("project", replace_id, "replace", "committed", 2),
        ("project", delete_id, "delete", "deleted", 3),
        ("project", recreate_id, "replace", "committed", 4),
        ("copy", duplicate_id, "duplicate", "committed", 1),
        ("copy", copy_replace_id, "replace", "committed", 2),
    )
    for project_id, intent_id, kind, status, generation in expected:
        proof = coordinator.intent_status(project_id, intent_id, kind=kind)
        assert proof["status"] == status
        assert proof["kind"] == kind
        assert proof["generation"] == generation
        assert coordinator._outcome_path(intent_id).is_file()

    # Re-opening the coordinator proves these are durable receipts rather than
    # process-local history or the current marker's operation id.
    recovered = topology.ProjectTopologyCoordinator(coordinator.projects_root)
    recovered.initialize(validate=_validate)
    assert recovered.intent_status(
        "project", create_id, kind="create",
    )["generation"] == 1
    assert recovered.intent_status(
        "project", delete_id, kind="delete",
    )["status"] == "deleted"
    assert recovered.intent_status(
        "copy", duplicate_id, kind="duplicate",
    )["generation"] == 1


def test_kind_mismatch_conflicts_and_only_proven_abort_releases_operation_id(
    tmp_path: Path,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("project", build=_build("project", "old"), validate=_validate)
    operation_id = "7" * 32
    state = coordinator._read_state("project")
    assert state is not None
    journal = coordinator._new_journal(
        operation_id,
        "replace",
        target_id="project",
        old_generation=state["generation"],
        new_generation=state["generation"] + 1,
        old_status="live",
        old_operation_id=state["operation_id"],
    )
    coordinator._claim_journal(journal)

    assert coordinator.intent_status(
        "project", operation_id, kind="delete",
    )["status"] == "conflict"
    with pytest.raises(topology.ZeError) as pending_reuse:
        coordinator.create(
            "other", build=_build("other", "bad"), validate=_validate,
            operation_id=operation_id,
        )
    assert pending_reuse.value.code == "project_busy"

    # A prepared operation with no namespace effect may be fully rolled back;
    # after its journal is durably removed the same logical intent can replay.
    coordinator._finish_journal(operation_id)
    coordinator.replace(
        "project", build=_build("project", "new"), validate=_validate,
        operation_id=operation_id,
    )
    assert coordinator.intent_status(
        "project", operation_id, kind="delete",
    )["status"] == "conflict"
    assert coordinator.intent_status(
        "project", operation_id, kind="replace",
    )["status"] == "committed"

    # Once committed, the global id can neither be repurposed for a different
    # target nor executed again after its journal has been cleaned.
    with pytest.raises(topology.ZeError) as committed_reuse:
        coordinator.create(
            "other", build=_build("other", "bad"), validate=_validate,
            operation_id=operation_id,
        )
    assert committed_reuse.value.code == "project_topology_conflict"


def test_outcome_write_failure_keeps_journal_until_recovery_can_seal_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    operation_id = "8" * 32
    real_atomic_json = topology._atomic_json

    def fail_outcome(path: Path, value: dict[str, Any], **kwargs: Any) -> bool:
        if Path(path).parent == coordinator.outcome_root:
            raise OSError("injected outcome fsync failure")
        return real_atomic_json(path, value, **kwargs)

    monkeypatch.setattr(topology, "_atomic_json", fail_outcome)
    commit = coordinator.create(
        "project", build=_build("project", "value"), validate=_validate,
        operation_id=operation_id,
    )
    assert commit.cleanup_pending is True
    assert not coordinator._outcome_path(operation_id).exists()
    assert json.loads(
        coordinator._journal_path(operation_id).read_text(encoding="utf-8")
    )["phase"] == "cleanup_pending"

    monkeypatch.setattr(topology, "_atomic_json", real_atomic_json)
    recovered = topology.ProjectTopologyCoordinator(coordinator.projects_root)
    recovered.initialize(validate=_validate)
    assert not recovered._journal_path(operation_id).exists()
    assert recovered._outcome_path(operation_id).is_file()
    assert recovered.intent_status(
        "project", operation_id, kind="create",
    )["status"] == "committed"


def test_generation_cas_and_lock_scoped_verifiers_fail_before_namespace_cutover(
    tmp_path: Path,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("source", build=_build("source", "v1"), validate=_validate)

    with pytest.raises(topology.ZeError) as stale_duplicate:
        coordinator.duplicate(
            "source", "copy-stale",
            rewrite=_rewrite("copy-stale", "v1"), validate=_validate,
            expected_generation=2,
        )
    assert stale_duplicate.value.code == "project_generation_stale"
    assert not coordinator.path_for("copy-stale").exists()

    with pytest.raises(topology.ZeError) as denied_duplicate:
        coordinator.duplicate(
            "source", "copy-denied",
            rewrite=_rewrite("copy-denied", "v1"), validate=_validate,
            expected_generation=1,
            verify_source=lambda _root: (_ for _ in ()).throw(
                topology.ZeError("acl_denied", "membership changed")
            ),
        )
    assert denied_duplicate.value.code == "acl_denied"
    assert not coordinator.path_for("copy-denied").exists()

    with pytest.raises(topology.ZeError) as stale_delete:
        coordinator.delete("source", expected_generation=2)
    assert stale_delete.value.code == "project_generation_stale"

    with pytest.raises(topology.ZeError) as denied_delete:
        coordinator.delete(
            "source", expected_generation=1,
            verify_existing=lambda _root: (_ for _ in ()).throw(
                topology.ZeError("acl_denied", "ownership changed")
            ),
        )
    assert denied_delete.value.code == "acl_denied"
    with coordinator.open_live("source") as handle:
        assert handle.generation == 1
        assert json.loads((handle.path / "document.json").read_text())["value"] == "v1"


def test_replace_expected_zero_only_creates_absent_namespace(tmp_path: Path) -> None:
    coordinator = _new_coordinator(tmp_path)
    first = coordinator.replace(
        "demo", build=_build("demo", "v1"), validate=_validate,
        expected_generation=0,
    )
    assert first.generation == 1
    with pytest.raises(topology.ZeError) as stale:
        coordinator.replace(
            "demo", build=_build("demo", "bad"), validate=_validate,
            expected_generation=0,
        )
    assert stale.value.code == "project_generation_stale"
    second = coordinator.replace(
        "demo", build=_build("demo", "v2"), validate=_validate,
        expected_generation=1,
    )
    assert second.generation == 2


def test_intent_status_v2_separates_read_scope_from_durable_reservation(
    tmp_path: Path,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    operation_id = "a" * 32

    untouched = coordinator.intent_status(
        "claimed", operation_id, kind="create", principal_id="alice",
    )
    assert untouched["status"] == "not_committed"
    assert untouched["kind"] == "create"
    assert untouched["proof_revision"] == 2
    assert untouched["principal_scoped"] is True
    assert untouched["intent_reserved"] is False
    assert "principal_bound" not in untouched
    assert not (coordinator.intent_root / f"{operation_id}.json").exists()
    assert not coordinator.path_for("claimed").exists()

    committed = coordinator.create(
        "claimed", build=_build("claimed", "alice"), validate=_validate,
        operation_id=operation_id, principal_id="alice",
    )
    assert committed.generation == 1
    proof = coordinator.intent_status(
        "claimed", operation_id, kind="create", principal_id="alice",
    )
    assert proof["status"] == "committed"
    assert proof["kind"] == "create"
    assert proof["proof_revision"] == 2
    assert proof["principal_scoped"] is True
    assert proof["intent_reserved"] is True
    with pytest.raises(topology.ZeError) as foreign_outcome:
        coordinator.intent_status(
            "claimed", operation_id, kind="create", principal_id="bob",
        )
    assert foreign_outcome.value.code == "acl_denied"


def test_unstarted_intent_reconciliation_cannot_allocate_control_files(tmp_path: Path) -> None:
    coordinator = _new_coordinator(tmp_path)
    lock_names = {path.name for path in coordinator.lock_root.iterdir()}
    for index in range(128):
        proof = coordinator.intent_status(
            "absent-project", f"{index:032x}", kind="create",
            principal_id="anonymous-guest",
        )
        assert proof["status"] == "not_committed"
        assert proof["proof_revision"] == 2
        assert proof["principal_scoped"] is True
        assert proof["intent_reserved"] is False
    assert list(coordinator.intent_root.iterdir()) == []
    assert {path.name for path in coordinator.lock_root.iterdir()} == lock_names


def test_failed_mutation_keeps_exact_intent_reservation_for_bounded_retry(
    tmp_path: Path,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    operation_id = "b" * 32

    def fail_build(_root: Path) -> None:
        raise RuntimeError("injected pre-effect failure")

    with pytest.raises(topology.ZeError) as failed:
        coordinator.create(
            "reserved", build=fail_build, validate=_validate,
            operation_id=operation_id, principal_id="alice",
        )
    assert failed.value.code == "project_topology_write_failed"

    proof = coordinator.intent_status(
        "reserved", operation_id, kind="create", principal_id="alice",
    )
    assert proof == {
        "project_id": "reserved",
        "intent_id": operation_id,
        "status": "not_committed",
        "kind": "create",
        "proof_revision": 2,
        "principal_scoped": True,
        "intent_reserved": True,
    }
    assert coordinator._read_principal_claim(operation_id) is not None
    assert not coordinator._journal_path(operation_id).exists()


def test_scoped_positive_proofs_without_principal_binding_fail_closed(
    tmp_path: Path,
) -> None:
    coordinator = _new_coordinator(tmp_path)
    outcome_id = "c" * 32
    coordinator.create(
        "legacy", build=_build("legacy", "value"), validate=_validate,
        operation_id=outcome_id,
    )

    with pytest.raises(topology.ZeError) as unbound_outcome:
        coordinator.intent_status(
            "legacy", outcome_id, kind="create", principal_id="alice",
        )
    assert unbound_outcome.value.code == "project_recovery_required"

    coordinator._outcome_path(outcome_id).unlink()
    with pytest.raises(topology.ZeError) as unbound_marker:
        coordinator.intent_status(
            "legacy", outcome_id, kind="create", principal_id="alice",
        )
    assert unbound_marker.value.code == "project_recovery_required"

    journal_id = "d" * 32
    journal = coordinator._new_journal(
        journal_id, "create", target_id="pending", old_generation=0,
        new_generation=1,
    )
    coordinator._claim_journal(journal)
    with pytest.raises(topology.ZeError) as unbound_journal:
        coordinator.intent_status(
            "pending", journal_id, kind="create", principal_id="alice",
        )
    assert unbound_journal.value.code == "project_recovery_required"


# ==============================================================
# adopt-heal(启动自愈):身份记录残缺/矛盾但文档完好 → 就地重建而非死冻结
# ==============================================================

def _reinit(tmp_path: Path) -> topology.ProjectTopologyCoordinator:
    """模拟进程重启:新 coordinator 对同一目录重跑启动对账。"""
    fresh = topology.ProjectTopologyCoordinator(tmp_path / "projects", lock_timeout=2.0)
    fresh.initialize(validate=_validate)
    return fresh


def _state_of(coord: topology.ProjectTopologyCoordinator, pid: str) -> dict:
    return json.loads(coord._state_path(pid).read_text(encoding="utf-8"))


def test_adopt_heal_rebuilds_missing_marker(tmp_path: Path) -> None:
    """marker 丢失(raw copy 只搬了内容+目录账本)→ 重启自愈为 live,而非 adopt-mismatch。"""
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("victim", build=_build("victim", "v1"), validate=_validate)
    root = coordinator.path_for("victim")
    (root / topology.PROJECT_MARKER).unlink()

    healed = _reinit(tmp_path)
    state = _state_of(healed, "victim")
    assert state["status"] == "live"
    assert (root / topology.PROJECT_MARKER).exists()
    marker = json.loads((root / topology.PROJECT_MARKER).read_text(encoding="utf-8"))
    assert marker["project_id"] == "victim"
    assert marker["generation"] == state["generation"]
    assert marker["operation_id"] == state["operation_id"]
    # 可正常获取 live 句柄(死胡同消失)
    healed._require_live("victim")


def test_adopt_heal_rebuilds_missing_state_row(tmp_path: Path) -> None:
    """目录账本行丢失(只搬了项目目录)→ 重启自愈为 live。"""
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("victim2", build=_build("victim2", "v1"), validate=_validate)
    coordinator._state_path("victim2").unlink()

    healed = _reinit(tmp_path)
    assert _state_of(healed, "victim2")["status"] == "live"
    healed._require_live("victim2")


def test_adopt_heal_reconciles_conflicting_records_with_generation_bump(tmp_path: Path) -> None:
    """marker 与账本行矛盾 → 自愈为一致身份,代际取 max+1 栅栏过期引用。"""
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("victim3", build=_build("victim3", "v1"), validate=_validate)
    root = coordinator.path_for("victim3")
    marker = json.loads((root / topology.PROJECT_MARKER).read_text(encoding="utf-8"))
    old_gen = marker["generation"]
    marker["generation"] = old_gen + 3   # 人为制造两侧代际矛盾
    (root / topology.PROJECT_MARKER).write_text(json.dumps(marker), encoding="utf-8")

    healed = _reinit(tmp_path)
    state = _state_of(healed, "victim3")
    assert state["status"] == "live"
    assert state["generation"] == old_gen + 4   # max(marker, state)+1
    healed._require_live("victim3")


def test_adopt_heal_refuses_renamed_dir_and_reports_reason(tmp_path: Path) -> None:
    """文件夹改名(文档 durable id ≠ 命名空间)修不了 → 仍 fail-closed,但原因可读。"""
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("original", build=_build("original", "v1"), validate=_validate)
    src = coordinator.path_for("original")
    renamed = src.parent / "renamed-slug"
    src.rename(renamed)                          # raw 改名:目录名与 doc id 脱钩
    (renamed / topology.PROJECT_MARKER).unlink()  # raw copy 常见:marker 也丢了
    coordinator._state_path("original").unlink()
    # 给改名后的命名空间放一行孤儿账本(复刻 design 现场:账本在、marker 缺、id 不符)
    coordinator._write_state("renamed-slug", 1, "recovery_required", "adopt-mismatch")

    healed = _reinit(tmp_path)
    assert _state_of(healed, "renamed-slug")["status"] == "recovery_required"
    with pytest.raises(topology.ZeError) as exc:
        healed._require_live("renamed-slug")
    assert exc.value.code == "project_recovery_required"
    assert exc.value.context.get("reason") == "adopt-invalid-document"


def test_fresh_create_unaffected_by_heal_path(tmp_path: Path) -> None:
    """正常 create 的项目重启后仍 live(自愈分支不碰健康路径)。"""
    coordinator = _new_coordinator(tmp_path)
    coordinator.create("healthy", build=_build("healthy", "v1"), validate=_validate)
    healed = _reinit(tmp_path)
    assert _state_of(healed, "healthy")["status"] == "live"
    healed._require_live("healthy")
