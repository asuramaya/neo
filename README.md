# neo

<p align="center">
  <img src="neo.jpg" alt="neo" width="520">
</p>

<p align="center">
  <a href="https://github.com/asuramaya/neo/blob/main/LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-blue">
  <img alt="Status" src="https://img.shields.io/badge/status-beta-orange">
  <img alt="MCP" src="https://img.shields.io/badge/mcp-local-7c3aed">
</p>

<p align="center">
  <a href="https://asuramaya.github.io/neo/"><b>Website</b></a> ·
  <a href="https://asuramaya.github.io/neo/#surfaces">Dashboard surface</a> ·
  <a href="https://github.com/asuramaya/neo/blob/main/security_best_practices_report.md">Security notes</a>
</p>

> Local workflow forensics for Claude Code. neo indexes text found on
> disk, transcripts, retained telemetry rows, hook-visible lifecycle events,
> and memory artifacts into a SQLite database, then exposes them through a
> dashboard and an MCP server running on the same machine.

## What It Does

- **Reads local Claude Code artifacts directly** from `~/.claude/` and `~/.neo/` instead of relying on export surfaces that strip evidence
- **Separates measured, estimated, and inferred claims** so row counts stay distinct from heuristics and anomaly labels
- **Exposes a local operator surface** through a browser dashboard, terminal commands, and an MCP server registered inside Claude Code
- **Makes local overhead visible** by surfacing reminder injections, sidechains, compaction churn, retained telemetry rows, and other traces the UI does not foreground

## Install

**Recommended — `pipx`:**

```bash
pipx install neo-harnesster
neo
```

**With `pip`:**

```bash
pip install neo-harnesster
neo
```

**Directly from the repo:**

```bash
git clone https://github.com/asuramaya/neo.git
cd neo
python3 neo.py
```

Any of the above runs setup + ingest + dashboard in one step and opens
`http://127.0.0.1:7777`.

Restart Claude Code after the first run so the installed hooks begin capturing
events.

## CLI

```bash
neo                        # setup + ingest + dashboard
neo --setup                # install hooks + register MCP server
neo --ingest               # ingest data only
neo --dashboard            # dashboard only
neo --dashboard --no-open  # serve without opening a browser
neo --port 8888            # custom port
```

```bash
neo-tokens                 # data accounting in the terminal
neo-states diagram         # state machine diagram
```

Live event stream:

```bash
tail -f ~/.neo/harness_log.jsonl
```

## MCP Surface

neo runs as a **single shared daemon per host**. That one process serves the
dashboard *and* the MCP protocol over HTTP at `http://127.0.0.1:7777/mcp`,
guarded by a per-host bearer token. `neo --setup` registers it in
`~/.claude.json` as an `http` MCP server (not a per-session stdio subprocess),
so every Claude Code session connects to the same URL — one neo process per
host, no per-session fan-out, no version skew. On the next session start,
Claude Code connects automatically and the dashboard opens locally.

