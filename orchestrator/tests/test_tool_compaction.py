import asyncio

import pytest

import context_engine as ce

LINE = "Bu satır tam otuz karakterdir.\n"
LONG_NOTE = "".join(f"{i:04d} {LINE}" for i in range(500))  # 500 lines of 36 characters


def found(*paths, query="q"):
    return {"query": query, "count": len(paths),
            "results": [{"path": path, "name": path.rsplit("/", 1)[-1]} for path in paths]}


# ----------------------------------------------------------------------
# search_notes
# ----------------------------------------------------------------------

def test_search_keeps_paths_only():
    assert ce.compact_search(found("a/x.md", "b/y.md", query="sqlite")) == {
        "query": "sqlite", "count": 2, "results": ["a/x.md", "b/y.md"],
    }


def test_search_drops_duplicate_paths_and_keeps_order():
    result = ce.compact_search(found("b.md", "a.md", "b.md", "c.md", "a.md"))
    assert result["results"] == ["b.md", "a.md", "c.md"] and result["count"] == 3
    assert "note" not in result


def test_search_at_the_limit_has_no_note():
    result = ce.compact_search(found(*(f"n/{i}.md" for i in range(ce.MAX_SEARCH_RESULTS))))
    assert len(result["results"]) == ce.MAX_SEARCH_RESULTS and "note" not in result


def test_search_over_the_limit_is_cut_and_says_so():
    result = ce.compact_search(found(*(f"n/{i}.md" for i in range(25))))
    assert result["results"] == [f"n/{i}.md" for i in range(20)]
    assert result["count"] == 25
    assert result["note"].startswith("5 sonuç daha var")


@pytest.mark.parametrize("raw", [{}, {"query": "q", "results": None}, {"query": "q", "results": []}])
def test_search_without_results(raw):
    assert ce.compact_search(raw)["results"] == []


# ----------------------------------------------------------------------
# read_note
# ----------------------------------------------------------------------

@pytest.mark.parametrize("size", [0, 100, ce.MAX_NOTE_CHARS])
def test_note_under_the_limit_is_unchanged(size):
    note = {"path": "a.md", "content": "x" * size}
    assert ce.compact_note(note) is note


def test_long_note_is_cut_after_a_line_end():
    result = ce.compact_note({"path": "a.md", "content": LONG_NOTE})

    assert result["path"] == "a.md"
    assert LONG_NOTE.startswith(result["content"])
    assert ce.MAX_NOTE_CHARS - len("0000 " + LINE) < len(result["content"]) <= ce.MAX_NOTE_CHARS
    assert result["content"].endswith(LINE)
    assert result["truncated"] is True
    assert result["total_chars"] == len(LONG_NOTE)
    assert result["next_offset"] == len(result["content"])
    assert f"read_note(path, offset={result['next_offset']})" in result["note"]
    assert "offset" not in result


def test_parts_join_back_to_the_note():
    parts, offset = [], 0
    while True:
        result = ce.compact_note({"path": "a.md", "content": LONG_NOTE}, offset)
        parts.append(result["content"])
        if not result.get("truncated"):
            break
        assert result["next_offset"] > offset
        offset = result["next_offset"]

    assert "".join(parts) == LONG_NOTE
    # 166 whole lines (5,976 characters) fit in a part
    assert [len(part) for part in parts] == [5976, 5976, 5976, 72]
    # the last part says where it started and is not marked as cut
    assert result["offset"] == offset and result["total_chars"] == len(LONG_NOTE)
    assert "next_offset" not in result and "note" not in result


def test_note_without_line_ends_is_cut_at_the_limit():
    result = ce.compact_note({"path": "a.md", "content": "x" * 15000})
    assert len(result["content"]) == ce.MAX_NOTE_CHARS and result["next_offset"] == ce.MAX_NOTE_CHARS


def test_offset_past_the_end_returns_no_content():
    result = ce.compact_note({"path": "a.md", "content": "kısa not"}, 500)
    assert result["content"] == "" and "truncated" not in result
    assert result["total_chars"] == 8 and "kalmadı" in result["note"]


def test_result_without_text_content_is_passed_through():
    error = {"status": "error", "error": "not found"}
    assert ce.compact_note(error, 10) is error


@pytest.mark.parametrize("value,offset", [(None, 0), (0, 0), (5980, 5980), ("120", 120), (-5, 0), ("x", 0),
                                          (3.7, 3), ([], 0)])
def test_offset_argument(value, offset):
    assert ce.note_offset(value) == offset


# ----------------------------------------------------------------------
# execute_read_tool / execute_write_tool (main.py harness)
# ----------------------------------------------------------------------

@pytest.fixture
def vault(orch, monkeypatch):
    """main.py with the Obsidian Agent calls replaced; vault.requests lists what reached it."""
    orch.requests = []
    orch.notes = {"long.md": LONG_NOTE, "short.md": "kısa not"}
    orch.TASK_CONTEXTS["t1"] = {"messages": [], "pending_tools": {}, "next_iteration": 1}

    async def search_notes(query, task_id):
        orch.requests.append(("search", query))
        return found(*(f"n/{i}.md" for i in range(25)), query=query)

    async def read_note(path, task_id):
        orch.requests.append(("read", path))
        if path not in orch.notes:
            raise orch.httpx.HTTPError("HTTP 404")
        return {"path": path, "content": orch.notes[path]}

    async def write(*args):
        orch.requests.append(("write", *args[:-1]))
        return {"status": "ok"}

    monkeypatch.setattr(orch, "search_notes", search_notes)
    monkeypatch.setattr(orch, "read_note", read_note)
    for name in ("create_note", "update_note", "move_note"):
        monkeypatch.setattr(orch, name, write)
    return orch


