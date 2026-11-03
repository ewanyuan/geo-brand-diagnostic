"""Persistent local Chrome workspace. No account inputs or screenshots are logged.

All Playwright operations run on one worker thread. HTTP/CLI requests only
transport actions; the browser survives the end of each client connection.
"""

import base64
import concurrent.futures
import json
import os
import queue
import re
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
from urllib.request import ProxyHandler, Request, build_opener

from playwright.sync_api import sync_playwright

from browser_runtime import configuration
from login_sites import SITES
from app_paths import child_command, user_home
from profile_lock import ProfileLock

ROOT = user_home()
ROOT.mkdir(parents=True, exist_ok=True)
PORT = 8878
TOKEN = ROOT / "login-service.token"
INSTANCE = ROOT / "browser-service.json"
CATALOG = {site["id"]: site for site in SITES}
KEYS = {"ENTER": "Enter", "TAB": "Tab", "BACKSPACE": "Backspace", "ESCAPE": "Escape", "SELECT_ALL": "Control+a"}
CHAT = {
    "deepseek": {"editor": 'textarea#chat-input, textarea[placeholder]', "answer": '.ds-markdown', "user": 'body'},
    "doubao": {"editor": 'textarea[placeholder*="发消息"], [data-testid="chat_input"] [contenteditable="true"]',
               "answer": '[data-testid="receive_message"] [data-testid="message_text_content"]', "user": '[data-testid="send_message"]'},
    "kimi": {"editor": '[contenteditable="true"][role="textbox"], .chat-input-editor, textarea',
             "answer": '.segment-assistant .markdown-container:not(.toolcall-content-text) > .markdown', "user": '.segment-user'},
}


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def safe_url(url):
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def browser_error_message(stage, exc):
    if isinstance(exc, ValueError):
        return str(exc)
    headline = str(exc).splitlines()[0] if str(exc) else ""
    operation = re.match(r"^(?:Locator|Page|ElementHandle)\.[a-z_]+", headline)
    network = re.search(r"net::ERR_[A-Z0-9_]+", headline)
    code = network.group() if network else ("输入框尚未就绪" if "not editable" in headline else type(exc).__name__)
    detail = (operation.group() + " / " if operation else "") + code
    return f"{stage}未完成（{detail}），请查看本机浏览器的登录、验证或网络提示"


