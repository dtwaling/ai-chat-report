from __future__ import annotations

from pathlib import Path

from chat_report.core.classifier import canonicalize_path, classify_session, is_rule_path, is_skill_path
from chat_report.core.ir import CustomizationRefIR, SessionIR, SubagentSummaryIR, TokenBreakdownIR, ToolCallIR, TurnIR


def _session(call: ToolCallIR) -> SessionIR:
    return SessionIR("2.0", "cursor", "x", "id", "id", None, False, None, None, None, None, None, None, None, None, None, None, 0, 0, [], [], 0, 0, TokenBreakdownIR(None, None, None), turns=[TurnIR(1, "u", None, "", None, None, None, None, False, None, None, tool_calls=[call])])


def _call(kind: str, path: str) -> ToolCallIR:
    return ToolCallIR(1, 1, "b", None, None, "Read", kind, "", None, None, path, {}, "", "", 0, "completed", False, False, None, None)


def test_canonicalize_windows_uri_unc_and_posix() -> None:
    assert canonicalize_path("file:///C:/Work\\Thing.py", Path("C:/Work")) == "thing.py"
    assert canonicalize_path(".cursor/skills/demo/SKILL.md", Path("C:/Work")) == ".cursor/skills/demo/skill.md"
    assert canonicalize_path("C:/Work/.cursor/skills/demo/SKILL.md", Path("C:/Work")) == ".cursor/skills/demo/skill.md"
    assert canonicalize_path("file://server/share\\Path.txt") == "//server/share/path.txt"
    assert canonicalize_path("./src//Thing.py") == "src/Thing.py"
    assert canonicalize_path("file://localhost/C:/Work/Thing.py") == "c:/work/thing.py"


def test_skill_path_requires_skill_markdown_filename() -> None:
    assert is_skill_path(".cursor/skills/demo/SKILL.md")
    assert not is_skill_path(".cursor/skills/demo/helper.py")
    assert is_rule_path(".cursor/rules/nested/team-rule.md")


def test_source_hooks_file_is_not_customization() -> None:
    session = _session(_call("file_read", "src/hooks/useAuth.ts"))
    classify_session(session)
    assert session.turns[0].tool_calls[0].semantic_class == "native-read"
    assert session.customizations == []


def test_classify_session_is_idempotent_and_tracks_reads(tmp_path: Path) -> None:
    workspace = tmp_path
    skill = workspace / ".cursor" / "skills" / "demo" / "SKILL.md"
    rule = workspace / ".cursor" / "rules" / "demo.mdc"
    nested_rule = workspace / ".cursor" / "rules" / "nested" / "sub.mdc"
    cursorrules = workspace / ".cursorrules"
    agents = workspace / "AGENTS.md"
    claude = workspace / "CLAUDE.md"
    hooks = workspace / ".cursor" / "hooks.json"
    skill.parent.mkdir(parents=True)
    nested_rule.parent.mkdir(parents=True)
    skill.write_text("x")
    rule.write_text("x")
    nested_rule.write_text("x")
    cursorrules.write_text("x")
    agents.write_text("x")
    claude.write_text("x")
    hooks.write_text("{}")
    session = _session(_call("file_read", str(skill)))
    session.customizations = [CustomizationRefIR("rule", str(rule), "context_attached", 1)]
    session.turns[0].tool_calls.extend([
        _call("file_read", str(agents)),
        _call("file_edit", str(claude)),
        _call("file_write", str(hooks)),
        _call("file_read", str(cursorrules)),
    ])
    session.subagents = [SubagentSummaryIR("child", 1, None, None, None, "completed", None, 0, 0, 0, 0, [str(skill), "helper.py"], [str(rule), "helper.py"])]
    classify_session(session, workspace)
    first = [(x.kind, x.path, x.source) for x in session.customizations]
    classify_session(session, workspace)
    assert session.turns[0].tool_calls[0].semantic_class == "skill-read"
    assert [(x.kind, x.path, x.source) for x in session.customizations] == first
    assert ("skill", canonicalize_path(str(skill), workspace), "explicit_read") in first
    assert ("rule", canonicalize_path(str(nested_rule), workspace), "workspace_active") in first
    assert {source for _, _, source in first} == {"context_attached", "explicit_read", "explicit_edit", "workspace_active"}
    assert {kind for kind, _, _ in first} == {"skill", "rule", "agent_doc", "hook"}
    assert session.subagents[0].skills_read == [canonicalize_path(str(skill), workspace)]
    assert session.subagents[0].rules_read == [canonicalize_path(str(rule), workspace)]
