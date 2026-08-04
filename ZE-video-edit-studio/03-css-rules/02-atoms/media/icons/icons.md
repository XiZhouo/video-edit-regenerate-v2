# Icons — 图标映射表(单一真源)+ 迭代规则

> 图标 = **令牌化图库**(css 16.2.0):美术源 `02-atoms/icons.svg`(16×16 网格 · 1.5px 圆头描边 ·
> 极简几何,贴 Geist 器物感)经 `scripts/icons_build.py` 生成三个产物——
> `01-tokens/icons.css`(`--ze-icon-<name>` data URI 令牌)· `02-atoms/icon-map.css`
> (`.ze-icon[data-icon=<name>]` → mask)· `02-atoms/icons.json`(名册 manifest)。
> **生成物禁手改**,门禁 `icon_tokens_check` 守(生成物同步 / 本表⇄sprite 双向 / data-icon 名册)。
> Unicode glyph 列保留为**降级/纯文本场景**(log、title 属性、无 CSS 环境);同一语义 SVG 与 glyph 成对登记。

## 用法(在该用的地方直接引用)

```tsx
<span className="ze-icon" data-icon="close" aria-hidden="true" />
<span className="ze-icon" data-icon="refresh" data-size="sm" data-tone="danger" data-spin="true" />
```

- 色 = mask × `currentColor`:随文字 ink / `.ze-icon[data-tone]` / 暗色全自动;
- 尺寸 `data-size` 闭集 xs(12)/sm(14)/默认 16/lg(20);
- 注入轴(`ToolbarAction.icon` / `CanvasToolDef.icon`)双轨:值是图库名 → 渲染 token 图标,否则按字符直显(向后兼容;helper = `01-layout/lib/zeIcon.tsx` 的 `IconOrGlyph`);
- 皮肤可在 `[data-mod]` 块覆盖某 `--ze-icon-*` 令牌换图标作品(词表不变,合法主题缝);
- 图标永远 `aria-hidden`,可达性语义放按钮的 `aria-label`/`title`。

## 风格规范(创作说明 —— 设计/重绘图标时照此执行,含交给 claude design 的全部约束)

**基因:极简优雅的器物感**(贴 Geist / 晴空瓷釉皮肤)——一个隐喻用最少的笔画讲完,几何干净,
没有装饰细节。判断标准:去掉任何一笔就不再达意,加任何一笔都是多余。

| 维度 | 规范 |
|---|---|
| 画布 | `viewBox="0 0 16 16"`;**安全边距 ≥2px**(笔画中心线落在 2..14 区间;触边语义如 crop 的出血线可至 1.5) |
| 笔画 | 统一 `stroke-width="1.5"` + `stroke-linecap="round"` + `stroke-linejoin="round"`;**单图标内禁混用笔画宽**。既有唯一例外:pause 用 2(双竖棒 1.5 太弱),不再新增例外 |
| 着色 | 一律 `currentColor` **单色**:描边类 `fill="none" stroke="currentColor"`,实心类 `fill="currentColor" stroke="none"`。禁多色/渐变/不透明度分层——token 管线走 mask,只取 alpha,任何分层着色都会丢失 |
| 形态二分 | **描边为主**;只有"小面积实心更清晰"的才整体实心(dot/spark/sort-asc/sort-desc/drag/play/stop 与三角箭头);描边件内允许 ≤1 个实心小点/小块作焦点(info/warn/help 的 ı 点、transform 的角柄、points 的中心点) |
| 几何词汇 | 直线 / 正圆 / 圆弧 / 圆角矩形(`rx` 0.6–1.5);自由曲线只用于隐喻必需(brush 的笔肚);**虚线** `stroke-dasharray="2 2"`~`"2.5 2"` 只表达「虚拟/选区/对称轴」语义(marquee/lasso/flip 轴),不作装饰 |
| 光学中心 | 视觉重量居中于 (8,8);**会挂 `data-spin` 旋转的图标(refresh 及未来 loading 类),其圆弧几何圆心必须精确 = (8,8)**——CSS 旋转绕盒中心,圆心偏 1px 转起来就偏摆 1px(真实返工案例:refresh 圆心曾在 (8,7),已修;新弧端点自查:两端点到 (8,8) 距离 = 半径) |
| 箭头语言 | 方向箭头 = V 形两短线(pan/download/chevron 族);旋转/循环/撤销类 = **L 形括号贴弧端**(refresh/undo/redo/rotate/loop 同一句法,全系列一致) |
| 命名 | `id="ze-i-<kebab-name>"`,语义命名不描形(close 不叫 x,marquee 不叫 dashed-rect) |
| 禁 | 写实细节/透视/阴影/表情化;直接挪用其它图标集的路径数据(风格语言可参考,几何必须原创);同一语义第二枚(先查本表防重复隐喻) |

