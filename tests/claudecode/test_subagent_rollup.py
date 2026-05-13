"""Tests for ``aggregates.subagent_rollup`` (PR-B3B commit 4).

The rollup is an additive optional field on ``aggregates`` per
DISCOVERY.md consideration #4 (PM-signed-off 2026-05-12). It's emitted
when the main session JSONL has a sibling ``<session-uuid>/subagents/``
directory containing ``agent-<id>.jsonl`` files. The primary session's
``aggregates.by_class`` is unchanged: subagent tool calls are
deliberately tracked separately so the success metric components A/B
reflect main-agent behavior only.

Per-subagent shape: ``{agent_id, agent_type, description, by_class}``.
``agent_type`` and ``description`` come from the ``<agent-id>.meta.json``
sidecar (DISCOVERY.md Q1); missing sidecar -> empty strings.

Validator tolerance of the additive field is already pinned in
``tests/common/test_locked_contract_extra_keys.py`` (7 cases added in
PR-B3A commit 1).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from common import _locked_contract as contract

from claudecode._paths import subagents_dir_from_jsonl
from claudecode._report import build_locked_report, build_subagent_rollup

from . import _fixtures as fx


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_main_session(jsonl_path: Path, records: list[dict]) -> None:
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    fx.write_jsonl(jsonl_path, records)


def _write_subagent(
    subagents_dir: Path,
    agent_id: str,
    *,
    records: list[dict],
    agent_type: str = "general-purpose",
    description: str = "Synthetic subagent",
) -> Path:
    """Write a subagent JSONL + meta sidecar."""
    subagents_dir.mkdir(parents=True, exist_ok=True)
    jp = subagents_dir / f"agent-{agent_id}.jsonl"
    # Subagent records carry isSidechain=true and a fresh agentId.
    sa_records = [
        {**r, "isSidechain": True, "agentId": agent_id}
        for r in records
    ]
    fx.write_jsonl(jp, sa_records)
    mp = subagents_dir / f"agent-{agent_id}.meta.json"
    mp.write_text(
        json.dumps({"agentType": agent_type, "description": description}),
        encoding="utf-8",
    )
    return jp


def _read_records():
    return [
        fx.user_typed_string("explore"),
        fx.assistant_tool_use("Read", tool_input={"file_path": "a.py"},
                              tool_use_id="t1"),
        fx.user_tool_result("t1", "contents"),
    ]


def _grep_records():
    return [
        fx.assistant_tool_use("Grep", tool_input={"pattern": "TODO"},
                              tool_use_id="g1"),
        fx.user_tool_result("g1", "lines"),
    ]


def _optimus_records():
    return [
        fx.assistant_tool_use("mcp__optimus__optimus_search",
                              tool_input={"query": "x"}, tool_use_id="o1"),
        fx.user_tool_result("o1", "results"),
    ]


# ---------------------------------------------------------------------------
# subagents_dir_from_jsonl
# ---------------------------------------------------------------------------


def test_subagents_dir_paired_with_jsonl_stem(tmp_path):
    """For `<dir>/<sid>.jsonl` the subagents dir is `<dir>/<sid>/subagents/`."""
    sid = "11111111-1111-1111-1111-111111111111"
    jp = tmp_path / "project" / f"{sid}.jsonl"
    expected = tmp_path / "project" / sid / "subagents"
    assert subagents_dir_from_jsonl(jp) == expected


# ---------------------------------------------------------------------------
# build_subagent_rollup
# ---------------------------------------------------------------------------


def test_rollup_returns_none_when_dir_missing(tmp_path):
    """No subagents dir => no rollup; callers must NOT attach the key."""
    assert build_subagent_rollup(tmp_path / "absent") is None


def test_rollup_returns_none_when_dir_empty(tmp_path):
    d = tmp_path / "empty_subagents"
    d.mkdir()
    assert build_subagent_rollup(d) is None


def test_rollup_classifies_per_agent(tmp_path):
    """Two subagents, distinct tool vocabularies; rollup keeps them separated."""
    sub = tmp_path / "subs"
    _write_subagent(sub, "aaa", records=_read_records())
    _write_subagent(sub, "bbb", records=_optimus_records(),
                    agent_type="optimus-runner", description="Runs optimus")
    rollup = build_subagent_rollup(sub)
    assert rollup is not None
    assert rollup["total_subagent_calls"] == 2  # one Read + one optimus
    by_id = {s["agent_id"]: s for s in rollup["subagents"]}
    assert by_id["aaa"]["by_class"]["broad-sweep-read"] == 1
    assert by_id["aaa"]["by_class"]["optimus-mcp"] == 0
    assert by_id["aaa"]["agent_type"] == "general-purpose"
    assert by_id["bbb"]["by_class"]["optimus-mcp"] == 1
    assert by_id["bbb"]["by_class"]["broad-sweep-read"] == 0
    assert by_id["bbb"]["agent_type"] == "optimus-runner"
    assert by_id["bbb"]["description"] == "Runs optimus"


def test_rollup_missing_meta_sidecar_blanks_fields(tmp_path):
    sub = tmp_path / "subs"
    sub.mkdir()
    jp = sub / "agent-zzz.jsonl"
    fx.write_jsonl(jp, [
        {**fx.assistant_tool_use("Bash", tool_input={"command": "ls"},
                                  tool_use_id="b1"),
         "isSidechain": True, "agentId": "zzz"},
        {**fx.user_tool_result("b1", "out"),
         "isSidechain": True, "agentId": "zzz"},
    ])
    rollup = build_subagent_rollup(sub)
    assert rollup is not None
    s0 = rollup["subagents"][0]
    assert s0["agent_id"] == "zzz"
    assert s0["agent_type"] == ""
    assert s0["description"] == ""
    assert s0["by_class"]["bash"] == 1


def test_rollup_subagents_sorted_deterministic(tmp_path):
    sub = tmp_path / "subs"
    for aid in ("c", "a", "b"):
        _write_subagent(sub, aid, records=_read_records())
    rollup = build_subagent_rollup(sub)
    ids = [s["agent_id"] for s in rollup["subagents"]]
    # Sorted by filename ("agent-a.jsonl", "agent-b.jsonl", "agent-c.jsonl")
    assert ids == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Integration with build_locked_report
# ---------------------------------------------------------------------------


def test_build_locked_report_attaches_rollup_when_subagents_dir_provided(tmp_path):
    sid = "22222222-2222-2222-2222-222222222222"
    main_jp = tmp_path / "p" / f"{sid}.jsonl"
    _write_main_session(main_jp, _read_records())
    sub_dir = subagents_dir_from_jsonl(main_jp)
    _write_subagent(sub_dir, "child1", records=_grep_records())

    main_records = list(__import__("claudecode._jsonl",
                                    fromlist=["iter_records"]).iter_records(main_jp))
    report = build_locked_report(
        main_records,
        session_id_override=sid,
        subagents_dir=sub_dir,
    )
    rollup = report["aggregates"]["subagent_rollup"]
    assert rollup["total_subagent_calls"] == 1
    assert rollup["subagents"][0]["agent_id"] == "child1"
    assert rollup["subagents"][0]["by_class"]["broad-sweep-grep"] == 1


def test_build_locked_report_no_rollup_when_subagents_dir_missing(tmp_path):
    sid = "33333333-3333-3333-3333-333333333333"
    main_jp = tmp_path / "p" / f"{sid}.jsonl"
    _write_main_session(main_jp, _read_records())
    sub_dir = subagents_dir_from_jsonl(main_jp)  # doesn't exist

    main_records = list(__import__("claudecode._jsonl",
                                    fromlist=["iter_records"]).iter_records(main_jp))
    report = build_locked_report(
        main_records,
        session_id_override=sid,
        subagents_dir=sub_dir,
    )
    # No subagents -> key absent (additive omitted, not None).
    assert "subagent_rollup" not in report["aggregates"]


def test_build_locked_report_primary_by_class_unaffected_by_subagent_records(
    tmp_path,
):
    """Main has 1 Read; subagent has 5 Reads + 3 Grep. Main's by_class must
    show 1 Read / 0 Grep regardless of what the subagent did."""
    sid = "44444444-4444-4444-4444-444444444444"
    main_jp = tmp_path / "p" / f"{sid}.jsonl"
    _write_main_session(main_jp, _read_records())  # 1 Read
    sub_dir = subagents_dir_from_jsonl(main_jp)
    # Subagent does 5 Reads + 3 Greps
    sa_records = []
    for i in range(5):
        sa_records.append(fx.assistant_tool_use(
            "Read", tool_input={"file_path": f"f{i}.py"},
            tool_use_id=f"sr{i}"))
        sa_records.append(fx.user_tool_result(f"sr{i}", "x"))
    for i in range(3):
        sa_records.append(fx.assistant_tool_use(
            "Grep", tool_input={"pattern": f"p{i}"},
            tool_use_id=f"sg{i}"))
        sa_records.append(fx.user_tool_result(f"sg{i}", "y"))
    _write_subagent(sub_dir, "heavy", records=sa_records)

    main_records = list(__import__("claudecode._jsonl",
                                    fromlist=["iter_records"]).iter_records(main_jp))
    report = build_locked_report(
        main_records,
        session_id_override=sid,
        subagents_dir=sub_dir,
    )
    assert report["aggregates"]["by_class"]["broad-sweep-read"] == 1
    assert report["aggregates"]["by_class"]["broad-sweep-grep"] == 0
    # Rollup separately captures the subagent's haul
    bc = report["aggregates"]["subagent_rollup"]["subagents"][0]["by_class"]
    assert bc["broad-sweep-read"] == 5
    assert bc["broad-sweep-grep"] == 3
    assert report["aggregates"]["subagent_rollup"]["total_subagent_calls"] == 8


def test_build_locked_report_with_rollup_still_validator_conformant(tmp_path):
    sid = "55555555-5555-5555-5555-555555555555"
    main_jp = tmp_path / "p" / f"{sid}.jsonl"
    _write_main_session(main_jp, _read_records())
    sub_dir = subagents_dir_from_jsonl(main_jp)
    _write_subagent(sub_dir, "alpha", records=_grep_records())

    main_records = list(__import__("claudecode._jsonl",
                                    fromlist=["iter_records"]).iter_records(main_jp))
    report = build_locked_report(
        main_records,
        session_id_override=sid,
        subagents_dir=sub_dir,
    )
    contract.assert_locked_single_chat_conforms(report)


# ---------------------------------------------------------------------------
# CLI integration -- end-to-end single mode picks up subagents on disk
# ---------------------------------------------------------------------------


def test_cli_single_mode_emits_subagent_rollup_when_present(
    claudecode_chat_report, tmp_path,
):
    sid = "66666666-6666-6666-6666-666666666666"
    main_jp = tmp_path / "p" / f"{sid}.jsonl"
    _write_main_session(main_jp, _read_records())
    sub_dir = subagents_dir_from_jsonl(main_jp)
    _write_subagent(sub_dir, "child", records=_optimus_records(),
                    agent_type="optimus-runner",
                    description="Discovery dispatch")

    out = tmp_path / "out"
    rc = claudecode_chat_report.main([
        sid, "--session-jsonl", str(main_jp),
        "--out", str(out), "--format", "json",
    ])
    assert rc == 0
    report = json.loads((out / f"{sid}.json").read_text(encoding="utf-8"))
    rollup = report["aggregates"]["subagent_rollup"]
    assert rollup["total_subagent_calls"] == 1
    assert rollup["subagents"][0]["agent_id"] == "child"
    assert rollup["subagents"][0]["by_class"]["optimus-mcp"] == 1


def test_cli_single_mode_no_rollup_when_no_subagents(claudecode_chat_report,
                                                       tmp_path):
    sid = "77777777-7777-7777-7777-777777777777"
    main_jp = tmp_path / "p" / f"{sid}.jsonl"
    _write_main_session(main_jp, _read_records())

    out = tmp_path / "out"
    claudecode_chat_report.main([
        sid, "--session-jsonl", str(main_jp),
        "--out", str(out), "--format", "json",
    ])
    report = json.loads((out / f"{sid}.json").read_text(encoding="utf-8"))
    assert "subagent_rollup" not in report["aggregates"]


def test_cli_diff_mode_carries_rollup_on_each_side(claudecode_chat_report,
                                                     tmp_path):
    sid_a = "8a8a8a8a-8a8a-8a8a-8a8a-8a8a8a8a8a8a"
    sid_b = "8b8b8b8b-8b8b-8b8b-8b8b-8b8b8b8b8b8b"
    jp_a = tmp_path / "p" / f"{sid_a}.jsonl"
    jp_b = tmp_path / "p" / f"{sid_b}.jsonl"
    _write_main_session(jp_a, _read_records())
    _write_main_session(jp_b, _read_records())
    _write_subagent(subagents_dir_from_jsonl(jp_a), "a_sub",
                    records=_grep_records())
    _write_subagent(subagents_dir_from_jsonl(jp_b), "b_sub",
                    records=_optimus_records())

    out = tmp_path / "out"
    claudecode_chat_report.main([
        sid_a, sid_b, "--diff",
        "--session-jsonl", str(jp_a),
        "--session-jsonl", str(jp_b),
        "--out", str(out), "--format", "json",
    ])
    diff = json.loads(
        (out / f"diff-{sid_a[:12]}-vs-{sid_b[:12]}.json").read_text(encoding="utf-8")
    )
    before_rollup = diff["before"]["aggregates"]["subagent_rollup"]
    after_rollup = diff["after"]["aggregates"]["subagent_rollup"]
    assert before_rollup["subagents"][0]["agent_id"] == "a_sub"
    assert after_rollup["subagents"][0]["agent_id"] == "b_sub"
