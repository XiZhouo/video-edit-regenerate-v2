# 03-editor — Editor

> 一句话:editor 是中区的工作面。它把 entity 渲染成可看、可选、可编辑、可运行的界面单元。

## 当前裁定

编辑器按 **entity + command + layout** 消费 core 能力:

- entity 是数据真相:图层、消息、HTML 页面、pipeline item、属性目标都先是 entity。
- command 是写入真相:编辑器只 dispatch command,不直接改业务数据。
- layout 是组合真相:多个 editor 在 L5 LayoutTree 里拼成工作台,编辑器之间不互相 import。

标准公共命名:

| 公共 editor class | 说明 | 兼容名 |
|---|---|---|
| `canvas2d` | 统一 2D 工作面。raster/vector/viewer 是 preset,不是三种编辑器 | - |
| `scene3d` | 统一 3D 工作面。game-engine/modeling 是 profile | - |
| `cardlist` | 卡片列表/缩略图/图层 flat 列表/候选池 | `gallery` |
| `property` | 属性编辑器。基于选中 entity 范围自动排版信息和控制 | `form` 实现层 |
| `pipeline` | 管线操作台 + notebook 式运行视图 | - |
| `outline` | 层级 entity 大纲。tree 是底层 primitive | - |
| `conversation` | AI 对话/人机问答/PTY profile 宿主 | `dialog` |
| `html` | HTML source/preview/sandbox/render/edit | - |

`form` 不再称为属性面板,它只是低级字段/记录表单实现;`gallery` 和 `dialog` 只作为旧 class alias 保留。新 mod 和新文档一律写 `property`、`cardlist`、`conversation`。

机器真源:

- editor 注册与采纳状态: [`03-css-rules/EDITOR_MANIFEST.json`](../../../../03-css-rules/EDITOR_MANIFEST.json)
- slot 到组件的消费逻辑: [`../lib/slotAdapters.tsx`](../lib/slotAdapters.tsx)
- editor class 类型: [`types.ts`](./types.ts)

## 功能区关系

功能区分三类:

- `ManageZone`:搜索、筛选、排序、选择管理、全选、反选、清空、删除选中。负责“看什么、管哪些实体”。
- `ToolZone`:工具、运行、retry、paint、select、transform、pipeline lifecycle。负责“对当前对象做什么”。
- 内容功能区按编辑器命名:`Canvas2DZone`、`Model3DZone`、`CommandZone`、`PropertyZone`、`OutlineZone`、`CardListZone`、`HtmlZone`、`ConversationZone`。

旧 `filterbar/control/toolbar/actionbar` 是实现兼容层。新文档和 manifest 统一映射到 ManageZone / ToolZone。

## 怎么用

mod 不写 editor 组件,只在 [`02-mod/04-layout.config.ts`](../../../../02-mod/04-layout.config.ts) 声明 slot:

```ts
slot('layers', 'cardlist', ['layer'], {
  layoutMode: 'compact-list',
  cardSize: 'compact',
  thumbField: 'assetRef',
  labelField: 'name',
  visibilityField: 'visible',
  lockedField: 'locked',
  reorderCommand: 'layer.reorder',
})

slot('canvas', 'canvas2d', ['layer'], {
  preset: 'ps-lite',
  layerType: 'layer',
  bgField: 'sourceRef',
  paintCommand: 'artwork.paint_layer',
})

slot('props', 'property', ['layer'], {
  followSelection: true,
  sectionsByType: {
    layer: [{ id: 'basic', kind: 'fields', title: '基础', fields: ['name', 'opacity', 'visible'] }],
  },
})

slot('run', 'pipeline', ['candidate'], {
  pipelineId: 'object-finalize',
  startCommandId: 'object.finalize',
  allowRunSwitch: false,
})

slot('chat', 'conversation', ['message', 'page'], {
  roleField: 'role',
  textField: 'text',
  replyCommand: 'page.generate',
  replyTargetType: 'page',
  replyTargetIdArg: 'id',
  replyTextArg: 'prompt',
})

slot('preview', 'html', ['page'], { htmlField: 'html' })
```

## 2D 编辑器白话

