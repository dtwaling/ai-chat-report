"""Read-only Cursor SQLite extraction helpers."""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from chat_report.core.adapter import StoreAccessError
from chat_report.core.ir import AttributionIR, ThinkingIR, TokenBreakdownIR


def _int_num(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _sqlite_ro_uri(db_path: Path) -> str:
    raw_str = str(db_path)
    if raw_str.startswith("\\\\"):
        forward = raw_str.lstrip("\\").replace("\\", "/")
        return f"file:////{quote(forward, safe='/:@')}?mode=ro"
    abs_path = db_path.absolute()
    abs_str = str(abs_path)
    if abs_str.startswith("\\\\"):
        forward = abs_str.lstrip("\\").replace("\\", "/")
        return f"file:////{quote(forward, safe='/:@')}?mode=ro"
    uri = abs_path.as_uri()
    parsed = urlsplit(uri)
    if parsed.netloc:
        forward = f"{parsed.netloc}{parsed.path}"
        return f"file:////{forward.lstrip('/')}?mode=ro"
    return f"{uri}?mode=ro"


def open_ro(path: Path) -> sqlite3.Connection:
    """Open an existing SQLite database in immutable read-only URI mode."""
    db_path = Path(path)
    if not db_path.is_file():
        raise StoreAccessError(f"Cursor database is missing or unreadable: {db_path}")
    con: sqlite3.Connection | None = None
    try:
        con = sqlite3.connect(_sqlite_ro_uri(db_path), uri=True)
        con.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        return con
    except sqlite3.Error as exc:
        if con is not None:
            con.close()
        raise StoreAccessError(f"Cannot open Cursor database read-only: {db_path}") from exc


def decode_json(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def fetch_kv_json(con: sqlite3.Connection, key: str) -> Any:
    row = con.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (key,)).fetchone()
    return decode_json(row[0]) if row else None


def fetch_composer_data(con: sqlite3.Connection, chat_id: str) -> dict[str, Any]:
    value = fetch_kv_json(con, f"composerData:{chat_id}")
    return value if isinstance(value, dict) else {}


def fetch_ordered_headers(
    con: sqlite3.Connection, chat_id: str, composer_data: dict[str, Any]
) -> list[dict[str, Any]]:
    """Load inline headers or all ordered composerConvHeaders chunks."""
    headers = composer_data.get("fullConversationHeadersOnly")
    if isinstance(headers, list) and headers:
        return [item for item in headers if isinstance(item, dict)]
    try:
        rows = con.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key >= ? AND key < ? ORDER BY key ASC",
            (f"composerConvHeaders:{chat_id}:", f"composerConvHeaders:{chat_id};"),
        ).fetchall()
    except sqlite3.Error:
        return []
    result: list[dict[str, Any]] = []
    for _, value in rows:
        chunk = decode_json(value)
        if isinstance(chunk, list):
            result.extend(item for item in chunk if isinstance(item, dict))
    return result


def _created_at_ms(bubble: dict[str, Any]) -> int | None:
    value = _int_num(bubble.get("createdAtMs"))
    if value is not None:
        return value
    raw = bubble.get("createdAt")
    num_raw = _int_num(raw)
    if num_raw is not None:
        return num_raw
    if isinstance(raw, str):
        try:
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return int(parsed.timestamp() * 1000)
        except ValueError:
            return None
    return None


def bubble_timestamps(bubble: dict[str, Any]) -> tuple[int | None, int | None]:
    """Return bubble start/end milliseconds, falling back to its ISO creation time."""
    created_at_ms = _created_at_ms(bubble)
    started_at_ms = _int_num(bubble.get("startedAtMs"))
    completed_at_ms = _int_num(bubble.get("completedAtMs"))
    start = started_at_ms if started_at_ms is not None else created_at_ms
    end = completed_at_ms if completed_at_ms is not None else created_at_ms
    return start, end


