import json
import sqlite3

import pytest

from chat_report.adapters.cursor.adapter import CursorAdapter
from chat_report.adapters.cursor import adapter as adapter_module
from chat_report.adapters.cursor.store import (
    fetch_attribution,
    fetch_offloaded_content,
    open_ro,
    order_bubbles,
)
from chat_report.core.adapter import ExtractOptions, StoreAccessError
from chat_report.core.ir import ResolvedTarget
from tests._fixtures import (
    create_legacy_early_state_db,
    create_legacy_mid_state_db,
    create_synthetic_state_db,
    create_synthetic_tracking_db,
)


def test_open_ro_and_extract_v18_session(tmp_path):
    state = create_synthetic_state_db(tmp_path / "state.vscdb")
    tracking = create_synthetic_tracking_db(tmp_path / "tracking.db")
    con = sqlite3.connect(state)
    user_key = "bubbleId:0a7d3243-61b6-4b3a-8fc5-43c73e2db583:bubble-000"
    user = json.loads(con.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (user_key,)).fetchone()[0])
    user["context"]["fileSelections"] = [{"relativeWorkspacePath": "src/selected.py"}]
    user["context"]["mentions"] = [{"uri": "file:///src/mentioned.py"}]
    user["attachedCodeChunks"] = [{"uri": "file:///src/chunk.py"}]
    user["messageRequestContext"] = {"files": [{"relativeWorkspacePath": "src/request.py"}]}
    con.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(user), user_key))
    assistant_key = "bubbleId:0a7d3243-61b6-4b3a-8fc5-43c73e2db583:bubble-001"
    assistant = json.loads(con.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (assistant_key,)).fetchone()[0])
    assistant["modelInfo"] = {"modelName": "per-turn-model"}
    con.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(assistant), assistant_key))
    con.commit()
    con.close()
    session = CursorAdapter().extract_session("0a7d3243-61b6-4b3a-8fc5-43c73e2db583",
        ExtractOptions(state_db=state, tracking_db=tracking))
    assert session.token_breakdown.by_category["system_prompt"]["tokens"] == 5
    assert session.turns
    assert session.attribution is not None
    assert session.attribution.distinct_files_touched == ["README.md", "src/example.py"]
    turn = session.turns[0]
    assert (turn.input_tokens, turn.output_tokens, turn.model_name) == (4, 6, "per-turn-model")
    assert ".cursor/rules/test.mdc" in turn.attached_rules
    assert turn.attached_files == ["src/selected.py", "file:///src/mentioned.py", "file:///src/chunk.py", "src/request.py"]
    con = open_ro(state)
    try:
        assert fetch_offloaded_content(con, "deadbeef") == "before content"
        assert fetch_offloaded_content(con, "composer.content.deadbeef") == "before content"
        assert fetch_offloaded_content(con, "ofsContent:composer.content.deadbeef") == "before content"
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            con.execute("CREATE TABLE cannot_write (id INTEGER)")
    finally:
        con.close()
    with pytest.raises(StoreAccessError):
        open_ro(tmp_path / "missing.db")


def test_extract_session_preserves_resolved_target_provenance(tmp_path):
    state = create_synthetic_state_db(tmp_path / "state.vscdb")
    tracking = create_synthetic_tracking_db(tmp_path / "tracking.db")
    target = ResolvedTarget("0a7d3243-61b6-4b3a-8fc5-43c73e2db583", "request-0", "request-id")
    session = CursorAdapter().extract_session(target.session_id, ExtractOptions(state_db=state, tracking_db=tracking), resolved_from=target)
    assert session.resolved_from == {"session_id": target.session_id, "input_id": "request-0", "source": "request-id"}


def test_tracking_database_override_and_default_failure_handling(tmp_path, monkeypatch):
    state = create_synthetic_state_db(tmp_path / "state.vscdb")
    missing = tmp_path / "missing-tracking.db"
    with pytest.raises(StoreAccessError):
        CursorAdapter().extract_session("0a7d3243-61b6-4b3a-8fc5-43c73e2db583",
            ExtractOptions(state_db=state, tracking_db=missing))
    corrupt = tmp_path / "corrupt-tracking.db"
    corrupt.write_text("not sqlite", encoding="utf-8")
    with pytest.raises(StoreAccessError):
        CursorAdapter().extract_session("0a7d3243-61b6-4b3a-8fc5-43c73e2db583",
            ExtractOptions(state_db=state, tracking_db=corrupt))
    monkeypatch.setattr(adapter_module, "resolve_tracking_db_with_provenance", lambda _: (missing, False))
    session = CursorAdapter().extract_session("0a7d3243-61b6-4b3a-8fc5-43c73e2db583", ExtractOptions(state_db=state))
    assert session.attribution is None
    assert "cursor-tracking-db-unavailable" in {warning["code"] for warning in session.warnings}


