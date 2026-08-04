# Scene3d editor

代码入口: `Scene3dEditor.tsx`。

## 说明

3D 资产预览与轻量交互壳。core 给容器、状态、字段和命令接缝；three.js 或其它重渲染器由 mod 注入。

## Core 负责

- mesh asset_ref 解析、renderer 缺失/加载/错误状态
- camera 所有权分流:个人观察相机进 `user×skill×project×slot` view-state,
  领域相机才经 `cameraCommand` dispatch
- selection/displayMode/transform 的字段读取和显式 command dispatch
- 个人相机快照的 JSON 安全/容量边界、高频变更 debounce、scope 切换隔离与 reset 清理
- displayModes/transformMode 等 UI 控制入口
- `controls.resetView` / `controls.wireframe` 显式声明外壳按钮 capability；两项缺省均为 false，
  只显示渲染器真实支持的按钮

## Core 不负责

- three.js 打包、模型加载器、材质/灯光/后处理算法
- 复杂 3D 编辑器的业务状态机

## 相机所有权

- 默认:`cameraCommand` 存在时是 `domain`,否则是 `personal`。
- `cameraMode: 'personal' | 'domain'` 可显式覆盖;显式 `personal` 即使保留
  `cameraCommand` 也绝不 dispatch。
- personal 首开无偏好时,`cameraField` 只是 transient 初值;再无初值则由 renderer
  home/fit。orbit/pan/zoom 稳定后才持久。
- reset 删除 durable personal camera,回到 entity transient 初值或 renderer home;不把自动
  home 写成用户偏好。

## 外壳控件 capability

- `controls: { resetView: true }` 只显示复位视角。
- `controls: { wireframe: true }` 只显示线框开关。
- 两项可同时开启；未声明时均不显示，避免渲染器不支持却出现死按钮。
- `displayModes` 下拉独立于 controls；存在多个模式时仍显示 toolbar。

## 消费入口

mod 不直接把这个组件塞进业务页面；mod 在 LayoutTree 里声明 slot，`slotAdapters.tsx` 把 `view_state` 翻译成 props。稳定 import 仍然是 `@ze/core/04-user-facing/01-layout/03-editor`。
