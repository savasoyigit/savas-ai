"""
Job alert mails from a Gmail inbox over IMAP (plan-j karar B, revised
2026-11-16: Microsoft no longer lets a personal account register an app,
so Outlook.com redirects the alerts to Gmail).

Read only: the inbox is opened read-only and bodies are fetched with
BODY.PEEK (nothing is marked read). secrets/gmail.env holds GMAIL_ADDRESS
and GMAIL_APP_PASSWORD (a Google app password); never printed.

  python3 automations/jobs/mail.py check   (scripts/set_gmail.sh runs this)
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import lib

HOST = "imap.gmail.com"
MAIL_FILE = Path(os.getenv("SAVAS_MAIL_FILE", lib.ROOT / "secrets" / "gmail.env"))
FIRST_LOOK_DAYS = 3
MAX_MESSAGES = 200
SITE_DOMAINS = ("linkedin.com", "kariyer.net")
ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


class MailNotConfigured(lib.AutomationError):
    pass


def read_settings(path: Path = MAIL_FILE) -> dict:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise MailNotConfigured(f"{path.name} yok: scripts/set_gmail.sh") from exc
    settings = dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))
    if not settings.get("GMAIL_ADDRESS") or not settings.get("GMAIL_APP_PASSWORD"):
        raise MailNotConfigured(f"{path.name} eksik: scripts/set_gmail.sh")
    return settings


def connect(path: Path = MAIL_FILE) -> imaplib.IMAP4_SSL:
    settings = read_settings(path)
    try:
        imap = imaplib.IMAP4_SSL(HOST, 993, timeout=lib.TIMEOUT)
        imap.login(settings["GMAIL_ADDRESS"], settings["GMAIL_APP_PASSWORD"])
    except imaplib.IMAP4.error as exc:
        # The server's text, never the password.
        raise lib.AutomationError("Gmail girişi reddedildi (uygulama şifresini set_gmail.sh ile yenile)") from exc
    except OSError as exc:
        raise lib.AutomationError(f"Gmail bağlantısı kurulamadı: {type(exc).__name__}") from exc
    return imap


def alert_sender(message: email.message.Message, text: str) -> str:
    """
    The alert's own sender. A redirected mail keeps it in From; a forwarded
    one names it in its headers or in the quoted "From:" line of the body.
    """
    headers = [str(message[h]) for h in ("From", "Reply-To", "Sender", "Resent-From", "X-Original-From") if message[h]]
    # One header at a time: a malformed one must not hide the others (strict parsing).
    candidates = [address for header in headers for _, address in getaddresses([header]) if address]
    candidates += ADDRESS.findall(text[:5000])
    for address in candidates:
        domain = address.rsplit("@", 1)[-1].lower()
        if any(domain == d or domain.endswith("." + d) for d in SITE_DOMAINS):
            return address
    return candidates[0] if candidates else ""


def to_message(raw: bytes) -> dict:
    """An RFC 822 mail -> the shape parsers.parse reads."""
    message = email.message_from_bytes(raw, policy=email.policy.default)
    part = message.get_body(preferencelist=("html", "plain"))
    body = part.get_content() if part else ""
    if part is not None and part.get_content_type() == "text/plain":
        # Plain text: keep the links clickable for the parsers.
        body = re.sub(r"(https://\S+)", r'<a href="\1">\1</a>', body).replace("\n", "<br>")
    try:
        received = parsedate_to_datetime(message["Date"]).astimezone(timezone.utc)
    except (TypeError, ValueError):
        received = datetime.now(timezone.utc)
    return {
        "from": {"emailAddress": {"address": alert_sender(message, body)}},
        "subject": str(message.get("Subject", "")),
        "receivedDateTime": received.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "body": {"content": body},
    }


def fetch_new(state: dict, path: Path = MAIL_FILE) -> list[dict]:
    """
    Inbox mails newer than state["mail_uid"] (first look: the last few
    days), oldest first; state is updated in place.
    """
    imap = connect(path)
    try:
        status, data = imap.select("INBOX", readonly=True)
        if status != "OK":
            raise lib.AutomationError("Gmail INBOX açılamadı")
        validity = (imap.response("UIDVALIDITY")[1] or [b""])[0]
        validity = validity.decode() if isinstance(validity, bytes) else str(validity or "")
        last = state.get("mail_uid") if state.get("mail_uidvalidity") == validity else None

        if last is None:
            since = (datetime.now(timezone.utc) - timedelta(days=FIRST_LOOK_DAYS)).strftime("%d-%b-%Y")
            status, data = imap.uid("SEARCH", None, "SINCE", since)
        else:
            status, data = imap.uid("SEARCH", None, "UID", f"{int(last) + 1}:*")
        uids = sorted(int(u) for u in (data[0] or b"").split())
        # "n:*" always returns the newest mail, even when it is older than n.
        uids = [u for u in uids if last is None or u > int(last)][-MAX_MESSAGES:]

        messages = []
        for uid in uids:
            status, parts = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
            raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
            if status == "OK" and raw:
                messages.append(to_message(raw))
        if uids:
            state["mail_uid"] = uids[-1]
        elif last is None:
            status, data = imap.uid("SEARCH", None, "ALL")
            every = [int(u) for u in (data[0] or b"").split()]
            state["mail_uid"] = max(every, default=0)
        state["mail_uidvalidity"] = validity
        return messages
    finally:
        try:
            imap.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def check():
    """Log in once and say how many mails the inbox has; run by set_gmail.sh."""
    imap = connect()
    try:
        status, data = imap.select("INBOX", readonly=True)
        print(f"Gmail bağlantısı tamam: INBOX {data[0].decode() if status == 'OK' else '?'} mesaj (salt okunur).")
    finally:
        imap.logout()


if __name__ == "__main__":
    if sys.argv[1:] == ["check"]:
        try:
            check()
        except lib.AutomationError as exc:
            sys.exit(str(exc))
    else:
        sys.exit("kullanım: mail.py check")