**交付流(claude design 重绘/新增时)**:只动 `icons.svg`(在对应分组里画/改 `<symbol>`)→
`python3 scripts/icons_build.py`(三生成物再生)→ 本表补/改行 → `python3 scripts/icon_tokens_check.py` 绿
→ css 契约 bump(改既有图标几何=取值重调 PATCH/MINOR,增图标=MINOR)。**禁手改三个生成物**;
gallery `/gallery.html#l2-icon` 是全量目检页(63 枚网格 + 尺寸/语义色/旋转变体)。

## 迭代规则(飞轮:新图标怎么进来)

1. **先入库再上岗**:语义不在本表 = 违规(每个语义只用一个图标,全系列一致);
2. `icons.svg` 画 `<symbol id="ze-i-<name>">`(16×16 viewBox · 1.5px 圆头描边 · currentColor;实心件 `fill="currentColor" stroke="none"`);
3. 跑 `python3 scripts/icons_build.py`(令牌/映射/名册三产物再生);
4. 本表补语义行(glyph 降级列一并定);
5. css 契约 **MINOR** bump + 编年史;`verify_all` 全绿(icon-tokens 门禁守 1-4 步没偷懒);
6. 同日 `skill_sync --layers css` 下发(main.tsx 的令牌 import 随 core 层走)。

**消费者驱动,不预造**:只为真实出现的语义画图标(EXTENSION-RULES P-3 同理);
装饰性、纯展示字符(gallery 演示里的 `○`/`★` 等)不进本表。

## 同义裁决(已拍板,防漂移)

| 裁决 | 内容 |
|---|---|
| 关闭统一 `close` | `×`(U+00D7)出局,一律 `✕`/`ze-i-close` |
| `rotate` ≠ `refresh` | 旋转 90°(AssetEditor)语义独立,不复用刷新 |
| 主题 = `theme`(◐ 造型) | `☀/☾` 出局 |
| busy 态 ≠ 图标 | 进行中用 `.ze-spinner`,不用 `…` 顶替 label;「更多」才是 `more`(⋯) |
| 导出 = `download` | 画布 export 工具复用,不另画 |
| 错误占位 = `warn` | `.ze-error-placeholder__icon` 的 `!` 复用警告三角 |

## 动作

| symbol id | glyph 降级 | 语义 | 用在哪 |
|---|---|---|---|
| `ze-i-close` | `✕` | 关闭 / 删除 / 移除 | dialog 关闭、行内删除、chip 删除、toast 关闭 |
| `ze-i-check` | `✓` | 成功 / 选中 / 完成 | 成功态、checkbox、完成标记、采用候选、菜单选中标记 |
| `ze-i-plus` | `＋` | 新增 | 工具栏"新增"、添加空物体、新增候选槽 |
| `ze-i-minus` | `−` | 收起 / 减少 | 折叠区块、数值步进 |
| `ze-i-search` | — | 搜索 / 过滤 | 工具栏搜索框前缀 |
| `ze-i-edit` | — | 编辑 | bbox 编辑、重命名 |
| `ze-i-trash` | — | 删除(不可逆) | 删除物体/候选(danger 按钮内)|
| `ze-i-copy` | — | 复制 | 复制图片 URL/融合图 |
| `ze-i-download` | `⇩` | 下载 / 导出 | 导出 PSD/PNG、下载图片、画布 export 工具 |
| `ze-i-refresh` | `↻` | 重试 / 刷新 / 重绘 | 失败重试、重新画入、刷新 |
| `ze-i-undo` / `ze-i-redo` | `↶` `↷` | 撤销 / 恢复 | Header/footer/画布撤销恢复 |
| `ze-i-rotate` | `⟳` | 旋转 90° | AssetEditor 变换排 |
| `ze-i-flip-h` / `ze-i-flip-v` | `⇋` `⥯` | 水平 / 垂直翻转 | AssetEditor 变换排 |
| `ze-i-more` | `⋯` | 更多 | 溢出菜单触发 |
| `ze-i-zoom-in` / `ze-i-zoom-out` | `＋` `−` | 放大 / 缩小 | timeline/画布缩放钮 |
| `ze-i-upload` | — | 导入 / 上传 | header 文件下拉「导入」(与 download 对偶,V 形箭头同句法)|
| `ze-i-save-as` | — | 另存为 | header 文件行内「另存为」(折角文档 + 加号)|
| `ze-i-snapshot` | — | 快照(时点存档) | header 文件下拉「创建快照」(相机造型)|
| `ze-i-share` | — | 共享 / 成员 | 头像菜单成员面板标题(三点连线)|

## 状态 / 提示

| symbol id | glyph 降级 | 语义 | 用在哪 |
|---|---|---|---|
| `ze-i-info` | `ℹ` | 信息 | callout info、说明 |
| `ze-i-warn` | `⚠` | 警告 / 错误占位 | callout warn、危险确认、错误占位 icon |
| `ze-i-help` | `?` | 帮助 | Header 帮助/操作说明 |
| `ze-i-dot` | `•` | 列表点 / 中性状态 | neutral callout、状态灯(配 data-tone)|
| `ze-i-spark` | — | AI / 生成 | AI 任务、生成候选按钮、ai-coworker |

