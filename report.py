#!/usr/bin/env python3
"""Generate an offline usage dashboard from OpenCode's local SQLite database."""

import argparse
from collections import Counter
from datetime import date, datetime, time, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import urlsplit, urlunsplit


TOKEN_FIELDS = ("input", "output", "reasoning", "cache_read", "cache_write")
CHUNK_SIZE = 250


def json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def token_usage(message):
    tokens = message.get("tokens") or {}
    cache = tokens.get("cache") or {}
    usage = {name: tokens.get(name, 0) or 0 for name in TOKEN_FIELDS[:3]}
    usage.update(cache_read=cache.get("read", 0) or 0, cache_write=cache.get("write", 0) or 0)
    usage["total"] = sum(usage.values())
    return usage


def report_window(args, generated_at):
    if args.days is not None and (args.date_from or args.date_to):
        raise ValueError("Use --days or --from/--to, not both.")
    if args.date_to and not args.date_from:
        raise ValueError("--to requires --from.")
    if args.date_from:
        start = datetime.combine(date.fromisoformat(args.date_from), time.min).astimezone()
        if args.date_to:
            last_day = date.fromisoformat(args.date_to)
            end = datetime.combine(last_day + timedelta(days=1), time.min).astimezone()
            end = min(end, generated_at)
        else:
            end = generated_at
    else:
        days = args.days if args.days is not None else 30
        if days <= 0:
            raise ValueError("--days must be greater than zero.")
        end = generated_at
        start = datetime.fromtimestamp(end.timestamp() - days * 86400).astimezone()
    if start >= end:
        raise ValueError("The report start must be before its end.")
    return start, end


def empty_session(row, projects):
    project = projects.get(row["project_id"], {})
    project_path = project.get("worktree")
    if not project_path or project_path == "/":
        project_path = row["directory"]
    project_name = project.get("name") or Path(project_path).name or project_path
    title = row["title"]
    automatic_type = None
    for kind in ("user", "project"):
        if title.startswith(f"[memory-review:{kind}:"):
            automatic_type = kind
    return {
        "id": row["id"],
        "parent_id": row["parent_id"],
        "title": title,
        "directory": row["directory"],
        "project": project_path,
        "project_name": project_name,
        "created": row["time_created"],
        "automatic_type": automatic_type,
        "in_window": False,
        "first_activity": None,
        "last_activity": None,
        "metrics": Counter(),
        "daily": {},
        "models": {},
        "tools": {},
        "sources": {},
    }


def add_usage(session, day, model, values):
    session["metrics"].update(values)
    session["daily"].setdefault(day, Counter()).update(values)
    if model:
        session["models"].setdefault(model, Counter()).update(values)


def source_reference(tool, arguments):
    if tool == "read":
        return arguments.get("filePath") or arguments.get("path")
    if tool == "skill":
        return arguments.get("name")
    if tool == "webfetch" and arguments.get("url"):
        try:
            url = urlsplit(arguments["url"])
            host = url.hostname or ""
            if ":" in host:
                host = f"[{host}]"
            if url.port:
                host += f":{url.port}"
            return urlunsplit((url.scheme, host, url.path, "", ""))
        except ValueError:
            return "Unparseable URL"
    return None


