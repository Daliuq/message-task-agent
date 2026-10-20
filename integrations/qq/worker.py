"""Isolated QQBackup SQLCipher + QQNT_Export adapter; never opens original DBs writable."""
from __future__ import annotations

import base64
import contextlib
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "vendor/QQNT_Export"))
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
logging.disable(logging.CRITICAL)

from sqlalchemy import create_engine, func, inspect, literal_column
from sqlalchemy.orm import sessionmaker
import sqlcipher3.dbapi2 as sc
from db import DatabaseManager
from db.models import C2cMessage, GroupMessage
from parser.message import MessageParser
from exporters.chatlab_json import ChatLabJSONExporter


class ReadError(RuntimeError):
    """Only these deliberate, secret-free errors may reach the dashboard."""


def encoded(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {str(k): encoded(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encoded(v) for v in value]
    return value


def fingerprint(row):
    return hashlib.sha256(json.dumps(encoded(row), ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def copy_snapshot(source, target):
    """Fail rather than mix a changing main database and a stale WAL."""
    for attempt in range(3):
        paths = [source, Path(str(source) + "-wal")]
        before = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in paths if p.exists()}
        for suffix in ("", "-wal", "-shm"):
            Path(str(target) + suffix).unlink(missing_ok=True)
        with source.open("rb") as f:
            plain = f.read(16) == b"SQLite format 3\x00"
        if plain:
            with contextlib.closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
                with contextlib.closing(sqlite3.connect(target)) as dst:
                    src.backup(dst)
            return True
        with source.open("rb") as src, target.open("wb") as dst:
            src.seek(1024)
            shutil.copyfileobj(src, dst, 4 << 20)
        wal = Path(str(source) + "-wal")
        if wal.exists():
            shutil.copyfile(wal, str(target) + "-wal")
        after = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in paths if p.exists()}
        if before == after:
            return False
        time.sleep(0.25)
    raise ReadError("QQ 数据库正在持续变化，未形成一致快照；请稍后重试，不必重新取钥")


class LocalManager(DatabaseManager):
    def __new__(cls, *args, **kwargs):
        return object.__new__(cls)

    def __init__(self, engines):
        self._engines = engines
        self.session = sessionmaker(binds={model: engines[name]
            for name, models in self._models.items() if name in engines
            for model in models.values()})()

    def _message_query(self, model):
        # Upstream export filters empty placeholder rows. Our archival layer keeps
        # EVERY local row, including unknown and placeholder messages.
        return self.session.query(model)


@contextlib.contextmanager
def open_local(request):
    spec = importlib.util.spec_from_file_location("qqbackup_decrypt", HERE / "vendor/nt_msg_db_util/1.decrypt.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    helper.DB_KEY = request.get("key", "")
    engines, warnings = {}, []
    with tempfile.TemporaryDirectory(prefix="mta-qq-snapshot-") as tmp:
        manager = None
        try:
            source_dir = Path(request["directory"]).resolve()
            for name in ("nt_msg", "group_info", "profile_info", "emoji"):
                source = source_dir / (name + ".db")
                if not source.is_file():
                    if name == "nt_msg":
                        raise ReadError("QQ 消息数据库不存在")
                    continue
                target = Path(tmp) / (name + ".db")
                try:
                    plain = copy_snapshot(source, target)
                    def connect(path=target, is_plain=plain):
                        conn = sqlite3.connect(path) if is_plain else helper.open_enc(path)
                        conn.execute("PRAGMA query_only=ON")
                        return conn
                    engine = create_engine("sqlite://", creator=connect,
                        module=sqlite3 if plain else sc)
                    with engine.connect() as conn:
                        conn.exec_driver_sql("SELECT count(*) FROM sqlite_master").fetchone()
                    engines[name] = engine
                except Exception:
                    if name == "nt_msg":
                        raise ReadError("QQ 消息库快照读取失败；请检查密钥、磁盘空间或稍后重试") from None
                    warnings.append(name + " 辅助资料库未读取，名称可能回退为账号或群号")
            manager = LocalManager(engines)
            yield manager, warnings
        finally:
            if manager:
                manager.session.close()
            for engine in engines.values():
                engine.dispose()
            helper.DB_KEY = ""


def queries(manager):
    tables = set(inspect(manager._engines["nt_msg"]).get_table_names())
    result = {}
    if "group_msg_table" in tables:
        result.update({"group:" + str(k): (GroupMessage, v)
                       for k, v in manager.group_messages([]).items()})
    if "c2c_msg_table" in tables:
        result.update({"private:" + str(k): (C2cMessage, v)
                       for k, v in manager.c2c_messages([]).items()})
    if not {"group_msg_table", "c2c_msg_table"} & tables:
        raise ReadError("没有找到支持的 QQ 群聊或私聊表，不能把不兼容数据当作空列表")
    return result


def profile_names(manager):
    engine = manager._engines.get("profile_info")
    if not engine:
        return {}
    try:
        cols = {c["name"] for c in inspect(engine).get_columns("profile_info_v6")}
        fields = [f for f in ("1000", "1002", "20009", "20002") if f in cols]
        with engine.connect() as conn:
            rows = conn.exec_driver_sql('SELECT ' + ','.join('"' + f + '"' for f in fields)
                                       + ' FROM "profile_info_v6"').mappings()
            names = {}
            for r in rows:
                name = r.get("20009") or r.get("20002")
                if name:
                    for identity in (r.get("1000"), r.get("1002")):
                        if identity is not None:
                            names[str(identity)] = str(name)
            return names
    except Exception:
        return {}


def discover(manager, account):
    names = profile_names(manager)
    candidates = []
    for identity, (model, query) in queries(manager).items():
        peer = identity.split(":", 1)[1]
        display = manager.group_info(peer).display_name if identity.startswith("group:") else names.get(peer)
        if not display and model is C2cMessage:
            number = query.order_by(None).with_entities(model.interlocutor_num).first()
            display = names.get(str(number[0])) if number else None
        count = query.order_by(None).with_entities(func.count(model.id)).scalar()
        candidates.append({"account_key": account, "account_name": account,
            "conversation_key": identity, "display_name": str(display or peer)[:300],
            "conversation_type": "group" if model is GroupMessage else "private",
            "message_count": int(count)})
    return candidates


def native_rows(manager, query):
    sql = str(query.with_entities(literal_column("*")).statement.compile(compile_kwargs={"literal_binds": True}))
    with manager._engines["nt_msg"].connect() as conn:
        rows = conn.exec_driver_sql(sql).mappings()
        while batch := rows.fetchmany(200):
            for row in batch:
                yield dict(row)


def message_for(model, row, parser, formatter, shard):
    obj = model()
    for attr in inspect(model).column_attrs:
        setattr(obj, attr.key, row.get(attr.columns[0].name))
    parsed = None
    try:
        parsed = parser.parse_group_message(obj) if model is GroupMessage else parser.parse_c2c_message(obj)
        text = formatter._build_content(parsed.elements)
    except Exception:
        text = None
    stamp = row.get("40050")
    moment = None
    try:
        moment = datetime.fromtimestamp(float(stamp), timezone.utc).isoformat() if stamp and float(stamp) > 0 else None
    except (ValueError, TypeError, OverflowError, OSError):
        pass
    types = sorted({element.type.name for element in parsed.elements}) if parsed else []
    media = [element for element in (parsed.elements if parsed else [])
             if element.type.name in {"IMAGE", "VOICE", "VIDEO", "FILE", "ONLINE_FILE", "ONLINE_FOLDER"}]
    return {"external_id": "qq:" + model.__tablename__ + ":" + str(row["40001"]),
        "local_id": int(row["40001"]), "server_id": str(row["40001"]), "shard": shard,
        "source_time": moment, "text": text or "[正文未解析或空占位，原始字段已保存]",
        "sender": str(row.get("40020") or row.get("40033") or ""),
        "message_type": "+".join(types) or "unknown", "time_basis": "qq_message_time",
        "attachments": [{"filename": str(element.content.get("name") or ""),
                         "content_type": "application/octet-stream", "available": False} for element in media],
        "native_fingerprint": fingerprint(row), "raw_row": encoded(row),
        "parsed": encoded(asdict(parsed)) if parsed else None}


def read_pages(manager, scope, config, checkpoint, live_cursor):
    identity = scope["conversation_key"]
    found = queries(manager)
    if identity not in found:
        raise ReadError("所选 QQ 会话已不在本机数据库中，请重新发现会话")
    model, query = found[identity]
    query = query.order_by(None)
    label = "nt_msg.db:" + model.__tablename__
    expected = checkpoint.get("manifest", {}).get("shards", [])
    if expected:
        spec = expected[0]
        if len(expected) != 1 or spec["id"] != label:
            raise ReadError("QQ 续扫检查点与会话不匹配")
        query = query.filter(model.id > spec["start_after"], model.id <= spec["upper"])
    rows = native_rows(manager, query.order_by(model.id))
    known = checkpoint.get("qq_baseline", {}) if config["mode"] == "incremental" else {}
    selected, digest = [], hashlib.sha256()
    min_time, max_time, unknown, scanned, lower, upper, upper_anchor = None, None, 0, 0, None, None, None
    for row in rows:
        key = row.get("40001")
        if type(key) is not int:
            raise ReadError("QQ 原始消息主键不是整数，停止导入以免跳过记录")
        if upper is not None and key <= upper:
            raise ReadError("QQ 消息主键不唯一，停止导入")
        lower = key if lower is None else lower
        upper = key
        signature = fingerprint(row)
        upper_anchor = [key, signature]
        digest.update((str(key) + ":" + signature + "\n").encode())
        scanned += 1
        stamp = row.get("40050")
        valid = isinstance(stamp, (int, float)) and 0 < stamp < 253402300800
        if not valid:
            unknown += 1
        if config["mode"] == "range" and (not valid or not
                datetime.fromisoformat(config["start"]).timestamp() <= stamp < datetime.fromisoformat(config["end"]).timestamp()):
            continue
        external = "qq:" + model.__tablename__ + ":" + str(key)
        if config["mode"] == "incremental" and known.get(external) == signature:
            continue
        selected.append(key)
        if valid:
            min_time = stamp if min_time is None else min(stamp, min_time)
            max_time = stamp if max_time is None else max(stamp, max_time)
    spec = {"id": label, "upper": upper if upper is not None else -1, "upper_anchor": upper_anchor,
            "start_after": lower - 1 if lower is not None else -1, "count": len(selected),
            "min_time": min_time, "max_time": max_time, "unknown_time_count": unknown,
            "scanned_count": scanned, "native_snapshot_hash": digest.hexdigest()}
    if expected and spec != expected[0]:
        raise ReadError("QQ 源快照发生变化，请新建扫描；已保存记录和检查点保留，不会误报续扫完成")
    yield {"type": "manifest", "shards": [spec], "coverage": "local_snapshot_only",
           "incremental_strategy": "native_identity_and_fingerprint_reconciliation",
           "media": "raw_columns_saved_but_external_media_not_copied"}
    parser = MessageParser(manager)
    formatter = ChatLabJSONExporter(Path("unused.json"), {})
    after = checkpoint.get("positions", {}).get(label, spec["start_after"])
    pending = [key for key in selected if key > after]
    for start in range(0, len(pending), config["batch_size"]):
        keys = pending[start:start + config["batch_size"]]
        messages = [message_for(model, row, parser, formatter, label)
            for row in native_rows(manager, query.filter(model.id.in_(keys)).order_by(model.id))]
        if [m["local_id"] for m in messages] != keys:
            raise ReadError("QQ 快照分页计数不一致，未声明导入完成")
        after = keys[-1]
        yield {"type": "batch", "shard": label, "after": after, "messages": messages,
               "anchor": [after, messages[-1]["native_fingerprint"]]}
    if after < spec["upper"]:
        yield {"type": "batch", "shard": label, "after": spec["upper"], "messages": [], "anchor": upper_anchor}
    yield {"type": "done"}


def main():
    marker, action = "__QQ_RESULT__", "discover"
    try:
        request = json.loads(sys.stdin.readline())
        action = request.get("action", "discover")
        if action == "import":
            marker = "__CHAT_IMPORT__"
        with open_local(request) as (manager, warnings):
            if action == "discover":
                print(marker + json.dumps({"candidates": discover(manager, request["account"]),
                    "warnings": warnings}, ensure_ascii=False), flush=True)
            elif action == "import":
                for frame in read_pages(manager, request["scope"], request["config"], request["checkpoint"], request["live_cursor"]):
                    print(marker + json.dumps(frame, ensure_ascii=False, separators=(",", ":")), flush=True)
                    if sys.stdin.readline().strip() != "ACK":
                        raise ReadError("主进程未确认持久化，停止读取")
            else:
                raise ReadError("未知 QQ 读取操作")
        return 0
    except Exception as exc:
        # Never serialize native exception SQL/parameters or the request with keys.
        error = str(exc) if isinstance(exc, ReadError) else "QQ 读取失败（" + type(exc).__name__ + "）；请检查数据库兼容性或读取环境"
        print(marker + json.dumps({"type": "error", "error": error}, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
