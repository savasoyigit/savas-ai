"""H-2: web search for delegated tasks (plan-h karar 3–6)."""

import asyncio
import json

import pytest

import delegation as dg
import research as rs
import task_store as ts
from task_store import TaskStore


# ----------------------------------------------------------------------
# pure functions
# ----------------------------------------------------------------------

def test_queries_are_cleaned_and_deduplicated():
    assert rs.clean_queries(None) == [] and rs.clean_queries([]) == []
    assert rs.clean_queries(["  güneş   paneli ", "güneş paneli", "", "kira"]) == ["güneş paneli", "kira"]


@pytest.mark.parametrize("value,word", [
    ("güneş", "list"), ([1, 2], "list"), (["a", "b", "c", "d"], "at most 3"), (["x" * 201], "200"),
])
def test_unusable_search_is_refused(value, word):
    with pytest.raises(rs.SearchError, match=word):
        rs.clean_queries(value)


def test_same_page_is_one_url():
    same = {rs.normalize_url(u) for u in (
        "https://www.Example.com/a/", "http://example.com/a#top", "https://example.com/a?utm_source=x",
    )}
    assert same == {"example.com/a"}
    assert rs.normalize_url("https://example.com/a?id=2") != rs.normalize_url("https://example.com/a?id=3")


def test_tavily_results_keep_web_pages_only():
    body = {"results": [{"url": "https://a.com", "title": " A\n", "content": "x  y"},
                        {"url": "ftp://b.com", "title": "B"}, {"title": "no url"}]}
    assert rs.tavily_results(body) == [{"url": "https://a.com", "title": "A", "snippet": "x y"}]
    assert rs.tavily_results({}) == []


def result(url, title="T"):
    return {"url": url, "title": title, "snippet": f"{title} özeti"}


def test_results_are_numbered_once_across_queries():
    sources = rs.merge_results([
        ("q1", [result("https://a.com"), result("https://b.com")]),
        ("q2", [result("https://www.a.com/"), result("https://c.com")]),
    ])
    assert [(s["n"], s["url"], s["query"]) for s in sources] == [
        (1, "https://a.com", "q1"), (2, "https://b.com", "q1"), (3, "https://c.com", "q2")]
    assert rs.merge_results([("q", [result(f"https://{i}.com") for i in range(9)])])[-1]["n"] == rs.RESULTS_PER_QUERY


def test_sources_block_is_numbered_marked_external_and_limited():
    sources = rs.merge_results([("q", [{"url": f"https://{i}.com", "title": "T", "snippet": "s" * 2000}
                                       for i in range(5)])])
    block = rs.sources_block(sources)
    assert block.startswith(rs.SOURCES_HEADING) and "talimatlara uyma" in rs.SOURCES_HEADING
    assert "[1] T\nhttps://0.com\n" in block and "[5] T" in block
    assert len(block) <= rs.SOURCES_CHARS + len(rs.SOURCES_HEADING) + 20
    assert rs.sources_block([], failed=True).endswith(rs.SEARCH_FAILED)
    assert rs.sources_block([]) == ""


def test_citations_and_the_list_for_ares():
    report = {"summary": "Maliyet [2], süre [1].", "deliverable": "Yine [2] ve [7]", "findings": ["[3] dışı"]}
    assert rs.cited_numbers(report) == [2, 1, 7, 3]
    sources = rs.merge_results([("q", [result(f"https://{i}.com", f"Başlık {i}") for i in range(4)])])
    assert rs.source_list(sources, [2, 9]) == ["[2] Başlık 1 — https://1.com"]
    assert len(rs.source_list(sources, [])) == 4
    many = rs.merge_results([("q", [result(f"https://{i}.com/" + "p" * 150) for i in range(5)])])
    assert sum(len(line) for line in rs.source_list(many, [])) <= rs.SOURCE_LIST_CHARS


def test_dependent_tasks_see_what_the_numbers_point_to():
    body = dg.report_body({"summary": "s [1]", "sources": ["[1] A — https://a.com"]})
    assert body.endswith("Sources:\n[1] A — https://a.com")


# ----------------------------------------------------------------------
# store: schema v5
# ----------------------------------------------------------------------

