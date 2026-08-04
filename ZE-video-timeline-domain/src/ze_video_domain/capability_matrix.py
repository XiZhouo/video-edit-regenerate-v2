"""模型能力 + 引用语法真值表 —— **数据从服务采集,不手抄**。

真相来源:视频服务的 ``/discover``,由仓根的 ``sync_capability_matrix.py`` 采集成
同目录的 ``capability_matrix.json``。加一个模型 = 重跑一次同步,本文件不动。

## 形状(与旧版最大的区别)

约束**按模式**存,不按模型存::

    models[id].ref_grammar    只有记号写法/起始序号/按不按类型计数(真正与模式无关)
    models[id].slots[mode]    准入类型、必备类型、数量上下限、尺寸下限、替代参数
    models[id].params         画幅比/时长/分辨率 —— 换模型时判断参数还合不合法

为什么较真这件事:服务返回的 ``limits`` 本来就是按模式加前缀的
(``reference_*`` 属于多参考、``edit_*`` 属于视频编辑)。把它们压平成模型级,
会造成一类很隐蔽的错 —— **拿 A 模式的限制去卡 B 模式**。最典型的是 gemini-omni:
多参考模式确实「必须至少一张图」,但编辑模式是「正好 1 个视频,图片可选」;
压平之后「一个待编辑视频 + 一句话」这个最标准的用法会被自己拦死。

## 能不能用某个模型,是三件事的交集

    这一步要的**能力** ∩ 这一步必须上传的**媒体类型** ∩ **配额**装得下

只看能力会高估。例如「参考图 + 原视频改视频」这一步:会多参考的有 10 个模型,
但必须把原视频送上去,而收视频的只有 5 个 —— 真答案是 5。
:func:`models_for_step` / :func:`models_for_mode` 按完整公式算。

## 记号家族(第 N 个输入怎么称呼)

- ``file0``    Seedance:``@file_0``、``@file_1`` …(0 起,所有输入一条队)
- ``image1``   Grok:``@Image1`` …(1 起,按类型计数)
- ``bracket1`` HappyHorse:``[Image 1]`` …(1 起,按类型计数)
- ``angle1``   Kling-omni:``<<<image_1>>>`` / ``<<<video_1>>>``(1 起,**按类型分别计数**)
- ``natural``  Veo / Kling-v3 / Gemini-omni:没有记号,靠句子描述
"""
from __future__ import annotations

import json
import os
from typing import Any, Iterable, Mapping

_MATRIX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "capability_matrix.json")

with open(_MATRIX_PATH, encoding="utf-8") as _fh:
    MATRIX: dict[str, Any] = json.load(_fh)

MODES: tuple[str, ...] = tuple(MATRIX.get("modes") or ())
ALIASES: dict[str, str] = dict(MATRIX.get("aliases") or {})
_SPECS: dict[str, Any] = dict(MATRIX.get("models") or {})

MODE_LABELS = {
    "text-to-video": "文生视频",
    "image-to-video": "图生视频(首帧)",
    "keyframe-to-video": "首尾帧",
    "reference-to-video": "多参考",
    "edit": "视频编辑",
}

_FAMILY_BY_TOKEN = {
    "@file_{n}": "file0",
    "@Image{n}": "image1",
    "[Image {n}]": "bracket1",
    "<<<{kind}_{n}>>>": "angle1",
}

_CAP_FLAG = {
    "text-to-video": "t2v",
    "image-to-video": "i2v",
    "keyframe-to-video": "kf2v",
    "reference-to-video": "ref",
    "edit": "edit",
}


def _media_union(spec: Mapping[str, Any]) -> tuple[str, ...]:
    kinds: list[str] = []
    for slot in (spec.get("slots") or {}).values():
        for kind in slot.get("kinds") or []:
            if kind not in kinds:
                kinds.append(kind)
    return tuple(kinds)


