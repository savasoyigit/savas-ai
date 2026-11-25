import asyncio
import json

import pytest

import context_engine as ce

FULL = {
    "status": "ok",
    "summary": "SQLite tek dosyada tutarlı sorgu sağlar. Markdown taşınabilir kalır.",
    "findings": ["SQLite'ta tam metin arama hazır.", "Markdown dosyaları her editörde açılır."],
    "artifacts": ["Projeler/Notlar.md"],
    "open_questions": ["Not sayısı kaç olacak?"],
}


def dumps(value):
    return json.dumps(value, ensure_ascii=False)


# ----------------------------------------------------------------------
# Shares and limits
# ----------------------------------------------------------------------

@pytest.mark.parametrize("workers,share", [(1, 3000), (2, 1500), (3, 1000), (5, 600), (9, 600), (0, 3000)])
def test_report_share_splits_the_delegation_budget(workers, share):
    assert ce.report_share(workers) == share


@pytest.mark.parametrize("share,words", [(3000, 150), (1000, 62), (600, 37)])
def test_summary_word_limit_follows_the_share(share, words):
    assert ce.summary_words(share) == words


@pytest.mark.parametrize("share,reserve,tokens", [(1000, 2000, 433), (3000, 2000, 1100), (600, 2000, 400),
                                                  (3000, 800, 800)])
def test_worker_completion_tokens(share, reserve, tokens):
    assert ce.worker_completion_tokens(share, reserve) == tokens


def test_instructions_state_schema_and_this_calls_limits():
    text = ce.report_instructions(1000)
    for key in ("status", "summary", "findings", "artifacts", "open_questions", "deliverable"):
        assert f'"{key}"' in text
    assert "1000 karakter" in text and "62 kelime" in text
    assert "150 kelime" in ce.report_instructions(3000)


# ----------------------------------------------------------------------
# Parser
# ----------------------------------------------------------------------

def test_valid_json_is_kept_as_is():
    report, parsed = ce.parse_worker_report(dumps(FULL), 3000)
    assert parsed and report == FULL


def test_json_inside_a_code_fence():
    text = f"İşte rapor:\n```json\n{dumps(FULL)}\n```\nBaşka bir şey yok."
    report, parsed = ce.parse_worker_report(text, 3000)
    assert parsed and report == FULL


def test_json_surrounded_by_prose():
    report, parsed = ce.parse_worker_report(f"Rapor: {dumps(FULL)} Saygılar.", 3000)
    assert parsed and report == FULL


def test_raw_newlines_inside_strings_are_tolerated():
    text = '{"status": "ok", "summary": "ilk satır\nikinci satır"}'
    report, parsed = ce.parse_worker_report(text, 3000)
    assert parsed and report["summary"] == "ilk satır\nikinci satır"


def test_types_are_coerced_and_unknown_keys_dropped():
    report, parsed = ce.parse_worker_report(dumps({
        "status": "Completed", "summary": "  özet  ", "findings": "tek bulgu",
        "artifacts": [], "open_questions": None, "confidence": 0.9, "deliverable": {"a": 1},
    }), 3000)
    assert parsed
    assert report == {"status": "ok", "summary": "özet", "findings": ["tek bulgu"], "deliverable": '{"a": 1}'}


@pytest.mark.parametrize("status,expected", [("ok", "ok"), ("partial", "partial"), ("failed", "failed"),
                                             ("done", "ok"), ("blocked", "partial"), (None, "partial")])
def test_status_values(status, expected):
    report, _ = ce.parse_worker_report(dumps({"status": status, "summary": "x"}), 3000)
    assert report["status"] == expected


def test_field_limits():
    report, _ = ce.parse_worker_report(dumps({
        "status": "ok",
        "summary": " ".join(f"k{i}" for i in range(200)),
        "findings": [f"bulgu {i}" for i in range(9)] + ["x" * 500],
        "artifacts": [f"a/{i}.md" for i in range(9)],
        "open_questions": [f"soru {i}?" for i in range(9)],
    }), 100000)
    assert report["summary"].endswith("k149" + ce.TRIM_MARK)
    assert len(report["findings"]) == ce.MAX_FINDINGS
    assert len(report["artifacts"]) == ce.MAX_ARTIFACTS
    assert len(report["open_questions"]) == ce.MAX_OPEN_QUESTIONS

    long_item = ce.as_items(["x" * 500], 5)[0]
    assert len(long_item) == ce.MAX_ITEM_CHARS and long_item.endswith("…")


