#!/usr/bin/env python3
"""
neo — see what Claude Code hides from you

  python3 neo.py              # ingest + setup + dashboard
  python3 neo.py --setup      # install hooks + register MCP server
  python3 neo.py --ingest     # ingest data only
  python3 neo.py --dashboard  # dashboard only
  python3 neo.py --mcp        # run as stdio MCP server (Claude Code spawns this)
  python3 neo.py --port 8888  # custom port
  python3 neo.py --dashboard --no-open  # don't auto-launch browser
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
import traceback
import webbrowser
import http.server
import importlib.resources
from http import HTTPStatus
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

SCRIPT_DIR = Path(__file__).parent.resolve()
from . import db
from . import states
from . import tokens

SOURCE_PROBE_PATH = SCRIPT_DIR / "harness_probe.py"
APP_DIR = Path.home() / ".neo"
INSTALLED_PROBE_PATH = APP_DIR / "harness_probe.py"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
CLAUDE_CONFIG_PATH = Path.home() / ".claude.json"
TEMPLATE_PATH = SCRIPT_DIR / "dashboard.html"
RESOURCE_TEMPLATE_PACKAGE = "neo"
RESOURCE_TEMPLATE_NAME = "dashboard.html"
DEFAULT_PORT = 7777
LEGACY_APP_DIR = Path.home() / ".harnesster"
LEGACY_DB_NAME = "harnesster.db"
MCP_SERVER_NAME = "neo"
MAX_LIMIT = 500
MAX_OFFSET = 100000
MAX_ANALYZE_EVENTS = 5000
MAX_SEARCH_TERM_LENGTH = 200
SEARCH_RESULTS_PER_TABLE = 50
DEFAULT_CORRELATION_TIMELINE_LIMIT = 720
NO_OPEN_FLAG = "--no-open"


class BadRequest(Exception):
    """Raised when a request parameter or request shape is invalid."""


def ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def ensure_private_file(path: Path, mode: int = 0o600) -> None:
    try:
        os.chmod(path, mode)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def migrate_legacy_state() -> bool:
    """Move ~/.harnesster -> ~/.neo and rename harnesster.db -> neo.db.

    Also handles partial upgrades where ~/.neo already exists but legacy
    files are still present in ~/.harnesster. Returns True if any
    migration step was performed.
    """
    if not LEGACY_APP_DIR.exists():
        return False

    performed = False
    new_db = APP_DIR / "neo.db"

    if not APP_DIR.exists():
        try:
            shutil.move(str(LEGACY_APP_DIR), str(APP_DIR))
            performed = True
        except OSError as exc:
            print(f"[neo] could not migrate {LEGACY_APP_DIR} -> {APP_DIR}: {exc}",
                  file=sys.stderr)
            return False
    else:
        ensure_private_dir(APP_DIR)
        for legacy_item in sorted(LEGACY_APP_DIR.iterdir(), key=lambda p: p.name):
            target_name = "neo.db" if legacy_item.name == LEGACY_DB_NAME else legacy_item.name
            target_path = APP_DIR / target_name
            if target_path.exists():
                continue
            try:
                shutil.move(str(legacy_item), str(target_path))
                performed = True
            except OSError as exc:
                print(f"[neo] could not migrate {legacy_item} -> {target_path}: {exc}",
                      file=sys.stderr)
        try:
            LEGACY_APP_DIR.rmdir()
        except OSError:
            pass

    legacy_db = APP_DIR / LEGACY_DB_NAME
    if legacy_db.exists() and not new_db.exists():
        try:
            legacy_db.rename(new_db)
            performed = True
        except OSError as exc:
            print(f"[neo] could not rename db {legacy_db} -> {new_db}: {exc}",
                  file=sys.stderr)

    if performed:
        print(f"[neo] migrated state: {LEGACY_APP_DIR} -> {APP_DIR}",
              file=sys.stderr)
    return performed


def sync_installed_probe() -> Path:
    if not SOURCE_PROBE_PATH.is_file():
        raise FileNotFoundError(f"probe source not found: {SOURCE_PROBE_PATH}")

    ensure_private_dir(APP_DIR)

    copy_required = True
    if INSTALLED_PROBE_PATH.exists():
        try:
            copy_required = SOURCE_PROBE_PATH.read_bytes() != INSTALLED_PROBE_PATH.read_bytes()
        except OSError:
            copy_required = True

    if copy_required:
        shutil.copy2(SOURCE_PROBE_PATH, INSTALLED_PROBE_PATH)

    ensure_private_file(INSTALLED_PROBE_PATH)
    return INSTALLED_PROBE_PATH


def build_hook_command(probe_path: Path, event_name: str) -> str:
    python_exe = shlex.quote(sys.executable)
    quoted_probe = shlex.quote(str(probe_path))
    quoted_event = shlex.quote(event_name)
    return f"{python_exe} {quoted_probe} {quoted_event}"


def setup() -> None:
    print("neo setup")
    print("=" * 40)
    migrate_legacy_state()
    ensure_private_dir(APP_DIR)
    installed_probe = sync_installed_probe()

    if not SETTINGS_PATH.exists():
        print("ERROR: settings.json not found")
        sys.exit(1)

    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            settings = json.load(f)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"settings.json is not valid JSON: {exc}") from exc

    if not isinstance(settings, dict):
        raise SystemExit("settings.json must contain a JSON object")

    def make_hook(arg: str):
        return {
            "matcher": ".*",
            "hooks": [{
                "type": "command",
                "command": build_hook_command(installed_probe, arg),
                "async": True,
            }],
        }

    hooks = settings.get("hooks", {})
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        raise SystemExit("settings.json field 'hooks' must be a JSON object")
    changed = False
    all_events = {
        "PreToolUse": "pretool",
        "PostToolUse": "posttool",
        "PostToolUseFailure": "posttool_fail",
        "Notification": "notification",
        "SessionStart": "session_start",
        "SessionEnd": "session_end",
        "Stop": "stop",
        "SubagentStart": "subagent_start",
        "SubagentStop": "subagent_stop",
        "PreCompact": "pre_compact",
        "PostCompact": "post_compact",
        "UserPromptSubmit": "user_prompt",
        "InstructionsLoaded": "instructions_loaded",
        "PermissionRequest": "perm_request",
        "PermissionDenied": "perm_denied",
        "TaskCreated": "task_created",
        "TaskCompleted": "task_completed",
        "FileChanged": "file_changed",
        "CwdChanged": "cwd_changed",
        "ConfigChange": "config_change",
    }

    for event, arg in all_events.items():
        existing = hooks.get(event, [])
        if not isinstance(existing, list):
            existing = [existing]

        has_probe = any("harness_probe.py" in json.dumps(h) for h in existing)
        if has_probe:
            for h in existing:
                for hook in h.get("hooks", []):
                    if hook.get("type") == "command" and "harness_probe.py" in hook.get("command", ""):
                        new_cmd = build_hook_command(installed_probe, arg)
                        if hook.get("command") != new_cmd:
                            hook["command"] = new_cmd
                            changed = True
        else:
            existing.append(make_hook(arg))
            changed = True

        hooks[event] = existing

    if changed:
        settings["hooks"] = hooks
        backup_path = SETTINGS_PATH.with_suffix(SETTINGS_PATH.suffix + ".bak")
        shutil.copy2(SETTINGS_PATH, backup_path)
        temp_path = SETTINGS_PATH.with_suffix(SETTINGS_PATH.suffix + ".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, SETTINGS_PATH)
        print(f"hooks installed: {installed_probe}")
    else:
        print("hooks up to date.")

    subprocess.run(
        [sys.executable, str(installed_probe), "notification"],
        input='{"test":"setup"}',
        text=True,
        check=False,
    )

    register_mcp_server()

    print("restart Claude Code for hooks to take effect.\n")


def register_mcp_server() -> None:
    """Register neo as a stdio MCP server in ~/.claude.json.

    Claude Code reads this file at startup to learn which MCP servers to
    spawn. We add (or update) an entry under ``mcpServers.neo`` that runs
    ``python -m neo.mcp_server``. Idempotent: re-running setup just keeps
    the command in sync with the current Python interpreter.
    """
    if not CLAUDE_CONFIG_PATH.exists():
        config: dict = {}
    else:
        try:
            with open(CLAUDE_CONFIG_PATH, encoding="utf-8") as f:
                config = json.load(f)
        except json.JSONDecodeError as exc:
            print(f"WARN: ~/.claude.json is not valid JSON ({exc}); skipping MCP registration.")
            return

    if not isinstance(config, dict):
        print("WARN: ~/.claude.json must contain a JSON object; skipping MCP registration.")
        return

    servers = config.get("mcpServers")
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        print("WARN: ~/.claude.json field 'mcpServers' must be an object; skipping.")
        return

    desired = {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-m", "neo.mcp_server"],
        "env": {},
    }

    current = servers.get(MCP_SERVER_NAME)
    if current == desired:
        print(f"mcp server '{MCP_SERVER_NAME}' already registered.")
        return

    servers[MCP_SERVER_NAME] = desired
    config["mcpServers"] = servers

    if CLAUDE_CONFIG_PATH.exists():
        backup = CLAUDE_CONFIG_PATH.with_suffix(CLAUDE_CONFIG_PATH.suffix + ".bak")
        shutil.copy2(CLAUDE_CONFIG_PATH, backup)

    temp_path = CLAUDE_CONFIG_PATH.with_suffix(CLAUDE_CONFIG_PATH.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, CLAUDE_CONFIG_PATH)
    print(f"mcp server '{MCP_SERVER_NAME}' registered in {CLAUDE_CONFIG_PATH}")


def get_setup_status() -> dict:
    status = {
        "has_settings": SETTINGS_PATH.exists(),
        "settings_parse_error": None,
        "installed_probe_exists": INSTALLED_PROBE_PATH.is_file(),
        "hooks_configured": False,
        "hook_command_count": 0,
    }

    if not status["has_settings"]:
        return status

    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            settings = json.load(f)
    except Exception as exc:
        status["settings_parse_error"] = str(exc)
        return status

    hooks = settings.get("hooks", {})
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        status["settings_parse_error"] = "settings.json field 'hooks' must be a JSON object"
        return status
    hook_count = 0
    for config in hooks.values():
        entries = config if isinstance(config, list) else [config]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for hook in entry.get("hooks", []):
                if not isinstance(hook, dict):
                    continue
                if hook.get("type") == "command" and "harness_probe.py" in hook.get("command", ""):
                    hook_count += 1

    status["hook_command_count"] = hook_count
    status["hooks_configured"] = hook_count > 0
    return status


def escape_like(term: str) -> str:
    return (
        term.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def load_dashboard_html() -> str:
    if TEMPLATE_PATH.is_file():
        return TEMPLATE_PATH.read_text(encoding="utf-8")
    return importlib.resources.files(RESOURCE_TEMPLATE_PACKAGE).joinpath(
        RESOURCE_TEMPLATE_NAME
    ).read_text(encoding="utf-8")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "neo"
    sys_version = ""

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        try:
            self.enforce_allowed_host()
            if path in ("/", "/dashboard"):
                content = load_dashboard_html().encode("utf-8")
                self.respond(HTTPStatus.OK, content, "text/html")
                return

            if path == "/favicon.ico":
                self.respond(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
                return

            if path == "/api/summary":
                self.json_response(db.summary())
                return

            if path == "/api/status":
                self.json_response(get_setup_status())
                return

            if path == "/api/telemetry":
                limit = self.get_int_param(params, "limit", 500, minimum=1, maximum=MAX_LIMIT)
                offset = self.get_int_param(params, "offset", 0, minimum=0, maximum=MAX_OFFSET)
                rows = db.query(
                    "SELECT time, event, session_id, parent_session_id, version, model "
                    "FROM telemetry ORDER BY time DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
                self.json_response(rows)
                return

            if path == "/api/sessions":
                limit = self.get_int_param(params, "limit", 500, minimum=1, maximum=MAX_LIMIT)
                rows = db.query(
                    "SELECT project, session_id, mtime, agent_count, compaction_count "
                    "FROM sessions ORDER BY project, mtime DESC LIMIT ?",
                    (limit,),
                )
                self.json_response(rows)
                return

            if path == "/api/agents":
                limit = self.get_int_param(params, "limit", 100, minimum=1, maximum=MAX_LIMIT)
                rows = db.query(
                    "SELECT id, project, session_id, file_name, is_compaction, mtime, size_bytes, message_count "
                    "FROM agents ORDER BY mtime DESC LIMIT ?",
                    (limit,),
                )
                self.json_response(rows)
                return

            if path == "/api/messages":
                agent_id = self.get_int_param(params, "agent_id", None, minimum=1, maximum=10_000_000, required=True)
                limit = self.get_int_param(params, "limit", 1000, minimum=1, maximum=2000)
                rows = db.query(
                    "SELECT role, content FROM messages WHERE agent_id=? ORDER BY idx LIMIT ?",
                    (agent_id, limit),
                )
                self.json_response(rows)
                return

            if path == "/api/memory":
                limit = self.get_int_param(params, "limit", 200, minimum=1, maximum=MAX_LIMIT)
                rows = db.query(
                    "SELECT project, file_name, content FROM memory_files ORDER BY project, file_name LIMIT ?",
                    (limit,),
                )
                self.json_response(rows)
                return

            if path == "/api/hooks":
                limit = self.get_int_param(params, "limit", 100, minimum=1, maximum=MAX_LIMIT)
                rows = db.query(
                    "SELECT timestamp, event_type, data_json FROM hook_events ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                )
                self.json_response(rows)
                return

            if path == "/api/search":
                term = params.get("q", [""])[0].strip()
                if not term:
                    self.json_response([])
                    return
                if len(term) > MAX_SEARCH_TERM_LENGTH:
                    raise BadRequest(f"search term must be {MAX_SEARCH_TERM_LENGTH} characters or fewer")

                like = "%" + escape_like(term) + "%"
                results = []
                for row in db.query(
                    "SELECT time, event, session_id FROM telemetry "
                    "WHERE event LIKE ? ESCAPE '\\' OR session_id LIKE ? ESCAPE '\\' LIMIT ?",
                    (like, like, SEARCH_RESULTS_PER_TABLE),
                ):
                    row["source"] = "telemetry"
                    results.append(row)
                for row in db.query(
                    "SELECT role, content, agent_id FROM messages "
                    "WHERE content LIKE ? ESCAPE '\\' LIMIT ?",
                    (like, SEARCH_RESULTS_PER_TABLE),
                ):
                    row["source"] = "message"
                    results.append(row)
                for row in db.query(
                    "SELECT project, file_name, content FROM memory_files "
                    "WHERE content LIKE ? ESCAPE '\\' LIMIT ?",
                    (like, SEARCH_RESULTS_PER_TABLE),
                ):
                    row["source"] = "memory"
                    results.append(row)
                self.json_response(results)
                return

            if path == "/api/tasks":
                limit = self.get_int_param(params, "limit", 200, minimum=1, maximum=MAX_LIMIT)
                rows = db.query(
                    "SELECT session_id, task_id, subject, description, status "
                    "FROM tasks ORDER BY session_id LIMIT ?",
                    (limit,),
                )
                self.json_response(rows)
                return

            if path == "/api/correlations":
                project_limit = self.get_int_param(params, "project_limit", 100, minimum=1, maximum=MAX_LIMIT)
                event_limit = self.get_int_param(params, "event_limit", 100, minimum=1, maximum=MAX_LIMIT)
                session_limit = self.get_int_param(params, "session_limit", 200, minimum=1, maximum=MAX_LIMIT)
                timeline_limit = self.get_int_param(
                    params,
                    "timeline_limit",
                    DEFAULT_CORRELATION_TIMELINE_LIMIT,
                    minimum=1,
                    maximum=5000,
                )

                hook_timeline = db.query(
                    "SELECT minute, count, event_type FROM ("
                    "SELECT substr(timestamp, 1, 16) as minute, COUNT(*) as count, event_type "
                    "FROM hook_events GROUP BY minute, event_type ORDER BY minute DESC LIMIT ?"
                    ") ORDER BY minute",
                    (timeline_limit,),
                )
                hook_totals = db.query(
                    "SELECT event_type, COUNT(*) as count "
                    "FROM hook_events GROUP BY event_type ORDER BY count DESC LIMIT ?",
                    (event_limit,),
                )
                agents_per_project = db.query(
                    "SELECT project, COUNT(*) as total, "
                    "SUM(CASE WHEN is_compaction=1 THEN 1 ELSE 0 END) as compactions, "
                    "SUM(CASE WHEN is_compaction=0 THEN 1 ELSE 0 END) as agents, "
                    "SUM(message_count) as total_messages "
                    "FROM agents GROUP BY project ORDER BY total DESC LIMIT ?",
                    (project_limit,),
                )
                tel_by_type = db.query(
                    "SELECT event, COUNT(*) as count FROM telemetry GROUP BY event ORDER BY count DESC LIMIT ?",
                    (event_limit,),
                )
                session_spans = db.query(
                    "SELECT session_id, MIN(time) as first_seen, MAX(time) as last_seen, COUNT(*) as event_count "
                    "FROM telemetry GROUP BY session_id ORDER BY first_seen DESC LIMIT ?",
                    (session_limit,),
                )
                self.json_response({
                    "hook_timeline": hook_timeline,
                    "hook_totals": hook_totals,
                    "agents_per_project": agents_per_project,
                    "telemetry_by_type": tel_by_type,
                    "session_spans": session_spans,
                })
                return

            if path == "/api/reminders":
                limit = self.get_int_param(params, "limit", 50, minimum=1, maximum=MAX_LIMIT)
                rows = db.query(
                    "SELECT source_file, line_number, content, timestamp "
                    "FROM system_reminders "
                    "ORDER BY COALESCE(timestamp, '') DESC, source_file DESC, line_number DESC LIMIT ?",
                    (limit,),
                )
                self.json_response(rows)
                return

            if path == "/api/states":
                self.json_response(states.get_state_diagram())
                return

            if path == "/api/analyze":
                limit = self.get_int_param(params, "limit", MAX_ANALYZE_EVENTS, minimum=1, maximum=MAX_ANALYZE_EVENTS)
                hook_events = db.query(
                    "SELECT timestamp, event_type, data_json as data "
                    "FROM hook_events ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                )
                hook_events.reverse()
                for event in hook_events:
                    if isinstance(event.get("data"), str):
                        try:
                            event["data"] = json.loads(event["data"])
                        except json.JSONDecodeError:
                            pass
                analysis = states.analyze_session(hook_events)
                self.json_response(analysis)
                return

            if path == "/api/tokens":
                self.json_response(tokens.summary())
                return

            self.respond(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
        except BadRequest as exc:
            self.json_error(HTTPStatus.BAD_REQUEST, str(exc))
        except PermissionError as exc:
            self.json_error(HTTPStatus.FORBIDDEN, str(exc))
        except Exception:
            traceback.print_exc()
            self.json_error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal server error")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            self.enforce_allowed_host()
            if path != "/api/ingest":
                self.respond(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
                return

            self.enforce_same_origin_post()
            self.read_request_body(max_bytes=1024 * 64)
            result = db.ingest_all()
            self.json_response(result)
        except BadRequest as exc:
            self.json_error(HTTPStatus.BAD_REQUEST, str(exc))
        except PermissionError as exc:
            self.json_error(HTTPStatus.FORBIDDEN, str(exc))
        except Exception:
            traceback.print_exc()
            self.json_error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal server error")

    def do_OPTIONS(self):
        try:
            self.enforce_allowed_host()
            self.respond(HTTPStatus.METHOD_NOT_ALLOWED, b"method not allowed", "text/plain")
        except PermissionError as exc:
            self.json_error(HTTPStatus.FORBIDDEN, str(exc))

    def enforce_allowed_host(self) -> None:
        allowed_hosts = getattr(self.server, "allowed_hosts", set())
        host = (self.headers.get("Host") or "").strip().lower()
        if host in allowed_hosts:
            return
        raise PermissionError("unexpected Host header")

    def get_int_param(self, params, name, default, minimum=0, maximum=None, required=False):
        raw = params.get(name, [None])[0]
        if raw in (None, ""):
            if required:
                raise BadRequest(f"missing required parameter: {name}")
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise BadRequest(f"invalid integer for {name}") from exc
        if value < minimum:
            raise BadRequest(f"{name} must be >= {minimum}")
        if maximum is not None and value > maximum:
            raise BadRequest(f"{name} must be <= {maximum}")
        return value

    def read_request_body(self, max_bytes: int) -> bytes:
        length_header = self.headers.get("Content-Length", "0")
        try:
            length = int(length_header)
        except ValueError as exc:
            raise BadRequest("invalid Content-Length") from exc
        if length < 0 or length > max_bytes:
            raise BadRequest("request body too large")
        if length == 0:
            return b""
        return self.rfile.read(length)

    def enforce_same_origin_post(self) -> None:
        allowed_origins = getattr(self.server, "allowed_origins", set())
        origin = self.headers.get("Origin")
        referer = self.headers.get("Referer")

        if origin and origin in allowed_origins:
            return
        if referer and any(referer == allowed or referer.startswith(allowed + "/") for allowed in allowed_origins):
            return

        raise PermissionError("cross-origin POST blocked")

    def json_response(self, data, status=HTTPStatus.OK):
        content = json.dumps(data, default=str).encode("utf-8")
        self.respond(status, content, "application/json")

    def json_error(self, status, message: str):
        self.json_response({"error": message}, status=status)

    def respond(self, code, content: bytes, content_type: str):
        try:
            self.send_response(int(code))
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            if content_type == "text/html":
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
                )
            self.end_headers()
            self.wfile.write(content)
        except BrokenPipeError:
            pass

    def log_message(self, *args):
        pass


class ThreadedHTTPServer(ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        pass


def should_open_browser(args) -> bool:
    return NO_OPEN_FLAG not in args


def serve(port: int = DEFAULT_PORT, launch_browser: bool = True) -> None:
    server = ThreadedHTTPServer(("127.0.0.1", port), Handler)
    server.allowed_origins = {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
    }
    server.allowed_hosts = {
        f"127.0.0.1:{port}",
        f"localhost:{port}",
    }
    url = "http://127.0.0.1:" + str(port)
    print("neo: " + url, file=sys.stderr)
    if launch_browser:
        open_browser(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
    finally:
        server.server_close()


def get_port(args):
    if "--port" in args:
        idx = args.index("--port")
        if idx + 1 < len(args):
            try:
                port = int(args[idx + 1])
            except ValueError as exc:
                raise SystemExit("--port must be an integer") from exc
            if port < 1 or port > 65535:
                raise SystemExit("--port must be between 1 and 65535")
            return port
        raise SystemExit("--port requires a value")
    return DEFAULT_PORT


def main(argv=None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    port = get_port(args)
    launch_browser = should_open_browser(args)

    # Migrate legacy ~/.harnesster -> ~/.neo before any sub-command touches
    # state. setup() also calls this; harmless to call twice.
    migrate_legacy_state()

    if "--mcp" in args:
        from . import mcp_server
        mcp_server.serve()
        return

    if "--setup" in args:
        setup()
    elif "--ingest" in args:
        result = db.ingest_all()
        print("ingested:", result)
        print("db:", db.summary())
    elif "--dashboard" in args:
        serve(port, launch_browser=launch_browser)
    elif not args or "--port" in args:
        setup()
        db.ingest_all()
        print("data:", db.summary())
        print()
        serve(port, launch_browser=launch_browser)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
