"""ZE-video-edit-studio 的 Repository 级资源闸门声明.

Repository 加载一次,注入每个 project Supervisor;StageSpec.resource_scopes 因而
把视频服务在途任务做成真实跨 project 的单进程硬上限(与 ②③ 同源纪律)。
"""

from __future__ import annotations

from sdk import GateRegistry, ResourceContract


def register(gate_registry: GateRegistry) -> None:
    gate_registry.get_or_create(ResourceContract(
        resource="提示词优化模型",
        scope="api.llm",
        max_concurrency=2,
        rate_per_sec=1.0,
        enforcement_scope="process",
        on_violation="wait",
    ))
    gate_registry.get_or_create(ResourceContract(
        resource="视频生成服务",
        scope="api.videogen",
        max_concurrency=2,
        rate_per_sec=1.0,
        enforcement_scope="process",
        on_violation="wait",
    ))
    gate_registry.get_or_create(ResourceContract(
        resource="ffmpeg 重编码",
        scope="cpu.ffmpeg",
        max_concurrency=2,
        enforcement_scope="process",
        on_violation="wait",
    ))
    gate_registry.get_or_create(ResourceContract(
        resource="图片去背景",
        scope="cpu.image",
        max_concurrency=1,
        enforcement_scope="process",
        on_violation="wait",
    ))
