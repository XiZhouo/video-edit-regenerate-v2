# CSS 层迁移记录(框架侧,随 03-css-rules 整树复制)

CSS 契约见 `02-mod/_locks/css.lock.json`:= `--ze-*` token 词表 + `.ze-*` class 词表 + `data-*` 属性词表。JSX 只发这套词汇,CSS 只针对这套词汇。

## 版本规则
- **MAJOR bump = 破坏契约**:重命名/删除任一 `--ze-*` token 或 `.ze-*` class、改 `data-*` 语义。`Skill.Sync` 报 `incompat` + token/class diff;作者据本文件改 `02-mod/07-theme.css` 或 JSX 的 class 使用后手动 bump。
- **MINOR / PATCH = 加性**:新增 token / class / 组件模块。`compat_bump`,自动复制,mod 零改动。

## 定制纪律(再次强调)
skill 永不编辑本目录任何文件(每次迭代被整树复制替换)。所有定制只在 `02-mod/07-theme.css`,且只用 token 重写 + `html[data-mod="<skill>"]` 作用域。重写 token(非写死 hex)→ dark 模式自动跟随。

## 记录

### 20.2.0 (2026-07-19) — 单一助手合体(加性 MINOR)

- +`.ze-guide__hotspot`:停靠精灵上的透明可点区(点击=开合对话面板;教学/回执期不渲染)。
- +`.ze-guide__picker[data-side="left"|"right"]`:tour 选择器随对话面板停靠边。
- `@deprecated`:`.ze-guide__launcher`/`.ze-guide__mini`(独立「教我用」角标退役,入口移入
  对话面板栏);copilot-shell `__orb/__orb-core` 不再被渲染。类与令牌全部保留至 G1 日落,
  禁新增消费。无删除/改名 → MINOR。

### 20.1.0 (2026-07-19) — copilot-shell 精灵同体材质(加性 MINOR)

- CopilotDock 小球(`.ze-copilot__orb-core`/`__pulse`)改为**变形体停靠态材质**:`--ze-guide-fill`
  白芯 + accent 1.5px 描边 + 柔光,与 guide-shell 精灵合一(同屏一个助手实体;05号规划本意
  「精灵球=CopilotDock」)。
- busy=thinking 呼吸加速:120ms 高频闪改 `--ze-dur-breathe-fast`(新令牌 1.4s;定稿 §5-1,
  气泡三点仍是 thinking 主信号)。
- 词表:+`--ze-dur-breathe-fast`;guide-shell 补 `.ze-guide__note[data-tone="danger"]` 危险色(确认拉起错误行,属性选择器加性);无删除/改名;面板与交互零改动。

### 20.0.0 (2026-07-19) — 引导精灵变形体 v2(guide-shell 视觉整体替换,破坏性 MAJOR)

- **设计真源**:《ZE精灵变形体视觉设计-20260718》(用户 10 轮迭代定稿 + sprite-v2.html 参考实现);
  参数照搬 `spriteEngine.ts` 的 `SPRITE_PARAMS`,不原创。`MOTION.md` 精灵章整体重写为变形体契约。
- **删除(破坏点)**:旧 4 层 Siri 美术 `.ze-guide__shell/__aura/__core/__spark(--b)/__ring` +
  目标视觉贴皮 `.ze-guide__frame/__cursor/__ripple` + 12 个 `ze-guide-*` keyframes。精灵改为
  **单实体白芯蓝边变形虫**(球⇄光标⇄环形矩形,28 点系逐点插值),亲自飞到目标变光标/长成环带,
  贴 rect 的独立指向环/取景框不复存在。旧 frame `top/left` transition 记债随删清账。
- **新增**:`.ze-guide__stage/__trail/__glow/__svg/__shape/__veil/__wash/__rip/__mini`(几何由
  `webapp/src/lib/spriteEngine.ts` rAF 驱动;皮肤只管填色/描边/涟漪/launcher/降级);令牌
  `--ze-guide-fill/veil/wash/trail-hi/shadow/frame-glow`。
- **废弃保留**:`--ze-orb-a/b/c`、`--ze-guide-aura/core-hi/spark/edge` 标 `@deprecated`,定义保留
  (下游 19.x 过渡),禁新增消费,日落随 G1 列车。
- **不变**:`data-form`/`data-state` 词表、bubble/launcher/picker 结构类、`--ze-z-guide`/`--ze-spring`。
- **下游**:`Skill.Sync` 将报 incompat(class diff)。**禁单发**——与 webapp 模板
  (GuideLayer.tsx + spriteEngine.ts)同车随 G1 列车下发;@layer 改造不在本版。

### 19.9.0 (2026-07-18) — 前端美学批1:排版角色/动效语义档/微交互族谱(加性 MINOR)

- **tokens**:`--ze-lh-loose`(1.65,CJK 段落)/`--ze-tracking-caps`(0.06em,caps 角色唯一字距例外)/`--ze-dur-enter`(240ms)/`--ze-dur-exit`(160ms)/`--ze-card-pad`(10px,声明先行,card atom 改值随批2)。
- **新 atom `typography/type-roles`**:`data-ty` 8+1 排版角色矩阵(display/title/panel/body/label/meta/micro/caps+num),选角色即得整组排版;9px 只限冗余辅助信息。
- **新 atom `feedback/interaction`**:选中态唯一配方 `[data-selected]`(accent 边+wash+glow)/顶缘高光 `.ze-card-hi`/13 手感族谱(`.ze-press-sm`/`.ze-lift`(仅 ≥32px)/`.ze-enter*` 错峰/`.ze-springy`/`.ze-breathe`)/全局动效降级(`data-motion="off"`+`prefers-reduced-motion`)/`:focus-visible` 焦点环。
- **guide-shell**:T15-1 内层几何真 morph(enter 档过渡);T15-4 sprite 位移 transform 化(translate3d 合成层)+退场余晖(exit 档 opacity)。
- **MOTION.md 升格**为全局动效契约(五档语义/缓动分工/属性纪律)。
- **纯加性零破坏**:未改任何既有 token 值与既有类;三条门禁(data-ty 闭集/内联 fontSize 判红/动效字面量)随美学批2 一次 toolchain 落。改前截图基线=bot-规划/css基线-20260718。

### 19.4.0 (2026-07-14) — Timeline 拉范围创建草稿态（加性 MINOR）

- 新增 `.ze-timeline__clip[data-draft="true"]`：L2 timeline 原子统一提供 token 化虚线信息色轮廓、半透明与 `pointer-events: none`，让尚未提交的拖拽区间可见但不遮挡轨道 pointer 提交链。
- 调用方只发瞬时 `data-draft` 事实属性以及运行时位置/尺寸；不得在 TSX 或 mod 内联透明度、轮廓、颜色或事件命中规则。draft 不代表实体、选择或运行态。
- 这是 opt-in 加性状态；既有 timeline 若不发 `data-draft`，视觉和交互逐字保持。无 token/class 删除或重命名，也没有既有 data-* 改义，因此 19.3.1→19.4.0 为 MINOR。`EDITOR_MANIFEST.json` 不变：本次新增的是 timeline 原子状态，不是新的 editor 能力声明。

### 19.3.1 (2026-07-13) — Editor manifest 个人视图能力账实纠正（metadata PATCH）

- `scene3d` 将 `camera-persist` 从 planned 移入 builtin，并在 `viewState` 登记 `cameraMode`；`form/property` 登记基于稳定 `InspectorSectionSpec.id` 的个人折叠持久化，同时明确无稳定 id 的 `x-group` 仍是挂载期状态。
- editor-manifest gate 交叉检查这些机器声明与 core 实现 seam，避免实现已经落地、manifest 仍宣称 planned 的假绿。
- 没有新增、删除或改名任何 CSS token/class/data-*，也没有视觉取值变化；因此 19.3.0→19.3.1 为 metadata PATCH。

### 19.3.0 (2026-07-13) — Table/Form 实体局部运行态结构（加性 MINOR）

- Table 新增 `.ze-editor__row-actions-inner` 与 `.ze-editor__tbl-status-row`，让进度和删除动作共存，并把错误作为紧邻真实实体行的附着状态；活动行仍可选择和检查。
- Form 新增 `.ze-editor__form-status` 与 `.ze-editor__unit-preview-run`，错误不再居中替换整块表单，运行提示可叠在仍可见的预览上。既有 token、class 与 data-* 无删除/重命名，19.2.0→19.3.0。

### 19.2.0 (2026-07-13) — CardList 实体运行态局部错误布局（加性 MINOR）

- 新增 `.ze-thumb__error`，只负责卡片内错误信息的最小宽度与 card/compact list 跨列结构；颜色、边框、扫描动画仍由 L2 `state-frame` 和 `ErrorPlaceholder` 单一拥有。
- `.ze-thumb[data-status="error"]` 不再强制 `cursor: default`，因为错误实体仍可选择和检查。class/token/data-* 无删除或重命名，19.1.0→19.2.0。

### 19.1.0 (2026-07-10) — 操作手册安全阅读原子(加性,MINOR)
- 新增 L2 `.ze-operation-manual` 原子，在 `.ze-om` 语义根内统一负责标题、列表、表格、代码、媒体、`details` 与 `.ze-om__callout` 的阅读排版；所有视觉值只消费 `--ze-*` token。
- core 手册渲染改为 DOMPurify allowlist 边界：只保留语义/媒体标签和 `ze-om*` class，拒绝脚本、事件、表单、SVG、嵌入页、`style`/`link` 与危险 URL；媒体相对路径以最终 fragment URL 为基准解析。
- 下游 `operation-manual.html` 若仍内嵌 `<style>` 或 `style` 属性，升级后会被刻意移除；把通用结构改为 `.ze-om*` 语义 class，不要在工具侧重建私有 CSS。
- 词表只新增 class，无删除/重命名 → MINOR。

### 17.4.0 (2026-06-13) — gallery 吃 v2 + 筛选条(加性,MINOR)
- 新增 `.ze-editor__zone[data-role="filterbar"]`(筛选类操作区行字面:状态 pill 视图筛选——改 `view_state` 即时、可逆、无 confirm;**不锁** `data-lines=1`(pill 多则换行成簇可纵向生长)+ 补 `padding-block:var(--ze-space-1)` 与 1×row-h 行视觉对齐;其余几何复用 `__zone` 通用骨架) + `.ze-editor__zone[data-layout="wrap-cluster"]`(换行成簇布局:`flex-wrap:wrap` + `min-height:var(--ze-row-h)`)。字面供 `editor_manifest_check` 子串匹配 FilterBarZone selector(行几何令牌仍住 L2 `editor-shell.css`,L3/L4 零令牌)。
- 配套 core 1.37.0:新 `zones/FilterBarZone`(状态 pill 单选,改 view_state 即时无 confirm,视图类);`zones/ToolRowZone` 补可选 `viewState`/`onViewChange` props 并传 `renderToolbarAction`(修 P1 遗留缺口——此前写死 undefined 只能渲 command,现可渲 search/cycle/toggle;canvas2d 现调用不传不受影响);`GalleryEditor` 删 `ToolbarZone` → `header={<HeaderZone title count/>}` + `zones=[ToolRowZone(搜索/档位 cycle/对比 toggle 从原头排迁出) / FilterBarZone(filters) / 条件命令区]`;批量采纳/删除(`GalleryAction` scope:selection)从 filterbar 剥离为**条件命令区**(选中>0 才渲该钮,走 confirm + busy 单飞,容器 `.ze-editor__zone[data-role=control]` 复用 ControlZone 几何;ControlZone 契约保持纯 `ToolbarAction[]` 不被 GalleryAction 污染)。
- gallery manifest 条目:zones 改 `header/tools/filterbar/control/body`(工具行 zone id=`tools` 非 `toolbar`,避 `TOOLBAR_SELECTOR_NOT_STANDARD`)+ 标 `noRawToolbar:true`(gallery 不再裸用 `.ze-editor__toolbar`,门禁拧紧到 gallery)。
- `gallery.css`(L4,零令牌):`.ze-editor__filterbar` / `.ze-editor__filteractions` 改造后零 JSX 消费转孤儿,标 `⊘deprecated 待 MAJOR 清理`(按删 class=MAJOR 家规不在本 MINOR 批删,同 `.ze-editor__toolbar` / `.ze-share-*` 处置);`.ze-editor__filtergroup` 仍由 FilterBarZone 消费、保留。
- 无删改 token/class → 纯加性 MINOR。

