from __future__ import annotations

import base64
import ctypes
import email
import imaplib
import json
import os
import re
from ctypes import wintypes
from datetime import date, datetime, time, timedelta, timezone
from email import policy
from email.utils import parsedate_to_datetime
from typing import Any

from .store import Store

IMAP_HOST = "imap.qq.com"
IMAP_PORT = 993
CREDENTIAL_TARGET = "CodexQQMailClient/primary"
CRED_TYPE_GENERIC = 1
BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")


class CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
    ]


def load_saved_credentials() -> dict[str, str] | None:
    """Read the existing QQ Mail app entry from Windows Credential Manager."""
    if os.name != "nt":
        raise RuntimeError("当前配置读取仅支持 Windows 凭据管理器")
    api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    ptr = ctypes.POINTER(CREDENTIALW)()
    if not api.CredReadW(CREDENTIAL_TARGET, CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
        error = ctypes.get_last_error()
        if error == 1168:
            return None
        raise ctypes.WinError(error)
    try:
        raw = ctypes.string_at(ptr.contents.CredentialBlob, ptr.contents.CredentialBlobSize)
        value = json.loads(raw.decode("utf-8"))
        address, auth_code = value.get("address"), value.get("auth_code")
        if not address or not auth_code:
            raise RuntimeError("已保存的QQ邮箱凭据不完整")
        return {"address": str(address), "auth_code": str(auth_code)}
    finally:
        api.CredFree(ptr)


def _decode_modified_utf7(value: str) -> str:
    def decode(match: re.Match[str]) -> str:
        encoded = match.group(1)
        if not encoded:
            return "&"
        padded = encoded.replace(",", "/") + "=" * ((-len(encoded)) % 4)
        return base64.b64decode(padded).decode("utf-16-be", errors="replace")
    return re.sub(r"&([A-Za-z0-9+,]*)-", decode, value)


def _encode_modified_utf7(value: str) -> str:
    chunks: list[str] = []
    non_ascii: list[str] = []
    def flush() -> None:
        if non_ascii:
            raw = "".join(non_ascii).encode("utf-16-be")
            chunks.append("&" + base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",") + "-")
            non_ascii.clear()
    for char in value:
        if char == "&":
            flush()
            chunks.append("&-")
        elif ord(char) < 0x20 or ord(char) > 0x7E:
            non_ascii.append(char)
        else:
            flush()
            chunks.append(char)
    flush()
    return "".join(chunks)


