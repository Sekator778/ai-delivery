"""Bot-notification policy for the agent pipeline (issue #19).

Owner feedback 2026-08-14: a re-ingest / limit-park resume fired FOUR
Telegram messages for one event (TRIAGE reused, pipeline started, stage
started, parked), and the "Stages:" line showed a tier-narrowed subset
without any indication of why it differed from the full stage menu.

Default policy — one message each for: task ingested+started (ONCE per
task — never repeated on a later resume/re-ingest of the SAME task),
clarify questions, limit-park (with resume time), auto-resume, PR ready /
awaiting-approval, and terminal failure. Everything else (per-stage
start/finish/retry, triage diagnostics, hotfix-loop progress) is LOG-ONLY —
worklog.md and state history always get it — and only mirrored to the bot
when the operator opts in via NOTIFY_VERBOSE=1.

Pure decision logic lives here so it is unit-testable without touching the
bot / botctl-send-text subprocess (tests/test_notify_policy.py). Callers in
stage_runner_agent.py own the actual I/O (_send_telegram / _notify_bot).
"""
from __future__ import annotations

import os

# Fires once per task lifetime. Suppressed on any resume/re-ingest (a
# limit-park requeue, a clarify round-trip, a watcher respawn) — the
# equivalent "we're continuing" ping is the "auto_resumed" event instead.
ONCE_PER_TASK: frozenset = frozenset({"pipeline_started"})

# Always notify, regardless of NOTIFY_VERBOSE or resume state — the
# operator-facing lifecycle events issue #19 asks to keep.
ALWAYS: frozenset = frozenset({
    "clarify_needed",
    "limit_parked",
    "auto_resumed",
    "pr_ready",
    "terminal_failed",
})

# Per-stage / diagnostic noise. Always logged (worklog.md + state history);
# mirrored to the bot only under NOTIFY_VERBOSE=1.
VERBOSE_ONLY: frozenset = frozenset({
    "triage_classified",
    "triage_reused",
    "triage_upgraded",
    "invest_report",
    "stage_started",
    "stage_done",
    "stage_retry",
    "stage_recovered",
    "hotfix_iteration",
})


def verbose_enabled(env: dict | None = None) -> bool:
    """True when the operator opted into per-stage progress notifications."""
    src = os.environ if env is None else env
    return str(src.get("NOTIFY_VERBOSE", "")).strip() == "1"


def should_notify(event: str, *, verbose: bool = False, is_resume: bool = False) -> bool:
    """Pure decision: does *event* warrant a bot-facing notification right now?

    - VERBOSE_ONLY events fire only when the caller opted into
      NOTIFY_VERBOSE=1 (checked first — resume state never overrides
      operator-requested verbosity).
    - ONCE_PER_TASK events fire on a fresh task only; a resume/re-ingest
      suppresses them (the caller sends "auto_resumed" instead — see
      stage_runner_agent.run_pipeline).
    - ALWAYS events fire unconditionally.
    - Anything unclassified fails OPEN (notify) — a forgotten event must
      not silently vanish; classify it into one of the sets above instead
      of relying on this fallback.
    """
    if event in VERBOSE_ONLY:
        return verbose
    if event in ONCE_PER_TASK:
        return not is_resume
    return True


def render_stage_list(stages: list) -> str:
    """Render the pipeline's planned stage list for a human-facing message —
    the exact same join the runner uses in its `[agent-pipeline] stages=...`
    log line, so the two can never drift apart (issue #19 bullet 1)."""
    return " → ".join(stages)
