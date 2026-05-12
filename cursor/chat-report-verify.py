#!/usr/bin/env python3
"""Verification script for chat-report.py — exercises all modes, ID resolution,
and denial-marker extraction.

Runs chat-report.py as a subprocess so the argparse layer is exercised.
Prints PASS/FAIL lines and exits non-zero on any failure.

Requires: tools/chat-report-verify.config.json with test IDs for the local
machine. See chat-report-verify.config.template.json for the schema.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "chat-report.py"
CONFIG = Path(__file__).resolve().parent / "chat-report-verify.config.json"
WORKSPACE = Path(__file__).resolve().parent.parent


def run_report(*cli_args: str, out_dir: Path | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(SCRIPT)]
    if out_dir:
        cmd.extend(["--out", str(out_dir)])
    cmd.extend(cli_args)
    return subprocess.run(cmd, capture_output=True, text=True, cwd=str(WORKSPACE))


def test_result(name: str, passed: bool, detail: str = "") -> bool:
    status = "PASS" if passed else "FAIL"
    suffix = f" — {detail}" if detail else ""
    print(f"  [{status}] {name}{suffix}")
    return passed


def main() -> int:
    if not CONFIG.exists():
        print(f"ERROR: config not found: {CONFIG}", file=sys.stderr)
        print("Copy chat-report-verify.config.template.json and fill in IDs for your machine.", file=sys.stderr)
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
        if test_result(name, ok, detail):
            passed += 1
        else:
            failed += 1

    with tempfile.TemporaryDirectory(prefix="chat-report-verify-") as tmpdir:
        out = Path(tmpdir)

        # --- Test 1: Single-chat mode with direct chat ID ---
        print("\n[Test 1] Single-chat mode (direct chat ID)")
        r = run_report(chat_id, "--format", "json", out_dir=out)
        check("exit code 0", r.returncode == 0, f"rc={r.returncode}")
        json_file = out / f"{chat_id}.json"
        check("JSON output exists", json_file.exists())
        if json_file.exists():
            data = json.loads(json_file.read_text())
            check("chatId in output", data.get("chatId") == chat_id)
            check("summary present", "summary" in data)
            check("toolTrajectory present", "toolTrajectory" in data)

        # --- Test 2: Request ID resolution (if provided) ---
        if request_id:
            print("\n[Test 2] Request ID → chat ID resolution (tracking DB)")
            r = run_report(request_id, "--format", "json", "--verbose", out_dir=out)
            check("exit code 0", r.returncode == 0, f"rc={r.returncode}")
            check("stderr shows resolution", "resolved" in r.stderr.lower() or "request" in r.stderr.lower(),
                  r.stderr[:200])
        else:
            print("\n[Test 2] SKIPPED — no requestId in config")

        # --- Test 3: Request ID with no ai_code_hashes (bubble scan) ---
        if request_id_no_hashes:
            print("\n[Test 3] Request ID resolution (bubble-scan fallback)")
            r = run_report(request_id_no_hashes, "--format", "json", "--verbose", out_dir=out)
            check("exit code 0", r.returncode == 0, f"rc={r.returncode}")
            check("stderr shows bubble-scan", "bubble-scan" in r.stderr or "resolved" in r.stderr.lower(),
                  r.stderr[:200])
        else:
            print("\n[Test 3] SKIPPED — no requestIdNoHashes in config")

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
        check("exit code non-zero", r.returncode != 0,
              f"rc={r.returncode}")
        check("error mentions forced flag", "request-id" in r.stderr.lower() or "not a request" in r.stderr.lower(),
              r.stderr[:200])

        # --- Test 6: Diff mode ---
        if len(chat_ids_for_aggregate) >= 2:
            print("\n[Test 6] Diff mode")
            id_a, id_b = chat_ids_for_aggregate[0], chat_ids_for_aggregate[1]
            r = run_report("--diff", id_a, id_b, "--format", "json", out_dir=out)
            check("exit code 0", r.returncode == 0, f"rc={r.returncode}")
            diff_files = list(out.glob("diff-*.json"))
            check("diff JSON exists", len(diff_files) > 0)
            if diff_files:
                diff_data = json.loads(diff_files[0].read_text())
                check("toolCountDelta present", "toolCountDelta" in diff_data)
                check("newFailureModes present", "newFailureModes" in diff_data)
                check("resolvedFailureModes present", "resolvedFailureModes" in diff_data)
                check("categoryShift present", "categoryShift" in diff_data)
                check("guardHitDelta present", "guardHitDelta" in diff_data)
        else:
            print("\n[Test 6] SKIPPED — need ≥2 chatIdsForAggregate")

        # --- Test 7: Aggregate mode ---
        if len(chat_ids_for_aggregate) >= 2:
            print("\n[Test 7] Aggregate mode")
            r = run_report("--aggregate", *chat_ids_for_aggregate, "--format", "json", out_dir=out)
            check("exit code 0", r.returncode == 0, f"rc={r.returncode}")
            agg_files = list(out.glob("aggregate-*.json"))
            check("aggregate JSON exists", len(agg_files) > 0)
            if agg_files:
                agg_data = json.loads(agg_files[0].read_text())
                check("chatInventory present", "chatInventory" in agg_data)
                check("perToolRollup present", "perToolRollup" in agg_data)
                check("failureModeFrequency present", "failureModeFrequency" in agg_data)
                check("chatCount matches",
                      agg_data.get("chatCount", 0) >= 1,
                      f"chatCount={agg_data.get('chatCount')}")

                # Cross-check: total calls in aggregate == sum of individual
                individual_totals: dict[str, int] = {}
                for cid in chat_ids_for_aggregate:
                    ir = run_report(cid, "--format", "json", out_dir=out)
                    if ir.returncode == 0:
                        ipath = out / f"{cid}.json"
                        if ipath.exists():
                            idata = json.loads(ipath.read_text())
                            for tname, cnt in idata.get("summary", {}).get("toolCallsByName", {}).items():
                                individual_totals[tname] = individual_totals.get(tname, 0) + cnt

                agg_totals: dict[str, int] = {}
                for tname, rdata in agg_data.get("perToolRollup", {}).items():
                    agg_totals[tname] = rdata.get("calls", 0)

                totals_match = all(
                    agg_totals.get(t, 0) == individual_totals.get(t, 0)
                    for t in set(agg_totals) | set(individual_totals)
                )
                check("aggregate totals match individual sums", totals_match,
                      f"agg={sum(agg_totals.values())} indiv={sum(individual_totals.values())}")
        else:
            print("\n[Test 7] SKIPPED — need ≥2 chatIdsForAggregate")

        # --- Test 8: Denial-marker auto-extraction ---
        print("\n[Test 8] Denial-marker auto-extraction")
        r = run_report(chat_id, "--format", "json", "--verbose",
                       "--workspace", str(WORKSPACE), out_dir=out)
        check("exit code 0", r.returncode == 0, f"rc={r.returncode}")
        check("stderr lists markers", "denial-markers" in r.stderr.lower(), r.stderr[:300])
        has_auto = "auto:" in r.stderr
        check("at least one auto-extracted marker", has_auto, r.stderr[:400])

        # --- Test 9: CLI --denial-marker override ---
        print("\n[Test 9] CLI --denial-marker override")
        r = run_report(chat_id, "--format", "json", "--verbose",
                       "--denial-marker", "custom-test-denial-marker-12345",
                       out_dir=out)
        check("exit code 0", r.returncode == 0, f"rc={r.returncode}")
        check("custom marker in verbose output", "custom-test-denial-marker-12345" in r.stderr,
              r.stderr[:300])

        # --- Test 10: Error paths for --diff ---
        print("\n[Test 10] Error paths")
        r = run_report("--diff", chat_id, "--format", "json", out_dir=out)
        check("--diff with 1 ID fails", r.returncode != 0)

    print(f"\n{'='*50}")
    print(f"Results: {passed}/{total} passed, {failed} failed")
    return 1 if failed > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
