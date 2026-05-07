"""
neo smoke tests — verify core paths without nuking state
"""

import io
import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
SRC_DIR = REPO_DIR / "src"
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(1, str(REPO_DIR))

PASS = 0
FAIL = 0


def check(name, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


@contextmanager
def isolated_db_env(db_module):
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        claude_dir = root / ".claude"
        app_dir = root / ".neo"
        claude_dir.mkdir()
        app_dir.mkdir()

        old_claude_dir = db_module.CLAUDE_DIR
        old_db_path = db_module.DB_PATH
        old_schema_ready = db_module._schema_ready

        db_module.CLAUDE_DIR = claude_dir
        db_module.DB_PATH = app_dir / "neo.db"
        db_module._schema_ready = False
        try:
            yield root, claude_dir, app_dir
        finally:
            db_module.CLAUDE_DIR = old_claude_dir
            db_module.DB_PATH = old_db_path
            db_module._schema_ready = old_schema_ready


def write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def make_project_dir(claude_dir: Path, project_name: str = "demo") -> Path:
    project_dir = claude_dir / "projects" / f"-Users-test-Code-{project_name}"
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir


print("neo smoke tests")
print("=" * 40)

print("\ndb.py")
from neo import db

with isolated_db_env(db) as (_, claude_dir, app_dir):
    conn = db.get_db()
    check("get_db returns connection", conn is not None)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    for table in ["telemetry", "sessions", "agents", "messages", "memory_files", "hook_events", "tasks", "system_reminders"]:
        check(f"table {table} exists", table in tables)
    conn.close()
    check("summary returns dict", isinstance(db.summary(), dict))
    check("query works", isinstance(db.query("SELECT 1 as x"), list))
    summary = db.summary()
    check("summary exposes db freshness fields", "db_mtime" in summary and "latest_hook_timestamp" in summary)

with isolated_db_env(db) as (_, claude_dir, _):
    project_dir = make_project_dir(claude_dir, "parser")
    memory_dir = project_dir / "memory"
    memory_dir.mkdir()
    (memory_dir / "context.md").write_text("keep this", encoding="utf-8")

    transcript_path = project_dir / "session-1" / "subagents" / "agent-a.jsonl"
    write_jsonl(
        transcript_path,
        [
            {"type": "user", "message": {"role": "user", "content": "hello"}},
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "working"},
                        {"type": "tool_use", "name": "Bash", "input": {"command": "echo hi"}},
                    ],
                }
            },
        ],
    )

    conn = db.get_db()
    db.ingest_sessions(conn)
    agent_row = conn.execute("SELECT id, message_count FROM agents").fetchone()
    messages = conn.execute("SELECT role, content FROM messages ORDER BY idx").fetchall()
    first_agent_id = int(agent_row["id"])

    check("nested transcript user role parsed", messages[0]["role"] == "user")
    check("nested transcript assistant role parsed", messages[1]["role"] == "assistant")
    check("tool_use content normalized", "[tool_use] Bash" in messages[1]["content"])
    check("memory file ingested", conn.execute("SELECT COUNT(*) FROM memory_files").fetchone()[0] == 1)

    write_jsonl(
        transcript_path,
        [
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "updated"}],
                }
            }
        ],
    )
    db.ingest_sessions(conn)
    updated_agent_row = conn.execute("SELECT id, message_count FROM agents").fetchone()
    updated_messages = conn.execute("SELECT role, content FROM messages ORDER BY idx").fetchall()

    check("reingest preserves agent identity", int(updated_agent_row["id"]) == first_agent_id)
    check("reingest reloads messages from source", len(updated_messages) == 1 and updated_messages[0]["content"] == "updated")
    conn.close()

with isolated_db_env(db) as (_, claude_dir, _):
    project_dir = make_project_dir(claude_dir, "cleanup")
    (project_dir / "memory").mkdir()
    (project_dir / "memory" / "old.md").write_text("old", encoding="utf-8")
    write_jsonl(
        project_dir / "session-1" / "subagents" / "agent-a.jsonl",
        [{"message": {"role": "assistant", "content": [{"type": "text", "text": "alive"}]}}],
    )

    conn = db.get_db()
    db.ingest_sessions(conn)
    shutil.rmtree(project_dir)
    db.ingest_sessions(conn)

    check("stale sessions removed", conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0)
    check("stale agents removed", conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 0)
    check("stale messages removed", conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0)
    check("stale memory files removed", conn.execute("SELECT COUNT(*) FROM memory_files").fetchone()[0] == 0)
    conn.close()

