"""F-7: fixes for the F-6 findings (plan-f, F-7 uygulama planı)."""

import asyncio
import json
from pathlib import Path

import pytest

import memory
from context_engine import ARES_RETRIEVED_NOTE, RETRIEVED_HEADING, RETRIEVED_NOTE, build_context
from task_store import TaskStore

DATA = Path(__file__).parent / "data"
EPISODES = json.loads((DATA / "episodes_f6.json").read_text(encoding="utf-8"))["episodes"]


# ----------------------------------------------------------------------
# finding 1: no "saved" without the tool
# ----------------------------------------------------------------------

def test_prompt_says_to_claim_a_save_only_after_the_tool(orch):
    assert "kaydettiğini yalnızca remember aracını çağırıp sonucunu aldıysan söyle" in orch.SYSTEM_PROMPT
    assert "çelişki sonucu gelirse bunu kullanıcıya bildir" in orch.SYSTEM_PROMPT


def test_ares_block_note_says_the_block_is_read_only():
    context = build_context("# Ares\n", None, 6000, 0, retrieved=["- Dil: Türkçe (ares, 2026-11-15)"],
                            retrieved_chars=1200, retrieved_note=ARES_RETRIEVED_NOTE)
    assert f"{ARES_RETRIEVED_NOTE}\n- Dil: Türkçe" in context
    assert "remember çağır" in ARES_RETRIEVED_NOTE and "remember" not in RETRIEVED_NOTE


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
DELEGATE = {"id": "call_d", "type": "function", "function": {"name": "delegate_to_worker", "arguments": json.dumps(
    {"worker": "jarvis", "task": "Rapor biçimini uygula"})}}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "data" / "savas.db"))


@pytest.fixture
def app(db, orch, monkeypatch):
    monkeypatch.setattr(orch, "OMNIROUTE_KEY", "test-key")
    yield orch
    orch.task_store.close()


def test_ares_gets_the_read_only_note_and_workers_the_plain_one(app):
    app.task_store.save_fact(memory.new_fact({"key": "Rapor biçimi", "value": "GG.AA.YYYY"}, "t0", "ares"))
    prompts = {}
    queue = [reply(tool_calls=[DELEGATE])]

    def handler(url, payload):
        system = payload["messages"][0]["content"]
        if "tools" not in payload:
            prompts["jarvis"] = system
            return reply(REPORT)
        prompts.setdefault("ares", system)
        return queue.pop(0) if queue else reply("tamam")

    app.fake_client.handler = staticmethod(handler)
    asyncio.run(app.create_task_endpoint(app.TaskRequest(message="Rapor biçimi nasıl olmalı?")))

    assert f"{RETRIEVED_HEADING}{ARES_RETRIEVED_NOTE}\n- Rapor biçimi" in prompts["ares"]
    worker_block = prompts["jarvis"].split(RETRIEVED_HEADING)[1]
    assert worker_block.startswith(RETRIEVED_NOTE + "\n") and "remember" not in worker_block.split("\n\n#")[0]


# ----------------------------------------------------------------------
# finding 2: the answer stays in the episode line
# ----------------------------------------------------------------------

def episode(title, answer="Kısa cevap.", status="completed"):
    task = {"task_id": "t1", "status": status, "message": title, "result": {"response": answer} if answer else {}}
    return memory.build_episode(task)


def test_long_title_leaves_room_for_the_answer():
    line = memory.episode_line(episode(
        "Araç kullanmadan ve kimseye delege etmeden kendin yanıtla: Bir REST API'de 401 ile 403 durum "
        "kodlarının farkını üç cümleyle açıkla.",
        "401 kimlik doğrulaması yok demektir. 403 yetki yok demektir. " * 5))
    assert " → 401 kimlik doğrulaması yok demektir." in line
    assert len(line) <= memory.RETRIEVED_LINE_CHARS


@pytest.mark.parametrize("data", EPISODES[:3], ids=["a", "b", "c"])
def test_f6_benchmark_episodes_now_carry_their_answers(data):
    line = memory.episode_line(data)
    assert " → " in line and len(line) <= memory.RETRIEVED_LINE_CHARS


def test_episode_without_an_answer_is_title_only():
    line = memory.episode_line(episode("Docker loglarını incele", answer=None, status="failed"))
    assert line.endswith("): Docker loglarını incele")


def test_short_episode_line_is_unchanged():
    assert memory.episode_line(episode("Rapor hazırla", "Rapor yazıldı.")).endswith(
        "Rapor hazırla → Rapor yazıldı.")


# ----------------------------------------------------------------------
# finding 3: command words do not match
# ----------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Araç kullanmadan ve kimseye delege etmeden kendin yanıtla: REST API", ["rest", "apı"]),
    ("Şu soruyu delegate_to_workers ile üç worker'a paralel delege et", ["soruyu", "üç", "paralel"]),
    ("Bunu hatırla: raporlarda tarih biçimi", ["raporlarda", "tarih", "biçimi"]),
    ("Bu görevi JARVIS'e delege et", ["jarvıs"]),
])
def test_command_words_and_underscores(text, expected):
    assert [w for w in memory.query_words(text) if len(w) >= 3] == [w for w in expected if len(w) >= 3]


