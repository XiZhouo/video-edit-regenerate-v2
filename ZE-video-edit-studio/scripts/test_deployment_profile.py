from __future__ import annotations

import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))

from deployment_profile import RECIPE_SCHEMA, evaluate_deployment_profile, validate_recipe  # noqa: E402


def _surface(root: Path, *, api: bool = True) -> None:
    (root / "server.py").write_text("# server\n", encoding="utf-8")
    profile = {
        "schema": "ze.surface_profile.v1",
        "primary": "api",
        "surfaces": ["api"],
        "human_control": "external_review",
        "entrypoints": {"api": "server.py"},
        "cli": None,
        "api": ({
            "server": "server.py",
            "healthcheck_path": "/health",
            "port_required": True,
            "exposure": "licensed-team",
            "authentication": "bearer",
            "contract": "openapi.json",
            "versioning": "path",
        } if api else None),
        "web": None,
    }
    (root / "ZE_SURFACE.json").write_text(json.dumps(profile), encoding="utf-8")
    (root / "openapi.json").write_text('{"openapi":"3.1.0","info":{"title":"x","version":"1"},"paths":{}}', encoding="utf-8")


def _recipe() -> dict:
    return {
        "schema": RECIPE_SCHEMA,
        "skill_uid": "skill_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "skill_id": "ZE-example",
        "service_kind": "api",
        "delivery_modes": ["self-hosted-full", "creator-hosted-api"],
        "runtime": {"install": "ze-dependency-adapter", "start": "python3 server.py", "healthcheck_path": "/health"},
        "artifact": {
            "include": ["."],
            "exclude": [".data/", ".env", ".git/", ".venv/", "node_modules/", "__pycache__/", "*.pyc"],
        },
        "data": {"roots": [".data"], "persistence": "local-filesystem", "local_project_access": "browser-file-picker"},
        "secrets": [{"name": "ZE_API_SIGNING_KEY", "required": True, "scope": "runtime"}],
        "provider_requirements": {"allowed": ["local-machine", "ssh-baremetal"], "required_capabilities": ["secrets"]},
        "api": {"exposure": "licensed-team", "authentication": "bearer", "contract": "openapi.json", "versioning": "path"},
    }


def test_portable_recipe_passes_without_target_values(tmp_path: Path) -> None:
    _surface(tmp_path)
    result = validate_recipe(tmp_path, _recipe())
    assert result["valid"] is True, result["errors"]


def test_target_bound_values_are_rejected(tmp_path: Path) -> None:
    _surface(tmp_path)
    recipe = _recipe()
    recipe["port"] = 8080
    recipe["account_id"] = "218d72635f5816a2369e752aff754cce"
    result = validate_recipe(tmp_path, recipe)
    assert result["valid"] is False
    assert any("target-bound field forbidden" in error for error in result["errors"])


def test_creator_hosted_api_requires_external_contract_and_auth(tmp_path: Path) -> None:
    _surface(tmp_path)
    recipe = _recipe()
    recipe["api"] = {"exposure": "app-internal", "authentication": "none", "contract": "/openapi.json", "versioning": "none"}
    result = validate_recipe(tmp_path, recipe)
    assert result["valid"] is False
    assert "creator-hosted-api delivery requires licensed-team or public API" in result["errors"]


def test_gpu_cannot_claim_cloudflare_workers(tmp_path: Path) -> None:
    _surface(tmp_path)
    recipe = _recipe()
    recipe["provider_requirements"] = {
        "allowed": ["ssh-baremetal", "cloudflare-workers"],
        "required_capabilities": ["gpu"],
    }
    result = validate_recipe(tmp_path, recipe)
    assert result["valid"] is False
    assert "GPU recipe cannot allow cloudflare-workers" in result["errors"]


def test_v3_local_filesystem_cannot_claim_cloudflare_workers(tmp_path: Path) -> None:
    _surface(tmp_path)
    recipe = _recipe()
    recipe["provider_requirements"] = {
        "allowed": ["cloudflare-workers"],
        "required_capabilities": ["local_filesystem"],
    }
    result = validate_recipe(tmp_path, recipe)
    assert result["valid"] is False
    assert "local_filesystem recipe cannot allow cloudflare-workers" in result["errors"]


def test_unknown_provider_artifacts_field_is_rejected(tmp_path: Path) -> None:
    _surface(tmp_path)
    recipe = _recipe()
    recipe["provider_artifacts"] = {"cloudflare-workers": "wrangler.toml"}
    result = validate_recipe(tmp_path, recipe)
    assert result["valid"] is False
    assert any("provider_artifacts" in error for error in result["errors"])


def test_legacy_recipe_remains_readable_but_is_marked(tmp_path: Path) -> None:
    result = validate_recipe(tmp_path, {"service_kind": "web", "start": "bash start.sh", "port": 8000})
    assert result["valid"] is True
    assert result["legacy"] is True
    assert result["warnings"]


def test_v2_portable_recipe_remains_valid_without_inventing_uid(tmp_path: Path) -> None:
    _surface(tmp_path)
    recipe = _recipe()
    recipe["schema"] = "ze.deploy-recipe.v2"
    recipe.pop("skill_uid")
    result = validate_recipe(tmp_path, recipe)
    assert result["valid"] is True
    assert result["legacy"] is False
    assert result["schema"] == "ze.deploy-recipe.v2"
    assert result["warnings"]


def test_v3_accepts_non_ze_current_id_but_requires_stable_uid(tmp_path: Path) -> None:
    _surface(tmp_path)
    recipe = _recipe()
    recipe["skill_id"] = "RS-example"
    assert validate_recipe(tmp_path, recipe)["valid"] is True
    recipe.pop("skill_uid")
    result = validate_recipe(tmp_path, recipe)
    assert result["valid"] is False
    assert any("skill_uid" in error for error in result["errors"])


