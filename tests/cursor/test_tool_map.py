import pytest

from chat_report.adapters.cursor.tool_map import map_tool_call, parse_mcp_raw_name, unwrap_mcp_payload
from chat_report.core.ir import CanonicalToolKind


@pytest.mark.parametrize(("raw_name", "kind"), [
    ("read_file_v2", "file_read"), ("edit_file_v2", "file_edit"), ("write_file_v2", "file_write"),
    ("delete_file_v2", "file_delete"), ("ripgrep_raw_search", "search_grep"), ("glob_file_search", "search_glob"),
    ("codebase_search", "search_semantic"), ("run_terminal_command_v2", "shell_exec"), ("await_shell", "shell_await"),
    ("task_v2", "subagent_dispatch"), ("get_mcp_tools", "mcp_discovery"), ("fetch_mcp_resource", "mcp_resource"),
    ("ask_question", "user_interaction"), ("create_plan", "workflow_control"), ("web_fetch", "web_access"),
    ("unknown", "other"), ("mcp-server-tool", "mcp_call"),
])
def test_maps_all_non_mcp_canonical_kinds(raw_name, kind):
    call = map_tool_call({"name": raw_name, "params": {"targetFile": "x"}, "result": {}},
        call_index=1, turn_index=1, bubble_id="b")
    assert call.canonical_kind == kind


def test_unwraps_mcp_and_reclassifies_cursor_dynamic_tool():
    assert parse_mcp_raw_name("mcp-server-a-tool") == ("server", "a-tool")
    assert parse_mcp_raw_name("mcp-atlassian-rovo-mcp-getJiraIssue") == ("atlassian-rovo-mcp", "getJiraIssue")
    assert parse_mcp_raw_name("mcp-eci-lore-skills_describe") == ("eci-lore", "skills_describe")
    assert unwrap_mcp_payload({"tools": [{"parameters": '{"path":"x"}'}]}) == {"path": "x"}
    call = map_tool_call({"name": "CallDynamicTool", "params": {"namespace": "cursor", "toolName": "Task"},
        "result": {"agentId": "child"}}, call_index=1, turn_index=1, bubble_id="b")
    assert call.canonical_kind == CanonicalToolKind.SUBAGENT_DISPATCH.value
    assert call.input_params["via_dynamic_tool"] is True
    assert call.subagent_session_id == "child"


def test_mcp_envelope_names_take_precedence_over_raw_name():
    call = map_tool_call({
        "name": "mcp-incorrect-name",
        "params": {"tools": [{"serverName": "atlassian-rovo-mcp", "name": "getJiraIssue", "parameters": {}}]},
        "result": {},
    }, call_index=1, turn_index=1, bubble_id="b")
    assert (call.mcp_namespace, call.mcp_tool_name) == ("atlassian-rovo-mcp", "getJiraIssue")


def test_dynamic_mcp_envelopes_preserve_names_and_unwrap_arguments():
    cursor_call = map_tool_call({
        "name": "CallDynamicTool",
        "params": {"tools": [{"serverName": "cursor", "name": "ReadLints", "parameters": '{"path": "src/a.py"}'}]},
        "result": {},
    }, call_index=1, turn_index=1, bubble_id="b")
    assert cursor_call.canonical_kind == CanonicalToolKind.WORKFLOW_CONTROL.value
    assert cursor_call.input_params == {"path": "src/a.py", "via_dynamic_tool": True}
    lore_call = map_tool_call({
        "name": "CallDynamicTool",
        "params": {"tools": [{"serverName": "plugin-eci-core-lore", "name": "repo_grep", "parameters": '{"pattern": "foo"}'}]},
        "result": {},
    }, call_index=2, turn_index=1, bubble_id="b")
    assert lore_call.canonical_kind == CanonicalToolKind.MCP_CALL.value
    assert (lore_call.mcp_namespace, lore_call.mcp_tool_name) == ("plugin-eci-core-lore", "repo_grep")
    assert lore_call.input_params == {"pattern": "foo"}


def test_mcp_unwraps_multi_content_and_discovers_tools():
    call = map_tool_call({
        "name": "mcp-server-tool",
        "params": '{"tools":[{"parameters":"{\\"path\\":\\"src\\"}"}]}',
        "result": {"result": {"content": [{"text": "first"}, {"text": "second"}]}},
    }, call_index=1, turn_index=1, bubble_id="b")
    assert call.input_params == {"path": "src"}
    assert call.output_summary == "first\nsecond"
    discovery = map_tool_call({
        "name": "get_mcp_tools", "params": {"namespace": "server"},
        "result": {"result": {"content": [{"text": '{"tools":["one", {"name":"two"}]}'}]}},
    }, call_index=2, turn_index=1, bubble_id="b")
    assert discovery.input_params["_discovered_tools"] == [("server", "one"), ("server", "two")]


def test_status_priority_prefers_denied_then_cancelled_then_loading():
    denied = map_tool_call({"name": "read_file", "status": "failed", "result": "Auto-review blocked"},
        call_index=1, turn_index=1, bubble_id="b")
    cancelled = map_tool_call({"name": "read_file", "status": "cancelled", "error": "bad"},
        call_index=2, turn_index=1, bubble_id="b")
    loading = map_tool_call({"name": "read_file", "status": "loading", "error": "bad"},
        call_index=3, turn_index=1, bubble_id="b")
    assert (denied.status, denied.is_denied) == ("denied", True)
    assert cancelled.status == "cancelled"
    assert loading.status == "loading"


@pytest.mark.parametrize("result", ["error: broken", '{"error":"broken"}'])
def test_error_payload_prefix_is_error(result):
    call = map_tool_call({"name": "read_file", "result": result},
        call_index=1, turn_index=1, bubble_id="b")
    assert (call.status, call.is_error) == ("error", True)


def test_json_string_result_with_nonzero_exit_code_is_error():
    call = map_tool_call({
        "name": "run_terminal_command_v2",
        "status": "completed",
        "result": '{"output": "fail", "exitCode": 1}',
    }, call_index=1, turn_index=1, bubble_id="b")
    assert (call.status, call.is_error) == ("error", True)


@pytest.mark.parametrize("result", [{"error": None, "output": "ok"}, '{"error": null, "output": "ok"}'])
def test_null_result_error_is_completed(result):
    call = map_tool_call({"name": "read_file", "result": result},
        call_index=1, turn_index=1, bubble_id="b")
    assert (call.status, call.is_error) == ("completed", False)


def test_large_successful_command_quoting_denial_marker_is_not_denied():
    call = map_tool_call({
        "name": "run_terminal_command_v2",
        "status": "completed",
        "result": {"exitCode": 0, "output": "x" * 500 + " Auto-review blocked " + "y" * 500},
    }, call_index=1, turn_index=1, bubble_id="b")
    assert (call.status, call.is_denied, call.is_error) == ("completed", False, False)


def test_explicit_hook_rejection_and_large_non_rejection():
    rejected = map_tool_call({
        "name": "run_terminal_command_v2",
        "result": {"rejected": True, "output": "Rejected: Command execution was blocked by a hook: policy"},
    }, call_index=1, turn_index=1, bubble_id="b")
    assert (rejected.status, rejected.is_denied) == ("denied", True)
    not_rejected = map_tool_call({
        "name": "run_terminal_command_v2",
        "result": {"rejected": False, "exitCode": 1, "output": "x" * 500 + "Blocked by preToolUse hook"},
    }, call_index=2, turn_index=1, bubble_id="b")
    assert (not_rejected.status, not_rejected.is_denied) == ("error", False)
