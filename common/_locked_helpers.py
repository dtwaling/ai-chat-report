"""IDE-agnostic helpers for the locked structured-report shape.

Three small functions that every IDE variant needs identically:

- ``normalize_session_id``: canonical session-ID form for cross-IDE
  aggregation (MISSION-BRIEF section 4.1).
- ``classify_informed_precision_read``: the informed-vs-uninformed Read
  classification per ``docs/telemetry-heuristic.md``. Operates on locked-
  shape tool_call dicts so it's strictly IDE-independent.
- ``compute_success_metric_components``: pre-computes Component A and B
  per ``docs/decisions/success-metric.md`` from an aggregates ``by_class``
  block plus informed/uninformed Read counts.

Extracted from the cursor variant in PR-B3A so the Claude Code variant can
import the same logic without reaching into a sibling-IDE package or
duplicating the math. The cursor variant now re-exports these from here.
"""

from __future__ import annotations

import re
from typing import Any


def normalize_session_id(session_id: str) -> str:
    """Canonical session-ID form: lowercase, non-alphanumerics stripped.

    Enables cross-IDE aggregation by content rather than IDE-specific format.
    """
    return re.sub(r"[^a-z0-9]", "", (session_id or "").lower())


def classify_informed_precision_read(
    tool_call: dict[str, Any],
    prior_in_turn: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Classify a tool_call against the informed-precision-read heuristic.

    ``tool_call`` is a locked-shape tool_call dict (or any dict with
    ``tool_class`` set). ``prior_in_turn`` is the list of locked-shape
    tool_call dicts that preceded this one within the same agent turn.

    A Read is "informed" if it follows a DIRECTORY_INDEX.md read in the
    same turn; otherwise "uninformed". Non-Read tool calls (including the
    dir-index read itself) are classified not-applicable.
    """
    tool_class = tool_call.get("tool_class")
    if tool_class != "broad-sweep-read":
        return {
            "applicable": False,
            "classification": "n/a",
            "reason": f"tool_class={tool_class!r} is not a broad-sweep Read",
        }
    prior = prior_in_turn or []
    saw_dir_index = any(p.get("tool_class") == "directory-index-read" for p in prior)
    if saw_dir_index:
        return {
            "applicable": True,
            "classification": "informed",
            "reason": "preceded by a DIRECTORY_INDEX.md read in the same turn",
        }
    return {
        "applicable": True,
        "classification": "uninformed",
        "reason": "no DIRECTORY_INDEX.md read preceded this Read in the same turn",
    }


def compute_success_metric_components(
    by_class: dict[str, int],
    informed_count: int,
    uninformed_count: int,
) -> dict[str, Any]:
    """Compute Components A and B per ``docs/decisions/success-metric.md``.

    Component A: optimus-mcp count >= 1.0x (broad-sweep-read + grep + glob).
    Component B: informed-precision-read count >= 1.0x uninformed-read count.
    Overall: A AND B. partial_pass: A XOR B.

    Edge cases:
    - Zero denominator with zero numerator => ratio 1.0, pass=True (trivial).
    - Zero denominator with positive numerator => ratio inf, pass=True.
    """
    optimus = int(by_class.get("optimus-mcp", 0))
    broad_sweep = sum(int(by_class.get(k, 0)) for k in (
        "broad-sweep-read", "broad-sweep-grep", "broad-sweep-glob",
    ))
    if broad_sweep == 0:
        ratio_a = 1.0 if optimus == 0 else float("inf")
    else:
        ratio_a = optimus / broad_sweep
    pass_a = optimus >= broad_sweep

    informed = int(informed_count)
    uninformed = int(uninformed_count)
    if uninformed == 0:
        ratio_b = 1.0 if informed == 0 else float("inf")
    else:
        ratio_b = informed / uninformed
    pass_b = informed >= uninformed

    overall_pass = pass_a and pass_b
    partial_pass = (pass_a or pass_b) and not overall_pass

    return {
        "component_a": {
            "definition": "optimus_* count >= 1.0x broad-sweep (read+grep+glob) count",
            "optimus_count": optimus,
            "broad_sweep_count": broad_sweep,
            "ratio": ratio_a,
            "pass": pass_a,
        },
        "component_b": {
            "definition": "informed-precision-read count >= 1.0x uninformed-read count",
            "informed_count": informed,
            "uninformed_count": uninformed,
            "ratio": ratio_b,
            "pass": pass_b,
            "heuristic_failure_modes_flagged": [],
        },
        "overall": {
            "pass": overall_pass,
            "partial_pass": partial_pass,
        },
    }
