"""02-mod/05-code/document_register.py — ZE-video-edit-studio 文档声明.

① 视频编辑台("视频修图"):画面框选 + 滚轮定区间 → AI 按标注精确修改。

实体 schema 对齐共享域层 ze.video_project.v1(``_domain/schema.py``),但以 build-v3
JSON-schema 形态声明(供 FormEditor 渲染)。复杂结构(bbox/interval/快照)以扁平数值
字段或 JSON 字符串承载(沿用 ③ timeline-editor 的 keyframesJson 约定)。命令层在调用
域层 send_compiler 时把扁平字段组装回域实体形状。

入口:register(registry: DocumentRegistry) -> None(见 05-code/README.md)。
"""

from __future__ import annotations

from pathlib import Path

from sdk import DocumentRegistry, Entity

from _domain import ffmpeg_ops


def _drop_none(data: dict) -> dict:
    """框架对已声明类型的字段拒 None;缺 demo 素材时直接不写这个键。"""
    return {k: v for k, v in data.items() if v is not None}

# 实体 schema 是共享域层语义在 UI 层的投影;schema 层(_domain.schema)是语义真源。

PROJECT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "label": "项目名"},
        "width": {"type": "number", "minimum": 16, "maximum": 8192, "label": "画布宽"},
        "height": {"type": "number", "minimum": 16, "maximum": 8192, "label": "画布高"},
        "fps": {"type": "number", "minimum": 1, "maximum": 120, "label": "帧率"},
        "background": {"type": "string", "label": "画布底色(留边处的颜色)"},
        "globalContext": {"type": "string", "label": "全局背景(风格/基调)"},
    },
}

ASSET_SCHEMA = {
    "type": "object",
    "properties": {
        "assetType": {"type": "string", "enum": ["video", "image", "audio"], "label": "类型",
                      "x-group": "素材"},
        "assetRef": {"type": "asset_ref", "label": "素材文件", "x-group": "引用"},
        "processedRef": {"type": "asset_ref", "label": "处理后的透明素材", "x-group": "引用"},
        "sourcePath": {"type": "string", "label": "源文件路径", "x-group": "引用"},
        "duration": {"type": "number", "minimum": 0, "maximum": 36000, "label": "时长(秒)",
                     "x-group": "素材"},
        "width": {"type": "number", "label": "宽", "x-group": "素材"},
        "height": {"type": "number", "label": "高", "x-group": "素材"},
        "fps": {"type": "number", "label": "帧率", "x-group": "素材"},
        "seedanceAssetId": {"type": "string", "label": "Seedance 资产 id(真人/版权素材)",
                            "x-group": "引用"},
        "contentHash": {"type": "string", "label": "内容哈希(去重)", "x-group": "引用"},
        "placeholder": {"type": "boolean", "label": "占位(待上传)", "x-group": "素材"},
        "cutoutStatus": {
            "type": "string",
            "enum": ["idle", "queued", "succeeded", "failed", "requires_runtime"],
            "label": "去背景状态",
            "x-group": "素材",
        },
        "cutoutMethod": {"type": "string", "label": "去背景方式", "x-group": "素材"},
        "cutoutError": {"type": "string", "label": "去背景说明", "x-group": "素材"},
    },
}


CLIP_SCHEMA = {
    "type": "object",
    "properties": {
        # 时间线是一摞图层:一行 = 一个片段。layer 越大越上,上层盖下层;
        # visible 决定这一行参不参与合成 —— 关掉它,看到的就是下面那层(这就是「对比」)。
        "layer": {"type": "number", "minimum": 0, "label": "层序(越大越上)", "x-group": "图层"},
        "visible": {"type": "boolean", "label": "显示", "x-group": "图层"},
        "assetId": {"type": "string", "label": "素材", "x-group": "引用"},
        "start": {"type": "number", "minimum": 0, "maximum": 36000, "label": "起始(秒)",
                  "x-group": "时间"},
        "duration": {"type": "number", "minimum": 0.2, "maximum": 36000, "label": "时长(秒)",
                     "x-group": "时间"},
        "sourceIn": {"type": "number", "minimum": 0, "maximum": 36000, "label": "源入点(秒)",
                     "x-group": "时间"},
        # 几何:这一层怎么摆进画布。画布是输出规格,素材尺寸通常不等于它。
        # 缺省(contain/0/1/不裁)= 等比装进去居中留边 = 升级前的行为,老项目画面不变。
        # 位移与裁切都是**比例**不是像素 —— 换画布、换素材分辨率,含义不变。
        "fitMode": {"type": "string", "enum": ["contain", "cover", "stretch", "none"],
                    "label": "适配方式", "x-group": "几何"},
        "x": {"type": "number", "minimum": -4, "maximum": 4, "label": "横向位移(画布宽的比例)",
              "x-group": "几何"},
        "y": {"type": "number", "minimum": -4, "maximum": 4, "label": "纵向位移(画布高的比例)",
              "x-group": "几何"},
        "scale": {"type": "number", "minimum": 0.05, "maximum": 8, "label": "缩放",
                  "x-group": "几何"},
        "cropL": {"type": "number", "minimum": 0, "maximum": 0.95, "label": "左裁(比例)",
                  "x-group": "几何"},
        "cropT": {"type": "number", "minimum": 0, "maximum": 0.95, "label": "上裁(比例)",
                  "x-group": "几何"},
        "cropR": {"type": "number", "minimum": 0, "maximum": 0.95, "label": "右裁(比例)",
                  "x-group": "几何"},
        "cropB": {"type": "number", "minimum": 0, "maximum": 0.95, "label": "下裁(比例)",
                  "x-group": "几何"},
        "xKind": {"type": "string", "enum": ["source", "gen", "bridge"], "label": "片段来源",
                  "x-group": "生成"},
        "videoRef": {"type": "asset_ref", "label": "正片", "x-group": "生成"},
        "adoptedCandidateId": {"type": "string", "label": "已采纳候选", "x-group": "生成"},
    },
}

