"""Nobody may outlive the runner that spawned it (issue #18).

Incident 2026-08-14: operator-killed stage runners left their ``claude``
children running, re-parented to init — one burned its agentic loop for 3 h 11 m
against the subscription with no task and no owner. The same race let the dying
runner's stage write ``03-dev-agent.md`` back into a re-created
``active/<task>/`` after the limit park had already moved the dir away.

Covered here:
  * process-group ownership — a real child spawned in its own session, with a
    grandchild of its own, dies AS A GROUP (the claude CLI's subagent subtree is
    the part that burns tokens; a bare pid TERM left it alive);
  * the runner-side wiring — a real "runner" process that installs the handlers
    and is then SIGTERMed takes its child group down before dying;
  * the orphan-sweep matcher — a pure function over ``ps`` fixtures, including
    the safety exclusions that spare the owner's interactive Claude Code
    sessions (they carry the same --dangerously-skip-permissions flag);
  * park ordering — no live child is left when the task dir moves.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import limit_stall as ls  # noqa: E402
import proc_reaper as pr  # noqa: E402
import stage_runner_agent as sra  # noqa: E402


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_dead(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


# A child that spawns a grandchild of its own and reports its pid — the shape of
# the claude CLI, which is a thin wrapper over the subagent processes that cost
# money. Both must die.
_CHILD_WITH_GRANDCHILD = textwrap.dedent(
    """
    import subprocess, sys, time
    g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    sys.stdout.write("%d\\n" % g.pid)
    sys.stdout.flush()
    time.sleep(300)
    """
)


class GroupKillTests(unittest.TestCase):
    """terminate_child kills the GROUP, not just the pid it was handed."""

    def test_child_and_grandchild_die_together(self) -> None:
        proc = pr.spawn([sys.executable, "-c", _CHILD_WITH_GRANDCHILD],
                        stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.stdout.close)
        grandchild = int(proc.stdout.readline().strip())
        self.addCleanup(lambda: _alive(grandchild) and os.kill(grandchild, signal.SIGKILL))

        self.assertTrue(_alive(grandchild))
        self.assertTrue(pr.terminate_child(proc, grace=5))

        self.assertIsNotNone(proc.returncode)          # reaped, not a zombie
        self.assertTrue(_wait_dead(grandchild))        # the group went with it

    def test_child_leads_its_own_group(self) -> None:
        """start_new_session is what makes the group kill safe: without it the
        child shares the runner's group and killpg would TERM the runner."""
        proc = pr.spawn([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(proc.kill)
        self.assertEqual(pr.pgid_of(proc), proc.pid)
        self.assertNotEqual(pr.pgid_of(proc), os.getpgrp())

    def test_tracked_children_are_forgotten_once_killed(self) -> None:
        proc = pr.spawn([sys.executable, "-c", "import time; time.sleep(30)"])
        self.assertIn(proc.pid, [p.pid for p, _ in pr.tracked()])
        pr.terminate_child(proc, grace=5)
        self.assertNotIn(proc.pid, [p.pid for p, _ in pr.tracked()])

    def test_kill_tracked_reports_what_it_killed(self) -> None:
        procs = [pr.spawn([sys.executable, "-c", "import time; time.sleep(30)"])
                 for _ in range(2)]
        self.assertEqual(pr.kill_tracked(grace=5), 2)
        for proc in procs:
            self.assertIsNotNone(proc.returncode)
        self.assertEqual(pr.tracked(), [])


class RunnerSignalTests(unittest.TestCase):
    """A killed runner must take its children down before dying — the whole
    point of #18. Real processes: a runner that installs the handlers, spawns a
    child (which spawns a grandchild), then gets SIGTERM/SIGINT."""

    RUNNER = textwrap.dedent(
        """
        import subprocess, sys, time
        sys.path.insert(0, %(dispatcher)r)
        import proc_reaper as pr
        pr.install()
        proc = pr.spawn([sys.executable, "-c", %(body)r],
                        stdout=subprocess.PIPE, text=True)
        grandchild = proc.stdout.readline().strip()
        sys.stdout.write("%%d %%s\\n" %% (proc.pid, grandchild))
        sys.stdout.flush()
        time.sleep(300)
        """
    )

    def _spawn_runner(self) -> "tuple[subprocess.Popen, int, int]":
        src = self.RUNNER % {
            "dispatcher": str(REPO_ROOT / "dispatcher"),
            "body": _CHILD_WITH_GRANDCHILD,
        }
        runner = subprocess.Popen([sys.executable, "-c", src],
                                  stdout=subprocess.PIPE, text=True,
                                  start_new_session=True)
        self.addCleanup(runner.stdout.close)
        child, grandchild = (int(x) for x in runner.stdout.readline().split())
        self.addCleanup(lambda: runner.poll() is None and runner.kill())
        for pid in (child, grandchild):
            self.addCleanup(lambda p=pid: _alive(p) and os.kill(p, signal.SIGKILL))
        return runner, child, grandchild

    def _assert_signal_reaps(self, sig: int) -> None:
        runner, child, grandchild = self._spawn_runner()
        self.assertTrue(_alive(child) and _alive(grandchild))

        runner.send_signal(sig)
        runner.wait(timeout=30)

        self.assertTrue(_wait_dead(child), "runner died and left its claude child")
        self.assertTrue(_wait_dead(grandchild),
                        "runner died and left the child's own subtree")

    def test_sigterm_takes_the_child_group_down(self) -> None:
        self._assert_signal_reaps(signal.SIGTERM)

    def test_sigint_takes_the_child_group_down(self) -> None:
        self._assert_signal_reaps(signal.SIGINT)

    def test_runner_still_dies_of_the_signal(self) -> None:
        """The exit status stays truthful (-SIGTERM), so whoever watches this
        pid still sees a signalled death rather than a clean exit."""
        runner, _child, _grandchild = self._spawn_runner()
        runner.send_signal(signal.SIGTERM)
        self.assertEqual(runner.wait(timeout=30), -signal.SIGTERM)


class OrphanMatcherTests(unittest.TestCase):
    """The sweep's safety table. Pure function over ps fixtures — every negative
    here is a process that MUST survive the sweep."""

    HEADER = "  PID  PPID TTY      ARGS\n"
    ORPHAN = ("95225     1 ??       claude --dangerously-skip-permissions "
              "--session-id 2ee41342-0000-0000-0000-000000000000 "
              "--output-format stream-json --verbose -p prompt")

    def _rows(self, *lines: str) -> "list[pr.ProcRow]":
        return pr.find_orphans(self.HEADER + "\n".join(lines) + "\n")

    def test_pipeline_orphan_is_matched(self) -> None:
        found = self._rows(self.ORPHAN)
        self.assertEqual([r.pid for r in found], [95225])

    def test_interactive_session_with_a_tty_is_never_touched(self) -> None:
        """The near-miss of 2026-08-12: the owner's own sessions run with the
        same --dangerously-skip-permissions flag."""
        self.assertEqual(
            self._rows("1395     1 ttys001  claude --dangerously-skip-permissions "
                       "--output-format stream-json"),
            [])

    def test_bare_interactive_session_is_never_touched(self) -> None:
        self.assertEqual(
            self._rows("1225  1080 ttys000  claude --dangerously-skip-permissions"),
            [])

    def test_live_child_of_a_running_runner_is_never_touched(self) -> None:
        """ppid != 1 — a runner is still alive and owns this child."""
        self.assertEqual(
            self._rows("95225 60123 ??       claude --dangerously-skip-permissions "
                       "--output-format stream-json --verbose -p prompt"),
            [])

    def test_signature_needs_both_flags(self) -> None:
        self.assertEqual(
            self._rows("95225     1 ??       claude --dangerously-skip-permissions "
                       "-p prompt"),
            [])
        self.assertEqual(
            self._rows("95225     1 ??       claude --output-format stream-json "
                       "-p prompt"),
            [])

    def test_other_programs_quoting_the_flags_are_never_touched(self) -> None:
        """argv[0] must BE the claude CLI — a grep/editor whose command line
        merely contains the flags is not a runaway agent."""
        self.assertEqual(
            self._rows("77000     1 ??       grep -r --dangerously-skip-permissions "
                       "--output-format stream-json /Users/x/ai-delivery"),
            [])

    def test_node_wrapped_cli_is_matched(self) -> None:
        """ps may show the CLI through its interpreter — missing a real orphan
        is the failure mode the argv[0] check must not introduce."""
        found = self._rows(
            "95226     1 ??       node /usr/local/lib/node_modules/@anthropic-ai/"
            "claude-code/cli.js --dangerously-skip-permissions "
            "--output-format stream-json -p prompt")
        self.assertEqual([r.pid for r in found], [95226])

    def test_wrapper_script_is_not_the_cli(self) -> None:
        self.assertEqual(
            self._rows("95227     1 ??       /bin/sh /Users/x/run-agent.sh "
                       "--dangerously-skip-permissions --output-format "
                       "stream-json"),
            [])

    def test_desktop_app_is_never_touched(self) -> None:
        self.assertEqual(
            self._rows("2081     1 ??       /Applications/Claude.app/Contents/"
                       "MacOS/Claude"),
            [])

    def test_linux_ps_shapes_parse(self) -> None:
        """`?` for no tty, `pts/3` for one — procps prints both differently."""
        found = self._rows(
            "4242     1 ?        /usr/local/bin/claude "
            "--dangerously-skip-permissions --output-format stream-json -p x",
            "4243     1 pts/3    /usr/local/bin/claude "
            "--dangerously-skip-permissions --output-format stream-json -p x")
        self.assertEqual([r.pid for r in found], [4242])

    def test_header_and_junk_lines_are_dropped(self) -> None:
        self.assertEqual(pr.parse_ps(self.HEADER + "garbage\n"), [])

    def test_the_sweeper_never_matches_itself(self) -> None:
        self.assertFalse(pr.is_pipeline_orphan(
            pr.ProcRow(os.getpid(), 1, "??",
                       "claude --dangerously-skip-permissions "
                       "--output-format stream-json")))
        self.assertFalse(pr.is_pipeline_orphan(
            pr.ProcRow(1, 0, "??", "claude --dangerously-skip-permissions "
                                   "--output-format stream-json")))

    def test_sweep_over_the_real_process_table_is_safe(self) -> None:
        """Dry-run against this machine's actual ps output: it must not select
        the interactive Claude Code session this suite may be running under."""
        rows = pr.sweep(dry_run=True, log=lambda _msg: None)
        for row in rows:
            self.assertEqual(row.ppid, 1)
            self.assertIn(row.tty, pr._NO_TTY)


class ParkOrderingTests(unittest.TestCase):
    """The dir may only move once nothing can write into it any more."""

    def test_children_are_dead_before_the_park_moves_the_dir(self) -> None:
        proc = pr.spawn([sys.executable, "-c", "import time; time.sleep(300)"])
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        seen: dict = {}

        def _fake_park(task_dir, task_id, **kwargs):
            seen["child_dead"] = proc.poll() is not None
            seen["tracked"] = pr.tracked()
            return task_dir

        orig_park, orig_hint = ls.park, sra._limit_stall_text
        ls.park, sra._limit_stall_text = _fake_park, lambda *_a, **_k: ""
        try:
            sra._park_limit_stall(Path("/nonexistent/task"), "TASK-X",
                                  "developer", 1.0, 2.0)
        finally:
            ls.park, sra._limit_stall_text = orig_park, orig_hint

        self.assertTrue(seen.get("child_dead"),
                        "park() moved the task dir while a claude child was live")
        self.assertEqual(seen.get("tracked"), [])

    def test_moved_task_dir_is_re_resolved_not_re_created(self) -> None:
        """The split-brain half of #18: a post-move write must follow the task
        to its bucket instead of re-creating active/<task>/."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tasks = Path(tmp) / "tasks"
            stale = tasks / "active" / "stt-local-path-source"
            moved = tasks / "awaiting-input" / "stt-local-path-source"
            moved.mkdir(parents=True)

            self.assertEqual(sra._task_dir_now(stale), moved)
            self.assertFalse(stale.exists(), "re-resolving must not re-create it")

    def test_existing_task_dir_is_returned_untouched(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / "tasks" / "active" / "task"
            live.mkdir(parents=True)
            self.assertEqual(sra._task_dir_now(live), live)


class VanishedTaskDirTests(unittest.TestCase):
    """Runner-side breadcrumbs must tolerate a dir that was parked away — and
    must never mkdir it back into existence."""

    def test_state_writes_are_dropped_not_recreated(self) -> None:
        import tempfile
        from runner_state import _append_history, _append_worklog, _update_state
        with tempfile.TemporaryDirectory() as tmp:
            gone = Path(tmp) / "active" / "task"
            _update_state(gone, stage="developer")
            _append_history(gone, "developer", "note")
            _append_worklog(gone, "note")
            self.assertFalse(gone.exists())

    def test_a_real_missing_state_file_still_raises(self) -> None:
        """Only a VANISHED DIR is tolerated; a corrupt/missing state.json in a
        live task dir stays a loud failure."""
        import tempfile
        from runner_state import _update_state
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                _update_state(Path(tmp), stage="developer")


REPO_ROOT_BOT = Path(__file__).resolve().parent.parent / "bot"
sys.path.insert(0, str(REPO_ROOT_BOT))

import watchdog as _wd_mod  # noqa: E402


class AsyncChildTests(unittest.TestCase):
    """Unit tests for the AsyncChild adapter (proc_reaper.AsyncChild)."""

    class _FakeProc:
        def __init__(self, pid: int, returncode=None):
            self.pid = pid
            self.returncode = returncode

    class _FakeLoop:
        def __init__(self, closed: bool):
            self._closed = closed

        def is_closed(self) -> bool:
            return self._closed

    def test_poll_returns_none_while_loop_open_and_child_live(self) -> None:
        """poll() must not call waitpid while the loop is open (ADR-002)."""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(proc.kill)
        fake_proc = self._FakeProc(proc.pid, returncode=None)
        child = pr.AsyncChild(fake_proc, self._FakeLoop(closed=False))
        result = child.poll()
        self.assertIsNone(result)

    def test_poll_returns_returncode_when_already_set(self) -> None:
        """poll() forwards a non-None returncode without calling waitpid."""
        fake_proc = self._FakeProc(pid=99999, returncode=0)
        child = pr.AsyncChild(fake_proc, self._FakeLoop(closed=False))
        self.assertEqual(child.poll(), 0)

    def test_poll_reaps_when_loop_closed(self) -> None:
        """poll() may call os.waitpid only when the loop is closed."""
        proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(42)"])
        proc.wait()   # kernel reaps into zombie state until we waitpid
        # proc.returncode is set by Popen.wait() but we simulate an AsyncChild
        # scenario where returncode is still None (loop was open at wait() time)
        # by creating a fresh FakeProc.  Then we use a closed-loop so poll()
        # will call waitpid itself.  Because proc.wait() already reaped the
        # child, waitpid raises ChildProcessError, and poll() returns -1.
        fake_proc = self._FakeProc(pid=proc.pid, returncode=None)
        child = pr.AsyncChild(fake_proc, self._FakeLoop(closed=True))
        rc = child.poll()
        # Either -1 (already reaped by Popen.wait → ChildProcessError) or
        # the actual exit code if the zombie wasn't collected yet.
        self.assertIsNotNone(rc)

    def test_pid_property_delegates_to_proc(self) -> None:
        fake_proc = self._FakeProc(pid=12345, returncode=None)
        child = pr.AsyncChild(fake_proc, self._FakeLoop(closed=False))
        self.assertEqual(child.pid, 12345)

    def test_returncode_property_delegates_to_proc(self) -> None:
        fake_proc = self._FakeProc(pid=1, returncode=7)
        child = pr.AsyncChild(fake_proc, self._FakeLoop(closed=True))
        self.assertEqual(child.returncode, 7)

    def test_signal_group_is_public(self) -> None:
        """signal_group() must exist and be callable (used by _shutdown_children)."""
        self.assertTrue(callable(pr.signal_group))


class WatchdogTerminationTests(unittest.TestCase):
    """Unit tests for Watchdog.record_termination and the non-downgrade rule."""

    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._state_path = Path(self._tmp.name) / "watchdog.json"
        self._wd = _wd_mod.Watchdog(state_path=self._state_path)
        # Set up a root task and a pending dispatch.
        self._root_id = "root-test-001"
        self._task_id = "task-abc123"
        self._wd.start_root_task(self._root_id)
        self._wd.record_dispatch(
            self._root_id, self._task_id, None, "developer", "/tmp/proj",
            "do the thing",
        )

    def _find_entry(self):
        root = self._wd.roots[self._root_id]
        for d in root.dispatches:
            if d.task_id == self._task_id:
                return d
        return None

    def test_record_termination_sets_terminated_status(self) -> None:
        self._wd.record_termination(self._root_id, self._task_id)
        entry = self._find_entry()
        self.assertEqual(entry.status, "terminated")
        self.assertIsNotNone(entry.completed_at)

    def test_record_termination_persists_to_disk(self) -> None:
        self._wd.record_termination(self._root_id, self._task_id)
        wd2 = _wd_mod.Watchdog(state_path=self._state_path)
        root2 = wd2.roots.get(self._root_id)
        self.assertIsNotNone(root2)
        for d in root2.dispatches:
            if d.task_id == self._task_id:
                self.assertEqual(d.status, "terminated")
                return
        self.fail("task_id not found in reloaded watchdog state")

    def test_record_completion_does_not_downgrade_terminated(self) -> None:
        """A late record_completion after shutdown must not overwrite terminated."""
        self._wd.record_termination(self._root_id, self._task_id)
        self._wd.record_completion(self._root_id, self._task_id, success=True)
        entry = self._find_entry()
        self.assertEqual(entry.status, "terminated")

    def test_record_completion_still_works_for_non_terminated(self) -> None:
        """The guard must not break the normal completion path."""
        self._wd.record_completion(self._root_id, self._task_id, success=False)
        entry = self._find_entry()
        self.assertEqual(entry.status, "failed")

    def test_stuck_pending_ignores_terminated_entries(self) -> None:
        """terminated is non-pending — stuck_pending must not flag it."""
        self._wd.record_termination(self._root_id, self._task_id)
        root = self._wd.roots[self._root_id]
        # Force last_progress_at to be very old on the terminated entry.
        for d in root.dispatches:
            if d.task_id == self._task_id:
                d.last_progress_at = 0.0
        stuck = root.stuck_pending(idle_sec=1)
        self.assertIsNone(stuck)


# ---------------------------------------------------------------------------
# Bot-shaped harness: a self-contained asyncio script that mirrors the bot's
# _shutdown_children() kill path for real-process integration tests.
# ---------------------------------------------------------------------------

_BOT_HARNESS = textwrap.dedent(
    """
    import asyncio, os, signal, sys, time
    sys.path.insert(0, %(dispatcher)r)
    import proc_reaper as pr

    GRACE = float(os.environ.get("ORPHAN_SIGNAL_GRACE_SEC", "5"))

    async def _shutdown(entries):
        own_pgid = os.getpgrp()
        to_signal = []
        for handle, pgid in entries:
            if handle.poll() is not None:
                pr.untrack(handle)
                continue
            if pgid == own_pgid:
                continue
            to_signal.append((handle, pgid))
        for handle, pgid in to_signal:
            pr.signal_group(pgid, signal.SIGTERM)
        if to_signal:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(h.wait() for h, _ in to_signal),
                                   return_exceptions=True),
                    timeout=GRACE,
                )
            except asyncio.TimeoutError:
                pass
        for handle, pgid in to_signal:
            if pr.group_alive(pgid):
                pr.signal_group(pgid, signal.SIGKILL)
            pr.untrack(handle)
        if to_signal:
            import sys as _sys
            _sys.stderr.write("shutdown: killed %%d child group(s)\\n" %% len(to_signal))

    async def main():
        stop_event = asyncio.Event()
        exit_signal = 0

        def _on_sig(signum):
            nonlocal exit_signal
            exit_signal = signum
            stop_event.set()

        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, _on_sig, signal.SIGTERM)
        loop.add_signal_handler(signal.SIGINT, _on_sig, signal.SIGINT)

        children_info = []
        for body in %(bodies)r:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", body,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            child = pr.AsyncChild(proc, loop)
            pr.track(child, proc.pid)
            grandchild_pid = int((await proc.stdout.readline()).strip())
            children_info.append((proc.pid, grandchild_pid))

        # Report pids to the test
        for cpid, gpid in children_info:
            sys.stdout.write("%%d %%d\\n" %% (cpid, gpid))
        sys.stdout.flush()

        try:
            await stop_event.wait()
        finally:
            entries = pr.tracked()
            await _shutdown(entries)

        if exit_signal:
            signal.signal(exit_signal, signal.SIG_DFL)
            os.kill(os.getpid(), exit_signal)

    asyncio.run(main())
    """
)

_BOT_HARNESS_NORMAL_EXIT = textwrap.dedent(
    """
    import asyncio, os, signal, sys
    sys.path.insert(0, %(dispatcher)r)
    import proc_reaper as pr

    GRACE = float(os.environ.get("ORPHAN_SIGNAL_GRACE_SEC", "5"))

    async def _shutdown(entries):
        own_pgid = os.getpgrp()
        to_signal = []
        for handle, pgid in entries:
            if handle.poll() is not None:
                pr.untrack(handle)
                continue
            if pgid == own_pgid:
                continue
            to_signal.append((handle, pgid))
        for handle, pgid in to_signal:
            pr.signal_group(pgid, signal.SIGTERM)
        if to_signal:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(h.wait() for h, _ in to_signal),
                                   return_exceptions=True),
                    timeout=GRACE,
                )
            except asyncio.TimeoutError:
                pass
        for handle, pgid in to_signal:
            if pr.group_alive(pgid):
                pr.signal_group(pgid, signal.SIGKILL)
            pr.untrack(handle)

    async def main():
        loop = asyncio.get_running_loop()
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", %(body)r,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        child = pr.AsyncChild(proc, loop)
        pr.track(child, proc.pid)
        grandchild_pid = int((await proc.stdout.readline()).strip())
        sys.stdout.write("%%d %%d\\n" %% (proc.pid, grandchild_pid))
        sys.stdout.flush()
        # Normal exit — finally still kills children
        try:
            pass
        finally:
            entries = pr.tracked()
            await _shutdown(entries)

    asyncio.run(main())
    """
)


class BotSignalTests(unittest.TestCase):
    """A bot-shaped asyncio process kills its registered children on exit.

    Real processes only — no signal mocking (FR-016).
    """

    _ENV = {**os.environ, "ORPHAN_SIGNAL_GRACE_SEC": "2"}

    def _spawn_harness(self, bodies=None):
        if bodies is None:
            bodies = [_CHILD_WITH_GRANDCHILD]
        src = _BOT_HARNESS % {
            "dispatcher": str(REPO_ROOT / "dispatcher"),
            "bodies": bodies,
        }
        harness = subprocess.Popen(
            [sys.executable, "-c", src],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=self._ENV,
        )
        self.addCleanup(harness.stdout.close)
        self.addCleanup(harness.stderr.close)
        self.addCleanup(lambda: harness.poll() is None and harness.kill())

        children = []
        for _ in bodies:
            line = harness.stdout.readline().strip()
            cpid, gpid = (int(x) for x in line.split())
            children.append((cpid, gpid))
            self.addCleanup(
                lambda p=cpid: _alive(p) and os.kill(p, signal.SIGKILL)
            )
            self.addCleanup(
                lambda p=gpid: _alive(p) and os.kill(p, signal.SIGKILL)
            )
        return harness, children

    def _assert_signal_kills_children(self, sig: int) -> None:
        harness, children = self._spawn_harness()
        child_pid, grandchild_pid = children[0]
        self.assertTrue(_alive(child_pid))
        self.assertTrue(_alive(grandchild_pid))

        harness.send_signal(sig)
        harness.wait(timeout=30)

        self.assertTrue(_wait_dead(child_pid),
                        "harness left its claude child alive")
        self.assertTrue(_wait_dead(grandchild_pid),
                        "harness left the child's own subtree alive")

    def test_sigterm_kills_child_and_grandchild(self) -> None:
        """FR-003: SIGTERM → child and grandchild both die."""
        self._assert_signal_kills_children(signal.SIGTERM)

    def test_sigint_kills_child_and_grandchild(self) -> None:
        """FR-004: SIGINT → same result."""
        self._assert_signal_kills_children(signal.SIGINT)

    def test_harness_exits_with_sigterm_status(self) -> None:
        """FR-009: exit status reflects SIGTERM, not a generic code."""
        harness, _ = self._spawn_harness()
        harness.send_signal(signal.SIGTERM)
        rc = harness.wait(timeout=30)
        self.assertEqual(rc, -signal.SIGTERM)

    def test_two_children_both_killed_on_sigterm(self) -> None:
        """FR-008: two registered children — SIGTERM kills both."""
        harness, children = self._spawn_harness(
            bodies=[_CHILD_WITH_GRANDCHILD, _CHILD_WITH_GRANDCHILD]
        )
        for cpid, gpid in children:
            self.assertTrue(_alive(cpid))
            self.assertTrue(_alive(gpid))

        harness.send_signal(signal.SIGTERM)
        harness.wait(timeout=30)

        for cpid, gpid in children:
            self.assertTrue(_wait_dead(cpid),
                            f"child pid={cpid} survived SIGTERM")
            self.assertTrue(_wait_dead(gpid),
                            f"grandchild pid={gpid} survived SIGTERM")

    def test_normal_exit_kills_child(self) -> None:
        """FR-005: normal exit path also kills registered children."""
        src = _BOT_HARNESS_NORMAL_EXIT % {
            "dispatcher": str(REPO_ROOT / "dispatcher"),
            "body": _CHILD_WITH_GRANDCHILD,
        }
        harness = subprocess.Popen(
            [sys.executable, "-c", src],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=self._ENV,
        )
        self.addCleanup(harness.stdout.close)
        self.addCleanup(harness.stderr.close)
        self.addCleanup(lambda: harness.poll() is None and harness.kill())
        line = harness.stdout.readline().strip()
        child_pid, grandchild_pid = (int(x) for x in line.split())
        self.addCleanup(
            lambda p=child_pid: _alive(p) and os.kill(p, signal.SIGKILL)
        )
        self.addCleanup(
            lambda p=grandchild_pid: _alive(p) and os.kill(p, signal.SIGKILL)
        )

        harness.wait(timeout=30)

        self.assertTrue(_wait_dead(child_pid),
                        "normal-exit harness left its child alive")
        self.assertTrue(_wait_dead(grandchild_pid),
                        "normal-exit harness left the grandchild alive")

    def test_already_exited_child_skipped_without_error(self) -> None:
        """FR-011: an exited child is dropped from the registry silently."""
        harness, children = self._spawn_harness()
        child_pid, grandchild_pid = children[0]
        # Kill the child group manually before signalling the harness.
        os.killpg(child_pid, signal.SIGKILL)
        _wait_dead(child_pid)
        _wait_dead(grandchild_pid)

        # The harness should still shut down cleanly with no stuck process.
        harness.send_signal(signal.SIGTERM)
        rc = harness.wait(timeout=30)
        # Harness exits (signal-driven or clean).
        self.assertIsNotNone(rc)


if __name__ == "__main__":
    unittest.main()
