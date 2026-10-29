import base64
import json
from pathlib import Path
import sqlite3

import pytest

from chat_report.adapters.cursor.adapter import CursorAdapter, _customization_kind, _reasoning_effort
from chat_report.adapters.cursor import adapter as adapter_module
from chat_report.adapters.cursor.store import (
    _sqlite_ro_uri,
    bubble_timestamps,
    extract_thinking,
    extract_token_breakdown,
    extract_token_counts,
    fetch_attribution,
    fetch_conversation_blobs,
    fetch_offloaded_content,
    has_non_narration_thinking_in_bubble,
    is_narration_block,
    open_ro,
    order_bubbles,
)
from chat_report.core.adapter import ExtractOptions, StoreAccessError
from chat_report.core.classifier import classify_session
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
    bubble["context"]["cursorRules"] = [{"filename": "chat-report/SKILL.md", "slashSubject": {"kind": "skill"}}]
    con.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(bubble), key))
    con.commit()
    con.close()
    skill_file = tmp_path / ".cursor" / "skills" / "chat-report" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text("# Chat Report", encoding="utf-8")
    session = CursorAdapter().extract_session(session_id, ExtractOptions(state_db=state, tracking_db=tracking, workspace=tmp_path))
    assert any(ref.kind == "skill" and ref.path == "D:/skills/test/SKILL.md" for ref in session.customizations)
    assert any(ref.kind == "skill" and ref.path == "chat-report/SKILL.md" and ref.source == "context_attached" for ref in session.customizations)
    classify_session(session, tmp_path)
    assert any(ref.kind == "skill" and ref.path == ".cursor/skills/chat-report/skill.md" and ref.source == "context_attached" for ref in session.customizations)


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


def test_subagent_none_status_normalizes_to_completed(tmp_path):
    session_id = "0a7d3243-61b6-4b3a-8fc5-43c73e2db583"
    state = create_synthetic_state_db(tmp_path / "state.vscdb", session_id=session_id, bubble_count=2)
    tracking = create_synthetic_tracking_db(tmp_path / "tracking.db", session_id=session_id)
    con = sqlite3.connect(state)
    parent = json.loads(con.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (f"composerData:{session_id}",)).fetchone()[0])
    parent["subagentComposerIds"] = ["child"]
    con.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(parent), f"composerData:{session_id}"))
    con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (f"composerData:child", json.dumps({"name": "child", "createdAt": 1, "lastUpdatedAt": 2, "status": "none", "isSubagent": True, "parentComposerId": session_id})))
    con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", ("bubbleId:child:user", json.dumps({"type": 1, "text": "child"})))
    con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", ("composerConvHeaders:child:000000", json.dumps([{"bubbleId": "user", "type": 1}])))
    con.commit()
    con.close()
    session = CursorAdapter().extract_session(session_id, ExtractOptions(state_db=state, tracking_db=tracking))
    assert session.subagents[0].status == "completed"


def test_live_shaped_token_breakdown_and_usage_tree():
    composer_data = {
        "promptTokenBreakdown": {
            "totalUsedTokens": 4500,
            "maxTokens": 128000,
            "categories": [
                {"id": "system_prompt", "label": "System", "estimatedTokens": 1200, "characterCount": 4800},
                {"id": "tools", "label": "Tools", "estimatedTokens": 800, "characterCount": 3200},
            ],
        },
        "promptContextUsageTree": {
            "schemaVersion": 2,
            "nodes": [{"name": "system", "tokens": 1200}],
        },
    }
    tb = extract_token_breakdown(composer_data)
    assert tb.total_context_tokens_used == 4500
    assert tb.context_token_limit == 128000
    assert tb.by_category == {
        "system_prompt": {"tokens": 1200, "chars": 4800},
        "tools": {"tokens": 800, "chars": 3200},
    }
    assert tb.top_context_nodes == [{"name": "system", "tokens": 1200}]


