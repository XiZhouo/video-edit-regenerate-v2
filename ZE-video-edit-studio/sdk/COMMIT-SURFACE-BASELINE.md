# W00 写入路径提交基线

这份说明只描述测试目标，不改变运行时行为。对应机器可执行矩阵在
`sdk/test_commit_surface_matrix.py`。

## 统一目标

当前有七条合法写路径，都必须满足同一条顺序：

`修改内存 → 持久化 Document → 追加 OpLog → 发布协作事件 → 返回调用方`

HTTP command / confirm 外面还有幂等包装层，因此两条 HTTP 证据进一步要求：

`… → 发布协作事件 → action 返回 → 幂等成功记录持久化 → HTTP wrapper 返回`

测试使用真实 `_run_idempotent` 与真实 `IdempotencyStore`，并在成功记录完成原子替换后记
`idempotency_success`，不是只在说明文字里假设这个边界。

这条顺序表达的是一个最小安全边界：协作者不能先看见尚未持久化的“幽灵变更”；保存失败时也
不能留下貌似已经提交成功的事件。Document 与 OpLog 目前仍是两个独立写入单元，W00 不把它们
误称为数据库事务。

原先的第八条是 MCP confirm。它现在不是写路径，而是必须固定拒绝的安全边界：AI
可以发起待批准操作，但拿不到网页的一次性 approval nonce，也看不到 `ze_confirm` 工具，
即使绕过工具目录直接调用也必须得到 `confirmation_human_required`，且零 mutation。

## 七条写路径 + 一条拒绝边界

| 入口 | 可执行证据 | 当前状态 | 保护目标 |
|---|---|---|---|
| HTTP command | `sdk/test_server_finalize_order.py::test_http_command_commits_before_publish` | 通过并保护 | coordinator 完整提交后才返回，成功幂等记录最后落盘 |
| HTTP confirm | `sdk/test_server_finalize_order.py::test_http_confirm_commits_before_publish` | 通过并保护 | 人工批准保留同一 trace，按 receipt 决定 consume/release/freeze |
| MCP command | `sdk/test_mcp_commit_order.py::test_mcp_command_commits_before_publish` | 通过并保护 | MCP 与其他入口共用 coordinator 和业务幂等键 |
| MCP AI approval | `sdk/test_mcp_commit_order.py::test_mcp_ai_cannot_self_approve_pending_command` | 固定拒绝 | 不广告批准工具、无 nonce、直接绕调仍零 mutation |
| CLI | `scripts/test_ze_command_finalize.py::test_cli_command_commits_before_publish` | 通过并保护 | CLI 复用同一提交边界 |
| Pipeline worker | `01-core/03-commands-operations/03-pipeline/test_supervisor.py::test_pipeline_worker_commits_before_publish` | 通过并保护 | worker 业务提交先于协作发布与 artifact 完成证明 |
| undo | `sdk/test_server_finalize_order.py::test_undo_route_persists_before_appending_no_double_save` | 通过并保护 | 修改 → save → append → publish |
| redo | `sdk/test_server_finalize_order.py::test_redo_route_persists_before_append_before_publish` | 通过并保护 | 修改 → save → append → publish |

## 失败基线

`01-core/03-commands-operations/01-command/test_mutation_failure_baseline.py` 已把曾经的两条
W04 strict xfail 转成永久回归：handler 修改 Document 后抛错，以及修改后返回非 Operation，
都必须完整恢复 Document。

## W00 有意不覆盖的边界

- HTTP 成功账本的先后顺序已经覆盖；“Document 已保存但 OpLog 失败”这类不确定状态目前仍会删除
  幂等记录，W03 必须补成禁止自动重放的 `indeterminate` 语义。
- `live_collab` / `with_save` 的命名与行为修正属于 W02；本基线统一用当前会触发持久化的
  `with_save`，避免在 W00 偷改语义。
- Asset、网络调用等 handler 外部副作用不在 Document 快照内，须在 W04/W05 通过 effect/port
  契约处理。
- 当前文件兼容层仍不宣称多文件 ACID，也不宣称跨进程同步已经完成；这些由 W06 的事务存储承接。
