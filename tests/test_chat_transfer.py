import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from taskagent.store import Store


class ChatTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();root=Path(self.temp.name)
        self.store=Store(root/'agent.sqlite3',root/'data')
        self.existing=self.store.upsert_chat_subscription('wechat','wxid-test','test@chatroom','原订阅',
            enabled=True,full_scan_enabled=True,tags=['工作','招聘'])
        self.candidate={"account_key":"12345","conversation_key":"group:9001","display_name":"新 QQ 群",
                        "conversation_type":"group","message_count":3}
        run=self.store.start_chat_discovery('qq');self.store.finish_chat_discovery(run,[self.candidate])
        self.candidate_id=self.store.chat_discovery_results('qq')['qq']['candidates'][0]['id']

    def tearDown(self):self.temp.cleanup()

    def test_batch_add_and_disable_only_changes_explicit_scopes(self):
        other=self.store.upsert_chat_subscription('qq','12345','group:other','保留订阅',enabled=True,full_scan_enabled=True)
        r=self.store.transfer_chat_subscriptions(candidate_ids=[self.candidate_id],disable_ids=[self.existing['id']])
        by_id={s['id']:s for s in r['subscriptions']}
        self.assertEqual((r['added'],r['disabled']),(1,1))
        self.assertFalse(by_id[self.existing['id']]['enabled'])
        self.assertFalse(by_id[self.existing['id']]['full_scan_enabled'])
        self.assertEqual([t['name'] for t in by_id[self.existing['id']]['tags']],['工作','招聘'])
        self.assertTrue(by_id[other['id']]['enabled'])
        qq=next(s for s in r['subscriptions'] if s['conversation_key']=='group:9001')
        self.assertTrue(qq['enabled']);self.assertFalse(qq['full_scan_enabled'])
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM source_items').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM chat_import_jobs').fetchone()[0],0) if c.execute("SELECT 1 FROM sqlite_master WHERE name='chat_import_jobs'").fetchone() else None

    def test_invalid_scope_rolls_back_entire_batch(self):
        with self.assertRaises(ValueError):self.store.transfer_chat_subscriptions(
            candidate_ids=[self.candidate_id],disable_ids=['does-not-exist'])
        self.assertEqual(len(self.store.chat_subscriptions()),1)
        self.assertTrue(self.store.chat_subscriptions()[0]['enabled'])

    def test_stale_candidate_rolls_back_disables(self):
        run=self.store.start_chat_discovery('qq');self.store.finish_chat_discovery(run,[self.candidate])
        with self.assertRaises(ValueError):self.store.transfer_chat_subscriptions(
            candidate_ids=[self.candidate_id],disable_ids=[self.existing['id']])
        self.assertTrue(self.store.chat_subscriptions()[0]['enabled'])

    def test_reenable_retains_identity_and_tags(self):
        self.store.transfer_chat_subscriptions(disable_ids=[self.existing['id']])
        r=self.store.transfer_chat_subscriptions(enable_ids=[self.existing['id']],full_scan_enabled=True)
        self.assertEqual(r['enabled'],1)
        restored=r['subscriptions'][0]
        self.assertEqual(restored['id'],self.existing['id'])
        self.assertTrue(restored['enabled']);self.assertTrue(restored['full_scan_enabled'])
        self.assertEqual(len(restored['tags']),2)

    def test_conflicting_or_malformed_deltas_are_rejected(self):
        for payload in ({},{'enable_ids':'bad'},{'disable_ids':[self.existing['id']]*2},
            {'enable_ids':[self.existing['id']],'disable_ids':[self.existing['id']]},
            {'candidate_ids':[self.candidate_id],'full_scan_enabled':'yes'}):
            with self.assertRaises(ValueError):self.store.transfer_chat_subscriptions(**payload)


if __name__=='__main__':unittest.main()
