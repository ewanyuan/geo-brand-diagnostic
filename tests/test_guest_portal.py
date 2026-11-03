import json
import re
import tempfile
import time
import unittest
from pathlib import Path

from guest_portal import COOKIE, create_app
from guest_workspaces import WorkspacePool
from login_sites import SITES


class FakeWorkspace:
    def __init__(self, root, runtime_config):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.preparing = False
        self.active = False
        self.requests = []
        self.confirmed = set()

    def status(self):
        return {"connected": True, "active": self.active, "preparing": False,
                "platforms": [{**s, "state": "login_ready", "confirmed": s["id"] in self.confirmed} for s in SITES]}

    def request(self, payload):
        self.requests.append(payload)
        if payload["action"] == "prepare":
            self.active = True
        elif payload["action"] == "confirmed":
            self.confirmed.add(payload["site"])
        elif payload["action"] in ("finish", "shutdown"):
            self.active = False
            (self.root / "login-readiness.json").write_text(json.dumps(self.status()), encoding="utf-8")
        elif payload["action"] == "snapshot":
            return {"image": self.root.name, "site": SITES[0]}
        return self.status()


class GuestPortalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="geo-guest-test-")
        self.pool = WorkspacePool(Path(self.temp.name) / "participants", maximum=2,
                                  factory=FakeWorkspace, sweep=False)
        self.app = create_app({"TESTING": True, "STATE_DIR": self.temp.name,
                               "SECURE_COOKIE": False, "WORKSPACE_POOL": self.pool})
        self.store = self.app.extensions["guest_store"]

    def tearDown(self):
        self.temp.cleanup()

    def login(self, name="Participant"):
        participant, raw = self.store.invite(name)
        client = self.app.test_client()
        response = client.post("/api/access", json={"invite": raw})
        self.assertEqual(response.status_code, 200)
        html = client.get("/login-center").get_data(as_text=True)
        csrf = re.search(r'name="login-token" content="([a-f0-9]+)"', html).group(1)
        return participant, raw, client, {"X-GEO-Login-Token": csrf}

    def call(self, client, headers, action, **extra):
        return client.post("/api/login-center", headers=headers, json={"action": action, **extra})

    def test_entry_required_and_diagnostic_not_exposed(self):
        client = self.app.test_client()
        self.assertEqual(self.call(client, {}, "status").status_code, 401)
        self.assertEqual(client.get("/login-center").status_code, 302)
        for path in ("/api/projects", "/static/app.js", "/static/../guest_portal.py", "/data/access.sqlite3"):
            self.assertEqual(client.get(path).status_code, 404)
        self.assertEqual(client.get("/healthz").json, {"ok": True})

    def self_start(self, client, name="Visitor"):
        response = client.post("/api/access/start", json={"name": name})
        self.assertEqual(response.status_code, 200)
        html = client.get("/login-center").get_data(as_text=True)
        csrf = re.search(r'name="login-token" content="([a-f0-9]+)"', html).group(1)
        return response.json["participant_id"], {"X-GEO-Login-Token": csrf}

    def test_start_needs_no_invite_and_same_name_is_isolated(self):
        a, b = self.app.test_client(), self.app.test_client()
        first, ha = self.self_start(a, "Same name")
        second, hb = self.self_start(b, "Same name")
        self.assertNotEqual(first, second)
        self.assertEqual(a.get("/").location, "/login-center")
        self.assertEqual(a.post("/api/access/start", json={"name": "Other", "participant_id": second}).json["participant_id"], first)
        self.call(a, ha, "prepare", site="doubao")
        self.assertFalse(self.call(b, hb, "status").json["active"])
        self.assertEqual(self.call(a, hb, "status").status_code, 403)

    def test_start_validation_and_foreign_origin(self):
        client = self.app.test_client()
        for name in ("", " " * 3, "x" * 41, [], None):
            self.assertEqual(client.post("/api/access/start", json={"name": name}).status_code, 400)
        self.assertEqual(client.post("/api/access/start", json={"name": "Visitor"},
                                    headers={"Origin": "https://other.example"}).status_code, 403)

    def test_complete_saves_and_only_authorizes_confirmed_supported_sites(self):
        client = self.app.test_client()
        uid, headers = self.self_start(client)
        self.call(client, headers, "prepare", site="doubao")
        self.assertEqual(client.post("/api/access/complete", json={"allow_collection": True}).status_code, 403)
        response = client.post("/api/access/complete", headers=headers, json={"allow_collection": True})
        self.assertFalse(response.json["enabled"])
        self.call(client, headers, "prepare", site="doubao")
        self.call(client, headers, "confirmed", site="doubao")
        response = client.post("/api/access/complete", headers=headers, json={"allow_collection": True})
        self.assertTrue(response.json["enabled"])
        self.assertFalse(response.json["workspace"]["active"])
        with self.store.connect() as conn:
            expiry = conn.execute("SELECT expires FROM sessions WHERE participant=?", (uid,)).fetchone()[0]
        self.assertLessEqual(response.json["until"], expiry)
        client.post("/api/access/logout", headers=headers)
        self.assertFalse(next(a for a in self.store.collection_accounts() if a["id"] == uid)["can_collect"])

    def test_deepseek_saved_profile_can_be_authorized_without_reopening(self):
        client = self.app.test_client()
        uid, headers = self.self_start(client)
        self.call(client, headers, "prepare", site="deepseek")
        self.call(client, headers, "confirmed", site="deepseek")
        self.assertFalse(client.post("/api/access/complete", headers=headers,
                                     json={"allow_collection": False}).json["enabled"])
        self.assertNotIn(uid, self.pool.slots)
        response = client.post("/api/access/complete", headers=headers, json={"allow_collection": True})
        self.assertTrue(response.json["enabled"])
        self.assertNotIn(uid, self.pool.slots)
        own = next(a for a in self.store.collection_accounts() if a["id"] == uid)
        self.assertTrue(own["platforms"]["deepseek"])

    def test_invitations_one_use_expiry_and_revocation(self):
        uid, raw, client, headers = self.login()
        self.assertEqual(self.app.test_client().post("/api/access", json={"invite": raw}).status_code, 401)
        _, expired = self.store.invite("Expired")
        with self.store.connect() as conn:
            conn.execute("UPDATE invites SET expires=0 WHERE used IS NULL")
        self.assertEqual(self.app.test_client().post("/api/access", json={"invite": expired}).status_code, 401)
        self.store.revoke(uid)
        self.assertEqual(self.call(client, headers, "status").status_code, 401)

    def test_two_people_cannot_select_or_read_each_others_profile(self):
        first, _, a, ha = self.login("Alice")
        second, _, b, hb = self.login("Bob")
        self.assertEqual(self.call(a, ha, "prepare", participant=second, root="../Bob").status_code, 200)
        self.assertTrue(self.call(a, ha, "status").json["active"])
        self.assertFalse(self.call(b, hb, "status").json["active"])
        self.assertEqual(self.call(b, hb, "snapshot", site="doubao", participant=first).status_code, 409)
        self.assertEqual(self.call(b, hb, "prepare").status_code, 200)
        self.assertEqual(self.call(a, ha, "snapshot", site="doubao", participant=second).json["image"], first)
        self.assertEqual(self.call(b, hb, "snapshot", site="doubao").json["image"], second)
        self.assertNotEqual(self.pool.slots[first].workspace.root, self.pool.slots[second].workspace.root)
        self.assertEqual(self.pool.slots[first].workspace.requests[0], {"action": "prepare"})
        self.assertEqual(self.call(a, hb, "finish").status_code, 403)
        self.assertTrue(self.call(b, hb, "status").json["active"])

    def test_cross_site_and_unsupported_actions_blocked(self):
        _, _, client, headers = self.login()
        self.assertEqual(self.call(client, {}, "prepare").status_code, 403)
        foreign = {**headers, "Origin": "https://other.example"}
        self.assertEqual(self.call(client, foreign, "prepare").status_code, 403)
        for action in ("collect", "shutdown", "open_url", []):
            self.assertEqual(self.call(client, headers, action).status_code, 400)
        self.assertEqual(self.call(client, headers, "prepare", site=[]).status_code, 400)
        response = client.get("/login-center")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

    def test_capacity_release_and_idle_save(self):
        self.pool.maximum = 1
        first, _, a, ha = self.login("One")
        second, _, b, hb = self.login("Two")
        self.call(a, ha, "prepare")
        self.assertEqual(self.call(b, hb, "prepare").status_code, 409)
        self.call(a, ha, "finish")
        self.assertNotIn(first, self.pool.slots)
        self.assertTrue((self.pool.directory(first) / "login-readiness.json").exists())
        self.assertEqual(self.call(b, hb, "prepare").status_code, 200)
        self.pool.slots[second].touched = time.monotonic() - 5000
        self.pool.expire_idle()
        self.assertNotIn(second, self.pool.slots)
        self.assertTrue((self.pool.directory(second) / "login-readiness.json").exists())
        self.assertFalse(self.call(b, hb, "status").json["active"])

    def test_https_cookie_logout_and_html_name_escaping(self):
        uid, _, client, headers = self.login('</script><img src=x onerror=alert(1)>')
        html = client.get("/login-center").get_data(as_text=True)
        self.assertNotIn('</script><img', html)
        self.assertIn('\\u003c/script>', html)
        self.assertIsNotNone(client.get_cookie(COOKIE))
        self.assertEqual(client.post("/api/access/logout", headers=headers).status_code, 200)
        self.assertIsNone(client.get_cookie(COOKIE))
        self.assertEqual(self.call(client, headers, "status").status_code, 401)
        # The profile stays available for a newly issued invitation to the same person.
        _, raw = self.store.invite("Same person", participant=uid)
        self.assertEqual(client.post("/api/access", json={"invite": raw}).status_code, 200)
        secure = create_app({"TESTING": True, "STATE_DIR": self.temp.name, "WORKSPACE_POOL": self.pool,
                             "SECURE_COOKIE": True}).test_client()
        self.assertEqual(secure.get("/login-center").status_code, 400)
        _, raw = self.store.invite("Secure")
        response = secure.post("/api/access", base_url="https://login.example", json={"invite": raw})
        cookie = response.headers["Set-Cookie"]
        self.assertIn("Secure;", cookie)
        self.assertIn("HttpOnly;", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_invite_hash_only_and_session_expiry(self):
        _, raw, client, headers = self.login()
        with self.store.connect() as conn:
            self.assertNotEqual(conn.execute("SELECT hash FROM invites").fetchone()[0], raw)
            conn.execute("UPDATE sessions SET expires=0")
        self.assertEqual(self.call(client, headers, "status").status_code, 401)
        with self.assertRaises(ValueError):
            self.pool.directory("../another-person")


if __name__ == "__main__":
    unittest.main()
