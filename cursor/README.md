# chat-report -- Cursor variant

Extract Optimus-test-relevant signals from a Cursor chat session and emit a
report in the **locked structured-report shape** that downstream Optimus v2
telemetry, success-metric evaluation, and spike-1 integration depend on.

Both the Cursor variant (this directory) and the Claude Code variant (`../claudecode/`)
emit the same shape -- consumers must not branch on IDE.

---

## Installation

Python 3.10+, no third-party dependencies. Just clone the repo and run the
script.

```bash
python cursor/chat-report.py --help
```

---

## Invocation

Three CLI modes. All three emit both markdown and JSON to the output dir
(`.cursor/local/chat-reports/` by default).

### Single chat

```bash
python cursor/chat-report.py <request-or-chat-uuid>
```

The CLI auto-resolves Request IDs (the UUIDs Cursor surfaces via
right-click -> "Copy Request ID") to chat IDs through the ai-tracking DB,
with bubble-scan and direct chat-ID fallbacks. Force resolution with
`--request-id <uuid>` or `--chat-id <uuid>` if needed.

### Diff (before / after)

```bash
python cursor/chat-report.py --diff <idA> <idB>
```

Emits `before` + `after` + `delta` blocks. Delta = `after - before` per
aggregate counter and per Component A/B ratio.

### Aggregate (rollup)

```bash
python cursor/chat-report.py --aggregate <id1> <id2> ...
```

Emits per-session aggregates plus a top-level summed aggregate.

### Common flags

| Flag | Purpose |
|------|---------|
| `--shape {locked,legacy}` | Output JSON shape. `locked` is the default and matches the integration contract; `legacy` is deprecated and emits a stderr warning. |
| `--format {md,json,both}` | Output format. Default `both`. |
| `--out <dir>` | Output directory. Default `.cursor/local/chat-reports/`. |
| `--state-db <path>` | Override Cursor state DB location. |
| `--tracking-db <path>` | Override ai-tracking DB location. |
| `--request-id <uuid>` / `--chat-id <uuid>` | Force ID resolution mode. |
| `--denial-marker <str>` | Add an extra denial marker (repeatable). |
| `--verbose` | Echo resolution and denial-marker detail to stderr. |
| `--dump-bubbles` | Also write raw bubble payloads (`<chatId>.bubbles.jsonl`). |

---

## Where Cursor stores chat history per OS

Cursor is a VS Code fork and inherits VS Code's profile conventions.

| OS | `state.vscdb` (chat history) | `ai-code-tracking.db` |
|----|------------------------------|------------------------|
| Windows | `%APPDATA%\Cursor\User\globalStorage\state.vscdb` | `%USERPROFILE%\.cursor\ai-tracking\ai-code-tracking.db` |
| macOS | `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` | `~/.cursor/ai-tracking/ai-code-tracking.db` |
| Linux | `~/.config/Cursor/User/globalStorage/state.vscdb` (or `$XDG_CONFIG_HOME/Cursor/User/...`) | `~/.cursor/ai-tracking/ai-code-tracking.db` |

The Windows path was empirically verified for this project; macOS and Linux
are convention-based (VS Code fork inheritance). If your install diverges,
override with `CURSOR_STATE_DB` / `CURSOR_TRACKING_DB` env vars or the
`--state-db` / `--tracking-db` flags.

---

## Shape contract

The full contract lives in `MISSION-BRIEF.md` section 4. Top-level fields
in every JSON report:

```
report_version: "1.0"             (string, always "1.0" for the current spec)
report_kind:    "single-chat" | "diff" | "aggregate"
ide:            "cursor"          (always "cursor" from this tool)
ide_version:    string            ("mixed" in diff/aggregate when sessions differ)
session_id:     string            (raw)
session_id_normalized: string     (lowercased, no separators -- cross-IDE join key)
session_start_iso / session_end_iso / session_duration_s
turns:          [...]             (single-chat only; diff/aggregate carry per-session reports)
aggregates:     { ... }
warnings:       [{code, message}, ...]
```

