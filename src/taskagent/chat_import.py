"""Durable, asynchronous chat import. Scanning never invokes an LLM or sends mail."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .store import Store, _json, utc_now
from .mail import _range_bound

_DDL = """
CREATE TABLE IF NOT EXISTS chat_import_jobs(
 id TEXT PRIMARY KEY,config_json TEXT NOT NULL,status TEXT NOT NULL,
 owner TEXT,lease_until TEXT,cancel_requested INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL,finished_at TEXT);
CREATE TABLE IF NOT EXISTS chat_import_scopes(
 job_id TEXT NOT NULL REFERENCES chat_import_jobs(id),scope_id TEXT NOT NULL REFERENCES source_scopes(id),
 status TEXT NOT NULL,checkpoint_json TEXT NOT NULL DEFAULT '{}',manifest_json TEXT,
 processed INTEGER NOT NULL DEFAULT 0,items INTEGER NOT NULL DEFAULT 0,versions INTEGER NOT NULL DEFAULT 0,
 error TEXT,PRIMARY KEY(job_id,scope_id));
CREATE UNIQUE INDEX IF NOT EXISTS ix_chat_import_scope_owner ON chat_import_scopes(scope_id)
 WHERE status IN ('queued','running');
"""


class ImportCancelled(RuntimeError):
    pass


class ChatImports:
    def __init__(self, store: Store):
        self.store = store
        with store.connect() as conn:
            conn.executescript(_DDL)
            conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(7,?)", (utc_now(),))

    def create(self, *, platform="wechat", mode="full", scope_ids=None,
               start=None, end=None, batch_size=200) -> dict:
        if platform not in {"wechat", "qq"} or mode not in {"full", "range", "incremental"}:
            raise ValueError("平台或扫描方式无效")
        if type(batch_size) is not int or not 1 <= batch_size <= 1000:
            raise ValueError("batch_size 必须为 1–1000")
        if scope_ids is not None and (not isinstance(scope_ids, list) or
                                     not scope_ids or not all(isinstance(i, str) for i in scope_ids)):
            raise ValueError("scope_ids 必须为非空 ID 数组；省略时使用已启用范围")
        if mode == "range":
            if not isinstance(start, str) or not isinstance(end, str):
                raise ValueError("范围导入必须提供开始和结束日期")
            begin, finish = _range_bound(start, end=False), _range_bound(end, end=True)
            if begin >= finish:
                raise ValueError("结束时间必须晚于开始")
            start, end = begin.isoformat(), finish.isoformat()
        elif start is not None or end is not None:
            raise ValueError("仅时间范围导入接收起止日期")
        available = self.store.active_chat_subscriptions(platform, full_scan=mode != "incremental")
        selected = [s for s in available if scope_ids is None or s["id"] in scope_ids]
        if scope_ids is not None and set(scope_ids) != {s["id"] for s in selected}:
            raise ValueError("所选会话不存在、未启用订阅或未启用参与全量扫描")
        if not selected:
            raise ValueError("没有可导入会话，请先选择聊天并启用对应扫描选项")
        if platform == "qq":
            from .qq import status
            ready = {a["account"] for a in status(self.store.data_dir)["accounts"] if a["readable"]}
            if any(s["account_key"] not in ready for s in selected):
                raise ValueError("QQ 账号缺少已验证密钥，无法解密；请先完成取钥")
        config = {"platform": platform, "mode": mode, "start": start, "end": end,
                  "batch_size": batch_size, "scope_ids": [s["id"] for s in selected]}
        job_id = str(uuid.uuid4())
        try:
            with self.store.transaction() as conn:
                conn.execute("INSERT INTO chat_import_jobs(id,config_json,status,created_at) VALUES(?,?,'queued',?)",
                             (job_id, _json(config), utc_now()))
                for scope in selected:
                    conn.execute("INSERT INTO chat_import_scopes(job_id,scope_id,status) VALUES(?,?,'queued')",
                                 (job_id, scope["id"]))
        except sqlite3.IntegrityError as exc:
            raise ValueError("所选会话已有未结束的导入，请先查看或恢复原任务") from exc
        return self.job(job_id)

    def job(self, job_id: str) -> dict:
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM chat_import_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ValueError("导入任务不存在")
            value = dict(row)
            value["config"] = json.loads(value.pop("config_json"))
            value.pop("owner")
            value["scopes"] = []
            for child in conn.execute("""SELECT j.*,sc.display_name FROM chat_import_scopes j
                    JOIN source_scopes sc ON sc.id=j.scope_id WHERE job_id=? ORDER BY sc.display_name""", (job_id,)):
                scope = dict(child)
                scope["checkpoint"] = json.loads(scope.pop("checkpoint_json"))
                manifest_json = scope.pop("manifest_json")
                scope["manifest"] = json.loads(manifest_json) if manifest_json else None
                scope["total"] = sum(s["count"] for s in (scope["manifest"] or {}).get("shards", []))
                value["scopes"].append(scope)
            value["processed"] = sum(s["processed"] for s in value["scopes"])
            value["items"] = sum(s["items"] for s in value["scopes"])
            value["versions"] = sum(s["versions"] for s in value["scopes"])
            value["resumable"] = value["status"] not in {"completed", "cancelled"} and (
                not value["lease_until"] or value["lease_until"] <= utc_now())
            return value

    def jobs(self) -> list[dict]:
        with self.store.connect() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM chat_import_jobs ORDER BY created_at DESC,rowid DESC LIMIT 30")]
        return [self.job(i) for i in ids]

    def cancel(self, job_id: str) -> dict:
        with self.store.transaction() as conn:
            row = conn.execute("SELECT status,lease_until FROM chat_import_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ValueError("导入任务不存在")
            if row["status"] != "completed":
                conn.execute("UPDATE chat_import_jobs SET cancel_requested=1 WHERE id=?", (job_id,))
                if not row["lease_until"] or row["lease_until"] <= utc_now():
                    conn.execute("UPDATE chat_import_jobs SET status='cancelled',finished_at=? WHERE id=?", (utc_now(), job_id))
                    conn.execute("UPDATE chat_import_scopes SET status='cancelled' WHERE job_id=? AND status<>'completed'", (job_id,))
        return self.job(job_id)

    @staticmethod
    def _lease() -> str:
        return (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat(timespec="seconds")

    def run(self, job_id: str, reader=None) -> dict:
        """Claim or resume a job; caller may use a background thread. Commit precedes reader ACK."""
        if reader is None:
            from .chat_reader import read_wechat_messages, read_qq_messages
            def reader(scope, config, checkpoint, live_cursor):
                def check_cancelled():
                    with self.store.connect() as conn:
                        self._check(conn, job_id, owner, scope["id"], config)
                    return False
                if config["platform"] == "qq":
                    return read_qq_messages(scope, config, checkpoint, live_cursor, data_dir=self.store.data_dir, cancelled=check_cancelled)
                return read_wechat_messages(scope, config, checkpoint, live_cursor, cancelled=check_cancelled)
        owner = str(uuid.uuid4())
        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM chat_import_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ValueError("导入任务不存在")
            if row["status"] in {"completed", "cancelled"}:
                return self.job(job_id)
            if row["lease_until"] and row["lease_until"] > utc_now():
                raise ValueError("任务正在运行，请勿重复启动")
            config = json.loads(row["config_json"])
            try:
                conn.execute("UPDATE chat_import_scopes SET status='queued',error=NULL WHERE job_id=? AND status<>'completed'", (job_id,))
            except sqlite3.IntegrityError as exc:
                raise ValueError("已有新任务占用此会话，请先结束新任务") from exc
            conn.execute("UPDATE chat_import_jobs SET status='running',owner=?,lease_until=?,finished_at=NULL WHERE id=?",
                         (owner, self._lease(), job_id))
        stop = threading.Event()
        def heartbeat():
            while not stop.wait(15):
                try:
                    with self.store.connect() as conn:
                        conn.execute("UPDATE chat_import_jobs SET lease_until=? WHERE id=? AND owner=? AND status='running'",
                                     (self._lease(), job_id, owner))
                except sqlite3.Error:
                    pass
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            for entry in self.job(job_id)["scopes"]:
                if entry["status"] == "completed":
                    continue
                scope = next(s for s in self.store.chat_subscriptions() if s["id"] == entry["scope_id"])
                try:
                    with self.store.transaction() as conn:
                        self._check(conn, job_id, owner, scope["id"], config)
                        conn.execute("UPDATE chat_import_scopes SET status='running' WHERE job_id=? AND scope_id=?", (job_id, scope["id"]))
                    with self.store.connect() as conn:
                        row = conn.execute("SELECT cursor_json FROM source_cursors WHERE scope_id=? AND cursor_name='chat_native_v1'", (scope["id"],)).fetchone()
                        live_cursor = json.loads(row[0]) if row else {}
                    if config["platform"] == "qq" and config["mode"] == "incremental" and "qq_baseline" not in entry["checkpoint"]:
                        # Freeze the identity/fingerprint baseline for this JOB, so resume
                        # cannot exclude pages that this same job already committed.
                        with self.store.transaction() as conn:
                            baseline = {r["external_id"]: json.loads(r["metadata_json"]).get("native_fingerprint")
                                for r in conn.execute("""SELECT i.external_id,v.metadata_json FROM source_items i
                                    JOIN item_versions v ON v.id=i.latest_version_id WHERE i.scope_id=?""", (scope["id"],))}
                            entry["checkpoint"]["qq_baseline"] = baseline
                            conn.execute("UPDATE chat_import_scopes SET checkpoint_json=? WHERE job_id=? AND scope_id=?",
                                (_json(entry["checkpoint"]), job_id, scope["id"]))
                    stream = reader(scope, config, entry["checkpoint"], live_cursor)
                    got_done = False
                    try:
                        for frame in stream:
                            with self.store.transaction() as conn:
                                self._check(conn, job_id, owner, scope["id"], config)
                                self._apply_frame(conn, job_id, scope, config, frame)
                            if frame["type"] == "done":
                                got_done = True
                        if not got_done:
                            raise RuntimeError("读取器提前结束，未返回完成标记；保留已入库分页和检查点")
                    finally:
                        close = getattr(stream, "close", None)
                        if close:
                            close()
                except ImportCancelled:
                    raise
                except Exception as exc:
                    with self.store.transaction() as conn:
                        if not self._owns(conn, job_id, owner):
                            raise ImportCancelled("任务已由其他进程接管")
                        conn.execute("UPDATE chat_import_scopes SET status='failed',error=? WHERE job_id=? AND scope_id=?",
                                     (f"{type(exc).__name__}: {str(exc)[:500]}", job_id, scope["id"]))
            with self.store.transaction() as conn:
                failed = conn.execute("SELECT COUNT(*) FROM chat_import_scopes WHERE job_id=? AND status<>'completed'", (job_id,)).fetchone()[0]
                conn.execute("UPDATE chat_import_jobs SET status=?,owner=NULL,lease_until=NULL,finished_at=? WHERE id=? AND owner=?",
                             ("partial" if failed else "completed", utc_now(), job_id, owner))
        except ImportCancelled:
            with self.store.transaction() as conn:
                if self._owns(conn, job_id, owner):
                    conn.execute("UPDATE chat_import_scopes SET status='cancelled' WHERE job_id=? AND status<>'completed'", (job_id,))
                    conn.execute("UPDATE chat_import_jobs SET status='cancelled',owner=NULL,lease_until=NULL,finished_at=? WHERE id=? AND owner=?",
                                 (utc_now(), job_id, owner))
        finally:
            stop.set()
            thread.join(timeout=2)
        return self.job(job_id)

    @staticmethod
    def _owns(conn, job_id, owner):
        row = conn.execute("SELECT owner FROM chat_import_jobs WHERE id=?", (job_id,)).fetchone()
        return row and row[0] == owner

    def _check(self, conn, job_id, owner, scope_id, config):
        job = conn.execute("SELECT owner,cancel_requested FROM chat_import_jobs WHERE id=?", (job_id,)).fetchone()
        if not job or job["owner"] != owner:
            raise ImportCancelled("任务所有权已变化")
        if job["cancel_requested"]:
            raise ImportCancelled("用户取消")
        scope = conn.execute("SELECT enabled,full_scan_enabled FROM source_scopes WHERE id=?", (scope_id,)).fetchone()
        if not scope or not scope["enabled"] or (config["mode"] != "incremental" and not scope["full_scan_enabled"]):
            raise ValueError("会话订阅或全量扫描已关闭，已停止读取")

    def _raw_path(self, data: dict) -> str:
        raw = _json(data).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        relative = Path("raw/chat") / digest[:2] / (digest + ".json")
        path = self.store.data_dir / relative
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                with temp.open("wb") as f:
                    f.write(raw)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp, path)
            finally:
                temp.unlink(missing_ok=True)
        return relative.as_posix()

    def _apply_frame(self, conn, job_id, scope, config, frame):
        kind, scope_id = frame.get("type"), scope["id"]
        entry = conn.execute("SELECT * FROM chat_import_scopes WHERE job_id=? AND scope_id=?", (job_id, scope_id)).fetchone()
        if kind == "manifest":
            if entry["manifest_json"] and json.loads(entry["manifest_json"])["shards"] != frame["shards"]:
                raise ValueError("源快照发生变化，无法按旧检查点续扫，请新建导入任务")
            conn.execute("UPDATE chat_import_scopes SET manifest_json=? WHERE job_id=? AND scope_id=?",
                         (_json(frame), job_id, scope_id))
            checkpoint = json.loads(entry["checkpoint_json"])
            checkpoint["manifest"] = frame
            conn.execute("UPDATE chat_import_scopes SET checkpoint_json=? WHERE job_id=? AND scope_id=?", (_json(checkpoint), job_id, scope_id))
            return
        manifest = json.loads(entry["manifest_json"]) if entry["manifest_json"] else None
        if not manifest:
            raise ValueError("读取器未先声明源快照")
        if kind == "done":
            checkpoint = json.loads(entry["checkpoint_json"])
            if entry["processed"] != sum(s["count"] for s in manifest["shards"]):
                raise ValueError("已保存记录数与源快照计数不一致，不能声明导入完成")
            for shard in manifest["shards"]:
                if checkpoint.get("positions", {}).get(shard["id"], shard["start_after"]) != shard["upper"]:
                    raise ValueError("部分分片尚未持久化到快照边界，不能声明导入完成")
            conn.execute("UPDATE chat_import_scopes SET status='completed' WHERE job_id=? AND scope_id=?", (job_id, scope_id))
            return
        if kind != "batch":
            raise ValueError("未知读取器帧")
        shard = next((s for s in manifest["shards"] if s["id"] == frame["shard"]), None)
        checkpoint = json.loads(entry["checkpoint_json"])
        positions = checkpoint.setdefault("positions", {})
        before = positions.get(frame["shard"], shard["start_after"] if shard else -1)
        after = frame["after"]
        if not shard or type(after) is not int or not before < after <= shard["upper"]:
            raise ValueError("分页检查点不递增或超出源快照")
        messages = frame["messages"]
        if not isinstance(messages, list) or len(messages) > config["batch_size"]:
            raise ValueError("分页大小无效")
        if not messages and after != shard["upper"]:
            raise ValueError("空页只能推进至已验证的分片终点")
        ids = [m["local_id"] for m in messages]
        if ids != sorted(set(ids)) or any(type(i) is not int or not before < i <= after for i in ids):
            raise ValueError("页内消息主键无效")
        added, versions = 0, 0
        now = utc_now()
        for message in messages:
            if not isinstance(message.get("external_id"), str) or not message["external_id"]:
                raise ValueError("消息缺少稳定身份")
            if not isinstance(message.get("text"), str):
                raise ValueError("消息正文必须为文本")
            stamp = message.get("source_time")
            if stamp:
                moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if moment.tzinfo is None:
                    raise ValueError("消息时间缺少时区")
            if config["mode"] == "range" and (not stamp or not datetime.fromisoformat(config["start"]) <= moment < datetime.fromisoformat(config["end"])):
                raise ValueError("读取器返回了范围外或时间未知的消息")
            raw_path = self._raw_path(message)
            digest = hashlib.sha256(_json(message).encode()).hexdigest()
            row = conn.execute("SELECT id FROM source_items WHERE scope_id=? AND external_id=?", (scope_id, message["external_id"])).fetchone()
            item_id = row[0] if row else str(uuid.uuid4())
            if not row:
                conn.execute("INSERT INTO source_items(id,source_id,scope_id,external_id,first_seen_at) VALUES(?,?,?,?,?)",
                             (item_id, scope["source_id"], scope_id, message["external_id"], now))
                added += 1
            version_id = str(uuid.uuid4())
            metadata = {"subject": scope["display_name"], "from": message.get("sender", ""),
                        "source_time": stamp, "time_basis": ("qq_message_time" if config["platform"] == "qq" else "wechat_create_time") if stamp else "unknown",
                        "native_identity": {"local_id": message["local_id"], "shard": frame["shard"], "server_id": str(message.get("server_id", ""))},
                        "message_type": message.get("message_type", "unknown"), "attachments": message.get("attachments", [])}
            if config["platform"] == "qq":
                metadata["native_fingerprint"] = message.get("native_fingerprint")
            inserted = conn.execute("INSERT OR IGNORE INTO item_versions VALUES(?,?,?,?,?,?,?,?)",
                (version_id, item_id, digest, raw_path, "application/vnd.mta.chat+json", message["text"], _json(metadata), now)).rowcount
            if inserted:
                versions += 1
                conn.execute("UPDATE source_items SET latest_version_id=? WHERE id=?", (version_id, item_id))
                conn.execute("INSERT INTO events(id,item_version_id,event_type,created_at) VALUES(?,?,?,?)",
                             (str(uuid.uuid4()), version_id, "new" if not row else "changed", now))
        positions[frame["shard"]] = after
        conn.execute("UPDATE chat_import_scopes SET checkpoint_json=?,processed=processed+?,items=items+?,versions=versions+? WHERE job_id=? AND scope_id=?",
                     (_json(checkpoint), len(messages), added, versions, job_id, scope_id))
        if config["mode"] != "range":
            live = conn.execute("SELECT cursor_json FROM source_cursors WHERE scope_id=? AND cursor_name='chat_native_v1'", (scope_id,)).fetchone()
            value = json.loads(live[0]) if live else {"positions": {}, "anchors": {}}
            old = value.setdefault("positions", {}).get(frame["shard"], -1)
            if after >= old or config["mode"] == "full":
                value["positions"][frame["shard"]] = after
                value.setdefault("anchors", {})[frame["shard"]] = frame["anchor"]
            conn.execute("INSERT INTO source_cursors(source_id,scope_id,cursor_name,cursor_json,updated_at) VALUES(?,?,'chat_native_v1',?,?) ON CONFLICT(source_id,scope_id,cursor_name) DO UPDATE SET cursor_json=excluded.cursor_json,updated_at=excluded.updated_at",
                         (scope["source_id"], scope_id, _json(value), now))
        conn.execute("INSERT INTO scan_runs(id,source_id,scope_id,started_at,finished_at,status,item_count,cursor_before,cursor_after) VALUES(?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), scope["source_id"], scope_id, now, now, "ok", len(messages),
             _json({"chat_job_id": job_id, "mode": config["mode"], "shard": frame["shard"], "after": before}),
             _json({"chat_job_id": job_id, "mode": config["mode"], "shard": frame["shard"], "after": after})))

    def messages(self, scope_id=None, limit=50) -> list[dict]:
        with self.store.connect() as conn:
            return [{**dict(row), "metadata": json.loads(row["metadata_json"])} for row in conn.execute(
                """SELECT i.id,i.external_id,i.scope_id,v.content_text,v.metadata_json,v.raw_path,v.observed_at,sc.display_name
                   FROM source_items i JOIN item_versions v ON v.id=i.latest_version_id
                   JOIN source_scopes sc ON sc.id=i.scope_id JOIN sources s ON s.id=i.source_id
                   WHERE s.kind IN ('wechat_chat','qq_chat') AND (? IS NULL OR i.scope_id=?)
                   ORDER BY json_extract(v.metadata_json,'$.source_time') DESC,i.id LIMIT ?""",
                (scope_id, scope_id, min(max(int(limit), 1), 200)))]


def start_background(imports: ChatImports, job_id: str):
    def work():
        try:
            imports.run(job_id)
        except Exception:
            # Persistent lease/checkpoint makes unexpected process/thread failures recoverable.
            pass
    threading.Thread(target=work, daemon=True, name="chat-import-" + job_id[:8]).start()
