"""Firefox worker installed under geo on the authorized collection host.

Reads a small JSON batch from stdin and writes one machine-readable result line.
The browser uses a dedicated profile so a user can log into a site in the remote
desktop without putting cookies or credentials in the GEO project.
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support.ui import WebDriverWait

from profile_lock import ProfileLock


ROOT = Path.home() / ".local" / "share" / "geo-browser"
GECKODRIVER = Path.home() / ".local" / "bin" / "geckodriver"
PREFIX = "GEO_BROWSER_RESULT:"
SITES = {
    "DeepSeek": {
        "slug": "deepseek",
        "url": "https://chat.deepseek.com/",
        "editor": 'textarea#chat-input, textarea[placeholder]',
        "answer": '.ds-markdown',
        "question_container": "body",
    },
    "豆包": {
        "slug": "doubao",
        "url": "https://www.doubao.com/chat/",
        "editor": '[data-testid="chat_input"] [contenteditable="true"]',
        "answer": '[data-testid="receive_message"] [data-testid="message_text_content"]',
        "question_container": '[data-testid="message-list"]',
    },
    "Kimi": {
        "slug": "kimi",
        "url": "https://www.kimi.com/",
        "editor": '[contenteditable="true"][role="textbox"]',
        "answer": '[data-testid="message_text_content"]',
        "question_container": "main",
    },
}


def result(question_id, status="error", **fields):
    return {"question_id": question_id, "status": status, "asked_at": "",
            "answer": "", "page_url": "", "evidence_url": "",
            "screenshot_base64": "", "error_message": "", **fields}


def screenshot(driver):
    try:
        return driver.get_screenshot_as_base64()
    except WebDriverException:
        return ""


def page_url(driver):
    try:
        return driver.current_url
    except WebDriverException:
        return ""


def driver_for(site, participant_root=None):
    root = Path(participant_root) if participant_root is not None else ROOT
    active = root / "login-center-active.json"
    if active.exists():
        try:
            os.kill(json.loads(active.read_text())["pid"], 0)
        except (OSError, ValueError, KeyError):
            pass
        else:
            raise WebDriverException("LOGIN_CENTER_ACTIVE")
    if participant_root is not None and not (root / "central-login.json").is_file():
        raise WebDriverException("LOGIN_PROFILE_NOT_SAVED")
    profile = root / "profiles" / ("central-login" if (root / "central-login.json").exists() else site["slug"])
    profile.mkdir(parents=True, exist_ok=True)
    options = Options()
    options.add_argument("-headless")
    options.add_argument("-profile")
    options.add_argument(str(profile))
    options.page_load_strategy = "eager"
    service = Service(executable_path=str(GECKODRIVER), log_output=os.devnull)
    driver = webdriver.Firefox(options=options, service=service)
    driver.set_window_size(1365, 900)
    driver.set_page_load_timeout(45)
    return driver


def login_visible(driver):
    try:
        return bool(driver.execute_script(r"""
            const text = document.body?.innerText || '';
            return /\/sign_in\/?$/.test(location.pathname)
              || !!document.querySelector('.login-modal-next-mask, [data-testid=login-modal]')
              || (text.includes('手机号登录') && text.includes('扫码'))
              || (text.includes('WeChat QR login') && text.includes('Send code'));
        """))
    except WebDriverException:
        return False


def close_doubao_promo(driver):
    try:
        driver.execute_script("""
            const buttons = [...document.querySelectorAll('button,[role="button"]')];
            const close = buttons.find(e => {
              const text = ((e.getAttribute('aria-label') || '') + ' ' +
                            (e.getAttribute('title') || '')).toLowerCase();
              return text.includes('关闭') || text.includes('close');
            });
            if (close) close.click();
        """)
    except WebDriverException:
        pass


def wait_answer(driver, site, previous_count=0):
    end = time.monotonic() + 115
    last = ""
    unchanged_since = time.monotonic()
    while time.monotonic() < end:
        if login_visible(driver):
            return "", "页面要求登录"
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, site["answer"])
            element = elements[-1] if len(elements) > previous_count else None
            answer = element.text.strip() if element else ""
            streaming = element.get_attribute("data-streaming") if element else None
        except WebDriverException:
            answer = ""
            streaming = None
        if answer != last:
            last = answer
            unchanged_since = time.monotonic()
        if answer and streaming != "true" and time.monotonic() - unchanged_since >= 7:
            return answer, ""
        time.sleep(1)
    return "", "等待网页回答超时"


def collect_one(driver, site, question):
    qid = question["id"]
    row = result(qid)
    stage = "open"
    try:
        driver.get(site["url"])
        stage = "find_input"
        editor = WebDriverWait(driver, 35).until(
            lambda d: next((node for node in d.find_elements(
                By.CSS_SELECTOR, site["editor"]) if node.is_displayed()), None)
        )
        if login_visible(driver):
            return result(qid, page_url=page_url(driver),
                          screenshot_base64=screenshot(driver), error_message="页面要求登录")
        stage = "type"
        previous_count = (len(driver.find_elements(By.CSS_SELECTOR, site["answer"]))
                          if site["slug"] == "deepseek" else 0)
        editor.click()
        editor.send_keys(question["text"])
        if login_visible(driver):
            return result(qid, page_url=page_url(driver),
                          screenshot_base64=screenshot(driver), error_message="页面要求登录")
        sent_at = datetime.now().astimezone().isoformat(timespec="seconds")
        stage = "submit"
        editor.send_keys(Keys.ENTER)
        def appeared(d):
            if login_visible(d):
                return True
            try:
                return d.execute_script("""
                    const prompt = arguments[0], box = arguments[1], input = arguments[2];
                    return [...document.querySelectorAll(box)].some(e => e.innerText.includes(prompt))
                      && ![...document.querySelectorAll(input)].some(e => (e.value || e.innerText || '').trim());
                """, question["text"], site["question_container"], site["editor"])
            except WebDriverException:
                return False
        WebDriverWait(driver, 20).until(appeared)
        if login_visible(driver):
            return result(qid, page_url=page_url(driver),
                          screenshot_base64=screenshot(driver), error_message="页面要求登录")
        row["asked_at"] = sent_at
        stage = "answer"
        answer, error = wait_answer(driver, site, previous_count)
        if site["slug"] == "doubao":
            close_doubao_promo(driver)
        row.update(page_url=page_url(driver), answer=answer,
                   screenshot_base64=screenshot(driver),
                   error_message=error, status="complete" if answer else "error")
        return row
    except WebDriverException as exc:
        row.update(page_url=page_url(driver), screenshot_base64=screenshot(driver),
                   error_message=f"网页加载或输入失败（{stage}:{type(exc).__name__}）")
        return row


def main():
    try:
        payload = json.load(sys.stdin)
        site = SITES[payload["platform"]]
        questions = payload["questions"]
        if not isinstance(questions, list) or not 1 <= len(questions) <= 3:
            raise ValueError("invalid batch")
        if any(not isinstance(q.get("id"), int) or
               not isinstance(q.get("text"), str) or not q["text"].strip()
               for q in questions):
            raise ValueError("invalid questions")
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        print(PREFIX + json.dumps({"results": []}, ensure_ascii=False))
        return
    try:
        driver = driver_for(site)
    except WebDriverException as exc:
        message = ("请先在平台登录中心点「保存并结束登录」再采集" if "LOGIN_CENTER_ACTIVE" in str(exc)
                   else "Firefox 启动失败；请确认采集专用浏览器已关闭")
        rows = [result(q["id"], error_message=message)
                for q in questions]
    else:
        try:
            rows = [collect_one(driver, site, q) for q in questions]
        finally:
            try:
                driver.quit()
            except WebDriverException:
                pass
    print(PREFIX + json.dumps({"results": rows}, ensure_ascii=False))


if __name__ == "__main__":
    main()
