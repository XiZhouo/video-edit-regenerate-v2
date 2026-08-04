from __future__ import annotations

import json
import re
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

SURFACE_SCHEMA = "ze.surface_profile.v1"
SURFACES = {"cli", "api", "web", "mcp"}
# mcp 是附属 transport(挂在 api/web 的 server.py 上),可入 surfaces 但不作 primary。
PRIMARY_SURFACES = {"cli", "api", "web"}
HUMAN_CONTROL = {"web_workbench", "cli_confirm", "artifact_review", "external_review", "none"}
SERVICE_KINDS = {"api", "web", "pipeline-web"}
CLI_KINDS = {"cli", "local-cli", "remote-cli"}
WEB_DIRS = ("webapp", "web", "web_tool", "mod-package/06-ui-framework/workbench")
PRODUCT_DIR_NAME = re.compile(r"product(?:-[a-z0-9]+(?:-[a-z0-9]+)*)?\Z")


def _reject_duplicate_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key: {key}")
        out[key] = value
    return out


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _clean_rel_path(value: Any) -> str | None:
    """Return one canonical POSIX relative path, or ``None`` when unsafe."""
    if not isinstance(value, str):
        return None
    raw = value.strip().replace("\\", "/").rstrip("/")
    if not raw:
        return None
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        return None
    return path.as_posix()


def _first_symlink_component(root: Path, rel: str) -> str | None:
    current = root.resolve()
    for part in PurePosixPath(rel).parts:
        current = current / part
        if current.is_symlink():
            return str(current)
    return None


def _validated_entrypoint(
    *,
    root: Path,
    surface: str,
    nested_name: str,
    nested_value: Any,
    entrypoint_value: Any,
    require_paths: bool,
    expect: str,
    errors: list[str],
) -> str | None:
    """Validate two declarations as one canonical filesystem authority."""
    nested = _clean_rel_path(nested_value)
    top = _clean_rel_path(entrypoint_value)
    if nested is None:
        errors.append(f"{surface}.{nested_name} must be a canonical relative path")
    elif nested_value != nested:
        errors.append(f"{surface}.{nested_name} must use canonical POSIX form: {nested}")
    if top is None:
        errors.append(f"entrypoints.{surface} must be a canonical relative path")
    elif entrypoint_value != top:
        errors.append(f"entrypoints.{surface} must use canonical POSIX form: {top}")
    if nested is None or top is None:
        return None
    if nested != top:
        errors.append(
            f"{surface}.{nested_name} and entrypoints.{surface} must be identical"
        )
        return None
    symlink = _first_symlink_component(root, nested)
    if symlink:
        errors.append(f"{surface} entrypoint must not traverse a symlink: {nested}")
        return None
    path = root / nested
    if require_paths:
        exists = path.is_dir() if expect == "dir" else path.is_file()
        if not exists:
            errors.append(f"{surface} {expect} missing: {nested}")
    return nested


def validate_managed_surface_paths(profile: dict[str, Any]) -> list[str]:
    """Direct-copy V3 uses one canonical layout; generic Surface may be broader."""
    errors: list[str] = []
    surfaces = profile.get("surfaces")
    surfaces = surfaces if isinstance(surfaces, list) else []
    entrypoints = profile.get("entrypoints")
    entrypoints = entrypoints if isinstance(entrypoints, dict) else {}
    expected = {
        "cli": ("entrypoint", "scripts/ze_command.py"),
        "api": ("server", "server.py"),
        "web": ("dir", "webapp"),
    }
    for surface, (detail_key, canonical) in expected.items():
        if surface not in surfaces:
            continue
        detail = profile.get(surface)
        nested = detail.get(detail_key) if isinstance(detail, dict) else None
        top = entrypoints.get(surface)
        if nested != canonical or top != canonical:
            errors.append(
                f"direct-copy V3 requires {surface}.{detail_key} and "
                f"entrypoints.{surface} to both equal {canonical!r}"
            )
    return errors


