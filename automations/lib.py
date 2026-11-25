"""
Shared pieces for host automations (roadmap J-2, docs/plan-j.md karar K).

Stdlib only; run by systemd user timers (automations/systemd/). An
automation reads the orchestrator API on 127.0.0.1, keeps its state in
data/automations/, and tells the phones through the gateway's
POST /internal/notify. Nothing here prints a secret.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(os.getenv("SAVAS_AI_ROOT", Path(__file__).resolve().parents[1]))
STATE_DIR = Path(os.getenv("SAVAS_AUTOMATION_STATE", ROOT / "data" / "automations"))
KEY_FILE = Path(os.getenv("SAVAS_AUTOMATION_KEY_FILE", ROOT / "secrets" / "automation.env"))
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://127.0.0.1:18000").rstrip("/")
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:18010").rstrip("/")
LOCAL_TZ = ZoneInfo(os.getenv("TZ", "Europe/Istanbul"))
TIMEOUT = 30
# Some APIs (Remotive) refuse urllib's default agent; say who is asking.
USER_AGENT = "SAVAS-AI/1.0 (personal job search; stdlib urllib)"


class AutomationError(Exception):
    pass


def log(name: str, message: str):
    """One line to stdout: journald keeps it (journalctl --user -u savas-<name>)."""
    print(f"[{name}] {message}", flush=True)


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def local_date(iso: str | None) -> str | None:
    """An API timestamp (UTC ISO) -> local YYYY-MM-DD."""
    if not iso:
        return None
    return datetime.fromisoformat(iso).astimezone(LOCAL_TZ).date().isoformat()


def request(method: str, url: str, body=None, headers: dict | None = None, timeout: float = TIMEOUT):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={
        "accept": "application/json", "user-agent": USER_AGENT,
        **({"content-type": "application/json"} if data else {}), **(headers or {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as answer:
            return json.loads(answer.read() or b"null")
    except urllib.error.HTTPError as exc:
        raise AutomationError(f"{method} {url} -> HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise AutomationError(f"{method} {url} -> {type(exc).__name__}") from exc


def api(path: str):
    return request("GET", ORCHESTRATOR_URL + path)


def notify_key() -> str:
    """SAVAS_NOTIFY_KEY from the environment or secrets/automation.env; never printed."""
    if os.getenv("SAVAS_NOTIFY_KEY"):
        return os.environ["SAVAS_NOTIFY_KEY"]
    try:
        for line in KEY_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("SAVAS_NOTIFY_KEY="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    raise AutomationError(f"no notify key ({KEY_FILE.name} missing: scripts/set_automation_key.sh)")


def notify(title: str, body: str = "", url: str = "/ui/", tag: str = "automation") -> dict:
    return request("POST", GATEWAY_URL + "/internal/notify",
                   {"title": title, "body": body, "url": url, "tag": tag},
                   {"x-savas-notify-key": notify_key()})


def propose_note(path: str, content: str, reason: str) -> dict:
    """An Obsidian note for the user's approval (POST /notes/propose); written on approve."""
    return request("POST", ORCHESTRATOR_URL + "/notes/propose", {"path": path, "content": content, "reason": reason})


class State:
    """data/automations/<name>.json; written atomically."""

    def __init__(self, name: str):
        self.path = STATE_DIR / f"{name}.json"

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save(self, data: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


@contextmanager
def single_run(name: str):
    """One copy at a time: a second start while the first runs exits quietly."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / f"{name}.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log(name, "already running; skipped")
            sys.exit(0)
        yield


def main(name: str, run):
    """Entry point: lock, run, and a non-zero exit (journald, systemd) on failure."""
    with single_run(name):
        try:
            run()
        except AutomationError as exc:
            log(name, f"failed: {exc}")
            sys.exit(1)
