# Claude Code Chat-History Store -- Discovery Report

**Status:** B.1 deliverable for the chat-report sibling delegated session.
**Feasibility gate verdict:** **POSITIVE** -- every Q1-Q3 sub-question is answerable from on-disk artifacts alone, and the store carries materially richer tool-call attribution than the Cursor variant.
**Authored:** 2026-05-12
**Claude Code version observed (this session):** `2.1.140`
**Observation method:** empirical introspection of the running session's own JSONL (Windows), cross-checked against 14 other JSONLs from sibling project directories on the same machine, plus official documentation lookups via the `claude-code-guide` agent for cross-OS path confirmation.

This report is the chat-report sibling project's load-bearing input for downstream consumers:
- **Spike-1** (`docs/decomp/pre-M1-spikes.md`): consumes the structured report shape this discovery enables.
- **Success metric** (`docs/decisions/success-metric.md`): Component A counts tool classes the schema must expose; Component B's informed-precision-read heuristic depends on Q3.d temporal ordering and Q3.e turn boundaries.
- **M5 plugin format research** (B.2): separate report; this is Q1-Q3 only.

The locked report shape that this discovery must support is in the mission brief at section 4 (`MISSION-BRIEF.md` clone root / `optimus/docs/delegated-sessions/chat-report-mission-brief.md` canonical).

---

## Q1 -- Where on disk is the chat-history store, per supported OS?

**Per-OS root + sanitization rule (officially documented + empirically verified on Windows):**

| OS | Path |
|----|------|
| Windows | `%USERPROFILE%\.claude\projects\<sanitized-cwd>\<session-uuid>.jsonl` |
| macOS | `~/.claude/projects/<sanitized-cwd>/<session-uuid>.jsonl` |
| Linux | `~/.claude/projects/<sanitized-cwd>/<session-uuid>.jsonl` |

**Sanitization rule:** the working directory the user invoked Claude Code in is converted to a project directory name by replacing every non-alphanumeric character with `-`. Empirical Windows evidence: `C:\_Source\ai-chat-report\` becomes `C---Source-ai-chat-report`. POSIX evidence (per docs): `/home/dustin/dev/project` becomes `-home-dustin-dev-project`.

**Per-session subagent transcripts + tool-result spillover** live in a session-named subdirectory next to the main JSONL:

```
~/.claude/projects/<sanitized-cwd>/
  <session-uuid>.jsonl                          -- the main session transcript
  <session-uuid>/                                -- session-private artifacts
    subagents/
      agent-<agent-id>.jsonl                     -- one JSONL per subagent invocation
      agent-<agent-id>.meta.json                 -- {agentType, description}
    tool-results/
      <tool_use_id>.txt                          -- full payload spillover (see Q2/Q3.c)
