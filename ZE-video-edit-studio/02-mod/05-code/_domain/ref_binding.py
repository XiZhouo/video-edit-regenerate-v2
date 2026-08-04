"""输入引用绑定层 —— 把人写的素材名翻成目标模型认的记号,并且和实发文件同源产出。

## 为什么要有这一层

一次生成请求 = 一段提示词 + 一组**有顺序**的输入文件。提示词必须指明「哪个输入干什么用」,
而每家模型指认输入的方式都不一样::

    seedance     @file_0        0 起,所有输入排一条队
    grok         @Image1        1 起,按类型计数
    happyhorse   [Image 1]      1 起,按类型计数
    kling-omni   <<<image_1>>> / <<<video_1>>>   1 起,**按类型分别计数**
    veo / kling-v3 / gemini-omni                 没有记号,只能靠句子描述

同一批 ``[图A, 视频B, 图C]``:seedance 是 file_0/file_1/file_2,kling 是
image_1/video_1/image_2 —— **同一批里出现两个「1」,指的是不同文件**。
翻错了不会报错,只是结果「不太对」,没人说得清哪不对。

## 三条不让步的设计

1. **输入是中性的。**只有名字、媒体类型、文件三个属性。它是「被编辑的对象」还是「风格参考」
   还是「反例」,由提示词那句话决定,不在字段名或类型枚举里编码用途 —— 那会把一种用法焊死,
   而同一个素材在不同任务里本可以扮演完全不同的角色。
2. **记号、输入顺序、真实文件是同一次编译的产物,作为不可分的结果一起返回。**
   绝不允许在一处算记号、另一处组文件列表 —— 两边各自演化,记号就会指向错误的文件。
   (这正是本仓上一版的真实缺陷:命名素材只把 ``{name, media_type}`` 传下去,
   文件没跟着走,于是 ``@file_0`` 指向的是另一条路组出来的数组第 0 个。)
3. **花钱之前拦。**收不了这个类型、超配额、类型互相挤占、引用了不存在的名字、尺寸不足,
   都在发送前判定并说清是**哪个输入的什么问题**,不静默丢弃后照发。

## 与真值表的分工(这一条是本层的立身之本)

约束**按模式**取,不按模型取::

    ref_grammar   只有记号写法/起始序号/按不按类型计数 —— 真正与模式无关的东西
    slots[mode]   准入类型、必备类型、数量上下限、尺寸下限、替代参数 —— 逐模式

把逐模式的规矩压平成模型级,会造成一类很隐蔽的错:**拿 A 模式的限制去卡 B 模式**。
最典型的一例是 gemini-omni —— reference 模式确实「必须至少一张图,纯视频会被上游拒」,
但 edit 模式的规矩是「正好 1 个视频,图片可选」。压平之后,
「一个待编辑视频 + 一句话说改哪里」这个视频编辑最标准的用法会被自己拦死。

## 占位符写法

用 ``{素材名}``(花括号),**不用 ``@名字``** —— 后者和 seedance 自己的记号 ``@file_0`` 同形,
素材恰好叫 ``file_0`` 时会静默指错。花括号没有这个歧义。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from . import capability_matrix as caps

# 人写的引用:{素材名}。名字里不允许再出现花括号。
PLACEHOLDER = re.compile(r"\{([^{}]+)\}")

_ZH_KIND = {"image": "图片", "video": "视频", "audio": "音频"}


def zh_kind(kind: str) -> str:
    return _ZH_KIND.get(kind, kind)


@dataclass(frozen=True)
class Input:
    """一个中性输入。角色不在这里,在提示词里。"""

    name: str
    kind: str                       # image / video / audio
    ref: Any = None                 # 资产引用(AssetRef / sha256 / URL),本层不关心其形态
    pixels: tuple[int, int] | None = None   # (宽, 高),仅用于尺寸硬限校验


@dataclass
class Compiled:
    """一次编译的完整结果 —— 记号、顺序、文件三者同源,必须整体使用。"""

    prompt: str
    files: list = field(default_factory=list)       # 实发文件,顺序即契约
    order: list = field(default_factory=list)       # [{name, media_type, index}] 给人看的清单
    tokens: dict = field(default_factory=dict)      # 名字 → 记号
    errors: list = field(default_factory=list)      # 非空即不得发送
    warnings: list = field(default_factory=list)    # 黄:能发,但请人看一眼

    @property
    def ok(self) -> bool:
        return not self.errors


def _token_for(grammar: Mapping[str, Any], kind: str, n: int) -> str | None:
    template = grammar.get("token")
    if not template:
        return None
    return str(template).replace("{kind}", kind).replace("{n}", str(n))


def _quota_errors(slot: Mapping[str, Any], inputs: Sequence[Input]) -> list[str]:
    errs: list[str] = []
    by_kind: dict[str, int] = {}
    for r in inputs:
        by_kind[r.kind] = by_kind.get(r.kind, 0) + 1
    total = len(inputs)

    max_total = slot.get("max_total")
    # 类型互相挤占:带了视频,总量上限会降(kling-omni 7 → 4)。
    with_video = slot.get("max_total_with_video")
    if with_video is not None and by_kind.get("video"):
        if total > with_video:
            errs.append(f"带参考视频时最多 {with_video} 个输入"
                        f"(不带视频可到 {max_total if max_total is not None else '更多'}),现在有 {total} 个")
            return errs
    elif max_total is not None and total > max_total:
        errs.append(f"最多 {max_total} 个输入,现在有 {total} 个")

    for kind, cap in (slot.get("max_by_kind") or {}).items():
        if by_kind.get(kind, 0) > cap:
            errs.append(f"最多 {cap} 个{zh_kind(kind)}输入,现在有 {by_kind[kind]} 个")
    for kind, need in (slot.get("min_by_kind") or {}).items():
        if by_kind.get(kind, 0) < need:
            errs.append(f"本模式需要 {need} 个{zh_kind(kind)}输入,现在有 {by_kind.get(kind, 0)} 个")
    return errs


def compile_inputs(model: str, mode: str, prompt: str,
                   inputs: Sequence[Input]) -> Compiled:
    """把提示词里的 ``{素材名}`` 翻成本模型本模式认的记号,同时产出实发文件顺序。

    ``inputs`` 的顺序 = 用户摆的顺序 = 实发顺序 = 记号编号依据。三者同一次遍历得出。
    """
    inputs = list(inputs)
    canonical = caps.canonical_model(model)
    out = Compiled(prompt=prompt)

    slot = caps.mode_slot(canonical, mode)
    if slot is None:
        out.errors.append(f"{canonical} 不支持「{caps.mode_label(mode)}」")
        return out

    grammar = caps.model_grammar(canonical)
    kinds_allowed = list(slot.get("kinds") or [])
    scope = str(grammar.get("scope") or "none")
    start = int(grammar.get("start") or 0)

    # 1) 重名会让 {名字} 指向歧义 —— 先拦,否则后面的映射本身就是错的。
    seen_names: dict[str, int] = {}
    for r in inputs:
        seen_names[r.name] = seen_names.get(r.name, 0) + 1
    for name, count in seen_names.items():
        if count > 1:
            out.errors.append(f"输入名「{name}」重复了 {count} 次,{{{name}}} 会指向哪个说不清 —— 请改成不同的名字")

    # 2) 类型准入(按本模式,不按模型)。
    if inputs and not kinds_allowed:
        out.errors.append(f"「{caps.mode_label(mode)}」不接受输入文件,现在给了 {len(inputs)} 个")
    for r in inputs:
        if kinds_allowed and r.kind not in kinds_allowed:
            allowed = "/".join(zh_kind(k) for k in kinds_allowed)
            out.errors.append(f"输入「{r.name}」是{zh_kind(r.kind)},本模式只收{allowed}")

    # 3) 数量与配额。
    min_total = int(slot.get("min_total") or 0)
    if len(inputs) < min_total:
        alt = slot.get("alt_param")
        hint = f";或改用 {alt}" if alt else ""
        out.errors.append(f"「{caps.mode_label(mode)}」至少需要 {min_total} 个输入,现在有 {len(inputs)} 个{hint}")
    max_total = slot.get("max_total")
    if max_total == 0 and inputs:
        pass   # 已在第 2 步用更好懂的话报过
    if inputs:
        out.errors.extend(_quota_errors(slot, inputs))

    # 4) 必备类型 —— 只在本模式真的要求时才判(edit 不要求图片,reference 才要求)。
    for need in (slot.get("required_kinds") or []):
        if inputs and not any(r.kind == need for r in inputs):
            out.errors.append(f"「{caps.mode_label(mode)}」必须至少包含 1 个{zh_kind(need)}输入")

    # 5) 尺寸硬限(happyhorse:输入图小于 300x300 会被上游拒)。
    min_px = slot.get("min_pixels")
    if min_px:
        for r in inputs:
            if r.kind == "image" and r.pixels and (r.pixels[0] < min_px[0] or r.pixels[1] < min_px[1]):
                out.errors.append(
                    f"输入「{r.name}」是 {r.pixels[0]}x{r.pixels[1]},小于本模型要求的 {min_px[0]}x{min_px[1]}")

    # 6) 编号 + 文件顺序 —— 同一次遍历得出,不允许在别处重算。
    counters: dict[str, int] = {}
    for idx, r in enumerate(inputs):
        out.files.append(r.ref)
        out.order.append({"name": r.name, "media_type": r.kind, "index": idx})
        if scope == "flat":
            n = start + idx
        elif scope == "per_kind":
            n = start + counters.get(r.kind, 0)
            counters[r.kind] = counters.get(r.kind, 0) + 1
        else:
            continue          # 无记号语法:顺序仍是隐含契约,但没有记号可写
        token = _token_for(grammar, r.kind, n)
        if token:
            out.tokens[r.name] = token

    # 7) 提示词引用了不存在的输入 —— 拦住,别让模型收到一个没人给的记号。
    referenced = PLACEHOLDER.findall(prompt)
    known = {r.name for r in inputs}
    for missing in sorted(set(referenced) - known):
        out.errors.append(f"提示词里引用了 {{{missing}}},但没有这个输入")
    for unused in sorted(known - set(referenced)):
        out.warnings.append(f"输入「{unused}」登记了但提示词里没提到它")

    # 8) 替换。无记号模型退化成把名字用书名号括起来的自然语言 —— 顺序仍是隐含契约。
    def _sub(match: re.Match) -> str:
        name = match.group(1)
        if name not in known:
            return match.group(0)          # 未知的原样保留,已在第 7 步报红
        return out.tokens.get(name) or f"「{name}」"

    out.prompt = PLACEHOLDER.sub(_sub, prompt)
    return out
