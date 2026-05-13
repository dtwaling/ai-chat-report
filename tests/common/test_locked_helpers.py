"""Tests for common/_locked_helpers.py.

The three helpers (``normalize_session_id``, ``classify_informed_precision_read``,
``compute_success_metric_components``) were extracted from cursor/chat-report.py
in PR-B3A. The cursor variant's existing test suite already exercises them
indirectly via the full pipeline; this module pins their behavior directly
so future refactors get a clear regression signal.
"""

from __future__ import annotations

import math

import pytest

from common import _locked_helpers as h


# ---------------------------------------------------------------------------
# normalize_session_id
# ---------------------------------------------------------------------------


def test_normalize_session_id_strips_hyphens_and_lowercases():
    assert h.normalize_session_id("D5093750-DD1A-4A15-ADAE-79334235A2E7") == \
        "d5093750dd1a4a15adae79334235a2e7"


def test_normalize_session_id_empty_input():
    assert h.normalize_session_id("") == ""
    assert h.normalize_session_id(None) == ""  # type: ignore[arg-type]


def test_normalize_session_id_strips_underscores_and_other_punct():
    assert h.normalize_session_id("abc_DEF-123.xyz") == "abcdef123xyz"


# ---------------------------------------------------------------------------
# classify_informed_precision_read
# ---------------------------------------------------------------------------


def _read_call() -> dict:
    return {"tool_class": "broad-sweep-read"}


def _dir_index_call() -> dict:
    return {"tool_class": "directory-index-read"}


def _other_call(cls: str = "other") -> dict:
    return {"tool_class": cls}


def test_ipr_non_read_returns_not_applicable():
    out = h.classify_informed_precision_read(_other_call("bash"))
    assert out["applicable"] is False
    assert out["classification"] == "n/a"
    assert "bash" in out["reason"]


def test_ipr_read_with_no_prior_is_uninformed():
    out = h.classify_informed_precision_read(_read_call(), prior_in_turn=[])
    assert out["applicable"] is True
    assert out["classification"] == "uninformed"


def test_ipr_read_after_dir_index_in_same_turn_is_informed():
    out = h.classify_informed_precision_read(
        _read_call(),
        prior_in_turn=[_dir_index_call()],
    )
    assert out["applicable"] is True
    assert out["classification"] == "informed"


def test_ipr_read_after_other_reads_only_is_still_uninformed():
    out = h.classify_informed_precision_read(
        _read_call(),
        prior_in_turn=[_read_call(), _read_call(), _other_call("bash")],
    )
    assert out["classification"] == "uninformed"


def test_ipr_dir_index_call_itself_is_not_applicable():
    """The DIRECTORY_INDEX.md read is the predicate, not the subject."""
    out = h.classify_informed_precision_read(_dir_index_call())
    assert out["applicable"] is False
    assert out["classification"] == "n/a"


# ---------------------------------------------------------------------------
# compute_success_metric_components
# ---------------------------------------------------------------------------


def _by_class(**counts: int) -> dict[str, int]:
    base = {
        "broad-sweep-read": 0, "broad-sweep-grep": 0, "broad-sweep-glob": 0,
        "optimus-mcp": 0, "directory-index-read": 0,
        "edit": 0, "write": 0, "bash": 0, "other": 0,
    }
    base.update(counts)
    return base


def test_smc_zero_zero_passes_trivially():
    out = h.compute_success_metric_components(_by_class(), 0, 0)
    assert out["component_a"]["ratio"] == 1.0
    assert out["component_a"]["pass"] is True
    assert out["component_b"]["ratio"] == 1.0
    assert out["component_b"]["pass"] is True
    assert out["overall"]["pass"] is True
    assert out["overall"]["partial_pass"] is False


def test_smc_zero_denominator_positive_numerator_yields_inf_pass():
    by_class = _by_class(**{"optimus-mcp": 5})
    out = h.compute_success_metric_components(by_class, 3, 0)
    assert math.isinf(out["component_a"]["ratio"])
    assert out["component_a"]["pass"] is True
    assert math.isinf(out["component_b"]["ratio"])
    assert out["component_b"]["pass"] is True


def test_smc_component_a_pass_b_fail_is_partial():
    by_class = _by_class(**{"optimus-mcp": 10, "broad-sweep-read": 5})
    out = h.compute_success_metric_components(by_class, 1, 9)
    assert out["component_a"]["pass"] is True   # 10 >= 5
    assert out["component_b"]["pass"] is False  # 1 < 9
    assert out["overall"]["pass"] is False
    assert out["overall"]["partial_pass"] is True


def test_smc_both_fail_is_not_partial():
    by_class = _by_class(**{"optimus-mcp": 1, "broad-sweep-read": 5,
                            "broad-sweep-grep": 5})
    out = h.compute_success_metric_components(by_class, 1, 9)
    assert out["component_a"]["pass"] is False
    assert out["component_b"]["pass"] is False
    assert out["overall"]["pass"] is False
    assert out["overall"]["partial_pass"] is False


def test_smc_broad_sweep_aggregates_three_classes():
    by_class = _by_class(**{
        "broad-sweep-read": 2,
        "broad-sweep-grep": 3,
        "broad-sweep-glob": 5,
        "optimus-mcp": 10,
    })
    out = h.compute_success_metric_components(by_class, 0, 0)
    assert out["component_a"]["broad_sweep_count"] == 10
    assert out["component_a"]["optimus_count"] == 10
    assert out["component_a"]["ratio"] == 1.0
    assert out["component_a"]["pass"] is True


def test_smc_component_b_uninformed_count_drives_ratio_only():
    """informed/uninformed are independent of by_class."""
    out = h.compute_success_metric_components(_by_class(), 5, 5)
    assert out["component_b"]["ratio"] == 1.0
    assert out["component_b"]["pass"] is True
    out2 = h.compute_success_metric_components(_by_class(), 4, 5)
    assert out2["component_b"]["pass"] is False


def test_smc_heuristic_failure_modes_starts_empty():
    out = h.compute_success_metric_components(_by_class(), 1, 1)
    assert out["component_b"]["heuristic_failure_modes_flagged"] == []
