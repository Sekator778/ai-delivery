"""Limit-outage detection + parking for the agent pipeline (issue #11).

Incident 2026-08-12 (task ``public-publish-script``, architect stage): the Claude
Max 5-hour window was exhausted mid-stage. NO limit message ever reached the
stage log — the claude CLI's own retry machinery kept re-attempting the inner
Agent call (``tool_progress`` events carrying ``subagent_retry`` with
``elapsed_time_seconds: 0``) until ``STAGE_TIMEOUT_SEC`` (1800 s) killed the
stage: rc=124, task → failed, $4.42 burned, nothing produced.

Two defects, two fixes:

  * The runner could not SEE the storm. ``subprocess.run(capture_output=True)``
    only hands the stream over after the child exits, so a live retry loop is
    invisible until the wall-clock timeout fires. ``LimitStallWatch`` is fed the
    ``--output-format stream-json`` NDJSON lines incrementally (see
    ``stage_runner_agent._run_claude_stage``) and classifies the run as a
    LIMIT_STALL from either an api-error marker or a silence window with
    accumulating subagent retries.
  * A limit outage was reported as a FAILURE. ``park()`` instead writes
    ``stage='waiting-limits'`` + ``resume_at`` and moves the task into the
    existing ``awaiting-input/`` bucket; the watcher requeues it once
    ``resume_at`` passes (``is_due``) and the runner's artifact-based resume
    skips the stages that already completed.

Everything above the ``park()`` boundary is pure and I/O-free, so classification,
reset-time parsing and the requeue decision are unit-testable without spawning a
child process (``tests/test_limit_stall.py``).

TODO(#11): optional backend rotation — for stages whose ``model_routing``
permits, rotate to deepseek/glm on LIMIT_STALL instead of parking. Deliberately
NOT implemented here (issue #11 lists it as optional); the cross-provider
fallback in ``stage_runner_agent`` still covers the post-hoc 429 case.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from runner_state import _append_worklog, _update_state
from telegram_io import _notify_bot, _send_telegram

# ── Tunables ────────────────────────────────────────────────────────────────
# Silence window: no assistant / tool-result event for this long, while subagent
# retries pile up, is a retry storm rather than a slow stage.
STALL_WINDOW_SEC = int(os.environ.get("LIMIT_STALL_WINDOW_SEC", "300"))
# Retries observed before the silence window is allowed to fire. A single retry
# is a normal transient; a storm repeats.
STALL_MIN_RETRIES = int(os.environ.get("LIMIT_STALL_MIN_RETRIES", "2"))
# Fallback park duration when the stream carries no parseable reset time.
LIMIT_BACKOFF_MIN = int(os.environ.get("LIMIT_BACKOFF_MIN", "30"))
# Safety cap on a PARSED reset time — a misparse must not park a task for weeks.
LIMIT_PARK_MAX_HOURS = int(os.environ.get("LIMIT_PARK_MAX_HOURS", "12"))
# How many times one task may be auto-requeued out of a limit park (counted in
# state.limit_parks, bumped by the watcher's resume sweep) before it waits for
# the operator. Without it, a permanently exhausted account would park/requeue
# forever.
LIMIT_PARK_LIMIT = int(os.environ.get("LIMIT_PARK_LIMIT", "4"))

# state.stage while parked. NOT a new bucket: the task dir lives in the existing
# awaiting-input/, this label only tells the watcher/bot why it is there.
PARKED_STAGE = "waiting-limits"

_AWAITING_INPUT_DIR = Path(__file__).resolve().parent.parent / "tasks" / "awaiting-input"

# ── Stream classification ───────────────────────────────────────────────────
# Markers are matched case-insensitively against a raw stream line. Kept TIGHT
# and quote-shaped: the same stream carries tool_result payloads (file contents,
# grep output) that can quote the words "rate limit" from our own source, so a
# loose list would false-positive and kill healthy stages. Model text and tool
# results are additionally skipped unless the event is flagged is_error.
LIMIT_MARKERS = (
    '"api_error_status":429',
    '"api_error_status": 429',
    '"error":"rate_limit"',
    '"error": "rate_limit"',
    "rate_limit_error",
    "rate limit exceeded",
    "429 too many requests",
    "overloaded_error",
    "you've hit your session limit",
    "hit your usage limit",
    "claude ai usage limit reached",
)

# The CLI broadcasts an INFORMATIONAL rate_limit_event in every session
# (an allow-status + resetsAt of the current window). It must never satisfy the
# word-marker scan — it is handled structurally in LimitStallWatch.feed, and its
# resetsAt epoch is the exact resume time. (Incident 2026-08-13: the old
# five_hour marker matched this event and killed two healthy stages within
# seconds of starting.)
_RATE_LIMIT_EVENT_TYPE = "rate_limit_event"

# Statuses that mean "this request went through". Matched by PREFIX, not
# equality: the structural fix above assumed the only permissive value was the
# bare "allowed", and upstream also sends "allowed_warning" once utilization
# crosses surpassedThreshold (0.9). On 2026-08-15 that killed a healthy reviewer
# stage 136s in — with all three lenses mid-flight — parked the task for 90
# minutes and told the operator limits were exhausted, while 9% of the five-hour
# window was still available. The word says allowed; treat it as allowed.
#
# Deliberately a prefix rule rather than an enumeration of blocking statuses: an
# unrecognised status still parks the task, which is the fail-safe direction.
_ALLOW_STATUS_PREFIX = "allowed"

# Event types that carry model text / tool results — i.e. attacker-free but
# quote-prone payloads. Markers inside them count only when is_error is set.
_QUOTE_PRONE_TYPES = frozenset({"assistant", "user"})
# Event types that prove the stage is still making progress.
_PROGRESS_TYPES = frozenset({"assistant", "user", "result"})
# The retry breadcrumb the incident's stream was full of.
_RETRY_MARKER = "subagent_retry"

VERDICT_API_ERROR = "api-error"
VERDICT_STALL = "stall"


def _looks_like_error_event(line: str, event: dict | None) -> bool:
    if event is not None and event.get("is_error"):
        return True
    low = line.lower()
    return '"is_error":true' in low or '"is_error": true' in low


def has_limit_marker(line: str, event: dict | None = None) -> bool:
    """True when *line* carries an api-error signature that means "the backend
    is refusing us", not "the model happened to type the words"."""
    if not line:
        return False
    etype = str((event or {}).get("type") or "")
    if etype == _RATE_LIMIT_EVENT_TYPE:
        return False  # structural handling in feed(); never word-scan it
    if etype in _QUOTE_PRONE_TYPES and not _looks_like_error_event(line, event):
        return False
    low = line.lower()
    return any(marker in low for marker in LIMIT_MARKERS)


