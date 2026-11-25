import copy
import json
from pathlib import Path

import pytest

from model_router import DEFAULT_REGISTRY_PATH, RegistryError, load_registry

VALID = {
    "models": [
        {"id": "gh/a", "provider": "github-copilot", "location": "cloud",
         "capabilities": ["tool_calling"], "enabled": True, "max_output": 4096,
         "context": None, "fallback": "gh/b", "verified": "2026-11-13", "notes": ""},
        {"id": "gh/b", "provider": "github-copilot", "location": "cloud",
         "capabilities": [], "enabled": True, "max_output": 4096,
         "context": 128000, "fallback": None, "verified": None, "notes": "x"},
    ],
    "task_types": {
        "orchestrate": {"requires": ["tool_calling"], "prefer": ["gh/a"]},
        "general": {"requires": [], "prefer": []},
    },
    "workers": {
        "ares": {"default": "orchestrate", "allowed": ["orchestrate"]},
        "jarvis": {"default": "general", "allowed": ["general"]},
    },
    "default_model": "gh/a",
}


def write(tmp_path, data):
    path = tmp_path / "models.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def broken(mutate):
    data = copy.deepcopy(VALID)
    mutate(data)
    return data


def test_valid_registry_loads(tmp_path):
    reg = load_registry(write(tmp_path, VALID))
    assert [m.id for m in reg.models] == ["gh/a", "gh/b"]
    assert reg.model("gh/a").fallback == "gh/b"
    assert reg.model("gh/b").verified is None
    assert reg.task_types["orchestrate"].requires == ("tool_calling",)
    assert reg.workers["ares"].default == "orchestrate"
    assert reg.default_model == "gh/a"


def test_repo_config_is_valid():
    reg = load_registry(DEFAULT_REGISTRY_PATH)
    assert reg.model(reg.default_model).enabled


def test_missing_file_has_clear_error(tmp_path):
    with pytest.raises(RegistryError, match="bulunamadı"):
        load_registry(tmp_path / "yok.json")


def test_invalid_json_has_line_number(tmp_path):
    with pytest.raises(RegistryError, match="satır 1"):
        load_registry(write(tmp_path, "{bad"))


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d["models"].append(copy.deepcopy(d["models"][0])), "tekrar eden id"),
    (lambda d: d["models"][0]["capabilities"].append("magic"), "bilinmeyen capability"),
    (lambda d: d["models"][0].update(location="mars"), "location"),
    (lambda d: d["models"][0].update(fallback="gh/none"), "fallback"),
    (lambda d: d["models"][0].update(fallback="gh/a"), "kendisi"),
    (lambda d: d["models"][1].update(enabled=False), "fallback"),
    (lambda d: d["models"][0].update(verified="dün"), "verified"),
    (lambda d: d["models"][0].update(max_output=0), "max_output"),
    (lambda d: d["models"][0].update(extra=1), "bilinmeyen alan"),
    (lambda d: d["models"][0].pop("enabled"), "eksik alan"),
    (lambda d: d.update(colour="red"), "bilinmeyen alan"),
    (lambda d: d["task_types"]["orchestrate"].update(requires=[]), "tool_calling"),
    (lambda d: d["task_types"]["general"].update(prefer=["gh/none"]), "prefer"),
    (lambda d: d["workers"]["jarvis"].update(default="coding"), "default"),
    (lambda d: d["workers"]["jarvis"].update(allowed=["nope"]), "allowed"),
    (lambda d: d["workers"]["ares"].update(default="general", allowed=["general"]), "ares"),
    (lambda d: d.update(default_model="gh/none"), "default_model"),
])
def test_invalid_registry_is_rejected(tmp_path, mutate, message):
    with pytest.raises(RegistryError, match=message):
        load_registry(write(tmp_path, broken(mutate)))


def test_all_errors_reported_together(tmp_path):
    data = broken(lambda d: (d["models"][0].update(location="mars"),
                             d.update(default_model="gh/none")))
    with pytest.raises(RegistryError) as exc:
        load_registry(write(tmp_path, data))
    assert "location" in str(exc.value) and "default_model" in str(exc.value)
