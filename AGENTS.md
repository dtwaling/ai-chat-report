# AGENTS.md

Operational guide for agents working in `dtw-ai-chat-report` (`chat_report` v2.0, schema `2.0`).
For system design, module boundaries, data contracts, and the repository search map, read `ARCHITECTURE.md`.

## 1. Purpose

`chat_report` extracts Cursor chat sessions from local SQLite databases (`state.vscdb` and `ai-code-tracking.db`), normalizes session data into `SessionIR`, evaluates deterministic heuristics (`H01` through `H08`), and emits JSON v2.0 and Markdown reports.

- **Runtime:** Python 3.10+ standard library only.
- **Test runner:** `pytest` (`python -m pytest tests/ -q`).
- **Database safety:** All SQLite connections use immutable read-only URI mode (`?mode=ro`).

## 2. CLI Entrypoints

| Entrypoint   | Command                                        | Purpose                                                                              |
| :----------- | :--------------------------------------------- | :----------------------------------------------------------------------------------- |
| Primary CLI  | `python -m chat_report ...`                    | Main CLI for extraction, diff, aggregation, validation, and bundling.                |
| Skill runner | `python skills/chat-report/scripts/run.py ...` | Self-contained skill launcher and atomic skill bundler.                              |
| Legacy shim  | `python cursor/chat-report.py ...`             | Strips deprecated v1 flags (`--shape`, `--tools`) and forwards to `chat_report.cli`. |

### Modes (Mutually Exclusive)

```powershell
# Extract the latest main-thread session (excludes subagents)
python -m chat_report --latest

# Extract a session by chat UUID or request UUID
python -m chat_report <uuid>

# Emit bounded Stage 1 agent digest (<= 2500 chars) to stdout + write .json and .md files
python -m chat_report --latest --agent-digest

# List N most recent sessions (default: 10) as TSV
python -m chat_report --list-recent 10

# Compare two sessions (before vs after)
python -m chat_report --diff <before_uuid> <after_uuid>

# Aggregate 2 or more sessions into a cohort report
python -m chat_report --aggregate <id1> <id2>

# Validate a JSON report against the v2.0 schema and arithmetic invariants
python -m chat_report --validate <path/to/report.json>

# Install global runtime + CLI shims + lightweight User skill (or Workspace skill with --workspace)
python -m chat_report --install [--bin-dir <dir>] [--runtime-dir <dir>] [--workspace <dir>] [--force]

# Install or refresh only the lightweight SKILL.md (defaults to User scope; Workspace with --workspace)
python -m chat_report --install-skill [--workspace <dir>] [--skill-dir <dir>] [--force]

# Install a self-contained skill bundle into a destination directory
python -m chat_report --bundle-into <dest_skill_dir> --force
```

### Flags

| Flag                       | Default                      | Effect                                                                                     |
| :------------------------- | :--------------------------- | :----------------------------------------------------------------------------------------- |
| `--harness <name>`         | `cursor`                     | Select registered harness adapter.                                                         |
| `--out <dir>`              | `.cursor/local/chat-reports` | Output directory for generated `.json` and `.md` reports.                                  |
| `--format {md,json,both}`  | `both`                       | Output format (`--agent-digest` forces `both`).                                            |
| `--agent-digest`           | `False`                      | Print Stage 1 digest (`<= 2500` chars) to stdout. Valid only with `<uuid>` or `--latest`.  |
| `--request-id`             | `False`                      | Force UUID resolution through request ID tables and bubble scans.                          |
| `--chat-id`                | `False`                      | Force UUID resolution against `composerData:<uuid>` only.                                  |
| `--workspace <dir>`        | `Path.cwd()`                 | Workspace root for `.cursor/hooks.json`, active customizations, and path canonicalization. |
| `--no-subagents`           | `False`                      | Skip child subagent extraction.                                                            |
| `--list-include-subagents` | `False`                      | Include child subagent sessions in `--list-recent` output.                                 |
| `--include-orphans`        | `False`                      | Include unreferenced `bubbleId:<chatId>:*` rows omitted from conversation headers.         |
| `--state-db <path>`        | OS default                   | Override path to Cursor `state.vscdb`.                                                     |
| `--tracking-db <path>`     | OS default                   | Override path to Cursor `ai-code-tracking.db`.                                             |
| `--denial-marker <str>`    | `[]`                         | Repeatable custom hook or harness denial substring marker.                                 |
| `--bin-dir <dir>`          | `~/.local/bin`               | Directory for `chat-report` CLI shims during `--install`.                                  |
| `--runtime-dir <dir>`      | OS user data dir             | Directory for the standalone `chat_report` runtime during `--install`.                     |
| `--skill-dir <dir>`        | Scope default                | Explicit destination directory for `SKILL.md` during `--install` / `--install-skill`.      |
| `--no-skill`               | `False`                      | Skip `SKILL.md` installation during `--install`.                                           |
| `--force`, `-f`            | `False`                      | Overwrite existing targets during `--install`, `--install-skill`, or `--bundle-into`.      |
| `--verbose`                | `False`                      | Print diagnostic parse warnings to stderr.                                                 |
| `-V`, `--version`          | -                            | Print `chat-report n.n.n (schema n.n)` and exit.                                           |

### Environment Variables & Exit Codes

- `CURSOR_STATE_DB`: Fallback path to `state.vscdb` when `--state-db` is omitted.
- `CURSOR_TRACKING_DB`: Fallback path to `ai-code-tracking.db` when `--tracking-db` is omitted.
- `CURSOR_PRODUCT_JSON`: Fallback path to Cursor `product.json` for version detection.
- `CHAT_REPORT_BIN_DIR`: Default shim directory for `--install` when `--bin-dir` is omitted.
- `CHAT_REPORT_RUNTIME_DIR`: Default runtime directory for `--install` when `--runtime-dir` is omitted.
- `CHAT_REPORT_PYTHON`: Explicit Python 3.10+ interpreter override used by installed CLI shims.
- **Exit codes:** `0` success, `1` runtime/storage/validation error, `2` CLI usage error.

## 3. Agent Workflow & Engineering Rules

1. **Maintain layer isolation:**
   - `chat_report/core/` is harness-agnostic. Never import `chat_report.adapters.*` inside `chat_report/core/`.
   - `chat_report/adapters/cursor/` extracts raw `SessionIR` only. Do not import `chat_report.core.classifier` inside the adapter.
2. **Preserve runtime portability:**
   - Do not add third-party runtime dependencies. Use Python 3.10+ standard library modules only.
   - Keep all strings and documentation cross-platform and ASCII-clean.
3. **Preserve database immutability:**
   - Open SQLite databases strictly through `chat_report.adapters.cursor.store.open_ro()` (`?mode=ro`, with paths from `chat_report.adapters.cursor.paths.resolve_state_db()`). Never write to or lock host databases.
4. **Sync local skill bundle after code or skill edits:**
   - `.cursor/skills/chat-report/` is a bundled copy of `skills/chat-report/` and `chat_report/`.
   - After modifying `chat_report/` or `skills/chat-report/`, refresh the local bundle:
     ```powershell
     python -m chat_report --bundle-into .cursor/skills/chat-report --force
     ```
5. **Run validation gates before finishing:**
   - Run the full test suite: `python -m pytest tests/ -q`
   - If report generation or schema logic changed, validate generated JSON output with `python -m chat_report --validate <report.json>`.
