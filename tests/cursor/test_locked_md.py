"""Tests for the locked-shape markdown rendering (Cursor variant).

MISSION-BRIEF section 4.2 defines the markdown structure: header, summary table,
success-metric snapshot, turn-by-turn trajectory, denials, errors, warnings.
JSON is the source of truth; markdown is a rendering. The renderer must
handle inf ratios and missing optional sections cleanly.
"""

from __future__ import annotations

from typing import Any

import pytest

from . import _fixtures as fx


def _build_locked(chat_report, chat_id: str, bubbles: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    meta = fx.make_meta(chat_id=chat_id)
    ordered = fx.make_ordered(*bubbles)
    rows = chat_report.build_bubble_rows(ordered)
    return chat_report.build_locked_report(chat_id, meta, rows, ordered)


def _two_turn_chat(chat_report) -> dict[str, Any]:
    return _build_locked(chat_report, "f9a48f94-b698-455e-946d-9734c86babe9", [
        ("u", fx.make_user_bubble("u", text="check issue 35")),
        ("r0", fx.make_tool_call_bubble("r0", tool_name="read_file_v2",
                                        params={"targetFile": "src/foo.py"})),
        ("o0", fx.make_tool_call_bubble("o0", tool_name="optimus_search",
                                        params={"query": "issue 35"})),
    ])


def test_md_single_chat_header_present(chat_report, tmp_path):
    report = _two_turn_chat(chat_report)
    out = tmp_path / "report.md"
    chat_report.write_locked_md_report(out, report)
    md = out.read_text(encoding="utf-8")

    assert "# Chat report" in md
    assert "f9a48f94-b698-455e-946d-9734c86babe9" in md
    assert "cursor" in md.lower()  # IDE field
    # session_duration_s is computed from fixture timestamps
    assert "Duration" in md


def test_md_single_chat_summary_table_lists_all_classes(chat_report, tmp_path):
    report = _two_turn_chat(chat_report)
    out = tmp_path / "report.md"
    chat_report.write_locked_md_report(out, report)
    md = out.read_text(encoding="utf-8")

    # Each locked tool_class appears in the summary table.
    for cls in (
        "broad-sweep-read", "broad-sweep-grep", "broad-sweep-glob",
        "optimus-mcp", "directory-index-read",
        "edit", "write", "bash", "other",
    ):
        assert cls in md


def test_md_single_chat_success_metric_snapshot(chat_report, tmp_path):
    report = _two_turn_chat(chat_report)
    out = tmp_path / "report.md"
    chat_report.write_locked_md_report(out, report)
    md = out.read_text(encoding="utf-8")

    assert "Component A" in md
    assert "Component B" in md
    # Component A ratio displayed (1 optimus / 1 broad-sweep = 1.0)
    assert "1.0" in md or "1.00" in md
    assert "Overall" in md


def test_md_single_chat_turn_by_turn_trajectory(chat_report, tmp_path):
    report = _two_turn_chat(chat_report)
    out = tmp_path / "report.md"
    chat_report.write_locked_md_report(out, report)
    md = out.read_text(encoding="utf-8")

    # Turn header for the assistant turn (index 1 in this fixture).
    assert "Turn 0" in md or "turn 0" in md.lower()
    assert "Turn 1" in md or "turn 1" in md.lower()
    # Tool names visible.
    assert "read_file_v2" in md
    assert "optimus_search" in md


def test_md_single_chat_warnings_section_renders(chat_report, tmp_path):
    report = _two_turn_chat(chat_report)
    out = tmp_path / "report.md"
    chat_report.write_locked_md_report(out, report)
    md = out.read_text(encoding="utf-8")
    # The per-turn-timestamps warning is always emitted for Cursor.
    assert "cursor-per-turn-timestamps-synthesized" in md
    assert "Warnings" in md or "warnings" in md.lower()


def test_md_single_chat_denials_section_appears_when_present(chat_report, tmp_path):
    chat_id = "f9a48f94-b698-455e-946d-9734c86babe9"
    report = _build_locked(chat_report, chat_id, [
        ("u", fx.make_user_bubble("u")),
        ("g", fx.make_tool_call_bubble(
            "g", tool_name="ripgrep_raw_search", params={"pattern": "p"},
            is_denial_marker="This workspace routes all search through Optimus MCP",
        )),
    ])
    out = tmp_path / "report.md"
    chat_report.write_locked_md_report(out, report)
    md = out.read_text(encoding="utf-8")

    assert "Denials" in md
    assert "ripgrep_raw_search" in md


def test_md_single_chat_handles_inf_ratio(chat_report, tmp_path):
    """Renderer must not crash when Component A ratio is inf (zero broad-sweep + positive optimus)."""
    chat_id = "f9a48f94-b698-455e-946d-9734c86babe9"
    report = _build_locked(chat_report, chat_id, [
        ("u", fx.make_user_bubble("u")),
        ("o", fx.make_tool_call_bubble("o", tool_name="optimus_search", params={"query": "x"})),
    ])
    out = tmp_path / "report.md"
    chat_report.write_locked_md_report(out, report)
    md = out.read_text(encoding="utf-8")

    assert "Component A" in md
    # Ratio should display as inf or a sentinel string -- not crash.
    assert ("inf" in md.lower()) or ("∞" in md) or ("infinite" in md.lower())


def test_md_diff_renders_delta_tables(chat_report, tmp_path):
    before = _two_turn_chat(chat_report)
    after = _build_locked(chat_report, "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", [
        ("u", fx.make_user_bubble("u")),
        ("o0", fx.make_tool_call_bubble("o0", tool_name="optimus_search",
                                        params={"query": "x"})),
        ("o1", fx.make_tool_call_bubble("o1", tool_name="optimus_grep",
                                        params={"pattern": "y"})),
    ])
    diff = chat_report.build_locked_diff_report(before, after)
    out = tmp_path / "diff.md"
    chat_report.write_locked_md_diff(out, diff)
    md = out.read_text(encoding="utf-8")

    assert "Diff" in md
    assert "Before" in md
    assert "After" in md
    # Delta arithmetic surfaces somewhere.
    assert "+1" in md or "+2" in md or "delta" in md.lower()


def test_md_aggregate_renders_per_session_inventory(chat_report, tmp_path):
    s1 = _build_locked(chat_report, "11111111-1111-1111-1111-111111111111", [
        ("u", fx.make_user_bubble("u")),
        ("r", fx.make_tool_call_bubble("r", tool_name="read_file_v2",
                                       params={"targetFile": "src/a.py"})),
    ])
    s2 = _build_locked(chat_report, "22222222-2222-2222-2222-222222222222", [
        ("u", fx.make_user_bubble("u")),
        ("o", fx.make_tool_call_bubble("o", tool_name="optimus_search",
                                       params={"query": "y"})),
    ])
    agg = chat_report.build_locked_aggregate_report([s1, s2])
    out = tmp_path / "agg.md"
    chat_report.write_locked_md_aggregate(out, agg)
    md = out.read_text(encoding="utf-8")

    assert "Aggregate" in md
    assert "2 sessions" in md.lower() or "Sessions: 2" in md or "session_count" in md.lower()
    # Both session IDs surface in the per-session inventory.
    assert "11111111" in md
    assert "22222222" in md


def test_md_aggregate_renders_summed_success_metric(chat_report, tmp_path):
    s1 = _build_locked(chat_report, "11111111-1111-1111-1111-111111111111", [
        ("u", fx.make_user_bubble("u")),
        ("o0", fx.make_tool_call_bubble("o0", tool_name="optimus_search",
                                        params={"query": "x"})),
        ("o1", fx.make_tool_call_bubble("o1", tool_name="optimus_grep",
                                        params={"pattern": "y"})),
    ])
    s2 = _build_locked(chat_report, "22222222-2222-2222-2222-222222222222", [
        ("u", fx.make_user_bubble("u")),
        ("r", fx.make_tool_call_bubble("r", tool_name="read_file_v2",
                                       params={"targetFile": "src/a.py"})),
    ])
    agg = chat_report.build_locked_aggregate_report([s1, s2])
    out = tmp_path / "agg.md"
    chat_report.write_locked_md_aggregate(out, agg)
    md = out.read_text(encoding="utf-8")

    # Summed: 2 optimus / 1 broad-sweep = 2.0 -> Component A PASS
    assert "Component A" in md
    assert "PASS" in md or "True" in md or "✓" in md or "pass" in md.lower()


# ---------------------------------------------------------------------------
# CLI plumbing -- markdown gets written when shape=locked + fmt in (md,both)
# ---------------------------------------------------------------------------

def _stub_build_report_factory(fixtures: dict[str, list]):
    """Return a build_report stub that emits a fake legacy report with the given bubbles."""
    def _stub(chat_id, state_con, track_con, tools, input_id=None, resolved_from=None):
        ordered = fx.make_ordered(*fixtures[chat_id])
        meta = fx.make_meta(chat_id=chat_id)
        return {
            "chatId": chat_id, "meta": meta,
            "_orderedRaw": ordered, "_perToolRollup": {},
            "summary": {"toolCallsIncluded": 0, "bubbleCount": 0, "userBubbles": 0,
                        "assistantBubbles": 0, "errorCount": 0, "denialCount": 0,
                        "toolCallsByName": {}, "toolCallsByCategory": {},
                        "statusCounts": {}, "thinkingBlockCount": 0},
            "toolTrajectory": [], "optimusCalls": [],
            "denials": [], "errors": [],
            "attribution": {"codeHashesBySource": {}, "deletedFiles": [], "scoredCommits": []},
            "bubbles": [],
        }
    return _stub


def test_run_single_mode_locked_writes_md(chat_report, tmp_path, monkeypatch):
    chat_id = "f9a48f94-b698-455e-946d-9734c86babe9"
    monkeypatch.setattr(chat_report, "build_report", _stub_build_report_factory({
        chat_id: [
            ("u", fx.make_user_bubble("u")),
            ("r", fx.make_tool_call_bubble("r", tool_name="read_file_v2",
                                           params={"targetFile": "src/a.py"})),
        ],
    }))
    rc = chat_report._run_single_mode(
        resolved=[(chat_id, chat_id, None)],
        state_con=None, track_con=None,
        tools="all", fmt="both", out_dir=tmp_path,
        dump_bubbles=False, shape="locked",
    )
    assert rc == 0
    assert (tmp_path / f"{chat_id}.md").exists()
    assert (tmp_path / f"{chat_id}.json").exists()


def test_run_diff_mode_locked_writes_md(chat_report, tmp_path, monkeypatch):
    chat_a = "f9a48f94-b698-455e-946d-9734c86babe9"
    chat_b = "84110591-80f7-4b81-9ccd-59eaba77c8b7"
    monkeypatch.setattr(chat_report, "build_report", _stub_build_report_factory({
        chat_a: [("u", fx.make_user_bubble("u")),
                 ("r", fx.make_tool_call_bubble("r", tool_name="read_file_v2",
                                                params={"targetFile": "src/a.py"}))],
        chat_b: [("u", fx.make_user_bubble("u")),
                 ("o", fx.make_tool_call_bubble("o", tool_name="optimus_search",
                                                params={"query": "y"}))],
    }))
    rc = chat_report._run_diff_mode(
        resolved=[(chat_a, chat_a, None), (chat_b, chat_b, None)],
        state_con=None, track_con=None,
        tools="all", fmt="both", out_dir=tmp_path,
        shape="locked",
    )
    assert rc == 0
    md_files = list(tmp_path.glob("diff-*.md"))
    assert len(md_files) == 1


def test_run_aggregate_mode_locked_writes_md(chat_report, tmp_path, monkeypatch):
    chat_a = "11111111-1111-1111-1111-111111111111"
    chat_b = "22222222-2222-2222-2222-222222222222"
    monkeypatch.setattr(chat_report, "build_report", _stub_build_report_factory({
        chat_a: [("u", fx.make_user_bubble("u")),
                 ("r", fx.make_tool_call_bubble("r", tool_name="read_file_v2",
                                                params={"targetFile": "src/a.py"}))],
        chat_b: [("u", fx.make_user_bubble("u")),
                 ("o", fx.make_tool_call_bubble("o", tool_name="optimus_search",
                                                params={"query": "y"}))],
    }))
    rc = chat_report._run_aggregate_mode(
        resolved=[(chat_a, chat_a, None), (chat_b, chat_b, None)],
        state_con=None, track_con=None,
        tools="all", fmt="both", out_dir=tmp_path,
        shape="locked",
    )
    assert rc == 0
    md_files = list(tmp_path.glob("aggregate-*.md"))
    assert len(md_files) == 1
