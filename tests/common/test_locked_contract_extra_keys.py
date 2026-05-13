"""Locked-shape validator: tolerance of additive extra keys.

The locked structured-report shape (MISSION-BRIEF section 4.5) treats any
additive non-breaking change as not requiring a version bump. Downstream
consumers must accept extra keys gracefully.

The Claude Code variant emits ``aggregates.subagent_rollup`` (per
DISCOVERY.md consideration #4, PM-signed-off 2026-05-12 as additive). The
Cursor variant does not. The shared validator in ``common/_locked_contract``
must therefore accept reports both with and without that field.

These tests lock in the additive-tolerance property of the validator before
the Claude Code variant starts emitting the field.
"""

from __future__ import annotations

import pytest

from common import _locked_contract as contract


def _minimal_aggregates() -> dict:
    """A minimal valid aggregates block with every required tool_class zero-filled."""
    return {
        "total_tool_calls": 0,
        "by_class": {
            "broad-sweep-read": 0,
            "broad-sweep-grep": 0,
            "broad-sweep-glob": 0,
            "optimus-mcp": 0,
            "directory-index-read": 0,
            "edit": 0,
            "write": 0,
            "bash": 0,
            "other": 0,
        },
        "success_metric_components": {
            "component_a": {
                "definition": "optimus_* count >= 1.0x broad-sweep count",
                "optimus_count": 0,
                "broad_sweep_count": 0,
                "ratio": 0.0,
                "pass": False,
            },
            "component_b": {
                "definition": "informed >= 1.0x uninformed",
                "informed_count": 0,
                "uninformed_count": 0,
                "ratio": 0.0,
                "pass": False,
                "heuristic_failure_modes_flagged": [],
            },
            "overall": {"pass": False, "partial_pass": False},
        },
        "denials": [],
        "errors": [],
    }


def _minimal_single_chat_report(*, ide: str = "claude-code") -> dict:
    return {
        "report_version": "1.0",
        "report_kind": "single-chat",
        "ide": ide,
        "ide_version": "2.1.140",
        "session_id": "d5093750-dd1a-4a15-adae-79334235a2e7",
        "session_id_normalized": "d5093750dd1a4a15adae79334235a2e7",
        "session_start_iso": "2026-05-12T22:00:00.000Z",
        "session_end_iso": "2026-05-12T23:00:00.000Z",
        "session_duration_s": 3600,
        "turns": [],
        "aggregates": _minimal_aggregates(),
        "warnings": [],
    }


# --- Additive extra-key tolerance --------------------------------------------


def test_subagent_rollup_extra_key_at_aggregates_tolerated():
    """The future claudecode subagent_rollup additive field must not violate the contract."""
    report = _minimal_single_chat_report()
    report["aggregates"]["subagent_rollup"] = {
        "total_subagent_calls": 4,
        "subagents": [
            {
                "agent_id": "agent-abc",
                "agent_type": "general-purpose",
                "by_class": {"broad-sweep-read": 2, "broad-sweep-grep": 1, "other": 1},
            }
        ],
    }
    # MUST NOT raise.
    contract.assert_locked_single_chat_conforms(report)


def test_subagent_rollup_omission_still_valid():
    """Cursor variant omits the field; the validator must accept that."""
    report = _minimal_single_chat_report(ide="cursor")
    assert "subagent_rollup" not in report["aggregates"]
    contract.assert_locked_single_chat_conforms(report)


def test_top_level_extra_key_tolerated():
    """Any unknown top-level key is additive per section 4.5."""
    report = _minimal_single_chat_report()
    report["claude_code_session_metadata"] = {"requestId": "req_011CaygyHx43"}
    contract.assert_locked_single_chat_conforms(report)


def test_turn_extra_key_tolerated():
    """Per-turn extra keys (e.g., promptId backlink) are additive."""
    report = _minimal_single_chat_report()
    report["turns"].append({
        "turn_index": 0,
        "role": "assistant",
        "started_iso": "2026-05-12T22:00:00.000Z",
        "ended_iso": "2026-05-12T22:00:01.000Z",
        "tool_calls": [],
        "prompt_id": "d294ea65-34e1-436a-a69e-339482addd16",  # extra key
    })
    contract.assert_locked_single_chat_conforms(report)


def test_tool_call_extra_key_tolerated():
    """Per-tool-call extra keys (e.g., tool_use_id, agent_id) are additive."""
    report = _minimal_single_chat_report()
    report["turns"].append({
        "turn_index": 0,
        "role": "assistant",
        "started_iso": "2026-05-12T22:00:00.000Z",
        "ended_iso": "2026-05-12T22:00:01.000Z",
        "tool_calls": [{
            "call_index": 0,
            "tool_name": "Read",
            "tool_class": "broad-sweep-read",
            "input_summary": "Read MISSION-BRIEF.md",
            "input_payload": {"file_path": "MISSION-BRIEF.md"},
            "output_summary": "read 471 lines",
            "output_status": "ok",
            "denial_reason": None,
            "elapsed_ms": None,
            "informed_precision_read": {
                "applicable": True,
                "classification": "uninformed",
                "reason": "no DIRECTORY_INDEX.md read preceded this Read in the same turn",
            },
            "tool_use_id": "toolu_01HD4eo1Ad2XhtnTkpRoLkrT",  # extra key
            "agent_id": None,  # extra key
        }],
    })
    contract.assert_locked_single_chat_conforms(report)


# --- Required-field violations still caught ---------------------------------


def test_missing_required_field_still_rejected():
    """Adding extra keys must not weaken required-key enforcement."""
    report = _minimal_single_chat_report()
    report["aggregates"]["subagent_rollup"] = {"foo": "bar"}
    del report["session_id"]
    with pytest.raises(AssertionError, match="session_id"):
        contract.assert_locked_single_chat_conforms(report)


def test_bad_tool_class_still_rejected():
    """Tool-class enum still enforced even with extra subagent_rollup field."""
    report = _minimal_single_chat_report()
    report["aggregates"]["subagent_rollup"] = {"placeholder": True}
    report["turns"].append({
        "turn_index": 0,
        "role": "assistant",
        "started_iso": "2026-05-12T22:00:00.000Z",
        "ended_iso": "2026-05-12T22:00:01.000Z",
        "tool_calls": [{
            "call_index": 0,
            "tool_name": "Read",
            "tool_class": "not-a-real-class",  # bad
            "input_summary": "x",
            "input_payload": {},
            "output_summary": "y",
            "output_status": "ok",
            "denial_reason": None,
            "elapsed_ms": None,
            "informed_precision_read": {
                "applicable": False,
                "classification": "n/a",
                "reason": "",
            },
        }],
    })
    with pytest.raises(AssertionError, match="tool_class"):
        contract.assert_locked_single_chat_conforms(report)
