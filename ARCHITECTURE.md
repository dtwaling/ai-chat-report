# ARCHITECTURE.md

System architecture, data contracts, and context map for `dtw-ai-chat-report` (`chat_report` v2.0, schema `2.0`).

## 1. Context Map (Agent Navigation Guide)

Use these tables to locate symbols, responsibilities, and test files without broad repository scans.

### Repository File Map

| Path                                      | Key Symbols                                                                                                                                                                                                       | Responsibility                                                                                                 |
| :---------------------------------------- | :---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------- |
| `chat_report/__init__.py`                 | `__version__`, `SCHEMA_VERSION`                                                                                                                                                                                   | Package version (`2.0.1`) and report contract version (`2.0`).                                                 |
| `chat_report/cli.py`                      | `main()`, `_parser()`, `strip_deprecated_v1_argv()`                                                                                                                                                               | CLI argument normalization, mode dispatch, atomic file output, and bundler delegation.                         |
| `chat_report/core/ir.py`                  | `SessionIR`, `TurnIR`, `ToolCallIR`, `ThinkingIR`, `TokenBreakdownIR`, `CustomizationRefIR`, `SubagentSummaryIR`, `AttributionIR`, `DiagnosticFinding`, `CanonicalToolKind`, `SemanticToolClass`, `HEURISTIC_IDS` | Harness-agnostic intermediate representation dataclasses, enums, and schema constants.                         |
| `chat_report/core/adapter.py`             | `HarnessAdapter`, `ExtractOptions`, `SessionListEntry`, `ADAPTER_REGISTRY`, `register_adapter()`, `get_adapter()`                                                                                                 | Adapter protocol, extraction options, lookup/store exceptions, and adapter registry.                           |
| `chat_report/core/classifier.py`          | `classify_session()`, `classify_tool_call()`, `canonicalize_path()`                                                                                                                                               | Post-extraction semantic tool classification, cross-OS path canonicalization, and customization inventory.     |
| `chat_report/core/diagnostics.py`         | `evaluate_diagnostics()`, `_h01()`                                                                                                                                                                                | Deterministic Stage 1 diagnostic heuristics (`H01` through `H08`).                                             |
| `chat_report/core/compare.py`             | `build_single_session_report()`, `build_diff_report()`, `build_aggregate_report()`, `build_session_aggregates()`                                                                                                  | Arithmetic rollups, single-session payload assembly, before/after diffs, and multi-session cohort rollups.     |
| `chat_report/core/render/json_v2.py`      | `assert_v2_report_conforms()`, `render_single_session_json()`, `render_diff_json()`, `render_aggregate_json()`                                                                                                    | Strict v2.0 JSON contract validator and deterministic JSON serializer.                                         |
| `chat_report/core/render/markdown.py`     | `render_single_session_md()`, `render_diff_md()`, `render_aggregate_md()`                                                                                                                                         | Human-readable Markdown renderers for single, diff, and aggregate reports.                                     |
| `chat_report/core/render/digest.py`       | `render_agent_digest()`, `DIGEST_MAX_CHARS`                                                                                                                                                                       | Budgeted Stage 1 stdout digest (`<= 2500` chars) with Stage 2 subagent audit recommendation.                   |
| `chat_report/adapters/cursor/paths.py`    | `resolve_state_db()`, `resolve_tracking_db_with_provenance()`, `detect_cursor_version()`                                                                                                                          | Cross-OS Cursor SQLite path resolution (Windows, macOS, Linux) and 4-step Cursor version detection.            |
| `chat_report/adapters/cursor/store.py`    | `open_ro()`, `fetch_composer_data()`, `fetch_ordered_headers()`, `fetch_bubbles()`, `order_bubbles()`, `extract_token_breakdown()`, `fetch_attribution()`                                                         | Read-only SQLite URI connection helper (`?mode=ro`) and raw Cursor KV/tracking queries.                        |
| `chat_report/adapters/cursor/resolver.py` | `resolve_chat_id()`, `list_recent_sessions()`, `resolve_latest_session_id()`                                                                                                                                      | 4-way session/request UUID resolution and schema-adaptive `composerHeaders` listing.                           |
| `chat_report/adapters/cursor/tool_map.py` | `map_tool_call()`, `parse_mcp_raw_name()`, `unwrap_mcp_payload()`                                                                                                                                                 | Cursor `toolFormerData` normalization into `ToolCallIR`, MCP payload unwrapping, and denial status mapping.    |
| `chat_report/adapters/cursor/hooks.py`    | `load_denial_markers()`, `detect_denial()`, `extract_markers_from_hook_script()`                                                                                                                                  | JSONC parser for `.cursor/hooks.json` and hook script denial string scanner.                                   |
| `chat_report/adapters/cursor/adapter.py`  | `CursorAdapter`                                                                                                                                                                                                   | Orchestrates Cursor session extraction, turn assembly, timestamp synthesis, attribution, and child subagents.  |
| `skills/chat-report/SKILL.md`             | -                                                                                                                                                                                                                 | Two-stage Cursor skill specification (Stage 1 deterministic CLI digest, Stage 2 conditional auditor subagent). |
| `skills/chat-report/scripts/run.py`       | `bundle_into()`, `main()`                                                                                                                                                                                         | Standalone skill runner and atomic directory bundler (`--bundle-into`).                                        |
| `cursor/chat-report.py`                   | `main()`                                                                                                                                                                                                          | Backward-compatible v1 CLI forwarding shim.                                                                    |

