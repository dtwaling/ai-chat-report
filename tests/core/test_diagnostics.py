from __future__ import annotations

from pathlib import Path

from chat_report.core.classifier import classify_session
from chat_report.core.compare import build_diff_report
from chat_report.core.diagnostics import evaluate_diagnostics
from chat_report.core.ir import CustomizationRefIR, HEURISTIC_IDS, SessionIR, SubagentSummaryIR, ThinkingIR, TokenBreakdownIR, ToolCallIR, TurnIR


def _call(index: int, kind: str, *, turn: int = 1, error: bool = False, denied: bool = False, namespace: str | None = None, tool: str | None = None, path: str | None = None, params: dict | None = None, output: str = "", output_len: int = 0, status: str | None = None, started: int | None = None, ended: int | None = None, child: str | None = None) -> ToolCallIR:
    return ToolCallIR(index, turn, f"b{turn}", None, None, kind, kind, "", namespace, tool, path, params or {}, "", output, output_len, status or ("denied" if denied else "error" if error else "completed"), error or denied, denied, "x" * 200 if error else None, "invalid parameter " + "x" * 200 if error else None, started, ended, None, child)


def _session(calls: list[ToolCallIR], *, texts: dict[int, str] | None = None, thinking: str = "", token: TokenBreakdownIR | None = None, customizations: list[CustomizationRefIR] | None = None, subagents: list[SubagentSummaryIR] | None = None, loops: int = 0) -> SessionIR:
    turns = [TurnIR(index, f"u{index}", None, (texts or {}).get(index, ""), None, None, None, None, False, None, None, thinking_blocks=[ThinkingIR(f"b{index}", thinking)] if thinking and index == 1 else [], tool_calls=[call for call in calls if call.turn_index == index]) for index in sorted({call.turn_index for call in calls} or {1})]
    return SessionIR("2.0", "cursor", "x", "id", "id", None, False, None, None, None, None, None, None, None, None, None, None, 0, 0, [], [], loops, 0, token or TokenBreakdownIR(None, None, None), customizations=customizations or [], turns=turns, subagents=subagents or [])


def test_blind_mcp_call_and_friction_are_stable() -> None:
    session = _session([_call(1, "mcp_call", error=True, namespace="demo")], thinking="Blocked by conflicting instruction.")
    first = evaluate_diagnostics(session)
    second = evaluate_diagnostics(session)
    assert {x.id for x in first} == {"H02_BLIND_DYNAMIC_TOOL_CALL", "H08_INSTRUCTION_FRICTION_SIGNAL"}
    assert [x.finding_key for x in first] == [x.finding_key for x in second]


def test_h01_attached_mentioned_domain_and_catalog_controls() -> None:
    ref = CustomizationRefIR("skill", "/skills/canvas/SKILL.md", "context_attached")
    mentioned = _session([], texts={1: "Please use canvas skill."}, customizations=[ref])
    catalog = _session([], texts={1: "<available_skills><agent_skill fullPath='/skills/canvas/SKILL.md'/></available_skills>"}, customizations=[ref])
    domain = _session([_call(1, "file_write", path="x.canvas.tsx")], customizations=[ref])
    assert any(f.id == HEURISTIC_IDS[0] for f in evaluate_diagnostics(mentioned))
    assert not evaluate_diagnostics(catalog)
    assert any(f.id == HEURISTIC_IDS[0] for f in evaluate_diagnostics(domain))
    workspace_only = _session([_call(1, "file_write", path="x.canvas.tsx")], customizations=[CustomizationRefIR("skill", "/skills/canvas/SKILL.md", "workspace_active")])
    satisfied = _session([], texts={1: "use canvas skill"}, customizations=[ref])
    satisfied.subagents = [SubagentSummaryIR("child", 1, None, None, None, "completed", None, 0, 0, 0, 0, ["/skills/canvas/SKILL.md"], [])]
    assert not any(f.id == HEURISTIC_IDS[0] for f in evaluate_diagnostics(workspace_only))
    assert not any(f.id == HEURISTIC_IDS[0] for f in evaluate_diagnostics(satisfied))


