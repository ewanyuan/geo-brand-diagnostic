"""Exercise the actual exe with no Python/Node/AI commands available to it.

The controller is a developer test harness. Test records are visibly synthetic,
live in a new user home, and never enter the owner's project or browser profile.
"""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import tarfile
import time
import zipfile
import sys
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

from playwright.sync_api import sync_playwright
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_discovery import browser_candidates


def extract_package(package, target):
    if package.suffix == ".zip":
        with zipfile.ZipFile(package) as archive:
            assert not any(Path(n).is_absolute() or ".." in Path(n).parts for n in archive.namelist())
            archive.extractall(target)
    else:
        with tarfile.open(package, "r:gz") as archive:
            # Validate links as well as members before extracting native .app links.
            for member in archive.getmembers():
                destination = target / member.name
                assert destination.resolve().is_relative_to(target.resolve())
                assert not member.isdev() and not member.isfifo()
                if member.issym() or member.islnk():
                    linked = (destination.parent if member.issym() else target) / member.linkname
                    assert linked.resolve().is_relative_to(target.resolve())
            if not hasattr(tarfile, "data_filter"):
                raise RuntimeError("Native tar package verification requires a Python with tarfile.data_filter")
            archive.extractall(target, filter="data")


def package_layout(install):
    for app, executable in (("GEO-Windows", "GEO诊断台.exe"), ("GEO-Desktop", "geo-diagnostic"),
                            ("GEO诊断台.app", "Contents/MacOS/geo-diagnostic")):
        root = install / app
        if (root / executable).is_file():
            manifest = root / "package-manifest.json" if app != "GEO诊断台.app" else install / "package-manifest.json"
            return root, root / executable, json.loads(manifest.read_text(encoding="utf-8"))
    raise RuntimeError("No native executable in package")


def launch_controller(engine):
    for _, options in browser_candidates():
        try:
            return engine.chromium.launch(**options, headless=True)
        except Exception:
            pass
    raise RuntimeError("No browser for the verification controller")


