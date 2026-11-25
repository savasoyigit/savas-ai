import copy

import pytest

from model_router import (
    DEFAULT_REGISTRY_PATH,
    Selection,
    load_registry,
    parse_registry,
    select,
)

REPO = load_registry(DEFAULT_REGISTRY_PATH)
TASK_TYPES = sorted(REPO.task_types)
LOCAL_MODEL = "gpt-oss-20b-Q4_K_M"


def model(id, caps=(), enabled=True, verified="2026-11-13", fallback=None, location="cloud"):
    return {"id": id, "provider": "p", "location": location, "capabilities": list(caps),
            "enabled": enabled, "max_output": 100, "context": None,
            "fallback": fallback, "verified": verified, "notes": ""}


BASE = {
    "models": [
        model("m/tool", ["tool_calling"], fallback="m/tool2"),
        model("m/tool2", ["tool_calling"]),
        model("m/b", fallback="m/a"),
        model("m/a"),
        model("m/unverified", ["tool_calling"], verified=None),
        model("m/off", ["tool_calling"], enabled=False),
    ],
    "task_types": {
        "orchestrate": {"requires": ["tool_calling"], "prefer": ["m/off", "m/unverified", "m/tool"]},
        "general": {"requires": [], "prefer": ["m/b"]},
        "plain": {"requires": [], "prefer": []},
    },
    "workers": {
        "ares": {"default": "orchestrate", "allowed": ["orchestrate"]},
        "w": {"default": "general", "allowed": ["general", "plain"]},
    },
    "default_model": "m/a",
}


def reg(mutate=None):
    data = copy.deepcopy(BASE)
    if mutate:
        mutate(data)
    return parse_registry(data)


# --- repo config: every worker x task_type -------------------------------

def repo_expected(agent, task_type, allow_local):
    route = REPO.workers[agent]
    used = task_type if task_type in route.allowed else route.default
    requires = set(REPO.task_types[used].requires)
    # gh/gpt-4o: tool calling verified (C-7), "coding" not granted, so it
    # backs up every task type except coding.
    fallbacks = ("gh/gpt-4o",) if requires <= {"tool_calling"} else ()
    # The local model (tool_calling + coding, C-8.1) closes every chain.
    if allow_local:
        fallbacks += (LOCAL_MODEL,)
    return used, "gh/gpt-4.1", fallbacks


# allow_local=False is what main.py passes while LOCAL_LLM_URL is unset.
@pytest.mark.parametrize("allow_local", [True, False])
@pytest.mark.parametrize("agent", sorted(REPO.workers))
@pytest.mark.parametrize("task_type", TASK_TYPES + [None])
def test_repo_matrix(agent, task_type, allow_local):
    used, expected_model, expected_fallbacks = repo_expected(agent, task_type, allow_local)
    sel = select(agent, task_type, registry=REPO, allow_local=allow_local)
    assert isinstance(sel, Selection)
    assert sel.model == expected_model
    assert sel.fallbacks == expected_fallbacks
    assert sel.local == (frozenset({LOCAL_MODEL}) if allow_local else frozenset())
    assert sel.reason.startswith(f"{agent}/{used}:")


@pytest.mark.parametrize("allow_local, fallbacks", [(True, (LOCAL_MODEL,)), (False, ())])
def test_repo_ares_moves_to_gpt_4o_when_primary_is_down(allow_local, fallbacks):
    sel = select("ares", "orchestrate", registry=REPO, unavailable={"gh/gpt-4.1"}, env_model="env-model",
                 allow_local=allow_local)
    assert sel.model == "gh/gpt-4o" and sel.fallbacks == fallbacks


def test_repo_coding_has_no_cloud_backup_model():
    # gpt-4.1 down: gpt-4o lacks "coding" -> env MODEL, not gpt-4o
    sel = select("jarvis", "coding", registry=REPO, unavailable={"gh/gpt-4.1"}, env_model="env-model",
                 allow_local=False)
    assert sel.model == "env-model" and "env MODEL" in sel.reason


def test_repo_coding_backup_is_the_local_model():
    sel = select("jarvis", "coding", registry=REPO, unavailable={"gh/gpt-4.1"}, env_model="env-model")
    assert sel.model == LOCAL_MODEL and sel.fallbacks == ()
    assert "local" in sel.reason


# --- rules on a synthetic registry ---------------------------------------

@pytest.mark.parametrize("agent, task_type, unavailable, model, fallbacks, reason_part", [
    # prefer order; disabled and unverified entries are skipped
    ("ares", None, set(), "m/tool", ("m/tool2",), "prefer[2]"),
    # primary unavailable -> next candidate; its own fallback is used
    ("ares", None, {"m/tool"}, "m/tool2", (), "alfabetik"),
    # fallback unavailable -> no fallback offered
    ("ares", None, {"m/tool2"}, "m/tool", (), "prefer[2]"),
    # worker default task type
    ("w", None, set(), "m/b", ("m/a",), "prefer[0]"),
    # allowed explicit task type with empty prefer -> alphabetical
    ("w", "plain", set(), "m/a", (), "alfabetik"),
    # not-allowed task type falls back to the worker default
    ("w", "orchestrate", set(), "m/b", ("m/a",), "izinli değil"),
    # unknown task type also falls back to default
    ("w", "nope", set(), "m/b", ("m/a",), "izinli değil"),
])
def test_rules(agent, task_type, unavailable, model, fallbacks, reason_part):
    sel = select(agent, task_type, registry=reg(), unavailable=unavailable)
    assert (sel.model, sel.fallbacks) == (model, fallbacks)
    assert reason_part in sel.reason


def test_default_model_when_requirements_unmet():
    r = reg(lambda d: d["task_types"]["general"].update(requires=["vision"]))
    sel = select("w", None, registry=r)
    assert sel.model == "m/a" and "default_model" in sel.reason and sel.fallbacks == ()


def test_default_model_not_used_for_ares_without_tool_calling():
    sel = select("ares", None, registry=reg(), unavailable={"m/tool", "m/tool2"}, env_model="env-model")
    assert sel.model == "env-model" and "env MODEL" in sel.reason


def test_all_unavailable_falls_back_to_env():
    sel = select("w", None, registry=reg(), unavailable={"m/a", "m/b", "m/tool", "m/tool2"}, env_model="env-model")
    assert sel == Selection("env-model", "w/general: aday yok, default_model kullanılamaz → env MODEL", ())


def test_unknown_agent_uses_general_or_default():
    sel = select("stranger", None, registry=reg())
    assert sel.model == "m/b" and "registry'de yok" in sel.reason


def test_config_error_falls_back_to_env(tmp_path):
    bad = tmp_path / "models.json"
    bad.write_text("{bad")
    sel = select("jarvis", "coding", registry_path=bad, env_model="env-model")
    assert sel.model == "env-model" and sel.fallbacks == ()
    assert "config hatası" in sel.reason


def test_deterministic():
    r = reg()
    results = {select("w", "plain", registry=r) for _ in range(20)}
    assert len(results) == 1
