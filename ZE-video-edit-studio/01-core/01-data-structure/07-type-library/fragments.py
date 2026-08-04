"""fragments — 类型库内容本体(T1 字段基元 / T2 实体原型 / T3 文档组合)。

**纯 dict 字面量、零逻辑**(治理决定):解析、组合、校验全在 type_library.py。
规则本体见同目录 SPEC.md;词表单一真源见 TYPE-REGISTRY.md(GATE-TYPE-07 双向核对)。

字面量里的两个组合指令(由 type_library.resolve 展开,fragments 自身不解释):
  {"$use": "<field_id>", ...}     单字段基元引用(只许 cosmetic 覆盖:label/x-group/step/default/required)
  {"$cluster": "<id>", "required": [...]}
                                  平铺组引用(point2d/size2d/transform2d/time_range/prompt_block
                                  与三具名簇 run/audio/rect);required 列表按属性名点名必填

命名法见 SPEC §2(snake_case;layer 镜像 Canvas2dLayer 是编辑器契约优先例外)。
"""

from __future__ import annotations

# ==============================================================
# T1 字段基元(21 条;media/mask/thumb/mesh 四件套计 1 条、占 4 个 id)
# 每条: version / label / role(角色表体例) / fields(属性名→schema) / example / doc(关键裁定)
# ==============================================================