def request(base, path, payload=None, headers=None, raw=False):
    headers = dict(headers or {})
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = Request(base + path, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
    with build_opener(ProxyHandler({})).open(req, timeout=30) as response:
        data = response.read()
        return data if raw else json.loads(data)


def wait_instance(home, process):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Package exited early ({process.returncode})")
        try:
            info = json.loads((home / "app-instance.json").read_text(encoding="utf-8"))
            base = f"http://127.0.0.1:{info['port']}"
            status = request(base, "/api/desktop/status", headers={"X-GEO-App-Token": info["token"]})
            if status.get("product") == "geo-brand-diagnostic":
                return base, info
        except (OSError, ValueError, KeyError):
            time.sleep(0.2)
    raise RuntimeError("Package startup timed out")


def verify(package, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="geo-package-acceptance-"))
    assert temporary.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
    home = temporary / "用户 数据"
    install = temporary / "程序 中文 空格"
    install.mkdir()
    report = {"package": package.name, "package_sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
              "same_machine_isolated_test": True, "other_clean_windows_pc_verified": False,
              "real_platform_login_in_package_verified": False, "checks": {},
              "fixture_records_are_not_real_platform_answers": True}
    process = None
    base = None
    info = None
    occupied = socket.socket()
    occupied.bind(("127.0.0.1", 0))
    occupied.listen()
    try:
        extract_package(package, install)
        app, exe, manifest = package_layout(install)
        for f in manifest['files']:
            if "symlink" in f:
                assert (app / f['path']).is_symlink() and os.readlink(app / f['path']) == f['symlink']
            else:
                assert hashlib.sha256((app / f['path']).read_bytes()).hexdigest() == f['sha256']
        report.update(native_platform=manifest.get("platform", "windows"), architecture=manifest.get("architecture", "x86_64"),
                      other_clean_platform_pc_verified=False, real_platform_accounts_on_this_os_verified=False)
        assert not list(app.rglob("*.sqlite3"))
        assert not list(app.rglob("browser-runtime.json"))
        report["checks"]["A2_clean_package_and_manifest"] = True
        permitted = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
                     "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMSPEC", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE"}
        permitted.update({"HOME", "LANG", "LC_ALL", "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"})
        env = {k:v for k,v in os.environ.items() if k.upper() in permitted}
        empty_path = temporary / "empty-path"; empty_path.mkdir()
        env.update(PATH=str(Path(os.environ["SYSTEMROOT"]) / "System32") if os.name == "nt" else str(empty_path), GEO_USER_HOME=str(home),
                   GEO_PORT=str(occupied.getsockname()[1]), PYINSTALLER_RESET_ENVIRONMENT="1")
        assert all(shutil.which(name, path=env["PATH"]) is None for name in ("python", "python3", "node", "codex", "claude"))
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        browser_check = temporary / "browser-check.json"
        checked = subprocess.run([str(exe), "--check-environment", str(browser_check)], cwd=install, env=env,
                                 creationflags=flags, timeout=90)
        assert checked.returncode == 0, "Bundled browser check failed"
        browser_result = json.loads(browser_check.read_text(encoding="utf-8"))
        assert browser_result["browser_input_verified"]
        report["checks"]["A4_bundled_browser_driver_real_input"] = browser_result
        process = subprocess.Popen([str(exe), "--no-open"], cwd=install, env=env, creationflags=flags)
        base, info = wait_instance(home, process)
        assert info["port"] != occupied.getsockname()[1]
        assert request(base, "/api/projects") == []
        report["checks"]["A1_exe_with_no_python_node_or_ai_commands"] = True
        report["checks"]["A3_port_collision"] = True
        duplicate = subprocess.run([str(exe), "--no-open"], cwd=install, env=env, creationflags=flags, timeout=15)
        assert duplicate.returncode == 0
        assert json.loads((home / "app-instance.json").read_text())["port"] == info["port"]
        report["checks"]["A3_duplicate_launch_reuses_instance"] = True
        with sync_playwright() as engine:
            browser = launch_controller(engine)
            try:
                page = browser.new_page(viewport={"width":1366,"height":900})
                page.goto(base, wait_until="networkidle")
                page.get_by_role("button", name="创建项目", exact=True).click()
                form = page.locator("#project-create-form")
                form.locator('[name="brand"]').fill("验收品牌（非真实诊断）")
                form.locator('[type="submit"]').click()
                page.locator('#browser-run-form').wait_for()
                assert page.locator('#browser-run-form [name=question_ids]:checked').count() == 1
                report["checks"]["brand_only_project_and_default_question"] = True
                page.locator('[data-nav="input"]').first.wait_for()
                page.locator('[data-nav="input"]').first.click()
                form = page.locator("#observation-form")
                qid = form.locator('[name="question_id"] option').nth(1).get_attribute("value")
                form.locator('[name="question_id"]').select_option(qid)
                form.locator('[name="platform"]').select_option("DeepSeek")
                form.locator('[name="answer"]').fill("验收品牌（非真实诊断）用于程序验收。手工测试记录，不是官网回答，无明确推荐顺序。")
                form.locator('[type="submit"]').click()
                page.get_by_text("监测记录已保存，指标已更新", exact=True).wait_for()
                project = request(base, "/api/projects")[0]
                pid = project["id"]
                assert request(base, f"/api/projects/{pid}/analytics")["metrics"]["top3_rate"] is None
                page.locator('[data-action="edit-observation"]').first.click()
                form = page.locator("#observation-form")
                form.locator('[name="rank_reviewed"]').check()
                form.locator('[name="sentiment"]').select_option("neutral")
                form.locator('[name="competitor_ranks"]').fill("验收竞品 | 无明确名次")
                form.locator('[type="submit"]').click()
                page.get_by_text("监测记录已保存，指标已更新", exact=True).wait_for()
                # The toast may already exist while the request is in flight.
                page.wait_for_function("!document.querySelector('#observation-form [name=rank_reviewed]').checked")
                stats = request(base, f"/api/projects/{pid}/analytics")["metrics"]
                assert stats["rank_sample"] == 1 and stats["top3_rate"] == 0, stats
                report["checks"]["A6_ui_review_unknown_then_explicit_unranked"] = True
                image_dir = home / "data/evidence"
                image_dir.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(image_dir / "acceptance.png"))
                row = request(base, f"/api/projects/{pid}/observations")[0]
                req = Request(base + f"/api/projects/{pid}/observations/{row['id']}",
                              data=json.dumps({"evidence_image":"acceptance.png"}).encode(), method="PUT",
                              headers={"Content-Type":"application/json"})
                with build_opener(ProxyHandler({})).open(req, timeout=10):
                    pass
                page.goto(base + f"/report/{pid}", wait_until="networkidle")
                page.get_by_role("heading", name="品牌总览", exact=True).wait_for()
                assert "目标品牌名次已核对 1 / 1" in page.inner_text("body")
                pdf_path = output.with_name(output.stem + "-report-fixture.pdf")
                page.pdf(path=str(pdf_path), format="A4", print_background=True)
                assert pdf_path.read_bytes().startswith(b"%PDF")
                page.screenshot(path=str(output.with_name(output.stem + "-report-fixture.png")))
                archive_bytes = request(base, f"/api/projects/{pid}/export.zip", raw=True)
                with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                    assert "evidence/acceptance.png" in archive.namelist()
                    assert len(json.loads(archive.read("project.json"))["observations"]) == 1
                    assert not any("profile" in n or "token" in n for n in archive.namelist())
                report["checks"]["A7_ui_report_real_pdf_and_evidence_zip"] = True
                html = request(base, "/login-center", raw=True).decode()
                login_token = re.search(r'name="login-token" content="([^"]+)"',html).group(1)
                status = request(base, "/api/login-center", {"action":"status"}, {"X-GEO-Login-Token":login_token})
                assert status["connected"] and status["runtime"] == "local"
                platforms = request(base, "/api/browser-platforms")["platforms"]
                assert platforms == ["DeepSeek","豆包","Kimi"], platforms
                assert not any(s.get("confirmed") for s in status["platforms"])
                report["checks"]["A4_frozen_child_service_starts_without_python"] = True
            finally:
                browser.close()
        marker = home / "browser-profile/acceptance-retain.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("TEST PROFILE MARKER - NOT A LOGIN")
        request(base, "/api/desktop/quit", {}, {"X-GEO-App-Token":info["token"]})
        process.wait(timeout=15)
        assert process.returncode == 0
        deadline = time.monotonic() + 5
        while (home / "browser-service.json").exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not (home / "browser-service.json").exists()
        assert not (home / "app-instance.json").exists()
        report["checks"]["A3_quit_releases_app_and_browser_service"] = True
        replacement = temporary / "替换后的程序文件夹"
        replacement.mkdir()
        extract_package(package, replacement)
        _, replacement_exe, _ = package_layout(replacement)
        process = subprocess.Popen([str(replacement_exe), "--no-open"], cwd=replacement, env=env, creationflags=flags)
        base, info = wait_instance(home, process)
        assert request(base, "/api/projects")[0]["brand"] == "验收品牌（非真实诊断）"
        assert marker.exists()
        report["checks"]["A8_reinstall_in_another_folder_retains_project_and_profile_directory"] = True
        report["all_automated_package_checks_passed"] = True
    finally:
        if process and process.poll() is None:
            try:
                if base and info:
                    request(base, "/api/desktop/quit", {}, {"X-GEO-App-Token":info["token"]})
                process.wait(timeout=15)
            except Exception:
                process.terminate()
                process.wait(timeout=10)
        occupied.close()
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        resolved = temporary.resolve()
        if resolved.is_relative_to(Path(tempfile.gettempdir()).resolve()) and resolved.name.startswith("geo-package-acceptance-"):
            for attempt in range(30):
                try:
                    shutil.rmtree(resolved)
                    break
                except PermissionError:
                    if attempt == 29:
                        raise
                    time.sleep(0.2)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("package", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.package.resolve(), args.output.resolve())
    print(json.dumps(result, ensure_ascii=True))
