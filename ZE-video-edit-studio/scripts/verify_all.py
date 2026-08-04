#!/usr/bin/env python3
"""verify_all — 一把跑 ZE Skill Build 全部门禁,全绿则产出 ZE_BUILD_EVIDENCE.json(部署 preflight 要它)。

门禁(任一红 → exit 1,不写 evidence;顺序同下方 _run 调用):
   0a. deployment-profile 可移植部署配方、provider/交付兼容性与明显泄漏门禁
   0b. artifact-variant   Base Artifact 构建键、SBOM、Series Overlay 与跨作用域隔离门禁
   1. pytest-fast       全量后端测,排除 07-persistence(数据流持久层单独成门)
   1a. pytest-slow     与 fast 同目标的 @slow 墙钟测试；目标无 slow 用例时合法空过
   1b. pytest-persistence 07-persistence 持久层测(~7min/2000 测,单独一门,便于定向跳过)
   1c. stdlib-shadow   注入目录模块名禁与 stdlib 撞名 + import sdk 金丝雀(防 queue.py 遮蔽再犯;上游-only)
   2. css-contract     前端-CSS 词表门禁
   2b. mod-renderer    下游 02-mod/05-renderers.ts(x) 层级/功能区消费门禁
   3. css-layer-guard  层级单一真相源门禁(原子单一定义 / 区域只用令牌 / CSS 只住皮肤 / accent 参数化)
   3b. css-module-structure CSS 对象文件夹 + 状态 token/UI 状态对象门禁
   4. css-container    容器适应契约门禁(flex 链 min-0 滚动归属 / ellipsis 截断;SPEC §2.10)
   4a. css-scroll-affordance 软滚动 fade + 纤细滚动条视觉门禁(内容流不生切;硬 viewport 豁免)
   4a1. css-typography      字号/字重/字距合同门禁(font-size/weight/tokenized;letter-spacing=0)
   4b. editor-frame    编辑器外壳单一真相源(*Editor.tsx 必经 EditorFrame 组装,无裸 .ze-editor 外壳)
   4b1. design-layer-contract L4/L5 React 禁越级 primitive import / L2 atom class(基线棘轮)
   4c. responsive      L5 工作台响应式契约(viewport meta / 窄栏 @container 堆叠 / split pane 无 inline flex;SPEC §2.11)
   4d. usage-coverage  叶子 README 须含「## 怎么用」消费视角段(上游-only,下游软跳过)
   4e. editor-manifest 编辑器 manifest 自洽(注册表/功能区 selector/fixture;上游-only,下游软跳过)
   4f. type-registry   类型库词表双向同步(GATE-TYPE-07)+ mod archetype 防漂移(GATE-TYPE-09;受管,下游真把关)
   5. doc-status       完成状态钉在机器源(adopted 编辑器不得被散文说成未做)
   6. ai-interface     introspect readers 双面暴露(server + CLI)一致门禁
   7. command-async    慢命令必须 stage:native execute 不得直调 llm_call/image_call(防假装瞬时)
   7a. ai-effect-write ai-callable 命令 producer 禁 artifact 协议外的 open(写)/write_*/subprocess(安全)
   7b. usability       可用性烟雾(空输入护栏;受管,mod 未声明 usability_cases 时软跳过)
   7c. eval-suite      离线 command benchmark/eval(声明 eval_cases 后 dispatch + 断言)
   8. ops-contract     core/sdk env↔配置 schema 门禁
   9. i18n-contract    UI 代码默认零裸中文门禁
  10. i18n-audit       key 一致性门禁(missing-key / parity / registry;需 node+esbuild)
  11. i18n-english     chrome 区零裸英文门禁(扫 header/footer/auth,反向守硬编码英文)
  12. typecheck        webapp `tsc -b`
  12b. web-build       webapp `npm run build`(vite/rollup 真出 dist;治 tsc 绿 build 炸)
  13. ts-unit          webapp vitest 纯逻辑单测(layoutOps / selectionModel / 02-interaction)
  14. managed-drift   机器归属契约 + 受管层 drift 预检(防产品目录被误覆盖)
  15. lock-consistency 全层 lock == 当前 source_contract(防忘 --init-locks)

跑法(用带 pytest 的解释器,如项目 .venv 或 /tmp/pwenv):
  python3 scripts/verify_all.py            # 全绿 exit 0 + 写 ZE_BUILD_EVIDENCE.json(部署 preflight 真源)
  python3 scripts/verify_all.py --no-evidence

定向校验(W4 精准校验 — 改一处只跑相关门禁,秒级出结果;定向模式不写 evidence):
  python3 scripts/verify_all.py --changed              # 据 git 改动自动选门禁 + 收窄 pytest 目标
  python3 scripts/verify_all.py --only pytest-fast     # 只跑指定门禁(逗号分隔)
  python3 scripts/verify_all.py --skip pytest-persistence  # 跑全部但排除指定门禁
部署 preflight 仍要求一次无参全量绿产出 evidence;定向只为开发迭代提速,不替代它。

可移植:用 `sys.executable` 跑 python 门禁(假设该解释器有 pytest);typecheck 走 `npm`。
"""
from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import skill_sync
from artifact_variant import evaluate_artifact_variant
from deployment_profile import evaluate_deployment_profile
from dependency_manifest import MANIFEST_NAME as DEPENDENCY_MANIFEST_NAME, build_manifest
from release_builder import evaluate_build_declaration
from surface_profile import has_surface, load_surface_profile, validate_surface_profile


