"""Watcher reconcile: terminal-but-stranded-in-active (PR3, committee 2026-06-02).

Belt-and-suspenders for bug #3 — a runner that dies after setting a terminal
stage but before the bucket move leaves the task in active/ forever (the watcher
treats 'failed'/'awaiting-input' as terminal and used to just skip it). Also
aligns the agent-path iteration_cap stop (which sets stage='awaiting-input'
without moving) with the canonical contract: the bot reads awaiting-input/, so
the task must end up there.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import watcher as w  # noqa: E402


class BucketResolverTests(unittest.TestCase):
    def test_maps_only_the_two_reconciled_stages(self) -> None:
        self.assertEqual(w._terminal_bucket_for("failed"), w.FAILED_DIR)
        self.assertEqual(w._terminal_bucket_for("failed: target gone"), w.FAILED_DIR)
        self.assertEqual(w._terminal_bucket_for("awaiting-input"), w.AWAITING_INPUT_DIR)
        # left where the existing operator flows expect them:
        self.assertIsNone(w._terminal_bucket_for("awaiting-approval"))
        self.assertIsNone(w._terminal_bucket_for("done"))
        self.assertIsNone(w._terminal_bucket_for("developer"))


class ReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {n: getattr(w, n) for n in (
            "ACTIVE_DIR", "FAILED_DIR", "AWAITING_INPUT_DIR",
            "RECONCILE_TERMINAL_IN_ACTIVE")}
        self.root = Path(tempfile.mkdtemp())
        w.ACTIVE_DIR = self.root / "active"
        w.FAILED_DIR = self.root / "failed"
        w.AWAITING_INPUT_DIR = self.root / "awaiting-input"
        w.RECONCILE_TERMINAL_IN_ACTIVE = True
        w.ACTIVE_DIR.mkdir(parents=True)

    def tearDown(self) -> None:
        for n, v in self._saved.items():
            setattr(w, n, v)
        shutil.rmtree(self.root, ignore_errors=True)

    def _mk(self, stage: str, age_s: float, pid: str | None = None,
            tid: str = "TASK-RC-1") -> Path:
        d = w.ACTIVE_DIR / tid
        d.mkdir()
        (d / "state.json").write_text(json.dumps({"stage": stage}))
        if pid is not None:
            (d / ".runner.pid").write_text(pid + "\n")
        old = time.time() - age_s
        os.utime(d / "state.json", (old, old))
        return d

    def test_failed_stranded_moves_to_failed(self) -> None:
        d = self._mk("failed", age_s=w.GRACE_PERIOD + 60)
        self.assertTrue(w._reconcile_terminal_in_active(d, "failed", d.name))
        self.assertFalse(d.exists())
        self.assertTrue((w.FAILED_DIR / d.name).is_dir())

    def test_awaiting_input_stranded_moves_to_bucket(self) -> None:
        d = self._mk("awaiting-input", age_s=w.GRACE_PERIOD + 60, pid="999999")
        self.assertTrue(w._reconcile_terminal_in_active(d, "awaiting-input", d.name))
        self.assertFalse(d.exists())
        self.assertTrue((w.AWAITING_INPUT_DIR / d.name).is_dir())

    def test_within_grace_is_left_alone(self) -> None:
        d = self._mk("failed", age_s=1)  # maybe a runner mid-move
        self.assertFalse(w._reconcile_terminal_in_active(d, "failed", d.name))
        self.assertTrue(d.exists())

    def test_flag_off_disables_reconcile(self) -> None:
        w.RECONCILE_TERMINAL_IN_ACTIVE = False
        d = self._mk("failed", age_s=w.GRACE_PERIOD + 60)
        self.assertFalse(w._reconcile_terminal_in_active(d, "failed", d.name))
        self.assertTrue(d.exists())

    def test_non_reconciled_stage_is_left_alone(self) -> None:
        d = self._mk("awaiting-approval", age_s=w.GRACE_PERIOD + 60)
        self.assertFalse(w._reconcile_terminal_in_active(d, "awaiting-approval", d.name))
        self.assertTrue(d.exists())

    def test_scan_orphans_relocates_stranded_failed(self) -> None:
        d = self._mk("failed", age_s=w.GRACE_PERIOD + 60)
        w.scan_orphans()
        self.assertFalse(d.exists())
        self.assertTrue((w.FAILED_DIR / d.name).is_dir())


if __name__ == "__main__":
    unittest.main()
