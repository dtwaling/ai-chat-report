"""Harness-agnostic Intermediate Representation (SessionIR) and enums for AI Chat Report v2.0."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from chat_report import SCHEMA_VERSION


class CanonicalToolKind(str, Enum):
    """Normalized functional category for any tool call across harnesses."""

    FILE_READ = "file_read"
    FILE_EDIT = "file_edit"
    FILE_WRITE = "file_write"
    FILE_DELETE = "file_delete"
    SEARCH_GREP = "search_grep"
    SEARCH_GLOB = "search_glob"
    SEARCH_SEMANTIC = "search_semantic"
    SHELL_EXEC = "shell_exec"
    SHELL_AWAIT = "shell_await"
    SUBAGENT_DISPATCH = "subagent_dispatch"
    MCP_DISCOVERY = "mcp_discovery"
    MCP_CALL = "mcp_call"
    MCP_RESOURCE = "mcp_resource"
    USER_INTERACTION = "user_interaction"
    WORKFLOW_CONTROL = "workflow_control"
    WEB_ACCESS = "web_access"
    OTHER = "other"


class SemanticToolClass(str, Enum):
    """High-level semantic intent populated by classifier.py."""

    SKILL_READ = "skill-read"
    RULE_READ = "rule-read"
    HOOK_CONFIG_READ = "hook-config-read"
    NATIVE_READ = "native-read"
    NATIVE_SEARCH = "native-search"
    NATIVE_EDIT = "native-edit"
    NATIVE_SHELL = "native-shell"
    SUBAGENT_DISPATCH = "subagent-dispatch"
    DYNAMIC_TOOL_DISCOVERY = "dynamic-tool-discovery"
    MCP_CALL = "mcp-call"
    AGENT_WORKFLOW = "agent-workflow"


CANONICAL_TOOL_KINDS: frozenset[str] = frozenset(k.value for k in CanonicalToolKind)
SEMANTIC_TOOL_CLASSES: frozenset[str] = frozenset(c.value for c in SemanticToolClass)
TOOL_CALL_STATUSES: tuple[str, ...] = ("completed", "error", "denied", "cancelled", "loading")
CUSTOMIZATION_KINDS: frozenset[str] = frozenset(("skill", "rule", "agent_doc", "hook"))
CUSTOMIZATION_SOURCES: frozenset[str] = frozenset(
    ("context_attached", "explicit_read", "explicit_edit", "workspace_active")
)
RESOLVED_SOURCES: frozenset[str] = frozenset(
    ("latest", "chat-id", "request-id", "request-id-scan")
)
SEVERITY_LEVELS: tuple[str, ...] = ("high", "medium", "low", "info")
SEVERITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2, "info": 3}

HEURISTIC_IDS: tuple[str, ...] = (
    "H01_UNREAD_ATTACHED_OR_MENTIONED_SKILL",
    "H02_BLIND_DYNAMIC_TOOL_CALL",
    "H03_TOOL_RETRY_THRASH",
    "H04_HOOK_OR_HARNESS_DENIAL",
    "H05_EDIT_LINT_THRASH",
    "H06_PROMPT_OR_TOOL_TOKEN_SPIKE",
    "H07_SUBAGENT_DISPATCH_ANOMALY",
    "H08_INSTRUCTION_FRICTION_SIGNAL",
)


def normalize_session_id(raw_id: str) -> str:
    """Return canonical lowercase stripped session identifier."""
    return raw_id.strip().lower()


@dataclass
class ResolvedTarget:
    """Result of resolving a user-supplied or latest session selector."""

    session_id: str
    input_id: str
    source: str  # "latest" | "chat-id" | "request-id" | "request-id-scan"

    def to_dict(self) -> dict[str, str]:
        return {
            "session_id": self.session_id,
            "input_id": self.input_id,
            "source": self.source,
        }


@dataclass
class TokenBreakdownIR:
    total_context_tokens_used: int | None
    context_token_limit: int | None
    context_usage_percent: float | None
    by_category: dict[str, dict[str, int]] = field(default_factory=dict)
    top_context_nodes: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ThinkingIR:
    bubble_id: str
    text: str
    duration_ms: int | None = None
    started_at_ms: int | None = None
    completed_at_ms: int | None = None


@dataclass
class ToolCallIR:
    call_index: int
    turn_index: int
    bubble_id: str
    tool_call_id: str | None
    model_call_id: str | None
    raw_tool_name: str
    canonical_kind: str
    semantic_class: str
    mcp_namespace: str | None
    mcp_tool_name: str | None
    target_path: str | None
    input_params: dict[str, Any]
    input_summary: str
    output_summary: str
    output_char_len: int
    status: str  # "completed" | "error" | "denied" | "cancelled" | "loading"
    is_error: bool
    is_denied: bool
    denial_reason: str | None
    error_message: str | None
    started_at_ms: int | None = None
    completed_at_ms: int | None = None
    elapsed_ms: int | None = None
    subagent_session_id: str | None = None
    offloaded_before_id: str | None = None
    offloaded_after_id: str | None = None


@dataclass
class CustomizationRefIR:
    kind: str  # "skill" | "rule" | "agent_doc" | "hook"
    path: str
    source: str  # "context_attached" | "explicit_read" | "explicit_edit" | "workspace_active"
    turn_index: int | None = None


@dataclass
class TurnIR:
    turn_index: int
    user_bubble_id: str
    request_id: str | None
    user_text: str
    model_name: str | None
    started_at_ms: int | None
    completed_at_ms: int | None
    duration_ms: int | None
    timestamps_synthesized: bool
    input_tokens: int | None
    output_tokens: int | None
    attached_rules: list[str] = field(default_factory=list)
    attached_files: list[str] = field(default_factory=list)
    thinking_blocks: list[ThinkingIR] = field(default_factory=list)
    tool_calls: list[ToolCallIR] = field(default_factory=list)
    assistant_texts: list[str] = field(default_factory=list)


@dataclass
class SubagentSummaryIR:
    session_id: str
    parent_turn_index: int | None
    subagent_type: str | None
    name: str | None
    model_name: str | None
    status: str | None
    duration_ms: int | None
    turn_count: int
    tool_call_count: int
    error_count: int
    denial_count: int
    skills_read: list[str] = field(default_factory=list)
    rules_read: list[str] = field(default_factory=list)


@dataclass
class AttributionIR:
    composer_code_hashes_count: int
    human_code_hashes_count: int
    distinct_files_touched: list[str] = field(default_factory=list)
    scored_commits: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SessionIR:
    schema_version: str
    harness: str
    harness_version: str
    session_id: str
    session_id_normalized: str
    parent_session_id: str | None
    is_subagent: bool
    subagent_type: str | None
    title: str | None
    mode: str | None
    status: str | None
    model_name: str | None
    max_mode: bool | None
    reasoning_effort: str | None
    started_at_ms: int | None
    ended_at_ms: int | None
    duration_ms: int | None
    total_lines_added: int
    total_lines_removed: int
    files_added: list[str]
    files_removed: list[str]
    stop_hook_loop_count: int
    orphan_bubble_count: int
    token_breakdown: TokenBreakdownIR
    customizations: list[CustomizationRefIR] = field(default_factory=list)
    turns: list[TurnIR] = field(default_factory=list)
    subagents: list[SubagentSummaryIR] = field(default_factory=list)
    attribution: AttributionIR | None = None
    warnings: list[dict[str, str]] = field(default_factory=list)
    resolved_from: dict[str, str] | None = None


@dataclass
class DiagnosticFinding:
    id: str
    finding_key: str
    name: str
    severity: str
    turn_index: int | None
    bubble_id: str | None
    summary: str
    error_message: str | None
    thinking_snippet: str | None
    evidence: dict[str, Any] = field(default_factory=dict)
    remediation_hint: str = ""