class Workspace:
    def __init__(self, root=None, runtime_config=None):
        self.root = Path(root) if root is not None else ROOT
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runtime_config = runtime_config
        self.engine = None
        self.context = None
        self.pages = {}
        self.base_pages = {}
        self.frames = {}
        self.preparing = False
        self.collecting = False
        self.stopping = False
        self.request_lock = threading.Lock()
        self.browser_channel = ""
        self.items = {s["id"]: {**s, "state": "not_prepared", "message": "尚未准备", "methods": [], "confirmed": False} for s in SITES}
        try:
            saved = json.loads((self.root / "login-readiness.json").read_text(encoding="utf-8"))
            for item in saved.get("platforms", []):
                if item.get("id") in self.items and item.get("last_answer_at"):
                    self.items[item["id"]]["last_answer_at"] = item["last_answer_at"]
        except (OSError, ValueError, TypeError):
            pass
        self.jobs = queue.Queue()
        self.thread = threading.Thread(target=self.work, daemon=True)
        self.thread.start()

    def work(self):
        while True:
            payload, future = self.jobs.get()
            completed = False
            try:
                result = self.perform(payload)
                completed = True
                if future:
                    future.set_result(result)
            except Exception as exc:
                if future:
                    future.set_result({"error": str(exc) if isinstance(exc, ValueError) else "网页操作未完成，请检查本机浏览器窗口"})
            finally:
                if payload.get("action") == "shutdown" and not completed:
                    self.stopping = False
                if payload.get("action") == "collect":
                    self.collecting = False
                self.jobs.task_done()
            if completed and payload.get("action") == "shutdown":
                return

    def status(self):
        return {"connected": True, "active": self.context is not None,
                "preparing": self.preparing, "runtime": "local", "location": "本机专用浏览器",
                "collecting": self.collecting, "native": True, "browser": self.browser_channel,
                "platforms": [dict(self.items[s["id"]]) for s in SITES]}

    def persist(self):
        (self.root / "login-readiness.json").write_text(json.dumps(self.status(), ensure_ascii=False, indent=2), encoding="utf-8")

    def request(self, payload):
        action = payload.get("action")
        if action == "status":
            return self.status()
        with self.request_lock:
            if self.stopping:
                return {"error": "GEO 浏览器正在退出，请稍后重试"}
            if self.collecting:
                return {"error": "GEO 正在提问，请等当前批次结束后再操作浏览器"}
            if self.preparing:
                return {"error": "正在打开浏览器页面，请稍等"}
            if action == "prepare":
                site = payload.get("site")
                if site is not None and site not in CATALOG:
                    return {"error": "未知平台"}
                self.preparing = True
                self.jobs.put((payload, None))
                return self.status()
            if action == "collect":
                self.collecting = True
            if action == "shutdown":
                self.stopping = True
            future = concurrent.futures.Future()
            self.jobs.put((payload, future))
        return future.result(timeout=700 if action == "collect" else 55)

    def start(self):
        if self.context:
            try:
                if self.context.pages:
                    return
            except Exception:
                pass
            self.context = None
            self.pages.clear(); self.base_pages.clear(); self.frames.clear()
        if not self.engine:
            self.engine = sync_playwright().start()
        cfg = self.runtime_config if self.runtime_config is not None else configuration()
        proxy = cfg.get("proxy", "")
        options = {"headless": False, "no_viewport": True,
                   "accept_downloads": False, "args": ["--disable-background-networking"]}
        if proxy:
            p = urlsplit(proxy)
            if p.hostname not in ("127.0.0.1", "localhost") or p.username or p.password:
                raise ValueError("只允许使用已有的本机代理")
            if cfg.get("network_route") == "physical-cn":
                import browser_network
                proxy = browser_network.start(proxy, int(cfg.get("direct_interface", 9)))
                options["proxy"] = {"server": proxy, "bypass": "localhost,127.0.0.1"}
            else:
                options["proxy"] = {"server": proxy, "bypass": cfg.get("proxy_bypass", "localhost,127.0.0.1")}
        from browser_discovery import browser_candidates, external_environment
        for channel, browser_options in browser_candidates(cfg.get("browser_channel")):
            try:
                self.context = self.engine.chromium.launch_persistent_context(
                    str(self.root / "browser-profile"), **options,
                    **browser_options, env=external_environment())
                self.browser_channel = channel
                break
            except Exception as exc:
                if "Executable doesn't exist" not in str(exc) and "not found" not in str(exc):
                    raise ValueError("专用浏览器未能启动，请关闭旧的 GEO 浏览器窗口后重试") from None
        if not self.context:
            raise ValueError("未找到可用浏览器，请安装 Chrome 或 Edge；Linux 也可使用系统 Chromium")
        context = self.context
        context.on("close", lambda: self.browser_closed(context))
        self.context.set_default_timeout(10000)
        self.context.set_default_navigation_timeout(35000)

    def browser_closed(self, context):
        if self.context is context:
            self.context = None
            self.pages.clear(); self.base_pages.clear(); self.frames.clear()

    def open_one(self, site_id):
        if site_id not in CATALOG:
            raise ValueError("未知平台")
        self.start()
        page = self.pages.get(site_id)
        if page is None or page.is_closed():
            page = self.context.new_page()
            self.pages[site_id] = self.base_pages[site_id] = page
            try:
                page.goto(CATALOG[site_id]["url"], wait_until="domcontentloaded", timeout=25000)
            except Exception:
                pass
        page.bring_to_front()
        self.inspect(site_id)
        self.persist()
        return self.status()

    def page(self, site_id):
        if site_id not in CATALOG or not self.context:
            raise ValueError("请先准备该平台登录页")
        page = self.pages.get(site_id)
        if page is None or page.is_closed():
            page = self.base_pages.get(site_id)
        if page is None or page.is_closed():
            raise ValueError("这个平台的页面已关闭，请重新准备")
        self.pages[site_id] = page
        return page

    def inspect(self, site_id):
        page = self.page(site_id)
        data = page.evaluate(r"""() => {
          const vis=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0};
          const t=document.body?.innerText||'';
          const b=[...document.querySelectorAll('button,a,[role="button"]')].filter(vis).map(e=>(e.innerText||e.getAttribute('aria-label')||'').trim());
          const i=[...document.querySelectorAll('input')].filter(vis).map(e=>e.getAttribute('placeholder')||'');
          return {length:t.length,title:document.title,
            login:b.some(x=>/^(登录|立即登录|登录\/注册|登录或注册|Log in|Login|Sign in)$/i.test(x))||/手机号登录|扫码登录|验证码登录|WeChat QR login|Sign in to|手机号码/.test(t),
            verification:/安全验证|请完成验证|验证你是人类|verify you are human|just a moment/i.test(t+' '+document.title),
            failed:/Access Denied|Forbidden|Service Unavailable|403 ERROR|request could not be satisfied|Request blocked|拒绝访问|无法访问此网站|暂不支持.*地区|not available in your region/i.test(t),
            methods:['手机号','验证码','扫码','微信','飞书','Google','邮箱'].filter(x=>t.includes(x)||i.some(y=>y.includes(x)))};
        }""")
        if re.search(r"region-ban|access.?denied", page.url, re.I):
            data["failed"] = True
        for frame in page.frames[1:]:
            try:
                if not frame.frame_element().is_visible():
                    continue
                text = frame.locator("body").inner_text(timeout=1000)
                for method in ('手机号','验证码','扫码','微信','飞书','Google','邮箱'):
                    if method in text and method not in data["methods"]:
                        data["methods"].append(method)
                if re.search(r'手机号|验证码|扫码登录|密码登录', text):
                    data["login"] = True
            except Exception:
                pass
        state = "error" if data["failed"] else "verification" if data["verification"] else "login_ready" if data["login"] else "page_ready"
        if data["length"] < 20:
            state = "loading"
        messages = {"error": "网站返回访问错误，需确认访问条件", "verification": "页面有人工验证，集中登录时处理",
                    "login_ready": "登录页面已打开，等待集中登录", "page_ready": "网页已打开，登录状态待本人确认", "loading": "页面尚未加载完成"}
        self.items[site_id].update(state=state, message=messages[state], methods=data["methods"],
                                  checked_at=now(), page_url=safe_url(page.url), chat_ready=False)

    def login_visible(self, page):
        return page.evaluate(r"""() => {
          const visible=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0
            &&e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})};
          return /\/sign_in\/?$/.test(location.pathname)
            || [...document.querySelectorAll('.login-modal-next-mask,[data-testid="login-modal"],input')].some(e=>visible(e)&&(
              e.matches('.login-modal-next-mask,[data-testid="login-modal"]')
              ||e.type==='password'||e.autocomplete==='one-time-code'
              ||/手机号|手机号码|验证码|phone number|verification code/i.test(e.placeholder||'')));
        }""")

    def answer_finished(self, answer, site_id):
        if site_id == "doubao":
            return answer.get_attribute("data-streaming") == "false"
        if site_id == "kimi":
            return answer.evaluate(r"""e => {
              const row=e.closest('.segment-assistant');
              const tools=row?.querySelector('.segment-assistant-actions');
              if(!tools)return false;
              const r=tools.getBoundingClientRect();
              return r.width>0&&r.height>0&&tools.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})&&tools.querySelectorAll('svg').length>=3;
            }""")
        # DeepSeek currently exposes icon-only stop/send controls. Require the
        # completed response's own toolbar, never a historical response's tools.
        return answer.evaluate(r"""e => {
          const row=e.closest('[data-virtual-list-item-key]');
          if(!row)return false;
          return [...row.querySelectorAll('[role="button"],button')].some(b=>
            /^(朗读|Read aloud|Read out loud)$/i.test(b.getAttribute('aria-label')||''));
        }""")

    def editor(self, page, site_id):
        return page.locator(CHAT[site_id]["editor"]).filter(visible=True).first

    def site_error(self, page):
        # Scope service errors to a visible popup, never the model's answer text.
        return page.evaluate(r"""() => {
          const visible=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0
            &&e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})};
          const roots=[...document.querySelectorAll('[role="dialog"],[role="alertdialog"],[class*="modal"],[class*="dialog"]')].filter(visible);
          for(const root of roots){
            const text=root.innerText||'';
            if(/和\s*Kimi\s*聊天的人太多了|服务繁忙|请求过于频繁|达到.*(?:限额|上限)|次数已用完|rate limit|too many requests/i.test(text))
              return '平台当前繁忙或额度受限，请稍后重试；本次未取得有效回答';
          }
          return '';
        }""")

    def ensure_no_login(self, page, message, grace=3):
        # Some SPAs briefly mount the sign-in form while restoring an already
        # valid account during navigation. A persistent prompt still fails.
        deadline = time.monotonic() + grace
        while self.login_visible(page):
            if time.monotonic() >= deadline:
                raise ValueError(message)
            page.wait_for_timeout(200)

    def watch_sent(self, page, site_id, prompt):
        if site_id == "deepseek":
            return ""
        key = "__geo_send_" + secrets.token_hex(12)
        page.evaluate(r"""({key,prompt,selector}) => {
          const normalize = text => (text||'').replace(/\s+/g,'');
          const wanted = normalize(prompt);
          const previous = new WeakSet(document.querySelectorAll(selector));
          const state = {sent:false};
          const check = node => {
            if(node.nodeType!==1)return;
            const closest = node.closest(selector);
            const candidates = [...(closest?[closest]:[]),...node.querySelectorAll(selector)];
            if(candidates.some(e=>!previous.has(e)&&normalize(e.textContent).includes(wanted))){state.sent=true;state.observer?.disconnect();}
          };
          state.observer = new MutationObserver(records=>{
            for(const record of records){
              for(const node of record.addedNodes)check(node);
              if(record.target.nodeType===1)check(record.target);
              else if(record.target.parentElement)check(record.target.parentElement);
            }
          });
          state.observer.observe(document.body,{childList:true,subtree:true,characterData:true});
          window[key]=state;
        }""", {"key":key,"prompt":prompt,"selector":CHAT[site_id]["user"]})
        return key

    def confirm_sent(self, page, input_node, site_id, prompt, watch_key="", timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            refusal = self.site_error(page)
            if refusal:
                raise ValueError(refusal)
            self.ensure_no_login(page,"发送时平台要求登录，请在浏览器完成后重试")
            sent = page.evaluate("""({prompt,box,userSelector,key}) =>
              (Boolean(key&&window[key]?.sent)||[...document.querySelectorAll(userSelector)].some(e=>(e.innerText||'').replace(/\\s+/g,'').includes(prompt.replace(/\\s+/g,''))))
              && (!box.isConnected || !(box.value===undefined?box.innerText:box.value)?.trim())
            """, {"prompt": prompt, "box": input_node, "userSelector": CHAT[site_id]["user"], "key":watch_key})
            if sent:
                return
            page.wait_for_timeout(200)
        raise ValueError("未确认问题已发送，请查看本机浏览器；不要连续重复点击采集")

    def check_one(self, site_id):
        page = self.page(site_id)
        self.inspect(site_id)
        ready = (site_id in CHAT and not self.login_visible(page)
                 and self.editor(page, site_id).count() > 0
                 and self.editor(page, site_id).is_editable())
        self.items[site_id].update(chat_ready=bool(ready))
        if ready:
            self.items[site_id].update(state="chat_ready", message="已找到可输入的聊天框，可回诊断台尝试采集")
        self.persist()
        return self.status()

    def inspect_chat(self, site_id):
        """Local maintenance metadata only; never return input values or chat text."""
        page = self.page(site_id)
        return {"page_url": safe_url(page.url), "dom": page.evaluate(r"""() => {
          const visible=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0
            &&e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})};
          const meta=e=>({tag:e.tagName,cls:typeof e.className==='string'?e.className.slice(0,200):'',role:e.getAttribute('role'),
            editable:e.isContentEditable,disabled:Boolean(e.disabled),readOnly:Boolean(e.readOnly),inert:Boolean(e.closest('[inert]')),
            testid:e.getAttribute('data-testid'),aria:/^(朗读|Read aloud|Read out loud|Copy|复制|发送|Send|停止生成|停止回答|Stop generating|Stop response|doc_editor)$/i.test(e.getAttribute('aria-label')||'')?e.getAttribute('aria-label'):null,has_aria:e.hasAttribute('aria-label'),
            placeholder:e.getAttribute('placeholder'),streaming:e.getAttribute('data-streaming'),
            state:e.getAttribute('data-state')});
          const editors=[...document.querySelectorAll('textarea,[contenteditable="true"]')].filter(visible);
          const blocks=[...document.querySelectorAll('[data-testid*="message"],[class*="markdown"],[class*="assistant"],[class*="segment"]')].filter(visible);
          return {editors:editors.map(e=>({node:meta(e),parents:[meta(e.parentElement),meta(e.parentElement.parentElement)]})),
            message_nodes:blocks.slice(-35).map(e=>({node:meta(e),parents:[meta(e.parentElement),meta(e.parentElement.parentElement)],
              controls:[...e.parentElement.parentElement.querySelectorAll('button,[role="button"]')].slice(-18).map(meta)})),
            testids:[...new Set([...document.querySelectorAll('[data-testid]')].map(e=>e.getAttribute('data-testid')))].filter(t=>/message|chat|send|stop|copy|input|answer/i.test(t)).slice(0,120),
            editor_controls:editors.flatMap(e=>[...e.parentElement.parentElement.parentElement.querySelectorAll('button,[role="button"],[data-testid]')].filter(visible).map(meta))};
        }""")}

    def expose_login(self, page):
        # Stop at login entry points. Never click consent, send-code or submit controls.
        info = page.evaluate("""() => [...document.querySelectorAll('input')].some(e=>{
          const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&(e.type==='password'||/手机|验证码|密码|email|phone/i.test(e.placeholder||''));})""")
        if info:
            return
        if re.search(r"region-ban|access.?denied", page.url, re.I):
            return
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            for label in ('登录','立即登录','登录/注册','登录或注册','Log in','Login','Sign in'):
                for candidate in page.get_by_text(label, exact=True).all():
                    if candidate.is_visible():
                        candidate.click(timeout=5000)
                        return
            page.wait_for_timeout(300)

    def prepare_one(self, site_id):
        self.items[site_id].update(state="preparing", message="正在打开登录页")
        phase = "start"
        try:
            self.start()
            phase = "navigate"
            page = self.pages.get(site_id)
            if not page or page.is_closed():
                page = self.context.new_page()
                self.pages[site_id] = self.base_pages[site_id] = page
            try:
                page.goto(CATALOG[site_id]["url"], wait_until="domcontentloaded")
            except Exception:
                pass
            try:
                page.wait_for_function("(document.body?.innerText||'').length>100", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(2500)
            before = list(self.context.pages)
            try:
                self.expose_login(page)
            except Exception:
                pass
            page.wait_for_timeout(1200)
            new = [p for p in self.context.pages if p not in before]
            if new:
                self.pages[site_id] = new[-1]
            self.inspect(site_id)
            self.frames.pop(site_id, None)
        except Exception as exc:
            self.items[site_id].update(state="error", message=f"页面未准备成功（{phase}:{type(exc).__name__}），可单独重试", checked_at=now())
        self.persist()

    def focus(self, page):
        for frame in page.frames:
            try:
                info = frame.evaluate("""() => {const e=document.activeElement;return {
                  editable:document.hasFocus()&&e?.tagName==='INPUT'&&!['file','hidden','button','submit'].includes(e.type),
                  label:e?.getAttribute('placeholder')||e?.getAttribute('aria-label')||'', password:e?.type==='password'};}""")
                if info["editable"]:
                    return info
            except Exception:
                pass
        return {"editable": False, "label": "", "password": False}

    def snapshot(self, site_id):
        page = self.page(site_id)
        self.inspect(site_id)
        frame_id = secrets.token_hex(12)
        self.frames[site_id] = frame_id
        width, height = page.evaluate("[innerWidth,innerHeight]")
        return {"site": dict(self.items[site_id]), "frame_id": frame_id, "width": width, "height": height,
                "focus": self.focus(page), "image": base64.b64encode(page.screenshot()).decode()}

    def collect(self, payload):
        platform = payload.get("platform")
        site_id = {"DeepSeek": "deepseek", "豆包": "doubao", "Kimi": "kimi"}.get(platform)
        questions = payload.get("questions", [])
        if not site_id or not isinstance(questions, list) or not 1 <= len(questions) <= 3:
            raise ValueError("采集请求无效")
        if any(not isinstance(q, dict) or not isinstance(q.get("id"), int)
               or not isinstance(q.get("text"), str) or not 1 <= len(q["text"].strip()) <= 1000 for q in questions):
            raise ValueError("问题句无效")
        self.start()
        rows = []
        for question in questions:
            row = {"question_id": question["id"], "status": "error", "answer": "", "asked_at": "", "error_message": ""}
            page = None
            listeners = []
            watch_key = ""
            stage = "打开网页"
            try:
                page = self.context.new_page()
                self.pages[site_id] = self.base_pages[site_id] = page
                page.bring_to_front()
                page.goto(CATALOG[site_id]["url"], wait_until="domcontentloaded")
                stage = "检查聊天框"
                page.wait_for_timeout(1500)
                if self.login_visible(page):
                    raise ValueError("请在已打开的本机浏览器完成登录，再回 GEO 重新采集")
                editor = self.editor(page, site_id)
                editor.wait_for(state="visible", timeout=25000)
                if self.login_visible(page):
                    raise ValueError("网页要求登录，请在本机浏览器中完成")
                selectors = CHAT[site_id]["answer"]
                baseline = page.locator(selectors).count()
                stage = "发送问题"
                editor.fill(question["text"])
                if self.login_visible(page):
                    raise ValueError("网页要求登录，请在本机浏览器中完成")
                sent_at = now()
                input_node = editor.element_handle()
                generation = {"request": None, "finished": False, "failed": False}
                def started(req):
                    if req.method == "POST" and re.search(r"/chat/completions?/?$", urlsplit(req.url).path):
                        generation["request"] = req
                def finished(req):
                    if req is generation["request"]:
                        response = req.response()
                        generation["finished"] = bool(response and 200 <= response.status < 300)
                        generation["failed"] = not generation["finished"]
                def failed(req):
                    if req is generation["request"]:
                        generation["failed"] = True
                page.on("request", started)
                page.on("requestfinished", finished)
                page.on("requestfailed", failed)
                listeners = [("request", started), ("requestfinished", finished), ("requestfailed", failed)]
                watch_key = self.watch_sent(page, site_id, question["text"])
                editor.press("Enter")
                self.confirm_sent(page, input_node, site_id, question["text"], watch_key)
                row["asked_at"] = sent_at
                stage = "等待完整回答"
                last = ""; changed = time.monotonic(); deadline = time.monotonic() + 115
                while time.monotonic() < deadline:
                    self.ensure_no_login(page,"发送时平台要求重新登录，请完成后重试")
                    refusal = self.site_error(page)
                    if refusal:
                        raise ValueError(refusal)
                    if generation["failed"]:
                        raise ValueError("平台回答连接中断或返回错误，本次不计入有效样本")
                    answers = page.locator(selectors)
                    count = answers.count()
                    if site_id == "kimi" and count > baseline:
                        answer = "\n\n".join(answers.nth(i).inner_text(timeout=3000).strip() for i in range(baseline, count)).strip()
                    else:
                        answer = answers.last.inner_text(timeout=3000).strip() if count > baseline else ""
                    streaming = page.evaluate(r"""() => {
                      const visible=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0
                        &&e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})};
                      return [...document.querySelectorAll('[data-streaming="true"],button,[role="button"]')].some(e=>visible(e)&&(
                        e.getAttribute('data-streaming')==='true'||/^(停止生成|停止回答|停止输出|Stop generating|Stop response)$/i.test(
                        (e.getAttribute('aria-label')||e.getAttribute('title')||e.innerText||'').trim())));
                    }""")
                    if answer != last:
                        last = answer; changed = time.monotonic()
                    ended = bool(answer and self.answer_finished(answers.last, site_id))
                    network_ended = generation["request"] is None or generation["finished"]
                    if last and ended and network_ended and not streaming and time.monotonic() - changed > 8:
                        row.update(answer=last, status="complete")
                        break
                    page.wait_for_timeout(800)
                if row["status"] != "complete":
                    row["error_message"] = "等待完整回答超时；本次不计入有效样本，请查看浏览器"
                if row["status"] == "complete":
                    self.items[site_id].update(last_answer_at=now(), chat_ready=True)
                row["screenshot_base64"] = base64.b64encode(page.screenshot()).decode()
            except Exception as exc:
                # Keep only operation/error codes; never persist a Playwright
                # call log, which can contain the supplied text or page data.
                row["error_message"] = browser_error_message(stage, exc)
                if page and row["asked_at"]:
                    try:
                        row["screenshot_base64"] = base64.b64encode(page.screenshot()).decode()
                    except Exception:
                        pass
            row["page_url"] = safe_url(page.url) if page and not page.is_closed() else ""
            if page:
                for event, handler in listeners:
                    page.remove_listener(event, handler)
                if watch_key:
                    try:
                        page.evaluate("key=>{window[key]?.observer.disconnect();delete window[key]}", watch_key)
                    except Exception:
                        pass
            rows.append(row)
            if row["status"] != "complete":
                rows.extend({"question_id": q["id"], "status": "error", "error_message": "上一题未完成，后续问题尚未发送；处理浏览器提示后重试"}
                            for q in questions[len(rows):])
                break
        self.persist()
        return {"results": rows}

    def perform(self, payload):
        action = payload.get("action")
        if action == "prepare":
            try:
                for site_id in ([payload["site"]] if payload.get("site") else CATALOG):
                    self.prepare_one(site_id)
            finally:
                self.preparing = False
                self.persist()
            return self.status()
        if action == "collect":
            return self.collect(payload)
        if action == "open":
            return self.open_one(payload.get("site"))
        if action == "check":
            return self.check_one(payload.get("site"))
        if action == "inspect_chat":
            return self.inspect_chat(payload.get("site"))
        if action in ("finish", "shutdown"):
            if self.context:
                self.context.close(); self.context = None
            self.pages.clear(); self.base_pages.clear(); self.frames.clear()
            self.persist()
            if action == "shutdown" and self.engine:
                self.engine.stop(); self.engine = None
            return self.status()
        site_id = payload.get("site")
        page = self.page(site_id)
        if action == "snapshot":
            return self.snapshot(site_id)
        if action == "confirmed":
            self.items[site_id]["confirmed"] = True; self.persist()
            return self.snapshot(site_id)
        if action == "refresh":
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
            return self.snapshot(site_id)
        if not payload.get("frame_id") or payload["frame_id"] != self.frames.get(site_id):
            return {"error": "画面已更新，请先刷新画面"}
        self.frames.pop(site_id, None)
        before = list(self.context.pages)
        if action == "click":
            x, y = int(payload.get("x", -1)), int(payload.get("y", -1))
            w, h = page.evaluate("[innerWidth,innerHeight]")
            if not (0 <= x < w and 0 <= y < h):
                raise ValueError("点击位置超出画面")
            page.mouse.click(x, y)
        elif action == "type":
            text = payload.get("text", "")
            if not isinstance(text, str) or not 1 <= len(text) <= 300 or not self.focus(page)["editable"]:
                return {"error": "请先在画面中选中要填写的登录输入框"}
            page.keyboard.insert_text(text)
        elif action == "key" and payload.get("key") in KEYS:
            page.keyboard.press(KEYS[payload["key"]])
        elif action == "scroll":
            page.mouse.wheel(0, max(-700, min(700, int(payload.get("dy", 0)))))
        else:
            raise ValueError("不支持的操作")
        page.wait_for_timeout(600)
        new = [p for p in self.context.pages if p not in before]
        if new:
            self.pages[site_id] = new[-1]
        return self.snapshot(site_id)


def token():
    if not TOKEN.exists():
        try:
            fd = os.open(TOKEN, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(32))
        except FileExistsError:
            pass
    return TOKEN.read_text().strip()


def call(payload):
    try:
        info = json.loads(INSTANCE.read_text(encoding="utf-8"))
        port = int(info["port"])
        if not 1 <= port <= 65535:
            raise ValueError()
    except (OSError, ValueError, KeyError):
        raise OSError("Browser service is not running") from None
    req = Request(f"http://127.0.0.1:{port}/request", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "X-Login-Token": token()})
    with build_opener(ProxyHandler({})).open(req, timeout=710 if payload.get("action") == "collect" else 60) as response:
        return json.load(response)


def client_request(payload):
    try:
        return call(payload)
    except (OSError, URLError):
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        subprocess.Popen(child_command("--browser-service"), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        for _ in range(50):
            time.sleep(0.2)
            try:
                return call(payload)
            except (OSError, URLError):
                pass
        return {"error": "本机专用浏览器服务未能启动"}


def serve():
    lock = ProfileLock(ROOT / "browser-service.lock")
    if not lock.acquire():
        return
    secret = token()
    workspace = Workspace()
    http = None
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_POST(self):
            if self.path != "/request" or self.headers.get("X-Login-Token") != secret:
                self.send_error(403); return
            payload_action = ""
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 20000: raise ValueError()
                payload = json.loads(self.rfile.read(size))
                payload_action = payload.get("action", "")
                result = workspace.request(payload)
            except Exception:
                result = {"error": "本机浏览器操作未完成，请刷新状态确认页面"}
            data = json.dumps(result, ensure_ascii=False).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data)
            if payload_action == "shutdown" and not result.get("error"):
                threading.Thread(target=http.shutdown, daemon=True).start()
    try:
        try:
            http = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        except OSError:
            http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        INSTANCE.write_text(json.dumps({"port": http.server_port}), encoding="utf-8")
        http.serve_forever()
    finally:
        if http:
            http.server_close()
        INSTANCE.unlink(missing_ok=True)
        lock.release()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        serve()
    else:
        payload = json.load(sys.stdin)
        result = client_request(payload)
        print(json.dumps(result, ensure_ascii=False))
