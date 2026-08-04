# Audit Gates — 跨叶子校验规则注册表

> 每条规则一个 gate 编号，对应 [audit_gates.py](./audit_gates.py) 一个 check 函数。给 CI / 发布前 / 周期巡检跑。

## 1. 命名约定

```
GATE-<LEAF>-<NN>
```

| `LEAF` | 范围 |
|---|---|
| `DOC` | 01-document |
| `ENT` | 02-entity |
| `AST` | 03-asset |
| `OP` | 02-operation |
| `CMD` | 01-command |
| `PIPE` | 03-pipeline |
| `AI` | 05-ai-coworker |
| `CC` | 05-concurrency |
| `RB` | 06-resource-budget |
| `CONF` | 02-conflict-resolution |
| `CAP` | 05-capacity-limit |
| `UNDO` | 04-history-rollback |
| `RT` | 01-realtime-sync |
| `COMMIT` | 07-persistence / 所有生产写入口 |
| `DB` | 07-persistence / SQLite transaction kernel |
| `AUTH` | 04-user-facing/03-authentication |
| `PROTO` | _protocol (本节自身) |
| `TYPE` | 07-type-library |

`NN` 是该叶子内 gate 顺序号（01 起）。

## 2. Gate 注册表

### Document (01-document)

| Gate | 名字 | 检查什么 | 形态 |
|---|---|---|---|
| `GATE-DOC-01` | document_version_conflict | `doc.save(expected_version=N)` 跟当前 `_v` 不等抛 `version_conflict` | runtime |
| `GATE-DOC-02` | document_collections_index_consistent | `document.json` 的 `collections` 索引指向的所有 entity 文件存在 | runtime |
| `GATE-DOC-03` | collaboration_mode_declared | `collaboration_mode` 必显式（`live_collab` / `with_save` / `realtime_checkpoint`），不能省 | static |
| `GATE-DOC-04` | schema_version_required | `schema_version` 字段必填非空 | static |
| `GATE-DOC-05` | save_policy_explicit_and_safe | mod 必须恰好一次字面量声明 `auto_commit` / `checkpoint_commit`；缺失、动态值与 `legacy` 均失败 | static AST |
| `GATE-DOC-06` | read_only_overlay_atomic_identity | 只读 source 晚发生 identity 冲突时不得覆盖 canonical Entity，也不得留下已注册 source 或先前物化的半批实体 | runtime |
| `GATE-DOC-07` | safe_entity_file_store_commit_boundary | hostile UTF-8 identity 只映射到 framed hash；安全单组件 legacy 自动迁移；unsafe symlink 在 commit 前拒绝；rename 后 fsync 失败诚实 UNKNOWN 且版本对齐 | runtime filesystem |
| `GATE-DOC-08` | multiprocess_durable_head_fence | 两个进程先打开同一 head 再同时保存，必须恰好一个提交、一个在任何 generation 写入前 `version_conflict`；跨进程探针确认 `write_generation` 恰好调用一次，最终 head 可完整重开且只含胜者 | runtime multiprocess |
| `GATE-DOC-09` | validated_document_head_authority | `Document.open`、save CAS、metadata reader 与 topology staging rewrite 必须共用唯一 strict parser；完整精确 payload 只归 sealed `DocumentHead`，`DocumentMetadata` 只持各已验证字段的 compact immutable fragment，嵌套值无别名；无 topology authority 的 rewrite 零改动拒绝，错误 root inode 必须由 opened `EntityFileStore` 拒绝。AST 还保证 public open 与 strict legacy importer 共用同一 locked-store Entity loader，scan capability 只有该 importer 一个生产消费者，token/raw primitive 不逃逸定义/唯一 wrapper，并把 topology identity pin 原样传到 store；capability 不被误称为防恶意同进程代码的安全沙箱 | runtime + static AST |
| `GATE-DOC-10` | atomic_document_commit_candidate | 只有 exact capability + exclusive mutation session 能签发一个 opaque、不可复制的 Document commit candidate；它精确绑定进程/线程/Document/session/before/after head，单 session 只能签发一次。SQL 未提交时 rollback 必须原位恢复 Entity identity、业务值、Document version 与 timestamp；session 关闭后旧 candidate 永久失效，private carrier 不进入稳定 SDK | runtime + surface boundary |

### Entity (02-entity)

| Gate | 名字 | 检查什么 | 形态 |
|---|---|---|---|
| `GATE-ENT-01` | entity_cas_protocol | `update` / `delete` / `restore` 必传 `expected_version`；不匹配抛 `cas_version_mismatch` | runtime |
| `GATE-ENT-02` | entity_read_only_protection | `read_only=True` 的 entity 任何写操作抛 `read_only_violation` | runtime |
| `GATE-ENT-03` | entity_soft_deleted_no_update | 软删后 `update` 抛 `entity_soft_deleted`；可 `restore` | runtime |
| `GATE-ENT-04` | entity_actor_log_append_only | `_actor_log` 只追加不修改 | runtime |

### Asset (03-asset)

| Gate | 名字 | 检查什么 | 形态 |
|---|---|---|---|
| `GATE-AST-01` | asset_checksum_integrity | `get(ref)` 时 sha256 不匹配抛 `asset_corrupted` | runtime |
| `GATE-AST-02` | asset_document_reachability_gc | 无 Document 的 GC fail-closed；有 Document 时先按完整多重集对账，stale refs 不得导致误删/误保 | runtime |
| `GATE-AST-03` | asset_dedup_no_duplicate_blob | 同 sha256 磁盘上只一份 blob 文件 | runtime |
| `GATE-AST-04` | asset_release_idempotent_at_zero | refs=0 再 release 不变成负数 | runtime |
| `GATE-AST-05` | asset_shared_pool_fallback | 共享池:读委托 fallback / 写留本地 / is_local 区分 / promote 复制（P3） | runtime |
| `GATE-AST-06` | http_asset_capacity_fence | HTTP 上传必须同时限制声明/流式实际字节与项目本地总量；相同 sha 去重不重复占额，两个环境旋钮归 ops 唯一管理 | runtime + static |
| `GATE-AST-07` | media_ticket_private_delivery | 票据签名/域分离 runtime 在所有形态执行；api/web 才检查 Repository 与 HTTP 私有交付，web 才检查前端媒体 URL；主 session token 不得进入媒体 URL，票据绑定 opaque principal + tool + project + generation + sha | runtime + surface-aware static + HTTP/TS tests |

### Operation (02-operation)

| Gate | 名字 | 检查什么 | 形态 |
|---|---|---|---|
| `GATE-OP-01` | operation_immutable | Operation 是 frozen dataclass | static |
| `GATE-OP-02` | oplog_hash_chain_intact | `OpLog.verify_chain()` 始终返 True；篡改任何 entry 应返 False | runtime |
| `GATE-OP-03` | operation_source_valid | `source ∈ {user, ai, sync, system}` | runtime |
| `GATE-OP-04` | patch_op_valid | `Patch.op ∈ {set, delete, create, soft_delete, restore, name}` | runtime |
| `GATE-OP-05` | reverse_patches_present | 每个 Operation 有 `reverse_patches`（即使为空也是 tuple）| runtime |
| `GATE-OP-06` | operation_external_envelope | `Operation.external` 为 None 时 `to_dict` 省略键（历史 oplog hash 不变）；有值时 round-trip + 链通过 | runtime |
| `GATE-OP-07` | oplog_truncated_tail_durable_repair | 真实文件留下无 LF 尾片；LOCK_EX 下物理 truncate + fsync 后续写，连续两次 fresh reload 都保留新 op；LF-terminated 坏 record 必须 fail closed | runtime filesystem |
| `GATE-OP-08` | oplog_strict_storage_boundary | 最终/直接父目录 symlink、hardlink 与特殊文件 fail closed；文件权限为 0600；重复键/非有限数拒绝且错误不泄露 payload/path；caller 并发改嵌套值不能打断 hash；record/总量上限在发布前生效，有限历史 hash 不变 | runtime filesystem |

### Command (01-command)

| Gate | 名字 | 检查什么 | 形态 |
|---|---|---|---|
| `GATE-CMD-01` | command_unique_id | 同一 registry 不能注册重复 id | runtime |
| `GATE-CMD-02` | command_sources_allowed_valid | `sources_allowed` 元素必须在 SOURCES | runtime |
| `GATE-CMD-03` | command_execute_required | `CommandSpec.execute` 不能为 None | runtime |
| `GATE-CMD-04` | command_execute_returns_operation | dispatch 时 execute 必返 Operation / CommandMutation；其他类型抛 validation_failed | runtime |
| `GATE-CMD-05` | command_dispatch_source_acl | source 不在 `sources_allowed` 抛 `acl_denied` | runtime |
| `GATE-CMD-06` | mod_no_direct_data_mutation | mod 业务代码（02-mod/05-code/）不能直接调 `doc.add` / `entity.update`（必走 command）| static |
| `GATE-CMD-07` | command_confirm_gate | `requires_confirm` 未确认时零变更返 `PendingConfirmation`；reserve 取得唯一执行权，committed 后才消费 token | runtime |

