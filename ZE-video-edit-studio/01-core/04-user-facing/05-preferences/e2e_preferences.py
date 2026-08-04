"""05-preferences 真实浏览器冒烟:偏好(主题 + 语言)读 localStorage → 应用到 <html> → token 真切换 → 刷新持久化。

无前端单测基建(本叶子是纯前端 React glue,与 03-editor / 05-i18n 同),正确性走 Playwright 浏览器冒烟。
偏好在登录前就生效(PreferencesProvider 在 AuthProvider 之外),故本冒烟不需要登录。

运行(需先起 dev server):
    # 后端
    ZE_DATA_DIR=/tmp/zev3pref ZE_DEV=1 PORT=8211 .venv/bin/uvicorn server:app --port 8211 &
    # 前端
    cd webapp && VITE_API_BASE=http://127.0.0.1:8211 npx vite --port 5211 --strictPort &
    # 冒烟(playwright 装在隔离 venv;浏览器缓存见 PLAYWRIGHT_BROWSERS_PATH)
    PLAYWRIGHT_BROWSERS_PATH=$HOME/Library/Caches/ms-playwright \
        /tmp/pwenv/bin/python3 01-core/04-user-facing/05-preferences/e2e_preferences.py

环境坑(同仓库其它前端验证):SSE 长连接使 networkidle 永不触发 → 等 .ze-login/.ze-app selector;
用空闲端口(5211/8211)避开常占的 5173。
"""
from playwright.sync_api import sync_playwright
import os

FE = os.environ.get("E2E_FE", "http://127.0.0.1:5211")
results = []


def ok(name, cond):
    results.append((name, bool(cond)))
    print(("OK " if cond else "XX "), name)


def set_prefs(pg, theme=None, locale=None):
    pg.evaluate(
        """({theme, locale}) => {
            if (theme) localStorage.setItem('ze.theme', theme);
            if (locale) localStorage.setItem('ze.locale', locale);
        }""",
        {"theme": theme, "locale": locale},
    )
    pg.reload()
    pg.wait_for_selector(".ze-login, .ze-app", timeout=15000)
    pg.wait_for_timeout(300)


def html_attr(pg, name):
    return pg.evaluate("(n) => document.documentElement.getAttribute(n)", name)


def bg(pg):
    return pg.evaluate("() => getComputedStyle(document.body).backgroundColor")


def login_title(pg):
    el = pg.query_selector(".ze-login__title") or pg.query_selector(".ze-login h1, .ze-login h2")
    return el.inner_text() if el else ""


def run():
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.goto(FE)
        pg.wait_for_selector(".ze-login, .ze-app", timeout=15000)

        # 1) light + zh
        set_prefs(pg, theme="light", locale="zh")
        ok("light 应用到 <html data-theme>", html_attr(pg, "data-theme") == "light")
        ok("zh 应用到 <html lang>", html_attr(pg, "lang") == "zh-CN")
        light_bg = bg(pg)
        zh_title = login_title(pg)

        # 2) dark:token 真切换
        set_prefs(pg, theme="dark")
        ok("dark 应用到 <html data-theme>", html_attr(pg, "data-theme") == "dark")
        dark_bg = bg(pg)
        ok("dark vs light 背景 token 实切换", dark_bg and light_bg and dark_bg != light_bg)

        # 3) 持久化:再 reload 仍 dark(不重设 localStorage)
        pg.reload()
        pg.wait_for_selector(".ze-login, .ze-app", timeout=15000)
        pg.wait_for_timeout(300)
        ok("刷新后 dark 持久化", html_attr(pg, "data-theme") == "dark")

        # 4) en:locale 切换 + 文案变 + lang 持久化
        set_prefs(pg, locale="en")
        ok("en 应用到 <html lang>", html_attr(pg, "lang") == "en")
        en_title = login_title(pg)
        ok("登录页文案随 locale 变(zh != en)", zh_title and en_title and zh_title != en_title)

        b.close()

    passed = sum(1 for _, c in results if c)
    print("\n=== %d/%d PASS ===" % (passed, len(results)))
    return passed == len(results)


import sys
sys.exit(0 if run() else 1)
