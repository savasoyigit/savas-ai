"""Runner jobs (plan-i karar B), on a temporary tree; no socket, no docker."""

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import savas_runner as runner  # noqa: E402

TASK = "3f2a9c1e-0000-4000-8000-000000000001"


@pytest.fixture
def tree(tmp_path, monkeypatch):
    repo = tmp_path / "AI"
    (repo / "orchestrator" / "app").mkdir(parents=True)
    (repo / "orchestrator" / "app" / "main.py").write_text("print('x')\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "a.md").write_text("not\n")
    (repo / "orchestrator" / "tests" / "data").mkdir(parents=True)
    (repo / "orchestrator" / "tests" / "data" / "set.json").write_text("{}\n")
    for folder in ("secrets", "data", ".venv", "backups", "local-llm/data", ".claude", "sandbox", "__pycache__"):
        (repo / folder).mkdir(parents=True, exist_ok=True)
        (repo / folder / "x.txt").write_text("gizli\n")
    (repo / "omniroute").mkdir()
    (repo / "omniroute" / ".env").write_text("KEY=1\n")
    (repo / "x.db").write_bytes(b"db")
    (repo / "big.txt").write_bytes(b"a" * (runner.MAX_FILE_BYTES + 1))
    (repo / "link.md").symlink_to(repo / "secrets" / "x.txt")
    (repo / "linkdir").symlink_to(repo / "secrets")

    repos = tmp_path / "repos.json"
    repos.write_text(json.dumps({"repos": {"savas-ai": str(repo)}}))
    monkeypatch.setattr(runner, "REPOS_FILE", repos)
    monkeypatch.setattr(runner, "SANDBOX_ROOT", tmp_path / "sandbox-root")
    return repo


def copied(tmp_path):
    root = tmp_path / "sandbox-root" / TASK / runner.REPO_COPY
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def test_prepare_copies_code_and_leaves_secrets_out(tree, tmp_path):
    result = runner.prepare(TASK, "savas-ai")
    assert result["status"] == "ok" and result["reused"] is False and result["files"] == 3
    assert copied(tmp_path) == ["docs/a.md", "orchestrator/app/main.py", "orchestrator/tests/data/set.json"]
    assert result["skipped"] >= 4
    assert json.loads((tmp_path / "sandbox-root" / TASK / "repo.json").read_text()) == {"repo": "savas-ai"}


def test_prepare_again_reuses_the_sandbox(tree):
    runner.prepare(TASK, "savas-ai")
    assert runner.prepare(TASK, "savas-ai")["reused"] is True


@pytest.mark.parametrize("task", ["../x", "ABC-def-123", "short", "a" * 65, None, 5, "a/b-cdefgh"])
def test_bad_task_ids_are_refused(tree, task):
    with pytest.raises(runner.JobError, match="task must match"):
        runner.prepare(task, "savas-ai")


def test_unknown_repo_is_refused(tree):
    with pytest.raises(runner.JobError, match="unknown repo"):
        runner.prepare(TASK, "/home/savas")


# Built at run time so this file itself never matches the scan.
@pytest.mark.parametrize("text", [
    "TAVILY_API_KEY=" + "tvly" + "-abcdefghijklmnop1234",
    "tok" + "en = '" + "abcdefghijklmnopqrstuvwxyz123456'",
    "-----BEGIN OPENSSH " + "PRIVATE KEY-----",
    "gh" + "p_abcdefghijklmnopqrstuvwxyz0123",
])
def test_a_copy_with_a_secret_is_not_handed_out(tree, tmp_path, text):
    (tree / "docs" / "leak.md").write_text(text + "\n")
    with pytest.raises(runner.JobError, match="secret pattern found.*docs/leak.md") as refused:
        runner.prepare(TASK, "savas-ai")
    assert text not in str(refused.value)
    assert list((tmp_path / "sandbox-root").iterdir()) == []


def test_ordinary_code_is_not_a_secret(tree):
    (tree / "docs" / "ok.py").write_text('KEY = os.getenv("TAVILY_API_KEY", "")\nheaders = "Bearer tvly-test"\n')
    assert runner.prepare(TASK, "savas-ai")["status"] == "ok"


def test_copy_size_is_limited(tree, monkeypatch):
    monkeypatch.setattr(runner, "MAX_COPY_BYTES", 5)
    with pytest.raises(runner.JobError, match="over"):
        runner.prepare(TASK, "savas-ai")


def test_cleanup_removes_only_the_sandbox(tree, tmp_path):
    runner.prepare(TASK, "savas-ai")
    assert runner.cleanup(TASK) == {"status": "ok", "task": TASK, "removed": True}
    assert runner.cleanup(TASK)["removed"] is False
    assert (tree / "orchestrator" / "app" / "main.py").exists()


def test_old_sandboxes_are_removed(tree, tmp_path):
    runner.prepare(TASK, "savas-ai")
    old = tmp_path / "sandbox-root" / TASK
    past = time.time() - (runner.SANDBOX_DAYS + 1) * 86400
    os.utime(old, (past, past))
    assert runner.remove_old_sandboxes() == 1 and not old.exists()


def test_paths_outside_the_root_are_never_removed(tree, tmp_path):
    with pytest.raises(runner.JobError):
        runner.remove_tree(tree)
    assert tree.exists()


def request(socket_path, method, path, body=None):
    import http.client
    import socket

    class Connection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.connect(str(socket_path))

    connection = Connection("localhost")
    data = json.dumps(body).encode() if body is not None else None
    connection.request(method, path, body=data, headers={"Content-Type": "application/json"})
    response = connection.getresponse()
    return response.status, json.loads(response.read())


def test_http_over_the_unix_socket(tree, tmp_path):
    import threading

    socket_path = tmp_path / "s" / "run.sock"
    socket_path.parent.mkdir()
    server = runner.Server(str(socket_path), runner.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert request(socket_path, "GET", "/health") == (200, {"status": "ok", "jobs": ["/cleanup", "/diff", "/prepare", "/test"]})
        status, body = request(socket_path, "POST", "/prepare", {"task": TASK, "repo": "savas-ai"})
        assert status == 200 and body["files"] == 3 and "ms" in body
        assert request(socket_path, "POST", "/prepare", {"task": "../etc", "repo": "savas-ai"})[0] == 400
        assert request(socket_path, "POST", "/run", {"cmd": "rm"})[0] == 404
        assert request(socket_path, "POST", "/prepare", ["x"])[0] == 400
    finally:
        server.shutdown()
        server.server_close()


# ----------------------------------------------------------------------
# I-3: test runs (plan-i karar C)
# ----------------------------------------------------------------------

class Done:
    def __init__(self, code, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


def test_test_command_is_fixed_and_locked_down(tree, tmp_path):
    command = runner.test_command(TASK, 2, "pytest", tmp_path / "copy")
    joined = " ".join(command)
    for flag in ("--rm", "--network none", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges",
                 "--user 1000:1000", "--memory 1g", "--cpus 1", "--pids-limit 256", "--tmpfs /tmp:size=256m"):
        assert flag in joined
    assert f"{tmp_path / 'copy'}:/work:ro" in command
    assert command[command.index(runner.TEST_IMAGE) + 1:] == runner.TEST_PROFILES["pytest"]
    assert command[4] == f"savas-test-{TASK[:40]}-2"


def test_run_records_the_log_and_returns_the_tail(tree, tmp_path, monkeypatch):
    runner.prepare(TASK, "savas-ai")
    monkeypatch.setattr(runner, "TEST_OUTPUT_CHARS", 10)
    calls = []

    def fake(command, **kwargs):
        calls.append((command, kwargs))
        return Done(1, "x" * 50, "FAILED test_a")

    result = runner.run_tests(TASK, "pytest", fake)
    assert result["run"] == 1 and result["exit_code"] == 1 and result["passed"] is False
    assert result["output"] == "ILED test_a"[-10:]
    assert calls[0][1]["timeout"] == runner.TEST_TIMEOUT_SECONDS
    log = tmp_path / "sandbox-root" / TASK / "runs" / "1.log"
    assert log.read_text() == "x" * 50 + "FAILED test_a"
    assert runner.run_tests(TASK, "pytest", lambda c, **k: Done(0, "ok"))["passed"] is True


def test_timeout_kills_the_container(tree, monkeypatch):
    runner.prepare(TASK, "savas-ai")
    calls = []

    def fake(command, **kwargs):
        calls.append(command)
        if command[1] == "run":
            raise runner.subprocess.TimeoutExpired(command, 300)
        return Done(0)

    result = runner.run_tests(TASK, "pytest", fake)
    assert result["timed_out"] is True and result["passed"] is False and "timed out" in result["output"]
    assert calls[1] == [runner.DOCKER, "kill", f"savas-test-{TASK[:40]}-1"]


def test_runs_are_limited_and_profiles_fixed(tree):
    runner.prepare(TASK, "savas-ai")
    for _ in range(runner.MAX_TEST_RUNS):
        runner.run_tests(TASK, "pytest", lambda c, **k: Done(0))
    with pytest.raises(runner.JobError, match="at most"):
        runner.run_tests(TASK, "pytest", lambda c, **k: Done(0))
    with pytest.raises(runner.JobError, match="unknown profile"):
        runner.run_tests(TASK, "rm -rf /", lambda c, **k: Done(0))


def test_no_sandbox_no_test(tree):
    with pytest.raises(runner.JobError, match="prepare it first"):
        runner.run_tests(TASK, "pytest", lambda c, **k: Done(0))


# ----------------------------------------------------------------------
# I-4: diff and the user's apply script (plan-i karar E)
# ----------------------------------------------------------------------

import subprocess  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "apply_patch.sh"


def edit_copy(tmp_path, relative, text):
    path = tmp_path / "sandbox-root" / TASK / runner.REPO_COPY / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_diff_has_changed_and_new_files_only(tree, tmp_path):
    runner.prepare(TASK, "savas-ai")
    edit_copy(tmp_path, "orchestrator/app/main.py", "print('y')\n")
    edit_copy(tmp_path, "pkg/new.py", "x = 1")
    result = runner.make_diff(TASK)
    assert result["files"] == ["orchestrator/app/main.py", "pkg/new.py"]
    assert result["added"] == 2 and result["removed"] == 1 and result["conflicts"] == []
    assert "--- a/orchestrator/app/main.py\n+++ b/orchestrator/app/main.py" in result["diff"]
    assert "--- /dev/null\n+++ b/pkg/new.py" in result["diff"]
    assert "\\ No newline at end of file" in result["diff"]
    assert Path(result["patch"]).read_text() == result["diff"]


def test_live_change_after_the_copy_is_a_conflict(tree, tmp_path):
    runner.prepare(TASK, "savas-ai")
    edit_copy(tmp_path, "orchestrator/app/main.py", "print('y')\n")
    (tree / "orchestrator" / "app" / "main.py").write_text("print('canlı değişti')\n")
    result = runner.make_diff(TASK)
    assert result["files"] == [] and result["conflicts"] == ["orchestrator/app/main.py"]


def test_unchanged_copy_gives_an_empty_patch(tree):
    runner.prepare(TASK, "savas-ai")
    assert runner.make_diff(TASK)["files"] == [] and runner.make_diff(TASK)["diff"] == ""


def test_sandbox_without_manifest_is_refused(tree, tmp_path):
    runner.prepare(TASK, "savas-ai")
    (tmp_path / "sandbox-root" / TASK / runner.MANIFEST).unlink()
    with pytest.raises(runner.JobError, match="prepare it again"):
        runner.make_diff(TASK)


def apply(tmp_path, *args, answer=None):
    env = {**os.environ, "SAVAS_SANDBOX": str(tmp_path / "sandbox-root"),
           "SAVAS_REPOS": str(tmp_path / "repos.json"), "SAVAS_BACKUPS": str(tmp_path / "backups")}
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, input=answer)


def test_apply_script_backs_up_and_patches_the_repo(tree, tmp_path):
    runner.prepare(TASK, "savas-ai")
    edit_copy(tmp_path, "orchestrator/app/main.py", "print('y')\n")
    edit_copy(tmp_path, "pkg/new.py", "x = 1\n")
    runner.make_diff(TASK)

    done = apply(tmp_path, TASK, "--yes")
    assert done.returncode == 0, done.stderr
    assert (tree / "orchestrator" / "app" / "main.py").read_text() == "print('y')\n"
    assert (tree / "pkg" / "new.py").read_text() == "x = 1\n"
    assert len(list((tmp_path / "backups").glob("savas-ai-*-pre-patch-*.tgz"))) == 1
    assert not list(tree.rglob("*.orig")) and not list(tree.rglob("*.rej"))
    assert (tmp_path / "sandbox-root" / TASK / "applied").exists()


def test_apply_script_asks_and_no_means_nothing_changes(tree, tmp_path):
    runner.prepare(TASK, "savas-ai")
    edit_copy(tmp_path, "orchestrator/app/main.py", "print('y')\n")
    runner.make_diff(TASK)
    done = apply(tmp_path, TASK, answer="h\n")
    assert done.returncode == 1 and "vazgeçildi" in done.stdout
    assert (tree / "orchestrator" / "app" / "main.py").read_text() == "print('x')\n"
    assert not (tmp_path / "backups").exists()


def test_apply_script_refuses_a_stale_patch_and_bad_input(tree, tmp_path):
    runner.prepare(TASK, "savas-ai")
    edit_copy(tmp_path, "orchestrator/app/main.py", "print('y')\n")
    runner.make_diff(TASK)
    (tree / "orchestrator" / "app" / "main.py").write_text("print('başka')\n")
    stale = apply(tmp_path, TASK, "--yes")
    assert stale.returncode == 1 and "kuru deneme başarısız" in stale.stderr
    assert (tree / "orchestrator" / "app" / "main.py").read_text() == "print('başka')\n"
    assert apply(tmp_path, "../etc").returncode == 2


def test_apply_script_refuses_forbidden_paths(tree, tmp_path):
    runner.prepare(TASK, "savas-ai")
    (tmp_path / "sandbox-root" / TASK / runner.PATCH_FILE).write_text(
        "--- /dev/null\n+++ b/secrets/x\n@@ -0,0 +1 @@\n+k\n")
    done = apply(tmp_path, TASK, "--yes")
    assert done.returncode == 1 and "yasak yol" in done.stderr
    assert not (tree / "secrets" / "x").exists()


# ----------------------------------------------------------------------
# M-3: repo objects, excludes, overlay, profile per repo (plan-m karar B, D)
# ----------------------------------------------------------------------

@pytest.fixture
def webapp(tmp_path, monkeypatch, tree):
    app = tmp_path / "webapp"
    (app / "profile-images").mkdir(parents=True)
    (app / "profile-images" / "a.png").write_bytes(b"png")
    (app / "app.py").write_text("x = 1\n")
    (app / "app.py.old").write_text("old\n")
    (app / "index.html.backup-1").write_text("old\n")
    (app / "askimiz.db.before-x").write_text("db\n")
    home = tmp_path / "runner"
    (home / "overlays" / "web" / "tests").mkdir(parents=True)
    (home / "overlays" / "web" / "tests" / "test_smoke.py").write_text("def test_ok():\n    pass\n")
    monkeypatch.setattr(runner, "HERE", home)
    monkeypatch.setattr(runner, "OVERLAY_ROOT", home / "overlays")
    repos = json.loads(runner.REPOS_FILE.read_text())
    repos["repos"]["web"] = {"path": str(app), "test_profile": "webapp", "overlay": "overlays/web",
                             "exclude": ["profile-images", "*.old", "*backup*", "*.db.*"]}
    runner.REPOS_FILE.write_text(json.dumps(repos))
    return app


def test_repo_object_excludes_and_adds_the_overlay(webapp, tmp_path):
    result = runner.prepare(TASK, "web")
    assert result["files"] == 1
    assert copied(tmp_path) == ["app.py", "tests/test_smoke.py"]
    assert json.loads((tmp_path / "sandbox-root" / TASK / runner.OVERLAY_LIST).read_text()) == ["tests/test_smoke.py"]
    # The old string form still works.
    assert runner.load_repos()["savas-ai"]["test_profile"] == "pytest"


def test_overlay_is_restored_before_tests_and_never_in_the_patch(webapp, tmp_path):
    runner.prepare(TASK, "web")
    edit_copy(tmp_path, "tests/test_smoke.py", "# zayıflatıldı\n")
    edit_copy(tmp_path, "app.py", "x = 2\n")
    seen = []
    runner.run_tests(TASK, "webapp", lambda c, **k: seen.append(c) or Done(0))
    assert "savas-test-webapp:1" in seen[0] and seen[0][-1] == "tests"
    smoke = tmp_path / "sandbox-root" / TASK / runner.REPO_COPY / "tests" / "test_smoke.py"
    assert smoke.read_text().startswith("def test_ok")
    diff = runner.make_diff(TASK)
    assert diff["files"] == ["app.py"]


def test_profile_must_be_the_repos_own(webapp):
    runner.prepare(TASK, "web")
    with pytest.raises(runner.JobError, match="profile webapp"):
        runner.run_tests(TASK, "pytest", lambda c, **k: Done(0))


def test_overlay_outside_the_overlay_root_is_refused(webapp):
    repos = json.loads(runner.REPOS_FILE.read_text())
    repos["repos"]["web"]["overlay"] = "../../etc"
    runner.REPOS_FILE.write_text(json.dumps(repos))
    with pytest.raises(runner.JobError, match="overlay"):
        runner.prepare(TASK, "web")


def test_apply_script_reads_repo_objects(webapp, tmp_path):
    runner.prepare(TASK, "web")
    edit_copy(tmp_path, "app.py", "x = 3\n")
    runner.make_diff(TASK)
    env = {**os.environ, "SAVAS_SANDBOX": str(tmp_path / "sandbox-root"),
           "SAVAS_REPOS": str(runner.REPOS_FILE), "SAVAS_BACKUPS": str(tmp_path / "backups")}
    done = subprocess.run(["bash", str(SCRIPT), TASK, "--yes"], env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert (webapp / "app.py").read_text() == "x = 3\n"


def test_auto_profile_is_the_repos_own(webapp):
    runner.prepare(TASK, "web")
    seen = []
    runner.run_tests(TASK, "auto", lambda c, **k: seen.append(c) or Done(0))
    assert "savas-test-webapp:1" in seen[0] and seen[0][-1] == "tests"
