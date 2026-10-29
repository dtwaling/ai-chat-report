"""Cursor adapter for extracting SessionIR from read-only Cursor stores."""

from __future__ import annotations

from datetime import datetime, timezone
import re
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from chat_report.adapters.cursor.hooks import load_denial_markers
from chat_report.adapters.cursor.paths import (
    detect_cursor_version,
    resolve_state_db,
    resolve_tracking_db_with_provenance,
)
from chat_report.adapters.cursor.resolver import (
    list_recent_sessions,
    resolve_chat_id,
)
from chat_report.adapters.cursor.store import (
    ConversationBlobs,
    bubble_timestamps,
    extract_thinking,
    extract_token_breakdown,
    extract_token_counts,
    fetch_attribution,
    fetch_bubbles,
    fetch_composer_data,
    fetch_conversation_blobs,
    fetch_ordered_headers,
    has_non_narration_thinking_in_bubble,
    open_ro,
    order_bubbles,
)
from chat_report.adapters.cursor.tool_map import map_tool_call
from chat_report.core.adapter import (
    ExtractOptions,
    SessionListEntry,
    SessionNotFoundError,
    StoreAccessError,
    register_adapter,
)
from chat_report.core.ir import (
    CustomizationRefIR,
    ResolvedTarget,
    SCHEMA_VERSION,
    SessionIR,
    SubagentSummaryIR,
    TurnIR,
    normalize_session_id,
)


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _clean_subagent_type(value: Any) -> str | None:
    text = _text(value).strip()
    if not text or text.lower() in ("unspecified", "unknown", "custom"):
        return None
    return text


def _normalize_session_status(raw_status: Any, turns: list[TurnIR], dispatch_status: str | None = None) -> str:
    """Normalize Cursor's inconsistent completed-session status values."""
    status = _text(raw_status).strip().lower()
    dispatch = (dispatch_status or "").strip().lower()
    if dispatch in ("aborted", "cancelled", "canceled"):
        return "aborted"
    if status in ("error", "errored", "failed") or dispatch in ("error", "errored", "failed"):
        return "errored"
    if dispatch in ("completed", "success"):
        return "completed"
    if status in ("aborted", "cancelled", "canceled"):
        return "aborted"
    if status in ("running", "loading", "in_progress"):
        return "running"
    if status in ("completed", "success"):
        return "completed"
    if status in ("none", "", "unknown") and turns:
        return "completed"
    return status or "completed"