FIELDS: dict[str, dict] = {
    "point2d": {
        "version": 1, "label": "二维点", "role": "连续 2D 位置",
        "fields": {
            "x": {"type": "number", "label": "X"},
            "y": {"type": "number", "label": "Y"},
        },
        "example": {"x": 12.5, "y": 40},
        "doc": "平铺非对象——编辑器几何映射(xField/yField)直读平铺字段;FormZone 自动出数值控件",
    },
    "size2d": {
        "version": 1, "label": "像素尺寸", "role": "像素栅格尺寸",
        "fields": {
            "width": {"type": "integer", "minimum": 0, "label": "宽"},
            "height": {"type": "integer", "minimum": 0, "label": "高"},
        },
        "example": {"width": 1920, "height": 1080},
        "doc": "整数:像素离散,浮点尺寸是 v2 漂移源",
    },
    "bbox_xyxy": {
        "version": 1, "label": "检测框", "role": "视觉检测互通框 [x1,y1,x2,y2] 像素",
        "fields": {
            "bbox_xyxy": {
                "type": "array", "minItems": 4, "maxItems": 4,
                "items": {"type": "integer", "minimum": 0}, "label": "检测框",
            },
        },
        "example": {"bbox_xyxy": [10, 20, 110, 220]},
        "doc": "对齐 supervision/COCO;非 xywh 非归一化;x2>x1 是命令层不变量(schema 不表达跨元素约束)。"
               "与 rect_xywh 双轨并存角色不同:本框做视觉互通,rect 做 gizmo 编辑;互转走命令",
    },
    "rect_xywh": {
        "version": 1, "label": "放置矩形", "role": "嵌套用可操纵放置矩形(gizmo 语义)",
        "fields": {
            "rect": {
                "type": "object", "additionalProperties": False, "label": "矩形",
                "properties": {
                    "x": {"type": "number", "required": True},
                    "y": {"type": "number", "required": True},
                    "w": {"type": "number", "minimum": 0, "required": True},
                    "h": {"type": "number", "minimum": 0, "required": True},
                    "rotation": {"type": "number"},
                },
            },
        },
        "example": {"rect": {"x": 0, "y": 0, "w": 100, "h": 50}},
        "doc": "对象形只用于数组内嵌套;实体顶层几何用 ze.cluster.rect 平铺(CAS 按键合并粒度细)",
    },
    "transform2d": {
        "version": 1, "label": "渲染变换", "role": "渲染变换(关键帧可动画集),原点 canvas_center",
        "fields": {
            "x": {"type": "number", "label": "X"},
            "y": {"type": "number", "label": "Y"},
            "scale": {"type": "number", "exclusiveMinimum": 0, "label": "缩放"},
            "rotation": {"type": "number", "label": "旋转(度)"},
            "opacity": {"type": "number", "minimum": 0, "maximum": 1, "label": "不透明度"},
        },
        "example": {"x": 0, "y": 0, "scale": 1, "rotation": 0, "opacity": 1},
        "doc": "scale 非 w/h——resize 语义归 rect;角度用度非弧度",
    },
    "time_range": {
        "version": 1, "label": "时间区间", "role": "时间轴占位",
        "fields": {
            "start": {"type": "number", "minimum": 0, "label": "起始(秒)"},
            "duration": {"type": "number", "minimum": 0, "label": "时长(秒)"},
        },
        "example": {"start": 2.5, "duration": 4},
        "doc": "非 in/out 双端点;秒非帧(fps 是文档设置)",
    },
    "easing": {
        "version": 1, "label": "缓动", "role": "关键帧插值曲线",
        "fields": {
            "easing": {
                "type": "string", "label": "缓动",
                "enum": ["linear", "ease", "ease-in", "ease-out", "ease-in-out"],
            },
        },
        "example": {"easing": "ease-in-out"},
        "doc": "CSS 命名集,可直喂 transition",
    },
    "keyframes": {
        "version": 1, "label": "关键帧", "role": "transform2d 的时间采样",
        "fields": {
            "keyframes": {
                "type": "array", "label": "关键帧",
                "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "time": {"type": "number", "minimum": 0, "required": True},
                        "easing": {"type": "string",
                                   "enum": ["linear", "ease", "ease-in", "ease-out", "ease-in-out"]},
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "scale": {"type": "number"},
                        "rotation": {"type": "number"},
                        "opacity": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                },
            },
        },
        "example": {"keyframes": [{"time": 0, "x": 0}, {"time": 1.5, "x": 120, "easing": "ease"}]},
        "doc": "帧属性与 transform2d 平铺同形(v2 零迁移);整组一字段非每帧一实体",
    },
    "parent": {
        "version": 1, "label": "父节点", "role": "层级树父指针",
        "fields": {
            "parent": {"type": "entity_ref", "label": "父节点"},
        },
        "example": {"parent": {"$ref": "chapter/c1"}},
        "doc": "child→parent 非 children[]:并发加子在 children[] 模型必撞版本冲突(CAS 论证);"
               "to: 在落点(archetype)绑定",
    },
    "order": {
        "version": 1, "label": "序号", "role": "同级稳定序(0 基)",
        "fields": {
            "order": {"type": "integer", "minimum": 0, "label": "序号"},
        },
        "example": {"order": 0},
        "doc": "不收 zIndex/sort_key 别名;重排走命令重写",
    },
    "tags": {
        "version": 1, "label": "标签集", "role": "检索标签集",
        "fields": {
            "tags": {
                "type": "array", "uniqueItems": True, "label": "标签",
                "items": {"type": "string", "minLength": 1},
            },
        },
        "example": {"tags": ["户外", "白天"]},
        "doc": "tags=复数检索面 / label=单一语义名,分工不混",
    },
    "label": {
        "version": 1, "label": "语义名", "role": "数据语义名(是什么:\"apple\")",
        "fields": {
            "label": {"type": "string", "minLength": 1, "label": "标签"},
        },
        "example": {"label": "apple"},
        "doc": "与信封 name(哪一个)划界:name 是实例名,label 是类别语义",
    },
    "class_id": {
        "version": 1, "label": "类目号", "role": "数据集类目号",
        "fields": {
            "class_id": {"type": "integer", "minimum": 0, "label": "类目号"},
        },
        "example": {"class_id": 17},
        "doc": "COCO/YOLO 互通",
    },
    "confidence": {
        "version": 1, "label": "置信度", "role": "模型置信度(仅此一义)",
        "fields": {
            "confidence": {"type": "number", "minimum": 0, "maximum": 1, "label": "置信度"},
        },
        "example": {"confidence": 0.93},
        "doc": "不是 score(score 语义超载);编辑器侧 scoreField:'confidence' 映射",
    },
    "workflow_status": {
        "version": 1, "label": "人审状态", "role": "人审生命周期",
        "closed": True, "pinned_name": "status",
        "fields": {
            "status": {
                "type": "string", "label": "状态",
                "enum": ["draft", "in_review", "approved", "rejected"],
            },
        },
        "example": {"status": "in_review"},
        "doc": "落点字段名钉死 status(编辑器 statusField 约定);closed:加值=core MAJOR(SPEC §4);"
               "与 run_status 两枚举不许混用",
    },
    "run_status": {
        "version": 1, "label": "执行状态", "role": "机器执行结局",
        "closed": True, "pinned_name": "status",
        "fields": {
            "status": {
                "type": "string", "label": "状态",
                "enum": ["pending", "running", "succeeded", "failed", "cancelled"],
            },
        },
        "example": {"status": "succeeded"},
        "doc": "持久化结局态;与会话态 RunPhase 刻意分词(skipped/stale 不持久化);closed 同上",
    },
    "stage_list": {
        "version": 1, "label": "阶段表", "role": "任务静态分阶段(SSE 缺席时的回退渲染源)",
        "fields": {
            "stages": {
                "type": "array", "label": "阶段",
                "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "label": {"type": "string", "required": True},
                        "status": {"type": "string"},
                        "progress": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                },
            },
        },
        "example": {"stages": [{"label": "下载", "status": "done", "progress": 1},
                               {"label": "重建", "status": "active", "progress": 0.4}]},
        "doc": "逐键对齐 taskStages 读法;item.status 是编辑器拥有词表(done/error/active/idle),"
               "库不闭枚举(编辑器演化不应触发库版本)",
    },
    "prompt_block": {
        "version": 1, "label": "提示词对", "role": "一次生成的正负词对",
        "fields": {
            "prompt_block": {
                "type": "object", "additionalProperties": False, "label": "提示词",
                "properties": {
                    "prompt": {"type": "string", "required": True},
                    "negative_prompt": {"type": "string"},
                },
            },
        },
        "example": {"prompt_block": {"prompt": "a red apple", "negative_prompt": "blurry"}},
        "doc": "negative_prompt 全拼(SD 生态);实体顶层用 $cluster 平铺为两字段,对象形只供嵌套",
    },
    "color_hex": {
        "version": 1, "label": "颜色", "role": "UI/标记色 恰 #RRGGBB 六位无 alpha",
        "fields": {
            "color": {"type": "string", "format": "color", "label": "颜色"},
        },
        "example": {"color": "#3366ff"},
        "doc": "validator 模式恰六位(field_types._FORMAT_PATTERNS)+原生取色器只 emit 六位;"
               "透明度走独立 opacity,不用 #RRGGBBAA",
    },
    "timestamp_iso": {
        "version": 1, "label": "时间点", "role": "数据内人读时间点",
        "fields": {
            "timestamp": {"type": "string", "format": "iso_datetime", "label": "时间"},
        },
        "example": {"timestamp": "2026-06-11T10:30:00Z"},
        "doc": "信封 created_at(epoch float)不归它管;机器执行时刻用 run 簇的 epoch 秒",
    },
    # ---- asset_ref 四件套(一槽一职的资产角色槽;计 1 条占 4 id) ----
    # 法 4:按角色命名、无类型后缀(值形状自述类型;编辑器映射按角色读)。
    # 次常用角色(crop/preview/before/after/background/rows)同法直接落 archetype,登记 TYPE-REGISTRY 附则。
    "media": {
        "version": 1, "label": "主媒体", "role": "实体的主资产槽(图/视频/音频/模型)",
        "fields": {"media": {"type": "asset_ref", "label": "媒体"}},
        "example": {"media": {"sha256": "a" * 64, "mime": "image/png", "ext": ".png"}},
        "doc": "asset_ref={sha256,mime,size,ext} 内容寻址;编辑器经 assetUrl(ref) 解 URL",
    },
    "mask": {
        "version": 1, "label": "遮罩", "role": "二值/灰度遮罩资产槽",
        "fields": {"mask": {"type": "asset_ref", "label": "遮罩"}},
        "example": {"mask": {"sha256": "b" * 64, "mime": "image/png"}},
        "doc": "不叫 mask_asset_ref(法 4)",
    },
    "thumb": {
        "version": 1, "label": "缩略图", "role": "列表/网格用小图资产槽",
        "fields": {"thumb": {"type": "asset_ref", "label": "缩略图"}},
        "example": {"thumb": {"sha256": "c" * 64, "mime": "image/jpeg"}},
        "doc": "可再生派生物,不当真相源",
    },
    "mesh": {
        "version": 1, "label": "网格", "role": "三维网格/点云资产槽",
        "fields": {"mesh": {"type": "asset_ref", "label": "网格"}},
        "example": {"mesh": {"sha256": "d" * 64, "ext": ".glb"}},
        "doc": "Scene3d meshField 按角色读",
    },
}


