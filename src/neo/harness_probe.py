"""
harness_probe.py — capture Claude Code events to database and log

Hook script for settings.json. Writes to both JSONL (for tail -f)
and SQLite (for dashboard).
"""

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

LOG_DIR = Path.home() / ".neo"
LOG_FILE = LOG_DIR / "harness_log.jsonl"
DB_FILE = LOG_DIR / "neo.db"
MAX_STDIN_BYTES = 1_000_000
MAX_EVENT_TYPE_LENGTH = 64
MAX_STORED_DATA_CHARS = 200_000


def _ensure_private_dir() -> None:
    LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(LOG_DIR, 0o700)
    except OSError:
        pass


def _ensure_private_file(path: Path, mode: int = 0o600) -> None:
    try:
        os.chmod(path, mode)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _harden_db_files() -> None:
    for suffix in ("", "-wal", "-shm"):
        _ensure_private_file(Path(str(DB_FILE) + suffix))


def _read_stdin() -> str:
    data = sys.stdin.read(MAX_STDIN_BYTES + 1)
    if len(data) > MAX_STDIN_BYTES:
        return data[:MAX_STDIN_BYTES]
    return data


def _normalize_event_type(event_type: str) -> str:
    event_type = (event_type or "unknown").strip() or "unknown"
    return event_type[:MAX_EVENT_TYPE_LENGTH]


def _normalize_data(data):
    serialized = json.dumps(data, default=str, ensure_ascii=False)
    if len(serialized) <= MAX_STORED_DATA_CHARS:
        return data, serialized
    truncated = {
        "truncated": True,
        "preview": serialized[:MAX_STORED_DATA_CHARS],
        "original_length": len(serialized),
    }
    return truncated, json.dumps(truncated, ensure_ascii=False)


def _detect_source(data) -> str | None:
    """Return 'neo_mcp_call' if the event is self-traffic from neo's MCP server.

    Claude Code namespaces MCP tools as ``mcp__<server>__<tool>`` — when the
    user (or the model) queries neo through its MCP server, the resulting
    PreToolUse / PostToolUse hooks carry a ``tool_name`` with that prefix.
    Tagging here keeps downstream reads filterable without dropping the row,
    so observer overhead can still be audited explicitly.
    """
    if not isinstance(data, dict):
        return None
    tool_name = data.get("tool_name") or ""
    if isinstance(tool_name, str) and tool_name.startswith("mcp__neo__"):
        return "neo_mcp_call"
    return None


def _append_jsonl(entry) -> None:
    fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    finally:
        _ensure_private_file(LOG_FILE)


def _write_db(timestamp: str, event_type: str, data_json: str, source: str | None) -> None:
    conn = sqlite3.connect(str(DB_FILE), timeout=5)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hook_events (
                id INTEGER PRIMARY KEY,
                timestamp TEXT,
                event_type TEXT,
                data_json TEXT,
                source TEXT
            )
            """
        )
        try:
            conn.execute("ALTER TABLE hook_events ADD COLUMN source TEXT")
        except sqlite3.OperationalError:
            pass
        conn.execute(
            "INSERT INTO hook_events (timestamp, event_type, data_json, source) VALUES (?,?,?,?)",
            (timestamp, event_type, data_json, source),
        )
        conn.commit()
    finally:
        conn.close()
        _harden_db_files()


def log_event(event_type):
    _ensure_private_dir()
    event_type = _normalize_event_type(event_type)

    try:
        raw = _read_stdin()
        if not raw.strip():
            return

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"raw": raw}

        source = _detect_source(data)
        data, data_json = _normalize_data(data)
        timestamp = datetime.now().isoformat()
        entry = {"timestamp": timestamp, "event_type": event_type, "data": data}
        if source:
            entry["source"] = source

        _append_jsonl(entry)
        try:
            _write_db(timestamp, event_type, data_json, source)
        except Exception:
            pass

    except Exception as exc:
        _append_jsonl({
            "timestamp": datetime.now().isoformat(),
            "event_type": "error",
            "error": str(exc),
        })


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: harness_probe.py <event_type>")
        sys.exit(1)
    log_event(sys.argv[1])
