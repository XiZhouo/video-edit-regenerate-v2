# 02-mod — 业务方填具体

业务方在这定义"本 skill 的具体形态"：本工具有哪些 Entity 类型、注册了哪些 Command、用什么界面布局。

**只写定义清单，不重复 core 的抽象逻辑**。所有抽象机制（Document 怎么工作、Command 怎么注册、Editor 怎么渲染）都在 [01-core/](../01-core/)。

> **⛔ 受管层只读（每次改代码先判断落点）**:本 skill 里 `01-core/`、`sdk/`、`webapp/src`(除 `product/`)、`03-css-rules/`、`05-i18n/`、`04-ops/`、toolchain 脚本都是从 build-v3 整层复制来的受管拷贝,**直接改 = 下一次 `skill_sync` 整层替换时被静默覆盖**(commit/lock/门禁全绿都拦不住;2026-07-02 真实事故:三笔 gallery UI 定制被一次 sync 抹掉)。通用改进 → 改 build-v3 上游再 sync 下发;本 skill 业务 → 只落本目录(02-mod)或 `webapp/src/product/`;热修过受管层 → 同周期回流上游。详见 [01-core/README.md](../01-core/README.md) 顶部判断树。

> **CSS 只读**:`03-css-rules/`(皮肤)是从 build-v3 同步来的只读产物,**别在本 skill 里写/改共享 CSS**(那是原创 CSS,违反规则注册表)。需要新样式 → 回 build-v3 按层补 + `skill_sync --layers css` 下发;本 skill 自身的视觉定制 = 本 skill 皮肤 `03-css-rules/01-tokens/tokens.css` 末尾 `html[data-mod]` 覆盖块(scaffold 注入/手加;02-mod 里不放 CSS,定制后不再 `--layers css` 同步)。规则全集见 [DESIGN-SYSTEM.md §0.3 规则注册表](../DESIGN-SYSTEM.md)。本 skill JSX 用的 `.ze-*`/颜色由已下发的 `css_contract_check` 校验合法(皮肤层级门禁留在 build-v3 上游、只校验皮肤创作,见 §0.2)。

## 6 个定义文件

1. [01-document.md](./01-document.md) — 本 skill 的 Document schema + demo 项目
2. [02-entities.md](./02-entities.md) — 本 skill 的 Entity 类型清单
3. [03-commands.md](./03-commands.md) — 本 skill 注册的 Command 表
4. [04-layout.md](./04-layout.md) — 本 skill 界面布局 + 用哪些 Editor
5. [05-code/](./05-code/) — 业务方实际代码（handler / panel / verify）
6. [06-resources.md](./06-resources.md) — 本 skill 的重资源合同 + 各路径并发声明（乘法审计输入）
