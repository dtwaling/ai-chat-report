"""Synthetic Cursor database fixtures used by extraction tests."""

from __future__ import annotations

import json
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from chat_report.adapters.cursor.adapter import CursorAdapter
from chat_report.adapters.cursor.paths import resolve_state_db
from chat_report.core.adapter import ExtractOptions, HarnessAdapter
from chat_report.core.ir import SessionIR


DEFAULT_SESSION_ID = "0a7d3243-61b6-4b3a-8fc5-43c73e2db583"


def create_synthetic_state_db(
    db_path: Path, *, session_id: str = DEFAULT_SESSION_ID, bubble_count: int = 20
) -> Path:
    """Create a complete synthetic Cursor state database with no host dependencies."""
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("""CREATE TABLE composerHeaders (
        composerId TEXT PRIMARY KEY, workspaceId TEXT, createdAt INTEGER, lastUpdatedAt INTEGER,
        isArchived INTEGER, isSubagent INTEGER, recency INTEGER, checkpointAt INTEGER,
        value TEXT, subagentTypeName TEXT)""")
    data = {
        "composerId": session_id, "name": "Synthetic session", "createdAt": 1000,
        "lastUpdatedAt": 1000 + bubble_count * 100, "unifiedMode": "agent", "status": "completed",
        "modelConfig": {"modelName": "test-model", "maxMode": True},
        "contextTokensUsed": 100, "contextTokenLimit": 1000, "contextUsagePercent": 10.0,
        "totalLinesAdded": 3, "totalLinesRemoved": 1, "addedFiles": ["src/example.py"],
        "removedFiles": [], "stopHookLoopCount": 0, "subagentComposerIds": [],
        "promptTokenBreakdown": {"system_prompt": {"tokens": 5, "chars": 10}, "tools": {"tokens": 4, "chars": 8}},
        "promptContextUsageTree": [{"name": "system", "tokens": 5}], "headerLog": {"chunkSize": 256, "chunkCount": 1, "count": bubble_count},
        "fullConversationHeadersOnly": [],
    }
    con.execute("INSERT INTO composerHeaders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, "workspace", 1000, data["lastUpdatedAt"], 0, 0, 1, None, json.dumps({"name": data["name"]}), None))
    con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (f"composerData:{session_id}", json.dumps(data)))
    headers = []
    for index in range(bubble_count):
        bubble_id = f"bubble-{index:03d}"
        is_user = index % 2 == 0
        bubble = {
            "type": 1 if is_user else 2, "text": f"{'User' if is_user else 'Assistant'} {index}",
            "createdAt": "2026-01-01T00:00:00Z", "startedAtMs": 1000 + index * 100,
            "completedAtMs": 1050 + index * 100,
            "thinking": {"text": "reasoning"} if not is_user else None,
            "thinkingDurationMs": 25, "tokenCount": {"inputTokens": 2, "outputTokens": 3},
        }
        if is_user:
            bubble["requestId"] = f"request-{index}"
            bubble["context"] = {"cursorRules": [{"path": ".cursor/rules/test.mdc"}], "mentions": [], "fileSelections": []}
            bubble["attachedCodeChunks"] = []
        else:
            bubble["toolFormerData"] = {"name": "read_file_v2", "toolCallId": f"tool-{index}", "modelCallId": "model-1",
                "status": "completed", "params": {"targetFile": "src/example.py"}, "result": {"content": "ok"}}
        con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (f"bubbleId:{session_id}:{bubble_id}", json.dumps(bubble)))
        headers.append({"bubbleId": bubble_id, "type": bubble["type"]})
    con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (f"composerConvHeaders:{session_id}:000000", json.dumps(headers)))
    con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", ("ofsContent:composer.content.deadbeef", "before content"))
    con.commit()
    con.close()
    return db_path


def create_synthetic_tracking_db(db_path: Path, *, session_id: str = DEFAULT_SESSION_ID) -> Path:
    """Create a synthetic ai-code-tracking database."""
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE ai_code_hashes (requestId TEXT, conversationId TEXT, source TEXT, fileName TEXT)")
    con.executemany("INSERT INTO ai_code_hashes VALUES (?, ?, ?, ?)", [
        ("request-0", session_id, "composer", "src/example.py"),
        ("request-1", session_id, "human", "README.md"),
    ])
    con.execute("CREATE TABLE scored_commits (commitHash TEXT, commitMessage TEXT, linesAdded INTEGER, composerLinesAdded INTEGER, humanLinesAdded INTEGER, scoredAt INTEGER)")
    con.execute("INSERT INTO scored_commits VALUES (?, ?, ?, ?, ?, ?)", ("abc", "Synthetic", 3, 2, 1, 2000))
    con.commit()
    con.close()
    return db_path


