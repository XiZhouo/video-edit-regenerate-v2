"""W05 artifact completion receipt 强回归。"""

from __future__ import annotations

import hashlib
import errno
import json
import os
from pathlib import Path

import pytest

import artifact_scan as scan
from stage import StageSpec
from stage_plan import StagePlan
from work_list import WorkItem


def _item(*paths: str) -> WorkItem:
    return WorkItem(item_id="i1", entity_type="item", entity_id="e1", artifacts=tuple(paths))


def test_naked_artifact_empty_partial_and_legacy_marker_never_mean_done(tmp_path):
    item = _item("out/a.txt")
    final = tmp_path / "out/a.txt"
    final.parent.mkdir(parents=True)
    final.write_text("raw")

    assert not scan.item_done(item, tmp_path, "r1", "gen")
    marker = scan.item_marker_path(tmp_path, "r1", item.item_id)
    marker.parent.mkdir(parents=True)
    marker.write_text("")
    assert not scan.item_done(item, tmp_path, "r1", "gen")
    assert scan.scan_item(item, tmp_path, "r1", "gen") == "error"
    marker.write_text('{"schema":"ze.pipeline.done-receipt/v1"}')
    assert not scan.item_done(item, tmp_path, "r1", "gen")

    plan = StagePlan((StageSpec(id="gen", label="GEN", run=lambda _c, _a: {}),))
    scan.legacy_stage_marker_path(tmp_path, "r1", item.item_id, "gen").write_text("ok")
    assert scan.completed_stage_count(item, tmp_path, "r1", plan) == 0
    assert scan.scan_item_staged(item, tmp_path, "r1", plan) == "error"


def test_absent_receipt_is_pending_but_corrupt_receipt_is_repair_required(tmp_path):
    item = _item("out/a.txt")
    plan = StagePlan((StageSpec(id="gen", label="GEN", run=lambda _c, _a: {}),))
    assert scan.scan_item(item, tmp_path, "r1", "gen") == "pending"
    assert scan.scan_item_staged(item, tmp_path, "r1", plan) == "pending"

    marker = scan.stage_marker_path(tmp_path, "r1", "i1", "gen")
    marker.parent.mkdir(parents=True)
    marker.write_text("{broken")
    assert scan.scan_item_staged(item, tmp_path, "r1", plan) == "error"


def test_success_receipt_binds_checksum_size_identity_and_nonce(tmp_path):
    item = _item("out/a.txt")
    first = scan.begin_attempt(item, tmp_path, "r1", "gen", single_stage=True)
    second = scan.begin_attempt(item, tmp_path, "r1", "gen", single_stage=True)
    assert first.nonce != second.nonce
    assert first.staging_paths[0] != second.staging_paths[0]

    payload = b"committed artifact"
    first.staging_paths[0].write_bytes(payload)
    receipt = scan.finalize_attempt(
        first,
        commit_id="commit-1",
        commit_kind="document",
        operation_ids=("op-1",),
        require_all=True,
    )

    assert receipt["schema"] == scan.DONE_RECEIPT_SCHEMA
    assert receipt["version"] == 1
    assert receipt["commit_kind"] == "document"
    assert receipt["run_id"] == "r1" and receipt["item_id"] == "i1"
    assert receipt["stage_id"] == "gen"
    assert receipt["attempt_nonce"] == first.nonce
    assert receipt["operation_ids"] == ["op-1"]
    assert receipt["artifacts"] == [{
        "path": "out/a.txt",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "attempt_path": first.staging_paths[0].relative_to(tmp_path).as_posix(),
        "previous": None,
    }]
    assert scan.item_done(item, tmp_path, "r1", "gen")

    (tmp_path / "out/a.txt").write_text("tampered")
    assert not scan.item_done(item, tmp_path, "r1", "gen")


def test_artifact_only_receipt_is_explicit_and_has_no_operations(tmp_path):
    item = _item("out/a.txt")
    attempt = scan.begin_attempt(item, tmp_path, "r1", "gen", single_stage=True)
    attempt.staging_paths[0].write_text("x")
    receipt = scan.finalize_attempt(
        attempt,
        commit_id="artifact-only:abc",
        commit_kind="artifact_only",
        operation_ids=(),
        require_all=True,
    )
    assert receipt["commit_kind"] == "artifact_only"
    assert receipt["operation_ids"] == []

    other = scan.begin_attempt(_item(), tmp_path, "r2", "gen", single_stage=True)
    with pytest.raises(scan.ArtifactProtocolError):
        scan.finalize_attempt(
            other,
            commit_id="artifact-only:bad",
            commit_kind="artifact_only",
            operation_ids=("op-must-not-exist",),
            require_all=True,
        )


