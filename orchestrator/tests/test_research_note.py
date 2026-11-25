"""H-3: research note template; vault write through the normal approval."""

import asyncio
import json

import pytest

import research as rs


SOURCES = [
    {"n": 1, "url": "https://a.com/x", "title": "Fiyatlar [2026]", "snippet": "s", "engine": "tavily",
     "query": "elektrik fiyatı", "fetched_at": "2026-11-15T19:00:00+00:00"},
    {"n": 2, "url": "https://b.com", "title": "", "snippet": "s", "engine": "tavily",
     "query": "tarife", "fetched_at": "2026-11-15T19:00:00+00:00"},
]
TASK = {
    "task_id": "w-1", "kind": "worker", "worker": "oracle", "status": "completed",
    "message": "Türkiye'de 2026 konut elektriği kWh fiyatını araştır.\nKısa özet ver.",
    "result": {"status": "ok", "summary": "Düşük kademe 3,24 TL [1].", "findings": ["Sınır günlük 8 kWh [2]"],
               "deliverable": "", "open_questions": ["Vergi oranı?"]},
}


# ----------------------------------------------------------------------
# template
# ----------------------------------------------------------------------

def test_path_is_in_the_research_folder_with_a_plain_slug():
    assert rs.research_note_path(TASK["message"], "2026-11-15") == \
        "Araştırma/2026-11-15-turkiye-de-2026-konut-elektrigi-kwh-fiyatini-arast.md"
    assert rs.research_note_path("!!!", "2026-11-15") == "Araştırma/2026-11-15-arastirma.md"


def test_note_keeps_the_report_and_numbers_the_sources():
    note = rs.research_note(TASK, SOURCES, "2026-11-15")
    assert note.startswith("---\ntype: research\nsource_task: w-1\nworker: oracle\ncreated: 2026-11-15\n---\n")
    assert "\n# Türkiye'de 2026 konut elektriği kWh fiyatını araştır.\n" in note
    assert "**Soru:** Türkiye'de 2026 konut elektriği kWh fiyatını araştır. Kısa özet ver." in note
    assert "## Özet\n\nDüşük kademe 3,24 TL [1]." in note
    assert "## Bulgular\n\n- Sınır günlük 8 kWh [2]" in note and "## Açık sorular\n\n- Vergi oranı?" in note
    assert "## Çıktı" not in note
    assert '1. [Fiyatlar (2026)](https://a.com/x) — arama: "elektrik fiyatı", 2026-11-15' in note
    assert "2. [https://b.com](https://b.com)" in note


def test_note_without_sources_says_so():
    assert rs.research_note(TASK, [], "2026-11-15").endswith("_Bu raporun kayıtlı kaynağı yok._\n")


# ----------------------------------------------------------------------
# create_note arguments
# ----------------------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "data" / "savas.db"))


@pytest.fixture
def app(db, orch, monkeypatch):
    monkeypatch.setattr(orch, "OMNIROUTE_KEY", "test-key")
    orch.task_store.save_task({"task_id": "parent-0", "message": "m", "status": "completed"})
    orch.task_store.save_task({**TASK, "parent_id": "parent-0"})
    orch.task_store.save_sources("w-1", SOURCES)
    yield orch
    orch.task_store.close()


def test_plain_create_note_is_unchanged(app):
    arguments = {"path": "notlar/x.md", "content": "içerik"}
    assert app.prepare_create_note(arguments) is arguments


@pytest.mark.parametrize("arguments,word", [
    ({"path": "x.md"}, "needs path and content"),
    ({"content": "c"}, "needs path and content"),
    ({"from_task": "w-1", "content": "c"}, "not both"),
    ({"from_task": "yok"}, "completed worker task"),
    ({"from_task": "parent-0"}, "completed worker task"),
])
def test_unusable_arguments_are_refused(app, arguments, word):
    with pytest.raises(ValueError, match=word):
        app.prepare_create_note(arguments)


