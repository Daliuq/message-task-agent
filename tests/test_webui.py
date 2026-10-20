from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from taskagent.store import Store, utc_now
from taskagent.webui import make_handler


class WebUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = root / "test.sqlite3"
        self.data = root / "data"
        store = Store(self.db, self.data)
        self.task_id = str(uuid.uuid4())
        now = utc_now()
        with store.transaction() as conn:
            conn.execute(
                """INSERT INTO tasks(id,identity_key,title,description,status,priority,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (self.task_id, "test:ui", "UI 测试任务", "面板状态更新验证", "open", 2, now, now),
            )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.db, self.data))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def test_dashboard_and_overview_are_served_locally(self):
        with urllib.request.urlopen(self.base + "/", timeout=3) as response:
            self.assertEqual(response.status, 200)
            page = response.read().decode("utf-8")
            self.assertIn("消息任务台", page)
            self.assertIn("扫描并发现会话", page)
            self.assertIn('id="subscriptionConfigDialog"', page)
            self.assertIn('id="subscriptionEnabled" type="checkbox">', page)
            self.assertIn('id="subscriptionFullScan" type="checkbox">', page)
        with urllib.request.urlopen(self.base + "/api/overview", timeout=3) as response:
            value = json.loads(response.read())
        self.assertEqual(value["counts"]["tasks"], 1)
        self.assertEqual(value["open_tasks"], 1)

    def test_local_visual_assets_and_animation_libraries_are_served(self):
        for path, marker in (
            ("/app.css", "Editorial dashboard refinement"),
            ("/app.js", "matchMedia"),
            ("/vendor/gsap.min.js", "GreenSock"),
            ("/vendor/ScrollTrigger.min.js", "ScrollTrigger"),
            ("/art/workflow.svg", "<svg"),
        ):
            with urllib.request.urlopen(self.base + path, timeout=3) as response:
                body = response.read().decode("utf-8", errors="replace")
                self.assertEqual(response.status, 200)
                self.assertIn(marker, body)

    def test_task_status_change_is_persisted(self):
        request = urllib.request.Request(
            self.base + f"/api/tasks/{self.task_id}/status",
            data=json.dumps({"status": "completed"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Requested-With": "fetch"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(json.loads(response.read())["status"], "completed")
        self.assertEqual(Store(self.db, self.data).tasks()[0]["status"], "completed")

    def test_mutating_request_requires_fetch_header(self):
        request = urllib.request.Request(
            self.base + f"/api/tasks/{self.task_id}/status",
            data=b'{"status":"completed"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(raised.exception.code, 403)

    def test_range_scan_rejects_missing_bounds_before_mailbox_access(self):
        request = urllib.request.Request(
            self.base + "/api/scan-range",
            data=b"{}",
            headers={"Content-Type": "application/json", "X-Requested-With": "fetch"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(raised.exception.code, 400)

    def test_chat_subscriptions_can_be_created_listed_and_paused(self):
        request = urllib.request.Request(
            self.base + "/api/subscriptions",
            data=json.dumps({
                "platform": "wechat", "account_key": "test-account",
                "conversation_key": "stable-chat-id", "display_name": "测试群",
                "enabled": True, "full_scan_enabled": True,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Requested-With": "fetch"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            created = json.loads(response.read())
            self.assertEqual(response.status, 201)
        with urllib.request.urlopen(self.base + "/api/subscriptions", timeout=3) as response:
            self.assertEqual(json.loads(response.read())[0]["id"], created["id"])

        request = urllib.request.Request(
            self.base + f"/api/subscriptions/{created['id']}/settings",
            data=json.dumps({"enabled": False, "full_scan_enabled": True}).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Requested-With": "fetch"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            updated = json.loads(response.read())
        self.assertFalse(updated["enabled"])
        self.assertTrue(updated["full_scan_enabled"])

    def test_new_chat_subscription_defaults_to_unselected(self):
        request = urllib.request.Request(
            self.base + "/api/subscriptions",
            data=json.dumps({
                "platform": "qq", "account_key": "default-account",
                "conversation_key": "default-chat", "display_name": "默认关闭群",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Requested-With": "fetch"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            created = json.loads(response.read())
        self.assertFalse(created["enabled"])
        self.assertFalse(created["full_scan_enabled"])

    def test_chat_tags_can_be_saved_reused_and_cleared_through_settings(self):
        store = Store(self.db, self.data)
        chat = store.upsert_chat_subscription("qq", "acct", "g", "测试群")
        def save(payload):
            request = urllib.request.Request(self.base + f"/api/subscriptions/{chat['id']}/settings",
                data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "X-Requested-With": "fetch"}, method="POST")
            with urllib.request.urlopen(request, timeout=3) as response:
                return json.loads(response.read())
        updated = save({"enabled": False, "full_scan_enabled": False, "tags": ["工作", "生活"]})
        self.assertEqual({t["name"] for t in updated["tags"]}, {"工作", "生活"})
        with urllib.request.urlopen(self.base + "/api/chat-tags", timeout=3) as response:
            catalog = json.loads(response.read())
        self.assertEqual(len(catalog), 2)
        self.assertTrue(all(t["chat_count"] == 1 for t in catalog))
        self.assertEqual(save({"enabled": True, "full_scan_enabled": False})["tags"], updated["tags"])
        for invalid in (None, "生活", [False], [" "], ["x" * 33]):
            with self.subTest(tags=invalid), self.assertRaises(urllib.error.HTTPError) as raised:
                save({"enabled": True, "full_scan_enabled": True, "tags": invalid})
            self.assertEqual(raised.exception.code, 400)
        self.assertFalse(save({"enabled": False, "full_scan_enabled": False, "tags": []})["tags"])

    def test_chat_tag_ui_is_present_and_names_are_returned_as_data(self):
        store = Store(self.db, self.data)
        store.upsert_chat_subscription("wechat", "acct", "g", "测试群", tags=['<img src=x onerror=alert(1)>'])
        with urllib.request.urlopen(self.base + "/api/subscriptions", timeout=3) as response:
            value = json.loads(response.read())
        self.assertEqual(value[0]["tags"][0]["name"], '<img src=x onerror=alert(1)>')
        with urllib.request.urlopen(self.base + "/", timeout=3) as response:
            page = response.read().decode()
        for control in ("subscriptionTagFilter", "subscriptionTagChoices", "subscriptionTagName"):
            self.assertIn(f'id="{control}"', page)

    def test_range_scan_returns_partial_folder_errors_as_structured_result(self):
        result = {
            "ok": False, "folders": [
                {"folder": "INBOX", "status": "failed", "error": "INTERNALDATE parse failed"},
                {"folder": "Drafts", "status": "ok", "error": None},
            ],
        }
        request = urllib.request.Request(
            self.base + "/api/scan-range",
            data=json.dumps({"start": "2026-10-18", "end": "2026-10-19"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Requested-With": "fetch"},
            method="POST",
        )
        with patch("taskagent.webui.load_saved_credentials", return_value={"address": "test@example.com", "auth_code": "test-code"}), \
             patch("taskagent.webui.scan_mailbox_range", return_value=result):
            with urllib.request.urlopen(request, timeout=3) as response:
                self.assertEqual(response.status, 200)
                body = json.loads(response.read())
        self.assertFalse(body["ok"])
        self.assertEqual(body["folders"][0]["error"], "INTERNALDATE parse failed")

    def test_subscription_settings_reject_missing_flags(self):
        request = urllib.request.Request(
            self.base + "/api/subscriptions/missing/settings",
            data=b"{}",
            headers={"Content-Type": "application/json", "X-Requested-With": "fetch"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(raised.exception.code, 400)

    def test_chat_discovery_and_selection_use_mocked_adapter(self):
        result = {"candidates": [{
            "account_key": "wxid-test", "account_name": "测试账号",
            "conversation_key": "room-001@chatroom", "display_name": "项目群",
            "message_count": 12, "conversation_type": "group",
        }], "warnings": []}
        request = urllib.request.Request(
            self.base + "/api/chat-discovery",
            data=json.dumps({"platform": "wechat"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Requested-With": "fetch"},
            method="POST",
        )
        with patch("taskagent.webui.discover_chat_candidates", return_value=result):
            with urllib.request.urlopen(request, timeout=3) as response:
                scan = json.loads(response.read())
        self.assertEqual(scan["run"]["status"], "completed")
        self.assertEqual(len(scan["candidates"]), 1)
        self.assertFalse(scan["candidates"][0]["subscribed"])

        request = urllib.request.Request(
            self.base + "/api/subscriptions/select",
            data=json.dumps({"candidate_ids": [scan["candidates"][0]["id"]]}).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Requested-With": "fetch"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            selected = json.loads(response.read())
            self.assertEqual(response.status, 201)
        self.assertEqual(selected["subscriptions"][0]["display_name"], "项目群")
        self.assertTrue(selected["subscriptions"][0]["enabled"])
        self.assertFalse(selected["subscriptions"][0]["full_scan_enabled"])

    def test_chat_discovery_endpoint_records_qq_adapter_unavailable(self):
        request = urllib.request.Request(
            self.base + "/api/chat-discovery",
            data=b'{"platform":"qq"}',
            headers={"Content-Type": "application/json", "X-Requested-With": "fetch"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(raised.exception.code, 501)
        value = json.loads(raised.exception.read())
        self.assertIn("请先取钥", value["error"])
        history = Store(self.db, self.data).chat_discovery_results("qq")["qq"]
        self.assertEqual(history["run"]["status"], "failed")

    def test_dashboard_uses_discovery_selection_instead_of_manual_add(self):
        with urllib.request.urlopen(self.base + "/", timeout=3) as response:
            page = response.read().decode("utf-8")
        self.assertIn('data-discover-chat="wechat"', page)
        self.assertIn('data-discover-chat="qq"', page)
        self.assertIn('id="chatCandidateList"', page)
        self.assertIn('id="saveChatSelections"', page)
        self.assertNotIn('id="openSubscriptionConfig"', page)
        self.assertIn("不支持手动新建或修改", page)


if __name__ == "__main__":
    unittest.main()
