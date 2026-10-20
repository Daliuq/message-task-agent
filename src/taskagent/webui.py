from __future__ import annotations

import ipaddress
import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from typing import Any, Callable

from .chat_discovery import ScannerUnavailable, discover_chat_candidates
from .mail import load_saved_credentials, scan_mailbox, scan_mailbox_range
from .store import Store

STATIC_DIR = Path(__file__).with_name("static")


def _limit(query: str, default: int = 100, maximum: int = 500) -> int:
    try:
        value = int(parse_qs(query).get("limit", [str(default)])[0])
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, maximum))


def _overview(store: Store) -> dict[str, Any]:
    with store.connect() as conn:
        latest = conn.execute(
            """SELECT r.finished_at,r.status,r.item_count,s.display_name,sc.scope_key
               FROM scan_runs r JOIN sources s ON s.id=r.source_id
               JOIN source_scopes sc ON sc.id=r.scope_id
               ORDER BY r.finished_at DESC LIMIT 1"""
        ).fetchone()
        event_rows = conn.execute("SELECT status,COUNT(*) AS n FROM events GROUP BY status").fetchall()
        folders = conn.execute(
            """SELECT sc.scope_key,c.cursor_json,c.updated_at
               FROM source_cursors c JOIN sources s ON s.id=c.source_id
               JOIN source_scopes sc ON sc.id=c.scope_id
               WHERE s.kind='qq_mail' ORDER BY sc.scope_key"""
        ).fetchall()
        last_mail = conn.execute(
            """SELECT v.observed_at, v.metadata_json
               FROM source_items i JOIN sources s ON s.id=i.source_id
               JOIN item_versions v ON v.id=i.latest_version_id
               WHERE s.kind='qq_mail' ORDER BY v.observed_at DESC LIMIT 1"""
        ).fetchone()
    events = {row["status"]: int(row["n"]) for row in event_rows}
    latest_scan = dict(latest) if latest else None
    last_email = None
    if last_mail:
        metadata = json.loads(last_mail["metadata_json"])
        last_email = {"observed_at": last_mail["observed_at"],
                      "subject": metadata.get("subject", "(无主题)"),
                      "from": metadata.get("from", "")}
    return {
        "counts": store.stats(),
        "event_status": events,
        "latest_scan": latest_scan,
        "last_email": last_email,
        "folders": [{"name": row["scope_key"], "cursor": json.loads(row["cursor_json"]),
                     "updated_at": row["updated_at"]} for row in folders],
        "open_tasks": sum(1 for task in store.tasks() if task["status"] == "open"),
    }


