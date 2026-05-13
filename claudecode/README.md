# chat-report -- Claude Code variant

Extract Optimus-test-relevant signals from a Claude Code chat session and
emit a report in the **locked structured-report shape** that downstream
Optimus v2 telemetry, success-metric evaluation, and spike-1 integration
depend on.

Both the Claude Code variant (this directory) and the Cursor variant
(`../cursor/`) emit the same shape -- consumers must not branch on IDE.

---

## Installation

Python 3.10+, no third-party dependencies. Just clone the repo and run
the script.

```bash
python claudecode/chat-report.py --help
```

---

## Invocation

Three CLI modes. All three emit both markdown and JSON to the output dir
(`.claude/local/chat-reports/` by default).

### Single chat

```bash
python claudecode/chat-report.py <session-uuid>
```

The session UUID is the basename (minus `.jsonl`) of the session
transcript file under `~/.claude/projects/<sanitized-cwd>/`. The CLI
resolves the JSONL path from `--cwd` (defaults to the current working
directory) via the sanitization rule (see below). Override with
`--session-jsonl <path>` to point at a transcript directly.

### Diff (before / after)

```bash
python claudecode/chat-report.py --diff <idA> <idB>
```

Emits `before` + `after` + `delta` blocks. Delta = `after - before` per
aggregate counter and per Component A/B ratio. Pair direct paths with
`--session-jsonl <pathA> --session-jsonl <pathB>` (repeat the flag once
per id; paths pair by index with the positional ids).

### Aggregate (rollup)

```bash
python claudecode/chat-report.py --aggregate <id1> <id2> ...
```

Emits per-session aggregates plus a top-level summed aggregate. Same
`--session-jsonl` pairing rule as diff.

### Common flags

| Flag | Purpose |
|------|---------|
| `--cwd <dir>` | Working directory the session was invoked from. Used to resolve the JSONL path under `~/.claude/projects/<sanitized-cwd>/`. Defaults to the current working directory. |
| `--session-jsonl <path>` | Direct path to a session JSONL. Overrides `--cwd` resolution. Repeatable for diff / aggregate (paired by index with the positional ids). |
| `--shape locked` | Output JSON shape. Only `locked` is supported on the Claude Code variant -- this is a greenfield emitter with no legacy shape to deprecate. |
| `--format {md,json,both}` | Output format. Default `both`. |
| `--out <dir>` | Output directory. Default `.claude/local/chat-reports/`. |

---

## Where Claude Code stores chat history

The Claude Code transcript store is **HOME-rooted with no per-OS
divergence** -- unlike the Cursor variant which had a macOS
`Library/Application Support` / Linux XDG split.

| OS | Path |
|----|------|
| Windows | `%USERPROFILE%\.claude\projects\<sanitized-cwd>\<session-uuid>.jsonl` |
| macOS | `~/.claude/projects/<sanitized-cwd>/<session-uuid>.jsonl` |
| Linux | `~/.claude/projects/<sanitized-cwd>/<session-uuid>.jsonl` |

**Sanitization rule:** the working directory the user invoked Claude
Code in is converted to a project directory name by replacing every
non-alphanumeric character with `-`. Examples:

- `C:\_Source\ai-chat-report\` -> `C---Source-ai-chat-report`
- `/home/dustin/dev/project` -> `-home-dustin-dev-project`

**Override the root** with the `CLAUDE_CONFIG_DIR` env var. The
chat-report tool honors it on every platform; do not hardcode
`~/.claude/`.

**Per-session subagent transcripts + tool-result spillover** live in a
session-named subdirectory next to the main JSONL:

```
~/.claude/projects/<sanitized-cwd>/
  <session-uuid>.jsonl                  -- main session transcript
  <session-uuid>/
    subagents/
      agent-<agent-id>.jsonl            -- one JSONL per subagent invocation
      agent-<agent-id>.meta.json        -- {agentType, description}
    tool-results/
      <tool_use_id>.txt                 -- full payload spillover
