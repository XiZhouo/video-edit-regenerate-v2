# 03-css-rules — 视觉系统

ZE skill 共享的视觉规则。跟 [01-core/04-user-facing/](../01-core/04-user-facing/) 平级，独立成一块。

> 当前皮肤:**精密瓷釉 Precision Porcelain v4**。目录已经按对象模块化：每个 token/UI/zone/editor/composition 对象都有自己的文件夹、代码、规则、校验和扩展说明。

## 规则注册表(权威)+ CSS 住址

全部 CSS 合规规则的**单一权威清单 = [DESIGN-SYSTEM.md §0.3 规则注册表](../DESIGN-SYSTEM.md)**(19 条,每条对应一个机械门禁 `kind`)。本目录任何文件、各层 SPEC、`.tsx`/HTML 里和 CSS 有关的改动都受其约束;迭代/迁移/加功能时不确定就查注册表。

**CSS 住址(规则 `CSS_OUTSIDE_SKIN`)**:**全仓任何 `.css` 文件只准住本目录 `03-css-rules/`。** `webapp/` `01-core/` `02-mod/` 等别处一律不许有 `.css` —— 那些地方只 `import` 皮肤 CSS + 在 JSX 贴 `className`。外壳 chrome(画廊等)若必须自带样式,只能内联在 `webapp/*.html` 的 `<style>` 里且只用 `var(--ze-*)` 令牌(规则 `SHELL_CSS_TOKEN_ONLY`)。这条把「皮肤即唯一 CSS 真相源」钉成门禁。

## 阅读顺序

0. [CSS-MODULE-PHILOSOPHY.md](./CSS-MODULE-PHILOSOPHY.md) — 为什么每个对象都是文件夹，以及 mod 怎么消费。
1. [STATE-MODEL.md](./STATE-MODEL.md) — 空、执行中、正常、异常等状态如何从 entity 传到 UI 对象自身。
2. [01-tokens/](./01-tokens/) — 令牌与状态语言。
3. [02-atoms/](./02-atoms/) — UI 对象（按钮、输入、表格、缩略图、shell、状态对象等）。
4. [03-zones/](./03-zones/) — 编辑器内部功能区。
5. [04-editors/](./04-editors/) — 单个编辑器。
6. [05-compositions/](./05-compositions/) — 工作台、Header/Footer、Layout、账号等编辑器组合层。

## 全栈总览页

设计体系总览已合并进**交互式 gallery**(`webapp/gallery.html`,React)——单页从令牌到组合层(L1→L5),每层带配套规则卡 + 真实视觉/组件,L4 是真编辑器,L5 是工作台组合。顶部 light/dark + 工具主题色切换实时换肤驱动全页。

跑法:`cd webapp && npm run dev` → 打开 `http://localhost:5173/gallery.html`。

> 旧的静态 `03-css-rules/gallery.html`(L4 是死 mockup,与 React 版重复维护会漂移)已删除,被上面这个唯一总览取代。
