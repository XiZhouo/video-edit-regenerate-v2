# Authentication — 可配置登录入口 + ZE Session

## 1. 这是什么

`AuthRuntimeConfig` + `UserStore` + `SessionManager` 是框架的身份认证接缝。部署者可选择 `minimal-local`（仅本地账号）、`federated`（仅飞书）或 `hybrid`（两者并存）。无论入口是哪一种，认证完成后都签发本地 `session_token`，后续从 token 还原 `Actor` 给 `CommandRegistry.dispatch`。

浏览器先读取 `GET /api/auth/config`，只显示当前部署允许的入口。该接口不返回 App Secret、账号库路径或 token。登录、注册和飞书 SSE 路由仍在后端重复检查模式，因此篡改前端不能启用被关闭的入口。**未登录不能访问受保护项目和写操作**。

### 为什么是独立叶子

| 问题 | 原因 |
|---|---|
| **Actor.user_id 之前没人填** | 现有 Actor 是个 dataclass，user_id 是字符串占位；谁来灌真值？需要 UserStore |
| **不同部署需要不同身份入口** | 公司可用飞书；独立团队可只建本地账号；同一份代码通过进程环境选择，不在项目数据里分叉 |
| **跨 skill 共享身份** | 业务同事在 5 个 ZE skill 之间跳，登录一次到处通；用户表得放全局位置 |
| **本地 session 仍必要** | 飞书授权只发生在登录时；后续项目 ACL / OpLog / CommandRegistry 全部消费 ZE session |
| **session 可持久化** | 同一台电脑重开页面/服务重启后不应反复登录；TTL 到期或主动 logout 才失效 |

## 2. 数据模型

### User

```python
@dataclass
class User:
    user_id: str            # 飞书用户 = "feishu:<open_id>"; 直接作 Actor.user_id
    password_hash: str      # 兼容旧本地账号; 飞书用户为空
    password_salt: str      # 兼容旧本地账号; 飞书用户为空
    created_at: float       # time.time()
    roles: tuple[str, ...] = ("user",)   # 占位; 当前不校验
    display_name: str = ""
    email: str = ""
    avatar_url: str = ""
    auth_provider: str = "password"      # password / feishu
    external_id: str = ""                # 飞书 open_id

    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, d: dict) -> "User": ...
    def verify_password(self, password: str) -> bool: ...
```

**为什么飞书 user_id = `feishu:<open_id>`**：

- `open_id` 是 larkauth 当前返回的稳定身份字段；email/name 只作展示，不作身份主键
- 加 `feishu:` 前缀可以避免跟旧本地 username 碰撞
- 跟现有 `Actor.user_id` 自然对接，项目 ACL / OpLog / CommandRegistry 不需要理解 OAuth

**本地账号路径**：

- `minimal-local` 的 UI 使用邮箱形状作为登录名；邮箱只作字符串主键，不发送验证邮件
- `hybrid` 保留历史 username 兼容，同时提供飞书入口
- `federated` 在服务端关闭本地注册与登录
- 飞书用户没有 password_hash/password_salt，`verify_password()` 永远返回 False

**为什么 salt 单独存而不混进 hash**（本地密码路径）：

- `hashlib.pbkdf2_hmac` 需要同一 salt 才能验证；salt 必须随用户记录保存
- salt 每用户独立，撞 hash 难度 × 用户数
- 改密码 / 升级 hash 算法时 salt 是迁移锚点

### UserStore

```python
class UserStore:
    """全局用户表 (~/.claude/ze_users.json by default)."""

    DEFAULT_PATH = Path.home() / ".claude" / "ze_users.json"

    def __init__(self, path: str | Path | None = None) -> None: ...

    def register(self, username: str, password: str) -> User
        """注册. username 已存在抛 user_exists; 名/密码格式不合抛 validation_failed."""

    def authenticate(self, username: str, password: str) -> User
        """登录. 用户不存在 / 密码错统一抛 auth_failed (不区分, 防枚举).
        成功返回 User."""

    def upsert_external_user(
        self, *, provider: str, external_id: str,
        display_name: str = "", email: str = "", avatar_url: str = "",
    ) -> User
        """外部 OAuth 身份写入. 飞书使用 provider="feishu", external_id=open_id."""

    def get(self, user_id: str) -> User | None
        """读单个用户; 不存在返 None (不抛)."""

    def exists(self, user_id: str) -> bool

    def list_users(self) -> list[User]
        """列全部用户; admin / 调试用."""

    def _load(self) -> None: ...     # 内部: 读 json
    def _save(self) -> None: ...     # 内部: atomic 写 json
```