### 17.3.0 (2026-06-13) — pipeline 吃 v2 + 控制行(加性,MINOR)
- 新增 `.ze-editor__zone[data-role="control"]`(控制类操作区行字面:对象/过程生命周期命令——暂停/继续/重试/导出;几何复用 `__zone` 通用骨架,本规则只标语义 `justify-content:flex-start`)。字面供 `editor_manifest_check` 子串匹配 ControlZone selector(行几何令牌仍住 L2 `editor-shell.css`,L3/L4 零令牌)。
- 配套 core 1.36.0:新 `zones/ControlZone`(run 生命周期命令件,`ToolbarAction[kind=command]`→dispatch);`PipelineEditor` 删手搓 `.ze-editor__toolbar` → `header={<HeaderZone/>}` + `zones=[run-select toolbar 行 / ControlZone / 只读 notebook 导出行]`;每 step cell 与操作台 cell 改**信息+控制单元**(快照/日志/参数表单=info、rerun/run_from/Run=control,复用 `.ze-editor__unit` 词族,嵌套限一层)。
- `pipeline.css`(L4,零令牌)删孤儿 `.ze-pipeline__toolbar`,cell 视觉对齐 `__unit` 二分(卡片自带边框、内距移入信息/控制区)。
- 门禁转硬:`editor_manifest_check` 新增 `ZONE_NEST_VALID`(声明 nest 须 `unitSelector`+`infoSelector` 且各在 CSS、嵌套限一层)+ `EDITOR_NO_RAW_TOOLBAR`(`noRawToolbar:true` opt-in 名单禁裸 `className="ze-editor__toolbar"`,不误伤未迁编辑器);pipeline manifest 条目 toolbar zone 拆 header+control、cells/console 加 nest、标 `noRawToolbar:true`。
- 无删改 token/class → 纯加性 MINOR。

### 17.2.0 (2026-06-13) — 属性面板「信息+控制单元」(加性,MINOR)
- 新增 `.ze-editor__unit`(属性面板核心砖块,竖排卡,单元间发丝线 `var(--ze-line-soft)`) + `.ze-editor__unit-info[data-role=info]`(信息区,字段竖流) + `.ze-editor__unit-control[data-role=control]`(控制区,FormAction 钮组,`border-top:1px solid var(--ze-line-soft)` 作信息↔控制二分边界)。
- 行几何令牌住 L2 `02-atoms/editor-shell.css`(同 `__zone` 法理),L3/L4 零令牌;`data-role=info/control` 字面出现在 css(供 `editor_manifest_check` 子串匹配)。
- 配套 core 1.35.0:死拓展点 `InspectorSectionSpec` 接线(`FieldRow` 从 FormZone 抽出 + `InfoControlUnit` 组合 + `FormEditor.sections` 路径,空 sections 字节等价);`preview` kind 规划(planned),本批落 fields/readonly-kv/params 三 kind。
- 无删改 token/class → 纯加性 MINOR。

### 17.1.0 (2026-06-13) — 编辑器功能区行栈 v2 地基(加性,MINOR)
- 新增 `.ze-editor__header`(身份行,几何同 __toolbar:1×row-h 三连锁+发丝下缘) + `.ze-editor__zone[data-role=toolbar|filterbar|control][data-lines=1][data-layout=hug-scroll]`(操作区行通用骨架,行几何令牌住 L2) + `.ze-editor__zone-actions`(右侧动作容器,同 __toolbar-actions)。
- 配套 core 1.34.0:EditorFrame +`header`/`zones` 槽(旧 `toolbar` @deprecated 保活) + 新组件 HeaderZone(身份)/ToolRowZone(工具行)/renderToolbarAction(共享操作件渲染)。
- canvas2d 首迁:三路径 title→HeaderZone、工具+裸Run→ToolRowZone(裸 Run 收编为声明式 runAction 走 dispatch);CanvasToolbarZone 保留给 graph/scene3d(迁移期并存)。
- 旧 `.ze-editor__toolbar` 一字不动保活;全迁后 ⊘defer 待 MAJOR。纯新增 class/data-* → MINOR。

### 17.0.0 (2026-06-13) — 移除运行时分屏/关闭槽位(删 class+icon 令牌,MAJOR)
- 配合 core 1.33.0(用户裁决整套移除):删 `.ze-slot__chrome` 词族(pane 悬浮分屏/关闭入口;`.ze-slot`/`.ze-slot__empty` 保留);删图标 `split-pane`(icons.svg 删 symbol → icons_build 再生成 icons.css/icon-map.css/icons.json,icons.md 注销;`--ze-icon-split-pane` 令牌与 data-icon 名一并消失,零消费者)。删 class/token 按规则 MAJOR。

### 16.6.0 (2026-06-12) — 统一拉伸手柄语言(compat;补记,详见 _contract.json 编年史)
- footer 缝手柄对齐 `.ze-split__divider`(resizer 词族重写为前置兄弟缝元素/handle 拉伸暗示删除,词表 class 无删除)。

### 16.5.0 (2026-06-12) — 浮层瓷盘 surface-float(加性 MINOR;补记,详见 _contract.json 编年史)
- 新增 `--ze-surface-float` 第四 surface 档;popover/dialog/toast/header 菜单浮层体由 glass 改 float(全不透明,深度走投影),backdrop-filter 退出浮层体;门禁 FLOAT_SURFACE_OPAQUE(R-FLOAT)。

### 16.4.0 (2026-06-12) — header-v2 词族/icon(加性 MINOR;补记,详见 _contract.json 编年史)
- `--ze-font-display` 衬线品牌栈;`.ze-header__file-*` 文件切换器词族;icons +upload/save-as/snapshot/share;`.ze-share-*` 三原子 ⊘defer。

### 16.3.0 (2026-06-12) — 精密瓷釉 v4:线条系统 + 36 档工具主题色(加性 MINOR,含行为级机制变更)
- **外部设计包合入**(`~/Downloads/ze 111.zip`;基线逐字节核验 = 当前 16.2.1:未改文件全等、6 原子真追加、令牌词表 0 删 18 增;核验与影响面记录见 `plans/ze-css-v4-precision-porcelain-evaluation.md`):`tokens.css` 整重写(实色边线三档/玻璃 0.66→0.82 + blur 14→10/字阶 -1pt/radius 5/8/10/行高 28→26/墨阶压深/阴影减半/info 245°→225° 青石蓝)+ **`themes.css` 新增**(36 档 `[data-ze-theme="NN"]` 手工主题色,light/dark × 4 设计值,每 10° 一档)+ 6 原子尾追加 [v4] 段(header-shell/footer-shell/editor-shell/thumb/badge/spatial)。
- **新增令牌 18**:`--ze-line-soft/mid/strong`、`--ze-accent-tint/soft`、`--ze-semantic-{success,info,warn,danger}-wash`、`--ze-data-1…8`。**新增词表 5 类**:`.ze-header__rename` / `.ze-header__sync-dot[data-state]` / `.ze-header__sync-label` / `.ze-footer__op`(内 b/em)/ `.ze-header__icon` 默认隐藏(词表保留);另立 `data-zone="toolbar|canvas-toolbar|content|statusbar"` 功能区标注惯例(本轮铺 toolbar/canvas-toolbar/statusbar 三个单根操作区,content 侧根节点异构缓办)。
- **主题色机制切换(行为变更,词表兼容)**:accent 不再由 `--ze-accent-h` 推导,`--ze-accent-h/-c` 降为静态兼容别名 → 旧式 `html[data-mod]{--ze-accent-h:…}` 换色**静默失效**(切换时全系 grep 证实 0 使用,故仍按家规词表口径计 MINOR);新口径 = `<html data-ze-theme="NN">`(DESIGN-SYSTEM §0.2)。wash/tint/focus-ring/glow 由 accent-500 相对色派生,`oklch(from …)` 需 Chrome 119+/Safari 16.4+/FF 128+(evergreen 2026 全支持;老浏览器兜底 = 每档机械展开显式值)。
- **配套**:门禁规则 4 `ACCENT_HUE_HARDCODED` 改验「设计值只准定义于 tokens/themes + 每档 light/dark×4 齐全」(css_layer_guard,toolchain 1.13.0);core 1.30.0(Header 三段重排:FileMenu 入左段/项目名居中可改名/sync dot;Footer 折叠条 op 行;ZeIcon edit 笔;EditorFrame 三操作区 data-zone);gallery 主题滑杆 → 36 档切换器 + 新令牌入册。皮肤 DNA 落档 SKIN-PORCELAIN.md §0。

### 16.2.1 (2026-06-12) — refresh 旋转偏摆修复 + 图标风格规范落档(PATCH)
- **refresh 重绘**:旧弧 `M13 7.4A5 5 0 1 0 12.6 9` 几何圆心解算在 (8.02, 7.00)——`data-spin` 绕盒中心 (8,8) 旋转时偏摆 1px(用户目检抓出)。新弧端点 (11.54,11.54)→(11.54,4.46) r=5(圆心 (8.009,8.000),偏差 0.009px),270° 弧 + 弧端 L 形括号箭头;四相位(0/90/180/270°)截图核验同心。
- **icons.md +「风格规范(创作说明)」节**:极简优雅基因判据 / 16 网格与 ≥2px 安全边距 / 1.5 圆头笔画(pause=2 唯一例外封口)/ currentColor 单色(mask 只取 alpha,禁分层着色)/ 描边为主的形态二分 / 几何词汇与虚线语义 / **spin 类圆弧几何圆心必须 =(8,8) 铁律(本次返工教训)** / L 括号箭头句法 / 命名与禁则 / claude design 交付流(只动 icons.svg → icons_build → 本表 → 门禁)。
- 既有图标几何取值重调、token/class 词表零变 → **PATCH**(16.2.0→16.2.1)。

### 16.2.0 (2026-06-12) — 图标令牌图库(token 化,加性 MINOR)
- **图标 = 令牌**:美术源 `02-atoms/icons.svg`(63 symbol;本批补画 28 个真实消费者驱动的缺口——rotate/flip-h/flip-v/more/zoom-in/zoom-out/split-pane/step-prev/step-next/loop/unlock/user/cube/chart + 画布工具 14)经 `scripts/icons_build.py` 生成三件 **GENERATED 禁手改**:`01-tokens/icons.css`(`--ze-icon-*` data URI 令牌,foundation 第三件)+ `02-atoms/icon-map.css`(`.ze-icon[data-icon=<name>]` → `mask-image: var(令牌)` + `background: currentColor`)+ `02-atoms/icons.json`(名册)。用法 `<span class="ze-icon" data-icon="close" aria-hidden/>`——在该用的地方直接引用,色随 currentColor/data-tone/暗色全自动,皮肤可经 `[data-mod]` 覆盖单令牌换图标作品。
- **为什么换形态**:15.5.0 的 `<use href="…icons.svg#id">` 外链 sprite 在打包环境路径接不通,落地后全仓零消费(SPEC 自标 ⊘defer);token 化让图标进 CSS 契约词表、可被门禁机器校验。`icon.css` 补 `[data-icon]` mask 底盘(repeat/position/size;background 在生成的逐名规则给——未知名退化为不可见空位而非实心方块)。
- **消费切换**:全仓 unicode 字符图标(~55 处:icon-button 36 + 独立 span + 空态/占位)迁 `ze-icon`;两条字符注入轴(`ToolbarAction.icon`/`CanvasToolDef.icon`)经 `01-layout/lib/zeIcon.IconOrGlyph` 双轨收编(图库名→token 图标,其余字符直显,旧 glyph 注入零破坏);glyph 仍是纯文本场景合法降级。同义裁决入 icons.md(×→close、rotate≠refresh、☀☾→theme、busy=spinner≠图标、export=download、错误占位=warn)。
- **门禁**:新 `icon_tokens_check`(图标R1 `ICON_TOKENS_STALE` 生成物同步 / 图标R2 `ICON_MD_DESYNC` 表⇄实双向 / 图标R3 `ICON_UNKNOWN_NAME` data-icon 名册),§0.3 注册 + rule_registry GATE_SCRIPTS 收编 + verify_all `_run_if_present`(上游-only,下游软跳过);`icons_build.py --check` 同逻辑。
- 纯新增 token/class 属性/文件,无删改既有词表 → **MINOR**(16.1.0→16.2.0)。迭代规则单一真源:`02-atoms/icons.md`。