def order_bubbles(
    headers: list[dict[str, Any]],
    bubbles_by_id: dict[str, dict[str, Any]],
    include_orphans: bool = False,
) -> tuple[list[tuple[str, dict[str, Any]]], int, list[dict[str, str]]]:
    """Order bubbles by canonical header sequence, excluding unreferenced bubbles."""
    warnings: list[dict[str, str]] = []
    if not headers:
        ordered = sorted(
            bubbles_by_id.items(),
            key=lambda item: (bubble_timestamps(item[1])[0] or 0, item[0]),
        )
        warnings.append({
            "code": "cursor-header-order-missing-sorted-by-timestamp",
            "message": "Conversation headers were absent; bubbles were sorted by timestamp.",
        })
        return ordered, 0, warnings
    seen: set[str] = set()
    ordered: list[tuple[str, dict[str, Any]]] = []
    for header in headers:
        bubble_id = header.get("bubbleId")
        if isinstance(bubble_id, str) and bubble_id in bubbles_by_id and bubble_id not in seen:
            ordered.append((bubble_id, bubbles_by_id[bubble_id]))
            seen.add(bubble_id)
    orphan_count = len(bubbles_by_id) - len(seen)
    if include_orphans:
        ordered.extend((bid, bubble) for bid, bubble in bubbles_by_id.items() if bid not in seen)
    return ordered, orphan_count, warnings


def fetch_bubbles(con: sqlite3.Connection, chat_id: str) -> dict[str, dict[str, Any]]:
    rows = con.execute(
        "SELECT key, value FROM cursorDiskKV WHERE key >= ? AND key < ? ORDER BY key ASC",
        (f"bubbleId:{chat_id}:", f"bubbleId:{chat_id};"),
    ).fetchall()
    prefix = f"bubbleId:{chat_id}:"
    result: dict[str, dict[str, Any]] = {}
    for key, value in rows:
        bubble = decode_json(value)
        bubble_id = str(key)[len(prefix):]
        if isinstance(bubble, dict) and bubble_id:
            result[bubble_id] = bubble
    return result


def extract_thinking(bubble_id: str, bubble: dict[str, Any]) -> list[ThinkingIR]:
    blocks: list[ThinkingIR] = []
    seen_texts: set[str] = set()
    thinking = bubble.get("thinking")
    candidates: list[Any] = [thinking] if thinking else []
    legacy = bubble.get("allThinkingBlocks")
    if isinstance(legacy, list):
        candidates.extend(legacy)
    elif legacy:
        candidates.append(legacy)
    for raw_candidate in candidates:
        candidate = decode_json(raw_candidate)
        text = candidate.get("text") if isinstance(candidate, dict) else candidate
        if isinstance(text, str) and text.strip():
            cleaned = text.strip()
            if cleaned in seen_texts:
                continue
            seen_texts.add(cleaned)
            started_at_ms, completed_at_ms = bubble_timestamps(bubble)
            blocks.append(ThinkingIR(
                bubble_id=bubble_id, text=cleaned,
                duration_ms=_int_num(bubble.get("thinkingDurationMs")),
                started_at_ms=started_at_ms,
                completed_at_ms=completed_at_ms,
            ))
    return blocks


def extract_token_counts(bubble: dict[str, Any]) -> tuple[int | None, int | None]:
    token_count = bubble.get("tokenCount")
    if isinstance(token_count, dict):
        inp, out = token_count.get("inputTokens"), token_count.get("outputTokens")
        return (_int_num(inp), _int_num(out))
    return (None, token_count) if isinstance(token_count, int) and not isinstance(token_count, bool) else (None, None)