### Task-to-File Lookup

| Task                                                         | Primary Files to Inspect / Edit                                                                       | Verification Tests                                                                                                                      |
| :----------------------------------------------------------- | :---------------------------------------------------------------------------------------------------- | :-------------------------------------------------------------------------------------------------------------------------------------- |
| Add or modify a diagnostic heuristic (`H01`..`H08`)          | `chat_report/core/diagnostics.py`, `chat_report/core/ir.py`                                           | `tests/core/test_diagnostics.py`                                                                                                        |
| Map a new Cursor tool or MCP call format                     | `chat_report/adapters/cursor/tool_map.py`, `chat_report/core/ir.py`, `chat_report/core/classifier.py` | `tests/cursor/test_tool_map.py`, `tests/core/test_classifier.py`                                                                        |
| Update hook denial detection or `.cursor/hooks.json` parsing | `chat_report/adapters/cursor/hooks.py`, `chat_report/adapters/cursor/tool_map.py`                     | `tests/cursor/test_hooks.py`                                                                                                            |
| Change Cursor SQLite extraction or bubble ordering           | `chat_report/adapters/cursor/store.py`, `chat_report/adapters/cursor/adapter.py`                      | `tests/cursor/test_store_v18.py`                                                                                                        |
| Modify UUID resolution or `--list-recent` query logic        | `chat_report/adapters/cursor/resolver.py`, `chat_report/adapters/cursor/paths.py`                     | `tests/cursor/test_resolver.py`, `tests/cursor/test_paths.py`, `tests/cursor/test_cross_os_paths.py`                                    |
| Change JSON v2.0 report fields or validation invariants      | `chat_report/core/ir.py`, `chat_report/core/compare.py`, `chat_report/core/render/json_v2.py`         | `tests/core/test_compare.py`, `tests/core/test_render.py`, `tests/cursor/test_contract_conformance.py`, `tests/cursor/test_locked_*.py` |
| Change Markdown report layout or Stage 1 digest budget       | `chat_report/core/render/markdown.py`, `chat_report/core/render/digest.py`                            | `tests/core/test_render.py`, `tests/cursor/test_locked_md.py`                                                                           |
| Change CLI flags, exit codes, or skill bundling              | `chat_report/cli.py`, `skills/chat-report/scripts/run.py`, `cursor/chat-report.py`                    | `tests/core/test_cli_e2e.py`, `tests/cursor/test_verify_script.py`                                                                      |

## 2. Pipeline Architecture

```
CLI (chat_report/cli.py)
  |
  +--> 1. Resolve Target: HarnessAdapter.resolve_id() / list_recent()
  |       (chat_report/adapters/cursor/resolver.py)
  |
  +--> 2. Extract Raw SessionIR: HarnessAdapter.extract_session()
  |       (chat_report/adapters/cursor/adapter.py + store.py + tool_map.py + hooks.py)
  |
  +--> 3. Classify SessionIR: classify_session(session, workspace)
  |       (chat_report/core/classifier.py)
  |
  +--> 4. Evaluate Heuristics: evaluate_diagnostics(session, workspace)
  |       (chat_report/core/diagnostics.py -> H01..H08 findings)
  |
  +--> 5. Assemble Report Payload:
  |       build_single_session_report() | build_diff_report() | build_aggregate_report()
  |       (chat_report/core/compare.py)
  |
  +--> 6. Validate & Render:
          assert_v2_report_conforms() -> JSON (.json) + Markdown (.md) + optional Stage 1 Digest (stdout)
          (chat_report/core/render/json_v2.py, markdown.py, digest.py)
```

