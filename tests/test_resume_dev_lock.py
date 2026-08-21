"""Resume restores the Developer branch/PR lock (2026-08-15).

Regression for a real failure: task `stt-local-path-source` was limit-parked,
auto-resumed, and the resume path SKIPPED the completed Developer stage — which
is also the code that persists ``state.branch`` / ``state.pr_url``. The reviewer
then found one critical, the hotfix fixed it on the correct branch and PR, and
the runner failed the whole task with rc=5 anyway, because the fail-closed
hotfix gate cannot verify a lock that is not there. $14.56 spent, work green,
task moved to tasks/failed/.

Same class as #14 (cost_usd/iteration/triage/base_branch lost across re-ingest);
those four fields were covered, these two were not.

The gate itself is correct and stays fail-closed — these tests pin that the lock
is restored from the Developer ARTIFACT (what the stage did), never from git
HEAD (where the worktree happens to be now), which is what makes the drift check
meaningful.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import stage_runner_agent as sra  # noqa: E402
from stage_prompts import STAGE_ARTIFACT_MAP  # noqa: E402

BRANCH = "feat/stt-local-path-source"
PR_URL = "https://github.com/owner/repo/pull/17"

# The shape the Developer stage actually writes (verified against the real
# 03-dev-agent.md of the failed task).
DEV_ARTIFACT = f"""# Dev Agent Summary — demo

**Branch:** `{BRANCH}`
**PR:** {PR_URL} (base: `dev`)

Files changed: bot/bot.py, bot/stt_utils.py

DEV_COMPLETE: local path source wired
BRANCH: {BRANCH}
PR_URL: {PR_URL}
TESTS: 142/142
"""


class RestoreDevLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.task_dir = Path(self._tmp.name)

    def _write(self, state: dict, artifact: "str | None" = DEV_ARTIFACT) -> None:
        (self.task_dir / "state.json").write_text(json.dumps(state, indent=2) + "\n")
        if artifact is not None:
            (self.task_dir / STAGE_ARTIFACT_MAP["developer"]).write_text(artifact)

    def _state(self) -> dict:
        return json.loads((self.task_dir / "state.json").read_text())

    def test_restores_both_fields_from_artifact(self) -> None:
        # Exactly the failed task's state: base_branch survived re-ingest (#14),
        # branch/pr_url did not.
        self._write({"id": "demo", "stage": "reviewer", "base_branch": "dev"})
        self.assertTrue(sra._restore_dev_lock_from_artifact(self.task_dir))
        state = self._state()
        self.assertEqual(state["branch"], BRANCH)
        self.assertEqual(state["pr_url"], PR_URL)

    def test_preserves_unrelated_state_fields(self) -> None:
        self._write({"id": "demo", "cost_usd": 14.56, "iteration": 2,
                     "base_branch": "dev", "triage": {"tier": "M"}})
        sra._restore_dev_lock_from_artifact(self.task_dir)
        state = self._state()
        self.assertEqual(state["cost_usd"], 14.56)
        self.assertEqual(state["iteration"], 2)
        self.assertEqual(state["triage"], {"tier": "M"})

    def test_intact_lock_is_not_touched(self) -> None:
        # A normal (non-resumed) run already has the lock — do not overwrite it
        # from an artifact that could be stale.
        self._write({"branch": "feat/original", "pr_url": PR_URL})
        self.assertFalse(sra._restore_dev_lock_from_artifact(self.task_dir))
        self.assertEqual(self._state()["branch"], "feat/original")

    def test_half_set_lock_stays_unset_when_artifact_is_silent(self) -> None:
        # Fail-closed: an artifact naming neither leaves the gate blocking
        # rather than half-restoring a lock the gate would then trust.
        self._write({"id": "demo"}, artifact="# Dev Agent Summary\n\nno markers\n")
        self.assertFalse(sra._restore_dev_lock_from_artifact(self.task_dir))
        state = self._state()
        self.assertNotIn("branch", state)
        self.assertNotIn("pr_url", state)

    def test_missing_artifact_is_a_no_op(self) -> None:
        self._write({"id": "demo"}, artifact=None)
        self.assertFalse(sra._restore_dev_lock_from_artifact(self.task_dir))

    def test_missing_state_file_is_a_no_op(self) -> None:
        (self.task_dir / STAGE_ARTIFACT_MAP["developer"]).write_text(DEV_ARTIFACT)
        self.assertFalse(sra._restore_dev_lock_from_artifact(self.task_dir))

    def test_unreadable_state_json_does_not_raise(self) -> None:
        (self.task_dir / "state.json").write_text("{ not json")
        (self.task_dir / STAGE_ARTIFACT_MAP["developer"]).write_text(DEV_ARTIFACT)
        self.assertFalse(sra._restore_dev_lock_from_artifact(self.task_dir))


class HotfixGateAfterRestoreTests(unittest.TestCase):
    """The point of the restore: the fail-closed gate now has something to
    check, and still refuses a hotfix that actually drifted."""

    def test_gate_inputs_present_after_restore(self) -> None:
        with TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            (task_dir / "state.json").write_text(
                json.dumps({"id": "demo", "base_branch": "dev"}) + "\n")
            (task_dir / STAGE_ARTIFACT_MAP["developer"]).write_text(DEV_ARTIFACT)
            sra._restore_dev_lock_from_artifact(task_dir)
            state = json.loads((task_dir / "state.json").read_text())

            # Mirrors the gate's own reads in _execute_single_stage_inner.
            expected_branch = state.get("branch") or ""
            expected_pr = state.get("pr_url") or ""
            self.assertTrue(expected_branch and expected_pr,
                            "gate would still fail-closed after restore")
            # Hotfix that stayed put → accepted.
            self.assertEqual(BRANCH, expected_branch)
            self.assertEqual(PR_URL, expected_pr)
            # Hotfix that drifted → still caught.
            self.assertNotEqual("main", expected_branch)


if __name__ == "__main__":
    unittest.main()