# ==============================================================
# T1 字段簇 ×3(整合清单 3/4/5:跨原型共享的平铺组)
# ==============================================================

CLUSTERS: dict[str, dict] = {
    "run": {
        "version": 1, "label": "执行簇", "role": "机器执行记录(task 与 data_query 共组)",
        "fields": {
            "status": {"$use": "run_status"},
            "error": {
                "type": "object", "label": "错误",
                "properties": {
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                    "trace_id": {"type": "string"},
                },
            },
            "started_at": {"type": "number", "minimum": 0, "label": "开始(epoch 秒)"},
            "finished_at": {"type": "number", "minimum": 0, "label": "结束(epoch 秒)"},
        },
        "example": {"status": "failed",
                    "error": {"code": "timeout", "message": "deadline exceeded"},
                    "started_at": 1765346400.0, "finished_at": 1765346460.5},
        "doc": "status 字段名是硬约束(taskStages 直读 data.status);时刻用 epoch 秒 float"
               "(机器记录),人读时间点才用 timestamp_iso",
    },
    "audio": {
        "version": 1, "label": "音频簇", "role": "音量/静音/淡入淡出(从 clip 抽出供 audio 工具复用)",
        "fields": {
            "volume": {"type": "number", "minimum": 0, "maximum": 1, "label": "音量"},
            "muted": {"type": "boolean", "label": "静音"},
            "fade_in": {"type": "number", "minimum": 0, "label": "淡入(秒)"},
            "fade_out": {"type": "number", "minimum": 0, "label": "淡出(秒)"},
        },
        "example": {"volume": 0.8, "muted": False, "fade_in": 0.5, "fade_out": 1},
        "doc": "整合清单 4",
    },
    "rect": {
        "version": 1, "label": "几何簇", "role": "实体顶层平铺放置几何(placed_object 等共享)",
        "fields": {
            "x": {"type": "number", "x-group": "几何", "label": "X"},
            "y": {"type": "number", "x-group": "几何", "label": "Y"},
            "w": {"type": "number", "minimum": 0, "x-group": "几何", "label": "宽"},
            "h": {"type": "number", "minimum": 0, "x-group": "几何", "label": "高"},
            "rotation": {"type": "number", "minimum": -180, "maximum": 180, "step": 1,
                         "x-group": "变换", "label": "旋转"},
        },
        "example": {"x": 40, "y": 40, "w": 140, "h": 90, "rotation": 0},
        "doc": "平铺对约定:顶层几何平铺(CAS 细粒度+FormZone 自动滑杆);数组内嵌套才用 rect_xywh 对象形。"
               "layer 因编辑器契约例外(camelCase width/height)单列,张力已注记",
    },
}


