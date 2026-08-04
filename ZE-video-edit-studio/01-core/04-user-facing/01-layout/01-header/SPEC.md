# Header — 工作台顶部固定栏

## 1. 这是什么

ZE 工作台**屏幕最顶部的固定栏** —— 容纳跟"当前 doc 工程级"操作和"全 skill 级"系统设置:文件 / 设置 / 帮助菜单、项目名、状态指示(主题/夜间模式收在设置菜单内)。

业务白话视角:Header 是用户进入任何 ZE 工作台第一眼看到的"标题栏 + 系统菜单"。不属于任何 editor,属于整张工作台框架;切换 doc / 切换 editor 时 Header 不变,只内容更新。

Header 跟中区 [editor](../03-editor/SPEC.md) 和底部 [Footer](../02-footer/SPEC.md) **平级**,共同构成 layout 三大区。

## 2. 边界与接口契约

### 2.1 跟其他叶子的关系

```
Header (本叶子, L4)
   |
   ├─ 调用 01-command (文件菜单触发的 new/open/snapshot/rename/export/delete/close 都走 command)
   ├─ 调用 01-document (当前 doc 名、版本)
   ├─ 调用 04-history-rollback (Ctrl+S 创建命名快照)
   ├─ 调用 03-authentication (用户头像 / 登录 / 登出)
   ├─ 订阅 02-data-flow/01-realtime-sync (同步状态指示;live_collab 下无 unsaved 概念)
   ├─ 切换 L1 token 主题 (light/dark/system)
   ├─ 显示 L7 Interaction 的全局错误计数 / trace-id 摘要
   └─ 内部组件全部用 L2 .ze-* (button / menu-item / popover / icon-button / divider / avatar)
```

不直接访问 entity / asset / oplog —— 全部通过 command dispatch 间接走。

### 2.2 三段布局(header-v2,1.31.0)

```
┌──────────────────────────────────────────────────────────────────────┐
│ skill-name(衬线字标)      │   file-name ▾  ●sync   │  ⚙  ?  avatar  │
│ ←──────── 左 ──────────  │ ←──────  中  ──────→   │ ←──── 右 ────→ │
└──────────────────────────────────────────────────────────────────────┘
```

| 段 | 宽 | default 内容 |
|---|---|---|
| **左** | 1fr | 衬线品牌字标(`--ze-font-display`,唯一消费点);**无菜单** |
| **中** | auto(绝对居中) | **文件名 = 文件切换器 trigger**(点击开下拉:文件列表 + 行内操作 + 其他文件操作)+ 同步灯 |
| **右** | 1fr | 设置菜单(icon 令牌)+ 帮助菜单(icon 令牌)+ 用户头像(**含成员/共享面板**) |

变更史:v1 左段=图标+名+中段文件菜单;v4(1.30.0) FileMenu 移入左段;**v2(1.31.0) FileMenu 退役**,
文件操作整体并入中段文件切换器,「共享」按钮并入头像菜单。中段仍是 mod 自定义最频繁的区域。

### 2.3 默认功能详单

> 每项展开为:位置 / 显示 / 触发 / 逻辑 / 边界 5 个维度。逐项可批注。

#### 左段

##### 1. skill 图标

- **位置**:Header 左段最左
- **显示**:36×36px,mod 声明的 SVG icon(单色,可被 token 着色)
- **触发**:M1 不可点击(纯标识);M2 点击 → 关闭当前 doc + 回项目选择页
- **数据源**:`02-mod/04-layout.md` 的 `header.skill_icon: <path>`;无声明走 ZE 通用图标
- **边界**:icon 文件丢失 / 加载失败 → fallback 为 skill 名首字母圆形头像

##### 2. skill 名

- **位置**:左段紧贴图标右
- **显示**:mod 声明的人类可读 skill 名,`--ze-fs-md` + `--ze-fw-semibold`
- **触发**:不可点击;hover 显 tooltip(从 SKILL.md description 取 1 句简介)
- **数据源**:`02-mod/04-layout.md` 的 `header.skill_name: <str>`;默认走 SKILL.md 的 `name` 字段
- **边界**:超长(>20 字)右截断 `...`,完整名在 hover tooltip 里

