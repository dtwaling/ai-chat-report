#!/usr/bin/env python3
"""Chat-report — extract Optimus-test-relevant signals from a Cursor chat.

Given one or more Cursor chat/request IDs (UUIDs), produce a markdown report
and a JSON dump per chat that captures the full tool-call trajectory, thinking
blocks, guard/denial events, errors, and attribution data. Designed for
iterative Optimus evaluation: run a test chat, grab the Request ID from the
Cursor UI (right-click → "Copy Request ID"), run this script, read the report.

Modes:
  Single-chat (default) — per-chat report (one per ID)
  --diff <idA> <idB>    — before/after comparison between two chats
  --aggregate <id> ...  — rollup across N chats

ID resolution: the CLI accepts any UUID — Request IDs (from Cursor UI) are
resolved to chat IDs automatically via the tracking DB, with bubble-scan and
direct chat-ID fallbacks. Use --request-id / --chat-id to force resolution.

Data sources (both opened read-only):
  1. %APPDATA%\\Cursor\\User\\globalStorage\\state.vscdb
       - cursorDiskKV.composerData:<chatId>  → chat metadata
       - cursorDiskKV.bubbleId:<chatId>:<bubbleId>  → per-turn data
  2. %USERPROFILE%\\.cursor\\ai-tracking\\ai-code-tracking.db
       - ai_code_hashes WHERE conversationId = <chatId>
       - ai_deleted_files WHERE conversationId = <chatId>
       - scored_commits (joined by time window)

Outputs (default: .cursor/local/chat-reports/):
  <chatId>.md / .json              — single-chat report
  diff-<idA>-vs-<idB>.md / .json   — diff report
  aggregate-<ISO-ts>.md / .json    — aggregate report
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Paths and config
# ---------------------------------------------------------------------------

def _default_state_db() -> Path:
    return Path(os.environ.get(
        "CURSOR_STATE_DB",
        os.path.expandvars(r"%APPDATA%\Cursor\User\globalStorage\state.vscdb"),
    ))


def _default_tracking_db() -> Path:
    return Path(os.environ.get(
        "CURSOR_TRACKING_DB",
        os.path.expandvars(r"%USERPROFILE%\.cursor\ai-tracking\ai-code-tracking.db"),
    ))


DEFAULT_OUT_DIR = Path(".cursor/local/chat-reports")

BUBBLE_TYPE_NAMES = {1: "user", 2: "assistant"}

_DURATION_WARN_THRESHOLD_S = 600   # absolute minimum before flagging (10 min)
_DURATION_PER_BUBBLE_S = 30        # expected ceiling per bubble for a healthy chat

_FALLBACK_DENIAL_MARKERS = (
    "Blocked by preToolUse hook",
    '"permissionDecision":"deny"',
    '"permissionDecision": "deny"',
    "This file is blocked from direct Read",
    "This workspace routes search through Optimus MCP",
    "This workspace routes all search through Optimus MCP",
    "Do not use SemanticSearch",
    "Do not use the Grep tool",
    "Do not run",
)

# Tier 2: context-required patterns for short/ambiguous markers that must
# co-occur with denial framing language to count as a denial signal.
_CONTEXT_DENIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:blocked|denied|redirected|use instead|do not (?:use|run))"
        r".{0,80}optimus_(?:search|grep|resolve|prune|extract)", re.I | re.S),
    re.compile(
        r"optimus_(?:search|grep|resolve|prune|extract)"
        r".{0,80}(?:blocked|denied|redirected|instead)", re.I | re.S),
    re.compile(
        r"(?:optimus-read-guard|optimus-shell-guard)"
        r".{0,80}(?:deny|denied|block)", re.I | re.S),
    re.compile(
        r"(?:preToolUse hook|guard hook).{0,80}(?:deny|denied|block)", re.I),
    re.compile(
        r"[Uu]se optimus_\w+.{0,80}(?:instead|blocked|do not)", re.S),
)

# Active denial markers — populated at startup by _load_denial_markers()
HOOK_DENIAL_MARKERS: tuple[str, ...] = _FALLBACK_DENIAL_MARKERS

# Minimum length for a marker to be trusted as a bare substring match.
# Shorter auto-extracted markers are deferred to Tier 2 context matching.
_BARE_MARKER_MIN_LEN = 30


# ---------------------------------------------------------------------------
# Dynamic hook-denial detection
# ---------------------------------------------------------------------------

def _find_workspace_root(start: Path | None = None) -> Path:
    """Walk up from *start* looking for .cursor/hooks.json; fall back to CWD."""
    p = (start or Path.cwd()).resolve()
    for d in [p] + list(p.parents):
        if (d / ".cursor" / "hooks.json").exists():
            return d
    return Path.cwd().resolve()


def _extract_markers_from_hook_script(path: Path) -> list[tuple[str, str]]:
    """Extract string literals near deny decisions from a hook .mjs script.

    Returns list of (marker_text, source_annotation) tuples.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []

    lines = text.splitlines()
    markers: list[tuple[str, str]] = []
    source = f"auto:{path.name}"

    deny_line_indices: set[int] = set()
    for i, line in enumerate(lines):
        if re.search(r'\bdeny\b|"permissionDecision"\s*:\s*"deny"', line):
            deny_line_indices.add(i)

    string_pattern = re.compile(r'''(?:"([^"\\]*(?:\\.[^"\\]*)*)"|'([^'\\]*(?:\\.[^'\\]*)*)')''')
    seen: set[str] = set()

    for i, line in enumerate(lines):
        near_deny = any(abs(i - d) <= 5 for d in deny_line_indices)
        in_message_field = bool(re.search(r'\b(?:reason|message|agent_message|msg)\b', line, re.IGNORECASE))
        if not near_deny and not in_message_field:
            continue
        for m in string_pattern.finditer(line):
            val = m.group(1) if m.group(1) is not None else m.group(2)
            if val is None:
                continue
            val = val.replace("\\n", " ").replace('\\"', '"').replace("\\'", "'").strip()
            if len(val) < 8 or not val.strip() or "${" in val:
                continue
            if val.startswith(("//", "/*", "node:", "node_modules")):
                continue
            if val not in seen:
                seen.add(val)
                markers.append((val, source))
    return markers

def _pop_markers(ws_root: Path, hooks_json: Path, verbose: bool = False) -> list[tuple[str, str]]:
    extracted: list[tuple[str, str]] = []
    text= hooks_json.read_text(encoding="utf-8")
    cleaned = re.sub(r'//[^\n]*', '', text)
    cfg = json.loads(cleaned)
    hooks = cfg.get("hooks", {})
    pre_hooks = hooks.get("preToolUse", [])
    for hook in pre_hooks:
        if not isinstance(hook, dict):
            continue
        cmd = hook.get("command", "")
        parts = cmd.split()
        script_path = None
        for part in parts:
            if part.endswith(".mjs") or part.endswith(".js"):
                script_path = ws_root / part
                break
        if script_path and script_path.exists():
            extracted.extend(_extract_markers_from_hook_script(script_path))

    return extracted

def _cleaned_markers(all_markers: list[tuple[str, str]], auto_extracted: bool, verbose: bool = False) -> tuple[str, ...]:
    seen: set[str] = set()
    deduped: list[str] = []
    sources: dict[str, str] = {}
    skipped_short: list[tuple[str, str]] = []
    for text, src in all_markers:
        if text in seen:
            continue
        seen.add(text)
        # Auto-extracted markers shorter than the threshold are handled by
        # Tier 2 context patterns — don't add them as bare-match markers.
        if src.startswith("auto:") and len(text) < _BARE_MARKER_MIN_LEN:
            skipped_short.append((text, src))
            continue
        deduped.append(text)
        sources[text] = src

    if verbose:
        print(f"  [denial-markers] {len(deduped)} active markers (Tier 1 bare-match):", file=sys.stderr)
        for text in deduped:
            print(f"    [{sources[text]}] {text!r}", file=sys.stderr)
        if skipped_short:
            print(f"  [denial-markers] {len(skipped_short)} short auto-extracted markers "
                  f"deferred to Tier 2 context patterns:", file=sys.stderr)
            for text, src in skipped_short:
                print(f"    [{src}] {text!r}", file=sys.stderr)
        print(f"  [denial-markers] {len(_CONTEXT_DENIAL_PATTERNS)} Tier 2 context patterns active", file=sys.stderr)
        if not auto_extracted:
            print(f"  [denial-markers] note: no markers auto-extracted from hook scripts", file=sys.stderr)

    return tuple(deduped)

def _load_denial_markers(workspace: Path | None = None,
                         extra_markers: list[str] | None = None,
                         verbose: bool = False) -> tuple[str, ...]:
    """Build the active denial-marker set from hook scripts + fallback + CLI."""
    ws_root = _find_workspace_root(workspace)
    hooks_json = ws_root / ".cursor" / "hooks.json"
    all_markers: list[tuple[str, str]] = []
    auto_extracted = False

    try:
        if hooks_json.exists():
            all_markers.extend(_pop_markers(ws_root, hooks_json, verbose))
            if all_markers:
                auto_extracted = True
    except (json.JSONDecodeError, OSError) as e:
        if verbose:
            print(f"  [denial-markers] warning: failed to parse {hooks_json}: {e}", file=sys.stderr)

    for m in _FALLBACK_DENIAL_MARKERS:
        all_markers.append((m, "fallback"))

    if extra_markers:
        for m in extra_markers:
            all_markers.append((m, "cli"))

    return _cleaned_markers(all_markers, auto_extracted, verbose) #tuple(deduped)


# ---------------------------------------------------------------------------
# ID resolution
# ---------------------------------------------------------------------------

_resolve_cache: dict[str, tuple[str, str]] = {}


