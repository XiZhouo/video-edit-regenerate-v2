"""ProviderHub resolver 能力矩阵(规划 §6.2A 无费用契约矩阵)—— 纯离线、零网络、零真费用。

用 fixtures 构造「连接态 × 模型 × require × profile × pin」组合,钉死 §3.5 解析裁定:
所选=所调、pin 不可用硬阻断不静默换、9 类错误码、可用性只读连接 public 面。这是把统一解析
算法钉在 Build 的退出门(后续接线批只准委托它,不准再生第二套选择逻辑)。

§6.2A 表格 11 行 → 守它的测试(**映射写在这里,免得下次还要重新考古**):
  1. 无任何连接              → test_no_connection_public_all_unavailable
                              + test_snapshot_public_dict_guest_empty_all_requires_connection
  2. 仅 Codex                → test_codex_only_gpt_available_banana_not
  3. 仅 Gemini API           → test_gemini_only_banana_available_gpt_not
  4. 仅 OpenAI API           → test_openai_only_gpt_available
  5. internal + internal     → test_internal_service_internal_profile_both_available
  6. internal + public       → test_internal_service_public_profile_hidden
  7. 主A+备B auto,A 失败转B → sdk/test_model.py::test_image_auto_failover_on_unreached
                              (反面:no_failover_on_http_error / no_failover_on_timeout —— 只对
                               「未到达 provider」的失败换渠道,避免双扣费)
  8. pin A,A 失败不 fallback → test_pin_unavailable_hard_blocks_no_silent_swap
                              + sdk/test_model.py::test_image_route_explicit_no_fallback
  9. 凭证过期                → test_needs_reauth_maps_credential_expired
 10. /models 不支持          → test_discovery_unsupported_keeps_descriptor_models_available
 11. 免登录 guest(demo)     → test_guest_has_no_options_and_no_persisted_traces
"""

from __future__ import annotations

import sys
from pathlib import Path

# 门禁 _pytest_code 会注入 sdk 与 01-core 叶子路径;直跑时补 Build root 头,让 `import sdk` 生效。
_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import sdk  # noqa: E402,F401  (side-effect: _paths 注入 credentials 等叶子路径)
from credentials import (  # noqa: E402
    PROVIDER_CODEX_CLI, PROVIDER_OPENAI_API, PROVIDER_GEMINI_API, PROVIDER_ANTHROPIC_API,
    STATUS_ACTIVE, STATUS_NEEDS_REAUTH,
)
from sdk.hub import (  # noqa: E402
    resolve, resolve_image_route, resolve_image_routes_ordered,
    project_connections, CapabilityRequirement, errors,
    CAP_IMAGE_GENERATE, CAP_IMAGE_MULTI_REFERENCE, CAP_IMAGE_NATIVE_SIZE,
    AUDIENCE_INTERNAL, AUDIENCE_PUBLIC, snapshot_public_dict,
)

GEN = CapabilityRequirement(feature_id="foundry.image.generate", capability="image",
                            require=(CAP_IMAGE_GENERATE,))


def _pub(provider, *, status=STATUS_ACTIVE, caps=("image",), has_secret=True, cid=None):
    """构造一条 CredentialRecord.public_dict() 形状(绝不含 secret)。"""
    return {"id": cid or f"conn-{provider}", "provider": provider, "label": provider,
            "capabilities": list(caps), "status": status, "metadata": {},
            "last4": "1234", "has_secret": has_secret}


def _conns(*pubs):
    return project_connections(pubs)


def _by_id(snap):
    return {o.model_id: o for o in snap.options}


# ── 连接态矩阵(§6.2A)──────────────────────────────────────────────────────

def test_no_connection_public_all_unavailable():
    snap = resolve(GEN, _conns(), service_configured=False, deploy_audience=AUDIENCE_PUBLIC)
    assert snap.current is None
    assert not snap.has_available
    for o in snap.options:
        assert not o.available and o.reason == errors.REQUIRES_CONNECTION
        assert o.fix and o.fix.get("action") == "connect"


