"""Normalize Cursor toolFormerData payloads into ToolCallIR records."""

from __future__ import annotations

import json
import re
from typing import Any

from chat_report.adapters.cursor.hooks import detect_denial
from chat_report.core.ir import CanonicalToolKind, ToolCallIR


_TOOL_KINDS: dict[str, CanonicalToolKind] = {
    "read_file_v2": CanonicalToolKind.FILE_READ, "read_file": CanonicalToolKind.FILE_READ, "read": CanonicalToolKind.FILE_READ,
    "edit_file_v2": CanonicalToolKind.FILE_EDIT, "edit_file": CanonicalToolKind.FILE_EDIT, "search_replace": CanonicalToolKind.FILE_EDIT, "strreplace": CanonicalToolKind.FILE_EDIT, "apply_patch": CanonicalToolKind.FILE_EDIT,
    "write_file_v2": CanonicalToolKind.FILE_WRITE, "write_file": CanonicalToolKind.FILE_WRITE, "create_file": CanonicalToolKind.FILE_WRITE, "edit_notebook": CanonicalToolKind.FILE_WRITE, "editnotebook": CanonicalToolKind.FILE_WRITE, "write": CanonicalToolKind.FILE_WRITE,
    "delete_file_v2": CanonicalToolKind.FILE_DELETE, "delete_file": CanonicalToolKind.FILE_DELETE, "delete": CanonicalToolKind.FILE_DELETE,
    "ripgrep_raw_search": CanonicalToolKind.SEARCH_GREP, "grep_search": CanonicalToolKind.SEARCH_GREP, "rg": CanonicalToolKind.SEARCH_GREP, "grep": CanonicalToolKind.SEARCH_GREP,
    "glob_file_search": CanonicalToolKind.SEARCH_GLOB, "file_search": CanonicalToolKind.SEARCH_GLOB, "list_dir": CanonicalToolKind.SEARCH_GLOB, "list_dir_v2": CanonicalToolKind.SEARCH_GLOB, "glob": CanonicalToolKind.SEARCH_GLOB,
    "codebase_search": CanonicalToolKind.SEARCH_SEMANTIC, "semantic_search": CanonicalToolKind.SEARCH_SEMANTIC,
    "run_terminal_command_v2": CanonicalToolKind.SHELL_EXEC, "run_terminal_cmd": CanonicalToolKind.SHELL_EXEC, "shell": CanonicalToolKind.SHELL_EXEC,
    "await_terminal_command": CanonicalToolKind.SHELL_AWAIT, "await_shell": CanonicalToolKind.SHELL_AWAIT, "awaitshell": CanonicalToolKind.SHELL_AWAIT,
    "task_v2": CanonicalToolKind.SUBAGENT_DISPATCH, "task": CanonicalToolKind.SUBAGENT_DISPATCH,
    "get_mcp_tools": CanonicalToolKind.MCP_DISCOVERY, "getdynamictools": CanonicalToolKind.MCP_DISCOVERY,
    "fetch_mcp_resource": CanonicalToolKind.MCP_RESOURCE, "read_mcp_resource": CanonicalToolKind.MCP_RESOURCE, "fetchmcpresource": CanonicalToolKind.MCP_RESOURCE,
    "ask_question": CanonicalToolKind.USER_INTERACTION, "send_to_call": CanonicalToolKind.USER_INTERACTION, "askquestion": CanonicalToolKind.USER_INTERACTION, "sendtocall": CanonicalToolKind.USER_INTERACTION,
    "create_plan": CanonicalToolKind.WORKFLOW_CONTROL, "createplan": CanonicalToolKind.WORKFLOW_CONTROL, "switch_mode": CanonicalToolKind.WORKFLOW_CONTROL, "read_lints": CanonicalToolKind.WORKFLOW_CONTROL, "todo_write": CanonicalToolKind.WORKFLOW_CONTROL, "create_goal": CanonicalToolKind.WORKFLOW_CONTROL, "update_goal": CanonicalToolKind.WORKFLOW_CONTROL, "update_current_step": CanonicalToolKind.WORKFLOW_CONTROL, "switchmode": CanonicalToolKind.WORKFLOW_CONTROL, "readlints": CanonicalToolKind.WORKFLOW_CONTROL, "todowrite": CanonicalToolKind.WORKFLOW_CONTROL, "creategoal": CanonicalToolKind.WORKFLOW_CONTROL, "updategoal": CanonicalToolKind.WORKFLOW_CONTROL, "updatecurrentstep": CanonicalToolKind.WORKFLOW_CONTROL,
    "web_fetch": CanonicalToolKind.WEB_ACCESS, "web_search": CanonicalToolKind.WEB_ACCESS, "generate_image": CanonicalToolKind.WEB_ACCESS, "webfetch": CanonicalToolKind.WEB_ACCESS, "websearch": CanonicalToolKind.WEB_ACCESS, "generateimage": CanonicalToolKind.WEB_ACCESS,
}
KNOWN_MCP_SERVER_PREFIXES: tuple[str, ...] = (
    "atlassian-rovo-mcp", "cursor-ide-browser", "plugin-eci-core-lore",
    "plugin-atlassian-atlassian", "plugin-postman-postman", "plugin-nexus-nexus",
    "plugin-context7-context7", "user-lore", "cursor-origin",
)
_POWERSHELL_CMDLETS: frozenset[str] = frozenset({
    "get-command", "format-list", "format-table", "select-object", "where-object",
    "foreach-object", "sort-object", "measure-object", "out-string", "out-null",
    "get-childitem", "get-content", "get-item", "test-path", "write-output",
    "write-host", "select-string", "cd", "chdir", "sl", "set-location",
    "pushd", "popd", "push-location", "pop-location", "echo", "pwd", "get-location",
})


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _exe_basename(raw_token: str) -> str:
    cleaned = raw_token.strip().strip("\"'")
    name = cleaned.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _split_shell_tokens(segment: str) -> list[str]:
    return [tok.strip("\"'") for tok in re.findall(r'"[^"]*"|\'[^\']*\'|\S+', segment)]


