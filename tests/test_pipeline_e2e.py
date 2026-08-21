"""End-to-end pipeline test — drive the REAL run_pipeline() over a throwaway task
with only stage *execution* stubbed (no claude / git / network).

The unit suite pins individual gates (convergence, handoff, triage, recovery);
this checks that they COMPOSE inside the orchestrator's control flow:

  * stage sequencing over the composed menu, including the tester‖security
    parallel hop,
  * the post-pipeline decision integration (the real _decide_post_pipeline_stage
    reads the stubbed reviewer verdict + the live state.json),
  * the happy-path approve route to awaiting-approval, and
  * the request_changes → hotfix-loop → approve cycle with its iteration bump.

Seams stubbed: _run_pipeline_stage_with_breadcrumbs / _run_two_stages_parallel
(stage exec) and the telegram/notify/model-routing side effects. Everything else
— locking, triage gate, stage composition, the decision + hotfix loop, the
terminal state write — is the production code path.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import stage_runner_agent as sra  # noqa: E402


class PipelineE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        # Deterministic stage menu: no optional stages, triage off → the full
        # base pipeline, so the executed order is reproducible.
        self._env = {
            k: os.environ.get(k)
            for k in (
                "PATTERN_DETECTION_ENABLED", "TASKS_STAGE_ENABLED",
                "ANALYZE_STAGE_ENABLED", "EDGE_CASES_STAGE_ENABLED",
                "DISCOVERY_ENABLED", "TRIAGE_MODE", "TRIAGE_DISABLED",
            )
        }
        os.environ["PATTERN_DETECTION_ENABLED"] = "0"
        os.environ["TRIAGE_DISABLED"] = "1"
        for k in ("TASKS_STAGE_ENABLED", "ANALYZE_STAGE_ENABLED",
                  "EDGE_CASES_STAGE_ENABLED", "DISCOVERY_ENABLED", "TRIAGE_MODE"):
            os.environ.pop(k, None)

        self._saved = {
            n: getattr(sra, n) for n in (
                "_run_pipeline_stage_with_breadcrumbs", "_run_two_stages_parallel",
                "_send_telegram", "_notify_bot",
                "_resolve_anthropic_model_for_target", "_apply_tier_model_routing",
                "_RUNNER_LOCK_FH",
            )
        }
        sra._send_telegram = lambda *a, **k: None
        sra._notify_bot = lambda *a, **k: None
        sra._resolve_anthropic_model_for_target = lambda *a, **k: None
        sra._apply_tier_model_routing = lambda *a, **k: None

    def tearDown(self) -> None:
        for n, v in self._saved.items():
            setattr(sra, n, v)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ── helpers ──────────────────────────────────────────────────────────────
    def _make_task(self, tmp: str, *, iteration_cap: int = 3) -> tuple[Path, Path]:
        root = Path(tmp)
        target = root / "repo"
        target.mkdir()
        task = root / "task"
        task.mkdir()
        (task / "spec.json").write_text(json.dumps({
            "target_repo": str(target),
            "prompt": "add a thing",
            "cost_cap_usd": 20,
            "iteration_cap": iteration_cap,
        }))
        (task / "state.json").write_text("{}")
        return task, target

    def _install_stage_stubs(self, executed: list, verdict_for) -> None:
        """Stub stage execution. `verdict_for(iteration)` returns the reviewer
        verdict dict to persist into 06-review-agent.json on the reviewer pass."""
        def breadcrumbs(task_dir, target_repo, stage, state, task_id,
                        cost, cost_cap, state_path):
            executed.append(stage)
            if stage == "reviewer":
                it = int(json.loads(
                    (task_dir / "state.json").read_text()).get("iteration") or 1)
                (task_dir / "06-review-agent.json").write_text(
                    json.dumps({"verdict": verdict_for(it)}))
            return 0, cost + 0.5, state

        def parallel(task_dir, target_repo, s1, s2, state, task_id,
                     cost, cost_cap, state_path):
            executed.append(s1)
            executed.append(s2)
            return 0, cost + 1.0, state, 0, None, None

        sra._run_pipeline_stage_with_breadcrumbs = breadcrumbs
        sra._run_two_stages_parallel = parallel

    # ── tests ────────────────────────────────────────────────────────────────
    def test_happy_path_terminal_state_and_order(self) -> None:
        executed: list = []
        self._install_stage_stubs(
            executed, lambda it: {"verdict": "approve", "critical": 0})
        with tempfile.TemporaryDirectory() as tmp:
            task, _ = self._make_task(tmp)
            rc = sra.run_pipeline(task)
            st = json.loads((task / "state.json").read_text())
        self.assertEqual(rc, 0)
        self.assertEqual(st["stage"], "awaiting-approval")
        self.assertEqual(executed, sra._pipeline_stages_for_agent({}))
        # reviewer ran exactly once on the happy path (no hotfix loop)
        self.assertEqual(executed.count("reviewer"), 1)

    def test_request_changes_then_approve_runs_one_hotfix_iteration(self) -> None:
        executed: list = []

        def verdict_for(it):
            # iteration 1 blocks with a real critical; the hotfix pass approves.
            return ({"verdict": "request_changes", "critical": 1} if it == 1
                    else {"verdict": "approve", "critical": 0})

        self._install_stage_stubs(executed, verdict_for)
        with tempfile.TemporaryDirectory() as tmp:
            task, _ = self._make_task(tmp, iteration_cap=3)
            rc = sra.run_pipeline(task)
            st = json.loads((task / "state.json").read_text())
        self.assertEqual(rc, 0)
        self.assertEqual(st["stage"], "awaiting-approval")
        self.assertEqual(st["iteration"], 2)            # one hotfix iteration
        self.assertIn("developer-hotfix", executed)     # hotfix loop entered
        self.assertEqual(executed.count("reviewer"), 2)  # initial + one hotfix

    def test_nitpick_request_changes_zero_critical_does_not_loop(self) -> None:
        # The $15.98 guard, end-to-end: request_changes with 0 critical must NOT
        # open the hotfix loop — it routes straight to awaiting-approval.
        executed: list = []
        self._install_stage_stubs(
            executed, lambda it: {"verdict": "request_changes",
                                  "critical": 0, "warning": 3})
        with tempfile.TemporaryDirectory() as tmp:
            task, _ = self._make_task(tmp)
            rc = sra.run_pipeline(task)
            st = json.loads((task / "state.json").read_text())
        self.assertEqual(rc, 0)
        self.assertEqual(st["stage"], "awaiting-approval")
        self.assertEqual(st["iteration"], 1)            # NO hotfix iteration
        self.assertNotIn("developer-hotfix", executed)

    def test_missing_spec_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task"
            task.mkdir()
            self.assertEqual(sra.run_pipeline(task), 2)


if __name__ == "__main__":
    unittest.main()