### 16.1.0 (2026-06-11) — 属性面板/管线操作台配套(加性,MINOR)
- `form.css` +`.ze-editor__actionbar`:form 编辑器业务动作条 zone(FormAction 闭集钮组;flex-wrap 换行、flex:none 不抢 body 高、min-width:0 容器R2;视觉走 .ze-button,内距/间距贴 .ze-pad/.ze-gap——gallery `__filterbar` 同款零令牌结构)。SlotFormActions(slotAdapters 内联 style 补丁)就此收编进 CSS 体系。
- `pipeline.css` +`.ze-pipeline__cell[data-cell="console"]`(StartConsole 操作台 cell,常驻 .ze-pipeline 顶部 flex:none)+`.ze-pipeline__cell[data-collapsed="true"]`(step 折叠后只剩 disclosure 头行,gap 收 0);matrix running 格子在 font-weight 之上复用既有 `@keyframes ze-pipeline-pulse`(执行中格子有运动感;零新 keyframes、零新 class → 词表只加 data-* 两值)。
- 纯加性:无删改 token/class/data-* 语义 → **MINOR 16.0.0→16.1.0**。

### 16.0.0 (2026-06-11) — canvas2d 纯化删面板族 + 卡片列表词表(删 class,MAJOR)
- **删**(M3 纯化,家规删 class=MAJOR):`spatial.css` 的 `.ze-layered-canvas__panel/__panel-section/__panel-title/__layers/__layer-row(+data-active)/__props(+label/span)/__filter-row(+span)/__empty/__brush-controls/__actions` 全族 + 768 响应行——图层列表/属性面板从画布摘除(列表=gallery 卡片列表实例,属性=form,经 L5 layered_canvas 组合;core 1.27.0 实体化);`.ze-layered-canvas` 根 grid 改单列。**新增** `.ze-layered-canvas__hud-brush`(笔刷双滑杆底缘浮条)。
- **加**(原拟 15.10.0 并入本版):`thumb.css` +`[data-thumb-size="sm"/"lg"]`(网格列宽三档+列表两模式媒体列宽)/`[data-media="none"]`(无媒体卡列模板)/`.ze-thumb__toggles`(眼睛/锁钮:媒体卡贴媒体区左上、无媒体卡行首内联 data-inline)/`.ze-thumb[data-hidden]`(整卡压暗,钮区不压)/`[data-locked]`(左缘锁条);`gallery.css` +`.ze-editor__filterbar/__filtergroup/__filteractions`(筛选/动作区,零令牌结构)。
- 零消费者证据:三 cohort 均默认皮肤、02-mod 零 `.ze-layered-canvas__panel*` 引用;同步走 `--layers css` 对齐。

### 15.9.0 (2026-06-11) — canvas2d layer brush cursor/controls 扩展(加性,MINOR)
- `spatial.css` 在既有 `.ze-layered-canvas__cursor` 上加性支持 `--ze-brush-size` / `--ze-brush-hardness` 运行时 CSS vars 与内部硬度环,让 layer 编辑与历史 maskField 单层像素入口共享画前笔刷预览语义。
- 新增 `.ze-layered-canvas__brush-controls` 作为图层属性面板中的笔刷控制组。所有视觉仍在 L2 spatial 原子消费令牌;TSX 只传运行时几何值,不写业务 CSS。
- 新增 class/子结构与 CSS var 传参均加性,无删改 token/class/data-* 语义 → **MINOR**。

### 15.8.0 (2026-06-11) — gallery/card-list thumb 原子扩展(加性,MINOR)
- `thumb.css` 加性扩展 `.ze-thumb-grid[data-layout="card-list"|"compact-list"]`、`.ze-thumb__description`、`.ze-thumb__meta`,把卡片视图、紧凑列表与摘要 chip 收进既有 L2 缩略卡组件原子。`gallery` 的“卡片列表”是同一 ThumbnailGrid 的 layout mode,不是 table list,也不是新 editor class。
- 所有视觉仍只在 L2 `thumb.css` 消费令牌;L4 `gallery.css` 不增加令牌和业务选择器。旧 `.ze-thumb-grid` 未声明 `data-layout` 时保持原网格行为。
- 新增 class/data-* 均加性,无删改 token/class/data 语义 → **MINOR**。

### 15.7.0 (2026-06-10) — pipeline notebook editor entityUi + step controls(加性,MINOR)
- Pipeline editor 机器规范从“run selector 面板”收敛为 **固定 pipeline notebook**:manifest 标明 `fixed-pipeline-bind` 为默认交互,`optional-run-select` 仅在 `allowRunSwitch=true` 出现;summary 区承接 `entityUi` 配表的 label/thumb/summary fields 与运行中动画。
- 加性新增 `.ze-pipeline__entity` / `__entity-main` / `__entity-thumb` / `__cell-actions` 与 `data-animation` 分支(`thumbnail-pulse`、`progress-outline`、`soft-veil`、`scanline`),供管线对应 entity 在暂停/继续/运行态下有可声明的工作台反馈。
- cell 层交互扩展为 `rerun-step` / `run-from-step`,仍只发 command,不直接改 entity。新增 class/data-* 均加性,无删改 → **MINOR**。

### 15.6.0 (2026-06-10) — 表单/审阅 UI 一致性:披露原子 + 控件列几何法(加性,MINOR)
修法:折叠组头不再用 `.ze-button[data-variant=ghost]`(全宽 ghost 在表单列里读作输入控件、`▾` 与 select 下拉指示撞形、sm 高破 row-h 行节奏)。
- **新增 L2 原子 `.ze-disclosure`/`__caret`**(disclosure.css):透明底、1×row-h、caret 列宽=space-3 与段体 pad 对齐、`▸` 随 `aria-expanded` 旋转、hover=overlay-soft、focus-ring;标题排版由子 `.ze-text[data-role=title]` 供给 —— 统一标题配方仍只住 editor-shell/text 两处。inspector.css 头注 + 03-zones/SPEC §5.2 同步改写(并修正 8.0.0 遗留的已删类名清单)。
- **控件列几何法**(field-row.css 加性):固有尺寸控件 checkbox/color `justify-self:start`(修 FormZone 裸 `<input type=checkbox>` 经 grid stretch 整盒拉伸、原生勾形居中如表格单元格;FormZone 补挂 `.ze-checkbox`,归位 §3.2 既有选型法);新增 `.ze-field-row__value` 固定宽右槽(min-width=space-8、右对齐、tabular-nums)替代 Slider/ParamPanel 两处 JSX 内联 minWidth/textAlign;`.ze-param__slider-row` 死选择器标记 deprecated(删除=MAJOR,下批清理)。
- **textarea.css 新增 `data-rows`**(闭集 4|8,height=n×row-h):review 批注从填满 pane 改 4×row-h 有界(review.css `.ze-review__note` 去 flex:1 与 resize:none,类保留作语义挂点;**行为变更:批注框恢复 base 的 resize:vertical,可手动拉高** —— 有界默认高 + 用户自主扩展)。
- **配套法条**:§3.1 成对决策(通过=primary/驳回=secondary,danger 只给不可逆;ReviewEditor 驳回 ghost→secondary)+ 披露头禁按钮法;§3.2 placeholder 禁与 label 同文(ReviewEditor 改 `editor.notePlaceholder`,i18n 2.2.0)。
- 纯新增 class/data-* + 既有类加规则,无删改 → **MINOR**。

### 15.5.0 (2026-06-10) — SVG 图标系统(加性,MINOR)
- **图标全量重设**(贴瓷釉皮肤气质:16×16 网格 · 1.5px 圆头描边 · currentColor 极简几何):新增 `02-atoms/icons.svg`(sprite,35 symbol)+ `02-atoms/icon.css`(`.ze-icon` 原子:`data-size` xs/sm/lg · `data-tone` 同 .ze-text 词表 · `data-spin`),挂进 index.css。`icons.md` 重写为 SVG 映射单一真源(symbol id ↔ 语义 ↔ 用处),Unicode glyph 列保留为降级——**不删旧 glyph 语义,JSX 可渐进迁移**。注意 settings 造型从齿轮改为调节滑杆(语义不变)。
- **gallery 零改动**:总览仍是 webapp React gallery(内容/逻辑不动,换皮肤即新视觉)。唯一跟随项:`webapp/src/gallery/fixtures/tokens.ts` 加性补 7 个新令牌进总表 + role 文案与新 tokens.css 注释同步(补丁见交付包 webapp-patches/,内容侧文件、不在 CSS 契约内)。
- 纯新增 class/文件,无删改 → **MINOR**。

### 15.4.0 (2026-06-10) — 皮肤「晴空瓷釉 Porcelain Glass」:全量视觉重调 + 7 个新令牌(加性,MINOR)
**词表零删改(token / class / data-* 全保留),纯取值重调 + 加性新增 → MINOR。** 皮肤 DNA:玻璃形式(半透面板 + 发丝边 + 顶缘高光 + 浮动层次)× 瓷釉色彩(乳白中性 / 低彩环境 / accent 只在选中、焦点、状态灯上「通电」)。详见 `SKIN-PORCELAIN.md`(含跟随调整清单)。
- **新增令牌(7,均有消费方)**:`--ze-surface-glass`(玻璃面板底)、`--ze-glass-blur`/`--ze-glass-saturate`(backdrop-filter 配套)、`--ze-shadow-panel`(常驻面板海拔)、`--ze-edge-highlight`(顶缘高光)、`--ze-glow-accent`(accent 辉光)、`--ze-dur-breathe`(状态灯呼吸 2.4s)。
- **取值重调(名字不变)**:字族 Geist/Geist Mono 优先(base.css 顶部 @import 加载,离线部署换本地 @font-face);圆角 4/6/10 → 6/10/14;阴影全组换双层弥散(1px 贴地 + 大半径泛光)且 **dark 块重定义海拔组**(此前阴影主题不变);easing-standard → cubic-bezier(0.2,0,0,1) snap;墨阶/面阶按瓷釉重调;`--ze-overlay-soft` 减淡为发丝线档。
- **结构性视觉改造(只动 01-tokens + 02-atoms,03/04 零令牌不变量保持)**:
  - `base.css`:body 铺环境光斑(引 accent 色相种子,随 skill 主题色联动;light/dark 两组)。
  - `editor-shell.css`:`.ze-editor` = 浮动玻璃面板(glass 底 + blur + radius-lg + panel 阴影 + 顶缘高光 + overflow:clip);toolbar/statusbar 底转 transparent;**统一编辑器标题**:`.ze-editor__title` = fs-sm + semibold + 主墨 + 0.01em(与 `.ze-text[data-role=title]` 同配方,全系列标题只许改这两处)。
  - `layout-tree.css`:`.ze-layout-root` 加 space-2 空气缝;分割条从 1px 实线改为 space-2 透明缝(hover/拖拽中心点亮 accent 细线);空 slot 改虚线幽灵轮廓;窄栏堆叠加 gap。
  - `header-shell.css` / `footer-shell.css`:顶/底栏 = 浮动玻璃条(margin 空气缝 + glass + blur + 描边 + 高光 + panel 阴影);菜单弹层/submenu 转玻璃;脉冲动画 400ms → `--ze-dur-breathe` 呼吸;footer 展开主体内胆转 transparent/sunken。
  - `tab.css`:tab 从下划线改「釉面胶囊」(选中 = accent-wash 底 + accent 字);headers 底转 transparent。
  - `card.css`/`thumb.css`/`dialog.css`/`popover.css`/`toast.css`:阴影栈前插顶缘高光;dialog/popover/toast 转玻璃 + 背景模糊;dialog 遮罩加 3px 雾化;thumb 选中加辉光。
  - `button.css`:primary 加 `--ze-glow-accent`;secondary 转 raised 实底。`switch.css`:checked 加辉光。`progress.css`:accent 填充加辉光(语义变体不发光)。`text.css`:`[data-role=title]` 对齐编辑器标题配方(次墨 → 主墨)。
- **不变量核验**:03-zones / 04-editors 仍零令牌;accent 阶梯/wash/辉光全部引 `var(--ze-accent-h)`(ACCENT_HUE_HARDCODED 过);无新字面量字号(HARDCODED_FONT_SIZE 过);断点集未动;行高系统(--ze-row-h=28px)未动;`.ze-tab` 改胶囊后 `data-active/data-disabled/data-density` 语义不变。
- **已知跟随项**:见 `SKIN-PORCELAIN.md`(字体加载、gallery 烟测、backdrop-filter 降级、05-renderers 若有 raised 假设)。