```

**Lifecycle controls** that affect what's on disk:

- **Default retention is 30 days.** Configurable via the
  `cleanupPeriodDays` setting. The tool prints a clear "session not
  found" message rather than failing silently when a JSONL has been
  swept.
- **`CLAUDE_CODE_SKIP_PROMPT_HISTORY`** (env) and
  **`--no-session-persistence`** (CLI) cause Claude Code to skip writing
  the JSONL at all. Sessions started under either flag have no
  transcript on disk.

The full discovery + format spec is in `DISCOVERY.md`.

---

## Shape contract

The full contract lives in `MISSION-BRIEF.md` section 4. Top-level
fields in every JSON report:

```
report_version: "1.0"             (string, always "1.0" for the current spec)
report_kind:    "single-chat" | "diff" | "aggregate"
ide:            "claude-code"     (always "claude-code" from this tool)
ide_version:    string            ("mixed" in diff/aggregate when sessions differ)
session_id:     string            (raw)
session_id_normalized: string     (lowercased, no separators -- cross-IDE join key)
session_start_iso / session_end_iso / session_duration_s
turns:          [...]             (single-chat only; diff/aggregate carry per-session reports)
aggregates:     { ... }
warnings:       [{code, message}, ...]
```

Each `turn.tool_calls[i]` carries `call_index`, `tool_name`,
`tool_class`, `input_summary` / `input_payload`,
`output_summary` / `output_status`, `denial_reason`, `elapsed_ms`, and
an `informed_precision_read` block with
`{applicable, classification, reason}`.

`tool_class` enum:
`broad-sweep-read` | `broad-sweep-grep` | `broad-sweep-glob` |
`directory-index-read` | `optimus-mcp` | `edit` | `write` | `bash` |
`other`.

`aggregates.success_metric_components.component_a` measures
**optimus_* count >= 1.0x broad-sweep count**; `component_b` measures
**informed-precision-read count >= 1.0x uninformed-read count**.
`overall.pass` is the AND; `overall.partial_pass` is exactly-one-
component-passing.

### Validating a saved report

Use the verify script's offline mode (no claudecode state needed):

```bash
python claudecode/chat-report-verify.py --validate path/to/report.json
```

Auto-detects single-chat / diff / aggregate via `report_kind`. Exits 0
on conform, non-zero on violation with a path-qualified error message.

For end-to-end verification against the local claudecode store, copy
`chat-report-verify.config.template.json` to
`chat-report-verify.config.json`, fill in real session UUIDs (and
optional `sessionJsonl` paths), then run `chat-report-verify.py`
with no arguments.

---

## Shape versioning policy

Per brief section 4.5:

- **`report_version: "1.0"`** at the top of every JSON.
- **Breaking changes** (rename a field, change a type, remove a key)
  bump the minor version.
- **Additive changes** (new optional field, new tool_class enum value)
  do NOT bump the version. Consumers must tolerate unknown extra keys.

---

## Subagent rollup (additive)

Claude Code sessions that dispatch subagents (Task / Agent / Explore
tool invocations) produce one JSONL per subagent under
`<session-uuid>/subagents/agent-<agent-id>.jsonl`. The chat-report tool
attaches a per-session `aggregates.subagent_rollup` ledger when those
JSONLs exist:

```jsonc
{
  "aggregates": {
    "by_class": { ... },              // PRIMARY session only
    "subagent_rollup": {              // additive; absent when no subagents
      "total_subagent_calls": 32,
      "subagents": [
        {
          "agent_id": "...",
          "agent_type": "general-purpose",
          "description": "Branch ship-readiness audit",
          "by_class": { "read": 12, "grep": 3, "edit": 0, ... }
        },
        ...
      ]
    }
  }
}
```

**Per-DISCOVERY.md #4 PM sign-off (2026-05-12):**

- The field is **additive**: omitted entirely (no key) when no subagent
  JSONLs exist for the session, never emitted as `null`.
- The primary session's `aggregates.by_class` is **NOT augmented** with
  subagent tool calls. Subagent activity is a separate ledger;
  double-counting is explicitly disallowed.
- Cursor variant reports may omit the field. Downstream consumers treat
  it as optional and tolerate either presence or absence.

---

## JSON `Infinity` handling

When Component A's broad-sweep count is zero with a non-zero optimus
count (or Component B's uninformed count is zero with a non-zero
informed count), `ratio` is mathematically infinite. The report emits
Python's `float("inf")`, which Python's stdlib `json` library writes as
the literal token `Infinity` (non-standard JSON, but the Python default
with `allow_nan=True`).

Consumers reading these JSON files must do one of:

- Use Python's `json.load` / `json.loads` (default `parse_constant`
  handles `Infinity` / `-Infinity` / `NaN`).
- Pre-process with `allow_nan=True` aware parsers (e.g., `simplejson`
  with `allow_nan=True`, or post-replace the literals before
  strict-JSON parsing).

The `assert_locked_*_conforms` validators in
`common/_locked_contract.py` treat `float` as the type for `ratio` and
tolerate any IEEE-754 value.

---

## No legacy shape

Unlike the Cursor variant, the Claude Code variant has **no legacy
shape**. The tool is greenfield as of B.3 -- the locked shape is the
only output. There is no `--shape=legacy` mode and no deprecation
window to navigate. The `--shape` argument is preserved on the CLI for
forward compatibility but currently accepts only `locked`.

---

## Files in this directory

| File | Purpose |
|------|---------|
| `chat-report.py` | Main emitter. CLI + library. |
| `chat-report-verify.py` | Verification harness. `--validate <path>` for offline JSON checks; default mode runs the full integration suite against the local claudecode store (requires `chat-report-verify.config.json`). |
| `chat-report-verify.config.template.json` | Template for the per-machine integration-mode config. Copy to `chat-report-verify.config.json` and fill in your session UUIDs. |
| `_jsonl.py` | JSONL record streaming. |
| `_paths.py` | HOME-rooted path resolution + sanitization + `CLAUDE_CONFIG_DIR` honoring. |
| `_classify.py` | Tool-name -> tool_class mapping. |
| `_report.py` | Locked-shape builder + markdown / JSON writers. |
| `DISCOVERY.md` | B.1 deliverable: empirical JSONL store spec + lifecycle controls. |
| (validator) | The locked-shape contract validator lives in `common/_locked_contract.py` -- it is IDE-agnostic and shared with the Cursor variant. |
