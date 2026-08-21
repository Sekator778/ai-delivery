"""Anti-thrash convergence gate (PR1, committee 2026-06-02).

Pins the compromise between "terminate early" and "actually deliver the fix":
the upgrade ladder must only grant more rope when the reviewer's critical count
is genuinely SHRINKING. The M-validation incident (tg-20260601-185117-42e1)
looped 2->2 criticals, was granted the M->L upgrade anyway, and burned an extra
(timeout-prone) tier of iterations for ~$17.31. The gate denies that upgrade
while still letting a healthy [5,3,1] loop earn it.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import stage_runner_agent as sra  # noqa: E402


class CriticalIsConvergingTests(unittest.TestCase):
    """The pure predicate the upgrade-ladder gate keys on."""

    def test_flat_tail_is_thrash(self) -> None:
        # The incident: criticals never moved.
        self.assertFalse(sra._critical_is_converging([2, 2], 2))
        self.assertFalse(sra._critical_is_converging([2, 2, 2], 2))

    def test_rising_tail_is_thrash(self) -> None:
        self.assertFalse(sra._critical_is_converging([2, 3, 2], 2))
        self.assertFalse(sra._critical_is_converging([1, 3], 2))

    def test_strictly_decreasing_is_converging(self) -> None:
        self.assertTrue(sra._critical_is_converging([5, 3, 1], 2))
        self.assertTrue(sra._critical_is_converging([4, 2], 2))  # halved at the cap

    def test_decreasing_then_stall_is_thrash_within_window(self) -> None:
        # progress earlier, but the recent window stalled -> deny more rope
        self.assertFalse(sra._critical_is_converging([5, 3, 3], 2))

    def test_window_only_sees_last_k_plus_1(self) -> None:
        # k=1 -> window of 2 -> only the LAST transition matters
        self.assertTrue(sra._critical_is_converging([9, 9, 3, 1], 1))   # last pair 3>1
        self.assertFalse(sra._critical_is_converging([1, 3, 9, 9], 1))  # last pair 9,9

    def test_failopen_on_missing_or_short_trend(self) -> None:
        # No evidence of thrash yet -> never block the first upgrade.
        self.assertTrue(sra._critical_is_converging([], 2))
        self.assertTrue(sra._critical_is_converging([2], 2))
        self.assertTrue(sra._critical_is_converging(None, 2))


class TrendPersistenceTests(unittest.TestCase):
    """_decide_post_pipeline_stage records every reviewer pass's critical count
    into state.review_trend, deduped on the iteration number."""

    def _task(self, tmp: str, *, iteration: int, critical: int,
              verdict: str = "request_changes") -> Path:
        d = Path(tmp)
        (d / "state.json").write_text(json.dumps({
            "stage": "reviewer", "iteration": iteration,
        }) + "\n")
        (d / "06-review-agent.json").write_text(json.dumps({
            "verdict": {"verdict": verdict, "critical": critical,
                        "warning": 0, "suggestion": 0},
        }) + "\n")
        return d

    def test_records_critical_for_the_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = self._task(tmp, iteration=1, critical=2)
            sra._decide_post_pipeline_stage(d, 0.0, 20.0, {"iteration_cap": 2})
            st = json.loads((d / "state.json").read_text())
            self.assertEqual(st["review_trend"], [2])
            self.assertEqual(st["review_trend_iter"], 1)

    def test_same_iteration_does_not_double_append(self) -> None:
        # the upgrade ladder re-calls the fn within the SAME iteration; the
        # identical review must not be counted twice (would fake a decrease).
        with tempfile.TemporaryDirectory() as tmp:
            d = self._task(tmp, iteration=2, critical=2)
            sra._decide_post_pipeline_stage(d, 0.0, 20.0, {"iteration_cap": 2})
            # pre-seed a trend as if iteration 1 already recorded a 2
            st = json.loads((d / "state.json").read_text())
            st["review_trend"] = [2, 2]
            st["review_trend_iter"] = 2
            (d / "state.json").write_text(json.dumps(st) + "\n")
            sra._decide_post_pipeline_stage(d, 0.0, 20.0, {"iteration_cap": 2})
            st = json.loads((d / "state.json").read_text())
            self.assertEqual(st["review_trend"], [2, 2])  # unchanged

    def test_zero_critical_is_recorded_before_nitpick_guard(self) -> None:
        # a 0-critical request_changes still records the 0 (helps the math) and
        # returns awaiting-approval via the nitpick guard.
        with tempfile.TemporaryDirectory() as tmp:
            d = self._task(tmp, iteration=1, critical=0)
            stage, reason = sra._decide_post_pipeline_stage(
                d, 0.0, 20.0, {"iteration_cap": 2})
            self.assertEqual((stage, reason), ("awaiting-approval", "approve_no_critical"))
            st = json.loads((d / "state.json").read_text())
            self.assertEqual(st["review_trend"], [0])

    def test_incident_trend_then_gate_denies(self) -> None:
        # End-to-end of the predicate against a real persisted [2,2] trend.
        with tempfile.TemporaryDirectory() as tmp:
            d = self._task(tmp, iteration=1, critical=2)
            sra._decide_post_pipeline_stage(d, 0.0, 20.0, {"iteration_cap": 2})
            # iteration 2, still 2 criticals
            st = json.loads((d / "state.json").read_text())
            st["iteration"] = 2
            (d / "state.json").write_text(json.dumps(st) + "\n")
            sra._decide_post_pipeline_stage(d, 0.0, 20.0, {"iteration_cap": 2})
            trend = json.loads((d / "state.json").read_text())["review_trend"]
            self.assertEqual(trend, [2, 2])
            self.assertFalse(sra._critical_is_converging(trend, 2))  # gate -> deny upgrade


if __name__ == "__main__":
    unittest.main()