#### 中段

##### 3. 项目名(doc.name)

- **位置**:中段最左
- **显示**:当前 `Document.name`(无名 doc 显占位 `<未命名>`,刚启动 / 未打开 doc 时显 `<未打开任何项目>`)
- **数据源**:订阅 `01-document`,doc 切换 / rename 时自动更新
- **触发**:M1 只读 + hover 显完整数据目录路径(`<skill>-data/projects/<doc_id>/`);M2 点击 → 进入 inline 重命名
- **边界**:超长(>40 字)中间截断 `xxx...yyy`;rename 走 command `document.rename`,并携带当前已渲染的 Document `expected_version`;CAS 冲突时拉取权威快照,不覆盖协作者的新名字

##### 4. 同步状态指示

> 新 ZE v3 默认走 `live_collab + auto_commit`。保存状态与协作同步是两条独立状态：
> 前者回答 canonical 是否耐久，后者回答事件流是否追上；框架不提供手动保存模式。

- **位置**:项目名后紧贴
- **显示**:小圆点 ●,**仅在非默认状态时显示**:
  - `in-sync`(真实同步源确认后)→ 不显(避免视觉噪音)
  - `syncing`(本地有改动正在广播给远端)→ 蓝色 ●(`--ze-semantic-info`)
  - `delayed`(广播延迟 > 2s)→ 黄色 ●(`--ze-semantic-warn`)
  - `conflict`(远端冲突待解决)→ 红色 ●(`--ze-semantic-danger`)
- **数据源**:订阅 `02-data-flow/01-realtime-sync.state`
- **触发**:点击 ● → 跳 Footer 展开"同步详情"面板,查看具体延迟 / 冲突详情
- **边界**:服务器完全失联(`offline`)走 Footer 服务器状态 segment,不进本指示器;hover ● 显当前状态文字描述

##### 5. 文件切换器(中段文件名下拉,header-v2)

- **位置**:中段;trigger = 当前文件名本体(`.ze-header__file-trigger`,文件名 + caret)
- **显示**:点击展开 popover(`data-anchor="center" data-wide`),两区:
  - **文件列表区**(`.ze-header__file-list`):当前用户可见的全部项目,**最近打开(LRU)在前,
    余按 updated_at 倒序**;当前文件行 `data-current` 高亮 + ✓;每行 hover/focus 出
    **行内按钮组**(icon 令牌 + aria-label):重命名 `edit`(5.8)/ 复制 `copy`(5.10)/
    另存为 `save-as`(5.4)/ 删除 `trash`(5.9,danger)。**点击行 = 切换打开该文件**(写 LRU)。
  - **操作区**(底部 MenuGroup):新建(5.1)/ 导入(5.5b)/ 导出(5.5)/ 创建快照(5.3)/
    关闭(5.6)+ mod `file_menu_append` 追加项(重载点 2 落点)。
- **触发**:点击文件名;Esc / 点空白 / 选行后自动关;打开瞬间刷新列表(无 SSE 广播,开即拉)
- **数据源**:列表经 HeaderShell 新 prop `projects`(App 从 03-authentication `useProject().projects`
  注入);LRU 存 `localStorage['ze.recent.<uid>']`,在 ProjectContext.openProject 唯一漏斗写入
- **边界**:未打开 doc 时 trigger 显占位文案、**仍可开列表**;操作区中快照/导出/关闭无 doc 灰显,
  conflict 时快照灰显(同 5.3 既有规则);行内按钮受 hide 白名单 gate(重载点 4);
  最近列表长度 = `recent_max`(5..50,默认 10),仅影响 LRU 优先段,不截全量列表
- 旧形态(左段「文件 ▾」九项菜单 + 5.7 最近项目 submenu)自 1.31.0 退役;
  5.2「打开」对话框降级为兜底(doc_id 直开),不再是主路径

