# keyframe-curve editor validation rules

## 子模块结构

- 必须存在 `CurveEditor.tsx`(代码入口)+ `README.md` / `CONSUMPTION.md` / `VALIDATION.md`。
- 纯量算留 `03-editor/lib/curveModel.ts`(value↔y、值域、排序),配 `curveModel.test.ts`;时间轴量算复用 `timelineModel.ts`。

## 机器校验

- 拖动改 time+value 必须一次 `entity.update {patches, expected_version}`(单 Operation、原子撤销)。
- value/time 字段映射必须落在 entity.data;snap/valueMin/valueMax 是数字。
- `CurveEditor.tsx` 不得内嵌别的 `*Editor.tsx`(尤其 timeline/media)。
- 04-editors 皮肤零令牌:颜色经 atom 类(`.ze-text`/`.ze-edge`/`.ze-surface` 等),不写 `var(--ze-*)`。

## 人工审查口径

- 新增能力先问:是不是通用「值×时间」工作面能力;能否经 view_state/command 表达;若需与别的 editor 同屏,是否应上移 L5 LayoutTree。
- 转 adopted 前提:有真实消费者接 UI + 补 gallery fixture + manifest status 改 adopted。
