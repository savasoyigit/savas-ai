"""K-3b: CEO gateway login rules (docs/plan-k.md Ek A). Stdlib only, no FastAPI."""

import os
import stat
import subprocess
from pathlib import Path

import importlib.util

import pytest

# Loaded by path under its own name: no conftest.py and no sys.path entry here,
# because the orchestrator tests import their own `conftest` and `main` by name.
_spec = importlib.util.spec_from_file_location("gateway_auth", Path(__file__).resolve().parents[1] / "app" / "auth.py")
auth = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(auth)

KEY = "k" * 48
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def stored():
    return auth.hash_password("doğru-şifre-123")


@pytest.fixture
def config(stored):
    return auth.Config("savas", stored, KEY, hours=12)


# ----------------------------------------------------------------------
# password
# ----------------------------------------------------------------------

def test_hash_is_salted_scrypt_and_verifies(stored):
    assert stored.startswith("scrypt:16384:8:1:")
    assert "$" not in stored  # docker compose would expand it in the env_file
    assert auth.hash_password("doğru-şifre-123") != stored
    assert auth.verify_password("doğru-şifre-123", stored)
    assert not auth.verify_password("yanlış", stored)


@pytest.mark.parametrize("bad", ["", "düz", "md5$x", "scrypt$a$b$c$d$e", "scrypt$16384$8$1$!!$!!",
                                 "scrypt:16384:8", "scrypt$16384$8$1$xxxx"])
def test_broken_hashes_never_verify(bad):
    assert not auth.verify_password("x", bad)


def test_mangled_hash_like_the_live_one_is_refused():
    # What compose left of a "$" hash: 4 fields. It must close the door, not crash.
    assert not auth.verify_password("x", "scrypt68$1xx$abc")


def test_old_dollar_format_still_verifies():
    import hashlib
    salt = b"0123456789abcdef"
    digest = hashlib.scrypt(b"eski", salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    assert auth.verify_password("eski", f"scrypt$16384$8$1${auth.b64(salt)}${auth.b64(digest)}")


def test_login_needs_both_user_and_password(config):
    assert config.check_login("savas", "doğru-şifre-123")
    assert not config.check_login("savas", "yanlış")
    assert not config.check_login("baska", "doğru-şifre-123")


@pytest.mark.parametrize("user,hash_,key", [
    (None, "scrypt:x", KEY), ("savas", None, KEY), ("savas", "scrypt:x", None), ("savas", "scrypt:x", "kısa"),
])
def test_missing_settings_close_the_door(user, hash_, key):
    config = auth.Config(user, hash_, key)
    assert not config.ready
    assert not config.check_login("savas", "x")
    assert config.read_cookie("a.b") is None


# ----------------------------------------------------------------------
# session cookie
# ----------------------------------------------------------------------

def test_cookie_round_trip_and_expiry(config):
    cookie = config.make_cookie(now=1000)
    assert config.read_cookie(cookie, now=1000 + 12 * 3600 - 1) == "savas"
    assert config.read_cookie(cookie, now=1000 + 12 * 3600 + 1) is None


def test_tampered_or_foreign_cookies_are_refused(config, stored):
    cookie = config.make_cookie(now=1000)
    payload, mac = cookie.split(".")
    forged = auth.b64(b'{"u": "savas", "exp": 99999999999}')
    for value in (None, "", "x", "a.b.c", f"{forged}.{mac}", f"{payload}.{mac[:-2]}xx"):
        assert config.read_cookie(value, now=1000) is None
    other_key = auth.Config("savas", stored, "z" * 48)
    assert other_key.read_cookie(cookie, now=1000) is None


def test_a_new_password_ends_open_sessions(config):
    cookie = config.make_cookie(now=1000)
    changed = auth.Config("savas", auth.hash_password("yeni-şifre-456"), KEY)
    assert changed.read_cookie(cookie, now=1000) is None


# ----------------------------------------------------------------------
# lockout
# ----------------------------------------------------------------------

def test_five_failures_lock_for_fifteen_minutes():
    lock = auth.Lockout()
    for i in range(4):
        lock.fail(now=100 + i)
    assert lock.locked(now=104) == 0
    lock.fail(now=105)
    assert lock.locked(now=105) == 15 * 60
    assert lock.locked(now=105 + 15 * 60) == 0


def test_old_failures_and_a_success_reset_the_count():
    lock = auth.Lockout()
    for i in range(4):
        lock.fail(now=i)
    lock.fail(now=2000)  # the first four are older than the window
    assert lock.locked(now=2000) == 0
    for i in range(3):
        lock.fail(now=2001 + i)
    lock.success()
    lock.fail(now=2010)
    assert lock.locked(now=2010) == 0


# ----------------------------------------------------------------------
# what passes the gateway
# ----------------------------------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("GET", "/ui/"), ("GET", "/ui/app.js"), ("GET", "/health"), ("GET", "/workers"), ("GET", "/tasks"),
    ("GET", "/tasks/14f8677f-f494-4e27-b810-94c4d2e68669"), ("GET", "/activity"), ("GET", "/approvals"),
    ("GET", "/usage/today"), ("GET", "/metrics"),
    ("GET", "/goals"), ("GET", "/goals/14f8677f-f494-4e27-b810-94c4d2e68669"), ("POST", "/goals"),
    ("POST", "/goals/14f8677f-f494-4e27-b810-94c4d2e68669/continue"),
    # K-4 actions
    ("POST", "/task"), ("POST", "/approvals/ec13f3e3-137a-4ace-b03b-f081d6df0a85/approve"),
    ("POST", "/approvals/ec13f3e3-137a-4ace-b03b-f081d6df0a85/reject"),
    ("POST", "/tasks/03ff2618-01b3-4576-a4f2-19ce124c9fef/cancel"),
    # J-6
    ("POST", "/tasks/03ff2618-01b3-4576-a4f2-19ce124c9fef/review"),
    # K-5
    ("GET", "/workers/jarvis/chat"), ("POST", "/workers/gordon/chat"),
    # J-3
    ("GET", "/jobs"), ("GET", "/jobs/14f8677f-f494-4e27-b810-94c4d2e68669"),
    ("POST", "/jobs/14f8677f-f494-4e27-b810-94c4d2e68669/package"),
    ("POST", "/jobs/14f8677f-f494-4e27-b810-94c4d2e68669/package/save"),
    ("POST", "/jobs/14f8677f-f494-4e27-b810-94c4d2e68669/status"),
    ("GET", "/jobs/14f8677f-f494-4e27-b810-94c4d2e68669/package/cv.docx"),
    ("GET", "/jobs/14f8677f-f494-4e27-b810-94c4d2e68669/package/letter.docx"),
    ("GET", "/career/photo"),
])
def test_page_endpoints_pass(method, path):
    assert auth.allowed(method, path)