###### 5.1 新建

- **快捷键**:`Ctrl/Cmd+N`
- **逻辑流**:
  1. 若 mod 只声明 1 种 schema → 跳到 3
  2. 若声明多种 → 弹 `.ze-dialog` 让用户选 schema
  3. 检查当前 doc 是否 unsaved:若是,弹 3 选项 dialog "保存 / 不保存 / 取消"
  4. dispatch `document.new(schema)` → core 创建空 doc + 若 mod 注册了 demo_bootstrap 就跑一次
  5. Layout 切到该 doc 的 default LayoutTree(localStorage 无则取 mod default)
- **失败态**:磁盘写失败 → toast "无法创建:`<错误信息>`"

###### 5.2 打开

- **快捷键**:`Ctrl/Cmd+O`
- **逻辑流**:
  1. 扫 `<skill>-data/projects/` 目录,列出所有 `<doc_id>/document.json` 的 `(id, name, updated_at, _v)`
  2. 弹 `.ze-dialog` 显 doc 列表(按 `updated_at` 降序),含搜索框 + 单选
  3. 选中后:同 5.1.3 处理 unsaved
  4. dispatch `document.open(doc_id)` → core 关当前 + 打开新
  5. Layout 加载新 doc 的 LayoutTree(localStorage 持久化的 → 否则 mod default)
- **失败态**:doc 损坏(`document.json` 缺字段 / 校验失败)→ toast + 错误详情可点开 dialog

###### 5.3 创建快照

> live_collab 下改动已实时落盘,**这里不是"保存"** —— 是在 OpLog 里打一个**命名锚点**,作为 rollback 的稳定参照,顺便保留 `Ctrl/Cmd+S` 的肌肉记忆 + 给"我现在状态稳了"的心理锚。

- **显示**:子菜单 "创建快照..."
- **快捷键**:`Ctrl/Cmd+S`
- **启用条件**:doc 已打开 且 同步状态 ∈ `{in-sync, syncing}`(`conflict` 时灰显)
- **逻辑流**:
  1. 弹 `.ze-dialog` 让用户输入快照名(默认 = `header.snapshot_label_format` 渲染当前时间戳)+ 可选描述
  2. dispatch `document.snapshot(label, description?)` → core 通过 `04-history-rollback` 在 OpLog 当前位置打命名标记 + 持久化
  3. 成功 → toast "已创建快照: `<label>`";Footer 快照列表新增该项
  4. 失败(同步冲突)→ dialog "本地有未解决冲突,请先在 Footer 同步详情面板中解决"
- **关联**:undo / redo / rollback 走 `04-history-rollback` 的命令面板,**不依赖快照** —— OpLog 任意位置都可回;快照只是给长会话提供易识别的"版本"锚点
- **边界**:快照名重复 → 自动加 `(2)` `(3)` 后缀;创建快照不影响当前编辑(非阻塞)

###### 5.4 另存为

- **快捷键**:`Ctrl/Cmd+Shift+S`
- **逻辑流**:
  1. 弹 dialog 让用户输入新 `doc_id`(默认 `<原 doc_id>-copy`)+ 新 `name`(默认 `<原 name> (副本)`)
  2. 实时校验 `doc_id` 冲突(扫 `<skill>-data/projects/<new_id>/` 存在否),冲突 → 红字 + 禁止确认
  3. dispatch `document.save_as(new_doc_id, new_name)` → core 复制当前 doc 的 `document.json` + `entities/`(`assets/` 因内容寻址不复制 blob,只增引用计数)
  4. 关闭当前 doc + 打开副本
- **边界**:若当前 doc unsaved → 先弹 "保存原 doc 后再另存吗?"(避免漏改),用户拒绝则只复制磁盘最新版

###### 5.5 导出

