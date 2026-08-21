"""Post-pipeline disposition — what state.stage becomes after a reviewer pass.

Extracted from stage_runner_agent.py (god-module split, 2026-06-04). One decision
function, _decide_post_pipeline_stage, that reads the reviewer verdict
(06-review-agent.json) and maps it to the next pipeline state. It is where two of
the project's load-bearing cost-control policies live:

  * the nitpick guard — a `request_changes` verdict with ZERO criticals routes to
    awaiting-approval instead of re-opening the developer-hotfix loop (the bug
    that burned $15.98 and bloated a PR 420->1284 lines on a `--version` task);
  * the convergence-trend recorder — each pass's critical count is appended
    (deduped per iteration) to state.review_trend so the upgrade ladder in
    run_pipeline can tell real progress ([5,3,1]) from perpetual-criticals
    thrash ([2,2]).

Pure decision + state write; imports _update_state from runner_state and the stop
evaluator from auto_loop (lazily, inside the function).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from runner_state import _update_state


def _decide_post_pipeline_stage(
    task_dir: Path, cumulative_cost: float, cost_cap: float, spec: dict,
) -> tuple[str, str]:
    """Inspect 06-review-agent.json and decide what state.stage should be.

    Returns (next_stage, stop_reason_str). Imports auto_loop lazily to keep
    this module importable in contexts (tests) where auto_loop isn't on
    sys.path.

    Mapping (when Reviewer ran and verdict parsed):
      verdict=approve + critical=0 → ('awaiting-approval', 'approve')
      verdict=request_changes:
        iteration < cap → ('request-changes-pending', 'request_changes')
                          run_pipeline's while-loop picks this up and runs
                          [developer-hotfix, tester, security, reviewer]
        iteration ≥ cap → ('awaiting-input', 'iteration_cap')
      cumulative_cost ≥ cap → ('failed', 'cost_cap')
      verdict=unparseable / missing → ('awaiting-input', 'unparseable')
    """
    review_json = task_dir / "06-review-agent.json"
    if not review_json.exists():
        # Reviewer wasn't part of this pass (e.g. stages list didn't include it)
        # or the artifact never landed — fall back to awaiting-approval so the
        # bot keyboard still surfaces a manual review option.
        return "awaiting-approval", "no_reviewer"

    try:
        data = json.loads(review_json.read_text())
    except Exception as exc:
        print(f"warn: 06-review-agent.json unreadable: {exc}", file=sys.stderr)
        return "awaiting-input", "unparseable"

    verdict_block = data.get("verdict", {}) or {}
    verdict_label = (verdict_block.get("verdict") or "").lower()
    critical = int(verdict_block.get("critical") or 0)

    # state.json carries the running iteration count for the agent path;
    # initial pipeline pass is iteration 1.
    state = json.loads((task_dir / "state.json").read_text())
    iteration = int(state.get("iteration") or 1)

    # ── Convergence trend (anti-thrash, committee 2026-06-02) ───────────────
    # Record this reviewer pass's critical count so the upgrade ladder (in
    # run_pipeline) can tell genuine progress ([5,3,1] → keep going) from
    # perpetual-criticals thrash ([2,2] → stop, the M-validation incident).
    # Deduped on `iteration`: the ladder re-calls this fn within the SAME
    # iteration after an upgrade, and without the guard the identical review
    # would be appended twice and fake a decrease. Recorded BEFORE the nitpick
    # guard's early return and even for a 0-critical pass — a 0 only helps the
    # convergence math and never re-opens a loop on its own.
    if state.get("review_trend_iter") != iteration:
        trend = list(state.get("review_trend") or [])
        trend.append(critical)
        _update_state(task_dir, review_trend=trend, review_trend_iter=iteration)

    # ── Nitpick guard (2026-05-30) ──────────────────────────────────────────
    # A `request_changes` verdict with ZERO critical findings must NOT open the
    # developer-hotfix loop. Warnings/suggestions are quality nice-to-haves, not
    # merge blockers. The loop trigger keying on the verdict LABEL (not the
    # critical count) is what burned $15.98 and bloated PR #6 from 420→1284
    # lines on a trivial `--version` task (tg-20260530-193650-1979): three
    # hotfix rounds, `critical: 0` every pass, never converging (warnings GREW
    # 2→5). Treat 0-critical as mergeable — route to awaiting-approval exactly
    # like `approve`, and let the operator read the non-blocking findings in
    # 06-review.md / on the PR. ONLY `critical > 0` re-opens the hotfix loop.
    if verdict_label == "request_changes" and critical == 0:
        warn = int(verdict_block.get("warning") or 0)
        sugg = int(verdict_block.get("suggestion") or 0)
        print(
            f"nitpick-guard: request_changes with 0 critical "
            f"({warn} warning, {sugg} suggestion) → awaiting-approval "
            f"(no hotfix loop)",
            file=sys.stderr,
        )
        return "awaiting-approval", "approve_no_critical"

    iteration_cap = int(spec.get("iteration_cap") or 3)

    try:
        from auto_loop import ReviewerVerdict, LoopState, evaluate_stop, StopReason
    except ImportError:
        print("warn: auto_loop import failed; falling back to simple mapping",
              file=sys.stderr)
        if verdict_label == "approve" and critical == 0:
            return "awaiting-approval", "approve"
        if verdict_label == "request_changes":
            return "request-changes-pending", "request_changes"
        return "awaiting-input", "unknown"

    verdict_obj = ReviewerVerdict(
        verdict=verdict_label or "unknown",
        critical=critical,
        warning=int(verdict_block.get("warning") or 0),
        suggestion=int(verdict_block.get("suggestion") or 0),
    )
    loop_state = LoopState(iteration=iteration, iteration_cap=iteration_cap)
    stop = evaluate_stop(verdict_obj, loop_state, cumulative_cost, cost_cap)

    if stop is StopReason.APPROVE:
        return "awaiting-approval", "approve"
    if stop is StopReason.ITERATION_CAP:
        return "awaiting-input", "iteration_cap"
    if stop is StopReason.COST_CAP:
        # 2026-06-07: a budget abort is operator-gated, not a hard fail. Parking
        # to awaiting-input (instead of failed/) lets the bot offer
        # [Продолжить]/[Удалить] and — crucially — stops the watcher from
        # respawning an over-budget orphan into a silent re-fail loop (the
        # $21-twice incident). The earlier "cost_cap → failed" decision is
        # superseded; the dollar cap is notional under a flat plan, so a human
        # decides whether to extend it.
        return "awaiting-input", "cost_cap"
    if stop is StopReason.STAGNANT:
        return "awaiting-input", "stagnant"
    if stop is StopReason.WATCHDOG_IDLE:
        return "awaiting-input", "watchdog_idle"
    if stop is StopReason.WATCHDOG_TOTAL:
        return "awaiting-input", "watchdog_total"
    # stop is None — REQUEST_CHANGES with room to iterate. Step 2c turns
    # this into an actual hotfix iteration; for now mark the state so the
    # bot keyboard / operator can act on it.
    return "request-changes-pending", "request_changes"