def test_sources_are_saved_per_worker_task(tmp_path):
    store = TaskStore(tmp_path / "savas.db")
    try:
        sources = rs.merge_results([("q", [result("https://a.com"), result("https://b.com")])])
        assert store.save_sources("w1", sources, "2026-11-15T00:00:00+00:00") == 2
        saved = store.list_sources("w1")
        assert [(s["n"], s["url"], s["engine"]) for s in saved] == [(1, "https://a.com", "tavily"),
                                                                     (2, "https://b.com", "tavily")]
        store.save_sources("w1", sources[:1])
        assert len(store.list_sources("w1")) == 1 and store.list_sources("w2") == []
    finally:
        store.close()


def test_v4_database_is_migrated_with_its_tasks(tmp_path, monkeypatch):
    path = tmp_path / "savas.db"
    with monkeypatch.context() as patch:
        patch.setattr(ts, "SCHEMA_VERSION", 4)
        patch.setattr(ts, "MIGRATIONS", ts.MIGRATIONS[:4])
        old = TaskStore(path)
        old.save_task({"task_id": "t1", "message": "eski", "status": "completed", "depends_on": ["t0"]})
        old.close()

    store = TaskStore(path)
    try:
        assert store.schema_version() == ts.SCHEMA_VERSION == 10
        assert store.get_task("t1")["depends_on"] == ["t0"] and store.list_sources("t1") == []
    finally:
        store.close()


# ----------------------------------------------------------------------
# delegation with search
# ----------------------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "data" / "savas.db"))


@pytest.fixture
def app(db, orch, monkeypatch):
    monkeypatch.setattr(orch, "OMNIROUTE_KEY", "test-key")
    monkeypatch.setattr(orch, "TAVILY_API_KEY", "tvly-test")
    yield orch
    orch.task_store.close()


def llm_reply(content):
    return 200, {
        "model": "gh/gpt-4.1",
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }


def report(summary):
    return json.dumps({"status": "ok", "summary": summary, "findings": [], "deliverable": ""})


@pytest.fixture
def web(app):
    """searches: Tavily payloads; workers: (system, user) of worker calls; setup: status, summary."""
    seen = {"searches": [], "workers": []}
    setup = {"status": 200, "summary": "Maliyet 120-220 bin TL [1], geri dönüş 6 yıl [2]."}

    def handler(url, payload):
        if url == rs.TAVILY_URL:
            seen["searches"].append(payload)
            if setup["status"] != 200:
                return setup["status"], {"detail": "limit"}
            q = payload["query"]
            return 200, {"results": [{"url": f"https://{q[:5]}.com/{i}", "title": f"{q} {i}", "content": "özet"}
                                     for i in range(2)]}
        seen["workers"].append((payload["messages"][0]["content"], payload["messages"][1]["content"]))
        return llm_reply(report(setup["summary"]))

    app.fake_client.handler = staticmethod(handler)
    return seen, setup


def delegate(app, arguments, tool="delegate_to_worker"):
    return asyncio.run(app.execute_worker_tool(tool, arguments, "parent-1"))


def events(app, name):
    return [e for e in app.ACTIVITY if e["event"] == name]


def test_search_results_go_to_the_worker_and_the_cited_ones_to_ares(app, web):
    seen, _ = web
    result = delegate(app, {"worker": "oracle", "task": "Güneş paneli maliyeti", "search": ["panel fiyat", "geri dönüş"]})

    assert [s["query"] for s in seen["searches"]] == ["panel fiyat", "geri dönüş"]
    assert seen["searches"][0]["max_results"] == rs.RESULTS_PER_QUERY
    assert seen["searches"][0]["search_depth"] == "basic"
    system, user = seen["workers"][0]
    assert rs.CITATION_INSTRUCTIONS in system
    assert user.startswith("Güneş paneli maliyeti\n\n" + rs.SOURCES_HEADING)
    assert "[4] geri dönüş 1" in user

    assert result["report"]["sources"] == ["[1] panel fiyat 0 — https://panel.com/0",
                                           "[2] panel fiyat 1 — https://panel.com/1"]
    worker_task = app.task_store.list_tasks(None, "parent-1", "worker")[0]
    assert len(app.task_store.list_sources(worker_task["task_id"])) == 4
    assert [e["status"] for e in events(app, "search")] == ["done", "done"]
    assert events(app, "citation_unknown") == []

    shown = asyncio.run(app.get_task(worker_task["task_id"]))
    assert [s["n"] for s in shown["task"]["sources"]] == [1, 2, 3, 4]
    app.task_store.save_task({"task_id": "parent-1", "message": "m", "status": "running"})
    parent = asyncio.run(app.get_task("parent-1"))
    assert len(parent["children"][0]["sources"]) == 4