def test_json_string_thinking_deduplication_and_timestamps():
    bubble = {
        "thinking": '{"text": "unwrapped thought"}',
        "allThinkingBlocks": [{"text": "unwrapped thought"}, '{"text": "second thought"}'],
        "createdAt": "2026-01-01T00:00:01",
        "thinkingDurationMs": 142.9,
    }
    blocks = extract_thinking("b-1", bubble)
    assert [b.text for b in blocks] == ["unwrapped thought", "second thought"]
    assert blocks[0].duration_ms == 142
    assert blocks[0].started_at_ms == 1767225601000
    assert blocks[0].completed_at_ms == 1767225601142
    assert bubble_timestamps({"createdAtMs": 1234.8}) == (1234, 1234)


def test_legacy_created_at_only_does_not_synthesize_zero_elapsed_ms(tmp_path):
    session_id = "0a7d3243-61b6-4b3a-8fc5-43c73e2db583"
    state = create_synthetic_state_db(tmp_path / "state.vscdb", session_id=session_id, bubble_count=2)
    tracking = create_synthetic_tracking_db(tmp_path / "tracking.db", session_id=session_id)
    con = sqlite3.connect(state)
    assistant_key = f"bubbleId:{session_id}:bubble-001"
    assistant = json.loads(con.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (assistant_key,)).fetchone()[0])
    assistant.pop("startedAtMs", None)
    assistant.pop("completedAtMs", None)
    assistant["createdAt"] = "2026-01-01T00:00:05Z"
    assistant["toolFormerData"] = {
        "name": "run_terminal_command_v2",
        "params": {"command": "echo hi"},
        "result": {"output": "hi", "exitCode": 0},
        "additionalData": {"status": "completed"},
    }
    con.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(assistant), assistant_key))
    con.commit()
    con.close()
    session = CursorAdapter().extract_session(session_id, ExtractOptions(state_db=state, tracking_db=tracking))
    call = session.turns[0].tool_calls[0]
    assert call.started_at_ms == 1767225605000
    assert call.completed_at_ms == 1767225605000
    assert call.elapsed_ms is None
    assert "_dispatch_status" not in call.input_params


def test_open_ro_special_char_filenames_and_unc_uri(tmp_path):
    special_dir = tmp_path / "dir #1 %20"
    special_dir.mkdir()
    state = create_synthetic_state_db(special_dir / "state #1.vscdb", bubble_count=2)
    con = open_ro(state)
    try:
        assert con.execute("SELECT COUNT(*) FROM cursorDiskKV").fetchone()[0] > 0
    finally:
        con.close()
    unc_uri = _sqlite_ro_uri(Path(r"\\server\share\dir #1\state.vscdb"))
    assert unc_uri.startswith("file:////server/share/dir%20%231/state.vscdb?mode=ro")


def test_subagent_info_extraction_and_customization_paths(tmp_path):
    assert _customization_kind(".cursor/rules/my-rule.md") == "rule"
    assert _customization_kind(r".cursor\hooks\guard.py") == "hook"
    assert _customization_kind(r"C:\repo\skills\my-skill\SKILL.md") == "skill"
    session_id = "0a7d3243-61b6-4b3a-8fc5-43c73e2db583"
    state = create_synthetic_state_db(tmp_path / "state.vscdb", session_id=session_id, bubble_count=2)
    tracking = create_synthetic_tracking_db(tmp_path / "tracking.db", session_id=session_id)
    track_con = sqlite3.connect(tracking)
    track_con.execute("INSERT INTO ai_code_hashes VALUES (?, ?, ?, ?)", ("req-win", session_id, "composer", "/C:/Users/dev/app.py"))
    track_con.commit()
    track_con.close()

    con = sqlite3.connect(state)
    parent = json.loads(con.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (f"composerData:{session_id}",)).fetchone()[0])
    parent["subagentComposerIds"] = ["CHILD-1"]
    con.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(parent), f"composerData:{session_id}"))
    assistant_key = f"bubbleId:{session_id}:bubble-001"
    assistant = json.loads(con.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (assistant_key,)).fetchone()[0])
    assistant["toolFormerData"] = {
        "name": "task_v2",
        "params": {"subagent_type": "explore"},
        "result": {"agentId": "child-1"},
        "additionalData": {"status": "completed"},
    }
    con.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(assistant), assistant_key))
    child_data = {
        "name": "Child Subagent",
        "createdAt": 10,
        "lastUpdatedAt": 20,
        "status": "completed",
        "subagentTypeName": "unspecified",
        "subagentInfo": {
            "subagentType": 1,
            "parentComposerId": session_id,
            "subagentTypeName": "unspecified",
        },
    }
    for cid in ("CHILD-1", "child-1"):
        con.execute("INSERT OR REPLACE INTO cursorDiskKV VALUES (?, ?)", (f"composerData:{cid}", json.dumps(child_data)))
        con.execute("INSERT OR REPLACE INTO cursorDiskKV VALUES (?, ?)", (f"bubbleId:{cid}:user", json.dumps({"type": 1, "text": "go"})))
        con.execute("INSERT OR REPLACE INTO cursorDiskKV VALUES (?, ?)", (f"composerConvHeaders:{cid}:000000", json.dumps([{"bubbleId": "user", "type": 1}])))
    con.commit()
    con.close()

    adapter = CursorAdapter()
    parent_ir = adapter.extract_session(session_id, ExtractOptions(state_db=state, tracking_db=tracking))
    assert "C:/Users/dev/app.py" in parent_ir.attribution.distinct_files_touched
    assert len(parent_ir.subagents) == 1
    assert parent_ir.subagents[0].subagent_type == "explore"
    assert parent_ir.subagents[0].parent_turn_index == 1

    child_ir = adapter.extract_session("CHILD-1", ExtractOptions(state_db=state, tracking_db=tracking))
    assert child_ir.is_subagent is True
    assert child_ir.parent_session_id == session_id
    assert child_ir.subagent_type is None