# ==============================================================
# T2 实体原型 ×13
# 通用:信封字段(id/name/version/actor_log)不进 data;schema 即 declare_entity_type 实参;
# sealed=删改即 core MAJOR 且 mod 不得重定义/收紧;未列字段=x_ 自由区(SPEC §6 纪律)。
# binding=逐 prop 核对过的编辑器绑定常量表(信息面;字段名是承重墙,转写不得漂移)。
# ==============================================================

# Canvas2dLayer 1:1 镜像区(命名法 2 编辑器契约优先例外:camelCase 沿用)。
# kind/blendMode 是**编辑器拥有的开放词表**(canvas2dModel.ts 随工具演化),库存 string 不闭枚举;
# shape/bbox/filter/meta 为编辑器扩展槽,库存开放 object。entity 形态只在 manifest 交换时出现,
# 文档内走 scene.layers 内联栈(编辑器原生零适配)。
_LAYER_PROPS: dict = {
    "kind": {"type": "string", "required": True, "label": "图层类型"},
    "visible": {"type": "boolean", "label": "可见"},
    "locked": {"type": "boolean", "label": "锁定"},
    "opacity": {"type": "number", "minimum": 0, "maximum": 1, "label": "不透明度"},
    "blendMode": {"type": "string", "label": "混合模式"},
    "x": {"type": "number", "label": "X"},
    "y": {"type": "number", "label": "Y"},
    "width": {"type": "number", "label": "宽"},
    "height": {"type": "number", "label": "高"},
    "rotation": {"type": "number", "label": "旋转"},
    "flipX": {"type": "boolean", "label": "水平翻转"},
    "flipY": {"type": "boolean", "label": "垂直翻转"},
    "assetRef": {"type": "asset_ref", "label": "位图"},
    "maskRef": {"type": "asset_ref", "label": "遮罩"},
    "text": {"type": "string", "label": "文本"},
    "shape": {"type": "object", "label": "矢量形状"},
    "bbox": {
        "type": "object", "label": "标注框",
        "properties": {
            "x": {"type": "number", "required": True},
            "y": {"type": "number", "required": True},
            "w": {"type": "number", "required": True},
            "h": {"type": "number", "required": True},
            "rotation": {"type": "number"},
        },
    },
    "filter": {
        "type": "object", "label": "滤镜",
        "properties": {
            "name": {"type": "string", "required": True},
            "amount": {"type": "number"},
        },
    },
    "meta": {"type": "object", "label": "元数据"},
}

