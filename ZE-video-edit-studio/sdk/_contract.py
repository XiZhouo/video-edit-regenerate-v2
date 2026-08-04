"""CORE 层契约版本，供 Skill.Sync 判定 same / compat_bump / incompat。

接口版本描述 ``sdk.__all__``、公开签名与 handler / bootstrap 协议；破坏性变化由
``PUBLIC_SURFACE.lock.json`` 门禁识别。Skill.Sync 的 checksum 另有唯一职责：对当前
Surface 实际会复制的 core 受管文件做纯内容摘要，不 import 运行时代码，也不把 ops
导出或 CPython 反射细节混进 core。完整迁移说明见 ``01-core/_protocol/MIGRATIONS.md``；
更早记录保留在 Git 历史中，避免继续把数百条历史压进源码注释。

版本规则：
  - MAJOR：破坏性变化，Skill.Sync 拒绝自动覆盖下游。
  - MINOR / PATCH：加性或兼容变化，Skill.Sync 可复制并更新 lock。
"""

# 3.58.1: Local Link 完整性修复(PATCH)——保持 server 单写、本地只读镜像原设计，
#         但把镜像相对路径收敛为封闭 ASCII 形状，服务端扩展名不能再改变写入目录；
#         快照 generation 必须与当前项目一致，内容寻址资产落盘前重算 SHA-256，缺失或
#         摘要不符时不推进 document.json/lastBackupAt。并发版本变化会排队再镜像，unlink
#         先撤销旧异步 epoch，持久句柄删除失败时不再冒充已经解除关联。无 Python 公开面变化。
# 3.58.0: 本地 Update Agent(加性 MINOR)——新增受信 Workspace/Launcher 专用的
#         LocalUpdateAgent / TrustedReleaseKey / InstallAuthorization / LocalUpdateError。
#         代理只写独立软件安装根，不接收项目路径、不暴露 HTTP 路由；使用预置信任根验证
#         Manifest 与 Artifact 两层 Ed25519 签名，持久化最高可信 Manifest 防重放/同版换包，
#         安全解包到不可变版本目录，通过 current.json 原子切换，健康检查失败或进程中断后
#         回指上一健康版，并写无项目路径的 Receipt。Workspace 平台安装包和项目 Schema
#         迁移继续是独立交易，不能由本代理静默执行。
# 3.57.0: 部署可选登录入口(加性 MINOR)——新增无密钥 `/api/auth/config` 与
#         minimal-local/federated/hybrid 三模式；本地模式把邮箱形状仅作登录名且明确不做
#         邮件验证，hybrid 默认仍飞书优先。既有 UserStore/Session/项目 ACL/user_id 均不改写；
#         前端只按服务端能力显示入口，禁用方法在后端同样拒绝。新增 TypeScript 配置类型/接口，
#         并修 record_login 遗漏 password_algo 导致新 PBKDF2 账号二次登录失败；Python
#         sdk.__all__ 不变。
# 3.56.0: AI 通道层(加性 MINOR)—— image_call 按新配置 ze.image.route 分发四路线
#         (service 原样默认 / codex-cli 个人订阅原生 image_gen / openai-api / gemini-api),
#         公共签名与返回契约 list[bytes] 不变(凭据经线程上下文解析,下游零改动);显式路线
#         不可用抛 requires_credential 绝不静默回退,计费路线 retries=0。新增 provider
#         gemini_api_key(绑定即验证 gemini_validate_key,新导出)、codex capability 补 image
#         (存量读侧并集迁移)。codex 子进程收敛单一 _codex_exec 原语(进程组 kill),
#         生图密封调用(env 白名单剔 OPENAI_API_KEY/版本 pin/provenance 事件流校验),
#         同 CODEX_HOME 跨功能互斥防 OAuth 互踢。规约成文 AI-CHANNEL-RULES.md。
# 3.55.0: entity.batch_add 幂等选项(加性 MINOR)—— args 新增可选 on_conflict="error"(默认)|
#         "skip":skip 下已存在同 type+id 的项静默跳过(不产 patch、不覆盖);supervisor 透传
#         stage 输出可选字段 created_entities_on_conflict。默认行为零变化。根治「存在性检查在
#         dispatch 时、batch_add 在 stage 完成时」的确定性 id 竞态(design D3 自动建帧反哺)。
# 3.54.0: B3 前端门面收口(纯加性,零 Python 面变化)—— 01-core/04-user-facing 新增稳定门面
#         barrel `index.ts`(按需显式再导出 App.tsx 与全舰队 02-mod/05-renderers.tsx 实际
#         import 的符号面;旧深路径全部保留可用,不移动/不删除任何文件或既有导出);webapp/src
#         的 App.tsx 壳体拆出两个组合点 AppChrome.tsx(chrome 组装:Header/Footer/copilot/
#         toasts/guide)与 AppRenderers.tsx(路由 + mod renderers glob 注入),DOM 结构与
#         行为逐节点等价、下游 API 零变化。sdk.__all__ / 签名 / handler-bootstrap 协议不变,
#         新增导出面 = compat_bump 3.53.0→3.54.0。
# 3.53.0: W2d 凭据落盘加密升级 —— credential 密文新增 `sealed-v2` 信封(AES-256-GCM +
#         HKDF-SHA256 域分离子钥 + 记录身份 `owner|id|provider` 作 AAD),修 `sealed-v1` 手搓
#         keystream/MAC 复用单钥且密文未钉死其记录的隐患。装了 `cryptography`(requirements 新增
#         `cryptography>=42`)即写 v2;缺库优雅降级仍写 v1(import 失败不崩,fleet 安全第一);
#         `_open_sealed` 按 schema 分支(v1 手搓解 = 兼容+迁移源,v2 走 AEAD),读 v2 但无库 =
#         fail-closed(`credential_crypto_unavailable`,绝不静默回退明文);upsert 时对存量 v1 惰性
#         reseal-v2(解不出保留 v1 零丢)。私有 `_seal`/`_open_sealed` 签名新增 owner/id/provider
#         关键字(默认空,v1 忽略),sdk.__all__ / 公开签名 / handler-bootstrap 协议不变,v1 存量
#         读写全兼容 = compat_bump 3.52.0→3.53.0。
# 3.52.0: 生成调用超时可配置化 —— `image_call` / `llm_call` 的 `timeout` 由写死 int(180 / 120)
#         改为 `int | None = None`:显式传 int 仍最高优先(既有 caller 不变),传 None(缺省)时读
#         `ze.image.timeout_sec`(默认 600)/ `ze.llm.timeout_sec`(默认 120)。并把 HTTP 路读响应体
#         阶段的裸 `TimeoutError` 在 `_post_json` 显式包成 ZeError("model_call_timeout")=**超时不再
#         被 `_with_retry` 重试**(此前落通用 except 被当瞬时 I/O 重试,单击 3×timeout+三倍计费)。
#         签名默认值变更(int→int|None)——PUBLIC_SURFACE.lock 的 image_call/llm_call 签名 pin 需随
#         lock-dance 重生成;行为对既有显式 int caller 完全兼容 = compat_bump 3.51.0→3.52.0。
# 3.48.0: 系统 1b「github 仓库关联」—— 新增 per-user `github_repo` credential provider
#         (sealed PAT + owner/repo/ref 关联元数据,owner/repo/ref 进 public_dict/查关联可回显,
#         token 仍只写不回)、只读 github contents 客户端 sdk.github_read_path(rate-limit 感知、
#         二进制/超大结构化拒、诚实上游错误),以及新只读 MCP 元工具 `ze_file`(读关联仓库相对
#         路径,过项目 ACL 同 ze_read_asset,token 只服务端用)。全加性:新 provider/新 sdk 导出/
#         新元工具,旧接口签名不变 = compat_bump 3.47.0→3.48.0。
# 3.42.0: GalleryEditor 加 opt-in `duplicateCommand` prop —— mod 在 cardlist slot 声明
#         (如 entity.duplicate)后,卡片动作区渲「复制」选中项,对每张选中卡逐个 dispatch
#         {type,id,expected_version}(与 entity.delete selection fan-out 同形,避免批量 {type,ids}
#         打单实体 handler 崩)。slotAdapters 透传 view.duplicateCommand;未声明的 gallery 行为不变。
#         加性 opt-in、无破坏 = compat_bump 3.41.0→3.42.0。
# 3.41.0: add bounded opt-in Timeline range creation, guarded schema replay,
#         and same-tab anonymous demo reload recovery.
# ``dynamicTracks`` derives occupied lanes and keeps one trailing empty lane
# while the bounded capacity has room; ``maxTracks`` defaults to 64 and is
# itself capped at 512 so malformed data/config cannot create unbounded DOM.
# ``rangeCreate`` alone enables pointer-drag creation and still requires an
# explicit addCommand.  Empty editors expose one actionable lane only when an
# opt-in and addCommand are both present.  Range validity compares endpoints
# after independent snap+clamp, so same-grid jitter and right-boundary motion
# cannot create an entity.  Draft JSX emits only data-draft plus left/width;
# CSS 19.4 owns opacity, outline and pointer hit behavior.  Defaults preserve
# the historical fixed-track, Empty and double-click behavior.  One shared
# track normalizer also keeps malformed historical track values visible.
# 3.40.0: add a private, dormant delegated Pipeline mutation request that
# revalidates the exact FileCommitStore, Document, canonical history runtime,
# current fresh complete-history reduction, attested Start authority, work-item
# to entity binding, stage, target fields/lifecycle, cardinality, current
# command policy digest, and the actual worker Actor/source both when prepared
# and when consumed.  Requests are keyed-integrity protected, one-shot,
# noncopyable/nonserializable, and invalid after fork or history movement.  They
# carry no execution authority and cannot dispatch, mutate, register a worker,
# create an action/manifest/run, issue or apply Control lifecycle, or use dynamic
# entity create.  They only read reducer-projected Control facts to deny stale,
# terminal, or reconcile-required requests; legacy lifecycle is never promoted;
# Batch 5 still owns grant registry and Supervisor lifecycle.  The attestation
# ledger and canonical history graph now fail closed under partial install,
# stale/tampered facts, same-id foreign Documents, head drift, or replacement
# workers.  GATE-PIPE-09 proves this seam stays private and dormant.
# 3.39.0: atomically activate sealed ze.pipeline-start.v2 / control.v2 writes
# with a reserved-id factory, exact active registered-command root snapshots,
# mandatory CommitCoordinator prospective complete-history preflight, and a
# committed+durable fresh FileCommitStore/OpLog reconcile.  Public Operation
# construction still rejects v2; v1 remains carrier-only.  Successful fresh
# reduction creates only a private process-local, noncopyable/nonserializable,
# fork-invalidated attestation retained outside receipts/events/SDK.  It is not
# a worker grant.  Post-durable reconcile failures expose a typed replay-unsafe
# classification consumed consistently by HTTP/MCP/CLI/confirmation and the
# idempotency ledger; preflight failures remain rollback-safe.  Protected
# Supervisor starts derive Start and placeholder root identity from the same
# handler and freeze retry_failed(None) to one exact failed-item scope under
# lock, but deliberately create no action/manifest/run/worker before the Batch
# 5 grant lifecycle.  Delegated mutation remains dormant Batch 4 work.
# 3.38.0: add the private, dormant ze.pipeline-start.v2 / control.v2
# authority codec and complete-history reducer.  v1 remains a permanently
# untrusted resource-bounded carrier because it was writable before sealed
# semantics existed.  Start exact targets must equal the WorkList; bounded
# dynamic creation is separate, wildcard-free creation_scopes whose type,
# fields, per-operation and per-run limits are authorized by the root policy.
# The v2 Control envelope is 2,179,072 bytes / 12 levels / 4,123 nodes so the
# declared 4,096-item maximum is reachable.  StageSpec/StagePlan and
# WorkItem/WorkList now deep-own one canonical execution intent shared by
# runtime and metadata serialization.  The reducer rechecks the complete
# chain/head, contiguous initial closure, exact entity.add/reverse snapshots,
# Start self-hash and every Control transition.  Breakpoints reduce to an
# ordered active set; clearing one does not resume and leaves reconciliation
# sticky.  Only bounded facts are returned.  The generic Operation writer
# still rejects v2.  A sealed reserved-ID factory, v2 writer activation,
# mandatory CommitCoordinator pre-store/reconcile must land atomically in
# Batch 3.  Reconcile may only return a private sealed attestation; grant
# issuance/registry remains a Batch 5 lifecycle concern.  3.38 has neither.