- **逻辑流**:
  1. 弹 dialog 选导出格式:
     - **JSON**:整 doc 单文件(`document.json` + entities 嵌入 + assets 用 base64 嵌入或外链)
     - **ZIP**:完整目录打包(含 `assets/blobs/` 原文件,体积大但可还原)
     - **mod 自定义**:mod 在 `02-mod/04-layout.md` 声明额外格式
  2. 选输出路径(浏览器走 download API;桌面端走文件选择器)
  3. dispatch `document.export(format, target_path)` → core 序列化 → 输出
- **边界**:大 doc(>100MB)走 L7 长任务规则 — 显进度条 + 可取消(进 Footer pipeline 列表)

###### 5.6 关闭

- **快捷键**:`Ctrl/Cmd+W`(若 mod 没占用,需 KeymapManager 拦截浏览器默认)
- **逻辑流**:
  1. 检查同步状态:
     - `in-sync` → 直接关
     - `syncing` / `delayed` → 弹 dialog "有本地改动正在同步",3 选项:等同步完成再关 / 强制关(可能丢未广播改动)/ 取消
     - `conflict` → 弹 dialog "有未解决冲突",3 选项:跳 Footer 解决 / 强制关(放弃本地改动)/ 取消
  2. dispatch `document.close` → core 释放内存 entity + 关闭 sync 通道 + 广播 "本人下线" 给协作者
  3. Layout 回 "无 doc" 状态:中区显项目选择页(welcome page),Footer skill 标识改 "未打开任何项目"
- **边界**:Ctrl+W 在某些浏览器是关 tab,KeymapManager 必须 `preventDefault`

###### 5.7 最近项目

- **显示**:子菜单 "最近项目 ▸",hover 展开 submenu;submenu 最多列 10 项(LRU,mod 可改长度)
- **形态(header-v2)**:不再是独立 submenu —— LRU 只决定 5 号文件列表的**排序优先段**:
  LRU 命中的项目按最近打开序排前,未命中的按 updated_at 倒序接后
- **数据源**:`localStorage["ze.recent.<user_id>"]` 数组(按用户隔离),在
  `03-authentication/ProjectContext.openProject`(所有打开动作唯一漏斗)写入:进队列头、
  重复移头、超 N 踢尾(N = `header.recent_max`,默认 10,范围 5..50)
- **边界**:LRU 为空 → 列表纯 updated_at 倒序;已不可见的项目(被删/被移出协作)合并时自动过滤

###### 5.8 重命名

- **显示(header-v2)**:文件列表行内按钮 `edit`(任意行,不限当前文件)
- **启用条件**:行存在;受 `hide: ['rename']` 白名单 gate(重载点 4)
- **逻辑流**:
  1. 弹 `.ze-dialog` 输入新 name(预填当前 `doc.name`,自动全选)
  2. 若是非当前行,先 GET 该目标的权威 Document,以其 `version` 作为一次性的 CAS 基线;dispatch `document.rename({doc_id, name, expected_version})`(行级目标;缺 doc_id 回退当前 doc)→ core 改 `Document.name` + 原子提交 + 广播给协作者。`409` 后只刷新,不得拿新版本自动重放旧名字
  3. Header 项目名(3 号)+ 最近项目列表(5.7)+ Footer skill 标识同步更新
- **关联**:跟中段 3 号"项目名"M2 的 inline 重命名是**同一个 command**(`document.rename`),只是入口不同
- **边界**:空字符串 → 红字禁止确认;超 100 字符 → 警告但允许(由用户决定)

###### 5.9 删除项目