## 折叠 / 方向 / 排序

| symbol id | glyph 降级 | 语义 | 用在哪 |
|---|---|---|---|
| `ze-i-chevron-down` / `ze-i-chevron-right` / `ze-i-chevron-up` | `▾` `▸` `▴` | 折叠展开 | inspector collapsible、outliner 节点、菜单 caret、footer/状态栏展开收起 |
| `ze-i-sort-asc` / `ze-i-sort-desc` | `▲` `▼` | 排序方向 | 表头排序指示 |
| `ze-i-drag` | — | 拖拽把手 | 行/卡重排把手 |

## 运行控制

| symbol id | glyph 降级 | 语义 | 用在哪 |
|---|---|---|---|
| `ze-i-play` | `▶` | 运行 / 继续 / 播放 | 管线继续、任务启动、媒体播放 |
| `ze-i-pause` | `⏸` | 暂停 | 管线/任务/媒体暂停 |
| `ze-i-stop` | `⏹` | 停止(不可逆) | 停止任务/管线取消 |
| `ze-i-step-prev` / `ze-i-step-next` | `⤙` `⤚` | 上一帧 / 下一帧 | transport 帧步进 |
| `ze-i-loop` | `↺` | 循环 | transport 循环开关 |

## 对象 / 内容

| symbol id | glyph 降级 | 语义 | 用在哪 |
|---|---|---|---|
| `ze-i-image` | `▦` | 图片 / 缩略占位 | gallery 占位、媒体类目 |
| `ze-i-layers` | — | 图层 / 分层导出 | PSD 分层、修饰图层 |
| `ze-i-grid` | — | 网格视图 | 画廊/表格视图切换 |
| `ze-i-folder` | — | 项目 / 目录 | 打开项目、最近项目 |
| `ze-i-eye` / `ze-i-eye-off` | `●` `○` | 显示 / 隐藏 | 物体行可见性开关、图层显隐 |
| `ze-i-lock` / `ze-i-unlock` | `▣` `▢` | 锁定 / 解锁 | 物体行编辑锁、图层锁 |
| `ze-i-user` | `👤` | 用户 / 头像缺省 | 未登录头像 fallback |
| `ze-i-cube` | `◫` | 3D 场景 / 3D 模型 | scene3d 空态、AssetView 3D 模型(glb/obj/ply)类型 |
| `ze-i-chart` | `▤` | 图表 | chart 空态 |
| `ze-i-video` | — | 视频 | AssetView 视频预览/类型 |
| `ze-i-audio` | — | 音频 | AssetView 音频预览/类型 |
| `ze-i-file` | — | 文件(通用) | AssetView 非预览类型兜底(asset_ref 挂载) |
| `ze-i-pdf` | — | PDF 文档 | AssetView PDF 类型 |
| `ze-i-archive` | — | 压缩包 | AssetView 压缩包(zip 等)类型 |

## 画布工具(canvasTools 注入轴)

| symbol id | glyph 降级 | 语义 | 用在哪 |
|---|---|---|---|
| `ze-i-pan` | `✋` | 平移 | canvas 工具 pan |
| `ze-i-marquee` | `⬚` | 框选 | canvas 工具 select |
| `ze-i-transform` | `↔` | 变换 | canvas 工具 transform |
| `ze-i-brush` | `🖌` | 画笔 | canvas 工具 brush |
| `ze-i-eraser` | `⌫` | 橡皮 | canvas 工具 eraser |
| `ze-i-text` | `T` | 文字 | canvas 工具 text |
| `ze-i-crop` | `⌗` | 裁切 | canvas 工具 crop |
| `ze-i-filter` | `◐` | 滤镜 | canvas 工具 filter |
| `ze-i-rect` | `▭` | 矩形 | canvas 工具 rect |
| `ze-i-ellipse` | `◯` | 椭圆 | canvas 工具 ellipse |
| `ze-i-lasso` | `➰` | 套索 | canvas 工具 lasso |
| `ze-i-polygon` | `⬡` | 多边形 | canvas 工具 polygon |
| `ze-i-wand` | `✨` | 魔棒 | canvas 工具 wand |
| `ze-i-points` | `⊕` | 点提示(SAM) | canvas 工具 sam-points |

## chrome

| symbol id | glyph 降级 | 语义 | 用在哪 |
|---|---|---|---|
| `ze-i-settings` | `⚙` | 设置 / 配置 | Header 设置入口(造型=调节滑杆,非齿轮)|
| `ze-i-theme` | `◐` | 主题切换 | Header 日夜模式 |

> 装饰性、纯展示用的字符(gallery 演示里的 `○`/`★` 等)不进本表——它们不承载固定语义,不应在产品 UI 复用。
> 字符态排版(`·` 分隔、`—` 空值占位、`×` 尺寸乘号、`…` 截断)是文本不是图标,亦不进本表。
