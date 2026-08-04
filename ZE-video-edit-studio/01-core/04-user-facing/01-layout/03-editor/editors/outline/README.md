# Outline editor

大纲 / 层级树编辑器(`outline` class)。把带 `parentId` / `order` 的扁平实体集渲成可折叠的单父层级树。

## 职责

- **层级 + 选择**:展示父子结构、展开/折叠、单选/多选(⌘/⇧/⌘A/⌘⇧A/Esc)。它管"选中哪个节点、树长什么样",不管节点内容怎么编辑。
- **树状图层面板**:可选行控件包括眼睛、锁、缩略图、badge、双击重命名、复制、删除和拖拽 reparent。锁定节点不能作为拖拽源,但其它节点仍可拖到它前后。
- **选择驱动联动**:与 `canvas2d` / `scene3d` / `form` 经 `selection[nodeType]` 联动 —— outline 选中谁,内容编辑器就渲谁。互不 import(L3 三律)。

## 不是什么

- 不是 `graph`:graph 是 DAG(连边/多入度),outline 是严格单父层级树。
- 不是 `gallery`:节点集扁平(无 `parentId`)时,mod 应在 layout 里绑 `gallery` compact-list 而非 `outline`(见 SPEC「outline ⇄ thumbnail 替代规则」)。

## 组合

`header`(HeaderZone:类型名 + 计数) + 可选 `control`(mod 声明命令时渲) + `outliner` 内容区(树)。相邻 slot 常放 `form`(选中节点属性)。

## 模型

树结构走 `lib/treeModel.ts`(纯逻辑,vitest 覆盖):`flattenVisible` 拍平可见行、`computeReparent` 算拖拽改父的 CAS 批量 patch。行控件只发通用命令或字段 patch:显隐/锁定/重命名走 `entity.update`,删除走 `deleteCommand ?? entity.delete`,复制走 mod 声明的 `duplicateCommand`。
