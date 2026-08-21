"""Cost/iteration carry-forward across dispatcher re-ingest (ai-delivery-private#14).

Pins the fix for: moving a mid-flight task dir back to tasks/inbox/ — manual
requeue OR the limit-park auto-requeue sweep
(watcher.scan_limit_parked_for_resume) — used to reset cost_usd (and
iteration) to 0 in task_dispatcher._write_state_json, even though the
existing state.json carried the real accumulated spend. Since run_pipeline
seeds its cost-cap check from state.cost_usd ("Carry prior cost forward on
resume", stage_runner_agent.py), the reset let an operator bypass cost_cap by
repeatedly requeuing — observed: cost_usd=0.00 on resume vs. $11.32 already
spent, real cumulative cost $13.45 against a $20 cap that never tripped.

The watcher-respawn path (stage_runner_agent.py run_pipeline reads
state.cost_usd directly off disk) never goes through _write_state_json and
never had this bug; these tests exercise the ingest path specifically,
mirroring the existing carry-forward pattern for branch/pr_url/worktree (see
IngestManualSpecTests in test_spec_schema.py) and the triage sticky-verdict
mechanism (test_triage_sticky.py).

Run with: bot/venv/bin/python3 -m unittest tests.test_cost_carry
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema  # module-scope import, intentional — see ADR-003 (test_spec_schema.py)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import auto_loop  # noqa: E402
import task_dispatcher as td  # noqa: E402

SCHEMA_PATH = REPO_ROOT / "dispatcher" / "schema" / "spec.schema.json"


def _load_schema() -> dict:
    with SCHEMA_PATH.open() as f:
        return json.load(f)


class CostCarryIngestTests(unittest.TestCase):
    """Integration tests for task_dispatcher._ingest() / _write_state_json()
    re-ingest carry-forward of cost_usd / iteration / triage / base_branch.

    Monkey-patches task_dispatcher's directory globals to a temp tree (same
    pattern as IngestManualSpecTests in test_spec_schema.py) so the test
    never touches the live tasks/ queue and never spawns a real pipeline.
    """

    def setUp(self) -> None:
        self._schema = _load_schema()
        self._tmp = Path(tempfile.mkdtemp())

        self._inbox = self._tmp / "inbox"
        self._active = self._tmp / "active"
        self._failed = self._tmp / "failed"
        for d in (self._inbox, self._active, self._failed):
            d.mkdir()

        self._orig = {
            "INBOX_DIR": td.INBOX_DIR,
            "ACTIVE_DIR": td.ACTIVE_DIR,
            "FAILED_DIR": td.FAILED_DIR,
            "TASKS_DIR": td.TASKS_DIR,
            "_spawn_stage_runner": td._spawn_stage_runner,
        }
        td.INBOX_DIR = self._inbox
        td.ACTIVE_DIR = self._active
        td.FAILED_DIR = self._failed
        td.TASKS_DIR = self._tmp

        self._spawned: list[Path] = []
        td._spawn_stage_runner = lambda task_dir: self._spawned.append(task_dir)

    def tearDown(self) -> None:
        td.INBOX_DIR = self._orig["INBOX_DIR"]
        td.ACTIVE_DIR = self._orig["ACTIVE_DIR"]
        td.FAILED_DIR = self._orig["FAILED_DIR"]
        td.TASKS_DIR = self._orig["TASKS_DIR"]
        td._spawn_stage_runner = self._orig["_spawn_stage_runner"]
        shutil.rmtree(str(self._tmp), ignore_errors=True)

    def _make_spec_dir(
        self, folder_name: str, spec: dict, prior_state: dict | None = None,
    ) -> Path:
        """Create inbox/<folder_name>/spec.json (+ optional prior state.json,
        simulating a mid-flight task moved back to inbox/ for re-ingest)."""
        folder = self._inbox / folder_name
        folder.mkdir()
        spec_path = folder / "spec.json"
        spec_path.write_text(json.dumps(spec))
        if prior_state is not None:
            (folder / "state.json").write_text(json.dumps(prior_state))
            (folder / "worklog.md").write_text("# Worklog\n\n- prior run\n")
        return spec_path

    # ------------------------------------------------------------------ #
    # Re-ingest of a mid-flight task preserves spent budget + iteration   #
    # ------------------------------------------------------------------ #
    def test_reingest_preserves_cost_and_iteration(self) -> None:
        """#14: a task dir with prior state.json history (re-ingest — the
        manual-requeue / limit-park-requeue shape) must carry cost_usd and
        iteration forward, not reset them to 0."""
        task_id = "reingest-cost-test"
        spec = {
            "trigger": "manual", "user": "op", "prompt": "resume after requeue",
            "target_repo": "/tmp/test", "task_id": task_id, "cost_cap_usd": 20,
        }
        prior_state = {
            "id": task_id, "stage": "tester", "iteration": 2,
            "cost_usd": 11.32, "cost_cap_usd": 20,
            "triage": {"tier": "M", "caps": {"iteration_cap": 2}},
            "base_branch": "dev",
            "history": [{"at": "2026-08-13T00:00:00+00:00", "stage": "tester",
                         "note": "prior run"}],
        }
        spec_path = self._make_spec_dir(task_id, spec, prior_state)

        td._ingest(spec_path, self._schema)

        task_dir = self._active / task_id
        self.assertTrue(task_dir.exists())
        state = json.loads((task_dir / "state.json").read_text())

        self.assertEqual(state["cost_usd"], 11.32,
                          "cost_usd must survive re-ingest, not reset to 0")
        self.assertEqual(state["iteration"], 2,
                          "iteration must survive re-ingest, not reset to 0")
        self.assertEqual(state["triage"], prior_state["triage"])
        self.assertEqual(state["base_branch"], "dev")

        # A fresh 'ingested' history entry is still appended (re-ingest IS an
        # event worth recording) — carry-forward means the OLD spend/iteration
        # numbers survive, not that history/stage get frozen too.
        self.assertEqual(state["stage"], "received")
        self.assertEqual(len(state["history"]), 1)
        self.assertEqual(state["history"][0]["note"], "ingested by task_dispatcher")

    # ------------------------------------------------------------------ #
    # A genuinely fresh spec (no prior state.json) still starts at 0      #
    # ------------------------------------------------------------------ #
    def test_fresh_spec_starts_at_zero(self) -> None:
        """A brand-new task (no prior state.json in the inbox folder) must
        NOT pick up any stray cost/iteration — the carry-forward is gated on
        prior state.json actually existing."""
        task_id = "fresh-cost-test"
        spec = {
            "trigger": "manual", "user": "op", "prompt": "brand new task",
            "target_repo": "/tmp/test", "task_id": task_id,
        }
        spec_path = self._make_spec_dir(task_id, spec, prior_state=None)

        td._ingest(spec_path, self._schema)

        state = json.loads((self._active / task_id / "state.json").read_text())
        self.assertEqual(state["cost_usd"], 0.0)
        self.assertEqual(state["iteration"], 0)
        self.assertNotIn("triage", state)
        self.assertNotIn("base_branch", state)

    # ------------------------------------------------------------------ #
    # Loose *.json spec (no folder at all) also starts at 0 — same gate   #
    # ------------------------------------------------------------------ #
    def test_loose_json_spec_starts_at_zero(self) -> None:
        """The other _ingest() entry shape (a loose inbox/<x>.json, no
        pre-existing folder) trivially has no prior state.json either."""
        task_id = "loose-cost-test"
        spec = {
            "trigger": "manual", "user": "op", "prompt": "loose spec",
            "target_repo": "/tmp/test", "task_id": task_id,
        }
        spec_path = self._inbox / f"{task_id}.json"
        spec_path.write_text(json.dumps(spec))

        td._ingest(spec_path, self._schema)

        state = json.loads((self._active / task_id / "state.json").read_text())
        self.assertEqual(state["cost_usd"], 0.0)
        self.assertEqual(state["iteration"], 0)

    # ------------------------------------------------------------------ #
    # Zero is a legitimate preserved value, not "absent"                  #
    # ------------------------------------------------------------------ #
    def test_reingest_preserves_explicit_zero_cost(self) -> None:
        """prior cost_usd == 0.0 must still be treated as 'present' (the
        carry-forward loop checks `is not None`, not truthiness), so a task
        requeued before its first billable stage doesn't accidentally fall
        back to some other default."""
        task_id = "zero-cost-reingest-test"
        spec = {
            "trigger": "manual", "user": "op", "prompt": "requeued pre-spend",
            "target_repo": "/tmp/test", "task_id": task_id,
        }
        prior_state = {
            "id": task_id, "stage": "received", "iteration": 0, "cost_usd": 0.0,
            "history": [{"at": "2026-08-13T00:00:00+00:00", "stage": "received",
                         "note": "prior ingest"}],
        }
        spec_path = self._make_spec_dir(task_id, spec, prior_state)

        td._ingest(spec_path, self._schema)

        state = json.loads((self._active / task_id / "state.json").read_text())
        self.assertEqual(state["cost_usd"], 0.0)
        self.assertEqual(state["iteration"], 0)

    # ------------------------------------------------------------------ #
    # The preserved value is what actually trips the $-cap (not a reset   #
    # value) — proves the budget gate enforces TRUE cumulative spend.     #
    # ------------------------------------------------------------------ #
    def test_cap_triggers_on_true_cumulative_spend_after_reingest(self) -> None:
        """#14's concrete bypass scenario: a task requeued at $18.50 spent
        against a $20 cap, whose next stage adds $5. The budget-gate cap
        check (auto_loop.evaluate_stop — the same evaluator
        post_pipeline._decide_post_pipeline_stage calls) must see the TRUE
        cumulative ($23.50, over cap) once it starts from the value
        _write_state_json preserved, not from a reset-to-0 value (which
        would hide the breach: $0 + $5 = $5, under cap)."""
        task_id = "cap-trigger-test"
        cost_cap = 20.0
        spec = {
            "trigger": "manual", "user": "op", "prompt": "near the cap",
            "target_repo": "/tmp/test", "task_id": task_id,
            "cost_cap_usd": cost_cap,
        }
        prior_state = {
            "id": task_id, "stage": "tester", "iteration": 1,
            "cost_usd": 18.50, "cost_cap_usd": cost_cap,
            "history": [{"at": "2026-08-13T00:00:00+00:00", "stage": "tester",
                         "note": "prior run"}],
        }
        spec_path = self._make_spec_dir(task_id, spec, prior_state)

        td._ingest(spec_path, self._schema)

        state = json.loads((self._active / task_id / "state.json").read_text())
        preserved_cost = state["cost_usd"]
        self.assertEqual(preserved_cost, 18.50)

        # Next stage's cost, as run_pipeline would accumulate it
        # (cumulative_cost = state.get("cost_usd") + stage cost).
        next_stage_cost = 5.00
        cumulative_after_next_stage = preserved_cost + next_stage_cost

        verdict = auto_loop.ReviewerVerdict(
            verdict="request_changes", critical=1, warning=0, suggestion=0)
        loop_state = auto_loop.LoopState(iteration=1, iteration_cap=3)
        stop = auto_loop.evaluate_stop(
            verdict, loop_state, cumulative_after_next_stage, cost_cap)
        self.assertIs(stop, auto_loop.StopReason.COST_CAP,
                       "the $-cap must trip on the TRUE cumulative spend")

        # Regression control: reproduce the PRE-FIX behavior (cost reset to
        # 0 on re-ingest) and show it would have hidden the breach — this is
        # the exact bypass the issue reports.
        cumulative_if_reset_to_zero = 0.0 + next_stage_cost
        stop_pre_fix = auto_loop.evaluate_stop(
            verdict, loop_state, cumulative_if_reset_to_zero, cost_cap)
        self.assertIsNone(
            stop_pre_fix,
            "sanity check: the pre-fix reset-to-0 behavior would NOT have "
            "tripped the cap here — proving the bug was a real bypass")


if __name__ == "__main__":
    unittest.main()
