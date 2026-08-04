# 03-authentication — Authentication

> 一句话：**部署可选登录入口 + 本地 session**。公司环境可只用飞书，独立团队可只用本地账号，也可两者并存；三种入口最终都签发同一种 session，再还原 Actor 给 CommandRegistry。

## 在框架里的位置

- 上一级：[04-user-facing/](../) — 用户层 3 个叶子
- 同级：[01-layout/](../01-layout/) / [02-interaction/](../02-interaction/)
- 改动：[01-command/](../../03-commands-operations/01-command/) 加 `enforce_login` 参数
- 协议：跟 [02-entity/](../../01-data-structure/02-entity/) 的 `Actor` 直接对接

## 本叶子文件

| 文件 | 用途 |
|---|---|
| [README.md](./README.md) | 本文件（薄导览）|
| [SPEC.md](./SPEC.md) | 11 节完整规则（User / UserStore / SessionManager / 集成）|
| [auth.py](./auth.py) | 登录模式合同 + User/UserStore + 外部身份 upsert + SessionManager + hash 工具 + build_actor |
| [credentials.py](./credentials.py) | 当前 ZE 用户的外部账号绑定 vault（API Key / Codex / Claude profile）|
| [test_auth.py](./test_auth.py) | pytest（52 测试，含端到端集成）|
| [test_credentials.py](./test_credentials.py) | pytest（账号绑定隔离 / secret 封存 / owner scope）|
| [test_credentials_atomic.py](./test_credentials_atomic.py) | 凭据表严格读取、原子提交、故障注入和跨进程 lost-update 证据 |
| `projectMutationIntent.ts` | 项目 create/duplicate/delete/demo-reset 的稳定业务意图、浏览器暂存与结果对账 |

## 当前状态

**✅ 已实装**（无跨叶子依赖项全做）

- `User` dataclass — user_id + password_hash/password_salt(兼容旧账号) + display_name/email/avatar_url + auth_provider/external_id + created_at + roles
- `hash_password / verify_password` — PBKDF2-SHA256(210,000 次) + 随机 salt + hmac.compare_digest；旧 SHA256 账号登录后惰性迁移
- `AuthRuntimeConfig` — `minimal-local` / `federated` / `hybrid` 三种部署模式；`/api/auth/config` 只公开可用入口，不公开密钥和路径
- `LoginPage` — 按服务端能力展示本地登录/注册、飞书登录或切换入口；后端路由重复校验同一模式
- `UserStore(path=None)` — 持久化到 `~/.claude/ze_users.json`（atomic 写）；register / authenticate / get / exists / list_users
- `UserStore.upsert_external_user(provider="feishu", external_id=open_id, ...)` — 飞书用户映射到稳定本地 `user_id="feishu:<open_id>"`
- `SessionManager(ttl_sec=7d, path=None)` — session 表；服务态传 `.data/sessions.json` 持久化，测试/临时态可纯内存；login / logout / get_user_id / is_valid / cleanup_expired
- `build_actor(user)` — User → Actor 桥
- `CredentialStore` — 绑定外部账号到当前登录用户；密钥写入后不回显；每次写都在跨进程锁内严格重读，再以唯一临时文件 + fsync + atomic replace 提交整份 `credentials.json`
- `credential_context` — HTTP 命令 / pipeline stage 的用户凭证上下文，供 `sdk.llm_call` 优先使用当前登录人的绑定
- `CommandRegistry(enforce_login=False)` — 默认 False；启用后空 user_id + source≠system 抛 `auth_required`
- 防枚举：用户不存在 / 密码错统一 `auth_failed` + 同 message
- audit gates 10 个 — GATE-AUTH-01..10
- 项目写意图 — 32hex intent + opaque Idempotency-Key；Create/Duplicate 预分配 target；
  intent/outcome 同时绑定服务端 principal、target 与 kind。HTTP 408、未知 4xx/5xx、响应合同
  缺失默认都是 outcome unknown。只有 wire v2、exact target/kind、`principal_scoped=true` 的
  `not_committed` 才授权同身份的一次有界重放；`intent_reserved` 单独说明 exact intent 是否已有
  耐久认领。false 只是当前 GET 的时点观察，不是租约，真正 POST 仍须原子认领并可能输掉竞争。
