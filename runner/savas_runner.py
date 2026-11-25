#!/usr/bin/env python3
"""
SAVAS runner (roadmap Faz I, docs/plan-i.md).

A small host service, run by systemd as the user `savas`. The
orchestrator reaches it only through a unix socket; it has no
docker.sock and no mount of the live code (plan-i karar A).

Fixed jobs only (karar B):
    GET  /health
    POST /prepare  {"task": "<id>", "repo": "<name>"}  copy without secrets
    POST /test     {"task": "<id>", "profile": "<name>"}  tests in a
                   throwaway container without network (karar C, I-3)
    POST /diff     {"task": "<id>"}  unified diff of the copy against the
                   live repo, written to change.patch (I-4); applying it is
                   the user's job (scripts/apply_patch.sh, karar E)
    POST /cleanup  {"task": "<id>"}                    remove the sandbox

No shell is ever run. Inputs are checked against TASK_ID and the repo
allowlist (runner/repos.json, never mounted into a container). A copy
that contains a secret pattern is removed and the job fails; the reply
names files, never their content.

Stdlib only; tests: runner/tests (no network, no docker).
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import socketserver
import stat
import subprocess
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPOS_FILE = Path(os.environ.get("RUNNER_REPOS", HERE / "repos.json"))
SANDBOX_ROOT = Path(os.environ.get("RUNNER_SANDBOX", HERE.parent / "sandbox"))
SOCKET_PATH = Path(os.environ.get("RUNNER_SOCKET", HERE / "sock" / "run.sock"))

TASK_ID = re.compile(r"^[a-z0-9-]{8,64}$")
REPO_COPY = "repo"
MAX_BODY_BYTES = 4096
MAX_COPY_BYTES = 50 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
SANDBOX_DAYS = 7

# I-3 (plan-i karar C). The model picks a profile name, never a command.
DOCKER = shutil.which("docker") or "/usr/bin/docker"
TEST_IMAGE = "savas-test-python:1"
TEST_PROFILES = {
    "pytest": ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "orchestrator/tests"],
    # M-3 (plan-m karar D): web apps; the tests set their own fake config.
    "webapp": ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
}
PROFILE_IMAGES = {"pytest": TEST_IMAGE, "webapp": "savas-test-webapp:1"}
DEFAULT_PROFILE = "pytest"
# M-3: files the runner adds to a copy (smoke tests); never part of a patch.
OVERLAY_ROOT = HERE / "overlays"
OVERLAY_LIST = "overlay.json"
TEST_TIMEOUT_SECONDS = 300
MAX_TEST_RUNS = 5
TEST_OUTPUT_CHARS = 20000

# I-4: what was copied, by hash, so a diff never undoes a live change
# made after the copy (such a file is a conflict and left out).
MANIFEST = "manifest.json"
PATCH_FILE = "change.patch"
DIFF_OUTPUT_CHARS = 20000

# Never copied, at any depth (plan-i karar B; CLAUDE.md tool directories).
EXCLUDED_DIRS = {
    "secrets", ".venv", "venv", "__pycache__", ".pytest_cache", "backups",
    ".git", "sandbox", "sock", "node_modules",
    ".agents", ".claude", ".codex", ".opencode", ".smart-env",
}
# Relative to the repository root: the task DB and the model files.
# (orchestrator/tests/data holds test fixtures and is copied.)
EXCLUDED_PATHS = {"data", "local-llm/data"}
EXCLUDED_FILES = (
    ".env", "*.env", ".env.*", "*.pem", "*.key", "*.p12", "id_rsa*", "id_ed25519*",
    "*.db", "*.db-*", "*.sqlite", "*.tgz", "*.tar", "*.gz", "*.zip", "*.gguf",
)
# A copy with any of these is not handed out.
SECRET_PATTERNS = re.compile(
    r"tvly-[A-Za-z0-9_-]{16,}"
    r"|sk-[A-Za-z0-9_-]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?i:(?:api[_-]?key|secret|token|password)[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_\-/+]{24,})"
)


class JobError(Exception):
    """A refused or failed job; the message goes back to the orchestrator."""


def load_repos() -> dict:
    """
    name -> {"path", "exclude", "test_profile", "overlay"}. An entry is a
    path (I-2) or an object (M-3, plan-m karar B).
    """
    data = json.loads(REPOS_FILE.read_text(encoding="utf-8"))
    repos = {}
    for name, entry in data["repos"].items():
        entry = {"path": entry} if isinstance(entry, str) else dict(entry)
        profile = entry.get("test_profile") or DEFAULT_PROFILE
        if profile not in TEST_PROFILES:
            raise JobError(f"repo {name}: unknown test profile")
        overlay = None
        if entry.get("overlay"):
            overlay = (HERE / entry["overlay"]).resolve()
            if OVERLAY_ROOT.resolve() not in overlay.parents:
                raise JobError(f"repo {name}: overlay must be under runner/overlays")
        repos[name] = {
            "path": Path(entry["path"]),
            "exclude": tuple(str(x) for x in entry.get("exclude") or ()),
            "test_profile": profile,
            "overlay": overlay,
        }
    return repos


def check_task(task) -> str:
    if not isinstance(task, str) or not TASK_ID.match(task):
        raise JobError("task must match ^[a-z0-9-]{8,64}$")
    return task


def sandbox_of(task: str) -> Path:
    path = (SANDBOX_ROOT / task).resolve()
    if path.parent != SANDBOX_ROOT.resolve():
        raise JobError("sandbox path is outside the sandbox root")
    return path


def excluded_file(name: str, extra=()) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in (*EXCLUDED_FILES, *extra))


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_repo(source: Path, target: Path, manifest: dict | None = None, exclude=()) -> dict:
    """Regular files only; links, sockets and excluded names are left out."""
    files = copied = skipped = 0
    for folder, dirs, names in os.walk(source, followlinks=False):
        here = Path(folder)
        dirs[:] = sorted(
            d for d in dirs
            if d not in EXCLUDED_DIRS and not (here / d).is_symlink()
            and str((here / d).relative_to(source)) not in EXCLUDED_PATHS
            and not any(fnmatch.fnmatch(d, pattern) for pattern in exclude)
        )
        for name in sorted(names):
            path = here / name
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or excluded_file(name, exclude):
                skipped += 1
                continue
            if info.st_size > MAX_FILE_BYTES:
                skipped += 1
                continue
            copied += info.st_size
            if copied > MAX_COPY_BYTES:
                raise JobError(f"repository copy is over {MAX_COPY_BYTES // (1024 * 1024)} MB")
            destination = target / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
            if manifest is not None:
                manifest[str(path.relative_to(source))] = file_hash(destination)
            files += 1
    return {"files": files, "bytes": copied, "skipped": skipped}


def secret_hits(root: Path) -> list[str]:
    hits = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
            if SECRET_PATTERNS.search(text):
                hits.append(str(path.relative_to(root)))
    return hits


def remove_tree(path: Path):
    if path.parent.resolve() != SANDBOX_ROOT.resolve():
        raise JobError("refusing to remove a path outside the sandbox root")
    shutil.rmtree(path, ignore_errors=True)


def remove_old_sandboxes(now: float | None = None) -> int:
    now = now or time.time()
    removed = 0
    for path in SANDBOX_ROOT.iterdir() if SANDBOX_ROOT.exists() else []:
        if path.is_dir() and not path.is_symlink() and now - path.stat().st_mtime > SANDBOX_DAYS * 86400:
            remove_tree(path)
            removed += 1
    return removed


def add_overlay(config: dict, copy: Path) -> list[str]:
    """Copy the repo's overlay files into the copy (again before each test run)."""
    overlay = config.get("overlay")
    if not overlay or not overlay.is_dir():
        return []
    added = []
    for path in sorted(p for p in overlay.rglob("*") if p.is_file() and not p.is_symlink()):
        relative = path.relative_to(overlay)
        if "__pycache__" in relative.parts:
            continue
        destination = copy / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            destination.unlink()
        shutil.copyfile(path, destination)
        added.append(str(relative))
    return added


