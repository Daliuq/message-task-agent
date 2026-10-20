"""Exercise the real pinned QQ reader with synthetic databases, never real accounts."""
import base64
from contextlib import ExitStack
import json
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from taskagent.chat_import import ChatImports
from taskagent.chat_reader import read_qq_messages
from taskagent.qq import ROOT, discover
from taskagent.store import Store

PYTHON = ROOT / '.venv/Scripts/python.exe'


@unittest.skipUnless(PYTHON.is_file(), 'QQ isolated runtime not installed')
class QQIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        code = "import sys,base64;sys.path.insert(0,sys.argv[1]);import element_pb2;p=element_pb2.Elements();p.elements.add(type=1,text='合成 QQ 正文');print(base64.b64encode(p.SerializeToString()).decode())"
        result = subprocess.run([str(PYTHON), '-c', code, str(ROOT/'vendor/QQNT_Export')],
                                check=True,capture_output=True,text=True,encoding='utf-8')
        cls.body = base64.b64decode(result.stdout.strip())

    def setUp(self):
        self.temp=TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.source=self.root/'source';self.source.mkdir()
        self.store=Store(self.root/'agent.sqlite3',self.root/'data')
        self.imports=ChatImports(self.store)
        columns='"40001" INTEGER PRIMARY KEY,"40021" TEXT,"40027" INTEGER,"40030" INTEGER,"40011" INTEGER,"40800" BLOB,"40050" INTEGER,"40020" TEXT,"40033" INTEGER,"40003" INTEGER,"40090" TEXT,"99999" BLOB'
        with sqlite3.connect(self.source/'nt_msg.db') as conn:
            conn.execute('CREATE TABLE group_msg_table('+columns+')')
            conn.execute('CREATE TABLE c2c_msg_table('+columns+')')
            conn.execute('CREATE TABLE nt_uid_mapping_table("48901" INTEGER PRIMARY KEY,"48902" TEXT,"48912" TEXT,"1002" INTEGER)')
            conn.execute("INSERT INTO nt_uid_mapping_table VALUES(1,'u_peer',NULL,20001)")
            for key,stamp in [(-30,1792425599),(-10,1792425600),(10,1792512000)]:
                conn.execute('INSERT INTO group_msg_table VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (key,'9001',9001,9001,2,self.body,stamp,'u_sender',30001,100,'群名片',b'\x00\xff'))
            conn.execute('INSERT INTO c2c_msg_table VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (3,'u_peer',1,20001,2,self.body,1792425600,'u_peer',20001,100,'',b'private'))
        with sqlite3.connect(self.source/'profile_info.db') as conn:
            conn.execute('CREATE TABLE profile_info_v6("1000" TEXT,"1002" INTEGER,"20009" TEXT,"20002" TEXT)')
            conn.execute("INSERT INTO profile_info_v6 VALUES('u_peer',20001,'合成好友','测试昵称')")
        self.request={'account':'12345','directory':str(self.source),'key':'synthetic-only'}
        self.patches=ExitStack()
        self.patches.enter_context(patch('taskagent.qq.status',return_value={'accounts':[{'account':'12345','readable':True}]}))
        self.patches.enter_context(patch('taskagent.qq.request_for',return_value=self.request))
        self.scope=self.store.upsert_chat_subscription('qq','12345','group:9001','合成 QQ 群',enabled=True,full_scan_enabled=True,tags=['测试'])

    def tearDown(self):
        self.patches.close();self.temp.cleanup()

    def create(self,**kwargs):
        return self.imports.create(platform='qq',scope_ids=[self.scope['id']],batch_size=2,**kwargs)

    def run_job(self,**kwargs):
        return self.imports.run(self.create(**kwargs)['id'])

    def test_real_upstream_discovery_lists_groups_and_private_without_subscribing(self):
        before=len(self.store.chat_subscriptions())
        result=discover(self.store.data_dir)
        self.assertEqual(len(result['candidates']),2)
        self.assertEqual(sum(x['message_count'] for x in result['candidates']),4)
        self.assertIn('合成好友',[x['display_name'] for x in result['candidates']])
        self.assertEqual(len(self.store.chat_subscriptions()),before)

    def test_full_import_parses_text_preserves_unknown_columns_and_negative_ids(self):
        job=self.run_job()
        self.assertEqual((job['status'],job['processed'],job['items']),('completed',3,3))
        messages=self.imports.messages(self.scope['id'])
        self.assertEqual({m['content_text'] for m in messages},{'合成 QQ 正文'})
        self.assertTrue(all(m['metadata']['time_basis']=='qq_message_time' for m in messages))
        raw=json.loads((self.store.data_dir/messages[0]['raw_path']).read_text(encoding='utf-8'))
        self.assertEqual(raw['raw_row']['99999'],{'$base64':'AP8='})
        self.assertEqual(len(self.store.tasks()),0)
        self.assertEqual(len(self.store.outbox_preview()),0)

    def test_repeated_full_and_incremental_have_no_duplicate_versions_or_events(self):
        self.run_job()
        full=self.run_job()
        incremental=self.run_job(mode='incremental')
        self.assertEqual((full['items'],full['versions']),(0,0))
        self.assertEqual((incremental['status'],incremental['processed'],incremental['items']),('completed',0,0))
        self.assertEqual(incremental['scopes'][0]['manifest']['shards'][0]['scanned_count'],3)
        with self.store.connect() as conn:self.assertEqual(conn.execute('SELECT count(*) FROM events').fetchone()[0],3)

    def test_incremental_finds_lower_id_late_history_and_changed_existing_message(self):
        self.run_job()
        with sqlite3.connect(self.source/'nt_msg.db') as conn:
            conn.execute('INSERT INTO group_msg_table VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (-100,'9001',9001,9001,2,self.body,1000,'u_late',30001,100,'',b'late'))
            conn.execute('UPDATE group_msg_table SET "99999"=? WHERE "40001"=-10',(b'changed',))
        job=self.run_job(mode='incremental')
        self.assertEqual((job['status'],job['processed'],job['items'],job['versions']),('completed',2,1,2))
        self.assertEqual(self.run_job(mode='incremental')['processed'],0)

    def test_range_uses_beijing_inclusive_day_and_does_not_hide_unknown_times(self):
        job=self.run_job(mode='range',start='2026-10-20',end='2026-10-20')
        self.assertEqual((job['status'],job['processed']),('completed',1))
        with sqlite3.connect(self.source/'nt_msg.db') as conn:
            conn.execute('UPDATE group_msg_table SET "40050"=NULL WHERE "40001"=-30')
        ranged=self.run_job(mode='range',start='2026-10-20',end='2026-10-20')
        self.assertEqual(ranged['scopes'][0]['manifest']['shards'][0]['unknown_time_count'],1)
        self.assertEqual(self.run_job()['processed'],3)

    def test_incremental_resume_keeps_frozen_baseline_after_committed_pages(self):
        job=self.create(mode='incremental')
        def interrupt(scope,config,checkpoint,live):
            for frame in read_qq_messages(scope,config,checkpoint,live,data_dir=self.store.data_dir):
                yield frame
                if frame['type']=='batch':raise RuntimeError('synthetic interruption')
        first=self.imports.run(job['id'],interrupt)
        self.assertEqual((first['status'],first['processed']),('partial',2))
        resumed=self.imports.run(job['id'])
        self.assertEqual((resumed['status'],resumed['processed'],resumed['items']),('completed',3,3))
        self.assertEqual(self.run_job(mode='incremental')['processed'],0)

    def test_private_import_uses_the_same_pipeline(self):
        private=self.store.upsert_chat_subscription('qq','12345','private:u_peer','合成好友',enabled=True,full_scan_enabled=True)
        job=self.imports.create(platform='qq',scope_ids=[private['id']])
        result=self.imports.run(job['id'])
        self.assertEqual((result['status'],result['processed']),('completed',1))
        self.assertEqual(self.imports.messages(private['id'])[0]['content_text'],'合成 QQ 正文')

    def test_disabled_subscription_does_not_start_a_reader(self):
        self.store.set_chat_subscription_options(self.scope['id'],enabled=False,full_scan_enabled=False)
        with self.assertRaises(ValueError):self.create()


if __name__=='__main__':unittest.main()
