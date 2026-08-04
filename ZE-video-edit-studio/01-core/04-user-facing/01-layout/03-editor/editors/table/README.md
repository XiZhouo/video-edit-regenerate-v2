# Table editor

代码入口: `TableEditor.tsx`。

## 说明

多实体表格。它把同一个 entity_type 的多条记录按 schema 字段排成可扫描、可选择、可右键操作的工作列表。

## Core 负责

- 列和行的显示、搜索框、单选/多选、行级上下文菜单
- 把选中写入全局 selection，让 property/canvas/review 等独立 editor 跟随
- 把新增、复制、重命名、删除这类意图转成 command dispatch

## Core 不负责

- 字段含义、排序后的业务解释、复制命令的实际副作用
- 详情属性面板，详情必须由独立 property editor 承担

## 消费入口

mod 不直接把这个组件塞进业务页面；mod 在 LayoutTree 里声明 slot，`slotAdapters.tsx` 把 `view_state` 翻译成 props。稳定 import 仍然是 `@ze/core/04-user-facing/01-layout/03-editor`。
