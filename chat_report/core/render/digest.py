"""Strictly bounded Stage 1 agent digest renderer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from chat_report.core.ir import SEVERITY_RANK


MAX_DIGEST_OUTPUT_PATH_CHARS = 300
DIGEST_MAX_CHARS = 2500
_BUDGETS = (800, 400, 750, 550)


def _truncate(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else f"{text[:limit - 3]}..."


def _bounded_path(path: str | Path) -> str:
    value = str(path)
    if len(value) > MAX_DIGEST_OUTPUT_PATH_CHARS:
        raise ValueError(f"Digest output path exceeds {MAX_DIGEST_OUTPUT_PATH_CHARS} characters.")
    return value


def _touched(report: dict[str, Any]) -> list[str]:
    priority = {
        ("explicit_read", "skill"): 0, ("explicit_edit", "skill"): 0,
        ("context_attached", "skill"): 1,
        ("explicit_read", "rule"): 2, ("explicit_edit", "rule"): 2,
        ("explicit_read", "agent_doc"): 2, ("explicit_edit", "agent_doc"): 2,
        ("context_attached", "rule"): 3, ("context_attached", "agent_doc"): 3,
        ("context_attached", "hook"): 3, ("explicit_read", "hook"): 3, ("explicit_edit", "hook"): 3,
    }
    items = [
        (priority[(item["source"], item["kind"])], item["path"])
        for item in report["session"]["customizations"]
        if (item["source"], item["kind"]) in priority
    ]
    return [path for _, path in sorted(set(items), key=lambda item: (item[0], item[1]))]


def _append_budgeted(heading: str, lines: list[str], omitted: str, budget: int, reserve: int = 55) -> str:
    section = heading
    shown = 0
    for line in lines:
        if len(section) + len(line) + reserve > budget:
            break
        section += line
        shown += 1
    remaining = len(lines) - shown
    if remaining:
        suffix = omitted.format(remaining)
        if len(section) + len(suffix) <= budget:
            section += suffix
    return section


def render_agent_digest(report: dict[str, Any], json_path: str | Path, markdown_path: str | Path) -> str:
    """Return a <=2500-character digest for a single-session report."""
    json_file, markdown_file = _bounded_path(json_path), _bounded_path(markdown_path)
    session, aggregates, diagnostics = report["session"], report["aggregates"], report["diagnostics"]
    context_percent = float(session["token_breakdown"]["context_usage_percent"] or 0.0)
    header = (
        f"# Chat Report ({str(session['session_id'])[:64]})\n"
        f"JSON: {json_file}\n"
        f"MD: {markdown_file}\n"
        f"model={str(session['model_name'] or 'unknown')[:40]} "
        f"turns={aggregates['turn_count']} tools={aggregates['tool_calls_total']} "
        f"err={aggregates['status_counts']['error']} den={aggregates['status_counts']['denied']} "
        f"ctx={context_percent:.1f}%\n"
    )
    assert len(header) <= _BUDGETS[0]
    customizations = _touched(report)
    custom_section = _append_budgeted(
        "## Customizations Touched\n",
        [f"- {path}\n" for path in customizations],
        "... (+{} more in JSON session.customizations)\n",
        _BUDGETS[1],
    )
    findings = sorted(
        diagnostics["findings"],
        key=lambda item: (SEVERITY_RANK[item["severity"]], item["turn_index"] or 0, item["id"], item["finding_key"]),
    )
    finding_lines = [
        f"- [{item['severity'].upper()}] {item['id']} (turn={item['turn_index']}, bubble={item['bubble_id']}): "
        f"{_truncate(item['summary'], 100)} | err={_truncate(item.get('error_message'), 70)} | "
        f"think={_truncate(item.get('thinking_snippet'), 70)}\n"
        for item in findings
    ]
    findings_section = _append_budgeted(
        "## Triggered Findings\n",
        finding_lines,
        "... (+{} more in JSON diagnostics.findings)\n",
        _BUDGETS[2],
    )
    warrants_audit = any(item["severity"] in {"high", "medium"} or item["id"] == "H08_INSTRUCTION_FRICTION_SIGNAL" for item in findings)
    footer_lines = [f"- JSON audit input: {json_file}\n"]
    footer_lines.extend(f"- {path}\n" for path in customizations)
    footer = _append_budgeted(
        "## Stage 2 Recommendation\n"
        f"Stage 2 Subagent Audit: {'warranted' if warrants_audit else 'not warranted'}.\n"
        "Audit Inputs:\n",
        footer_lines,
        " (+{} more in JSON)\n",
        _BUDGETS[3],
        reserve=30,
    )
    sections = [header, custom_section, findings_section, footer]
    digest = "".join(sections)
    return digest[:DIGEST_MAX_CHARS]