# 3.37.0: Operation/new_operation append optional pipeline metadata after the
# complete 3.36 positional/keyword surface.  None remains omitted from the
# canonical body and retains the historical pickle argument shape; present
# metadata is detached and hashed independently from external provenance.
# The carrier accepts only ze.pipeline-start.v1 / ze.pipeline-control.v1 and
# applies their 4 MiB/64 KiB byte, depth, node, and string budgets before full
# encoding and again after decoding.  Command/history/oplog introspection and
# HTTP/MCP/CLI projections share one private redactor that omits the full
# authority body and continues to use bounded Pipeline status.  This
# release does not issue a Pipeline
# grant: sealed construction, fresh OpLog reduction, durable reconciliation,
# delegated mutation, Supervisor lifecycle, and cold recovery remain the next
# authority batches.

# 3.36.0: opt-in entity mutation policies bind closed field/lifecycle grants
# to a registered business root with mutation_authority=True and to the real
# Document delta, frozen Actor/source, exact ordered Operation/reverse audit,
# and final canonical cardinality.  The new authority flag trails the complete
# legacy CommandSpec positional surface with default False; new callers pass it
# by keyword, and generic entity.* helpers never become roots.  Patch appends
# optional provenance/position/ancestry envelopes; absent envelopes are omitted
# from to_dict so historical Operation bodies and OpLog hashes stay byte-stable.
# Production command introspection consumes detached registered_declaration(s)
# rather than treating the compatibility get/list CommandSpec objects as truth.
# DocumentMutationSnapshot keeps its legacy six-argument constructor; added
# schema/policy/admission authorities remain init=False internals.  Policies are
# runtime-only: framework bootstrap installs them only while unpublished,
# complete registry validation seals the Document one-way, and protected live
# writes then require coordinator coverage plus command/history authority.
# Bootstrap now also binds one framework-private HistoryAuthority for the exact
# Document/OpLog/UndoController/CommitCoordinator/CommitStore graph, project
# generation, and runtime revision before recovery or Context publication.  One
# coordinator transaction owns one fresh canonical head/rows snapshot; history
# source tickets bind exact source bodies to one verified replay Operation, and
# per-append permits bind the same Operation objects/bodies/order to the store.
# Only an exact durable OpLog suffix advances the trusted head and makes a
# deferred replay confirmable.  Freeze retains the path deny lease; bootstrap-
# only retirement plus a fresh Context is the reload boundary.  Fork children
# discard inherited capabilities and must bootstrap fresh runtime objects.
# HistoryAuthorityError exposes typed outcome_unknown and durability_phase;
# the coordinator may attach copied JSON-like commit_facts.  HTTP, MCP, CLI,
# Pipeline, and idempotency consume that type/boolean rather than code/message.
# IdempotencyStore.run adds the trailing keyword-only optional
# outcome_unknown_classifier=None.  Omitted callers retain prior behavior;
# typed unknowns (and classifier failures) retain a repair tombstone instead of
# re-authorizing replay.  FileCommitStore remains Document-generation-first /
# OpLog-second across separate durability domains: authority verifies/freeze-
# fences the suffix but does not claim one physical commit point, multi-process
# authority, or safe auto-reload.
# Runtime history identities/tickets never enter business snapshots or OpLog.
# This is a same-process framework misuse fence, not a hostile Python sandbox.
# Documents/types without policies preserve prior CRUD/delta/cardinality
# behavior; registration-time execution/security freezing applies to every
# CommandRegistry, so later mutation of its public CommandSpec is introspection
# compatibility only and no longer rewrites runtime authority.
# 3.35.1: optional WorkItem recovery_identity_fields bind untrusted manifest
# seed members to the already committed Document entity before marker repair or
# worker recovery.  Malformed/mismatched declarations fail closed; old items
# without the opt-in declaration keep their prior recovery behavior.
# 3.35.0: Scene3D shell controls are explicit fail-closed capabilities, and
# declarative FormAction results can request one framework-owned JSON download.
# The App consumes only a committed operation file intent paired with a
# succeeded after-commit receipt in the same project generation/namespace;
# the server fixes the route, directory, MIME, ACL, filename, and nofollow IO.
# 3.34.1: managed admin-route tests now use a framework-owned config key,
# keeping the core suite independent of Build V3's demo-only mod settings.
# Production config/CAS behavior and the public surface are unchanged.
# 3.34.0: R1.1b adds Scene3D personal/domain camera ownership, bounded and
# settled scoped camera persistence, durable reset/stale-writer protection,
# and stable-id Property/Form disclosure memory.  Personal state remains local
# per-device view state and never enters Document/Operation/undo.
# 3.33.0: R1 adds the first shared scoped personal-view store (Gallery and
# Canvas2D), signs placeholder identity end to end through supervisor recovery
# and SSE, and makes Table/Form run presentation entity-local and non-blocking.
# The new EntityRunStatus flag and frontend preference surface are additive.
# 3.32.1: DOC-09 audits repository-owned source only and prunes virtualenv,
# dependency, build, cache, and runtime-data trees before parsing Python.  This
# prevents the verifier's host interpreter from rejecting unrelated installed
# syntax while preserving the production authority checks.
# 3.32.0: R1.2a makes entity run state non-blocking in CardList, propagates
# unknown progress honestly through supervisor/chain/footer, and keeps Timeline
# as the second real editor consumer.  Python SDK symbols stay unchanged; the
# additive version also propagates the managed frontend/runtime implementation.
# 3.43.0: W2d hardens per-web-user credential binding.  ZE_SERVER_MODE forces
# fail-closed (requires_credential) for actors with no active CLI binding, and
# a lone ambient fallback now writes an audit event.  Revocation removes the
# per-credential isolated home dir (refresh token) from disk, not just the
# record.  New STATUS_NEEDS_REAUTH + CredentialStore.mark_needs_reauth let the
# copilot flip a login that no longer authenticates and skip it thereafter.
# All additive: existing symbols, statuses, and public surface are preserved.
# 3.43.1: project_topology now hardens self-built directories (data/credential
# roots, staging/trash, project stages) with an explicit mkdir(mode=0o700) plus
# os.chmod(0o700) on creation, decoupling their permissions from the ambient
# umask so a lax parent chain or historical 775 cannot leak them to the group.
# Pre-existing directories and ancestor chains are left untouched; no public
# symbol changes.
# 3.44.0: copilot IntentContext injection (S3). ze_entities gains optional
# include_data=true (ACL-gated full entity data via the same _member_context
# gate); ze_manual added to MCP meta-tools (paginated markdown manual). New
# pure sdk.copilot_context module composes the floating-copilot system prompt
# into two structurally isolated channels — instruction (what the tool is,
# fallback-derived from ai_flow + manual + command semantics; plus how it
# works) and data (current context: selection structure, oplog signals, task
# signals). All additive; entity data/oplog/selection never enter the system
# channel. A clean seam (static_tool_layer) leaves the tool_understanding
# instance path for S2/U0-02 to wire later.
# 3.45.0: enum-option + summary-chip human-label mechanism. field_types adds a
# shape gate for the x-ze-enum-labels field-schema key ({enumValue: str |
# {locale: str}}; other x- keys still pass generically). Frontend gains
# enumOptionLabel(fs, value, locale) — enum dropdowns (FieldRow/ParamPanel) now
# render the declared human name (bilingual, with legacy flat enumLabels as
# fallback, else the raw token); GridZone summary chips label from the field's
# i18nLabel (schema label / x-ze-label-i18n) instead of the bare field key,
# via a new optional fieldSchemas prop threaded GalleryEditor→GridZone. All
# additive and fallback-safe: undeclared fields/enums render exactly as before.
# 3.45.1 (PATCH): pipeline supervisor drops a stage result when cancel_event is
# set during the run (single- and multi-stage paths) instead of committing it —
# prevents cancelled pipelines from writing Document/OpLog (golden-nugget
# 909720051 upstreamed to core; +2 regression tests). Bug-fix only, no API change.
# 3.46.0 (MINOR): MCP gains a read-only ze_read_asset meta-tool (mode=meta/text/
# describe) so external agents can see project assets. All three modes pass the
# same durable ACL gate as HTTP /api/asset (now shared in sdk.asset_access:
# durable_asset_access + asset_ref_for_read_locked) — only this project's local
# blob or a blob referenced by the canonical Document is served; otherwise
# acl_denied, no blob. describe returns a native MCP image content block (base64)
# so the client model views the image directly. Additive, read-only, no Operation.
# 3.47.0 (MINOR): webapp gains a client-side "link local folder" module
# (webapp/src/lib/localLink*.ts + useLocalLink + LocalLinkSettings, wired via the
# existing header settings_menu_append seam). File System Access API keeps a
# persistent FileSystemDirectoryHandle in IndexedDB and mirrors the project one
# way server→local (document.json + content-addressed asset blobs, incremental);
# reconnect via requestPermission, import via the existing manifest.preview/import
# chain. Single-writer invariant unchanged: server still owns .data/, the linked
# folder is a read-only mirror/export target, never a second writer. Graceful
# degrade when showDirectoryPicker is absent (only F1/F2 export/import shown).
# Frontend-only, additive, no Python sdk.__all__ change.
# 3.49.0 (MINOR): guided-tour logic layer (系统 3a). New sdk.guided_tour compiles
# the U0 ze.tool-understanding.v1 key_flows into a projected guided_tour (closed
# step kinds say/point/highlight/act/wait/ask; actor→指路 vs 代操作 mode; confirm
# derived from command_semantics.confirmation_intent==human_required, never
# hand-written; semantic_target→cmd:/entity:/control:/tool: targets, with a
# reserved presentation slot for the system-3b overlay). No instance → fallback
# derivation from ai_flow/manual so all tools stay inferable. IntrospectionMeta
# gains set_tool_understanding/tool_understanding; describe_context (ze_describe)
# gains a guided_tours summary array. New ze_guide MCP meta-tool drives a tour:
# guide steps stream as broker guide_step/guide_end frames on the existing SSE
# channel; act steps dispatch through the same ACL/CAS + confirmation gate as
# command tools (high-risk → pending, never self-approved). Additive, read paths
# unchanged; no lock touched.
# 3.50.0 (MINOR): guided-tour frontend GuideLayer (系统 3b). New read-only core web
# route GET /api/context/tours projects full tour steps for the human『教我用』local
# player (summaries already in describe). Webapp GuideLayer consumes guide_step/
# guide_end SSE frames (routed off the existing /api/events stream via an in-memory
# guideBus, no second EventSource) and renders sprite/spotlight/point/ghost-ripple,
# resolving cmd:/region:/guide:/control:/editor:/entity: targets to live rects.
# Governance red line: act only draws a ghost-click; GuideLayer never synthesizes DOM
# mouse events and never executes commands — real execution stays on the command bus
# (ze_guide dispatch for AI, or the user's own click on the pointed governed button).
# Additive, read paths unchanged; no lock touched.
# 3.51.0 (MINOR): guide-shell visual full volume (系统 4, pure presentation). The
# managed webapp GuideLayer (webapp/src, a core sync surface) upgrades the single-color
# orb into a Siri layered guide-shell: __aura/__core/__spark/__ring composed from the new
# --ze-orb-a/b/c oklch multi-hue tokens (css 19.6.0), an 8-state presentation FSM
# (hidden/appearing/idle/moving/pointing/framing/acting/dismissing) in a new pure module
# webapp/src/lib/guideMotion.ts, and orb↔cursor↔frame form morph driven by step kind +
# anchor presence, all --ze-spring eased with prefers-reduced-motion fallbacks. useGuideRuntime
# gains an additive `ending` flag (guide_end linger → dismissing) — frame consumption, target
# resolution, and the three drive paths are unchanged. Red line preserved: still pure
# presentation — no dispatch/synthetic DOM event/command bus reference; act still draws only a
# CSS ghost-ripple. No Python sdk.__all__ change. Frontend-only, additive; no lock touched
# (local core.lock relock + downstream sync deferred to the G1 migration train).
# 3.59.0 (2026-07-19) — 精灵变形体 v2 + AI 行为具象化(加性 MINOR)。详见
# 01-core/_protocol/MIGRATIONS.md 3.59.0 条:PendingStore.peek(只读)、
# sdk.persistence.pending_confirmation_frame(帧无 nonce)、MCP 两派发点 pending 广播、
# GET /api/command/pending-approval(cookie 专属)、前端 spriteEngine/aiActivityBus/
# useAiActivity。sdk.__all__ 无变化;handler 签名不变 → 加性。
# 3.60.1 (2026-07-20) — undo/redo 普通文档修复(兼容 PATCH)。_history_operation_scope
# 的 mark_history_replay_ticket 从「仅 protected」改为「持票即推进」:未声明 mutation
# policy 的普通文档票据曾停在 BOUND,提交预检要求 REPLAYED → 首次 undo/redo 100% 在
# preflight 撞 history_ticket_state_invalid(HTTP 409)。签发/绑定本就不看 protected,
# 生命周期自此对称;protected 专属的 validate_history_replay_ticket 前置校验不变。
# 新增 sdk/test_undo_http_unprotected.py 钉死「unprotected+真历史」测试空窗。
# 无 sdk.__all__ 变化、无接口签名变化 → PATCH。发现现场:ZE-batch-image-foundry
# 标准升级 P5 浏览器验收(foundry 升级档案 05 号台账)。
# 3.61.0 (2026-07-20) — 结构诊断器 Schema Doctor Step 1(加性 MINOR)。field_types 新增三个
# 纯只读函数:describe_schema(schema→人类/AI 可读结构树+样例)、minimal_example(最小合法样例,
# 生成后回验,不保证合法处诚实标 _incomplete)、diagnose(汇总 value 对 schema 的全部不符,永不
# raise)。与 validate 的遇错即 raise 契约互补而不替代(活写路径依赖 validate 不变);_required_fields
# 提取为单一真源(validate/describe/diagnose 共用)。含深度/条数护栏防大数组爆炸,防漂移 property
# gate 守 diagnose⊇validate 一致性。barrel 追加导出 canonicalize_entity_schema(原缺)+describe_schema
# +minimal_example+diagnose。sdk.__all__ 加 4 项 = 公开 API 加性,无破坏。R-C18 实现蓝图见
# foundry 升级档案 09 号。后续 Step 2-5(sdk report 层/端点/CLI/死胡同升级)另行加性叠加。
# 3.62.0 (2026-07-20) — 拓扑收养自愈 adopt-heal(根治「requires topology repair」死胡同)。
# 启动对账 _initialize_locked 对「身份记录残缺(marker/账本行只剩一样)或矛盾、但文档完好且
# 命名空间相符」的静默项目**就地重建身份记录**(_adopt_heal:validate+_write_marker 内含
# document 身份守卫,全过才 live;矛盾病例代际取 max+1 栅栏过期引用),修不了的才维持
# fail-closed 冻结。_require_live 冻结报错带 reason(状态行 operation_id);error_projection
# 按 reason 出人话(改名/缺文档/坏树各说各的+指路 topology doctor)。新增
# scripts/topology_doctor.py(scan 只读诊断/heal 显式修复:备份→对齐 doc id→清残记录→
# 旧语法翻译(对象字段布尔 required 提升父级名单)→交给启动收养重建)。根因:raw copy/改名
# 搬运项目目录打破「目录名==doc id==marker==账本行」四位一致。5 测新增,178 拓扑测全绿。
# 3.63.0 (2026-07-20) — R-C17 关联源统一项目级(加性 MINOR)。一个项目 = 0/1 本地文件夹
# (IndexedDB, 不变) + 0/1 git 仓库(新: 项目级 sidecar .ze-git-link.json, owner/repo/ref 从
# 账号级 credential 降到项目级);只有 PAT 留账号级(github_repo credential 纯化为令牌持有者,
# metadata owner/repo/ref 保留为 legacy 迁移源不再权威)。新增 project_git_link.py +
# Repo read/set/clear + GET/PUT/DELETE /api/projects/{id}/git-link(成员ACL+幂等)。
# ze_file 改「项目链接权威 + 账号 PAT 实时解析 + 未迁移项目整组回退账号历史绑定」——
# 修跨项目串仓库 bug(fallback 保障未迁移用户零破坏, 故 MINOR 而非 MAJOR;错误码加性扩
# github_pat_not_bound)。UI: 设置菜单两块关联迁入「文件→项目关联…」对话框(模式B overlay,
# 不动 DialogKind), 项目卡加 git 徽标(存在性探针+全兜底防热路径500)。GithubLinkSettings
# 保留未引用(legacy, 后续清理波移除)。GATE-LINK-PROJECT-SCOPE 防回归。蓝图+对抗审见
# foundry 升级档案 09 号。
# 3.64.0 (2026-07-21) — 「没绑就先说」不变量上生到编辑器动作(加性 MINOR;对外公测就绪能力①)。
# 命令声明 requires_route='image' 后,cardlist/property 的生图/重生动作按钮据 aiRoutes 在未绑
# 凭据时自动置灰+「去绑定」——把驱动层 428「跑失败」提前成点击前置灰。introspect 下发
# requires_route(subset 加字段) → CommandSpecBrief.requires_route? → SlotContextValue.aiRoutes?
# (App useAiRoutes 注入) → GalleryEditor.renderActionButton + actionGating.renderFormActionButton
# (ActionBarZone/InfoControlUnit 经安全 useContext(SlotContext) 透传)在 requires_route==='image'
# 且 aiRoutes 存在时包 AiRouteGate。全加性:未声明 requires_route 的命令逐层字节级不变;无
# provider 的展示台 useContext 返 null 不门控。驱动层 requires_credential(428) 仍兜底。
# 并含能力③:llm 路线(codex-cli/claude-cli)凭据解析在 ZE_SERVER_MODE 下 fail-closed
# ——_resolve_codex_cli/_resolve_claude_binary 无个人绑定即抛 requires_credential(对齐
# image/copilot),防多租户下陌生人烧公司订阅;本地单机(server_mode 关)仍回落默认账号,零变。
# 对抗审查两处一致性补全(并入本版):(a)_server_mode() 真值集复用 _truthy(含 "on"),并把
# server.py copilot 分支窄集补齐——消除 ZE_SERVER_MODE=on 时传输层进 server 态而凭据层 fail-open
# 的裂脑;(b)ze.image.route='auto' 时 /api/ai-routes 补 'auto' 合成行(可用性=内网服务已配 或
# 已绑任一个人路线),前端 AiRoute.route 联合加 'auto',使 auto 态也被 AiRouteGate 门控,不再旁路。
# 3.65.0 (2026-07-21) — 内容合规同意门 AiConsentGate(加性 MINOR;对外公测就绪能力④)。
# 首次 AI 生成前须接受服务条款 + 内容须知。机制在 core、条款文本是 policy(config):GET /api/ai-consent
# 出部署配置的 {text,url,version}(registry ze.ai.terms_*);useAiConsent 按 version 比对 localStorage
# (ze.ai.consent.v1)判是否已接受(version 空=部署未配 → accepted 恒真,门不激活);AiConsentGate 未接受
# 时 onClickCapture 拦首击弹条款(BaseDialog),同意后 persist 放行。接线镜像①:SlotContext.aiConsent?
# (App useAiConsent 注入)→ GalleryEditor/actionGating 在命令声明 requires_route(image/llm 皆需)时包
# AiConsentGate,条款门在外、路线门在内。全加性:未配条款或未声明 requires_route 的命令逐层字节级不变;
# 后端不存接受态(前端 localStorage 持有,可更新可版本化)。
# 连带修复(④审查暴露的既有问题):BaseDialog 改 createPortal 到 document.body。原内联渲染在 zone 内
# (ConfirmDialog / dialogRegistry / AiConsentGate)时,祖先 .ze-editor 的 container-type+backdrop-filter
# 把 position:fixed 包含块从视口改写为面板并 overflow:clip 裁切,致遮罩只盖面板、弹窗居中在面板且被裁。
# portal 逃离该包含块恢复真视口全屏模态(与 ContextMenu/ImageLightbox 同策略;.ze-dialog 定位走全局类不依赖
# 祖先,Header 对话框无回归)。所有 BaseDialog 消费者受益,非仅 AiConsentGate。
# 4.0.0 (2026-07-29) — credential provider 闭集新增 cursor_api_key；见
# 01-core/_protocol/MIGRATIONS.md。公开 enum 值合同变化按规则升 MAJOR。
# 4.1.0 (2026-07-29) — 前端账号变化通知(加性 MINOR)：账号弹窗成功绑定、切换默认项或
# 删除后统一广播 CREDENTIALS_CHANGED_EVENT，能力入口可立即重查当前用户账号。
INTERFACE_VERSION = "4.1.0"
