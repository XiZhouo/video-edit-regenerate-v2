---
name: ZE Skill Build
description: >-
  ZE Skill Build —— 所有 ZE/RS 系列 cli/api/web 多 Surface 工具共享的当前底层框架定义，同时是新 skill 的模板/出生点。
  业务方只填 02-mod/（entity 类型 / command / 布局 / renderers / 09-i18n），其余受管层
  01-core/ + 04-ops/ + 05-i18n/ 按 surface 复制；Web 工具额外复制 03-css-rules/ + webapp/（升级走 scripts/skill_sync.py）。
  已落地能力：FastAPI 进程内单例 + SSE 实时推送 + 真账号/多项目按人隔离/协作共享 ACL +
  OpLog 落盘 + undo/redo 跨重启（footer 按钮 + ⌘Z）+ 内容寻址 AssetStore + entity 级加载/管线/占位
  + 编辑器全集（table/form/gallery/canvas2d/scene3d/asset/media）+ 04-ops 配置中枢 +
  05-i18n 词表层（暗色主题 + 中英切换）+ sdk 聚合层 + scripts/ze_command.py CLI 镜像 +
  skill_scaffold 一键生新 skill + verify_all surface-aware 门禁。当用户提到 ZE Skill Build、ZE 框架、core/mod 架构、
  新建 ZE cli/api/web 工具、bootstrap、sdk 聚合、editor 叶子、SSE 实时同步、账号/多项目、undo/redo、
  i18n/主题、配置中枢时使用。
---

# ZE Skill Build

ZE 当前工程框架的定义 + 模板。内部实现代际只用于迁移和兼容记录，不进入公开标准名。完整说明见 [README.md](./README.md)；Surface 规则见 [SURFACE-PROFILES.md](./SURFACE-PROFILES.md)；多环境 recipe、Base Artifact / Series Overlay、API 交付与代码可见性边界见 [DEPLOYMENT-PROFILES.md](./DEPLOYMENT-PROFILES.md)；环境无关依赖、按需准备、租约和安全回收见 [DEPENDENCY-ENVIRONMENTS.md](./DEPENDENCY-ENVIRONMENTS.md)；直接复制安全见 [DIRECT-COPY-FLYWHEEL.md](./DIRECT-COPY-FLYWHEEL.md)；工程护栏见 [ARCHITECTURE-GUARDRAILS.md](./ARCHITECTURE-GUARDRAILS.md)；逐叶子规则看各叶子 `SPEC.md`。

**在四段标准链中的位置**(路由权威见 `ZE-skill-system`):Build 是四核心之一(System 定事实与入口 / **Design** 定业务意图与体验验收 / **Build** 把意图造成可运行候选 / **Deploy** 绑目标上线)。**输入** = Design 产出并确认的 Tool Brief / redesign_spec;**输出** = 可运行候选 + 环境无关 `ZE_DEPENDENCIES.json` / `ZE_ARTIFACT_BUILD.json` + 已签名 `ZE_ARTIFACT_VARIANT.json` + `ZE_BUILD_EVIDENCE.json`。Build 独占「受管代码 / CSS 单源 / 工程门禁 / 版本与同步 / 本机受管依赖环境 / Base Artifact 生产」;不做业务访谈(归 Design)、不写目标服务器事实、不冒充上线(归 Deploy)。理由卡见 `ZE-skill-system/modules/standard-skill-charter.md §1.3`。

## 最小运行

```bash
bash start.sh            # 生产态(默认):构建 webapp/dist → 后端单端口同源托管 UI+API(:8039)
ZE_DEV=1 bash start.sh   # 开发态:后端 API(:8039) + Vite dev(:5173, HMR 跨端口)
```

- 端口由 `PORT` 控（默认 8039，已部署 amber:8039；旧文档的 8000 已废）。
- 后端 API：`GET /health`、`GET /api/document`、`POST /api/command/{id}`、`GET /api/events`(SSE)、
  账号 `/api/auth/*`、项目 `/api/projects`、历史 `/api/undo`·`/api/redo`·`/api/history`、资产 `/api/asset/*`。
- CLI（AI 的左手，server 不在跑时用）：`python scripts/ze_command.py run <command_id> --payload '<json>' [--project <pid>]`
- 数据目录：`$ZE_DATA_DIR`（默认 `<skill>/.data`）；多项目落 `projects/<pid>/`，账号全局共享。
- 验证：`python scripts/verify_all.py`（pytest + css/ops/i18n + deployment-profile + artifact-variant + typecheck + lock 自洽 → 产 ZE_BUILD_EVIDENCE.json）。

