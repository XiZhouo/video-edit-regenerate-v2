# Pipeline — 长任务多步协调

## 1. 这是什么

Pipeline = **协调多个 Stage 顺序执行的运行流**。Stage 是长任务的一步（带 progress + cancel + retry）。

业务白话视角：业务同事在 ZE-seek-find-object-generator 点"AI 自动生成 10 张关卡背景"，UI 不能等 10 张图全部生成（5 分钟）才返回；得：
1. 立即返回任务 ID
2. 期间显示进度（"3/10 完成"）
3. 用户能中途按"取消"
4. 失败的 1 张能单独重试（不重跑成功的 9 张）

这套能力**Command 给不了**——Command 是一次同步原子动作。Pipeline 专门做这事。

> **慢任务的唯一合法入口 = stage**:任何会调 `llm_call`/`image_call`(或其它慢/外部调用)的动作,
> 必须 `kind="stage"` 走本管线,**不得**塞进 `kind="native"` 的 `execute` 假装瞬时(否则无加载态 /
> footer 无管线 / 不可暂停·重试)。此约束由门禁 `scripts/command_async_check.py` 强制,详见
> [01-command/SPEC.md](../01-command/SPEC.md) "kind 三态" 段的硬规则。

### Pipeline vs Command

| 维度 | Command (含 kind=compose) | Pipeline |
|---|---|---|
| 执行模式 | 同步、一次返回 Operation | 异步、多 stage、可分次返回结果 |
| 进度可见 | ❌ 过程不可见 | ✅ progress callback 每 stage / 每 item 报告 |
| 取消 | ❌（必须跑完）| ✅ checkpoint() 协议；ctx.cancel() 中断 |
| 单 stage 失败 | 整个失败 | 单 stage 失败可单独重试，其他 stage 不影响 |
| 中间状态 | 无 | StageResult 落到 PipelineRun，可查可 retry |
| 调 ZE 数据变更 | 自己产生 Operation | stage handler 内部走 command 产生 Operation |

**关键**：Pipeline 不替代 Command；它是**协调多 Command 的执行流层**。Stage handler 内部跑业务逻辑（计算 / 调外部 API / 调 ZE command 做数据变更），完成后产生 StageResult。

## 2. Stage vs Command 边界

| | Stage | Command |
|---|---|---|
| 用途 | 长任务的一步 | 一次原子数据变更 |
| 时间 | 秒级 ~ 分钟级 | 毫秒级（同步） |
| 副作用 | 任意（计算 / API / 调 command） | 改 ZE entity 数据 |
| 产物 | StageResult（output / error）| Operation（patches / reverse_patches）落 OpLog |
| 进度 | progress callback | 不报进度（瞬间完成）|
| 取消 | checkpoint() 协议 | 不支持取消 |

**典型组合**：

```
Stage "extract_keywords":      调外部 NLP API 抽关键词，不动 ZE 数据 → 只产 StageResult
Stage "create_entities":        在 stage handler 里 dispatch command "entry.batch-create"
                               → 产生 Operation 落 OpLog（命令通道）+ StageResult（pipeline 通道）
Stage "build_index":           调外部索引服务，不动 ZE 数据 → 只产 StageResult
```

Pipeline = `[extract_keywords, create_entities, build_index]` 三步顺序跑。

## 3. StageSpec / StageContext / StageResult

### StageSpec — stage 声明

```python
@dataclass(frozen=True)
class StageSpec:
    id: str                                                          # "ai-generate-bg"
    label: str                                                       # 人看的名字
    run: Callable[[StageContext, dict], dict]                        # 业务逻辑
    cancel_handler: Callable[[StageContext], None] | None = None     # 取消时清理 (释放资源等)
    retry_safe: bool = True                                          # 重试是否安全 (false 表示重试可能产生重复副作用)
    produces: tuple[str, ...] = ()                                    # 声明产物(done/manifest 摘要)
    resource_scopes: tuple[str, ...] = ()                              # 准入资源域
    resource_costs: dict[str, int] = field(default_factory=dict)        # 每个资源域的正整数成本
    command_id: str = ""                                              # 用户可见 command cell;空=stage.id
    input_ports: tuple[dict, ...] = ()                                # step 输入快照/展示端口
    output_ports: tuple[dict, ...] = ()                               # step 输出快照/展示端口
    interrupt_safe: bool = False                                      # interrupt 后是否可从 stage 边界继续
    pause_after: bool = False                                         # 完成本 stage 后停在边界
```

**run 函数签名**：`(ctx: StageContext, args: dict) -> dict`
- `ctx` — 运行时上下文（progress / cancel token）
- `args` — caller 传入的参数
- 返回 — output dict（落到 StageResult.output）
- 抛 ZeError / CancelledError — 进入对应 status 分支

`frozen=True` 不是全部保证。构造时会把全部执行字段放入内部快照，端口和资源对象只接受
精确、有限 JSON 类型并递归冻结；`StagePlan` 再从该快照克隆每个 `StageSpec`，不会保留 caller
原对象作为运行权威。执行、重试安全检查、断点/资源判断与 `StagePlan.to_meta()` 因而消费同一份
批准意图；改原始 dict/list、公开属性、公开 `__dict__` 的 detached view、base 容器 mutator 或
序列化返回值，都不能让正常框架读取造成“描述 A、执行 B”。这不是 hostile Python sandbox：能够
直接改真实 private storage 的同进程代码，以及可变 handler/code identity，仍属于 §11 的独立治理，
必须在后续 sealed factory、grant 与 Supervisor 消费前继续绑定。

### StageContext — 运行时上下文

