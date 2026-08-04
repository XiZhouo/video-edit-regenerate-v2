#!/usr/bin/env python3
"""sync_domain.py — distribute the shared video domain into consumer skills.

This is the cross-skill analog of build-v3's ``scripts/skill_sync.py``. It copies
this repo's ``src/ze_video_domain/`` package into a consumer skill's managed
domain slot ``02-mod/05-code/_domain/`` and records a version+checksum lock so
drift and upgrades are detectable.

Discipline (mirrors ZE "受管层必回流上游" hard rule):
- The source repo is the single source of truth. A consumer never hand-edits its
  ``_domain/`` copy; any change must land in this repo first, then re-sync.
- ``--verify`` detects local drift (a consumer's copy edited out of band) — the
  fix is to reflow the change upstream, not to re-sync over it.
- Before an actual sync wave across all three quartet skills, run each skill's
  ``verify_all`` (the sync must not clobber downstream value).

Usage:
  sync_domain.py --list
  sync_domain.py --sync   /path/to/ZE-video-edit-studio
  sync_domain.py --verify /path/to/ZE-video-edit-studio
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import shutil
import sys

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
SOURCE_PKG = os.path.join(REPO_ROOT, "src", "ze_video_domain")
LOCK_REGISTRY = os.path.join(REPO_ROOT, "domain_lock.json")
TARGET_REL = os.path.join("02-mod", "05-code", "_domain")
LOCK_FILENAME = "_DOMAIN_LOCK.json"


def _version() -> str:
    """版本只有一处真相:``__init__.DOMAIN_VERSION``。

    用文本解析而不是 import —— 同步器要能在没装依赖的环境里跑。
    根目录的 ``VERSION`` 只是给人看的镜像,由 tests 里的守卫保证两者一致:
    从前同步器读 VERSION、消费方 import DOMAIN_VERSION,两份已经各自演化过一次
    (域层升到 0.2.0,同步进去的锁却还写 0.1.0)。
    """
    init_py = os.path.join(SOURCE_PKG, "__init__.py")
    with open(init_py, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("DOMAIN_VERSION"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f"DOMAIN_VERSION not found in {init_py}")


def _iter_source_files(root: str):
    for dirpath, _dirs, files in os.walk(root):
        if "__pycache__" in dirpath:
            continue
        for name in sorted(files):
            if name.endswith(".pyc"):
                continue
            yield os.path.join(dirpath, name)


def _tree_checksum(root: str) -> str:
    """Deterministic checksum over relative-path + content of every file."""
    h = hashlib.sha256()
    for path in sorted(_iter_source_files(root)):
        rel = os.path.relpath(path, root)
        h.update(rel.encode("utf-8"))
        with open(path, "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _target_domain_dir(skill_dir: str) -> str:
    return os.path.join(skill_dir, TARGET_REL)


def sync(skill_dir: str) -> int:
    if not os.path.isdir(skill_dir):
        print(f"error: skill dir not found: {skill_dir}", file=sys.stderr)
        return 2
    target = _target_domain_dir(skill_dir)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if os.path.isdir(target):
        shutil.rmtree(target)
    shutil.copytree(SOURCE_PKG, target,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    checksum = _tree_checksum(SOURCE_PKG)
    version = _version()
    lock = {
        "schema": "ze.video_domain_lock.v1",
        "source_repo": "ZE-video-timeline-domain",
        "domain_version": version,
        "checksum": checksum,
        "synced_at": _now(),
        "note": "Managed layer — do not hand-edit. Changes must reflow to "
                "ZE-video-timeline-domain then re-sync (受管层必回流上游).",
    }
    with open(os.path.join(target, LOCK_FILENAME), "w", encoding="utf-8") as fh:
        json.dump(lock, fh, ensure_ascii=False, indent=2)
    _register_consumer(os.path.basename(skill_dir.rstrip("/")), version, checksum)
    print(f"synced ze_video_domain {version} → {target}")
    print(f"  checksum {checksum[:16]}…  files {sum(1 for _ in _iter_source_files(SOURCE_PKG))}")
    return 0


def verify(skill_dir: str) -> int:
    target = _target_domain_dir(skill_dir)
    lock_path = os.path.join(target, LOCK_FILENAME)
    if not os.path.exists(lock_path):
        print(f"MISSING: {skill_dir} has no synced _domain")
        return 1
    with open(lock_path, encoding="utf-8") as fh:
        lock = json.load(fh)
    source_checksum = _tree_checksum(SOURCE_PKG)  # source has no lock file
    # checksum the target minus its own lock file
    target_checksum = _tree_checksum_excluding(target, {LOCK_FILENAME})
    if lock["domain_version"] != _version():
        print(f"BEHIND: {skill_dir} at {lock['domain_version']}, source {_version()} — re-sync")
        return 1
    if target_checksum != source_checksum:
        print(f"DRIFT: {skill_dir}/_domain edited out of band — reflow upstream, do NOT re-sync over it")
        return 1
    print(f"OK: {skill_dir} in sync at {lock['domain_version']} ({source_checksum[:16]}…)")
    return 0


def _tree_checksum_excluding(root: str, exclude_names: set[str]) -> str:
    h = hashlib.sha256()
    for path in sorted(_iter_source_files(root)):
        if os.path.basename(path) in exclude_names:
            continue
        rel = os.path.relpath(path, root)
        h.update(rel.encode("utf-8"))
        with open(path, "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()


def _register_consumer(skill_id: str, version: str, checksum: str) -> None:
    registry = {"schema": "ze.video_domain_registry.v1", "consumers": {}}
    if os.path.exists(LOCK_REGISTRY):
        with open(LOCK_REGISTRY, encoding="utf-8") as fh:
            registry = json.load(fh)
    registry.setdefault("consumers", {})[skill_id] = {
        "domain_version": version, "checksum": checksum, "synced_at": _now()}
    with open(LOCK_REGISTRY, "w", encoding="utf-8") as fh:
        json.dump(registry, fh, ensure_ascii=False, indent=2)


def list_consumers() -> int:
    print(f"ze_video_domain source version: {_version()}")
    if not os.path.exists(LOCK_REGISTRY):
        print("no consumers registered yet")
        return 0
    with open(LOCK_REGISTRY, encoding="utf-8") as fh:
        registry = json.load(fh)
    for skill, info in registry.get("consumers", {}).items():
        print(f"  {skill}: {info['domain_version']} (synced {info['synced_at']})")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Sync the shared video domain into a skill")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--sync", metavar="SKILL_DIR")
    group.add_argument("--verify", metavar="SKILL_DIR")
    group.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    if args.list:
        return list_consumers()
    if args.sync:
        return sync(args.sync)
    return verify(args.verify)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