### 15.1.0 (2026-06-09) — 视觉单一真相源:JSX 内联视觉收口 + 门禁规则 8(加性,MINOR)
**立的规则**:所有视觉(色/字号/字重/字体/边框/阴影)只准住 `03-css-rules` 的 CSS;`.tsx` 只发 `className`+`data-*`,不内联视觉值 → 改样式/换皮只动 CSS,JSX 不碰(CSS 模块可独立编辑)。规则落在 4 处供后续 AI:`DESIGN-SYSTEM.md` §0.1(立法)、`02-atoms/SPEC.md` §3.5(怎么贴)/§3.6(如何扩展)、`css_layer_guard` 规则 8 docstring+hint(自解释)、各被改组件文件头(纠正旧「var() 内联即合规」误导)。
- **背景(病根)**:CSS 模块虽 100% 隔离 `03-css-rules`,但 01-core 5 个 chrome 组件(ConfigDialog / confirmDialogs / displayDialogs / formDialogs / Trans)+ UserAvatar 把视觉内联在 JSX;其中写死 `fontSize:13/12`、`fontWeight:600` 绕过 token,而 `css_layer_guard` 只扫 `.css` 看不见。
- **加性新增(MINOR)**:`02-atoms/kbd.css` 的 `.ze-kbd`(键帽,Trans + 快捷键面板共用)+ `text.css` 的 `.ze-rich`(Trans 富文本内联标记容器:子 `<b>` 固定主墨、`<kbd>` 走键帽)+ `.ze-text[data-tone="muted"]`(三级墨)/`[data-tone="secondary"]`(次墨,纯改色不动字号,解耦色与字号)。挂进 `02-atoms/index.css`。
- **JSX 收口**:5 dialog 的内联视觉(色/字号/字重)全迁到 `.ze-text[data-role/size/weight/tone]` / `.ze-kv-list` / `.ze-kbd`,写死字面量清零(`fontSize:12`→`data-size=xs`(fs-xs=11,~1px 收敛));`UserAvatar` 头像底图改为经 `--ze-avatar-url` CSS var 传运行时 URL,静态 `background-size/position` 进 `header-shell.css`;结构性内联(布局/运行时尺寸)保留。
- **门禁规则 8 `LAYER_INLINE_VISUAL`**:扫 `01-core`+`02-mod` 的 `.tsx`,内联视觉(`color`/`background`/`backgroundColor`/`backgroundImage`/`fontSize`/`fontWeight`/`fontFamily`/`boxShadow`/`borderColor`/`textShadow`/`border`)即报。放行:结构性内联、CSS 自定义属性 `'--ze-x': v` 传值、行尾 `/* layer-guard-allow-inline */` 转义。`test_css_layer_guard.py` 加 9 条正负用例(全过)。webapp(gallery dev harness,不出货)不在范围。
- **范围**:只 build-v3 源(框架质量收口 + 加性门禁);下游 cohort 随 Skill.Sync 同时拿到修好的 01-core + 新门禁(sync-safe,无半态)。**纯加性(新增 class/属性,无删改)→ MINOR**。core.lock checksum 变(01-core/02-atoms 在校验范围)、保留;i18n/ops/toolchain 仅时间戳。

### 结构改址 (2026-06-09) — Phase 5:物理目录名对齐设计体系词表(4-skill lockstep,**无版本 bump**)
**纯目录改名,token / class / data-* 词表完全不变 → 不动 `interface_version`(15.0.3 不变),不向下游发 css 接口变更信号。**
- **改名映射**(每个 skill 的 `03-css-rules/` 下):`01-visual-reference→01-tokens`·`02-ui-module→02-atoms`·`03-functional-zone→03-zones`·`04-layout-region→04-editors`。把物理目录名从旧词对齐到 `DESIGN-SYSTEM.md` 的 5 层词表(L1 tokens / L2 atoms / L3 zones / L4 editors)。
- **范围 = 整个 v3 cohort lockstep**:这 4 个目录名是被逐字复制到全 cohort 的框架结构,故 `ZE-skill-build-v3`(源 + 脚手架)+ `ZE-image-prompt-studio-v3` + `ZE-image-cleanup-v3` + `ZE-image-to-splat-v3` 一次性同步改名,保持 cohort 同构(否则部分同步会新旧名混用出错)。
- **机制**:每 skill 原地 `git mv` 4 目录 + 对该 skill 全部跟踪文件 sed 替换 4 个目录名串(只作路径段出现:`@ze/css/<dir>/` import、`../03-css-rules/<dir>/` 相对路径、Python `SKIN_ROOT / "<dir>"`、门禁脚本、docs);从不作 class/token 名,故全局替换安全。`skill_scaffold.py` 同步更新 → 新建 skill 直接产出新名。各 skill `skill_sync.py --init-locks` 重算锁(改名改了被哈希的 01-core import 路径等)。
- **核验**:build-v3 全 15 门禁 + tsc 0 + 浏览器(gallery/index 零 error、`.ze-button` 原子 + `--ze-accent-500` 解析);3 下游各自 `verify_all` 全绿(= 改名前基线)+ tsc 0;全 cohort 跟踪文件残留旧名 grep = 0。
- **无 token / class / data-* 增删改** → 词表零变更,故无 MAJOR/MINOR/PATCH bump;仅记录此结构改址。

### 15.0.3 (2026-06-09) — L5 chrome 轮 batch5(收官):footer(底栏)去令牌 + 门禁豁免清空(迁址,无词表变更,PATCH)
**绝对严格分层最后一块 —— footer.css(L4)`var(--ze-*)` 清零;至此 03/04 全目录零令牌、无任何豁免。**
- **工作台底部固定栏 shell chrome 上移 L2**(新建 `02-atoms/footer-shell.css`):折叠态 segment 行(`.ze-footer__bar`/`__segment*`/`__dot`/`__trace`,状态/同步/进度/错误/trace)+ 拖拽把手/resizer(`__handle`/`__resizer`,含 `[data-expanded]` 态切 + 伪元素提示)+ 展开主体 section(`__expanded`/`__section*`,op log / pipelines / details tabs / 错误列表 / trace 链,49 子类全集)。它是框架级工作台 shell(FooterShell + CollapsedBar/DetailsTabs/OpLogPanel/PipelineList)。含状态化 hover(handle/segment/oplog-entry/pipeline/error-entry)、大量 `data-state`/`data-source`/`data-level`/`data-status` 选择器、`@keyframes ze-footer-pulse` 脉冲、section 左侧竖条边界,这套「状态化 + 令牌 + 动画」与 `.ze-header`(header-shell)/ `.ze-editor`(editor-shell)同类,无法降为 L4 无令牌结构,故整组收成 L2 组件原子。**class 名全不变**(`.ze-footer` 前缀),仅定义文件 L4→L2。
- **footer 复用的通用原子不随迁**:`.ze-tabs[data-density=compact]`/`.ze-progress`/`.ze-icon-button[data-size=xs]`/`.ze-kv-list` 由各自原子文件供给(footer.css 里早已只是注释提及、不自造)。注意 `.ze-editor-footer*`(`03-zones/editor-footer.css`,L3)是**不同 block**,不在本批。
- **L4 `footer.css` 清空为指针 stub**。
- **门禁收官**:`css_layer_guard` 的 `TOKEN_ABOVE_L2_EXEMPT` 清空并**整体删除**,`check_token_above_l2` 简化为纯目录扫描 —— `TOKEN_ABOVE_L2_DIRS`(03-zones + 04-editors)全部 `.css` 一律零令牌、无任何豁免。负向测试:往 footer.css 注入 `var(--ze-ink-primary)` → 报 `LAYER_TOKEN_ABOVE_L2`,还原后绿。
- **至此「绝对严格分层」(foundation 之上只有 L2 直接消费令牌)在真实代码 + 门禁全部落地**:全仓真令牌消费仅在 `01-tokens`(tokens.css 定义 + base.css 文档基线)+ `02-atoms`(原子 + 框架 shell:editor/layout-tree/auth/header/footer-shell + thumb/chart/spatial/timeline/field-row + 基础原子)。
- **无新增/删除/重命名 class**、无 token / data-* 语义变更 → **PATCH**。未触碰 01-core,4 锁无变化。

### 15.0.2 (2026-06-09) — L5 chrome 轮 batch4:header(顶栏)去令牌(迁址,无词表变更,PATCH)
**header.css(L4)`var(--ze-*)` 清零。**
- **工作台顶部固定栏 shell chrome 上移 L2**(新建 `02-atoms/header-shell.css`):三段栅格容器(`.ze-header`/`__left`/`__middle`/`__right`)+ skill 图标/名(`__icon`/`__skill-name`)+ 项目名/同步指示(`__project-name`/`__sync-indicator`)+ 菜单 trigger/popover/submenu/info(`__menu-trigger*`/`__menu-popover`/`__menu-submenu`/`__menu-info-*`)+ 主题切换 + 头像(`__theme-toggle`/`__avatar`)。它是框架级工作台 shell(HeaderShell + LeftSegment/MiddleSegment/RightSegment + menus/controls)。含状态化样式 —— `menu-trigger`/`theme-toggle`/`avatar` 的 `:hover`、`sync-indicator`/`avatar` 的 `data-state` 选择器、`@keyframes ze-header-pulse` 冲突态脉冲、popover/submenu 绝对定位 ——这套「状态化 + 令牌 + 动画」与 `.ze-editor`(editor-shell)/ `.ze-login`(auth-shell)同类,无法降为 L4 无令牌结构,故整组收成 L2 组件原子。**class 名全不变**(`.ze-header` 前缀),仅定义文件 L4→L2。
- **menu-item / menu-group / menu-section-header 通用菜单 class 不随迁**(早已在 `02-atoms/menu-item.css`);header-shell.css 只含 header 专属 chrome(trigger/popover/submenu/info)。`ShareButton` 的 `.ze-share-count` 已随 batch3 进 auth-shell,本批不重复。
- **L4 `header.css` 清空为指针 stub**;从 `css_layer_guard` 的 `TOKEN_ABOVE_L2_EXEMPT` 移除(豁免仅剩 `footer` 1 个)。HeaderShell.tsx 仍 import 本 stub(无副作用;header-shell 样式经 `02-atoms/index.css` 全局提供)。
- **无新增/删除/重命名 class**、无 token / data-* 语义变更 → **PATCH**。未触碰 01-core,4 锁无变化。

### 15.0.1 (2026-06-09) — L5 chrome 轮 batch3:auth(账号面)去令牌(迁址,无词表变更,PATCH)
**auth.css(L4)`var(--ze-*)` 清零。**
- **账号面 shell chrome 上移 L2**(新建 `02-atoms/auth-shell.css`):登录 gate(`.ze-login*`)/ 项目选择器(`.ze-project-picker*`,11 子类)/ 共享对话框(`.ze-share-*`,13 子类)。它们是框架级账号面 shell(LoginPage / ProjectPicker / ShareButton)。含状态化样式 —— 项目行 `.ze-project-picker__row:hover` 切 `accent-wash` 描边、共享 chip、模态 `.ze-share-overlay`(`z-modal`)——这套「状态化 hover + 令牌」与 `.ze-editor`(editor-shell)/ `.ze-split`(layout-tree)同类,无法降为 L4 无令牌结构,故整组收成 L2 组件原子。视觉 DNA 仍复用 `.ze-card`/`.ze-input`/`.ze-button`(本族只补账号面自有布局/文字/行/chip)。**class 名全不变**,仅定义文件 L4→L2。
- **L4 `auth.css` 清空为指针 stub**;从 `css_layer_guard` 的 `TOKEN_ABOVE_L2_EXEMPT` 移除(豁免剩 `header`/`footer` 2 个)。LoginPage/ProjectPicker/ShareButton 仍 import 本 stub(无副作用;auth-shell 样式经 `02-atoms/index.css` 全局提供)。
- **判断说明**:closer read 后定 verbatim 升 L2(非逐 JSX 贴原子)—— picker row / share chip 的状态化 hover 令牌 + 跨 3 个 auth-gated 屏难逐一像素核验,与 editor-shell 同类,verbatim 更稳更一致。`02-atoms` 本会话已实质成为「令牌消费的可复用 CSS 层」= 原子 + 框架 shell 组件。
- **无新增/删除/重命名 class**、无 token / data-* 语义变更 → **PATCH**。未触碰 01-core,4 锁无变化。

