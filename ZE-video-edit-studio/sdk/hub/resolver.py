"""能力/模型解析(规划 §3.5)—— 唯一选择算法,纯函数,零 I/O、零费用、离线可测。

调用方声明 CapabilityRequirement(要什么能力),这里读当前用户 AccessConnection + Build
ProviderDescriptor/ModelDefinition,算出 EffectiveCapabilitySnapshot(每个候选模型一条
ModelBinding + 可解释 trace)。核心裁定:

- 选择优先级(§3.5 步6):pin > 私有覆盖 > 泳道 > 意图 > auto。
- pin 不可用 = **硬阻断**(current=None,返 model_unavailable/capability_mismatch),绝不静默
  换模型(§3.5 步7 / §13 / 并轨既有「显式路线不回退」裁定);auto 是唯一可兜底泳道。
- options 恒含全部候选(可用+不可用),让选择器「显示所有可能性、只区别显示能用的、只能选
  能用的」(呼应 F7 x-ze-enum-option-routes 语义)。
- 不可用 reason 归 9 类权威码之一,带 fix(一步到正确设置位置)。

依赖仅 types + errors + descriptors(不 import server/store/网络);可用性判据只读连接 public
面(status active),secret 只在真调用时经 store.reveal_secret 出——本层不碰。
"""

from __future__ import annotations

from typing import Optional

from . import errors
from .types import (
    AccessConnection, CapabilityRequirement, ModelBinding,
    EffectiveCapabilitySnapshot, capability_domain,
)
from .descriptors import (
    descriptor_by_id, descriptor_by_route, model_by_id, models_for_capability,
)

# Deploy audience:internal-service descriptor 仅在 internal profile 可见(public 隐藏组织服务)
AUDIENCE_INTERNAL = "internal"
AUDIENCE_PUBLIC = "public"
AUDIENCE_LOCAL = "local"

# 规范路线优先级(一模型多路线时挑哪条):与 model.py `_pick_image_route` 的固定 provider 顺序
# 逐字对齐——service(内网免费池化)> openai-api > gemini-api > codex-cli。让 resolver 的路由选择
# 与既有 auto 行为机械等价(避免按 served_by 声明顺序挑导致「同连 openai+codex 却换了条路」的分歧),
# 是 image_call 委托 resolver(B1-3)行为保持的前提。
ROUTE_PREFERENCE = ("service", "openai-api", "gemini-api", "codex-cli")


def _ordered_served_by(served_by: tuple[str, ...]) -> list[str]:
    """把模型的 served_by descriptor 按 ROUTE_PREFERENCE 排序(未列入的排后,保持稳定)。"""
    def key(desc_id: str) -> int:
        d = descriptor_by_id(desc_id)
        route = d.route if d else ""
        return ROUTE_PREFERENCE.index(route) if route in ROUTE_PREFERENCE else len(ROUTE_PREFERENCE)
    return sorted(served_by, key=key)


def _descriptor_visible(descriptor, deploy_audience: str) -> bool:
    """internal-service 在 public profile 不可见(规划 §4.3:C 端不出现组织内部渠道)。"""
    if descriptor is None:
        return False
    aud = str(descriptor.endpoint_policy.get("audience") or "")
    if aud == AUDIENCE_INTERNAL and deploy_audience == AUDIENCE_PUBLIC:
        return False
    return True


def _route_availability(descriptor, connections, capability, *, service_configured):
    """一条 served_by 路线是否可用 → (available, connection_id, reason)。

    内网服务(credential_provider="")凭 service_configured;个人路线凭当前用户有一条 active 且
    粗能力覆盖该 capability 的连接。status 由 CredentialStore 保证(active ⟹ 凭证可用),故这里
    只看 is_active,不重判 secret。"""
    if not descriptor.credential_provider:                 # 内网服务
        if service_configured:
            return True, None, None
        return False, None, errors.REQUEST_INVALID          # ze.image.service_url 未配
    # 个人路线:找该 provider 的连接
    conns = [c for c in connections if c.provider == descriptor.credential_provider]
    if not conns:
        return False, None, errors.REQUIRES_CONNECTION
    # 有连接但都不 active:needs_reauth → credential_expired;其余 → requires_connection
    active = [c for c in conns if c.is_active and capability in c.capabilities]
    if active:
        return True, active[0].connection_id, None
    if any(c.status == "needs_reauth" for c in conns):
        return False, conns[0].connection_id, errors.CREDENTIAL_EXPIRED
    # 连了但粗能力不覆盖该 capability(如只有 llm 的连接要 image)
    if any(c.is_active for c in conns) and not any(capability in c.capabilities for c in conns):
        return False, conns[0].connection_id, errors.CAPABILITY_MISMATCH
    return False, conns[0].connection_id, errors.REQUIRES_CONNECTION