def test_codex_only_gpt_available_banana_not():
    snap = resolve(GEN, _conns(_pub(PROVIDER_CODEX_CLI)),
                   service_configured=False, deploy_audience=AUDIENCE_PUBLIC)
    b = _by_id(snap)
    # GPT Image 2 由 codex 产出 → 可用;Banana Pro(service/gemini)→ 无连接不可用
    assert b["fal-gpt-image-2"].available and b["fal-gpt-image-2"].route == "codex-cli"
    assert not b["gemini3"].available and b["gemini3"].reason == errors.REQUIRES_CONNECTION
    assert snap.current == "fal-gpt-image-2"          # auto 落到唯一可用


def test_gemini_only_banana_available_gpt_not():
    snap = resolve(GEN, _conns(_pub(PROVIDER_GEMINI_API)),
                   service_configured=False, deploy_audience=AUDIENCE_PUBLIC)
    b = _by_id(snap)
    assert b["gemini3"].available and b["gemini3"].route == "gemini-api"
    assert not b["fal-gpt-image-2"].available
    assert snap.current == "gemini3"


def test_openai_only_gpt_available():
    snap = resolve(GEN, _conns(_pub(PROVIDER_OPENAI_API)),
                   service_configured=False, deploy_audience=AUDIENCE_PUBLIC)
    b = _by_id(snap)
    assert b["fal-gpt-image-2"].available and b["fal-gpt-image-2"].route == "openai-api"
    assert not b["gemini3"].available


def test_internal_service_internal_profile_both_available():
    snap = resolve(GEN, _conns(), service_configured=True, deploy_audience=AUDIENCE_INTERNAL)
    b = _by_id(snap)
    assert b["gemini3"].available and b["gemini3"].route == "service"
    assert b["fal-gpt-image-2"].available and b["fal-gpt-image-2"].route == "service"
    assert snap.current is not None


def test_internal_service_public_profile_hidden():
    # 内网服务在 public profile 不可见 → 无个人连接则全不可用(规划 §4.3)
    snap = resolve(GEN, _conns(), service_configured=True, deploy_audience=AUDIENCE_PUBLIC)
    assert not snap.has_available
    for o in snap.options:
        assert o.reason == errors.REQUIRES_CONNECTION


# ── pin 语义(§3.5 步7:不可用硬阻断,不静默换)────────────────────────────

def test_pin_available_lands_on_pin():
    snap = resolve(GEN, _conns(_pub(PROVIDER_CODEX_CLI)), deploy_audience=AUDIENCE_PUBLIC,
                   pin="fal-gpt-image-2")
    assert snap.current == "fal-gpt-image-2" and snap.pin == "fal-gpt-image-2"


def test_pin_unavailable_hard_blocks_no_silent_swap():
    # 只连 codex(能产 GPT),却 pin Banana(要 service/gemini)→ 硬阻断:current=None,
    # 绝不悄悄换成可用的 GPT;pin 保留可见,其它模型仍在 options(供用户改选)。
    snap = resolve(GEN, _conns(_pub(PROVIDER_CODEX_CLI)), deploy_audience=AUDIENCE_PUBLIC,
                   pin="gemini3")
    assert snap.current is None                        # 硬阻断
    assert snap.pin == "gemini3"                        # pin 保留
    b = _by_id(snap)
    assert not b["gemini3"].available                   # pin 目标不可用
    assert b["fal-gpt-image-2"].available               # 可用模型仍呈现,但没被偷偷选中


# ── 失效态 ──────────────────────────────────────────────────────────────────

def test_needs_reauth_maps_credential_expired():
    snap = resolve(GEN, _conns(_pub(PROVIDER_CODEX_CLI, status=STATUS_NEEDS_REAUTH)),
                   deploy_audience=AUDIENCE_PUBLIC)
    b = _by_id(snap)
    assert not b["fal-gpt-image-2"].available
    assert b["fal-gpt-image-2"].reason == errors.CREDENTIAL_EXPIRED
    assert b["fal-gpt-image-2"].fix.get("action") == "reauth"