### 15.0.0 (2026-06-09) — L5 chrome 轮 batch2:app-shell 去令牌 + 文档基线归 L1(含删 class,MAJOR)
**体系性修正 —— 文档基线归 L1 foundation;app-shell.css(L4)`var(--ze-*)` 清零。**
- **文档基线 → L1**(新建 `01-tokens/base.css`):全局 reset(`*{box-sizing}` + `html/body/#root` 整页不滚高度链)+ `body` 把令牌实例化成文档默认外观(底色/墨色/字体/行高)。**第一性原理**:这是「把令牌词表落到文档根」,与 `tokens.css` 的 `:root{--ze-*}` 定义令牌是同一桩 foundation 事的两半 —— 既非可复用组件(L2 原子)、也非组合(L3/4/5),而是地基(一切 .ze-* 与上层都继承自 body 基线)。故归 L1;门禁本就不扫 L1。在 `tokens.css` 后 import(`webapp/src/main.tsx` + `webapp/src/gallery/main.tsx`)。
- **DESIGN-SYSTEM.md §2 L1 段同步**:L1 = 令牌定义(tokens.css)+ 文档基线(base.css);铁律精确化为「**foundation 之上**只有 L2 直接消费令牌」(L1 自身 base.css 把令牌落到 body 是 foundation 内部实例化、不计)。
- **删 class(MAJOR)**:`.ze-app__welcome` / `.ze-app__welcome-title` / `.ze-app__welcome-text`(+ 后代 `code`)。全仓零 JSX 消费 = 死码:App.tsx 的无项目态渲染 `<ProjectPicker />`(03-authentication),welcome 卡是 ProjectPicker 之前的遗留物,早被取代。
- **app-shell.css(L4)**:只剩 `.ze-app` / `.ze-app__main`(纯结构容器,flex/height/overflow,零令牌);从 `css_layer_guard` 的 `TOKEN_ABOVE_L2_EXEMPT` 移除(门禁现强制零令牌,豁免剩 auth/header/footer 3 个)。
- 删 class 按规则 MAJOR。core.lock checksum 变(DESIGN-SYSTEM.md/01-tokens/webapp 入口在 core 校验范围)、保留;i18n/ops/toolchain 仅时间戳。

### 14.0.4 (2026-06-09) — L5 chrome 轮 batch1:layout(LayoutTree)去令牌(迁址,无词表变更,PATCH)
**L5 chrome 去令牌轮开始** —— layout.css(L4)`var(--ze-*)` 清零。
- **LayoutTree 布局 primitive 上移 L2**(新建 `02-atoms/layout-tree.css`):`.ze-layout-root`(中区根)/ `.ze-split`/`__pane`/`__divider`(二叉分屏 + 可拖分割条)/ `.ze-slot`/`__chrome`/`__empty`(EditorSlot 容器)。它是框架级布局 primitive(LayoutRenderer 渲染,任何用本框架 LayoutTree 的应用都复用)。分割条 `.ze-split__divider` 状态化(常态 `overlay-soft` 底,`:hover`/`[data-dragging]` 切 `accent-500` + transition)、slot chrome 悬浮显隐(`:hover`/`:focus-within` 才显),这套状态化 + 后代/伪类选择器的令牌依赖无法降为 L4 无令牌结构,故整组收成 L2 组件原子(同 `.ze-spatial`/`.ze-table`)。tabs(`.ze-tabs`/`.ze-tab`)仍由 `tab.css` 供给,本文件不重复。**class 名全不变**(`ze-layout-root`/`ze-split`/`ze-slot`,无 `ze-editor__` 污染),仅定义文件 L4→L2。
- **L4 `layout.css` 清空为指针 stub**;并从 `css_layer_guard` 的 `TOKEN_ABOVE_L2_EXEMPT` 移除 —— 门禁现强制其零令牌(豁免集剩 app-shell/auth/header/footer 4 个)。`.ze-slot__chrome` 里的分屏/关闭按钮沿用 `.ze-button`(L5 用 L2 原子合法,无需另造 `.ze-pane__*`)。
- **无新增/删除/重命名 class**、无 token / data-* 语义变更 → **PATCH**。本批未触碰 01-core,4 锁无变化。

### 14.0.3 (2026-06-09) — Phase 4 batch13(收官):editor 外壳去令牌(迁址,无词表变更,PATCH)
**绝对严格分层落地最后一块 —— editor.css(L4)`var(--ze-*)` 清零;至此 L4 全 16 文件零令牌。**
- **编辑器共享外壳整组上移 L2**(新建 `02-atoms/editor-shell.css`):`.ze-editor` 根 + `__toolbar`/`__title`/`__count`/`__toolbar-actions`/`__search`/`__empty`/`__loading`/`__statusbar`/`__statusbar-counts`/`__statusbar-op`/`__statusbar-toggle`/`__statusbar-reveal`/`__canvas-body`/`__canvas-overlay`/`__canvas-skeleton`/`__canvas-hint`。它是全 15 编辑器共享的 shell chrome(EditorFrame 统一组装),令牌视觉(底色/墨色/字体/内距/分隔线/状态条)+ 后代/状态选择器(statusbar-reveal hover/active 等)无法降为 L4 无令牌结构,故整组收成 L2 组件原子(同 `.ze-table`/`.ze-thumb`/`.ze-chart`)。它是 ZE 里复用面最广的 shell 组件,L2 归属合理;`ze-editor` block 被 15 编辑器消费、dead_atom alive。**class 名沿用 `.ze-editor*`**(命名正确,不同于 form 那个误名的 `.ze-editor__field` 字段行,故不重命名),仅定义文件 L4→L2。
- **L4 `editor.css` 清空为指针 stub**(无单一编辑器类型专属画法残留 —— 各类型画法在 table/form/gallery/... 各自文件;不删文件;`03-editor/index.tsx` import 保留)。根 `.ze-editor` 的 `container-type` 仍为各类型文件的 `@container` 提供查询上下文(DOM 结构不变)。
- **无新增/删除/重命名 class**、无 token / data-* 语义变更 → **PATCH**。
- 门禁:`TOKEN_ABOVE_L2_FILES` 追加 editor.css(全 16 个 L4 文件均已登记零令牌)。
- ⚠️ **踩坑**:stub 注释里写 `__statusbar*/__canvas-*` 的 `*/` 被 PostCSS 当成注释提前闭合 → Vite 500。CSS 门禁(Python 正则)不跑 PostCSS、未拦住,靠浏览器暴露。教训:CSS 注释正文禁出现 `*/` 字面。

### 14.0.2 (2026-06-09) — Phase 4 batch12:timeline 去令牌(迁址 + 补 gallery 实例,无词表变更,PATCH)
**绝对严格分层推进到 timeline 编辑器**:timeline.css(L4)`var(--ze-*)` 清零。
- **时间轴可视化整组上移 L2**(新建 `02-atoms/timeline.css`):`.ze-timeline__scroll`/`__ruler`/`__tick`/`__lanes`/`__lane`/`__clip`/`__clip-label`/`__clip-handle`/`__playhead`。TimelineZone(框架级内容区 primitive)的时间轴是内聚状态化组件 —— 轨道行 `.ze-timeline__lane` 走行高系统 `min-height=row-h`;片段 `.ze-timeline__clip` 在轨内绝对定位且 选中(`[data-selected]` info 描边)/ 运行态(`[data-run-phase]` 淡出/红边)分支;clip 底色由片段 data 经内联 `--clip-color`(非系统令牌的局部变量)注入 + 系统令牌回退 `var(--clip-color, var(--ze-overlay-accent))`。这套「row-h 行模型 + 内联 var 回退底色 + 状态分支 + 标尺/手柄/播放头专属令牌」无法用通用 utility 原子表达(`.ze-surface` 接不了 `--clip-color` 回退),故整组收成 L2 组件原子(同 `.ze-table`/`.ze-chart`/`.ze-thumb`)。**class 名全不变**(全 `ze-timeline` 前缀,无 `ze-editor__` 污染),仅定义文件 L4→L2。
- **L4 `timeline.css` 清空为指针 stub**(timeline 全是该可视化组件、无 `.ze-editor__*` 残留;不删文件;`03-editor/index.tsx` import 保留)。EDITOR_MANIFEST timeline `rowSelectors`=`.ze-timeline__lane` 不变(`editor_manifest_check` 扫 02-atoms 认 row-h)。
- **补 gallery l4-timeline 实例**:`EditorsShowcase.tsx`(TimelineShowcase + Section,接 `fixtures/timeline.ts` 的 TIMELINE_ENTITIES/VIEW)+ `sections.ts`(l4-timeline 锚点)。此前 timeline 无 gallery 编辑器实例、无法浏览器核验;补齐后照常 token-probe 像素核验(lane row-h / clip 内联底色 / 选中 info 描边 / handle / playhead / ruler 全匹配,零 console error)。
- **无新增/删除/重命名 class**、无 token / data-* 语义变更 → **PATCH**。
- 门禁:`TOKEN_ABOVE_L2_FILES` 追加 timeline.css。

### 14.0.1 (2026-06-09) — Phase 4 batch11:spatial 去令牌(迁址,无词表变更,PATCH)
**绝对严格分层推进到 spatial 共享基元**:spatial.css(L4)`var(--ze-*)` 清零。
- **空间编辑共享 primitive 整组上移 L2**(新建 `02-atoms/spatial.css`):`.ze-canvas*`(CanvasViewport)/ `.ze-spatial*`(SpatialCanvas)/ `.ze-gizmo*`(TransformGizmo)/ `.ze-mask*`(MaskCanvas)是框架级共享 primitive(canvas2d / asset / image-cleanup 多编辑器复用,在 `primitives/`)。其视觉核心是「多态 SVG stroke」—— `.ze-spatial__box` 选中切 `accent-500`、`[data-status=error]` 切 `danger`、`running/pending` 切 `info` 虚线;`.ze-gizmo__frame/__arm/__handle` 描边;`.ze-spatial__marquee` 框选;`.ze-spatial__checkerboard` 棋盘格多档令牌渐变;HUD。SVG 的 fill/stroke + 状态分支非通用 utility 原子(`.ze-surface`/`.ze-text` 管 background/color)能贴,故整组 4 primitive 收成 L2 组件原子(同 `.ze-table`/`.ze-thumb`/`.ze-chart`)。**class 名全不变**(全 `ze-canvas/spatial/gizmo/mask` 前缀,无 `ze-editor__` 污染),仅定义文件 L4→L2。
- **L4 `spatial.css` 清空为指针 stub**:spatial 全是共享 primitive、无 `.ze-editor__*` 编辑器残留,故 L4 去掉所有规则、留头注释指向 L2。**不删文件**(尊重 no-delete);`03-editor/index.tsx` 的 spatial.css import 保留(导入空 stub 无副作用)。
- 消费方:CanvasViewport/TransformGizmo/MaskCanvas 仅改 doc 注释指向(无 className 改动,整组迁移类名不变)。
- **无新增/删除/重命名 class**、无 token / data-* 语义变更 → **PATCH**。
- 门禁:`TOKEN_ABOVE_L2_FILES` 追加 spatial.css(守 stub 零令牌不回退)。

