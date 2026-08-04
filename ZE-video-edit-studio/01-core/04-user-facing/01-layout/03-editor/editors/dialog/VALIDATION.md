# Dialog editor validation

## 真浏览器(gallery showcase)

- transcript 按 order 渲气泡;`data-role` 驱动配色,发送方(userRole)`data-self=true` 靠右 accent、其余靠左。
- 发送:输入框打字 + 点发送(或 Enter)→ `entity.add` 一条 user 消息 → 新气泡出现并滚到底(demo dispatch 真改本地态)。
- pending(status.phase=running)→ 末尾"生成中"占位气泡。
- 空态显 `editor.dialogEmpty`。
- 0 console error。

## 门禁

`editor_manifest_check`:dialog 条目齐(id/class/status/rootSelector/needs + functionalZones selector 在 CSS + fixture + builtinInteractions + 子模块三文档)。`editor_frame_check`:走 EditorFrame、无裸根壳、无 `toolbar=` 槽。`css_layer_guard`:`.ze-dialog*` 只用令牌、零字面色。`css_contract`:无未定义 class。

## 后续(未在本类)

反问 `dialog.answer` 暂挂续跑、终端 PTY-over-WS 子面 —— 需 supervisor/WS 后端,见 README 边界。
