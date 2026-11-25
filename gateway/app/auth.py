"""
CEO gateway: login and request rules (roadmap K-3b, docs/plan-k.md Ek A).

Pure functions and stdlib only, so the tests run without FastAPI. The
password is stored as a scrypt hash in secrets/ui-auth.env, written by
the user with scripts/set_ui_password.sh; nothing here prints it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time

SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN = 2 ** 14, 8, 1, 32
COOKIE = "savas_session"
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60

# What the page may reach through the gateway (A5). Anything else: 404.
ALLOWED = (
    ("GET", re.compile(r"/ui/.*")),
    ("GET", re.compile(r"/(health|workers|activity|approvals|usage/today|tasks|jobs)")),
    # L-1a: metrics report, read only.
    ("GET", re.compile(r"/metrics")),
    # J-3: job listings, read only (POST /jobs/import stays local).
    ("GET", re.compile(r"/jobs/[0-9a-f-]{8,64}")),
    # J-5: application package, saving it (as approved note proposals) and the user's tracking.
    ("POST", re.compile(r"/jobs/[0-9a-f-]{8,64}/(package|package/save|status)")),
    ("GET", re.compile(r"/jobs/[0-9a-f-]{8,64}/package/(cv|letter)\.docx")),
    ("GET", re.compile(r"/career/photo")),
    # L-3b (plan-l3 G): goals; starting one writes packages (LLM), nothing is sent anywhere.
    ("GET", re.compile(r"/goals(/[0-9a-f-]{8,64})?")),
    ("POST", re.compile(r"/goals")),
    ("POST", re.compile(r"/goals/[0-9a-f-]{8,64}/continue")),
    ("GET", re.compile(r"/tasks/[0-9a-f-]{8,64}")),
    # K-4 actions (A5): the orchestrator's own endpoints; the Approval Layer is unchanged.
    ("POST", re.compile(r"/task")),
    ("POST", re.compile(r"/approvals/[0-9a-f-]{8,64}/(approve|reject)")),
    ("POST", re.compile(r"/tasks/[0-9a-f-]{8,64}/(cancel|review)")),
    # K-5 chat with a worker (no tools; the orchestrator refuses Ares and unknown workers).
    ("GET", re.compile(r"/workers/[a-z0-9_-]{1,32}/chat")),
    ("POST", re.compile(r"/workers/[a-z0-9_-]{1,32}/chat")),
)
# Fetched by the phone without a session (home-screen icon, manifest).
PUBLIC = re.compile(r"/ui/(manifest\.json|icons/[a-z0-9-]+\.(png|svg))")


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_LEN)
    # ":" not "$": docker compose reads "$1" in an env_file as a variable (seen live, K-3b).
    return f"scrypt:{SCRYPT_N}:{SCRYPT_R}:{SCRYPT_P}:{b64(salt)}:{b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, digest = re.split(r"[:$]", stored)
        if kind != "scrypt":
            return False
        expected = unb64(digest)
        got = hashlib.scrypt(password.encode(), salt=unb64(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


class Config:
    """The login settings; unusable (fail closed) when the secret file is missing."""

    def __init__(self, user: str | None, password_hash: str | None, session_key: str | None, hours: float = 12):
        self.user = user or ""
        self.password_hash = password_hash or ""
        self.session_key = (session_key or "").encode()
        self.seconds = int(hours * 3600)

    @property
    def ready(self) -> bool:
        return bool(self.user and self.password_hash.startswith(("scrypt:", "scrypt$")) and len(self.session_key) >= 32)

    def check_login(self, user: str, password: str) -> bool:
        if not self.ready:
            return False
        same_user = hmac.compare_digest(user.encode(), self.user.encode())
        good = verify_password(password, self.password_hash)  # always computed: same time for a wrong user
        return same_user and good

    def signing_key(self) -> bytes:
        # A new password ends every open session.
        return hmac.new(self.session_key, self.password_hash.encode(), hashlib.sha256).digest()

    def make_cookie(self, now: float | None = None) -> str:
        payload = b64(json.dumps({"u": self.user, "exp": int((time.time() if now is None else now) + self.seconds)}).encode())
        mac = b64(hmac.new(self.signing_key(), payload.encode(), hashlib.sha256).digest())
        return f"{payload}.{mac}"

    def read_cookie(self, value: str | None, now: float | None = None) -> str | None:
        """The user of a valid, unexpired cookie, else None."""
        if not self.ready or not value or value.count(".") != 1:
            return None
        payload, mac = value.split(".")
        expected = b64(hmac.new(self.signing_key(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(mac, expected):
            return None
        try:
            data = json.loads(unb64(payload))
        except ValueError:
            return None
        if data.get("u") != self.user or data.get("exp", 0) < (time.time() if now is None else now):
            return None
        return data["u"]


class Lockout:
    """
    Failed logins. Behind tailscale serve every request comes from the
    same proxy address, so the count is global: one user, one lock (A4).
    """

    def __init__(self, max_failures: int = MAX_FAILURES, seconds: int = LOCK_SECONDS):
        self.max_failures = max_failures
        self.seconds = seconds
        self.failures: list[float] = []
        self.locked_until = 0.0

    def locked(self, now: float | None = None) -> int:
        """Seconds left on the lock, 0 when open."""
        return max(0, int(self.locked_until - (time.time() if now is None else now)))

    def fail(self, now: float | None = None):
        now = time.time() if now is None else now
        self.failures = [t for t in self.failures if now - t < self.seconds] + [now]
        if len(self.failures) >= self.max_failures:
            self.locked_until = now + self.seconds
            self.failures = []

    def success(self):
        self.failures = []


def allowed(method: str, path: str) -> bool:
    return any(method == m and rule.fullmatch(path) for m, rule in ALLOWED)


def is_public(method: str, path: str) -> bool:
    return method == "GET" and bool(PUBLIC.fullmatch(path))


def csrf_ok(headers) -> bool:
    """A state change through the gateway: this site's Origin and the page's own header (A5)."""
    return headers.get("x-savas-csrf") == "1" and same_origin(headers.get("origin"), headers.get("host"))


def safe_next(target: str | None) -> str:
    """Only a path on this site after login; never another host."""
    if target and target.startswith("/ui/") and "//" not in target and "\\" not in target:
        return target
    return "/ui/"


def same_origin(origin: str | None, host: str | None) -> bool:
    """A form post from this site: Origin's host equals Host (A4, login CSRF)."""
    if not origin or not host:
        return False
    return re.sub(r"^https?://", "", origin).rstrip("/") == host
