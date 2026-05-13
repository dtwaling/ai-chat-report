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
  script). Override the config path with ``--config <path>``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# Make the sibling production-module importable when this script is run
# directly via ``python claudecode/chat-report-verify.py`` from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from common import _locked_contract as contract  # noqa: E402

SCRIPT = Path(__file__).resolve().parent / "chat-report.py"
DEFAULT_CONFIG = Path(__file__).resolve().parent / "chat-report-verify.config.json"
WORKSPACE = Path(__file__).resolve().parent.parent


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
# Integration mode -- exercises chat-report.py against the local claudecode store
# ---------------------------------------------------------------------------

def run_report(*cli_args: str, out_dir: Path | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(SCRIPT)]
    if out_dir:
        cmd.extend(["--out", str(out_dir)])
    cmd.extend(cli_args)
    return subprocess.run(cmd, capture_output=True, text=True, cwd=str(WORKSPACE))


def _check_locked(validator, payload: dict[str, Any]) -> tuple[bool, str]:
    try:
        validator(payload)
    except AssertionError as e:
        return False, str(e)
    return True, ""


def check_subagent_rollup(
    aggregates: dict[str, Any],
) -> tuple[bool, str, str]:
    """Inspect ``aggregates.subagent_rollup`` against the additive contract.

    Returns ``(ok, check_name, detail)``. The contract (DISCOVERY.md #4):

    * Key absent: PASS -- the additive-omit pattern is honored.
    * Key present, value is ``None``: FAIL -- the spec disallows ``null``
      emission. A regression that emits ``"subagent_rollup": null`` must
      surface as a check failure.
    * Key present, value is a dict with the expected shape: PASS.
    * Key present, anything else: FAIL.

    The presence check uses ``in``, not ``.get(...)``, precisely so the
    null case is distinguishable from the omit case.
    """
    if "subagent_rollup" not in aggregates:
        return True, ("subagent_rollup omitted when no subagents"
                      " (additive omit honored)"), ""
    rollup = aggregates["subagent_rollup"]
    if rollup is None:
        return (
            False,
            "subagent_rollup must be omitted when absent, not null",
            "spec disallows null emission; key present with None violates DISCOVERY.md #4",
        )
    ok = (
        isinstance(rollup, dict)
        and "total_subagent_calls" in rollup
        and isinstance(rollup.get("subagents"), list)
    )
    if not ok:
        return False, "subagent_rollup shape conformance", f"rollup={rollup!r}"
    return True, "subagent_rollup shape conformance", ""


def _single_args(sid: str, session_jsonl: str | None, cwd: str | None) -> list[str]:
    """Build positional + path-resolution args for single-mode invocation."""
    args = [sid]
    if session_jsonl:
        args.extend(["--session-jsonl", session_jsonl])
    elif cwd:
        args.extend(["--cwd", cwd])
    return args


def _diff_args(
    ids: list[str], jsonls: list[str] | None, cwd: str | None,
) -> list[str]:
    args = ["--diff", *ids]
    if jsonls:
        for p in jsonls:
            args.extend(["--session-jsonl", p])
    elif cwd:
        args.extend(["--cwd", cwd])
    return args


def _aggregate_args(
    ids: list[str], jsonls: list[str] | None, cwd: str | None,
) -> list[str]:
    args = ["--aggregate", *ids]
    if jsonls:
        for p in jsonls:
            args.extend(["--session-jsonl", p])
    elif cwd:
        args.extend(["--cwd", cwd])
    return args


