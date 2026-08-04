# 本 skill 的布局 + Header / Footer / Editor 配置

> mod 业务方在这填本 skill 的工作台外观和入口配置。
> 完整规则见 [`../01-core/04-user-facing/01-layout/`](../01-core/04-user-facing/01-layout/) 各子叶子 SPEC。
>
> **本文件全空也合法** —— core 自带 default 兜底,渲染"模板态"。
> 删掉下方 `<填>` 占位填你自己的值即可。

## 1. Header 配置

> 完整 SPEC: [`01-header/SPEC.md`](../01-core/04-user-facing/01-layout/01-header/SPEC.md)

```yaml
header:
  # 必备:skill 名 = 工具/产品名(显示在 Header 左段),**不是项目/文件名**(那是 doc.name,显示在中段)。
  # 机器可读对应物:02-mod/04-layout.config.ts 的 `skillName` + 可选 `headerConfig`。
  skill_name: "ZE 示例工作台"

  # 可选:skill 图标 SVG / PNG 路径。不填走首字母圆形 fallback。
  # skill_icon: "/icons/my-skill.svg"

  # 操作说明(帮助菜单 → 操作手册):core 1.20.0 起**默认内联** webapp/public/operation-manual.html
  # 语义 HTML 片段(由 ZE-skill-tutorial-writer 操作说明工作台产出;唯一真源 = webapp/src/product/
  # operation-manual.zh.json)。无需配置即走默认路径;Header 与 ProjectPicker 从同一个
  # `headerConfig.manual_html` 读取。core 会移除片段内全部脚本/事件/表单/SVG/嵌入页/私有 CSS，
  # 只保留安全语义/媒体标签与 ze-om* class，再由统一 token CSS 排版。
  # manual_html: "/operation-manual.html"   # 覆盖默认片段路径(一般不用填)
  # manual: "/docs/manual.md"               # 回退:片段取不到时给的外链

  # 可选:反馈渠道 URL。不填则帮助菜单 → 反馈 灰显。
  # feedback_url: "https://feishu.cn/x/your-form"

  # 可选:最近项目列表上限。不填走 10。范围 5..50。
  # recent_max: 15

  # 可选:快照默认命名格式。不填走 "YYYY-MM-DD HH:mm:ss"。
  # snapshot_label_format: "v{YYYY}-{MM}-{DD} {HH}:{mm}"
```

## 2. Header 动态扩展(代码层,可选)

> 用于:无法用 yaml 表达的扩展(React 子树 / 自定义 dialog)。
> 在 `02-mod/05-code/header-overrides.tsx` 里写,App.tsx 通过 `overrides={...}` 传进 HeaderShell。

```yaml
# 数据类扩展可在这里 yaml 化(然后 App.tsx 解析转 React props):

# 文件菜单 - 新建对话框的项目类型列表
# new_doc_schemas:
#   - id: "blank"
#     name: "空项目"
#   - id: "template"
#     name: "从模板新建"

# 文件菜单 - 导出对话框的额外格式
# export_formats:
#   - id: "custom-pdf"
#     label: "PDF 报告"
#     ext: ".pdf"

# 关于对话框的额外字段
# about_extras:
#   - label: "业务版本"
#     value: "v2.3"
```

## 3. Layout(中区)—— 前端声明在 `04-layout.config.ts`

> 完整 SPEC: [`01-layout/SPEC.md`](../01-core/04-user-facing/01-layout/SPEC.md)

中区布局 + 新建默认字段是**业务知识**,声明在同目录 [`04-layout.config.ts`](./04-layout.config.ts)(webapp 经 `@mod/04-layout.config` 读)。框架 `App.tsx` 不再内联布局 —— 这是 Block3「复制替换地基」:core 的 App.tsx 因此可整树复制到任意 skill,不含业务。

**首选 preset(四种真实工作流形态,~6 行出 3-4 面板,不用手写树):**

