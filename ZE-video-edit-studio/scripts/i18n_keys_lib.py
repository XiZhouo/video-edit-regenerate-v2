#!/usr/bin/env python3
"""i18n_keys_lib — i18n_sync / i18n_audit 共用底座。

三件事:
  1. dump():跑 i18n_dump_keys.mjs 拿真词表的 key 集(ground truth,不手解析 TS)。
  2. scan_used_keys():扫 UI 代码里**静态**调用的 key(t('a.b') / getT()('a.b') /
     <Trans k="a.b"> / labelKey: 'a.b');动态 key(模板串 / 变量索引)天然跳过。
  3. insert_key():把一个 dot-path 叶子按嵌套结构插进 TS 词表文本(brace 匹配,immutable
     返回新文本)—— 给 i18n_sync --fill 用。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import i18n_contract_check as cc  # noqa: E402  复用注释剥离 + SCAN_ROOTS

BUILD_ROOT = Path(__file__).resolve().parents[1]

# ── purity 门禁(i18n_audit 检查 4)的专名白名单 ──────────────────────────────
# 语言纯度规则(用户拍板):zh 值除白名单词外全中文;en 值零 CJK。
# 白名单=技术缩写/文件格式 + 品牌/引擎/模型名(casefold 比对)。**标题常用词刻意不豁免**:
# Manifest→清单、Mask→蒙版、Prompt→提示词、Bot→机器人、Adapter→适配器、Workbench→工作台。
# 新增豁免词必须在 commit message 说明理由。
PURITY_ALLOW = frozenset({
    # 技术缩写/格式/单位
    "ai", "2d", "3d", "gif", "psd", "png", "jpg", "jpeg", "svg", "glb", "ply", "zip",
    "json", "url", "uri", "id", "api", "mcp", "cli", "sse", "gpu", "cpu", "rgb", "rgba",
    "alpha", "ok", "ms", "px", "fps", "md", "html", "css", "js", "ts", "pdf", "mp4",
    "mp3", "wav", "webp", "ndjson", "ze-pack", "utf-8", "http", "https", "ws", "ssh",
    # 品牌/引擎/模型
    "codex", "claude", "cursor", "gpt", "sam", "luna", "playcanvas", "three", "unity", "applovin",
    "moloco", "mintegral", "tiktok", "meta", "google", "comfyui", "feishu", "nas",
    "vtracer", "esrgan", "gemini", "seedream", "flux", "hunyuan", "qwen", "openai",
    # 键盘键名(快捷键提示文案)
    "ctrl", "shift", "alt", "cmd", "enter", "esc", "escape", "tab", "space", "del",
    # 平台字段/主机/内部品牌(飞书官方中文口径用 App ID/App Secret;amber=部署主机名;hupo=公司名)
    "app", "pid", "dry-run", "amber", "hupo", "lark",
    # 广告/Web 技术缩写与规格词(playable 等强技术工具的界面沿用平台原文,译成中文反而不可读)
    "cta", "sdk", "mraid", "coppa", "orientation", "portrait", "landscape",
    "bytes", "byte", "mb", "kb", "crossorigin", "modulepreload", "preset", "mock",
    "token", "sdk", "spec", "vanilla", "playworks", "blender", "ads",
    # GitHub/Git 仓库字段名(仓库关联面板原文展示 owner/repo/ref/PAT,译成中文反不可读)
    "owner", "repo", "ref", "pat",
})

_PURITY_PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")          # {count} / {{count}} 插值
_PURITY_BACKTICK_RE = re.compile(r"`[^`]*`")                 # 反引号内=代码原样
_PURITY_ENV_IDENT_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")  # ENV_STYLE 常量名
_PURITY_TOKEN_RE = re.compile(r"\d*[A-Za-z][A-Za-z0-9]*(?:[+&.\-/_][A-Za-z0-9]+)*")
_PURITY_CAMEL_RE = re.compile(r"^[a-z][a-z0-9]*[A-Z]")       # camelCase 代码标识符(getDisplayMedia)
_PURITY_PASCAL_RE = re.compile(r"^[A-Z][a-z0-9]+[A-Z]")      # PascalCase 标识符(NotifyLog/OpLog)
# 系列名(生效文案不出现,用户裁定)。不用 \b:Python \b 把 CJK 当 word 字符,「导出ZE信封」会漏。
_SERIES_PREFIX_RE = re.compile(r"(?<![A-Za-z0-9_])ZE(?![A-Za-z0-9_])")
_CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")


def has_cjk(value: str) -> bool:
    return bool(_CJK_RE.search(value))


def has_series_prefix(value: str) -> bool:
    """值里含独立系列名 ZE(任何语言的生效文案都不该出现;`.ze-*` 技术命名空间与 ZE_ENV 常量不算)。"""
    s = _PURITY_BACKTICK_RE.sub(" ", value)
    s = _PURITY_ENV_IDENT_RE.sub(" ", s)
    s = re.sub(r"[.\-]ze[.\-]?[a-z0-9-]*", " ", s, flags=re.I)
    return bool(_SERIES_PREFIX_RE.search(s))


def zh_latin_violations(value: str) -> list[str]:
    """zh 词表值里、剥离插值/反引号/ENV 常量后,白名单外的拉丁词 token 列表(空=合规)。"""
    s = _PURITY_PLACEHOLDER_RE.sub(" ", value)
    s = _PURITY_BACKTICK_RE.sub(" ", s)
    s = _PURITY_ENV_IDENT_RE.sub(" ", s)
    bad: list[str] = []
    for tok in _PURITY_TOKEN_RE.findall(s):
        if len(tok) < 2:
            continue
        if tok.casefold() in PURITY_ALLOW:
            continue  # 整词白名单(含连字符词 ze-pack / utf-8)
        if _PURITY_CAMEL_RE.match(tok) or _PURITY_PASCAL_RE.match(tok):
            continue  # camelCase / PascalCase 代码标识符
        if "." in tok or "_" in tok or "/" in tok:
            continue  # 带点/下划线/斜杠 = 代码路径或标识符(open_id / 02-mod/04-layout.md / a.b.c)
        segs = re.split(r"[+&.\-/_]", tok)
        if all(len(seg) <= 1 or seg.isdigit() or seg.casefold() in PURITY_ALLOW for seg in segs):
            continue  # 各段皆白名单/数字/单字母(Ctrl+Z / SAM-3)
        bad.append(tok)
    return bad

# 静态 key 调用点:t('a.b') / getT()('a.b') / <Trans ... k="a.b"> / labelKey: 'a.b'
# 只认单/双引号字面量;模板串(`...${x}`)与变量索引不匹配 → 动态 key 自动跳过。
_USED_PATTERNS = [
    re.compile(r"""(?<![.\w])t\(\s*['"]([\w.]+)['"]"""),   # t('a.b') —— 排除 foo.t(/x.t(
    re.compile(r"""\bgetT\(\)\(\s*['"]([\w.]+)['"]"""),
    re.compile(r"""<Trans\b[^>]*?\bk=['"]([\w.]+)['"]"""),
    re.compile(r"""\blabelKey:\s*['"]([\w.]+)['"]"""),     # labelKey: 'a.b'(layout.config 定义处)
    re.compile(r"""\bconfirmKey:\s*['"]([\w.]+)['"]"""),   # confirmKey: 'a.b'(同上;t(a.confirmKey) 动态扫不到)
]