### Pipeline (03-pipeline)

| Gate | 名字 | 检查什么 | 形态 |
|---|---|---|---|
| `GATE-PIPE-01` | stage_checkpoint_for_cancel | stage.run 应周期性调 `ctx.checkpoint()` 才能 cancel | static（提示性）|
| `GATE-PIPE-02` | stage_no_direct_data_mutation | stage handler 改 ZE 数据应走 command（跟 GATE-CMD-06 同源）| static |
| `GATE-PIPE-03` | pipeline_status_terminal | PipelineRun.status 终态后不能退回 running | runtime（待实施） |
| `GATE-PIPE-04` | pipeline_multistage_data_model | StagePlan 校验 + EntityRunStatus 单 stage 字节兼容 + stage 标记连续前缀语义（P2a） | runtime |
| `GATE-PIPE-05` | pipeline_start_requires_one_stage_arg | supervisor.start 必传 item_stage / stage_plan 之一、不可同传（P2b） | runtime |
| `GATE-PIPE-06` | pipeline_runview_metrics | run-view ETA/吞吐线性外推 + 每 stage 完成统计纯函数不变量（P2d） | runtime |
| `GATE-PIPE-07` | pipeline_resource_scope_acquire | stage resource_scopes 接入：无 registry/未注册降级 + 已注册真持槽（P2c-B） | runtime |
| `GATE-PIPE-08` | artifact_completion_receipt | 裸文件不算 done；receipt 必须绑定 commit/operation/checksum；损坏 receipt fail closed 为 repair 而不是 pending | runtime |
| `GATE-PIPE-09` | pipeline_delegated_mutation_stays_dormant | delegated mutation 只能是 private authority check：完整识别直接、相对、别名与字面量动态 import，稳定 SDK/public lock 零导出；dormant 模块的顶层结构、依赖、调用和对象写入均为严格允许列表，任何新 I/O、thread/process、dispatch/commit/publish、worker/action/manifest/run/Control lifecycle 默认失败；读取 reducer 的 Control facts 只可用于 deny-only 拒绝 | whole-tree static dormant-boundary |

### AI Coworker (05-ai-coworker)

> 详见 [05-ai-coworker/SPEC.md](../03-commands-operations/05-ai-coworker/SPEC.md)。本组**本轮只注册规则,均未实施 runtime check**(见 §5)。

| Gate | 名字 | 检查什么 | 形态 |
|---|---|---|---|
| `GATE-AI-01` | provenance_required_when_source_ai | source='ai' 的写入必须带非空溯源（args.provenance/ai_provenance）| runtime ✅（`AiGovernor.guard` require_provenance）|
| `GATE-AI-02` | ai_op_respects_version_cas | source='ai' 提交必传 `expected_version`，冲突→error marker 不覆盖（复用 `GATE-ENT-01`）| runtime（对既有 supervisor `_commit_ok` 行为的断言，可即实施）|
| `GATE-AI-03` | ai_provenance_round_trip | `AiProvenance` 经 entity to_dict/from_dict 无损还原 | runtime（机制已有，可即实施）|
| `GATE-AI-04` | ai_io_safety_filter_applied | source='ai' 输入过 moderator 钩，拦截→`ai_safety_blocked` | runtime ✅（`AiGovernor` moderator,阈值/moderator 走 mod config）|
| `GATE-AI-05` | high_risk_needs_approval | `ai_high_risk`/高成本 command 的 source='ai' → 复用两段式 confirm 人审 | runtime ✅（`AiGovernor` → `requires_confirm`/`PendingConfirmation`）|
| `GATE-AI-06` | cost_cap_enforced | 单次估算 `cost_usd` ≤ per-call cap，超则 `ai_cost_exceeded`（日累计 ledger 留后续）| runtime ✅（`AiGovernor` per_call_cap_usd）|
| `GATE-AI-07` | ai_run_phase_valid | AI 长任务 `EntityRunStatus.phase ∈ RUN_PHASES` | runtime（机制已有；若加 awaiting_approval 需先扩 RUN_PHASES）|

### Concurrency (05-concurrency)

| Gate | 名字 | 检查什么 | 形态 |
|---|---|---|---|
| `GATE-CC-01` | lock_acquire_release_balanced | 拿了 lock 必释放（业务方 try/finally pattern）| static（提示性）|
| `GATE-CC-02` | queue_finish_after_start | `start` 后必 `finish`，记录耗时 | static |
| `GATE-CC-03` | rate_configured_before_use | 长期场景应 `configure` 后再 `acquire`，避免默认放行隐患 | static（提示性）|
| `GATE-CC-04` | lock_ttl_positive | `AdvisoryLockManager(ttl_sec=)` 必须 > 0 | runtime |

### Resource Budget (06-resource-budget)

| Gate | 名字 | 检查什么 | 形态 |
|---|---|---|---|
| `GATE-RB-01` | budget_from_env | `ResourceContract` 工厂/`from_env` 正常读 + env 覆盖优先 + 非法值抛 `resource_budget_invalid` | runtime |
| `GATE-RB-02` | scope_naming_enforced | `validate_scope` 接受点分命名、拒裸名/大写/单段抛 `resource_scope_invalid` | runtime |
| `GATE-RB-03` | gate_single_entry_per_scope | `GateRegistry` 同 scope 重复注册抛 `resource_scope_duplicate`（唯一入口保证） | runtime |
| `GATE-RB-04` | runtime_active_le_budget | **运行时自证**：真起线程压闸门峰值活跃数 <= max_concurrency；畸形闸门触发 `resource_budget_exceeded` | runtime |
| `GATE-RB-05` | concurrency_audit_multiplication | **乘法审计**：乘积（path×per_path×sub×cost）超预算时 `audit_concurrency(raise_on_exceed=True)` 抛 `concurrency_audit_failed`，不超返 ok 报告 | runtime |
| `GATE-RB-06` | capacity_weighted_le_budget | **容量类加权预算**：`CapacityGate.acquire(cost)` 加权峰值 `sum(cost) <= capacity`；单任务 `cost>capacity` 立即拒；畸形闸门触发 `resource_budget_exceeded` | runtime |
| `GATE-RB-07` | budget_declared_before_use | 每个被 `acquire` 的 scope 必先有 `ResourceContract` 声明（原则 2：先定义预算再写代码） | static（提示性）|
| `GATE-RB-08` | gate_at_resource_not_business | 重资源调用应在 `gate.acquire` with 体内，不在多条业务路径各自限流（原则 1） | static（提示性）|

### Conflict Resolution (02-conflict-resolution)

| Gate | 名字 | 检查什么 | 形态 |
|---|---|---|---|
| `GATE-CONF-01` | conflict_strategy_known | `strategy` 必须在 `ALL_STRATEGIES` | runtime |
| `GATE-CONF-02` | resolver_is_pure | resolve 不修改输入；调多次同结果 | runtime |

### Field Types (04-field-types)

| Gate | 名字 | 检查什么 | 形态 |
|---|---|---|---|
| `GATE-FT-01` | string_rejects_non_str | string type 拒非 str | runtime |
| `GATE-FT-02` | required_dual_style | 父对象 required 数组 + 字段 required: True 都接受 | runtime |
| `GATE-FT-03` | asset_ref_form | asset_ref 校 sha256 64 hex + mime/size/ext 形态 | runtime |
| `GATE-FT-04` | entity_ref_form | entity_ref 校 `{$ref: type/id}` + 可选 `to` | runtime |
| `GATE-FT-05` | nullable_via_oneof | `oneOf [string, null]` 接受 str + None | runtime |
| `GATE-FT-06` | bool_not_number | `bool` 不视为 `number` / `integer`（Python 坑修复）| runtime |
| `GATE-FT-07` | sanitize_strips_control | `sanitize` 剔除控制字符（留 \t\n\r）+ maxLength 截断 + 对象递归 + 不改原值 | runtime |

### Capacity Limit (05-capacity-limit)

| Gate | 名字 | 检查什么 | 形态 |
|---|---|---|---|
| `GATE-CAP-01` | capacity_limits_from_env | `from_env` 正常读 `ZE_CAPACITY_*`；非法值抛 `capacity_config_invalid` | runtime |
| `GATE-CAP-02` | capacity_exceeded_throws | entity 数/体积/字段、单 asset、项目 asset 总额 5 类 `check_*` 超限均抛 `capacity_exceeded` + context 含 `limit_name`/`limit`/`actual` | runtime |
| `GATE-CAP-03` | capacity_measure_entity | `measure_entity` 返 `data_size` (JSON UTF-8 字节) + `field_count` (递归) | runtime |

### History & Rollback (04-history-rollback)

