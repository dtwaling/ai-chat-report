from __future__ import annotations

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


def test_h02_discovery_filters_and_h03_empty_search() -> None:
    discovered = _call(1, "mcp_discovery", namespace="x", tool="one")
    called = _call(2, "mcp_call", namespace="x", tool="two")
    cancelled = _call(1, "mcp_discovery", namespace="x", status="cancelled")
    assert any(f.id == HEURISTIC_IDS[1] for f in evaluate_diagnostics(_session([discovered, called])))
    assert any(f.id == HEURISTIC_IDS[1] for f in evaluate_diagnostics(_session([cancelled, called])))
    empty = [_call(i, "search_grep", params={"pattern": "x"}, output="No matches found", output_len=16) for i in range(1, 4)]
    assert any(f.evidence.get("branch") == "4b" for f in evaluate_diagnostics(_session(empty)))


def test_failure_thrash_requires_three_calls() -> None:
    two = _session([_call(i, "shell_exec", error=True) for i in range(1, 3)])
    three = _session([_call(i, "shell_exec", error=True) for i in range(1, 4)])
    assert "H03_TOOL_RETRY_THRASH" not in {x.id for x in evaluate_diagnostics(two)}
    assert "H03_TOOL_RETRY_THRASH" in {x.id for x in evaluate_diagnostics(three)}


def test_h03_uses_path_then_mcp_then_kind_precedence() -> None:
    path_calls = [_call(i, "mcp_call", error=True, namespace="x", tool="same", path="one.py") for i in range(1, 4)]
    mcp_calls = [_call(i, "mcp_call", error=True, namespace="x", tool="same") for i in range(4, 7)]
    kind_calls = [_call(i, "shell_exec", error=True) for i in range(7, 10)]
    keys = {finding.evidence["group_key"] for finding in evaluate_diagnostics(_session(path_calls + mcp_calls + kind_calls)) if finding.id == HEURISTIC_IDS[2] and finding.evidence.get("branch") == "4a"}
    assert keys == {"path:one.py", "mcp:x:same", "kind:shell_exec"}


def test_h04_h05_h06_and_h08_contracts() -> None:
    denied = _call(1, "shell_exec", denied=True)
    recovered = _call(2, "shell_exec")
    finding = next(f for f in evaluate_diagnostics(_session([denied, recovered], loops=1)) if f.id == HEURISTIC_IDS[3])
    assert finding.evidence == {"recovered": True, "looped": True}
    edits = [_call(1, "file_edit", path="a.py"), _call(2, "workflow_control"), _call(3, "file_edit", turn=2, path="a.py"), _call(4, "shell_exec", turn=2, error=True, params={"command": "pytest a.py"}), _call(5, "file_edit", turn=3, path="a.py")]
    edits[1].raw_tool_name = "ReadLints"
    assert any(f.id == HEURISTIC_IDS[4] for f in evaluate_diagnostics(_session(edits)))
    wrong_file = [_call(1, "file_edit", path="test.py"), _call(2, "shell_exec", error=True, params={"command": "pytest other.py"}), _call(3, "file_edit", turn=2, path="test.py"), _call(4, "shell_exec", turn=2, error=True, params={"command": "pytest other.py"}), _call(5, "file_edit", turn=3, path="test.py")]
    assert not any(f.id == HEURISTIC_IDS[4] for f in evaluate_diagnostics(_session(wrong_file)))
    for token in (TokenBreakdownIR(None, None, None, {"rules": {"tokens": 15001}}), TokenBreakdownIR(None, None, 75.0), TokenBreakdownIR(None, None, None)):
        calls = [_call(1, "file_read", output_len=40000)] if token.context_usage_percent is None and not token.by_category else []
        assert any(f.id == HEURISTIC_IDS[5] for f in evaluate_diagnostics(_session(calls, token=token)))
    friction = next(f for f in evaluate_diagnostics(_session([_call(1, "shell_exec", error=True)], thinking="blocked by " + "x" * 200)) if f.id == HEURISTIC_IDS[7])
    blind = next(f for f in evaluate_diagnostics(_session([_call(1, "mcp_call", error=True, namespace="x")])) if f.id == HEURISTIC_IDS[1])
    assert friction.severity == "medium" and len(friction.thinking_snippet or "") == 160 and len(blind.error_message or "") == 160


def test_h07_child_and_sequential_waves() -> None:
    child = SubagentSummaryIR("child", 1, None, None, None, "failed", None, 0, 0, 1, 0)
    dispatch = _call(1, "subagent_dispatch", child="child", started=0, ended=5)
    wave = [_call(i, "subagent_dispatch", started=(i - 1) * 10, ended=(i - 1) * 10 + 5) for i in range(2, 5)]
    findings = evaluate_diagnostics(_session([dispatch, *wave], subagents=[child]))
    assert {f.finding_key for f in findings if f.id == HEURISTIC_IDS[6]} >= {"H07_SUBAGENT_DISPATCH_ANOMALY:sub:child", "H07_SUBAGENT_DISPATCH_ANOMALY:turn:1"}
    parallel = [_call(i, "subagent_dispatch", started=0, ended=10) for i in range(1, 4)]
    for call in parallel:
        call.model_call_id = "batch"
    assert not any(f.finding_key.endswith(":turn:1") for f in evaluate_diagnostics(_session(parallel)))
    overlapping = [_call(i, "subagent_dispatch", started=i, ended=20) for i in range(1, 4)]
    assert not any(f.finding_key.endswith(":turn:1") for f in evaluate_diagnostics(_session(overlapping)))


def test_finding_keys_are_stable_for_all_heuristics() -> None:
    calls = [_call(1, "mcp_call", error=True, namespace="bad"), _call(2, "shell_exec", denied=True), _call(3, "shell_exec", error=True), _call(4, "shell_exec", error=True), _call(5, "shell_exec", error=True), _call(6, "file_edit", path="a.py"), _call(7, "workflow_control"), _call(8, "file_edit", turn=2, path="a.py"), _call(9, "workflow_control", turn=2), _call(10, "file_edit", turn=3, path="a.py"), _call(11, "file_read", output_len=40000)]
    calls[6].raw_tool_name = calls[8].raw_tool_name = "ReadLints"
    session = _session(calls, texts={1: "use canvas skill"}, thinking="blocked by rules", customizations=[CustomizationRefIR("skill", "/skills/canvas/SKILL.md", "context_attached")], subagents=[SubagentSummaryIR("bad-child", 1, None, None, None, "failed", None, 0, 0, 1, 0)])
    first, second = evaluate_diagnostics(session), evaluate_diagnostics(session)
    assert {finding.id for finding in first} == set(HEURISTIC_IDS)
    assert [finding.finding_key for finding in first] == [finding.finding_key for finding in second]
