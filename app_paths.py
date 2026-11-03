"""Read-only application assets and separately writable user data."""

import os
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
VERSION = "0.3.1"


def user_home():
    override = os.environ.get("GEO_USER_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "GeoBrandDiagnostic"
    # Keep the previous source version's existing sessions on Unix machines.
    legacy = Path.home() / "GeoBrandDiagnostic"
    if legacy.is_dir():
        return legacy
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "GeoBrandDiagnostic"
    xdg = Path(os.environ.get("XDG_DATA_HOME", "")).expanduser()
    if not xdg.is_absolute():
        xdg = Path.home() / ".local" / "share"
    return xdg / "GeoBrandDiagnostic"


def data_dir():
    override = os.environ.get("GEO_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False) or os.environ.get("GEO_USER_HOME"):
        return user_home() / "data"
    return APP_ROOT / "data"


def child_command(mode):
    if getattr(sys, "frozen", False):
        return [sys.executable, mode]
    return [sys.executable, "-X", "utf8", str(APP_ROOT / "standalone.py"), mode]
