"""Cross-OS Cursor SQLite path resolution and version provenance detection."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

_VERSION_REGEX = re.compile(r"\b(\d+\.\d+\.\d+)\b")


def home_dir() -> Path:
    """Resolve OS-specific user home directory without os.path.expanduser."""
    var = "USERPROFILE" if sys.platform == "win32" else "HOME"
    value = (os.environ.get(var) or "").strip()
    if not value:
        raise ValueError(
            "Cannot determine user home directory; set USERPROFILE/HOME or pass "
            "--state-db / CURSOR_STATE_DB and --tracking-db / CURSOR_TRACKING_DB."
        )
    return Path(value)


def cursor_user_dir() -> Path:
    """Resolve Cursor's User directory across Windows, macOS, and Linux."""
    if sys.platform == "win32":
        appdata = (os.environ.get("APPDATA") or "").strip()
        if appdata:
            return Path(appdata) / "Cursor" / "User"
        return home_dir() / "AppData" / "Roaming" / "Cursor" / "User"
    if sys.platform == "darwin":
        return home_dir() / "Library" / "Application Support" / "Cursor" / "User"
    xdg = (os.environ.get("XDG_CONFIG_HOME") or "").strip()
    base = Path(xdg) if xdg else home_dir() / ".config"
    return base / "Cursor" / "User"


def default_state_db() -> Path:
    """Return the OS-default path to Cursor's globalStorage/state.vscdb."""
    return cursor_user_dir() / "globalStorage" / "state.vscdb"


def default_tracking_db() -> Path:
    """Return the OS-default path to Cursor's ai-code-tracking.db."""
    return home_dir() / ".cursor" / "ai-tracking" / "ai-code-tracking.db"


def resolve_state_db(explicit: Path | None = None) -> Path:
    """Resolve state.vscdb path with precedence: CLI explicit > CURSOR_STATE_DB > default."""
    if explicit is not None:
        return Path(explicit)
    env_val = (os.environ.get("CURSOR_STATE_DB") or "").strip()
    if env_val:
        return Path(env_val)
    return default_state_db()


def resolve_tracking_db_with_provenance(explicit: Path | None = None) -> tuple[Path, bool]:
    """Resolve ai-code-tracking.db and whether an explicit override was supplied."""
    env_val = (os.environ.get("CURSOR_TRACKING_DB") or "").strip()
    is_explicit_override = (explicit is not None) or bool(env_val)
    if explicit is not None:
        return Path(explicit), True
    if env_val:
        return Path(env_val), True
    return default_tracking_db(), is_explicit_override


def _os_product_json_candidates() -> list[Path]:
    """Return OS-specific default candidate paths for Cursor's product.json."""
    if sys.platform == "win32":
        candidates: list[Path] = []
        local_app = (os.environ.get("LOCALAPPDATA") or "").strip()
        if local_app:
            candidates.append(Path(local_app) / "Programs" / "Cursor" / "resources" / "app" / "product.json")
        prog_files = (os.environ.get("PROGRAMFILES") or "").strip()
        if prog_files:
            candidates.append(Path(prog_files) / "Cursor" / "resources" / "app" / "product.json")
        return candidates
    if sys.platform == "darwin":
        candidates = [Path("/Applications/Cursor.app/Contents/Resources/app/product.json")]
        try:
            candidates.append(home_dir() / "Applications" / "Cursor.app" / "Contents" / "Resources" / "app" / "product.json")
        except ValueError:
            pass
        return candidates
    return [
        Path("/usr/share/cursor/resources/app/product.json"),
        Path("/opt/Cursor/resources/app/product.json"),
    ]


def detect_cursor_version(
    state_con: sqlite3.Connection | None,
    composer_data: dict[str, Any] | None = None,
    *,
    product_json_path: Path | None = None,
) -> str:
    """Detect Cursor version via 4-step precedence: composer_data -> ItemTable -> product.json -> 'unknown'."""
    if isinstance(composer_data, dict):
        for k in ("cursorVersion", "ideVersion", "version"):
            val = composer_data.get(k)
            if isinstance(val, str) and val.strip():
                return val.strip()

    if state_con is not None:
        try:
            keys = ("cursor.version", "releaseNotes/lastVersion", "workbench.telemetry.lastVersion")
            for k in keys:
                row = state_con.execute("SELECT value FROM ItemTable WHERE key = ?", (k,)).fetchone()
                if row and row[0] is not None:
                    raw = row[0].decode("utf-8", errors="replace") if isinstance(row[0], (bytes, bytearray)) else str(row[0])
                    m = _VERSION_REGEX.search(raw)
                    if m:
                        return m.group(1)
        except sqlite3.Error:
            pass

    product_candidates: list[Path] = []
    if product_json_path is not None:
        product_candidates.append(Path(product_json_path))
    env_prod = (os.environ.get("CURSOR_PRODUCT_JSON") or "").strip()
    if env_prod:
        product_candidates.append(Path(env_prod))
    product_candidates.extend(_os_product_json_candidates())

    for cand in product_candidates:
        try:
            if cand.is_file():
                data = json.loads(cand.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    ver = data.get("version")
                    if isinstance(ver, str):
                        m = _VERSION_REGEX.search(ver)
                        if m:
                            return m.group(1)
        except (OSError, json.JSONDecodeError):
            continue

    return "unknown"