ARCHETYPES: dict[str, dict] = {
    "asset_item": {
        "version": 1, "label": "媒资", "collab_unit": True,
        "sealed": ["media", "kind", "width", "height", "duration"],
        "fields": {
            "media": {"$use": "media", "required": True},
            "kind": {"type": "string", "required": True, "label": "种类",
                     "enum": ["image", "video", "audio", "model3d", "file"]},
            "size": {"$cluster": "size2d"},
            "duration": {"type": "number", "minimum": 0, "label": "时长(秒)"},
            "thumb": {"$use": "thumb"},
            "tags": {"$use": "tags"},
            "source": {"type": "string", "label": "来源"},
        },
        "binding": {
            "gallery": {"thumbField": "media"},
            "media": {"mediaField": "media"},
            "asset": {"assetField": "media"},
            "scene3d": {"meshField": "media"},
        },
        "example": {"media": {"sha256": "a" * 64, "mime": "image/png", "ext": ".png"},
                    "kind": "image", "width": 1024, "height": 768, "tags": ["素材"]},
        "doc": "蒸馏 video-timeline(assets[])/prompt-studio/reference-pack;按 kind 分流编辑器",
    },
    "scene": {
        "version": 1, "label": "场景锚", "collab_unit": True, "singleton": True,
        "sealed": ["width", "height", "background", "status"],
        "fields": {
            "width": {"type": "integer", "minimum": 1, "required": True,
                      "x-group": "画布", "label": "宽"},
            "height": {"type": "integer", "minimum": 1, "required": True,
                       "x-group": "画布", "label": "高"},
            "background": {"type": "asset_ref", "label": "背景"},
            "status": {"$use": "workflow_status"},
            "difficulty": {"type": "integer", "minimum": 1, "maximum": 5, "label": "难度"},
            "tags": {"$use": "tags"},
            "fps": {"type": "number", "exclusiveMinimum": 0, "label": "帧率"},
            # 弃用于画布编辑(2026-06-11 裁定:图层=layer 实体,见 layered_image preset);
            # 字段保留(非 sealed,删=MAJOR 不做)仅作信封 legacy 折展兼容。
            "layers": {
                "type": "array", "label": "图层栈",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "required": True},
                        "name": {"type": "string"},
                        **_LAYER_PROPS,
                    },
                },
            },
            "active_layer": {"type": "string", "label": "活动图层"},
        },
        "binding": {
            "canvas2d": {"bgField": "background"},
            "canvas2d_ps_lite": {"preset": "ps-lite", "layersField": "layers",
                                 "activeLayerField": "active_layer"},
            "form": {},
        },
        "example": {"width": 1920, "height": 1080, "status": "draft", "difficulty": 3},
        "doc": "整合产物:原 level + layered canvas 复合统一为一个单例锚(粒度法 B)。"
               "一关卡/一画布/一条成片(别名 sequence,fps 落锚)=一 document 的作品级属性载体;"
               "width/height int≥1 是 size2d 的收紧形(作品画布不存在 0 尺寸)",
    },
    "placed_object": {
        "version": 1, "label": "放置物", "collab_unit": True,
        "sealed": ["x", "y", "w", "h", "rotation", "media", "status"],
        "fields": {
            "geometry": {"$cluster": "rect", "required": ["x", "y", "w", "h"]},
            "order": {"$use": "order"},
            "media": {"$use": "media"},
            "label": {"$use": "label"},
            "color": {"$use": "color_hex"},   # 标记色(勘误 2026-06-11 加性:标注框普遍带色,Pilot A 实证)
            "status": {"$use": "workflow_status"},
        },
        "binding": {
            "canvas2d": {"xField": "x", "yField": "y", "wField": "w", "hField": "h",
                         "rotationField": "rotation"},
            "form": {},
        },
        "example": {"x": 40, "y": 40, "w": 140, "h": 90, "rotation": 0,
                    "label": "apple", "color": "#3366ff", "status": "draft"},
        "doc": "蒸馏 seek-find object/image-to-scene;已去 level 引用(单关单档,锚即上下文,整合清单 2)",
    },
    "layer": {
        "version": 1, "label": "图层", "collab_unit": True,
        "sealed": ["kind", "visible", "locked", "opacity", "blendMode", "x", "y",
                   "width", "height", "rotation", "flipX", "flipY", "assetRef",
                   "maskRef", "text", "order"],
        "fields": {
            **_LAYER_PROPS,
            "order": {"$use": "order"},
        },
        "binding": {
            # PS 图层工作台三联(layered_canvas 预设):图层列表=卡片列表编辑器实例,
            # 画布只合成渲染,属性=form——三编辑器零直连,全经全局 selection[<layer 桶>]。
            "gallery": {"layoutMode": "compact-list", "thumbField": "assetRef",
                        "visibilityField": "visible", "lockedField": "locked",
                        "reorderCommand": "<mod 声明,如 layer.reorder {type,id,before_id}>"},
            "canvas2d": {"layerType": "<layer 桶名>(合成渲染按 order 排序;活动层=selection)"},
            "form": {},
        },
        "example": {"kind": "raster", "visible": True, "locked": False, "opacity": 1,
                    "blendMode": "normal", "x": 0, "y": 0, "width": 512, "height": 512,
                    "rotation": 0, "flipX": False, "flipY": False, "order": 0},
        "doc": "命名法 2 例外区:1:1 镜像 Canvas2dLayer(camelCase 含 blendMode/assetRef/flipX);"
               "kind/blendMode 是编辑器拥有的开放词表(随 canvas2d 工具演化),库不闭枚举。"
               "**entity form 是画布主形态**(选择驱动属性面板/逐层 CAS/逐层协作;裁定 2026-06-11,"
               "推翻初版「内联栈为主」);scene.layers 内联栈仅作信封 legacy 折展兼容",
    },
    "clip": {
        "version": 1, "label": "片段", "collab_unit": True,
        "sealed": ["asset", "track", "start", "duration", "keyframes"],
        "fields": {
            # asset 可选(勘误 2026-06-11):timeline 双击空轨新增的 core 编辑器流先建后挂素材,
            # 必填会击穿该流(Pilot A 实证);零注解消费者窗口原位修正,见 MIGRATIONS 1.26.0。
            "asset": {"type": "entity_ref", "to": "asset_item", "label": "素材"},
            "track": {"type": "integer", "minimum": 0, "required": True, "label": "轨道"},
            "range": {"$cluster": "time_range", "required": ["start", "duration"]},
            "source_in": {"type": "number", "minimum": 0, "label": "素材入点(秒)"},
            "transform": {"$cluster": "transform2d"},
            "audio": {"$cluster": "audio"},
            "keyframes": {"$use": "keyframes"},
            "color": {"$use": "color_hex"},
            "label": {"$use": "label"},
        },
        "binding": {
            "timeline": {"fields": {"track": "track", "start": "start",
                                    "duration": "duration", "label": "label",
                                    "color": "color"}},
            "form": {},
        },
        "example": {"asset": {"$ref": "asset_item/a1"}, "track": 0, "start": 0,
                    "duration": 4, "color": "#3366ff", "label": "开场"},
        "doc": "demo-cut 全字段实证;track=整数泳道号是裁定非沿袭(CRDT 友好、免级联 join,整合清单 6)",
    },
    "track": {
        "version": 1, "label": "轨道", "collab_unit": True,
        "sealed": ["order", "kind"],
        "fields": {
            "order": {"$use": "order", "required": True},
            "kind": {"type": "string", "required": True, "label": "轨类型",
                     "enum": ["visual", "audio", "data"]},
            "label": {"$use": "label"},
            "muted": {"type": "boolean", "label": "静音"},
            "locked": {"type": "boolean", "label": "锁定"},
            "color": {"$use": "color_hex"},
        },
        "binding": {
            "table": {"renameField": "label"},
        },
        "example": {"order": 0, "kind": "visual", "label": "主轨"},
        "doc": "clip↔track 经整数 order 连接非 trackId 字符串(TimelineZone 双击空轨 dispatch 整数泳道);"
               "重排轨道须级联重写 clip.track=命令层不变量",
    },
    "task": {
        "version": 1, "label": "任务", "collab_unit": False,
        "sealed": ["status", "stages", "error"],
        "fields": {
            "run": {"$cluster": "run", "required": ["status"]},
            "kind": {"type": "string", "label": "任务类型"},
            "stages": {"$use": "stage_list"},
            "input": {"type": "object", "label": "输入"},
            "output": {"type": "object", "label": "输出"},
            "cost_usd": {"type": "number", "minimum": 0, "label": "成本(USD)"},
        },
        "binding": {
            "task": {"stagesField": "stages"},
        },
        "example": {"status": "running", "kind": "generate",
                    "stages": [{"label": "排队", "status": "done"},
                               {"label": "生成", "status": "active", "progress": 0.6}]},
        "doc": "collab_unit=False:supervisor 单写者;status 字段名硬约束(taskStages 直读 data.status);"
               "run 簇与 data_query 共组(整合清单 3)。残差:taskStages 的 status 别名映射"
               "(succeeded→done 等)待编辑器区开放后补(SPEC §10)",
    },
    "review": {
        "version": 1, "label": "审核", "collab_unit": True,
        "sealed": ["target", "status", "note"],
        "fields": {
            "target": {"type": "entity_ref", "required": True, "label": "对象"},
            "status": {"$use": "workflow_status", "required": True},
            "note": {"type": "string", "label": "审阅意见"},
            "before": {"type": "asset_ref", "label": "处理前"},
            "after": {"type": "asset_ref", "label": "处理后"},
            "reviewer": {"type": "string", "label": "审核人"},
            "reviewed_at": {"$use": "timestamp_iso", "label": "审核时间"},
        },
        "binding": {
            "review": {"statusField": "status", "noteField": "note",
                       "_assets": "beforeUrl/afterUrl 由装配层 assetUrl(before/after) 解出"},
        },
        "example": {"target": {"$ref": "placed_object/p1"}, "status": "approved",
                    "note": "通过", "reviewer": "u1"},
        "doc": "target 不绑 to(审核对象跨型);ReviewEditor 写路径只 emit approved/rejected,在枚举内",
    },
    "prompt_preset": {
        "version": 1, "label": "提示词预设", "collab_unit": True,
        "sealed": ["prompt", "negative_prompt", "model"],
        "fields": {
            "words": {"$cluster": "prompt_block", "required": ["prompt"]},
            "model": {"type": "string", "label": "模型"},
            "params": {"type": "object", "label": "参数"},
            "refs": {"type": "array", "items": {"type": "asset_ref"}, "label": "参考图"},
            "tags": {"$use": "tags"},
            "status": {"$use": "workflow_status"},
        },
        "binding": {
            "form": {},
            "text": {"field": "prompt"},
        },
        "example": {"prompt": "a red apple on wood", "negative_prompt": "blurry",
                    "model": "gpt-image-2", "tags": ["产品图"]},
        "doc": "model 自由 string 不锁枚举(模型迭代快于库版本);prompt-studio 的 15 个结构化字段"
               "是 mod 私有域(x_ 区),不进库",
    },
    "annotation": {
        "version": 1, "label": "标注", "collab_unit": False,
        "sealed": ["target", "class_id", "confidence", "bbox_xyxy", "mask", "crop",
                   "track_id", "frame", "source_adapter"],
        "fields": {
            "target": {"type": "entity_ref", "to": "asset_item", "required": True, "label": "对象"},
            "label": {"$use": "label", "required": True},
            "class_id": {"$use": "class_id"},
            "confidence": {"$use": "confidence"},
            "bbox_xyxy": {"$use": "bbox_xyxy"},
            "mask": {"$use": "mask"},
            "crop": {"type": "asset_ref", "label": "裁切图"},
            "track_id": {"type": "integer", "minimum": 0, "label": "追踪号"},
            "frame": {"type": "integer", "minimum": 0, "label": "帧号"},
            "source_adapter": {"type": "string", "label": "来源适配器"},
            "status": {"$use": "workflow_status"},
        },
        "binding": {
            "gallery": {"thumbField": "crop", "scoreField": "confidence",
                        "labelField": "label"},
        },
        "example": {"target": {"$ref": "asset_item/a1"}, "label": "person",
                    "class_id": 0, "confidence": 0.97, "bbox_xyxy": [10, 20, 110, 220],
                    "status": "in_review"},
        "doc": "detect-route/sam3/tracker/supervision 草案逐字段对齐;collab_unit=False(AI 批量写);"
               "诚实声明:bbox_xyxy 不能 gizmo 直编(互通优先),改框走命令或转 placed_object",
    },
    "chapter": {
        "version": 1, "label": "章节", "collab_unit": True,
        "sealed": ["body", "parent", "order"],
        "fields": {
            "body": {"type": "string", "label": "正文"},
            "parent": {"$use": "parent", "to": "chapter"},
            "order": {"$use": "order"},
            "tags": {"$use": "tags"},
            "status": {"$use": "workflow_status"},
        },
        "binding": {
            "text": {"field": "body"},
            "table": {},
        },
        "example": {"body": "# 第一章\n\n正文……", "order": 0, "status": "draft"},
        "doc": "标题=信封 name(终结 title/name 双轨);长文协作 CRDT 价值最大",
    },
    "block": {
        "version": 1, "label": "块", "collab_unit": True,
        "sealed": ["blockType", "text", "level", "variant", "language",
                   "checked", "url", "caption", "media", "parent", "order"],
        "fields": {
            # blockType 是编辑器拥有的开放词表(随块注册表演化,库不闭枚举,同 layer.kind):
            # paragraph/heading/blockquote/code/checklist-item/divider/callout/figure/toggle/…
            "blockType": {"type": "string", "required": True, "label": "块类型"},
            "text": {"type": "string", "label": "文本(markdown 行内/源)"},
            "level": {"type": "integer", "minimum": 1, "maximum": 6, "label": "标题级"},
            "variant": {"type": "string", "label": "变体"},
            "language": {"type": "string", "label": "代码语言"},
            "checked": {"type": "boolean", "label": "勾选"},
            "url": {"type": "string", "label": "链接"},
            "caption": {"$use": "label"},
            "media": {"$use": "media"},
            "parent": {"$use": "parent", "to": "block"},
            "order": {"$use": "order"},
            "_raw": {"type": "string", "label": "原样保留"},
        },
        "binding": {
            "document": {"blockTypeField": "blockType", "textField": "text",
                         "parentField": "parent", "orderField": "order",
                         "mediaField": "media"},
            "outline": {"parentField": "parent", "orderField": "order"},
            "form": {},
        },
        "example": {"blockType": "paragraph", "text": "Hello **world**", "order": 0},
        "doc": "Notion/Obsidian/飞书 块=L2 atom 实体化;单父树(parent/order,同 chapter,根 parent=null)。"
               "blockType 是编辑器拥有的开放词表(随块注册表演化、库不闭枚举,同 layer.kind);"
               "text 存 markdown 行内源(聚焦编辑/失焦渲染);figure 资产走 media 不内联二进制;"
               "_raw 保 round-trip 未识别块。document 块编辑器 + outline 目录 + form 属性经 selection[block] 联动",
    },
    "data_query": {
        "version": 1, "label": "数据查询", "collab_unit": True,
        "sealed": ["sql", "status", "columns", "rows"],
        "fields": {
            "sql": {"type": "string", "required": True, "label": "SQL"},
            "dialect": {"type": "string", "label": "方言",
                        "enum": ["bigquery", "postgres", "mysql", "sqlite", "duckdb"]},
            "run": {"$cluster": "run"},
            "columns": {
                "type": "array", "label": "列",
                "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string", "required": True},
                        "dtype": {"type": "string"},
                    },
                },
            },
            "rows": {"type": "asset_ref", "label": "结果行"},
            "row_count": {"type": "integer", "minimum": 0, "label": "行数"},
        },
        "binding": {
            "code": {"codeField": "sql", "langField": "dialect"},
        },
        "example": {"sql": "SELECT 1", "dialect": "bigquery", "status": "succeeded",
                    "columns": [{"name": "f0_", "dtype": "INT64"}], "row_count": 1},
        "doc": "结果行进 asset 不进 entity(容量叶子推论);sql 人编 status 机写故 collab_unit=True;"
               "dialect 枚举非 closed(加方言=core MINOR,SPEC §4)",
    },
    "graph_node": {
        "version": 1, "label": "图节点", "collab_unit": True,
        "sealed": ["x", "y", "deps"],
        "fields": {
            "position": {"$cluster": "point2d", "required": ["x", "y"]},
            "label": {"$use": "label"},
            "deps": {"type": "array", "items": {"type": "string"}, "label": "上游"},
            "kind": {"type": "string", "label": "节点类型"},
            "params": {"type": "object", "label": "参数"},
            "ports": {
                "type": "array", "label": "端口",
                "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string", "required": True},
                        "direction": {"type": "string", "enum": ["in", "out"]},
                        "dtype": {"type": "string"},
                    },
                },
            },
        },
        "binding": {
            "graph": {"fields": {"x": "x", "y": "y", "label": "label", "deps": "deps"}},
        },
        "example": {"x": 120, "y": 80, "label": "blur", "deps": ["n1", "n2"],
                    "kind": "filter"},
        "doc": "deps 是裸 string[] 上游 id 非 entity_ref(GraphCanvasZone.depsOf 直接当 string[] 画边)"
               "——库内唯一裸 id 关系,TYPE-REGISTRY 白名单单列;边实体 v1 不立(导入折进 deps+x_edge_meta)",
    },
}