def extract_token_breakdown(composer_data: dict[str, Any]) -> TokenBreakdownIR:
    raw_categories = composer_data.get("promptTokenBreakdown")
    categories: dict[str, dict[str, int]] = {}
    fallback_used: int | None = None
    fallback_limit: int | None = None
    if isinstance(raw_categories, dict):
        fallback_used = _int_num(raw_categories.get("totalUsedTokens"))
        fallback_limit = _int_num(raw_categories.get("maxTokens"))
        cat_list = raw_categories.get("categories")
        if isinstance(cat_list, list):
            for item in cat_list:
                if isinstance(item, dict):
                    cid = item.get("id") or item.get("label")
                    if isinstance(cid, str) and cid:
                        tokens = _int_num(item.get("estimatedTokens"))
                        if tokens is None:
                            tokens = _int_num(item.get("tokens"))
                        chars = _int_num(item.get("characterCount"))
                        if chars is None:
                            chars = _int_num(item.get("chars"))
                        categories[cid] = {
                            "tokens": tokens if tokens is not None else 0,
                            "chars": chars if chars is not None else 0,
                        }
        else:
            for name, raw in raw_categories.items():
                if isinstance(raw, dict):
                    entry: dict[str, int] = {}
                    tokens = _int_num(raw.get("tokens"))
                    if tokens is None:
                        tokens = _int_num(raw.get("estimatedTokens"))
                    chars = _int_num(raw.get("chars"))
                    if chars is None:
                        chars = _int_num(raw.get("characterCount"))
                    if tokens is not None:
                        entry["tokens"] = tokens
                    if chars is not None:
                        entry["chars"] = chars
                    categories[str(name)] = entry
    tree = composer_data.get("promptContextUsageTree")
    if isinstance(tree, list):
        top_nodes = [node for node in tree if isinstance(node, dict)]
    elif isinstance(tree, dict) and isinstance(tree.get("nodes"), list):
        top_nodes = [node for node in tree["nodes"] if isinstance(node, dict)]
    else:
        top_nodes = []
    total_used = _int_num(composer_data.get("contextTokensUsed"))
    if total_used is None:
        total_used = fallback_used
    token_limit = _int_num(composer_data.get("contextTokenLimit"))
    if token_limit is None:
        token_limit = fallback_limit
    usage_pct = composer_data.get("contextUsagePercent")
    return TokenBreakdownIR(
        total_context_tokens_used=total_used,
        context_token_limit=token_limit,
        context_usage_percent=float(usage_pct) if isinstance(usage_pct, (int, float)) and not isinstance(usage_pct, bool) else None,
        by_category=categories,
        top_context_nodes=top_nodes,
    )


def fetch_offloaded_content(con: sqlite3.Connection, content_id: str | None) -> str | None:
    if not content_id:
        return None
    normalized = content_id
    if normalized.startswith("ofsContent:"):
        key = normalized
    else:
        if not normalized.startswith("composer.content."):
            normalized = f"composer.content.{normalized}"
        key = f"ofsContent:{normalized}"
    value = fetch_kv_json(con, key)
    return value if isinstance(value, str) else None


def fetch_attribution(
    track_con: sqlite3.Connection, chat_id: str, window_ms: tuple[int, int] | None,
    warnings: list[dict[str, str]] | None = None,
) -> AttributionIR:
    rows = track_con.execute(
        "SELECT source, COUNT(*) FROM ai_code_hashes WHERE conversationId = ? GROUP BY source", (chat_id,)
    ).fetchall()
    counts = {str(source): int(count) for source, count in rows}
    columns = {str(row[1]) for row in track_con.execute("PRAGMA table_info(ai_code_hashes)").fetchall()}
    files: list[str] = []
    if "fileName" in columns:
        raw_files = [
            re.sub(r"^/([A-Za-z]:/)", r"\1", str(row[0]).replace("\\", "/"))
            for row in track_con.execute(
                "SELECT DISTINCT fileName FROM ai_code_hashes WHERE conversationId = ? AND fileName IS NOT NULL ORDER BY fileName ASC",
                (chat_id,),
            ).fetchall()
        ]
        files = list(dict.fromkeys(raw_files))
    elif warnings is not None:
        warnings.append({"code": "cursor-tracking-filename-column-missing", "message": "ai_code_hashes has no fileName column; distinct_files_touched set to []."})
    commits: list[dict[str, Any]] = []
    tables = {str(row[0]) for row in track_con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if window_ms is not None and "scored_commits" in tables:
        start, end = window_ms
        columns = [str(row[1]) for row in track_con.execute("PRAGMA table_info(scored_commits)").fetchall()]
        for row in track_con.execute(
            "SELECT * FROM scored_commits WHERE scoredAt BETWEEN ? AND ? ORDER BY scoredAt ASC",
            (start - 60000, end + 300000),
        ).fetchall():
            commits.append(dict(zip(columns, row)))
    return AttributionIR(counts.get("composer", 0), counts.get("human", 0), files, commits)
