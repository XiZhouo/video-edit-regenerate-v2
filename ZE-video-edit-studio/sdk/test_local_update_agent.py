from __future__ import annotations

import base64
import copy
import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sdk.local_update_agent import (
    InstallAuthorization,
    LocalUpdateAgent,
    LocalUpdateError,
    TrustedReleaseKey,
    artifact_signature_message,
)


CHECKED_AT = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
SCOPE = {
    "series_id": "ze",
    "organization_id": "org-hupo",
    "target_id": "public-local",
}


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _zip(path: Path, payload: bytes = b"export const run = () => 'ok';\n") -> None:
    info = zipfile.ZipInfo("app/main.js", date_time=(2020, 1, 1, 0, 0, 0))
    info.external_attr = 0o100644 << 16
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(info, payload)


def _unsafe_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("../escape.txt", "no")


def _key_record(private: Ed25519PrivateKey, *, key_id: str = "release-main") -> TrustedReleaseKey:
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return TrustedReleaseKey(
        key_id=key_id,
        public_key=public,
        series_id="ze",
        organization_id="org-hupo",
        allowed_artifact_roles=("runtime",),
    )


def _manifest(
    artifact: Path,
    private: Ed25519PrivateKey,
    *,
    version: str = "1.0.0",
    manifest_version: int = 1,
    migrations: list[dict] | None = None,
) -> tuple[dict, bytes]:
    artifact_bytes = artifact.read_bytes()
    artifact_digest = _digest(artifact_bytes)
    detached = private.sign(artifact_signature_message(artifact_digest))
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    unsigned = {
        "schema": "ze.update-manifest.v1",
        "manifest_id": f"update-skill-runtime-{version.replace('.', '-')}",
        "manifest_version": manifest_version,
        "scope": copy.deepcopy(SCOPE),
        "channel": "stable",
        "state": "active",
        "published_at": "2026-07-18T00:00:00Z",
        "expires_at": "2027-07-18T00:00:00Z",
        "component": {
            "component_id": "dialogue-runtime",
            "kind": "skill-runtime",
            "version": version,
            "severity": "recommended",
            "release_notes": {"title": version, "summary": "tested update"},
        },
        "rollout": {"percentage": 100, "seed": "release-seed"},
        "artifact": {
            "platform": "any",
            "architecture": "any",
            "immutable_url": f"https://releases.example.test/sha256/{artifact_digest[7:]}",
            "size_bytes": len(artifact_bytes),
            "sha256": artifact_digest,
            "signature": {
                "algorithm": "ed25519",
                "key_id": "release-main",
                "public_key_digest": _digest(public),
                "signature_digest": _digest(detached),
            },
        },
        "compatibility": {},
        "install": {
            "restart": "required",
            "safe_point": "no-unsaved-work",
            "estimated_seconds": 5,
            "migration_confirmation": "required" if migrations else "not-required",
        },
        "project_migrations": migrations or [],
    }
    payload = _canonical(unsigned)
    manifest = {
        **unsigned,
        "signature": {
            "algorithm": "ed25519",
            "key_id": "release-main",
            "public_key_digest": _digest(public),
            "payload_digest": _digest(payload),
            "value": base64.b64encode(private.sign(payload)).decode(),
        },
    }
    return manifest, detached


def _offer(manifest: dict, *, mode: str = "auto-download-prompt-install") -> dict:
    return {
        "schema": "ze.update-offer.v1",
        "scope": copy.deepcopy(manifest["scope"]),
        "manifest_id": manifest["manifest_id"],
        "manifest_version": manifest["manifest_version"],
        "component_id": manifest["component"]["component_id"],
        "current_version": "0.9.0",
        "offered_version": manifest["component"]["version"],
        "decision": "available",
        "severity": "recommended",
        "effective_mode": mode,
        "organization_managed": False,
        "can_download": True,
        "can_install_now": True,
        "automatic_download_allowed": True,
        "automatic_install_now_allowed": mode == "patch-auto",
        "local_project_access_preserved": True,
        "requires_restart": True,
        "reason_codes": [],
        "safe_point_blockers": [],
        "actions": ["install-on-exit"] if mode == "install-on-exit" else ["install-now"],
        "checked_at": "2026-07-19T12:00:00Z",
    }


