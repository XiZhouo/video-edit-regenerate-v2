from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


sys.path.insert(0, str(Path(__file__).resolve().parent))

from artifact_variant import evaluate_artifact_variant  # noqa: E402
from release_builder import ReleaseBuildError, build_release  # noqa: E402


UID = "skill_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _private_key(path: Path) -> Path:
    key = Ed25519PrivateKey.generate()
    path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    return path


def _ready_dependencies(root: Path, *, status: str = "ready") -> None:
    _write_json(root / "ZE_DEPENDENCIES.json", {
        "schema": "ze.dependency-manifest.v1",
        "skill_id": "ZE-example",
        "status": status,
        "generated_by": "ZE-skill-build",
        "environments": [],
    })


def _local_case(tmp_path: Path, *, build_command: str | None = None) -> tuple[Path, Path]:
    root = tmp_path / "skill"
    root.mkdir()
    (root / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "build_step.py").write_text(
        "from pathlib import Path\nPath('generated.txt').write_text('built\\n')\n",
        encoding="utf-8",
    )
    _write_json(root / "ZE_SURFACE.json", {
        "schema": "ze.surface_profile.v1",
        "primary": "cli",
        "surfaces": ["cli"],
        "human_control": "cli_confirm",
        "entrypoints": {"cli": "source.py"},
        "cli": {"entrypoint": "source.py", "supports": ["run"]},
        "api": None,
        "web": None,
    })
    runtime = {"install": "ze-dependency-adapter", "start": "python3 source.py"}
    if build_command is not None:
        runtime["build"] = build_command
    _write_json(root / "deploy.json", {
        "schema": "ze.deploy-recipe.v3",
        "skill_uid": UID,
        "skill_id": "ZE-example",
        "service_kind": "local-cli",
        "delivery_modes": ["local-full"],
        "runtime": runtime,
        "artifact": {
            "include": ["."],
            "exclude": [
                ".data/", ".env", ".git/", ".venv/", "node_modules/",
                "__pycache__/", "*.pyc", "artifacts/", "catalog/",
            ],
        },
        "data": {
            "roots": [".data"],
            "persistence": "local-filesystem",
            "local_project_access": "mounted-path",
        },
        "secrets": [],
        "provider_requirements": {
            "allowed": ["local-machine"],
            "required_capabilities": ["local_filesystem"],
        },
    })
    _ready_dependencies(root)
    _write_json(root / "ZE_ARTIFACT_BUILD.json", {
        "schema": "ze.artifact-build.v1",
        "version": "1.0.0",
        "channel_eligibility": ["stable", "beta"],
        "minimum_launcher_version": "1.0.0",
        "migration": "none",
        "rollback": "supported",
        "variants": [{
            "variant_id": "local-runtime",
            "runtime_variant": "python-cli",
            "delivery_mode": "local-full",
            "artifact_role": "runtime",
            "feature_set": ["cli"],
            "payload_paths": ["."],
        }],
    })
    return root, _private_key(tmp_path / "release-key.pem")


def test_release_builder_runs_build_and_writes_verified_deterministic_base(tmp_path: Path) -> None:
    root, key = _local_case(tmp_path, build_command="python3 build_step.py")

    first = build_release(
        root,
        config_path=Path("ZE_ARTIFACT_BUILD.json"),
        private_key_path=key,
        key_id="release-main",
    )

    assert first["ok"] is True
    assert first["validation"] == "ready"
    assert (root / "generated.txt").read_text(encoding="utf-8") == "built\n"
    manifest = json.loads((root / "ZE_ARTIFACT_VARIANT.json").read_text(encoding="utf-8"))
    base = manifest["variants"][0]["base_artifact"]
    with zipfile.ZipFile(root / base["path"]) as archive:
        assert archive.read("ZE_DEPENDENCIES.json") == (root / "ZE_DEPENDENCIES.json").read_bytes()
        assert "generated.txt" in archive.namelist()
    before = (root / base["path"]).read_bytes()

    second = build_release(
        root,
        config_path=Path("ZE_ARTIFACT_BUILD.json"),
        private_key_path=key,
        key_id="release-main",
    )

    assert second["manifest_digest"] == first["manifest_digest"]
    assert (root / base["path"]).read_bytes() == before
    assert evaluate_artifact_variant(root)["valid"] is True


