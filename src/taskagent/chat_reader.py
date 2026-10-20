"""Private local subprocess protocol: each yielded page is acknowledged after DB commit."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

from .chat_discovery import _wechat_python

MARKER = "__CHAT_IMPORT__"


def read_wechat_messages(scope, config, checkpoint, live_cursor, *, timeout=900, cancelled=None):
    worker = Path(__file__).with_name("wechat_message_worker.py")
    yield from _read_worker(_wechat_python(), worker, {"scope": scope, "config": config,
        "checkpoint": checkpoint, "live_cursor": live_cursor}, timeout=timeout, cancelled=cancelled, name="微信")


def read_qq_messages(scope, config, checkpoint, live_cursor, *, data_dir=None, timeout=900, cancelled=None):
    from .qq import ROOT, request_for, runtime_python
    request = {**request_for(scope["account_key"], data_dir), "action": "import", "scope": scope,
               "config": config, "checkpoint": checkpoint, "live_cursor": live_cursor}
    yield from _read_worker(runtime_python(), ROOT / "worker.py", request, timeout=timeout, cancelled=cancelled, name="QQ")


def _read_worker(python, worker, request, *, timeout, cancelled, name):
    proc = subprocess.Popen([str(python), str(worker)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
        errors="strict", env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    messages = queue.Queue(maxsize=4)
    def pump():
        try:
            for line in proc.stdout:
                if line.startswith(MARKER):
                    messages.put(line[len(MARKER):])
        except Exception:
            pass
        finally:
            # Don't leave a pipe reader stuck if the consuming job is cancelled.
            try:
                messages.put(None, timeout=1)
            except queue.Full:
                pass
    threading.Thread(target=pump, daemon=True).start()
    try:
        proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        saw_done = False
        last_activity = time.monotonic()
        while True:
            if cancelled:
                cancelled()
            try:
                line = messages.get(timeout=min(1, timeout))
            except queue.Empty as exc:
                if time.monotonic() - last_activity >= timeout:
                    raise RuntimeError(name + "读取超时；已提交分页保留，可恢复导入") from exc
                continue
            last_activity = time.monotonic()
            if line is None:
                break
            frame = json.loads(line)
            if frame.get("type") == "error":
                raise RuntimeError(frame.get("error", name + "消息读取失败"))
            yield frame
            if frame.get("type") == "done":
                saw_done = True
            proc.stdin.write("ACK\n")
            proc.stdin.flush()
        if proc.wait(timeout=10) != 0 or not saw_done:
            raise RuntimeError(name + "读取进程异常结束；未把残缺数据标记为完成")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        for stream in (proc.stdin, proc.stdout):
            if stream:
                stream.close()


def qq_local_status(root=None, data_dir=None):
    from .qq import status
    return status(data_dir=data_dir, root=root)
