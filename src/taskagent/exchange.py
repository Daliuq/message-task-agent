"""D1: immutable local exchange batches and audited, idempotent result ingestion.

No model calls, mail access or notifications are sent here. Candidate tasks stay
in the review queue until a later resolution stage is explicitly implemented.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import MESSAGES, RESULT, prompt_package, validate
from .store import Store, _json, utc_now

_DDL = """
CREATE TABLE IF NOT EXISTS exchange_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS event_order(
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 event_id TEXT NOT NULL UNIQUE REFERENCES events(id));
CREATE TRIGGER IF NOT EXISTS exchange_event_sequence AFTER INSERT ON events BEGIN
 INSERT OR IGNORE INTO event_order(event_id) VALUES(NEW.id);
END;
INSERT OR IGNORE INTO event_order(event_id)
 SELECT id FROM events WHERE NOT EXISTS
 (SELECT 1 FROM event_order WHERE event_id=events.id) ORDER BY created_at,id;
CREATE TABLE IF NOT EXISTS exchange_channels(
 id TEXT PRIMARY KEY,cursor INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS exchange_batches(
 id TEXT PRIMARY KEY,idempotency_key TEXT NOT NULL UNIQUE,
 config_json TEXT NOT NULL,channel_id TEXT NOT NULL,
 request_id TEXT NOT NULL UNIQUE,messages_json TEXT NOT NULL,request_json TEXT NOT NULL,
 state TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS exchange_members(
 request_id TEXT NOT NULL REFERENCES exchange_batches(request_id),
 event_id TEXT NOT NULL REFERENCES events(id),
 version_id TEXT NOT NULL,PRIMARY KEY(request_id,event_id));
CREATE TABLE IF NOT EXISTS exchange_origins(
 dataset_id TEXT NOT NULL,event_id TEXT NOT NULL,version_id TEXT NOT NULL,
 local_event_id TEXT NOT NULL REFERENCES events(id),fingerprint TEXT NOT NULL,
 PRIMARY KEY(dataset_id,event_id,version_id));
CREATE TABLE IF NOT EXISTS exchange_imports(
 dataset_id TEXT NOT NULL,batch_id TEXT NOT NULL,payload_json TEXT NOT NULL,
 PRIMARY KEY(dataset_id,batch_id));
CREATE TABLE IF NOT EXISTS exchange_previews(
 id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload_json TEXT NOT NULL,
 inspection_json TEXT NOT NULL,applied_json TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS exchange_results(
 id INTEGER PRIMARY KEY AUTOINCREMENT,request_id TEXT NOT NULL,event_id TEXT NOT NULL,
 result_json TEXT NOT NULL,status TEXT NOT NULL,consumption_id TEXT NOT NULL,
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS parsed_candidates(
 id TEXT PRIMARY KEY,consumption_id TEXT NOT NULL REFERENCES event_consumptions(id),
 event_id TEXT NOT NULL REFERENCES events(id),candidate_key TEXT NOT NULL,
 candidate_json TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'needs_review',
 UNIQUE(consumption_id,candidate_key));
"""


def _id() -> str:
    return str(uuid.uuid4())


def _dt(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("时间戳必须带时区")
    return result


def _unique(values, label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} 不允许重复")


def _fingerprint(event: dict) -> str:
    fields = {key: event[key] for key in (
        "origin", "source", "native_identity", "event_type", "source_time", "time_basis", "content")}
    return hashlib.sha256(_json(fields).encode()).hexdigest()


def _content_fingerprint(event: dict) -> str:
    return hashlib.sha256(_json({k: event[k] for k in (
        "source", "native_identity", "event_type", "source_time", "time_basis", "content")}).encode()).hexdigest()


class Exchange:
    def __init__(self, store: Store):
        self.store = store
        with store.connect() as conn:
            installed = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='exchange_meta'").fetchone()
            if not installed:
                # Additive migration; retain an SQLite-consistent recovery copy.
                backup_dir = store.data_dir / "backups"
                backup_dir.mkdir(parents=True, exist_ok=True)
                backup = backup_dir / f"before-exchange-{_id()}.sqlite3"
                with sqlite3.connect(backup) as target:
                    conn.backup(target)
            conn.executescript("BEGIN IMMEDIATE;\n" + _DDL + "\nCOMMIT;")
            conn.execute("INSERT OR IGNORE INTO exchange_meta VALUES('dataset_id',?)", (_id(),))
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(5,?)", (utc_now(),))
            self.dataset_id = conn.execute("SELECT value FROM exchange_meta WHERE key='dataset_id'").fetchone()[0]

    def _event(self, row) -> dict:
        metadata = json.loads(row["metadata_json"])
        if "exchange_event" in metadata:
            event = metadata["exchange_event"]
        else:
            received = metadata.get("received_at") if row["kind"] == "qq_mail" else metadata.get("source_time")
            basis = "imap_internaldate" if row["kind"] == "qq_mail" and received else metadata.get("time_basis", "unknown")
            # Do not invent a receipt time for old imports or use RFC822 Date.
            try:
                source_time = _dt(received).isoformat() if received else None
            except (ValueError, TypeError, AttributeError):
                source_time, basis = None, "unknown"
            event = {
                "origin": {"dataset_id": self.dataset_id, "event_id": row["event_id"],
                           "item_version_id": row["version_id"]},
                "event_type": "message.created" if row["event_type"] == "new" else "message.changed",
                "source": {"kind": {"wechat_chat": "wechat", "qq_chat": "qq"}.get(row["kind"], row["kind"]), "account_key": row["external_key"],
                           "scope_key": row["scope_key"], "display_name": row["display_name"]},
                "native_identity": {"external_id": row["external_id"]},
                "source_time": source_time, "time_basis": basis, "observed_at": row["observed_at"],
                "content": {"subject": str(metadata.get("subject", "")), "text": row["content_text"],
                            "attachments": [
                                {"attachment_id": f"attachment-{i}", "filename": a.get("filename") or "",
                                 "content_type": a.get("content_type") or "application/octet-stream",
                                 "available": False, "included": False}
                                for i, a in enumerate(metadata.get("attachments", []))]},
            }
            if row["kind"] == "qq_mail":
                event["native_identity"]["uidvalidity"] = metadata.get("uidvalidity")
            elif row["kind"] in {"wechat_chat", "qq_chat", "codex_scheduled", "chatgpt_scheduled"}:
                event["native_identity"].update(metadata.get("native_identity", {}))
        return {**event, "event_id": row["event_id"], "event_sequence": row["sequence"],
                "item_id": row["item_id"], "item_version_id": row["version_id"]}

    def scopes(self) -> list[dict]:
        with self.store.connect() as conn:
            return [dict(row) for row in conn.execute(
                """SELECT sc.id,s.kind,s.display_name,sc.scope_key,
                   (SELECT COUNT(*) FROM source_items i WHERE i.scope_id=sc.id) AS item_count
                   FROM source_scopes sc JOIN sources s ON s.id=sc.source_id ORDER BY s.kind,sc.scope_key""")]

    def create_export(self, *, idempotency_key: str, scope_ids: list[str] | None = None,
                      mode: str = "pending", start: str | None = None, end: str | None = None,
                      limit: int = 100) -> dict:
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
            raise ValueError("必须提供稳定的 idempotency_key（1–200 字符）")
        if mode not in {"pending", "incremental", "range"}:
            raise ValueError("mode 必须为 pending / incremental / range")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit 必须为 1–1000")
        if scope_ids is not None and (not isinstance(scope_ids, list) or
                                     not all(isinstance(x, str) for x in scope_ids)):
            raise ValueError("scope_ids 必须为数组")
        scopes = sorted(set(scope_ids or []))
        if mode == "range":
            if not start or not end or _dt(start) >= _dt(end):
                raise ValueError("范围需有效的带时区起止时间，采用 [start,end)")
        elif start is not None or end is not None:
            raise ValueError("非范围导出不接收起止日期")
        config = {"scope_ids": scopes, "mode": mode, "start": start, "end": end, "limit": limit}
        channel = _json({"scope_ids": scopes, "mode": mode})
        with self.store.transaction() as conn:
            old = conn.execute("SELECT * FROM exchange_batches WHERE idempotency_key=?",
                               (idempotency_key,)).fetchone()
            if old:
                if old["config_json"] != _json(config):
                    raise ValueError("同一 idempotency_key 不可用于不同导出配置")
                batch_id = old["id"]
            else:
                preparing = conn.execute(
                    "SELECT id FROM exchange_batches WHERE channel_id=? AND state='preparing'", (channel,)).fetchone()
                if preparing:
                    raise ValueError(f"有未完成文件输出的批次，请恢复：{preparing['id']}")
                existing_scopes = {r[0] for r in conn.execute("SELECT id FROM source_scopes")}
                if set(scopes) - existing_scopes:
                    raise ValueError("scope_ids 包含不存在的范围")
                conn.execute("INSERT OR IGNORE INTO exchange_channels(id) VALUES(?)", (channel,))
                after = conn.execute("SELECT cursor FROM exchange_channels WHERE id=?", (channel,)).fetchone()[0]
                if mode != "incremental":
                    after = 0
                rows = conn.execute(
                    """SELECT o.sequence,e.id AS event_id,e.event_type,e.status,
                       v.id AS version_id,v.content_text,v.metadata_json,v.observed_at,
                       i.id AS item_id,i.external_id,i.scope_id,s.kind,s.external_key,s.display_name,sc.scope_key
                       FROM event_order o JOIN events e ON e.id=o.event_id
                       JOIN item_versions v ON v.id=e.item_version_id JOIN source_items i ON i.id=v.item_id
                       JOIN sources s ON s.id=i.source_id JOIN source_scopes sc ON sc.id=i.scope_id
                       WHERE o.sequence>? ORDER BY o.sequence""", (after,))
                events, through = [], after
                for row in rows:
                    through = row["sequence"]
                    if scopes and row["scope_id"] not in scopes:
                        continue
                    if row["status"] != "pending":
                        continue
                    event = self._event(row)
                    if mode == "range":
                        if not event["source_time"] or not _dt(start) <= _dt(event["source_time"]) < _dt(end):
                            continue
                    events.append(event)
                    if len(events) >= limit:
                        break
                batch_id, request_id = _id(), _id()
                package = {
                    "format": "mta.messages", "version": "1.0", "dataset_id": self.dataset_id,
                    "batch_id": batch_id, "created_at": utc_now(), "timezone": "Asia/Shanghai",
                    "selection": {"mode": mode, "scope_ids": scopes, "after_sequence": after,
                                  "through_sequence": through, "start_inclusive": start, "end_exclusive": end},
                    "event_count": len(events), "events": events,
                }
                validate(package, "messages")
                prompt = prompt_package()
                request = {
                    "format": "mta.analysis-request", "version": "1.0",
                    "dataset_id": self.dataset_id, "batch_id": batch_id, "request_id": request_id,
                    "prompt_id": prompt["prompt_id"], "prompt_version": prompt["prompt_version"],
                    "result_contract_version": "1.0", "reference_time": utc_now(),
                    "timezone": "Asia/Shanghai", "events": events, "prompt": prompt,
                    "dedup_policy": {"automatic_merge_window_hours": 12, "automatic_merge_enabled": False,
                                     "reason": "D1 尚未配置 RAG；候选入复核队列，不自动创建或合并任务"},
                    "rag_context": [{"event_id": e["event_id"], "retrieval_status": "unavailable",
                                     "embedding_profile": None, "candidates": []} for e in events],
                    "output_schema": RESULT,
                    "result_binding": {"dataset_id": self.dataset_id, "batch_id": batch_id,
                                       "request_id": request_id, "prompt_id": prompt["prompt_id"],
                                       "prompt_version": prompt["prompt_version"]},
                }
                conn.execute("INSERT INTO exchange_batches VALUES(?,?,?,?,?,?,?,?,?)",
                             (batch_id, idempotency_key, _json(config), channel, request_id,
                              _json(package), _json(request), "preparing", utc_now()))
                for event in events:
                    conn.execute("INSERT INTO exchange_members VALUES(?,?,?)",
                                 (request_id, event["event_id"], event["item_version_id"]))
                    conn.execute("UPDATE events SET status='awaiting_result',lease_owner=?,lease_until=NULL WHERE id=?",
                                 ("exchange:" + request_id, event["event_id"]))
                    origin = event["origin"]
                    conn.execute("INSERT OR IGNORE INTO exchange_origins VALUES(?,?,?,?,?)",
                                 (origin["dataset_id"], origin["event_id"], origin["item_version_id"],
                                  event["event_id"], _fingerprint(event)))
        return self.finish_export(batch_id)

    def finish_export(self, batch_id: str) -> dict:
        with self.store.connect() as conn:
            batch = conn.execute("SELECT * FROM exchange_batches WHERE id=?", (batch_id,)).fetchone()
            if not batch:
                raise ValueError("批次不存在")
        if batch["state"] == "cancelled":
            return self.batch(batch_id)
        destination = self.store.data_dir / "exchange" / batch_id
        destination.mkdir(parents=True, exist_ok=True)
        for name in ("messages.json", "analysis-request.json", "prompt.json", "prompt.txt",
                     "analysis-result.template.json", "messages.schema.json", "analysis-result.schema.json"):
            payload, _ = self.file(batch_id, name)
            tmp = destination / (name + "." + _id() + ".tmp")
            try:
                with tmp.open("wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp, destination / name)
            finally:
                tmp.unlink(missing_ok=True)
        with self.store.transaction() as conn:
            current = conn.execute("SELECT state FROM exchange_batches WHERE id=?", (batch_id,)).fetchone()[0]
            if current == "preparing":
                package = json.loads(batch["messages_json"])
                if package["selection"]["mode"] == "incremental":
                    conn.execute("UPDATE exchange_channels SET cursor=MAX(cursor,?) WHERE id=?",
                                 (package["selection"]["through_sequence"], batch["channel_id"]))
                conn.execute("UPDATE exchange_batches SET state='active' WHERE id=?", (batch_id,))
        return self.batch(batch_id)

    def file(self, batch_id: str, name: str) -> tuple[bytes, str]:
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM exchange_batches WHERE id=?", (batch_id,)).fetchone()
            if not row:
                raise ValueError("批次不存在")
            request = json.loads(row["request_json"])
            if name == "messages.json":
                value = json.loads(row["messages_json"])
            elif name == "analysis-request.json":
                value = request
            elif name == "prompt.json":
                value = request["prompt"]
            elif name == "prompt.txt":
                return (request["prompt"]["system_template"] + "\n\n请同时上传 analysis-request.json。").encode("utf-8"), "text/plain"
            elif name == "messages.schema.json":
                value = MESSAGES
            elif name == "analysis-result.schema.json":
                value = RESULT
            elif name in {"analysis-result.json", "analysis-result.template.json"}:
                value = {"format": "mta.analysis-result", "version": "1.0",
                         **request["result_binding"], "results": []}
                if name == "analysis-result.template.json":
                    value["results"] = [
                        {"event_id": e["event_id"], "item_version_id": e["item_version_id"], "status": "failed",
                         "facts": [], "candidates": [], "warnings": ["模板占位：尚未解析，请替换为真实结果"]}
                        for e in request["events"]]
                else:
                    rows = conn.execute(
                        """SELECT r.result_json FROM exchange_results r WHERE request_id=?
                           AND r.id=(SELECT MAX(r2.id) FROM exchange_results r2
                           WHERE r2.request_id=r.request_id AND r2.event_id=r.event_id) ORDER BY r.id""",
                        (row["request_id"],)).fetchall()
                    value["results"] = [json.loads(r[0]) for r in rows]
            else:
                raise ValueError("不支持的导出文件名")
        return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"), "application/json"

    def batch(self, batch_id: str) -> dict:
        with self.store.connect() as conn:
            batch = conn.execute("SELECT * FROM exchange_batches WHERE id=?", (batch_id,)).fetchone()
            if not batch:
                raise ValueError("批次不存在")
            package = json.loads(batch["messages_json"])
            results = conn.execute(
                """SELECT r.* FROM exchange_results r WHERE request_id=?
                   AND r.id=(SELECT MAX(r2.id) FROM exchange_results r2
                   WHERE r2.request_id=r.request_id AND r2.event_id=r.event_id)""",
                (batch["request_id"],)).fetchall()
            succeeded = sum(r["status"] != "failed" for r in results)
            review = sum(r["status"] == "needs_review" for r in results)
            failed = sum(r["status"] == "failed" for r in results)
            return {"id": batch_id, "request_id": batch["request_id"], "state": batch["state"],
                    "created_at": batch["created_at"], "selection": package["selection"],
                    "event_count": package["event_count"], "parsed_count": succeeded,
                    "review_event_count": review, "failed_count": failed,
                    "missing_count": package["event_count"] - len(results),
                    "all_parsed": succeeded == package["event_count"],
                    "directory": str(self.store.data_dir / "exchange" / batch_id)}

    def batches(self) -> list[dict]:
        with self.store.connect() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM exchange_batches ORDER BY created_at DESC,rowid DESC LIMIT 100")]
        return [self.batch(value) for value in ids]

    def cancel(self, batch_id: str) -> dict:
        with self.store.transaction() as conn:
            row = conn.execute("SELECT request_id FROM exchange_batches WHERE id=?", (batch_id,)).fetchone()
            if not row:
                raise ValueError("批次不存在")
            conn.execute("UPDATE events SET status='pending',lease_owner=NULL,lease_until=NULL WHERE status='awaiting_result' AND lease_owner=?",
                         ("exchange:" + row["request_id"],))
            conn.execute("UPDATE exchange_batches SET state='cancelled' WHERE id=?", (batch_id,))
        return self.batch(batch_id)

    def _inspect_messages(self, conn, payload: dict) -> dict:
        validate(payload, "messages")
        if payload["event_count"] != len(payload["events"]):
            raise ValueError("event_count 与 events 长度不一致")
        _unique([e["event_id"] for e in payload["events"]], "event_id")
        _unique([e["event_sequence"] for e in payload["events"]], "event_sequence")
        _unique([_json(e["origin"]) for e in payload["events"]], "origin")
        selection = payload["selection"]
        if selection["mode"] == "range":
            if not selection["start_inclusive"] or not selection["end_exclusive"]:
                raise ValueError("range 缺少起止时间")
            if _dt(selection["start_inclusive"]) >= _dt(selection["end_exclusive"]):
                raise ValueError("range 结束时间必须晚于开始")
        if selection["after_sequence"] > selection["through_sequence"]:
            raise ValueError("序号范围倒置")
        for event in payload["events"]:
            if not selection["after_sequence"] < event["event_sequence"] <= selection["through_sequence"]:
                raise ValueError("event_sequence 超出导出区间")
        receipt = conn.execute("SELECT payload_json FROM exchange_imports WHERE dataset_id=? AND batch_id=?",
                               (payload["dataset_id"], payload["batch_id"])).fetchone()
        if receipt and receipt[0] != _json(payload):
            raise ValueError("同一消息批次 ID 对应不同内容")
        actions = []
        seen_native = set()
        for event in payload["events"]:
            origin = event["origin"]
            row = conn.execute("SELECT * FROM exchange_origins WHERE dataset_id=? AND event_id=? AND version_id=?",
                               (origin["dataset_id"], origin["event_id"], origin["item_version_id"])).fetchone()
            if row and row["fingerprint"] != _fingerprint(event):
                raise ValueError(f"origin 版本内容冲突：{event['event_id']}")
            native = (origin["dataset_id"], event["source"]["kind"], event["source"]["account_key"],
                      event["source"]["scope_key"], _json(event["native_identity"]), _content_fingerprint(event))
            existing_native = conn.execute(
                """SELECT e.id FROM events e JOIN item_versions v ON v.id=e.item_version_id
                   JOIN source_items i ON i.id=v.item_id JOIN sources s ON s.id=i.source_id
                   JOIN source_scopes sc ON sc.id=i.scope_id
                   WHERE s.kind=? AND s.external_key=? AND sc.scope_key=? AND i.external_id=? AND v.content_hash=?""",
                ({"wechat": "wechat_chat", "qq": "qq_chat"}.get(native[1], native[1]), "exchange:" + _json([native[0], native[2]]), native[3], native[4], native[5])).fetchone()
            actions.append("already_present" if row else "alias" if existing_native or native in seen_native else "insert")
            seen_native.add(native)
        return {"actions": actions, "new_events": actions.count("insert"),
                "already_present": actions.count("already_present") + actions.count("alias"), "event_count": len(actions)}

    def _inspect_results(self, conn, payload: dict) -> dict:
        validate(payload, "results")
        batch = conn.execute("SELECT * FROM exchange_batches WHERE request_id=?", (payload["request_id"],)).fetchone()
        if not batch or batch["state"] != "active":
            raise ValueError("请求不存在、文件尚未输出完成或请求已取消")
        request = json.loads(batch["request_json"])
        for field in ("dataset_id", "batch_id", "request_id", "prompt_id", "prompt_version"):
            if payload[field] != request[field]:
                raise ValueError(f"{field} 与请求快照不符")
        _unique([r["event_id"] for r in payload["results"]], "结果 event_id")
        events = {e["event_id"]: e for e in request["events"]}
        actions = []
        for result in payload["results"]:
            eid = result["event_id"]
            event = events.get(eid)
            if not event or event["item_version_id"] != result["item_version_id"]:
                raise ValueError(f"未知事件或消息版本不匹配：{eid}")
            prior = conn.execute("SELECT * FROM exchange_results WHERE request_id=? AND event_id=? ORDER BY id DESC LIMIT 1",
                                 (payload["request_id"], eid)).fetchone()
            if prior and prior["result_json"] == _json(result):
                actions.append("already_applied")
                continue
            if prior and prior["status"] != "failed":
                raise ValueError("该事件已有不同的成功结果；D1 不支持覆盖，请保留原结果并等待更正流程")
            live = conn.execute("SELECT status,lease_owner,item_version_id FROM events WHERE id=?", (eid,)).fetchone()
            if not live or live["status"] != "awaiting_result" or live["lease_owner"] != "exchange:" + payload["request_id"]:
                raise ValueError(f"事件处理权已变化，拒绝迟到结果：{eid}")
            if live["item_version_id"] != result["item_version_id"]:
                raise ValueError("消息版本已变化")
            _unique([f["fact_id"] for f in result["facts"]], "fact_id")
            _unique([c["candidate_id"] for c in result["candidates"]], "candidate_id")
            fact_ids = {f["fact_id"] for f in result["facts"]}
            source_text = event["content"]["subject"] + "\n" + event["content"]["text"]
            for fact in result["facts"]:
                if fact["quote"] and fact["quote"] not in source_text:
                    raise ValueError(f"事实证据不在原文中：{fact['fact_id']}")
                if fact["type"] in {"deadline", "event_time"}:
                    _dt(fact["value"])
            for candidate in result["candidates"]:
                if set(candidate["evidence_fact_ids"]) - fact_ids:
                    raise ValueError("候选引用未知事实")
                if not candidate["evidence_quote"] or candidate["evidence_quote"] not in source_text:
                    raise ValueError("候选 evidence_quote 必须引用消息原文")
                if not candidate["title"].strip():
                    raise ValueError("候选标题不能为空白")
                if candidate["matched_task_id"] is not None or candidate["suggested_action"] in {"link", "update"}:
                    raise ValueError("当前请求没有 RAG 候选，不允许编造已有任务关联")
                begin, end = candidate["fixed_start_at"], candidate["fixed_end_at"]
                if (begin is None) != (end is None) or (begin is not None and _dt(end) <= _dt(begin)):
                    raise ValueError("固定时间必须成对且结束晚于开始")
                if (candidate["estimated_minutes"] is None) != (candidate["estimate_basis"] == "unknown"):
                    raise ValueError("estimated_minutes 与 estimate_basis 不一致")
            if result["status"] == "failed" and (result["candidates"] or result["facts"]):
                raise ValueError("failed 结果不能同时包含有效事实或任务候选")
            if not result["candidates"] and not result["warnings"]:
                raise ValueError("无任务／处理失败时须在 warnings 说明原因")
            actions.append("failed" if result["status"] == "failed" else
                           "needs_review" if result["candidates"] or result["status"] == "needs_review" else "parsed")
        returned_ids = {r["event_id"] for r in payload["results"]}
        return {"actions": actions, "returned_count": len(actions),
                "not_in_this_file": sorted(set(events) - returned_ids),
                "already_applied": actions.count("already_applied"),
                "parsed": actions.count("parsed"), "needs_review": actions.count("needs_review"),
                "failed": actions.count("failed"), "tasks_created": 0,
                "note": "D1：仅保存解析与待确认候选；未执行 RAG、未创建正式任务、未发送邮件"}

    def preview(self, kind: str, payload: dict) -> dict:
        if kind not in {"messages", "results"}:
            raise ValueError("不支持的导入类型")
        inspector = self._inspect_messages if kind == "messages" else self._inspect_results
        with self.store.transaction() as conn:
            summary = inspector(conn, payload)
            preview_id = _id()
            conn.execute("INSERT INTO exchange_previews VALUES(?,?,?,?,?,?)",
                         (preview_id, kind, _json(payload), _json(summary), None, utc_now()))
        return {"preview_id": preview_id, "kind": kind, **summary}

    def apply(self, preview_id: str) -> dict:
        with self.store.transaction() as conn:
            preview = conn.execute("SELECT * FROM exchange_previews WHERE id=?", (preview_id,)).fetchone()
            if not preview:
                raise ValueError("预检记录不存在")
            if preview["applied_json"]:
                return {**json.loads(preview["applied_json"]), "replayed": True}
            payload = json.loads(preview["payload_json"])
            inspector = self._inspect_messages if preview["kind"] == "messages" else self._inspect_results
            summary = inspector(conn, payload)
            if _json(summary) != preview["inspection_json"]:
                raise ValueError("预检已过期：本地状态已变化，请重新预检")
            if preview["kind"] == "messages":
                self._apply_messages(conn, payload, summary["actions"])
            else:
                self._apply_results(conn, payload, summary["actions"])
            report = {"preview_id": preview_id, "kind": preview["kind"], **summary, "replayed": False}
            conn.execute("UPDATE exchange_previews SET applied_json=? WHERE id=?", (_json(report), preview_id))
        return report

    def _apply_messages(self, conn, payload: dict, actions: list[str]) -> None:
        for event, action in zip(payload["events"], actions):
            if action == "already_present":
                continue
            source, origin = event["source"], event["origin"]
            storage_kind = {"wechat": "wechat_chat", "qq": "qq_chat"}.get(source["kind"], source["kind"])
            # No implicit account merge: imported sources are an isolated, disabled namespace.
            account = "exchange:" + _json([origin["dataset_id"], source["account_key"]])
            existed = conn.execute("SELECT id FROM sources WHERE kind=? AND external_key=?",
                                   (storage_kind, account)).fetchone()
            sid, scope_id = self.store._source_scope(conn, storage_kind, account,
                                                     source["display_name"] + "（文件导入）", source["scope_key"])
            if not existed:
                conn.execute("UPDATE sources SET enabled=0 WHERE id=?", (sid,))
            # Imported namespaces never become live subscriptions implicitly.
            conn.execute("UPDATE source_scopes SET enabled=0,full_scan_enabled=0 WHERE id=?", (scope_id,))
            external_id = _json(event["native_identity"])
            item = conn.execute("SELECT id FROM source_items WHERE source_id=? AND scope_id=? AND external_id=?",
                                (sid, scope_id, external_id)).fetchone()
            item_id = item["id"] if item else _id()
            if not item:
                conn.execute("INSERT INTO source_items(id,source_id,scope_id,external_id,first_seen_at) VALUES(?,?,?,?,?)",
                             (item_id, sid, scope_id, external_id, utc_now()))
            fingerprint = _content_fingerprint(event)
            version = conn.execute("SELECT id FROM item_versions WHERE item_id=? AND content_hash=?",
                                   (item_id, fingerprint)).fetchone()
            version_id = version["id"] if version else _id()
            if not version:
                metadata = {"exchange_event": event, "subject": event["content"]["subject"],
                            "received_at": event["source_time"], "folder": source["scope_key"]}
                conn.execute("INSERT INTO item_versions VALUES(?,?,?,?,?,?,?,?)",
                             (version_id, item_id, fingerprint, None, "application/vnd.mta.message+json",
                              event["content"]["text"], _json(metadata), event["observed_at"]))
                conn.execute("UPDATE source_items SET latest_version_id=? WHERE id=?", (version_id, item_id))
            event_type = "new" if event["event_type"] == "message.created" else "changed"
            local = conn.execute("SELECT id FROM events WHERE item_version_id=? AND event_type=?",
                                 (version_id, event_type)).fetchone()
            event_id = local["id"] if local else _id()
            if not local:
                conn.execute("INSERT INTO events(id,item_version_id,event_type,created_at) VALUES(?,?,?,?)",
                             (event_id, version_id, event_type, utc_now()))
            conn.execute("INSERT INTO exchange_origins VALUES(?,?,?,?,?)",
                         (origin["dataset_id"], origin["event_id"], origin["item_version_id"], event_id, _fingerprint(event)))
        conn.execute("INSERT OR IGNORE INTO exchange_imports VALUES(?,?,?)",
                     (payload["dataset_id"], payload["batch_id"], _json(payload)))

    def _apply_results(self, conn, payload: dict, actions: list[str]) -> None:
        request = conn.execute("SELECT request_json FROM exchange_batches WHERE request_id=?",
                               (payload["request_id"],)).fetchone()[0]
        for result, action in zip(payload["results"], actions):
            if action == "already_applied":
                continue
            eid, cid, now = result["event_id"], _id(), utc_now()
            attempt = conn.execute("SELECT attempts FROM events WHERE id=?", (eid,)).fetchone()[0] + 1
            consumption_status = "failed" if action == "failed" else "succeeded"
            error = "\n".join(result["warnings"]) if action == "failed" else None
            conn.execute(
                """INSERT INTO event_consumptions(id,event_id,consumer_name,attempt_no,status,input_json,
                   output_json,error,started_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (cid, eid, "exchange-v1", attempt, consumption_status, request, _json(result), error, now, now))
            conn.execute("INSERT INTO agent_runs VALUES(?,?,?,?,?,?,?)",
                         (_id(), cid, "external-json-import", payload["prompt_version"],
                          consumption_status, _json(result), now))
            if action == "failed":
                conn.execute("UPDATE events SET attempts=?,last_error=? WHERE id=?", (attempt, error, eid))
            else:
                conn.execute("UPDATE events SET status='succeeded',attempts=?,lease_owner=NULL,lease_until=NULL,last_error=NULL WHERE id=?",
                             (attempt, eid))
                for candidate in result["candidates"]:
                    conn.execute("INSERT INTO parsed_candidates(id,consumption_id,event_id,candidate_key,candidate_json) VALUES(?,?,?,?,?)",
                                 (_id(), cid, eid, candidate["candidate_id"], _json(candidate)))
            conn.execute("INSERT INTO exchange_results(request_id,event_id,result_json,status,consumption_id,created_at) VALUES(?,?,?,?,?,?)",
                         (payload["request_id"], eid, _json(result), action, cid, now))

    def candidates(self) -> list[dict]:
        with self.store.connect() as conn:
            return [{**dict(r), "candidate": json.loads(r["candidate_json"])}
                    for r in conn.execute("SELECT * FROM parsed_candidates ORDER BY rowid DESC LIMIT 200")]
