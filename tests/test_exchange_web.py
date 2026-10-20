import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from taskagent.cli import main
from taskagent.store import Store
from taskagent.webui import make_handler
from test_exchange import seed


class ExchangeHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db, self.data = self.root / "db.sqlite3", self.root / "data"
        self.store = Store(self.db, self.data)
        seed(self.store)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.db, self.data))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return json.loads(response.read())

    def post(self, path, value):
        request = urllib.request.Request(self.base + path, data=json.dumps(value).encode(),
                                        headers={"X-Requested-With": "fetch", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    def test_exchange_end_to_end_local_http(self):
        scopes = self.get("/api/exchange/scopes")
        batch = self.post("/api/exchange/exports", {"idempotency_key": "test", "scope_ids": [scopes[0]["id"]]})
        self.assertEqual(batch["event_count"], 2)
        request = self.get(f"/api/exchange/exports/{batch['id']}/files/analysis-request.json")
        self.assertFalse(request["dedup_policy"]["automatic_merge_enabled"])
        result = self.get(f"/api/exchange/exports/{batch['id']}/files/analysis-result.template.json")
        for r in result["results"]:
            r.update(status="ok", warnings=["无任务"])
        pre = self.post("/api/exchange/results/preview", {"document": json.dumps(result)})
        applied = self.post("/api/exchange/results/apply", {"preview_id": pre["preview_id"]})
        self.assertEqual(applied["parsed"], 2)
        self.assertTrue(self.get(f"/api/exchange/exports/{batch['id']}")["all_parsed"])
        self.assertEqual(self.store.tasks(), [])
        self.assertEqual(self.get("/api/prompts/task-extractor/export")["format"], "mta.prompt")

    def test_exchange_accepts_larger_documents_but_retains_origin_guard(self):
        batch = self.post("/api/exchange/exports", {"idempotency_key": "test"})
        result = self.get(f"/api/exchange/exports/{batch['id']}/files/analysis-result.template.json")
        result["results"][0]["warnings"] = ["x" * 20000]
        preview = self.post("/api/exchange/results/preview", {"document": json.dumps(result)})
        self.assertEqual(preview["failed"], 2)
        req = urllib.request.Request(
            self.base + "/api/exchange/results/apply", data=b"{}", headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(req)
        self.assertEqual(error.exception.code, 403)
        req = urllib.request.Request(
            self.base + "/api/exchange/results/apply", data=b"{}",
            headers={"X-Requested-With": "fetch", "Origin": "http://other.example", "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(req)
        self.assertEqual(error.exception.code, 403)

    def test_kind_mismatch_and_malformed_reply_do_not_apply(self):
        batch = self.post("/api/exchange/exports", {"idempotency_key": "test"})
        result = self.get(f"/api/exchange/exports/{batch['id']}/files/analysis-result.template.json")
        preview = self.post("/api/exchange/results/preview", {"document": json.dumps(result)})
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post("/api/exchange/messages/apply", {"preview_id": preview["preview_id"]})
        self.assertEqual(error.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError):
            self.post("/api/exchange/results/preview", {"document": '{"a":1,"a":2}'})
        self.assertEqual(self.get(f"/api/exchange/exports/{batch['id']}")["parsed_count"], 0)

    def test_cli_export_creates_usable_files(self):
        from contextlib import redirect_stdout
        import io
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["--db", str(self.db), "--data-dir", str(self.data),
                           "export-batch", "--key", "cli-test", "--limit", "1"])
        self.assertEqual(status, 0)
        batch = json.loads(output.getvalue())
        path = Path(batch["directory"])
        self.assertTrue((path / "messages.json").exists())
        self.assertTrue((path / "analysis-result.schema.json").exists())


if __name__ == "__main__":
    unittest.main()
