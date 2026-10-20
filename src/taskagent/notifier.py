from __future__ import annotations

import hashlib
import smtplib
from email.message import EmailMessage
from typing import Any

from .store import Store

SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465


def send_next_notification(store: Store, address: str, auth_code: str, recipient: str,
                           *, timeout: int = 25) -> dict[str, Any] | None:
    """Send one queued message; uncertain SMTP DATA completion is never auto-retried."""
    store.recover_interrupted_deliveries()
    notification = store.claim_next_notification(recipient)
    if notification is None:
        return None
    key = notification["dedupe_key"]
    message = EmailMessage()
    message["From"] = address
    message["To"] = recipient
    message["Subject"] = notification["subject"]
    message["Message-ID"] = f"<task-notify-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:28]}@local.invalid>"
    message.set_content(notification["body"])

    smtp = None
    phase = "connect"
    try:
        smtp = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=timeout)
        smtp.login(address, auth_code)
        code, response = smtp.mail(address)
        if code >= 400:
            raise RuntimeError(f"SMTP MAIL rejected: {code} {response!r}")
        code, response = smtp.rcpt(recipient)
        if code >= 400:
            raise RuntimeError(f"SMTP RCPT rejected: {code} {response!r}")
        phase = "data"
        code, response = smtp.data(message.as_bytes())
        if code >= 400:
            store.finish_delivery(notification, "failed", smtp_response=f"{code} {response!r}")
            return {"id": notification["id"], "status": "failed", "smtp_response": str(response)}
        store.finish_delivery(notification, "accepted", smtp_response=f"{code} {response!r}")
        return {"id": notification["id"], "status": "sent", "smtp_response": str(response)}
    except Exception as exc:
        outcome = "delivery_unknown" if phase == "data" else "failed"
        error = f"{type(exc).__name__}: {exc}".replace(auth_code, "[REDACTED]")[:2000]
        store.finish_delivery(notification, outcome, error=error)
        return {"id": notification["id"], "status": outcome, "error": error}
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:
                pass