```python
@dataclass
class StageContext:
    task_id: str                       # 这次跑的唯一 ID
    stage_id: str
    trace_id: str
    progress: float = 0.0              # 0.0 ~ 1.0
    label: str = ""
    eta_sec: float = 0.0
    _cancelled: threading.Event
    _progress_cb: Callable | None      # caller 传的 progress 监听

    def set_progress(ratio, label="", eta_hint=0.0) -> None
    def log(level, message, **extra) -> None              # 执行日志;无监听时 no-op
    def checkpoint() -> None                                # 检 cancel/interrupt;触发抛对应错误
    def interrupt_checkpoint() -> None                      # Jupyter 式 interrupt 检查点
    @property is_cancelled -> bool
    @property is_interrupted -> bool
    def cancel() -> None                                    # 外部调; 标记取消
    def interrupt() -> None                                 # 外部调; 标记中断
```

**checkpoint() 是 cancel 的唯一感知点**：

```python
def my_run(ctx, args):
    for i, item in enumerate(items):
        ctx.checkpoint()                  # 每轮检 cancel; 命中抛 CancelledError
        process(item)
        ctx.set_progress((i+1) / len(items), label=f"已处理 {i+1}/{len(items)}")
        ctx.log("info", "item processed", item_id=item["id"])
    return {"items_processed": len(items)}
```

**log() 是运行可视化日志入口**:
- `ctx.log(level, message, **extra)` 只写执行日志,不改 Document、不进 OpLog。
- `PipelineSupervisor` 把它映射到 `pipeline_status.runs[].log_events[]`,同时派生旧
  `log_lines[]` 以保持旧 UI/CLI 兼容。
- `extra` 可带 `code` / `artifact_path` / 业务调试字段;保留字段
  `source/run_id/trace_id/item_id/stage_id/level/message/ts` 由 supervisor 统一填。

caller 在另一个线程 / async context 里调 `runner.cancel(task_id)`：

```python
# 主线程
result_future = executor.submit(runner.run, spec, args, on_progress=cb)

# 另一线程 / UI 触发
runner.cancel(task_id)                    # ctx._cancelled.set()
# stage handler 下次 checkpoint() 时抛 CancelledError → result.status = "cancelled"
```

### StageResult — 完成结果

```python
@dataclass
class StageResult:
    task_id: str
    stage_id: str
    status: str                         # ok | cancelled | interrupted | error
    output: dict = {}                   # stage.run 的返回值
    error: dict | None = None           # 失败时的 error_contract dict
    started_at: float = 0.0
    ended_at: float = 0.0
```

## 4. StageRunner — 跑单 stage

```python
class StageRunner:
    def __init__()                                          # 内部维护 task_id → ctx 映射
    def run(spec, args, *, task_id=None, on_progress=None, trace_id=None) -> StageResult
    def cancel(task_id) -> bool                              # 返回是否找到并标记
    def active() -> list[str]                                # 列在跑的 task_id
```

**run 流程**：
1. 造 StageContext（task_id / stage_id / trace_id / progress_cb）
2. 注册到 `_tasks[task_id]`
3. try:
   - `output = spec.run(ctx, args)`
   - `result.status = "ok"`, `output = output`
   - `ctx.set_progress(1.0, label="done")`
4. except CancelledError:
   - `result.status = "cancelled"`
   - 调 `spec.cancel_handler(ctx)` 清理（异常吞掉，不影响 status）
5. except InterruptedError:
   - `result.status = "interrupted"`
   - 不调用 cancel_handler;run 可 resume
6. except ZeError:
   - `result.status = "error"`, `error = {code, message, context}`
7. except Exception:
   - `result.status = "error"`, `error = {code: "stage_error", message, trace_id}`
8. finally:
   - 从 `_tasks` 摘掉
   - `result.ended_at = time.time()`
9. return result

**关键**：StageRunner 是同步——`run(...)` 阻塞直到 stage 完成。caller 想异步用，自己包 thread / asyncio.to_thread。

## 4.1 Pipeline notebook ports / snapshots

Pipeline editor 的 cell 来自 `StageSpec`/`StagePlan` 静态声明 + `PipelineRun.step_states` 运行态。

**端口合同**：`StageSpec` 的真实运行时合同是一个 exact JSON extension bag；框架只把非空
`id` 解释为稳定端口身份，其余键值不做有损归一化，并完整进入 manifest/authority 摘要。
这是为了同时容纳 core 的 `{id}`、notebook UI 的展示字段，以及真实编辑器已经使用的
`kind=payload / index / type / audio / video / data / entity` 等方言。`StagePlan.to_meta()` 是
唯一完整序列化入口，必须同时带出 `resource_costs` 和 `pause_after`，并复制全部 port bag。

下面只是当前 notebook renderer 能直接理解的一组**约定字段**，不是 authority codec 的闭集：

```ts
type PipelinePort = {
  id: string
  label?: string
  kind: 'text'|'number'|'boolean'|'json'|'entity'|'asset'|'image'|'mask'|'bbox'|'table'|'metric'|'patch'|'log'|'artifact'
  source: 'args'|'work_item_payload'|'entity_field'|'config'|'stage_output'|'artifact'|'operation_patch'
  field?: string
  path?: string
  entityType?: string
  preview?: 'inline'|'thumbnail'|'full'|'json'|'kv'|'diff'|'none'
  required?: boolean
  secret?: boolean
}
```

未知扩展键可以被不认识它的 renderer 忽略，但不能被 serializer/codec 删除或改写；没有
exact string `id`、重复端口 id、非 JSON 值或超资源预算仍然 fail closed。

**快照路径**:

```text
.pipelines/<run_id>/status/<item_id>.stage.<stage_id>.input.json
.pipelines/<run_id>/status/<item_id>.stage.<stage_id>.result.json
```