def _quote_imap_string(value: str) -> str:
    """Quote an IMAP astring, including mailbox names containing spaces."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _parse_list_line(line: bytes) -> tuple[bool, str] | None:
    match = re.search(rb'(\([^)]*\))\s+(?:NIL|"(?:[^"\\]|\\.)*")\s+("(?:[^"\\]|\\.)*"|[^ ]+)\s*$', line)
    if not match:
        return None
    flags = match.group(1).decode("ascii", errors="ignore").lower()
    raw_name = match.group(2).strip()
    if raw_name.startswith(b'"') and raw_name.endswith(b'"'):
        raw_name = raw_name[1:-1].replace(b'\\"', b'"').replace(b'\\\\', b'\\')
    name = _decode_modified_utf7(raw_name.decode("ascii", errors="replace"))
    return ("\\noselect" in flags, name)


def _list_folders(client: imaplib.IMAP4_SSL) -> list[str]:
    # Passing an empty string emits `LIST  *` in imaplib; RFC-style servers
    # expect the quoted empty reference name: `LIST "" *`.
    status, rows = client.list()
    if status != "OK":
        raise RuntimeError("无法列出邮箱文件夹")
    folders: list[str] = []
    for row in rows or []:
        if not isinstance(row, bytes):
            continue
        parsed = _parse_list_line(row)
        if parsed and not parsed[0]:
            folders.append(parsed[1])
    return list(dict.fromkeys(folders))


def _uidvalidity(client: imaplib.IMAP4_SSL) -> str | None:
    try:
        _name, data = client.response("UIDVALIDITY")
        if data and data[0]:
            match = re.search(rb"\d+", data[0] if isinstance(data[0], bytes) else str(data[0]).encode())
            return match.group(0).decode("ascii") if match else None
    except Exception:
        return None
    return None


def _body_text(msg: email.message.Message) -> str:
    plain: list[str] = []
    html: list[str] = []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            content = str(part.get_content())
        except Exception:
            payload = part.get_payload(decode=True) or b""
            content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        (plain if content_type == "text/plain" else html).append(content)
    return "\n\n".join(plain or html)


def _message(raw: bytes, uid: int, folder: str, uidvalidity: str | None,
             received_at: str | None = None) -> dict[str, Any]:
    msg = email.message_from_bytes(raw, policy=policy.default)
    date_raw = str(msg.get("Date", ""))
    try:
        parsed = parsedate_to_datetime(date_raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        date_iso = parsed.isoformat(timespec="seconds")
    except Exception:
        date_iso = date_raw
    attachments = []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        filename = part.get_filename()
        if filename or part.get_content_disposition() == "attachment":
            try:
                payload_size = len(part.get_payload(decode=True) or b"")
            except Exception:
                payload_size = None
            attachments.append({"filename": filename, "content_type": part.get_content_type(), "size_bytes": payload_size})
    metadata = {
        "uid": uid, "folder": folder, "uidvalidity": uidvalidity,
        "from": str(msg.get("From", "")), "to": str(msg.get("To", "")),
        "cc": str(msg.get("Cc", "")), "subject": str(msg.get("Subject", "(无主题)")),
        "date": date_iso, "message_id": str(msg.get("Message-ID", "")),
        "attachments": attachments,
    }
    if received_at:
        metadata["received_at"] = received_at
    return {"uid": uid, "raw": raw, "metadata": metadata, "body_text": _body_text(msg)}


def _internaldate(rows: list[Any] | tuple[Any, ...]) -> str | None:
    # RFC 3501 normally returns a quoted date with a numeric timezone. Be tolerant of
    # servers that space-pad the day, omit a leading zero on the hour, or emit +08:00.
    pattern = re.compile(rb'\bINTERNALDATE\s+"([^"]+)"', re.IGNORECASE)
    for row in rows or []:
        header = row[0] if isinstance(row, tuple) and row else row
        if not isinstance(header, bytes):
            continue
        match = pattern.search(header)
        if not match:
            continue
        try:
            value = match.group(1).decode("ascii").strip()
        except UnicodeDecodeError:
            continue
        value = re.sub(r"([+-]\d{2}):(\d{2})$", r"\1\2", value)
        try:
            received = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            continue
        # A timestamp without a timezone is not safe for exact Beijing-time filtering.
        if received is None or received.tzinfo is None:
            continue
        return received.isoformat(timespec="seconds")
    return None


def _fetch_message(client: imaplib.IMAP4_SSL, uid: int, folder: str,
                   uidvalidity: str | None, *, require_internaldate: bool = False) -> dict[str, Any]:
    status, rows = client.uid("fetch", str(uid), "(BODY.PEEK[] INTERNALDATE)")
    if status != "OK":
        raise RuntimeError(f"读取 UID {uid} 失败")
    raw = next((row[1] for row in (rows or [])
                if isinstance(row, tuple) and len(row) > 1 and isinstance(row[1], bytes)), None)
    if raw is None:
        raise RuntimeError(f"UID {uid} 未返回完整 MIME 邮件")
    received_at = _internaldate(rows or [])
    if require_internaldate and not received_at:
        raise RuntimeError(f"UID {uid} 未返回有效 INTERNALDATE，无法准确按收件时间过滤")
    return _message(raw, uid, folder, uidvalidity, received_at)


def _fetch_messages(client: imaplib.IMAP4_SSL, uids: list[int], folder: str,
                    uidvalidity: str | None) -> list[dict[str, Any]]:
    """Fetch one bounded UID page in a single IMAP round trip."""
    if not uids:
        return []
    uid_set = ",".join(str(uid) for uid in uids)
    status, rows = client.uid("fetch", uid_set, "(UID BODY.PEEK[] INTERNALDATE)")
    if status != "OK":
        raise RuntimeError(f"批量读取 UID {uid_set} 失败")
    messages: dict[int, dict[str, Any]] = {}
    uid_pattern = re.compile(rb"\bUID\s+(\d+)\b", re.IGNORECASE)
    for row in rows or []:
        if not isinstance(row, tuple) or len(row) < 2 or not isinstance(row[1], bytes):
            continue
        header = row[0]
        if not isinstance(header, bytes):
            continue
        match = uid_pattern.search(header)
        if not match:
            continue
        uid = int(match.group(1))
        if uid not in uids:
            continue
        received_at = _internaldate([row])
        if not received_at:
            # Some IMAP servers omit INTERNALDATE from a multi-UID FETCH response.
            # Retry this UID alone before failing; never substitute the RFC 822
            # Date header because it is the sender's timestamp, not receipt time.
            date_status, date_rows = client.uid("fetch", str(uid), "(INTERNALDATE)")
            if date_status == "OK":
                received_at = _internaldate(date_rows or [])
            if not received_at:
                raise RuntimeError(f"UID {uid} 未返回有效 INTERNALDATE，无法准确按收件时间过滤")
        messages[uid] = _message(row[1], uid, folder, uidvalidity, received_at)
    missing = [uid for uid in uids if uid not in messages]
    if missing:
        raise RuntimeError(f"批量响应缺少 UID：{', '.join(map(str, missing[:20]))}")
    return [messages[uid] for uid in uids]


def _range_bound(value: str | date | datetime, *, end: bool) -> datetime:
    """Date-only bounds use Beijing calendar days; datetime end bounds are exclusive."""
    date_only = False
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, time.min, BEIJING)
        date_only = True
    else:
        raw = str(value).strip()
        try:
            date_value = date.fromisoformat(raw)
        except ValueError:
            date_value = None
        if date_value is not None:
            result = datetime.combine(date_value, time.min, BEIJING)
            date_only = True
        else:
            result = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=BEIJING)
    result = result.astimezone(BEIJING)
    if end and date_only:
        result += timedelta(days=1)
    return result


def _imap_date(value: date) -> str:
    month = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")[value.month - 1]
    return f"{value.day:02d}-{month}-{value.year:04d}"


def scan_mailbox(
    store: Store,
    address: str,
    auth_code: str,
    *,
    max_per_folder: int = 25,
    import_history: bool = False,
    folders: list[str] | None = None,
    newest_first: bool = False,
) -> dict[str, Any]:
    """Read-only IMAP scan. The DB cursor moves only inside the persisted ingest transaction."""
    if max_per_folder < 1:
        raise ValueError("max_per_folder 必须大于 0")
    client = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=30)
    folders_result: list[dict[str, Any]] = []
    try:
        client.login(address, auth_code)
        available_folders = _list_folders(client)
        if folders is not None:
            requested = list(dict.fromkeys(folders))
            missing = [name for name in requested if name not in available_folders]
            if missing:
                raise ValueError(f"指定的邮箱文件夹不存在或不可选：{', '.join(missing)}")
            scan_folders = [name for name in available_folders if name in set(requested)]
        else:
            scan_folders = available_folders
        for folder in scan_folders:
            result: dict[str, Any] = {"folder": folder, "status": "pending", "new_count": 0}
            try:
                mailbox_name = _quote_imap_string(_encode_modified_utf7(folder))
                status, _ = client.select(mailbox_name, readonly=True)
                if status != "OK":
                    raise RuntimeError("无法以只读方式打开文件夹")
                validity = _uidvalidity(client)
                status, search = client.uid("search", None, "ALL")
                if status != "OK":
                    raise RuntimeError("UID SEARCH ALL 失败")
                all_uids = sorted({int(value) for value in (search[0].split() if search and search[0] else [])})
                highest = all_uids[-1] if all_uids else 0
                cursor = store.get_mail_cursor(address, folder)
                if cursor is None:
                    baseline_uid = 0 if import_history else highest
                    initialized = store.initialize_mail_cursor(address, folder, validity, baseline_uid,
                                                              status="history_import_started" if import_history else "baseline")
                    cursor = store.get_mail_cursor(address, folder)
                    if initialized and not import_history:
                        result.update(status="baseline", highest_uid=highest, message="首次运行建立基线，不导入历史邮件")
                        folders_result.append(result)
                        continue
                    if cursor is None:
                        raise RuntimeError("建立邮箱游标失败")
                elif cursor.get("uidvalidity") and validity and str(cursor["uidvalidity"]) != str(validity):
                    store.record_scan_status(address, folder, "requires_reconcile", cursor_before=cursor,
                                             error="UIDVALIDITY changed; cursor was not advanced")
                    result.update(status="requires_reconcile", error="UIDVALIDITY 已变化；游标未推进")
                    folders_result.append(result)
                    continue
                candidates = [uid for uid in all_uids if uid > int(cursor.get("uid", 0))]
                if import_history and newest_first:
                    pending = candidates[-max_per_folder:]
                else:
                    pending = candidates[:max_per_folder]
                if not pending:
                    store.record_scan_status(address, folder, "ok", cursor_before=cursor, cursor_after=cursor)
                    result.update(status="ok", more_pending=False)
                    folders_result.append(result)
                    continue
                messages = []
                for uid in pending:
                    messages.append(_fetch_message(client, uid, folder, validity))
                before = {"uid": int(cursor.get("uid", 0)), "uidvalidity": cursor.get("uidvalidity")}
                counts = store.ingest_mail_batch(address, folder, validity, messages, pending[-1], cursor_before=before)
                result.update(status="ok", new_count=counts["items"], new_versions=counts["versions"],
                              events=counts["events"], more_pending=any(uid > pending[-1] for uid in all_uids))
            except Exception as exc:
                safe_error = f"{type(exc).__name__}: {exc}".replace(auth_code, "[REDACTED]")
                result.update(status="failed", error=safe_error[:2000])
                try:
                    store.record_scan_status(address, folder, "failed", error=result["error"])
                except Exception:
                    pass
            folders_result.append(result)
        return {"ok": all(item["status"] not in {"failed", "requires_reconcile"} for item in folders_result),
                "account": address, "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "folders": folders_result}
    finally:
        try:
            client.logout()
        except Exception:
            pass


def scan_mailbox_range(
    store: Store,
    address: str,
    auth_code: str,
    start: str | date | datetime,
    end: str | date | datetime,
    *,
    folders: list[str] | None = None,
    batch_size: int = 50,
) -> dict[str, Any]:
    """Fully backfill messages received in [start, end) without changing UID cursors.

    Date-only input means inclusive Beijing calendar dates for both bounds. Datetime
    input uses an inclusive start and exclusive end; naive datetimes mean Beijing time.
    IMAP SEARCH narrows candidates by INTERNALDATE day, then fetched INTERNALDATE is
    checked precisely before any message is committed.
    """
    if batch_size < 1 or batch_size > 500:
        raise ValueError("batch_size 必须在 1 到 500 之间")
    range_start = _range_bound(start, end=False)
    range_end = _range_bound(end, end=True)
    if range_end <= range_start:
        raise ValueError("时间范围结束点必须晚于开始点")

    range_record = {
        "mode": "time_range_full",
        "start_inclusive": range_start.isoformat(timespec="seconds"),
        "end_exclusive": range_end.isoformat(timespec="seconds"),
    }
    # Widen the server-side day search by one day on either side. Exact local-time
    # filtering below prevents boundary drift across server/client time zones.
    search_since = range_start.date() - timedelta(days=1)
    search_before = range_end.date() + timedelta(days=2)
    client = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=60)
    folders_result: list[dict[str, Any]] = []
    try:
        client.login(address, auth_code)
        available_folders = _list_folders(client)
        if folders is not None:
            requested = list(dict.fromkeys(folders))
            missing = [name for name in requested if name not in available_folders]
            if missing:
                raise ValueError(f"指定的邮箱文件夹不存在或不可选：{', '.join(missing)}")
            scan_folders = [name for name in available_folders if name in set(requested)]
        else:
            scan_folders = available_folders

        for folder in scan_folders:
            result: dict[str, Any] = {
                "folder": folder, "status": "pending", "candidate_count": 0,
                "matched_count": 0, "new_count": 0, "new_versions": 0, "events": 0,
                "incremental_cursor_changed": False,
            }
            validity: str | None = None
            try:
                mailbox_name = _quote_imap_string(_encode_modified_utf7(folder))
                status, _ = client.select(mailbox_name, readonly=True)
                if status != "OK":
                    raise RuntimeError("无法以只读方式打开文件夹")
                validity = _uidvalidity(client)
                status, search = client.uid("search", None, "SINCE", _imap_date(search_since),
                                            "BEFORE", _imap_date(search_before))
                if status != "OK":
                    raise RuntimeError("UID SEARCH 时间范围失败")
                uids = sorted({int(value) for value in (search[0].split() if search and search[0] else [])})
                result["candidate_count"] = len(uids)
                totals = {"items": 0, "versions": 0, "events": 0}
                for offset in range(0, len(uids), batch_size):
                    page = uids[offset:offset + batch_size]
                    matching: list[dict[str, Any]] = []
                    for message in _fetch_messages(client, page, folder, validity):
                        received = datetime.fromisoformat(message["metadata"]["received_at"])
                        received = received.astimezone(BEIJING)
                        if range_start <= received < range_end:
                            matching.append(message)
                    result["matched_count"] += len(matching)
                    if matching:
                        counts = store.ingest_mail_range_batch(address, folder, validity, matching)
                        for key in totals:
                            totals[key] += counts[key]
                result.update(status="ok", new_count=totals["items"], new_versions=totals["versions"],
                              events=totals["events"])
                store.record_scan_status(
                    address, folder, "ok", item_count=result["matched_count"],
                    cursor_before={**range_record, "uidvalidity": validity},
                    cursor_after={"mode": "time_range_full", "uidvalidity": validity,
                                  "incremental_cursor_unchanged": True},
                )
            except Exception as exc:
                safe_error = f"{type(exc).__name__}: {exc}".replace(auth_code, "[REDACTED]")
                result.update(status="failed", error=safe_error[:2000])
                try:
                    store.record_scan_status(
                        address, folder, "failed", item_count=result["matched_count"],
                        cursor_before={**range_record, "uidvalidity": validity},
                        cursor_after={"mode": "time_range_full", "uidvalidity": validity,
                                      "incremental_cursor_unchanged": True}, error=safe_error[:2000],
                    )
                except Exception:
                    pass
            folders_result.append(result)
        return {"ok": all(item["status"] != "failed" for item in folders_result),
                "account": address, "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                **range_record, "folders": folders_result}
    finally:
        try:
            client.logout()
        except Exception:
            pass
