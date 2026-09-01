"""Append-only SQLite ledger of per-stage pipeline costs.

Survives `tasks/done/<id>` cleanup — once a stage finishes, its cost row
is permanent. Backed by a single sqlite3 file at ~/.ai-delivery/cost.db
(creates parent dir on first call).

Schema (created idempotently on first call):
    cost_events(
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ts              TEXT    NOT NULL,           -- ISO-8601 UTC
        task_id         TEXT    NOT NULL,
        stage           TEXT    NOT NULL,           -- ba|architect|...|reviewer|reviewer-agent-poc
        backend         TEXT    NOT NULL,           -- anthropic|deepseek|glm
        profile         TEXT,                       -- named key profile (T15); NULL = the global key
        cost_usd        REAL    NOT NULL,
        input_tokens    INTEGER,
        output_tokens   INTEGER,
        cache_read_tokens   INTEGER,
        cache_creation_tokens INTEGER,
        source          TEXT    NOT NULL,           -- 'cli' | 'computed:<model>' | 'cli-no-price-table:<backend>'
        elapsed_sec     REAL,
        session_id      TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_task ON cost_events(task_id);
    CREATE INDEX IF NOT EXISTS idx_ts ON cost_events(ts);

Public API: one function. Pure write — no read helpers in this module;
callers query the db directly via sqlite3 CLI or a future /cost-report
command.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("dispatcher.cost_ledger")

LEDGER_PATH = Path(os.environ.get(
    "COST_LEDGER_PATH",
    str(Path.home() / ".ai-delivery" / "cost.db"),
))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS cost_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL,
    task_id         TEXT    NOT NULL,
    stage           TEXT    NOT NULL,
    backend         TEXT    NOT NULL,
    profile         TEXT,
    cost_usd        REAL    NOT NULL,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cache_read_tokens   INTEGER,
    cache_creation_tokens INTEGER,
    source          TEXT    NOT NULL,
    elapsed_sec     REAL,
    session_id      TEXT
);
CREATE INDEX IF NOT EXISTS idx_task ON cost_events(task_id);
CREATE INDEX IF NOT EXISTS idx_ts ON cost_events(ts);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns a ledger written by an older version does not have.

    ``CREATE TABLE IF NOT EXISTS`` does nothing to a table that already exists,
    so a database created before a column was introduced would fail every
    INSERT that names it. Idempotent and silent on a current schema."""
    have = {row[1] for row in conn.execute("PRAGMA table_info(cost_events)")}
    for column, ddl in (
        ("profile", "ALTER TABLE cost_events ADD COLUMN profile TEXT"),
    ):
        if column not in have:
            conn.execute(ddl)


def _connect() -> sqlite3.Connection:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(LEDGER_PATH), timeout=10.0)
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def record(
    *,
    task_id: str,
    stage: str,
    backend: str,
    cost_usd: float,
    source: str,
    profile: Optional[str] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    cache_read_tokens: Optional[int] = None,
    cache_creation_tokens: Optional[int] = None,
    elapsed_sec: Optional[float] = None,
    session_id: Optional[str] = None,
) -> None:
    """Append one cost event. Best-effort: SQLite errors are logged but
    never raised — cost-ledger failures must not break the pipeline.
    """
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO cost_events "
                "(ts, task_id, stage, backend, profile, cost_usd, input_tokens, "
                "output_tokens, cache_read_tokens, cache_creation_tokens, "
                "source, elapsed_sec, session_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, task_id, stage, backend, profile, float(cost_usd),
                 input_tokens, output_tokens, cache_read_tokens,
                 cache_creation_tokens, source, elapsed_sec, session_id),
            )
    except sqlite3.Error as exc:
        log.warning("cost_ledger.record failed (task=%s stage=%s): %s",
                    task_id, stage, exc)
