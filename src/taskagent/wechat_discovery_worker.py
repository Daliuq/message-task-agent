"""One-shot local WeChat chat-list reader used by the dashboard discovery API."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from wechatauto import WeChatDB, auto_detect_db_dir, list_accounts


def main() -> int:
    try:
        db_dir = auto_detect_db_dir()
        accounts = list_accounts(db_dir) if db_dir else []
        if not accounts:
            raise RuntimeError("未找到本机微信账号数据库；请确认微信电脑版已登录")

        candidates: list[dict[str, object]] = []
        warnings: list[str] = []
        for account in accounts:
            account_key = str(account.get("account", "")).strip()
            if not account_key:
                continue
            try:
                shared_keys = Path(tempfile.gettempdir()) / "wechatauto_db" / account_key / "keys.json"
                with tempfile.TemporaryDirectory(prefix="message_task_wechat_discovery_") as workdir:
                    keys_file = shared_keys if shared_keys.is_file() else Path(workdir) / "keys.json"
                    db = WeChatDB(
                        db_dir=db_dir,
                        account=account_key,
                        workdir=workdir,
                        keys_file=str(keys_file),
                    )
                    chats = db.list_message_chats()
                for chat in chats:
                    conversation_key = str(chat.get("username", "")).strip()
                    if not conversation_key:
                        continue
                    display_name = str(chat.get("name") or conversation_key).strip()
                    candidates.append({
                        "account_key": account_key,
                        "account_name": str(account.get("self_nick") or account_key),
                        "conversation_key": conversation_key,
                        "display_name": display_name,
                        "message_count": max(0, int(chat.get("message_count", 0))),
                        "conversation_type": "group" if conversation_key.endswith("@chatroom") else "private",
                    })
            except Exception as exc:
                warnings.append(f"账号 {account_key}：{type(exc).__name__}: {exc}")
        payload = {"candidates": candidates, "warnings": warnings}
        print("__CHAT_DISCOVERY_RESULT__" + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return 0
    except Exception as exc:
        print(f"微信会话发现失败：{type(exc).__name__}: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
