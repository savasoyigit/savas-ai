"""
CEO gateway (roadmap K-3b, docs/plan-k.md Ek A; Web Push K-3f, Ek B).

127.0.0.1:18010 -> login -> an allow-listed proxy to the orchestrator.
The orchestrator itself is unchanged and stays local without a login.
With VAPID keys the gateway also watches the approval queue and sends
"approval waiting" notifications to the subscribed phones.
"""

import asyncio
import hmac
import html
import json
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import parse_qs, quote

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

import auth
import push

ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://ai-orchestrator:8000").rstrip("/")
FAIL_DELAY_SECONDS = float(os.getenv("UI_FAIL_DELAY_SECONDS", "1"))
MAX_FORM_BYTES = 4096
MAX_ACTION_BYTES = 16384

config = auth.Config(
    os.getenv("UI_USER"),
    os.getenv("UI_PASSWORD_HASH"),
    os.getenv("UI_SESSION_KEY"),
    float(os.getenv("UI_SESSION_HOURS", "12")),
)
lockout = auth.Lockout()

LOGIN_PAGE = (Path(__file__).parent / "login.html").read_text(encoding="utf-8")

# Web Push (Ek B). Without keys or a writable /data the rest works, push is off.
log = logging.getLogger("uvicorn.error")
PUSH_POLL_SECONDS = float(os.getenv("PUSH_POLL_SECONDS", "15"))
PUSH_SUBJECT = os.getenv("VAPID_SUBJECT", "")
store = push.Store(os.getenv("PUSH_STORE", "/data/push.json"))
store_lock = asyncio.Lock()
try:
    vapid_key = push.load_vapid_key(os.getenv("VAPID_PRIVATE_KEY"))
except push.PushError:
    vapid_key = None
PUSH_READY = bool(vapid_key and PUSH_SUBJECT.startswith(("mailto:", "https://")) and store.writable())
LOCAL_TZ = ZoneInfo(os.getenv("TZ", "Europe/Istanbul"))
# J-2 (plan-j F): host automations notify through POST /internal/notify with this
# key (secrets/automation.env, written by scripts/set_automation_key.sh).
NOTIFY_KEY = os.getenv("SAVAS_NOTIFY_KEY", "")
NOTIFY_PER_HOUR = 30
notify_sent: deque = deque()

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    # Not "no-referrer": with it browsers send "Origin: null" on a form post and
    # the login's same-origin check refuses every real login (seen live, K-3b).
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}
# Hop-by-hop and length headers are the gateway's own business.
DROP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding", "server", "date"}

client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))


async def send_all(message: dict) -> list[int]:
    """One notification to every subscription; gone ones (404/410) are dropped."""
    payload = json.dumps(message, ensure_ascii=False).encode()
    statuses = []
    for sub in store.load()["subscriptions"]:
        try:
            body = push.encrypt(payload, sub["p256dh"], sub["auth"])
            answer = await client.post(sub["endpoint"], content=body,
                                       headers=push.headers(sub["endpoint"], vapid_key, PUSH_SUBJECT))
            status = answer.status_code
        except (httpx.HTTPError, push.PushError) as exc:
            log.warning("push send failed: %s", type(exc).__name__)
            status = 0
        if status in (404, 410):
            async with store_lock:
                store.remove(sub["endpoint"])
        elif status >= 300 or status == 0:
            log.warning("push service answered %s", status)
        statuses.append(status)
    return statuses


async def watch_approvals():
    """
    Every PUSH_POLL_SECONDS: new pending approvals (B1), and failures,
    budget blocks and model alerts from /activity (plan-j L) -> notifications.
    """
    while True:
        try:
            answer = await client.get(ORCHESTRATOR_URL + "/approvals", params={"status": "pending"})
            answer.raise_for_status()
            pending = answer.json().get("approvals") or []
            async with store_lock:
                before = store.load()
                fresh, data = push.new_approvals(pending, before)
                if data != before:
                    store.save(data)
            for approval in fresh:
                now = datetime.now(LOCAL_TZ).strftime("%H:%M")
                statuses = await send_all(push.notification(approval, now))
                log.info("approval notification: %s/%s -> %s",
                         approval.get("agent"), approval.get("action"), statuses)

            answer = await client.get(ORCHESTRATOR_URL + "/activity", params={"limit": 200})
            answer.raise_for_status()
            events = answer.json().get("events") or []
            async with store_lock:
                before = store.load()
                alerts, data = push.new_alerts(events, before, time.time())
                if data != before:
                    store.save(data)
            for message in alerts:
                statuses = await send_all(message)
                log.info("alert notification: %s -> %s", message["title"], statuses)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the watcher must outlive a bad poll
            log.warning("watch failed: %s", type(exc).__name__)
        await asyncio.sleep(PUSH_POLL_SECONDS)


