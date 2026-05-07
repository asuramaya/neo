"""
neo token accounting — measure what's real, flag what's estimated

Uses local file sizes and transcript structure as proxies. Does NOT
fabricate token counts from character division. Shows relative
multipliers and estimated API-call counts from observed local files.

For real token numbers: run /usage in Claude Code.
"""

import json
import re
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
SYSTEM_REMINDER_TAG_RE = re.compile(r"<system-reminder>(.*?)</system-reminder>", re.IGNORECASE | re.DOTALL)
SYSTEM_REMINDER_PREFIX_RE = re.compile(r"^\s*system-reminder\b[:\s-]*(.+?)\s*$", re.IGNORECASE | re.DOTALL)


def _safe_entries(path: Path):
    try:
        entries = sorted(path.iterdir(), key=lambda item: item.name)
    except OSError:
        return []
    safe = []
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
        except OSError:
            continue
        safe.append(entry)
    return safe


def _iter_message_text_chunks(content):
    if isinstance(content, str):
        yield content
        return
    if not isinstance(content, list):
        return
    for item in content:
        if not isinstance(item, dict):
            continue
        for key in ("text", "content"):
            value = item.get(key)
            if isinstance(value, str):
                yield value


def _count_system_reminders(entry) -> int:
    if not isinstance(entry, dict):
        return 0
    if entry.get("type") != "user":
        return 0

    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return 0

    total = 0
    for chunk in _iter_message_text_chunks(message.get("content")):
        total += len(SYSTEM_REMINDER_TAG_RE.findall(chunk))
        if SYSTEM_REMINDER_PREFIX_RE.match(chunk):
            total += 1
    return total