def test_anthropic_only_has_no_image_source_and_says_connect():
    """P0-E「仅 Claude」:连了 Anthropic 但它不产图 → 一个图像模型都不可用。

    理由必须是 REQUIRES_CONNECTION 而不是 CAPABILITY_MISMATCH,尽管用户明明已经连了东西 ——
    这条码的定义原文就是「未连接任何**能满足该能力**的来源」,而 Anthropic 满足不了 image。
    修复动作 `connect`(去连一个能出图的)也因此是对的。

    钉住它,是因为「已连接却说要你去连接」看着像 bug,很容易被后人「顺手改成
    capability_mismatch」—— 那条码的语义是「连接/模型不具备要求的**细**能力」
    (如 openai 连接只声明了 llm),两者管的不是一回事,换了就把「去连个能出图的」
    这个正确指引换成了「你的连接缺个细能力」这种没法照做的话。
    """
    snap = resolve(GEN, _conns(_pub(PROVIDER_ANTHROPIC_API)),
                   service_configured=False, deploy_audience=AUDIENCE_PUBLIC)
    assert snap.current is None
    assert not snap.has_available
    for o in snap.options:
        assert not o.available
        assert o.reason == errors.REQUIRES_CONNECTION
        assert o.fix and o.fix.get("action") == "connect"


def test_connection_without_image_cap_is_capability_mismatch():
    # openai 连接但只声明 llm(无 image)→ 对 GPT(served by openai)是能力不匹配
    snap = resolve(GEN, _conns(_pub(PROVIDER_OPENAI_API, caps=("llm",))),
                   deploy_audience=AUDIENCE_PUBLIC)
    b = _by_id(snap)
    assert not b["fal-gpt-image-2"].available
    assert b["fal-gpt-image-2"].reason == errors.CAPABILITY_MISMATCH


def test_impossible_require_is_capability_mismatch():
    # 单模型无法同时满足 multi_reference(仅 Banana)+ native_size(仅 GPT)→ 全 capability_mismatch
    req = CapabilityRequirement(feature_id="x.image", capability="image",
                                require=(CAP_IMAGE_MULTI_REFERENCE, CAP_IMAGE_NATIVE_SIZE))
    snap = resolve(req, _conns(_pub(PROVIDER_CODEX_CLI)), service_configured=True,
                   deploy_audience=AUDIENCE_INTERNAL)
    for o in snap.options:
        assert not o.available and o.reason == errors.CAPABILITY_MISMATCH
    assert snap.current is None


# ── 优先级(§3.5 步6:pin > 私有覆盖 > 意图 > auto)────────────────────────

def test_intent_honored_over_auto_first():
    # 两模型都可用(内网服务),意图指 GPT → current 跟意图,不落 auto 首个
    snap = resolve(GEN, _conns(), service_configured=True, deploy_audience=AUDIENCE_INTERNAL,
                   intent="fal-gpt-image-2")
    assert snap.current == "fal-gpt-image-2"


def test_preference_overrides_intent():
    snap = resolve(GEN, _conns(), service_configured=True, deploy_audience=AUDIENCE_INTERNAL,
                   preference="gemini3", intent="fal-gpt-image-2")
    assert snap.current == "gemini3"


def test_options_always_show_all_candidates():
    # 「显示所有可能性、只区别显示能用的」:无论连接态,options 恒含全部候选模型
    snap = resolve(GEN, _conns(), deploy_audience=AUDIENCE_PUBLIC)
    assert {o.model_id for o in snap.options} == {"gemini3", "fal-gpt-image-2"}


