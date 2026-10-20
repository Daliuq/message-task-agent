from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    external_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(kind, external_key)
);
CREATE TABLE IF NOT EXISTS source_scopes (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    scope_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    full_scan_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT '',
    UNIQUE(source_id, scope_key)
);
CREATE TABLE IF NOT EXISTS scan_runs (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    scope_id TEXT NOT NULL REFERENCES source_scopes(id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    item_count INTEGER NOT NULL DEFAULT 0,
    cursor_before TEXT,
    cursor_after TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS source_cursors (
    source_id TEXT NOT NULL REFERENCES sources(id),
    scope_id TEXT NOT NULL REFERENCES source_scopes(id),
    cursor_name TEXT NOT NULL,
    cursor_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(source_id, scope_id, cursor_name)
);
CREATE TABLE IF NOT EXISTS source_items (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    scope_id TEXT NOT NULL REFERENCES source_scopes(id),
    external_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    latest_version_id TEXT,
    deleted_at TEXT,
    UNIQUE(source_id, scope_id, external_id)
);
CREATE TABLE IF NOT EXISTS item_versions (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL,
    raw_path TEXT,
    content_type TEXT NOT NULL,
    content_text TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    UNIQUE(item_id, content_hash)
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    item_version_id TEXT NOT NULL REFERENCES item_versions(id),
    event_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_until TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(item_version_id, event_type)
);
CREATE INDEX IF NOT EXISTS ix_events_queue ON events(status, lease_until, created_at);
CREATE TABLE IF NOT EXISTS event_consumptions (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id),
    consumer_name TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    status TEXT NOT NULL,
    input_json TEXT NOT NULL,
    output_json TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE(event_id, consumer_name, attempt_no)
);
CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    consumption_id TEXT NOT NULL UNIQUE REFERENCES event_consumptions(id),
    agent_name TEXT NOT NULL,
    agent_version TEXT NOT NULL,
    status TEXT NOT NULL,
    output_json TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    identity_key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    due_at TEXT,
    estimated_minutes INTEGER,
    fixed_start_at TEXT,
    fixed_end_at TEXT,
    priority INTEGER NOT NULL DEFAULT 3,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_revisions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    changed_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_occurrences (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    event_id TEXT NOT NULL REFERENCES events(id),
    source_item_id TEXT NOT NULL REFERENCES source_items(id),
    occurrence_type TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    UNIQUE(task_id, event_id)
);
CREATE TABLE IF NOT EXISTS plan_runs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    objective_json TEXT NOT NULL,
    explanation TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS plan_items (
    id TEXT PRIMARY KEY,
    plan_run_id TEXT NOT NULL REFERENCES plan_runs(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    starts_at TEXT,
    ends_at TEXT,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS notification_outbox (
    id TEXT PRIMARY KEY,
    notification_type TEXT NOT NULL,
    task_id TEXT REFERENCES tasks(id),
    dedupe_key TEXT NOT NULL UNIQUE,
    recipient TEXT,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    created_at TEXT NOT NULL,
    sent_at TEXT,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS delivery_attempts (
    id TEXT PRIMARY KEY,
    outbox_id TEXT NOT NULL REFERENCES notification_outbox(id),
    attempted_at TEXT NOT NULL,
    status TEXT NOT NULL,
    smtp_response TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS task_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_discovery_runs (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL CHECK(platform IN ('wechat','qq')),
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    item_count INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE TABLE IF NOT EXISTS chat_discovery_candidates (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES chat_discovery_runs(id) ON DELETE CASCADE,
    account_key TEXT NOT NULL,
    account_name TEXT NOT NULL,
    conversation_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    conversation_type TEXT NOT NULL DEFAULT 'chat',
    UNIQUE(run_id, account_key, conversation_key)
);
CREATE INDEX IF NOT EXISTS ix_chat_discovery_candidates_run
    ON chat_discovery_candidates(run_id, account_name, display_name);
CREATE TABLE IF NOT EXISTS chat_tags (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_scope_tags (
    scope_id TEXT NOT NULL REFERENCES source_scopes(id) ON DELETE CASCADE,
    tag_id TEXT NOT NULL REFERENCES chat_tags(id) ON DELETE CASCADE,
    PRIMARY KEY(scope_id, tag_id)
);
CREATE INDEX IF NOT EXISTS ix_chat_scope_tags_tag ON chat_scope_tags(tag_id, scope_id);
"""


class ClosingConnection(sqlite3.Connection):
    """Make `with Store.connect()` close the handle as well as delimit a transaction."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()

class Store:
    """SQLite-backed source/event/task store. Cursor changes share the ingest transaction."""

    def __init__(self, db_path: str | Path, data_dir: str | Path):
        self.db_path = Path(db_path)
        self.data_dir = Path(data_dir)
        self.raw_dir = self.data_dir / "raw"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(_SCHEMA)
            scope_columns = {row["name"] for row in conn.execute("PRAGMA table_info(source_scopes)")}
            if "full_scan_enabled" not in scope_columns:
                conn.execute("ALTER TABLE source_scopes ADD COLUMN full_scan_enabled INTEGER NOT NULL DEFAULT 1")
            if "updated_at" not in scope_columns:
                conn.execute("ALTER TABLE source_scopes ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE source_scopes SET updated_at=created_at WHERE updated_at='' ")
            task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
            if "fixed_start_at" not in task_columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN fixed_start_at TEXT")
            if "fixed_end_at" not in task_columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN fixed_end_at TEXT")
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, ?)",
                (utc_now(),),
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(2, ?)",
                (utc_now(),),
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(3, ?)",
                (utc_now(),),
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(4, ?)",
                (utc_now(),),
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(6, ?)",
                (utc_now(),),
            )

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _source_scope(
        conn: sqlite3.Connection, kind: str, external_key: str, display_name: str, scope_key: str
    ) -> tuple[str, str]:
        now = utc_now()
        source_id = conn.execute(
            "SELECT id FROM sources WHERE kind=? AND external_key=?", (kind, external_key)
        ).fetchone()
        if source_id:
            sid = source_id["id"]
        else:
            sid = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO sources(id,kind,external_key,display_name,created_at) VALUES(?,?,?,?,?)",
                (sid, kind, external_key, display_name, now),
            )
        scope = conn.execute(
            "SELECT id FROM source_scopes WHERE source_id=? AND scope_key=?", (sid, scope_key)
        ).fetchone()
        if scope:
            scope_id = scope["id"]
        else:
            scope_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO source_scopes(id,source_id,scope_key,display_name,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (scope_id, sid, scope_key, scope_key, now, now),
            )
        return sid, scope_id

    def upsert_chat_subscription(
        self, platform: str, account_key: str, conversation_key: str, display_name: str,
        *, enabled: bool = False, full_scan_enabled: bool = False, tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create or update a chat scope used as a subscription filter by future adapters."""
        if not all(isinstance(value, str) for value in
                   (platform, account_key, conversation_key, display_name)):
            raise ValueError("平台、账号、聊天 ID 和显示名称必须是文本")
        platform = platform.strip().lower()
        kind_by_platform = {"wechat": "wechat_chat", "qq": "qq_chat"}
        if platform not in kind_by_platform:
            raise ValueError("platform 只支持 wechat 或 qq")
        account_key = account_key.strip()
        conversation_key = conversation_key.strip()
        display_name = display_name.strip()
        if not account_key or len(account_key) > 200:
            raise ValueError("账号标识必须为 1-200 个字符")
        if not conversation_key or len(conversation_key) > 500:
            raise ValueError("聊天 ID 必须为 1-500 个字符")
        if not display_name or len(display_name) > 300:
            raise ValueError("聊天名称必须为 1-300 个字符")
        if not isinstance(enabled, bool) or not isinstance(full_scan_enabled, bool):
            raise ValueError("enabled 和 full_scan_enabled 必须为布尔值")
        tag_names = self._validate_chat_tags(tags) if tags is not None else None
        now = utc_now()
        kind = kind_by_platform[platform]
        with self.transaction() as conn:
            source = conn.execute("SELECT id FROM sources WHERE kind=? AND external_key=?",
                                  (kind, account_key.lower())).fetchone()
            if source:
                source_id = source["id"]
                conn.execute("UPDATE sources SET display_name=? WHERE id=?", (account_key, source_id))
            else:
                source_id = str(uuid.uuid4())
                conn.execute("INSERT INTO sources(id,kind,external_key,display_name,created_at) VALUES(?,?,?,?,?)",
                             (source_id, kind, account_key.lower(), account_key, now))
            scope = conn.execute("SELECT id FROM source_scopes WHERE source_id=? AND scope_key=?",
                                 (source_id, conversation_key)).fetchone()
            if scope:
                scope_id = scope["id"]
                conn.execute(
                    "UPDATE source_scopes SET display_name=?,enabled=?,full_scan_enabled=?,updated_at=? WHERE id=?",
                    (display_name, int(enabled), int(full_scan_enabled), now, scope_id),
                )
            else:
                scope_id = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO source_scopes(id,source_id,scope_key,display_name,enabled,full_scan_enabled,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (scope_id, source_id, conversation_key, display_name, int(enabled),
                     int(full_scan_enabled), now, now),
                )
            if tag_names is not None:
                self._replace_chat_tags(conn, scope_id, tag_names)
        return next(item for item in self.chat_subscriptions() if item["id"] == scope_id)

    def set_chat_subscription_options(self, scope_id: str, *, enabled: bool,
                                      full_scan_enabled: bool, tags: list[str] | None = None) -> None:
        if not isinstance(enabled, bool) or not isinstance(full_scan_enabled, bool):
            raise ValueError("enabled 和 full_scan_enabled 必须为布尔值")
        tag_names = self._validate_chat_tags(tags) if tags is not None else None
        with self.transaction() as conn:
            row = conn.execute(
                """SELECT sc.id FROM source_scopes sc JOIN sources s ON s.id=sc.source_id
                   WHERE sc.id=? AND s.kind IN ('wechat_chat','qq_chat')""", (scope_id,)
            ).fetchone()
            if not row:
                raise KeyError(scope_id)
            conn.execute(
                "UPDATE source_scopes SET enabled=?,full_scan_enabled=?,updated_at=? WHERE id=?",
                (int(enabled), int(full_scan_enabled), utc_now(), scope_id),
            )
            if tag_names is not None:
                self._replace_chat_tags(conn, scope_id, tag_names)

    @staticmethod
    def _validate_chat_tags(tags: list[str]) -> list[str]:
        if not isinstance(tags, list) or len(tags) > 20:
            raise ValueError("tags 必须为数组，每个聊天最多 20 个标签")
        names = {}
        for value in tags:
            if not isinstance(value, str):
                raise ValueError("标签名称必须为文本")
            name = unicodedata.normalize("NFC", value).strip()
            if not name or len(name) > 32 or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
                raise ValueError("标签名称须为 1–32 个字符，不能包含换行或控制字符")
            names.setdefault(name.casefold(), name)
        return list(names.values())

    @staticmethod
    def _replace_chat_tags(conn: sqlite3.Connection, scope_id: str, names: list[str]) -> None:
        conn.execute("DELETE FROM chat_scope_tags WHERE scope_id=?", (scope_id,))
        for name in names:
            key = name.casefold()
            conn.execute("INSERT OR IGNORE INTO chat_tags(id,name,normalized_name,created_at) VALUES(?,?,?,?)",
                         (str(uuid.uuid4()), name, key, utc_now()))
            tag_id = conn.execute("SELECT id FROM chat_tags WHERE normalized_name=?", (key,)).fetchone()[0]
            conn.execute("INSERT INTO chat_scope_tags(scope_id,tag_id) VALUES(?,?)", (scope_id, tag_id))

    def chat_tags(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                """SELECT t.id,t.name,COUNT(st.scope_id) AS chat_count FROM chat_tags t
                   LEFT JOIN chat_scope_tags st ON st.tag_id=t.id GROUP BY t.id ORDER BY t.normalized_name""")]

    def chat_subscriptions(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            tags_by_scope: dict[str, list[dict[str, str]]] = {}
            for tag in conn.execute(
                """SELECT st.scope_id,t.id,t.name FROM chat_scope_tags st JOIN chat_tags t ON t.id=st.tag_id
                   ORDER BY t.normalized_name"""):
                tags_by_scope.setdefault(tag["scope_id"], []).append({"id": tag["id"], "name": tag["name"]})
            rows = conn.execute(
                """SELECT sc.id,sc.source_id,CASE s.kind WHEN 'wechat_chat' THEN 'wechat' ELSE 'qq' END AS platform,
                          s.external_key AS account_key,s.display_name AS account_name,
                          sc.scope_key AS conversation_key,sc.display_name,
                          sc.enabled,sc.full_scan_enabled,sc.created_at,sc.updated_at,
                          (SELECT COUNT(*) FROM source_items i WHERE i.scope_id=sc.id) AS item_count
                   FROM source_scopes sc JOIN sources s ON s.id=sc.source_id
                   WHERE s.kind IN ('wechat_chat','qq_chat')
                   ORDER BY CASE s.kind WHEN 'wechat_chat' THEN 0 ELSE 1 END,
                            s.external_key,sc.display_name"""
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["enabled"] = bool(item["enabled"])
                item["full_scan_enabled"] = bool(item["full_scan_enabled"])
                item["tags"] = tags_by_scope.get(item["id"], [])
                result.append(item)
            return result

    def active_chat_subscriptions(self, platform: str, *, full_scan: bool = False) -> list[dict[str, Any]]:
        if platform not in {"wechat", "qq"}:
            raise ValueError("platform 只支持 wechat 或 qq")
        return [item for item in self.chat_subscriptions()
                if item["platform"] == platform and item["enabled"]
                and (item["full_scan_enabled"] or not full_scan)]

    def start_chat_discovery(self, platform: str) -> str:
        if platform not in {"wechat", "qq"}:
            raise ValueError("platform 只支持 wechat 或 qq")
        run_id = str(uuid.uuid4())
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO chat_discovery_runs(id,platform,status,started_at) VALUES(?,?,?,?)",
                (run_id, platform, "running", utc_now()),
            )
        return run_id

    def finish_chat_discovery(
        self, run_id: str, candidates: list[dict[str, Any]], *, error: str | None = None,
    ) -> None:
        if not isinstance(candidates, list):
            raise ValueError("候选会话必须是列表")
        with self.transaction() as conn:
            run = conn.execute(
                "SELECT platform,status FROM chat_discovery_runs WHERE id=?", (run_id,)
            ).fetchone()
            if not run or run["status"] != "running":
                raise KeyError(run_id)
            inserted = 0
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    raise ValueError("候选会话格式无效")
                account_key = candidate.get("account_key")
                account_name = candidate.get("account_name") or account_key
                conversation_key = candidate.get("conversation_key")
                display_name = candidate.get("display_name") or conversation_key
                message_count = candidate.get("message_count", 0)
                conversation_type = candidate.get("conversation_type", "chat")
                if not all(isinstance(value, str) and value.strip() for value in
                           (account_key, account_name, conversation_key, display_name, conversation_type)):
                    raise ValueError("候选会话缺少账号或会话标识")
                if len(account_key) > 200 or len(account_name) > 300 or len(conversation_key) > 500 or len(display_name) > 300:
                    raise ValueError("候选会话字段过长")
                if isinstance(message_count, bool) or not isinstance(message_count, int) or message_count < 0:
                    raise ValueError("消息数必须是非负整数")
                conn.execute(
                    """INSERT OR REPLACE INTO chat_discovery_candidates
                       (id,run_id,account_key,account_name,conversation_key,display_name,message_count,conversation_type)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), run_id, account_key.strip(), account_name.strip(),
                     conversation_key.strip(), display_name.strip(), message_count,
                     conversation_type.strip()[:40]),
                )
                inserted += 1
            conn.execute(
                """UPDATE chat_discovery_runs SET status=?,finished_at=?,item_count=?,error=?
                   WHERE id=?""",
                ("partial" if error else "completed", utc_now(), inserted, error, run_id),
            )

    def fail_chat_discovery(self, run_id: str, error: str) -> None:
        with self.transaction() as conn:
            changed = conn.execute(
                """UPDATE chat_discovery_runs SET status='failed',finished_at=?,error=?
                   WHERE id=? AND status='running'""",
                (utc_now(), str(error)[:2000], run_id),
            ).rowcount
            if not changed:
                raise KeyError(run_id)

    def chat_discovery_results(self, platform: str | None = None) -> dict[str, Any]:
        platforms = [platform] if platform else ["wechat", "qq"]
        if any(value not in {"wechat", "qq"} for value in platforms):
            raise ValueError("platform 只支持 wechat 或 qq")
        output: dict[str, Any] = {}
        with self.connect() as conn:
            for value in platforms:
                run = conn.execute(
                    """SELECT id,platform,status,started_at,finished_at,item_count,error
                       FROM chat_discovery_runs WHERE platform=?
                       ORDER BY started_at DESC LIMIT 1""",
                    (value,),
                ).fetchone()
                if not run:
                    output[value] = {"run": None, "candidates": []}
                    continue
                rows = conn.execute(
                    """SELECT c.id,c.account_key,c.account_name,c.conversation_key,c.display_name,
                              c.message_count,c.conversation_type,
                              EXISTS(
                                SELECT 1 FROM source_scopes sc JOIN sources s ON s.id=sc.source_id
                                WHERE s.kind=? AND lower(s.external_key)=lower(c.account_key)
                                  AND sc.scope_key=c.conversation_key
                              ) AS subscribed
                       FROM chat_discovery_candidates c WHERE c.run_id=?
                       ORDER BY c.account_name COLLATE NOCASE,c.display_name COLLATE NOCASE""",
                    ("wechat_chat" if value == "wechat" else "qq_chat", run["id"]),
                ).fetchall()
                output[value] = {
                    "run": dict(run),
                    "candidates": [{**dict(row), "subscribed": bool(row["subscribed"])} for row in rows],
                }
        return output

    def select_discovered_chat_candidates(
        self, candidate_ids: list[str], *, full_scan_enabled: bool = False,
    ) -> list[dict[str, Any]]:
        if not isinstance(candidate_ids, list) or not candidate_ids or any(
            not isinstance(value, str) or not value for value in candidate_ids
        ):
            raise ValueError("请选择至少一个已发现的会话")
        if len(candidate_ids) > 500 or len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("所选会话数量无效")
        if not isinstance(full_scan_enabled, bool):
            raise ValueError("full_scan_enabled 必须为布尔值")
        with self.transaction() as conn:
            scope_ids = self._select_chat_candidates(conn, candidate_ids, full_scan_enabled)
        by_id = {item["id"]: item for item in self.chat_subscriptions()}
        return [by_id[scope_id] for scope_id in scope_ids]

    def _select_chat_candidates(self, conn, candidate_ids, full_scan_enabled):
        scope_ids = []
        now = utc_now()
        for candidate_id in candidate_ids:
            row = conn.execute(
                """SELECT c.*,r.platform FROM chat_discovery_candidates c
                   JOIN chat_discovery_runs r ON r.id=c.run_id
                   WHERE c.id=? AND r.status IN ('completed','partial')
                     AND r.id=(SELECT newest.id FROM chat_discovery_runs newest
                               WHERE newest.platform=r.platform
                               ORDER BY newest.started_at DESC,newest.rowid DESC LIMIT 1)""",
                (candidate_id,),
            ).fetchone()
            if not row:
                raise ValueError("所选会话不在最新扫描结果中，请重新扫描后再选")
            kind = "wechat_chat" if row["platform"] == "wechat" else "qq_chat"
            source = conn.execute(
                "SELECT id FROM sources WHERE kind=? AND external_key=?",
                (kind, row["account_key"].lower()),
            ).fetchone()
            if source:
                source_id = source["id"]
                conn.execute("UPDATE sources SET display_name=? WHERE id=?",
                             (row["account_key"], source_id))
            else:
                source_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO sources(id,kind,external_key,display_name,created_at) VALUES(?,?,?,?,?)",
                    (source_id, kind, row["account_key"].lower(), row["account_key"], now),
                )
            scope = conn.execute(
                "SELECT id FROM source_scopes WHERE source_id=? AND scope_key=?",
                (source_id, row["conversation_key"]),
            ).fetchone()
            if scope:
                scope_id = scope["id"]
                conn.execute(
                    """UPDATE source_scopes SET display_name=?,enabled=1,full_scan_enabled=?,updated_at=?
                       WHERE id=?""",
                    (row["display_name"], int(full_scan_enabled), now, scope_id),
                )
            else:
                scope_id = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO source_scopes(id,source_id,scope_key,display_name,enabled,
                              full_scan_enabled,created_at,updated_at)
                       VALUES(?,?,?,?,1,?,?,?)""",
                    (scope_id, source_id, row["conversation_key"], row["display_name"],
                     int(full_scan_enabled), now, now),
                )
            scope_ids.append(scope_id)
        return scope_ids

    def transfer_chat_subscriptions(self, *, candidate_ids=None, enable_ids=None, disable_ids=None,
                                    full_scan_enabled=False):
        """Apply only explicit shuttle deltas atomically; never delete chat data/tags."""
        candidate_ids = [] if candidate_ids is None else candidate_ids
        enable_ids = [] if enable_ids is None else enable_ids
        disable_ids = [] if disable_ids is None else disable_ids
        for values in (candidate_ids, enable_ids, disable_ids):
            if not isinstance(values, list) or len(values)>500 or any(not isinstance(v,str) or not v for v in values) or len(set(values))!=len(values):
                raise ValueError("会话 ID 必须为不重复的数组，每批最多 500 个")
        if not isinstance(full_scan_enabled,bool):
            raise ValueError("full_scan_enabled 必须为布尔值")
        if not (candidate_ids or enable_ids or disable_ids):
            raise ValueError("没有需要保存的订阅变更")
        if set(enable_ids) & set(disable_ids):
            raise ValueError("同一会话不能同时启用和停用")
        with self.transaction() as conn:
            for scope_id in enable_ids + disable_ids:
                found = conn.execute("""SELECT sc.id FROM source_scopes sc JOIN sources s ON s.id=sc.source_id
                    WHERE sc.id=? AND s.kind IN ('wechat_chat','qq_chat')""", (scope_id,)).fetchone()
                if not found:
                    raise ValueError("会话范围不存在，请刷新后重试")
            added = self._select_chat_candidates(conn,candidate_ids,full_scan_enabled) if candidate_ids else []
            if set(added) & set(disable_ids):
                raise ValueError("同一会话不能同时加入和移出")
            for scope_id in enable_ids:
                conn.execute("UPDATE source_scopes SET enabled=1,full_scan_enabled=?,updated_at=? WHERE id=?",
                    (int(full_scan_enabled),utc_now(),scope_id))
            for scope_id in disable_ids:
                conn.execute("UPDATE source_scopes SET enabled=0,full_scan_enabled=0,updated_at=? WHERE id=?",
                    (utc_now(),scope_id))
        return {"added":len(added),"enabled":len(enable_ids),"disabled":len(disable_ids),
                "subscriptions":self.chat_subscriptions()}

    def get_mail_cursor(self, account: str, folder: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT c.cursor_json FROM source_cursors c
                   JOIN sources s ON s.id=c.source_id JOIN source_scopes sc ON sc.id=c.scope_id
                   WHERE s.kind='qq_mail' AND s.external_key=? AND sc.scope_key=? AND c.cursor_name='imap_uid'""",
                (account.strip().lower(), folder),
            ).fetchone()
            return json.loads(row["cursor_json"]) if row else None

    def initialize_mail_cursor(
        self, account: str, folder: str, uidvalidity: str | None, uid: int, *, status: str = "baseline"
    ) -> bool:
        account_key = account.strip().lower()
        with self.transaction() as conn:
            source_id, scope_id = self._source_scope(conn, "qq_mail", account_key, account, folder)
            existing = conn.execute(
                "SELECT cursor_json FROM source_cursors WHERE source_id=? AND scope_id=? AND cursor_name='imap_uid'",
                (source_id, scope_id),
            ).fetchone()
            if existing:
                return False
            value = {"uid": int(uid), "uidvalidity": uidvalidity}
            conn.execute(
                "INSERT INTO source_cursors(source_id,scope_id,cursor_name,cursor_json,updated_at) VALUES(?,?, 'imap_uid', ?, ?) ",
                (source_id, scope_id, _json(value), utc_now()),
            )
            run_id = str(uuid.uuid4())
            now = utc_now()
            conn.execute(
                "INSERT INTO scan_runs(id,source_id,scope_id,started_at,finished_at,status,item_count,cursor_before,cursor_after) VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, source_id, scope_id, now, now, status, 0, None, _json(value)),
            )
        return True

    def record_scan_status(
        self, account: str, folder: str, status: str, *, item_count: int = 0,
        cursor_before: dict[str, Any] | None = None, cursor_after: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self.transaction() as conn:
            source_id, scope_id = self._source_scope(conn, "qq_mail", account.strip().lower(), account, folder)
            now = utc_now()
            conn.execute(
                "INSERT INTO scan_runs(id,source_id,scope_id,started_at,finished_at,status,item_count,cursor_before,cursor_after,error) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), source_id, scope_id, now, now, status, item_count,
                 _json(cursor_before) if cursor_before else None,
                 _json(cursor_after) if cursor_after else None, error),
            )

    def _write_raw(self, raw: bytes) -> str:
        digest = hashlib.sha256(raw).hexdigest()
        relative = Path("raw") / "email" / digest[:2] / f"{digest}.eml"
        target = self.data_dir / relative
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temp.open("xb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.replace(temp, target)
                except FileExistsError:
                    temp.unlink(missing_ok=True)
            finally:
                temp.unlink(missing_ok=True)
        return relative.as_posix()

    def ingest_mail_batch(
        self,
        account: str,
        folder: str,
        uidvalidity: str | None,
        messages: list[dict[str, Any]],
        next_uid: int,
        *,
        cursor_before: dict[str, Any],
    ) -> dict[str, int]:
        if not messages:
            raise ValueError("空批次不得推进邮箱游标")
        uids = [int(message["uid"]) for message in messages]
        if uids != sorted(set(uids)):
            raise ValueError("本批 UID 必须严格递增且唯一")
        if int(next_uid) != uids[-1]:
            raise ValueError("游标只能推进到本批最后一个已持久化 UID")
        raw_paths = [self._write_raw(bytes(message["raw"])) for message in messages]
        counts = {"items": 0, "versions": 0, "events": 0}
        account_key = account.strip().lower()
        with self.transaction() as conn:
            source_id, scope_id = self._source_scope(conn, "qq_mail", account_key, account, folder)
            cursor_row = conn.execute(
                "SELECT cursor_json FROM source_cursors WHERE source_id=? AND scope_id=? AND cursor_name='imap_uid'",
                (source_id, scope_id),
            ).fetchone()
            if not cursor_row:
                raise RuntimeError("邮箱文件夹尚未建立基线")
            current = json.loads(cursor_row["cursor_json"])
            if current.get("uidvalidity") and uidvalidity and str(current["uidvalidity"]) != str(uidvalidity):
                raise RuntimeError("UIDVALIDITY 已变化；需先执行文件夹核对，游标未推进")
            if uids[0] <= int(current.get("uid", 0)):
                raise RuntimeError("批次包含不大于当前游标的 UID；游标未推进")
            started = utc_now()
            scan_id = str(uuid.uuid4())
            for message, raw_path in zip(messages, raw_paths):
                uid = int(message["uid"])
                external_id = f"{uidvalidity or 'unknown'}:{uid}"
                item = conn.execute(
                    "SELECT id,latest_version_id FROM source_items WHERE source_id=? AND scope_id=? AND external_id=?",
                    (source_id, scope_id, external_id),
                ).fetchone()
                is_new_item = item is None
                item_id = str(uuid.uuid4()) if is_new_item else item["id"]
                if is_new_item:
                    conn.execute(
                        "INSERT INTO source_items(id,source_id,scope_id,external_id,first_seen_at) VALUES(?,?,?,?,?)",
                        (item_id, source_id, scope_id, external_id, started),
                    )
                    counts["items"] += 1
                raw = bytes(message["raw"])
                content_hash = hashlib.sha256(raw).hexdigest()
                version_id = str(uuid.uuid4())
                version_result = conn.execute(
                    "INSERT OR IGNORE INTO item_versions(id,item_id,content_hash,raw_path,content_type,content_text,metadata_json,observed_at) VALUES(?,?,?,?,?,?,?,?)",
                    (version_id, item_id, content_hash, raw_path, "message/rfc822",
                     str(message.get("body_text", "")), _json(message.get("metadata", {})), started),
                )
                if version_result.rowcount == 0:
                    continue
                counts["versions"] += 1
                version = conn.execute(
                    "SELECT id FROM item_versions WHERE item_id=? AND content_hash=?", (item_id, content_hash)
                ).fetchone()
                actual_version_id = version["id"]
                event_type = "new" if is_new_item else "changed"
                event_result = conn.execute(
                    "INSERT OR IGNORE INTO events(id,item_version_id,event_type,created_at) VALUES(?,?,?,?)",
                    (str(uuid.uuid4()), actual_version_id, event_type, started),
                )
                if event_result.rowcount:
                    counts["events"] += 1
                conn.execute("UPDATE source_items SET latest_version_id=? WHERE id=?", (actual_version_id, item_id))
            cursor_after = {"uid": int(next_uid), "uidvalidity": uidvalidity or current.get("uidvalidity")}
            conn.execute(
                "UPDATE source_cursors SET cursor_json=?,updated_at=? WHERE source_id=? AND scope_id=? AND cursor_name='imap_uid'",
                (_json(cursor_after), utc_now(), source_id, scope_id),
            )
            conn.execute(
                "INSERT INTO scan_runs(id,source_id,scope_id,started_at,finished_at,status,item_count,cursor_before,cursor_after) VALUES(?,?,?,?,?,?,?,?,?)",
                (scan_id, source_id, scope_id, started, utc_now(), "ok", len(messages),
                 _json(cursor_before), _json(cursor_after)),
            )
        return counts

    def ingest_mail_range_batch(
        self, account: str, folder: str, uidvalidity: str | None,
        messages: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Persist a historical/range batch without reading or moving the incremental UID cursor."""
        if not messages:
            raise ValueError("空批次无需持久化")
        uids = [int(message["uid"]) for message in messages]
        if uids != sorted(set(uids)):
            raise ValueError("本批 UID 必须严格递增且唯一")
        raw_paths = [self._write_raw(bytes(message["raw"])) for message in messages]
        counts = {"items": 0, "versions": 0, "events": 0}
        account_key = account.strip().lower()
        with self.transaction() as conn:
            source_id, scope_id = self._source_scope(conn, "qq_mail", account_key, account, folder)
            observed_at = utc_now()
            for message, raw_path in zip(messages, raw_paths):
                external_id = f"{uidvalidity or 'unknown'}:{int(message['uid'])}"
                item = conn.execute(
                    "SELECT id FROM source_items WHERE source_id=? AND scope_id=? AND external_id=?",
                    (source_id, scope_id, external_id),
                ).fetchone()
                is_new_item = item is None
                item_id = str(uuid.uuid4()) if is_new_item else item["id"]
                if is_new_item:
                    conn.execute(
                        "INSERT INTO source_items(id,source_id,scope_id,external_id,first_seen_at) VALUES(?,?,?,?,?)",
                        (item_id, source_id, scope_id, external_id, observed_at),
                    )
                    counts["items"] += 1
                raw = bytes(message["raw"])
                content_hash = hashlib.sha256(raw).hexdigest()
                version_id = str(uuid.uuid4())
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO item_versions(id,item_id,content_hash,raw_path,content_type,content_text,metadata_json,observed_at) VALUES(?,?,?,?,?,?,?,?)",
                    (version_id, item_id, content_hash, raw_path, "message/rfc822",
                     str(message.get("body_text", "")), _json(message.get("metadata", {})), observed_at),
                ).rowcount
                if not inserted:
                    continue
                counts["versions"] += 1
                actual_version_id = conn.execute(
                    "SELECT id FROM item_versions WHERE item_id=? AND content_hash=?",
                    (item_id, content_hash),
                ).fetchone()["id"]
                event_type = "new" if is_new_item else "changed"
                event_inserted = conn.execute(
                    "INSERT OR IGNORE INTO events(id,item_version_id,event_type,created_at) VALUES(?,?,?,?)",
                    (str(uuid.uuid4()), actual_version_id, event_type, observed_at),
                ).rowcount
                counts["events"] += int(bool(event_inserted))
                conn.execute("UPDATE source_items SET latest_version_id=? WHERE id=?",
                             (actual_version_id, item_id))
        return counts

    def claim_next_event(self, consumer_name: str, lease_seconds: int = 180) -> dict[str, Any] | None:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat(timespec="seconds")
        until = (now_dt + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
        with self.transaction() as conn:
            row = conn.execute(
                """SELECT e.id AS event_id,e.item_version_id,e.event_type,e.attempts,
                          v.content_text,v.metadata_json,v.raw_path,i.id AS item_id,
                          s.kind AS source_kind,s.display_name AS source_name,s.external_key,
                          sc.scope_key
                   FROM events e JOIN item_versions v ON v.id=e.item_version_id
                   JOIN source_items i ON i.id=v.item_id JOIN sources s ON s.id=i.source_id
                   JOIN source_scopes sc ON sc.id=i.scope_id
                   WHERE e.status='pending' OR (e.status='processing' AND e.lease_until < ?)
                   ORDER BY e.created_at LIMIT 1""", (now,)
            ).fetchone()
            if not row:
                return None
            attempt_no = int(row["attempts"]) + 1
            consumption_id = str(uuid.uuid4())
            input_value = {
                "event_id": row["event_id"], "event_type": row["event_type"],
                "source": {"kind": row["source_kind"], "name": row["source_name"],
                           "external_key": row["external_key"], "scope": row["scope_key"]},
                "content": row["content_text"], "metadata": json.loads(row["metadata_json"]),
            }
            conn.execute(
                "UPDATE events SET status='processing',attempts=?,lease_owner=?,lease_until=?,last_error=NULL WHERE id=?",
                (attempt_no, consumer_name, until, row["event_id"]),
            )
            conn.execute(
                "INSERT INTO event_consumptions(id,event_id,consumer_name,attempt_no,status,input_json,started_at) VALUES(?,?,?,?,?,?,?)",
                (consumption_id, row["event_id"], consumer_name, attempt_no, "processing", _json(input_value), now),
            )
            return {"consumption_id": consumption_id, "attempt_no": attempt_no, **input_value,
                    "item_id": row["item_id"], "raw_path": row["raw_path"]}

    @staticmethod
    def _candidate(candidate: dict[str, Any], event_id: str, index: int) -> dict[str, Any]:
        title = str(candidate.get("title", "")).strip()
        if not title:
            raise ValueError(f"第 {index + 1} 个任务候选缺少 title")
        identity_key = str(candidate.get("identity_key") or f"event:{event_id}:{index}").strip()
        if not identity_key or len(identity_key) > 240:
            raise ValueError("identity_key 必须为 1-240 个字符")
        estimated = candidate.get("estimated_minutes")
        if estimated is not None:
            estimated = int(estimated)
            if estimated <= 0 or estimated > 24 * 60:
                raise ValueError("estimated_minutes 必须在 1 到 1440 之间")
        priority = int(candidate.get("priority", 3))
        if priority not in (1, 2, 3, 4, 5):
            raise ValueError("priority 必须是 1-5")
        due_at = candidate.get("due_at")
        if due_at is not None:
            due_at = str(due_at).strip() or None
            if due_at:
                datetime.fromisoformat(due_at.replace("Z", "+00:00"))
        fixed_start = candidate.get("fixed_start_at")
        fixed_end = candidate.get("fixed_end_at")
        if (fixed_start is None) != (fixed_end is None):
            raise ValueError("fixed_start_at 和 fixed_end_at 必须同时提供")
        if fixed_start is not None:
            fixed_start = str(fixed_start).strip()
            fixed_end = str(fixed_end).strip()
            start_dt = datetime.fromisoformat(fixed_start.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(fixed_end.replace("Z", "+00:00"))
            if end_dt <= start_dt:
                raise ValueError("fixed_end_at 必须晚于 fixed_start_at")
        return {"title": title, "identity_key": identity_key,
                "description": str(candidate.get("description", "")),
                "due_at": due_at, "estimated_minutes": estimated, "priority": priority,
                "fixed_start_at": fixed_start, "fixed_end_at": fixed_end}

    def complete_event(
        self, event: dict[str, Any], agent_result: dict[str, Any], *, agent_name: str, agent_version: str
    ) -> dict[str, Any]:
        candidates = agent_result.get("tasks", [])
        if not isinstance(candidates, list):
            raise ValueError("Agent 输出 tasks 必须是数组")
        normalized = [self._candidate(item, event["event_id"], i) for i, item in enumerate(candidates)]
        now = utc_now()
        applied: list[dict[str, Any]] = []
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT status,item_version_id,attempts,lease_owner,lease_until FROM events WHERE id=?", (event["event_id"],)
            ).fetchone()
            if not row:
                raise RuntimeError("事件不存在")
            if row["status"] == "succeeded":
                conn.execute(
                    "UPDATE event_consumptions SET status='superseded',finished_at=? WHERE id=? AND status='processing'",
                    (now, event["consumption_id"]),
                )
                return {"tasks": [], "already_applied": True}
            consumption = conn.execute(
                "SELECT * FROM event_consumptions WHERE id=?", (event["consumption_id"],)
            ).fetchone()
            if (not consumption or consumption["event_id"] != event["event_id"] or
                consumption["status"] != "processing" or row["status"] != "processing" or
                row["attempts"] != consumption["attempt_no"] or
                row["lease_owner"] != consumption["consumer_name"] or
                not row["lease_until"] or row["lease_until"] < now):
                raise RuntimeError("消费租约已失效或事件已被其他请求预留，拒绝迟到结果")
            version = conn.execute(
                "SELECT item_id FROM item_versions WHERE id=?", (row["item_version_id"],)
            ).fetchone()
            if not version:
                raise RuntimeError("事件的原始消息版本不存在")
            for candidate in normalized:
                task_row = conn.execute(
                    "SELECT * FROM tasks WHERE identity_key=?", (candidate["identity_key"],)
                ).fetchone()
                if not task_row:
                    task_id = str(uuid.uuid4())
                    conn.execute(
                        "INSERT INTO tasks(id,identity_key,title,description,due_at,estimated_minutes,fixed_start_at,fixed_end_at,priority,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (task_id, candidate["identity_key"], candidate["title"], candidate["description"],
                         candidate["due_at"], candidate["estimated_minutes"], candidate["fixed_start_at"],
                         candidate["fixed_end_at"], candidate["priority"], now, now),
                    )
                    task_row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                else:
                    task_id = task_row["id"]
                    before = {k: task_row[k] for k in ("title", "description", "due_at", "estimated_minutes",
                                                        "fixed_start_at", "fixed_end_at", "priority")}
                    after = dict(before)
                    for field in ("title", "description", "due_at", "estimated_minutes",
                                  "fixed_start_at", "fixed_end_at", "priority"):
                        if candidate[field] is not None and candidate[field] != "":
                            after[field] = candidate[field]
                    if after != before and task_row["status"] != "completed":
                        conn.execute(
                            "UPDATE tasks SET title=?,description=?,due_at=?,estimated_minutes=?,fixed_start_at=?,fixed_end_at=?,priority=?,updated_at=? WHERE id=?",
                            (after["title"], after["description"], after["due_at"], after["estimated_minutes"],
                             after["fixed_start_at"], after["fixed_end_at"], after["priority"], now, task_id),
                        )
                        conn.execute(
                            "INSERT INTO task_revisions(id,task_id,changed_at,reason,before_json,after_json) VALUES(?,?,?,?,?,?)",
                            (str(uuid.uuid4()), task_id, now, "agent_update", _json(before), _json(after)),
                        )
                occurrence_id = str(uuid.uuid4())
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO task_occurrences(id,task_id,event_id,source_item_id,occurrence_type,observed_at) VALUES(?,?,?,?,?,?)",
                    (occurrence_id, task_id, event["event_id"], version["item_id"], "notification", now),
                ).rowcount
                count = conn.execute(
                    "SELECT COUNT(*) AS n FROM task_occurrences WHERE task_id=?", (task_id,)
                ).fetchone()["n"]
                if inserted and count == 1:
                    subject = f"新增待办：{candidate['title']}"
                    body = _task_email_body(task_row, candidate, count, event)
                    conn.execute(
                        "INSERT OR IGNORE INTO notification_outbox(id,notification_type,task_id,dedupe_key,subject,body,created_at) VALUES(?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), "new_task", task_id, f"new:{task_id}", subject, body, now),
                    )
                elif inserted and count > 1 and task_row["status"] != "completed":
                    subject = f"再次提醒：未完成事项「{candidate['title']}」"
                    body = _task_email_body(task_row, candidate, count, event, repeated=True)
                    conn.execute(
                        "INSERT OR IGNORE INTO notification_outbox(id,notification_type,task_id,dedupe_key,subject,body,created_at) VALUES(?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), "repeat_reminder", task_id,
                         f"repeat:{task_id}:{event['event_id']}", subject, body, now),
                    )
                applied.append({"task_id": task_id, "title": candidate["title"], "occurrences": count,
                                "status": task_row["status"], "created": count == 1 and inserted == 1})
            output_json = _json({"tasks": normalized})
            conn.execute(
                "INSERT INTO agent_runs(id,consumption_id,agent_name,agent_version,status,output_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), event["consumption_id"], agent_name, agent_version, "succeeded", output_json, now),
            )
            conn.execute(
                "UPDATE event_consumptions SET status='succeeded',output_json=?,finished_at=? WHERE id=?",
                (output_json, now, event["consumption_id"]),
            )
            conn.execute(
                "UPDATE events SET status='succeeded',lease_owner=NULL,lease_until=NULL,last_error=NULL WHERE id=?",
                (event["event_id"],),
            )
        return {"tasks": applied, "already_applied": False}

    def fail_event(self, event: dict[str, Any], error: str, *, agent_name: str = "unknown", agent_version: str = "") -> None:
        now = utc_now()
        clean = str(error)[:2000]
        with self.transaction() as conn:
            consumption = conn.execute(
                "SELECT * FROM event_consumptions WHERE id=?", (event["consumption_id"],)
            ).fetchone()
            current = conn.execute("SELECT status,attempts,lease_owner FROM events WHERE id=?",
                                   (event["event_id"],)).fetchone()
            if (not consumption or not current or consumption["event_id"] != event["event_id"] or
                consumption["status"] != "processing" or current["status"] != "processing" or
                current["attempts"] != consumption["attempt_no"] or
                current["lease_owner"] != consumption["consumer_name"]):
                conn.execute(
                    "UPDATE event_consumptions SET status='superseded',finished_at=? WHERE id=? AND status='processing'",
                    (now, event["consumption_id"]),
                )
                return
            conn.execute(
                "UPDATE event_consumptions SET status='failed',error=?,finished_at=? WHERE id=?",
                (clean, now, event["consumption_id"]),
            )
            conn.execute(
                "INSERT OR IGNORE INTO agent_runs(id,consumption_id,agent_name,agent_version,status,output_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), event["consumption_id"], agent_name, agent_version, "failed", None, now),
            )
            conn.execute(
                "UPDATE events SET status='pending',lease_owner=NULL,lease_until=NULL,last_error=? WHERE id=?",
                (clean, event["event_id"]),
            )

    def set_task_status(self, task_id: str, status: str) -> None:
        if status not in {"open", "completed", "deferred", "cancelled"}:
            raise ValueError("不支持的任务状态")
        with self.transaction() as conn:
            row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError(task_id)
            old = row["status"]
            conn.execute("UPDATE tasks SET status=?,updated_at=? WHERE id=?", (status, utc_now(), task_id))
            conn.execute(
                "INSERT INTO task_revisions(id,task_id,changed_at,reason,before_json,after_json) VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()), task_id, utc_now(), "user_status_change", _json({"status": old}), _json({"status": status})),
            )

    def tasks(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT t.*,COUNT(o.id) AS occurrence_count FROM tasks t
                   LEFT JOIN task_occurrences o ON o.task_id=t.id GROUP BY t.id
                   ORDER BY CASE t.status WHEN 'open' THEN 0 ELSE 1 END, t.due_at IS NULL, t.due_at"""
            ).fetchall()
            return [dict(row) for row in rows]

    def save_plan(self, status: str, objective: dict[str, Any], explanation: str,
                  items: list[dict[str, Any]]) -> str:
        """Persist one schedule decision and every considered task in a single transaction."""
        plan_id = str(uuid.uuid4())
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO plan_runs(id,created_at,status,objective_json,explanation) VALUES(?,?,?,?,?)",
                (plan_id, now, status, _json(objective), explanation),
            )
            for item in items:
                conn.execute(
                    "INSERT INTO plan_items(id,plan_run_id,task_id,starts_at,ends_at,decision,reason) VALUES(?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), plan_id, item["task_id"], item.get("starts_at"),
                     item.get("ends_at"), item["decision"], item.get("reason", "")),
                )
        return plan_id

    def plan_items(self, plan_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM plan_items WHERE plan_run_id=? ORDER BY starts_at,task_id", (plan_id,)
            ).fetchall()]

    def outbox_preview(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id,notification_type,task_id,subject,body,status,created_at FROM notification_outbox ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def claim_next_notification(self, recipient: str) -> dict[str, Any] | None:
        recipient = recipient.strip()
        if not recipient or "@" not in recipient:
            raise ValueError("请提供有效的目标邮箱")
        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM notification_outbox WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            attempt_id = str(uuid.uuid4())
            conn.execute(
                "UPDATE notification_outbox SET status='sending',recipient=? WHERE id=? AND status='queued'",
                (recipient, row["id"]),
            )
            conn.execute(
                "INSERT INTO delivery_attempts(id,outbox_id,attempted_at,status) VALUES(?,?,?,'sending')",
                (attempt_id, row["id"], now),
            )
            return {**dict(row), "recipient": recipient, "delivery_attempt_id": attempt_id}

    def finish_delivery(self, notification: dict[str, Any], outcome: str,
                        *, smtp_response: str | None = None, error: str | None = None) -> None:
        if outcome not in {"accepted", "failed", "delivery_unknown"}:
            raise ValueError("未知邮件投递结果")
        now = utc_now()
        outbox_status = {"accepted": "sent", "failed": "failed", "delivery_unknown": "delivery_unknown"}[outcome]
        with self.transaction() as conn:
            conn.execute(
                "UPDATE delivery_attempts SET status=?,smtp_response=?,error=? WHERE id=?",
                (outcome, (smtp_response or "")[:1000] or None, (error or "")[:2000] or None,
                 notification["delivery_attempt_id"]),
            )
            conn.execute(
                "UPDATE notification_outbox SET status=?,sent_at=?,last_error=? WHERE id=? AND status='sending'",
                (outbox_status, now if outcome == "accepted" else None,
                 (error or "")[:2000] or None, notification["id"]),
            )

    def recover_interrupted_deliveries(self) -> int:
        """Never auto-resend work left in 'sending'; SMTP acceptance may be uncertain."""
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat(timespec="seconds")
        cutoff = (now_dt - timedelta(minutes=5)).isoformat(timespec="seconds")
        with self.transaction() as conn:
            rows = conn.execute(
                """SELECT o.id AS outbox_id,d.id AS attempt_id FROM notification_outbox o
                   JOIN delivery_attempts d ON d.outbox_id=o.id
                   WHERE o.status='sending' AND d.status='sending' AND d.attempted_at < ?""",
                (cutoff,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE notification_outbox SET status='delivery_unknown',last_error=? WHERE id=? AND status='sending'",
                    ("投递尝试超过 5 分钟仍未结束；需人工核对邮箱", row["outbox_id"]),
                )
                conn.execute(
                    "UPDATE delivery_attempts SET status='delivery_unknown',error=? WHERE id=? AND status='sending'",
                    ("进程可能在 SMTP DATA 阶段退出；是否接收未知", row["attempt_id"]),
                )
            return len(rows)

    def retry_failed_notification(self, notification_id: str) -> None:
        with self.transaction() as conn:
            row = conn.execute("SELECT status FROM notification_outbox WHERE id=?", (notification_id,)).fetchone()
            if not row:
                raise KeyError(notification_id)
            if row["status"] != "failed":
                raise ValueError("仅明确失败的邮件可手动重试；delivery_unknown 不得自动重发")
            conn.execute(
                "UPDATE notification_outbox SET status='queued',last_error=NULL WHERE id=?",
                (notification_id,),
            )
    def stats(self) -> dict[str, int]:
        with self.connect() as conn:
            result = {}
            for table in ("sources", "source_items", "item_versions", "events", "event_consumptions", "tasks", "notification_outbox"):
                result[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            result["pending_events"] = int(conn.execute("SELECT COUNT(*) FROM events WHERE status='pending'").fetchone()[0])
            kinds = {r[0]: r[1] for r in conn.execute("SELECT s.kind,COUNT(*) FROM source_items i JOIN sources s ON s.id=i.source_id GROUP BY s.kind")}
            result["mail_items"] = int(kinds.get("qq_mail", 0))
            result["chat_items"] = int(kinds.get("wechat_chat", 0) + kinds.get("qq_chat", 0))
            result["scheduled_items"] = int(kinds.get("codex_scheduled", 0) + kinds.get("chatgpt_scheduled", 0))
            return result


def _task_email_body(task: sqlite3.Row, candidate: dict[str, Any], count: int, event: dict[str, Any], repeated: bool = False) -> str:
    lines = ["未完成事项再次收到一条独立通知。" if repeated else "发现一项待办，已加入本地任务库。",
             "", f"任务：{candidate['title']}"]
    due = candidate.get("due_at") or task["due_at"]
    if due:
        lines.append(f"截止：{due}")
    estimate = candidate.get("estimated_minutes") or task["estimated_minutes"]
    if estimate:
        lines.append(f"预计耗时：{estimate} 分钟")
    lines.extend([f"当前状态：{task['status']}", f"独立通知次数：{count}",
                  f"来源：{event['source']['name']} / {event['source']['scope']}",
                  f"事件 ID：{event['event_id']}", "", "此邮件是待发送预览；任务完成/延期状态需在本地任务库中确认。"])
    return "\n".join(lines)



