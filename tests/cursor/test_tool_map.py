import pytest

from chat_report.adapters.cursor.tool_map import (
    is_benign_nonzero_shell_exit,
    map_tool_call,
    parse_mcp_raw_name,
    resolve_governing_shell_command,
    unwrap_mcp_payload,
)
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


def test_negative_hook_denial_controls_on_short_completed_outputs():
    grep_call = map_tool_call({
        "name": "ripgrep_raw_search",
        "status": "completed",
        "params": {"pattern": "hook"},
        "result": {"output": "line 10: hook script blocked the test in fixture\nline 12: \"agent_message\": \"ok\""},
    }, call_index=1, turn_index=1, bubble_id="b", denial_markers=("agent_message",))
    assert (grep_call.status, grep_call.is_denied, grep_call.is_error) == ("completed", False, False)

    shell_call = map_tool_call({
        "name": "run_terminal_command_v2",
        "status": "completed",
        "params": {"command": "pytest -q"},
        "result": {"exitCode": 0, "output": "test_guard_hook_blocked passed"},
    }, call_index=2, turn_index=1, bubble_id="b")
    assert (shell_call.status, shell_call.is_denied, shell_call.is_error) == ("completed", False, False)

    grep_marker_line = map_tool_call({
        "name": "ripgrep_raw_search",
        "status": "completed",
        "params": {"pattern": "Blocked by preToolUse hook"},
        "result": {"output": 'tool_map.py:273:        "Blocked by preToolUse hook",'},
    }, call_index=3, turn_index=1, bubble_id="b")
    assert (grep_marker_line.status, grep_marker_line.is_denied, grep_marker_line.is_error) == ("completed", False, False)


def test_user_decision_rejected_populates_denial_reason_and_gates_subagent_id():
    call = map_tool_call({
        "name": "read_file_v2",
        "status": "completed",
        "userDecision": "rejected",
        "rawArgs": '{"file_path": "src/secret.py"}',
        "additionalData": {"subagentComposerId": "should-not-attach"},
        "result": {"agentId": "also-not-attach"},
    }, call_index=1, turn_index=1, bubble_id="b")
    assert call.status == "denied"
    assert call.is_denied is True
    assert call.denial_reason == "rejected by user"
    assert call.target_path == "src/secret.py"
    assert call.subagent_session_id is None


def test_expanded_target_paths_tool_kinds_and_mcp_discovery_namespace():
    nb_call = map_tool_call({
        "name": "editnotebook",
        "params": {"target_notebook": "notebooks/demo.ipynb"},
        "result": {},
    }, call_index=1, turn_index=1, bubble_id="b")
    assert nb_call.canonical_kind == CanonicalToolKind.FILE_WRITE.value
    assert nb_call.target_path == "notebooks/demo.ipynb"

    plan_call = map_tool_call({
        "name": "createplan",
        "params": {"working_directory": "d:/repo"},
        "result": {},
    }, call_index=2, turn_index=1, bubble_id="b")
    assert plan_call.canonical_kind == CanonicalToolKind.WORKFLOW_CONTROL.value
    assert plan_call.target_path == "d:/repo"

    lints_call = map_tool_call({
        "name": "ReadLints",
        "params": {"paths": ["src/first.py", "src/second.py"]},
        "result": {},
    }, call_index=3, turn_index=1, bubble_id="b")
    assert lints_call.target_path == "src/first.py"

    disc_call = map_tool_call({
        "name": "GetDynamicTools",
        "params": {"namespace": "plugin-eci-core-lore", "toolName": "repo_grep"},
        "result": {},
    }, call_index=4, turn_index=1, bubble_id="b")
    assert disc_call.canonical_kind == CanonicalToolKind.MCP_DISCOVERY.value
    assert (disc_call.mcp_namespace, disc_call.mcp_tool_name) == ("plugin-eci-core-lore", "repo_grep")


