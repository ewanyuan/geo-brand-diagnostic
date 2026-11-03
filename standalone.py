"""Double-click launcher; also dispatches the bundled browser helper."""

import argparse
import json
import os
import secrets
import sys
import threading
import time
import webbrowser
import shutil
import subprocess
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

from app_paths import VERSION, data_dir, user_home
from profile_lock import ProfileLock


def open_product_url(url):
    if sys.platform.startswith("linux") and getattr(sys, "frozen", False):
        from browser_discovery import external_environment
        opener = shutil.which("xdg-open") or "/usr/bin/xdg-open"
        subprocess.Popen([opener, url], env=external_environment(), start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        webbrowser.open(url)


def local_request(info, path, method="GET"):
    port = int(info["port"])
    if not 1 <= port <= 65535:
        raise ValueError("Invalid local port")
    req = Request(f"http://127.0.0.1:{port}{path}", method=method,
                  headers={"X-GEO-App-Token": info["token"]})
    with build_opener(ProxyHandler({})).open(req, timeout=3) as response:
        return json.load(response)


def existing_instance(path):
    for _ in range(30):
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
            if local_request(info, "/api/desktop/status").get("product") == "geo-brand-diagnostic":
                return info
        except (OSError, ValueError, KeyError):
            time.sleep(0.1)
    raise RuntimeError("GEO 正在启动或退出，请稍后再打开")


def wait_browser_exit():
    instance = user_home() / "browser-service.json"
    probe = ProfileLock(user_home() / "browser-service.lock")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if probe.acquire():
            try:
                instance.unlink(missing_ok=True)
            finally:
                probe.release()
            return True
        time.sleep(0.1)
    return False


def launch(open_page=True):
    from flask import jsonify, request
    from werkzeug.serving import make_server
    from server import create_app

    home = user_home()
    home.mkdir(parents=True, exist_ok=True)
    instance = home / "app-instance.json"
    lock = ProfileLock(home / "app.lock")
    if not lock.acquire():
        info = existing_instance(instance)
        if open_page:
            open_product_url(f"http://127.0.0.1:{info['port']}/")
        return
    http = None
    secret = secrets.token_urlsafe(32)
    closing = threading.Event()
    try:
        app = create_app({"DESKTOP_TOKEN": secret, "SEED_DEMO": False})
        @app.before_request
        def block_new_work_while_closing():
            if closing.is_set() and request.path.startswith("/api/") and request.path != "/api/desktop/status":
                return jsonify(error="GEO 正在退出，请稍后重新打开"), 503
        @app.get("/api/desktop/status")
        def status():
            if not secrets.compare_digest(request.headers.get("X-GEO-App-Token", ""), secret):
                return jsonify(error="请从 GEO 窗口操作"), 403
            return jsonify(product="geo-brand-diagnostic", version=VERSION, packaged=bool(getattr(sys,"frozen",False)))

        @app.post("/api/desktop/quit")
        def quit_app():
            if not secrets.compare_digest(request.headers.get("X-GEO-App-Token", ""), secret):
                return jsonify(error="请从 GEO 窗口操作"), 403
            from local_login_service import call
            closing.set()
            try:
                state = call({"action": "shutdown"})
                if state.get("error"):
                    closing.clear()
                    return jsonify(error=state["error"]), 409
            except (OSError, ValueError):
                pass
            if not wait_browser_exit():
                closing.clear()
                return jsonify(error="浏览器后台尚未退出，请稍后重试"), 503
            threading.Thread(target=http.shutdown, daemon=True).start()
            return jsonify(ok=True)

        preferred = int(os.environ.get("GEO_PORT", "8765"))
        try:
            http = make_server("127.0.0.1", preferred, app, threaded=True)
        except (OSError, SystemExit):
            http = make_server("127.0.0.1", 0, app, threaded=True)
        info = {"port": http.server_port, "token": secret, "version": VERSION}
        instance.write_text(json.dumps(info), encoding="utf-8")
        if open_page:
            open_product_url(f"http://127.0.0.1:{info['port']}/")
        http.serve_forever()
    finally:
        if http:
            http.server_close()
        from local_login_service import call
        try:
            call({"action": "shutdown"})
        except (OSError, ValueError):
            pass
        wait_browser_exit()
        instance.unlink(missing_ok=True)
        lock.release()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser-service", action="store_true")
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--quit", action="store_true")
    parser.add_argument("--check-environment", metavar="JSON_FILE")
    args = parser.parse_args()
    if args.browser_service:
        from local_login_service import serve
        serve()
    elif args.check_environment:
        from environment_check import check
        result = check()
        Path(args.check_environment).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0 if result["browser_input_verified"] else 1
    elif args.quit:
        info = existing_instance(user_home() / "app-instance.json")
        local_request(info, "/api/desktop/quit", "POST")
    else:
        launch(not args.no_open)
    return 0


def configure_windowed_logs():
    if sys.stdout is None or sys.stderr is None:
        logs = data_dir() / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        stream = open(logs / "application.log", "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = stream
        if sys.stderr is None:
            sys.stderr = stream


if __name__ == "__main__":
    try:
        configure_windowed_logs()
        raise SystemExit(main())
    except Exception as exc:
        # Windowed executables have no stdout/stderr. Preserve a readable error.
        logs = data_dir() / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "startup-error.txt").write_text(str(exc), encoding="utf-8")
        if os.name == "nt" and not any(a in sys.argv for a in ("--no-open", "--quit", "--browser-service", "--check-environment")):
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, f"GEO 未能启动：{exc}\n详情：{logs / 'startup-error.txt'}", "GEO 诊断台", 0x10)
        raise SystemExit(1)