def _mail_items(store: Store, limit: int) -> list[dict[str, Any]]:
    with store.connect() as conn:
        rows = conn.execute(
            """SELECT i.id AS item_id, i.external_id, i.first_seen_at,
                      v.id AS version_id, v.content_text, v.metadata_json, v.observed_at,
                      v.raw_path, sc.scope_key AS folder, s.display_name AS source_name,
                      (SELECT e.status FROM events e WHERE e.item_version_id=v.id
                       ORDER BY e.created_at DESC LIMIT 1) AS event_status
               FROM source_items i JOIN sources s ON s.id=i.source_id
               JOIN source_scopes sc ON sc.id=i.scope_id
               JOIN item_versions v ON v.id=i.latest_version_id
               WHERE s.kind='qq_mail' ORDER BY v.observed_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    result = []
    for row in rows:
        metadata = json.loads(row["metadata_json"])
        result.append({**dict(row), "metadata": metadata})
        result[-1].pop("metadata_json", None)
    return result


def _events(store: Store, limit: int) -> list[dict[str, Any]]:
    with store.connect() as conn:
        rows = conn.execute(
            """SELECT e.id,e.event_type,e.status,e.attempts,e.last_error,e.created_at,
                      json_extract(v.metadata_json,'$.subject') AS subject,
                      json_extract(v.metadata_json,'$.from') AS sender,
                      sc.scope_key AS folder,s.display_name AS source_name,
                      (SELECT COUNT(*) FROM event_consumptions ec WHERE ec.event_id=e.id) AS consumption_count
               FROM events e JOIN item_versions v ON v.id=e.item_version_id
               JOIN source_items i ON i.id=v.item_id JOIN sources s ON s.id=i.source_id
               JOIN source_scopes sc ON sc.id=i.scope_id
               ORDER BY e.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def _scans(store: Store, limit: int) -> list[dict[str, Any]]:
    with store.connect() as conn:
        rows = conn.execute(
            """SELECT r.id,r.started_at,r.finished_at,r.status,r.item_count,r.error,
                      s.display_name,sc.scope_key,r.cursor_before,r.cursor_after
               FROM scan_runs r JOIN sources s ON s.id=r.source_id
               JOIN source_scopes sc ON sc.id=r.scope_id
               ORDER BY r.started_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            for field in ("cursor_before", "cursor_after"):
                value[field] = json.loads(value[field]) if value[field] else None
            result.append(value)
        return result


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(db_path: str | Path, data_dir: str | Path) -> type[BaseHTTPRequestHandler]:
    db_path = Path(db_path)
    data_dir = Path(data_dir)

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "MessageTaskDashboard/1.0"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:
            # Avoid logging email subjects, bodies, addresses, or query strings.
            print(f"[dashboard] {self.address_string()} {self.command} {self.path.split('?')[0]}")

        def _json(self, value: Any, status: int = 200) -> None:
            payload = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def _static(self, name: str) -> None:
            if name not in {"index.html", "app.css", "app.js", "exchange.js", "chat-import.js", "scheduled.js", "vendor/gsap.min.js",
                            "vendor/ScrollTrigger.min.js", "art/workflow.svg"}:
                self.send_error(404)
                return
            path = STATIC_DIR / name
            payload = path.read_bytes()
            content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            if name.endswith((".html", ".css", ".js")):
                content_type += "; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(payload)

        def _download(self, content: bytes, content_type: str, filename: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)

        def _exchange_get(self, store: Store, path: str) -> None:
            from .exchange import Exchange
            from .contracts import prompt_package
            try:
                if path == "/api/prompts/task-extractor/export":
                    self._download(json.dumps(prompt_package(), ensure_ascii=False, indent=2).encode("utf-8"),
                                   "application/json", "prompt.json")
                    return
                exchange = Exchange(store)
                parts = path.strip("/").split("/")
                if path == "/api/exchange/scopes":
                    self._json(exchange.scopes())
                elif path == "/api/exchange/candidates":
                    self._json(exchange.candidates())
                elif path == "/api/exchange/exports":
                    self._json(exchange.batches())
                elif len(parts) == 4 and parts[:3] == ["api", "exchange", "exports"]:
                    self._json(exchange.batch(parts[3]))
                elif len(parts) == 6 and parts[:3] == ["api", "exchange", "exports"] and parts[4] == "files":
                    content, content_type = exchange.file(parts[3], parts[5])
                    self._download(content, content_type, parts[5])
                else:
                    self.send_error(404)
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
            except Exception as exc:
                self._json({"error": f"本地交换读取失败：{type(exc).__name__}"}, 500)

        def _exchange_post(self, store: Store, path: str, payload: dict) -> None:
            from .exchange import Exchange
            from .contracts import loads_document
            try:
                exchange = Exchange(store)
                parts = path.strip("/").split("/")
                if path == "/api/exchange/exports":
                    result = exchange.create_export(
                        idempotency_key=payload.get("idempotency_key"),
                        scope_ids=payload.get("scope_ids"), mode=payload.get("mode", "pending"),
                        start=payload.get("start"), end=payload.get("end"), limit=payload.get("limit", 100))
                elif path in {"/api/exchange/messages/preview", "/api/exchange/results/preview"}:
                    document = payload.get("document")
                    if not isinstance(document, str):
                        raise ValueError("document 必须为 JSON 文本")
                    result = exchange.preview(parts[2], loads_document(document))
                elif path in {"/api/exchange/messages/apply", "/api/exchange/results/apply"}:
                    preview_id = payload.get("preview_id")
                    if not isinstance(preview_id, str):
                        raise ValueError("缺少 preview_id")
                    # Binding the kind prevents accidentally applying a message preview as a result.
                    with store.connect() as conn:
                        row = conn.execute("SELECT kind FROM exchange_previews WHERE id=?", (preview_id,)).fetchone()
                    if not row or row["kind"] != parts[2]:
                        raise ValueError("预检类型不匹配")
                    result = exchange.apply(preview_id)
                elif len(parts) == 5 and parts[:3] == ["api", "exchange", "exports"] and parts[4] == "cancel":
                    result = exchange.cancel(parts[3])
                elif len(parts) == 5 and parts[:3] == ["api", "exchange", "exports"] and parts[4] == "resume":
                    result = exchange.finish_export(parts[3])
                else:
                    self.send_error(404)
                    return
                self._json(result)
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
            except OSError:
                self._json({"error": "本地文件输出失败；请在批次列表恢复输出，不要重新提交另一批次"}, 500)
            except Exception as exc:
                self._json({"error": f"本地交换处理失败：{type(exc).__name__}"}, 500)

        def _host_ok(self) -> bool:
            hostname = (self.headers.get("Host", "").split(":", 1)[0].strip("[]").lower())
            if hostname == "localhost":
                return True
            try:
                return ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                return False

        def _post_origin_ok(self) -> bool:
            if self.headers.get("X-Requested-With") != "fetch":
                return False
            origin = self.headers.get("Origin")
            if not origin:
                return True
            parsed = urlsplit(origin)
            host = self.headers.get("Host", "").lower()
            return parsed.scheme == "http" and parsed.netloc.lower() == host

        def do_GET(self) -> None:
            if not self._host_ok():
                self.send_error(403)
                return
            parsed = urlsplit(self.path)
            store = Store(db_path, data_dir)
            if parsed.path.startswith("/api/scheduled/"):
                from .scheduled_results import ScheduledResults
                reader = ScheduledResults(store)
                if parsed.path == "/api/scheduled/status":
                    self._json(reader.status())
                elif parsed.path == "/api/scheduled/messages":
                    self._json(reader.messages(_limit(parsed.query)))
                elif parsed.path == "/api/scheduled/example":
                    example = {"format": "mta.scheduled-results", "version": "1.0", "results": [{
                        "provider": "chatgpt", "account_key": "personal", "task_id": "真实任务ID", "run_id": "真实运行ID",
                        "title": "任务标题", "source_time": "2026-10-20T08:00:00+08:00", "run_status": "completed",
                        "text": "这里放该次执行的完整结果，不是通知摘要", "completeness": "full"}]}
                    self._download(json.dumps(example, ensure_ascii=False, indent=2).encode("utf-8"), "application/json", "scheduled-results.example.json")
                elif parsed.path.startswith("/api/scheduled/messages/") and parsed.path.endswith("/raw"):
                    item_id = parsed.path.split("/")[-2]
                    with store.connect() as conn:
                        row = conn.execute("SELECT v.raw_path FROM source_items i JOIN sources s ON s.id=i.source_id JOIN item_versions v ON v.id=i.latest_version_id WHERE i.id=? AND s.kind IN ('codex_scheduled','chatgpt_scheduled')", (item_id,)).fetchone()
                    file = (store.data_dir / row[0]).resolve() if row else None
                    if not file or not file.is_relative_to((store.data_dir / "raw/scheduled").resolve()) or not file.is_file():
                        self._json({"error": "没有本地定时结果原文"}, 404)
                    else:
                        self._download(file.read_bytes(), "application/json", "scheduled-result.json")
                else:
                    self.send_error(404)
                return
            if parsed.path.startswith("/api/exchange/") or parsed.path == "/api/prompts/task-extractor/export":
                self._exchange_get(store, parsed.path)
            elif parsed.path.startswith("/api/chat-imports") or parsed.path.startswith("/api/chat-messages") or parsed.path == "/api/qq-local-status":
                from .chat_import import ChatImports
                imports = ChatImports(store)
                try:
                    if parsed.path == "/api/qq-local-status":
                        from .chat_reader import qq_local_status
                        self._json(qq_local_status(data_dir=store.data_dir))
                    elif parsed.path == "/api/chat-imports":
                        self._json(imports.jobs())
                    elif parsed.path.startswith("/api/chat-imports/"):
                        self._json(imports.job(parsed.path.split("/")[-1]))
                    elif parsed.path == "/api/chat-messages":
                        self._json(imports.messages(parse_qs(parsed.query).get("scope_id", [None])[0], _limit(parsed.query, 50, 200)))
                    elif parsed.path.startswith("/api/chat-messages/") and parsed.path.endswith("/raw"):
                        item_id = parsed.path.split("/")[-2]
                        with store.connect() as conn:
                            row = conn.execute("""SELECT v.raw_path FROM source_items i JOIN item_versions v ON v.id=i.latest_version_id
                                JOIN sources s ON s.id=i.source_id WHERE i.id=? AND s.kind IN ('wechat_chat','qq_chat')""", (item_id,)).fetchone()
                        if not row or not row[0]:
                            raise ValueError("原始消息不存在")
                        file = (store.data_dir / row[0]).resolve()
                        if not file.is_relative_to((store.data_dir / "raw/chat").resolve()):
                            raise ValueError("不是本机聊天扫描保存的原始记录")
                        self._download(file.read_bytes(), "application/json", "chat-message.json")
                    else:
                        self.send_error(404)
                except (ValueError, OSError) as exc:
                    self._json({"error": str(exc)}, 400)
            elif parsed.path == "/":
                self._static("index.html")
            elif parsed.path == "/app.css":
                self._static("app.css")
            elif parsed.path == "/app.js":
                self._static("app.js")
            elif parsed.path == "/exchange.js":
                self._static("exchange.js")
            elif parsed.path == "/chat-import.js":
                self._static("chat-import.js")
            elif parsed.path == "/scheduled.js":
                self._static("scheduled.js")
            elif parsed.path == "/vendor/gsap.min.js":
                self._static("vendor/gsap.min.js")
            elif parsed.path == "/vendor/ScrollTrigger.min.js":
                self._static("vendor/ScrollTrigger.min.js")
            elif parsed.path == "/art/workflow.svg":
                self._static("art/workflow.svg")
            elif parsed.path == "/api/overview":
                self._json(_overview(store))
            elif parsed.path == "/api/mail":
                self._json(_mail_items(store, _limit(parsed.query)))
            elif parsed.path == "/api/events":
                self._json(_events(store, _limit(parsed.query)))
            elif parsed.path == "/api/tasks":
                self._json(store.tasks())
            elif parsed.path == "/api/outbox":
                self._json(store.outbox_preview(_limit(parsed.query)))
            elif parsed.path == "/api/scans":
                self._json(_scans(store, _limit(parsed.query)))
            elif parsed.path == "/api/subscriptions":
                self._json(store.chat_subscriptions())
            elif parsed.path == "/api/chat-tags":
                self._json(store.chat_tags())
            elif parsed.path == "/api/chat-discovery":
                platform = parse_qs(parsed.query).get("platform", [None])[0]
                try:
                    self._json(store.chat_discovery_results(platform))
                except ValueError as exc:
                    self._json({"error": str(exc)}, 400)
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            if not self._host_ok() or not self._post_origin_ok():
                self.send_error(403)
                return
            parsed = urlsplit(self.path)
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._json({"error": "Content-Length 无效"}, 400)
                return
            max_body = 8 * 1024 * 1024 if parsed.path.startswith(("/api/exchange/", "/api/scheduled/import")) else 16_384
            if length < 0 or length > max_body:
                self.send_error(413)
                return
            try:
                body = self.rfile.read(length) if length else b"{}"
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("请求体必须是 JSON 对象")
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                self._json({"error": f"无效请求：{exc}"}, 400)
                return
            parsed = urlsplit(self.path)
            store = Store(db_path, data_dir)
            if parsed.path.startswith("/api/scheduled/"):
                from .scheduled_results import ScheduledResults, start_background
                reader = ScheduledResults(store)
                try:
                    if parsed.path == "/api/scheduled/scan":
                        job = reader.start()
                        start_background(reader, job["id"])
                        self._json(job, 202)
                    elif parsed.path == "/api/scheduled/import":
                        self._json(reader.import_packet(payload))
                    else:
                        self.send_error(404)
                except (ValueError, TypeError, OSError) as exc:
                    self._json({"error": str(exc)}, 400)
                return
            if parsed.path.startswith("/api/chat-imports"):
                from .chat_import import ChatImports, start_background
                imports = ChatImports(store)
                try:
                    if parsed.path == "/api/chat-imports":
                        job = imports.create(platform=payload.get("platform", "wechat"), mode=payload.get("mode", "full"),
                            scope_ids=payload.get("scope_ids"), start=payload.get("start"), end=payload.get("end"),
                            batch_size=payload.get("batch_size", 200))
                        start_background(imports, job["id"])
                        self._json(job, 202)
                    elif parsed.path.endswith("/resume"):
                        job_id = parsed.path.split("/")[-2]
                        job = imports.job(job_id)
                        if not job["resumable"]:
                            raise ValueError("任务正在运行、已完成或已取消，不能恢复")
                        start_background(imports, job_id)
                        self._json(job, 202)
                    elif parsed.path.endswith("/cancel"):
                        self._json(imports.cancel(parsed.path.split("/")[-2]))
                    else:
                        self.send_error(404)
                except (ValueError, TypeError) as exc:
                    self._json({"error": str(exc)}, 400)
                return
            if parsed.path.startswith("/api/exchange/"):
                self._exchange_post(store, parsed.path, payload)
                return
            if parsed.path == "/api/chat-discovery":
                platform = payload.get("platform")
                if platform not in {"wechat", "qq"}:
                    self._json({"error": "platform 只支持 wechat 或 qq"}, 400)
                    return
                run_id = store.start_chat_discovery(platform)
                try:
                    result = discover_chat_candidates(platform, data_dir=store.data_dir) if platform == "qq" else discover_chat_candidates(platform)
                    candidates = result.get("candidates", [])
                    warnings = result.get("warnings", [])
                    warning = "; ".join(str(item) for item in warnings) or None
                    store.finish_chat_discovery(run_id, candidates, error=warning)
                    self._json({"run_id": run_id, **store.chat_discovery_results(platform)[platform]})
                except ScannerUnavailable as exc:
                    store.fail_chat_discovery(run_id, str(exc))
                    self._json({"error": str(exc), "run_id": run_id}, 501)
                except Exception as exc:
                    message = f"扫描失败：{type(exc).__name__}: {exc}"
                    store.fail_chat_discovery(run_id, message)
                    self._json({"error": message, "run_id": run_id}, 502)
            elif parsed.path == "/api/subscriptions/transfer":
                try:
                    self._json(store.transfer_chat_subscriptions(candidate_ids=payload.get("candidate_ids"),
                        enable_ids=payload.get("enable_ids"),disable_ids=payload.get("disable_ids"),
                        full_scan_enabled=payload.get("full_scan_enabled",False)))
                except (ValueError,TypeError) as exc:
                    self._json({"error":str(exc)},400)
            elif parsed.path == "/api/subscriptions/select":
                try:
                    subscriptions = store.select_discovered_chat_candidates(
                        payload.get("candidate_ids", []),
                        full_scan_enabled=payload.get("full_scan_enabled", False),
                    )
                    self._json({"subscriptions": subscriptions}, 201)
                except (ValueError, TypeError) as exc:
                    self._json({"error": str(exc)}, 400)
            elif parsed.path == "/api/subscriptions":
                try:
                    if "tags" in payload and not isinstance(payload["tags"], list):
                        raise ValueError("tags 必须为数组")
                    subscription = store.upsert_chat_subscription(
                        payload.get("platform", ""), payload.get("account_key", ""),
                        payload.get("conversation_key", ""), payload.get("display_name", ""),
                        enabled=payload.get("enabled", False),
                        full_scan_enabled=payload.get("full_scan_enabled", False),
                        tags=payload.get("tags"),
                    )
                    self._json(subscription, 201)
                except (ValueError, TypeError) as exc:
                    self._json({"error": str(exc)}, 400)
            elif parsed.path.startswith("/api/subscriptions/") and parsed.path.endswith("/settings"):
                scope_id = parsed.path[len("/api/subscriptions/"):-len("/settings")].strip("/")
                try:
                    if "tags" in payload and not isinstance(payload["tags"], list):
                        raise ValueError("tags 必须为数组")
                    enabled = payload.get("enabled")
                    full_scan_enabled = payload.get("full_scan_enabled")
                    store.set_chat_subscription_options(
                        scope_id, enabled=enabled, full_scan_enabled=full_scan_enabled,
                        tags=payload.get("tags"),
                    )
                    subscription = next(item for item in store.chat_subscriptions() if item["id"] == scope_id)
                    self._json(subscription)
                except (KeyError, ValueError, TypeError, StopIteration) as exc:
                    self._json({"error": str(exc) or "订阅不存在"}, 400)
            elif parsed.path == "/api/scan":
                try:
                    credentials = load_saved_credentials()
                    if not credentials:
                        self._json({"error": "未找到已保存的 QQ 邮箱配置"}, 409)
                        return
                    result = scan_mailbox(store, credentials["address"], credentials["auth_code"])
                    safe = {key: value for key, value in result.items() if key != "account"}
                    self._json(safe, 200 if result.get("ok") else 502)
                except Exception as exc:
                    self._json({"error": f"扫描失败：{type(exc).__name__}: {exc}"}, 502)
            elif parsed.path == "/api/scan-range":
                start, end = payload.get("start"), payload.get("end")
                if not isinstance(start, str) or not isinstance(end, str):
                    self._json({"error": "请提供开始日期和结束日期"}, 400)
                    return
                try:
                    credentials = load_saved_credentials()
                    if not credentials:
                        self._json({"error": "未找到已保存的 QQ 邮箱配置"}, 409)
                        return
                    result = scan_mailbox_range(store, credentials["address"], credentials["auth_code"],
                                                start, end)
                    safe = {key: value for key, value in result.items() if key != "account"}
                    # The scan request succeeded even if individual folders failed.
                    # Keep per-folder status in the payload so the UI can show details.
                    self._json(safe)
                except Exception as exc:
                    self._json({"error": f"范围扫描失败：{type(exc).__name__}: {exc}"}, 502)
            elif parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/status"):
                task_id = parsed.path[len("/api/tasks/"):-len("/status")].strip("/")
                status = payload.get("status")
                try:
                    store.set_task_status(task_id, status)
                    self._json({"ok": True, "task_id": task_id, "status": status})
                except (KeyError, ValueError, TypeError) as exc:
                    self._json({"error": str(exc)}, 400)
            else:
                self.send_error(404)

    return DashboardHandler


def serve_ui(db_path: str | Path, data_dir: str | Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    try:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("Web UI 仅允许绑定本机回环地址")
    except ValueError as exc:
        if host != "localhost":
            raise ValueError("Web UI 仅允许绑定本机回环地址") from exc
    server = _Server((host, port), make_handler(db_path, data_dir))
    print(f"本地任务面板已启动：http://{host}:{server.server_port}/")
    print("仅监听本机；扫描邮箱只会在面板中点击确认后执行。按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