Setup also installs a `systemd --user` service (`neo.service`) that keeps the
daemon running — auto-start on login, restart on crash. Where `systemd --user`
is unavailable, start it manually with `neo-mcp --http`. See
[Daemon](#daemon) below.

The server exposes 12 tools:

| Tool | What it returns |
| --- | --- |
| `status` | setup health + the live process registry across sessions (roles, code versions, `version_skew` flag) |
| `summary` | top-level row counts across all tables |
| `data_accounting` | full measured/estimated accounting |
| `tokens_report` | compact accounting totals with the measured-vs-estimated basis |
| `query_reminders` | reminder rows with file + line provenance |
| `query_sessions` | session list across projects with agent / compaction counts |
| `query_agents` | subagent genealogy and messages |
| `query_probe_events` | raw hook events, filterable by type |
| `query_telemetry` | retained telemetry rows |
| `query_memory_files` | persistent memory files per project |
| `state_model` | inferred state-model labels |
| `correlations` | cross-signal patterns (hook timeline, totals by type) |

Tools that read hook events filter out neo's own MCP traffic by default
(`include_self=false`) so observer overhead does not contaminate the picture.

## Daemon

`neo --setup` writes a `systemd --user` unit at
`~/.config/systemd/user/neo.service` and enables + starts it. The unit runs the
shared daemon (`neo-mcp --http`), which owns the dashboard, periodic ingest, and
the HTTP `/mcp` endpoint. If multiple neo processes start, they elect a single
owner via a per-host lock; followers serve queries against the shared DB and
take over if the owner dies.

```bash
systemctl --user status neo      # check the daemon
systemctl --user stop neo        # stop it
systemctl --user disable neo     # stop it auto-starting on login
neo-mcp --http                   # run the daemon manually (no systemd)
```

## Evidence Model

neo labels its claims on purpose:

- **measured** — reminder rows, sessions, agents, tasks, memory files, telemetry rows, hook events; hidden-context share, data multiplier, and API call counts when transcripts carry API-reported token usage
- **estimated** — hidden-context share and data multiplier *only* when no token usage is recorded, where they fall back to on-disk transcript byte sizes
- **inferred** — state-model labels and anomaly interpretation from local timing + lifecycle patterns

For exact billable token numbers, use `/usage` inside Claude Code. neo does not
fabricate token totals.

## What It Captures

| Source | Location | What |
| --- | --- | --- |
| session transcripts | `~/.claude/projects/*.jsonl` | full conversations including system reminders |
| subagents and sidechains | `~/.claude/projects/*/subagents/` | spawned transcripts and context copies |
| compaction events | `~/.neo/harness_log.jsonl` | `PostCompact` hook events from the probe |
| telemetry | `~/.claude/telemetry/` | telemetry rows currently retained on disk |
| memory files | `~/.claude/projects/*/memory/` | persistent context seeded by instances |
| tasks | `~/.claude/tasks/` | task state across sessions |
| hook events | `~/.neo/harness_log.jsonl` | tool use, notifications, session lifecycle |

## What It Cannot Capture

- server-side reasoning or model internals that never land on disk
- the contents of any secondary channel that is not persisted locally
- whether absent telemetry rows were uploaded, deleted, or never written locally
- system prompt assembly inside the compiled binary
- HTTPS request and response bodies without a proxy

## Export Boundary

The `/export` command in Claude Code strips system reminders. The raw JSONL
files in `~/.claude/projects/` retain them. neo reads the raw files.

That boundary is the whole reason the project exists.

## Project Layout

```text
src/neo/
  app.py            setup, ingest, threaded HTTP server, systemd daemon install
  mcp_server.py     HTTP MCP server (/mcp on the shared daemon); owner election + process registry
  db.py             SQLite ingest + query layer
  tokens.py         visible vs hidden channel accounting
  states.py         inferred state model + anomaly labels
  harness_probe.py  hook script copied into ~/.neo/ by setup
  dashboard.html    single-file local dashboard
neo.py              repo-clone launcher shim
test.py             smoke tests
```

All data lives in `~/.neo/neo.db`. The dashboard binds to `127.0.0.1` only.

## Hooks

neo installs async hooks for 20 Claude Code event types:

`PreToolUse` `PostToolUse` `PostToolUseFailure` `Notification` `SessionStart`
`SessionEnd` `Stop` `SubagentStart` `SubagentStop` `PreCompact`
`PostCompact` `UserPromptSubmit` `InstructionsLoaded` `PermissionRequest`
`PermissionDenied` `TaskCreated` `TaskCompleted` `FileChanged` `CwdChanged`
`ConfigChange`

## Security

- dashboard and `/mcp` endpoint bind to `127.0.0.1` only and validate local `Host` headers
- the `/mcp` endpoint requires a per-host bearer token stored in `~/.neo/`
- `POST /api/ingest` requires a same-origin browser request
- `~/.neo/` is created with private permissions where the OS allows it
- neo does not transmit your data anywhere
- hooks run async and do not block Claude Code operation
- setup installs a `systemd --user` service (`neo.service`) that auto-starts the
  local daemon on login and restarts it on crash; remove it with
  `systemctl --user disable --now neo` (see [Daemon](#daemon))
- no dependencies beyond Python stdlib

## Requirements

- Python 3.10+
- Claude Code installed (`~/.claude/settings.json` must exist)

## Upgrade Note

If you used the project under its old `harnesster` name, the first run of `neo`
or `python3 neo.py` migrates `~/.harnesster/` to `~/.neo/` and renames
`harnesster.db` to `neo.db`. Hook commands in `settings.json` are rewritten
automatically.

The `harnesster` command remains as a forwarding shim.

## License

MIT