快照只保存 bounded JSON:大图/视频/文件只保存 asset ref 或 artifact path,不保存 base64。输出过大时写 `{truncated:true}`。

**Step 状态**:

`PipelineRun.step_states[]` 是 notebook editor 的细节数据源,包含
`item_id/entity/stage_id/command_id/phase/progress/started_at/ended_at/input_snapshot/output_snapshot/operation_ids/error`。
Footer 可以继续只读 run 级摘要和 `active_items`,不用理解 step 细节。

## 4.2 Lifecycle: pause / interrupt / cancel / retry / run-from-step

- `pipeline.pause`:不打断当前 stage,只在 item/stage 边界暂停。
- `pipeline.interrupt`:请求当前 stage 在下次 `ctx.checkpoint()` 处抛 `InterruptedError`;run 进入 `interrupted`,已完成 step 保持 done,下游 pending,可 `pipeline.resume`。
- `pipeline.cancel`:终止整个 run,不可 resume。
- `pipeline.resume`:paused 或 interrupted run 从未完成 step 继续;已完成 step 的 result snapshot 不重算。
- `pipeline.retry_failed`:只重跑 error item/step,不重跑 done step。
- `pipeline.rerun_step`:重跑指定 step,并把该 step 和下游标 `stale`。
- `pipeline.run_from_step`:从指定 step 跑到最后,上游输出保持不变,指定 step 及下游先标 `stale`。

所有会「再执行」 stage 的入口由 Supervisor 统一强制 `StageSpec.retry_safe`：

- 自动 `_maybe_retry` 只要本次将执行的 stage suffix 含显式
  `retry_safe=False`，就不清 marker、不入队，item 以
  `pipeline_replay_unsafe` 结束并保留原错作为 `cause`。
- `retry_failed` / `rerun_step` / `run_from_step` 在改 marker、标 stale 或起
  worker 之前做同一检查；不安全时抛结构化
  `ZeError(code="pipeline_replay_unsafe")`。
- 进程恢复时，「无 done/error marker」无法证明 stage 从未开始，
  因此 `recover -> resume` 把它当潜在重放检查。中断后重启当前
  stage 同理。普通 breakpoint/pause 停在 stage 边界时，下一 stage
  尚未执行，其首次执行不受该闸门阻断。
- 检查是精确 suffix：例如从 B 开始只看 B..end，已完成的 A
  即使 `retry_safe=False` 也不会误拦。

Canonical OpLog authority 的错误不按错误文案猜测重试语义，而只消费
`HistoryAuthorityError.outcome_unknown` 这一 typed fact：

- `outcome_unknown=true` 表示 Document/OpLog 的写入结果需要对账。Supervisor 将它投影成
  耐久 `commit_repair_required` item marker，保留 durability phase 与 primitive commit facts，
  并令自动 retry、手动 retry/rerun/run-from、重启 recover 后的 replay 全部 fail closed；
- `outcome_unknown=false` 表示 producer 前的 authority mismatch。当前 Context 不自动或手动
  重放，而是写 `history_authority_mismatch + reload_required=true`。只有新 Context 完成
  `recover()` 后，用户显式 retry 才得到一次进程内许可，且仍须通过 `StageSpec.retry_safe`
  suffix 检查；recover 本身绝不启动 worker；
- `_commit_ok`、`_commit_stage` 是正常 typed 投影边界，worker 还有同一投影的最终 backstop，
  保证未来新增的 history consumer 也不能让 daemon thread 无 marker 退出。

**冷恢复执行身份（manifest × durable Document）**：`run.json` 只是不可信执行声明，
resolver 判定结构 canonical 仍不足以证明它对应原输入。需要耐久 provenance 的 WorkItem 必须：

1. 把不可变输入/adapter/配置同时写入 `payload.seed_data`；
2. 用非空、无重复的 `payload.recovery_identity_fields: string[]` 列出其中要绑定的顶层字段；
3. 在 mod resolver 的 payload 闭集中要求该列表精确等于业务合同，不能让磁盘声明自行删减保护范围。

Supervisor 在初次启动（含复用既有 entity）和冷恢复时，把每个声明字段的 seed 值与当前
Document entity 做类型敏感的 canonical JSON 比较。缺 entity、缺字段、NaN/非 JSON 或任一不等均以
`pipeline_recovery_identity_mismatch` 拒绝；冷恢复在修复任何 marker、登记 run 或启动 worker 前停止。
`WorkItem` 只接受精确 JSON 值：拒绝自定义对象、行为型容器、NaN/Infinity、循环与非字符串 key。
公开 payload 是递归只读兼容视图，真正的执行/序列化权威是一份构造时生成的 canonical UTF-8 JSON
bytes；`to_dict()`/stage 每次从它解出 detached object。`WorkList` 还会深克隆 item，并分别冻结 run、
name、items 的 canonical identity，使公开字段与 manifest 序列化共用同一事实。因而 caller 原始容器、
公开嵌套 list/dict、公开字段的 `object.__setattr__`、公开 `__dict__` detached view、deepcopy/pickle 或
序列化返回值都不能删减保护字段、改写 seed 或换绑 entity；真实 private storage 的敌对同进程反射不在
本合同内。反序列化不会用 `dict()/tuple()/or []` 吞掉错形：payload/artifacts/items 必须分别是 exact
dict/list/list，item 也必须是 exact dict，否则按损坏 manifest 拒绝。使用点仍会复验完整声明，任何内部
快照异常都按 mismatch 停止，而不是依赖 `all([])` 放行。
这条合同只把 manifest 绑定到框架已信任的耐久 Document，不是 manifest 自签，也不把 Document
升级为防磁盘篡改账本。旧 WorkItem 不声明该字段时维持原兼容行为，但不得宣称已有执行身份绑定。

