"""Use the server's existing Firefox with a separate profile for each person."""
from login_service import LoginBrowser


class FirefoxWorkspace(LoginBrowser):
    def __init__(self, root, runtime_config=None):
        super().__init__(root=root)

    def request(self, payload):
        if payload.get("action") == "shutdown":
            payload = {"action": "finish"}
        try:
            return self.action(payload)
        except Exception:
            return {"error": "网页操作未完成，请刷新状态后重试"}
