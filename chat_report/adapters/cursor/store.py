"""Read-only Cursor SQLite extraction helpers."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

from chat_report.core.adapter import StoreAccessError
from chat_report.core.ir import AttributionIR, ThinkingIR, TokenBreakdownIR


def open_ro(path: Path) -> sqlite3.Connection:
    """Open an existing SQLite database in immutable read-only URI mode."""
    db_path = Path(path)
    if not db_path.is_file():
        raise StoreAccessError(f"Cursor database is missing or unreadable: {db_path}")
    con: sqlite3.Connection | None = None
    try:
        con = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
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
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE ? ORDER BY key ASC",
            (f"composerConvHeaders:{chat_id}:%",),
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
    value = bubble.get("createdAtMs")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raw = bubble.get("createdAt")
    if isinstance(raw, str):
        try:
            return int(dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return None
    return None


def bubble_timestamps(bubble: dict[str, Any]) -> tuple[int | None, int | None]:
    """Return bubble start/end milliseconds, falling back to its ISO creation time."""
    created_at_ms = _created_at_ms(bubble)
    started_at_ms = bubble.get("startedAtMs")
    completed_at_ms = bubble.get("completedAtMs")
    start = started_at_ms if isinstance(started_at_ms, int) and not isinstance(started_at_ms, bool) else created_at_ms
    end = completed_at_ms if isinstance(completed_at_ms, int) and not isinstance(completed_at_ms, bool) else created_at_ms
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
        "SELECT key, value FROM cursorDiskKV WHERE key LIKE ? ORDER BY key ASC",
        (f"bubbleId:{chat_id}:%",),
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
    thinking = bubble.get("thinking")
    candidates: list[Any] = [thinking] if thinking else []
    legacy = bubble.get("allThinkingBlocks")
    if isinstance(legacy, list):
        candidates.extend(legacy)
    elif legacy:
        candidates.append(legacy)
    for candidate in candidates:
        text = candidate.get("text") if isinstance(candidate, dict) else candidate
        if isinstance(text, str) and text.strip():
            started_at_ms, completed_at_ms = bubble_timestamps(bubble)
            blocks.append(ThinkingIR(
                bubble_id=bubble_id, text=text.strip(),
                duration_ms=bubble.get("thinkingDurationMs") if isinstance(bubble.get("thinkingDurationMs"), int) else None,
                started_at_ms=started_at_ms,
                completed_at_ms=completed_at_ms,
            ))
    return blocks


def extract_token_counts(bubble: dict[str, Any]) -> tuple[int | None, int | None]:
    token_count = bubble.get("tokenCount")
    if isinstance(token_count, dict):
        inp, out = token_count.get("inputTokens"), token_count.get("outputTokens")
        return (inp if isinstance(inp, int) else None, out if isinstance(out, int) else None)
    return (None, token_count) if isinstance(token_count, int) and not isinstance(token_count, bool) else (None, None)


def extract_token_breakdown(composer_data: dict[str, Any]) -> TokenBreakdownIR:
    raw_categories = composer_data.get("promptTokenBreakdown")
    categories: dict[str, dict[str, int]] = {}
    if isinstance(raw_categories, dict):
        for name, raw in raw_categories.items():
            if isinstance(raw, dict):
                categories[str(name)] = {
                    key: value for key, value in raw.items()
                    if key in ("tokens", "chars") and isinstance(value, int) and not isinstance(value, bool)
                }
    tree = composer_data.get("promptContextUsageTree")
    return TokenBreakdownIR(
        total_context_tokens_used=composer_data.get("contextTokensUsed") if isinstance(composer_data.get("contextTokensUsed"), int) else None,
        context_token_limit=composer_data.get("contextTokenLimit") if isinstance(composer_data.get("contextTokenLimit"), int) else None,
        context_usage_percent=float(composer_data["contextUsagePercent"]) if isinstance(composer_data.get("contextUsagePercent"), (int, float)) else None,
        by_category=categories,
        top_context_nodes=tree if isinstance(tree, list) else [],
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
        files = [str(row[0]) for row in track_con.execute(
            "SELECT DISTINCT fileName FROM ai_code_hashes WHERE conversationId = ? AND fileName IS NOT NULL ORDER BY fileName ASC",
            (chat_id,),
        ).fetchall()]
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