def _build_binding(model, requirement, connections, *, service_configured, deploy_audience, trace):
    """给一个候选模型算 ModelBinding:遍历 served_by,第一条可用路线胜出;全不可用取最具信息量
    的 reason(capability_mismatch > credential_expired > requires_connection)。"""
    require = set(requirement.require)
    model_caps = set(model.capabilities)
    # 模型细能力不满足硬要求 → capability_mismatch(该模型做不到,不是没连接)
    if not require.issubset(model_caps):
        missing = sorted(require - model_caps)
        trace.append(f"model {model.model_id}: capability_mismatch(缺 {missing})")
        return ModelBinding(
            model_id=model.model_id, model_label=model.i18n_label,
            descriptor_id="", route="", connection_id=None, available=False,
            reason=errors.CAPABILITY_MISMATCH,
            fix=errors.make_fix(errors.CAPABILITY_MISMATCH,
                                feature=requirement.feature_id, missing=missing),
            capabilities=model.capabilities,
        )
    # 遍历能产出该模型的路线
    reason_rank = {errors.CAPABILITY_MISMATCH: 3, errors.CREDENTIAL_EXPIRED: 2,
                   errors.REQUIRES_CONNECTION: 1, errors.REQUEST_INVALID: 0}
    best_reason: Optional[str] = None
    best_conn: Optional[str] = None
    for desc_id in _ordered_served_by(model.served_by):   # 规范优先级挑路(所选=所调,与 auto 等价)
        desc = descriptor_by_id(desc_id)
        if not _descriptor_visible(desc, deploy_audience):
            continue
        ok, conn_id, reason = _route_availability(
            desc, connections, requirement.capability, service_configured=service_configured)
        if ok:
            trace.append(f"model {model.model_id}: available via {desc.route}")
            return ModelBinding(
                model_id=model.model_id, model_label=model.i18n_label,
                descriptor_id=desc_id, route=desc.route, connection_id=conn_id,
                available=True, reason=None, fix=None, capabilities=model.capabilities,
            )
        if reason is not None and reason_rank.get(reason, 0) >= reason_rank.get(best_reason, -1):
            best_reason, best_conn = reason, conn_id
    reason = best_reason or errors.REQUIRES_CONNECTION
    trace.append(f"model {model.model_id}: unavailable({reason})")
    # requires_connection/capability_mismatch → connect;credential_expired → reauth
    fix_provider = None
    for desc_id in model.served_by:
        d = descriptor_by_id(desc_id)
        if d and d.credential_provider:
            fix_provider = d.credential_provider
            break
    return ModelBinding(
        model_id=model.model_id, model_label=model.i18n_label,
        descriptor_id="", route="", connection_id=best_conn, available=False,
        reason=reason,
        fix=errors.make_fix(reason, capability=requirement.capability,
                            provider=fix_provider, model_id=model.model_id),
        capabilities=model.capabilities,
    )


def resolve(
    requirement: CapabilityRequirement,
    connections: tuple[AccessConnection, ...],
    *,
    service_configured: bool = False,
    pin: Optional[str] = None,
    intent: Optional[str] = None,
    preference: Optional[str] = None,
    lane: "tuple[str, ...]" = (),
    cooldown: "dict[str, str] | None" = None,
    deploy_audience: str = AUDIENCE_INTERNAL,
) -> EffectiveCapabilitySnapshot:
    """解析当前用户在当前功能下的可用模型投影。

    connections:当前用户的连接(接线层用 credential_store.list_for_user(user) 投影);
    service_configured:ze.image.service_url 是否已配;pin/intent/preference:见 §3.5 优先级;
    deploy_audience:public 时隐藏内网服务路线。返回可解释 EffectiveCapabilitySnapshot。"""
    trace: list[str] = [
        f"feature={requirement.feature_id} capability={requirement.capability} "
        f"require={list(requirement.require)} audience={deploy_audience} "
        f"connections={len(connections)} service={service_configured}"
    ]
    candidates = models_for_capability(requirement.capability)
    bindings = tuple(
        _build_binding(m, requirement, connections,
                       service_configured=service_configured,
                       deploy_audience=deploy_audience, trace=trace)
        for m in candidates
    )
    # §3.5 步5 健康冷却:近期实际失败的模型(接线层从 InvocationReceipt 算出 {model_id: error_code})
    # 解析期临时置灰(available=False + 该错误码),不静默、不等真调用才失败;窗口过了自然恢复。
    if cooldown:
        import dataclasses
        bindings = tuple(
            dataclasses.replace(b, available=False, reason=cooldown[b.model_id],
                                fix=errors.make_fix(cooldown[b.model_id], capability=requirement.capability))
            if (b.available and b.model_id in cooldown) else b
            for b in bindings
        )
        for _mid, _code in cooldown.items():
            trace.append(f"cooldown {_mid}: 近期失败({_code})→ 临时置灰")
    by_id = {b.model_id: b for b in bindings}
    available_ids = [b.model_id for b in bindings if b.available]

    current: Optional[str] = None
    preference_blocked: Optional[str] = None
    if pin is not None:
        # pin 优先且**硬阻断**:pin 不可用不静默换,current=None,让 UI 提示更换(§3.5 步7)
        pin_b = by_id.get(pin)
        if pin_b is None:
            trace.append(f"pin {pin}: 不在候选(能力域不符)→ 硬阻断")
        elif pin_b.available:
            current = pin
            trace.append(f"pin {pin}: 可用 → current")
        else:
            trace.append(f"pin {pin}: 不可用({pin_b.reason})→ 硬阻断,不换模型")
    elif preference:
        # preference 是**用户保存过的**每功能选择(「记住我的选择」写的就是它),所以和 pin 同待遇:
        # 规划 §3.5「保存过的 pin 失效,保留原值供用户看见,但禁用执行并提示更换;不能偷偷改项目数据」。
        #
        # 此前这里是软回退 —— preference 不可用就一路落到 lane→intent→auto[0]。那正是规划禁止的
        # 「偷偷换」:用户明确指定过某个模型,它没了,系统安静地用了另一个,结果里也看不出来。
        # lane/intent/auto 继续保持软回退是对的 —— 那三层本来就是「用户没表态时怎么挑」,
        # 而 preference 是用户表过态了。
        pref_b = by_id.get(preference)
        if pref_b is None:
            preference_blocked = errors.MODEL_UNAVAILABLE
            trace.append(f"preference {preference}: 不在候选(能力域不符)→ 硬阻断,不换模型")
        elif not pref_b.available:
            preference_blocked = pref_b.reason or errors.MODEL_UNAVAILABLE
            trace.append(f"preference {preference}: 不可用({pref_b.reason})→ 硬阻断,不换模型")
        else:
            current = preference
            trace.append(f"preference {preference}: 可用 → current")
    else:
        # 用户没表过态:全局能力泳道 > 意图 > auto(首个可用)—— 这三层软回退是设计如此
        if current is None:
            for m in lane:                        # 用户全局能力泳道:按声明序取首个可用
                if m in available_ids:
                    current = m
                    trace.append(f"lane {m}: 可用 → current(泳道序)")
                    break
        if current is None and intent and intent in available_ids:
            current = intent
            trace.append(f"intent {intent}: 可用 → current")
        if current is None and available_ids:
            current = available_ids[0]
            trace.append(f"auto → {current}")
        elif current is None:
            trace.append("auto:无可用模型")

    return EffectiveCapabilitySnapshot(
        feature_id=requirement.feature_id, capability=requirement.capability,
        options=bindings, pin=pin, current=current, trace=tuple(trace),
        preference=preference or None, preference_blocked=preference_blocked,
    )


