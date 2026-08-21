"""bot.py — Telegram entry point for ai-delivery.

Receives text messages, authorizes the sender, hands the prompt to the
meta-agent (claude CLI), and lets the meta-agent reply through botctl-*
scripts. This module never sends Telegram messages itself for normal replies.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import log_redact
import watchdog as _watchdog_mod

import aiohttp
from aiohttp import web
from telegram import (
    BotCommand,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

import budget_buttons  # budget-stop [Продолжить]/[Удалить] surface (own module)
import stt_utils  # pure STT helpers: sanitize_filename, derive_transcript_path, etc. (ADR-002)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STATE_PATH = Path.home() / ".claude-tg-bot" / "state.json"
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
# Cap on how much of a single "[<channel> event] {...}" stream-json dump (the
# full meta-agent event, which can carry tool output read by the agent) is
# written to the log per line. The redaction filter still runs on it first —
# this only bounds size, not content.
META_EVENT_LOG_MAX = int(os.environ.get("META_EVENT_LOG_MAX", "2000"))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
DEFAULT_META_DIR = Path.home() / ".claude-tg-bot" / "meta"

# Whisper STT server (M2). Default is the local docker stack on 8765.
WHISPER_URL = os.environ.get("WHISPER_URL", "http://127.0.0.1:8765/transcribe")

# STT transcript output directory — auto-created on first use (FR-007/FR-008).
STT_OUTPUT_DIR = Path(os.path.expanduser(os.environ.get("STT_OUTPUT_DIR", "~/Downloads/transcripts")))
# Maximum download size for URL audio sources (FR-005, NFR-001).
STT_URL_MAX_MB = int(os.environ.get("STT_URL_MAX_MB", "100"))
# Whisper-cli transcription step timeout on the server side (FR-014, ADR-005).
WHISPER_TIMEOUT_SEC = int(os.environ.get("WHISPER_TIMEOUT_SEC", "600"))
# ffmpeg conversion step timeout on the server side (FR-015, ADR-005).
FFMPEG_TIMEOUT_SEC = int(os.environ.get("FFMPEG_TIMEOUT_SEC", "60"))

# Local HTTP server (M3) for sub-Claude dispatch from the meta-agent.
# Defaults to 127.0.0.1:8766 — never expose to LAN.
HTTP_HOST = os.environ.get("BOT_HTTP_HOST", "127.0.0.1")
HTTP_PORT = int(os.environ.get("BOT_HTTP_PORT", "8766"))

THIN_MODE_ENABLED = os.environ.get("THIN_MODE_ENABLED", "false").lower() in ("1", "true", "yes")
THIN_MODE_INBOX = Path.home() / "projects" / "ai-delivery" / "tasks" / "inbox"
THIN_MODE_AWAITING = Path.home() / "projects" / "ai-delivery" / "tasks" / "awaiting-approval"
THIN_MODE_DONE = Path.home() / "projects" / "ai-delivery" / "tasks" / "done"
THIN_MODE_AWAITING_INPUT = Path.home() / "projects" / "ai-delivery" / "tasks" / "awaiting-input"
THIN_MODE_FAILED = Path.home() / "projects" / "ai-delivery" / "tasks" / "failed"
BOT_DEFAULT_TARGET_REPO = os.environ.get("BOT_DEFAULT_TARGET_REPO", "").strip()

# Meta-run control (#8). A stray reply once spawned a context-free meta run
# that held meta_lock for 2.5h with no ping, no cap and no way to stop it from
# Telegram. META_TIMEOUT_SEC caps wall clock, META_PROGRESS_SEC paces the
# "still working" ping, META_KILL_GRACE_SEC is the TERM→KILL escalation delay.
META_TIMEOUT_SEC = int(os.environ.get("META_TIMEOUT_SEC", "600"))
META_PROGRESS_SEC = int(os.environ.get("META_PROGRESS_SEC", "60"))
META_KILL_GRACE_SEC = int(os.environ.get("META_KILL_GRACE_SEC", "10"))

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

USER_REGISTRY: dict[int, dict[str, str]] = {}
meta_lock = asyncio.Lock()

# The single in-flight meta run (meta_lock serializes them, so one record is
# enough) — see _meta_run_begin(). Touched only from the event loop thread.
_meta_run: Optional[dict] = None

# Per-task dispatch context for sub-Claude invocations (M3). Keyed by task_id.
active_tasks: dict[str, dict] = {}

_watchdog = _watchdog_mod.Watchdog()

# Shutdown coordination (bot-children-reaping, issue #20).
# Set first in _shutdown_children(); any new spawn site must check it.
_shutting_down: bool = False   # latch: admission closed during drain
_exit_signal: int = 0          # signal number that triggered the stop

# Stored by run_all() so the HTTP /notify endpoint can send Telegram messages
# without going through the python-telegram-bot handler machinery.
_telegram_bot: object = None

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# USER_REGISTRY loader
# ---------------------------------------------------------------------------

def load_user_registry() -> dict[int, dict[str, str]]:
    users_json = os.environ.get("BOT_USERS_JSON")
    if users_json:
        return {int(k): v for k, v in json.loads(users_json).items()}
    owner_id = os.environ.get("OWNER_TELEGRAM_ID")
    owner_name = os.environ.get("OWNER_NAME", "Owner")
    if not owner_id:
        logger.critical("OWNER_TELEGRAM_ID or BOT_USERS_JSON must be set")
        raise SystemExit(1)
    return {
        int(owner_id): {
            "name": owner_name,
            "meta_dir": os.environ.get("META_DIR", str(DEFAULT_META_DIR)),
            "chat_id": owner_id,
        }
    }


# ---------------------------------------------------------------------------
# Project registry (multi-target /task)
# ---------------------------------------------------------------------------
# `bot/projects.json` maps short aliases to absolute paths so /task can
# target any of N repos in parallel instead of being pinned to a single
# BOT_DEFAULT_TARGET_REPO. The dispatcher already runs DISPATCHER_MAX_STAGES
# pipelines concurrently; this just unlocks the input UX. Syntax:
#
#     /task @sandbox  Add divide() to app/calc.py
#     /task @userbot  Fix login retry loop
#     /task           Bare prompt → falls back to projects._default
#
# An entry is either a plain path string or `{"path": ..., "base": "dev"}` —
# the extended form pins the branch the pipeline cuts from and PRs against for
# that target (#6). Both shapes are parsed by dispatcher/project_registry.py,
# the ONE parser shared with the dispatcher, so the two can never disagree.
#
# `/projects` lists registered aliases. projects.json is gitignored
# (paths are per-host); projects.example.json is the public template.

sys.path.append(str(Path(__file__).resolve().parent.parent / "dispatcher"))
import project_registry as _registry  # noqa: E402  (path set just above)
from child_env import build_child_env  # noqa: E402  (same shared dispatcher dir)
import proc_reaper as _proc_reaper    # noqa: E402  (same shared dispatcher dir)

PROJECTS_FILE = Path(__file__).resolve().parent / "projects.json"
_PROJECTS_CACHE: Optional[dict] = None


def _load_projects() -> dict:
    """Read bot/projects.json. Empty dict if absent — caller falls back
    to BOT_DEFAULT_TARGET_REPO."""
    global _PROJECTS_CACHE
    if _PROJECTS_CACHE is not None:
        return _PROJECTS_CACHE
    _PROJECTS_CACHE = _registry.load_registry(PROJECTS_FILE)
    if not _PROJECTS_CACHE and PROJECTS_FILE.exists():
        logger.warning("projects.json present but empty/unparseable: %s",
                       PROJECTS_FILE)
    return _PROJECTS_CACHE


def _resolve_target_repo(body: str) -> tuple[str, str, Optional[str]]:
    """Strip a leading @alias from the /task body and resolve to a path.

    Returns (cleaned_body, target_repo_path, alias_used).
    alias_used is None when the user didn't specify @alias (fall-through
    case). target_repo_path is the resolved absolute path, validated
    against projects.json; falls back to BOT_DEFAULT_TARGET_REPO if no
    registry entry matches or no @alias was passed.

    Special return: target_repo_path is the literal string "@UNKNOWN:<alias>"
    when the user passed an alias that isn't registered — caller surfaces
    a Telegram error message with the list of known aliases.
    """
    body = body.lstrip()
    alias: Optional[str] = None
    if body.startswith("@"):
        head, _, rest = body.partition(" ")
        alias = head[1:].strip()
        body = rest.lstrip()

    registry = _load_projects()
    projects = _registry.project_paths(registry)

    if alias:
        if alias in projects:
            return body, projects[alias], alias
        return body, f"@UNKNOWN:{alias}", alias

    default_alias = _registry.default_alias(registry)
    if default_alias and default_alias in projects:
        return body, projects[default_alias], default_alias

    return body, BOT_DEFAULT_TARGET_REPO, None


# ---------------------------------------------------------------------------
# State IO
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load state: %s", exc)
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(STATE_PATH.parent), suffix=".json")
    os.close(fd)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------------------
# Meta-run control (#8) — wall-clock cap, progress pings, /cancel
# ---------------------------------------------------------------------------

def _meta_run_active() -> bool:
    """True while the claude child of a meta run is still alive."""
    run = _meta_run
    return bool(run and run["proc"].returncode is None)


def _meta_elapsed_min(run: dict) -> int:
    return int((asyncio.get_running_loop().time() - run["started_at"]) // 60)


def _fmt_sec(sec: int) -> str:
    """Minutes read better than seconds for the default 600s cap, but a
    sub-minute cap must not be reported as "0 мин"."""
    return f"{sec // 60} мин" if sec >= 60 else f"{sec} сек"


def _resolve_chat_id(chat_id: Optional[int]) -> Optional[int]:
    """Explicit chat wins; otherwise fall back to last_chat_id, which every
    interactive caller persists before invoking the meta-agent (that is also
    where botctl-send-text routes its replies)."""
    if chat_id:
        return chat_id
    raw = load_state().get("last_chat_id")
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _signal_meta_child(proc, sig: int) -> None:
    """Signal the whole process group — claude spawns helper processes, and
    signalling only the direct child leaves them running."""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError, OSError) as exc:
        logger.debug("killpg(%s) failed (%s); falling back to direct signal", sig, exc)
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            pass


async def _terminate_meta_child(run: dict, reason: str) -> None:
    """TERM the claude child, escalate to KILL after META_KILL_GRACE_SEC."""
    proc = run["proc"]
    if proc.returncode is not None or run.get("stopping"):
        return
    run["stopping"] = True
    logger.warning("meta run stop (%s): TERM pid=%d", reason, proc.pid)
    _signal_meta_child(proc, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), META_KILL_GRACE_SEC)
    except asyncio.TimeoutError:
        logger.warning("meta child pid=%d ignored TERM; KILL", proc.pid)
        _signal_meta_child(proc, signal.SIGKILL)


async def _meta_progress_ping(run: dict) -> None:
    """One status message per run, edited in place — a new message every
    minute would bury the chat."""
    bot = _telegram_bot
    chat_id = run.get("chat_id")
    if bot is None or chat_id is None:
        return
    text = (
        f"⏳ работаю… {_meta_elapsed_min(run)} мин, /cancel чтобы остановить"
    )
    if text == run.get("progress_text"):
        return  # nothing changed — Telegram rejects an identical edit
    run["progress_text"] = text
    try:
        if run.get("progress_msg_id") is None:
            sent = await bot.send_message(chat_id, text)
            run["progress_msg_id"] = sent.message_id
        else:
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=run["progress_msg_id"],
            )
    except Exception as exc:  # noqa: BLE001 — a ping must never kill the run
        logger.debug("meta progress ping failed: %s", exc)


async def _meta_supervisor(run: dict) -> None:
    """Ping the chat every META_PROGRESS_SEC and stop the run once it exceeds
    META_TIMEOUT_SEC. Cancelled by _meta_run_end() when the child exits first.
    """
    deadline = run["started_at"] + META_TIMEOUT_SEC
    loop = asyncio.get_running_loop()
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            run["timed_out"] = True
            logger.warning(
                "%s-agent exceeded META_TIMEOUT_SEC=%d — stopping",
                run["label"], META_TIMEOUT_SEC,
            )
            await _terminate_meta_child(run, "timeout")
            return
        await asyncio.sleep(min(META_PROGRESS_SEC, remaining))
        if deadline - loop.time() > 0:
            await _meta_progress_ping(run)


def _meta_run_begin(proc, channel_label: str, chat_id: Optional[int]) -> dict:
    global _meta_run
    run = {
        "proc": proc,
        "label": channel_label,
        "chat_id": _resolve_chat_id(chat_id),
        "started_at": asyncio.get_running_loop().time(),
        "partial": [],
        "progress_msg_id": None,
        "timed_out": False,
        "cancelled": False,
        "stopping": False,
    }
    run["supervisor"] = asyncio.create_task(_meta_supervisor(run))
    _meta_run = run
    return run


async def _meta_run_end(run: dict) -> None:
    """Called only after the child is reaped, so cancelling the supervisor can
    no longer strand a process mid TERM→KILL escalation."""
    global _meta_run
    if run["proc"].returncode is None:
        # The reader bailed out with the child still alive — never leave it
        # behind, it would keep burning tokens with nobody listening.
        await _terminate_meta_child(run, "reader ended")
        await run["proc"].wait()
    run["supervisor"].cancel()
    try:
        await run["supervisor"]
    except asyncio.CancelledError:
        pass
    if run.get("progress_msg_id") is not None and _telegram_bot is not None:
        try:
            await _telegram_bot.delete_message(
                chat_id=run["chat_id"], message_id=run["progress_msg_id"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("meta progress cleanup failed: %s", exc)
    if _meta_run is run:
        _meta_run = None


async def _report_meta_stop(run: dict) -> None:
    """Tell the owner why the run ended and hand back whatever the agent
    managed to say — a killed run answers through no other channel."""
    bot = _telegram_bot
    chat_id = run.get("chat_id")
    if bot is None or chat_id is None:
        return
    if run["timed_out"]:
        head = (
            f"⏱ meta-запуск ({run['label']}) превысил "
            f"{_fmt_sec(META_TIMEOUT_SEC)} и был остановлен."
        )
    else:
        head = (
            f"⏹ meta-запуск ({run['label']}) остановлен по /cancel "
            f"через {_meta_elapsed_min(run)} мин."
        )
    partial = "\n\n".join(run["partial"][-10:]).strip()
    text = head if not partial else f"{head}\n\nЧастичный ответ:\n{partial[-3000:]}"
    try:
        await bot.send_message(chat_id, text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("meta stop report failed: %s", exc)


async def _ack_queued_behind_meta(update: Update) -> None:
    """A message that lands while a meta run holds meta_lock waits for it —
    say so instead of leaving the owner with silence (#8)."""
    if not meta_lock.locked():
        return
    run = _meta_run
    mins = _meta_elapsed_min(run) if run else 0
    try:
        await update.message.reply_text(
            f"⏳ принял, в очереди за текущей meta-задачей ({mins} мин), "
            f"/cancel её если срочно"
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("queue ack failed: %s", exc)


async def cancel_command(update: Update, context: object) -> None:
    """`/cancel` — TERM the claude child of the running meta-agent.

    Before this the only way out of a runaway run was killing the process by
    hand on the host (#8).
    """
    user_id = update.effective_user.id
    if user_id not in USER_REGISTRY:
        logger.warning("Silent reject: /cancel from unauthorized user_id=%d", user_id)
        return
    if not _meta_run_active():
        await context.bot.send_message(
            update.effective_chat.id,
            "🟢 Нечего отменять — активного meta-запуска нет.",
        )
        return
    run = _meta_run
    run["cancelled"] = True
    await context.bot.send_message(
        update.effective_chat.id,
        f"⏹ Останавливаю meta-запуск ({run['label']}, "
        f"{_meta_elapsed_min(run)} мин)…",
    )
    await _terminate_meta_child(run, "/cancel")


# ---------------------------------------------------------------------------
# Meta-agent invocation
# ---------------------------------------------------------------------------

async def run_meta_claude(
    meta_dir: str,
    prompt: str,
    user_id: int,
    session_key: str = "meta_session_id",
    channel_label: str = "meta",
    chat_id: Optional[int] = None,
) -> int:
    """Invoke claude CLI as a meta-agent and capture/resume its session.

    session_key controls which per-user session ID we resume — defaults to
    the regular Q&A session. /main passes its own key ("main_session_id")
    so the framework-self-dev channel stays independent of project Q&A.
    channel_label is used only for log lines.

    Returns the rc of the final attempt (0 = success) so callers like
    /main can decide what to report back. Existing callers that ignore
    the return value are unaffected.
    """
    state = load_state()
    user_key = str(user_id)
    session_id: Optional[str] = (
        state.get("users", {}).get(user_key, {}).get(session_key)
    )

    # Wrapped in a helper so we can retry without --resume if the stored
    # session_id has expired. Claude Code deletes transcripts after 30 days
    # by default (cleanupPeriodDays), and `claude --resume <gone-id>` emits
    # an error_during_execution result with "No conversation found with
    # session ID: ..." — compass §7 explicit warning. Pre-fix, the user got
    # no response at all on stale sessions; the fix detects the error and
    # falls through to a fresh session.
    rc, stopped = await _spawn_meta_claude(
        meta_dir=meta_dir,
        prompt=prompt,
        user_key=user_key,
        session_id=session_id,
        session_key=session_key,
        channel_label=channel_label,
        state=state,
        chat_id=chat_id,
    )
    # A timed-out / cancelled run also exits non-zero — retrying it would just
    # start the same runaway again from scratch (#8).
    if stopped:
        return rc
    if rc != 0 and session_id:
        logger.warning(
            "%s-agent rc=%d with --resume %s; retrying without session",
            channel_label, rc, session_id,
        )
        # Drop the stale id from state BEFORE retry so the new run captures
        # a fresh one cleanly.
        users = state.get("users", {})
        if user_key in users and session_key in users[user_key]:
            users[user_key][session_key] = ""
            save_state(state)
        rc, _ = await _spawn_meta_claude(
            meta_dir=meta_dir,
            prompt=prompt,
            user_key=user_key,
            session_id=None,
            session_key=session_key,
            channel_label=channel_label,
            state=state,
            chat_id=chat_id,
        )
    return rc


async def _spawn_meta_claude(
    *,
    meta_dir: str,
    prompt: str,
    user_key: str,
    session_id: Optional[str],
    session_key: str,
    channel_label: str,
    state: dict,
    chat_id: Optional[int] = None,
) -> tuple[int, bool]:
    """Single attempt of `claude -p` with optional --resume.

    Returns (rc, stopped) — `stopped` is True when the run was killed by the
    wall-clock cap or by /cancel, which tells the caller not to retry. Side
    effect: persists captured session_id to state.json on first event
    that carries one.

    The "session expired" signal lives in two places: (a) stderr emits
    `No conversation found with session ID: <id>`; (b) the result event
    has `subtype: error_during_execution` and `errors: [...]`. Both are
    treated as failure via the process rc=1; we surface either by
    returning that rc upward.
    """
    base = [
        CLAUDE_BIN, "--dangerously-skip-permissions",
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
    ]
    args = base[:2] + ["--resume", session_id] + base[2:] if session_id else base

    logger.info(
        "Starting %s-agent in %s (session %s, key=%s)",
        channel_label, meta_dir, session_id or "new", session_key,
    )

    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=meta_dir,
        # Allowlisted env (ai-delivery-private#13) — the meta-agent answers
        # questions about the repo and runs botctl-*; it does NOT need the
        # Telegram token (the bot talks to Telegram itself; botctl-* source
        # bot/.env on their own) nor any other operator secret. Passing no
        # env= at all used to hand it the bot's entire environment.
        env=build_child_env("anthropic"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Own process group so /cancel and the wall-clock cap can signal the
        # whole claude tree, not just the CLI wrapper (#8).
        start_new_session=True,
    )
    _meta_child = _proc_reaper.AsyncChild(proc, asyncio.get_running_loop())
    _proc_reaper.track(_meta_child, proc.pid)  # pgid == pid (start_new_session)
    run = _meta_run_begin(proc, channel_label, chat_id)

    async def _stream_stderr() -> None:
        assert proc.stderr is not None
        async for line in proc.stderr:
            logger.warning(
                "[%s stderr] %s",
                channel_label, line.decode(errors="replace").rstrip(),
            )

    stderr_task = asyncio.create_task(_stream_stderr())

    assert proc.stdout is not None
    session_captured = False
    try:
        async for line in proc.stdout:
            text = line.decode(errors="replace").strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                logger.debug("[%s stdout] %s", channel_label, text)
                continue

            event_str = json.dumps(event, ensure_ascii=False)
            if len(event_str) > META_EVENT_LOG_MAX:
                event_str = (
                    f"{event_str[:META_EVENT_LOG_MAX]}"
                    f"...<truncated, {len(event_str)} chars total>"
                )
            logger.debug("[%s event] %s", channel_label, event_str)

            if not session_captured and "session_id" in event:
                users = state.setdefault("users", {})
                users.setdefault(user_key, {})[session_key] = event["session_id"]
                save_state(state)
                session_captured = True
                logger.info(
                    "Captured %s=%s for user %s",
                    session_key, event["session_id"], user_key,
                )

            # Keep the agent's own text so a stopped run can still hand back
            # what it produced — botctl-send-text never fires on a killed run.
            if event.get("type") == "assistant":
                for block in (event.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        chunk = (block.get("text") or "").strip()
                        if chunk:
                            run["partial"].append(chunk)
                            del run["partial"][:-10]

        await proc.wait()
    finally:
        stderr_task.cancel()
        try:
            await stderr_task
        except asyncio.CancelledError:
            pass
        # Remove from the live-child registry and kill any process-group
        # leftovers the claude CLI may have left behind (FR-007, FR-019).
        _proc_reaper.untrack(_meta_child)
        _proc_reaper.kill_group_leftovers(_meta_child.pid)
        # Always drop the run record — a leaked one would make /cancel and the
        # queue ack lie about a run that is no longer there.
        await _meta_run_end(run)

    stopped = bool(run["timed_out"] or run["cancelled"])
    if stopped:
        await _report_meta_stop(run)

    logger.info("%s-agent exited with rc=%d", channel_label, proc.returncode)
    return (proc.returncode or 0), stopped


# ---------------------------------------------------------------------------
# Thin-mode: write spec.json to file-queue inbox
# ---------------------------------------------------------------------------

def _write_spec_to_inbox(
    prompt: str,
    user: str,
    chat_id: int,
    message_id: int,
    target_repo: Optional[str] = None,
) -> str:
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%d-%H%M%S")
    task_id = f"tg-{ts}-{secrets.token_hex(2)}"
    spec = {
        "trigger": "telegram",
        "user": user,
        "prompt": prompt,
        "target_repo": target_repo or BOT_DEFAULT_TARGET_REPO,
        "telegram_thread": {
            "chat_id": chat_id,
            "message_id": message_id,
        },
        "task_id": task_id,
        "created_at": now.isoformat().replace("+00:00", "Z"),
    }
    task_dir = THIN_MODE_INBOX / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    spec_path = task_dir / "spec.json"
    spec_path.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    logger.info("thin-mode: wrote spec %s (target=%s)", spec_path, spec["target_repo"])
    return task_id


# ---------------------------------------------------------------------------
# Thin-mode: approval prompt (5.G)
# ---------------------------------------------------------------------------

def _send_approval_prompt(context: object, task_id: str) -> None:
    """Send a [Да]/[Нет] inline keyboard for an awaiting-approval task.

    Reads ``state.json`` from ``awaiting-approval/<task_id>/`` and dispatches
    the approval prompt back into the original Telegram thread. The actual
    ``context.bot.send_message`` call is a coroutine, so we schedule it on
    the running event loop (the dispatcher bridge will call this from inside
    the bot's asyncio loop). Falls back to ``asyncio.run`` if there is no
    running loop (e.g. manual smoke test from a sync REPL).

    Raises ``FileNotFoundError`` if the state file is missing — caller
    decides how to handle.
    """
    state_path = THIN_MODE_AWAITING / task_id / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))

    thread = state.get("telegram_thread") or {}
    chat_id = thread.get("chat_id")
    if chat_id is None:
        raise ValueError(f"state.json for {task_id} missing telegram_thread.chat_id")
    message_id = thread.get("message_id")
    pr_url = state.get("pr_url") or "N/A"

    text = f"✓ APPROVE. PR: {pr_url}\nСлить (merge --squash)?"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Да", callback_data=f"approve:{task_id}"),
                InlineKeyboardButton("Нет", callback_data=f"deny:{task_id}"),
            ]
        ]
    )

    send_kwargs: dict = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": keyboard,
    }
    if message_id is not None:
        send_kwargs["reply_to_message_id"] = message_id

    coro = context.bot.send_message(**send_kwargs)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — caller is sync (smoke test). Run to completion.
        asyncio.run(coro)
    else:
        loop.create_task(coro)
    logger.info("thin-mode: scheduled approval prompt for %s (pr=%s)", task_id, pr_url)