def test_h01_ignores_available_catalog_attributes_and_bare_english_word() -> None:
    loop = CustomizationRefIR("skill", "/skills/loop/SKILL.md", "workspace_active")
    session = _session([], texts={1: '<available_skills description="catalog"><agent_skill fullPath="/skills/loop/SKILL.md"/></available_skills> Please fix the retry loop.'}, customizations=[loop])
    assert not any(f.id == HEURISTIC_IDS[0] for f in evaluate_diagnostics(session))


def test_h01_credits_inlined_manually_attached_and_slash_attached_skills() -> None:
    inlined_text = (
        "<manually_attached_skills>\n"
        "Skill Name: chat-report\n"
        "Path: d:/repo/.cursor/skills/chat-report/SKILL.md\n"
        "SKILL.md content:\n# Chat Report\n"
        "</manually_attached_skills>\n"
        "Use /chat-report"
    )
    inlined_session = _session(
        [_call(1, "shell_exec", params={"command": "python -m chat_report --latest"})],
        texts={1: inlined_text},
        customizations=[CustomizationRefIR("skill", ".cursor/skills/chat-report/skill.md", "workspace_active")],
    )
    assert not any(f.id == HEURISTIC_IDS[0] for f in evaluate_diagnostics(inlined_session, Path("d:/repo")))

    slash_attached_session = _session(
        [_call(1, "shell_exec", params={"command": "python -m chat_report --latest"})],
        texts={1: "Let's take this for a test run. Use /chat-report"},
        customizations=[
            CustomizationRefIR("skill", ".cursor/skills/chat-report/skill.md", "context_attached", 1),
            CustomizationRefIR("skill", ".cursor/skills/chat-report/skill.md", "workspace_active"),
        ],
    )
    assert not any(f.id == HEURISTIC_IDS[0] for f in evaluate_diagnostics(slash_attached_session))

    unattached_slash_session = _session(
        [_call(1, "shell_exec", params={"command": "echo hi"})],
        texts={1: "Use /chat-report"},
        customizations=[CustomizationRefIR("skill", ".cursor/skills/chat-report/skill.md", "workspace_active")],
    )
    assert any(f.id == HEURISTIC_IDS[0] for f in evaluate_diagnostics(unattached_slash_session))

    legacy_tag_session = _session(
        [_call(1, "file_write", path="x.canvas.tsx")],
        texts={
            1: (
                '<manually_attached_skills>\n'
                '<agent_skill fullPath="/skills/canvas/SKILL.md"/>\n'
                '</manually_attached_skills>\n'
                'Build a canvas.'
            )
        },
        customizations=[
            CustomizationRefIR("skill", "/skills/canvas/SKILL.md", "context_attached", 1),
            CustomizationRefIR("skill", "/skills/can/SKILL.md", "workspace_active"),
        ],
    )
    legacy_findings = [f for f in evaluate_diagnostics(legacy_tag_session) if f.id == HEURISTIC_IDS[0]]
    assert [f.evidence["path"] for f in legacy_findings] == ["/skills/canvas/SKILL.md"]


def test_h01_read_remains_satisfied_without_workspace(tmp_path: Path) -> None:
    workspace = tmp_path
    skill = workspace / ".cursor" / "skills" / "canvas" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("x")
    session = _session([_call(1, "file_read", path=str(skill))], texts={1: "use canvas skill"}, customizations=[CustomizationRefIR("skill", str(skill), "context_attached")])
    classify_session(session, workspace)
    assert not any(f.id == HEURISTIC_IDS[0] for f in evaluate_diagnostics(session))
    assert not any(f["id"] == HEURISTIC_IDS[0] for f in build_diff_report(session, session)["before"]["diagnostics"]["findings"])
    mixed_case = _session([_call(1, "file_read", path="/Skills/Canvas/SKILL.md")], texts={1: "use canvas skill"}, customizations=[CustomizationRefIR("skill", "/skills/canvas/SKILL.md", "context_attached"), CustomizationRefIR("skill", "SKILL.md", "context_attached")])
    assert not any(f.id == HEURISTIC_IDS[0] for f in evaluate_diagnostics(mixed_case))