def test_latest_chat_generation_uuid_populates_last_turn_missing_request_id(tmp_path):
    session_id = "0a7d3243-61b6-4b3a-8fc5-43c73e2db583"
    state = create_synthetic_state_db(tmp_path / "state.vscdb", session_id=session_id, bubble_count=2)
    tracking = create_synthetic_tracking_db(tmp_path / "tracking.db", session_id=session_id)
    con = sqlite3.connect(state)
    composer_key = f"composerData:{session_id}"
    composer = json.loads(con.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (composer_key,)).fetchone()[0])
    composer["latestChatGenerationUUID"] = "d48e54ae-7761-4aa1-bae2-94598a62cfef"
    con.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(composer), composer_key))
    user_key = f"bubbleId:{session_id}:bubble-000"
    user = json.loads(con.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (user_key,)).fetchone()[0])
    user["requestId"] = ""
    con.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(user), user_key))
    con.commit()
    con.close()

    session = CursorAdapter().extract_session(session_id, ExtractOptions(state_db=state, tracking_db=tracking))
    assert session.turns[-1].request_id == "d48e54ae-7761-4aa1-bae2-94598a62cfef"


def test_resumed_subagent_uses_latest_dispatch_status_and_preserves_type(tmp_path):
    session_id = "0a7d3243-61b6-4b3a-8fc5-43c73e2db583"
    state = create_synthetic_state_db(tmp_path / "state.vscdb", session_id=session_id, bubble_count=2)
    tracking = create_synthetic_tracking_db(tmp_path / "tracking.db", session_id=session_id)
    con = sqlite3.connect(state)
    parent = json.loads(con.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (f"composerData:{session_id}",)).fetchone()[0])
    parent["subagentComposerIds"] = ["child-resumed"]
    con.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(parent), f"composerData:{session_id}"))
    first_key = f"bubbleId:{session_id}:bubble-001"
    first = json.loads(con.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (first_key,)).fetchone()[0])
    first["toolFormerData"] = {
        "name": "task_v2",
        "params": {"subagent_type": "general-purpose"},
        "result": {"agentId": "child-resumed"},
        "additionalData": {"status": "aborted"},
    }
    con.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(first), first_key))
    second = {
        "type": 2,
        "text": "resumed",
        "startedAtMs": 1200,
        "completedAtMs": 1250,
        "toolFormerData": {
            "name": "task_v2",
            "params": {"resume": "child-resumed"},
            "result": {"agentId": "child-resumed"},
            "additionalData": {"status": "success"},
        },
    }
    con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (f"bubbleId:{session_id}:bubble-002", json.dumps(second)))
    headers_key = f"composerConvHeaders:{session_id}:000000"
    con.execute(
        "UPDATE cursorDiskKV SET value = ? WHERE key = ?",
        (
            json.dumps([
                {"bubbleId": "bubble-000", "type": 1},
                {"bubbleId": "bubble-001", "type": 2},
                {"bubbleId": "bubble-002", "type": 2},
            ]),
            headers_key,
        ),
    )
    child_data = {
        "name": "Resumed Coder",
        "createdAt": 10,
        "lastUpdatedAt": 20,
        "status": "aborted",
        "isSubagent": True,
        "parentComposerId": session_id,
    }
    con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", ("composerData:child-resumed", json.dumps(child_data)))
    con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", ("bubbleId:child-resumed:user", json.dumps({"type": 1, "text": "fix"})))
    con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", ("composerConvHeaders:child-resumed:000000", json.dumps([{"bubbleId": "user", "type": 1}])))
    con.commit()
    con.close()

    session = CursorAdapter().extract_session(session_id, ExtractOptions(state_db=state, tracking_db=tracking))
    assert len(session.subagents) == 1
    assert session.subagents[0].status == "completed"
    assert session.subagents[0].subagent_type == "general-purpose"


