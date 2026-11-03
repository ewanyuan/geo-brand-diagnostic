"""Check the bundled browser driver locally without logging in or networking."""

from app_paths import VERSION
from browser_discovery import browser_candidates, external_environment


def check():
    from playwright.sync_api import sync_playwright
    result = {"version": VERSION, "requires_ai_tool": False, "requires_python_install": False,
              "browser_input_verified": False, "browser": "", "errors": []}
    with sync_playwright() as engine:
        for channel, options in browser_candidates():
            try:
                browser = engine.chromium.launch(**options, headless=True, env=external_environment())
                try:
                    page = browser.new_page()
                    page.set_content('<label>GEO<input id="geo-check"></label>')
                    page.locator("#geo-check").fill("GEO 本机输入检查")
                    result.update(browser_input_verified=page.locator("#geo-check").input_value() == "GEO 本机输入检查",
                                  browser=channel)
                    break
                finally:
                    browser.close()
            except Exception as exc:
                result["errors"].append(f"{channel} 未能启动，请检查是否安装或被组织策略限制")
                # Match the product: only a missing installation permits trying
                # a different browser; a broken/in-use profile needs attention.
                if "Executable doesn't exist" not in str(exc) and "not found" not in str(exc):
                    break
    return result