def dump(build_root: Path | None = None) -> dict:
    """跑 node dump_keys,返回 {locale_codes, default_locale, meta_codes, framework, mod}。"""
    root = build_root or BUILD_ROOT
    script = root / "scripts" / "i18n_dump_keys.mjs"
    proc = subprocess.run(
        ["node", str(script), str(root)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"i18n_dump_keys.mjs failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


def defined_keys(dumped: dict, locale: str) -> set[str]:
    """某 locale 的全部已定义 key = 框架 key ∪ {'mod.'+k}。"""
    fw = set(dumped.get("framework", {}).get(locale, []))
    mod = {f"mod.{k}" for k in dumped.get("mod", {}).get(locale, [])}
    return fw | mod


def scan_used_keys(build_root: Path | None = None) -> dict[str, list[str]]:
    """扫 SCAN_ROOTS 下 UI 代码 + 04-layout.config 的静态 key → {key: [file:line, ...]}。
    C2:layout.config 的 labelKey/confirmKey 是动态 key(t(a.labelKey))的**定义处**——
    渲染处是变量索引扫不到,必须在定义处抓,否则 audit 的 missing/parity 对动作 key 形同虚设。"""
    root = build_root or BUILD_ROOT
    used: dict[str, list[str]] = {}

    def scan_one(p: Path) -> None:
        text = cc._strip_block_comments(p.read_text(encoding="utf-8"))
        for i, line in enumerate(text.splitlines(), 1):
            code = cc._strip_line_comment(line)
            for pat in _USED_PATTERNS:
                for m in pat.finditer(code):
                    used.setdefault(m.group(1), []).append(f"{p.relative_to(root)}:{i}")

    for rel in cc.SCAN_ROOTS:
        base = root / rel
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if p.suffix not in (".ts", ".tsx"):
                continue
            if "__pycache__" in p.parts or "node_modules" in p.parts:
                continue
            scan_one(p)
    lc = root / cc.LAYOUT_CONFIG_REL
    if lc.exists():
        scan_one(lc)
    return used


# ── --fill 用:把一个 dot-path 叶子插进 TS 词表文本 ───────────────────────────

def _find_block_close(text: str, open_idx: int) -> int:
    """从 open_idx(指向 '{')起,brace 匹配返回配对 '}' 的下标。

    **字符串感知**:跳过 '...' / "..." / `...` 里的内容 —— 词表 value 常含 `{owner}`/`{count}`
    这类插值占位,不能当成块的 brace 计数(否则错配到字符串里的 `}`)。
    """
    depth = 0
    i = open_idx
    n = len(text)
    quote = ""
    while i < n:
        c = text[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = ""
        elif c in "'\"`":
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("unbalanced braces")


def _indent_at(text: str, idx: int) -> str:
    """idx 所在行的前导缩进。"""
    line_start = text.rfind("\n", 0, idx) + 1
    j = line_start
    while j < len(text) and text[j] in " \t":
        j += 1
    return text[line_start:j]


def insert_key(text: str, dot_path: str, value: str, root_open_re: str) -> str:
    """
    把 dot_path(如 'editor.foo' / 'a.b.c')的叶子插进 TS 词表文本(immutable 返回新文本)。

    - 沿已存在的命名空间块逐层下钻;缺失的中间层就地建块。
    - 多行块与**内联块**(`{ a: 'x' }`)都支持。
    - value 是字符串字面量内容(不含引号);自动加单引号。
    - root_open_re:匹配对象根声明的正则(如 r"export const zh\\b"),从它起找第一个 '{'。
    - 幂等:叶子已存在则原样返回。
    """
    parts = dot_path.split(".")
    m = re.search(root_open_re, text)
    if not m:
        raise ValueError(f"root open not found via {root_open_re!r}")
    open_idx = text.index("{", m.start())
    return _insert_into_block(text, open_idx, parts, value)


def _is_inline(text: str, open_idx: int, close_idx: int) -> bool:
    return "\n" not in text[open_idx:close_idx]


def _insert_into_block(text: str, open_idx: int, parts: list[str], value: str) -> str:
    close_idx = _find_block_close(text, open_idx)
    block = text[open_idx + 1:close_idx]
    head = parts[0]
    key_re = re.compile(r"(?m)^[ \t]*" + re.escape(head) + r"\s*:")  # 多行块:行首 head:
    inline_re = re.compile(r"(?:^|[{,]\s*)" + re.escape(head) + r"\s*:")  # 内联块:{ / , 后 head:
    inline = _is_inline(text, open_idx, close_idx)
    km = (inline_re if inline else key_re).search(block)

    if len(parts) == 1:
        if km:
            return text  # 幂等
        return _add_entry(text, open_idx, close_idx, inline, head, f"'{value}'")

    if km:
        # head 已存在且 path 还要下钻 → head 的 value 必须是对象块(不是字符串叶子)。
        # 从冒号后第一个非空白字符判断;若是引号 → 把叶子当 namespace 用,拒绝(否则会插进字符串里)。
        after = open_idx + 1 + km.end()
        j = after
        while j < close_idx and text[j] in " \t\n":
            j += 1
        if j >= close_idx or text[j] != "{":
            raise ValueError(f"'{head}' 是字符串叶子,不能当 namespace 插 {'.'.join(parts)}")
        return _insert_into_block(text, j, parts[1:], value)

    # head 不存在 → 合成嵌套对象字面量作为 value
    if inline:
        return _add_entry(text, open_idx, close_idx, True, head, _synth_inline(parts[1:], value))
    base_indent = _indent_at(text, open_idx)
    child_indent = base_indent + "  "
    return _add_entry(text, open_idx, close_idx, False, head,
                      _synth_block(parts[1:], value, child_indent))


def _synth_inline(parts: list[str], value: str) -> str:
    """合成内联嵌套字面量:['a','b'] → `{ a: { b: 'v' } }`。"""
    if len(parts) == 1:
        return f"{{ {parts[0]}: '{value}' }}"
    return f"{{ {parts[0]}: {_synth_inline(parts[1:], value)} }}"


def _synth_block(parts: list[str], value: str, indent: str) -> str:
    """合成多行嵌套字面量(value_text,供 _add_entry 多行分支)。"""
    inner = indent + "  "
    if len(parts) == 1:
        return f"{{\n{inner}{parts[0]}: '{value}',\n{indent}}}"
    return f"{{\n{inner}{parts[0]}: {_synth_block(parts[1:], value, inner)},\n{indent}}}"


def _add_entry(text: str, open_idx: int, close_idx: int, inline: bool,
               leaf: str, value_text: str) -> str:
    """往块里加 `leaf: value_text`(内联或多行,保证逗号合法)。"""
    if inline:
        inner = text[open_idx + 1:close_idx]
        if inner.strip() == "":
            new_inner = f" {leaf}: {value_text} "
        else:
            trimmed = inner.rstrip()
            sep = "" if trimmed.endswith(",") else ","
            new_inner = f"{trimmed}{sep} {leaf}: {value_text} "
        return text[:open_idx + 1] + new_inner + text[close_idx:]

    base_indent = _indent_at(text, open_idx)
    child_indent = base_indent + "  "
    insertion = f"{child_indent}{leaf}: {value_text},\n"
    close_line_start = text.rfind("\n", 0, close_idx) + 1
    # 保证前一个条目以 ',' 或 '{' 结尾(否则补逗号)
    j = close_line_start - 1
    while j > open_idx and text[j] in " \t\n":
        j -= 1
    prefix = text[:close_line_start]
    if j > open_idx and text[j] not in ",{":
        prefix = text[:j + 1] + "," + text[j + 1:close_line_start]
    return prefix + insertion + text[close_line_start:]
