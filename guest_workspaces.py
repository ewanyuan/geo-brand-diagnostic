"""Bounded, participant-owned browser workspaces for the standalone portal."""

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from local_login_service import Workspace
from login_sites import SITES


class WorkspaceUnavailable(RuntimeError):
    pass


def workspace_factory(root, runtime_config):
    if runtime_config.get("engine") == "firefox":
        from guest_firefox import FirefoxWorkspace
        return FirefoxWorkspace(root=root)
    return Workspace(root=root, runtime_config=runtime_config)


@dataclass
class Slot:
    workspace: object
    touched: float = field(default_factory=time.monotonic)
    lock: object = field(default_factory=threading.Lock)
    inflight: int = 0
    closing: bool = False


class WorkspacePool:
    def __init__(self, root, maximum=2, idle_seconds=1200, runtime_config=None,
                 factory=workspace_factory, sweep=True):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.maximum = maximum
        self.idle_seconds = idle_seconds
        self.runtime_config = runtime_config or {"browser_channel": "chromium"}
        self.factory = factory
        self.slots = {}
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        if sweep:
            threading.Thread(target=self._sweep, daemon=True).start()

    def directory(self, participant):
        # Only a server-issued ID reaches the filesystem, never a request field.
        if not isinstance(participant, str) or not re.fullmatch(r"[a-f0-9]{32}", participant):
            raise ValueError("Invalid participant")
        return self.root / participant

    def inactive_status(self, participant):
        platforms = [{**s, "state": "not_prepared", "methods": [], "confirmed": False} for s in SITES]
        try:
            saved = json.loads((self.directory(participant) / "login-readiness.json").read_text(encoding="utf-8"))
            allowed = {"state", "methods", "confirmed", "message", "checked_at", "page_url"}
            for site in platforms:
                previous = next((p for p in saved.get("platforms", []) if p.get("id") == site["id"]), {})
                site.update({k: v for k, v in previous.items() if k in allowed})
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return {"connected": True, "active": False, "preparing": False, "platforms": platforms}

    def request(self, participant, payload):
        self.directory(participant)
        action = payload["action"]
        with self.lock:
            slot = self.slots.get(participant)
            if slot and slot.closing:
                raise WorkspaceUnavailable("正在保存你的工作区，请稍后刷新状态")
            if action == "status":
                result = slot.workspace.status() if slot else self.inactive_status(participant)
                return {**result, "runtime": "guest", "location": "你的专属浏览器"}
            if not slot:
                if action == "finish":
                    return {**self.inactive_status(participant), "location": "你的专属浏览器"}
                if action != "prepare":
                    raise WorkspaceUnavailable("请先准备你的平台登录页")
                if len(self.slots) >= self.maximum:
                    raise WorkspaceUnavailable("服务器当前的登录名额已满，请稍后重试；你的已保存会话会保留")
                slot = Slot(self.factory(root=self.directory(participant), runtime_config=dict(self.runtime_config)))
                self.slots[participant] = slot
            slot.inflight += 1
            slot.touched = time.monotonic()
        try:
            with slot.lock:
                if slot.closing:
                    raise WorkspaceUnavailable("正在保存你的工作区，请稍后刷新状态")
                result = slot.workspace.request(payload)
                if action == "finish" and not result.get("error"):
                    with self.lock:
                        slot.closing = True
                    closed = slot.workspace.request({"action": "shutdown"})
                    if closed.get("error"):
                        with self.lock:
                            slot.closing = False
                        raise WorkspaceUnavailable("工作区仍在保存，请稍后刷新状态")
                    with self.lock:
                        self.slots.pop(participant, None)
                return {**result, "runtime": "guest", "location": "你的专属浏览器"}
        finally:
            with self.lock:
                slot.inflight -= 1
                slot.touched = time.monotonic()

    def expire_idle(self):
        with self.lock:
            expired = [(uid, slot) for uid, slot in self.slots.items()
                       if not slot.closing and not slot.inflight and not slot.workspace.preparing
                       and time.monotonic() - slot.touched > self.idle_seconds]
            for _, slot in expired:
                slot.closing = True
        for uid, slot in expired:
            try:
                with slot.lock:
                    result = slot.workspace.request({"action": "shutdown"})
                    if result.get("error"):
                        raise RuntimeError()
                with self.lock:
                    self.slots.pop(uid, None)
            except Exception:
                # Keep the slot reserved if its profile has not been released.
                with self.lock:
                    slot.closing = False

    def _sweep(self):
        while not self.stopping.wait(30):
            self.expire_idle()
