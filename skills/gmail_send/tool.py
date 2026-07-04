from __future__ import annotations

import base64
import json
import sys
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_google_common"))

from lisan_google import GMAIL_API, GoogleAuthError, api_request, headers_dict  # noqa: E402


def run(args: dict[str, Any], vault: Path, config: dict[str, Any]) -> str:
    body_text = str(args.get("body") or "").strip()
    if not body_text:
        return "Error: body is required"
    to = str(args.get("to") or "").strip()
    subject = str(args.get("subject") or "").strip()
    cc = str(args.get("cc") or "").strip()
    reply_id = str(args.get("reply_to_message_id") or "").strip()

    thread_id = ""
    try:
        if reply_id:
            original = api_request(
                "GET",
                f"{GMAIL_API}/messages/{reply_id}",
                params={
                    "format": "metadata",
                    "metadataHeaders": ["From", "Reply-To", "Subject", "Message-ID"],
                },
                config=config,
            )
            headers = headers_dict(original)
            thread_id = str(original.get("threadId") or "")
            if not to:
                to = headers.get("reply-to") or headers.get("from", "")
            if not subject:
                subject = headers.get("subject", "")
                if subject and not subject.lower().startswith("re:"):
                    subject = f"Re: {subject}"
        if not to:
            return "Error: no recipient — pass 'to' or a reply_to_message_id"
        if not subject:
            return "Error: no subject — pass 'subject' (or reply to a message that has one)"

        message = MIMEText(body_text)
        message["To"] = to
        message["Subject"] = subject
        if cc:
            message["Cc"] = cc
        if reply_id:
            original_mid = headers.get("message-id", "")
            if original_mid:
                message["In-Reply-To"] = original_mid
                message["References"] = original_mid

        payload: dict[str, Any] = {
            "raw": base64.urlsafe_b64encode(message.as_bytes()).decode()
        }
        if thread_id:
            payload["threadId"] = thread_id
        result = api_request(
            "POST", f"{GMAIL_API}/messages/send", body=payload, config=config
        )
    except GoogleAuthError as exc:
        return f"Error: {exc}"
    return json.dumps(
        {
            "status": "sent",
            "id": result.get("id", ""),
            "threadId": result.get("threadId", ""),
            "to": to,
            "subject": subject,
        },
        indent=2,
        ensure_ascii=False,
    )