def test_in_flight_session_and_child_subagent_and_attribution_window(tmp_path):
    session_id = "0a7d3243-61b6-4b3a-8fc5-43c73e2db583"
    state = create_synthetic_state_db(tmp_path / "state.vscdb", session_id=session_id, bubble_count=4)
    tracking = create_synthetic_tracking_db(tmp_path / "tracking.db", session_id=session_id)
    con = sqlite3.connect(state)
    parent = json.loads(con.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (f"composerData:{session_id}",)).fetchone()[0])
    parent["isContinuationInProgress"] = True
    parent["subagentComposerIds"] = ["child-in-flight"]
    con.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(parent), f"composerData:{session_id}"))
    child_data = {
        "name": "Active Subagent",
        "createdAt": 1000,
        "lastUpdatedAt": 1200,
        "status": "running",
        "isSubagent": True,
        "parentComposerId": session_id,
    }
    con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", ("composerData:child-in-flight", json.dumps(child_data)))
    con.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (
            "bubbleId:child-in-flight:user",
            json.dumps({
                "type": 1,
                "text": "run",
                "startedAtMs": 1000,
                "completedAtMs": 1100,
                "tokenCount": {"inputTokens": 5, "outputTokens": 10},
                "toolFormerData": {"name": "read_file_v2", "status": "loading", "params": {"targetFile": "a.py"}},
            }),
        ),
    )
    con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", ("composerConvHeaders:child-in-flight:000000", json.dumps([{"bubbleId": "user", "type": 1}])))
    con.commit()
    con.close()

    session = CursorAdapter().extract_session(session_id, ExtractOptions(state_db=state, tracking_db=tracking))
    assert session.in_flight is True
    assert session.status is None
    assert session.ended_at_ms is None
    assert session.duration_ms is None
    assert isinstance(session.captured_at_ms, int)
    assert session.turns[0].input_tokens == 4
    assert session.turns[0].output_tokens == 6
    assert session.turns[-1].input_tokens is None
    assert session.turns[-1].output_tokens is None
    assert any(w["code"] == "cursor-session-in-flight" for w in session.warnings)
    assert len(session.subagents) == 1
    assert session.subagents[0].in_flight is True
    assert session.subagents[0].status is None
    assert session.subagents[0].duration_ms is None

    # Now verify completed session does not extend attribution window to captured_at_ms, while in_flight does
    track_con = sqlite3.connect(tracking)
    late_commit_time = session.captured_at_ms - 1000
    track_con.execute(
        "INSERT INTO scored_commits VALUES (?, ?, ?, ?, ?, ?)",
        ("def456", "In-flight commit", 5, 5, 0, late_commit_time),
    )
    track_con.commit()
    track_con.close()

    session_live = CursorAdapter().extract_session(session_id, ExtractOptions(state_db=state, tracking_db=tracking))
    assert any(c["commitHash"] == "def456" for c in session_live.attribution.scored_commits)

    con = sqlite3.connect(state)
    parent["isContinuationInProgress"] = False
    con.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(parent), f"composerData:{session_id}"))
    con.commit()
    con.close()

    session_done = CursorAdapter().extract_session(session_id, ExtractOptions(state_db=state, tracking_db=tracking))
    assert session_done.in_flight is False
    assert session_done.duration_ms == 350
    assert not any(c["commitHash"] == "def456" for c in session_done.attribution.scored_commits)