# ==============================================================
# T3 文档组合 presets ×7(粒度四法见 SPEC §3)
# entity_types: 桶名(mod 可经 apply_preset(rename=) 改名)→ archetype 短名。
# singleton: 作品型锚桶(法 B,导入/校验须恰一个);容器型(法 C)无。
# layout_hint 是给人/scaffold 的提示字符串,不是机器绑定。
# ==============================================================

DOC_PRESETS: dict[str, dict] = {
    "timeline": {
        "version": 1, "label": "时间线成片", "granularity": "作品型(一条成片)",
        "entity_types": {"sequence": "scene", "asset_item": "asset_item",
                         "track": "track", "clip": "clip"},
        "singleton": ["sequence"],
        "collaboration_mode": "with_save",
        "layout_hint": "catalog_browse: gallery(素材) / timeline(clip) / form",
        "doc": "sequence 锚(scene 别名)收编原 project{width,height,fps,background} 块",
    },
    "scene": {
        "version": 1, "label": "场景关卡", "granularity": "作品型(一关卡=一档,范式案例)",
        "entity_types": {"scene": "scene", "placed_object": "placed_object",
                         "asset_item": "asset_item"},
        "singleton": ["scene"],
        "collaboration_mode": "with_save",
        "layout_hint": "single_canvas: canvas2d(平铺几何, bgField=background) / form",
        "doc": "关卡列表=project picker 多文档,不在档内(粒度法 D);seek-find 改造范式",
    },
    "layered_image": {
        "version": 1, "label": "分层画布", "granularity": "作品型(一张画布)",
        "entity_types": {"scene": "scene", "layer": "layer", "asset_item": "asset_item"},
        "singleton": ["scene"],
        "collaboration_mode": "with_save",
        "layout_hint": "layered_canvas: gallery(layer, compact-list, eye/lock/reorder) / "
                       "canvas2d(layer 合成, preset=ps-lite) / form(layer)",
        "doc": "图层=layer 实体(画布主形态:卡片列表选层→全局 selection→form 属性/画布活动层;"
               "逐层 CAS)。scene.layers 内联栈弃用于画布编辑,仅信封 legacy 折展兼容",
    },
    "review_queue": {
        "version": 1, "label": "审核队列", "granularity": "容器型(法 C:批次即作品)",
        "entity_types": {"task": "task", "review": "review", "asset_item": "asset_item"},
        "singleton": [],
        "collaboration_mode": "realtime_checkpoint",
        "layout_hint": "pipeline_batch: task / review",
        "doc": "AI 后台不随浏览器关;可经 P1 只读源汇览多个作品档",
    },
    "knowledge": {
        "version": 1, "label": "知识空间", "granularity": "作品型(一个知识空间)",
        "entity_types": {"chapter": "chapter"},
        "singleton": [],
        "collaboration_mode": "live_collab",
        "layout_hint": "table / text / form",
        "doc": "树状层级靠 chapter.parent;空间本身即作品,无单例锚",
    },
    "document": {
        "version": 1, "label": "块文档", "granularity": "作品型(一篇文档)",
        "entity_types": {"block": "block"},
        "singleton": [],
        "collaboration_mode": "live_collab",
        "layout_hint": "document(block, 正文) / outline(block, 目录树) / form(block 属性)",
        "doc": "Notion/Obsidian/飞书 式块文档:block.parent 单父树根 parent=null(无单例锚,同 knowledge);"
               "正文=block 实体流(document 编辑器)、目录=同 block 的 outline 实例、块属性=form,"
               "三编辑器经全局 selection[block] 联动。一篇文档=一 Document(多篇用 DocumentIndex)",
    },
    "dataset": {
        "version": 1, "label": "查询工作簿", "granularity": "容器型(一个查询工作簿)",
        "entity_types": {"data_query": "data_query"},
        "singleton": [],
        "collaboration_mode": "with_save",
        "layout_hint": "table / code / form",
        "doc": "",
    },
    "node_graph": {
        "version": 1, "label": "节点图", "granularity": "作品型(一张图)",
        "entity_types": {"graph_node": "graph_node"},
        "singleton": [],
        "collaboration_mode": "live_collab",
        "layout_hint": "single_canvas: graph",
        "doc": "图本身即作品;节点平铺,边由 deps 表达",
    },
}

# 已知缺口(诚实声明):三维域 v1 无独立 preset——以 asset_item(kind=model3d)+Scene3d{meshField}
# 承载,待 splat/3d 系列 v3 化后立 ze.doc.scene3d(加性,SPEC §10)。
