"""K-3f: Web Push pieces of the CEO gateway (docs/plan-k.md Ek B)."""

import importlib.util
import json
import os
import stat
import struct
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Loaded by path under its own name (see test_auth.py).
_spec = importlib.util.spec_from_file_location("gateway_push", Path(__file__).resolve().parents[1] / "app" / "push.py")
push = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(push)


def private_key(b64):
    return ec.derive_private_key(int.from_bytes(push.unb64(b64), "big"), ec.SECP256R1())


# RFC 8291 Appendix A.
RFC = {
    "plaintext": b"When I grow up, I want to be a watermelon",
    "as_private": "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw",
    "ua_private": "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94",
    "ua_public": "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
    "salt": "DGv6ra1nlYgDCS1FRnbzlw",
    "auth": "BTBZMqHH6r4Tts7J_aSIgg",
    "body": ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_"
             "yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"),
}


def decrypt(body: bytes, ua_private_b64: str, auth_b64: str) -> bytes:
    """The user agent's side, written from the RFC: an independent check of encrypt()."""
    salt, rs, idlen = body[:16], struct.unpack(">I", body[16:20])[0], body[20]
    as_public = body[21:21 + idlen]
    ua = private_key(ua_private_b64)
    ua_public = push.public_bytes(ua)
    shared = ua.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
    ikm = push._hmac(push._hmac(push.unb64(auth_b64), shared), b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    prk = push._hmac(salt, ikm)
    cek = push._hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = push._hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    record = AESGCM(cek).decrypt(nonce, body[21 + idlen:], None)
    assert rs == 4096 and record.endswith(b"\x02")
    return record[:-1]


def test_rfc8291_appendix_a_vector():
    body = push.encrypt(RFC["plaintext"], RFC["ua_public"], RFC["auth"],
                        salt=push.unb64(RFC["salt"]), server_key=private_key(RFC["as_private"]))
    assert push.b64(body) == RFC["body"]


def test_a_fresh_message_decrypts_on_the_device_side():
    ua = ec.generate_private_key(ec.SECP256R1())
    ua_private = push.b64(ua.private_numbers().private_value.to_bytes(32, "big"))
    auth = push.b64(os.urandom(16))
    payload = json.dumps({"title": "Onay bekliyor", "body": "obsidian/create_note · 12:00"}).encode()
    body = push.encrypt(payload, push.b64(push.public_bytes(ua)), auth)
    assert decrypt(body, ua_private, auth) == payload
    assert body != push.encrypt(payload, push.b64(push.public_bytes(ua)), auth)  # new salt and key each time


@pytest.mark.parametrize("p256dh,auth", [("AAAA", RFC["auth"]), (RFC["ua_public"], "AAAA"), ("B" * 87, RFC["auth"])])
def test_bad_device_keys_are_refused(p256dh, auth):
    with pytest.raises(push.PushError):
        push.encrypt(b"x", p256dh, auth)


def test_large_payload_is_refused():
    with pytest.raises(push.PushError, match="too large"):
        push.encrypt(b"x" * 5000, RFC["ua_public"], RFC["auth"])


# ----------------------------------------------------------------------
# VAPID
# ----------------------------------------------------------------------

def test_vapid_key_round_trip_and_no_dollar():
    raw = push.new_vapid_key()
    assert "$" not in raw and len(push.unb64(raw)) == 32
    key = push.load_vapid_key(raw)
    assert len(push.unb64(push.vapid_public(key))) == 65


@pytest.mark.parametrize("bad", [None, "", "abc", push.b64(b"x" * 31)])
def test_bad_vapid_keys(bad):
    with pytest.raises(push.PushError):
        push.load_vapid_key(bad)


def test_vapid_token_is_a_valid_es256_jwt_for_the_push_origin():
    key = push.load_vapid_key(push.new_vapid_key())
    header = push.vapid_header("https://web.push.apple.com/QGuQyavXutnMH?x=1", key, "mailto:a@example.com", now=1000)
    token = header.split("t=")[1].split(",")[0]
    k = header.split("k=")[1]
    head, claims, sig = token.split(".")
    assert json.loads(push.unb64(head)) == {"typ": "JWT", "alg": "ES256"}
    assert json.loads(push.unb64(claims)) == {"aud": "https://web.push.apple.com", "exp": 1000 + 3600,
                                              "sub": "mailto:a@example.com"}
    raw = push.unb64(sig)
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), push.unb64(k))
    public.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                  f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))


def test_send_headers():
    key = push.load_vapid_key(push.new_vapid_key())
    h = push.headers("https://fcm.googleapis.com/fcm/send/x", key, "mailto:a@example.com")
    assert h["Content-Encoding"] == "aes128gcm" and h["TTL"] == "43200" and h["Urgency"] == "high"
    assert h["Authorization"].startswith("vapid t=")