def create_legacy_early_state_db(db_path: Path, *, session_id: str = DEFAULT_SESSION_ID) -> Path:
    """Create an early Cursor v3..13-shaped session with legacy turn fields."""
    create_synthetic_state_db(db_path, session_id=session_id, bubble_count=2)
    con = sqlite3.connect(db_path)
    data = json.loads(con.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (f"composerData:{session_id}",)).fetchone()[0])
    data["fullConversationHeadersOnly"] = [{"bubbleId": "bubble-000", "type": 1}, {"bubbleId": "bubble-001", "type": 2}]
    data.pop("headerLog", None)
    con.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(data), f"composerData:{session_id}"))
    for bubble_id, thinking in (("bubble-000", "legacy thought"), ("bubble-001", [{"text": "second thought"}])):
        key = f"bubbleId:{session_id}:{bubble_id}"
        bubble = json.loads(con.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (key,)).fetchone()[0])
        bubble.pop("startedAtMs", None)
        bubble.pop("completedAtMs", None)
        bubble.pop("createdAt", None)
        bubble.pop("thinking", None)
        bubble["allThinkingBlocks"] = thinking
        bubble["tokenCount"] = 7
        con.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(bubble), key))
    con.commit()
    con.close()
    return db_path


def create_legacy_mid_state_db(db_path: Path, *, session_id: str = DEFAULT_SESSION_ID) -> Path:
    """Create a v14..17-shaped session with request context and an orphan bubble."""
    create_synthetic_state_db(db_path, session_id=session_id, bubble_count=2)
    con = sqlite3.connect(db_path)
    key = f"bubbleId:{session_id}:bubble-000"
    bubble = json.loads(con.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (key,)).fetchone()[0])
    bubble["messageRequestContext"] = {"rules": [".cursor/rules/legacy.mdc"], "files": ["src/legacy.py"]}
    con.execute("UPDATE cursorDiskKV SET value = ? WHERE key = ?", (json.dumps(bubble), key))
    orphan = {"type": 2, "text": "orphan", "startedAtMs": 9999, "completedAtMs": 10000}
    con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (f"bubbleId:{session_id}:orphan", json.dumps(orphan)))
    con.commit()
    con.close()
    return db_path


def select_live_smoke_session(adapter: HarnessAdapter, opts: ExtractOptions) -> SessionIR | None:
    """Return one extractable live session, or None when no state database exists."""
    db_path = resolve_state_db(opts.state_db)
    if not db_path.is_file():
        return None
    entries = adapter.list_recent(10, opts)
    if not entries:
        return None
    fallback = None
    for entry in entries:
        ir = adapter.extract_session(entry.session_id, opts)
        if len(ir.turns) >= 1:
            fallback = ir
            if ir.status == "completed" and any(turn.user_bubble_id != "__synthetic_turn_1__" for turn in ir.turns):
                return ir
    if fallback is None:
        raise AssertionError("Listed Cursor sessions did not produce turns.")
    return fallback


def run_stage1_benchmark(bubble_count: int = 20, warmup: int = 1, runs: int = 5) -> float:
    """Benchmark future end-to-end CLI using isolated synthetic databases."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state_db = create_synthetic_state_db(root / "state.vscdb", bubble_count=bubble_count)
        tracking_db = create_synthetic_tracking_db(root / "tracking.db")
        command = [sys.executable, "-m", "chat_report", "--latest", "--state-db", str(state_db),
            "--tracking-db", str(tracking_db), "--out", str(root / "out"), "--agent-digest"]
        for _ in range(warmup):
            subprocess.run(command, check=True, capture_output=True)
        timings: list[float] = []
        for _ in range(runs):
            started = time.perf_counter()
            subprocess.run(command, check=True, capture_output=True)
            timings.append(time.perf_counter() - started)
    return statistics.median(timings)


def run_live_dogfood_gate() -> str:
    """Run the Phase 4 live-host gate once all command entrypoints exist."""
    ir = select_live_smoke_session(CursorAdapter(), ExtractOptions())
    if ir is None:
        return "LIVE_DB_ABSENT_OR_EMPTY"
    with tempfile.TemporaryDirectory() as directory:
        out_dir = Path(directory)
        bundle = out_dir / "bundle"
        subprocess.run([sys.executable, "skills/chat-report/scripts/run.py", "--bundle-into", str(bundle)], check=True, capture_output=True)
        common_args = [ir.session_id, "--out", str(out_dir), "--agent-digest"]
        for entrypoint in (
            [sys.executable, "-m", "chat_report"],
            [sys.executable, "cursor/chat-report.py"],
            [sys.executable, "skills/chat-report/scripts/run.py"],
            [sys.executable, str(bundle / "scripts" / "run.py")],
        ):
            subprocess.run(entrypoint + common_args, check=True, capture_output=True)
            subprocess.run(entrypoint + ["--list-recent", "5"], check=True, capture_output=True)
        subprocess.run([sys.executable, "-m", "chat_report", "--validate", str(out_dir / f"{ir.session_id}.json")], check=True, capture_output=True)
    return f"OK:{ir.session_id}"
