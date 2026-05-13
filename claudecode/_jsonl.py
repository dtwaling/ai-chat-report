"""JSONL streaming parser + turn/tool-call grouping for the Claude Code store.

DISCOVERY.md spelled out the on-disk format. This module exposes:

- ``iter_records(path)`` -- a streaming generator over the session JSONL.
- ``is_user_typed_prompt(record)`` / ``extract_user_prompt_text(record)`` --
  distinguish user-typed input from tool-result echoes inside ``user`` records.
- ``collect_tool_uses(record)`` / ``collect_tool_results(record)`` -- extract
  the structured content blocks the locked-shape builder needs.
- ``partition_into_turns(records)`` -- group records into a sequence of
  ``Turn`` objects, each pairing a user-typed prompt with all the assistant
  + tool-result records that follow until the next user-typed prompt.
- ``pair_tool_calls(turn)`` -- match tool_use blocks with their tool_result
  responses by ``tool_use_id`` within a single assistant turn.

The parser is deliberately tolerant: unknown record types are ignored
(surfaced to callers via ``unknown_record_types(records)`` so the report
layer can emit a warning code), malformed lines are skipped silently, and
records that lack the metadata envelope are dropped from the iterator.
The strict assertions live in the report layer, not here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator


# Record-type names known to the format (DISCOVERY.md Q2 + cross-session
# observations + this session's empirical sample). The set is intentionally
# inclusive: a missing type isn't a parser error, just a warning.
KNOWN_RECORD_TYPES: frozenset[str] = frozenset({
    "assistant",
    "user",
    "attachment",
    "last-prompt",
    "permission-mode",
    "ai-title",
    "file-history-snapshot",
    "system",
    "queue-operation",
    "pr-link",  # observed empirically 2026-05-13; not in initial DISCOVERY.md
})


# ---------------------------------------------------------------------------
# Streaming parser
# ---------------------------------------------------------------------------


def iter_records(path: Path) -> Iterator[dict[str, Any]]:
    """Stream JSON records from a JSONL file, one record per line.

    Lines that fail to parse as JSON are skipped silently. Blank lines are
    skipped. The caller receives only well-formed JSON objects.
    """
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                yield rec


def unknown_record_types(records: Iterable[dict[str, Any]]) -> list[str]:
    """Return the sorted, de-duplicated list of record types we don't know.

    Caller (report layer) surfaces these as a warning code so a future
    additive record type doesn't silently disappear from the report.
    """
    seen: set[str] = set()
    for r in records:
        t = r.get("type")
        if isinstance(t, str) and t not in KNOWN_RECORD_TYPES:
            seen.add(t)
    return sorted(seen)


# ---------------------------------------------------------------------------
# User-typed prompt vs tool_result echo
# ---------------------------------------------------------------------------


def _user_content(record: dict[str, Any]) -> Any:
    msg = record.get("message")
    if not isinstance(msg, dict):
        return None
    return msg.get("content")


def is_user_typed_prompt(record: dict[str, Any]) -> bool:
    """True iff this ``user`` record carries user-typed text.

    Distinguishes the user's keystrokes from a tool_result echo. Two cases:

    1. ``content`` is a string -- always user-typed.
    2. ``content`` is a list -- user-typed iff NO block has
       ``type == "tool_result"``. (Even a list with text + image blocks is
       user-typed; only a tool_result presence flips the bit.)

    Records of types other than ``user`` always return False.
    """
    if record.get("type") != "user":
        return False
    content = _user_content(record)
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return False
        return True
    return False


def extract_user_prompt_text(record: dict[str, Any]) -> str:
    """Return the user-typed text from a user-prompt record.

    For string content, returns it verbatim. For list content, concatenates
    all text-typed blocks. Returns empty string when no extractable text
    exists (e.g., image-only prompts).
    """
    content = _user_content(record)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    return ""


# ---------------------------------------------------------------------------
# tool_use / tool_result extraction
# ---------------------------------------------------------------------------


def collect_tool_uses(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Return ``tool_use`` blocks from an assistant record (empty if none).

    Each block carries ``id``, ``name``, ``input``, and an optional
    ``caller`` field (DISCOVERY.md Q3.a). Non-assistant records always
    return empty.
    """
    if record.get("type") != "assistant":
        return []
    msg = record.get("message")
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content
            if isinstance(b, dict) and b.get("type") == "tool_use"]