def test_trace_is_explainable():
    snap = resolve(GEN, _conns(_pub(PROVIDER_CODEX_CLI)), deploy_audience=AUDIENCE_PUBLIC)
    assert snap.trace and any("available via codex-cli" in t for t in snap.trace)


def test_route_preference_tiebreak_openai_over_codex():
    # 同连 openai+codex、无 service,选 GPT Image 2(served_by service/codex/openai)→ 按
    # ROUTE_PREFERENCE(service>openai>gemini>codex)挑 openai-api,不按 served_by 声明序落 codex。
    # 这与 model._pick_image_route 固定 provider 顺序对齐(image_call 委托行为保持的关键)。
    snap = resolve(GEN, _conns(_pub(PROVIDER_OPENAI_API), _pub(PROVIDER_CODEX_CLI)),
                   service_configured=False, deploy_audience=AUDIENCE_PUBLIC)
    gpt = next(o for o in snap.options if o.model_id == "fal-gpt-image-2")
    assert gpt.available and gpt.route == "openai-api"


# ── 错误码归一(§3.5:前后端一次成型)──────────────────────────────────────

def test_legacy_code_normalization():
    assert errors.normalize_code("not_bound") == errors.REQUIRES_CONNECTION
    assert errors.normalize_code("needs_reauth") == errors.CREDENTIAL_EXPIRED
    assert errors.normalize_code("version_blocked") == errors.MODEL_UNAVAILABLE
    assert errors.normalize_code("model_scope_denied") == errors.POLICY_DENIED
    assert errors.normalize_code(errors.RATE_LIMITED) == errors.RATE_LIMITED
    assert errors.normalize_code(None) is None


# ── 能力泳道(§3.5 步6 第三级:pin > preference > lane > intent > auto)────────
_BOTH = dict(service_configured=True, deploy_audience=AUDIENCE_INTERNAL)  # 内网服务→两模型都可用


def test_lane_honors_declared_order():
    # 泳道按声明序取首个可用;换序 → 换选中,证明按泳道序而非 auto 固定序裁定
    a = resolve(GEN, _conns(), lane=("fal-gpt-image-2", "gemini3"), **_BOTH)
    b = resolve(GEN, _conns(), lane=("gemini3", "fal-gpt-image-2"), **_BOTH)
    assert a.current == "fal-gpt-image-2"
    assert b.current == "gemini3"


def test_lane_skips_unavailable_takes_next():
    # 泳道首选不可用(只连 codex,gemini3 要 service/gemini)→ 跳过取下一可用 GPT,不报错
    snap = resolve(GEN, _conns(_pub(PROVIDER_CODEX_CLI)), deploy_audience=AUDIENCE_PUBLIC,
                   lane=("gemini3", "fal-gpt-image-2"))
    assert snap.current == "fal-gpt-image-2"


def test_preference_overrides_lane():
    # 私有默认(preference)优先级高于全局泳道
    snap = resolve(GEN, _conns(), preference="fal-gpt-image-2",
                   lane=("gemini3", "fal-gpt-image-2"), **_BOTH)
    assert snap.current == "fal-gpt-image-2"


def test_lane_overrides_intent():
    # 全局泳道优先级高于本次意图
    snap = resolve(GEN, _conns(), lane=("gemini3",), intent="fal-gpt-image-2", **_BOTH)
    assert snap.current == "gemini3"


# ── 健康冷却(§3.5 步5:近期实际失败的模型解析期临时置灰)──────────────────────
def test_cooldown_greys_model_with_reason():
    snap = resolve(GEN, _conns(), cooldown={"gemini3": errors.RATE_LIMITED}, **_BOTH)
    g = _by_id(snap)["gemini3"]
    assert not g.available and g.reason == errors.RATE_LIMITED


def test_cooldown_selection_skips_cooled_to_healthy():
    # gemini3 正冷却(不可用)→ current 只可能落健康的 GPT,绝不选中冷却模型
    snap = resolve(GEN, _conns(), cooldown={"gemini3": errors.RATE_LIMITED}, **_BOTH)
    assert snap.current == "fal-gpt-image-2"
    assert snap.current != "gemini3"


