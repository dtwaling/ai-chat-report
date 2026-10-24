import json
import sqlite3

import pytest

from chat_report.adapters.cursor.resolver import (
    list_recent_sessions,
    resolve_chat_id,
    resolve_latest_session_id,
)
from chat_report.core.adapter import SessionNotFoundError


def _state_connection() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    return con


def _put_composer_data(
    con: sqlite3.Connection,
    session_id: str,
    **overrides: object,
) -> None:
    data = {
        "name": session_id,
        "createdAt": 10,
        "lastUpdatedAt": 20,
        "modelConfig": {"modelName": "test-model"},
    }
    data.update(overrides)
    con.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (f"composerData:{session_id}", json.dumps(data)),
    )


def test_resolve_chat_id_uses_chat_tracking_and_bubble_sources():
    state_con = _state_connection()
    _put_composer_data(state_con, "chat-id")
    state_con.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        ("bubbleId:bubble-chat:bubble-id", json.dumps({"requestId": "bubble-request"})),
    )
    track_con = sqlite3.connect(":memory:")
    track_con.execute(
        "CREATE TABLE ai_code_hashes (requestId TEXT, conversationId TEXT)"
    )
    track_con.execute(
        "INSERT INTO ai_code_hashes VALUES (?, ?)", ("tracking-request", "tracked-chat")
    )

    assert resolve_chat_id("chat-id", state_con, track_con).source == "chat-id"
    tracked = resolve_chat_id("tracking-request", state_con, track_con)
    assert (tracked.session_id, tracked.source) == ("tracked-chat", "request-id")
    scanned = resolve_chat_id("bubble-request", state_con, None, force="request-id")
    assert (scanned.session_id, scanned.source) == ("bubble-chat", "request-id-scan")

    with pytest.raises(SessionNotFoundError):
        resolve_chat_id("tracking-request", state_con, track_con, force="chat-id")


def test_list_recent_headers_excludes_subagents_and_resolves_metadata():
    state_con = _state_connection()
    state_con.execute(
        """CREATE TABLE composerHeaders (
            composerId TEXT,
            name TEXT,
            value TEXT,
            createdAt INTEGER,
            lastUpdatedAt INTEGER,
            isSubagent INTEGER,
            subagentTypeName TEXT,
            recency INTEGER
        )"""
    )
    _put_composer_data(state_con, "main", modelConfig={"modelName": "main-model"})
    _put_composer_data(
        state_con,
        "child",
        isSubagent=True,
        parentComposerId="main",
        subagentTypeName="explore",
    )
    state_con.executemany(
        "INSERT INTO composerHeaders VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("main", None, json.dumps({"name": "Main title"}), 1, 20, 0, None, 2),
            ("child", "Child title", None, 2, 30, 1, "explore", 3),
        ],
    )

    entries = list_recent_sessions(state_con)
    assert [(entry.session_id, entry.title) for entry in entries] == [
        ("main", "Main title")
    ]

    entries = list_recent_sessions(state_con, list_include_subagents=True)
    assert [(entry.session_id, entry.is_subagent, entry.subagent_type) for entry in entries] == [
        ("child", True, "explore"),
        ("main", False, None),
    ]
    assert resolve_latest_session_id(state_con).session_id == "main"


def test_list_recent_falls_back_to_composer_data_and_sorts_by_recency():
    state_con = _state_connection()
    _put_composer_data(state_con, "older", recency=1, createdAt=20, lastUpdatedAt=30)
    _put_composer_data(state_con, "newer", recency=2, createdAt=1, lastUpdatedAt=2)
    _put_composer_data(
        state_con,
        "child",
        recency=3,
        isSubagent=True,
        parentComposerId="newer",
    )

    assert [entry.session_id for entry in list_recent_sessions(state_con)] == [
        "newer",
        "older",
    ]
    assert [entry.session_id for entry in list_recent_sessions(
        state_con, list_include_subagents=True
    )] == ["child", "newer", "older"]


def test_headers_remain_authoritative_when_only_subagents_are_indexed(tmp_path):
    state_con = sqlite3.connect(tmp_path / "state.vscdb")
    state_con.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    state_con.execute(
        """CREATE TABLE composerHeaders (
            composerId TEXT,
            isSubagent INTEGER,
            recency INTEGER
        )"""
    )
    _put_composer_data(state_con, "indexed-child", isSubagent=True)
    _put_composer_data(state_con, "unindexed-main", recency=99)
    state_con.execute(
        "INSERT INTO composerHeaders VALUES (?, ?, ?)",
        ("indexed-child", 1, 1),
    )

    assert list_recent_sessions(state_con, list_include_subagents=False) == []
    with pytest.raises(SessionNotFoundError):
        resolve_latest_session_id(state_con)


def test_headers_without_is_subagent_use_composer_data_fallback():
    state_con = _state_connection()
    state_con.execute("CREATE TABLE composerHeaders (composerId TEXT, recency INTEGER)")
    _put_composer_data(state_con, "main")
    _put_composer_data(state_con, "child", isSubagent=True, parentComposerId="main")
    state_con.executemany("INSERT INTO composerHeaders VALUES (?, ?)", [("main", 1), ("child", 2)])
    assert [entry.session_id for entry in list_recent_sessions(state_con)] == ["main"]


def test_list_recent_recognizes_subagent_info_in_v18_composer_data():
    state_con = _state_connection()
    _put_composer_data(state_con, "main", recency=10)
    _put_composer_data(
        state_con,
        "child-v18",
        recency=20,
        isSubagent=False,
        parentComposerId=None,
        subagentTypeName="unspecified",
        subagentInfo={
            "subagentType": 1,
            "parentComposerId": "main",
            "subagentTypeName": "generalPurpose",
        },
    )
    assert [entry.session_id for entry in list_recent_sessions(state_con)] == ["main"]
    all_entries = list_recent_sessions(state_con, list_include_subagents=True)
    assert [(e.session_id, e.is_subagent, e.subagent_type) for e in all_entries] == [
        ("child-v18", True, "generalPurpose"),
        ("main", False, None),
    ]

