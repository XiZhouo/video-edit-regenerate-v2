# SKIN — 精密瓷釉 Precision Porcelain(v4 · css 16.3.0;基底:晴空瓷釉 v15.4.0)

> 本文件随皮肤文件夹走:**皮肤 = 整个 `03-css-rules/` 文件夹,换肤 = 换文件夹**。
> v4 在晴空瓷釉之上做"词表只增不删"的精密化迭代(§0);§1–§4 是晴空瓷釉基底 DNA,仍然成立,
> 与 §0 冲突处以 §0 为准(已逐处标注)。

## 0. v4 精密瓷釉(css 16.3.0,2026-06-12)

**设计目标**:维持晴空瓷釉的气质(玻璃面板/冷调乳灰/通电 accent),但更**精密、清晰、简洁、有逻辑**。
来源 = 外部设计包(`ZE Gallery.dc.html` / `ZE 精密瓷釉.dc.html` 为视觉验收基准;合入核验记录见
`plans/ze-css-v4-precision-porcelain-evaluation.md`)。

| 决策 | 怎么改 | 为什么 |
|---|---|---|
| 线条系统 | 结构边线从 8.5% alpha 叠色 → 实色三档 `--ze-line-soft/mid/strong`;`--ze-overlay-soft` 只当 hover 叠底 | alpha 边线发虚;实色三档 = 精密感的来源 |
| 玻璃更实 | `--ze-surface-glass` 0.66→0.82、blur 14→10、saturate 1.2→1.1 | 玻璃气质保留,文字/边界清晰 |
| 密度收紧 | 行高 28→26、字阶 -1pt、radius 5/8/10、阴影半径约减半、edge-highlight 0.55 | chrome 更薄,高密度工作台 |
| **36 档工具主题色** | `themes.css`:`[data-ze-theme="NN"]` 每 10° 一档,light/dark × 4 手工设计值(500/700/200/soft);L/C 按色相区手工锁定,任意档主按钮反白对比 ≥4.5:1 | 「每个工具遵守同一套规范,只有主题色不同」;公式推导给不出跨色相的对比度一致性 |
| 派生跟随 | wash/tint/focus-ring/glow 在 tokens.css 用相对色 `oklch(from var(--ze-accent-500) …)` | 换档自动跟随,零散落覆盖 |
| info 换青石蓝 | `--ze-semantic-info` 245°→225° | 运行态不与"选中"(主题蓝)抢蓝,语义分离 |
| Header 语义 | 栅格 `1fr auto 1fr`:项目名居中可改名(`__rename` 笔 + `__sync-dot/__sync-label`),工具名退为左侧字标,FileMenu 入左段,`.ze-header__icon` 词表保留默认隐藏 | 项目名是用户的"文档",是主角 |
| Footer 语义 | 折叠条「左 = 最近一次操作(`.ze-footer__op`),右 = 状态灯组」 | 一条折叠条同时回答"刚发生了什么/系统现在怎么样" |
| 数据序列 | `--ze-data-1…8` 等明度手工配平 | 图表/标记有了规定式序列 |

**机制变更(行为级,词表兼容)**:accent 不再由 `--ze-accent-h` 色相种子推导;`--ze-accent-h/-c`
降为静态兼容别名 → 旧式 `html[data-mod] { --ze-accent-h: … }` 换色**静默失效**(切换时全系 grep
证实 0 使用)。新口径 = 宿主 `<html data-ze-theme="NN">` 一行声明(每 skill 占一档;02/08/16/23
与语义色同相,重状态工具慎用)。门禁规则 4(`ACCENT_HUE_HARDCODED`)改验:设计值只准定义在
tokens.css/themes.css + 每档 light/dark×4 齐全。

**浏览器底线**:相对色语法 `oklch(from …)` 需 Chrome 119+ / Safari 16.4+ / Firefox 128+
(2026 evergreen 全支持);如遇锁旧版内网浏览器,把 themes.css 每档的派生值机械展开为显式值即可。

