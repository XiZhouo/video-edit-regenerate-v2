"""i18n 收口冒烟:footer 全中文 + 接上工具名 + 数据标签(类型/字段显示名)走 schema.label。

证本轮三件事在真浏览器里成立(对应 05-i18n footer 迁移 + slotAdapters/编辑器 typeLabel + demo schema.label):
  - footer 展开态文案为中文(最近操作/流水线/同步/错误/追踪…),且无残留英文(Recent Ops/Pipelines/...)。
  - footer skill 段 = 工具名「ZE 示例工作台」,不再是 "Unnamed Skill"(App 给 FooterShell 传了 config)。
  - box 表头 = 中文字段显示名(标签/颜色/旋转/X/Y/宽/高);多实体编辑器标题 = 类型显示名「标注框」。

运行(先起 BE+FE,同 e2e_editors.py 范式;默认 locale=zh):
    ZE_DATA_DIR=/tmp/zev3i18n ... .venv/bin/uvicorn server:app --port 8232 &
    cd webapp && VITE_API_BASE=http://127.0.0.1:8232 npx vite --port 5232 --strictPort &
    PLAYWRIGHT_BROWSERS_PATH=$HOME/Library/Caches/ms-playwright E2E_FE=http://127.0.0.1:5232 \
        /tmp/pwenv/bin/python3 01-core/04-user-facing/01-layout/03-editor/e2e_i18n_labels.py
"""
from playwright.sync_api import sync_playwright
import os
import sys
import time

FE = os.environ.get("E2E_FE", "http://127.0.0.1:5232")
results = []

# 迁移前 footer 写死的英文 —— 任一出现即说明没迁干净
ENGLISH_LEAKS = ["Recent Ops", "No recent operations", "Pipelines",
                 "No active pipelines", "Unnamed Skill"]


def ok(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("OK " if cond else "XX "), name, extra)


def run():
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1400, "height": 900})
        # 显式锁 locale=zh(默认即 zh,这里固化避免环境漂移)
        pg.add_init_script("try{localStorage.setItem('ze.locale','zh')}catch(e){}")
        pg.goto(FE)
        pg.wait_for_selector(".ze-login, .ze-app", timeout=15000)

        pg.click(".ze-login__switch")
        pg.fill(".ze-login__card input[autocomplete='username']", f"i18n_{int(time.time())}")
        pg.fill(".ze-login__card input[type='password']", "pw123456")
        pg.click(".ze-login__card button[type='submit']")
        pg.wait_for_selector(".ze-project-picker", timeout=15000)

        pg.fill(".ze-project-picker__new input.ze-input", "i18n 冒烟")
        seed = pg.query_selector(".ze-project-picker__seed-label input[type='checkbox']")
        if seed and not seed.is_checked():
            seed.check()
        pg.click(".ze-project-picker__new button[type='submit']")
        pg.wait_for_selector(".ze-app", timeout=15000)
        pg.wait_for_selector(".ze-spatial__box", timeout=15000)
        pg.wait_for_timeout(400)

        # ---- footer:接上工具名(折叠态 skill 段)----
        skill_seg = pg.query_selector(".ze-footer__segment[data-segment='skill']")
        skill_txt = (skill_seg.inner_text() if skill_seg else "") or ""
        ok("footer skill 段 = 工具名「ZE 示例工作台」", "ZE 示例工作台" in skill_txt, f"(got {skill_txt!r})")

        # ---- footer:展开 → 全中文,无英文残留 ----
        handle = pg.query_selector(".ze-footer__handle")
        if handle:
            handle.click()
            pg.wait_for_selector(".ze-footer__expanded", timeout=5000)
            pg.wait_for_timeout(200)
        foot_txt = (pg.query_selector(".ze-footer").inner_text() or "")
        ok("footer 含『最近操作』(OpLogPanel 已迁)", "最近操作" in foot_txt)
        ok("footer 含『流水线』(PipelineList 已迁)", "流水线" in foot_txt)
        ok("footer 含同步/错误/追踪 tab(DetailsTabs 已迁)",
           all(s in foot_txt for s in ("同步", "错误", "追踪")))
        leaks = [s for s in ENGLISH_LEAKS if s in foot_txt]
        ok("footer 无英文残留", not leaks, f"(leaks={leaks})")

        # ---- 数据标签:box 表头 = 中文字段显示名 ----
        ths = [th.inner_text().strip() for th in pg.query_selector_all(".ze-editor--table thead th")]
        want_cols = ["标签", "颜色", "旋转", "X", "Y", "宽", "高"]
        got_cols = [c for c in want_cols if c in ths]
        ok("box 表头 = 中文字段显示名(标签/颜色/旋转/X/Y/宽/高)",
           len(got_cols) == len(want_cols), f"(ths={ths})")
        ok("box 表头无裸 field key(label/color/rotation/w/h)",
           not any(k in ths for k in ("label", "color", "rotation", "w", "h")), f"(ths={ths})")

        # ---- 数据标签:多实体编辑器标题 = 类型显示名「标注框」 ----
        titles = [el.inner_text().strip() for el in pg.query_selector_all(".ze-editor__title")]
        ok("多实体编辑器标题含类型显示名『标注框』(非裸 'box')",
           any("标注框" in t for t in titles) and "box" not in titles, f"(titles={titles})")

        b.close()

    passed = sum(1 for _, c in results if c)
    print("\n=== %d/%d PASS ===" % (passed, len(results)))
    return passed == len(results)


sys.exit(0 if run() else 1)