### SessionManager

```python
class SessionManager:
    """session 表; path 传入时持久化,否则纯内存."""

    def __init__(self, ttl_sec: int = 7 * 86400, path: str | Path | None = None) -> None: ...

    def login(self, user: User) -> str
        """造 session_token (32 hex bytes); 返回."""

    def logout(self, token: str) -> bool
        """删 session; 返回是否找到."""

    def get_user_id(self, token: str) -> str | None
        """token → user_id; 不存在或过期返 None."""

    def is_valid(self, token: str) -> bool

    def cleanup_expired(self) -> int
        """主动清理过期 session; 返回清掉数. 一般不手动调; get_user_id 也会顺手清."""

    def active_count(self) -> int
        """当前活动 session 数 (诊断/admin用)."""
```

### 工具函数

```python
def hash_password(
    password: str, salt: str | None = None,
) -> tuple[str, str]
    """PBKDF2-SHA256. salt 不传则随机生成. 返 (hash_hex, salt_hex)."""

def verify_password(password: str, expected_hash: str, salt: str) -> bool
    """hmac.compare_digest 防 timing attack."""

def build_actor(
    user: User, *, device_id: str = "default", source: str = "user",
) -> Actor
    """User → Actor; user_id = user.user_id."""
```

## 3. 密码哈希协议

**新账号算法**：`hashlib.pbkdf2_hmac("sha256", password, salt, 210_000)` → 64 hex 字符。旧记录缺少 `password_algo` 时按历史 SHA256 验证，成功后立即重算 PBKDF2 并原子落盘。

**salt 生成**：`secrets.token_hex(16)` → 32 hex 字符（16 字节熵）

**比较**：`hmac.compare_digest(computed_hash, stored_hash)` 防 timing attack（不要用 `==`）

**`authenticate` 防 timing attack**：用户不存在时仍跑一次 `hash_password(password, _DUMMY_SALT)`，让"用户不存在"跟"密码错"两条失败路径耗时基本一致，无法靠响应时间区分。

**为什么当前不用 bcrypt / argon2**：

- 不引第三方依赖（纯标准库）
- PBKDF2 是当前零第三方依赖下的慢哈希基线；公开运营前仍应评估 Argon2id、邮件验证、找回密码和集中风控

**密码强度校验**（写在 UserStore.register 里）：

- 长度 8-128 字符
- 不能纯空白
- 不能有前后空白（前端通常会 trim，存了带空白的密码下次登录必败）
- 不强制大小写 / 数字（业务方约束太严内网用户反感）

**username 校验**：

- 长度 1-64
- 不能含 `:`/`/`/`\\` 等可能跟其他 ID 系统冲突的字符
- 不能含 Unicode 控制字符（`Cc`/`Cf` 分类，含 `\x00` / ZWSP `U+200B` / RTL marker 等；防钓鱼）

## 4. Session 协议

**token 格式**：`secrets.token_hex(32)` → 64 hex 字符（256 位熵）

**TTL**：默认 `7 * 86400` 秒（7 天）；超过则自动失效

**过期清理时机**：

- `get_user_id(token)` / `is_valid(token)` 调用时顺手清当前 token
- `cleanup_expired()` 显式调批量清
- 不开后台线程定时扫（避免进程退出残留资源）

**进程退出**：`SessionManager(path=...)` 会从 JSON 恢复未过期 session；`path=None` 时保持纯内存,用于测试和一次性脚本。

**SessionInfo 内部结构**：

```python
@dataclass
class _SessionInfo:
    user_id: str
    created_at: float
    expires_at: float
```

不暴露给外部；外部只看 token。

## 5. 持久化协议