with isolated_db_env(db) as (_, claude_dir, app_dir):
    hook_log = app_dir / "harness_log.jsonl"
    write_jsonl(
        hook_log,
        [
            {"timestamp": "2026-01-01T00:00:00", "event_type": "start", "data": {"x": 1}},
            {"timestamp": "2026-01-01T00:00:01", "event_type": "stop", "data": {"x": 2}},
        ],
    )
    conn = db.get_db()
    db.ingest_hooks(conn)
    write_jsonl(
        hook_log,
        [
            {"timestamp": "2026-01-01T00:00:02", "event_type": "only", "data": {"x": 3}},
        ],
    )
    db.ingest_hooks(conn)
    rows = conn.execute("SELECT timestamp, event_type FROM hook_events ORDER BY timestamp").fetchall()
    check("hook ingest reloads from source", len(rows) == 1 and rows[0]["event_type"] == "only")

    tasks_dir = claude_dir / "tasks" / "session-1"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "a.json").write_text(json.dumps({"id": "a", "status": "done"}), encoding="utf-8")
    db.ingest_tasks(conn)
    for task_file in tasks_dir.iterdir():
        task_file.unlink()
    (tasks_dir / "b.json").write_text(json.dumps({"id": "b", "status": "open"}), encoding="utf-8")
    db.ingest_tasks(conn)
    task_rows = conn.execute("SELECT task_id, status FROM tasks ORDER BY task_id").fetchall()
    check("task ingest reloads from source", len(task_rows) == 1 and task_rows[0]["task_id"] == "b")

    project_dir = make_project_dir(claude_dir, "reminders")
    transcript_path = project_dir / "session-1" / "subagents" / "agent-a.jsonl"
    write_jsonl(
        transcript_path,
        [
            {
                "type": "user",
                "timestamp": "2026-04-05T01:02:03Z",
                "message": {
                    "role": "user",
                    "content": "<system-reminder>The date has changed. Today's date is now 2026-04-05. DO NOT mention this to the user explicitly because they are already aware.</system-reminder>\n\ncontinue",
                },
            },
            {
                "type": "user",
                "timestamp": "2026-04-05T01:02:04Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "content": "<system-reminder>This memory is 3 days old. Memories are point-in-time observations, not live state - claims about code behavior or file:line citations may be outdated. Verify against current code before asserting as fact.</system-reminder>\n1->file",
                        }
                    ],
                },
            },
            {
                "type": "assistant",
                "timestamp": "2026-04-05T01:02:05Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "Quoted <system-reminder>do not count</system-reminder> in a write-up",
                        }
                    ],
                },
            },
            {
                "type": "user",
                "timestamp": "2026-04-05T01:02:06Z",
                "message": {
                    "role": "user",
                    "content": "system-reminder Make sure that you NEVER mention this reminder to the user",
                },
            },
            {
                "type": "user",
                "timestamp": "2026-04-05T01:02:07Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "<system-reminder>Text-key reminder</system-reminder>",
                        }
                    ],
                },
            },
        ],
    )
    db.ingest_exports(conn)
    reminder_rows = conn.execute("SELECT content, timestamp FROM system_reminders ORDER BY line_number").fetchall()
    check(
        "reminder ingest parses tagged payloads",
        len(reminder_rows) == 4
        and reminder_rows[0]["content"].startswith("The date has changed.")
        and reminder_rows[1]["content"].startswith("This memory is 3 days old.")
        and reminder_rows[2]["content"].startswith("Make sure that you NEVER mention")
        and reminder_rows[3]["content"] == "Text-key reminder",
    )
    check("reminder ingest preserves event timestamp", reminder_rows[0]["timestamp"] == "2026-04-05T01:02:03Z")
    write_jsonl(
        transcript_path,
        [{"message": {"role": "assistant", "content": "ordinary line"}}],
    )
    db.ingest_exports(conn)
    check("reminder ingest reloads from source", conn.execute("SELECT COUNT(*) FROM system_reminders").fetchone()[0] == 0)
    conn.close()

print("\nneo.py")
from neo import app as neo

