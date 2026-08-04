"""ProviderHub 内核对象(规划 §3.4)—— 纯数据契约,零 I/O。

这是「AI 接入受管模块」的类型地基:调用方声明需要什么能力(CapabilityRequirement),
resolver 读当前用户连接(AccessConnection,投影自 System CredentialStore)+ Build 注册的
provider/model(ProviderDescriptor / ModelDefinition)算出可用选项(ModelBinding)+
可解释快照(EffectiveCapabilitySnapshot)。

设计红线(与规划 §3.3/§11.4 一致):
- AccessConnection 绝不携带 secret —— 只投影 public 元数据(has_secret/status/capabilities),
  密文只在真调用时经 CredentialStore.reveal_secret 出;本层不新建第二套明文凭证。
- ProviderDescriptor / ModelDefinition 是 Build 拥有的静态注册表,用户不可铸造;endpoint 允许
  清单归此处(SSRF 边界),不在各路由重造。
- 所有对象纯 dataclass、可离线单测;不 import server / store / 网络。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ── 能力标签 ────────────────────────────────────────────────────────────────
# 两层粒度(规划 §2.2 问题9 / 侦察 risk[9]):
#   粗粒度(provider 级,= CredentialStore.DEFAULT_CAPABILITIES 的值域):连接携带这层。
#   细粒度(model 级):CapabilityRequirement / ModelDefinition 用这层。
# 细→粗 domain 映射由 capability_domain() 收口,让「连接有 image 粗能力」可服务任意
# image.* 细能力的模型(受路由与 per-model 能力矩阵进一步约束)。

# 粗粒度域(与 credentials.DEFAULT_CAPABILITIES 对齐)
DOMAIN_LLM = "llm"
DOMAIN_IMAGE = "image"
DOMAIN_AGENT = "agent"
DOMAIN_EMBEDDING = "embedding"
DOMAIN_FILE = "file"

# 细粒度能力标签(model 级;require[] 用这层)
CAP_TEXT_CHAT = "text.chat"
CAP_AGENT_TOOL_USE = "agent.tool_use"
CAP_IMAGE_GENERATE = "image.generate"
CAP_IMAGE_EDIT = "image.edit"
CAP_IMAGE_MULTI_REFERENCE = "image.multi_reference"
CAP_IMAGE_ASPECT_RATIO = "image.aspect_ratio"
CAP_IMAGE_NATIVE_SIZE = "image.native_size"
CAP_IMAGE_RESOLUTION_TIER = "image.resolution_tier"
CAP_EMBEDDING = "embedding"


def capability_domain(tag: str) -> str:
    """细能力标签 → 粗粒度域(连接凭 coarse cap 判该域可服务)。

    `image.generate` → `image`;`text.chat` → `llm`;`agent.tool_use` → `agent`。
    未带点的标签视为已是粗粒度域,原样返回(向后兼容 `image`/`llm` 直接作 require)。"""
    if "." not in tag:
        return tag
    head = tag.split(".", 1)[0]
    return {"text": DOMAIN_LLM, "agent": DOMAIN_AGENT, "image": DOMAIN_IMAGE}.get(head, head)


# ── §3.4 内核对象 ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProviderDescriptor:
    """Build 拥有的 provider 静态描述(用户不可编辑)。

    `credential_provider` 映射到 credentials.PROVIDERS 之一(或 "" 表示无需凭证的内网服务);
    `route` 是既有 image 路线字面量(service/openai-api/gemini-api/codex-cli)——本层收编,
    避免路线字面量在 ≥5 处重复。`endpoint_policy` 是 SSRF 边界的唯一权威(base_url 允许清单)。"""
    id: str
    kind: str                       # internal-service | openai-compatible | anthropic | gemini | official-cli | video-task
    route: str                      # 既有 image 路线字面量(与 model.py 一致)
    credential_provider: str        # credentials.PROVIDERS 之一;"" = 内网服务(无用户凭证)
    capabilities: tuple[str, ...] = ()   # 粗粒度域集合(该 provider 能覆盖哪些 domain)
    endpoint_policy: dict[str, Any] = field(default_factory=dict)  # {base_url_allowlist, test_path, discovery_path}
    i18n_key: str = ""


@dataclass(frozen=True)
class ModelDefinition:
    """Build 注册的逻辑模型:把散在 foundry MODEL_OPTION_ROUTES / 04-ops IMAGE 常量里的
    模型真相收敛为唯一权威。`served_by` 是能产出该模型的 descriptor id 集合(一模型多路线:
    GPT Image 2 由 service/codex/openai 三条产出——泛化 MODEL_OPTION_ROUTES)。
    `capabilities` 是 per-model 细能力矩阵(承载 CredentialStore 粗 caps 推不出的
    image.edit/multi_reference 等;transparentBackground 是本地后处理,**不**是能力)。"""
    model_id: str
    i18n_label: str
    capabilities: tuple[str, ...]          # 细粒度能力标签
    served_by: tuple[str, ...]             # ProviderDescriptor.id 集合
    param_schema_ref: str = ""
    aliases: tuple[str, ...] = ()          # 旧 id → 本 canonical 的兼容别名(下游归一用,Build owns)
    limits: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AccessConnection:
    """当前用户的一条连接实例 —— 投影自 CredentialStore.public_dict(),**绝无 secret**。

    `kind`/`audience` 是派生值(由 descriptor 反查 + metadata 推,本片不落盘,规避严格 schema
    加列 = 存量 store 全拒载的雷)。`status` 是连接生命周期(active/needs_secret/needs_reauth/
    disabled),直接来自记录。判可用只看 status + has_secret + capabilities,不 reveal。"""
    connection_id: str
    provider: str
    kind: str                       # 由 descriptor 反查;缺省 "api_key"
    status: str                     # credentials.STATUS_*
    capabilities: tuple[str, ...]   # 粗粒度域集合(连接携带这层)
    has_secret: bool
    last4: str = ""
    audience: str = "default"       # public | internal | local(本片恒 default,持久化延后)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True)
class CapabilityRequirement:
    """调用方(工具功能)声明的硬要求(规划 §3.5)。加性泛化既有 requires_route 字符串:
    从「要哪条固定路线」升为「要哪种能力 + 哪些细能力标签」,由 resolver 解析成真实模型。"""
    feature_id: str                 # 如 foundry.image.generate
    capability: str                 # 主泳道:image | llm | agent | embedding
    require: tuple[str, ...] = ()    # 细粒度能力标签集合(模型须全覆盖)


@dataclass(frozen=True)
class ModelBinding:
    """resolver 产出的一个候选(不持久化)。`available=False` 时 `reason` 是 9 类错误码之一、
    `fix` 是「一步到正确设置位置」的修复入口(规划 §3.5/§3.6)。"""
    model_id: str
    model_label: str
    descriptor_id: str
    route: str
    connection_id: Optional[str]
    available: bool
    reason: Optional[str] = None            # HubErrorCode 之一(available=False 时)
    fix: Optional[dict[str, Any]] = None
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class EffectiveCapabilitySnapshot:
    """当前用户 × 当前环境 × 当前功能的可用模型投影(= GET /api/ai/options 返回体,
    /api/ai-routes 的严格超集)。`trace` 是可解释路由日志(规划 §3.5 第 8 步:返回完整
    可解释 trace)。`pin`/`current` 呼应既有 ai-routes 的 current 字段。"""
    feature_id: str
    capability: str
    options: tuple[ModelBinding, ...]
    pin: Optional[str] = None
    current: Optional[str] = None
    trace: tuple[str, ...] = ()
    # 用户保存过的每功能选择(「记住我的选择」)。失效时 current=None 且 preference_blocked 带原因码 ——
    # 规划 §3.5:保留原值供用户看见,但禁用执行并提示更换,不能偷偷换成别的模型。
    preference: Optional[str] = None
    preference_blocked: Optional[str] = None

    @property
    def has_available(self) -> bool:
        return any(o.available for o in self.options)


@dataclass(frozen=True)
class InvocationReceipt:
    """一次**真实** AI 调用的回执(规划 §3.4 line 197 / line 269):记录**实际落位**——实际连接、
    model ID、路线、参数摘要、延迟、结果、错误分类、成本引用——而非逻辑选择。

    执行元数据,**进独立日志(InvocationLog)不进 Operation 撤销栈**(editor-arch:只有数据变更
    是 Operation;API 延迟/token/错误日志走独立日志系统)。Commerce 消费其**脱敏事件**做计费
    (§3.4 line 429/514)。`turn_id` 贯通一轮请求(§3.4 line 566;未接线时 None)。

    脱敏红线:receipt **不含任何 secret**(无 secret 字段);`connection_id` 是引用非密文;
    `params_summary`/`result_summary` 必须是**摘要**(尺寸/张数/mime),caller 不得塞完整 prompt
    或图片 bytes。`idempotency_key` 用于去重——同键重复 record 只留一条(不重复记账)。"""
    receipt_id: str
    idempotency_key: str
    feature_id: str
    capability: str
    provider: str                        # 实际 provider descriptor id(非逻辑选择)
    model_id: str                        # 实际 model
    route: str                           # 实际路线 service/openai-api/gemini-api/codex-cli
    status: str                          # "ok" | "error"
    created_at: float                    # 时间戳(caller 传 time.time())
    connection_id: Optional[str] = None  # 实际连接引用(内网 service = None);非 secret
    turn_id: Optional[str] = None        # 贯通一轮(§3.4 line 566;未接线 None)
    latency_ms: Optional[int] = None
    params_summary: Optional[dict[str, Any]] = None   # 摘要(尺寸/张数),非完整 prompt
    result_summary: Optional[dict[str, Any]] = None   # mime/尺寸/输出引用,非 bytes
    error_code: Optional[str] = None     # 9 类统一错误码之一(status="error" 时)
    cost_ref: Optional[dict[str, Any]] = None         # 计费引用/预估方式(不承诺精确价格)
    provenance: Optional[str] = None     # provenance 标记(如原生工具调用已观测)
    actor_user_id: Optional[str] = None  # 归属用户(计费按此;非敏感,非 secret)
    trace: tuple[str, ...] = ()          # 解析轨迹(§3.5 第8步:派发经 resolve() 时落其 EffectiveCapabilitySnapshot.trace,让回执可解释「为何选中这条」);未接线=空,走 None-drop

    def public_dict(self) -> dict[str, Any]:
        """脱敏序列化(Commerce/前端消费)。本对象按构造即无 secret,此处只做 dict 化 +
        丢弃 None 空位,保持载荷紧凑。"""
        raw = {
            "receipt_id": self.receipt_id,
            "idempotency_key": self.idempotency_key,
            "feature_id": self.feature_id,
            "capability": self.capability,
            "provider": self.provider,
            "model_id": self.model_id,
            "route": self.route,
            "status": self.status,
            "created_at": self.created_at,
            "connection_id": self.connection_id,
            "turn_id": self.turn_id,
            "latency_ms": self.latency_ms,
            "params_summary": self.params_summary,
            "result_summary": self.result_summary,
            "error_code": self.error_code,
            "cost_ref": self.cost_ref,
            "provenance": self.provenance,
            "actor_user_id": self.actor_user_id,
            "trace": list(self.trace) or None,   # 空轨迹走 None-drop,保持载荷紧凑
        }
        return {k: v for k, v in raw.items() if v is not None}
