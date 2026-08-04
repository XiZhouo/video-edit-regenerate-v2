# 02-atoms — UI 模块

> 一句话：`.ze-*` class 组件库。

按钮 / 输入 / 卡片 / 弹层 / callout / kv 列表 / skeleton / status tile / state-frame 等基础 UI 对象的 CSS 规则。

## 本叶子文件

- README.md — 本文件 (导览)
- SPEC.md — 完整规则 + 技术文档
- 对象文件夹(一原子/家族一文件夹,代码在 `code.css`)+ index.css 聚合(已实现;节点清单见 gallery L2 注册表)

## 怎么用

消费口径:下游 skill 的 `03-css-rules` 是同步来的只读皮肤,mod 作者不在这里写/改 CSS(授权模型见 [DESIGN-SYSTEM §0.2](../../DESIGN-SYSTEM.md))。

- **JSX 只贴 class + `data-*` 档**,不内联视觉值(DESIGN-SYSTEM §0.1)。典型落点:`02-mod/05-code/renderers/` 的自定义面板:

```tsx
<button className="ze-button" data-variant="primary" data-size="sm">保存</button>
<span className="ze-text" data-role="tertiary" data-size="xs">3 项已选</span>
<div className="ze-stack" data-gap="2" data-pad="2">…</div>
<article className="ze-thumb" data-ui-state={entity.ui_state} data-run-phase={entity.run_phase}>…</article>
```

- 选哪个组件/变体不凭手感,查 [SPEC.md §3 选择矩阵](./SPEC.md);下游 `css_contract_check` 校验 JSX 用的 `.ze-*` 都在皮肤词表内。
- skill 自身视觉定制只走本 skill 皮肤 `01-tokens/tokens.css` 末尾 `html[data-mod]` 覆盖块(见 [../01-tokens/README.md](../01-tokens/README.md));需要新原子/新变体 → 回 build-v3 在对应对象文件夹加(扩展规则 [SPEC.md §3.6](./SPEC.md),授权模型 DESIGN-SYSTEM §0.2)。
- 对象状态不另造“空态页面/执行中页面”;直接给对象自身贴 `data-ui-state` / `data-run-phase`,具体见 [../STATE-MODEL.md](../STATE-MODEL.md)。
