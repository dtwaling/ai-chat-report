#!/usr/bin/env python3
"""Chat-report -- extract Optimus-test-relevant signals from a Claude Code session.

Given a Claude Code session UUID, produce a markdown report and a JSON dump
that conform to MISSION-BRIEF section 4 (the locked structured-report
shape). Designed for iterative Optimus evaluation: run a test session,
grab the session UUID from ``~/.claude/projects/<cwd>/`` listing, run this
script, read the report.

Modes:
  Single-chat (default)  -- per-session report
  --diff <idA> <idB>     -- before/after comparison (deferred to PR-B3B)
  --aggregate <id> ...   -- rollup across N sessions (deferred to PR-B3B)

Source: Claude Code's on-disk JSONL store. Path layout per DISCOVERY.md
Q1:

  $CLAUDE_CONFIG_DIR / projects / <sanitized-cwd> / <session-uuid>.jsonl

Where ``<sanitized-cwd>`` is the invocation working directory with every
non-alphanumeric character replaced by a hyphen. Default root is
``~/.claude/`` on every supported OS; the ``CLAUDE_CONFIG_DIR`` env var
overrides.

Common invocations:

  # Single session in the current working dir
  python claudecode/chat-report.py <session-uuid>

  # Direct path to a JSONL file (skips cwd resolution)
  python claudecode/chat-report.py <session-uuid> \\
      --session-jsonl ~/.claude/projects/foo/abc.jsonl

  # Custom output dir + format
  python claudecode/chat-report.py <session-uuid> \\
      --out /tmp/reports --format both
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make sibling packages importable when this script is run directly from
# the repo root via ``python claudecode/chat-report.py``.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from claudecode._jsonl import iter_records  # noqa: E402
from claudecode._paths import session_jsonl_path  # noqa: E402
from claudecode._report import (  # noqa: E402
    build_locked_report,
    write_locked_json_report,
    write_locked_md_report,
)


DEFAULT_OUT_DIR = Path(".claude") / "local" / "chat-reports"


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="chat-report",
        description="Claude Code session -> locked structured-report.",
    )
    p.add_argument(
        "ids", nargs="+",
        help="Claude Code session UUID(s). One per session for single mode; "
             "exactly two for --diff; one or more for --aggregate.",
    )
    p.add_argument(
        "--cwd", default=None,
        help="Working directory the session was invoked from (used to resolve "
             "the JSONL path under ~/.claude/projects/<sanitized-cwd>/). "
             "Defaults to the current working directory.",
    )
    p.add_argument(
        "--session-jsonl", default=None, type=Path,
        help="Direct path to a session JSONL file. Overrides --cwd resolution.",
    )
    p.add_argument(
        "--out", default=str(DEFAULT_OUT_DIR), type=Path,
        help=f"Output directory (default: {DEFAULT_OUT_DIR}).",
    )
    p.add_argument(
        "--shape", choices=["locked"], default="locked",
        help="Report shape. Only 'locked' is supported on the Claude Code "
             "variant -- this is a greenfield emitter with no legacy shape "
             "to deprecate.",
    )
    p.add_argument(
        "--format", choices=["md", "json", "both"], default="both",
        help="Output format(s) to emit.",
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--diff", action="store_true",
        help="Diff mode: compare two sessions. Deferred to PR-B3B.",
    )
    g.add_argument(
        "--aggregate", action="store_true",
        help="Aggregate mode: rollup across N sessions. Deferred to PR-B3B.",
    )
    return p


def _resolve_jsonl_path(session_id: str, args: argparse.Namespace) -> Path:
    if args.session_jsonl is not None:
        return args.session_jsonl
    cwd = args.cwd if args.cwd is not None else str(Path.cwd())
    return session_jsonl_path(session_id, cwd)


def _run_single(session_id: str, args: argparse.Namespace) -> int:
    jsonl_path = _resolve_jsonl_path(session_id, args)
    if not jsonl_path.exists():
        sys.stderr.write(
            f"chat-report: session JSONL not found: {jsonl_path}\n"
            "  (Default retention is 30 days; check the cleanupPeriodDays "
            "setting and that CLAUDE_CODE_SKIP_PROMPT_HISTORY was not set "
            "for this session. Use --session-jsonl to point at an explicit "
            "file.)\n"
        )
        return 2

    records = list(iter_records(jsonl_path))
    if not records:
        sys.stderr.write(
            f"chat-report: session JSONL is empty: {jsonl_path}\n"
        )
        return 2

    report = build_locked_report(records, session_id_override=session_id)

    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = session_id
    if args.format in ("json", "both"):
        write_locked_json_report(out_dir / f"{stem}.json", report)
    if args.format in ("md", "both"):
        write_locked_md_report(out_dir / f"{stem}.md", report)
    sys.stderr.write(
        f"chat-report: wrote {args.format} for {session_id} to {out_dir}\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)

    if args.diff:
        raise NotImplementedError(
            "Diff mode lands in PR-B3B (feat(claudecode): diff + aggregate "
            "modes + subagent rollup). The shape is the same; the math is "
            "shared with the cursor variant."
        )
    if args.aggregate:
        raise NotImplementedError(
            "Aggregate mode lands in PR-B3B. Same as --diff above."
        )

    if len(args.ids) != 1:
        sys.stderr.write(
            "chat-report: single-chat mode takes exactly one session UUID. "
            "For multiple sessions use --diff (two) or --aggregate (one or "
            "more) once those land in PR-B3B.\n"
        )
        return 2

    return _run_single(args.ids[0], args)


if __name__ == "__main__":
    sys.exit(main())