@asynccontextmanager
async def lifespan(_app):
    watcher = asyncio.create_task(watch_approvals()) if PUSH_READY else None
    log.info("web push %s", "on" if PUSH_READY else "off (no VAPID keys or /data not writable)")
    yield
    if watcher:
        watcher.cancel()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    if request.url.path.startswith(("/ui/", "/login", "/session", "/push/")) and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-store"
    return response


def login_page(error: str = "", next_path: str = "/ui/", status: int = 200) -> HTMLResponse:
    body = LOGIN_PAGE.replace("{{error}}", html.escape(error)).replace("{{next}}", html.escape(next_path, quote=True))
    if not config.ready:
        body = body.replace("{{notice}}", "Giriş yapılandırılmamış: sunucuda <code>scripts/set_ui_password.sh</code> çalıştırılmalı.")
    else:
        body = body.replace("{{notice}}", "")
    return HTMLResponse(body, status_code=status)


@app.get("/")
async def root():
    return RedirectResponse("/ui/", status_code=302)


@app.get("/login")
async def login_form(next: str | None = None):
    return login_page(next_path=auth.safe_next(next))


@app.post("/login")
async def login(request: Request):
    if not auth.same_origin(request.headers.get("origin"), request.headers.get("host")):
        return login_page("İstek bu siteden gelmedi.", status=403)

    raw = await request.body()
    form = parse_qs(raw[:MAX_FORM_BYTES].decode("utf-8", "replace"))
    user = (form.get("username") or [""])[0]
    password = (form.get("password") or [""])[0]
    next_path = auth.safe_next((form.get("next") or [None])[0])

    wait = lockout.locked()
    if wait:
        return login_page(f"Çok fazla hatalı deneme. {wait // 60 + 1} dakika sonra tekrar deneyin.", next_path, 429)

    if not config.check_login(user, password):
        lockout.fail()
        await asyncio.sleep(FAIL_DELAY_SECONDS)
        return login_page("Kullanıcı adı veya şifre hatalı.", next_path, 401)

    lockout.success()
    response = RedirectResponse(next_path, status_code=303)
    response.set_cookie(auth.COOKIE, config.make_cookie(), max_age=config.seconds,
                        httponly=True, secure=True, samesite="strict", path="/")
    return response


@app.post("/logout")
async def logout(request: Request):
    if not auth.same_origin(request.headers.get("origin"), request.headers.get("host")):
        return JSONResponse({"detail": "forbidden"}, status_code=403)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    return response


@app.get("/session")
async def session(request: Request):
    user = config.read_cookie(request.cookies.get(auth.COOKIE))
    if not user:
        return JSONResponse({"detail": "login required"}, status_code=401)
    return {"user": user}


def refuse(request: Request, csrf: bool) -> JSONResponse | None:
    """Session required; a state change also needs this site's Origin and the CSRF header."""
    if not config.read_cookie(request.cookies.get(auth.COOKIE)):
        return JSONResponse({"detail": "login required"}, status_code=401)
    if csrf and not auth.csrf_ok(request.headers):
        return JSONResponse({"detail": "forbidden"}, status_code=403)
    return None


@app.get("/push/status")
async def push_status(request: Request):
    if denied := refuse(request, csrf=False):
        return denied
    return {"enabled": PUSH_READY, "subscriptions": len(store.load()["subscriptions"]) if PUSH_READY else 0}


