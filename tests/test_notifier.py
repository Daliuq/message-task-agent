import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from taskagent.notifier import send_next_notification
from taskagent.store import Store, utc_now


class FakeSMTP:
    scenario = "accepted"
    last_message = b""
    def __init__(self, *_args, **_kwargs):
        pass
    def login(self, _user, _secret):
        return 235, b"authenticated"
    def mail(self, _sender):
        if self.scenario == "mail_rejected":
            return 550, b"sender rejected"
        return 250, b"sender ok"
    def rcpt(self, _recipient):
        return 250, b"recipient ok"
    def data(self, content):
        self.__class__.last_message = content
        if self.scenario == "timeout_after_data":
            raise TimeoutError("socket timed out")
        return (550, b"rejected") if self.scenario == "data_rejected" else (250, b"accepted")
    def quit(self):
        return 221, b"bye"


class NotifierTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        base = Path(self.temp.name)
        self.store = Store(base / "db.sqlite3", base / "data")
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO notification_outbox(id,notification_type,dedupe_key,subject,body,created_at) VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()), "test", "test-key", "主题", "邮件正文", utc_now()),
            )

    def tearDown(self):
        self.temp.cleanup()

    def send(self, scenario="accepted"):
        FakeSMTP.scenario = scenario
        with patch("taskagent.notifier.smtplib.SMTP_SSL", FakeSMTP):
            return send_next_notification(self.store, "sender@qq.com", "not-a-real-secret", "recipient@example.com")

    def test_accepted_mail_is_recorded_sent_with_stable_message_id(self):
        result = self.send()
        self.assertEqual(result["status"], "sent")
        self.assertIn(b"Message-ID: <task-notify-", FakeSMTP.last_message)
        self.assertIn(b"Subject:", FakeSMTP.last_message)
        self.assertEqual(self.store.outbox_preview()[0]["status"], "sent")

    def test_timeout_during_data_is_unknown_and_not_retried(self):
        result = self.send("timeout_after_data")
        self.assertEqual(result["status"], "delivery_unknown")
        self.assertIsNone(self.send())
        self.assertEqual(self.store.outbox_preview()[0]["status"], "delivery_unknown")

    def test_explicit_rejection_can_only_be_retried_manually(self):
        result = self.send("mail_rejected")
        self.assertEqual(result["status"], "failed")
        row = self.store.outbox_preview()[0]
        self.store.retry_failed_notification(row["id"])
        self.assertEqual(self.store.outbox_preview()[0]["status"], "queued")


if __name__ == "__main__":
    unittest.main()
