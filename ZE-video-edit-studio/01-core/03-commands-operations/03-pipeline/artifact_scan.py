"""artifact_scan — receipt 驱动的 pipeline 完成相位。

最终文件存在只是“有一些字节”，不是业务完成。W05 起，done 必须由一个可校验的
结构化 receipt 证明：Document commit 已成功、哪些 Operation 属于它，以及最终产物
的 sha256/size。Stage 原始输出写 attempt 目录；commit 成功后才原子 promote 到最终
路径并写 receipt。完全没有 completion 证据时，孤立最终文件仍保守地视为 pending；
一旦存在旧版 ``*.done`` 文本、空/半截/损坏 receipt，则说明有一个无法证明的完成意图，
必须 fail closed 为 repair-required，不能当作安全可重放的 pending。

这是单进程文件存储的兼容层，不是 W06 的数据库 scheduler / durable outbox。
"""

from __future__ import annotations

import hashlib
import errno
import json
import os
import stat
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from work_list import WorkItem, WorkList
    from stage_plan import StagePlan

PIPELINES_DIR = ".pipelines"
DONE_RECEIPT_SCHEMA = "ze.pipeline.done-receipt/v1"
DONE_RECEIPT_VERSION = 1
_COUNTER_LOCK = threading.Lock()
_FINGERPRINT_CACHE_LIMIT = 4096
_FINGERPRINT_CACHE: "OrderedDict[tuple[Any, ...], str]" = OrderedDict()
_FINGERPRINT_CACHE_LOCK = threading.Lock()
_UNSUPPORTED_DIR_FSYNC = frozenset(filter(None, (
    errno.EINVAL,
    errno.EBADF,
    getattr(errno, "ENOTSUP", None),
    getattr(errno, "EOPNOTSUPP", None),
)))


