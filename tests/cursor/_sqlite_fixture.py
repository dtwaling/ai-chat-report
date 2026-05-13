"""Synthetic Cursor SQLite fixtures for end-to-end contract conformance tests.

Cursor's chat history lives in two SQLite DBs:

  - ``state.vscdb`` -- a ``cursorDiskKV`` table keyed by ``composerData:<chatId>``
    (chat meta blob) and ``bubbleId:<chatId>:<bubbleId>`` (per-turn blob).
  - ``ai-code-tracking.db`` -- attribution tables (``ai_code_hashes``,
    ``ai_deleted_files``, ``scored_commits``).

This module builds minimal but realistic instances of both, sufficient to drive
the full ``chat-report.py`` pipeline (open_ro -> fetch_chat_meta -> fetch_bubbles
-> order_bubbles -> build_bubble_rows -> build_locked_report -> write_json_report).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any


def _new_bubble_id() -> str:
    return str(uuid.uuid4())


def write_state_vscdb(
    path: Path,
    chat_id: str,
    bubbles: list[dict[str, Any]],
    *,
    meta_overrides: dict[str, Any] | None = None,
) -> None:
    """Create a ``state.vscdb`` with one chat + its bubbles.

    ``bubbles`` is a list of bubble payload dicts (as produced by
    ``tests/cursor/_fixtures.py`` make_*_bubble helpers). Each gets a fresh
    bubble_id and is wired into ``meta.fullConversationHeadersOnly`` so
    ``order_bubbles`` recovers the original sequence.
    """
    header_order: list[dict[str, str]] = []
    bubble_rows: list[tuple[str, str]] = []
    for b in bubbles:
        bid = _new_bubble_id()
        header_order.append({"bubbleId": bid})
        bubble_rows.append(
            (f"bubbleId:{chat_id}:{bid}", json.dumps(b))
        )

    meta = {
        "composerId": chat_id,
        "name": "synthetic-fixture",
        "createdAt": 1_780_000_000_000,
        "lastUpdatedAt": 1_780_000_300_000,
        "mode": "agent",
        "isAgentic": True,
        "status": "completed",
        "contextTokensUsed": 12345,
        "contextTokenLimit": 200_000,
        "contextUsagePercent": 6.2,
        "totalLinesAdded": 0,
        "totalLinesRemoved": 0,
        "stopHookLoopCount": 0,
        "modelConfig": {"modelName": "claude-opus-4-7"},
        "addedFiles": [],
        "removedFiles": [],
        "todos": [],
        "fullConversationHeadersOnly": header_order,
    }
    if meta_overrides:
        meta.update(meta_overrides)

    con = sqlite3.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)"
        )
        con.execute(
            "INSERT OR REPLACE INTO cursorDiskKV(key, value) VALUES (?, ?)",
            (f"composerData:{chat_id}", json.dumps(meta).encode("utf-8")),
        )
        for key, blob in bubble_rows:
            con.execute(
                "INSERT OR REPLACE INTO cursorDiskKV(key, value) VALUES (?, ?)",
                (key, blob.encode("utf-8")),
            )
        con.commit()
    finally:
        con.close()


def write_empty_tracking_db(path: Path) -> None:
    """Create a minimal ``ai-code-tracking.db`` with empty attribution tables.

    The chat-report code is defensive (swallows ``sqlite3.Error`` on missing
    tables) but exercising the happy-path schema is still cleaner.
    """
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS ai_code_hashes ("
            "  conversationId TEXT, requestId TEXT, source TEXT, createdAt INTEGER"
            ")"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS ai_deleted_files ("
            "  conversationId TEXT, gitPath TEXT, composerId TEXT, model TEXT, "
            "  deletedAt INTEGER"
            ")"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS scored_commits ("
            "  commitHash TEXT, branchName TEXT, scoredAt INTEGER,"
            "  linesAdded INTEGER, linesDeleted INTEGER,"
            "  composerLinesAdded INTEGER, composerLinesDeleted INTEGER,"
            "  humanLinesAdded INTEGER, humanLinesDeleted INTEGER,"
            "  tabLinesAdded INTEGER, tabLinesDeleted INTEGER,"
            "  v1AiPercentage REAL, v2AiPercentage REAL,"
            "  commitMessage TEXT, commitDate INTEGER"
            ")"
        )
        con.commit()
    finally:
        con.close()