def _legacy_entry(spec: Mapping[str, Any]) -> dict[str, Any]:
    caps_ = set(spec.get("capabilities") or [])
    grammar = spec.get("ref_grammar") or {}
    ratios = (spec.get("params") or {}).get("aspect_ratios")
    return {
        "t2v": "text-to-video" in caps_,
        "i2v": "image-to-video" in caps_,
        "kf2v": "keyframe-to-video" in caps_,
        "ref": "reference-to-video" in caps_,
        "edit": "edit" in caps_,
        "syntax": _FAMILY_BY_TOKEN.get(grammar.get("token") or "", "natural"),
        "media_ref": _media_union(spec),
        "ratios": tuple(ratios) if ratios else None,
    }


# 旧形状视图(既有调用方与测试依赖它)。由 JSON 派生 —— 只有一份真相。
MODELS: dict[str, dict[str, Any]] = {mid: _legacy_entry(spec) for mid, spec in _SPECS.items()}


# --- 基础查询 ----------------------------------------------------------------

def canonical_model(model: str) -> str:
    """把别名或规范 id 解析成规范 id。"""
    if model in _SPECS:
        return model
    if model in ALIASES and ALIASES[model] in _SPECS:
        return ALIASES[model]
    raise ValueError(f"unknown model {model!r}")


def model_spec(model: str) -> dict[str, Any]:
    return _SPECS[canonical_model(model)]


def model_grammar(model: str) -> dict[str, Any]:
    """记号写法。**只有与模式无关的部分** —— 配额和必备类型不在这里。"""
    return dict(model_spec(model).get("ref_grammar") or {})


def mode_label(mode: str) -> str:
    return MODE_LABELS.get(mode, mode)


def mode_slot(model: str, mode: str) -> dict[str, Any] | None:
    """本模型在**这个模式**下的输入位规格;不支持该模式则 ``None``。"""
    slot = (model_spec(model).get("slots") or {}).get(mode)
    return dict(slot) if slot is not None else None


def modes_for_model(model: str) -> list[str]:
    return [m for m in MODES if m in (model_spec(model).get("slots") or {})]


def model_supports(model: str, capability: str) -> bool:
    """本模型会不会 ``capability``(text-to-video / edit / …)。"""
    if capability not in _CAP_FLAG:
        raise ValueError(f"unknown capability {capability!r}; expected one of {tuple(_CAP_FLAG)}")
    return capability in (model_spec(model).get("capabilities") or [])


def models_for_capability(capability: str) -> list[str]:
    """会 ``capability`` 的规范 id(稳定排序)。

    **只看能力,不看媒体类型** —— 要判断「这一步能不能用」请用 :func:`models_for_step`。
    """
    if capability not in _CAP_FLAG:
        raise ValueError(f"unknown capability {capability!r}")
    return sorted(mid for mid in _SPECS if capability in (_SPECS[mid].get("capabilities") or []))


def model_accepts_media(model: str, media_type: str) -> bool:
    """本模型**在任意模式下**收不收这个媒体类型(粗筛)。

    逐模式判断请用 :func:`mode_accepts_media`。
    """
    return media_type in MODELS[canonical_model(model)]["media_ref"]


def mode_accepts_media(model: str, mode: str, media_type: str) -> bool:
    slot = mode_slot(model, mode)
    return bool(slot) and media_type in (slot.get("kinds") or [])


def syntax_family(model: str) -> str:
    return MODELS[canonical_model(model)]["syntax"]


def ref_token(model: str, index: int, media_type: str = "image") -> str | None:
    """本模型下第 ``index`` 个输入的记号(``index`` 是**它所在计数域内**的 0 基序号)。

    计数域由 ``ref_grammar.scope`` 决定:``flat`` 时是所有输入排一条队的序号;
    ``per_kind`` 时是**同类型输入之间**的序号 —— kling 的 ``<<<image_1>>>`` 与
    ``<<<video_1>>>`` 各自从 1 起,同一批里出现两个「1」是正常的。
    自然语言模型返回 ``None``。

    ⚠️ 单独调用很容易把计数域搞错(把「第 2 个文件」当成「第 2 个视频」)。
    正常路径请用 ``ref_binding.compile_inputs`` —— 那里记号、顺序、文件是同一次遍历产出的。
    """
    grammar = model_grammar(model)
    template = grammar.get("token")
    if not template:
        return None
    start = int(grammar.get("start") or 0)
    kind = "video" if media_type == "video" else "image"
    return str(template).replace("{kind}", kind).replace("{n}", str(start + index))


