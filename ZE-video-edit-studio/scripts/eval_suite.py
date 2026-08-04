#!/usr/bin/env python3
"""eval_suite.py — 离线 command benchmark / eval 门禁。

目标:让每个 skill 能声明一组**真实业务动作**的可重复评测,而不是只靠静态门禁假绿。
用例声明在 mod 的 `commands_register.register()` 里:

    registry.eval_cases = [
        {
            "id": "toggle_done_happy_path",
            "command": "item.toggle_done",
            "seed": [{"type": "item", "id": "item-1", "data": {"title": "T", "done": False}}],
            "args": {"id": "item-1"},
            "expect": {
                "status": "ok",
                "operation": {"kind": "native", "patch_count": 1},
                "patches": [{"entity_type": "item", "entity_id": "item-1", "field": "done", "after": True}],
                "entities": [{"type": "item", "id": "item-1", "data": {"done": True}}],
                "max_latency_ms": 200,
            },
        },
        {
            "id": "missing_entity_rejects",
            "command": "entity.duplicate",
            "args": {"type": "item", "id": "missing"},
            "expect": {"status": "error", "error_code": "entity_not_found"},
        },
        {   # requires_confirm 命令的一阶段声明式用例(C5):未确认 dispatch 必须
            # 返回 PendingConfirmation(带 token、零落库);可用 entities/entity_counts
            # 断言 doc 未变。二阶段编舞由 core 单测覆盖,eval 不复刻。
            "id": "dangerous_command_gates",
            "command": "batch.discard_all",
            "expect": {"status": "pending_confirmation",
                       "entity_counts": {"item": 3}},
        },
    ]

语义:
  - `expect.operation` 除 partial subset 与 `patch_count` 外,另支持撤销粒度断言:
    `reverse_patch_count`(reverse_patches 数量等值)与 `group_id_present`(group_id 非空与否)。
  - 未声明 `registry.eval_cases` → 渐进跳过(旧 skill 不炸)。
  - 声明为空但存在 mod native/stage 命令 → 失败(防 checkbox)。
  - 声明后,每个 case 在独立临时 project 中 seed → dispatch → 断言 operation / patch / entity / latency。
  - 默认阻断 sdk.model 出网点,case 若意外走到外部会以确定性错误失败。

跑法:
  python scripts/eval_suite.py
  python scripts/eval_suite.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def contains_subset(actual: Any, expected: Any) -> bool:
    """expected 是 partial pattern:dict 只要求列出的 key;list 按顺序递归匹配。"""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        for key, value in expected.items():
            if key not in actual or not contains_subset(actual[key], value):
                return False
        return True
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) < len(expected):
            return False
        return all(contains_subset(a, e) for a, e in zip(actual, expected))
    return actual == expected


def _patch_matches(actual_patch: dict, expected_patch: dict) -> bool:
    return contains_subset(actual_patch, expected_patch)


def _load_cases(ctx) -> list[dict] | None:
    cases = getattr(ctx.cmd_reg, "eval_cases", None)
    if cases is None:
        return None
    if not isinstance(cases, list):
        raise TypeError("registry.eval_cases must be a list")
    return cases


def _builtin_ids() -> set[str]:
    from sdk import AssetStore, CommandRegistry, register_builtin_commands, register_manifest_commands
    reg = CommandRegistry()
    register_builtin_commands(reg)
    with tempfile.TemporaryDirectory(prefix="ze-eval-builtin-") as td:
        register_manifest_commands(reg, asset_store=AssetStore(Path(td) / "a"),
                                   exports_dir=Path(td) / "exports")
    return {s.id for s in reg.list()}


def _block_external_calls() -> None:
    import sdk.model as model
    from sdk import ZeError

    def _blocked(*_a, **_k):
        raise ZeError("model_call_failed", "__eval_suite_external_reached__", context={})

    model._post_json = _blocked


def _actor(case: dict):
    from sdk import Actor
    actor = case.get("actor") or {}
    return Actor(
        user_id=str(actor.get("user_id") or "__eval__"),
        device_id=str(actor.get("device_id") or "eval"),
        source=str(actor.get("source") or case.get("source") or "user"),
    )


def _seed_entities(ctx, case: dict) -> None:
    from sdk import Entity

    for raw in case.get("seed") or []:
        ctx.doc.add(Entity(
            type=raw["type"],
            id=raw.get("id"),
            name=raw.get("name", ""),
            data=dict(raw.get("data") or {}),
        ))


def _assert_operation(op_dict: dict, expect: dict, errors: list[str]) -> None:
    op_expect = dict(expect.get("operation") or {})
    patch_count = op_expect.pop("patch_count", None)
    # 撤销粒度断言面(四步闸第 2 步「撤销粒度不变」的声明位;纯增量,同 patch_count 语义):
    #   reverse_patch_count — reverse_patches 数量等值断言(undo 覆盖面)
    #   group_id_present    — group_id 非空与否(合并撤销分组是否存在)
    reverse_patch_count = op_expect.pop("reverse_patch_count", None)
    group_id_present = op_expect.pop("group_id_present", None)
    if op_expect and not contains_subset(op_dict, op_expect):
        errors.append(f"operation mismatch: expected subset {op_expect}, got {op_dict}")
    if patch_count is not None and len(op_dict.get("patches") or []) != int(patch_count):
        errors.append(f"patch_count mismatch: expected {patch_count}, got {len(op_dict.get('patches') or [])}")
    if reverse_patch_count is not None and (
            len(op_dict.get("reverse_patches") or []) != int(reverse_patch_count)):
        errors.append(
            f"reverse_patch_count mismatch: expected {reverse_patch_count}, "
            f"got {len(op_dict.get('reverse_patches') or [])}")
    if group_id_present is not None and bool(op_dict.get("group_id")) != bool(group_id_present):
        errors.append(
            f"group_id_present mismatch: expected {bool(group_id_present)}, "
            f"got {op_dict.get('group_id')!r}")

    actual_patches = op_dict.get("patches") or []
    for expected_patch in expect.get("patches") or []:
        if not any(_patch_matches(p, expected_patch) for p in actual_patches):
            errors.append(f"missing expected patch subset: {expected_patch}")


def _assert_entities(ctx, expect: dict, errors: list[str]) -> None:
    for expected in expect.get("entities") or []:
        try:
            entity = ctx.doc.get(expected["type"], expected["id"])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"entity not found for expectation {expected}: {type(exc).__name__}: {exc}")
            continue
        actual = entity.to_dict()
        if not contains_subset(actual, expected):
            errors.append(f"entity mismatch: expected subset {expected}, got {actual}")

    for entity_type, expected_count in (expect.get("entity_counts") or {}).items():
        actual_count = len(ctx.doc.list(entity_type))
        if actual_count != int(expected_count):
            errors.append(f"entity_count[{entity_type}] mismatch: expected {expected_count}, got {actual_count}")


def _run_case(repo, case: dict) -> dict:
    from sdk import ZeError

    cid = case.get("id") or case.get("command") or "<unnamed>"
    pid = repo.create_project(name=f"eval:{cid}", owner="__eval__", seed=bool(case.get("seed_demo", False)))
    ctx = repo.get_or_load(pid)
    _seed_entities(ctx, case)

    expect = dict(case.get("expect") or {})
    expected_status = expect.get("status", "ok")
    source = str(case.get("source") or "user")
    actor = _actor(case)
    args = dict(case.get("args") or {})
    started = time.perf_counter()
    errors: list[str] = []
    op_dict: dict | None = None
    error_code = ""

    try:
        op = ctx.cmd_reg.dispatch(case["command"], args, actor=actor, source=source,
                                  doc=ctx.doc, op_log=ctx.op_log)
        latency_ms = int((time.perf_counter() - started) * 1000)
        # C5(大整合反哺):requires_confirm 命令的声明式一阶段用例——未 confirmed 的
        # dispatch 必须返回 PendingConfirmation(带 token、零落库),而非直接执行。
        # 二阶段(reserve→confirm→finalize)是网页人工通道的编舞,由 core 单测覆盖;
        # eval 不复刻(复刻=自造第二条确认通道,架构禁止)。
        if expected_status == "pending_confirmation":
            from sdk import PendingConfirmation
            if not isinstance(op, PendingConfirmation):
                errors.append(f"expected PendingConfirmation, got {type(op).__name__}")
            else:
                if not op.token:
                    errors.append("PendingConfirmation.token is empty")
                if op.command_id != case["command"]:
                    errors.append(f"pending command_id mismatch: {op.command_id}")
                _assert_entities(ctx, expect, errors)   # 断言零落库(doc 未变)
            op_dict = None
        else:
            op_dict = op.to_dict() if hasattr(op, "to_dict") else dict(op)
            if expected_status != "ok":
                errors.append(f"expected error {expect.get('error_code') or '<any>'}, got ok")
            else:
                _assert_operation(op_dict, expect, errors)
                _assert_entities(ctx, expect, errors)
    except ZeError as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        error_code = exc.code
        if expected_status != "error":
            errors.append(f"unexpected ZeError {exc.code}: {exc.message}")
        elif expect.get("error_code") and exc.code != expect.get("error_code"):
            errors.append(f"error_code mismatch: expected {expect.get('error_code')}, got {exc.code}")
    except Exception as exc:  # noqa: BLE001
        latency_ms = int((time.perf_counter() - started) * 1000)
        errors.append(f"unexpected exception {type(exc).__name__}: {exc}")

    max_latency = expect.get("max_latency_ms")
    if max_latency is not None and latency_ms > int(max_latency):
        errors.append(f"latency {latency_ms}ms > budget {max_latency}ms")

    return {
        "id": cid,
        "command": case.get("command"),
        "status": "passed" if not errors else "failed",
        "latency_ms": latency_ms,
        **({"operation": op_dict} if op_dict is not None else {}),
        **({"error_code": error_code} if error_code else {}),
        **({"errors": errors} if errors else {}),
    }


def run() -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="ze-eval-suite-"))
    old_env = {
        "ZE_DATA_DIR": os.environ.get("ZE_DATA_DIR"),
        "ZE_USERS_PATH": os.environ.get("ZE_USERS_PATH"),
    }
    os.environ["ZE_DATA_DIR"] = str(tmp / "data")
    os.environ["ZE_USERS_PATH"] = str(tmp / "users.json")
    try:
        _block_external_calls()
        from sdk.bootstrap import bootstrap
        repo = bootstrap()
        pid = repo.create_project(name="eval-probe", owner="__eval__", seed=False)
        ctx = repo.get_or_load(pid)
        cases = _load_cases(ctx)
        mod_commands = {
            s.id for s in ctx.cmd_reg.list()
            if s.id not in _builtin_ids() and not s.id.startswith("pipeline.") and s.kind in {"native", "stage"}
        }
        if cases is None:
            return {
                "ok": True,
                "skipped": "mod 未声明 registry.eval_cases(声明后 eval suite 强制执行)",
                "hint": "在 commands_register.register() 里设 registry.eval_cases=[...]",
                "command_preview": sorted(mod_commands),
                "cases": [],
            }
        if not cases and mod_commands:
            return {
                "ok": False,
                "error": "registry.eval_cases 声明为空但存在 mod native/stage 命令",
                "command_preview": sorted(mod_commands),
                "cases": [],
            }
        results = [_run_case(repo, case) for case in cases]
        covered = sorted({r["command"] for r in results if r.get("command")})
        uncovered = sorted(mod_commands - set(covered))
        failed = [r for r in results if r["status"] != "passed"]
        return {
            "ok": not failed,
            "schema": "ze.eval_suite.result.v1",
            "case_count": len(results),
            "covered_commands": covered,
            "uncovered_commands": uncovered,
            "coverage": {
                "covered": len(set(covered) & mod_commands),
                "total_mod_native_or_stage": len(mod_commands),
            },
            "cases": results,
        }
    finally:
        import shutil
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run declared ZE command eval cases")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    try:
        res = run()
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, indent=2))
        traceback.print_exc()
        return 1
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif res.get("skipped"):
        print(f"⊘ eval-suite: {res['skipped']}")
    elif res.get("ok"):
        cov = res.get("coverage") or {}
        print(
            f"✓ eval-suite: {res.get('case_count', 0)} cases passed; "
            f"command coverage {cov.get('covered', 0)}/{cov.get('total_mod_native_or_stage', 0)}"
        )
    else:
        print("✗ eval-suite failed")
        for case in res.get("cases", []):
            if case.get("status") == "failed":
                print(f"  [{case.get('id')}] {case.get('command')} — {'; '.join(case.get('errors') or [])}")
        if res.get("error"):
            print(f"  {res['error']}")
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
