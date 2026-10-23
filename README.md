# AI Chat Report v2.0

AI Chat Report v2.0 extracts Cursor chat sessions from local SQLite stores (`state.vscdb` and `ai-code-tracking.db`), normalizes session data into a harness-agnostic intermediate representation (`SessionIR`), runs deterministic diagnostic heuristics (`H01` through `H08`), and emits JSON v2.0 and Markdown reports.

Runtime requirement: Python 3.10+ standard library only (`pytest` is used for tests).

---

## Architecture and Repository Layout

| Path | Responsibility |
|------|----------------|
| `chat_report/core/` | Harness-agnostic intermediate representation (`ir.py`), adapter protocol and registry (`adapter.py`), semantic tool classifier (`classifier.py`), deterministic heuristics (`diagnostics.py`), comparison engine (`compare.py`), and renderers (`render/json_v2.py`, `render/markdown.py`, `render/digest.py`). |
| `chat_report/adapters/cursor/` | Cursor `3.26.16` (`_v: 18` `composerData`, `_v: 3` `bubbleId`, plus legacy `_v: 3..17`) read-only SQLite adapter (`paths.py`, `store.py`, `resolver.py`, `tool_map.py`, `hooks.py`, `adapter.py`). |
| `chat_report/cli.py` | Unified command-line interface (`python -m chat_report`). |
| `skills/chat-report/` | Cursor agent skill (`SKILL.md`) and self-contained launcher/bundler (`scripts/run.py`). |
| `cursor/chat-report.py` | Backward-compatible CLI shim that forwards invocations to `chat_report.cli` and strips deprecated v1 flags (`--shape`, `--tools`) with a warning. |
| `tests/` | Unit and end-to-end test suites (`tests/cursor/`, `tests/core/`, `tests/_fixtures.py`). |

All SQLite connections open database files in read-only URI mode (`?mode=ro`).

---

## CLI Usage

Run from the repository root with `python -m chat_report` (or via the `python cursor/chat-report.py` shim):

```powershell
# Inspect the most recent main-thread session
python -m chat_report --latest

# Inspect a specific session by chat UUID or request UUID
python -m chat_report <uuid>

# Emit a compact Stage 1 agent digest (<= 2500 chars) and write .json + .md reports
python -m chat_report --latest --agent-digest

# List the 10 most recent sessions (or specify N)
python -m chat_report --list-recent 10

# Compare two sessions (before vs after)
python -m chat_report --diff <before_uuid> <after_uuid>

# Aggregate two or more sessions into a cohort report
python -m chat_report --aggregate <id1> <id2>

# Validate a saved v2.0 JSON report offline
python -m chat_report --validate path/to/report.json
```

### Modes (Mutually Exclusive)

| Mode | Description |
|------|-------------|
| `<uuid>` | Extract a single session by chat ID or request ID. |
| `--latest` | Extract the most recent main-thread session (`isSubagent == 0`). |
| `--list-recent [N]` | Print recent sessions (default `10`) as tab-separated rows. |
| `--diff <a> <b>` | Compare session `<a>` (before) and session `<b>` (after). |
| `--aggregate <id1> <id2> ...` | Aggregate `>= 2` sessions into a cohort rollup report. |
| `--validate <json_path>` | Validate an existing JSON report against the v2.0 contract. |

### Operational Flags

| Flag | Description |
|------|-------------|
| `--harness cursor` | Select the harness adapter (default: `cursor`). |
| `--out <dir>` | Output directory for generated reports (default: `.cursor/local/chat-reports`). |
| `--format {md,json,both}` | Report output format (default: `both`). |
| `--agent-digest` | Print a bounded Stage 1 digest (`<= 2500` chars) to stdout and force `both` output formats. Supported with `<uuid>` and `--latest`. |
| `--request-id` | Force UUID resolution through `ai_code_hashes.requestId` and `bubbleId` request ID scan. |
| `--chat-id` | Force UUID resolution against `composerData:<uuid>` chat IDs only. |
| `--workspace <dir>` | Workspace directory for `.cursor/hooks.json` discovery, path canonicalization, and active customization scanning (default: current working directory). |
| `--no-subagents` | Skip child subagent session extraction. |
| `--list-include-subagents` | Include child subagent sessions when running `--list-recent`. |
| `--include-orphans` | Include unreferenced `bubbleId:<chatId>:*` rows omitted from conversation headers. |
| `--state-db <path>` | Explicit path to Cursor `state.vscdb`. |
| `--tracking-db <path>` | Explicit path to Cursor `ai-code-tracking.db`. |
| `--denial-marker <str>` | Repeatable custom hook or harness denial substring marker. |
| `--verbose` | Enable verbose diagnostic output. |

### Environment Variables

| Variable | Purpose |
|----------|---------|
| `CURSOR_STATE_DB` | Override the default OS path to `state.vscdb` when `--state-db` is not passed. |
| `CURSOR_TRACKING_DB` | Override the default OS path to `ai-code-tracking.db` when `--tracking-db` is not passed. |
| `CURSOR_PRODUCT_JSON` | Override the path to Cursor `product.json` for version detection fallback. |