def test_cooldown_pin_hard_blocks_no_silent_swap():
    # pin 到正冷却的模型 → 硬阻断(current=None),不静默换健康模型;pin 保留可见(§3.5 步7)
    snap = resolve(GEN, _conns(), pin="gemini3",
                   cooldown={"gemini3": errors.RATE_LIMITED}, **_BOTH)
    assert snap.current is None and snap.pin == "gemini3"
    assert not _by_id(snap)["gemini3"].available


# ── P6a:有序候选(派发期 failover 地基)────────────────────────────────────────
def test_image_routes_ordered_all_available_in_preference_order():
    # 连 openai+codex(无 service)→ 有序返回 [openai-api, codex-cli](ROUTE_PREFERENCE:service>openai>gemini>codex)
    conns = _conns(_pub(PROVIDER_OPENAI_API), _pub(PROVIDER_CODEX_CLI))
    ordered = resolve_image_routes_ordered(None, conns, service_configured=False,
                                           deploy_audience=AUDIENCE_PUBLIC)
    assert ordered == ("openai-api", "codex-cli")


def test_image_route_equals_first_of_ordered():
    # resolve_image_route 恒等于有序首元素(委托一致,防漂移)
    conns = _conns(_pub(PROVIDER_OPENAI_API), _pub(PROVIDER_CODEX_CLI))
    ordered = resolve_image_routes_ordered(None, conns, deploy_audience=AUDIENCE_PUBLIC)
    assert resolve_image_route(None, conns, deploy_audience=AUDIENCE_PUBLIC) == ordered[0]


def test_image_route_none_when_ordered_empty():
    conns = _conns()   # 无连接、无 service
    assert resolve_image_routes_ordered(None, conns, deploy_audience=AUDIENCE_PUBLIC) == ()
    assert resolve_image_route(None, conns, deploy_audience=AUDIENCE_PUBLIC) is None


def test_image_routes_ordered_model_routes_filter():
    # model_routes 限定某模型只能走 codex → 有序只含 codex(其余可用路线被 model 约束滤掉)
    conns = _conns(_pub(PROVIDER_OPENAI_API), _pub(PROVIDER_CODEX_CLI))
    ordered = resolve_image_routes_ordered("m", conns, model_routes={"m": ["codex-cli"]},
                                           deploy_audience=AUDIENCE_PUBLIC)
    assert ordered == ("codex-cli",)


def test_image_routes_ordered_service_first_when_configured():
    # 配了内网服务 → service 排最前(ROUTE_PREFERENCE 首),failover 从它开始
    conns = _conns(_pub(PROVIDER_OPENAI_API))
    ordered = resolve_image_routes_ordered(None, conns, service_configured=True,
                                           deploy_audience=AUDIENCE_INTERNAL)
    assert ordered[0] == "service" and "openai-api" in ordered


# ── /api/ai/options 序列化(EffectiveCapabilitySnapshot → JSON)──────────────

def test_snapshot_public_dict_shape_and_json():
    import json
    snap = resolve(GEN, _conns(_pub(PROVIDER_CODEX_CLI)), deploy_audience=AUDIENCE_PUBLIC)
    d = snapshot_public_dict(snap)
    # 结构:恒含全部候选、current、has_available、trace
    assert d["feature_id"] == GEN.feature_id and d["capability"] == "image"
    assert {o["model_id"] for o in d["options"]} == {"gemini3", "fal-gpt-image-2"}
    assert d["has_available"] is True and d["current"] == "fal-gpt-image-2"
    assert json.dumps(d)                                  # 全 JSON 可序列化
    # 不可用项带 9 类码 + i18n key;可用项 reason=None
    gemini = next(o for o in d["options"] if o["model_id"] == "gemini3")
    assert gemini["available"] is False
    assert gemini["reason"] == errors.REQUIRES_CONNECTION
    assert gemini["reason_i18n"] == "ai.error.requires_connection"
    gpt = next(o for o in d["options"] if o["model_id"] == "fal-gpt-image-2")
    assert gpt["available"] is True and gpt["reason"] is None and gpt["route"] == "codex-cli"


