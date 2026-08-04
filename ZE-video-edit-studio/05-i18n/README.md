# 05-i18n — 国际化(说)

ZE skill 共享的**字串词表层**。跟 [03-css-rules](../03-css-rules/)(看)、[04-ops](../04-ops/)(配)平级,独立成块、独立复制替换。

> 一句话:组件只发 `t('ns.key')`,字典只定义这套 key;由 `scripts/i18n_contract_check.py` 守一致。

## 是什么 / 不是什么
- **是**:纯 TS 字典(`zh.ts` 真源 + typed `en.ts` 镜像)+ `t()/getT/translate` resolver + locale 观察者。**无 React**。
- **不是**:React glue(`useT` / `Trans` / `PreferencesProvider`、`<html lang/data-theme>` 应用、theme)—— 那些是 core 的 `04-user-facing/05-preferences/`(穿 class 的组件在 core,正如 css 是纯 .css)。

## 文件
- `locales.ts` — **语言注册表(增减语言唯一真源)**:`LOCALE_CODES` / `LOCALE_META`(htmlLang+dict)/ `DEFAULT_LOCALE`;`Locale` 类型从它派生
- `zh.ts` — 中文字典(`as const` 唯一真源,嵌套 namespace)
- `en.ts` — 英文字典(`as Dict` 强镜像,缺 key 编译失败)
- `index.ts` — `t/getT/translate` + `Dict`/`I18nKey`/`TKey` + `setModMessages(byLocale)` + re-export 注册表
- `locale-store.ts` — 模块级 locale 观察者(state + localStorage `ze.locale` + subscribe)
- `detect.ts` — 首次 locale 解析(localStorage→`DEFAULT_LOCALE`)
- `_contract.json` / `MIGRATIONS.md` — 层契约 + 迁移记录

## 增减语言(直接完成)
1. `locales.ts` 的 `LOCALE_CODES` 加/删一个码(如 `'ja'`)、`LOCALE_META` 加/删对应项(`htmlLang` + `dict`)。
2. 新建 / 删除 `05-i18n/<code>.ts`(`as Dict`,tsc 强制补全所有 key)。
3. 各 skill 在自己的 `02-mod/09-i18n.ts` 的 `messages` map 里加该语言的业务译文(没加也不崩,`mod.*` 回落 `DEFAULT_LOCALE`)。
其余(`Locale` 类型 / `DICTS` / detect / `<html lang>` / 设置菜单语言项)全部自动跟随,无需逐处改。

## 加一句文案(标准三步)
1. 组件里直接写 `t('editor.foo')`(框架 key)或 `t('mod.xxx')`(业务 key)。
2. 跑 `python scripts/i18n_sync.py --fill` —— `foo` 自动出现在每本词表(占位 `TODO:editor.foo`)。
3. 把 `TODO:` 占位换成真实译文。
门禁:`i18n_contract_check.py`(UI 代码默认零裸中文)+ `i18n_audit.py`(用了没定义 / 各语言没对齐 / 注册表不一致)双卡,都并入 `verify_all`。

## 用法
```ts
// 组件(经 core 的 React hook)
import { useT } from '@ze/core/04-user-facing/05-preferences'
const t = useT(); <span>{t('common.save')}</span>

// 非组件(service / handler)
import { getT } from '@ze/i18n'
getT()('common.loading')
```

## 边界
- 框架字串在本层(随 sync 替换);**业务字串**在 `02-mod/09-i18n.ts`(`mod.*`,App 经 `setModMessages` 注入合并)—— i18n 版的 `07-theme.css`。
- locale 是**用户偏好**,存 localStorage(`ze.locale`),**不进** `.data/config.json`(配置中枢只管部署/工具配置)。

## 校验标准(双向门禁)
UI chrome 字串必须走 `t('ns.key')`;**裸写任何语言都违规**,两个反向门禁各守一向:
- `scripts/i18n_contract_check.py` —— **零裸中文**:全扫 `01-core/04-user-facing` / `webapp/src` / `02-mod/05-code` 的 .ts/.tsx,代码区(剥注释后)出现 CJK 字面量即违规(逼中文进字典)。豁免:词表(`05-i18n/*`)、mod 业务 i18n(`02-mod/09-i18n`)、写进 Document 的 seed(`02-mod/04-layout.config`)、demo harness(`webapp/src/gallery/`)。
- `scripts/i18n_english_check.py` —— **零裸英文 chrome**:只扫纯 chrome 目录(`01-layout/01-header`、`02-footer`、`03-authentication`),查 JSX 文本节点 + `title` / `aria-label` / `placeholder` 属性的**字面量**;出现连续 ≥2 ASCII 字母且非 `{t(...)}` 即违规。allowlist:语言中立单位 / 缩写(ms、px、id、3d…)+ snake_case 标识符(`doc_id`,属数据边界)+ 几何符号(▶ ⚠ — # …非 ASCII 字母,天然放行)。**不扫 editors**——它们显示 data 标识,扫了必误报。
- 再加 `scripts/i18n_audit.py`(key 一致:用了没定义 / zh↔en parity / 注册表)。三者并入 `verify_all`。**新增组件默认即被双向门禁守,无需登记**。

## chrome vs 数据 边界(谁翻译、谁不翻译)
把「界面外壳」与「文档内容」分清,是不混语言的根本(见 [editor-architecture] 的 `Document≠UI`):
- **UI chrome → 必 i18n**:标题、空态、按钮、状态、tab、字段 / 列的**显示文字**、提示(title / aria / placeholder)。走上方「加一句文案(标准三步)」。
- **数据 → 不翻译**:实体类型 id(`box` / `shot`)、schema 字段 key(`x` / `color`)、`entity.name`、写进 Document 的 seed —— 它们是 Document 内容,不是 chrome。故 `i18n_contract` 对 seed / 词表豁免、`i18n_english` 对 snake_case 标识符豁免。
- **数据的「显示名」走 schema `label`(不是翻译 key)**:字段显示名 = `properties[f].label`,类型显示名 = schema 顶层 `label`;**框架透传、mod 填**(语言中立,mod 给什么显什么)。分工:chrome 文字用 `t()`,数据显示名用 `label`,两者不混用。
  - *取舍*:`label` 是 mod 写死的固定值,**不随 locale 切换**(框架只透传)。zh 单语场景填中文即消除混合;真双语 skill 需自给 locale 感知的 `label`(在 mod 层按当前 locale 选值)。

## 设计 / 扩展准则
- **命名空间复用**:同类 chrome 复用既有 ns(footer 段共用 `footer.*`、同步态共用 `sync.*`),别另造平行 key。
- **zh 真源 + en 强镜像**:`zh.ts` 是 shape 真源(`as const`),`en.ts`(`as Dict`)缺一个 key 即编译失败 —— 翻译不会偷偷掉队。
- **mod 业务串**走 `02-mod/09-i18n.ts`(`mod.*`),不塞进本层框架词表。
- **本层 + 门禁脚本经 Skill.Sync 整块下发到所有 skill** —— 写在这里的规则即全系列标准;新 skill 出生即继承双向门禁,无历史包袱。

[editor-architecture]: ../01-core/README.md