**用户表位置**：`~/.claude/ze_users.json`（全局跨 skill 共享）

**Session 表位置**：服务态由 `Repository` 传入 `<ZE_DATA_DIR or skill/.data>/sessions.json`。`.data/` 是部署保留目录,rsync 不覆盖,所以 redeploy/restart 后浏览器旧 token 仍可恢复。

**为什么放 `~/.claude/`**：

- 业务同事在多个 ZE skill 间跳；一份用户表全 skill 通用
- `~/.claude/` 已经是 ZE 框架共享目录（`feishu_token.json` / `server-deploy/` 都在）
- 不放进 Document（每个 doc 自己一份用户表就违反"全局账号"）

**格式**：

```json
{
  "schema_version": "1.0.0",
  "users": [
    {
      "user_id": "feishu:ou_xxx",
      "password_hash": "",
      "password_salt": "",
      "created_at": 1716700000.0,
      "roles": ["user"],
      "display_name": "张三",
      "email": "zhangsan@example.com",
      "avatar_url": "https://...",
      "auth_provider": "feishu",
      "external_id": "ou_xxx"
    }
  ]
}
```

**Atomic 写**：先写 `<path>.tmp`，再 `os.replace(tmp, final)`（同 v3 其他叶子）

**文件权限**：`0o600`（仅 owner 可读写）。用 `os.open(...O_CREAT, 0o600)` 创建 tmp 文件 + `os.replace` 后再 `os.chmod(0o600)` 兜底，防共享系统 umask 把 hash 文件设成 world-readable。

**测试时**：`UserStore(path="/tmp/test_users.json")` 自定义路径不污染全局

## 6. CommandRegistry 集成

**新增可选参数 `enforce_login`**：

```python
class CommandRegistry:
    def __init__(
        self, *,
        broker=None,
        enforce_login: bool = False,    # 新加
    ) -> None: ...

    def dispatch(self, ..., source: str, actor: Actor, ...) -> Operation:
        ...
        # source check 之后, poll 之前; 任何 source 都要 user_id 非空
        if self._enforce_login:
            if not actor.user_id:
                raise ZeError(
                    "auth_required",
                    f"command {command_id!r} requires logged-in user",
                    context={"command_id": command_id, "source": source},
                )
        ...
```

**默认 False**：v3 现有 526 测试不破坏（它们没传 enforce_login）。

**业务方启用**：`CommandRegistry(enforce_login=True)` —— 任何 source（user / ai / sync / system）的命令都要带有效 user_id。

**为什么不留 `source=system` 旁路**：

- `source` 由 caller 控制；留旁路 = 默认信任 caller
- web layer 出 bug 把前端请求的 source 当成 "system" 就绕过校验
- framework 自动化任务请用一个**保留账号**（例如 `user_id="__system__"`）正常 login 后再 dispatch；安全模型更清晰

### 标准接入模式（web layer）

```python
def http_handler(request):
    token = request.headers.get("X-Session-Token", "")
    user_id = session_mgr.get_user_id(token)

    if request.method in ("GET", "HEAD"):
        # 读: 不要求登录; 任何人可看
        return render(doc.list(entity_type))

    if not user_id:
        return 401   # 写: 必须登录

    user = user_store.get(user_id)
    actor = build_actor(user)
    op = cmd_registry.dispatch(
        command_id, args, actor=actor, source="user",
        doc=doc, op_log=op_log,
    )
    return jsonify(op.to_dict())
```

## 7. 跟其他叶子的关系

| 叶子 | 关系 |
|---|---|
| [02-entity/](../../01-data-structure/02-entity/) | 用 `Actor` 现有 dataclass + `ZeError` |
| [01-command/](../../03-commands-operations/01-command/) | **本叶子加 `enforce_login` 参数**进 CommandRegistry |
| [04-history-rollback/](../../02-data-flow/04-history-rollback/) | UndoController 接受 Actor；caller 走 `build_actor(user)` 喂给它 |
| [_protocol/](../../_protocol/) | 10 个 GATE-AUTH-* gate |
| [01-document/](../../01-data-structure/01-document/) | durable ACL 严格校验 owner/collaborators；授权与重命名都消费 Document version CAS |

