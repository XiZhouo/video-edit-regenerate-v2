#!/usr/bin/env python3
"""从视频服务 /discover 生成**逐模式**能力真值表。

为什么要有这个脚本:模型能做什么、收什么文件、收几个,是**服务的事实**,不是我们的记忆。
手抄一份表意味着模型一变我们就悄悄地错,而且错得没人发现。这里把它变成一次机械采集。

为什么是「逐模式」:服务返回的 ``limits`` **本来就是按模式加前缀的** ——
``reference_required_file_types`` / ``reference_file_types`` / ``max_reference_*`` 属于
reference-to-video;``edit_file_types`` / ``edit_required_videos`` / ``edit_max_videos`` 属于 edit。
把它们压平成「模型级」会造成一类很隐蔽的错:**拿 A 模式的限制去卡 B 模式**。
最典型的一例是 gemini-omni:reference 模式确实「纯视频会被拒、必须至少一张图」,
但 edit 模式的规矩是「正好 1 个视频,图片可选」——压平之后,
「一个待编辑视频 + 一句话说改哪里」这个视频编辑最标准的用法就会被自己拦死。
所以本文件的**核心约定**:

    ref_grammar  只放真正与模式无关的东西(记号写法/起始序号/按不按类型计数)
    slots[mode]  放该模式的准入类型、必备类型、数量上下限、尺寸下限、替代参数

用法:
    python3 sync_capability_matrix.py                  # 从 $ZE_VIDEOGEN_BASE_URL/discover 拉取并写入
    python3 sync_capability_matrix.py discover.json    # 用本地快照生成(离线/可复现)
    python3 sync_capability_matrix.py --check          # 只比对,不写;有差异 exit 1(给门禁用)

服务地址只从环境变量取(``ZE_VIDEOGEN_BASE_URL`` / ``VIDEOGEN_API_BASE_URL``),
不在代码里写死内网地址。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from typing import Any

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(REPO_ROOT, "src", "ze_video_domain", "capability_matrix.json")
SCHEMA_ID = "ze.video_capability_matrix.v1"
BASE_URL_ENV_VARS = ("ZE_VIDEOGEN_BASE_URL", "VIDEOGEN_API_BASE_URL")

# 服务声明的五种模式。顺序固定 —— 生成物要可 diff。
MODES = ("text-to-video", "image-to-video", "keyframe-to-video", "reference-to-video", "edit")

# 记号写法从 limits.reference_note 里机械解析。认不出来就**标红,不猜** ——
# 猜一个错的记号比没有记号更糟:模型会把它当普通文字,而我们以为绑定成功了。
REF_SYNTAX_PATTERNS = (
    (r"@file_0,\s*@file_1", "@file_{n}", 0, "flat"),
    (r"@Image1,\s*@Image2", "@Image{n}", 1, "per_kind"),
    (r"\[Image 1\],\s*\[Image 2\]", "[Image {n}]", 1, "per_kind"),
    (r"<<<image_1>>>,\s*<<<image_2>>>", "<<<{kind}_{n}>>>", 1, "per_kind"),
)


def base_url() -> str:
    for var in BASE_URL_ENV_VARS:
        value = os.environ.get(var)
        if value:
            return value.rstrip("/")
    raise SystemExit(
        "视频服务地址未配置;设置 env " + " 或 ".join(BASE_URL_ENV_VARS)
        + ",或传入一个本地 discover.json 快照")


def fetch_discover() -> dict[str, Any]:
    url = f"{base_url()}/discover"
    with urllib.request.urlopen(url, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _min_pixels(raw: Any) -> list[int] | None:
    """``"300x300"`` → ``[300, 300]``。服务给的是字符串,不是数组。"""
    if not isinstance(raw, str):
        return None
    m = re.fullmatch(r"\s*(\d+)\s*[xX×]\s*(\d+)\s*", raw)
    return [int(m.group(1)), int(m.group(2))] if m else None


def ref_grammar(limits: dict[str, Any]) -> dict[str, Any]:
    """只解析**与模式无关**的记号写法。配额/必备类型一律不放这里。"""
    note = str(limits.get("reference_note") or "").strip()
    grammar: dict[str, Any] = {"token": None, "start": 0, "scope": "none", "note": note}
    for pattern, token, start, scope in REF_SYNTAX_PATTERNS:
        if re.search(pattern, note):
            grammar.update({"token": token, "start": start, "scope": scope})
            return grammar
    if note and not re.search(r"[一-鿿]", note):
        # 有英文 note 却认不出写法 = 服务加了新语法而我们还不认识。标出来让门禁红。
        grammar["unrecognised_syntax"] = True
    return grammar


def _slot_text_to_video() -> dict[str, Any]:
    return {"kinds": [], "required_kinds": [], "min_total": 0, "max_total": 0,
            "note": "文生视频不带文件"}


def _slot_image_to_video() -> dict[str, Any]:
    # 结构性约束:图生视频就是「一张首帧」,与 reference 配额无关。
    return {"kinds": ["image"], "required_kinds": ["image"], "min_total": 1, "max_total": 1,
            "note": "首帧图,恰好 1 张"}


def _slot_keyframe_to_video() -> dict[str, Any]:
    # 结构性约束:首帧 + 尾帧。同样与 reference 配额无关(kling-v3 的
    # max_reference_images=1 说的是参考图,不是首尾帧)。
    return {"kinds": ["image"], "required_kinds": ["image"], "min_total": 2, "max_total": 2,
            "note": "首帧 + 尾帧,恰好 2 张"}


def _slot_reference_to_video(limits: dict[str, Any]) -> dict[str, Any]:
    max_by_kind: dict[str, int] = {}
    if isinstance(limits.get("max_reference_images"), int):
        max_by_kind["image"] = limits["max_reference_images"]
    elif isinstance(limits.get("tested_reference_images"), int):
        max_by_kind["image"] = limits["tested_reference_images"]
    if isinstance(limits.get("tested_reference_videos"), int):
        max_by_kind["video"] = limits["tested_reference_videos"]
    slot: dict[str, Any] = {
        "kinds": list(limits.get("reference_file_types") or []),
        "required_kinds": list(limits.get("reference_required_file_types") or []),
        "min_total": 1,
        "max_total": limits.get("max_reference_files"),
        "max_by_kind": max_by_kind,
        "max_total_with_video": limits.get("max_reference_files_with_video"),
        "min_pixels": _min_pixels(limits.get("image_min_pixels")),
        "note": str(limits.get("reference_note") or ""),
    }
    return {k: v for k, v in slot.items() if v not in (None, {}, "")}


def _slot_edit(limits: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """edit 的规矩与 reference **完全不同**,必须单独取 ``edit_*`` 前缀的键。

    服务原话:「实测 edit 使用文件输入时必须正好 1 个视频,可附加图片;
    previous_interaction_id 编辑不传文件。」—— 所以图片是**可选**的,
    required 的是视频,而且整组文件可以被 previous_interaction_id 整体替代。
    """
    max_by_kind: dict[str, int] = {}
    min_by_kind: dict[str, int] = {}
    if isinstance(limits.get("edit_max_videos"), int):
        max_by_kind["video"] = limits["edit_max_videos"]
    if isinstance(limits.get("tested_edit_images"), int):
        max_by_kind["image"] = limits["tested_edit_images"]
    if isinstance(limits.get("edit_required_videos"), int):
        min_by_kind["video"] = limits["edit_required_videos"]
    slot: dict[str, Any] = {
        "kinds": list(limits.get("edit_file_types") or []),
        "required_kinds": [],          # 明确:edit 不要求图片
        "min_total": 0,                # 可以一个文件都不传(改用 alt_param)
        "min_by_kind": min_by_kind,    # 但只要传文件,视频数就必须够
        "max_by_kind": max_by_kind,
        "alt_param": "previous_interaction_id" if "previous_interaction_id" in params else None,
        "note": str(limits.get("edit_note") or ""),
    }
    return {k: v for k, v in slot.items() if v not in (None, {}, "")}


def build_slots(capabilities: list[str], limits: dict[str, Any],
                params: dict[str, Any]) -> dict[str, Any]:
    builders = {
        "text-to-video": lambda: _slot_text_to_video(),
        "image-to-video": lambda: _slot_image_to_video(),
        "keyframe-to-video": lambda: _slot_keyframe_to_video(),
        "reference-to-video": lambda: _slot_reference_to_video(limits),
        "edit": lambda: _slot_edit(limits, params),
    }
    return {mode: builders[mode]() for mode in MODES if mode in capabilities}


def build_params(limits: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """把「换了模型之后哪些参数还合法」需要的东西收进来。

    批量场景切换模型时要靠它判断:时长还在不在范围内、画幅比还支不支持。
    gemini-omni 干脆没有 duration —— 那就是 None,不是 0,更不是默认值。
    """
    out: dict[str, Any] = {
        "aspect_ratios": list(limits.get("aspect_ratios") or []) or None,
        "resolutions": list(limits.get("resolutions") or []) or None,
        "duration_range": limits.get("duration_range"),
        "duration_options": limits.get("duration_options"),
        "duration_by_resolution": limits.get("duration_by_resolution"),
        "knobs": {k: v for k, v in params.items() if k != "aspect_ratio"} or None,
    }
    return {k: v for k, v in out.items() if v is not None}


def build_matrix(discover: dict[str, Any]) -> dict[str, Any]:
    models: dict[str, Any] = {}
    for mid, spec in (discover.get("models") or {}).items():
        limits = spec.get("limits") or {}
        params = spec.get("params") or {}
        capabilities = list(spec.get("capabilities") or [])
        unknown = [c for c in capabilities if c not in MODES]
        entry: dict[str, Any] = {
            "provider": spec.get("provider"),
            "description": spec.get("description"),
            "capabilities": capabilities,
            "ref_grammar": ref_grammar(limits),
            "slots": build_slots(capabilities, limits, params),
            "params": build_params(limits, params),
        }
        if unknown:
            # 服务上了新模式而我们还没建槽 —— 不静默忽略。
            entry["unknown_capabilities"] = unknown
        models[mid] = entry
    return {
        "schema": SCHEMA_ID,
        "service_name": discover.get("name"),
        "service_version": discover.get("version"),
        # 不落内网地址:只记「从哪个环境变量指向的服务采的」。
        "generated_from": "$" + BASE_URL_ENV_VARS[0] + "/discover",
        "generated_at": int(time.time()),
        "modes": list(MODES),
        "aliases": dict(discover.get("model_aliases") or {}),
        "models": models,
    }


def _comparable(matrix: dict[str, Any]) -> str:
    """去掉时间戳后的可比形态 —— 只有真内容变了才算变。"""
    clone = {k: v for k, v in matrix.items() if k != "generated_at"}
    return json.dumps(clone, ensure_ascii=False, sort_keys=True, indent=1)


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    snapshot = next((a for a in argv[1:] if not a.startswith("--")), None)
    discover = json.load(open(snapshot, encoding="utf-8")) if snapshot else fetch_discover()
    matrix = build_matrix(discover)

    if check_only:
        if not os.path.exists(OUT_PATH):
            print(f"MISSING {OUT_PATH} — 先跑一次不带 --check 的同步")
            return 1
        current = json.load(open(OUT_PATH, encoding="utf-8"))
        if _comparable(current) == _comparable(matrix):
            print(f"OK  真值表与服务一致({len(matrix['models'])} 模型)")
            return 0
        print("DRIFT  真值表与服务已不一致 —— 重跑同步并复核受影响的工具")
        return 1

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(matrix, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    modes_seen = sorted({m for spec in matrix["models"].values() for m in spec["slots"]})
    print(f"WROTE {OUT_PATH}")
    print(f"  {len(matrix['models'])} 模型 / 模式 {modes_seen}")
    for mode in MODES:
        n = sum(1 for s in matrix["models"].values() if mode in s["slots"])
        print(f"    {mode:22s} {n:2d} 个模型")
    flagged = [m for m, s in matrix["models"].items() if s["ref_grammar"].get("unrecognised_syntax")]
    if flagged:
        print(f"  ⚠️ 认不出记号写法(需人工确认): {flagged}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
