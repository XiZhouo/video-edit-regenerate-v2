/**
 * markdown — 受控、零依赖的 markdown → 安全 HTML 渲染(纯函数,vitest 可测)。
 *
 * 用途:① text 编辑器预览态把整段 markdown 字段渲成阅读视图;② document 块编辑器渲散文块的
 * 行内标记 / 未知块的 _raw 兜底。core 不打包重 markdown 库 —— 完整 CommonMark / 扩展(表格 /
 * 脚注 / 数学 / mermaid)走 mod 经 lazyModule 注入(SPEC 块编辑器 view_state.richEditorModuleId)。
 *
 * 安全(security.md):所有文本先转义 &<>"(内容来自 entity.data,可能含 <script>);链接 href 仅放行
 * http(s)/mailto/相对(/、#、./),其余(javascript:/data: 等)丢弃 href 退化为纯文本。
 * 输出语义裸标签(h1-h6/p/ul/ol/li/blockquote/pre>code/hr/strong/em/code/a),由 L2 原子
 * .ze-text[data-prose] 经后代选择器统一排版(块视觉只住 L2,本模块只产结构,不写样式)。
 *
 * 覆盖子集(够百科正文用,非完整 CommonMark):ATX 标题、围栏代码、引用、有序/无序列表、
 * 任务清单、分隔线、段落 + 行内 **粗** *斜* `码` [文](url) 与软换行。
 */

const ESC: Record<string, string> = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }

