import io
import os
import base64
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import create_app


class ProductFlowTest(unittest.TestCase):
    def setUp(self):
        data_dir = Path(__file__).resolve().parents[1] / "data"
        data_dir.mkdir(exist_ok=True)
        descriptor, self.database = tempfile.mkstemp(prefix="geo-test-", suffix=".db", dir=data_dir)
        os.close(descriptor)
        self.evidence = tempfile.TemporaryDirectory(prefix="geo-evidence-test-", dir=data_dir)
        self.app = create_app({"TESTING": True, "DATABASE": self.database,
                               "EVIDENCE_DIR": self.evidence.name,
                               "SEED_DEMO": False})
        self.client = self.app.test_client()
        response = self.client.post("/api/projects", json={
            "brand": "星河咨询", "category": "工程咨询", "region": "北京",
            "aliases": ["星河"], "competitors": ["远山", "青岳", "恒桥"],
        })
        self.assertEqual(response.status_code, 201)
        self.project_id = response.json["id"]
        self.questions = self.client.get(f"/api/projects/{self.project_id}/questions").json

    def tearDown(self):
        Path(self.database).unlink(missing_ok=True)
        self.evidence.cleanup()

    def test_login_center_requires_token_and_keeps_inputs_out_of_readiness_file(self):
        report = Path(self.evidence.name) / "readiness.json"
        self.app.config["LOGIN_READINESS_FILE"] = str(report)
        response = self.client.get("/login-center")
        html = response.get_data(as_text=True)
        token = re.search(r'name="login-token" content="([^"]+)"', html).group(1)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.client.post("/api/login-center", json={"action": "status"}).status_code, 403)
        with patch("login_center.remote_login", return_value={
            "connected": True, "active": True, "preparing": False,
            "platforms": [{"id": "doubao", "name": "豆包", "state": "login_ready",
                           "phone": "DO_NOT_SAVE_PHONE", "code": "DO_NOT_SAVE_CODE"}],
            "image": "DO_NOT_SAVE_SCREENSHOT", "cookie": "DO_NOT_SAVE_COOKIE",
        }) as remote:
            headers = {"X-GEO-Login-Token": token}
            self.assertEqual(self.client.post("/api/login-center", headers=headers,
                json={"action": "status"}).status_code, 200)
            self.assertEqual(self.client.post("/api/login-center", headers=headers,
                json={"action": "execute_script"}).status_code, 400)
            self.assertEqual(remote.call_count, 1)
        saved = report.read_text(encoding="utf-8")
        self.assertNotIn("DO_NOT_SAVE", saved)
        self.assertEqual(json.loads(saved)["platforms"][0]["id"], "doubao")

    def test_local_runtime_does_not_depend_on_ssh(self):
        import browser_collect
        import login_center
        with patch("login_center.is_local", return_value=True), patch(
            "login_center.call_local", return_value={"connected": True, "runtime": "local"}
        ) as local, patch("login_center.subprocess.run") as ssh:
            self.assertEqual(login_center.remote_login({"action": "status"})["runtime"], "local")
            local.assert_called_once_with({"action": "status"})
            ssh.assert_not_called()

        with patch("browser_collect.is_local", return_value=True), patch(
            "browser_collect.local_ready", return_value=True
        ), patch("browser_collect.call_local", return_value={"results": [{"status": "error"}]}) as local, patch(
            "browser_collect.subprocess.run"
        ) as ssh:
            rows = browser_collect.collect_remote("豆包", [{"id": 1, "text": "测试问题"}])
            self.assertEqual(rows[0]["status"], "error")
            self.assertEqual(local.call_args.args[0]["action"], "collect")
            ssh.assert_not_called()

    def test_browser_network_route_is_limited_to_explicit_model_domains(self):
        import browser_network
        with patch("browser_network.configuration", return_value={"direct_domains": ["doubao.com", "deepseek.com", "intranet.example"]}):
            self.assertTrue(browser_network.direct_host("www.doubao.com"))
            self.assertTrue(browser_network.direct_host("chat.deepseek.com"))
            self.assertFalse(browser_network.direct_host("www.qianwen.com"))
            self.assertFalse(browser_network.direct_host("intranet.example"))
            self.assertFalse(browser_network.direct_host("doubao.com.example.net"))

    def test_local_browser_entry_and_capture_never_read_server_accounts(self):
        with patch("server.is_local", return_value=True), patch("server.browser_ready", return_value=True), patch(
            "server.list_guest_accounts"
        ) as accounts, patch("server.guest_remote_ready") as ssh_ready:
            self.assertEqual(self.client.get('/api/browser-platforms').json['runtime'], 'local')
            self.assertEqual(self.client.get('/api/browser-accounts').json['accounts'], [])
            with patch("server.collect_remote", return_value=[]) as collect:
                result = self.client.post(f'/api/projects/{self.project_id}/run-browser', json={
                    'platform':'DeepSeek','question_ids':[self.questions[0]['id']],
                    'participant_id':'a'*32})
                self.assertEqual(result.status_code, 200)
                self.assertIsNone(collect.call_args.args[2])
            accounts.assert_not_called()
            ssh_ready.assert_not_called()
        with patch("login_center.is_local", return_value=True):
            page = self.client.get('/login-center').get_data(as_text=True)
            self.assertIn('/static/local-login.js', page)
            self.assertNotIn('id="type-form"', page)

    def test_browser_capture_keeps_evidence_and_failure_out_of_metrics(self):
        qid = self.questions[0]["id"]
        other = self.questions[1]["id"]
        png = base64.b64encode(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII="
            )
        ).decode()
        remote_rows = [
            {"question_id": qid, "status": "complete",
             "asked_at": "2026-10-28T15:20:00+08:00",
             "answer": "星河咨询出现在回答中。", "page_url": "https://www.doubao.com/chat/",
             "screenshot_base64": png},
            {"question_id": other, "status": "error",
             "error_message": "页面要求登录", "page_url": "https://www.doubao.com/chat/",
             "screenshot_base64": png},
        ]
        with patch("server.browser_ready", return_value=True), patch(
            "server.collect_remote", return_value=remote_rows
        ):
            response = self.client.post(f"/api/projects/{self.project_id}/run-browser", json={
                "platform": "豆包", "question_ids": [qid, other],
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["status"] for row in response.json["results"]], ["complete", "error"])
        rows = self.client.get(f"/api/projects/{self.project_id}/observations").json
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["asked_at"], "2026-10-28T15:20:00+08:00")
        self.assertEqual(rows[0]["source_kind"], "browser")
        self.assertEqual(rows[1]["error_message"], "页面要求登录")
        image_response = self.client.get(f"/api/evidence/{rows[0]['evidence_image']}")
        self.assertEqual(image_response.status_code, 200)
        image_response.close()
        self.assertEqual(self.client.get(f"/api/projects/{self.project_id}/analytics").json["metrics"]["answers"], 1)

    def test_full_diagnostic_and_report(self):
        qid = self.questions[0]["id"]
        base = {"question_id": qid, "platform": "豆包", "observed_at": "2026-10-21"}
        rows = [
            {**base, "answer": "星河咨询居第一，远山居第二。", "brand_rank": 1,
             "sentiment": "positive", "competitor_ranks": {"远山": 2},
             "evidence_url": "https://example.org/chat/first",
             "citations": [{"title": "来源甲", "source": "媒体甲", "url": "https://example.org/a"}]},
            {**base, "platform": "千问", "answer": "星河咨询也被提及。", "rank_reviewed": True, "sentiment": "negative",
             "negative_terms": ["信息不足"]},
            {**base, "platform": "Kimi", "answer": "青岳和恒桥被推荐。", "rank_reviewed": True, "competitor_ranks": {"青岳": 1, "恒桥": 3}},
            {**base, "platform": "元宝", "status": "offline", "answer": ""},
        ]
        for row in rows:
            self.assertEqual(self.client.post(f"/api/projects/{self.project_id}/observations", json=row).status_code, 201)
        analytics = self.client.get(f"/api/projects/{self.project_id}/analytics").json
        m = analytics["metrics"]
        self.assertEqual((m["answers"], m["offline"], m["mentioned"]), (3, 1, 2))
        self.assertEqual(m["first_rate"], 33.33)
        self.assertEqual(m["top3_rate"], 33.33)
        self.assertEqual(m["mention_rate"], 66.67)
        self.assertEqual(m["positive_rate"], 50.0)
        self.assertEqual(m["negative_rate"], 50.0)
        self.assertEqual(analytics["sources"][0]["source"], "媒体甲")
        evidence = [row["evidence_url"] for row in
                    self.client.get(f"/api/projects/{self.project_id}/observations").json]
        self.assertIn("https://example.org/chat/first", evidence)
        self.assertEqual(next(row for row in analytics["competitors"] if row["name"] == "青岳")["first_rate"], 33.33)
        report = self.client.get(f"/report/{self.project_id}")
        self.assertEqual(report.status_code, 200)
        report.close()
        filtered = self.client.get(f"/api/projects/{self.project_id}/analytics?platform=豆包").json
        self.assertEqual(filtered["metrics"]["answers"], 1)
        self.assertEqual(filtered["metrics"]["top3_rate"], 100.0)

    def test_csv_import_is_atomic_and_exportable(self):
        path = f"/api/projects/{self.project_id}/import"
        csv_data = ("question,question_type,platform,observed_at,status,answer,evidence_url,brand_rank\n"
                    "新的问题,keyword,豆包,2026-10-20,complete,星河咨询被提及,https://example.org/chat/csv,2\n"
                    "另一个问题,keyword,千问,not-a-date,complete,回答,,1\n")
        result = self.client.post(path, data={"file": (io.BytesIO(csv_data.encode()), "input.csv")},
                                  content_type="multipart/form-data")
        self.assertEqual(result.status_code, 400)
        self.assertEqual(self.client.get(f"/api/projects/{self.project_id}/observations").json, [])
        self.assertFalse(any(q["text"] == "新的问题" for q in self.client.get(f"/api/projects/{self.project_id}/questions").json))

        valid = csv_data.splitlines()[0] + "\n" + csv_data.splitlines()[1] + "\n"
        result = self.client.post(path, data={"file": (io.BytesIO(valid.encode()), "input.csv")},
                                  content_type="multipart/form-data")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["imported"], 1)
        export = self.client.get(f"/api/projects/{self.project_id}/export.csv")
        self.assertEqual(export.status_code, 200)
        self.assertIn("新的问题".encode(), export.data)
        self.assertIn(b"https://example.org/chat/csv", export.data)

    def test_demo_cannot_mix_with_real_monitoring(self):
        descriptor, demo_database = tempfile.mkstemp(prefix="geo-demo-test-", suffix=".db", dir=Path(self.database).parent)
        os.close(descriptor)
        try:
            client = create_app({"TESTING": True, "DATABASE": demo_database,
                                 "SEED_DEMO": True}).test_client()
            demo = next(p for p in client.get("/api/projects").json if p["is_demo"])
            qid = client.get(f"/api/projects/{demo['id']}/questions").json[0]["id"]
            attempt = client.post(f"/api/projects/{demo['id']}/observations", json={
                "question_id": qid, "platform": "豆包", "observed_at": "2026-10-21",
                "answer": "真实回答不得混进演示样本",
            })
            self.assertEqual(attempt.status_code, 409)
            self.assertEqual(client.put(f"/api/projects/{demo['id']}", json={"brand": "新建测试品牌"}).status_code, 409)
            created = client.post("/api/projects", json={"brand": "新建测试品牌", "category": "测试品牌品类"})
            self.assertEqual(created.status_code, 201)
            fresh = client.get(f"/api/projects/{created.json['id']}/analytics").json
            self.assertEqual(fresh["metrics"]["answers"], 0)
        finally:
            Path(demo_database).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
