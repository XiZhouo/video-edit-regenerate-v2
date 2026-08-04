"""sdk/types.py — 类型库统一入口 façade(零逻辑转出口,镜像 sdk/model.py 的门面角色)。

mod 端用法(02-mod/05-code/document_register.py):

    from sdk import types as zt

    def register(registry):
        zt.apply_preset(registry, "scene")                      # 整 preset 组合
        registry.declare_entity_type(                           # 或逐原型
            name="shot",
            schema=zt.archetype("asset_item", extend={"x_caption": {"type": "string"}}),
            collaboration_unit=True,
        )

本体在 01-core/01-data-structure/07-type-library/(type_library.py + fragments.py);
规则见该叶子 SPEC.md,词表见 TYPE-REGISTRY.md。路径由 sdk/_paths 注入。
"""

from __future__ import annotations

from type_library import (  # noqa: F401
    apply_preset,
    archetype,
    assert_tightens,
    cluster,
    doc_preset,
    field,
    list_fragments,
    resolve,
    resolve_type_schema,
)
