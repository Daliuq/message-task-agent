from __future__ import annotations

import unittest
import uuid
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory

from taskagent.agent import AgentWorker
from taskagent.store import Store


class FixedAgent:
    name = "fixed-test-agent"
    version = "test-1"

    def __init__(self, identity_key="task:apply-deadline"):
        self.identity_key = identity_key

    def analyze(self, event):
        if "Task: fail-once" in event["content"]:
            raise RuntimeError("simulated agent failure")
        if "Task: none" in event["content"]:
            return {"tasks": []}
        return {"tasks": [{
            "identity_key": self.identity_key,
            "title": "提交材料",
            "description": "向招聘系统提交申请材料",
            "due_at": "2026-10-21T23:59:00+08:00",
            "estimated_minutes": 45,
            "priority": 2,
        }]}


def raw_email(subject: str, body: str) -> bytes:
    message = EmailMessage()
    message["From"] = "hr@example.org"
    message["To"] = "candidate@qq.com"
    message["Subject"] = subject
    message["Message-ID"] = f"<{uuid.uuid4()}@example.org>"
    message.set_content(body)
    return message.as_bytes()


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        base = Path(self.temp.name)
        self.store = Store(base / "db.sqlite3", base / "data")
        self.account = "candidate@qq.com"
        self.folder = "INBOX"
        self.store.initialize_mail_cursor(self.account, self.folder, "88", 0, status="test_import")

    def tearDown(self):
        self.temp.cleanup()

    def ingest(self, uid: int, subject="提醒", body="Task: submit"):
        cursor = self.store.get_mail_cursor(self.account, self.folder)
        return self.store.ingest_mail_batch(
            self.account, self.folder, "88",
            [{"uid": uid, "raw": raw_email(subject, body),
              "body_text": body,
              "metadata": {"uid": uid, "folder": self.folder, "subject": subject}}],
            uid, cursor_before=cursor,
        )

    def test_two_distinct_notifications_link_to_one_task_and_queue_repeat_email(self):
        self.ingest(1)
        worker = AgentWorker(self.store, FixedAgent())
        first = worker.process_once()
        self.assertEqual(first["status"], "succeeded")
        self.ingest(2, subject="再次提醒")
        second = worker.process_once()
        self.assertEqual(second["tasks"][0]["occurrences"], 2)

        tasks = self.store.tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["occurrence_count"], 2)
        notifications = self.store.outbox_preview()
        self.assertCountEqual([item["notification_type"] for item in notifications],
                              ["new_task", "repeat_reminder"])
        self.assertIn("再次收到", next(item["body"] for item in notifications
                                     if item["notification_type"] == "repeat_reminder"))

    def test_completed_task_is_not_reopened_or_resent_for_old_reminder(self):
        self.ingest(1)
        worker = AgentWorker(self.store, FixedAgent())
        worker.process_once()
        task_id = self.store.tasks()[0]["id"]
        self.store.set_task_status(task_id, "completed")
        self.ingest(2, subject="旧通知重现")
        worker.process_once()
        task = self.store.tasks()[0]
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["occurrence_count"], 2)
        self.assertEqual(len(self.store.outbox_preview()), 1)

    def test_failed_agent_attempt_is_logged_and_retry_is_idempotent(self):
        self.ingest(1, body="Task: fail-once")
        worker = AgentWorker(self.store, FixedAgent())
        first = worker.process_once()
        self.assertEqual(first["status"], "failed")
        # A deterministic second attempt succeeds; the failed attempt remains auditable.
        class RetryAgent(FixedAgent):
            def analyze(self, event):
                return {"tasks": [{"identity_key": "task:retry", "title": "重试成功"}]}
        retry = AgentWorker(self.store, RetryAgent()).process_once()
        self.assertEqual(retry["status"], "succeeded")
        with self.store.connect() as conn:
            statuses = [row[0] for row in conn.execute(
                "SELECT status FROM event_consumptions ORDER BY attempt_no")]
        self.assertEqual(statuses, ["failed", "succeeded"])
        self.assertEqual(len(self.store.tasks()), 1)

    def test_cursor_and_messages_roll_back_together_on_invalid_metadata(self):
        cursor = self.store.get_mail_cursor(self.account, self.folder)
        with self.assertRaises(TypeError):
            self.store.ingest_mail_batch(
                self.account, self.folder, "88",
                [{"uid": 1, "raw": raw_email("bad", "body"), "body_text": "body",
                  "metadata": {"unserializable": object()}}], 1, cursor_before=cursor)
        self.assertEqual(self.store.get_mail_cursor(self.account, self.folder)["uid"], 0)
        self.assertEqual(self.store.stats()["source_items"], 0)
        self.assertEqual(self.store.stats()["events"], 0)

    def test_same_event_content_has_one_event_and_full_raw_mime_is_retained(self):
        self.ingest(1)
        with self.store.connect() as conn:
            row = conn.execute("SELECT raw_path FROM item_versions").fetchone()
            event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        raw_file = self.store.data_dir / row["raw_path"]
        self.assertTrue(raw_file.exists())
        self.assertIn(b"Subject: =?utf-8?", raw_file.read_bytes())
        self.assertEqual(event_count, 1)


if __name__ == "__main__":
    unittest.main()
