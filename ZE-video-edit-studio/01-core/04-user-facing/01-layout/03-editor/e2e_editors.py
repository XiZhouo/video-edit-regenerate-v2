"""03-editor 真实浏览器冒烟:预设编辑器深度交互 + 闭环(P1 空间 / P2 表 / P5 表单)。

无前端单测基建(纯 React glue);正确性走 Playwright 真浏览器拖测。核心证:
  - canvas2d 空间编辑器:选中 + 拖动一个 box → 真位移 → entity.update 闭环 → SSE 回灌后稳定(不抖、不回弹)。
  - 框选新增:add 模式拖一个矩形 → box.add 闭环 → box 计数 +1(spatial 与 table 两面同步)。
  - table 深化:表头排序改变行序;form 深化:rotation 滑杆 + color 取色器存在。

运行(先起 BE+FE,见 e2e_preferences.py 同范式):
    ZE_DATA_DIR=/tmp/zev3editors ZE_DEV=1 PORT=8231 .venv/bin/uvicorn server:app --port 8231 &
    cd webapp && VITE_API_BASE=http://127.0.0.1:8231 npx vite --port 5231 --strictPort &
    PLAYWRIGHT_BROWSERS_PATH=$HOME/Library/Caches/ms-playwright \
        /tmp/pwenv/bin/python3 01-core/04-user-facing/01-layout/03-editor/e2e_editors.py

环境坑:SSE 长连接使 networkidle 永不触发 → 等 .ze-app / .ze-spatial selector;用空闲端口避开 5173。

P4(M3)新增 step 10:pane chrome 分屏 / 关闭(运行时布局操作)。tab 拖拽重排(moveTab)
在 demo tree 里无 tabs 容器,改由 layoutOps.test.ts 的 5 个 moveTab 单测覆盖(纯函数,e2e 无从触发)。
"""
from playwright.sync_api import sync_playwright
import os
import sys
import time

FE = os.environ.get("E2E_FE", "http://127.0.0.1:5231")
results = []


def ok(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("OK " if cond else "XX "), name, extra)


# .ze-spatial__box polygon 的质心(容器相对 px);返回按 cx 升序的列表
_CENTROIDS_JS = """() => {
  const boxes = [...document.querySelectorAll('.ze-spatial__box')];
  return boxes.map(b => {
    const pts = b.getAttribute('points').trim().split(/\\s+/).map(s => s.split(',').map(Number));
    const n = pts.length;
    const cx = pts.reduce((a,p)=>a+p[0],0)/n, cy = pts.reduce((a,p)=>a+p[1],0)/n;
    return {cx, cy};
  }).sort((a,b)=>a.cx-b.cx);
}"""


def centroids(pg):
    return pg.evaluate(_CENTROIDS_JS)


