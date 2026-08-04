# 01-core/04-user-facing — 人用的

UI 框架 + 交互体验 + 身份验证 + 用户偏好，给真人在浏览器里用。

`index.ts` 是本层的**稳定门面 barrel**(B3 门面收口,core 3.54.0):webapp 装配层与下游
`02-mod/05-renderers.tsx` 统一 `import ... from '@ze/core/04-user-facing'`;按需显式导出、
不 `export *`,旧深路径继续可用。收录纪律见该文件头注释。

## 子节点

1. [01-layout/](./01-layout/) — 界面布局（Header / Footer / Editor 三大区域 + 编辑器组合规则）**[已实装]**
2. [02-interaction/](./02-interaction/) — 交互体验（快反馈 / 乐观 UI / 进度可见 / 取消按钮 / 错误清晰 / 队列位置 / trace-id）**[空壳:仅 README+SPEC,无实现;乐观 UI/进度/错误等当前由 01-layout + useDocument 就地承担]**
3. [03-authentication/](./03-authentication/) — 身份验证（登录 / 角色 / 实体级 ACL）**[已实装:真账号 + 多项目按人隔离 + 协作共享]**
5. [05-preferences/](./05-preferences/) — 用户偏好（主题 light/dark/system + 语言 zh/en；穿戴 [05-i18n](../../../05-i18n/) 词表 + 应用暗色 token）**[已实装]**

> 编号缺 `04-`:历史预留空号,从未落地;偏好层接通时落在 `05-preferences`。
>
> SPEC §3「字段级 ACL / 注入防护 / 通信加密 / 日志防篡改 / 离线加密」是目标态设计,当前实装聚焦"登录 + 项目级 ACL"(内网单机够用);企业级多租户项判定 YAGNI 不做。