async def rate_limit_callback(update: Update, context: object) -> None:
    """Handle rate-limit recovery inline-keyboard actions from admin."""
    query = update.callback_query
    if query is None:
        return

    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None or user_id not in USER_REGISTRY:
        try:
            await query.answer()
        except Exception:
            pass
        return

    await query.answer()

    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 2:
        logger.warning("rate_limit_callback: malformed data=%r", data)
        return
    action = parts[0]
    task_id = parts[1]

    task_dir = THIN_MODE_AWAITING_INPUT / task_id
    if not task_dir.is_dir():
        await query.edit_message_text(
            f"❌ Задача {task_id} не найдена в awaiting-input/. Возможно, уже обработана."
        )
        return

    if action == "rl_cancel":
        THIN_MODE_FAILED.mkdir(parents=True, exist_ok=True)
        dst = THIN_MODE_FAILED / task_id
        if dst.exists():
            shutil.rmtree(str(dst))
        shutil.move(str(task_dir), str(dst))
        await query.edit_message_text(f"❌ Задача {task_id} отменена. Перемещена в failed/.")
        logger.info("rate_limit_callback: cancelled task_id=%s", task_id)
        return

    if action == "rl_switch":
        # callback_data is "rl_switch:<task_id>:<failed_stage>[:<target_backend>]".
        # Legacy 3-part form defaults the target to deepseek (DeepSeek was the
        # only alternative pre-Block-5.2).
        if len(parts) < 3:
            await query.edit_message_text(f"❌ malformed callback_data: {data}")
            return
        failed_stage = parts[2]
        target_backend = parts[3] if len(parts) >= 4 else "deepseek"
        if target_backend not in ("anthropic", "deepseek", "glm"):
            await query.edit_message_text(
                f"❌ unknown backend in callback: {target_backend}"
            )
            return

        spec_path = task_dir / "spec.json"
        try:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            await query.edit_message_text(f"❌ Не удалось прочитать spec.json: {exc}")
            return

        routing = spec.setdefault("model_routing", {})
        routing[failed_stage] = target_backend
        spec_path.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        THIN_MODE_INBOX.mkdir(parents=True, exist_ok=True)
        dst = THIN_MODE_INBOX / task_id
        if dst.exists():
            shutil.rmtree(str(dst))
        shutil.move(str(task_dir), str(dst))

        pretty = {"anthropic": "Anthropic", "deepseek": "DeepSeek", "glm": "GLM"}[target_backend]
        await query.edit_message_text(
            f"🔄 Задача {task_id}: {failed_stage} переключена на {pretty}.\n"
            f"Задача возвращена в inbox, диспатчер перезахватит с новым роутингом."
        )
        logger.info(
            "rate_limit_callback: switched %s stage=%s to %s",
            task_id, failed_stage, target_backend,
        )
        return

    if action == "rl_windmill":
        if len(parts) < 3:
            await query.edit_message_text(f"❌ malformed callback_data: {data}")
            return
        resets_at = int(parts[2])

        spec_path = task_dir / "spec.json"
        try:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            await query.edit_message_text(f"❌ Не удалось прочитать spec.json: {exc}")
            return

        prompt = spec.get("prompt", "")
        target_repo = spec.get("target_repo", BOT_DEFAULT_TARGET_REPO)

        dt = datetime.fromtimestamp(resets_at, tz=timezone.utc)
        # One-shot cron. Windmill (CE) requires a 6-field cron with a leading
        # SECONDS field — a bare 5-field "min hour dom mon dow" is rejected with
        # HTTP 400 ("Bad request: cron"). Prepend "0 " for the seconds slot.
        cron = f"0 {dt.minute} {dt.hour} {dt.day} {dt.month} *"

        windmill_token = os.environ.get("WINDMILL_TOKEN", "").strip()
        if not windmill_token:
            await query.edit_message_text("❌ WINDMILL_TOKEN не настроен в .env бота")
            return

        base_url = os.environ.get("WINDMILL_BASE_URL", "http://localhost").rstrip("/")
        url = f"{base_url}/api/w/ai-delivery/schedules/create"
        schedule_name = f"rl-retry-{task_id}"

        payload = {
            "path": f"f/ai_delivery/{schedule_name}",
            "schedule": cron,
            "timezone": "Europe/Warsaw",
            "script_path": "f/ai_delivery/pipeline_trigger",
            "is_flow": True,
            "args": {
                "prompt": prompt,
                "target_repo": target_repo,
            },
            "enabled": True,
        }
        headers = {
            "Authorization": f"Bearer {windmill_token}",
            "Content-Type": "application/json",
        }

        logger.info("rate_limit_callback: creating Windmill schedule %s cron=%s", schedule_name, cron)

        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    status = resp.status
                    body = await resp.text()
        except Exception as exc:
            logger.exception("rate_limit_callback: Windmill request failed: %s", exc)
            await query.edit_message_text(f"❌ Windmill request failed: {exc!r}")
            return

        if status in (200, 201):
            dt_str = dt.strftime("%Y-%m-%d %H:%M UTC")
            await query.edit_message_text(
                f"⏰ Задача {task_id} запланирована в Windmill на {dt_str}.\n"
                f"После сброса лимита запустится автоматически с оригинальным провайдером (anthropic).\n"
                f"Schedule: {schedule_name}"
            )
            logger.info("rate_limit_callback: Windmill schedule %s created for %s", schedule_name, task_id)
        else:
            await query.edit_message_text(f"❌ Windmill {status}: {body[:200]}")
            logger.warning("rate_limit_callback: Windmill create failed status=%d body=%s", status, body[:500])
        return

    await query.edit_message_text(f"❌ Неизвестное действие: {action}")