def test_browser_secret_literal_is_rejected(tmp_path: Path) -> None:
    _surface(tmp_path)
    source = tmp_path / "webapp" / "src"
    source.mkdir(parents=True)
    (source / "bad.ts").write_text('const x = "DISCORD_BOT_TOKEN=not-a-real-secret-but-forbidden"', encoding="utf-8")
    result = validate_recipe(tmp_path, _recipe())
    assert result["valid"] is False
    assert any("browser-delivered source" in error for error in result["errors"])


def test_evidence_projection_reports_provider_and_confidentiality_boundaries(tmp_path: Path) -> None:
    _surface(tmp_path)
    result = evaluate_deployment_profile(tmp_path, _recipe())
    profile = result["deployment_profile"]
    assert result["valid"] is True
    assert profile["provider_compatibility"]["ssh-baremetal"]["status"] == "native"
    assert profile["provider_compatibility"]["remote-api-client"]["status"] == "blocked"
    assert profile["delivery_compatibility"]["creator-hosted-api"]["status"] == "supported"
    assert profile["confidentiality_checks"]["frontend_source_visibility"] == "visible-to-browser"


def test_remote_api_client_is_client_only_not_native_backend(tmp_path: Path) -> None:
    _surface(tmp_path)
    recipe = _recipe()
    recipe["provider_requirements"]["allowed"].append("remote-api-client")
    result = evaluate_deployment_profile(tmp_path, recipe)
    assert result["valid"] is True
    assert result["deployment_profile"]["provider_compatibility"]["remote-api-client"]["status"] == "client-only"


def test_legacy_evidence_does_not_invent_cloudflare_support(tmp_path: Path) -> None:
    result = evaluate_deployment_profile(
        tmp_path,
        {"service_kind": "web", "start": "bash start.sh", "port": 8000},
    )
    compatibility = result["deployment_profile"]["provider_compatibility"]
    assert compatibility["ssh-baremetal"]["status"] == "legacy-path"
    assert compatibility["cloudflare-workers"]["status"] == "blocked"
    assert compatibility["cloudflare-pages"]["status"] == "blocked"


def test_pages_profile_requires_and_proves_static_frontend_delivery(tmp_path: Path) -> None:
    _surface(tmp_path)
    surface = json.loads((tmp_path / "ZE_SURFACE.json").read_text(encoding="utf-8"))
    surface.update({
        "primary": "web",
        "surfaces": ["web"],
        "entrypoints": {"web": "webapp/src/main.ts"},
        "api": None,
        "web": {"entry": "webapp/src/main.ts", "distribution": "static"},
    })
    (tmp_path / "webapp" / "src").mkdir(parents=True)
    (tmp_path / "webapp" / "src" / "main.ts").write_text("export {};\n", encoding="utf-8")
    (tmp_path / "ZE_SURFACE.json").write_text(json.dumps(surface), encoding="utf-8")
    recipe = _recipe()
    recipe.update({
        "service_kind": "web",
        "delivery_modes": ["frontend-only"],
        "runtime": {"install": "ze-dependency-adapter", "build": "npm run build", "start": "static", "healthcheck_path": "/"},
        "artifact": {
            "include": ["webapp/dist/"],
            "exclude": [".data/", ".env", ".git/", ".venv/", "node_modules/", "__pycache__/", "*.pyc"],
        },
        "data": {"roots": [], "persistence": "none", "local_project_access": "none"},
        "secrets": [],
        "provider_requirements": {"allowed": ["cloudflare-pages"], "required_capabilities": ["static_assets"]},
        "api": {"exposure": "none", "authentication": "none", "contract": "openapi.json", "versioning": "none"},
    })
    result = evaluate_deployment_profile(tmp_path, recipe)
    assert result["valid"] is True, result["errors"]
    assert result["deployment_profile"]["provider_compatibility"]["cloudflare-pages"] == {
        "status": "native",
        "reasons": [],
    }

    recipe["provider_requirements"]["required_capabilities"] = ["gpu"]
    invalid = validate_recipe(tmp_path, recipe)
    assert "GPU recipe cannot allow cloudflare-pages" in invalid["errors"]


def test_invalid_recipe_still_returns_a_structured_evidence_projection(tmp_path: Path) -> None:
    _surface(tmp_path)
    recipe = _recipe()
    recipe["provider_requirements"] = ["ssh-baremetal"]
    result = evaluate_deployment_profile(tmp_path, recipe)
    assert result["valid"] is False
    assert result["deployment_profile"]["provider_compatibility"]["ssh-baremetal"]["status"] == "blocked"


def test_hybrid_delivery_requires_external_authenticated_api(tmp_path: Path) -> None:
    _surface(tmp_path)
    recipe = _recipe()
    recipe["delivery_modes"] = ["hybrid"]
    assert validate_recipe(tmp_path, recipe)["valid"] is True

    recipe["api"]["exposure"] = "app-internal"
    result = validate_recipe(tmp_path, recipe)
    assert result["valid"] is False
    assert "hybrid delivery requires licensed-team or public API" in result["errors"]


def test_hybrid_can_expose_remote_api_client_compatibility(tmp_path: Path) -> None:
    _surface(tmp_path)
    recipe = _recipe()
    recipe["delivery_modes"] = ["hybrid", "evidence-only"]
    recipe["provider_requirements"]["allowed"].append("remote-api-client")
    result = evaluate_deployment_profile(tmp_path, recipe)
    assert result["valid"] is True, result["errors"]
    assert result["deployment_profile"]["provider_compatibility"]["remote-api-client"]["status"] == "client-only"
    assert result["deployment_profile"]["delivery_compatibility"]["evidence-only"]["status"] == "supported"
