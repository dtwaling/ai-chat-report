"""Hook script denial marker scanner and loader for Cursor (.cursor/hooks.json)."""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

BARE_MARKER_MIN_LEN = 12

FALLBACK_DENIAL_MARKERS: tuple[str, ...] = (
    "Blocked by preToolUse hook",
    '"permissionDecision":"deny"',
    '"permissionDecision": "deny"',
    "Auto-review blocked",
    "rejected by user",
    "blocked by a hook",
)

CONTEXT_DENIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:preToolUse hook|guard hook|hook script).{0,80}(?:deny|denied|block|blocked)", re.I | re.S),
)

_STRING_LITERAL_PATTERN = re.compile(r'''(?:"([^"\\]*(?:\\.[^"\\]*)*)"|'([^'\\]*(?:\\.[^'\\]*)*)')''')
_DENY_LINE_PATTERN = re.compile(r'\bdeny\b|"permissionDecision"\s*:\s*"deny"|\'permissionDecision\'\s*:\s*\'deny\'')
_MESSAGE_FIELD_PATTERN = re.compile(r"\b(?:reason|message|agent_message|msg)\b", re.IGNORECASE)
_DENIAL_TERM_PATTERN = re.compile(r"(?:deny|denied|block|blocked|reject|rejected)", re.IGNORECASE)
_HOOK_SCRIPT_EXTENSIONS = (".py", ".js", ".mjs")


def strip_jsonc_comments(text: str) -> str:
    """Strip // and /* */ comments from JSONC text while preserving string literals."""
    out: list[str] = []
    i = 0
    n = len(text)
    in_str = False
    quote_char = ""
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote_char:
                in_str = False
            i += 1
            continue

        if ch in ('"', "'"):
            in_str = True
            quote_char = ch
            out.append(ch)
            i += 1
            continue

        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] not in ("\n", "\r"):
                i += 1
            continue

        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue

        out.append(ch)
        i += 1

    cleaned = "".join(out)
    out = []
    i = 0
    in_str = False
    quote_char = ""
    while i < len(cleaned):
        ch = cleaned[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < len(cleaned):
                out.append(cleaned[i + 1])
                i += 2
                continue
            if ch == quote_char:
                in_str = False
            i += 1
            continue
        if ch in ('"', "'"):
            in_str = True
            quote_char = ch
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < len(cleaned) and cleaned[j].isspace():
                j += 1
            if j < len(cleaned) and cleaned[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def extract_hook_script_token(command: str) -> str | None:
    """Extract the first .py/.js/.mjs script path token from a hook command string."""
    if not command or not command.strip():
        return None
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError:
        tokens = command.split()

    for raw_tok in tokens:
        tok = raw_tok.strip().strip('"').strip("'")
        low = tok.lower()
        if any(low.endswith(ext) for ext in _HOOK_SCRIPT_EXTENSIONS):
            return tok
    return None


def extract_markers_from_hook_script(path: Path) -> list[str]:
    """Scan a .py, .js, or .mjs hook script for denial message literals (len >= 12)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []

    lines = text.splitlines()
    deny_indices: set[int] = {
        idx for idx, line in enumerate(lines) if _DENY_LINE_PATTERN.search(line)
    }

    extracted: list[str] = []
    seen: set[str] = set()
    for idx, line in enumerate(lines):
        near_deny = any(abs(idx - d) <= 5 for d in deny_indices)
        in_msg = bool(_MESSAGE_FIELD_PATTERN.search(line))
        if not near_deny and not in_msg:
            continue
        for m in _STRING_LITERAL_PATTERN.finditer(line):
            prefix = line[:m.start()].rstrip()
            if line[m.end():].lstrip().startswith(":") and (not prefix or prefix[-1] in "{,("):
                continue
            val = m.group(1) if m.group(1) is not None else m.group(2)
            if val is None:
                continue
            val = val.replace("\\n", " ").replace('\\"', '"').replace("\\'", "'").strip()
            if len(val) < BARE_MARKER_MIN_LEN or "${" in val:
                continue
            if not any(ch.isspace() for ch in val) and not _DENIAL_TERM_PATTERN.search(val):
                continue
            if val.startswith(("//", "/*", "node:", "node_modules", "http://", "https://")):
                continue
            if val not in seen:
                seen.add(val)
                extracted.append(val)
    return extracted


def load_denial_markers(
    workspace: Path | None = None,
    extra_markers: list[str] | None = None,
    verbose: bool = False,
) -> tuple[str, ...]:
    """Load denial markers from workspace .cursor/hooks.json + fallback + extra markers."""
    collected: list[str] = []

    if workspace is not None:
        ws_root = Path(workspace)
        hooks_json = ws_root / ".cursor" / "hooks.json"
        if hooks_json.is_file():
            try:
                raw_text = hooks_json.read_text(encoding="utf-8")
                cfg = json.loads(strip_jsonc_comments(raw_text))
                if isinstance(cfg, dict):
                    hooks_obj = cfg.get("hooks")
                    if isinstance(hooks_obj, dict):
                        for hook_list in hooks_obj.values():
                            if not isinstance(hook_list, list):
                                continue
                            for hook_entry in hook_list:
                                if not isinstance(hook_entry, dict):
                                    continue
                                cmd = str(hook_entry.get("command") or "")
                                script_tok = extract_hook_script_token(cmd)
                                if not script_tok:
                                    continue
                                script_path = Path(script_tok)
                                if not script_path.is_absolute():
                                    script_path = ws_root / script_path
                                if script_path.is_file():
                                    collected.extend(extract_markers_from_hook_script(script_path))
            except (OSError, json.JSONDecodeError) as exc:
                if verbose:
                    print(f"[chat-report] warning: failed to parse {hooks_json}: {exc}", file=sys.stderr)

    collected.extend(FALLBACK_DENIAL_MARKERS)
    if extra_markers:
        for m in extra_markers:
            if m and m.strip():
                collected.append(m.strip())

    deduped: list[str] = []
    seen: set[str] = set()
    for m in collected:
        if m not in seen:
            seen.add(m)
            deduped.append(m)
    return tuple(deduped)


def detect_denial(result_text: str, denial_markers: tuple[str, ...] = FALLBACK_DENIAL_MARKERS) -> tuple[bool, str | None]:
    """Return (is_denied, denial_reason) if result_text matches any active denial marker or pattern."""
    if not result_text:
        return False, None

    for marker in denial_markers:
        if marker and marker in result_text:
            return True, marker

    for pat in CONTEXT_DENIAL_PATTERNS:
        m = pat.search(result_text)
        if m:
            return True, m.group(0)[:120]

    return False, None
