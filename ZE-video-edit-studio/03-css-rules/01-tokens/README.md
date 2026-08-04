# 01-tokens — 视觉参考

> 一句话：Design tokens 取值表。

颜色 (surface 4 层 / ink / accent 8 阶 / semantic 4 / focus / overlay) / 字号 / 行高 / 字重 / 间距 (base 2px) / 圆角 / 阴影 / 动效时长。每个 token 含取值理由。

## 本叶子文件

- README.md — 本文件 (导览)
- SPEC.md — 完整规则 + 技术文档
- `design/tokens/code.css` — 设计令牌字典(真实代码)
- `theme/tool-themes/code.css` — 36 档工具主题色
- `foundation/base/code.css` — 文档基线
- `media/icon-tokens/code.css` — 图标令牌生成物

## 怎么用

消费口径:下游 skill 的 `03-css-rules` 是 `skill_sync --layers css` 同步来的只读皮肤,mod 作者不在这里写/改 CSS(授权模型见 [DESIGN-SYSTEM §0.2](../../DESIGN-SYSTEM.md))。

- **不直接引令牌**:mod 的 JSX 不写视觉值,令牌经 02-atoms 的 `.ze-*` 原子间接消费(贴法见 [../02-atoms/README.md](../02-atoms/README.md));运行时动态值按 DESIGN-SYSTEM §0.1 经 CSS var 传递。
- **状态也先在这里命名**:空、执行中、正常、异常、过期、禁用都先落成 `--ze-state-*` token,再由 L2 UI 对象消费。业务只发 entity 状态事实,详见 [../STATE-MODEL.md](../STATE-MODEL.md)。
- **换主题色 = 声明一个主题档**(v4):`themes.css` 提供 36 档手工设计的工具主题色(`[data-ze-theme="NN"]`,每档 light/dark × 4 设计值);skill 在宿主 `<html>` 上声明一行,wash/tint/focus-ring/glow 由 accent-500 相对色自动跟随,明暗主题自动贯穿:

```html
<!-- 本 skill 的宿主 html — 每个 skill 占一个编号(02/08/16/23 近语义色,重状态工具慎用) -->
<html data-ze-theme="22">
```

  ⚠️ 旧式「`html[data-mod]` 覆盖 `--ze-accent-h` 换色」自 css 16.3.0 起静默失效(accent 不再由色相推导,`--ze-accent-h/-c` 仅静态兼容别名)。非 accent 令牌的覆盖仍可走 `tokens.css` 末尾 `html[data-mod]` 块;定制过皮肤的 skill **不要再 `--layers css` 同步**(整层复制会覆盖此块;见 DESIGN-SYSTEM §0.2)。

- 需要新 token 档位 → 回 build-v3 在对应 token 对象文件夹按层加(DESIGN-SYSTEM §0.2),不在下游手改。

token 全表与取值理由见 [SPEC.md](./SPEC.md)(主题色机制 §2.7,情境选 token §3)。