def test_release_builder_does_not_expose_session_credentials_to_build_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, key = _local_case(tmp_path, build_command="python3 build_step.py")
    (root / "build_step.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "Path('generated.txt').write_text(os.environ.get('CLOUDFLARE_API_TOKEN', ''))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "must-not-enter-build")

    result = build_release(
        root,
        config_path=Path("ZE_ARTIFACT_BUILD.json"),
        private_key_path=key,
        key_id="release-main",
    )

    assert result["ok"] is True
    assert (root / "generated.txt").read_text(encoding="utf-8") == ""


def test_release_builder_emits_provider_runnable_static_package(tmp_path: Path) -> None:
    root = tmp_path / "skill"
    (root / "webapp" / "dist").mkdir(parents=True)
    (root / "webapp" / "dist" / "index.html").write_text("<h1>ZE</h1>\n", encoding="utf-8")
    _write_json(root / "ZE_SURFACE.json", {
        "schema": "ze.surface_profile.v1",
        "primary": "web",
        "surfaces": ["web"],
        "human_control": "web_workbench",
        "entrypoints": {"web": "webapp/dist/index.html"},
        "cli": None,
        "api": None,
        "web": {"entry": "webapp/dist/index.html", "distribution": "static"},
    })
    _write_json(root / "deploy.json", {
        "schema": "ze.deploy-recipe.v3",
        "skill_uid": UID,
        "skill_id": "ZE-example",
        "service_kind": "web",
        "delivery_modes": ["frontend-only"],
        "runtime": {
            "install": "ze-dependency-adapter", "start": "static",
            "healthcheck_path": "/",
        },
        "artifact": {
            "include": ["webapp/dist/"],
            "exclude": [".data/", ".env", ".git/", ".venv/", "node_modules/", "__pycache__/", "*.pyc"],
            "build_output": "webapp/dist",
        },
        "data": {"roots": [], "persistence": "none", "local_project_access": "none"},
        "secrets": [],
        "provider_requirements": {
            "allowed": ["cloudflare-pages"],
            "required_capabilities": ["static_assets"],
        },
        "api": {"exposure": "none", "authentication": "none", "contract": "none"},
    })
    _ready_dependencies(root)
    _write_json(root / "ZE_ARTIFACT_BUILD.json", {
        "schema": "ze.artifact-build.v1",
        "version": "1.0.0",
        "channel_eligibility": ["beta"],
        "minimum_launcher_version": "1.0.0",
        "migration": "none",
        "rollback": "supported",
        "variants": [{
            "variant_id": "cloudflare-static",
            "runtime_variant": "cloudflare-static-assets",
            "delivery_mode": "frontend-only",
            "artifact_role": "client",
            "feature_set": ["web"],
            "payload_paths": ["webapp/dist"],
            "runtime_package": {
                "module_format": "static",
                "entrypoint": None,
                "assets_directory": "webapp/dist",
                "bindings": [],
            },
        }],
    })
    key = _private_key(tmp_path / "release-key.pem")

    result = build_release(
        root,
        config_path=Path("ZE_ARTIFACT_BUILD.json"),
        private_key_path=key,
        key_id="release-main",
    )

    assert result["validation"] == "ready"
    manifest = json.loads((root / "ZE_ARTIFACT_VARIANT.json").read_text(encoding="utf-8"))
    base = manifest["variants"][0]["base_artifact"]
    assert base["runtime_package"]["manifest_path"] == "ze-runtime.json"
    with zipfile.ZipFile(root / base["path"]) as archive:
        package = json.loads(archive.read("ze-runtime.json"))
        assert package["runtime_family"] == "cloudflare-static-assets"
        assert package["source_visibility"] == "browser-visible"
        assert "payload/webapp/dist/index.html" in archive.namelist()


