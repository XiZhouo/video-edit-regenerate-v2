# Html editor validation

## 真浏览器(gallery showcase)

- 预览态:渲染 `<iframe sandbox="allow-scripts">`,`srcDoc` = HTML 源;iframe 填满内容区。
- **安全**:iframe 的 `sandbox` 属性恒为 `allow-scripts`(不含 `allow-same-origin`/`allow-popups`)——core 画死、不可经 view_state 放宽。
- 源码态:textarea 显 HTML 源(等宽),编辑后 blur 写回(CAS);切回预览前先提交。
- 预览/源码 toggle 切换正常;外部改 HTML 不覆盖源码态未提交编辑。
- 0 console error。

## 门禁

`editor_manifest_check`:html 条目齐(id/class/status/rootSelector/needs + functionalZones selector 在 CSS + fixture 存在 + builtinInteractions 非空 + 子模块三文档)。`editor_frame_check`:走 EditorFrame、无裸根壳、无 `toolbar=` 弃用槽。`css_contract`:无未定义 class。