@pytest.mark.parametrize("method,path", [
    ("POST", "/approvals/x/approve"), ("POST", "/tasks/x/cancel"), ("GET", "/memory/facts"),
    ("GET", "/tasks/03ff2618-01b3-4576-a4f2-19ce124c9fef/review"), ("POST", "/tasks/x/review"),
    ("POST", "/tasks/03ff2618-01b3-4576-a4f2-19ce124c9fef/review/x"),
    ("POST", "/memory/facts/1/resolve"), ("GET", "/task"), ("POST", "/tasks"), ("PUT", "/task"),
    ("POST", "/approvals/ec13f3e3-137a-4ace-b03b-f081d6df0a85"), ("POST", "/approvals/ec13f3e3-137a/approve/x"),
    ("GET", "/approvals/ec13f3e3-137a-4ace-b03b-f081d6df0a85/approve"), ("POST", "/workers/../chat"),
    ("POST", "/workers/Jarvis/chat"), ("DELETE", "/workers/jarvis/chat"), ("POST", "/workers/jarvis/chat/x"),
    ("GET", "/auto-approval"), ("POST", "/auto-approval"), ("DELETE", "/memory/facts/1"), ("GET", "/approvals/x"),
    ("GET", "/tasks/../memory"), ("GET", "/docs"), ("GET", "/openapi.json"), ("GET", "/ui"), ("GET", "/workers/x"),
    ("POST", "/jobs/import"), ("GET", "/jobs/import"), ("POST", "/jobs"), ("GET", "/jobs/x"), ("POST", "/notes/propose"),
    ("GET", "/jobs/14f8677f-f494-4e27-b810-94c4d2e68669/package/x.docx"),
    ("POST", "/career/photo"), ("GET", "/career/photo/x"), ("GET", "/career"),
    ("POST", "/metrics"), ("GET", "/metrics/x"),
    ("GET", "/goals/x"), ("POST", "/goals/14f8677f-f494-4e27-b810-94c4d2e68669"), ("POST", "/goals/x/continue"),
    ("GET", "/goals/14f8677f-f494-4e27-b810-94c4d2e68669/continue"),
    ("POST", "/jobs/14f8677f-f494-4e27-b810-94c4d2e68669/package/cv.docx"),
    ("POST", "/jobs/score"), ("POST", "/jobs/x/package"), ("POST", "/jobs/14f8677f-f494-4e27-b810-94c4d2e68669/package/x"),
    ("DELETE", "/jobs/14f8677f-f494-4e27-b810-94c4d2e68669/status"), ("GET", "/jobs/14f8677f-f494-4e27-b810-94c4d2e68669/package/save"),
])
def test_everything_else_is_not_found(method, path):
    assert not auth.allowed(method, path)


