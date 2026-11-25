"""I-2: read-only repository tool over a runner-made sandbox copy (plan-i karar F)."""

import asyncio
import json

import pytest

import repo_tools as rt


# ----------------------------------------------------------------------
# pure functions
# ----------------------------------------------------------------------

@pytest.fixture
def copy(tmp_path):
    root = tmp_path / "repo"
    (root / "orchestrator" / "app").mkdir(parents=True)
    (root / "orchestrator" / "app" / "main.py").write_text("print('ana')\n")
    (root / "orchestrator" / "app" / "memory.py").write_text("x = 1\n")
    (root / "docs").mkdir()
    (root / "docs" / "roadmap.md").write_text("# Yol\n")
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
    (tmp_path / "outside.txt").write_text("dışarıda\n")
    (root / "link.txt").symlink_to(tmp_path / "outside.txt")
    return root


def test_list_all_and_by_pattern(copy):
    assert rt.list_files(copy, None)["files"] == [
        "docs/roadmap.md", "logo.png", "orchestrator/app/main.py", "orchestrator/app/memory.py"]
    assert rt.list_files(copy, "orchestrator/app/*.py") == {
        "pattern": "orchestrator/app/*.py", "count": 2,
        "files": ["orchestrator/app/main.py", "orchestrator/app/memory.py"]}


def test_long_list_is_cut_with_a_note(copy, monkeypatch):
    monkeypatch.setattr(rt, "MAX_LISTED_FILES", 2)
    listed = rt.list_files(copy, "*")
    assert listed["count"] == 4 and len(listed["files"]) == 2 and "daha dar" in listed["note"]


def test_read_text_file(copy):
    assert rt.read_file(copy, "orchestrator/app/main.py") == {"path": "orchestrator/app/main.py",
                                                              "content": "print('ana')\n"}
    assert rt.read_file(copy, "/docs/roadmap.md")["content"] == "# Yol\n"


@pytest.mark.parametrize("path,word", [
    ("../outside.txt", "No such file"), ("link.txt", "No such file|Not a regular"),
    ("logo.png", "Not a text"), ("yok.py", "No such file"), ("orchestrator", "Not a regular"), ("", "required"),
])
def test_unreadable_paths_are_refused(copy, path, word):
    with pytest.raises(rt.RepoError, match=word):
        rt.read_file(copy, path)


def test_big_file_is_refused(copy, monkeypatch):
    monkeypatch.setattr(rt, "MAX_READ_BYTES", 3)
    with pytest.raises(rt.RepoError, match="larger"):
        rt.read_file(copy, "docs/roadmap.md")


@pytest.mark.parametrize("repo", ["savas-ai", "x"])
def test_repo_names(repo):
    assert rt.check_repo_name(repo) == repo


@pytest.mark.parametrize("repo", ["/home/savas", "../x", "", None, "A"])
def test_bad_repo_names(repo):
    with pytest.raises(rt.RepoError):
        rt.check_repo_name(repo)


# ----------------------------------------------------------------------
# through main.py with a stand-in runner
# ----------------------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "data" / "savas.db"))


@pytest.fixture
def app(db, orch, tmp_path, monkeypatch):
    monkeypatch.setattr(orch, "OMNIROUTE_KEY", "test-key")
    monkeypatch.setattr(orch, "RUNNER_SOCKET", "/run/savas-runner/run.sock")
    monkeypatch.setattr(orch, "SANDBOX_ROOT", str(tmp_path / "sandbox"))
    yield orch
    orch.task_store.close()


@pytest.fixture
def runner(app, tmp_path, monkeypatch):
    """calls: runner requests; setup['fail']: error to return."""
    calls, setup = [], {"fail": None}

    async def fake(path, body):
        calls.append((path, body))
        if setup["fail"]:
            return {"status": "error", "error": setup["fail"]}
        sandbox = tmp_path / "sandbox" / body["task"]
        (sandbox / "repo" / "orchestrator").mkdir(parents=True)
        (sandbox / "repo" / "orchestrator" / "main.py").write_text("print(1)\n" * 2000)
        (sandbox / "repo.json").write_text(json.dumps({"repo": body["repo"]}))
        return {"status": "ok", "files": 1, "skipped": 3}

    monkeypatch.setattr(app, "runner_call", fake)
    return calls, setup