def _lookup_request_id(
    track_con: sqlite3.Connection, uuid_val: str,
) -> str | None:
    """Resolve uuid_val as a request ID via ai_code_hashes.

    Returns the conversationId on hit or None on miss. Propagates
    `sqlite3.Error` -- the caller decides whether to swallow it (e.g. for
    a best-effort fallback chain) or surface it (e.g. forced single-path
    resolution where a DB error is a hard failure, not a "not found").
    """
    row = track_con.execute(
        "SELECT DISTINCT conversationId FROM ai_code_hashes WHERE requestId = ?",
        (uuid_val,),
    ).fetchone()
    if row and row["conversationId"]:
        return row["conversationId"]
    return None


def _lookup_chat_id(
    state_con: sqlite3.Connection, uuid_val: str,
) -> bool:
    """Return True iff `composerData:{uuid_val}` exists in cursorDiskKV."""
    row = state_con.execute(
        "SELECT 1 FROM cursorDiskKV WHERE key = ? AND value IS NOT NULL",
        (f"composerData:{uuid_val}",),
    ).fetchone()
    return bool(row)


def _scan_bubbles_for_request(
    state_con: sqlite3.Connection, uuid_val: str,
) -> str | None:
    """Scan all `bubbleId:{chat}:{bubble}` keys and return the chat_id whose
    bubble blob has `requestId == uuid_val`, or None if no match.

    Swallows DB errors (returns None) to match the original best-effort
    fallback behavior.
    """
    try:
        rows = state_con.execute(
            "SELECT key FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'",
        ).fetchall()
        for r in rows:
            key = r["key"]
            parts = key.split(":")
            if len(parts) >= 3:
                potential_chat_id = parts[1]
                blob_row = state_con.execute(
                    "SELECT value FROM cursorDiskKV WHERE key = ? AND value IS NOT NULL",
                    (key,),
                ).fetchone()
                if blob_row:
                    data = decode_blob(blob_row["value"])
                    if isinstance(data, dict) and data.get("requestId") == uuid_val:
                        return potential_chat_id
    except sqlite3.Error:
        return None
    return None


def resolve_chat_id(uuid_val: str,
                    state_con: sqlite3.Connection,
                    track_con: sqlite3.Connection,
                    force: str | None = None,
                    verbose: bool = False) -> tuple[str, str]:
    """Resolve a UUID to (chatId, source).

    source is one of: "request-id", "request-id-scan", "chat-id".
    Raises ValueError if resolution fails.
    """
    if uuid_val in _resolve_cache:
        cached = _resolve_cache[uuid_val]
        if verbose:
            print(f"  resolved {uuid_val} → {cached[0]} [cached, {cached[1]}]", file=sys.stderr)
        return cached

    if force == "chat-id":
        if _lookup_chat_id(state_con, uuid_val):
            result = (uuid_val, "chat-id")
            _resolve_cache[uuid_val] = result
            if verbose:
                print(f"  resolved {uuid_val} → {uuid_val} [forced chat-id]", file=sys.stderr)
            return result
        raise ValueError(
            f"UUID {uuid_val} not found as chat ID (composerData:{uuid_val} not in state.vscdb). "
            f"Flag --chat-id was used, so no other resolution paths were attempted."
        )

    if force == "request-id":
        # Forced single-path resolution: a sqlite3.Error here is a hard
        # failure, not a "not found" -- let the helper propagate it.
        chat_id = _lookup_request_id(track_con, uuid_val)
        if chat_id:
            result = (chat_id, "request-id")
            _resolve_cache[uuid_val] = result
            if verbose:
                print(f"  resolved request {uuid_val} → chat {chat_id} [via ai_code_hashes, forced]", file=sys.stderr)
            return result
        raise ValueError(
            f"UUID {uuid_val} not found as a Request ID in ai_code_hashes. "
            f"Flag --request-id was used, so no other resolution paths were attempted."
        )

    # Step 1: Try Request ID lookup in tracking DB. Best-effort: swallow
    # sqlite3.Error and fall through to Step 2.
    try:
        chat_id = _lookup_request_id(track_con, uuid_val)
    except sqlite3.Error:
        chat_id = None
    if chat_id:
        result = (chat_id, "request-id")
        _resolve_cache[uuid_val] = result
        if verbose:
            print(f"  resolved request {uuid_val} → chat {chat_id} [via ai_code_hashes]", file=sys.stderr)
        return result

    # Step 2: Fallback to bubble scan in state.vscdb
    scanned_chat_id = _scan_bubbles_for_request(state_con, uuid_val)
    if scanned_chat_id:
        result = (scanned_chat_id, "request-id-scan")
        _resolve_cache[uuid_val] = result
        if verbose:
            print(f"  resolved request {uuid_val} → chat {scanned_chat_id} [via bubble-scan]", file=sys.stderr)
        return result

    # Step 3: Direct chat-ID lookup
    if _lookup_chat_id(state_con, uuid_val):
        result = (uuid_val, "chat-id")
        _resolve_cache[uuid_val] = result
        if verbose:
            print(f"  resolved {uuid_val} → {uuid_val} [direct chat-id]", file=sys.stderr)
        return result

    raise ValueError(
        f"UUID {uuid_val} could not be resolved. Tried:\n"
        f"  1. Request ID lookup in ai_code_hashes — no match\n"
        f"  2. Bubble scan in state.vscdb (requestId field) — no match\n"
        f"  3. Direct chat-ID lookup (composerData:{uuid_val}) — not found"
    )


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def open_ro(path: Path) -> sqlite3.Connection:
    """Open a SQLite DB read-only, tolerating another process holding the file."""
    if not path.exists():
        raise FileNotFoundError(f"database not found: {path}")
    uri = f"file:{path.as_posix()}?mode=ro&immutable=0"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def decode_blob(value: Any) -> Any:
    """Decode a cursorDiskKV BLOB value to a Python object.

    Returns None if the value is null or cannot be decoded as JSON.
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8", errors="replace")
        except Exception:
            return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def maybe_parse_json(value: Any) -> Any:
    """If value is a JSON string, decode it once; otherwise return unchanged."""
    if isinstance(value, str):
        s = value.strip()
        if s.startswith(("{", "[", '"')):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return value
    return value


def epoch_ms_to_iso(ms: Any) -> str | None:
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def preview(value: Any, limit: int = 200) -> str:
    """Produce a compact, bounded string preview of any value."""
    if value is None:
        return ""
    if isinstance(value, str):
        s = value
    else:
        try:
            s = json.dumps(value, default=str)
        except Exception:
            s = str(value)
    s = s.replace("\r", " ").replace("\n", " ")
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

CHAT_META_FIELDS = (
    "composerId", "name", "subtitle", "createdAt", "lastUpdatedAt",
    "mode", "unifiedMode", "forceMode", "isAgentic", "status",
    "contextTokensUsed", "contextTokenLimit", "contextUsagePercent",
    "totalLinesAdded", "totalLinesRemoved",
    "stopHookLoopCount",
)


def fetch_chat_meta(state_con: sqlite3.Connection, chat_id: str) -> dict[str, Any] | None:
    row = state_con.execute(
        "SELECT value FROM cursorDiskKV WHERE key = ? AND value IS NOT NULL",
        (f"composerData:{chat_id}",),
    ).fetchone()
    if not row:
        return None
    data = decode_blob(row["value"])
    if not isinstance(data, dict):
        return None
    out: dict[str, Any] = {k: data.get(k) for k in CHAT_META_FIELDS}
    out["addedFiles"] = data.get("addedFiles") or []
    out["removedFiles"] = data.get("removedFiles") or []
    out["todos"] = data.get("todos") or []
    mc = data.get("modelConfig") or {}
    if isinstance(mc, dict):
        out["modelName"] = mc.get("modelName") or mc.get("model") or mc.get("name")
    out["headerOrder"] = [
        h.get("bubbleId") for h in (data.get("fullConversationHeadersOnly") or [])
        if isinstance(h, dict) and h.get("bubbleId")
    ]
    return out


def fetch_bubbles(state_con: sqlite3.Connection, chat_id: str) -> dict[str, dict]:
    """Return {bubbleId: bubble_payload} for every stored bubble in the chat."""
    rows = state_con.execute(
        "SELECT key, value FROM cursorDiskKV "
        "WHERE key LIKE ? AND value IS NOT NULL",
        (f"bubbleId:{chat_id}:%",),
    ).fetchall()
    out: dict[str, dict] = {}
    prefix = f"bubbleId:{chat_id}:"
    for r in rows:
        bid = r["key"][len(prefix):] if r["key"].startswith(prefix) else r["key"]
        data = decode_blob(r["value"])
        if isinstance(data, dict):
            out[bid] = data
    return out


def order_bubbles(meta: dict, bubbles_by_id: dict[str, dict]) -> list[tuple[int, str, dict]]:
    """Return [(index, bubbleId, payload), ...] ordered by the chat's header order."""
    ordered: list[tuple[int, str, dict]] = []
    seen: set[str] = set()
    for bid in meta.get("headerOrder") or []:
        if bid in bubbles_by_id and bid not in seen:
            ordered.append((len(ordered), bid, bubbles_by_id[bid]))
            seen.add(bid)
    for bid, payload in bubbles_by_id.items():
        if bid not in seen:
            ordered.append((len(ordered), bid, payload))
    return ordered


# ---------------------------------------------------------------------------
# Tool-call normalization
# ---------------------------------------------------------------------------

def classify_tool(name: str) -> str:
    """Return one of: 'mcp_optimus', 'mcp_other', 'builtin'."""
    if not name:
        return "builtin"
    low = name.lower()
    if low.startswith("mcp-") or low.startswith("mcp_"):
        if "optimus_" in low or low.endswith("-optimus") or "-optimus-" in low:
            return "mcp_optimus"
        return "mcp_other"
    return "builtin"


_KNOWN_OPTIMUS_TOOLS = (
    "optimus_search", "optimus_grep", "optimus_resolve",
    "optimus_prune", "optimus_extract",
)