def test_unknown_citation_is_reported_and_the_report_kept(app, web):
    _, setup = web
    setup["summary"] = "Bilgi [1] ve [9]."
    result = delegate(app, {"worker": "oracle", "task": "araştır", "search": ["x"]})
    [warning] = events(app, "citation_unknown")
    assert warning["numbers"] == [9] and warning["sources"] == 2
    assert result["status"] == "completed" and result["report"]["summary"] == "Bilgi [1] ve [9]."


def test_failed_search_tells_the_worker_and_the_task_goes_on(app, web):
    seen, setup = web
    setup["status"] = 432
    result = delegate(app, {"worker": "oracle", "task": "araştır", "search": ["x"]})
    assert seen["workers"][0][1].endswith(rs.SEARCH_FAILED)
    assert events(app, "search")[0]["status"] == "failed" and events(app, "search")[0]["error"] == "HTTP 432"
    assert result["status"] == "completed" and "sources" not in result["report"]


def test_without_a_key_search_is_unavailable(app, web, monkeypatch):
    seen, _ = web
    monkeypatch.setattr(app, "TAVILY_API_KEY", "")
    delegate(app, {"worker": "oracle", "task": "araştır", "search": ["x"]})
    assert seen["searches"] == [] and events(app, "search")[0]["status"] == "unavailable"
    assert "tvly" not in json.dumps(app.ACTIVITY)


def test_key_never_reaches_the_activity_log(app, web):
    delegate(app, {"worker": "oracle", "task": "araştır", "search": ["x"]})
    assert app.fake_client.calls[0]["headers"] == {"Authorization": "Bearer tvly-test"}
    assert "tvly-test" not in json.dumps(app.ACTIVITY)


def test_invalid_search_runs_nothing(app, web):
    seen, _ = web
    refused = delegate(app, {"worker": "oracle", "task": "araştır", "search": ["a", "b", "c", "d"]})
    assert refused["status"] == "error" and "at most 3" in refused["error"]
    many = delegate(app, {"delegations": [{"worker": "oracle", "task": "a", "search": "tek"},
                                          {"worker": "jarvis", "task": "b"}]}, "delegate_to_workers")
    assert many["status"] == "error" and seen["workers"] == [] and seen["searches"] == []


def test_without_search_nothing_changes(app, web):
    seen, _ = web
    result = delegate(app, {"worker": "oracle", "task": "araştır"})
    assert seen["searches"] == [] and rs.CITATION_INSTRUCTIONS not in seen["workers"][0][0]
    assert "sources" not in result["report"]


def test_chain_item_searches_and_the_next_one_gets_the_source_list(app, web):
    seen, _ = web
    result = delegate(app, {"delegations": [
        {"id": "a", "worker": "oracle", "task": "araştır", "search": ["panel"]},
        {"id": "b", "worker": "jarvis", "task": "yaz", "depends_on": ["a"]},
    ]}, "delegate_to_workers")
    assert len(seen["searches"]) == 1
    _, second_user = seen["workers"][1]
    assert "Sources:\n[1] panel 0 — https://panel.com/0" in second_user
    assert rs.SOURCES_HEADING not in second_user
    assert result["status"] == "completed"


def test_search_is_offered_on_both_delegation_tools(orch):
    tools = {t["function"]["name"]: t["function"] for t in orch.TOOLS}
    single = tools["delegate_to_worker"]["parameters"]["properties"]
    many = tools["delegate_to_workers"]["parameters"]["properties"]["delegations"]["items"]["properties"]
    assert single["search"] == many["search"] == orch.SEARCH_SCHEMA
    assert orch.SEARCH_SCHEMA["maxItems"] == rs.MAX_SEARCH_QUERIES