def _split_top_level_segments(command: str) -> list[str]:
    segments: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if ch == "'" and not in_double:
            in_single = not in_single
            buf.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            buf.append(ch)
            i += 1
            continue
        if not in_single and not in_double:
            if command.startswith("&&", i) or command.startswith("||", i):
                seg = "".join(buf).strip()
                if seg:
                    segments.append(seg)
                buf.clear()
                i += 2
                continue
            if ch in (";", "|"):
                seg = "".join(buf).strip()
                if seg:
                    segments.append(seg)
                buf.clear()
                i += 1
                continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        segments.append(tail)
    return segments


def _native_shell_commands(params: dict[str, Any]) -> list[tuple[str, list[str]]]:
    parsing = params.get("parsingResult")
    if isinstance(parsing, dict):
        exec_cmds = parsing.get("executableCommands")
        if isinstance(exec_cmds, list):
            native = [
                (
                    _exe_basename(str(entry["name"])),
                    [
                        str(a.get("value") or "").strip("\"'")
                        for a in (entry.get("args") or [])
                        if isinstance(a, dict)
                    ],
                )
                for entry in exec_cmds
                if isinstance(entry, dict)
                and _exe_basename(str(entry.get("name") or ""))
                and _exe_basename(str(entry.get("name") or "")) not in _POWERSHELL_CMDLETS
            ]
            if native:
                return native
    matches: list[tuple[str, list[str]]] = []
    for seg in _split_top_level_segments(str(params.get("command") or "")):
        tokens = _split_shell_tokens(seg)
        while tokens and tokens[0] in ("&", "."):
            tokens = tokens[1:]
        if not tokens:
            continue
        if tokens[0].startswith("$"):
            continue
        base = _exe_basename(tokens[0])
        if not base or base in _POWERSHELL_CMDLETS:
            continue
        matches.append((base, tokens[1:]))
    return matches


def resolve_governing_shell_command(params: dict[str, Any]) -> tuple[str, list[str]]:
    native = _native_shell_commands(params)
    return native[-1] if native else ("", [])


