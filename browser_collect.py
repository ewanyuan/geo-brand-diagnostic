"""Run website collection in the selected local or authorized SSH browser."""

import json
import os
import socket
import subprocess
from pathlib import Path
from browser_runtime import call_local, is_local, local_ready


PLATFORMS = ("DeepSeek", "豆包", "Kimi")
RESULT_PREFIX = "GEO_BROWSER_RESULT:"
GUEST_RESULT_PREFIX = "GEO_GUEST_RESULT:"
GUEST_ROOT = "~/.local/share/geo-login-center/app"
GUEST_PYTHON = "~/.local/share/geo-login-center/venv/bin/python"
DEFAULT_SSH_TARGET = ""
DEFAULT_SSH_KEY = ""


class BrowserCollectionError(Exception):
    pass


def browser_ready():
    if is_local():
        return local_ready()
    target = os.getenv("GEO_BROWSER_SSH_TARGET", "").strip()
    key = os.getenv("GEO_BROWSER_SSH_KEY", "").strip()
    return bool(target and key and Path(key).is_file())


def guest_remote_ready():
    target = os.getenv("GEO_BROWSER_SSH_TARGET", DEFAULT_SSH_TARGET).strip()
    key = os.getenv("GEO_BROWSER_SSH_KEY", DEFAULT_SSH_KEY).strip()
    return bool(target and key and Path(key).is_file())


def _guest_ssh(command, payload=None, timeout=35):
    if not guest_remote_ready():
        raise BrowserCollectionError("远程采集机尚未连接")
    target = os.getenv("GEO_BROWSER_SSH_TARGET", DEFAULT_SSH_TARGET).strip()
    key = os.getenv("GEO_BROWSER_SSH_KEY", DEFAULT_SSH_KEY).strip()
    argv = ["ssh", "-T", "-i", key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15", target, *command]
    proxy_helper = Path(__file__).resolve().parent / "tools" / "ssh_via_local_proxy.py"
    if target and proxy_helper.is_file():
        try:
            with socket.create_connection(("127.0.0.1", 17891), timeout=0.5):
                argv[argv.index(target):argv.index(target)] = [
                    "-o", f"ProxyCommand=python {proxy_helper.as_posix()}"
                ]
        except OSError:
            pass
    try:
        process = subprocess.run(argv, input=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                                 text=True, capture_output=True, encoding="utf-8", timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise BrowserCollectionError("远程采集机连接或运行超时") from None
    if process.returncode:
        raise BrowserCollectionError("远程采集机未能完成请求")
    return process.stdout


def list_guest_accounts():
    output = _guest_ssh([GUEST_PYTHON, f"{GUEST_ROOT}/guest_portal.py", "list", "--json"])
    try:
        accounts = json.loads(output.strip())
    except (ValueError, TypeError):
        raise BrowserCollectionError("远程服务未返回可识别的账号授权状态") from None
    if not isinstance(accounts, list):
        raise BrowserCollectionError("远程服务返回的账号授权状态无效")
    return accounts


def collect_remote(platform, questions, participant_id=None):
    if platform not in PLATFORMS:
        raise BrowserCollectionError("尚未支持该网页平台")
    if participant_id is not None:
        if not isinstance(participant_id, str) or len(participant_id) != 32:
            raise BrowserCollectionError("登录账号选择无效")
        output = _guest_ssh(
            [GUEST_PYTHON, f"{GUEST_ROOT}/guest_collect.py"],
            {"participant_id": participant_id, "platform": platform, "questions": questions},
            timeout=45 + 150 * len(questions),
        )
        line = next((line[len(GUEST_RESULT_PREFIX):] for line in reversed(output.splitlines())
                     if line.startswith(GUEST_RESULT_PREFIX)), None)
        if line is None:
            raise BrowserCollectionError("远程服务没有返回可识别的采集结果")
        try:
            result = json.loads(line)
        except json.JSONDecodeError:
            raise BrowserCollectionError("远程服务返回的数据格式无效") from None
        if not isinstance(result, dict) or not isinstance(result.get("results"), list):
            raise BrowserCollectionError("远程服务返回的数据结构无效")
        return result["results"]
    if not browser_ready():
        raise BrowserCollectionError("浏览器采集机尚未配置")
    if is_local():
        try:
            return call_local({"action": "collect", "platform": platform, "questions": questions},
                              timeout=720)["results"]
        except RuntimeError as exc:
            raise BrowserCollectionError(str(exc)) from None
        except KeyError:
            raise BrowserCollectionError("本机网页采集未返回结果，请检查浏览器窗口") from None
    target = os.environ["GEO_BROWSER_SSH_TARGET"].strip()
    key = os.environ["GEO_BROWSER_SSH_KEY"].strip()
    remote_python = os.getenv(
        "GEO_BROWSER_REMOTE_PYTHON",
        "~/.local/share/geo-browser/venv/bin/python",
    )
    remote_worker = os.getenv(
        "GEO_BROWSER_REMOTE_WORKER",
        "~/.local/share/geo-browser/browser_worker.py",
    )
    command = [
        "ssh", "-T", "-i", key, "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=15", target, remote_python, remote_worker,
    ]
    payload = json.dumps({"platform": platform, "questions": questions}, ensure_ascii=False)
    try:
        process = subprocess.run(
            command, input=payload, text=True, capture_output=True,
            encoding="utf-8", timeout=45 + 150 * len(questions), check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BrowserCollectionError("采集机连接或运行超时") from exc
    if process.returncode:
        raise BrowserCollectionError("采集机未能完成网页采集")
    output = next(
        (line[len(RESULT_PREFIX):] for line in reversed(process.stdout.splitlines())
         if line.startswith(RESULT_PREFIX)), None,
    )
    if output is None:
        raise BrowserCollectionError("采集机没有返回可识别的结果")
    try:
        result = json.loads(output)
    except json.JSONDecodeError as exc:
        raise BrowserCollectionError("采集机返回的数据格式无效") from exc
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        raise BrowserCollectionError("采集机返回的数据结构无效")
    return result["results"]