- 浏览器主体 fence — 每次 follow-up 固定发起时 Bearer token、owner partition 与 Provider
  生命周期；切号/卸载后只保留原主体 tombstone，绝不借新账号继续对账或重放。
- 原生 transport 身份 — 通用旧接口仍兼容 Bearer > cookie > query，但框架只为
  `EventSource` 主动生成 session query，并在该 transport 内优先于共享 SSO cookie。
  `<img>/<video>` 不复用这条路：
  页面以 Bearer/cookie 批量换取短期、单资产媒体票据，URL 不出现主 session token；实际 GET
  仍重验 durable ACL、项目 generation 与共享引用。
- 存储隔离 — project/recent key 包含稳定 tool/deployment namespace 与登录 uid（匿名 demo
  使用仅限浏览器 UI 状态的 guest storage id）；mutation intent 另用服务端 HttpOnly cookie
  派生的 opaque guestScope。两者不混权，同源部署的不同编辑器不能互相恢复项目或消费回执。
- 匿名 demo 身份 — 只读模板请求先建立 HttpOnly guest cookie；首个 mutation 不能同时创建
  身份与 sandbox；sandbox owner 必须精确匹配当前 cookie，`__demo__` 前缀不构成授权。当前 tab
  用 sessionStorage 记住“正在体验”并用本地 project key 恢复同一隐藏沙盒；刷新不重置 demo，
  cookie 变化或权威文档拒绝时仍会清除候选并回到选择页。

**⏳ 待补**（业务方需求驱动）

- 改密码 API + 旧 session 失效
- `doc.acl` 真校验（role-based）
- 跨设备 session 与未解决项目意图接力（W06 事务请求表；当前浏览器用 sessionStorage）
- union_id 映射（当前服务只返回 open_id；如 larkauth 后续返回 union_id，应优先作为跨应用稳定锚点）

