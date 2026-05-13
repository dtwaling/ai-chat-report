"""Tests for claudecode/_classify.py.

DISCOVERY.md section 2 enumerates the tool-name -> locked tool_class mapping
for Claude Code. These tests pin the classifier's behavior on every
documented mapping plus the optimus-MCP pattern match plus the
DIRECTORY_INDEX.md special case.
"""

from __future__ import annotations

import pytest

from claudecode._classify import classify_tool_class_claudecode as cc


# ---------------------------------------------------------------------------
# Direct name -> class mappings (DISCOVERY.md section 2 table)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,expected", [
    ("Grep", "broad-sweep-grep"),
    ("Glob", "broad-sweep-glob"),
    ("Bash", "bash"),
    ("Edit", "edit"),
    ("MultiEdit", "edit"),
    ("Write", "write"),
])
def test_direct_mappings(name, expected):
    assert cc(name, {}) == expected


def test_read_without_dir_index_is_broad_sweep_read():
    assert cc("Read", {"file_path": "src/foo.py"}) == "broad-sweep-read"


def test_read_with_no_input_is_broad_sweep_read():
    assert cc("Read") == "broad-sweep-read"


# ---------------------------------------------------------------------------
# Read -> directory-index-read special case
# ---------------------------------------------------------------------------


def test_read_dir_index_classified_as_directory_index_read():
    assert cc("Read", {"file_path": ".cursor/DIRECTORY_INDEX.md"}) == \
        "directory-index-read"


def test_read_dir_index_works_with_windows_separators():
    assert cc("Read", {"file_path": r".cursor\DIRECTORY_INDEX.md"}) == \
        "directory-index-read"


def test_read_dir_index_at_repo_root():
    assert cc("Read", {"file_path": "DIRECTORY_INDEX.md"}) == \
        "directory-index-read"


def test_read_dir_index_is_case_sensitive_on_filename():
    """The canonical filename is DIRECTORY_INDEX.md; lowercase doesn't match."""
    assert cc("Read", {"file_path": "directory_index.md"}) == "broad-sweep-read"


def test_read_with_partial_match_doesnt_trigger():
    """Files with 'DIRECTORY_INDEX' as substring (but not endswith) don't match."""
    assert cc("Read", {"file_path": "docs/DIRECTORY_INDEX_OLD.md"}) == \
        "broad-sweep-read"


def test_read_with_alt_path_keys():
    """Tolerance for tools that name the path-arg something other than file_path."""
    assert cc("Read", {"path": "DIRECTORY_INDEX.md"}) == "directory-index-read"
    assert cc("Read", {"filePath": "DIRECTORY_INDEX.md"}) == "directory-index-read"


# ---------------------------------------------------------------------------
# optimus-MCP pattern matching
# ---------------------------------------------------------------------------


def test_optimus_mcp_prefix_full_form():
    assert cc("mcp__optimus__optimus_search", {}) == "optimus-mcp"
    assert cc("mcp__optimus__optimus_grep", {}) == "optimus-mcp"
    assert cc("mcp__optimus__optimus_resolve", {}) == "optimus-mcp"


def test_optimus_mcp_case_insensitive_on_server_id():
    """Server-id 'Optimus' or 'OPTIMUS' still matches."""
    assert cc("mcp__Optimus__tool", {}) == "optimus-mcp"
    assert cc("mcp__OPTIMUS__tool", {}) == "optimus-mcp"


def test_optimus_mcp_substring_match_on_server_id():
    """If the server is renamed 'optimus-eval' or similar, still matches."""
    assert cc("mcp__optimus-eval__search", {}) == "optimus-mcp"
    assert cc("mcp__myoptimusserver__tool", {}) == "optimus-mcp"


def test_non_optimus_mcp_falls_to_other():
    """Other MCP servers are not optimus -> other."""
    assert cc("mcp__claude_ai_PubMed__search_articles", {}) == "other"
    assert cc("mcp__Excalidraw__create_view", {}) == "other"


def test_malformed_mcp_name_falls_to_other():
    """Single-underscore or short MCP names don't match the pattern."""
    assert cc("mcp__incomplete", {}) == "other"
    assert cc("mcp_optimus_search", {}) == "other"


# ---------------------------------------------------------------------------
# Catch-all "other"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "Agent", "AskUserQuestion", "TaskCreate", "TaskUpdate", "TaskList",
    "ToolSearch", "NotebookEdit", "WebFetch", "WebSearch", "Skill",
])
def test_known_other_tools_map_to_other(name):
    assert cc(name, {}) == "other"


def test_empty_name_returns_other():
    assert cc("", {}) == "other"
    assert cc(None, {}) == "other"  # type: ignore[arg-type]
