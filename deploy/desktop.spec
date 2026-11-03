# Native build only: PyInstaller embeds this operating system's Python/driver.
import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

root = Path(SPECPATH).parent
assets = ["index.html", "app.js", "style.css", "local-login.html", "local-login.js", "login.css"]
datas = [(str(root / "static" / name), "static") for name in assets]
datas += [(str(root / "deploy/DESKTOP_README.txt"), "."),
          (str(root / "CROSS_PLATFORM.md"), "."),
          (str(root / "LICENSE"), "."),
          (str(root / "THIRD_PARTY_NOTICES.md"), "."),
          (os.environ["GEO_BUILD_PYTHON_LICENSE"], "LICENSES")]
datas += collect_data_files("tzdata")
for package in ("Flask", "playwright", "werkzeug", "jinja2", "click", "itsdangerous", "markupsafe", "greenlet", "pyee", "tzdata"):
    datas += copy_metadata(package)
a = Analysis([str(root / "standalone.py")], pathex=[str(root)], datas=datas,
             hiddenimports=["browser_network"], excludes=["pytest", "IPython", "tkinter", "numpy", "pandas"])
pyz = PYZ(a.pure)
windows = sys.platform == "win32"
macos = sys.platform == "darwin"
exe = EXE(pyz, a.scripts, [], exclude_binaries=True,
          name="GEO诊断台" if windows else "geo-diagnostic",
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=not (windows or macos), disable_windowed_traceback=True,
          codesign_identity=os.environ.get("GEO_CODESIGN_IDENTITY") if macos else None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False,
               name="GEO-Windows" if windows else "GEO-Desktop")
if macos:
    app = BUNDLE(coll, name="GEO诊断台.app", bundle_identifier="local.geo.brand-diagnostic",
                 version=os.environ["GEO_BUILD_VERSION"],
                 codesign_identity=os.environ.get("GEO_CODESIGN_IDENTITY"),
                 info_plist={"CFBundleDisplayName": "GEO诊断台", "NSHighResolutionCapable": True})