## 8. 持久化 / 配置

| 项 | 位置 | 说明 |
|---|---|---|
| 用户表 | `~/.claude/ze_users.json` | atomic 写; 跨 skill 共享; schema_version 1.0.0 |
| Session 表 | `.data/sessions.json` 或内存 | 服务态持久化; path=None 时纯内存 |
| Session TTL | `SessionManager(ttl_sec=...)` 构造参数 | 默认 7 天; caller 可覆盖 |
| 外部账号表 | `<ZE-system auth>/credentials.json` | record + sealed secret 的单文件聚合；schema_version 1.0.0；跨进程锁 + strict load + durable replace |
| 凭据 host key | `<ZE-system auth>/credentials.key` | 首次 seal 前 durable create，之后只读；独立于账号表的 prepare 聚合 |
| CLI OAuth 文件 | `ai-homes/.../auth.json` 或 `.credentials.json` | 每个 CLI home 一个独立文件聚合；unique temp + fsync + replace，不与账号表跨文件原子 |

### 8.1 CredentialStore 单文件事务

`credentials.json` 把公开 record 与 API secret 的 sealed blob 放在同一个提交边界。每个
`upsert_for_user / delete_for_user / set_default_for_user / mark_used / mark_verified` 都必须完成以下顺序：

1. 取得目标路径的跨进程 `.lock`；
2. 从磁盘 strict load 到局部 candidate，不先覆盖当前内存；
3. 校验顶层 schema、每个 record 的完整字段与规范值、record id 唯一性；
4. 校验 `secret_ref` 双向一致（不能缺 secret，也不能有孤儿 secret），再验证 sealed blob
   的字段、base64、nonce/mac 长度、MAC 和 UTF-8 明文；
5. 完整 JSON 序列化（拒 NaN/Infinity），写同目录随机唯一 temp，flush + file fsync；
6. `os.replace(temp, credentials.json)`，再 fsync 父目录；
7. 只有完成 replace 后，才把整个 candidate 一次性换成进程内快照。

因此错误语义不是含糊的“保存失败”：

| code | 已发生什么 | caller 怎么做 |
|---|---|---|
| `credential_store_corrupt` | 已有文件不是合法完整 1.0.0，或 record/secret/key 无法互相验证；内存不清空 | 停写并修复/恢复文件，不能默认空表继续 |
| `credential_store_unavailable` | 已有目标缺失或不可读；内存不清空 | 恢复存储可用性 |
| `credential_store_write_failed` | replace 前失败；旧目标与调用前内存未变 | 可用同一业务意图重试 |
| `credential_store_durability_unknown` | replace 已完成，完整新文件可见且内存已跟随；目录 fsync 失败 | 不得盲重试；先读取/对账 |
| `credential_key_*` | 独立 host key 的读取、校验、首次创建失败或耐久性未知 | 停止 secret 写入；不可生成新 key 覆盖旧 key |
| `credential_cli_auth_*` | CLI OAuth 文件自身的写失败或 replace 后耐久性未知 | 按 `replaced` 区分安全重试与先对账 |

所有返回的 `CredentialRecord` 都是深拷贝，尤其 `metadata` 不与内部状态共享。

Provider 是 record 身份的一部分，创建后不可原地切换；需换 provider 时新建绑定，防止旧
secret 被带入另一类消费器。REST metadata 只允许 provider 白名单字段；`home_dir/is_default/bin`
由服务端维护。宿主可执行程序只从运维 ConfigRegistry 读，即使旧 metadata 存在 `bin` 也不执行。
Gateway `base_url` 只接受 provider HTTPS host allowlist，禁止 loopback/私有目标、userinfo、自定义端口、
query/fragment，防止 SSRF 与 Authorization 误投。对外 `public_dict()` 不返 `secret_ref/home_dir/bin`。

### 8.2 1.0.0 兼容与迁移边界

当前仓库从引入 CredentialStore 起只发布过 schema `1.0.0`；旧实现写出的合法文件已经包含
`schema_version / credentials / secrets` 三个顶层字段和 `CredentialRecord` 的全部字段，现实现有
显式 restart round-trip 测试覆盖，可直接读取。旧 key reader 接受 `>=32` 字节并取前 32 字节，
该兼容行为保留；新建 key 固定 32 字节。

