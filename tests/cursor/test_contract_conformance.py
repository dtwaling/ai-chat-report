"""End-to-end contract conformance tests for the locked-shape emitter.

Spins up synthetic Cursor SQLite fixtures (state.vscdb + ai-code-tracking.db),
runs the full ``chat-report.py`` pipeline through them, reads the emitted JSON,
and validates it against the section-4 contract. This catches integration
seams that the unit tests do not cross.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from . import _fixtures as fx
from . import _sqlite_fixture as sqlfx
from . import _locked_contract as contract


def _write_single_chat_fixture(tmp_path: Path, chat_id: str) -> tuple[Path, Path]:
    state_db = tmp_path / "state.vscdb"
    tracking_db = tmp_path / "ai-code-tracking.db"
    bubbles = [
        fx.make_user_bubble("u", text="real-fixture test"),
        fx.make_assistant_text_bubble("a", text="On it."),
        fx.make_tool_call_bubble("r0", tool_name="read_file_v2",
                                 params={"targetFile": ".cursor/DIRECTORY_INDEX.md"}),
        fx.make_tool_call_bubble("r1", tool_name="read_file_v2",
                                 params={"targetFile": "src/foo.py"}),
        fx.make_tool_call_bubble("o0", tool_name="optimus_search",
                                 params={"query": "x"}),
        fx.make_tool_call_bubble("o1", tool_name="optimus_grep",
                                 params={"pattern": "y"}),
        fx.make_tool_call_bubble("g0", tool_name="ripgrep_raw_search",
                                 params={"pattern": "TODO"}),
        fx.make_tool_call_bubble("b0", tool_name="run_terminal_command_v2",
                                 params={"command": "ls"}),
        fx.make_tool_call_bubble("e0", tool_name="edit_file_v2",
                                 params={"targetFile": "src/foo.py"}),
    ]
    sqlfx.write_state_vscdb(state_db, chat_id, bubbles)
    sqlfx.write_empty_tracking_db(tracking_db)
    return state_db, tracking_db


def test_contract_single_chat_via_full_pipeline(chat_report, tmp_path):
    chat_id = "f9a48f94-b698-455e-946d-9734c86babe9"
    state_db, tracking_db = _write_single_chat_fixture(tmp_path, chat_id)
    out_dir = tmp_path / "out"
    rc = chat_report.main([
        chat_id,
        "--shape", "locked",
        "--format", "both",
        "--out", str(out_dir),
        "--state-db", str(state_db),
        "--tracking-db", str(tracking_db),
        "--chat-id", chat_id,
    ])
    assert rc == 0
    json_path = out_dir / f"{chat_id}.json"
    md_path = out_dir / f"{chat_id}.md"
    assert json_path.exists()
    assert md_path.exists()

    report = json.loads(json_path.read_text(encoding="utf-8"))
    contract.assert_locked_single_chat_conforms(report)

    # Sanity-check classification: directory-index-read PRECEDES src/foo.py read,
    # so src/foo.py should be classified informed.
    assistant_turn = next(t for t in report["turns"] if t["role"] == "assistant")
    foo_reads = [tc for tc in assistant_turn["tool_calls"]
                 if tc["tool_name"] == "read_file_v2"
                 and tc["input_payload"].get("targetFile") == "src/foo.py"]
    assert len(foo_reads) == 1
    assert foo_reads[0]["informed_precision_read"]["classification"] == "informed"


def test_contract_diff_via_full_pipeline(chat_report, tmp_path):
    chat_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    chat_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    state_db = tmp_path / "state.vscdb"
    tracking_db = tmp_path / "ai-code-tracking.db"
    sqlfx.write_state_vscdb(state_db, chat_a, [
        fx.make_user_bubble("u"),
        fx.make_tool_call_bubble("r", tool_name="read_file_v2",
                                 params={"targetFile": "src/a.py"}),
    ])
    # Append second chat to the same state.vscdb.
    sqlfx.write_state_vscdb(state_db, chat_b, [
        fx.make_user_bubble("u"),
        fx.make_tool_call_bubble("o", tool_name="optimus_search",
                                 params={"query": "y"}),
    ])
    sqlfx.write_empty_tracking_db(tracking_db)
    out_dir = tmp_path / "out"
    rc = chat_report.main([
        "--diff", chat_a, chat_b,
        "--shape", "locked",
        "--format", "both",
        "--out", str(out_dir),
        "--state-db", str(state_db),
        "--tracking-db", str(tracking_db),
    ])
    assert rc == 0
    matches = list(out_dir.glob("diff-*.json"))
    assert len(matches) == 1
    diff = json.loads(matches[0].read_text(encoding="utf-8"))
    contract.assert_locked_diff_conforms(diff)
    md_matches = list(out_dir.glob("diff-*.md"))
    assert len(md_matches) == 1
    # Sanity: chat_a had 1 broad-sweep-read, chat_b had 1 optimus -> delta correct
    assert diff["delta"]["by_class"]["broad-sweep-read"] == -1
    assert diff["delta"]["by_class"]["optimus-mcp"] == 1


def test_contract_aggregate_via_full_pipeline(chat_report, tmp_path):
    chats = [
        ("11111111-1111-1111-1111-111111111111", "read_file_v2", {"targetFile": "src/a.py"}),
        ("22222222-2222-2222-2222-222222222222", "optimus_search", {"query": "x"}),
        ("33333333-3333-3333-3333-333333333333", "optimus_grep", {"pattern": "y"}),
    ]
    state_db = tmp_path / "state.vscdb"
    tracking_db = tmp_path / "ai-code-tracking.db"
    for chat_id, tool_name, params in chats:
        sqlfx.write_state_vscdb(state_db, chat_id, [
            fx.make_user_bubble("u"),
            fx.make_tool_call_bubble("t", tool_name=tool_name, params=params),
        ])
    sqlfx.write_empty_tracking_db(tracking_db)

    out_dir = tmp_path / "out"
    rc = chat_report.main([
        "--aggregate", *[c[0] for c in chats],
        "--shape", "locked",
        "--format", "both",
        "--out", str(out_dir),
        "--state-db", str(state_db),
        "--tracking-db", str(tracking_db),
    ])
    assert rc == 0
    json_matches = list(out_dir.glob("aggregate-*.json"))
    assert len(json_matches) == 1
    agg = json.loads(json_matches[0].read_text(encoding="utf-8"))
    contract.assert_locked_aggregate_conforms(agg)
    assert agg["session_count"] == 3
    # 2 optimus, 1 broad-sweep => Component A passes
    assert agg["aggregates"]["success_metric_components"]["component_a"]["pass"] is True


def test_contract_validator_rejects_bad_shape(chat_report):
    """The validator must actually fail on a malformed report."""
    bogus = {
        "report_version": "1.0",
        "report_kind": "single-chat",
        "ide": "cursor",
        "ide_version": "unknown",
        "session_id": "x",
        # missing: session_id_normalized, turns, aggregates, etc.
    }
    with pytest.raises(AssertionError, match="missing required keys"):
        contract.assert_locked_single_chat_conforms(bogus)


def test_contract_validator_rejects_bad_tool_class(chat_report):
    chat_id = "f9a48f94-b698-455e-946d-9734c86babe9"
    meta = fx.make_meta(chat_id=chat_id)
    ordered = fx.make_ordered(
        ("u", fx.make_user_bubble("u")),
        ("r", fx.make_tool_call_bubble("r", tool_name="read_file_v2",
                                       params={"targetFile": "src/a.py"})),
    )
    rows = chat_report.build_bubble_rows(ordered)
    report = chat_report.build_locked_report(chat_id, meta, rows, ordered)
    # Corrupt one tool_class to a non-enum value.
    report["turns"][1]["tool_calls"][0]["tool_class"] = "not-a-real-class"
    with pytest.raises(AssertionError, match="tool_class"):
        contract.assert_locked_single_chat_conforms(report)
