# 05-preferences — SPEC

偏好层 = **theme + locale + personal view state**：同一类东西(per-user / per-device、改了立即生效、不进 Document / 不进 OpLog)。本叶子是唯一 React glue 与本地持久化 authority。

对应架构 State Layers 里的 **Rendering Hints / Interaction State**(会话态,不持久化进文档,只落 localStorage)。

---

## §1 范围

| 在本叶子 | 不在本叶子 |
|---|---|
| theme 三态(light / dark / system)的 state + 持久化 + 应用 | 暗色具体配色(在 [tokens.css](../../../03-css-rules/01-tokens/tokens.css) 的 `[data-theme="dark"]`)|
| locale 的 React 订阅(`useSyncExternalStore`)+ `<html lang>` | 词表本体 / `translate` / locale store(在 [05-i18n](../../../../05-i18n/),纯 TS)|
| `useT` / `Trans` React hook | 非 React 取词(用 `@ze/i18n` 的 `getT`)|
| `user × skill × project × slot` 的版本化视图 slice、校验与迁移 | Document、Operation、undo、共享领域选择或 ACL |
| Scene3D 个人相机、稳定声明分区的折叠偏好 | 领域相机、无稳定 ID 的显示分组、跨设备云同步 |

---

## §2 Theme(主题)

- 三态:`'light' | 'dark' | 'system'`,初值从 `localStorage['ze.theme']` 读,缺省 `system`。
- 应用:effect 把解析后的明暗写到 `document.documentElement` 的 `data-theme`(`light`/`dark`);`tokens.css` 的 `[data-theme="dark"]` 接管整套 token,无需组件感知。
- `system`:经 `matchMedia('(prefers-color-scheme: dark)')` 解析,并**监听 OS 切换实时重应用**(组件卸载时 `removeEventListener`)。
- `setTheme` 写 state + `localStorage`;localStorage 不可用时静默降级(不抛)。

## §3 Locale(语言)

- locale 的**真源在 05-i18n 的模块级 store**(`localStorage['ze.locale']`),不是本叶子的 React state —— 这样非 React 代码(handler / `getT`)与组件读同一个值。
- 本叶子用 `useSyncExternalStore(subscribeLocale, getLocale)` 订阅 → store 变即重渲染。
- effect 把 locale 写 `<html lang>`(`zh`→`zh-CN`,其余→`en`)。
- `setLocale` 透传到 store 的 `setLocale`。

## §4 Personal view state

- `ViewStateScope` 四轴缺一不可；key 对每段 URI 编码，envelope 再逐字段核对 scope，避免分隔符碰撞或错误 key 读取另一作用域。
- envelope 和 slice 分别有版本。slice 只有通过自己的 codec decode/migrate 后才可返回；迁移成功会回写当前版本，未知/损坏数据只回落默认值。
- `useScopedViewState` 的 identity 同时包含 scope、slice、codec version 与 storage authority。scope/storage 切换的当前 render 直接使用新 authority；被动同步用条件式 functional update，不能覆盖新 identity 已经发生的用户写入。
- 旧 identity 捕获的异步 setter/reset 在 scope 或 storage authority 切换后必须失效；同一用户动作内的多个 functional update 必须顺序合成，不能因 React 批处理互相覆盖。
- `setValue(..., {persist:false})` 只更新当前 UI，不把自动计算的默认值冒充用户偏好。Canvas2D 首开 auto-fit 必须走这条瞬时路径；用户 pan/zoom/显式 fit 才持久化。
- `resetValue()` 只删除当前 slice，保留同 slot 的其它偏好。Scene3D reset 用它清个人相机，再回 entity transient 初值或 renderer home；自动 home 不可回写成用户偏好。
- disclosure 身份只允许显式 `ownerId × contextId × stateKey`，Property/Form 的 `stateKey` 固定来自 `InspectorSectionSpec.id`。标题、翻译值和数组序号永远不能充当身份；重复 section id 整份声明拒绝。
- 无稳定 ID 的 ParamPanel“高级项”和 FormZone `x-group` 仍只在本次挂载内折叠，不制造不可迁移的伪持久化 key。
- 当前默认 storage 是 `localStorage`：这是 per-device 体验状态，不是权限边界，也不承诺跨设备同步。未来云端实现必须保持同一 scope/slice/version 合同，并由登录用户鉴权。

## §5 翻译 hook

- `useT()` 返回 `(key, params?) => string`,内部 `translate(locale, key, params)`;**仅 locale 变时换新引用**(`useCallback([locale])`),避免把 `t` 当 prop/deps 的组件无谓重渲染。
- `Trans` 处理富文本占位(`**bold**` / `<kbd>`),颜色一律走 `var(--ze-*)`(过 css 门禁)。

## §6 装配契约(App 必须做的)

1. 顶层包 `<PreferencesProvider>`(在 AuthProvider 之外,登录页也要能切主题/语言)。
2. `setModMessages(modZh, modEn)` 注入业务字典(`mod.*` 命名空间)。
3. 把 `theme/onThemeChange/locale/onLocaleChange` 传给 `HeaderShell` —— header 的主题/语言控件走 **onChange 回调**,不再 dispatch 到后端。
4. `SlotContext` 注入真实 `user.id + skillId + projectId`；slot adapter 追加 `slotId` 后才能启用持久视图态。缺任一轴必须降级为内存态，不能用无用户 key 共享数据。

> 历史坑(已修):早期 theme/locale 经 header dispatch 打到后端 `/api/command`(无此命令 → 静默 no-op),夜间模式/语言其实**没生效**。现由本 Provider 直接应用 + 持久化。详见 [_protocol/MIGRATIONS.md](../../_protocol/MIGRATIONS.md) 与 [05-i18n/MIGRATIONS.md](../../../../05-i18n/MIGRATIONS.md)。

## §7 与契约/复制的关系

- 本叶子是 core 的一部分,随 `01-core` 整树复制；所有 regular file 都进入与复制规则同源的 core managed inventory（改了走 compat_bump，见 [scripts/skill_sync.py](../../../../scripts/skill_sync.py)）。
- 依赖 `@ze/i18n` 别名(指向顶层 [05-i18n](../../../../05-i18n/));core 是 i18n 的下游(单向无环)。

## §8 验证

自动测试覆盖 view-state envelope/hook、批处理与 stale-setter、Gallery codec、Canvas2D viewport、Scene3D personal/domain/reset/debounce，以及 Property/Form 稳定分区折叠；真实浏览器冒烟 [e2e_preferences.py](./e2e_preferences.py) 继续覆盖:
1. 切换语言 → 界面文案随 locale 变;
2. 切暗色 → `<html data-theme="dark"]`,computed token 是暗色 oklch 值(非白底白字);
3. 刷新 → 主题 + 语言都从 localStorage 还原;
4. 新会话预置 locale 生效(登录页即已本地化)。

里程碑：theme、locale、Gallery、Canvas2D、Scene3D 个人相机与显式稳定分区折叠已接入同一层。当前 authority 仍是本机 `localStorage`；不得把个人视图记忆宣称成跨设备云同步或飞书式保存完成。
