#!/usr/bin/env python3
"""Verification script for chat-report.py.

Two modes:

* ``--validate <path.json>`` -- offline JSON validation against the locked
  structured-report shape (``common/_locked_contract.py``). Auto-detects
  single-chat / diff / aggregate via the ``report_kind`` field. Returns 0
  on conform, non-zero on violation. No Cursor state DB needed.

* Default (no ``--validate``) -- integration mode. Runs chat-report.py as a
  subprocess against the local Cursor state DB and asserts the emitted JSON
  conforms to the locked shape. Requires ``chat-report-verify.config.json``
  with real chat / request IDs for the local machine (see the template
  alongside this script).
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
# directly via ``python cursor/chat-report-verify.py`` from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from common import _locked_contract as contract  # noqa: E402

SCRIPT = Path(__file__).resolve().parent / "chat-report.py"
CONFIG = Path(__file__).resolve().parent / "chat-report-verify.config.json"
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
# Integration mode -- exercises chat-report.py against the local Cursor state
# ---------------------------------------------------------------------------

def run_report(*cli_args: str, out_dir: Path | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(SCRIPT)]
    if out_dir:
        cmd.extend(["--out", str(out_dir)])
    cmd.extend(cli_args)
    return subprocess.run(cmd, capture_output=True, text=True, cwd=str(WORKSPACE))


def _check_locked(name: str, validator, payload: dict[str, Any]) -> tuple[bool, str]:
    try:
        validator(payload)
    except AssertionError as e:
        return False, str(e)
    return True, ""


def integration_main() -> int:
    if not CONFIG.exists():
        print(f"ERROR: config not found: {CONFIG}", file=sys.stderr)
        print(
            "Copy chat-report-verify.config.template.json and fill in IDs for "
            "your machine, or use --validate <path.json> for offline validation.",
            file=sys.stderr,
        )
        return 2

    with CONFIG.open() as f:
        cfg = json.load(f)

    request_id = cfg.get("requestId")
    chat_id = cfg.get("chatId")
    request_id_no_hashes = cfg.get("requestIdNoHashes")
    chat_ids_for_aggregate = cfg.get("chatIdsForAggregate", [])

    if not chat_id:
        print("ERROR: config must contain at least 'chatId'", file=sys.stderr)
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
        print("\n[Test 1] Single-chat mode (direct chat ID, locked shape)")
        r = run_report(chat_id, "--format", "json", out_dir=out)
        check("exit code 0", r.returncode == 0, f"rc={r.returncode}")
        json_file = out / f"{chat_id}.json"
        check("JSON output exists", json_file.exists())
        if json_file.exists():
            data = json.loads(json_file.read_text(encoding="utf-8"))
            ok, detail = _check_locked("locked single-chat conformance",
                                       contract.assert_locked_single_chat_conforms, data)
            check("locked single-chat conformance", ok, detail)
            check("ide is 'cursor'", data.get("ide") == "cursor",
                  f"ide={data.get('ide')!r}")

        # --- Test 2: Request ID resolution (if provided) ---
        if request_id:
            print("\n[Test 2] Request ID -> chat ID resolution (tracking DB)")
            r = run_report(request_id, "--format", "json", "--verbose", out_dir=out)
            check("exit code 0", r.returncode == 0, f"rc={r.returncode}")
            check("stderr shows resolution",
                  "resolved" in r.stderr.lower() or "request" in r.stderr.lower(),
                  r.stderr[:200])
        else:
            print("\n[Test 2] SKIPPED -- no requestId in config")

        # --- Test 3: Bubble-scan fallback for request IDs without hashes ---
        if request_id_no_hashes:
            print("\n[Test 3] Request ID resolution (bubble-scan fallback)")
            r = run_report(request_id_no_hashes, "--format", "json", "--verbose", out_dir=out)
            check("exit code 0", r.returncode == 0, f"rc={r.returncode}")
            check("stderr shows bubble-scan",
                  "bubble-scan" in r.stderr or "resolved" in r.stderr.lower(),
                  r.stderr[:200])
        else:
            print("\n[Test 3] SKIPPED -- no requestIdNoHashes in config")

        # --- Test 4: Invalid UUID error path ---
        print("\n[Test 4] Invalid UUID error path")
        fake_uuid = "00000000-0000-0000-0000-000000000000"
        r = run_report(fake_uuid, "--format", "json", out_dir=out)
        check("exit code non-zero", r.returncode != 0)
        check("error mentions resolution paths",
              "ai_code_hashes" in r.stderr or "bubble" in r.stderr.lower() or "composerData" in r.stderr,
              r.stderr[:300])

        # --- Test 5: Forced --request-id on a chat ID (should fail) ---
        print("\n[Test 5] Forced --request-id with a chat ID (expect failure)")
        r = run_report("--request-id", chat_id, "--format", "json", out_dir=out)
        check("exit code non-zero", r.returncode != 0, f"rc={r.returncode}")
        check("error mentions forced flag",
              "request-id" in r.stderr.lower() or "not a request" in r.stderr.lower(),
              r.stderr[:200])

        # --- Test 6: Diff mode (locked shape contract) ---
        if len(chat_ids_for_aggregate) >= 2:
            print("\n[Test 6] Diff mode (locked shape)")
            id_a, id_b = chat_ids_for_aggregate[0], chat_ids_for_aggregate[1]
            r = run_report("--diff", id_a, id_b, "--format", "json", out_dir=out)
            check("exit code 0", r.returncode == 0, f"rc={r.returncode}")
            diff_files = list(out.glob("diff-*.json"))
            check("diff JSON exists", len(diff_files) > 0)
            if diff_files:
                diff_data = json.loads(diff_files[0].read_text(encoding="utf-8"))
                ok, detail = _check_locked("locked diff conformance",
                                           contract.assert_locked_diff_conforms, diff_data)
                check("locked diff conformance", ok, detail)
        else:
            print("\n[Test 6] SKIPPED -- need >=2 chatIdsForAggregate")

        # --- Test 7: Aggregate mode (locked shape contract) ---
        if len(chat_ids_for_aggregate) >= 2:
            print("\n[Test 7] Aggregate mode (locked shape)")
            r = run_report("--aggregate", *chat_ids_for_aggregate, "--format", "json", out_dir=out)
            check("exit code 0", r.returncode == 0, f"rc={r.returncode}")
            agg_files = list(out.glob("aggregate-*.json"))
            check("aggregate JSON exists", len(agg_files) > 0)
            if agg_files:
                agg_data = json.loads(agg_files[0].read_text(encoding="utf-8"))
                ok, detail = _check_locked("locked aggregate conformance",
                                           contract.assert_locked_aggregate_conforms, agg_data)
                check("locked aggregate conformance", ok, detail)
                check("session_count matches sessions",
                      agg_data.get("session_count") == len(agg_data.get("sessions", [])),
                      f"session_count={agg_data.get('session_count')}")
        else:
            print("\n[Test 7] SKIPPED -- need >=2 chatIdsForAggregate")

        # --- Test 8: Denial-marker auto-extraction ---
        print("\n[Test 8] Denial-marker auto-extraction")
        r = run_report(chat_id, "--format", "json", "--verbose",
                       "--workspace", str(WORKSPACE), out_dir=out)
        check("exit code 0", r.returncode == 0, f"rc={r.returncode}")
        check("stderr lists markers", "denial-markers" in r.stderr.lower(), r.stderr[:300])
        check("at least one auto-extracted marker", "auto:" in r.stderr, r.stderr[:400])

        # --- Test 9: CLI --denial-marker override ---
        print("\n[Test 9] CLI --denial-marker override")
        r = run_report(chat_id, "--format", "json", "--verbose",
                       "--denial-marker", "custom-test-denial-marker-12345",
                       out_dir=out)
        check("exit code 0", r.returncode == 0, f"rc={r.returncode}")
        check("custom marker in verbose output",
              "custom-test-denial-marker-12345" in r.stderr, r.stderr[:300])

        # --- Test 10: --diff error path ---
        print("\n[Test 10] Error paths")
        r = run_report("--diff", chat_id, "--format", "json", out_dir=out)
        check("--diff with 1 ID fails", r.returncode != 0)

        # --- Test 11: Legacy shape deprecation warning ---
        print("\n[Test 11] --shape=legacy deprecation warning")
        r = run_report(chat_id, "--format", "json", "--shape", "legacy", out_dir=out)
        check("exit code 0 (deprecated but still works)",
              r.returncode == 0, f"rc={r.returncode}")
        check("DeprecationWarning emitted",
              "DeprecationWarning" in r.stderr and "--shape=legacy" in r.stderr,
              r.stderr[:300])

    print(f"\n{'=' * 50}")
    print(f"Results: {passed}/{total} passed, {failed} failed")
    return 1 if failed > 0 else 0


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Verify chat-report.py output against the locked-shape contract. "
            "Use --validate <path.json> for offline validation (no Cursor DB "
            "needed); run without --validate for the full integration suite."
        ),
    )
    ap.add_argument("--validate", type=Path, default=None, metavar="PATH",
                    help="Validate a saved JSON report against the locked shape "
                         "and exit. Auto-detects single-chat / diff / aggregate "
                         "via the report_kind field.")
    args = ap.parse_args(argv)

    if args.validate is not None:
        return validate_json_file(args.validate)

    return integration_main()


if __name__ == "__main__":
    raise SystemExit(main())