def run():
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1400, "height": 900})
        pg.goto(FE)
        pg.wait_for_selector(".ze-login, .ze-app", timeout=15000)

        # 1) 注册新用户(用户名带时间戳,避免复跑撞已存在用户)
        pg.click(".ze-login__switch")                    # → 注册模式
        pg.fill(".ze-login__card input[autocomplete='username']", f"e2e_{int(time.time())}")
        pg.fill(".ze-login__card input[type='password']", "pw123456")
        pg.click(".ze-login__card button[type='submit']")
        pg.wait_for_selector(".ze-project-picker", timeout=15000)

        # 2) 建带 seed 的 demo 项目(_seed 播种 3 个 box)
        pg.fill(".ze-project-picker__new input.ze-input", "编辑器冒烟")
        seed = pg.query_selector(".ze-project-picker__seed-label input[type='checkbox']")
        if seed and not seed.is_checked():
            seed.check()
        pg.click(".ze-project-picker__new button[type='submit']")
        pg.wait_for_selector(".ze-app", timeout=15000)
        pg.wait_for_selector(".ze-spatial__box", timeout=15000)
        pg.wait_for_timeout(500)

        cont = pg.query_selector(".ze-spatial").bounding_box()
        c0 = centroids(pg)
        ok("空间编辑器初始 3 个 box", len(c0) == 3, f"(got {len(c0)})")
        if len(c0) < 1:
            b.close(); return False

        # 3) 选中 + 拖动最左 box(Box A,rotation 0)。drag +90,+50(屏幕 px,scale≈1)
        a = c0[0]
        sx, sy = cont["x"] + a["cx"], cont["y"] + a["cy"]
        dx, dy = 90, 50
        pg.mouse.move(sx, sy)
        pg.mouse.down()
        pg.mouse.move(sx + dx / 2, sy + dy / 2, steps=4)
        pg.mouse.move(sx + dx, sy + dy, steps=4)
        pg.mouse.up()
        pg.wait_for_timeout(700)                          # entity.update → SSE 回灌

        c1 = centroids(pg)
        ok("拖动后 box 计数不变(没误增/误删)", len(c1) == 3, f"(got {len(c1)})")
        # 找到移动后最接近 (a + delta) 的 box
        target = (a["cx"] + dx, a["cy"] + dy)
        moved = min(c1, key=lambda c: (c["cx"] - target[0]) ** 2 + (c["cy"] - target[1]) ** 2) if c1 else None
        err = ((moved["cx"] - target[0]) ** 2 + (moved["cy"] - target[1]) ** 2) ** 0.5 if moved else 999
        ok("box 真位移到拖拽落点(±18px)", err < 18, f"(err={err:.1f}px, want≈{target})")

        # 4) SSE 回灌后稳定:再读一次质心应与上次一致(不抖、不回弹)
        pg.wait_for_timeout(400)
        c2 = centroids(pg)
        moved2 = min(c2, key=lambda c: (c["cx"] - target[0]) ** 2 + (c["cy"] - target[1]) ** 2) if c2 else None
        jitter = ((moved2["cx"] - moved["cx"]) ** 2 + (moved2["cy"] - moved["cy"]) ** 2) ** 0.5 if moved2 and moved else 999
        ok("回灌后位置稳定(jitter<2px,不回弹)", jitter < 2, f"(jitter={jitter:.2f}px)")

        # 5) 框选新增:开 add 模式 → 空白处拖一个矩形 → box.add 闭环 → 计数 +1
        add_btn = None
        for btn in pg.query_selector_all(".ze-spatial__hud button"):
            if "框选" in (btn.inner_text() or "") or "Add box" in (btn.inner_text() or ""):
                add_btn = btn; break
        if add_btn:
            add_btn.click()
            ex, ey = cont["x"] + 260, cont["y"] + 12        # 顶部空白带(boxes 从 y≈40 起)
            pg.mouse.move(ex, ey)
            pg.mouse.down()
            pg.mouse.move(ex + 70, ey + 26, steps=5)
            pg.mouse.up()
            pg.wait_for_timeout(800)
            c3 = centroids(pg)
            ok("框选新增 → box.add 闭环 → 计数 +1", len(c3) == 4, f"(got {len(c3)})")
        else:
            ok("找到框选新增按钮", False, "(HUD 无 addBox 按钮)")

        # 6) table 深化:box 表存在 + 表头可排序(点一下行序变化)
        tbl = pg.query_selector(".ze-editor--table")
        ok("box 表编辑器存在", tbl is not None)
        if tbl:
            ths = pg.query_selector_all(".ze-editor--table th[data-sortable='true']")
            ok("表头可排序(data-sortable)", len(ths) > 0, f"(cols={len(ths)})")
            if ths:
                def first_cell():
                    c = pg.query_selector(".ze-editor--table tbody tr td")
                    return c.inner_text() if c else ""
                before = first_cell()
                # 找 label 列点两下(desc)使行序确定变化
                ths[0].click(); pg.wait_for_timeout(150)
                ths[0].click(); pg.wait_for_timeout(150)
                after = first_cell()
                ok("点表头后排序生效(首行变化或排序标记出现)",
                   after != before or pg.query_selector(".ze-table th .ze-text[data-tone='accent']") is not None,
                   f"('{before}'→'{after}')")

        # 7) form 深化:选中 box 后 inspector 有 rotation 滑杆 + color 取色器
        # 经 table 行选中(比空间坐标稳;且 spatial 仍在 add 模式时点击不会选中)
        row = pg.query_selector(".ze-editor--table tbody tr")
        if row:
            row.click()
            pg.wait_for_timeout(300)
        slider = pg.query_selector(".ze-editor--form .ze-editor__field[data-control='slider'] input[type='range']")
        color = pg.query_selector(".ze-editor--form .ze-editor__field-color")
        ok("form 含 rotation 滑杆(schema min/max → slider)", slider is not None)
        ok("form 含 color 取色器(format:color)", color is not None)

        # 8) 属性面板行距一致:所有单行字段(label/color/rotation/x/y/w/h)行高相等(±1px)
        heights = pg.eval_on_selector_all(
            ".ze-editor--form .ze-editor__field",
            "els => els.map(e => Math.round(e.getBoundingClientRect().height))")
        ok("属性面板字段行高一致(max-min≤1px)",
           len(heights) >= 4 and (max(heights) - min(heights)) <= 1,
           f"(heights={heights})")

        # 9) 字段壳统一:每个字段都是 .ze-editor__field(含分组内),无打断栅格的异类行
        n_fields = len(heights)
        ok("字段数 = box schema 字段数(7:label/color/rotation/x/y/w/h)", n_fields == 7, f"(got {n_fields})")

        # 10) 槽位静态声明守卫:slot 渲染自 mod default 树;运行时分屏/关闭 chrome
        #     已按用户裁决移除(core 1.33.0,SPEC §2.3),DOM 不得再出现 .ze-slot__chrome。
        slots0 = len(pg.query_selector_all(".ze-slot"))
        ok("初始 slot 数 ≥ 4(demo split 树)", slots0 >= 4, f"(got {slots0})")
        chromes = pg.query_selector_all(".ze-slot__chrome")
        ok("slot 无运行时分屏/关闭 chrome(已移除)", len(chromes) == 0, f"(chrome={len(chromes)})")

        b.close()

    passed = sum(1 for _, c in results if c)
    print("\n=== %d/%d PASS ===" % (passed, len(results)))
    return passed == len(results)


sys.exit(0 if run() else 1)