def sandbox_repo(sandbox: Path) -> tuple[str, dict]:
    try:
        repo = json.loads((sandbox / "repo.json").read_text(encoding="utf-8"))["repo"]
        return repo, load_repos()[repo]
    except (OSError, ValueError, KeyError):
        raise JobError("sandbox has no manifest or unknown repo; prepare it again") from None


def prepare(task, repo) -> dict:
    task = check_task(task)
    repos = load_repos()
    if repo not in repos:
        raise JobError(f"unknown repo; allowed: {', '.join(sorted(repos))}")

    config = repos[repo]
    sandbox = sandbox_of(task)
    copy = sandbox / REPO_COPY
    if copy.is_dir():
        return {"status": "ok", "task": task, "repo": repo, "reused": True}

    SANDBOX_ROOT.mkdir(mode=0o755, parents=True, exist_ok=True)
    remove_old_sandboxes()
    temporary = Path(tempfile.mkdtemp(prefix=f"{task}.", dir=SANDBOX_ROOT))
    try:
        manifest: dict = {}
        counts = copy_repo(config["path"], temporary / REPO_COPY, manifest, config["exclude"])
        overlay = add_overlay(config, temporary / REPO_COPY)
        (temporary / OVERLAY_LIST).write_text(json.dumps(overlay), encoding="utf-8")
        hits = secret_hits(temporary / REPO_COPY)
        if hits:
            raise JobError(f"secret pattern found; sandbox not created: {', '.join(hits[:10])}")
        (temporary / "repo.json").write_text(json.dumps({"repo": repo}), encoding="utf-8")
        (temporary / MANIFEST).write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
        os.chmod(temporary, 0o755)
        temporary.rename(sandbox)
    finally:
        if temporary.exists():
            remove_tree(temporary)

    return {"status": "ok", "task": task, "repo": repo, "reused": False, **counts}


