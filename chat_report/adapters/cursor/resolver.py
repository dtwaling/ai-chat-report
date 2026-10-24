"""Four-way Cursor session ID resolution and schema-adaptive composerHeaders listing."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from chat_report.core.adapter import SessionListEntry, SessionNotFoundError
from chat_report.core.ir import ResolvedTarget


def _decode_json_blob(value: Any) -> Any:
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


def _clean_subagent_type(value: Any) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned and cleaned.lower() not in ("unspecified", "unknown", "custom"):
            return cleaned
    return None


def _subagent_meta_from_dict(data: dict[str, Any] | None) -> tuple[bool, str | None]:
    if not isinstance(data, dict):
        return False, None
    sub_info = data.get("subagentInfo") if isinstance(data.get("subagentInfo"), dict) else {}
    parent_id = (
        (data.get("parentComposerId") if isinstance(data.get("parentComposerId"), str) and data.get("parentComposerId").strip() else None)
        or (sub_info.get("parentComposerId") if isinstance(sub_info.get("parentComposerId"), str) and sub_info.get("parentComposerId").strip() else None)
    )
    is_sub = bool(data.get("isSubagent")) or bool(parent_id) or bool(sub_info)
    sub_type = _clean_subagent_type(data.get("subagentTypeName")) or _clean_subagent_type(sub_info.get("subagentTypeName"))
    return is_sub, sub_type


def _extract_model_name(composer_data: dict[str, Any] | None) -> str | None:
    if not isinstance(composer_data, dict):
        return None
    mc = composer_data.get("modelConfig")
    if isinstance(mc, dict):
        for k in ("modelName", "model", "name"):
            v = mc.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    for k in ("modelName", "model"):
        v = composer_data.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _fetch_composer_data_dict(state_con: sqlite3.Connection, composer_id: str) -> dict[str, Any] | None:
    try:
        row = state_con.execute(
            "SELECT value FROM cursorDiskKV WHERE key = ? AND value IS NOT NULL",
            (f"composerData:{composer_id}",),
        ).fetchone()
        if not row:
            return None
        data = _decode_json_blob(row[0])
        return data if isinstance(data, dict) else None
    except sqlite3.Error:
        return None


def _lookup_chat_id(state_con: sqlite3.Connection, raw_id: str) -> bool:
    row = state_con.execute(
        "SELECT 1 FROM cursorDiskKV WHERE key = ? AND value IS NOT NULL",
        (f"composerData:{raw_id}",),
    ).fetchone()
    return bool(row)


def _lookup_request_id_in_tracking(track_con: sqlite3.Connection, raw_id: str) -> str | None:
    row = track_con.execute(
        "SELECT DISTINCT conversationId FROM ai_code_hashes WHERE requestId = ? LIMIT 1",
        (raw_id,),
    ).fetchone()
    if row and row[0]:
        return str(row[0])
    return None


def _scan_bubbles_for_request_id(state_con: sqlite3.Connection, raw_id: str) -> str | None:
    try:
        rows = state_con.execute(
            "SELECT key, value FROM cursorDiskKV "
            "WHERE key LIKE 'bubbleId:%' AND value IS NOT NULL AND CAST(value AS TEXT) LIKE ?",
            (f"%{raw_id}%",),
        )
        for key_val, blob_val in rows:
            key = str(key_val)
            parts = key.split(":")
            if len(parts) < 3:
                continue
            chat_id = parts[1]
            data = _decode_json_blob(blob_val)
            if isinstance(data, dict) and data.get("requestId") == raw_id:
                return chat_id
    except sqlite3.Error:
        return None
    return None


def resolve_chat_id(
    raw_id: str,
    state_con: sqlite3.Connection,
    track_con: sqlite3.Connection | None,
    *,
    force: str | None = None,
) -> ResolvedTarget:
    """Resolve raw_id via 4-way precedence or forced chat-id / request-id mode."""
    cleaned = (raw_id or "").strip()
    if not cleaned:
        raise SessionNotFoundError("Session/Request UUID cannot be empty.")

    if force == "chat-id":
        if _lookup_chat_id(state_con, cleaned):
            return ResolvedTarget(session_id=cleaned, input_id=cleaned, source="chat-id")
        raise SessionNotFoundError(
            f"UUID '{cleaned}' not found as chat ID (composerData:{cleaned} absent in state.vscdb)."
        )

    if force == "request-id":
        if track_con is not None:
            try:
                hit = _lookup_request_id_in_tracking(track_con, cleaned)
            except sqlite3.Error:
                hit = None
            if hit:
                return ResolvedTarget(session_id=hit, input_id=cleaned, source="request-id")
        scanned = _scan_bubbles_for_request_id(state_con, cleaned)
        if scanned:
            return ResolvedTarget(session_id=scanned, input_id=cleaned, source="request-id-scan")
        raise SessionNotFoundError(
            f"UUID '{cleaned}' not found as Request ID in ai_code_hashes or state.vscdb bubbles."
        )

    # Default unforced precedence:
    # Step 1: Direct chat-id lookup in cursorDiskKV
    if _lookup_chat_id(state_con, cleaned):
        return ResolvedTarget(session_id=cleaned, input_id=cleaned, source="chat-id")

    # Step 2: Request ID in ai_code_hashes (when tracking DB available)
    if track_con is not None:
        try:
            hit = _lookup_request_id_in_tracking(track_con, cleaned)
        except sqlite3.Error:
            hit = None
        if hit:
            return ResolvedTarget(session_id=hit, input_id=cleaned, source="request-id")

    # Step 3: Bubble scan in state.vscdb
    scanned = _scan_bubbles_for_request_id(state_con, cleaned)
    if scanned:
        return ResolvedTarget(session_id=scanned, input_id=cleaned, source="request-id-scan")

    raise SessionNotFoundError(
        f"UUID '{cleaned}' could not be resolved as a Cursor chat ID or Request ID."
    )


def _to_int_or_none(val: Any) -> int | None:
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    return None


def list_recent_sessions(
    state_con: sqlite3.Connection,
    limit: int = 10,
    list_include_subagents: bool = False,
) -> list[SessionListEntry]:
    """List recent Cursor sessions using schema-adaptive composerHeaders or composerData fallback."""
    effective_limit = max(1, int(limit))
    try:
        pragma_rows = state_con.execute("PRAGMA table_info(composerHeaders)").fetchall()
        cols = {str(r[1]) for r in pragma_rows}
        if "composerId" in cols:
            proj_name = "name" if "name" in cols else "NULL AS name"
            proj_val = "value" if "value" in cols else "NULL AS value"
            proj_created = "createdAt" if "createdAt" in cols else "NULL AS createdAt"
            proj_updated = "lastUpdatedAt" if "lastUpdatedAt" in cols else "NULL AS lastUpdatedAt"
            proj_sub = "COALESCE(isSubagent, 0) AS isSubagent" if "isSubagent" in cols else "0 AS isSubagent"
            proj_sub_type = "subagentTypeName" if "subagentTypeName" in cols else "NULL AS subagentTypeName"

            where_clause = ""
            if not list_include_subagents and "isSubagent" in cols:
                where_clause = "WHERE COALESCE(isSubagent, 0) = 0"

            order_parts = [f"{c} DESC" for c in ("recency", "lastUpdatedAt", "createdAt") if c in cols]
            order_parts.append("composerId ASC")
            order_sql = ", ".join(order_parts)

            sql = (
                f"SELECT composerId, {proj_name}, {proj_val}, {proj_created}, "
                f"{proj_updated}, {proj_sub}, {proj_sub_type} "
                f"FROM composerHeaders {where_clause} ORDER BY {order_sql}"
            )
            rows = state_con.execute(sql).fetchall()
            entries: list[SessionListEntry] = []
            for r in rows:
                cid = str(r[0] or "").strip()
                if not cid:
                    continue
                row_name = r[1] if isinstance(r[1], str) and r[1].strip() else None
                val_dict = _decode_json_blob(r[2])
                val_dict = val_dict if isinstance(val_dict, dict) else {}
                created_ms = _to_int_or_none(r[3]) or _to_int_or_none(val_dict.get("createdAt"))
                updated_ms = _to_int_or_none(r[4]) or _to_int_or_none(val_dict.get("lastUpdatedAt"))
                is_sub = bool(r[5])
                sub_type = _clean_subagent_type(r[6])

                cdata = _fetch_composer_data_dict(state_con, cid)
                val_is_sub, val_sub_type = _subagent_meta_from_dict(val_dict)
                c_is_sub, c_sub_type = _subagent_meta_from_dict(cdata)
                if not is_sub and (val_is_sub or c_is_sub):
                    is_sub = True
                if not list_include_subagents and is_sub:
                    continue

                title = (
                    row_name
                    or (val_dict.get("name") if isinstance(val_dict.get("name"), str) and val_dict.get("name").strip() else None)
                    or (cdata.get("name") if isinstance(cdata, dict) and isinstance(cdata.get("name"), str) and cdata.get("name").strip() else None)
                )
                if not sub_type:
                    sub_type = val_sub_type or c_sub_type
                if created_ms is None and isinstance(cdata, dict):
                    created_ms = _to_int_or_none(cdata.get("createdAt"))
                if updated_ms is None and isinstance(cdata, dict):
                    updated_ms = _to_int_or_none(cdata.get("lastUpdatedAt"))

                model_name = _extract_model_name(cdata) or _extract_model_name(val_dict)
                entries.append(
                    SessionListEntry(
                        session_id=cid,
                        title=title,
                        created_at_ms=created_ms,
                        last_updated_at_ms=updated_ms,
                        is_subagent=is_sub,
                        subagent_type=sub_type,
                        model_name=model_name,
                    )
                )
                if len(entries) >= effective_limit:
                    break
            return entries
    except sqlite3.OperationalError:
        pass

    # Fallback: scan composerData:% in cursorDiskKV
    fallback_candidates: list[tuple[int, int, int, str, SessionListEntry]] = []
    try:
        kv_rows = state_con.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key >= 'composerData:' AND key < 'composerData;' AND value IS NOT NULL"
        ).fetchall()
        prefix = "composerData:"
        for key_val, blob_val in kv_rows:
            k = str(key_val)
            cid = k[len(prefix):] if k.startswith(prefix) else k
            data = _decode_json_blob(blob_val)
            if not isinstance(data, dict):
                continue
            is_sub, sub_type = _subagent_meta_from_dict(data)
            if not list_include_subagents and is_sub:
                continue
            created_ms = _to_int_or_none(data.get("createdAt"))
            updated_ms = _to_int_or_none(data.get("lastUpdatedAt"))
            recency = _to_int_or_none(data.get("recency")) or 0
            title = data.get("name") if isinstance(data.get("name"), str) and data.get("name").strip() else None
            entry = SessionListEntry(
                session_id=cid,
                title=title,
                created_at_ms=created_ms,
                last_updated_at_ms=updated_ms,
                is_subagent=is_sub,
                subagent_type=sub_type,
                model_name=_extract_model_name(data),
            )
            fallback_candidates.append(
                (-recency, -(updated_ms or 0), -(created_ms or 0), cid, entry)
            )
    except sqlite3.Error:
        return []

    fallback_candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    return [item[4] for item in fallback_candidates[:effective_limit]]


def resolve_latest_session_id(state_con: sqlite3.Connection) -> ResolvedTarget:
    """Resolve the most recent main-thread session (always forcing list_include_subagents=False)."""
    entries = list_recent_sessions(state_con, limit=1, list_include_subagents=False)
    if not entries:
        raise SessionNotFoundError("No main-thread Cursor sessions found in state.vscdb.")
    latest_id = entries[0].session_id
    return ResolvedTarget(session_id=latest_id, input_id="--latest", source="latest")
