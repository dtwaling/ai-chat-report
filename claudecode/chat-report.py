#!/usr/bin/env python3
"""Chat-report -- extract Optimus-test-relevant signals from a Claude Code session.

Given a Claude Code session UUID, produce a markdown report and a JSON dump
that conform to MISSION-BRIEF section 4 (the locked structured-report
shape). Designed for iterative Optimus evaluation: run a test session,
grab the session UUID from ``~/.claude/projects/<cwd>/`` listing, run this
script, read the report.

Modes:
  Single-chat (default)  -- per-session report
  --diff <idA> <idB>     -- before/after comparison between two sessions
  --aggregate <id> ...   -- rollup across N sessions (deferred to next commit)

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

  # Diff two sessions (one --session-jsonl per id, in order)
  python claudecode/chat-report.py <id-a> <id-b> --diff \\
      --session-jsonl ~/.claude/projects/foo/<id-a>.jsonl \\
      --session-jsonl ~/.claude/projects/foo/<id-b>.jsonl

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

from common._diff_aggregate import (  # noqa: E402
    build_locked_aggregate_report,
    build_locked_diff_report,
)

from claudecode._jsonl import iter_records  # noqa: E402
from claudecode._paths import (  # noqa: E402
    session_jsonl_path,
    subagents_dir_from_jsonl,
)
from claudecode._report import (  # noqa: E402
    build_locked_report,
    write_locked_json_report,
    write_locked_md_aggregate,
    write_locked_md_diff,
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
        "--session-jsonl", action="append", default=None, dest="session_jsonl",
        help="Direct path to a session JSONL file. Overrides --cwd resolution. "
             "Repeat once per id in --diff / --aggregate modes (paired by "
             "index with the positional ids).",
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
        help="Diff mode: compare two sessions. Requires exactly two positional ids.",
    )
    g.add_argument(
        "--aggregate", action="store_true",
        help="Aggregate mode: rollup across N sessions. Deferred to a later commit.",
    )
    return p


def _resolve_jsonl_path(
    session_id: str,
    args: argparse.Namespace,
    *,
    index: int = 0,
) -> Path:
    """Resolve the JSONL path for a given session id.

    If ``--session-jsonl`` was passed, that list (paired by index with the
    positional ids) wins. Otherwise resolve via ``--cwd`` /
    ``CLAUDE_CONFIG_DIR``.

    Raises ``IndexError`` when ``--session-jsonl`` was passed but doesn't
    have a path for the requested index.
    """
    paths = args.session_jsonl
    if paths:
        if index >= len(paths):
            raise IndexError(
                f"--session-jsonl: expected {len(args.ids)} path(s) to pair "
                f"with positional ids, got {len(paths)}."
            )
        return Path(paths[index])
    cwd = args.cwd if args.cwd is not None else str(Path.cwd())
    return session_jsonl_path(session_id, cwd)


def _not_found_msg(path: Path) -> str:
    return (
        f"chat-report: session JSONL not found: {path}\n"
        "  (Default retention is 30 days; check the cleanupPeriodDays "
        "setting and that CLAUDE_CODE_SKIP_PROMPT_HISTORY was not set "
        "for this session. Use --session-jsonl to point at an explicit "
        "file.)\n"
    )


def _load_session_or_exit_code(
    session_id: str, args: argparse.Namespace, *, index: int = 0,
) -> tuple[list[dict] | None, Path | None, int]:
    """Load records + resolve subagents dir for one session.

    On error: returns (None, None, 2) after writing to stderr.
    On success: returns (records, subagents_dir, 0). ``subagents_dir`` is
    always paired with the JSONL path; existence is checked downstream.
    """
    try:
        jsonl_path = _resolve_jsonl_path(session_id, args, index=index)
    except IndexError as exc:
        sys.stderr.write(f"chat-report: {exc}\n")
        return None, None, 2
    if not jsonl_path.exists():
        sys.stderr.write(_not_found_msg(jsonl_path))
        return None, None, 2
    records = list(iter_records(jsonl_path))
    if not records:
        sys.stderr.write(
            f"chat-report: session JSONL is empty: {jsonl_path}\n"
        )
        return None, None, 2
    return records, subagents_dir_from_jsonl(jsonl_path), 0


def _run_single(session_id: str, args: argparse.Namespace) -> int:
    records, sub_dir, rc = _load_session_or_exit_code(session_id, args, index=0)
    if records is None:
        return rc
    report = build_locked_report(
        records, session_id_override=session_id, subagents_dir=sub_dir,
    )

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


def _run_diff(args: argparse.Namespace) -> int:
    if len(args.ids) != 2:
        sys.stderr.write(
            "chat-report: --diff requires exactly two session UUIDs.\n"
        )
        return 2
    sid_a, sid_b = args.ids

    records_a, sub_a, rc = _load_session_or_exit_code(sid_a, args, index=0)
    if records_a is None:
        return rc
    records_b, sub_b, rc = _load_session_or_exit_code(sid_b, args, index=1)
    if records_b is None:
        return rc

    before = build_locked_report(
        records_a, session_id_override=sid_a, subagents_dir=sub_a,
    )
    after = build_locked_report(
        records_b, session_id_override=sid_b, subagents_dir=sub_b,
    )
    diff = build_locked_diff_report(before, after)

    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"diff-{sid_a[:12]}-vs-{sid_b[:12]}"
    if args.format in ("json", "both"):
        write_locked_json_report(out_dir / f"{stem}.json", diff)
    if args.format in ("md", "both"):
        write_locked_md_diff(out_dir / f"{stem}.md", diff)
    sys.stderr.write(
        f"chat-report: wrote {args.format} for diff to {out_dir}\n"
    )
    return 0


def _run_aggregate(args: argparse.Namespace) -> int:
    """Aggregate mode: rollup across N sessions."""
    reports: list[dict] = []
    for index, sid in enumerate(args.ids):
        records, sub_dir, rc = _load_session_or_exit_code(sid, args, index=index)
        if records is None:
            return rc
        reports.append(build_locked_report(
            records, session_id_override=sid, subagents_dir=sub_dir,
        ))
    agg = build_locked_aggregate_report(reports)

    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = f"aggregate-{ts}"
    if args.format in ("json", "both"):
        write_locked_json_report(out_dir / f"{stem}.json", agg)
    if args.format in ("md", "both"):
        write_locked_md_aggregate(out_dir / f"{stem}.md", agg)
    sys.stderr.write(
        f"chat-report: wrote {args.format} for aggregate "
        f"({agg['session_count']} sessions) to {out_dir}\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)

    if args.diff:
        return _run_diff(args)
    if args.aggregate:
        return _run_aggregate(args)

    if len(args.ids) != 1:
        sys.stderr.write(
            "chat-report: single-chat mode takes exactly one session UUID. "
            "For multiple sessions use --diff (two) or --aggregate (one or "
            "more).\n"
        )
        return 2

    return _run_single(args.ids[0], args)


if __name__ == "__main__":
    sys.exit(main())
