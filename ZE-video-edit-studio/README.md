# ZE Skill Build

> ZE 当前工程框架。历史工程代际只作为迁移证据保留，不进入标准名称，也不是另一条可选 current 链路。

## 服务器部署运维（amber · ZE-video-edit-studio · :8119）

**重启本服务必须走 `bash start.sh`，不要裸起 uvicorn。**

`start.sh` 会注入部署必需环境（如 `ZE_SERVER_MODE=1`、`ZE_VIDEOGEN_BASE_URL` 默认网关、端口取自 `deploy.json`）。直接 `uvicorn server:app ...` 会丢掉这些变量，表现为「视频生成服务地址未配置」等运行时错误。只改本目录、只杀本目录 cwd 对应进程；不要动其他部署端口或 Hub 凭据目录。

```bash
cd /home/amber/deployments/ZE-video-edit-studio
# 若 dist 已就绪、无需现场构建，可：
# export ZE_TARGET_DEPENDENCIES_PREPARED=1
bash start.sh
```

## 这是什么

ZE 系列每个 skill（ZE-knowledge-base / ZE-seek-find-object-generator / ZE-project-atlas 等）都共享一套**多 Surface 底层框架**：数据怎么存、命令怎么执行、多人怎么同步、AI 怎么用 CLI/API 操作、人怎么通过声明过的控制面确认，Web 工具怎么布局和统一视觉。

本 skill 就是这套**底层框架的定义**——所有 ZE skill 复制本 skill 的受管工程层；CLI/API-only skill 不需要 Web 文件，Web 工作台 skill 才复制 `03-css-rules/` + `webapp/`。业务方只在 `02-mod/` 里写自己的 entity 类型 / command / 布局 / 主题 / 字典。

Python 运行时正式支持 **3.9–3.13**；最低版本以 `pyproject.toml` 为机器真源，关键持久化、同步锁与整树测试必须同时通过 3.9 和当前发布运行时，不能依赖某一版 CPython 的反射输出或 JSON 实现细节。

## Rule Capsule

## Role
定义 ZE Skill Build 工程形态、受管层、脚手架、同步、直接复制安全和验证门禁。

## Owns
`SURFACE-PROFILES.md`、`DIRECT-COPY-FLYWHEEL.md`、`ARCHITECTURE-GUARDRAILS.md`、`01-core/`、`03-css-rules/`、`04-ops/`、`05-i18n/`、`sdk/`、`scripts/skill_scaffold.py`、`scripts/skill_sync.py`、`scripts/verify_all.py`、`scripts/fleet_health_check.py`、`scripts/design_layer_contract_check.py`、`scripts/eval_suite.py`。

## Does Not Own
ZE 系列成员资格、Hub registry 展示治理、服务器端口分配、业务判断流和单个 work skill 的领域算法。

## Invariants
受管层在下游是只读同步产物；`02-mod/` 与 `.data/` 永不被同步覆盖；Web 不是最低成立条件；`ZE_SURFACE.json` 是能力形态源；跨阶段只交 artifact，不 import 其他标准 skill 的内部实现。

## Extension Path
新增 surface 或 gate 时先改 `SURFACE-PROFILES.md`，再改 scaffold/sync/verify reader 与 fixtures，最后改 Hub/deploy/system 消费方。

## Gates
`python scripts/verify_all.py`、`python -m pytest scripts/test_surface_profile.py scripts/test_skill_scaffold.py scripts/test_design_layer_contract_check.py scripts/test_eval_suite.py`、`scripts/skill_sync.py --verify-locks`、`python scripts/fleet_health_check.py --root .. --compact`。

## Consumers
新建 work skill、已迁移到当前标准结构的 skill、ZE-skill-system catalog/audit、ZE-skill-deploy preflight/snapshot。

## 顶层 5 层

| 目录 | 是什么 | 业务方动吗 |
|---|---|---|
| [01-core/](./01-core/) | 框架抽象 + 默认实现（数据 / 数据流 / 命令 / UI）| 不动，整体复制 |
| [02-mod/](./02-mod/) | 业务方定义本 skill 形态（entity 清单 / command 表 / 布局 / renderers / 09-i18n）| **就这里要填** |
| [03-css-rules/](./03-css-rules/) | 视觉规则·"看"（design token / UI 模块 / 功能区）| 不动，整体复制 |
| [04-ops/](./04-ops/) | 运维·"配"（配置管理）| 不动；通用 knob 回 ZE Skill Build，专属配置走 mod overlay |
| [05-i18n/](./05-i18n/) | 多语言·"说"（zh/en 词表 + resolver；03/04 的孪生层）| 不动，整体复制 |

> 复制/升级的搬运由 [scripts/skill_sync.py](./scripts/skill_sync.py) 处理，受管层（core / ops / i18n / toolchain；Web surface 另含 css / webapp）各自独立判 same / compat_bump / incompat。`02-mod/` 与 `.data/` 永不被覆盖。机器边界见 [ZE_LAYER_MANIFEST.json](./ZE_LAYER_MANIFEST.json)，直接复制安全的完整规则见 [DIRECT-COPY-FLYWHEEL.md](./DIRECT-COPY-FLYWHEEL.md)。

## 顶层 10 个标准文件