| Gate | 名字 | 检查什么 | 形态 |
|---|---|---|---|
| `GATE-UNDO-01` | undo_produces_rollback_op | undo 后 OpLog 末尾 `kind=rollback` | runtime |
| `GATE-UNDO-02` | redo_inverts_undo | undo 后 redo 恢复原数据状态 | runtime |
| `GATE-UNDO-03` | apply_patch_set | `Document.apply_patch_directly` 处理 set patch + CAS bump | runtime |
| `GATE-UNDO-04` | apply_patch_meta_ops | 元操作 create / soft_delete / restore / hard_remove 全跑通 | runtime |
| `GATE-UNDO-05` | group_id_aggregation | 同 group_id 多 op 一次 undo 反所有 | runtime |

### Realtime Sync (01-realtime-sync)

| Gate | 名字 | 检查什么 | 形态 |
|---|---|---|---|
| `GATE-RT-01` | broker_publish_routes_to_subscribers | `publish_change` → 订阅者 callback 收到 | runtime |
| `GATE-RT-02` | subscribe_entity_type_filter | `entity_type` 过滤生效 | runtime |
| `GATE-RT-03` | subscribe_since_seq_replay | `since_seq` 触发 replay buffer 中 seq >= N 的消息 | runtime |
| `GATE-RT-04` | unsubscribe_stops_delivery | `unsubscribe` 后不再收到 | runtime |
| `GATE-RT-05` | payload_format_entity_change | payload 含 `type=entity_change` + 必需字段 | runtime |
| `GATE-RT-06` | cmd_dispatch_has_no_commit_side_effects | 即使收到旧 `broker` / `op_log` 参数，`CommandRegistry.dispatch` 也只返回 Operation，不 append、不发布；canonical event 只归 `CommitCoordinator` | runtime |
| `GATE-RT-07` | entity_status_snapshot_replaces | SSE 重连的 entity status 全量快照必须标记 replace，客户端先清旧 map 再重建；server 接线只适用于 api/web，TS 消费端只适用于 web | surface-aware static + TS runtime tests |

### Commit boundary (07-persistence)

| Gate | 名字 | 检查什么 | 形态 |
|---|---|---|---|
| `GATE-COMMIT-01` | commit_durable_before_visibility | 真实 FileCommitStore + Coordinator 严格 `save → append → publish`，事件必须含 `commit_id/document_version` | runtime |
| `GATE-COMMIT-02` | production_surfaces_use_commit_coordinator | HTTP、MCP、CLI、Pipeline 与 bootstrap 都显式接入 coordinator，旧 finalizer 不得复活；canonical history authority 的生产 import inventory 必须逐成员精确等于 bootstrap / CommitCoordinator / Undo / Operation、公共 ticket 校验器、server 唯一的 neutral-error HTTP handler、SDK 唯一的 typed transport/idempotency classifier，以及 Pipeline 唯一的 typed failure→耐久 marker 投影器；Undo 的 legacy group rollback 只可消费不授予任何能力的 exact-bound deny probe，禁止别名增权、整模块/star/dynamic import 和 `sys.modules` 绕过，raw transaction lookup、私有 capability/port 不得进入 history `__all__`、稳定 SDK 或 public lock | whole-tree static wiring + exact private-consumer boundary |
| `GATE-COMMIT-03` | nested_commit_producer_rejected | producer 内递归 execute 在任何 store write 前失败，必须改为一笔 `CommandMutation` | runtime |
| `GATE-COMMIT-04` | pipeline_requires_commit_coordinator | PipelineSupervisor 默认缺 coordinator 即失败；legacy adapter 只能显式用于测试 | runtime |
| `GATE-COMMIT-05` | mutation_session_failure_matrix | 双 `NOT_DURABLE` 必须原位恢复同一 Entity 并归类 aborted；写入结果 unknown 必须保留当前图并归类 indeterminate，禁止假回滚 | runtime |
| `GATE-COMMIT-06` | producer_effect_forbidden | producer 调框架外部 effect port 必须在 store 前以 `producer_effect_forbidden` 失败，ContextVar 随异常复位 | runtime |
| `GATE-COMMIT-07` | confirmation_receipt_lifecycle | 确认 token 必须 actor-bound + human nonce-bound；HTTP 人工批准走统一 executor，MCP/AI 不得拥有批准入口或 nonce，只能发起/取消本人 pending；committed 消费、完整回滚 aborted 释放、indeterminate 冻结且不可重放 | runtime + static |
| `GATE-COMMIT-08` | atomic_commit_plan_closure | 真实构造并重验涵盖 rooted snapshot、Operation/hash chain、逐 operation outbox、serializable action、pipeline intent、exact asset occurrence、inbox 与 confirmation 的 immutable plan；所有计数/摘要/tail/head 必须闭合，底层篡改必须被 writer-side full revalidation 拒绝。integrity 层不得依赖 SQLite、文件、Document/Entity runtime、SDK 或 I/O | runtime + dependency boundary |
| `GATE-COMMIT-09` | sqlite_same_transaction_writer_fault_matrix | 私有 writer 只能接收一个完整 `AtomicCommitPlan`，在同一 `BEGIN IMMEDIATE` 内写 snapshot/head/commit/Operation/target/outbox/action/pipeline/asset/inbox/confirmation 与 receipt，再做闭包校验和 fresh reconcile。gate runtime 验证 pre-COMMIT 普通失败与进程控制零残留、post-COMMIT ack-loss 与进程控制可 fresh 证明且信号原样传播、durability barrier 失败只能 UNKNOWN；AST 另强制 writer test 保留真实 child `os._exit(71)` 的 before/after-COMMIT 两态 fresh 分类。并冻结三 revision change manifest 为 v2、baseline v1 使用独立 hash domain | runtime + static transaction/crash boundary，surface-neutral |
| `GATE-COMMIT-10` | worker_projection_and_sqlite_first_claim | 冻结纯 Worker 结果投影的 payload 所有权与 schema-v8 首次 OUTBOX claim，并真实验证 exact retry、pre-COMMIT 回滚、ack-loss、SQLite-now 时间栅栏和全局身份冲突。schema-v9 sidecar 承载 OUTBOX `AUTHORIZE_EFFECT / COMPLETE / RECONCILE` 的 sealed pair、固定 SQL、payload-before-head、authorizer、fresh reconcile 与 durability boundary；历史 schema-v10 只加固时间/dispatch 写入边界，历史 schema-v11 只修正 cutover chronology/state 结构，current schema-v12 只追加 install-closure 表示底座。3.23.0 同一道 gate 继续锁住通用 production writer 的 pipeline/action/multi-outbox 同事务闭包、transition CAS、控制身份 preflight 与合法 Worker 后缀重联，以及不接原 plan 的 production rooted reader：pinned snapshot 内重建 inner/outer receipt、三组 target、control/ledger/current/allocation，第二轮同时 fencing runtime/worker/dispatch。plan-free auditor必须重建 multi-allocation batch、pipeline CREATE/TRANSITION、action/pipeline genesis及受支持 Worker 后缀，并以 `EXACT / ABSENT / CONFLICT / CORRUPT / UNKNOWN`、资源预算和 fresh publish fence 分类。renew/expire/retry、action/pipeline worker execution及 atomic-v2 扇出仍必须 UNKNOWN；gate 不冒充 dispatcher、生产挂载或 authority router | runtime + pure/static/fixed-SQL/read/audit boundary，surface-neutral |
| `GATE-COMMIT-11` | canonical_cutover_asset_members_and_phase_head | 锁定 cutover asset stage 中 project/blob/occurrence 三类成员的唯一 canonical 身份、value digest、metadata digest、稠密分类 ordinal 与冻结向量；并把 live/deleted baseline、Document/Entity、asset blob/ref、commit/head与scheduler genesis固定为逐列投影。历史 V11 对 member-mapping carrier 与 pre-mount baseline outer receipt 继续稳定报告 schema-upgrade requirement；历史 V12 必须从同一 sealed plan 产生分块 mapping、baseline-v2、baseline-specific outer sidecar 与 install closure，零 target 复用生产协议三组空根，asset row 保持 fresh coverage 背书的 available revision 1。private fresh reader只能 SELECT raw rows，必须重组 chunk、重算 artifact/outer/root/closure 并逐项把 sidecar ordinal 对回 mapping；current V13 authority reader只有消费这份盖章结果与V13 phase-binding语义链才可继续。后四阶段 head 的既有纯链仍重证前驱、typed pointer 与 chronology；本门不冒充 fixed install writer、coordinator 或 full auditor | runtime + pure/static canonical-integrity/persistability/fresh-read boundary，surface-neutral |
| `GATE-COMMIT-12` | sqlite_cutover_install_fixed_writer | current-V13 private install writer只能消费完整重验的V12 install-closure projection，并要求V13 phase-binding前缀尚为空；不能接raw plan、baseline、coverage、clock、裸connection或SQL callback。static gate锁定API、16条exact plain INSERT的目标列/值位次与唯一marker CAS、完整authorizer action、helper外无mutation、V3 terminal-shadow三表只读、closure-last、同一事务恰好两次fresh semantic read、COMMIT后新连接reconcile、current identity与production caller为零；同时冻结legacy三类generic↔child闭合、schema-V10生产保留集+JOIN、CAST-aware BLOB防线及唯一coverage/stage结构闭包。runtime probe在独立empty数据库覆盖初始absent、pre-COMMIT零残留、install、exact retry、ack-loss、durability UNKNOWN、真实BUSY、同/异projection双线程竞争、production/legacy BLOB污染、rollback/commit-close uncertainty、stable kernel CORRUPT与process-control原信号；live/mixed/deleted、asset coverage metadata、partial namespace、额外V3行及active suffix由focused writer suite另证。本门不声称已覆盖真实多进程竞争或等长语义篡改，也不冒充全局plan-free cutover auditor、asset/audit/activation/mount writer、coordinator、dispatcher或authority router | runtime + static fixed-SQL/authorizer/order/reconcile/production-zero boundary，surface-neutral |
| `GATE-COMMIT-13` | sqlite_cutover_plan_free_durable_audit | private V12 cutover auditor只接exact `SqliteDatabase`和可选detached expected identity，不接plan/projection、clock、裸connection、SQL callback或writer capability。专用`mode=ro + query_only` opener只设置connection-local安全PRAGMA，不修复persistent journal/permission漂移；一个pinned snapshot重证文件/schema/security/FK/full audit、reserved receipt、V5/V8/V12 closure、当前baseline/scheduler、audit/activation/mount/head，第二个fresh snapshot逐字段fence marker/runtime/closure/head/audit/worker/allocation/dispatch/receipt/business high-water；确定的EXACT/ABSENT再过不修复profile/permissions的read-only durability fence。逻辑行/TEXT+BLOB/VM预算、legacy/production reserved receipt闭合和unavailable cause-chain只由private neutral verifier实现一次；auditor做五态映射，neutral层不认识状态、writer capability或mutation SQL，phase writer也直接消费该leaf而不借用auditor私有实现。它区分headless closure与五个head阶段，expected只在数据库先自证后比较；coverage-only/stage-only/stage+coverage preinstall与健康post-mount suffix保持UNKNOWN，混合lineage、pre-mount suffix、未来time/high-water、reserved BLOB、carrier/head/audit/mount或mounted额外row/scheduler漂移归CORRUPT，确定性结构坏库必须双观测。首张INSTALL certificate按因果后继集合流式重建exact receipt count/root；successor在256 hop/16 MiB内验证raw21列、parent、pure predecessor、worker membership、final head及不超过current append-only上界，但无authenticated checkpoint时不猜历史receipt/worker as-of并返回UNKNOWN。I/O/Memory/cleanup/fence、100万行/512MiB TEXT+BLOB/1亿SQL VM步逻辑预算不可用不冒充CORRUPT；不宣称硬RSS/page/time门。独立非pytest probe锁住六恢复点逐字段identity、真实writer/worker、journal不修复、stage/mixed、周期链与未来高水位、篡改、逐字段fence、durability以及snapshot/progress/cleanup process-control；static边界冻结完整private owner forward graph、direct/dynamic/test/probe逃逸、SDK/production caller为零。detached结果不是写权限；本门只证明审计边界，不为3.29.0 finalizer或任何后续writer签发权限，也不冒充coordinator、router、dispatcher、真实多进程crash矩阵或checkpoint | runtime + static query-only/two-snapshot/replay/production-zero boundary，surface-neutral |
| `GATE-COMMIT-14` | sqlite_cutover_phase_one_head_finalizer | private `SQLiteCutoverPhaseHeadWriter`只接exact `SqliteDatabase` 与模块私有capability，`finalize()`不接plan/projection、clock、裸connection、SQL callback或detached auditor identity。static边界锁定唯一`BEGIN IMMEDIATE`、唯一fixed `INSERT INTO business_cutover_head`、禁UPDATE/DELETE/DDL/写PRAGMA/ATTACH/触发器代写、V12 schema identity不变和production caller为零；BEGIN后即开启transaction-wide mutation firewall，只有canonical head INSERT的极窄窗口可写，前后两次raw proof中的注入DML都必须被拒绝并回滚。写前必须从raw rows重证install generic/specialised receipt、V12 closure、`read_only / state_revision=1` runtime、空head和空post-install业务/worker/audit/activation/mount suffix，再由冻结的纯head issuer派生revision-1 `installed / read_only` row。COMMIT后使用新连接fresh `reconcile()`重证唯一canonical head与空suffix，确定成功还必须通过storage durability barrier；ack-loss只能fresh恢复。独立非pytest probe覆盖absent/install/finalize/retry、pre/post-COMMIT、durability、竞争、fixed-SQL/authorizer、proof内DML拒绝、空suffix证明、detached-auditor绕过及process-control；本门不翻file/active authority，不写asset/audit/activation/mount，不冒充coordinator/router/dispatcher | runtime + static fixed-SQL/raw-reproof/reconcile/production-zero boundary，surface-neutral |
| `GATE-COMMIT-15` | sqlite_cutover_asset_provider_phase_writer | private asset-provider writer只接exact database、私有capability和same-PID/same-thread/active external observation，不接plan、projection、预签binding、裸connection或SQL。门禁冻结四阶段共享纯mapper的asset 91列布局、唯一`BEGIN IMMEDIATE`、事务内raw install/closure/coverage/runtime/receipt/scheduler重证、detached READY逐字段交叉绑定、fixed binding INSERT→head CAS→semantic readback→COMMIT顺序，以及两个极窄authorizer窗口。exact retry必须匹配provider、pending admission、source freeze与maintenance scope/proof/time；runtime覆盖pre-COMMIT双行回滚、ack-loss fresh恢复、stale observation、coverage/checkpoint冲突、live lease失效与其他DML拒绝。writer/issuer保持SDK和production caller为零；本门不证明真实coordinator、file lease实现或后三阶段writer | runtime + static fixed-SQL/raw-reproof/lease/retry/production-zero boundary，surface-neutral |