ANNOTATION_SCHEMA = {
    "type": "object",
    "properties": {
        # 标注也是时间线上的一行,而且**跟视频层共用同一根层序数轴**:
        # 视频片段满屏不透明,盖住它下面的一切(下面的视频层、下面的标注框);
        # 标注只是一个框,不盖任何东西。所以某一刻画面 = 最上面那个可见视频层,
        # 画在它上面的框 = 层序比它大的可见标注 —— 看见什么就发送什么。
        "layer": {"type": "number", "minimum": 0, "label": "层序(越大越上)", "x-group": "图层"},
        "visible": {"type": "boolean", "label": "显示", "x-group": "图层"},
        # 内部沿用 annotation 以兼容现有项目，产品界面统一称为「画面引导」。
        "kind": {
            "type": "string",
            "enum": ["box", "text", "image", "arrow"],
            "label": "引导方式",
        },
        "label": {"type": "string", "label": "说明文字"},
        "number": {"type": "number", "label": "编号"},
        "color": {"type": "string", "label": "标注色"},
        "targetClipId": {"type": "string", "label": "建立时最上层是谁", "x-group": "定位"},
        # bbox(项目画布坐标系),扁平四字段
        "bboxX": {"type": "number", "label": "框 X", "x-group": "空间"},
        "bboxY": {"type": "number", "label": "框 Y", "x-group": "空间"},
        "bboxW": {"type": "number", "label": "框宽", "x-group": "空间"},
        "bboxH": {"type": "number", "label": "框高", "x-group": "空间"},
        # 这一行在时间线上占哪一段。duration=0 是合法的单帧标注。
        "start": {"type": "number", "minimum": 0, "label": "起始(秒)", "x-group": "时间"},
        "duration": {"type": "number", "minimum": 0, "label": "时长(秒)", "x-group": "时间"},
        "refImageAssetId": {"type": "string", "label": "参考图(挂图=模式三)", "x-group": "参考"},
        # 箭头/多段路径的项目画布坐标，JSON: [{x,y}, ...]。
        "pointsJson": {"type": "string", "label": "路径点(JSON)", "x-group": "空间"},
    },
}

GUIDE_KEYFRAME_SCHEMA = {
    "type": "object",
    "properties": {
        "parentId": {"type": "string", "label": "所属画面引导"},
        # 相对画面引导 start 的秒数；移动整条引导区间时，内部运动无需重写。
        "time": {"type": "number", "minimum": 0, "maximum": 36000,
                 "label": "关键画面时间(相对秒)"},
        "bboxX": {"type": "number", "label": "位置 X", "x-group": "空间"},
        "bboxY": {"type": "number", "label": "位置 Y", "x-group": "空间"},
        "bboxW": {"type": "number", "minimum": 0, "label": "宽", "x-group": "空间"},
        "bboxH": {"type": "number", "minimum": 0, "label": "高", "x-group": "空间"},
        "easing": {"type": "string", "enum": ["linear"], "label": "变化方式"},
    },
}

REFERENCE_FRAME_SCHEMA = {
    "type": "object",
    "properties": {
        "at": {"type": "number", "minimum": 0, "maximum": 36000,
               "label": "参考画面时间(秒)"},
        "enabled": {"type": "boolean", "label": "启用"},
        "targetClipId": {"type": "string", "label": "目标片段"},
        "label": {"type": "string", "label": "说明"},
    },
}

EXPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string",
                   "enum": ["queued", "succeeded", "failed", "requires_runtime"],
                   "label": "状态"},
        "preset": {"type": "string", "label": "预设"},
        "fileRef": {"type": "asset_ref", "label": "成品文件"},
        "width": {"type": "number", "label": "宽"},
        "height": {"type": "number", "label": "高"},
        "fps": {"type": "number", "label": "帧率"},
        "duration": {"type": "number", "label": "时长(秒)"},
        "lastError": {"type": "string", "label": "最近错误"},
    },
}

SEND_PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {"type": "number", "minimum": 1, "maximum": 3, "label": "模式(1标注入视频/2拆参考图/3直发)"},
        "provider": {"type": "string", "enum": ["omni", "seedance"], "label": "模型提供方"},
        "strategy": {
            "type": "string",
            "enum": ["annotated_video", "modified_frames"],
            "label": "引导方式",
        },
        "model": {"type": "string", "label": "模型"},
        "duration": {"type": "number", "label": "时长(秒)"},
        "ratio": {"type": "string", "label": "画幅比"},
        "paddingSec": {"type": "number", "minimum": 0, "label": "前后 padding(秒)"},
        "targetClipId": {"type": "string", "label": "目标片段(限定标注/回贴范围)"},
    },
}

SEND_JOB_SCHEMA = {
    "type": "object",
    "properties": {
        # queued = 已交给 pipeline、还没轮到跑。edit.send 落的就是这个状态;
        # 少了它,真发一次必抛 schema_violation(这条被上一版的前置校验挡在后面,
        # 测试从来没走到,直到「标注真的发出去」这一批才暴露)。
        "status": {"type": "string",
                   "enum": ["draft", "compiled", "queued", "dispatched", "succeeded",
                            "failed", "cancelled", "requires_runtime"],
                   "label": "状态"},
        "mode": {"type": "number", "label": "模式"},
        "provider": {"type": "string", "label": "模型提供方"},
        "strategy": {"type": "string", "label": "引导方式"},
        "model": {"type": "string", "label": "模型"},
        "targetClipId": {"type": "string", "label": "目标片段(回贴去处)"},
        "promptSnapshot": {"type": "string", "label": "实发提示词(快照)"},
        "imageStepPromptSnapshot": {"type": "string", "label": "图步提示词(模式二·发图片服务)"},
        "blocked": {"type": "boolean", "label": "红线阻断(禁止派发)"},
        "sendIntervalJson": {"type": "string", "label": "截段区间(标注区间并集+padding)"},
        "safetyDiffJson": {"type": "string", "label": "安全改写 diff"},
        "compileWarningsJson": {"type": "string", "label": "编译告警"},
        "pendingAdoptHint": {"type": "string", "label": "采纳建议"},
        "fileOrderJson": {"type": "string", "label": "文件序(JSON 快照)"},
        "renderSurfacesJson": {"type": "string", "label": "渲染面清单(JSON)"},
        "editedFramesJson": {"type": "string", "label": "修改画面时间(JSON)"},
        "taskId": {"type": "string", "label": "上游任务 id"},
        "elapsedSec": {"type": "number", "label": "耗时(秒)"},
        "lastError": {"type": "string", "label": "最近错误"},
    },
}


PROMPT_OPTIMIZATION_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["queued", "succeeded", "failed", "cancelled"],
            "label": "状态",
        },
        "sourcePrompt": {"type": "string", "label": "用户原始说明"},
        "targetModel": {"type": "string", "label": "目标视频模型"},
        "optimizedPrompt": {"type": "string", "label": "优化结果"},
        "warningsJson": {"type": "string", "label": "告警(JSON)"},
        "factsUsedJson": {"type": "string", "label": "采用事实(JSON)"},
        "lastError": {"type": "string", "label": "最近错误"},
    },
}




DEMO_MEDIA = Path(__file__).resolve().parent / "demo_media"


def _demo_ref(filename: str) -> dict | None:
    """把随 skill 一起发的 demo 视频灌进 Repository 共享池,返回它的 AssetRef。

    为什么要真文件:没有它,demo 项目打开就是一块「这一刻没有可见图层」的灰屏 ——
    人看不出这工具在干什么。这两段是 2026-07-21 真跑出来的原片与编辑结果。

    共享池(``<data_dir>/shared_assets``)是每个 project asset_store 的只读 fallback,
    所以灌一次、所有 demo 沙盒都读得到。文件缺位(裁剪发布)就退回占位,不炸。
    """
    path = DEMO_MEDIA / filename
    if not path.is_file():
        return None
    try:
        from sdk.bootstrap import data_dir
        from sdk import AssetStore
        store = AssetStore(data_dir() / "shared_assets")
        return store.put(path.read_bytes(), mime="video/mp4", ext=".mp4").to_dict()
    except Exception:                                   # noqa: BLE001 — demo 素材不该拖垮建项目
        return None


