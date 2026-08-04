# Media editor

代码入口: `MediaEditor.tsx`。

## 说明

音视频播放器。它负责播放、速率、时间显示和 in/out 标记，不负责剪辑时间线。

## Core 负责

- media asset_ref 解析、video/audio 播放控制、播放速率
- markCommand 的 in/out 标记提交
- 共享时间游标(C0):双向接通 Interaction State 的 `currentTime` —— scrub/播放提升为共享游标、外部改 currentTime 则 seek 回去(防回环走 `lib/playhead`);装配层未接该轴则退回纯本地播放，行为不变

## Core 不负责

- 多轨剪辑、关键帧、转码、抽帧 pipeline
- 复杂时间线；那是 timeline editor

## 消费入口

mod 不直接把这个组件塞进业务页面；mod 在 LayoutTree 里声明 slot，`slotAdapters.tsx` 把 `view_state` 翻译成 props。稳定 import 仍然是 `@ze/core/04-user-facing/01-layout/03-editor`。