### Layer Rules

1. **`chat_report/core/`** defines `SessionIR`, `HarnessAdapter`, semantic classification, heuristics, report builders, and renderers. It has zero dependency on Cursor-specific storage schemas.
2. **`chat_report/adapters/cursor/`** implements `HarnessAdapter` and registers `"cursor"` in `ADAPTER_REGISTRY`. It translates Cursor SQLite records into `SessionIR` with `CanonicalToolKind` values, leaving `ToolCallIR.semantic_class` empty until `classify_session()` runs.
3. **`skills/chat-report/`** packages `SKILL.md`, `scripts/run.py`, and a copy of `chat_report/` when installed via `--bundle-into`. `run.py` automatically resolves the bundled `scripts/chat_report` package if present, or falls back to the repository root `chat_report` package.

## 3. Cursor Storage & Resolution Contracts

### Read-Only SQLite Databases

- **`state.vscdb`** (resolved via `--state-db` > `CURSOR_STATE_DB` > OS `Cursor/User/globalStorage/state.vscdb`):
  - `composerHeaders`: Indexed session metadata table (`composerId`, `name`, `createdAt`, `lastUpdatedAt`, `isSubagent`, `recency`, `value`, `subagentTypeName`). Falls back to scanning `composerData:*` in `cursorDiskKV` when `composerHeaders` is absent.
  - `cursorDiskKV`: Key-value table storing:
    - `composerData:<chatId>`: Session-level metadata (`_v: 18` and legacy `_v: 3..17`), model config, `promptTokenBreakdown`, `promptContextUsageTree`, `subagentComposerIds`, and inline `fullConversationHeadersOnly`.
    - `composerConvHeaders:<chatId>:<chunk>`: Ordered conversation bubble header chunks.
    - `bubbleId:<chatId>:<bubbleId>`: Individual turn bubbles (`type == 1` user, `type == 2` assistant/tool/thinking) containing `text`, `thinking`, `toolFormerData`, `tokenCount`, and attached `context`.
    - `ofsContent:composer.content.<id>`: Offloaded tool call before/after file content blobs.
  - `ItemTable`: Queried for Cursor IDE version fallback (`cursor.version`, `releaseNotes/lastVersion`, `workbench.telemetry.lastVersion`).
- **`ai-code-tracking.db`** (resolved via `--tracking-db` > `CURSOR_TRACKING_DB` > `~/.cursor/ai-tracking/ai-code-tracking.db`):
  - `ai_code_hashes`: Maps `requestId` and `conversationId` to `source` (`composer` vs `human`) and `fileName`.
  - `scored_commits`: Commit attribution records matched against the session time window (`[start - 60s, end + 300s]`).
  - If the default tracking database is missing, extraction succeeds with `session.attribution = None` and emits warning `cursor-tracking-db-unavailable`. If `--tracking-db` or `CURSOR_TRACKING_DB` was explicitly set and is unreadable, extraction raises `StoreAccessError`.

### 4-Way Session ID Resolution (`resolver.py`)

Unless forced by `--chat-id` or `--request-id`, `resolve_chat_id()` resolves `<uuid>` in order:
1. Direct `composerData:<uuid>` lookup in `state.vscdb` (`source="chat-id"`).
2. `ai_code_hashes.requestId` lookup in `ai-code-tracking.db` (`source="request-id"`).
3. `composerData:*` scan for `latestChatGenerationUUID`, `chatGenerationUUID`, or `requestId` (`source="request-id-scan"`).
4. `bubbleId:*` scan for `requestId`, `serverBubbleId`, `bubbleId`, or bubble key segment (`source="request-id-scan"`).

`resolve_latest_session_id()` (`--latest`) scans recent main-thread sessions and skips empty 0-bubble tabs and in-flight Turn-1 `/chat-report` self-invocations when a prior substantive session exists.

