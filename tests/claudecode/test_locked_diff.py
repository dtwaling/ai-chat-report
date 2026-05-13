"""Tests for the claudecode --diff mode (PR-B3B commit 2).

Drives ``claudecode/chat-report.py main(--diff a b)`` end-to-end against
two synthetic JSONL fixtures and asserts:

* file emission (json + md) under the expected ``diff-A-vs-B`` stem
* validator conformance via ``common._locked_contract.assert_locked_diff_conforms``
* propagation of ``ide == "claude-code"`` (the diff math itself is
  IDE-neutral and tested in ``tests/common/test_diff_aggregate.py``; here
  we pin the variant-specific wiring)
* CLI guards: rc!=0 on 1-id or 3-id ``--diff`` invocations, on missing
  fixtures, on empty fixtures
* --format md / json / both honored
* basic markdown content sanity (summary table + success-metric deltas)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from common import _locked_contract as contract

from . import _fixtures as fx


_DELIM = re.compile(r"^[A-Za-z0-9]+$")


def _write_session(
    project_dir: Path,
    sid: str,
    *,
    records: list[dict],
    version: str = "2.1.140",
) -> Path:
    """Write a synthetic session JSONL under a project dir."""
    project_dir.mkdir(parents=True, exist_ok=True)
    jsonl = project_dir / f"{sid}.jsonl"
    # Force each record's version so the per-record helper picks up our value
    # rather than the fixture's _DEFAULT_VERSION constant.
    records = [{**r, "version": version} for r in records]
    fx.write_jsonl(jsonl, records)
    return jsonl


def _before_records():
    """Two reads, zero optimus, one bash error."""
    return [
        fx.user_typed_string("explore the repo"),
        fx.assistant_tool_use("Read", tool_input={"file_path": "a.py"},
                              tool_use_id="t1"),
        fx.user_tool_result("t1", "contents-a"),
        fx.assistant_tool_use("Read", tool_input={"file_path": "b.py"},
                              tool_use_id="t2"),
        fx.user_tool_result("t2", "contents-b"),
    ]


def _after_records():
    """One optimus call, one bash, one read."""
    return [
        fx.user_typed_string("now use optimus"),
        fx.assistant_tool_use("mcp__optimus__optimus_search",
                              tool_input={"query": "foo"}, tool_use_id="u1"),
        fx.user_tool_result("u1", "results"),
        fx.assistant_tool_use("Bash", tool_input={"command": "ls"},
                              tool_use_id="u2"),
        fx.user_tool_result("u2", "files"),
        fx.assistant_tool_use("Read", tool_input={"file_path": "c.py"},
                              tool_use_id="u3"),
        fx.user_tool_result("u3", "contents-c"),
    ]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_cli_diff_emits_both_formats(claudecode_chat_report, tmp_path):
    project = tmp_path / "p"
    sid_a = "11111111-1111-1111-1111-111111111111"
    sid_b = "22222222-2222-2222-2222-222222222222"
    jsonl_a = _write_session(project, sid_a, records=_before_records())
    jsonl_b = _write_session(project, sid_b, records=_after_records())
    out = tmp_path / "out"

    rc = claudecode_chat_report.main([
        sid_a, sid_b, "--diff",
        "--session-jsonl", str(jsonl_a),
        "--session-jsonl", str(jsonl_b),
        "--out", str(out),
    ])
    assert rc == 0

    stem = f"diff-{sid_a[:12]}-vs-{sid_b[:12]}"
    assert (out / f"{stem}.json").exists()
    assert (out / f"{stem}.md").exists()


def test_cli_diff_json_conforms_to_contract(claudecode_chat_report, tmp_path):
    project = tmp_path / "p"
    sid_a, sid_b = "aaaa", "bbbb"
    ja = _write_session(project, sid_a, records=_before_records())
    jb = _write_session(project, sid_b, records=_after_records())
    out = tmp_path / "out"
    claudecode_chat_report.main([
        sid_a, sid_b, "--diff",
        "--session-jsonl", str(ja),
        "--session-jsonl", str(jb),
        "--out", str(out), "--format", "json",
    ])
    diff = json.loads((out / f"diff-{sid_a[:12]}-vs-{sid_b[:12]}.json").read_text(encoding="utf-8"))
    contract.assert_locked_diff_conforms(diff)
    assert diff["ide"] == "claude-code"
    assert diff["report_kind"] == "diff"


def test_cli_diff_delta_arithmetic_end_to_end(claudecode_chat_report, tmp_path):
    """Counts: before = 2 reads + 0 optimus + 0 bash; after = 1 read + 1
    optimus + 1 bash. Deltas should be: read -1, optimus +1, bash +1."""
    project = tmp_path / "p"
    sid_a, sid_b = "abefore", "aafter"
    ja = _write_session(project, sid_a, records=_before_records())
    jb = _write_session(project, sid_b, records=_after_records())
    out = tmp_path / "out"
    claudecode_chat_report.main([
        sid_a, sid_b, "--diff",
        "--session-jsonl", str(ja),
        "--session-jsonl", str(jb),
        "--out", str(out), "--format", "json",
    ])
    diff = json.loads((out / f"diff-{sid_a[:12]}-vs-{sid_b[:12]}.json").read_text(encoding="utf-8"))
    by_class = diff["delta"]["by_class"]
    assert by_class["broad-sweep-read"] == -1
    assert by_class["optimus-mcp"] == 1
    assert by_class["bash"] == 1
    # Component A: 0 -> 1 optimus, 2 -> 1 broad_sweep
    assert diff["delta"]["component_a"]["optimus_count_delta"] == 1
    assert diff["delta"]["component_a"]["broad_sweep_count_delta"] == -1
    # pass_before=False (0/2), pass_after=True (1/1)
    assert diff["delta"]["component_a"]["pass_before"] is False
    assert diff["delta"]["component_a"]["pass_after"] is True


# ---------------------------------------------------------------------------
# Format filtering
# ---------------------------------------------------------------------------


def test_cli_diff_format_json_only(claudecode_chat_report, tmp_path):
    project = tmp_path / "p"
    sid_a, sid_b = "ja", "jb"
    ja = _write_session(project, sid_a, records=_before_records())
    jb = _write_session(project, sid_b, records=_after_records())
    out = tmp_path / "out"
    rc = claudecode_chat_report.main([
        sid_a, sid_b, "--diff",
        "--session-jsonl", str(ja),
        "--session-jsonl", str(jb),
        "--out", str(out), "--format", "json",
    ])
    assert rc == 0
    stem = f"diff-{sid_a[:12]}-vs-{sid_b[:12]}"
    assert (out / f"{stem}.json").exists()
    assert not (out / f"{stem}.md").exists()


def test_cli_diff_format_md_only(claudecode_chat_report, tmp_path):
    project = tmp_path / "p"
    sid_a, sid_b = "ma", "mb"
    ja = _write_session(project, sid_a, records=_before_records())
    jb = _write_session(project, sid_b, records=_after_records())
    out = tmp_path / "out"
    rc = claudecode_chat_report.main([
        sid_a, sid_b, "--diff",
        "--session-jsonl", str(ja),
        "--session-jsonl", str(jb),
        "--out", str(out), "--format", "md",
    ])
    assert rc == 0
    stem = f"diff-{sid_a[:12]}-vs-{sid_b[:12]}"
    assert (out / f"{stem}.md").exists()
    assert not (out / f"{stem}.json").exists()


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_cli_diff_rejects_one_id(claudecode_chat_report, tmp_path, capsys):
    rc = claudecode_chat_report.main([
        "only-one", "--diff",
        "--session-jsonl", str(tmp_path / "x.jsonl"),
        "--out", str(tmp_path / "out"),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "exactly two" in err


def test_cli_diff_rejects_three_ids(claudecode_chat_report, tmp_path, capsys):
    rc = claudecode_chat_report.main([
        "a", "b", "c", "--diff",
        "--session-jsonl", str(tmp_path / "x.jsonl"),
        "--session-jsonl", str(tmp_path / "y.jsonl"),
        "--session-jsonl", str(tmp_path / "z.jsonl"),
        "--out", str(tmp_path / "out"),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "exactly two" in err


def test_cli_diff_missing_fixture_returns_nonzero(claudecode_chat_report,
                                                    tmp_path, capsys):
    project = tmp_path / "p"
    sid_a, sid_b = "exists", "absent"
    ja = _write_session(project, sid_a, records=_before_records())
    rc = claudecode_chat_report.main([
        sid_a, sid_b, "--diff",
        "--session-jsonl", str(ja),
        "--session-jsonl", str(tmp_path / "absent.jsonl"),
        "--out", str(tmp_path / "out"),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found" in err


def test_cli_diff_empty_jsonl_returns_nonzero(claudecode_chat_report, tmp_path):
    sid_a, sid_b = "fulla", "emptyb"
    project = tmp_path / "p"
    ja = _write_session(project, sid_a, records=_before_records())
    jb = project / f"{sid_b}.jsonl"
    project.mkdir(parents=True, exist_ok=True)
    jb.write_text("", encoding="utf-8")
    rc = claudecode_chat_report.main([
        sid_a, sid_b, "--diff",
        "--session-jsonl", str(ja),
        "--session-jsonl", str(jb),
        "--out", str(tmp_path / "out"),
    ])
    assert rc == 2


def test_cli_diff_mismatched_session_jsonl_count_rejected(claudecode_chat_report,
                                                           tmp_path, capsys):
    """Two ids but one --session-jsonl: ambiguous; reject."""
    project = tmp_path / "p"
    sid_a, sid_b = "ma", "mb"
    ja = _write_session(project, sid_a, records=_before_records())
    rc = claudecode_chat_report.main([
        sid_a, sid_b, "--diff",
        "--session-jsonl", str(ja),  # only one given for two ids
        "--out", str(tmp_path / "out"),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--session-jsonl" in err


# ---------------------------------------------------------------------------
# MD rendering smoke
# ---------------------------------------------------------------------------


def test_cli_diff_md_renders_summary_and_deltas(claudecode_chat_report, tmp_path):
    project = tmp_path / "p"
    sid_a, sid_b = "ma", "mb"
    ja = _write_session(project, sid_a, records=_before_records())
    jb = _write_session(project, sid_b, records=_after_records())
    out = tmp_path / "out"
    claudecode_chat_report.main([
        sid_a, sid_b, "--diff",
        "--session-jsonl", str(ja),
        "--session-jsonl", str(jb),
        "--out", str(out), "--format", "md",
    ])
    md = (out / f"diff-{sid_a[:12]}-vs-{sid_b[:12]}.md").read_text(encoding="utf-8")
    # Must mention both session IDs, the summary table, and success-metric deltas.
    assert sid_a[:12] in md
    assert sid_b[:12] in md
    assert "claude-code" in md
    assert "Before vs After" in md or "Summary" in md
    assert "Component A" in md
    assert "Component B" in md


# ---------------------------------------------------------------------------
# Cross-session ide_version flag
# ---------------------------------------------------------------------------


def test_cli_diff_ide_version_mixed_when_versions_differ(claudecode_chat_report,
                                                           tmp_path):
    project = tmp_path / "p"
    sid_a, sid_b = "va", "vb"
    ja = _write_session(project, sid_a, records=_before_records(), version="2.1.140")
    jb = _write_session(project, sid_b, records=_after_records(), version="2.2.0")
    out = tmp_path / "out"
    claudecode_chat_report.main([
        sid_a, sid_b, "--diff",
        "--session-jsonl", str(ja),
        "--session-jsonl", str(jb),
        "--out", str(out), "--format", "json",
    ])
    diff = json.loads((out / f"diff-{sid_a[:12]}-vs-{sid_b[:12]}.json").read_text(encoding="utf-8"))
    assert diff["ide_version"] == "mixed"
