"""Synthetic bubble + meta builders for cursor-variant tests.

Tests construct in-memory fixtures here rather than spinning up SQLite, so the
locked-shape emitter can be exercised in isolation from Cursor's state.vscdb.
"""

from __future__ import annotations

from typing import Any


# Reference timestamps used across fixtures. UTC, ms since epoch.
SESSION_START_MS = 1_780_000_000_000  # 2026-05-29T04:26:40Z
SESSION_END_MS = 1_780_000_300_000    # 2026-05-29T04:31:40Z (5 minutes later)


def make_meta(
    chat_id: str = "f9a48f94-b698-455e-946d-9734c86babe9",
    *,
    name: str = "Test chat",
    created_at_ms: int = SESSION_START_MS,
    last_updated_at_ms: int = SESSION_END_MS,
    model_name: str = "claude-opus-4-7",
    mode: str = "agent",
) -> dict[str, Any]:
    """Build a minimal but realistic ``meta`` dict matching ``fetch_chat_meta``'s shape."""
    return {
        "composerId": chat_id,
        "name": name,
        "subtitle": None,
        "createdAt": created_at_ms,
        "lastUpdatedAt": last_updated_at_ms,
        "mode": mode,
        "unifiedMode": None,
        "forceMode": None,
        "isAgentic": True,
        "status": "completed",
        "contextTokensUsed": 12_345,
        "contextTokenLimit": 200_000,
        "contextUsagePercent": 6.2,
        "totalLinesAdded": 0,
        "totalLinesRemoved": 0,
        "stopHookLoopCount": 0,
        "modelName": model_name,
        "addedFiles": [],
        "removedFiles": [],
        "todos": [],
        "headerOrder": [],
    }


def make_user_bubble(bubble_id: str, *, text: str = "what's up", request_id: str | None = None) -> dict[str, Any]:
    """Build a synthetic Cursor user bubble payload (``type == 1``)."""
    return {
        "type": 1,
        "text": text,
        "requestId": request_id,
        "tokenCount": None,
        "toolFormerData": None,
        "allThinkingBlocks": [],
        "lints": [],
        "gitDiffs": None,
        "attachedCodeChunks": [],
        "recentlyViewedFiles": [],
    }


def make_assistant_text_bubble(bubble_id: str, *, text: str = "Let me look.") -> dict[str, Any]:
    """Build a synthetic assistant text-only bubble (``type == 2``, no tool call)."""
    return {
        "type": 2,
        "text": text,
        "requestId": "",
        "tokenCount": None,
        "toolFormerData": None,
        "allThinkingBlocks": [],
        "lints": [],
        "gitDiffs": None,
        "attachedCodeChunks": [],
        "recentlyViewedFiles": [],
    }


def make_tool_call_bubble(
    bubble_id: str,
    *,
    tool_name: str,
    params: dict[str, Any] | None = None,
    result_text: str = "ok",
    status: str = "completed",
    is_denial_marker: str | None = None,
) -> dict[str, Any]:
    """Build a synthetic assistant tool-call bubble.

    ``params`` mirrors Cursor's ``toolFormerData.params``. The unwrap_mcp_payload
    helper handles both raw dicts and nested envelopes; tests use raw dicts.

    ``is_denial_marker``: if set, the result text contains the marker so the
    denial-detection path fires.
    """
    payload_text = result_text if is_denial_marker is None else (
        f"{result_text}\n\n[hook] {is_denial_marker}"
    )
    return {
        "type": 2,
        "text": "",
        "requestId": "",
        "tokenCount": None,
        "toolFormerData": {
            "toolCallId": f"tcid-{bubble_id}",
            "modelCallId": f"mcid-{bubble_id}",
            "toolIndex": 0,
            "name": tool_name,
            "tool": tool_name,
            "status": status,
            "params": params or {},
            "result": payload_text,
        },
        "allThinkingBlocks": [],
        "lints": [],
        "gitDiffs": None,
        "attachedCodeChunks": [],
        "recentlyViewedFiles": [],
    }


def make_ordered(*bubbles: tuple[str, dict[str, Any]]) -> list[tuple[int, str, dict[str, Any]]]:
    """Wrap a sequence of (bubble_id, payload) pairs in the ``(index, id, payload)`` tuple shape that ``order_bubbles`` returns."""
    return [(idx, bid, payload) for idx, (bid, payload) in enumerate(bubbles)]