### SQLite transaction kernel (07-persistence)

| Gate | 名字 | 检查什么 | 形态 |
|---|---|---|---|
| `GATE-DB-01` | sqlite_kernel_safety_profile | 真实逐连接读回 WAL/FULL/FK ON/trusted schema OFF/busy timeout，application/schema/migration checksum 与已知 schema 对象集合一致；POSIX local FS、DB/bootstrap-lock 路径类型/权限/非法 timeout fail closed；非 initialize 打开缺失 authority 时零创建，安全基线不可配置降级 | runtime，surface-neutral |
| `GATE-DB-02` | sqlite_crash_receipt_reconcile | 独立进程在 COMMIT 前后真实 `os._exit`；新进程 fresh reconcile，验证 committed receipt 可找回、未提交不冒充，同一 transaction id 不重复 | runtime multiprocess，surface-neutral |
| `GATE-DB-03` | sqlite_business_schema_v2 | 冻结 `MIGRATIONS[:2]` 的 v2 三张 business 表，并在 current V13 中证明其精确 DDL/STRICT/PK/FK 未被改写、仍为空且 authority=legacy；普通 UoW 不可写；真实 V1 逐版升到 current V13 依然保留 repository marker/receipt。这是 V2 历史契约门，不把预留 payload 列当作已实现 codec | runtime，surface-neutral |
| `GATE-DB-04` | deterministic_snapshot_codec | 真实执行严格 JSON 值固定向量、dict 顺序无关的唯一 UTF-8 bytes、纯函数性、双向逆变换与精确 32-byte SHA-256；正负 4300 位 int 在 encode/decode 均接受，4301 位均以 `business_codec_integer_too_large` 拒绝；bounded decode 用 `json.loads` 禁用陷阱证明 node flood 在 parser 前失败；拒绝其他非 JSON 类型、非有限数字、非法 UTF-8、重复 key、循环/超深嵌套和非 canonical bytes；审计 codec 不引入 Document/Entity/SQLite 依赖 | runtime + dependency-boundary，surface-neutral |
| `GATE-DB-05` | business_snapshot_root_and_restore | 真实 capture/verify/restore 一个多 Entity Document；校验 bounded byte/node 与 120 层业务域，用反 identity 插入的 `z/a` 同时钉住 rows 排序与恢复位次，删减、多添或重编叶子（同时重算该行 hash）均与旧 project root 不一致；restore 不经 `Document.open` / `Entity.from_dict` / `Document.add` 且固定要求 runtime rehydration；审计 snapshot 模块不依赖 SQLite/file/topology/sdk/OpLog，current V13 仍为 authority=legacy，历史 business/import/catalog/proof/transaction 与 36 张 production/runtime 私有表全部为空。root 是一致性而非带密钥真实性证明 | runtime + dependency-boundary，surface-neutral |
| `GATE-DB-06` | runtime_load_single_transition | 真实执行 rooted restore 一次性交接、声明安装、changed-row 最终 schema 拒绝、canonical no-op/changed 分类、exact-one Document version 与 before/after root CAS、typed durability outcome、commit 后 overlay、final authority fence 和 manifest-bound one-shot admission；静态扫描 state/projection/coordinator 三层，证明依赖单向、全部公开对象原样 re-export、seam 不依赖 storage/topology/log/cache/I/O 实现且不公开 pending candidate/proof/unverified legacy issuer | runtime + dependency-boundary，surface-neutral |
| `GATE-DB-07` | strict_legacy_scan_payload_receipt | 真实从 live topology 扫描 exact generation/root/head，生成 canonical manifest 与完整 opaque plan；验证 row graph 隔离、transaction/receipt/ordinal/source/storage/business-root 逐字段绑定、exact reservation↔plan verifier、attempt-token recovery、共享者不可窃取、漂移先 quarantine、adapter/runtime 独立 one-shot lane、fork child 失效与 parent 正常收口。静态钉住 state←facade 单向依赖、pure protocol/integrity 单源、严格 import/I/O allowlist、Document scan 的唯一生产消费者与 private adapter 的唯一写入职责；最后重证 fresh SQLite `user_version=13`、`authority=legacy` 与历史 transaction、V8 production/worker、V9 sidecar、V12 install-closure、V13 phase-binding 私有表全部为空 | runtime + static dependency/consumer boundary，surface-neutral |
| `GATE-DB-08` | sqlite_legacy_import_schema_v3 | 冻结 `MIGRATIONS[:3]` 的 V1/V2/V3 顺序、checksum 与两张 STRICT import ledger 的精确列/PK/UNIQUE/FK/CHECK/deferral，同时在 current V13 fresh/升级库中证明这些历史对象未被改写；后续私有表仍为空且 authority=legacy。generic commit 对 import 保留 prefix/kind 在 callback/receipt 前拒绝（含恶意 `str` 子类），普通 UoW 不可写 ledger，原生 SQL 的非法 hex/protocol/status/count/BLOB/FK/批内重复 project·document 均由数据库拒绝；污染 V2 升级失败后 user_version/migration ledger/DDL/fingerprint 完整回滚，健康 V1/V2 升 current V13 保留 marker/receipt。静态证明 pure V3 contract/schema 不做 I/O、不依赖 sdk/topology/Document，import 写入仍只归 core importer，V4–V13 的追加能力不得篡改该历史职责 | runtime + static dependency boundary，surface-neutral |
| `GATE-DB-09` | sqlite_legacy_import_terminal_shadow | 真实执行 EMPTY→TERMINAL_SHADOW、exact retry、same/cross-id conflict、disjoint batch、tamper→CORRUPT、fixed SQL/authorizer rollback 与 fresh reconcile；EMPTY 时 generic UoW 对 current V13 全部 private 表 READ fail closed，callback 后 handle revoke；TERMINAL_SHADOW 时 generic commit 在 callback 前关闭。静态钉住固定 INSERT、共享 integrity、唯一 writer/private bridge、durability barrier 与生产零 caller；importer/sealer 共用 byte-exact reconcile，并发验证在同一 WAL snapshot，ack-loss/async/durability uncertainty 只能 fresh 分类或 UNKNOWN/quarantine，全程 `authority=legacy` | runtime + static dependency/consumer boundary，surface-neutral |
| `GATE-DB-10` | sqlite_legacy_catalog_historical_seal_v4 | 冻结 `MIGRATIONS[:4]` 的 V4 migration/checksum，并在 current V13 fresh 与 authentic V3→current V13 升级库中证明两张 STRICT append-only catalog ledger 的精确 DDL/FK/index/trigger 未被改写；真实验证 empty/nonempty seal 与 receipt 幂等。private coordinator 必须在同一 strict catalog barrier 内按全量活跃项目做“第一次扫描→import→独立第二次扫描→seal→fresh reconcile”，两次 descriptor/import identity 必须全等；sealer 只有四条固定 plain INSERT，authorizer 拒绝其他表、UPDATE/DELETE/PRAGMA，core capability 只有唯一 private SDK caller。importer/sealer 共用一个更低层的 byte-exact reconcile，seal BLOB 或 import/member 同长篡改必须同步得到 CORRUPT。证据只覆盖完成观测时的 live business catalog，明确不覆盖 tombstone/OpLog/assets/outbox/当前性；持久行与返回值始终 `authority=legacy`、`cutover_ready=false`，不进入 `PUBLIC_SURFACE.lock` | runtime + static dependency/consumer boundary，surface-neutral |
| `GATE-DB-11` | sqlite_repository_proof_substrate_v5 | 冻结 `MIGRATIONS[:5]` 的 V1–V5 migration/checksum，在 current V13 普通库中验证三张 V5 `STRICT`/append-only/private proof 表仍保持原义，并验证纯完整状态摘要与 deleted generation high-water。持久 header 只能是 `authority=legacy / authority_state=cutover_prepared / cutover_ready=0`；它是后续 cutover 的静态证据地基，不是 authority flip、业务 transaction store 或跨设备同步 | runtime + static dependency boundary，surface-neutral |
| `GATE-DB-12` | sqlite_repository_exact_proof_v5 | 在 current V13 普通初始化库中，按冻结的 V5 proof/writer 语义真实封存并 fresh reconcile empty、deleted-only、live、mixed 四种完整仓库形态，逐项核对 live 与 V4 catalog、deleted high-water、control/seal/member/receipt namespace；重试必须逐字节幂等，健康不同 projection 是 CONFLICT，同 id 同长 BLOB 篡改是 CORRUPT。writer 只能执行 control/receipt/seal/member 四条固定 plain INSERT，authorizer 拒绝其他表、UPDATE/DELETE/PRAGMA；首条 INSERT 后失败必须全回滚，COMMIT 后 ack-loss 只能经 fresh exact reconcile 恢复成功，post-COMMIT cancellation 必须 fresh 分类后原样传播，durability barrier 与 BUSY/IO 不确定性只能返回 UNKNOWN。持久态与结果始终 legacy-only、`cutover_ready=false`，private coordinator 的 production caller 必须为 0 | runtime + static dependency/consumer boundary，surface-neutral |
| `GATE-DB-13` | sqlite_business_transaction_substrate_v6 | 冻结 V1–V5 migration/checksum 不变，并把已发布 V6 checksum 永久钉为 `41fe4ba1…225254`；验证 V6 自身仍是唯一追加的 DDL-only substrate。17 张 transaction 表由单一 registry 收口、全部 `STRICT`/private，current V13 fresh install 后仍逐表为空，`kernel_repository.authority=legacy`，没有 runtime control/head/commit/receipt。generic UoW 在 callback 前拒绝保留 transaction prefix/kind。另在冻结的 V7 历史前缀中用 schema 叶真实 SQLite seed/SAVEPOINT 逐组漏掉 durable action、pipeline intent、asset ref，三次 head finalization 必须分别失败；精确闭合后，三组 late append 必须失败，commit-scoped asset refs 的 UPDATE/DELETE 也必须由 append-only trigger 拒绝。V7–V13 只能追加后续能力，不能回写 V6 历史 | runtime + migration/private/closure boundary，surface-neutral |
| `GATE-DB-14` | sqlite_local_revision_compatibility_v7 | 冻结 `MIGRATIONS[:7]`：完整 v1–v6 registry/checksum 与 v7 checksum `d96606b2…aa0656`；在真实历史 v7 registry 中构造 authentic empty v6 并只升级到 v7，要求先 fail-closed preflight，再为历史单项目原型回填 local revision、重建 head 的 local FK、安装 project/head binding guard，最终 ledger 精确且仍 legacy/空。另以合法第二项目 baseline 推进 global 后再提交原项目 head，fresh writer/reconcile 必须得到 `global/project/head = 3/2/2`。该门不把已有活跃 v7 数据偷偷送进 v8；当前 v8 兼容性由 DB16 单独负责 | runtime + historical migration compatibility boundary，surface-neutral |
| `GATE-DB-15` | sqlite_rooted_reader_closure_and_linearization | 在冻结的 v7 历史 registry 中，私有 reader 于第一个 `BEGIN` 重证 runtime、project/head/commit/receipt、v2 snapshot、Operation/target/outbox/action/pipeline/asset、inbox/confirmation 与 canonical manifest，返回无 path/row/connection 的 detached rooted carrier；fresh 第二 snapshot 只做 authority/control publish fence。真实全成员提交必须读回 `2/2/2` 与完整计数；在 pinned read 后、返回前重叠提交时，本次仍返回旧 `1/1/1`，下一次才返回新 `2/2/2`，明确线性化点而不虚构“返回时最新”。v8 writer/reader 的新原子收口不由这道历史门冒充 | runtime + historical rooted read/linearization boundary，surface-neutral |
| `GATE-DB-16` | sqlite_production_authority_worker_schema_v8 | 永久冻结历史 V8 migration 名称、checksum=`f6c8a8e1…8ad80b`、schema fingerprint=`0d96ffa4…fce771` 与 canonical migration-ledger root=`9595ea54…c1834a`；只用 `MIGRATIONS[:8]` 真实从 pristine V7 升 V8，逐字保留 V1–V7 ledger，authority 仍为 legacy，历史 17 张 transaction 表与 V8 的 25 张 production/worker 表全部 `STRICT`、private、空，preflight 临时表不残留且 FK 全闭合。preflight 必须同时覆盖 17 张旧表、legacy authority 与 9 组保留 receipt prefix/kind；真实污染一张旧表后升级失败，user_version、ledger、metadata、旧数据与 DDL 必须完整回滚。关键 append-only、head CAS、worker chain、outbox allocation 与 dispatch 初始/推进状态、mount-v2 父回执 kind/time projection、cutover phase/binding、activation 与 atomic-finalization trigger 必须存在；模拟旧 V7 registry 打开 V8 时必须在 callback 前 fail closed | runtime + historical migration/schema/old-writer boundary，surface-neutral |
| `GATE-DB-17` | sqlite_worker_payload_projection_schema_v9 | 永久冻结历史 V9 migration 名称、checksum=`23018e6b…0dba0`、schema fingerprint=`ccb5b4d2…0ddf` 与 canonical migration-ledger root=`32ade765…b26a`；精确证明 V8 的 25 张表未改、V9 只追加 effect request、result payload、proof payload 三张 `STRICT`/private sidecar。fresh V9 与 authentic V8→V9 都必须保持外键闭合，前八条 ledger 逐字不变；孤立保留 receipt 等不闭合 V8 状态必须在 DDL 前失败，并把 user_version、ledger、metadata、旧数据和全部临时/正式 V9 对象原子回滚。旧 V8 writer/reader 打开 V9 必须在 callback 前 fail closed。本门只证明历史 sidecar 结构、镜像/闭合触发器、迁移和版本隔离；已经存在但未挂载的 sidecar writer与 plan-free auditor，其事务、重联和生产零边界分别由 `GATE-COMMIT-10 / GATE-PROJECT-13` 证明 | runtime + historical migration/schema/old-writer boundary，surface-neutral |
| `GATE-DB-18` | sqlite_runtime_timestamp_dispatch_hardening_v10 | 冻结历史 V10 `runtime_timestamp_dispatch_hardening_v1` 的 checksum=`1be88f26…af81baa`、schema fingerprint=`4ef1cb22…45bc5e` 与 canonical migration-ledger root=`f0ebef5c…32392d7`，并逐字证明 V8/V9 migration、schema identity 与 25+3 张表未被改写，V10 不新增业务表。迁移必须以 34 个 timestamp trigger 封闭 V8/V9 共 32 个弱时间列及生产保留 `transaction_receipts` 父行：required 值必须为 exact 24-byte 可解析毫秒 UTC，nullable 值只能为 NULL 或同样 canonical；append-only 表守 INSERT，可变 head/current 同时守 INSERT/UPDATE。另以 2 个 tuple trigger 把 `outbox_dispatch_head` 的 published 封成 genesis 或完整正数三元组，把 inflight 封成全 NULL 或 `published_sequence + 1` 的完整三元组。gate 只用冻结的 `MIGRATIONS[:10]` 真实执行 fresh V10、authentic V9→V10、逐字段 preflight、全部 guard 的合法/非法 replay、24-byte `CHECK(NULL)` 垃圾、边界值和部分 dispatch 元组拒绝；脏 V9 必须在任何 V10 对象落地前原子回滚，旧 V9 writer/reader 打开 V10 必须 callback-zero fail closed。本门是 historical gate，V11/V12 分别由 DB-19/20 证明 | runtime + historical additive hardening/atomic-upgrade/old-writer boundary，surface-neutral |
| `GATE-DB-19` | sqlite_mount_chronology_state_closure_v11 | 永久冻结历史 V11 `mount_chronology_state_closure_v1` 的 checksum=`48de0b25…f54cb`、schema fingerprint=`d92e9a6a…c1c74f` 与 canonical migration-ledger root=`39c8bc8f…14de70`，且逐字保留 V1–V10 registry。V11 只允许在 authentic empty V10 production namespace + legacy authority 上原子重建 `business_cutover_mount_receipts` 与 `business_cutover_head`：mount receipt 强制 `coverage_at <= install_at <= mount_at`，head 强制 `state_revision == phase_revision`、五阶段 pointer/authority/binding/CAS 闭包并重装完整 trigger 表面。gate 真实执行 fresh V11、authentic V10→V11、反向 chronology 与 split revision 拒绝；已有 production row 的脏 V10 必须在 DDL/ledger 落地前原子回滚，旧 V10 writer/reader 打开 V11 必须 callback-zero fail closed | runtime + historical rebuild/chronology/state/atomic-upgrade/old-writer boundary，surface-neutral |
| `GATE-DB-20` | sqlite_cutover_install_closure_v12 | 冻结 historical V12 `cutover_install_closure_v1` 的 checksum=`aa61960d…fccf35a2`、schema fingerprint=`4c265275…7195b226`、canonical migration-ledger root=`1537102b…37cbb77e` 与 V1–V11 不可改写前缀；V12 只在 authentic、legacy、production-empty 且 V5 source receipt 唯一/规范的 V11 上追加 artifact header/chunk、baseline atomic sidecar、install closure 四张 `STRICT`/append-only 历史表，当时总量为 17 张 transaction + 32 张 production/runtime（V8 25 + V9 3 + V12 4）。gate 真实执行 fresh V12 与 authentic V11→V12，证明脏 V11 在任何 DDL/ledger 落地前原子回滚；并反证 16 MiB 确切分块、header/chunk 顺序与长度、baseline inner/outer atomic 逐列 mirror、V5→V8→install provenance、source 时间不可原位改写、live repository revision 连续、closure 计数/root、initial head 前置以及 closure 后不可补 artifact/chunk/install sidecar或倒灌 install-time baseline。private fresh reader 必须从 raw rows/chunks 重组 artifact，重算来源、覆盖、摘要与回执 root，把可能因 deleted member 而不连续的 live ordinal 精确对回 mapping，才能为 current V13 authority reader提供 detached V12 closure 证据。这道门不冒充 fixed SQL install writer、cutover coordinator、full durable auditor 或 production authority router | runtime + historical additive install-representation/fresh-read/atomic-upgrade/production-zero boundary，surface-neutral |
| `GATE-DB-21` | sqlite_direct_predecessor_phase_bindings_v13 | 冻结current V13 `direct_predecessor_phase_bindings_v1`的checksum=`c65b56a6…08860e`、fingerprint=`28ea7b26…b25527`、migration root=`00eacfae…5a23a`与V1–V12不可改写前缀。V13只接受clean/authentic V12，在首张永久表前要求legacy authority、17张transaction与32张V12 production/runtime表、production reserved receipt全空；脏ledger/receipt原子回滚。四张`STRICT`/append-only表逐列承载OLD head、direct evidence、admission/maintenance、runtime/receipt/scheduler checkpoint和expected NEW head；正反guard与fresh all-field reader共同拒绝pointer fork、stale snapshot、前序证据拼接、manifest/checkpoint/source篡改及非canonical head。纯合同使用独立domain、累积typed pointer与确定性manifest。current表数固定17+36。本门只证明四阶段carrier/guard/reader；asset-provider writer由`GATE-COMMIT-15`另证，audit/activation/mount三个writer、coordinator、router与production caller仍不属于本门 | runtime + pure/current additive binding/semantic-read/atomic-upgrade boundary，surface-neutral |