未知 schema、缺字段或“尽量跳过坏 record”不属于迁移。未来改变文件形状时必须先写独立、可审计、
输入输出版本明确的迁移器；运行时 loader 不推断、不补默认值，以免把账号/secret 静默丢掉。

### 8.3 明确不在同一事务里的边界

host key、`credentials.json`、CLI home OAuth 文件是三个文件聚合。首次 API secret 写入把 key 创建
当作 prepare：key 可以先存在而 record 尚未提交，但绝不允许 record 引用不存在/打不开的 key。
CLI 绑定流程也可能先写 OAuth 文件再写 metadata（或相反）；每个单文件不会半写，但跨文件失败仍
可能留下孤儿 home 或 needs-secret record。这个问题需要后续 durable action journal + reconciler 的
prepare/finalize 协议解决，不能由多次 `os.replace` 冒充原子事务。

账号 REST 写入已统一进入 IdempotencyStore，创建时业务 id 由 actor + 稳定 key 派生；响应丢失后重试不会新建
第二条账号。`*_durability_unknown` 会冻结该 key 为 outcome-unknown，客户端必须先 GET 对账。

本批保持 `sealed-v1` 密文兼容：它仍是现有 HMAC keystream + MAC 格式，没有悄悄改密文算法。
顶级安全目标应另做带 `key_id` 的标准 AEAD（AES-GCM / ChaCha20-Poly1305）、keyring/KMS 与双读迁移；
没有显式迁移就替换 `_seal` 会让全部现有 secret 永久打不开，因此不属于本次文件事务修复。

### 8.4 项目写意图 proof wire v2

Authentication 前端负责把“这次查询用的是谁”与“这个 intent 是否已经被谁耐久认领”分开，不能再用
一个含糊的 `principal_bound` 同时表达两件事。公开 intent GET 必须返回
`proof_revision=2`、`principal_scoped` 与 `intent_reserved`：

| 证明组合 | 准确含义 | 客户端动作 |
|---|---|---|
| `not_committed + principal_scoped=true + intent_reserved=false` | 服务端按当前 token/guest 检查过，但共享锁快照里还没有 exact intent+principal+target+kind 认领；GET 没有写控制文件 | 只可用完全相同的 intent/key/target/kind 发一次 POST；false 不是租约，POST 原子认领仍可能输掉竞争 |
| `not_committed + principal_scoped=true + intent_reserved=true` | 写请求曾耐久认领 exact 身份，但在 namespace effect 前已证明回滚；journal 可删除，永久 claim 仍保留 | 同一主体可做一次有界重试，禁止换 target、kind 或账号 |
| `committed/deleted/in_progress + principal_scoped=true + intent_reserved=true` | 正向 outcome/journal，或 claim 与 marker/tombstone 的组合，已经同时证明主体与操作身份 | 完成、等待或继续对账，不得再发第二笔业务写 |
| revision 不等于 2、scope 不是 true、字段缺失/类型错误 | 响应不能证明当前主体的结果 | 冻结为 outcome unknown，禁止自动重放 |

`principal_scoped=true` 是**本次请求的认证求值范围**，不声称 GET 已经持有认领；
`intent_reserved=true` 才是 durable identity reservation。公开查询读到 committed/deleted/in-progress
证据却缺 principal hash，或只有 marker/tombstone 而没有任何主体 claim 时，服务端必须
`project_recovery_required` fail closed；前端不得把旧版无主体正向记录归到当前账号。耐久文件仍只保存
域分离 hash，不保存 token、guest cookie 或原始账号。`principal_bound` 字段从 v2 删除。

### 8.5 原生浏览器 transport 与媒体票据

普通 fetch 应使用显式 Bearer、再 HttpOnly cookie；通用旧接口仍保留 Bearer > cookie > query 的
兼容解析，但框架只为无法设置 header 的 `EventSource` 主动生成 session query，并在该 transport
内优先于可能属于另一工具的共享 cookie。资产端点不消费通用 query 兼容。