def is_benign_nonzero_shell_exit(
    params: dict[str, Any],
    unwrapped_result: Any,
    *,
    raw_status: str = "",
    error_value: Any = None,
) -> bool:
    if (
        raw_status in ("error", "failed")
        or bool(error_value)
        or not isinstance(unwrapped_result, dict)
        or unwrapped_result.get("rejected") is True
    ):
        return False
    exit_code = unwrapped_result.get("exitCode")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code == 0:
        return False
    if any(unwrapped_result.get(key) for key in ("error", "isError", "Error")):
        return False
    native_cmds = _native_shell_commands(params)
    if len(native_cmds) != 1:
        return False
    base, args = native_cmds[0]
    lower_args = [a.lower() for a in args]
    out_str = str(unwrapped_result.get("output") or "")
    if base in ("fc", "diff", "cmp") and exit_code == 1 and not re.search(r"cannot open|no such file|cannot find", out_str, re.I):
        return True
    if base in ("rg", "ripgrep", "grep", "egrep", "fgrep", "findstr") and exit_code == 1 and not re.search(r"regex parse error|syntax error|no such file|cannot find", out_str, re.I):
        return True
    if base in ("pip", "pip3") and len(lower_args) >= 1 and lower_args[0] == "show" and exit_code == 1:
        return True
    if base in ("where", "which", "command") and exit_code == 1:
        return True
    if base == "git" and "diff" in lower_args and any(f in lower_args for f in ("--quiet", "--exit-code")) and exit_code == 1:
        return True
    if base == "robocopy" and 0 < exit_code < 8:
        return True
    return False


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def unwrap_mcp_payload(payload: Any) -> Any:
    """Decode Cursor's nested MCP parameter/result payload, including JSON strings."""
    value = _json(payload)
    if not isinstance(value, dict):
        return value
    tools = _json(value.get("tools"))
    if isinstance(tools, list) and tools and isinstance(_json(tools[0]), dict):
        parameters = _json(_json(tools[0]).get("parameters"))
        if parameters is not None:
            return parameters
    result = _json(value.get("result"))
    if not isinstance(result, dict):
        result = value
    if isinstance(result, dict):
        content = _json(result.get("content"))
        if isinstance(content, list):
            texts = [
                str(text)
                for item in content
                if isinstance(_json(item), dict)
                for text in [_json(item).get("text")]
                if text is not None
            ]
            if texts:
                return _json("\n".join(texts))
    return value


def parse_mcp_raw_name(raw_name: str) -> tuple[str | None, str | None]:
    """Parse Cursor's mcp-namespace-tool or mcp_namespace_tool naming forms."""
    raw = (raw_name or "").strip()
    if raw.startswith("mcp-") or raw.startswith("mcp_"):
        remainder = raw[4:]
        for prefix in KNOWN_MCP_SERVER_PREFIXES:
            if remainder.startswith(f"{prefix}-") or remainder.startswith(f"{prefix}_"):
                return prefix, remainder[len(prefix) + 1:]
        if "-" in remainder:
            underscore_index = remainder.find("_")
            hyphens = [match.start() for match in re.finditer("-", remainder)]
            if underscore_index >= 0:
                candidates = [index for index in hyphens if index < underscore_index]
                if candidates:
                    split_at = candidates[-1]
                    return remainder[:split_at], remainder[split_at + 1:]
            split_at = remainder.index("-")
            return remainder[:split_at], remainder[split_at + 1:]
        if "_" in remainder:
            namespace, tool = remainder.split("_", 1)
            return namespace, tool
    return None, None


def _as_dict(value: Any) -> dict[str, Any]:
    decoded = _json(value)
    return decoded if isinstance(decoded, dict) else {}


def _mcp_envelope_names(params: dict[str, Any]) -> tuple[str | None, str | None]:
    tools = _json(params.get("tools"))
    if isinstance(tools, list) and tools:
        envelope = _as_dict(tools[0])
        return (
            _first_str(envelope, ("serverName", "namespace")),
            _first_str(envelope, ("name", "toolName")),
        )
    return None, None


