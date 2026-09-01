#!/usr/bin/env python3
"""ops/cost-report.py — read-only slices over the per-stage cost ledger.

`dispatcher/cost_ledger.py` appends one row per finished stage to the SQLite
ledger at ``~/.ai-delivery/cost.db`` (``COST_LEDGER_PATH``): task, stage,
backend, the honest cost, token counts including cache, the source label
(``cli`` / ``computed:<model>`` / ``cli-no-price-table:<backend>``), elapsed
time and session id. Its docstring promised "a future /cost-report command" —
this is that command, minus the Telegram surface.

The report exists to answer the questions the DeepSeek-first plan's step 4
parks on real data rather than on impressions:

  1. Which stage is the money? In particular: what share of a task does the
     reviewer actually take (the plan's "reviewer ~40%?" question).
  2. What did a given task cost, stage by stage, and at which tier?
  3. What would DeepSeek's off-peak half-rate mean for what we already ran?
     The price override holds PEAK rates as the honest upper bound, so every
     off-peak row in the ledger is priced high on purpose — this slice
     separates the two so the calibration is a reading, not a guess.

Output is markdown tables on stdout, meant to be pasted into STATE/ notes;
``--csv DIR`` writes the same three slices as CSV instead.

The database is opened READ-ONLY (``file:...?mode=ro``): this tool issues no
writes, and sqlite enforces that rather than trusting the code.

Usage:
  ops/cost-report.py                          last 7 days from the default db
  ops/cost-report.py --since 2026-08-10       explicit window (UTC)
  ops/cost-report.py --db /path/to/cost.db    a copy of the live ledger
  ops/cost-report.py --task bot-children-reaping   one task, stage by stage
  ops/cost-report.py --csv reports/           write CSVs instead of markdown
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DB = Path(os.environ.get(
    "COST_LEDGER_PATH", str(Path.home() / ".ai-delivery" / "cost.db")))

# DeepSeek's peak windows in UTC, from the 2026-08-17 price note in
# STATE/PLAN-2026-08-15-deepseek-first-parallel.md (off-peak is half of peak).
# Data, not logic — override with --peak-windows when the provider moves them.
DEFAULT_PEAK_WINDOWS = "01:00-04:00,06:00-10:00"

DEFAULT_WINDOW_DAYS = 7


# ── input helpers ────────────────────────────────────────────────────────────

def _parse_when(value: str, *, end: bool) -> str:
    """A --since/--until value as an ISO-8601 UTC string comparable to ts.

    Accepts a bare date (2026-08-10) or a full timestamp. A bare date means the
    START of that day for --since and the END of it for --until, so
    `--since X --until X` covers exactly day X instead of nothing.
    """
    raw = value.strip()
    try:
        if len(raw) == 10:
            day = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if end:
                day += timedelta(days=1) - timedelta(seconds=1)
            return day.isoformat(timespec="seconds")
        stamp = datetime.fromisoformat(raw)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        raise SystemExit(f"error: cannot read '{value}' as a date or timestamp")


def _parse_peak_windows(spec: str) -> list[tuple[int, int]]:
    """"01:00-04:00,06:00-10:00" → [(60, 240), (360, 600)] in minutes-of-day.
    A window whose end precedes its start wraps past midnight."""
    windows: list[tuple[int, int]] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            start_s, end_s = chunk.split("-", 1)
            sh, sm = (int(x) for x in start_s.split(":", 1))
            eh, em = (int(x) for x in end_s.split(":", 1))
        except ValueError:
            raise SystemExit(f"error: cannot read peak window '{chunk}' "
                             f"(expected HH:MM-HH:MM)")
        windows.append((sh * 60 + sm, eh * 60 + em))
    return windows


def _is_peak(ts: str, windows: list[tuple[int, int]]) -> bool | None:
    """True/False for a timestamp against the peak windows, None when the
    timestamp cannot be read (a malformed row must not be silently bucketed)."""
    try:
        stamp = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(timezone.utc)
    minute = stamp.hour * 60 + stamp.minute
    for start, end in windows:
        if start <= end:
            if start <= minute < end:
                return True
        elif minute >= start or minute < end:  # wraps past midnight
            return True
    return False


def _connect_ro(db: Path) -> sqlite3.Connection:
    if not db.is_file():
        raise SystemExit(
            f"error: no ledger at {db}\n"
            f"       point --db at a copy of the live one, or set "
            f"COST_LEDGER_PATH.")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _has_column(conn: sqlite3.Connection, column: str) -> bool:
    """Whether the ledger carries this column. This tool opens the database
    read-only and therefore cannot migrate it, so a ledger written before a
    column existed must still report — see cost_ledger._migrate for the writer
    side."""
    try:
        return any(row[1] == column
                   for row in conn.execute("PRAGMA table_info(cost_events)"))
    except sqlite3.Error:
        return False


def _fetch(conn: sqlite3.Connection, since: str, until: str,
           task: str | None) -> list[sqlite3.Row]:
    # A ledger older than the profile column still selects a `profile` of NULL,
    # so every slice below can read the field unconditionally.
    profile_col = "profile" if _has_column(conn, "profile") else "NULL AS profile"
    sql = ("SELECT ts, task_id, stage, backend, cost_usd, input_tokens, "
           "output_tokens, cache_read_tokens, cache_creation_tokens, source, "
           f"elapsed_sec, {profile_col} FROM cost_events WHERE ts >= ? AND ts <= ?")
    params: list[object] = [since, until]
    if task:
        sql += " AND task_id = ?"
        params.append(task)
    try:
        return list(conn.execute(sql + " ORDER BY ts", params))
    except sqlite3.Error as exc:
        raise SystemExit(f"error: cannot read the ledger: {exc}")


def _tier_of(task_id: str, tasks_dir: Path) -> str:
    """The task's triage tier, best-effort. The ledger is self-sufficient; the
    tier only lives in the task dir (``triage.json``, written by triage_wiring),
    which survives only until the bucket is cleaned. '-' when unavailable."""
    import json
    for bucket in sorted(tasks_dir.glob("*")):
        candidate = bucket / task_id / "triage.json"
        if candidate.is_file():
            try:
                return str(json.loads(candidate.read_text()).get("tier") or "-")
            except (OSError, ValueError):
                return "-"
    return "-"


# ── rendering ────────────────────────────────────────────────────────────────

def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_(no rows)_\n"
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    out = ["| " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)) + " |",
           "|" + "|".join("-" * (w + 2) for w in widths) + "|"]
    for row in rows:
        out.append("| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(row)) + " |")
    return "\n".join(out) + "\n"


def _usd(value: float) -> str:
    return f"${value:.4f}"


def _pct(part: float, whole: float) -> str:
    return f"{100.0 * part / whole:.1f}%" if whole else "—"


def _counted(values: dict[str, int]) -> str:
    """{'anthropic': 4, 'deepseek': 1} → 'anthropic×4, deepseek×1'."""
    return ", ".join(f"{k}×{v}" for k, v in
                     sorted(values.items(), key=lambda kv: (-kv[1], kv[0])))


# ── slices ───────────────────────────────────────────────────────────────────

def slice_by_stage(rows: list[sqlite3.Row]) -> tuple[list[str], list[list[str]], dict]:
    """Per-stage spend, and each stage's share — both of the window's total and
    of the average task. The two differ whenever tasks differ in size, and the
    per-task share is the one that answers 'is the reviewer 40% of a task?'."""
    total = sum(r["cost_usd"] for r in rows) or 0.0
    per_task_total: dict[str, float] = defaultdict(float)
    per_task_stage: dict[tuple[str, str], float] = defaultdict(float)
    agg: dict[str, dict] = defaultdict(
        lambda: {"runs": 0, "cost": 0.0, "tasks": set(),
                 "backends": defaultdict(int), "sources": defaultdict(int),
                 "elapsed": 0.0})
    for r in rows:
        stage, task = r["stage"], r["task_id"]
        a = agg[stage]
        a["runs"] += 1
        a["cost"] += r["cost_usd"]
        a["tasks"].add(task)
        a["backends"][r["backend"]] += 1
        a["sources"][r["source"]] += 1
        a["elapsed"] += float(r["elapsed_sec"] or 0.0)
        per_task_total[task] += r["cost_usd"]
        per_task_stage[(task, stage)] += r["cost_usd"]

    shares: dict[str, list[float]] = defaultdict(list)
    for (task, stage), cost in per_task_stage.items():
        task_total = per_task_total[task]
        if task_total:
            shares[stage].append(cost / task_total)

    headers = ["stage", "runs", "tasks", "total", "mean/run",
               "share of window", "share of a task (mean)", "mean min",
               "backends", "sources"]
    table: list[list[str]] = []
    mean_share: dict[str, float] = {}
    for stage, a in sorted(agg.items(), key=lambda kv: -kv[1]["cost"]):
        stage_shares = shares.get(stage) or []
        mean = sum(stage_shares) / len(stage_shares) if stage_shares else 0.0
        mean_share[stage] = mean
        table.append([
            stage, str(a["runs"]), str(len(a["tasks"])), _usd(a["cost"]),
            _usd(a["cost"] / a["runs"]), _pct(a["cost"], total),
            f"{100.0 * mean:.1f}%" + (f" (n={len(stage_shares)})" if stage_shares else ""),
            f"{a['elapsed'] / a['runs'] / 60:.1f}" if a["runs"] else "—",
            _counted(a["backends"]), _counted(a["sources"]),
        ])
    return headers, table, {"total": total, "mean_share": mean_share,
                            "tasks": len(per_task_total)}


def slice_by_task(rows: list[sqlite3.Row], tasks_dir: Path) -> tuple[list[str], list[list[str]]]:
    agg: dict[str, dict] = defaultdict(
        lambda: {"cost": 0.0, "stages": defaultdict(float), "runs": 0,
                 "backends": defaultdict(int), "first": None, "last": None})
    for r in rows:
        a = agg[r["task_id"]]
        a["cost"] += r["cost_usd"]
        a["stages"][r["stage"]] += r["cost_usd"]
        a["runs"] += 1
        a["backends"][r["backend"]] += 1
        a["first"] = min(a["first"] or r["ts"], r["ts"])
        a["last"] = max(a["last"] or r["ts"], r["ts"])

    headers = ["task", "tier", "runs", "total", "priciest stage",
               "backends", "first event", "last event"]
    table: list[list[str]] = []
    for task, a in sorted(agg.items(), key=lambda kv: -kv[1]["cost"]):
        top_stage, top_cost = max(a["stages"].items(), key=lambda kv: kv[1])
        table.append([
            task, _tier_of(task, tasks_dir), str(a["runs"]), _usd(a["cost"]),
            f"{top_stage} ({_pct(top_cost, a['cost'])})",
            _counted(a["backends"]), a["first"][:16], a["last"][:16],
        ])
    return headers, table


def slice_task_stages(rows: list[sqlite3.Row], task: str) -> tuple[list[str], list[list[str]]]:
    """Every event of one task, in order — the slice that gets compared against
    that task's worklog.md line by line."""
    total = sum(r["cost_usd"] for r in rows if r["task_id"] == task) or 0.0
    headers = ["ts", "stage", "backend", "cost", "share", "in tok", "out tok",
               "cache r/w", "min", "source"]
    table = []
    for r in rows:
        if r["task_id"] != task:
            continue
        table.append([
            r["ts"][:16], r["stage"], r["backend"], _usd(r["cost_usd"]),
            _pct(r["cost_usd"], total),
            f"{r['input_tokens'] or 0:,}", f"{r['output_tokens'] or 0:,}",
            f"{r['cache_read_tokens'] or 0:,}/{r['cache_creation_tokens'] or 0:,}",
            f"{float(r['elapsed_sec'] or 0.0) / 60:.1f}", r["source"],
        ])
    table.append(["**total**", "", "", _usd(total), "100.0%", "", "", "", "", ""])
    return headers, table