@app.get("/push/key")
async def push_key(request: Request):
    if denied := refuse(request, csrf=False):
        return denied
    if not PUSH_READY:
        return JSONResponse({"detail": "push is not configured"}, status_code=503)
    return {"key": push.vapid_public(vapid_key)}


async def json_body(request: Request) -> dict | None:
    raw = await request.body()
    if len(raw) > MAX_FORM_BYTES:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


@app.post("/push/subscribe")
async def push_subscribe(request: Request):
    if denied := refuse(request, csrf=True):
        return denied
    if not PUSH_READY:
        return JSONResponse({"detail": "push is not configured"}, status_code=503)
    try:
        subscription = push.check_subscription(await json_body(request))
    except push.PushError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=422)
    async with store_lock:
        data = store.add(subscription)
    return {"status": "subscribed", "subscriptions": len(data["subscriptions"])}


@app.post("/push/unsubscribe")
async def push_unsubscribe(request: Request):
    if denied := refuse(request, csrf=True):
        return denied
    body = await json_body(request) or {}
    async with store_lock:
        removed = store.remove(str(body.get("endpoint") or ""))
    return {"status": "removed" if removed else "unknown"}


@app.post("/push/test")
async def push_test(request: Request):
    if denied := refuse(request, csrf=True):
        return denied
    if not PUSH_READY:
        return JSONResponse({"detail": "push is not configured"}, status_code=503)
    now = datetime.now(LOCAL_TZ).strftime("%H:%M")
    statuses = await send_all({"title": "SAVAS AI", "body": f"Test bildirimi · {now}", "url": "/ui/", "tag": "test"})
    return {"sent": len(statuses), "statuses": statuses}


@app.post("/internal/notify")
async def internal_notify(request: Request):
    """A host automation's message to the phones (plan-j F). Needs the automation key."""
    given = request.headers.get("x-savas-notify-key", "")
    if len(NOTIFY_KEY) < 32 or not hmac.compare_digest(given.encode(), NOTIFY_KEY.encode()):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    if not PUSH_READY:
        return JSONResponse({"detail": "push is not configured"}, status_code=503)
    try:
        message = push.check_notify(await json_body(request))
    except push.PushError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    now = time.time()
    while notify_sent and now - notify_sent[0] > 3600:
        notify_sent.popleft()
    if len(notify_sent) >= NOTIFY_PER_HOUR:
        return JSONResponse({"detail": "too many notifications this hour"}, status_code=429)
    notify_sent.append(now)

    statuses = await send_all(message)
    log.info("automation notification: %s -> %s", message["title"], statuses)
    return {"sent": len(statuses), "statuses": statuses}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def catch_all(request: Request, path: str):
    return await proxy(request, path)


async def proxy(request: Request, path: str) -> Response:
    target = "/" + path
    method = request.method

    if not auth.allowed(method, target):
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    if not auth.is_public(method, target) and not config.read_cookie(request.cookies.get(auth.COOKIE)):
        if target.startswith("/ui/") and method == "GET":
            return RedirectResponse("/login?next=" + quote(target), status_code=302)
        return JSONResponse({"detail": "login required"}, status_code=401)

    headers = {"accept": request.headers.get("accept", "*/*")}  # no cookie, no auth header upstream
    body = None
    if method != "GET":
        if not auth.csrf_ok(request.headers):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        body = await request.body()
        if len(body) > MAX_ACTION_BYTES:
            return JSONResponse({"detail": "request too large"}, status_code=413)
        headers["content-type"] = "application/json"

    try:
        upstream = await client.request(
            method,
            ORCHESTRATOR_URL + target,
            params=request.query_params,
            headers=headers,
            content=body,
        )
    except httpx.HTTPError:
        return JSONResponse({"detail": "orchestrator unreachable"}, status_code=502)

    headers = {k: v for k, v in upstream.headers.items() if k.lower() not in DROP_HEADERS}
    # A redirect from the orchestrator names its internal address; keep it on this site.
    if "location" in headers and headers["location"].startswith(ORCHESTRATOR_URL):
        headers["location"] = headers["location"][len(ORCHESTRATOR_URL):] or "/"
    return Response(upstream.content, status_code=upstream.status_code, headers=headers)
