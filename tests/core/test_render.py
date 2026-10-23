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
        "Duration: 2000 ms",
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
        "Thinking (25 ms): reasoning",
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