```

Empirically observed: dispatching a subagent during this session created `subagents/` and `tool-results/` subdirs on demand. The subagent JSONL carries the **same `sessionId`** as the parent (provenance preserved) AND a new `agentId` field linking back to the parent's Agent tool invocation. The accompanying `.meta.json` is the small (~87 bytes) sidecar holding the subagent's type and PM-supplied description.

**Override + lifecycle controls (from official docs):**
- **Root directory override:** `CLAUDE_CONFIG_DIR` env var. The chat-report tool MUST honor this; do not hardcode `~/.claude/`.
- **Default retention:** **30 days**, configurable via the `cleanupPeriodDays` setting. The chat-report tool MUST surface a clear "session expired / not found" warning rather than failing silently when a session JSONL has been swept.
- **Skip session persistence:** `CLAUDE_CODE_SKIP_PROMPT_HISTORY` env var; `--no-session-persistence` CLI flag. Sessions started under either flag will have NO JSONL on disk -- the chat-report tool must produce a clean "no data" report rather than a partial one in that case.
- **Path stability:** no documented breaking changes in the 2.x line. No backwards-compatibility shims needed at this writing.

**Sources for the per-OS confirmations + lifecycle controls:**
- <https://code.claude.com/docs/en/sessions> -- "Export and locate session data" section.
- <https://code.claude.com/docs/en/claude-directory> -- `.claude/` directory layout.
- <https://code.claude.com/docs/en/env-vars> -- `CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_SKIP_PROMPT_HISTORY`.
- <https://code.claude.com/docs/en/settings> -- `cleanupPeriodDays`.

**Worktree-isolation subagent runs land at the SAME location.** Empirically verified during the cold-reviewer pass on this discovery report: dispatching a subagent with `isolation: "worktree"` wrote its transcript to `<session-uuid>/subagents/agent-<agent-id>.jsonl` in the parent host's `~/.claude/projects/<sanitized-cwd>/` -- not into the temporary worktree's own `.claude/` directory. The worktree is filesystem-isolation for code, not transcript-storage redirection. Q1 path claim holds unchanged for isolated subagents.

**Undocumented adjacent path demystified -- NOT load-bearing:** `~/.claude/sessions/` is **per-process liveness state**, NOT chat history. Each file is named by the host OS process ID (e.g. `4352.json`, `51312.json`) and carries `{pid, sessionId, cwd, startedAt, updatedAt, version, peerProtocol, kind: "interactive", entrypoint: "cli", status: "idle" | "busy"}`. This is the daemon-style discovery state that lets a second Claude Code invocation see which sessions are already running on the host. The chat-report tool MUST NOT read from `~/.claude/sessions/`; the canonical source is `~/.claude/projects/`.

### Q1 verdict: POSITIVE.

Location + sanitization + lifecycle controls are all knowable and stable. Worktree-isolation does not perturb the path. The PID-keyed liveness directory is structurally separate and confirmed irrelevant.

---

## Q2 -- What is the storage format?

**Format:** **JSONL** (newline-delimited JSON, UTF-8). One record per line. Append-only as the session progresses (verified empirically -- the running session's file grew from 663KB to 857KB during the discovery work).

**No SQLite. No proprietary binary. No external decoder needed.** Standard JSON parsing is sufficient. (Contrast: Cursor stores everything in a SQLite BLOB field of `state.vscdb` requiring `decode_blob()` and JSON-string-of-JSON unwrapping. The Claude Code format is dramatically friendlier.)

**Record types observed in this session (`d5093750-...`, 297 records total over ~15 user turns):**

| Type | Count | Purpose |
|------|------:|---------|
| `attachment` | 110 | System-injected context (hook outputs, task reminders, skill listings, deferred-tool deltas, MCP-server instructions) |
| `assistant` | 81 | Assistant turns. `message.content[]` carries text / `tool_use` / `thinking` blocks |
| `user` | 55 | User turns. `message.content[]` carries `tool_result` blocks for tool returns + occasional raw user text |
| `last-prompt` | 15 | Turn boundary marker. `{type, leafUuid, sessionId}`. `leafUuid` points at the assistant uuid that preceded the prompt. **15 entries == 15 user prompts** (1:1 with user-typed messages -- key for Q3.e) |
| `permission-mode` | 15 | Captures permission mode at each prompt (`default`, etc.). 1:1 with `last-prompt`. |
| `ai-title` | 14 | Title-generation records (the auto-generated session title). Not load-bearing for the chat-report contract |
| `file-history-snapshot` | 7 | File-history tracking ("did the model edit a file we are tracking"). Sidecar metadata; not load-bearing |

**Cross-session schema stability (probed in a sibling project, `108dc93e-...`, Claude Code version 2.1.128):** same core type set. Two additional types observed: `system` (carries `{subtype: "turn_duration", durationMs, messageCount, ...}` per turn -- useful telemetry) and `queue-operation` (records enqueued/dequeued user prompts, e.g. STOP commands). The chat-report tool MUST treat unknown types as ignorable warnings rather than fatal -- the format is clearly growing additively.

**Per-record metadata fields present on essentially every record** (the "envelope"):

```
parentUuid, isSidechain, uuid, type, timestamp, userType, entrypoint, cwd, sessionId, version, gitBranch
```

- `parentUuid` + `uuid`: explicit DAG linkage. The session is a tree, not just a flat list -- subagent invocations branch from a parent assistant message's uuid.
- `isSidechain`: boolean; `true` for subagent-internal records (in subagent JSONLs), `false` for main thread.
- `timestamp`: ISO 8601 with millisecond precision (e.g. `2026-05-13T01:16:32.909Z`). UTC.
- `version`: Claude Code version (e.g. `2.1.140`). **Recorded per-record, not per-session header** -- which means a long-lived session can span Claude Code upgrades and the version delta is visible per-record. Surface this in the report.
- `cwd`: working directory at the time the record was written. Captures `cd` events mid-session.
- `gitBranch`: branch at the time the record was written. Same -- captures branch switches.
- `sessionId`: UUID of the parent session (the user's primary turn). Subagent records carry the parent session ID, NOT a new one -- which makes cross-referencing transcripts trivial.

### Q2 inline example records

**Assistant `tool_use` record (Read call, envelope + rich `message.usage` for cost analysis):**

```json
{
  "parentUuid": "15ef93dd-e39b-4153-b8d4-b47cd0abc505",
  "isSidechain": false,
  "message": {
    "model": "claude-opus-4-7",
    "id": "msg_01HD4eo1Ad2XhtnTkpRoLkrT",
    "type": "message",
    "role": "assistant",
    "content": [
      {
        "type": "tool_use",
        "id": "toolu_0118ckfKgqY7fQHiUcGUjgLL",
        "name": "Read",
        "input": {"file_path": "C:\\_Source\\ai-chat-report\\MISSION-BRIEF.md"},
        "caller": {"type": "direct"}
      }
    ],
    "stop_reason": "tool_use",
    "stop_sequence": null,
    "stop_details": null,
    "usage": {
      "input_tokens": 6,
      "cache_creation_input_tokens": 20511,
      "cache_read_input_tokens": 23552,
      "output_tokens": 370,
      "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
      "service_tier": "standard",
      "cache_creation": {"ephemeral_1h_input_tokens": 20511, "ephemeral_5m_input_tokens": 0},
      "speed": "standard"
    },
    "diagnostics": null
  },
  "requestId": "req_011CaygyHx43ddGfuULX5JzX",
  "type": "assistant",
  "uuid": "987decb5-66d3-4db5-8df2-bc9390a5a501",
  "timestamp": "2026-05-13T01:20:28.612Z",
  "userType": "external", "entrypoint": "cli",
  "cwd": "C:\\_Source\\ai-chat-report",
  "sessionId": "d5093750-dd1a-4a15-adae-79334235a2e7",
  "version": "2.1.140",
  "gitBranch": "claudecode-variant-discovery"
}
```

**Bonus capture (not required by locked shape but valuable):** `message.usage` provides per-turn token accounting -- input, output, cache creation (split by 1h vs 5m ephemeral lifetime), cache read, server-tool-use counters, service tier. This enables cost-per-turn analysis directly from the JSONL. The locked report shape does not require this, but the chat-report tool could surface it as an extended/off-contract field for forensics.

**User `tool_result` record (Bash response with `toolUseResult` sidecar + `sourceToolAssistantUUID` backlink):**

```json
{
  "parentUuid": "a7bec404-1696-48d8-902e-b29ba22e49f2",
  "isSidechain": false,
  "promptId": "d294ea65-34e1-436a-a69e-339482addd16",
  "type": "user",
  "message": {
    "role": "user",
    "content": [
      {
        "tool_use_id": "toolu_01AbKsC445JTiGXtjbtLbYVB",
        "type": "tool_result",
        "content": "On branch claudecode-variant-discovery\nnothing to commit, ...[TRUNCATED]",
        "is_error": false
      }
    ]
  },
  "uuid": "dc8bd298-808f-41b8-ac05-759b00f9bc77",
  "timestamp": "2026-05-13T01:22:15.645Z",
  "toolUseResult": {
    "stdout": "On branch claudecode-variant-discovery\nnothing to commit, working tree clean\n...",
    "stderr": "",
    "interrupted": false,
    "isImage": false,
    "noOutputExpected": false
  },
  "sourceToolAssistantUUID": "a7bec404-1696-48d8-902e-b29ba22e49f2",
  "userType": "external", "entrypoint": "cli",
  "cwd": "C:\\_Source\\ai-chat-report",
  "sessionId": "d5093750-dd1a-4a15-adae-79334235a2e7",
  "version": "2.1.140",
  "gitBranch": "claudecode-variant-discovery"
}
```

Note: `sourceToolAssistantUUID` directly points at the assistant uuid that issued the `tool_use`. The chat-report tool should prefer this over walking the `parentUuid` DAG for pairing tool_use <-> tool_result records.

**`last-prompt` record (canonical turn boundary marker):**

```json
{
  "type": "last-prompt",
  "leafUuid": "33f88f25-1ea6-45fd-be21-6be5b64abb3c",
  "sessionId": "d5093750-dd1a-4a15-adae-79334235a2e7"
}
```

`leafUuid` points at the assistant record that preceded the user's prompt.

### Q2 verdict: POSITIVE.

JSONL with stable, additively-growing schema. Standard JSON parsing suffices. Cross-version stability empirically tested at 2.1.140 and 2.1.128 (two adjacent 2.1.x patch versions on the same machine -- a 2.0.x sample would harden the "stable across the 2.x line" claim further; the official docs assert no breaking changes which is what carries the gate currently).

---

## Q3 -- Is tool-call attribution captured per turn?

### Q3.a -- Exact tool name used?

**YES.** Every `tool_use` block carries an explicit `name` field with the as-called tool name. Empirically observed in this session: `Read`, `Bash`, `Edit`, `Write`, `Grep`, `TaskCreate`, `TaskUpdate`, `ToolSearch`, `AskUserQuestion`. MCP-tool naming convention (confirmed via the deferred-tools listing in the session attachments): `mcp__<server-id>__<tool-name>` (e.g., `mcp__claude_ai_Cloudflare_Developer_Platform__d1_database_query`). The classifier the chat-report tool builds must recognize this prefix and route to the `optimus-mcp` class when the server is the optimus MCP server.

**`tool_use` block schema:**
```json
{
  "type": "tool_use",
  "id": "toolu_<base58>",
  "name": "Read",
  "input": { /* arbitrary tool-specific schema */ },
  "caller": {"type": "direct" /* or other origin markers */}
}
```

The `caller` field is uniformly `{"type": "direct"}` in this session (the assistant calling tools directly). Worth probing in a session that uses tools-spawning-tools patterns to see if other origin markers exist.

### Q3.b -- Input parameters per tool call?

**YES, FULLY.** `tool_use.input` is the complete input payload as a JSON object (or string in degenerate cases). Empirically observed: `{file_path: "..."}` for Read, full `{command, description, run_in_background?, timeout?}` for Bash, `{pattern, path?, glob?, output_mode?, ...}` for Grep, full `{file_path, content}` for Write, full `{file_path, old_string, new_string, replace_all?}` for Edit.

**The locked report shape (`MISSION-BRIEF.md` section 4.1)** allows both `input_summary` (short, deterministic) and `input_payload` (full). The full payload is directly available without truncation in the JSONL.

### Q3.c -- Output / result?

**YES, with a two-tier storage model.**

**Inline (always present):** the `tool_result` block on the corresponding `user`-type record carries:
```json
{
  "type": "tool_result",
  "tool_use_id": "toolu_<...>",      -- matches the tool_use.id verbatim
  "is_error": null | true | false,
  "content": "<string>" | [<content-block>, ...]
}
```

Plus a top-level `toolUseResult` sidecar on the SAME user record, with **per-tool-typed metadata**:

| Tool | `toolUseResult` sidecar shape |
|------|-------------------------------|
| `Read` | `{type: "text", file: {...}}` |
| `Bash` | `{stdout, stderr, interrupted, isImage, noOutputExpected}` |
| `Grep` | (string when input-validation failed; structured shape otherwise -- needs more sampling) |
| `Edit` | `{filePath, oldString, newString, originalFile, structuredPatch, userModified, replaceAll}` |
| `Write` | `{type: "create", filePath, content, structuredPatch, originalFile, userModified}` |
| `TaskCreate` | `{task: {...}}` |
| `TaskUpdate` | `{success, taskId, updatedFields, statusChange}` |
| `AskUserQuestion` | `{questions, answers}` |
| `ToolSearch` | `{matches, query, total_deferred_tools}` |

**Spillover (only when content exceeds the inline budget):** large tool results are split:
- **Inline `tool_result.content`** carries a summarized/truncated view (empirically: 2KB-11KB range for results that originally were 80KB+).
- **Full raw payload** is written to `~/.claude/projects/<sanitized-cwd>/<session-uuid>/tool-results/<tool_use_id>.txt` as plain text.

Empirical evidence: two `WebFetch` calls in a subagent invocation produced 80KB+ raw payloads. Their inline tool_result.content was 2042 and 2247 bytes respectively; the full content is in `tool-results/toolu_01HRVyAhXzStyeGDpEEpJLGg.txt` (80540 bytes) and `tool-results/toolu_01PpQKXvscFQggRhknkuQD6f.txt` (82884 bytes), keyed by tool_use_id.

**This split has no Cursor analogue.** The chat-report tool's `--full-payloads` output (per brief section 4.6) should resolve to these spillover files when present.

**Error / denial detection:** the `is_error: true` flag on the `tool_result` block is the primary signal. Empirically observed in this session: 6 errored tool results across `Read` (file-not-found, content-exceeds-tokens), `Write` (user denied a Write tool use -- the rejection message lands as content), `Grep` (input validation). The PM rejection content is verbatim user-typed text, so the chat-report tool's `denial_reason` field can extract from the content string. No separate `denial_reason` field exists -- it's content-string-based, like the Cursor variant's marker matching.

### Q3.d -- Temporal ordering within a turn?

**YES, three independent ordering signals:**

1. **Append-only file order.** The JSONL is written in chronological order. Reading top-to-bottom is reading the session as it unfolded.
2. **`timestamp` field.** ISO 8601 ms-precision UTC on every record. Sufficient to reconstruct order even if file is concatenated with another or processed out-of-order.
3. **`parentUuid` -> `uuid` DAG.** Each record's parent is the previous step in the trajectory. This is the explicit chain -- traversal order = causal order.

All three agree on the sessions inspected. The chat-report tool can use any of them; file order is the most efficient default for sequential processing.

### Q3.e -- Agent-turn boundary?

**YES, two complementary signals:**

1. **`last-prompt` records** mark the boundary on each user-typed prompt: `{type: "last-prompt", leafUuid, sessionId}`. The `leafUuid` points at the most recent assistant record before the prompt, so reading backwards is easy. Empirically: 15 `last-prompt` records in this session = 15 user prompts.

2. **`parentUuid` chains** within a turn. Walk forward from a user message's `uuid`; everything in that forward chain (until the next user message's `uuid` appears as a `parentUuid`) belongs to the same agent turn.

**Subagent boundary:** `isSidechain: true` marks subagent records. The subagent's transcript lives in its OWN JSONL (under `<session-uuid>/subagents/agent-<agent-id>.jsonl`), but its records carry the **parent's** `sessionId` and a new `agentId` field. The parent JSONL's tool_use for Agent dispatch is the entry point. Cross-reference: scan the parent JSONL for `tool_use` records where `name == "Agent"`; for each, look up `subagents/agent-<agent-id>.meta.json` by the agent ID returned in the tool_result.

**`promptId` semantics empirically resolved (was a risk; now closed).** The `promptId` field appears only on `user`-type records (70 of 401 records carried it in the running session, ALL `user`-type). Only **one** distinct `promptId` was observed across the whole session despite 22 user prompts having been issued -- so `promptId` is NOT a per-turn identifier. Hypothesis: it groups records by some longer-lived context (perhaps the model's persistent conversation thread, since `user` records are what get sent back to the API). The chat-report tool should NOT use `promptId` as a turn boundary. The `last-prompt` records and `parentUuid` DAG remain the canonical turn-boundary signals; Q3.e claim is unchanged.

### Q3 overall verdict: POSITIVE on all five sub-questions.

Tool-name + input + output + temporal + turn-boundary are ALL captured in a directly machine-parseable form, often more granularly than the Cursor variant's source data.

---

## Implementation considerations the B.3 build must handle

These are observations that affect the B.3 (Claude Code variant build) plan. None of them invalidate the gate; all of them shape how the build is structured.

### 1. Native JSON > BLOB unwrap

Python's `json.loads()` per line is the only parser needed. No SQLite, no `decode_blob()`, no nested JSON-string-of-JSON. The B.3 implementation should be **noticeably simpler** than the Cursor variant's bubble parser. Recommend a streaming line-by-line parser to handle very large session JSONLs (this 297-record session is ~857KB; a long-lived session could easily exceed 50MB).

### 2. Tool-class mapping is direct

The locked report shape's `tool_class` enum maps directly to tool names:

| `tool_class` | Tool names that map to it |
|--------------|---------------------------|
| `broad-sweep-read` | `Read` (when not preceded by a DIRECTORY_INDEX.md consult in the same turn -- see informed-precision heuristic) |
| `broad-sweep-grep` | `Grep` |
| `broad-sweep-glob` | `Glob` |
| `optimus-mcp` | `mcp__<...optimus...>__*` -- the optimus MCP server's exposed tools |
| `directory-index-read` | `Read` where `file_path` ends with `DIRECTORY_INDEX.md` |
| `edit` | `Edit`, `MultiEdit` (if it surfaces) |
| `write` | `Write` |
| `bash` | `Bash` |
| `other` | everything else (Agent, AskUserQuestion, TaskCreate, etc.) |

### 3. Informed-precision-read classification

The locked report shape (section 4.1) requires per-Read classification as `informed` vs `uninformed`. Per `docs/telemetry-heuristic.md`, an informed read = preceded by a DIRECTORY_INDEX.md consult **within the same agent turn**.

Implementation:
- Walk records in file order.
- For each `Read` call, scan backwards within the same agent turn (bounded by the preceding `last-prompt` or the most recent user message) for a prior `Read` of `DIRECTORY_INDEX.md`.
- If found: `classification: "informed"`. Otherwise: `classification: "uninformed"`.
- Flag the 5 known failure modes from `docs/telemetry-heuristic.md` as warning codes when applicable (`system-prompt-dir-index`, `dir-index-then-sweep`, `cached-context`, `same-turn-ambiguity`, `cross-ide-comparability`). The locked shape has `heuristic_failure_modes_flagged: ["<string>", ...]` for exactly this.

### 4. Subagent rollups [PM SIGN-OFF: 2026-05-12 -- additive, no escalation]

The locked report shape is per-session. Subagent transcripts inflate tool-call counts dramatically (the claude-code-guide dispatch alone produced 6 WebFetch + WebSearch calls). Decision: per spike-1's behavior-on-the-primary-agent measurement, **the main session's tool calls are the canonical count**. Subagent transcripts roll up into a separate top-level `aggregates.subagent_rollup` field (new, optional); subagent tool calls do NOT add to the primary session's `aggregates.by_class` counters -- otherwise the metric becomes gameable (dispatching a subagent inflates the optimus-MCP count without changing primary-agent behavior).

This is a small extension to the locked shape (one new optional field). Brief section 4.5 says additive non-breaking fields do NOT bump version; brief section 1 says "Any change to the locked structured-report shape" requires escalation. Reading: "change" in section 1 means "breaking change" in the section 4.5 sense; an additive optional field is non-breaking by construction. PM-confirmed (Dustin) on 2026-05-12 in the B.1 gate-clearance sync: additive interpretation stands. **The Cursor variant remains free to omit the field; downstream consumers MUST treat `aggregates.subagent_rollup` as optional.**

### 5. Lifecycle / retention warnings

The chat-report tool must surface clear warnings (not errors) for:
- Session JSONL not found -> warn "expired or never persisted; check `cleanupPeriodDays` setting and `CLAUDE_CODE_SKIP_PROMPT_HISTORY` env var."
- Session JSONL present but `~/.claude/projects/` directory itself missing -> warn "no Claude Code session history on this machine; install state may be reset."
- `CLAUDE_CONFIG_DIR` set to a non-default value -> mention in the report header so reviewers know the source root.

### 6. Subagent-spawned tool calls in spillover

Empirically, `tool-results/` is shared across the session + its subagents (the spillover IDs from the claude-code-guide subagent's WebFetch calls landed in the parent session's `tool-results/` dir). The chat-report tool should resolve spillover files by tool_use_id regardless of whether the call was main-thread or sidechain.

### 7. Adapter for record-type expansion

Two record types observed in older sessions (`system`, `queue-operation`) but not the running session. The format is clearly additive. The chat-report tool should **log + ignore** unknown record types rather than fail. Surface unknown types as a top-level `warnings[]` entry per the locked shape.

---

## Risks and unknowns (NOT gate-negative; flagged for B.3 planning)

1. **`caller` field richness.** Every tool_use in this session has `caller: {"type": "direct"}`. The field's existence suggests other values exist (e.g., "via Agent dispatch", "via slash-command expansion"). The B.3 implementation should treat the field as opaque metadata and surface it in the report rather than enumerate.

2. **`Grep` toolUseResult shape variation.** Empirically observed: a failed Grep call (input validation error) carried a plain-string `toolUseResult` rather than a structured object. Successful Grep calls likely carry a structured shape; need more sampling to characterize. The B.3 implementation should treat the sidecar as opaque and fall back to `tool_result.content` parsing.

3. **`stop_reason` distribution.** This session shows 100% `tool_use` stop reason because every assistant turn ended on a tool call. Real-world sessions will mix `end_turn`, `max_tokens`, `stop_sequence`, etc. Need to handle gracefully.

4. **Plugin tool naming variation.** The deferred-tools listing in this session contains many MCP tools (`mcp__claude_ai_Cloudflare_Developer_Platform__d1_database_query` etc.). Whether plugin-internal slash commands or skills surface as `tool_use` records with their own naming is worth probing.

5. **Denial-marker content-string fragility (cold-reviewer flag).** Denials surface via `is_error: true` plus content-string text (the user's denial message lands verbatim in `tool_result.content`). There is no separate structured `denial_reason` field. If denial wording changes across CC versions or hook-script variations, the chat-report tool's denial classifier silently breaks. Mitigation: the Cursor variant has the same problem and solves it with a marker-extraction-from-hook-scripts pattern (`cursor/chat-report.py:_load_denial_markers`) -- the Claude Code variant should adopt the same pattern, reading hook-script source from the resolved plugin install location (B.2 deliverable will surface that).

6. **Cross-version sampling thinness.** Only two adjacent 2.1.x patches (2.1.140 and 2.1.128) were sampled empirically. Official docs assert no breaking changes in the 2.x line; this is the gate-passing evidence. The B.3 implementation should snapshot a 2.0.x JSONL (or older) if one can be obtained, and add a regression fixture covering it.

### Risks closed during cold-reviewer pass (no longer outstanding)

- ~~Subagent worktree-isolation transcript location~~ -- resolved POSITIVE: same path as non-isolated.
- ~~`promptId` field semantics~~ -- resolved: not a turn boundary; `last-prompt` is canonical.
- ~~`~/.claude/sessions/` purpose~~ -- resolved: PID-keyed daemon liveness state, not chat history.

---

## Cross-references

- Mission brief (canonical): `C:/_Source/optimus/docs/delegated-sessions/chat-report-mission-brief.md`
- Mission brief (convenience copy): `C:/_Source/ai-chat-report/MISSION-BRIEF.md` (gitignored locally)
- Sibling charter (load-bearing): `C:/_Source/optimus/docs/decisions/chat-report-sibling-charter.md`
- Telemetry heuristic + known failure modes: `C:/_Source/optimus/docs/telemetry-heuristic.md`
- Success metric: `C:/_Source/optimus/docs/decisions/success-metric.md`
- Glossary entry on this store: `C:/_Source/optimus/docs/glossary.md` ("Claude Code chat-history store" section)
- Cursor variant (counterpart): `C:/_Source/ai-chat-report/cursor/chat-report.py`

---

## Gate decision

**Gate 1 verdict: POSITIVE.** Confirmed independently by a cold-reviewer pass (worktree-isolated subagent) on this same report; reviewer verdict was CONFIRM POSITIVE with caveats. Caveats were one of three kinds:

- **Resolved empirically before PR**: worktree-isolation subagent transcript location (same path), `~/.claude/sessions/` purpose (PID-keyed liveness), `promptId` semantics (not a turn marker). These three are reflected in the body of the report; the "Risks closed during cold-reviewer pass" section enumerates them.
- **Resolved by PM sign-off**: the additive `aggregates.subagent_rollup` field proposal (Implementation Consideration #4 above). PM ruling on 2026-05-12: additive, no escalation. Cursor variant may omit the field; downstream consumers treat it as optional.
- **Carried into PR body as caveats**: macOS/Linux paths are doc-only until the first non-Windows fixture runs; cross-version sampling is thin at two adjacent 2.1.x patches.

Recommend PR'ing this report (PR 2 per the brief's PR cadence) and proceeding to B.2 (plugin format research) in parallel with awaiting human review on PR 2. B.3 build is unblocked once B.2 also clears its gate.

**Recommended PR title (per brief section 6.3):** `docs(claudecode): chat-history store discovery findings`
