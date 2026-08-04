"""视频编辑再生成的 Provider / 引导方式契约。

这是单工具业务语义，按 ZE Skill Build 的 P-2/P-3 留在 ``02-mod``：
编辑器只产生统一的画面引导，提交时再选择模型和编译方式。
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable


ANNOTATED_VIDEO = "annotated_video"
MODIFIED_FRAMES = "modified_frames"

OMNI = "omni"
SEEDANCE = "seedance"

OMNI_MAX_INPUT_DURATION = 10.0
SEEDANCE_MIN_OUTPUT_DURATION = 4
SEEDANCE_MAX_OUTPUT_DURATION = 15


@dataclass(frozen=True)
class ProviderCapability:
    provider: str
    strategies: tuple[str, ...]
    verified_strategies: tuple[str, ...]


CAPABILITIES: dict[str, ProviderCapability] = {
    # 产品不提前裁剪组合；真实服务是否接受由运行时响应回答。
    OMNI: ProviderCapability(
        OMNI,
        (ANNOTATED_VIDEO, MODIFIED_FRAMES),
        (),
    ),
    SEEDANCE: ProviderCapability(
        SEEDANCE,
        (ANNOTATED_VIDEO, MODIFIED_FRAMES),
        (),
    ),
}


def infer_provider(model: str) -> str:
    value = str(model or "").strip().lower()
    return SEEDANCE if value.startswith("seedance") else OMNI


def normalize_profile(*, provider: str | None, model: str,
                      strategy: str | None, legacy_mode: Any = None) -> dict[str, str]:
    """兼容旧 ``mode`` 配置并返回明确的三元组。"""
    chosen_provider = str(provider or "").strip() or infer_provider(model)
    if strategy:
        chosen_strategy = str(strategy)
    else:
        chosen_strategy = MODIFIED_FRAMES if int(legacy_mode or 1) == 2 else ANNOTATED_VIDEO
    validate_profile(chosen_provider, model, chosen_strategy)
    return {
        "provider": chosen_provider,
        "modelId": str(model or "").strip(),
        "strategy": chosen_strategy,
    }


def validate_profile(provider: str, model: str, strategy: str) -> None:
    if provider not in CAPABILITIES:
        raise ValueError(f"未知模型提供方: {provider}")
    if strategy not in CAPABILITIES[provider].strategies:
        raise ValueError(f"{provider} 不支持 {strategy}")
    inferred = infer_provider(model)
    if str(model or "").strip() and inferred != provider:
        raise ValueError(f"模型 {model} 不属于 {provider}")


def service_mode(provider: str, strategy: str) -> str:
    validate_profile(provider, "", strategy)
    if provider == OMNI:
        return "edit"
    return "reference-to-video"


def plan_submission_groups(
    *,
    provider: str,
    total_duration: float,
    focus_groups: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Choose the exact timeline windows shared by prompt optimization and generation.

    If the current video fits the provider, submit it whole.  Only an overlong
    video is clipped around its guide-derived focus; clipped windows are padded
    to a whole-second duration within the provider range.
    """
    total = float(total_duration)
    if total <= 0:
        raise ValueError("当前视频时长必须大于 0 秒")
    if provider == OMNI:
        minimum, maximum = 1, int(OMNI_MAX_INPUT_DURATION)
    elif provider == SEEDANCE:
        minimum, maximum = SEEDANCE_MIN_OUTPUT_DURATION, SEEDANCE_MAX_OUTPUT_DURATION
        if total < minimum - 1e-6:
            raise ValueError(
                f"Seedance 提交视频至少需要 {minimum} 秒，当前视频为 {total:.2f} 秒"
            )
    else:
        raise ValueError(f"未知模型提供方: {provider}")

    source_groups = [dict(group) for group in focus_groups]
    numbers = sorted({
        int(number)
        for group in source_groups
        for number in (group.get("numbers") or [])
    })
    if total <= maximum + 1e-6:
        return [{
            "start": 0.0,
            "end": round(total, 3),
            "numbers": numbers,
            "full": True,
        }]
    if not source_groups:
        raise ValueError("没有可用于截取的画面引导区间")

    planned: list[dict[str, Any]] = []
    for group in source_groups:
        start = max(0.0, float(group.get("start") or 0))
        end = min(total, float(group.get("end") or 0))
        if end <= start:
            continue
        aligned_start = math.floor(start + 1e-9)
        whole_seconds = max(minimum, math.ceil(end - 1e-9) - aligned_start)
        if whole_seconds > maximum:
            provider_label = "Seedance" if provider == SEEDANCE else "Omni"
            action = "生成" if provider == SEEDANCE else "处理"
            raise ValueError(
                f"{provider_label} 单次最多{action} {maximum} 秒，"
                f"当前画面引导需要覆盖 {end - start:.2f} 秒；请拆成多个时段"
            )
        planned_start = float(aligned_start)
        planned_end = planned_start + whole_seconds
        if planned_end > total:
            planned_end = total
            planned_start = total - whole_seconds
        planned.append({
            "start": round(planned_start, 3),
            "end": round(planned_end, 3),
            "numbers": sorted({int(value) for value in (group.get("numbers") or [])}),
            "full": False,
        })
    if not planned:
        raise ValueError("算不出符合模型时长要求的提交区间")
    return planned