def _seed(doc) -> None:
    """demo 项目 = 2026-07-26 那次真实验的现场:把木地板换成青草地,猫不动。

    两段视频都是真的(gemini-omni-flash-preview 实跑),标注框的坐标就是当时
    烧进画面的那个红框(x 120–1159 / y 258–699,量自 ①-2 的真帧)。

    时间线是一摞图层,开箱就摆好了两层:

        第 1 层  AI 改过的:地板成了青草地   ← 最上面,所以你看到的就是它
        第 0 层  原片:猫走过木地板         ← 一直在下面,没有被覆盖

    把第 1 层的眼睛关掉,画面立刻回到木地板 —— 这就是这个工具的「对比」。
    """
    src_ref = _demo_ref("source.mp4")
    out_ref = _demo_ref("edited.mp4")
    doc.add(Entity(type="project", id="project", name="木地板换青草地",
                   data={"name": "木地板换青草地", "width": 1280, "height": 720,
                         "fps": 24, "globalContext": "室内实拍,橘猫走过木地板,自然光"}))
    doc.add(Entity(type="asset", id="asset-src", name="原片·猫走过木地板",
                   data=_drop_none({"assetType": "video", "duration": 4.0, "width": 1280,
                                    "height": 720, "fps": 24, "assetRef": src_ref,
                                    "placeholder": src_ref is None})))
    doc.add(Entity(type="clip", id="clip-1", name="原片·木地板",
                   data={"assetId": "asset-src", "layer": 0, "visible": True,
                         "start": 0.0, "duration": 4.0, "sourceIn": 0.0, "xKind": "source"}))
    doc.add(Entity(type="annotation", id="ann-1", name="地板换青草地",
                   data={"kind": "box",
                         "label": "把框内的木地板换成青草地,猫、家具与光影保持不变",
                         "number": 1, "color": ffmpeg_ops.annotation_hex(0),
                         # 层序 2 = 压在两层视频(0 原片 / 1 AI 改过)之上,所以看得见
                         "layer": 2, "visible": True, "targetClipId": "clip-1",
                         "bboxX": 120, "bboxY": 258, "bboxW": 1039, "bboxH": 441,
                         "start": 0.0, "duration": 4.0}))
    if out_ref is not None:
        # AI 回来的结果 = 新的一层,插在最上面。关掉它的眼睛就看见下面的原片。
        doc.add(Entity(type="clip", id="clip-2", name="AI 改过:地板 → 青草地",
                       data={"layer": 1, "visible": True, "start": 0.0, "duration": 4.0,
                             "sourceIn": 0.0, "xKind": "gen", "videoRef": out_ref}))
    doc.add(Entity(type="send_profile", id="profile-1", name="默认发送",
                   data={"mode": 1, "provider": "omni", "strategy": "annotated_video",
                         "model": "gemini-omni-flash-preview", "duration": 4.0,
                         "ratio": "16:9", "paddingSec": 0.5}))


def register(registry: DocumentRegistry) -> None:
    registry.declare_entity_type(name="project", schema=PROJECT_SCHEMA, collaboration_unit=True)
    registry.declare_entity_type(name="asset", schema=ASSET_SCHEMA, collaboration_unit=True)
    registry.declare_entity_type(name="clip", schema=CLIP_SCHEMA, collaboration_unit=True)
    registry.declare_entity_type(name="annotation", schema=ANNOTATION_SCHEMA, collaboration_unit=True)
    registry.declare_entity_type(name="guide_keyframe", schema=GUIDE_KEYFRAME_SCHEMA,
                                 collaboration_unit=True)
    registry.declare_entity_type(name="reference_frame", schema=REFERENCE_FRAME_SCHEMA,
                                 collaboration_unit=True)
    registry.declare_entity_type(name="send_profile", schema=SEND_PROFILE_SCHEMA, collaboration_unit=True)
    registry.declare_entity_type(name="send_job", schema=SEND_JOB_SCHEMA, collaboration_unit=True)
    registry.declare_entity_type(name="prompt_optimization", schema=PROMPT_OPTIMIZATION_SCHEMA,
                                 collaboration_unit=True)
    registry.declare_entity_type(name="export", schema=EXPORT_SCHEMA, collaboration_unit=True)
    registry.set_collaboration_mode("with_save")
    registry.set_save_policy("auto_commit")   # GATE-DOC-05:必须恰好声明一次
    registry.set_demo_project(name="木地板换青草地 demo", bootstrap=_seed)
