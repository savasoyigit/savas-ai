"""F-5: conflicting facts are flagged and resolved by the user (plan-f karar J)."""

import asyncio
from datetime import datetime, timezone

import pytest

import memory
import task_store as ts
from task_store import TaskStore

NOW = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)


def fact(value, key="Dil", scope="global", status="active", now=NOW, importance=2):
    return {**memory.new_fact({"key": key, "value": value, "importance": importance, "scope": scope},
                              "t0", "ares", now), "status": status}


# ----------------------------------------------------------------------
# task_store.py
# ----------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    store = TaskStore(tmp_path / "savas.db")
    yield store
    store.close()


def statuses(store):
    return {f["value"]: f["status"] for f in store.list_facts()}


def test_use_new_activates_the_flagged_fact_and_supersedes_the_old(store):
    old = store.save_fact(fact("Türkçe"))
    new = store.save_fact(fact("İngilizce", status="flagged"))

    assert store.resolve_fact(new, True, NOW.isoformat()) == {"fact_id": new, "status": "active", "superseded": [old]}
    assert statuses(store) == {"Türkçe": "superseded", "İngilizce": "active"}


def test_keep_old_supersedes_the_flagged_fact(store):
    store.save_fact(fact("Türkçe"))
    new = store.save_fact(fact("İngilizce", status="flagged"))

    assert store.resolve_fact(new, False, NOW.isoformat())["superseded"] == [new]
    assert statuses(store) == {"Türkçe": "active", "İngilizce": "superseded"}


def test_use_new_without_an_active_fact_only_activates(store):
    store.save_fact(fact("Türkçe", status="deleted"))
    new = store.save_fact(fact("İngilizce", status="flagged"))
    assert store.resolve_fact(new, True, NOW.isoformat())["superseded"] == []
    assert statuses(store)["İngilizce"] == "active"


def test_only_the_same_key_and_scope_is_touched(store):
    other_scope = store.save_fact(fact("Python", scope="jarvis"))
    other_key = store.save_fact(fact("Pazartesi", key="Toplantı"))
    store.save_fact(fact("Türkçe"))
    second = store.save_fact(fact("Almanca", status="flagged"))
    new = store.save_fact(fact("İngilizce", status="flagged"))

    store.resolve_fact(new, True, NOW.isoformat())

    assert store.get_fact(other_scope)["status"] == "active" and store.get_fact(other_key)["status"] == "active"
    assert store.get_fact(second)["status"] == "flagged"


@pytest.mark.parametrize("status", ["active", "superseded", "deleted"])
def test_only_flagged_facts_can_be_resolved(store, status):
    fact_id = store.save_fact(fact("Türkçe", status=status))
    with pytest.raises(ts.StoreError):
        store.resolve_fact(fact_id, True, NOW.isoformat())
    assert store.get_fact(fact_id)["status"] == status


def test_missing_fact_cannot_be_resolved(store):
    with pytest.raises(ts.StoreError):
        store.resolve_fact(999, True, NOW.isoformat())


def test_resolve_is_one_transaction(store, monkeypatch):
    old = store.save_fact(fact("Türkçe"))
    new = store.save_fact(fact("İngilizce", status="flagged"))
    real = store.set_fact_status

    def fail_second(fact_id, status):
        if fact_id == new:
            raise RuntimeError("disk")
        return real(fact_id, status)

    monkeypatch.setattr(store, "set_fact_status", fail_second)
    with pytest.raises(RuntimeError):
        store.resolve_fact(new, True, NOW.isoformat())

    assert store.get_fact(old)["status"] == "active" and store.get_fact(new)["status"] == "flagged"


def test_find_flagged_facts(store):
    store.save_fact(fact("Türkçe"))
    a = store.save_fact(fact("İngilizce", status="flagged"))
    store.save_fact(fact("Almanca", status="flagged", scope="oracle"))
    assert [f["fact_id"] for f in store.find_flagged_facts("dil", "global")] == [a]


