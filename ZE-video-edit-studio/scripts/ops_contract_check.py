#!/usr/bin/env python3
"""OPS.Contract.Check — core/sdk 的 env 读取与 OPS 配置词表(04-ops)契约一致性门禁。

对位 `css_contract_check.py`(JSX↔css 词表):那个扫 .tsx 断言 className 都在 css 定义过;
本检查器扫 .py 断言 core/sdk 读的每个 `ZE_*` 环境变量都在 `config_schema.py` 声明过。

A 轨闭环(加性·不动 core):core 那些 `os.environ.get("ZE_…")`(及 `src.get(...)`、
`_ENV_PREFIX + "..."` 拼接、`"ZE_RB_" + scope` 动态 scope 等读法)**原样不动**,本门禁只做
静态交叉校验 —— 谁加了未声明的 env 读取,这里 exit 1。

**硬门禁(正向,exit 1)**:core/sdk/server.py 出现的每个 `ZE_*` 字面量,必须被 schema 覆盖
(exact 精确键 / prefix 前缀族)。等价 css 的"className 必须已定义"。

**软报告(不 exit)**:schema 声明了但代码里没见到的精确键(孤儿)。同 css 检查器只做单向硬门禁
—— 不因"未用的声明"失败(拼接键如 ZE_CAPACITY_ENTITY_COUNT_MAX 永不以整串字面量出现,属正常)。

**硬门禁(反向 R1,secret 贯通,exit 1)**:schema 每个 `secret=True` 的键,seed 后 ConfigItem.secret
必须仍为 True、snapshot 必脱敏 `***`(声明→seed→面板三段不许断线)。教训:registry seed 曾漏传
`secret=` → 主密钥/飞书 Secret 明文回显任何登录用户(b2e55b4bc)。纯 ops 内存自校验,零假阳性。
全量反向(每个声明键都要消费者)刻意不做——动态拼接键静态不可判,硬化必假红。

**已知盲区**(维护者勿误信):只扫 `["']ZE_…` 引号起头的字面量;`PORT`/`VITE_API_BASE`(非 ZE_
前缀、在 .sh/.ts 内)不扫,已在 config_schema.LAUNCH_KNOBS 登记。整行 `#` 注释跳过;
docstring 内的 ZE_ 仍纳入(契约相关)。

用法:
  python scripts/ops_contract_check.py          # 扫描 + 报告;有未覆盖 exit 1(可做部署门禁)
  python scripts/ops_contract_check.py --json    # 机器可读
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

BUILD_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = BUILD_ROOT / "04-ops" / "01-config-management"
sys.path.insert(0, str(CONFIG_DIR))
import config_schema as cs  # noqa: E402

# 扫描范围:core 框架 + sdk 装配 + 顶层 server.py(均 LAYER core 的 .py)
SCAN_DIRS = [BUILD_ROOT / "01-core", BUILD_ROOT / "sdk"]
SCAN_FILES = [BUILD_ROOT / "server.py"]

# 完整引号包围的 ZE_ 环境变量字面量。结尾引号是必要边界：
# ``"ZE_SURFACE.json"`` 是配置文件名，不能被截断误报成 ``ZE_SURFACE``。
_ZE_LITERAL_RE = re.compile(
    r"""(?P<quote>["'])(?P<name>ZE_[A-Z0-9_]+)(?P=quote)""",
)


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(BUILD_ROOT))
    except ValueError:
        return p.name


def _iter_py() -> list:
    out = []
    for root in SCAN_DIRS:
        if root.is_dir():
            out.extend(
                p for p in root.rglob("*.py")
                if "__pycache__" not in p.parts and not p.name.startswith("test_")
            )
    out.extend(f for f in SCAN_FILES if f.is_file())
    return sorted(out)