def test_only_icons_and_manifest_are_public():
    assert auth.is_public("GET", "/ui/manifest.json")
    assert auth.is_public("GET", "/ui/icons/icon-180.png")
    for path in ("/ui/", "/ui/app.js", "/ui/icons/../app.js", "/workers"):
        assert not auth.is_public("GET", path)
    assert not auth.is_public("POST", "/ui/manifest.json")


@pytest.mark.parametrize("target,expected", [
    ("/ui/", "/ui/"), ("/ui/index.html", "/ui/index.html"), (None, "/ui/"), ("https://evil.example/", "/ui/"),
    ("//evil.example/ui/", "/ui/"), ("/ui//evil", "/ui/"), ("/workers", "/ui/"), ("/ui/\\evil", "/ui/"),
])
def test_after_login_only_this_site(target, expected):
    assert auth.safe_next(target) == expected


def test_same_origin_check():
    assert auth.same_origin("https://host.tailnet.ts.net:8443", "host.tailnet.ts.net:8443")
    assert auth.same_origin("http://127.0.0.1:18010", "127.0.0.1:18010")
    assert not auth.same_origin("https://evil.example", "127.0.0.1:18010")
    assert not auth.same_origin(None, "127.0.0.1:18010")


# ----------------------------------------------------------------------
# scripts/set_ui_password.sh
# ----------------------------------------------------------------------

def run_script(tmp_path, answers):
    env = {**os.environ, "SAVAS_AI_ROOT": str(ROOT), "SAVAS_UI_AUTH_FILE": str(tmp_path / "ui-auth.env")}
    return subprocess.run(["bash", str(ROOT / "scripts" / "set_ui_password.sh")], input=answers, text=True,
                          capture_output=True, env=env, timeout=60)


def test_script_writes_a_hash_never_the_password(tmp_path):
    done = run_script(tmp_path, "savas\nuzun-test-şifresi-1\nuzun-test-şifresi-1\n")
    assert done.returncode == 0, done.stderr
    out = tmp_path / "ui-auth.env"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    text = out.read_text()
    assert "$" not in text  # what compose reads must reach the container unchanged
    assert "uzun-test-şifresi-1" not in text and "uzun-test-şifresi-1" not in done.stdout
    values = dict(line.split("=", 1) for line in text.strip().splitlines())
    config = auth.Config(values["UI_USER"], values["UI_PASSWORD_HASH"], values["UI_SESSION_KEY"])
    assert config.ready and config.check_login("savas", "uzun-test-şifresi-1")


@pytest.mark.parametrize("answers,word", [
    ("savas\nkisa\nkisa\n", "12"), ("savas\nuzun-test-şifresi-1\nbaşka-şifre-0000\n", "aynı"), ("a b\n", "kullanıcı"),
])
def test_script_refuses_bad_input(tmp_path, answers, word):
    done = run_script(tmp_path, answers)
    assert done.returncode != 0 and word in done.stderr
    assert not (tmp_path / "ui-auth.env").exists()


def test_script_asks_before_replacing(tmp_path):
    (tmp_path / "ui-auth.env").write_text("eski\n")
    done = run_script(tmp_path, "h\n")
    assert done.returncode == 1 and (tmp_path / "ui-auth.env").read_text() == "eski\n"


@pytest.mark.parametrize("headers,ok", [
    ({"x-savas-csrf": "1", "origin": "https://savas.example:10000", "host": "savas.example:10000"}, True),
    ({"origin": "https://savas.example:10000", "host": "savas.example:10000"}, False),
    ({"x-savas-csrf": "0", "origin": "https://savas.example:10000", "host": "savas.example:10000"}, False),
    ({"x-savas-csrf": "1", "origin": "https://evil.example", "host": "savas.example:10000"}, False),
    ({"x-savas-csrf": "1", "host": "savas.example:10000"}, False),
    ({"x-savas-csrf": "1", "origin": "null", "host": "savas.example:10000"}, False),
])
def test_actions_need_the_page_header_and_this_origin(headers, ok):
    assert auth.csrf_ok(headers) is ok