def test_flagged_and_superseded_facts_are_not_retrieved(store):
    store.save_fact(fact("Türkçe"))
    new = store.save_fact(fact("İngilizce", status="flagged"))
    found = lambda: [f["value"] for f in store.search_facts('"dil"*', ["global"], NOW.isoformat(), 5)]

    assert found() == ["Türkçe"]
    store.resolve_fact(new, True, NOW.isoformat())
    assert found() == ["İngilizce"]


# ----------------------------------------------------------------------
# main.py
# ----------------------------------------------------------------------

@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "data" / "savas.db"))


@pytest.fixture
def db(app, orch):
    yield orch
    orch.task_store.close()


def remember(orch, value, key="Dil", task_id="t1", **extra):
    return orch.execute_memory_tool({"key": key, "value": value, "importance": 2, **extra}, task_id)


def resolve(orch, fact_id, choice):
    return asyncio.run(orch.resolve_fact_endpoint(fact_id, orch.ResolveFactRequest(choice=choice)))


def test_conflict_is_flagged_and_marked_to_the_model(db):
    old = remember(db, "Türkçe")["fact_id"]
    result = remember(db, "İngilizce")

    assert result["status"] == "conflict" and result["existing"] == {"fact_id": old, "value": "Türkçe"}
    assert db.task_store.get_fact(result["fact_id"])["status"] == "flagged"
    assert db.task_store.get_fact(old)["status"] == "active"
    [event] = [e for e in db.ACTIVITY if e["event"] == "memory" and e["status"] == "conflict"]
    assert event["fact_id"] == result["fact_id"] and event["key"] == "Dil"


def test_the_same_conflicting_value_again_is_not_a_second_flag(db):
    remember(db, "Türkçe")
    first = remember(db, "İngilizce")
    again = remember(db, " ingilizce ", task_id="t2")

    assert again["status"] == "conflict" and again["fact_id"] == first["fact_id"]
    assert len(db.task_store.find_flagged_facts("dil", "global")) == 1
    assert db.task_store.get_fact(first["fact_id"])["confirmations"] == 2


def test_another_conflicting_value_is_flagged_separately(db):
    remember(db, "Türkçe")
    remember(db, "İngilizce")
    remember(db, "Almanca")
    assert len(db.task_store.find_flagged_facts("dil", "global")) == 2


def test_resolve_use_new(db):
    old = remember(db, "Türkçe")["fact_id"]
    new = remember(db, "İngilizce")["fact_id"]

    assert resolve(db, new, "use_new") == {"fact_id": new, "status": "active", "superseded": [old],
                                           "choice": "use_new"}
    assert remember(db, "İngilizce", task_id="t3")["status"] == "confirmed"
    [event] = [e for e in db.ACTIVITY if e["event"] == "memory" and e["status"] == "resolved"]
    assert (event["fact_id"], event["choice"], event["superseded"]) == (new, "use_new", [old])


def test_resolve_keep_old(db):
    old = remember(db, "Türkçe")["fact_id"]
    new = remember(db, "İngilizce")["fact_id"]

    assert resolve(db, new, "keep_old")["superseded"] == [new]
    assert db.task_store.get_fact(old)["status"] == "active"
    listed = asyncio.run(db.list_facts(status="flagged"))
    assert listed["count"] == 0


def test_resolve_refusals(db):
    old = remember(db, "Türkçe")["fact_id"]

    for fact_id, choice, code in ((old, "both", 422), (999, "use_new", 404), (old, "use_new", 409)):
        with pytest.raises(db.HTTPException) as refused:
            resolve(db, fact_id, choice)
        assert refused.value.status_code == code
    assert db.task_store.get_fact(old)["status"] == "active"


def test_resolve_without_a_store_is_503(orch):
    with pytest.raises(orch.HTTPException) as refused:
        resolve(orch, 1, "use_new")
    assert refused.value.status_code == 503


def test_resolve_endpoint_is_async(orch):
    import inspect
    assert inspect.iscoroutinefunction(orch.resolve_fact_endpoint)


def test_nothing_is_corrected_automatically(db):
    """Karar J: a conflict never changes the active fact by itself."""
    old = remember(db, "Türkçe")["fact_id"]
    for value in ("İngilizce", "Almanca", "İngilizce"):
        remember(db, value)
    assert [f["fact_id"] for f in db.task_store.list_facts(status="active")] == [old]