# ----------------------------------------------------------------------
# Fitting into the share
# ----------------------------------------------------------------------

def big_report():
    return {
        "status": "ok",
        "summary": "Birinci cümle burada biter. " * 12,
        "findings": [f"bulgu {i} " + "y" * 120 for i in range(5)],
        "artifacts": ["Projeler/A.md"],
        "open_questions": [f"soru {i} " + "z" * 120 for i in range(3)],
        "deliverable": "def dedupe(items):\n    seen = set()\n" + "    # satır\n" * 80,
    }


def test_report_under_the_share_is_untouched():
    assert ce.fit_report(FULL, 3000) == FULL


@pytest.mark.parametrize("share", [3000, 1500, 1000, 600])
def test_fitted_report_never_exceeds_the_share(share):
    report = ce.fit_report(big_report(), share)
    assert ce.serialized_length(report) <= share
    assert report["status"] == "ok" and report["summary"]


def test_trim_order_findings_then_questions_then_deliverable_then_summary():
    full = big_report()
    size = ce.serialized_length(full)

    # a little over: only findings are dropped, from the end
    report = ce.fit_report(full, size - 50)
    assert report["findings"] == full["findings"][:4]
    assert report["open_questions"] == full["open_questions"]
    assert report["deliverable"] == full["deliverable"] and report["summary"] == full["summary"]

    # findings gone, questions start to go; deliverable and summary intact
    without_findings = size - ce.serialized_length({"findings": full["findings"]})
    report = ce.fit_report(full, without_findings - 100)
    assert "findings" not in report
    assert report["open_questions"] == full["open_questions"][:2]
    assert report["deliverable"] == full["deliverable"]

    # lists gone, deliverable is cut and marked; summary intact
    report = ce.fit_report(full, 900)
    assert "findings" not in report and "open_questions" not in report
    assert report["deliverable"].endswith(ce.TRIM_MARK)
    assert full["deliverable"].startswith(report["deliverable"][:-len(ce.TRIM_MARK)].rstrip())
    assert report["summary"] == full["summary"]

    # smaller than the summary alone: deliverable is gone, summary cut at a sentence end
    report = ce.fit_report(full, 250)
    assert "deliverable" not in report
    assert report["summary"].endswith("biter." + ce.TRIM_MARK)
    assert report["artifacts"] == full["artifacts"]


def test_long_artifact_list_does_not_empty_the_summary():
    full = big_report()
    full["artifacts"] = [f"Projeler/{i}/" + "a" * 180 + ".md" for i in range(5)]
    report = ce.fit_report(full, 600)

    assert ce.serialized_length(report) <= 600
    assert len(report["summary"]) >= 100
    assert 0 < len(report["artifacts"]) < 5
    assert report["artifacts"] == full["artifacts"][:len(report["artifacts"])]


def test_fit_report_does_not_modify_its_input():
    full = big_report()
    before = dumps(full)
    ce.fit_report(full, 600)
    assert dumps(full) == before


# ----------------------------------------------------------------------
# Fallback
# ----------------------------------------------------------------------

PLAIN = "Birinci paragraf burada.\n\n" + "İkinci paragraf oldukça uzun bir metin. " * 40


def test_plain_text_falls_back_and_is_trimmed():
    report, parsed = ce.parse_worker_report(PLAIN, 600)
    assert not parsed
    assert report["status"] == ce.UNSTRUCTURED
    assert set(report) == {"status", "summary"}
    assert report["summary"].startswith("Birinci paragraf burada.")
    assert report["summary"].endswith("metin." + ce.TRIM_MARK)
    assert ce.serialized_length(report) <= 600


def test_short_plain_text_is_not_marked_as_trimmed():
    report, parsed = ce.parse_worker_report("Kısa cevap.", 600)
    assert not parsed and report == {"status": ce.UNSTRUCTURED, "summary": "Kısa cevap."}