class LimitStallWatch:
    """Incremental classifier over the claude ``stream-json`` NDJSON output.

    Fed one raw line at a time while the child is still running; ``verdict()``
    answers "is this a limit outage?" without waiting for the process to exit.
    Pure: no I/O, no clock of its own (``now`` is injectable), so the whole
    decision table is unit-testable.
    """

    def __init__(self, *, window_sec: int | None = None,
                 min_retries: int | None = None, now: float | None = None) -> None:
        self.window_sec = STALL_WINDOW_SEC if window_sec is None else window_sec
        self.min_retries = STALL_MIN_RETRIES if min_retries is None else min_retries
        self.last_progress = time.time() if now is None else now
        self.retries = 0
        self.progress_events = 0
        self.error_hint = ""
        # Advertised end of the current rate-limit window (epoch), captured
        # from informational rate_limit_events — used for precise resume_at.
        self.reset_at_hint: float | None = None

    def feed(self, line: str, now: float | None = None) -> None:
        text = (line or "").strip()
        if not text:
            return
        now = time.time() if now is None else now
        event: dict | None = None
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
                event = parsed if isinstance(parsed, dict) else None
            except (json.JSONDecodeError, ValueError):
                event = None

        etype = str((event or {}).get("type") or "")
        if etype == _RATE_LIMIT_EVENT_TYPE:
            info = (event or {}).get("rate_limit_info") or {}
            reset = info.get("resetsAt") or info.get("resets_at")
            if isinstance(reset, (int, float)) and reset > 0:
                self.reset_at_hint = float(reset)
            status = str(info.get("status") or "").lower()
            if (status and not status.startswith(_ALLOW_STATUS_PREFIX)
                    and not self.error_hint):
                self.error_hint = text[-2000:]
            return  # informational unless status says otherwise; never word-scan

        if _RETRY_MARKER in text:
            # A retry is the OPPOSITE of progress — never refresh the clock.
            self.retries += 1
        elif etype in _PROGRESS_TYPES:
            self.last_progress = now
            self.progress_events += 1

        if not self.error_hint and has_limit_marker(text, event):
            self.error_hint = text[-2000:]

    def verdict(self, now: float | None = None) -> str | None:
        """``'api-error'`` / ``'stall'`` / ``None`` (keep running)."""
        if self.error_hint:
            return VERDICT_API_ERROR
        now = time.time() if now is None else now
        if (self.retries >= self.min_retries
                and (now - self.last_progress) >= self.window_sec):
            return VERDICT_STALL
        return None