async def _append_memory_bank_entry(
    task_dir: Path, pr_url: str,
) -> tuple[bool, str]:
    """After a successful merge, append a one-line entry to the target repo's
    memory-bank/current-state.md so the next pipeline run starts with fresh
    context. Best-effort: any failure is logged and returns (False, reason)
    without breaking the merge flow.

    Returns (ok, detail). detail is the log line on success or the error on
    failure.
    """
    try:
        spec = json.loads((task_dir / "spec.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"spec.json read failed: {exc}"

    target_repo = spec.get("target_repo") or ""
    if not target_repo:
        return False, "spec.target_repo missing"

    target_path = Path(target_repo)
    bank = target_path / "memory-bank" / "current-state.md"
    if not bank.exists():
        return False, f"no memory-bank/current-state.md at {target_path}"

    if not (target_path / ".git").exists():
        return False, f"{target_path} is not a git repo"

    # Build the entry. One line so the file doesn't bloat over time.
    task_id = task_dir.name
    prompt = (spec.get("prompt") or "").strip().splitlines()[0][:120]
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    entry = f"- {ts} — `{task_id}` — {prompt} — merged via {pr_url}"

    content = bank.read_text(encoding="utf-8")
    section = "## Recent merged changes (auto-logged)"
    if section in content:
        # Split the file into [head, section_body, trailer]:
        #   head    = everything up to and including the section header line
        #   body    = the auto-logged entries we currently maintain
        #   trailer = the next "## " section (if any) and the rest of the file
        head, _, after = content.partition(section)
        after_lines = after.splitlines()
        # Find the next sibling "## " header, if any.
        next_idx = next(
            (i for i, ln in enumerate(after_lines) if ln.startswith("## ")),
            None,
        )
        body_lines = after_lines if next_idx is None else after_lines[:next_idx]
        trailer_lines = [] if next_idx is None else after_lines[next_idx:]

        # Collect only entries belonging to *our* section (lines starting with "- ").
        existing_entries = [ln for ln in body_lines if ln.startswith("- ")]
        # Prepend the new entry, cap at 50 to avoid unbounded growth.
        kept = [entry] + existing_entries[:49]

        new_body = "\n" + "\n".join(kept) + "\n"
        trailer = ""
        if trailer_lines:
            trailer = "\n" + "\n".join(trailer_lines) + "\n"
        new_content = head + section + new_body + trailer
        if not new_content.endswith("\n"):
            new_content += "\n"
    else:
        # Add the section at the end of the file.
        sep = "" if content.endswith("\n") else "\n"
        new_content = content + sep + "\n" + section + "\n\n" + entry + "\n"

    try:
        bank.write_text(new_content, encoding="utf-8")
    except OSError as exc:
        return False, f"write failed: {exc}"

    # Commit + push. main may have a branch-protection rule blocking direct
    # pushes; in that case we still leave the file edited locally — admin can
    # pick it up by hand. Don't fail merge confirmation either way.
    try:
        for cmd in (
            ["git", "-C", str(target_path), "add", "memory-bank/current-state.md"],
            ["git", "-C", str(target_path), "commit", "-m",
             f"docs(memory-bank): record merged task {task_id}\n\nPR: {pr_url}"],
            ["git", "-C", str(target_path), "push"],
        ):
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err_b = await proc.communicate()
            if proc.returncode != 0:
                err = err_b.decode(errors="replace").strip()
                return False, f"{cmd[2]} {cmd[3]} failed rc={proc.returncode}: {err[:200]}"
    except Exception as exc:  # noqa: BLE001 — best-effort, never crash merge flow
        return False, f"git invocation failed: {exc!r}"

    return True, f"appended entry, pushed to {target_repo}"


async def approval_callback(update: Update, context: object) -> None:
    """Handle Да/Нет inline-keyboard taps for an awaiting-approval task."""
    query = update.callback_query
    if query is None:
        return

    # Auth gate — mirror handle_text: silent reject for unknown users.
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None or user_id not in USER_REGISTRY:
        logger.warning(
            "Silent reject: callback from unauthorized user_id=%s chat_id=%s",
            user_id, getattr(update.effective_chat, "id", None),
        )
        # Still answer the query so the spinner stops on the Telegram client.
        try:
            await query.answer()
        except Exception:  # noqa: BLE001 - best-effort
            pass
        return

    await query.answer()

    data = query.data or ""
    if ":" not in data:
        logger.warning("approval_callback: malformed callback_data=%r", data)
        return
    action, task_id = data.split(":", 1)

    task_dir = THIN_MODE_AWAITING / task_id

    if action == "deny":
        await query.edit_message_text(
            f"Отмена. Задача {task_id} осталась в awaiting-approval/. Решай вручную."
        )
        logger.info("approval_callback: deny task_id=%s", task_id)
        return

    if action != "approve":
        logger.warning("approval_callback: unknown action=%r task_id=%s", action, task_id)
        return

    state_path = task_dir / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        await query.edit_message_text(
            f"❌ Не удалось прочитать state.json для {task_id}: {exc}"
        )
        return

    pr_url = state.get("pr_url")
    if not pr_url:
        await query.edit_message_text(
            f"❌ В state.json нет pr_url для {task_id}. Слить нечего."
        )
        return

    proc = await asyncio.create_subprocess_exec(
        "gh", "pr", "merge", "--squash", pr_url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    rc = proc.returncode if proc.returncode is not None else -1
    stdout = stdout_b.decode(errors="replace")
    stderr = stderr_b.decode(errors="replace")

    if rc == 0:
        # Auto-log the merged change to the target repo's memory-bank BEFORE
        # moving the task dir (helper still needs spec.json from awaiting/).
        bank_ok, bank_detail = await _append_memory_bank_entry(task_dir, pr_url)
        if bank_ok:
            logger.info(
                "approval_callback: memory-bank updated task_id=%s detail=%s",
                task_id, bank_detail,
            )
        else:
            logger.warning(
                "approval_callback: memory-bank update skipped task_id=%s reason=%s",
                task_id, bank_detail,
            )

        try:
            THIN_MODE_DONE.mkdir(parents=True, exist_ok=True)
            shutil.move(str(task_dir), str(THIN_MODE_DONE / task_id))
        except OSError as exc:
            logger.error("approval_callback: merged but failed to move %s: %s", task_id, exc)
            await query.edit_message_text(
                f"✓ Merged.\nPR: {pr_url}\n(не удалось переместить папку: {exc})"
            )
            return
        suffix = f"\n📒 memory-bank updated" if bank_ok else ""
        await query.edit_message_text(f"✓ Merged.\nPR: {pr_url}{suffix}")
        logger.info("approval_callback: merged task_id=%s pr=%s", task_id, pr_url)
    else:
        await query.edit_message_text(
            f"❌ Merge failed (rc={rc}):\n{stderr[:500]}"
        )
        logger.warning(
            "approval_callback: merge failed task_id=%s rc=%d stderr=%s stdout=%s",
            task_id, rc, stderr[:500], stdout[:500],
        )


# ---------------------------------------------------------------------------
# /memo + /recall — semantic long-term memory (Qdrant + FastEmbed)
# ---------------------------------------------------------------------------
# Replaces the Ollama-based stack from Phase X (qwen2.5:14b + bge-m3, ~9.5 GB
# disk, 30-60 s per fact). FastEmbed runs ONNX locally, ~2 GB cached on
# first use, sub-100 ms per embedding on CPU. SOTA multilingual (RU+EN) via
# `intfloat/multilingual-e5-large`; override with MEMO_EMBED_MODEL +
# MEMO_EMBED_DIMS for any model in fastembed.TextEmbedding.list_supported_models().

MEMO_QDRANT_URL = os.environ.get("MEMO_QDRANT_URL", "http://127.0.0.1:6333")
MEMO_COLLECTION = os.environ.get("MEMO_COLLECTION", "meta_agent_mem")
MEMO_EMBED_MODEL = os.environ.get(
    "MEMO_EMBED_MODEL",
    "intfloat/multilingual-e5-large",
)
MEMO_EMBED_DIMS = int(os.environ.get("MEMO_EMBED_DIMS", "1024"))

_embedder = None
_collection_initialized = False


def _get_embedder():
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding
        logger.info(
            "memo: loading FastEmbed model %s (first call may download ~2 GB)",
            MEMO_EMBED_MODEL,
        )
        _embedder = TextEmbedding(model_name=MEMO_EMBED_MODEL)
    return _embedder


def _embed_text(text: str) -> list[float]:
    embedder = _get_embedder()
    return list(next(iter(embedder.embed([text]))))


async def _ensure_collection(session: aiohttp.ClientSession) -> None:
    global _collection_initialized
    if _collection_initialized:
        return
    async with session.get(f"{MEMO_QDRANT_URL}/collections/{MEMO_COLLECTION}") as r:
        if r.status == 200:
            _collection_initialized = True
            return
    body = {"vectors": {"size": MEMO_EMBED_DIMS, "distance": "Cosine"}}
    async with session.put(
        f"{MEMO_QDRANT_URL}/collections/{MEMO_COLLECTION}", json=body
    ) as r:
        if r.status not in (200, 201):
            raise RuntimeError(
                f"Qdrant collection create failed: {r.status} {await r.text()}"
            )
    _collection_initialized = True


async def memo_command(update: Update, context: object) -> None:
    """/memo <text> — store a fact in long-term semantic memory."""
    user_id = update.effective_user.id
    if user_id not in USER_REGISTRY:
        return

    user = USER_REGISTRY[user_id]
    text = update.message.text.strip()
    if text.startswith("/memo"):
        text = text[5:].strip()
    if not text:
        await context.bot.send_message(
            update.effective_chat.id,
            "Использование: `/memo <факт>` — сохранить в долговременную память",
        )
        return

    logger.info("memo: storing fact from %s: %s", user["name"], text[:100])

    try:
        loop = asyncio.get_running_loop()
        vector = await loop.run_in_executor(None, _embed_text, text)

        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            await _ensure_collection(session)

            point_id = uuid.uuid4().hex
            payload = {
                "points": [{
                    "id": point_id,
                    "vector": vector,
                    "payload": {
                        "text": text,
                        "user": user["name"],
                        "source": "telegram",
                        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    },
                }],
            }
            async with session.put(
                f"{MEMO_QDRANT_URL}/collections/{MEMO_COLLECTION}/points?wait=true",
                json=payload,
            ) as resp:
                qdrant_resp = await resp.json()

        if qdrant_resp.get("result", {}).get("status") == "completed":
            await context.bot.send_message(
                update.effective_chat.id,
                f"✓ Запомнил: {text[:200]}",
            )
            logger.info("memo: stored fact id=%s user=%s", point_id, user["name"])
        else:
            await context.bot.send_message(
                update.effective_chat.id,
                f"❌ Qdrant: {qdrant_resp}",
            )
    except Exception as exc:
        logger.exception("memo: failed to store fact")
        await context.bot.send_message(
            update.effective_chat.id,
            f"❌ Ошибка: {exc!r}",
        )


async def recall_command(update: Update, context: object) -> None:
    """/recall <query> — semantic search across stored memos (top-5)."""
    user_id = update.effective_user.id
    if user_id not in USER_REGISTRY:
        return

    text = update.message.text.strip()
    if text.startswith("/recall"):
        text = text[7:].strip()
    if not text:
        await context.bot.send_message(
            update.effective_chat.id,
            "Использование: `/recall <запрос>` — семантический поиск по памяти",
        )
        return

    try:
        loop = asyncio.get_running_loop()
        vector = await loop.run_in_executor(None, _embed_text, text)

        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            await _ensure_collection(session)

            async with session.post(
                f"{MEMO_QDRANT_URL}/collections/{MEMO_COLLECTION}/points/search",
                json={"vector": vector, "limit": 5, "with_payload": True},
            ) as resp:
                if resp.status != 200:
                    await context.bot.send_message(
                        update.effective_chat.id,
                        f"❌ Qdrant {resp.status}",
                    )
                    return
                data = await resp.json()

        hits = data.get("result", [])
        if not hits:
            await context.bot.send_message(
                update.effective_chat.id,
                "Ничего не вспомнил.",
            )
            return

        lines = [f"🔎 По запросу «{text[:60]}»:\n"]
        for h in hits:
            score = h.get("score", 0.0)
            pl = h.get("payload", {})
            t = pl.get("text", "")
            ts = pl.get("timestamp", "")[:10]
            lines.append(f"• [{score:.2f}] {ts} — {t[:200]}")
        await context.bot.send_message(
            update.effective_chat.id,
            "\n".join(lines),
        )
    except Exception as exc:
        logger.exception("recall: failed")
        await context.bot.send_message(
            update.effective_chat.id,
            f"❌ Ошибка: {exc!r}",
        )


# ---------------------------------------------------------------------------
# /usage command — delegates to ccusage CLI for Claude Code session costs
# ---------------------------------------------------------------------------

TASKS_ROOT = Path.home() / "projects" / "ai-delivery" / "tasks"
USAGE_BUCKETS = ("active", "awaiting-approval", "awaiting-input", "done", "failed")


async def usage_command(update: Update, context: object) -> None:
    """`/usage [today|week|month|all]` — cost report via ccusage CLI."""
    user_id = update.effective_user.id
    if user_id not in USER_REGISTRY:
        logger.warning(
            "Silent reject: /usage from unauthorized user_id=%d", user_id,
        )
        return

    body = (update.message.text or "").removeprefix("/usage").strip().lower()
    window = body if body in ("today", "week", "month", "all") else "today"

    try:
        today = datetime.now(timezone.utc).strftime("%Y%m%d")

        if window == "today":
            args = ["npx", "ccusage", "daily", "--json",
                    "--since", today, "--until", today]
        elif window == "week":
            since = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y%m%d")
            args = ["npx", "ccusage", "daily", "--json",
                    "--since", since, "--until", today]
        elif window == "month":
            since = (datetime.now(timezone.utc) - timedelta(days=29)).strftime("%Y%m%d")
            args = ["npx", "ccusage", "daily", "--json",
                    "--since", since, "--until", today]
        else:  # all
            args = ["npx", "ccusage", "daily", "--json"]

        result = subprocess.run(args, capture_output=True, text=True, timeout=30)

        if result.returncode != 0:
            raise RuntimeError(
                f"ccusage exit {result.returncode}: {result.stderr.strip()}"
            )

        data = json.loads(result.stdout)
        periods = data.get("daily") or []
        totals = data.get("totals") or {}

        label = {
            "today": "сегодня", "week": "за 7 дней",
            "month": "за 30 дней", "all": "за всё время",
        }[window]

        total_cost = float(totals.get("totalCost") or 0)
        lines = [f"📊 ccusage — {label}", f"Total: ${total_cost:.4f}"]

        recent = periods[-7:]
        if recent:
            lines.append("")
            for entry in recent:
                period = entry.get("period", "?")
                cost = entry.get("totalCost", 0)
                tokens = entry.get("totalTokens", 0)
                lines.append(
                    f"  {period}  ${cost:.4f}  ({tokens / 1000:.0f}k tok)"
                )

        model_costs: dict[str, dict] = {}
        for entry in periods:
            for mb in entry.get("modelBreakdowns") or []:
                name = mb.get("modelName", "unknown")
                row = model_costs.setdefault(name, {"cost": 0.0, "count": 0})
                row["cost"] += float(mb.get("cost") or 0)
                row["count"] += 1

        if model_costs:
            lines.append("")
            lines.append("По моделям:")
            for name, row in sorted(
                model_costs.items(), key=lambda x: x[1]["cost"], reverse=True,
            ):
                lines.append(
                    f"  {name}  ${row['cost']:.4f}  ({row['count']} period)"
                )

        text = "\n".join(lines)
    except Exception as exc:
        logger.exception("usage_command failed")
        text = f"❌ /usage error: {exc!r}"

    await context.bot.send_message(update.effective_chat.id, text)


# ---------------------------------------------------------------------------
# /main — direct framework-self-development channel
# ---------------------------------------------------------------------------

AI_DELIVERY_ROOT = Path.home() / "projects" / "ai-delivery"


async def _git_capture(*args: str, cwd: str) -> str:
    """Run `git <args>` in cwd, return stripped stdout. Empty on any failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", cwd, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            return ""
        return out.decode(errors="replace").strip()
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("git %s failed: %s", " ".join(args), exc)
        return ""


async def main_command(update: Update, context: object) -> None:
    """`/main <prompt>` — direct channel for framework self-development.

    Bypasses both the BA→…→Review pipeline (which is for target projects)
    and the regular Q&A session (which is scoped to investigating a target
    project's code). /main converses with a meta-agent whose cwd is the
    ai-delivery repo itself, with its own per-user session ID so the
    framework conversation never mixes with project Q&A.

    Use it for: editing bot.py, dispatcher/, .claude/agents/, ROADMAP,
    refining system prompts, adding new commands — anything that improves
    the framework itself rather than a downstream project.
    """
    user_id = update.effective_user.id
    if user_id not in USER_REGISTRY:
        logger.warning(
            "Silent reject: /main from unauthorized user_id=%d", user_id,
        )
        return

    user = USER_REGISTRY[user_id]
    body = (update.message.text or "").removeprefix("/main").strip()
    if not body:
        await context.bot.send_message(
            update.effective_chat.id,
            "Использование: /main <запрос на доработку фреймворка>\n"
            "Пример: /main добавь команду /usage week — показать траты за неделю\n"
            "/main работает в репо ai-delivery и пишет код прямо туда.",
        )
        return

    corr_id = uuid.uuid4().hex[:8]
    logger.info("[%s] /main from %s: %s", corr_id, user["name"], body)

    state = load_state()
    state["last_user"] = user["name"]
    state["last_chat_id"] = str(update.effective_chat.id)
    state["last_input_type"] = "main"
    state["last_correlation_id"] = corr_id
    save_state(state)

    framing = (
        f"[FROM {user['name']} via /main]\n"
        f"You are working inside the ai-delivery framework repo itself "
        f"(cwd is {AI_DELIVERY_ROOT}). The user is asking you to modify "
        f"the framework — not a target project. Treat this conversation "
        f"as continuous self-development:\n"
        f"- Read STATE/CURRENT.md and STATE/ROADMAP.md to anchor context\n"
        f"- Read CLAUDE.md if it exists, then ARCHITECTURE.md\n"
        f"- All code changes go through git commit (Ubuntu→server bridge)\n"
        f"- For non-trivial changes, update STATE/CURRENT.md afterwards\n\n"
        f"Request: {body}"
    )

    preview = body if len(body) <= 80 else body[:80] + "…"
    try:
        await context.bot.send_message(
            update.effective_chat.id,
            f"🔧 /main принят [{corr_id}]: {preview} — работаю в ai-delivery…",
        )
    except Exception as exc:
        logger.warning("[%s] /main start-ack failed: %s", corr_id, exc)

    repo = str(AI_DELIVERY_ROOT)
    await _ack_queued_behind_meta(update)
    async with meta_lock:
        head_before = await _git_capture("rev-parse", "HEAD", cwd=repo)
        rc = await run_meta_claude(
            meta_dir=repo,
            prompt=framing,
            user_id=user_id,
            session_key="main_session_id",
            channel_label="main",
            chat_id=update.effective_chat.id,
        )
        head_after = await _git_capture("rev-parse", "HEAD", cwd=repo)

    new_commits = ""
    if rc == 0 and head_before and head_after and head_after != head_before:
        new_commits = await _git_capture(
            "log", f"{head_before}..{head_after}", "--format=%h %s",
            cwd=repo,
        )

    if rc == 0 and new_commits:
        final_text = f"✅ /main готово [{corr_id}]:\n{new_commits}"
    elif rc == 0:
        final_text = f"✅ /main готово [{corr_id}] — без коммита"
    else:
        final_text = f"⚠️ /main rc={rc} [{corr_id}] — без коммита, см. логи"

    try:
        await context.bot.send_message(update.effective_chat.id, final_text)
    except Exception as exc:
        logger.warning("[%s] /main final-ack failed: %s", corr_id, exc)


# ---------------------------------------------------------------------------
# /help — discoverability
# ---------------------------------------------------------------------------

HELP_TEXT = """🤖 *ai-delivery — справка*

*Команды* (Telegram-канал):
• `/task [@alias] <текст>` — задача в pipeline (BA→Arch→Dev→Test→Sec→Reviewer). `@alias` выбирает целевой репо (см. `/projects`); без `@` — в проект по умолчанию. Открывает PR, спрашивает [Да]/[Нет].
• `/stt` — *переключатель* режима «голос→текст». Включил → любое голосовое/аудио возвращает только распознанный текст (без задач и вопросов); `/stt` ещё раз — выключил. Разово: ответь `/stt` на конкретное голосовое/аудио. ⚠️ Требует поднятого `services/stacks/voice/`.
• `/projects` — список известных алиасов и куда они указывают.
• `/refresh_code [@alias]` — принудительный re-index CodeGraph для target-репо (escape hatch когда watcher confused). Без `@alias` — default project.
• `/main <текст>` — прямой канал к Claude в репо ai-delivery (фреймворк сам себя развивает).
• `/usage [today|week|all]` — отчёт по тратам, по стадиям и бэкендам.
• `/memo <факт>` — *вручную* записать в семантическую память.
• `/recall <запрос>` — *вручную* поискать по памяти (top-5 cosine).
• `/schedule <name> <cron> <prompt>` — повторяющаяся задача через Windmill.
• `/cancel` — остановить текущий meta-запуск (Q&A/`/main`). Сам запуск и так ограничен `META_TIMEOUT_SEC` (по умолчанию 10 мин), пинг о прогрессе — раз в `META_PROGRESS_SEC`.
• `/help` — эта справка.

*Без команды* (просто текст):
Любое сообщение → meta-агент отвечает по target-проекту. Контекст memory-bank/ + mem0 подкидывается автоматически.

*Голос*:
• Обычный режим: голосовое → Whisper STT → если начинается с «таск/task» → роутится в `/task`, иначе вопрос мета-агенту.
• Нужен *только текст* → нажми `/stt` (режим распознавания ВКЛ). Теперь каждое голосовое/аудио возвращает просто текст, без задач и вопросов. `/stt` ещё раз — вернуться в обычный режим. Разово, не трогая режим: ответь `/stt` на конкретное голосовое/аудио.
⚠️ Любой голос/аудио требует поднятого `services/stacks/voice/`.

*Память*:
• Конвейер помнит сам: каждая завершённая задача пишет типизированный урок (`task_lesson`) по своему репозиторию, и стадии ba/architect/developer следующих задач получают уроки этого репозитория прямо в промпт.
• `/memo` / `/recall` — ручная семантическая память поверх того же хранилища: фиксируй факты, которые в задачах не всплывут.

*После merge*:
`memory-bank/current-state.md` в target-репо авто-апдейтится строкой про задачу. Это per-project markdown-память, в git.
"""


async def help_command(update: Update, context: object) -> None:
    user_id = update.effective_user.id
    if user_id not in USER_REGISTRY:
        logger.warning(
            "Silent reject: /help from unauthorized user_id=%d chat_id=%s",
            user_id, getattr(update.effective_chat, "id", None),
        )
        return
    await context.bot.send_message(
        update.effective_chat.id,
        HELP_TEXT,
        parse_mode="Markdown",
        reply_markup=_main_keyboard(),
    )


# ---------------------------------------------------------------------------
# Persistent reply keyboard + native /-menu (set on bot startup)
# ---------------------------------------------------------------------------

def _main_keyboard() -> ReplyKeyboardMarkup:
    """Visible 6-button keyboard pinned below the chat input.

    Tap → the literal text (e.g. `/task`) appears in the input field;
    user types the body and presses Send. Buttons for verb-only flow
    (`/usage`, `/help`) submit on first tap. `resize_keyboard=True`
    makes the keyboard match the chat width — looks native on phones.
    """
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("/task"), KeyboardButton("/projects")],
            [KeyboardButton("/main"), KeyboardButton("/usage")],
            [KeyboardButton("/memo"), KeyboardButton("/recall")],
            [KeyboardButton("/help")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


async def projects_command(update: Update, context: object) -> None:
    """`/projects` — list registered aliases and their target paths."""
    user_id = update.effective_user.id
    if user_id not in USER_REGISTRY:
        logger.warning(
            "Silent reject: /projects from unauthorized user_id=%d", user_id,
        )
        return
    registry = _load_projects()
    projects = _registry.parse_projects(registry)
    default = _registry.default_alias(registry)
    if not projects:
        await context.bot.send_message(
            update.effective_chat.id,
            "Реестр пуст. Создай <code>bot/projects.json</code> по образцу "
            "<code>bot/projects.example.json</code> и рестартани бот.",
        )
        return
    lines = ["<b>Известные проекты</b> (alias → path):", ""]
    for alias in sorted(projects):
        marker = " ← default" if alias == default else ""
        base = projects[alias].base
        base_note = f"\nbase: <code>{base}</code>" if base else ""
        lines.append(
            f"<b>@{alias}</b>{marker}\n<code>{projects[alias].path}</code>{base_note}")
    lines.append("")
    lines.append("Использование: <code>/task @&lt;alias&gt; &lt;описание&gt;</code>")
    await context.bot.send_message(
        update.effective_chat.id,
        "\n".join(lines),
        parse_mode="HTML",
    )


async def refresh_code_command(update: Update, context: object) -> None:
    """`/refresh-code [@alias]` — force a full CodeGraph re-index of the
    target repo. Escape hatch for when the file watcher gets confused
    (rare). With no @alias, defaults to projects.json _default.
    """
    user_id = update.effective_user.id
    if user_id not in USER_REGISTRY:
        logger.warning(
            "Silent reject: /refresh-code from unauthorized user_id=%d", user_id,
        )
        return

    chat_id = update.effective_chat.id
    body = (update.message.text or "")
    for prefix in ("/refresh-code", "/refresh_code"):
        if body.startswith(prefix):
            body = body[len(prefix):]
            break
    _, target_repo, alias = _resolve_target_repo(body.strip())

    if target_repo.startswith("@UNKNOWN:"):
        registry = _load_projects()
        known = sorted(_registry.project_paths(registry))
        await context.bot.send_message(
            chat_id,
            f"❌ Неизвестный alias <code>@{alias}</code>. Доступны: "
            f"{', '.join(known) or '(пусто)'}",
            parse_mode="HTML",
        )
        return

    target_path = Path(target_repo)
    if not target_path.is_dir():
        await context.bot.send_message(
            chat_id,
            f"❌ Path не существует: <code>{target_repo}</code>",
            parse_mode="HTML",
        )
        return

    if not (target_path / ".codegraph").is_dir():
        await context.bot.send_message(
            chat_id,
            f"❌ <code>{target_repo}</code> не инициализирован под CodeGraph.\n"
            f"На хосте: <code>ops/setup-codegraph.sh {target_repo}</code>",
            parse_mode="HTML",
        )
        return

    label = f"@{alias}" if alias else target_repo
    await context.bot.send_message(
        chat_id,
        f"🔄 CodeGraph re-index <code>{label}</code> — codegraph index --force",
        parse_mode="HTML",
    )

    # systemd-managed bot may not have ~/.npm-global/bin in its PATH —
    # prepend it so the global codegraph install is reachable.
    npm_global_bin = os.path.expanduser("~/.npm-global/bin")
    env = {**os.environ, "PATH": f"{npm_global_bin}:{os.environ.get('PATH', '')}"}

    started = datetime.now(timezone.utc)
    try:
        result = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                ["codegraph", "index", "--force"],
                cwd=str(target_path),
                capture_output=True, text=True, timeout=600, env=env,
            ),
        )
    except subprocess.TimeoutExpired:
        await context.bot.send_message(
            chat_id,
            f"❌ codegraph index timeout (>10 мин) для <code>{target_repo}</code>",
            parse_mode="HTML",
        )
        return
    except FileNotFoundError:
        await context.bot.send_message(
            chat_id,
            "❌ <code>codegraph</code> бинарь не в PATH. Установи: "
            "<code>npm install -g @colbymchenry/codegraph</code>",
            parse_mode="HTML",
        )
        return

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    if result.returncode != 0:
        tail = (result.stderr or "no stderr").strip().splitlines()[-6:]
        await context.bot.send_message(
            chat_id,
            f"❌ codegraph index exit={result.returncode} ({elapsed:.1f}s)\n"
            f"<pre>{chr(10).join(tail)[:500]}</pre>",
            parse_mode="HTML",
        )
        return

    tail = (result.stdout or "").strip().splitlines()[-6:]
    await context.bot.send_message(
        chat_id,
        f"✅ CodeGraph re-indexed <code>{label}</code> in {elapsed:.1f}s\n"
        f"<pre>{chr(10).join(tail)[:500]}</pre>",
        parse_mode="HTML",
    )


async def start_command(update: Update, context: object) -> None:
    """`/start` — show the persistent keyboard. Telegram's default greeting."""
    user_id = update.effective_user.id
    if user_id not in USER_REGISTRY:
        logger.warning(
            "Silent reject: /start from unauthorized user_id=%d", user_id,
        )
        return
    await context.bot.send_message(
        update.effective_chat.id,
        "Привет. Клавиатура внизу — основные команды. /help — полная справка.",
        reply_markup=_main_keyboard(),
    )


async def _publish_bot_commands(bot) -> None:
    """Push the autocomplete menu shown when the user taps `/` in chat.

    This is separate from the keyboard above: it surfaces commands +
    one-line descriptions inside Telegram's native picker UI. Persists
    server-side — only needs to be called once per bot lifetime, but
    re-publishing on every restart is cheap and keeps the menu in sync
    with the running code.
    """
    commands = [
        BotCommand("task",     "Pipeline в проект: /task @alias описание"),
        BotCommand("stt",      "Вкл/выкл режим «голос/аудио → текст»"),
        BotCommand("projects", "Список целевых проектов"),
        BotCommand("refresh_code", "Принудительный re-index CodeGraph [@alias]"),
        BotCommand("main",     "Прямой канал к Claude в этом репо"),
        BotCommand("memo",     "Сохранить факт в долговременную память"),
        BotCommand("recall",   "Найти в памяти по смыслу"),
        BotCommand("usage",    "Отчёт по тратам"),
        BotCommand("cancel",   "Остановить текущий meta-запуск"),
        BotCommand("schedule", "Повторяющаяся задача через Windmill"),
        BotCommand("help",     "Полная справка"),
        BotCommand("start",    "Показать клавиатуру"),
    ]
    try:
        await bot.set_my_commands(commands)
        logger.info("Telegram /-menu: registered %d commands", len(commands))
    except Exception as exc:
        logger.warning("Telegram /-menu: set_my_commands failed: %s", exc)


# ---------------------------------------------------------------------------
# Telegram handler
# ---------------------------------------------------------------------------

async def task_command(update: Update, context: object) -> None:
    """`/task <prompt>` — explicit pipeline trigger.

    Writes spec.json to the file-queue inbox. The dispatcher daemon picks it up,
    runs the BA→Architect→Developer→Tester→Security→Reviewer pipeline, and
    opens a PR against the target repo.

    Without /task prefix, plain text falls through to handle_text → meta-agent Q&A.
    """
    user_id = update.effective_user.id
    if user_id not in USER_REGISTRY:
        logger.warning(
            "Silent reject: /task from unauthorized user_id=%d chat_id=%s",
            user_id, getattr(update.effective_chat, "id", None),
        )
        return

    user = USER_REGISTRY[user_id]
    body = (update.message.text or "").removeprefix("/task").strip()
    if not body:
        registry = _load_projects()
        projects = _registry.project_paths(registry)
        default = _registry.default_alias(registry) or "—"
        aliases_line = ", ".join(f"@{a}" for a in sorted(projects)) or "—"
        await context.bot.send_message(
            update.effective_chat.id,
            "Использование:\n"
            "  /task <описание>                — в проект по умолчанию\n"
            "  /task @<alias> <описание>       — в конкретный проект\n\n"
            f"Известные алиасы: {aliases_line}\n"
            f"По умолчанию: @{default}\n"
            "Пример: /task @sandbox добавь divide(a,b) в app/calc.py с тестами",
        )
        return

    body, target_repo, alias = _resolve_target_repo(body)
    if target_repo.startswith("@UNKNOWN:"):
        bad_alias = target_repo[len("@UNKNOWN:"):]
        registry = _load_projects()
        projects = _registry.project_paths(registry)
        aliases_line = ", ".join(f"@{a}" for a in sorted(projects)) or "—"
        await context.bot.send_message(
            update.effective_chat.id,
            f"❌ Неизвестный алиас: @{bad_alias}\n"
            f"Известные: {aliases_line}\n"
            "Добавить новый: правка bot/projects.json + рестарт бота.",
        )
        return
    if not body:
        await context.bot.send_message(
            update.effective_chat.id,
            f"❌ После @{alias} нужно описание задачи.",
        )
        return

    tid = _write_spec_to_inbox(
        prompt=body,
        user=user["name"],
        chat_id=update.effective_chat.id,
        message_id=update.message.message_id,
        target_repo=target_repo,
    )
    target_label = f"@{alias} ({target_repo})" if alias else target_repo
    await context.bot.send_message(
        update.effective_chat.id,
        f"✓ Принято как задача в {target_label}\n"
        f"task_id={tid}. Дирижёр запустит pipeline.",
    )


# Buckets scanned when matching a reply to a clarify prompt. awaiting-input
# first — that is the only live one; the rest exist so a reply to a STALE
# question is recognised and rejected instead of falling through to the
# meta-agent (#8: a June reply spawned a context-free 2.5h run).
_CLARIFY_BUCKETS = (
    "awaiting-input", "inbox", "active", "awaiting-approval",
    "failed", "done", "cancelled",
)
_CLARIFY_PROMPT_MARK = "Уточнение для задачи"


def _find_clarify_task(bot_message_id: int) -> tuple[Optional[Path], str, dict]:
    """Locate the task whose clarify_pending points at `bot_message_id`.

    Returns (task_dir, bucket, state); (None, "", {}) when nothing matches.
    """
    for bucket in _CLARIFY_BUCKETS:
        bucket_dir = TASKS_ROOT / bucket
        if not bucket_dir.is_dir():
            continue
        for task_dir in sorted(bucket_dir.iterdir()):
            if not task_dir.is_dir() or task_dir.name == "_TEMPLATE":
                continue
            try:
                state = json.loads(
                    (task_dir / "state.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            pending = state.get("clarify_pending") or {}
            if pending.get("bot_message_id") == bot_message_id:
                return task_dir, bucket, state
    return None, "", {}


def _is_clarify_prompt_message(msg) -> bool:
    """Last-resort detection for a clarify prompt whose task record is already
    gone (archived/pruned): bot-authored + the prompt's own wording."""
    author = getattr(msg, "from_user", None)
    if author is None or not author.is_bot:
        return False
    return _CLARIFY_PROMPT_MARK in (msg.text or msg.caption or "")


async def _maybe_handle_clarify_reply(update: Update) -> bool:
    """If this message replies to a clarify-prompt the bot sent earlier (we
    stored its message_id in state.json:clarify_pending.bot_message_id),
    record the answers and bounce the task back to inbox/ so the dispatcher
    re-ingests it. Returns True if the reply was consumed — including the
    stale case, where consuming it is the whole point: a reply to a closed
    task must never reach the meta-agent.
    """
    reply = update.message.reply_to_message
    if reply is None:
        return False

    task_dir, bucket, state = _find_clarify_task(reply.message_id)

    if task_dir is None:
        # No task owns this message id. If it still looks like one of our
        # clarify prompts, the task is long gone — reject rather than forward.
        if _is_clarify_prompt_message(reply):
            await update.message.reply_text(
                "🚫 эта задача уже закрыта (не найдена в tasks/), ответ не принят.\n"
                "Новый запрос — обычным сообщением или /task.",
            )
            logger.info(
                "stale clarify reply rejected: no task owns msg_id=%d",
                reply.message_id,
            )
            return True
        return False

    task_id = task_dir.name
    if bucket != "awaiting-input":
        stage = str(state.get("stage") or bucket)
        await update.message.reply_text(
            f"🚫 эта задача уже закрыта ({bucket}/{stage}), ответ не принят.\n"
            f"Задача <code>{task_id}</code>. Нужен новый прогон — "
            f"<code>/requeue {task_id}</code> или /task.",
            parse_mode="HTML",
        )
        logger.info(
            "stale clarify reply rejected: task_id=%s bucket=%s", task_id, bucket,
        )
        return True

    state_path = task_dir / "state.json"
    pending = state.get("clarify_pending") or {}
    questions: list[str] = pending.get("questions") or []
    # Local parser (avoid dispatcher import to keep bot lean).
    answers = _parse_reply_answers(update.message.text or "", len(questions))
    qa_pairs = [
        {"question": q, "answer": a or "<empty>"}
        for q, a in zip(questions, answers)
    ]
    target = task_dir / "clarifications.md"
    lines: list[str] = []
    if not target.exists():
        lines.extend([
            "# Clarifications",
            "",
            "Operator answers to BA's remaining [NEEDS CLARIFICATION] markers.",
            "BA reads this file on re-ingest and uses answers to replace markers.",
            "",
        ])
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"## {stamp}")
    lines.append("")
    for idx, entry in enumerate(qa_pairs, 1):
        lines.append(f"**Q{idx}.** {entry['question']}")
        lines.append("")
        lines.append(f"**A{idx}.** {entry['answer']}")
        lines.append("")
    with target.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    # Cleanup the pending marker before bouncing back to inbox.
    state.pop("clarify_pending", None)
    pending_path = task_dir / "clarifications-pending.json"
    if pending_path.exists():
        pending_path.unlink()
    state["stage"] = "inbox"
    state_path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    THIN_MODE_INBOX.mkdir(parents=True, exist_ok=True)
    dst = THIN_MODE_INBOX / task_id
    if dst.exists():
        shutil.rmtree(str(dst))
    shutil.move(str(task_dir), str(dst))

    await update.message.reply_text(
        f"✅ Принято {len(qa_pairs)} ответ(а/ов) для задачи "
        f"<code>{task_id}</code>. Задача возвращена в inbox/; "
        f"BA перезапустится с учётом clarifications.md.",
        parse_mode="HTML",
    )
    logger.info(
        "clarify reply consumed: task_id=%s answers=%d", task_id, len(qa_pairs),
    )
    return True


def _parse_reply_answers(reply_text: str, expected_count: int) -> list[str]:
    """Same logic as dispatcher.clarify.parse_reply_answers; inlined to keep
    bot.py free of dispatcher imports."""
    import re as _re
    enum_re = _re.compile(r"^\s*(\d+)\s*[\.\):]\s*(.+?)\s*$")
    enumerated: dict[int, str] = {}
    for line in reply_text.splitlines():
        m = enum_re.match(line)
        if m:
            enumerated[int(m.group(1))] = m.group(2).strip()
    if enumerated:
        return [enumerated.get(i, "").strip() for i in range(1, expected_count + 1)]
    lines = [line.strip() for line in reply_text.splitlines() if line.strip()]
    out = lines[:expected_count]
    out.extend([""] * (expected_count - len(out)))
    return out


async def handle_text(update: Update, context: object) -> None:
    """Plain text → meta-agent Q&A (read-only investigation by default).

    For development tasks (creating PRs, modifying code), the user explicitly
    invokes /task. Everything else is interpreted as a question and goes to
    the meta-agent, which can use Read/Grep/Bash (via botctl) to investigate
    and answer via botctl-send-text.
    """
    user_id = update.effective_user.id
    if user_id not in USER_REGISTRY:
        # Silent rejection — log only, never reply. Replying confirms the bot
        # exists to unknown senders. Owner-only by design.
        logger.warning(
            "Silent reject: text from unauthorized user_id=%d chat_id=%s",
            user_id, getattr(update.effective_chat, "id", None),
        )
        return

    # C.2 — if this is a reply to a "clarify_needed" prompt, route to the
    # clarification-resume flow instead of meta-agent Q&A.
    if (
        update.message.reply_to_message is not None
        and await _maybe_handle_clarify_reply(update)
    ):
        return

    # FR-002/FR-003: bare http(s) URL while /stt mode is ON → URL-download path.
    # Must come after auth and clarify-reply checks; falls through to Q&A otherwise.
    text_content = update.message.text or ""
    if _stt_mode_on(user_id) and stt_utils.is_bare_url(text_content):
        await _run_stt_url(update, context)
        return

    # FR-001: local audio file path while /stt mode is ON → local-path STT.
    # Covers ~/… paths and /.hidden/…-style paths that are not tagged as
    # bot_command by Telegram (ADR-007). A URL never starts with / or ~/ so
    # there is no overlap with the URL guard above.
    if _stt_mode_on(user_id) and stt_utils.is_local_path_candidate(text_content):
        _lp_corr_id = uuid.uuid4().hex[:8]
        await _run_stt_local(update, context, text_content.strip(), _lp_corr_id)
        return

    user = USER_REGISTRY[user_id]
    prompt = f"[FROM {user['name']}] {update.message.text}"
    corr_id = uuid.uuid4().hex[:8]
    logger.info(
        "[%s] Q&A from %s: %s",
        corr_id, user["name"], update.message.text,
    )

    # Persist routing state BEFORE invoking meta — botctl-send-text reads
    # last_chat_id while meta-agent is still running.
    state = load_state()
    state["last_user"] = user["name"]
    state["last_chat_id"] = str(update.effective_chat.id)
    state["last_input_type"] = "text"
    state["last_correlation_id"] = corr_id
    save_state(state)

    await _ack_queued_behind_meta(update)
    async with meta_lock:
        await run_meta_claude(
            user["meta_dir"], prompt, user_id,
            chat_id=update.effective_chat.id,
        )


# ---------------------------------------------------------------------------
# Voice handling (M2)
# ---------------------------------------------------------------------------

async def transcribe_voice(audio_path: str, content_type: str = "audio/ogg") -> str:
    """POST an audio file to the local Whisper STT server, return the text.

    The server re-encodes with ffmpeg, which sniffs the real container from
    the bytes — so any ffmpeg-decodable input works (ogg/opus voice notes,
    mp3, m4a, wav, …). `content_type` is only an advisory hint on the upload
    and does not affect decoding.
    """
    # Client timeout derived from server-side budgets + 30 s margin (ADR-005).
    # Ensures the client never kills a legitimately long transcription.
    total_timeout = FFMPEG_TIMEOUT_SEC + WHISPER_TIMEOUT_SEC + 30
    timeout = aiohttp.ClientTimeout(total=total_timeout)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        with open(audio_path, "rb") as fh:
            form = aiohttp.FormData()
            form.add_field("file", fh, filename=os.path.basename(audio_path), content_type=content_type)
            form.add_field("language", "ru")
            async with session.post(WHISPER_URL, data=form) as resp:
                body = await resp.text()
                if resp.status != 200:
                    # Parse structured JSON error from the server (FR-017, ADR-001).
                    # Shape: {"detail": {"reason": "...", "step": "...", "stderr_tail": "..."}}
                    reason: str
                    try:
                        err_payload = json.loads(body)
                        detail = err_payload.get("detail") or {}
                        if isinstance(detail, dict):
                            reason = detail.get("reason") or str(detail)
                        else:
                            reason = str(detail)[:400]
                    except (json.JSONDecodeError, AttributeError):
                        reason = body[:400]
                    raise RuntimeError(f"STT failed ({resp.status}): {reason}")
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"STT failed: invalid JSON {body[:200]}") from exc
    text = (payload.get("text") or "").strip()
    if not text:
        raise RuntimeError(f"STT failed: empty transcription {body[:200]}")
    return text


async def handle_voice(update: Update, context: object) -> None:
    user_id = update.effective_user.id
    if user_id not in USER_REGISTRY:
        # Silent rejection — log only, never reply (owner-only by design).
        logger.warning(
            "Silent reject: voice from unauthorized user_id=%d chat_id=%s",
            user_id, getattr(update.effective_chat, "id", None),
        )
        return

    user = USER_REGISTRY[user_id]
    corr_id = uuid.uuid4().hex[:8]

    # /stt toggle: while transcription mode is ON, a plain voice note just
    # returns its text — no pipeline, no meta-agent Q&A.
    if _stt_mode_on(user_id):
        await _run_stt(update, context, update.message, corr_id)
        return

    fd, ogg_path = tempfile.mkstemp(prefix=f"voice_{uuid.uuid4().hex}_", suffix=".ogg")
    os.close(fd)
    try:
        voice = await update.message.voice.get_file()
        await voice.download_to_drive(ogg_path)
        logger.info("[%s] Downloaded voice from %s to %s", corr_id, user["name"], ogg_path)

        try:
            transcribed = await transcribe_voice(ogg_path)
        except (RuntimeError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("[%s] STT failed: %s", corr_id, exc)
            await context.bot.send_message(
                update.effective_chat.id,
                "🎙 STT не отвечает — голосовой стек (Whisper) не запущен.\n"
                "Подними: `cd services/stacks/voice && docker compose up -d`\n"
                "Или напиши текстом.",
            )
            return
        logger.info("[%s] Transcribed voice (%d chars)", corr_id, len(transcribed))

        prompt = f"[VOICE] [FROM {user['name']}] {transcribed}"

        # Voice→pipeline trigger: transcribed text starts with "таск" / "task".
        # Otherwise voice is Q&A through the meta-agent (same flip as text).
        trigger_words = ("таск ", "task ", "таск:", "task:")
        if transcribed.lower().lstrip().startswith(trigger_words):
            # Strip the trigger keyword from the prompt body.
            body = transcribed.lstrip()
            for tw in trigger_words:
                if body.lower().startswith(tw):
                    body = body[len(tw):].strip()
                    break
            tid = _write_spec_to_inbox(
                prompt=body,
                user=user["name"],
                chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
            )
            await context.bot.send_message(
                update.effective_chat.id,
                f"🎙→📋 Принято как задача, task_id={tid}. Дирижёр запустит pipeline.",
            )
            return

        # Persist routing state BEFORE invoking meta — botctl-{say,send-text}
        # read last_chat_id while meta-agent is still running.
        state = load_state()
        state["last_user"] = user["name"]
        state["last_chat_id"] = str(update.effective_chat.id)
        state["last_input_type"] = "voice"
        state["last_correlation_id"] = corr_id
        save_state(state)

        await _ack_queued_behind_meta(update)
        async with meta_lock:
            await run_meta_claude(
                user["meta_dir"], prompt, user_id,
                chat_id=update.effective_chat.id,
            )
    finally:
        try:
            os.remove(ogg_path)
        except OSError as exc:
            logger.debug("[%s] Failed to remove %s: %s", corr_id, ogg_path, exc)


# ---------------------------------------------------------------------------
# /stt — pure speech-to-text (voice notes + attached audio files → text)
#
# Standalone feature, deliberately isolated from the /task pipeline and the
# meta-agent Q&A: it ONLY returns the transcript. `/stt` is a per-user TOGGLE:
#   • /stt           → flip transcription mode on/off
#   • while ON       → every voice/audio you send returns just its text
#   • /stt (reply)   → one-off: transcribe the replied-to voice/audio, no flip
#   • /stt caption   → one-off: transcribe the captioned audio, no flip
# With mode OFF, a plain voice note keeps its pipeline/Q&A routing in
# handle_voice, and a bare audio FILE gets a hint pointing at /stt.
# ---------------------------------------------------------------------------

# Telegram caps bot file downloads (getFile) at 20 MB.
_TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024
# Stay safely under Telegram's 4096-char per-message cap when echoing text.
_TG_MSG_LIMIT = 4000


def _stt_mode_on(user_id) -> bool:
    """Is the per-user `/stt` transcription toggle currently ON?"""
    modes = load_state().get("stt_mode")
    return bool(isinstance(modes, dict) and modes.get(str(user_id)))


def _set_stt_mode(user_id, on: bool) -> None:
    """Persist the per-user `/stt` toggle in state.json."""
    state = load_state()
    modes = state.get("stt_mode")
    if not isinstance(modes, dict):
        modes = {}
    modes[str(user_id)] = bool(on)
    state["stt_mode"] = modes
    save_state(state)


def _extract_audio_source(msg) -> Optional[tuple]:
    """Return (tg_obj, filename, content_type, file_size) for the first audio
    payload on `msg`, or None if it carries no transcribable audio.

    Handles native voice notes, audio files (music), and documents whose MIME
    type is audio/*. Every returned tg_obj exposes async .get_file().
    """
    if msg is None:
        return None
    voice = getattr(msg, "voice", None)
    if voice:
        return (voice, "voice.ogg", voice.mime_type or "audio/ogg", voice.file_size)
    audio = getattr(msg, "audio", None)
    if audio:
        return (audio, audio.file_name or "audio.mp3",
                audio.mime_type or "audio/mpeg", audio.file_size)
    doc = getattr(msg, "document", None)
    if doc:
        mime = doc.mime_type or ""
        fname = doc.file_name or "audio"
        # Primary check: MIME type starts with audio/
        if mime.startswith("audio/"):
            return (doc, fname, mime, doc.file_size)
        # Extension fallback: some Telegram clients (e.g. sending .m4a from iOS)
        # report generic MIME (application/octet-stream) but a correct extension
        # (FR-001, AC-1.1).
        ext = os.path.splitext(fname)[1].lower()
        if ext in stt_utils.AUDIO_EXTENSIONS:
            return (doc, fname, mime or "audio/octet-stream", doc.file_size)
    return None


async def _send_long_text(context, chat_id, text, reply_to=None) -> None:
    """Send `text` as plain message(s), chunked under Telegram's size cap."""
    first = True
    for i in range(0, len(text), _TG_MSG_LIMIT):
        await context.bot.send_message(
            chat_id,
            text[i:i + _TG_MSG_LIMIT],
            reply_to_message_id=reply_to if first else None,
        )
        first = False


async def _save_transcript_and_reply(
    context, chat_id: int, text: str, stem: str, reply_to: int | None, corr_id: str,
) -> None:
    """Write *text* to a collision-free .txt file in STT_OUTPUT_DIR and reply
    with the absolute path + a ~200-char preview (FR-007–FR-012).

    On any filesystem error the exception propagates so the caller can surface
    a readable message instead of silence (NFR-004).
    """
    transcript_path = stt_utils.derive_transcript_path(STT_OUTPUT_DIR, stem)
    transcript_path.write_text(text, encoding="utf-8")
    logger.info("[%s] /stt transcript written to %s", corr_id, transcript_path)

    preview = text.strip()[:200]
    reply_text = f"📝 {transcript_path}\n\n{preview}"
    await context.bot.send_message(
        chat_id,
        reply_text,
        reply_to_message_id=reply_to,
    )


async def _run_stt(update, context, source_msg, corr_id) -> None:
    """Download the audio on `source_msg`, transcribe it, save to file, reply.

    Pure STT: no pipeline, no meta-agent. On any failure it replies with a
    readable message — never raises into the handler (NFR-004).
    """
    chat_id = update.effective_chat.id
    source = _extract_audio_source(source_msg)
    if source is None:
        await context.bot.send_message(
            chat_id,
            "🎙 Не вижу аудио для распознавания.\n"
            "Пришли голосовое/аудиофайл с подписью <code>/stt</code> "
            "или ответь <code>/stt</code> на голосовое сообщение.",
            parse_mode="HTML",
        )
        return

    tg_obj, filename, content_type, file_size = source
    if file_size and file_size > _TG_DOWNLOAD_LIMIT:
        await context.bot.send_message(
            chat_id,
            f"🎙 Файл слишком большой ({file_size // (1024 * 1024)} МБ). "
            "Telegram-боту доступны только файлы до 20 МБ. "
            "Отправь прямую ссылку на аудиофайл (http/https) — "
            "в режиме /stt бот скачает его сам (до "
            f"{STT_URL_MAX_MB} МБ, FR-006).",
        )
        return

    # Determine transcript stem before downloading.
    # Voice notes carry no meaningful original name → use local timestamp (FR-010).
    is_voice_note = getattr(source_msg, "voice", None) is not None
    if is_voice_note:
        stem = datetime.now().strftime("voice-%Y%m%d-%H%M%S")
    else:
        stem = stt_utils.sanitize_filename(filename)

    suffix = os.path.splitext(filename)[1] or ".ogg"
    fd, audio_path = tempfile.mkstemp(prefix=f"stt_{uuid.uuid4().hex}_", suffix=suffix)
    os.close(fd)
    try:
        tg_file = await tg_obj.get_file()
        await tg_file.download_to_drive(audio_path)
        logger.info("[%s] /stt downloaded %s (%s), source=%s",
                    corr_id, filename, content_type,
                    "voice" if is_voice_note else "file")

        try:
            await context.bot.send_chat_action(chat_id, "typing")
        except Exception:  # chat action is best-effort
            pass

        try:
            text = await transcribe_voice(audio_path, content_type=content_type)
        except RuntimeError as exc:
            # Server returned a structured error — relay it verbatim (FR-017).
            logger.warning("[%s] /stt STT server error: %s", corr_id, exc)
            await context.bot.send_message(chat_id, f"🎙 Ошибка транскрибации: {exc}")
            return
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("[%s] /stt STT unreachable: %s", corr_id, exc)
            await context.bot.send_message(
                chat_id,
                "🎙 STT не отвечает — голосовой стек (Whisper) не запущен.\n"
                "Подними: `cd services/stacks/voice && docker compose up -d`",
            )
            return

        logger.info("[%s] /stt transcribed %d chars", corr_id, len(text))
        try:
            await _save_transcript_and_reply(
                context, chat_id, text, stem,
                reply_to=source_msg.message_id, corr_id=corr_id,
            )
        except OSError as exc:
            logger.warning("[%s] /stt file write failed: %s", corr_id, exc)
            await context.bot.send_message(
                chat_id,
                f"🎙 Транскрипт получен, но не удалось сохранить файл: {exc}",
            )
    except Exception as exc:  # download / Telegram-side errors
        logger.warning("[%s] /stt failed: %s", corr_id, exc)
        await context.bot.send_message(
            chat_id, f"🎙 Не удалось обработать аудио: {exc}",
        )
    finally:
        try:
            os.remove(audio_path)
        except OSError:
            pass


async def _run_stt_url(update, context) -> None:
    """Handle a bare http(s) URL message while /stt mode is ON.

    Flow (FR-002/FR-004/FR-005, ADR-003):
    1. HEAD pre-validation → accept / inconclusive / reject
    2. Reject immediately on non-audio Content-Type (FR-004c)
    3. Stream download with a mid-stream byte-counter cap (NFR-001)
    4. Hand temp file to transcribe-and-save flow
    """
    chat_id = update.effective_chat.id
    url = update.message.text.strip()
    corr_id = uuid.uuid4().hex[:8]
    logger.info("[%s] /stt url source: %s", corr_id, url)

    # HEAD pre-validation — 10 s timeout; expiry → inconclusive (NFR-008).
    head_timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=head_timeout) as session:
            async with session.head(url, allow_redirects=False) as resp:
                head_status = resp.status
                head_ct = resp.headers.get("Content-Type")
                head_is_redirect = resp.status in range(300, 400)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        head_status = None
        head_ct = None
        head_is_redirect = False

    head_result = stt_utils.HeadResult(
        status=head_status,
        content_type=head_ct,
        is_redirect=head_is_redirect,
    )
    verdict = stt_utils.classify_head_response(head_result, url)
    logger.info("[%s] /stt HEAD verdict=%s status=%s ct=%s",
                corr_id, verdict, head_status, head_ct)

    if verdict == "reject":
        await context.bot.send_message(
            chat_id,
            "🎙 Ссылка не похожа на аудиофайл — сервер вернул не-аудио Content-Type "
            "и в URL нет аудио-расширения. Проверь ссылку.",
        )
        return

    # Derive stem from the URL's last path segment (FR-009).
    url_path_part = url.split("?")[0].rstrip("/")
    url_basename = url_path_part.split("/")[-1] if "/" in url_path_part else url_path_part
    stem = stt_utils.sanitize_filename(url_basename) if url_basename else "audio"

    # Determine suffix for the temp file (prefer extension from URL).
    url_ext = os.path.splitext(url_basename)[1].lower()
    suffix = url_ext if url_ext in stt_utils.AUDIO_EXTENSIONS else ".bin"

    max_bytes = STT_URL_MAX_MB * 1024 * 1024
    fd, audio_path = tempfile.mkstemp(prefix=f"stt_url_{uuid.uuid4().hex}_", suffix=suffix)
    os.close(fd)

    try:
        await context.bot.send_chat_action(chat_id, "typing")
    except Exception:
        pass

    try:
        dl_timeout = aiohttp.ClientTimeout(total=FFMPEG_TIMEOUT_SEC + WHISPER_TIMEOUT_SEC + 30)
        async with aiohttp.ClientSession(timeout=dl_timeout) as session:
            async with session.get(url) as resp:
                if resp.status not in range(200, 300):
                    await context.bot.send_message(
                        chat_id,
                        f"🎙 Не удалось скачать аудио: сервер вернул {resp.status}.",
                    )
                    return
                received = 0
                with open(audio_path, "wb") as fh:
                    async for chunk in resp.content.iter_chunked(65536):
                        received += len(chunk)
                        if received > max_bytes:
                            logger.warning(
                                "[%s] /stt url download exceeded %d MB cap",
                                corr_id, STT_URL_MAX_MB,
                            )
                            await context.bot.send_message(
                                chat_id,
                                f"🎙 Файл по ссылке превысил лимит {STT_URL_MAX_MB} МБ — "
                                "скачивание прервано. Попробуй обрезать запись.",
                            )
                            return
                        fh.write(chunk)
        logger.info("[%s] /stt url downloaded %d bytes", corr_id, received)

        try:
            text = await transcribe_voice(audio_path)
        except RuntimeError as exc:
            logger.warning("[%s] /stt url STT server error: %s", corr_id, exc)
            await context.bot.send_message(chat_id, f"🎙 Ошибка транскрибации: {exc}")
            return
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("[%s] /stt url STT unreachable: %s", corr_id, exc)
            await context.bot.send_message(
                chat_id,
                "🎙 STT не отвечает — голосовой стек (Whisper) не запущен.\n"
                "Подними: `cd services/stacks/voice && docker compose up -d`",
            )
            return

        logger.info("[%s] /stt url transcribed %d chars", corr_id, len(text))
        try:
            await _save_transcript_and_reply(
                context, chat_id, text, stem,
                reply_to=update.message.message_id, corr_id=corr_id,
            )
        except OSError as exc:
            logger.warning("[%s] /stt url file write failed: %s", corr_id, exc)
            await context.bot.send_message(
                chat_id,
                f"🎙 Транскрипт получен, но не удалось сохранить файл: {exc}",
            )
    except Exception as exc:
        logger.warning("[%s] /stt url failed: %s", corr_id, exc)
        await context.bot.send_message(
            chat_id, f"🎙 Не удалось обработать ссылку: {exc}",
        )
    finally:
        try:
            os.remove(audio_path)
        except OSError:
            pass


async def _run_stt_local(update, context, raw_path: str, corr_id: str) -> None:
    """Transcribe a local audio file in-place — no temp copy, no cleanup (ADR-009).

    Flow (FR-004/FR-005/FR-006/FR-007/FR-008/FR-009, ADR-008/ADR-009):
      check_local_audio_path(raw_path)
        ├─ "bad_extension" → reply naming exact path (FR-005/AC-2.2) → return
        ├─ "not_found"     → reply naming exact path (FR-006/AC-2.3) → return
        └─ "ok"            → transcribe_voice(str(path)) (FR-007, ADR-004/ADR-005)
                              ├─ RuntimeError       → relay server reason          → return
                              ├─ ClientError/Timeout → "STT не отвечает…"          → return
                              ├─ OSError            → "Не удалось прочитать файл"  → return
                              └─ text → _save_transcript_and_reply(...)
                                          └─ OSError → "не удалось сохранить файл"

    FR-010/NFR-005: the source file is NEVER deleted, moved, or modified.
    There is deliberately NO finally/cleanup block — this coroutine does not
    own the source file (ADR-009). Do NOT add one.
    """
    chat_id = update.effective_chat.id

    check = stt_utils.check_local_audio_path(raw_path)
    if check.verdict == "bad_extension":
        logger.warning(
            "[%s] /stt local rejected: verdict=bad_extension path=%s",
            corr_id, check.display,
        )
        supported = ", ".join(sorted(stt_utils.AUDIO_EXTENSIONS))
        await context.bot.send_message(
            chat_id,
            f"🎙 Расширение файла не поддерживается: {check.display}\n"
            f"Поддерживаемые форматы: {supported}",
        )
        return
    if check.verdict == "not_found":
        logger.warning(
            "[%s] /stt local rejected: verdict=not_found path=%s",
            corr_id, check.display,
        )
        await context.bot.send_message(
            chat_id,
            f"🎙 Файл не найден: {check.display}",
        )
        return

    # verdict == "ok" — proceed to transcription
    path = check.path
    try:
        file_size = path.stat().st_size
    except OSError:
        file_size = -1
    logger.info("[%s] /stt local source: %s (%d bytes)", corr_id, path, file_size)

    # W-3 / NFR-001: reject files that exceed the shared size cap before reading them
    # into memory.  Mirrors the guard in _run_stt_url (STT_URL_MAX_MB env var, default
    # 100 MB).  file_size == -1 (stat failed) is allowed through — the subsequent
    # open() inside transcribe_voice will surface any real access error as OSError.
    _max_bytes = STT_URL_MAX_MB * 1024 * 1024
    if file_size > _max_bytes:
        logger.warning(
            "[%s] /stt local size check: %d bytes > %d MB limit, rejecting",
            corr_id, file_size, STT_URL_MAX_MB,
        )
        await context.bot.send_message(
            chat_id,
            f"🎙 Файл {check.display} превышает лимит {STT_URL_MAX_MB} МБ.",
        )
        return

    try:
        await context.bot.send_chat_action(chat_id, "typing")
    except Exception:
        pass  # chat action is best-effort

    try:
        text = await transcribe_voice(str(path))
    except RuntimeError as exc:
        # Server returned a structured error — relay it verbatim (ADR-001).
        logger.warning("[%s] /stt local STT server error: %s", corr_id, exc)
        await context.bot.send_message(chat_id, f"🎙 Ошибка транскрибации: {exc}")
        return
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.warning("[%s] /stt local STT unreachable: %s", corr_id, exc)
        await context.bot.send_message(
            chat_id,
            "🎙 STT не отвечает — голосовой стек (Whisper) не запущен.\n"
            "Подними: `cd services/stacks/voice && docker compose up -d`",
        )
        return
    except OSError as exc:
        # TOCTOU race: file deleted or permissions revoked between is_file() and open().
        # NFR-001: every failure path must produce exactly one readable reply.
        logger.warning("[%s] /stt local file read error: %s", corr_id, exc)
        await context.bot.send_message(
            chat_id,
            f"🎙 Не удалось прочитать файл: {check.display}",
        )
        return

    logger.info("[%s] /stt local transcribed %d chars", corr_id, len(text))
    stem = stt_utils.sanitize_filename(path.name)
    try:
        await _save_transcript_and_reply(
            context, chat_id, text, stem,
            reply_to=update.message.message_id, corr_id=corr_id,
        )
    except OSError as exc:
        logger.warning("[%s] /stt local file write failed: %s", corr_id, exc)
        await context.bot.send_message(
            chat_id,
            f"🎙 Транскрипт получен, но не удалось сохранить файл: {exc}",
        )
    # NOTE: no finally block — FR-010/ADR-009: do NOT delete the source file.


async def stt_command(update: Update, context: object) -> None:
    """`/stt` — toggle transcription mode, or one-off transcribe a reply/path.

    Invocation forms (in dispatch priority order):
      • `/stt` as reply to audio   → one-off transcribe the replied-to audio.
      • `/stt <path>`              → local-path STT when arg starts with / or ~/
                                     (FR-002, ADR-006/ADR-011); mode NOT changed.
      • Bare `/stt` or non-path    → flip the per-user transcription mode.
      • While mode ON              → every voice/audio returns just its text.
      • `/stt` caption on audio    → one-off transcribe the captioned audio.

    Deliberately bypasses the /task pipeline and meta-agent Q&A.
    """
    user_id = update.effective_user.id
    if user_id not in USER_REGISTRY:
        logger.warning(
            "Silent reject: /stt from unauthorized user_id=%d chat_id=%s",
            user_id, getattr(update.effective_chat, "id", None),
        )
        return
    corr_id = uuid.uuid4().hex[:8]

    # One-off: `/stt` in reply to an audio message → transcribe just that.
    # Keeps priority over the path-arg branch (ADR-011 / BRD Out-of-scope freeze).
    reply = update.message.reply_to_message
    if _extract_audio_source(reply) is not None:
        await _run_stt(update, context, reply, corr_id)
        return

    # Path argument: `/stt /path/to/file.m4a` → local-path STT (FR-002, ADR-006).
    # Inserted AFTER reply-audio (which keeps priority, ADR-011) and BEFORE toggle.
    # ADR-006: extract argument by entity length to preserve embedded spaces (AC-1.2).
    _msg_text = update.message.text or ""
    _entities = update.message.entities or []
    _cmd_len = (
        _entities[0].length
        if _entities and _entities[0].offset == 0
        else None
    )
    _action, _raw_arg = stt_utils.classify_stt_command(_msg_text, _cmd_len)
    if _action == "path":
        await _run_stt_local(update, context, _raw_arg, corr_id)
        return

    # Otherwise toggle the per-user transcription mode.
    new_on = not _stt_mode_on(user_id)
    _set_stt_mode(user_id, new_on)
    logger.info("[%s] /stt mode -> %s for user %d", corr_id, new_on, user_id)
    if new_on:
        msg = (
            "🎙→📝 Режим распознавания <b>ВКЛЮЧЁН</b>.\n"
            "Шли голосовые/аудио — верну только текст, без задач и вопросов.\n"
            "<code>/stt</code> ещё раз — выключить."
        )
    else:
        msg = (
            "📝→💬 Режим распознавания <b>выключен</b>.\n"
            "Голос снова идёт в Q&A (а «таск …» — в pipeline)."
        )
    await context.bot.send_message(
        update.effective_chat.id, msg, parse_mode="HTML",
    )


async def stt_caption_handler(update: Update, context: object) -> None:
    """Voice/audio sent WITH a `/stt` caption → transcribe (no pipeline)."""
    user_id = update.effective_user.id
    if user_id not in USER_REGISTRY:
        logger.warning(
            "Silent reject: /stt caption from unauthorized user_id=%d", user_id,
        )
        return
    corr_id = uuid.uuid4().hex[:8]
    await _run_stt(update, context, update.message, corr_id)


async def audio_hint_handler(update: Update, context: object) -> None:
    """Bare audio FILE (no /stt caption): transcribe if mode is ON, else hint.

    Only fires for Audio / audio-Document, never for voice notes (those are
    handled by handle_voice, which has its own STT-mode short-circuit).
    """
    user_id = update.effective_user.id
    if user_id not in USER_REGISTRY:
        logger.warning(
            "Silent reject: audio from unauthorized user_id=%d", user_id,
        )
        return
    if _stt_mode_on(user_id):
        await _run_stt(update, context, update.message, uuid.uuid4().hex[:8])
        return
    await context.bot.send_message(
        update.effective_chat.id,
        "🎧 Аудиофайл получен. Включи режим распознавания командой "
        "<code>/stt</code> и пришли снова — верну текст. "
        "Или ответь <code>/stt</code> на это сообщение для разовой расшифровки.",
        parse_mode="HTML",
        reply_to_message_id=update.message.message_id,
    )


async def unknown_command_probe(update: Update, context: object) -> None:
    """Catch-all for unrecognized Telegram commands while /stt mode is ON.

    Telegram tags the first word of "/Users/owner/rec.m4a" as a bot_command
    entity, so that message never reaches handle_text (registered as
    ~filters.COMMAND). This probe catches such messages and routes them to
    _run_stt_local when the sender is authorized, mode is ON, and the text is
    a local-path candidate (ADR-007, FR-001/AC-1.1).

    All other cases return silently — preserving today's silence for unknown
    commands, for unauthorized senders, and for users with mode OFF.

    IMPORTANT: KEEP THIS REGISTRATION LAST among all CommandHandler and
    command-matching MessageHandler registrations. Any handler registered
    AFTER this one would be permanently shadowed (risk R2, ADR-007).
    """
    user_id = update.effective_user.id
    if user_id not in USER_REGISTRY:
        # Silent rejection — log only, no reply (FR-011/AC-4.1).
        logger.warning(
            "Silent reject: unknown command from unauthorized user_id=%d chat_id=%s",
            user_id, getattr(update.effective_chat, "id", None),
        )
        return

    text_content = (update.message.text or "").strip()
    if _stt_mode_on(user_id) and stt_utils.is_local_path_candidate(text_content):
        corr_id = uuid.uuid4().hex[:8]
        await _run_stt_local(update, context, text_content, corr_id)
    # Otherwise: bare return — silence for unknown commands, mode OFF, non-paths.


# ---------------------------------------------------------------------------
# Windmill schedule command (Phase 5.F)
# ---------------------------------------------------------------------------

_SCHEDULE_USAGE = (
    "Usage: /schedule <name> <cron> <prompt...>\n"
    "Example: /schedule nightly-deps 0 2 * * * Bump npm minor versions and open PR\n"
    "<name>: kebab-case; <cron>: 5 fields (min hour dom mon dow)."
)


async def schedule_command(update: Update, context: object) -> None:
    user_id = update.effective_user.id
    if user_id not in USER_REGISTRY:
        # Silent rejection — log only, never reply (owner-only by design).
        logger.warning(
            "Silent reject: /schedule from unauthorized user_id=%d chat_id=%s",
            user_id, getattr(update.effective_chat, "id", None),
        )
        return

    raw = (update.message.text or "").strip()
    # Strip the leading "/schedule" (or "/schedule@BotName") so we can split
    # the remainder ourselves — split(maxsplit=1) gives [command, rest].
    parts = raw.split(maxsplit=1)
    if len(parts) < 2:
        await context.bot.send_message(update.effective_chat.id, _SCHEDULE_USAGE)
        return
    remainder = parts[1]

    # Need at least: name + 5 cron fields + prompt = 7 whitespace-separated tokens.
    tokens = remainder.split(maxsplit=6)
    if len(tokens) < 7:
        await context.bot.send_message(update.effective_chat.id, _SCHEDULE_USAGE)
        return

    name = tokens[0]
    cron = " ".join(tokens[1:6])
    prompt = tokens[6].strip()

    # Basic 5-field cron check ("^\S+ \S+ \S+ \S+ \S+").
    import re
    if not re.match(r"^\S+ \S+ \S+ \S+ \S+$", cron):
        await context.bot.send_message(update.effective_chat.id, _SCHEDULE_USAGE)
        return

    if not prompt:
        await context.bot.send_message(update.effective_chat.id, _SCHEDULE_USAGE)
        return

    windmill_token = os.environ.get("WINDMILL_TOKEN", "").strip()
    if not windmill_token:
        await context.bot.send_message(
            update.effective_chat.id,
            "❌ WINDMILL_TOKEN не настроен в .env бота",
        )
        return

    base_url = os.environ.get("WINDMILL_BASE_URL", "http://localhost").rstrip("/")
    url = f"{base_url}/api/w/ai-delivery/schedules/create"
    payload = {
        "path": f"f/ai_delivery/{name}",
        # Windmill CE needs a 6-field cron (leading SECONDS slot); the operator
        # types the standard 5-field form, so prepend "0 " for the seconds.
        "schedule": f"0 {cron}",
        "timezone": "Europe/Warsaw",
        "script_path": "f/ai_delivery/pipeline_trigger",
        "is_flow": True,
        "args": {
            "prompt": prompt,
            "target_repo": BOT_DEFAULT_TARGET_REPO,
        },
        "enabled": True,
    }
    headers = {
        "Authorization": f"Bearer {windmill_token}",
        "Content-Type": "application/json",
    }

    logger.info(
        "Creating Windmill schedule name=%s cron=%r url=%s", name, cron, url
    )

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                status = resp.status
                body = await resp.text()
    except Exception as exc:
        logger.exception("Windmill schedule HTTP call failed: %s", exc)
        await context.bot.send_message(
            update.effective_chat.id,
            f"❌ Windmill request failed: {exc!r}",
        )
        return

    if status in (200, 201):
        prompt_preview = prompt[:80]
        reply = (
            f"✓ Schedule создан: {name}\n"
            f"Cron: {cron}\n"
            f"Target: {BOT_DEFAULT_TARGET_REPO}\n"
            f"Prompt: {prompt_preview}"
        )
        await context.bot.send_message(update.effective_chat.id, reply)
        return

    logger.warning(
        "Windmill schedule create failed status=%d body=%s", status, body[:500]
    )
    await context.bot.send_message(
        update.effective_chat.id,
        f"❌ Windmill {status}: {body[:200]}",
    )


# ---------------------------------------------------------------------------
# Task queue ops — /tasks (list) + /requeue (unblock a parked task)
# ---------------------------------------------------------------------------

TASKS_ROOT = THIN_MODE_INBOX.parent          # …/ai-delivery/tasks
_TASK_BUCKETS = ("active", "awaiting-input", "awaiting-approval", "failed", "done")


def _read_task_brief(task_dir: Path) -> dict:
    """Best-effort {id, stage, cost, reason} for a task folder, for /tasks."""
    out = {"id": task_dir.name, "stage": "?", "cost": 0.0, "reason": ""}
    try:
        st = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
        out["stage"] = str(st.get("stage", "?"))
        out["cost"] = float(st.get("cost_usd") or 0.0)
    except Exception:  # noqa: BLE001
        pass
    uf = task_dir / "UNRESOLVED-FINDINGS.md"
    if uf.exists():
        try:
            for ln in uf.read_text(encoding="utf-8").splitlines():
                if "Stopped because" in ln:
                    out["reason"] = ln.split("because:", 1)[-1].strip().strip("`-* ")[:60]
                    break
        except Exception:  # noqa: BLE001
            pass
    return out


async def tasks_command(update: Update, context: object) -> None:
    """List tasks per bucket with stage / cost / park-reason. Owner-only."""
    import html as _html
    user_id = update.effective_user.id
    if user_id not in USER_REGISTRY:
        logger.warning("Silent reject: /tasks from unauthorized user_id=%d", user_id)
        return
    lines = ["📋 <b>Tasks</b>"]
    any_task = False
    for bucket in _TASK_BUCKETS:
        bdir = TASKS_ROOT / bucket
        if not bdir.is_dir():
            continue
        entries = sorted(
            p for p in bdir.iterdir() if p.is_dir() and p.name != "_TEMPLATE"
        )
        if not entries:
            continue
        any_task = True
        lines.append(f"\n<b>{bucket}</b> ({len(entries)}):")
        for p in entries[:12]:
            b = _read_task_brief(p)
            row = (f"• <code>{_html.escape(b['id'])}</code> — "
                   f"{_html.escape(b['stage'])} — ${b['cost']:.2f}")
            if b["reason"]:
                row += f" — {_html.escape(b['reason'])}"
            lines.append(row)
        if len(entries) > 12:
            lines.append(f"  …(+{len(entries) - 12})")
    if not any_task:
        lines.append("\n(пусто — нет задач в работе)")
    lines.append("\n↻ <code>/requeue &lt;id&gt; [указания]</code> — вернуть parked-задачу в работу")
    await context.bot.send_message(
        update.effective_chat.id, "\n".join(lines), parse_mode="HTML",
    )


async def requeue_command(update: Update, context: object) -> None:
    """Move a parked task back to inbox/ so the dispatcher re-ingests it. The
    runner resumes via artifact-skip (completed stages are not re-run). Optional
    free-text guidance is appended to clarifications.md for BA to read. Owner-only."""
    user_id = update.effective_user.id
    if user_id not in USER_REGISTRY:
        logger.warning("Silent reject: /requeue from unauthorized user_id=%d", user_id)
        return
    parts = (update.message.text or "").split(maxsplit=2)
    if len(parts) < 2:
        await context.bot.send_message(
            update.effective_chat.id,
            "Usage: /requeue <task_id> [доп. указания]\nСписок id — /tasks",
        )
        return
    task_id = parts[1].strip()
    guidance = parts[2].strip() if len(parts) > 2 else ""

    if (TASKS_ROOT / "active" / task_id).is_dir():
        await context.bot.send_message(
            update.effective_chat.id, f"⏳ {task_id} уже active — requeue не нужен.")
        return
    src = next(
        (TASKS_ROOT / b / task_id for b in ("awaiting-input", "awaiting-approval", "failed")
         if (TASKS_ROOT / b / task_id).is_dir()),
        None,
    )
    if src is None:
        await context.bot.send_message(
            update.effective_chat.id,
            f"❌ {task_id} не найден в awaiting-input / awaiting-approval / failed.")
        return
    if not (src / "spec.json").is_file():
        await context.bot.send_message(
            update.effective_chat.id, f"❌ {task_id}: нет spec.json — нечего ре-queue'ить.")
        return
    dst = THIN_MODE_INBOX / task_id
    if dst.exists():
        await context.bot.send_message(
            update.effective_chat.id, f"❌ inbox/{task_id} уже существует — конфликт.")
        return

    now = datetime.now(timezone.utc)
    if guidance:
        try:
            with (src / "clarifications.md").open("a", encoding="utf-8") as fh:
                fh.write(f"\n## Operator re-queue guidance ({now:%Y-%m-%d %H:%M UTC})\n\n{guidance}\n")
        except Exception:  # noqa: BLE001
            logger.exception("requeue: failed to append guidance for %s", task_id)
    try:
        with (src / "worklog.md").open("a", encoding="utf-8") as fh:
            fh.write(f"- {now.isoformat(timespec='seconds')} — re-queued via /requeue"
                     f"{' (+guidance)' if guidance else ''}\n")
    except Exception:  # noqa: BLE001
        pass

    THIN_MODE_INBOX.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    logger.info("requeue: moved %s → inbox (guidance=%s)", task_id, bool(guidance))
    await context.bot.send_message(
        update.effective_chat.id,
        f"↻ <code>{task_id}</code> возвращён в inbox — диспетчер подхватит за ~5с"
        + (" (с доп. указаниями для BA)" if guidance else "")
        + ".\nРаннер resume'ится с места (artifact-skip пропустит готовые стадии).",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Sub-Claude delegation (M3)
# ---------------------------------------------------------------------------

def _subagent_env(backend: str = "deepseek") -> dict[str, str]:
    """Build env for sub-Claude subprocess.

    Two backends:

    - "deepseek" (default): route the spawned `claude` CLI to DeepSeek's
      Anthropic-compatible endpoint via env overrides. Used for the
      volume-heavy roles — Developer, Tester, Security, Reviewer — where
      DeepSeek V4 Pro produces good output at ~1/30 the Max cost.

    - "anthropic": leave os.environ as-is so the spawned `claude` falls
      through to the operator's Max OAuth credentials in
      ~/.claude/.credentials.json. Used for the "thinking" roles —
      Business Analyst and Architect — where Opus 4.7 gives meaningfully
      better requirements / design judgment that's worth the token cost.

    If DEEPSEEK_API_KEY is unset for the "deepseek" backend, falls back
    silently to anthropic with a logged warning so the system stays
    usable in degraded mode.

    The env is built by ALLOWLIST (child_env.build_child_env,
    ai-delivery-private#13) rather than copied from os.environ: a sub-Claude
    needs base system vars + the routed backend's model/auth family, never the
    Telegram bot token, owner ids, Windmill or LangSmith keys.
    """
    env = build_child_env(backend)
    if backend == "anthropic":
        # Strip any DeepSeek overrides that might be in os.environ from a
        # parent shell, so the subprocess uses the default Anthropic auth.
        for k in (
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "CLAUDE_CODE_SUBAGENT_MODEL",
        ):
            env.pop(k, None)
        return env
    # backend == "deepseek" (or unknown — treat as deepseek)
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        logger.warning(
            "DEEPSEEK_API_KEY not set in bot/.env — sub-Claude will fall "
            "back to Claude Max (consumes orchestrator quota)"
        )
        return build_child_env("anthropic")
    env["ANTHROPIC_BASE_URL"] = os.environ.get(
        "DEEPSEEK_ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic"
    )
    env["ANTHROPIC_AUTH_TOKEN"] = api_key
    env["ANTHROPIC_MODEL"] = os.environ.get(
        "DEEPSEEK_MODEL_PRIMARY", "deepseek-v4-pro"
    )
    env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = os.environ.get(
        "DEEPSEEK_MODEL_SONNET", "deepseek-v4-pro"
    )
    env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = os.environ.get(
        "DEEPSEEK_MODEL_HAIKU", "deepseek-v4-flash"
    )
    env["CLAUDE_CODE_SUBAGENT_MODEL"] = os.environ.get(
        "DEEPSEEK_MODEL_SUBAGENT", "deepseek-v4-flash"
    )
    return env


async def run_subtask(
    task_id: str,
    project: str,
    prompt: str,
    new_session: bool,
    chrome: bool,
    root_id: str | None = None,
    backend: str = "deepseek",
) -> None:
    args = [
        CLAUDE_BIN, "--dangerously-skip-permissions",
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
    ]
    env = _subagent_env(backend)
    actual_backend = "DeepSeek" if env.get("ANTHROPIC_BASE_URL", "").endswith("deepseek.com/anthropic") else "Max"
    logger.info(
        "Starting sub-Claude task_id=%s project=%s new_session=%s chrome=%s backend=%s",
        task_id, project, new_session, chrome, actual_backend,
    )

    # Fall back to task_id if root_id wasn't threaded through (this dispatch
    # IS the root). Watchdog uses root_id for progress accounting.
    if root_id is None:
        root_id = task_id

    _sub_child = None   # may stay None if spawn fails before assignment
    output_chunks: list[str] = []
    rc = -1
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=project,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # own process group (FR-002)
        )
        _sub_child = _proc_reaper.AsyncChild(proc, asyncio.get_running_loop())
        _proc_reaper.track(_sub_child, proc.pid)  # pgid == pid

        async def _stream_stderr() -> None:
            assert proc.stderr is not None
            async for line in proc.stderr:
                decoded = line.decode(errors="replace").rstrip()
                output_chunks.append(decoded)
                logger.warning("[sub %s stderr] %s", task_id, decoded)
                # stderr also counts as progress — keeps the idle clock fresh
                # when sub-Claude is logging warnings but still alive.
                _watchdog.record_progress(root_id, task_id)

        stderr_task = asyncio.create_task(_stream_stderr())

        assert proc.stdout is not None
        async for line in proc.stdout:
            decoded = line.decode(errors="replace").rstrip()
            if not decoded:
                continue
            output_chunks.append(decoded)
            logger.debug("[sub %s stdout] %s", task_id, decoded)
            # Every stdout line from the sub-Claude (each tool_use, each
            # thinking block, each assistant message) is real progress.
            # Watchdog's idle check uses this to detect stuck dispatches
            # without artificially capping legitimate long work.
            _watchdog.record_progress(root_id, task_id)

        await proc.wait()
        stderr_task.cancel()
        try:
            await stderr_task
        except asyncio.CancelledError:
            pass
        rc = proc.returncode if proc.returncode is not None else -1
    except Exception as exc:
        logger.exception("Sub-Claude task %s failed: %s", task_id, exc)
        output_chunks.append(f"EXCEPTION: {exc!r}")
        rc = -1
    finally:
        # Remove from registry and kill any process-group leftovers (FR-007, FR-019).
        if _sub_child is not None:
            pgid = _sub_child.pid
            _proc_reaper.untrack(_sub_child)
            _proc_reaper.kill_group_leftovers(pgid)
        logger.info("Sub-Claude task_id=%s exited rc=%d", task_id, rc)
        await on_subtask_done(task_id, project, rc, "\n".join(output_chunks))


async def on_subtask_done(task_id: str, project: str, rc: int, output: str) -> None:
    ctx = active_tasks.pop(task_id, None)
    if ctx is not None:
        _watchdog.record_completion(
            ctx.get("root_id", task_id), task_id, success=(rc == 0)
        )
    if ctx is None:
        logger.warning("on_subtask_done: unknown task_id=%s", task_id)
        return

    # During shutdown the meta-agent must not be re-entered — a new meta child
    # spawned after the registry snapshot would outlive the bot (ADR-007).
    if _shutting_down:
        return

    user_meta_dir = ctx.get("meta_dir")
    user_id = ctx.get("user_id")
    if not user_meta_dir or user_id is None:
        logger.error("on_subtask_done: missing dispatch context for %s", task_id)
        return

    prompt = (
        f"[SUBTASK_DONE] task_id={task_id} project={project} rc={rc}\n"
        f"{output[-4000:]}"
    )
    logger.info("Re-entering meta-agent for completed task %s", task_id)
    async with meta_lock:
        await run_meta_claude(user_meta_dir, prompt, user_id)


# ---------------------------------------------------------------------------
# HTTP server (M3) — local-only on 127.0.0.1:8766
# ---------------------------------------------------------------------------

async def _handle_run_in_project(request: web.Request) -> web.Response:
    if _shutting_down:
        return web.Response(status=503, text="bot is shutting down")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON"}, status=400)

    project = body.get("project")
    prompt = body.get("prompt")
    new_session = bool(body.get("new_session", False))
    chrome = bool(body.get("chrome", False))
    parent_task_id: Optional[str] = body.get("parent_task_id")
    # backend = "deepseek" (default, cheap) | "anthropic" (Max OAuth, expensive
    # but better judgment — use for BA and Architect roles per CLAUDE.md
    # tiering policy).
    backend = body.get("backend", "deepseek")
    if backend not in ("deepseek", "anthropic"):
        return web.json_response(
            {"error": "backend must be 'deepseek' or 'anthropic'"}, status=400
        )

    if not isinstance(project, str) or not project:
        return web.json_response({"error": "project required"}, status=400)
    if not isinstance(prompt, str) or not prompt:
        return web.json_response({"error": "prompt required"}, status=400)

    project_path = Path(project)
    if not project_path.is_absolute() or not project_path.is_dir():
        return web.json_response({"error": "project must be an absolute path to an existing directory"}, status=400)

    if parent_task_id and parent_task_id in active_tasks:
        root_id = active_tasks[parent_task_id].get("root_id", parent_task_id)
    else:
        root_id = None

    for tid, ctx in active_tasks.items():
        if ctx.get("project") == project:
            return web.json_response({"error": "project busy", "task_id": tid}, status=409)

    requested_user_id = body.get("user_id")
    if requested_user_id is not None:
        try:
            user_id = int(requested_user_id)
        except (TypeError, ValueError):
            return web.json_response({"error": "invalid user_id"}, status=400)
    else:
        if not USER_REGISTRY:
            return web.json_response({"error": "no users registered"}, status=500)
        user_id = next(iter(USER_REGISTRY))

    if user_id not in USER_REGISTRY:
        return web.json_response({"error": "unknown user_id"}, status=400)

    task_id = "task-" + uuid.uuid4().hex[:8]
    if root_id is None:
        root_id = task_id
    agent_role = f"subagent:{Path(project).name}"
    decision = _watchdog.check_dispatch(root_id, parent_task_id, agent_role, prompt)
    if not decision.allow:
        logger.warning("watchdog blocked dispatch: %s", decision.reason)
        return web.json_response(
            {"error": "circuit_breaker", "reason": decision.reason,
             "root_id": root_id, "event": decision.to_event(root_id)},
            status=403,
        )
    _watchdog.record_dispatch(root_id, task_id, parent_task_id,
                              agent_role, project, prompt)
    active_tasks[task_id] = {
        "project": project,
        "user_id": user_id,
        "meta_dir": USER_REGISTRY[user_id]["meta_dir"],
        "started_at": asyncio.get_running_loop().time(),
        "new_session": new_session,
        "chrome": chrome,
        "root_id": root_id,
        "parent_task_id": parent_task_id,
    }
    asyncio.create_task(
        run_subtask(
            task_id, project, prompt, new_session, chrome, root_id, backend
        )
    )
    logger.info("Dispatched sub-Claude task_id=%s project=%s", task_id, project)
    return web.json_response({"task_id": task_id, "root_id": root_id})


async def _handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "active_tasks": len(active_tasks)})


async def _handle_notify(request: web.Request) -> web.Response:
    """Bridge endpoint for the ai-delivery stage-runner.

    Accepts: {"signal": "approval_needed", "task_id": "<id>"}

    When the stage-runner finishes a pipeline and moves the task to
    awaiting-approval/, it POSTs here so the bot sends the inline-keyboard
    approval prompt to the original Telegram thread.
    """
    if _telegram_bot is None:
        return web.json_response({"error": "bot not ready"}, status=503)

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON"}, status=400)

    signal = body.get("signal")
    task_id = body.get("task_id")

    if not signal or not task_id:
        return web.json_response({"error": "signal and task_id required"}, status=400)

    if signal == "approval_needed":
        # Build a minimal context object that _send_approval_prompt expects
        class _Ctx:
            bot = _telegram_bot
        try:
            _send_approval_prompt(_Ctx, task_id)
        except (FileNotFoundError, ValueError) as exc:
            logger.warning("/notify approval_needed failed for %s: %s", task_id, exc)
            return web.json_response({"error": str(exc)}, status=404)
        logger.info("/notify: approval prompt sent for %s", task_id)
        return web.json_response({"ok": True, "signal": signal, "task_id": task_id})

    if signal == "rate_limit_hit":
        resets_at = body.get("resets_at", 0)
        rate_limit_type = body.get("rate_limit_type", "unknown")
        current_backend = body.get("current_backend", "unknown")
        failed_stage = body.get("failed_stage", "unknown")

        # Read telegram_thread from state.json
        state_path = THIN_MODE_AWAITING_INPUT / task_id / "state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("/notify rate_limit_hit: cannot read state for %s: %s", task_id, exc)
            return web.json_response({"error": f"cannot read state: {exc}"}, status=404)

        thread = state.get("telegram_thread") or {}
        chat_id = thread.get("chat_id")
        if chat_id is None:
            logger.warning("/notify rate_limit_hit: no chat_id for %s, cannot notify", task_id)
            return web.json_response({"error": "no chat_id in state"}, status=400)

        message_id = thread.get("message_id")
        resets_str = "неизвестно"
        if resets_at:
            try:
                # resetsAt may be a Unix timestamp (int) or ISO-8601 string
                if isinstance(resets_at, (int, float)):
                    dt = datetime.fromtimestamp(resets_at, tz=timezone.utc)
                else:
                    dt = datetime.fromisoformat(str(resets_at).replace("Z", "+00:00"))
                resets_str = dt.strftime("%H:%M UTC")
            except (ValueError, TypeError, OSError) as exc:
                logger.warning("/notify rate_limit_hit: bad resets_at=%r: %s", resets_at, exc)

        text = (
            f"⚠️ Rate-limit на стадии {failed_stage} ({current_backend})\n"
            f"Сброс: {resets_str}\n"
            f"Выбери действие:"
        )
        # Build one switch button per *other* backend, so the admin can rotate
        # through anthropic → deepseek → glm → anthropic when limits hit.
        backend_labels = {"anthropic": "Anthropic", "deepseek": "DeepSeek", "glm": "GLM"}
        keyboard_rows = []
        for target, pretty in backend_labels.items():
            if target == current_backend:
                continue
            keyboard_rows.append([
                InlineKeyboardButton(
                    f"🔄 Переключить на {pretty}",
                    callback_data=f"rl_switch:{task_id}:{failed_stage}:{target}",
                ),
            ])
        keyboard_rows.append([
            InlineKeyboardButton(
                "⏰ Запланировать в Windmill",
                callback_data=f"rl_windmill:{task_id}:{resets_at}",
            ),
        ])
        keyboard_rows.append([
            InlineKeyboardButton(
                "❌ Отмена",
                callback_data=f"rl_cancel:{task_id}",
            ),
        ])
        keyboard = InlineKeyboardMarkup(keyboard_rows)

        send_kwargs: dict = {
            "chat_id": chat_id,
            "text": text,
            "reply_markup": keyboard,
        }
        if message_id is not None:
            send_kwargs["reply_to_message_id"] = message_id

        class _Ctx:
            bot = _telegram_bot
        coro = _Ctx.bot.send_message(**send_kwargs)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coro)
        else:
            loop.create_task(coro)
        logger.info("/notify: rate_limit prompt sent for %s", task_id)
        return web.json_response({"ok": True, "signal": signal, "task_id": task_id})

    if signal == "clarify_needed":
        questions = body.get("questions") or []
        if not isinstance(questions, list) or not questions:
            return web.json_response({"error": "questions[] required"}, status=400)

        state_path = THIN_MODE_AWAITING_INPUT / task_id / "state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("/notify clarify_needed: cannot read state for %s: %s", task_id, exc)
            return web.json_response({"error": f"cannot read state: {exc}"}, status=404)

        thread = state.get("telegram_thread") or {}
        chat_id = thread.get("chat_id")
        if chat_id is None:
            logger.warning("/notify clarify_needed: no chat_id for %s", task_id)
            return web.json_response({"error": "no chat_id in state"}, status=400)

        header = (
            f"❓ Уточнение для задачи <code>{task_id}</code>\n"
            "BA не смог разрешить эти вопросы через defaults. Ответь "
            "<b>reply</b>'ем на это сообщение — по одной строке на вопрос "
            "в том же порядке (можно «1. ответ», «2. ответ», … или просто "
            "переносы строк).\n"
        )
        body_lines = [f"{idx}. {q}" for idx, q in enumerate(questions, 1)]
        text = header + "\n" + "\n".join(body_lines)

        send_kwargs: dict = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": ForceReply(selective=True),
        }
        message_id = thread.get("message_id")
        if message_id is not None:
            send_kwargs["reply_to_message_id"] = message_id

        sent = await _telegram_bot.send_message(**send_kwargs)

        # Persist the bot message id so handle_text can detect a reply to it.
        state.setdefault("clarify_pending", {})
        state["clarify_pending"].update({
            "count": len(questions),
            "questions": questions,
            "bot_message_id": sent.message_id,
        })
        state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8")

        logger.info("/notify: clarify prompt sent for %s (msg_id=%d)", task_id, sent.message_id)
        return web.json_response({"ok": True, "signal": signal, "task_id": task_id})

    if signal == "budget_stop":
        # Parked budget/cap stop → offer [Продолжить]/[Удалить]. All logic lives in
        # budget_buttons (keeping bot.py from growing).
        err = await budget_buttons.send_prompt(_telegram_bot, task_id, body)
        if err:
            logger.warning("/notify budget_stop: %s for %s", err, task_id)
            return web.json_response({"error": err}, status=404)
        return web.json_response({"ok": True, "signal": signal, "task_id": task_id})

    return web.json_response({"error": f"unknown signal: {signal}"}, status=400)