def test_h02_discovery_filters_and_h03_empty_search() -> None:
    discovered = _call(1, "mcp_discovery", namespace="x", tool="one")
    called = _call(2, "mcp_call", namespace="x", tool="two")
    cancelled = _call(1, "mcp_discovery", namespace="x", status="cancelled")
    assert any(f.id == HEURISTIC_IDS[1] for f in evaluate_diagnostics(_session([discovered, called])))
    assert any(f.id == HEURISTIC_IDS[1] for f in evaluate_diagnostics(_session([cancelled, called])))
    empty = [_call(i, "search_grep", params={"pattern": "x"}, output="No matches found", output_len=16) for i in range(1, 4)]
    assert any(f.evidence.get("branch") == "4b" for f in evaluate_diagnostics(_session(empty)))
    assert [f.finding_key for f in evaluate_diagnostics(_session(empty))] == [f.finding_key for f in evaluate_diagnostics(_session(empty))]


def test_failure_thrash_requires_three_calls() -> None:
    two = _session([_call(i, "shell_exec", error=True) for i in range(1, 3)])
    three = _session([_call(i, "shell_exec", error=True) for i in range(1, 4)])
    assert "H03_TOOL_RETRY_THRASH" not in {x.id for x in evaluate_diagnostics(two)}
    assert "H03_TOOL_RETRY_THRASH" in {x.id for x in evaluate_diagnostics(three)}


def test_h03_uses_path_then_mcp_then_kind_precedence() -> None:
    path_calls = [_call(i, "mcp_call", error=True, namespace="x", tool="same", path="one.py") for i in range(1, 4)]
    mcp_calls = [_call(i, "mcp_call", error=True, namespace="x", tool="same") for i in range(4, 7)]
    shell_calls = [_call(i, "shell_exec", error=True, params={"command": '& "C:\\Program Files\\Git\\bin\\git.exe" push'}) for i in range(7, 10)]
    distinct_shell = [
        _call(10, "shell_exec", error=True, params={"command": "pytest -q"}),
        _call(11, "shell_exec", error=True, params={"command": "mypy src"}),
        _call(12, "shell_exec", error=True, params={"command": "ruff check"}),
    ]
    kind_calls = [_call(i, "other", error=True) for i in range(13, 16)]
    keys = {finding.evidence["group_key"] for finding in evaluate_diagnostics(_session(path_calls + mcp_calls + shell_calls + distinct_shell + kind_calls)) if finding.id == HEURISTIC_IDS[2] and finding.evidence.get("branch") == "4a"}
    assert keys == {"path:one.py", "mcp:x:same", "shell:git", "kind:other"}
    cross_turn = [_call(1, "shell_exec", turn=1, error=True), _call(2, "shell_exec", turn=1, error=True), _call(3, "shell_exec", turn=2, error=True), _call(4, "shell_exec", turn=2, error=True), _call(5, "shell_exec", turn=2, error=True)]
    thrash = [f for f in evaluate_diagnostics(_session(cross_turn)) if f.id == HEURISTIC_IDS[2]]
    assert len(thrash) == 1 and thrash[0].evidence["call_indexes"] == [1, 2, 3, 4, 5]


