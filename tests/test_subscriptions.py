from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from taskagent.store import Store


class ChatSubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = Store(root / "db.sqlite3", root / "data")

    def tearDown(self):
        self.temp.cleanup()

    def test_upsert_is_idempotent_and_updates_subscription_fields(self):
        first = self.store.upsert_chat_subscription(
            "wechat", "MyAccount", "chat-001", "项目群"
        )
        self.assertFalse(first["enabled"])
        self.assertFalse(first["full_scan_enabled"])
        second = self.store.upsert_chat_subscription(
            "WECHAT", "myaccount", "chat-001", "项目群（更新）",
            enabled=False, full_scan_enabled=False,
        )

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.chat_subscriptions()), 1)
        self.assertEqual(second["display_name"], "项目群（更新）")
        self.assertFalse(second["enabled"])
        self.assertFalse(second["full_scan_enabled"])

    def test_platform_and_scan_flags_filter_active_subscriptions(self):
        wechat = self.store.upsert_chat_subscription("wechat", "account", "room-1", "微信群")
        self.store.upsert_chat_subscription(
            "qq", "account", "room-1", "QQ群", enabled=True, full_scan_enabled=False
        )
        self.store.set_chat_subscription_options(
            wechat["id"], enabled=False, full_scan_enabled=True
        )

        self.assertEqual(self.store.active_chat_subscriptions("wechat"), [])
        self.assertEqual(self.store.active_chat_subscriptions("wechat", full_scan=True), [])
        self.assertEqual(len(self.store.active_chat_subscriptions("qq")), 1)
        self.assertEqual(self.store.active_chat_subscriptions("qq", full_scan=True), [])

        self.store.set_chat_subscription_options(
            self.store.chat_subscriptions()[1]["id"], enabled=True, full_scan_enabled=True
        )
        self.assertEqual(len(self.store.active_chat_subscriptions("qq", full_scan=True)), 1)

    def test_rejects_invalid_platform_flags_and_unknown_scope(self):
        with self.assertRaises(ValueError):
            self.store.upsert_chat_subscription("email", "acct", "id", "名称")
        with self.assertRaises(ValueError):
            self.store.upsert_chat_subscription("qq", "acct", "id", "名称", enabled=1)
        with self.assertRaises(KeyError):
            self.store.set_chat_subscription_options(
                "missing", enabled=True, full_scan_enabled=True
            )

    def test_chat_has_many_tags_and_tags_are_shared_across_chats_and_platforms(self):
        first = self.store.upsert_chat_subscription("qq", "one", "group-1", "项目群", tags=["工作", "生活"])
        second = self.store.upsert_chat_subscription("wechat", "two", "group-2", "团队群", tags=["工作"])
        self.assertEqual(len(first["tags"]), 2)
        shared = next(tag for tag in first["tags"] if tag["name"] == "工作")
        self.assertEqual(second["tags"][0]["id"], shared["id"])
        counts = {tag["name"]: tag["chat_count"] for tag in self.store.chat_tags()}
        self.assertEqual(counts, {"工作": 2, "生活": 1})
        self.assertFalse(first["enabled"])
        self.assertFalse(first["full_scan_enabled"])

    def test_chat_tags_normalize_and_deduplicate_without_renaming_catalog(self):
        first = self.store.upsert_chat_subscription("qq", "acct", "g", "群", tags=[" Work ", "work", "e\u0301", "é"])
        second = self.store.upsert_chat_subscription("qq", "acct", "g2", "群2", tags=["WORK", "é"])
        self.assertEqual({t["name"] for t in first["tags"]}, {"Work", "é"})
        self.assertEqual(first["tags"], second["tags"])
        self.assertEqual(len(self.store.chat_tags()), 2)

    def test_omitted_tags_preserve_but_empty_array_clears_only_this_chat(self):
        first = self.store.upsert_chat_subscription("qq", "acct", "g1", "群1", tags=["工作"])
        self.store.upsert_chat_subscription("qq", "acct", "g2", "群2", tags=["工作"])
        self.store.set_chat_subscription_options(first["id"], enabled=True, full_scan_enabled=False)
        self.assertTrue(next(s for s in self.store.chat_subscriptions() if s["id"] == first["id"])["tags"])
        self.store.set_chat_subscription_options(first["id"], enabled=True, full_scan_enabled=False, tags=[])
        self.assertFalse(next(s for s in self.store.chat_subscriptions() if s["id"] == first["id"])["tags"])
        self.assertEqual(self.store.chat_tags()[0]["chat_count"], 1)

    def test_invalid_tags_do_not_partially_change_flags_or_catalog(self):
        chat = self.store.upsert_chat_subscription("qq", "acct", "g", "群", tags=["生活"])
        for invalid in ("工作", [1], [""], ["x" * 33], ["foo\nbar"], ["正常", "bad\x00"], [str(i) for i in range(21)]):
            with self.subTest(tags=invalid), self.assertRaises(ValueError):
                self.store.set_chat_subscription_options(chat["id"], enabled=True, full_scan_enabled=True, tags=invalid)
        saved = self.store.chat_subscriptions()[0]
        self.assertFalse(saved["enabled"])
        self.assertFalse(saved["full_scan_enabled"])
        self.assertEqual(saved["tags"], chat["tags"])
        self.assertEqual(len(self.store.chat_tags()), 1)

    def test_tags_survive_reinitialization_and_discovery_reselection(self):
        chat = self.store.upsert_chat_subscription("qq", "acct", "g", "旧名", tags=["工作", "生活"])
        store = Store(self.store.db_path, self.store.data_dir)
        store.upsert_chat_subscription("qq", "acct", "g", "新名")
        run = store.start_chat_discovery("qq")
        store.finish_chat_discovery(run, [{"account_key": "acct", "conversation_key": "g", "display_name": "扫描名"}])
        candidate = store.chat_discovery_results("qq")["qq"]["candidates"][0]
        selected = store.select_discovered_chat_candidates([candidate["id"]])[0]
        self.assertEqual(selected["tags"], chat["tags"])

    def test_tags_apply_only_to_chat_scopes_and_links_have_foreign_keys(self):
        self.store.initialize_mail_cursor("demo@example.org", "INBOX", "1", 0)
        with self.store.connect() as conn:
            mail_scope = conn.execute("SELECT id FROM source_scopes").fetchone()[0]
        with self.assertRaises(KeyError):
            self.store.set_chat_subscription_options(mail_scope, enabled=True, full_scan_enabled=True, tags=["工作"])
        self.assertEqual(self.store.chat_tags(), [])
        chat = self.store.upsert_chat_subscription("qq", "acct", "g", "群", tags=["工作"])
        with self.store.transaction() as conn:
            conn.execute("DELETE FROM source_scopes WHERE id=?", (chat["id"],))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM chat_scope_tags").fetchone()[0], 0)
        self.assertEqual(self.store.chat_tags()[0]["chat_count"], 0)

    def test_discovery_persists_candidates_and_only_selected_chats_become_subscriptions(self):
        run_id = self.store.start_chat_discovery("wechat")
        self.store.finish_chat_discovery(run_id, [
            {
                "account_key": "wxid-test", "account_name": "测试账号",
                "conversation_key": "room-001@chatroom", "display_name": "项目群",
                "message_count": 17, "conversation_type": "group",
            },
            {
                "account_key": "wxid-test", "account_name": "测试账号",
                "conversation_key": "friend-001", "display_name": "联系人",
                "message_count": 4, "conversation_type": "private",
            },
        ])
        result = self.store.chat_discovery_results("wechat")["wechat"]
        self.assertEqual(result["run"]["status"], "completed")
        self.assertEqual(result["run"]["item_count"], 2)
        self.assertFalse(any(item["subscribed"] for item in result["candidates"]))
        selected = self.store.select_discovered_chat_candidates([result["candidates"][0]["id"]])
        self.assertEqual(len(selected), 1)
        self.assertTrue(selected[0]["enabled"])
        self.assertFalse(selected[0]["full_scan_enabled"])
        latest = self.store.chat_discovery_results("wechat")["wechat"]["candidates"]
        self.assertTrue(next(item for item in latest if item["id"] == result["candidates"][0]["id"])["subscribed"])
        self.assertEqual(len(self.store.chat_subscriptions()), 1)

    def test_candidate_from_an_old_scan_cannot_be_selected_after_new_scan_starts(self):
        old_run = self.store.start_chat_discovery("wechat")
        self.store.finish_chat_discovery(old_run, [{
            "account_key": "acct", "conversation_key": "room", "display_name": "旧结果",
        }])
        old_candidate = self.store.chat_discovery_results("wechat")["wechat"]["candidates"][0]
        self.store.start_chat_discovery("wechat")
        with self.assertRaisesRegex(ValueError, "最新扫描结果"):
            self.store.select_discovered_chat_candidates([old_candidate["id"]])


if __name__ == "__main__":
    unittest.main()
