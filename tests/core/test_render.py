"""Critical output-contract tests for v2 report renderers."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from chat_report.adapters.cursor.adapter import CursorAdapter
from chat_report.core.adapter import ExtractOptions
from chat_report.core.classifier import classify_session
from chat_report.core.compare import build_aggregate_report, build_diff_report, build_single_session_report
from chat_report.core.render.digest import MAX_DIGEST_OUTPUT_PATH_CHARS, _touched, render_agent_digest
from chat_report.core.render.json_v2 import assert_v2_report_conforms
from chat_report.core.render.markdown import render_aggregate_md, render_diff_md, render_single_session_md
from tests._fixtures import create_synthetic_state_db, create_synthetic_tracking_db


def _report(tmp_path: Path) -> dict:
    state = create_synthetic_state_db(tmp_path / "state.vscdb")
    tracking = create_synthetic_tracking_db(tmp_path / "tracking.db")
    options = ExtractOptions(state_db=state, tracking_db=tracking, workspace=tmp_path)
    session = classify_session(CursorAdapter().extract_session("0a7d3243-61b6-4b3a-8fc5-43c73e2db583", options), tmp_path)
    return build_single_session_report(session, workspace=tmp_path)


def test_single_report_conforms_and_markdown_has_seven_sections(tmp_path: Path) -> None:
    report = _report(tmp_path)
    assert_v2_report_conforms(report)
    markdown = render_single_session_md(report)
    for section in range(1, 8):
        assert f"## {section}." in markdown
    for category in ("system_prompt", "rules", "skills", "tools", "mcp", "subagents", "conversation"):
        assert category in markdown


def test_validator_rejects_wrong_status_total(tmp_path: Path) -> None:
    report = _report(tmp_path)
    report["aggregates"]["status_counts"]["completed"] += 1
    with pytest.raises(ValueError, match="status total"):
        assert_v2_report_conforms(report)


def test_validator_rejects_corrupt_session_turn_diff_and_aggregate(tmp_path: Path) -> None:
    report = _report(tmp_path)
    broken_session = deepcopy(report)
    broken_session["session"].pop("title")
    with pytest.raises(ValueError, match="session keys"):
        assert_v2_report_conforms(broken_session)
    broken_turn = deepcopy(report)
    broken_turn["turns"][0]["tool_calls"][0]["turn_index"] = 99
    with pytest.raises(ValueError, match="turn_index"):
        assert_v2_report_conforms(broken_turn)
    session = CursorAdapter().extract_session(
        report["session"]["session_id"],
        ExtractOptions(state_db=tmp_path / "state.vscdb", tracking_db=tmp_path / "tracking.db"),
    )
    session = classify_session(session, tmp_path)
    diff = build_diff_report(session, session)
    diff["delta"]["thinking_block_count_delta"] = 1
    with pytest.raises(ValueError, match="thinking block delta"):
        assert_v2_report_conforms(diff)
    aggregate = build_aggregate_report([session, session])
    aggregate["rollup"]["total_thinking_blocks"] += 1
    with pytest.raises(ValueError, match="aggregate thinking"):
        assert_v2_report_conforms(aggregate)
    mismatched_subagents = deepcopy(report)
    mismatched_subagents["aggregates"]["customization_summary"]["subagents_spawned"] = 1
    with pytest.raises(ValueError, match="subagent count"):
        assert_v2_report_conforms(mismatched_subagents)
    mismatched_skills = deepcopy(report)
    mismatched_skills["aggregates"]["customization_summary"]["skills_read"] = ["skills/not-read/SKILL.md"]
    with pytest.raises(ValueError, match="skills_read"):
        assert_v2_report_conforms(mismatched_skills)


def test_diff_and_aggregate_render(tmp_path: Path) -> None:
    report = _report(tmp_path)
    session = report["session"]
    state = create_synthetic_state_db(tmp_path / "other.vscdb", session_id="11111111-1111-1111-1111-111111111111")
    tracking = create_synthetic_tracking_db(tmp_path / "other.db", session_id="11111111-1111-1111-1111-111111111111")
    options = ExtractOptions(state_db=state, tracking_db=tracking, workspace=tmp_path)
    other = classify_session(CursorAdapter().extract_session("11111111-1111-1111-1111-111111111111", options), tmp_path)
    before = classify_session(
        CursorAdapter().extract_session(
            session["session_id"],
            ExtractOptions(state_db=tmp_path / "state.vscdb", tracking_db=tmp_path / "tracking.db"),
        ),
        tmp_path,
    )
    diff = build_diff_report(before, other)
    aggregate = build_aggregate_report([before, other])
    assert_v2_report_conforms(diff)
    assert_v2_report_conforms(aggregate)
    diff_markdown = render_diff_md(diff)
    for value in (
        "## 1. Compared Sessions",
        before.session_id,
        other.session_id,
        "## 2. Execution Deltas",
        "Turns: +0; tools: +0",
        "## 3. Customization Changes",
        "Skills added: None",
        "Rules removed: None",
        "## 4. Diagnostic Transitions",
        "New finding IDs: None",
        "## 5. Attribution Delta",
        "Composer hashes: +0; human hashes: +0",
        "Lines: +0 / +0",
    ):
        assert value in diff_markdown
    aggregate_markdown = render_aggregate_md(aggregate)
    for value in (
        "## 1. Cohort Summary",
        "Sessions: 2; turns: 20; tools: 20",
        "## 2. Tool & Status Rollup",
        "native-read",
        "completed",
        "## 3. Diagnostic Cohort Findings",
        "Findings: 0",
        "## 4. Customization Frequencies",
        "Rules: {}",
        "## 5. Attribution Rollup",
        "composer_code_hashes_count",
    ):
        assert value in aggregate_markdown


def test_markdown_sections_and_digest_bounds_under_adversarial_input(tmp_path: Path) -> None:
    report = _report(tmp_path)
    report["session"]["customizations"] = [
        {"kind": "rule", "path": ".cursor/rules/test.mdc", "source": "context_attached", "turn_index": 1},
        {"kind": "skill", "path": "skills/demo/SKILL.md", "source": "explicit_read", "turn_index": 1},
        {"kind": "agent_doc", "path": "AGENTS.md", "source": "explicit_read", "turn_index": 1},
    ]
    report["aggregates"]["customization_summary"].update(
        {
            "attached_rules": [".cursor/rules/test.mdc"],
            "skills_read": ["skills/demo/SKILL.md"],
            "rules_read": ["AGENTS.md"],
            "subagents_spawned": 1,
        }
    )
    report["aggregates"]["subagent_rollup"].update(
        {"count": 1, "by_type": {"generalPurpose": 1}, "by_status": {"completed": 1}, "total_tool_calls": 2, "total_errors": 0}
    )
    report["session"]["subagents"] = [
        {
            "session_id": "child-session",
            "parent_turn_index": 1,
            "subagent_type": "generalPurpose",
            "name": "child",
            "model_name": "test-model",
            "status": "completed",
            "duration_ms": 25,
            "turn_count": 1,
            "tool_call_count": 2,
            "error_count": 0,
            "denial_count": 0,
            "skills_read": ["skills/demo/SKILL.md"],
            "rules_read": ["AGENTS.md"],
        }
    ]
    report["turns"][0]["tool_calls"][0].update({"canonical_kind": "mcp_discovery", "semantic_class": "dynamic-tool-discovery", "mcp_namespace": "demo"})
    report["aggregates"]["tool_calls_by_canonical_kind"] = {"file_read": 9, "mcp_discovery": 1}
    report["aggregates"]["tool_calls_by_semantic_class"] = {"dynamic-tool-discovery": 1, "native-read": 9}
    assert_v2_report_conforms(report)
    report["session"]["reasoning_effort"] = "medium"
    report["session"]["orphan_bubble_count"] = 2
    report["session"]["token_breakdown"]["by_category"]["summarized_conversation"] = {"tokens": 1, "chars": 4}
    markdown = render_single_session_md(report)
    for expected in (
        "### Attached Rules & Docs",
        "### Skills Read (SKILL.md)",
        "### Rules & Agent Docs Read (.mdc, .cursorrules, AGENTS.md, CLAUDE.md)",
        "### MCP Namespaces Discovered & Called",
        "### Subagents Dispatched",
        "avg_ms",
        "max_ms",
        "total_ms",
        "Turn-by-Turn Execution Timeline",
        "Git Code Attribution",
        "Synthetic session",
        "0a7d3243-61b6-4b3a-8fc5-43c73e2db583",
        "request-0",
        "test-model (maxMode=True, reasoning_effort=medium)",
        "Duration: 1950 ms",
        "Status: completed",
        "+3 / -1",
        "Orphan Bubbles Filtered: 2",
        "| system_prompt | 5 | 50.0% |",
        "| rules | 0 | 0.0% |",
        "| skills | 0 | 0.0% |",
        "| tools | 4 | 40.0% |",
        "| mcp | 0 | 0.0% |",
        "| subagents | 0 | 0.0% |",
        "| conversation | 0 | 0.0% |",
        "| summarized_conversation | 1 | 10.0% |",
        ".cursor/rules/test.mdc",
        "skills/demo/SKILL.md",
        "AGENTS.md",
        "demo: 0 calls",
        "Total spawned: 1",
        "generalPurpose: 1",
        "0 anomalies detected",
        "| dynamic-tool-discovery | 1 | 0 | 0 | 50.0 | 50 | 50 |",
        "| native-read | 9 | 0 | 0 | 50.0 | 50 | 450 |",
        "read_file_v2=9",
        "| generalPurpose | `child-session` | 1 | 2 | 0 | skills/demo/SKILL.md | 25 |",
        "### Turn 1",
        "User Prompt Preview: User 0",
        "Thinking (250 ms): reasoning",
        "Tool: `edit_file_v2` [completed] 50 ms",
        "Assistant Output Summary: Assistant 1",
        "Composer code hashes: 1",
        "Human code hashes: 1",
        "README.md, src/example.py",
        "abc: Synthetic",
    ):
        assert expected in markdown
    with_finding = deepcopy(report)
    with_finding["diagnostics"] = {
        "total_findings": 1,
        "by_severity": {"high": 1, "medium": 0, "low": 0, "info": 0},
        "findings": [{
            "id": "H01_UNREAD_ATTACHED_OR_MENTIONED_SKILL",
            "finding_key": "H01_UNREAD_ATTACHED_OR_MENTIONED_SKILL:test",
            "name": "Unread skill",
            "severity": "high",
            "turn_index": 1,
            "bubble_id": "bubble-001",
            "summary": "Read the skill first.",
            "error_message": "test error",
            "thinking_snippet": "test thinking",
            "evidence": {},
            "remediation_hint": "Read it.",
        }],
    }
    rendered_finding = render_single_session_md(with_finding)
    assert "[HIGH] H01_UNREAD_ATTACHED_OR_MENTIONED_SKILL (turn=1, bubble=bubble-001): Read the skill first." in rendered_finding
    assert "Error: test error" in rendered_finding
    assert "Thinking: test thinking" in rendered_finding
    report["session"]["session_id"] = "s" * 64
    report["session"]["model_name"] = "m" * 500
    report["session"]["customizations"] = [
        {
            "kind": "agent_doc" if index == 0 else "skill",
            "path": f"{'x' * 260}{index}/{'AGENTS.md' if index == 0 else 'SKILL.md'}",
            "source": "explicit_read",
            "turn_index": 1,
        }
        for index in range(15)
    ]
    report["diagnostics"]["findings"] = [
        {
            "id": "H01_UNREAD_ATTACHED_OR_MENTIONED_SKILL",
            "finding_key": f"H01_UNREAD_ATTACHED_OR_MENTIONED_SKILL:{index}",
            "name": "Test finding",
            "severity": "medium",
            "turn_index": 1,
            "bubble_id": "bubble",
            "summary": "s" * 150,
            "error_message": "e" * 100,
            "thinking_snippet": "t" * 100,
            "evidence": {},
            "remediation_hint": "Fix it",
        }
        for index in range(25)
    ]
    digest = render_agent_digest(report, "j" * MAX_DIGEST_OUTPUT_PATH_CHARS, "m" * MAX_DIGEST_OUTPUT_PATH_CHARS)
    assert len(digest) <= 2500
    assert "(+" in digest and "more in JSON session.customizations)" in digest
    assert "(+" in digest and "more in JSON diagnostics.findings)" in digest
    assert "Audit Inputs:" in digest
    assert "ctx=" in digest
    assert "\n## Customizations Touched\n" in digest
    with pytest.raises(ValueError):
        render_agent_digest(report, "x" * (MAX_DIGEST_OUTPUT_PATH_CHARS + 1), "ok")


def test_touched_customizations_use_best_priority_and_skip_workspace_active(tmp_path: Path) -> None:
    report = _report(tmp_path)
    report["session"]["customizations"] = [
        {"kind": "rule", "path": "same.mdc", "source": "context_attached", "turn_index": 1},
        {"kind": "rule", "path": "same.mdc", "source": "explicit_read", "turn_index": 1},
        {"kind": "hook", "path": "hooks.json", "source": "explicit_edit", "turn_index": 1},
        {"kind": "skill", "path": "demo/SKILL.md", "source": "context_attached", "turn_index": 1},
        {"kind": "agent_doc", "path": "AGENTS.md", "source": "explicit_read", "turn_index": 1},
        {"kind": "skill", "path": "ignored/SKILL.md", "source": "workspace_active", "turn_index": None},
    ]
    assert _touched(report) == ["demo/SKILL.md", "AGENTS.md", "same.mdc", "hooks.json"]


def test_validator_type_guards_and_distribution_cross_checks(tmp_path: Path) -> None:
    report = _report(tmp_path)

    non_str_finding_key = deepcopy(report)
    non_str_finding_key["diagnostics"] = {
        "total_findings": 1,
        "by_severity": {"high": 1, "medium": 0, "low": 0, "info": 0},
        "findings": [{
            "id": "H01_UNREAD_ATTACHED_OR_MENTIONED_SKILL",
            "finding_key": 12345,
            "name": "Unread skill",
            "severity": "high",
            "turn_index": 1,
            "bubble_id": "bubble-001",
            "summary": "Read the skill first.",
            "error_message": None,
            "thinking_snippet": None,
            "evidence": {},
            "remediation_hint": "Read it.",
        }],
    }
    with pytest.raises(ValueError, match="finding.finding_key must be a string"):
        assert_v2_report_conforms(non_str_finding_key)

    non_str_skills = deepcopy(report)
    non_str_skills["aggregates"]["customization_summary"]["skills_read"] = [123]
    with pytest.raises(ValueError, match="skills_read must be sorted distinct paths"):
        assert_v2_report_conforms(non_str_skills)

    mismatched_severity = deepcopy(report)
    mismatched_severity["diagnostics"] = {
        "total_findings": 1,
        "by_severity": {"high": 0, "medium": 1, "low": 0, "info": 0},
        "findings": [{
            "id": "H01_UNREAD_ATTACHED_OR_MENTIONED_SKILL",
            "finding_key": "H01_UNREAD_ATTACHED_OR_MENTIONED_SKILL:test",
            "name": "Unread skill",
            "severity": "high",
            "turn_index": 1,
            "bubble_id": "bubble-001",
            "summary": "Read the skill first.",
            "error_message": None,
            "thinking_snippet": None,
            "evidence": {},
            "remediation_hint": "Read it.",
        }],
    }
    with pytest.raises(ValueError, match="by_severity distribution mismatch"):
        assert_v2_report_conforms(mismatched_severity)

    mismatched_status = deepcopy(report)
    mismatched_status["aggregates"]["status_counts"]["completed"] -= 1
    mismatched_status["aggregates"]["status_counts"]["error"] += 1
    with pytest.raises(ValueError, match="status counts distribution mismatch"):
        assert_v2_report_conforms(mismatched_status)

    mismatched_tool_elapsed = deepcopy(report)
    mismatched_tool_elapsed["aggregates"]["timing"]["total_tool_elapsed_ms"] += 10
    with pytest.raises(ValueError, match="total tool elapsed ms mismatch"):
        assert_v2_report_conforms(mismatched_tool_elapsed)

    mismatched_thinking_dur = deepcopy(report)
    mismatched_thinking_dur["aggregates"]["total_thinking_duration_ms"] += 5
    with pytest.raises(ValueError, match="total thinking duration mismatch"):
        assert_v2_report_conforms(mismatched_thinking_dur)

    mismatched_session_ms = deepcopy(report)
    mismatched_session_ms["aggregates"]["timing"]["total_session_ms"] = 999999
    with pytest.raises(ValueError, match="timing.total_session_ms mismatch"):
        assert_v2_report_conforms(mismatched_session_ms)

    with_subagent = deepcopy(report)
    with_subagent["session"]["subagents"] = [{
        "session_id": "child-1",
        "parent_turn_index": 1,
        "subagent_type": "explore",
        "name": "explorer",
        "model_name": "test-model",
        "status": "completed",
        "duration_ms": 10,
        "turn_count": 1,
        "tool_call_count": 1,
        "error_count": 0,
        "denial_count": 0,
        "skills_read": [],
        "rules_read": [],
    }]
    with_subagent["aggregates"]["customization_summary"]["subagents_spawned"] = 1
    with_subagent["aggregates"]["subagent_rollup"] = {
        "count": 1,
        "by_type": {"generalPurpose": 1},
        "by_status": {"completed": 1},
        "total_tool_calls": 1,
        "total_errors": 0,
    }
    with pytest.raises(ValueError, match="subagent rollup by_type mismatch"):
        assert_v2_report_conforms(with_subagent)

    bad_subagent_field = deepcopy(with_subagent)
    bad_subagent_field["aggregates"]["subagent_rollup"]["by_type"] = {"explore": 1}
    bad_subagent_field["session"]["subagents"][0]["skills_read"] = [42]
    with pytest.raises(ValueError, match="subagent.skills_read must be a string list"):
        assert_v2_report_conforms(bad_subagent_field)

    session = classify_session(
        CursorAdapter().extract_session(
            report["session"]["session_id"],
            ExtractOptions(state_db=tmp_path / "state.vscdb", tracking_db=tmp_path / "tracking.db"),
        ),
        tmp_path,
    )
    diff = build_diff_report(session, session)
    diff["harness_version"] = "0.0.0-wrong"
    with pytest.raises(ValueError, match="diff harness_version mismatch"):
        assert_v2_report_conforms(diff)

    aggregate = build_aggregate_report([session, session])
    aggregate["sessions"][0]["attribution"]["distinct_files_touched"] = [999]
    with pytest.raises(ValueError, match="attribution distinct files invalid"):
        assert_v2_report_conforms(aggregate)


def test_markdown_table_escaping_context_summary_and_tool_target_detail(tmp_path: Path) -> None:
    report = _report(tmp_path)
    report["session"]["token_breakdown"]["total_context_tokens_used"] = 12000
    report["session"]["token_breakdown"]["context_token_limit"] = 200000
    report["session"]["token_breakdown"]["context_usage_percent"] = 6.0
    report["session"]["subagents"] = [{
        "session_id": "sub|1\nid",
        "parent_turn_index": 1,
        "subagent_type": "type|pipe\nline",
        "name": "child",
        "model_name": "test-model",
        "status": "completed",
        "duration_ms": 50,
        "turn_count": 1,
        "tool_call_count": 1,
        "error_count": 0,
        "denial_count": 0,
        "skills_read": ["skills/a|b/SKILL.md"],
        "rules_read": [],
    }]
    report["turns"][0]["tool_calls"][0]["target_path"] = "src/target.py"
    report["turns"][1]["tool_calls"][0]["target_path"] = None
    report["turns"][1]["tool_calls"][0]["input_params"] = {"command": "pytest -q"}
    markdown = render_single_session_md(report)
    assert "- Context Usage: 12000 / 200000 tokens (6.0%)" in markdown
    assert r"| type\|pipe line | `sub\|1 id` | 1 | 1 | 0 | skills/a\|b/SKILL.md | 50 |" in markdown
    assert "- Tool: `edit_file_v2` [completed] 50 ms -> `src/target.py`" in markdown
    assert "- Tool: `read_file_v2` [completed] 50 ms -> `pytest -q`" in markdown
    report["session"]["reasoning_effort"] = None
    assert "reasoning_effort=unknown" in render_single_session_md(report)


def test_validator_rejects_in_flight_captured_at_ms_and_top_context_nodes_violations(tmp_path: Path) -> None:
    report = _report(tmp_path)

    # (a) non-boolean session.in_flight, subagents[].in_flight, or sessions[].in_flight
    bad_in_flight = deepcopy(report)
    bad_in_flight["session"]["in_flight"] = "yes"
    with pytest.raises(ValueError, match="session.in_flight must be bool"):
        assert_v2_report_conforms(bad_in_flight)

    bad_sub_in_flight = deepcopy(report)
    bad_sub_in_flight["session"]["subagents"] = [{
        "session_id": "c1",
        "parent_turn_index": 1,
        "subagent_type": "explore",
        "name": "child",
        "model_name": "m",
        "status": "completed",
        "duration_ms": 10,
        "turn_count": 1,
        "tool_call_count": 1,
        "error_count": 0,
        "denial_count": 0,
        "skills_read": [],
        "rules_read": [],
        "in_flight": 1,
    }]
    with pytest.raises(ValueError, match="subagent.in_flight must be bool"):
        assert_v2_report_conforms(bad_sub_in_flight)

    # (b) non-null status, ended_at_ms, duration_ms, or aggregates.timing.total_session_ms when session.in_flight is True
    for field_name in ("status", "ended_at_ms", "duration_ms"):
        bad_live = deepcopy(report)
        bad_live["session"]["in_flight"] = True
        bad_live["session"]["status"] = None
        bad_live["session"]["ended_at_ms"] = None
        bad_live["session"]["duration_ms"] = None
        bad_live["aggregates"]["timing"]["total_session_ms"] = None
        bad_live["session"][field_name] = "completed" if field_name == "status" else 100
        with pytest.raises(ValueError, match=f"in-flight session.{field_name} must be null"):
            assert_v2_report_conforms(bad_live)

    bad_live_agg_timing = deepcopy(report)
    bad_live_agg_timing["session"]["in_flight"] = True
    bad_live_agg_timing["session"]["status"] = None
    bad_live_agg_timing["session"]["ended_at_ms"] = None
    bad_live_agg_timing["session"]["duration_ms"] = None
    bad_live_agg_timing["aggregates"]["timing"]["total_session_ms"] = 100
    with pytest.raises(ValueError, match="timing.total_session_ms mismatch"):
        assert_v2_report_conforms(bad_live_agg_timing)

    # non-null status/duration_ms on an in-flight subagent
    for sub_field, sub_val in (("status", "completed"), ("duration_ms", 10)):
        bad_live_sub = deepcopy(bad_sub_in_flight)
        bad_live_sub["session"]["subagents"][0]["in_flight"] = True
        bad_live_sub["session"]["subagents"][0]["status"] = None
        bad_live_sub["session"]["subagents"][0]["duration_ms"] = None
        bad_live_sub["session"]["subagents"][0][sub_field] = sub_val
        with pytest.raises(ValueError, match=f"in-flight subagent.{sub_field} must be null"):
            assert_v2_report_conforms(bad_live_sub)

    # (c) non-int or boolean captured_at_ms on session
    for bad_cap in (True, "12345"):
        bad_cap_rep = deepcopy(report)
        bad_cap_rep["session"]["captured_at_ms"] = bad_cap
        with pytest.raises(ValueError, match="session.captured_at_ms must be an int"):
            assert_v2_report_conforms(bad_cap_rep)

    # Aggregate session summary checks: in_flight, captured_at_ms, and timing.total_session_ms != duration_ms
    session = classify_session(
        CursorAdapter().extract_session(
            report["session"]["session_id"],
            ExtractOptions(state_db=tmp_path / "state.vscdb", tracking_db=tmp_path / "tracking.db"),
        ),
        tmp_path,
    )
    aggregate = build_aggregate_report([session, session])
    bad_agg_bool = deepcopy(aggregate)
    bad_agg_bool["sessions"][0]["in_flight"] = "no"
    with pytest.raises(ValueError, match="aggregate session in_flight must be bool"):
        assert_v2_report_conforms(bad_agg_bool)

    for agg_field, agg_val in (("status", "completed"), ("duration_ms", 100)):
        bad_agg_live = deepcopy(aggregate)
        bad_agg_live["sessions"][0]["in_flight"] = True
        bad_agg_live["sessions"][0]["status"] = None
        bad_agg_live["sessions"][0]["duration_ms"] = None
        bad_agg_live["sessions"][0]["aggregates"]["timing"]["total_session_ms"] = None
        bad_agg_live["sessions"][0][agg_field] = agg_val
        with pytest.raises(ValueError, match=f"in-flight aggregate session {agg_field} must be null"):
            assert_v2_report_conforms(bad_agg_live)

    bad_agg_timing = deepcopy(aggregate)
    bad_agg_timing["sessions"][0]["aggregates"]["timing"]["total_session_ms"] = 999999
    with pytest.raises(ValueError, match="aggregate session timing.total_session_ms mismatch"):
        assert_v2_report_conforms(bad_agg_timing)

    bad_agg_cap = deepcopy(aggregate)
    bad_agg_cap["sessions"][0]["captured_at_ms"] = False
    with pytest.raises(ValueError, match="aggregate session captured_at_ms must be an int"):
        assert_v2_report_conforms(bad_agg_cap)

    # (d) out-of-order top_context_nodes (by token count, character count tie-breaker, or id/label tie-breaker), > 15 nodes, or boolean token/char fields
    bad_nodes_order = deepcopy(report)
    bad_nodes_order["session"]["token_breakdown"]["top_context_nodes"] = [
        {"id": "small", "tokens": 10},
        {"id": "large", "tokens": 20},
    ]
    with pytest.raises(ValueError, match="top_context_nodes not sorted"):
        assert_v2_report_conforms(bad_nodes_order)

    bad_nodes_char_tie = deepcopy(report)
    bad_nodes_char_tie["session"]["token_breakdown"]["top_context_nodes"] = [
        {"id": "a", "tokens": 10, "characterCount": 50},
        {"id": "b", "tokens": 10, "characterCount": 100},
    ]
    with pytest.raises(ValueError, match="top_context_nodes not sorted"):
        assert_v2_report_conforms(bad_nodes_char_tie)

    bad_nodes_id_tie = deepcopy(report)
    bad_nodes_id_tie["session"]["token_breakdown"]["top_context_nodes"] = [
        {"id": "z", "tokens": 10, "chars": 50},
        {"id": "a", "tokens": 10, "chars": 50},
    ]
    with pytest.raises(ValueError, match="top_context_nodes not sorted"):
        assert_v2_report_conforms(bad_nodes_id_tie)

    bad_nodes_cap = deepcopy(report)
    bad_nodes_cap["session"]["token_breakdown"]["top_context_nodes"] = [
        {"id": f"n{i:02d}", "tokens": 100 - i} for i in range(16)
    ]
    with pytest.raises(ValueError, match="top_context_nodes invalid"):
        assert_v2_report_conforms(bad_nodes_cap)

    bad_nodes_bool = deepcopy(report)
    bad_nodes_bool["session"]["token_breakdown"]["top_context_nodes"] = [
        {"id": "a", "estimatedTokens": True}
    ]
    with pytest.raises(ValueError, match="top_context_nodes.estimatedTokens must be an int"):
        assert_v2_report_conforms(bad_nodes_bool)