def slice_by_profile(rows: list[sqlite3.Row]) -> tuple[list[str], list[list[str]]]:
    """Spend per named key profile (T15) — which key actually paid.

    Empty for a single-key install: a row written without a profile, or by a
    ledger older than the column, reports under `(global key)` and that is the
    only row, so the caller skips the section entirely."""
    total = sum(r["cost_usd"] for r in rows) or 0.0
    agg: dict[str, dict] = defaultdict(
        lambda: {"runs": 0, "cost": 0.0, "tasks": set(), "backends": defaultdict(int)})
    for r in rows:
        key = r["profile"] or "(global key)"
        a = agg[key]
        a["runs"] += 1
        a["cost"] += r["cost_usd"]
        a["tasks"].add(r["task_id"])
        a["backends"][r["backend"]] += 1
    headers = ["profile", "runs", "tasks", "total", "mean/run", "share", "backends"]
    table = [[name, str(a["runs"]), str(len(a["tasks"])), _usd(a["cost"]),
              _usd(a["cost"] / a["runs"]), _pct(a["cost"], total),
              _counted(a["backends"])]
             for name, a in sorted(agg.items(), key=lambda kv: -kv[1]["cost"])]
    return headers, table


def slice_deepseek(rows: list[sqlite3.Row],
                   windows: list[tuple[int, int]]) -> tuple[list[str], list[list[str]], dict]:
    """Recomputed (``computed:<model>``) rows split peak / off-peak.

    Every row is priced at the PEAK rates the override carries, deliberately —
    the cap is a safety net, not a billing system. So the off-peak bucket is the
    overstatement: at a half off-peak rate its true cost is half of what the
    ledger says. Both numbers are printed; the verdict is the operator's."""
    agg: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"runs": 0, "cost": 0.0, "in": 0, "out": 0, "cr": 0, "cw": 0})
    for r in rows:
        source = r["source"] or ""
        if not source.startswith("computed:"):
            continue
        peak = _is_peak(r["ts"], windows)
        bucket = "peak" if peak else ("off-peak" if peak is False else "unknown ts")
        a = agg[(source.split(":", 1)[1], bucket)]
        a["runs"] += 1
        a["cost"] += r["cost_usd"]
        a["in"] += r["input_tokens"] or 0
        a["out"] += r["output_tokens"] or 0
        a["cr"] += r["cache_read_tokens"] or 0
        a["cw"] += r["cache_creation_tokens"] or 0

    headers = ["model", "window", "runs", "in tok", "out tok", "cache r/w",
               "cost as recorded", "at half rates"]
    table: list[list[str]] = []
    off_peak_cost = 0.0
    for (model, bucket), a in sorted(agg.items()):
        if bucket == "off-peak":
            off_peak_cost += a["cost"]
        table.append([
            model, bucket, str(a["runs"]), f"{a['in']:,}", f"{a['out']:,}",
            f"{a['cr']:,}/{a['cw']:,}", _usd(a["cost"]),
            _usd(a["cost"] / 2) if bucket == "off-peak" else "—",
        ])
    return headers, table, {"off_peak_cost": off_peak_cost}


