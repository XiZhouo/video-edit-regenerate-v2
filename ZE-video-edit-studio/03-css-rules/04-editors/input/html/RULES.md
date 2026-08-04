# html — 规则

- 预览唯一安全策略:iframe `sandbox="allow-scripts"`,**不给** `allow-same-origin` / `allow-popups`(产物 HTML 不可信)。沙盒在 React 画死,CSS/mod 不可放宽。
- 本文件零令牌:只 iframe 填满(flex/min-height/width)+ 去默认边框(border:0),全是几何。
- 源码态不另起样式,复用 `.ze-code__textarea`(等宽基座,见 input/code)。
- 行高/字号/色不在此声明 —— 走 L2 原子。