def _dunder_all_lines(text: str) -> set:
    """Return module-level ``__all__`` assignment lines; exports are not env reads."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    lines: set = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "__all__"
                   for target in node.targets):
            continue
        end = getattr(node, "end_lineno", node.lineno) or node.lineno
        lines.update(range(node.lineno, end + 1))
    return lines


def check_file(path: Path) -> list:
    """扫一个 .py,返回未被 schema 覆盖的 ZE_ 字面量违规。"""
    violations = []
    text = path.read_text(encoding="utf-8")
    exported_lines = _dunder_all_lines(text)
    for i, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue  # 整行注释
        if i in exported_lines:
            continue  # `__all__` 的元素是符号引用,不是 env 键
        for match in _ZE_LITERAL_RE.finditer(line):
            literal = match.group("name")
            if not cs.is_covered(literal):
                violations.append({
                    "file": _rel(path), "line": i, "env": literal,
                    "hint": "core/sdk 读了 config_schema 未声明的 ZE_ env(请在 04-ops/01-config-management/config_schema.py 声明)",
                })
    return violations


def check_secret_masking() -> dict:
    """D4-R1 secret 贯通(反向定向断言,零假阳性):schema secret=True 的键,seed 后
    ConfigItem.secret 必须仍 True、snapshot 必脱敏 ***。内存 registry,零磁盘写。"""
    import registry as reg_mod  # CONFIG_DIR 已入 sys.path

    problems: list = []
    reg = reg_mod.ConfigRegistry()
    reg_mod.seed_framework_config(reg)
    schema_secrets = [k for k in cs.FRAMEWORK_KNOBS if getattr(k, "secret", False)]
    snap = {e["key"]: e for e in reg.snapshot()}
    for spec in schema_secrets:
        entry = snap.get(spec.key)
        if entry is None:
            problems.append({"key": spec.key, "why": "schema secret 键 seed 后不在 registry(声明→seed 断线)"})
        elif not entry.get("secret"):
            problems.append({"key": spec.key, "why": "seed 丢了 secret=True(b2e55b4bc 同型泄露:面板将明文回显)"})
    if schema_secrets:
        protected = schema_secrets[0].key
        try:
            reg.declare(protected, "text", default="downgraded")
        except ValueError:
            pass
        else:
            problems.append({
                "key": protected,
                "why": "重复 declare 可降级 secret/readonly 合同，应只允许 override_default",
            })
    if schema_secrets and not problems:
        # 机制证明:直接注内存哑值(绕 set_values 的 readonly 守卫——测的是脱敏语义非可写性;
        # _values 纯内存,零磁盘写),snapshot 必脱敏、reveal 必可读回。
        probe = schema_secrets[0].key
        reg._values[probe] = "dummy-secret-probe"  # noqa: SLF001 门禁自校验,不落盘
        masked = {e["key"]: e for e in reg.snapshot()}.get(probe, {})
        if masked.get("value") != "***":
            problems.append({"key": probe, "why": f"snapshot 未脱敏(value={masked.get('value')!r})"})
        revealed = {e["key"]: e for e in reg.snapshot(reveal_secret=True)}.get(probe, {})
        if revealed.get("value") != "dummy-secret-probe":
            problems.append({"key": probe, "why": "reveal_secret 读不回明文(注入未生效,检查器自身失真)"})
    return {"schema_secret_count": len(schema_secrets), "problems": problems}


def check_config_atomic_wiring(path: Path | None = None) -> dict:
    """配置写入口必须直接落到 ConfigRegistry.update_values。

    这是一条有意很窄的静态门禁：历史实现先 ``set_values`` 改内存、再
    ``save_values`` 落盘，第二步失败时内存与磁盘分叉。仅靠 registry 自身的原子
    测试抓不到门面重新拼回两步调用，因此这里检查真正被 HTTP/MCP 消费的
    ``Repository.save_config``。AST 只认调用名，不依赖缩进、换行或注释。
    """
    target = path or (BUILD_ROOT / "sdk" / "bootstrap.py")
    problems: list[dict] = []
    try:
        source = target.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(target))
    except (OSError, SyntaxError) as exc:
        return {
            "file": _rel(target),
            "calls": [],
            "problems": [{"why": f"无法解析配置门面:{type(exc).__name__}: {exc}"}],
        }

    save_config: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "Repository":
            continue
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == "save_config":
                save_config = child
                break
    if save_config is None:
        problems.append({"why": "缺少 Repository.save_config，配置写入口契约失联"})
        calls: list[str] = []
    else:
        calls = sorted({
            call.func.attr
            for call in ast.walk(save_config)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
        })
        if "update_values" not in calls:
            problems.append({"why": "Repository.save_config 未调用原子 update_values"})
        legacy = sorted({"set_values", "save_values"}.intersection(calls))
        if legacy:
            problems.append({
                "why": "Repository.save_config 重新引入内存/磁盘两步写",
                "calls": legacy,
            })
        kwonly = {arg.arg: default for arg, default in zip(
            save_config.args.kwonlyargs,
            save_config.args.kw_defaults,
        )}
        if "expected_version" not in kwonly or kwonly["expected_version"] is not None:
            problems.append({"why": "Repository.save_config 的 expected_version 必须是无默认值的必填关键字参数"})
    return {"file": _rel(target), "calls": calls, "problems": problems}


def check_config_surface_wiring(path: Path | None = None) -> dict:
    """Global config REST must require admin and pass mandatory CAS to the repository."""
    target = path or (BUILD_ROOT / "server.py")
    problems: list[dict] = []
    try:
        tree = ast.parse(target.read_text(encoding="utf-8"), filename=str(target))
    except (OSError, SyntaxError) as exc:
        return {
            "file": _rel(target), "calls": [],
            "problems": [{"why": f"无法解析配置 REST:{type(exc).__name__}: {exc}"}],
        }
    handler = next((
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "put_config"
    ), None)
    if handler is None:
        return {
            "file": _rel(target), "calls": [],
            "problems": [{"why": "缺少 put_config REST 入口"}],
        }
    calls: list[str] = []
    save_has_cas = False
    for call in (node for node in ast.walk(handler) if isinstance(node, ast.Call)):
        if isinstance(call.func, ast.Name):
            calls.append(call.func.id)
        elif isinstance(call.func, ast.Attribute):
            calls.append(call.func.attr)
        if isinstance(call.func, ast.Attribute) and call.func.attr == "save_config":
            save_has_cas = any(keyword.arg == "expected_version" for keyword in call.keywords)
    calls = sorted(set(calls))
    if "_require_super_admin" not in calls:
        problems.append({"why": "全局配置 PUT 未经 super-admin 门禁"})
    if not save_has_cas:
        problems.append({"why": "全局配置 PUT 未把 expected_version 传给 save_config"})
    return {"file": _rel(target), "calls": calls, "problems": problems}


def check_config_client_wiring(path: Path | None = None) -> dict:
    """The public TypeScript client must make CAS and idempotency impossible to omit."""
    target = path or (
        BUILD_ROOT / "01-core" / "04-user-facing" / "03-authentication" / "authApi.ts"
    )
    try:
        source = target.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "file": _rel(target),
            "problems": [{"why": f"无法读取配置客户端:{type(exc).__name__}: {exc}"}],
        }
    match = re.search(
        r"export\s+async\s+function\s+apiPutConfig\s*\((.*?)\)\s*:\s*Promise.*?\{(.*?)\n\}",
        source,
        flags=re.DOTALL,
    )
    if match is None:
        return {"file": _rel(target), "problems": [{"why": "缺少 apiPutConfig TypeScript 客户端"}]}
    signature, body = match.groups()
    problems: list[dict] = []
    if re.search(r"\bidempotencyKey\s*\?\s*:", signature):
        problems.append({"why": "apiPutConfig 的 idempotencyKey 仍可省略"})
    if re.search(r"\bexpectedVersion\s*\?\s*:", signature):
        problems.append({"why": "apiPutConfig 的 expectedVersion 仍可省略"})
    if not re.search(r"\bexpectedVersion\s*:\s*number\b", signature):
        problems.append({"why": "apiPutConfig 未声明必填 number expectedVersion"})
    if "expected_version: expectedVersion" not in body:
        problems.append({"why": "apiPutConfig 请求体未无条件携带 expected_version"})
    if "expectedVersion !== undefined" in body or "expectedVersion != null" in body:
        problems.append({"why": "apiPutConfig 仍按条件发送 expected_version"})
    return {"file": _rel(target), "problems": problems}


def run() -> dict:
    files = _iter_py()
    seen: set = set()
    violations = []
    for f in files:
        for v in check_file(f):
            violations.append(v)
        for match in _ZE_LITERAL_RE.finditer(f.read_text(encoding="utf-8")):
            seen.add(match.group("name"))
    # 软报告:声明了但没在代码字面量见到的精确键(拼接键正常,故仅 info)
    orphans = sorted(
        v for v in cs.exact_env_vars()
        if v not in seen and not any(v.startswith(p) for p in cs.env_prefixes())
    )
    secret = check_secret_masking()
    config_atomic = check_config_atomic_wiring()
    config_surface = check_config_surface_wiring()
    config_client = check_config_client_wiring()
    return {
        "ok": (
            len(violations) == 0
            and not secret["problems"]
            and not config_atomic["problems"]
            and not config_surface["problems"]
            and not config_client["problems"]
        ),
        "scanned_files": len(files),
        "declared_exact": len(cs.exact_env_vars()),
        "declared_prefixes": list(cs.env_prefixes()),
        "violation_count": len(violations),
        "violations": violations,
        "orphans_info": orphans,
        "secret_masking": secret,
        "config_atomic_wiring": config_atomic,
        "config_surface_wiring": config_surface,
        "config_client_wiring": config_client,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="core/sdk env 读取 与 OPS 配置词表 契约检查")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    res = run()
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"扫描 {res['scanned_files']} 个 .py;声明 {res['declared_exact']} 精确键 + "
              f"前缀族 {res['declared_prefixes']}")
        if res["orphans_info"]:
            print(f"  (info) schema 声明但未见字面量(拼接键可正常): {res['orphans_info']}")
        if res["ok"]:
            print(f"✓ core/sdk 的 ZE_ env 读取都在 OPS 词表声明,契约一致;"
                  f"{res['secret_masking']['schema_secret_count']} 个 secret 键 seed→snapshot 贯通脱敏(R1);"
                  "配置写入已经原子 update_values 门面(R2);"
                  "全局 PUT 与公开客户端已强制 super-admin + CAS + 幂等(R3)")
        else:
            print(f"✗ {res['violation_count']} 处未声明 env 读取")
            for v in res["violations"]:
                print(f"  [{v['env']}] {v['file']}:{v['line']}  — {v['hint']}")
            for p in res["secret_masking"]["problems"]:
                print(f"  [SECRET_MASKING] {p['key']}  — {p['why']}")
            for p in res["config_atomic_wiring"]["problems"]:
                print(f"  [CONFIG_ATOMIC] {res['config_atomic_wiring']['file']}  — {p['why']}")
            for p in res["config_surface_wiring"]["problems"]:
                print(f"  [CONFIG_SURFACE] {res['config_surface_wiring']['file']}  — {p['why']}")
            for p in res["config_client_wiring"]["problems"]:
                print(f"  [CONFIG_CLIENT] {res['config_client_wiring']['file']}  — {p['why']}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