图片、视频等媒体 URL 必须先由普通认证 fetch 换取 `MediaTicketSigner` 签发的短期能力。票据只含
opaque principal 指纹，并绑定 tool、project、topology generation 与 asset sha；签名 key 从稳定
credential master key 域分离派生，不允许随机进程 key。媒体 GET 验签后还必须重读 durable ACL
和引用事实，所以撤权、项目 delete/reset/recreate、共享引用移除都会立即使读取失败。无票据时仅
Authorization/cookie 直读兼容；历史 `?token=<session>` 资产 URL 不再认证。所有资产响应无论成功
失败均是 `private, no-store`。

### 8.6 前端账号变化通知

`CopilotAccountDialog` 的每次成功绑定、切换默认项或删除都会通过 `onChanged` 触发
`notifyCredentialsChanged()`。该函数在当前浏览器窗口派发
`CREDENTIALS_CHANGED_EVENT`；提示词优化器、能力门控等账号消费者监听该事件后必须立即重新读取
服务端账号列表，不能把窗口重新聚焦或关闭弹窗当作唯一刷新时机。

事件只表示“当前用户的账号集合可能变化”，不携带 provider、密钥、模型或账号 ID。消费者必须
重新走带当前认证信息的 API 获取真值，避免跨用户泄漏和陈旧缓存。`focus` /
`visibilitychange` 可以继续作为恢复现场的兜底，但不能替代成功写入后的明确通知。

**重置用户表**：删 `~/.claude/ze_users.json` 即可（业务方手动；不在 API 暴露 `drop_all`）

## 9. audit gates

注册到 [_protocol/AUDIT-GATES.md](../../_protocol/AUDIT-GATES.md)：

| Gate | 名字 | 检查什么 | 形态 |
|---|---|---|---|
| `GATE-AUTH-01` | hash_password_salt_random | 同密码两次哈希 salt 不同；hash 不同 | runtime |
| `GATE-AUTH-02` | hash_password_verify_round_trip | hash + salt 能正确 verify_password | runtime |
| `GATE-AUTH-03` | user_store_register_authenticate | 注册后能用同密码登录 | runtime |
| `GATE-AUTH-04` | user_store_auth_failed_unified | 用户不存在 vs 密码错统一抛 auth_failed | runtime |
| `GATE-AUTH-05` | session_token_lookup | login 返 token; get_user_id 还原 user_id | runtime |
| `GATE-AUTH-06` | session_expired_returns_none | TTL 过期 token get_user_id 返 None | runtime |
| `GATE-AUTH-07` | cmd_dispatch_enforce_login | `enforce_login=True` 时空 user_id 抛 `auth_required` | runtime |
| `GATE-AUTH-08` | credential_store_transaction_boundary | 凭据不泄密、provider 稳定、default 互斥、文件 0600 | runtime |
| `GATE-AUTH-09` | operation_manual_trust_boundary | Header/ProjectPicker 共用唯一 allowlist 净化器和 HTML sink，危险标签、属性、URL、CSS 均被拒绝 | static + TS runtime |
| `GATE-AUTH-10` | acl_shape_and_document_cas | 损坏 ACL fail closed；rename/set_acl 在所有 surface 强制用户已渲染的 Document version CAS | runtime + static |

## 10. 红线规则 + 反模式

红线：

- **密码不存明文** — 新账号必须 PBKDF2-SHA256 + 独立随机 salt；旧 SHA256 仅作惰性迁移输入
- **关闭入口必须由后端执行** — 隐藏按钮不是权限；register/login/lark SSE 都要检查 `AuthRuntimeConfig`
- **auth_failed 不区分原因** — 用户不存在跟密码错统一返同一码；防枚举攻击
- **session 可持久化但有 TTL** — 只保存 token/user_id/created_at/expires_at; 不进 Operation / OpLog
- **`enforce_login=False` 默认** — 不破坏现有 v3 测试
- **token 不进 Operation / OpLog** — token 是临时凭证，不能落进数据；只 `actor.user_id` 进数据
- **session token 不进媒体 URL** — EventSource query 是窄兼容例外，媒体必须使用短期、单 sha
  能力票据，并在读取时重验 durable 权限