def read(vault, name, arguments, task_id="t1"):
    return asyncio.run(vault.execute_read_tool(name, arguments, task_id))


def write(vault, name, arguments, task_id="t1"):
    return asyncio.run(vault.execute_write_tool(name, arguments, task_id))


def results(vault, status):
    return [e for e in vault.ACTIVITY if e["event"] == "tool_result" and e["status"] == status]


def test_search_result_is_compacted(vault):
    result = read(vault, "search_notes", {"query": "sqlite"})
    assert len(result["results"]) == 20 and result["count"] == 25 and "note" in result
    assert [(e["tool"], e["result_count"], e["returned"]) for e in results(vault, "truncated")] == [
        ("search_notes", 25, 20)]


def test_repeated_search_is_not_answered_twice(vault):
    read(vault, "search_notes", {"query": "SQLite"})
    again = read(vault, "search_notes", {"query": "  sqlite "})

    assert again["repeat"] is True and "results" not in again
    assert vault.requests == [("search", "SQLite")]
    assert [e["tool"] for e in results(vault, "repeat")] == ["search_notes"]


def test_long_note_is_read_in_parts(vault):
    first = read(vault, "read_note", {"path": "long.md"})
    second = read(vault, "read_note", {"path": "long.md", "offset": first["next_offset"]})

    assert first["truncated"] and second["truncated"]
    assert first["content"] + second["content"] == LONG_NOTE[:second["next_offset"]]
    assert [(e["path"], e["offset"], e["next_offset"]) for e in results(vault, "truncated")] == [
        ("long.md", 0, first["next_offset"]), ("long.md", first["next_offset"], second["next_offset"])]


def test_short_note_is_returned_as_is(vault):
    assert read(vault, "read_note", {"path": "short.md"}) == {"path": "short.md", "content": "kısa not"}
    assert results(vault, "truncated") == []


def test_repeated_note_part_is_not_answered_twice(vault):
    first = read(vault, "read_note", {"path": "long.md"})
    again = read(vault, "read_note", {"path": "long.md", "offset": 0})

    assert again == ce.repeated_note("long.md", 0) and "content" not in again
    assert vault.requests == [("read", "long.md")]
    # another part of the same note is a new read
    assert "content" in read(vault, "read_note", {"path": "long.md", "offset": first["next_offset"]})


@pytest.mark.parametrize("tool,arguments", [
    ("update_note", {"path": "short.md", "content": "yeni"}),
    ("create_note", {"path": "short.md", "content": "yeni"}),
    ("move_note", {"source": "short.md", "destination": "arsiv/short.md"}),
    ("move_note", {"source": "eski.md", "destination": "short.md"}),
])
def test_note_can_be_read_again_after_a_write_to_it(vault, tool, arguments):
    read(vault, "read_note", {"path": "short.md"})
    read(vault, "read_note", {"path": "long.md"})
    write(vault, tool, arguments)

    assert "content" in read(vault, "read_note", {"path": "short.md"})
    # a note that was not written stays recorded
    assert read(vault, "read_note", {"path": "long.md"})["repeat"] is True


def test_search_runs_again_after_any_write(vault):
    read(vault, "search_notes", {"query": "sqlite"})
    write(vault, "create_note", {"path": "yeni.md", "content": "sqlite"})

    assert "results" in read(vault, "search_notes", {"query": "sqlite"})
    assert [r for r in vault.requests if r[0] == "search"] == [("search", "sqlite")] * 2


def test_failed_read_is_not_recorded(vault):
    with pytest.raises(vault.httpx.HTTPError):
        read(vault, "read_note", {"path": "yok.md"})

    vault.notes["yok.md"] = "artık var"
    assert read(vault, "read_note", {"path": "yok.md"})["content"] == "artık var"


def test_record_belongs_to_one_task(vault):
    vault.TASK_CONTEXTS["t2"] = {"messages": [], "pending_tools": {}, "next_iteration": 1}
    read(vault, "read_note", {"path": "short.md"})

    assert "content" in read(vault, "read_note", {"path": "short.md"}, task_id="t2")
    write(vault, "update_note", {"path": "short.md", "content": "yeni"}, task_id="t2")
    assert read(vault, "read_note", {"path": "short.md"})["repeat"] is True


def test_without_a_task_context_nothing_is_recorded(vault):
    for _ in range(2):
        assert "content" in read(vault, "read_note", {"path": "short.md"}, task_id="yok")
    write(vault, "update_note", {"path": "short.md", "content": "yeni"}, task_id="yok")


def test_list_folder_is_untouched(vault, monkeypatch):
    async def list_folder(path, task_id):
        return {"path": path, "items": ["a.md"] * 50}

    monkeypatch.setattr(vault, "list_folder", list_folder)
    for _ in range(2):
        assert len(read(vault, "list_folder", {"path": "n"})["items"]) == 50


def test_read_note_schema_has_an_optional_offset(orch):
    tools = {tool["function"]["name"]: tool["function"] for tool in orch.TOOLS}
    parameters = tools["read_note"]["parameters"]

    assert parameters["properties"]["offset"]["type"] == "integer"
    assert parameters["required"] == ["path"] and parameters["additionalProperties"] is False
    assert "next_offset" in tools["read_note"]["description"]
    assert str(ce.MAX_NOTE_CHARS) in tools["read_note"]["description"]
    assert str(ce.MAX_SEARCH_RESULTS) in tools["search_notes"]["description"]


def test_system_prompt_warns_about_rewriting_a_truncated_note(orch):
    assert "truncated" in orch.SYSTEM_PROMPT and "update_note" in orch.SYSTEM_PROMPT