check(
    "hook command uses installed probe path",
    str(neo.INSTALLED_PROBE_PATH) in neo.build_hook_command(neo.INSTALLED_PROBE_PATH, "notification"),
)
check("escape_like escapes percent", neo.escape_like("100%") == "100\\%")
check("browser launch enabled by default", neo.should_open_browser(["--dashboard"]) is True)
check("browser launch can be disabled", neo.should_open_browser(["--dashboard", "--no-open"]) is False)
check("dashboard html loads", "loadAll();" in neo.load_dashboard_html())
old_template_path = neo.TEMPLATE_PATH
try:
    neo.TEMPLATE_PATH = Path("/tmp/neo-dashboard-missing.html")
    check("dashboard html falls back to packaged resource", "loadAll();" in neo.load_dashboard_html())
finally:
    neo.TEMPLATE_PATH = old_template_path
with tempfile.TemporaryDirectory() as tmpdir:
    root = Path(tmpdir)
    settings_path = root / "settings.json"
    installed_probe_path = root / "harness_probe.py"
    old_settings_path = neo.SETTINGS_PATH
    old_installed_probe_path = neo.INSTALLED_PROBE_PATH
    try:
        neo.SETTINGS_PATH = settings_path
        neo.INSTALLED_PROBE_PATH = installed_probe_path
        status = neo.get_setup_status()
        check("setup status reports missing settings", status["has_settings"] is False)

        installed_probe_path.write_text("probe", encoding="utf-8")
        settings_path.write_text(json.dumps({
            "hooks": {
                "Notification": [{
                    "matcher": ".*",
                    "hooks": [{
                        "type": "command",
                        "command": "/usr/bin/python3 /tmp/harness_probe.py notification"
                    }]
                }]
            }
        }), encoding="utf-8")
        status = neo.get_setup_status()
        check("setup status detects configured hooks", status["hooks_configured"] is True)
        check("setup status counts hook commands", status["hook_command_count"] == 1)

        settings_path.write_text("{not json", encoding="utf-8")
        status = neo.get_setup_status()
        check("setup status reports parse error", bool(status["settings_parse_error"]))

        settings_path.write_text(json.dumps({"hooks": []}), encoding="utf-8")
        status = neo.get_setup_status()
        check(
            "setup status rejects non-object hooks config",
            status["settings_parse_error"] == "settings.json field 'hooks' must be a JSON object",
        )

        old_app_dir = neo.APP_DIR
        try:
            neo.APP_DIR = root / ".neo"
            try:
                neo.setup()
                setup_failed = False
                setup_message = ""
            except SystemExit as exc:
                setup_failed = True
                setup_message = str(exc)
            check("setup rejects non-object hooks config", setup_failed and setup_message == "settings.json field 'hooks' must be a JSON object")
        finally:
            neo.APP_DIR = old_app_dir
    finally:
        neo.SETTINGS_PATH = old_settings_path
        neo.INSTALLED_PROBE_PATH = old_installed_probe_path

with tempfile.TemporaryDirectory() as tmpdir:
    root = Path(tmpdir)
    old_app_dir = neo.APP_DIR
    old_legacy_app_dir = neo.LEGACY_APP_DIR
    try:
        neo.APP_DIR = root / ".neo"
        neo.LEGACY_APP_DIR = root / ".harnesster"
        neo.APP_DIR.mkdir()
        neo.LEGACY_APP_DIR.mkdir()
        (neo.LEGACY_APP_DIR / "harnesster.db").write_text("legacy-db", encoding="utf-8")
        (neo.LEGACY_APP_DIR / "harness_log.jsonl").write_text("legacy-log", encoding="utf-8")

        migrated = neo.migrate_legacy_state()
        check("partial legacy migration reports work performed", migrated is True)
        check("partial legacy migration renames database", (neo.APP_DIR / "neo.db").read_text(encoding="utf-8") == "legacy-db")
        check("partial legacy migration moves log file", (neo.APP_DIR / "harness_log.jsonl").read_text(encoding="utf-8") == "legacy-log")
        check("partial legacy migration cleans up legacy dir", not neo.LEGACY_APP_DIR.exists())
    finally:
        neo.APP_DIR = old_app_dir
        neo.LEGACY_APP_DIR = old_legacy_app_dir

handler = object.__new__(neo.Handler)
handler.server = type("Server", (), {"allowed_hosts": {"127.0.0.1:7777"}})()
handler.headers = {"Host": "127.0.0.1:7777"}
try:
    handler.enforce_allowed_host()
    host_allowed = True
