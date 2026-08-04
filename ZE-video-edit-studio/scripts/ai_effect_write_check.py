#!/usr/bin/env python3
"""AI.Effect.Write.Check — AI 可调命令 producer 的文件写/子进程执行安全门禁。

威胁模型:``sources_allowed`` 含 ``ai`` 的命令可被 MCP/copilot 以 ``source='ai'`` 直接调用。
若这类命令的 execute(producer)直接 ``open(...,"w")`` / ``Path.write_*`` / ``subprocess`` /
``os.system``,等于把「AI 触发的任意文件写 / 命令执行」暴露到不可回滚、不经审批的通道。
框架红线:producer 只校验/改 Document/构造 Operation;一切文件产物必须走 artifact 协议
(StageSpec.run → 暂存 → commit 成功后 promote + receipt,见 03-pipeline/artifact_scan.py),
一切子进程/外部效果放 StageSpec.run 或 DeferredAction.run。producer 内做这些 = 违规。

两级输出:
  - ERROR(H,exit 1):ai-callable 命令的 producer(+一跳 helper)有 open(写)/write_*/subprocess/
    os.system|popen。修法:作者显式收 ``sources_allowed``(去掉 ``ai``)或把写/执行移进 artifact 协议。
  - WARN(C,不影响 exit):命令有 mutation_authority 或 producer 触 FS,却未显式声明 ``sources_allowed``
    (在吃默认全通道)。对齐 CLAUDE.md「声明的规则落机械 lint」;只提示,不 fail。

与既有 command-async 门禁互补:后者禁 producer 一切外部效果(架构视角,含 write_text/subprocess),
但漏 **裸 open(...,"w")**;本门禁补齐 open 检测并按 ai-callability 定级(安全视角)。
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from command_async_check import (  # noqa: E402
    _called_helpers,
    _constant_kwarg,
    _default_files,
    _dotted_name,
    _is_command_decorator,
    _walk_body,
    BUILD_ROOT,
)

# open()/`.open()` 的写模式识别:合法 mode 串只由这些字符组成,且含至少一个写字符。
# 这样 `.open("http://www...")` 这类非文件 open(首参是长 URL,含 'w' 但整体不是 mode 串)不误报。
_MODE_ALPHABET = set("rwaxbt+U")
_MODE_WRITE_CHARS = set("wax+")
_WRITE_ATTR_METHODS = {"write_text", "write_bytes"}
_SUBPROCESS_CALLS = {"run", "call", "check_call", "check_output", "Popen", "getoutput", "getstatusoutput"}
_OS_EXEC_CALLS = {"system", "popen"}


def _looks_like_write_mode(value: str) -> bool:
    return bool(value) and set(value) <= _MODE_ALPHABET and bool(set(value) & _MODE_WRITE_CHARS)


def _is_write_open(call: ast.Call, *, path_open: bool) -> bool:
    """open(file, mode) / path.open(mode):仅当能看到写模式的**字符串字面量**才判写。

    非常量模式(变量/表达式)不判(与 command-async 同类静态盲点;运行时 port guard 兜底),
    以零误报换取「明显的 open(...,'w')」被硬拦。读模式('r'/'rb')与无模式参数(默认读)放行。
    """
    modes: list[str] = []
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            modes.append(kw.value.value)
    mode_index = 0 if path_open else 1          # builtin open: 首参是路径,次参是 mode
    if len(call.args) > mode_index:
        node = call.args[mode_index]
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            modes.append(node.value)
    return any(_looks_like_write_mode(m) for m in modes)


def _effect_label(call: ast.Call) -> str | None:
    fn = call.func
    if isinstance(fn, ast.Name):
        if fn.id == "open" and _is_write_open(call, path_open=False):
            return "open(write)"
        return None
    if not isinstance(fn, ast.Attribute):
        return None
    attr = fn.attr
    receiver = _dotted_name(fn.value).lower()
    if attr == "open" and _is_write_open(call, path_open=True):
        return f"{_dotted_name(fn)}(write)"
    if attr in _WRITE_ATTR_METHODS:
        return _dotted_name(fn)
    if receiver == "subprocess" and attr in _SUBPROCESS_CALLS:
        return _dotted_name(fn)
    if receiver == "os" and attr in _OS_EXEC_CALLS:
        return _dotted_name(fn)
    return None


def _effects_in(fn: ast.AST) -> list[str]:
    found: list[str] = []
    for node in _walk_body(fn):
        if isinstance(node, ast.Call):
            label = _effect_label(node)
            if label and label not in found:
                found.append(label)
    return found


def _kwarg_node(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _literal_str_tuple(node: ast.expr | None) -> tuple[str, ...] | None:
    """把 ``sources_allowed=("user","system")`` 解析成字面 tuple;非字面(变量/表达式)→ None。"""
    if isinstance(node, (ast.Tuple, ast.List)):
        out: list[str] = []
        for el in node.elts:
            if isinstance(el, ast.Constant) and isinstance(el.value, str):
                out.append(el.value)
            else:
                return None
        return tuple(out)
    return None


def _is_ai_callable(sources_node: ast.expr | None) -> bool:
    if sources_node is None:
        return True                              # 默认 SOURCES 含 "ai"
    literal = _literal_str_tuple(sources_node)
    if literal is None:
        return True                              # 非字面无法静态判定 → 安全门 fail-closed
    return "ai" in literal


def _bool_literal(node: ast.expr | None) -> bool | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    return None


def _registered_commands(tree: ast.Module):
    """(command_id, execute_fn, sources_node, mutation_node) —— 装饰器 + CommandSpec 两式。"""
    defs = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    out: list[tuple[str, ast.AST, ast.expr | None, ast.expr | None]] = []
    seen: set[tuple[str, int]] = set()

    def _add(command_id: str, fn: ast.AST, sources: ast.expr | None, mut: ast.expr | None) -> None:
        key = (command_id, getattr(fn, "lineno", 0))
        if key not in seen:
            seen.add(key)
            out.append((command_id, fn, sources, mut))

    for fn in defs.values():
        for dec in getattr(fn, "decorator_list", ()):
            if isinstance(dec, ast.Call) and _is_command_decorator(dec):
                _add(_constant_kwarg(dec, "id") or fn.name, fn,
                     _kwarg_node(dec, "sources_allowed"), _kwarg_node(dec, "mutation_authority"))
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
            _add(_constant_kwarg(spec_call, "id") or execute_name, defs[execute_name],
                 _kwarg_node(spec_call, "sources_allowed"), _kwarg_node(spec_call, "mutation_authority"))
    return out


def _scan_file(path: Path) -> tuple[list[dict], list[dict]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        return ([{
            "kind": "PARSE_ERROR", "command": "", "file": path.name,
            "detail": f"无法解析 {path.name}: {exc}", "hint": "修语法后重跑",
        }], [])
    defs = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    errors: list[dict] = []
    warnings: list[dict] = []
    for command_id, fn, sources_node, mut_node in _registered_commands(tree):
        effects = [(name, "direct") for name in _effects_in(fn)]
        for helper_name in _called_helpers(fn):
            helper = defs.get(helper_name)
            if helper is not None:
                effects.extend((name, f"helper:{helper_name}") for name in _effects_in(helper))
        compact: list[tuple[str, str]] = []
        for effect in effects:
            if effect not in compact:
                compact.append(effect)
        has_fs_effect = bool(compact)
        ai_callable = _is_ai_callable(sources_node)
        sources_declared = sources_node is not None
        mutation_authority = _bool_literal(mut_node) is True

        flagged_error = has_fs_effect and ai_callable
        if flagged_error:
            detail = ", ".join(f"{name}({route})" for name, route in compact)
            errors.append({
                "kind": "AI_CALLABLE_PRODUCER_WRITES_FS",
                "command": command_id,
                "file": path.name,
                "detail": f"ai-callable 命令 {command_id!r} 的 producer 直接 {detail}",
                "hint": (
                    "producer 只能改 Document/构造 Operation;文件写走 artifact 协议"
                    "(StageSpec.run→暂存→receipt),子进程走 StageSpec.run/DeferredAction.run;"
                    "确实无需 AI 调用则显式声明 sources_allowed 去掉 'ai'"
                ),
            })

        # C:声明卫生 lint(只 WARN)。已被 H 判 error 的不重复。
        if not flagged_error and not sources_declared and (mutation_authority or has_fs_effect):
            reason = "mutation_authority=True" if mutation_authority else "producer 触 FS"
            warnings.append({
                "kind": "UNDECLARED_SOURCES_ALLOWED",
                "command": command_id,
                "file": path.name,
                "detail": (
                    f"命令 {command_id!r} {reason},却未显式声明 sources_allowed(在吃默认全通道)"
                ),
                "hint": "显式写 sources_allowed=(...) 声明允许哪些调用源(user/ai/sync/system)",
            })
    return errors, warnings


def run(mod_dir: Path | None = None) -> dict:
    from command_async_check import DEFAULT_MOD_DIR
    target = Path(mod_dir) if mod_dir is not None else DEFAULT_MOD_DIR
    files = _default_files(target)
    errors: list[dict] = []
    warnings: list[dict] = []
    for path in files:
        e, w = _scan_file(path)
        errors.extend(e)
        warnings.extend(w)
    return {
        "ok": not errors,
        "mod_dir": str(target),
        "scanned": [str(p.relative_to(BUILD_ROOT)) if p.is_relative_to(BUILD_ROOT) else p.name
                    for p in files],
        "error_count": len(errors),
        "errors": errors,
        "warning_count": len(warnings),
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="AI 可调命令 producer 文件写/子进程安全门禁")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--mod-dir", default=None)
    args = parser.parse_args()
    result = run(Path(args.mod_dir) if args.mod_dir else None)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"扫描 {len(result['scanned'])} 个 command source")
        for warning in result["warnings"]:
            print(f"  ⚠ [{warning['kind']}] {warning['detail']}\n      → {warning['hint']}")
        if result["ok"]:
            print("✓ ai-callable producer 无 artifact 协议外的文件写/子进程")
        else:
            print(f"✗ {result['error_count']} 处 ai 可调 producer 写文件/执行子进程违规")
            for violation in result["errors"]:
                print(f"  [{violation['kind']}] {violation['detail']}\n      → {violation['hint']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
