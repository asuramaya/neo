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


# Per-file parse cache keyed by (path, mtime_ns, size). Transcripts are large
# and mostly append-only, so re-parsing every file on each dashboard load is the
# dominant cost. The dashboard server is long-lived, so this memo turns repeat
# scans from seconds into milliseconds; a changed file (new mtime/size) misses
# and is re-parsed.
_FILE_STATS_CACHE = {}


def analyze_session_file(filepath):
    """Analyze a single JSONL transcript for structure, not fake token counts."""
    cache_key = None
    try:
        st = filepath.stat()
        cache_key = (str(filepath), st.st_mtime_ns, st.st_size)
    except OSError:
        pass

    if cache_key is not None:
        cached = _FILE_STATS_CACHE.get(cache_key)
        if cached is not None:
            return dict(cached)

    stats = {
        "file": str(filepath),
        "file_size_bytes": 0,
        "messages": 0,
        "user_messages": 0,
        "assistant_messages": 0,
        "system_messages": 0,
        "tool_results": 0,
        "api_calls_with_usage": 0,
        # Token kinds kept distinct so cache is first-class, never bundled into
        # a single "input" figure. fresh input vs cache-creation vs cache-read
        # have very different cost and meaning.
        "reported_input_tokens": 0,          # fresh, uncached input
        "reported_cache_creation_tokens": 0,  # written to cache (billed ~1.25x)
        "reported_cache_read_tokens": 0,      # re-sent from cache (billed ~0.1x)
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
                        stats["reported_input_tokens"] += inp
                        stats["reported_cache_creation_tokens"] += cache_create
                        stats["reported_cache_read_tokens"] += cache_read
                        stats["reported_output_tokens"] += out
                elif msg_type == "system":
                    stats["system_messages"] += 1
                elif msg_type == "tool_result":
                    stats["tool_results"] += 1

    except Exception as e:
        stats["error"] = str(e)

    stats["has_real_usage"] = stats["api_calls_with_usage"] > 0

    if cache_key is not None and "error" not in stats:
        # Drop any stale entries for this path (older mtime/size) before caching.
        path_str = cache_key[0]
        for key in [k for k in _FILE_STATS_CACHE if k[0] == path_str]:
            del _FILE_STATS_CACHE[key]
        _FILE_STATS_CACHE[cache_key] = dict(stats)

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
                                   "reported_output_tokens": 0, "reported_cache_read_tokens": 0,
                                   "has_real_usage": False,
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


def _channel_tokens(stats_list):
    """Sum real reported token usage across a list of channel stats.

    Returns token kinds kept separate. cache_read is re-sent cached context
    (cheap, repeated); fresh is everything actually computed this turn.
    """
    inp = sum(s.get("reported_input_tokens", 0) for s in stats_list)
    cache_creation = sum(s.get("reported_cache_creation_tokens", 0) for s in stats_list)
    cache_read = sum(s.get("reported_cache_read_tokens", 0) for s in stats_list)
    out = sum(s.get("reported_output_tokens", 0) for s in stats_list)
    calls = sum(s.get("api_calls_with_usage", 0) for s in stats_list)
    return {
        "input": inp,
        "cache_creation": cache_creation,
        "cache_read": cache_read,
        "output": out,
        "fresh": inp + cache_creation + out,
        "total": inp + cache_creation + cache_read + out,
        "calls": calls,
    }


def compute_session(channels):
    """Compute metrics for one session from channel data.

    Two parallel accountings are produced:
      - tokens: real input+output usage reported by the API in each transcript.
        This is the accurate measure and drives the headline multiplier / hidden %
        whenever any channel reported usage.
      - bytes: on-disk transcript size, a fallback proxy used only when no usage
        was recorded. No companion channel is fabricated (it is not locally
        measurable).
    """
    primary = [channels["primary"]] if channels["primary"] else []
    subs = channels["subagents"]
    sides = channels["sidechains"]
    compacts = channels["compactions"]
    hidden_channels = subs + sides + compacts

    primary_size = channels["primary"]["file_size_bytes"] if channels["primary"] else 0
    primary_msgs = channels["primary"]["messages"] if channels["primary"] else 0
    primary_reminders = channels["primary"]["system_reminders"] if channels["primary"] else 0

    sub_size = sum(s["file_size_bytes"] for s in subs)
    sub_msgs = sum(s["messages"] for s in subs)
    sub_reminders = sum(s["system_reminders"] for s in subs)

    side_size = sum(s["file_size_bytes"] for s in sides)
    side_msgs = sum(s["messages"] for s in sides)
    side_reminders = sum(s["system_reminders"] for s in sides)

    compact_size = sum(s["file_size_bytes"] for s in compacts)
    compact_msgs = sum(s["messages"] for s in compacts)
    compact_reminders = sum(s["system_reminders"] for s in compacts)

    visible_size = primary_size
    hidden_size = sub_size + side_size + compact_size
    total_size = visible_size + hidden_size

    # Real token accounting, with cache kept first-class (never folded into a
    # single "input" number).
    vis = _channel_tokens(primary)
    hid = _channel_tokens(hidden_channels)
    visible_tokens = vis["total"]
    hidden_tokens = hid["total"]
    total_tokens = visible_tokens + hidden_tokens
    fresh_tokens = vis["fresh"] + hid["fresh"]
    cache_read_tokens = vis["cache_read"] + hid["cache_read"]
    api_calls = vis["calls"] + hid["calls"]
    has_usage = total_tokens > 0
    cache_read_pct = round(cache_read_tokens / max(total_tokens, 1) * 100, 1)

    if has_usage:
        basis = "tokens"
        multiplier = round(total_tokens / max(visible_tokens, 1), 1)
        hidden_pct = round(hidden_tokens / max(total_tokens, 1) * 100, 1)
        transmissions = api_calls
    else:
        basis = "file_size_estimate"
        multiplier = round(total_size / max(visible_size, 1), 1)
        hidden_pct = round(hidden_size / max(total_size, 1) * 100, 1)
        # One transmission per channel transcript present (rough proxy).
        transmissions = len(primary) + len(hidden_channels)

    return {
        "primary_size_kb": round(primary_size / 1024, 1),
        "primary_messages": primary_msgs,
        "subagent_count": len(subs),
        "subagent_size_kb": round(sub_size / 1024, 1),
        "subagent_messages": sub_msgs,
        "sidechain_count": len(sides),
        "sidechain_size_kb": round(side_size / 1024, 1),
        "sidechain_messages": side_msgs,
        "compaction_count": len(compacts),
        "compaction_size_kb": round(compact_size / 1024, 1),
        "compaction_messages": compact_msgs,
        "visible_size_kb": round(visible_size / 1024, 1),
        "hidden_size_kb": round(hidden_size / 1024, 1),
        "total_size_kb": round(total_size / 1024, 1),
        "visible_tokens": visible_tokens,
        "hidden_tokens": hidden_tokens,
        "total_tokens": total_tokens,
        "fresh_tokens": fresh_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_read_pct": cache_read_pct,
        "visible_fresh_tokens": vis["fresh"],
        "visible_cache_read_tokens": vis["cache_read"],
        "hidden_fresh_tokens": hid["fresh"],
        "hidden_cache_read_tokens": hid["cache_read"],
        "api_calls": api_calls,
        "has_real_usage": has_usage,
        "basis": basis,
        "multiplier": multiplier,
        "transmissions": transmissions,
        "system_reminders": primary_reminders + sub_reminders + side_reminders + compact_reminders,
        "hidden_data_pct": hidden_pct,
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

    # Real token accounting aggregated across sessions, cache kept first-class.
    tok_visible = sum(r["visible_tokens"] for r in results)
    tok_hidden = sum(r["hidden_tokens"] for r in results)
    tok_total = tok_visible + tok_hidden
    tok_fresh = sum(r["fresh_tokens"] for r in results)
    tok_cache_read = sum(r["cache_read_tokens"] for r in results)
    tok_visible_fresh = sum(r["visible_fresh_tokens"] for r in results)
    tok_visible_cache = sum(r["visible_cache_read_tokens"] for r in results)
    tok_hidden_fresh = sum(r["hidden_fresh_tokens"] for r in results)
    tok_hidden_cache = sum(r["hidden_cache_read_tokens"] for r in results)
    tok_cache_pct = round(tok_cache_read / max(tok_total, 1) * 100, 1)
    sessions_with_usage = sum(1 for r in results if r["has_real_usage"])
    has_usage = tok_total > 0

    if has_usage:
        basis = "tokens"
        data_multiplier = round(tok_total / max(tok_visible, 1), 1)
        hidden_pct = round(tok_hidden / max(tok_total, 1) * 100, 1)
    else:
        basis = "file_size_estimate"
        data_multiplier = round(total_all / max(total_visible, 1), 1)
        hidden_pct = round(total_hidden / max(total_all, 1) * 100, 1)
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
            "sessions_with_usage": sessions_with_usage,
            "visible_data_mb": round(total_visible / 1024, 2),
            "hidden_data_mb": round(total_hidden / 1024, 2),
            "total_data_mb": round(total_all / 1024, 2),
            "visible_tokens": tok_visible,
            "hidden_tokens": tok_hidden,
            "total_tokens": tok_total,
            "fresh_tokens": tok_fresh,
            "cache_read_tokens": tok_cache_read,
            "cache_read_pct": tok_cache_pct,
            "visible_fresh_tokens": tok_visible_fresh,
            "visible_cache_read_tokens": tok_visible_cache,
            "hidden_fresh_tokens": tok_hidden_fresh,
            "hidden_cache_read_tokens": tok_hidden_cache,
            "data_multiplier": data_multiplier,
            "hidden_data_pct": hidden_pct,
            "transmissions": total_transmissions,
            "system_reminders": total_reminders,
            "sidechains": total_sidechains,
            "subagents": total_subagents,
            "compactions": total_compactions,
            "basis": basis,
            "data_source": "reported_token_usage" if has_usage else "local_file_size_estimate",
            "note": (
                "multiplier and hidden % computed from API-reported token usage; "
                "cache reads are reported separately, not folded into input. MB "
                "figures are on-disk transcript bytes. No companion channel is "
                "fabricated. For billable totals use /usage in Claude Code."
                if has_usage else
                "no token usage found in transcripts — multiplier and hidden % "
                "fall back to on-disk byte sizes (companion channel not fabricated)."
            ),
        },
    }


