import unittest
from unittest.mock import patch
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory

from taskagent.mail import _internaldate, scan_mailbox, scan_mailbox_range
from taskagent.store import Store


class InternalDateParsingTests(unittest.TestCase):
    def test_accepts_space_padded_day_single_digit_hour_and_colon_timezone(self):
        row = (b'1 (UID 1 INTERNALDATE " 9-Oct-2026 7:08:09 +08:00" BODY[] {1}', b"x")
        self.assertEqual(_internaldate([row]), "2026-10-09T07:08:09+08:00")

    def test_rejects_internaldate_without_timezone(self):
        row = (b'1 (UID 1 INTERNALDATE "19-Oct-2026 07:08:09" BODY[] {1}', b"x")
        self.assertIsNone(_internaldate([row]))


def raw_mail(uid):
    message = EmailMessage()
    message["From"] = "jobs@example.org"
    message["To"] = "me@qq.com"
    message["Subject"] = f"新增职位 {uid}"
    message.set_content(f"正文 {uid}")
    return message.as_bytes()


class FakeIMAP:
    def __init__(self, mailboxes):
        self.mailboxes = mailboxes
        self.selected = None
    def login(self, _address, _code):
        return "OK", [b"authenticated"]
    def list(self, *_args):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Sent Messages"']
    def select(self, folder, readonly=False):
        assert readonly is True
        if folder.startswith('"') and folder.endswith('"'):
            folder = folder[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        self.selected = folder
        return "OK", [str(len(self.mailboxes.get(folder, {}))).encode()]
    def response(self, name):
        return name, [b"17"]
    def uid(self, command, *args):
        mailbox = self.mailboxes[self.selected]
        if command == "search":
            return "OK", [b" ".join(str(uid).encode() for uid in sorted(mailbox))]
        if command == "fetch":
            uid = int(args[0])
            return "OK", [(b"1 (BODY[] {n})".replace(b"{n}", str(len(mailbox[uid])).encode()), mailbox[uid])]
        raise AssertionError(command)
    def logout(self):
        return "BYE", [b"logged out"]


class FakeRangeIMAP(FakeIMAP):
    def __init__(self, mailboxes, received_at, *, omit_batch_date_uids=None,
                 omit_individual_date_uids=None):
        super().__init__(mailboxes)
        self.received_at = received_at
        self.omit_batch_date_uids = set(omit_batch_date_uids or ())
        self.omit_individual_date_uids = set(omit_individual_date_uids or ())
        self.date_retries = []
        self.range_search = None

    def uid(self, command, *args):
        if command == "search":
            self.range_search = args
            mailbox = self.mailboxes[self.selected]
            return "OK", [b" ".join(str(uid).encode() for uid in sorted(mailbox))]
        if command == "fetch":
            if args[1] == "(INTERNALDATE)":
                uid = int(args[0])
                self.date_retries.append(uid)
                header = b"1 (UID " + str(uid).encode()
                if uid not in self.omit_individual_date_uids:
                    header += (b' INTERNALDATE "' + self.received_at[uid].encode() + b'"')
                return "OK", [(header + b")", None)]
            uids = [int(value) for value in args[0].split(",")]
            rows = []
            for uid in uids:
                header = b'1 (UID ' + str(uid).encode()
                if uid not in self.omit_batch_date_uids:
                    header += b' INTERNALDATE "' + self.received_at[uid].encode() + b'"'
                header += b' BODY[] {' + str(len(self.mailboxes[self.selected][uid])).encode() + b'})'
                rows.append((header, self.mailboxes[self.selected][uid]))
            return "OK", rows
        raise AssertionError(command)


class MailScanTests(unittest.TestCase):
    def test_range_scan_retries_missing_batched_internaldate_per_uid(self):
        with TemporaryDirectory() as temp:
            base = Path(temp)
            store = Store(base / "db.sqlite3", base / "data")
            mailboxes = {"INBOX": {251: raw_mail(251)}}
            received = {251: "19-Oct-2026 09:30:00 +0800"}
            client = FakeRangeIMAP(mailboxes, received, omit_batch_date_uids={251})
            with patch("taskagent.mail.imaplib.IMAP4_SSL", return_value=client):
                result = scan_mailbox_range(store, "me@qq.com", "dummy", "2026-10-19", "2026-10-19",
                                            folders=["INBOX"])
            self.assertTrue(result["ok"])
            self.assertEqual(client.date_retries, [251])
            self.assertEqual(result["folders"][0]["matched_count"], 1)
            self.assertEqual(store.stats()["source_items"], 1)

    def test_range_scan_still_fails_if_individual_internaldate_retry_is_missing(self):
        with TemporaryDirectory() as temp:
            base = Path(temp)
            store = Store(base / "db.sqlite3", base / "data")
            mailboxes = {"INBOX": {251: raw_mail(251)}}
            received = {251: "19-Oct-2026 09:30:00 +0800"}
            client = FakeRangeIMAP(mailboxes, received, omit_batch_date_uids={251},
                                   omit_individual_date_uids={251})
            with patch("taskagent.mail.imaplib.IMAP4_SSL", return_value=client):
                result = scan_mailbox_range(store, "me@qq.com", "dummy", "2026-10-19", "2026-10-19",
                                            folders=["INBOX"])
            folder_result = result["folders"][0]
            self.assertFalse(result["ok"])
            self.assertEqual(folder_result["status"], "failed")
            self.assertIn("UID 251 未返回有效 INTERNALDATE", folder_result["error"])
            self.assertEqual(store.stats()["source_items"], 0)

    def test_full_date_range_scan_filters_received_time_and_preserves_incremental_cursor(self):
        with TemporaryDirectory() as temp:
            base = Path(temp)
            store = Store(base / "db.sqlite3", base / "data")
            store.initialize_mail_cursor("me@qq.com", "INBOX", "17", 99)
            mailboxes = {"INBOX": {10: raw_mail(10), 11: raw_mail(11), 12: raw_mail(12)}}
            received = {10: "18-Oct-2026 23:59:59 +0800",
                        11: "19-Oct-2026 13:15:00 +0800",
                        12: "20-Oct-2026 00:00:00 +0800"}
            client = FakeRangeIMAP(mailboxes, received)
            with patch("taskagent.mail.imaplib.IMAP4_SSL", return_value=client):
                result = scan_mailbox_range(store, "me@qq.com", "dummy", "2026-10-19", "2026-10-19",
                                            folders=["INBOX"], batch_size=1)
            self.assertTrue(result["ok"])
            folder_result = result["folders"][0]
            self.assertEqual(folder_result["candidate_count"], 3)
            self.assertEqual(folder_result["matched_count"], 1)
            self.assertEqual(folder_result["new_count"], 1)
            self.assertFalse(folder_result["incremental_cursor_changed"])
            self.assertEqual(client.range_search, (None, "SINCE", "18-Oct-2026", "BEFORE", "22-Oct-2026"))
            self.assertEqual(store.get_mail_cursor("me@qq.com", "INBOX")["uid"], 99)
            self.assertEqual(store.stats()["source_items"], 1)
            with store.connect() as conn:
                metadata = conn.execute("SELECT metadata_json FROM item_versions").fetchone()[0]
                run = conn.execute("SELECT cursor_before,cursor_after FROM scan_runs WHERE cursor_before IS NOT NULL ORDER BY rowid DESC LIMIT 1").fetchone()
            self.assertIn('"received_at":"2026-10-19T13:15:00+08:00"', metadata)
            self.assertIn('"mode":"time_range_full"', run["cursor_before"])
            self.assertIn('"incremental_cursor_unchanged":true', run["cursor_after"])

            client = FakeRangeIMAP(mailboxes, received)
            with patch("taskagent.mail.imaplib.IMAP4_SSL", return_value=client):
                repeated = scan_mailbox_range(store, "me@qq.com", "dummy", "2026-10-19", "2026-10-19",
                                              folders=["INBOX"], batch_size=2)
            self.assertEqual(repeated["folders"][0]["matched_count"], 1)
            self.assertEqual(repeated["folders"][0]["new_count"], 0)
            self.assertEqual(store.stats()["events"], 1)

    def test_scoped_newest_first_sample_ingests_only_one_requested_folder(self):
        with TemporaryDirectory() as temp:
            base = Path(temp)
            store = Store(base / "db.sqlite3", base / "data")
            mailboxes = {"INBOX": {1: raw_mail(1), 2: raw_mail(2)},
                         "Sent Messages": {1: raw_mail(3)}}
            with patch("taskagent.mail.imaplib.IMAP4_SSL",
                       side_effect=lambda *a, **kw: FakeIMAP(mailboxes)):
                result = scan_mailbox(store, "me@qq.com", "dummy", max_per_folder=1,
                                      import_history=True, folders=["INBOX"], newest_first=True)
            self.assertTrue(result["ok"])
            self.assertEqual([item["folder"] for item in result["folders"]], ["INBOX"])
            self.assertEqual(result["folders"][0]["new_count"], 1)
            self.assertEqual(store.stats()["source_items"], 1)
            self.assertIsNone(store.get_mail_cursor("me@qq.com", "Sent Messages"))
            with store.connect() as conn:
                metadata = conn.execute("SELECT metadata_json FROM item_versions").fetchone()[0]
            self.assertIn('"uid":2', metadata)

    def test_baseline_then_incremental_all_folders_readonly(self):
        with TemporaryDirectory() as temp:
            base = Path(temp)
            store = Store(base / "db.sqlite3", base / "data")
            mailboxes = {"INBOX": {1: raw_mail(1), 2: raw_mail(2)}, "Sent Messages": {1: raw_mail(9)}}
            with patch("taskagent.mail.imaplib.IMAP4_SSL", side_effect=lambda *a, **kw: FakeIMAP(mailboxes)):
                first = scan_mailbox(store, "me@qq.com", "not-read-or-logged", max_per_folder=10)
            self.assertTrue(first["ok"])
            self.assertEqual([f["status"] for f in first["folders"]], ["baseline", "baseline"])
            self.assertEqual(store.stats()["events"], 0)

            mailboxes["INBOX"][3] = raw_mail(3)
            mailboxes["Sent Messages"][2] = raw_mail(10)
            with patch("taskagent.mail.imaplib.IMAP4_SSL", side_effect=lambda *a, **kw: FakeIMAP(mailboxes)):
                second = scan_mailbox(store, "me@qq.com", "not-read-or-logged", max_per_folder=10)
            self.assertTrue(second["ok"])
            self.assertEqual([f["new_count"] for f in second["folders"]], [1, 1])
            self.assertEqual(store.stats()["events"], 2)
            self.assertEqual(store.get_mail_cursor("me@qq.com", "INBOX")["uid"], 3)
            with store.connect() as conn:
                raw_path = conn.execute("SELECT raw_path FROM item_versions LIMIT 1").fetchone()[0]
            self.assertTrue((base / "data" / raw_path).exists())


if __name__ == "__main__":
    unittest.main()