def test_committed_checkpoint_repairs_crash_before_done_marker(tmp_path, monkeypatch):
    item = _item("out/a.txt")
    attempt = scan.begin_attempt(item, tmp_path, "r1", "gen", single_stage=True)
    attempt.staging_paths[0].write_text("repair me")
    real_promote = scan._promote_checkpoint

    def crash_after_checkpoint(_data_dir, _checkpoint):
        raise OSError("simulated crash")

    monkeypatch.setattr(scan, "_promote_checkpoint", crash_after_checkpoint)
    with pytest.raises(OSError):
        scan.finalize_attempt(
            attempt,
            commit_id="commit-1",
            commit_kind="document",
            operation_ids=("op-1",),
            require_all=True,
        )
    assert scan.item_commit_path(tmp_path, "r1", "i1").exists()
    assert not scan.item_marker_path(tmp_path, "r1", "i1").exists()

    monkeypatch.setattr(scan, "_promote_checkpoint", real_promote)
    assert scan.repair_item_completion(item, tmp_path, "r1", "gen")
    assert scan.item_done(item, tmp_path, "r1", "gen")


def test_promote_rejects_existing_different_target(tmp_path):
    item = _item("out/a.txt")
    final = tmp_path / "out/a.txt"
    final.parent.mkdir(parents=True)
    final.write_text("original result")
    attempt = scan.begin_attempt(item, tmp_path, "r1", "gen", single_stage=True)
    attempt.staging_paths[0].write_text("this attempt")
    # begin 之后的另一个 writer 抢先改了 final；本 attempt 不能覆盖它。
    final.write_text("someone else's result")

    with pytest.raises(scan.ArtifactProtocolError) as exc_info:
        scan.finalize_attempt(
            attempt,
            commit_id="commit-1",
            commit_kind="document",
            operation_ids=("op-1",),
            require_all=True,
        )
    assert exc_info.value.code == "artifact_target_conflict"
    assert final.read_text() == "someone else's result"
    assert not scan.item_done(item, tmp_path, "r1", "gen")


def test_explicit_rerun_replaces_only_the_final_it_observed(tmp_path):
    item = _item("out/a.txt")
    first = scan.begin_attempt(item, tmp_path, "r1", "gen", single_stage=True)
    first.staging_paths[0].write_text("v1")
    scan.finalize_attempt(
        first, commit_id="c1", commit_kind="document",
        operation_ids=("o1",), require_all=True,
    )
    scan.item_marker_path(tmp_path, "r1", "i1").unlink()
    scan.item_commit_path(tmp_path, "r1", "i1").unlink()

    rerun = scan.begin_attempt(item, tmp_path, "r1", "gen", single_stage=True)
    rerun.staging_paths[0].write_text("v2")
    scan.finalize_attempt(
        rerun, commit_id="c2", commit_kind="document",
        operation_ids=("o2",), require_all=True,
    )

    assert (tmp_path / "out/a.txt").read_text() == "v2"
    assert scan.item_done(item, tmp_path, "r1", "gen")


def test_multistage_done_requires_valid_receipts_covering_all_artifacts(tmp_path):
    item = _item("out/a.txt", "out/b.txt")
    plan = StagePlan((
        StageSpec(id="a", label="A", run=lambda _c, _a: {}),
        StageSpec(id="b", label="B", run=lambda _c, _a: {}),
    ))
    attempt_a = scan.begin_attempt(item, tmp_path, "r1", "a", single_stage=False)
    attempt_a.staging_paths[0].write_text("a")
    scan.finalize_attempt(
        attempt_a, commit_id="ca", commit_kind="document",
        operation_ids=("oa",), require_all=False,
    )
    assert scan.completed_stage_count(item, tmp_path, "r1", plan) == 1
    assert not scan.stage_item_done(item, tmp_path, "r1", plan)

    # b receipt 存在但没有补齐 b.txt：前缀完整仍不能让 item 假 done。
    attempt_b0 = scan.begin_attempt(item, tmp_path, "r1", "b", single_stage=False)
    scan.finalize_attempt(
        attempt_b0, commit_id="cb0", commit_kind="artifact_only",
        operation_ids=(), require_all=False,
    )
    assert scan.completed_stage_count(item, tmp_path, "r1", plan) == 2
    assert not scan.stage_item_done(item, tmp_path, "r1", plan)

    attempt_b = scan.begin_attempt(item, tmp_path, "r1", "b", single_stage=False)
    attempt_b.staging_paths[1].write_text("b")
    scan.finalize_attempt(
        attempt_b, commit_id="cb", commit_kind="document",
        operation_ids=("ob",), require_all=False,
    )
    assert scan.stage_item_done(item, tmp_path, "r1", plan)
    assert scan.scan_item_staged(item, tmp_path, "r1", plan) == "done"


def test_receipt_path_outside_workitem_whitelist_is_rejected(tmp_path):
    item = _item("out/a.txt")
    final = tmp_path / "out/a.txt"
    final.parent.mkdir(parents=True)
    final.write_text("a")
    scan.mark_item_done(
        tmp_path, "r1", "i1", "gen", artifact_paths=("out/a.txt",),
    )
    marker = scan.item_marker_path(tmp_path, "r1", "i1")
    receipt = json.loads(marker.read_text())
    receipt["artifacts"][0]["path"] = "../escape.txt"
    marker.write_text(json.dumps(receipt))
    assert not scan.item_done(item, tmp_path, "r1", "gen")


