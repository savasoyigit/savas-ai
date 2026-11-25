"""
Web Push for the CEO gateway (roadmap K-3f, docs/plan-k.md Ek B).

RFC 8291 (aes128gcm message encryption) and RFC 8292 (VAPID) with
`cryptography` only; the HTTP send is in main.py. Also the small state
file (subscriptions, approvals already announced) and the rule that says
which pending approvals are new.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import struct
import time
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

RECORD_SIZE = 4096
JWT_SECONDS = 3600  # Apple's push service refuses long-lived tokens; one hour is safe everywhere.
MAX_SUBSCRIPTIONS = 10
MAX_SEEN = 500
# Where a subscription may point (B5): the browsers' push services, nothing else (no SSRF).
PUSH_HOSTS = ("web.push.apple.com", "fcm.googleapis.com", "updates.push.services.mozilla.com")
PUSH_HOST_SUFFIXES = (".push.apple.com",)


# J-2 (plan-j L): alerts from /activity, and messages from host automations.
ALERT_COOLDOWN = 30 * 60
NOTIFY_TITLE_CHARS = 60
NOTIFY_BODY_CHARS = 200
NOTIFY_TAG = re.compile(r"[a-z0-9-]{1,40}")


class PushError(ValueError):
    """A subscription or key that cannot be used; the message is safe to show."""


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def public_bytes(key) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


# ----------------------------------------------------------------------
# RFC 8291: message encryption (one record)
# ----------------------------------------------------------------------

def encrypt(payload: bytes, p256dh: str, auth: str, *, salt: bytes | None = None, server_key=None) -> bytes:
    """The aes128gcm body for one subscription."""
    ua_public = unb64(p256dh)
    auth_secret = unb64(auth)
    if len(ua_public) != 65 or len(auth_secret) != 16:
        raise PushError("subscription keys have the wrong size")
    if len(payload) > RECORD_SIZE - 17 - 86:
        raise PushError("payload too large")

    server_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_public = public_bytes(server_key)
    try:
        ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    except ValueError as exc:
        raise PushError("subscription key is not a P-256 point") from exc
    shared = server_key.exchange(ec.ECDH(), ua_key)

    key_info = b"WebPush: info\x00" + ua_public + as_public
    ikm = _hmac(_hmac(auth_secret, shared), key_info + b"\x01")
    salt = salt or os.urandom(16)
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]

    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)  # 0x02: last record, no padding
    header = salt + struct.pack(">I", RECORD_SIZE) + bytes([len(as_public)]) + as_public
    return header + ciphertext


# ----------------------------------------------------------------------
# RFC 8292: VAPID
# ----------------------------------------------------------------------

def new_vapid_key() -> str:
    """A new private key as the raw 32-byte scalar, base64url (no "$": compose-safe)."""
    key = ec.generate_private_key(ec.SECP256R1())
    return b64(key.private_numbers().private_value.to_bytes(32, "big"))


def load_vapid_key(private_b64: str | None):
    try:
        raw = unb64(private_b64 or "")
        if len(raw) != 32:
            raise ValueError
        return ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())
    except (ValueError, TypeError) as exc:
        raise PushError("VAPID private key is missing or malformed") from exc


def vapid_public(key) -> str:
    return b64(public_bytes(key))


def vapid_header(endpoint: str, key, subject: str, now: float | None = None) -> str:
    parts = urlsplit(endpoint)
    claims = {
        "aud": f"{parts.scheme}://{parts.netloc}",
        "exp": int((time.time() if now is None else now) + JWT_SECONDS),
        "sub": subject,
    }
    signing_input = (
        b64(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
        + "."
        + b64(json.dumps(claims, separators=(",", ":")).encode())
    )
    r, s = decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
    token = signing_input + "." + b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={token}, k={vapid_public(key)}"


def headers(endpoint: str, key, subject: str, now: float | None = None) -> dict:
    return {
        "Authorization": vapid_header(endpoint, key, subject, now),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": "43200",
        "Urgency": "high",
    }


# ----------------------------------------------------------------------
# subscriptions and the approvals already announced
# ----------------------------------------------------------------------

def check_subscription(data) -> dict:
    """A browser PushSubscription.toJSON() -> {endpoint, p256dh, auth}. Raises PushError."""
    if not isinstance(data, dict):
        raise PushError("subscription must be an object")
    endpoint = data.get("endpoint")
    keys = data.get("keys") or {}
    if not isinstance(endpoint, str) or len(endpoint) > 1024:
        raise PushError("endpoint missing")
    parts = urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    if (
        parts.scheme != "https" or parts.port not in (None, 443) or parts.username or parts.password
        or not (host in PUSH_HOSTS or host.endswith(PUSH_HOST_SUFFIXES))
    ):
        raise PushError("endpoint is not a known push service")
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    try:
        if len(unb64(p256dh)) != 65 or len(unb64(auth)) != 16:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise PushError("subscription keys are malformed") from exc
    return {"endpoint": endpoint, "p256dh": p256dh, "auth": auth}


def new_approvals(pending: list[dict], state: dict) -> tuple[list[dict], dict]:
    """
    Which pending approvals to announce, and the next state. The first
    look only learns what is already waiting (B1): no burst after setup.
    """
    ids = [a["approval_id"] for a in pending if a.get("approval_id")]
    seen = list(state.get("seen") or [])
    if not state.get("initialized"):
        return [], {**state, "initialized": True, "seen": ids[-MAX_SEEN:]}
    fresh = [a for a in pending if a.get("approval_id") and a["approval_id"] not in seen]
    seen = (seen + [a["approval_id"] for a in fresh])[-MAX_SEEN:]
    return fresh, {**state, "seen": seen}


def clip(text, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


GOAL_TITLES = {"completed": "Hedef bitti", "waiting": "Hedef ilan metni bekliyor",
               "stopped": "Hedef durdu", "failed": "Hedef başarısız"}


def alert(event: dict) -> tuple[str, dict] | None:
    """(cooldown key, notification) for an event worth a push, else None."""
    kind, status = event.get("event"), event.get("status")
    agent = event.get("worker") or event.get("agent") or "?"
    model = event.get("model") or "?"

    if kind == "task" and status in ("failed", "background_error"):
        task_id = str(event.get("task_id") or "")
        key, title = f"task-{task_id}", "Görev başarısız"
        body, url = clip(event.get("error") or "hata ayrıntısı yok", 120), f"/ui/#task={task_id}"
    elif (kind == "budget_check" or kind == "chat") and status == "blocked":
        key, title = f"budget-{agent}", "Bütçe sınırı"
        body, url = f"{agent}: {clip(event.get('error') or 'bütçe aşıldı', 100)}", "/ui/"
    elif kind == "model_alert":
        key, title = f"model-alert-{model}", "Model uyarısı"
        status_text = f" · HTTP {event['http_status']}" if event.get("http_status") else ""
        body, url = f"{model} · {event.get('kind') or '?'}{status_text}", "/ui/"
    elif kind == "model_circuit" and status == "open":
        key, title = f"model-open-{model}", "Model geçici devre dışı"
        body, url = f"{model} · {event.get('seconds', '?')} sn", "/ui/"
    elif kind == "goal" and status in GOAL_TITLES:
        # L-3b: a goal the user started has ended or needs listing texts.
        goal_id = str(event.get("goal_id") or "")
        key, title = f"goal-{goal_id}-{status}", GOAL_TITLES[status]
        body = clip(event.get("error") or f"{event.get('used', '?')} token", 120)
        url = "/ui/#goals"
    else:
        return None

    tag = "alert-" + hashlib.sha256(key.encode()).hexdigest()[:12]
    return key, {"title": title, "body": body, "url": url, "tag": tag}


def new_alerts(events: list[dict], state: dict, now: float) -> tuple[list[dict], dict]:
    """
    Alerts for events newer than the last look, one per key per
    ALERT_COOLDOWN. The first look only learns where the log is.
    """
    stamps = [e["timestamp"] for e in events if e.get("timestamp")]
    after = state.get("activity_after")
    if after is None:
        return [], {**state, "activity_after": max(stamps, default=""), "alerted": {}}

    alerted = {k: t for k, t in (state.get("alerted") or {}).items() if now - t < ALERT_COOLDOWN}
    out = []
    for event in sorted((e for e in events if e.get("timestamp", "") > after), key=lambda e: e["timestamp"]):
        found = alert(event)
        if found and found[0] not in alerted:
            alerted[found[0]] = now
            out.append(found[1])
    return out, {**state, "activity_after": max([after, *stamps]), "alerted": alerted}


def check_notify(data) -> dict:
    """A message from a host automation (POST /internal/notify). Raises PushError."""
    if not isinstance(data, dict):
        raise PushError("message must be an object")
    title, body = data.get("title"), data.get("body", "")
    url, tag = data.get("url", "/ui/"), data.get("tag", "automation")
    if not isinstance(title, str) or not title.strip() or len(title) > NOTIFY_TITLE_CHARS:
        raise PushError(f"title must be 1-{NOTIFY_TITLE_CHARS} characters")
    if not isinstance(body, str) or len(body) > NOTIFY_BODY_CHARS:
        raise PushError(f"body must be at most {NOTIFY_BODY_CHARS} characters")
    # Only a page of this site: a notification never sends the phone elsewhere.
    if not isinstance(url, str) or not url.startswith("/ui/") or "//" in url or "\\" in url or len(url) > 200:
        raise PushError("url must be a /ui/ path")
    if not isinstance(tag, str) or not NOTIFY_TAG.fullmatch(tag):
        raise PushError("tag must be [a-z0-9-]{1,40}")
    return {"title": title.strip(), "body": body, "url": url, "tag": tag}


def notification(approval: dict, local_time: str) -> dict:
    """Least information (B6): who wants what, and when. No arguments, no note text."""
    return {
        "title": "Onay bekliyor",
        "body": f"{approval.get('agent', '?')}/{approval.get('action', '?')} · {local_time}",
        "url": "/ui/#approvals",
        "tag": f"approval-{approval.get('approval_id', '')}",
    }


class Store:
    """data/gateway/push.json; written atomically, mode 600."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        loaded = {"subscriptions": list(data.get("subscriptions") or []),
                  "seen": list(data.get("seen") or []), "initialized": bool(data.get("initialized"))}
        # J-2: where the activity log was last read, and recent alert keys.
        if "activity_after" in data:
            loaded["activity_after"] = data["activity_after"]
            loaded["alerted"] = dict(data.get("alerted") or {})
        return loaded

    def save(self, data: dict):
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, self.path)

    def writable(self) -> bool:
        return self.path.parent.is_dir() and os.access(self.path.parent, os.W_OK)

    def add(self, subscription: dict) -> dict:
        data = self.load()
        subs = [s for s in data["subscriptions"] if s["endpoint"] != subscription["endpoint"]]
        subs.append({**subscription, "created": int(time.time())})
        data["subscriptions"] = subs[-MAX_SUBSCRIPTIONS:]
        self.save(data)
        return data

    def remove(self, endpoint: str) -> bool:
        data = self.load()
        before = len(data["subscriptions"])
        data["subscriptions"] = [s for s in data["subscriptions"] if s["endpoint"] != endpoint]
        if len(data["subscriptions"]) != before:
            self.save(data)
            return True
        return False
