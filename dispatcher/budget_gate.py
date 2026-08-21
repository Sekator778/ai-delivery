"""Budget-stop operator gate (2026-06-07).

One place that parks a budget/cap-stopped task in ``awaiting-input/`` and asks the
bot to surface ``[Продолжить]/[Удалить]`` buttons. Used by BOTH:

  * the stage runner — when the post-pipeline decision is a cap stop (cost_cap /
    iteration_cap / stagnant / …), and
  * the watcher — when it finds an over-budget orphan it must NOT respawn (a
    respawn would skip every completed stage and just re-hit the cap — the
    ``$21-twice`` re-fail loop).

Extracted into its own module so the "move + notify" logic isn't copy-pasted across
the runner and the watcher (DRY). ``awaiting-input/`` is NOT auto-ingested by the
dispatcher, so a parked task waits here for a human decision instead of burning
another run.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from telegram_io import _notify_bot

# Stops where the operator can meaningfully tap [Продолжить] (extend the budget /
# grant another iteration) or [Удалить]. Drives the budget_stop bot prompt and
# gates which post-pipeline dispositions route through park().
BUDGET_STOP_REASONS = frozenset({
    "cost_cap", "iteration_cap", "stagnant", "watchdog_idle", "watchdog_total",
    "unparseable", "unknown",
})

_AWAITING_INPUT_DIR = Path(__file__).resolve().parent.parent / "tasks" / "awaiting-input"


def park(task_dir: Path, task_id: str, *, stop_reason: str,
         cost_usd: float, cost_cap: float) -> None:
    """Park a budget/cap-stopped task: set state terminal, move it to
    ``awaiting-input/``, and POST ``budget_stop`` so the bot sends the
    [Продолжить]/[Удалить] keyboard. Move THEN notify, so the bot finds the task in
    the bucket. Best-effort — never raises (a notify hiccup must not mask the
    pipeline result). Idempotent: a no-op move when the task is already relocated."""
    pr_url, iteration, iteration_cap = "", 1, 3
    state_path = task_dir / "state.json"
    try:
        st = json.loads(state_path.read_text())
        pr_url = (st.get("pr_url") or "").strip()
        iteration = int(st.get("iteration") or 1)
        iteration_cap = int(st.get("iteration_cap") or 3)
        st["stage"] = "awaiting-input"
        state_path.write_text(json.dumps(st, indent=2, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass
    try:
        if task_dir.is_dir() and task_dir.parent.name == "active":
            _AWAITING_INPUT_DIR.mkdir(parents=True, exist_ok=True)
            dst = _AWAITING_INPUT_DIR / task_id
            if dst.exists():
                shutil.rmtree(str(dst))
            shutil.move(str(task_dir), str(dst))
    except Exception as exc:  # noqa: BLE001
        print(f"warn: budget-stop bucket move failed: {exc}", file=sys.stderr)
    try:
        _notify_bot(
            "budget_stop", task_id,
            stop_reason=stop_reason,
            cost_usd=round(float(cost_usd or 0.0), 4),
            cost_cap=float(cost_cap or 0.0),
            iteration=iteration,
            iteration_cap=iteration_cap,
            pr_url=pr_url,
        )
    except Exception:  # noqa: BLE001
        pass