恢复拒绝不是静默 `None`：安全 run 目录中的 JSON/shape 错误、目录与 manifest `run_id` 不等、
resolver 不认识、或执行身份不匹配，都会在 `PipelineSupervisor.snapshot()` 中派生一条
`status='error' / recovery_required=true / recovery_error.code=<固定码>` 的只读 run 视图。
该视图所有 lifecycle 按钮关闭、`failed_count=0`（避免 UI 误给 retry），不回显异常 message/path/stack；
manifest 仍在，所以进程重启会再次得出同一 repair 信号。它不会相信坏 manifest 的 entity target 去
污染任意实体状态；业务 repair/quarantine 命令仍是后续独立能力。
每次 `recover()` 都按当次安全目录扫描重算 failure projection：已注册的 live run 不再读取同名磁盘
manifest，已修复或移除的目录会清掉旧 failure，任何 public run id 最多出现一条快照。

物理读取同样 fail-closed：run 目录必须是非 symlink、当前 uid 持有的真实目录；`run.json` 通过
该目录 fd 以 `O_NOFOLLOW|O_NONBLOCK` 打开，必须是当前 uid、single-link、非空且不超过 8 MiB 的
普通文件，并在读取前后保持 inode/size/mtime 不变。manifest `run_id` 必须显式存在且逐字等于目录名，
不得用 `setdefault` 猜回。symlink directory 不属于可信 run，symlink/hardlink/FIFO/变化文件统一拒绝。

**刻意边界(禁打补丁)**:framework 只内置以上 7 个 lifecycle 命令(pipeline_commands.py)。
`pipeline.start` **永属 mod**——启动含 WorkList/StagePlan 构造,是业务语义;mod start 命令在 producer 内调
`supervisor.prepare_start(..., pipeline_id=, start_command_id=)`，并返回同一笔 `CommandMutation`；
直接 `supervisor.start()` 会在 HTTP 已打开的 CommitCoordinator 内形成嵌套提交，属于门禁违规。
把 `start_command_id` 传实后,管线编辑器
操作台(StartConsole)即可经 `view_state.startCommandId` 关联(见 03-editor SPEC §3.3 pipeline)。
RunPhase **无 queued 相位**(pending 即排队,显示层经 taskStages.RUN_STATUS_ALIAS 映射,显式测试钉死)。

进度与计数是两种事实：`done/total`、stage summary 可以在没有工作量测量时继续显示；
Supervisor 只有收到 `ctx.set_progress()` 才发布数值。只要任一未完成 item/stage 没有有限真值，
item/run 总进度就是 `None`，前端必须展示 indeterminate，不能用 0、30 或等分 stage 数补齐。
全部完成（含恢复得到的耐久完成证据）才固定为 `1.0`。

## 4.2.1 Durable authority（core 3.40：dormant delegated check 已接通，仍无 grant）

Pipeline 能恢复“状态”不代表恢复了“写权限”。manifest、resolver、marker 和目录最多说明
本地曾经有一次运行；真正的权限根必须来自 canonical OpLog 中一条受业务命令授权的 Start，
并在每次进程启动时从完整 fresh history 重新证明。

3.37 的 `ze.pipeline-start.v1 / control.v1` 只建立了带资源预算的 Operation carrier。当时
caller 还可以在没有 sealed factory 的情况下写入任意 v1 dict，所以 v1 **永久不解释为权限**，
升级代码也不能把旧 metadata 追认为历史授权。

3.38 使用新的 strict v2 合同，但故意保持 writer 休眠：

1. `pipeline_authority_codec.py` 只负责 owned、类型精确的 Start/Control 解析。Start 同时绑定
   document/run/pipeline、真实 root command、原始 WorkList、由 `StagePlan.to_meta()` 产生的完整
   stage 列表、placeholder 与连续初始 Operation 身份、执行参数、root command digest、带 canonical
   entity schema digest 的 3.36 mutation policy、实际 worker Actor/dispatch source 和与 WorkList
   完全相同的 delegated target 集合。动态创建不伪装成 wildcard target，而是单列
   `delegation.creation_scopes`；每项只允许一个明确 entity type、排序去重的字段、1..4,096 的
   per-operation/per-run 上限（前者不得大于后者，所有 scope 的 per-run 合计也不得超过 4,096），
   root policy 必须授权该类型的 `create` 与全部所列字段。document policy 的类型范围必须精确等于
   target type 与 creation-scope type 的并集。Control 绑定 Start Operation/self-hash、action 参数、前后
   generation 与 expected state。descriptor 摘要覆盖这些全部事实，不留“文档写一份、代码另猜一份”的口子。
2. codec 与 v1 carrier 使用同一 legacy JSON byte 算法，在 `json.loads`、行为型 deepcopy 或完整对象分配前
   先扫 byte/depth/node/text 预算；Start 上限为 4 MiB/32 层/100,000 节点，v2 Control 为
   **2,179,072 bytes/12 层/4,123 节点**，从而与 4,096 个合法 item id 的合同一致。Frozen 重新物化
   先验长度，再验摘要与类型精确投影。`False/0`、`1/1.0`、
   行为型容器、环、未知键、空 `retry_failed`、清单外 target 与 policy/schema 漂移都拒绝。