def _first_str(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _is_structured_denial_payload(unwrapped_result: Any, result_text: str, kind: CanonicalToolKind) -> tuple[bool, str | None]:
    if kind in (CanonicalToolKind.FILE_READ, CanonicalToolKind.SEARCH_GREP, CanonicalToolKind.SEARCH_GLOB):
        return False, None
    prefixes = ("Blocked by preToolUse hook", "Auto-review blocked")
    if isinstance(unwrapped_result, dict):
        if unwrapped_result.get("permissionDecision") == "deny":
            reason = _first_str(unwrapped_result, ("reason", "message", "agent_message", "error")) or '"permissionDecision":"deny"'
            return True, reason[:160]
        hook_out = unwrapped_result.get("hookSpecificOutput")
        if isinstance(hook_out, dict) and hook_out.get("permissionDecision") == "deny":
            reason = _first_str(hook_out, ("reason", "message", "agent_message")) or '"permissionDecision":"deny"'
            return True, reason[:160]
        for key in ("output", "error", "message", "content"):
            val = unwrapped_result.get(key)
            if isinstance(val, str):
                stripped = val.lstrip()
                for prefix in prefixes:
                    if stripped.startswith(prefix):
                        return True, prefix
    stripped_text = result_text.lstrip()
    for prefix in prefixes:
        if stripped_text.startswith(prefix):
            return True, prefix
    if stripped_text.startswith(('{"permissionDecision":"deny"', '{"permissionDecision": "deny"')):
        return True, '"permissionDecision":"deny"'
    return False, None
def _error_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _discovered_tools(value: Any, params: dict[str, Any]) -> list[tuple[str, str]]:
    """Extract discovered namespace/tool pairs from Cursor MCP catalog shapes."""
    decoded = unwrap_mcp_payload(value)
    sources = [decoded] if isinstance(decoded, dict) else []
    pairs: list[tuple[str, str]] = []
    for source in sources:
        namespace = _first_str(source, ("namespace", "server")) or _first_str(params, ("namespace", "server")) or ""
        tool_name = _first_str(source, ("toolName", "tool_name", "name")) or _first_str(params, ("toolName", "tool_name")) or ""
        if tool_name or (namespace and not any(field in source for field in ("tools", "matches", "namespaces"))):
            pairs.append((namespace, tool_name))
        for field in ("tools", "matches"):
            entries = _json(source.get(field))
            if isinstance(entries, list):
                for entry in entries:
                    entry = _json(entry)
                    if isinstance(entry, dict):
                        pairs.append((
                            _first_str(entry, ("namespace", "server")) or namespace,
                            _first_str(entry, ("toolName", "tool_name", "name")) or "",
                        ))
                    elif isinstance(entry, str):
                        pairs.append((namespace, entry))
        namespaces = _json(source.get("namespaces"))
        if isinstance(namespaces, dict):
            for ns, tools in namespaces.items():
                if isinstance(tools, list):
                    pairs.extend((str(ns), str(tool)) for tool in tools if isinstance(tool, str))
                else:
                    pairs.append((str(ns), ""))
        elif isinstance(namespaces, list):
            pairs.extend((str(ns), "") for ns in namespaces if isinstance(ns, str))
    return list(dict.fromkeys(pair for pair in pairs if pair[0] or pair[1]))


def map_tool_call(
    tool_former_data: dict[str, Any],
    *,
    call_index: int,
    turn_index: int,
    bubble_id: str,
    started_at_ms: int | None = None,
    completed_at_ms: int | None = None,
    denial_markers: tuple[str, ...] = (),
    tool_blob: dict[str, Any] | None = None,
) -> ToolCallIR:
    """Map one Cursor toolFormerData object to a canonical tool-call record."""
    tfd = tool_former_data if isinstance(tool_former_data, dict) else {}
    raw_name = str(tfd.get("name") or "")
    params = _as_dict(tfd.get("params"))
    if not params:
        params = _as_dict(tfd.get("rawArgs"))
    result_raw = tfd.get("result")
    result = _as_dict(result_raw)
    namespace, mcp_tool = parse_mcp_raw_name(raw_name)
    envelope_namespace, envelope_tool = _mcp_envelope_names(params)
    if envelope_namespace:
        namespace = envelope_namespace
    if envelope_tool:
        mcp_tool = envelope_tool
    is_dynamic_mcp_call = raw_name.lower() in ("calldynamictool", "call_mcp_tool")
    if is_dynamic_mcp_call:
        namespace = _first_str(params, ("namespace", "server")) or namespace
        mcp_tool = _first_str(params, ("toolName", "tool_name", "name")) or mcp_tool
    kind = _TOOL_KINDS.get(raw_name.lower())
    via_dynamic = False
    if kind is None and namespace is not None:
        if namespace.lower() == "cursor" and mcp_tool:
            kind = _TOOL_KINDS.get(mcp_tool.lower(), CanonicalToolKind.OTHER)
            via_dynamic = kind is not CanonicalToolKind.OTHER
        else:
            kind = CanonicalToolKind.MCP_CALL
    if is_dynamic_mcp_call and kind is None:
        kind = CanonicalToolKind.MCP_CALL
    if kind is None:
        kind = CanonicalToolKind.OTHER
    if kind is CanonicalToolKind.MCP_CALL or via_dynamic:
        dynamic_arguments = _as_dict(params.get("arguments")) if is_dynamic_mcp_call else {}
        params = _as_dict(unwrap_mcp_payload(params))
        if dynamic_arguments:
            params = {**params, **dynamic_arguments}
        unwrapped_result = unwrap_mcp_payload(result_raw)
        result = _as_dict(unwrapped_result)
    else:
        unwrapped_result = _json(result_raw)
        result = _as_dict(unwrapped_result)
    if via_dynamic:
        params["via_dynamic_tool"] = True
    if kind is CanonicalToolKind.MCP_DISCOVERY:
        namespace = namespace or _first_str(params, ("namespace", "server"))
        mcp_tool = mcp_tool or _first_str(params, ("toolName", "tool_name", "name"))
        params["_discovered_tools"] = _discovered_tools(result_raw, params)
    target = _first_str(
        params,
        (
            "relativeWorkspacePath",
            "targetFile",
            "target_file",
            "filePath",
            "file_path",
            "target_notebook",
            "targetNotebook",
            "path",
            "effectiveUri",
            "targetDirectory",
            "target_directory",
            "working_directory",
            "workingDirectory",
        ),
    )
    if target is None:
        paths_val = params.get("paths")
        if isinstance(paths_val, list) and paths_val and isinstance(paths_val[0], str) and paths_val[0]:
            target = paths_val[0]
    additional = _as_dict(tfd.get("additionalData"))
    if "totalMatches" in additional or "totalFiles" in additional:
        params["_total_matches"] = _int(additional.get("totalMatches"))
        params["_total_files"] = _int(additional.get("totalFiles"))
    result_text = _error_text(unwrapped_result)
    if kind is CanonicalToolKind.SEARCH_GREP and unwrapped_result is None:
        if tool_blob and tool_blob.get("result_text"):
            result_text = str(tool_blob["result_text"])
        elif "totalMatches" in additional or "topFiles" in additional:
            top_files = additional.get("topFiles") if isinstance(additional.get("topFiles"), list) else []
            total_matches = _int(additional.get("totalMatches"))
            if total_matches is None and top_files:
                total_matches = sum(_int(f.get("matchCount")) or 0 for f in top_files if isinstance(f, dict))
            if total_matches is not None and total_matches > 0:
                result_text = json.dumps(
                    {
                        "totalFiles": additional.get("totalFiles"),
                        "totalMatches": total_matches,
                        "topFiles": top_files,
                    },
                    sort_keys=True,
                )
            else:
                result_text = ""
    if (
        kind is CanonicalToolKind.FILE_READ
        and isinstance(unwrapped_result, dict)
        and "contents" not in unwrapped_result
        and "content" not in unwrapped_result
        and tool_blob is not None
    ):
        blob_text = str(tool_blob.get("result_text") or "")
        file_size = tool_blob.get("file_size")
        total_lines = tool_blob.get("total_lines") or unwrapped_result.get("totalLinesInFile")
        if isinstance(file_size, int) and not isinstance(file_size, bool):
            params["_file_size"] = file_size
        if isinstance(tool_blob.get("read_range"), dict):
            params["_read_range"] = tool_blob["read_range"]
        if blob_text:
            result_text = blob_text
        elif isinstance(file_size, int) and not isinstance(file_size, bool) and file_size > 0:
            result_text = json.dumps(
                {
                    "fileSize": file_size,
                    "readRange": tool_blob.get("read_range"),
                    "totalLinesInFile": total_lines,
                },
                sort_keys=True,
            )
    raw_status = str(tfd.get("status") or "").lower()
    error_value = tfd.get("error")
    has_nonzero_exit = isinstance(unwrapped_result, dict) and unwrapped_result.get("exitCode") not in (None, 0)
    if kind is CanonicalToolKind.SHELL_EXEC:
        base_exe, _ = resolve_governing_shell_command(params)
        if base_exe:
            params["_governing_exe"] = base_exe
        if has_nonzero_exit:
            exit_code_int = _int(unwrapped_result.get("exitCode"))
            if exit_code_int is not None and exit_code_int != 0:
                params["_exit_code"] = exit_code_int
            if is_benign_nonzero_shell_exit(params, unwrapped_result, raw_status=raw_status, error_value=error_value):
                has_nonzero_exit = False
                params["_benign_nonzero_exit"] = True
    has_result_error = isinstance(unwrapped_result, dict) and any(
        unwrapped_result.get(key) for key in ("error", "isError", "Error")
    )
    error_prefix = isinstance(unwrapped_result, str) and result_text.lstrip()[:80].lower().startswith(
        ('{"error"', '{"iserror":true', "error:")
    )
    explicit_rejected = unwrapped_result.get("rejected") if isinstance(unwrapped_result, dict) else None
    is_completed_file_read = (
        kind is CanonicalToolKind.FILE_READ
        and raw_status in ("completed", "")
        and not error_value
        and not has_result_error
        and isinstance(unwrapped_result, dict)
        and "contents" in unwrapped_result
    )
    has_error_indicator = (
        raw_status in ("error", "failed")
        or bool(error_value)
        or has_nonzero_exit
        or has_result_error
        or error_prefix
    )
    is_clean_completed = raw_status in ("completed", "") and not has_error_indicator and explicit_rejected is not True
    structured_hit, structured_reason = (
        _is_structured_denial_payload(unwrapped_result, result_text, kind)
        if explicit_rejected is None and len(result_text) <= 400
        else (False, None)
    )
    denial_eligible = has_error_indicator or structured_hit
    if explicit_rejected is True:
        is_denied, denial_reason = True, (
            _first_str(unwrapped_result, ("output", "error", "message")) or "rejected"
        )[:160]
    elif is_completed_file_read or not denial_eligible or (explicit_rejected is False and len(result_text) > 400):
        is_denied, denial_reason = False, None
    elif is_clean_completed:
        is_denied, denial_reason = structured_hit, structured_reason
    else:
        scan_text = result_text[:400] if len(result_text) > 400 and raw_status in ("completed", "") else result_text
        is_denied, denial_reason = detect_denial(scan_text, denial_markers) if denial_markers else detect_denial(scan_text)
    if is_denied or str(tfd.get("userDecision") or "").lower() == "rejected":
        denial_reason = denial_reason or "rejected by user"
        status, is_error, error_message = "denied", True, denial_reason
    elif raw_status in ("cancelled", "canceled", "aborted"):
        status, is_error, error_message = "cancelled", False, None
    elif raw_status == "loading":
        status, is_error, error_message = "loading", False, None
    elif raw_status in ("error", "failed") or error_value or has_nonzero_exit or has_result_error or error_prefix:
        status, is_error = "error", True
        error_message = _error_text(error_value or unwrapped_result)[:1000]
    else:
        status, is_error, error_message = "completed", False, None
    elapsed = completed_at_ms - started_at_ms if isinstance(started_at_ms, int) and isinstance(completed_at_ms, int) and completed_at_ms >= started_at_ms else None
    subagent_id: str | None = None
    if kind is CanonicalToolKind.SUBAGENT_DISPATCH:
        additional = _as_dict(tfd.get("additionalData"))
        subagent_id = _first_str(additional, ("subagentComposerId",))
        if not subagent_id:
            subagent_id = _first_str(result, ("agentId",))
    return ToolCallIR(
        call_index=call_index, turn_index=turn_index, bubble_id=bubble_id,
        tool_call_id=_first_str(tfd, ("toolCallId",)), model_call_id=_first_str(tfd, ("modelCallId",)),
        raw_tool_name=raw_name, canonical_kind=kind.value, semantic_class="", mcp_namespace=namespace,
        mcp_tool_name=mcp_tool, target_path=target, input_params=params,
        input_summary=_error_text(params)[:1000], output_summary=result_text[:1000],
        output_char_len=len(result_text), status=status, is_error=is_error, is_denied=status == "denied",
        denial_reason=denial_reason, error_message=error_message, started_at_ms=started_at_ms,
        completed_at_ms=completed_at_ms, elapsed_ms=elapsed, subagent_session_id=subagent_id,
        offloaded_before_id=_first_str(result, ("beforeContentId",)),
        offloaded_after_id=_first_str(result, ("afterContentId",)),
    )