Exit codes: `0` on success, `1` on runtime, storage, or validation error, and `2` on CLI usage error.

---

## JSON v2.0 Schema and Report Kinds

Every JSON report shares the v2.0 top-level envelope:

- `schema_version`: `"2.0"`
- `report_kind`: `"single-session"` | `"diff"` | `"aggregate"`
- `generated_at_iso`: UTC ISO-8601 timestamp
- `harness`: `"cursor"`
- `harness_version`: detected Cursor version string (or `"mixed"` when compared sessions differ)
- `warnings`: list of `{"code": str, "message": str}` diagnostic warnings

| `report_kind` | Output Filename Pattern | Top-Level Payload Keys |
|---------------|-------------------------|------------------------|
| `single-session` | `<session_id>.json` / `.md` | `session`, `aggregates`, `diagnostics`, `turns` |
| `diff` | `diff-<before[:12]>-vs-<after[:12]>.json` / `.md` | `before`, `after`, `delta` |
| `aggregate` | `aggregate-<YYYYMMDDTHHMMSS_ffffffZ>-<cohort_hash8>[-<seq>].json` / `.md` | `session_count`, `sessions`, `rollup` |

Offline validation (`--validate <json_path>`) enforces all structural keys, status sets, call-index ordering, and arithmetic invariants across `single-session`, `diff`, and `aggregate` reports.

---

## Deterministic Diagnostic Heuristics (`H01` through `H08`)

| Heuristic ID | Severity | Condition Detected |
|--------------|----------|--------------------|
| `H01_UNREAD_ATTACHED_OR_MENTIONED_SKILL` | `high` | A skill is attached, mentioned by the user or thinking trace, or matched by attached-skill domain activity without a corresponding `SKILL.md` read in the parent or child subagent sessions. |
| `H02_BLIND_DYNAMIC_TOOL_CALL` | `medium` | An external MCP tool is called without prior `mcp_discovery` (`GetDynamicTools`) or fails with schema/parameter validation errors. |
| `H03_TOOL_RETRY_THRASH` | `high` | Three or more consecutive or same-turn failing/denied tool calls on the same target path, MCP tool, or canonical tool kind (Branch 4a), or three or more identical empty `search_grep`/`search_glob` queries (Branch 4b). |
| `H04_HOOK_OR_HARNESS_DENIAL` | `high` | A tool call is blocked or rejected by a workspace hook or harness policy (`is_denied == True`). |
| `H05_EDIT_LINT_THRASH` | `medium` | A file is edited three or more times across two or more turns with intervening lint checks or failing test/typecheck commands across at least two edit intervals. |
| `H06_PROMPT_OR_TOOL_TOKEN_SPIKE` | `medium` | Combined rules and skills exceed `15,000` tokens, context usage reaches `>= 75.0%`, or any tool call output reaches `>= 40,000` characters. |
| `H07_SUBAGENT_DISPATCH_ANOMALY` | `medium` | A child subagent finishes with non-completed status or tool errors, or a turn dispatches three or more subagents sequentially across separate execution waves instead of parallel batching. |
| `H08_INSTRUCTION_FRICTION_SIGNAL` | `medium` / `info` | Thinking blocks contain conflict phrases indicating competing rules, skills, or harness restrictions (`medium` when the turn also has a tool error or denial, otherwise `info`). |

---

## Cursor Skill (`skills/chat-report/`)

The `skills/chat-report/SKILL.md` skill implements a two-stage token-bounded workflow:

1. **Stage 1 (Deterministic CLI + Digest):** Run `python "<skill_dir>/scripts/run.py" --latest --agent-digest` in a single shell call. If Stage 1 reports zero `HIGH`/`MEDIUM` findings and zero `H08` friction signals, and the user did not ask why a customization or tool failed or request a deep root-cause audit, return the summary and report link immediately with zero subagent dispatches.
2. **Stage 2 (Conditional Subagent Dispatch):** When `H01` through `H08` findings fire or the user asks why a skill, rule, hook, or tool failed or was ignored, dispatch the `Customization & Harness Conflict Auditor` subagent with the generated `<session_id>.json` report and touched customization files to produce a structured Root Cause, Conflicting Directives, and Proposed Diff report. An optional `Onboarding Walkthrough Coach` subagent is available when the user requests an educational walkthrough.

### Installing a Self-Contained Skill Bundle

Package `SKILL.md`, `scripts/run.py`, and the complete `chat_report` runtime into a standalone skill directory:

```powershell
python skills/chat-report/scripts/run.py --bundle-into <dest_skill_dir> [--force]
```

The bundler stages files into a sibling temporary directory and swaps the destination atomically with automatic rollback on failure.

---

## Testing

Run the full test suite from the repository root:

```powershell
python -m pytest tests/ -q
```
