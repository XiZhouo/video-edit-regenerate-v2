#!/usr/bin/env python3
"""usability_check.py — 可用性烟雾门禁(轨⑤):治"假绿"。

其它门禁(pytest/css/i18n/typecheck/lock)全过 ≠ 人能用:空壳命令、无校验命令、
"空进空出"都能过。本门禁专盯一类真人最容易撞的失败:**命令静默接受空输入、
不报错地产出垃圾**(image-prompt-studio 早期就是:没填意图→点按钮→LLM 收空→出占位图)。

做法:对每个 mod 注册的 native 命令,在一个**空白实体**上 dispatch,断言它**不会静默通过**:
  - expect="reject"(默认):必须 raise ZeError(命令有空输入护栏)→ PASS;
    成功返回 Operation = 空输入静默通过 → FAIL;抛非 ZeError = 崩溃 → FAIL。
  - expect="ok":空输入合法(纯创建类命令),必须成功 → PASS。
  - expect="pending":requires_confirm 两段化命令,裸 dispatch 只产惰性 pending(零落库),
    成功即 PASS;真正的空输入拒绝在 confirm 段 handler,由 mod pytest 全编舞覆盖。
  护栏型拒绝码 = validation_failed / schema_violation / poll_failed(poll 前置拦截同为护栏)。

命令的"输入实体类型"无法从 spec.touches 可靠推断(如 generate touches=result 却读 instruction),
故由 mod 显式声明用例:在 commands_register.register() 里设
    registry.usability_cases = [
        {"command": "instruction.brief_fill", "entity_type": "instruction"},          # expect 默认 reject
        {"command": "some.create",            "entity_type": "x", "expect": "ok"},
    ]
框架 builtin(entity.* / document.*)默认豁免(各有独立测试)。

**覆盖强制**:每个 mod native 命令都必须有一条 usability_case(reject / ok / pending),否则 FAIL ——
逼"新加命令必须想清楚空输入怎么办",这正是"后续不再出现"的机制。

跑:`<skill>/.venv/bin/python scripts/usability_check.py`(从 skill 根目录)。退出码 0=过,1=失败。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _builtin_ids() -> set[str]:
    """框架命令 id 集合(豁免对象)= 空 registry 注册 builtin + manifest 后的全部 id。

    豁免理由同 entity.*:框架命令各有独立测试(manifest.* 在 test_envelope.py 全谱),
    可用性烟雾只管 **mod** native 命令。
    """
    import tempfile as _tf
    from sdk import CommandRegistry, register_builtin_commands, register_manifest_commands
    from sdk import AssetStore as _AS
    reg = CommandRegistry()
    register_builtin_commands(reg)
    with _tf.TemporaryDirectory(prefix="ze-usability-ids-") as td:
        register_manifest_commands(reg, asset_store=_AS(Path(td) / "a"),
                                   exports_dir=Path(td) / "exports")
    return {s.id for s in reg.list()}


def run() -> dict:
    # 隔离数据目录,避免污染真实 .data;收尾删掉(finally)。
    tmp = Path(tempfile.mkdtemp(prefix="ze-usability-"))
    os.environ["ZE_DATA_DIR"] = str(tmp / "data")
    os.environ.setdefault("ZE_USERS_PATH", str(tmp / "users.json"))
    try:
        return _run_in(tmp)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def _run_in(tmp: Path) -> dict:
    from sdk import Actor, Entity, ZeError
    from sdk.bootstrap import bootstrap, SYSTEM_USER_ID
    import sdk.model as model

    # 唯一出网点 _post_json 替成"立即抛 model_call_failed"——命令若**没拦**空输入、走到调模型,
    # 就撞这里(确定性、零网络、不会挂);真有护栏则在此之前已 raise validation_failed。
    # 据 error code 区分"护栏拦下"vs"已抵达外部",门禁测的是护栏而非环境/网络。
    _SENTINEL = "__usability_external_reached__"

    def _blocked_post(*_a, **_k):
        raise ZeError("model_call_failed", _SENTINEL, context={})
    model._post_json = _blocked_post

    repo = bootstrap()
    # 配完整(占位)模型配置,让"配置缺失"不会误报成 validation_failed —— 唯一剩下的
    # validation_failed 来源就只有命令自己的空输入护栏。URL 不会真用到(_post_json 已被替)。
    repo.config.set_values({
        "ze.llm.route": "gateway",
        "ze.llm.gateway_base": "http://usability.invalid/v1",
        "ze.llm.gateway_key": "sk-usability-placeholder",
        "ze.image.service_url": "http://usability.invalid/v1",
    })
    pid = repo.create_project(name="__usability__", owner=SYSTEM_USER_ID, seed=False)
    ctx = repo.get_or_load(pid)
    actor = Actor(user_id=SYSTEM_USER_ID, source="ai")

    # 算"护栏型拒绝"的 error code:命令在调外部前自己拦下空输入应是这些;
    # model_call_failed 等 = 没拦住、已经去调模型 = 护栏失效。
    # poll_failed:spec.poll() 前置条件拒绝同为护栏型(design coresync 反哺——缺它逼下游
    # 撤 poll 守卫改 expect=ok)。
    GUARD_CODES = {"validation_failed", "schema_violation", "poll_failed"}

    builtins = _builtin_ids()
    mod_native = {s.id for s in ctx.cmd_reg.list()
                  if s.id not in builtins and s.kind == "native"}

    if not mod_native:
        return {"ok": True, "skipped": "无 mod native 命令,无需可用性烟雾", "cases": []}

    cases = getattr(ctx.cmd_reg, "usability_cases", None)
    if cases is None:
        # 未声明 = 尚未采用本门禁 → 软跳过(声明后即强制全覆盖;受管下发的渐进 rollout 语义)
        return {"ok": True,
                "skipped": "mod 未声明 registry.usability_cases(声明后本门禁强制每个 native 命令有用例)",
                "hint": "在 commands_register.register() 里设 registry.usability_cases=[{command,entity_type,expect?=reject|ok|pending}]",
                "uncovered_preview": sorted(mod_native)}
    if not cases:
        return {"ok": False,
                "error": "registry.usability_cases 声明为空但存在 mod native 命令",
                "hint": "给每个 native 命令一条用例(reject / ok / pending)",
                "uncovered": sorted(mod_native)}

    schema = ctx.doc.schema  # {entity_type: {properties: {field: {type, required, ...}}}}

    def _empty_for(field_spec: dict) -> object:
        t = (field_spec or {}).get("type")
        return {"number": 0, "integer": 0, "boolean": False,
                "array": [], "object": {}}.get(t, "")  # 默认 string → ""

    def _blank_data(entity_type: str, override: dict) -> dict:
        """造"建好但没填"的实体数据:必填字段置类型空值(string→""),mimic 真人新建未填。
        这样过 schema(required=存在性)却语义为空 —— 正是命令护栏该拦的输入。"""
        props = (schema.get(entity_type) or {}).get("properties") or {}
        data = {f: _empty_for(spec) for f, spec in props.items() if spec.get("required")}
        data.update(override)
        return data

    covered = {c.get("command") for c in cases}
    uncovered = sorted(mod_native - covered)
    stale = sorted(c.get("command") for c in cases if c.get("command") not in mod_native)
    results: list[dict] = []
    n = 0
    for case in cases:
        cmd = case.get("command")
        et = case.get("entity_type")
        expect = case.get("expect", "reject")
        if cmd not in mod_native:
            # 声明了未注册/已改名的命令 → FAIL(否则给假覆盖:门禁过却没真测过这命令)
            results.append({"command": cmd, "verdict": "FAIL",
                            "detail": "usability_case 指向未注册的 mod native 命令(改名/删除残留?)"})
            continue
        n += 1
        blank = Entity(type=et, id=f"__usab_{n}", data=_blank_data(et, dict(case.get("data") or {})))
        ctx.doc.add(blank)
        verdict, detail = "PASS", ""
        try:
            ctx.cmd_reg.dispatch(cmd, {"id": blank.id, "type": et},
                                 actor=actor, source="ai", doc=ctx.doc, op_log=ctx.op_log)
            # 成功返回:expect=ok(直接成功)或 pending(requires_confirm 两段化:裸 dispatch
            # 只产惰性 pending,真拒绝在 confirm 段——语义显性声明,design coresync 反哺)
            if expect in ("ok", "pending"):
                verdict, detail = "PASS", ("空输入合法且成功(empty-safe)" if expect == "ok"
                                           else "两段化 pending(护栏在 confirm 段,由 mod 测试覆盖)")
            else:
                verdict, detail = "FAIL", "空输入未被拒绝(静默通过 = 疑似空进空出);execute 应 raise ZeError"
        except ZeError as e:
            if expect in ("ok", "pending"):
                verdict, detail = "FAIL", f"expect={expect} 却被拒:{e.code} {getattr(e, 'message', '')}"
            elif e.code in GUARD_CODES:
                verdict, detail = "PASS", f"空输入被护栏拦下({e.code})"
            else:
                # 已经走到调模型/外部才失败 → 护栏没拦住空输入(生产环境配置可达时就会空进空出)
                verdict, detail = "FAIL", (f"空输入未在调外部前被拦,走到了 {e.code} "
                                           f"({getattr(e, 'message', '')});execute 应先 raise validation_failed")
        except Exception as e:  # noqa: BLE001
            verdict, detail = "FAIL", f"空输入抛非 ZeError(崩溃):{type(e).__name__}: {e}"
        results.append({"command": cmd, "entity_type": et, "expect": expect,
                        "verdict": verdict, "detail": detail})

    failed = [r for r in results if r["verdict"] == "FAIL"]
    ok = not failed and not uncovered
    out = {"ok": ok, "cases": results}
    if uncovered:
        out["uncovered"] = uncovered
        out["hint"] = "每个 mod native 命令都要一条 usability_case(reject|ok|pending);未覆盖的请补声明"
    if stale:
        out["stale_cases"] = stale
    return out


def main() -> int:
    try:
        res = run()
    except Exception as e:  # noqa: BLE001
        import traceback
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        traceback.print_exc()
        return 1
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if res.get("ok"):
        n = len([c for c in res.get("cases", []) if c.get("verdict") == "PASS"])
        print(f"✓ 可用性烟雾:{n} 条命令空输入均被妥善处理(拒绝/empty-safe)")
        return 0
    print("✗ 可用性烟雾失败 — 有命令静默接受空输入,或命令未声明可用性用例")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