def test_search_grep_and_file_read_output_recovery():
    # ripgrep_raw_search with tool_blob result_text
    rg_blob = map_tool_call(
        {
            "name": "ripgrep_raw_search",
            "status": "completed",
            "params": {"pattern": "SessionIR"},
            "result": None,
            "additionalData": {"totalMatches": 12, "totalFiles": 3},
        },
        call_index=1,
        turn_index=1,
        bubble_id="b",
        tool_blob={"result_text": "chat_report/core/ir.py:198:class SessionIR:"},
    )
    assert rg_blob.output_char_len == len("chat_report/core/ir.py:198:class SessionIR:")
    assert rg_blob.input_params["_total_matches"] == 12
    assert rg_blob.input_params["_total_files"] == 3

    # ripgrep_raw_search fallback to additionalData when totalMatches > 0
    rg_add = map_tool_call(
        {
            "name": "ripgrep_raw_search",
            "status": "completed",
            "params": {"pattern": "SessionIR"},
            "result": None,
            "additionalData": {"totalFiles": 1, "topFiles": [{"uri": "ir.py", "matchCount": 4}]},
        },
        call_index=2,
        turn_index=1,
        bubble_id="b",
    )
    assert rg_add.output_char_len > 0
    assert '"totalMatches": 4' in rg_add.output_summary

    # ripgrep_raw_search with 0 matches stays empty
    rg_zero = map_tool_call(
        {
            "name": "ripgrep_raw_search",
            "status": "completed",
            "params": {"pattern": "NonExistentSymbol"},
            "result": None,
            "additionalData": {"totalMatches": 0, "totalFiles": 0, "topFiles": []},
        },
        call_index=3,
        turn_index=1,
        bubble_id="b",
    )
    assert rg_zero.output_char_len == 0
    assert rg_zero.output_summary == ""

    # read_file_v2 with metadata-only tool_blob does not fabricate >= 40000 H06 spike
    rf_meta = map_tool_call(
        {
            "name": "read_file_v2",
            "status": "completed",
            "params": {"targetFile": "large.bin"},
            "result": {"totalLinesInFile": 5000},
        },
        call_index=4,
        turn_index=1,
        bubble_id="b",
        tool_blob={"result_text": "", "file_size": 150000, "total_lines": 5000, "read_range": {"startLine": 1, "endLine": 100}},
    )
    assert rf_meta.input_params["_file_size"] == 150000
    assert rf_meta.input_params["_read_range"] == {"startLine": 1, "endLine": 100}
    assert 0 < rf_meta.output_char_len < 200
    assert '"fileSize": 150000' in rf_meta.output_summary