3. `pipeline_authority_reducer.py` 只接 detached 的完整 OpLog rows + exact head。它重新验证全链、
   唯一 Start、连续初始闭包、同一业务 actor/source、每个 placeholder 的 exact
   `entity.add(create↔delete)` snapshot/position、Start self-hash 和全部 Control 的命令、scope、
   generation、状态迁移及 terminal/reconcile 语义。`set_breakpoint(halt=true)` 只把 stage 加进按 id
   排序的 active set，不假装运行已经 paused；`halt=false` 只移除该 stage，不自动 resume，并把
   `reconciliation_pending` 置为 sticky，因为它扩大了后续执行可能性。后来的 pause/interrupt 不能抹掉
   这份外部效果不确定性；terminal tombstone 才清空 active set 和 reconciliation。输出只有后续 grant
   所需的有界事实，不返回完整 descriptor，也不读取 Document、manifest、resolver、marker 或 Supervisor。

3.39 在同一批完成 Batch 3，但没有越过 grant 边界：

1. 最外层真实业务 command handler 获得唯一 opaque root snapshot；private factory 只能从这份仍活着、仍匹配注册摘要与 mutation-policy attestation 的 root 取得 actor/source/document/policy，并先预留 id 再构造 Start/Control。generic `Operation/new_operation` 仍拒绝 v2；exact seal 不能复制、pickle、replace、跨 fork、跨 store、并发复用或在安全失败以外重新签发。
2. `prepare_start` 对受保护 Document 禁止 caller-supplied Start。Start 必须是本次 Operation tuple 第一项，descriptor 的 `initial_operation_ids` 必须逐项同序等于整个实际 tuple；root actor/source/document 同时覆盖 Start 与全部新 placeholder。上游 item/shot producer 已提供稳定 pipeline id 并证明真实业务 command→CommitCoordinator 的 legacy 兼容链；因为默认 Document 尚无 mutation policy，它们不算 v2 消费。v2 完整纵向证据来自同一生产代码路径的 protected registered-command integration fixture。
3. CommitCoordinator 在 store 前把 fresh 旧 rows 与本次 exact tuple 合成 prospective complete history，再运行 3.38 reducer。v2 的实际 OpLog append 还要逐条消费与 exact seal、permit、OpLog、顺序绑定的一次性 writer authorization；raw/unbound writer 即使拿到相似对象也在 I/O 前拒绝。任何不能证明双 `NOT_DURABLE` 的 receipt 会先撤销该 run 的旧 attestation，防止 dirty/indeterminate Control 后旧状态继续被信任。
4. 只有 receipt committed 且 Document/OpLog 均 durable、history transaction 已最终收口时，coordinator 才从同一 exact FileCommitStore/OpLog 重新读取完整 rows/head，再次 reducer。head、fingerprint、store、permit 或 fresh I/O 任一不符都返回 typed replay-unsafe error并保持零 attestation；event build/publish 在这之后，失败不撤销已成立的 durable proof，也不执行 deferred action。HTTP/MCP/CLI/confirmation/idempotency 共用 `outcome_unknown` typed fact，不按 message 猜；post-write HTTP 固定为 repair 语义 409，preflight unavailable 才可为 503。
5. 成功结果是 private per-store/run attestation：不可复制/序列化、fork 后失效、强持有到下一次可能改变该 run 的耐久尝试，并从不进入 receipt、event、SDK 或 worker。它不是 reducer public projection，也不是 grant。

3.40 增加 Batch 4 的 dormant delegated mutation seam，把“系统 worker 请求”收窄成一张只可校验一次、永不可执行的进程内 request。它的意义是先证明“如果未来有 grant，这一次写请求仍然没有超出原业务命令”，不是提前造一个可用 worker permit。

**prepare 与 consume 都重新从运行现实证明，不信上一次检查结果：**

1. prepare 先要求 exact `FileCommitStore`、exact `Document`、exact sealed `CommandRegistry` 和属于同一 store/document 的 active Batch 3 attestation。它通过 canonical history verifier 重验 Document、OpLog、UndoController、store、epoch/pid 与 fresh head 是同一张 active runtime graph，然后用当前完整 OpLog rows/head 再跑 pure reducer。
2. consume 先要求原 request、attestation、store、Document、registry 和实际 worker identity 仍是原精确对象/值，然后再跑一次同样的 canonical history verify + complete-history fresh reduce。因此 prepare 后的 head 漂移、authority freeze、换 store/Document/registry/controller 或耐久 Control 变化，都会在 consume 时 fail closed。
3. 两次都要求当前 reduction 与 attested Start 的静态授权事实一致：document/run/pipeline/Start 身份、descriptor digest、root digest/policy digest、worker、WorkList `item→(entity_type, entity_id)`、stage set、placeholder、delegated target 和 creation scope 都不能悄悄改变。terminal 或 `reconcile_required` 直接拒绝。

**单次请求的实际范围也会同时被五层收窄：**

1. item 必须在耐久 WorkList 中精确绑定这一 entity，stage 必须在 Start 批准的 stage set 内；不允许拿合法 item 去改另一个合法 target。
2. 请求的字段路径必须排序、唯一且合法；dot-path 用第一个顶层 root 对授权，但保留完整路径给未来真实 patch audit。field root 和 lifecycle 既必须是 Start target 的子集，又必须仍被当前 Document mutation policy 授权给原 root command。
3. 目标必须还是同一个非 read-only `Entity` 对象，type/id 不变、version 精确等于 prepare 声明。Document 当前的全局 mutation-policy cardinality 也必须仍然合法；目标消失不会被当成可动态 create。
4. prepare 与 consume 都必须收到 actual `Actor` 与 dispatch source，它们必须逐字等于 Start 中的 worker actor/source，且 Actor 自身 source 与请求 source 一致。不能只拿 Start 的声明跟它自己比较后就放行当前 worker。
5. Command 叶子的 private revalidation 每次都重验原 command 仍是当前 registry 中的 business mutation authority，registration digest 与 Start root digest 一致，再从当前 schema/policy 投影 document policy 并比对 Start digest。这个 helper 只返回 detached policy projection，不调 command handler、不 dispatch、不建立 command authority scope。Start root 或本次 primary Operation id 以 `entity.*` 开头都固定拒绝，通用 CRUD 不能升格为授权根。

