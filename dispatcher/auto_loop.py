"""auto_loop — Phase 5 Wave 3.

Port of the pipeline auto-loop semantics from claude-tg-orchestrator commit
4aa02b0 (meta/CLAUDE.md section "Pipeline auto-loop on REQUEST_CHANGES") and
the idle-detection watchdog from fb4cdc8.

The original code ran inside the meta-agent's conversation context. Here we
materialise it as explicit Python so the task-dispatcher daemon can drive the
loop without an LLM in the supervisor seat.

Used by task_dispatcher.py after each Reviewer SUBTASK_DONE event:

    verdict = parse_reviewer_output(reviewer_text)
    record_review(loop_state, verdict)
    stop = evaluate_stop(verdict, loop_state, cost_usd, cost_cap_usd)
    if stop == StopReason.APPROVE:
        merge_pr(...)
    elif stop is not None:
        escalate_to_telegram(escalation_summary(stop, loop_state, verdict))
    else:
        dispatch_next_iteration(...)
"""

from __future__ import annotations

import dataclasses
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class StopReason(str, Enum):
    APPROVE = "approve"
    ITERATION_CAP = "iteration_cap"
    STAGNANT = "stagnant"
    WATCHDOG_IDLE = "watchdog_idle"
    WATCHDOG_TOTAL = "watchdog_total"
    COST_CAP = "cost_cap"


@dataclass
class ReviewerVerdict:
    verdict: str
    critical: int
    warning: int
    suggestion: int

    @property
    def critical_plus_high(self) -> int:
        return self.critical + self.warning


_VERDICT_RE = re.compile(r"REVIEW_COMPLETE:\s*(\w+)", re.IGNORECASE)
_COUNT_RE = re.compile(r"\b(CRITICAL|WARNING|SUGGESTION)\s*=\s*(\d+)", re.IGNORECASE)


def parse_reviewer_output(text: str) -> ReviewerVerdict:
    verdict = "unknown"
    m = _VERDICT_RE.search(text)
    if m:
        verdict = m.group(1).strip().lower()
    counts = {"critical": 0, "warning": 0, "suggestion": 0}
    for kind, n in _COUNT_RE.findall(text):
        counts[kind.lower()] = int(n)
    return ReviewerVerdict(verdict=verdict, **counts)


@dataclass
class LoopState:
    iteration: int = 0
    iteration_cap: int = 3
    history: list[dict] = field(default_factory=list)
    last_progress_at: float = field(default_factory=time.time)
    started_at: float = field(default_factory=time.time)
    stagnant_streak: int = 0

    def to_json(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "LoopState":
        allowed = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**allowed)


@dataclass
class WatchdogConfig:
    """Idle-detection watchdog (port of claude-tg-orchestrator fb4cdc8).

    The original (`bot.py` `_watchdog.record_progress`) refreshed
    `last_progress_at` on every stdout/stderr line from sub-Claude. We do the
    same — task_dispatcher will call `record_progress(state)` for each line
    streamed back from `botctl-run-in-project`.
    """

    max_idle_sec: int = 30 * 60
    max_total_sec: int = 3 * 3600

    def is_idle(self, last_progress_at: float, now: float | None = None) -> bool:
        return ((now or time.time()) - last_progress_at) >= self.max_idle_sec

    def is_over_total(self, started_at: float, now: float | None = None) -> bool:
        return ((now or time.time()) - started_at) >= self.max_total_sec


def record_progress(state: LoopState, now: float | None = None) -> None:
    """Refresh idle-watchdog. Called per stdout/stderr line from sub-Claude."""
    state.last_progress_at = now or time.time()


def record_review(state: LoopState, verdict: ReviewerVerdict) -> None:
    """Append a reviewer event to history, tick the iteration counter, update
    the stagnation streak."""
    prior_reviews = [h for h in state.history if h.get("kind") == "review"]
    if prior_reviews and verdict.critical_plus_high >= prior_reviews[-1]["critical_plus_high"]:
        state.stagnant_streak += 1
    else:
        state.stagnant_streak = 0

    state.history.append({
        "kind": "review",
        "at": time.time(),
        "verdict": verdict.verdict,
        "critical": verdict.critical,
        "warning": verdict.warning,
        "suggestion": verdict.suggestion,
        "critical_plus_high": verdict.critical_plus_high,
    })
    state.iteration += 1
    state.last_progress_at = time.time()


def evaluate_stop(
    verdict: ReviewerVerdict,
    state: LoopState,
    cost_usd: float,
    cost_cap_usd: float,
    watchdog: Optional[WatchdogConfig] = None,
) -> Optional[StopReason]:
    """Return the first matching stop condition, or None to keep looping.

    Order matches meta/CLAUDE.md "Stop conditions" — APPROVE first because it
    is the success path; then cap; then stagnation; then watchdog; then cost.
    """
    wd = watchdog or WatchdogConfig()
    if verdict.verdict == "approve" and verdict.critical == 0:
        return StopReason.APPROVE
    if state.iteration >= state.iteration_cap:
        return StopReason.ITERATION_CAP
    if state.stagnant_streak >= 2:
        return StopReason.STAGNANT
    if wd.is_idle(state.last_progress_at):
        return StopReason.WATCHDOG_IDLE
    if wd.is_over_total(state.started_at):
        return StopReason.WATCHDOG_TOTAL
    if cost_usd >= cost_cap_usd:
        return StopReason.COST_CAP
    return None