def test_command(task: str, run: int, profile: str, copy: Path) -> list[str]:
    """The fixed docker argv of one test run (no shell)."""
    return [
        DOCKER, "run", "--rm", "--name", f"savas-test-{task[:40]}-{run}",
        "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--user", "1000:1000",
        "--memory", "1g", "--memory-swap", "1g", "--cpus", "1", "--pids-limit", "256",
        "--tmpfs", "/tmp:size=256m", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "HOME=/tmp",
        "-v", f"{copy}:/work:ro", "-w", "/work",
        PROFILE_IMAGES[profile], *TEST_PROFILES[profile],
    ]


def run_tests(task, profile, run_command=subprocess.run) -> dict:
    task = check_task(task)
    if profile not in (*TEST_PROFILES, "auto"):
        raise JobError(f"unknown profile; allowed: {', '.join(sorted(TEST_PROFILES))}, auto")

    sandbox = sandbox_of(task)
    copy = sandbox / REPO_COPY
    if not copy.is_dir():
        raise JobError("no sandbox for this task; prepare it first")
    _, config = sandbox_repo(sandbox)
    if profile == "auto":
        profile = config["test_profile"]
    if profile != config["test_profile"]:
        raise JobError(f"this repo is tested with profile {config['test_profile']}")
    # The worker may have edited the smoke tests; the run uses the originals.
    add_overlay(config, copy)

    runs = sandbox / "runs"
    runs.mkdir(exist_ok=True)
    run = len(list(runs.glob("*.log"))) + 1
    if run > MAX_TEST_RUNS:
        raise JobError(f"at most {MAX_TEST_RUNS} test runs per task")

    command = test_command(task, run, profile, copy)
    timed_out = False
    try:
        done = run_command(command, capture_output=True, text=True, timeout=TEST_TIMEOUT_SECONDS)
        exit_code, output = done.returncode, (done.stdout or "") + (done.stderr or "")
    except subprocess.TimeoutExpired as exc:
        run_command([DOCKER, "kill", command[4]], capture_output=True, text=True, timeout=30)
        timed_out, exit_code = True, None
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        output += f"\n[runner] timed out after {TEST_TIMEOUT_SECONDS} s; container killed\n"

    (runs / f"{run}.log").write_text(output, encoding="utf-8")
    return {
        "status": "ok", "task": task, "profile": profile, "run": run,
        "exit_code": exit_code, "passed": exit_code == 0, "timed_out": timed_out,
        "output": output[-TEST_OUTPUT_CHARS:],
    }


