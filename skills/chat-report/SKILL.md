---
name: chat-report
description: Inspect Cursor chat sessions ("chat report", "under the hood", "why did the agent ignore my skill/rule") and diagnose customization or harness failures.
---

# Chat Report

Use this skill to produce a deterministic Cursor session report before any deep diagnostic work.

## Step 1: Run Stage 1

Use one Shell call. From an installed skill:

```powershell
python "<skill_dir>/scripts/run.py" --latest --agent-digest
```

Other supported calls are:

```powershell
python "<skill_dir>/scripts/run.py" --list-recent 5
python "<skill_dir>/scripts/run.py" <uuid> --agent-digest
python "<skill_dir>/scripts/run.py" --diff <before_uuid> <after_uuid>
python "<skill_dir>/scripts/run.py" --aggregate <uuid_1> <uuid_2>
```

From the repository root, use `python -m chat_report --latest --agent-digest`.

## Step 2: Evaluate Stage 1

If Stage 1 reports `0` HIGH and `0` MEDIUM findings, has `0` H08 friction signals, and the user did not ask why a skill, rule, hook, or native/MCP tool failed or was ignored, and did not explicitly request root-cause analysis or a deep customization audit, present the summary and link the generated Markdown report. Dispatch `0` subagents and stop.

## Step 3: Conditional Stage 2

Dispatch one `generalPurpose` subagent named `Customization & Harness Conflict Auditor` when any H01 through H08 HIGH or MEDIUM finding fires, an H08 friction signal fires, the user asks why a skill, rule, hook, or tool failed or was ignored, or the user requests root-cause analysis or a deep customization audit even when Stage 1 reports zero findings.

Give it the generated `<session_id>.json`, the list of triggered H01-H08 findings from the digest, the exact touched customization files listed in `Audit Inputs:` or `session.customizations`, and the tool/thinking trace. It must read the applicable `SKILL.md`, `.mdc`, `.cursorrules`, `AGENTS.md`, `CLAUDE.md`, and `.cursor/hooks.json` files and return:

1. Root Cause
2. Conflicting Directives
3. Proposed Diff

Only when the user explicitly requests onboarding or an under-the-hood teaching guide, also dispatch the optional `generalPurpose` `Onboarding Walkthrough Coach`. It should explain prompt token budget, rule and skill loading, the tool execution waterfall, and actionable customization and prompting takeaways.