def test_existing_parent_symlink_cannot_escape_data_dir(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "out").symlink_to(outside, target_is_directory=True)
    item = _item("out/a.txt")

    with pytest.raises(scan.ArtifactProtocolError) as exc_info:
        scan.begin_attempt(item, tmp_path, "r1", "gen", single_stage=True)
    assert exc_info.value.code == "artifact_path_invalid"
    assert not (outside / "a.txt").exists()


def test_existing_attempt_parent_symlink_cannot_escape_data_dir(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-attempt-outside"
    outside.mkdir()
    attempt_parent = tmp_path / ".pipelines/r1/attempts/i1"
    attempt_parent.mkdir(parents=True)
    (attempt_parent / "gen").symlink_to(outside, target_is_directory=True)

    with pytest.raises(scan.ArtifactProtocolError) as exc_info:
        scan.begin_attempt(_item(), tmp_path, "r1", "gen", single_stage=True)
    assert exc_info.value.code == "artifact_path_invalid"
    assert list(outside.iterdir()) == []


def test_repeated_scan_uses_bounded_fingerprint_cache_and_replace_invalidates(
    tmp_path, monkeypatch,
):
    item = _item("out/a.txt")
    final = tmp_path / "out/a.txt"
    final.parent.mkdir(parents=True)
    final.write_text("aaaa")
    scan.mark_item_done(tmp_path, "r1", "i1", "gen", artifact_paths=("out/a.txt",))
    scan._clear_fingerprint_cache_for_tests()

    reads = 0
    real_open = Path.open

    def counting_open(self, *args, **kwargs):
        nonlocal reads
        if self == final and args and args[0] == "rb":
            reads += 1
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)
    assert scan.item_done(item, tmp_path, "r1", "gen")
    assert scan.item_done(item, tmp_path, "r1", "gen")
    assert reads == 1

    replacement = tmp_path / "replacement.txt"
    replacement.write_text("bbbb")  # 同 size，新 inode；不能命中旧 digest。
    os.replace(replacement, final)
    assert not scan.item_done(item, tmp_path, "r1", "gen")
    assert reads == 2


def test_in_place_tamper_invalidates_fingerprint_cache(tmp_path, monkeypatch):
    item = _item("out/a.txt")
    final = tmp_path / "out/a.txt"
    final.parent.mkdir(parents=True)
    final.write_text("aaaa")
    scan.mark_item_done(tmp_path, "r1", "i1", "gen", artifact_paths=("out/a.txt",))
    scan._clear_fingerprint_cache_for_tests()

    reads = 0
    real_open = Path.open

    def counting_open(self, *args, **kwargs):
        nonlocal reads
        if self == final and args and args[0] == "rb":
            reads += 1
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)
    assert scan.item_done(item, tmp_path, "r1", "gen")
    final.write_text("zzzz")  # 同 inode/size，mtime_ns/ctime_ns 改变。
    assert not scan.item_done(item, tmp_path, "r1", "gen")
    assert reads == 2


def test_fingerprint_recomputes_when_file_changes_during_read(tmp_path, monkeypatch):
    final = tmp_path / "artifact.bin"
    final.write_bytes(b"aaaa")
    scan._clear_fingerprint_cache_for_tests()
    real_open = Path.open
    binary_reads = 0
    mutate_once = True

    class _MutatingReader:
        def __init__(self, fh):
            self._fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self._fh.__exit__(*args)

        def read(self, size=-1):
            nonlocal mutate_once
            value = self._fh.read(size)
            if mutate_once:
                mutate_once = False
                with real_open(final, "wb") as writer:
                    writer.write(b"bbbb")
            return value

    def mutating_open(self, *args, **kwargs):
        nonlocal binary_reads
        fh = real_open(self, *args, **kwargs)
        if self == final and args and args[0] == "rb":
            binary_reads += 1
            return _MutatingReader(fh)
        return fh

    monkeypatch.setattr(Path, "open", mutating_open)
    expected = (hashlib.sha256(b"bbbb").hexdigest(), 4)
    assert scan._fingerprint(final) == expected
    assert binary_reads == 2  # 变化中的第一轮不缓存，第二轮真实重读。
    assert scan._fingerprint(final) == expected
    assert binary_reads == 2  # 稳定结果才进入 cache。


def test_real_directory_fsync_error_is_not_reported_as_success(tmp_path, monkeypatch):
    target = tmp_path / "status/receipt.json"

    def fail(_path):
        raise OSError(errno.EIO, "injected directory fsync")

    monkeypatch.setattr(scan, "_fsync_directory", fail)
    with pytest.raises(scan.ArtifactProtocolError) as caught:
        scan.atomic_write_json(target, {"state": "done"})
    assert caught.value.code == "artifact_durability_unknown"
    # replace already happened; caller must reconcile instead of blindly retrying.
    assert json.loads(target.read_text()) == {"state": "done"}