def _stage(
    agent: LocalUpdateAgent,
    artifact: Path,
    manifest: dict,
    detached: bytes,
) -> None:
    result = agent.stage_file(
        manifest,
        _offer(manifest),
        artifact,
        detached,
        checked_at=CHECKED_AT,
    )
    assert result["outcome"] == "verified"


def _human_authorization() -> InstallAuthorization:
    return InstallAuthorization(
        reason="user-confirmed",
        safe_point_confirmed=True,
        user_confirmed=True,
    )


def test_signed_stage_activate_and_project_files_remain_outside_transaction(tmp_path: Path) -> None:
    private = Ed25519PrivateKey.generate()
    artifact = tmp_path / "runtime.zip"
    _zip(artifact)
    manifest, detached = _manifest(artifact, private)
    project = tmp_path / "project"
    project.mkdir()
    sentinel = project / "game.project"
    sentinel.write_text("author-owned", encoding="utf-8")
    agent = LocalUpdateAgent(tmp_path / "updates", [_key_record(private)])

    _stage(agent, artifact, manifest, detached)
    receipt = agent.activate(
        manifest,
        _offer(manifest),
        _human_authorization(),
        lambda payload: (payload / "app" / "main.js").read_text().startswith("export"),
        checked_at=CHECKED_AT,
    )

    assert receipt["outcome"] == "healthy"
    assert agent.resolve_current("dialogue-runtime").joinpath("app/main.js").is_file()
    assert sentinel.read_text(encoding="utf-8") == "author-owned"
    serialized = json.dumps(agent.read_state(), ensure_ascii=False)
    assert str(project) not in serialized
    assert str(artifact) not in serialized


def test_manifest_cannot_bring_its_own_trust_key(tmp_path: Path) -> None:
    signer = Ed25519PrivateKey.generate()
    trusted = Ed25519PrivateKey.generate()
    artifact = tmp_path / "runtime.zip"
    _zip(artifact)
    manifest, detached = _manifest(artifact, signer)
    agent = LocalUpdateAgent(tmp_path / "updates", [_key_record(trusted, key_id="other-key")])

    with pytest.raises(LocalUpdateError, match="release-key-untrusted") as error:
        agent.stage_file(manifest, _offer(manifest), artifact, detached, checked_at=CHECKED_AT)
    assert error.value.code == "release-key-untrusted"


def test_tampered_artifact_does_not_advance_trusted_manifest_state(tmp_path: Path) -> None:
    private = Ed25519PrivateKey.generate()
    original = tmp_path / "runtime.zip"
    _zip(original)
    manifest, detached = _manifest(original, private)
    tampered = tmp_path / "tampered.zip"
    _zip(tampered, b"different")
    agent = LocalUpdateAgent(tmp_path / "updates", [_key_record(private)])

    with pytest.raises(LocalUpdateError) as error:
        agent.stage_file(manifest, _offer(manifest), tampered, detached, checked_at=CHECKED_AT)
    assert error.value.code in {"artifact-size-mismatch", "artifact-digest-mismatch"}
    assert agent.read_state()["highest_manifests"] == {}


def test_archive_traversal_is_rejected_before_activation_pointer(tmp_path: Path) -> None:
    private = Ed25519PrivateKey.generate()
    artifact = tmp_path / "unsafe.zip"
    _unsafe_zip(artifact)
    manifest, detached = _manifest(artifact, private)
    agent = LocalUpdateAgent(tmp_path / "updates", [_key_record(private)])
    _stage(agent, artifact, manifest, detached)

    with pytest.raises(LocalUpdateError, match="archive-path-unsafe"):
        agent.activate(
            manifest,
            _offer(manifest),
            _human_authorization(),
            lambda _: True,
            checked_at=CHECKED_AT,
        )
    assert not (tmp_path / "escape.txt").exists()
    assert not (tmp_path / "updates/components/dialogue-runtime/current.json").exists()


