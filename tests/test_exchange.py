import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from taskagent.contracts import loads_document, validate, MESSAGES, RESULT
from taskagent.exchange import Exchange
from taskagent.store import Store


def seed(store, count=2):
    store.initialize_mail_cursor("demo@example.org", "INBOX", "17", 0)
    store.ingest_mail_batch(
        "demo@example.org", "INBOX", "17",
        [{"uid": i, "raw": f"Subject: demo {i}\r\n\r\nbody {i}".encode(),
          "body_text": f"提交材料 {i}", "metadata": {"uid": i, "uidvalidity": "17", "subject": f"提醒 {i}",
           "received_at": f"2026-10-20T{8+i:02}:00:00+08:00"}}
         for i in range(1, count+1)], count, cursor_before={"uid": 0})


def reply(exchange, batch_id, candidates=False):
    result = json.loads(exchange.file(batch_id, "analysis-result.template.json")[0])
    request = json.loads(exchange.file(batch_id, "analysis-request.json")[0])
    for event, result_item in zip(request["events"], result["results"]):
        result_item.update(status="ok", warnings=["人工确认无额外事项"])
        if candidates:
            result_item["candidates"] = [{
                "candidate_id": "c1", "title": "提交材料", "description": "", "entity": "", "action": "提交",
                "instance_key": None, "due_at": None, "fixed_start_at": None, "fixed_end_at": None,
                "estimated_minutes": None, "estimate_basis": "unknown", "priority": 3,
                "evidence_fact_ids": [], "evidence_quote": event["content"]["text"],
                "suggested_action": "create", "matched_task_id": None, "reason": "原文",
                "confidence": 1.0}]
    return result


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "a.sqlite3", self.root / "a")
        seed(self.store)
        self.exchange = Exchange(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def export(self, key="first", **kwargs):
        return self.exchange.create_export(idempotency_key=key, **kwargs)

    def apply(self, kind, data, exchange=None):
        exchange = exchange or self.exchange
        preview = exchange.preview(kind, data)
        return exchange.apply(preview["preview_id"])

    def test_schema_definitions_and_parser(self):
        from jsonschema import Draft202012Validator
        for schema in (MESSAGES, RESULT):
            Draft202012Validator.check_schema(schema)
        self.assertEqual(loads_document('```json\n{"x":1}\n```'), {"x": 1})
        for text in ('{"x":1,"x":2}', '{"x":NaN}', 'text {"x":1}', '[]'):
            with self.assertRaises(ValueError):
                loads_document(text)

    def test_export_reserves_but_never_consumes_or_moves_scan_cursor(self):
        batch = self.export(mode="incremental")
        self.assertEqual(batch["event_count"], 2)
        self.assertEqual(batch["parsed_count"], 0)
        self.assertEqual(self.store.get_mail_cursor("demo@example.org", "INBOX")["uid"], 2)
        self.assertIsNone(self.store.claim_next_event("legacy-worker"))
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM event_consumptions").fetchone()[0], 0)
        self.assertEqual(self.export("next", mode="incremental")["event_count"], 0)
        files = list(Path(batch["directory"]).glob("*"))
        self.assertEqual(len(files), 7)
        self.assertTrue(list((self.store.data_dir / "backups").glob("*.sqlite3")))

    def test_idempotency_key_reuses_snapshot_and_rejects_config_change(self):
        first = self.export()
        second = self.export()
        self.assertEqual(first["id"], second["id"])
        with self.assertRaises(ValueError):
            self.export(limit=1)

    def test_reinitialization_does_not_advance_event_sequence(self):
        with self.store.connect() as conn:
            before = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='event_order'").fetchone()[0]
        for _ in range(3):
            Exchange(self.store)
        with self.store.connect() as conn:
            after = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='event_order'").fetchone()[0]
        self.assertEqual(before, after)

    def test_partial_results_and_duplicate_apply_are_audited(self):
        batch = self.export()
        data = reply(self.exchange, batch["id"], candidates=True)
        data["results"] = data["results"][:1]
        first = self.apply("results", data)
        self.assertEqual(first["needs_review"], 1)
        self.assertEqual(first["tasks_created"], 0)
        self.assertEqual(len(self.exchange.candidates()), 1)
        self.assertEqual(len(self.store.tasks()), 0)
        self.assertEqual(self.exchange.batch(batch["id"])["missing_count"], 1)
        self.assertEqual(self.apply("results", data)["already_applied"], 1)
        self.assertTrue(self.exchange.apply(first["preview_id"])["replayed"])
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM event_consumptions").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 0)
        self.assertEqual(json.loads(self.exchange.file(batch["id"], "analysis-result.json")[0]), data)

    def test_no_tasks_is_success_and_needs_review_is_not_retried(self):
        batch = self.export()
        data = reply(self.exchange, batch["id"])
        data["results"][1]["status"] = "needs_review"
        self.apply("results", data)
        result = self.exchange.batch(batch["id"])
        self.assertTrue(result["all_parsed"])
        self.assertEqual(result["review_event_count"], 1)
        self.assertIsNone(self.store.claim_next_event("worker"))

    def test_failure_can_be_retried_with_different_result(self):
        batch = self.export()
        template = json.loads(self.exchange.file(batch["id"], "analysis-result.template.json")[0])
        self.assertEqual(self.apply("results", template)["failed"], 2)
        self.assertEqual(self.exchange.batch(batch["id"])["failed_count"], 2)
        successful = reply(self.exchange, batch["id"])
        self.apply("results", successful)
        self.assertTrue(self.exchange.batch(batch["id"])["all_parsed"])
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM event_consumptions").fetchone()[0], 4)

    def test_invalid_or_mismatched_results_cannot_partially_write(self):
        batch = self.export()
        original = reply(self.exchange, batch["id"], candidates=True)
        mutations = [
            lambda x: x.update(batch_id="wrong"),
            lambda x: x["results"][1].update(event_id="unknown"),
            lambda x: x["results"][1]["candidates"][0].update(matched_task_id="invented"),
            lambda x: x["results"][1]["candidates"][0].update(estimated_minutes=-5),
            lambda x: x["results"][1]["candidates"][0].update(evidence_quote="not in the message"),
            lambda x: x["results"].append(x["results"][0]),
            lambda x: x["results"][1]["candidates"][0].update(confidence=float("nan")),
        ]
        for mutate in mutations:
            data = copy.deepcopy(original)
            mutate(data)
            with self.assertRaises(ValueError):
                self.exchange.preview("results", data)
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM event_consumptions").fetchone()[0], 0)

    def test_changed_successful_result_is_not_overwritten(self):
        batch = self.export()
        data = reply(self.exchange, batch["id"])
        self.apply("results", data)
        data["results"][0]["warnings"] = ["changed"]
        with self.assertRaises(ValueError):
            self.exchange.preview("results", data)

    def test_cancel_releases_missing_but_not_successful_events(self):
        batch = self.export()
        data = reply(self.exchange, batch["id"])
        data["results"] = data["results"][:1]
        self.apply("results", data)
        pending = reply(self.exchange, batch["id"])
        preview = self.exchange.preview("results", pending)
        self.exchange.cancel(batch["id"])
        with self.assertRaises(ValueError):
            self.exchange.apply(preview["preview_id"])
        second = self.export("new-request")
        self.assertEqual(second["event_count"], 1)

    def test_preview_expires_if_another_import_has_applied(self):
        batch = self.export()
        data = reply(self.exchange, batch["id"])
        a = self.exchange.preview("results", data)
        b = self.exchange.preview("results", data)
        self.exchange.apply(a["preview_id"])
        with self.assertRaises(ValueError):
            self.exchange.apply(b["preview_id"])

    def test_export_failure_recovers_same_batch_before_checkpoint_advance(self):
        with patch("taskagent.exchange.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.export(mode="incremental")
        failed = self.exchange.batches()[0]
        self.assertEqual(failed["state"], "preparing")
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT cursor FROM exchange_channels").fetchone()[0], 0)
        with self.assertRaises(ValueError):
            self.export("other", mode="incremental")
        recovered = self.export(mode="incremental")
        self.assertEqual(recovered["id"], failed["id"])
        self.assertEqual(recovered["state"], "active")

    def test_range_uses_source_time_and_retains_checkpoint(self):
        batch = self.export(mode="range", start="2026-10-20T09:30:00+08:00", end="2026-10-20T11:00:00+08:00")
        self.assertEqual(batch["event_count"], 1)
        self.assertEqual(self.export("inc", mode="incremental")["event_count"], 1)

    def test_import_roundtrip_preserves_origin_and_does_not_touch_real_cursors(self):
        batch = self.export()
        data = json.loads(self.exchange.file(batch["id"], "messages.json")[0])
        store_b = Store(self.root / "b.sqlite3", self.root / "b")
        exchange_b = Exchange(store_b)
        first = self.apply("messages", data, exchange_b)
        self.assertEqual(first["new_events"], 2)
        self.assertEqual(self.apply("messages", data, exchange_b)["already_present"], 2)
        with store_b.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM source_cursors").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM source_scopes WHERE enabled=1").fetchone()[0], 0)
        back = exchange_b.create_export(idempotency_key="b-to-a")
        returned = json.loads(exchange_b.file(back["id"], "messages.json")[0])
        self.assertEqual(returned["events"][0]["origin"], data["events"][0]["origin"])
        self.assertEqual(self.apply("messages", returned)["already_present"], 2)
        self.assertEqual(self.store.stats()["events"], 2)

    def test_import_native_identity_alias_and_changed_origin_conflict(self):
        batch = self.export()
        data = json.loads(self.exchange.file(batch["id"], "messages.json")[0])
        b = Exchange(Store(self.root / "b.sqlite3", self.root / "b"))
        self.apply("messages", data, b)
        alias = copy.deepcopy(data)
        alias["batch_id"] = "another-batch"
        alias["events"][0]["origin"]["event_id"] = "alias-event"
        alias["events"][1]["origin"]["event_id"] = "alias-event2"
        self.assertEqual(self.apply("messages", alias, b)["new_events"], 0)
        self.assertEqual(b.store.stats()["events"], 2)
        altered = copy.deepcopy(data)
        altered["batch_id"] = "changed-content"
        altered["events"][0]["content"]["text"] = "altered"
        with self.assertRaises(ValueError):
            b.preview("messages", altered)

    def test_new_event_after_exchange_initialization_has_monotonic_sequence(self):
        first = self.export(limit=1, mode="incremental")
        self.store.ingest_mail_range_batch("demo@example.org", "INBOX", "17", [
            {"uid": 4, "raw": b"late old message", "body_text": "old",
             "metadata": {"received_at": "2026-01-01T00:00:00+08:00"}}])
        second = self.export("second", mode="incremental")
        self.assertEqual(first["event_count"], 1)
        self.assertEqual(second["event_count"], 2)
        self.assertGreater(second["selection"]["through_sequence"], first["selection"]["through_sequence"])

    def test_late_legacy_worker_cannot_clear_new_exchange_reservation(self):
        old = self.store.claim_next_event("old")
        with self.store.transaction() as conn:
            conn.execute("UPDATE events SET lease_until='2000-01-01T00:00:00+00:00',created_at='2000-01-01T00:00:00+00:00' WHERE id=?", (old["event_id"],))
        fresh = self.store.claim_next_event("fresh")
        self.assertEqual(fresh["event_id"], old["event_id"])
        self.store.fail_event(fresh, "retry later")
        batch = self.export()
        with self.assertRaises(RuntimeError):
            self.store.complete_event(old, {"tasks": []}, agent_name="old", agent_version="1")
        self.store.fail_event(old, "late error")
        with self.store.connect() as conn:
            row = conn.execute("SELECT status,lease_owner FROM events WHERE id=?", (old["event_id"],)).fetchone()
        self.assertEqual(row["status"], "awaiting_result")
        self.assertEqual(row["lease_owner"], "exchange:" + batch["request_id"])


if __name__ == "__main__":
    unittest.main()
