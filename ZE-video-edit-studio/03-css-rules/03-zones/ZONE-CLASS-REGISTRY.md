# 功能区结构类注册表 · Zone Class Registry（闭集 · 唯一权威）

> 本表把 [SPEC.md](./SPEC.md) 的 **管理 / 工具 / 内容 + chrome** 模型形式化为 **8 个结构类的闭集**。
> - **结构类 = UI 结构模式**,不是业务能力名,也不是屏幕位置。"字段组""控制条""列表""视口"是结构类;"试玩广告制作参数""AI 方案预览""Luna 导出进度""校验问题列表"是业务实例。
> - **命名沿用既有 zone 词表**(ManageZone / ToolZone / 内容 zone / chrome),每个结构类由 1..N 个**既有** zone 组件实现——本表是"存在哪些结构类"的闭集权威,不新造第三套命名(反熵)。
> - 分工:SPEC.md 讲"怎么判断 / 怎么用",`zones/types.ts` 定组件契约,**本表定闭集 + 每类规格 + 收敛证据**。
> - 收敛依据 = 四个真实 v3 产品实测(Packer / Foundry / Playable / Audit),不是纸面推演。见 §3。

## 0. 三条铁律

1. **CSS 只认结构类,不认业务实例。** `03-css-rules` 的 CSS selector 里不得出现真业务域词(`playable` / `luna` / `seek` / `hidden` / `brand` / `cta` / `export-platform` / `recipe` …)。业务语义全在 mod 声明的字段 + 命令 + `data-*` 里。由 `zone_class_check` 门禁把关(§4 机读闭集)。
2. **结构类是闭集。** 新 UI 只能引用本表已登记的 8 类;真出现一个落不进任何类的新结构模式,**回本表加类**,不在业务层私造功能区。
3. **差异必须承载语义。** 每个视觉差异都编码层级 / 角色 / 状态;同结构必须同形式。(承接 UI 三准则:层级编码 / 语义对位 / 功能形式统一)

## 1. 闭集:8 结构类 × 3 tier

结构类落在编辑器的三个 tier。tier 决定边界基调,结构类决定内部规格。

### Tier A · 编辑器 chrome(贯通宽,不块化)

#### A1. 状态区类 · Status-Area

- **解决**:"当前是谁、系统状态如何"。不解决"要做什么"。
- **既有实现**:`HeaderZone`(`__header`:身份 / 标题 / 计数)+ `StatusZone`(`__statusbar`:running / queued / latestOp / segments)+ `StatusTile` 原子行。
- **边界**:`chrome`——单边发丝线或透明,贯通编辑器宽,不块化(规则 `EDITOR_BAR_NOT_BLOCK`)。
- **滚动角色**:chrome-fixed——`flex: 0 0 auto` 锁 1 行,自身不滚(01-layout SPEC §2.10 C2c)。
- **尺寸**:固定 1 行,`--ze-row-h`,禁纵向 padding(规则 `ROW_VPAD_BREAKS_BASELINE`);状态段最多显 3,超出合并摘要;窄栏只留标题 + 状态点 + 计数。
- **允许开关**:`StatusSegment[]`(badge / count / progress)、HeaderZone title / count、保存 / 同步 / 运行 / 版本状态、trace(折叠或弹出)。
- **禁止**:业务主按钮、表单、长日志、错误列表(详情交给 列表类)。
- **证据**:P ✓ / F ✓ / A ✓(StatusTile 行 + HeaderZone);PL ~(状态嵌在卡内)。**吞并草稿的 单行信息 + 状态条**(实测同为 StatusTile / 身份行,草稿的 identity/system 二分在形式上无差异)。

### Tier B · 操作区(横条,`control-group` 边界)

#### B1. 控制条类 · Control-Bar

