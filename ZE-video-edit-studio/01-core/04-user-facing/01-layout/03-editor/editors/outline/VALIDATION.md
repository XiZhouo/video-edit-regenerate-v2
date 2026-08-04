# Outline editor validation

## 纯逻辑(vitest)

`lib/treeModel.test.ts` 覆盖:
- `childrenByParent`:按父分组 + 兄弟按 order 排序;悬挂父引用回退根(节点不消失)。
- `flattenVisible`:DFS 前序 + depth;折叠节点隐藏子树但保留自身;折叠根隐藏整棵子树。
- `isDescendant`:传递后代检测(拖拽改父环检测基石)。
- `computeReparent`:拒绝自移 / 移入自身后代 / 目标父不存在 / no-op;合法移动出带 `expected_version` 的 CAS 批量 patch、只为真变了的兄弟出 update。

## 真浏览器(gallery showcase)

- 树按层级渲染,depth 缩进逐级递增(行内 `paddingInlineStart` calc 令牌×深度)。
- 有子节点显 caret;点 caret 折叠/展开(`data-expanded` 驱动 caret 转向),子树显隐随之变。
- 点节点选中(节点 = `.ze-menu-item` 原子,`data-active` → accent wash);⌘/⇧ 多选;⌘A 全选可见、⌘⇧A 反选可见、Esc 清空;选择与相邻编辑器经 selection 联动。
- 声明 `visibilityField/lockedField` 后,眼睛/锁按钮 dispatch `entity.update {patches:{field:!cur}, expected_version}`;锁定节点本身不可拖拽 reparent。
- 声明 `renameField` 后,双击标题可编辑;声明 `duplicateCommand/deleteCommand` 后,复制/删除按钮发 `{type,id,expected_version}`。
- 声明 `thumbField/badgeField` 后,行缩略图和 badge 正常显示,但不改变选择协议。
- 窄容器(≤480px)隐 label 只留图标列(@container 断点)。
- 0 console error。

## 门禁

`editor_manifest_check`:outline 条目齐(id/class/status/rootSelector/needs + functionalZones selector 在 CSS + fixture 存在 + builtinInteractions 非空 + 子模块三文档)。`editor_frame_check`:走 EditorFrame、无裸根壳。`css_layer_guard`:L3 `03-zones/navigation/outliner` 零令牌(节点视觉走 L2 `.ze-menu-item` 原子,本层只留无令牌结构 + 几何字面量)。
