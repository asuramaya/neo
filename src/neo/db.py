"""
neo data layer — one database, all sources

Production notes:
- preserves foreign-key identities during upserts
- treats source folders/logs as the source of truth
- avoids symlink traversal
- keeps DB artifacts private on disk
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

CLAUDE_DIR = Path.home() / ".claude"
DB_PATH = Path.home() / ".neo" / "neo.db"
MAX_MEMORY_FILE_BYTES = 5000
MAX_REMINDER_CONTENT_BYTES = 4000
MAX_MESSAGE_CONTENT_BYTES = 3000

SYSTEM_REMINDER_TAG_RE = re.compile(r"<system-reminder>(.*?)</system-reminder>", re.IGNORECASE | re.DOTALL)
SYSTEM_REMINDER_PREFIX_RE = re.compile(r"^\s*system-reminder\b[:\s-]*(.+?)\s*$", re.IGNORECASE | re.DOTALL)

_schema_lock = threading.Lock()
_schema_ready = False
_ingest_lock = threading.Lock()


def _db_path() -> Path:
    return Path(str(DB_PATH))


def _db_dir() -> Path:
    return _db_path().parent


def _hook_log_path() -> Path:
    return _db_dir() / "harness_log.jsonl"


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _ensure_private_file(path: Path, mode: int = 0o600) -> None:
    try:
        os.chmod(path, mode)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _harden_database_files() -> None:
    base = _db_path()
    for suffix in ("", "-wal", "-shm"):
        _ensure_private_file(Path(str(base) + suffix))


def _commit_and_harden(conn: sqlite3.Connection) -> None:
    conn.commit()
    _harden_database_files()


def _is_safe_regular_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _iter_safe_entries(directory: Path) -> Iterator[Path]:
    try:
        entries = []
        for entry in directory.iterdir():
            try:
                if entry.is_symlink():
                    continue
            except OSError:
                continue
            entries.append(entry)
        for entry in sorted(entries, key=lambda p: p.name):
            yield entry
    except OSError:
        return


def _walk_safe_files(base: Path, suffix: str) -> Iterator[Path]:
    for root, dirnames, filenames in os.walk(base, followlinks=False):
        safe_dirnames = []
        for dirname in sorted(dirnames):
            child = Path(root) / dirname
            try:
                if child.is_symlink():
                    continue
            except OSError:
                continue
            safe_dirnames.append(dirname)
        dirnames[:] = safe_dirnames

        for filename in sorted(filenames):
            if suffix and not filename.endswith(suffix):
                continue
            path = Path(root) / filename
            if _is_safe_regular_file(path):
                yield path


def _read_text_prefix(path: Path, limit: int) -> str:
    with open(path, encoding="utf-8", errors="ignore") as fh:
        return fh.read(limit)


def _row_delta(cursor: sqlite3.Cursor) -> int:
    return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0


def _normalize_content(content) -> str:
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if "text" in item:
                    parts.append(str(item["text"]))
                    continue
                if item.get("type") == "tool_use":
                    name = item.get("name", "tool_use")
                    input_data = item.get("input")
                    if input_data is None:
                        parts.append(f"[tool_use] {name}")
                    else:
                        try:
                            rendered = json.dumps(input_data, sort_keys=True)
                        except TypeError:
                            rendered = str(input_data)
                        parts.append(f"[tool_use] {name} {rendered}")
                    continue
                try:
                    parts.append(json.dumps(item, sort_keys=True))
                except TypeError:
                    parts.append(str(item))
            else:
                parts.append(str(item))
        return " ".join(parts)
    if isinstance(content, dict):
        try:
            return json.dumps(content, sort_keys=True)
        except TypeError:
            return str(content)
    return str(content)


def _iter_message_text_chunks(content) -> Iterator[str]:
    if isinstance(content, str):
        yield content
        return
    if not isinstance(content, list):
        return
    for item in content:
        if not isinstance(item, dict):
            continue
        for key in ("text", "content"):
            text = item.get(key)
            if isinstance(text, str):
                yield text


def _read_first_jsonl_entry(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    return json.loads(line)
    except Exception:
        pass
    return {}


def _is_compaction_file(path: Path) -> bool:
    entry = _read_first_jsonl_entry(path)
    return entry.get("isCompaction") is True or "compact" in path.name


def _extract_system_reminder_text(text: str) -> str | None:
    stripped = text.lstrip()
    if stripped.lower().startswith("<system-reminder>"):
        match = SYSTEM_REMINDER_TAG_RE.search(stripped)
        if not match:
            return None
        reminder = match.group(1)
    elif stripped.lower().startswith("system-reminder"):
        match = SYSTEM_REMINDER_PREFIX_RE.match(stripped)
        if not match:
            return None
        reminder = match.group(1)
    else:
        return None

    reminder = reminder.replace("\r\n", "\n").replace("\r", "\n").strip()
    return reminder[:MAX_REMINDER_CONTENT_BYTES] if reminder else None


def _extract_system_reminder_entry(entry: dict) -> tuple[str, str] | None:
    if entry.get("type") != "user":
        return None

    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return None

    for chunk in _iter_message_text_chunks(message.get("content")):
        reminder = _extract_system_reminder_text(chunk)
        if reminder:
            return reminder, str(entry.get("timestamp", "") or "")

    return None


def get_db() -> sqlite3.Connection:
    global _schema_ready
    _ensure_private_dir(_db_dir())
    conn = sqlite3.connect(str(_db_path()), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    with _schema_lock:
        if not _schema_ready:
            _init_schema(conn)
            _schema_ready = True
    _harden_database_files()
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS telemetry (
            id INTEGER PRIMARY KEY,
            time TEXT,
            event TEXT,
            session_id TEXT,
            parent_session_id TEXT,
            device_id TEXT,
            version TEXT,
            model TEXT,
            platform TEXT,
            arch TEXT,
            process_json TEXT,
            raw_json TEXT,
            source_file TEXT,
            UNIQUE(time, event, session_id)
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY,
            project TEXT,
            session_id TEXT,
            mtime TEXT,
            agent_count INTEGER DEFAULT 0,
            compaction_count INTEGER DEFAULT 0,
            UNIQUE(project, session_id)
        );

        CREATE TABLE IF NOT EXISTS agents (
            id INTEGER PRIMARY KEY,
            project TEXT,
            session_id TEXT,
            file_name TEXT,
            is_compaction INTEGER DEFAULT 0,
            mtime TEXT,
            size_bytes INTEGER,
            message_count INTEGER DEFAULT 0,
            UNIQUE(project, session_id, file_name)
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY,
            agent_id INTEGER REFERENCES agents(id) ON DELETE CASCADE,
            idx INTEGER,
            role TEXT,
            content TEXT
        );

        CREATE TABLE IF NOT EXISTS memory_files (
            id INTEGER PRIMARY KEY,
            project TEXT,
            file_name TEXT,
            content TEXT,
            UNIQUE(project, file_name)
        );

        CREATE TABLE IF NOT EXISTS hook_events (
            id INTEGER PRIMARY KEY,
            timestamp TEXT,
            event_type TEXT,
            data_json TEXT,
            source TEXT
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            task_id TEXT,
            subject TEXT,
            description TEXT,
            status TEXT,
            raw_json TEXT,
            UNIQUE(session_id, task_id)
        );

        CREATE TABLE IF NOT EXISTS system_reminders (
            id INTEGER PRIMARY KEY,
            source_file TEXT,
            line_number INTEGER,
            content TEXT,
            timestamp TEXT,
            UNIQUE(source_file, line_number)
        );

        CREATE INDEX IF NOT EXISTS idx_tel_session ON telemetry(session_id);
        CREATE INDEX IF NOT EXISTS idx_tel_time ON telemetry(time);
        CREATE INDEX IF NOT EXISTS idx_agents_project ON agents(project);
        CREATE INDEX IF NOT EXISTS idx_agents_session ON agents(project, session_id);
        CREATE INDEX IF NOT EXISTS idx_messages_agent ON messages(agent_id);
        CREATE INDEX IF NOT EXISTS idx_hooks_time ON hook_events(timestamp);
        CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id);
        CREATE INDEX IF NOT EXISTS idx_reminders_source ON system_reminders(source_file, line_number);
        """
    )
    try:
        conn.execute("ALTER TABLE hook_events ADD COLUMN source TEXT")
    except sqlite3.OperationalError:
        pass
    _commit_and_harden(conn)