**request 生命周期：**

- 只能由 framework issuer 构造；内部账本绑定 attestation/store/Document/registry/Entity 对象身份与全部标量事实，发行后任一事实改写都使整张 request 失效；
- 不可 copy、deepcopy 或 pickle，fork child 会更换进程密钥并清空账本；不论 consume 成功还是拒绝，这张 request 都已消耗，不允许 retry/reuse；
- 公开属性 `execution_authority` 恒为 `false`。consume 成功仅代表“当下可以证明这张请求的范围没有越权”，不是允许它实际写入。

所以 core 3.40 仍然没有受保护 worker execution：这个 seam 会读取完整历史中 reducer 归约出的 Control facts，只做 stale/terminal/reconcile-required 的 deny-only 检查；它不 mutate Document、不 dispatch command、不签发/注册 grant、不创建 worker、`DeferredAction`、manifest 或 run，也不签发或执行/应用 v2 Control lifecycle，不会从 legacy lifecycle 猜权。动态 creation 需要 Batch 5 run/grant registry 后才能校验 per-operation/per-run 额度与竞态；hot/cold grant lifecycle、cancel/discard 先撤权、manifest authority cache 和真实 worker 消费同样全在 Batch 5。未来真实执行时，还必须在同一 CommitCoordinator session 内跑 3.36 的 actual-delta 与 reversible Operation coverage；Batch 4 的 policy projection 不能取代这一层。

本版 v2 完整纵向链因此仍只到 sealed **Start** 的 durable attestation + 一次 dormant request 校验：protected Start 提交后没有 DeferredAction、manifest、run 注册或 worker，只有 unprotected/legacy Start 继续原热运行兼容行为。Control factory、writer/reducer 与故障测试已经成立，但现有 `pipeline.pause/resume/cancel/retry/...` 仍产生 legacy lifecycle Operation；它们尚未消费 v2 Control，也不能在 Batch 5 被事后猜成 Control。Batch 5 接 control reduce、manifest authority cache 与 grant registry 时，必须把缺 v2 Control 的过渡 run 判为 fail-closed/recovery-required 或经显式权威迁移，不能信 marker/resolver 补权。

## 4.3 requires_runtime 约定(外部运行时缺位,COOKBOOK R4)

mod start 命令检测到外部运行时缺位(网关未配/服务未起)时,**不假装成功也不裸抛**,两条通道:

- **结构化正道(推荐)**:返回空 patches 的 `kind='stage'` Operation,带
  `error={"code": "requires_runtime", "message": …, "missing": [缺位项…], "hint": …}`——
  op 入 OpLog(留「曾尝试启动但缺 runtime」审计),`CommandResult.op.error` 原样到前端,
  管线操作台原地渲缺位块(标题 + missing 逐行 + hint)。
- **降级通道**:`raise ZeError("requires_runtime", …)` → footer 错误条 toast(可接受,但无原地结构化展示)。

结构化展示**必须**走 op.error 通道——禁各 mod 自造第三种形态。

## 4.4 Artifact completion receipt（W05 文件兼容层）

最终 artifact 文件存在不等于 pipeline 完成。Stage 的 `_artifacts` 参数指向本次
不可复用 UUID attempt 目录；`_final_artifacts` 只用于读取既有最终文件。完成顺序固定为：

1. stage 在 attempt 路径写原始输出；失败/取消时保留供诊断，不触碰最终路径；
2. Document mutation 返回真实 committed 后，写结构化 committed checkpoint；
3. promote 前再次核对 staging 的 sha256/size。Attempt 同时记住开始时 final hash；
   目标仍等于该基线时才 CAS replace，目标已等于新 hash 视为幂等，其余异 hash
   抛 `artifact_target_conflict`。因此显式 rerun 可替换旧版，并发胜者/篡改不会被覆盖；
4. 文件和目录 fsync 后，原子写 done receipt。目录 fsync 只对明确不支持的 errno
   降级；EIO 等真实错误进 durability-unknown/repair。

receipt 固定校验 schema/version、run/item/stage、`commit_kind`、`commit_id`、
`operation_ids`，以及 WorkItem 白名单内每个 artifact 的相对路径、sha256、size。
`commit_kind=document` 表示真实 Document commit；纯文件 stage 必须明确写
`commit_kind=artifact_only` 且 `operation_ids=[]`，不得冒充 CommitReceipt。单 stage
receipt 必须覆盖全部 artifact；多 stage 只有在所有连续 stage receipt 有效、且它们的
artifact union 覆盖 WorkItem 全部声明路径时才是 done。裸文件且无任何 completion 证据时仍是
pending；但旧文本 `ok` marker、空/半/损坏 receipt、checksum 不符都说明“存在一个无法证明的
completion 意图”，必须 fail closed 为 repair-required，不得当成可重放 pending。

扫描仍核对真实 SHA-256，但不能在每次 SSE/footer snapshot 都重读大视频。实现使用线程安全
有上限的 LRU fingerprint cache，key 包含 resolved path、dev、inode、size、mtime_ns、
ctime_ns；任一 stat 变化都会重读。hash 前后 stat 不一致的结果不缓存，并重新读取一次；
持续变化则本轮 fail closed。它只消除“同一未变化文件”的重复 I/O，不把校验降级成 size 判断。