`canvas2d` 是一个带图层的 2D 母体,可以按像素运行,也可以按矢量运行,还可以只当查看器。PS、Illustrator、viewer 不是三类 editor,而是同一 editor 的 preset/profile。

- ZE-sketch-to-design:矢量层用于标注,像素层用于图像。
- ZE-seek-find-background-generator / ZE-seek-find-object-generator:图层和候选对象用 `cardlist/property` 管理,画布只负责投影与操作。
- ZE Seek Find Object Finalizer:回执 mask 转成像素图层,后续消费逻辑自己计算。
- ZE-desktop-pet-studio:只需要单图层像素绘制,仍然走 `canvas2d` 的收窄 preset。

图层列表和属性面板不进入画布内部。flat 图层栈用 `cardlist`,复杂层级用 `outline`,属性用 `property`,三者通过全局 selection 联动。

## CardList 介绍

CardList 是通用卡片容器编辑器:每张卡代表一个 entity,可以有图也可以没图;卡片负责展示缩略图、标题、描述、摘要、状态和动作。它不是“图片九宫格”的小组件,而是资产池、候选池、图层列表、对象清单的统一工作面。每行数量由容器宽度和当前缩略图尺寸自动涌现;选择全集包括单选、多选、加选、减选、全选、反选、清空、删除和批量动作。

`cardSize: 'compact' | 'balanced' | 'spacious'` 是 mod 默认 preset；用户运行时通过无级“缩略图尺寸”滑杆保存个人视图偏好，不是裸显 LG/MD/SM 循环。搜索、筛选、排序和缩略图共用 `user × skill × project × slot` 的版本化 slice；切换账号或项目不会串状态。

运行态是实体的附加提示，不是替代内容：running/error 卡仍显示已有图片、标题和摘要，也仍可选择、键盘激活、右键、预览、显隐、锁定与排序；只有删除在途目标被禁用。pending 只有在 Supervisor 明确签发 `placeholder:true` 时用骨架，前端不得从空 data、名字或文案猜身份。错误只附着到对应卡片，局部重试不把整个列表或卡片锁死。

Canvas2D 的 pan/zoom/显式 fit 使用同一 scoped store。第一次打开且没有用户偏好时会自动 fit，但这次计算只更新当前画面、不写 storage；用户真正操作后才成为可恢复偏好。相机/视口不进入领域 Operation 或 undo。

## Property 介绍

`property` 不是普通 form。它是 selection/entity range 的信息展示和控制台:单选、多选、跨类型、混合值、只读信息、可编辑字段、运行态、provenance、引用关系都在这里排版。PropertyZone 由多个 InfoControlUnit 组成:信息区只放数据,控制区只放 command。所有修改走 command;批量修改是一条 Operation,保证一次 undo。

## Pipeline 介绍

`pipeline` 常驻显示管线骨架,无 run 时也不整块消失。它面向用户显示“能理解的步骤”,可以把多个底层 command 合成一个步骤;每步展示输入、输出、状态、错误、artifact 和历史。标准 lifecycle 包括 pause、interrupt、resume、cancel、retry、rerun step、run from step。

Finalizer 这类业务面板应配置成一个合成步骤:输入=抠图后对象,输出=最终结果;输入/输出历史用下拉切换,当前选中项作为 rerun 上下文。

## 二开边界

`canvas2d` 和 `pipeline` 是基础原始编辑器模块。mod 先用 `view_state` 的 preset/features/tools/command/ports/entity UI 收窄能力;仍不够时再写 `custom` renderer,并复用 core 导出的基础模块:

```ts
import {
  Canvas2dEditor,
  PipelineEditor,
  CanvasViewport,
  resolveCanvas2dPreset,
  type CustomPaneProps,
} from '@ze/core/04-user-facing/01-layout/03-editor'
```

二开规则:

- 继续使用 `CustomPaneProps.dispatch / assetUrl / statusOf / viewState`。
- 写入只 dispatch command,不直接改 entity。
- 视觉只贴现有 `.ze-*` class / data-*;缺共享 CSS 回 build-v3 的 `03-css-rules` 源头补。
- 运行态只读 `EntityRunStatus` / `PipelineRun`,不要在 mod 里另造 pipeline/canvas 状态模型；占位只认 runtime 签发的 `placeholder`。
