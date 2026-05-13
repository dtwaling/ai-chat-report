"""Tool-name -> locked-shape tool_class mapping for Claude Code.

DISCOVERY.md section 2 ("Implementation considerations -- tool-class
mapping is direct") enumerates the mapping. This module codifies it.

Counterpart to ``cursor.classify_tool_class_cursor`` in the cursor variant;
the two are deliberately IDE-specific because the source vocabulary differs
(Cursor: ``read_file_v2``, ``run_terminal_command_v2``, etc.; Claude Code:
``Read``, ``Bash``, etc.).

The function exposes the same signature shape as cursor's classifier
(``classify_tool_class_claudecode(tool_name, tool_input)``) so the report
layer (commit 5+ of this PR) can stay IDE-agnostic at the call site.
"""

from __future__ import annotations

from typing import Any


# Direct name -> tool_class mapping. Any tool-name not in this table and
# not matching the optimus-MCP prefix pattern falls through to "other".
_TOOL_NAME_TO_CLASS: dict[str, str] = {
    # Read tools: base class is broad-sweep-read; the dir-index special-
    # case is applied downstream by inspecting input.file_path.
    "Read": "broad-sweep-read",
    # Search tools.
    "Grep": "broad-sweep-grep",
    "Glob": "broad-sweep-glob",
    # Shell.
    "Bash": "bash",
    # Edits.
    "Edit": "edit",
    "MultiEdit": "edit",
    # Writes.
    "Write": "write",
}


def _file_path_from_input(tool_input: Any) -> str:
    """Best-effort extraction of the file-path arg from a tool_use.input dict.

    Claude Code's stable naming is ``file_path``; some tools (e.g. MCP
    servers) may use alternate spellings, so we probe a small set without
    introducing an order dependency on un-namespaced reads.
    """
    if not isinstance(tool_input, dict):
        return ""
    for key in ("file_path", "path", "filePath", "uri"):
        v = tool_input.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def _is_optimus_mcp_tool(name: str) -> bool:
    """True iff ``name`` is an optimus-MCP-server-exposed tool.

    Per DISCOVERY.md: MCP tools are named ``mcp__<server-id>__<tool-name>``.
    An optimus-MCP tool is any MCP-prefixed tool whose server-id segment
    contains "optimus" (case-insensitive). This catches plausibly-renamed
    server IDs without locking the chat-report tool to a single hardcoded
    server name.
    """
    if not name.startswith("mcp__"):
        return False
    parts = name.split("__")
    # Format: ["mcp", "<server-id>", "<tool-name>", ...] -- minimum 3 parts.
    if len(parts) < 3:
        return False
    server_id = parts[1].lower()
    return "optimus" in server_id


def classify_tool_class_claudecode(tool_name: str, tool_input: Any = None) -> str:
    """Map a Claude Code tool-call to the locked tool_class enum.

    The mapping (DISCOVERY.md section 2):

    | Tool name           | tool_class                              |
    |---------------------|-----------------------------------------|
    | Read                | broad-sweep-read OR directory-index-read|
    | Grep                | broad-sweep-grep                        |
    | Glob                | broad-sweep-glob                        |
    | Bash                | bash                                    |
    | Edit / MultiEdit    | edit                                    |
    | Write               | write                                   |
    | mcp__*optimus*__*   | optimus-mcp                             |
    | (everything else)   | other                                   |

    Read is special: when ``tool_input.file_path`` ends with
    ``DIRECTORY_INDEX.md`` (path separators normalized), the call is the
    directory-index consult that the informed-precision-read heuristic
    looks for, classified as ``directory-index-read``. The match is
    case-sensitive on the filename per the canonical name.
    """
    name = (tool_name or "").strip()
    if not name:
        return "other"

    if name == "Read":
        path = _file_path_from_input(tool_input)
        if path and path.replace("\\", "/").endswith("DIRECTORY_INDEX.md"):
            return "directory-index-read"
        return "broad-sweep-read"

    base = _TOOL_NAME_TO_CLASS.get(name)
    if base is not None:
        return base

    if _is_optimus_mcp_tool(name):
        return "optimus-mcp"

    return "other"