def _declared_skill_id(root) -> str:
    """身份解析:deploy.json.skill_id 显式声明优先(W11-5:owner 子目录里的 surface,
    如 ZE-skill-system/web,目录名不再等于项目身份),否则回退目录名。加性可选字段。"""
    try:
        declared = json.loads((root / "deploy.json").read_text(encoding="utf-8")).get("skill_id")
        if isinstance(declared, str) and declared.strip():
            return declared.strip()
    except (OSError, ValueError):
        pass
    return root.name

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

# --- W4 定向校验:门禁选择 -----------------------------------------------------
# 无 flag = 全量(写 evidence,部署 preflight 语义完全不变);--only/--skip/--changed 任一
# 激活即"定向模式"(_TARGETED):只跑选中门禁、且**不写 evidence**(定向结果不是完整证据,
# 部署仍须一次无参全量绿)。目的:改一处代码时只跑相关门禁,把单次校验从数十分钟压到秒级。
#   python3 scripts/verify_all.py --only pytest-fast,type-registry
#   python3 scripts/verify_all.py --skip pytest-persistence
#   python3 scripts/verify_all.py --changed      # 据 git 改动自动选门禁 + 收窄 pytest 目标
_ONLY: set[str] | None = None
_SKIP: set[str] = set()
_TARGETED = False

# pytest 目标:07-persistence 是重头(~7min/2000 测),从 fast 拆出去单独成门。
_DEFAULT_FAST_TARGETS = ("01-core", "sdk", "scripts", "02-mod/05-code")
_PERSISTENCE_DIR = "01-core/02-data-flow/07-persistence"
# --changed 模式下按需运行的贵门禁;其余(秒级扫描门)恒跑,消除映射漏报面。
_EXPENSIVE_GATES = {
    "pytest-fast",
    "pytest-slow",
    "pytest-persistence",
    "typecheck",
    "web-build",
    "ts-unit",
    "i18n-audit",
}


def _selected(name: str) -> bool:
    if _ONLY is not None and name not in _ONLY:
        return False
    return name not in _SKIP


def _filtered(name: str) -> dict:
    return {"name": name, "command": "(定向模式未选中)", "status": "skipped",
            "reason": "filtered by --only/--skip/--changed", "filtered": True}


# pytest 需要叶子 sys.path 注入(平铺布局);隔离数据目录避免污染。
def _pytest_code(
    targets,
    ignore_dirs=(),
    marker: str | None = None,
    *,
    allow_no_tests: bool = False,
) -> str:
    args = [*targets, "-q", "-p", "no:cacheprovider", *[f"--ignore={d}" for d in ignore_dirs]]
    if marker:
        args += ["-m", marker]
    return (
        "import sys,glob,os; sys.path.insert(0,'.'); import sdk; "
        "[sys.path.insert(0,os.path.abspath(d)) for d in glob.glob('01-core/**/',recursive=True) "
        "if any(f.endswith('.py') and not f.startswith('test_') for f in os.listdir(d)) "
        "and os.path.abspath(d) not in sys.path]; "
        f"import pytest; _code=pytest.main({args!r}); "
        f"sys.exit(0 if {allow_no_tests!r} and "
        "_code == pytest.ExitCode.NO_TESTS_COLLECTED else _code)"
    )


def _flag_value(args: list[str], flag: str) -> str | None:
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def _split_names(v: str) -> set[str]:
    return {s for s in v.replace(",", " ").split() if s}


def _changed_paths() -> list[str]:
    """build-v3 目录内相对路径的改动清单(tracked diff vs HEAD + untracked),供 --changed 映射。"""
    prefix = ROOT.name + "/"
    try:
        r = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(ROOT), "status", "--porcelain", "--", "."],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:  # noqa: BLE001 — 无 git/超时 → 退回全量
        return []
    paths: list[str] = []
    for line in r.stdout.splitlines():
        p = line[3:]
        if " -> " in p:  # rename
            p = p.split(" -> ", 1)[1]
        p = p.strip().strip('"')
        if p.startswith(prefix):
            p = p[len(prefix):]
        if p:
            paths.append(p)
    return paths