```ts
// 02-mod/04-layout.config.ts
export const modLayout: ModLayoutConfig = {
  preset: 'catalog_browse',          // linear_process | catalog_browse | single_canvas | pipeline_batch
  slots: {
    list:      { editor: 'cardlist', bind: ['candidate'], view: { cardSize: 'balanced' } },
    workbench: { editor: 'canvas2d', bind: ['level'] },
    inspector: { editor: 'property', bind: ['object'] },
  },
}
```

| preset | 形态 | 典型 |
|---|---|---|
| `linear_process` | Input \| Main \| Output | 抹除 / 图生 3D / 线稿 |
| `catalog_browse` | List \| Workbench \| Inspector | seek-find / 素材库 |
| `single_canvas`  | Canvas (+ Params) | 绘制类 |
| `pipeline_batch` | Config \| 结果网格+进度 | 批量生成(Log 走 footer) |
| `layered_canvas` | 图层列表 \| 画布 \| 属性 | PS 图层工作台(图像编辑/关卡设计) |

**layered_canvas 三联样例**(图层=layer 实体,`zt.archetype('layer')`;详 COOKBOOK R14):

```ts
layout = { preset: 'layered_canvas', slots: {
  layers:    { editor: 'cardlist', bind: ['layer'], view: {
                 layoutMode: 'compact-list', cardSize: 'compact', thumbField: 'assetRef',
                 visibilityField: 'visible', lockedField: 'locked',
                 reorderCommand: 'layer.reorder' } },
  canvas:    { editor: 'canvas2d', bind: ['layer'], view: { preset: 'ps-lite', layerType: 'layer' } },
  inspector: { editor: 'property', bind: ['layer'] },
} }
```

三个编辑器零直连:点卡片→全局 selection['layer']→property 跟随+画布活动层(三铁律,03-editor SPEC §2.1b)。

**逃生舱**:preset 不够用时用 `tree`(原始 LayoutNode);本 demo 单 item 类型即用 tree 摆"列表+详情"两栏。

**新建默认字段**:`export const newEntityDefaults = { item: { name, data } }` —— App 的 `onAddFor` 据此 dispatch `entity.add`。

**实用极简**:常改的参数走 ParamPanel 常驻字段,少改的进 ParamPanel `advancedFields`(默认折叠),不要为它们新开面板。default theme(light/dark/system)走 Header 配置。

### 3.1 `canvas2d` 统一 2D 编辑器声明

`canvas2d` 是 build v3 的 PS-lite 母体。业务工具不要再复制画布;用 `preset/features/tools` 收窄能力,再把写入映射到 Product Command:

```ts
slot('workbench', 'canvas2d', ['artwork'], {
  preset: 'ps-lite',
  bgField: 'sourceRef',
  layerType: 'layer',                    // layered 数据源 = layer 实体桶(1.27.0 实体化)
  importCommand: 'artwork.import_source',
  layerAddCommand: 'artwork.layer_add',  // 可选;缺省回退内置 entity.add
  paintCommand: 'artwork.paint_layer',   // 可选;缺省回退 entity.update {assetRef|maskRef} 逐层 CAS
  exportCommand: 'artwork.export_png',
})
```

图层的列表/属性**不在画布里**:flat 列表=cardlist 卡片列表实例,tree/group 层级=outline 实例,属性=property,经 L5 组合(上节样例);改属性/眼睛锁=内置 `entity.update` 逐层 CAS,删=`entity.delete`,cardlist 重排走 `reorderCommand`,outline 改父/重排走 `entity.batch_update` 写 parent/order。

简化范围层(如 ZE Seek Find Object Library)用同一母体关闭图层列表语义,只暴露一张可画的像素选择层。字段名里仍有 `rangeMaskField` 是历史接缝;概念上它只是 asset_ref 像素层,业务后续可拿它当范围或遮罩计算:

```ts
slot('range', 'canvas2d', ['asset'], {
  preset: 'single-range-layer',
  bgField: 'imageRef',
  rangeMaskField: 'rangeMaskRef',
  maskCommand: 'Asset.UpdateRangeMask',
  tools: ['brush', 'eraser'],
})
```