def test_snapshot_public_dict_guest_empty_all_requires_connection():
    # 访客(无连接)→ /api/ai/options 返只读态:全 requires_connection,current None(F6 空态)
    d = snapshot_public_dict(resolve(GEN, _conns(), deploy_audience=AUDIENCE_PUBLIC))
    assert d["current"] is None and d["has_available"] is False
    assert all(o["reason"] == errors.REQUIRES_CONNECTION for o in d["options"])



# ── §6.2A 发现失败的三种形态(零网络:直接让 urlopen 抛,验非抛错风格的 (ids, code) 返回)──
def urllib_error_http():
    import urllib.error
    return urllib.error.HTTPError("http://x/models", 404, "Not Found", {}, None)


def urllib_error_url():
    import urllib.error
    return urllib.error.URLError("unreachable")


def _discover_with(probe_mod, exc):
    """stub urlopen 抛给定异常,跑 discover_models,还原后返回 (ids, code)。"""
    import urllib.request
    original = urllib.request.urlopen

    def _boom(*_a, **_k):
        raise exc

    urllib.request.urlopen = _boom
    try:
        return probe_mod.discover_models("http://example.invalid/v1/models", kind="openai-compatible")
    finally:
        urllib.request.urlopen = original


# ══════════════════════════════════════════════════════════════════════════════
# §6.2A 无费用契约矩阵 —— 补齐最后两行(其余九行由本文件上方与 sdk/test_model.py 覆盖:
#   主A+备B auto 转 B / pin 不 fallback / bad-request 不换渠道
#   → test_image_auto_failover_on_unreached · test_image_auto_no_failover_on_http_error
#     · test_image_route_explicit_no_fallback)
# 全部零网络、零费用:stub 掉发现调用,只验裁定。
# ══════════════════════════════════════════════════════════════════════════════

def test_discovery_unsupported_keeps_descriptor_models_available():
    """§6.2A「`/models` 不支持 → descriptor/manual 模型仍可用,明确『未自动发现』」。

    机制:发现结果落**独立分层** DiscoveredModelStore(source='discovered'),从不并进
    models_for_capability 的静态表。所以 /models 返 404/超时/坏 JSON 都只让「自动发现」这一路失败,
    descriptor 声明的模型照常可解析 —— 发现是**增益**,不是可用性的前提。"""
    from sdk.hub import probe
    # 三种「不支持」形态各跑一次:HTTP 错 / 网络不可达 / 坏 JSON —— 都必须是非抛错的 (空列表, 错误码)
    for exc, expect_code in (
        (urllib_error_http(), errors.MODEL_UNAVAILABLE),
        (urllib_error_url(), errors.PROVIDER_UNREACHABLE),
        (ValueError("bad json"), errors.REQUEST_INVALID),
    ):
        ids, code = _discover_with(probe, exc)
        assert ids == [], "发现失败不得伪造模型"
        assert code is not None, "发现失败必须给出错误码(而非静默空)"

    # 关键契约:发现全挂,descriptor 模型依然可用(用户能照常出图)
    snap = resolve(GEN, _conns(_pub(PROVIDER_GEMINI_API)), deploy_audience=AUDIENCE_PUBLIC)
    assert snap.has_available, "发现不可用不该影响 descriptor 声明的模型"
    assert any(o.available for o in snap.options)


