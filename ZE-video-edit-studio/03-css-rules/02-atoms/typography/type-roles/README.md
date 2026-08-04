# type-roles — 排版角色矩阵(T1,css 19.9.0)

## 怎么用
文本元素挂 `data-ty="<角色>"` 即得整组排版(字号/字重/行高/墨色/字距),不再自拼:

| 角色 | 组合 | 用途 |
|---|---|---|
| `display` | 22/600/tight/primary | 统计大数字、画布展示 |
| `title` | 15/600/tight/primary | 页面、对话框标题 |
| `panel` | 12/600/tight/primary | 面板头标题 |
| `body` | 12/400/normal/primary | 正文 |
| `label` | 12/500/tight/secondary | 控件标签、按钮 |
| `meta` | 10.5/400/snug/secondary | 元数据、辅助说明 |
| `micro` | 9/500/snug/tertiary | 计数、刻度、trace(仅冗余辅助) |
| `caps` | 9/600/snug/tertiary + uppercase + 0.06em | 分组锚点(组头) |
| `num`(修饰,或 `data-num`) | mono + tabular-nums | 一切数值 |

9px 边界规则:`micro`/`caps` 不得承载唯一信息——唯一通道 ≥`meta`(10.5),关键解释 ≥`body`(12)。
来源:ZE前端美学优化规划 T1;闭集门禁随美学批2。
