"""F-4: memory retrieval into the D-5 context (plan-f karar G, H)."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import memory
from context_engine import DAILY_HEADING, RETRIEVED_HEADING, RETRIEVED_NOTE, build_context, fit_lines
from task_store import TaskStore

NOW = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)
SET = json.loads((Path(__file__).parent / "data" / "retrieval_tr.json").read_text(encoding="utf-8"))


# ----------------------------------------------------------------------
# query
# ----------------------------------------------------------------------

def test_query_words_drop_stopwords_short_words_digits_and_apostrophe_suffixes():
    assert memory.query_words("Bana Obsidian'daki 12 notu ve İstanbul ofisini göster, notu!") == \
        ["obsidian", "notu", "istanbul", "ofisini", "göster"]


def test_query_words_are_capped():
    assert len(memory.query_words(" ".join(f"kelime{i}" for i in range(30)))) == memory.MAX_QUERY_WORDS


def test_fts_query_uses_four_letter_prefixes():
    assert memory.fts_query("Kombiyi kaç dereceye ayarladım?") == '"komb"* OR "dere"* OR "ayar"*'
    assert memory.fts_query("ne zaman? 17") is None
    assert memory.fts_query("") is None


@pytest.fixture
def store(tmp_path):
    store = TaskStore(tmp_path / "savas.db")
    yield store
    store.close()


def fact(key, value, now=NOW, **extra):
    return {**memory.new_fact({"key": key, "value": value, "importance": 2}, "t0", "ares", now), **extra}


def test_measured_set_still_gives_the_f4_result(store):
    """The numbers in memory.py (QUERY_STEM_CHARS) come from this set."""
    ids = {doc_id: store.save_fact(fact(key, value)) for doc_id, (key, value) in SET["docs"].items()}
    found = relevant = first = 0
    for text, expected in SET["queries"]:
        if not expected:
            continue
        hits = [f["fact_id"] for f in store.search_facts(memory.fts_query(text), ["global"], NOW.isoformat(), 5)]
        wanted = {ids[doc_id] for doc_id in expected}
        relevant += len(wanted)
        found += len(wanted & set(hits))
        first += bool(hits and hits[0] in wanted)

    assert (found, relevant) == (45, 45)
    assert first == 32


@pytest.mark.parametrize("text,key", [
    ("istanbul ofisi", "İstanbul ofisi"),
    ("İSTANBUL", "İstanbul ofisi"),
    ("ışıklar", "Işık otomasyonu"),
    ("IŞIK", "Işık otomasyonu"),
    ("şekersiz kahve", "Kahve"),
])
def test_turkish_letters_match(store, text, key):
    store.save_fact(fact("İstanbul ofisi", "Kadıköy'de"))
    store.save_fact(fact("Işık otomasyonu", "Gün batımında"))
    store.save_fact(fact("Kahve", "Şekersiz filtre"))
    [hit] = store.search_facts(memory.fts_query(text), ["global"], NOW.isoformat(), 5)
    assert hit["key"] == key


def test_search_facts_takes_only_active_unexpired_facts_of_the_scopes(store):
    store.save_fact(fact("Dil", "Türkçe"))
    store.save_fact(fact("Dil", "İngilizce", status="flagged"))
    store.save_fact(fact("Dil", "Almanca", scope="oracle"))
    store.save_fact(fact("Dil", "Python", scope="jarvis"))
    store.save_fact({**fact("Dil", "Latince", now=NOW - timedelta(days=200))})

    values = {f["value"] for f in store.search_facts('"dil"*', ["global", "jarvis"], NOW.isoformat(), 5)}
    assert values == {"Türkçe", "Python"}
    assert len(store.search_facts('"dil"*', ["global", "jarvis"], NOW.isoformat(), 1)) == 1


def episode(task_id, title, now=NOW, answer="cevap"):
    task = {"task_id": task_id, "status": "completed", "message": title, "result": {"response": answer}}
    return memory.build_episode(task, now=now)


def test_search_episodes_skips_the_current_task_and_expired_ones(store):
    store.save_episode(episode("t1", "Docker loglarını incele"))
    store.save_episode(episode("t2", "Docker imajlarını temizle"))
    store.save_episode(episode("t3", "Docker eski görev", now=NOW - timedelta(days=200)))

    found = [e["task_id"] for e in store.search_episodes('"dock"*', NOW.isoformat(), 5, "t2")]
    assert found == ["t1"]
    assert {e["task_id"] for e in store.search_episodes('"dock"*', NOW.isoformat(), 5)} == {"t1", "t2"}


# ----------------------------------------------------------------------
# block
# ----------------------------------------------------------------------

def test_lines_show_source_and_date():
    [fact_line, episode_line] = memory.retrieved_lines(
        [fact("Dil", "Türkçe")], [episode("t1", "Rapor hazırla", answer="Rapor yazıldı.")])
    assert fact_line == "- Dil: Türkçe (ares, 2026-11-15)"
    assert episode_line == "- Görev (2026-11-15, completed): Rapor hazırla → Rapor yazıldı."


def test_long_lines_are_clipped():
    [line] = memory.retrieved_lines([fact("Not", "uzun " * 100)])
    assert len(line) <= memory.RETRIEVED_LINE_CHARS


def test_fit_lines_keeps_whole_lines_in_order():
    assert fit_lines(["aaa", "bbb", "ccc"], 7) == "aaa\nbbb"
    assert fit_lines(["aaaa"], 3) == ""


IDENTITY = "# Ares\n\n## Rules\n\nKurallar.\n"
DAILY = "## Current State\n\n" + "durum " * 200


def test_block_sits_between_identity_and_daily():
    context = build_context(IDENTITY, DAILY, 6000, 100, retrieved=["- Dil: Türkçe (ares, 2026-11-15)"],
                            retrieved_chars=1200)
    assert context.index("Kurallar.") < context.index(RETRIEVED_HEADING) < context.index(DAILY_HEADING)
    assert f"{RETRIEVED_NOTE}\n- Dil: Türkçe" in context


def test_block_stays_within_its_limit_with_whole_lines():
    lines = [f"- satır {i} " + "x" * 280 for i in range(10)]
    context = build_context(IDENTITY, None, 6000, 0, retrieved=lines, retrieved_chars=1200)
    block = context.split(RETRIEVED_HEADING)[1]
    assert len(block) <= 1200 and block.count("- satır") == 3 and block.endswith("x")


def test_block_comes_before_the_daily_memory_when_room_is_short():
    lines = ["- " + "b" * 500]
    without = build_context(IDENTITY, DAILY, 1500, 200)
    with_block = build_context(IDENTITY, DAILY, 1500, 200, retrieved=lines, retrieved_chars=800)
    assert "b" * 500 in with_block
    assert len(with_block) <= 1500 - 200 and len(with_block) - len(without) < len(lines[0]) + 100
    assert DAILY_HEADING in with_block


def test_no_block_without_lines_or_room():
    assert RETRIEVED_HEADING not in build_context(IDENTITY, None, 6000, 0, retrieved=[], retrieved_chars=1200)
    assert RETRIEVED_HEADING not in build_context(IDENTITY, None, 6000, 0, retrieved=["- " + "x" * 900],
                                                  retrieved_chars=800)
    assert RETRIEVED_HEADING not in build_context(IDENTITY, None, 100, 50, retrieved=["- kısa"],
                                                  retrieved_chars=800)


def test_empty_memory_changes_nothing():
    assert build_context(IDENTITY, DAILY, 6000, 100) == build_context(IDENTITY, DAILY, 6000, 100, retrieved=[],
                                                                      retrieved_chars=1200)


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


REPORT = json.dumps({"status": "done", "summary": "özet", "findings": [], "artifacts": [],
                     "open_questions": [], "deliverable": ""})


def delegate(*pairs):
    return {"id": "call_d", "type": "function", "function": {"name": "delegate_to_workers", "arguments": json.dumps(
        {"delegations": [{"worker": w, "task": t} for w, t in pairs]})}}


def run(orch, message, *replies):
    """Run one task; returns {"ares": system prompt, worker: system prompt}."""
    queue = list(replies)
    prompts = {}

    def handler(url, payload):
        system = payload["messages"][0]["content"]
        if "tools" not in payload:
            prompts[payload["messages"][1]["content"]] = system
            return reply(REPORT)
        prompts.setdefault("ares", system)
        return queue.pop(0) if queue else reply("tamam")

    orch.fake_client.handler = staticmethod(handler)
    asyncio.run(orch.create_task_endpoint(orch.TaskRequest(message=message)))
    return prompts


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "data" / "savas.db"))
    yield None


@pytest.fixture
def orch_db(app, orch, monkeypatch):
    monkeypatch.setattr(orch, "OMNIROUTE_KEY", "test-key")
    yield orch
    orch.task_store.close()


def remember_fact(orch, key, value, scope="global"):
    orch.task_store.save_fact(memory.new_fact({"key": key, "value": value, "importance": 2, "scope": scope},
                                              "t0", "ares"))


def test_ares_gets_matching_facts_and_episodes(orch_db):
    remember_fact(orch_db, "Kombi ayarı", "Kışın 21 derece")
    run(orch_db, "Kombi bakımını planla")

    prompt = run(orch_db, "Kombinin bakımı ne zaman?")["ares"]

    block = prompt.split(RETRIEVED_HEADING)[1]
    assert "- Kombi ayarı: Kışın 21 derece (ares," in block
    assert "- Görev (" in block and "Kombi bakımını planla" in block
    [event] = [e for e in orch_db.ACTIVITY if e["status"] == "retrieved"][-1:]
    assert event["agent"] == "ares" and len(event["facts"]) == 1 and len(event["episodes"]) == 1


def test_no_match_no_block_and_no_event(orch_db):
    remember_fact(orch_db, "Kombi ayarı", "Kışın 21 derece")
    prompt = run(orch_db, "Bana bir fıkra anlat")["ares"]
    assert RETRIEVED_HEADING not in prompt
    assert not [e for e in orch_db.ACTIVITY if e.get("status") == "retrieved"]


def test_workers_get_their_scope_and_global_facts_but_no_episodes(orch_db):
    remember_fact(orch_db, "Kod stili", "Kısa fonksiyonlar", scope="jarvis")
    remember_fact(orch_db, "Kod incelemesi", "Türkçe yorum", scope="global")
    remember_fact(orch_db, "Kod kaynakları", "Resmi dokümanlar", scope="oracle")
    run(orch_db, "Kod incelemesi yap")

    prompts = run(orch_db, "Kodu incele", reply(tool_calls=[delegate(("jarvis", "Kod stilini uygula"),
                                                                      ("oracle", "Kod kaynaklarını bul"))]))

    jarvis, oracle = prompts["Kod stilini uygula"], prompts["Kod kaynaklarını bul"]
    assert "Kısa fonksiyonlar" in jarvis and "Türkçe yorum" in jarvis and "Resmi dokümanlar" not in jarvis
    assert "Resmi dokümanlar" in oracle and "Kısa fonksiyonlar" not in oracle
    assert "- Görev (" not in jarvis + oracle
    assert len(jarvis.split(RETRIEVED_HEADING)[1].split("\n\n#")[0]) <= memory.WORKER_RETRIEVAL_CHARS


def test_ares_block_stays_within_its_limit(orch_db):
    for i in range(8):
        remember_fact(orch_db, f"Kombi notu {i}", "uzun açıklama " * 30)
    block = run(orch_db, "Kombi")["ares"].split(RETRIEVED_HEADING)[1].split("\n\n#")[0]
    assert len(block) <= memory.ARES_RETRIEVAL_CHARS
    assert block.count("- Kombi notu") <= memory.MAX_RETRIEVED_FACTS


def test_without_a_store_there_is_no_block(orch, monkeypatch):
    monkeypatch.setattr(orch, "OMNIROUTE_KEY", "test-key")
    assert orch.retrieve_memory("t1", "Kombi bakımı", "ares", with_episodes=True) == []
    assert RETRIEVED_HEADING not in run(orch, "Kombi bakımı")["ares"]


def test_empty_memory_leaves_the_prompt_as_before(orch_db, orch, monkeypatch):
    """F-6 target: no extra load while memory is empty."""
    prompt = run(orch_db, "Kombi bakımı")["ares"]
    assert RETRIEVED_HEADING not in prompt