class ArtifactProtocolError(RuntimeError):
    """稳定的 artifact completion 错误，供 supervisor 落结构化 error marker。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ArtifactAttempt:
    """一次 stage 尝试的 staging 路径与最终路径绑定。"""

    data_dir: Path
    run_id: str
    item_id: str
    stage_id: str
    number: int
    nonce: str
    final_paths: tuple[str, ...]
    staging_paths: tuple[Path, ...]
    previous_fingerprints: tuple[tuple[str, int] | None, ...]
    single_stage: bool


def _safe_component(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in (".", "..")
        or any(char in value for char in ("/", "\\", "\x00"))
    ):
        raise ArtifactProtocolError("artifact_path_invalid", f"unsafe {label}: {value!r}")
    return value


def _contained_path(data_dir: Path, path: Path | str) -> Path:
    """文本白名单之外再做 resolved containment，阻断已有父目录 symlink 逃逸。"""
    base = Path(data_dir).resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(base)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ArtifactProtocolError(
            "artifact_path_invalid", f"artifact path escapes data_dir: {candidate}",
        ) from exc
    return candidate


def run_dir(data_dir: Path, run_id: str) -> Path:
    return _contained_path(
        Path(data_dir), Path(PIPELINES_DIR) / _safe_component(run_id, "run_id"),
    )


def manifest_path(data_dir: Path, run_id: str) -> Path:
    return _contained_path(Path(data_dir), run_dir(data_dir, run_id) / "run.json")


def error_marker_path(data_dir: Path, run_id: str, item_id: str) -> Path:
    return _contained_path(Path(data_dir), run_dir(data_dir, run_id) / "status" / f"{_safe_component(item_id, 'item_id')}.error.json")


def item_marker_path(data_dir: Path, run_id: str, item_id: str) -> Path:
    return _contained_path(Path(data_dir), run_dir(data_dir, run_id) / "status" / f"{_safe_component(item_id, 'item_id')}.done.json")


def item_commit_path(data_dir: Path, run_id: str, item_id: str) -> Path:
    return _contained_path(Path(data_dir), run_dir(data_dir, run_id) / "status" / f"{_safe_component(item_id, 'item_id')}.commit.json")


def stage_marker_path(data_dir: Path, run_id: str, item_id: str, stage_id: str) -> Path:
    item_id = _safe_component(item_id, "item_id")
    stage_id = _safe_component(stage_id, "stage_id")
    return _contained_path(Path(data_dir), run_dir(data_dir, run_id) / "status" / f"{item_id}.stage.{stage_id}.done.json")


def legacy_stage_marker_path(data_dir: Path, run_id: str, item_id: str, stage_id: str) -> Path:
    """W05 前的文本 marker 路径；只供迁移诊断，绝不证明 done。"""
    item_id = _safe_component(item_id, "item_id")
    stage_id = _safe_component(stage_id, "stage_id")
    return _contained_path(Path(data_dir), run_dir(data_dir, run_id) / "status" / f"{item_id}.stage.{stage_id}.done")


def stage_commit_path(data_dir: Path, run_id: str, item_id: str, stage_id: str) -> Path:
    item_id = _safe_component(item_id, "item_id")
    stage_id = _safe_component(stage_id, "stage_id")
    return _contained_path(Path(data_dir), run_dir(data_dir, run_id) / "status" / f"{item_id}.stage.{stage_id}.commit.json")


def stage_input_path(data_dir: Path, run_id: str, item_id: str, stage_id: str) -> Path:
    item_id = _safe_component(item_id, "item_id")
    stage_id = _safe_component(stage_id, "stage_id")
    return _contained_path(Path(data_dir), run_dir(data_dir, run_id) / "status" / f"{item_id}.stage.{stage_id}.input.json")


def stage_result_path(data_dir: Path, run_id: str, item_id: str, stage_id: str) -> Path:
    item_id = _safe_component(item_id, "item_id")
    stage_id = _safe_component(stage_id, "stage_id")
    return _contained_path(Path(data_dir), run_dir(data_dir, run_id) / "status" / f"{item_id}.stage.{stage_id}.result.json")


def stage_stale_marker_path(data_dir: Path, run_id: str, item_id: str, stage_id: str) -> Path:
    item_id = _safe_component(item_id, "item_id")
    stage_id = _safe_component(stage_id, "stage_id")
    return _contained_path(Path(data_dir), run_dir(data_dir, run_id) / "status" / f"{item_id}.stage.{stage_id}.stale")


def attempt_counter_path(data_dir: Path, run_id: str, item_id: str, stage_id: str) -> Path:
    item_id = _safe_component(item_id, "item_id")
    stage_id = _safe_component(stage_id, "stage_id")
    return _contained_path(Path(data_dir), run_dir(data_dir, run_id) / "status" / f"{item_id}.stage.{stage_id}.attempts")


def atomic_write_text(path: Path, value: str) -> None:
    """同目录临时文件 + fsync + replace；marker/counter 永不暴露半截内容。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as fh:
            fh.write(value)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
        try:
            _fsync_directory(path.parent)
        except OSError as exc:
            raise ArtifactProtocolError(
                "artifact_durability_unknown",
                "artifact metadata was replaced but directory fsync failed",
            ) from exc
    finally:
        if temp.exists():
            temp.unlink()


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _fsync_directory(path: Path) -> bool:
    """Sync a rename directory; only explicit unsupported errno may degrade."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        if exc.errno in _UNSUPPORTED_DIR_FSYNC:
            return False
        raise
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno in _UNSUPPORTED_DIR_FSYNC:
                return False
            raise
    finally:
        os.close(fd)
    return True


def read_json_file(path: Path) -> dict:
    try:
        if path.exists() and path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
    except Exception:
        return {}
    return {}


def _evidence_exists(path: Path) -> bool:
    """Existence/I/O uncertainty both mean replay must fail closed."""
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _safe_relative(path: str) -> str:
    if not isinstance(path, str) or not path or path == "." or "\\" in path or "\x00" in path:
        raise ArtifactProtocolError("artifact_path_invalid", f"unsafe artifact path: {path!r}")
    p = PurePosixPath(path)
    windows = PureWindowsPath(path)
    if p.is_absolute() or windows.is_absolute() or windows.drive or ".." in p.parts:
        raise ArtifactProtocolError("artifact_path_invalid", f"unsafe artifact path: {path!r}")
    return p.as_posix()


def _stat_fingerprint_key(path: Path, value: os.stat_result) -> tuple[Any, ...]:
    return (
        str(path.resolve(strict=False)),
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _clear_fingerprint_cache_for_tests() -> None:
    with _FINGERPRINT_CACHE_LOCK:
        _FINGERPRINT_CACHE.clear()


def _fingerprint(path: Path, *, use_cache: bool = True) -> tuple[str, int] | None:
    """真实 SHA-256 + bounded stat-keyed cache，避免 SSE 热循环反复读大文件。"""
    path = Path(path)
    for _attempt in range(2):
        try:
            before = path.stat()
            if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
                return None
            key = _stat_fingerprint_key(path, before)
            cached = None
            if use_cache:
                with _FINGERPRINT_CACHE_LOCK:
                    cached = _FINGERPRINT_CACHE.get(key)
                    if cached is not None:
                        _FINGERPRINT_CACHE.move_to_end(key)
            if cached is not None:
                # 命中后再 stat 一次，避免恰在 lookup 窗口被 replace/tamper。
                after = path.stat()
                if _stat_fingerprint_key(path, after) == key:
                    return cached, before.st_size
                continue

            digest = hashlib.sha256()
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(chunk)
            after = path.stat()
            if _stat_fingerprint_key(path, after) != key:
                # 读的过程中变了：本轮结果不缓存，重新基于新 stat 读一次。
                continue
            value = digest.hexdigest()
            if use_cache:
                with _FINGERPRINT_CACHE_LOCK:
                    _FINGERPRINT_CACHE[key] = value
                    _FINGERPRINT_CACHE.move_to_end(key)
                    while len(_FINGERPRINT_CACHE) > _FINGERPRINT_CACHE_LIMIT:
                        _FINGERPRINT_CACHE.popitem(last=False)
            return value, before.st_size
        except (OSError, RuntimeError):
            return None
    # 连续两次读取都在变化，fail closed；下次 scan 会再尝试。
    return None


def _next_attempt_number(data_dir: Path, run_id: str, item_id: str, stage_id: str) -> int:
    path = attempt_counter_path(data_dir, run_id, item_id, stage_id)
    with _COUNTER_LOCK:
        current = 0
        try:
            current = int(path.read_text(encoding="utf-8")) if path.exists() else 0
            if current < 0:
                raise ValueError("negative attempt counter")
        except (OSError, ValueError):
            # 坏 counter 不能覆盖旧 attempt；从目录中保守恢复最大序号。
            base = _contained_path(
                Path(data_dir), run_dir(data_dir, run_id) / "attempts" / item_id / stage_id,
            )
            if base.exists():
                current = max((
                    int(p.name.split("-", 1)[0])
                    for p in base.iterdir()
                    if p.is_dir() and p.name.split("-", 1)[0].isdigit()
                ), default=0)
        number = current + 1
        atomic_write_text(path, str(number))
        return number


def begin_attempt(
    item: "WorkItem",
    data_dir: Path,
    run_id: str,
    stage_id: str,
    *,
    single_stage: bool,
) -> ArtifactAttempt:
    """分配一次持久 attempt；stage 只能拿到 staging 路径。"""
    data_dir = Path(data_dir).resolve()
    number = _next_attempt_number(data_dir, run_id, item.item_id, stage_id)
    # number 只给人看/给 demo 做 flaky 次数；目录身份由不可复用 nonce 决定，
    # 不把正确性押在 read-modify-write counter 上（W06 多进程前也不会撞路径）。
    nonce = uuid.uuid4().hex
    root = (
        run_dir(data_dir, run_id) / "attempts" / item.item_id / stage_id
        / f"{number}-{nonce}"
    )
    root = _contained_path(Path(data_dir), root)
    finals = tuple(_safe_relative(path) for path in item.artifacts)
    resolved_finals: set[str] = set()
    for final in finals:
        resolved = str(_contained_path(Path(data_dir), final).resolve(strict=False))
        if resolved in resolved_finals:
            raise ArtifactProtocolError(
                "artifact_path_invalid", "artifact paths resolve to the same final target",
            )
        resolved_finals.add(resolved)
    root.mkdir(parents=True, exist_ok=True)
    staging = tuple(
        # 保留最终 basename/suffix，避免图片/视频 stage 因临时扩展名而选错编码器。
        root / f"{index:04d}-{PurePosixPath(rel).name}"
        for index, rel in enumerate(finals)
    )
    for path in staging:
        path.parent.mkdir(parents=True, exist_ok=True)
    previous = tuple(
        _fingerprint(_contained_path(data_dir, rel), use_cache=False)
        for rel in finals
    )
    return ArtifactAttempt(
        data_dir=data_dir, run_id=run_id, item_id=item.item_id,
        stage_id=stage_id, number=number, nonce=nonce, final_paths=finals,
        staging_paths=staging, previous_fingerprints=previous,
        single_stage=single_stage,
    )


def _receipt_paths(attempt: ArtifactAttempt) -> tuple[Path, Path]:
    if attempt.single_stage:
        return (
            item_commit_path(attempt.data_dir, attempt.run_id, attempt.item_id),
            item_marker_path(attempt.data_dir, attempt.run_id, attempt.item_id),
        )
    return (
        stage_commit_path(attempt.data_dir, attempt.run_id, attempt.item_id, attempt.stage_id),
        stage_marker_path(attempt.data_dir, attempt.run_id, attempt.item_id, attempt.stage_id),
    )


def _operation_ids(values: Iterable[str]) -> list[str]:
    out = list(values)
    if (
        any(not isinstance(value, str) or not value for value in out)
        or len(set(out)) != len(out)
    ):
        raise ArtifactProtocolError("artifact_receipt_invalid", "operation_ids must be unique strings")
    return out


def finalize_attempt(
    attempt: ArtifactAttempt,
    *,
    commit_id: str,
    commit_kind: str,
    operation_ids: Iterable[str],
    require_all: bool,
) -> dict[str, Any]:
    """commit 后 checkpoint → atomic promote → done receipt。

    多 stage 可只产出 WorkItem.artifacts 的一个子集；最终 item done 时会检查全部
    stage receipt 的 union。单 stage 必须一次覆盖全部声明产物。
    """
    if not isinstance(commit_id, str) or not commit_id:
        raise ArtifactProtocolError("artifact_receipt_invalid", "commit_id must not be empty")
    if commit_kind not in ("document", "artifact_only"):
        raise ArtifactProtocolError("artifact_receipt_invalid", "invalid commit_kind")
    artifacts: list[dict[str, Any]] = []
    missing: list[str] = []
    data_dir = attempt.data_dir
    for rel, staged, previous in zip(
        attempt.final_paths, attempt.staging_paths, attempt.previous_fingerprints,
    ):
        _contained_path(data_dir, staged)
        _contained_path(data_dir, rel)
        fp = _fingerprint(staged)
        if fp is None:
            missing.append(rel)
            continue
        sha256, size = fp
        artifacts.append({
            "path": rel,
            "sha256": sha256,
            "size": size,
            "attempt_path": staged.relative_to(data_dir).as_posix(),
            "previous": (
                {"sha256": previous[0], "size": previous[1]}
                if previous is not None else None
            ),
        })
    if require_all and missing:
        raise ArtifactProtocolError(
            "artifact_output_missing",
            f"stage did not produce declared artifact(s): {', '.join(missing)}",
        )

    now = time.time()
    normalized_operation_ids = _operation_ids(operation_ids)
    if commit_kind == "artifact_only" and normalized_operation_ids:
        raise ArtifactProtocolError(
            "artifact_receipt_invalid", "artifact_only receipt cannot contain operation_ids",
        )
    if commit_kind == "document" and not normalized_operation_ids:
        raise ArtifactProtocolError(
            "artifact_receipt_invalid", "document receipt requires operation_ids",
        )
    checkpoint = {
        "schema": DONE_RECEIPT_SCHEMA,
        "version": DONE_RECEIPT_VERSION,
        "state": "committed",
        "run_id": attempt.run_id,
        "item_id": attempt.item_id,
        "stage_id": attempt.stage_id,
        "attempt": attempt.number,
        "attempt_nonce": attempt.nonce,
        "commit_id": commit_id,
        "commit_kind": commit_kind,
        "operation_ids": normalized_operation_ids,
        "artifacts": artifacts,
        "committed_at": now,
    }
    commit_path, done_path = _receipt_paths(attempt)
    # 这是 crash recovery 的关键边界：checkpoint 只会在业务 commit 返回 committed 后出现。
    atomic_write_json(commit_path, checkpoint)
    _promote_checkpoint(data_dir, checkpoint)
    done = {**checkpoint, "state": "done", "completed_at": time.time()}
    atomic_write_json(done_path, done)
    return done


def _valid_artifact_entry(entry: Any, expected: set[str]) -> tuple[str, str, int] | None:
    if not isinstance(entry, dict):
        return None
    path = entry.get("path")
    sha256 = entry.get("sha256")
    size = entry.get("size")
    if (
        not isinstance(path, str) or path not in expected
        or not isinstance(sha256, str) or len(sha256) != 64
        or any(ch not in "0123456789abcdef" for ch in sha256)
        or not isinstance(size, int) or isinstance(size, bool) or size <= 0
    ):
        return None
    return path, sha256, size


def _valid_previous_fingerprint(entry: dict[str, Any]) -> tuple[str, int] | None | bool:
    """Return prior fingerprint, None for absent, False for malformed."""
    if "previous" not in entry:
        return False
    previous = entry.get("previous")
    if previous is None:
        return None
    if not isinstance(previous, dict) or set(previous) != {"sha256", "size"}:
        return False
    sha256 = previous.get("sha256")
    size = previous.get("size")
    if (
        not isinstance(sha256, str) or len(sha256) != 64
        or any(ch not in "0123456789abcdef" for ch in sha256)
        or not isinstance(size, int) or isinstance(size, bool) or size <= 0
    ):
        return False
    return sha256, size


def _valid_receipt_header(
    receipt: dict[str, Any], *, run_id: str, item_id: str, stage_id: str | None,
) -> bool:
    operation_ids = receipt.get("operation_ids")
    commit_kind = receipt.get("commit_kind")
    return bool(
        receipt.get("schema") == DONE_RECEIPT_SCHEMA
        and receipt.get("version") == DONE_RECEIPT_VERSION
        and receipt.get("state") == "done"
        and receipt.get("run_id") == run_id
        and receipt.get("item_id") == item_id
        and isinstance(receipt.get("stage_id"), str) and receipt.get("stage_id")
        and (stage_id is None or receipt.get("stage_id") == stage_id)
        and isinstance(receipt.get("attempt"), int)
        and not isinstance(receipt.get("attempt"), bool)
        and receipt.get("attempt") > 0
        and isinstance(receipt.get("attempt_nonce"), str)
        and bool(receipt.get("attempt_nonce"))
        and isinstance(receipt.get("commit_id"), str) and receipt.get("commit_id")
        and commit_kind in ("document", "artifact_only")
        and isinstance(operation_ids, list)
        and all(isinstance(value, str) and value for value in operation_ids)
        and len(set(operation_ids)) == len(operation_ids)
        and (commit_kind != "artifact_only" or not operation_ids)
        and (commit_kind != "document" or bool(operation_ids))
        and isinstance(receipt.get("artifacts"), list)
    )


def _valid_checkpoint_header(
    checkpoint: dict[str, Any], *, run_id: str, item_id: str, stage_id: str,
) -> bool:
    operation_ids = checkpoint.get("operation_ids")
    commit_kind = checkpoint.get("commit_kind")
    return bool(
        checkpoint.get("schema") == DONE_RECEIPT_SCHEMA
        and checkpoint.get("version") == DONE_RECEIPT_VERSION
        and checkpoint.get("state") == "committed"
        and checkpoint.get("run_id") == run_id
        and checkpoint.get("item_id") == item_id
        and checkpoint.get("stage_id") == stage_id
        and isinstance(checkpoint.get("attempt"), int)
        and not isinstance(checkpoint.get("attempt"), bool)
        and checkpoint.get("attempt") > 0
        and isinstance(checkpoint.get("attempt_nonce"), str)
        and bool(checkpoint.get("attempt_nonce"))
        and isinstance(checkpoint.get("commit_id"), str) and checkpoint.get("commit_id")
        and commit_kind in ("document", "artifact_only")
        and isinstance(operation_ids, list)
        and all(isinstance(value, str) and value for value in operation_ids)
        and len(set(operation_ids)) == len(operation_ids)
        and (commit_kind != "artifact_only" or not operation_ids)
        and (commit_kind != "document" or bool(operation_ids))
        and isinstance(checkpoint.get("artifacts"), list)
    )


def _validated_receipt_artifacts(
    marker: Path,
    item: "WorkItem",
    data_dir: Path,
    run_id: str,
    *,
    stage_id: str | None,
    require_all: bool,
) -> set[str] | None:
    receipt = read_json_file(marker)
    if not _valid_receipt_header(receipt, run_id=run_id, item_id=item.item_id, stage_id=stage_id):
        return None
    expected = {_safe_relative(rel) for rel in item.artifacts}
    seen: set[str] = set()
    for entry in receipt["artifacts"]:
        valid = _valid_artifact_entry(entry, expected)
        if valid is None:
            return None
        rel, sha256, size = valid
        if rel in seen:
            return None
        seen.add(rel)
        actual = _fingerprint(_contained_path(Path(data_dir), rel))
        if actual != (sha256, size):
            return None
    if require_all and seen != expected:
        return None
    return seen


def _promote_checkpoint(data_dir: Path, checkpoint: dict[str, Any]) -> None:
    """幂等完成 checkpoint 中的 promote；已有正确 final 可直接通过。"""
    data_dir = Path(data_dir)
    expected = {
        str(entry.get("path")) for entry in checkpoint.get("artifacts", [])
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }
    for entry in checkpoint.get("artifacts", []):
        valid = _valid_artifact_entry(entry, expected)
        if valid is None:
            raise ArtifactProtocolError("artifact_checkpoint_invalid", "invalid artifact checkpoint")
        rel, sha256, size = valid
        final = _contained_path(data_dir, rel)
        final_fingerprint = _fingerprint(final)
        if final_fingerprint == (sha256, size):
            continue
        previous = _valid_previous_fingerprint(entry)
        if previous is False:
            raise ArtifactProtocolError(
                "artifact_checkpoint_invalid", "previous artifact fingerprint is invalid",
            )
        # Rerun is a compare-and-swap, not an unconditional overwrite.  The
        # final may be replaced only while it still equals the bytes observed
        # when this attempt began.  A concurrent winner/tamper is a conflict.
        if final_fingerprint != previous:
            raise ArtifactProtocolError(
                "artifact_target_conflict",
                f"final artifact changed after attempt began: {rel}",
            )
        attempt_rel = entry.get("attempt_path")
        if not isinstance(attempt_rel, str):
            raise ArtifactProtocolError("artifact_checkpoint_invalid", "attempt_path missing")
        attempt_rel = _safe_relative(attempt_rel)
        prefix = (
            PurePosixPath(PIPELINES_DIR)
            / str(checkpoint.get("run_id"))
            / "attempts"
            / str(checkpoint.get("item_id"))
            / str(checkpoint.get("stage_id"))
            / f"{checkpoint.get('attempt')}-{checkpoint.get('attempt_nonce')}"
        )
        if PurePosixPath(attempt_rel).parts[:len(prefix.parts)] != prefix.parts:
            raise ArtifactProtocolError("artifact_checkpoint_invalid", "attempt_path outside run")
        staged = _contained_path(data_dir, attempt_rel)
        # promote 边界必须真实重读 staging，不能仅相信刚才的 stat-keyed cache。
        if _fingerprint(staged, use_cache=False) != (sha256, size):
            raise ArtifactProtocolError(
                "artifact_completion_indeterminate",
                f"neither final nor staged artifact matches receipt: {rel}",
            )
        final.parent.mkdir(parents=True, exist_ok=True)
        # stage 可能用普通 write()；promote 前强制把已核对的源内容刷到盘。
        try:
            with staged.open("rb") as fh:
                os.fsync(fh.fileno())
        except OSError as exc:
            raise ArtifactProtocolError(
                "artifact_staging_fsync_failed", f"cannot fsync staged artifact: {rel}",
            ) from exc
        os.replace(staged, final)
        try:
            _fsync_directory(final.parent)
        except OSError as exc:
            raise ArtifactProtocolError(
                "artifact_durability_unknown",
                f"artifact was promoted but directory fsync failed: {rel}",
            ) from exc


def _repair_completion(
    item: "WorkItem", data_dir: Path, run_id: str, stage_id: str, *, single_stage: bool,
) -> bool:
    done_path = (
        item_marker_path(data_dir, run_id, item.item_id) if single_stage
        else stage_marker_path(data_dir, run_id, item.item_id, stage_id)
    )
    require_all = single_stage
    if _validated_receipt_artifacts(
        done_path, item, data_dir, run_id, stage_id=stage_id, require_all=require_all,
    ) is not None:
        return True
    commit_path = (
        item_commit_path(data_dir, run_id, item.item_id) if single_stage
        else stage_commit_path(data_dir, run_id, item.item_id, stage_id)
    )
    checkpoint = read_json_file(commit_path)
    if not _valid_checkpoint_header(
        checkpoint, run_id=run_id, item_id=item.item_id, stage_id=stage_id,
    ):
        return False
    expected = {_safe_relative(rel) for rel in item.artifacts}
    covered: set[str] = set()
    for entry in checkpoint["artifacts"]:
        valid = _valid_artifact_entry(entry, expected)
        if valid is None or valid[0] in covered:
            return False
        covered.add(valid[0])
    if require_all and covered != expected:
        return False
    try:
        _promote_checkpoint(Path(data_dir), checkpoint)
        done = {**checkpoint, "state": "done", "completed_at": time.time()}
        atomic_write_json(done_path, done)
    except (ArtifactProtocolError, OSError):
        return False
    return _validated_receipt_artifacts(
        done_path, item, data_dir, run_id, stage_id=stage_id, require_all=require_all,
    ) is not None


def repair_item_completion(
    item: "WorkItem", data_dir: Path, run_id: str, stage_id: str,
) -> bool:
    return _repair_completion(item, data_dir, run_id, stage_id, single_stage=True)


def repair_stage_completion(
    item: "WorkItem", data_dir: Path, run_id: str, stage_id: str,
) -> bool:
    return _repair_completion(item, data_dir, run_id, stage_id, single_stage=False)


def mark_stage_done(
    data_dir: Path,
    run_id: str,
    item_id: str,
    stage_id: str,
    *,
    artifact_paths: Iterable[str] = (),
    commit_id: str = "test-file-adapter",
    commit_kind: str = "artifact_only",
    operation_ids: Iterable[str] = (),
) -> None:
    """测试/迁移夹具：写完整 v1 receipt；生产路径必须走 finalize_attempt。"""
    artifacts: list[dict[str, Any]] = []
    for rel_value in artifact_paths:
        rel = _safe_relative(rel_value)
        fp = _fingerprint(_contained_path(Path(data_dir), rel))
        if fp is None:
            raise ArtifactProtocolError("artifact_output_missing", f"artifact missing: {rel}")
        sha256, size = fp
        artifacts.append({"path": rel, "sha256": sha256, "size": size})
    atomic_write_json(stage_marker_path(data_dir, run_id, item_id, stage_id), {
        "schema": DONE_RECEIPT_SCHEMA,
        "version": DONE_RECEIPT_VERSION,
        "state": "done",
        "run_id": run_id,
        "item_id": item_id,
        "stage_id": stage_id,
        "attempt": 1,
        "attempt_nonce": uuid.uuid4().hex,
        "commit_id": commit_id,
        "commit_kind": commit_kind,
        "operation_ids": _operation_ids(operation_ids),
        "artifacts": artifacts,
        "completed_at": time.time(),
    })


def mark_item_done(
    data_dir: Path,
    run_id: str,
    item_id: str,
    stage_id: str,
    *,
    artifact_paths: Iterable[str] = (),
    commit_id: str = "test-file-adapter",
    commit_kind: str = "artifact_only",
    operation_ids: Iterable[str] = (),
) -> None:
    """测试/迁移夹具：写单 stage 的完整 v1 item receipt。"""
    artifacts: list[dict[str, Any]] = []
    for rel_value in artifact_paths:
        rel = _safe_relative(rel_value)
        fp = _fingerprint(_contained_path(Path(data_dir), rel))
        if fp is None:
            raise ArtifactProtocolError("artifact_output_missing", f"artifact missing: {rel}")
        sha256, size = fp
        artifacts.append({"path": rel, "sha256": sha256, "size": size})
    atomic_write_json(item_marker_path(data_dir, run_id, item_id), {
        "schema": DONE_RECEIPT_SCHEMA,
        "version": DONE_RECEIPT_VERSION,
        "state": "done",
        "run_id": run_id,
        "item_id": item_id,
        "stage_id": stage_id,
        "attempt": 1,
        "attempt_nonce": uuid.uuid4().hex,
        "commit_id": commit_id,
        "commit_kind": commit_kind,
        "operation_ids": _operation_ids(operation_ids),
        "artifacts": artifacts,
        "completed_at": time.time(),
    })


def mark_stage_stale(data_dir: Path, run_id: str, item_id: str, stage_id: str) -> None:
    atomic_write_text(stage_stale_marker_path(data_dir, run_id, item_id, stage_id), "stale")


def clear_stage_stale(data_dir: Path, run_id: str, item_id: str, stage_id: str) -> None:
    path = stage_stale_marker_path(data_dir, run_id, item_id, stage_id)
    if path.exists():
        path.unlink()


def clear_stage_markers(data_dir: Path, run_id: str, item_id: str, plan: "StagePlan") -> None:
    for stage_id in plan.stage_ids:
        for path in (
            stage_marker_path(data_dir, run_id, item_id, stage_id),
            stage_commit_path(data_dir, run_id, item_id, stage_id),
        ):
            if path.exists():
                path.unlink()
        clear_stage_stale(data_dir, run_id, item_id, stage_id)


def clear_stage_markers_from(
    data_dir: Path, run_id: str, item_id: str, plan: "StagePlan", stage_id: str,
) -> None:
    try:
        start = plan.index_of(stage_id)
    except ValueError:
        return
    for current in plan.stage_ids[start:]:
        for path in (
            stage_marker_path(data_dir, run_id, item_id, current),
            stage_commit_path(data_dir, run_id, item_id, current),
        ):
            if path.exists():
                path.unlink()
        mark_stage_stale(data_dir, run_id, item_id, current)


def completed_stage_count(item: "WorkItem", data_dir: Path, run_id: str, plan: "StagePlan") -> int:
    """只数连续且 receipt+checksum 均有效的 stage 前缀。"""
    count = 0
    for stage_id in plan.stage_ids:
        artifacts = _validated_receipt_artifacts(
            stage_marker_path(data_dir, run_id, item.item_id, stage_id),
            item, data_dir, run_id, stage_id=stage_id, require_all=False,
        )
        if artifacts is None:
            break
        count += 1
    return count


def stage_item_done(item: "WorkItem", data_dir: Path, run_id: str, plan: "StagePlan") -> bool:
    if completed_stage_count(item, data_dir, run_id, plan) < len(plan):
        return False
    covered: set[str] = set()
    for stage_id in plan.stage_ids:
        artifacts = _validated_receipt_artifacts(
            stage_marker_path(data_dir, run_id, item.item_id, stage_id),
            item, data_dir, run_id, stage_id=stage_id, require_all=False,
        )
        if artifacts is None:
            return False
        covered.update(artifacts)
    return covered == {_safe_relative(rel) for rel in item.artifacts}


def scan_item_staged(item: "WorkItem", data_dir: Path, run_id: str, plan: "StagePlan") -> str:
    if error_marker_path(data_dir, run_id, item.item_id).exists():
        return "error"
    if stage_item_done(item, data_dir, run_id, plan):
        return "done"
    gap = False
    for stage_id in plan.stage_ids:
        marker = stage_marker_path(data_dir, run_id, item.item_id, stage_id)
        valid = _validated_receipt_artifacts(
            marker, item, data_dir, run_id, stage_id=stage_id, require_all=False,
        )
        evidence = any(_evidence_exists(path) for path in (
            marker,
            stage_commit_path(data_dir, run_id, item.item_id, stage_id),
            legacy_stage_marker_path(data_dir, run_id, item.item_id, stage_id),
        ))
        if valid is None:
            if evidence:
                return "error"
            gap = True
        elif gap:
            # A later committed stage without its predecessor is not a valid
            # replay boundary, even if that later receipt is well formed.
            return "error"
    return "pending"


def item_done(
    item: "WorkItem", data_dir: Path, run_id: str | None = None, stage_id: str | None = None,
) -> bool:
    """单 stage done 只认结构化 receipt；省略 run_id 时保守返回 False。"""
    if not run_id:
        return False
    return _validated_receipt_artifacts(
        item_marker_path(data_dir, run_id, item.item_id),
        item, data_dir, run_id, stage_id=stage_id, require_all=True,
    ) is not None


def scan_item(
    item: "WorkItem", data_dir: Path, run_id: str, stage_id: str | None = None,
) -> str:
    if error_marker_path(data_dir, run_id, item.item_id).exists():
        return "error"
    if item_done(item, data_dir, run_id, stage_id):
        return "done"
    evidence = any(_evidence_exists(path) for path in (
        item_marker_path(data_dir, run_id, item.item_id),
        item_commit_path(data_dir, run_id, item.item_id),
    ))
    if stage_id:
        evidence = evidence or _evidence_exists(
            legacy_stage_marker_path(data_dir, run_id, item.item_id, stage_id),
        )
    if evidence:
        return "error"
    return "pending"


def scan_progress(work_list: "WorkList", data_dir: Path, stage_id: str | None = None) -> float:
    items = work_list.items
    if not items:
        return 1.0
    done = sum(
        1 for item in items
        if item_done(item, data_dir, work_list.run_id, stage_id)
    )
    return done / len(items)


def eta_throughput(done: int, pending: int, elapsed_sec: float) -> tuple[float | None, float | None]:
    if done <= 0 or elapsed_sec <= 0:
        return None, None
    throughput = done / elapsed_sec
    eta = 0.0 if pending <= 0 else pending / throughput
    return eta, throughput


def stage_summary(work_list: "WorkList", data_dir: Path, plan: "StagePlan") -> list[dict]:
    total = len(work_list.items)
    counts = [0] * len(plan)
    for item in work_list.items:
        completed = completed_stage_count(item, data_dir, work_list.run_id, plan)
        for index in range(completed):
            counts[index] += 1
    return [
        {"stage_id": stage.id, "label": stage.label, "done": counts[index], "total": total}
        for index, stage in enumerate(plan.stages)
    ]


def scan_counts(
    work_list: "WorkList", data_dir: Path, stage_id: str | None = None,
) -> dict[str, int]:
    done = error = 0
    for item in work_list.items:
        phase = scan_item(item, data_dir, work_list.run_id, stage_id)
        if phase == "done":
            done += 1
        elif phase == "error":
            error += 1
    total = len(work_list.items)
    return {"done": done, "error": error, "pending": total - done - error, "total": total}