def normalize_tool_name(name: str) -> str:
    """Strip Cursor's MCP server-name prefix to get the base tool name.

    Cursor registers MCP tools with a prefix that varies across sessions
    depending on project path encoding, Cursor version, and re-registration
    timing.  Examples:

        'mcp-eci-optimus-optimus-optimus_search' -> 'optimus_search'
        'mcp-optimus-optimus_grep'               -> 'optimus_grep'
        'mcp-some-server-do_thing'               -> 'do_thing'
        'read_file_v2'                           -> 'read_file_v2'
    """
    if not name:
        return name
    low = name.lower()
    if not (low.startswith("mcp-") or low.startswith("mcp_")):
        return name

    for suffix in _KNOWN_OPTIMUS_TOOLS:
        if low.endswith(suffix):
            return suffix

    # Generic MCP tool: the base name follows the last '-' that precedes
    # an identifier containing '_' (MCP tool names use underscores, server
    # prefixes use hyphens).  Fall back to last '-' segment.
    parts = name.split("-")
    for i in range(len(parts) - 1, 0, -1):
        candidate = "-".join(parts[i:])
        if "_" in candidate:
            return candidate
    return parts[-1] if len(parts) > 1 else name


def unwrap_mcp_payload(payload: Any) -> Any:
    """Unwrap Cursor's nested MCP params/result envelopes."""
    p = maybe_parse_json(payload)
    if isinstance(p, dict):
        if "tools" in p and isinstance(p["tools"], list) and p["tools"]:
            t = p["tools"][0]
            if isinstance(t, dict) and "parameters" in t:
                parsed = maybe_parse_json(t["parameters"])
                return {"name": t.get("name"), "serverName": t.get("serverName"), "parameters": parsed}
        if "result" in p and isinstance(p["result"], str):
            inner = maybe_parse_json(p["result"])
            if isinstance(inner, dict) and isinstance(inner.get("content"), list):
                texts = []
                for c in inner["content"]:
                    if isinstance(c, dict) and c.get("type") == "text":
                        texts.append(c.get("text") or "")
                if texts:
                    return {"text": "\n".join(texts), "raw": inner}
            return inner
    return p


def _detect_error_in_result(result: Any, result_text: str) -> tuple[bool, str | None]:
    """Detect MCP/tool-returned error payloads even when tfd.status == 'completed'."""
    if isinstance(result, dict):
        for key in ("error", "isError", "Error"):
            if key in result and result[key]:
                val = result[key]
                return True, str(val)[:200] if not isinstance(val, bool) else "error flag set"
    if result_text:
        low = result_text.lstrip()[:80].lower()
        if low.startswith('{"error"') or low.startswith('{"iserror":true') or low.startswith('error:'):
            return True, result_text[:200]
    return False, None


def _detect_denial(result_text: str) -> bool:
    """Two-tier denial detection: bare substring for high-confidence markers,
    context-required regex for short/ambiguous markers."""
    if not result_text:
        return False
    # Tier 1: long, unambiguous markers — bare substring match
    for marker in HOOK_DENIAL_MARKERS:
        if len(marker) >= _BARE_MARKER_MIN_LEN and marker in result_text:
            return True
    # Also check short markers from HOOK_DENIAL_MARKERS that are in the
    # curated _FALLBACK set (these were chosen for specificity).
    for marker in _FALLBACK_DENIAL_MARKERS:
        if marker in result_text:
            return True
    # Tier 2: context-required regex for short/ambiguous markers
    for pat in _CONTEXT_DENIAL_PATTERNS:
        if pat.search(result_text):
            return True
    return False


def _measure_result(result: Any) -> tuple[str, int]:
    """Return (result_text, result_size) for a tool-call result of any shape.

    Size is byte/char-length of `text` for text/string results, or the JSON-serialized
    length otherwise; -1 if serialization fails.
    """
    if isinstance(result, dict) and "text" in result and isinstance(result["text"], str):
        return result["text"], len(result["text"])
    if isinstance(result, str):
        return result, len(result)
    if result is None:
        return "", 0
    try:
        return "", len(json.dumps(result, default=str))
    except Exception:
        return "", -1


def _detect_tool_issues(
    result: Any, result_text: str, status: Any
) -> tuple[bool, bool, str | None, str]:
    """Detect denial / error conditions on a tool-call result.

    `result_text` must be the value returned by ``_measure_result(result)`` --
    the helpers re-derive denial/payload-error from both, so passing
    inconsistent values would yield wrong flags.

    Returns ``(is_denial, is_error, payload_error_msg, effective_status)``.
    """
    is_denial = _detect_denial(result_text)
    payload_error, payload_error_msg = _detect_error_in_result(result, result_text)
    is_error = (status not in (None, "completed")) or is_denial or payload_error
    effective_status = (
        "payload_error" if (payload_error and status == "completed") else (status or "(none)")
    )
    return is_denial, is_error, payload_error_msg, effective_status


def extract_tool_call(bubble: dict) -> dict | None:
    tfd = bubble.get("toolFormerData")
    if not isinstance(tfd, dict):
        return None
    raw_name = tfd.get("name") or ""
    name = normalize_tool_name(raw_name)
    params = unwrap_mcp_payload(tfd.get("params"))
    result = unwrap_mcp_payload(tfd.get("result"))

    result_text, result_size = _measure_result(result)
    status = tfd.get("status")
    is_denial, is_error, payload_error_msg, effective_status = _detect_tool_issues(
        result, result_text, status
    )

    return {
        "toolCallId": tfd.get("toolCallId"),
        "modelCallId": tfd.get("modelCallId"),
        "toolIndex": tfd.get("toolIndex"),
        "name": name,
        "rawName": raw_name,
        "tool": tfd.get("tool"),
        "status": status,
        "effectiveStatus": effective_status,
        "payloadErrorMessage": payload_error_msg,
        "category": classify_tool(raw_name),
        "params": params,
        "resultSize": result_size,
        "resultPreview": preview(result_text or result, 400),
        "isDenial": is_denial,
        "isError": is_error,
    }


def extract_thinking(bubble: dict) -> list[str]:
    """Return any surfaced thinking-block texts."""
    tb = bubble.get("allThinkingBlocks")
    out: list[str] = []
    if isinstance(tb, list):
        for item in tb:
            if isinstance(item, dict):
                text = item.get("text") or item.get("thinking") or ""
                if text:
                    out.append(text)
            elif isinstance(item, str):
                out.append(item)
    return out


# ---------------------------------------------------------------------------
# Attribution (ai-code-tracking.db)
# ---------------------------------------------------------------------------

def _fetch_code_hashes(track_con: sqlite3.Connection, chat_id: str, out: dict[str, Any]) -> None:
    """Populate codeHashesBySource / codeHashFirstAt / codeHashLastAt on `out`."""
    try:
        rows = track_con.execute(
            "SELECT source, COUNT(*) AS n, MIN(createdAt) AS firstAt, MAX(createdAt) AS lastAt "
            "FROM ai_code_hashes WHERE conversationId = ? GROUP BY source",
            (chat_id,),
        ).fetchall()
        firsts, lasts = [], []
        for r in rows:
            out["codeHashesBySource"][r["source"] or "(null)"] = r["n"]
            if r["firstAt"] is not None:
                firsts.append(r["firstAt"])
            if r["lastAt"] is not None:
                lasts.append(r["lastAt"])
        out["codeHashFirstAt"] = min(firsts) if firsts else None
        out["codeHashLastAt"] = max(lasts) if lasts else None
    except sqlite3.Error as e:
        out["codeHashesError"] = str(e)


def _fetch_deleted_files(track_con: sqlite3.Connection, chat_id: str, out: dict[str, Any]) -> None:
    """Populate deletedFiles on `out`."""
    try:
        rows = track_con.execute(
            "SELECT gitPath, composerId, model, deletedAt FROM ai_deleted_files "
            "WHERE conversationId = ? ORDER BY deletedAt",
            (chat_id,),
        ).fetchall()
        out["deletedFiles"] = [dict(r) for r in rows]
    except sqlite3.Error as e:
        out["deletedFilesError"] = str(e)


def _fetch_scored_commits(track_con: sqlite3.Connection, window: tuple[int, int], out: dict[str, Any]) -> None:
    """Populate scoredCommits on `out` for commits within the [start, end] window."""
    start, end = window
    try:
        rows = track_con.execute(
            "SELECT commitHash, branchName, scoredAt, linesAdded, linesDeleted, "
            "composerLinesAdded, composerLinesDeleted, humanLinesAdded, humanLinesDeleted, "
            "tabLinesAdded, tabLinesDeleted, v1AiPercentage, v2AiPercentage, "
            "commitMessage, commitDate "
            "FROM scored_commits WHERE scoredAt BETWEEN ? AND ? ORDER BY scoredAt",
            (start, end),
        ).fetchall()
        out["scoredCommits"] = [dict(r) for r in rows]
    except sqlite3.Error as e:
        out["scoredCommitsError"] = str(e)


def fetch_attribution(track_con: sqlite3.Connection, chat_id: str, window: tuple[int, int] | None) -> dict[str, Any]:
    out: dict[str, Any] = {"codeHashesBySource": {}, "codeHashFirstAt": None, "codeHashLastAt": None,
                          "deletedFiles": [], "scoredCommits": []}
    _fetch_code_hashes(track_con, chat_id, out)
    _fetch_deleted_files(track_con, chat_id, out)
    if window is not None:
        _fetch_scored_commits(track_con, window, out)
    return out


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

@dataclass
class BubbleRow:
    index: int
    bubbleId: str
    type: int
    typeName: str
    textPreview: str
    textLength: int
    tokenCount: int | None
    requestId: str | None
    toolCall: dict | None
    thinkingCount: int
    thinkingPreviews: list[str] = field(default_factory=list)
    lintCount: int = 0
    hasGitDiff: bool = False
    attachedFiles: list[str] = field(default_factory=list)
    recentlyViewedFiles: list[str] = field(default_factory=list)