def _project_name(path: Path) -> str:
    name = path.name
    for prefix in ["-Users-", "-home-"]:
        if prefix in name:
            parts = name.split("-")
            try:
                idx = [i for i, part in enumerate(parts) if part.lower() == "code"]
                if idx:
                    return "-".join(parts[idx[-1] + 1 :])
            except Exception:
                pass
    return name


def ingest_telemetry(conn: sqlite3.Connection) -> int:
    tel_dir = CLAUDE_DIR / "telemetry"
    if not tel_dir.exists():
        return 0

    count = 0
    for path in sorted(tel_dir.glob("*.json"), key=lambda p: p.name):
        if not _is_safe_regular_file(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    event_data = event.get("event_data", {})
                    cursor = conn.execute(
                        """
                        INSERT OR IGNORE INTO telemetry
                        (time, event, session_id, parent_session_id, device_id, version, model, platform, arch, process_json, raw_json, source_file)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_data.get("client_timestamp", ""),
                            event_data.get("event_name", ""),
                            event_data.get("session_id", ""),
                            event_data.get("parent_session_id", ""),
                            event_data.get("device_id", ""),
                            event_data.get("env", {}).get("version", ""),
                            event_data.get("model", ""),
                            event_data.get("env", {}).get("platform", ""),
                            event_data.get("env", {}).get("arch", ""),
                            event_data.get("process", ""),
                            line,
                            path.name,
                        ),
                    )
                    count += _row_delta(cursor)
        except OSError:
            pass

    _commit_and_harden(conn)
    return count


def _upsert_session(conn: sqlite3.Connection, project: str, session_id: str, mtime: str, agent_count: int, compaction_count: int) -> None:
    conn.execute(
        """
        INSERT INTO sessions (project, session_id, mtime, agent_count, compaction_count)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(project, session_id)
        DO UPDATE SET
            mtime = excluded.mtime,
            agent_count = excluded.agent_count,
            compaction_count = excluded.compaction_count
        """,
        (project, session_id, mtime, agent_count, compaction_count),
    )


def _upsert_agent(conn: sqlite3.Connection, project: str, session_id: str, file_name: str, is_compaction: int, mtime: str, size_bytes: int) -> int:
    conn.execute(
        """
        INSERT INTO agents (project, session_id, file_name, is_compaction, mtime, size_bytes)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(project, session_id, file_name)
        DO UPDATE SET
            is_compaction = excluded.is_compaction,
            mtime = excluded.mtime,
            size_bytes = excluded.size_bytes
        """,
        (project, session_id, file_name, is_compaction, mtime, size_bytes),
    )
    row = conn.execute(
        "SELECT id FROM agents WHERE project=? AND session_id=? AND file_name=?",
        (project, session_id, file_name),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"failed to resolve agent id for {project}/{session_id}/{file_name}")
    return int(row[0])


def _reload_agent_messages(conn: sqlite3.Connection, agent_id: int, transcript_path: Path) -> int:
    parsed_messages = []
    try:
        with open(transcript_path, encoding="utf-8", errors="ignore") as fh:
            for idx, raw_line in enumerate(fh):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    outer = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = outer.get("message") if isinstance(outer.get("message"), dict) else outer
                role = payload.get("role") or payload.get("type") or outer.get("type") or "?"
                content = _normalize_content(payload.get("content", ""))[:MAX_MESSAGE_CONTENT_BYTES]
                parsed_messages.append((idx, str(role), content))
    except OSError:
        return -1

    conn.execute("DELETE FROM messages WHERE agent_id=?", (agent_id,))
    for idx, role, content in parsed_messages:
        conn.execute(
            "INSERT INTO messages (agent_id, idx, role, content) VALUES (?, ?, ?, ?)",
            (agent_id, idx, role, content),
        )
    conn.execute("UPDATE agents SET message_count=? WHERE id=?", (len(parsed_messages), agent_id))
    return len(parsed_messages)


def _delete_stale_agents(conn: sqlite3.Connection, project: str, session_id: str, keep_file_names: set[str]) -> None:
    rows = conn.execute(
        "SELECT id, file_name FROM agents WHERE project=? AND session_id=?",
        (project, session_id),
    ).fetchall()
    for row in rows:
        agent_id = int(row[0])
        file_name = row[1]
        if file_name in keep_file_names:
            continue
        conn.execute("DELETE FROM messages WHERE agent_id=?", (agent_id,))
        conn.execute("DELETE FROM agents WHERE id=?", (agent_id,))


def _delete_stale_sessions(conn: sqlite3.Connection, project: str, keep_session_ids: set[str]) -> None:
    rows = conn.execute("SELECT session_id FROM sessions WHERE project=?", (project,)).fetchall()
    for row in rows:
        session_id = row[0]
        if session_id in keep_session_ids:
            continue
        stale_agent_rows = conn.execute(
            "SELECT id FROM agents WHERE project=? AND session_id=?",
            (project, session_id),
        ).fetchall()
        for agent_row in stale_agent_rows:
            agent_id = int(agent_row[0])
            conn.execute("DELETE FROM messages WHERE agent_id=?", (agent_id,))
        conn.execute("DELETE FROM agents WHERE project=? AND session_id=?", (project, session_id))
        conn.execute("DELETE FROM sessions WHERE project=? AND session_id=?", (project, session_id))


def _delete_stale_memory_files(conn: sqlite3.Connection, project: str, keep_file_names: set[str]) -> None:
    rows = conn.execute("SELECT file_name FROM memory_files WHERE project=?", (project,)).fetchall()
    for row in rows:
        file_name = row[0]
        if file_name not in keep_file_names:
            conn.execute("DELETE FROM memory_files WHERE project=? AND file_name=?", (project, file_name))


def ingest_sessions(conn: sqlite3.Connection) -> int:
    proj_dir = CLAUDE_DIR / "projects"
    if not proj_dir.exists():
        return 0

    scanned_sessions = 0
    seen_projects: set[str] = set()

    for project in _iter_safe_entries(proj_dir):
        if not project.is_dir():
            continue

        project_name = _project_name(project)
        seen_projects.add(project_name)
        seen_session_ids: set[str] = set()
        seen_memory_files: set[str] = set()

        for session in _iter_safe_entries(project):
            if not session.is_dir():
                continue

            if session.name == "memory":
                for memory_file in _iter_safe_entries(session):
                    if not _is_safe_regular_file(memory_file):
                        continue
                    try:
                        content = _read_text_prefix(memory_file, MAX_MEMORY_FILE_BYTES)
                    except OSError:
                        continue
                    seen_memory_files.add(memory_file.name)
                    conn.execute(
                        """
                        INSERT INTO memory_files (project, file_name, content)
                        VALUES (?, ?, ?)
                        ON CONFLICT(project, file_name)
                        DO UPDATE SET content = excluded.content
                        """,
                        (project_name, memory_file.name, content),
                    )
                continue

            session_id = session.name
            seen_session_ids.add(session_id)
            sa_dir = session / "subagents"
            agent_files = []
            if sa_dir.exists() and sa_dir.is_dir():
                agent_files = [
                    path for path in _iter_safe_entries(sa_dir)
                    if _is_safe_regular_file(path) and path.suffix == ".jsonl"
                ]
            compacts = [path for path in agent_files if _is_compaction_file(path)]
            regulars = [path for path in agent_files if not _is_compaction_file(path)]

            try:
                session_mtime = datetime.fromtimestamp(session.stat().st_mtime).isoformat()
            except OSError:
                session_mtime = ""

            _upsert_session(
                conn,
                project_name,
                session_id,
                session_mtime,
                len(regulars),
                len(compacts),
            )

            keep_agent_file_names: set[str] = set()
            for transcript in agent_files:
                keep_agent_file_names.add(transcript.name)
                try:
                    mtime = datetime.fromtimestamp(transcript.stat().st_mtime).isoformat()
                    size_bytes = transcript.stat().st_size
                except OSError:
                    continue

                is_compact = 1 if _is_compaction_file(transcript) else 0
                agent_id = _upsert_agent(
                    conn,
                    project_name,
                    session_id,
                    transcript.name,
                    is_compact,
                    mtime,
                    size_bytes,
                )
                message_count = _reload_agent_messages(conn, agent_id, transcript)
                if message_count >= 0:
                    conn.execute("UPDATE agents SET message_count=? WHERE id=?", (message_count, agent_id))

            _delete_stale_agents(conn, project_name, session_id, keep_agent_file_names)
            scanned_sessions += 1

        _delete_stale_sessions(conn, project_name, seen_session_ids)
        _delete_stale_memory_files(conn, project_name, seen_memory_files)

    existing_projects = [row[0] for row in conn.execute("SELECT DISTINCT project FROM sessions").fetchall()]
    for project_name in existing_projects:
        if project_name in seen_projects:
            continue
        stale_agent_rows = conn.execute("SELECT id FROM agents WHERE project=?", (project_name,)).fetchall()
        for agent_row in stale_agent_rows:
            conn.execute("DELETE FROM messages WHERE agent_id=?", (int(agent_row[0]),))
        conn.execute("DELETE FROM agents WHERE project=?", (project_name,))
        conn.execute("DELETE FROM sessions WHERE project=?", (project_name,))
        conn.execute("DELETE FROM memory_files WHERE project=?", (project_name,))

    _commit_and_harden(conn)
    return scanned_sessions


def ingest_hooks(conn: sqlite3.Connection) -> int:
    log_file = _hook_log_path()
    conn.execute("DELETE FROM hook_events")

    if not log_file.exists() or not _is_safe_regular_file(log_file):
        _commit_and_harden(conn)
        return 0

    count = 0
    try:
        with open(log_file, encoding="utf-8", errors="ignore") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                conn.execute(
                    "INSERT INTO hook_events (timestamp, event_type, data_json, source) VALUES (?, ?, ?, ?)",
                    (
                        event.get("timestamp", ""),
                        event.get("event_type", ""),
                        json.dumps(event.get("data", {})),
                        event.get("source"),
                    ),
                )
                count += 1
    except OSError:
        pass

    _commit_and_harden(conn)
    return count


def ingest_tasks(conn: sqlite3.Connection) -> int:
    tasks_dir = CLAUDE_DIR / "tasks"
    conn.execute("DELETE FROM tasks")

    if not tasks_dir.exists():
        _commit_and_harden(conn)
        return 0

    count = 0
    for session_dir in _iter_safe_entries(tasks_dir):
        if not session_dir.is_dir():
            continue
        session_id = session_dir.name
        for task_file in _iter_safe_entries(session_dir):
            if not _is_safe_regular_file(task_file) or task_file.suffix != ".json":
                continue
            try:
                with open(task_file, encoding="utf-8", errors="ignore") as fh:
                    data = json.loads(fh.read())
            except (OSError, json.JSONDecodeError):
                continue
            conn.execute(
                "INSERT INTO tasks (session_id, task_id, subject, description, status, raw_json) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    data.get("id", task_file.stem),
                    data.get("subject", ""),
                    data.get("description", ""),
                    data.get("status", ""),
                    json.dumps(data),
                ),
            )
            count += 1

    _commit_and_harden(conn)
    return count


def ingest_exports(conn: sqlite3.Connection) -> int:
    """Parse raw JSONL transcripts for live system-reminder injections."""
    proj_dir = CLAUDE_DIR / "projects"
    conn.execute("DELETE FROM system_reminders")

    if not proj_dir.exists():
        _commit_and_harden(conn)
        return 0

    count = 0
    for path in _walk_safe_files(proj_dir, ".jsonl"):
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for line_number, raw_line in enumerate(fh, start=1):
                    if "system-reminder" not in raw_line:
                        continue
                    try:
                        entry = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    extracted = _extract_system_reminder_entry(entry)
                    if not extracted:
                        continue
                    reminder_text, reminder_timestamp = extracted
                    conn.execute(
                        "INSERT INTO system_reminders (source_file, line_number, content, timestamp) VALUES (?, ?, ?, ?)",
                        (str(path), line_number, reminder_text, reminder_timestamp),
                    )
                    count += 1
        except OSError:
            pass

    _commit_and_harden(conn)
    return count


def ingest_all() -> dict[str, int]:
    with _ingest_lock:
        conn = get_db()
        try:
            telemetry_count = ingest_telemetry(conn)
            session_count = ingest_sessions(conn)
            hook_count = ingest_hooks(conn)
            task_count = ingest_tasks(conn)
            reminder_count = ingest_exports(conn)
            return {
                "telemetry": telemetry_count,
                "sessions": session_count,
                "hooks": hook_count,
                "tasks": task_count,
                "reminders": reminder_count,
            }
        finally:
            conn.close()


def query(sql: str, params: Iterable = ()) -> list[dict]:
    conn = get_db()
    try:
        return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]
    finally:
        conn.close()


def summary() -> dict:
    conn = get_db()
    try:
        device_row = conn.execute(
            "SELECT MIN(device_id) FROM telemetry WHERE device_id IS NOT NULL AND device_id != ''"
        ).fetchone()
        db_mtime = None
        try:
            db_mtime = datetime.fromtimestamp(_db_path().stat().st_mtime).isoformat()
        except OSError:
            db_mtime = None
        return {
            "telemetry": conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0],
            "sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            "agents": conn.execute("SELECT COUNT(*) FROM agents WHERE is_compaction=0").fetchone()[0],
            "compactions": conn.execute("SELECT COUNT(*) FROM hook_events WHERE event_type='post_compact'").fetchone()[0],
            "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "memory_files": conn.execute("SELECT COUNT(*) FROM memory_files").fetchone()[0],
            "hook_events": conn.execute("SELECT COUNT(*) FROM hook_events").fetchone()[0],
            "system_reminders": conn.execute("SELECT COUNT(*) FROM system_reminders").fetchone()[0],
            "device": device_row[0] if device_row else None,
            "db_mtime": db_mtime,
            "latest_hook_timestamp": conn.execute("SELECT MAX(timestamp) FROM hook_events").fetchone()[0],
            "latest_telemetry_time": conn.execute("SELECT MAX(time) FROM telemetry").fetchone()[0],
            "latest_session_mtime": conn.execute("SELECT MAX(mtime) FROM sessions").fetchone()[0],
        }
    finally:
        conn.close()


if __name__ == "__main__":
    print("ingesting ~/.claude/ data...")
    result = ingest_all()
    print(f"  telemetry: {result['telemetry']}")
    print(f"  sessions:  {result['sessions']}")
    print(f"  hooks:     {result['hooks']}")
    print(f"  tasks:     {result['tasks']}")
    print(f"  reminders: {result['reminders']}")
    stats = summary()
    print(f"\ndb: {_db_path()}")
    print(
        f"  {stats['telemetry']} tel | {stats['sessions']} ses | {stats['agents']} agents | "
        f"{stats['compactions']} compact | {stats['messages']} msgs | {stats['system_reminders']} reminders"
    )
