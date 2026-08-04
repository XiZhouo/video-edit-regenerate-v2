"""ZE-video-edit-studio 业务配置旋钮.

只声明本 skill 特有的旋钮;框架的 ze.llm.* / ze.image.* / ze.ai.* 由 seed_framework_config
播种。视频/图片服务地址走 env(skill 内不落 key)。
"""

from __future__ import annotations

from sdk import ConfigRegistry


def register(registry: ConfigRegistry) -> None:
    registry.declare("edit.auto_adopt", "bool", default=True,
                     label="空段首结果自动采纳",
                     desc="空段的首个成功结果自动采纳(可撤销,重生成永不自动)。",
                     group="Video Edit Studio")
    registry.declare("edit.default_padding", "float", default=0.5,
                     label="截段 padding(秒)",
                     desc="发送区间前后各外扩的秒数;未滚动的单帧区间靠它撑开成可发段。",
                     group="Video Edit Studio")
    registry.declare("edit.default_strategy", "enum", default="video-edit-v1",
                     choices=("video-edit-v1", "general-v1", "ad-short"),
                     label="默认提示词策略",
                     desc="video-edit-v1 按时间线/画面引导优化;general-v1 通用;ad-short 广告短片。",
                     group="Video Edit Studio")
    registry.declare("edit.default_model", "text", default="gemini-omni-flash-preview",
                     label="默认模型",
                     desc="模式一(标注入视频)仅 gemini-omni 支持 edit;模式二/三走参考视频模型池。",
                     group="Video Edit Studio")
