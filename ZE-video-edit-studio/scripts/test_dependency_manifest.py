from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dependency_manifest import audit_regenerable_paths, build_manifest


def _node_project(root: Path) -> None:
    (root / "package.json").write_text(
        json.dumps({
            "name": "example",
            "engines": {"node": ">=20"},
            "scripts": {"build": "example-build"},
        }),
        encoding="utf-8",
    )
    (root / "package-lock.json").write_text(
        json.dumps({
            "name": "example",
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "example"},
                "node_modules/example": {
                    "version": "1.0.0",
                    "resolved": "https://registry.example.invalid/example.tgz",
                    "integrity": "sha512-test",
                },
            },
        }),
        encoding="utf-8",
    )


def test_build_manifest_emits_ready_locked_node_environment(tmp_path: Path) -> None:
    _node_project(tmp_path)
    manifest = build_manifest(tmp_path, skill_id="ZE-example")
    assert manifest["status"] == "ready"
    assert manifest["generated_by"] == "ZE-skill-build"
    environment = manifest["environments"][0]
    assert environment["lock"]["format"] == "npm-package-lock"
    assert environment["materialization"]["link_path"] == "node_modules"
    serialized = json.dumps(manifest)
    assert "install_command" not in serialized
    assert "npm ci" not in serialized


def test_unlocked_python_is_draft_but_locally_described(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("fastapi>=0.115\n", encoding="utf-8")
    manifest = build_manifest(tmp_path, skill_id="ZE-example")
    assert manifest["status"] == "draft"
    environment = manifest["environments"][0]
    assert environment["install_policy"]["integrity"] == "descriptor-only"
    assert "lock" not in environment


def test_hashed_python_lock_is_ready(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("example==1.0.0\n", encoding="utf-8")
    (tmp_path / "requirements.lock").write_text(
        "example==1.0.0 --hash=sha256:0123456789abcdef\n", encoding="utf-8"
    )
    manifest = build_manifest(tmp_path, skill_id="ZE-example")
    assert manifest["status"] == "ready"
    assert manifest["environments"][0]["lock"]["format"] == "pip-hashed-requirements"


def test_audit_reports_regenerable_paths_without_deleting_them(tmp_path: Path) -> None:
    generated = tmp_path / "node_modules"
    generated.mkdir()
    (generated / "large.bin").write_bytes(b"x" * 128)
    data = tmp_path / ".data"
    data.mkdir()
    (data / "project.json").write_text("{}", encoding="utf-8")

    rows = audit_regenerable_paths(tmp_path)

    assert rows == [{"path": "node_modules", "kind": "unmanaged-regenerable", "size_bytes": 128}]
    assert (generated / "large.bin").is_file()
    assert (data / "project.json").is_file()
