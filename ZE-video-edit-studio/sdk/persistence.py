"""Canonical CommitExecution projection and human-confirmation executor.

All mutation transports consume the same CommitReceipt facts. Approval authority
is intentionally narrower: only the HTTP human channel receives a nonce and calls
``execute_confirmation``; MCP/AI has no approval execution path.
"""

from __future__ import annotations

from typing import Any

from .error_projection import commit_outcome_unknown
from ._operation_projection import public_operation_payload


def execute_confirmation(
    *,
    registry: Any,
    coordinator: Any,
    token: str,
    actor: Any,
    document: Any,
    approval_channel: str,
    approval_nonce: str,
) -> Any:
    """Reserve → execute one coordinator commit → finalize the confirmation token.

    Ordinary producer/validation exceptions are proven pre-storage and release the token.
    A rollback failure or a non-``Exception`` interruption may have left the outcome unsafe,
    so the token is frozen for repair.  Normal CommitExecution results are classified by the
    receipt itself (committed consume; rolled-back aborted release; dirty/unknown freeze).
    """
    reservation = registry.reserve_confirmation(
        token,
        actor=actor,
        approval_channel=approval_channel,
        approval_nonce=approval_nonce,
    )
    try:
        execution = coordinator.execute(
            lambda: registry.confirm(
                reservation,
                actor=actor,
                doc=document,
                op_log=None,
            ),
            trace_id=reservation.pending.trace_id,
        )
    except Exception as exc:
        code = str(getattr(exc, "code", "") or "")
        if commit_outcome_unknown(exc):
            registry.pending_store.freeze(
                reservation,
                code=code or "history_authority_mismatch",
                message="confirmation commit outcome requires repair",
            )
        elif code == "mutation_rollback_failed":
            registry.pending_store.freeze(
                reservation,
                code=code,
                message=str(exc) or "confirmation rollback failed",
            )
        else:
            registry.pending_store.release(reservation)
        raise
    except BaseException as exc:
        registry.pending_store.freeze(
            reservation,
            code="confirmation_execution_interrupted",
            message=str(exc) or type(exc).__name__,
        )
        raise
    registry.pending_store.finalize(reservation, execution)
    return execution


def persistence_payload(doc: Any, *, document_durable: bool) -> dict[str, Any]:
    """Return the additive W02 persistence envelope for HTTP/MCP/CLI clients."""
    policy = str(getattr(doc, "effective_save_policy", "legacy") or "legacy")
    return {
        "status": "clean" if document_durable else "dirty",
        "document_version": int(getattr(doc, "version", 0) or 0),
        "save_policy": policy,
        "document_durable": bool(document_durable),
        # W02 的成功路径均自动持久化；dirty 只为迁移期/失败投影保留。无完整
        # CommitReceipt 时不能证明业务命令可安全重放，因此一律不建议重试。
        "retry_safe": False,
    }


def commit_execution_payload(
    execution: Any,
    *,
    document: Any,
    many: bool = False,
    include_approval: bool = False,
) -> dict[str, Any]:
    """把统一 CommitExecution 投影为现有 HTTP/MCP/CLI 加性响应形状。

    这只是 transport 序列化，不再执行 save/append/publish。真正的耐久事实直接使用
    CommitReceipt 固定 wire dict，避免每个入口重新解释提交是否成功。
    """
    pending = getattr(execution, "pending_confirmation", None)
    if pending is not None:
        return {"pending": pending.to_public_dict(include_approval=include_approval)}
    return _committed_payload(execution, document=document, many=many)


def pending_confirmation_frame(pending: Any, *, doc_id: str) -> dict[str, Any]:
    """``pending_confirmation`` 广播帧:AI 发起的需确认命令 → 通知网页拉起人工批准 UI。

    只携带 token(定位符)与展示信息;**approval_nonce 绝不进广播**——批准材料仅经
    浏览器 cookie 专属端点 ``GET /api/command/pending-approval`` 下发(AI 子进程只持
    Bearer session token、无浏览器 cookie,结构上拿不到 nonce,不能自批)。
    ``entity_ref`` 是从冻结 args 提取的展示级锚点提示(精灵圈定目标用),可为空。
    """
    args = getattr(pending, "args", None) or {}
    entity_type = str(args.get("entity_type", "") or "")
    entity_id = str(args.get("entity_id", "") or args.get("id", "") or "")
    actor = getattr(pending, "actor", None) or {}
    return {
        "type": "pending_confirmation",
        "doc_id": doc_id,
        "token": pending.token,
        "command_id": pending.command_id,
        "source": pending.source,
        "summary": getattr(pending, "summary", "") or "",
        "ts": pending.ts,
        "actor": {"user_id": str(actor.get("user_id", "") or "")},
        "entity_ref": (
            {"entity_type": entity_type, "entity_id": entity_id}
            if entity_type or entity_id else None
        ),
    }


def _committed_payload(execution: Any, *, document: Any, many: bool) -> dict[str, Any]:

    operations = tuple(getattr(execution, "operations", ()) or ())
    events = tuple(getattr(execution, "events", ()) or ())
    actions = tuple(getattr(execution, "actions", ()) or ())
    receipt = getattr(execution, "receipt", None)
    if not operations:
        # no-op（例如已经无可撤销内容）没有新的 commit receipt；返回当前 canonical
        # 的兼容 clean 快照，让 UI 恢复此前 settled 状态而不是误报“缺回执待修复”。
        base = {"ops": [], "changes": []} if many else {"op": None}
        base["persistence"] = persistence_payload(document, document_durable=True)
        return base
    if receipt is None:
        raise ValueError("mutation CommitExecution requires a receipt")

    payload: dict[str, Any]
    if many:
        payload = {
            "ops": [public_operation_payload(operation) for operation in operations],
            "changes": [dict(event) for event in events],
        }
    else:
        payload = {"op": public_operation_payload(operations[0])}
        if len(operations) > 1:
            # 命令主 Operation 固定在首位，保持旧客户端的 ``op`` 合同；同一 logical
            # commit 的占位/派生 Operations 通过加性的 ``ops`` 与 changes[] 暴露。
            payload["ops"] = [
                public_operation_payload(operation) for operation in operations
            ]
        if len(events) > 1:
            payload["changes"] = [dict(event) for event in events]
        elif events:
            payload["changes"] = dict(events[0])
    payload["persistence"] = receipt.to_dict()
    if actions:
        payload["actions"] = [action.to_dict() for action in actions]
        statuses = {action.status.value for action in actions}
        payload["action_status"] = (
            "failed" if "failed" in statuses
            else "skipped" if "skipped" in statuses
            else "succeeded"
        )
    return payload
