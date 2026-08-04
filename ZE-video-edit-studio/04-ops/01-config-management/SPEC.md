# 01-config-management — 配置管理(SPEC)

> 一句话:ZE v3 框架"所有配置的家"。LLM 路线 / 服务密钥 / 模型 / 工具设置 / 偏好,统一在
> 此声明,经 header ⚙ 设置面板进入。

本层**不是** v2 的 SaaS 部署拓扑(mode=single/team/enterprise、replicas、vault)。当前文件后端
先把单进程写入做到原子；多进程、数据库事务、跨设备同步与配置审计由 W06 持久化层承接，
不能把这里的进程锁或版本号宣传成分布式保证。

## 1. 双轨设计

配置有两种性质,各按不同方式与 core 配合:

| | A. 框架运行旋钮(`ZE_*`) | B. 业务/LLM/密钥配置 + 面板 |
|---|---|---|
| 性质 | core 启动**读 env**(纯读) | 声明 + 运行时可编辑 + UI + 存 .data + 密钥脱敏 |
| 与 core | **照 css**:声明词表 + 静态门禁,**不动 core、0 import** | **照 core Registry 范式**:bootstrap 装配 + mod register + API + 面板 |
| 产物 | `config_schema.py` + `config.py` + `scripts/ops_contract_check.py` | `registry.py`(ConfigRegistry)+ server `/api/config` + 设置 Dialog |

A 轨与 css 完全同构:`config_schema.py` ↔ `tokens.css`(词表),`ops_contract_check.py` ↔
`css_contract_check.py`(门禁),`_contract.json`/`MIGRATIONS.md`/`ops.lock.json` ↔ css 三件套。

## 2. 配置种类(kind)

`text / path / int / float / bool / enum / secret / endpoint / model-ref / constant`。
决定校验方式(`config.py:coerce`)与面板控件(text→输入框、enum→单选、bool→开关、
secret→密码框+`***`、model-ref→下拉、endpoint→输入、constant→只读)。

## 3. 作用域(scope)与存储后端(务必分清)

| scope | 谁设 | 存哪 | 面板 |
|---|---|---|---|
| `pref` 偏好(主题/语言) | 每用户每设备 | 客户端 localStorage | 即时切换 |
| `tool` 工具/业务/LLM 配置 | operator(部署者) | 服务端 `.data/config.json` | 可编辑 |
| `secret` 密钥 | operator | `.data/config.json`(脱敏) + 默认源 `ZE-hupo-api/.env` | 密码框,回显 `***` |
| `framework` 运行旋钮 | operator(env) | 环境变量 | **只读展示** |

> 别让用户以为主题(本机)和 GPU 限额(本部署 env)存一个地方。

## 4. env 命名约定 + 旋钮 vs 常量

- env 旋钮统一 `ZE_*` 前缀(`ZE_RB_*` 资源预算、`ZE_CAPACITY_*` 容量护栏、`ZE_DATA_DIR` 等)。
- **旋钮**(env_overridable=True):部署可调,进门禁。
- **固定常量**(env_overridable=False):写死调优值(SSE 超时、队列大小、session TTL…),
  **不**升 env,只登记在 `DOCUMENTED_CONSTANTS` 供一眼看全,不进门禁。
  > 不是所有写死常量都该升 env——那是另一种过度 config。

## 5. 框架自带配置组(治碎片化)

近乎每个 ZE skill 都要 LLM 路线 + 图片服务。全系列调研发现同一概念各 skill 各起名
(`HUPO_API_KEY`/`API_KEY`/`ZE_ELEMENT_PLANNER_API_KEY`…)。故框架在
`registry.seed_framework_config()` 预声明**标准命名的通用组**,新 v3 skill 直接继承:

- **LLM 路线组**:`ze.llm.route`(enum `claude-cli|codex-cli|gateway`)、`ze.llm.model`、
  `ze.llm.gateway_base`(默认 `https://api.ai.hupo.games/v1`)、`ze.llm.gateway_key`(secret,
  默认源 `ZE-hupo-api/.env`)、`ze.llm.codex_account`/`codex_bin`/`claude_bin`。
- **图片服务组**:`ze.image.service_url`(默认 `http://10.0.0.252:12018`)、`ze.image.model`(15 模型 enum)。
- mod 在 `02-mod/05-code/config_register.py` 只加本 skill 特有旋钮 + 覆盖默认;通用组白拿。

