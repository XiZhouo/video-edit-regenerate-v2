# OPS 层迁移记录(框架侧,随 04-ops 整树复制)

OPS 契约见 `02-mod/_locks/ops.lock.json`:= 04-ops 的配置/运维 schema 面。

## 版本规则
同 core/css:MAJOR = 破坏配置 schema(`Skill.Sync` 报 incompat);MINOR/PATCH = 加性(compat_bump)。三层(core/css/ops)各自独立 `interface_version`,可独立复制替换。

## 记录
### 1.17.0 (2026-07-19) — 引导精灵配置组（加性）
- 新增 `ze.guide.ai_receipt`（默认 `True`）：AI 经命令总线改动实体/长任务起止时，引导精灵给出视觉回执（点/圈 + 跨面板高亮）的总开关。**纯视觉**；高危命令的确认拉起(pending_confirmation)不受此开关影响——那是治理面不是装饰面。
- 新增 `ze.guide.sound`（默认 `True`）：精灵落点确认 tick 一声（40ms/音量 0.05）；reduced-motion 下强制静音。
- 纯新增 config 声明 → MINOR。

### 1.16.0 (2026-07-19) — 登录入口部署合同（加性）
- 新增 `ZE_AUTH_MODE`：`minimal-local` 仅本地账号、`federated` 仅飞书、`hybrid` 两者并存。默认 `hybrid`，保持既有飞书优先且本地账号接口可用的兼容行为。
- 新增 `ZE_AUTH_REGISTRATION`：`open` 允许创建本部署账号，`closed` 只允许已有账号登录；`federated` 始终按关闭处理。
- 新增 `ZE_AUTH_PREFERRED_METHOD`：只决定 `hybrid` 登录页首屏；显式选择当前模式不允许的方法会让配置 fail-closed，不会静默换身份边界。
- 三项都是服务进程环境变量。项目文件、网页请求和普通用户设置均不能改写；公开 `/api/auth/config` 只返回方法能力，不返回飞书密钥、账号库路径或会话。

### 1.12.0 (2026-07-10) — SQLite kernel 锁等待预算（加性）
- 新增且只新增一个数据库运行旋钮 `ZE_SQLITE_BUSY_TIMEOUT_MS`，默认 `5000`。它只决定 SQLite 在同机多进程争用写锁时最多等待多久；超时必须显式失败，不能无限挂住请求。
- `journal_mode=WAL`、`synchronous=FULL`、`foreign_keys=ON`、`trusted_schema=OFF`、本地文件系统约束和私有文件权限属于正确性基线，**不允许**通过环境变量降级。网络文件系统不在支持范围内。
- W06.1 只交付可验证的 SQLite 事务 kernel；Repository、Document、OpLog、Pending/Action/Pipeline 等业务 authority 尚未切换。因此本次升级不能被描述为“业务写入已经 ACID”或“已经支持跨设备同步”。

### 1.11.0 (2026-07-10) — 资产上传与项目总量硬上限（加性）
- 新增 `ZE_HTTP_ASSET_UPLOAD_MAX_BYTES`（默认 32 MiB）：HTTP 上传按 chunk 累计，`Content-Length` 和 chunked 请求都不能越过上限。
- 新增 `ZE_CAPACITY_DOC_TOTAL_ASSETS_SIZE_MAX`（默认 512 MiB）：按项目本地唯一内容寻址 blob 计总量；重复 sha 不重复占额，坏 metadata fail-closed。
- 大文件不靠抬高内存 body 上限解决，后续进入可恢复分片上传协议。

### 1.10.0 (2026-07-10) — 项目拓扑单进程降级开关（加性）
- 新增 `ZE_ALLOW_SINGLE_PROCESS_TOPOLOGY`，默认关闭。只有确认部署永远单进程且平台不提供 `flock` 时才可设为 `1`；多进程或多 worker 部署不得启用。

### 1.9.0 (2026-07-10) — 配置候选集原子提交与声明安全（加性）
- `update_values(path, updates, expected_version=...)` 在同一把锁内完成候选集校验、严格 JSON 序列化、唯一临时文件、file fsync、atomic replace、directory fsync，最后才切换内存。
- replace 前失败是 `config_write_failed`，旧文件/旧内存不变；replace 后目录 fsync 失败是 `config_durability_unknown`，完整新文件已可见，内存必须跟随它，调用方不得盲目重放。
- 文件不存在才视为空 override；损坏 JSON、非 object、未知/只读键、非法值都以 `config_load_invalid` fail closed，不清空旧内存。
- 配置声明不允许重复 `declare`；mod 如只改默认值必须调 `override_default`，从结构上阻止把 readonly secret 重声明成可编辑 text。Secret kind/flag/scope、enum choices、scope 与 endpoint URL 在声明/写入时校验。网关与图片服务 endpoint 还有 host allowlist。
- core 的生产门面必须传 `expected_version`且仅 super-admin 可改全局配置；`ops_contract_check` 以 AST 防止回退成 `set_values + save_values`、普通 edit 权限或丢 CAS。
- 本版 revision 仍是进程内计数。多进程不丢更新、跨设备订阅和耐久审计属 W06 事务存储，不在本版假承诺。

### 1.1.0 (2026-06-02) — 配置中枢落地(加性)
- `01-config-management/` 从空壳填实,成为 v3 框架"所有配置的家":
  - `config_schema.py` —— `ZE_*` 旋钮**权威词表**(对位 css 的 tokens.css):17 框架 env 旋钮 + 固定常量登记 + 启动旋钮;每条带 kind/scope/secret/match。
  - `config.py` —— 类型化 + 校验 reader(打错值报 ConfigError,不静默回落)。
  - `registry.py` —— `ConfigRegistry`(kind/scope/secret)+ `seed_framework_config`(框架自带 LLM 路线组 / 图片服务组,治全系列命名碎片化);经 sdk barrel re-export,bootstrap 装配 + mod `config_register.py` 声明 + `.data/config.json` 存值。
- 门禁:`scripts/ops_contract_check.py`(对位 `css_contract_check.py`)断言 core/sdk 读的每个 `ZE_*` 都在 schema 声明过。
- **不做**:v2 SaaS 拓扑 / 多环境 / 热更新 / 配置审计(YAGNI)。端口/部署仍归 ZE-skill-deploy,面板只读。
- 加性:不破坏既有 token/旋钮,Skill.Sync 判 compat_bump。

### 1.3.0 (2026-06-04) — deploy.json internal_ports schema 文档化(P3b)
- `README.md` 加「部署端口」节:`deploy.json.internal_ports` 数组(每项 `{port:18000-18999, name?, bind_host?=127.0.0.1, proxy?}`,默认 `[]` 全兼容),配对约定对外 8xxx ↔ 内部 18xxx。
- 部署侧 `ZE-skill-deploy/server-deploy/scripts/ports.py` 早已端到端支持(check/verify/reserve/render_md);本批只补 schema 文档 + `port-governance.md` 的 per-item 形态。
- 纯文档加性、无 schema/code 变更 → Skill.Sync 判 compat_bump(README 进 ops 校验和)。

### 1.0.0 (2026-05-30) — 基线
- 占位:运维 / 配置入口。运行态数据一律放 `.data/`(在所有复制目标之外,sync 不碰),不得放进 01-core/03-css-rules/04-ops/sdk(那些每次迭代被 rmtree 整树替换)。
