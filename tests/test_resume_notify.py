"""Resume notification names WHY + WHERE (2026-08-18).

Regression for a real operator report: two different resumes of the SAME task
— a limit-park resume and a clarify round-trip on 2026-08-17 — produced
byte-identical "resumed — N/M stage(s) already done, continuing" Telegram
messages, so the operator read them as duplicate sends. The auto_resumed
message now names the resume reason (clarify answers / limit park / watcher
respawn) and the stage it continues from.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import stage_runner_agent as sra  # noqa: E402

# The full 8-stage menu — lets "continuing from ba (1/8 done)" mirror the BRD
# example exactly (discovery done, everything else pending).
STAGES = ["discovery", "ba", "pattern-detector", "architect",
          "developer", "tester", "security", "reviewer"]


class ResumeReasonTests(unittest.TestCase):
    """_resume_reason(task_dir, state) — the WHY, inferred from task state the
    runner already has. Ordered so the most specific signal wins."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.task_dir = Path(self._tmp.name)

    def test_clarify_round_trip_when_answers_present(self) -> None:
        (self.task_dir / "clarifications.md").write_text("Q1\nA: yes\n")
        self.assertEqual(sra._resume_reason(self.task_dir, {}), "clarify answers")

    def test_limit_park_when_limit_stall_marker_present(self) -> None:
        state = {"limit_stall": {"stage": "developer", "reason": "api-error"}}
        self.assertEqual(sra._resume_reason(self.task_dir, state), "limit park")

    def test_limit_park_when_limit_parks_counter_present(self) -> None:
        self.assertEqual(sra._resume_reason(self.task_dir, {"limit_parks": 2}),
                         "limit park")

    def test_watcher_respawn_is_the_fallback(self) -> None:
        self.assertEqual(sra._resume_reason(self.task_dir, {}), "watcher respawn")

    def test_missing_state_dict_defaults_to_fallback(self) -> None:
        self.assertEqual(sra._resume_reason(self.task_dir, None), "watcher respawn")

    def test_clarify_wins_over_limit_marker(self) -> None:
        # A task can carry both (parked mid-clarify) — the clarify round-trip is
        # the more specific signal and must win.
        (self.task_dir / "clarifications.md").write_text("Q1\nA: yes\n")
        state = {"limit_stall": {"stage": "ba"}}
        self.assertEqual(sra._resume_reason(self.task_dir, state), "clarify answers")


class ResumedNotificationTests(unittest.TestCase):
    """_resumed_notification(task_id, stages, task_dir, state) — the full
    auto_resumed body, built from the same done/continuing computation the
    runner's skip loop uses (pinned via the _stage_artifact_ready seam)."""

    def _render(self, ready_stages, state=None, ba_rerun=False):
        # Mirror _ba_needs_clarify_rerun's real contract: it only ever returns
        # True for BA (every other stage is False), so discovery stays done.
        with mock.patch.object(
                sra, "_stage_artifact_ready",
                side_effect=lambda _d, s: s in ready_stages), \
             mock.patch.object(
                sra, "_ba_needs_clarify_rerun",
                side_effect=lambda _d, s: ba_rerun and s == "ba"):
            return sra._resumed_notification(
                "tg-42", STAGES, Path("/tmp/nonexistent"), state)

    def test_names_reason_and_continuing_stage(self) -> None:
        # discovery done → resumes from ba: matches the BRD's canonical example.
        msg = self._render({"discovery"}, state={})
        self.assertEqual(
            msg,
            "[tg-42] resumed after watcher respawn — continuing from ba (1/8 done)",
        )

    def test_clarify_reason(self) -> None:
        # clarify is signalled by the state the runner reads, not the stage list.
        with mock.patch.object(
                sra, "_resume_reason", return_value="clarify answers"):
            msg = self._render({"discovery"}, state={})
        self.assertIn("resumed after clarify answers", msg)
        self.assertIn("continuing from ba (1/8 done)", msg)

    def test_limit_park_reason(self) -> None:
        msg = self._render({"discovery", "ba", "architect"},
                           state={"limit_stall": {"stage": "developer"}})
        self.assertIn("resumed after limit park", msg)
        self.assertIn("continuing from pattern-detector (3/8 done)", msg)

    def test_continue_from_mid_pipeline_stage(self) -> None:
        # developer is the next pending stage after a crash there.
        msg = self._render({"discovery", "ba", "pattern-detector", "architect"},
                           state={})
        self.assertIn("continuing from developer (4/8 done)", msg)

    def test_all_stages_done_omits_continuing(self) -> None:
        # Nothing left to continue from — drop the clause rather than print a
        # nonsense "continuing from" with no stage name.
        msg = self._render(set(STAGES), state={})
        self.assertEqual(
            msg,
            "[tg-42] resumed after watcher respawn — 8/8 stage(s) already done",
        )
        self.assertNotIn("continuing from", msg)

    def test_ba_clarify_rerun_keeps_ba_as_continuing(self) -> None:
        # BA has an artifact but still needs a clarify re-run → NOT done, so the
        # message still points at ba (the operator's answers are about to land).
        msg = self._render({"discovery", "ba"}, state={}, ba_rerun=True)
        self.assertIn("continuing from ba (1/8 done)", msg)


if __name__ == "__main__":
    unittest.main()