def collect_report(connection, start, end, generated_at, database):
    projects = {row["id"]: dict(row) for row in connection.execute("SELECT id, name, worktree FROM project")}
    sessions = {
        row["id"]: empty_session(row, projects)
        for row in connection.execute(
            "SELECT id, parent_id, title, directory, project_id, time_created FROM session"
        )
    }
    warnings = []
    for session in sessions.values():
        ancestor = session
        visited = {session["id"]}
        while ancestor["parent_id"] in sessions:
            parent_id = ancestor["parent_id"]
            if parent_id in visited:
                raise ValueError(f"Cycle in session parent links: {session['id']}")
            visited.add(parent_id)
            ancestor = sessions[parent_id]
        session["root_id"] = ancestor["id"]

    first_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    # Oldest session owns copied assistant history. Do this before processing parts.
    rows = connection.execute(
        "SELECT m.id, m.session_id, m.time_created, m.data "
        "FROM message m JOIN session s ON s.id = m.session_id "
        "WHERE m.time_created >= ? AND m.time_created < ? "
        "ORDER BY s.time_created, s.id, m.time_created, m.id",
        (first_ms, end_ms),
    )
    fingerprints = set()
    messages = {}
    mismatched_totals = 0
    for row in rows:
        session = sessions[row["session_id"]]
        session["in_window"] = True
        created = row["time_created"]
        session["first_activity"] = min(session["first_activity"] or created, created)
        session["last_activity"] = max(session["last_activity"] or created, created)
        message = json.loads(row["data"])
        day = datetime.fromtimestamp(created / 1000).strftime("%Y-%m-%d")
        assistant = message.get("role") == "assistant"
        model = None
        usage = token_usage(message) if assistant else {}
        if assistant:
            model = f"{message.get('providerID') or 'unknown'}/{message.get('modelID') or 'unknown'}"
            if usage["total"]:
                identity = {key: value for key, value in message.items() if key not in ("id", "sessionID", "parentID")}
                fingerprint = hashlib.sha256(
                    json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
                ).digest()
                if fingerprint in fingerprints:
                    add_usage(session, day, None, {"duplicate_messages": 1, "duplicate_tokens": usage["total"]})
                    continue
                fingerprints.add(fingerprint)
            reported_total = (message.get("tokens") or {}).get("total")
            mismatched_totals += reported_total is not None and reported_total != usage["total"]
            usage["responses"] = 1
            usage["missing_usage"] = int(not message.get("tokens"))
            usage["pending_responses"] = int(not (message.get("time") or {}).get("completed") and not message.get("error"))
            usage["response_errors"] = int(bool(message.get("error")))
            cost = message.get("cost") or 0
            usage["reported_cost"] = cost if cost > 0 else 0
            usage["cost_reported_responses"] = int(cost > 0)
            add_usage(session, day, model, usage)
        messages[row["id"]] = {
            "session": session,
            "created": created,
            "day": day,
            "model": model,
            "tokens": usage.get("total", 0),
            "assistant": assistant,
        }
    if mismatched_totals:
        warnings.append(f"{mismatched_totals} responses have a reported total different from the sum of token components. This report uses the component sum.")

    print(f"Reading tool records for {len(messages):,} messages…", file=sys.stderr)
    ordered_messages = sorted(messages, key=lambda mid: (messages[mid]["created"], mid))
    epochs = Counter()
    seen_outputs = {}
    tool_turns = set()
    for offset in range(0, len(ordered_messages), CHUNK_SIZE):
        batch = ordered_messages[offset:offset + CHUNK_SIZE]
        placeholders = ",".join("?" for _ in batch)
        parts = connection.execute(
            f"SELECT id, message_id, time_created, data FROM part WHERE message_id IN ({placeholders}) "
            "AND json_extract(data, '$.type') IN ('tool', 'compaction')",
            batch,
        )
        ordered_parts = sorted(parts, key=lambda row: (messages[row["message_id"]]["created"], row["time_created"], row["id"]))
        for row in ordered_parts:
            message = messages[row["message_id"]]
            session = message["session"]
            sid = session["id"]
            part = json.loads(row["data"])
            if part["type"] == "compaction":
                epochs[sid] += 1
                add_usage(session, message["day"], None, {"compactions": 1})
                continue
            # Tool records attached to non-assistant messages are not model tool calls.
            if not message["assistant"]:
                continue
            tool = part.get("tool") or "unknown"
            state = part.get("state") or {}
            arguments = state.get("input") or {}
            output = state.get("output") or state.get("error") or ""
            if not isinstance(output, str):
                output = json_text(output)
            input_chars = len(json_text(arguments))
            output_chars = len(output)
            timing = state.get("time") or {}
            duration = None
            if timing.get("start") is not None and timing.get("end") is not None:
                duration = max(0, timing["end"] - timing["start"])
            values = {
                "calls": 1,
                "errors": int(state.get("status") == "error"),
                "unfinished": int(state.get("status") not in ("completed", "error")),
                "truncated": int(bool((state.get("metadata") or {}).get("truncated"))),
                "input_chars": input_chars,
                "output_chars": output_chars,
                "payload_chars": input_chars + output_chars,
                "duration_ms": duration or 0,
                "timed_calls": int(duration is not None),
            }
            source = source_reference(tool, arguments) if isinstance(arguments, dict) else None
            if source:
                source_key = json_text([tool, source])
                source_totals = session["sources"].setdefault(source_key, {"tool": tool, "source": source, "metrics": Counter()})
                if state.get("status") == "completed":
                    output_hash = hashlib.sha256(output.encode("utf-8")).digest()
                    # URL query strings are excluded from exported references, not from identity.
                    identity = arguments.get("url") if tool == "webfetch" else source
                    key = (sid, tool, identity, output_hash)
                    previous = seen_outputs.get(key)
                    repeat = previous is not None
                    available = repeat and previous["epoch"] == epochs[sid] and not (
                        previous["compacted"] and previous["compacted"] <= row["time_created"]
                    )
                    values["identical_repeats"] = int(repeat)
                    values["repeat_candidates"] = int(available)
                    values["repeat_candidate_chars"] = output_chars if available else 0
                    seen_outputs[key] = {"epoch": epochs[sid], "compacted": timing.get("compacted")}
                source_totals["metrics"].update(values)
            session["tools"].setdefault(tool, Counter()).update(values)
            add_usage(session, message["day"], message["model"], {
                "tool_calls": 1,
                "tool_errors": values["errors"],
                "tool_unfinished": values["unfinished"],
                "tool_input_chars": input_chars,
                "tool_output_chars": output_chars,
                "repeat_candidates": values.get("repeat_candidates", 0),
                "repeat_candidate_chars": values.get("repeat_candidate_chars", 0),
            })
            if row["message_id"] not in tool_turns:
                tool_turns.add(row["message_id"])
                add_usage(session, message["day"], message["model"], {"tool_turn_tokens": message["tokens"]})

    selected_ids = {sid for sid, session in sessions.items() if session["in_window"]}
    for sid in list(selected_ids):
        parent = sessions[sid]["parent_id"]
        while parent in sessions:
            selected_ids.add(parent)
            parent = sessions[parent]["parent_id"]
    selected_sessions = [sessions[sid] for sid in sorted(selected_ids)]
    summary = Counter()
    for session in selected_sessions:
        summary.update(session["metrics"])
        session["sources"] = list(session["sources"].values())
        for field in ("created", "first_activity", "last_activity"):
            timestamp = session[field]
            session[field + "_iso"] = datetime.fromtimestamp(timestamp / 1000).astimezone().isoformat(timespec="seconds") if timestamp is not None else None
    if summary["missing_usage"]:
        warnings.append(f"{summary['missing_usage']} assistant messages have no recorded token usage.")
    return {
        "schema_version": 1,
        "meta": {
            "generated_at": generated_at.isoformat(timespec="seconds"),
            "start": start.isoformat(timespec="seconds"),
            "end_exclusive": end.isoformat(timespec="seconds"),
            "timezone": generated_at.tzname(),
            "database": str(database),
            "warnings": warnings,
            "accounting": {
                "total": "input + output + reasoning + cache_read + cache_write",
                "period": "Message creation timestamps in [start, end); daily buckets use the generator machine's local timezone.",
                "forks": "Positive-usage assistant records with identical metadata, excluding message/session/parent IDs, count once. The oldest session owns the usage. Their copied tool records are excluded too.",
                "tools": "Arguments and results are measured in Unicode characters, not tokens. Arguments use compact JSON serialization.",
                "repeats": "Identical successful output for the same source and session, since the last recorded compaction, with no recorded pruning before the repeat. Candidates for review, not proven wasted calls. Detection is limited to the report window.",
                "cost": "Only positive provider-reported costs are summed. Zero/missing values are not treated as evidence of free usage; reported amounts may be partial.",
                "sources": "Source references only; URL credentials, query strings and fragments are omitted. Prompt text, tool arguments and tool output bodies are not exported.",
            },
        },
        "summary": dict(summary),
        "sessions": selected_sessions,
    }


