#!/usr/bin/env python3
"""ZE Skill Build validator for portable deploy recipes and delivery boundaries.

The validator is intentionally provider-neutral.  It proves that a Skill says
what it needs without embedding a customer host, production port, domain, or
credential value.  Deploy resolves those values later in a Deployment Binding.

Recipe v3 adds stable ``skill_uid`` and accepts a current id from any Series.
Recipe v2 stays fully readable for already migrated ZE Skills; legacy target-
bound ``deploy.json`` files keep their existing compatibility path.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from surface_profile import load_surface_profile


RECIPE_SCHEMA = "ze.deploy-recipe.v3"
COMPAT_RECIPE_SCHEMAS = {"ze.deploy-recipe.v2"}
LEGACY_SCHEMA = "ze.deploy-recipe.legacy"
SERVICE_KINDS = {"local-cli", "remote-cli", "api", "web", "pipeline-web"}
DELIVERY_MODES = {
    "local-full", "self-hosted-full", "creator-hosted-api", "frontend-only",
    "hybrid", "evidence-only",
}
PROVIDERS = {
    "local-machine", "ssh-baremetal", "cloudflare-pages",
    "cloudflare-workers", "remote-api-client",
}
CAPABILITIES = {
    "persistent_process", "gpu", "websocket", "static_assets", "secrets", "sql",
    "object_storage", "kv", "queues", "custom_domains",
}
SCOPED_CAPABILITIES = CAPABILITIES | {"local_filesystem", "subprocess"}
API_EXPOSURES = {"none", "app-internal", "licensed-team", "public"}
API_AUTH = {"none", "session", "bearer", "oauth2", "service-token"}
API_VERSIONING = {"path", "header", "none"}

_TARGET_BOUND_KEYS = {
    "port", "bind_host", "host", "hostname", "domain", "route", "account_id",
    "worker_name", "database_id", "namespace_id", "bucket_name", "deploy_root",
    "credential_ref", "token", "password", "api_key", "private_key", "client_secret",
}
_SECRET_TEXT = re.compile(
    r"(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"(?:DISCORD|GITHUB|CLOUDFLARE|OPENAI|ANTHROPIC)_(?:BOT_)?(?:TOKEN|SECRET|API_KEY)\s*[:=]\s*['\"]?[^'\"\s]{4,})",
    re.IGNORECASE,
)
_REQUIRED_EXCLUDES = {".data/", ".env", ".git/", ".venv/", "node_modules/", "__pycache__/", "*.pyc"}


class DuplicateKeyError(ValueError):
    pass


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_recipe(root: Path) -> dict[str, Any]:
    path = root / "deploy.json"
    if path.is_symlink():
        return {"_load_error": "deploy.json must not be a symlink"}
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs)
    except FileNotFoundError:
        return {"_load_error": "deploy.json is missing"}
    except (OSError, UnicodeError, json.JSONDecodeError, DuplicateKeyError) as exc:
        return {"_load_error": f"deploy.json invalid: {exc}"}
    if not isinstance(value, dict):
        return {"_load_error": "deploy.json must contain an object"}
    return value


def recipe_version(recipe: Mapping[str, Any]) -> str:
    schema = recipe.get("schema")
    if schema == RECIPE_SCHEMA or schema in COMPAT_RECIPE_SCHEMAS:
        return str(schema)
    return LEGACY_SCHEMA


def _canonical_rel(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip().replace("\\", "/").rstrip("/")
    if not raw or raw.startswith("/"):
        return None
    path = PurePosixPath(raw)
    if "." in path.parts or ".." in path.parts:
        return None
    return path.as_posix()


def _walk_target_bound(value: Any, path: str = "$") -> list[str]:
    errors: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key in _TARGET_BOUND_KEYS:
                errors.append(f"target-bound field forbidden in portable recipe: {child_path}")
            errors.extend(_walk_target_bound(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            errors.extend(_walk_target_bound(child, f"{path}[{index}]"))
    return errors


def _exact_keys(value: Mapping[str, Any], allowed: set[str], required: set[str], path: str) -> list[str]:
    errors: list[str] = []
    missing = sorted(required - set(value))
    extra = sorted(set(value) - allowed)
    if missing:
        errors.append(f"{path} missing required field(s): {', '.join(missing)}")
    if extra:
        errors.append(f"{path} contains unknown field(s): {', '.join(extra)}")
    return errors


def _string_list(value: Any, allowed: set[str] | None, path: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        return [f"{path} must be {'a non-empty ' if nonempty else 'an '}array"]
    if any(not isinstance(item, str) or not item for item in value):
        return [f"{path} must contain non-empty strings"]
    if len(value) != len(set(value)):
        return [f"{path} must not contain duplicates"]
    if allowed is not None:
        unknown = sorted(set(value) - allowed)
        if unknown:
            return [f"{path} contains unsupported value(s): {', '.join(unknown)}"]
    return []


def validate_recipe(root: Path, recipe: Mapping[str, Any] | None = None, *, require_paths: bool = True) -> dict[str, Any]:
    recipe = dict(recipe) if recipe is not None else load_recipe(root)
    if recipe.get("_load_error"):
        return {"schema": None, "valid": False, "legacy": False, "errors": [str(recipe["_load_error"])], "warnings": []}
    schema = recipe.get("schema")
    if schema != RECIPE_SCHEMA and schema not in COMPAT_RECIPE_SCHEMAS:
        errors: list[str] = []
        warnings = ["legacy deploy.json: target fields may still be embedded; migrate when this Skill enters a Release Set"]
        service_kind = recipe.get("service_kind") or recipe.get("deployment_kind")
        if service_kind not in SERVICE_KINDS | {"service", None}:
            errors.append("legacy service_kind/deployment_kind is unsupported")
        if service_kind not in {"local-cli", None} and not recipe.get("start"):
            errors.append("legacy service recipe requires start")
        return {"schema": LEGACY_SCHEMA, "valid": not errors, "legacy": True, "errors": errors, "warnings": warnings}

    is_v3 = schema == RECIPE_SCHEMA
    warnings = [] if is_v3 else [
        "portable recipe v2 remains valid; assign a stable skill_uid before creating a scoped v2 Release Set"
    ]
    required_top = {
        "schema", "skill_id", "service_kind", "delivery_modes", "runtime",
        "artifact", "data", "secrets", "provider_requirements",
    }
    allowed_top = required_top | {"api"}
    if is_v3:
        required_top.add("skill_uid")
        allowed_top.add("skill_uid")
    errors = _exact_keys(
        recipe,
        allowed_top,
        required_top,
        "$",
    )
    errors.extend(_walk_target_bound(recipe))
    skill_id = recipe.get("skill_id")
    if is_v3:
        skill_uid = recipe.get("skill_uid")
        if not isinstance(skill_uid, str) or re.fullmatch(r"skill_[0-9a-f]{32}", skill_uid) is None:
            errors.append("$.skill_uid must be a stable ZE Skill UID")
        if not isinstance(skill_id, str) or re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,127}", skill_id
        ) is None:
            errors.append("$.skill_id must be a canonical current Skill id")
    elif not isinstance(skill_id, str) or re.fullmatch(r"ZE-[a-z0-9]+(?:-[a-z0-9]+)*", skill_id) is None:
        errors.append("$.skill_id must be a canonical ZE Skill id in recipe v2")
    service_kind = recipe.get("service_kind")
    if service_kind not in SERVICE_KINDS:
        errors.append("$.service_kind is unsupported")
    errors.extend(_string_list(recipe.get("delivery_modes"), DELIVERY_MODES, "$.delivery_modes", nonempty=True))

    runtime = recipe.get("runtime")
    if not isinstance(runtime, dict):
        errors.append("$.runtime must be an object")
        runtime = {}
    else:
        errors.extend(_exact_keys(runtime, {"install", "build", "start", "healthcheck_path", "working_directory", "graceful_stop_seconds"}, {"install", "start"}, "$.runtime"))
    for key in ("install", "start"):
        if not isinstance(runtime.get(key), str) or not runtime.get(key).strip():
            errors.append(f"$.runtime.{key} must be a non-empty string")
    if is_v3 and runtime.get("install") != "ze-dependency-adapter":
        errors.append(
            "$.runtime.install must be ze-dependency-adapter; dependency installation "
            "is selected by Deploy from ZE_DEPENDENCIES.json"
        )
    if service_kind in {"api", "web", "pipeline-web"}:
        health = runtime.get("healthcheck_path")
        if not isinstance(health, str) or not health.startswith("/"):
            errors.append("service recipe requires $.runtime.healthcheck_path")

    artifact = recipe.get("artifact")
    if not isinstance(artifact, dict):
        errors.append("$.artifact must be an object")
        artifact = {}
    else:
        errors.extend(_exact_keys(artifact, {"include", "exclude", "build_output"}, {"include", "exclude"}, "$.artifact"))
    errors.extend(_string_list(artifact.get("include"), None, "$.artifact.include", nonempty=True))
    errors.extend(_string_list(artifact.get("exclude"), None, "$.artifact.exclude"))
    excludes = set(artifact.get("exclude") or []) if isinstance(artifact.get("exclude"), list) else set()
    missing_excludes = sorted(_REQUIRED_EXCLUDES - excludes)
    if missing_excludes:
        errors.append("$.artifact.exclude misses confidentiality/runtime paths: " + ", ".join(missing_excludes))

    data = recipe.get("data")
    if not isinstance(data, dict):
        errors.append("$.data must be an object")
        data = {}
    else:
        errors.extend(_exact_keys(data, {"roots", "persistence", "local_project_access"}, {"roots", "persistence"}, "$.data"))
    errors.extend(_string_list(data.get("roots"), None, "$.data.roots"))
    if data.get("persistence") not in {"none", "local-filesystem", "sql", "object-storage", "hybrid"}:
        errors.append("$.data.persistence is unsupported")
    if data.get("local_project_access") not in {None, "none", "browser-file-picker", "local-agent", "mounted-path"}:
        errors.append("$.data.local_project_access is unsupported")

    secrets = recipe.get("secrets")
    if not isinstance(secrets, list):
        errors.append("$.secrets must be an array")
        secrets = []
    names: set[str] = set()
    for index, item in enumerate(secrets):
        if not isinstance(item, dict):
            errors.append(f"$.secrets[{index}] must be an object")
            continue
        errors.extend(_exact_keys(item, {"name", "required", "scope", "description"}, {"name", "required", "scope"}, f"$.secrets[{index}]"))
        name = item.get("name")
        if not isinstance(name, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", name) is None:
            errors.append(f"$.secrets[{index}].name is invalid")
        elif name in names:
            errors.append(f"duplicate secret declaration: {name}")
        else:
            names.add(name)
        if not isinstance(item.get("required"), bool):
            errors.append(f"$.secrets[{index}].required must be boolean")
        if item.get("scope") not in {"build", "runtime"}:
            errors.append(f"$.secrets[{index}].scope is unsupported")

    requirements = recipe.get("provider_requirements")
    if not isinstance(requirements, dict):
        errors.append("$.provider_requirements must be an object")
        requirements = {}
    else:
        errors.extend(_exact_keys(requirements, {"allowed", "required_capabilities"}, {"allowed", "required_capabilities"}, "$.provider_requirements"))
    errors.extend(_string_list(requirements.get("allowed"), PROVIDERS, "$.provider_requirements.allowed", nonempty=True))
    capability_vocabulary = SCOPED_CAPABILITIES if is_v3 else CAPABILITIES
    errors.extend(_string_list(
        requirements.get("required_capabilities"),
        capability_vocabulary,
        "$.provider_requirements.required_capabilities",
    ))
    required_capabilities = set(requirements.get("required_capabilities") or []) if isinstance(requirements.get("required_capabilities"), list) else set()
    allowed_providers = set(requirements.get("allowed") or []) if isinstance(requirements.get("allowed"), list) else set()
    if "gpu" in required_capabilities and "cloudflare-pages" in allowed_providers:
        errors.append("GPU recipe cannot allow cloudflare-pages")
    if "gpu" in required_capabilities and "cloudflare-workers" in allowed_providers:
        errors.append("GPU recipe cannot allow cloudflare-workers")
    if "cloudflare-pages" in allowed_providers:
        unsupported = sorted(
            required_capabilities - {"static_assets", "custom_domains"}
        )
        if unsupported:
            errors.append(
                "cloudflare-pages supports only static_assets/custom_domains; unsupported: "
                + ", ".join(unsupported)
            )
        if service_kind not in {"web", "pipeline-web"}:
            errors.append("cloudflare-pages requires web or pipeline-web service_kind")
        if "frontend-only" not in set(recipe.get("delivery_modes") or []):
            errors.append("cloudflare-pages requires frontend-only delivery mode")
    if is_v3 and "cloudflare-workers" in allowed_providers:
        for capability in ("local_filesystem", "subprocess"):
            if capability in required_capabilities:
                errors.append(
                    f"{capability} recipe cannot allow cloudflare-workers"
                )

    api = recipe.get("api")
    if service_kind in {"api", "web", "pipeline-web"} and not isinstance(api, dict):
        errors.append(f"{service_kind} recipe requires $.api")
    if isinstance(api, dict):
        errors.extend(_exact_keys(api, {"exposure", "authentication", "contract", "versioning"}, {"exposure", "authentication", "contract"}, "$.api"))
        if api.get("exposure") not in API_EXPOSURES:
            errors.append("$.api.exposure is unsupported")
        if api.get("authentication") not in API_AUTH:
            errors.append("$.api.authentication is unsupported")
        if api.get("versioning") not in API_VERSIONING | {None}:
            errors.append("$.api.versioning is unsupported")
        contract = api.get("contract")
        if not isinstance(contract, str) or not contract:
            errors.append("$.api.contract must be declared")
        elif (
            contract == "none"
            and api.get("exposure") == "none"
            and api.get("authentication") == "none"
        ):
            pass
        elif require_paths and not contract.startswith("/"):
            rel = _canonical_rel(contract)
            if rel is None or not (root / rel).is_file():
                errors.append(f"API contract file missing or unsafe: {contract}")
        if api.get("exposure") in {"licensed-team", "public"} and api.get("authentication") == "none":
            errors.append("licensed-team/public API must authenticate callers")
        if "creator-hosted-api" in (recipe.get("delivery_modes") or []) and api.get("exposure") in {"none", "app-internal"}:
            errors.append("creator-hosted-api delivery requires licensed-team or public API")
        if "hybrid" in (recipe.get("delivery_modes") or []) and api.get("exposure") in {"none", "app-internal"}:
            errors.append("hybrid delivery requires licensed-team or public API")
    elif "hybrid" in (recipe.get("delivery_modes") or []):
        errors.append("hybrid delivery requires $.api")

    # 浏览器交付源(webapp/src)里的 secret 字面量:打进前端 bundle = 直接泄漏。
    # _SECRET_TEXT 只匹配「名字+赋值+引号值」/私钥块形态,普通用词不误伤。
    browser_src = root / "webapp" / "src"
    if browser_src.is_dir():
        for src_file in sorted(browser_src.rglob("*")):
            if src_file.suffix not in {".ts", ".tsx", ".js", ".jsx", ".json", ".env"}:
                continue
            try:
                text = src_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if _SECRET_TEXT.search(text):
                errors.append(
                    "secret literal in browser-delivered source: "
                    f"{src_file.relative_to(root)}"
                )

    surface = load_surface_profile(root)
    surface_api = surface.get("api") if isinstance(surface.get("api"), dict) else None
    if isinstance(api, dict) and surface_api:
        for key in ("exposure", "authentication", "contract", "versioning"):
            if key in surface_api and surface_api.get(key) != api.get(key):
                errors.append(f"ZE_SURFACE.json api.{key} disagrees with deploy.json api.{key}")

    errors.extend(confidentiality_errors(root))
    return {"schema": schema, "valid": not errors, "legacy": False, "errors": errors, "warnings": warnings}


def confidentiality_errors(root: Path) -> list[str]:
    """Catch obvious secret material in browser-delivered source.

    This is deliberately narrow and deterministic.  It does not claim to prove
    absence of secrets or make frontend implementation confidential.
    """
    web = root / "webapp"
    if not web.is_dir():
        return []
    errors: list[str] = []
    for path in web.rglob("*"):
        if not path.is_file() or any(part in {"node_modules", "dist", ".git"} for part in path.parts):
            continue
        if path.suffix.lower() not in {".ts", ".tsx", ".js", ".jsx", ".json", ".html", ".env"}:
            continue
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if _SECRET_TEXT.search(text):
            errors.append(f"browser-delivered source appears to contain secret material: {path.relative_to(root)}")
    return errors


def _provider_compatibility(
    recipe: Mapping[str, Any], validation: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    """Project portable recipe intent into provider-neutral evidence.

    This is deliberately not a Target compatibility decision.  Build can prove
    which runtimes the candidate contains; only Deploy can compare that proof
    with one Organization's concrete Target capabilities and policies.
    """

    if validation.get("legacy") is True:
        return {
            "local-machine": {
                "status": "unknown",
                "reasons": ["legacy recipe has no portable local runtime proof"],
            },
            "ssh-baremetal": {
                "status": "legacy-path",
                "reasons": ["kept readable by the existing SSH deployment chain"],
            },
            "cloudflare-workers": {
                "status": "blocked",
                "reasons": ["legacy recipe has no Workers runtime artifact proof"],
            },
            "cloudflare-pages": {
                "status": "blocked",
                "reasons": ["legacy recipe has no signed static runtime artifact proof"],
            },
            "remote-api-client": {
                "status": "blocked",
                "reasons": ["legacy recipe has no separated API-client artifact proof"],
            },
        }

    requirements = (
        recipe.get("provider_requirements")
        if isinstance(recipe.get("provider_requirements"), dict)
        else {}
    )
    allowed = set(requirements.get("allowed") or [])
    required = set(
        requirements.get("required_capabilities") or []
    )
    delivery_modes = set(recipe.get("delivery_modes") or [])
    invalid = not bool(validation.get("valid"))
    result: dict[str, dict[str, Any]] = {}
    for provider in sorted(PROVIDERS):
        reasons: list[str] = []
        if invalid:
            reasons.append("deployment profile is invalid")
        if provider not in allowed:
            reasons.append("provider is not listed in provider_requirements.allowed")
        if provider == "cloudflare-workers":
            unsupported = sorted(required & {"gpu", "local_filesystem", "subprocess"})
            if unsupported:
                reasons.append(
                    "Workers cannot satisfy required capabilities: " + ", ".join(unsupported)
                )
        if provider == "cloudflare-pages":
            unsupported = sorted(required - {"static_assets", "custom_domains"})
            if unsupported:
                reasons.append(
                    "Pages static delivery cannot satisfy required capabilities: "
                    + ", ".join(unsupported)
                )
            if not (delivery_modes & {"frontend-only"}):
                reasons.append("frontend-only delivery mode is not declared")
            if recipe.get("service_kind") not in {"web", "pipeline-web"}:
                reasons.append("Pages requires a web or pipeline-web service")
        if provider == "remote-api-client" and not (
            delivery_modes & {"creator-hosted-api", "hybrid"}
        ):
            reasons.append("creator-hosted-api or hybrid delivery mode is not declared")
        if reasons:
            status = "blocked"
        elif provider == "remote-api-client":
            status = "client-only"
        else:
            status = "native"
        result[provider] = {"status": status, "reasons": reasons}
    return result


def _delivery_compatibility(recipe: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    declared = set(recipe.get("delivery_modes") or [])
    confidentiality = {
        "local-full": "not confidential to the device owner",
        "self-hosted-full": "not confidential to the target infrastructure administrator",
        "creator-hosted-api": (
            "backend may remain undistributed only when it runs on a creator-owned Target"
        ),
        "frontend-only": "browser-delivered HTML/CSS/JavaScript remains visible to the user",
        "hybrid": (
            "server implementation may remain undistributed; the local adapter/client remains visible "
            "to its device owner"
        ),
        "evidence-only": "contains contracts and evidence rather than executable runtime secrecy",
    }
    return {
        mode: {
            "status": "supported" if mode in declared else "blocked",
            "confidentiality": confidentiality[mode],
        }
        for mode in sorted(DELIVERY_MODES)
    }


def deployment_profile_summary(
    recipe: Mapping[str, Any], validation: Mapping[str, Any]
) -> dict[str, Any]:
    """Return the stable subset written into ZE_BUILD_EVIDENCE.json."""

    api = recipe.get("api") if isinstance(recipe.get("api"), dict) else None
    requirements = (
        recipe.get("provider_requirements")
        if isinstance(recipe.get("provider_requirements"), dict)
        else {}
    )
    data = recipe.get("data") if isinstance(recipe.get("data"), dict) else {}
    validation_errors = [str(item) for item in validation.get("errors") or []]
    browser_secret_errors = [
        item
        for item in validation_errors
        if "browser-delivered source" in item or "secret literal" in item
    ]
    return {
        "schema": validation.get("schema"),
        "legacy": bool(validation.get("legacy")),
        "service_kind": recipe.get("service_kind") or recipe.get("deployment_kind"),
        "provider_compatibility": _provider_compatibility(recipe, validation),
        "delivery_compatibility": _delivery_compatibility(recipe),
        "api_contract": (
            {
                "exposure": api.get("exposure"),
                "authentication": api.get("authentication"),
                "contract": api.get("contract"),
                "versioning": api.get("versioning", "none"),
            }
            if api is not None
            else None
        ),
        "confidentiality_checks": {
            "browser_secret_scan": "failed" if browser_secret_errors else "passed",
            "frontend_source_visibility": "visible-to-browser",
            "backend_rule": (
                "backend-hidden is only eligible for creator-hosted-api on a creator-owned Target"
            ),
            "proof_scope": (
                "mechanical checks catch obvious browser secret literals; they do not prove "
                "absolute secrecy or prevent behavioral inference"
            ),
        },
        "known_runtime_constraints": {
            "required_capabilities": sorted(requirements.get("required_capabilities") or []),
            "data_persistence": data.get("persistence"),
            "local_project_access": data.get("local_project_access", "none"),
        },
    }


def evaluate_deployment_profile(
    root: Path,
    recipe: Mapping[str, Any] | None = None,
    *,
    require_paths: bool = True,
) -> dict[str, Any]:
    """Validate once and attach the evidence projection used by Build/Deploy."""

    loaded = dict(recipe) if recipe is not None else load_recipe(root)
    validation = validate_recipe(root, loaded, require_paths=require_paths)
    return {
        **validation,
        "deployment_profile": deployment_profile_summary(loaded, validation),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate ZE Skill Build portable deployment profile")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)
    result = evaluate_deployment_profile(Path(args.root).resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
