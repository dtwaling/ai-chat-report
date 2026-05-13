"""End-to-end contract conformance tests for the claudecode locked-shape emitter.

Spins up synthetic JSONL fixtures, runs the full build_locked_report
pipeline against them, and validates the emitted dict against the
common/_locked_contract.py validator. Plus a real-world smoke that runs
against this machine's live session JSONL when available.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from common import _locked_contract as contract

from claudecode._jsonl import iter_records
from claudecode._report import (
    build_locked_report,
    write_locked_json_report,
    write_locked_md_report,
)

from . import _fixtures as fx


# ---------------------------------------------------------------------------
# Synthetic fixtures + contract conformance
# ---------------------------------------------------------------------------


def _basic_mixed_session():
    """A realistic-shape session with one user turn + several tool classes."""
    u = fx.user_typed_string("explore the repo")
    a1 = fx.assistant_tool_use("Read",
                                tool_input={"file_path": "DIRECTORY_INDEX.md"},
                                tool_use_id="t1")
    r1 = fx.user_tool_result("t1", "index")
    a2 = fx.assistant_tool_use("Read",
                                tool_input={"file_path": "src/foo.py"},
                                tool_use_id="t2")
    r2 = fx.user_tool_result("t2", "data")
    a3 = fx.assistant_tool_use("Grep",
                                tool_input={"pattern": "TODO", "path": "src/"},
                                tool_use_id="t3")
    r3 = fx.user_tool_result("t3", "src/foo.py:1: TODO\n")
    a4 = fx.assistant_tool_use("Bash",
                                tool_input={"command": "ls"},
                                tool_use_id="t4")
    r4 = fx.user_tool_result("t4", "src/\ntests/\n")
    a5 = fx.assistant_tool_use("mcp__optimus__optimus_search",
                                tool_input={"query": "foo"},
                                tool_use_id="t5")
    r5 = fx.user_tool_result("t5", "results...")
    return [u, a1, r1, a2, r2, a3, r3, a4, r4, a5, r5]


def test_synthetic_session_passes_locked_contract():
    report = build_locked_report(_basic_mixed_session())
    contract.assert_locked_single_chat_conforms(report)


def test_synthetic_session_aggregates_by_class():
    report = build_locked_report(_basic_mixed_session())
    aggs = report["aggregates"]
    assert aggs["total_tool_calls"] == 5
    assert aggs["by_class"]["directory-index-read"] == 1
    assert aggs["by_class"]["broad-sweep-read"] == 1
    assert aggs["by_class"]["broad-sweep-grep"] == 1
    assert aggs["by_class"]["bash"] == 1
    assert aggs["by_class"]["optimus-mcp"] == 1


def test_synthetic_session_component_a_pass():
    """1 optimus call vs 1+1+0 broad-sweep -> ratio 0.5, fail (1 < 2)."""
    report = build_locked_report(_basic_mixed_session())
    a = report["aggregates"]["success_metric_components"]["component_a"]
    assert a["optimus_count"] == 1
    assert a["broad_sweep_count"] == 2  # broad-sweep-read + broad-sweep-grep
    assert a["pass"] is False


def test_zero_call_session_still_conforms():
    """A user prompt with no assistant response still produces a valid report."""
    report = build_locked_report([fx.user_typed_string("hi")])
    contract.assert_locked_single_chat_conforms(report)


def test_session_with_only_orphan_records_still_conforms():
    """Records before any user prompt land in an orphan initial turn."""
    records = [fx.attachment("preamble"), fx.assistant_text("system note")]
    report = build_locked_report(records)
    contract.assert_locked_single_chat_conforms(report)


def test_session_with_denial_conforms():
    u = fx.user_typed_string("write")
    a = fx.assistant_tool_use("Write",
                               tool_input={"file_path": "x", "content": "y"},
                               tool_use_id="t1")
    rejection = "The tool use was rejected by the user."
    r = fx.user_tool_result("t1", rejection, is_error=True)
    report = build_locked_report([u, a, r])
    contract.assert_locked_single_chat_conforms(report)
    assert len(report["aggregates"]["denials"]) == 1


def test_json_round_trip_conforms(tmp_path):
    """Write + re-read the JSON; validator still passes."""
    report = build_locked_report(_basic_mixed_session())
    path = tmp_path / "report.json"
    write_locked_json_report(path, report)
    re_read = json.loads(path.read_text(encoding="utf-8"))
    contract.assert_locked_single_chat_conforms(re_read)


def test_md_writer_runs_clean(tmp_path):
    """Markdown writer produces non-empty output; assertions are structural."""
    report = build_locked_report(_basic_mixed_session())
    path = tmp_path / "report.md"
    write_locked_md_report(path, report)
    md = path.read_text(encoding="utf-8")
    assert "# chat-report (claude-code)" in md
    assert "## Summary by tool class" in md
    assert "## Success metric snapshot" in md
    assert "## Turn-by-turn trajectory" in md


# ---------------------------------------------------------------------------
# Real-world smoke against the live JSONL on this machine
# ---------------------------------------------------------------------------


def test_live_session_jsonl_emits_conforming_report():
    """Smoke: parse this machine's biggest session JSONL, emit, validate."""
    project_dir = Path.home() / ".claude" / "projects" / "C---Source-ai-chat-report"
    if not project_dir.exists():
        pytest.skip("no live Claude Code session data on this host")
    jsonls = sorted(project_dir.glob("*.jsonl"))
    if not jsonls:
        pytest.skip("no session JSONLs in this project's claude dir")
    target = max(jsonls, key=lambda pth: pth.stat().st_size)
    records = list(iter_records(target))
    report = build_locked_report(records, session_id_override=target.stem)
    contract.assert_locked_single_chat_conforms(report)
    # Sanity: a real session has a non-trivial number of tool calls.
    assert report["aggregates"]["total_tool_calls"] > 0
    # And the user_prompt_text additive field is populated on user turns
    # with non-empty text (the actual user typed something).
    user_turns = [t for t in report["turns"] if t["role"] == "user"]
    assert user_turns
    assert any(t.get("user_prompt_text") for t in user_turns)
