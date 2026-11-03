"""Cross-platform data placement and package invariants; not a Mac runtime claim."""

import os
import sys
import tempfile
import tarfile
import io
import unittest
from pathlib import Path
from unittest.mock import patch

import app_paths
import browser_discovery

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))
from build_desktop import build, inventory, native_target
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from verify_windows_package import extract_package


class CrossPlatformTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="geo-platform-")
        self.home = Path(self.temporary.name)
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.path_home = patch.object(Path, "home", return_value=self.home)
        self.path_home.start()

    def tearDown(self):
        self.path_home.stop()
        self.env.stop()
        self.temporary.cleanup()

    def test_platform_specific_user_directories_and_explicit_override(self):
        with patch.object(sys, "platform", "darwin"):
            self.assertEqual(app_paths.user_home(), self.home / "Library/Application Support/GeoBrandDiagnostic")
        with patch.object(sys, "platform", "linux"):
            self.assertEqual(app_paths.user_home(), self.home / ".local/share/GeoBrandDiagnostic")
            with patch.dict(os.environ, {"XDG_DATA_HOME": str(self.home / "XDG 中文")}):
                self.assertEqual(app_paths.user_home(), self.home / "XDG 中文/GeoBrandDiagnostic")
            with patch.dict(os.environ, {"XDG_DATA_HOME": "relative"}):
                self.assertEqual(app_paths.user_home(), self.home / ".local/share/GeoBrandDiagnostic")
        with patch.object(sys, "platform", "win32"), patch.dict(os.environ, {"LOCALAPPDATA": str(self.home / "local")}):
            self.assertEqual(app_paths.user_home(), self.home / "local/GeoBrandDiagnostic")
        with patch.dict(os.environ, {"GEO_USER_HOME": str(self.home / "explicit")}):
            self.assertEqual(app_paths.user_home(), self.home / "explicit")

    def test_existing_unix_profile_is_not_abandoned(self):
        legacy = self.home / "GeoBrandDiagnostic"
        legacy.mkdir()
        for operating_system in ("darwin", "linux"):
            with patch.object(sys, "platform", operating_system):
                self.assertEqual(app_paths.user_home(), legacy)

    def test_linux_native_chromium_is_available_without_downloading_a_browser(self):
        with patch.object(sys, "platform", "linux"), patch.object(browser_discovery.Path, "is_file", return_value=False), patch.object(browser_discovery.shutil, "which", side_effect=lambda n: "/usr/bin/chromium" if n == "chromium" else None):
            choices = browser_discovery.browser_candidates()
            self.assertEqual(choices[-1], ("chromium", {"executable_path": "/usr/bin/chromium"}))
            self.assertEqual(browser_discovery.browser_candidates("chromium"), [choices[-1]])
            self.assertEqual(browser_discovery.browser_candidates("msedge"), [("msedge", {"channel": "msedge"})])
        with patch.object(sys, "platform", "darwin"):
            self.assertEqual([n for n, _ in browser_discovery.browser_candidates()], ["chrome", "msedge"])

    def test_native_build_refuses_cross_os_before_any_mutation(self):
        with patch.object(sys, "platform", "win32"), patch("build_desktop.platform.machine", return_value="AMD64"), patch("build_desktop.subprocess.run") as run:
            self.assertEqual(native_target(), ("windows", "x86_64"))
            with self.assertRaisesRegex(RuntimeError, "Run this build on macos"):
                build("macos")
            run.assert_not_called()
        with patch.object(sys, "platform", "darwin"), patch("build_desktop.platform.machine", return_value="arm64"):
            self.assertEqual(native_target(), ("macos", "arm64"))

    def test_external_browser_library_path_does_not_change_bundled_helpers(self):
        with patch.object(sys,'platform','linux'), patch.object(sys,'frozen',True,create=True), patch.dict(os.environ,{'LD_LIBRARY_PATH':'/bundle/private','LD_LIBRARY_PATH_ORIG':'/system/original'}):
            self.assertEqual(browser_discovery.external_environment()['LD_LIBRARY_PATH'],'/system/original')
            self.assertEqual(os.environ['LD_LIBRARY_PATH'],'/bundle/private')
        with patch.object(sys,'platform','linux'), patch.object(sys,'frozen',True,create=True), patch.dict(os.environ,{'LD_LIBRARY_PATH':'/bundle/private'}):
            self.assertNotIn('LD_LIBRARY_PATH',browser_discovery.external_environment())

    def test_package_audit_rejects_private_data(self):
        (self.home / "geo.sqlite3").write_bytes(b"private")
        with self.assertRaisesRegex(RuntimeError, "Private data"):
            inventory(self.home)

    def test_environment_check_does_not_hide_an_installed_broken_browser(self):
        import environment_check
        from unittest.mock import MagicMock
        engine = MagicMock()
        engine.chromium.launch.side_effect = [RuntimeError("Browser closed by policy"), MagicMock()]
        with patch('playwright.sync_api.sync_playwright') as playwright, patch.object(environment_check, 'browser_candidates', return_value=[('chrome',{'channel':'chrome'}),('chromium',{})]):
            playwright.return_value.__enter__.return_value = engine
            result = environment_check.check()
        self.assertFalse(result['browser_input_verified'])
        self.assertEqual(engine.chromium.launch.call_count, 1)

    @unittest.skipIf(os.name == "nt", "Symlink traversal is a Unix extraction behavior")
    def test_native_tar_extraction_rejects_link_chain_escape(self):
        archive = self.home / "escape.tar.gz"
        target = self.home / "install"; target.mkdir()
        (self.home / "outside").mkdir()
        (target / "outside").mkdir()
        with tarfile.open(archive, "w:gz") as bundle:
            first=tarfile.TarInfo('a'); first.type=tarfile.SYMTYPE; first.linkname='.'; bundle.addfile(first)
            second=tarfile.TarInfo('b'); second.type=tarfile.SYMTYPE; second.linkname='a/../outside'; bundle.addfile(second)
            payload=tarfile.TarInfo('b/file'); payload.size=4; bundle.addfile(payload,io.BytesIO(b'test'))
        try:
            extract_package(archive,target)
        except (tarfile.FilterError, AssertionError):
            pass
        # New Python security updates may safely normalize the link instead.
        for path in target.rglob('*'):
            if path.is_symlink():
                self.assertTrue(path.resolve().is_relative_to(target.resolve()))
        self.assertFalse((self.home/'outside/file').exists())

    @unittest.skipIf(os.name == "nt", "Unix archive symlink behavior verified on Unix")
    def test_bundle_symlinks_are_preserved_and_cannot_escape(self):
        app = self.home / "app"; app.mkdir()
        (app / "framework").write_bytes(b"library")
        (app / "alias").symlink_to("framework")
        self.assertIn({"path": "alias", "symlink": "framework"}, inventory(app))
        (app / "outside").symlink_to(self.home)
        with self.assertRaisesRegex(RuntimeError, "symlink escapes"):
            inventory(app)


if __name__ == "__main__":
    unittest.main()
