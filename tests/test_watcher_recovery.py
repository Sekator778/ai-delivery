"""Watcher orphan-recovery tests.

Pin the fix for the orphan-at-received gap (observed 2026-05-31): a task that
sits in active/ at stage "received" — its runner died during/before triage,
before the first pipeline stage is written — was recovered by NEITHER daemon.
The dispatcher never revisits active/, and the watcher used to punt "received"
back to the dispatcher. The watcher must now ADOPT such pre-pipeline orphans
(past the grace period, no live runner) and respawn them; the runner's flock
makes a redundant respawn a no-op.

Also pins that "awaiting_clarify" (a paused-externally state) is treated as
terminal so the watcher never respawns a task waiting on operator answers.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import watcher as w  # noqa: E402


class OrphanAdoptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_spawn = w._spawn_runner
        self.spawned: list[str] = []
        # Record adoptions instead of really spawning a runner.
        w._spawn_runner = lambda td: (self.spawned.append(td.name) or 4242)
        w._respawn_counts.clear()

    def tearDown(self) -> None:
        w._spawn_runner = self._orig_spawn
        w._respawn_counts.clear()

    def _make_task(self, stage: str, age_s: float, pid: str | None = None) -> Path:
        d = Path(tempfile.mkdtemp()) / "TASK-0001"
        d.mkdir()
        (d / "state.json").write_text(json.dumps({"stage": stage}))
        if pid is not None:
            (d / ".runner.pid").write_text(pid + "\n")
        # Age state.json so _classify_orphan sees it as past the grace period.
        old = time.time() - age_s
        os.utime(d / "state.json", (old, old))
        return d

    def test_received_orphan_past_grace_is_adopted(self) -> None:
        d = self._make_task("received", age_s=w.GRACE_PERIOD + 60)
        w._classify_orphan(d, {"stage": "received"})
        self.assertEqual(self.spawned, [d.name])           # respawned

    def test_received_orphan_with_dead_pid_is_adopted(self) -> None:
        # A stale .runner.pid (dead/foreign pid) must not block adoption — this
        # is exactly the situation a crashed runner leaves behind.
        d = self._make_task("received", age_s=w.GRACE_PERIOD + 60, pid="999999")
        w._classify_orphan(d, {"stage": "received"})
        self.assertEqual(self.spawned, [d.name])

    def test_received_within_grace_is_left_for_dispatcher(self) -> None:
        d = self._make_task("received", age_s=1)           # fresh ingest → dispatcher's
        w._classify_orphan(d, {"stage": "received"})
        self.assertEqual(self.spawned, [])

    def test_unknown_stage_is_not_adopted(self) -> None:
        d = self._make_task("frobnicate", age_s=w.GRACE_PERIOD + 60)
        w._classify_orphan(d, {"stage": "frobnicate"})
        self.assertEqual(self.spawned, [])                 # only PRE_PIPELINE adopted

    def test_received_in_pre_pipeline_set(self) -> None:
        self.assertIn("received", w.PRE_PIPELINE_STAGES)


class TerminalStageTests(unittest.TestCase):
    def test_awaiting_clarify_is_terminal(self) -> None:
        # underscore form the agent runner writes — must be recognized as paused
        self.assertTrue(w._is_terminal_stage("awaiting_clarify"))

    def test_awaiting_input_is_terminal(self) -> None:
        self.assertTrue(w._is_terminal_stage("awaiting-input"))

    def test_failed_prefix_is_terminal(self) -> None:
        self.assertTrue(w._is_terminal_stage("failed: target_repo not found"))

    def test_pipeline_stage_is_not_terminal(self) -> None:
        self.assertFalse(w._is_terminal_stage("developer"))


if __name__ == "__main__":
    unittest.main()