# ----------------------------------------------------------------------
# subscriptions
# ----------------------------------------------------------------------

def sub(endpoint="https://web.push.apple.com/abc"):
    return {"endpoint": endpoint, "keys": {"p256dh": RFC["ua_public"], "auth": RFC["auth"]}}


@pytest.mark.parametrize("endpoint", [
    "https://web.push.apple.com/abc", "https://api.push.apple.com/x", "https://fcm.googleapis.com/fcm/send/x",
    "https://updates.push.services.mozilla.com/wpush/v2/x",
])
def test_known_push_services_are_accepted(endpoint):
    assert push.check_subscription(sub(endpoint))["endpoint"] == endpoint


@pytest.mark.parametrize("endpoint", [
    "http://web.push.apple.com/abc", "https://web.push.apple.com:8443/abc", "https://evil.example/abc",
    "https://web.push.apple.com.evil.example/x", "https://u:p@web.push.apple.com/x", "https://127.0.0.1/x",
    "https://ai-orchestrator:8000/approvals", "https://push.apple.com.evil/x", None,
])
def test_other_endpoints_are_refused(endpoint):
    with pytest.raises(push.PushError):
        push.check_subscription(sub(endpoint))


def test_malformed_subscriptions_are_refused():
    for bad in (None, [], {"endpoint": "https://web.push.apple.com/a"},
                {"endpoint": "https://web.push.apple.com/a", "keys": {"p256dh": "x", "auth": "y"}}):
        with pytest.raises(push.PushError):
            push.check_subscription(bad)


def test_store_add_replace_remove_and_limit(tmp_path):
    store = push.Store(tmp_path / "push.json")
    assert store.load() == {"subscriptions": [], "seen": [], "initialized": False} and store.writable()
    store.add(push.check_subscription(sub()))
    store.add(push.check_subscription(sub()))  # same endpoint: replaced, not doubled
    assert len(store.load()["subscriptions"]) == 1
    assert stat.S_IMODE((tmp_path / "push.json").stat().st_mode) == 0o600
    for i in range(12):
        store.add(push.check_subscription(sub(f"https://web.push.apple.com/{i}")))
    assert len(store.load()["subscriptions"]) == push.MAX_SUBSCRIPTIONS
    assert store.remove("https://web.push.apple.com/11") and not store.remove("https://web.push.apple.com/11")


def test_store_survives_a_broken_file(tmp_path):
    (tmp_path / "push.json").write_text("{bozuk")
    assert push.Store(tmp_path / "push.json").load()["subscriptions"] == []
    assert not push.Store(tmp_path / "yok" / "push.json").writable()


# ----------------------------------------------------------------------
# which approvals to announce
# ----------------------------------------------------------------------

def approvals(*ids):
    return [{"approval_id": i, "agent": "obsidian", "action": "create_note", "arguments": {"content": "gizli"}} for i in ids]


def test_first_look_learns_without_announcing():
    fresh, state = push.new_approvals(approvals("a", "b"), {"seen": [], "initialized": False})
    assert fresh == [] and state == {"seen": ["a", "b"], "initialized": True}


def test_only_new_approvals_are_announced_once():
    state = {"seen": ["a"], "initialized": True}
    fresh, state = push.new_approvals(approvals("a", "b"), state)
    assert [a["approval_id"] for a in fresh] == ["b"]
    fresh, state = push.new_approvals(approvals("a", "b"), state)
    assert fresh == []
    fresh, state = push.new_approvals([], state)  # answered: nothing to say, still remembered
    assert fresh == [] and "b" in state["seen"]


def test_seen_list_is_bounded():
    state = {"seen": [str(i) for i in range(push.MAX_SEEN)], "initialized": True}
    _, state = push.new_approvals(approvals("yeni"), state)
    assert len(state["seen"]) == push.MAX_SEEN and state["seen"][-1] == "yeni"


def test_notification_carries_no_arguments():
    note = push.notification(approvals("a")[0], "12:34")
    assert note == {"title": "Onay bekliyor", "body": "obsidian/create_note · 12:34", "url": "/ui/#approvals",
                    "tag": "approval-a"}
    assert "gizli" not in json.dumps(note)


# ----------------------------------------------------------------------
# J-2: alerts from the activity log, messages from automations
# ----------------------------------------------------------------------

def ev(ts, event, status, **more):
    return {"timestamp": f"2026-11-16T10:00:{ts:02d}+00:00", "event": event, "status": status, **more}


