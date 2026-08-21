"""Process-group ownership + orphan reaping for the claude children (issue #18).

Incident 2026-08-14: an operator killed the stage runners (pause/park flow).
Every runner died instantly — and every ``claude`` child it had spawned kept
running, re-parented to init (``ppid 1``). One of them burned its agentic loop
for 3 h 11 m against the subscription with no task, no log and no owner. The
same window produced a split-brain task dir: the limit park moved the task to
``awaiting-input/`` while the abandoned child wrote ``03-dev-agent.md`` back
into a re-created ``active/<task>/``.

Two defects, two fixes:

  * **Nobody owned the child.** The claude CLI spawns its own subagent/MCP
    subtree, so a bare ``proc.terminate()`` on the CLI wrapper leaves the
    expensive part alive, and a kill of the RUNNER left everything alive.
    Every child is now spawned with ``start_new_session=True`` (it becomes the
    leader of its own process group == its own pid) and killed as a GROUP
    (TERM → grace → KILL), from the stage exit paths, from a SIGTERM/SIGINT
    handler on the runner itself, and from ``atexit`` as a belt.
  * **Nobody swept.** A child orphaned by SIGKILL (which no handler can catch)
    or by a reboot-era daemon still has to be found later. ``sweep()`` matches
    the pipeline signature over ``ps`` output and kills what it finds.

SAFETY — the sweep must never touch an interactive Claude Code session. Those
run with the very same ``--dangerously-skip-permissions`` flag on this machine
(near-miss 2026-08-12: two "orphans" were the owner's own sessions —
``memory/lessons/kill_only_with_identity_evidence.md``). Three conditions are
required, ALL of them, and each one alone spares an interactive session:
``ppid == 1`` (a live runner still owns its child), no controlling tty (an
interactive session always has one), and BOTH pipeline flags
(``--dangerously-skip-permissions`` AND ``--output-format stream-json``) on a
process whose argv[0] is the claude CLI.

The matcher is a pure function over ``ps`` text (``find_orphans``), so the
whole safety table is unit-testable without spawning anything
(``tests/test_orphan_reaping.py``).
"""
from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, Iterable, NamedTuple, Optional

# TERM → KILL escalation window, shared by the group kill and the sweep.
KILL_GRACE_SEC = float(os.environ.get("ORPHAN_KILL_GRACE_SEC", "10"))
# The runner is dying while it kills its children — keep that window short.
SIGNAL_GRACE_SEC = float(os.environ.get("ORPHAN_SIGNAL_GRACE_SEC", "5"))
# Set to 0 to disable the periodic watcher sweep (the explicit CLI still works).
SWEEP_ENABLED = os.environ.get("ORPHAN_SWEEP_ENABLED", "1").strip() != "0"

# `ps -axo pid,ppid,tty,args` is the portable spelling: BSD/macOS and
# procps/Linux both accept it (`tty` is `tt` on macOS output, `TTY` here).
PS_ARGV = ("ps", "-axo", "pid,ppid,tty,args")
# Both must be present — the pipeline is the only caller that combines them.
SIGNATURE_ARGS = ("--dangerously-skip-permissions", "--output-format stream-json")
# ps prints these when a process has NO controlling terminal.
_NO_TTY = frozenset({"??", "?", "-", ""})


def _log(message: str) -> None:
    print(message, file=sys.stderr)


# ── Child registry ──────────────────────────────────────────────────────────
# Every live claude child of THIS process, with the pgid captured at spawn time
# (reading it back after the child exits raises ProcessLookupError).

_LOCK = threading.Lock()
_LIVE: "dict[int, tuple[subprocess.Popen, int]]" = {}


def track(proc: "subprocess.Popen", pgid: Optional[int] = None) -> None:
    """Register a spawned child so the signal/atexit paths can kill its group."""
    if pgid is None:
        pgid = pgid_of(proc)
    if pgid is None:
        return
    with _LOCK:
        _LIVE[proc.pid] = (proc, pgid)


def untrack(proc: "subprocess.Popen") -> None:
    with _LOCK:
        _LIVE.pop(proc.pid, None)