/** 转义 HTML 元字符(&<>");单引号不在属性场景里用,无需转。 */
export function escapeHtml(s: string): string {
  return s.replace(/[&<>"]/g, (c) => ESC[c])
}

/** href 安全过滤:放行 http(s)/mailto/相对路径;危险协议(javascript:/data: 等)返回 null。 */
export function safeHref(raw: string): string | null {
  const url = raw.trim()
  if (/^(https?:|mailto:)/i.test(url)) return url
  if (/^[/#]/.test(url) || /^\.\.?\//.test(url)) return url
  // 无协议前缀的裸相对路径(如 assets/x.png)放行;带任意协议(scheme:)的一律丢弃。
  if (!/^[a-z][a-z0-9+.-]*:/i.test(url)) return url
  return null
}

/**
 * 行内标记渲染:输入是「需转义的原始文本」,输出安全 HTML 片段。
 * 顺序:抽出行内代码占位(其内不再二次标记)→ 转义 → 粗/斜 → 链接 → 还原代码。
 */
export function renderInlineMarks(text: string): string {
  const codes: string[] = []
  // 行内代码先抽成哨兵占位(@@ZECODE<n>@@):不含 &<>"、* _ [ ] ( ),不被后续转义/标记/链接误伤。
  let s = (text ?? '').replace(/`([^`]+)`/g, (_, c: string) => {
    codes.push(`<code>${escapeHtml(c)}</code>`)
    return `@@ZECODE${codes.length - 1}@@`
  })
  s = escapeHtml(s)
  // 粗体先于斜体(** 不被 * 吞);斜体 _ 用边界规避 snake_case 词内下划线。
  s = s
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/__([^_]+)__/g, '<strong>$1</strong>')
    .replace(/\*([^*\n]+)\*/g, '<em>$1</em>')
    .replace(/(?<![A-Za-z0-9])_([^_\n]+)_(?![A-Za-z0-9])/g, '<em>$1</em>')
  // 链接:url 允许一层嵌套括号(Wikipedia 的 Cat_(animal) 之类)。
  s = s.replace(/\[([^\]]+)\]\(((?:[^()]|\([^()]*\))*)\)/g, (_, label: string, url: string) => {
    const href = safeHref(url)
    return href ? `<a href="${escapeHtml(href)}">${label}</a>` : label
  })
  // 还原代码占位;越界(输入恰含字面 @@ZECODE<n>@@、无对应抽取)→ 保留原文,不出 undefined。
  s = s.replace(/@@ZECODE(\d+)@@/g, (m: string, i: string) => codes[Number(i)] ?? m)
  return s
}

/** 块起始判定:用于段落聚合的终止(遇到任一块语法即收束当前段)。 */
function isBlockStart(line: string): boolean {
  return /^```/.test(line)
    || /^ {0,3}#{1,6}\s/.test(line)
    || /^ {0,3}>/.test(line)
    || /^ {0,3}([-*+]|\d+\.)\s+/.test(line)
    || /^ {0,3}([-*_])( *\1){2,} *$/.test(line)
}

const UL_RE = /^ {0,3}([-*+])\s+(.*)$/
const OL_RE = /^ {0,3}(\d+)\.\s+(.*)$/

/**
 * 整段 markdown → 安全 HTML 块流(text 预览 / document 块 _raw 兜底)。
 * 行级解析:围栏代码 → 分隔线 → 标题 → 引用(递归)→ 列表/任务清单 → 段落(软换行 <br/>)。
 */
export function renderMarkdown(md: string, depth = 0): string {
  // 引用递归深度上限:防 `> > > …` 病态深嵌套爆栈(超限则把整段当散文行内渲染)。
  if (depth > 16) return `<p>${renderInlineMarks(md ?? '')}</p>`
  const lines = (md ?? '').replace(/\r\n?/g, '\n').split('\n')
  const out: string[] = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    if (/^\s*$/.test(line)) { i++; continue }

    // 围栏代码(内部原样,不再行内标记;语言标签放宽到非空白非反引号,如 c++ / f# / text)
    const fence = line.match(/^```\s*([^\s`]*)\s*$/)
    if (fence) {
      const buf: string[] = []
      i++
      while (i < lines.length && !/^```\s*$/.test(lines[i])) { buf.push(lines[i]); i++ }
      i++ // 跳过闭合 ```
      const lang = fence[1] ? ` data-language="${escapeHtml(fence[1])}"` : ''
      out.push(`<pre${lang}><code>${escapeHtml(buf.join('\n'))}</code></pre>`)
      continue
    }

    // 分隔线
    if (/^ {0,3}([-*_])( *\1){2,} *$/.test(line)) { out.push('<hr/>'); i++; continue }

    // ATX 标题
    const h = line.match(/^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$/)
    if (h) {
      const lvl = h[1].length
      out.push(`<h${lvl}>${renderInlineMarks(h[2])}</h${lvl}>`)
      i++; continue
    }

    // 引用(剥 > 前缀后递归渲染内层)
    if (/^ {0,3}>/.test(line)) {
      const buf: string[] = []
      while (i < lines.length && /^ {0,3}>/.test(lines[i])) {
        buf.push(lines[i].replace(/^ {0,3}> ?/, '')); i++
      }
      out.push(`<blockquote>${renderMarkdown(buf.join('\n'), depth + 1)}</blockquote>`)
      continue
    }

    // 列表(无序/任务清单/有序;同型连续聚合)
    if (UL_RE.test(line) || OL_RE.test(line)) {
      const ordered = OL_RE.test(line)
      const re = ordered ? OL_RE : UL_RE
      const items: string[] = []
      while (i < lines.length && re.test(lines[i])) {
        const content = lines[i].match(re)![2]
        const task = !ordered && content.match(/^\[([ xX])\]\s+(.*)$/)
        if (task) {
          const checked = task[1].toLowerCase() === 'x' ? ' checked' : ''
          items.push(`<li data-task="true"><input type="checkbox" disabled${checked}/>${renderInlineMarks(task[2])}</li>`)
        } else {
          items.push(`<li>${renderInlineMarks(content)}</li>`)
        }
        i++
      }
      out.push(ordered ? `<ol>${items.join('')}</ol>` : `<ul>${items.join('')}</ul>`)
      continue
    }

    // 段落(聚合到空行 / 下一个块起始;行内软换行 <br/>)
    const para: string[] = []
    while (i < lines.length && !/^\s*$/.test(lines[i]) && !isBlockStart(lines[i])) {
      para.push(lines[i]); i++
    }
    out.push(`<p>${para.map((l) => renderInlineMarks(l)).join('<br/>')}</p>`)
  }
  return out.join('\n')
}
