"""Build 拥有的 Provider / Model 注册表(规划 §3.3:descriptor/模型定义归 Build)。

把散在多处的路线与模型真相收敛为**唯一权威**:
- 路线字面量(service/openai-api/gemini-api/codex-cli)原散在 model.py、server.py、foundry
  MODEL_OPTION_ROUTES ≥5 处 → 这里一处。
- 模型→路线集合(foundry MODEL_OPTION_ROUTES)→ ModelDefinition.served_by 泛化(一模型多路线)。
- provider 能力(credentials.DEFAULT_CAPABILITIES 粗粒度)→ descriptor.capabilities 的 seed 参照。

endpoint 允许清单(SSRF 边界,规划 §4.4)归 descriptor,不在各路由重造;用户不可铸造 descriptor。
本片聚焦 image 域(foundry 是第一黄金消费者)+ 主 LLM/agent provider;后续批加视频/嵌入等。
"""

from __future__ import annotations

from credentials import (  # provider 字符串的唯一来源(与 model.py 同源,防漂移)
    PROVIDER_OPENAI_API, PROVIDER_ANTHROPIC_API, PROVIDER_GEMINI_API,
    PROVIDER_CODEX_CLI, PROVIDER_CLAUDE_CLI,
)

from .types import (
    ProviderDescriptor, ModelDefinition,
    DOMAIN_LLM, DOMAIN_IMAGE, DOMAIN_AGENT,
    CAP_TEXT_CHAT, CAP_AGENT_TOOL_USE,
    CAP_IMAGE_GENERATE, CAP_IMAGE_EDIT, CAP_IMAGE_MULTI_REFERENCE,
    CAP_IMAGE_ASPECT_RATIO, CAP_IMAGE_NATIVE_SIZE, CAP_IMAGE_RESOLUTION_TIER,
)

# descriptor id(= 既有 image 路线字面量;LLM-only 的用 provider 名派生)
DESC_SERVICE = "service"
DESC_OPENAI_API = "openai-api"
DESC_GEMINI_API = "gemini-api"
DESC_CODEX_CLI = "codex-cli"
DESC_ANTHROPIC_API = "anthropic-api"
DESC_CLAUDE_CLI = "claude-cli"

# 内网服务的 base_url 来自 ze.image.service_url(组织配置、非用户输入),故无 SSRF 允许清单;
# 用户可填 endpoint 的 provider 才需 allowlist(§4.4)。
_PROVIDER_DESCRIPTORS: tuple[ProviderDescriptor, ...] = (
    ProviderDescriptor(
        id=DESC_SERVICE, kind="internal-service", route="service",
        credential_provider="",                    # 内网服务无用户凭证
        capabilities=(DOMAIN_IMAGE, DOMAIN_LLM),
        endpoint_policy={"config_key": "ze.image.service_url", "audience": "internal"},
        i18n_key="ai.provider.service",
    ),
    ProviderDescriptor(
        id=DESC_OPENAI_API, kind="openai-compatible", route="openai-api",
        credential_provider=PROVIDER_OPENAI_API,
        capabilities=(DOMAIN_LLM, DOMAIN_IMAGE),
        endpoint_policy={"base_url_allowlist": ("api.openai.com",),
                         "test_path": "/v1/models", "discovery_path": "/v1/models"},
        i18n_key="ai.provider.openai",
    ),
    ProviderDescriptor(
        id=DESC_GEMINI_API, kind="gemini", route="gemini-api",
        credential_provider=PROVIDER_GEMINI_API,
        capabilities=(DOMAIN_LLM, DOMAIN_IMAGE),
        endpoint_policy={"base_url_allowlist": ("generativelanguage.googleapis.com",)},
        i18n_key="ai.provider.gemini",
    ),
    ProviderDescriptor(
        id=DESC_CODEX_CLI, kind="official-cli", route="codex-cli",
        credential_provider=PROVIDER_CODEX_CLI,
        capabilities=(DOMAIN_LLM, DOMAIN_AGENT, DOMAIN_IMAGE),
        endpoint_policy={"official_login": True},   # 官方设备登录;无用户填 endpoint
        i18n_key="ai.provider.codex",
    ),
    ProviderDescriptor(
        id=DESC_ANTHROPIC_API, kind="anthropic", route="anthropic-api",
        credential_provider=PROVIDER_ANTHROPIC_API,
        capabilities=(DOMAIN_LLM,),
        endpoint_policy={"base_url_allowlist": ("api.anthropic.com",)},
        i18n_key="ai.provider.anthropic",
    ),
    ProviderDescriptor(
        id=DESC_CLAUDE_CLI, kind="official-cli", route="claude-cli",
        credential_provider=PROVIDER_CLAUDE_CLI,
        capabilities=(DOMAIN_LLM, DOMAIN_AGENT),
        endpoint_policy={"official_login": True},
        i18n_key="ai.provider.claude",
    ),
)

_DESCRIPTOR_BY_ID = {d.id: d for d in _PROVIDER_DESCRIPTORS}
_DESCRIPTOR_BY_PROVIDER = {d.credential_provider: d for d in _PROVIDER_DESCRIPTORS if d.credential_provider}
_DESCRIPTOR_BY_ROUTE = {d.route: d for d in _PROVIDER_DESCRIPTORS}