def test_event_clock_ahead_of_capture_warning(tmp_path):
    session_id = "0a7d3243-61b6-4b3a-8fc5-43c73e2db583"
    state = create_synthetic_state_db(tmp_path / "state.vscdb", session_id=session_id, bubble_count=2)
    tracking = create_synthetic_tracking_db(tmp_path / "tracking.db", session_id=session_id)
    con = sqlite3.connect(state)
    key = f"bubbleId:{session_id}:bubble-001"
    bubble = json.loads(con.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (key,)).fetchone()[0])
    bubble["completedAtMs"] = 9999999999999
    con.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(bubble), key))
    con.commit()
    con.close()

    session = CursorAdapter().extract_session(session_id, ExtractOptions(state_db=state, tracking_db=tracking))
    assert any(w["code"] == "cursor-event-clock-ahead-of-capture" for w in session.warnings)


def test_conversation_blobs_narration_signatures_and_thinking_duration(tmp_path):
    session_id = "0a7d3243-61b6-4b3a-8fc5-43c73e2db583"
    state = create_synthetic_state_db(tmp_path / "state.vscdb", session_id=session_id, bubble_count=2)
    tracking = create_synthetic_tracking_db(tmp_path / "tracking.db", session_id=session_id)

    thinking_sig = base64.b64encode(b"\x08\x04\x12\x12\x0a\x10\x08\x13\x18\x02\x38\x01\x42\x08thinking").decode("ascii")
    narration_sig = base64.b64encode(b"\x08\x04\x12\x13\x0a\x11\x08\x13\x18\x02\x38\x01\x42\x09narration").decode("ascii")
    assert thinking_sig == "CAQSEgoQCBMYAjgBQgh0aGlua2luZw=="
    assert narration_sig == "CAQSEwoRCBMYAjgBQgluYXJyYXRpb24="

    # Explicitly thinking-signed block with 1..49 ms duration preserves duration_ms and completed_at_ms
    short_signed_bubble = {
        "thinking": {"text": "fast genuine thought", "signature": thinking_sig},
        "startedAtMs": 5000,
        "thinkingDurationMs": 25,
    }
    short_blocks = extract_thinking("b-short", short_signed_bubble)
    assert len(short_blocks) == 1
    assert short_blocks[0].duration_ms == 25
    assert short_blocks[0].completed_at_ms == 5025

    # Untimed genuine thinking block (thinkingDurationMs=None, completedAtMs=None)
    untimed_bubble = {"thinking": {"text": "deep untimed thought"}}
    assert has_non_narration_thinking_in_bubble(untimed_bubble, narration_texts=set()) is True
    assert _reasoning_effort({"modelConfig": "invalid-not-a-dict"}, has_non_narration_thinking=True) == "enabled"
    assert _reasoning_effort({"modelConfig": {"selectedModels": [{"parameters": [{"id": "effort", "value": "high"}]}]}}) == "high"
    assert _reasoning_effort({"modelConfig": {"modelName": "claude-opus-4-7-thinking-high"}}) == "high"
    assert _reasoning_effort({"modelConfig": {"modelName": "claude-opus-5-5[effort=max]"}}) == "max"
    assert _reasoning_effort({"modelConfig": {}}, has_non_narration_thinking=False) is None

    # Build conversationState protobuf framing with 5 blobs:
    # 1: system prompt with <always_applied_workspace_rule> and <agent_requestable_workspace_rule>
    # 2: assistant with narration signature ("narration only text") and shared text ("shared text")
    # 3: assistant with thinking signature ("shared text") and malformed signature
    # 4 & 5: duplicate toolCallId blobs merging result_text and file_size
    h1 = "11" * 32
    h2 = "22" * 32
    h3 = "33" * 32
    h4 = "44" * 32
    h5 = "55" * 32
    raw_proto = b"".join(b"\x0a\x22\x0a\x20" + bytes.fromhex(h) for h in (h1, h2, h3, h4, h5))
    conv_state = "~" + base64.b64encode(raw_proto).decode("ascii")

    con = sqlite3.connect(state)
    composer = json.loads(con.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (f"composerData:{session_id}",)).fetchone()[0])
    composer["conversationState"] = conv_state
    composer["context"] = {"cursorRules": [{"path": ".cursor/rules/session-rule.mdc"}]}
    con.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(composer), f"composerData:{session_id}"))

    blob1 = {
        "role": "system",
        "content": (
            '<always_applied_workspace_rule name="d:/repo/AGENTS.md">rules</always_applied_workspace_rule>\n'
            '<agent_requestable_workspace_rule fullPath="d:/repo/.cursor/rules/ignored.mdc">skip</agent_requestable_workspace_rule>'
        ),
    }
    blob2 = {
        "role": "assistant",
        "providerOptions": {
            "cursor": {
                "anthropicNativeContent": json.dumps([
                    {"type": "thinking", "thinking": "narration only text", "signature": narration_sig},
                    {"type": "thinking", "thinking": "shared text", "signature": narration_sig},
                ])
            }
        },
    }
    blob3 = {
        "role": "assistant",
        "providerOptions": {
            "cursor": {
                "anthropicNativeContent": [
                    {"type": "thinking", "thinking": "shared text", "signature": thinking_sig},
                    {"type": "thinking", "thinking": "bad sig text", "signature": "%%%not-base64%%%"},
                ]
            }
        },
    }
    blob4 = {
        "role": "tool",
        "id": "tool-1",
        "content": [{"toolCallId": "tool-1", "result": "recovered tool text"}],
    }
    blob5 = {
        "role": "tool",
        "id": "tool-1",
        "providerOptions": {
            "cursor": {
                "highLevelToolCallResult": {
                    "output": {"success": {"fileSize": 120000, "totalLines": 3000, "readRange": {"startLine": 1, "endLine": 50}}}
                }
            }
        },
    }
    for h, b in ((h1, blob1), (h2, blob2), (h3, blob3), (h4, blob4), (h5, blob5)):
        con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (f"agentKv:blob:{h}", json.dumps(b)))

    # Set bubble-001 to have narration-only thinking with 200ms duration, assistant text quoting <always_applied_workspace_rule>, and read_file_v2 without inline content
    assistant_key = f"bubbleId:{session_id}:bubble-001"
    assistant = json.loads(con.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (assistant_key,)).fetchone()[0])
    assistant["text"] = 'Assistant prose discussing <always_applied_workspace_rule name="quoted-in-assistant.mdc">.'
    assistant["thinking"] = {"text": "narration only text"}
    assistant["thinkingDurationMs"] = 200
    assistant["tokenCount"] = {"inputTokens": 0, "outputTokens": 0}
    assistant["toolFormerData"] = {
        "name": "read_file_v2",
        "toolCallId": "tool-1",
        "modelCallId": "model-1",
        "status": "completed",
        "params": {"targetFile": "src/example.py"},
        "result": {"totalLinesInFile": 3000},
    }
    con.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(assistant), assistant_key))
    # Also set bubble-000 tokenCount to 0/0 and include an <always_applied_workspace_rule> in type == 1 user text
    user_key = f"bubbleId:{session_id}:bubble-000"
    user = json.loads(con.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (user_key,)).fetchone()[0])
    user["text"] = 'User prompt with <always_applied_workspace_rule name="user-bubble-rule.mdc">rule</always_applied_workspace_rule>'
    user["tokenCount"] = {"inputTokens": 0, "outputTokens": 0}
    con.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(user), user_key))
    con.commit()

    ro_con = open_ro(state)
    try:
        blobs = fetch_conversation_blobs(ro_con, composer)
        assert blobs.narration_texts == {"narration only text"}
        assert "shared text" not in blobs.narration_texts
        assert blobs.tool_results["tool-1"]["result_text"] == "recovered tool text"
        assert blobs.tool_results["tool-1"]["file_size"] == 120000
        assert fetch_conversation_blobs(ro_con, {"conversationState": "~not_valid_base64_!!!"}) == fetch_conversation_blobs(ro_con, {})
    finally:
        ro_con.close()
    con.close()

    # Shared text in a genuine multi-second block keeps its duration
    shared_blocks = extract_thinking("b-shared", {"thinking": {"text": "shared text"}, "startedAtMs": 1000, "thinkingDurationMs": 2500}, narration_texts=blobs.narration_texts)
    assert shared_blocks[0].duration_ms == 2500
    assert shared_blocks[0].completed_at_ms == 3500

    # End-to-end extraction: narration-only session does not set reasoning_effort="enabled", zeroes out narration duration/completed_at_ms, recovers tool output, and enumerates rules
    session = CursorAdapter().extract_session(session_id, ExtractOptions(state_db=state, tracking_db=tracking))
    assert session.reasoning_effort is None
    t_block = session.turns[0].thinking_blocks[0]
    assert t_block.text == "narration only text"
    assert t_block.duration_ms is None
    assert t_block.completed_at_ms is None
    assert session.turns[0].input_tokens is None
    assert session.turns[0].output_tokens is None
    call = session.turns[0].tool_calls[0]
    assert call.output_summary == "recovered tool text"
    assert call.output_char_len == len("recovered tool text")
    assert call.input_params["_file_size"] == 120000
    cust_paths = {c.path for c in session.customizations}
    assert "d:/repo/AGENTS.md" in cust_paths
    assert "user-bubble-rule.mdc" in cust_paths
    assert ".cursor/rules/session-rule.mdc" in cust_paths
    assert "d:/repo/.cursor/rules/ignored.mdc" not in cust_paths
    assert "quoted-in-assistant.mdc" not in cust_paths