def test_h04_h05_h06_and_h08_contracts() -> None:
    denied = _call(1, "shell_exec", denied=True)
    recovered = _call(2, "shell_exec")
    finding = next(f for f in evaluate_diagnostics(_session([denied, recovered], loops=1)) if f.id == HEURISTIC_IDS[3])
    assert finding.evidence == {"recovered": True, "looped": True}
    edits = [_call(1, "file_edit", path="a.py"), _call(2, "workflow_control"), _call(3, "file_edit", turn=2, path="a.py"), _call(4, "shell_exec", turn=2, error=True, params={"command": "pytest a.py"}), _call(5, "file_edit", turn=3, path="a.py")]
    edits[1].raw_tool_name = "ReadLints"
    h05_edit_finding = next(f for f in evaluate_diagnostics(_session(edits)) if f.id == HEURISTIC_IDS[4])
    assert h05_edit_finding.turn_index == 3 and h05_edit_finding.bubble_id == "b3"
    clean_lints = [_call(1, "file_edit", path="a.py"), _call(2, "workflow_control", output="No linter errors found."), _call(3, "file_edit", turn=2, path="a.py"), _call(4, "workflow_control", turn=2, output="No linter errors found."), _call(5, "file_edit", turn=3, path="a.py")]
    clean_lints[1].raw_tool_name = clean_lints[3].raw_tool_name = "ReadLints"
    assert not any(f.id == HEURISTIC_IDS[4] for f in evaluate_diagnostics(_session(clean_lints)))
    clean_json_lints = [
        _call(1, "file_edit", path="a.py"),
        _call(2, "workflow_control", params={"paths": ["a.py"]}, output='{"linterErrorsByFile": [{"relativeWorkspacePath": "a.py"}]}'),
        _call(3, "file_edit", turn=2, path="a.py"),
        _call(4, "workflow_control", turn=2, params={"paths": ["a.py"]}, output='{"linterErrorsByFile": [{"relativeWorkspacePath": "a.py", "linterErrors": [ ]}'[:75]),
        _call(5, "file_edit", turn=3, path="a.py"),
    ]
    clean_json_lints[1].raw_tool_name = clean_json_lints[3].raw_tool_name = "read_lints"
    assert not any(f.id == HEURISTIC_IDS[4] for f in evaluate_diagnostics(_session(clean_json_lints)))
    truncated_mixed_output = (
        '{"linterErrorsByFile": [{"relativeWorkspacePath": "a.py"}, '
        '{"relativeWorkspacePath": "other.py", "linterErrors": [{"message": "unterminated string literal'
    )
    truncated_mixed_lints = [
        _call(1, "file_edit", path="a.py"),
        _call(2, "file_edit", path="other.py"),
        _call(3, "workflow_control", params={"paths": ["a.py", "other.py"]}, output=truncated_mixed_output),
        _call(4, "file_edit", turn=2, path="a.py"),
        _call(5, "file_edit", turn=2, path="other.py"),
        _call(6, "workflow_control", turn=2, params={"paths": ["a.py", "other.py"]}, output=truncated_mixed_output),
        _call(7, "file_edit", turn=3, path="a.py"),
        _call(8, "file_edit", turn=3, path="other.py"),
    ]
    truncated_mixed_lints[2].raw_tool_name = truncated_mixed_lints[5].raw_tool_name = "read_lints"
    truncated_mixed_h05 = [f.finding_key for f in evaluate_diagnostics(_session(truncated_mixed_lints)) if f.id == HEURISTIC_IDS[4]]
    assert truncated_mixed_h05 == ["H05_EDIT_LINT_THRASH:other.py"]
    errored_json_lints = [
        _call(1, "file_edit", path="a.py"),
        _call(2, "workflow_control", params={"paths": ["a.py"]}, output='{"linterErrorsByFile": [{"relativeWorkspacePath": "a.py", "linterErrors": [{"message": "unused import"}]}]}'),
        _call(3, "file_edit", turn=2, path="a.py"),
        _call(4, "workflow_control", turn=2, params={"paths": ["a.py"]}, output='{"linterErrorsByFile": [{"relativeWorkspacePath": "a.py", "linterErrors": [{"message": "type error"}]}]}'),
        _call(5, "file_edit", turn=4, path="a.py"),
    ]
    errored_json_lints[1].raw_tool_name = errored_json_lints[3].raw_tool_name = "read_lints"
    errored_h05 = next(f for f in evaluate_diagnostics(_session(errored_json_lints)) if f.id == HEURISTIC_IDS[4])
    assert errored_h05.turn_index == 4
    mixed_json_lints = [
        _call(1, "file_edit", path="a.py"),
        _call(2, "workflow_control", params={"paths": ["a.py", "other.py"]}, output='{"linterErrorsByFile": [{"relativeWorkspacePath": "a.py"}, {"relativeWorkspacePath": "other.py", "linterErrors": [{"message": "err"}]}]}'),
        _call(3, "file_edit", turn=2, path="a.py"),
        _call(4, "workflow_control", turn=2, params={"paths": ["a.py", "other.py"]}, output='{"linterErrorsByFile": [{"relativeWorkspacePath": "a.py"}, {"relativeWorkspacePath": "other.py", "linterErrors": [{"message": "err"}]}]}'),
        _call(5, "file_edit", turn=3, path="a.py"),
    ]
    mixed_json_lints[1].raw_tool_name = mixed_json_lints[3].raw_tool_name = "read_lints"
    assert not any(f.id == HEURISTIC_IDS[4] for f in evaluate_diagnostics(_session(mixed_json_lints)))
    other_paths_lint = [_call(1, "file_edit", path="a.py"), _call(2, "workflow_control", path="other.py", params={"paths": ["other.py"]}, output="line 1: error"), _call(3, "file_edit", turn=2, path="a.py"), _call(4, "workflow_control", turn=2, path="other.py", params={"paths": ["other.py"]}, output="line 1: error"), _call(5, "file_edit", turn=3, path="a.py")]
    other_paths_lint[1].raw_tool_name = other_paths_lint[3].raw_tool_name = "ReadLints"
    assert not any(f.id == HEURISTIC_IDS[4] for f in evaluate_diagnostics(_session(other_paths_lint)))
    second_path_lint = [_call(1, "file_edit", path="src/a.py"), _call(2, "workflow_control", path="src/other.py", params={"paths": ["src/other.py", "src/a.py"]}, output="line 1: error"), _call(3, "file_edit", turn=2, path="src/a.py"), _call(4, "workflow_control", turn=2, path="src", params={"paths": ["src"]}, output="line 1: error"), _call(5, "file_edit", turn=3, path="src/a.py")]
    second_path_lint[1].raw_tool_name = second_path_lint[3].raw_tool_name = "ReadLints"
    assert any(f.id == HEURISTIC_IDS[4] for f in evaluate_diagnostics(_session(second_path_lint)))
    wrong_file = [_call(1, "file_edit", path="test.py"), _call(2, "shell_exec", error=True, params={"command": "pytest other.py"}), _call(3, "file_edit", turn=2, path="test.py"), _call(4, "shell_exec", turn=2, error=True, params={"command": "pytest other.py"}), _call(5, "file_edit", turn=3, path="test.py")]
    assert not any(f.id == HEURISTIC_IDS[4] for f in evaluate_diagnostics(_session(wrong_file)))
    for token in (TokenBreakdownIR(None, None, None, {"rules": {"tokens": 15001}}), TokenBreakdownIR(None, None, 75.0), TokenBreakdownIR(None, None, None)):
        calls = [_call(1, "file_read", turn=2, output_len=40000)] if token.context_usage_percent is None and not token.by_category else []
        h06 = next(f for f in evaluate_diagnostics(_session(calls, token=token)) if f.id == HEURISTIC_IDS[5])
        if calls:
            assert h06.turn_index == 2 and h06.bubble_id == "b2"
    friction = next(f for f in evaluate_diagnostics(_session([_call(1, "shell_exec", error=True)], thinking="blocked by " + "x" * 200)) if f.id == HEURISTIC_IDS[7])
    mid_thinking = ("a" * 220) + " blocked by rules " + ("z" * 220)
    mid_friction = next(f for f in evaluate_diagnostics(_session([], thinking=mid_thinking)) if f.id == HEURISTIC_IDS[7])
    blind = next(f for f in evaluate_diagnostics(_session([_call(1, "mcp_call", error=True, namespace="x")])) if f.id == HEURISTIC_IDS[1])
    assert friction.severity == "medium" and friction.turn_index == 1 and friction.bubble_id == "b1" and len(friction.thinking_snippet or "") == 160 and len(blind.error_message or "") == 160
    assert mid_friction.turn_index == 1 and mid_friction.bubble_id == "b1" and len(mid_friction.thinking_snippet or "") == 160 and "blocked by" in (mid_friction.thinking_snippet or "")
    quoted_only = _session([], thinking='My focus is on markers like `"Blocked by preToolUse hook"` in code.')
    assert not any(f.id == HEURISTIC_IDS[7] for f in evaluate_diagnostics(quoted_only))


