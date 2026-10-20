import hashlib
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from taskagent.chat_import import ChatImports
from taskagent.store import Store
from taskagent.wechat_message_worker import read_pages
from taskagent.exchange import Exchange

USER = 'group@chatroom'
TABLE = 'Msg_' + hashlib.md5(USER.encode()).hexdigest()


class FakeDB:
    def __init__(self, root): self.root = Path(root)
    def _message_dbs(self): return [p.name for p in sorted(self.root.glob('message_*.db'))]
    def _open(self, rel):
        conn = sqlite3.connect(self.root / rel)
        conn.row_factory = sqlite3.Row
        return conn
    def _shard_label(self, conn): return Path(conn.execute('PRAGMA database_list').fetchone()[2]).name
    def _name2id_map(self, conn): return {1: 'sender-demo'}
    def _msg_row_to_dict(self, row, maps):
        return {'content': row['message_content'], 'type': '文本', 'sender_username': 'sender-demo'}


def source_files(root):
    root.mkdir()
    for part in range(2):
        with sqlite3.connect(root / f'message_{part}.db') as conn:
            conn.execute(f'CREATE TABLE "{TABLE}"(local_id INTEGER PRIMARY KEY,server_id INTEGER,create_time INTEGER,sort_seq INTEGER,message_content TEXT,packed_info_data BLOB)')
            conn.executemany(f'INSERT INTO "{TABLE}" VALUES(?,?,?,?,?,?)',
                [(i, part*100+i, 1792425600+i, 100, f'合成消息 {part}-{i}', b'\x00\xff') for i in range(1,4)])


class ChatImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root/'source'
        source_files(self.source)
        self.store = Store(self.root/'agent.sqlite3', self.root/'data')
        self.scope = self.store.upsert_chat_subscription('wechat','account-demo',USER,'合成测试群',enabled=True,full_scan_enabled=True,tags=['工作'])
        self.imports = ChatImports(self.store)
    def tearDown(self): self.tmp.cleanup()
    def reader(self, scope, config, checkpoint, live):
        yield from read_pages(FakeDB(self.source), scope['conversation_key'], config, checkpoint, live)
    def create(self, **kwargs):
        return self.imports.create(scope_ids=[self.scope['id']],batch_size=2,**kwargs)
    def run_job(self, **kwargs):
        return self.imports.run(self.create(**kwargs)['id'],self.reader)
    def test_full_import_saves_all_shards_and_raw_binary_fields(self):
        result=self.run_job()
        self.assertEqual(result['status'],'completed')
        self.assertEqual((result['processed'],result['items'],result['versions']),(6,6,6))
        messages=self.imports.messages()
        self.assertEqual(len(messages),6)
        raw=json.loads((self.store.data_dir/messages[0]['raw_path']).read_text(encoding='utf-8'))
        self.assertEqual(raw['raw_row']['packed_info_data'],{'$base64':'AP8='})
        self.assertEqual(len(self.store.tasks()),0)
        self.assertEqual(len(self.store.outbox_preview()),0)
        self.assertEqual(self.store.chat_subscriptions()[0]['tags'],self.scope['tags'])
        with self.store.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM event_consumptions').fetchone()[0],0)
    def test_repeated_full_import_is_idempotent_and_no_extra_events(self):
        self.run_job()
        second=self.run_job()
        self.assertEqual((second['processed'],second['items'],second['versions']),(6,0,0))
        with self.store.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM events').fetchone()[0],6)
    def test_incremental_keeps_tied_sequences_and_late_timestamp_new_primary_keys(self):
        self.run_job()
        self.assertEqual(self.run_job(mode='incremental')['processed'],0)
        with sqlite3.connect(self.source/'message_0.db') as conn:
            conn.execute(f'INSERT INTO "{TABLE}" VALUES(4,4,1792300000,100,?,?)',('补同步旧时间消息',b''))
        delta=self.run_job(mode='incremental')
        self.assertEqual((delta['processed'],delta['items']),(1,1))
    def test_range_does_not_advance_incremental_cursors(self):
        result=self.run_job(mode='range',start='2026-10-20',end='2026-10-20')
        self.assertEqual(result['status'],'completed')
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM source_cursors WHERE cursor_name='chat_native_v1'").fetchone()[0],0)
        # Both boundary days inclusive in UI/CLI; source timestamps are in the selected Beijing day.
        self.assertEqual(result['processed'],6)
    def test_failed_job_resumes_committed_page_without_duplicate_counting(self):
        job=self.create()
        def failing(*args):
            for frame in self.reader(*args):
                yield frame
                if frame['type']=='batch': raise RuntimeError('synthetic interruption')
        result=self.imports.run(job['id'],failing)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['processed'],2)
        resumed=ChatImports(Store(self.store.db_path,self.store.data_dir)).run(job['id'],self.reader)
        self.assertEqual(resumed['status'],'completed')
        self.assertEqual((resumed['processed'],resumed['items']),(6,6))
    def test_bad_second_record_rolls_back_page_and_checkpoint(self):
        job=self.create()
        original=self.imports._raw_path
        with patch.object(self.imports,'_raw_path',side_effect=[original({'temporary':'orphan'}),OSError('disk full')]):
            result=self.imports.run(job['id'],self.reader)
        self.assertEqual(result['processed'],0)
        self.assertEqual(self.imports.messages(),[])
        self.assertEqual(result['scopes'][0]['checkpoint'].get('positions',{}),{})
        self.assertEqual(self.imports.run(job['id'],self.reader)['items'],6)
    def test_missing_done_or_premature_done_never_claims_complete(self):
        def missing(*args):
            for frame in self.reader(*args):
                if frame['type']=='batch': return
                yield frame
        first=self.imports.run(self.create()['id'],missing)
        self.assertEqual(first['status'],'partial')
        def premature(*args):
            for frame in self.reader(*args):
                yield frame
                if frame['type']=='manifest':
                    yield {'type':'done'}
                    return
        result=self.imports.run(first['id'],premature)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['processed'],0)
    def test_cancel_keeps_committed_messages_and_new_job_can_rescan(self):
        job=self.create()
        def cancel(*args):
            for frame in self.reader(*args):
                yield frame
                if frame['type']=='batch': self.imports.cancel(job['id'])
        result=self.imports.run(job['id'],cancel)
        self.assertEqual(result['status'],'cancelled')
        self.assertEqual(len(self.imports.messages()),2)
        self.assertEqual(self.run_job()['items'],4)
    def test_enabled_scope_selection_and_conflicting_jobs(self):
        with self.assertRaises(ValueError): self.imports.create(scope_ids=[])
        self.store.set_chat_subscription_options(self.scope['id'],enabled=True,full_scan_enabled=False)
        with self.assertRaises(ValueError): self.create()
        job=self.create(mode='incremental')
        with self.assertRaises(ValueError): self.create(mode='incremental')
        self.imports.cancel(job['id'])
        self.assertEqual(self.create(mode='incremental')['status'],'queued')
    def test_shutdown_subscription_during_read_stops_before_persistence(self):
        def stop(*args):
            for frame in self.reader(*args):
                if frame['type']=='batch': self.store.set_chat_subscription_options(self.scope['id'],enabled=False,full_scan_enabled=False)
                yield frame
        result=self.imports.run(self.create()['id'],stop)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(self.imports.messages(),[])
    def test_live_lease_rejects_duplicate_run_and_expired_lease_resumes(self):
        job=self.create()
        with self.store.connect() as conn:
            conn.execute("UPDATE chat_import_jobs SET status='running',lease_until='2099-01-01T00:00:00+00:00' WHERE id=?",(job['id'],))
        with self.assertRaises(ValueError):self.imports.run(job['id'],self.reader)
        with self.store.connect() as conn:conn.execute('UPDATE chat_import_jobs SET lease_until=NULL WHERE id=?',(job['id'],))
        self.assertEqual(self.imports.run(job['id'],self.reader)['status'],'completed')
    def test_changed_native_boundary_is_not_silently_skipped(self):
        self.run_job()
        with sqlite3.connect(self.source/'message_0.db') as conn:conn.execute(f'UPDATE "{TABLE}" SET server_id=999 WHERE local_id=3')
        result=self.run_job(mode='incremental')
        self.assertEqual(result['status'],'partial')
        self.assertIn('重置',result['scopes'][0]['error'])
    def test_full_changed_content_creates_a_version_not_a_new_message(self):
        self.run_job()
        with sqlite3.connect(self.source/'message_0.db') as conn:conn.execute(f'UPDATE "{TABLE}" SET message_content=? WHERE local_id=1',('修改正文',))
        result=self.run_job()
        self.assertEqual((result['items'],result['versions']),(0,1))
    def test_chat_messages_export_to_d1_contract_and_roundtrip(self):
        self.run_job()
        exchange=Exchange(self.store)
        batch=exchange.create_export(idempotency_key='chat-export')
        package=json.loads(exchange.file(batch['id'],'messages.json')[0])
        self.assertEqual(package['event_count'],6)
        self.assertTrue(all(e['source']['kind']=='wechat' for e in package['events']))
        self.assertTrue(all(e['time_basis']=='wechat_create_time' for e in package['events']))
        other=Store(self.root/'other.sqlite3',self.root/'other')
        imported=Exchange(other)
        imported.apply(imported.preview('messages',package)['preview_id'])
        self.assertEqual(len(ChatImports(other).messages()),6)
        again=imported.create_export(idempotency_key='again')
        returned=json.loads(imported.file(again['id'],'messages.json')[0])
        self.assertEqual(exchange.preview('messages',returned)['new_events'],0)
    def test_qq_is_explicitly_unavailable_not_empty_success(self):
        qq=self.store.upsert_chat_subscription('qq','acct','group','QQ',enabled=True,full_scan_enabled=True)
        with self.assertRaisesRegex(ValueError,'解密'): self.imports.create(platform='qq',scope_ids=[qq['id']])

    def test_empty_range_and_unknown_time_are_counted_honestly(self):
        empty=self.run_job(mode='range',start='2020-01-01',end='2020-01-01')
        self.assertEqual((empty['status'],empty['processed']),('completed',0))
        with sqlite3.connect(self.source/'message_0.db') as conn:
            conn.execute(f'INSERT INTO "{TABLE}" VALUES(4,4,NULL,100,?,?)',('原始时间未知',b''))
        ranged=self.run_job(mode='range',start='2026-10-20',end='2026-10-20')
        self.assertEqual(ranged['processed'],6)
        self.assertEqual(sum(s['unknown_time_count'] for s in ranged['scopes'][0]['manifest']['shards']),1)
        full=self.run_job()
        self.assertEqual(full['processed'],7)
        self.assertTrue(any(m['metadata']['source_time'] is None for m in self.imports.messages()))

    def test_missing_source_row_after_interruption_cannot_be_marked_complete(self):
        job=self.create()
        def failing(*args):
            for frame in self.reader(*args):
                yield frame
                if frame['type']=='batch':raise RuntimeError('interrupted')
        self.imports.run(job['id'],failing)
        with sqlite3.connect(self.source/'message_0.db') as conn:conn.execute(f'DELETE FROM "{TABLE}" WHERE local_id=3')
        resumed=self.imports.run(job['id'],self.reader)
        self.assertEqual(resumed['status'],'partial')
        self.assertEqual(resumed['processed'],2)

    def test_old_runner_cannot_cancel_a_new_lease_owners_scope(self):
        job=self.create()
        def taken_over(*args):
            for frame in self.reader(*args):
                if frame['type']=='batch':
                    with self.store.connect() as conn:
                        conn.execute("UPDATE chat_import_jobs SET owner='new-owner' WHERE id=?",(job['id'],))
                yield frame
        result=self.imports.run(job['id'],taken_over)
        self.assertEqual(result['status'],'running')
        self.assertEqual(result['scopes'][0]['status'],'running')
        self.assertEqual(self.imports.messages(),[])


if __name__=='__main__':unittest.main()
