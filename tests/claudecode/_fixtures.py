"""Synthetic JSONL record builders for claudecode tests.

These mirror the empirical Claude Code record shape (DISCOVERY.md Q2) at
the minimum fidelity each test needs. The envelope fields the parser
cares about (``type``, ``message``, ``uuid``, ``timestamp``, ``sessionId``,
``version``, ``cwd``, ``gitBranch``, ``isSidechain``) are populated with
deterministic defaults; tests override specific fields when relevant.
"""

from __future__ import annotations

import json
import uuid as _uuid
from pathlib import Path
from typing import Any


_DEFAULT_SESSION = "d5093750-dd1a-4a15-adae-79334235a2e7"
_DEFAULT_CWD = r"C:\_Source\ai-chat-report"
_DEFAULT_VERSION = "2.1.140"
_DEFAULT_GIT_BRANCH = "claudecode-variant-build"
_DEFAULT_TS = "2026-05-13T02:00:00.000Z"


def _envelope(**overrides: Any) -> dict[str, Any]:
    env = {
        "uuid": str(_uuid.uuid4()),
        "parentUuid": None,
        "isSidechain": False,
        "timestamp": _DEFAULT_TS,
        "userType": "external",
        "entrypoint": "cli",
        "cwd": _DEFAULT_CWD,
        "sessionId": _DEFAULT_SESSION,
        "version": _DEFAULT_VERSION,
        "gitBranch": _DEFAULT_GIT_BRANCH,
    }
    env.update(overrides)
    return env


def user_typed_string(text: str, **overrides: Any) -> dict[str, Any]:
    """User record with content as a plain string (the common case)."""
    return _envelope(
        type="user",
        message={"role": "user", "content": text},
        **overrides,
    )


def user_typed_blocks(*blocks: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """User record with content as a list of blocks (image/paste/etc.).

    Caller passes the inner content blocks directly (e.g., a text block).
    None should contain a ``tool_result``-type block; that's a tool_result
    echo, not a user-typed prompt.
    """
    return _envelope(
        type="user",
        message={"role": "user", "content": list(blocks)},
        **overrides,
    )


def user_tool_result(
    tool_use_id: str,
    content: Any,
    *,
    is_error: bool | None = None,
    tool_use_result: Any = None,
    source_uuid: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """User record echoing a tool_result for a prior assistant tool_use."""
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
    }
    if is_error is not None:
        block["is_error"] = is_error
    rec = _envelope(
        type="user",
        message={"role": "user", "content": [block]},
        **overrides,
    )
    if tool_use_result is not None:
        rec["toolUseResult"] = tool_use_result
    if source_uuid is not None:
        rec["sourceToolAssistantUUID"] = source_uuid
    return rec


def assistant_text(text: str, **overrides: Any) -> dict[str, Any]:
    """Assistant record with a single text content block."""
    return _envelope(
        type="assistant",
        message={
            "model": "claude-opus-4-7",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
        **overrides,
    )


def assistant_tool_use(
    tool_name: str,
    *,
    tool_input: dict[str, Any] | None = None,
    tool_use_id: str | None = None,
    caller: dict[str, Any] | None = None,
    text_prefix: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Assistant record with a tool_use block (and optional preceding text)."""
    content: list[dict[str, Any]] = []
    if text_prefix is not None:
        content.append({"type": "text", "text": text_prefix})
    block = {
        "type": "tool_use",
        "id": tool_use_id or f"toolu_{_uuid.uuid4().hex[:24]}",
        "name": tool_name,
        "input": tool_input if tool_input is not None else {},
        "caller": caller if caller is not None else {"type": "direct"},
    }
    content.append(block)
    rec = _envelope(
        type="assistant",
        message={
            "model": "claude-opus-4-7",
            "role": "assistant",
            "content": content,
        },
        **overrides,
    )
    return rec


def last_prompt(leaf_uuid: str, *, last_prompt_text: str | None = None,
                **overrides: Any) -> dict[str, Any]:
    """``last-prompt`` turn-boundary marker. Carries minimal payload."""
    out: dict[str, Any] = {
        "type": "last-prompt",
        "leafUuid": leaf_uuid,
        "sessionId": overrides.get("sessionId", _DEFAULT_SESSION),
    }
    if last_prompt_text is not None:
        out["lastPrompt"] = last_prompt_text
    return out


def attachment(payload: str = "system context", **overrides: Any) -> dict[str, Any]:
    """``attachment`` system-injected context record. Not load-bearing."""
    return _envelope(
        type="attachment",
        message={"content": payload},
        **overrides,
    )


def sidechain_record(record: dict[str, Any]) -> dict[str, Any]:
    """Mark a record as ``isSidechain: true`` (subagent-internal)."""
    record["isSidechain"] = True
    return record


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    """Serialize records to a JSONL file. One JSON object per line."""
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r))
            f.write("\n")
