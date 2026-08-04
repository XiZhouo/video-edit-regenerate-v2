"""ProviderHub 受管模块(规划 §3.4/§5.3)—— Build 拥有的「AI 接入」内核。

用户入口叫「AI 设置」,内部技术名 ProviderHub。这里是契约与解析地基(纯数据 + 纯函数):
调用方声明 CapabilityRequirement,resolve() 读当前用户 AccessConnection(投影自 System
CredentialStore,不建第二套明文)+ Build 注册的 ProviderDescriptor/ModelDefinition,算出
EffectiveCapabilitySnapshot(可用模型 + 9 类统一错误码 + 可解释 trace)。

首片只含契约与离线解析;真实派发接线(image_call 委托、GET /api/ai/options、CapabilityModelPicker)
在后续监督批落地(见 ZE-最终执行规划 §10 Batch 1)。
"""

from __future__ import annotations

from . import errors
from .types import (
    ProviderDescriptor, ModelDefinition, AccessConnection, ModelBinding,
    CapabilityRequirement, EffectiveCapabilitySnapshot, InvocationReceipt, capability_domain,
    DOMAIN_LLM, DOMAIN_IMAGE, DOMAIN_AGENT, DOMAIN_EMBEDDING, DOMAIN_FILE,
    CAP_TEXT_CHAT, CAP_AGENT_TOOL_USE, CAP_IMAGE_GENERATE, CAP_IMAGE_EDIT,
    CAP_IMAGE_MULTI_REFERENCE, CAP_IMAGE_ASPECT_RATIO, CAP_IMAGE_NATIVE_SIZE,
    CAP_IMAGE_RESOLUTION_TIER, CAP_EMBEDDING,
)
from .invocation_log import InvocationLog, receipt_from_sink, record_from_sink
from .preference_store import PreferenceStore
from .capability_lane_store import CapabilityLaneStore
from .discovered_model_store import DiscoveredModelStore
from .endpoint_guard import is_safe_registerable_endpoint, assert_resolves_public, guard_outbound_host, guarded_opener, GuardedRedirectHandler
from .descriptors import (
    all_provider_descriptors, descriptor_by_id, descriptor_for_provider,
    descriptor_by_route,
    all_model_definitions, model_by_id, models_for_capability,
    model_routes_map, legacy_alias_map,
)
from .connections import access_connection_from_public, project_connections
from .resolver import (
    resolve, resolve_image_route, resolve_image_routes_ordered, ROUTE_PREFERENCE,
    AUDIENCE_INTERNAL, AUDIENCE_PUBLIC, AUDIENCE_LOCAL,
)
from .serialize import binding_public_dict, snapshot_public_dict
from .probe import (
    probe_url, classify_http_status, ProbeResult, build_probe_request, is_probeable,
    discover_models, parse_discovered_models,
)

__all__ = [
    "errors",
    "ProviderDescriptor", "ModelDefinition", "AccessConnection", "ModelBinding",
    "CapabilityRequirement", "EffectiveCapabilitySnapshot", "InvocationReceipt",
    "InvocationLog", "receipt_from_sink", "record_from_sink",
    "PreferenceStore", "CapabilityLaneStore", "DiscoveredModelStore",
    "capability_domain",
    "DOMAIN_LLM", "DOMAIN_IMAGE", "DOMAIN_AGENT", "DOMAIN_EMBEDDING", "DOMAIN_FILE",
    "CAP_TEXT_CHAT", "CAP_AGENT_TOOL_USE", "CAP_IMAGE_GENERATE", "CAP_IMAGE_EDIT",
    "CAP_IMAGE_MULTI_REFERENCE", "CAP_IMAGE_ASPECT_RATIO", "CAP_IMAGE_NATIVE_SIZE",
    "CAP_IMAGE_RESOLUTION_TIER", "CAP_EMBEDDING",
    "all_provider_descriptors", "descriptor_by_id", "descriptor_for_provider",
    "descriptor_by_route",
    "all_model_definitions", "model_by_id", "models_for_capability",
    "model_routes_map", "legacy_alias_map",
    "access_connection_from_public", "project_connections",
    "resolve", "resolve_image_route", "resolve_image_routes_ordered", "ROUTE_PREFERENCE",
    "AUDIENCE_INTERNAL", "AUDIENCE_PUBLIC", "AUDIENCE_LOCAL",
    "binding_public_dict", "snapshot_public_dict",
    "probe_url", "classify_http_status", "ProbeResult", "build_probe_request", "is_probeable",
    "discover_models", "parse_discovered_models",
    "is_safe_registerable_endpoint", "assert_resolves_public", "guard_outbound_host",
]
