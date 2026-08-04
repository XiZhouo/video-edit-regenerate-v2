# 05-preferences — 用户偏好与个人视图状态

> 纯前端叶子。统一承载主题、语言，以及不属于领域数据的个人视图状态。词表 / resolver 本体在 `@ze/i18n`(纯 TS 层,无 React)；本目录负责 React 接线、版本化本地持久化与作用域隔离。

完整规则看 [SPEC.md](./SPEC.md)。

## 在框架里的位置

- 上一级:[04-user-facing/](../) — 人用的(布局 / 交互 / 身份 / **偏好**)
- 孪生层:[03-css-rules/](../../../03-css-rules/)("看")+ [04-ops/](../../../04-ops/)("配")+ [05-i18n/](../../../05-i18n/)("说");本叶子是把 i18n 的"说"和 css 的暗色 token 在运行时真正接到 `<html>` 上的那只手
- 为什么是 05 不是 04:`04-user-facing/04-*` 是历史预留空号(从未落地),偏好层接通时落在 05

## 导出(从 `@ze/core/04-user-facing/05-preferences`)

| 符号 | 用途 |
|---|---|
| `PreferencesProvider` | 顶层 Provider:持有 theme state + 订 i18n locale store,effect 写 `<html data-theme>` / `<html lang>` |
| `usePreferences` | 取 `{ theme, setTheme, locale, setLocale }` |
| `useTheme` / `useLocale` | 上者的窄切片 |
| `useT` | 翻译 hook:`const t = useT(); t('common.save')`(locale 变即重渲染)|
| `Trans` | 富文本翻译组件(`**bold**` / `<kbd>`)|
| `useScopedViewState` | 读取/更新 `user × skill × project × slot` 的版本化视图 slice；瞬时默认可明确不持久化 |
| `readViewStateSlice` / `writeViewStateSlice` | 非组件或测试使用的同一份严格 envelope/slice 编解码入口 |
| `ViewStateScope` / `ViewStateSliceCodec` | 作用域与每类视图状态的版本、校验、迁移合同 |
| `getT` / `setModMessages` / 类型 | 从 `@ze/i18n` 再导出的便利出口(非组件代码 / App 注入 mod 字典用)|

## 边界(为什么拆成两层)

- **05-i18n**(纯 TS):词表 `zh.ts`/`en.ts` + `translate` + 模块级 locale store。无 React,可被 handler / 测试直接调。
- **05-preferences**(本叶子,React):`useSyncExternalStore` 订上面的 store + DOM 应用 + theme 持久化；同时拥有个人视图状态的唯一存储 envelope。穿 class、用 hook 的都在这层(core 侧)。

## 个人视图状态

- 完整作用域固定为 `userId + skillId + projectId + slotId`，key 分段编码，envelope 内再次保存并严格核对同一 scope。
- 每个 slice 自带版本与迁移器；损坏、未知版本或不可用 storage 均回落默认值，不污染领域 Document。
- scope 切换的第一个 render 就读取新 scope，不允许旧项目状态闪现或成为新写入的基值。
- 初次 auto-fit 是瞬时默认，只有用户 pan/zoom/显式 fit 后才落偏好。
- 当前 authority 是浏览器 `localStorage`，所以它是 per-device 偏好隔离，不是 ACL 或跨设备云同步安全边界；后续云同步必须复用同一 scope/slice 合同并由服务端鉴权，不能另造第二套状态形状。

## 验证方式

`viewStateStore.test.ts` / `viewStateStoreHook.test.ts` 覆盖 scope 隔离、版本迁移、损坏降级、首帧切换与 effect 竞态；Gallery/Canvas2D 各有 codec/viewport 测试。主题和语言仍由真实浏览器冒烟 [e2e_preferences.py](./e2e_preferences.py) 验证切换、刷新和 computed token。

## 怎么用

主题 / 语言开箱即用:框架 App.tsx 顶层包 `<PreferencesProvider>`,Header 设置菜单切主题(light/dark/system → `<html data-theme>`,暗色 token 自动接管)和语言(→ `<html lang>` + 订阅组件重渲染),都落 localStorage——mod 零代码。mod 只管业务词表:

```ts
// ① 02-mod/09-i18n.ts 按 locale 导出业务字典(App 启动 setModMessages(messages) 合并进 mod.* 命名空间)
export const messages = {
  zh: { entity: { item: '待办' } },
  en: { entity: { item: 'Item' } },
}

// ② mod 组件里取词(locale 变即重渲染;非 React 代码用 @ze/i18n 的 getT())
import { useT } from '@ze/core/04-user-facing/05-preferences'
const t = useT()
t('mod.entity.item')
```

某 locale 缺译时整体回落默认语言,不崩。完整规则见 [SPEC.md](./SPEC.md)。
