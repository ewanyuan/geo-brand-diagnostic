"""Local, CSRF-protected gateway to the authorized remote browser."""

import json
import os
import secrets
import subprocess
from pathlib import Path

from flask import Blueprint, Response, current_app, jsonify, request

from browser_collect import browser_ready
from login_sites import SITES
from browser_runtime import call_local, is_local
from app_paths import data_dir

ALLOWED = {"status", "prepare", "open", "check", "snapshot", "click", "type", "key", "scroll", "refresh", "confirmed", "finish"}


def remote_login(payload):
    if is_local():
        return call_local(payload)
    if not browser_ready():
        raise RuntimeError("采集机尚未配置")
    command = ["ssh", "-T", "-i", os.environ["GEO_BROWSER_SSH_KEY"],
               "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
               "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=12",
               os.environ["GEO_BROWSER_SSH_TARGET"],
               "~/.local/share/geo-browser/venv/bin/python",
               "~/.local/share/geo-browser/login_service.py", "client"]
    try:
        result = subprocess.run(command, input=json.dumps(payload), text=True,
                                encoding="utf-8", capture_output=True, timeout=65)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("采集机暂时连接不上，稍后可点刷新状态；已打开的登录页会保留") from None
    if result.returncode:
        raise RuntimeError("采集机暂时连接不上，稍后可点刷新状态；已打开的登录页会保留")
    try:
        response = json.loads(result.stdout.strip())
        if not isinstance(response, dict):
            raise ValueError()
    except (ValueError, TypeError):
        raise RuntimeError("采集机没有返回可识别的状态") from None
    if response.get("error"):
        raise RuntimeError(response["error"])
    return response


def login_blueprint():
    bp = Blueprint("login_center", __name__)
    csrf = secrets.token_urlsafe(32)

    @bp.get("/login-center")
    def login_page():
        template = "local-login.html" if is_local() else "login.html"
        html = (Path(__file__).parent / "static" / template).read_text(encoding="utf-8")
        html = html.replace("__LOGIN_TOKEN__", csrf).replace("__LOGIN_SITES__", json.dumps(
            [{**s, "state": "not_prepared", "methods": []} for s in SITES], ensure_ascii=False))
        html = html.replace("__LOGIN_CONFIG__", '{"shared":false}')
        response = Response(html, mimetype="text/html")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @bp.post("/api/login-center")
    def login_action():
        if not secrets.compare_digest(request.headers.get("X-GEO-Login-Token", ""), csrf):
            return jsonify(error="请从平台登录中心操作"), 403
        payload = request.get_json(silent=True) or {}
        action = payload.get("action")
        if action not in ALLOWED:
            return jsonify(error="不支持的操作"), 400
        try:
            response = remote_login(payload)
            if action == "status":
                target = Path(current_app.config.get("LOGIN_READINESS_FILE") or
                              data_dir() / "login-readiness.json")
                target.parent.mkdir(exist_ok=True)
                allowed = {"id", "name", "url", "collection", "state", "message", "methods",
                           "checked_at", "page_url", "confirmed"}
                report = {k: response.get(k) for k in ("connected", "active", "preparing")}
                report["platforms"] = [{k: v for k, v in s.items() if k in allowed}
                                       for s in response.get("platforms", [])]
                target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            return jsonify(response)
        except RuntimeError as exc:
            if action == "status":
                return jsonify(connected=False, active=False, preparing=False, error=str(exc),
                               platforms=[{**s, "state": "unknown", "message": "等待连接专用浏览器", "methods": []} for s in SITES])
            return jsonify(error=str(exc)), 503
    return bp
