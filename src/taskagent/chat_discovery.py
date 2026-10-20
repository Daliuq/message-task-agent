from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


class ScannerUnavailable(RuntimeError):
    pass


def _wechat_python() -> Path:
    configured = os.environ.get("WECHAT_PYTHON", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return path
        raise ScannerUnavailable(f"WECHAT_PYTHON 指向的解释器不存在：{path}")

    candidates = [
        Path.home() / "Documents/Codex/2026-10-14/wei-x/work/wechatauto-env/Scripts/python.exe",
    ]
    codex_dir = Path.home() / "Documents/Codex"
    if codex_dir.is_dir():
        candidates.extend(codex_dir.glob("*/wei-x/work/wechatauto-env/Scripts/python.exe"))
    for path in candidates:
        if path.is_file():
            return path
    raise ScannerUnavailable(
        "未找到已安装的微信读取环境。请设置 WECHAT_PYTHON 指向 wechatauto-env/Scripts/python.exe。"
    )


def discover_wechat_chats(*, timeout_seconds: int = 120) -> dict[str, Any]:
    """Enumerate local WeChat accounts and chats without exporting message bodies."""
    worker = Path(__file__).with_name("wechat_discovery_worker.py")
    try:
        completed = subprocess.run(
            [str(_wechat_python()), str(worker)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"微信会话发现超时（{timeout_seconds} 秒）") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()[-1:] or completed.stdout.strip().splitlines()[-1:]
        raise RuntimeError(detail[0][:500] if detail else "微信读取进程失败")
    marker = "__CHAT_DISCOVERY_RESULT__"
    lines = [line[len(marker):] for line in completed.stdout.splitlines() if line.startswith(marker)]
    if not lines:
        raise RuntimeError("微信读取进程没有返回有效的发现结果")
    try:
        result = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError("微信读取进程返回了无效 JSON") from exc
    candidates = result.get("candidates")
    warnings = result.get("warnings", [])
    if not isinstance(candidates, list) or not isinstance(warnings, list):
        raise RuntimeError("微信读取结果格式无效")
    return {"candidates": candidates, "warnings": [str(item)[:500] for item in warnings]}


def discover_chat_candidates(platform: str, *, data_dir=None) -> dict[str, Any]:
    if platform == "wechat":
        return discover_wechat_chats()
    if platform == "qq":
        from .qq import discover
        return discover(data_dir)
    raise ValueError("platform 只支持 wechat 或 qq")