详见 [SPEC.md §11](./SPEC.md#11-当前实装状态).

## 关键设计决策

- **飞书 open_id = 登录主键**：ZE 内部 user_id 写成 `feishu:<open_id>`，避免跟旧本地 username 冲突
- **登录入口由部署决定**：`ZE_AUTH_MODE=minimal-local|federated|hybrid`；项目文件和浏览器不能改写身份边界
- **minimal-local 的邮箱只是登录名**：统一小写并校验邮箱形状，但当前不发验证邮件、不宣称邮箱已验证
- **ZE session 继续保留**：飞书只负责证明身份；项目 ACL、OpLog、CommandRegistry 仍消费 ZE 自己的 session 和 Actor
- **外部账号不塞进 User**：`users.json` 只表示“这个人是谁”；API Key / Codex / Claude profile 进独立 `credentials.json`，通过 owner_user_id 关联
- **个人绑定优先，服务配置兜底**：用户触发的 AI/外部服务先查本人 CredentialStore；缺失时才回落 `ze.llm.*` 这类工具级配置
- **密钥写入型 UI**：前端只显示 provider/status/last4/last_used；secret 字段保存后永不回显
- **凭据表只有一个提交边界**：record 与 sealed secret 同在一份 `credentials.json`；任一坏 record、坏 secret、丢引用或孤儿 secret 都让整份表 fail-closed，绝不“跳过坏行后继续运行”
- **返回值不是后门**：list/get/upsert/mark_verified 返回独立副本；调用方修改 `metadata` 或 record 字段不会偷改 vault 内存
- **密码 PBKDF2-SHA256 + 随机 salt**：纯标准库实现；新账号使用 210,000 次迭代，旧 SHA256 记录只读兼容并在成功登录后升级
- **session 可持久化**：浏览器 token 存 localStorage，服务端 `.data/sessions.json` 保留未过期 token；同一台电脑重开页面/服务重启后不用反复登录
- **防枚举**：`auth_failed` 不区分"用户不存在 / 密码错"；同一 message 防被遍历用户名
- **enforce_login 默认 False**：v3 现有 526 测试不破坏；业务方启用变 True
- **`source=system` 例外**：admin / 框架自动化任务不需登录态
- **未登录读放行**：框架只拦写（dispatch）；读（doc.get / list / find）任何人随便看

## 怎么用

### 飞书 OAuth + 用 token 还原 Actor → dispatch

```python
from auth import UserStore, SessionManager, build_actor
from command import CommandRegistry
from entity import Actor

# 1. 全局用户表 (默认 ~/.claude/ze_users.json)
store = UserStore()

# 2. 飞书 OAuth 服务推回 user_info 后，按 open_id upsert
user = store.upsert_external_user(
    provider="feishu",
    external_id=user_info["open_id"],
    display_name=user_info.get("name", ""),
    email=user_info.get("email", ""),
    avatar_url=user_info.get("avatar_url", ""),
)
session_mgr = SessionManager(path=data_dir / "sessions.json")
token = session_mgr.login(user)
# token 给前端 (cookie / localStorage / Authorization header)

# 4. 后续每个请求: 从 header 拿 token → 还原 user_id → 造 actor
def handle_request(request):
    token = request.headers.get("X-Session-Token", "")
    user_id = session_mgr.get_user_id(token)
    if not user_id:
        return 401   # 未登录
    user = store.get(user_id)
    actor = build_actor(user)
    # dispatch
    reg.dispatch(command_id, args, actor=actor, source="user", doc=doc, op_log=op_log)
```

### 业务方启用 enforce_login（推荐）

```python
reg = CommandRegistry(enforce_login=True)
# 之后所有 source!=system 的 dispatch 都要求 actor.user_id 非空
# web layer 兜底; 即使前端忘了 401 检查, dispatch 也拦
```

### 当前用户绑定外部账号

```python
from credentials import CredentialStore

vault = CredentialStore()
rec = vault.upsert_for_user(
    "feishu:ou_xxx",
    provider="hupo_gateway_key",
    label="我的网关",
    secret="sk-...",
    metadata={"base_url": "https://api.ai.hupo.games/v1"},
)

# 公共 API / 前端只能拿 metadata,不会拿到 secret。
public = rec.public_dict()

# 运行时由 CredentialResolver / sdk.llm_call 在当前 actor 上下文中读取。
secret = vault.reveal_secret("feishu:ou_xxx", rec.id)
```

#### 凭据保存到底保证什么

一次 `upsert / delete / set_default / mark_used / mark_verified` 的顺序固定为：取得
`credentials.json.lock` → 从磁盘严格读出当前完整版本 → 在局部副本中修改 record 与 secret →
校验所有字段、引用和密文 MAC → 写入本目录下的随机唯一临时文件 → flush + 文件 fsync →
atomic replace → 父目录 fsync → 最后一次性切换进程内快照。

- replace 前任一步失败：目标文件和调用前内存都保持旧版本，可以安全重试；错误码
  `credential_store_write_failed`。
- replace 已成功、父目录 fsync 报真实 I/O 错误：磁盘当前是完整新文件，内存也跟随新版本，
  但掉电耐久性无法确认；错误码 `credential_store_durability_unknown`，不得当作“没保存”盲重试。
- 已存在的文件只接受 schema `1.0.0` 的当前完整形状。仓内没有更早的 credential schema；
  未知版本需要显式迁移器，不能靠默认值猜测。旧 1.0 实现生成的完整文件可直接重启读取；
  旧实现允许的“长度大于 32 字节 key、取前 32 字节”行为继续兼容。

这里有三个**明确分开的文件聚合**：`credentials.json`（record + sealed secret）、
`credentials.key`（首次封存前创建、之后只读的 host key）、CLI home 内的 `auth.json` /
`.credentials.json`（CLI 自己会刷新的 OAuth 文件）。后两类文件自身也使用唯一临时文件、fsync 和
atomic replace，不再用 `O_TRUNC` 半写；但三者目前没有跨文件事务。比如“CLI auth 已写成功、
metadata record 写失败”仍可能留下可回收的孤儿 home。成熟的多进程版本应由 W06 action journal /
reconciler 记录 prepare/finalize 状态，而不是假装三文件能由一次 rename 原子提交。

用户填写说明统一放在工作台设置菜单「我的 AI 账号」：

- API Key 类：填写 provider 允许的 HTTPS Base URL（可选）和 API Key；保存后只显示状态和尾号
- Codex / Claude CLI：通过设备登录或完整 OAuth JSON 绑定 account/profile；可执行路径仅运维可配，普通用户不能传 `bin`
- Provider 不可原地切换；需换类型时新建账号，防止旧 secret 被异类消费器读取
- 多人共享项目时，谁点击 AI 功能就用谁自己的绑定；pipeline stage 继承启动者上下文

### 未登录只读（推荐 web layer 模式）

```python
def handle_request(request):
    user_id = session_mgr.get_user_id(request.headers.get("X-Session-Token", ""))

    if request.method in ("GET", "HEAD"):
        # 读: 任何人可看
        return doc.list(entity_type)

    if not user_id:
        return 401   # 写: 必须登录
    actor = build_actor(store.get(user_id))
    return reg.dispatch(...)
```

### 自定义 session TTL

```python
# 短: 1 小时, 业务敏感场景
sm = SessionManager(ttl_sec=3600)

# 长: 30 天
sm = SessionManager(ttl_sec=30 * 86400)
```

### 持久化 session（服务态默认）

```python
# path 传入时,login/logout/过期清理都会 atomic 写 JSON;
# path=None 时保持纯内存,适合单测或一次性脚本。
sm = SessionManager(path=tmp_path / "sessions.json")
```

### 测试时不污染全局用户表

```python
# UserStore(path=...) 自定义路径
store = UserStore(path=tmp_path / "users.json")
```

## 跟其他叶子的关系

| 叶子 | 关系 |
|---|---|
| [02-entity/](../../01-data-structure/02-entity/) | 用 `Actor` 现有 dataclass + `ZeError` |
| [01-command/](../../03-commands-operations/01-command/) | **本叶子加 `enforce_login`** 进 CommandRegistry |
| [04-history-rollback/](../../02-data-flow/04-history-rollback/) | UndoController 接 Actor；caller 走 `build_actor(user)` |
| [_protocol/](../../_protocol/) | 10 个 GATE-AUTH-* gate |
| [01-document/](../../01-data-structure/01-document/) | durable ACL 严格校验 owner/collaborators；授权与重命名都消费 Document version CAS |

## 跟 audit gate 关系

| Gate | 验证 |
|---|---|
| `GATE-AUTH-01` | 同密码两次 hash salt 不同 → hash 不同 |
| `GATE-AUTH-02` | hash + salt round-trip verify |
| `GATE-AUTH-03` | 注册后能 authenticate |
| `GATE-AUTH-04` | 用户不存在/密码错统一 `auth_failed`（防枚举） |
| `GATE-AUTH-05` | login → token → get_user_id 还原 |
| `GATE-AUTH-06` | TTL 过期 token 返 None + 顺手清 |
| `GATE-AUTH-07` | enforce_login=True 时空 user_id 抛 `auth_required` |
| `GATE-AUTH-08` | CredentialStore 的安全公开视图、provider 不变式、default 互斥与 0600 文件权限 |
| `GATE-AUTH-09` | Header/ProjectPicker 共用唯一操作手册净化器；危险 HTML、URL、CSS 无法越过信任边界 |
| `GATE-AUTH-10` | 损坏 ACL 必须进入 recovery；rename/set_acl 在所有入口强制 Document version CAS |

注册在 [_protocol/AUDIT-GATES.md](../../_protocol/AUDIT-GATES.md)。

## 跑测试

```bash
cd 01-core/04-user-facing/03-authentication/
python -m pytest test_auth.py -v             # 52 测试
python -m pytest test_credentials.py -v      # 账号绑定 vault 测试

# 跑 audit gates
python ../../../_protocol/audit_gates.py --leaf AUTH
```