### 14.0.0 (2026-06-09) — Phase 4 batch10:form 去令牌(含删/重命名 class,MAJOR)
**绝对严格分层推进到 form 编辑器**:form.css(L4)`var(--ze-*)` 清零。
- **表单字段行 primitive 上移 L2 并改名**(新建 `02-atoms/field-row.css`):字段行(FormZone schema 驱动表单 + ParamPanel 参数面板复用)承载行高系统(SPEC §4)—— 单行 `min-height=row-h`、控件垂直居中不撑行;多行例外 `[data-multiline]` → 高度 `calc(row-h*4)`、label 顶对齐;run-phase 骨架等高占位;`@container` 响应式单列;grid 列宽 `var(--ze-field-label-w)`。这套「row-h 行模型 + label-w 列宽 + calc(row-h*n) 多行高 + 状态分支」的令牌依赖,非通用 utility 原子(`.ze-grid`/`.ze-gap` 无 label-w 列 / row-h 行档)能表达,故收成 L2 组件原子(同 `.ze-table`/`.ze-thumb`)。
- **重命名 class(MAJOR)**:`.ze-editor__field` → `.ze-field-row`、`.ze-editor__field-label` → `.ze-field-row__label`、`.ze-editor__field-color` → `.ze-field-row__color`(去 `ze-editor__` 前缀,避免污染 02-atoms 的 atom 命名 / dead_atom 的 `ze-editor` block 追踪)。消费方 FormZone / ParamPanel / ZonesShowcase(form demo)同步改名。
- **EDITOR_MANIFEST**:form `rowSelectors` 由 `.ze-editor__field` 改指 `.ze-field-row`(行高系统;`editor_manifest_check` 扫 02-atoms 认 row-h)。
- **form.css(L4)去令牌**:只剩 `.ze-editor__form-body`=`.ze-pad[p=3]`+`.ze-gap[gap=1]`(留 `flex; flex-direction:column; min-height:0; overflow:auto` + `[data-run-phase=error]{place-content:center}`)。
- 门禁:`TOKEN_ABOVE_L2_FILES` 追加 form.css。删/改 class 按规则 MAJOR。

### 13.0.2 (2026-06-09) — Phase 4 batch9:chart 去令牌(迁址 + 去令牌,无词表变更,PATCH)
**绝对严格分层推进到 chart 编辑器**:chart.css(L4)`var(--ze-*)` 清零。
- **SVG 图表可视化 primitive 上移 L2**(新建 `02-atoms/chart.css`):ChartZone(框架级内容区组件)的 plot 子树 —— `.ze-chart__frame`/`__plot`/`__svg` + 绘制图元 `.ze-chart__axis`/`__bar`/`__line`/`__area`/`__dot`/`__slice` + 悬停 overlay `.ze-chart__crosshair`/`__tooltip`/`__tooltip-label`/`__tooltip-value`。SVG 图元视觉是「多态 + 多系列 fill/stroke」(hover/selected 切 accent-700、饼切片按 `data-i` 循环 6 档语义色),非通用 utility 原子(`.ze-surface`/`.ze-text` 管 background/color 而非 SVG fill/stroke)能贴;tooltip 的 `transform: calc(-100% - space-2)` 含令牌、crosshair 的 `color-mix(ink 28%)` 亦非通用原子可表达 —— 故整组收成 L2 组件原子(同 `.ze-table`/`.ze-thumb` 模式)。`EntityLoadingFrame` 只转发 `className`(不转发 data-*),故 `.ze-chart__frame` 的 padding 无法贴 `.ze-pad`,连同 plot 子树一并入 L2。**class 名不变,仅定义文件 L4→L2**。
- **chart.css(L4)去令牌**:只剩 KPI strip —— `.ze-chart__kpis`=`.ze-gap[3]`+`.ze-pad[py=2][px=3]`+`.ze-edge[bottom]`(留 `display:flex; flex-wrap:wrap`;内含的 `.ze-status-tile` 早是 L2 组件)。
- **无新增/删除 class**:chart 图元同名迁文件,KPI strip 复用既有 L2 原子。无 token / data-* 语义变更 → **PATCH**。
- 门禁:`TOKEN_ABOVE_L2_FILES` 追加 chart.css。

### 13.0.1 (2026-06-09) — Phase 4 batch8:gallery 去令牌(迁址 + 去令牌,无词表变更,PATCH)
**绝对严格分层推进到 gallery 编辑器**:gallery.css(L4)`var(--ze-*)` 清零。
- **缩略图网格 primitive 上移 L2**(新建 `02-atoms/thumb.css`):`.ze-thumb*` / `.ze-thumb-grid` / `.ze-marquee` 是框架级共享组件(ThumbnailGrid / ThumbnailCard,被 gallery / asset / task 三编辑器复用)。卡片视觉天然状态化 —— 选中(`[data-selected]`)/ 多选(`[data-multi-selected]` inset 环)/ 错误(`[data-status=error]`)/ 拖拽(`[data-dragging]` 半透明)/ 落点插入线(`[data-drag-over]`),全靠 data-* 后代选择器在同一张卡上分支(同 `.ze-card` / `.ze-table` 的组件原子模式),无法降为 L4 无令牌结构;故令牌样式整体收成 L2 组件原子,消费合法收敛 L2。**class 名不变,仅定义文件 L4→L2**(词表零变更)。
- **gallery.css(L4)去令牌**:只剩 GalleryEditor 自有结构 —— `.ze-editor__gallery-body`=`.ze-pad[p=3]`(留 flex/overflow);对比双视口 `.ze-editor__compare`=`.ze-gap[2]`+`.ze-pad[2]`(grid-template-columns / @container 留字面 px)、`.ze-editor__compare-pane`=`.ze-edge[all]`+`.ze-round[md]`、`.ze-editor__compare-label`=`.ze-pad[py=1][px=2]`+`.ze-text[role=secondary][size=xs]`+`.ze-surface[raised]`+`.ze-edge[bottom]`(留 ellipsis)。
- **无新增/删除 class**:thumb/marquee 同名迁文件,compare 视口复用既有 L2 原子。无 token / data-* 语义变更 → **PATCH**。
- 门禁:`TOKEN_ABOVE_L2_FILES` 追加 gallery.css。

### 13.0.0 (2026-06-09) — Phase 4 batch7:compare 去令牌(含删 class,MAJOR)
**绝对严格分层推进到 compare 对比图元(CompareFrame primitive)**:CSS `var(--ze-*)` 清零。
- **新增 L2 原子**:
  - `.ze-surface[data-surface=strong]`:`overlay-strong` 浮层标签底(对比图元角标 tag)。
  - `.ze-size[data-w=6]` / `[data-h=6]`:把空间令牌当宽/高(滑块手柄 24×24 定尺方块)。
  - `.ze-inset[data-top/left/right=2]`:绝对定位偏移(只补 top/left/right 偏移、不设 position,由消费元素自带 position:absolute)—— 浮层角标/HUD/贴角元素用。
- **compare.css 去令牌**:三态(并排/滑块/叠加)全去令牌。toolbar=`.ze-row`+`.ze-pad[p=2]`+`.ze-edge[edge=bottom]`;pair=`.ze-grid[cols=2][gap=2]`+`.ze-pad[p=2]`;cell=`.ze-stack[gap=1][align=center]`;img/stage 底=`.ze-surface[sunken]`;caption/opacity-label=`.ze-text[role=secondary][size=xs]`。**描边强调色走 currentColor**:divider 贴 `.ze-text[data-tone=accent]` 给色,其 `border-left:2px solid currentColor` 与子 handle 的 `border:2px solid currentColor`(继承 divider 色)同步取 accent —— L4 本层不写 accent 令牌。handle=`.ze-size[w/h=6]`+`.ze-round[pill]`+`.ze-surface[raised]`+`.ze-elevate`;tag=`.ze-inset`+`.ze-pad[py=0-5][px=2]`+`.ze-text[size=xs][tone=inverse]`+`.ze-surface[strong]`+`.ze-round[sm]`。本层只留定位/层叠/overflow/flex 骨架。
- **删 class(MAJOR)**:`.ze-compare__cap`(纯令牌字号+色 → `.ze-text[role=secondary][size=xs]`)、`.ze-compare__tag--before`/`--after`(纯令牌左右偏移 → `.ze-inset[data-left=2]`/`[data-right=2]`)。
- 门禁:`TOKEN_ABOVE_L2_FILES` 追加 compare.css。删 class 按规则 MAJOR。

### 12.0.0 (2026-06-09) — Phase 4 batch6:task 去令牌(含删 class,MAJOR)
**绝对严格分层推进到 task 编辑器**:CSS `var(--ze-*)` 清零。
- **新增 L2 原子**:
  - `.ze-gap[data-gap]`:只补 gap、不动 display(给已自带 display 的容器贴;区别于 .ze-row/.ze-stack 连 display 一起给)。
  - `.ze-size[data-minh=8][data-maxw=16]`:把空间令牌当尺寸(任务行最小高、阶段标签最大宽)。
  - `.ze-row-card`:带边框的可选中/可悬停行卡 —— 边框 + :hover 底色 + `[data-selected=true]` 强调色边 + `[data-status=error]` 危险色边;底色由叠加的 .ze-surface 给。
- **task.css 去令牌**:行=`.ze-gap`+`.ze-pad[py=2][px=3]`+`.ze-round[md]`+`.ze-surface[raised]`+`.ze-size[minh=8]`+`.ze-row-card`;名字=`.ze-text[data-weight=medium]`;阶段 pill=`.ze-round[pill]`+`.ze-surface[sunken]`+`.ze-pad[px=2]`+`.ze-gap`+`.ze-text[data-role=secondary][data-size=xs][data-tone=success/danger/accent]`;本层只留 flex 骨架/省略/行高 1。
- **删 class(MAJOR)**:`.ze-task-row__stage`(纯令牌单行小字 → `.ze-text[data-role=tertiary]`)。
- 门禁:`TOKEN_ABOVE_L2_FILES` 追加 task.css。删 class 按规则 MAJOR。

### 11.0.0 (2026-06-09) — Phase 4 batch5:table 去令牌 + .ze-table 组件原子(含删 class,MAJOR)
**绝对严格分层推进到 table 编辑器**:CSS `var(--ze-*)` 清零。
- **新增 L2 组件原子 `.ze-table`**(02-atoms/table.css):表格样式天然靠 th/td/tr+:hover+后代选择器(每格贴 className 不现实),故整张表收成一个组件原子(同 `.ze-card`/`.ze-badge`)。容器贴 `.ze-table`,内部列头/单元格/选中(`tr.is-selected`/`tr[data-multi-selected]`)/run-phase(pending-running 降明度、error 淡红底)由本原子经后代选择器统一给。令牌消费收敛 L2。
- **table.css(L4)去令牌**:table 元素=`.ze-editor__tbl`+`.ze-table`;本层只留 `.ze-editor__tbl-scroll`(滚动)、`.ze-editor__row-actions`(对齐)、`.ze-editor__tbl th/td max-width` 响应式(字面 px,容器查询)。
- **删 class(MAJOR)**:`.ze-editor__th-sort`(排序箭头颜色 → `.ze-text[data-tone=accent]`)。
- **行高系统**:table `rowSelectors` 从 `.ze-editor__tbl th/td` 改指 `.ze-table th/td`(EDITOR_MANIFEST.json);`editor_manifest_check.py` 的 row-h 扫描范围纳入 `02-atoms`(P4 后 row-h 可落在 L2 组件原子上)。e2e 排序断言选择器同步改 `.ze-table th .ze-text[data-tone=accent]`。
- 门禁:`TOKEN_ABOVE_L2_FILES` 追加 table.css。删 class 按规则 MAJOR。

### 10.1.0 (2026-06-09) — Phase 4 batch4:graph 去令牌 + 带状态/画布原子(纯加性,MINOR)
**绝对严格分层推进到 graph 编辑器**:CSS `var(--ze-*)` 清零。新增 4 个 L2 原子(将被 timeline/table/gallery/compare 复用):
- **`.ze-elevate`**:阴影 —— base=浮动弹层/拖拽投影(shadow-floating);`data-elev=sm`=节点/卡片细微 `0 1px 2px overlay-soft`。
- **`.ze-dotgrid`**:点阵画布底(surface-canvas + radial-gradient 点,bg-size=space-6);空间编辑器网格背景。
- **`.ze-selectable[data-selected=true]`**:选中描边(semantic-info,outline-offset -1px);节点/片段/卡片通用,data-selected 驱动不 dispatch。
- **`.ze-runstate[data-run-phase]`**:实体运行态视觉 —— pending/running 淡出 opacity .55,error 描红 semantic-danger;可贴任意可运行元素。
- **graph.css 去令牌**:画布=`.ze-dotgrid`;节点=`.ze-pad[data-px=2]`+`.ze-edge[data-edge=all][data-tone=strong]`+`.ze-round[data-radius=md]`+`.ze-surface[data-surface=raised]`+`.ze-text`+`.ze-elevate[data-elev=sm]`+`.ze-selectable`+`.ze-runstate`;连边 `stroke:currentColor`(色由 svg 贴 `.ze-text[data-role=tertiary]` 提供 ink-tertiary)。本层只留定位/flex 居中/抓手光标/省略/线宽结构。
- 无删/改 class,纯加性 MINOR。门禁 `TOKEN_ABOVE_L2_FILES` 追加 graph.css。