- **解决**:"看什么、管哪些实体"(管理)与"对当前对象做什么"(工具)。
- **既有实现(两子角色,同一结构类)**:`ManageZone`(role=manage:搜索 / 筛选 / 排序 / 选择管理 / 全选 / 反选 / 清空 / 删除选中)+ `ToolZone`(role=tool:brush / eraser / select / transform / run / retry / command buttons)。旧 `filterbar→manage`,`control` / `toolrow` / `actionbar` / `toolbar` / `canvas-toolbar`→`tool`(见 SPEC 兼容表)。**七个历史近义名在组件层早已是这一个类的两个子角色。**
- **边界**:`control-group`——外层不画同款四边块;按钮、分段控件、chip、组间分隔线表达边界。少数确需面板感的操作区才显式 `data-boundary="panel"`。
- **滚动角色**:hug——`flex: 0 0 auto` 永不被压;`hug-scroll` 只横滚、`wrap-cluster` 3 行预算内自滚,均不纵向增长。
- **布局**(横条,`data-layout`):`wrap-cluster`(pill 筛选)/ `flex-fill`(按比例占满换行)/ `hug-scroll`(单行不够横滚)。最多 3 行,超出进更多菜单;控件高不超 `--ze-row-h`;子项须有 min-width。
- **允许开关**:`ToolbarAction`(command / toggle / search / cycle)、`GalleryAction`、`FormAction`、`GridFilter`、`CardSort`、`RowAction`;搜索 / 筛选 / 排序 / 分组 / 批量选择 / 主次动作 / 更多菜单 / 横向滚动。
- **禁止**:大表单、结果列表、长文本说明、业务详情、运行日志;写数据的按钮必须绑命令;危险动作必须确认。
- **证据**:P ✓ / F ✓ / A ✓;PL 的 action 进了 customZones(仍是本类)。管理与工具**保留二子角色**(语义有别:管集合 vs 改对象),不塌成一个无区分的按钮排。

#### B1.3 · control-bar 实例族子角色映射(历史 zone id → manage / tool)

> 这是**收拢既有词表的查找视图**,不新造命名 —— 子角色沿用 B1 的 manage / tool 二分,来源 = `zones/types.ts` 的 `ActionZoneId` 兼容层注释 + B1 SPEC 兼容表。表内 file:line 以当前 main 现查为准。真删 / 日落(7 个历史 id 清退 + `@deprecated` 类型标注 + inline 自写 zone 迁 `FunctionZone`)归 **W13 正名日落**,本表只作其**文档伴生物**,不触碰下方 `<!-- ZONE-CLASS-REGISTRY:BEGIN/END -->` 机读 fence,`zone_class_check` 零影响。

| 历史 zone id | 子角色 | 既有 zone 组件 | 发射的 data-zone / data-role | 活 CSS selector |
|---|---|---|---|---|
| `filterbar` | manage | `FilterBarZone.tsx` | `data-zone=manage` · `data-role=filterbar` · `data-function-zone=manage` | `.ze-editor__zone[data-role="filterbar"]`(editor-shell:418)✓ |
| `control` | tool | `ControlZone.tsx`(+ `PromptZone`/`ActionBarZone` 复用 `data-role=control`) | `data-zone=tool` · `data-role=control` · `data-function-zone=tool` | `.ze-editor__zone[data-role="control"]`(editor-shell:410)✓ |
| `toolbar` | tool | `ToolRowZone.tsx`(新)· `ToolbarZone.tsx`(legacy)· `MaskToolbarZone.tsx` | ToolRow/Mask:`data-zone=toolbar` · `data-role=toolbar`;Toolbar(legacy):`.ze-editor__toolbar` `data-zone=toolbar`(无 data-role) | `.ze-editor__toolbar`(editor-shell:52)✓;`[data-role="toolbar"]` 无专属规则(走 `.ze-editor__zone` 基座) |
| `actionbar` | tool | `ActionBarZone.tsx` | `data-zone=actionbar` · `data-role=control`(注:role 复用 control,非 actionbar) | `.ze-editor__actionbar`(04-editors/input/form:22)✓ |
| `transport` | tool | `TransportZone.tsx` | `data-zone=transport` · `data-role=transport` | `.ze-media__controls`(04-editors/media/media:21)✓;`[data-role="transport"]` 无专属规则 |
| `canvas-toolbar` | tool | `CanvasToolbarZone.tsx` | `data-zone=canvas-toolbar`(贴 `.ze-editor__toolbar`,无 data-role) | 共用 `.ze-editor__toolbar`(editor-shell:52)✓ |
| `context-menu` | tool | 无 zone 组件(**虚位**) | 无(仅 `ActionZoneId` union 遗留) | 无活 CSS;行右键走 `RowAction.placement` |

> 撞车提示(供 W13 清退消歧):`control` 的 `data-role="control"` 与「信息+控制单元」控制区 `.ze-editor__unit-control[data-role="control"]`(字段组类 C2)**同串异义** —— 清退 `control` zone id 前须先消歧,否则误伤 property/pipeline 的单元控制区。

### Tier C · 内容区(主内容,每 editor 唯一)

#### C1. 列表类 · List

