"""sdk — ZE-skill-build 框架统一 import 门面 (barrel).

`02-mod/05-code/` 的业务代码 + 顶层 server.py / scripts/ze_command.py 都从这里取
框架类, 不直接 import 深层叶子路径:

    from sdk import DocumentRegistry, CommandRegistry, register_builtin_commands

好处 (跟 webapp 的 @ze vite alias 同理): core 升级 (整体替换 01-core) 时,
mod / 运行时的 import 不变; 业务方不必知道叶子的物理路径.

本模块零业务逻辑 (框架层, 复制即用). `import _paths` 先把叶子目录注入 sys.path,
再从各叶子 re-export 公开符号.
"""

from __future__ import annotations

from . import _paths  # noqa: F401  (side-effect: 注入 sys.path; 必须最先)

# ---- 数据结构 ----
from entity import Entity, Actor, AiProvenance, ZeError       # noqa: E402
from document import (                                          # noqa: E402
    Document, DocumentMutationSession, DocumentMutationSnapshot,
    DocumentRegistry, ReadOnlySourceSpec,
)
from demo_scenario import (  # noqa: E402
    instantiate_scenario, load_scenario_manifest, scenario_config,
)
from document_index import DocumentIndex, DocumentIndexEntry   # noqa: E402
from field_types import (    # noqa: E402
    make_validator, validate, sanitize,
    canonicalize_entity_schema, describe_schema, minimal_example, diagnose,
)
from .schema_doctor import (    # noqa: E402  (本目录模块, 相对 import 同 introspect)
    build_report as diagnose_build_report,
    diagnose_document, diagnose_structure, describe_expected_structure,
    SCHEMA_DOCTOR_REPORT_SCHEMA,
)
from asset import AssetStore, AssetRef                         # noqa: E402
from type_library import resolve_type_schema                   # noqa: E402
from envelope import (  # noqa: E402
    export_manifest, import_manifest, register_manifest_commands,
    make_pack_loader, materialize_read_only_sources,
)

# ---- 类型库组合 API(`from sdk import types as zt`;本体 01-data-structure/07-type-library) ----
from . import types                                            # noqa: E402, F401

# ---- 命令 / 操作 ----
from command import (                                         # noqa: E402
    CommandMutation, CommandRegistry, CommandSpec, DeferredAction,
)
from ai_governor import AiGovernor, GuardDecision             # noqa: E402
from builtin_commands import register_builtin_commands        # noqa: E402
from operation import (                                       # noqa: E402
    Operation, OpLog, Patch, new_operation,
    SOURCES, KINDS, OPS, ENTITY_META_FIELD,
    ConfirmationReservation, PendingConfirmation,
    new_pending_confirmation, PendingStore,
    EXTERNAL_TARGET, external_ai, external_api,
)
from commit_contracts import (                                # noqa: E402
    SavePolicy, CommitStatus, EventStatus, CommitStage,
    DurabilityState, DurabilityFacts, CommitFailure, CommitReceipt,
)
from candidate import (                                       # noqa: E402
    CANDIDATE_ENTITY_TYPE, CANDIDATE_STATUSES, CandidateRecord,
    new_candidate_record, candidate_adopt_operation,
    candidate_reject_operation,
)

# ---- 协议 (变更聚合 + 广播 payload) ----
from change_set import (                                      # noqa: E402
    EntityChange, affected_entities, to_broadcast_payload,
    to_status_payload, to_pipeline_status_payload,
)
from run_status import EntityRunStatus, RUN_PHASES, StageStatus  # noqa: E402

# ---- 实时同步 ----
from broker import RealtimeBroker, Subscription               # noqa: E402

# ---- 管线 (stage / work-list / supervisor / 生命周期 command) ----
from stage import (                                           # noqa: E402
    StageSpec, StageContext, StageResult, StageRunner, CancelledError, InterruptedError,
)
from stage_plan import StagePlan                              # noqa: E402
from pipeline import (                                        # noqa: E402
    PipelineRunner, PipelineStep, PipelineRun, PipelineStepResult,
)
from work_list import WorkItem, WorkList                      # noqa: E402
from supervisor import PipelineSupervisor                     # noqa: E402
from pipeline_commands import (                               # noqa: E402
    register_pipeline_commands, PIPELINE_LIFECYCLE_COMMANDS,
)

