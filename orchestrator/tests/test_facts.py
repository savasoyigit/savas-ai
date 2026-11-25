"""F-3: lasting facts — remember tool, expiry, purge, API (plan-f karar D, E, F, J)."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

import memory
import task_store as ts
from task_store import TaskStore

NOW = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)


# ----------------------------------------------------------------------
# memory.py
# ----------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("İstanbul", "istanbul"),
    ("IŞIK", "ışık"),
    ("  Tercih   edilen   Dil. ", "tercih edilen dil"),
    ("\"Kod stili\"!", "kod stili"),
])
def test_normalize_folds_turkish_case_spaces_and_edge_punctuation(text, expected):
    assert memory.normalize(text) == expected


def test_new_fact_has_source_task_and_expiry_by_importance():
    fact = memory.new_fact({"key": " Tercih  edilen dil ", "value": "Türkçe", "importance": 1}, "t1", "ares", NOW)

    assert fact["key"] == "Tercih edilen dil" and fact["key_norm"] == "tercih edilen dil"
    assert (fact["source"], fact["task_id"], fact["scope"], fact["status"]) == ("ares", "t1", "global", "active")
    assert fact["expires_at"] == (NOW + timedelta(days=30)).isoformat()
    assert memory.new_fact({"key": "k", "value": "v", "importance": 2}, "t1", "ares", NOW)["expires_at"] == \
        (NOW + timedelta(days=180)).isoformat()
    assert memory.new_fact({"key": "k", "value": "v", "importance": 3}, "t1", "ares", NOW)["expires_at"] is None


def test_expires_in_days_overrides_importance():
    fact = memory.new_fact({"key": "k", "value": "v", "importance": 3, "expires_in_days": 7}, "t1", "ares", NOW)
    assert fact["expires_at"] == (NOW + timedelta(days=7)).isoformat()


def test_importance_defaults_to_2():
    assert memory.new_fact({"key": "k", "value": "v"}, "t1", "ares", NOW)["importance"] == 2


@pytest.mark.parametrize("arguments,word", [
    ({"key": "", "value": "v"}, "key"),
    ({"key": "k" * 101, "value": "v"}, "key"),
    ({"key": "k", "value": "  "}, "value"),
    ({"key": "k", "value": "v" * 501}, "value"),
    ({"key": "k", "value": "v", "importance": 4}, "importance"),
    ({"key": "k", "value": "v", "importance": True}, "importance"),
    ({"key": "k", "value": "v", "importance": "2"}, "importance"),
    ({"key": "k", "value": "v", "scope": "herkes"}, "scope"),
    ({"key": "k", "value": "v", "expires_in_days": 0}, "expires_in_days"),
    ({"key": "k", "value": "v", "expires_in_days": 3651}, "expires_in_days"),
    ({"key": "k", "value": "v", "expires_in_days": 1.5}, "expires_in_days"),
])
def test_unusable_arguments_are_refused(arguments, word):
    with pytest.raises(ValueError) as refused:
        memory.new_fact(arguments, "t1", "ares", NOW)
    assert word in str(refused.value)


def test_later_expiry_prefers_no_expiry():
    assert memory.later_expiry("2026-12-01", "2027-01-01") == "2027-01-01"
    assert memory.later_expiry(None, "2027-01-01") is None
    assert memory.later_expiry("2027-01-01", None) is None


def test_same_value_ignores_case_and_spacing():
    assert memory.same_fact_value("Türkçe ", "türkçe")
    assert not memory.same_fact_value("Türkçe", "İngilizce")


# ----------------------------------------------------------------------
# task_store.py
# ----------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    store = TaskStore(tmp_path / "savas.db")
    yield store
    store.close()


def fact(key="Dil", value="Türkçe", importance=2, now=NOW, **extra):
    return {**memory.new_fact({"key": key, "value": value, "importance": importance}, "t1", "ares", now), **extra}


def test_fact_is_saved_and_read_back(store):
    fact_id = store.save_fact(fact())
    saved = store.get_fact(fact_id)
    assert saved["fact_id"] == fact_id and saved["value"] == "Türkçe" and saved["task_id"] == "t1"
    assert store.get_fact(999) is None


def test_fact_needs_its_fields_and_a_known_status(store):
    with pytest.raises(ts.StoreError):
        store.save_fact({"key": "k", "value": "v"})
    with pytest.raises(ts.StoreError):
        store.save_fact(fact(status="unknown"))
    with pytest.raises(ts.StoreError):
        store.set_fact_status(1, "gone")


def test_find_active_fact_skips_other_scope_flagged_and_expired(store):
    store.save_fact(fact(scope="jarvis"))
    store.save_fact(fact(status="flagged"))
    store.save_fact(fact(importance=1, now=NOW - timedelta(days=31)))
    assert store.find_active_fact("dil", "global", NOW.isoformat()) is None

    fact_id = store.save_fact(fact())
    assert store.find_active_fact("dil", "global", NOW.isoformat())["fact_id"] == fact_id


def test_confirm_counts_and_moves_the_expiry(store):
    fact_id = store.save_fact(fact())
    store.confirm_fact(fact_id, None)
    confirmed = store.get_fact(fact_id)
    assert confirmed["confirmations"] == 2 and confirmed["expires_at"] is None


def test_list_facts_filters_and_hides_expired(store):
    store.save_fact(fact(key="a", importance=1, now=NOW - timedelta(days=40)))
    store.save_fact(fact(key="b", scope="oracle"))
    store.save_fact(fact(key="c", status="flagged"))

    assert [f["key"] for f in store.list_facts()] == ["c", "b", "a"]
    assert [f["key"] for f in store.list_facts(now=NOW.isoformat())] == ["c", "b"]
    assert [f["key"] for f in store.list_facts(status="active", now=NOW.isoformat())] == ["b"]
    assert [f["key"] for f in store.list_facts(scope="oracle")] == ["b"]
    assert len(store.list_facts(limit=1)) == 1


def test_purge_deletes_only_long_expired_facts(store):
    old = store.save_fact(fact(key="eski", importance=1, now=NOW - timedelta(days=61)))
    recent = store.save_fact(fact(key="yeni", importance=1, now=NOW - timedelta(days=45)))
    forever = store.save_fact(fact(key="süresiz", importance=3, now=NOW - timedelta(days=999)))

    assert store.purge_facts((NOW - timedelta(days=memory.FACT_PURGE_DAYS)).isoformat()) == 1
    assert store.get_fact(old) is None
    assert store.get_fact(recent) and store.get_fact(forever)


def test_facts_are_in_the_full_text_index(store):
    fact_id = store.save_fact(fact(key="Kod stili", value="Kısa fonksiyonlar, İstanbul ofisi"))
    found = store.conn.execute("SELECT rowid FROM facts_fts WHERE facts_fts MATCH 'fonksiyon*'").fetchall()
    assert [row[0] for row in found] == [fact_id]

    store.conn.execute("DELETE FROM facts WHERE fact_id = ?", (fact_id,))
    assert store.conn.execute("SELECT rowid FROM facts_fts WHERE facts_fts MATCH 'fonksiyon*'").fetchall() == []


def test_v2_database_is_migrated_with_its_episodes(tmp_path, monkeypatch):
    path = tmp_path / "savas.db"
    with monkeypatch.context() as patch:
        patch.setattr(ts, "SCHEMA_VERSION", 2)
        patch.setattr(ts, "MIGRATIONS", ts.MIGRATIONS[:2])
        old = TaskStore(path)
        old.save_task({"task_id": "t1", "message": "eski", "status": "completed"})
        old.save_episode({"task_id": "t1", "status": "completed", "title": "eski", "summary": "s",
                          "workers": [], "created_at": NOW.isoformat(),
                          "expires_at": (NOW + timedelta(days=180)).isoformat()})
        old.close()

    store = TaskStore(path)
    try:
        assert store.schema_version() == ts.SCHEMA_VERSION
        assert store.get_task("t1")["message"] == "eski" and store.get_episode("t1")["title"] == "eski"
        assert store.get_fact(store.save_fact(fact()))["value"] == "Türkçe"
    finally:
        store.close()


# ----------------------------------------------------------------------
# main.py
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


def remember(**arguments):
    return {"id": "call_m", "type": "function",
            "function": {"name": "remember", "arguments": json.dumps(arguments)}}


def run(orch, *calls):
    """One task: the model makes `calls` in order, then answers. Returns the task id."""
    queue = [reply(tool_calls=[call]) for call in calls]
    orch.fake_client.handler = staticmethod(lambda url, payload: queue.pop(0) if queue else reply("tamam"))
    return asyncio.run(orch.create_task_endpoint(orch.TaskRequest(message="bunu hatırla")))["task_id"]


def tool_results(orch, task_id):
    return [e for e in orch.ACTIVITY if e["event"] == "memory" and e["agent"] == "ares" and e["task_id"] == task_id
            and e["status"] != "retrieved"]


@pytest.fixture(params=["store"])
def app(request, tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "data" / "savas.db"))
    orch = request.getfixturevalue("orch")
    monkeypatch.setattr(orch, "OMNIROUTE_KEY", "test-key")
    yield orch
    orch.task_store.close()


def test_remember_is_offered_to_ares_without_approval(orch):
    tools = {tool["function"]["name"]: tool["function"] for tool in orch.TOOLS}
    assert tools["remember"]["parameters"]["required"] == ["key", "value", "importance"]
    assert "remember" not in orch.WRITE_ACTIONS


def test_remember_saves_a_fact_with_its_task_and_source(app):
    task_id = run(app, remember(key="Tercih edilen dil", value="Türkçe", importance=3))

    assert app.TASKS[task_id]["status"] == "completed"
    [saved] = app.task_store.list_facts()
    assert (saved["key"], saved["value"], saved["task_id"], saved["source"]) == \
        ("Tercih edilen dil", "Türkçe", task_id, "ares")
    assert saved["expires_at"] is None and saved["status"] == "active"
    [event] = tool_results(app, task_id)
    assert event["status"] == "saved" and event["fact_id"] == saved["fact_id"]
    assert app.APPROVALS == {}


def test_same_value_again_is_a_confirmation(app):
    run(app, remember(key="Dil", value="Türkçe", importance=1))
    task_id = run(app, remember(key="dil ", value="türkçe", importance=2))

    [saved] = app.task_store.list_facts()
    assert saved["confirmations"] == 2
    assert saved["expires_at"] > (datetime.now(timezone.utc) + timedelta(days=170)).isoformat()
    assert tool_results(app, task_id)[0]["status"] == "confirmed"


def test_different_value_is_flagged_and_the_old_one_stays(app):
    run(app, remember(key="Dil", value="Türkçe", importance=2))
    task_id = run(app, remember(key="Dil", value="İngilizce", importance=2))

    by_value = {f["value"]: f["status"] for f in app.task_store.list_facts()}
    assert by_value == {"Türkçe": "active", "İngilizce": "flagged"}
    assert tool_results(app, task_id)[0]["status"] == "conflict"


def test_conflict_result_tells_the_model_the_existing_value(app):
    run(app, remember(key="Dil", value="Türkçe", importance=2))
    result = app.execute_memory_tool({"key": "Dil", "value": "İngilizce", "importance": 2}, "t2")

    assert result["status"] == "conflict"
    assert result["existing"]["value"] == "Türkçe" and "user" in result["note"]


def test_other_scope_is_a_separate_fact(app):
    run(app, remember(key="Dil", value="Türkçe", importance=2))
    result = app.execute_memory_tool({"key": "Dil", "value": "Python", "importance": 2, "scope": "jarvis"}, "t2")
    assert result["status"] == "saved"


def test_invalid_arguments_come_back_as_an_error_and_the_task_goes_on(app):
    task_id = run(app, remember(key="Dil", value="Türkçe", importance=9))

    assert app.TASKS[task_id]["status"] == "completed"
    assert app.task_store.list_facts() == []
    assert tool_results(app, task_id)[0]["status"] == "invalid"


def test_the_tool_result_reaches_the_model(app):
    payloads = []

    def handler(url, payload):
        payloads.append(payload)
        return reply(tool_calls=[remember(key="Dil", value="Türkçe", importance=2)]) if len(payloads) == 1 \
            else reply("tamam")

    app.fake_client.handler = staticmethod(handler)
    asyncio.run(app.create_task_endpoint(app.TaskRequest(message="hatırla")))

    [tool_message] = [m for m in payloads[1]["messages"] if m.get("role") == "tool"]
    assert tool_message["tool_call_id"] == "call_m"
    assert json.loads(tool_message["content"])["status"] == "saved"


def test_without_a_store_nothing_is_saved(orch):
    assert orch.task_store is None
    assert orch.execute_memory_tool({"key": "k", "value": "v", "importance": 2}, "t1")["status"] == "unavailable"


def test_list_and_delete_endpoints(app):
    run(app, remember(key="Dil", value="Türkçe", importance=2))
    [listed] = asyncio.run(app.list_facts())["facts"]

    assert asyncio.run(app.delete_fact(listed["fact_id"])) == {"fact_id": listed["fact_id"], "status": "deleted"}
    assert asyncio.run(app.list_facts(status="active"))["count"] == 0
    assert asyncio.run(app.list_facts(status="deleted"))["count"] == 1

    with pytest.raises(app.HTTPException) as missing:
        asyncio.run(app.delete_fact(999))
    assert missing.value.status_code == 404
    with pytest.raises(app.HTTPException) as unknown:
        asyncio.run(app.list_facts(status="gone"))
    assert unknown.value.status_code == 422


def test_expired_facts_are_listed_only_on_request(app):
    app.task_store.save_fact(fact(importance=1, now=datetime.now(timezone.utc) - timedelta(days=31)))
    assert asyncio.run(app.list_facts())["count"] == 0
    assert asyncio.run(app.list_facts(include_expired=True))["count"] == 1


def test_delete_without_a_store_is_503(orch):
    with pytest.raises(orch.HTTPException) as refused:
        asyncio.run(orch.delete_fact(1))
    assert refused.value.status_code == 503
    assert asyncio.run(orch.list_facts()) == {"count": 0, "facts": []}


def test_startup_purges_long_expired_facts(app):
    now = datetime.now(timezone.utc)
    old = app.task_store.save_fact(fact(key="eski", importance=1, now=now - timedelta(days=61)))
    recent = app.task_store.save_fact(fact(key="yeni", importance=1, now=now - timedelta(days=45)))
    path = str(app.task_store.path)
    app.task_store.close()

    store = app.open_task_store(path)
    assert store.get_fact(old) is None and store.get_fact(recent)["key"] == "yeni"


def test_fact_endpoints_are_async(orch):
    import inspect
    for name in ("list_facts", "delete_fact"):
        assert inspect.iscoroutinefunction(getattr(orch, name)), name
