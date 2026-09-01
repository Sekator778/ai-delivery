"""Budget-stop operator buttons — bot side (2026-06-07).

Extracted from bot.py (already a ~2800-line god-module — do NOT grow it). This is
the Telegram surface for a budget/cap-stopped task:

  * ``send_prompt`` — posts the ``[Продолжить]/[Удалить]`` keyboard when the
    dispatcher POSTs a ``budget_stop`` signal to /notify;
  * ``budget_callback`` — handles the taps: Продолжить bumps the cost/iteration
    caps and bounces the task to ``inbox/`` (dispatcher re-ingests with the new
    budget); Удалить moves it to ``failed/`` (artifacts preserved, no re-run).

Single responsibility, no bot.py internals imported — paths are derived locally.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

logger = logging.getLogger(__name__)

_TASKS_ROOT = Path.home() / "projects" / "ai-delivery" / "tasks"
AWAITING_INPUT_DIR = _TASKS_ROOT / "awaiting-input"
INBOX_DIR = _TASKS_ROOT / "inbox"
FAILED_DIR = _TASKS_ROOT / "failed"

_REASON_RU = {
    "cost_cap": "достигнут лимит стоимости",
    "token_cap": "достигнут лимит токенов",
    "iteration_cap": "достигнут лимит итераций",
    "stagnant": "правки перестали сходиться",
    "watchdog_idle": "стадия зависла (idle)",
    "watchdog_total": "превышено общее время",
    "unparseable": "вердикт ревьюера не распознан",
    "unknown": "остановлено",
}


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")


def _move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        shutil.rmtree(str(dst))
    shutil.move(str(src), str(dst))


def _build_prompt(task_id: str, body: dict) -> tuple[str, InlineKeyboardMarkup]:
    """(text, keyboard) for the budget_stop prompt. Pure — no I/O."""
    reason = body.get("stop_reason", "cost_cap")
    pr_url = body.get("pr_url") or ""
    pr_line = f"\nPR: {pr_url}" if pr_url else ""
    text = (
        f"⏸️ Задача <code>{task_id}</code> остановлена: "
        f"{_REASON_RU.get(reason, reason)}.\n"
        f"Стоимость ${body.get('cost_usd', 0)}/${body.get('cost_cap', 0)}, "
        f"итерация {body.get('iteration', 0)}/{body.get('iteration_cap', 0)}."
        f"{pr_line}\n\nЧто делать?"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Продолжить (+$10, +1 итерация)",
                              callback_data=f"bud_continue:{task_id}")],
        [InlineKeyboardButton("🗑 Удалить", callback_data=f"bud_discard:{task_id}")],
    ])
    return text, keyboard


async def send_prompt(telegram_bot, task_id: str, body: dict) -> str | None:
    """Post the budget_stop keyboard to the task's Telegram thread. Returns None on
    success or a short error string the caller surfaces as the /notify response."""
    state = _read_json(AWAITING_INPUT_DIR / task_id / "state.json")
    if state is None:
        return "cannot read state"
    thread = state.get("telegram_thread") or {}
    chat_id = thread.get("chat_id")
    if chat_id is None:
        return "no chat_id in state"
    text, keyboard = _build_prompt(task_id, body)
    kwargs: dict = {"chat_id": chat_id, "text": text,
                    "reply_markup": keyboard, "parse_mode": "HTML"}
    message_id = thread.get("message_id")
    if message_id is not None:
        kwargs["reply_to_message_id"] = message_id
    await telegram_bot.send_message(**kwargs)
    logger.info("budget_stop prompt sent for %s (reason=%s)", task_id, body.get("stop_reason"))
    return None


async def budget_callback(update: Update, context: object) -> None:
    """[Продолжить]/[Удалить] taps for a budget-stopped task in awaiting-input/."""
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    if len(parts) < 2:
        logger.warning("budget_callback: malformed data=%r", query.data)
        return
    action, task_id = parts[0], parts[1]

    task_dir = AWAITING_INPUT_DIR / task_id
    if not task_dir.is_dir():
        await query.edit_message_text(
            f"❌ Задача {task_id} не найдена в awaiting-input/. Возможно, уже обработана.")
        return

    if action == "bud_discard":
        _move(task_dir, FAILED_DIR / task_id)
        await query.edit_message_text(
            f"🗑 Задача {task_id} удалена (перемещена в failed/, артефакты сохранены).")
        logger.info("budget_callback: discarded %s", task_id)
        return

    if action == "bud_continue":
        bump_cost = float(os.environ.get("BUDGET_CONTINUE_COST_BUMP", "10") or 10)
        bump_iter = int(os.environ.get("BUDGET_CONTINUE_ITER_BUMP", "1") or 1)
        spec_path = task_dir / "spec.json"
        spec = _read_json(spec_path)
        if spec is None:
            await query.edit_message_text("❌ Не удалось прочитать spec.json.")
            return
        new_cap = float(spec.get("cost_cap_usd") or 20) + bump_cost
        new_iter = int(spec.get("iteration_cap") or 3) + bump_iter
        spec["cost_cap_usd"], spec["iteration_cap"] = new_cap, new_iter
        _write_json(spec_path, spec)

        # Bounce to inbox/ so the dispatcher re-ingests with the new budget; the
        # runner's artifact-resume skips completed stages and the extra headroom
        # lets the hotfix loop actually finish.
        state = _read_json(task_dir / "state.json") or {}
        state["stage"] = "inbox"
        _write_json(task_dir / "state.json", state)
        _move(task_dir, INBOX_DIR / task_id)
        await query.edit_message_text(
            f"▶️ Задача {task_id} продолжена: кэп ${new_cap:.0f}, лимит итераций "
            f"{new_iter}. Возвращена в inbox — диспатчер перезапустит.")
        logger.info("budget_callback: continued %s cap=%.2f iter=%d", task_id, new_cap, new_iter)
        return

    logger.warning("budget_callback: unknown action=%r task_id=%s", action, task_id)
