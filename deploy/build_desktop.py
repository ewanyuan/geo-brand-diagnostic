"""Build for the current OS/CPU. Does not copy user data or cross-compile."""

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_paths import VERSION


def native_target():
    operating_system = {"win32": "windows", "darwin": "macos", "linux": "linux"}.get(sys.platform)
    cpu = {"amd64": "x86_64", "x86_64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    if not operating_system or not cpu:
        raise RuntimeError("Only native Windows, macOS and Linux x86_64/arm64 builds are configured")
    return operating_system, cpu


def inventory(app):
    forbidden = {"geo.sqlite3", "browser-runtime.json", "login-service.token", "id_rsa", "id_ed25519"}
    files = []
    for path in sorted(app.rglob("*")):
        relative = path.relative_to(app)
        if path.name in forbidden or any(p in {"browser-profile", "evidence", ".ssh", "verification", "data"} for p in relative.parts):
            raise RuntimeError(f"Private data was included: {relative}")
        if path.is_symlink():
            if not path.resolve().is_relative_to(app.resolve()):
                raise RuntimeError(f"Bundle symlink escapes package: {relative}")
            files.append({"path": relative.as_posix(), "symlink": os.readlink(path)})
        elif path.is_file():
            files.append({"path": relative.as_posix(), "bytes": path.stat().st_size,
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return files


def build(expected_platform=None):
    operating_system, cpu = native_target()
    if expected_platform and expected_platform != operating_system:
        raise RuntimeError(f"Run this build on {expected_platform}; current host is {operating_system}")
    label = f"{operating_system}-{cpu}-{VERSION}"
    target, work = ROOT / "dist" / label, ROOT / "build" / label
    if not target.resolve().is_relative_to((ROOT / "dist").resolve()) or not work.resolve().is_relative_to((ROOT / "build").resolve()):
        raise RuntimeError("Build paths escaped the project workspace")
    target.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, GEO_BUILD_VERSION=VERSION)
    env.pop("PLAYWRIGHT_BROWSERS_PATH", None)
    licenses = [Path(sys.base_prefix) / "LICENSE.txt",
                Path(sys.base_prefix) / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "LICENSE.txt",
                Path(sys.base_prefix) / "Resources/LICENSE.txt"]
    license_file = next((p for p in licenses if p.is_file()), None)
    if not license_file:
        raise RuntimeError("Python distribution LICENSE.txt is required to redistribute its runtime")
    env["GEO_BUILD_PYTHON_LICENSE"] = str(license_file)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(target), "--workpath", str(work), str(ROOT / "deploy/desktop.spec")],
                   cwd=ROOT, env=env, check=True)
    # macOS distribution is the entire .app; tar preserves its framework symlinks.
    app = target / ("GEO诊断台.app" if operating_system == "macos" else "GEO-Windows" if operating_system == "windows" else "GEO-Desktop")
    # BUNDLE has already signed the .app; never modify any of its contents here.
    if operating_system != "macos":
        readme = "LINUX_README.txt" if operating_system == "linux" else "DESKTOP_README.txt"
        shutil.copyfile(ROOT / "deploy" / readme, app / "先读我.txt")
        shutil.copyfile(ROOT / "CROSS_PLATFORM.md", app / "平台与验收.md")
    if operating_system == "linux":
        for name in ("启动GEO.sh",):
            shutil.copyfile(ROOT / "deploy" / name, app / name)
            (app / name).chmod(0o755)
    manifest = {"version": VERSION, "platform": operating_system, "architecture": cpu,
                "built_at": datetime.now(timezone.utc).isoformat(),
                "requires_python_install": False, "requires_ai_tool": False,
                "collection_platforms": ["DeepSeek", "豆包", "Kimi"],
                "libc": list(platform.libc_ver()) if operating_system == "linux" else None,
                "executable": "Contents/MacOS/geo-diagnostic" if operating_system == "macos" else "GEO诊断台.exe" if operating_system == "windows" else "geo-diagnostic",
                "files": inventory(app)}
    # Keep the manifest outside a signed .app so writing it never invalidates signing.
    (target / "package-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if operating_system != "macos":
        shutil.copyfile(target / "package-manifest.json", app / "package-manifest.json")
    archive_path = target / (f"GEO-{label}.zip" if operating_system == "windows" else f"GEO-{label}.tar.gz")
    if operating_system == "windows":
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(p for p in app.rglob("*") if p.is_file()):
                archive.write(path, app.name + "/" + path.relative_to(app).as_posix())
    else:
        with tarfile.open(archive_path, "w:gz", dereference=False) as archive:
            archive.add(app, arcname=app.name)
            if operating_system == "macos":
                archive.add(target / "package-manifest.json", arcname="package-manifest.json")
                archive.add(ROOT / "deploy/DESKTOP_README.txt", arcname="先读我.txt")
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    (target / "SHA256.txt").write_text(f"{digest}  {archive_path.name}\n", encoding="ascii")
    print(json.dumps({"package": str(archive_path), "platform": operating_system, "architecture": cpu,
                      "sha256": digest, "bytes": archive_path.stat().st_size}, ensure_ascii=True))
    return archive_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=("windows", "linux", "macos"))
    build(parser.parse_args().platform)
