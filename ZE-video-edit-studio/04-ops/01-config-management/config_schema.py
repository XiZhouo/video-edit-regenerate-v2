#!/usr/bin/env python3
"""config_schema — ZE v3 配置旋钮的**唯一权威词表**(A 轨)。

对位 css 的 `03-css-rules/01-tokens/tokens.css`:tokens.css 是 `--ze-*` token
的权威词表、JSX 只能发这套词汇并由 `css_contract_check.py` 守一致;本文件是 `ZE_*` 运行
旋钮的权威词表、core/sdk 只能读这套 env 并由 `ops_contract_check.py` 守一致。

两类条目:
  - **env 旋钮**(env_overridable=True):部署时可经环境变量调的运行参数。检查器断言
    core/sdk 里出现的每个 `ZE_*` 字面量都被这里覆盖(exact 精确键 / prefix 前缀族)。
  - **固定常量**(env_overridable=False):写死在代码里的调优常量,**不**升成 env;只登记
    在此供「一眼看全」,不进检查器门禁。

match 语义(应对调研发现的三种读法):
  - `exact`  —— 字面量精确等于 env_var(如 `ZE_DATA_DIR`)。
  - `prefix` —— 任何以 env_var 开头的字面量都算被覆盖(应对 `_ENV_PREFIX + "..."` 拼接键、
                `"ZE_RB_" + scope.upper()` 动态 scope 族)。

scope:`framework`(框架运行参数)/`tool`(业务工具)/`pref`(用户偏好)/`secret`(密钥)。
本文件只放 framework 与少量固定常量;tool/secret 的通用组在 registry.seed_framework_config,
业务项由 mod 的 config_register.py 声明。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# 配置「种类」—— 决定校验方式 + 面板渲染控件
KIND_TEXT = "text"
KIND_PATH = "path"
KIND_INT = "int"
KIND_FLOAT = "float"
KIND_BOOL = "bool"
KIND_ENUM = "enum"
KIND_SECRET = "secret"
KIND_ENDPOINT = "endpoint"
KIND_MODEL_REF = "model-ref"
KIND_CONSTANT = "constant"   # 已记录固定常量(非 env)

VALID_KINDS = frozenset({
    KIND_TEXT, KIND_PATH, KIND_INT, KIND_FLOAT, KIND_BOOL, KIND_ENUM,
    KIND_SECRET, KIND_ENDPOINT, KIND_MODEL_REF, KIND_CONSTANT,
})

VALID_SCOPES = frozenset({"framework", "tool", "pref", "secret"})
VALID_MATCH = frozenset({"exact", "prefix"})


@dataclass(frozen=True)
class KnobSpec:
    """一个配置旋钮 / 一条已记录常量的声明。frozen → 词表不可变。"""

    key: str                       # 规范键(点分,如 'ze.data_dir');面板/registry 用
    env_var: str                   # 对应环境变量名(或前缀);constant 类填代码符号名
    kind: str                      # VALID_KINDS 之一
    default: Any                   # 默认值(None 表示"不设")
    label: str                     # 人读名(中文)
    desc: str                      # 一句话说明
    scope: str = "framework"
    match: str = "exact"           # exact | prefix
    env_overridable: bool = True   # False = 固定常量,不进门禁
    secret: bool = False           # True = 面板脱敏、值落 .data、默认源含 ZE-hupo-api/.env
    choices: Optional[tuple] = None      # enum/model-ref 的取值集
    consuming: str = ""            # 消费方(文件/模块),便于追溯

    def __post_init__(self) -> None:
        if self.kind not in VALID_KINDS:
            raise ValueError(f"未知 kind: {self.kind}({self.key})")
        if self.scope not in VALID_SCOPES:
            raise ValueError(f"未知 scope: {self.scope}({self.key})")
        if self.match not in VALID_MATCH:
            raise ValueError(f"未知 match: {self.match}({self.key})")


# ==============================================================
# § A 框架 env 旋钮(部署时可调;检查器门禁覆盖)
# ==============================================================

FRAMEWORK_KNOBS: tuple = (
    # —— 路径 / 持久化 ——
    KnobSpec("ze.data_dir", "ZE_DATA_DIR", KIND_PATH, None,
             "数据目录", "本 skill 所有项目数据/文件的根;默认 <skill>/.data,部署时指向兄弟数据目录",
             consuming="sdk/bootstrap.py:62"),
    KnobSpec("ze.users_path", "ZE_USERS_PATH", KIND_PATH, "~/.claude/ze_users.json",
             "账号库路径", "全局登录账号库;跨 skill 共享,一次登录到处通",
             consuming="sdk/bootstrap.py:276"),
    # —— 使用埋点(旁路遥测;空 = 关闭,零外连)——
    KnobSpec("ze.usage_collector_url", "ZE_USAGE_COLLECTOR_URL", KIND_ENDPOINT, None,
             "使用埋点采集器地址", "配了才把使用事件 push 到中心采集器(ZE-skill-system /api/usage/ingest);空=关闭",
             consuming="sdk/telemetry.py:41,server.py:48"),
    # —— 登录入口(部署级身份边界;项目配置与浏览器不能覆盖)——
    KnobSpec("ze.auth.mode", "ZE_AUTH_MODE", KIND_ENUM, "hybrid",
             "登录模式", "minimal-local=仅本地账号;federated=仅飞书;hybrid=两者并存",
             choices=("minimal-local", "federated", "hybrid"),
             consuming="auth.py:auth_runtime_config,server.py:/api/auth/config"),
    KnobSpec("ze.auth.registration", "ZE_AUTH_REGISTRATION", KIND_ENUM, "open",
             "本地账号注册", "open=允许创建本部署账号;closed=只允许已有账号登录;federated 模式强制关闭",
             choices=("open", "closed"),
             consuming="auth.py:auth_runtime_config,server.py:/api/auth/register"),
    KnobSpec("ze.auth.preferred_method", "ZE_AUTH_PREFERRED_METHOD", KIND_ENUM, "feishu",
             "默认登录入口", "hybrid 模式首屏使用 local 或 feishu;必须是当前模式允许的方法",
             choices=("local", "feishu"),
             consuming="auth.py:auth_runtime_config,LoginPage.tsx"),
    # —— 飞书 OAuth 登录(larkauth 代理;空 App ID/Secret = 使用 larkauth 服务端默认应用)——
    KnobSpec("ze.lark_auth_base_url", "ZE_LARK_AUTH_BASE_URL", KIND_ENDPOINT,
             "http://larkauth.tools.hupogames.com",
             "飞书认证代理服务地址;ZE 后端经 /auth/sse 连接,前端不直连",
             "ZE 后端代理连接该服务的 /auth/sse,成功后签发本地 session",
             consuming="server.py:76"),
    KnobSpec("ze.feishu_app_id", "ZE_FEISHU_APP_ID", KIND_SECRET, None,
             "自定义飞书 App ID", "可选;与 ZE_FEISHU_APP_SECRET 成对传给 larkauth,为空则用认证服务默认应用",
             scope="secret", secret=True, consuming="server.py:203"),
    KnobSpec("ze.feishu_app_secret", "ZE_FEISHU_APP_SECRET", KIND_SECRET, None,
             "自定义飞书 App Secret", "可选;只在 ZE 后端环境变量中使用,不进前端/OpLog/项目数据",
             scope="secret", secret=True, consuming="server.py:204"),
    # —— 资源预算:GPU 共享 ——
    KnobSpec("ze.rb.force_gpu", "ZE_RB_FORCE_GPU", KIND_TEXT, None,
             "强制 GPU 断言", "1/true/yes 或 0/false/no:人工断言有/无 GPU(自动探测不准时)",
             consuming="resource_budget.py:131"),
    KnobSpec("ze.rb.gpu_vram_mb", "ZE_RB_GPU_VRAM_MB", KIND_INT, None,
             "GPU 显存(MB)", "显式给值才探显存(如 amber 4090=24576);缺省保守 8192",
             consuming="resource_budget.py:163"),
    KnobSpec("ze.rb.lock_dir", "ZE_RB_LOCK_DIR", KIND_PATH, "/tmp/ze_rb",
             "资源锁目录", "机器级资源锁(多 skill 抢同一 GPU 时排队)",
             consuming="resource_budget.py:647,758"),
    # ZE_RB_<SCOPE>_{MAX_CONCURRENCY,RATE_PER_SEC,CAPACITY} —— 动态 scope 族,prefix 覆盖
    KnobSpec("ze.rb.scope_override", "ZE_RB_", KIND_TEXT, None,
             "单资源限流族", "ZE_RB_<域>_MAX_CONCURRENCY / _RATE_PER_SEC / _CAPACITY:按资源域限流",
             match="prefix", consuming="resource_budget.py:105,316,341,372"),
    # —— 容量护栏:ZE_CAPACITY_* ——
    KnobSpec("ze.capacity.entity_count_max", "ZE_CAPACITY_ENTITY_COUNT_MAX", KIND_INT, 10_000,
             "单文档最大实体数", "超过即拒(默认 1 万)", consuming="capacity.py:50"),
    KnobSpec("ze.capacity.entity_data_size_max", "ZE_CAPACITY_ENTITY_DATA_SIZE_MAX", KIND_INT, 1_000_000,
             "单实体最大字节", "单实体 JSON UTF-8 字节上限(默认 1MB)", consuming="capacity.py:51"),
    KnobSpec("ze.capacity.entity_field_count_max", "ZE_CAPACITY_ENTITY_FIELD_COUNT_MAX", KIND_INT, 100,
             "单实体最大字段数", "递归键总数上限(默认 100)", consuming="capacity.py:52"),
    KnobSpec("ze.capacity.asset_blob_size_max", "ZE_CAPACITY_ASSET_BLOB_SIZE_MAX", KIND_INT, 512 * 1024 * 1024,
             "单资产最大字节", "单 asset blob 上限(默认 512MB)", consuming="capacity.py:53"),
    KnobSpec("ze.capacity.doc_total_assets_size_max", "ZE_CAPACITY_DOC_TOTAL_ASSETS_SIZE_MAX", KIND_INT, 512 * 1024 * 1024,
             "单项目资产总字节", "本地唯一 asset blob 总额(默认 512MB)", consuming="capacity.py:54,230"),
    KnobSpec("ze.http.asset_upload_max_bytes", "ZE_HTTP_ASSET_UPLOAD_MAX_BYTES", KIND_INT, 32 * 1024 * 1024,
             "网页单次上传上限", "HTTP asset 流式读取硬上限(默认 32MB;大文件走分片协议)", consuming="server.py:_read_limited_asset_body"),
    KnobSpec("ze.sqlite.busy_timeout_ms", "ZE_SQLITE_BUSY_TIMEOUT_MS", KIND_INT, 5_000,
             "SQLite 写锁等待毫秒", "同机多进程争用写锁时的有界等待时间(默认 5000ms);不改变 WAL/FULL/local-filesystem 安全基线",
             consuming="01-core/02-data-flow/07-persistence/sqlite_kernel.py"),
    # 容量族前缀(应对 _ENV_PREFIX = "ZE_CAPACITY_" 拼接读法)
    KnobSpec("ze.capacity.prefix", "ZE_CAPACITY_", KIND_TEXT, None,
             "容量护栏族", "ZE_CAPACITY_* 前缀(capacity.py 经 _ENV_PREFIX 拼接读取)",
             match="prefix", env_overridable=True, consuming="capacity.py:47"),
    # —— 账号 / 身份权威目录(ZE-system 共享;auth.py / bootstrap 读)——
    KnobSpec("ze.system_auth_dir", "ZE_SYSTEM_AUTH_DIR", KIND_PATH, None,
             "身份权威目录", "ZE-system 共享的 users/sessions/policies/audit 目录;空=默认 ZE-skill-system/ZE-skill-hub-data/auth",
             consuming="auth.py:75"),
    KnobSpec("ze.super_admin_users", "ZE_SUPER_ADMIN_USERS", KIND_TEXT, None,
             "超级管理员名单", "逗号分隔 user_id;对所有工具拥有 edit 权限(角色/权限管理)",
             consuming="auth.py:186"),
    KnobSpec("ze.sessions_path", "ZE_SESSIONS_PATH", KIND_PATH, None,
             "会话库路径", "登录 session 落盘位置;空=身份目录/sessions.json", consuming="sdk/bootstrap.py:281"),
    KnobSpec("ze.policies_path", "ZE_POLICIES_PATH", KIND_PATH, None,
             "权限策略库路径", "工具级权限矩阵落盘;空=身份目录/policies.json", consuming="sdk/bootstrap.py:283"),
    KnobSpec("ze.auth_audit_path", "ZE_AUTH_AUDIT_PATH", KIND_PATH, None,
             "身份审计日志路径", "登录/权限变更 NDJSON 审计;空=身份目录/auth_audit.ndjson", consuming="sdk/bootstrap.py:284"),
    KnobSpec("ze.credential_master_key", "ZE_CREDENTIAL_MASTER_KEY", KIND_SECRET, None,
             "账号绑定密钥", "可选;用于封存 ZE Credential Vault 中的用户 API Key,为空则生成本机 key 文件",
             scope="secret", secret=True, consuming="credentials.py"),
    # —— 项目拓扑锁(仅无 flock 平台的显式降级)——
    KnobSpec("ze.topology.allow_single_process", "ZE_ALLOW_SINGLE_PROCESS_TOPOLOGY", KIND_BOOL, False,
             "允许单进程拓扑降级", "仅在平台没有 flock 时显式置 1；会放弃跨进程项目生命周期安全，生产保持 False",
             consuming="sdk/project_topology.py"),
    # —— 部署态硬门(start.sh 恒设 ZE_SERVER_MODE=1;进程级、非每项目可调,代码直读 os.environ)——
    KnobSpec("ze.server_mode", "ZE_SERVER_MODE", KIND_BOOL, False,
             "部署/多租户硬门", "1/true=部署态:强制 per-web-user 凭据绑定 fail-closed(server.py)+ 硬拒 MCP loopback-trust 仍要 token(mcp_bridge);由 start.sh 注入,本地开发默认 False",
             consuming="server.py,sdk/mcp_bridge.py"),
    # —— MCP surface(sdk/mcp_bridge.py;外部 agent 经 /mcp 标准协议驱动命令总线)——
    KnobSpec("ze.mcp.require_token", "ZE_MCP_REQUIRE_TOKEN", KIND_BOOL, True,
             "MCP 强制 token", "默认 True:任何 MCP 调用都要带 session token 并过 ACL;显式 0/false 才允许本机开发免 token",
             consuming="sdk/mcp_bridge.py:139"),
    KnobSpec("ze.mcp.allow_loopback_trust", "ZE_MCP_ALLOW_LOOPBACK_TRUST", KIND_BOOL, False,
             "MCP 本机免 token 开发开关", "1/true=直连 loopback 且无代理头时使用 __mcp__ 本机全权;生产保持 False",
             consuming="sdk/mcp_bridge.py:127"),
    KnobSpec("ze.mcp.bind", "ZE_MCP_BIND", KIND_TEXT, None,
             "MCP 绑定地址提示", "声明 MCP 端点对外暴露的 host;非 loopback 时自动等同 require_token(防越权)",
             consuming="sdk/mcp_bridge.py:142"),
    # —— 外发通知(notify 薄驱动 + notify.push 命令;飞书凭证复用 ze.feishu_*)——
    KnobSpec("ze.notify.dry_run", "ZE_NOTIFY_DRY_RUN", KIND_BOOL, True,
             "通知 dry-run", "True=只记 NotifyLog 不真发(安全默认);生产置 false 才真发飞书",
             consuming="sdk/notify.py"),
    KnobSpec("ze.notify.log_path", "ZE_NOTIFY_LOG_PATH", KIND_PATH, None,
             "通知审计日志路径", "外发通知 NDJSON(发送/失败/dry-run 留痕);空=身份目录/notify_audit.ndjson",
             consuming="sdk/bootstrap.py:285,sdk/notify.py:107"),
    KnobSpec("ze.notify.lark_cli_bin", "ZE_NOTIFY_LARK_CLI_BIN", KIND_PATH, None,
             "lark-cli 路径", "显式指定 lark-cli 可执行(绕 amber service PATH 漏 ~/.npm-global/bin);空=自动定位",
             consuming="sdk/notify.py"),
    KnobSpec("ze.notify.recipients", "ZE_NOTIFY_RECIPIENTS", KIND_TEXT, None,
             "收件人名册", "逗号分隔 key:open_id(或 key=open_id);notify.push 推荐选 ZE 用户,仍兼容名册 key",
             consuming="builtin_commands.py"),
    KnobSpec("ze.notify.recipient_policy", "ZE_NOTIFY_RECIPIENT_POLICY", KIND_ENUM, "roster_or_known",
             "收件人白名单策略", "roster=仅名册 / known_users=名册∪ZE-system 用户 / roster_or_known / any=任意 open_id(慎用)",
             choices=("roster", "known_users", "roster_or_known", "any"),
             consuming="builtin_commands.py"),
    KnobSpec("ze.notify.rate_limit", "ZE_NOTIFY_RATE_LIMIT", KIND_INT, 30,
             "通知限流(条/分钟)", "每用户每分钟最大发送数,防刷飞书(默认 30)", consuming="builtin_commands.py"),
    KnobSpec("ze.notify.deep_link_base", "ZE_NOTIFY_DEEP_LINK_BASE", KIND_ENDPOINT, None,
             "通知深链基址", "通知正文里回工具的链接基址,如 https://<host>;空=不附深链",
             consuming="mod"),
    # —— 审核角色(多审核人;消费方=审核类 mod,如 seek-find-level-audit-v3)——
    KnobSpec("ze.review.reviewer_open_ids", "ZE_REVIEW_REVIEWER_OPEN_IDS", KIND_TEXT, None,
             "审核人名单", "逗号分隔 open_id;登录命中即授 reviewer 角色,提交审核时扇出通知",
             consuming="mod"),
)


# ==============================================================
# § B 已记录固定常量(非 env;只为可发现性,不进门禁)
# ==============================================================

DOCUMENTED_CONSTANTS: tuple = (
    KnobSpec("const.sse_event_timeout_s", "SSE_EVENT_TIMEOUT", KIND_CONSTANT, 15.0,
             "SSE 事件超时(秒)", "server.py:399 单事件等待上限", env_overridable=False, consuming="server.py:399"),
    KnobSpec("const.sse_queue_max", "SSE_QUEUE_MAXSIZE", KIND_CONSTANT, 256,
             "SSE 队列上限", "server.py:373 /api/events 队列容量", env_overridable=False, consuming="server.py:373"),
    KnobSpec("const.lock_ttl_s", "ADVISORY_LOCK_TTL", KIND_CONSTANT, 300.0,
             "咨询锁 TTL(秒)", "lock.py:55 AdvisoryLockManager 默认 5 分钟", env_overridable=False, consuming="lock.py:55"),
    KnobSpec("const.queue_initial_eta_s", "INITIAL_ETA_SEC", KIND_CONSTANT, 5.0,
             "队列 ETA 初值(秒)", "queue.py:41 无样本时 ETA", env_overridable=False, consuming="queue.py:41"),
    KnobSpec("const.queue_samples_per_stage", "SAMPLES_PER_STAGE", KIND_CONSTANT, 20,
             "每 stage 历史样本数", "queue.py:42", env_overridable=False, consuming="queue.py:42"),
    KnobSpec("const.rate_poll_interval_s", "POLL_INTERVAL_SEC", KIND_CONSTANT, 0.05,
             "限速轮询间隔(秒)", "rate.py:31", env_overridable=False, consuming="rate.py:31"),
    KnobSpec("const.broker_replay_buffer", "REPLAY_BUFFER_SIZE", KIND_CONSTANT, 256,
             "实时 replay 缓冲", "broker.py:86 RealtimeBroker 重放缓冲", env_overridable=False, consuming="broker.py:86"),
    KnobSpec("const.session_ttl_s", "DEFAULT_SESSION_TTL_SEC", KIND_CONSTANT, 7 * 86400,
             "会话有效期(秒)", "auth.py:60 默认 7 天", env_overridable=False, consuming="auth.py:60"),
)


# ==============================================================
# § C 启动/前端 env(非 .py,门禁不扫;仅登记可发现)
# ==============================================================

LAUNCH_KNOBS: tuple = (
    KnobSpec("ze.port", "PORT", KIND_INT, 8000,
             "后端端口", "start.sh 读取;正式端口登记归 ZE-skill-deploy/ports.json(本表只读展示)",
             consuming="start.sh:10", env_overridable=True),
    KnobSpec("ze.vite_api_base", "VITE_API_BASE", KIND_ENDPOINT, "",
             "前端 API 基址", "前端 dev 指向后端;authApi.ts/lib/api.ts 读取",
             consuming="webapp/.../authApi.ts:12", env_overridable=True),
)


# ==============================================================
# 查询助手(检查器 + reader + 面板共用)
# ==============================================================

def all_specs() -> tuple:
    """全部声明(框架旋钮 + 固定常量 + 启动)。"""
    return FRAMEWORK_KNOBS + DOCUMENTED_CONSTANTS + LAUNCH_KNOBS


def gated_specs() -> tuple:
    """进检查器门禁的 env 旋钮(env_overridable=True 且非启动类——.py 可见)。"""
    return tuple(s for s in FRAMEWORK_KNOBS if s.env_overridable)


def exact_env_vars() -> set:
    """门禁:精确匹配的 env 变量名集。"""
    return {s.env_var for s in gated_specs() if s.match == "exact"}


def env_prefixes() -> tuple:
    """门禁:前缀族(应对拼接/动态 scope 键)。"""
    return tuple(s.env_var for s in gated_specs() if s.match == "prefix")


def is_covered(literal: str) -> bool:
    """门禁核心:一个 `ZE_*` 字面量是否被 schema 覆盖(精确键 或 前缀族)。"""
    if literal in exact_env_vars():
        return True
    return any(literal.startswith(p) for p in env_prefixes())


def by_scope(scope: str) -> tuple:
    return tuple(s for s in all_specs() if s.scope == scope)