def run_integration(cfg: dict[str, Any]) -> int:
    """Run the integration check suite against the config-specified sessions.

    Returns 0 if all checks pass, 1 if any fail. Prints per-check PASS/FAIL
    to stdout and a summary line at the end.
    """
    sid = cfg.get("sessionId")
    session_jsonl = cfg.get("sessionJsonl")
    cwd = cfg.get("cwd")
    agg_ids = cfg.get("sessionIdsForAggregate", []) or []
    agg_jsonls = cfg.get("sessionJsonlsForAggregate", []) or None

    if not sid:
        print("ERROR: config must contain at least 'sessionId'", file=sys.stderr)
        return 2

    passed = 0
    failed = 0
    total = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal passed, failed, total
        total += 1
        status = "PASS" if ok else "FAIL"
        suffix = f" -- {detail}" if detail else ""
        print(f"  [{status}] {name}{suffix}")
        if ok:
            passed += 1
        else:
            failed += 1

    with tempfile.TemporaryDirectory(prefix="chat-report-verify-") as tmpdir:
        out = Path(tmpdir)

        # --- Test 1: Single-chat mode (locked shape contract) ---
        print("\n[Test 1] Single-chat mode (locked shape)")
        r = run_report(
            *_single_args(sid, session_jsonl, cwd),
            "--format", "json", out_dir=out,
        )
        check("exit code 0", r.returncode == 0,
              f"rc={r.returncode}; stderr={r.stderr[:200]}")
        json_file = out / f"{sid}.json"
        check("JSON output exists", json_file.exists())
        if json_file.exists():
            data = json.loads(json_file.read_text(encoding="utf-8"))
            ok, detail = _check_locked(
                contract.assert_locked_single_chat_conforms, data,
            )
            check("locked single-chat conformance", ok, detail)
            check("ide is 'claude-code'", data.get("ide") == "claude-code",
                  f"ide={data.get('ide')!r}")

        # --- Test 2: Subagent rollup additive field (data-permitting) ---
        if json_file.exists():
            print("\n[Test 2] Subagent rollup additive behavior")
            data = json.loads(json_file.read_text(encoding="utf-8"))
            agg = data.get("aggregates", {})
            ok, name, detail = check_subagent_rollup(agg)
            check(name, ok, detail)

        # --- Test 3: Invalid session UUID error path ---
        print("\n[Test 3] Invalid session UUID error path")
        fake_uuid = "00000000-0000-0000-0000-000000000000"
        # Use an explicit nonexistent JSONL so the test is hermetic even when
        # cwd resolution could happen to find a stray file.
        bogus_jsonl = str(out / f"{fake_uuid}.does-not-exist.jsonl")
        r = run_report(fake_uuid, "--session-jsonl", bogus_jsonl,
                       "--format", "json", out_dir=out)
        check("exit code non-zero", r.returncode != 0, f"rc={r.returncode}")
        check("error mentions JSONL not found",
              "not found" in r.stderr.lower(), r.stderr[:200])

        # --- Test 4: Diff mode (locked shape) ---
        if len(agg_ids) >= 2:
            print("\n[Test 4] Diff mode (locked shape)")
            id_a, id_b = agg_ids[0], agg_ids[1]
            diff_jsonls = agg_jsonls[:2] if agg_jsonls else None
            r = run_report(
                *_diff_args([id_a, id_b], diff_jsonls, cwd),
                "--format", "json", out_dir=out,
            )
            check("exit code 0", r.returncode == 0,
                  f"rc={r.returncode}; stderr={r.stderr[:200]}")
            diff_files = list(out.glob("diff-*.json"))
            check("diff JSON exists", len(diff_files) > 0)
            if diff_files:
                diff_data = json.loads(diff_files[0].read_text(encoding="utf-8"))
                ok, detail = _check_locked(
                    contract.assert_locked_diff_conforms, diff_data,
                )
                check("locked diff conformance", ok, detail)
        else:
            print("\n[Test 4] SKIPPED -- need >=2 sessionIdsForAggregate")

        # --- Test 5: Aggregate mode (locked shape + session_count math) ---
        if len(agg_ids) >= 2:
            print("\n[Test 5] Aggregate mode (locked shape)")
            r = run_report(
                *_aggregate_args(agg_ids, agg_jsonls, cwd),
                "--format", "json", out_dir=out,
            )
            check("exit code 0", r.returncode == 0,
                  f"rc={r.returncode}; stderr={r.stderr[:200]}")
            agg_files = list(out.glob("aggregate-*.json"))
            check("aggregate JSON exists", len(agg_files) > 0)
            if agg_files:
                agg_data = json.loads(agg_files[0].read_text(encoding="utf-8"))
                ok, detail = _check_locked(
                    contract.assert_locked_aggregate_conforms, agg_data,
                )
                check("locked aggregate conformance", ok, detail)
                check("session_count matches sessions",
                      agg_data.get("session_count") == len(agg_data.get("sessions", [])),
                      f"session_count={agg_data.get('session_count')}")
        else:
            print("\n[Test 5] SKIPPED -- need >=2 sessionIdsForAggregate")

        # --- Test 6: --diff error path (one id) ---
        print("\n[Test 6] --diff with 1 id (expect failure)")
        r = run_report("--diff", sid, "--format", "json", out_dir=out)
        check("--diff with 1 id fails", r.returncode != 0,
              f"rc={r.returncode}")

    print(f"\n{'=' * 50}")
    print(f"Results: {passed}/{total} passed, {failed} failed")
    return 1 if failed > 0 else 0


def integration_main(config_path: Path) -> int:
    if not config_path.exists():
        print(f"ERROR: config not found: {config_path}", file=sys.stderr)
        print(
            "Copy chat-report-verify.config.template.json to "
            f"{config_path.name} and fill in IDs for your machine, or "
            "use --validate <path.json> for offline validation.",
            file=sys.stderr,
        )
        return 2

    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"ERROR: config is not valid JSON ({config_path}): {e}",
              file=sys.stderr)
        return 2

    return run_integration(cfg)


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Verify chat-report.py output against the locked-shape contract. "
            "Use --validate <path.json> for offline validation (no claudecode "
            "state needed); run without --validate for the full integration "
            "suite (requires --config or chat-report-verify.config.json next "
            "to this script)."
        ),
    )
    ap.add_argument("--validate", type=Path, default=None, metavar="PATH",
                    help="Validate a saved JSON report against the locked shape "
                         "and exit. Auto-detects single-chat / diff / aggregate "
                         "via the report_kind field.")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG, metavar="PATH",
                    help="Path to the integration-mode config JSON "
                         f"(default: {DEFAULT_CONFIG.name} next to this script).")
    args = ap.parse_args(argv)

    if args.validate is not None:
        return validate_json_file(args.validate)

    return integration_main(args.config)


if __name__ == "__main__":
    raise SystemExit(main())