- **解决**:"可逐条定位 / 浏览 / 选择的记录或候选集合"。
- **既有实现**:`CardListZone`(cardlist)+ `TableZone`(list / table)+ `GridZone`(grid)。密度是开关不是新类。
- **边界**:`rows`——以行分隔 / 选中态 / 滚动归属表达结构,外层不再套同款卡;软滚动 fade(规则 `SOFT_SCROLL_NEEDS_FADE`)。
- **滚动角色**:scroll-owner——纵滚唯一发生在列表 body(`__gallery-body` / `__tbl-scroll`);卡/行自身 hug 不压缩。
- **尺寸**:普通行 1 行 / 复杂行最多 2 行;卡片网格最小卡宽 120px + 固定比例;缺失 / loading / error / ready 占位一致;超量内部滚动。
- **允许开关**:密度(`row` / `card` / `result`)、缩略图、`ListColumn`、`RowAction`(行内 / 右键)、`GridFilter`、严重级 badge、评分 / 标签、单选 / 多选、分组、采纳 / 删除 / 定位 / 修复 / 忽略。
- **禁止**:卡里塞完整表单或嵌 editor;候选采纳 / 多选写入必须走命令且可撤销;只显红字不能定位。
- **证据**:**全 4 产品**,常多实例。**吞并草稿的 行列表 + 卡片网格 + 结果列表**——Audit 实测三者是同一个 `ScrollStack + ze-card`,仅内容密度不同(问题清单 / 规则卡 / 产物行同构)。

#### C2. 字段组类 · Field-Group

- **解决**:"编辑一组字段"与"信息 + 控制单元"。
- **既有实现**:`PropertyZone`(property / form)经 `InspectorSectionSpec`(kind=fields / readonly-kv / params / controls / preview / variants / asset)+ `InfoControlUnit`(信息区 `[data-role=info]` + 控制区 `[data-role=control]` 固定二分)。`ze-disclosure` 折叠 = **内部原子开关**(吞并草稿的 折叠区)。
- **边界**:`unit`——raised 单元,信息区不夹命令、控制收一处、嵌套限一层。
- **滚动角色**:单元 hug(`.ze-editor__unit` `flex: 0 0 auto`,与 overflow:hidden 成对锁);scroll-owner 是 `__form-body`。
- **尺寸**:默认单列,任何宽度不拆半宽字段矩阵;普通字段 1 行,多行文本只用 3 / 4 / 8 行;高级字段默认折叠;组内 > 8 字段建议分组。
- **允许开关**:只读 / 可写 / 必填 / 禁用、高级折叠、实时 / 延迟提交、重置默认、混合值显示、批量编辑、`InspectorControlSpec`(傻瓜参数)、`FormAction`。
- **禁止**:没有写入的假字段、把导出字段混进制作字段、字段外再套业务卡壳、用 `.ze-grid` 把 field-row 拆碎。每个可写字段必须有字段路径或命令,改后刷新依赖预览 / 校验 / 状态。
- **证据**:**全 4 产品**。

#### C3. 视口类 · Viewport

- **解决**:"可看或可交互的视觉主区域"。
- **既有实现**:`Canvas2DZone`(canvas2d)+ `Model3DZone`(scene3d)+ media / preview + `Canvas2dSpatial` overlay 层。
- **边界**:`viewport`——sunken surface + edge + radius + overflow,禁裸奔;硬裁切,不叠软滚动 mask(规则 `HARD_VIEWPORT_FORBIDS_FADE`)。
- **滚动角色**:viewport-clip——`overflow: hidden` 硬裁,pan/zoom 归 viewport 自身,不做滚动 fade。
- **尺寸**:填满容器或声明固定比例;loading / empty / busy / error / ready 占位一致;内部 renderer 监听容器尺寸,不由内容撑破外部布局;overlay 不遮挡核心交互除非明确阻断态。
- **允许开关**:缩放 / 平移 / 选择 / overlay / 辅助线 / 标尺 / **显示模式选择器(贴 `.ze-viewport` + `data-display-mode="phone|free|safe"`,比例/minHeight 走框架 token `--ze-viewport-*`;`data-safe-area` 业务态)** / 空态 / 错误态。
- **禁止**:裸放无边界、塞字段组 / 完整列表 / 导出配置;渲染失败必须有错误态。
- **证据**:P ✓ / PL ✓ / A ✓(Canvas2dSpatial 双 overlay);F ✗。

#### C4. 树类 · Tree

- **解决**:"层级对象的选择与少量操作"。
- **既有实现**:`OutlineZone`(outline)。
- **边界**:`rows`——节点行 + 缩进 + 展开 / 折叠 + 选择;过长内部滚动。
- **滚动角色**:scroll-owner——`.ze-outliner` `flex: 1; min-height: 0; overflow: auto`。
- **尺寸**:每节点 1 行,节点名超长省略;窄栏只显名称,MD 以上显状态 / 类型。
- **允许开关**:搜索 / 展开深度 / 拖拽排序 / 重命名 / 删除 / 只读 / 多选 / 定位。
- **禁止**:默认给普通用户当主路径(除非层级编辑就是主任务)、直接编辑危险字段、节点内塞复杂表单;节点操作走命令,展开态属视图态。
- **证据**:PL ✓(scene_node outline)。稀有但真实——Audit 把规则按状态线性分组(非层级),不是树。