def _item_path(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        for key in ("path", "relativeWorkspacePath", "uri", "filename", "cursorRuleFilename"):
            path = value.get(key)
            if isinstance(path, str) and path:
                return path
    return None


def _paths(values: Any) -> list[str]:
    return [path for value in values if (path := _item_path(value))] if isinstance(values, list) else []


def _append_unique(target: list[str], values: list[str]) -> None:
    target.extend(value for value in values if value not in target)


def _customization_kind(path: str) -> str | None:
    normalized = path.replace("\\", "/").lower()
    name = normalized.rsplit("/", 1)[-1]
    if name == "skill.md":
        return "skill"
    if name in ("agents.md", "claude.md"):
        return "agent_doc"
    if normalized.endswith((".mdc", ".cursorrules")) or "/.cursor/rules/" in f"/{normalized}":
        return "rule"
    if name == "hooks.json" or "/.cursor/hooks/" in f"/{normalized}":
        return "hook"
    return None


def _reasoning_effort(data: dict[str, Any], has_non_narration_thinking: bool = False) -> str | None:
    config = data.get("modelConfig") if isinstance(data.get("modelConfig"), dict) else {}
    selected_models = config.get("selectedModels")
    if isinstance(selected_models, list):
        for model in selected_models:
            if not isinstance(model, dict):
                continue
            parameters = model.get("parameters")
            if isinstance(parameters, list):
                for parameter in parameters:
                    if isinstance(parameter, dict) and parameter.get("id") in ("reasoning_effort", "effort", "thinking_effort"):
                        value = parameter.get("value")
                        if isinstance(value, str) and value:
                            return value
    for candidate in (
        config.get("reasoningEffort"),
        config.get("effort"),
        data.get("reasoningEffort"),
        data.get("effort"),
    ):
        if isinstance(candidate, str) and candidate:
            return candidate
    model_str = _text(config.get("modelName")) or _text(data.get("modelName"))
    if model_str:
        m = re.search(r"\beffort=(minimal|low|medium|high|max)\b", model_str, re.I)
        if m:
            return m.group(1).lower()
        m = re.search(r"-thinking-(minimal|low|medium|high|max)\b", model_str, re.I)
        if m:
            return m.group(1).lower()
    if has_non_narration_thinking:
        return "enabled"
    return None


def _extract_prompt_rule_refs(texts: Iterable[str], turn_index: int = 1) -> list[CustomizationRefIR]:
    refs: list[CustomizationRefIR] = []
    for text in texts:
        if not isinstance(text, str) or not text:
            continue
        for match in re.finditer(r"""<always_applied_workspace_rule\s+name=["']([^"']+)["']>""", text, re.I):
            path = match.group(1)
            kind = _customization_kind(path)
            if kind in ("rule", "agent_doc"):
                refs.append(CustomizationRefIR(kind=kind or "rule", path=path, source="context_attached", turn_index=turn_index))
    return refs


def _context_refs(bubble: dict[str, Any], turn_index: int) -> list[CustomizationRefIR]:
    refs: list[CustomizationRefIR] = []
    context = bubble.get("context")
    if isinstance(context, dict):
        for key in ("cursorRules",):
            values = context.get(key)
            for path in _paths(values):
                refs.append(CustomizationRefIR(_customization_kind(path) or "rule", path, "context_attached", turn_index))
        for key in ("mentions", "fileSelections"):
            for path in _paths(context.get(key)):
                kind = _customization_kind(path)
                if kind is not None:
                    refs.append(CustomizationRefIR(kind, path, "context_attached", turn_index))
    for path in _paths(bubble.get("cursorRules")):
        refs.append(CustomizationRefIR(_customization_kind(path) or "rule", path, "context_attached", turn_index))
    chunks = bubble.get("attachedCodeChunks")
    if isinstance(chunks, list):
        for path in _paths(chunks):
            kind = _customization_kind(path)
            if kind is not None:
                refs.append(CustomizationRefIR(kind, path, "context_attached", turn_index))
    request_context = bubble.get("messageRequestContext")
    if isinstance(request_context, dict):
        for path in _paths(request_context.get("rules")):
            refs.append(CustomizationRefIR(_customization_kind(path) or "rule", path, "context_attached", turn_index))
        for path in _paths(request_context.get("files")):
            kind = _customization_kind(path)
            if kind is not None:
                refs.append(CustomizationRefIR(kind, path, "context_attached", turn_index))
    text = _text(bubble.get("text"))
    if bubble.get("type") == 1:
        refs.extend(_extract_prompt_rule_refs([text], turn_index=turn_index))
    in_manual = False
    for line in text.splitlines():
        lower = line.lower()
        if "<manually_attached_skills" in lower:
            in_manual = True
            continue
        if "</manually_attached_skills" in lower:
            in_manual = False
            continue
        if in_manual:
            for match in re.finditer(r"""fullpath\s*=\s*["']([^"']+)["']""", line, re.IGNORECASE):
                refs.append(CustomizationRefIR("skill", match.group(1), "context_attached", turn_index))
            path_match = re.match(r"^\s*path\s*:\s*(.+?)\s*$", line, re.IGNORECASE)
            if path_match and _customization_kind(path_match.group(1)) == "skill":
                refs.append(CustomizationRefIR("skill", path_match.group(1), "context_attached", turn_index))
    return refs


def _attached_files(bubble: dict[str, Any]) -> list[str]:
    context = bubble.get("context")
    files: list[str] = []
    if isinstance(context, dict):
        _append_unique(files, _paths(context.get("fileSelections")))
        _append_unique(files, _paths(context.get("mentions")))
    _append_unique(files, _paths(bubble.get("attachedCodeChunks")))
    request_context = bubble.get("messageRequestContext")
    if isinstance(request_context, dict):
        _append_unique(files, _paths(request_context.get("files")))
    return files


class CursorAdapter:
    """Extract Cursor chat sessions without importing core.classifier."""

    harness_name = "cursor"

    def list_recent(self, limit: int, options: ExtractOptions) -> list[SessionListEntry]:
        con = open_ro(resolve_state_db(options.state_db))
        try:
            return list_recent_sessions(con, limit, options.list_include_subagents)
        finally:
            con.close()

    def resolve_id(self, raw_id: str, options: ExtractOptions) -> ResolvedTarget:
        state_con = open_ro(resolve_state_db(options.state_db))
        track_con = None
        try:
            tracking_path, explicit = resolve_tracking_db_with_provenance(options.tracking_db)
            try:
                track_con = open_ro(tracking_path)
            except StoreAccessError:
                if explicit:
                    raise
            return resolve_chat_id(raw_id, state_con, track_con, force=options.force_id_kind)
        finally:
            if track_con is not None:
                track_con.close()
            state_con.close()

    def extract_session(
        self, session_id: str, options: ExtractOptions, *, resolved_from: ResolvedTarget | None = None
    ) -> SessionIR:
        return self._extract(session_id, options, resolved_from, set())

    def _extract(
        self, session_id: str, options: ExtractOptions, resolved_from: ResolvedTarget | None,
        visited_ids: set[str],
    ) -> SessionIR:
        normalized = normalize_session_id(session_id)
        if normalized in visited_ids:
            raise ValueError(f"Cycle detected while extracting Cursor subagent '{session_id}'.")
        visited_ids.add(normalized)
        state_con = open_ro(resolve_state_db(options.state_db))
        track_con = None
        warnings: list[dict[str, str]] = []
        captured_at_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        try:
            data = fetch_composer_data(state_con, session_id)
            if not data:
                raise SessionNotFoundError(f"Cursor chat '{session_id}' was not found.")
            headers = fetch_ordered_headers(state_con, session_id, data)
            ordered, orphan_count, order_warnings = order_bubbles(
                headers, fetch_bubbles(state_con, session_id), options.include_orphans
            )
            warnings.extend(order_warnings)
            conv_blobs = fetch_conversation_blobs(state_con, data)
            turns: list[TurnIR] = []
            pending: list[tuple[str, dict[str, Any]]] = []
            turn_times: dict[int, list[tuple[int | None, int | None]]] = {}
            turn_bubbles: dict[int, list[tuple[str, dict[str, Any]]]] = {}
            current: TurnIR | None = None
            call_index = 1
            markers = load_denial_markers(options.workspace, options.extra_denial_markers, options.verbose)
            for bubble_id, bubble in ordered:
                bubble_type = bubble.get("type")
                if bubble_type == 1:
                    started_at_ms, completed_at_ms = bubble_timestamps(bubble)
                    current = TurnIR(len(turns) + 1, bubble_id, _text(bubble.get("requestId")) or None, _text(bubble.get("text")),
                        None, started_at_ms, completed_at_ms, None, False, None, None)
                    turns.append(current)
                    turn_times[current.turn_index] = []
                    turn_bubbles[current.turn_index] = []
                    for pending_id, pending_bubble in pending:
                        self._append_bubble(current, pending_id, pending_bubble, markers, call_index, conv_blobs)
                        turn_times[current.turn_index].append(bubble_timestamps(pending_bubble))
                        turn_bubbles[current.turn_index].append((pending_id, pending_bubble))
                        call_index += 1 if isinstance(pending_bubble.get("toolFormerData"), dict) else 0
                    pending.clear()
                    self._append_bubble(current, bubble_id, bubble, markers, call_index, conv_blobs)
                    turn_times[current.turn_index].append((started_at_ms, completed_at_ms))
                    turn_bubbles[current.turn_index].append((bubble_id, bubble))
                    call_index += 1 if isinstance(bubble.get("toolFormerData"), dict) else 0
                    continue
                if current is None:
                    pending.append((bubble_id, bubble))
                    continue
                self._append_bubble(current, bubble_id, bubble, markers, call_index, conv_blobs)
                turn_times[current.turn_index].append(bubble_timestamps(bubble))
                turn_bubbles[current.turn_index].append((bubble_id, bubble))
                call_index += 1 if isinstance(bubble.get("toolFormerData"), dict) else 0
            if not turns:
                current = TurnIR(1, "__synthetic_turn_1__", None, "", None, None, None, None, False, None, None)
                turns.append(current)
                turn_times[current.turn_index] = []
                turn_bubbles[current.turn_index] = []
                warnings.append({"code": "cursor-no-user-bubbles-synthetic-turn", "message": "No user bubbles were found; created synthetic Turn 1."})
                for bubble_id, bubble in pending:
                    self._append_bubble(current, bubble_id, bubble, markers, call_index, conv_blobs)
                    turn_times[current.turn_index].append(bubble_timestamps(bubble))
                    turn_bubbles[current.turn_index].append((bubble_id, bubble))
                    call_index += 1 if isinstance(bubble.get("toolFormerData"), dict) else 0
            latest_gen_uuid = _text(data.get("latestChatGenerationUUID")).strip() or None
            if (
                latest_gen_uuid
                and turns
                and turns[-1].request_id is None
                and latest_gen_uuid not in {t.request_id for t in turns if t.request_id}
            ):
                turns[-1].request_id = latest_gen_uuid
            for turn in turns:
                bubbles = turn_bubbles.get(turn.turn_index, [])
                timestamps = turn_times.get(turn.turn_index, [])
                starts = [started for started, _ in timestamps if isinstance(started, int)]
                ends = [completed for _, completed in timestamps if isinstance(completed, int)]
                if starts:
                    turn.started_at_ms = min(starts)
                if ends:
                    turn.completed_at_ms = max(ends)
                if turn.started_at_ms is not None and turn.completed_at_ms is not None and turn.completed_at_ms >= turn.started_at_ms:
                    turn.duration_ms = turn.completed_at_ms - turn.started_at_ms
                if not starts and not ends:
                    turn.timestamps_synthesized = True
                input_tokens: list[int] = []
                output_tokens: list[int] = []
                model_name: str | None = None
                attached_rules: list[str] = []
                attached_files: list[str] = []
                for _, bubble in bubbles:
                    input_count, output_count = extract_token_counts(bubble)
                    if input_count is not None:
                        input_tokens.append(input_count)
                    if output_count is not None:
                        output_tokens.append(output_count)
                    model_info = bubble.get("modelInfo")
                    if model_name is None and isinstance(model_info, dict):
                        model_name = _text(model_info.get("modelName")) or None
                    refs = _context_refs(bubble, turn.turn_index)
                    _append_unique(attached_rules, [ref.path for ref in refs if ref.kind in ("rule", "agent_doc")])
                    _append_unique(attached_files, _attached_files(bubble))
                turn.input_tokens = sum(input_tokens) if input_tokens else None
                turn.output_tokens = sum(output_tokens) if output_tokens else None
                turn.model_name = model_name or (_text((data.get("modelConfig") or {}).get("modelName")) if isinstance(data.get("modelConfig"), dict) else None)
                turn.attached_rules = attached_rules
                turn.attached_files = attached_files
            if any(turn.timestamps_synthesized for turn in turns):
                warnings.append({"code": "cursor-per-turn-timestamps-synthesized", "message": "Bubble timestamps were absent; turn timestamps were synthesized."})
            in_flight = (
                bool(data.get("isContinuationInProgress"))
                or bool(data.get("generatingBubbleIds"))
                or str(data.get("status") or "").strip().lower() in ("running", "loading", "in_progress")
                or any(call.status == "loading" for turn in turns for call in turn.tool_calls)
            )
            turn_starts = [t.started_at_ms for t in turns if isinstance(t.started_at_ms, int) and not t.timestamps_synthesized]
            turn_ends = [t.completed_at_ms for t in turns if isinstance(t.completed_at_ms, int) and not t.timestamps_synthesized]
            composer_start = _int(data.get("createdAt"))
            composer_end = max(
                [x for x in (_int(data.get("lastUpdatedAt")), _int(data.get("conversationCheckpointLastUpdatedAt"))) if x is not None],
                default=None,
            )
            start = min(turn_starts) if turn_starts else composer_start
            end = max(turn_ends) if turn_ends else composer_end
            if in_flight:
                session_ended_at_ms = None
                session_duration_ms = None
                session_status = None
                if turns:
                    turns[-1].input_tokens = None
                    turns[-1].output_tokens = None
                for turn in turns:
                    if any(call.status == "loading" for call in turn.tool_calls):
                        turn.input_tokens = None
                        turn.output_tokens = None
                warnings.append({
                    "code": "cursor-session-in-flight",
                    "message": "Session was active at capture; session status and duration_ms are set to null.",
                })
            else:
                session_ended_at_ms = end
                session_duration_ms = (end - start) if isinstance(start, int) and isinstance(end, int) and end >= start else None
                session_status = _normalize_session_status(data.get("status"), turns)
            has_non_narration_thinking = any(
                has_non_narration_thinking_in_bubble(bubble_dict, conv_blobs.narration_texts)
                for _, bubble_dict in ordered
            )
            subagent_info = data.get("subagentInfo") if isinstance(data.get("subagentInfo"), dict) else {}
            parent_session_id = _text(data.get("parentComposerId")) or _text(subagent_info.get("parentComposerId")) or None
            is_subagent = bool(data.get("isSubagent")) or bool(parent_session_id) or bool(subagent_info)
            subagent_type = _clean_subagent_type(data.get("subagentTypeName")) or _clean_subagent_type(subagent_info.get("subagentTypeName"))
            session = SessionIR(SCHEMA_VERSION, self.harness_name, detect_cursor_version(state_con, data), session_id, normalized,
                parent_session_id, is_subagent, subagent_type,
                _text(data.get("name")) or None, _text(data.get("unifiedMode")) or None, session_status,
                _text((data.get("modelConfig") or {}).get("modelName")) if isinstance(data.get("modelConfig"), dict) else None,
                bool((data.get("modelConfig") or {}).get("maxMode")) if isinstance(data.get("modelConfig"), dict) and "maxMode" in data.get("modelConfig", {}) else None,
                _reasoning_effort(data, has_non_narration_thinking=has_non_narration_thinking),
                start, session_ended_at_ms, session_duration_ms,
                _int(data.get("totalLinesAdded")) or 0, _int(data.get("totalLinesRemoved")) or 0,
                data.get("addedFiles") if isinstance(data.get("addedFiles"), list) else [],
                data.get("removedFiles") if isinstance(data.get("removedFiles"), list) else [],
                _int(data.get("stopHookLoopCount")) or 0, orphan_count, extract_token_breakdown(data), turns=turns,
                warnings=warnings, resolved_from=resolved_from.to_dict() if resolved_from else None,
                in_flight=in_flight, captured_at_ms=captured_at_ms)
            for turn in turns:
                if turn.timestamps_synthesized:
                    turn.started_at_ms = start
                    turn.completed_at_ms = end
                    if isinstance(start, int) and isinstance(end, int) and end >= start:
                        turn.duration_ms = end - start
                for _, bubble in turn_bubbles.get(turn.turn_index, []):
                    for ref in _context_refs(bubble, turn.turn_index):
                        if ref not in session.customizations:
                            session.customizations.append(ref)
            composer_ctx = data.get("context") if isinstance(data.get("context"), dict) else {}
            composer_ctx_rules = _paths(composer_ctx.get("cursorRules"))
            session_rule_refs = [
                CustomizationRefIR(_customization_kind(path) or "rule", path, "context_attached", 1)
                for path in composer_ctx_rules
            ] + _extract_prompt_rule_refs(conv_blobs.prompt_texts, turn_index=1)
            for ref in session_rule_refs:
                if ref not in session.customizations:
                    session.customizations.append(ref)
                if turns and ref.kind in ("rule", "agent_doc") and ref.path not in turns[0].attached_rules:
                    turns[0].attached_rules.append(ref.path)
            event_timestamps: list[int] = []
            for turn in turns:
                if turn.timestamps_synthesized:
                    continue
                for ts in (turn.started_at_ms, turn.completed_at_ms):
                    if isinstance(ts, int) and not isinstance(ts, bool):
                        event_timestamps.append(ts)
                for call in turn.tool_calls:
                    for ts in (call.started_at_ms, call.completed_at_ms):
                        if isinstance(ts, int) and not isinstance(ts, bool):
                            event_timestamps.append(ts)
                for block in turn.thinking_blocks:
                    for ts in (block.started_at_ms, block.completed_at_ms):
                        if isinstance(ts, int) and not isinstance(ts, bool):
                            event_timestamps.append(ts)
            if event_timestamps and max(event_timestamps) > captured_at_ms:
                skew_ms = max(event_timestamps) - captured_at_ms
                warnings.append({
                    "code": "cursor-event-clock-ahead-of-capture",
                    "message": f"Bubble/tool timestamps exceed capture time by up to {skew_ms} ms; treat timestamps as relative intervals.",
                })
            tracking_path, explicit_tracking = resolve_tracking_db_with_provenance(options.tracking_db)
            try:
                track_con = open_ro(tracking_path)
                win_start = min([x for x in (composer_start, start) if isinstance(x, int)], default=None)
                if in_flight:
                    win_end = max([x for x in (composer_end, end, captured_at_ms) if isinstance(x, int)], default=None)
                else:
                    win_end = max([x for x in (composer_end, end) if isinstance(x, int)], default=None)
                window = (win_start, win_end) if isinstance(win_start, int) and isinstance(win_end, int) and win_end >= win_start else None
                session.attribution = fetch_attribution(track_con, session_id, window, warnings)
            except (StoreAccessError, sqlite3.Error) as exc:
                if explicit_tracking:
                    if isinstance(exc, StoreAccessError):
                        raise
                    raise StoreAccessError(f"Cannot read Cursor tracking database: {tracking_path}") from exc
                session.attribution = None
                warnings.append({"code": "cursor-tracking-db-unavailable", "message": "Default ai-code-tracking.db is unavailable."})
            if options.include_subagents:
                self._attach_subagents(session, data, options, visited_ids)
            return session
        finally:
            if track_con is not None:
                track_con.close()
            state_con.close()
            visited_ids.remove(normalized)

    @staticmethod
    def _append_bubble(
        turn: TurnIR,
        bubble_id: str,
        bubble: dict[str, Any],
        markers: tuple[str, ...],
        call_index: int,
        conv_blobs: ConversationBlobs | None = None,
    ) -> None:
        narration_texts = conv_blobs.narration_texts if conv_blobs else None
        turn.thinking_blocks.extend(extract_thinking(bubble_id, bubble, narration_texts=narration_texts))
        if bubble.get("type") == 2 and _text(bubble.get("text")):
            turn.assistant_texts.append(_text(bubble.get("text")))
        tfd = bubble.get("toolFormerData")
        if isinstance(tfd, dict):
            started_ms, completed_ms = bubble_timestamps(bubble)
            has_real_span = _int(bubble.get("startedAtMs")) is not None and _int(bubble.get("completedAtMs")) is not None
            tool_blob = conv_blobs.tool_results.get(_text(tfd.get("toolCallId"))) if conv_blobs else None
            call = map_tool_call(tfd, call_index=call_index, turn_index=turn.turn_index, bubble_id=bubble_id,
                started_at_ms=started_ms, completed_at_ms=completed_ms if has_real_span else None, denial_markers=markers,
                tool_blob=tool_blob)
            if not has_real_span:
                call.completed_at_ms = completed_ms
            if call.canonical_kind == "subagent_dispatch":
                additional = tfd.get("additionalData")
                if isinstance(additional, dict):
                    status = _text(additional.get("status")) or _text(additional.get("terminationReason"))
                    if status:
                        call.input_params["_dispatch_status"] = status
            turn.tool_calls.append(call)

    def _attach_subagents(self, session: SessionIR, data: dict[str, Any], options: ExtractOptions, visited_ids: set[str]) -> None:
        dispatch: dict[str, int] = {}
        for turn in session.turns:
            for call in turn.tool_calls:
                if call.subagent_session_id:
                    norm_call_sub = normalize_session_id(call.subagent_session_id)
                    if norm_call_sub not in dispatch:
                        dispatch[norm_call_sub] = turn.turn_index
        ordered_ids: list[str] = []
        seen_norm_ids: set[str] = set()
        raw_sub_ids = list(data.get("subagentComposerIds") or []) + [
            call.subagent_session_id
            for turn in session.turns
            for call in turn.tool_calls
            if call.subagent_session_id
        ]
        for sub_id in raw_sub_ids:
            if isinstance(sub_id, str) and sub_id:
                norm_sub = normalize_session_id(sub_id)
                if norm_sub not in seen_norm_ids and norm_sub not in visited_ids:
                    seen_norm_ids.add(norm_sub)
                    ordered_ids.append(sub_id)
        child_options = replace(options, include_subagents=False)
        for sub_id in ordered_ids:
            norm_sub = normalize_session_id(sub_id)
            try:
                child = self._extract(sub_id, child_options, None, visited_ids)
            except (StoreAccessError, LookupError):
                session.warnings.append({
                    "code": "cursor-subagent-extract-failed",
                    "message": f"Failed to extract child subagent '{sub_id}'.",
                })
                continue
            reads = [call.target_path for turn in child.turns for call in turn.tool_calls if call.canonical_kind == "file_read" and not call.is_error and call.target_path]
            dispatch_calls = [
                call
                for turn in session.turns
                for call in turn.tool_calls
                if call.subagent_session_id and normalize_session_id(call.subagent_session_id) == norm_sub
            ]
            dispatch_call = dispatch_calls[-1] if dispatch_calls else None
            dispatch_status = _text(dispatch_call.input_params.get("_dispatch_status")) or None if dispatch_call else None
            resolved_sub_type = child.subagent_type
            if not resolved_sub_type:
                for call in dispatch_calls:
                    for key in ("subagent_type", "name", "subagentType"):
                        candidate = _clean_subagent_type(call.input_params.get(key))
                        if candidate:
                            resolved_sub_type = candidate
                            break
                    if resolved_sub_type:
                        break
            sub_status = None if child.in_flight else _normalize_session_status(child.status, child.turns, dispatch_status)
            sub_duration = None if child.in_flight else child.duration_ms
            session.subagents.append(SubagentSummaryIR(sub_id, dispatch.get(norm_sub), resolved_sub_type, child.title, child.model_name,
                sub_status, sub_duration, len(child.turns), sum(len(t.tool_calls) for t in child.turns),
                sum(1 for t in child.turns for c in t.tool_calls if c.is_error), sum(1 for t in child.turns for c in t.tool_calls if c.is_denied),
                [path for path in reads if _customization_kind(path) == "skill"],
                [path for path in reads if _customization_kind(path) in ("rule", "agent_doc")],
                in_flight=child.in_flight))


register_adapter("cursor", CursorAdapter)