def tracked() -> "list[tuple[subprocess.Popen, int]]":
    with _LOCK:
        return list(_LIVE.values())


def spawn(argv: "list[str]", **kwargs) -> "subprocess.Popen":
    """``Popen(argv)`` in its OWN session, registered for group kill.

    ``start_new_session=True`` is what makes the child a process-group leader
    (pgid == pid): the group can then be signalled as a unit without ever
    reaching the runner's own group.
    """
    kwargs.setdefault("start_new_session", True)
    proc = subprocess.Popen(argv, **kwargs)
    track(proc, proc.pid if kwargs["start_new_session"] else None)
    return proc


class AsyncChild:
    """Popen-shaped handle over an asyncio.subprocess.Process, for the registry.

    poll() never calls waitpid while the event loop is open — doing so would
    race the loop's child watcher and hang proc.wait() in callers.  Only when
    the loop is closed (atexit belt) is waitpid safe.
    """

    def __init__(self, proc, loop) -> None:
        self._proc = proc
        self._loop = loop

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def returncode(self):
        return self._proc.returncode

    def poll(self):
        rc = self._proc.returncode
        if rc is None and self._loop.is_closed():
            try:
                pid, status = os.waitpid(self._proc.pid, os.WNOHANG)
                if pid == self._proc.pid:
                    rc = (
                        -os.WTERMSIG(status) if os.WIFSIGNALED(status)
                        else os.WEXITSTATUS(status)
                    )
                else:
                    rc = None  # still running (shouldn't happen post-loop)
            except ChildProcessError:
                rc = -1  # already reaped by someone else
        return rc

    def terminate(self) -> None:
        try:
            os.kill(self._proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    def kill(self) -> None:
        try:
            os.kill(self._proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    async def wait(self):
        """Awaitable wait — forwards to the underlying asyncio Process."""
        return await self._proc.wait()


# ── Group kill ──────────────────────────────────────────────────────────────


def pgid_of(proc: "subprocess.Popen") -> Optional[int]:
    """The child's process-group id, or None once it is gone."""
    try:
        return os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError, OSError):
        return None


def group_alive(pgid: int) -> bool:
    """True while ANY process remains in *pgid* (a zombie counts until reaped)."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _signal_group(pgid: int, sig: int) -> bool:
    try:
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def signal_group(pgid: int, sig: int) -> bool:
    """Public wrapper around _signal_group — for callers that need to signal
    a specific group without going through the full terminate_child path."""
    return _signal_group(pgid, sig)


def _own_group(pgid: Optional[int]) -> bool:
    """True when *pgid* is the group this very process lives in — signalling it
    would kill the runner instead of the child."""
    if pgid is None:
        return False
    try:
        return pgid == os.getpgrp()
    except OSError:  # pragma: no cover — POSIX always has it
        return False


def _wait_gone(proc: "subprocess.Popen", pgid: Optional[int],
               timeout: float) -> bool:
    """Poll until the child is reaped AND its group is empty.

    ``proc.poll()`` (non-blocking waitpid) is what REAPS the direct child — an
    unreaped zombie keeps answering ``kill(pgid, 0)`` forever. Polling instead
    of ``proc.wait()`` also keeps this callable from a signal handler: a
    blocking wait would deadlock on Popen's own waitpid lock.
    """
    deadline = time.time() + timeout
    while True:
        proc.poll()
        if proc.returncode is not None and (pgid is None or not group_alive(pgid)):
            return True
        if time.time() >= deadline:
            return proc.returncode is not None and (pgid is None
                                                    or not group_alive(pgid))
        time.sleep(0.05)


def terminate_child(proc: "subprocess.Popen", *, grace: float = KILL_GRACE_SEC,
                    pgid: Optional[int] = None) -> bool:
    """TERM the child's whole process GROUP, KILL it if it outlives *grace*.

    The group — not the pid: the claude CLI's subagent/MCP children survive a
    bare pid TERM and are the part that keeps burning tokens (#18). Falls back
    to the pid alone when the child is NOT in a group of its own (never signal
    our own group). Always reaps, so no zombie is left behind. Returns True when
    the child is verified dead and its group empty — the caller must not move a
    task directory on False.
    """
    if pgid is None:
        with _LOCK:
            entry = _LIVE.get(proc.pid)
        pgid = entry[1] if entry else pgid_of(proc)
    if _own_group(pgid):
        pgid = None

    try:
        if pgid is not None:
            _signal_group(pgid, signal.SIGTERM)
        else:
            proc.terminate()
    except OSError:
        untrack(proc)
        return proc.poll() is not None

    gone = _wait_gone(proc, pgid, grace)
    if not gone:
        try:
            if pgid is not None:
                _signal_group(pgid, signal.SIGKILL)
            else:
                proc.kill()
        except OSError:
            pass
        gone = _wait_gone(proc, pgid, grace)
    untrack(proc)
    return gone


def kill_group_leftovers(pgid: Optional[int], *,
                         grace: float = KILL_GRACE_SEC) -> bool:
    """TERM→KILL whatever is STILL in *pgid* after the direct child exited.

    A normally-finished claude CLI can leave a detached MCP server or subagent
    behind; nothing else would ever kill it, since the pid the runner knows is
    already reaped. Returns True when something had to be killed.
    """
    if pgid is None or _own_group(pgid) or not group_alive(pgid):
        return False
    _signal_group(pgid, signal.SIGTERM)
    deadline = time.time() + grace
    while time.time() < deadline:
        if not group_alive(pgid):
            return True
        time.sleep(0.05)
    _signal_group(pgid, signal.SIGKILL)
    return True


def kill_tracked(*, grace: float = KILL_GRACE_SEC) -> int:
    """Kill the groups of every child THIS process still owns. Returns the count."""
    entries = tracked()
    killed = 0
    for proc, pgid in entries:
        if proc.poll() is not None and not group_alive(pgid):
            untrack(proc)
            continue
        if not terminate_child(proc, grace=grace, pgid=pgid):
            _log(f"[proc-reaper] WARNING: claude child pid={proc.pid} "
                 f"(pgid={pgid}) survived TERM+KILL")
        killed += 1
    return killed


# ── Runner-side wiring: a killed runner takes its children with it ──────────

_installed = False


def _on_signal(signum: int, _frame: object) -> None:
    """SIGTERM/SIGINT on the RUNNER: kill the children, then die of the same
    signal (default handler re-raised, so the exit status stays truthful for
    whoever is watching this pid)."""
    killed = kill_tracked(grace=SIGNAL_GRACE_SEC)
    if killed:
        _log(f"[proc-reaper] {signal.Signals(signum).name}: killed {killed} "
             f"claude child group(s) before exiting")
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def install() -> None:
    """Wire the SIGTERM/SIGINT handlers + the atexit belt. Idempotent; only the
    main thread may install signal handlers, so a non-main caller still gets the
    atexit half."""
    global _installed
    if _installed:
        return
    _installed = True
    atexit.register(_atexit_kill)
    try:
        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)
    except ValueError:  # not the main thread
        pass


def _atexit_kill() -> None:
    killed = kill_tracked(grace=SIGNAL_GRACE_SEC)
    if killed:
        _log(f"[proc-reaper] atexit: killed {killed} orphaned claude child "
             f"group(s)")


# ── Orphan sweep ────────────────────────────────────────────────────────────


class ProcRow(NamedTuple):
    pid: int
    ppid: int
    tty: str
    args: str


def parse_ps(output: str) -> "list[ProcRow]":
    """Rows of ``ps -axo pid,ppid,tty,args``; header and junk lines dropped."""
    rows: list[ProcRow] = []
    for line in (output or "").splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue  # the "PID PPID TTY ARGS" header
        rows.append(ProcRow(pid, ppid, parts[2], parts[3]))
    return rows


# Interpreters the CLI may be launched through — for these, and ONLY these, the
# second token is inspected too (`node .../claude-code/cli.js`). Anything else
# in argv[0] (grep, sh, an editor) is not the CLI no matter what it quotes.
_LAUNCHERS = frozenset({"node", "nodejs", "bun", "deno", "python", "python3",
                        "env"})


def _looks_like_claude(token: str) -> bool:
    return any(part == "claude" or part.startswith(("claude-", "claude."))
               for part in token.split("/") if part)


def _is_claude_cli(args: str) -> bool:
    """True when the process IS the claude CLI — not a shell, editor or grep
    that merely happens to carry the flags on its command line."""
    tokens = args.split()
    if not tokens:
        return False
    if _looks_like_claude(tokens[0]):
        return True
    if os.path.basename(tokens[0]) in _LAUNCHERS and len(tokens) > 1:
        return _looks_like_claude(tokens[1])
    return False


def is_pipeline_orphan(row: ProcRow) -> bool:
    """The full safety table. EVERY condition is load-bearing — see the module
    docstring: an interactive Claude Code session fails at least one of them."""
    if row.pid <= 1 or row.pid == os.getpid():
        return False
    if row.ppid != 1:                      # a live parent still owns it
        return False
    if row.tty not in _NO_TTY:             # a human is attached to it
        return False
    if not _is_claude_cli(row.args):
        return False
    return all(marker in row.args for marker in SIGNATURE_ARGS)


def find_orphans(ps_output: str) -> "list[ProcRow]":
    return [row for row in parse_ps(ps_output) if is_pipeline_orphan(row)]


def _read_ps() -> str:
    try:
        proc = subprocess.run(list(PS_ARGV), capture_output=True, text=True,
                              timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        _log(f"[proc-reaper] ps failed: {exc}")
        return ""
    return proc.stdout or ""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _kill_orphan(row: ProcRow, grace: float) -> str:
    """TERM the orphan (its whole group when it leads one), KILL after *grace*.

    A pre-#18 orphan is NOT a group leader — its pgid belongs to the runner that
    died and may since have been recycled onto an unrelated process, so the
    group is only signalled when the orphan itself leads it.
    """
    try:
        pgid = os.getpgid(row.pid)
    except OSError:
        pgid = None
    group = pgid if (pgid is not None and pgid == row.pid
                     and not _own_group(pgid)) else None

    if group is not None:
        _signal_group(group, signal.SIGTERM)
    else:
        try:
            os.kill(row.pid, signal.SIGTERM)
        except OSError:
            return "gone"

    deadline = time.time() + grace
    while time.time() < deadline:
        if not _pid_alive(row.pid):
            return "TERM"
        time.sleep(0.1)

    if group is not None:
        _signal_group(group, signal.SIGKILL)
    else:
        try:
            os.kill(row.pid, signal.SIGKILL)
        except OSError:
            return "TERM"
    return "KILL"


def sweep(*, grace: float = KILL_GRACE_SEC,
          log: Optional[Callable[[str], None]] = None,
          dry_run: bool = False) -> "list[ProcRow]":
    """Find and kill pipeline-signature orphans. Returns the rows acted on.

    One log line per kill — an orphan is a silent token burner, so its death is
    the only record that it ever existed.
    """
    emit = log or _log
    rows = find_orphans(_read_ps())
    for row in rows:
        if dry_run:
            emit(f"[proc-reaper] orphan claude pid={row.pid} (ppid=1, no tty) "
                 f"— dry-run, not killed")
            continue
        how = _kill_orphan(row, grace)
        emit(f"[proc-reaper] killed orphaned claude pid={row.pid} ({how}) — "
             f"ppid=1, no tty, pipeline signature")
    return rows


def main(argv: "Optional[Iterable[str]]" = None) -> int:
    """CLI: ``python3 dispatcher/proc_reaper.py [--dry-run]``. Used by
    ``ops/atlas/aidstack.sh down`` — stopping the stack must not leave a child
    behind."""
    args = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in args or "-n" in args
    rows = sweep(dry_run=dry_run)
    if not rows:
        _log("[proc-reaper] no orphaned claude children")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