- **显示(header-v2)**:文件列表行内按钮 `trash`(danger 色;任意行)
- **启用条件**:行存在;受 `hide: ['delete']` 白名单 gate;**服务端 owner-only 硬校验**(非 owner 的行不显删除钮,后端再校验一道)
- **逻辑流**:
  1. 弹强确认 `.ze-dialog`:
     - 显示项目名 + 数据目录完整路径 + 当前在线协作者数(>0 时额外警告"其他人正在编辑该项目")
     - **必须手动键入完整项目名**才启用"删除"按钮(防误删,`header.delete_require_text_confirmation: true` 强制 on)
     - 警告文案:"此操作不可恢复;asset 引用计数清零的 blob 也会一并删除"
  2. 用户确认 → dispatch `document.delete(doc_id)` → core:
     - 关闭当前 doc 的 sync 通道 + 广播 `doc_deleted` 事件给其他协作者
     - 删整个 `<skill>-data/projects/<doc_id>/` 目录(原子操作)
     - 释放 asset_refs;`ref_count=0` 的 blob 走 AssetStore GC 删除
     - 从最近项目列表移除该 `doc_id`
  3. Layout 回 "无 doc" 状态;Footer skill 标识改 "未打开任何项目"
- **协作者侧的连锁反应**:其他端收到 `doc_deleted` → 自动 close + toast "项目已被 `<who>` 删除"(强制踢出,不留任何挽回选项)
- **边界**:header-v2 起支持删除任意行(不必先打开);删除当前文件 → Layout 回"无 doc"态;红线 `delete_require_text_confirmation` 恒 true,行内入口同样弹手打全名确认

###### 5.10 复制(duplicate,header-v2 新增)

- **显示**:文件列表行内按钮 `copy`(任意行)
- **逻辑流**:dispatch `document.duplicate({doc_id})` → App 拦截 → `POST /api/projects/{pid}/duplicate`
  (服务端 copy 项目目录 + 重写 document.json 的 id/name/acl,新副本 owner = 操作者)→
  refresh 列表 + 打开新副本
- **与另存为(5.4)的分工**:复制 = 一键带"副本"后缀;另存为 = 行级带名复制(弹输入框起名),
  二者同走 duplicate 端点,只差是否询问命名
- **边界**:大项目同步复制有阻塞(v1 接受,MIGRATIONS 注记后续 task 化);非项目成员不可复制

#### 右段

##### 6. 设置菜单(下拉容器)

- **位置**:右段左侧
- **显示**:icon 令牌 `settings`(`ZeIcon`,调节滑杆造型;1.29 起禁字符表情)
- **触发**:点击 / `Alt+,`;Esc / 点空白关闭
- **子项**:4 条(6.1 - 6.4)

###### 6.1 主题

- **显示**:submenu 3 选项 "亮 / 暗 / 跟随系统",当前主题前显 ✓
- **逻辑流**:`onThemeChange(next)` → PreferencesProvider(05-preferences)写 `<html data-theme>`(system 经 matchMedia 解析)→ L1 token 自动适配 → 组件无需重渲染。**偏好不走 command/dispatch**(属 Rendering Hint、非 domain data、不可 undo)。
- **持久化**:`localStorage["ze.theme"]`,跨 session 保持
- **关联**:主题(夜间模式)只在此处切换;早先的右段独立 ◐ 控件(原 8 号)已移除,避免页面与设置菜单重复入口

###### 6.2 语言

- **显示**:submenu(中 / 英;mod 可声明额外语种)
- **逻辑流**:`onLocaleChange(zh|en)` → PreferencesProvider 写 `<html lang>` + 05-i18n locale store → `useT` 订阅触发组件 re-render(文本变,非 token 变)。同样**不走 command**。
- **持久化**:`localStorage["ze.locale"]`
- **边界**:i18n 资源缺失某 key → fallback 到 `en`,fallback 失败再 fallback 到 key 本身(显出来让维护者看到)

###### 6.3 数据目录

- **显示**:当前 `ZE_DATA_DIR` 路径(只读 monospace,`--ze-fs-sm` + `--ze-font-mono`)+ 右侧 📋 复制按钮
- **触发**:点 📋 → 复制完整路径到剪贴板 + toast "已复制"
- **数据源**:服务端启动时计算的 `DATA_DIR`(`server.py` 里),通过 API `/system/info` 取
- **边界**:M1 只读;M2 加 "切换数据目录" 按钮(弹路径 picker + 重启 skill 提示)

