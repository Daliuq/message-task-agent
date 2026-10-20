import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from taskagent.chat_import import ChatImports
from taskagent.store import Store
from taskagent.webui import make_handler
from taskagent.wechat_message_worker import read_pages
from test_chat_import import FakeDB, source_files, USER


class ChatImportHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.db,self.data=self.root/'test.sqlite3',self.root/'data'
        self.store=Store(self.db,self.data)
        self.scope=self.store.upsert_chat_subscription('wechat','account-demo',USER,'HTTP合成群',enabled=True,full_scan_enabled=True)
        self.imports=ChatImports(self.store)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.db,self.data))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()
    def get(self,path):
        with urllib.request.urlopen(self.base+path,timeout=3) as r:return json.loads(r.read())
    def post(self,path,value,headers=None):
        req=urllib.request.Request(self.base+path,data=json.dumps(value).encode(),headers=headers or {'Content-Type':'application/json','X-Requested-With':'fetch'})
        with urllib.request.urlopen(req,timeout=3) as r:return r.status,json.loads(r.read())
    def test_start_returns_202_without_waiting_for_local_reader(self):
        with patch('taskagent.chat_import.start_background') as started:
            status,job=self.post('/api/chat-imports',{'scope_ids':[self.scope['id']]})
        self.assertEqual(status,202)
        started.assert_called_once()
        self.assertEqual(self.get('/api/chat-imports')[0]['id'],job['id'])
        self.assertEqual(self.get('/api/chat-imports/'+job['id'])['status'],'queued')
        _,cancelled=self.post('/api/chat-imports/'+job['id']+'/cancel',{})
        self.assertEqual(cancelled['status'],'cancelled')
    def test_validation_and_same_origin_precede_source_access(self):
        with patch('taskagent.chat_import.start_background') as started:
            for value in ({'scope_ids':[]},{'mode':'range','scope_ids':[self.scope['id']]},{'mode':'invalid'}):
                with self.assertRaises(urllib.error.HTTPError) as error:self.post('/api/chat-imports',value)
                self.assertEqual(error.exception.code,400)
            started.assert_not_called()
        with self.assertRaises(urllib.error.HTTPError) as error:self.post('/api/chat-imports',{},headers={'Content-Type':'application/json'})
        self.assertEqual(error.exception.code,403)
    def test_saved_chat_content_and_raw_fields_can_be_read_and_downloaded(self):
        source_files(self.root/'source')
        def reader(scope,config,checkpoint,live):yield from read_pages(FakeDB(self.root/'source'),USER,config,checkpoint,live)
        job=self.imports.create(scope_ids=[self.scope['id']])
        self.imports.run(job['id'],reader)
        messages=self.get('/api/chat-messages?limit=2')
        self.assertEqual(len(messages),2)
        self.assertIn('合成消息',messages[0]['content_text'])
        raw=self.get('/api/chat-messages/'+messages[0]['id']+'/raw')
        self.assertIn('raw_row',raw)
        self.assertEqual(self.get('/api/chat-messages?scope_id=missing'),[])
        with self.assertRaises(urllib.error.HTTPError):self.get('/api/chat-messages/missing/raw')
    def test_qq_native_probe_and_import_ui_are_explicit_about_limitations(self):
        with patch('taskagent.chat_reader.qq_local_status',return_value={'accounts':[],'supported':False,'note':'unsupported'}):
            self.assertFalse(self.get('/api/qq-local-status')['supported'])
        with urllib.request.urlopen(self.base+'/',timeout=3) as r:
            html=r.read().decode()
        self.assertIn('聊天正文导入',html)
        self.assertIn('chat-import.js',html)
        with urllib.request.urlopen(self.base+'/chat-import.js',timeout=3) as r:self.assertEqual(r.status,200)

    def test_transfer_endpoint_is_atomic_and_keeps_body_import_separate(self):
        run=self.store.start_chat_discovery('qq')
        self.store.finish_chat_discovery(run,[{'account_key':'12345','conversation_key':'group:9001','display_name':'HTTP 合成 QQ 群','message_count':3,'conversation_type':'group'}])
        cid=self.store.chat_discovery_results('qq')['qq']['candidates'][0]['id']
        status,result=self.post('/api/subscriptions/transfer',{'candidate_ids':[cid],'disable_ids':[self.scope['id']],'full_scan_enabled':True})
        self.assertEqual(status,200)
        self.assertEqual((result['added'],result['disabled']),(1,1))
        self.assertEqual(len(self.get('/api/chat-imports')),0)
        self.assertEqual(len(self.get('/api/chat-messages')),0)
        self.assertFalse(next(s for s in result['subscriptions'] if s['id']==self.scope['id'])['enabled'])
        before=result['subscriptions']
        with self.assertRaises(urllib.error.HTTPError):self.post('/api/subscriptions/transfer',{'disable_ids':['missing']})
        self.assertEqual(self.get('/api/subscriptions'),before)


if __name__=='__main__':unittest.main()