def test_order_bubbles_filters_orphans_and_falls_back_to_timestamp():
    bubbles = {"a": {"startedAtMs": 20}, "b": {"startedAtMs": 10}}
    ordered, count, warnings = order_bubbles([{"bubbleId": "a"}], bubbles)
    assert ([item[0] for item in ordered], count, warnings) == (["a"], 1, [])
    ordered, count, warnings = order_bubbles([], bubbles)
    assert [item[0] for item in ordered] == ["b", "a"]
    assert count == 0
    assert warnings[0]["code"] == "cursor-header-order-missing-sorted-by-timestamp"


def test_attribution_warns_when_legacy_tracking_lacks_filename():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE ai_code_hashes (conversationId TEXT, source TEXT)")
    con.execute("INSERT INTO ai_code_hashes VALUES (?, ?)", ("chat", "composer"))
    warnings = []
    attribution = fetch_attribution(con, "chat", None, warnings)
    assert attribution.composer_code_hashes_count == 1
    assert attribution.distinct_files_touched == []
    assert warnings[0]["code"] == "cursor-tracking-filename-column-missing"


def test_legacy_early_fields_synthesize_timestamps(tmp_path):
    state = create_legacy_early_state_db(tmp_path / "legacy.vscdb")
    tracking = create_synthetic_tracking_db(tmp_path / "tracking.db")
    session = CursorAdapter().extract_session("0a7d3243-61b6-4b3a-8fc5-43c73e2db583",
        ExtractOptions(state_db=state, tracking_db=tracking))
    assert session.turns[0].output_tokens == 14
    assert [block.text for block in session.turns[0].thinking_blocks] == ["legacy thought", "second thought"]
    assert session.turns[0].timestamps_synthesized is True
    assert any(warning["code"] == "cursor-per-turn-timestamps-synthesized" for warning in session.warnings)


def test_legacy_mid_context_and_orphan_filtering(tmp_path):
    state = create_legacy_mid_state_db(tmp_path / "legacy.vscdb")
    tracking = create_synthetic_tracking_db(tmp_path / "tracking.db")
    session = CursorAdapter().extract_session("0a7d3243-61b6-4b3a-8fc5-43c73e2db583",
        ExtractOptions(state_db=state, tracking_db=tracking))
    assert session.orphan_bubble_count == 1
    assert ".cursor/rules/legacy.mdc" in {ref.path for ref in session.customizations}
    assert ".cursor/rules/legacy.mdc" in session.turns[0].attached_rules
    assert "src/legacy.py" in session.turns[0].attached_files
    assert all("orphan" not in text for turn in session.turns for text in turn.assistant_texts)


def test_manual_skills_accept_lowercase_fullpath_attribute(tmp_path):
    session_id = "0a7d3243-61b6-4b3a-8fc5-43c73e2db583"
    state = create_synthetic_state_db(tmp_path / "state.vscdb", session_id=session_id, bubble_count=2)
    tracking = create_synthetic_tracking_db(tmp_path / "tracking.db", session_id=session_id)
    con = sqlite3.connect(state)
    key = f"bubbleId:{session_id}:bubble-000"
    bubble = json.loads(con.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (key,)).fetchone()[0])
    bubble["text"] = '<manually_attached_skills>\n<agent_skill fullpath="D:/skills/test/SKILL.md">\n</manually_attached_skills>'
    con.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(bubble), key))
    con.commit()
    con.close()
    session = CursorAdapter().extract_session(session_id, ExtractOptions(state_db=state, tracking_db=tracking))
    assert any(ref.kind == "skill" and ref.path == "D:/skills/test/SKILL.md" for ref in session.customizations)