def test_running_worker_task_cannot_be_saved(app):
    app.task_store.save_task({"task_id": "w-2", "kind": "worker", "worker": "oracle", "status": "running",
                              "message": "m", "parent_id": "parent-0"})
    with pytest.raises(ValueError, match="completed worker task"):
        app.prepare_create_note({"from_task": "w-2"})


def test_from_task_builds_the_note_and_a_default_path(app):
    day = app.utc_now()[:10]
    prepared = app.prepare_create_note({"from_task": "w-1"})
    assert prepared == {"path": rs.research_note_path(TASK["message"], day),
                        "content": rs.research_note(app.task_store.get_task("w-1"),
                                                    app.task_store.list_sources("w-1"), day)}
    assert app.prepare_create_note({"from_task": "w-1", "path": "Araştırma/elektrik.md"})["path"] == \
        "Araştırma/elektrik.md"


def test_research_result_tells_ares_its_task_id(app):
    result = {"status": "completed", "report": {"summary": "x [1]"}}
    app.attach_sources(result, SOURCES, "parent-0", "oracle", "w-1")
    assert result["task_id"] == "w-1"


def test_create_note_offers_from_task_and_requires_nothing_fixed(orch):
    tool = next(t["function"] for t in orch.TOOLS if t["function"]["name"] == "create_note")
    assert "from_task" in tool["parameters"]["properties"] and "required" not in tool["parameters"]


# ----------------------------------------------------------------------
# through Ares and the approval
# ----------------------------------------------------------------------

def reply(content=None, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return 200, {
        "model": "gh/gpt-4.1",
        "choices": [{"message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }


def note_call(arguments):
    return {"id": "call_n", "type": "function",
            "function": {"name": "create_note", "arguments": json.dumps(arguments)}}


@pytest.fixture
def ares(app, monkeypatch):
    """replies(*queue); writes: what reached the vault."""
    writes = []

    async def write(path, content, task_id):
        writes.append((path, content))
        return {"status": "ok"}

    monkeypatch.setattr(app, "create_note", write)

    def replies(*queue):
        queue = list(queue)
        app.fake_client.handler = staticmethod(lambda url, payload: queue.pop(0) if queue else reply("kaydedildi"))

    return replies, writes


def test_note_waits_for_approval_with_the_full_text_and_is_written_as_approved(app, ares):
    replies, writes = ares
    replies(reply(tool_calls=[note_call({"from_task": "w-1"})]))
    waiting = asyncio.run(app.create_task_endpoint(app.TaskRequest(message="araştırmayı kaydet")))

    assert waiting["status"] == "waiting_approval" and writes == []
    approval = waiting["pending_approvals"][0]
    assert approval["action"] == "create_note" and "from_task" not in approval["arguments"]
    assert approval["arguments"]["path"].startswith("Araştırma/")
    assert "## Kaynaklar" in approval["arguments"]["content"]

    done = asyncio.run(app.approve_endpoint(approval["approval_id"]))
    assert done["status"] == "completed"
    assert writes == [(approval["arguments"]["path"], approval["arguments"]["content"])]


def test_rejected_note_writes_nothing(app, ares):
    replies, writes = ares
    replies(reply(tool_calls=[note_call({"from_task": "w-1"})]))
    waiting = asyncio.run(app.create_task_endpoint(app.TaskRequest(message="araştırmayı kaydet")))
    asyncio.run(app.reject_endpoint(waiting["pending_approvals"][0]["approval_id"]))
    assert writes == []


def test_bad_from_task_goes_back_to_ares_without_an_approval(app, ares):
    replies, writes = ares
    seen = []

    def handler(url, payload):
        seen.append(payload)
        return reply(tool_calls=[note_call({"from_task": "yok"})]) if len(seen) == 1 else reply("bulunamadı")

    app.fake_client.handler = staticmethod(handler)
    done = asyncio.run(app.create_task_endpoint(app.TaskRequest(message="kaydet")))

    assert done["status"] == "completed" and writes == []
    assert asyncio.run(app.list_approvals(status="pending"))["approvals"] == []
    [tool_message] = [m for m in seen[1]["messages"] if m.get("role") == "tool"]
    assert "completed worker task" in json.loads(tool_message["content"])["error"]
