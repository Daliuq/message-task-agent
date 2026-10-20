"""Pinned wechatauto-replica bridge. Executes SELECTs against isolated decrypted caches only.

Private APIs are deliberately wrapped here, not spread through the application.
Original message columns (including blobs) are preserved as JSON/base64.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MARKER = "__CHAT_IMPORT__"


def encode(value):
    if isinstance(value, bytes):
        return {"$base64": base64.b64encode(value).decode("ascii")}
    return value


def anchor(conn, table, local_id):
    row = conn.execute(f'SELECT local_id,server_id,create_time FROM "{table}" WHERE local_id=?', (local_id,)).fetchone()
    return list(row) if row else None


def read_pages(db, user, config, checkpoint, live_cursor):
    """Keyset pagination by shard/local_id, not by non-unique sort_seq or live OFFSET."""
    md5 = user if re.fullmatch(r"[0-9a-f]{32}", user) else hashlib.md5(user.encode()).hexdigest()
    table = "Msg_" + md5
    connections, shards = [], []
    resume_manifest = checkpoint.get("manifest", {})
    expected = {s["id"]: s for s in resume_manifest.get("shards", [])}
    try:
        rels = db._message_dbs()
        if not rels:
            raise RuntimeError("没有可读取消息分片，不能声明已导入全部历史")
        for rel in sorted(rels):
            conn = db._open(rel)
            connections.append(conn)
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if not exists:
                continue
            label = str(rel).replace("\\", "/")
            count, unique, maximum = conn.execute(f'SELECT COUNT(*),COUNT(DISTINCT local_id),MAX(local_id) FROM "{table}"').fetchone()
            if count != unique:
                raise RuntimeError("分片消息主键不唯一，停止导入以免跳过记录")
            current_upper = maximum if maximum is not None else -1
            if label in expected:
                spec = expected[label]
                if current_upper < spec["upper"] or anchor(conn, table, spec["upper"]) != spec["upper_anchor"]:
                    raise RuntimeError("源消息分片已重置或被替换，请重新建立全量导入任务")
            else:
                if expected:
                    # New shards arriving during resume belong to the next job.
                    continue
                after = live_cursor.get("positions", {}).get(label, -1) if config["mode"] == "incremental" else -1
                old_anchor = live_cursor.get("anchors", {}).get(label)
                if config["mode"] == "incremental" and after >= 0 and (
                    current_upper < after or anchor(conn, table, after) != old_anchor):
                    raise RuntimeError("增量源主键已重置，必须重新全量导入；没有跳过旧记录")
                where = "local_id>? AND local_id<=?"
                params = [after, current_upper]
                if config["mode"] == "range":
                    where += " AND create_time>=? AND create_time<?"
                    params += [datetime.fromisoformat(config["start"]).timestamp(), datetime.fromisoformat(config["end"]).timestamp()]
                selected, min_time, max_time = conn.execute(f'SELECT COUNT(*),MIN(create_time),MAX(create_time) FROM "{table}" WHERE {where}', params).fetchone()
                unknown = conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE create_time IS NULL OR create_time<=0').fetchone()[0]
                spec = {"id": label, "upper": current_upper, "upper_anchor": anchor(conn, table, current_upper),
                        "start_after": after, "count": selected, "min_time": min_time, "max_time": max_time,
                        "unknown_time_count": unknown}
            shards.append((conn, spec))
        if expected and set(expected) != {s["id"] for _, s in shards}:
            raise RuntimeError("续扫所需的消息分片缺失；未标记为完成")
        if not shards:
            raise RuntimeError("所选会话没有可验证的消息表；请重新发现会话后再试")
        yield {"type": "manifest", "shards": [s for _, s in shards],
               "coverage": "local_snapshot_only", "media": "raw_columns_saved_but_external_media_not_copied"}
        for conn, spec in shards:
            label = spec["id"]
            after = checkpoint.get("positions", {}).get(label, spec["start_after"])
            maps = {db._shard_label(conn): db._name2id_map(conn)}
            while after < spec["upper"]:
                where, params = "local_id>? AND local_id<=?", [after, spec["upper"]]
                if config["mode"] == "range":
                    where += " AND create_time>=? AND create_time<?"
                    params += [datetime.fromisoformat(config["start"]).timestamp(), datetime.fromisoformat(config["end"]).timestamp()]
                rows = conn.execute(f'SELECT * FROM "{table}" WHERE {where} ORDER BY local_id LIMIT ?', [*params, config["batch_size"]]).fetchall()
                messages = []
                for row in rows:
                    data = dict(row)
                    data["shard"] = db._shard_label(conn)
                    parsed = db._msg_row_to_dict(data, maps)
                    server_id = data.get("server_id")
                    stable = f"server:{server_id}" if server_id and str(server_id) != "0" else f"local:{label}:{data['local_id']}"
                    stamp = data.get("create_time")
                    moment = datetime.fromtimestamp(float(stamp), timezone.utc).isoformat() if stamp and float(stamp) > 0 else None
                    text = parsed.get("content")
                    if not isinstance(text, str):
                        text = "[正文未解析，原始字段已保存]"
                    mtype = parsed.get("type", "unknown")
                    attachments = [] if mtype in {"文本", "系统消息"} else [{"filename": "", "content_type": "application/octet-stream", "available": False}]
                    messages.append({"external_id": stable, "local_id": data["local_id"], "server_id": str(server_id or ""),
                        "shard": label, "source_time": moment, "text": text,
                        "sender": parsed.get("sender_username") or str(parsed.get("sender_id") or ""),
                        "message_type": mtype, "attachments": attachments,
                        "raw_row": {key: encode(value) for key, value in dict(row).items()}})
                after = rows[-1]["local_id"] if rows else spec["upper"]
                yield {"type": "batch", "shard": label, "after": after, "messages": messages,
                       "anchor": anchor(conn, table, after)}
        yield {"type": "done"}
    finally:
        for conn in connections:
            conn.close()


def main():
    import sys
    from wechatauto import WeChatDB, auto_detect_db_dir, list_accounts
    def emit(frame):
        print(MARKER + json.dumps(frame, ensure_ascii=False, separators=(",", ":")), flush=True)
        if sys.stdin.readline().strip() != "ACK":
            raise RuntimeError("主进程没有确认分页持久化，停止读取")
    try:
        request = json.loads(sys.stdin.readline())
        root = auto_detect_db_dir()
        account = request["scope"]["account_key"]
        accounts = list_accounts(root) if root else []
        if account not in {str(a.get("account", "")) for a in accounts}:
            raise RuntimeError("订阅账号不在本机可读微信数据库中")
        keys = Path(tempfile.gettempdir()) / "wechatauto_db" / account / "keys.json"
        with tempfile.TemporaryDirectory(prefix="message_task_wechat_import_") as cache:
            db = WeChatDB(db_dir=root, account=account, workdir=cache,
                          keys_file=str(keys if keys.is_file() else Path(cache) / "keys.json"))
            for frame in read_pages(db, request["scope"]["conversation_key"], request["config"],
                                    request["checkpoint"], request["live_cursor"]):
                emit(frame)
        return 0
    except Exception as exc:
        # No raw message or key material in operational error output.
        detail = re.sub(r"[a-fA-F0-9]{32,}", "[redacted]", str(exc))[:400]
        print(MARKER + json.dumps({"type": "error", "error": type(exc).__name__ + ": " + detail}, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
