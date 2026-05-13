"""Tests for claudecode/_jsonl.py.

Covers streaming parse, user-typed-prompt detection, tool_use / tool_result
extraction, turn partitioning, and tool-call pairing. Uses synthetic
fixtures from ``_fixtures.py``.

A separate end-to-end test runs against the live JSONL on this machine to
prove the parser handles real-world record-type distribution; that test
self-skips when Claude Code isn't installed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claudecode import _jsonl as J

from . import _fixtures as fx


# ---------------------------------------------------------------------------
# iter_records
# ---------------------------------------------------------------------------


def test_iter_records_streams_well_formed_lines(tmp_path):
    f = tmp_path / "s.jsonl"
    records = [fx.user_typed_string("hi"), fx.assistant_text("hello")]
    fx.write_jsonl(f, records)
    got = list(J.iter_records(f))
    assert len(got) == 2
    assert got[0]["type"] == "user"
    assert got[1]["type"] == "assistant"


def test_iter_records_skips_blank_lines(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text(
        json.dumps(fx.user_typed_string("hi")) + "\n"
        "\n"
        "   \n"
        + json.dumps(fx.assistant_text("ok")) + "\n",
        encoding="utf-8",
    )
    assert len(list(J.iter_records(f))) == 2


def test_iter_records_skips_malformed_json(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text(
        json.dumps(fx.user_typed_string("hi")) + "\n"
        "not-json\n"
        + json.dumps(fx.assistant_text("ok")) + "\n",
        encoding="utf-8",
    )
    assert len(list(J.iter_records(f))) == 2


def test_iter_records_skips_non_object_lines(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text(
        json.dumps(fx.user_typed_string("hi")) + "\n"
        "[1, 2, 3]\n"
        "\"just a string\"\n"
        + json.dumps(fx.assistant_text("ok")) + "\n",
        encoding="utf-8",
    )
    got = list(J.iter_records(f))
    assert len(got) == 2


# ---------------------------------------------------------------------------
# unknown_record_types
# ---------------------------------------------------------------------------


def test_unknown_record_types_returns_only_unknown_sorted_unique():
    records = [
        fx.user_typed_string("hi"),
        fx.assistant_text("ok"),
        {"type": "novel-type-xyz"},
        {"type": "another-novel"},
        {"type": "novel-type-xyz"},
        fx.last_prompt("leaf"),
    ]
    assert J.unknown_record_types(records) == ["another-novel", "novel-type-xyz"]


def test_unknown_record_types_known_types_excluded():
    records = [
        {"type": t} for t in J.KNOWN_RECORD_TYPES
    ]
    assert J.unknown_record_types(records) == []


# ---------------------------------------------------------------------------
# is_user_typed_prompt + extract_user_prompt_text
# ---------------------------------------------------------------------------


def test_is_user_typed_prompt_true_for_string_content():
    assert J.is_user_typed_prompt(fx.user_typed_string("hello")) is True


def test_is_user_typed_prompt_true_for_list_with_only_text():
    rec = fx.user_typed_blocks({"type": "text", "text": "pasted snippet"})
    assert J.is_user_typed_prompt(rec) is True


def test_is_user_typed_prompt_false_for_tool_result_echo():
    rec = fx.user_tool_result("toolu_abc", content="output text")
    assert J.is_user_typed_prompt(rec) is False


def test_is_user_typed_prompt_false_for_non_user_records():
    assert J.is_user_typed_prompt(fx.assistant_text("hi")) is False
    assert J.is_user_typed_prompt(fx.last_prompt("u")) is False
    assert J.is_user_typed_prompt(fx.attachment()) is False


def test_extract_user_prompt_text_string():
    assert J.extract_user_prompt_text(fx.user_typed_string("hello world")) == "hello world"


def test_extract_user_prompt_text_text_blocks_concatenated():
    rec = fx.user_typed_blocks(
        {"type": "text", "text": "line one"},
        {"type": "text", "text": "line two"},
    )
    assert J.extract_user_prompt_text(rec) == "line one\nline two"


def test_extract_user_prompt_text_empty_when_no_text():
    rec = fx.user_typed_blocks({"type": "image", "source": {"data": "..."}})
    assert J.extract_user_prompt_text(rec) == ""


# ---------------------------------------------------------------------------
# collect_tool_uses / collect_tool_results
# ---------------------------------------------------------------------------


def test_collect_tool_uses_from_assistant_record():
    rec = fx.assistant_tool_use("Read", tool_input={"file_path": "x"},
                                 tool_use_id="toolu_abc")
    blocks = J.collect_tool_uses(rec)
    assert len(blocks) == 1
    assert blocks[0]["name"] == "Read"
    assert blocks[0]["id"] == "toolu_abc"
    assert blocks[0]["input"] == {"file_path": "x"}


def test_collect_tool_uses_empty_for_text_only_assistant():
    assert J.collect_tool_uses(fx.assistant_text("hi")) == []


def test_collect_tool_uses_empty_for_user_record():
    assert J.collect_tool_uses(fx.user_typed_string("hi")) == []


def test_collect_tool_results_from_user_record():
    rec = fx.user_tool_result("toolu_abc", content="result text", is_error=False)
    blocks = J.collect_tool_results(rec)
    assert len(blocks) == 1
    assert blocks[0]["tool_use_id"] == "toolu_abc"
    assert blocks[0]["is_error"] is False


def test_collect_tool_results_empty_for_user_typed_record():
    assert J.collect_tool_results(fx.user_typed_string("hi")) == []


# ---------------------------------------------------------------------------
# partition_into_turns
# ---------------------------------------------------------------------------


def test_partition_one_user_prompt_yields_one_turn():
    records = [fx.user_typed_string("hello")]
    turns = J.partition_into_turns(records)
    assert len(turns) == 1
    assert turns[0].index == 0
    assert turns[0].user_record is records[0]


def test_partition_user_assistant_yields_one_turn_with_assistant_attached():
    u = fx.user_typed_string("hi")
    a = fx.assistant_text("hello")
    turns = J.partition_into_turns([u, a])
    assert len(turns) == 1
    assert turns[0].user_record is u
    assert turns[0].assistant_records == [a]


def test_partition_second_user_prompt_starts_new_turn():
    u1 = fx.user_typed_string("first")
    a1 = fx.assistant_text("ack")
    u2 = fx.user_typed_string("second")
    a2 = fx.assistant_text("ack2")
    turns = J.partition_into_turns([u1, a1, u2, a2])
    assert len(turns) == 2
    assert turns[0].user_record is u1
    assert turns[0].assistant_records == [a1]
    assert turns[1].user_record is u2
    assert turns[1].assistant_records == [a2]


def test_partition_tool_result_lands_in_current_turn():
    u = fx.user_typed_string("read foo")
    a = fx.assistant_tool_use("Read", tool_input={"file_path": "foo"},
                               tool_use_id="toolu_1")
    r = fx.user_tool_result("toolu_1", content="bar")
    turns = J.partition_into_turns([u, a, r])
    assert len(turns) == 1
    assert turns[0].tool_result_records == [r]


def test_partition_attachments_and_last_prompts_land_in_trailing():
    u = fx.user_typed_string("hi")
    att = fx.attachment("sys context")
    lp = fx.last_prompt("leaf")
    turns = J.partition_into_turns([u, att, lp])
    assert turns[0].trailing_records == [att, lp]


def test_partition_pre_user_records_form_orphan_initial_turn():
    """Records before the first user prompt are not lost."""
    att = fx.attachment("preamble")
    u = fx.user_typed_string("hi")
    turns = J.partition_into_turns([att, u])
    # First turn carries the orphan attachment.
    assert len(turns) == 2
    assert turns[0].user_record is None
    assert turns[0].trailing_records == [att]
    assert turns[1].user_record is u


def test_partition_sidechain_records_dropped():
    """isSidechain records belong to subagent JSONLs, not the main turn list."""
    u = fx.user_typed_string("hi")
    a = fx.assistant_text("ok")
    sidechain = fx.sidechain_record(fx.assistant_text("subagent work"))
    turns = J.partition_into_turns([u, a, sidechain])
    assert len(turns) == 1
    assert turns[0].assistant_records == [a]


def test_partition_returns_empty_for_empty_input():
    assert J.partition_into_turns([]) == []


# ---------------------------------------------------------------------------
# pair_tool_calls
# ---------------------------------------------------------------------------


def test_pair_tool_calls_matches_by_tool_use_id():
    u = fx.user_typed_string("read foo")
    a = fx.assistant_tool_use("Read", tool_input={"file_path": "foo"},
                               tool_use_id="toolu_1")
    r = fx.user_tool_result("toolu_1", content="contents of foo")
    turns = J.partition_into_turns([u, a, r])
    paired = J.pair_tool_calls(turns[0])
    assert len(paired) == 1
    assert paired[0].tool_use_block["id"] == "toolu_1"
    assert paired[0].tool_result_block is not None
    assert paired[0].tool_result_block["tool_use_id"] == "toolu_1"


def test_pair_tool_calls_preserves_call_order():
    u = fx.user_typed_string("multi")
    a1 = fx.assistant_tool_use("Read", tool_use_id="t1")
    a2 = fx.assistant_tool_use("Read", tool_use_id="t2")
    a3 = fx.assistant_tool_use("Read", tool_use_id="t3")
    r2 = fx.user_tool_result("t2", "r2")
    r1 = fx.user_tool_result("t1", "r1")
    r3 = fx.user_tool_result("t3", "r3")
    # Results are written out of order; tool_use call order is what matters.
    turns = J.partition_into_turns([u, a1, a2, a3, r2, r1, r3])
    paired = J.pair_tool_calls(turns[0])
    assert [pc.tool_use_block["id"] for pc in paired] == ["t1", "t2", "t3"]


def test_pair_tool_calls_unmatched_use_yields_paired_with_none_result():
    """Session interrupted mid-tool-call: tool_use exists, no result."""
    u = fx.user_typed_string("interrupted")
    a = fx.assistant_tool_use("Bash", tool_input={"command": "sleep 999"},
                               tool_use_id="toolu_orphan")
    turns = J.partition_into_turns([u, a])
    paired = J.pair_tool_calls(turns[0])
    assert len(paired) == 1
    assert paired[0].tool_use_block["id"] == "toolu_orphan"
    assert paired[0].tool_result_block is None
    assert paired[0].result_record is None


def test_pair_tool_calls_multi_block_assistant_record():
    """A single assistant record can carry multiple tool_use blocks in order."""
    # The fixture builder yields one tool_use per assistant record; build a
    # multi-block record manually here to verify within-record order.
    u = fx.user_typed_string("multi")
    multi_assistant = {
        "type": "assistant",
        "isSidechain": False,
        "uuid": "ma-uuid",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tA", "name": "Read",
                 "input": {"file_path": "a"}, "caller": {"type": "direct"}},
                {"type": "text", "text": "interleaved"},
                {"type": "tool_use", "id": "tB", "name": "Read",
                 "input": {"file_path": "b"}, "caller": {"type": "direct"}},
            ],
        },
    }
    rA = fx.user_tool_result("tA", "a-contents")
    rB = fx.user_tool_result("tB", "b-contents")
    turns = J.partition_into_turns([u, multi_assistant, rA, rB])
    paired = J.pair_tool_calls(turns[0])
    assert [pc.tool_use_block["id"] for pc in paired] == ["tA", "tB"]


# ---------------------------------------------------------------------------
# Real-world smoke
# ---------------------------------------------------------------------------


def test_iter_records_against_live_session_jsonl():
    """Smoke: parse this machine's own session JSONL without errors.

    Skips if the file is unavailable (e.g., sandboxed CI). Asserts we get
    a stream of dict records with known record types in the majority.
    """
    candidate = Path.home() / ".claude" / "projects" / "C---Source-ai-chat-report"
    if not candidate.exists():
        pytest.skip("no live Claude Code session data on this host")
    jsonls = sorted(candidate.glob("*.jsonl"))
    if not jsonls:
        pytest.skip("no session JSONLs in this project's claude dir")
    # Pick the biggest one (current session is likely the biggest).
    target = max(jsonls, key=lambda p: p.stat().st_size)
    records = list(J.iter_records(target))
    assert len(records) > 0, f"empty record stream from {target}"
    types = {r.get("type") for r in records}
    # Every real session has at least user + assistant + last-prompt.
    assert {"user", "assistant", "last-prompt"} <= types
    # Most records should have a known type. Unknown types are tolerated
    # but should be a small minority.
    unknown = J.unknown_record_types(records)
    assert len(unknown) <= 2, f"surprising number of unknown types: {unknown}"


def test_partition_against_live_session_jsonl():
    """Smoke: partitioning yields one turn per user-typed prompt."""
    candidate = Path.home() / ".claude" / "projects" / "C---Source-ai-chat-report"
    if not candidate.exists():
        pytest.skip("no live Claude Code session data on this host")
    jsonls = sorted(candidate.glob("*.jsonl"))
    if not jsonls:
        pytest.skip("no session JSONLs in this project's claude dir")
    target = max(jsonls, key=lambda p: p.stat().st_size)
    records = list(J.iter_records(target))
    turns = J.partition_into_turns(records)
    user_typed = sum(1 for r in records if J.is_user_typed_prompt(r))
    # Every user-typed prompt produces one turn. There may also be one
    # orphan initial turn (no user_record) carrying pre-prompt attachments.
    assert user_typed > 0
    assert len(turns) >= user_typed
    # Every Turn either has a user_record or is the orphan initial turn.
    for t in turns:
        if t.user_record is None:
            # The orphan must carry SOMETHING -- otherwise it shouldn't exist.
            assert (t.assistant_records or t.tool_result_records
                    or t.trailing_records)
