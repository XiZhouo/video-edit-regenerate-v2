"""Prompt-strategy layer — pluggable JSON strategies + provider chain.

Reimplements seedance-site-v2's optimiser as framework-agnostic domain logic.
A strategy is a JSON declaration (id/provider/systemPrompt/userTemplate/…). The
runner renders the user template, then walks a provider chain
``codex-skill → gateway → skip``. The actual codex-exec / gateway-HTTP calls are
INJECTED as callables by the hosting skill's stage layer, so this module has no
network/subprocess dependency and is fully testable. When no provider is
available the runner returns ``None`` (skip) — the human writes the prompt by
hand; that is a designed degradation, not a failure (段卡 shows the provider).

Output contract is the authoritative JSON Schema from seedance-site-v2
(``prompt_optimizer_output_schema``): ``workflow`` nests suggestedMode /
suggestedDuration / assetRoles / riskChecks / promptPlan; optimizedPrompt /
notes / warnings / strategyId are top-level.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Mapping

_PLACEHOLDER = re.compile(r"\{(\w+)\}")

STRATEGY_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["strategyId", "workflow", "optimizedPrompt", "notes", "warnings"],
    "properties": {
        "strategyId": {"type": "string"},
        "workflow": {
            "type": "object",
            "additionalProperties": False,
            "required": ["adType", "hook", "suggestedMode", "suggestedDuration",
                         "assetRoles", "riskChecks", "promptPlan"],
            "properties": {
                "adType": {"type": "string"},
                "hook": {"type": "string"},
                "suggestedMode": {"type": "string"},
                "suggestedDuration": {"type": "number"},
                "assetRoles": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["label", "role", "reason"],
                    "properties": {"label": {"type": "string"}, "role": {"type": "string"},
                                   "reason": {"type": "string"}}}},
                "riskChecks": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["type", "level", "message"],
                    "properties": {"type": {"type": "string"}, "level": {"type": "string"},
                                   "message": {"type": "string"}}}},
                "promptPlan": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["time", "content"],
                    "properties": {"time": {"type": "string"}, "content": {"type": "string"}}}},
            },
        },
        "optimizedPrompt": {"type": "string", "minLength": 1},
        "notes": {"type": "array", "items": {"type": "string"}},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
}

VIDEO_EDIT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["optimized_prompt", "warnings", "facts_used"],
    "properties": {
        "optimized_prompt": {"type": "string", "minLength": 1},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "facts_used": {"type": "array", "items": {"type": "string"}},
    },
}

_STRATEGY_DIR = os.path.join(os.path.dirname(__file__), "strategies")
DEFAULT_STRATEGY_ID = "general-v1"


def strategies_dir() -> str:
    return _STRATEGY_DIR


def load_strategy(strategy_id: str) -> dict[str, Any]:
    path = os.path.join(_STRATEGY_DIR, f"{strategy_id}.json")
    if not os.path.exists(path):
        raise ValueError(f"unknown strategy {strategy_id!r}")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def list_strategies() -> list[dict[str, str]]:
    out = []
    for name in sorted(os.listdir(_STRATEGY_DIR)):
        if name.endswith(".json"):
            data = load_strategy(name[:-5])
            out.append({"id": data["id"], "name": data.get("name", data["id"]),
                        "provider": data.get("provider", "llm")})
    return out


def render_user_template(strategy: Mapping[str, Any], fields: Mapping[str, Any]) -> str:
    """Substitute ``{key}`` placeholders in the strategy userTemplate.

    Only substitutes keys the caller actually supplied. An unknown placeholder is
    left VERBATIM — critical because the shipped strategies end with the literal
    instruction ``素材引用用 {素材名} 占位``: python's ``\\w`` is unicode-aware and would
    match 素材名, so substituting-with-empty would delete the very token the
    strategy is telling the model to use.
    """
    template = strategy.get("userTemplate", "")

    def _sub(m: "re.Match[str]") -> str:
        key = m.group(1)
        return str(fields[key]) if key in fields else m.group(0)

    return _PLACEHOLDER.sub(_sub, template)


def output_schema_for(strategy: Mapping[str, Any]) -> dict[str, Any]:
    if strategy.get("outputContract") == "video-edit-v1":
        return VIDEO_EDIT_OUTPUT_SCHEMA
    return STRATEGY_OUTPUT_SCHEMA


def validate_output(data: Any, strategy: Mapping[str, Any] | None = None) -> tuple[bool, list[str]]:
    """Lightweight validation against STRATEGY_OUTPUT_SCHEMA (stdlib only).

    Not a full JSON-Schema engine — checks the required keys / nesting / types
    the domain relies on, enough to reject a malformed model response before it
    reaches send_profile.
    """
    errors: list[str] = []

    def _req(obj: Any, keys: list[str], where: str) -> None:
        if not isinstance(obj, dict):
            errors.append(f"{where} must be an object")
            return
        for k in keys:
            if k not in obj:
                errors.append(f"{where}.{k} missing")

    if strategy and strategy.get("outputContract") == "video-edit-v1":
        _req(data, ["optimized_prompt", "warnings", "facts_used"], "root")
        if isinstance(data, dict):
            if isinstance(data.get("optimized_prompt"), str) and not data["optimized_prompt"].strip():
                errors.append("optimized_prompt must be non-empty")
            for field in ("warnings", "facts_used"):
                if field in data and not isinstance(data[field], list):
                    errors.append(f"{field} must be an array")
        return (not errors, errors)

    _req(data, ["strategyId", "workflow", "optimizedPrompt", "notes", "warnings"], "root")
    if isinstance(data, dict):
        wf = data.get("workflow")
        _req(wf, ["adType", "hook", "suggestedMode", "suggestedDuration",
                  "assetRoles", "riskChecks", "promptPlan"], "workflow")
        if isinstance(data.get("optimizedPrompt"), str) and not data["optimizedPrompt"].strip():
            errors.append("optimizedPrompt must be non-empty")
    return (not errors, errors)


def run_strategy(strategy_id: str, fields: Mapping[str, Any], *,
                 providers: Mapping[str, Callable[[dict, dict], dict | None]] | None = None
                 ) -> dict[str, Any]:
    """Run the provider chain for ``strategy_id``.

    ``providers`` maps provider-name -> callable(strategy, rendered_fields) that
    returns a validated output dict or ``None`` if unavailable (degrade). The
    chain is: the strategy's declared provider first (if a callable exists), then
    ``gateway``, then skip. Returns::

        {"provider": "codex-skill"|"gateway"|"skip", "output": dict|None,
         "warnings": [...]}
    """
    providers = dict(providers or {})
    strategy = load_strategy(strategy_id)
    declared = strategy.get("provider", "llm")
    rendered = {**fields}
    warnings: list[str] = []

    chain = []
    if declared in providers:
        chain.append(declared)
    if "gateway" in providers and "gateway" not in chain:
        chain.append("gateway")

    for name in chain:
        provider = providers[name]
        try:
            output = provider(dict(strategy), dict(rendered))
        except Exception as exc:  # a provider failure degrades to the next link
            warnings.append(f"策略 provider {name} 失败：{exc}")
            output = None
        if output is not None:
            ok, errs = validate_output(output, strategy)
            if ok:
                output.setdefault("strategyId", strategy_id)
                return {"provider": name, "output": output, "warnings": warnings}
            warnings.append(f"策略 provider {name} 输出不合规：{errs}")

    warnings.append("所有 provider 不可用，已跳过策略优化（可人工手写提示词）")
    return {"provider": "skip", "output": None, "warnings": warnings}