#### C5. 输入区类 · Input

- **解决**:"用户输入意图 / 文本"。
- **既有实现**:`ConversationZone`(conversation)——上下文摘要 + 输入框 + 建议 + 附件 / 引用 + 发送 / 停止 / 重试;transcript 复用 列表类 语言。
- **边界**:transcript 段 `rows`;输入段 `control-group`。
- **滚动角色**:transcript 段 scroll-owner;输入段 hug(输入框 3→8 行封顶,不无限增高)。
- **尺寸**:输入框默认 3 行 / 展开最多 8 行;上下文摘要 1 行;窄栏按钮换到输入框下方。
- **允许开关**:建议 prompt / 附件 / 上下文范围 / 停止 / 重试 / 清空 / 引用当前选择。
- **禁止**:输入后直接改正式数据(输出只能进 决策确认 / 任务处理 / 澄清问题)、输入区无限增高、不说明当前上下文、绕过方案确认。
- **证据**:**全 4 产品**(F / PL 是 chat / AI 意图;P / A 是控制条内的 search-compose)。

#### C6. 决策确认类 · Decision-Confirm

- **解决**:"用户确认即将发生的修改"。
- **既有实现(本期无专属 zone)**:经 customZone(PL `proposalActions`)/ `FormAction` unit-control(A finding triage)/ `callout` 原子 + 动作钮组实现。第二期可提取专属 `ConfirmZone` 组件。
- **边界**:`unit` 或 `panel`;与 视口类 配合可就地预览(PL 的 AI 方案覆盖 scene3d snapshot 后再确认)。
- **滚动角色**:hug——折叠 2 行/展开 8 行封顶,不接管滚动(预览定比例/定高)。
- **尺寸**:折叠态 2 行 / 展开最多 8 行;预览固定比例或固定高度;窄栏优先摘要 + 确认入口。
- **允许开关**:diff / 风险 / 预览快照 / 局部采纳 / 重新生成 / 确认应用 / 取消 / 影响范围。
- **禁止**:一句"已生成"直接应用、无取消、无 diff / 影响说明、应用不可撤销;确认应用必须走命令并形成一次可撤销 Operation。
- **证据**:PL ✓(AI 方案卡)/ A ✓(finding 处置钮组)。当前寄生实现,登记为结构类以约束"AI / 自动修改必须经确认"。

## 2. 砍(不进闭集)

四产品零真实消费,不登记、不实现 CSS。真出现新模式时回本表按规格加类,不私造。

| 草稿类 | 裁定 | 证据 |
|---|---|---|
| 任务栈类 | **砍** | 0 独立渲染;生成 / 导出 / 预检的任务态都寄生在 列表类 + 状态区类(F 网格显 genStatus、PL 只显最新 proposal、A 无队列 UI)。 |
| 对比视口类 | **砍** | 0 消费;PL 的 AI 修改前后是**就地覆盖 snapshot**,非并排。草稿 §7.10 此项是纸上假设。 |
| 槽位网格类 | **砍** | 0 真实消费;P 的 item-bar 是 1D 游戏复刻(带 `mod-renderer-allow-inline-visual` 豁免),PL 的品牌 / 模型 / 音效槽是 `node.logic.socket` 逻辑非视觉网格。 |

## 3. 四产品收敛证据矩阵

P=Packer(列表 / 表格 / 校验)、F=Foundry(批量任务 / 队列 / 结果)、PL=Playable(视口 / AI / 导出,最富)、A=Audit(问题行列表)。✓=真用 ~=部分 / 嵌在别处 ✗=未用。

| 结构类(闭集) | P | F | PL | A | 收敛自草稿 |
|---|---|---|---|---|---|
| 状态区类 | ✓ | ✓ | ~ | ✓ | 单行信息 + 状态条 |
| 控制条类 | ✓ | ✓ | ✓(customZone) | ✓ | 控制条(合 7 近义名) |
| 列表类 | ✓ | ✓ | ✓ | ✓ | 行列表 + 卡片网格 + 结果列表 |
| 字段组类 | ✓ | ✓ | ✓ | ✓ | 字段组 + 折叠区(降原子) |
| 视口类 | ✓ | ✗ | ✓ | ✓ | 视口 |
| 树类 | ✗ | ✗ | ✓ | ✗ | 树 |
| 输入区类 | ✓ | ✓ | ✓ | ✓ | 输入区 |
| 决策确认类 | ✗ | ✗ | ✓ | ✓ | 决策确认 |
| ~~任务栈 / 对比视口 / 槽位网格~~ | ✗ | ~ / ✗ / ✗ | ~ / ✗ / ✗ | ✗ | **砍** |

