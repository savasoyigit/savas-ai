"""J-2: host automations (docs/plan-j.md karar K, L). No network: the API is stubbed."""

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import daily_summary  # noqa: E402
import lib  # noqa: E402
import weekly_report  # noqa: E402

USAGE = {
    "date": "2026-11-16",
    "system": {"used": 28530, "budget": 50000, "remaining": 21470, "local_tokens": 2589},
    "agents": {"ares": {"calls": 8, "cloud_tokens": 18206, "local_tokens": 2589, "budget": 20000},
               "jarvis": {"calls": 4, "cloud_tokens": 6911, "local_tokens": 0, "budget": 15000}},
}
TASKS = [
    {"task_id": "a", "status": "completed", "created_at": "2026-11-16T09:25:30+00:00", "message": "iş"},
    {"task_id": "b", "status": "failed", "created_at": "2026-11-16T20:59:00+00:00", "message": "gece", "error": "x"},  # 23:59 local
    {"task_id": "c", "status": "failed", "created_at": "2026-11-16T21:30:00+00:00", "message": "ertesi gün"},  # 00:30 local, 17th
    {"task_id": "d", "status": "completed", "created_at": "2026-11-10T08:00:00+00:00", "message": "eski"},
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(lib, "STATE_DIR", tmp_path / "state")
    sent = []
    monkeypatch.setattr(lib, "notify", lambda *a, **k: sent.append(a) or {"sent": 1})
    answers = {"/usage/today": USAGE, "/tasks?limit=200": {"tasks": TASKS}, "/tasks?limit=500": {"tasks": TASKS},
               "/approvals?status=pending": {"count": 1, "approvals": [{}]}}
    monkeypatch.setattr(lib, "api", lambda path: answers[path])
    monkeypatch.setattr(lib, "propose_note", lambda *a: sent.append(("propose", *a)) or {"status": "waiting_approval"})
    return sent


def test_local_dates_follow_istanbul_time():
    assert lib.local_date("2026-11-16T20:59:00+00:00") == "2026-11-16"
    assert lib.local_date("2026-11-16T21:30:00+00:00") == "2026-11-17"
    assert lib.local_date(None) is None


def test_summary_counts_today_in_local_time():
    s = daily_summary.summary(USAGE, TASKS, 1, "2026-11-16")
    assert s["tasks"] == 2 and s["by_status"] == {"completed": 1, "failed": 1}
    assert s["cloud_tokens"] == 28530 and s["agents"]["jarvis"]["calls"] == 4
    assert daily_summary.message(s) == "2 görev (1 başarısız) · 28.530/50.000 bulut token · 2.589 yerel · 1 onay bekliyor"


def test_daily_summary_pushes_once_a_day_and_keeps_a_snapshot(env):
    daily_summary.run(snapshot_only=False)
    daily_summary.run(snapshot_only=False)
    assert len(env) == 1 and env[0][0] == "SAVAS AI · Günün özeti" and env[0][2] == "/ui/#tasks"
    assert lib.State("daily/2026-11-16").load()["tasks"] == 2


def test_snapshot_only_never_pushes(env):
    daily_summary.run(snapshot_only=True)
    assert env == [] and lib.State("daily/2026-11-16").load()["cloud_tokens"] == 28530


def test_weekly_report(env, monkeypatch):
    days = weekly_report.week_days(date(2026, 11, 16))
    assert days[0] == "2026-11-10" and days[-1] == "2026-11-16" and len(days) == 7
    snap = daily_summary.summary(USAGE, TASKS, 0, "2026-11-16")
    text, totals = weekly_report.report(days, {"2026-11-16": snap}, TASKS)
    assert totals == {"tasks": 3, "failed": 1, "cloud_tokens": 28530, "local_tokens": 2589, "measured_days": 1}
    assert "| 2026-11-15 | 0 | 0 | – | – |" in text and "| ares | 8 | 18206 | 2589 |" in text
    assert "gece — x" in text and "ertesi gün" not in text


def test_weekly_report_writes_the_file_and_pushes(env, monkeypatch):
    lib.State("daily/2026-11-16").save(daily_summary.summary(USAGE, TASKS, 0, "2026-11-16"))
    monkeypatch.setattr(lib, "now_local", lambda: lib.datetime(2026, 11, 16, 21, 40, tzinfo=lib.LOCAL_TZ))
    weekly_report.run()
    text = (lib.STATE_DIR / "reports" / "2026-W47.md").read_text()
    assert text.startswith("# SAVAS AI haftalık rapor")
    assert env[0] == ("propose", "SAVAS-AI/Raporlar/Haftalık 2026-W47.md", text, "Haftalık rapor 2026-W47")
    assert env[1][0] == "SAVAS AI · Haftalık rapor" and env[1][1].startswith("3 görev (1 başarısız)")
    assert env[1][1].endswith("not onay bekliyor") and env[1][2] == "/ui/#approvals"
    weekly_report.run()  # a second run the same week proposes nothing new
    assert [e[0] for e in env] == ["propose", "SAVAS AI · Haftalık rapor", "SAVAS AI · Haftalık rapor"]
    assert env[2][2] == "/ui/"


def test_notify_key_comes_from_the_file(tmp_path, monkeypatch):
    monkeypatch.delenv("SAVAS_NOTIFY_KEY", raising=False)
    monkeypatch.setattr(lib, "KEY_FILE", tmp_path / "automation.env")
    with pytest.raises(lib.AutomationError, match="set_automation_key"):
        lib.notify_key()
    (tmp_path / "automation.env").write_text("OTHER=1\nSAVAS_NOTIFY_KEY=abc-123\n")
    assert lib.notify_key() == "abc-123"


def test_a_failed_call_is_an_automation_error():
    with pytest.raises(lib.AutomationError, match="URLError|HTTP"):
        lib.request("GET", "http://127.0.0.1:9/none")


def test_a_second_copy_exits_quietly(tmp_path, monkeypatch):
    monkeypatch.setattr(lib, "STATE_DIR", tmp_path)
    with lib.single_run("x"):
        with pytest.raises(SystemExit) as caught:
            with lib.single_run("x"):
                pass
    assert caught.value.code == 0


def test_key_script_writes_a_compose_safe_key_and_never_prints_it(tmp_path):
    out = tmp_path / "automation.env"
    result = subprocess.run(["bash", str(HERE.parent / "scripts" / "set_automation_key.sh")], capture_output=True,
                            text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "SAVAS_AUTOMATION_KEY_FILE": str(out)})
    assert result.returncode == 0
    key = out.read_text().strip().split("=", 1)[1]
    assert len(key) >= 60 and "$" not in key and ":" not in key and key not in result.stdout + result.stderr
    assert oct(out.stat().st_mode & 0o777) == "0o600"


def test_units_point_at_the_scripts():
    units = HERE / "systemd"
    for name in ("daily-summary", "daily-snapshot", "weekly-report"):
        service = (units / f"savas-{name}.service").read_text()
        script = service.split("%h/AI/")[1].split()[0]
        assert (HERE.parent / script).exists()
        assert "OnCalendar=" in (units / f"savas-{name}.timer").read_text()
