"""Command — 所有动作的统一入口 (CommandSpec + CommandRegistry).

完整 SPEC 见同目录 SPEC.md.

已实装 (无跨叶子依赖):
  - CommandSpec:执行/展示/确认/AI 治理/schema/source/ACL/调度声明 + mutation_authority
  - CommandRegistry:公开兼容 spec + 注册时冻结的 execution/security/introspection authority
  - dispatch:source/Actor 冻结 → 只读 preflight → execute → 真实受保护 delta/Operation 审计
  - mutation_operation_audit.py 单向消费 Document graph + Operation；Document 不反向依赖本叶子

W03 起的边界:
  - CommandRegistry 只负责校验、确认门控和执行 handler。
  - OpLog 持久化、Document 持久化和 canonical entity_change 发布由
    CommitCoordinator 统一完成。这些副作用不得回流到 dispatch/confirm。

待补 (跨叶子):
  - conflict_strategy 真合并逻辑      [02-data-flow/02-conflict-resolution]
  - scheduling 调度内核 (queued / low-priority)  [02-data-flow]
  - acl 真校验 (role / 白名单)        [04-user-facing/03-authentication]
  - 跨 skill 调用                      [03-commands-operations/04-cross-skill-call]
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import math
import re
import sys
import threading
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable

# 共享: 从兄弟目录 import entity (Actor / ZeError) + operation (Operation / OpLog)
_LEAF_DIR = Path(__file__).resolve().parent
_ENTITY_DIR = _LEAF_DIR.parent.parent / "01-data-structure" / "02-entity"
_DOCUMENT_DIR = _LEAF_DIR.parent.parent / "01-data-structure" / "01-document"
_OPERATION_DIR = _LEAF_DIR.parent / "02-operation"
_PROTOCOL_DIR = _LEAF_DIR.parent.parent / "_protocol"
for d in (_ENTITY_DIR, _DOCUMENT_DIR, _OPERATION_DIR, _PROTOCOL_DIR):
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))

from entity import Actor, ZeError  # noqa: E402
from mutation_policy_coverage import (  # noqa: E402
    MUTATION_POLICY_COVERAGE_CAPABILITY as _MUTATION_POLICY_COVERAGE_CAPABILITY,
)
from operation import (  # noqa: E402
    SOURCES, Operation, OpLog,
    ConfirmationReservation, PendingConfirmation,
    new_pending_confirmation, PendingStore,
)
from mutation_operation_audit import (  # noqa: E402
    validate_mutation_operation_coverage,
)


_COMMAND_DISPATCH_LOCAL = threading.local()
_CONFIRMATION_DISPATCH_LOCAL = threading.local()
_REGISTRY_PENDING_STORES: weakref.WeakSet[Any] = weakref.WeakSet()


def _reset_command_dispatch_state_after_fork() -> None:
    global _COMMAND_DISPATCH_LOCAL
    global _CONFIRMATION_DISPATCH_LOCAL
    _COMMAND_DISPATCH_LOCAL = threading.local()
    _CONFIRMATION_DISPATCH_LOCAL = threading.local()
    for store in tuple(_REGISTRY_PENDING_STORES):
        store._reset_after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_command_dispatch_state_after_fork)


def _command_dispatch_stack() -> list[
    tuple[
        Any, Any, str, Actor, str, tuple[str, str, str], tuple[Any, ...] | None,
        Any,
    ]
]:
    stack = getattr(_COMMAND_DISPATCH_LOCAL, "stack", None)
    if stack is None:
        stack = []
        _COMMAND_DISPATCH_LOCAL.stack = stack
    return stack


_PIPELINE_ROOT_SNAPSHOT_CAPABILITY = object()
_VERIFIED_PIPELINE_ROOT_CAPABILITY = object()


def _pipeline_root_error(
    code: str,
    message: str,
    *,
    reason: str,
    command_id: str = "",
) -> ZeError:
    context = {"reason": reason}
    if command_id:
        context["command_id"] = command_id
    return ZeError(code, message, context=context)


def _canonical_pipeline_authority_json(value: Any, *, reason: str) -> bytes:
    """Canonicalize a framework-owned passive authority projection."""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", "strict")
    except (
        AttributeError,
        TypeError,
        ValueError,
        OverflowError,
        UnicodeEncodeError,
        RecursionError,
    ) as exc:
        raise _pipeline_root_error(
            "pipeline_start_authority_required",
            "active command authority cannot be projected safely",
            reason=reason,
        ) from exc


class _PipelineDispatchRootAuthority:
    """One root-handler lifetime; never a durable or worker authority.

    The object is inserted by ``CommandRegistry.dispatch`` itself and shared by
    every nested dispatch frame.  Its one exact snapshot may be consumed once
    while that frame is still live.  A similar-looking object made by command
    code is therefore never sufficient to issue a sealed Pipeline Start.
    """

    __slots__ = (
        "registry",
        "document",
        "command_id",
        "registered",
        "actor_bytes",
        "source",
        "policy_authority",
        "registration_digest",
        "snapshot",
        "consumed",
        "lock",
    )

    def __init__(
        self,
        *,
        registry: Any,
        document: Any,
        command_id: str,
        registered: Any,
        actor_payload: dict[str, Any],
        source: str,
        policy_authority: tuple[Any, int, Any] | None,
        registration_digest: bytes,
    ) -> None:
        self.registry = registry
        self.document = document
        self.command_id = command_id
        self.registered = registered
        self.actor_bytes = _canonical_pipeline_authority_json(
            actor_payload,
            reason="root_actor_invalid",
        )
        self.source = source
        self.policy_authority = policy_authority
        self.registration_digest = registration_digest
        self.consumed = False
        self.lock = threading.RLock()
        self.snapshot = _ActiveCommandRootSnapshot(
            self,
            _capability=_PIPELINE_ROOT_SNAPSHOT_CAPABILITY,
        )


class _ActiveCommandRootSnapshot:
    """Opaque, process-local view of the currently executing business root.

    This value only carries a live issuance seam.  It is neither serializable
    nor reusable and it never becomes a worker grant.
    """

    __slots__ = ("_authority",)

    def __init__(self, authority: Any, *, _capability: object) -> None:
        if (
            _capability is not _PIPELINE_ROOT_SNAPSHOT_CAPABILITY
            or type(authority) is not _PipelineDispatchRootAuthority
        ):
            raise TypeError("active command root snapshots are framework-only")
        object.__setattr__(self, "_authority", authority)

    @property
    def actor(self) -> dict[str, Any]:
        authority = object.__getattribute__(self, "_authority")
        return json.loads(authority.actor_bytes.decode("utf-8", "strict"))

    @property
    def source(self) -> str:
        return object.__getattribute__(self, "_authority").source

    @property
    def command_id(self) -> str:
        return object.__getattribute__(self, "_authority").command_id

    @property
    def document_id(self) -> str:
        document = object.__getattribute__(self, "_authority").document
        return getattr(document, "id", "")

    @property
    def protected(self) -> bool:
        return object.__getattribute__(self, "_authority").policy_authority is not None

    def __copy__(self) -> Any:
        raise TypeError("active command root snapshots cannot be copied")

    def __deepcopy__(self, _memo: Any) -> Any:
        raise TypeError("active command root snapshots cannot be copied")

    def __reduce_ex__(self, _protocol: int) -> Any:
        raise TypeError("active command root snapshots cannot be serialized")


class _VerifiedPipelineRoot:
    """One-shot detached result consumed immediately by the sealed factory."""

    __slots__ = (
        "_actor_bytes",
        "source",
        "command_id",
        "document_id",
        "root_command_digest",
        "_document_policy_bytes",
    )

    def __init__(
        self,
        *,
        actor_bytes: bytes,
        source: str,
        command_id: str,
        document_id: str,
        root_command_digest: str,
        document_policy: dict[str, Any],
        _capability: object,
    ) -> None:
        if _capability is not _VERIFIED_PIPELINE_ROOT_CAPABILITY:
            raise TypeError("verified Pipeline roots are framework-only")
        self._actor_bytes = bytes(actor_bytes)
        self.source = source
        self.command_id = command_id
        self.document_id = document_id
        self.root_command_digest = root_command_digest
        self._document_policy_bytes = _canonical_pipeline_authority_json(
            document_policy,
            reason="document_policy_invalid",
        )

    @property
    def actor(self) -> dict[str, Any]:
        return json.loads(self._actor_bytes.decode("utf-8", "strict"))

    @property
    def document_policy(self) -> dict[str, Any]:
        return json.loads(self._document_policy_bytes.decode("utf-8", "strict"))

    def __copy__(self) -> Any:
        raise TypeError("verified Pipeline roots cannot be copied")

    def __deepcopy__(self, _memo: Any) -> Any:
        raise TypeError("verified Pipeline roots cannot be copied")

    def __reduce_ex__(self, _protocol: int) -> Any:
        raise TypeError("verified Pipeline roots cannot be serialized")


def _active_command_root_snapshot(
    *,
    registry: Any,
    document: Any,
) -> _ActiveCommandRootSnapshot:
    """Return the only snapshot for the live outer business handler."""
    stack = _command_dispatch_stack()
    if not stack:
        raise _pipeline_root_error(
            "pipeline_start_authority_required",
            "Pipeline Start requires an active registered business command",
            reason="active_root_dispatch_missing",
        )
    root_frame = stack[0]
    if (
        type(root_frame) is not tuple
        or len(root_frame) != 8
        or root_frame[0] is not registry
        or root_frame[1] is not document
        or type(root_frame[7]) is not _PipelineDispatchRootAuthority
    ):
        raise _pipeline_root_error(
            "pipeline_start_authority_required",
            "Pipeline Start does not belong to the active command scope",
            reason="active_root_scope_mismatch",
        )
    authority = root_frame[7]
    if (
        authority.registry is not registry
        or authority.document is not document
        or authority.snapshot is None
        or type(authority.snapshot) is not _ActiveCommandRootSnapshot
    ):
        raise _pipeline_root_error(
            "pipeline_authority_stale",
            "active Pipeline root authority is no longer valid",
            reason="active_root_authority_changed",
            command_id=str(root_frame[2]),
        )
    return authority.snapshot


def _project_pipeline_document_policy(
    document: Any,
    *,
    command_id: str,
    entity_types: tuple[str, ...],
) -> dict[str, Any]:
    """Rebuild the exact scoped runtime policy stored in a Pipeline Start."""

    if (
        type(entity_types) is not tuple
        or any(type(value) is not str or not value for value in entity_types)
        or tuple(sorted(set(entity_types))) != entity_types
    ):
        raise _pipeline_root_error(
            "pipeline_start_identity_mismatch",
            "Pipeline target entity types must be one sorted exact tuple",
            reason="pipeline_target_types_invalid",
            command_id=command_id,
        )
    schema = document.schema
    mutation_policies = document.mutation_policies
    entity_policies: list[dict[str, Any]] = []
    for entity_type in entity_types:
        if entity_type not in schema or entity_type not in mutation_policies:
            raise _pipeline_root_error(
                "pipeline_start_authority_required",
                "Pipeline target is outside the protected Document policy",
                reason="pipeline_target_policy_missing",
                command_id=command_id,
            )
        schema_digest = hashlib.sha256(
            _canonical_pipeline_authority_json(
                schema[entity_type],
                reason="entity_schema_invalid",
            )
        ).hexdigest()
        entity_policies.append({
            "entity_type": entity_type,
            "entity_schema_digest": schema_digest,
            "policy": mutation_policies[entity_type],
        })
    return {
        "schema": "ze.pipeline-document-policy.v1",
        "entity_policies": entity_policies,
    }


def _consume_pipeline_authority_root(
    snapshot: Any,
    *,
    document: Any,
    command_id: str,
    document_id: str,
    run_id: str,
    pipeline_id: str,
    entity_types: tuple[str, ...],
) -> _VerifiedPipelineRoot:
    """Consume one exact active root and project its sealed policy identity.

    This is intentionally private.  ``pipeline_authority_commit`` calls it at
    Start issuance; no caller-provided actor, source, registration digest or
    policy bytes are accepted as authority.
    """
    if type(snapshot) is not _ActiveCommandRootSnapshot:
        raise _pipeline_root_error(
            "pipeline_start_authority_required",
            "Pipeline Start requires an exact active command root snapshot",
            reason="active_root_snapshot_invalid",
        )
    authority = object.__getattribute__(snapshot, "_authority")
    if type(authority) is not _PipelineDispatchRootAuthority:
        raise _pipeline_root_error(
            "pipeline_start_authority_required",
            "Pipeline Start root snapshot is invalid",
            reason="active_root_snapshot_invalid",
        )
    with authority.lock:
        stack = _command_dispatch_stack()
        if (
            not stack
            or type(stack[0]) is not tuple
            or len(stack[0]) != 8
            or stack[0][7] is not authority
            or stack[-1][7] is not authority
            or authority.snapshot is not snapshot
            or authority.document is not document
        ):
            raise _pipeline_root_error(
                "pipeline_authority_stale",
                "Pipeline Start root is not active in this command scope",
                reason="active_root_scope_changed",
                command_id=authority.command_id,
            )
        if authority.consumed:
            raise _pipeline_root_error(
                "pipeline_authority_stale",
                "Pipeline Start root was already consumed",
                reason="active_root_already_consumed",
                command_id=authority.command_id,
            )
        if (
            type(command_id) is not str
            or command_id != authority.command_id
            or type(document_id) is not str
            or document_id != getattr(document, "id", None)
            or type(run_id) is not str
            or not run_id
            or type(pipeline_id) is not str
            or not pipeline_id
        ):
            raise _pipeline_root_error(
                "pipeline_start_identity_mismatch",
                "Pipeline Start identity differs from its active business root",
                reason="active_root_identity_mismatch",
                command_id=authority.command_id,
            )
        if (
            type(entity_types) is not tuple
            or any(type(value) is not str or not value for value in entity_types)
            or tuple(sorted(set(entity_types))) != entity_types
        ):
            raise _pipeline_root_error(
                "pipeline_start_identity_mismatch",
                "Pipeline target entity types must be one sorted exact tuple",
                reason="pipeline_target_types_invalid",
                command_id=authority.command_id,
            )

        registry = authority.registry
        registered = registry._registered_command(authority.command_id)
        mutation_authority = registry._is_mutation_authority_bound(
            authority.command_id,
            expected=authority.registered,
        )
        if (
            registered is not authority.registered
            or _registered_command_declaration_digest(registered)
            != authority.registration_digest
        ):
            raise _pipeline_root_error(
                "pipeline_authority_stale",
                "Pipeline root registration changed before issuance",
                reason="active_root_registration_changed",
                command_id=authority.command_id,
            )
        start_issuance = bool(entity_types)
        if start_issuance and authority.command_id.startswith("entity."):
            raise _pipeline_root_error(
                "pipeline_start_authority_required",
                "generic entity commands cannot become Pipeline authority roots",
                reason="pipeline_generic_command_forbidden",
                command_id=authority.command_id,
            )
        if start_issuance and not mutation_authority:
            raise _pipeline_root_error(
                "pipeline_start_authority_required",
                "sealed Pipeline Start requires a business mutation authority",
                reason="business_mutation_authority_missing",
                command_id=authority.command_id,
            )
        if start_issuance and authority.policy_authority is None:
            raise _pipeline_root_error(
                "pipeline_start_authority_required",
                "sealed Pipeline Start requires a protected Document policy root",
                reason="protected_policy_root_missing",
                command_id=authority.command_id,
            )
        if authority.policy_authority is not None:
            current_policy = registry._mutation_policy_dispatch_attestation(
                authority.command_id,
            )
            expected_policy = authority.policy_authority
            if (
                current_policy[0] is not expected_policy[0]
                or current_policy[1] != expected_policy[1]
                or current_policy[2] is not expected_policy[2]
            ):
                raise _pipeline_root_error(
                    "pipeline_authority_stale",
                    "Pipeline root policy authority changed before issuance",
                    reason="active_root_policy_changed",
                    command_id=authority.command_id,
                )
            document._require_mutation_policy_dispatch_authority(
                registry_authority=registry,
                registry_epoch=current_policy[0],
                registry_pid=current_policy[1],
                command_id=authority.command_id,
                command_attestation=current_policy[2],
            )

        document_policy = _project_pipeline_document_policy(
            document,
            command_id=authority.command_id,
            entity_types=entity_types,
        )

        # Consume only after every fallible revalidation and projection step.
        authority.consumed = True
        return _VerifiedPipelineRoot(
            actor_bytes=authority.actor_bytes,
            source=authority.source,
            command_id=authority.command_id,
            document_id=document_id,
            root_command_digest=authority.registration_digest.hex(),
            document_policy=document_policy,
            _capability=_VERIFIED_PIPELINE_ROOT_CAPABILITY,
        )


def _revalidate_pipeline_delegated_mutation_root(
    *,
    registry: Any,
    document: Any,
    command_id: str,
    root_command_digest: str,
    entity_types: tuple[str, ...],
) -> dict[str, Any]:
    """Re-prove the current business root for one dormant delegated check.

    The returned value is a passive detached policy projection.  No command
    handler is invoked and no dispatch, worker or execution authority is
    created.  Future grant-aware execution must still run the real Document
    delta and Operation coverage checks in its coordinator session.
    """

    if type(registry) is not CommandRegistry:
        raise _pipeline_root_error(
            "pipeline_authority_stale",
            "delegated mutation registry is not the sealed command registry",
            reason="delegated_registry_type_invalid",
            command_id=command_id,
        )
    if type(command_id) is not str or not command_id:
        raise TypeError("command_id must be a non-empty exact string")
    if command_id.startswith("entity."):
        raise _pipeline_root_error(
            "pipeline_start_authority_required",
            "generic entity commands cannot become delegated authority roots",
            reason="pipeline_delegated_generic_command_forbidden",
            command_id=command_id,
        )
    if not entity_types:
        raise _pipeline_root_error(
            "pipeline_authority_stale",
            "delegated mutation must remain scoped to a protected entity type",
            reason="delegated_entity_scope_empty",
            command_id=command_id,
        )
    if (
        type(root_command_digest) is not str
        or len(root_command_digest) != 64
        or any(character not in "0123456789abcdef" for character in root_command_digest)
    ):
        raise _pipeline_root_error(
            "pipeline_authority_stale",
            "delegated mutation root digest is invalid",
            reason="delegated_root_digest_invalid",
            command_id=command_id,
        )
    registered = registry._registered_command(command_id)
    if not registry._is_mutation_authority_bound(
        command_id,
        expected=registered,
    ):
        raise _pipeline_root_error(
            "pipeline_start_authority_required",
            "delegated mutation requires a business mutation authority",
            reason="delegated_business_authority_missing",
            command_id=command_id,
        )
    current_digest = _registered_command_declaration_digest(registered).hex()
    if not hmac.compare_digest(current_digest, root_command_digest):
        raise _pipeline_root_error(
            "pipeline_authority_stale",
            "delegated mutation root command changed since Pipeline Start",
            reason="delegated_root_digest_mismatch",
            command_id=command_id,
        )
    current_policy = registry._mutation_policy_dispatch_attestation(command_id)
    document._require_mutation_policy_dispatch_authority(
        registry_authority=registry,
        registry_epoch=current_policy[0],
        registry_pid=current_policy[1],
        command_id=command_id,
        command_attestation=current_policy[2],
    )
    return _project_pipeline_document_policy(
        document,
        command_id=command_id,
        entity_types=entity_types,
    )


# ==============================================================
# 提交后动作合同
# ==============================================================

@dataclass(frozen=True)
class DeferredAction:
    """只允许在业务 mutation 已耐久并完成 canonical 发布后执行的动作。

    ``run`` 接收本次 ``commit_id``。它不能改变已经成立的业务提交；失败会由
    CommitCoordinator 单独记录，caller 也不得因此重放命令。典型用途是启动或
    控制 pipeline、写运行态 manifest、唤醒 worker 等不可回滚副作用。
    """

    id: str
    name: str
    run: Callable[[str], Any] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("DeferredAction.id must not be empty")
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("DeferredAction.name must not be empty")
        if not callable(self.run):
            raise TypeError("DeferredAction.run must be callable")


@dataclass(frozen=True)
class CommandMutation:
    """一个命令的全部 Operation 与提交后动作。

    多条 ``operations`` 是一笔 logical commit，而不是多次独立提交。handler 可以
    在 producer 阶段构造 Document mutation，但线程、网络、运行态文件与状态广播只能
    放进 ``after_commit``。这样提交失败时不会出现“命令没记住，后台任务却已启动”。
    """

    operations: tuple[Operation, ...]
    after_commit: tuple[DeferredAction, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.operations, tuple) or not self.operations:
            raise ValueError("CommandMutation.operations must be a non-empty tuple")
        if any(not isinstance(operation, Operation) for operation in self.operations):
            raise TypeError("CommandMutation.operations must contain only Operation values")
        operation_ids = tuple(operation.id for operation in self.operations)
        if len(set(operation_ids)) != len(operation_ids):
            raise ValueError("CommandMutation operation ids must be unique")
        if not isinstance(self.after_commit, tuple) or any(
            not isinstance(action, DeferredAction) for action in self.after_commit
        ):
            raise TypeError("CommandMutation.after_commit must contain DeferredAction values")
        action_ids = tuple(action.id for action in self.after_commit)
        if len(set(action_ids)) != len(action_ids):
            raise ValueError("CommandMutation action ids must be unique")


# ==============================================================
# CommandSpec
# ==============================================================

_DEFAULT_SOURCES_ALLOWED = SOURCES   # 默认 user/ai/sync/system 全允许
_COMMAND_KINDS = ("native", "stage", "compose")
_CONFLICT_STRATEGIES = (
    "reject", "last-write-wins", "three-way-merge", "crdt",
)
_SCHEDULING_MODES = ("immediate", "queued", "low-priority")


def _validate_command_spec_invariants(
    spec: Any,
    *,
    require_execute: bool,
) -> None:
    """Validate the complete mutable declaration at construction/registration."""
    if type(require_execute) is not bool:
        raise TypeError("require_execute must be an exact bool")
    if type(spec) is not CommandSpec:
        raise TypeError("spec must be an exact CommandSpec")
    command_id = spec.id if type(spec.id) is str else ""

    def reject(field_name: str, message: str) -> None:
        raise ZeError(
            "validation_failed",
            message,
            context={"command_id": command_id, "field": field_name},
        )

    if type(spec.id) is not str or not spec.id:
        reject("id", "CommandSpec.id must be a non-empty exact string")
    if type(spec.label) is not str or not spec.label:
        reject("label", "CommandSpec.label must be a non-empty exact string")
    if type(spec.kind) is not str or spec.kind not in _COMMAND_KINDS:
        reject("kind", "CommandSpec.kind is not a known command kind")
    if type(spec.phase) is not int or spec.phase < 1:
        reject("phase", "CommandSpec.phase must be a positive exact integer")
    if type(spec.touches) is not list or any(
        type(value) is not str or not value for value in spec.touches
    ):
        reject("touches", "CommandSpec.touches must be an exact list of non-empty strings")
    if (
        type(spec.conflict_strategy) is not str
        or spec.conflict_strategy not in _CONFLICT_STRATEGIES
    ):
        reject("conflict_strategy", "CommandSpec.conflict_strategy is not known")
    if type(spec.scheduling) is not str or spec.scheduling not in _SCHEDULING_MODES:
        reject("scheduling", "CommandSpec.scheduling is not known")
    if type(spec.sources_allowed) is not tuple or any(
        type(value) is not str or value not in SOURCES
        for value in spec.sources_allowed
    ):
        reject(
            "sources_allowed",
            "CommandSpec.sources_allowed must be an exact tuple of known sources",
        )
    for field_name in (
        "shortcut", "confirm_template", "description", "requires_route",
    ):
        if type(getattr(spec, field_name)) is not str:
            reject(field_name, f"CommandSpec.{field_name} must be an exact string")
    for field_name in (
        "acl", "params_schema", "outputs_schema", "ui_ports",
    ):
        if type(getattr(spec, field_name)) is not dict:
            reject(field_name, f"CommandSpec.{field_name} must be an exact dict")
    for field_name in (
        "mutation_authority", "requires_confirm", "ai_high_risk",
    ):
        if type(getattr(spec, field_name)) is not bool:
            reject(field_name, f"CommandSpec.{field_name} must be an exact bool")
    if type(spec.ai_cost_usd) not in (int, float) or type(spec.ai_cost_usd) is bool:
        reject("ai_cost_usd", "CommandSpec.ai_cost_usd must be a finite number")
    if not math.isfinite(float(spec.ai_cost_usd)) or spec.ai_cost_usd < 0:
        reject(
            "ai_cost_usd",
            "CommandSpec.ai_cost_usd must be a finite non-negative number",
        )
    if spec.poll is not None and not callable(spec.poll):
        reject("poll", "CommandSpec.poll must be callable or None")
    if require_execute and not callable(spec.execute):
        reject("execute", "CommandSpec.execute must be callable at registration")
    if not require_execute and spec.execute is not None and not callable(spec.execute):
        reject("execute", "CommandSpec.execute must be callable or None")


@dataclass
class CommandSpec:
    """命令声明. 由 @registry.command(...) 装饰器或 CommandRegistry.register 创建.

    execute 函数签名: (args, actor, source, doc) -> Operation | CommandMutation
    poll 函数签名:    (args) -> bool (无 poll 时默认 True)
    """
    id: str
    label: str
    kind: str = "native"                                              # native | stage | compose
    phase: int = 1
    touches: list[str] = field(default_factory=list)
    conflict_strategy: str = "reject"
    scheduling: str = "immediate"
    acl: dict[str, Any] = field(default_factory=dict)
    sources_allowed: tuple[str, ...] = _DEFAULT_SOURCES_ALLOWED
    shortcut: str = ""
    # 确认门控(opt-in 两段式):requires_confirm=True → dispatch 不直接执行,先返回
    # PendingConfirmation；随后只允许带一次性 nonce 的网页人工通道 reserve/confirm。
    # AI/MCP 可以发起 pending，但没有批准能力。默认 False → 普通命令行为不变。
    # confirm_template 是给人审 UI 的可读摘要模板(`{arg}` 插值,best-effort)。
    requires_confirm: bool = False
    confirm_template: str = ""
    # AI 最低接口(Command.Describe)用:人/AI 可读的解释 + 机器可读参数 schema。
    # 可选、默认空 —— 加性,不改 handler 契约;mod 按需填,空时 Describe 回退 label。
    description: str = ""
    params_schema: dict[str, Any] = field(default_factory=dict)
    # Pipeline notebook editor 用:命令完成后承诺的输出 schema + 输入/输出展示端口。
    # 只描述 UI/复盘契约,不改变 execute(args,...) 运行签名;旧命令默认空、完全兼容。
    outputs_schema: dict[str, Any] = field(default_factory=dict)
    ui_ports: dict[str, Any] = field(default_factory=dict)
    # AI 治理(05-ai-coworker;source='ai' 时 AiGovernor 读):单次成本估算(USD)+ 是否高风险
    # (高风险强制人审,复用两段式 confirm)。可选、默认空 → 加性,不改 handler 契约。
    ai_cost_usd: float = 0.0
    ai_high_risk: bool = False
    poll: Callable[[dict[str, Any]], bool] | None = None
    execute: Callable[..., Operation | CommandMutation] | None = None
    # Appended after the complete legacy positional surface: old positional
    # callers keep their exact meaning on Python 3.9, while new code should set
    # this by keyword.  Only explicit business commands may become protected
    # mutation-policy roots; generic CRUD helpers keep the default False.
    mutation_authority: bool = False
    # AI 通道声明(3.56「没绑就先说」不变量,AI-CHANNEL-RULES 第 2 步):本命令消耗的
    # 通道能力('image' / 'llm' / ...)。声明后前端入口据 /api/ai-routes 自动做按钮
    # 门控(置灰+原因+去绑定),点击前就呈现通道状态。可选、默认空 → 加性零破坏,
    # 且**必须缀在 legacy positional surface(...poll, execute, mutation_authority)之后**
    # 保住旧位置参数调用的含义。运行期兜底仍在驱动层(requires_credential → 428 带 fix)。
    requires_route: str = ""

    def __post_init__(self) -> None:
        _validate_command_spec_invariants(self, require_execute=False)


def _freeze_command_declaration(value: Any, *, path: str) -> Any:
    """Own one JSON declaration without invoking user-defined copy hooks.

    Command declarations are configuration, not executable object graphs.  A
    registration snapshot therefore accepts exact JSON containers/scalars and
    converts every container to an immutable framework-owned representation.
    This prevents a caller from changing nested ACL/schema values through an
    alias after registration.
    """
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ZeError(
                "validation_failed",
                f"{path} must not contain a non-finite number",
            )
        return value
    if type(value) is list or type(value) is tuple:
        return tuple(
            _freeze_command_declaration(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        )
    if type(value) is dict:
        owned: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ZeError(
                    "validation_failed",
                    f"{path} keys must be exact strings",
                )
            owned[key] = _freeze_command_declaration(
                item, path=f"{path}.{key}",
            )
        return MappingProxyType(owned)
    raise ZeError(
        "validation_failed",
        f"{path} must contain only exact JSON values",
        context={"value_type": type(value).__name__},
    )


_MAPPING_PROXY_TYPE = type(MappingProxyType({}))


def _detach_registered_declaration(value: Any) -> Any:
    """Return fresh JSON containers without exposing the sealed authority."""
    if type(value) is _MAPPING_PROXY_TYPE:
        return {
            key: _detach_registered_declaration(item)
            for key, item in value.items()
        }
    if type(value) is tuple:
        return [_detach_registered_declaration(item) for item in value]
    if value is None or type(value) in (bool, int, float, str):
        return value
    raise TypeError("registered command declaration contains an invalid value")


@dataclass(frozen=True)
class _CommandGovernanceView:
    """Per-dispatch immutable view passed to an optional AI governor."""

    id: str
    label: str
    kind: str
    phase: int
    touches: tuple[str, ...]
    mutation_authority: bool
    conflict_strategy: str
    scheduling: str
    acl: Any = field(repr=False, compare=False)
    sources_allowed: tuple[str, ...]
    shortcut: str
    requires_confirm: bool
    confirm_template: str
    description: str
    params_schema: Any = field(repr=False, compare=False)
    outputs_schema: Any = field(repr=False, compare=False)
    ui_ports: Any = field(repr=False, compare=False)
    ai_cost_usd: float
    ai_high_risk: bool


@dataclass(frozen=True)
class _RegisteredCommand:
    """Framework-owned execution/security authority captured at registration.

    ``CommandSpec`` remains mutable for long-standing introspection and test
    compatibility.  Dispatch, confirmation, polling, AI governance and
    mutation-policy authority never consume that public object after
    registration; they consume this detached record instead.
    """

    public_spec: CommandSpec = field(repr=False, compare=False)
    id: str
    label: str
    kind: str
    phase: int
    touches: tuple[str, ...]
    mutation_authority: bool
    conflict_strategy: str
    scheduling: str
    acl: Any = field(repr=False, compare=False)
    sources_allowed: tuple[str, ...]
    shortcut: str
    requires_confirm: bool
    confirm_template: str
    description: str
    params_schema: Any = field(repr=False, compare=False)
    outputs_schema: Any = field(repr=False, compare=False)
    ui_ports: Any = field(repr=False, compare=False)
    ai_cost_usd: float
    ai_high_risk: bool
    requires_route: str
    poll: Callable[[dict[str, Any]], bool] | None = field(
        repr=False, compare=False,
    )
    execute: Callable[..., Operation | CommandMutation] = field(
        repr=False, compare=False,
    )

    @classmethod
    def capture(cls, spec: CommandSpec) -> "_RegisteredCommand":
        _validate_command_spec_invariants(spec, require_execute=True)

        def exact_string(name: str) -> str:
            return getattr(spec, name)

        ai_cost_usd = float(spec.ai_cost_usd)

        return cls(
            public_spec=spec,
            id=exact_string("id"),
            label=exact_string("label"),
            kind=exact_string("kind"),
            phase=spec.phase,
            touches=tuple(spec.touches),
            mutation_authority=spec.mutation_authority,
            conflict_strategy=exact_string("conflict_strategy"),
            scheduling=exact_string("scheduling"),
            acl=_freeze_command_declaration(spec.acl, path="CommandSpec.acl"),
            sources_allowed=tuple(spec.sources_allowed),
            shortcut=exact_string("shortcut"),
            requires_confirm=spec.requires_confirm,
            confirm_template=exact_string("confirm_template"),
            description=exact_string("description"),
            params_schema=_freeze_command_declaration(
                spec.params_schema, path="CommandSpec.params_schema",
            ),
            outputs_schema=_freeze_command_declaration(
                spec.outputs_schema, path="CommandSpec.outputs_schema",
            ),
            ui_ports=_freeze_command_declaration(
                spec.ui_ports, path="CommandSpec.ui_ports",
            ),
            ai_cost_usd=ai_cost_usd,
            ai_high_risk=spec.ai_high_risk,
            requires_route=exact_string("requires_route"),
            poll=spec.poll,
            execute=spec.execute,
        )

    def governance_view(self) -> _CommandGovernanceView:
        # A fresh value is used per callback.  Even an adversarial governor
        # using object.__setattr__ cannot change this registry's durable view.
        return _CommandGovernanceView(
            id=self.id,
            label=self.label,
            kind=self.kind,
            phase=self.phase,
            touches=self.touches,
            mutation_authority=self.mutation_authority,
            conflict_strategy=self.conflict_strategy,
            scheduling=self.scheduling,
            acl=self.acl,
            sources_allowed=self.sources_allowed,
            shortcut=self.shortcut,
            requires_confirm=self.requires_confirm,
            confirm_template=self.confirm_template,
            description=self.description,
            params_schema=self.params_schema,
            outputs_schema=self.outputs_schema,
            ui_ports=self.ui_ports,
            ai_cost_usd=self.ai_cost_usd,
            ai_high_risk=self.ai_high_risk,
        )

    def declaration(self) -> dict[str, Any]:
        """Fresh passive projection used by production introspection/consumers."""
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "phase": self.phase,
            "touches": _detach_registered_declaration(self.touches),
            "mutation_authority": self.mutation_authority,
            "conflict_strategy": self.conflict_strategy,
            "scheduling": self.scheduling,
            "acl": _detach_registered_declaration(self.acl),
            "sources_allowed": _detach_registered_declaration(
                self.sources_allowed,
            ),
            "shortcut": self.shortcut,
            "requires_confirm": self.requires_confirm,
            "requires_route": self.requires_route,
            "confirm_template": self.confirm_template,
            "description": self.description,
            "params_schema": _detach_registered_declaration(
                self.params_schema,
            ),
            "outputs_schema": _detach_registered_declaration(
                self.outputs_schema,
            ),
            "ui_ports": _detach_registered_declaration(self.ui_ports),
            "ai_cost_usd": self.ai_cost_usd,
            "ai_high_risk": self.ai_high_risk,
            "has_poll": self.poll is not None,
        }


def _registered_command_declaration_digest(
    registered: _RegisteredCommand,
) -> bytes:
    """Hash every passive registration-time field without callable reprs."""
    try:
        payload = json.dumps(
            registered.declaration(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", "strict")
    except (AttributeError, TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ZeError(
            "mutation_command_registry_corrupted",
            "registered command declaration authority is invalid",
            context={
                "command_id": getattr(registered, "id", ""),
                "reason": "registered_command_declaration_invalid",
                "error_type": type(exc).__name__,
            },
        ) from exc
    return hashlib.sha256(payload).digest()


# ==============================================================
# CommandRegistry
# ==============================================================


class _RegistryPendingStore(PendingStore):
    """Own confirmation state and its exact Document authority under one lock."""

    def __init__(self) -> None:
        super().__init__()
        self._document_authority: dict[str, Any] = {}
        self._owner_pid = os.getpid()
        _REGISTRY_PENDING_STORES.add(self)

    def _reset_after_fork(self) -> None:
        # A lock inherited while another parent thread owned it is unusable in
        # the child.  Replace it without acquiring the inherited object, then
        # discard pending/reserved/frozen authority alike.
        self._lock = threading.RLock()
        self._entries = {}
        self._document_authority = {}
        self._owner_pid = os.getpid()

    def _guard_process(self) -> None:
        if self._owner_pid != os.getpid():
            self._reset_after_fork()

    def _prune_expired_pending(self, now: float) -> None:
        before = set(self._entries)
        super()._prune_expired_pending(now)
        for token in before - set(self._entries):
            self._document_authority.pop(token, None)

    def put_bound(self, pending: PendingConfirmation, document: Any) -> None:
        self._guard_process()
        with self._lock:
            super().put(pending)
            self._document_authority[pending.token] = document

    def reserve(
        self,
        token: str,
        *,
        actor: Any,
        approval_channel: str,
        approval_nonce: str,
    ) -> ConfirmationReservation:
        self._guard_process()
        return super().reserve(
            token,
            actor=actor,
            approval_channel=approval_channel,
            approval_nonce=approval_nonce,
        )

    def document_for(
        self, reservation: ConfirmationReservation,
    ) -> Any:
        self._guard_process()
        with self._lock:
            self._require_reservation(reservation)
            return self._document_authority.get(reservation.token)

    def consume(self, reservation: ConfirmationReservation) -> None:
        self._guard_process()
        with self._lock:
            super().consume(reservation)
            self._document_authority.pop(reservation.token, None)

    def cancel(self, token: str, *, actor: Any) -> bool:
        self._guard_process()
        with self._lock:
            removed = super().cancel(token, actor=actor)
            if removed:
                self._document_authority.pop(token, None)
            return removed


class CommandRegistry:
    """命令注册 + dispatch.

    用法 (mod 端):
        registry = CommandRegistry()

        @registry.command(id="entry.create", label="...", kind="native", touches=["entry"])
        def create_entry(args, actor, source, doc):
            ...
            return new_operation(...)

        execution = commit_coordinator.execute(lambda: registry.dispatch(
            "entry.create", args, actor=..., source="user", doc=doc,
        ))
    """

    def __init__(
        self, *,
        broker: Any | None = None,
        enforce_login: bool = False,
        ai_governor: Any | None = None,
    ) -> None:
        """broker: 仅为过渡期调用方保留的兼容参数。

        W03 起 CommandRegistry 不持久化 Operation、不发布 canonical
        entity_change。即使传入 broker 也不会调用它；事务提交和发布由
        CommitCoordinator 拥有。保留参数是为了让旧 bootstrap 可以分步迁移，
        不是另一条提交通道。

        enforce_login: 启用后, source != 'system' 的 dispatch 要求 actor.user_id
        非空, 否则抛 auth_required. 见 04-user-facing/03-authentication SPEC §6.
        默认 False 兼容现有测试.

        ai_governor: 05-ai-coworker 的 AiGovernor;不传则 source='ai' 不做额外治理
        (向后兼容)。挂上后 source='ai' 的 dispatch 经 guard():provenance/safety/cost,
        且高风险/高成本命令复用既有两段式 confirm 门控。
        """
        self._specs: dict[str, CommandSpec] = {}
        self._registered_commands: dict[str, _RegisteredCommand] = {}
        # Private compatibility alias for the first mutation-policy rollout.
        # Values are now complete sealed registrations, not four-field tuples.
        self._mutation_authority_bindings = self._registered_commands
        # A registry becomes an immutable process-local authority generation
        # when a Document seals against it.  The opaque epoch and issuer PID
        # prevent an inherited post-fork object from remaining authoritative;
        # per-command attestations bind the exact registration record, both
        # callables, the authority bit, and the complete passive declaration.
        self._mutation_policy_owner_pid = os.getpid()
        self._mutation_policy_epoch = object()
        self._mutation_policy_registry_sealed = False
        self._mutation_policy_attestations: dict[str, tuple[Any, ...]] = {}
        # 故意不保存/不消费 broker。参数只是迁移期的调用签名兼容层；
        # 保存为成员容易让后续代码误以为 CommandRegistry 仍拥有 publish 权。
        del broker
        self._enforce_login = enforce_login
        self._ai_governor = ai_governor
        # 待确认请求暂存(confirm 门控用);first-class 成员 → 无 bootstrap 也可用(便于单测)。
        self.pending_store = _RegistryPendingStore()

    # ---- 注册 ----

    def register(self, spec: CommandSpec) -> None:
        if self._mutation_policy_registry_sealed:
            raise ZeError(
                "mutation_command_registry_sealed",
                "commands cannot be registered after runtime authority was sealed",
                context={"reason": "late_command_registration_forbidden"},
            )
        self._guard_mutation_policy_process()
        registered = _RegisteredCommand.capture(spec)
        if registered.id in self._specs:
            raise ZeError(
                "validation_failed",
                f"command {registered.id!r} already registered",
                context={"command_id": registered.id},
            )
        self._specs[registered.id] = spec
        self._registered_commands[registered.id] = registered

    def command(
        self, **spec_kwargs: Any,
    ) -> Callable[
        [Callable[..., Operation | CommandMutation]],
        Callable[..., Operation | CommandMutation],
    ]:
        """装饰器: @reg.command(id=..., label=..., kind=...) -> 把 fn 注册成 execute."""
        def deco(
            fn: Callable[..., Operation | CommandMutation],
        ) -> Callable[..., Operation | CommandMutation]:
            spec = CommandSpec(execute=fn, **spec_kwargs)
            self.register(spec)
            return fn
        return deco

    # ---- 查询 ----

    def get(self, command_id: str) -> CommandSpec:
        if command_id not in self._specs:
            raise ZeError(
                "unknown_command",
                f"command {command_id!r} not registered",
                context={"command_id": command_id},
            )
        return self._specs[command_id]

    def has(self, command_id: str) -> bool:
        return command_id in self._specs

    def is_mutation_authority(self, command_id: str) -> bool:
        """Return the immutable registration-time business-authority bit."""
        return self._is_mutation_authority_bound(command_id, expected=None)

    def _is_mutation_authority_bound(
        self,
        command_id: str,
        *,
        expected: _RegisteredCommand | None,
    ) -> bool:
        spec = self.get(command_id)
        binding = self._registered_commands.get(command_id)
        if (
            type(binding) is not _RegisteredCommand
            or binding.public_spec is not spec
            or binding.execute is not spec.execute
            or type(binding.mutation_authority) is not bool
            or spec.mutation_authority is not binding.mutation_authority
            or binding.id != command_id
            or spec.id != command_id
            or (expected is not None and binding is not expected)
        ):
            raise ZeError(
                "mutation_command_registry_corrupted",
                "registered command mutation authority was replaced or mutated",
                context={"command_id": command_id},
            )
        return binding.mutation_authority

    def _registered_command(self, command_id: str) -> _RegisteredCommand:
        """Return the sealed registration and reject private-map replacement."""
        public_spec = self.get(command_id)
        registered = self._registered_commands.get(command_id)
        if (
            type(registered) is not _RegisteredCommand
            or registered.id != command_id
            or registered.public_spec is not public_spec
        ):
            raise ZeError(
                "mutation_command_registry_corrupted",
                "registered command execution authority was replaced",
                context={"command_id": command_id},
            )
        return registered

    def _guard_mutation_policy_process(self) -> None:
        if (
            type(self._mutation_policy_owner_pid) is not int
            or self._mutation_policy_owner_pid != os.getpid()
            or self._mutation_policy_epoch is None
        ):
            raise ZeError(
                "mutation_command_registry_process_mismatch",
                "command registry authority belongs to another process",
                context={"reason": "registry_process_epoch_mismatch"},
            )

    def _capture_mutation_policy_attestation(
        self, registered: _RegisteredCommand,
    ) -> tuple[Any, ...]:
        return (
            registered,
            registered.execute,
            registered.poll,
            registered.mutation_authority,
            _registered_command_declaration_digest(registered),
        )

    def _verify_mutation_policy_attestation(
        self, command_id: str,
    ) -> tuple[Any, ...]:
        self._guard_mutation_policy_process()
        if not self._mutation_policy_registry_sealed:
            raise ZeError(
                "mutation_command_registry_unsealed",
                "protected dispatch requires a sealed command registry",
                context={
                    "command_id": command_id,
                    "reason": "registry_authority_not_sealed",
                },
            )
        attestations = self._mutation_policy_attestations
        if (
            type(attestations) is not dict
            or len(attestations) != len(self._specs)
            or len(self._registered_commands) != len(self._specs)
        ):
            raise ZeError(
                "mutation_command_registry_corrupted",
                "sealed command registry inventory changed",
                context={
                    "command_id": command_id,
                    "reason": "sealed_registry_inventory_changed",
                },
            )
        registered = self._registered_command(command_id)
        authority = self._is_mutation_authority_bound(
            command_id, expected=registered,
        )
        attestation = attestations.get(command_id)
        if (
            type(attestation) is not tuple
            or len(attestation) != 5
            or attestation[0] is not registered
            or attestation[1] is not registered.execute
            or attestation[2] is not registered.poll
            or attestation[3] is not authority
            or type(attestation[4]) is not bytes
            or attestation[4]
            != _registered_command_declaration_digest(registered)
        ):
            raise ZeError(
                "mutation_command_registry_corrupted",
                "sealed command registration authority changed",
                context={
                    "command_id": command_id,
                    "reason": "sealed_command_attestation_changed",
                },
            )
        return attestation

    def _seal_mutation_policy_registry(
        self, command_ids: tuple[str, ...],
    ) -> tuple[Any, int, dict[str, Any]]:
        """Freeze the complete registry and return opaque policy bindings."""
        self._guard_mutation_policy_process()
        if (
            type(command_ids) is not tuple
            or any(type(value) is not str or not value for value in command_ids)
            or tuple(sorted(set(command_ids))) != command_ids
        ):
            raise TypeError("command_ids must be one sorted unique string tuple")
        if not self._mutation_policy_registry_sealed:
            captured: dict[str, tuple[Any, ...]] = {}
            for registered_id in self._specs:
                registered = self._registered_command(registered_id)
                self._is_mutation_authority_bound(
                    registered_id, expected=registered,
                )
                captured[registered_id] = self._capture_mutation_policy_attestation(
                    registered,
                )
            self._mutation_policy_attestations = captured
            self._mutation_policy_registry_sealed = True
        selected: dict[str, Any] = {}
        for command_id in command_ids:
            attestation = self._verify_mutation_policy_attestation(command_id)
            if attestation[3] is not True:
                raise ZeError(
                    "mutation_policy_command_not_authority",
                    "mutation policy command is not a business authority",
                    context={"command_id": command_id},
                )
            selected[command_id] = attestation
        return (
            self._mutation_policy_epoch,
            self._mutation_policy_owner_pid,
            selected,
        )

    def _mutation_policy_dispatch_attestation(
        self, command_id: str,
    ) -> tuple[Any, int, Any]:
        """Return one live seal token after revalidating its exact binding."""
        attestation = self._verify_mutation_policy_attestation(command_id)
        return (
            self._mutation_policy_epoch,
            self._mutation_policy_owner_pid,
            attestation,
        )

    def list(self) -> list[CommandSpec]:
        return list(self._specs.values())

    def registered_declaration(self, command_id: str) -> dict[str, Any]:
        """Return the detached registration-time declaration for one command."""
        return self._registered_command(command_id).declaration()

    def registered_declarations(self) -> list[dict[str, Any]]:
        """Return detached registration-time declarations in registry order."""
        return [
            self._registered_command(command_id).declaration()
            for command_id in self._specs
        ]

    def can(self, command_id: str, args: dict[str, Any]) -> bool:
        """spec.poll(args) 检查; 无 poll 默认 True; poll 抛错视为 False."""
        registered = self._registered_command(command_id)
        if registered.poll is None:
            return True
        try:
            return bool(registered.poll(args))
        except Exception:
            return False

    # ---- dispatch ----

    def dispatch(
        self,
        command_id: str,
        args: dict[str, Any],
        *,
        actor: Actor,
        source: str,
        doc: Any,                       # Document (不强制类型避免 import 循环)
        op_log: OpLog | None = None,
        confirmed: bool = False,
        turn_id: str = "",
    ) -> Operation | CommandMutation | PendingConfirmation:
        """完整 dispatch 流程.

        1. 取 spec (不存在 → unknown_command)
        2. 校 source 是否在 spec.sources_allowed (失败 → acl_denied)
        3. 调 spec.poll(args) (False → poll_failed)
        3.5 确认门控: spec.requires_confirm 且未 confirmed → 暂存并返回 PendingConfirmation
            (零变更:不 execute)。
        4. 调 spec.execute(args, actor, source, doc) 拿 Operation / CommandMutation
        5. 返回 mutation，由 CommitCoordinator 持久化、发布并执行 after-commit action。

        op_log 是已废弃的签名兼容参数，传入也不会 append。这个约束避免
        caller 和 CommandRegistry 各记一次，也避免“日志已落盘但文档未落盘”的
        半提交。
        """
        registered = self._registered_command(command_id)
        root_authority_binding = registered
        execute_handler = registered.execute
        poll_handler = registered.poll
        if type(actor) is not Actor:
            raise TypeError("actor must be an exact Actor")
        initial_actor_payload = Actor.to_dict(actor)
        callback_actor = Actor(**initial_actor_payload)
        actor_authority = (
            initial_actor_payload["user_id"],
            initial_actor_payload["device_id"],
            initial_actor_payload["source"],
        )

        def require_actor_authority() -> None:
            try:
                caller_current = Actor.to_dict(actor)
            except Exception:
                caller_current = None
            try:
                callback_current = Actor.to_dict(callback_actor)
            except Exception:
                callback_current = None
            if (
                caller_current != initial_actor_payload
                or callback_current != initial_actor_payload
            ):
                for candidate in (actor, callback_actor):
                    state = object.__getattribute__(candidate, "__dict__")
                    if type(state) is dict:
                        state.clear()
                        state.update(initial_actor_payload)
                raise ZeError(
                    "mutation_actor_authority_mutated",
                    "command callback mutated the frozen dispatch Actor",
                    context={"command_id": command_id},
                )

        # Resolve nested scope before *any* command-supplied callable or
        # stateful gate.  In particular poll/governor/pending hooks must never
        # get a chance to mutate a second Document and only then discover that
        # the outer rollback session cannot restore it.
        dispatch_stack = _command_dispatch_stack()
        root_dispatch = not dispatch_stack
        if dispatch_stack:
            (
                parent_registry,
                parent_doc,
                effective_command_id,
                parent_actor,
                parent_source,
                parent_actor_authority,
                parent_root_authority_binding,
                parent_pipeline_root_authority,
            ) = dispatch_stack[-1]
            if parent_registry is not self or parent_doc is not doc:
                raise ZeError(
                    "mutation_command_scope_violation",
                    "nested dispatch must use the same registry and Document",
                    context={
                        "command_id": command_id,
                        "reason": "nested_command_scope_mismatch",
                    },
                )
        else:
            effective_command_id = command_id
            parent_actor = actor
            parent_source = source
            parent_actor_authority = actor_authority
            parent_root_authority_binding = root_authority_binding
            parent_pipeline_root_authority = None

        # A protected Document is bound to the exact completed registry
        # generation and exact registration attestation.  Verify this before
        # consuming a confirmation grant or invoking poll/governor/render,
        # creating a mutation session, or entering the handler.
        root_policy_authority: tuple[Any, int, Any] | None = None
        policy_dispatch_required = getattr(
            doc, "_mutation_policy_dispatch_required", None,
        )
        if (
            not dispatch_stack
            and callable(policy_dispatch_required)
            and policy_dispatch_required()
        ):
            root_policy_authority = self._mutation_policy_dispatch_attestation(
                effective_command_id,
            )
            require_policy_authority = getattr(
                doc, "_require_mutation_policy_dispatch_authority", None,
            )
            if not callable(require_policy_authority):
                raise ZeError(
                    "mutation_policy_registry_authority_mismatch",
                    "protected Document has no registry authority verifier",
                    context={
                        "command_id": effective_command_id,
                        "reason": "document_registry_verifier_missing",
                    },
                )
            require_policy_authority(
                registry_authority=self,
                registry_epoch=root_policy_authority[0],
                registry_pid=root_policy_authority[1],
                command_id=effective_command_id,
                command_attestation=root_policy_authority[2],
            )

        def require_live_root_policy_authority() -> None:
            if root_policy_authority is None:
                return
            current = self._mutation_policy_dispatch_attestation(
                effective_command_id,
            )
            if (
                current[0] is not root_policy_authority[0]
                or current[1] != root_policy_authority[1]
                or current[2] is not root_policy_authority[2]
            ):
                raise ZeError(
                    "mutation_policy_registry_authority_mismatch",
                    "root command registry authority changed during dispatch",
                    context={
                        "command_id": effective_command_id,
                        "reason": "dispatch_registry_attestation_changed",
                    },
                )
            doc._require_mutation_policy_dispatch_authority(
                registry_authority=self,
                registry_epoch=current[0],
                registry_pid=current[1],
                command_id=effective_command_id,
                command_attestation=current[2],
            )

        if type(confirmed) is not bool:
            raise TypeError("confirmed must be an exact bool")
        if confirmed:
            grant = getattr(_CONFIRMATION_DISPATCH_LOCAL, "grant", None)
            if (
                type(grant) is not tuple
                or len(grant) != 5
                or grant[0] is not self
                or grant[1] is not doc
                or grant[2] != command_id
                or grant[3] is not actor
                or grant[4] != source
                or dispatch_stack
            ):
                raise ZeError(
                    "confirmation_authority_required",
                    "confirmed dispatch requires an exact reserved confirmation grant",
                    context={"command_id": command_id},
                )
            # Consume before any later gate/callback.  Nested dispatch cannot
            # reuse it, and a failed stale recheck must acquire a fresh grant
            # through confirm() before retrying.
            delattr(_CONFIRMATION_DISPATCH_LOCAL, "grant")

        if source not in registered.sources_allowed:
            raise ZeError(
                "acl_denied",
                f"command {command_id!r} forbidden for source {source!r}",
                context={
                    "command_id": command_id,
                    "source": source,
                    "sources_allowed": list(registered.sources_allowed),
                },
            )

        # 身份兜底: enforce_login 启用时, 任何 source 都要 actor.user_id 非空.
        # 不留 source=system 旁路 — source 由 caller 控制, 留旁路等于信任 caller;
        # framework 自动化任务请用一个保留账号 (例如注册 user_id="__system__")
        # 主动登录后再 dispatch, 安全模型更清晰.
        if self._enforce_login:
            if not initial_actor_payload["user_id"]:
                raise ZeError(
                    "auth_required",
                    f"command {command_id!r} requires logged-in user "
                    f"(empty actor.user_id with source={source!r})",
                    context={"command_id": command_id, "source": source},
                )

        if dispatch_stack and registered.requires_confirm:
            raise ZeError(
                "nested_confirmation_unsupported",
                "confirmation must belong to the outermost business command",
                context={"command_id": command_id},
            )

        # The rollback boundary starts before the first command-supplied
        # callable.  poll/governor/confirmation preparation are preflight only;
        # they may not leak a Document mutation even when they return false,
        # raise, or request a confirmation token.
        begin_session = getattr(doc, "begin_mutation_session", None)
        mutation_session = begin_session() if callable(begin_session) else None
        try:
            if root_dispatch and mutation_session is not None:
                begin_coverage = getattr(
                    mutation_session, "_begin_command_policy_segment", None,
                )
                if callable(begin_coverage):
                    begin_coverage(
                        registry_authority=(
                            self if root_policy_authority is not None else None
                        ),
                        registry_epoch=(
                            root_policy_authority[0]
                            if root_policy_authority is not None else None
                        ),
                        registry_pid=(
                            root_policy_authority[1]
                            if root_policy_authority is not None else None
                        ),
                        command_id=(
                            effective_command_id
                            if root_policy_authority is not None else ""
                        ),
                        command_attestation=(
                            root_policy_authority[2]
                            if root_policy_authority is not None else None
                        ),
                        _capability=_MUTATION_POLICY_COVERAGE_CAPABILITY,
                    )

            preflight_callback_ran = False
            if poll_handler is not None:
                preflight_callback_ran = True
                try:
                    poll_allowed = bool(poll_handler(args))
                except Exception:
                    poll_allowed = False
            else:
                poll_allowed = True
            if not poll_allowed:
                require_actor_authority()
                raise ZeError(
                    "poll_failed",
                    f"command {command_id!r} poll() returned false",
                    context={"command_id": command_id},
                )
            require_actor_authority()

            # AI 同事治理(05-ai-coworker;opt-in):source='ai' 且挂了 governor → guard()。
            require_confirm = registered.requires_confirm
            if source == "ai" and self._ai_governor is not None and not confirmed:
                preflight_callback_ran = True
                try:
                    decision = self._ai_governor.guard(
                        registered.governance_view(), args, callback_actor,
                    )
                finally:
                    require_actor_authority()
                require_confirm = require_confirm or getattr(
                    decision, "require_confirm", False,
                )

            if require_confirm and not confirmed:
                if dispatch_stack:
                    raise ZeError(
                        "nested_confirmation_unsupported",
                        "confirmation must belong to the outermost business command",
                        context={"command_id": command_id},
                    )
                # Confirmation projection stringifies caller args and freezes a
                # detached pending payload.  Treat that extension-facing work
                # as preflight even when no poll/governor was installed.
                preflight_callback_ran = True
                pending = new_pending_confirmation(
                    command_id=command_id,
                    args=args,
                    source=source,
                    actor=initial_actor_payload,
                    summary=_render_confirm(registered.confirm_template, args),
                    # §7.4 P0-E:归属哪一轮助手对话(caller 不传=空,即非助手轮发起,
                    # 取消某轮不该把它顺手清掉)。没有这一行,PendingStore.tokens_for_turn
                    # 永远返空 —— 「取消整轮释放挂起确认」会变成一个不报错的空操作。
                    turn_id=turn_id,
                )
                require_live_root_policy_authority()
                if mutation_session is not None:
                    assert_unchanged = getattr(
                        mutation_session, "assert_unchanged", None,
                    )
                    if preflight_callback_ran and callable(assert_unchanged):
                        assert_unchanged(stage="confirmation")
                    mutation_session.rollback()
                    mutation_session = None
                self.pending_store.put_bound(pending, doc)
                return pending

            if mutation_session is not None and preflight_callback_ran:
                assert_unchanged = getattr(
                    mutation_session, "assert_unchanged", None,
                )
                if callable(assert_unchanged):
                    assert_unchanged(stage="poll_governor")
            require_live_root_policy_authority()

            # Only the handler is a nested-command authority scope.  Preflight
            # callbacks cannot dispatch a hidden root and smuggle its mutation
            # into this command.
            if root_dispatch:
                pipeline_root_authority = _PipelineDispatchRootAuthority(
                    registry=self,
                    document=doc,
                    command_id=effective_command_id,
                    registered=parent_root_authority_binding,
                    actor_payload=initial_actor_payload,
                    source=source,
                    policy_authority=root_policy_authority,
                    registration_digest=_registered_command_declaration_digest(
                        parent_root_authority_binding,
                    ),
                )
            else:
                pipeline_root_authority = parent_pipeline_root_authority
            if type(pipeline_root_authority) is not _PipelineDispatchRootAuthority:
                raise ZeError(
                    "mutation_command_scope_violation",
                    "command dispatch has no exact root authority scope",
                    context={
                        "command_id": command_id,
                        "reason": "command_root_authority_missing",
                    },
                )
            dispatch_scope = (
                self,
                doc,
                effective_command_id,
                callback_actor,
                source,
                actor_authority,
                parent_root_authority_binding,
                pipeline_root_authority,
            )
            dispatch_stack.append(dispatch_scope)
            try:
                op = execute_handler(
                    args=args, actor=callback_actor, source=source, doc=doc,
                )
                if not isinstance(op, (Operation, CommandMutation)):
                    raise ZeError(
                        "validation_failed",
                        f"command {command_id!r} execute returned {type(op).__name__}, "
                        "expected Operation or CommandMutation",
                        context={"command_id": command_id},
                    )
            finally:
                if (
                    not dispatch_stack
                    or dispatch_stack[-1] != dispatch_scope
                ):
                    raise ZeError(
                        "mutation_command_scope_violation",
                        "command dispatch authority stack was corrupted",
                        context={
                            "command_id": command_id,
                            "reason": "command_dispatch_stack_corrupted",
                        },
                    )
                dispatch_stack.pop()
                require_actor_authority()

            require_live_root_policy_authority()

            if mutation_session is not None:
                operations = (
                    (op,) if isinstance(op, Operation) else op.operations
                )
                validate_mutation = getattr(
                    mutation_session, "validate_command_mutation", None,
                )
                protected_segment = False
                if callable(validate_mutation):
                    actual_touches = validate_mutation(
                        effective_command_id,
                        actor_payload=initial_actor_payload,
                        source=source,
                        operations=operations,
                    )
                    deferred_to_root = actual_touches is None
                    if deferred_to_root:
                        if root_dispatch:
                            raise ZeError(
                                "mutation_policy_coverage_invalid",
                                "root command policy validation cannot be deferred",
                                context={"command_id": effective_command_id},
                            )
                        if (
                            parent_source != source
                            or parent_actor_authority != actor_authority
                        ):
                            raise ZeError(
                                "mutation_command_scope_violation",
                                "protected nested dispatch must retain root actor/source",
                                context={
                                    "command_id": command_id,
                                    "reason": "nested_command_actor_source_mismatch",
                                },
                            )
                        record_nested = getattr(
                            mutation_session,
                            "_record_nested_command_operations",
                            None,
                        )
                        if callable(record_nested):
                            record_nested(
                                operations,
                                _capability=_MUTATION_POLICY_COVERAGE_CAPABILITY,
                            )
                        protected_segment = False
                    else:
                        protected_segment = bool(actual_touches) or bool(
                            getattr(
                                mutation_session,
                                "_policy_segment_enforced",
                                False,
                            )
                        )
                        if protected_segment:
                            validate_mutation_operation_coverage(
                                doc,
                                mutation_session.snapshot,
                                actual_touches,
                                operations,
                            )
                    if (
                        protected_segment
                        and not self._is_mutation_authority_bound(
                            effective_command_id,
                            expected=parent_root_authority_binding,
                        )
                    ):
                        raise ZeError(
                            "mutation_policy_command_not_authority",
                            "protected mutation requires an explicit business command authority",
                            context={"command_id": effective_command_id},
                        )
                    if protected_segment and not root_dispatch and (
                        parent_source != source
                        or parent_actor_authority != actor_authority
                    ):
                        raise ZeError(
                            "mutation_command_scope_violation",
                            "protected nested dispatch must retain root actor/source",
                            context={
                                "command_id": command_id,
                                "reason": "nested_command_actor_source_mismatch",
                            },
                        )
                    if (
                        protected_segment
                        and not root_dispatch
                        and not deferred_to_root
                    ):
                        record_nested = getattr(
                            mutation_session, "_record_nested_command_operations", None,
                        )
                        if callable(record_nested):
                            record_nested(
                                operations,
                                _capability=_MUTATION_POLICY_COVERAGE_CAPABILITY,
                            )
                # A policy opt-in command's primary durable audit identity must
                # equal the root identity that authorized its actual mutation.
                # Secondary CommandMutation operations may remain helper ops.
                primary = op if isinstance(op, Operation) else op.operations[0]
                if (
                    root_dispatch
                    and protected_segment
                    and primary.command_id != effective_command_id
                ):
                    raise ZeError(
                        "mutation_operation_authority_mismatch",
                        "primary Operation command_id must equal the root dispatch identity",
                        context={
                            "command_id": effective_command_id,
                            "operation_command_id": primary.command_id,
                        },
                    )
                if root_dispatch:
                    complete_coverage = getattr(
                        mutation_session, "_complete_command_policy_segment", None,
                    )
                    if callable(complete_coverage):
                        complete_coverage(
                            operations=operations,
                            protected_segment=protected_segment,
                            _capability=_MUTATION_POLICY_COVERAGE_CAPABILITY,
                        )
                require_live_root_policy_authority()
                require_actor_authority()
        except BaseException as exc:
            if mutation_session is not None:
                try:
                    mutation_session.rollback()
                except BaseException as rollback_exc:
                    if (
                        isinstance(rollback_exc, ZeError)
                        and rollback_exc.code == "mutation_rollback_failed"
                    ):
                        raise rollback_exc from exc
                    raise ZeError(
                        "mutation_rollback_failed",
                        f"mutation rollback failed: {rollback_exc}",
                        context={
                            "command_id": command_id,
                            "original_error_type": type(exc).__name__,
                            "rollback_error_type": type(rollback_exc).__name__,
                        },
                    ) from rollback_exc
            raise
        else:
            if mutation_session is not None:
                require_live_root_policy_authority()
                mutation_session.commit()

        # W03: CommandRegistry returns the mutation product only; coordinator
        # owns persistence, canonical event publication, and deferred actions.
        return op

    def reserve_confirmation(
        self,
        token: str,
        *,
        actor: Actor,
        approval_channel: str,
        approval_nonce: str,
    ) -> ConfirmationReservation:
        """取得 token 的唯一执行租约；真正消费时点由 CommitReceipt 决定。"""
        return self.pending_store.reserve(
            token,
            actor=actor,
            approval_channel=approval_channel,
            approval_nonce=approval_nonce,
        )

    def confirm(
        self,
        reservation: ConfirmationReservation,
        *,
        actor: Actor,
        doc: Any,
        op_log: OpLog | None = None,
    ) -> Operation | CommandMutation:
        """执行一个已 reserve 的确认请求，但不提前消费 token。

        用冻结快照里的 command_id/args/source 以 confirmed=True 重入 dispatch → 再过 acl/login/poll
        门(防 stale)，然后只返回 mutation。调用方必须让 CommitCoordinator 执行本方法，
        再以 ``PendingStore.finalize(reservation, execution)`` 消费/释放/冻结；任何入口都
        不得在 producer 开始前 pop token。op_log 只是已废弃的签名兼容参数。
        """
        pending = self.pending_store.pending_for(reservation)
        expected_document = self.pending_store.document_for(reservation)
        if expected_document is not doc:
            raise ZeError(
                "confirmation_document_mismatch",
                "confirmation belongs to another Document",
                context={"token": reservation.token},
            )
        if pending.actor.get("user_id") != getattr(actor, "user_id", None):
            # reserve 已验证；这里再守一次，防 reservation 被错误地交给另一 actor 执行。
            raise ZeError(
                "confirmation_actor_mismatch",
                "confirmation reservation belongs to another actor",
                context={"token": reservation.token},
            )
        if getattr(_CONFIRMATION_DISPATCH_LOCAL, "grant", None) is not None:
            raise ZeError(
                "confirmation_authority_busy",
                "this thread already holds a confirmation dispatch grant",
            )
        grant = (self, doc, pending.command_id, actor, pending.source)
        _CONFIRMATION_DISPATCH_LOCAL.grant = grant
        try:
            op = self.dispatch(
                pending.command_id,
                pending.args,
                actor=actor,
                source=pending.source,
                doc=doc,
                op_log=op_log,
                confirmed=True,
            )
        finally:
            if getattr(_CONFIRMATION_DISPATCH_LOCAL, "grant", None) is grant:
                delattr(_CONFIRMATION_DISPATCH_LOCAL, "grant")
        # confirmed=True 必走执行分支 → 返回已执行 mutation(类型收窄给调用方)。
        if not isinstance(op, (Operation, CommandMutation)):
            raise ZeError(
                "confirmation_result_invalid",
                "confirmed command did not return a mutation result",
                context={"command_id": pending.command_id},
            )
        return op

_CONFIRM_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _render_confirm(template: str, args: dict[str, Any]) -> str:
    """把 confirm_template 用 args best-effort 插值成确认摘要。

    安全:只替换 `{name}` 顶层占位为 str(args[name]),**不走 str.format 的 mini-language**
    (避免 `{x.__class__}` / `{x[0]}` 之类属性/下标遍历触达 Python 内部 —— args 是用户输入)。
    缺键的占位原样保留。
    """
    if not template:
        return ""
    return _CONFIRM_PLACEHOLDER.sub(
        lambda m: str(args[m.group(1)]) if m.group(1) in args else m.group(0),
        template,
    )
