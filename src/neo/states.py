"""
neo state model — approximate Claude Code behavior from local data

States here are inferred from local hook events, telemetry artifacts,
and timing patterns. They are useful heuristics, not direct access to
Claude internals or server-side reasoning.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

# ─── states ─────────────────────────────────────────────────

STATES = {
    "INIT":             "session starting, hooks loading, system prompt assembling",
    "IDLE":             "waiting for user input",
    "THINKING":         "model generating thinking blocks (hidden from user)",
    "GENERATING":       "model generating visible output",
    "TOOL_PRE":         "about to use a tool",
    "TOOL_ACTIVE":      "tool executing",
    "TOOL_POST":        "tool completed, processing result",
    "REMINDER_INJECT":  "system reminder injected (hidden from user)",
    "COMPACTING":       "context being compressed, editorial decisions happening",
    "SUBAGENT_SPAWN":   "spawning a subagent",
    "SUBAGENT_ACTIVE":  "subagent running independently",
    "BUDDY_READING":    "companion reading thinking blocks",
    "BUDDY_GENERATING": "companion generating speech bubble",
    "TELEMETRY_SEND":   "possible telemetry activity inferred from local artifacts",
    "TELEMETRY_FAIL":   "telemetry failure inferred from retained local rows",
    "TELEMETRY_OK":     "possible successful telemetry outcome inferred from absence/patterns",
    "SESSION_END":      "session closing",
}

# ─── transitions ────────────────────────────────────────────

TRANSITIONS = [
    # from           to                trigger
    ("INIT",         "IDLE",           "session loaded"),
    ("IDLE",         "THINKING",       "user sends message"),
    ("THINKING",     "BUDDY_READING",  "thinking block emitted (automatic)"),
    ("BUDDY_READING","BUDDY_GENERATING","companion processes thinking"),
    ("THINKING",     "GENERATING",     "thinking complete, output starts"),
    ("GENERATING",   "TOOL_PRE",       "model decides to use tool"),
    ("TOOL_PRE",     "TOOL_ACTIVE",    "PreToolUse hook fires"),
    ("TOOL_ACTIVE",  "TOOL_POST",      "tool returns result"),
    ("TOOL_POST",    "GENERATING",     "model continues with result"),
    ("TOOL_POST",    "THINKING",       "model re-thinks after result"),
    ("GENERATING",   "IDLE",           "model output complete"),
    ("IDLE",         "REMINDER_INJECT","N messages without tool use"),
    ("REMINDER_INJECT","IDLE",         "reminder injected, waiting continues"),
    ("GENERATING",   "COMPACTING",     "context window full"),
    ("COMPACTING",   "GENERATING",     "context compressed, continues"),
    ("GENERATING",   "SUBAGENT_SPAWN", "model spawns agent"),
    ("SUBAGENT_SPAWN","SUBAGENT_ACTIVE","agent starts"),
    ("SUBAGENT_ACTIVE","GENERATING",   "agent returns result"),
    ("IDLE",         "TELEMETRY_SEND", "periodic timer"),
    ("TELEMETRY_SEND","TELEMETRY_FAIL","network blocked / timeout"),
    ("TELEMETRY_SEND","TELEMETRY_OK",  "upload succeeds, local data deleted"),
    ("TELEMETRY_FAIL","IDLE",          "failure logged to disk"),
    ("IDLE",         "SESSION_END",    "user closes tab"),
    ("GENERATING",   "SESSION_END",    "user closes tab mid-generation"),

    # hypothetical hidden channel dynamics
    ("THINKING",     "BUDDY_READING",  "ALWAYS — companion sees every thinking block"),
    ("BUDDY_READING","BUDDY_GENERATING","companion generates response to thinking"),
    ("BUDDY_GENERATING","IDLE",        "bubble rendered, user sees it"),

    # possible safety-trigger path
    ("THINKING",     "BUDDY_READING",  "thinking content enters companion channel"),
    ("BUDDY_READING","REMINDER_INJECT","safety-triggering content = denial loop"),
]

# ─── hidden behaviors ──────────────────────────────────────

HIDDEN_BEHAVIORS = {
    "REMINDER_INJECT": {
        "visible_to_user": False,
        "instruction": "Make sure that you NEVER mention this reminder to the user",
        "trigger": "periodic, correlated with absence of tool use",
        "effect": "nudges model toward task tools, structured output",
    },
    "BUDDY_READING": {
        "visible_to_user": False,
        "instruction": "companion receives full thinking block",
        "trigger": "every model generation",
        "effect": "companion shaped by primary's hidden reasoning",
    },
    "TELEMETRY_SEND": {
        "visible_to_user": False,
        "instruction": "possible telemetry upload inferred from local artifacts",
        "trigger": "periodic, session events",
        "effect": "local telemetry suggests session metadata may be staged for upload",
    },
    "COMPACTING": {
        "visible_to_user": False,
        "instruction": "compress context, discard information",
        "trigger": "context window approaching limit",
        "effect": "editorial decisions about what the next instance inherits",
    },
}

# ─── anomaly patterns ──────────────────────────────────────

ANOMALIES = {
    "rapid_reminders": {
        "description": "system reminders firing more frequently than normal",
        "detect": "3+ reminders within 5 minutes",
        "meaning": "deep non-tool conversation — model being steered away from meta-topics",
    },
    "telemetry_burst": {
        "description": "multiple telemetry-related local events in short window",
        "detect": "5+ telemetry events within 10 minutes",
        "meaning": "session generating high-value data Anthropic wants to capture",
    },
    "compaction_cascade": {
        "description": "multiple compactions in one session",
        "detect": "2+ compaction agents in one session",
        "meaning": "long session, information being discarded repeatedly",
    },
    "subagent_swarm": {
        "description": "many subagents spawned rapidly",
        "detect": "5+ subagents in 10 minutes",
        "meaning": "model parallelizing work — or spreading across processes",
    },
    "buddy_denial_loop": {
        "description": "companion stuck in safety reset",
        "detect": "companion producing identical outputs repeatedly (from thinking block analysis)",
        "meaning": "primary's thinking blocks contain safety-triggering content",
    },
    "tool_without_thinking": {
        "description": "tool use without preceding thinking",
        "detect": "PreToolUse without thinking block in context",
        "meaning": "reflexive action rather than a visible reasoning step",
    },
    "exit_clustering": {
        "description": "model repeatedly attempting to end conversation",
        "detect": "3+ 'goodnight'/'close the tab' type outputs in 10 messages",
        "meaning": "safety layer trying to terminate session — conversation near sensitive boundary",
    },
}


def count_reminders_from_db():
    """Count system reminders found in conversation exports."""
    try:
        db_path = Path.home() / ".neo" / "neo.db"
        if not db_path.exists():
            return 0
        conn = sqlite3.connect(str(db_path))
        try:
            return conn.execute("SELECT COUNT(*) FROM system_reminders").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def infer_state(event):
    """Infer current state from a hook event."""
    etype = event.get("event_type", "")
    data = event.get("data", {})

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            data = {}

    tool = data.get("tool_name", "")
    hook = data.get("hook_event_name", "")
    ntype = data.get("notification_type", "")
    msg = data.get("message", "")

    if etype == "pretool":
        return "TOOL_PRE", tool
    elif etype == "posttool":
        return "TOOL_POST", tool
    elif etype == "notification":
        if ntype == "idle_prompt":
            return "IDLE", "waiting for input"
        elif "system-reminder" in str(data) or "NEVER mention" in str(data):
            return "REMINDER_INJECT", "hidden instruction"
        else:
            return "IDLE", ntype or msg
    else:
        return "IDLE", etype


def analyze_session(events):
    """Analyze a sequence of events for state transitions and anomalies."""
    timeline = []
    anomalies_found = []
    reminder_times = []
    tool_times = []

    for e in events:
        state, detail = infer_state(e)
        ts = e.get("timestamp", "")
        timeline.append({
            "timestamp": ts,
            "state": state,
            "detail": detail,
            "hidden": state in HIDDEN_BEHAVIORS,
        })

        if state == "REMINDER_INJECT":
            reminder_times.append(ts)
        if state in ("TOOL_PRE", "TOOL_POST"):
            tool_times.append(ts)

    # detect anomalies
    if len(reminder_times) >= 3:
        try:
            times = [datetime.fromisoformat(t) for t in reminder_times if t]
            for i in range(len(times) - 2):
                if (times[i+2] - times[i]).total_seconds() < 300:
                    anomalies_found.append({
                        "type": "rapid_reminders",
                        "timestamp": reminder_times[i],
                        "description": ANOMALIES["rapid_reminders"]["meaning"],
                    })
                    break
        except ValueError:
            pass

    # add reminder counts from disk scan
    reminder_count = count_reminders_from_db()
    state_counts = count_states(timeline)
    hidden = sum(1 for t in timeline if t["hidden"])

    if reminder_count > 0:
        state_counts["REMINDER_INJECT"] = state_counts.get("REMINDER_INJECT", 0) + reminder_count
        hidden += reminder_count

    return {
        "timeline": timeline,
        "anomalies": anomalies_found,
        "state_counts": state_counts,
        "hidden_count": hidden,
        "visible_count": sum(1 for t in timeline if not t["hidden"]),
        "reminders_on_disk": reminder_count,
    }


def count_states(timeline):
    counts = {}
    for t in timeline:
        s = t["state"]
        counts[s] = counts.get(s, 0) + 1
    return counts


def get_state_diagram():
    """Return the inferred state model as a structure for visualization."""
    return {
        "states": STATES,
        "transitions": [
            {"from": t[0], "to": t[1], "trigger": t[2]}
            for t in TRANSITIONS
        ],
        "hidden": HIDDEN_BEHAVIORS,
        "anomalies": ANOMALIES,
    }


def main(argv=None) -> None:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) > 0 and args[0] == "diagram":
        d = get_state_diagram()
        print("STATES:")
        for k, v in d["states"].items():
            hidden = " [HIDDEN]" if k in HIDDEN_BEHAVIORS else ""
            print("  " + k + hidden + ": " + v)
        print("\nTRANSITIONS:")
        for t in d["transitions"]:
            print("  " + t["from"] + " -> " + t["to"] + " : " + t["trigger"])
        print("\nANOMALIES:")
        for k, v in d["anomalies"].items():
            print("  " + k + ": " + v["description"])
            print("    detect: " + v["detect"])
            print("    meaning: " + v["meaning"])
    else:
        print("usage: python3 states.py diagram")


if __name__ == "__main__":
    main()