# ---- 资源上限 (02-data-flow/06-resource-budget):声明式硬上限,gate.acquire 唯一入口 ----
from resource_budget import (                                 # noqa: E402
    ResourceContract, ResourceGate, CapacityGate, GateRegistry,
)

# ---- 提交与持久化（02-data-flow/07-persistence）：所有写入口唯一边界 ----
from ports import (                                           # noqa: E402
    ActionReceipt, ActionStatus, CommitExecution, CommitIntent, CommitStore,
    EventDeliveryError, EventFactory, EventPublisher,
    MutationBatch, MutationRollbackError, MutationSession,
    NestedCommitError, WriteOutcomeError,
)
from file_store import FileCommitStore                        # noqa: E402
from commit_coordinator import (                              # noqa: E402
    BrokerEventPublisher, CanonicalEventFactory, CommitCoordinator,
)
from effect_context import (                                  # noqa: E402
    EffectPhase, after_commit_effect_phase, assert_external_effect_allowed,
    current_effect_phase, effect_phase, in_producer_phase,
    producer_effect_phase,
)

# ---- 历史 / 回滚 (02-data-flow/04-history-rollback) ----
from checkpoint import CheckpointManager, CheckpointMeta       # noqa: E402
from undo import (                                             # noqa: E402
    DeferredHistoryRecord, UndoController, RollbackConflict, GroupRollbackResult,
    rollback_operation_group, rollback_field, rollback_entity_to_op,
)

# ---- 用户面 (04-user-facing/03-authentication) ----
from auth import (  # noqa: E402
    User, UserStore, SessionManager, PolicyStore, AuditLog,
    build_actor, external_user_id, default_system_auth_dir, default_sessions_path,
    user_has_channel, user_public_card, user_matches_query, search_users, lookup_users,
    PERMISSION_READ, PERMISSION_EDIT, PERMISSION_BAN, PERMISSION_NONE,
    permission_allows,
    ACCESS_REQUEST_PENDING, ACCESS_REQUEST_APPROVED, ACCESS_REQUEST_REJECTED,
    MODEL_SCOPE_LLM, MODEL_SCOPE_IMAGE, MODEL_SCOPE_VIDEO, MODEL_SCOPE_VOICE,
    MODEL_SCOPE_TTS, MODEL_SCOPE_STT, MODEL_SCOPE_EMBEDDING, MODEL_SCOPES,
    USER_STATUS_ACTIVE, USER_STATUS_DISABLED, is_super_admin, super_admin_users,
)
from credentials import (  # noqa: E402
    CredentialRecord, CredentialStore, CredentialContext,
    credential_context, current_credential_context,
    default_credentials_path, default_credential_key_path,
    PROVIDER_HUPO_GATEWAY, PROVIDER_OPENAI_API, PROVIDER_ANTHROPIC_API,
    PROVIDER_CURSOR_API,
    PROVIDER_CODEX_CLI, PROVIDER_CLAUDE_CLI, PROVIDER_GITHUB_REPO,
    PROVIDER_GEMINI_API, PROVIDERS,
    API_KEY_PROVIDERS, CLI_PROVIDERS, SECRET_REQUIRED_PROVIDERS,
    META_HOME_DIR, META_BIN, META_ACCOUNT, META_DEFAULT,
    META_REPO_OWNER, META_REPO_NAME, META_REPO_REF,
    CLI_HOME_ENV, CLI_HOME_AUTH_FILE,
    ai_homes_dir, provision_cli_home, write_cli_auth_file, cli_home_env,
    remove_cli_home,
    STATUS_ACTIVE as CREDENTIAL_STATUS_ACTIVE,
    STATUS_NEEDS_SECRET as CREDENTIAL_STATUS_NEEDS_SECRET,
    STATUS_DISABLED as CREDENTIAL_STATUS_DISABLED,
    STATUS_NEEDS_REAUTH as CREDENTIAL_STATUS_NEEDS_REAUTH,
)
from project_git_link import (  # noqa: E402  (R-C17 项目级 git 关联)
    GitLinkRecord, PROJECT_GIT_LINK_FILE,
    read_link as project_git_link_read,
    write_link as project_git_link_write,
    delete_link as project_git_link_delete,
    link_exists as project_git_link_exists,
)
from github_repo import (  # noqa: E402
    read_path as github_read_path,
    normalize_path as github_normalize_path,
    validate_owner as github_validate_owner,
    validate_repo as github_validate_repo,
    validate_ref as github_validate_ref,
    DEFAULT_MAX_FILE_BYTES as GITHUB_DEFAULT_MAX_FILE_BYTES,
)
from cli_login import LOGIN_MANAGER, CliLoginManager  # noqa: E402
from identity import resolve_actor  # noqa: E402