恢复时，有 committed checkpoint 可幂等补 promote/done。当前仍有一个刻意
fail-closed 的小窗口：业务 commit 已返回、checkpoint 尚未落盘即进程崩溃。此时框架
不能从文件侧证明 completion，会保持 pending；恢复后的 `replay_guard` 对
`retry_safe=False` stage 阻止重放。W06 必须用 durable action journal/outbox 把
commit 与 completion intent 纳入同一事务，关闭这个窗口。当前协议只是单进程文件
存储兼容层，不承诺多进程 scheduler。

每个 WorkItem 从读取当前状态、执行 stage、提交 Document、promote artifact 到写 completion
receipt，都持有同一个 project lifecycle activity lease。replace/delete 先把 state 切到
quiescing，再等待所有共享 lease 退出；`shutdown(join_timeout)` 只要发现任一不合作 worker
仍存活就返回 `False`，调用方必须终止 topology 切换，不能把“已发 cancel”误当作“线程已停”。
新一代项目发布后，旧 worker 即使稍后苏醒，也会在下一次 lease 校验得到
`project_generation_stale`，无法向旧 Context 或新目录继续提交。

## 5. PipelineStep / PipelineRun / PipelineStepResult

### PipelineStep — 一个步骤

```python
@dataclass
class PipelineStep:
    stage: StageSpec
    fan_out: bool = False                # True 时跑 args["items"] 每 item 一次
```

### PipelineRun — 一次执行实例

```python
@dataclass
class PipelineRun:
    id: str
    steps: list[PipelineStepResult] = []
    status: str = "running"              # running | ok | partial | failed | cancelled
```

### PipelineStepResult — 单 step 结果（含 fan_out 多 item）

```python
@dataclass
class PipelineStepResult:
    step_id: str
    fan_out: bool
    results: list[StageResult] = []      # fan_out=False 时只 1 个; True 时每 item 1 个

    def ok_items() -> list[StageResult]
    def failed_items() -> list[StageResult]    # status not in ("ok", "cancelled")
```

## 6. PipelineRunner — 跑多 step + fan_out

```python
class PipelineRunner:
    def __init__(stage_runner=None)                          # 默认 StageRunner()
    def run(steps: list[PipelineStep], args: dict) -> PipelineRun
    def retry_failed(run: PipelineRun, steps, args) -> PipelineRun
```

**run 流程**：

```
For each step in steps:
    if step.fan_out:
        items = args["items"]
        for i, item in enumerate(items):
            r = stage_runner.run(step.stage, {"item": item, "_index": i, **args.get("step_args", {})})
            step_result.results.append(r)
    else:
        r = stage_runner.run(step.stage, args)
        step_result.results.append(r)

    run.steps.append(step_result)

    if step_result has failed_items and not args.get("continue_on_failure"):
        run.status = "partial" if has ok_items else "failed"
        return run        # 失败短路

run.status = "ok" if all clean else "partial"
return run
```

**`continue_on_failure` 标志**：caller 在 args 里传 `True` → 单 stage 失败也继续后续 step（场景：批处理容忍部分失败）。

### 7. fan_out 详解

**核心场景**：AI 批量生成。

```python
generate_bg_stage = StageSpec(
    id="ai-generate-bg",
    label="AI 生成关卡背景",
    run=lambda ctx, args: ai_call(args["item"]),     # item = 一张关卡描述
)

pipeline_runner.run(
    steps=[PipelineStep(stage=generate_bg_stage, fan_out=True)],
    args={"items": [scene1, scene2, ..., scene10]},  # 10 张
)
```

执行：
- 顺序对 items 跑 10 次 `stage.run(ctx, {"item": scene_i, "_index": i})`
- 失败的 item 仍记到 `step_result.results[i]`（status="error" / "cancelled"）
- ok 的 item 保留在 results；后续 retry 不重跑

**为什么不并发**：本叶子做同步顺序版（v2 β.2 一致）。并发 fan_out 涉及 thread pool / async 调度，留扩展点。如需并发，caller 可在 args 里传 worker count，PipelineRunner 子类覆盖 fan_out 逻辑。

## 8. precise retry — 失败 item 单独重跑

```python
run = runner.run(steps, args)
# 假设 step 0 fan_out=True, 10 items, item[3] 失败

run = runner.retry_failed(run, steps, args)
# 只重跑 item[3]; 其他 9 个 ok 保留
```

**retry 策略**：
- 遍历 run.steps[i] 跟 steps[i] 配对
- step_result.failed_items() 才重跑
- fan_out=True：找失败的 index，从 args["items"][index] 拿原 item 重跑；ok / cancelled 保留
- fan_out=False：失败就重跑一次
- retry 不限次数（caller 决定调几次）
- 重新计算 run.status

**retry_safe**：`StageSpec.retry_safe=False` 表示 stage 副作用不能重复
（比如发送邮件）。活管线的 Supervisor 在所有重放入口强制该声明，
不再依赖 caller 或 UI 记得禁用按钮。字段默认仍为 `True`：这是现有纯本地
stage 和旧 resolver 的兼容承诺，不代表框架能自动推断副作用安全。
新增的外部/不幂等 stage 必须显式填 `False`。底层同步
`PipelineRunner` 仍是无持久化的计算原语，它不代替 Supervisor 的这道强制闸门。

## 9. cancel 协议详解

**两端协作**：

- **stage handler 端**：业务方写 stage.run 时**周期性**调 `ctx.checkpoint()`。粒度由业务方控制：每 item / 每 N 秒 / 每 sub-step。
- **caller 端**：`runner.cancel(task_id)` 把 `ctx._cancelled.set()`；下次 checkpoint 命中抛 CancelledError。

**为什么不强制**：
- 不能在 framework 层"自动检 cancel"——业务逻辑可能在调外部 API（不可中断）
- 业务方知道哪里中断成本低（一次 API 调用之间）；framework 不知道
- 不 checkpoint 的 stage = "跑到底才能停"——业务方明确接受

