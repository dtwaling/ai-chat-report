"""Semantic classification and customization inventory for SessionIR."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, overload
from urllib.parse import unquote, urlparse

from chat_report.core.ir import CustomizationRefIR, SessionIR, ToolCallIR


_AGENT_SKILL_PATH_RE = re.compile(
    r"^(?P<prefix>.*/)?\.agents/skills/(?P<slug>[^/.][^/]*)(?:/skill)?\.md$",
    re.IGNORECASE,
)
_NON_SKILL_AGENT_STEMS: frozenset[str] = frozenset({"readme", "agents", "claude"})


def _agent_skill_match(path: str | None) -> tuple[str, str] | None:
    if not path:
        return None
    normalized = re.sub(r"/+", "/", str(path).strip().replace("\\", "/"))
    while normalized.startswith("./"):
        normalized = normalized[2:]
    match = _AGENT_SKILL_PATH_RE.match(normalized)
    if not match:
        return None
    slug = match.group("slug")
    if slug.lower() in _NON_SKILL_AGENT_STEMS:
        return None
    return match.group("prefix") or "", slug


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
    return value.endswith("/skill.md") or value == "skill.md" or _agent_skill_match(path) is not None


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
    if is_skill_path(path):
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
    if kind == "web_access":
        return "web-access"
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


def _canonicalize_customization_path(raw_path: str | None, workspace: Path | None = None) -> str:
    path = canonicalize_path(raw_path, workspace)
    agent_skill = _agent_skill_match(path)
    if agent_skill is not None:
        prefix, slug = agent_skill
        return f"{prefix}.cursor/skills/{slug.lower()}/skill.md"
    return path


def _build_ws_by_suffix(
    refs: Iterable[CustomizationRefIR],
    workspace: Path | None = None,
) -> dict[tuple[str, str], str]:
    ws_by_suffix: dict[tuple[str, str], str] = {}
    for ws_ref in refs:
        ws_kind = _customization_kind(ws_ref.path) or ws_ref.kind
        ws_path = _canonicalize_customization_path(ws_ref.path, workspace)
        lower_path = ws_path.lower()
        if ws_kind == "skill":
            if lower_path.startswith(".cursor/skills/"):
                suffix = lower_path[len(".cursor/skills/") :]
                ws_by_suffix.setdefault((ws_kind, suffix), ws_path)
                ws_by_suffix.setdefault((ws_kind, lower_path), ws_path)
                slug = Path(ws_path).parent.name.lower()
                if slug:
                    ws_by_suffix.setdefault((ws_kind, f".agents/skills/{slug}.md"), ws_path)
            elif "/.cursor/skills/" in lower_path:
                idx = lower_path.rfind("/.cursor/skills/")
                suffix = lower_path[idx + len("/.cursor/skills/") :]
                ws_by_suffix.setdefault((ws_kind, suffix), ws_path)
                ws_by_suffix.setdefault((ws_kind, lower_path), ws_path)
        elif ws_kind == "rule" and lower_path.startswith(".cursor/rules/"):
            suffix = lower_path[len(".cursor/rules/") :]
            ws_by_suffix.setdefault((ws_kind, suffix), ws_path)
            ws_by_suffix.setdefault((ws_kind, lower_path), ws_path)
    return ws_by_suffix


@overload
def canonicalize_customizations(
    target: str | None,
    workspace: Path | None = None,
) -> str: ...


@overload
def canonicalize_customizations(
    target: Iterable[CustomizationRefIR],
    workspace: Path | None = None,
) -> list[CustomizationRefIR]: ...


def canonicalize_customizations(
    target: str | Iterable[CustomizationRefIR] | None,
    workspace: Path | None = None,
) -> str | list[CustomizationRefIR]:
    """Canonicalize a customization path or deduplicate a CustomizationRefIR inventory."""
    if target is None or isinstance(target, str):
        path = _canonicalize_customization_path(target, workspace)
        if not path:
            return ""
        kind = _customization_kind(target or "") or _customization_kind(path)
        if kind and workspace and workspace.is_dir():
            ws_by_suffix = _build_ws_by_suffix(_workspace_customizations(workspace), workspace)
            path = ws_by_suffix.get((kind, path.lower()), path)
        return path
    refs = list(target)
    workspace_refs = _workspace_customizations(workspace) if workspace and workspace.is_dir() else []
    ws_by_suffix = _build_ws_by_suffix([*workspace_refs, *refs], workspace)
    normalized: dict[tuple[str, str, str, int | None], CustomizationRefIR] = {}
    for ref in refs:
        kind = _customization_kind(ref.path) or ref.kind
        path = _canonicalize_customization_path(ref.path, workspace)
        if kind:
            path = ws_by_suffix.get((kind, path.lower()), path)
        if kind in {"skill", "rule", "agent_doc", "hook"} and path:
            normalized[(kind, path, ref.source, ref.turn_index)] = CustomizationRefIR(kind, path, ref.source, ref.turn_index)
    return sorted(normalized.values(), key=lambda r: (r.path, r.kind, r.source, r.turn_index or -1))


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
    ws_by_suffix = _build_ws_by_suffix([*workspace_refs, *refs], workspace)
    session.customizations = canonicalize_customizations(refs, workspace)
    for turn in session.turns:
        turn.attached_rules = [
            ws_by_suffix.get((_customization_kind(p) or "rule", canonicalize_path(p, workspace).lower()), p)
            for p in turn.attached_rules
        ]
    for subagent in session.subagents:
        subagent.skills_read = sorted({
            ws_by_suffix.get(("skill", _canonicalize_customization_path(p, workspace).lower()), _canonicalize_customization_path(p, workspace))
            for p in subagent.skills_read
            if is_skill_path(p)
        })
        subagent.rules_read = sorted({canonicalize_path(p, workspace) for p in subagent.rules_read if is_rule_path(p)})
    return session