### 10.0.0 (2026-06-08) — Phase 4 batch3:text 去令牌(含删 class,MAJOR)
**绝对严格分层推进到 text 编辑器**:CSS `var(--ze-*)` 清零。
- **加性 L2 `text.css`**:`.ze-text[data-prose]` —— 富文本/正文排版原子,经后代选择器给 `p`(段距)/`code`(等宽+底色+倒角)样式。专为 markdown 预览这类 **dangerouslySetInnerHTML 注入、贴不上 className 的动态 HTML** 设计:容器贴 `.ze-text[data-prose]`,子节点排版由 L2 统一接管。
- **text.css 去令牌**:body 竖排+内距=`.ze-stack`+`.ze-pad[data-p=3]`(TextZone),预览=`.ze-text[data-prose]`,textarea 走 `.ze-textarea`;本层只留 flex/滚动/不可缩放结构。
- **删 class(MAJOR)**:`.ze-text__count`(P3 改用 ToolbarZone count 后无编辑器消费,仅 gallery ZonesShowcase demo 引用 → demo 同步改 `.ze-text[data-role=tertiary]`)。删 class 按规则 MAJOR。
- 门禁:`TOKEN_ABOVE_L2_FILES` 追加 text.css。

### 9.0.0 (2026-06-08) — Phase 4 batch2:code/review 去令牌(含删 class,MAJOR)
**绝对严格分层推进到 code/review 编辑器**:CSS `var(--ze-*)` 清零,视觉改贴 L2 原子。
- **加性 L2 `text.css`**:`.ze-text[data-size=xs/sm]`(只改字号、不动色,给"主色但小一档"如代码正文)+ `[data-leading=normal]`(行高;代码 gutter 与正文对齐需统一)。
- **code.css 去令牌**:gutter/textarea 等宽+字号+行高=`.ze-text[data-mono][data-size=xs][data-leading=normal]`,底色=`.ze-surface[data-surface=sunken/raised]`,行号槽右边框=`.ze-edge[data-edge=right]`,内距=`.ze-pad`;本层只留 flex/右对齐/预格式/tab/焦点环结构。
- **review.css 去令牌**:body 竖排+内距=`.ze-stack[data-gap=3][data-pad=3]`,动作行=`.ze-row`,批注标签=`.ze-text[data-role=secondary]`,状态徽章=`.ze-badge[data-variant=success/danger][data-emphasis=solid]`(approved/rejected)/默认中性(pending);本层只留填充/居中/不可缩放结构。
- **删 class(MAJOR)**:`.ze-code__count`(P3 改用 ToolbarZone count 后无 JSX 消费的孤儿)、`.ze-review__status`(→ `.ze-badge`)、`.ze-review__actions`(→ `.ze-row`)、`.ze-review__note-label`(→ `.ze-text`)。视觉能力全部由 L2 原子承接、无损失;删 class 按规则 MAJOR。
- 门禁:`TOKEN_ABOVE_L2_FILES` 追加 code.css/review.css(守零令牌不回退)。

### 8.1.0 (2026-06-08) — Phase 4 起:L4 编辑器去令牌地基 + 视觉实用原子族(纯加性,MINOR)
**绝对严格分层(DESIGN-SYSTEM §0)推进到 L4 编辑器(04-editors)**:逐文件把 CSS `var(--ze-*)` 清零,排版/视觉改贴 L2 原子,本层只留无令牌结构。本版先落地子集 + 地基。
- **加性 L2 视觉实用原子族 `02-atoms/utility.css`**(可贴任意元素,补 layout/text 原子):
  - `.ze-edge[data-edge=top/bottom/left/right/all]`(overlay-soft 分隔边框;`data-tone=strong` 加重)。
  - `.ze-round[data-radius=sm/md/lg/pill]`(倒角)。
  - `.ze-surface[data-surface=raised/sunken/canvas/inverse/accent-wash/overlay]`(任意元素底色;补 layout 原子的 data-surface,多 inverse/accent-wash/overlay 三档)。
  - `.ze-pad[data-p/px/py=…]`(内距,值=空间令牌档;layout 原子 data-pad 的通用版)。
- **加性 `text.css`**:`.ze-text[data-mono]`(等宽机器文本)+ `[data-tone=accent/danger/info/success/warn/inverse]`(语义/强调前景色)+ `[data-weight=bold]`。
- **首批转换文件(CSS var 清零)**:`media.css`(传输条=`.ze-bar[data-surface=raised][data-edge=bottom]`、时码=`.ze-text[data-mono]`、视口=`.ze-surface[data-surface=inverse]`、标记段=`.ze-surface[accent-wash]`+`.ze-round[pill]`)、`asset.css`(文件信息块=`.ze-stack`+`.ze-pad`)、`scene3d.css`(本就零令牌,改注释)。
- **门禁**:`css_layer_guard` 新增 `TOKEN_ABOVE_L2_FILES` 逐文件棘轮 —— 已转换的 04 文件守零令牌不回退;chrome(footer/header/auth/app-shell/layout)+ 其余编辑器文件留后续批次,整个 04 去净后并入 `TOKEN_ABOVE_L2_DIRS`。
- 纯加性:无 `--ze-*` token / `.ze-*` class 重命名或删除(`.ze-media__controls` 仅由"全令牌"瘦成"零令牌 flex-shrink 结构",class 仍在)。

### 8.0.0 (2026-06-08) — Phase 2b:L3 功能区去令牌(绝对严格分层,含删除/删文件,MAJOR)
**绝对严格分层(DESIGN-SYSTEM §0)落地到真实 CSS**:L3 功能区(03-zones)的 CSS `var(--ze-*)` 清零 —— 排版走 L2 布局原子、视觉走 L2 视觉/实用原子,本层只留无令牌的结构规则。
- **删文件 + 子类(视觉被 L2 完全承接)**:
  - `toolbar.css`(`.ze-canvas-toolbar` / `__tool` / `__sep` / `__icon` / `__label`)→ 条=`.ze-bar[data-surface=raised][data-edge=top]`、工具=`.ze-button[data-variant=ghost]`(`data-active` toggle)、分隔=`.ze-divider[data-orientation=vertical]`、撤销/重做=`.ze-icon-button`。
  - `context-menu.css`(`.ze-context-menu` / `__label`)→ `.ze-popover` + `.ze-menu-item` + `.ze-text[data-role=tertiary]`。
- **瘦身为零令牌结构(保留文件,删冗余视觉子类)**:
  - `outliner.css`:留 `.ze-outliner`(overflow)/`__caret`(rotate)/`__label`(省略)+ @container;节点改 `.ze-menu-item`(hover+`data-active` 选中),缩进由 React 行内 paddingLeft 给。
  - `editor-footer.css`:留 `__segment`(flex grow)+ @container;容器=`.ze-bar[data-edge=bottom]`、小字=`.ze-text[data-role=tertiary]`。
  - `inspector.css`:留 `.ze-param`(overflow);段标题=`.ze-bar`+`.ze-text[role=title]`、折叠头=`.ze-button[ghost]`、参数栈=`.ze-stack[gap][pad]`、滑杆行=`.ze-row`、读数=`.ze-text[role=tertiary]`、删 `__section-title/__section-action/__collapsible*/__caret/__slider-*`。
- **加性 L2**:`.ze-button[data-active]`(toggle,补全 button/icon-button/menu-item toggle 家族);布局原子 `data-pad`(容器内距,值=空间令牌档)。
- **新增门禁**:`css_layer_guard.py` 规则 7 `LAYER_TOKEN_ABOVE_L2` —— 03-zones 的 .css 禁 `var(--ze-*)`(Phase 4 扩到 04-editors)。
- 消费方改造:`CanvasToolbar`/`InspectorSection`/`ParamPanel`/`Slider` primitive 改贴 L2 原子类;gallery `ZonesShowcase` 三个 demo(inspector/context-menu/outliner)重组合;移除 `toolbar.css`/`context-menu.css` 的 import。
- 删 class/文件按规则 MAJOR。下游若曾贴这些 zone 子类需改贴 L2 原子组合(本仓已全改;外发工具未用这些内部 zone 类)。

### 7.2.0 (2026-06-08) — L2 实用原子扩充:data-surface + .ze-text(加性,MINOR)
- 布局原子(layout.css)加共享参数 `data-surface`(`raised`/`sunken`/`canvas` 容器底色),适用 stack/row/bar/grid。
- 新增 `02-atoms/text.css`:`.ze-text[data-role="secondary|tertiary|title"]` + `data-weight`(medium/semibold)/ `data-truncate`。角色文字(次要标签 / 元数据小字 / 段标题)封成 L2 原子。
- **为什么**:Phase 2 L3 去令牌(绝对严格分层)后,zone 的"容器底色 + 次要/三级小字 + 分组标题"在 L2 没有现成承接 → 自写 `var(背景/字号/墨色)` 会违例。这两个实用原子补齐 L2,让 L3 变纯组合(Option A,用户拍板)。
- 纯新增 class/attr,无删除/重命名 → 下游 compat 自动复制,mod 零改动。

### 7.1.0 (2026-06-08) — 新增 L2 布局原子族 layout.css(加性,MINOR)
- 新增 `02-atoms/layout.css`:`.ze-stack`(竖排)/`.ze-row`(横排)/`.ze-cluster`(成组换行)/`.ze-grid`(等宽网格)/`.ze-bar`(顶/底功能条)/`.ze-spacer`(弹性占位)/`.ze-fill`(撑满+可滚)。
- 参数走 `data-*`:`data-gap`(值=空间令牌档 key:0-5/1/2/3/4/6/8)、`data-align`、`data-justify`、`data-cols`(2/3/4/auto)、`data-wrap`、`data-edge`(top/bottom)。
- **为什么**:绝对严格分层(DESIGN-SYSTEM §0 铁律)后,L3 功能区 / L4 编辑器的 CSS 不准再直接用 `var(--ze-space-*)` 排版(那是 L1 令牌)。布局原子把排版动作封成 L2 积木,令牌消费收敛到本层。这是 Phase 2/4「去令牌」的地基。
- 纯新增 class,无删除/重命名 → 下游 compat 自动复制,mod 零改动。

### 7.0.0 (2026-06-08) — 删 .ze-editor__main/__aside:模型纠正 inspector=独立编辑器(含删除,MAJOR)
- **撤销 6.1.0**:删 `.ze-editor__main` / `.ze-editor__aside`。原因:把 inspector(属性栏)做成编辑器内嵌 aside 是错的模型。
- **正确模型**:编辑器自包含(操作区 + 内容区);**inspector = 独立的 form 编辑器**,在 **L5 布局组合**里与主编辑器并排,经 `selection[entity_type]` 全局选择联动(框架已支持,见 `02-mod/04-layout.config.ts` canvas2d+form 并排真实例)。
- 配套:删 `InspectorZone.tsx` + EditorFrame inspector 槽 + Canvas2dEditor inspector 用法;gallery 新增 L5「组合」层用 LayoutRenderer 正确演示(画布-图层-属性 三独立编辑器)。
- 下游若曾用 `.ze-editor__main/__aside`(本仓仅本会话引入、未外发)需移除。

### 6.1.0 (2026-06-08) — 编辑器 inspector 属性区(加性,MINOR)[已被 7.0.0 撤销]
- 新增 `.ze-editor__main`(内容 + 右侧属性栏横向并排)+ `.ze-editor__aside`(属性栏:宽 `--ze-size-panel-w`、左边框、可滚)。
- EditorFrame 仅在传 `inspector` 时启用这层 —— 让编辑器**看得见地**由 操作区(toolbar)+ 内容区(body)+ 属性区(inspector)拼成(Phase B-1:canvas2d spatial 首用,选中 bbox → 右侧属性跟随)。
- 纯新增 class,无删除/重命名 → 下游 compat 自动复制。