| 文件 | 用途 |
|---|---|
| `SKILL.md` | Claude Skill 元数据（name / description / 触发关键词）|
| `README.md` | 本文件 |
| `start.sh` | 生产启动入口；Scoped Deploy 只消费固定适配器准备好的环境和预构建前端 |
| `server.py` | FastAPI server 入口（数据目录约定 / 路由注册）|
| `deploy.json` | 环境无关运行配方与能力要求；不写目标地址、生产端口、账号或凭据 |
| `ZE_DEPENDENCIES.json` | descriptor/lock、运行时、平台、用途和可选适配策略的环境无关声明 |
| `ZE_ARTIFACT_BUILD.json` | 版本、更新通道和环境无关 Variant 生产声明 |
| `ZE_ARTIFACT_VARIANT.json` | release builder 生成的实际 Base/SBOM/签名闭包 |
| `ZE_LAYER_MANIFEST.json` | 受管层/产品层/运行态路径归属契约 |
| `package.json` | 前端依赖（如本 skill 有 web 前端）|
| `pyproject.toml` | Python 项目元数据 + pytest 配置 |
| `requirements.txt` | Python 运行时依赖 |
| `.gitignore` | 忽略数据目录 / 临时文件 / 二进制 |

> 此外:`sdk/`（统一 import 门面 + bootstrap）、`server.py`（FastAPI 运行时）、`scripts/`（CLI 镜像 + skill_sync/scaffold + 各契约门禁 + verify_all）、`webapp/`（前端 workspace）、`05-i18n/`（词表层）也都在顶层。`ZE_BUILD_EVIDENCE.json` 是 [verify_all.py](./scripts/verify_all.py) 全绿后生成的部署证据（非手写源文件）。按需依赖、租约与安全回收见 [DEPENDENCY-ENVIRONMENTS.md](./DEPENDENCY-ENVIRONMENTS.md)。旧目标缺 `ZE_LAYER_MANIFEST.json` 时,`skill_sync --apply` 会按 `ZE_SURFACE.json` 自动补写。

> **状态声明纪律**：完成状态只在机器可读源（`EDITOR_MANIFEST.json` / `introspect.__all__` / `_contract.json`）声明，散文不写会过期的"已/未做"；改代码同次同步真源。规矩见 [scripts/STATUS_DISCIPLINE.md](./scripts/STATUS_DISCIPLINE.md)，由 `doc_status_check` + `ai_interface_check` 两道门禁守（在 verify_all 内）。

正式产物不是手工拼 ZIP。先让 `ZE_DEPENDENCIES.json` 达到 `ready`，再运行：

```bash
python3 scripts/release_builder.py . --check
python3 scripts/release_builder.py . --private-key /secure/release-ed25519.pem --key-id release-main
```

构建器会生成环境无关 Base Artifact、SBOM、签名和 `ZE_ARTIFACT_VARIANT.json`，并在返回成功前复跑完整产物闭包校验。私钥必须在 Skill 目录之外。

## 怎么读

0. **第一次建工具、想从头走一遍** → [GETTING-STARTED.md](./GETTING-STARTED.md)(从零线性走查:scaffold → 跑通 → 填六件套 → 补 AI 三件 → verify_all 全绿 → 浏览器验收)
1. 先看 [PRESET-CONSUMPTION-LADDER.md](./PRESET-CONSUMPTION-LADDER.md) 理解 surface / data preset / layout preset / editor / zone / command 的消费阶梯
2. 再看 [01-core/README.md](./01-core/README.md) 理解框架 4 个子树
3. 再看 [02-mod/README.md](./02-mod/README.md) 看业务方要填什么
4. 更新或迁移其他 skill 前先读 [DIRECT-COPY-FLYWHEEL.md](./DIRECT-COPY-FLYWHEEL.md) 与 [MIGRATION-FLYWHEEL.md](./MIGRATION-FLYWHEEL.md)
5. 单个叶子（如 [01-core/01-data-structure/01-document/](./01-core/01-data-structure/01-document/)）：
   - 先读叶子的 `README.md`（薄导览）
   - 完整规则看 `SPEC.md`
   - 当前实装状态看 SPEC 末尾的"里程碑划分"

## 当前框架与历史工程目录

- 当前 `ZE-video-edit-studio/` 来自一次从零重写，不是从旧实现直接搬运。
- [ZE-video-edit-studio-v1-doctrine/](../ZE-video-edit-studio-v1-doctrine/) 与 [ZE-video-edit-studio-v2/](../ZE-video-edit-studio-v2/) 仅保留历史理由、迁移参照和冻结证据，**不接收新功能**。
- 历史目录是否归档或删除必须另开清理批次，先证明没有活跃消费者；本标准的功能改造不得顺手删除它们。

## 开发纪律

- **叶子自治 M1**：每个叶子先实装 M1（无跨叶子依赖）→ commit → 可以启动别的叶子；M2/M3 等被依赖叶子完成后回头补
- **SPEC 是目标态，代码是当前态**：SPEC 写完整设计，代码按当前里程碑实装；gap 由 SPEC §11 的里程碑表追踪
- **Surface 先声明再派生**：`ZE_SURFACE.json` 是能力形态源；`deploy.json.service_kind` 只描述运行/部署形态；Hub registry 和 catalog 只做展示投影。
- **叶子文件约定**（按叶子语言分两种）：
  - 后端叶子（`.py`，如 document / entity / undo）：`README.md`（薄导览）+ `SPEC.md`（完整规则）+ `<name>.py`（实现）+ `test_<name>.py`（pytest，进 verify_all）
  - 前端叶子（`.tsx`/`.ts`，如 03-editor / 05-preferences / 顶层 05-i18n）：`README.md` + `SPEC.md`，**无 pytest 单测基建**，正确性走真实浏览器冒烟（`e2e_*.py`，命名避开 pytest 收集，需起 dev server 手动跑）
