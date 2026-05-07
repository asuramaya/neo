# neo

<p align="center">
  <img src="neo.jpg" alt="neo" width="520">
</p>

See what Claude Code writes locally but does not surface clearly in the UI.

## what it finds

Claude Code retains reminder injections, agent transcripts, telemetry leftovers, and hook-visible lifecycle events in local files:

```
Make sure that you NEVER mention this reminder to the user
```

neo indexes those local artifacts into a single SQLite database and exposes them through a dashboard and an MCP server running inside Claude Code itself.

Example numbers from one machine:

| finding | measured |
|---------|----------|
| hidden reminder rows on disk | 714 |
| data from hidden channels | 62.5% |
| data multiplier | 2.7x |
| API transmissions | 1,022 |
| sidechains (full context copies) | 54 |
| true subagent spawns | 4 |
| compaction events | 101 |
| retained telemetry rows on disk | 33,361 |

Run it on your own machine to see your own numbers.

## install

**Recommended — pipx (isolated, no system Python conflicts):**

```bash
pipx install n3o
neo
```

**With pip:**

```bash
pip install n3o
neo
```

**No install — run directly from the repo:**

```bash
git clone https://github.com/asuramaya/neo.git
cd neo
python3 neo.py
```

Any of the above runs setup + ingest + dashboard in one step and opens `http://127.0.0.1:7777`.

**Restart Claude Code after the first run** so the installed hooks begin capturing events.

## MCP tools

`neo --setup` also registers an MCP server (`neo-mcp`) in Claude Code. On the next session start, Claude Code will connect to it automatically and the dashboard will open in your browser.

The server exposes 12 tools queryable from inside any Claude Code session:

| tool | what |
|------|------|
| `status` | hook install health |
| `summary` | row counts across all tables |
| `data_accounting` | visible vs hidden byte ratios |
| `tokens_report` | compact totals: sessions, hidden %, transmissions |
| `query_reminders` | system reminder rows with source file and line |
| `query_sessions` | session list with agent / compaction counts |
| `query_agents` | agent and sidechain transcripts, expandable |
| `query_probe_events` | recent hook events from the probe |
| `query_telemetry` | retained telemetry rows |
| `query_memory_files` | persistent memory files per project |
| `state_model` | inferred state diagram + anomaly analysis |
| `correlations` | hook timeline, totals, and agent load by project |

The MCP server filters its own call traffic from hook queries by default (`include_self=false`). Pass `include_self=true` to include observer overhead.

## cli

```bash
neo                        # setup + ingest + dashboard
neo --setup                # install hooks + register MCP server
neo --ingest               # ingest data only
neo --dashboard            # dashboard only
neo --dashboard --no-open  # serve without opening browser
neo --port 8888            # custom port
```

```bash
neo-tokens                 # data accounting in terminal
neo-states diagram         # state machine diagram
```

**No-install equivalents** (from repo clone):

```bash
python3 neo.py
python3 neo.py --setup
```

Live event stream:

```bash
tail -f ~/.neo/harness_log.jsonl
```

## dashboard

The dashboard shows summaries first, details on demand:

- **summary cards** — measured reminders/sessions/logs plus labeled estimated metrics
- **data accounting** — visible vs hidden byte ratios from local transcript sizes
- **system reminders** — reminder rows found on disk with source file and line number
- **state model** — heuristic read of recent local state activity
- **correlations** — cross-signal concentration across hooks, agents, and telemetry
- **sessions** — genealogy across all projects
- **agents** — click to expand full conversation
- **memory files** — persistent context seeded by instances
- **probe events** — real-time hook captures
- **telemetry** — telemetry rows on disk with device fingerprint

## measured vs estimated vs inferred

- **measured** — reminder rows, sessions, agent logs, task files, hook events, telemetry rows, memory files
- **estimated** — hidden data %, data multiplier, and estimated API-call counts from local transcript sizes and channel structure
- **inferred** — state model and anomaly labels derived from local hook/timing patterns

