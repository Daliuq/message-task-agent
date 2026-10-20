"""QQ native database bridge. Secrets stay in DPAPI files and child stdin only."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2] / "integrations" / "qq"


def runtime_python():
    path = ROOT / ".venv" / "Scripts" / "python.exe"
    if not path.is_file():
        raise ValueError("QQ 独立读取环境尚未安装")
    return path


def data_root():
    return Path(os.environ.get("QQ_DATA_ROOT") or Path.home() / "Documents/Tencent Files")


def _private_dir(data_dir=None):
    return Path(data_dir) / "qq" if data_dir is not None else ROOT.parents[1] / "data" / "qq"


def status(data_dir=None, root=None):
    root = Path(root) if root is not None else data_root()
    accounts = []
    if root.is_dir():
        for directory in sorted(root.iterdir()):
            database = directory / "nt_qq/nt_db/nt_msg.db"
            if not directory.name.isdecimal() or not database.is_file():
                continue
            key_file = _private_dir(data_dir) / (directory.name + ".dpapi.json")
            ready = False
            try:
                saved = json.loads(key_file.read_text(encoding="utf-8-sig"))
                ready = (saved.get("verified") is True and saved.get("account_key") == directory.name
                         and saved.get("protection") == "windows_dpapi_current_user"
                         and bool(saved.get("key_dpapi")))
            except (OSError, ValueError):
                pass
            accounts.append({"account": directory.name, "database": str(database),
                "bytes": database.stat().st_size, "readable": ready,
                "state": "ready" if ready else "awaiting_key"})
    runtime_ready = (ROOT / ".venv/Scripts/python.exe").is_file()
    supported = runtime_ready and any(a["readable"] for a in accounts)
    return {"root": str(root), "accounts": accounts, "supported": supported,
            "runtime_ready": runtime_ready,
            "note": "QQ 本地读取组件已接入；已验证密钥的账号可发现会话并导入。仅读取本机历史，不下载云端记录"}


def load_key(account, data_dir=None):
    if not isinstance(account, str) or not re.fullmatch(r"\d+", account):
        raise ValueError("QQ 账号标识无效")
    try:
        saved = json.loads((_private_dir(data_dir) / (account + ".dpapi.json")).read_text(encoding="utf-8-sig"))
        if saved.get("verified") is not True or saved.get("account_key") != account:
            raise ValueError()
        encrypted = bytes.fromhex(saved["key_dpapi"])
    except (OSError, ValueError, KeyError):
        raise ValueError("该 QQ 账号尚未保存通过验证的数据库密钥") from None
    if os.name != "nt":
        raise ValueError("此密钥仅可由保存它的 Windows 用户解密")

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buf = (ctypes.c_ubyte * len(encrypted)).from_buffer_copy(encrypted)
    source = Blob(len(encrypted), buf)
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt.CryptUnprotectData.restype = wintypes.BOOL
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ValueError("QQ 密钥解密失败；请使用保存密钥的同一 Windows 用户")
    try:
        return ctypes.string_at(target.data, target.size).decode("utf-16-le")
    finally:
        ctypes.memset(target.data, 0, target.size)
        kernel.LocalFree(target.data)


def request_for(account, data_dir=None):
    ready = next((a for a in status(data_dir)["accounts"] if a["account"] == account and a["readable"]), None)
    if not ready:
        raise ValueError("本机未找到该 QQ 账号数据库或已验证密钥，请先完成取钥")
    return {"account": account, "directory": str(Path(ready["database"]).parent),
            "key": load_key(account, data_dir)}


def discover(data_dir=None):
    accounts = [a for a in status(data_dir)["accounts"] if a["readable"]]
    if not accounts:
        from .chat_discovery import ScannerUnavailable
        raise ScannerUnavailable("未发现已保存有效密钥的 QQ 账号，请先取钥")
    candidates, warnings = [], []
    for account in accounts:
        request = {**request_for(account["account"], data_dir), "action": "discover"}
        try:
            result = subprocess.run([str(runtime_python()), str(ROOT / "worker.py")],
                input=json.dumps(request, ensure_ascii=False) + "\n", capture_output=True,
                text=True, encoding="utf-8", errors="strict", timeout=300,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            frames = [json.loads(line[len("__QQ_RESULT__"):]) for line in result.stdout.splitlines()
                      if line.startswith("__QQ_RESULT__")]
            if result.returncode or not frames:
                raise ValueError(frames[-1].get("error", "QQ 会话读取进程未正常完成") if frames else "QQ 会话读取进程未正常完成")
            candidates.extend(frames[-1]["candidates"])
            warnings.extend(frames[-1].get("warnings", []))
        except subprocess.TimeoutExpired:
            warnings.append("账号 " + account["account"] + " 读取超时；未把旧结果当成最新")
        except ValueError as exc:
            warnings.append("账号 " + account["account"] + "：" + str(exc))
    if not candidates and warnings:
        raise RuntimeError("；".join(warnings))
    return {"candidates": candidates, "warnings": warnings}
