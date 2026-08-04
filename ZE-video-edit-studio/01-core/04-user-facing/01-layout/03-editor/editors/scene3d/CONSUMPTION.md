# Scene3d editor consumption rules

## Layout 声明

```ts
slot('preview3d', 'scene3d', ['mesh'], { meshField: 'glb', rendererModuleId: 'scene3dThreeRenderer', displayModeField: 'displayMode', displayModes: ['material', 'wireframe'], controls: { resetView: true, wireframe: true } })
```

## `view_state` 消费规则

- meshField: mesh/splat/voxel 资产字段
- rendererModuleId 或 ctx.renderers.scene3d: 渲染后端注入
- cameraField:领域相机或 personal 模式的 transient 首开初值
- cameraMode:`personal | domain` 显式所有权;缺省由 cameraCommand 是否存在推导
- cameraCommand:仅 domain 模式提交领域相机;personal 模式绝不消费
- selectionField/displayModeField:显式领域交互状态字段
- selectionCommand/displayModeCommand/transformCommand:提交对应领域变化
- controls:`{ resetView?: boolean, wireframe?: boolean }` 外壳按钮 capability；缺省均为 false，
  每个按钮仅在对应值严格为 true 时显示。displayModes 多于一个时 toolbar 独立保留

## 组合逻辑

scene3d 做主视图时，旁边用 form 展示参数，用 gallery/table 切换资产。

## 写入规则

领域写入都必须经过 `dispatch(commandId, payload)`。如果某个字段能回退到内置 `entity.update/entity.delete`，也仍然要带 `type/id/expected_version`，让 undo、CAS、运行态和审计链路保持一致。

personal camera 不是领域写入:只接受有界的 plain JSON snapshot,进共享 view-state
store,不进 Document/Operation/undo。renderer 若每帧回调,不得自行写 storage;core 会 debounce,
参考 OrbitControls renderer 在交互稳定边界回传 plain pose。
