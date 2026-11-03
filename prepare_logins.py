"""Deploy and prepare the six login pages without sending any verification code."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from login_center import remote_login
from browser_runtime import is_local


def main():
    if is_local():
        result = remote_login({"action": "prepare"})
        deadline = time.monotonic() + 420
        while result.get("preparing") and time.monotonic() < deadline:
            print("正在本机专用浏览器准备平台页面…", flush=True)
            time.sleep(6)
            result = remote_login({"action": "status"})
        report = Path(__file__).resolve().parent / "data/login-readiness.json"
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        for site in result.get("platforms", []):
            print(site["name"] + "：" + site.get("message", "待检查"))
        return 0 if not result.get("preparing") else 4
    target = os.environ.get("GEO_BROWSER_SSH_TARGET", "")
    key = os.environ.get("GEO_BROWSER_SSH_KEY", "")
    if not target or not Path(key).is_file():
        print("请先配置采集机地址和私钥文件路径。")
        return 1
    ssh = ["-i", key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
           "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=25"]
    try:
        check = subprocess.run(["ssh", "-T", *ssh, target, "echo", "ready"],
                               capture_output=True, timeout=35)
        if check.returncode:
            print("采集机的 SSH 握手未完成；平台页面尚未部署，不会请求短信验证码。")
            return 2
        root = Path(__file__).resolve().parent
        files = [str(root / name) for name in ("login_service.py", "login_sites.py", "browser_worker.py")]
        copied = subprocess.run(["scp", *ssh, *files,
                                 target + ":~/.local/share/geo-browser/"],
                                capture_output=True, timeout=60)
        if copied.returncode:
            print("登录中心文件未能传到采集机，现有会话保持原状。")
            return 3
        result = remote_login({"action": "prepare"})
        deadline = time.monotonic() + 420
        while result.get("preparing") and time.monotonic() < deadline:
            print("正在准备平台页面…", flush=True)
            time.sleep(8)
            result = remote_login({"action": "status"})
        report = root / "data/login-readiness.json"
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        for site in result.get("platforms", []):
            print(site["name"] + "：" + site.get("message", site.get("state", "待检查")))
        return 0 if not result.get("preparing") else 4
    except (OSError, subprocess.TimeoutExpired, RuntimeError):
        print("采集机暂时未完成准备。恢复连接后可重复运行，已有浏览器会话由登录中心保留。")
        return 5


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