def build_bubble_rows(ordered: list[tuple[int, str, dict]]) -> list[BubbleRow]:
    rows: list[BubbleRow] = []
    for idx, bid, b in ordered:
        t = b.get("type")
        tc = extract_tool_call(b)
        thinking = extract_thinking(b)
        lints = b.get("lints") or b.get("multiFileLinterErrors") or []
        rows.append(BubbleRow(
            index=idx,
            bubbleId=bid,
            type=t if isinstance(t, int) else -1,
            typeName=BUBBLE_TYPE_NAMES.get(t, f"type{t}"),
            textPreview=preview(b.get("text"), 240),
            textLength=len(b.get("text") or "") if isinstance(b.get("text"), str) else 0,
            tokenCount=b.get("tokenCount") if isinstance(b.get("tokenCount"), int) else None,
            requestId=b.get("requestId") if isinstance(b.get("requestId"), str) else None,
            toolCall=tc,
            thinkingCount=len(thinking),
            thinkingPreviews=[preview(t, 240) for t in thinking[:3]],
            lintCount=len(lints) if isinstance(lints, list) else 0,
            hasGitDiff=bool(b.get("gitDiffs")),
            attachedFiles=[
                c.get("relativeWorkspacePath") or c.get("uri") or ""
                for c in (b.get("attachedCodeChunks") or [])
                if isinstance(c, dict)
            ][:10],
            recentlyViewedFiles=[
                f.get("relativeWorkspacePath") or f.get("uri") or ""
                for f in (b.get("recentlyViewedFiles") or [])
                if isinstance(f, dict)
            ][:10],
        ))
    return rows


def _category_included(category: str, filter_category: str) -> bool:
    if filter_category == "all":
        return True
    if filter_category == "mcp":
        return category.startswith("mcp_")
    if filter_category == "optimus":
        return category == "mcp_optimus"
    return True


def _build_per_tool_rollup(rows: list[BubbleRow], filter_category: str) -> dict[str, dict[str, Any]]:
    """Build per-tool rollup dict: {toolName: {calls, success, errors, denials, totalResultSize}}."""
    rollup: dict[str, dict[str, Any]] = {}
    for r in rows:
        if not r.toolCall:
            continue
        tc = r.toolCall
        if not _category_included(tc["category"], filter_category):
            continue
        name = tc["name"] or "(unknown)"
        if name not in rollup:
            rollup[name] = {"calls": 0, "success": 0, "errors": 0, "denials": 0,
                            "totalResultSize": 0, "category": tc["category"]}
        entry = rollup[name]
        entry["calls"] += 1
        entry["totalResultSize"] += tc["resultSize"] if tc["resultSize"] >= 0 else 0
        if tc["isDenial"]:
            entry["denials"] += 1
        elif tc["isError"]:
            entry["errors"] += 1
        else:
            entry["success"] += 1
    return dict(sorted(rollup.items(), key=lambda kv: (-kv[1]["calls"], kv[0])))


def compute_summary(rows: list[BubbleRow], filter_category: str) -> dict[str, Any]:
    by_tool: dict[str, int] = {}
    by_category: dict[str, int] = {"mcp_optimus": 0, "mcp_other": 0, "builtin": 0}
    status_counts: dict[str, int] = {}
    errors = 0
    denials = 0
    included_tools = 0
    for r in rows:
        if not r.toolCall:
            continue
        tc = r.toolCall
        if not _category_included(tc["category"], filter_category):
            continue
        included_tools += 1
        by_tool[tc["name"] or "(unknown)"] = by_tool.get(tc["name"] or "(unknown)", 0) + 1
        by_category[tc["category"]] = by_category.get(tc["category"], 0) + 1
        s = tc["status"] or "(none)"
        status_counts[s] = status_counts.get(s, 0) + 1
        if tc["isError"]:
            errors += 1
        if tc["isDenial"]:
            denials += 1
    return {
        "bubbleCount": len(rows),
        "userBubbles": sum(1 for r in rows if r.type == 1),
        "assistantBubbles": sum(1 for r in rows if r.type == 2),
        "toolCallsIncluded": included_tools,
        "toolCallsByName": dict(sorted(by_tool.items(), key=lambda kv: (-kv[1], kv[0]))),
        "toolCallsByCategory": by_category,
        "statusCounts": status_counts,
        "errorCount": errors,
        "denialCount": denials,
        "thinkingBlockCount": sum(r.thinkingCount for r in rows),
    }


def build_tool_trajectory(rows: list[BubbleRow], filter_category: str) -> list[dict]:
    traj: list[dict] = []
    for r in rows:
        if not r.toolCall:
            continue
        tc = r.toolCall
        if not _category_included(tc["category"], filter_category):
            continue
        traj.append({
            "bubbleIndex": r.index,
            "name": tc["name"],
            "category": tc["category"],
            "status": tc["status"],
            "resultSize": tc["resultSize"],
            "isError": tc["isError"],
            "isDenial": tc["isDenial"],
            "paramsPreview": preview(tc["params"], 180),
        })
    return traj


def _failure_mode_key(tc: dict) -> tuple[str, str, str]:
    """Build a failure-mode key for grouping: (toolName, effectiveStatus, errorPreview80)."""
    err_preview = tc.get("payloadErrorMessage") or tc.get("resultPreview") or ""
    err_preview = err_preview[:80].replace("\n", " ").replace("\r", " ")
    return (tc["name"], tc.get("effectiveStatus") or tc.get("status") or "(none)", err_preview)


# ---------------------------------------------------------------------------
# Shared markdown section renderers
# ---------------------------------------------------------------------------

def _render_chat_header(meta: dict, chat_id: str, summary: dict,
                        filter_category: str, duration_str: str | None,
                        input_id: str | None = None,
                        resolved_from: str | None = None,
                        duration_note: str | None = None) -> list[str]:
    """Render the standard chat header block. Returns lines."""
    lines: list[str] = []

    _md_kv(lines, "Chat ID", f"`{chat_id}`")
    if input_id and input_id != chat_id:
        _md_kv(lines, "Input", f"request-id `{input_id}` → resolved to chat `{chat_id}`"
           + (f" [{resolved_from}]" if resolved_from else ""))
    if meta.get("modelName"):
        _md_kv(lines, "Model", meta["modelName"])
    if meta.get("mode"):
        _md_kv(lines, "Mode", f"{meta['mode']}" + (" (agentic)" if meta.get("isAgentic") else ""))
    _md_kv(lines, "Created", epoch_ms_to_iso(meta.get("createdAt")) or "—")
    _md_kv(lines, "Last updated", epoch_ms_to_iso(meta.get("lastUpdatedAt")) or "—")
    dur_display = duration_str or "—"
    if duration_note:
        dur_display += f" *({duration_note})*"
    _md_kv(lines, "Duration", dur_display)
    _md_kv(lines, "Bubbles", f"{summary['bubbleCount']} ({summary['userBubbles']} user, {summary['assistantBubbles']} assistant)")
    cup = meta.get("contextUsagePercent")
    pct_str = ""
    if isinstance(cup, (int, float)):
        frac = cup / 100 if cup > 1 else cup
        pct_str = f" ({frac:.1%})"
    _md_kv(lines, "Context tokens",
       f"{meta.get('contextTokensUsed') or '?'} / {meta.get('contextTokenLimit') or '?'}{pct_str}")
    _md_kv(lines, "Lines added / removed", f"{meta.get('totalLinesAdded') or 0} / {meta.get('totalLinesRemoved') or 0}")
    _md_kv(lines, "Stop-hook loop count", meta.get("stopHookLoopCount") or 0)
    _md_kv(lines, "Tool-call filter", filter_category)
    lines.append("")
    return lines


def _render_tool_rollup_table(rollup: dict[str, dict[str, Any]]) -> list[str]:
    """Render per-tool rollup as a markdown table. Returns lines."""
    lines: list[str] = []
    lines.append("| Tool | Calls | Success | Errors | Denials | Success % | Avg Result Size |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for name, r in rollup.items():
        pct = f"{r['success'] / r['calls'] * 100:.0f}%" if r["calls"] > 0 else "—"
        avg = f"{r['totalResultSize'] // r['calls']}" if r["calls"] > 0 else "—"
        lines.append(f"| `{name}` | {r['calls']} | {r['success']} | {r['errors']} | {r['denials']} | {pct} | {avg} |")
    lines.append("")
    return lines


def _render_failure_mode_list(failures: list[tuple[tuple[str, str, str], Any]],
                              heading: str,
                              show_chat_ids: bool = False) -> list[str]:
    """Render a failure-mode list section. Returns lines."""
    lines: list[str] = []
    if not failures:
        lines.append(f"_{heading}: none_")
        lines.append("")
        return lines
    lines.append(f"**{heading}** ({len(failures)})")
    lines.append("")
    if show_chat_ids:
        lines.append("| Tool | Status | Error preview | Count | Chats |")
        lines.append("|---|---|---|---:|---|")
        for key, info in failures:
            tool_name, status, err_prev = key
            count = info.get("count", 1)
            chats = ", ".join(f"`{c[:8]}`" for c in info.get("chatIds", []))
            lines.append(f"| `{tool_name}` | {status} | {err_prev} | {count} | {chats} |")
    else:
        lines.append("| Tool | Status | Error preview |")
        lines.append("|---|---|---|")
        for key, _ in failures:
            tool_name, status, err_prev = key
            lines.append(f"| `{tool_name}` | {status} | {err_prev} |")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Report writers
# ---------------------------------------------------------------------------

def write_json_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True, default=str)
        f.write("\n")


def _md_h(lines: list[str], s: str, level: int = 2) -> None:
    """Append a markdown heading + blank line to `lines`."""
    lines.append("#" * level + " " + s)
    lines.append("")


def _md_kv(lines: list[str], k: str, v: Any) -> None:
    """Append a `- **key:** value` bullet to `lines`."""
    lines.append(f"- **{k}:** {v}")