def _changed_selection() -> tuple[set[str], list[str]]:
    """据改动路径决定跑哪些贵门禁 + 把 pytest-fast 收窄到改动叶子。返回 (要 skip 的贵门禁, fast 目标)。"""
    run_expensive: set[str] = set()
    fast_leaves: set[str] = set()
    for p in _changed_paths():
        if p.startswith(_PERSISTENCE_DIR) and p.endswith(".py"):
            run_expensive.add("pytest-persistence")
        elif p.startswith("01-core/") and p.endswith(".py"):
            run_expensive.add("pytest-fast")
            fast_leaves.add(os.path.dirname(p))
        elif p.startswith("sdk/") and p.endswith(".py"):
            run_expensive.add("pytest-fast"); fast_leaves.add("sdk")
        elif p.startswith("scripts/") and p.endswith(".py"):
            run_expensive.add("pytest-fast"); fast_leaves.add("scripts")
        elif p.startswith("02-mod/05-code/") and p.endswith(".py"):
            run_expensive.add("pytest-fast"); fast_leaves.add("02-mod/05-code")
        if p.endswith((".tsx", ".ts")) or p.startswith("03-css-rules/") or p.startswith("webapp/"):
            run_expensive.update({"typecheck", "web-build", "ts-unit", "i18n-audit"})
        if p.startswith("05-i18n/") or p.endswith("09-i18n.ts"):
            run_expensive.add("i18n-audit")
    skip = _EXPENSIVE_GATES - run_expensive
    # slow 与 fast 使用同一批定向叶子。某些叶子没有 @slow 用例是合法空集，
    # _pytest_code 会把 slow 门独有的 NO_TESTS_COLLECTED 规范成通过；其它 pytest
    # 门仍把空集当配置错误，避免错误路径/拼写被静默放行。
    if "pytest-fast" in run_expensive:
        skip.discard("pytest-slow")
    fast_targets = sorted(fast_leaves) if fast_leaves else list(_DEFAULT_FAST_TARGETS)
    return skip, fast_targets


def _run(name: str, cmd, cwd: Path = ROOT, env: dict | None = None) -> dict:
    if not _selected(name):
        return _filtered(name)
    shown = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
    print(f"\n▶ {name}: {shown}")
    e = {**os.environ, **(env or {})}
    r = subprocess.run(cmd, cwd=str(cwd), env=e, shell=isinstance(cmd, str))
    ok = r.returncode == 0
    print(f"  {'✓' if ok else '✗'} {name} (exit {r.returncode})")
    return {"name": name, "command": shown, "status": "passed" if ok else "failed"}


def _skip(name: str, command: str, why: str) -> dict:
    print(f"\n▶ {name}: (跳过 — {why})")
    # reason 持久化进 evidence:部署 preflight(ZE-skill-deploy inventory)要求 skipped gate 带原因,
    # 否则判 "gate skipped without reason" 拦下部署。why 是已算好的真原因,不是事后补的托词。
    return {"name": name, "command": command, "status": "skipped", "reason": why}