def test_failed_health_check_restores_previous_healthy_version(tmp_path: Path) -> None:
    private = Ed25519PrivateKey.generate()
    agent = LocalUpdateAgent(tmp_path / "updates", [_key_record(private)])
    first_artifact = tmp_path / "v1.zip"
    _zip(first_artifact, b"v1")
    first, first_signature = _manifest(first_artifact, private)
    _stage(agent, first_artifact, first, first_signature)
    assert agent.activate(
        first,
        _offer(first),
        _human_authorization(),
        lambda _: True,
        checked_at=CHECKED_AT,
    )["outcome"] == "healthy"

    second_artifact = tmp_path / "v2.zip"
    _zip(second_artifact, b"v2")
    second, second_signature = _manifest(
        second_artifact,
        private,
        version="1.1.0",
        manifest_version=2,
    )
    _stage(agent, second_artifact, second, second_signature)
    receipt = agent.activate(
        second,
        _offer(second),
        _human_authorization(),
        lambda _: False,
        checked_at=CHECKED_AT,
    )

    assert receipt["outcome"] == "rolled-back"
    assert receipt["current_version"] == "1.0.0"
    assert agent.resolve_current("dialogue-runtime").joinpath("app/main.js").read_bytes() == b"v1"


def test_interrupted_switch_is_rolled_back_on_next_agent_start(tmp_path: Path) -> None:
    private = Ed25519PrivateKey.generate()
    key = _key_record(private)
    agent = LocalUpdateAgent(tmp_path / "updates", [key])
    first_artifact = tmp_path / "v1.zip"
    _zip(first_artifact, b"v1")
    first, first_signature = _manifest(first_artifact, private)
    _stage(agent, first_artifact, first, first_signature)
    agent.activate(first, _offer(first), _human_authorization(), lambda _: True, checked_at=CHECKED_AT)

    second_artifact = tmp_path / "v2.zip"
    _zip(second_artifact, b"v2")
    second, second_signature = _manifest(
        second_artifact,
        private,
        version="1.1.0",
        manifest_version=2,
    )
    _stage(agent, second_artifact, second, second_signature)

    def terminate(_: Path) -> bool:
        raise SystemExit(9)

    with pytest.raises(SystemExit):
        agent.activate(
            second,
            _offer(second),
            _human_authorization(),
            terminate,
            checked_at=CHECKED_AT,
        )
    recovered = LocalUpdateAgent(tmp_path / "updates", [key])
    assert recovered.resolve_current("dialogue-runtime").joinpath("app/main.js").read_bytes() == b"v1"
    assert recovered.read_state()["components"]["dialogue-runtime"]["versions"]["1.1.0"][
        "status"
    ] == "interrupted"


def test_project_migration_and_unconfirmed_prompt_are_separate_hard_gates(tmp_path: Path) -> None:
    private = Ed25519PrivateKey.generate()
    artifact = tmp_path / "runtime.zip"
    _zip(artifact)
    manifest, detached = _manifest(artifact, private)
    agent = LocalUpdateAgent(tmp_path / "updates", [_key_record(private)])
    _stage(agent, artifact, manifest, detached)

    with pytest.raises(LocalUpdateError, match="user-confirmation-required"):
        agent.activate(
            manifest,
            _offer(manifest),
            InstallAuthorization(reason="user-confirmed", safe_point_confirmed=True),
            lambda _: True,
            checked_at=CHECKED_AT,
        )

    migrated = copy.deepcopy(manifest)
    migrated["project_migrations"] = [{
        "migration_id": "project-v2",
        "from_range": "*",
        "to_version": "1.0.0",
        "reversible": False,
        "backup_required": True,
        "summary": "separate transaction",
    }]
    with pytest.raises(LocalUpdateError, match="project-migration-separate-confirmation-required"):
        agent.activate(
            migrated,
            _offer(migrated),
            _human_authorization(),
            lambda _: True,
            checked_at=CHECKED_AT,
        )


def test_manifest_version_equivocation_is_blocked(tmp_path: Path) -> None:
    private = Ed25519PrivateKey.generate()
    agent = LocalUpdateAgent(tmp_path / "updates", [_key_record(private)])
    first_artifact = tmp_path / "first.zip"
    _zip(first_artifact, b"one")
    first, first_signature = _manifest(first_artifact, private)
    _stage(agent, first_artifact, first, first_signature)

    alternate_artifact = tmp_path / "alternate.zip"
    _zip(alternate_artifact, b"two")
    alternate, alternate_signature = _manifest(alternate_artifact, private)
    with pytest.raises(LocalUpdateError, match="manifest-version-equivocation"):
        agent.stage_file(
            alternate,
            _offer(alternate),
            alternate_artifact,
            alternate_signature,
            checked_at=CHECKED_AT,
        )