# --- 「这一步能用哪些模型」= 能力 ∩ 媒体 ∩ 配额 --------------------------------

def _fits_quota(slot: Mapping[str, Any], count: int, need: Iterable[str]) -> bool:
    """只按**总数**判配额。逐类型上限需要知道每个输入是什么,那是
    ``ref_binding.compile_inputs`` 的活 —— 这里不做半吊子归因。"""
    max_total = slot.get("max_total")
    if max_total is not None and count > max_total:
        return False
    if "video" in need:
        with_video = slot.get("max_total_with_video")
        if with_video is not None and count > with_video:
            return False
    if count < int(slot.get("min_total") or 0) and not slot.get("alt_param"):
        return False
    return True


def models_for_step(mode: str, *, required_kinds: Iterable[str] = (),
                    input_count: int | None = None) -> list[str]:
    """能跑「``mode`` 这一步,而且收得下这些输入」的模型(稳定排序)。

    ``required_kinds``  这一步**必须**送上去的媒体类型(如「参考图 + 原视频」= image, video)
    ``input_count``     这一步会送几个输入;给了就一并判总数配额
    """
    need = list(required_kinds)
    out: list[str] = []
    for mid in sorted(_SPECS):
        slot = mode_slot(mid, mode)
        if slot is None:
            continue
        if any(k not in (slot.get("kinds") or []) for k in need):
            continue
        if input_count is not None and not _fits_quota(slot, input_count, need):
            continue
        out.append(mid)
    return out


# --- 视频编辑台的三个模式 ------------------------------------------------------

MODE_CAPABILITY = {
    1: "edit",                # 模式一 标注入视频 → 编辑协议(只有 omni)
    2: "reference-to-video",  # 模式二 拆参考图 → 参考图 + 原视频
    3: "reference-to-video",  # 模式三 直发参照
}

# 每个模式**必须**送上去的媒体类型 —— 这一维决定了「会这个能力」≠「这一步能用」。
MODE_INPUT_KINDS: dict[int, tuple[str, ...]] = {
    1: ("video",),            # 烧了标注的视频段
    2: ("image", "video"),    # 参考图(已改好的关键帧) + 原视频段
    3: ("image",),            # 默认纯图参照;参照里含视频时用 extra_kinds 追加
}


def models_for_mode(mode: int, *, extra_kinds: Iterable[str] = ()) -> list[str]:
    """能服务编辑台 ``mode``(1|2|3)的模型。按完整公式算,不只看能力。"""
    if mode not in MODE_CAPABILITY:
        raise ValueError(f"unknown mode {mode!r}")
    kinds = list(MODE_INPUT_KINDS[mode]) + list(extra_kinds)
    return models_for_step(MODE_CAPABILITY[mode], required_kinds=kinds)


# --- 参数(换模型时判断还合不合法)---------------------------------------------

def model_params(model: str) -> dict[str, Any]:
    return dict(model_spec(model).get("params") or {})


def supports_ratio(model: str, ratio: str) -> bool:
    ratios = model_params(model).get("aspect_ratios")
    return True if not ratios else ratio in ratios


def duration_bounds(model: str) -> tuple[float, float] | None:
    """本模型的时长范围;不支持时长参数(如 gemini-omni)返回 ``None``。"""
    params = model_params(model)
    rng = params.get("duration_range")
    if isinstance(rng, (list, tuple)) and len(rng) == 2:
        return float(rng[0]), float(rng[1])
    options = params.get("duration_options")
    if isinstance(options, (list, tuple)) and options:
        return float(min(options)), float(max(options))
    return None


def duration_options(model: str) -> list[float] | None:
    """只接受离散时长的模型(veo 4/6/8、grok 6/8)返回可选值,否则 ``None``。"""
    options = model_params(model).get("duration_options")
    return [float(v) for v in options] if isinstance(options, (list, tuple)) and options else None