@pytest.mark.parametrize("text", ['{"status": "ok", "summary": "yarım kal', "{}", '{"note": "şema dışı"}', "[1, 2]"])
def test_broken_or_empty_json_falls_back(text):
    report, parsed = ce.parse_worker_report(text, 600)
    assert not parsed and report["status"] == ce.UNSTRUCTURED and report["summary"]


@pytest.mark.parametrize("text", ["", "   ", None])
def test_empty_output(text):
    report, parsed = ce.parse_worker_report(text, 600)
    assert not parsed and report == {"status": "failed", "summary": ce.EMPTY_RESPONSE}


# ----------------------------------------------------------------------
# run_worker / delegation (main.py harness, fake LLM)
# ----------------------------------------------------------------------

def answer(content):
    def handler(url, payload):
        return 200, {
            "model": payload["model"],
            "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        }
    return staticmethod(handler)


def report_events(orch):
    return [e for e in orch.ACTIVITY if e["event"] == "worker_report"]


def test_worker_result_carries_the_report_only(orch):
    orch.fake_client.handler = answer(dumps(FULL))
    result = asyncio.run(orch.run_worker("jarvis", "x yaz", "parent-1"))

    assert result == {"status": "completed", "worker": "jarvis", "report": FULL}
    assert [e["status"] for e in report_events(orch)] == ["parsed"]


def test_worker_prompt_states_the_schema_and_share(orch):
    asyncio.run(orch.run_worker("jarvis", "x yaz", "parent-1", report_chars=1000))
    system = orch.fake_client.calls[-1]["json"]["messages"][0]["content"]
    assert ce.report_instructions(1000) in system


def test_unstructured_worker_output_is_trimmed_and_logged(orch):
    orch.fake_client.handler = answer(PLAIN)
    result = asyncio.run(orch.run_worker("oracle", "araştır", "parent-1", report_chars=600))

    assert result["report"]["status"] == ce.UNSTRUCTURED
    assert ce.serialized_length(result["report"]) <= 600
    assert [e["status"] for e in report_events(orch)] == ["fallback"]


def test_full_worker_output_stays_in_the_daily_file(orch):
    orch.fake_client.handler = answer(PLAIN)
    asyncio.run(orch.run_worker("oracle", "araştır", "parent-1", report_chars=600))
    daily = next((orch.worker_manager.worker_daily_root("oracle")).glob("*.md")).read_text(encoding="utf-8")
    assert PLAIN.strip()[-60:] in daily


def test_cloud_worker_call_is_capped_and_reserved_by_its_share(orch):
    asyncio.run(orch.run_worker("jarvis", "x yaz", "parent-1", report_chars=1000))
    assert orch.fake_client.calls[-1]["json"]["max_tokens"] == 433

    check = [e for e in orch.ACTIVITY if e["event"] == "budget_check"][-1]
    assert check["completion_reserve"] == 433
    assert check["requested_tokens"] == check["estimated_prompt_tokens"] + 433


def test_single_delegation_gets_the_whole_budget(orch):
    asyncio.run(orch.execute_worker_tool("delegate_to_worker", {"worker": "jarvis", "task": "x yaz"}, "parent-1"))
    sent = orch.fake_client.calls[-1]["json"]
    assert ce.report_instructions(3000) in sent["messages"][0]["content"]
    assert sent["max_tokens"] == 1100


def test_parallel_delegation_splits_the_budget(orch):
    orch.fake_client.handler = answer(dumps(big_report()))
    delegations = [{"worker": w, "task": "değerlendir"} for w in ("jarvis", "oracle", "gordon")]
    result = asyncio.run(orch.execute_worker_tool("delegate_to_workers", {"delegations": delegations}, "parent-1"))

    assert [d["worker"] for d in result["delegations"]] == ["jarvis", "oracle", "gordon"]
    assert all(set(d) == {"status", "worker", "report"} for d in result["delegations"])
    assert all(ce.serialized_length(d["report"]) <= 1000 for d in result["delegations"])
    assert sum(ce.serialized_length(d["report"]) for d in result["delegations"]) <= ce.DELEGATION_RESULT_CHARS
    assert all(call["json"]["max_tokens"] == 433 for call in orch.fake_client.calls)
    assert all(ce.report_instructions(1000) in call["json"]["messages"][0]["content"]
               for call in orch.fake_client.calls)