def test_which_events_alert():
    failed = push.alert(ev(1, "task", "failed", agent="orchestrator", task_id="t1", error="Daily budget exceeded"))
    assert failed[1]["title"] == "Görev başarısız" and failed[1]["url"] == "/ui/#task=t1"
    assert push.alert(ev(1, "task", "background_error", task_id="t2", error="x"))[0] == "task-t2"
    assert push.alert(ev(1, "budget_check", "blocked", worker="jarvis", error="limit"))[1]["body"].startswith("jarvis:")
    assert push.alert(ev(1, "chat", "blocked", worker="gordon", error="limit"))[0] == "budget-gordon"
    assert push.alert(ev(1, "model_alert", "warning", model="gh/gpt-4.1", kind="access", http_status=403))[1]["body"] == "gh/gpt-4.1 · access · HTTP 403"
    assert push.alert(ev(1, "model_circuit", "open", model="m", seconds=60))[1]["title"] == "Model geçici devre dışı"
    goal = push.alert(ev(1, "goal", "waiting", goal_id="g1", used=6120))
    assert goal[0] == "goal-g1-waiting" and goal[1]["url"] == "/ui/#goals" and goal[1]["body"] == "6120 token"
    assert push.alert(ev(1, "goal", "stopped", goal_id="g1", error="bütçe"))[1]["body"] == "bütçe"
    for quiet in (ev(1, "goal", "started"), ev(1, "goal", "continued"), ev(1, "task", "completed"), ev(1, "task", "cancelled"), ev(1, "model_circuit", "closed", model="m"),
                  ev(1, "budget_check", "allowed"), ev(1, "chat", "failed")):
        assert push.alert(quiet) is None


def test_alert_tags_are_safe_for_the_notify_rule():
    _, message = push.alert(ev(1, "model_alert", "warning", model="gh/gpt-4.1 / ÇOK uzun model adı", kind="x"))
    assert push.NOTIFY_TAG.fullmatch(message["tag"])


def test_first_look_learns_the_log_without_alerting():
    events = [ev(1, "task", "failed", task_id="old", error="x")]
    alerts, state = push.new_alerts(events, {"seen": []}, now=1000)
    assert alerts == [] and state["activity_after"] == events[0]["timestamp"] and state["seen"] == []


def test_only_new_events_alert_once_with_a_cooldown():
    state = {"activity_after": ev(1, "x", "x")["timestamp"], "alerted": {}}
    events = [ev(1, "task", "failed", task_id="old"), ev(2, "task", "failed", task_id="t2"),
              ev(3, "budget_check", "blocked", worker="jarvis"), ev(4, "budget_check", "blocked", worker="jarvis")]
    alerts, state = push.new_alerts(events, state, now=1000)
    assert [a["title"] for a in alerts] == ["Görev başarısız", "Bütçe sınırı"]
    alerts, state = push.new_alerts(events + [ev(5, "budget_check", "blocked", worker="jarvis")], state, now=1100)
    assert alerts == []  # same key within the cooldown
    alerts, state = push.new_alerts([ev(6, "budget_check", "blocked", worker="jarvis")], state, now=1000 + push.ALERT_COOLDOWN)
    assert len(alerts) == 1


def test_an_orchestrator_restart_does_not_replay_old_events():
    state = {"activity_after": ev(30, "x", "x")["timestamp"], "alerted": {}}
    alerts, state = push.new_alerts([ev(10, "task", "failed", task_id="t")], state, now=1)
    assert alerts == [] and state["activity_after"].endswith(":30+00:00")


def test_store_keeps_the_alert_state(tmp_path):
    store = push.Store(tmp_path / "push.json")
    store.save({"subscriptions": [], "seen": ["a"], "initialized": True, "activity_after": "t", "alerted": {"k": 1}})
    assert store.load()["activity_after"] == "t" and store.load()["alerted"] == {"k": 1}
    store.add(push.check_subscription(sub()))
    assert store.load()["activity_after"] == "t"  # adding a phone keeps it


def test_notify_messages():
    assert push.check_notify({"title": "Günün özeti", "body": "8 görev"}) == {
        "title": "Günün özeti", "body": "8 görev", "url": "/ui/", "tag": "automation"}
    assert push.check_notify({"title": "x", "url": "/ui/#task=1", "tag": "daily-summary"})["url"] == "/ui/#task=1"


@pytest.mark.parametrize("bad", [
    None, [], {}, {"title": ""}, {"title": "x" * 61}, {"title": "x", "body": "y" * 201},
    {"title": "x", "url": "https://evil.example"}, {"title": "x", "url": "//evil.example"},
    {"title": "x", "url": "/ui//evil"}, {"title": "x", "url": "/login"}, {"title": "x", "tag": "Büyük Harf"},
    {"title": "x", "url": "/ui/\\evil"},
])
def test_bad_notify_messages(bad):
    with pytest.raises(push.PushError):
        push.check_notify(bad)