### Project lifecycle (sdk/project_topology)

| Gate | 名字 | 检查什么 | 形态 |
|---|---|---|---|
| `GATE-PROJECT-01` | generation_fenced_namespace | create/replace/delete 必须单调推进 generation；旧 Context 的 activity lease 必须稳定拒绝；删除结果可由 tombstone 对账 | runtime |
| `GATE-PROJECT-02` | production_lifecycle_wiring | Repository、HTTP、CommitCoordinator 与 PipelineSupervisor 必须统一消费 topology/lifecycle seam；拓扑写强制 intent+幂等键并提供只读对账端点 | static |
| `GATE-PROJECT-03` | duplicate_is_canonical_only | duplicate 只复制 document/entities/assets；oplog、pipeline attempt、exports 与临时状态不得进入副本 | runtime |
| `GATE-PROJECT-04` | principal_scoped_intent_evidence | v2 证明区分“按当前 principal 观察”与“该精确 intent 已耐久认领”：纯 GET 不分配 claim 且 `intent_reserved=false`，真实写入后为 true；缺主体绑定的正向证据 fail closed，其他 principal 不得对账，耐久证据不落原始身份 | runtime |
| `GATE-PROJECT-05` | stream_generation_cursor_fence | broker 缺口/超前游标 runtime 在所有形态执行；api/web 才检查 server 的 generation、event epoch、连续 cursor、权限撤销与快照屏障，web 才检查 TS 消费端 | runtime + surface-aware static |
| `GATE-PROJECT-06` | demo_exact_principal_and_identity_priority | 身份优先级 runtime 在所有形态执行；api/web 才检查 HTTP/EventSource/媒体身份接线与 demo owner/generation CAS，不要求 headless CLI 携带 server/Web 文件 | runtime + surface-aware static |
| `GATE-PROJECT-07` | legacy_copy_never_follows_aliases | 旧根迁移的 nested symlink/hardlink/FIFO 矩阵和 production duplicate symlink 均须在发布前拒绝，证明两条路径共用安全复制原语；来源/外部目标不变、staging 清空、错误不泄露子项 identity | runtime filesystem |
| `GATE-PROJECT-08` | validated_head_consumers_and_cache_drift_fence | 对 production sdk/core/mod/scripts 做有限 AST 污点分析，禁止直接 read/write/open、JSON load/dump、`os.replace` 或安全复制 helper 原样读取 `document.json`，可信 `Document.open` / metadata/head seam 与 Document/storage 权威实现除外；marker 发布与启动恢复都校验 namespace id==Document id，legacy migration 持 EX+missing-ok，duplicate/replace/delete 持同一 head snapshot 到 copy/cutover，staging rewrite 验 direct child+prepared duplicate，并把已 `fstat` 的 stage `(st_dev, st_ino)` pin 交给 Document 重开复核；cache miss 发布前与 cache hit 返回前都必须对 `_v`，前者失败 retire 未发布 Context，后者 `commit_repair_required` fail closed，均禁止自动 reload。此 gate 不宣称已把 Document+OpLog 变成原子事务 | surface-aware static AST |
| `GATE-PROJECT-09` | strict_catalog_migration_barrier_and_fork_fence | 所有普通 repository/activity lease 必须先持 catalog barrier SH，严格迁移按 barrier EX→repository EX 取得唯一事务窗口，且 EX 必须覆盖调用者的两次扫描、写入与最终对账全生命周期。严格枚举 fail closed 检查 state/root/marker/Document identity，拒绝多余字段、孤儿/缺失/不安全 root、非终态、recovery/journal/staging/trash，只交付排序后 live handles，deleted tombstone 不当作 live member。`register_at_fork` 在 fork 前拦住 fd open→registry 窗口，child 只 close 继承 fd 并重置 registry/thread-local，绝不调 `LOCK_UN`释放 parent 的 open-file-description lock；严格迁移不进入稳定 public surface | runtime multiprocess + static AST，surface-neutral |
| `GATE-PROJECT-10` | repository_writer_admission_and_complete_state | 耐久 admission 记录以 epoch/revision 拦住所有普通文件读写，拒绝必须发生在 repository/project/Document I/O 和 callback 之前；状态只能不可逆地 `legacy R→pending R+1→sqlite R+2`，pending 不得回开 legacy。首次 CAS 前必须在 strict repository EX→EX 范围内原子安装 private canonical freeze marker，绑定 exact epoch/source revision/runtime generation/code digest；marker 落地后即使进程死亡，所有普通 legacy lease 也必须继续拒绝，只允许 strict migration 恢复，两次 CAS 和最终 SQLite lease 都必须重验该 marker。SQLite 路径只能在 barrier SH 下以独立私有 capability 取得 exact epoch/cutover/source+2/deployment lease，且不得反向取得 repo lock；prepared→record→committed 首装边仍可崩溃恢复。只有 production `Repository` bootstrap 可把严格闭合的 pre-v5 store 升级，普通 constructor 不会复活缺失/损坏的已安装栅栏；private strict snapshot 必须同时覆盖 live 与 deleted generation high-water | runtime multiprocess + static AST，surface-neutral |
| `GATE-PROJECT-11` | same_barrier_exact_repository_proof | private coordinator 必须在同一个 barrier EX→repository EX 生命周期内完成 S0→v4 writer/current projection→S1→v5 write+fresh exact reconcile→S2，且 S1/S2 重建必须验证当前 PID/thread 仍持两把 EX lock。v5 namespace ABSENT 才可调用旧 v4 writer；PRESENT 必须走只读 current projection，不得再次调用旧 writer。最终证明覆盖排序后的完整 live+deleted union，不调用 admission CAS、不返回 path/handle/projection/descriptors，也不进入 public surface；释放 barrier 后只承认 historical proof，不冒充当前 authority | runtime + static AST，surface-neutral |
| `GATE-PROJECT-12` | business_transaction_production_zero | 扫描 server、bootstrap、MCP、公开 persistence、CommitCoordinator 与 PipelineSupervisor，证明它们没有 import/call V6 plan issuer/verifier 或 Document candidate seam；atomic/V6/V8/V9/V10/V11/V12/V13 私有名称不得进入 `sdk.__all__` / public-surface lock。真实 current V13 普通启动后 topology admission 与 kernel marker 仍为 legacy，17 张 transaction 表与 36 张 production/runtime 表全部为空，因此“合同和 schema 已存在”不能被误读成“已挂载/已翻 authority” | runtime + production surface AST，surface-neutral |
| `GATE-PROJECT-13` | private_business_runtime_production_zero | 对整棵生产Python树做forward/reverse import/call/dynamic-name扫描，baseline、cutover/asset/audit-certificate/activation/final-mount/canonical-five-phase-head/install-closure/phase-binding、atomic-control composite、worker execution/result projection、production authority、通用production writer、plan-free rooted reader、V12 install-closure reader、V13 phase-binding mapper/semantic reader、private install writer、global cutover durable auditor、private phase-1 head finalizer、asset-provider writer、首次claim、schema-v9 payload writer、worker durable auditor、专用test-support与canonical-root合同只能存在于精确私有owner/白名单依赖和`_protocol` probe；生产模块不得经probe、`test_*`或别名桥接。所有模块禁wildcard，plan/证书/head符号不得进稳定SDK/public lock；每个capability-bearing helper的依赖、call site和reference site逐项冻结。migration/receipt canonical root各只有一个算法调用点；transaction fast fence不得做full audit，独立full-audit保留page/FK/receipt、revision/current、文件identity与fresh publish profile。reader/auditor只能SELECT，普通topology + current schema-V13启动仍是legacy、无runtime control，17+36张私有业务表全空。asset-provider writer已经存在但仍private/unmounted，其external observation issuer与production caller为零；本门不证明audit/activation/mount三个writer、coordinator、router或dispatcher已经完成 | whole-tree static forward/reverse boundary + exact adapter ownership + runtime startup + hot/full call-graph，surface-neutral |