# ── main ─────────────────────────────────────────────────────────────────────

def _write_csv(directory: Path, name: str, headers: list[str],
               table: list[list[str]]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers)
        writer.writerows(table)
    return path


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Read-only slices over the per-stage cost ledger.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="The ledger is opened read-only; this tool never writes to it.",
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB,
                    help=f"ledger path (default: {DEFAULT_DB})")
    ap.add_argument("--since", help="window start, UTC (default: 7 days ago)")
    ap.add_argument("--until", help="window end, UTC (default: now)")
    ap.add_argument("--task", help="restrict to one task and print its stages")
    ap.add_argument("--focus-stage", default="reviewer",
                    help="stage called out in the summary line (default: reviewer)")
    ap.add_argument("--peak-windows", default=DEFAULT_PEAK_WINDOWS,
                    help=f"UTC peak windows (default: {DEFAULT_PEAK_WINDOWS})")
    ap.add_argument("--tasks-dir", type=Path,
                    default=Path(__file__).resolve().parent.parent / "tasks",
                    help="where task dirs live, for the best-effort tier column")
    ap.add_argument("--csv", type=Path, metavar="DIR",
                    help="write the slices as CSV into DIR instead of markdown")
    args = ap.parse_args()

    until = _parse_when(args.until, end=True) if args.until else \
        datetime.now(timezone.utc).isoformat(timespec="seconds")
    since = _parse_when(args.since, end=False) if args.since else \
        (datetime.now(timezone.utc) - timedelta(days=DEFAULT_WINDOW_DAYS)
         ).isoformat(timespec="seconds")
    windows = _parse_peak_windows(args.peak_windows)

    conn = _connect_ro(args.db)
    try:
        rows = _fetch(conn, since, until, args.task)
    finally:
        conn.close()

    if not rows:
        print(f"No cost events in {args.db} between {since} and {until}"
              + (f" for task {args.task}" if args.task else "") + ".")
        return 0

    stage_headers, stage_table, stage_meta = slice_by_stage(rows)
    task_headers, task_table = slice_by_task(rows, args.tasks_dir)
    prof_headers, prof_table = slice_by_profile(rows)
    # One row means one key paid for everything — nothing to attribute.
    show_profiles = len(prof_table) > 1
    ds_headers, ds_table, ds_meta = slice_deepseek(rows, windows)

    if args.csv:
        written = [_write_csv(args.csv, "by-stage", stage_headers, stage_table),
                   _write_csv(args.csv, "by-task", task_headers, task_table),
                   *([_write_csv(args.csv, "by-profile", prof_headers, prof_table)]
                     if show_profiles else []),
                   _write_csv(args.csv, "deepseek-calibration", ds_headers, ds_table)]
        if args.task:
            written.append(_write_csv(args.csv, f"task-{args.task}",
                                      *slice_task_stages(rows, args.task)))
        for path in written:
            print(path)
        return 0

    total = stage_meta["total"]
    print(f"# Cost report — {since} … {until}\n")
    print(f"{len(rows)} stage events, {stage_meta['tasks']} task(s), "
          f"{_usd(total)} total. Ledger: `{args.db}` (read-only).\n")

    focus = args.focus_stage
    if focus in stage_meta["mean_share"]:
        print(f"**{focus}** takes {100.0 * stage_meta['mean_share'][focus]:.1f}% "
              f"of the average task in this window.\n")

    print("## By stage\n")
    print(_md_table(stage_headers, stage_table))
    print("\n## By task\n")
    print(_md_table(task_headers, task_table))

    if show_profiles:
        print("\n## By key profile\n")
        print(_md_table(prof_headers, prof_table))

    if args.task:
        print(f"\n## Stages of {args.task}\n")
        print(_md_table(*slice_task_stages(rows, args.task)))

    print(f"\n## DeepSeek calibration (peak windows {args.peak_windows} UTC)\n")
    if ds_table:
        print(_md_table(ds_headers, ds_table))
        if ds_meta["off_peak_cost"]:
            off = ds_meta["off_peak_cost"]
            print(f"\nOff-peak rows carry peak rates on purpose: {_usd(off)} "
                  f"recorded, {_usd(off / 2)} at a half rate — so this window "
                  f"overstates DeepSeek by {_usd(off / 2)}. Whether to split the "
                  f"override by window is the step-4 call this number is for.")
    else:
        print("_No recomputed (`computed:<model>`) rows in this window — "
              "nothing to calibrate._")
    return 0


if __name__ == "__main__":
    sys.exit(main())