def _write_commands_registry(root: Path) -> None:
    """生成 commands/registry.json —— 部署 preflight 要它(deploy inventory 读其 schema/commands)。

    v3 命令真源在 02-mod/05-code/commands_register.py;本函数按真实注册的命令自省出一份机器清单,
    供部署/系统 Web 读取。**仅当缺失时生成**,不覆盖手维护的(如 tutorial-writer);任何 introspect
    失败都非致命(只 warn,不红 verify_all)。"""
    target = root / "commands" / "registry.json"
    if target.exists():
        return
    try:
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        # mod 命令实现目录:commands_register.py 的同级 import(如 import codex_advise)需它在 path
        # —— ze_command 正常 CLI 流由 bootstrap 注入,这里直调 _static_cmd_reg 须自行补。
        mod_code = root / "02-mod" / "05-code"
        if mod_code.is_dir() and str(mod_code) not in sys.path:
            sys.path.insert(0, str(mod_code))
        import ze_command  # scripts/ 已在 path;ze_command 有 __main__ 守卫,import 无副作用
        reg = ze_command._static_cmd_reg()
        commands = ze_command.list_commands(reg)
    except Exception as exc:  # noqa: BLE001 — mod 不可加载/introspect 失败 → 跳过,不拖累 verify
        print(f"  ⚠ commands/registry.json 未生成(introspect 不可用): {exc}")
        return
    manifest = {
        "schema": "ze-command-registry.v1",
        "skill_id": _declared_skill_id(root),
        "title": root.name,
        "generated_by": "ZE-skill-build/scripts/verify_all.py",
        "commands": commands,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("  → 写 commands/registry.json(部署 preflight 要)")


def _run_if_present(name: str, script_rel: str, why: str) -> dict:
    """皮肤/元门禁脚本(css_layer_guard / css_container / responsive / editor_frame / design_layer_contract / rule_registry)是
    build-v3 上游本地物、**不进受管 toolchain 清单** → 同步过的下游 skill 不带它们(它们校验「皮肤如何被
    创作」,创作只在上游;下游是皮肤只读消费者,见 skill_sync._TOOLCHAIN_FILES 注 + DESIGN-SYSTEM §0.2)。
    脚本在(=在 build-v3)则跑,不在(=下游)则软跳过、不红下游 verify_all。"""
    if not _selected(name):
        return _filtered(name)
    if (ROOT / script_rel).is_file():
        return _run(name, [PY, script_rel])
    return _skip(name, f"python3 {script_rel}", why)


def _surface_gate() -> dict:
    if not _selected("surface-profile"):
        return _filtered("surface-profile")
    profile = load_surface_profile(ROOT)
    errors = validate_surface_profile(ROOT, profile)
    status = "passed" if not errors else "failed"
    print(f"\n▶ surface-profile: ZE_SURFACE.json / inferred profile")
    if errors:
        for err in errors:
            print(f"  ✗ {err}")
    else:
        print(f"  ✓ primary={profile.get('primary')} surfaces={','.join(profile.get('surfaces') or [])}")
    return {
        "name": "surface-profile",
        "command": "python3 -c 'from surface_profile import validate_surface_profile'",
        "status": status,
        "surface_profile": {
            "primary": profile.get("primary"),
            "surfaces": profile.get("surfaces") or [],
            "human_control": profile.get("human_control"),
            "inferred": bool(profile.get("_inferred")),
        },
        **({"errors": errors} if errors else {}),
    }


def _deployment_profile_gate() -> dict:
    if not _selected("deployment-profile"):
        return _filtered("deployment-profile")
    print("\n▶ deployment-profile: portable recipe + delivery boundaries")
    result = evaluate_deployment_profile(ROOT)
    errors = [str(item) for item in result.get("errors") or []]
    warnings = [str(item) for item in result.get("warnings") or []]
    for warning in warnings:
        print(f"  ⚠ {warning}")
    if errors:
        for error in errors:
            print(f"  ✗ {error}")
    else:
        print(f"  ✓ schema={result.get('schema')} legacy={bool(result.get('legacy'))}")
    return {
        "name": "deployment-profile",
        "command": "python3 scripts/deployment_profile.py .",
        "status": "passed" if result.get("valid") else "failed",
        "deployment_profile": result.get("deployment_profile") or {},
        **({"warnings": warnings} if warnings else {}),
        **({"errors": errors} if errors else {}),
    }


def _dependency_manifest_gate() -> dict:
    if not _selected("dependency-manifest"):
        return _filtered("dependency-manifest")
    print("\n▶ dependency-manifest: environment-neutral descriptors + locks")
    path = ROOT / DEPENDENCY_MANIFEST_NAME
    errors: list[str] = []
    manifest: dict = {}
    if path.is_symlink() or not path.is_file():
        errors.append(f"{DEPENDENCY_MANIFEST_NAME} is missing or unsafe")
    else:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("manifest must be a JSON object")
            manifest = value
            expected = build_manifest(ROOT, skill_id=ROOT.name)
            for field in ("schema", "skill_id", "status", "generated_by", "environments"):
                if manifest.get(field) != expected.get(field):
                    errors.append(f"{field} is stale or disagrees with dependency descriptors/locks")
            unknown = sorted(set(manifest) - set(expected) - {"skill_uid"})
            if unknown:
                errors.append(f"unknown dependency manifest fields: {unknown}")
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            errors.append(str(exc))
    if errors:
        for error in errors:
            print(f"  ✗ {error}")
    else:
        print(
            f"  ✓ status={manifest.get('status')} "
            f"environments={len(manifest.get('environments') or [])}"
        )
    canonical = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8") if manifest else b""
    raw = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
    summary = {
        "schema": manifest.get("schema"),
        "status": manifest.get("status"),
        "manifest_digest": "sha256:" + hashlib.sha256(canonical).hexdigest() if canonical else None,
        "file_digest": "sha256:" + hashlib.sha256(raw).hexdigest() if raw else None,
        "environment_count": len(manifest.get("environments") or []),
        "environments": [
            {
                "environment_id": row.get("environment_id"),
                "ecosystem": row.get("ecosystem"),
                "purposes": row.get("purposes"),
                "artifact_roles": row.get("artifact_roles", []),
                "supported_strategies": (row.get("materialization") or {}).get("supported_strategies", []),
            }
            for row in manifest.get("environments") or []
            if isinstance(row, dict)
        ],
    }
    return {
        "name": "dependency-manifest",
        "command": "python3 scripts/dependency_manifest.py .",
        "status": "failed" if errors else "passed",
        "dependency_manifest": summary,
        **({"errors": errors} if errors else {}),
    }


def _artifact_variant_gate() -> dict:
    if not _selected("artifact-variant"):
        return _filtered("artifact-variant")
    print("\n▶ artifact-variant: reproducible Base Artifact + scoped overlays")
    result = evaluate_artifact_variant(ROOT)
    errors = [str(item) for item in result.get("errors") or []]
    warnings = [str(item) for item in result.get("warnings") or []]
    for warning in warnings:
        print(f"  ⚠ {warning}")
    if errors:
        for error in errors:
            print(f"  ✗ {error}")
    else:
        status = (result.get("artifact_variant") or {}).get("status")
        print(f"  ✓ artifact variant status={status}")
    return {
        "name": "artifact-variant",
        "command": "python3 scripts/artifact_variant.py .",
        "status": "passed" if result.get("valid") else "failed",
        "artifact_variant": result.get("artifact_variant") or {},
        **({"warnings": warnings} if warnings else {}),
        **({"errors": errors} if errors else {}),
    }


def _artifact_build_gate() -> dict:
    if not _selected("artifact-build"):
        return _filtered("artifact-build")
    print("\n▶ artifact-build: target-neutral variant production declaration")
    result = evaluate_build_declaration(ROOT)
    errors = [str(item) for item in result.get("errors") or []]
    warnings = [str(item) for item in result.get("warnings") or []]
    for warning in warnings:
        print(f"  ⚠ {warning}")
    for error in errors:
        print(f"  ✗ {error}")
    if not errors:
        status = (result.get("artifact_build") or {}).get("status")
        print(f"  ✓ artifact build declaration status={status}")
    return {
        "name": "artifact-build",
        "command": "python3 scripts/release_builder.py . --check",
        "status": "passed" if result.get("valid") else "failed",
        "artifact_build": result.get("artifact_build") or {},
        **({"warnings": warnings} if warnings else {}),
        **({"errors": errors} if errors else {}),
    }


def _mcp_gate() -> dict:
    """mcp surface 门禁:bridge 可导入 + 暴露工具 + server.py 已挂载 /mcp。"""
    if not _selected("mcp-surface"):
        return _filtered("mcp-surface")
    print("\n▶ mcp-surface: bridge import + server mount")
    errors: list[str] = []
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        import importlib
        mb = importlib.import_module("sdk.mcp_bridge")
        if not mb.mcp_surface_enabled(ROOT):
            errors.append("ZE_SURFACE.json surfaces 含 mcp,但 mcp_surface_enabled 判否")
        if len(mb._meta_tool_defs()) < 1:
            errors.append("MCP 未暴露任何工具")
    except Exception as e:  # noqa: BLE001
        errors.append(f"sdk.mcp_bridge 导入失败: {type(e).__name__}: {e}")
    server_src = (ROOT / "server.py").read_text(encoding="utf-8") if (ROOT / "server.py").exists() else ""
    if "register_mcp_routes" not in server_src:
        errors.append("server.py 未挂载 /mcp(缺 register_mcp_routes 调用)")
    status = "passed" if not errors else "failed"
    if errors:
        for err in errors:
            print(f"  ✗ {err}")
    else:
        print("  ✓ /mcp 已挂载,bridge 暴露元工具")
    return {
        "name": "mcp-surface",
        "command": "python3 -c 'import sdk.mcp_bridge'",
        "status": status,
        **({"errors": errors} if errors else {}),
    }


def _npm_has_script(script: str) -> bool:
    """webapp/package.json 是否声明了该 npm script(用于软跳过未采用的可选门禁)。"""
    pj = ROOT / "webapp" / "package.json"
    try:
        return script in json.loads(pj.read_text(encoding="utf-8")).get("scripts", {})
    except (OSError, ValueError):
        return False


def _source_contract_summary() -> dict:
    summary = {}
    for layer in skill_sync.LAYERS:
        version, checksum = skill_sync.source_contract(layer)
        summary[layer] = {
            "interface_version": version,
            "checksum": checksum,
        }
    return summary


def main() -> int:
    global _ONLY, _SKIP, _TARGETED
    argv = sys.argv[1:]
    only_arg = _flag_value(argv, "--only")
    skip_arg = _flag_value(argv, "--skip")
    changed_mode = "--changed" in argv
    fast_targets = list(_DEFAULT_FAST_TARGETS)
    if only_arg:
        _ONLY = _split_names(only_arg)
    if skip_arg:
        _SKIP |= _split_names(skip_arg)
    if changed_mode:
        changed_skip, fast_targets = _changed_selection()
        _SKIP |= changed_skip
    _TARGETED = bool(_ONLY is not None or _SKIP or changed_mode)
    if _TARGETED:
        note = []
        if _ONLY is not None:
            note.append(f"only={','.join(sorted(_ONLY))}")
        if _SKIP:
            note.append(f"skip={','.join(sorted(_SKIP))}")
        if changed_mode:
            note.append(f"changed→fast_targets={','.join(fast_targets)}")
        print(f"▶ 定向校验模式({'; '.join(note)}) — 不写 evidence,部署仍须一次无参全量绿")

    gates: list[dict] = []
    profile = load_surface_profile(ROOT)
    web_enabled = has_surface(profile, "web")
    mcp_enabled = has_surface(profile, "mcp")
    tmp = tempfile.mkdtemp(prefix="ze_verify_")
    try:
        gates.append(_surface_gate())
        gates.append(_dependency_manifest_gate())
        gates.append(_deployment_profile_gate())
        gates.append(_artifact_build_gate())
        gates.append(_artifact_variant_gate())
        gates.append(_run_if_present(
            "surface-diff", "scripts/surface_diff_check.py",
            "surface_diff_check 是 build-v3 上游创作门禁(符号级公共表面 diff,删/改名=incompat),"
            "不进受管 toolchain;下游消费表面而非定义表面,缺脚本即软跳过"))
        gates.append(_mcp_gate() if mcp_enabled else _skip("mcp-surface", "mcp-surface", "Surface 不含 mcp"))
        # pytest 拆两门:fast(排除 07-persistence)+ persistence(单独);--changed 时 fast 收窄到改动叶子。
        # ZE_SYSTEM_AUTH_DIR 必设(C2·大整合反哺):auth.py default_system_auth_dir() 未设时落到
        # 真实共享 hub 目录(~/.claude/skills/ZE-skill-system/ZE-skill-hub-data/auth/),调 bootstrap()
        # 的测试一旦触发登录/会话/审计写入即污染真实数据;设本变量即级联隔离 sessions/policies/
        # audit/credential/notify(全部默认派生自它)。
        pytest_env = {"ZE_DATA_DIR": tmp, "ZE_USERS_PATH": os.path.join(tmp, "u.json"),
                      "ZE_SYSTEM_AUTH_DIR": os.path.join(tmp, "auth")}
        # C3(大整合反哺):墙钟型慢测试(sleep/TTL/线程压测)标 @pytest.mark.slow,
        # fast 门排除(-m "not slow"),slow 门单跑(-m slow)——两门相补,零测试静默丢失;
        # 全量跑(evidence)两门都必须绿。
        gates.append(_run("pytest-fast", [PY, "-c", _pytest_code(fast_targets, (_PERSISTENCE_DIR,), marker="not slow")],
                          env=pytest_env))
        gates.append(_run("pytest-slow", [PY, "-c", _pytest_code(
            fast_targets,
            (_PERSISTENCE_DIR,),
            marker="slow",
            allow_no_tests=True,
        )],
                          env=pytest_env))
        gates.append(_run("pytest-persistence", [PY, "-c", _pytest_code((_PERSISTENCE_DIR,))],
                          env=pytest_env))
        # 注入目录模块名禁与 stdlib 撞名(防再现 queue.py 遮蔽 stdlib;上游创作门禁,下游软跳过)。
        gates.append(_run_if_present("stdlib-shadow", "scripts/stdlib_shadow_check.py",
                                     "上游注入机制门禁脚本缺失(非受管 toolchain;同步下游不带)"))
        if web_enabled:
            gates.append(_run("css-contract", [PY, "scripts/css_contract_check.py"]))
            gates.append(_run("mod-renderer", [PY, "scripts/mod_renderer_check.py"]))
            gates.append(_run_if_present("css-layer-guard", "scripts/css_layer_guard.py",
                                         "皮肤门禁脚本缺失(非受管 toolchain;同步下游不带)"))
            gates.append(_run_if_present("css-module-structure", "scripts/css_module_structure_check.py",
                                         "皮肤结构门禁脚本缺失(非受管 toolchain;同步下游不带)"))
            gates.append(_run_if_present("css-container", "scripts/css_container_contract_check.py",
                                         "皮肤门禁脚本缺失(非受管 toolchain;同步下游不带)"))
            gates.append(_run_if_present("css-scroll-affordance", "scripts/css_scroll_affordance_check.py",
                                         "皮肤门禁脚本缺失(非受管 toolchain;同步下游不带)"))
            gates.append(_run_if_present("css-typography", "scripts/css_typography_contract_check.py",
                                         "皮肤字体门禁脚本缺失(非受管 toolchain;同步下游不带)"))
            gates.append(_run_if_present("editor-frame", "scripts/editor_frame_check.py",
                                         "结构门禁脚本缺失(非受管 toolchain;同步下游不带)"))
            gates.append(_run_if_present("design-layer-contract", "scripts/design_layer_contract_check.py",
                                         "设计层越层门禁脚本缺失(非受管 toolchain;同步下游不带)"))
            gates.append(_run_if_present("responsive", "scripts/responsive_check.py",
                                         "皮肤门禁脚本缺失(非受管 toolchain;同步下游不带)"))
            gates.append(_run_if_present("zone-class", "scripts/zone_class_check.py",
                                         "功能区结构类门禁脚本缺失(非受管 toolchain;同步下游不带)"))
            gates.append(_run_if_present("zone-layout", "scripts/zone_layout_check.py",
                                         "功能区布局词表/场景密度门禁脚本缺失(非受管 toolchain;同步下游不带)"))
            gates.append(_run_if_present("rule-registry", "scripts/rule_registry_check.py",
                                         "元门禁脚本缺失(非受管 toolchain;同步下游不带)"))
            gates.append(_run_if_present("icon-tokens", "scripts/icon_tokens_check.py",
                                         "图标图库门禁脚本缺失(非受管 toolchain;同步下游不带)"))
            gates.append(_run_if_present("usage-coverage", "scripts/usage_coverage_check.py",
                                         "上游文档门禁脚本缺失(非受管 toolchain;同步下游不带)"))
            gates.append(_run_if_present("editor-manifest", "scripts/editor_manifest_check.py",
                                         "上游创作门禁脚本缺失(非受管 toolchain;同步下游不带)"))
        else:
            for name in ("css-contract", "mod-renderer", "css-layer-guard", "css-module-structure", "css-container",
                         "css-scroll-affordance", "editor-frame", "design-layer-contract",
                         "responsive", "zone-class", "rule-registry", "icon-tokens", "usage-coverage", "editor-manifest"):
                gates.append(_skip(name, name, "Surface 不含 web"))
        gates.append(_run("type-registry", [PY, "scripts/type_registry_check.py"]))
        gates.append(_run("doc-status", [PY, "scripts/doc_status_check.py"]))
        gates.append(_run("ai-interface", [PY, "scripts/ai_interface_check.py"]))
        gates.append(_run("command-async", [PY, "scripts/command_async_check.py"]))
        # ai-effect-write:ai-callable(source='ai' 可调)命令 producer 禁 artifact 协议外的
        # open(写)/write_*/subprocess(安全:防 AI 触发任意文件写/命令执行);随 core 进各 skill。
        gates.append(_run("ai-effect-write", [PY, "scripts/ai_effect_write_check.py"]))
        # mod-backend-brand(§5.3 硬门):mod 后端代码(.py)禁硬编码内部 endpoint/品牌
        # (public-ai-surface 管前端,本门管 02-mod backend)。上游门禁脚本,同步下游不带则软跳过。
        gates.append(_run_if_present("mod-backend-brand", "scripts/mod_backend_brand_check.py",
                                     "上游 mod 品牌门禁脚本缺失(非受管 toolchain;同步下游不带)"))
        # mod-fixed-model-enum(§5.3/§6.3 G2):mod 禁硬编码固定 provider/model 目录(须派生自 Build descriptors)
        gates.append(_run_if_present("mod-fixed-model-enum", "scripts/mod_fixed_model_enum_check.py",
                                     "上游 mod 模型目录门禁脚本缺失(非受管 toolchain;同步下游不带)"))
        # mod-raw-secret(§5.3):mod 实体 schema 禁声明 raw secret 字段(凭据须走 System CredentialStore)
        gates.append(_run_if_present("mod-raw-secret", "scripts/mod_raw_secret_check.py",
                                     "上游 mod 密钥字段门禁脚本缺失(非受管 toolchain;同步下游不带)"))
        # ai-requirements(§5.2/§5.3):AI 工具须声明 ZE_AI_REQUIREMENTS.json capability manifest,覆盖用到的 feature_id
        gates.append(_run_if_present("ai-requirements", "scripts/ai_requirements_check.py",
                                     "上游 AI 需求 manifest 门禁脚本缺失(非受管 toolchain;同步下游不带)"))
        # 编辑器工具条禁出 undo/redo 按钮(SPEC §222;上游-only,脚本非受管 toolchain、下游不带 → 软跳过)。
        gates.append(_run_if_present("editor-undo", "scripts/editor_zone_undo_guard.py",
                                     "上游编辑器门禁脚本缺失(非受管 toolchain;同步下游不带)"))
        # 可用性烟雾(受管):mod 未声明 usability_cases 时脚本自身软跳过(ok+skipped),声明后强制全覆盖。
        gates.append(_run("usability", [PY, "scripts/usability_check.py"],
                          env={"ZE_DATA_DIR": tmp, "ZE_SYSTEM_AUTH_DIR": os.path.join(tmp, "auth")}))
        gates.append(_run("eval-suite", [PY, "scripts/eval_suite.py"],
                          env={"ZE_DATA_DIR": tmp, "ZE_SYSTEM_AUTH_DIR": os.path.join(tmp, "auth")}))
        for g in ("ops", "i18n"):
            gates.append(_run(f"{g}-contract", [PY, f"scripts/{g}_contract_check.py"]))
        if web_enabled:
            gates.append(_run("i18n-audit", [PY, "scripts/i18n_audit.py"]))
            gates.append(_run("i18n-english", [PY, "scripts/i18n_english_check.py"]))
            # public-ai-surface:C 端前端显示层禁现内部品牌/内网地址(琥珀/api.ai.hupo.games/内网 IP)。
            # 上游 C 端红线门禁,脚本随 toolchain 同步;未带的下游软跳过。
            gates.append(_run_if_present("public-ai-surface", "scripts/public_ai_surface_check.py",
                                         "上游 C 端表面门禁脚本缺失(非受管 toolchain;同步下游不带)"))
            gates.append(_run("typecheck", "npm run typecheck", cwd=ROOT / "webapp"))
            # web-build:tsc 绿 ≠ vite build 绿(rollup 解析 / 动态 import / 资产引用 / 循环依赖等只在真 build 暴露)。
            # 治「typecheck 过、build 炸」——dist 是部署真正 serve 的产物,必须能产出。webapp 未声明 build 脚本则软跳过。
            gates.append(_run("web-build", "npm run build", cwd=ROOT / "webapp")
                         if _npm_has_script("build")
                         else _skip("web-build", "npm run build", "webapp 未声明 build 脚本"))
        else:
            for name in ("i18n-audit", "i18n-english", "public-ai-surface", "typecheck", "web-build"):
                gates.append(_skip(name, name, "Surface 不含 web"))
        # ts-unit 是可选门禁:随 toolchain 同步到未采用 vitest 的 skill 时软跳过(不红下游)。
        gates.append(_run("ts-unit", "npm run test", cwd=ROOT / "webapp")
                     if web_enabled and _npm_has_script("test")
                     else _skip("ts-unit", "npm run test", "Surface 不含 web 或 webapp 未声明 test 脚本"))
        gates.append(_run("managed-drift", [PY, "scripts/managed_drift_check.py"]))
        gates.append(_run("lock-consistency", [PY, "scripts/skill_sync.py", "--verify-locks"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed = all(g["status"] in ("passed", "skipped") for g in gates)
    print("\n" + "=" * 56)
    print(("✓ 全绿" if passed else "✗ 有失败") + ":  "
          + "  ".join(f"{g['name']}={g['status']}" for g in gates if not g.get("filtered")))

    if passed and not _TARGETED and "--no-evidence" not in sys.argv:
        evidence = {
            "schema": "ze-build-evidence.v1",
            "skill_id": _declared_skill_id(ROOT),
            "built_at": datetime.now(timezone.utc).isoformat(),
            "generator_skill": "ZE-skill-build",
            "passed": True,
            "surface_profile": {
                "primary": profile.get("primary"),
                "surfaces": profile.get("surfaces") or [],
                "human_control": profile.get("human_control"),
                "inferred": bool(profile.get("_inferred")),
            },
            "gate_policy": {
                "source": "ZE-skill-build/scripts/verify_all.py",
                "surface_aware": True,
                "skipped_gate_rule": "skipped gates must be explained by surface/profile or absent upstream-only creator gates",
                "pass_rule": "all gates must be passed or skipped",
            },
            "source_contract": {
                "schema": "ze.source_contract_snapshot.v1",
                "layers": _source_contract_summary(),
            },
            "artifact_contract": {
                "surface_profile": "ZE_SURFACE.json or inferred by scripts/surface_profile.py",
                "commands": "commands/registry.json",
                "deploy": "deploy.json draft only until ZE-skill-deploy Deploy.PrepareTarget reserves a target",
                "variant": "ZE_ARTIFACT_VARIANT.json for scoped-release-ready Base Artifact and overlays",
                "artifact_build": "ZE_ARTIFACT_BUILD.json for environment-neutral Base Artifact production",
                "dependencies": "ZE_DEPENDENCIES.json for environment-neutral dependency and target adapter selection",
                "evidence": "ZE_BUILD_EVIDENCE.json generated by current ZE Skill Build verify_all",
            },
            "deployment_profile": next(
                (
                    gate.get("deployment_profile")
                    for gate in gates
                    if gate.get("name") == "deployment-profile"
                ),
                {},
            ),
            "dependency_manifest": next(
                (
                    gate.get("dependency_manifest")
                    for gate in gates
                    if gate.get("name") == "dependency-manifest"
                ),
                {},
            ),
            "artifact_variant": next(
                (
                    gate.get("artifact_variant")
                    for gate in gates
                    if gate.get("name") == "artifact-variant"
                ),
                {},
            ),
            "artifact_build": next(
                (
                    gate.get("artifact_build")
                    for gate in gates
                    if gate.get("name") == "artifact-build"
                ),
                {},
            ),
            "gates": gates,
        }
        (ROOT / "ZE_BUILD_EVIDENCE.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print("  → 写 ZE_BUILD_EVIDENCE.json(部署 preflight 可过)")
        _write_commands_registry(ROOT)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