###### 6.4 协作状态【直接都实时协作就行，不管是部署到服务器多人打开，还是一个人本地打开多个网页窗口都算。实时协作的模式下，保存相关系统怎么处理需要你思考下】

> ZE v3 所有 mod 统一走 `live_collab` —— 不论部署到服务器多人开还是单人本地多 tab 都算同一种 mode。**"保存" 动作不存在** —— 每次改动通过 command → operation → 立即落盘 + 广播;`Ctrl+S` 改成"创建快照"(5.3),"是否已同步" 走 §4 同步状态指示。

- **显示**:`.ze-kv-list` 三行只读详情:
  - 模式 — "实时协作"(固定,只一种)
  - 在线协作者 — `N`(当前同 doc 的其他活跃 user / tab;不含自己)
  - 同步延迟 — `<ms>` ms(本地改动到 canonical 落盘的 P50 延迟,滚动窗 60s)
- **数据源**:订阅 `02-data-flow/01-realtime-sync.state`
- **触发**:M1 只读;M2 点击"协作者数"→ 跳 Header 右段协作头像列(M3)
- **设计说明**(给开发者):
  - 改动 = command → operation → 立即落盘 + 广播,无显式 "save"
  - snapshot = OpLog 上的命名锚点,跟落盘解耦(快照是给人的"我现在到这里了")
  - undo / redo / rollback 走 OpLog,跟 save / snapshot 都没关系
  - 协作冲突走 `02-conflict-resolution`,UI 在 Footer 展开"同步详情"
- **下游依赖**:`01-document SPEC §4` 保留 `live_collab` / `with_save` / `realtime_checkpoint` 以读取历史项目；新 scaffold 与 build-v3 mod 声明 `live_collab`，并由独立 `save_policy=auto_commit` 保证耐久。Header 的同步指示不能代替 Footer 的保存状态。

##### 7. 帮助菜单(下拉容器)

- **位置**:右段中间
- **显示**:icon 令牌 `help`(`ZeIcon`)
- **触发**:点击 / `F1`
- **子项**:4 条(7.1 - 7.4)

###### 7.1 操作手册

- **逻辑流**:dispatch `system.help.manual` → `ManualDialog`。
- **显示形态**:**优先安全内联语义片段** —— `ManualDialog` 与项目选择页共用 `operationManual` loader/renderer，加载 `config.manual_html ?? '/operation-manual.html'`。片段必须只有一个真实 `.ze-om` 根；DOMPurify allowlist 只保留语义/媒体标签和 `ze-om*` class，移除脚本、事件、表单、SVG、嵌入页、`style/link` 与危险 URL；图片/视频等相对 URL 以最终响应 URL 为基准解析。净化后的片段只能由 `.ze-operation-manual` renderer 输出。
- **CSS 所有权**:片段不得携带 `<style>`、`style` 属性或工具私有 CSS；标题、列表、表格、代码、媒体、`details`、callout 的通用排版唯一归 `03-css-rules/02-atoms/typography/operation-manual`，且只消费 `--ze-*` token。
- **标准落点**:操作说明片段固定 `webapp/public/operation-manual.html`(前端按根路径 `/operation-manual.html` 提供,Skill.Sync 安全区);独立页 `operation-manual.page.html`;**唯一真源是 `webapp/src/product/operation-manual.zh.json`**。三件套由 **ZE-skill-tutorial-writer 操作说明工作台**产出/替换,**core 只渲染、不生成**。
- **回退**:片段取不到(404 / 无 `.ze-om`)→ 若 `header.manual: <md_path|url>` 有值则给外链;再无 → "操作手册未配置"提示。
- **数据源(回退链接)**:`02-mod/04-layout.md` 的 `header.manual` / `header.manual_html`(后者覆盖默认片段路径)。

###### 7.2 快捷键

- **快捷键**:`?`(单键,无修饰)
- **逻辑流**:dispatch `system.help.shortcuts` → 弹 floating-panel
- **显示**:聚合所有已注册 keybinding,来自 `01-command` 的 `CommandSpec.shortcut` 字段;按"全局 / 当前 editor / 当前 zone"3 组归类
- **边界**:mod 注册 command 时声明的 shortcut 自动入此表,无需额外注册

