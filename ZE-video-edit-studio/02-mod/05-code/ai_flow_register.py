"""02-mod/05-code/ai_flow_register.py — ZE-video-edit-studio AI 接口声明.

Context.Describe(server API 与 scripts/ze_command.py describe 同形)据此产出对象图与
可执行命令。编辑器映射是 04-layout.config.ts 的 Python 镜像。
入口约定:register(meta: sdk.IntrospectionMeta) -> None。

⚠️ 这个文件是**给 AI 看的唯一入口**,它说的命令必须真的注册着。上一版这里写着
send.dispatch / candidate.verify / composite.mask_merge / candidate.adopt 四条,
而它们随候选制一起被删了 —— AI 照着调会在第二步就断链,且永远不知道该调 edit.send。
改命令面就必须回来改这里。
"""

from __future__ import annotations


def register(meta) -> None:
    meta.set_ai_flow({
        "summary": "视频修图:时间线是一摞图层(视频片段是图层,标注也是图层,每行可隐藏)。"
                   "在画面上圈一块 + 说一句要改成什么 → 把「可见图层 × 选定区间」合成一段、"
                   "把标注烧进画面 → 发给会视频编辑的模型 → 结果作为新图层插到最上面。",
        "upstream": [
            "导入源视频为 asset(asset.import;真人/版权走 seedanceAssetId)",
            "clip:一行一个视频图层,layer 越大越上,visible 决定参不参与合成",
        ],
        "steps": [
            "annotation.add: 建一条标注图层 —— bbox(项目画布坐标)+ start/duration"
            "(duration=0 即单帧)+ label(要改成什么)。编号自动顺延,颜色按编号从"
            "调色板取,与烧进画面的框色、提示词里说的色名同一个锚。",
            "entity.update: 改标注的 bbox / start / duration / label / visible。"
            "关掉 visible = 这条既不烧进画面、也不进提示词。",
            "guide.keyframe.set: 对矩形或图片，在当前画面记录位置与大小。"
            "记录多个位置后，画面与实际提交视频都会在相邻位置间线性移动。",
            "guide.keyframe.remove: 删除一个已记录的位置。",
            "edit.send: 主动作(慢)。参数 {start, end, prompt, model}。"
            "把区间内可见图层合成一段 → 烧上区间内可见标注 → 发 gemini-omni → "
            "结果落成新 clip(layer 取最上面)。模型不在真值表里会被直接拒。",
            "annotation.remove: 删标注并把剩余编号/颜色一起重排。",
            "send.compile: 可选的审查快照(不派发),按 send_profile 编译实发提示词/"
            "文件序/渲染面,落 send_job。要先看再发时用它。",
        ],
        "downstream": [
            "结果图层交 ③ 剪辑装配(handoff),或导出",
        ],
        "artifacts": [
            "clip(生成结果就是一个新图层,xKind=gen;不覆盖任何东西)",
            "send_job(可审计的实发快照,含 promptSnapshot)",
        ],
    })
    meta.set_manual(
        "# ZE Video Edit Studio · 说明书\n\n"
        "视频「修图」:片子整体是对的、具体画面有错/要换时用它(错字、错物料、去元素、局部换色换装)。\n\n"
        "## 时间线是一摞图层\n"
        "一行 = 一个图层。视频片段是图层,**标注也是图层** —— 它在时间线上占一行、有自己的起止、\n"
        "播放到点出现过点消失。每行左边一只眼睛,关掉就不参与:视频层不参与合成,标注层不烧进画面、\n"
        "也不进提示词。关掉最上面那层视频,看到的就是下面的原片 —— 这就是对比,没有另一套候选制。\n\n"
        "## 三步走\n"
        "1. **挑工具**:画布默认是「选择」——点框选中、拖框移动、拖角改大小。要圈新的一块,\n"
        "   先按 R(或点工具栏第二个)切到「矩形标注」。\n"
        "2. **圈一块 + 定一段**:按下拖框(空间)→ 未松手滚轮前后擦洗(3帧/格,Shift=1帧)\n"
        "   划出时间区间 → 松手落定,时间线上多一行。没滚过轮就是单帧,之后拖条子两端可以撑开。\n"
        "3. **发**:在时间尺上拖出要发的那一段,右侧核对「这次会发什么」(区间/层数/标注/模型),\n"
        "   写一句要改成什么,发送。结果回来是新的一层,盖在最上面。不要就删掉那一行。\n\n"
        "## 边界\n"
        "- 整段内容要重来→② 段重生成;纯剪辑排列→③;批量出片→④。\n"
        "- 全世界只有 gemini-omni 会视频编辑,所以模型没得挑(真值表说了算)。\n"
        "- 矩形与图片可在不同关键画面记录位置与大小，之间自动线性移动；文字与路径暂为静态。\n"
        "- 真人/名人名会被安全改写层替换为外观描述并显示 diff。\n"
    )
    # 与 04-layout.config.ts 逐槽对齐:三个 custom 面板。
    meta.declare_editors([
        {"slot": "ves-canvas", "editor_class": "custom",
         "bind": ["clip", "annotation", "guide_keyframe", "asset", "project"],
         "read_sources": ["clip", "annotation", "guide_keyframe", "asset", "project"],
         "view_state": {"rendererKey": "layerCanvas"}},
        {"slot": "ves-timeline", "editor_class": "custom",
         "bind": ["clip", "annotation", "guide_keyframe", "reference_frame", "asset"],
         "read_sources": ["clip", "annotation", "guide_keyframe", "reference_frame", "asset"],
         "view_state": {"rendererKey": "layerTimeline"}},
        {"slot": "ves-console", "editor_class": "custom",
         "bind": ["send_profile", "clip", "annotation", "send_job", "project", "asset",
                  "export"],
         "read_sources": ["send_profile", "clip", "annotation", "send_job",
                          "project", "asset", "export"],
         "view_state": {"rendererKey": "editConsole"}},
    ])
