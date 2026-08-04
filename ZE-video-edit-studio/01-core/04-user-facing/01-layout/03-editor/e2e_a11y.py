"""03-editor a11y 冒烟:扫交互式 gallery(全部 L4 编辑器节点 live)的常见 WCAG 缺陷,回归门禁。

无依赖(不引 axe):纯 DOM 扫描,查 4 类高频可访问性缺陷 ——
  NO_ACCESSIBLE_NAME — button / [role=button] 无可访问名(无文本/aria-label/title/内嵌 alt)。
  IMG_NO_ALT         — <img> 缺 alt 属性。
  INPUT_NO_LABEL     — input/select/textarea 无可访问名(placeholder 不算名,WCAG)。
  POSITIVE_TABINDEX  — tabindex>0 反模式(打乱焦点序)。

只扫 gallery(静态前端,全部编辑器同步渲染,无需 BE)。运行:
    cd webapp && npx vite --port 5173 --strictPort &
    PLAYWRIGHT_BROWSERS_PATH=$HOME/Library/Caches/ms-playwright \
        /usr/bin/python3 01-core/04-user-facing/01-layout/03-editor/e2e_a11y.py
有缺陷 → 非零退出(CI 门禁)。环境坑:用空闲端口避开占用;等 .ze-editor 出现再扫。
"""
from playwright.sync_api import sync_playwright
import os
import sys

FE = os.environ.get("E2E_A11Y_FE", "http://localhost:5173/gallery.html")

_AUDIT_JS = r"""
() => {
  const out = [];
  const name = (el) => (
    (el.getAttribute('aria-label') || '').trim() ||
    (el.getAttribute('title') || '').trim() ||
    (el.textContent || '').trim() ||
    (el.querySelector('img[alt]')?.getAttribute('alt') || '').trim()
  );
  const sel = (el) => {
    const cls = (el.getAttribute('class')||'').split(/\s+/).filter(Boolean).slice(0,2).join('.');
    return el.tagName.toLowerCase() + (cls ? '.'+cls : '');
  };
  document.querySelectorAll('button, [role="button"]').forEach(el => {
    if (!name(el)) out.push({kind:'NO_ACCESSIBLE_NAME', el: sel(el), html: el.outerHTML.slice(0,100)});
  });
  document.querySelectorAll('img').forEach(el => {
    if (!el.hasAttribute('alt')) out.push({kind:'IMG_NO_ALT', el: sel(el), html: el.outerHTML.slice(0,100)});
  });
  document.querySelectorAll('input, select, textarea').forEach(el => {
    const id = el.id;
    const labelled = (el.getAttribute('aria-label')||'').trim() || (el.getAttribute('title')||'').trim()
      || (id && document.querySelector('label[for="'+id+'"]')) || el.closest('label');
    if (!labelled) out.push({kind:'INPUT_NO_LABEL', el: sel(el), html: el.outerHTML.slice(0,100)});
  });
  document.querySelectorAll('[tabindex]').forEach(el => {
    if (parseInt(el.getAttribute('tabindex'), 10) > 0)
      out.push({kind:'POSITIVE_TABINDEX', el: sel(el), html: el.outerHTML.slice(0,80)});
  });
  return out;
}
"""


def main() -> int:
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 1000})
        pg.goto(FE, wait_until="networkidle")
        pg.wait_for_selector(".ze-editor", timeout=8000)
        pg.wait_for_timeout(400)  # 等 audio metadata / canvas 挂载
        violations = pg.evaluate(_AUDIT_JS)
        b.close()

    by: dict[str, list] = {}
    for v in violations:
        by.setdefault(v["kind"], []).append(v)
    if not violations:
        print("OK  a11y: 0 缺陷(全部 live 编辑器;按钮/图片/控件可访问名齐备,无正向 tabindex)")
        return 0
    print(f"XX  a11y: {len(violations)} 处缺陷")
    for kind, items in sorted(by.items()):
        print(f"\n## {kind} [{len(items)}]")
        for it in items:
            print(f"  {it['el']:34} {it['html']}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
