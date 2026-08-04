# Media editor consumption rules

## Layout 声明

```ts
slot('player', 'media', ['clip'], { mediaField: 'videoRef', fps: 24, markCommand: 'clip.mark_range' })
```

## `view_state` 消费规则

- mediaField: 音视频 asset_ref 字段
- fps: 帧率显示与时间换算
- markCommand: 提交 in/out 标记

## 组合逻辑

media + timeline + form 可以组成剪辑工作室：media 播放，timeline 排片段，form 改字段。

## 写入规则

所有写入都必须经过 `dispatch(commandId, payload)`。如果某个字段能回退到内置 `entity.update/entity.delete`，也仍然要带 `type/id/expected_version`，让 undo、CAS、运行态和审计链路保持一致。
