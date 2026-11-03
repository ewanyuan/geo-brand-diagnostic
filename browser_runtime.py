"""Select the project browser runtime without storing any account data here."""

import importlib.util
import json
import os
from app_paths import data_dir


def configuration():
    try:
        data = json.loads((data_dir() / "browser-runtime.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def is_local():
    return configuration().get("mode", "local") == "local"


def local_ready():
    return bool(importlib.util.find_spec("playwright"))


def call_local(payload, timeout=70):
    # Use HTTP in-process. A frozen GUI executable is not a Python interpreter
    # and has no stdout pipe suitable for the former script-based client.
    from local_login_service import client_request
    try:
        data = client_request(payload)
    except (OSError, ValueError):
        raise RuntimeError("本机专用浏览器暂未响应，请刷新状态确认页面后重试") from None
    if not isinstance(data, dict):
        raise RuntimeError("本机专用浏览器暂未响应，请刷新状态确认页面后重试")
    if data.get("error"):
        raise RuntimeError(data["error"])
    return data