def test_h07_child_and_sequential_waves() -> None:
    child = SubagentSummaryIR("child", 1, None, None, None, "failed", None, 0, 0, 1, 0)
    minor_err_child = SubagentSummaryIR("ok-child", 1, None, None, None, "completed", None, 1, 10, 1, 0)
    half_err_child = SubagentSummaryIR("half-err-child", 1, None, None, None, "completed", None, 1, 4, 2, 0)
    high_err_child = SubagentSummaryIR("high-err-child", 1, None, None, None, "completed", None, 1, 4, 3, 0)
    in_flight_child = SubagentSummaryIR("active-child", 1, "explore", None, None, None, None, 1, 2, 0, 0, in_flight=True)
    dispatch = _call(1, "subagent_dispatch", child="child", started=0, ended=5)
    wave = [_call(i, "subagent_dispatch", started=(i - 1) * 10, ended=(i - 1) * 10 + 5) for i in range(2, 5)]
    findings = evaluate_diagnostics(_session([dispatch, *wave], subagents=[child, minor_err_child, half_err_child, high_err_child, in_flight_child]))
    h07_keys = {f.finding_key for f in findings if f.id == HEURISTIC_IDS[6]}
    assert {"H07_SUBAGENT_DISPATCH_ANOMALY:sub:child", "H07_SUBAGENT_DISPATCH_ANOMALY:sub:high-err-child", "H07_SUBAGENT_DISPATCH_ANOMALY:turn:1"} <= h07_keys
    assert "H07_SUBAGENT_DISPATCH_ANOMALY:sub:ok-child" not in h07_keys
    assert "H07_SUBAGENT_DISPATCH_ANOMALY:sub:half-err-child" not in h07_keys
    assert "H07_SUBAGENT_DISPATCH_ANOMALY:sub:active-child" not in h07_keys
    parallel = [_call(i, "subagent_dispatch", started=0, ended=10) for i in range(1, 4)]
    for call in parallel:
        call.model_call_id = "batch"
    assert not any(f.finding_key.endswith(":turn:1") for f in evaluate_diagnostics(_session(parallel)))
    overlapping = [_call(i, "subagent_dispatch", started=i, ended=20) for i in range(1, 4)]
    assert not any(f.finding_key.endswith(":turn:1") for f in evaluate_diagnostics(_session(overlapping)))
    multi_step = [
        _call(1, "subagent_dispatch", started=0, ended=5),
        _call(2, "shell_exec", started=6, ended=8),
        _call(3, "subagent_dispatch", started=10, ended=15),
        _call(4, "file_read", started=16, ended=18),
        _call(5, "subagent_dispatch", started=20, ended=25),
    ]
    assert not any(f.finding_key.endswith(":turn:1") for f in evaluate_diagnostics(_session(multi_step)))
    alternating_roles = [
        _call(1, "subagent_dispatch", started=0, ended=5, params={"name": "general-purpose", "subagentType": "custom"}),
        _call(2, "subagent_dispatch", started=6, ended=10, params={"name": "cursor-task-reviewer", "subagentType": "custom"}),
        _call(3, "subagent_dispatch", started=11, ended=15, params={"name": "general-purpose", "subagentType": "custom"}),
        _call(4, "subagent_dispatch", started=16, ended=20, params={"name": "cursor-task-reviewer", "subagentType": "custom"}),
    ]
    assert not any(f.finding_key.endswith(":turn:1") for f in evaluate_diagnostics(_session(alternating_roles)))
    heterogeneous_fanout = [
        _call(1, "subagent_dispatch", started=0, ended=5, params={"subagent_type": "explore"}),
        _call(2, "subagent_dispatch", started=6, ended=10, params={"subagent_type": "docs-researcher"}),
        _call(3, "subagent_dispatch", started=11, ended=15, params={"subagent_type": "noether"}),
    ]
    assert any(f.finding_key.endswith(":turn:1") for f in evaluate_diagnostics(_session(heterogeneous_fanout)))
    assert [f.finding_key for f in findings] == [f.finding_key for f in evaluate_diagnostics(_session([dispatch, *wave], subagents=[child, minor_err_child, half_err_child, high_err_child]))]


def test_finding_keys_are_stable_for_all_heuristics() -> None:
    calls = [_call(1, "mcp_call", error=True, namespace="bad"), _call(2, "shell_exec", denied=True), _call(3, "shell_exec", error=True), _call(4, "shell_exec", error=True), _call(5, "shell_exec", error=True), _call(6, "file_edit", path="a.py"), _call(7, "workflow_control"), _call(8, "file_edit", turn=2, path="a.py"), _call(9, "workflow_control", turn=2), _call(10, "file_edit", turn=3, path="a.py"), _call(11, "file_read", output_len=40000)]
    calls[6].raw_tool_name = calls[8].raw_tool_name = "ReadLints"
    session = _session(calls, texts={1: "use canvas skill"}, thinking="blocked by rules", customizations=[CustomizationRefIR("skill", "/skills/canvas/SKILL.md", "context_attached")], subagents=[SubagentSummaryIR("bad-child", 1, None, None, None, "failed", None, 0, 0, 1, 0)])
    first, second = evaluate_diagnostics(session), evaluate_diagnostics(session)
    assert {finding.id for finding in first} == set(HEURISTIC_IDS)
    assert [finding.finding_key for finding in first] == [finding.finding_key for finding in second]