def _write_summary_section(
    lines: list[str],
    report: dict,
    filter_category: str,
) -> None:
    """Write the report header, summary table, tool-call-by-name table, and
    the per-Optimus-call detail blocks (when present)."""
    meta = report["meta"]
    summary = report["summary"]
    optimus_calls = report["optimusCalls"]

    _md_h(lines, f"Chat report — {meta.get('name') or report['chatId']}", 1)
    lines.extend(_render_chat_header(
        meta, report["chatId"], summary, filter_category, report.get("durationStr"),
        input_id=report.get("inputId"), resolved_from=report.get("resolvedFrom"),
        duration_note=report.get("durationNote"),
    ))

    _md_h(lines, "Summary", 2)
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Tool calls (included) | {summary['toolCallsIncluded']} |")
    lines.append(f"| MCP Optimus | {summary['toolCallsByCategory'].get('mcp_optimus', 0)} |")
    lines.append(f"| MCP other | {summary['toolCallsByCategory'].get('mcp_other', 0)} |")
    lines.append(f"| Builtin tools | {summary['toolCallsByCategory'].get('builtin', 0)} |")
    lines.append(f"| Errors / non-completed | {summary['errorCount']} |")
    lines.append(f"| Guard / hook denials | {summary['denialCount']} |")
    lines.append(f"| Thinking blocks captured | {summary['thinkingBlockCount']} |")
    lines.append("")

    if summary["toolCallsByName"]:
        _md_h(lines, "Tool-call counts by name", 3)
        lines.append("| Tool | Calls |")
        lines.append("|---|---:|")
        for name, n in summary["toolCallsByName"].items():
            lines.append(f"| `{name}` | {n} |")
        lines.append("")

    if optimus_calls:
        _md_h(lines, "Optimus MCP calls (detailed)", 2)
        for oc in optimus_calls:
            lines.append(f"### Bubble {oc['bubbleIndex']} — `{oc['name']}` [{oc['status']}]")
            lines.append("")
            lines.append(f"- **paramsPreview:** `{oc['paramsPreview']}`")
            lines.append(f"- **resultSize:** {oc['resultSize']} chars")
            if oc["resultPreview"]:
                lines.append("- **resultPreview:**")
                lines.append("")
                lines.append("    ```")
                lines.append("    " + oc["resultPreview"].replace("\n", "\n    "))
                lines.append("    ```")
            if oc.get("thinkingPreviews"):
                lines.append("- **thinking around this call:**")
                for t in oc["thinkingPreviews"]:
                    lines.append(f"    - {t}")
            lines.append("")


def _write_issues_section(
    lines: list[str],
    denials: list[dict],
    errors: list[dict],
) -> None:
    """Write the denials and errors tables (each section omitted when empty)."""
    if denials:
        _md_h(lines, "Guard / hook denials", 2)
        lines.append("| # | Bubble | Tool | Preview |")
        lines.append("|---:|---:|---|---|")
        for d in denials:
            lines.append(f"| {d['bubbleIndex']} | {d['bubbleIndex']} | `{d['name']}` | {d['resultPreview']} |")
        lines.append("")

    if errors:
        _md_h(lines, "Errors / non-completed tool calls", 2)
        lines.append("| Bubble | Tool | Status | Preview |")
        lines.append("|---:|---|---|---|")
        for e in errors:
            lines.append(f"| {e['bubbleIndex']} | `{e['name']}` | {e['status']} | {e['resultPreview'] or e.get('payloadErrorMessage') or ''} |")
        lines.append("")


def _write_trajectory_section(
    lines: list[str],
    traj: list[dict],
    attr: dict,
) -> None:
    """Write the tool-call trajectory table (when non-empty) and the
    attribution block (always emitted, including scored-commits sub-table
    when present)."""
    if traj:
        _md_h(lines, "Tool-call trajectory", 2)
        lines.append("| # | Bubble | Category | Tool | Status | Bytes | Params preview |")
        lines.append("|---:|---:|---|---|---|---:|---|")
        for i, t in enumerate(traj):
            flag = "!" if t["isError"] else ("G" if t["isDenial"] else "")
            lines.append(
                f"| {i} | {t['bubbleIndex']} | {t['category']} | `{t['name']}` | "
                f"{t['status'] or ''} {flag} | {t['resultSize']} | `{t['paramsPreview']}` |"
            )
        lines.append("")

    _md_h(lines, "Attribution (ai-code-tracking.db)", 2)
    lines.append("- Code hashes by source:")
    if attr.get("codeHashesBySource"):
        for src, n in sorted(attr["codeHashesBySource"].items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"    - `{src}`: {n}")
    else:
        lines.append("    - (none)")
    _md_kv(lines, "Code-hash first at", epoch_ms_to_iso(attr.get("codeHashFirstAt")) or "—")
    _md_kv(lines, "Code-hash last at", epoch_ms_to_iso(attr.get("codeHashLastAt")) or "—")
    _md_kv(lines, "AI-deleted files", len(attr.get("deletedFiles") or []))
    if attr.get("scoredCommits"):
        _md_h(lines, "Scored commits during chat window", 3)
        lines.append("| When | Hash | Branch | v2 AI % | Lines +/- | Message |")
        lines.append("|---|---|---|---:|---|---|")
        for c in attr["scoredCommits"]:
            iso = epoch_ms_to_iso(c.get("scoredAt")) or ""
            lines.append(
                f"| {iso} | `{str(c.get('commitHash'))[:10]}` | {c.get('branchName')} "
                f"| {c.get('v2AiPercentage') or ''} "
                f"| +{c.get('linesAdded') or 0} / -{c.get('linesDeleted') or 0} "
                f"| {preview(c.get('commitMessage') or '', 80)} |"
            )
        lines.append("")


def _write_bubble_table(lines: list[str], rows: list[dict]) -> None:
    """Write the bubble-timeline table at the bottom of the report."""
    _md_h(lines, "Bubble timeline", 2)
    lines.append("| # | Type | Tool | Text preview |")
    lines.append("|---:|---|---|---|")
    for r in rows:
        tool = r["toolCall"]["name"] if r.get("toolCall") else ""
        lines.append(f"| {r['index']} | {r['typeName']} | `{tool}` | {r['textPreview']} |")
    lines.append("")