def resolve_image_route(
    model_id: Optional[str],
    connections: tuple[AccessConnection, ...],
    *,
    service_configured: bool = False,
    model_routes: Optional[dict] = None,
    deploy_audience: str = AUDIENCE_INTERNAL,
) -> Optional[str]:
    """image_call auto 分支的路由选择——**model.py `_pick_image_route` 的行为镜像**,用
    AccessConnection(读投影)替 _find_credential、按 ROUTE_PREFERENCE 挑路。返回 route 字符串,
    或 None(无可用路线,对应 _pick_image_route 抛 requires_credential)。

    `model_routes[model_id]` = 能产该模型的路线集合(缺映射/未给 → allowed=None=全路线,同
    _pick_image_route)。这是 B1-3「image_call 委托 resolver」的落点:委托前先用差分测试证明它与
    _pick_image_route 在(config×凭证×model×model_routes)矩阵上机械等价(见 test_model 差分),
    等价后 image_call 只需把 _pick_image_route 换成本函数 + 连接投影,route 选择不变。"""
    ordered = resolve_image_routes_ordered(
        model_id, connections, service_configured=service_configured,
        model_routes=model_routes, deploy_audience=deploy_audience)
    return ordered[0] if ordered else None


def resolve_image_routes_ordered(
    model_id: Optional[str],
    connections: tuple[AccessConnection, ...],
    *,
    service_configured: bool = False,
    model_routes: Optional[dict] = None,
    deploy_audience: str = AUDIENCE_INTERNAL,
) -> "tuple[str, ...]":
    """P6 派发期 failover 地基:返回**全部可用**生图路线,按 ROUTE_PREFERENCE 有序
    (resolve_image_route 即其首元素)。auto 模式下 image_call 按此序逐条尝试,真调用失败(可重试类:
    429/超时/不可达)落下一条;pinned(显式路线)模式**不调本函数**——单路线硬失败,绝不静默换
    (no-silent-fallback,§3.5 步7 同源:只有 auto 才在候选间挑)。纯函数、零 I/O、可离线穷举测。"""
    allowed: Optional[set[str]] = None
    if model_id and isinstance(model_routes, dict):
        spec = model_routes.get(model_id)
        if isinstance(spec, (list, tuple)):
            allowed = {str(r) for r in spec}
        elif isinstance(spec, str):
            allowed = {spec}
    out: list[str] = []
    for route in ROUTE_PREFERENCE:
        if allowed is not None and route not in allowed:
            continue
        desc = descriptor_by_route(route)
        if desc is None or not _descriptor_visible(desc, deploy_audience):
            continue
        ok, _conn, _reason = _route_availability(
            desc, connections, "image", service_configured=service_configured)
        if ok:
            out.append(route)
    return tuple(out)