###### 7.3 版本

- **逻辑流**:dispatch `system.help.about` → 弹 dialog
- **显示信息**(逐行):
  - skill 名 + skill 版本(mod 在 `SKILL.md` / `pyproject.toml` 声明)
  - core 框架版本(本 SKILL `ZE-skill-build` 的 version)
  - 当前 git commit short hash(若 `.git/HEAD` 存在)
  - Python 版本 / Node 版本(if web)
  - 部署机名 + 启动时间(从服务端取)
- **边界**:版本信息缺失某项 → 该行显 `<未知>`,不报错

###### 7.4 反馈

- **逻辑流**:dispatch `system.help.feedback` → 在新标签页打开 mod 声明的反馈 URL
- **数据源**:`02-mod/04-layout.md` 的 `header.feedback_url`(GitHub issue 模板 / 飞书表单 / 内部 IM 群链接)
- **边界**:mod 未声明 → 子项灰显 + hover "尚未配置反馈渠道"

##### 8.(已移除)独立主题切换控件 ◐

> **历史**:早先右段在帮助菜单和头像之间有个独立 ◐ 三态切换控件,跟设置菜单 6.1 功能等价。
> 后判定为重复入口(同一偏好两处可改、徒增页面噪音),**已移除**:主题/夜间模式只在设置菜单 6.1 切换。
> 随之删去 `header.show_theme_toggle` 配置与 `hide: ['theme_toggle']` 白名单项(不再有可显隐的对象)。
> `theme`/`onThemeChange` 仍下传给设置菜单;`Theme` 类型统一来自 `05-preferences`(原先定义在 ThemeToggle 控件里)。
> 控件文件 `controls/ThemeToggle.tsx` 与字典 `theme.label.*` 已随之删除。

##### 9. 用户头像

- **位置**:Header 最右
- **显示**:
  - **已登录**:`.ze-avatar` 32px + hover 显用户名;avatar 走 `03-authentication.current_user.avatar_url`
  - **未登录**:`.ze-button` 文字 "登录" 或匿名圆形头像
- **触发(已登录,header-v2)**:点击展开下拉(`data-wide`):
  - 组 1:用户名 + email(只读)
  - 组 2:**成员面板**(`AclPanel`,role=group,原「共享」按钮并入):
    - 标题行(icon `share` + 「可参与编辑」);成员列表 = owner(徽记)+ collaborators
    - **人人可见列表(只读)**;owner 额外渲染:添加行(输入 + datalist autocomplete + 添加钮)
      与每行移除钮 → dispatch `document.set_acl({acl, expected_version})`,其中版本必须来自当前已渲染的 Document;经 SSE meta.acl 实时回流。`409` 后刷新成员列表,不得以旧 ACL 覆盖并发授权变更
  - 组 3:切换账号 / 登出 → dispatch `auth.switch` / `auth.logout`
- **变更史**:右段独立「共享」文字按钮(owner-only 自管 overlay)自 1.31.0 退役,
  共享能力收进头像菜单——右段只剩 设置/帮助/头像 三个控件
- **触发(未登录)**:点 "登录" → 弹登录 dialog(走 `03-authentication`)
- **M1**:仅匿名头像 + "登录" 按钮 placeholder(不实际跳转);完整登录走 `03-authentication` 启动后(M2 / M3)
- **边界**:authentication 服务不可达 → 显匿名 + hover "认证服务暂不可用"

### 2.4 高度策略

固定 28px(= L1 `--ze-space-7`)。视觉对齐 ZE-skill-build v1 topbar 紧凑度。

跨 skill 一致,**不允许 mod 改高度** —— Header 高度变化会影响中区计算,稳定值优先。

### 2.5 多 doc 处理