**改动面**:tokens.css 整重写 + themes.css 新增 + 6 原子尾部追加([v4] 段:header-shell /
footer-shell / editor-shell / thumb / badge / spatial);base.css / icons.css(GENERATED)/
03-zones / 04-editors 零改动。新增令牌 18 + 词表 5 类,详见 MIGRATIONS 16.3.0。

## 1. 皮肤 DNA(一句话)

**玻璃形式 × 瓷釉色彩**:工作台的 Header / Footer / 每个编辑器 pane 都是**浮在环境光上的半透玻璃面板**(空气缝 + 发丝描边 + 顶缘高光 + 背景模糊 + 面板海拔阴影);色彩是**乳白低彩的瓷釉**(中性奶白面、低彩度环境光斑);accent 只在**主操作 / 选中 / 焦点 / 状态灯**上出现并带辉光——「通电」感。

| 维度 | 取值 | 在哪定义 |
|---|---|---|
| 字族 | Geist + Geist Mono(系统栈兜底)| tokens.css `--ze-font-*`;加载在 base.css 顶部 @import |
| 圆角节奏 | 控件 6 / 卡片·弹层 10 / 浮动面板 14 | tokens.css `--ze-radius-*` |
| 玻璃模型 | `--ze-surface-glass`(66%/60% 釉面)+ `--ze-glass-blur` 14px + saturate 1.2 + `--ze-edge-highlight` + `--ze-shadow-panel` | tokens.css + 各 shell 原子 |
| 浮动布局 | 面板间空气缝 = `--ze-space-2`(8px):header/footer margin、layout-root padding、分割条即缝 | header/footer-shell、layout-tree |
| 动效 | snap 缓动 cubic-bezier(0.2,0,0,1);状态灯呼吸 `--ze-dur-breathe` 2.4s | tokens.css |
| 统一标题 | **编辑器标题 = 面板段头 = fs-sm + semibold + 主墨 + 0.01em**(`.ze-editor__title` ≡ `.ze-text[data-role=title]`)| editor-shell.css + text.css(只许改这两处)|
| 明暗 | light + dark 全套;dark 重定义颜色 **和海拔组**(阴影/高光/辉光)| tokens.css `[data-theme="dark"]` |

## 2. 改动清单(相对 v3 15.3.0)

- **01-tokens/tokens.css**:全量取值重调 + 新增 7 档(见 MIGRATIONS 15.4.0;每档注释含消费方)。
- **01-tokens/base.css**:Geist @import + body 环境光斑(引 `--ze-accent-h`,随 skill 主题色联动)。
- **02-atoms**(10+ 文件):editor-shell / layout-tree / header-shell / footer-shell / tab / card / thumb / dialog / popover / toast / button / switch / progress / text 的瓷釉化(详见 MIGRATIONS)。
- **图标系统(15.5.0)**:`02-atoms/icons.svg`(35 极简几何 symbol)+ `02-atoms/icon.css`(`.ze-icon`,data-size/tone/spin);`icons.md` = 语义映射单一真源,glyph 降级列保留,JSX 可渐进迁移。
- **gallery 零改动**:总览仍是 webapp React gallery(内容/逻辑不动)——换文件夹后 `cd webapp && npm run dev` 即是新皮肤。它的 chrome 全部走 var(--ze-*),无需修改。
- **03-zones / 04-editors**:**零改动**(它们已去令牌,只组合 L2;这正是皮肤可整体替换的前提)。
- _contract.json 15.5.0;MIGRATIONS.md 增 15.4.0 / 15.5.0 条目。

## 3. 跟随调整清单(替换文件夹后,皮肤之外要检查的事)

按优先级排;1-2 是必做,其余是核验。

