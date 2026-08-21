"""Watcher PR reconciliation (issue #4).

An awaiting-approval task with a recorded PR never gets revisited once its
stage_runner exits: the runner only sets state.stage=awaiting-approval and
leaves the task in active/ (no bucket move for that stage). If the owner
merges/closes the PR directly on github.com instead of tapping the bot's
inline keyboard, nothing reconciles the task — it orphans forever (observed:
tg-20260607-130837-5f9e sat in active/ since 2026-06-07).

_pr_reconcile_action is the pure decision core (no I/O, no gh) — tested
directly. _gh_pr_view / _reconcile_pr_task / scan_pr_reconciliation are
tested with gh, Telegram and the bot's /notify endpoint all stubbed out, so
these tests never shell out or hit the network.
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


class DecisionTests(unittest.TestCase):
    """Pure function — no subprocess, no filesystem, no network."""

    def test_merged_yields_done(self) -> None:
        self.assertEqual(
            w._pr_reconcile_action({"state": "MERGED", "mergedAt": "2026-08-10T00:00:00Z"}),
            "done",
        )

    def test_closed_yields_failed(self) -> None:
        self.assertEqual(
            w._pr_reconcile_action({"state": "CLOSED", "mergedAt": None}), "failed",
        )

    def test_open_yields_none(self) -> None:
        self.assertIsNone(w._pr_reconcile_action({"state": "OPEN", "mergedAt": None}))

    def test_gh_failure_yields_none(self) -> None:
        self.assertIsNone(w._pr_reconcile_action(None))

    def test_lowercase_state_still_matches(self) -> None:
        self.assertEqual(w._pr_reconcile_action({"state": "merged"}), "done")

    def test_unrecognized_shape_yields_none(self) -> None:
        self.assertIsNone(w._pr_reconcile_action({}))


class GhPrViewTests(unittest.TestCase):
    """subprocess.run stubbed — never shells out to a real gh."""

    def setUp(self) -> None:
        self._orig_run = w.subprocess.run

    def tearDown(self) -> None:
        w.subprocess.run = self._orig_run

    def test_success_returns_parsed_json(self) -> None:
        class _P:
            returncode = 0
            stdout = '{"state": "MERGED", "mergedAt": "2026-08-10T00:00:00Z"}'
            stderr = ""
        w.subprocess.run = lambda *a, **k: _P()
        self.assertEqual(
            w._gh_pr_view("https://github.com/o/r/pull/1"),
            {"state": "MERGED", "mergedAt": "2026-08-10T00:00:00Z"},
        )

    def test_nonzero_rc_returns_none(self) -> None:
        class _P:
            returncode = 1
            stdout = ""
            stderr = "not found"
        w.subprocess.run = lambda *a, **k: _P()
        self.assertIsNone(w._gh_pr_view("https://github.com/o/r/pull/1"))

    def test_gh_missing_returns_none_not_raise(self) -> None:
        def _boom(*a, **k):
            raise FileNotFoundError("gh: command not found")
        w.subprocess.run = _boom
        self.assertIsNone(w._gh_pr_view("https://github.com/o/r/pull/1"))

    def test_timeout_returns_none_not_raise(self) -> None:
        import subprocess as sp

        def _boom(*a, **k):
            raise sp.TimeoutExpired(cmd="gh", timeout=30)
        w.subprocess.run = _boom
        self.assertIsNone(w._gh_pr_view("https://github.com/o/r/pull/1"))

    def test_bad_json_returns_none(self) -> None:
        class _P:
            returncode = 0
            stdout = "not json"
            stderr = ""
        w.subprocess.run = lambda *a, **k: _P()
        self.assertIsNone(w._gh_pr_view("https://github.com/o/r/pull/1"))


class ReconcileTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {n: getattr(w, n) for n in (
            "ACTIVE_DIR", "AWAITING_APPROVAL_DIR", "FAILED_DIR", "DONE_DIR",
            "_gh_pr_view", "_notify_bot", "_send_telegram",
        )}
        self.root = Path(tempfile.mkdtemp())
        w.ACTIVE_DIR = self.root / "active"
        w.AWAITING_APPROVAL_DIR = self.root / "awaiting-approval"
        w.FAILED_DIR = self.root / "failed"
        w.DONE_DIR = self.root / "done"
        w.ACTIVE_DIR.mkdir(parents=True)
        self.notify_calls: list[tuple] = []
        self.telegram_calls: list[str] = []
        w._notify_bot = lambda *a, **k: self.notify_calls.append((a, k))
        w._send_telegram = lambda text: self.telegram_calls.append(text)

    def tearDown(self) -> None:
        for n, v in self._saved.items():
            setattr(w, n, v)
        shutil.rmtree(self.root, ignore_errors=True)

    def _mk(self, base: Path, pr_url: str, tid: str = "TASK-PR-1") -> Path:
        d = base / tid
        d.mkdir(parents=True)
        (d / "state.json").write_text(json.dumps({
            "id": tid, "stage": "awaiting-approval", "pr_url": pr_url,
            "history": [],
        }))
        return d

    def test_merged_moves_to_done_with_history_and_notify(self) -> None:
        d = self._mk(w.ACTIVE_DIR, "https://github.com/o/r/pull/9")
        w._gh_pr_view = lambda url: {"state": "MERGED", "mergedAt": "2026-08-10T00:00:00Z"}

        moved = w._reconcile_pr_task(d, d.name, "https://github.com/o/r/pull/9")

        self.assertTrue(moved)
        self.assertFalse(d.exists())
        dst = w.DONE_DIR / d.name
        self.assertTrue(dst.is_dir())
        state = json.loads((dst / "state.json").read_text())
        self.assertEqual(state["stage"], "done")
        self.assertTrue(state["history"])
        self.assertIn("merged", state["history"][-1]["note"].lower())
        # notified both channels, tolerating neither being reachable in prod
        self.assertEqual(len(self.telegram_calls), 1)
        self.assertIn("PR merged", self.telegram_calls[0])
        self.assertEqual(len(self.notify_calls), 1)
        args, kwargs = self.notify_calls[0]
        self.assertEqual(args[0], "pr_reconciled")
        self.assertEqual(kwargs["verdict"], "done")

    def test_closed_moves_to_failed(self) -> None:
        d = self._mk(w.ACTIVE_DIR, "https://github.com/o/r/pull/9")
        w._gh_pr_view = lambda url: {"state": "CLOSED", "mergedAt": None}

        moved = w._reconcile_pr_task(d, d.name, "https://github.com/o/r/pull/9")

        self.assertTrue(moved)
        dst = w.FAILED_DIR / d.name
        self.assertTrue(dst.is_dir())
        state = json.loads((dst / "state.json").read_text())
        self.assertEqual(state["stage"], "failed")

    def test_open_pr_is_left_alone(self) -> None:
        d = self._mk(w.ACTIVE_DIR, "https://github.com/o/r/pull/9")
        w._gh_pr_view = lambda url: {"state": "OPEN", "mergedAt": None}

        moved = w._reconcile_pr_task(d, d.name, "https://github.com/o/r/pull/9")

        self.assertFalse(moved)
        self.assertTrue(d.exists())
        self.assertEqual(self.notify_calls, [])
        self.assertEqual(self.telegram_calls, [])

    def test_gh_unreachable_is_left_alone_not_crash(self) -> None:
        d = self._mk(w.ACTIVE_DIR, "https://github.com/o/r/pull/9")
        w._gh_pr_view = lambda url: None  # simulates gh absent / network down

        moved = w._reconcile_pr_task(d, d.name, "https://github.com/o/r/pull/9")

        self.assertFalse(moved)
        self.assertTrue(d.exists())


class ScanPrReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {n: getattr(w, n) for n in (
            "ACTIVE_DIR", "AWAITING_APPROVAL_DIR", "FAILED_DIR", "DONE_DIR",
            "RECONCILE_INTERVAL_SEC", "_last_pr_reconcile_ts",
            "_reconcile_pr_task",
        )}
        self.root = Path(tempfile.mkdtemp())
        w.ACTIVE_DIR = self.root / "active"
        w.AWAITING_APPROVAL_DIR = self.root / "awaiting-approval"
        w.FAILED_DIR = self.root / "failed"
        w.DONE_DIR = self.root / "done"
        w.ACTIVE_DIR.mkdir(parents=True)
        w.AWAITING_APPROVAL_DIR.mkdir(parents=True)
        w.RECONCILE_INTERVAL_SEC = 300
        w._last_pr_reconcile_ts = 0.0
        self.reconciled: list[str] = []
        w._reconcile_pr_task = lambda d, tid, pr: (self.reconciled.append(tid) or True)

    def tearDown(self) -> None:
        for n, v in self._saved.items():
            setattr(w, n, v)
        shutil.rmtree(self.root, ignore_errors=True)

    def _mk(self, base: Path, tid: str, stage: str = "awaiting-approval",
            pr_url: str = "https://github.com/o/r/pull/1") -> None:
        d = base / tid
        d.mkdir(parents=True)
        state = {"id": tid, "stage": stage}
        if pr_url:
            state["pr_url"] = pr_url
        (d / "state.json").write_text(json.dumps(state))

    def test_reconciles_awaiting_approval_with_pr_in_active(self) -> None:
        self._mk(w.ACTIVE_DIR, "TASK-A")
        w.scan_pr_reconciliation()
        self.assertEqual(self.reconciled, ["TASK-A"])

    def test_checks_awaiting_approval_bucket_too(self) -> None:
        self._mk(w.AWAITING_APPROVAL_DIR, "TASK-B")
        w.scan_pr_reconciliation()
        self.assertEqual(self.reconciled, ["TASK-B"])

    def test_skips_task_without_pr_url(self) -> None:
        self._mk(w.ACTIVE_DIR, "TASK-C", pr_url="")
        w.scan_pr_reconciliation()
        self.assertEqual(self.reconciled, [])

    def test_skips_non_awaiting_approval_stage(self) -> None:
        self._mk(w.ACTIVE_DIR, "TASK-D", stage="developer")
        w.scan_pr_reconciliation()
        self.assertEqual(self.reconciled, [])

    def test_throttled_within_interval(self) -> None:
        self._mk(w.ACTIVE_DIR, "TASK-E")
        w.scan_pr_reconciliation()
        w.scan_pr_reconciliation()  # second call, same tick — must no-op
        self.assertEqual(self.reconciled, ["TASK-E"])

    def test_runs_again_after_interval_elapses(self) -> None:
        self._mk(w.ACTIVE_DIR, "TASK-F")
        w.scan_pr_reconciliation()
        w._last_pr_reconcile_ts = time.time() - (w.RECONCILE_INTERVAL_SEC + 1)
        w.scan_pr_reconciliation()
        self.assertEqual(self.reconciled, ["TASK-F", "TASK-F"])


if __name__ == "__main__":
    unittest.main()