# ── Reset-time parsing ──────────────────────────────────────────────────────
# The API returns `retry-after: <seconds>` and `anthropic-ratelimit-*-reset:
# <RFC3339>`; the CLI's limit line says "resets at <time>". None of them reliably
# survives the subagent retry loop, so this is best-effort enrichment on top of
# the flat backoff — never the trigger.
_ISO_RESET_RE = re.compile(
    r"reset[a-z_-]*[\"']?\s*[:=]\s*[\"']?"
    r"(\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:[Zz]|[+-]\d{2}:?\d{2})?)"
)
_RETRY_AFTER_RE = re.compile(r"retry[-_ ]?after[\"']?\s*[:=]\s*[\"']?(\d{1,6})")
_EPOCH_RESET_RE = re.compile(r"resets?_?at[\"']?\s*[:=]\s*[\"']?(\d{10})\b", re.IGNORECASE)
_CLOCK_RESET_RE = re.compile(
    r"reset[s]?\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.IGNORECASE)


def _iso_to_epoch(value: str) -> float | None:
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:  # RFC3339 from the API is UTC; naive means UTC here
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def parse_reset_at(text: str, now: float | None = None) -> float | None:
    """Epoch seconds of the limit reset advertised in *text*, or None.

    Tried in decreasing precision: RFC3339 ``*-reset`` value → ``retry-after``
    seconds → the CLI's human ``resets at <h[:mm][am|pm]>`` (next occurrence in
    LOCAL time, which is how the CLI prints it)."""
    if not text:
        return None
    now = time.time() if now is None else now

    # Structured epoch from the CLI's rate_limit_event ("resetsAt":1786656600) —
    # the most precise source; accept only plausible values (past hour .. +7d).
    m = _EPOCH_RESET_RE.search(text)
    if m:
        epoch = float(m.group(1))
        if now - 3600 <= epoch <= now + 7 * 86400:
            return epoch

    m = _ISO_RESET_RE.search(text)
    if m:
        epoch = _iso_to_epoch(m.group(1))
        if epoch is not None:
            return epoch

    m = _RETRY_AFTER_RE.search(text)
    if m:
        return now + float(m.group(1))

    m = _CLOCK_RESET_RE.search(text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        meridiem = (m.group(3) or "").lower()
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            return None
        base = datetime.fromtimestamp(now).replace(
            hour=hour, minute=minute, second=0, microsecond=0)
        epoch = base.timestamp()
        if epoch <= now:
            epoch += 86400.0  # already past today → the next occurrence
        return epoch
    return None


def resume_at_for(text: str, now: float | None = None,
                  backoff_min: int | None = None) -> float:
    """When to un-park: the advertised reset when we can trust it, else a flat
    ``LIMIT_BACKOFF_MIN`` backoff. A parsed time in the past (clock skew) or
    absurdly far out (misparse) falls back to the backoff / the cap."""
    now = time.time() if now is None else now
    backoff = LIMIT_BACKOFF_MIN if backoff_min is None else backoff_min
    fallback = now + backoff * 60.0
    parsed = parse_reset_at(text, now)
    if parsed is None or parsed <= now:
        return fallback
    return min(parsed, now + LIMIT_PARK_MAX_HOURS * 3600.0)


def to_epoch(value: object) -> float | None:
    """Read a ``resume_at`` back: accepts epoch numbers and ISO-8601 strings
    (what ``park`` writes). None when unusable."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        return _iso_to_epoch(value)
    return None


def is_due(state: dict | None, now: float | None = None) -> bool:
    """Pure requeue decision for the watcher sweep: True when this parked task's
    wait is over. False for anything not parked, still waiting, or already
    requeued ``LIMIT_PARK_LIMIT`` times (then the operator decides)."""
    st = state or {}
    if str(st.get("stage") or "") != PARKED_STAGE:
        return False
    if int(st.get("limit_parks") or 0) >= LIMIT_PARK_LIMIT:
        return False
    now = time.time() if now is None else now
    resume_at = to_epoch(st.get("resume_at"))
    if resume_at is None:
        return True  # parked without a usable timestamp — don't strand it
    return now >= resume_at


# ── Park (the only I/O in this module) ──────────────────────────────────────


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


def park(task_dir: Path, task_id: str, *, stage: str, reason: str,
         resume_at: float, hint: str = "", cost_usd: float = 0.0) -> Path:
    """Park a limit-stalled task: ``stage='waiting-limits'`` + ``resume_at``,
    move to ``awaiting-input/``, ping the owner. Move THEN notify (same order as
    budget_gate.park) so the bot finds the task in the bucket. Best-effort —
    never raises; a notify hiccup must not mask the pipeline result. Returns the
    task dir's new location (unchanged when the move was skipped/failed)."""
    resume_iso = _iso(resume_at)
    state: dict = {}
    try:
        state = json.loads((task_dir / "state.json").read_text())
    except Exception:  # noqa: BLE001
        state = {}
    if str(state.get("stage") or "") == PARKED_STAGE:
        # The parallel tester+security pair can stall together — the first park
        # already moved the directory; a second one must not re-notify.
        print(f"[limit-stall] {task_id} already parked — skipping duplicate park",
              file=sys.stderr)
        return task_dir
    resumes = int(state.get("limit_parks") or 0)
    try:
        _update_state(
            task_dir,
            stage=PARKED_STAGE,
            resume_at=resume_iso,
            limit_stall={
                "stage": stage,
                "reason": reason,
                "resume_at": resume_iso,
                "hint": (hint or "")[-500:],
            },
        )
    except Exception as exc:  # noqa: BLE001
        print(f"warn: limit-stall state update failed: {exc}", file=sys.stderr)
    try:
        _append_worklog(
            task_dir,
            f"LIMIT-STALL: stage {stage} — {reason}; parked until {resume_iso} "
            f"(auto-resumes used {resumes}/{LIMIT_PARK_LIMIT})",
        )
    except Exception:  # noqa: BLE001
        pass

    dst = task_dir
    try:
        if task_dir.is_dir() and task_dir.parent.name == "active":
            _AWAITING_INPUT_DIR.mkdir(parents=True, exist_ok=True)
            dst = _AWAITING_INPUT_DIR / task_id
            if dst.exists():
                shutil.rmtree(str(dst))
            shutil.move(str(task_dir), str(dst))
    except Exception as exc:  # noqa: BLE001
        print(f"warn: limit-stall bucket move failed: {exc}", file=sys.stderr)
        dst = task_dir

    try:
        _send_telegram(
            f"[{task_id}] ⏳ limits exhausted at {stage} — parked until "
            f"{resume_iso} (auto-resume, ${cost_usd:.4f} so far)")
        _notify_bot(
            "limit_stall_parked", task_id,
            failed_stage=stage,
            reason=reason,
            resume_at=resume_iso,
            cost_usd=round(float(cost_usd or 0.0), 4),
        )
    except Exception:  # noqa: BLE001
        pass
    return dst
