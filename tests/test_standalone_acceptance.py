"""Executable product promises from ACCEPTANCE.md, using isolated user data."""

import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import app_paths
from server import create_app


class StandaloneAcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="geo-acceptance-")
        self.home = Path(self.temp.name)
        self.evidence = self.home / "evidence"
        self.evidence.mkdir()
        self.app = create_app({"TESTING": True, "DATABASE": str(self.home / "geo.sqlite3"),
                               "EVIDENCE_DIR": str(self.evidence), "SEED_DEMO": False})
        self.client = self.app.test_client()
        project = self.client.post("/api/projects", json={"brand": "验收品牌", "category": "验收品类"})
        self.pid = project.json["id"]
        self.qid = self.client.get(f"/api/projects/{self.pid}").json["questions"][0]["id"]

    def tearDown(self):
        self.temp.cleanup()

    def observation(self, **extra):
        return {"question_id": self.qid, "platform": "DeepSeek", "observed_at": "2026-11-03",
                "answer": "验收品牌被提及，没有明确推荐顺序。", **extra}

    def test_brand_name_alone_creates_a_ready_project_without_guessing_its_category(self):
        created = self.client.post('/api/projects', json={'brand':'只有品牌名'})
        self.assertEqual(created.status_code, 201)
        project = self.client.get(f"/api/projects/{created.json['id']}").json
        self.assertEqual(project['category'], '')
        self.assertEqual(len(project['questions']), 4)
        self.assertTrue(all(q['kind']=='brand' and '只有品牌名' in q['text'] for q in project['questions']))
        updated = self.client.put(f"/api/projects/{project['id']}", json={'category':''})
        self.assertEqual(updated.status_code, 200)

    def test_A2_A8_frozen_data_does_not_live_in_installation_folder(self):
        with patch.dict(os.environ, {"GEO_USER_HOME": str(self.home)}, clear=True), patch.object(
                app_paths.sys, "frozen", True, create=True):
            self.assertEqual(app_paths.data_dir(), self.home / "data")
            self.assertNotEqual(app_paths.data_dir().parent, app_paths.APP_ROOT)
            self.assertEqual(app_paths.child_command("--browser-service"),
                             [app_paths.sys.executable, "--browser-service"])
        self.assertEqual(len(self.client.get("/api/projects").json), 1)
        self.assertFalse(self.client.get("/api/projects").json[0]["is_demo"])

    def test_A3_exit_during_collection_preserves_work_and_closing_rejects_new_work(self):
        from local_login_service import Workspace
        workspace = Workspace(root=self.home / "worker")
        started = threading.Event()
        release = threading.Event()
        original = workspace.perform
        def perform(payload):
            if payload["action"] == "collect":
                started.set()
                release.wait(5)
                return {"results": []}
            return original(payload)
        with patch.object(workspace, "perform", side_effect=perform):
            running = threading.Thread(target=workspace.request, args=({"action":"collect"},))
            running.start()
            try:
                self.assertTrue(started.wait(3))
                self.assertIn("error", workspace.request({"action":"shutdown"}))
            finally:
                release.set()
                running.join(5)
            self.assertNotIn("error", workspace.request({"action":"shutdown"}))
            workspace.thread.join(5)
            self.assertFalse(workspace.thread.is_alive())
            self.assertIn("error", workspace.request({"action":"collect"}))

    def test_A5_error_diagnostics_never_read_inputs_from_call_logs(self):
        from local_login_service import browser_error_message
        error = RuntimeError("Locator.fill: Timeout 10000ms exceeded.\nCall log:\n fill(net::ERR_PRIVATE_INPUT_SENTINEL)")
        message = browser_error_message("发送问题", error)
        self.assertNotIn("PRIVATE_INPUT_SENTINEL", message)
        self.assertNotIn("fill(", message)
        network = browser_error_message("打开网页", RuntimeError("Page.goto: net::ERR_HTTP2_PROTOCOL_ERROR at https://example.org/"))
        self.assertIn("net::ERR_HTTP2_PROTOCOL_ERROR", network)
        self.assertNotIn("example.org", network)

    def test_A6_unknown_ranking_is_not_zero_and_review_can_record_no_rank(self):
        created = self.client.post(f"/api/projects/{self.pid}/observations", json=self.observation())
        oid = created.json["id"]
        stats = self.client.get(f"/api/projects/{self.pid}/analytics").json
        self.assertIsNone(stats["metrics"]["top3_rate"])
        self.assertIsNone(stats["by_platform"][0]["first_rate"])
        self.assertEqual(stats["metrics"]["rank_sample"], 0)
        updated = self.client.put(f"/api/projects/{self.pid}/observations/{oid}", json={"rank_reviewed": True})
        self.assertTrue(updated.json["rank_reviewed"])
        stats = self.client.get(f"/api/projects/{self.pid}/analytics").json
        self.assertEqual(stats["metrics"]["top3_rate"], 0)
        self.assertEqual(stats["metrics"]["rank_sample"], 1)

    def test_A6_automatic_nonmention_is_not_human_review(self):
        self.client.post(f"/api/projects/{self.pid}/observations", json=self.observation(answer="只介绍其他机构。"))
        stats = self.client.get(f"/api/projects/{self.pid}/analytics").json["metrics"]
        self.assertEqual(stats["mentioned"], 0)
        self.assertEqual(stats["rank_sample"], 0)
        self.assertIsNone(stats["top3_rate"])

    def test_A6_unknown_competitor_ranks_do_not_establish_a_benchmark(self):
        self.client.put(f"/api/projects/{self.pid}",json={"competitors":["竞品甲","竞品乙","竞品丙","竞品丁"]})
        self.client.post(f"/api/projects/{self.pid}/observations",json=self.observation(
            answer="验收品牌、竞品甲、竞品乙、竞品丙、竞品丁。",brand_rank=1,
            competitor_ranks={"竞品甲":2,"竞品乙":3,"竞品丙":4}))
        stats=self.client.get(f"/api/projects/{self.pid}/analytics").json["metrics"]
        self.assertEqual(stats["top3_rate"],100)
        self.assertIsNone(stats["top3_benchmark"])

    def test_A7_evidence_export_contains_only_this_project_and_no_profile(self):
        image = self.evidence / "fixture-proof.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nTEST_ONLY_NOT_A_REAL_SCREENSHOT")
        (self.evidence / "other-project.png").write_bytes(b"EXCLUDE_OTHER_PROJECT")
        (self.home / "login-service.token").write_text("EXCLUDE_LOGIN_TOKEN")
        self.client.post(f"/api/projects/{self.pid}/observations", json=self.observation(
            evidence_image=image.name, rank_reviewed=True))
        result = self.client.get(f"/api/projects/{self.pid}/export.zip")
        self.assertEqual(result.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(result.data)) as archive:
            self.assertEqual(set(archive.namelist()), {"project.json", "observations.csv", "README.txt", "evidence/fixture-proof.png"})
            manifest = json.loads(archive.read("project.json"))
            self.assertEqual(manifest["project"]["brand"], "验收品牌")
            self.assertTrue(manifest["observations"][0]["rank_reviewed"])
            self.assertEqual(manifest["missing_evidence"], [])
            self.assertNotIn(b"EXCLUDE_LOGIN_TOKEN", b"".join(archive.read(n) for n in archive.namelist()))

    def test_A7_missing_evidence_is_explicit_and_csv_round_trip_retains_review(self):
        self.client.post(f"/api/projects/{self.pid}/observations", json=self.observation(
            evidence_image="missing.png", rank_reviewed=True))
        result = self.client.get(f"/api/projects/{self.pid}/export.zip")
        with zipfile.ZipFile(io.BytesIO(result.data)) as archive:
            self.assertEqual(json.loads(archive.read("project.json"))["missing_evidence"], ["missing.png"])
            csv = archive.read("observations.csv")
        imported = self.client.post(f"/api/projects/{self.pid}/import", data={"file": (io.BytesIO(csv), "roundtrip.csv")})
        self.assertEqual(imported.status_code, 200)
        self.assertTrue(all(r["rank_reviewed"] for r in self.client.get(f"/api/projects/{self.pid}/observations").json))


if __name__ == "__main__":
    unittest.main()