def test_token_counts_and_top_context_nodes_ranking():
    assert extract_token_counts({"tokenCount": {"inputTokens": 0, "outputTokens": 0}}) == (None, None)
    assert extract_token_counts({"tokenCount": {"inputTokens": 0, "outputTokens": 12}}) == (0, 12)
    assert extract_token_counts({"tokenCount": 0}) == (None, None)
    assert extract_token_counts({"tokenCount": -5}) == (None, None)

    tree = {
        "nodes": [
            {
                "id": "container",
                "estimatedTokens": 99999,
                "children": [
                    {"id": "b-tie", "estimatedTokens": 500.0, "characterCount": 2000, "tokens": True},
                    {"id": "a-tie", "estimatedTokens": 500, "characterCount": 2000, "chars": "bad"},
                    {"id": "char-winner", "estimatedTokens": 500, "characterCount": 3000},
                    {"id": "no-tokens", "characterCount": 100},
                ],
            },
            *[
                {"id": f"node-{i:02d}", "tokens": 1000 + i * 10}
                for i in range(15)
            ],
        ]
    }
    tb = extract_token_breakdown({"promptContextUsageTree": tree})
    assert len(tb.top_context_nodes) == 15
    assert tb.top_context_nodes[0]["id"] == "node-14"
    # Verify boolean and invalid fields were stripped on normalized nodes
    tie_Tree = [{"id": "b", "tokens": 10, "chars": 20}, {"id": "a", "tokens": 10, "chars": 20, "estimatedTokens": False}, {"id": "c", "tokens": 10, "chars": 50}]
    tb_tie = extract_token_breakdown({"promptContextUsageTree": tie_Tree})
    assert [n["id"] for n in tb_tie.top_context_nodes] == ["c", "a", "b"]
    assert "estimatedTokens" not in tb_tie.top_context_nodes[1]