def _fmt_tokens(n):
    n = n or 0
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(int(n))


def main(argv=None) -> None:
    s = summary()
    t = s["totals"]
    tok = bool(t["total_tokens"])

    def row(label, value):
        print(f"  {label:<13}{value}")

    print("NEO — DATA ACCOUNTING")
    print("─" * 52)
    row("sessions", f"{t['sessions_analyzed']} analyzed · {t['sessions_with_usage']} with token usage")
    row("basis", f"{t['basis']} (multiplier & hidden % from {'API usage' if tok else 'on-disk bytes'})")
    print()
    row("disk", f"{t['total_data_mb']:.1f} MB total · visible {t['visible_data_mb']:.1f} · hidden {t['hidden_data_mb']:.1f}")
    if tok:
        row("tokens", f"{_fmt_tokens(t['total_tokens'])} total · fresh {_fmt_tokens(t['fresh_tokens'])} · cache-read {_fmt_tokens(t['cache_read_tokens'])} ({t['cache_read_pct']}%)")
        row("", f"visible {_fmt_tokens(t['visible_tokens'])} (fresh {_fmt_tokens(t['visible_fresh_tokens'])} / cache {_fmt_tokens(t['visible_cache_read_tokens'])})")
        row("", f"hidden  {_fmt_tokens(t['hidden_tokens'])} (fresh {_fmt_tokens(t['hidden_fresh_tokens'])} / cache {_fmt_tokens(t['hidden_cache_read_tokens'])})")
    row("hidden share", f"{t['hidden_data_pct']}% · multiplier {t['data_multiplier']}x · API calls {t['transmissions']:,}")
    print()
    row("channels", f"{t['subagents']} subagents · {t['sidechains']} sidechains · {t['compactions']} compactions")
    row("reminders", f"{t['system_reminders']} system-reminder injections")
    print()

    key = "total_tokens" if tok else "total_size_kb"
    print(f"top sessions by {'tokens' if tok else 'disk size'}")
    for r in sorted(s["sessions"], key=lambda x: x.get(key, 0), reverse=True)[:10]:
        if tok:
            metric = f"{_fmt_tokens(r['total_tokens']):>7} · {r['hidden_data_pct']:>4}% hidden · {r['cache_read_pct']:>4}% cache · {r['api_calls']:,} calls"
        else:
            metric = f"{r['total_size_kb']:>8.0f}KB · {r['multiplier']}x · {r['transmissions']} tx"
        print(f"  {r['project'][:22]:<22} {r['session_id']:<9} {metric}")
    print()
    print("note: MB = on-disk transcript bytes; tokens = API-reported usage.")
    print("      run /usage in Claude Code for billable totals.")


if __name__ == "__main__":
    main()