def compile_submission_request(*, provider: str, model: str, strategy: str,
                               ratio: str, span_duration: float) -> dict[str, Any]:
    """把统一编辑意图编译成目标模型真正接受的请求字段。

    画面合成与回贴仍共用一条链；这里只分流模型协议：
    - Omni edit 的输入片段最多 10s，且不能传 ``duration`` / ``aspect_ratio``；
    - Seedance reference-to-video 的输入片段最多 15s，输出时长明确为 4–15s。

    超过单次上限时拒绝整段，不静默截断，避免用户选中的修改范围丢失。
    """
    validate_profile(provider, model, strategy)
    seconds = float(span_duration)
    if seconds <= 0:
        raise ValueError("本次修改区间必须大于 0 秒")

    if provider == OMNI:
        if seconds > OMNI_MAX_INPUT_DURATION + 1e-6:
            raise ValueError(
                f"Omni 单次最多处理 {OMNI_MAX_INPUT_DURATION:g} 秒，"
                f"当前修改区间为 {seconds:.2f} 秒；请缩短画面引导时段或改用 Seedance"
            )
        return {"mode": service_mode(provider, strategy)}

    if seconds > SEEDANCE_MAX_OUTPUT_DURATION + 1e-6:
        raise ValueError(
            f"Seedance 单次最多生成 {SEEDANCE_MAX_OUTPUT_DURATION} 秒，"
            f"当前修改区间为 {seconds:.2f} 秒；请把修改拆成多个时段"
        )
    duration = max(
        SEEDANCE_MIN_OUTPUT_DURATION,
        min(int(math.ceil(seconds - 1e-9)), SEEDANCE_MAX_OUTPUT_DURATION),
    )
    return {
        "mode": service_mode(provider, strategy),
        "ratio": str(ratio or "16:9"),
        "duration": duration,
    }


def file_tokens(provider: str, count: int) -> list[str]:
    """按实际上传顺序返回提示词中的文件记号。"""
    if provider == SEEDANCE:
        return [f"@file_{i}" for i in range(max(0, count))]
    return [f"文件{i + 1}" for i in range(max(0, count))]


def decorate_prompt(prompt: str, *, provider: str, strategy: str,
                    frame_times: Iterable[float] = ()) -> str:
    """把传输事实写进提示词，不改变用户原意。"""
    text = str(prompt or "").strip()
    if strategy == ANNOTATED_VIDEO:
        token = file_tokens(provider, 1)[0]
        suffix = (
            f"{token} 是带有临时画面引导的视频。请按其中的素材、框选、文字和箭头修改，"
            "没有引导的画面保持不变。"
        )
    else:
        times = list(frame_times)
        tokens = file_tokens(provider, 1 + len(times))
        refs = "；".join(
            f"{tokens[i + 1]} 对应原视频 {at:.3f}s 的修改画面"
            for i, at in enumerate(times)
        )
        suffix = (
            f"{tokens[0]} 是未经画面引导修改的原视频。{refs}。"
            "参考画面只约束对应时间附近的修改，其余内容、运动和光影保持连续。"
        )
    return f"{text}\n\n{suffix}".strip()


def public_capability_matrix() -> dict[str, Any]:
    return {
        provider: {
            "strategies": list(cap.strategies),
            "verifiedStrategies": list(cap.verified_strategies),
        }
        for provider, cap in CAPABILITIES.items()
    }