草稿 15 类 → 证据收敛为 **8 实类,砍 3**,其余并入。与既有 `ContentZoneId` + `ActionZoneId` 一一对得上,是"收拢既有词表"而非另起。

## 4. 机读闭集(`zone_class_check` 解析)

`scripts/zone_class_check.py` 解析本区:CSS selector 出现 `BUSINESS_WORD_DENYLIST` 词即红;`FRAMEWORK_TERM_ALLOWLIST` 是框架分类法白名单(不算业务词)。denylist 可随新迁移产品补词。

<!-- ZONE-CLASS-REGISTRY:BEGIN(zone_class_check.py 解析) -->

```yaml
CLASSES:            # 8 结构类闭集(id → 既有 zone 实现)
  status-area:      [HeaderZone, StatusZone]
  control-bar:      [ManageZone, ToolZone]
  list:             [CardListZone, TableZone, GridZone]
  field-group:      [PropertyZone]
  viewport:         [Canvas2DZone, Model3DZone]
  tree:             [OutlineZone]
  input:            [ConversationZone]
  decision-confirm: []          # 本期无专属 zone,经 customZone/FormAction/callout 实现

CUT:                # 零消费,不入闭集;真出现回本表加类
  - task-stack
  - compare-viewport
  - slot-grid

BUSINESS_WORD_DENYLIST:   # 真业务域词,禁进 03-css-rules CSS selector
  - playable
  - luna
  - seekfind
  - seek-find
  - hiddenobject
  - hidden-object
  - brand
  - cta
  - recipe
  - export-platform
  - itembar
  - item-bar
  - foundry
  - packer

FRAMEWORK_TERM_ALLOWLIST:  # 框架分类法,不算业务词(即使含 ai/scene 等子串)
  - ai        # Operation.source(user/ai/sync/system 审计枚举)
  - user
  - sync
  - system
  - empty     # data-ui-state / --ze-state-* 生命周期
  - running
  - error
  - stale
  - disabled
  - normal
  - scene3d   # EditorClass 名(.ze-editor--scene3d / .ze-scene3d-host 既定约定)
  - canvas2d
  - timeline
  - outline
  - manage    # zone role
  - tool
  - filterbar
  - control
  - toolbar
  - statusbar
  - inspector
```

<!-- ZONE-CLASS-REGISTRY:END -->

## 5. 黄金消费参考 + 已知逃逸

- **黄金实现** = `ZE-seek-find-level-audit-v3/02-mod/05-renderers.tsx`:三面板全 `editor:'custom'` 却 **100% 干净**——全程消费 `FunctionZone` / `HeaderZone` / `EditorFrame` / `Canvas2dSpatial` / `ze-*` 原子,零内联视觉。**证明:干净与否取决于是否消费 zone 组件 + 原子,而非是否 custom。** 新 custom renderer 照此实现。
- **viewport 显示模式(css 18.1.0 已框架化 ✅)**:原 PL `renderers.tsx` 的 `phoneStageStyle` / `freeStageStyle`(`aspectRatio:'9/16'` + `minHeight:320`)已提取为框架 token `--ze-viewport-phone-aspect` / `--ze-viewport-free-min-h` + L2 utility `.ze-viewport[data-display-mode]`;消费者贴 `.ze-viewport` + `data-display-mode` 声明式选档,零内联视觉。`data-safe-area` 保留业务视口态(框架只留 hook)。
- P 的游戏复刻内联带 `mod-renderer-allow-inline-visual` 豁免注释(像素级复刻,合规保留)。

## 6. 关联

- 边界语法 / 布局 / InfoControlUnit / Custom Renderer 判定:[SPEC.md](./SPEC.md)
- 组件契约(拓展点闭集):`01-core/04-user-facing/01-layout/03-editor/zones/types.ts`
- CSS 分层与令牌:[../../DESIGN-SYSTEM.md](../../DESIGN-SYSTEM.md)(§0.3 规则注册表登记 `zone_class_check`)
- 第二期(需另立项):实例声明模型 / editor 组合协议(主内容唯一 / 关系角色 / 响应式优先级)/ 专属 ConfirmZone / viewport-mode token / 下游内联清理。