### Authentication (04-user-facing/03-authentication)

| Gate | 名字 | 检查什么 | 形态 |
|---|---|---|---|
| `GATE-AUTH-01` | hash_password_salt_random | 同密码两次 hash salt 不同 → hash 不同 | runtime |
| `GATE-AUTH-02` | hash_password_verify_round_trip | hash + salt 能 verify；错密码失败 | runtime |
| `GATE-AUTH-03` | user_store_register_authenticate | 注册后能用同密码 authenticate | runtime |
| `GATE-AUTH-04` | user_store_auth_failed_unified | 用户不存在/密码错统一 `auth_failed` + 同 message（防枚举） | runtime |
| `GATE-AUTH-05` | session_token_lookup | login 返 64 hex token；get_user_id 还原 | runtime |
| `GATE-AUTH-06` | session_expired_returns_none | TTL 过期 token get_user_id 返 None + 顺手清 | runtime |
| `GATE-AUTH-07` | cmd_dispatch_enforce_login | `enforce_login=True` + 空 user_id + source≠system → `auth_required` | runtime |
| `GATE-AUTH-08` | credential_store_transaction_boundary | Credential 公开视图不泄密、provider 不可原地切换、default 互斥、存储文件 0600 | runtime |
| `GATE-AUTH-09` | operation_manual_trust_boundary | 仅 web：Header 与 ProjectPicker 必须共享一个 DOMPurify allowlist、运行时品牌和唯一 HTML sink；fragment 自带 CSS/脚本/事件/危险 URL 必须被拒绝 | web-only static + TS runtime tests |
| `GATE-AUTH-10` | acl_shape_and_document_cas | durable ACL fail-closed 与 command 级 Document CAS 在所有形态执行；只有 web 才要求 AclPanel/useDocument 传递并守住渲染版本 | runtime + surface-aware static |

