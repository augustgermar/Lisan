"""Direct SMTP transport migrated from the legacy Lisan mail service."""
from __future__ import annotations

import mimetypes
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Any


def _valid_address(value: str) -> bool:
    return bool(value.strip()) and "@" in value and "\n" not in value and "\r" not in value


def _resolve(value: str, domain: str) -> str:
    value = value.strip().lower()
    if "@" not in value:
        value = f"{value}@{domain}"
    if not _valid_address(value):
        raise ValueError(f"invalid email address: {value}")
    return value


def send_email(*, subject: str, body: str, recipients: list[str], config: dict[str, Any],
               html_body: str | None = None, sender: str | None = None,
               attachments: list[str] | None = None, dry_run: bool = False) -> dict[str, Any]:
    mail = config.get("mail", {})
    sender = (sender or mail.get("sender") or "").strip()
    relay = str(mail.get("relay") or "localhost").strip()
    port = int(mail.get("port") or 25)
    domain = str(mail.get("default_domain") or "").strip()
    if not sender:
        raise ValueError(
            "no sender address configured — set mail.sender in config.json"
        )
    subject = " ".join(subject.replace("\r", " ").replace("\n", " ").split())
    if not subject:
        raise ValueError("email subject is required")
    if not _valid_address(sender):
        raise ValueError(f"invalid sender address: {sender}")
    resolved = list(dict.fromkeys(_resolve(item, domain) for item in recipients))
    if not resolved:
        raise ValueError("at least one recipient is required")
    paths = [str(Path(item)) for item in (attachments or [])]
    for path in paths:
        if not Path(path).is_file() or not Path(path).readable():
            raise ValueError(f"attachment is not readable: {path}")
    result = {"ok": True, "dry_run": dry_run, "sender": sender, "recipients": resolved,
              "relay": relay, "port": port, "attachments": [Path(p).name for p in paths]}
    if dry_run:
        result["accepted_response"] = "250 dry-run; not sent"
        return result
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(resolved)
    message["Reply-To"] = sender
    message["Subject"] = subject
    message.set_content(body)
    if html_body:
        message.add_alternative(html_body, subtype="html")
    for path in paths:
        file_path = Path(path)
        content_type, _ = mimetypes.guess_type(file_path.name)
        maintype, subtype = (content_type or "application/octet-stream").split("/", 1)
        message.add_attachment(file_path.read_bytes(), maintype=maintype, subtype=subtype, filename=file_path.name)
    with smtplib.SMTP(relay, port, timeout=15) as smtp:
        smtp.ehlo()
        smtp.send_message(message, from_addr=sender, to_addrs=resolved)
    result["accepted_response"] = "250 accepted by SMTP relay"
    return result