v3 总章明确"单 Document"(一个 skill 实例只装一份 doc)。所以 Header **不显示 doc tab**;切换 doc 走 5.2 打开对话框。

例外:某些 skill 需要"对照阅读" / "多版本对比" —— 这是中区 editor 的事(layout split 多 editor),不是 Header 多 doc。

### 2.6 mod 重载点

mod 可在 `02-mod/04-layout.md` 声明:

1. **替换 slot 内容**:中段插入业务面板切换 tab、左段改 skill 名 / 图标
2. **追加菜单项**:`file_menu_append` 落**文件下拉操作区末尾**(header-v2);设置 / 帮助菜单仍挂子项末尾
3. **追加右段按钮**:业务自定义动作(挂在帮助菜单和用户头像之间)
4. **隐藏可选子项**:`header.hide: ["feedback", "rename", "delete"]` 等(白名单)
5. **替换 manual / feedback_url / skill_icon / skill_name 等数据源**
6. **最近项目列表长度**:`header.recent_max: <N>`(默认 10,范围 5..50)
7. **快照默认名模板**:`header.snapshot_label_format: <pattern>`(默认 `YYYY-MM-DD HH:mm:ss`)
8. **删除二次文本确认**:`header.delete_require_text_confirmation: true`(默认 true,**不允许 mod 改 false**)

mod **不能**:改高度 / 删默认容器(文件切换器 / 设置 / 帮助 / 头像) / 改快照与 save 的关系(快照不是 save) / 关闭删除二次确认(防误删红线) / 让 Header 触发业务 mutation command(那是 editor 的事)。

### 2.7 跟其他叶子的接口

| 接口方向 | 接口点 |
|---|---|
| → `01-command` | 所有菜单 / 按钮触发都走 command dispatch |
| → `01-document` | 显示当前 doc 名、`_v`(订阅 doc 变化);触发 new / open / snapshot / save_as / rename / export / delete / **duplicate** / close |
| → `04-history-rollback` | `Ctrl+S` / 5.3 创建命名快照(snapshot 在 OpLog 当前位置打锚点);undo / redo / rollback 不经 Header,走命令面板 |
| ← `02-data-flow/01-realtime-sync` | 订阅同步状态(in-sync / syncing / delayed / conflict)→ 驱动 §4 指示器 + §6.4 协作状态 |
| → `03-authentication` | 用户头像 / 登录 / 登出 / 切换账号;**文件列表数据源**(App 把 `useProject().projects` 经 `projects` prop 注入)+ LRU 写入漏斗(`openProject`)+ AclPanel 的 `apiListUsers` |
| → L1 token | 设置菜单主题切换触发 `data-theme` 变化 |
| → L2 module | `.ze-button` / `.ze-popover` / `.ze-menu-item` / `.ze-icon-button` / `.ze-avatar` / `.ze-chip` / `.ze-kv-list` / `.ze-dialog` |
| → L7 Interaction | 错误计数 / trace-id 摘要(可点跳 Footer 详情);snapshot / rename / delete 走 toast 反馈;冲突走 dialog |

## 11. 里程碑

- **M1** — 三段布局 + 文件菜单 9 子项 + 设置 4 子项(含主题/语言)+ 帮助 4 子项 + 匿名头像 + 同步状态 ● + mod 中段 slot 替换 + 快照创建走 04-history-rollback + 删除二次文本确认
- **M2** — mod 追加菜单项 / 右段按钮 + 全局快捷键面板 + 完整登录流(对接 03-authentication)+ 数据目录切换 + 5.2 打开 dialog 内删除 + inline 重命名(中段 3 号)+ 协作者列表入口
- **M3** — 多版本对比 / 协作头像列 + 实时光标显示 + 业务自定义 breadcrumb / skill 图标点击回 welcome

## §3-§10

> Stage B 填:§3 各菜单 dialog UI 细节、§4 高度 / 响应式约束、§6 mod slot 注册接口、§9 audit gate(Header 不直接 mutate entity、不触发非允许 command)、§10 reference 表。