def test_guest_has_no_options_and_no_persisted_traces(tmp_path):
    """§6.2A「免登录 guest(demo)→ 无模型选项;显示注册+连接引导;生成动作点击前禁用」。

    两条契约:①解析层 —— guest 没有连接,故全部候选不可用且每项带 requires_connection + fix
    (前端据此禁用生成并给引导,不是空白页);②存储层 —— guest 的偏好/泳道/发现**一律不落盘**
    (访客态不该在磁盘留下可被下一个访客读到的痕迹)。"""
    from sdk.hub import PreferenceStore, CapabilityLaneStore, DiscoveredModelStore

    snap = resolve(GEN, _conns(), deploy_audience=AUDIENCE_PUBLIC)
    d = snapshot_public_dict(snap)
    assert d["has_available"] is False and d["current"] is None
    for o in d["options"]:
        assert o["available"] is False
        assert o["reason"] == errors.REQUIRES_CONNECTION      # 不是「未知错误」
        assert o["fix"], "每个不可用项都要给修复入口(引导去连接)"

    for uid in ("", "__demo"):                                 # 空 uid 与 '__' 前缀都是访客
        p = PreferenceStore(tmp_path / "pref.json")
        p.set(uid, "foundry.image.generate", "image", "gemini3")
        assert p.get(uid, "foundry.image.generate", "image") is None
        assert not (tmp_path / "pref.json").exists(), "访客偏好不得落盘"

        lane = CapabilityLaneStore(tmp_path / "lane.json")
        lane.set(uid, "image", ["gemini3"])
        assert lane.get(uid, "image") == ()
        assert not (tmp_path / "lane.json").exists(), "访客泳道不得落盘"

        disc = DiscoveredModelStore(tmp_path / "disc.json")
        disc.record(uid, "conn-x", ["m1"])
        assert disc.list_for_credential(uid, "conn-x") == []
        assert not (tmp_path / "disc.json").exists(), "访客发现结果不得落盘"


# ---------------------------------------------------------------
# 元测试:本文件顶部那张「§6.2A 11 行 → 守它的测试」映射必须一直是真的
# ---------------------------------------------------------------

def test_matrix_docstring_mapping_points_at_tests_that_exist():
    """docstring 里点名的每个测试都必须真实存在。

    那张映射的存在理由是「免得下次还要重新考古」——可它只是散文,谁改个测试名它就开始撒谎,
    而且撒得毫无声息。这条把它钉成机器可验的:改名/删测试时,这里当场红。

    §6.2A 的第 7、8 行故意指向 sdk/test_model.py(失败转移与 pin 不 fallback 是**调用侧**行为,
    不在 resolver 里),所以两个文件都要扫。
    """
    import re
    from pathlib import Path

    here = Path(__file__)
    doc = here.read_text(encoding="utf-8")
    header = doc.split('"""')[1]

    # 只取真正的测试名:排除 `sdk/test_model.py` 这类模块路径引用
    named = {
        name for name in re.findall(r"\btest_[a-z0-9_]+", header)
        if not re.search(rf"{re.escape(name)}\.py", header)
    }
    assert len(named) >= 11, f"映射里点名的测试少得可疑:{sorted(named)}"

    defined = set(re.findall(r"^def (test_[a-z0-9_]+)", doc, re.M))
    model_path = here.parent.parent / "test_model.py"
    if model_path.is_file():
        defined |= set(re.findall(
            r"^def (test_[a-z0-9_]+)", model_path.read_text(encoding="utf-8"), re.M))

    missing = sorted(named - defined)
    assert not missing, (
        f"§6.2A 映射点名了不存在的测试(改名或删除后忘了同步顶部映射):{missing}")


def test_matrix_covers_every_declared_row():
    """11 行每行都要有守它的测试 —— 行号连续、不缺号。"""
    import re
    from pathlib import Path
    header = Path(__file__).read_text(encoding="utf-8").split('"""')[1]
    rows = {int(n) for n in re.findall(r"^\s*(\d+)\.\s", header, re.M)}
    assert rows == set(range(1, 12)), f"§6.2A 行号应为 1..11,实际 {sorted(rows)}"