- **坏凭据表不得降级为空表** — JSON、record、secret、key 任一不一致都 fail-closed
- **durability unknown 不等于失败** — replace 后目录 fsync 失败时，新文件和新内存都已生效；先对账，不盲重试

反模式:

- **AP-AUTH-1** UserStore.register 时 print(password) → 任何场景都不应 echo 密码
- **AP-AUTH-2** Session 永不过期或无 logout → 必须保留 TTL 和主动失效路径
- **AP-AUTH-3** `==` 比较哈希 → timing attack；用 `hmac.compare_digest`
- **AP-AUTH-4** 把 session_token 当 user_id 用 → token 是凭证不是身份；身份是 user_id
- **AP-AUTH-5** Reset 密码不让用户改密码后旧 session 失效 → 应一并失效；本 SPEC v1 未做改密码 API，业务方需要时加
- **AP-AUTH-6** 业务方把 user_store.list_users() 返回的 User.password_hash 暴露给前端 → 应有 sanitize 层
- **AP-AUTH-7** 把 enforce_login 跟 broker 混到一起 → 两个独立 concern，参数分开
- **AP-AUTH-8** CredentialStore 捕获坏行后 `continue` → 会把真实账号静默删掉；必须拒绝整份文件
- **AP-AUTH-9** `credentials.json.tmp` / `auth.json.tmp` 固定临时名 → 多进程互踩；必须使用同目录随机唯一 temp
- **AP-AUTH-10** 先改 `self._records` 再落盘 → replace 前失败造成内存/磁盘分叉；先提交 detached candidate

## 11. 当前实装状态

### ✅ 已实装

| 项 | 说明 |
|---|---|
| `User` dataclass + to_dict / from_dict / verify_password | PBKDF2-SHA256 + salt；旧 SHA256 惰性迁移 |
| `AuthRuntimeConfig` + `/api/auth/config` | minimal-local / federated / hybrid；公开响应不含密钥 |
| 配置驱动 `LoginPage` | 本地登录/注册、飞书登录、hybrid 切换和注册关闭态 |
| `UserStore(path=None)` 持久化 ~/.claude/ze_users.json | atomic 写; register / authenticate / get / exists / list_users |
| `SessionManager(ttl_sec=7d, path=None)` session | 服务态持久化; login / logout / get_user_id / is_valid / cleanup_expired / active_count |
| `hash_password(password, salt=None) → (hash, salt)` | 工具函数 |
| `verify_password(password, hash, salt) → bool` | `hmac.compare_digest` 防 timing attack |
| `build_actor(user, device_id, source)` | User → Actor |
| `CredentialStore` 严格读取 + 单文件聚合提交 | record/secret 双向校验；跨进程无 lost update；故障阶段语义稳定；返回深拷贝 |
| 前端账号变化通知 | 账号弹窗成功写入后广播无敏感信息的变化事件，能力入口立即重查 |
| CLI OAuth 文件原子替换 | 不再 `O_TRUNC` 半写；仍明确是独立文件聚合 |
| `CommandRegistry(enforce_login=False)` 新参数 | 默认 False 不破坏现有测试 |
| `dispatch` 加 source != system + actor.user_id 非空 兜底校验 | enforce_login=True 时生效 |
| audit gates 10 个 | GATE-AUTH-01..10 |

### ⏳ 待补（真有跨叶子依赖）

| 项 | 依赖 |
|---|---|
| 改密码 API + 旧 session 一并失效 | 业务方明确需求 |
| 更细粒度的 role/组织/空间 ACL | 当前只提供严格 owner + collaborators 文档级 ACL；组织策略由后续授权层扩展 |
| 跨设备 / 跨进程 session（Redis）| 多 server 部署 |
| 密码强度策略可配 | 业务方需求驱动 |

### 原则

- **当前边界**：本地账号是早期可用方案，不含邮箱验证、找回密码与集中风控；公开运营前必须单独补齐
- **不破坏现有测试**：`enforce_login` 默认 False
- **API 签名先定**：UserStore / SessionManager 后续不变签名