def test_f6_episodes_match_less_and_keep_the_relevant_ones(tmp_path):
    """Measured in F-7: 8 matches before, 5 after; the date-format tasks still find each other."""
    store = TaskStore(tmp_path / "savas.db")
    try:
        for data in EPISODES:
            store.save_episode(dict(data))
        found = {}
        for data in EPISODES:
            earlier = [e["task_id"] for e in store.search_episodes(memory.fts_query(data["title"]), "2026-11-15", 3,
                                                                    data["task_id"])
                       if e["created_at"] < data["created_at"]]
            found[data["task_id"]] = earlier
    finally:
        store.close()

    ids = [e["task_id"] for e in EPISODES]
    assert sum(len(v) for v in found.values()) == 5
    assert found[ids[0]] == [] and found[ids[1]] == []
    assert ids[3] in found[ids[4]]
    assert ids[3] in found[ids[5]]


def test_folded_capital_i_still_reaches_the_index():
    # Turkish folding gives "ı" for "I"; fts_query turns it into the index's "i".
    assert memory.fts_query("Bu görevi JARVIS'e delege et") == '"jarv"*'


def test_remember_asks_for_the_users_language_and_the_same_key(orch):
    """F-7 live check: the model saved weekly_report_day = Friday, then a Turkish key for the same topic."""
    tool = next(t["function"] for t in orch.TOOLS if t["function"]["name"] == "remember")
    assert "in the user's language" in tool["description"]
    assert "reuse its key" in tool["description"]


# ----------------------------------------------------------------------
# G-4: an old episode of the same task must not stand in for the work
# ----------------------------------------------------------------------

SCENARIO_1 = ("Önce Oracle, Türkiye'de bir ev için güneş paneli kurulumunun yaklaşık maliyetini ve geri dönüş "
              "süresini kısaca araştırsın; ardından Willy, Oracle'ın bulgularına dayanarak 120 kelimelik bir blog "
              "paragrafı yazsın. Vault'ta not arama veya not yazma. Willy'nin paragrafını bana ilet.")


def test_rules_say_old_episodes_are_records_and_workers_ran_only_when_delegated(orch):
    assert "eski sonucu yeni sonuç gibi sunma" in ARES_RETRIEVED_NOTE
    assert "eski sonucu" not in RETRIEVED_NOTE
    assert "worker'ın çalıştığını yalnızca bu görevde delege edip sonucunu aldıysan söyle" in orch.SYSTEM_PROMPT


def test_same_message_is_a_near_duplicate_even_when_the_title_is_clipped():
    title = memory.build_episode({"task_id": "t", "status": "completed", "message": SCENARIO_1,
                                  "result": {"response": "x"}})["title"]
    assert memory.near_duplicate(title, SCENARIO_1)
    assert memory.near_duplicate(title, SCENARIO_1.replace("Önce ", "").replace("kısaca ", ""))


@pytest.mark.parametrize("title,text", [
    ("Güneş paneli maliyetini araştır", SCENARIO_1),
    ("Rapor tarih biçimi ne olmalı?", "Haftalık rapor hangi gün gönderilecek?"),
    ("", SCENARIO_1),
])
def test_related_or_empty_titles_are_not_duplicates(title, text):
    assert not memory.near_duplicate(title, text)


def test_f6_benchmark_tasks_skip_their_own_earlier_runs(tmp_path):
    """G-4: the benchmark repeats F-6's messages; each found its own old run."""
    store = TaskStore(tmp_path / "savas.db")
    try:
        for data in EPISODES:
            store.save_episode(dict(data))
        for data in EPISODES:
            found = store.search_episodes(memory.fts_query(data["title"]), "2026-11-15", 6)
            kept = [e for e in found if not memory.near_duplicate(e["title"], data["title"])]
            assert data["task_id"] in [e["task_id"] for e in found]
            assert data["task_id"] not in [e["task_id"] for e in kept]
    finally:
        store.close()


def test_retrieval_drops_the_duplicate_and_fills_up_to_three(app):
    store = app.task_store
    for index, message in enumerate([SCENARIO_1, "Güneş paneli maliyeti araştırması",
                                     "Güneş paneli geri dönüş süresi", "Blog paragrafı: güneş paneli"]):
        store.save_episode(memory.build_episode({"task_id": f"old-{index}", "status": "completed",
                                                 "message": message, "result": {"response": "eski"}}))
    lines = app.retrieve_memory("new-1", SCENARIO_1, "ares", with_episodes=True)
    text = "\n".join(lines)
    assert "Önce Oracle" not in text and "eski" in text
    [event] = [e for e in app.ACTIVITY if e["event"] == "memory" and e.get("status") == "retrieved" and e.get("task_id") == "new-1"]
    assert event["duplicates_skipped"] == 1 and len(event["episodes"]) == 3


def test_current_facts_go_to_a_web_search(orch):
    # 2026-11-16: Ares answered a CVE question from its training data and called it nonexistent.
    assert "oracle'a search sorgularıyla delege et" in orch.SYSTEM_PROMPT
    assert "bir şeyin olmadığını ondan çıkarma" in orch.SYSTEM_PROMPT