def test_release_builder_emits_worker_package_with_root_dependency_contract(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skill"
    (root / "worker").mkdir(parents=True)
    (root / "worker" / "main.js").write_text(
        "export default { async fetch() { return new Response('ok') } }\n",
        encoding="utf-8",
    )
    _write_json(root / "openapi.json", {
        "openapi": "3.1.0", "info": {"title": "x", "version": "1.0.0"},
        "paths": {"/health": {"get": {"responses": {"200": {"description": "ok"}}}}},
    })
    _write_json(root / "ZE_SURFACE.json", {
        "schema": "ze.surface_profile.v1",
        "primary": "api",
        "surfaces": ["api"],
        "human_control": "external_review",
        "entrypoints": {"api": "worker/main.js"},
        "cli": None,
        "api": {
            "server": "worker/main.js", "healthcheck_path": "/health",
            "port_required": False, "exposure": "licensed-team",
            "authentication": "bearer", "contract": "openapi.json",
            "versioning": "path",
        },
        "web": None,
    })
    _write_json(root / "deploy.json", {
        "schema": "ze.deploy-recipe.v3",
        "skill_uid": UID,
        "skill_id": "ZE-example",
        "service_kind": "api",
        "delivery_modes": ["self-hosted-full"],
        "runtime": {
            "install": "ze-dependency-adapter", "start": "provider-entrypoint",
            "healthcheck_path": "/health",
        },
        "artifact": {
            "include": ["worker/"],
            "exclude": [".data/", ".env", ".git/", ".venv/", "node_modules/", "__pycache__/", "*.pyc"],
        },
        "data": {"roots": [], "persistence": "sql", "local_project_access": "none"},
        "secrets": [{"name": "ZE_API_KEY", "required": True, "scope": "runtime"}],
        "provider_requirements": {
            "allowed": ["cloudflare-workers"],
            "required_capabilities": ["secrets", "sql"],
        },
        "api": {
            "exposure": "licensed-team", "authentication": "bearer",
            "contract": "openapi.json", "versioning": "path",
        },
    })
    _ready_dependencies(root)
    _write_json(root / "ZE_ARTIFACT_BUILD.json", {
        "schema": "ze.artifact-build.v1",
        "version": "1.0.0",
        "channel_eligibility": ["beta"],
        "minimum_launcher_version": "1.0.0",
        "migration": "none",
        "rollback": "supported",
        "variants": [{
            "variant_id": "cloudflare-worker",
            "runtime_variant": "worker-js",
            "delivery_mode": "self-hosted-full",
            "artifact_role": "server",
            "feature_set": ["api"],
            "payload_paths": ["worker"],
            "runtime_package": {
                "module_format": "es-module",
                "entrypoint": "worker/main.js",
                "assets_directory": None,
                "bindings": [{"kind": "d1-database", "name": "DB"}],
            },
        }],
    })
    key = _private_key(tmp_path / "release-key.pem")

    result = build_release(
        root,
        config_path=Path("ZE_ARTIFACT_BUILD.json"),
        private_key_path=key,
        key_id="release-main",
    )

    assert result["validation"] == "ready"
    manifest = json.loads((root / "ZE_ARTIFACT_VARIANT.json").read_text(encoding="utf-8"))
    base = manifest["variants"][0]["base_artifact"]
    with zipfile.ZipFile(root / base["path"]) as archive:
        names = archive.namelist()
        assert "ZE_DEPENDENCIES.json" in names
        assert "payload/worker/main.js" in names
        package = json.loads(archive.read("ze-runtime.json"))
        assert package["runtime_family"] == "cloudflare-worker"
        assert package["source_visibility"] == "backend-not-browser-delivered"


def test_release_builder_refuses_draft_dependencies(tmp_path: Path) -> None:
    root, key = _local_case(tmp_path)
    _ready_dependencies(root, status="draft")

    with pytest.raises(ReleaseBuildError, match="immutable locks") as error:
        build_release(
            root,
            config_path=Path("ZE_ARTIFACT_BUILD.json"),
            private_key_path=key,
            key_id="release-main",
        )

    assert error.value.code == "dependency_manifest_not_ready"


def test_release_builder_refuses_private_key_inside_skill(tmp_path: Path) -> None:
    root, _ = _local_case(tmp_path)
    key = _private_key(root / "private.pem")

    with pytest.raises(ReleaseBuildError) as error:
        build_release(
            root,
            config_path=Path("ZE_ARTIFACT_BUILD.json"),
            private_key_path=key,
            key_id="release-main",
        )

    assert error.value.code == "release_private_key_inside_skill"


def test_release_builder_never_executes_shell_syntax(tmp_path: Path) -> None:
    root, key = _local_case(tmp_path, build_command="python3 build_step.py && touch escaped")

    with pytest.raises(ReleaseBuildError) as error:
        build_release(
            root,
            config_path=Path("ZE_ARTIFACT_BUILD.json"),
            private_key_path=key,
            key_id="release-main",
        )

    assert error.value.code == "artifact_build_shell_syntax_forbidden"
    assert not (root / "escaped").exists()