### 6.0.0 (2026-06-08) — 功能区模型重定义 + 删 palette/floating-panel(含删除,MAJOR)
- **概念重定义**:功能区(zone)从"编辑器周边 chrome 家具"改为 **编辑器内部按职责的功能分区**(操作区 + 内容区);编辑器 = 声明式组合功能区。03-zones/SPEC.md 重写(§2 词表 / §3 每编辑器组成 / §4 诚实现状)。
- **删 class**:`palette.css`、`floating-panel.css`(无任何编辑器需要、不在新词表)→ 删 `.ze-palette*` / `.ze-floating-panel*`。gallery `main.tsx` 去掉两者 import、`ZonesShowcase` 重做。
- **保留**:`toolbar`(canvas-toolbar)/`inspector` 已 wired;`context-menu`/`editor-footer`(statusbar)/`outliner` CSS 留存。
- **诚实标注**:内容区(list/grid/canvas/preview/timeline/text/form/chart)当前仍硬编码在 L4 编辑器 body,未抽成功能区组件 —— 抽象是后续 Phase B 大重构,本次只钉模型 + 删空占位。

### 5.0.0 (2026-06-08) — 层级精简 Tier2:删 1px/角色塌缩档 3 个(含删除,MAJOR)
| 删 | 去向 | 用量 | 理由 |
|---|---|---|---|
| `--ze-fs-2xs`(10px) | `fs-xs`(11px) | 15 | 与 xs 仅差 1px,过度细分 |
| `--ze-fs-md`(14px) | `fs-sm`(13px) | 11 | 与 sm 仅差 1px;字号 7→5 档,sm 作唯一默认(控件+正文) |
| `--ze-space-1-5`(6px) | `space-2`(8px) | 21 | "紧凑控件内边距"与 space-2"控件内边距"角色重叠 |
- 视觉位移 ≤2px(微 chrome +1 / 正文 −1 / 紧凑内边距 +2),浏览器全页 + 编辑器核验无破版。
- 字号档现 5 个(xs/sm/lg/xl/2xl);space 现 9 个。SPEC §2.2 + tokens.css 注释已同步。

> **⚠️ 后续更正(2026-07-26 补记)—— 本条对 `--ze-fs-2xs` 的结论已被推翻,别照它清理用法。**
>
> `--ze-fs-2xs` 在 **2026-06-23 的 `377826aa7`(Tighten build v3 css visual contracts)重新引入**,
> 值改为 **9px**(不是本条删除时的 10px),用途收窄为「微徽标/极小计数;只能用于
> badge/count/arrow 这类辅助信息」。同一提交把 SPEC 正文从「字号已精简为 5 档」改成
> 「以 `fs-2xs/xs/sm/lg/xl/2xl` **六档**收敛」,并把字号门禁由 `HARDCODED_FONT_SIZE`
> 改名为 `TYPOGRAPHY_FONT_SIZE_LITERAL` —— **唯独本变更日志当时没补条目**,于是它最新的一句
> 关于 2xs 的话一直停在「已删」。
>
> 为什么要紧:`scripts/SYNC-RUNBOOK.md` §2 让跨大版本的目标**靠 grep 本文件挑破坏面**
> (2026-07-26 实测 94 个 v3 面里 78 个走这条路)。照 5.0.0 这张表读,迁移者会判定
> `--ze-fs-2xs` 已删而去清理它的用法 —— 而它今天是上游 8 处引用的正规 token。
>
> 同表另两项经复核**确已删除**、状态准确:`--ze-fs-md`(0 定义 0 引用)、
> `--ze-space-1-5`(0 定义 0 引用)。此处只更正 2xs 一项。
>
> 不追补版本条目:重新引入发生在 5.0.0 与今天之间,确切该挂哪个版本号无从可靠还原,
> 编一个比留空更坏。此注只陈述 git 可复核的事实。

### 4.0.0 (2026-06-08) — 令牌层级整体精简 Tier1:删角色塌缩/别名/0引用 7 档(含删除,MAJOR)
全族用量实测后,从设计角度删「角色与邻档塌缩 / 纯别名 / 0 引用」的档(消费方先改去向,无能力损失):

| 删 | 去向 | 用量 | 理由 |
|---|---|---|---|
| `--ze-surface-chrome` | `surface-raised`(消费方本就 fallback 到它) | 3 | 纯别名 |
| `--ze-accent-50` / `-100` / `-900` | 删(无消费) | 0 | 0 引用;accent 改为只留 200/500/700 实际用档 |
| `--ze-shadow-elev-2` | `shadow-floating` | 3 | 与 floating 同为"浮层深投影",角色重叠 |
| `--ze-space-9`(36px) | `space-8`(32px) | 1 | layout-only 四档塌缩,≈32 |
| `--ze-dur-normal`(200ms) | `dur-fast`(120ms) | 2 | 夹 fast/slow 间最弱 |

- **accent 立场变更**:此前 SPEC「连续色阶,按需取档」保留 50/100/900 spare 阶;本次按「必要最少层级」改为**只留实际在用的 200/500/700**(色阶仍由 `--ze-accent-h` 种子参数化,需要新档随时加)。01-tokens/SPEC.md §2.2/§2.7 + tokens.css 注释已同步。
- **诚实化注释**:`space-0-5` 去掉错误的"罕用"(实测 33 用)。
- **不动**:ink/semantic/meta/font/fw/radius/z/size 已是必要最小;`space-0-5`/`lh-snug`/`radius-none`/`fw-regular` 用量真实,保留。
- Tier2(fs 1px 对并、space 布局尾档/6px)视觉有位移,另行评估。

### 3.0.0 (2026-06-05) — 删别名令牌 `--ze-field-row-h`(含删除,MAJOR)
- **删除** `--ze-field-row-h`(原为 `var(--ze-row-h)` 的向后兼容别名)。唯一消费方 `04-editors/form.css` 的 `min-height` 已内联为 `var(--ze-row-h)`。
- **理由**:令牌层级冗余核验 —— 该别名是产品层唯一可清的真冗余(其余每档都有角色+消费;accent 50/100/900 是刻意保留的连续色阶,不删)。
- **影响**:仅别名、无能力损失(row-h 仍在,行高系统不变);但按契约规则「删任一令牌 = MAJOR」,故 2.x→3.0.0。下游默认皮肤若曾直引 `--ze-field-row-h` 需改 `--ze-row-h`(本仓已无残留引用;上方 2.0.0 历史条提及它属历史记录,不回改)。

### 容器适应契约明文化 + 门禁(2026-06-05) — 无 vocab 变更(非版本 bump)
- **明文契约**:`01-layout/SPEC.md` 新增 §2.10「容器适应契约」(flex 链 `min-0` / 滚动单一归属 / ellipsis 截断 / 悬浮 chrome),`03-editor/SPEC.md` §2.8 改写为「reflow vs fluid/fill 两族 + 逐编辑器判定表」,并把 `--ze-bp-*`(2.0.0 已删)的过期引用改为 canonical px 直写({320,480,768,1024,1440}±16,`@container` 不吃 token)。
- **门禁** 新增 `scripts/css_container_contract_check.py`(R1 flex+scroll 缺 `min-height:0` / R2 ellipsis+flex 缺 `min-width:0`),进 `verify_all`(第 4 门禁);同 `css_layer_guard.py` 为 build-v3 本地皮肤门禁、不进 toolchain 受管清单。
- **修复** `04-editors/form.css` `.ze-editor__form-body` 补 `min-height:0`(R1 实证命中的潜伏 flex 溢出 bug;兄弟 `task-body` 早已正确)。属既有 class 的属性修复,不改 token/class 词表 → 不 bump css 契约版本。
- **实测** gallery.html 三档(xs 320 / md 600 / xl 1280):16 个 `.ze-editor` 全 0 横向溢出、无页面溢出,table 在 xs 真退化单列 → 分类表经浏览器验证。

### 2.1.0 (2026-06-03) — 字号债清零:新增 `--ze-fs-2xs` 微档(加性,MINOR)
- **新增令牌** `--ze-fs-2xs: 10px`:密集 chrome 微档(header 环境角标 / footer 分组 overline / op-log source 角标 / 计数胶囊 / 折叠三角)。`xs`(11px)之下唯一微档,只服务 header/footer 这类 chrome,正文/控件最小到 `xs`。
- **header/footer 字面量字号全部迁令牌**:header `7px→fs-2xs`、`13px→fs-sm`、`11px→fs-xs`;footer `10px / 9px×3 / 8px→fs-2xs`、`16px→fs-lg`。视觉变化仅 7/8/9px 微档统一升到 10px(均内容自适应元素,≤3px,无溢出;原 7px 角标近不可读,升级即改善)。
- **门禁** `HARDCODED_FONT_SIZE` 的 `FONT_SIZE_DEBT` 豁免清空 —— 全皮肤字号现 100% 走令牌,无文件豁免。
- 纯加性:未删/未重命名任何 token / class;`fs-2xs` 即时 7 处消费(`header.css`×1 + `footer.css`×5 + `editor.css`×1,含本会话 statusbar collapse 的 reveal 把手),非死令牌。

### 2.0.0 (2026-06-03) — 设计系统重设计 Phase 1:规定式 + 行高系统 + 门禁(含删除,MAJOR)
- **令牌去重(删 0 引用档)**:`fs-3xl`、`lh-relaxed`、`space-0`、`z-base`、`dur-instant`、`easing-decelerate/accelerate`、`--ze-bp-xs..xl` 删除。`fs-2xl`/`lh-tight`/`accent` 全阶梯保留(有引用或属连续色阶)。
- **断点不再是令牌**:CSS `@container` 不能用 `var()`,`--ze-bp-*` 永远死;改为门禁 `BREAKPOINT_LITERAL` 钉死 px 常量集 {320,480,768,1024,1440}±16。
- **`space-60` → `--ze-size-panel-w`**(它是尺寸不是间距);消费方(editor 搜索框/outliner/context-menu/floating-panel)同步迁移。
- **新增行高系统令牌**:`--ze-row-h`(=space-7,28px,行/控件标准高)、`--ze-field-row-h` 改为其 alias。table.css 改为 row-h 行模型(模板)。
- **令牌/模块 SPEC 改规定式**:tokens 注释 = 角色;01-tokens SPEC 加 §3 情境指南 + §4 行高系统;02-atoms SPEC 加选择矩阵 + 消费状态 + 新增 `icons.md` 映射表。
- **门禁**:css_layer_guard 加规则 5 `HARDCODED_FONT_SIZE`(header/footer 历史债 FONT_SIZE_DEBT 暂免)、规则 6 `BREAKPOINT_LITERAL`;新增 `scripts/editor_manifest_check.py` + `03-css-rules/EDITOR_MANIFEST.json`(每编辑器声明功能区/交互/行模型/fixture/需求;table=adopted,其余 pending);登记进 ZE_BUILD_EVIDENCE gates。
- 迁移影响:删除的令牌均 0 引用,真实代码零破坏(css_contract_check 验证);space-60 重命名已迁所有消费方。

### 1.2.0 (2026-06-03) — 功能区/布局区重排(加性)
- **一编辑器类型一文件**:`editor.css` 拆出 `table.css` / `form.css`;`canvas.css` 拆成 `spatial.css`(`.ze-canvas*`/`.ze-spatial*`/`.ze-gizmo*` 共享基元,canvas2d & asset 复用)+ `scene3d.css` + `asset.css` + `media.css`;共享 `__canvas-body` 脚手架归入 `editor.css` 基座。`canvas.css` 移除(内容零丢失)。
- **chart 归位**:`chart.css` 从 `03-zones/` 移到 `04-editors/`(class 不变;chart 是编辑器类型,内容画法归布局区)。
- **补齐 5 个 zone**:`palette` / `outliner` / `editor-footer`(zone id `footer`)/ `context-menu` / `floating-panel` 落 CSS,状态 styled-unwired(暂无编辑器消费)。
- 纯加性:无 `--ze-*` token / `.ze-*` class 重命名或删除;`03-editor/index.tsx` 的 import 清单同步更新,各 editor className 未变。

### 1.0.0 (2026-05-30) — 基线
- 01-tokens tokens(color/ink/accent/semantic/space/radius/shadow/motion/z/bp);light + `[data-theme="dark"]`。
- 02-atoms 25 件(加 **placeholder.css**:`.ze-skeleton-lines` / `.ze-progress-ring` / `.ze-error-placeholder` —— 加载/管线/占位,加性)。
- 04-editors:header / footer / editor / layout / app-shell + **editor-classes.css**(gallery/canvas/scene3d/asset/media/task/inspector/param 视觉,加性);editor.css 增 `[data-run-phase]` 骨架/错误布局。
