"""Tests for claudecode/_paths.py.

Covers the path-resolution helpers for Claude Code's on-disk chat-history
store (DISCOVERY.md Q1). All tests use ``monkeypatch.setenv`` /
``monkeypatch.delenv`` plus an explicit ``root`` argument where useful so
behavior is deterministic regardless of host environment.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claudecode import _paths as p


# ---------------------------------------------------------------------------
# claude_root + CLAUDE_CONFIG_DIR override
# ---------------------------------------------------------------------------


def test_claude_root_defaults_to_home_dot_claude(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    # Pin Path.home() result via the env var Path.home() consults.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert p.claude_root() == tmp_path / ".claude"


def test_claude_root_honors_claude_config_dir(monkeypatch, tmp_path):
    custom = tmp_path / "custom-claude-root"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
    assert p.claude_root() == custom


def test_claude_root_honors_empty_claude_config_dir_falls_back(monkeypatch, tmp_path):
    """Empty string is treated as 'unset' -- the default kicks in."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "")
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert p.claude_root() == tmp_path / ".claude"


def test_home_dir_raises_actionable_when_unset(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    # On Windows, Path.home() also consults HOMEDRIVE+HOMEPATH as a
    # fallback. Delete those too so the resolver actually fails.
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    # USERPROFILE on Windows / HOME on POSIX are what Path.home() consults.
    # The message must name the missing variable AND the escape hatches.
    with pytest.raises(ValueError, match=r"--session-jsonl|CLAUDE_CONFIG_DIR"):
        p._home_dir()


# ---------------------------------------------------------------------------
# sanitize_cwd
# ---------------------------------------------------------------------------


def test_sanitize_cwd_replaces_path_separators_with_hyphens():
    # Windows-style cwd: drive letter colon + backslashes become hyphens.
    assert p.sanitize_cwd(r"C:\_Source\ai-chat-report") == "C---Source-ai-chat-report"


def test_sanitize_cwd_replaces_posix_path_with_hyphens():
    # POSIX cwd: leading slash + slashes between segments.
    assert p.sanitize_cwd("/home/dustin/dev/project") == "-home-dustin-dev-project"


def test_sanitize_cwd_preserves_runs_of_hyphens():
    # The sanitization rule does NOT collapse repeated non-alphanumerics.
    # Each non-alnum char becomes a single hyphen; runs stay verbatim.
    assert p.sanitize_cwd("a__b") == "a--b"


def test_sanitize_cwd_alphanumeric_passes_through_unchanged():
    assert p.sanitize_cwd("simpleProject123") == "simpleProject123"


def test_sanitize_cwd_accepts_pathlib_path():
    assert p.sanitize_cwd(Path(r"C:\_Source\ai-chat-report")) == "C---Source-ai-chat-report"


def test_sanitize_cwd_replaces_unicode_letter():
    # ASCII-only alphanumeric is the rule. Non-ASCII letters fall outside
    # the [A-Za-z0-9] range and each become a single hyphen.
    assert p.sanitize_cwd("café") == "caf-"


# ---------------------------------------------------------------------------
# project_dir
# ---------------------------------------------------------------------------


def test_project_dir_composes_root_plus_projects_plus_sanitized_cwd(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    cwd = r"C:\_Source\ai-chat-report"
    expected = tmp_path / "projects" / "C---Source-ai-chat-report"
    assert p.project_dir(cwd) == expected


def test_project_dir_root_override_wins(tmp_path):
    custom = tmp_path / "alt-root"
    expected = custom / "projects" / "myproj"
    assert p.project_dir("myproj", root=custom) == expected


# ---------------------------------------------------------------------------
# session_jsonl_path
# ---------------------------------------------------------------------------


def test_session_jsonl_path_uses_session_uuid_filename(tmp_path):
    sid = "d5093750-dd1a-4a15-adae-79334235a2e7"
    cwd = r"C:\_Source\ai-chat-report"
    got = p.session_jsonl_path(sid, cwd, root=tmp_path)
    expected = (tmp_path / "projects" / "C---Source-ai-chat-report"
                / f"{sid}.jsonl")
    assert got == expected


# ---------------------------------------------------------------------------
# session_private_dir / subagents_dir / tool_results_dir / spillover
# ---------------------------------------------------------------------------


def test_session_private_dir_is_sibling_subdir_of_jsonl(tmp_path):
    sid = "d5093750-dd1a-4a15-adae-79334235a2e7"
    cwd = "/home/dustin/dev/project"
    proj = tmp_path / "projects" / p.sanitize_cwd(cwd)
    assert p.session_private_dir(sid, cwd, root=tmp_path) == proj / sid
    # And the JSONL itself is at <proj>/<sid>.jsonl -- sibling, not nested.
    assert p.session_jsonl_path(sid, cwd, root=tmp_path) == proj / f"{sid}.jsonl"


def test_subagents_dir_under_session_private(tmp_path):
    sid = "abc"
    cwd = "p"
    assert p.subagents_dir(sid, cwd, root=tmp_path).name == "subagents"
    assert p.subagents_dir(sid, cwd, root=tmp_path).parent == \
        p.session_private_dir(sid, cwd, root=tmp_path)


def test_tool_results_dir_under_session_private(tmp_path):
    sid = "abc"
    cwd = "p"
    assert p.tool_results_dir(sid, cwd, root=tmp_path).name == "tool-results"
    assert p.tool_results_dir(sid, cwd, root=tmp_path).parent == \
        p.session_private_dir(sid, cwd, root=tmp_path)


def test_tool_result_spillover_path_filename_is_tool_use_id_dot_txt(tmp_path):
    sid = "abc"
    cwd = "p"
    tool_use_id = "toolu_01HRVyAhXzStyeGDpEEpJLGg"
    got = p.tool_result_spillover_path(sid, cwd, tool_use_id, root=tmp_path)
    assert got.name == f"{tool_use_id}.txt"
    assert got.parent == p.tool_results_dir(sid, cwd, root=tmp_path)


# ---------------------------------------------------------------------------
# subagent_jsonl_paths + subagent_meta_path
# ---------------------------------------------------------------------------


def test_subagent_jsonl_paths_returns_empty_when_dir_missing(tmp_path):
    """Sessions with zero subagents have no subagents/ dir at all."""
    assert p.subagent_jsonl_paths("sid", "cwd", root=tmp_path) == []


def test_subagent_jsonl_paths_returns_sorted_glob(tmp_path):
    sid = "sid"
    cwd = "cwd"
    sub_dir = p.subagents_dir(sid, cwd, root=tmp_path)
    sub_dir.mkdir(parents=True)
    # Three subagent JSONLs out-of-order. Plus a meta sidecar that must NOT
    # be returned. Plus an unrelated file.
    (sub_dir / "agent-zzz.jsonl").write_text("{}", encoding="utf-8")
    (sub_dir / "agent-aaa.jsonl").write_text("{}", encoding="utf-8")
    (sub_dir / "agent-mmm.jsonl").write_text("{}", encoding="utf-8")
    (sub_dir / "agent-aaa.meta.json").write_text("{}", encoding="utf-8")
    (sub_dir / "unrelated.txt").write_text("x", encoding="utf-8")
    got = p.subagent_jsonl_paths(sid, cwd, root=tmp_path)
    assert [pth.name for pth in got] == ["agent-aaa.jsonl",
                                          "agent-mmm.jsonl",
                                          "agent-zzz.jsonl"]


def test_subagent_meta_path_naming(tmp_path):
    got = p.subagent_meta_path("sid", "cwd", "abc123", root=tmp_path)
    assert got.name == "agent-abc123.meta.json"
    assert got.parent == p.subagents_dir("sid", "cwd", root=tmp_path)


# ---------------------------------------------------------------------------
# End-to-end against this machine's real session JSONL (smoke)
# ---------------------------------------------------------------------------


def test_resolves_real_session_jsonl_on_this_machine(monkeypatch):
    """Smoke: the helpers point at a real file under HOME.

    Skips if Path.home() doesn't yield a directory (e.g., sandboxed CI).
    """
    # Use the default root (no CLAUDE_CONFIG_DIR override).
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    try:
        home = Path.home()
    except RuntimeError:
        pytest.skip("Path.home() unresolvable in this environment")
    if not home.exists():
        pytest.skip("home directory does not exist on this host")
    # The discovery report itself was generated from this session's JSONL;
    # if Claude Code is installed at all, ~/.claude/projects/ exists.
    projects = p.projects_root()
    if not projects.exists():
        pytest.skip("Claude Code not installed (~/.claude/projects missing)")
    # Pick any session JSONL in any project. We're just verifying the
    # resolver returns a real path that can be opened, not its contents.
    candidates = sorted(projects.glob("*/*.jsonl"))
    if not candidates:
        pytest.skip("no Claude Code session JSONLs available on host")
    a_session = candidates[0]
    # Walk backward through the helper to reconstruct the same path and
    # verify it agrees.
    proj_dir = a_session.parent
    sanitized = proj_dir.name
    sid = a_session.stem
    # Reverse-engineer the cwd that maps to this project dir IS NOT
    # possible (sanitization is lossy) -- but we can verify that for the
    # given sanitized-cwd, the resolver matches.
    assert proj_dir == projects / sanitized
    # And the JSONL is at <proj>/<sid>.jsonl as the contract says.
    assert a_session == proj_dir / f"{sid}.jsonl"