def main():
    default_database = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "opencode/opencode.db"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, help="Rolling number of 24-hour days (default: 30).")
    parser.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD", help="First local calendar date, inclusive.")
    parser.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", help="Last local calendar date, inclusive; capped at generation time.")
    parser.add_argument("--db", type=Path, default=default_database, help="OpenCode database path.")
    parser.add_argument("--out", type=Path, default=Path("report"), help="Output directory (default: ./report).")
    args = parser.parse_args()
    database = args.db.expanduser().resolve()
    try:
        if not database.is_file():
            raise ValueError(f"Database not found: {database}")
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
            generated_at = datetime.now().astimezone()
            start, end = report_window(args, generated_at)
            print(f"Reporting {start.isoformat(timespec='seconds')} → {end.isoformat(timespec='seconds')}", file=sys.stderr)
            report = collect_report(connection, start, end, generated_at, database)
        template = Path(__file__).with_name("template.html").read_text(encoding="utf-8")
        serialized = json_text(report)
        embedded = serialized.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
        html = template.replace("__REPORT_DATA__", embedded)
        output_dir = args.out.expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        json_path = output_dir / "usage.json"
        html_path = output_dir / "usage.html"
        json_path.write_text(serialized + "\n", encoding="utf-8")
        html_path.write_text(html, encoding="utf-8")
        print(f"Tokens: {report['summary'].get('total', 0):,}")
        print(f"Dashboard: {html_path}")
        print(f"Data:      {json_path}")
        print(f"Open:      {html_path.as_uri()}")
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