### Protocol (_protocol 本身)

| Gate | 名字 | 检查什么 | 形态 |
|---|---|---|---|
| `GATE-PROTO-01` | change_kind_valid | EntityChange.change_kind 必须在 CHANGE_KINDS 5 态 | runtime |
| `GATE-PROTO-02` | broadcast_payload_format | `to_broadcast_payload` 输出含 `type=entity_change` + 必需字段 | runtime |
| `GATE-PROTO-03` | snapshot_present_unless_hard_removed | EntityChange.snapshot 不为 None 除非 change_kind=hard_removed | runtime |
| `GATE-PROTO-04` | entity_change_ordering | `affected_entities` 返回按 (entity_type, entity_id) sorted | runtime |

### Type Library (07-type-library)

| Gate | 名字 | 检查什么 | 形态 |
|---|---|---|---|
| `GATE-TYPE-01` | type_id_format | 注册表全量 id 正则 + 三写法等价 + 畸形拒绝 | runtime |
| `GATE-TYPE-02` | type_fragment_self_validate | 每条 fragment 的 example 灌真 make_validator(13 原型全量) | runtime |
| `GATE-TYPE-03` | type_deep_copy | 改 zt.* 返回值不污染库 | runtime |
| `GATE-TYPE-04` | type_extend_additive_only | extend 非 x_ 前缀 / zt.field 约束键 → `type_extension_violation` | runtime |
| `GATE-TYPE-05` | type_seal_and_tighten_only | sealed 拒 narrow(`type_seal_violation`);非 sealed 只紧不松 | runtime |
| `GATE-TYPE-06` | type_provenance_annotations | x-ze-use / x-ze-archetype / x-ze-sealed / x-ze-ext 注解必在 | runtime |
| `GATE-TYPE-07` | type_registry_doc_in_sync | TYPE-REGISTRY.md fenced 表 ⇄ list_fragments() 双向一一对应+版本/标记不失真 | script(scripts/type_registry_check.py,受管随 toolchain 下发) |
| `GATE-TYPE-08` | type_envelope_conformance | 信封 round-trip 逐字段保真 + sha 不变 + 版本不符结构化拒绝 | runtime(envelope) |
| `GATE-TYPE-09` | type_mod_archetype_no_drift | mod 带注解 schema 的库属字段=库内 fragment@version 的合法收紧(sealed 全等);**下游真把关** | script(scripts/type_registry_check.py) |
| `GATE-TYPE-10` | type_broker_proposal_conformance | broker 协商层:opportunistic 导入恰好落地 proposal.accept、不半导入 required_field_unmet 实体、锚子物按 AP-03 丢弃、L0 资产可读(详尽套件 test_broker.py) | runtime(envelope) |
| `GATE-TYPE-11` | type_harvest_rule_legality | broker 收割规则(L1)合法性:build_document fail-fast 拦非法 rule(必填 entity_ref / 未覆盖必填 / 坏角色格式 / 桶未声明),合法 rule 放行;**下游真把关**(规则是下游自写,其 build_document 验自己的规则)。形态由设计的 script 修正为 runtime —— harvest_rules 是运行时声明非静态 schema | runtime(document.validate_harvest_rule) |