## 顶层结构

| 目录 | 是什么 | 业务方动吗 |
|---|---|---|
| `01-core/` | 框架抽象 + 默认实现（数据 / 数据流 / 命令 / UI）| 不动，整体复制 |
| `02-mod/` | 业务方定义本 skill 形态（entity / command / 布局 / 05-code 实现）| **就这里要填** |
| `03-css-rules/` | 视觉规则·"看"（design token / UI 模块 / 功能区）| **只读消费**，整体复制（CSS 飞轮见下） |
| `04-ops/` | 运维·"配"（配置管理）| 不动；通用 knob 回 ZE Skill Build，专属配置走 mod overlay |
| `05-i18n/` | 多语言·"说"（zh/en 词表 + resolver；03/04 的孪生层）| 不动，整体复制 |
| `sdk/` | 框架统一 import 门面（`from sdk import ...`）+ bootstrap | 不动 |
| `server.py` / `scripts/ze_command.py` | FastAPI 运行时 + CLI 镜像 | 不动 |
| `webapp/` | 前端 workspace 入口（也是新 skill 前端脚手架）| 不手改 core workspace；业务 UI 走 `02-mod` renderer/product_dir |
| `ZE_LAYER_MANIFEST.json` | 受管/产品/运行态路径归属契约 | 随 scaffold/sync 生成；迁移前先看 |
| `ZE_DEPENDENCIES.json` | 环境无关依赖声明 | descriptor/lock 变化后重生成；发布必须 ready |
| `ZE_ARTIFACT_BUILD.json` | 环境无关 Variant 生产声明 | 只声明运行变体/交付模式/产物角色，不写 Target |
| `scripts/release_builder.py` | Base/SBOM/签名实际生产器 | 私钥必须在 Skill 根之外；成功前复验完整闭包 |

### 扩展与迁移（飞轮文档）

- 新需求进不进 core / 落哪层 / 什么流程 → [EXTENSION-RULES.md](./EXTENSION-RULES.md)
- 受管层如何做到可直接复制、下游 drift 怎么处理 → [DIRECT-COPY-FLYWHEEL.md](./DIRECT-COPY-FLYWHEEL.md)
- 机器可读路径归属 → [`ZE_LAYER_MANIFEST.json`](./ZE_LAYER_MANIFEST.json)；旧目标由 `skill_sync --apply` 自动补写
- 迁移存量工具八步法（先迭代 build 再迁移）→ [MIGRATION-FLYWHEEL.md](./MIGRATION-FLYWHEEL.md)
- 全系需求矩阵 + 冲突注记（迁移前必查）→ [MIGRATION-MATRIX.md](./MIGRATION-MATRIX.md)
- 「我要做一个 ___ 工具」任务配方 → [COOKBOOK.md](./COOKBOOK.md)

### CSS 飞轮（迁移 / 加功能 / 改内容时怎么动 CSS）

「和 CSS 有关」的一切都受 **[DESIGN-SYSTEM.md §0.3 规则注册表](./DESIGN-SYSTEM.md)**(19 条机械门禁)约束,单源在 ZE Skill Build:

- **上游创作**:新 CSS 需求一律加在 **ZE Skill Build** 的对应层(L1 token / L2 原子 / L3-L4 只组合),由 Build 全套皮肤门禁(`css_layer_guard`/`css_container`/`responsive`/`rule_registry`)把关,`skill_sync --layers css` 下发。
- **下游只读消费**:迁移出的 skill 的 `03-css-rules` 是同步产物,**只读勿手改**;skill 自身视觉定制 = 本 skill 皮肤 `01-tokens/tokens.css` 末尾 `html[data-mod]` 覆盖块(scaffold 注入/手加,不新增 `.ze-*` 词表;定制后不再 `--layers css` 同步)。
- **门禁分工**:皮肤层级门禁(`css_layer_guard` 等)校验「皮肤如何被创作」、**只在 Build 上游跑**(不下发——在下游对只读皮肤跑只会报上游旧债);下游那道门 = `css_contract_check`(已随飞轮下发、必跑:校验下游 JSX 用的 `.ze-*`/颜色合法)。
- 详见 DESIGN-SYSTEM §0.2(授权与传播模型)+ §0.3(规则注册表 + 上下游分工)。
