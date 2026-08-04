# 02-footer — Footer

> 一句话：底部栏区域。

默认显示什么 / 点击展开内容 / 哪些事件该记录到 footer (status / progress / error / trace-id)。

## 本叶子文件

- README.md — 本文件 (导览)
- SPEC.md — 完整规则 + 技术文档 (待用户带着填)
- (代码文件待后续添加)

## 怎么用

Footer 由框架 App.tsx 装配（`FooterShell`），mod 零代码白拿：折叠态 24px 状态条（服务器 / 同步 / 任务数 / 错误计数 / trace-id 短码），点把手展开后是最近 30 条 op 日志（最新在顶）+ 每条活跃 pipeline 的进度/日志/暂停/停止/继续按钮 + 错误列表。

日志展柜的数据怎么来：

- **op 日志** = 后端 OpLog——mod 的命令一经 dispatch 执行就自动入列，不用埋点；
- **pipeline 列表** = 后端 `03-pipeline` 的 runs——mod 用 pipeline 跑长任务，进度与结构化执行日志（`log_events`）自动出现在这里；控制按钮只 dispatch `pipeline.pause / resume / cancel`；
- **错误** = 命令失败自动计数 + 入错误列表（即时可见另有右下角 toast）。

调参走 `FooterConfig`（`skill_name` / `oplog_max` / `expanded_height_default` 等，见 [types.ts](./types.ts)）；Footer 不做业务数据 mutation——业务按钮放 editor / Header。完整规则见 [SPEC.md](./SPEC.md)。
