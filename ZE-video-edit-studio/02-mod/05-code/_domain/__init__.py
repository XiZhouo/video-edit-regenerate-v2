"""ze_video_domain — shared managed domain layer for the ZE video quartet.

Framework-agnostic domain logic (schema, engines, compiler, strategy,
persistence) that is synced (as a managed layer) into each quartet skill's
``02-mod/05-code/_domain/``. It imports NO build-v3 sdk and does NO network/IO
except through the explicit ffmpeg / videogen seams, so it is fully unit-testable
and relocatable.

Contract version is ``DOMAIN_VERSION`` — bump on any schema/API change; the sync
tool records it in each consumer's version lock.
"""

from __future__ import annotations

# 0.11.3:video-edit-v1 增加代表帧事实槽位。
# 0.11.2:烧框与 send compiler 统一读取持久化 guide color。
# 0.11.1:回流编辑台黑/白/红画面引导调色板与 annotation_paint 单一映射。
# 0.11.0:新增 video-edit-v1 提示词策略与紧凑结构化输出契约。
# 0.3.0:新增 layer_compose —— 时间线是一摞图层(上层盖下层、隐藏的不参与),
#        编辑 = 可见图层×选定区间合成一段发出去,结果作为新图层插最上面。
#        这替掉了原来的「候选列表+采纳+回贴策略」那一套。
# 0.2.0:真值表改为从服务 /discover 机器采集、按**模式**存;新增 ref_binding
# (记号/顺序/文件一次编译整体产出);能力不匹配由「警告」升级为「拦」。
DOMAIN_VERSION = "0.11.3"
SCHEMA_ID = "ze.video_project.v1"

from . import (  # noqa: E402  (re-export surface)
    capability_matrix,
    candidate_flow,
    clip_math,
    errors,
    ffmpeg_ops,
    clip_geometry,
    layer_compose,
    render,
    ref_binding,
    safety_rewrite,
    schema,
    send_compiler,
    verify_report,
    videogen_client,
)

__all__ = [
    "DOMAIN_VERSION",
    "SCHEMA_ID",
    "capability_matrix",
    "candidate_flow",
    "clip_math",
    "errors",
    "ffmpeg_ops",
    "clip_geometry",
    "layer_compose",
    "render",
    "ref_binding",
    "safety_rewrite",
    "schema",
    "send_compiler",
    "verify_report",
    "videogen_client",
]