def tool(app, arguments, task_id="task-0001"):
    return asyncio.run(app.execute_read_tool("repo_read", arguments, task_id))


def test_first_use_prepares_the_copy_once(app, runner):
    calls, _ = runner
    listed = tool(app, {"repo": "savas-ai"})
    assert listed == {"repo": "savas-ai", "pattern": "*", "count": 1, "files": ["orchestrator/main.py"]}
    tool(app, {"repo": "savas-ai", "pattern": "*.py"})
    assert calls == [("/prepare", {"task": "task-0001", "repo": "savas-ai"})]
    [event] = [e for e in app.ACTIVITY if e["event"] == "runner"]
    assert event["status"] == "prepare" and event["files"] == 1 and event["skipped"] == 3


def test_reading_a_long_file_comes_in_parts_and_repeats_are_not_answered_twice(app, runner):
    app.TASK_CONTEXTS["task-0001"] = {}  # reads are remembered with the task context
    first = tool(app, {"repo": "savas-ai", "path": "orchestrator/main.py"})
    assert first["truncated"] is True and "repo_read(offset=" in first["note"]
    assert first["total_chars"] == len("print(1)\n" * 2000)
    again = tool(app, {"repo": "savas-ai", "path": "orchestrator/main.py"})
    assert again["repeat"] is True and "content" not in again
    rest = tool(app, {"repo": "savas-ai", "path": "orchestrator/main.py", "offset": first["next_offset"]})
    assert rest["content"].startswith("print(1)")


def test_errors_go_back_to_the_model(app, runner):
    calls, setup = runner
    assert tool(app, {"repo": "../x"})["status"] == "error"
    assert "No such file" in tool(app, {"repo": "savas-ai", "path": "../../etc/passwd"})["error"]
    assert "one repository per task" in tool(app, {"repo": "baska"})["error"]
    setup["fail"] = "secret pattern found; sandbox not created: docs/x.md"
    refused = tool(app, {"repo": "savas-ai"}, task_id="task-0002")
    assert refused["status"] == "error" and "secret pattern" in refused["error"]
    assert [e["status"] for e in app.ACTIVITY if e["event"] == "runner"][-1] == "failed"


def test_without_a_runner_the_tool_says_so(app, runner, monkeypatch):
    calls, _ = runner
    monkeypatch.setattr(app, "RUNNER_SOCKET", "")
    assert tool(app, {"repo": "savas-ai"}) == {"status": "error", "error": "Repository tools are not configured."}
    assert calls == []


def test_ares_reads_the_repository_without_an_approval(app, runner):
    def reply(content=None, tool_calls=None):
        message = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return 200, {"model": "gh/gpt-4.1",
                     "choices": [{"message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
                     "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}

    call = {"id": "call_r", "type": "function",
            "function": {"name": "repo_read", "arguments": json.dumps({"repo": "savas-ai"})}}
    seen = []

    def handler(url, payload):
        seen.append(payload)
        return reply(tool_calls=[call]) if len(seen) == 1 else reply("bir dosya var")

    app.fake_client.handler = staticmethod(handler)
    done = asyncio.run(app.create_task_endpoint(app.TaskRequest(message="repoda ne var?")))
    assert done["status"] == "completed" and done.get("pending_approvals") in (None, [])
    [message] = [m for m in seen[1]["messages"] if m.get("role") == "tool"]
    assert json.loads(message["content"])["files"] == ["orchestrator/main.py"]


def test_one_repository_tool_is_offered(orch):
    names = [t["function"]["name"] for t in orch.TOOLS]
    assert "repo_read" in names and "repo_list" not in names
    tool_def = next(t["function"] for t in orch.TOOLS if t["function"]["name"] == "repo_read")
    assert tool_def["parameters"]["required"] == ["repo"]
