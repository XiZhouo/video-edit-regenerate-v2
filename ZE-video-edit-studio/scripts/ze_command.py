#!/usr/bin/env python3
"""scripts/ze_command.py — ZE-skill-build 命令 CLI 镜像 (AI 的左手).

用法:
    # 写(走命令总线,产生 Operation)
    python scripts/ze_command.py run <command_id> [--project <id>] --payload '<json args>'
    python scripts/ze_command.py projects                # 列仓库里的 project

    # 读(AI 最低接口 introspection;与 server GET 路由共用同一 reader,同形返回)
    python scripts/ze_command.py describe [--project <id>]            # Context.Describe
    python scripts/ze_command.py commands [--project <id>]            # Command.List
    python scripts/ze_command.py command-describe <id> [--project]    # Command.Describe
    python scripts/ze_command.py editors [--project <id>]             # Editor.List
    python scripts/ze_command.py entities [--type T] [--project]      # Entity.List
    python scripts/ze_command.py entity-select --type T --id X [--project]  # Entity.Select
    python scripts/ze_command.py tasks [--project <id>]               # Task.List
    python scripts/ze_command.py task-status <run_id> [--project]     # Task.Status
    python scripts/ze_command.py oplog [--type T --id X --source S --limit N] [--project]  # OperationLog.Query
    python scripts/ze_command.py settings                             # Settings.Read(skill 级)
    python scripts/ze_command.py manual                               # Help.ReadManual(skill 级)

多项目仓库:bootstrap() 返回 Repository(多 project)。CLI 需指定 `--project <id>`
(仓库仅一个 project 时可省,自动选);在该 project 的 Context 上 dispatch / 读。

**信任边界**:CLI = 本机全权,actor 用保留系统账号 `__system__`,**不过 HTTP ACL**
(ACL 只在 server 的 HTTP 层挡远程用户)。server 在跑时勿用 CLI 并发改同一 project
(各持自己 in-memory Document,内存与磁盘可能不同步);CLI 适合 server 没跑时的批处理。
读子命令是只读 introspection,server 跑时并发读安全。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from contextlib import nullcontext
from pathlib import Path

# ROOT 上 path, 保证 `import sdk` 在任意 cwd 成立
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sdk import (  # noqa: E402
    Actor, CommandRegistry, ZeError, credential_context,
    diagnose_document, diagnose_build_report,
    describe_context, list_commands, describe_command, list_entities, entity_select,
    list_editors, list_pipelines, list_tasks, task_status, query_oplog, settings_read, read_manual,
)
from sdk.bootstrap import bootstrap, SYSTEM_USER_ID  # noqa: E402
from sdk.error_projection import (  # noqa: E402
    commit_outcome_unknown,
    is_commit_authority_error,
    public_commit_authority_context,
    public_error_message,
)
from sdk.persistence import commit_execution_payload  # noqa: E402


def _emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _commit_authority_error_payload(error: BaseException) -> dict[str, object]:
    payload: dict[str, object] = {
        "code": getattr(error, "code", "commit_authority_failed"),
        "message": public_error_message(error),
    }
    context = public_commit_authority_context(error)
    if context:
        payload["context"] = context
    return payload


def _ctx(repo, project):
    """定位并懒加载目标 project 的 Context(读子命令共用)。"""
    return repo.get_or_load(_resolve_project(repo, project))


def _resolve_project(repo, requested):
    """定位目标 project:显式 --project 优先;否则仓库唯一时自动选;多/零则报错。"""
    ids = repo.list_project_ids()
    if requested:
        if not repo.project_exists(requested):
            raise ZeError("project_not_found", f"no project {requested!r}", context={"available": ids})
        return requested
    if len(ids) == 1:
        return ids[0]
    if not ids:
        raise ZeError("project_not_found", "仓库没有任何 project(先经 web 新建)", context={})
    raise ZeError("validation_failed", f"仓库有多个 project,需用 --project 指定: {ids}",
                  context={"available": ids})


def _load_static_commands_register():
    path = ROOT / "02-mod" / "05-code" / "commands_register.py"
    spec = importlib.util.spec_from_file_location("_ze_static_commands_register", path)
    if spec is None or spec.loader is None:
        raise ZeError("command_registry_unavailable", f"cannot load commands_register.py: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    register = getattr(module, "register", None)
    if not callable(register):
        raise ZeError("command_registry_unavailable", f"commands_register.py has no register(): {path}")
    return register


def _static_cmd_reg() -> CommandRegistry:
    reg = CommandRegistry(enforce_login=False)
    _load_static_commands_register()(reg)
    return reg


def main() -> None:
    parser = argparse.ArgumentParser(prog="ze_command", description="ZE 命令 CLI 镜像")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("projects", help="列仓库里的 project")
    run = sub.add_parser("run", help="dispatch 一条 command")
    run.add_argument("command_id", help="如 entity.add / entity.update / item.toggle_done")
    run.add_argument("--project", default=None, help="目标 project id(仓库唯一时可省)")
    run.add_argument("--payload", default="{}", help="命令 args 的 JSON 字符串")
    run.add_argument("--idempotency-key", default="", help="同一写请求重试时复用的幂等键")
    run.add_argument("--json", action="store_true", help="兼容标准 ZE CLI；输出本来就是 JSON")

    # ---- 只读 introspection 子命令(AI 最低接口;与 server GET 共用 reader)----
    def _p(name, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--project", default=None, help="目标 project id(仓库唯一时可省)")
        return sp
    desc = _p("describe", "Context.Describe 或 Command.Describe 兼容入口")
    desc.add_argument("command_id", nargs="?", help="可选；给出时等同 command-describe")
    desc.add_argument("--json", action="store_true", help="兼容标准 ZE CLI；输出本来就是 JSON")
    cmds = _p("commands", "Command.List:列出命令")
    cmds.add_argument("--json", action="store_true", help="兼容标准 ZE CLI；输出本来就是 JSON")
    lst = _p("list", "Command.List 标准别名")
    lst.add_argument("--json", action="store_true", help="兼容标准 ZE CLI；输出本来就是 JSON")
    cd = _p("command-describe", "Command.Describe:单命令详情")
    cd.add_argument("command_id")
    cd.add_argument("--json", action="store_true", help="兼容标准 ZE CLI；输出本来就是 JSON")
    _p("editors", "Editor.List:编辑器映射")
    en = _p("entities", "Entity.List:轻量列实体")
    en.add_argument("--type", default=None)
    es = _p("entity-select", "Entity.Select:校验存在")
    es.add_argument("--type", required=True)
    es.add_argument("--id", required=True)
    _p("tasks", "Task.List:管线 run 快照")
    ts = _p("task-status", "Task.Status:单 run 状态")
    ts.add_argument("run_id")
    ol = _p("oplog", "OperationLog.Query:过滤审计轨迹")
    ol.add_argument("--type", default=None)
    ol.add_argument("--id", default=None)
    ol.add_argument("--source", default=None)
    ol.add_argument("--limit", type=int, default=None)
    dg = _p("diagnose", "Schema Doctor:诊断数据结构对不上(逐处 path/期望/修法/AI 指令)")
    dg.add_argument("--type", default=None, help="限单实体类型(配 --id)")
    dg.add_argument("--id", default=None, help="限单实体 id(配 --type)")
    dg.add_argument("--json", action="store_true", help="兼容标准 ZE CLI;输出本来就是 JSON")
    sub.add_parser("settings", help="Settings.Read:运行配置(skill 级,脱敏)")
    sub.add_parser("manual", help="Help.ReadManual:说明书(skill 级)")

    args = parser.parse_args()

    repo = bootstrap()

    if args.cmd == "projects":
        for pid in repo.list_project_ids():
            m = repo.project_meta(pid)
            print(f"{pid}\t{m['name']}\towner={m['acl'].get('owner', '')}")
        return

    # ---- skill 级只读(无需 project)----
    if args.cmd == "settings":
        _emit(settings_read(repo.config))   # 复用同一 reader(与 server /api/config 同源)
        return
    if args.cmd == "manual":
        _emit(read_manual(repo.introspect))
        return

    # ---- project 级只读 introspection ----
    _READERS = {
        "describe", "commands", "list", "command-describe", "editors",
        "entities", "entity-select", "tasks", "task-status", "oplog",
        "diagnose",
    }
    if args.cmd in _READERS:
        try:
            project_for_read = args.project
            if not project_for_read and (args.cmd in {"list", "commands", "command-describe"} or (args.cmd == "describe" and getattr(args, "command_id", None))):
                ids = repo.list_project_ids()
                if ids:
                    project_for_read = ids[0]
                else:
                    cmd_reg = _static_cmd_reg()
                    if args.cmd == "describe":
                        _emit(describe_command(cmd_reg, args.command_id))
                    elif args.cmd in ("commands", "list"):
                        _emit({"commands": list_commands(cmd_reg), "pipelines": list_pipelines(repo.introspect)})
                    elif args.cmd == "command-describe":
                        _emit(describe_command(cmd_reg, args.command_id))
                    return
            ctx = _ctx(repo, project_for_read)
            lifecycle_guard = getattr(ctx, "lifecycle_guard", nullcontext)
            with lifecycle_guard():
                with ctx.lock:
                    if args.cmd == "describe":
                        if getattr(args, "command_id", None):
                            _emit(describe_command(ctx.cmd_reg, args.command_id))
                        else:
                            _emit(describe_context(ctx, introspect=repo.introspect, config=repo.config))
                    elif args.cmd in ("commands", "list"):
                        _emit({"commands": list_commands(ctx.cmd_reg), "pipelines": list_pipelines(repo.introspect)})
                    elif args.cmd == "command-describe":
                        _emit(describe_command(ctx.cmd_reg, args.command_id))
                    elif args.cmd == "editors":
                        _emit({"editors": list_editors(repo.introspect, ctx.cmd_reg)})
                    elif args.cmd == "entities":
                        _emit({"entities": list_entities(ctx.doc, args.type)})
                    elif args.cmd == "entity-select":
                        _emit(entity_select(ctx.doc, args.type, args.id))
                    elif args.cmd == "tasks":
                        _emit({"tasks": list_tasks(ctx.supervisor)})
                    elif args.cmd == "task-status":
                        _emit(task_status(ctx.supervisor, args.run_id))
                    elif args.cmd == "oplog":
                        _emit(query_oplog(ctx.op_log, entity_type=args.type, entity_id=args.id,
                                          source=args.source, limit=args.limit))
                    elif args.cmd == "diagnose":
                        if args.type and args.id:
                            ent = ctx.doc.get(args.type, args.id)
                            schema = (ctx.doc.schema or {}).get(args.type) or {}
                            _emit(diagnose_build_report(
                                getattr(ent, "data", None), schema, entity_type=args.type,
                                entity_id=args.id, trigger="cli", redact=True,
                                path_prefix=f"{args.type}/{args.id}"))
                        else:
                            _emit(diagnose_document(ctx.doc, redact=True, trigger="cli"))
        except ZeError as e:
            _emit({"error": {"code": e.code, "message": str(e)}})
            sys.exit(1)
        except Exception as error:
            if not is_commit_authority_error(error):
                raise
            _emit({"error": _commit_authority_error_payload(error)})
            sys.exit(1)
        return

    if args.cmd != "run":
        parser.print_help()
        sys.exit(2)

    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": {"code": "bad_payload", "message": str(e)}}, ensure_ascii=False))
        sys.exit(2)
    if isinstance(payload, dict):
        body_key = str(payload.pop("_idempotency_key", "") or "")
        if not args.idempotency_key:
            args.idempotency_key = body_key

    try:
        ctx = repo.get_or_load(_resolve_project(repo, args.project))
    except ZeError as e:
        print(json.dumps({"error": {"code": e.code, "message": str(e)}}, ensure_ascii=False))
        sys.exit(1)
    except Exception as error:
        if not is_commit_authority_error(error):
            raise
        _emit({"error": _commit_authority_error_payload(error)})
        sys.exit(1)

    # CLI = 本机全权:系统账号(非空 user_id 过 enforce_login),source='ai',不过 HTTP ACL。
    cli_actor = Actor(user_id=SYSTEM_USER_ID, source="ai")
    lifecycle_guard = getattr(ctx, "lifecycle_guard", nullcontext)
    def _do():
        with lifecycle_guard():
            with ctx.lock:
                with credential_context(
                    cli_actor, repo.credential_store, repo.policy_store, repo.tool_id,
                ):
                    execution = ctx.commit.execute(lambda: ctx.cmd_reg.dispatch(
                        args.command_id, payload,
                        actor=cli_actor, source="ai",
                        doc=ctx.doc, op_log=None,
                    ))
                return commit_execution_payload(execution, document=ctx.doc)

    try:
        out = repo.idempotency_store.run(
            scope=f"cli.command.{args.command_id}:project={ctx.doc.id}:actor={SYSTEM_USER_ID}",
            key=args.idempotency_key,
            request_payload={"command_id": args.command_id, "project_id": ctx.doc.id, "payload": payload},
            action=_do,
            outcome_unknown_classifier=commit_outcome_unknown,
        )
    except ZeError as e:
        print(json.dumps({"error": {"code": e.code, "message": str(e)}}, ensure_ascii=False))
        sys.exit(1)
    except Exception as error:
        if not is_commit_authority_error(error):
            raise
        _emit({"error": _commit_authority_error_payload(error)})
        sys.exit(1)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