def make_http_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/run-in-project", _handle_run_in_project)
    app.router.add_post("/notify", _handle_notify)
    app.router.add_get("/health", _handle_health)
    return app


# ---------------------------------------------------------------------------
# Shutdown: fan-out group kill on one shared deadline (issue #20, ADR-008)
# ---------------------------------------------------------------------------

async def _shutdown_children() -> None:
    """Kill every live registered claude child group on a shared 5 s deadline.

    Called from run_all()'s finally block on both signal-driven and normal exit
    paths.  Sets _shutting_down first so no new child may be born after the
    snapshot is taken (ADR-007, FR-017).
    """
    global _shutting_down
    _shutting_down = True

    # Record every in-flight run_subtask dispatch as terminated BEFORE
    # signalling (FR-017) — the write is durable even if the bot is killed
    # mid-drain.
    for task_id, ctx in list(active_tasks.items()):
        root_id = ctx.get("root_id")
        if root_id and _watchdog is not None:
            try:
                _watchdog.record_termination(root_id, task_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "shutdown: could not record termination for %s: %s", task_id, exc
                )

    entries = _proc_reaper.tracked()
    if not entries:
        return

    own_pgid = os.getpgrp()
    to_signal: list = []
    for handle, pgid in entries:
        if handle.poll() is not None:
            _proc_reaper.untrack(handle)
            continue  # FR-011: already exited — drop without signalling
        if pgid == own_pgid:
            continue  # FR-010: never signal our own group
        to_signal.append((handle, pgid))

    # Send SIGTERM to every live child group (FR-003, FR-004, FR-012).
    for handle, pgid in to_signal:
        _proc_reaper.signal_group(pgid, signal.SIGTERM)

    # Await all children on one shared deadline (ADR-008, FR-006, NFR-001).
    if to_signal:
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *(h.wait() for h, _ in to_signal),
                    return_exceptions=True,
                ),
                timeout=_proc_reaper.SIGNAL_GRACE_SEC,
            )
        except asyncio.TimeoutError:
            pass

    # SIGKILL any group still alive after the deadline (FR-006, FR-019).
    for handle, pgid in to_signal:
        if _proc_reaper.group_alive(pgid):
            _proc_reaper.signal_group(pgid, signal.SIGKILL)
        _proc_reaper.untrack(handle)

    killed = len(to_signal)
    if killed > 0:
        logger.warning("shutdown: killed %d claude child group(s)", killed)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_all() -> None:
    global _telegram_bot

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.critical("TELEGRAM_BOT_TOKEN environment variable is not set")
        raise SystemExit(1)

    app = Application.builder().token(token).build()
    _telegram_bot = app.bot
    app.add_handler(CommandHandler("schedule", schedule_command))
    app.add_handler(CommandHandler("memo", memo_command))
    app.add_handler(CommandHandler("recall", recall_command))
    app.add_handler(CommandHandler("task", task_command))
    app.add_handler(CommandHandler("usage", usage_command))
    app.add_handler(CommandHandler("main", main_command, block=False))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("projects", projects_command))
    app.add_handler(CommandHandler("refresh_code", refresh_code_command))
    app.add_handler(CommandHandler("stt", stt_command))
    app.add_handler(CommandHandler("tasks", tasks_command))
    app.add_handler(CommandHandler("requeue", requeue_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    # block=False on the meta-agent entry points: the application processes
    # updates sequentially, so a long meta run used to freeze the whole update
    # queue — /cancel included (#8). meta_lock still serializes the runs
    # themselves, and waiting messages now get a queue ack.
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text, block=False))
    # Catch-all for unrecognized commands while /stt mode is ON (ADR-007, FR-001).
    # Telegram tags /Users in "/Users/owner/rec.m4a" as a bot_command entity, so
    # it never reaches handle_text. This probe catches such messages and routes
    # them to _run_stt_local when appropriate.
    # KEEP THIS LAST among TEXT & COMMAND handlers (R2); audio handlers below are safe.
    app.add_handler(
        MessageHandler(filters.TEXT & filters.COMMAND, unknown_command_probe, block=False))
    # /stt feature — must precede handle_voice so a voice/audio captioned
    # `/stt` is transcribed (pure STT) instead of routed into the pipeline.
    _stt_caption = filters.CaptionRegex(r"(?i)^\s*/stt\b")
    app.add_handler(MessageHandler(
        (filters.VOICE | filters.AUDIO | filters.Document.AUDIO) & _stt_caption,
        stt_caption_handler,
    ))
    # Bare audio files (no caption) get a hint; voice notes fall through to
    # handle_voice and keep their existing pipeline/Q&A routing.
    app.add_handler(MessageHandler(
        filters.AUDIO | filters.Document.AUDIO, audio_hint_handler,
    ))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice, block=False))
    app.add_handler(CallbackQueryHandler(rate_limit_callback, pattern=r"^rl_"))
    app.add_handler(CallbackQueryHandler(budget_buttons.budget_callback, pattern=r"^bud_"))
    app.add_handler(CallbackQueryHandler(approval_callback))

    http_app = make_http_app()
    runner = web.AppRunner(http_app)
    await runner.setup()
    site = web.TCPSite(runner, HTTP_HOST, HTTP_PORT)

    await app.initialize()
    await app.start()
    await _publish_bot_commands(app.bot)
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    await site.start()
    logger.info("HTTP server listening on %s:%d", HTTP_HOST, HTTP_PORT)

    stop_event = asyncio.Event()

    def _on_bot_signal(signum: int) -> None:
        global _exit_signal
        _exit_signal = signum
        stop_event.set()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, _on_bot_signal, signal.SIGTERM)
    loop.add_signal_handler(signal.SIGINT, _on_bot_signal, signal.SIGINT)

    try:
        await stop_event.wait()
    finally:
        # Kill all registered claude children before tearing down Telegram/HTTP
        # (FR-003, FR-004, FR-005, FR-015 — log only, no Telegram message).
        await _shutdown_children()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await runner.cleanup()


