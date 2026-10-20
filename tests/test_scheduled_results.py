import copy
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from taskagent.contracts import MESSAGES, validate
from taskagent.exchange import Exchange
from taskagent.scheduled_results import ScheduledResults, read_codex, validate_packet
from taskagent.store import Store


def packet(**kwargs):
    row = {"provider": "chatgpt", "account_key": "test", "task_id": "task-1", "run_id": "run-1",
        "title": "合成定时提醒", "source_time": "2026-10-20T08:00:00+08:00", "run_status": "completed",
        "text": "今日需提交材料，尚未提交。", "completeness": "full"}
    row.update(kwargs)
    return {"format": "mta.scheduled-results", "version": "1.0", "results": [row]}


def seed_codex(root):
    home = root / 'codex'
    (home / 'sqlite').mkdir(parents=True)
    (home / 'sessions').mkdir()
    with sqlite3.connect(home / 'sqlite/codex-dev.db') as c:
        c.executescript('''CREATE TABLE automations(id TEXT,name TEXT,kind TEXT,target_thread_id TEXT);
            CREATE TABLE automation_runs(thread_id TEXT,automation_id TEXT,created_at INTEGER,inbox_summary TEXT,archived_assistant_message TEXT,thread_title TEXT);''')
        c.execute('INSERT INTO automations VALUES(?,?,?,?)', ('a1','合成定时检查','heartbeat','thread1'))
    transcript = home / 'sessions/test.jsonl'
    with sqlite3.connect(home / 'state_5.sqlite') as c:
        c.execute('CREATE TABLE threads(id TEXT,rollout_path TEXT)')
        c.execute('INSERT INTO threads VALUES(?,?)', ('thread1',str(transcript)))
    return home, transcript


def turn(turn_id='turn1', trigger='automation_heartbeat_scheduled', text='完整结果', task_id='a1'):
    def event(p):
        return {"timestamp":"2026-10-20T00:00:00Z", "type":"event_msg", "payload":p}
    return [event({"type":"task_started","turn_id":turn_id,"turn_attribution":{"turn_trigger":trigger}}),
        {"type":"response_item","payload":{"type":"message","role":"user","content":[{"text":f'<heartbeat><automation_id>{task_id}</automation_id></heartbeat>'}]}},
        event({"type":"task_complete","turn_id":turn_id,"last_agent_message":text})]


class ScheduledResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=Store(self.root/'store.sqlite3',self.root/'data')
        self.reader=ScheduledResults(self.store,codex_home=self.root/'absent')
    def tearDown(self):
        self.tmp.cleanup()
    def test_replay_idempotent_and_does_not_complete_tasks(self):
        self.assertEqual(self.reader.ingest(packet())['events'],1)
        self.assertEqual(self.reader.ingest(packet())['unchanged'],1)
        self.assertEqual(self.store.stats()['source_items'],1)
        self.assertEqual(self.store.stats()['event_consumptions'],0)
        self.assertEqual(self.store.stats()['tasks'],0)
        self.assertEqual(self.store.stats()['notification_outbox'],0)
    def test_changed_result_versions_and_replay_does_not_regress(self):
        self.reader.ingest(packet())
        self.assertEqual(self.reader.ingest(packet(text='修订后的完整结果'))['versions'],1)
        self.reader.ingest(packet())
        self.assertEqual(self.reader.messages()[0]['content_text'],'修订后的完整结果')
        self.assertEqual(self.store.stats()['source_items'],1)
        self.assertEqual(self.store.stats()['item_versions'],2)
    def test_same_text_different_runs_remain_separate(self):
        self.reader.ingest(packet())
        self.reader.ingest(packet(run_id='run-2',source_time='2026-10-21T08:00:00+08:00'))
        self.assertEqual(self.store.stats()['source_items'],2)
        self.assertEqual(self.store.stats()['events'],2)
    def test_summary_is_not_queued_and_full_upgrade_never_downgrades(self):
        self.assertEqual(self.reader.ingest(packet(completeness='summary'))['events'],0)
        self.assertEqual(self.reader.ingest(packet())['events'],1)
        self.assertEqual(self.reader.ingest(packet(completeness='summary',text='通知更新'))['ignored_summary'],1)
        self.assertEqual(self.reader.messages()[0]['metadata']['completeness'],'full')
    def test_packet_is_atomic_and_strict(self):
        p=packet();bad=copy.deepcopy(p['results'][0]);bad['run_id']='bad';bad['source_time']='2026-10-20T00:00:00';p['results'].append(bad)
        with self.assertRaises(ValueError):self.reader.import_packet(p)
        self.assertEqual(self.store.stats()['source_items'],0)
        for p in (packet(provider='other'),packet(run_status='running'),packet(text=''),packet(completeness='maybe')):
            with self.assertRaises(ValueError):validate_packet(p)
        self.assertFalse((self.store.data_dir/'inbox/scheduled/imports').exists())
    def test_conflicting_same_run_in_packet_rejected(self):
        p=packet();p['results'].append({**p['results'][0],'text':'冲突正文'})
        with self.assertRaises(ValueError):self.reader.ingest(p)
        self.assertEqual(self.store.stats()['source_items'],0)
    def test_exchange_roundtrip_source_identity(self):
        self.reader.ingest(packet())
        e=Exchange(self.store);b=e.create_export(idempotency_key='scheduled')
        messages=json.loads(e.file(b['id'],'messages.json')[0]);validate(messages,'messages')
        row=messages['events'][0]
        self.assertEqual(row['source']['kind'],'chatgpt_scheduled')
        self.assertEqual(row['native_identity']['run_id'],'run-1')
        self.assertEqual(row['source_time'],'2026-10-20T00:00:00+00:00')
        self.assertEqual(self.store.stats()['event_consumptions'],0)
    def test_codex_selects_only_real_scheduled_triggers(self):
        home, transcript=seed_codex(self.root)
        items=turn()+turn('human','composer',task_id='a1')+turn('second',text='第二次结果')
        transcript.write_text(''.join(json.dumps(x)+'\n' for x in items),encoding='utf8')
        rows,w=read_codex(home)
        self.assertEqual(w,[]);self.assertEqual([r['run_id'] for r in rows],['turn1','second'])
        self.assertEqual([r['text'] for r in rows],['完整结果','第二次结果'])
    def test_codex_live_partial_tail_retried(self):
        home, transcript=seed_codex(self.root)
        transcript.write_text(''.join(json.dumps(x)+'\n' for x in turn())+'{"type":',encoding='utf8')
        rows,w=read_codex(home)
        self.assertEqual(len(rows),1);self.assertIn('仍在写入',w[0])
    def test_codex_summary_then_real_body_same_identity(self):
        home,transcript=seed_codex(self.root)
        with sqlite3.connect(home/'sqlite/codex-dev.db') as c:
            c.execute('INSERT INTO automation_runs VALUES(?,?,?,?,?,?)',('thread1','a1',1792454400000,'摘要',None,'标题'))
        rows,w=read_codex(home)
        self.reader.ingest({'format':'mta.scheduled-results','version':'1.0','results':rows})
        self.assertEqual(rows[0]['run_id'],'thread1');self.assertTrue(w)
        transcript.write_text(''.join(json.dumps(x)+'\n' for x in turn(trigger='automation_cron_scheduled')),encoding='utf8')
        rows,w=read_codex(home)
        self.reader.ingest({'format':'mta.scheduled-results','version':'1.0','results':rows})
        self.assertEqual(self.store.stats()['source_items'],1)
        self.assertEqual(self.store.stats()['events'],1)
    def test_inbox_full_scan_replays_and_failures_not_empty_success(self):
        self.reader.inbox.mkdir(parents=True)
        (self.reader.inbox/'ok.json').write_text(json.dumps(packet()),encoding='utf8')
        (self.reader.inbox/'bad.json').write_text('{bad',encoding='utf8')
        r=self.reader.run(self.reader.start()['id'])
        self.assertEqual(r['status'],'partial')
        self.assertEqual(r['sources'][0]['status'],'failed')
        self.assertEqual(r['sources'][1]['items'],1)
        self.assertEqual(len(r['sources'][1]['warnings']),1)
        r=self.reader.run(self.reader.start()['id'])
        self.assertEqual(r['sources'][1]['items'],0)
        self.assertEqual(r['sources'][1]['unchanged'],1)
        self.assertFalse(self.reader.status()['cloud_connected'])
    def test_concurrent_scan_rejected(self):
        self.reader.start()
        with self.assertRaises(ValueError):self.reader.start()
    def test_expired_scan_can_be_started_again(self):
        old=self.reader.start()
        with self.store.transaction() as c:
            c.execute("UPDATE scheduled_scan_jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?",(old['id'],))
        self.assertEqual(self.reader.status()['latest_job']['status'],'interrupted')
        self.assertNotEqual(old['id'],self.reader.start()['id'])
    def test_codex_unfinished_and_empty_results_are_reported(self):
        home,transcript=seed_codex(self.root)
        transcript.write_text(''.join(json.dumps(x)+'\n' for x in turn(text='')+turn('pending')[:1]),encoding='utf8')
        rows,w=read_codex(home)
        self.assertEqual(rows,[])
        self.assertEqual(len(w),2)
    def test_invalid_codex_root_never_creates_databases(self):
        with self.assertRaises(sqlite3.OperationalError):read_codex(self.root/'nonexistent')
        self.assertFalse((self.root/'nonexistent').exists())


if __name__=='__main__':unittest.main()
