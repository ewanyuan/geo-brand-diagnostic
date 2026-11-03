"""One GEO collection batch against an explicitly consented participant profile."""

import json
import re
import sys
from pathlib import Path

from guest_portal import AccessStore, DEFAULT_STATE
from browser_worker import SITES, collect_one, driver_for, result
from profile_lock import ProfileLock

PREFIX = "GEO_GUEST_RESULT:"
SUPPORTED = {"DeepSeek": "deepseek", "豆包": "doubao", "Kimi": "kimi"}


def collect(payload, state_dir=DEFAULT_STATE):
    participant = payload.get("participant_id")
    platform = payload.get("platform")
    questions = payload.get("questions")
    if not isinstance(participant, str) or not re.fullmatch(r"[a-f0-9]{32}", participant):
        raise ValueError("unknown participant")
    if platform not in SUPPORTED or not isinstance(questions, list) or not 1 <= len(questions) <= 3:
        raise ValueError("unsupported collection request")
    if any(not isinstance(q, dict) or not isinstance(q.get("id"), int) or
           not isinstance(q.get("text"), str) or not q["text"].strip() or len(q["text"]) > 1000
           for q in questions):
        raise ValueError("invalid question")

    store = AccessStore(state_dir)
    accounts = {item["id"]: item for item in store.collection_accounts()}
    account = accounts.get(participant)
    if not account or not account["can_collect"]:
        raise PermissionError("该用户尚未授权 GEO，或授权已过期")
    if not account.get("platforms", {}).get(SUPPORTED[platform]):
        raise PermissionError("用户尚未在登录中心确认此平台登录")

    root = Path(state_dir) / "participants" / participant
    rows = []
    profile_lock = ProfileLock(root / "profile-use.lock")
    if not profile_lock.acquire():
        return {"results": [result(q["id"], error_message="此账号正在登录或采集中，请稍后重试")
                            for q in questions]}
    try:
        try:
            driver = driver_for(SITES[platform], participant_root=root)
        except Exception as exc:
            message = ("请先在登录中心保存并结束此平台登录" if "LOGIN_CENTER_ACTIVE" in str(exc)
                       else "该平台登录资料尚未保存；请重新登录并确认此平台")
            return {"results": [result(q["id"], error_message=message) for q in questions]}
        try:
            for index, question in enumerate(questions):
                latest = next((item for item in store.collection_accounts() if item["id"] == participant), None)
                if not latest or not latest["can_collect"] or not latest.get("platforms", {}).get(SUPPORTED[platform]):
                    message = "GEO 使用授权已撤销或到期；本批剩余问题未发送"
                    rows.extend(result(q["id"], error_message=message) for q in questions[index:])
                    break
                rows.append(collect_one(driver, SITES[platform], question))
        finally:
            try:
                driver.quit()
            except Exception:
                pass
    finally:
        profile_lock.release()
    return {"results": rows}


def main():
    try:
        payload = json.load(sys.stdin)
        response = collect(payload)
    except PermissionError as exc:
        questions = payload.get("questions", []) if isinstance(locals().get("payload"), dict) else []
        response = {"results": [result(q.get("id"), error_message=str(exc))
                                for q in questions if isinstance(q, dict) and isinstance(q.get("id"), int)]}
    except (ValueError, TypeError, KeyError):
        response = {"results": []}
    except Exception:
        response = {"results": []}
    sys.stdout.write(PREFIX + json.dumps(response, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
