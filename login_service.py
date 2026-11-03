"""Remote, loopback-only browser service. Never logs request bodies or credentials.

The service survives SSH disconnects. A short-lived client transports JSON over
SSH stdin; screenshots stay in memory. Firefox owns its persistent session data.
"""

import fcntl
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.actions.action_builder import ActionBuilder
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support.ui import WebDriverWait

from profile_lock import ProfileLock
from login_sites import SITES

ROOT = Path.home() / ".local/share/geo-browser"
PORT = 8877
TOKEN_FILE = ROOT / "login-service.token"
STATE_FILE = ROOT / "login-readiness.json"
ACTIVE_FILE = ROOT / "login-center-active.json"
PROFILE = ROOT / "profiles/central-login"
CATALOG = {s["id"]: s for s in SITES}
KEYS = {"ENTER": Keys.ENTER, "TAB": Keys.TAB, "BACKSPACE": Keys.BACKSPACE,
        "ESCAPE": Keys.ESCAPE, "SELECT_ALL": Keys.CONTROL + "a"}


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def safe_url(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


class LoginBrowser:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else ROOT
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_file = self.root / "login-readiness.json"
        self.active_file = self.root / "login-center-active.json"
        self.profile = self.root / "profiles/central-login"
        self.profile_lock = None
        self.driver = None
        self.handles = {}
        self.base_handles = {}
        self.frames = {}
        self.lock = threading.RLock()
        self.preparing = False
        self.items = {s["id"]: {**s, "state": "not_prepared", "message": "尚未准备",
                                "methods": [], "confirmed": False} for s in SITES}
        if self.state_file.exists():
            try:
                saved = json.loads(self.state_file.read_text())
                for item in saved.get("platforms", []):
                    if item.get("id") in self.items:
                        self.items[item["id"]].update({k: v for k, v in item.items()
                            if k in ("state", "message", "methods", "checked_at", "page_url", "confirmed")})
            except (OSError, ValueError, TypeError):
                pass

    def status(self):
        return {"connected": True, "active": self.driver is not None,
                "preparing": self.preparing, "platforms": [dict(self.items[s["id"]],
                    opened=self.driver is not None and s["id"] in self.handles) for s in SITES]}

    def persist(self):
        self.state_file.write_text(json.dumps(self.status(), ensure_ascii=False, indent=2), encoding="utf-8")
        self.state_file.chmod(0o600)

    def start(self):
        if self.driver:
            try:
                if self.driver.window_handles:
                    return
            except WebDriverException:
                pass
            self.driver = None
            self.handles.clear()
            self.base_handles.clear()
            self.frames.clear()
            if self.profile_lock:
                self.profile_lock.release()
                self.profile_lock = None
            self.active_file.unlink(missing_ok=True)
        # A saved session must not be opened by the collector at the same time.
        self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        profile_lock = ProfileLock(self.root / "profile-use.lock")
        if not profile_lock.acquire():
            raise WebDriverException("PROFILE_IN_USE")
        self.profile_lock = profile_lock
        options = Options()
        options.add_argument("-headless")
        options.add_argument("-profile")
        options.add_argument(str(self.profile))
        options.set_preference("dom.ipc.processCount", 2)
        options.set_preference("media.autoplay.default", 5)
        options.page_load_strategy = "eager"
        service = Service(executable_path=str(Path.home() / ".local/bin/geckodriver"), log_output=os.devnull)
        try:
            self.driver = webdriver.Firefox(options=options, service=service)
            self.driver.set_window_size(1365, 950)
            self.driver.set_page_load_timeout(30)
            self.active_file.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
            self.active_file.chmod(0o600)
        except Exception:
            try:
                if self.driver:
                    self.driver.quit()
            except WebDriverException:
                pass
            self.driver = None
            self.active_file.unlink(missing_ok=True)
            self.profile_lock.release()
            self.profile_lock = None
            raise

    def select(self, site_id):
        if site_id not in CATALOG:
            raise ValueError("未知平台")
        if not self.driver or site_id not in self.handles:
            raise ValueError("请先准备该平台的登录页")
        if self.handles[site_id] not in self.driver.window_handles:
            self.handles[site_id] = self.base_handles.get(site_id)
        self.driver.switch_to.window(self.handles[site_id])
        self.driver.switch_to.default_content()

    def inspect(self, site_id):
        data = self.driver.execute_script(r"""
            const visible=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0};
            const text=document.body?.innerText||'';
            const buttons=[...document.querySelectorAll('button,a,[role="button"]')]
              .filter(visible).map(e=>(e.innerText||e.getAttribute('aria-label')||'').trim());
            const inputs=[...document.querySelectorAll('input')].filter(visible)
              .map(e=>(e.getAttribute('placeholder')||'')+' '+(e.getAttribute('autocomplete')||''));
            return {title:document.title, textLength:text.length,
              login:buttons.some(t=>/^(登录|立即登录|登录\/注册|登录或注册|Log in|Login|Sign in)$/i.test(t))
                || /手机号登录|扫码登录|验证码登录|WeChat QR login|Sign in to/.test(text),
              verify:/安全验证|请完成验证|验证你是人类|verify you are human/i.test(text),
              failed:/Access Denied|Forbidden|Service Unavailable|拒绝访问|无法访问此网站/i.test(text),
              methods:['手机号','验证码','扫码','微信','飞书','Google','邮箱'].filter(t=>text.includes(t)||inputs.some(x=>x.includes(t)))};
        """)
        state = "error" if data["failed"] else "verification" if data["verify"] else "login_ready" if data["login"] else "page_ready"
        if data["textLength"] < 20:
            state = "loading"
        message = {"verification": "页面有人工验证，集中登录时需处理", "login_ready": "登录页已打开，等待集中登录",
                   "page_ready": "网页已打开，登录状态待本人确认", "loading": "页面尚未加载完成",
                   "error": "网站返回访问错误，需恢复访问后再登录"}[state]
        self.items[site_id].update(state=state, message=message, methods=data["methods"],
                                  checked_at=now(), page_url=safe_url(self.driver.current_url))
        return data

    def open_login(self):
        # Only expose the login UI. Never accept agreements or request an OTP here.
        self.driver.execute_script("""
          const vis=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0;};
          const form=[...document.querySelectorAll('input')].some(e=>vis(e)&&
            (e.type==='password'||/手机|验证码|密码|email|phone/i.test(e.getAttribute('placeholder')||'')));
          if(form)return;
          const targets=['登录','立即登录','登录/注册','登录或注册','Log in','Login','Sign in'];
          const x=[...document.querySelectorAll('button,a,[role="button"]')].find(e=>{
            const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&targets.includes((e.innerText||'').trim());
          });if(x)x.click();
        """)

    def prepare_one(self, site_id):
        site = CATALOG[site_id]
        self.items[site_id].update(state="preparing", message="正在打开登录页")
        try:
            with self.lock:
                self.start()
                if site_id not in self.handles:
                    self.driver.switch_to.new_window("tab")
                    self.handles[site_id] = self.driver.current_window_handle
                    self.base_handles[site_id] = self.driver.current_window_handle
                self.select(site_id)
                try:
                    self.driver.get(site["url"])
                except WebDriverException:
                    pass
                try:
                    WebDriverWait(self.driver, 18).until(lambda d: d.execute_script(
                        "return (document.body?.innerText||'').length>100"))
                except WebDriverException:
                    pass
                # SPA login controls can appear after the chat composer.
                time.sleep(3)
                before = set(self.driver.window_handles)
                self.open_login()
                time.sleep(2)
                popups = [h for h in self.driver.window_handles if h not in before]
                if popups:
                    self.handles[site_id] = popups[-1]
                    self.select(site_id)
                self.inspect(site_id)
                self.frames.pop(site_id, None)
                self.persist()
        except Exception:
            self.items[site_id].update(state="error", message="页面未准备成功，可单独重试", checked_at=now())
            self.persist()

    def prepare(self, site_ids):
        if self.preparing:
            return self.status()
        self.preparing = True
        def job():
            try:
                for site_id in site_ids:
                    self.prepare_one(site_id)
            finally:
                self.preparing = False
                self.persist()
        threading.Thread(target=job, daemon=True).start()
        return self.status()

    def snapshot(self, site_id):
        self.select(site_id)
        self.inspect(site_id)
        frame = secrets.token_hex(12)
        self.frames[site_id] = frame
        width, height = self.driver.execute_script("return [innerWidth,innerHeight]")
        try:
            self.focused_element()
            focus = self.driver.execute_script("""
              const e=document.activeElement;
              return {editable:e?.tagName==='INPUT'&&!['file','hidden','button','submit'].includes(e.type),
                      label:e?.getAttribute('placeholder')||e?.getAttribute('aria-label')||'',
                      password:e?.type==='password'};
            """)
        finally:
            self.driver.switch_to.default_content()
        return {"site": dict(self.items[site_id]), "frame_id": frame, "width": width, "height": height,
                "focus": focus, "image": self.driver.get_screenshot_as_base64()}

    def focused_element(self):
        self.driver.switch_to.default_content()
        for _ in range(5):
            element = self.driver.switch_to.active_element
            if element.tag_name.lower() not in ("iframe", "frame"):
                return element
            self.driver.switch_to.frame(element)
        return self.driver.switch_to.active_element

    def action(self, payload):
        action = payload.get("action")
        if action == "status":
            return self.status()
        if action == "prepare":
            site_id = payload.get("site")
            if site_id is not None and site_id not in CATALOG:
                raise ValueError("未知平台")
            ids = [site_id] if site_id in CATALOG else list(CATALOG)
            return self.prepare(ids)
        if action == "finish":
            if self.preparing:
                raise ValueError("页面还在准备，请稍后结束")
            with self.lock:
                if self.driver:
                    try:
                        self.driver.quit()
                    except WebDriverException:
                        pass
                    self.driver = None
                self.handles.clear()
                self.base_handles.clear()
                self.frames.clear()
                self.active_file.unlink(missing_ok=True)
                try:
                    (self.root / "central-login.json").write_text(json.dumps({"saved_at": now()}), encoding="utf-8")
                    self.persist()
                finally:
                    if self.profile_lock:
                        self.profile_lock.release()
                        self.profile_lock = None
            return self.status()
        if self.preparing:
            raise ValueError("正在准备页面，完成后即可操作")
        site_id = payload.get("site")
        with self.lock:
            self.select(site_id)
            if action == "snapshot":
                return self.snapshot(site_id)
            if action == "confirmed":
                self.items[site_id]["confirmed"] = True
                self.persist()
                return self.snapshot(site_id)
            if action == "refresh":
                self.driver.refresh()
                time.sleep(2)
                return self.snapshot(site_id)
            if not payload.get("frame_id") or payload.get("frame_id") != self.frames.get(site_id):
                raise ValueError("画面已更新，请刷新画面后重试")
            self.frames.pop(site_id, None)
            before_windows = set(self.driver.window_handles)
            if action == "click":
                x, y = int(payload.get("x", -1)), int(payload.get("y", -1))
                w, h = self.driver.execute_script("return [innerWidth,innerHeight]")
                if not (0 <= x < w and 0 <= y < h):
                    raise ValueError("点击位置超出画面")
                actions = ActionBuilder(self.driver)
                actions.pointer_action.move_to_location(x, y)
                actions.pointer_action.click()
                actions.perform()
            elif action == "type":
                value = payload.get("text", "")
                if not isinstance(value, str) or not 1 <= len(value) <= 300:
                    raise ValueError("输入内容长度无效")
                element = self.focused_element()
                editable = self.driver.execute_script("""
                  const e=document.activeElement;return e?.tagName==='INPUT'&&!['file','hidden','button','submit'].includes(e.type);
                """)
                if not editable:
                    self.driver.switch_to.default_content()
                    raise ValueError("请先在画面中点选手机号、验证码或密码输入框")
                try:
                    element.send_keys(value)
                finally:
                    self.driver.switch_to.default_content()
            elif action == "key":
                key = payload.get("key")
                if key not in KEYS:
                    raise ValueError("不支持的按键")
                try:
                    self.focused_element().send_keys(KEYS[key])
                finally:
                    self.driver.switch_to.default_content()
            elif action == "scroll":
                self.driver.execute_script("scrollBy(0,arguments[0])", max(-700, min(700, int(payload.get("dy", 0)))))
            else:
                raise ValueError("不支持的操作")
            time.sleep(0.6)
            popups = [h for h in self.driver.window_handles if h not in before_windows]
            if popups:
                self.handles[site_id] = popups[-1]
            return self.snapshot(site_id)


def token():
    ROOT.mkdir(parents=True, exist_ok=True)
    if not TOKEN_FILE.exists():
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
    return TOKEN_FILE.read_text().strip()


def call(payload):
    req = Request(f"http://127.0.0.1:{PORT}/request", data=json.dumps(payload).encode(),
                  headers={"Content-Type": "application/json", "X-Login-Token": token()})
    with urlopen(req, timeout=55) as response:
        return json.load(response)


def serve():
    ROOT.mkdir(parents=True, exist_ok=True)
    lock = (ROOT / "login-service.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    secret = token()
    browser = LoginBrowser()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_POST(self):
            if self.path != "/request" or self.headers.get("X-Login-Token") != secret:
                self.send_error(403)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 8192:
                    raise ValueError("请求无效")
                payload = json.loads(self.rfile.read(size))
                result = browser.action(payload)
            except ValueError as exc:
                result = {"error": str(exc)}
            except Exception:
                result = {"error": "浏览器操作未完成，请刷新状态后重试"}
            data = json.dumps(result, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        serve()
    else:
        payload = json.load(sys.stdin)
        try:
            result = call(payload)
        except (OSError, URLError):
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "serve"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            for _ in range(20):
                time.sleep(0.2)
                try:
                    result = call(payload)
                    break
                except (OSError, URLError):
                    pass
            else:
                result = {"error": "登录服务未能启动"}
        print(json.dumps(result, ensure_ascii=False))
