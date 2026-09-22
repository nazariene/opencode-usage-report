# OpenCode usage

Generates an offline dashboard of OpenCode token usage and session activity from
a read-only snapshot of the local SQLite database. Includes searchable sessions,
project/model breakdowns, daily charts and session drill-downs.

## Usage

Requires **Python 3.9+**, `sqlite3` with SQLite JSON support, and a modern browser.
No third-party Python packages; `requirements.txt` contains only comments.

```bash
# Last 30 days (default)
python3 report.py --days 30 --out ./report

# Inclusive local calendar dates
python3 report.py --from 2026-08-01 --to 2026-08-31 --out ./report
```

Open **`report/usage.html`** in a browser. Regenerate and reload to refresh.
`report/usage.json` contains the same aggregates; the dashboard also exports CSV.

The default database is `~/.local/share/opencode/opencode.db`, respecting
`XDG_DATA_HOME`. Override it with `--db /path/to/opencode.db`.
Omit `--to` to report through generation time. The exact period and timezone appear
in the dashboard; usage is filtered by **message creation time**.

## Captured data

- **Tokens:** uncached input, output, reasoning, cache reads/writes and daily totals.
- **Sessions:** titles, IDs, projects, paths, activity dates, models, response counts
  and compactions. Subagents roll into parents by default; automated memory reviews
  appear in expandable groups.
- **Tools:** calls, errors, average duration, argument/result character counts,
  file/URL/skill references and repeated-read candidates.
- **Cost:** positive provider-reported amounts; missing coverage is marked partial
  or unavailable.

Copied fork usage is deduplicated; subagents are counted once. Tool text volume is
measured in **characters**, not billed tokens. Tool-turn tokens include the entire
model context, and repeated-read flags indicate candidates rather than proven waste.
Further accounting details are available inside the dashboard.

**Generated reports contain private metadata**, including titles, paths and source
URLs. They omit conversation/tool bodies and strip URL credentials, queries and
fragments. The HTML embeds the full dataset, even when filters hide rows.
`report/` is gitignored; custom output directories must be excluded separately.