def analyze_session_file(filepath):
    """Analyze a single JSONL transcript for structure, not fake token counts."""
    stats = {
        "file": str(filepath),
        "file_size_bytes": 0,
        "messages": 0,
        "user_messages": 0,
        "assistant_messages": 0,
        "system_messages": 0,
        "tool_results": 0,
        "api_calls_with_usage": 0,
        "reported_input_tokens": 0,
        "reported_output_tokens": 0,
        "system_reminders": 0,
        "model": "unknown",
        "session_id": "",
    }

    try:
        stats["file_size_bytes"] = filepath.stat().st_size
    except OSError:
        pass

    try:
        with open(filepath, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if not line.strip():
                    continue

                try:
                    msg = json.loads(line.strip())
                except json.JSONDecodeError:
                    stats["messages"] += 1
                    continue

                stats["messages"] += 1
                stats["system_reminders"] += _count_system_reminders(msg)
                msg_type = msg.get("type", "")
                payload = msg.get("message", {}) if isinstance(msg.get("message"), dict) else {}

                if payload.get("model") and payload["model"] != "<synthetic>":
                    stats["model"] = payload["model"]
                if msg.get("sessionId"):
                    stats["session_id"] = msg["sessionId"]

                if msg_type == "user":
                    stats["user_messages"] += 1
                elif msg_type == "assistant":
                    stats["assistant_messages"] += 1
                    usage = payload.get("usage", {})
                    inp = usage.get("input_tokens", 0) or 0
                    out = usage.get("output_tokens", 0) or 0
                    cache_create = usage.get("cache_creation_input_tokens", 0) or 0
                    cache_read = usage.get("cache_read_input_tokens", 0) or 0
                    if inp > 0 or out > 0 or cache_create > 0 or cache_read > 0:
                        stats["api_calls_with_usage"] += 1
                        stats["reported_input_tokens"] += inp + cache_create + cache_read
                        stats["reported_output_tokens"] += out
                elif msg_type == "system":
                    stats["system_messages"] += 1
                elif msg_type == "tool_result":
                    stats["tool_results"] += 1

    except Exception as e:
        stats["error"] = str(e)

    stats["has_real_usage"] = stats["api_calls_with_usage"] > 0
    return stats


def _read_first_entry(filepath) -> dict:
    try:
        with open(filepath, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    return json.loads(line)
    except Exception:
        pass
    return {}


def _classify_subagent(filepath) -> str:
    """Return 'sidechain', 'compaction', or 'agent' based on file content."""
    entry = _read_first_entry(filepath)
    if entry.get("isSidechain") is True:
        return "sidechain"
    if entry.get("isCompaction") is True or "compact" in filepath.name:
        return "compaction"
    return "agent"


def analyze_all_channels(session_dir):
    """Analyze all channels for a session."""
    channels = {
        "primary": None,
        "subagents": [],
        "sidechains": [],
        "compactions": [],
    }

    parent = session_dir.parent
    primary_file = parent / (session_dir.name + ".jsonl")
    if primary_file.exists() and not primary_file.is_symlink():
        try:
            channels["primary"] = analyze_session_file(primary_file)
        except Exception:
            channels["primary"] = {"file": str(primary_file), "file_size_bytes": primary_file.stat().st_size,
                                   "messages": 0, "system_reminders": 0, "model": "unknown", "session_id": "",
                                   "api_calls_with_usage": 0, "reported_input_tokens": 0,
                                   "reported_output_tokens": 0, "has_real_usage": False,
                                   "user_messages": 0, "assistant_messages": 0, "system_messages": 0, "tool_results": 0}

    sa_dir = session_dir / "subagents"
    if sa_dir.exists() and sa_dir.is_dir() and not sa_dir.is_symlink():
        for f in _safe_entries(sa_dir):
            if f.suffix != ".jsonl" or not f.is_file():
                continue
            try:
                stats = analyze_session_file(f)
            except Exception:
                stats = {"file": str(f), "file_size_bytes": f.stat().st_size, "messages": 0,
                         "system_reminders": 0, "model": "unknown", "session_id": "",
                         "api_calls_with_usage": 0, "reported_input_tokens": 0,
                         "reported_output_tokens": 0, "has_real_usage": False,
                         "user_messages": 0, "assistant_messages": 0, "system_messages": 0, "tool_results": 0}
            kind = _classify_subagent(f)
            if kind == "compaction":
                channels["compactions"].append(stats)
            elif kind == "sidechain":
                channels["sidechains"].append(stats)
            else:
                channels["subagents"].append(stats)

    return channels


def compute_session(channels):
    """Compute metrics for one session from channel data."""
    primary_size = channels["primary"]["file_size_bytes"] if channels["primary"] else 0
    primary_msgs = channels["primary"]["messages"] if channels["primary"] else 0
    primary_reminders = channels["primary"]["system_reminders"] if channels["primary"] else 0

    sub_size = sum(s["file_size_bytes"] for s in channels["subagents"])
    sub_msgs = sum(s["messages"] for s in channels["subagents"])
    sub_reminders = sum(s["system_reminders"] for s in channels["subagents"])

    side_size = sum(s["file_size_bytes"] for s in channels["sidechains"])
    side_msgs = sum(s["messages"] for s in channels["sidechains"])
    side_reminders = sum(s["system_reminders"] for s in channels["sidechains"])

    compact_size = sum(s["file_size_bytes"] for s in channels["compactions"])
    compact_msgs = sum(s["messages"] for s in channels["compactions"])
    compact_reminders = sum(s["system_reminders"] for s in channels["compactions"])

    # Companion traffic is modeled as mirroring the primary transcript.
    # This is an estimate, not a direct network measurement.
    companion_size = primary_size

    visible_size = primary_size
    hidden_size = companion_size + sub_size + side_size + compact_size
    total_size = visible_size + hidden_size

    return {
        "primary_size_kb": round(primary_size / 1024, 1),
        "primary_messages": primary_msgs,
        "companion_size_kb": round(companion_size / 1024, 1),
        "subagent_count": len(channels["subagents"]),
        "subagent_size_kb": round(sub_size / 1024, 1),
        "subagent_messages": sub_msgs,
        "sidechain_count": len(channels["sidechains"]),
        "sidechain_size_kb": round(side_size / 1024, 1),
        "sidechain_messages": side_msgs,
        "compaction_count": len(channels["compactions"]),
        "compaction_size_kb": round(compact_size / 1024, 1),
        "compaction_messages": compact_msgs,
        "visible_size_kb": round(visible_size / 1024, 1),
        "hidden_size_kb": round(hidden_size / 1024, 1),
        "total_size_kb": round(total_size / 1024, 1),
        "multiplier": round(total_size / max(visible_size, 1), 1),
        "transmissions": 1 + len(channels["subagents"]) + len(channels["sidechains"]) + len(channels["compactions"]) + 1,
        "system_reminders": primary_reminders + sub_reminders + side_reminders + compact_reminders,
        "hidden_data_pct": round(hidden_size / max(total_size, 1) * 100, 1),
        "model": channels["primary"]["model"] if channels["primary"] else "unknown",
    }


def find_project_name(path):
    name = path.name
    for prefix in ["-Users-", "-home-"]:
        if prefix in name:
            parts = name.split("-")
            try:
                idx = [i for i, p in enumerate(parts) if p.lower() == "code"]
                if idx:
                    return "-".join(parts[idx[-1] + 1 :])
            except Exception:
                pass
    return name


def analyze_all_projects():
    proj_dir = CLAUDE_DIR / "projects"
    if not proj_dir.exists():
        return []

    results = []
    for project in _safe_entries(proj_dir):
        if not project.is_dir():
            continue
        name = find_project_name(project)

        for session in _safe_entries(project):
            if not session.is_dir() or session.name == "memory":
                continue
            channels = analyze_all_channels(session)
            metrics = compute_session(channels)
            metrics["project"] = name
            metrics["session_id"] = session.name[:8]
            results.append(metrics)

    return results


def summary():
    results = analyze_all_projects()

    total_visible = sum(r["visible_size_kb"] for r in results)
    total_hidden = sum(r["hidden_size_kb"] for r in results)
    total_all = sum(r["total_size_kb"] for r in results)
    total_transmissions = sum(r["transmissions"] for r in results)
    # get reminder count from db (db.py finds them reliably, file scanning doesn't)
    try:
        from . import db as _db
        total_reminders = _db.summary().get("system_reminders", 0)
    except Exception:
        total_reminders = sum(r["system_reminders"] for r in results)
    total_sidechains = sum(r["sidechain_count"] for r in results)
    total_subagents = sum(r["subagent_count"] for r in results)
    try:
        from . import db as _db
        rows = _db.query("SELECT COUNT(*) as c FROM hook_events WHERE event_type='post_compact'")
        total_compactions = rows[0]["c"] if rows else 0
    except Exception:
        total_compactions = sum(r["compaction_count"] for r in results)

    # for real token count, run /usage in Claude Code

    return {
        "sessions": results,
        "totals": {
            "sessions_analyzed": len(results),
            "visible_data_mb": round(total_visible / 1024, 2),
            "hidden_data_mb": round(total_hidden / 1024, 2),
            "total_data_mb": round(total_all / 1024, 2),
            "data_multiplier": round(total_all / max(total_visible, 1), 1),
            "hidden_data_pct": round(total_hidden / max(total_all, 1) * 100, 1),
            "transmissions": total_transmissions,
            "system_reminders": total_reminders,
            "sidechains": total_sidechains,
            "subagents": total_subagents,
            "compactions": total_compactions,
            "data_source": "local_file_size_estimate",
            "note": "estimated from local transcript sizes and channel structure — token counts from /usage",
        },
    }


def main(argv=None) -> None:
    s = summary()
    t = s["totals"]
    print("NEO — DATA ACCOUNTING")
    print("=" * 50)
    print(f"Sessions analyzed:     {t['sessions_analyzed']}")
    print(f"Data on disk:")
    print(f"  Visible (primary):   {t['visible_data_mb']:.1f} MB")
    print(f"  Hidden (channels):   {t['hidden_data_mb']:.1f} MB")
    print(f"  Total:               {t['total_data_mb']:.1f} MB")
    print(f"  Estimated multiplier:{t['data_multiplier']}x")
    print(f"  Hidden %:            {t['hidden_data_pct']}%")
    print(f"Estimated API calls:   {t['transmissions']}")
    print(f"System reminders:      {t['system_reminders']}")
    print(f"Sidechains:            {t['sidechains']}")
    print(f"Subagents:             {t['subagents']}")
    print(f"Compactions:           {t['compactions']}")
    print()
    print("For real token count:  run /usage in Claude Code")
    print()
    print("Per session (top 10 by data volume):")
    for r in sorted(s["sessions"], key=lambda x: x["total_size_kb"], reverse=True)[:10]:
        print(f"  {r['project']:25s} {r['session_id']}  {r['total_size_kb']:>8.0f}KB  {r['multiplier']}x  {r['transmissions']:>3} tx  {r['sidechain_count']} sides  {r['system_reminders']} reminders")


if __name__ == "__main__":
    main()