# 逻辑模型注册:image 域首片(foundry 两模型,served_by 泛化 MODEL_OPTION_ROUTES)。
_MODEL_DEFINITIONS: tuple[ModelDefinition, ...] = (
    ModelDefinition(
        model_id="gemini3", i18n_label="Banana Pro",
        capabilities=(CAP_IMAGE_GENERATE, CAP_IMAGE_EDIT, CAP_IMAGE_MULTI_REFERENCE,
                      CAP_IMAGE_ASPECT_RATIO, CAP_IMAGE_RESOLUTION_TIER),
        served_by=(DESC_SERVICE, DESC_GEMINI_API),          # = MODEL_OPTION_ROUTES gemini3
        param_schema_ref="foundry.banana",
        aliases=("nano-banana-pro", "gemini", "gemini31flash"),  # = foundry MODEL_VISIBLE_ALIASES gemini3
    ),
    ModelDefinition(
        model_id="fal-gpt-image-2", i18n_label="GPT Image 2",
        capabilities=(CAP_IMAGE_GENERATE, CAP_IMAGE_EDIT, CAP_IMAGE_NATIVE_SIZE),
        served_by=(DESC_SERVICE, DESC_CODEX_CLI, DESC_OPENAI_API),  # = MODEL_OPTION_ROUTES fal-gpt-image-2
        param_schema_ref="foundry.gpt",
        aliases=("gpt-image-2", "openai", "openai-15", "openai-mini"),  # = foundry MODEL_VISIBLE_ALIASES gpt
    ),
    # agent 域(AI 助手 §7:agent.tool_use):官方 CLI 智能体引擎。model_id 对齐 ze.ai.copilot_engine
    # 的 claude/codex 取值(alias 收口),让「打开助手前查 agent.tool-use」的预检(P0-B)有可解析候选。
    ModelDefinition(
        model_id="claude-code", i18n_label="Claude Code",
        capabilities=(CAP_AGENT_TOOL_USE, CAP_TEXT_CHAT),
        served_by=(DESC_CLAUDE_CLI,),          # 官方设备登录的 Claude CLI 智能体
        param_schema_ref="",
        aliases=("claude",),                   # ze.ai.copilot_engine="claude"
    ),
    ModelDefinition(
        model_id="codex", i18n_label="Codex",
        capabilities=(CAP_AGENT_TOOL_USE, CAP_TEXT_CHAT),
        served_by=(DESC_CODEX_CLI,),           # 官方设备登录的 Codex CLI 智能体
        param_schema_ref="",
        aliases=(),                            # ze.ai.copilot_engine="codex"(model_id 即取值)
    ),
)

_MODEL_BY_ID = {m.model_id: m for m in _MODEL_DEFINITIONS}


def all_provider_descriptors() -> tuple[ProviderDescriptor, ...]:
    return _PROVIDER_DESCRIPTORS


def descriptor_by_id(descriptor_id: str) -> ProviderDescriptor | None:
    return _DESCRIPTOR_BY_ID.get(descriptor_id)


def descriptor_for_provider(provider: str) -> ProviderDescriptor | None:
    """由 credentials provider 字符串反查 descriptor(连接投影派生 kind/route 用)。"""
    return _DESCRIPTOR_BY_PROVIDER.get(provider)


def descriptor_by_route(route: str) -> ProviderDescriptor | None:
    """由 image 路线字面量(service/openai-api/…)反查 descriptor(resolve_image_route 用)。"""
    return _DESCRIPTOR_BY_ROUTE.get(route)


def all_model_definitions() -> tuple[ModelDefinition, ...]:
    return _MODEL_DEFINITIONS


def model_by_id(model_id: str) -> ModelDefinition | None:
    return _MODEL_BY_ID.get(model_id)


def models_for_capability(capability: str) -> tuple[ModelDefinition, ...]:
    """返回主泳道(image/llm/…)下的候选模型:模型任一细能力的 domain == capability。"""
    from .types import capability_domain
    return tuple(
        m for m in _MODEL_DEFINITIONS
        if any(capability_domain(c) == capability for c in m.capabilities)
    )


def model_routes_map(capability: str) -> dict[str, list[str]]:
    """{model_id: [route,...]} —— 某泳道每个模型的 served_by 路线集合(descriptor.route)。
    下游(foundry)派生 MODEL_OPTION_ROUTES 用,不再手抄 served_by。"""
    out: dict[str, list[str]] = {}
    for m in models_for_capability(capability):
        routes: list[str] = []
        for d in m.served_by:
            desc = _DESCRIPTOR_BY_ID.get(d)
            if desc is not None:
                routes.append(desc.route)
        out[m.model_id] = routes
    return out


def legacy_alias_map(capability: str) -> dict[str, str]:
    """{旧别名: canonical model_id} —— 某泳道所有模型的兼容别名并集(Build owns)。
    下游归一旧 id 用(取代 foundry 手抄的 MODEL_VISIBLE_ALIASES)。"""
    out: dict[str, str] = {}
    for m in models_for_capability(capability):
        for a in m.aliases:
            out[a] = m.model_id
    return out
