"""pipeline_commands — framework 内置的管线生命周期 command.

pause / interrupt / resume / cancel / retry_failed 等操作一个已存在的 run(按 pipeline_id),是通用的,
故 framework 提供。它们经闭包拿到 PipelineSupervisor(execute 签名 (args,actor,source,doc)
不带 supervisor),由 bootstrap 在 supervisor 建好后调 register_pipeline_commands 注册。

注:pipeline.start 是业务特定的(mod 知道要生成什么 work-list + stage)——故不在这里,
mod 用 registry.pipeline_supervisor 自建 start 命令(见 02-mod/05-code 示例)。

这些 command 不改 Document,只控 supervisor;返回空 patches 的 kind='stage' Operation,
让 oplog / footer / pipeline editor 仍记录"发生了管线控制"。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_OPERATION_DIR = _HERE.parent / "02-operation"
_COMMAND_DIR = _HERE.parent / "01-command"
for _d in (_OPERATION_DIR, _COMMAND_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from operation import Operation, new_operation  # noqa: E402
from command import (  # noqa: E402
    CommandMutation, CommandRegistry, CommandSpec, DeferredAction,
)

PIPELINE_LIFECYCLE_COMMANDS = (
    "pipeline.pause", "pipeline.interrupt", "pipeline.resume", "pipeline.cancel",
    "pipeline.retry_failed", "pipeline.rerun_step", "pipeline.run_from_step",
    "pipeline.set_breakpoint",
)


def _lifecycle_op(command_id: str, source: str, actor: Any, doc: Any) -> Operation:
    """空 patches 的生命周期 Operation(kind='stage';不改 Document)。"""
    return new_operation(
        command_id=command_id,
        source=source,
        actor=actor.to_dict(),
        document_id=doc.id,
        patches=[],
        reverse_patches=[],
        kind="stage",
    )


def register_pipeline_commands(registry: CommandRegistry, supervisor: Any) -> None:
    """注册生命周期 command(闭包持 supervisor)。bootstrap 在 supervisor 建好后调。"""

    def _owned_args(args: dict[str, Any], *keys: str) -> dict[str, Any]:
        """Detach every lifecycle value used after the producer returns."""
        owned: dict[str, Any] = {}
        for key in keys:
            if key not in args:
                continue
            value = args[key]
            owned[key] = list(value) if type(value) is list else value
        return owned

    def _prepared(
        command_id: str,
        args: dict[str, Any],
        actor: Any,
        source: str,
        doc: Any,
        action,
    ) -> CommandMutation:
        # 这里只做读验证 + 构造审计 Operation；真正 supervisor 副作用必须在
        # CommitCoordinator 已确认业务提交并接受 canonical event 后执行。
        validated_scope = supervisor.validate_lifecycle(command_id, args)
        op = _lifecycle_op(command_id, source, actor, doc)
        return CommandMutation(
            operations=(op,),
            after_commit=(DeferredAction(
                id=f"{command_id}:{op.id}",
                name=command_id,
                run=lambda _commit_id: action(validated_scope),
            ),),
        )

    def _pause(args: dict[str, Any], actor: Any, source: str, doc: Any) -> CommandMutation:
        owned = _owned_args(args, "pipeline_id")
        return _prepared(
            "pipeline.pause", owned, actor, source, doc,
            lambda _scope: supervisor.pause(owned["pipeline_id"]),
        )

    def _resume(args: dict[str, Any], actor: Any, source: str, doc: Any) -> CommandMutation:
        owned = _owned_args(args, "pipeline_id")
        return _prepared(
            "pipeline.resume", owned, actor, source, doc,
            lambda _scope: supervisor.resume(owned["pipeline_id"]),
        )

    def _interrupt(args: dict[str, Any], actor: Any, source: str, doc: Any) -> CommandMutation:
        owned = _owned_args(args, "pipeline_id")
        return _prepared(
            "pipeline.interrupt", owned, actor, source, doc,
            lambda _scope: supervisor.interrupt(owned["pipeline_id"]),
        )

    def _cancel(args: dict[str, Any], actor: Any, source: str, doc: Any) -> CommandMutation:
        owned = _owned_args(args, "pipeline_id")
        return _prepared(
            "pipeline.cancel", owned, actor, source, doc,
            lambda _scope: supervisor.cancel(owned["pipeline_id"]),
        )

    def _retry(args: dict[str, Any], actor: Any, source: str, doc: Any) -> CommandMutation:
        owned = _owned_args(args, "pipeline_id", "item_ids")
        return _prepared(
            "pipeline.retry_failed", owned, actor, source, doc,
            lambda scope: supervisor.retry_failed(
                owned["pipeline_id"],
                list(scope) if scope is not None else owned.get("item_ids"),
            ),
        )

    def _rerun_step(args: dict[str, Any], actor: Any, source: str, doc: Any) -> CommandMutation:
        owned = _owned_args(args, "pipeline_id", "stage_id", "item_id")
        return _prepared(
            "pipeline.rerun_step", owned, actor, source, doc,
            lambda _scope: supervisor.rerun_step(
                owned["pipeline_id"],
                stage_id=owned["stage_id"],
                item_id=owned.get("item_id"),
            ),
        )

    def _run_from_step(args: dict[str, Any], actor: Any, source: str, doc: Any) -> CommandMutation:
        owned = _owned_args(args, "pipeline_id", "stage_id", "item_id")
        return _prepared(
            "pipeline.run_from_step", owned, actor, source, doc,
            lambda _scope: supervisor.run_from_step(
                owned["pipeline_id"],
                stage_id=owned["stage_id"],
                item_id=owned.get("item_id"),
            ),
        )

    def _set_breakpoint(args: dict[str, Any], actor: Any, source: str, doc: Any) -> CommandMutation:
        # 「关闭」(halt=True)/「开启」(halt=False)某 stage 的 halt-after 断点;继续走 pipeline.resume。
        owned = _owned_args(args, "pipeline_id", "stage_id", "halt")
        return _prepared(
            "pipeline.set_breakpoint", owned, actor, source, doc,
            lambda _scope: supervisor.set_breakpoint(
                owned["pipeline_id"], stage_id=owned["stage_id"],
                halt=bool(owned.get("halt", True)),
            ),
        )

    registry.register(CommandSpec(id="pipeline.pause", label="暂停管线", kind="stage", execute=_pause))
    registry.register(CommandSpec(id="pipeline.interrupt", label="中断当前步骤", kind="stage", execute=_interrupt))
    registry.register(CommandSpec(id="pipeline.resume", label="继续管线", kind="stage", execute=_resume))
    registry.register(CommandSpec(id="pipeline.cancel", label="取消管线", kind="stage", execute=_cancel))
    registry.register(CommandSpec(id="pipeline.retry_failed", label="重试失败项", kind="stage", execute=_retry))
    registry.register(CommandSpec(id="pipeline.rerun_step", label="重跑步骤", kind="stage", execute=_rerun_step))
    registry.register(CommandSpec(id="pipeline.run_from_step", label="从步骤继续", kind="stage", execute=_run_from_step))
    registry.register(CommandSpec(id="pipeline.set_breakpoint", label="设置断点", kind="stage", execute=_set_breakpoint))
