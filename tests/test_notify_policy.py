"""Bot-notification policy (issue #19).

Owner feedback 2026-08-14: a re-ingest / limit-park resume fired FOUR
Telegram messages for one event, and the "Stages:" line didn't match the
actual planned run. Covered here:

  * should_notify(event, verbose, is_resume) — the pure decision table
    (dispatcher/notify_policy.py) that every bot-notify call site in
    stage_runner_agent.py now goes through.
  * render_stage_list — the single join used for both the
    "[agent-pipeline] stages=..." log line and the "Stages:" Telegram line,
    so they can never drift apart (bullet 1).
  * _task_is_resuming — the stage_runner_agent helper that decides whether a
    run_pipeline() invocation is a fresh start or a resume, which is what lets
    "pipeline started" fire exactly once per task.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import notify_policy as np  # noqa: E402
import stage_runner_agent as sra  # noqa: E402


class ShouldNotifyTests(unittest.TestCase):
    """Pure decision table — no I/O, no env, no filesystem."""

    def test_verbose_only_event_needs_verbose_flag(self) -> None:
        for event in sorted(np.VERBOSE_ONLY):
            self.assertFalse(
                np.should_notify(event, verbose=False, is_resume=False),
                f"{event} should be suppressed without NOTIFY_VERBOSE",
            )
            self.assertTrue(
                np.should_notify(event, verbose=True, is_resume=False),
                f"{event} should fire under NOTIFY_VERBOSE=1",
            )

    def test_verbose_only_event_ignores_resume_state(self) -> None:
        # Resume/fresh must never override the operator's verbose opt-in —
        # only NOTIFY_VERBOSE decides for this bucket.
        for event in sorted(np.VERBOSE_ONLY):
            self.assertTrue(
                np.should_notify(event, verbose=True, is_resume=True))
            self.assertFalse(
                np.should_notify(event, verbose=False, is_resume=True))

    def test_once_per_task_event_fires_only_when_not_resuming(self) -> None:
        for event in sorted(np.ONCE_PER_TASK):
            self.assertTrue(
                np.should_notify(event, verbose=False, is_resume=False),
                f"{event} should fire on a fresh task",
            )
            self.assertFalse(
                np.should_notify(event, verbose=False, is_resume=True),
                f"{event} must be suppressed on resume",
            )
            # verbose=1 must not resurrect a once-per-task event on resume —
            # that's exactly the duplicate the issue reports.
            self.assertFalse(
                np.should_notify(event, verbose=True, is_resume=True))

    def test_always_event_fires_unconditionally(self) -> None:
        for event in sorted(np.ALWAYS):
            for verbose in (False, True):
                for is_resume in (False, True):
                    self.assertTrue(
                        np.should_notify(event, verbose=verbose, is_resume=is_resume),
                        f"{event} verbose={verbose} is_resume={is_resume}",
                    )

    def test_default_policy_categories_are_covered(self) -> None:
        """Issue #19's six default-notify categories are exactly the ALWAYS
        set — regression guard against silently dropping/adding one."""
        self.assertEqual(
            np.ALWAYS,
            frozenset({
                "clarify_needed", "limit_parked", "auto_resumed",
                "pr_ready", "terminal_failed",
            }),
        )

    def test_no_event_is_double_classified(self) -> None:
        self.assertEqual(np.ONCE_PER_TASK & np.ALWAYS, frozenset())
        self.assertEqual(np.ONCE_PER_TASK & np.VERBOSE_ONLY, frozenset())
        self.assertEqual(np.ALWAYS & np.VERBOSE_ONLY, frozenset())

    def test_unclassified_event_fails_open(self) -> None:
        # An event nobody put in a set yet must still notify — silently
        # swallowing an unclassified signal is worse than one extra message.
        self.assertTrue(np.should_notify("some_future_event"))
        self.assertTrue(
            np.should_notify("some_future_event", verbose=False, is_resume=True))

    def test_defaults_are_quiet(self) -> None:
        # Calling with no kwargs (verbose/is_resume default False) must not
        # notify for per-stage noise — the "spammy by default" regression.
        self.assertFalse(np.should_notify("stage_started"))
        self.assertFalse(np.should_notify("stage_done"))
        self.assertFalse(np.should_notify("triage_reused"))


class VerboseEnabledTests(unittest.TestCase):
    def test_unset_is_false(self) -> None:
        self.assertFalse(np.verbose_enabled({}))

    def test_one_is_true(self) -> None:
        self.assertTrue(np.verbose_enabled({"NOTIFY_VERBOSE": "1"}))

    def test_other_values_are_false(self) -> None:
        for v in ("0", "true", "yes", " ", ""):
            self.assertFalse(np.verbose_enabled({"NOTIFY_VERBOSE": v}), v)


class RenderStageListTests(unittest.TestCase):
    """Bullet 1: the Telegram "Stages:" line must render the ACTUAL planned
    stage list — same join as the "[agent-pipeline] stages=..." log line."""

    def test_joins_with_arrow(self) -> None:
        self.assertEqual(
            np.render_stage_list(["ba", "developer", "tester"]),
            "ba → developer → tester",
        )

    def test_full_menu_with_optional_stages(self) -> None:
        # The exact sequence from the owner's bug report — discovery and
        # pattern-detector must show up when they're actually part of the plan.
        plan = ["discovery", "ba", "pattern-detector", "architect",
                "developer", "tester", "security", "reviewer"]
        self.assertEqual(
            np.render_stage_list(plan),
            "discovery → ba → pattern-detector → architect → developer → "
            "tester → security → reviewer",
        )

    def test_triage_narrowed_menu(self) -> None:
        # A tier-M narrowed list renders exactly as composed — no separate
        # constant to drift out of sync with it.
        self.assertEqual(
            np.render_stage_list(["ba", "developer", "tester", "security", "reviewer"]),
            "ba → developer → tester → security → reviewer",
        )

    def test_single_stage(self) -> None:
        self.assertEqual(np.render_stage_list(["ba"]), "ba")

    def test_empty_list(self) -> None:
        self.assertEqual(np.render_stage_list([]), "")


class TaskIsResumingTests(unittest.TestCase):
    """_task_is_resuming(task_dir) — the is_resume input to should_notify,
    computed from disk state alone (no state.json 'stage' field, which the
    dispatcher resets to 'received' on every re-ingest)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.task_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_brand_new_task_is_not_resuming(self) -> None:
        (self.task_dir / "spec.json").write_text("{}")
        self.assertFalse(sra._task_is_resuming(self.task_dir))

    def test_persisted_triage_verdict_means_resuming(self) -> None:
        # Sticky triage (triage.json) survives a re-ingest even when no stage
        # artifact exists yet — e.g. an S-tier task that skips BA. Needs both
        # tier and caps: _load_persisted_triage treats a partial file as
        # malformed (-> None, classify fresh) — see triage_wiring.py.
        (self.task_dir / "triage.json").write_text(json.dumps({
            "tier": "S", "confidence": 0.9,
            "caps": {"token_cap": 300_000, "iteration_cap": 1},
        }))
        self.assertTrue(sra._task_is_resuming(self.task_dir))

    def test_completed_stage_artifact_means_resuming(self) -> None:
        (self.task_dir / "01-ba-agent.md").write_text("x" * 100)
        self.assertTrue(sra._task_is_resuming(self.task_dir))

    def test_trivial_artifact_does_not_count(self) -> None:
        # Mirrors _stage_artifact_ready's own >50-byte floor (a 0-byte stub
        # left by a crashed stage must not look like completed work).
        (self.task_dir / "01-ba-agent.md").write_text("x")
        self.assertFalse(sra._task_is_resuming(self.task_dir))


if __name__ == "__main__":
    unittest.main()
