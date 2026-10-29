---
name: chat-report-dashboard
description: Render a chat-report session analysis (noether-metrics/1 metrics.json) as a Cursor canvas dashboard. Use when asked for a dashboard, canvas, or visual report of chat-report or noether-analyst results, or when writing a metrics.json for one.
---

# Chat Report Dashboard

Renders `<session_id>.metrics.json` (written by `noether-analyst`) as a canvas. The `Metrics` type in [template.canvas.tsx](template.canvas.tsx) is the schema contract for `metrics.json`. The template `DATA` is a synthetic example; never reuse its values.

## Workflow

1. Read the `canvas` skill (`~/.cursor/skills-cursor/canvas/SKILL.md`). Its rules apply.
2. Read `metrics.json`. Required = every non-optional property of `Metrics` plus `diff` when `report_kind == "diff"` and `aggregate` when `report_kind == "aggregate"`. Reject it if `schema` is not `noether-metrics/1` or a required property is missing; report what is missing. Never invent data.
3. Copy `template.canvas.tsx` to `~/.cursor/projects/<workspace>/canvases/chat-report-<report_kind>-<first 8 of session id>.canvas.tsx`.
4. Replace only the `DATA` constant (and its `SYNTHETIC EXAMPLE` comment) with the `metrics.json` contents. Do not edit components unless the data needs a panel the template lacks.
5. Confirm the write returns `Canvas TypeScript check: no errors`. Fix and rewrite until it does.
6. Return the canvas absolute path.

## Data rules

- `subject.generated_at`: chat-report `generated_at_iso`, ISO 8601 UTC.
- `quality: "excluded"` metrics keep `value: null`. The template hides them from headline stats and shows `--` in the ledger.
- `headline: true` on at most 5 trusted or adjusted metrics.
- `findings_high_medium` counts chat-report findings only. Its headline tone keys on that id.
- `findings[].source`: `chat-report` for H01-H08, `noether` for analyst findings.
- `efficacy` is judged; keep the `(judged)` labels.
- `data_quality` lists every adjusted or excluded field with the reason.
- Empty arrays hide their panel. Do not add placeholder rows.
- `diff` only for `report_kind: "diff"`; `aggregate` only for `"aggregate"`. Per-session trend line renders only with 3+ sessions.
- The SDK charts have no axis-label props. Units go in `CardHeader` titles and `valueSuffix`; source goes in the caption.
- ASCII only in strings.

## Panels

| Panel | Data | Component |
| --- | --- | --- |
| Verdict | `verdict` | `Callout` |
| Headline strip | `metrics[headline]` | `Stat` |
| Context composition | `context` | `UsageBar` + `Table` (categories, top nodes) |
| Tool outcomes, tool time | `tool_classes` | stacked / horizontal `BarChart` |
| Turns | `turns` | `Table`, tone by efficacy |
| Findings | `findings` | `Table`, tone by severity |
| Customization ledger | `customizations` | `Table`, danger when referenced but unread |
| Subagents | `subagents` | `Table` |
| Diff | `diff` | grouped `BarChart` + findings delta `Table` |
| Aggregate | `aggregate` | `LineChart` (3+ sessions), `BarChart` |
| Metric ledger | `metrics` | `Table` with formula, source, evidence, quality |
| Data quality | `data_quality` | `Callout` |
