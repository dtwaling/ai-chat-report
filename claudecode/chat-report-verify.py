#!/usr/bin/env python3
"""Verification script for the claudecode variant's chat-report.py.

Two modes:

* ``--validate <path.json>`` -- offline JSON validation against the locked
  structured-report shape (``common/_locked_contract.py``). Auto-detects
  single-chat / diff / aggregate via the ``report_kind`` field. Returns 0
  on conform, non-zero on violation. No filesystem state needed.

* Default (no ``--validate``) -- integration mode. Runs chat-report.py as
  a subprocess against the local claudecode JSONL store under
  ``~/.claude/projects/`` and asserts the emitted JSON conforms to the
  locked shape. Requires ``chat-report-verify.config.json`` with real
  session UUIDs for the local machine (see the template alongside this
  script).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make the sibling production-module importable when this script is run
# directly via ``python claudecode/chat-report-verify.py`` from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from common import _locked_contract as contract  # noqa: E402


# ---------------------------------------------------------------------------
# Offline --validate mode
# ---------------------------------------------------------------------------

_VALIDATORS = {
    "single-chat": contract.assert_locked_single_chat_conforms,
    "diff": contract.assert_locked_diff_conforms,
    "aggregate": contract.assert_locked_aggregate_conforms,
}


def validate_json_file(path: Path) -> int:
    """Validate a saved JSON report against the locked shape.

    Returns 0 on conform, non-zero on any failure (file missing, unparseable
    JSON, missing/wrong ``report_kind``, contract violation).
    """
    if not path.exists():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        return 2
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"ERROR: not valid JSON ({path}): {e}", file=sys.stderr)
        return 3
    except OSError as e:
        print(f"ERROR: cannot read {path}: {e}", file=sys.stderr)
        return 2

    kind = report.get("report_kind") if isinstance(report, dict) else None
    if kind not in _VALIDATORS:
        print(
            f"ERROR: cannot determine report_kind for {path}; "
            f"expected one of {sorted(_VALIDATORS)}, got {kind!r}",
            file=sys.stderr,
        )
        return 4

    try:
        _VALIDATORS[kind](report)
    except AssertionError as e:
        print(f"FAIL ({kind}): {e}", file=sys.stderr)
        return 1

    print(f"OK ({kind}): {path}")
    return 0


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Verify chat-report.py output against the locked-shape contract. "
            "Use --validate <path.json> for offline validation (no claudecode "
            "state needed); run without --validate for the full integration "
            "suite."
        ),
    )
    ap.add_argument("--validate", type=Path, default=None, metavar="PATH",
                    help="Validate a saved JSON report against the locked shape "
                         "and exit. Auto-detects single-chat / diff / aggregate "
                         "via the report_kind field.")
    args = ap.parse_args(argv)

    if args.validate is not None:
        return validate_json_file(args.validate)

    print(
        "ERROR: integration mode not yet implemented; use --validate <path.json> "
        "for offline validation.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
