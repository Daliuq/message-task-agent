"""Validate an in-memory QQ database passphrase against a private snapshot.

No passphrase, message body, or plaintext database is written to disk.
Uses the pinned QQBackup/nt_msg_db_util SQLCipher opener unchanged.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile


def verify(request):
    source = Path(request["database"])
    helper = Path(__file__).parent / "vendor" / "nt_msg_db_util" / "1.decrypt.py"
    spec = importlib.util.spec_from_file_location("qq_upstream_decrypt", helper)
    module = importlib.util.module_from_spec(spec)
    # Do not load unrelated application .env files into this isolated worker.
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    spec.loader.exec_module(module)
    module.DB_KEY = request["key"]
    with tempfile.TemporaryDirectory(prefix="mta-qq-key-") as tmp:
        snapshot = Path(tmp) / "nt_msg.db"
        paths = [source] + [Path(str(source) + suffix) for suffix in ("-wal",)]
        before = {str(p): (p.stat().st_size, p.stat().st_mtime_ns)
                  for p in paths if p.exists()}
        with contextlib.redirect_stdout(io.StringIO()):
            module.strip_header(source, snapshot)
        for suffix in ("-wal",):
            sidecar = Path(str(source) + suffix)
            if sidecar.exists():
                shutil.copyfile(sidecar, str(snapshot) + suffix)
        after = {str(p): (p.stat().st_size, p.stat().st_mtime_ns)
                 for p in paths if p.exists()}
        if before != after:
            raise RuntimeError("source_changed_during_snapshot")
        conn = module.open_enc(snapshot)
        try:
            conn.execute("PRAGMA query_only=ON")
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            verified = []
            for table in ("group_msg_table", "c2c_msg_table"):
                if table in names:
                    conn.execute('SELECT 1 FROM "' + table + '" LIMIT 1').fetchone()
                    verified.append(table)
            if not verified:
                raise RuntimeError("expected_qq_message_tables_missing")
            return {"verified": True, "table_count": len(names),
                    "message_tables_verified": verified,
                    "source_bytes": before[str(source)][0],
                    "scope": "schema_and_sample_page_not_full_integrity"}
        finally:
            conn.close()
            module.DB_KEY = ""


if __name__ == "__main__":
    try:
        result = verify(json.loads(sys.stdin.read()))
    except Exception:
        # Native SQLCipher details and exception values must not leak the key.
        result = {"verified": False, "error": "database_key_verification_failed"}
    print(json.dumps(result))
    sys.exit(0 if result.get("verified") else 1)
