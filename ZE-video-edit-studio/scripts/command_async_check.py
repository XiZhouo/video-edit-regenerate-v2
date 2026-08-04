#!/usr/bin/env python3
"""Producer.Effect.Check — command producer 外部副作用静态门禁。

``kind='stage'`` 只是命令目录元数据，不会让 CommandRegistry 自动异步执行。
因此所有 CommandSpec.execute（native/stage/compose 一视同仁）都只能校验、修改
Document 并构造 Operation/CommandMutation；模型、通知、文件发布、线程和运行态控制
必须位于 StageSpec.run 或 DeferredAction.run。

门禁扫描：
  - mod 的 ``@registry.command`` handler；
  - core 的 ``registry.register(CommandSpec(execute=...))`` handler；
  - handler 直接调用与一跳 module helper。

它与 runtime ``effect_context.assert_external_effect_allowed`` 互补：静态检查覆盖框架
无法拦截的任意 Python I/O，runtime port guard 防止动态/间接路径漏网。
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Iterable


BUILD_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MOD_DIR = BUILD_ROOT / "02-mod" / "05-code"
CORE_COMMAND_FILES = (
    BUILD_ROOT / "01-core" / "03-commands-operations" / "01-command" / "builtin_commands.py",
    BUILD_ROOT / "01-core" / "01-data-structure" / "07-type-library" / "envelope.py",
    BUILD_ROOT / "01-core" / "03-commands-operations" / "03-pipeline" / "pipeline_commands.py",
)

DIRECT_EFFECT_NAMES = {
    "llm_call", "image_call", "notify", "write_pack",
}
FILE_WRITE_METHODS = {
    "write_text", "write_bytes", "unlink", "rename", "replace", "mkdir", "rmdir",
}
ASSET_MUTATION_METHODS = {"add_ref", "release", "gc", "promote", "put_file"}
SUPERVISOR_MUTATION_METHODS = {
    "start", "pause", "resume", "cancel", "interrupt", "retry_failed",
    "rerun_step", "run_from_step", "set_breakpoint",
}
SUBPROCESS_CALLS = {"run", "call", "check_call", "check_output", "Popen"}


def _is_command_decorator(dec: ast.expr) -> bool:
    return (
        isinstance(dec, ast.Call)
        and isinstance(dec.func, ast.Attribute)
        and dec.func.attr == "command"
        and isinstance(dec.func.value, ast.Name)
        and "reg" in dec.func.value.id.lower()
    )


def _constant_kwarg(call: ast.Call, name: str) -> str | None:
    for kw in call.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return None


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        left = _dotted_name(node.value)
        return f"{left}.{node.attr}" if left else node.attr
    return ""


def _effect_label(call: ast.Call) -> str | None:
    fn = call.func
    if isinstance(fn, ast.Name):
        return fn.id if fn.id in DIRECT_EFFECT_NAMES else None
    if not isinstance(fn, ast.Attribute):
        return None
    receiver = _dotted_name(fn.value).lower()
    attr = fn.attr
    if attr in DIRECT_EFFECT_NAMES:
        return _dotted_name(fn)
    if attr in FILE_WRITE_METHODS:
        # Document.rename is canonical in-memory mutation, not pathlib rename.
        if attr == "rename" and receiver in {"doc", "document", "self._doc"}:
            return None
        # str.replace(old, new[, count]) 恒 ≥2 参;pathlib.Path.replace(target) 恒 1 参。
        # 按参数个数区分,时间戳等字符串处理不再被误判为文件写(design coresync 反哺)。
        if attr == "replace" and len(call.args) + len(call.keywords) >= 2:
            return None
        return _dotted_name(fn)
    if attr in ASSET_MUTATION_METHODS and any(token in receiver for token in ("asset", "store")):
        return _dotted_name(fn)
    if attr in SUPERVISOR_MUTATION_METHODS and any(
        token in receiver for token in ("supervisor", "pipeline")
    ):
        return _dotted_name(fn)
    if receiver == "subprocess" and attr in SUBPROCESS_CALLS:
        return _dotted_name(fn)
    if receiver == "threading" and attr == "Thread":
        return _dotted_name(fn)
    return None


def _walk_body(fn: ast.AST) -> Iterable[ast.AST]:
    """Walk one function body without treating deferred nested closures as execution."""
    stack = list(getattr(fn, "body", ()))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            stack.append(child)


def _effects_in(fn: ast.AST) -> list[str]:
    found: list[str] = []
    for node in _walk_body(fn):
        if not isinstance(node, ast.Call):
            continue
        label = _effect_label(node)
        if label and label not in found:
            found.append(label)
    return found


def _called_helpers(fn: ast.AST) -> list[str]:
    helpers: list[str] = []
    for node in _walk_body(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id not in helpers:
                helpers.append(node.func.id)
    return helpers


def _registered_commands(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    defs = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    commands: list[tuple[str, ast.AST]] = []

    for fn in defs.values():
        for dec in getattr(fn, "decorator_list", ()):
            if _is_command_decorator(dec):
                commands.append((_constant_kwarg(dec, "id") or fn.name, fn))
                break

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "register"
            and node.args
            and isinstance(node.args[0], ast.Call)
        ):
            continue
        spec_call = node.args[0]
        if _dotted_name(spec_call.func).split(".")[-1] != "CommandSpec":
            continue
        execute_name = None
        for kw in spec_call.keywords:
            if kw.arg == "execute" and isinstance(kw.value, ast.Name):
                execute_name = kw.value.id
                break
        if execute_name and execute_name in defs:
            commands.append((_constant_kwarg(spec_call, "id") or execute_name, defs[execute_name]))

    # Same handler can appear through a decorator and an explicit register in
    # generated code; report it once.
    unique: list[tuple[str, ast.AST]] = []
    seen: set[tuple[str, int]] = set()
    for command_id, fn in commands:
        key = (command_id, getattr(fn, "lineno", 0))
        if key not in seen:
            seen.add(key)
            unique.append((command_id, fn))
    return unique


def _scan_file(path: Path) -> list[dict]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        return [{
            "kind": "PARSE_ERROR", "command": "", "file": path.name,
            "detail": f"无法解析 {path.name}: {exc}", "hint": "修语法后重跑",
        }]
    defs = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    violations: list[dict] = []
    for command_id, fn in _registered_commands(tree):
        effects = [(name, "direct") for name in _effects_in(fn)]
        for helper_name in _called_helpers(fn):
            helper = defs.get(helper_name)
            if helper is None:
                continue
            effects.extend((name, f"helper:{helper_name}") for name in _effects_in(helper))
        if not effects:
            continue
        compact = []
        for effect in effects:
            if effect not in compact:
                compact.append(effect)
        detail = ", ".join(
            f"{name}({route})" for name, route in compact
        )
        violations.append({
            "kind": "PRODUCER_CALLS_EXTERNAL_EFFECT",
            "command": command_id,
            "file": path.name,
            "detail": f"命令 {command_id!r} 的 provisional producer 调用了 {detail}",
            "hint": (
                "Command handler 只能校验/修改 Document/构造 Operation；"
                "外部效果移到 StageSpec.run 或 DeferredAction.run"
            ),
        })

    # A command may be registered through a factory imported from another
    # module, for example ``registry.command(...)(make_handler(registry))``.
    # The handler-aware scan above deliberately does not attempt whole-program
    # call-graph reconstruction, so it cannot see that the nested function
    # returned by ``make_handler`` is the provisional producer.  Direct
    # ``PipelineSupervisor.start`` is nevertheless never a valid mod producer
    # primitive: production HTTP dispatch already owns CommitCoordinator and
    # ``start`` opens another commit through the same coordinator.  That path
    # fails with NestedCommitError.  Mods must prepare one CommandMutation (or
    # explicitly defer a post-commit action) instead.
    reported_start_lines = {
        getattr(node, "lineno", -1)
        for _command_id, fn in _registered_commands(tree)
        for node in _walk_body(fn)
        if isinstance(node, ast.Call) and _effect_label(node) == "supervisor.start"
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        receiver = _dotted_name(node.func.value).lower()
        if node.func.attr != "start" or "supervisor" not in receiver:
            continue
        if getattr(node, "lineno", -1) in reported_start_lines:
            continue
        violations.append({
            "kind": "MOD_DIRECT_SUPERVISOR_START",
            "command": "",
            "file": path.name,
            "line": getattr(node, "lineno", 0),
            "detail": (
                f"mod source 在第 {getattr(node, 'lineno', 0)} 行直接调用 "
                f"{_dotted_name(node.func)}；factory/跨文件注册也不能绕过提交边界"
            ),
            "hint": (
                "在 command producer 中调用 supervisor.prepare_start(...)，并返回它产生的 "
                "CommandMutation；确属提交后控制动作时返回显式 DeferredAction"
            ),
        })
    return violations


def _default_files(mod_dir: Path) -> list[Path]:
    files = sorted(
        path for path in mod_dir.glob("*.py")
        if not path.name.startswith("test_")
    ) if mod_dir.is_dir() else []
    try:
        is_build_default = mod_dir.resolve() == DEFAULT_MOD_DIR.resolve()
    except OSError:
        is_build_default = False
    if is_build_default:
        files.extend(path for path in CORE_COMMAND_FILES if path.is_file())
    return files


def run(mod_dir: Path = DEFAULT_MOD_DIR) -> dict:
    files = _default_files(Path(mod_dir))
    violations = [violation for path in files for violation in _scan_file(path)]
    return {
        "ok": not violations,
        "mod_dir": str(mod_dir),
        "scanned": [str(path.relative_to(BUILD_ROOT)) if path.is_relative_to(BUILD_ROOT)
                    else path.name for path in files],
        "violation_count": len(violations),
        "violations": violations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Command producer 外部副作用门禁")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--mod-dir", default=str(DEFAULT_MOD_DIR))
    args = parser.parse_args()
    result = run(Path(args.mod_dir))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"扫描 {len(result['scanned'])} 个 command source")
        if result["ok"]:
            print("✓ Command producer 无外部/不可回滚副作用")
        else:
            print(f"✗ {result['violation_count']} 处 producer effect 违规")
            for violation in result["violations"]:
                print(f"  [{violation['kind']}] {violation['detail']}\n      → {violation['hint']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