规则:大图/mask 只保存 `asset_ref`;编辑器只 dispatch command。缺少专用 command 时 core 回退内置 `entity.add/update`(逐层 CAS,Operation/undo/协作链路保留),但 mod 若有业务副作用(例如 range stale、归一化、审计)必须声明自己的 command。

### 3.2 `pipeline` 固定管线 notebook 声明

`pipeline` 是 build v3 的 Pipeline Notebook 母体。每个业务工具里的 pipeline editor 默认服务一个固定管线,不要让用户在正常业务界面里自由选 run。固定关系在 slot `view_state` 里声明:

```ts
slot('pipeline-output', 'pipeline', ['candidate'], {
  pipelineId: 'object-finalize',
  startCommandId: 'object.finalize_inline',
  runIdField: 'runId',
  itemIdField: 'itemId',
  pipelineIdField: 'pipelineId',
  resultField: 'resultRef',
  allowRunSwitch: false,
  entityUi: {
    labelField: 'name',
    thumbField: 'resultRef',
    statusAnimation: 'thumbnail-pulse',
    summaryFields: ['stage', 'maskCoverage', 'completedAt'],
  },
})
```

规则:

- cell 来自 `StageSpec.command_id/input_ports/output_ports` 与 `PipelineRun.step_states`,不是业务面板硬编码。
- 输入/输出展示按 port kind 渲染(`image/mask/entity/json/metric/patch/artifact/log`)。
- 重启/继续/interrupt/resume 的动画读 `EntityRunStatus.phase/stages` 与 `entityUi.statusAnimation`;CSS 动画只能用 build-v3 已登记的 data-* / class,不要在 mod 里写新 `.ze-*`。
- 需要历史浏览或调试 run selector 时显式 `allowRunSwitch:true`;生产态默认固定当前 entity 的 `runIdField` 或固定 `pipelineId/startCommandId` 对应 run。

### 3.3 基础模块的二开边界

`canvas2d` / `pipeline` 都是基础原始功能。mod 除了开启/关闭功能,也可以基于它们进一步开发,但顺序固定:

1. 先用 slot `view_state` 配置 preset/features/tools/字段/command/ports/entityUi。
2. 再用 L5 LayoutTree 组合多个独立 editor。
3. 仍不够时才用 `custom` renderer,在 `02-mod/05-renderers.ts` 中引用 core 导出的 editor/primitives/model helpers 组合。

禁止:

- 复制 `Canvas2dEditor` / `PipelineEditor` 源码到业务 skill 后手改。
- 在 mod 里新增共享 `.ze-*` CSS 或重写 build-v3 皮肤。
- 绕过 command 直接 mutate entity。
- 为单个工具把业务 if 写进 core editor。

## 4. Editor 选用清单

> 完整 SPEC: [`01-layout/03-editor/SPEC.md`](../01-core/04-user-facing/01-layout/03-editor/SPEC.md)

```yaml
# 本 skill 用 core 12 种 default editor 中哪几种 + 自定义类(可选)
# editors:
#   default:
#     - canvas2d
#     - form
#     - table
#   custom: []  # mod 自定义 editor class(开集)
```

## 自检清单(提交前 checklist)

- [x] 第 1 节 `skill_name` 填了真名(工具名,非项目名;与 04-layout.config.ts 的 `skillName` 一致)
- [ ] 启动 `cd webapp && npm run dev`,浏览器看到的 skill 名 = 你填的值
- [ ] 想加菜单 / 自定义按钮的话,去 `05-code/` 写,不要改 `01-core/` 里任何文件
- [ ] 中区布局填在 `04-layout.config.ts` —— **优先 preset**;手写 `tree` 或在 App.tsx 里堆 JSX 面板 = 反模式
- [ ] 外观定制只加在本 skill 皮肤 `03-css-rules/01-tokens/tokens.css` 末尾的 `html[data-mod]` 覆盖块(scaffold 注入/手加),**不**改皮肤其余部分;定制后不再 `--layers css` 同步
- [ ] core 升级(Skill.Sync 跑)后,**本文件 + 04-layout.config.ts + 02-mod 全部都不会被覆盖**