def test_parent_terminal_dispatch_overrides_child_in_flight(tmp_path):
    session_id = "0a7d3243-61b6-4b3a-8fc5-43c73e2db583"
    state = create_synthetic_state_db(tmp_path / "state.vscdb", session_id=session_id, bubble_count=2)
    tracking = create_synthetic_tracking_db(tmp_path / "tracking.db", session_id=session_id)
    con = sqlite3.connect(state)
    parent = json.loads(con.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (f"composerData:{session_id}",)).fetchone()[0])
    child_ids = [
        "child-aborted-stale-loading",
        "child-truly-in-flight",
        "child-sync-completed-no-additional",
        "child-bg-still-running",
    ]
    parent["subagentComposerIds"] = child_ids
    con.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(parent), f"composerData:{session_id}"))

    # Bubble 1: parent task_v2 call has status="completed" and additionalData={"status": "aborted"}
    b1_key = f"bubbleId:{session_id}:bubble-001"
    b1 = json.loads(con.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (b1_key,)).fetchone()[0])
    b1["toolFormerData"] = {
        "name": "task_v2",
        "status": "completed",
        "params": {"subagent_type": "explore"},
        "result": {"agentId": "child-aborted-stale-loading"},
        "additionalData": {"status": "aborted"},
    }
    con.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(b1), b1_key))

    # Bubble 2: parent task_v2 call has status="loading"
    b2 = {
        "type": 2,
        "text": "dispatching second subagent",
        "startedAtMs": 1200,
        "completedAtMs": 1250,
        "toolFormerData": {
            "name": "task_v2",
            "status": "loading",
            "params": {"subagent_type": "generalPurpose"},
            "result": {"agentId": "child-truly-in-flight"},
        },
    }
    # Bubble 3: parent task_v2 call has status="completed" and no additionalData (foreground)
    b3 = {
        "type": 2,
        "text": "dispatching third subagent",
        "startedAtMs": 1260,
        "completedAtMs": 1300,
        "toolFormerData": {
            "name": "task_v2",
            "status": "completed",
            "params": {"subagent_type": "explore"},
            "result": {"agentId": "child-sync-completed-no-additional"},
        },
    }
    # Bubble 4: parent task_v2 call has status="completed", additionalData={"status": "success", ...}, and run_in_background=True
    b4 = {
        "type": 2,
        "text": "dispatching background subagent",
        "startedAtMs": 1310,
        "completedAtMs": 1350,
        "toolFormerData": {
            "name": "task_v2",
            "status": "completed",
            "params": {"subagent_type": "generalPurpose", "run_in_background": True},
            "result": {"agentId": "child-bg-still-running"},
            "additionalData": {"status": "success", "subagentComposerId": "child-bg-still-running"},
        },
    }
    con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (f"bubbleId:{session_id}:bubble-002", json.dumps(b2)))
    con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (f"bubbleId:{session_id}:bubble-003", json.dumps(b3)))
    con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (f"bubbleId:{session_id}:bubble-004", json.dumps(b4)))
    con.execute(
        "UPDATE cursorDiskKV SET value = ? WHERE key = ?",
        (
            json.dumps([
                {"bubbleId": "bubble-000", "type": 1},
                {"bubbleId": "bubble-001", "type": 2},
                {"bubbleId": "bubble-002", "type": 2},
                {"bubbleId": "bubble-003", "type": 2},
                {"bubbleId": "bubble-004", "type": 2},
            ]),
            f"composerConvHeaders:{session_id}:000000",
        ),
    )

    for cid in child_ids:
        child_data = {
            "name": cid,
            "createdAt": 1000,
            "lastUpdatedAt": 1150,
            "status": "running",
            "isSubagent": True,
            "parentComposerId": session_id,
        }
        con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (f"composerData:{cid}", json.dumps(child_data)))
        con.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (
                f"bubbleId:{cid}:user",
                json.dumps({
                    "type": 1,
                    "text": "work",
                    "startedAtMs": 1000,
                    "completedAtMs": 1150,
                    "toolFormerData": {"name": "read_file_v2", "status": "loading", "params": {"targetFile": "a.py"}},
                }),
            ),
        )
        con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (f"composerConvHeaders:{cid}:000000", json.dumps([{"bubbleId": "user", "type": 1}])))
    con.commit()
    con.close()

    session = CursorAdapter().extract_session(session_id, ExtractOptions(state_db=state, tracking_db=tracking))
    by_id = {sub.session_id: sub for sub in session.subagents}
    aborted_sub = by_id["child-aborted-stale-loading"]
    assert aborted_sub.in_flight is False
    assert aborted_sub.status == "aborted"
    assert aborted_sub.duration_ms == 150

    live_sub = by_id["child-truly-in-flight"]
    assert live_sub.in_flight is True
    assert live_sub.status is None
    assert live_sub.duration_ms is None

    sync_done_sub = by_id["child-sync-completed-no-additional"]
    assert sync_done_sub.in_flight is False
    assert sync_done_sub.status == "completed"
    assert sync_done_sub.duration_ms == 150

    bg_live_sub = by_id["child-bg-still-running"]
    assert bg_live_sub.in_flight is True
    assert bg_live_sub.status is None
    assert bg_live_sub.duration_ms is None





