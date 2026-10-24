"""Deterministic Stage 1 diagnostics over classified SessionIR data."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from chat_report.core.classifier import canonicalize_path, is_skill_path
from chat_report.core.ir import CustomizationRefIR, DiagnosticFinding, HEURISTIC_IDS, SEVERITY_RANK, SessionIR, ToolCallIR, TurnIR, normalize_session_id


_SCHEMA_ERROR = re.compile(r"(schema|parameter|validation|required|unexpected|invalid argument)", re.I)
_FRICTION = re.compile(r"conflicting instruction|contradicts|rule says .* but|skill says .* but|blocked by|cannot use .* because|workaround|failed again|keep failing|not allowed to use", re.I)
_TEST_COMMAND = re.compile(r"\b(pytest|mypy|pyright|ruff|tsc|eslint|vitest|jest|(?:npm|pnpm|yarn)\s+test|cargo\s+(?:test|check|clippy)|go\s+(?:test|vet)|dotnet\s+(?:test|build)|(?:mvn|gradle)\s+test)", re.I)


def _calls(session: SessionIR) -> list[ToolCallIR]:
    return sorted((call for turn in session.turns for call in turn.tool_calls), key=lambda c: (c.call_index, c.turn_index))


def _finding(identifier: str, subject: str, name: str, severity: str, *, call: ToolCallIR | None = None,
             turn_index: int | None = None, bubble_id: str | None = None,
             summary: str, evidence: dict | None = None, hint: str = "", thinking: str | None = None) -> DiagnosticFinding:
    resolved_turn = turn_index if turn_index is not None else (call.turn_index if call else None)
    resolved_bubble = bubble_id if bubble_id is not None else (call.bubble_id if call else None)
    return DiagnosticFinding(identifier, f"{identifier}:{subject}", name, severity, resolved_turn,
                             resolved_bubble, summary,
                             call.error_message[:160] if call and call.error_message else None,
                             thinking[:160] if thinking else None, evidence or {}, hint)


def _dispatch_role(call: ToolCallIR) -> str:
    for key in ("subagent_type", "name", "subagentType"):
        val = call.input_params.get(key)
        if isinstance(val, str) and val.strip() and val.strip().lower() not in ("unspecified", "unknown", "custom"):
            return val.strip().lower()
    return ""


def _is_gate_role(role: str) -> bool:
    return bool(role) and bool(re.search(r"(?:^|[-_\s])(?:review|reviewer|test|tester)(?:$|[-_\s])", role))


def _unquoted_thinking(text: str) -> str:
    return re.sub(r'`[^`\n]+`|"[^"\n]+"', lambda m: " " * len(m.group(0)), text)

def _search_query(call: ToolCallIR) -> str:
    return str(call.input_params.get("pattern") or call.input_params.get("globPattern") or call.input_summary)


def _empty(call: ToolCallIR) -> bool:
    return not call.is_error and (
        call.output_char_len == 0
        or call.output_summary.strip() in ("", "No matches found", "0 matches", "[]", "{}")
    )


def _groups(calls: list[ToolCallIR], key_fn, predicate) -> list[list[ToolCallIR]]:
    """Build same-turn and consecutive qualifying runs, deduplicated by first call."""
    groups: dict[tuple[str, int], list[ToolCallIR]] = {}
    consecutive: list[ToolCallIR] = []
    for call in calls:
        if predicate(call) and (not consecutive or key_fn(call) == key_fn(consecutive[-1])):
            consecutive.append(call)
        else:
            if len(consecutive) >= 3:
                groups[(key_fn(consecutive[0]), consecutive[0].call_index)] = consecutive[:]
            consecutive = [call] if predicate(call) else []
    if len(consecutive) >= 3:
        groups[(key_fn(consecutive[0]), consecutive[0].call_index)] = consecutive[:]
    by_turn: dict[tuple[int, str], list[ToolCallIR]] = defaultdict(list)
    for call in calls:
        if predicate(call):
            by_turn[(call.turn_index, key_fn(call))].append(call)
    for (_, key), group in by_turn.items():
        if len(group) >= 3:
            group_indexes = {c.call_index for c in group}
            if any(
                existing_key == key and group_indexes <= {c.call_index for c in existing}
                for (existing_key, _), existing in groups.items()
            ):
                continue
            groups.setdefault((key, group[0].call_index), group)
    return list(groups.values())


def _h01(session: SessionIR, workspace: Path | None, calls: list[ToolCallIR]) -> list[DiagnosticFinding]:
    attached_skill_refs = [ref for ref in session.customizations if ref.kind == "skill" and ref.source == "context_attached"]
    known_skill_refs = [ref for ref in session.customizations if ref.kind == "skill" and ref.source in {"context_attached", "workspace_active"}]
    read_paths = {
        canonicalize_path(call.target_path, workspace).lower()
        for call in calls
        if (call.semantic_class == "skill-read" or (call.canonical_kind == "file_read" and is_skill_path(call.target_path))) and not call.is_error
    }
    read_paths.update(
        canonicalize_path(ref.path, workspace).lower()
        for ref in session.customizations
        if ref.kind == "skill" and ref.source == "explicit_read"
    )
    read_paths.update(canonicalize_path(path, workspace).lower() for sub in session.subagents for path in sub.skills_read)
    known_paths = {canonicalize_path(ref.path, workspace).lower() for ref in known_skill_refs}
    non_inlined_manual_turns: dict[str, TurnIR] = {}
    for turn in session.turns:
        manual_sections = re.findall(r"<manually_attached_skills>(.*?)</manually_attached_skills>", turn.user_text, re.S | re.I)
        inline_paths: list[str] = []
        tag_manual_paths: list[str] = []
        for section in manual_sections:
            inlined_for_section = {
                canonicalize_path(m.strip(), workspace).lower()
                for m in re.findall(r"^\s*Path\s*:\s*(.+?)\s*(?:\r?\n)+\s*SKILL\.md\s+content\s*:", section, re.M | re.I)
            }
            for match in re.findall(r"^\s*Path\s*:\s*(.+?)\s*$", section, re.M | re.I):
                cleaned_path = match.strip()
                inline_paths.append(cleaned_path)
                canon_inline = canonicalize_path(cleaned_path, workspace).lower()
                if is_skill_path(canon_inline):
                    if canon_inline in inlined_for_section:
                        read_paths.add(canon_inline)
                    else:
                        non_inlined_manual_turns.setdefault(canon_inline, turn)
            for raw_tag_path in re.findall(r'<agent_skill\s+fullPath=["\']([^"\']+)["\']', section, re.I):
                tag_manual_paths.append(raw_tag_path)
                canon_tag = canonicalize_path(raw_tag_path, workspace).lower()
                if is_skill_path(canon_tag):
                    non_inlined_manual_turns.setdefault(canon_tag, turn)
        for raw_path in re.findall(r'<agent_skill\s+fullPath=["\']([^"\']+)["\']', turn.user_text, re.I) + inline_paths:
            path = canonicalize_path(raw_path, workspace)
            if not is_skill_path(path) or path.lower() in known_paths:
                continue
            source = "context_attached" if (raw_path in inline_paths or raw_path in tag_manual_paths) else "workspace_active"
            ref = CustomizationRefIR("skill", path, source, turn.turn_index)
            known_skill_refs.append(ref)
            if source == "context_attached":
                attached_skill_refs.append(ref)
            known_paths.add(path.lower())
    findings: list[DiagnosticFinding] = []
    attached_paths = {canonicalize_path(ref.path, workspace).lower() for ref in attached_skill_refs}
    for ref in sorted(known_skill_refs, key=lambda r: r.path):
        path = canonicalize_path(ref.path, workspace)
        slug = Path(path).parent.name.lower()
        if not slug:
            continue
        relevant_turn = non_inlined_manual_turns.get(path.lower())
        mentioned = relevant_turn is not None
        for turn in session.turns:
            cleaned_user_text = re.sub(r"<available_skills\b[^>]*>.*?</available_skills>", "", turn.user_text, flags=re.S | re.I)
            prose = {match.lower() for match in re.findall(r"\b(?:use|follow|read|activate)\s+(?:the\s+)?([a-z0-9_-]{3,40})\s+skill\b", cleaned_user_text, re.I)}
            direct = bool(re.search(r"(?<![\w/])/" + re.escape(slug) + r"\b", cleaned_user_text, re.I))
            if direct and path.lower() in attached_paths and path.lower() not in non_inlined_manual_turns:
                read_paths.add(path.lower())
                continue
            if direct or slug in prose:
                mentioned, relevant_turn = True, turn
                break
            if any(
                slug in {
                    match.lower()
                    for pattern in (
                        r"\b(?:skill|SKILL\.md)\s+[`'\"]?([a-z0-9_-]{3,40})[`'\"]?",
                        r"\b(?:use|follow|read|activate)\s+(?:the\s+)?[`'\"]?([a-z0-9_-]{3,40})[`'\"]?\s+skill\b",
                    )
                    for match in re.findall(pattern, thinking.text, re.I)
                }
                for thinking in turn.thinking_blocks
            ):
                mentioned, relevant_turn = True, turn
                break
        tokens = [x for x in re.split(r"[-_]+", slug) if len(x) >= 4 and x not in {"skill", "cursor", "agent", "tool", "core", "plugin", "workflow", "report", "chat"}]
        def matches_domain(call: ToolCallIR) -> bool:
            haystack = " ".join(filter(None, [
                Path(call.target_path).name.lower() if call.target_path else "",
                (call.mcp_namespace or "").lower(),
                (call.mcp_tool_name or "").lower(),
            ]))
            haystack_tokens = set(re.findall(r"[a-z0-9]+", haystack))
            return (
                (slug == "canvas" and (call.target_path or "").lower().endswith(".canvas.tsx"))
                or (slug == "eci-yml" and Path(call.target_path or "").name.lower() == "eci.yml")
                or (bool(tokens) and all(token in haystack_tokens for token in tokens))
            )
        domain_call = next((call for call in calls if path.lower() in attached_paths and call.canonical_kind in {"file_edit", "file_write", "mcp_call"} and matches_domain(call)), None)
        domain = domain_call is not None
        if relevant_turn is None and domain_call is None and ref.turn_index is not None:
            relevant_turn = next((turn for turn in session.turns if turn.turn_index == ref.turn_index), None)
        if path.lower() not in read_paths and (mentioned or domain):
            findings.append(_finding(HEURISTIC_IDS[0], path or slug, "Skill Referenced Without Reading SKILL.md", "high",
                call=domain_call or (ToolCallIR(0, relevant_turn.turn_index, relevant_turn.user_bubble_id, None, None, "", "", "", None, None, None, {}, "", "", 0, "completed", False, False, None, None) if relevant_turn else None),
                summary=f"Skill '{slug}' was referenced or its domain was used without reading it.", evidence={"path": path, "slug": slug, "mentioned": mentioned, "domain_match": domain}, hint="Read the attached skill before acting in its domain."))
    return findings


def evaluate_diagnostics(session: SessionIR, workspace: Path | None = None) -> list[DiagnosticFinding]:
    """Evaluate all eight stable heuristics and return sorted unique findings."""
    calls = _calls(session)
    findings = _h01(session, workspace, calls)
    discovered_catalog = False
    discovered_namespaces: set[str] = set()
    discovered_tools: set[tuple[str, str]] = set()
    for call in calls:
        if call.canonical_kind == "mcp_discovery" and call.status == "completed" and not call.is_error:
            namespace = call.mcp_namespace or call.input_params.get("namespace") or call.input_params.get("server")
            tool = call.mcp_tool_name or call.input_params.get("toolName")
            pattern = call.input_params.get("pattern")
            pairs = call.input_params.get("_discovered_tools") or []
            for pair in pairs:
                if isinstance(pair, (tuple, list)) and len(pair) >= 2:
                    pair_namespace, pair_tool = pair[0], pair[1]
                    if pair_tool:
                        discovered_tools.add((str(pair_namespace), str(pair_tool)))
                    elif pair_namespace and not pattern and not tool:
                        discovered_namespaces.add(str(pair_namespace))
            if not namespace and not tool and not pattern:
                discovered_catalog = True
            elif namespace and not tool and not pattern:
                discovered_namespaces.add(str(namespace))
            elif namespace and tool:
                discovered_tools.add((str(namespace), str(tool)))
        elif call.canonical_kind == "mcp_call" and call.mcp_namespace != "cursor":
            unknown = not discovered_catalog and call.mcp_namespace not in discovered_namespaces and (call.mcp_namespace, call.mcp_tool_name) not in discovered_tools
            invalid = call.is_error and bool(_SCHEMA_ERROR.search(call.error_message or call.output_summary))
            if unknown or invalid:
                findings.append(_finding(HEURISTIC_IDS[1], str(call.call_index), "Dynamic Tool Called Without Discovery", "medium", call=call,
                    summary="MCP call lacked prior schema discovery or failed validation.", evidence={"undiscovered": unknown, "validation_error": invalid}, hint="Discover the namespace or tool schema before calling it."))
    def fail_key(call: ToolCallIR) -> str:
        if call.target_path:
            return f"path:{canonicalize_path(call.target_path, workspace)}"
        if call.mcp_tool_name:
            return f"mcp:{call.mcp_namespace or 'unknown'}:{call.mcp_tool_name}"
        return f"kind:{call.canonical_kind}"
    for group in _groups(calls, fail_key, lambda c: c.is_error or c.is_denied):
        key = fail_key(group[0])
        findings.append(_finding(HEURISTIC_IDS[2], f"4a:{key}:{group[0].call_index}", "Repeated Tool Failure / Retry Loop", "high", call=group[0],
            summary=f"{len(group)} failures shared {key}.", evidence={"branch": "4a", "group_key": key, "call_indexes": [c.call_index for c in group]}, hint="Inspect the first failure and change the next attempt."))
    def empty_key(call: ToolCallIR) -> str:
        return f"{call.canonical_kind}:{_search_query(call)}:{canonicalize_path(call.target_path, workspace)}"
    for group in _groups(calls, empty_key, lambda c: c.canonical_kind in {"search_grep", "search_glob"} and _empty(c)):
        key = empty_key(group[0])
        findings.append(_finding(HEURISTIC_IDS[2], f"4b:{key}:{group[0].call_index}", "Repeated Empty Search", "high", call=group[0],
            summary=f"{len(group)} empty searches repeated the same query.", evidence={"branch": "4b", "call_indexes": [c.call_index for c in group]}, hint="Change the query or search scope after an empty result."))
    for call in calls:
        if call.is_denied:
            recovered = any(not later.is_error and later.status == "completed" and later.turn_index in {call.turn_index, call.turn_index + 1} and later.call_index > call.call_index for later in calls)
            findings.append(_finding(HEURISTIC_IDS[3], str(call.call_index), "Hook or Harness Permission Denial", "high", call=call,
                summary="Tool call was denied by a hook or harness.", evidence={"recovered": recovered, "looped": session.stop_hook_loop_count > 0}, hint="Address the denial reason before retrying."))
    edits: dict[str, list[ToolCallIR]] = defaultdict(list)
    for call in calls:
        if call.canonical_kind in {"file_edit", "file_write"} and call.target_path and not call.is_error:
            edits[canonicalize_path(call.target_path, workspace)].append(call)
    for path, changes in edits.items():
        intervals = 0
        for previous, current in zip(changes, changes[1:]):
            between = [c for c in calls if previous.call_index < c.call_index < current.call_index]
            def validation_targets_file(call: ToolCallIR) -> bool:
                lint_name = call.raw_tool_name.lower() in ("read_lints", "readlints") or (call.mcp_tool_name or "").lower() in ("read_lints", "readlints")
                if lint_name:
                    out_lower = (call.output_summary or "").strip().lower()
                    if "no linter errors" in out_lower or out_lower in ("[]", '{"lints": []}', '{"diagnostics": []}', '{"lints":[]}', '{"diagnostics":[]}'):
                        return False
                    lint_details = f"{call.input_summary} {call.output_summary}".lower()
                    lint_name_match = bool(re.search(rf"(?<![\w.-]){re.escape(Path(path).name.lower())}(?![\w.-])", lint_details))
                    raw_paths = call.input_params.get("paths")
                    if isinstance(raw_paths, list):
                        str_paths = [p for p in raw_paths if isinstance(p, str) and p.strip()]
                        if str_paths:
                            return any(
                                canonicalize_path(p, workspace) == path or path.startswith(f"{canonicalize_path(p, workspace).rstrip('/')}/")
                                for p in str_paths
                            ) or lint_name_match
                    return not call.target_path or canonicalize_path(call.target_path, workspace) == path or lint_name_match
                if call.canonical_kind != "shell_exec" or not call.is_error:
                    return False
                command = str(call.input_params.get("command") or call.input_summary)
                if not _TEST_COMMAND.search(command):
                    return False
                details = f"{command} {call.error_message or ''} {call.output_summary or ''}".lower()
                name, stem = Path(path).name.lower(), Path(path).stem.lower()
                filenames = re.findall(r"\b[\w.-]+\.(?:py|ts|tsx|js|rs|go|cs)\b", command.lower())
                name_match = re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w.-])", details)
                stem_match = len(stem) >= 4 and stem not in {"test", "tests", "main", "init", "index", "conftest"} and re.search(rf"(?<![\w.-]){re.escape(stem)}(?![\w.-])", details)
                return bool(name_match or stem_match or len(filenames) == 0)
            if any(validation_targets_file(c) for c in between):
                intervals += 1
        if len(changes) >= 3 and len({c.turn_index for c in changes}) >= 2 and intervals >= 2:
            findings.append(_finding(HEURISTIC_IDS[4], path, "Edit-Lint-Edit Regression Loop", "medium", call=changes[0],
                summary=f"{len(changes)} edits to {path} crossed {intervals} failed validation intervals.", evidence={"edit_count": len(changes), "validation_intervals": intervals}, hint="Fix the validation failure before making repeated edits."))
    rules = session.token_breakdown.by_category.get("rules", {}).get("tokens", 0)
    skills = session.token_breakdown.by_category.get("skills", {}).get("tokens", 0)
    spike_tool_calls = [c for c in calls if c.output_char_len >= 40000]
    spike_calls = [c.call_index for c in spike_tool_calls]
    if rules + skills > 15000 or (session.token_breakdown.context_usage_percent or 0) >= 75.0 or spike_calls:
        findings.append(_finding(HEURISTIC_IDS[5], "session_token_or_output_spike", "Context Token Bloat / Unbounded Output", "medium",
            call=spike_tool_calls[0] if spike_tool_calls else None,
            summary="Context or tool output exceeded a configured diagnostic threshold.", evidence={"rules_skills_tokens": rules + skills, "context_usage_percent": session.token_breakdown.context_usage_percent, "spike_call_indexes": spike_calls}, hint="Limit attached context and unbounded tool output."))
    for sub in session.subagents:
        high_error_rate = sub.error_count > 0 and (
            sub.tool_call_count == 0 or (sub.error_count >= 3 and sub.error_count * 4 >= sub.tool_call_count * 3)
        )
        if sub.status != "completed" or sub.denial_count > 0 or high_error_rate:
            norm_sub = normalize_session_id(sub.session_id)
            dispatch = next(
                (call for call in calls if call.subagent_session_id and normalize_session_id(call.subagent_session_id) == norm_sub),
                None,
            )
            fallback_turn = next((turn for turn in session.turns if turn.turn_index == sub.parent_turn_index), None)
            anchor = dispatch
            if anchor is None and fallback_turn:
                anchor = ToolCallIR(0, fallback_turn.turn_index, fallback_turn.user_bubble_id, None, None, "", "", "", None, None, None, {}, "", "", 0, "completed", False, False, None, None)
            findings.append(_finding(HEURISTIC_IDS[6], f"sub:{sub.session_id}", "Subagent Failure", "medium",
                call=anchor,
                summary=f"Subagent {sub.session_id} did not complete cleanly.", evidence={"status": sub.status, "error_count": sub.error_count, "denial_count": sub.denial_count, "tool_call_count": sub.tool_call_count}, hint="Inspect the child session failure before redispatching."))
    for turn in session.turns:
        dispatch_runs: list[list[ToolCallIR]] = []
        current_run: list[ToolCallIR] = []
        for call in turn.tool_calls:
            if call.canonical_kind == "subagent_dispatch":
                if current_run:
                    prev_role, curr_role = _dispatch_role(current_run[-1]), _dispatch_role(call)
                    if prev_role != curr_role and (_is_gate_role(prev_role) or _is_gate_role(curr_role)):
                        dispatch_runs.append(current_run)
                        current_run = [call]
                        continue
                current_run.append(call)
            elif call.canonical_kind == "workflow_control":
                continue
            elif current_run:
                dispatch_runs.append(current_run)
                current_run = []
        if current_run:
            dispatch_runs.append(current_run)
        for dispatches in dispatch_runs:
            ids = {call.model_call_id.strip() for call in dispatches if call.model_call_id and call.model_call_id.strip()}
            parallel_batch = len(ids) == 1 and all(call.model_call_id and call.model_call_id.strip() for call in dispatches)
            if len(dispatches) >= 3 and not parallel_batch:
                timestamps = [(c.started_at_ms, c.completed_at_ms) for c in dispatches]
                if all(start is not None and end is not None for start, end in timestamps):
                    sorted_dispatches = sorted(dispatches, key=lambda c: (c.started_at_ms, c.call_index))
                    wave_count, running_end = 1, sorted_dispatches[0].completed_at_ms
                    for dispatch in sorted_dispatches[1:]:
                        if dispatch.started_at_ms >= running_end:
                            wave_count += 1
                            running_end = dispatch.completed_at_ms
                        else:
                            running_end = max(running_end, dispatch.completed_at_ms)
                    sequential = wave_count >= 2
                else:
                    positions = [turn.tool_calls.index(c) for c in dispatches]
                    sequential = len(ids) >= 2 or any(any(x.canonical_kind != "subagent_dispatch" for x in turn.tool_calls[a + 1:b]) for a, b in zip(positions, positions[1:]))
                    wave_count = None
                if sequential:
                    findings.append(_finding(HEURISTIC_IDS[6], f"turn:{turn.turn_index}", "Sequential Subagent Fanout", "medium",
                        call=dispatches[0],
                        summary=f"{len(dispatches)} subagents were dispatched in sequential waves.", evidence={"dispatch_count": len(dispatches), "wave_count": wave_count}, hint="Dispatch independent subagents in one parallel wave."))
                    break
    for turn in session.turns:
        severity = "medium" if any(c.is_error or c.is_denied for c in turn.tool_calls) else "info"
        for index, thinking in enumerate(turn.thinking_blocks):
            match = _FRICTION.search(_unquoted_thinking(thinking.text))
            if match:
                matched_text = thinking.text[match.start() : match.end()]
                if len(thinking.text) <= 160 or match.end() <= 160:
                    snippet = thinking.text[:160]
                else:
                    center = (match.start() + min(match.end(), match.start() + 160)) // 2
                    start = min(max(0, center - 80), len(thinking.text) - 160)
                    snippet = thinking.text[start : start + 160]
                findings.append(_finding(HEURISTIC_IDS[7], f"{thinking.bubble_id}:{index}", "Instruction Friction Signal", severity,
                    turn_index=turn.turn_index, bubble_id=thinking.bubble_id,
                    summary="Thinking text signals instruction, skill, or harness friction.", evidence={"match": matched_text}, hint="Inspect active instructions and harness constraints.", thinking=snippet))
    unique = {finding.finding_key: finding for finding in findings}
    return sorted(unique.values(), key=lambda f: (SEVERITY_RANK[f.severity], f.turn_index or 0, f.id, f.finding_key))
