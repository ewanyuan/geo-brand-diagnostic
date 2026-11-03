"""Native browser choices shared by the product and packaged environment check."""

import shutil
import sys
import os
from pathlib import Path


def external_environment():
    """System browsers must not load PyInstaller's private Linux libraries."""
    env = dict(os.environ)
    if sys.platform.startswith("linux") and getattr(sys, "frozen", False):
        original = env.get("LD_LIBRARY_PATH_ORIG")
        if original is None:
            env.pop("LD_LIBRARY_PATH", None)
        else:
            env["LD_LIBRARY_PATH"] = original
    return env


def browser_candidates(preferred=None):
    if preferred in ("chrome", "msedge"):
        return [(preferred, {"channel": preferred})]
    native = []
    if sys.platform.startswith("linux"):
        # Debian's launcher is a shell wrapper that itself needs PATH commands.
        # Prefer its actual native binary, including in a stripped test runtime.
        for path in ("/usr/lib/chromium/chromium", "/usr/lib64/chromium-browser/chromium-browser"):
            if Path(path).is_file() and os.access(path, os.X_OK):
                native.append(("chromium", {"executable_path": path}))
                break
        for name in ("chromium", "chromium-browser"):
            if native:
                break
            path = shutil.which(name)
            if path:
                native.append(("chromium", {"executable_path": path}))
                break
    if preferred == "chromium":
        return native or [("chromium", {})]
    return [("chrome", {"channel": "chrome"}), ("msedge", {"channel": "msedge"})] + native
