"""Tests for the deploy guard — `aidstack.sh restart` and the live-runner check.

The gap this covers: `up` is idempotent by design and never touches a live
daemon, so on a running stand it pulls new code onto disk and leaves the
daemons executing the old modules they imported at start. Meanwhile every stage
is a fresh process that reads the new code. `restart` is the command that
actually deploys, and it is guarded because restarting is the operation that
kills work in flight — a stage killed halfway is a paid Claude call thrown
away.

NB: aidstack.sh's warn() prints to stdout, not stderr — the file's own
convention, followed here rather than changed to suit the tests.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "ops" / "atlas" / "aidstack.sh"
LIVENESS = REPO_ROOT / "dispatcher" / "runner_liveness.py"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=True)


class _StandFixture(unittest.TestCase):
    """A throwaway checkout shaped like the stand, with no daemons running."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.work = self.tmp / "work"
        (self.work / "ops" / "atlas").mkdir(parents=True)
        (self.work / "dispatcher").mkdir(parents=True)
        (self.work / "tasks" / "active").mkdir(parents=True)
        (self.work / ".pids").mkdir(parents=True)

        shutil.copy(SCRIPT, self.work / "ops" / "atlas" / "aidstack.sh")
        shutil.copy(LIVENESS, self.work / "dispatcher" / "runner_liveness.py")
        # A stand-in for the real runner: what matters to the liveness check is
        # that the process command line carries the script name and the task id.
        (self.work / "dispatcher" / "stage_runner_agent.py").write_text(
            "import time, sys\ntime.sleep(300)\n"
        )

        subprocess.run(["git", "init", "-b", "dev", str(self.work)],
                       capture_output=True, check=True)
        _git(self.work, "config", "user.email", "fixture@test.io")
        _git(self.work, "config", "user.name", "fixture")
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-m", "init")

        self._runners: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        for proc in self._runners:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _start_runner(self, task_id: str) -> subprocess.Popen:
        task_dir = self.work / "tasks" / "active" / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            ["python3", str(self.work / "dispatcher" / "stage_runner_agent.py"), task_id],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._runners.append(proc)
        (task_dir / ".runner.pid").write_text(f"{proc.pid}\n")
        # The liveness check shells out to `ps`; give the process a moment to
        # exist as far as ps is concerned.
        time.sleep(0.4)
        return proc

    def _run(self, *args: str, **env_extra: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env.setdefault("AIDUP_PULL", "0")   # these tests are not about the pull
        env.update(env_extra)
        return subprocess.run(
            ["bash", str(self.work / "ops" / "atlas" / "aidstack.sh"), *args],
            cwd=str(self.work), env=env, capture_output=True, text=True)


class LiveRunnerGuardTests(_StandFixture):
    """restart / down must not kill a task that is mid-flight."""

    def test_restart_refuses_while_a_runner_is_live(self) -> None:
        self._start_runner("task-alpha")
        r = self._run("restart")
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, f"restart must refuse.\n{out}")
        self.assertIn("refused", out)
        self.assertIn("task-alpha", out, "the refusal must name the task")

    def test_down_refuses_while_a_runner_is_live(self) -> None:
        self._start_runner("task-beta")
        r = self._run("down")
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, f"down must refuse.\n{out}")
        self.assertIn("task-beta", out)

    def test_the_refusal_names_both_ways_out(self) -> None:
        self._start_runner("task-gamma")
        out = self._run("restart").stdout + self._run("restart").stderr
        self.assertIn("--wait", out)
        self.assertIn("--force", out)

    def test_force_proceeds_and_says_so(self) -> None:
        self._start_runner("task-delta")
        r = self._run("down", "--force")
        out = r.stdout + r.stderr
        self.assertIn("--force", out, "an override must announce itself")
        self.assertIn("task-delta", out)

    def test_wait_times_out_into_a_refusal_not_a_kill(self) -> None:
        """Timing out into --force would be the silent kill this guard prevents."""
        proc = self._start_runner("task-epsilon")
        r = self._run("restart", "--wait", AIDSTACK_WAIT_TIMEOUT="10")
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, f"a timeout must refuse.\n{out}")
        self.assertIn("still running", out)
        self.assertIsNone(proc.poll(), "the runner must still be alive")

    def test_no_live_runner_means_no_refusal(self) -> None:
        r = self._run("down")
        out = r.stdout + r.stderr
        self.assertNotIn("refused", out, f"nothing was running.\n{out}")

    def test_a_stale_pidfile_does_not_block_forever(self) -> None:
        """A pidfile outlives its process; a dead runner must not wedge deploys."""
        proc = self._start_runner("task-zeta")
        proc.terminate()
        proc.wait(timeout=5)
        time.sleep(0.3)
        out = self._run("down").stdout + self._run("down").stderr
        self.assertNotIn("refused", out, f"the runner is dead.\n{out}")

    def test_a_recycled_pid_is_not_mistaken_for_a_runner(self) -> None:
        """The check is not `kill -0` — an unrelated live pid must not count."""
        task_dir = self.work / "tasks" / "active" / "task-eta"
        task_dir.mkdir(parents=True)
        unrelated = subprocess.Popen(["sleep", "300"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._runners.append(unrelated)
        (task_dir / ".runner.pid").write_text(f"{unrelated.pid}\n")
        time.sleep(0.4)

        out = self._run("down").stdout + self._run("down").stderr
        self.assertNotIn("refused", out,
                         f"an unrelated live pid must not read as a runner.\n{out}")


class RestartExistsTests(_StandFixture):
    """`restart` is the deploy command; it has to be discoverable."""

    def test_restart_is_a_known_subcommand(self) -> None:
        r = self._run("restart")
        out = r.stdout + r.stderr
        self.assertNotIn("Usage:", out,
                         "restart fell through to the usage block — not wired up")

    def test_usage_names_restart_as_the_deploy_command(self) -> None:
        out = self._run("no-such-subcommand").stdout + self._run("no-such-subcommand").stderr
        self.assertIn("restart", out)
        self.assertIn("DEPLOY", out.upper())


class SplitBrainWarningTests(unittest.TestCase):
    """`up` pulls new code but never restarts a live daemon. Say so."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        self.work = self.tmp / "work"

        subprocess.run(["git", "init", "--bare", "-b", "dev", str(self.origin)],
                       capture_output=True, check=True)
        subprocess.run(["git", "init", "-b", "dev", str(self.work)],
                       capture_output=True, check=True)
        _git(self.work, "config", "user.email", "fixture@test.io")
        _git(self.work, "config", "user.name", "fixture")
        (self.work / "ops" / "atlas").mkdir(parents=True)
        (self.work / "dispatcher").mkdir(parents=True)
        (self.work / "tasks" / "active").mkdir(parents=True)
        (self.work / ".pids").mkdir(parents=True)
        shutil.copy(SCRIPT, self.work / "ops" / "atlas" / "aidstack.sh")
        shutil.copy(LIVENESS, self.work / "dispatcher" / "runner_liveness.py")
        (self.work / "README.md").write_text("v1\n")
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-m", "init")
        _git(self.work, "remote", "add", "origin", str(self.origin))
        _git(self.work, "push", "-u", "origin", "dev")

        self.daemon: subprocess.Popen | None = None

    def tearDown(self) -> None:
        if self.daemon:
            self.daemon.terminate()
            try:
                self.daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.daemon.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _advance_origin(self) -> None:
        other = self.tmp / "other"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(other)],
                       capture_output=True, check=True)
        _git(other, "config", "user.email", "other@test.io")
        _git(other, "config", "user.name", "other")
        (other / "README.md").write_text("v2\n")
        _git(other, "add", "-A")
        _git(other, "commit", "-m", "advance")
        _git(other, "push", "origin", "dev")
        shutil.rmtree(other)

    def _fake_live_daemon(self, name: str = "dispatcher") -> None:
        self.daemon = subprocess.Popen(["sleep", "300"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (self.work / ".pids" / f"{name}.pid").write_text(f"{self.daemon.pid}\n")

    def _up(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(self.work / "ops" / "atlas" / "aidstack.sh"), "up"],
            cwd=str(self.work), env=os.environ.copy(), capture_output=True, text=True)

    def test_pull_over_a_live_daemon_warns_that_nothing_was_deployed(self) -> None:
        self._advance_origin()
        self._fake_live_daemon()
        out = self._up().stdout + self._up().stderr
        self.assertIn("NOT applied", out,
                      "up must not look like a successful deploy on a live stand")
        self.assertIn("restart", out, "the warning must name the command that does deploy")

    def test_no_warning_when_nothing_was_pulled(self) -> None:
        self._fake_live_daemon()
        out = self._up().stdout + self._up().stderr
        self.assertNotIn("NOT applied", out, "nothing moved; there is nothing to warn about")

    def test_no_warning_when_no_daemon_was_running(self) -> None:
        self._advance_origin()
        out = self._up().stdout + self._up().stderr
        self.assertNotIn("NOT applied", out,
                         "a cold start deploys the new code by definition")


if __name__ == "__main__":
    unittest.main()
