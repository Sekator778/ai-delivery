"""Tier-aware backend + stage-timeout scaling (2026-06-03).

The first live L run (tg-20260603-061631-3967) timed out its developer stage: it
ran on the cheap DeepSeek default and hit the 900s wall mid-Maven-loop, then
handed off cleanly. Two fixes pinned here:
  1. L gets a longer per-stage wall-clock (_stage_timeout_for).
  2. L runs the build/verify stages on anthropic from iteration 0 instead of
     waiting for the iteration-2 escalation (_resolve_stage_backend tier arg).
Both honor explicit per-task routing and the iteration escalation that existed
before.

Updated 2026-08-15: the 2026-06-07 two-model decision made anthropic the default
for EVERY stage, so the tier-L force and the iteration escalation are no-ops
until an operator opts a stage back onto a cheap provider. These tests therefore
drive the tier logic through a stage that is actually cheap — the DEEPSEEK_STAGES
override — instead of asserting a default that no longer exists.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import backend_routing as br  # noqa: E402
import stage_runner_agent as sra  # noqa: E402


class _CheapDefaultsMixin:
    """Route the build/verify stages to deepseek for the duration of a test.

    BACKEND is one dict shared by backend_routing and stage_runner_agent, so
    patching it in place is what a real DEEPSEEK_STAGES boot does — without
    needing the module reimported under a mutated environment.
    """

    CHEAP_STAGES = ("developer", "developer-hotfix", "tester", "security")

    def setUp(self) -> None:
        super().setUp()
        self._saved = dict(sra.BACKEND)
        for stage in self.CHEAP_STAGES:
            sra.BACKEND[stage] = "deepseek"
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        sra.BACKEND.clear()
        sra.BACKEND.update(self._saved)


class StageTimeoutScalingTests(unittest.TestCase):
    def test_l_gets_longer_window(self) -> None:
        self.assertEqual(sra._stage_timeout_for("L"), sra.STAGE_TIMEOUT_SEC_L)
        self.assertEqual(sra._stage_timeout_for("l"), sra.STAGE_TIMEOUT_SEC_L)

    def test_other_tiers_keep_default(self) -> None:
        for tier in ("M", "S", "s", None, "", "X"):
            self.assertEqual(sra._stage_timeout_for(tier), sra.STAGE_TIMEOUT_SEC)

    def test_l_window_strictly_larger(self) -> None:
        self.assertGreater(sra.STAGE_TIMEOUT_SEC_L, sra.STAGE_TIMEOUT_SEC)


class TierBackendRoutingTests(_CheapDefaultsMixin, unittest.TestCase):
    def test_l_forces_anthropic_for_build_stages(self) -> None:
        for stage in self.CHEAP_STAGES:
            self.assertEqual(
                sra._resolve_stage_backend(stage, 0, {}, "L"),
                ("anthropic", "deepseek"),
                stage,
            )

    def test_non_l_keeps_cheap_default(self) -> None:
        for tier in ("M", "S", None):
            self.assertEqual(
                sra._resolve_stage_backend("developer", 0, {}, tier),
                ("deepseek", None),
            )

    def test_l_already_anthropic_stage_unchanged(self) -> None:
        # reviewer/architect are already anthropic and not in the force-set →
        # no spurious escalated_from annotation.
        self.assertEqual(sra._resolve_stage_backend("reviewer", 0, {}, "L"), ("anthropic", None))
        self.assertEqual(sra._resolve_stage_backend("architect", 0, {}, "L"), ("anthropic", None))

    def test_explicit_routing_honored_over_l_force(self) -> None:
        # An explicit per-task override wins; L-force only upgrades the DEFAULT.
        self.assertEqual(
            sra._resolve_stage_backend("developer", 0, {"developer": "glm"}, "L"),
            ("glm", None),
        )

    def test_iteration_escalation_preserved(self) -> None:
        # M developer still escalates to anthropic at iteration >= 2.
        self.assertEqual(
            sra._resolve_stage_backend("developer", 2, {}, "M"),
            ("anthropic", "deepseek"),
        )

    def test_backward_compatible_when_tier_omitted(self) -> None:
        # The old 3-arg call (no tier) behaves exactly as before.
        self.assertEqual(sra._resolve_stage_backend("developer", 0, {}), ("deepseek", None))
        self.assertEqual(sra._resolve_stage_backend("developer", 2, {}), ("anthropic", "deepseek"))


class DeepseekStageDefaultsTests(unittest.TestCase):
    """DEEPSEEK_STAGES — opt the mechanical stages back onto the cheap provider
    (2026-08-15) without touching the thinking stages or a spec file."""

    # An explicit all-anthropic baseline, NOT a copy of the live br.BACKEND:
    # when the operator's own DEEPSEEK_STAGES is exported, the live dict already
    # carries the override and every assertion below would test nothing.
    BASELINE = {stage: "anthropic" for stage in (
        "discovery", "ba", "pattern-detector", "tasks", "analyze", "edge-cases",
        "architect", "developer", "developer-hotfix", "tester", "security",
        "reviewer",
    )}

    def _apply(self, stages, key="sk-test"):
        backend = dict(self.BASELINE)
        import os
        saved = os.environ.get("DEEPSEEK_API_KEY")
        if key is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = key
        try:
            return br._apply_deepseek_stage_defaults(backend, frozenset(stages))
        finally:
            if saved is None:
                os.environ.pop("DEEPSEEK_API_KEY", None)
            else:
                os.environ["DEEPSEEK_API_KEY"] = saved

    def test_named_stage_moves_to_deepseek(self) -> None:
        backend = self._apply({"tester"})
        self.assertEqual(backend["tester"], "deepseek")

    def test_unnamed_stages_stay_anthropic(self) -> None:
        backend = self._apply({"tester"})
        for stage in ("ba", "architect", "developer", "reviewer", "security"):
            self.assertEqual(backend[stage], "anthropic", stage)

    def test_empty_is_a_no_op(self) -> None:
        self.assertEqual(self._apply(set()), self.BASELINE)

    def test_baseline_matches_the_shipped_stage_set(self) -> None:
        # Guards the hand-written BASELINE above against a stage added to
        # BACKEND later: a missing key would silently narrow these tests.
        self.assertEqual(set(self.BASELINE), set(br.BACKEND))

    def test_missing_key_keeps_anthropic(self) -> None:
        # Routing to a keyless backend only buys a silent late fallback in
        # _subagent_env — refuse the override instead.
        backend = self._apply({"tester"}, key=None)
        self.assertEqual(backend["tester"], "anthropic")

    def test_blank_key_keeps_anthropic(self) -> None:
        backend = self._apply({"tester"}, key="   ")
        self.assertEqual(backend["tester"], "anthropic")

    def test_unknown_stage_is_ignored_not_fatal(self) -> None:
        backend = self._apply({"tester", "not-a-stage"})
        self.assertEqual(backend["tester"], "deepseek")
        self.assertNotIn("not-a-stage", backend)

    def test_override_survives_into_resolution(self) -> None:
        # The dict _apply_deepseek_stage_defaults mutates is the one
        # _resolve_stage_backend reads, so an M-tier tester really runs cheap.
        saved = dict(sra.BACKEND)
        self.addCleanup(lambda: (sra.BACKEND.clear(), sra.BACKEND.update(saved)))
        sra.BACKEND["tester"] = "deepseek"
        self.assertEqual(sra._resolve_stage_backend("tester", 0, {}, "M"),
                         ("deepseek", None))
        # …and the two safety nets still fire on top of it.
        self.assertEqual(sra._resolve_stage_backend("tester", 0, {}, "L"),
                         ("anthropic", "deepseek"))
        self.assertEqual(sra._resolve_stage_backend("tester", 2, {}, "M"),
                         ("anthropic", "deepseek"))
        self.assertEqual(
            sra._resolve_stage_backend("tester", 0, {"tester": "anthropic"}, "M"),
            ("anthropic", None))


if __name__ == "__main__":
    unittest.main()