def main() -> None:
    import atexit
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # Root filter so every logger in this process — bot's own, httpx's,
    # telegram's — has bot tokens and secret-env-var values masked before
    # anything is written to logs/bot.log. Must run regardless of LOG_LEVEL:
    # a DEBUG/INFO root level is exactly what makes httpx's request-URL log
    # line (which embeds the bot token) visible in the first place.
    log_redact.install()
    log_redact.quiet_http_loggers()

    global USER_REGISTRY
    USER_REGISTRY = load_user_registry()
    logger.info("Loaded %d user(s) from registry", len(USER_REGISTRY))

    # Atexit belt — covers normal return, unhandled exception, SystemExit
    # (FR-005).  On the signal path _shutdown_children() already runs in
    # run_all()'s finally, so the registry is normally empty here (NFR-003).
    atexit.register(
        lambda: _proc_reaper.kill_tracked(grace=_proc_reaper.SIGNAL_GRACE_SEC)
    )

    asyncio.run(run_all())

    # Re-raise the received signal under SIG_DFL so the supervising process
    # (aidstack.sh, systemd) observes a truthful signal-driven exit status
    # (FR-009).  atexit does NOT run after os.kill here — the children were
    # already killed in the drain and the registry is empty.
    if _exit_signal:
        signal.signal(_exit_signal, signal.SIG_DFL)
        os.kill(os.getpid(), _exit_signal)


if __name__ == "__main__":
    main()
