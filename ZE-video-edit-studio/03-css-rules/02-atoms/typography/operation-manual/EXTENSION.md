# Extension

新增手册表现时，先确认它是所有编辑器都需要的通用阅读语义。若是：

1. 在 sanitizer 的允许标签/属性中加入最小能力；
2. 在本对象中增加以 `.ze-operation-manual .ze-om` 为边界的 token 化样式；
3. 增加安全与保真回归测试；
4. 更新 README/RULES 和 CSS 公共合同。

工具私有布局或配色不得在此扩展，也不得通过手册 fragment 注入。
