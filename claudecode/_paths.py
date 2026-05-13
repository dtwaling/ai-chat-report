"""Path resolution for Claude Code's on-disk chat-history store.

Layout (DISCOVERY.md Q1):

    <root>/projects/<sanitized-cwd>/
        <session-uuid>.jsonl                         -- main session transcript
        <session-uuid>/
            subagents/
                agent-<agent-id>.jsonl               -- one per subagent
                agent-<agent-id>.meta.json           -- {agentType, description}
            tool-results/
                <tool_use_id>.txt                    -- spillover for large payloads

``<root>`` defaults to ``~/.claude/`` and is overridden by the
``CLAUDE_CONFIG_DIR`` environment variable (DISCOVERY.md Q1 + Claude Code
docs at https://code.claude.com/docs/en/env-vars). The override applies on
every supported OS; no per-OS path divergence exists for the Claude Code
store (contrast: Cursor uses Library/Application Support on macOS and a
different config root on Linux).

The cwd-sanitization rule converts the invocation working directory to a
project directory name by replacing every non-alphanumeric character with
``-``. Empirically verified on Windows; documented for POSIX. Hyphens that
result from runs of non-alphanumeric characters are preserved verbatim
(not collapsed).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Match any single character that is not ASCII alphanumeric. Underscores and
# hyphens both count as non-alphanumeric and become hyphens themselves.
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")


def _home_dir() -> Path:
    """Return the user's home directory.

    Raises a clear, actionable error when neither USERPROFILE (Windows) nor
    HOME (POSIX) is set. The cursor variant adopted the same pattern after
    review; this mirrors it (see cursor/chat-report.py::_home_dir).
    """
    # Path.home() raises RuntimeError when the relevant env var is missing.
    # We catch + re-raise with a more actionable message that names the
    # claudecode-specific escape hatches.
    try:
        return Path.home()
    except RuntimeError as exc:
        # Surface the missing-var name + actionable workaround.
        missing = "USERPROFILE" if os.name == "nt" else "HOME"
        raise ValueError(
            f"Cannot resolve home directory: {missing} is not set. "
            "Set the environment variable, or pass --session-jsonl to "
            "point at the JSONL file directly, or set CLAUDE_CONFIG_DIR "
            "to the Claude Code root."
        ) from exc


def claude_root() -> Path:
    """Return the Claude Code root directory.

    ``CLAUDE_CONFIG_DIR`` env var wins when set (per Claude Code docs).
    Otherwise ``~/.claude/`` on every supported OS.
    """
    env_root = os.environ.get("CLAUDE_CONFIG_DIR")
    if env_root:
        return Path(env_root)
    return _home_dir() / ".claude"


def sanitize_cwd(cwd: str | Path) -> str:
    """Convert an invocation working directory to its on-disk project-dir name.

    The rule (DISCOVERY.md Q1): every non-alphanumeric character becomes
    ``-``. Empirical evidence: ``C:\\_Source\\ai-chat-report`` becomes
    ``C---Source-ai-chat-report``. The string is taken verbatim -- no
    normalization, no collapsing of repeated hyphens.

    The function accepts either a string or a Path; both are stringified
    before substitution.
    """
    return _NON_ALNUM_RE.sub("-", str(cwd))


def projects_root(root: Path | None = None) -> Path:
    """Return ``<claude-root>/projects/`` -- the per-cwd subdir parent."""
    return (root if root is not None else claude_root()) / "projects"


def project_dir(cwd: str | Path, root: Path | None = None) -> Path:
    """Return the on-disk project directory for an invocation cwd."""
    return projects_root(root) / sanitize_cwd(cwd)


def session_jsonl_path(
    session_id: str,
    cwd: str | Path,
    root: Path | None = None,
) -> Path:
    """Return the main session JSONL path for a session."""
    return project_dir(cwd, root) / f"{session_id}.jsonl"


def session_private_dir(
    session_id: str,
    cwd: str | Path,
    root: Path | None = None,
) -> Path:
    """Return the session-private subdirectory (subagents/, tool-results/)."""
    return project_dir(cwd, root) / session_id


def subagents_dir(
    session_id: str,
    cwd: str | Path,
    root: Path | None = None,
) -> Path:
    """Return ``<session-uuid>/subagents/``."""
    return session_private_dir(session_id, cwd, root) / "subagents"


def tool_results_dir(
    session_id: str,
    cwd: str | Path,
    root: Path | None = None,
) -> Path:
    """Return ``<session-uuid>/tool-results/``."""
    return session_private_dir(session_id, cwd, root) / "tool-results"


def tool_result_spillover_path(
    session_id: str,
    cwd: str | Path,
    tool_use_id: str,
    root: Path | None = None,
) -> Path:
    """Return the spillover file path for a given tool_use_id.

    Per DISCOVERY.md Q3.c: large tool results spill to
    ``<session-uuid>/tool-results/<tool_use_id>.txt`` when the inline
    payload exceeds Claude Code's per-result budget. Callers must check
    ``path.exists()`` before reading; spillover is not guaranteed.
    """
    return tool_results_dir(session_id, cwd, root) / f"{tool_use_id}.txt"


def subagent_jsonl_paths(
    session_id: str,
    cwd: str | Path,
    root: Path | None = None,
) -> list[Path]:
    """Return all subagent JSONL files for a session, sorted by filename.

    Returns an empty list when the subagents directory does not exist (the
    common case for sessions that dispatched no subagents). Filenames follow
    the ``agent-<agent-id>.jsonl`` convention; meta sidecars (``.meta.json``)
    are excluded from this list.
    """
    sub_dir = subagents_dir(session_id, cwd, root)
    if not sub_dir.is_dir():
        return []
    return sorted(sub_dir.glob("agent-*.jsonl"))


def subagent_meta_path(
    session_id: str,
    cwd: str | Path,
    agent_id: str,
    root: Path | None = None,
) -> Path:
    """Return the subagent meta sidecar path for a given agent_id.

    Sidecar carries ``{agentType, description}`` per DISCOVERY.md Q1.
    Callers must check ``exists()`` before reading.
    """
    return subagents_dir(session_id, cwd, root) / f"agent-{agent_id}.meta.json"
