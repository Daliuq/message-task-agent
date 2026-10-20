import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from taskagent.store import Store
from taskagent.webui import make_handler
from test_scheduled_results import packet


class ScheduledWebTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();root=Path(self.temp.name)
        self.store=Store(root/'app.sqlite3',root/'data')
        self.server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.store.db_path,self.store.data_dir))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()
    def get(self,path):
        return urllib.request.urlopen(self.base+path,timeout=3)
    def post(self,path,data,headers=None):
        r=urllib.request.Request(self.base+path,data=json.dumps(data).encode(),headers=headers or {'Content-Type':'application/json','X-Requested-With':'fetch'})
        return urllib.request.urlopen(r,timeout=3)
    def test_import_list_raw_and_replay(self):
        with self.post('/api/scheduled/import',packet()) as r:self.assertEqual(json.load(r)['items'],1)
        with self.get('/api/scheduled/messages') as r:rows=json.load(r)
        self.assertEqual(rows[0]['metadata']['provider'],'chatgpt')
        with self.get('/api/scheduled/messages/'+rows[0]['id']+'/raw') as r:self.assertEqual(json.load(r)['run_id'],'run-1')
        with self.post('/api/scheduled/import',packet()) as r:self.assertEqual(json.load(r)['unchanged'],1)
        with self.get('/api/scheduled/status') as r:self.assertFalse(json.load(r)['cloud_connected'])
        self.assertEqual(self.store.stats()['tasks'],0)
    def test_invalid_import_and_foreign_origin_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as error:self.post('/api/scheduled/import',packet(source_time='2026-10-20'))
        self.assertEqual(error.exception.code,400)
        with self.assertRaises(urllib.error.HTTPError) as error:self.post('/api/scheduled/scan',{},headers={'Content-Type':'application/json','X-Requested-With':'fetch','Origin':'https://example.org'})
        self.assertEqual(error.exception.code,403)
        self.assertEqual(self.store.stats()['source_items'],0)
    def test_scan_starts_background_and_static_assets_served(self):
        with patch('taskagent.scheduled_results.start_background') as start:
            with self.post('/api/scheduled/scan',{}) as r:self.assertEqual(r.status,202)
            self.assertEqual(start.call_count,1)
        with self.get('/scheduled.js') as r:self.assertEqual(r.status,200)
        with self.get('/') as r:self.assertIn(b'id="view-scheduled"',r.read())


if __name__=='__main__':unittest.main()
