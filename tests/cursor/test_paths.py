import json
import sqlite3
from pathlib import Path

from chat_report.adapters.cursor import paths


def test_default_paths_follow_windows_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))

    assert paths.home_dir() == tmp_path / "home"
    assert paths.cursor_user_dir() == tmp_path / "appdata" / "Cursor" / "User"
    assert paths.default_state_db() == (
        tmp_path / "appdata" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    )
    assert paths.default_tracking_db() == (
        tmp_path / "home" / ".cursor" / "ai-tracking" / "ai-code-tracking.db"
    )


def test_path_overrides_take_precedence(monkeypatch, tmp_path):
    explicit_state = tmp_path / "explicit-state.db"
    explicit_tracking = tmp_path / "explicit-tracking.db"
    monkeypatch.setenv("CURSOR_STATE_DB", str(tmp_path / "env-state.db"))
    monkeypatch.setenv("CURSOR_TRACKING_DB", str(tmp_path / "env-tracking.db"))

    assert paths.resolve_state_db(explicit_state) == explicit_state
    assert paths.resolve_state_db() == tmp_path / "env-state.db"
    assert paths.resolve_tracking_db_with_provenance(explicit_tracking) == (
        explicit_tracking,
        True,
    )
    assert paths.resolve_tracking_db_with_provenance() == (
        tmp_path / "env-tracking.db",
        True,
    )


def test_detect_cursor_version_uses_documented_precedence(monkeypatch, tmp_path):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
    con.execute(
        "INSERT INTO ItemTable VALUES (?, ?)",
        ("cursor.version", "Cursor 3.25.1"),
    )
    product_json = tmp_path / "product.json"
    product_json.write_text(json.dumps({"version": "3.24.0"}), encoding="utf-8")

    assert paths.detect_cursor_version(
        con, {"cursorVersion": "3.26.16"}, product_json_path=product_json
    ) == "3.26.16"
    assert paths.detect_cursor_version(con, {}, product_json_path=product_json) == "3.25.1"

    con.execute("DROP TABLE ItemTable")
    assert paths.detect_cursor_version(con, {}, product_json_path=product_json) == "3.24.0"

    product_json.write_text(json.dumps({"version": "dev"}), encoding="utf-8")
    monkeypatch.delenv("CURSOR_PRODUCT_JSON", raising=False)
    monkeypatch.setattr(paths, "_os_product_json_candidates", lambda: [])
    assert paths.detect_cursor_version(con, {}, product_json_path=product_json) == "unknown"
