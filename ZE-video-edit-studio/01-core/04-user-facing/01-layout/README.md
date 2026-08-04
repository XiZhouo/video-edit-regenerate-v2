# 01-layout — 界面布局

整个 web 工作台的 4 区结构 + 中区 LayoutTree 调度 + 3 大子区域（Header / Footer / Editor）的内部规则。

## 本叶子文件

- README.md — 本文件（导览）
- **SPEC.md — 父级协议**：4 区结构（Header / Main / Footer / Overlay）、Main 区 LayoutTree 数据结构、拖拽 / 拉伸 / tab / 持久化运行时行为、跟 mod 的接口

## 3 个子叶子

1. [01-header/](./01-header/) — Header（顶栏：样式排版 / 文件菜单 / 设置菜单 / 帮助菜单 / 日夜间模式切换）
2. [02-footer/](./02-footer/) — Footer（底栏：点击展开 / 哪些事件该记录到 footer / 状态消息）
3. [03-editor/](./03-editor/) — Editor（中区 EditorSlot 内的渲染单元：12 种 default + 开集 + entity 绑定；**容器规则在父级 SPEC**）

## 怎么用

mod 在 [`02-mod/04-layout.config.ts`](../../../02-mod/04-layout.config.ts) 声明中区怎么拼；框架 App.tsx 经 `loadLayout(modLayout)` 读入，`usePersistedLayout(skillId, docId, tree)` 把用户调整持久化到 localStorage，`LayoutRenderer` 渲染：

```ts
import type { ModLayoutConfig, LayoutNode, EditorClass } from '@ze/core/04-user-facing/01-layout'

export const skillName = '我的工作台'   // Header 左段产品名
export const skillId = 'ze-my-skill'    // 布局持久化 key + data-mod 皮肤作用域

const slot = (id: string, editor: EditorClass, bind: string[], view?: Record<string, unknown>): LayoutNode =>
  ({ kind: 'slot', id, editor_class: editor, entity_bindings: bind, view_state: view })

export const modLayout: ModLayoutConfig = {
  tree: { kind: 'split', id: 'root', direction: 'horizontal', ratio: 0.3,
    first: slot('list', 'table', ['item']),
    second: slot('detail', 'form', ['item']) },   // 三栏 = 两次 split 嵌套
}
```

也可不写 tree，用 `preset + slots`（catalog_browse / linear_process / single_canvas / pipeline_batch，见 [`lib/presets.ts`](./lib/presets.ts)）。slot 内各 editor 的 view_state 调键见 [03-editor/](./03-editor/)。完整规则见 [SPEC.md](./SPEC.md)。