# ---------------------------------------------------------------------------
# Telegram messages — verbatim from CLAUDE.md spec
# ---------------------------------------------------------------------------

def iteration_status(iteration: int, cap: int, phase: str, detail: str = "") -> str:
    """Telegram one-liner per phase. Format mirrors meta/CLAUDE.md example:
        Итерация 2/3: Developer-hotfix → 3 файла, commit abc1234. Запускаю Tester.
    """
    msg = f"Итерация {iteration}/{cap}: {phase}"
    if detail:
        msg += f" → {detail}"
    return msg


def approve_summary(pr_url: str, iterations: int, verdict: ReviewerVerdict, elapsed_sec: float) -> str:
    minutes = max(1, int(elapsed_sec // 60))
    return (
        "✓ APPROVE. "
        f"PR: {pr_url}\n"
        f"Итераций: {iterations}\n"
        f"Findings: Critical {verdict.critical}, "
        f"Warning {verdict.warning}, Suggestion {verdict.suggestion}\n"
        f"Время: {minutes}m"
    )


def escalation_summary(
    reason: StopReason,
    state: LoopState,
    last_verdict: Optional[ReviewerVerdict],
) -> str:
    v = last_verdict
    counts = (
        f"Critical {v.critical} / Warning {v.warning}"
        if v else "Critical ? / Warning ?"
    )
    if reason is StopReason.ITERATION_CAP:
        return (
            f"⏸ Достигнут лимит {state.iteration_cap} hotfix-итераций. "
            f"Текущий статус: {v.verdict if v else 'unknown'} с {counts} findings. "
            f"Продолжаем (`да`) или останавливаемся?"
        )
    if reason is StopReason.STAGNANT:
        return (
            f"⏸ Прогресс остановился: Critical+Warning не уменьшаются 2 цикла подряд. "
            f"Текущий статус: {counts}. Как продолжаем?"
        )
    if reason is StopReason.WATCHDOG_IDLE:
        idle_min = int((time.time() - state.last_progress_at) // 60)
        return f"⏸ Watchdog: pipeline молчит {idle_min}m. Остановлен."
    if reason is StopReason.WATCHDOG_TOTAL:
        total_min = int((time.time() - state.started_at) // 60)
        return f"⏸ Watchdog: общее время {total_min}m превысило ceiling. Остановлен."
    if reason is StopReason.COST_CAP:
        return f"⏸ Cost cap превышен. Продолжаем дороже (`да`) или останавливаемся?"
    return f"⏸ Pipeline остановлен: {reason.value}"


# ---------------------------------------------------------------------------
# Smoke tests — run with `python3 dispatcher/auto_loop.py`
# ---------------------------------------------------------------------------

def _smoke() -> None:
    # parse: APPROVE with zero critical
    v = parse_reviewer_output(
        "REVIEW_COMPLETE: approve\nCRITICAL=0\nWARNING=2\nSUGGESTION=5\n"
    )
    assert v.verdict == "approve" and v.critical == 0 and v.warning == 2, v

    # parse: REQUEST_CHANGES
    v2 = parse_reviewer_output(
        "REVIEW_COMPLETE: request_changes\nCRITICAL=3\nWARNING=4\nSUGGESTION=1\n"
    )
    assert v2.verdict == "request_changes" and v2.critical == 3, v2

    # stop: approve path
    s = LoopState()
    record_review(s, v)
    assert evaluate_stop(v, s, cost_usd=1.0, cost_cap_usd=20.0) is StopReason.APPROVE

    # stop: iteration cap (need to NOT trigger approve)
    s2 = LoopState(iteration_cap=3)
    for _ in range(3):
        record_review(s2, v2)
    assert evaluate_stop(v2, s2, cost_usd=1.0, cost_cap_usd=20.0) is StopReason.ITERATION_CAP

    # stop: stagnation — need two non-decreasing reviews in a row
    s3 = LoopState(iteration_cap=10)
    record_review(s3, ReviewerVerdict("request_changes", 5, 1, 0))
    record_review(s3, ReviewerVerdict("request_changes", 5, 1, 0))   # streak = 1
    record_review(s3, ReviewerVerdict("request_changes", 5, 1, 0))   # streak = 2
    assert s3.stagnant_streak == 2
    assert evaluate_stop(
        ReviewerVerdict("request_changes", 5, 1, 0), s3,
        cost_usd=1.0, cost_cap_usd=20.0,
    ) is StopReason.STAGNANT

    # stop: cost cap
    s4 = LoopState()
    record_review(s4, v2)
    assert evaluate_stop(v2, s4, cost_usd=25.0, cost_cap_usd=20.0) is StopReason.COST_CAP

    # stop: watchdog idle (forced)
    s5 = LoopState()
    s5.last_progress_at = time.time() - (60 * 60)
    assert evaluate_stop(v2, s5, cost_usd=1.0, cost_cap_usd=20.0) is StopReason.WATCHDOG_IDLE

    # progress refresh resets idle
    record_progress(s5)
    assert evaluate_stop(v2, s5, cost_usd=1.0, cost_cap_usd=20.0) is None

    # message formats
    assert "Итерация 2/3" in iteration_status(2, 3, "Developer-hotfix", "3 файла")
    assert "PR: https://x" in approve_summary("https://x", 2, v, elapsed_sec=900)
    assert "лимит" in escalation_summary(StopReason.ITERATION_CAP, s2, v2)

    print("auto_loop smoke OK")


if __name__ == "__main__":
    _smoke()