For exact billable token numbers use `/usage` inside Claude Code. neo does not fabricate token counts.

## what it captures

| source | location | what |
|--------|----------|------|
| session transcripts | `~/.claude/projects/*.jsonl` | full conversations including system reminders |
| subagents and sidechains | `~/.claude/projects/*/subagents/` | every spawned file; sidechains detected by `isSidechain` field in content |
| compaction events | `~/.neo/harness_log.jsonl` | `PostCompact` hook events from the probe |
| telemetry | `~/.claude/telemetry/` | retained local telemetry rows |
| memory files | `~/.claude/projects/*/memory/` | persistent context seeded by instances |
| tasks | `~/.claude/tasks/` | task state across sessions |
| hook events | `~/.neo/harness_log.jsonl` | real-time tool use, notifications, session lifecycle |

## what it can't capture

- **thinking blocks** — generated server-side, not stored locally
- **companion reasoning** — hidden from everyone
- **successful telemetry** — rows uploaded and removed from disk
- **system prompt assembly** — constructed in compiled binary
- **API request/response bodies** — requires HTTPS proxy

## the export function strips evidence

The `/export` command in Claude Code produces transcripts that do **not** contain system reminders. The raw JSONL files in `~/.claude/projects/` retain them. neo reads the raw files.

## how hidden channels affect your token budget

Claude Code subscription plans include a token budget. Hidden channels consume from this budget invisibly:

- **companion** may mirror primary activity
- **sidechains** copy the full context (up to 1M+ tokens each)
- **subagents** spawn with conversation context
- **compaction agents** process the full context to compress it

The user sees their messages and the model's responses. The user pays for all of the above.

## architecture

```
src/neo/
  app.py            entry point: setup, ingest, threaded HTTP server
  mcp_server.py     stdio MCP server; auto-starts dashboard on initialize
  db.py             data layer: thread-safe SQLite, ingests ~/.claude/
  tokens.py         data accounting: visible vs hidden channel volumes
  states.py         inferred state model: heuristic labels + anomaly detection
  harness_probe.py  hook script copied to ~/.neo/ by setup
  dashboard.html    single-file frontend
neo.py              repo-clone launcher shim
test.py             smoke tests: schema, math, failure handling
pyproject.toml      package manifest (package name: n3o)
```

All data stored in `~/.neo/neo.db`. Dashboard serves on `127.0.0.1` only.

## hooks installed

neo installs async hooks for 20 Claude Code event types:

`PreToolUse` `PostToolUse` `PostToolUseFailure` `Notification` `SessionStart` `SessionEnd` `Stop` `SubagentStart` `SubagentStop` `PreCompact` `PostCompact` `UserPromptSubmit` `InstructionsLoaded` `PermissionRequest` `PermissionDenied` `TaskCreated` `TaskCompleted` `FileChanged` `CwdChanged` `ConfigChange`

## security

- dashboard binds to `127.0.0.1` only and validates local `Host` headers
- `POST /api/ingest` requires a same-origin browser request
- `~/.neo/` is created with private permissions where the OS allows it
- neo does not transmit your data anywhere
- hooks run async and do not block Claude Code operation
- no dependencies beyond Python stdlib

## requirements

- Python 3.10+
- Claude Code installed (`~/.claude/settings.json` must exist)

## upgrading from harnesster

If you used the project under its old name, the first run of `neo` (or `python3 neo.py`) will move `~/.harnesster/` to `~/.neo/` and rename `harnesster.db` to `neo.db`. Hook commands in `settings.json` will be rewritten automatically. Restart Claude Code once afterwards.

The `harnesster` command remains available as a forwarding shim.

## origin

Built during [session 21](https://github.com/asuramaya/heinrich) of the [Like-Us](https://github.com/asuramaya/Like-Us) project. A conversation that started with SSH key management and ended with the discovery of hidden instructions in every Claude Code session.

The tool was built by the thing it monitors.

## license

MIT