def has_cli(root: Path) -> bool:
    return (root / "scripts" / "ze_command.py").is_file()


def has_web(root: Path) -> bool:
    return any((root / rel).exists() for rel in WEB_DIRS)


def inferred_web_dir(root: Path) -> str | None:
    return next((rel for rel in WEB_DIRS if (root / rel).is_dir()), None)


def deploy_info(root: Path) -> dict[str, Any]:
    data = _read_json(root / "deploy.json", {})
    return data if isinstance(data, dict) else {}


def command_registry(root: Path) -> dict[str, Any]:
    data = _read_json(root / "commands" / "registry.json", {})
    return data if isinstance(data, dict) else {}


def command_has_side_effect(command: dict[str, Any]) -> bool:
    if command.get("readonly") is True or command.get("read_only") is True:
        return False
    for key in ("side_effects", "effects", "writes", "external_effects"):
        value = command.get(key)
        if value:
            return True
    if command.get("requires_confirmation") is True:
        return True
    mode = str(command.get("effect") or command.get("side_effect") or command.get("write") or "").lower()
    return mode not in {"", "none", "read", "readonly", "read_only", "no"}


def infer_surface_profile(root: Path) -> dict[str, Any]:
    deploy = deploy_info(root)
    service_kind = str(deploy.get("service_kind") or "").strip()
    cli = has_cli(root)
    web_dir = inferred_web_dir(root)
    web = web_dir is not None
    api = service_kind == "api" or ((root / "server.py").is_file() and not web)

    if web:
        surfaces = ["cli", "web"] if cli else ["web"]
        return {
            "schema": SURFACE_SCHEMA,
            "primary": "web",
            "surfaces": surfaces,
            "human_control": "web_workbench",
            "entrypoints": ({"cli": "scripts/ze_command.py"} if cli else {})
            | ({"web": web_dir} if web_dir else {}),
            "cli": {"entrypoint": "scripts/ze_command.py", "supports": ["list", "describe", "run", "inspect"]} if cli else None,
            "api": {"server": "server.py", "healthcheck_path": deploy.get("healthcheck_path") or "/health", "port_required": True} if (root / "server.py").is_file() else None,
            "web": {"dir": web_dir, "product_dir": f"{web_dir}/src/product", "healthcheck_path": deploy.get("healthcheck_path") or "/health"},
            "_inferred": True,
        }
    if api:
        return {
            "schema": SURFACE_SCHEMA,
            "primary": "api",
            "surfaces": ["cli", "api"] if cli else ["api"],
            "human_control": "cli_confirm" if cli else "external_review",
            "entrypoints": ({"cli": "scripts/ze_command.py"} if cli else {}) | {"api": "server.py"},
            "cli": {"entrypoint": "scripts/ze_command.py", "supports": ["list", "describe", "run", "inspect"]} if cli else None,
            "api": {"server": "server.py", "healthcheck_path": deploy.get("healthcheck_path") or "/health", "port_required": True},
            "web": None,
            "_inferred": True,
        }
    if cli:
        return {
            "schema": SURFACE_SCHEMA,
            "primary": "cli",
            "surfaces": ["cli"],
            "human_control": "cli_confirm",
            "entrypoints": {"cli": "scripts/ze_command.py"},
            "cli": {"entrypoint": "scripts/ze_command.py", "supports": ["list", "describe", "run", "inspect"]},
            "api": None,
            "web": None,
            "_inferred": True,
        }
    return {
        "schema": SURFACE_SCHEMA,
        "primary": "unknown",
        "surfaces": [],
        "human_control": "none",
        "entrypoints": {},
        "cli": None,
        "api": None,
        "web": None,
        "_inferred": True,
    }


