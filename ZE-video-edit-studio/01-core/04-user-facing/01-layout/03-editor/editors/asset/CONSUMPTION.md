# Asset editor consumption rules

## Layout 声明

```ts
slot('asset', 'asset', ['image_asset'], { assetField: 'sourceRef', transformCommand: 'asset.transform' })
```

## `view_state` 消费规则

- assetField: 指定 asset_ref 字段；缺省时自动找第一个像 asset_ref 的字段
- transformCommand: rotate/flip/crop 等意图派发给 mod

## 组合逻辑

asset 常作为详情 viewer：gallery 选素材，asset 看大图，form 改元数据。

## 写入规则

所有写入都必须经过 `dispatch(commandId, payload)`。如果某个字段能回退到内置 `entity.update/entity.delete`，也仍然要带 `type/id/expected_version`，让 undo、CAS、运行态和审计链路保持一致。