def test_synthetic_turn_preserves_assistant_bubble_timestamps(tmp_path):
    session_id = "0a7d3243-61b6-4b3a-8fc5-43c73e2db583"
    state = create_synthetic_state_db(tmp_path / "state.vscdb", session_id=session_id, bubble_count=2)
    tracking = create_synthetic_tracking_db(tmp_path / "tracking.db", session_id=session_id)
    con = sqlite3.connect(state)
    con.execute("DELETE FROM cursorDiskKV WHERE key = ?", (f"bubbleId:{session_id}:bubble-000",))
    con.commit()
    con.close()
    session = CursorAdapter().extract_session(session_id, ExtractOptions(state_db=state, tracking_db=tracking))
    turn = session.turns[0]
    assert turn.user_bubble_id == "__synthetic_turn_1__"
    assert turn.timestamps_synthesized is False
    assert (turn.started_at_ms, turn.completed_at_ms) == (1100, 1150)
    warning_codes = {warning["code"] for warning in session.warnings}
    assert "cursor-no-user-bubbles-synthetic-turn" in warning_codes
    assert "cursor-per-turn-timestamps-synthesized" not in warning_codes


def test_pre_user_bubbles_keep_one_based_call_order(tmp_path):
    session_id = "0a7d3243-61b6-4b3a-8fc5-43c73e2db583"
    state = create_synthetic_state_db(tmp_path / "state.vscdb", session_id=session_id, bubble_count=2)
    tracking = create_synthetic_tracking_db(tmp_path / "tracking.db", session_id=session_id)
    con = sqlite3.connect(state)
    early = {
        "type": 2, "text": "early", "startedAtMs": 900, "completedAtMs": 950,
        "toolFormerData": {"name": "read_file_v2", "params": {"targetFile": "early.py"}, "result": {}},
    }
    con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (f"bubbleId:{session_id}:early", json.dumps(early)))
    headers_key = f"composerConvHeaders:{session_id}:000000"
    con.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (
        json.dumps([{"bubbleId": "early", "type": 2}, {"bubbleId": "bubble-000", "type": 1}, {"bubbleId": "bubble-001", "type": 2}]),
        headers_key,
    ))
    con.commit()
    con.close()
    session = CursorAdapter().extract_session(session_id, ExtractOptions(state_db=state, tracking_db=tracking))
    assert [call.call_index for call in session.turns[0].tool_calls] == [1, 2]
    assert [call.bubble_id for call in session.turns[0].tool_calls] == ["early", "bubble-001"]


def test_subagent_parent_linkage_and_cycles_are_safe(tmp_path):
    session_id = "0a7d3243-61b6-4b3a-8fc5-43c73e2db583"
    state = create_synthetic_state_db(tmp_path / "state.vscdb", session_id=session_id, bubble_count=2)
    tracking = create_synthetic_tracking_db(tmp_path / "tracking.db", session_id=session_id)
    con = sqlite3.connect(state)
    parent_data = json.loads(con.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (f"composerData:{session_id}",)).fetchone()[0])
    parent_data["subagentComposerIds"] = ["child-dispatched", "child-composer-only"]
    con.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(parent_data), f"composerData:{session_id}"))
    assistant_key = f"bubbleId:{session_id}:bubble-001"
    assistant = json.loads(con.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (assistant_key,)).fetchone()[0])
    assistant["toolFormerData"] = {"name": "task_v2", "result": {"agentId": "child-dispatched"}}
    con.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(assistant), assistant_key))
    for child in ("child-dispatched", "child-composer-only"):
        data = {"name": child, "createdAt": 1, "lastUpdatedAt": 2, "status": "completed", "isSubagent": True,
            "parentComposerId": session_id, "subagentComposerIds": [session_id, child]}
        con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (f"composerData:{child}", json.dumps(data)))
        con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (f"bubbleId:{child}:user", json.dumps({"type": 1, "text": "child", "startedAtMs": 1, "completedAtMs": 2})))
        con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (f"composerConvHeaders:{child}:000000", json.dumps([{"bubbleId": "user", "type": 1}])))
    con.commit()
    con.close()
    session = CursorAdapter().extract_session(session_id, ExtractOptions(state_db=state, tracking_db=tracking))
    parent_turns = {child.session_id: child.parent_turn_index for child in session.subagents}
    assert parent_turns == {"child-dispatched": 1, "child-composer-only": None}