def text_lines(path: Path) -> list[str] | None:
    try:
        return path.read_text(encoding="utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return None


def make_diff(task) -> dict:
    """
    Files of the copy that changed or are new, against the live repo.
    A file whose live version changed since the copy is a conflict and
    is left out. The patch applies with `patch -p1` in the repo root.
    """
    task = check_task(task)
    sandbox = sandbox_of(task)
    copy = sandbox / REPO_COPY
    if not copy.is_dir():
        raise JobError("no sandbox for this task; prepare it first")
    try:
        manifest = json.loads((sandbox / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise JobError("sandbox has no manifest or unknown repo; prepare it again") from None
    repo, config = sandbox_repo(sandbox)
    source = config["path"]
    try:
        overlay = set(json.loads((sandbox / OVERLAY_LIST).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        overlay = set()

    chunks, files, conflicts, binary = [], [], [], []
    added = removed = 0
    for path in sorted(p for p in copy.rglob("*") if p.is_file() and not p.is_symlink()):
        relative = str(path.relative_to(copy))
        if manifest.get(relative) == file_hash(path) or (relative in overlay and relative not in manifest):
            continue
        live = source / relative
        if relative in manifest:
            if not live.is_file() or file_hash(live) != manifest[relative]:
                conflicts.append(relative)
                continue
        elif live.exists():
            conflicts.append(relative)
            continue

        new = text_lines(path)
        old = text_lines(live) if relative in manifest else []
        if new is None or old is None:
            binary.append(relative)
            continue
        lines = list(difflib.unified_diff(
            old, new,
            fromfile=f"a/{relative}" if relative in manifest else "/dev/null",
            tofile=f"b/{relative}",
        ))
        if not lines:
            continue
        for line in lines:
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
        # A last line without a newline needs the marker patch expects.
        chunks.append("".join(
            line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
            for line in lines
        ))
        files.append(relative)

    patch = "".join(chunks)
    (sandbox / PATCH_FILE).write_text(patch, encoding="utf-8")
    return {
        "status": "ok", "task": task, "repo": repo, "files": files,
        "added": added, "removed": removed, "conflicts": conflicts, "binary": binary,
        "patch": str(sandbox / PATCH_FILE), "diff": patch[:DIFF_OUTPUT_CHARS],
        "truncated": len(patch) > DIFF_OUTPUT_CHARS,
    }


def cleanup(task) -> dict:
    sandbox = sandbox_of(check_task(task))
    existed = sandbox.exists()
    if existed:
        remove_tree(sandbox)
    return {"status": "ok", "task": task, "removed": existed}


JOBS = {
    "/prepare": lambda body: prepare(body.get("task"), body.get("repo")),
    "/test": lambda body: run_tests(body.get("task"), body.get("profile")),
    "/diff": lambda body: make_diff(body.get("task")),
    "/cleanup": lambda body: cleanup(body.get("task")),
}


class Handler(BaseHTTPRequestHandler):
    def reply(self, status: int, body: dict):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self.reply(200, {"status": "ok", "jobs": sorted(JOBS)})
        else:
            self.reply(404, {"status": "error", "error": "unknown path"})

    def do_POST(self):
        job = JOBS.get(self.path)
        if job is None:
            return self.reply(404, {"status": "error", "error": "unknown job"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            return self.reply(413, {"status": "error", "error": "request too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise JobError("body must be a JSON object")
            started = time.monotonic()
            result = job(body)
            result["ms"] = int((time.monotonic() - started) * 1000)
            self.reply(200, result)
        except (JobError, ValueError) as exc:
            self.reply(400, {"status": "error", "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - the service must stay up
            self.reply(500, {"status": "error", "error": type(exc).__name__})

    def address_string(self):
        return "unix"

    def log_message(self, format, *args):
        sys.stderr.write("savas-runner: " + (format % args) + "\n")


class Server(socketserver.UnixStreamServer):
    # One job at a time (plan-i karar C: a single test run at once).
    pass


def main():
    SOCKET_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if SOCKET_PATH.exists() or SOCKET_PATH.is_socket():
        SOCKET_PATH.unlink()
    old_umask = os.umask(0o177)
    try:
        server = Server(str(SOCKET_PATH), Handler)
    finally:
        os.umask(old_umask)
    sys.stderr.write(f"savas-runner: listening on {SOCKET_PATH}\n")
    server.serve_forever()


if __name__ == "__main__":
    main()
