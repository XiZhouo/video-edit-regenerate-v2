"""Upstream video-task persistence + restart recovery (pure model).

The video service is asynchronous: submit → poll → download. Across a process
restart the in-memory poll workers vanish, so the durable artifact is a
write-through task record. Recovery is lazy/pull-driven (seedance-site-v2 model):
task JSON survives on disk; ``scan_resumable`` finds the non-terminal ones and
the hosting skill re-spawns a worker (idempotently) for each.

This module is pure model + IO over the atomic writer — no threads, no HTTP. The
skill owns the worker threads and the HTTP client; it calls these helpers to
persist and to decide what to resume.
"""

from __future__ import annotations

import os
import time
from typing import Any

from . import atomic

TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")
DEFAULT_RETENTION_SECONDS = 30 * 24 * 60 * 60  # 30 days (project config may override)


def task_path(cache_dir: str, task_key: str) -> str:
    safe = "".join(c for c in str(task_key) if c.isalnum() or c in "-_.") or "task"
    return os.path.join(cache_dir, f"{safe}.json")


def save_task(cache_dir: str, task: dict[str, Any], *, now_ms: int) -> dict[str, Any]:
    """Write-through persist a task record atomically; stamps updatedAt."""
    record = {**task, "updatedAt": now_ms}
    atomic.write_json_atomic(task_path(cache_dir, task["taskKey"]), record)
    return record


def load_task(cache_dir: str, task_key: str) -> dict[str, Any] | None:
    return atomic.read_json(task_path(cache_dir, task_key), default=None)


def is_terminal(status: Any) -> bool:
    return status in TERMINAL_STATUSES


def scan_resumable(cache_dir: str) -> list[dict[str, Any]]:
    """Return non-terminal, not-yet-downloaded task records to resume polling.

    Called at startup by the hosting skill; each returned record gets a
    re-spawned worker. A downloaded task (has ``localRef``) is skipped even if
    its status looks non-terminal.
    """
    if not os.path.isdir(cache_dir):
        return []
    out: list[dict[str, Any]] = []
    for name in os.listdir(cache_dir):
        if not name.endswith(".json"):
            continue
        record = atomic.read_json(os.path.join(cache_dir, name), default=None)
        if not isinstance(record, dict):
            continue
        if record.get("localRef"):
            continue
        if is_terminal(record.get("status")):
            continue
        out.append(record)
    return out


def cleanup_old_tasks(cache_dir: str, *, now: float,
                      retention_seconds: int = DEFAULT_RETENTION_SECONDS) -> list[str]:
    """Remove task JSONs older than the retention window (by mtime). Returns removed keys.

    Wired to a startup/periodic call by the hosting skill — seedance-site-v2 left
    this ready-but-dormant; here it is live. Adopted candidates' finished videos
    live as content-addressed assets and are never touched by this.
    """
    if not os.path.isdir(cache_dir):
        return []
    cutoff = now - retention_seconds
    removed: list[str] = []
    for name in os.listdir(cache_dir):
        if not name.endswith(".json"):
            continue
        path = os.path.join(cache_dir, name)
        try:
            if os.stat(path).st_mtime < cutoff:
                os.remove(path)
                removed.append(name[:-5])
        except OSError:
            pass
    return removed


def now_ms() -> int:
    return int(time.time() * 1000)
