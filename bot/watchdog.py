"""watchdog.py — circuit breaker for sub-agent delegation chains.

Detects and blocks runaway delegation patterns inside a single user-driven
root task (one Telegram message that triggers some chain of sub-Claude
delegations). The five deterministic rules below catch the common failure
modes documented in `ARCHITECTURE.md §9.3` and §3.9.

State persists to `~/.claude-tg-bot/watchdog.json` so a long-running root
task survives a brief `bot.py` restart without losing its budget account.
Entries older than `ROOT_TASK_TTL_SEC` are pruned on every load — root
tasks are short-lived by design and shouldn't accumulate.

This module is independent of `bot.py` — it can be unit-tested in isolation.
The HTTP wiring (calling `check_dispatch` from `/run-in-project` and
`record_completion` from `on_subtask_done`) is added in a follow-up patch
to `bot/bot.py` once M2-M3-bot-patch lands.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_WATCHDOG_PATH_ENV = os.environ.get("WATCHDOG_STATE_PATH", "").strip()
WATCHDOG_PATH = (
    Path(_WATCHDOG_PATH_ENV) if _WATCHDOG_PATH_ENV
    else (Path.home() / ".claude-tg-bot" / "watchdog.json")
)

MAX_HOPS = int(os.environ.get("WATCHDOG_MAX_HOPS", "5"))
MAX_TOTAL_DISPATCHES_PER_ROOT = int(
    os.environ.get("WATCHDOG_MAX_TOTAL_DISPATCHES_PER_ROOT", "30")
)
MAX_INVOCATIONS_PER_AGENT_PER_ROOT = int(
    os.environ.get("WATCHDOG_MAX_INVOCATIONS_PER_AGENT_PER_ROOT", "5")
)
MAX_PROMPT_HASH_REPEAT = int(os.environ.get("WATCHDOG_MAX_PROMPT_HASH_REPEAT", "3"))
# Hard ceiling on total wall-clock for a root task. Default is 8 hours,
# generous enough to never interrupt legitimate long-running work (e.g., a
# Developer role compiling and iterating tests for hours). Set to 0 to
# disable the ceiling entirely.
MAX_DURATION_SEC = int(os.environ.get("WATCHDOG_MAX_DURATION_SEC", "28800"))
# Idle = time since the last tool_use / stdout event from a *pending*
# dispatch in this root. A sub-Claude making real progress emits events
# every few seconds (Bash output, Read, Edit, thinking blocks). If nothing
# has come from a pending dispatch for MAX_IDLE_SEC, the dispatch is stuck
# (looping internally, hung on network, deadlocked) — block new dispatches
# in this root until the operator investigates. Defaults to 10 min.
MAX_IDLE_SEC = int(os.environ.get("WATCHDOG_MAX_IDLE_SEC", "600"))
ROOT_TASK_TTL_SEC = int(os.environ.get("WATCHDOG_ROOT_TASK_TTL_SEC", "7200"))


@dataclass
class WatchdogDecision:
    allow: bool
    reason: str = ""

    def to_event(self, root_id: str) -> str:
        return f"[CIRCUIT_BREAKER] root={root_id} reason={self.reason}"


@dataclass
class DispatchEntry:
    task_id: str
    parent_task_id: str | None
    agent_role: str
    project: str
    prompt_hash: str
    dispatched_at: float
    completed_at: float | None = None
    status: str = "pending"
    # Updated by bot.py on every stdout event from the spawned sub-Claude.
    # Initialized to dispatched_at so the dispatch starts with a fresh
    # "active" timestamp; if no events come in for MAX_IDLE_SEC the
    # dispatch is considered stuck.
    last_progress_at: float | None = None


@dataclass
class RootTask:
    root_id: str
    started_at: float
    dispatches: list[DispatchEntry] = field(default_factory=list)

    def hop_depth(self, parent_task_id: str | None) -> int:
        if parent_task_id is None:
            return 0
        by_id = {d.task_id: d for d in self.dispatches}
        depth = 1
        current = parent_task_id
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            entry = by_id.get(current)
            if entry is None or entry.parent_task_id is None:
                return depth
            current = entry.parent_task_id
            depth += 1
            if depth > 100:
                return depth
        return depth

    def agent_count(self, agent_role: str) -> int:
        return sum(1 for d in self.dispatches if d.agent_role == agent_role)

    def prompt_hash_count(self, agent_role: str, prompt_hash: str) -> int:
        return sum(
            1
            for d in self.dispatches
            if d.agent_role == agent_role and d.prompt_hash == prompt_hash
        )

    def total_dispatches(self) -> int:
        return len(self.dispatches)

    def duration_sec(self) -> float:
        return time.time() - self.started_at

    def stuck_pending(self, idle_sec: int) -> DispatchEntry | None:
        """Return the most-stuck pending dispatch, or None if all are healthy.

        A pending dispatch is "stuck" when its last_progress_at is older than
        `idle_sec` seconds. Returns the dispatch with the oldest
        last_progress_at among stuck ones (most-stuck first).
        """
        now = time.time()
        stuck: list[DispatchEntry] = []
        for d in self.dispatches:
            if d.status != "pending":
                continue
            ts = d.last_progress_at if d.last_progress_at is not None else d.dispatched_at
            if now - ts > idle_sec:
                stuck.append(d)
        if not stuck:
            return None
        stuck.sort(key=lambda d: d.last_progress_at or d.dispatched_at)
        return stuck[0]


class Watchdog:
    def __init__(self, state_path: Path | None = None) -> None:
        self.state_path = state_path or WATCHDOG_PATH
        self.roots: dict[str, RootTask] = self._load()
        self._prune_stale()

    def _load(self) -> dict[str, RootTask]:
        if not self.state_path.exists():
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("watchdog: failed to load state (%s); starting empty", exc)
            return {}
        roots: dict[str, RootTask] = {}
        for rid, rdata in data.items():
            try:
                roots[rid] = RootTask(
                    root_id=rdata["root_id"],
                    started_at=float(rdata["started_at"]),
                    dispatches=[
                        DispatchEntry(**d) for d in rdata.get("dispatches", [])
                    ],
                )
            except (KeyError, TypeError) as exc:
                logger.warning("watchdog: bad entry %s (%s); skipping", rid, exc)
        return roots

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {rid: asdict(root) for rid, root in self.roots.items()}
        tmp_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp_path.replace(self.state_path)

    def _prune_stale(self) -> None:
        now = time.time()
        stale = [
            rid for rid, root in self.roots.items()
            if now - root.started_at > ROOT_TASK_TTL_SEC
        ]
        for rid in stale:
            logger.info("watchdog: pruning stale root_id=%s", rid)
            del self.roots[rid]
        if stale:
            self._save()

    @staticmethod
    def hash_prompt(prompt: str) -> str:
        normalized = " ".join(prompt.split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

    def start_root_task(self, root_id: str) -> None:
        if root_id in self.roots:
            return
        self.roots[root_id] = RootTask(root_id=root_id, started_at=time.time())
        logger.info("watchdog: new root task root_id=%s", root_id)
        self._save()

    def check_dispatch(
        self,
        root_id: str,
        parent_task_id: str | None,
        agent_role: str,
        prompt: str,
    ) -> WatchdogDecision:
        if root_id not in self.roots:
            self.start_root_task(root_id)
        root = self.roots[root_id]

        depth = root.hop_depth(parent_task_id) + 1
        if depth > MAX_HOPS:
            return WatchdogDecision(
                allow=False,
                reason=f"hop_limit: depth={depth} exceeds MAX_HOPS={MAX_HOPS}",
            )

        agent_count = root.agent_count(agent_role) + 1
        if agent_count > MAX_INVOCATIONS_PER_AGENT_PER_ROOT:
            return WatchdogDecision(
                allow=False,
                reason=(
                    f"agent_repeat: '{agent_role}' would be invoked "
                    f"{agent_count} times (limit "
                    f"{MAX_INVOCATIONS_PER_AGENT_PER_ROOT})"
                ),
            )

        total = root.total_dispatches() + 1
        if total > MAX_TOTAL_DISPATCHES_PER_ROOT:
            return WatchdogDecision(
                allow=False,
                reason=(
                    f"total_dispatches: {total} would exceed "
                    f"MAX_TOTAL_DISPATCHES_PER_ROOT={MAX_TOTAL_DISPATCHES_PER_ROOT}"
                ),
            )

        prompt_hash = self.hash_prompt(prompt)
        repeat = root.prompt_hash_count(agent_role, prompt_hash) + 1
        if repeat > MAX_PROMPT_HASH_REPEAT:
            return WatchdogDecision(
                allow=False,
                reason=(
                    f"prompt_repeat: '{agent_role}' would receive the same "
                    f"prompt hash {prompt_hash} for the {repeat}th time "
                    f"(limit {MAX_PROMPT_HASH_REPEAT})"
                ),
            )

        # Hard ceiling: very generous (default 8h). 0 disables.
        if MAX_DURATION_SEC > 0:
            duration = root.duration_sec()
            if duration > MAX_DURATION_SEC:
                return WatchdogDecision(
                    allow=False,
                    reason=(
                        f"duration: root task running for {duration:.0f}s "
                        f"exceeds MAX_DURATION_SEC={MAX_DURATION_SEC}"
                    ),
                )

        # Progress check: block new dispatch if any existing pending dispatch
        # in this root has gone idle. Catches the real problem (stuck loop,
        # hung subprocess) without artificially capping legitimate long work.
        stuck = root.stuck_pending(MAX_IDLE_SEC)
        if stuck is not None:
            last_ts = stuck.last_progress_at or stuck.dispatched_at
            idle = time.time() - last_ts
            return WatchdogDecision(
                allow=False,
                reason=(
                    f"idle: dispatch {stuck.task_id} ({stuck.agent_role}) has "
                    f"been silent for {idle:.0f}s "
                    f"(MAX_IDLE_SEC={MAX_IDLE_SEC}). Likely stuck."
                ),
            )

        return WatchdogDecision(allow=True)

    def record_dispatch(
        self,
        root_id: str,
        task_id: str,
        parent_task_id: str | None,
        agent_role: str,
        project: str,
        prompt: str,
    ) -> None:
        if root_id not in self.roots:
            self.start_root_task(root_id)
        now = time.time()
        entry = DispatchEntry(
            task_id=task_id,
            parent_task_id=parent_task_id,
            agent_role=agent_role,
            project=project,
            prompt_hash=self.hash_prompt(prompt),
            dispatched_at=now,
            last_progress_at=now,
        )
        self.roots[root_id].dispatches.append(entry)
        self._save()

    def record_progress(self, root_id: str, task_id: str) -> None:
        """Called by bot.py on every stdout event from a running sub-Claude.

        Updates the in-memory timestamp without persisting to disk on every
        call — the persistence happens on completion. This keeps the IO
        cost negligible even for chatty sub-Claudes that emit dozens of
        events per minute.
        """
        root = self.roots.get(root_id)
        if root is None:
            return
        for d in root.dispatches:
            if d.task_id == task_id and d.status == "pending":
                d.last_progress_at = time.time()
                return

    def record_completion(
        self, root_id: str, task_id: str, success: bool
    ) -> None:
        root = self.roots.get(root_id)
        if root is None:
            return
        for d in root.dispatches:
            if d.task_id == task_id:
                if d.status == "terminated":
                    return  # non-downgrade: terminated is a terminal state
                d.completed_at = time.time()
                d.status = "completed" if success else "failed"
                break
        self._save()

    def record_termination(self, root_id: str, task_id: str) -> None:
        """Mark a dispatch as terminated by the bot's shutdown path.

        Written before signals are sent so the state is durable even if the
        bot is SIGKILLed mid-drain.  record_completion will not overwrite a
        terminated entry (non-downgrade rule).
        """
        root = self.roots.get(root_id)
        if root is None:
            return
        for d in root.dispatches:
            if d.task_id == task_id:
                d.completed_at = time.time()
                d.status = "terminated"
                break
        self._save()

    def status(self, root_id: str | None = None) -> dict[str, Any]:
        if root_id:
            root = self.roots.get(root_id)
            if root is None:
                return {"error": f"no such root_id: {root_id}"}
            return self._root_summary(root)
        return {rid: self._root_summary(r) for rid, r in self.roots.items()}

    def _root_summary(self, root: RootTask) -> dict[str, Any]:
        roles = {d.agent_role for d in root.dispatches}
        now = time.time()
        pending = [d for d in root.dispatches if d.status == "pending"]
        max_idle = 0.0
        for d in pending:
            ts = d.last_progress_at if d.last_progress_at is not None else d.dispatched_at
            max_idle = max(max_idle, now - ts)
        return {
            "root_id": root.root_id,
            "started_at": root.started_at,
            "duration_sec": round(root.duration_sec(), 1),
            "total_dispatches": root.total_dispatches(),
            "by_agent": {r: root.agent_count(r) for r in roles},
            "pending": len(pending),
            "max_pending_idle_sec": round(max_idle, 1),
            "limits": {
                "MAX_HOPS": MAX_HOPS,
                "MAX_TOTAL_DISPATCHES_PER_ROOT": MAX_TOTAL_DISPATCHES_PER_ROOT,
                "MAX_INVOCATIONS_PER_AGENT_PER_ROOT": MAX_INVOCATIONS_PER_AGENT_PER_ROOT,
                "MAX_PROMPT_HASH_REPEAT": MAX_PROMPT_HASH_REPEAT,
                "MAX_DURATION_SEC": MAX_DURATION_SEC,
                "MAX_IDLE_SEC": MAX_IDLE_SEC,
            },
        }
