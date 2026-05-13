# ai-chat-report

Extract Optimus-test-relevant signals from a chat session in either
**Cursor** or **Claude Code** and emit a structured report in a single
locked JSON shape that downstream consumers (telemetry, success-metric
evaluation, spike-1 integration) can consume without branching on IDE.

Two IDE variants, one contract. The locked shape is canonical and
identical across both -- consumers MUST NOT branch on `ide`.

---

## Repository layout

| Path | Purpose |
|------|---------|
| [`cursor/`](cursor/README.md) | Cursor variant. Reads `state.vscdb` (SQLite). |
| [`claudecode/`](claudecode/README.md) | Claude Code variant. Reads `~/.claude/projects/<sanitized-cwd>/<session>.jsonl`. |
| `common/` | IDE-agnostic shared modules. Locked-shape contract validator (`_locked_contract.py`) + diff / aggregate math (`_diff_aggregate.py`) + locked-shape helpers (`_locked_helpers.py`). |
| `tests/` | pytest suites. `tests/common/` for IDE-neutral logic, `tests/cursor/` + `tests/claudecode/` for variant-specific behavior. |

Each variant directory carries its own README with CLI invocation, OS
paths, shape contract summary, and per-variant caveats. The variant
READMEs are the entry points for users -- this file is the project map.

---

## Quick start

Python 3.10+, no third-party dependencies.

```bash
# Cursor variant (reads from Cursor's SQLite state DB)
python cursor/chat-report.py <request-or-chat-uuid>

# Claude Code variant (reads from ~/.claude/projects/ JSONL)
python claudecode/chat-report.py <session-uuid>
```

Both variants support `--diff <a> <b>` and `--aggregate <a> <b> ...`
modes. See each variant's README for the full flag surface.

---

## The locked shape

The full contract is authored in the project mission brief
(`optimus/docs/delegated-sessions/chat-report-mission-brief.md` in the
parent repo) and enforced in code by `common/_locked_contract.py`. Three
report kinds:

| `report_kind` | Top-level fields |
|---------------|------------------|
| `single-chat` | `session_id`, `session_id_normalized`, `session_start_iso`, `session_end_iso`, `session_duration_s`, `turns`, `aggregates`, `warnings` |
| `diff` | `before`, `after`, `delta`, `generated_at_iso` |
| `aggregate` | `session_count`, `sessions`, `aggregates`, `generated_at_iso` |

Common to all three: `report_version` (`"1.0"`), `report_kind`, `ide`
(`"cursor"` or `"claude-code"`), `ide_version`.

**Versioning policy:** `report_version: "1.0"`. Breaking changes (rename
a field, change a type, remove a key) bump the minor version. Additive
changes (new optional field, new `tool_class` enum value) do NOT bump
the version. Consumers must tolerate unknown extra keys.

---

## Validating a saved report

Each variant ships a verify script with an offline `--validate` mode
that auto-detects `report_kind` and dispatches to the matching
contract assertion. No state-DB or JSONL store needed.

```bash
python cursor/chat-report-verify.py --validate path/to/report.json
python claudecode/chat-report-verify.py --validate path/to/report.json
```

Exits 0 on conform, non-zero on violation with a path-qualified error
message. The dispatch table itself is IDE-agnostic
(`common/_locked_contract.py`) -- either verify script will validate
either variant's output.

For end-to-end verification against the local IDE store, run either
verify script with no `--validate` flag (requires a per-machine config;
see the variant READMEs).

---

## Testing

```bash
python -m pytest tests/ -q
```

The full suite covers:

- Locked-shape contract conformance (single / diff / aggregate) for
  both variants.
- IDE-neutral diff + aggregate math (`tests/common/`).
- Per-variant CLI behavior, error paths, and parser specifics.
- Verify-script offline + integration-mode wiring.

---

## Provenance + governance

This repo is the chat-report sibling project under the Optimus v2
program. The mission brief, charter, and feasibility gates live in the
parent project (`github.com/DWaling-eci/optimus`,
`docs/delegated-sessions/chat-report-mission-brief.md` and
`docs/decisions/chat-report-sibling-charter.md`). Downstream consumers
(spike-1, success metric, telemetry heuristic) wire against the locked
shape this repo emits.

The locked shape is the integration contract. Any change to it requires
re-opening a decision in the parent project and is out of scope for
work in this repo.