def test_resolve_governing_shell_command_and_benign_nonzero_exits():
    assert resolve_governing_shell_command({"command": '& "C:\\Program Files\\Git\\bin\\git.exe" diff --quiet'}) == ("git", ["diff", "--quiet"])
    assert resolve_governing_shell_command({"command": "/usr/local/bin/rg 'a|b;c' src/ | Select-Object -First 5"}) == ("rg", ["a|b;c", "src/"])
    assert resolve_governing_shell_command({"command": "cd /repo && pip3 show pytest || echo fallback"}) == ("pip3", ["show", "pytest"])
    assert resolve_governing_shell_command({
        "command": "Get-Command node | Format-List",
        "parsingResult": {
            "executableCommands": [
                {"name": "fc.exe", "args": [{"value": "/b"}, {"value": "a.txt"}, {"value": "b.txt"}]},
                {"name": "Out-String", "args": []},
            ]
        },
    }) == ("fc", ["/b", "a.txt", "b.txt"])

    for cmd, code in (
        ("fc.exe a.txt b.txt", 1),
        ("rg missing_pattern src/", 1),
        ("pip show some-missing-pkg", 1),
        ("where.exe nonexistent_bin", 1),
        ('& "C:\\Program Files\\Git\\bin\\git.exe" diff --quiet', 1),
        ("robocopy src dst", 3),
    ):
        call = map_tool_call(
            {
                "name": "run_terminal_command_v2",
                "status": "completed",
                "params": {"command": cmd},
                "result": {"exitCode": code, "output": "Comparing files..."},
            },
            call_index=1,
            turn_index=1,
            bubble_id="b",
        )
        assert (call.status, call.is_error) == ("completed", False)
        assert call.input_params["_exit_code"] == code
        assert call.input_params["_benign_nonzero_exit"] is True

    # Real errors with exit code 1 or >= 8 stay error
    for cmd, code, out in (
        ("fc.exe missing.txt b.txt", 1, "FC: cannot open MISSING.TXT - No such file or directory"),
        ("rg '[unclosed' src/", 1, "regex parse error: unclosed character class"),
        ("pytest -q", 1, "1 failed"),
        ("robocopy src dst", 8, "ERROR"),
    ):
        call = map_tool_call(
            {
                "name": "run_terminal_command_v2",
                "status": "completed",
                "params": {"command": cmd},
                "result": {"exitCode": code, "output": out},
            },
            call_index=1,
            turn_index=1,
            bubble_id="b",
        )
        assert (call.status, call.is_error) == ("error", True)
        assert "_benign_nonzero_exit" not in call.input_params

    # Non-integer exitCode (e.g. "SIGKILL") does not raise, sets status == "error", and does not set _exit_code
    sigkill_call = map_tool_call(
        {
            "name": "run_terminal_command_v2",
            "status": "completed",
            "params": {"command": "pytest -q"},
            "result": {"exitCode": "SIGKILL", "output": "Killed"},
        },
        call_index=1,
        turn_index=1,
        bubble_id="b",
    )
    assert (sigkill_call.status, sigkill_call.is_error) == ("error", True)
    assert "_exit_code" not in sigkill_call.input_params
    assert "_benign_nonzero_exit" not in sigkill_call.input_params

    # Multiple native executables guard: >= 2 native executables is NOT reclassified as benign
    multi_native = map_tool_call(
        {
            "name": "run_terminal_command_v2",
            "status": "completed",
            "params": {"command": "pytest -q && rg TODO"},
            "result": {"exitCode": 1, "output": "1 failed"},
        },
        call_index=1,
        turn_index=1,
        bubble_id="b",
    )
    assert (multi_native.status, multi_native.is_error) == ("error", True)
    assert multi_native.input_params["_governing_exe"] == "rg"
    assert multi_native.input_params["_exit_code"] == 1
    assert "_benign_nonzero_exit" not in multi_native.input_params

    # Single native executable with PowerShell cmdlets IS reclassified as benign
    single_native_with_cmdlets = map_tool_call(
        {
            "name": "run_terminal_command_v2",
            "status": "completed",
            "params": {"command": "Get-Command foo | Format-List; pip show bar | Select-Object -First 5"},
            "result": {"exitCode": 1, "output": "WARNING: Package(s) not found: bar"},
        },
        call_index=1,
        turn_index=1,
        bubble_id="b",
    )
    assert (single_native_with_cmdlets.status, single_native_with_cmdlets.is_error) == ("completed", False)
    assert single_native_with_cmdlets.input_params["_governing_exe"] == "pip"
    assert single_native_with_cmdlets.input_params["_exit_code"] == 1
    assert single_native_with_cmdlets.input_params["_benign_nonzero_exit"] is True

    # Navigation prefix (cd D:\repo && rg -n pattern src/) with exitCode: 1 resolves _governing_exe == "rg" and is benign
    cd_and_rg = map_tool_call(
        {
            "name": "run_terminal_command_v2",
            "status": "completed",
            "params": {"command": r"cd D:\repo && rg -n pattern src/"},
            "result": {"exitCode": 1, "output": ""},
        },
        call_index=1,
        turn_index=1,
        bubble_id="b",
    )
    assert cd_and_rg.input_params["_governing_exe"] == "rg"
    assert cd_and_rg.status == "completed"
    assert cd_and_rg.is_error is False
    assert cd_and_rg.input_params["_benign_nonzero_exit"] is True




