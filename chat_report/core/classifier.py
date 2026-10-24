"""Semantic classification and customization inventory for SessionIR."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from chat_report.core.ir import CustomizationRefIR, SessionIR, ToolCallIR


def canonicalize_path(raw_path: str | None, workspace: Path | None = None) -> str:
    """Normalize a path without applying host-dependent filesystem resolution."""
    if not raw_path:
        return ""
    path = str(raw_path).strip()
    parsed = urlparse(path)
    if parsed.scheme.lower() == "file":
        is_unc_host = parsed.netloc and parsed.netloc.lower() != "localhost"
        path = f"//{parsed.netloc}{parsed.path}" if is_unc_host else parsed.path
        path = unquote(path)
    path = path.replace("\\", "/")
    if re.match(r"^/[A-Za-z]:/", path):
        path = path[1:]
    unc = path.startswith("//")
    path = re.sub(r"/+", "/", path)
    if unc:
        path = f"//{path.lstrip('/')}"
    while path.startswith("./"):
        path = path[2:]
    windows_path = bool(re.match(r"^[A-Za-z]:/", path)) or path.startswith("//")
    if windows_path:
        path = path.casefold()
    if workspace:
        root = canonicalize_path(str(workspace), None).rstrip("/")
        if bool(re.match(r"^[A-Za-z]:/", root)) or root.startswith("//"):
            path = path.casefold()
        if path == root:
            return ""
        if root and path.startswith(f"{root}/"):
            return path[len(root) + 1 :]
    return path


def is_skill_path(path: str | None) -> bool:
    value = (path or "").replace("\\", "/").lower()
    return value.endswith("/skill.md") or value == "skill.md"


def is_rule_path(path: str | None) -> bool:
    value = (path or "").replace("\\", "/").lower()
    name = value.rsplit("/", 1)[-1]
    return (
        name.endswith(".mdc")
        or name == ".cursorrules"
        or name in {"agents.md", "claude.md"}
        or "/.cursor/rules/" in f"/{value}"
    )


def is_hook_path(path: str | None) -> bool:
    value = (path or "").replace("\\", "/").lower()
    name = value.rsplit("/", 1)[-1]
    return name == "hooks.json" or "/.cursor/hooks/" in value or value.startswith(".cursor/hooks/")


def _customization_kind(path: str) -> str | None:
    name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if is_skill_path(path) and name == "skill.md":
        return "skill"
    if name in {"agents.md", "claude.md"}:
        return "agent_doc"
    if is_rule_path(path):
        return "rule"
    if is_hook_path(path):
        return "hook"
    return None


def classify_tool_call(tool_call: ToolCallIR) -> str:
    """Return the semantic tool class for a normalized tool call."""
    kind = tool_call.canonical_kind
    if kind == "file_read":
        if is_skill_path(tool_call.target_path):
            return "skill-read"
        if is_rule_path(tool_call.target_path):
            return "rule-read"
        if is_hook_path(tool_call.target_path):
            return "hook-config-read"
        return "native-read"
    if kind in {"search_grep", "search_glob", "search_semantic"}:
        return "native-search"
    if kind in {"file_edit", "file_write", "file_delete"}:
        return "native-edit"
    if kind in {"shell_exec", "shell_await"}:
        return "native-shell"
    if kind == "subagent_dispatch":
        return "subagent-dispatch"
    if kind == "mcp_discovery":
        return "dynamic-tool-discovery"
    if kind == "mcp_call":
        return f"mcp-call:{tool_call.mcp_namespace or 'unknown'}"
    return "agent-workflow"


def _workspace_customizations(workspace: Path) -> list[CustomizationRefIR]:
    candidates: list[tuple[str, str]] = []
    cursor = workspace / ".cursor"
    if cursor.is_dir():
        candidates.extend(("skill", str(p)) for p in cursor.glob("skills/*/SKILL.md"))
        candidates.extend(("rule", str(p)) for p in cursor.glob("rules/**/*.mdc"))
        hooks = cursor / "hooks.json"
        if hooks.is_file():
            candidates.append(("hook", str(hooks)))
    for name in (".cursorrules", "AGENTS.md", "CLAUDE.md"):
        candidate = workspace / name
        if candidate.is_file():
            candidates.append(("agent_doc" if name.lower() in {"agents.md", "claude.md"} else "rule", str(candidate)))
    return [CustomizationRefIR(kind, path, "workspace_active") for kind, path in candidates]


def classify_session(session: SessionIR, workspace: Path | None = None) -> SessionIR:
    """Classify calls and build a stable, deduplicated customization inventory."""
    refs = list(session.customizations)
    for turn in session.turns:
        for tool_call in turn.tool_calls:
            tool_call.semantic_class = classify_tool_call(tool_call)
            kind = _customization_kind(tool_call.target_path or "")
            if kind and not tool_call.is_error:
                if tool_call.canonical_kind == "file_read":
                    refs.append(CustomizationRefIR(kind, tool_call.target_path or "", "explicit_read", turn.turn_index))
                elif tool_call.canonical_kind in {"file_edit", "file_write"}:
                    refs.append(CustomizationRefIR(kind, tool_call.target_path or "", "explicit_edit", turn.turn_index))
    workspace_refs = _workspace_customizations(workspace) if workspace and workspace.is_dir() else []
    refs.extend(workspace_refs)
    ws_by_suffix: dict[tuple[str, str], str] = {}
    for ws_ref in workspace_refs:
        ws_kind = _customization_kind(ws_ref.path) or ws_ref.kind
        ws_path = canonicalize_path(ws_ref.path, workspace)
        if ws_kind == "skill" and ws_path.lower().startswith(".cursor/skills/"):
            ws_by_suffix[(ws_kind, ws_path[len(".cursor/skills/") :].lower())] = ws_path
        elif ws_kind == "rule" and ws_path.lower().startswith(".cursor/rules/"):
            ws_by_suffix[(ws_kind, ws_path[len(".cursor/rules/") :].lower())] = ws_path
    normalized: dict[tuple[str, str, str, int | None], CustomizationRefIR] = {}
    for ref in refs:
        kind = _customization_kind(ref.path) or ref.kind
        path = canonicalize_path(ref.path, workspace)
        if kind:
            path = ws_by_suffix.get((kind, path.lower()), path)
        if kind in {"skill", "rule", "agent_doc", "hook"} and path:
            normalized[(kind, path, ref.source, ref.turn_index)] = CustomizationRefIR(kind, path, ref.source, ref.turn_index)
    session.customizations = sorted(normalized.values(), key=lambda r: (r.path, r.kind, r.source, r.turn_index or -1))
    for turn in session.turns:
        turn.attached_rules = [
            ws_by_suffix.get((_customization_kind(p) or "rule", canonicalize_path(p, workspace).lower()), p)
            for p in turn.attached_rules
        ]
    for subagent in session.subagents:
        subagent.skills_read = sorted({canonicalize_path(p, workspace) for p in subagent.skills_read if is_skill_path(p)})
        subagent.rules_read = sorted({canonicalize_path(p, workspace) for p in subagent.rules_read if is_rule_path(p)})
    return session