# ── 保存过的选择失效 = 硬阻断(§3.5「不能偷偷改项目数据」)──────────────────
#
# 规划原话:「如果保存过的 pin 失效,保留原值供用户看见,但禁用执行并提示更换;不能偷偷改项目数据」。
# 用户能保存的那个东西是 preference(「记住我的选择」写的就是它),所以这条规则落在 preference 上。
# 此前 preference 不可用会一路软回退到 lane→intent→auto[0] —— 用户明确指定过的模型没了,
# 系统安静换了一个,结果里还看不出来。这一组把那条路堵死。

def test_saved_preference_unavailable_blocks_instead_of_substituting():
    # 只连 codex(gemini3 要 service/gemini 路线 → 不可用),保存的偏好正是 gemini3
    snap = resolve(GEN, _conns(_pub(PROVIDER_CODEX_CLI)), deploy_audience=AUDIENCE_PUBLIC,
                   preference="gemini3")
    assert snap.current is None, "保存过的选择不可用时不许换成别的模型"
    assert snap.preference == "gemini3", "原值必须留着给用户看见"
    assert snap.preference_blocked, "必须给出为什么用不了"


def test_blocked_preference_does_not_fall_through_to_lane_or_intent():
    # 同时给了 lane 和 intent 且它们都可用 —— 仍然不许拿它们顶替失效的保存值
    snap = resolve(GEN, _conns(_pub(PROVIDER_CODEX_CLI)), deploy_audience=AUDIENCE_PUBLIC,
                   preference="gemini3", lane=("fal-gpt-image-2",), intent="fal-gpt-image-2")
    assert snap.current is None
    assert snap.preference_blocked


def test_blocked_preference_reason_is_one_of_the_nine_codes():
    snap = resolve(GEN, _conns(_pub(PROVIDER_CODEX_CLI)), deploy_audience=AUDIENCE_PUBLIC,
                   preference="gemini3")
    assert snap.preference_blocked in errors.HUB_ERROR_CODES


def test_preference_outside_the_capability_domain_also_blocks():
    snap = resolve(GEN, _conns(), preference="not-a-real-model", **_BOTH)
    assert snap.current is None
    assert snap.preference_blocked == errors.MODEL_UNAVAILABLE


def test_available_preference_still_wins_and_is_not_marked_blocked():
    snap = resolve(GEN, _conns(), preference="gemini3", **_BOTH)
    assert snap.current == "gemini3"
    assert snap.preference == "gemini3"
    assert snap.preference_blocked is None


def test_lane_intent_auto_keep_soft_fallback():
    """只有 preference 改成硬阻断 —— 那三层本来就是「用户没表态时怎么挑」,必须继续软回退。

    这条是防过度收紧:把回退全砍了会让没保存过偏好的用户在首选不可用时直接不能用。
    """
    lane = resolve(GEN, _conns(_pub(PROVIDER_CODEX_CLI)), deploy_audience=AUDIENCE_PUBLIC,
                   lane=("gemini3", "fal-gpt-image-2"))
    assert lane.current == "fal-gpt-image-2", "泳道首选不可用应取下一个"
    intent = resolve(GEN, _conns(_pub(PROVIDER_CODEX_CLI)), deploy_audience=AUDIENCE_PUBLIC,
                     intent="gemini3")
    assert intent.current == "fal-gpt-image-2", "意图不可用应落 auto"


def test_blocked_preference_is_visible_in_the_serialized_payload():
    """前端只看 JSON —— 字段没序列化出去,UI 就无法「保留原值 + 置灰 + 提示」。"""
    from sdk.hub.serialize import snapshot_public_dict
    snap = resolve(GEN, _conns(_pub(PROVIDER_CODEX_CLI)), deploy_audience=AUDIENCE_PUBLIC,
                   preference="gemini3")
    payload = snapshot_public_dict(snap)
    assert payload["current"] is None
    assert payload["preference"] == "gemini3"
    assert payload["preference_blocked"] in errors.HUB_ERROR_CODES