def write_md_report(path: Path, report: dict, filter_category: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    _write_summary_section(lines, report, filter_category)
    _write_issues_section(lines, report["denials"], report["errors"])
    _write_trajectory_section(lines, report["toolTrajectory"], report["attribution"])
    _write_bubble_table(lines, report["bubbles"])

    with path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def write_bubble_dump(path: Path, ordered_raw: list[tuple[int, str, dict]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for idx, bid, payload in ordered_raw:
            f.write(json.dumps({"index": idx, "bubbleId": bid, "payload": payload}, default=str))
            f.write("\n")


# ---------------------------------------------------------------------------
# Diff mode
# ---------------------------------------------------------------------------

def _build_failure_set(report: dict) -> dict[tuple[str, str, str], int]:
    """Build {failure_mode_key: count} from a report's error+denial lists."""
    fm: dict[tuple[str, str, str], int] = {}
    for e in report.get("errors", []):
        key = (e["name"], e.get("status") or "(none)",
               (e.get("payloadErrorMessage") or e.get("resultPreview") or "")[:80])
        fm[key] = fm.get(key, 0) + 1
    for d in report.get("denials", []):
        key = (d["name"], "denial", (d.get("resultPreview") or "")[:80])
        fm[key] = fm.get(key, 0) + 1
    return fm


def _compare_tool_counts(sum_a: dict, sum_b: dict) -> list[dict]:
    """Compute per-tool count delta sorted by descending |delta|."""
    all_tools = sorted(set(sum_a["toolCallsByName"].keys()) | set(sum_b["toolCallsByName"].keys()))
    rows: list[dict] = []
    for name in all_tools:
        ca = sum_a["toolCallsByName"].get(name, 0)
        cb = sum_b["toolCallsByName"].get(name, 0)
        flags = []
        if ca == 0:
            flags.append("(new)")
        if cb == 0:
            flags.append("(gone)")
        rows.append({
            "tool": name, "countBefore": ca, "countAfter": cb,
            "delta": cb - ca, "flags": " ".join(flags),
        })
    rows.sort(key=lambda x: -abs(x["delta"]))
    return rows


def _compare_category_shift(sum_a: dict, sum_b: dict) -> list[dict]:
    """Compute per-category Before/After/delta rows for the standard categories."""
    return [
        {
            "category": cat,
            "before": sum_a["toolCallsByCategory"].get(cat, 0),
            "after": sum_b["toolCallsByCategory"].get(cat, 0),
            "delta": sum_b["toolCallsByCategory"].get(cat, 0) - sum_a["toolCallsByCategory"].get(cat, 0),
        }
        for cat in ("mcp_optimus", "mcp_other", "builtin")
    ]


def _compare_failure_modes(report_a: dict, report_b: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """Return (new, resolved, unchanged) failure-mode lists in serialized form."""
    fm_a = _build_failure_set(report_a)
    fm_b = _build_failure_set(report_b)
    new = [{"key": list(k), "count": fm_b[k]} for k in fm_b if k not in fm_a]
    resolved = [{"key": list(k), "count": fm_a[k]} for k in fm_a if k not in fm_b]
    unchanged = [{"key": list(k), "countBefore": fm_a[k], "countAfter": fm_b[k]}
                 for k in fm_a if k in fm_b]
    return new, resolved, unchanged


def _compare_attribution(report_a: dict, report_b: dict) -> dict[str, Any]:
    """Compute the attribution-delta sub-dict for a diff report."""
    attr_a = report_a.get("attribution", {})
    attr_b = report_b.get("attribution", {})
    code_hashes_a = sum(attr_a.get("codeHashesBySource", {}).values())
    code_hashes_b = sum(attr_b.get("codeHashesBySource", {}).values())
    return {
        "codeHashesBefore": code_hashes_a,
        "codeHashesAfter": code_hashes_b,
        "delta": code_hashes_b - code_hashes_a,
        "linesAddedBefore": report_a["meta"].get("totalLinesAdded") or 0,
        "linesAddedAfter": report_b["meta"].get("totalLinesAdded") or 0,
        "linesRemovedBefore": report_a["meta"].get("totalLinesRemoved") or 0,
        "linesRemovedAfter": report_b["meta"].get("totalLinesRemoved") or 0,
    }


def build_diff_report(report_a: dict, report_b: dict, filter_category: str) -> dict[str, Any]:
    """Build a diff report comparing two single-chat reports."""
    sum_a = report_a["summary"]
    sum_b = report_b["summary"]
    new_fms, resolved_fms, unchanged_fms = _compare_failure_modes(report_a, report_b)

    diff = {
        "type": "diff",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "filterCategory": filter_category,
        "chatIdBefore": report_a["chatId"],
        "chatIdAfter": report_b["chatId"],
        "nameBefore": report_a["meta"].get("name") or report_a["chatId"],
        "nameAfter": report_b["meta"].get("name") or report_b["chatId"],
        "toolCountDelta": _compare_tool_counts(sum_a, sum_b),
        "categoryShift": _compare_category_shift(sum_a, sum_b),
        "newFailureModes": new_fms,
        "resolvedFailureModes": resolved_fms,
        "unchangedFailureModes": unchanged_fms,
        "guardHitDelta": {
            "before": sum_a["denialCount"],
            "after": sum_b["denialCount"],
            "delta": sum_b["denialCount"] - sum_a["denialCount"],
        },
        "attributionDelta": _compare_attribution(report_a, report_b),
        "summaryBefore": sum_a,
        "summaryAfter": sum_b,
    }
    return diff


def _write_comparison_table(lines: list[str], diff: dict, report_a: dict, report_b: dict) -> None:
    """Append the side-by-side Before/After meta + summary table."""
    lines.append("| | Before | After |")
    lines.append("|---|---|---|")
    lines.append(f"| Chat ID | `{diff['chatIdBefore'][:12]}…` | `{diff['chatIdAfter'][:12]}…` |")
    lines.append(f"| Name | {diff['nameBefore']} | {diff['nameAfter']} |")
    lines.append(f"| Model | {report_a['meta'].get('modelName') or '—'} | {report_b['meta'].get('modelName') or '—'} |")
    dur_a = report_a.get("durationStr") or "—"
    dur_b = report_b.get("durationStr") or "—"
    if report_a.get("durationSuspect"):
        dur_a += " *"
    if report_b.get("durationSuspect"):
        dur_b += " *"
    lines.append(f"| Duration | {dur_a} | {dur_b} |")
    sa, sb = diff["summaryBefore"], diff["summaryAfter"]
    lines.append(f"| Bubbles | {sa['bubbleCount']} | {sb['bubbleCount']} |")
    lines.append(f"| Tool calls | {sa['toolCallsIncluded']} | {sb['toolCallsIncluded']} |")
    lines.append(f"| Errors | {sa['errorCount']} | {sb['errorCount']} |")
    lines.append(f"| Denials | {sa['denialCount']} | {sb['denialCount']} |")
    lines.append("")


def _write_diff_delta_tables(lines: list[str], diff: dict) -> None:
    """Append the tool-count and category-shift delta tables."""
    _md_h(lines, "Tool-call count delta", 2)
    lines.append("| Tool | Before | After | Δ | Notes |")
    lines.append("|---|---:|---:|---:|---|")
    for t in diff["toolCountDelta"]:
        d = t["delta"]
        sign = f"+{d}" if d > 0 else str(d)
        lines.append(f"| `{t['tool']}` | {t['countBefore']} | {t['countAfter']} | {sign} | {t['flags']} |")
    lines.append("")

    _md_h(lines, "Category shift", 2)
    lines.append("| Category | Before | After | Δ |")
    lines.append("|---|---:|---:|---:|")
    for c in diff["categoryShift"]:
        d = c["delta"]
        sign = f"+{d}" if d > 0 else str(d)
        lines.append(f"| {c['category']} | {c['before']} | {c['after']} | {sign} |")
    lines.append("")


def _write_diff_failure_sections(lines: list[str], diff: dict) -> None:
    """Append the three failure-mode sections (new / resolved / unchanged)."""
    _md_h(lines, "New failure modes (regressions)", 2)
    lines.extend(_render_failure_mode_list(
        [(tuple(fm["key"]), fm) for fm in diff["newFailureModes"]],
        "New failures in After",
    ))

    _md_h(lines, "Resolved failure modes (wins)", 2)
    lines.extend(_render_failure_mode_list(
        [(tuple(fm["key"]), fm) for fm in diff["resolvedFailureModes"]],
        "Resolved from Before",
    ))

    _md_h(lines, "Unchanged failure modes", 2)
    lines.extend(_render_failure_mode_list(
        [(tuple(fm["key"]), fm) for fm in diff["unchangedFailureModes"]],
        "Persisting in both",
    ))


def _write_diff_trailer(lines: list[str], diff: dict) -> None:
    """Append the guard-hit delta and attribution-summary delta sections."""
    gh = diff["guardHitDelta"]
    _md_h(lines, "Guard-hit delta", 2)
    gd = gh["delta"]
    gsign = f"+{gd}" if gd > 0 else str(gd)
    lines.append(f"- Before: {gh['before']} denials → After: {gh['after']} denials (Δ {gsign})")
    lines.append("")

    ad = diff["attributionDelta"]
    _md_h(lines, "Attribution summary delta", 2)
    lines.append(f"- Code hashes: {ad['codeHashesBefore']} → {ad['codeHashesAfter']} (Δ {ad['delta']:+d})")
    lines.append(f"- Lines added: {ad['linesAddedBefore']} → {ad['linesAddedAfter']}")
    lines.append(f"- Lines removed: {ad['linesRemovedBefore']} → {ad['linesRemovedAfter']}")
    lines.append("")


def write_diff_md(path: Path, diff: dict, report_a: dict, report_b: dict, filter_category: str) -> None:
    """Write a markdown diff report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    _md_h(lines, f"Diff report — {diff['nameBefore']} vs {diff['nameAfter']}", 1)
    _write_comparison_table(lines, diff, report_a, report_b)
    _write_diff_delta_tables(lines, diff)
    _write_diff_failure_sections(lines, diff)
    _write_diff_trailer(lines, diff)

    with path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ---------------------------------------------------------------------------
# Aggregate mode
# ---------------------------------------------------------------------------

def _build_chat_inventory(reports: list[dict]) -> list[dict]:
    """Build the per-chat inventory rows for an aggregate report."""
    inventory: list[dict] = []
    for rpt in reports:
        meta = rpt["meta"]
        summary = rpt["summary"]
        inventory.append({
            "chatId": rpt["chatId"],
            "name": meta.get("name") or rpt["chatId"],
            "model": meta.get("modelName") or "—",
            "duration": rpt.get("durationStr") or "—",
            "durationSuspect": rpt.get("durationSuspect", False),
            "bubbleCount": summary["bubbleCount"],
            "optimusCalls": summary["toolCallsByCategory"].get("mcp_optimus", 0),
            "totalErrors": summary["errorCount"],
            "totalDenials": summary["denialCount"],
        })
    return inventory


def _rollup_tool_stats(reports: list[dict]) -> dict[str, dict[str, Any]]:
    """Aggregate per-tool rollup data across reports and return the serialized
    per-tool rollup dict (sorted by call count desc, then name asc)."""
    accum: dict[str, dict[str, Any]] = {}
    for rpt in reports:
        chat_id = rpt["chatId"]
        for name, data in rpt.get("_perToolRollup", {}).items():
            if name not in accum:
                accum[name] = {
                    "calls": 0, "success": 0, "errors": 0, "denials": 0,
                    "totalResultSize": 0, "category": data["category"],
                    "chatIds": set(),
                }
            entry = accum[name]
            entry["calls"] += data["calls"]
            entry["success"] += data["success"]
            entry["errors"] += data["errors"]
            entry["denials"] += data["denials"]
            entry["totalResultSize"] += data["totalResultSize"]
            entry["chatIds"].add(chat_id)

    serialized: dict[str, dict[str, Any]] = {}
    for name, data in sorted(accum.items(), key=lambda kv: (-kv[1]["calls"], kv[0])):
        serialized[name] = {
            "calls": data["calls"],
            "success": data["success"],
            "errors": data["errors"],
            "denials": data["denials"],
            "totalResultSize": data["totalResultSize"],
            "category": data["category"],
            "avgResultSize": data["totalResultSize"] // data["calls"] if data["calls"] > 0 else 0,
            "successRate": round(data["success"] / data["calls"] * 100, 1) if data["calls"] > 0 else 0,
            "distinctChats": len(data["chatIds"]),
        }
    return serialized


def _extract_failure_modes(reports: list[dict]) -> list[dict]:
    """Aggregate failure-mode frequencies across reports, sorted by count desc."""
    modes: dict[tuple[str, str, str], dict[str, Any]] = {}
    for rpt in reports:
        chat_id = rpt["chatId"]
        for key, count in _build_failure_set(rpt).items():
            if key not in modes:
                modes[key] = {"count": 0, "chatIds": []}
            modes[key]["count"] += count
            modes[key]["chatIds"].append(chat_id)

    ordered = sorted(modes.items(), key=lambda kv: -kv[1]["count"])
    return [
        {"key": list(k), "count": v["count"], "chatIds": v["chatIds"]}
        for k, v in ordered
    ]


def _aggregate_attribution_totals(reports: list[dict]) -> dict[str, Any]:
    """Aggregate attribution counters across reports."""
    code_hashes: dict[str, int] = {}
    lines_added = 0
    lines_removed = 0
    scored_commits: list[dict] = []
    for rpt in reports:
        meta = rpt["meta"]
        attr = rpt.get("attribution", {})
        for src, n in attr.get("codeHashesBySource", {}).items():
            code_hashes[src] = code_hashes.get(src, 0) + n
        lines_added += meta.get("totalLinesAdded") or 0
        lines_removed += meta.get("totalLinesRemoved") or 0
        scored_commits.extend(attr.get("scoredCommits", []))

    return {
        "codeHashesBySource": code_hashes,
        "totalLinesAdded": lines_added,
        "totalLinesRemoved": lines_removed,
        "scoredCommitCount": len(scored_commits),
        "scoredCommitLinesAdded": sum(c.get("linesAdded") or 0 for c in scored_commits),
        "scoredCommitLinesDeleted": sum(c.get("linesDeleted") or 0 for c in scored_commits),
    }


def build_aggregate_report(reports: list[dict], filter_category: str) -> dict[str, Any]:
    """Build an aggregate report across N single-chat reports."""
    per_tool = _rollup_tool_stats(reports)
    optimus_rollup = {k: v for k, v in per_tool.items() if v["category"] == "mcp_optimus"}

    return {
        "type": "aggregate",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "filterCategory": filter_category,
        "chatCount": len(reports),
        "chatInventory": _build_chat_inventory(reports),
        "perToolRollup": per_tool,
        "optimusToolHealth": optimus_rollup,
        "failureModeFrequency": _extract_failure_modes(reports),
        "attributionTotals": _aggregate_attribution_totals(reports),
    }


def write_aggregate_md(path: Path, agg: dict) -> None:
    """Write a markdown aggregate report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    _md_h(lines, f"Aggregate report — {agg['chatCount']} chats", 1)
    lines.append(f"- **Generated:** {agg['generatedAt']}")
    lines.append(f"- **Filter:** {agg['filterCategory']}")
    lines.append("")

    _md_h(lines, "Chat inventory", 2)
    lines.append("| Chat ID | Name | Model | Duration | Bubbles | Optimus calls | Errors | Denials |")
    lines.append("|---|---|---|---|---:|---:|---:|---:|")
    any_suspect = False
    for c in agg["chatInventory"]:
        dur = c["duration"]
        if c.get("durationSuspect"):
            dur += " *"
            any_suspect = True
        lines.append(
            f"| `{c['chatId'][:12]}…` | {c['name']} | {c['model']} | {dur} "
            f"| {c['bubbleCount']} | {c['optimusCalls']} | {c['totalErrors']} | {c['totalDenials']} |"
        )
    if any_suspect:
        lines.append("")
        lines.append("_* Duration likely includes idle time._")
    lines.append("")

    _md_h(lines, "Per-tool rollup", 2)
    rollup = agg["perToolRollup"]
    if rollup:
        lines.append("| Tool | Calls | Success | Errors | Denials | Success % | Avg Size | Chats |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for name, r in rollup.items():
            lines.append(
                f"| `{name}` | {r['calls']} | {r['success']} | {r['errors']} | {r['denials']} "
                f"| {r['successRate']}% | {r['avgResultSize']} | {r['distinctChats']} |"
            )
        lines.append("")
    else:
        lines.append("_(no tool calls)_")
        lines.append("")

    optimus = agg["optimusToolHealth"]
    if optimus:
        _md_h(lines, "Optimus tool health", 2)
        lines.append("| Tool | Calls | Success | Errors | Denials | Success % | Avg Size | Chats |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for name, r in optimus.items():
            lines.append(
                f"| `{name}` | {r['calls']} | {r['success']} | {r['errors']} | {r['denials']} "
                f"| {r['successRate']}% | {r['avgResultSize']} | {r['distinctChats']} |"
            )
        lines.append("")

    _md_h(lines, "Failure-mode frequency", 2)
    fmf = agg["failureModeFrequency"]
    if fmf:
        lines.extend(_render_failure_mode_list(
            [(tuple(fm["key"]), fm) for fm in fmf],
            "All failure modes",
            show_chat_ids=True,
        ))
    else:
        lines.append("_(no failures)_")
        lines.append("")

    at = agg["attributionTotals"]
    _md_h(lines, "Attribution totals", 2)
    lines.append("- Code hashes by source:")
    if at.get("codeHashesBySource"):
        for src, n in sorted(at["codeHashesBySource"].items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"    - `{src}`: {n}")
    else:
        lines.append("    - (none)")
    lines.append(f"- **Total lines added:** {at['totalLinesAdded']}")
    lines.append(f"- **Total lines removed:** {at['totalLinesRemoved']}")
    lines.append(f"- **Scored commits:** {at['scoredCommitCount']} "
                 f"(+{at['scoredCommitLinesAdded']} / -{at['scoredCommitLinesDeleted']})")
    lines.append("")

    with path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _classify_tool_rows(
    rows: list, filter_category: str,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Partition rows' tool calls into (optimus_calls, denials, errors).

    A row enters `optimus_calls` if its tool category is `mcp_optimus`
    (regardless of filter; Optimus calls are always tracked). Denials and
    errors are gated by the filter via `_category_included`.
    """
    optimus_calls: list[dict] = []
    denials: list[dict] = []
    errors: list[dict] = []
    for r in rows:
        if not r.toolCall:
            continue
        tc = r.toolCall
        included = _category_included(tc["category"], filter_category)
        if tc["category"] == "mcp_optimus":
            optimus_calls.append({
                "bubbleIndex": r.index,
                "name": tc["name"],
                "status": tc["status"],
                "paramsPreview": preview(tc["params"], 300),
                "resultSize": tc["resultSize"],
                "resultPreview": tc["resultPreview"],
                "thinkingPreviews": r.thinkingPreviews,
            })
        if included and tc["isDenial"]:
            denials.append({
                "bubbleIndex": r.index,
                "name": tc["name"],
                "resultPreview": tc["resultPreview"],
            })
        if included and tc["isError"] and not tc["isDenial"]:
            errors.append({
                "bubbleIndex": r.index,
                "name": tc["name"],
                "status": tc.get("effectiveStatus") or tc["status"],
                "payloadErrorMessage": tc.get("payloadErrorMessage"),
                "resultPreview": tc["resultPreview"],
            })
    return optimus_calls, denials, errors


def _compute_chat_window(meta: dict) -> tuple[int, int] | None:
    """Build a (createdAt_ms, lastUpdatedAt_ms) window from chat meta when
    both timestamps are numeric; otherwise None."""
    if isinstance(meta.get("createdAt"), (int, float)) and isinstance(
        meta.get("lastUpdatedAt"), (int, float)
    ):
        return (int(meta["createdAt"]), int(meta["lastUpdatedAt"]))
    return None


def _compute_duration_stats(
    window: tuple[int, int] | None, bubble_count: int,
) -> tuple[str | None, bool, str | None]:
    """Compute (duration_str, duration_suspect, duration_note) from a window.

    Returns (None, False, None) when window is missing or the delta is
    negative. Flags the duration as suspect (with an idle-time note) when
    the elapsed time exceeds `max(_DURATION_WARN_THRESHOLD_S,
    bubble_count * _DURATION_PER_BUBBLE_S)`.
    """
    if not window:
        return None, False, None
    delta_ms = window[1] - window[0]
    if delta_ms < 0:
        return None, False, None
    secs = delta_ms // 1000
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    duration_str = f"{h}h {m}m {s}s" if h else (f"{m}m {s}s" if m else f"{s}s")
    expected_max_s = max(
        _DURATION_WARN_THRESHOLD_S, bubble_count * _DURATION_PER_BUBBLE_S
    )
    if secs > expected_max_s:
        return (
            duration_str,
            True,
            f"likely includes idle time "
            f"({duration_str} for {bubble_count} bubbles)",
        )
    return duration_str, False, None


def _build_bubble_payload(rows: list) -> list[dict]:
    """Project each bubble row into the public report shape."""
    return [
        {
            "index": r.index,
            "bubbleId": r.bubbleId,
            "type": r.type,
            "typeName": r.typeName,
            "textPreview": r.textPreview,
            "textLength": r.textLength,
            "tokenCount": r.tokenCount,
            "requestId": r.requestId,
            "lintCount": r.lintCount,
            "hasGitDiff": r.hasGitDiff,
            "attachedFiles": r.attachedFiles,
            "recentlyViewedFiles": r.recentlyViewedFiles,
            "thinkingCount": r.thinkingCount,
            "toolCall": (
                {
                    "name": r.toolCall["name"],
                    "category": r.toolCall["category"],
                    "status": r.toolCall["status"],
                    "resultSize": r.toolCall["resultSize"],
                    "isError": r.toolCall["isError"],
                    "isDenial": r.toolCall["isDenial"],
                    "paramsPreview": preview(r.toolCall["params"], 180),
                }
                if r.toolCall else None
            ),
        }
        for r in rows
    ]


def build_report(chat_id: str, state_con: sqlite3.Connection, track_con: sqlite3.Connection,
                 filter_category: str,
                 input_id: str | None = None,
                 resolved_from: str | None = None) -> dict[str, Any] | None:
    meta = fetch_chat_meta(state_con, chat_id)
    if not meta:
        return None
    bubbles_by_id = fetch_bubbles(state_con, chat_id)
    ordered = order_bubbles(meta, bubbles_by_id)
    rows = build_bubble_rows(ordered)

    summary = compute_summary(rows, filter_category)
    traj = build_tool_trajectory(rows, filter_category)
    per_tool_rollup = _build_per_tool_rollup(rows, filter_category)

    optimus_calls, denials, errors = _classify_tool_rows(rows, filter_category)

    window = _compute_chat_window(meta)
    attribution = fetch_attribution(track_con, chat_id, window)
    duration_str, duration_suspect, duration_note = _compute_duration_stats(
        window, len(ordered),
    )

    report = {
        "chatId": chat_id,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "filterCategory": filter_category,
        "meta": meta,
        "durationStr": duration_str,
        "durationSuspect": duration_suspect,
        "durationNote": duration_note,
        "summary": summary,
        "toolTrajectory": traj,
        "optimusCalls": optimus_calls,
        "denials": denials,
        "errors": errors,
        "attribution": attribution,
        "bubbles": _build_bubble_payload(rows),
        "_orderedRaw": ordered,
        "_perToolRollup": per_tool_rollup,
    }
    if input_id and input_id != chat_id:
        report["inputId"] = input_id
        report["resolvedFrom"] = resolved_from
    return report


def _build_argparser() -> argparse.ArgumentParser:
    """Construct the chat-report argparse.ArgumentParser."""
    ap = argparse.ArgumentParser(
        description="Extract Optimus-test-relevant signals from Cursor chats.",
    )
    ap.add_argument("ids", nargs="*", metavar="UUID",
                    help="Request IDs or chat IDs (auto-resolved)")

    mode_group = ap.add_mutually_exclusive_group()
    mode_group.add_argument("--diff", nargs=2, metavar="UUID",
                            help="Diff mode: compare two chats (before after)")
    mode_group.add_argument("--aggregate", nargs="+", metavar="UUID",
                            help="Aggregate mode: rollup across N chats")

    ap.add_argument("--request-id", action="append", dest="request_ids", metavar="UUID",
                    help="Force Request ID resolution (repeatable)")
    ap.add_argument("--chat-id", action="append", dest="chat_ids", metavar="UUID",
                    help="Force chat-ID interpretation (repeatable)")

    ap.add_argument("--tools", choices=("all", "mcp", "optimus"), default="all",
                    help="Which tool calls to include (default: all)")
    ap.add_argument("--format", choices=("md", "json", "both"), default="both",
                    help="Output format (default: both)")
    ap.add_argument("--dump-bubbles", action="store_true",
                    help="Also write <chatId>.bubbles.jsonl with raw bubble payloads")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR,
                    help=f"Output directory (default: {DEFAULT_OUT_DIR})")
    ap.add_argument("--state-db", type=Path, default=None,
                    help="Path to Cursor state.vscdb")
    ap.add_argument("--tracking-db", type=Path, default=None,
                    help="Path to ai-code-tracking.db")
    ap.add_argument("--workspace", type=Path, default=None,
                    help="Workspace root for hook-script detection (default: auto-detect)")
    ap.add_argument("--denial-marker", action="append", dest="denial_markers", metavar="TEXT",
                    help="Additional denial marker text (repeatable)")
    ap.add_argument("--verbose", action="store_true",
                    help="Print resolved IDs and denial markers to stderr")
    return ap


def _run_diff_mode(
    resolved: list[tuple[str, str, str | None]],
    state_con,
    track_con,
    tools: str,
    fmt: str,
    out_dir: Path,
) -> int:
    """Diff mode: compare exactly two chats. Returns rc (0 ok, 1 on error)."""
    if len(resolved) != 2:
        print("ERROR: --diff requires exactly two IDs", file=sys.stderr)
        return 1
    chat_a, input_a, source_a = resolved[0]
    chat_b, input_b, source_b = resolved[1]

    print(f"[chat-report] diff: {chat_a[:12]}… vs {chat_b[:12]}…")
    report_a = build_report(chat_a, state_con, track_con, tools, input_a, source_a)
    report_b = build_report(chat_b, state_con, track_con, tools, input_b, source_b)
    if report_a is None:
        print(f"  ERROR — no data for chat {chat_a}", file=sys.stderr)
        return 1
    if report_b is None:
        print(f"  ERROR — no data for chat {chat_b}", file=sys.stderr)
        return 1

    report_a.pop("_orderedRaw", None)
    report_b.pop("_orderedRaw", None)

    diff = build_diff_report(report_a, report_b, tools)
    stem = f"diff-{chat_a[:12]}-vs-{chat_b[:12]}"
    md_path = out_dir / f"{stem}.md"
    json_path = out_dir / f"{stem}.json"

    if fmt in ("md", "both"):
        write_diff_md(md_path, diff, report_a, report_b, tools)
        print(f"  wrote {md_path}")
    if fmt in ("json", "both"):
        write_json_report(json_path, diff)
        print(f"  wrote {json_path}")
    return 0


def _run_aggregate_mode(
    resolved: list[tuple[str, str, str | None]],
    state_con,
    track_con,
    tools: str,
    fmt: str,
    out_dir: Path,
) -> int:
    """Aggregate mode: rollup across N chats. Returns rc (0 ok, 1 if any
    chat skipped or no valid chats)."""
    if len(resolved) < 1:
        print("ERROR: --aggregate requires at least one ID", file=sys.stderr)
        return 1

    print(f"[chat-report] aggregate: {len(resolved)} chats")
    rc = 0
    reports: list[dict] = []
    for chat_id, input_id, source in resolved:
        rpt = build_report(chat_id, state_con, track_con, tools, input_id, source)
        if rpt is None:
            print(f"  SKIP — no data for chat {chat_id}", file=sys.stderr)
            rc = 1
            continue
        rpt.pop("_orderedRaw", None)
        reports.append(rpt)

    if not reports:
        print("ERROR: no valid chats to aggregate", file=sys.stderr)
        return 1

    agg = build_aggregate_report(reports, tools)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = f"aggregate-{ts}"
    md_path = out_dir / f"{stem}.md"
    json_path = out_dir / f"{stem}.json"

    if fmt in ("md", "both"):
        write_aggregate_md(md_path, agg)
        print(f"  wrote {md_path}")
    if fmt in ("json", "both"):
        write_json_report(json_path, agg)
        print(f"  wrote {json_path}")

    print(f"  chats={agg['chatCount']} tools={len(agg['perToolRollup'])} "
          f"failures={len(agg['failureModeFrequency'])}")
    return rc


def _run_single_mode(
    resolved: list[tuple[str, str, str | None]],
    state_con,
    track_con,
    tools: str,
    fmt: str,
    out_dir: Path,
    dump_bubbles: bool,
) -> int:
    """Single-chat mode (default). Returns rc (0 ok, 1 if any chat skipped)."""
    rc = 0
    for chat_id, input_id, source in resolved:
        print(f"[chat-report] {chat_id}")
        report = build_report(chat_id, state_con, track_con, tools, input_id, source)
        if report is None:
            print(f"  SKIP — no composerData row found", file=sys.stderr)
            rc = 1
            continue
        ordered_raw = report.pop("_orderedRaw", None)
        report.pop("_perToolRollup", None)

        md_path = out_dir / f"{chat_id}.md"
        json_path = out_dir / f"{chat_id}.json"

        if fmt in ("md", "both"):
            write_md_report(md_path, report, tools)
            print(f"  wrote {md_path}")
        if fmt in ("json", "both"):
            write_json_report(json_path, report)
            print(f"  wrote {json_path}")
        if dump_bubbles and ordered_raw is not None:
            dump_path = out_dir / f"{chat_id}.bubbles.jsonl"
            write_bubble_dump(dump_path, ordered_raw)
            print(f"  wrote {dump_path}")

        s = report["summary"]
        print(
            f"  bubbles={s['bubbleCount']} tools={s['toolCallsIncluded']} "
            f"errors={s['errorCount']} denials={s['denialCount']}"
        )
    return rc


def _resolve_input_ids(
    mode_ids: list[str],
    forced: list[tuple[str, str]],
    state_con,
    track_con,
    verbose: bool,
) -> tuple[list[tuple[str, str, str | None]], int]:
    """Resolve all input IDs to (chatId, inputId, resolvedFrom) tuples.

    Returns (resolved, rc). rc is non-zero if any resolution failed; on
    failure, `resolved` may be partial and the caller should bail.
    """
    resolved: list[tuple[str, str, str | None]] = []
    for uuid_val in mode_ids:
        try:
            chat_id, source = resolve_chat_id(uuid_val, state_con, track_con,
                                              verbose=verbose)
            resolved.append((chat_id, uuid_val, source if uuid_val != chat_id else None))
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return resolved, 1
    for uuid_val, force in forced:
        try:
            chat_id, source = resolve_chat_id(uuid_val, state_con, track_con,
                                              force=force, verbose=verbose)
            resolved.append((chat_id, uuid_val, source if uuid_val != chat_id else None))
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return resolved, 1
    return resolved, 0


def main(argv: list[str] | None = None) -> int:
    ap = _build_argparser()
    args = ap.parse_args(argv)

    if args.diff:
        mode = "diff"
        mode_ids = list(args.diff)
    elif args.aggregate:
        mode = "aggregate"
        mode_ids = list(args.aggregate)
    else:
        mode = "single"
        mode_ids = list(args.ids or [])

    forced: list[tuple[str, str]] = []
    for rid in (args.request_ids or []):
        forced.append((rid, "request-id"))
    for cid in (args.chat_ids or []):
        forced.append((cid, "chat-id"))

    all_input_ids = mode_ids + [uuid_val for uuid_val, _ in forced]

    if not all_input_ids:
        ap.error("No IDs provided. Supply UUIDs as positional arguments or via --diff/--aggregate/--request-id/--chat-id.")

    if mode == "single" and mode_ids and forced:
        print("  warning: both positional IDs and --request-id/--chat-id flags provided; "
              "all will be processed", file=sys.stderr)

    global HOOK_DENIAL_MARKERS
    HOOK_DENIAL_MARKERS = _load_denial_markers(
        workspace=args.workspace,
        extra_markers=args.denial_markers,
        verbose=args.verbose,
    )

    state_db = args.state_db or _default_state_db()
    tracking_db = args.tracking_db or _default_tracking_db()
    out_dir: Path = args.out

    try:
        state_con = open_ro(state_db)
    except Exception as e:
        print(f"ERROR opening state DB {state_db}: {e}", file=sys.stderr)
        return 2
    try:
        track_con = open_ro(tracking_db)
    except Exception as e:
        print(f"ERROR opening tracking DB {tracking_db}: {e}", file=sys.stderr)
        state_con.close()
        return 2

    _resolve_cache.clear()

    try:
        resolved, resolve_rc = _resolve_input_ids(
            mode_ids, forced, state_con, track_con, args.verbose,
        )
        if resolve_rc != 0:
            return resolve_rc

        if mode == "diff":
            return _run_diff_mode(
                resolved, state_con, track_con, args.tools, args.format, out_dir,
            )
        if mode == "aggregate":
            return _run_aggregate_mode(
                resolved, state_con, track_con, args.tools, args.format, out_dir,
            )
        return _run_single_mode(
            resolved, state_con, track_con, args.tools, args.format, out_dir,
            args.dump_bubbles,
        )
    finally:
        state_con.close()
        track_con.close()


if __name__ == "__main__":
    raise SystemExit(main())