Each `turn.tool_calls[i]` carries `call_index`, `tool_name`, `tool_class`,
`input_summary` / `input_payload`, `output_summary` / `output_status`,
`denial_reason`, `elapsed_ms`, and an `informed_precision_read` block with
`{applicable, classification, reason}`.

`tool_class` enum:
`broad-sweep-read` | `broad-sweep-grep` | `broad-sweep-glob` |
`directory-index-read` | `optimus-mcp` | `edit` | `write` | `bash` | `other`.

`aggregates.success_metric_components.component_a` measures
**optimus_* count >= 1.0x broad-sweep count**; `component_b` measures
**informed-precision-read count >= 1.0x uninformed-read count**. `overall.pass`
is the AND; `overall.partial_pass` is exactly-one-component-passing.

### Validating a saved report

Use the verify script's offline mode (no Cursor DB needed):

```bash
python cursor/chat-report-verify.py --validate path/to/report.json
```

Auto-detects single-chat / diff / aggregate via `report_kind`. Exits 0 on
conform, non-zero on violation with a path-qualified error message.

---

## Shape versioning policy

Per brief section 4.5:

- **`report_version: "1.0"`** at the top of every JSON.
- **Breaking changes** (rename a field, change a type, remove a key) bump the
  minor version.
- **Additive changes** (new optional field, new tool_class enum value) do NOT
  bump the version. Consumers must tolerate unknown extra keys.

---

## Known thinness: synthesized per-turn timestamps on Cursor

Cursor's bubble store does not record per-turn timestamps. The locked-shape
contract requires `turn.started_iso` / `turn.ended_iso`, so the cursor variant
synthesizes them from the chat-level `createdAt` / `lastUpdatedAt` window. The
report carries an explicit warning to flag this thinness to consumers:

```json
{"warnings": [{"code": "cursor-per-turn-timestamps-synthesized",
               "message": "..."}]}
```

The Claude Code variant will not carry this warning -- its JSONL store has
real per-record timestamps.

---

## JSON `Infinity` handling

When Component A's broad-sweep count is zero with a non-zero optimus count
(or Component B's uninformed count is zero with a non-zero informed count),
`ratio` is mathematically infinite. The report emits Python's `float("inf")`,
which Python's stdlib `json` library writes as the literal token `Infinity`
(non-standard JSON, but the Python default with `allow_nan=True`).

Consumers reading these JSON files must do one of:

- Use Python's `json.load` / `json.loads` (default `parse_constant` handles
  `Infinity` / `-Infinity` / `NaN`).
- Pre-process with `allow_nan=True` aware parsers (e.g., `simplejson` with
  `allow_nan=True`, or post-replace the literals before strict-JSON parsing).

The `assert_locked_*_conforms` validators in `common/_locked_contract.py`
treat `float` as the type for `ratio` and tolerate any IEEE-754 value.

---

## Legacy shape deprecation

The `--shape=legacy` mode emits the pre-locked-shape JSON. It is **deprecated
as of PR-A2** and prints a one-line `DeprecationWarning` to stderr on every
invocation. Existing consumers can continue using it during the deprecation
window; removal date is TBD by optimus and will be announced in the parent
project. Migrate to the default locked shape at your earliest opportunity.

---

## Files in this directory

| File | Purpose |
|------|---------|
| `chat-report.py` | Main emitter. CLI + library. |
| `chat-report-verify.py` | Verification harness. `--validate <path>` for offline JSON checks; default mode runs the full integration suite against the local Cursor state DB (requires `chat-report-verify.config.json`). |
| `chat-report-verify.config.template.json` | Template for the per-machine integration-mode config. Copy to `chat-report-verify.config.json` and fill in your IDs. |
| (validator moved) | The locked-shape contract validator lives in `common/_locked_contract.py` -- it is IDE-agnostic and shared with the Claude Code variant. |