## 4. Diagnostic Heuristics (`H01` through `H08`)

Implemented in `chat_report/core/diagnostics.py`. Findings are deduplicated by `finding_key` (`<HEURISTIC_ID>:<subject>`) and sorted by `(SEVERITY_RANK[severity], turn_index, id, finding_key)`.

| ID                                       | Severity          | Trigger Rule                                                                                                                                                                                                                                                                 |
| :--------------------------------------- | :---------------- | :--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `H01_UNREAD_ATTACHED_OR_MENTIONED_SKILL` | `high`            | A skill is referenced by unattached `/slug`, prose, or thinking text, or matched by attached-skill domain edits/MCP calls, without harness inlining (`<manually_attached_skills>` / slash attachment) or any non-error `SKILL.md` read in parent or child subagent sessions. |
| `H02_BLIND_DYNAMIC_TOOL_CALL`            | `medium`          | External MCP tool call (`mcp_namespace != "cursor"`) executed before `mcp_discovery` (`GetDynamicTools`) for that catalog/namespace/tool, or failed with schema/parameter validation errors.                                                                                 |
| `H03_TOOL_RETRY_THRASH`                  | `high`            | Branch `4a`: `>= 3` consecutive or same-turn errored/denied tool calls on the same target path, MCP tool, or canonical kind. Branch `4b`: `>= 3` consecutive or same-turn empty `search_grep`/`search_glob` calls with identical query and path.                             |
| `H04_HOOK_OR_HARNESS_DENIAL`             | `high`            | Any tool call with `is_denied == True` (matched via structured denial payload, user rejection, `.cursor/hooks.json` script markers, or fallback denial markers).                                                                                                             |
| `H05_EDIT_LINT_THRASH`                   | `medium`          | A file is edited `>= 3` times across `>= 2` turns with `>= 2` intervening failed lint (`ReadLints`) or test/typecheck shell intervals targeting that file.                                                                                                                   |
| `H06_PROMPT_OR_TOOL_TOKEN_SPIKE`         | `medium`          | `rules + skills > 15,000` tokens, `context_usage_percent >= 75.0%`, or any tool call has `output_char_len >= 40,000`.                                                                                                                                                        |
| `H07_SUBAGENT_DISPATCH_ANOMALY`          | `medium`          | Child subagent status is not `"completed"`, has `denial_count > 0`, has high error rate (`>= 3` errors and `>= 75%` of calls), or a turn dispatches `>= 3` subagents sequentially across `>= 2` waves instead of a single parallel batch.                                    |
| `H08_INSTRUCTION_FRICTION_SIGNAL`        | `medium` / `info` | Unquoted thinking text matches conflict phrases (`conflicting instruction`, `contradicts`, `blocked by`, `not allowed to use`, etc.). Severity is `medium` when the turn has a tool error or denial, otherwise `info`.                                                       |

## 5. Report Schema & Validation Invariants

`assert_v2_report_conforms()` in `chat_report/core/render/json_v2.py` runs automatically before every JSON render and via `--validate <json_path>`.

- **Envelope keys (all report kinds):** `schema_version` (`"2.0"`), `report_kind` (`"single-session"` | `"diff"` | `"aggregate"`), `generated_at_iso`, `harness`, `harness_version`, `warnings`.
- **Single-session invariants:**
  - Sequential 1-based `turn_index` (`1..N`) and global sequential 1-based `call_index` (`1..M`) across all turns.
  - `sum(status_counts.values()) == sum(tool_calls_by_semantic_class.values()) == sum(tool_calls_by_canonical_kind.values()) == tool_calls_total`.
  - `diagnostics.total_findings == len(diagnostics.findings) == sum(diagnostics.by_severity.values())`.
  - `error_message` and `thinking_snippet` on `DiagnosticFinding` are truncated to `<= 160` characters.
- **Stage 1 Digest (`digest.py`):**
  - Hard ceiling of `2500` characters (`DIGEST_MAX_CHARS`), partitioned across four budgeted sections (`800` header, `400` customizations touched, `750` triggered findings, `550` Stage 2 recommendation).
  - Recommends Stage 2 subagent audit (`warranted`) when any `high` or `medium` finding or `H08` friction signal fires.
