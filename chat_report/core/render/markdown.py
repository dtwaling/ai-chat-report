"""Human-readable Markdown renderers for v2 report payloads."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any


_CATEGORIES = ("system_prompt", "rules", "skills", "tools", "mcp", "subagents", "conversation")


def _text(value: Any, fallback: str = "unknown") -> str:
    return str(value) if value not in (None, "") else fallback


def _lines(items: list[str]) -> str:
    return "\n".join(f"- `{item}`" for item in items) if items else "- None"


def _short(value: str, limit: int = 180) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else f"{compact[:limit - 3]}..."


def render_single_session_md(report: dict[str, Any]) -> str:
    """Render a complete seven-section single-session report."""
    session, aggregates, diagnostics, turns = report["session"], report["aggregates"], report["diagnostics"], report["turns"]
    token = session["token_breakdown"]
    categories = list(_CATEGORIES)
    if "summarized_conversation" in token["by_category"]:
        categories.append("summarized_conversation")
    denominator = sum(int((token["by_category"].get(name) or {}).get("tokens") or 0) for name in token["by_category"])
    if denominator <= 0:
        denominator = int(token.get("total_context_tokens_used") or 0)
    request_ids = sorted({turn["request_id"] for turn in turns if turn.get("request_id")})
    summary = aggregates["customization_summary"]
    discovered = sorted({call.get("mcp_namespace") for turn in turns for call in turn.get("tool_calls", []) if call.get("canonical_kind") == "mcp_discovery" and call.get("mcp_namespace")})
    namespace_counts = aggregates["mcp_calls_by_namespace"]
    sections = [
        "# AI Chat Report",
        "## 1. Session Executive Summary",
        f"- Title: {_text(session['title'])}",
        f"- Session ID: `{session['session_id']}`",
        f"- Request ID(s): {', '.join(f'`{item}`' for item in request_ids) if request_ids else 'None'}",
        f"- Model: {_text(session['model_name'])} (maxMode={session['max_mode']}, reasoning_effort={_text(session['reasoning_effort'], 'none')})",
        f"- Duration: {_text(session['duration_ms'], 'unknown')} ms",
        f"- Status: {_text(session['status'])}",
        f"- Lines: +{session['total_lines_added']} / -{session['total_lines_removed']}",
        f"- Orphan Bubbles Filtered: {session['orphan_bubble_count']}",
        "",
        "## 2. Prompt & Context Anatomy",
        "| Category | Tokens | % of prompt budget |",
        "| --- | ---: | ---: |",
    ]
    for category in categories:
        values = token["by_category"].get(category, {})
        count = int(values.get("tokens") or 0)
        percent = (count / denominator * 100.0) if denominator > 0 else 0.0
        sections.append(f"| {category} | {count} | {percent:.1f}% |")
    sections.extend([
        "",
        "### Attached Rules & Docs",
        _lines(summary["attached_rules"]),
        "### Skills Read (SKILL.md)",
        _lines(summary["skills_read"]),
        "### Rules & Agent Docs Read (.mdc, .cursorrules, AGENTS.md, CLAUDE.md)",
        _lines(summary["rules_read"]),
        "### MCP Namespaces Discovered & Called",
        _lines([f"{name}: {namespace_counts.get(name, 0)} calls" for name in sorted(set(discovered) | set(namespace_counts))]),
        "### Subagents Dispatched",
        _lines(
            [f"Total spawned: {summary['subagents_spawned']}"]
            + [f"{name}: {count}" for name, count in aggregates["subagent_rollup"]["by_type"].items()]
        ),
        "",
        "## 3. Diagnostic Findings",
    ])
    if not diagnostics["findings"]:
        sections.append("0 anomalies detected")
    else:
        for finding in diagnostics["findings"]:
            sections.append(f"- [{finding['severity'].upper()}] {finding['id']} (turn={finding['turn_index']}, bubble={finding['bubble_id']}): {finding['summary']}")
            if finding.get("error_message"):
                sections.append(f"  - Error: {_short(finding['error_message'])}")
            if finding.get("thinking_snippet"):
                sections.append(f"  - Thinking: {_short(finding['thinking_snippet'])}")
    calls = [call for turn in turns for call in turn.get("tool_calls", [])]
    by_class: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_tool: Counter[str] = Counter()
    for call in calls:
        by_class[call.get("semantic_class", "unknown")].append(call)
        by_tool[call.get("raw_tool_name", "unknown")] += 1
    sections.extend(["", "## 4. Tool & Timing Breakdown", "| Class | Calls | Errors | Denials | avg_ms | max_ms | total_ms |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"])
    for name, group in sorted(by_class.items()):
        durations = [call["elapsed_ms"] for call in group if call.get("elapsed_ms") is not None]
        total = sum(durations)
        average = total / len(durations) if durations else 0.0
        sections.append(f"| {name} | {len(group)} | {sum(bool(call.get('is_error')) for call in group)} | {sum(bool(call.get('is_denied')) for call in group)} | {average:.1f} | {max(durations) if durations else 0} | {total} |")
    sections.append(f"- Per-tool counts: {', '.join(f'{name}={count}' for name, count in sorted(by_tool.items())) or 'None'}")
    sections.extend(["", "## 5. Subagent Rollup", "| Type | Session ID | Turns | Tool Calls | Errors | Skills Read | Duration ms |", "| --- | --- | ---: | ---: | ---: | --- | ---: |"])
    for subagent in session["subagents"]:
        sections.append(f"| {_text(subagent['subagent_type'])} | `{subagent['session_id']}` | {subagent['turn_count']} | {subagent['tool_call_count']} | {subagent['error_count']} | {', '.join(subagent['skills_read']) or 'None'} | {_text(subagent['duration_ms'], 'unknown')} |")
    if not session["subagents"]:
        sections.append("| None | None | 0 | 0 | 0 | None | 0 |")
    sections.extend(["", "## 6. Turn-by-Turn Execution Timeline"])
    for turn in turns:
        sections.extend([f"### Turn {turn['turn_index']}", f"- User Prompt Preview: {_short(turn.get('user_text') or '', 220)}"])
        for thinking in turn.get("thinking_blocks", []):
            sections.append(f"- Thinking ({_text(thinking.get('duration_ms'), 'unknown')} ms): {_short(thinking.get('text') or '')}")
        for call in turn.get("tool_calls", []):
            sections.append(f"- Tool: `{call.get('raw_tool_name')}` [{call.get('status')}] {_text(call.get('elapsed_ms'), 'unknown')} ms")
        for assistant_text in turn.get("assistant_texts", []):
            sections.append(f"- Assistant Output Summary: {_short(assistant_text)}")
    attribution = session.get("attribution")
    sections.extend(["", "## 7. Git Code Attribution"])
    if attribution is None:
        sections.append("Attribution unavailable.")
    else:
        sections.extend([
            f"- Composer code hashes: {attribution['composer_code_hashes_count']}",
            f"- Human code hashes: {attribution['human_code_hashes_count']}",
            f"- Distinct files touched: {', '.join(attribution['distinct_files_touched']) or 'None'}",
            "### Scored Commits",
            _lines([f"{item.get('commitHash', 'unknown')}: {item.get('commitMessage', '')}" for item in attribution["scored_commits"]]),
        ])
    return "\n".join(sections).rstrip() + "\n"


def render_diff_md(report: dict[str, Any]) -> str:
    """Render the five-section diff report."""
    delta = report["delta"]
    transitions = delta["diagnostics_delta"]
    return "\n".join([
        "# AI Chat Report Diff",
        "## 1. Compared Sessions",
        f"- Before: `{report['before']['session']['session_id']}`",
        f"- After: `{report['after']['session']['session_id']}`",
        "## 2. Execution Deltas",
        f"- Turns: {delta['turn_count_delta']:+d}; tools: {delta['tool_calls_total_delta']:+d}; duration: {_text(delta['duration_ms_delta'], 'unknown')} ms",
        "## 3. Customization Changes",
        f"- Skills added: {', '.join(delta['skills_read_added']) or 'None'}",
        f"- Skills removed: {', '.join(delta['skills_read_removed']) or 'None'}",
        f"- Rules added: {', '.join(delta['rules_read_added']) or 'None'}",
        f"- Rules removed: {', '.join(delta['rules_read_removed']) or 'None'}",
        "## 4. Diagnostic Transitions",
        f"- New finding IDs: {', '.join(transitions['new_finding_ids']) or 'None'}",
        f"- Resolved finding IDs: {', '.join(transitions['resolved_finding_ids']) or 'None'}",
        f"- Persisting finding keys: {', '.join(transitions['persisting_finding_keys']) or 'None'}",
        "## 5. Attribution Delta",
        f"- Composer hashes: {delta['attribution_delta']['composer_code_hashes_delta']:+d}; human hashes: {delta['attribution_delta']['human_code_hashes_delta']:+d}",
        f"- Lines: {delta['attribution_delta']['lines_added_delta']:+d} / {delta['attribution_delta']['lines_removed_delta']:+d}",
        "",
    ])


def render_aggregate_md(report: dict[str, Any]) -> str:
    """Render the five-section aggregate report."""
    rollup = report["rollup"]
    custom = rollup["customization_rollup"]
    return "\n".join([
        "# AI Chat Report Aggregate",
        "## 1. Cohort Summary",
        f"- Sessions: {report['session_count']}; turns: {rollup['total_turns']}; tools: {rollup['total_tool_calls']}",
        "## 2. Tool & Status Rollup",
        f"- Semantic classes: {rollup['tool_calls_by_semantic_class']}",
        f"- Statuses: {rollup['status_counts']}",
        "## 3. Diagnostic Cohort Findings",
        f"- Findings: {rollup['diagnostics_rollup']['total_findings']}",
        f"- By heuristic: {rollup['diagnostics_rollup']['by_heuristic_id']}",
        "## 4. Customization Frequencies",
        f"- Skills: {custom['skills_read_frequency']}",
        f"- Rules: {custom['rules_read_frequency']}",
        f"- Subagents: {custom['total_subagents_spawned']}",
        "## 5. Attribution Rollup",
        f"- Totals: {rollup['attribution_totals']}",
        "",
    ])
