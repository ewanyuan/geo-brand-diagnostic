"""Participant-owned login center, separate from diagnostic and collection routes."""

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, Response, g, jsonify, redirect, request, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

from guest_workspaces import WorkspacePool, WorkspaceUnavailable
from login_sites import SITES

ROOT = Path(__file__).resolve().parent
DEFAULT_STATE = Path.home() / ".local/share/geo-login-center/state"
COOKIE = "geo_guest_access"
ACTIONS = {"status", "prepare", "snapshot", "click", "type", "key", "scroll", "refresh", "confirmed", "finish"}
FIELDS = {"action", "site", "frame_id", "x", "y", "text", "key", "dy"}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def private_directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)
    return path


class AccessStore:
    def __init__(self, root):
        self.root = private_directory(root)
        self.database = self.root / "access.sqlite3"
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS participants (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    collection_until INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS invites (
                    hash TEXT PRIMARY KEY, participant TEXT NOT NULL REFERENCES participants(id),
                    expires INTEGER NOT NULL, used INTEGER);
                CREATE TABLE IF NOT EXISTS sessions (
                    hash TEXT PRIMARY KEY, participant TEXT NOT NULL REFERENCES participants(id),
                    expires INTEGER NOT NULL);
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(participants)")}
            if "collection_until" not in columns:
                conn.execute("ALTER TABLE participants ADD COLUMN collection_until INTEGER NOT NULL DEFAULT 0")
        if os.name != "nt":
            self.database.chmod(0o600)
        secret = self.root / "csrf.key"
        try:
            fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(secrets.token_bytes(32))
        self.secret = secret.read_bytes()
        if len(self.secret) != 32:
            raise RuntimeError("Invalid portal secret")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.database, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def invite(self, name, hours=48, participant=None):
        name = str(name).strip()
        if not 1 <= len(name) <= 80 or not 1 <= hours <= 720:
            raise ValueError("Name or expiry invalid")
        raw = secrets.token_urlsafe(32)
        with self.connect() as conn:
            if participant:
                row = conn.execute("SELECT id FROM participants WHERE id=? AND enabled=1", (participant,)).fetchone()
                if row is None:
                    raise ValueError("Participant does not exist or is revoked")
                conn.execute("UPDATE invites SET used=? WHERE participant=? AND used IS NULL", (int(time.time()), participant))
            else:
                participant = uuid.uuid4().hex
                conn.execute("INSERT INTO participants(id,name) VALUES (?,?)", (participant, name))
            conn.execute("INSERT INTO invites(hash,participant,expires) VALUES (?,?,?)",
                         (digest(raw), participant, int(time.time()) + hours * 3600))
        return participant, raw

    def start(self, name, seconds):
        participant, cookie = uuid.uuid4().hex, secrets.token_urlsafe(32)
        with self.connect() as conn:
            conn.execute("INSERT INTO participants(id,name) VALUES (?,?)", (participant, name))
            conn.execute("INSERT INTO sessions(hash,participant,expires) VALUES (?,?,?)",
                         (digest(cookie), participant, int(time.time()) + seconds))
        return participant, cookie

    def claim(self, raw, seconds):
        now = int(time.time())
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("""SELECT i.participant,p.name FROM invites i JOIN participants p
                ON i.participant=p.id WHERE i.hash=? AND i.used IS NULL AND i.expires>? AND p.enabled=1""",
                               (digest(raw), now)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE invites SET used=? WHERE hash=?", (now, digest(raw)))
            # Reissuing entry access invalidates older portal sessions, not platform cookies.
            conn.execute("DELETE FROM sessions WHERE participant=? OR expires<=?", (row["participant"], now))
            cookie = secrets.token_urlsafe(32)
            conn.execute("INSERT INTO sessions(hash,participant,expires) VALUES (?,?,?)",
                         (digest(cookie), row["participant"], now + seconds))
        return cookie

    def lookup(self, cookie):
        if not cookie or not re.fullmatch(r"[A-Za-z0-9_-]{43}", cookie):
            return None
        with self.connect() as conn:
            row = conn.execute("""SELECT p.id,p.name FROM sessions s JOIN participants p ON s.participant=p.id
                WHERE s.hash=? AND s.expires>? AND p.enabled=1""", (digest(cookie), int(time.time()))).fetchone()
        return dict(row) if row else None

    def csrf(self, cookie):
        return hmac.new(self.secret, cookie.encode(), hashlib.sha256).hexdigest()

    def logout(self, cookie):
        with self.connect() as conn:
            row = conn.execute("SELECT participant FROM sessions WHERE hash=?", (digest(cookie),)).fetchone()
            if row:
                conn.execute("UPDATE participants SET collection_until=0 WHERE id=?", (row["participant"],))
            conn.execute("DELETE FROM sessions WHERE hash=?", (digest(cookie),))

    def revoke(self, participant):
        with self.connect() as conn:
            conn.execute("UPDATE participants SET enabled=0 WHERE id=?", (participant,))
            conn.execute("DELETE FROM sessions WHERE participant=?", (participant,))

    def set_collection_permission(self, participant, cookie, enabled):
        now = int(time.time())
        with self.connect() as conn:
            session = conn.execute("SELECT expires FROM sessions WHERE hash=? AND participant=? AND expires>?",
                                   (digest(cookie), participant, now)).fetchone()
            until = min(now + 12 * 3600, session["expires"]) if enabled and session else 0
            changed = conn.execute("UPDATE participants SET collection_until=? WHERE id=? AND enabled=1",
                                   (until, participant)).rowcount
        return until if changed else 0

    def collection_accounts(self):
        with self.connect() as conn:
            rows = conn.execute("SELECT id,name,collection_until FROM participants WHERE enabled=1 ORDER BY rowid").fetchall()
        accounts = []
        for row in rows:
            platforms = {}
            try:
                saved = json.loads((Path(self.root) / "participants" / row["id"] / "login-readiness.json").read_text(encoding="utf-8"))
                platforms = {p["id"]: bool(p.get("confirmed")) for p in saved.get("platforms", [])
                             if p.get("id") in ("deepseek", "doubao", "kimi")}
            except (OSError, ValueError, TypeError, AttributeError):
                pass
            accounts.append({"id": row["id"], "name": row["name"], "collection_until": row["collection_until"],
                             "can_collect": row["collection_until"] > int(time.time()), "platforms": platforms})
        return accounts


class ClaimLimiter:
    def __init__(self):
        self.hits = {}
        self.lock = threading.Lock()

    def allowed(self, address):
        now = time.monotonic()
        with self.lock:
            self.hits = {ip: values for ip, values in self.hits.items() if values[-1] > now - 60}
            values = [v for v in self.hits.get(address, []) if v > now - 60]
            if len(values) >= 12 or address not in self.hits and len(self.hits) >= 4096:
                return False
            self.hits[address] = values + [now]
            return True


def create_app(test_config=None):
    app = Flask(__name__, static_folder=None)
    app.config.update(
        STATE_DIR=os.environ.get("GEO_GUEST_STATE_DIR", str(DEFAULT_STATE)),
        SECURE_COOKIE=os.environ.get("GEO_GUEST_ALLOW_LOCAL_HTTP") != "1",
        TRUST_PROXY=os.environ.get("GEO_GUEST_TRUST_PROXY") == "1",
        SESSION_SECONDS=12 * 3600, MAX_CONTENT_LENGTH=8192,
        MAX_WORKSPACES=int(os.environ.get("GEO_GUEST_MAX_WORKSPACES", "2")),
        BROWSER_CONFIG={"browser_channel": os.environ.get("GEO_GUEST_BROWSER_CHANNEL", "chromium"),
                        "engine": os.environ.get("GEO_GUEST_ENGINE", "chromium")},
    )
    if test_config:
        app.config.update(test_config)
    if not 1 <= app.config["MAX_WORKSPACES"] <= 20:
        raise ValueError("Invalid workspace limit")
    if app.config["TRUST_PROXY"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0)
    store = AccessStore(app.config["STATE_DIR"])
    pool = app.config.get("WORKSPACE_POOL") or WorkspacePool(
        private_directory(Path(app.config["STATE_DIR"]) / "participants"),
        maximum=app.config["MAX_WORKSPACES"], runtime_config=app.config["BROWSER_CONFIG"])
    app.extensions.update(guest_store=store, guest_pool=pool)
    limiter = ClaimLimiter()

    @app.before_request
    def guard():
        # Production is HTTPS only, including invite exchange and screenshots.
        if app.config["SECURE_COOKIE"] and not request.is_secure and request.path != "/healthz":
            return jsonify(error="请通过 HTTPS 地址访问登录中心"), 400
        origin = request.headers.get("Origin")
        if request.method == "POST" and origin and origin.rstrip("/") != request.host_url.rstrip("/"):
            return jsonify(error="请从你的专属登录入口操作"), 403
        protected = request.path in ("/login-center", "/api/login-center", "/api/access/logout",
                                     "/api/access/collection-consent", "/api/access/complete")
        if not protected:
            return None
        g.access_cookie = request.cookies.get(COOKIE, "")
        g.participant = store.lookup(g.access_cookie)
        if not g.participant:
            if request.path == "/login-center":
                return redirect("/")
            return jsonify(error="本次登录已到期，请回首页重新进入"), 401
        if request.method == "POST" and not hmac.compare_digest(
                request.headers.get("X-GEO-Login-Token", ""), store.csrf(g.access_cookie)):
            return jsonify(error="请从你的专属登录入口操作"), 403

    @app.after_request
    def headers(response):
        response.headers.update({
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        })
        return response

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify(error="请求内容过长"), 413

    @app.get("/healthz")
    def health():
        return jsonify(ok=True)

    @app.get("/")
    def entrance():
        if store.lookup(request.cookies.get(COOKIE, "")):
            return redirect("/login-center")
        return send_from_directory(ROOT / "static", "guest-access.html")

    @app.post("/api/access/start")
    def start():
        existing = store.lookup(request.cookies.get(COOKIE, ""))
        if existing:
            return jsonify(next="/login-center", participant_id=existing["id"])
        if not limiter.allowed(request.remote_addr or "unknown"):
            return jsonify(error="操作较频繁，请一分钟后再试"), 429
        payload = request.get_json(silent=True)
        name = payload.get("name") if isinstance(payload, dict) else None
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40:
            return jsonify(error="填一个称呼即可，最多 40 个字"), 400
        participant, cookie = store.start(name.strip(), app.config["SESSION_SECONDS"])
        response = jsonify(next="/login-center", participant_id=participant)
        response.set_cookie(COOKIE, cookie, max_age=app.config["SESSION_SECONDS"],
                            httponly=True, secure=app.config["SECURE_COOKIE"], samesite="Strict", path="/")
        return response

    @app.get("/static/<name>")
    def asset(name):
        if name not in {"login.css", "login.js", "guest-access.js"}:
            return "", 404
        return send_from_directory(ROOT / "static", name)

    @app.post("/api/access")
    def access():
        if not limiter.allowed(request.remote_addr or "unknown"):
            return jsonify(error="尝试次数较多，请稍等一分钟再打开邀请链接"), 429
        payload = request.get_json(silent=True)
        raw = payload.get("invite") if isinstance(payload, dict) else None
        if not isinstance(raw, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", raw):
            return jsonify(error="邀请链接无效或已使用，请向邀请人申请新的专属链接"), 401
        cookie = store.claim(raw, app.config["SESSION_SECONDS"])
        if cookie is None:
            return jsonify(error="邀请链接已过期或已使用，请向邀请人申请新的专属链接"), 401
        response = jsonify(next="/login-center")
        response.set_cookie(COOKIE, cookie, max_age=app.config["SESSION_SECONDS"],
                            httponly=True, secure=app.config["SECURE_COOKIE"], samesite="Strict", path="/")
        return response

    @app.get("/login-center")
    def portal():
        html = (ROOT / "static/login.html").read_text(encoding="utf-8")
        catalog = [{**s, "state": "not_prepared", "methods": []} for s in SITES]
        consent_until = store.collection_accounts()
        own = next((a for a in consent_until if a["id"] == g.participant["id"]), {})
        config = {"shared": True, "name": g.participant["name"],
                  "collectionUntil": own.get("collection_until", 0)}
        html = html.replace("__LOGIN_TOKEN__", store.csrf(g.access_cookie))
        html = html.replace("__LOGIN_SITES__", json.dumps(catalog, ensure_ascii=False))
        html = html.replace("__LOGIN_CONFIG__", json.dumps(config, ensure_ascii=False).replace("<", "\\u003c"))
        return Response(html, mimetype="text/html")

    @app.post("/api/login-center")
    def action():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("action"), str) or payload["action"] not in ACTIONS:
            return jsonify(error="不支持的操作"), 400
        if payload.get("site") is not None and (not isinstance(payload["site"], str) or payload["site"] not in {s["id"] for s in SITES}):
            return jsonify(error="未知平台"), 400
        # A submitted participant/profile/path is never accepted as ownership.
        payload = {k: v for k, v in payload.items() if k in FIELDS}
        try:
            result = pool.request(g.participant["id"], payload)
            return jsonify(result)
        except WorkspaceUnavailable as exc:
            return jsonify(error=str(exc)), 409
        except Exception:
            # Do not log payloads, screenshots or third-party exception contents.
            return jsonify(error="你的网页操作未完成，请刷新状态后重试"), 503

    @app.post("/api/access/logout")
    def logout():
        try:
            pool.request(g.participant["id"], {"action": "finish"})
        except Exception:
            pass  # Idle cleanup saves a workspace that is still preparing.
        store.logout(g.access_cookie)
        response = jsonify(ok=True)
        response.delete_cookie(COOKIE, secure=app.config["SECURE_COOKIE"], httponly=True, samesite="Strict", path="/")
        return response

    @app.post("/api/access/collection-consent")
    def collection_consent():
        payload = request.get_json(silent=True)
        enabled = payload.get("enabled") if isinstance(payload, dict) else None
        if not isinstance(enabled, bool):
            return jsonify(error="授权状态无效"), 400
        until = store.set_collection_permission(g.participant["id"], g.access_cookie, enabled)
        return jsonify(enabled=until > int(time.time()), until=until)

    @app.post("/api/access/complete")
    def complete():
        payload = request.get_json(silent=True)
        allow = payload.get("allow_collection") if isinstance(payload, dict) else None
        if not isinstance(allow, bool):
            return jsonify(error="请从页面上的完成按钮保存"), 400
        try:
            workspace = pool.request(g.participant["id"], {"action": "finish"})
            if workspace.get("error"):
                return jsonify(error=workspace["error"]), 409
        except Exception:
            return jsonify(error="页面还未保存完成，请稍后重试"), 409
        own = next((a for a in store.collection_accounts() if a["id"] == g.participant["id"]), {})
        eligible = any(own.get("platforms", {}).values())
        until = store.set_collection_permission(g.participant["id"], g.access_cookie, allow and eligible)
        return jsonify(workspace=workspace, enabled=until > int(time.time()), until=until)

    return app


def main():
    parser = argparse.ArgumentParser(description="Manage private login-center invitations")
    parser.add_argument("--state-dir", default=os.environ.get("GEO_GUEST_STATE_DIR", str(DEFAULT_STATE)))
    commands = parser.add_subparsers(dest="command", required=True)
    invite = commands.add_parser("invite")
    invite.add_argument("--name", required=True)
    invite.add_argument("--hours", type=int, default=48)
    invite.add_argument("--user", help="Reissue access to an existing participant profile")
    invite.add_argument("--base-url", default=os.environ.get("GEO_GUEST_BASE_URL", ""))
    listing = commands.add_parser("list")
    listing.add_argument("--json", action="store_true")
    revoke = commands.add_parser("revoke")
    revoke.add_argument("--user", required=True)
    commands.add_parser("serve-local")
    args = parser.parse_args()
    if args.command == "serve-local":
        app = create_app({"STATE_DIR": args.state_dir, "SECURE_COOKIE": False,
                          "BROWSER_CONFIG": {"browser_channel": "chrome"}})
        app.run(host="127.0.0.1", port=8766, debug=False)
        return
    store = AccessStore(args.state_dir)
    if args.command == "invite":
        url = urlsplit(args.base_url)
        local = url.hostname in ("127.0.0.1", "localhost")
        if (url.scheme != "https" and not (url.scheme == "http" and local)) or not url.netloc or url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
            parser.error("--base-url must be the HTTPS origin (or localhost for preview)")
        participant, raw = store.invite(args.name, args.hours, args.user)
        print("Participant: " + participant)
        print(args.base_url.rstrip("/") + "/#invite=" + raw)
    elif args.command == "list":
        accounts = store.collection_accounts()
        if args.json:
            print(json.dumps(accounts, ensure_ascii=True))
        else:
            for account in accounts:
                print(json.dumps(account, ensure_ascii=True))
    elif args.command == "revoke":
        store.revoke(args.user)
        print("Access revoked; saved platform profile retained.")


if __name__ == "__main__":
    main()