**cancel_handler**：stage 被 cancel 后调用，做资源清理（关连接 / 删临时文件 / 通知外部撤回）。异常吞掉，不影响 status。

## 10. 红线规则 + 反模式

红线：

- **Stage.run 不能依赖全局可变状态** — 否则 cancel / retry 行为不可预测（AP-P1）
- **fan_out 的 item 之间不能共享 mutable state** — 单 item 失败不应污染别的（AP-P2）
- **不能在 stage.run 里直接 mutate entity / doc** — 必须走 command dispatch（同 AP-C1）
- **PipelineRun 的状态机** — running → ok / partial / failed / cancelled；不能从终态退回 running
- **retry_failed 只重跑失败的** — 不能误重跑 ok 的（AP-P3）

反模式：

- **AP-P1** stage 函数依赖外部 list / dict 等可变变量 → retry 时状态不一致
- **AP-P2** fan_out 多 item 共用 buffer：`results = []; def run(ctx, args): results.append(...)` → 并发或重试错乱；应每次新建 + 通过 output 返回
- **AP-P3** caller 在 retry_failed 后忽略 status，把 "partial" 当 "ok" → 漏报错
- **AP-P4** stage handler 内部直接调 doc.add 改数据 → 不走 command，失审计 / 协作 / undo
- **AP-P5** 在 stage 函数里 catch CancelledError 自己处理 → 应该让它冒出来由 StageRunner 处理
- **AP-P6** progress callback 阻塞 / 抛错 → 影响 stage 执行；callback 必须快速 + 不抛
- **AP-P7** 把不耗时的命令放进 pipeline → Pipeline 是为长任务设计；瞬间完成的应该直接 dispatch command

## 11. 当前实装状态

### ✅ 已实装

| 项 | 说明 |
|---|---|
| `StageSpec` dataclass | id / label / run / cancel_handler / retry_safe |
| `StageContext` | task_id / stage_id / trace_id / progress / label / eta + set_progress / log / checkpoint / is_cancelled / cancel |
| `StageResult` | task_id / stage_id / status / output / error / started_at / ended_at |
| `StageRunner` | run（含 try/except 处理 CancelledError / ZeError / 通用 Exception）+ cancel + active |
| `PipelineStep` | stage + fan_out |
| `PipelineRun` | id / steps / status (running/ok/partial/failed/cancelled) |
| `PipelineStepResult` | step_id / fan_out / results + ok_items / failed_items |
| `PipelineRunner` | run（顺序多 step + fan_out 内部展开 + 失败短路 / continue_on_failure）|
| `PipelineRunner.retry_failed` | 只重跑失败的 item / step；ok / cancelled 保留 |
| Durable Start authority | core 3.39 sealed Start/Control factory、exact append permit、pre-store complete-history closure 与 committed+durable fresh attestation；attestation 仍非 grant |
| Dormant delegated mutation | core 3.40 prepare/consume 双重 fresh reduce，exact runtime graph + item/entity/stage/field/lifecycle/version/cardinality/worker + 当前 root/policy digest 重证；request 恒为 `execution_authority=false` |
| `CancelledError` 异常类 | stage 内部 checkpoint() 抛；StageRunner 兜底捕 |
| ZeError 共享 | 从 entity 模块 import |

### ⏳ 待补（真有跨叶子依赖）

| 项 | 依赖 |
|---|---|
| 并发 fan_out (thread pool / async)| 不必装；caller 用 PipelineRunner 子类自定义 |
| Progress 节流（防 callback 频率太高）| 等 [02-data-flow/01-realtime-sync/](../../02-data-flow/01-realtime-sync/) 启动后接入 broadcast |
| Pause / Resume (中间状态持久化)| 等 [02-data-flow/](../../02-data-flow/) 启动后按需扩展 |
| DAG 执行（非线性依赖）| 顺序步骤够用；DAG 真有业务需求再扩 |
| Stage 自动落 Operation kind="stage"| 业务方在 stage handler 里走 command 即可；自动落容易语义混乱 |
| 跨 skill stage（A skill 调 B skill 的 pipeline）| 等 [04-cross-skill-call/](../04-cross-skill-call/) |
| Protected worker execution | Batch 5 才接 grant registry、v2 Control hot/cold lifecycle、dynamic creation、manifest authority cache 与真实 worker；Batch 4 不实际执行 |

### 原则

- **签名先定**：`StageSpec` / `StageContext` / `PipelineStep` 等数据结构签名固定
- **caller 决定执行环境**：同步用同步；async 用 `asyncio.to_thread` 包；本叶子不假设
- **业务方写 stage handler 时**：必须周期性 `checkpoint()`；副作用走 command dispatch；不依赖全局可变状态


## 长任务三纪律(Grok-Build 借鉴 P1-⑤ Build 侧,2026-07-19)

对应三种真实事故的顺序纪律(思想移植自 grok compaction.rs,机制按 ZE 现实落):

1. **沉淀先于压缩/续跑**:长 pipeline 的每个阶段边界,先把阶段结论写入持久落点
   (run 记录/实体字段/检查点),再进入任何「摘要化/续跑/重试」——防「还没固化的关键
   结论被压丢」。
2. **墙钟兜底**:每个 stage 除 token/步数上限外声明 `wall_clock_budget_sec`(StageSpec
   可选字段,未声明按 supervisor 默认);超时切断重试——这是「token 上限拦不住的推理
   失控」的最后防线。
3. **检查点顺序**:批量写盘批次先 checkpoint 后写盘(合同见同目录 CHECKPOINT.md);
   恢复优先于重跑。

AgentSOP 侧的同构纪律(工作流步骤检查点)在其自身规则文档维护,不在本层重复。