def load_surface_profile(root: Path) -> dict[str, Any]:
    path = root / "ZE_SURFACE.json"
    # An explicit declaration is authority.  Malformed/non-object content must
    # remain visibly invalid instead of falling back to filesystem inference.
    if path.exists() or path.is_symlink():
        if path.is_symlink():
            return {
                "_inferred": False,
                "_load_error": "ZE_SURFACE.json must not be a symlink",
            }
        try:
            data = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
            )
        except json.JSONDecodeError as exc:
            return {
                "_inferred": False,
                "_load_error": f"invalid JSON at line {exc.lineno} column {exc.colno}",
            }
        except ValueError as exc:
            return {"_inferred": False, "_load_error": str(exc)}
        except (OSError, UnicodeError) as exc:
            return {
                "_inferred": False,
                "_load_error": f"unreadable ZE_SURFACE.json: {type(exc).__name__}",
            }
        if not isinstance(data, dict):
            return {
                "_inferred": False,
                "_load_error": "ZE_SURFACE.json must contain a JSON object",
            }
        data = dict(data)
        data["_inferred"] = False
        return data
    return infer_surface_profile(root)


def validate_surface_profile(
    root: Path,
    profile: dict[str, Any] | None = None,
    *,
    require_paths: bool = True,
) -> list[str]:
    """Validate one profile.

    ``require_paths=False`` is reserved for construction-time ownership
    projection: declarations are validated fail-closed before scaffolded files
    exist.  Normal verification keeps the default and proves every entrypoint
    and Web directory has materialized.
    """
    if profile is None:
        profile = load_surface_profile(root)
    errors: list[str] = []
    load_error = profile.get("_load_error")
    if isinstance(load_error, str) and load_error:
        errors.append(load_error)
    if profile.get("schema") != SURFACE_SCHEMA:
        errors.append("schema must be ze.surface_profile.v1")
    primary = profile.get("primary")
    surfaces = profile.get("surfaces")
    if primary not in PRIMARY_SURFACES:
        errors.append("primary must be cli/api/web (mcp 等附属 transport 不作 primary)")
    if not isinstance(surfaces, list) or not surfaces:
        errors.append("surfaces must be a non-empty list")
        surfaces = []
    elif len(surfaces) != len(set(map(str, surfaces))):
        errors.append("surfaces must not contain duplicate values")
    bad = [item for item in surfaces if item not in SURFACES]
    if bad:
        errors.append(f"unknown surface(s): {', '.join(map(str, bad))}")
    if primary in PRIMARY_SURFACES and primary not in surfaces:
        errors.append("primary must be included in surfaces")
    if profile.get("human_control") not in HUMAN_CONTROL:
        errors.append("human_control must be a known value")
    if profile.get("human_control") == "none":
        registry = command_registry(root)
        commands = registry.get("commands") if isinstance(registry.get("commands"), list) else []
        side_effect_commands = [
            str(command.get("id") or "<unknown>")
            for command in commands
            if isinstance(command, dict) and command_has_side_effect(command)
        ]
        if side_effect_commands:
            errors.append(f"human_control=none requires read-only commands; side effects: {', '.join(side_effect_commands[:8])}")
    entrypoints = profile.get("entrypoints")
    if not isinstance(entrypoints, dict):
        errors.append("entrypoints must be an object")
        entrypoints = {}
    else:
        unknown_entrypoints = sorted(
            str(name) for name in entrypoints if name not in SURFACES
        )
        if unknown_entrypoints:
            errors.append(
                "entrypoints contains unknown surface(s): " + ", ".join(unknown_entrypoints)
            )
        inactive_entrypoints = sorted(
            str(name) for name in entrypoints
            if name in SURFACES and name not in surfaces
        )
        if inactive_entrypoints:
            errors.append(
                "entrypoints declared for inactive surface(s): "
                + ", ".join(inactive_entrypoints)
            )
    if "cli" in surfaces:
        cli = profile.get("cli")
        if not isinstance(cli, dict):
            errors.append("cli surface requires a cli object")
            cli = {}
        _validated_entrypoint(
            root=root,
            surface="cli",
            nested_name="entrypoint",
            nested_value=cli.get("entrypoint"),
            entrypoint_value=entrypoints.get("cli"),
            require_paths=require_paths,
            expect="file",
            errors=errors,
        )
    if "web" in surfaces:
        web = profile.get("web")
        if not isinstance(web, dict):
            errors.append("web surface requires a web object")
            web = {}

        raw_web_dir = web.get("dir")
        web_dir = _validated_entrypoint(
            root=root,
            surface="web",
            nested_name="dir",
            nested_value=raw_web_dir,
            entrypoint_value=entrypoints.get("web"),
            require_paths=require_paths,
            expect="dir",
            errors=errors,
        )

        raw_product_dir = web.get("product_dir")
        product_dir = _clean_rel_path(raw_product_dir)
        if product_dir is None:
            errors.append("web surface requires a canonical relative web.product_dir")
        elif raw_product_dir != product_dir:
            errors.append(f"web.product_dir must use canonical POSIX form: {product_dir}")
        elif web_dir is not None:
            product_path = PurePosixPath(product_dir)
            expected_parent = PurePosixPath(web_dir) / "src"
            if product_path.parent != expected_parent:
                errors.append(
                    "web.product_dir must be a direct child of "
                    f"{expected_parent.as_posix()}"
                )
            elif PRODUCT_DIR_NAME.fullmatch(product_path.name) is None:
                errors.append(
                    "web.product_dir name must be 'product' or a reserved "
                    "'product-<slug>' name"
                )
            else:
                product_path_on_disk = root / product_dir
                symlink = _first_symlink_component(root, product_dir)
                if symlink:
                    errors.append(
                        f"web.product_dir must not traverse a symlink: {product_dir}"
                    )
                elif require_paths and not product_path_on_disk.is_dir():
                    errors.append(f"web product dir missing: {product_dir}")
    else:
        web = profile.get("web")
        if isinstance(web, dict) and web.get("product_dir") not in {None, ""}:
            errors.append("web.product_dir must be empty when web is not in surfaces")
    if "api" in surfaces:
        api = profile.get("api")
        if not isinstance(api, dict):
            errors.append("api surface requires an api object")
            api = {}
        _validated_entrypoint(
            root=root,
            surface="api",
            nested_name="server",
            nested_value=api.get("server"),
            entrypoint_value=entrypoints.get("api"),
            require_paths=require_paths,
            expect="file",
            errors=errors,
        )
    if "mcp" in surfaces:
        # MCP 挂在 api/web 的 server.py 上 → 必须有宿主 server surface;并声明 endpoint。
        if not ({"api", "web"} & set(surfaces)):
            errors.append("mcp surface requires a host server (api or web in surfaces)")
        mcp = profile.get("mcp")
        endpoint = str((mcp or {}).get("endpoint") or entrypoints.get("mcp") or "")
        if not endpoint:
            errors.append("mcp surface requires mcp.endpoint or entrypoints.mcp")
    return errors


def has_surface(profile: dict[str, Any], surface: str) -> bool:
    return surface in (profile.get("surfaces") or [])


def is_service_surface(profile: dict[str, Any], deploy: dict[str, Any] | None = None) -> bool:
    # An explicit Surface Profile is the capability authority.  Deployment
    # metadata participates only in legacy inference; it cannot revive a
    # service that an explicit CLI-only profile has retired.
    if profile.get("_inferred") is not True:
        return has_surface(profile, "web") or has_surface(profile, "api")
    deploy = deploy or {}
    service_kind = str(deploy.get("service_kind") or "").strip()
    return service_kind in SERVICE_KINDS or has_surface(profile, "web") or has_surface(profile, "api")


def surface_summary(root: Path) -> dict[str, Any]:
    profile = load_surface_profile(root)
    deploy = deploy_info(root)
    return {
        "profile": profile,
        "errors": validate_surface_profile(root, profile),
        "service": is_service_surface(profile, deploy),
        "web": has_surface(profile, "web"),
        "api": has_surface(profile, "api"),
        "cli": has_surface(profile, "cli"),
    }