# ---- 配置 (04-ops/01-config-management; 跨层下游叶子, 由 _paths 注入路径) ----
from registry import ConfigRegistry, ConfigItem, seed_framework_config  # noqa: E402

# ---- AI 最低接口只读 reader (本目录模块, 鸭子类型, 无叶子依赖故相对 import) ----
from .introspect import (  # noqa: E402
    IntrospectionMeta,
    describe_context, list_commands, describe_command,
    list_entities, entity_select, list_editors, list_pipelines,
    list_tasks, task_status, query_oplog,
    settings_read, read_manual,
)

# ---- 浮层 AI 气泡 IntentContext 组装 (纯函数; server 取料过 ACL 后喂进来) ----
from .copilot_context import (  # noqa: E402
    compose_copilot_context, static_tool_layer, dynamic_context_layer,
    structural_entity_summary, oplog_signals, task_signals, fold_data_channel,
)

# ---- 引导精灵逻辑层 (系统 3a; key_flows 投影 → guided_tour + SSE 帧, 纯函数) ----
from .guided_tour import (  # noqa: E402
    compile_guided_tours, guided_tour_summaries, find_tour,
    normalize_adhoc_steps, adhoc_tour_mode, project_semantic_target,
    guide_step_frame, guide_end_frame, TOUR_STEP_KINDS, TOUR_MODES,
)

# ---- 模型调用薄驱动 (本目录模块; 按 config 路线分发 LLM / 图片调用) ----
from .model import (  # noqa: E402
    llm_call, image_call, llm_schema_call, user_llm_schema_call,
    cursor_models_for_current_user, agent_chat, gemini_validate_key,
)

# ---- ProviderHub 受管模块 (AI 接入内核; 契约 + 唯一解析算法, 规划 §3.4/§5.3) ----
# 纯数据 + 纯函数地基: 声明能力需求 → 读用户连接(投影自 System CredentialStore)+ Build
# 注册的 provider/model → 算可用模型投影(9 类统一错误码 + 可解释 trace). 真实派发接线在
# 后续监督批委托它(退休 model.py 私有选择梯), 首片仅契约与离线解析.
from . import hub  # noqa: E402, F401

# ---- 外发通知薄驱动 (本目录模块; 按 channel 分发, 飞书私信 backend #1) ----
from .notify import (  # noqa: E402
    notify, NotifyResult, NotifyLog, default_notify_log_path,
    register_backend, available_channels,
)
from .idempotency import EffectLedger, IdempotencyStore, canonical_request_hash  # noqa: E402
from .assistant_turn import (  # noqa: E402  (AI 助手 Turn 协议内核 §7.1 P0-A)
    TurnStore, AssistantTurn, TurnEvent, TurnError, can_transition, is_terminal,
)
from .persistence import execute_confirmation  # noqa: E402
from .local_update_agent import (  # noqa: E402
    InstallAuthorization, LocalUpdateAgent, LocalUpdateError, TrustedReleaseKey,
)

