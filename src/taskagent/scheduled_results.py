"""Read scheduled outputs, not task definitions or private authentication stores.

Codex is a read-only local adapter. ChatGPT uses an explicit result inbox until a
web/desktop reader is connected; an empty inbox is never reported as a cloud scan.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .store import Store, _json, utc_now

KINDS = {"codex": "codex_scheduled", "chatgpt": "chatgpt_scheduled"}
_DDL = """
CREATE TABLE IF NOT EXISTS scheduled_scan_jobs (
 id TEXT PRIMARY KEY, status TEXT NOT NULL, started_at TEXT NOT NULL,
 finished_at TEXT, lease_until TEXT NOT NULL, result_json TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_scheduled_scan_running
 ON scheduled_scan_jobs(status) WHERE status='running';
"""


def _stamp(value: Any) -> str:
    if isinstance(value, bool):
        raise ValueError("时间不能是布尔值")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 10**11 else value, timezone.utc).isoformat()
    if not isinstance(value, str):
        raise ValueError("执行时间须为带时区的 ISO 时间")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("执行时间缺少时区")
    return result.astimezone(timezone.utc).isoformat()


def validate_packet(packet: dict) -> list[dict]:
    if not isinstance(packet, dict) or set(packet) != {"format", "version", "results"}:
        raise ValueError("需要 mta.scheduled-results 格式，字段为 format/version/results")
    if packet["format"] != "mta.scheduled-results" or packet["version"] != "1.0":
        raise ValueError("不支持的定时结果格式或版本")
    records = packet["results"]
    if not isinstance(records, list) or len(records) > 1000:
        raise ValueError("每包最多 1000 条执行结果")
    required = {"provider", "account_key", "task_id", "run_id", "title", "source_time", "run_status", "text", "completeness"}
    out = []
    identities = {}
    for value in records:
        if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {"thread_id"}:
            raise ValueError("执行结果字段缺失或有未知字段")
        r = dict(value)
        if r["provider"] not in KINDS or r["run_status"] not in {"completed", "failed", "cancelled"}:
            raise ValueError("来源或执行状态无效；只接收已结束的运行")
        if r["completeness"] not in {"full", "summary"}:
            raise ValueError("completeness 须为 full 或 summary")
        for field in ("account_key", "task_id", "run_id", "title"):
            if not isinstance(r[field], str) or not r[field].strip() or len(r[field]) > 500:
                raise ValueError(f"{field} 须为 1–500 字符")
        if not isinstance(r["text"], str) or not r["text"].strip() or len(r["text"]) > 1_000_000:
            raise ValueError("结果正文须为 1–1000000 字符")
        if "thread_id" in r and (not isinstance(r["thread_id"], str) or len(r["thread_id"]) > 500):
            raise ValueError("thread_id 无效")
        r["source_time"] = _stamp(r["source_time"])
        key = tuple(r[k] for k in ("provider", "account_key", "task_id", "run_id"))
        if key in identities:
            if identities[key] != r:
                raise ValueError("同一包内同一运行出现互相冲突的结果")
            continue
        identities[key] = r
        out.append(r)
    return out


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def read_codex(home: Path) -> tuple[list[dict], list[str]]:
    """Only registered cron/heartbeat threads; never collect arbitrary chat turns."""
    warnings: list[str] = []
    db = home / "sqlite" / "codex-dev.db"
    with closing(_ro(db)) as conn:
        automations = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM automations")}
        runs = [dict(r) for r in conn.execute("SELECT * FROM automation_runs WHERE thread_id NOT LIKE 'pending:%'")]
    threads: dict[str, dict] = {}
    for r in runs:
        threads.setdefault(r["thread_id"], {"cron": r, "heartbeat": []})
    for a in automations.values():
        if a.get("kind") == "heartbeat" and a.get("target_thread_id"):
            threads.setdefault(a["target_thread_id"], {"cron": None, "heartbeat": []})["heartbeat"].append(a["id"])
    with closing(_ro(home / "state_5.sqlite")) as conn:
        paths = {r["id"]: r["rollout_path"] for r in conn.execute("SELECT id,rollout_path FROM threads") if r["id"] in threads}
    results: list[dict] = []
    for thread_id, entry in threads.items():
        cron = entry["cron"]
        path_text = paths.get(thread_id)
        # Extended Windows paths and ordinary paths must compare as the same root.
        if path_text and path_text.startswith("\\\\?\\"):
            path_text = path_text[4:]
            if path_text.startswith("UNC\\"):
                path_text = "\\\\" + path_text[4:]
        path = Path(path_text) if path_text else None
        if path and not any(path.resolve().is_relative_to((home / d).resolve()) for d in ("sessions", "archived_sessions")):
            warnings.append(f"{thread_id}：运行记录路径不在会话目录内")
            continue
        if not path or not path.is_file():
            if cron and (cron.get("archived_assistant_message") or cron.get("inbox_summary")):
                full = bool(cron.get("archived_assistant_message"))
                results.append({"provider": "codex", "account_key": "local", "task_id": cron["automation_id"],
                    "run_id": thread_id, "thread_id": thread_id,
                    "title": automations.get(cron["automation_id"], {}).get("name") or cron.get("thread_title") or cron["automation_id"],
                    "source_time": _stamp(cron["created_at"]), "run_status": "completed",
                    "text": cron.get("archived_assistant_message") or cron["inbox_summary"],
                    "completeness": "full" if full else "summary"})
                if not full:
                    warnings.append(f"{thread_id}：仅有通知摘要，没有完整正文，未交给解析器")
            else:
                warnings.append(f"{thread_id}：未找到本机执行正文")
            continue
        current: dict | None = None
        cron_seen = False
        try:
            with path.open("r", encoding="utf-8") as stream:
                while True:
                    line = stream.readline()
                    if not line:
                        break
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        # A live writer may not have finished its last record. Retry it next scan.
                        if not line.endswith("\n"):
                            warnings.append(f"{thread_id}：末条记录仍在写入，下次重读")
                            break
                        raise ValueError("运行记录存在损坏的 JSON 行")
                    p = item.get("payload", {})
                    if not isinstance(p, dict):
                        continue
                    if item.get("type") == "event_msg" and p.get("type") == "task_started":
                        if current:
                            warnings.append(f"{thread_id}/{current['turn_id']}：未见运行结束记录，尚不作为完整结果保存")
                        trigger = (p.get("turn_attribution") or {}).get("turn_trigger", "") or ""
                        current = None
                        if trigger in {"automation_cron_scheduled", "automation_cron_manual", "automation_heartbeat_scheduled", "automation_heartbeat_manual"}:
                            task_id = cron["automation_id"] if cron else (entry["heartbeat"][0] if len(entry["heartbeat"]) == 1 else None)
                            current = {"turn_id": p.get("turn_id"), "task_id": task_id,
                                       "time": item.get("timestamp"), "text": "",
                                       "primary_cron": bool(cron and not cron_seen)}
                            if cron:
                                cron_seen = True
                    elif current and item.get("type") == "response_item" and p.get("type") == "message":
                        if p.get("role") == "user":
                            body = "\n".join(c.get("text", "") for c in p.get("content", []) if isinstance(c, dict))
                            match = re.search(r"<automation_id>\s*([^<]+?)\s*</automation_id>", body)
                            if match and match[1] in entry["heartbeat"]:
                                current["task_id"] = match[1]
                        elif p.get("role") == "assistant" and p.get("channel") == "final":
                            current["text"] = "\n".join(c.get("text", "") for c in p.get("content", []) if isinstance(c, dict))
                    elif current and item.get("type") == "event_msg" and p.get("type") in {"task_complete", "turn_aborted"}:
                        if p.get("turn_id") and p["turn_id"] != current["turn_id"]:
                            continue
                        text = current["text"] or p.get("last_agent_message") or ""
                        task_id = current["task_id"]
                        if task_id and current["turn_id"] and text.strip():
                            results.append({"provider": "codex", "account_key": "local", "task_id": task_id,
                                "run_id": thread_id if current["primary_cron"] else current["turn_id"], "thread_id": thread_id,
                                "title": automations.get(task_id, {}).get("name") or task_id,
                                "source_time": _stamp(current["time"]),
                                "run_status": "completed" if p["type"] == "task_complete" else "cancelled",
                                "text": text, "completeness": "full"})
                        else:
                            warnings.append(f"{thread_id}/{current['turn_id']}：缺少完整结果或稳定任务归属，未生成解析事件")
                        current = None
                if current:
                    warnings.append(f"{thread_id}/{current['turn_id']}：运行尚未结束，下次重新核对")
        except (OSError, ValueError) as exc:
            warnings.append(f"{thread_id}：读取失败 {type(exc).__name__}：{exc}")
    return results, warnings


class ScheduledResults:
    def __init__(self, store: Store, *, codex_home: Path | None = None):
        self.store = store
        self.home = codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        self.inbox = store.data_dir / "inbox" / "scheduled" / "chatgpt"
        with store.connect() as conn:
            conn.executescript(_DDL)
            conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(8,?)", (utc_now(),))

    def ingest(self, packet: dict) -> dict:
        records = validate_packet(packet)  # validate the whole packet before any mutation
        counts = {"items": 0, "versions": 0, "events": 0, "unchanged": 0, "ignored_summary": 0}
        with self.store.transaction() as conn:
            for r in records:
                sid, scope = self.store._source_scope(conn, KINDS[r["provider"]], r["account_key"],
                    "Codex 定时任务" if r["provider"] == "codex" else "ChatGPT 定时任务", r["task_id"])
                conn.execute("UPDATE source_scopes SET display_name=?,updated_at=? WHERE id=?", (r["title"], utc_now(), scope))
                existing = conn.execute("SELECT i.id,i.latest_version_id,v.metadata_json FROM source_items i LEFT JOIN item_versions v ON v.id=i.latest_version_id WHERE i.source_id=? AND i.scope_id=? AND i.external_id=?", (sid, scope, r["run_id"])).fetchone()
                if existing and json.loads(existing["metadata_json"])["completeness"] == "full" and r["completeness"] == "summary":
                    counts["ignored_summary"] += 1
                    continue  # never downgrade a complete output to a later notification summary
                canonical = _json(r).encode("utf-8")
                digest = hashlib.sha256(canonical).hexdigest()
                relative = Path("raw/scheduled") / digest[:2] / (digest + ".json")
                target = self.store.data_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    temp = target.with_suffix("." + uuid.uuid4().hex + ".tmp")
                    try:
                        with temp.open("xb") as f:
                            f.write(canonical); f.flush(); os.fsync(f.fileno())
                        os.replace(temp, target)
                    finally:
                        temp.unlink(missing_ok=True)
                item_id = existing["id"] if existing else str(uuid.uuid4())
                if not existing:
                    conn.execute("INSERT INTO source_items(id,source_id,scope_id,external_id,first_seen_at) VALUES(?,?,?,?,?)", (item_id, sid, scope, r["run_id"], utc_now()))
                    counts["items"] += 1
                prior = conn.execute("SELECT id FROM item_versions WHERE item_id=? AND content_hash=?", (item_id, digest)).fetchone()
                if prior:
                    counts["unchanged"] += 1
                    continue  # an old replay must not move latest_version backwards
                vid = str(uuid.uuid4())
                meta = {k: v for k, v in r.items() if k != "text"}
                meta.update(subject=r["title"], time_basis="scheduled_run_started_at", from_source=r["provider"],
                            native_identity={"task_id": r["task_id"], "run_id": r["run_id"]})
                conn.execute("INSERT INTO item_versions(id,item_id,content_hash,raw_path,content_type,content_text,metadata_json,observed_at) VALUES(?,?,?,?,?,?,?,?)", (vid, item_id, digest, relative.as_posix(), "application/json", r["text"], _json(meta), utc_now()))
                conn.execute("UPDATE source_items SET latest_version_id=? WHERE id=?", (vid, item_id))
                counts["versions"] += 1
                if r["completeness"] == "full":
                    conn.execute("INSERT INTO events(id,item_version_id,event_type,created_at) VALUES(?,?,?,?)", (str(uuid.uuid4()), vid, "changed" if existing else "new", utc_now()))
                    counts["events"] += 1
        return counts

    def import_packet(self, packet: dict) -> dict:
        # Durable inbox first; a failed process can safely re-read it next scan.
        validate_packet(packet)
        raw = _json(packet).encode("utf-8")
        inbox = self.store.data_dir / "inbox/scheduled" / "imports"
        inbox.mkdir(parents=True, exist_ok=True)
        target = inbox / (hashlib.sha256(raw).hexdigest() + ".json")
        temp = target.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            temp.write_bytes(raw); os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        return self.ingest(packet)

    def start(self) -> dict:
        now = utc_now()
        with self.store.transaction() as conn:
            conn.execute("UPDATE scheduled_scan_jobs SET status='interrupted',finished_at=? WHERE status='running' AND lease_until<?", (now, now))
            if conn.execute("SELECT 1 FROM scheduled_scan_jobs WHERE status='running'").fetchone():
                raise ValueError("已有定时结果扫描正在运行")
            job = {"id": str(uuid.uuid4()), "status": "running", "started_at": now}
            conn.execute("INSERT INTO scheduled_scan_jobs(id,status,started_at,lease_until) VALUES(?,?,?,?)", (job["id"], "running", now, (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()))
        return job

    def run(self, job_id: str) -> dict:
        sources = []
        try:
            records, warnings = read_codex(self.home)
            counts = {"items": 0, "versions": 0, "events": 0, "unchanged": 0}
            for start in range(0, len(records), 1000):
                saved = self.ingest({"format": "mta.scheduled-results", "version": "1.0", "results": records[start:start + 1000]})
                for k in counts:
                    counts[k] += saved[k]
            sources.append({"provider": "codex", "status": "partial" if warnings else "completed", "read": len(records), **counts, "warnings": warnings})
        except Exception as exc:
            sources.append({"provider": "codex", "status": "failed", "read": 0, "error": f"{type(exc).__name__}: {exc}"})
        counts = {"read": 0, "files": 0, "items": 0, "versions": 0, "events": 0, "unchanged": 0}
        errors = []
        paths = list(self.inbox.glob("*.json")) + list((self.store.data_dir / "inbox/scheduled/imports").glob("*.json"))
        for file in sorted(paths):
            try:
                if file.stat().st_size > 8 * 1024 * 1024:
                    raise ValueError("结果包超过 8 MiB")
                packet = json.loads(file.read_text(encoding="utf-8-sig"))
                records = [r for r in validate_packet(packet) if r["provider"] == "chatgpt"]
                # Imports may contain both providers. Preserve original provider identities.
                result = self.ingest({"format": "mta.scheduled-results", "version": "1.0", "results": records})
                counts["files"] += 1; counts["read"] += len(records)
                for k in ("items", "versions", "events", "unchanged"):
                    counts[k] += result[k]
            except Exception as exc:
                errors.append(f"{file.name}: {type(exc).__name__}: {exc}")
        sources.append({"provider": "chatgpt", "status": "inbox_only", **counts, "warnings": errors,
                        "message": "仅扫描本地结果收件箱；未连接 ChatGPT 网页/客户端，未读取云端任务或完整历史。"})
        # A cloud reader is not connected: never call a two-provider scan fully completed.
        result = {"id": job_id, "status": "partial", "sources": sources, "finished_at": utc_now()}
        with self.store.transaction() as conn:
            conn.execute("UPDATE scheduled_scan_jobs SET status=?,finished_at=?,result_json=? WHERE id=? AND status='running'", (result["status"], result["finished_at"], _json(result), job_id))
        return result

    def status(self) -> dict:
        with self.store.transaction() as conn:
            conn.execute("UPDATE scheduled_scan_jobs SET status='interrupted',finished_at=? WHERE status='running' AND lease_until<?", (utc_now(), utc_now()))
        with self.store.connect() as conn:
            job = conn.execute("SELECT * FROM scheduled_scan_jobs ORDER BY rowid DESC LIMIT 1").fetchone()
            counts = {r[0]: r[1] for r in conn.execute("SELECT s.kind,COUNT(*) FROM source_items i JOIN sources s ON s.id=i.source_id WHERE s.kind IN ('codex_scheduled','chatgpt_scheduled') GROUP BY s.kind")}
        return {"codex_home": str(self.home), "chatgpt_inbox": str(self.inbox),
                "cloud_connected": False, "counts": {k: counts.get(v, 0) for k, v in KINDS.items()},
                "latest_job": {k: v for k, v in dict(job).items() if k != "result_json"} if job else None,
                "latest_result": json.loads(job["result_json"]) if job and job["result_json"] else None}

    def messages(self, limit: int = 100) -> list[dict]:
        with self.store.connect() as conn:
            rows = conn.execute("""SELECT i.id,i.external_id,v.id AS version_id,v.content_text,v.metadata_json,v.observed_at,
                (SELECT e.status FROM events e WHERE e.item_version_id=v.id LIMIT 1) AS event_status
                FROM source_items i JOIN sources s ON s.id=i.source_id JOIN item_versions v ON v.id=i.latest_version_id
                WHERE s.kind IN ('codex_scheduled','chatgpt_scheduled')
                ORDER BY json_extract(v.metadata_json,'$.source_time') DESC,i.id LIMIT ?""", (min(500, max(1, limit)),)).fetchall()
        return [{**{k: r[k] for k in r.keys() if k != "metadata_json"}, "metadata": json.loads(r["metadata_json"])} for r in rows]


def start_background(reader: ScheduledResults, job_id: str) -> None:
    threading.Thread(target=reader.run, args=(job_id,), name="scheduled-results-scan", daemon=True).start()
