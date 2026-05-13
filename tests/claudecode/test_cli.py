"""CLI tests for claudecode/chat-report.py.

Loads the script via importlib (it's hyphenated and not importable by
name) and drives main(argv) directly, verifying argparse plumbing, file
discovery, output emission, and the deferred-mode NotImplementedError
escape hatches.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from common import _locked_contract as contract

from . import _fixtures as fx


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_session(tmp_path: Path, sid: str, cwd_sanitized: str = "synthetic"):
    """Write a synthetic session JSONL under a fake CLAUDE_CONFIG_DIR root."""
    root = tmp_path / "claude-root"
    project = root / "projects" / cwd_sanitized
    project.mkdir(parents=True)
    jsonl = project / f"{sid}.jsonl"
    records = [
        fx.user_typed_string("explore"),
        fx.assistant_tool_use("Read", tool_input={"file_path": "foo"},
                               tool_use_id="t1"),
        fx.user_tool_result("t1", "contents"),
    ]
    fx.write_jsonl(jsonl, records)
    return root, jsonl


# ---------------------------------------------------------------------------
# Single-chat mode
# ---------------------------------------------------------------------------


def test_cli_single_mode_emits_both_formats(claudecode_chat_report, tmp_path):
    sid = "abc-123"
    _, jsonl = _write_session(tmp_path, sid)
    out = tmp_path / "out"
    rc = claudecode_chat_report.main([
        sid, "--session-jsonl", str(jsonl), "--out", str(out),
    ])
    assert rc == 0
    assert (out / f"{sid}.json").exists()
    assert (out / f"{sid}.md").exists()
    # JSON validates against the contract.
    report = json.loads((out / f"{sid}.json").read_text(encoding="utf-8"))
    contract.assert_locked_single_chat_conforms(report)


def test_cli_single_mode_md_only(claudecode_chat_report, tmp_path):
    sid = "abc-123"
    _, jsonl = _write_session(tmp_path, sid)
    out = tmp_path / "out"
    rc = claudecode_chat_report.main([
        sid, "--session-jsonl", str(jsonl), "--out", str(out), "--format", "md",
    ])
    assert rc == 0
    assert (out / f"{sid}.md").exists()
    assert not (out / f"{sid}.json").exists()


def test_cli_single_mode_json_only(claudecode_chat_report, tmp_path):
    sid = "abc-123"
    _, jsonl = _write_session(tmp_path, sid)
    out = tmp_path / "out"
    rc = claudecode_chat_report.main([
        sid, "--session-jsonl", str(jsonl), "--out", str(out), "--format", "json",
    ])
    assert rc == 0
    assert (out / f"{sid}.json").exists()
    assert not (out / f"{sid}.md").exists()


def test_cli_missing_jsonl_returns_nonzero(claudecode_chat_report, tmp_path, capsys):
    rc = claudecode_chat_report.main([
        "missing-sid",
        "--session-jsonl", str(tmp_path / "absent.jsonl"),
        "--out", str(tmp_path / "out"),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found" in err
    # The error message must surface actionable next steps.
    assert "CLAUDE_CODE_SKIP_PROMPT_HISTORY" in err
    assert "cleanupPeriodDays" in err


def test_cli_empty_jsonl_returns_nonzero(claudecode_chat_report, tmp_path):
    sid = "empty-sid"
    jsonl = tmp_path / f"{sid}.jsonl"
    jsonl.write_text("", encoding="utf-8")
    rc = claudecode_chat_report.main([
        sid, "--session-jsonl", str(jsonl), "--out", str(tmp_path / "out"),
    ])
    assert rc == 2


def test_cli_resolves_jsonl_via_cwd_default(claudecode_chat_report, tmp_path,
                                              monkeypatch):
    """When --session-jsonl is omitted, the resolver walks CLAUDE_CONFIG_DIR."""
    sid = "abc-123"
    root, jsonl = _write_session(tmp_path, sid, cwd_sanitized="my-fake-cwd")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    out = tmp_path / "out"
    # We pass --cwd matching what sanitize_cwd would produce from "my-fake-cwd".
    rc = claudecode_chat_report.main([
        sid, "--cwd", "my/fake/cwd", "--out", str(out),
    ])
    # sanitize_cwd("my/fake/cwd") = "my-fake-cwd" -- matches the dir we made.
    assert rc == 0
    assert (out / f"{sid}.json").exists()


def test_cli_session_id_override_in_report(claudecode_chat_report, tmp_path):
    """The CLI's session-id arg wins over the records' embedded sessionId."""
    sid = "cli-arg-sid"
    _, jsonl = _write_session(tmp_path, sid)
    out = tmp_path / "out"
    rc = claudecode_chat_report.main([
        sid, "--session-jsonl", str(jsonl), "--out", str(out), "--format", "json",
    ])
    assert rc == 0
    report = json.loads((out / f"{sid}.json").read_text(encoding="utf-8"))
    assert report["session_id"] == sid


# ---------------------------------------------------------------------------
# Deferred mode escape hatches
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Multi-ID guard
# ---------------------------------------------------------------------------


def test_cli_rejects_multiple_ids_in_single_mode(claudecode_chat_report, tmp_path,
                                                   capsys):
    rc = claudecode_chat_report.main([
        "a", "b",
        "--session-jsonl", str(tmp_path / "x.jsonl"),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "exactly one session UUID" in err


# ---------------------------------------------------------------------------
# argparse surface
# ---------------------------------------------------------------------------


def test_cli_shape_only_locked_is_accepted(claudecode_chat_report, tmp_path):
    sid = "abc-123"
    _, jsonl = _write_session(tmp_path, sid)
    out = tmp_path / "out"
    rc = claudecode_chat_report.main([
        sid, "--session-jsonl", str(jsonl), "--out", str(out),
        "--shape", "locked",
    ])
    assert rc == 0


def test_cli_shape_legacy_rejected_by_argparse(claudecode_chat_report,
                                                 tmp_path, capsys):
    """No legacy shape on the claudecode variant -- argparse rejects it."""
    with pytest.raises(SystemExit):
        claudecode_chat_report.main([
            "abc",
            "--session-jsonl", str(tmp_path / "x.jsonl"),
            "--shape", "legacy",
        ])