__all__ = [
    # 数据结构
    "Entity", "Actor", "AiProvenance", "ZeError",
    "Document", "DocumentMutationSession", "DocumentMutationSnapshot",
    "DocumentRegistry", "ReadOnlySourceSpec",
    "instantiate_scenario", "load_scenario_manifest", "scenario_config",
    "DocumentIndex", "DocumentIndexEntry",
    "GitLinkRecord", "PROJECT_GIT_LINK_FILE",
    "project_git_link_read", "project_git_link_write",
    "project_git_link_delete", "project_git_link_exists",
    "make_validator", "validate", "sanitize",
    "canonicalize_entity_schema", "describe_schema", "minimal_example", "diagnose",
    "diagnose_build_report", "diagnose_document", "diagnose_structure",
    "describe_expected_structure", "SCHEMA_DOCTOR_REPORT_SCHEMA",
    "resolve_type_schema",
    "export_manifest", "import_manifest", "register_manifest_commands",
    "make_pack_loader", "materialize_read_only_sources",
    "AssetStore", "AssetRef",
    # 命令 / 操作
    "CommandMutation", "CommandRegistry", "CommandSpec", "DeferredAction",
    "register_builtin_commands",
    # AI 同事治理(05-ai-coworker;组合既有原语)
    "AiGovernor", "GuardDecision",
    "Operation", "OpLog", "Patch", "new_operation",
    "SOURCES", "KINDS", "OPS", "ENTITY_META_FIELD",
    # 确认门控 + 外部效果 provenance
    "ConfirmationReservation", "PendingConfirmation",
    "new_pending_confirmation", "PendingStore",
    "EXTERNAL_TARGET", "external_ai", "external_api",
    "SavePolicy", "CommitStatus", "EventStatus", "CommitStage",
    "DurabilityState", "DurabilityFacts", "CommitFailure", "CommitReceipt",
    "CANDIDATE_ENTITY_TYPE", "CANDIDATE_STATUSES", "CandidateRecord",
    "new_candidate_record", "candidate_adopt_operation",
    "candidate_reject_operation",
    # 协议
    "EntityChange", "affected_entities", "to_broadcast_payload",
    "to_status_payload", "to_pipeline_status_payload",
    "EntityRunStatus", "RUN_PHASES", "StageStatus",
    # 实时同步
    "RealtimeBroker", "Subscription",
    # 管线
    "StageSpec", "StageContext", "StageResult", "StageRunner", "CancelledError", "InterruptedError",
    "StagePlan",
    "PipelineRunner", "PipelineStep", "PipelineRun", "PipelineStepResult",
    "WorkItem", "WorkList", "PipelineSupervisor",
    "register_pipeline_commands", "PIPELINE_LIFECYCLE_COMMANDS",
    # 资源上限
    "ResourceContract", "ResourceGate", "CapacityGate", "GateRegistry",
    # 提交 / 持久化
    "ActionReceipt", "ActionStatus", "CommitExecution", "CommitIntent", "CommitStore",
    "EventDeliveryError", "EventFactory", "EventPublisher",
    "MutationBatch", "MutationRollbackError", "MutationSession",
    "NestedCommitError", "WriteOutcomeError", "FileCommitStore",
    "BrokerEventPublisher", "CanonicalEventFactory", "CommitCoordinator",
    "EffectPhase", "after_commit_effect_phase", "assert_external_effect_allowed",
    "current_effect_phase", "effect_phase", "in_producer_phase",
    "producer_effect_phase",
    "execute_confirmation",
    # 历史 / 回滚
    "CheckpointManager", "CheckpointMeta",
    "DeferredHistoryRecord", "UndoController", "RollbackConflict", "GroupRollbackResult",
    "rollback_operation_group", "rollback_field", "rollback_entity_to_op",
    # 用户面 (账号 / 身份)
    "User", "UserStore", "SessionManager", "PolicyStore", "AuditLog",
    "build_actor", "external_user_id", "default_system_auth_dir", "default_sessions_path",
    "user_has_channel", "user_public_card", "user_matches_query", "search_users", "lookup_users",
    "PERMISSION_READ", "PERMISSION_EDIT", "PERMISSION_BAN", "PERMISSION_NONE",
    "permission_allows",
    "ACCESS_REQUEST_PENDING", "ACCESS_REQUEST_APPROVED", "ACCESS_REQUEST_REJECTED",
    "MODEL_SCOPE_LLM", "MODEL_SCOPE_IMAGE", "MODEL_SCOPE_VIDEO", "MODEL_SCOPE_VOICE",
    "MODEL_SCOPE_TTS", "MODEL_SCOPE_STT", "MODEL_SCOPE_EMBEDDING", "MODEL_SCOPES",
    "USER_STATUS_ACTIVE", "USER_STATUS_DISABLED", "is_super_admin", "super_admin_users",
    "CredentialRecord", "CredentialStore", "CredentialContext",
    "credential_context", "current_credential_context",
    "default_credentials_path", "default_credential_key_path",
    "PROVIDER_HUPO_GATEWAY", "PROVIDER_OPENAI_API", "PROVIDER_ANTHROPIC_API",
    "PROVIDER_CURSOR_API",
    "PROVIDER_CODEX_CLI", "PROVIDER_CLAUDE_CLI", "PROVIDER_GITHUB_REPO",
    "PROVIDER_GEMINI_API", "PROVIDERS",
    "API_KEY_PROVIDERS", "CLI_PROVIDERS", "SECRET_REQUIRED_PROVIDERS",
    "META_HOME_DIR", "META_BIN", "META_ACCOUNT", "META_DEFAULT",
    "META_REPO_OWNER", "META_REPO_NAME", "META_REPO_REF",
    "CLI_HOME_ENV", "CLI_HOME_AUTH_FILE",
    "ai_homes_dir", "provision_cli_home", "write_cli_auth_file", "cli_home_env",
    "remove_cli_home",
    # github_repo 只读客户端(系统 1b 后端)
    "github_read_path", "github_normalize_path",
    "github_validate_owner", "github_validate_repo", "github_validate_ref",
    "GITHUB_DEFAULT_MAX_FILE_BYTES",
    "LOGIN_MANAGER", "CliLoginManager",
    "CREDENTIAL_STATUS_ACTIVE", "CREDENTIAL_STATUS_NEEDS_SECRET", "CREDENTIAL_STATUS_DISABLED",
    "CREDENTIAL_STATUS_NEEDS_REAUTH",
    "resolve_actor",
    # 配置 (04-ops)
    "ConfigRegistry", "ConfigItem", "seed_framework_config",
    # AI 最低接口 (只读 introspection reader; server + CLI 共用)
    "IntrospectionMeta",
    "describe_context", "list_commands", "describe_command",
    "list_entities", "entity_select", "list_editors", "list_pipelines",
    "list_tasks", "task_status", "query_oplog",
    "settings_read", "read_manual",
    # 浮层 AI 气泡 IntentContext 组装 (纯函数)
    "compose_copilot_context", "static_tool_layer", "dynamic_context_layer",
    "structural_entity_summary", "oplog_signals", "task_signals", "fold_data_channel",
    # 引导精灵逻辑层 (系统 3a; key_flows 投影 + SSE 帧)
    "compile_guided_tours", "guided_tour_summaries", "find_tour",
    "normalize_adhoc_steps", "adhoc_tour_mode", "project_semantic_target",
    "guide_step_frame", "guide_end_frame", "TOUR_STEP_KINDS", "TOUR_MODES",
    # 模型调用薄驱动 (深化点 #1; 按 config 路线分发)
    "llm_call", "image_call", "llm_schema_call", "user_llm_schema_call",
    "cursor_models_for_current_user", "agent_chat", "gemini_validate_key",
    # 外发通知 (薄驱动 + NotifyLog)
    "notify", "NotifyResult", "NotifyLog", "default_notify_log_path",
    "register_backend", "available_channels",
    # 请求级幂等账本
    "EffectLedger", "IdempotencyStore", "canonical_request_hash",
    "TurnStore", "AssistantTurn", "TurnEvent", "TurnError", "can_transition", "is_terminal",
    # 本地软件更新代理（仅供受信 Workspace/Launcher 调用；不得暴露为普通 HTTP 写接口）
    "InstallAuthorization", "LocalUpdateAgent", "LocalUpdateError", "TrustedReleaseKey",
]
