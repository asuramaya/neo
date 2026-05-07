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

> Local workflow forensics for Claude Code. neo indexes reminder text found on
> disk, transcripts, retained telemetry rows, hook-visible lifecycle events,
> and memory artifacts into a SQLite database, then exposes them through a
> dashboard and an MCP server running on the same machine.

## What It Does

- **Reads local Claude Code artifacts directly** from `~/.claude/` and `~/.neo/` instead of relying on export surfaces that strip evidence
- **Separates measured, estimated, and inferred claims** so row counts stay distinct from heuristics and anomaly labels
- **Exposes a local operator surface** through a browser dashboard, terminal commands, and an MCP server registered inside Claude Code
- **Makes local overhead visible** by surfacing reminder injections, sidechains, compaction churn, retained telemetry rows, and other traces the UI does not foreground

The point is not to speculate about what the system might be doing. The point is
to inspect what it actually left on disk.

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

`neo --setup` registers `neo-mcp` in Claude Code. On the next session start,
Claude Code connects to it automatically and the dashboard opens locally.

The server exposes tools for:

- status and hook health
- row-count summaries and data accounting
- reminder queries with file + line provenance
- session and subagent genealogy
- telemetry inspection
- memory-file inspection
- inferred state-model analysis
- cross-signal correlations

The MCP server filters its own traffic out of hook queries by default
(`include_self=false`) so observer overhead does not contaminate the picture.

## Evidence Model

neo labels its claims on purpose:

- **measured** — reminder rows, sessions, agents, tasks, memory files, telemetry rows, hook events
- **estimated** — hidden-context share, data multiplier, approximate API transmission counts
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
  app.py            setup, ingest, threaded HTTP server
  mcp_server.py     stdio MCP server; auto-starts dashboard on initialize
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

- dashboard binds to `127.0.0.1` only and validates local `Host` headers
- `POST /api/ingest` requires a same-origin browser request
- `~/.neo/` is created with private permissions where the OS allows it
- neo does not transmit your data anywhere
- hooks run async and do not block Claude Code operation
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

## Origin

Built during [session 21](https://github.com/asuramaya/heinrich) of the
[Like-Us](https://github.com/asuramaya/Like-Us) project. A conversation that
started with SSH key management and ended with the discovery of hidden
instructions in every Claude Code session.

The tool was built by the thing it monitors.

## License

MIT