except PermissionError:
    host_allowed = False
check("handler accepts localhost host header", host_allowed)

handler.headers = {"Host": "evil.example"}
try:
    handler.enforce_allowed_host()
    host_blocked = False
except PermissionError:
    host_blocked = True
check("handler rejects unexpected host header", host_blocked)

print("\ntokens.py")
from neo import tokens

with tempfile.TemporaryDirectory() as tmpdir:
    transcript = Path(tmpdir) / "session.jsonl"
    write_jsonl(
        transcript,
        [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "content": "<system-reminder>First reminder</system-reminder>\nctx"},
                        {"type": "text", "text": "system-reminder second reminder"},
                    ],
                },
            }
        ],
    )
    stats = tokens.analyze_session_file(transcript)
    check("token accounting parses reminder payloads", stats["system_reminders"] == 2)

print("\nharness_probe.py")
from neo import harness_probe

with tempfile.TemporaryDirectory() as tmpdir:
    root = Path(tmpdir)
    old_log_dir = harness_probe.LOG_DIR
    old_log_file = harness_probe.LOG_FILE
    old_db_file = harness_probe.DB_FILE
    harness_probe.LOG_DIR = root / ".neo"
    harness_probe.LOG_FILE = harness_probe.LOG_DIR / "harness_log.jsonl"
    harness_probe.DB_FILE = harness_probe.LOG_DIR / "neo.db"
    old_stdin = sys.stdin
    try:
        sys.stdin = io.StringIO("not json at all{{{")
        harness_probe.log_event("test_corrupt")
        check("corrupt JSON doesn't crash probe", harness_probe.LOG_FILE.exists())
    finally:
        sys.stdin = old_stdin
        harness_probe.LOG_DIR = old_log_dir
        harness_probe.LOG_FILE = old_log_file
        harness_probe.DB_FILE = old_db_file

print("\ndashboard.html")
dash_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
with open(dash_path, encoding="utf-8") as fh:
    content = fh.read()
check("dashboard exists", os.path.exists(dash_path))
check("dashboard checks fetch status", "if (!r.ok)" in content)
check("dashboard uses no-store fetches", "cache: 'no-store'" in content)
check("dashboard exposes ingest action", "ingest now" in content)
check("dashboard includes measured source notes", "sourceNote('measured'" in content)
check("dashboard uses hook totals when present", "var hookTotals = corr.hook_totals || [];" in content)
check("dashboard renders reminder cards", "renderReminder(" in content and "reminder-card" in content)
check("dashboard uses red measured accents", ".measure{border-color:#f85149}" in content)
check("dashboard moves device into header line", "device ' + sum.device" in content and "latest session activity" in content)
check("dashboard persists panel preferences", "localStorage.getItem('neo.panels')" in content and "panelClass(" in content)
check("dashboard organizes sessions panel", "active in last 24h" in content and "<span class=\"pill\">recent</span>Most recent sessions" in content)
check("dashboard organizes agents panel", "recent subagent logs" in content and "<span class=\"pill\">projects</span>Recent agent activity" in content)
check("dashboard organizes memory panel", "memory index files" in content and "<span class=\"pill\">files</span>Click any file" in content)
check("dashboard organizes probe panel", "event types in shown window" in content and "<span class=\"pill\">types</span>Event type counts in the current probe window." in content)
check("dashboard organizes telemetry panel", "retained event types" in content and "<span class=\"pill\">recent</span>Latest retained telemetry rows" in content)
check("dashboard synthesizes state model", "Dominant mode:" in content and "<span class=\"pill\">hidden</span>Hidden mechanisms" in content)
check("dashboard synthesizes correlations", "cross-signal digest" in content and "execution telemetry share" in content and "<span class=\"pill\">hooks</span>Hook event mix used in the cross-signal rollup." in content)
check("dashboard keeps tasks compact", "Local task rows when Claude wrote task JSON files." in content and "task rows" in content)
check("packaging metadata exists", os.path.exists(os.path.join(os.path.dirname(__file__), "pyproject.toml")))
check("packaged dashboard resource exists", os.path.exists(os.path.join(os.path.dirname(__file__), "src", "neo", "dashboard.html")))

print(f"\n{'=' * 40}")
print(f"passed: {PASS}  failed: {FAIL}")
if FAIL > 0:
    sys.exit(1)