def collect_tool_results(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Return ``tool_result`` blocks from a user record (empty if none).

    Each block carries ``tool_use_id``, ``content`` (possibly truncated --
    full payload may live in the spillover dir per DISCOVERY.md Q3.c), and
    an optional ``is_error`` flag. Non-user records always return empty.
    """
    if record.get("type") != "user":
        return []
    msg = record.get("message")
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content
            if isinstance(b, dict) and b.get("type") == "tool_result"]


# ---------------------------------------------------------------------------
# Turn grouping
# ---------------------------------------------------------------------------


@dataclass
class Turn:
    """A user prompt + the assistant work that followed.

    ``user_record`` is the ``user``-type record carrying the prompt text.
    ``assistant_records`` are all ``assistant``-type records belonging to
    this turn, in file order. ``tool_result_records`` are the user-type
    records that follow with ``tool_result`` content blocks (echoes of
    tool outputs). ``trailing_records`` is the catch-all for any other
    record type that appeared between this prompt and the next.
    """
    index: int
    user_record: dict[str, Any] | None
    assistant_records: list[dict[str, Any]] = field(default_factory=list)
    tool_result_records: list[dict[str, Any]] = field(default_factory=list)
    trailing_records: list[dict[str, Any]] = field(default_factory=list)


def partition_into_turns(records: Iterable[dict[str, Any]]) -> list[Turn]:
    """Group records into a sequence of Turn objects in file order.

    A new turn begins each time a user-typed prompt is encountered. The
    turn carries the user record itself plus every subsequent record up to
    (but not including) the next user-typed prompt. Records appearing
    before any user-typed prompt land in an initial turn with
    ``user_record=None`` -- a real session never starts that way but the
    parser must tolerate it (e.g., truncated JSONL).

    Subagent records (``isSidechain: true``) are silently dropped here --
    they are not part of the primary session's turn sequence. The subagent
    rollup field (commit 4+ of B.3) handles them separately by reading
    the per-subagent JSONLs from the ``subagents/`` directory.
    """
    turns: list[Turn] = []
    current = Turn(index=0, user_record=None)
    for rec in records:
        if rec.get("isSidechain"):
            continue
        rec_type = rec.get("type")
        if rec_type == "user" and is_user_typed_prompt(rec):
            # New user-typed prompt starts a new turn. If the previous
            # turn carries any data, flush it before resetting.
            if (current.user_record is not None
                    or current.assistant_records
                    or current.tool_result_records
                    or current.trailing_records):
                turns.append(current)
                current = Turn(index=len(turns), user_record=None)
            current.user_record = rec
            continue
        if rec_type == "assistant":
            current.assistant_records.append(rec)
        elif rec_type == "user":
            # User record that is NOT a user-typed prompt is a tool_result echo.
            current.tool_result_records.append(rec)
        else:
            # last-prompt, attachment, permission-mode, ai-title, system,
            # queue-operation, file-history-snapshot, pr-link, plus any
            # unknown additive type. Keep around for warnings / metadata.
            current.trailing_records.append(rec)

    # Flush the final turn if it carries any payload.
    if (current.user_record is not None
            or current.assistant_records
            or current.tool_result_records
            or current.trailing_records):
        turns.append(current)
    return turns


# ---------------------------------------------------------------------------
# Pairing tool_use with tool_result inside a turn
# ---------------------------------------------------------------------------


@dataclass
class PairedToolCall:
    """A tool_use linked to its tool_result, with both source records.

    ``tool_use_block`` carries ``id``, ``name``, ``input``, ``caller``.
    ``tool_result_block`` may be ``None`` when no matching result was
    observed (e.g., session interrupted mid-tool-call). ``assistant_record``
    and ``result_record`` are the envelope records for timestamps + the
    ``toolUseResult`` sidecar.
    """
    tool_use_block: dict[str, Any]
    tool_result_block: dict[str, Any] | None
    assistant_record: dict[str, Any]
    result_record: dict[str, Any] | None


def pair_tool_calls(turn: Turn) -> list[PairedToolCall]:
    """Match tool_use blocks with their tool_result responses by id.

    Walks ``assistant_records`` in order to preserve call sequence; for each
    ``tool_use`` block, looks up the first ``tool_result`` block in
    ``tool_result_records`` whose ``tool_use_id`` matches. Returns the
    results in call order (assistant-record order, then within-record
    block order).
    """
    # Build the lookup once: tool_use_id -> (block, record).
    result_index: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for rec in turn.tool_result_records:
        for block in collect_tool_results(rec):
            tuid = block.get("tool_use_id")
            if isinstance(tuid, str) and tuid not in result_index:
                # First match wins -- the inline result is what landed.
                result_index[tuid] = (block, rec)

    paired: list[PairedToolCall] = []
    for arec in turn.assistant_records:
        for ublock in collect_tool_uses(arec):
            tuid = ublock.get("id")
            if isinstance(tuid, str) and tuid in result_index:
                rblock, rrec = result_index[tuid]
                paired.append(PairedToolCall(
                    tool_use_block=ublock,
                    tool_result_block=rblock,
                    assistant_record=arec,
                    result_record=rrec,
                ))
            else:
                paired.append(PairedToolCall(
                    tool_use_block=ublock,
                    tool_result_block=None,
                    assistant_record=arec,
                    result_record=None,
                ))
    return paired