## 3. Gate 形态分类

| 形态 | 含义 | 怎么跑 |
|---|---|---|
| **runtime** | 跑代码 / 构造数据 → 检测行为 | pytest 测试驱动 |
| **static** | 扫源码（grep / AST）→ 检测违反 pattern | 单独脚本（CI 跑）|
| **提示性** | 难自动检测，靠 code review / 文档约束 | 文档提示，未来加 lint |

## 4. 跑 gate

```bash
cd 01-core/_protocol/
python -m pytest test_protocol.py -v    # 跑全部 runtime gate

# 或显式调
python audit_gates.py --all              # 跑全部
python audit_gates.py --leaf DOC         # 只跑 document 相关
python audit_gates.py --gate GATE-OP-02  # 只跑指定一个
```

输出：

```
[PASS] GATE-DOC-01: document_version_conflict
[PASS] GATE-DOC-02: document_collections_index_consistent
[PASS] GATE-DOC-05: save_policy_explicit_and_safe
[PASS] GATE-DOC-07: safe_entity_file_store_commit_boundary
[PASS] GATE-DOC-08: multiprocess_durable_head_fence
[PASS] GATE-DOC-09: validated_document_head_authority
[PASS] GATE-DB-07: strict_legacy_scan_payload_receipt
[PASS] GATE-DB-08: sqlite_legacy_import_schema_v3
[PASS] GATE-DB-09: sqlite_legacy_import_terminal_shadow
[PASS] GATE-DB-10: sqlite_legacy_catalog_historical_seal_v4
[PASS] GATE-DB-12: sqlite_repository_exact_proof_v5
[PASS] GATE-PROJECT-09: strict_catalog_migration_barrier_and_fork_fence
[PASS] GATE-PROJECT-11: same_barrier_exact_repository_proof
[FAIL] GATE-OP-02: oplog_hash_chain_intact
       原因: 篡改 entry 0 后 verify_chain 仍返 True (应返 False)
       证据: ...
[SKIP] GATE-PIPE-01: stage_checkpoint_for_cancel (static gate, 需 lint 工具)
```

## 5. 当前实装范围

**audit runner 已实施 gates**：
- DOC-01, DOC-02, DOC-05, DOC-06, DOC-07, DOC-08, DOC-09, DOC-10
- ENT-01, ENT-02, ENT-03
- AST-01, AST-02, AST-04, AST-05, AST-06, AST-07
- OP-02, OP-03, OP-04, OP-06, OP-07, OP-08
- CMD-01, CMD-03, CMD-04, CMD-05, CMD-07
- COMMIT-01, COMMIT-02, COMMIT-03, COMMIT-04, COMMIT-05, COMMIT-06, COMMIT-07, COMMIT-08, COMMIT-09, COMMIT-10, COMMIT-11, COMMIT-12, COMMIT-13, COMMIT-14, COMMIT-15
- CC-04
- CONF-01
- FT-01, FT-02, FT-03, FT-04, FT-05, FT-06, FT-07
- CAP-01, CAP-02, CAP-03
- RB-01, RB-02, RB-03, RB-04, RB-05, RB-06
- UNDO-01, UNDO-02, UNDO-03, UNDO-04, UNDO-05
- RT-01, RT-02, RT-03, RT-04, RT-05, RT-06, RT-07
- DB-01, DB-02, DB-03, DB-04, DB-05, DB-06, DB-07, DB-08, DB-09, DB-10, DB-11, DB-12, DB-13, DB-14, DB-15, DB-16, DB-17, DB-18, DB-19, DB-20, DB-21
- PROJECT-01, PROJECT-02, PROJECT-03, PROJECT-04, PROJECT-05, PROJECT-06, PROJECT-07, PROJECT-08, PROJECT-09, PROJECT-10, PROJECT-11, PROJECT-12, PROJECT-13
- AUTH-01, AUTH-02, AUTH-03, AUTH-04, AUTH-05, AUTH-06, AUTH-07, AUTH-08, AUTH-09, AUTH-10
- PROTO-01, PROTO-02, PROTO-03, PROTO-04
- PIPE-04, PIPE-05, PIPE-06, PIPE-07, PIPE-08, PIPE-09
- AI-01, AI-04, AI-05, AI-06（`AiGovernor` 组合既有原语:provenance/safety/cost/approval）
- TYPE-01, TYPE-02, TYPE-03, TYPE-04, TYPE-05, TYPE-06, TYPE-08, TYPE-10, TYPE-11（详尽套件在叶子 test_type_library.py / test_envelope.py;TYPE-07/09 经 `scripts/type_registry_check.py` 在 verify_all 强制,不进 GATES dict）

机器源以 `audit_gates.py` 的 `GATES` 为准(`run_all()` 全 PASS);本清单为人读摘要,可能略滞后。

**未实施**：
- 其余 static gates（DOC-03, DOC-04, OP-01, CMD-06, PIPE-01, PIPE-02, CC-01, CC-02, CC-03, RB-07, RB-08）— 等加 lint / AST 扫源码工具时再做；DOC-05 已用 AST 实施
- 其余 runtime gates（AST-03, CMD-02, CONF-02, ENT-04, OP-05, PIPE-03）— 其中部分已有叶子测试覆盖，仍需独立 gate 证据
- **AI Coworker 余项（AI-02/03/07）** — AI-02/03/07 是对既有行为的断言,可即实施;**AI-01/04/05/06 已由 `AiGovernor` 实施**(本轮 P1,见 05-ai-coworker/ai_governor.py)。日累计 cost ledger 留后续。
- **cross-skill-call / offline-first** — 这两叶子本轮只立规则、未注册 gate（cross-skill 候选前缀 `GATE-XSK-*`，待传输层定稿；注意 `CC` 已被 concurrency 占用）

> 注：本表**注册 162 个 gate** 命名（含规划/待补），其中 **140 个由 audit runner 实施**，另有 `scripts/type_registry_check.py` 的 TYPE-07/09（verify_all 强制），**合计 142 个可执行协议 gate**；**剩余待补 20 个**。当前分类计数包含 **DB×21**、COMMIT×15、PIPE×6、PROJECT×13。注册 ≠ 实施是有意的：规则先立，impl 随项目补。

## 6. 跟其他叶子 SPEC §9 的关系

- 各叶子 SPEC §9 内可以**引用**本表 gate 编号（如 document SPEC §9 写"Gate-DOC-01: ..."）
- 不在 SPEC 内重复 gate 实施细节；细节归本文档 + audit_gates.py
- 新 gate 加入时：本文档 §2 加一行 + audit_gates.py 加 check fn + test_protocol.py 加测试
