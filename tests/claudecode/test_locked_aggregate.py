"""Tests for the claudecode --aggregate mode (PR-B3B commit 3).

Drives ``claudecode/chat-report.py main(--aggregate id1 id2 id3)`` end-to-
end against N synthetic JSONL fixtures and asserts:

* file emission (json + md) under the expected ``aggregate-<ISO>`` stem
* validator conformance via ``common._locked_contract.assert_locked_aggregate_conforms``
* propagation of ``ide == "claude-code"`` (math is IDE-neutral and
  separately tested in ``tests/common/test_diff_aggregate.py``)
* CLI guards: rc!=0 on 0-id ``--aggregate`` (impossible -- argparse's
  ``nargs="+"`` rejects it), on mismatched ``--session-jsonl`` count,
  on missing fixtures
* --format md / json / both honored
* basic markdown content sanity (per-session inventory table + summed
  success-metric snapshot)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from common import _locked_contract as contract

from . import _fixtures as fx


_AGG_STEM_RE = re.compile(r"^aggregate-\d{8}T\d{6}Z$")


def _write_session(
    project_dir: Path,
    sid: str,
    *,
    records: list[dict],
    version: str = "2.1.140",
) -> Path:
    project_dir.mkdir(parents=True, exist_ok=True)
    jsonl = project_dir / f"{sid}.jsonl"
    records = [{**r, "version": version} for r in records]
    fx.write_jsonl(jsonl, records)
    return jsonl


def _read_records():
    return [
        fx.user_typed_string("explore"),
        fx.assistant_tool_use("Read", tool_input={"file_path": "a.py"},
                              tool_use_id="t1"),
        fx.user_tool_result("t1", "contents"),
    ]


def _optimus_records():
    return [
        fx.user_typed_string("use optimus"),
        fx.assistant_tool_use("mcp__optimus__optimus_search",
                              tool_input={"query": "x"}, tool_use_id="o1"),
        fx.user_tool_result("o1", "results"),
    ]


def _bash_records():
    return [
        fx.user_typed_string("run a command"),
        fx.assistant_tool_use("Bash", tool_input={"command": "ls"},
                              tool_use_id="b1"),
        fx.user_tool_result("b1", "files"),
    ]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_cli_aggregate_emits_both_formats(claudecode_chat_report, tmp_path):
    project = tmp_path / "p"
    sids = ["aaaa", "bbbb", "cccc"]
    paths = [
        _write_session(project, sids[0], records=_read_records()),
        _write_session(project, sids[1], records=_optimus_records()),
        _write_session(project, sids[2], records=_bash_records()),
    ]
    out = tmp_path / "out"

    rc = claudecode_chat_report.main([
        *sids, "--aggregate",
        "--session-jsonl", str(paths[0]),
        "--session-jsonl", str(paths[1]),
        "--session-jsonl", str(paths[2]),
        "--out", str(out),
    ])
    assert rc == 0

    json_files = list(out.glob("aggregate-*.json"))
    md_files = list(out.glob("aggregate-*.md"))
    assert len(json_files) == 1
    assert len(md_files) == 1
    assert _AGG_STEM_RE.match(json_files[0].stem)


def test_cli_aggregate_json_conforms_to_contract(claudecode_chat_report,
                                                   tmp_path):
    project = tmp_path / "p"
    sids = ["aa", "bb"]
    paths = [
        _write_session(project, sids[0], records=_read_records()),
        _write_session(project, sids[1], records=_optimus_records()),
    ]
    out = tmp_path / "out"
    claudecode_chat_report.main([
        *sids, "--aggregate",
        "--session-jsonl", str(paths[0]),
        "--session-jsonl", str(paths[1]),
        "--out", str(out), "--format", "json",
    ])
    agg = json.loads(list(out.glob("aggregate-*.json"))[0].read_text(encoding="utf-8"))
    contract.assert_locked_aggregate_conforms(agg)
    assert agg["ide"] == "claude-code"
    assert agg["report_kind"] == "aggregate"
    assert agg["session_count"] == 2
    assert len(agg["sessions"]) == 2


def test_cli_aggregate_sums_by_class_across_sessions(claudecode_chat_report,
                                                       tmp_path):
    """3 sessions: 1 read + 1 optimus + 1 bash. Summed counts: read=1,
    optimus-mcp=1, bash=1, others=0. Component A passes (1 >= 1)."""
    project = tmp_path / "p"
    sids = ["aa", "bb", "cc"]
    paths = [
        _write_session(project, sids[0], records=_read_records()),
        _write_session(project, sids[1], records=_optimus_records()),
        _write_session(project, sids[2], records=_bash_records()),
    ]
    out = tmp_path / "out"
    claudecode_chat_report.main([
        *sids, "--aggregate",
        "--session-jsonl", str(paths[0]),
        "--session-jsonl", str(paths[1]),
        "--session-jsonl", str(paths[2]),
        "--out", str(out), "--format", "json",
    ])
    agg = json.loads(list(out.glob("aggregate-*.json"))[0].read_text(encoding="utf-8"))
    by_class = agg["aggregates"]["by_class"]
    assert by_class["broad-sweep-read"] == 1
    assert by_class["optimus-mcp"] == 1
    assert by_class["bash"] == 1
    assert agg["aggregates"]["total_tool_calls"] == 3
    assert agg["aggregates"]["success_metric_components"]["component_a"]["pass"] is True


def test_cli_aggregate_single_session(claudecode_chat_report, tmp_path):
    """N=1 is a degenerate aggregate but still valid and useful."""
    project = tmp_path / "p"
    sid = "solo"
    jp = _write_session(project, sid, records=_optimus_records())
    out = tmp_path / "out"
    rc = claudecode_chat_report.main([
        sid, "--aggregate",
        "--session-jsonl", str(jp),
        "--out", str(out), "--format", "json",
    ])
    assert rc == 0
    agg = json.loads(list(out.glob("aggregate-*.json"))[0].read_text(encoding="utf-8"))
    assert agg["session_count"] == 1


# ---------------------------------------------------------------------------
# Format filtering
# ---------------------------------------------------------------------------


def test_cli_aggregate_format_json_only(claudecode_chat_report, tmp_path):
    project = tmp_path / "p"
    sids = ["a1", "a2"]
    paths = [
        _write_session(project, sids[0], records=_read_records()),
        _write_session(project, sids[1], records=_optimus_records()),
    ]
    out = tmp_path / "out"
    claudecode_chat_report.main([
        *sids, "--aggregate",
        "--session-jsonl", str(paths[0]),
        "--session-jsonl", str(paths[1]),
        "--out", str(out), "--format", "json",
    ])
    assert list(out.glob("aggregate-*.json"))
    assert not list(out.glob("aggregate-*.md"))


def test_cli_aggregate_format_md_only(claudecode_chat_report, tmp_path):
    project = tmp_path / "p"
    sids = ["m1", "m2"]
    paths = [
        _write_session(project, sids[0], records=_read_records()),
        _write_session(project, sids[1], records=_optimus_records()),
    ]
    out = tmp_path / "out"
    claudecode_chat_report.main([
        *sids, "--aggregate",
        "--session-jsonl", str(paths[0]),
        "--session-jsonl", str(paths[1]),
        "--out", str(out), "--format", "md",
    ])
    assert list(out.glob("aggregate-*.md"))
    assert not list(out.glob("aggregate-*.json"))


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_cli_aggregate_mismatched_session_jsonl_rejected(
    claudecode_chat_report, tmp_path, capsys
):
    project = tmp_path / "p"
    sids = ["a", "b", "c"]
    _write_session(project, sids[0], records=_read_records())
    rc = claudecode_chat_report.main([
        *sids, "--aggregate",
        "--session-jsonl", str(project / f"{sids[0]}.jsonl"),  # only one
        "--out", str(tmp_path / "out"),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--session-jsonl" in err


def test_cli_aggregate_missing_fixture_returns_nonzero(claudecode_chat_report,
                                                         tmp_path):
    project = tmp_path / "p"
    sids = ["exists", "absent"]
    j_exists = _write_session(project, sids[0], records=_read_records())
    rc = claudecode_chat_report.main([
        *sids, "--aggregate",
        "--session-jsonl", str(j_exists),
        "--session-jsonl", str(tmp_path / "absent.jsonl"),
        "--out", str(tmp_path / "out"),
    ])
    assert rc == 2


# ---------------------------------------------------------------------------
# MD rendering smoke
# ---------------------------------------------------------------------------


def test_cli_aggregate_md_renders_inventory_and_summary(claudecode_chat_report,
                                                          tmp_path):
    project = tmp_path / "p"
    sids = ["aabb", "ccdd"]
    paths = [
        _write_session(project, sids[0], records=_read_records()),
        _write_session(project, sids[1], records=_optimus_records()),
    ]
    out = tmp_path / "out"
    claudecode_chat_report.main([
        *sids, "--aggregate",
        "--session-jsonl", str(paths[0]),
        "--session-jsonl", str(paths[1]),
        "--out", str(out), "--format", "md",
    ])
    md = list(out.glob("aggregate-*.md"))[0].read_text(encoding="utf-8")
    assert "claude-code" in md
    # Per-session inventory: both session-id prefixes should appear.
    assert sids[0][:12] in md
    assert sids[1][:12] in md
    # Summed success metric block must be present.
    assert "Component A" in md
    assert "Component B" in md


# ---------------------------------------------------------------------------
# Cross-session ide_version
# ---------------------------------------------------------------------------


def test_cli_aggregate_ide_version_mixed_when_versions_differ(
    claudecode_chat_report, tmp_path
):
    project = tmp_path / "p"
    sids = ["v1", "v2"]
    paths = [
        _write_session(project, sids[0], records=_read_records(),
                       version="2.1.140"),
        _write_session(project, sids[1], records=_optimus_records(),
                       version="2.2.0"),
    ]
    out = tmp_path / "out"
    claudecode_chat_report.main([
        *sids, "--aggregate",
        "--session-jsonl", str(paths[0]),
        "--session-jsonl", str(paths[1]),
        "--out", str(out), "--format", "json",
    ])
    agg = json.loads(list(out.glob("aggregate-*.json"))[0].read_text(encoding="utf-8"))
    assert agg["ide_version"] == "mixed"