## 6. 硬边界:不碰 ZE-skill-deploy 地盘

端口 / 部署 / SSH / ui_themes 归 `ZE-skill-deploy/server-deploy/ports.json`。面板对端口等
**只读展示、绝不写**;不重复造端口登记。

## 7. 门禁(A 轨闭环)

`config_schema.py` 声明 `ZE_*` 旋钮 → core/sdk 那些 `os.environ.get`(及拼接/动态 scope 读法)
**原样不动** → `scripts/ops_contract_check.py` 扫 core/sdk/server.py 的 `ZE_*` 字面量,断言每个
都被 schema 覆盖(`exact` 精确键 / `prefix` 前缀族)→ 未覆盖 exit 1。

**已知盲区**(同 css 检查器,维护者勿误信):只做正向(读→声明)硬门禁;反向孤儿、默认值一致
为软报告。拼接成多段的键、`.ts`/`.sh` 里的 `PORT`/`VITE_API_BASE` 不在 .py 扫描内(已在
`LAUNCH_KNOBS` 登记可发现)。

## 8. B 轨写入与故障语义

设置 PUT 的唯一正确入口是 `ConfigRegistry.update_values(path, updates, expected_version=None)`：

1. 持有 registry 的 `RLock`，以当前 override 构造 candidate，逐项复用 kind/choices 校验；非法项仍按兼容形状返回 `(key, reason)`，合法项组成同一批提交。
2. 在碰磁盘前完整 JSON 序列化，拒绝 `NaN`、`Infinity` 与未知对象；每次创建同目录、0600、名字唯一的临时文件。
3. 严格执行 `write → flush → 文件 fsync → os.replace → 父目录 fsync`。成功 replace 前的失败不会改变目标文件，且清理临时文件。
4. 只有落盘完成后才把 `_values` 切到 candidate 并推进进程内 `version`。因此并发线程的两次读改写不会互相覆盖。

故障分两类，不能混说：

- `config_write_failed`：`context.stage` 标出 `mkdir/temp_create/write/flush/file_fsync/replace/serialize`，`replaced=False`；内存旧、磁盘旧，可重试。
- `config_durability_unknown`：仅指 replace 已完成后父目录 fsync 的真实 I/O 错误，`replaced=True`；磁盘当前是完整新文件，内存为避免分叉也切到新 candidate，但 rename 的掉电耐久性未知，不可把它当作“没保存”盲重试。目录 fsync 被平台明确标为不支持时 best-effort，并通过 `directory_fsync_supported=False` 暴露。

启动加载同样 fail closed：只有 `FileNotFoundError` 代表空 override；坏 JSON、非 object、未知键、
只读覆盖或非法值均抛 `config_load_invalid`，且不动调用前内存。这样配置损坏不会悄悄关闭治理策略
或改用默认密钥。`set_values()` 仅为兼容的内存入口，`save_values()` 仅为初始化/迁移；新生产写
路径不得把二者拼成事务。

`ConfigRegistry.update_values` 仍允许可选 `expected_version` 供初始化/迁移使用，但生产 REST 必须携带，且只有
super-admin 可写全局配置。它能阻止同一进程的陈旧页面覆盖新值，却没有持久化、
也不能协调另一 worker。当前 REST 的 GET 返回该 version，官方设置面板 PUT 会携带
`expected_version`；`config_durability_unknown` 还会让同一请求的幂等键进入 outcome-unknown，
防止“文件可能已经换好”时自动重放。W06 必须用数据库 revision/transaction（以及需要时的
审计/outbox）替代这个进程内版本与文件幂等账本。

同一 key 不能重复 `declare`。mod 只改框架默认值时使用 `override_default(key, value)`；
这样 kind/secret/readonly/scope/choices/endpoint allowlist 只有一个升级责任人，不会被下游重声明降级。

## 9. 本叶子文件
- `config_schema.py` — 权威词表(KnobSpec + FRAMEWORK_KNOBS/DOCUMENTED_CONSTANTS/LAUNCH_KNOBS)
- `config.py` — 类型化 reader(coerce/read/read_framework_effective)
- `registry.py` — ConfigRegistry + 原子 update/load + seed_framework_config(B 轨,见 §1/§8)
- `test_registry_atomic.py` — 各写入阶段故障注入、重启完整性、CAS 与线程并发证据
- 门禁在 `../../scripts/ops_contract_check.py`