1. **字体加载(必做)**:base.css 顶部用 Google Fonts @import 加载 Geist。内网/离线部署或构建器(vite)对 CSS 内 @import 有限制时,把这一行删掉,改在 webapp 入口 `index.html` 加 `<link>`,或落本地 `@font-face`(woff2 放 webapp 静态目录)。**字体加载失败不破版**——系统栈兜底,只是身份感降级。
1b. **图标迁移(渐进)**:JSX 里的 Unicode glyph 仍合法(icons.md 降级列);迁到 SVG 时用 `<svg class="ze-icon"><use href="…icons.svg#ze-i-x"/></svg>`。vite 环境建议把 icons.svg 内联注入一次(fetch + innerHTML)后用 `#id` 本地引用,避免外链 <use> 的兼容问题。
1c. **gallery 令牌总表补新档(必做,加性)**:用交付包 `webapp-patches/gallery-fixtures-tokens.ts` 覆盖 `webapp/src/gallery/fixtures/tokens.ts`——7 个新令牌进总表 + role 文案与新 tokens.css 逐字同步;其余 gallery 文件零改动。另:原 gallery.html 内联 chrome 的 `.g-motion-viz` 引用了已删令牌 `--ze-dur-normal`(4.0.0 已废,历史遗留)——顺手改成 `--ze-dur-fast` 即可,非本皮肤引入。
2. **gallery 烟测(必做)**:`cd webapp && npm run dev` → gallery.html,L1→L4 走查 + 顶部 light/dark 切换 + **36 档工具主题色切换**(v4 起替代 accent 色相滑块;辉光、wash、tint、focus ring 应全部跟随;若 gallery 自带 chrome 写死了旧 `--ze-*` 取值假设,如"header 必贴边",按新浮动模型微调 gallery 外壳即可,词表无变化)。
3. **backdrop-filter 降级**:旧 WebView / Firefox 关闭 backdrop-filter 时,玻璃面板退化为 66%/60% 半透底——叠在环境光斑上仍可读,但若 skill 的画布内容高对比(如纯黑媒体),可在该 skill 的 `[data-mod]` 覆盖块里把 `--ze-surface-glass` 提到 85-92% 不透明度。无需改皮肤。
4. **05-renderers / 02-mod 残余视觉假设**:门禁保证 `.tsx` 无内联视觉,但若个别 mod 的 HTML 外壳(`SHELL_CSS_TOKEN_ONLY` 范围)写过 "raised=纯白" 之类的颜色组合假设,核验一遍(全部走 `var(--ze-*)` 的不受影响)。
5. **门禁跑一遍**:`css_layer_guard.py` + `css_contract_check.py` + `editor_manifest_check.py`。预期全绿:accent 设计值只住 tokens/themes 且 36 档 light/dark×4 齐全(v4 规则 4);03/04 仍零令牌;行高系统走令牌(26px 自动跟随);新增令牌为加性。base.css 环境光斑仍引 `--ze-accent-h` 兼容别名 —— 合法(别名静态存在),不会报红。
6. **截图基线**:有视觉回归基线的 skill 需重录(整体观感变化是本次的目的)。
7. **per-skill 换色复核**:~~换 `--ze-accent-h` 的 skill~~(v4 起该口径失效,见 §0 机制变更)——改为:声明了 `data-ze-theme` 的 skill 抽查主按钮/选中/focus ring 跟随与反白对比 ≥4.5:1(themes.css 各档已手工锁定,通常免调)。

## 4. 调这套皮肤时的"只许动哪里"

- 想换主题色(v4):宿主 `<html data-ze-theme="NN">` 声明一档(themes.css 36 档)——辉光、wash、tint、焦点环全部自动跟随。~~旧式 `html[data-mod] { --ze-accent-h: … }`~~ 已失效(§0 机制变更;别名仅静态保留)。
- 想更/不那么"玻璃":只动 `--ze-surface-glass` 的 alpha 与 `--ze-glass-blur`。
- 想收紧浮动感(回贴边布局):header/footer 的 margin 与 layout-root 的 padding 归零 + `--ze-radius-lg` 调小,即回 v3 旧形式——形式开关都在 L2 shell 原子里,词表不动。
- 标题视觉:只许改 `.ze-editor__title`(editor-shell.css)与 `.ze-text[data-role=title]`(text.css),且二者必须保持同配方。
