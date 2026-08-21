"""PR base-branch enforcement (issue #10).

task pin-frontend-node resolved state.base_branch correctly
(`feat/local-longpolling`, from the registry) and the developer's branch WAS
cut from it, but the PR the developer subagent opened
(PR #4 on the target repo) targeted `dev-fix` — the
repo default — instead. `_try_open_draft_pr` (dispatcher/git_pr.py) already
passed `--base` on its own draft-PR fallback path, and the rendered developer
prompt already told the subagent to pass `--base {base_branch}` too — neither
is what opened PR #4 (its title has no `[handoff]` prefix, so it was the
subagent's own `gh pr create`, which silently dropped the flag despite the
instruction). Two changes close the gap:

  1. The rendered prompt now states the `--base` requirement as a hard,
     non-negotiable RULE (not just a workflow step), in both the
     orchestrator-level and the nested subagent-level instructions.
  2. A post-create backstop (_verify_and_repair_pr_base) checks the opened
     PR's actual baseRefName against the resolved base and self-repairs a
     mismatch with one `gh pr edit --base` retry — the code-level guarantee
     that no longer depends on the subagent following the prompt correctly.

This file tests both: the rendered prompt kwargs/text, and the verify/repair
decision logic in isolation (gh is stubbed — never shells out for real).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import git_pr as gp  # noqa: E402
import stage_runner_agent as sra  # noqa: E402


class _FakeCompletedProcess:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class DeveloperPromptBaseRuleTests(unittest.TestCase):
    """The rendered developer prompt must state --base as a hard rule, not
    just bury it in a numbered workflow step an LLM can paraphrase away."""

    def _render(self, base_branch: str = "feat/local-longpolling") -> str:
        task_dir = Path(tempfile.mkdtemp()) / "TASK-PRBASE"
        task_dir.mkdir()
        state = {"base_branch": base_branch}
        kw = sra._build_format_kwargs("developer", task_dir,
                                      Path("/repos/example-app"), state)
        return sra.STAGE_PROMPTS["developer"].format(**kw)

    def test_base_branch_flows_into_kwargs(self) -> None:
        task_dir = Path(tempfile.mkdtemp()) / "TASK-PRBASE"
        task_dir.mkdir()
        kw = sra._build_format_kwargs(
            "developer", task_dir, Path("/repos/example-app"),
            {"base_branch": "feat/local-longpolling"})
        self.assertEqual(kw["base_branch"], "feat/local-longpolling")

    def test_top_level_rules_state_base_as_non_negotiable(self) -> None:
        rendered = self._render()
        self.assertIn("BRANCH & PR RULES", rendered)
        self.assertIn(
            "Every `gh pr create` call MUST pass `--base feat/local-longpolling`",
            rendered,
        )

    def test_nested_subagent_prompt_also_states_the_rule(self) -> None:
        rendered = self._render()
        self.assertIn(
            "`gh pr create` MUST include `--base feat/local-longpolling` explicitly",
            rendered,
        )

    def test_workflow_step_five_still_has_the_flag(self) -> None:
        rendered = self._render()
        self.assertIn(
            "gh pr create --title '<title>' --body '<body>' "
            "--base feat/local-longpolling",
            rendered,
        )

    def test_resolved_base_used_verbatim_not_a_placeholder(self) -> None:
        # Regression guard: the resolved base must be the literal registry
        # value baked into the text the orchestrator relays to the subagent —
        # never a template placeholder that could be misread or dropped.
        rendered = self._render()
        self.assertNotIn("--base {base_branch}", rendered)
        self.assertNotIn("<base_branch>", rendered)


class PrBaseRefTests(unittest.TestCase):
    """`_pr_base_ref` — thin `gh pr view --json baseRefName` wrapper."""

    def setUp(self) -> None:
        self._orig_run = gp.subprocess.run

    def tearDown(self) -> None:
        gp.subprocess.run = self._orig_run

    def test_success_returns_base(self) -> None:
        gp.subprocess.run = lambda *a, **k: _FakeCompletedProcess(0, "dev-fix\n")
        self.assertEqual(
            gp._pr_base_ref(Path("/repo"), "https://github.com/o/r/pull/4"),
            "dev-fix",
        )

    def test_nonzero_rc_returns_none(self) -> None:
        gp.subprocess.run = lambda *a, **k: _FakeCompletedProcess(1, "", "not found")
        self.assertIsNone(
            gp._pr_base_ref(Path("/repo"), "https://github.com/o/r/pull/4"))

    def test_gh_missing_returns_none_not_raise(self) -> None:
        def _boom(*a, **k):
            raise FileNotFoundError("gh: command not found")
        gp.subprocess.run = _boom
        self.assertIsNone(
            gp._pr_base_ref(Path("/repo"), "https://github.com/o/r/pull/4"))


class VerifyAndRepairPrBaseTests(unittest.TestCase):
    """`_verify_and_repair_pr_base` — the post-create backstop. Pure decision
    logic over stubbed `gh` calls (never shells out for real)."""

    def setUp(self) -> None:
        self._orig_run = gp.subprocess.run

    def tearDown(self) -> None:
        gp.subprocess.run = self._orig_run

    def _stub(self, view_outputs, edit_rc: int = 0, edit_stderr: str = ""):
        """view_outputs: list of stdout strings returned by successive
        `gh pr view` calls (one popped per call); `gh pr edit` always uses
        edit_rc/edit_stderr."""
        outputs = list(view_outputs)

        def _run(cmd, **kwargs):
            if cmd[:3] == ["gh", "pr", "view"]:
                out = outputs.pop(0)
                return _FakeCompletedProcess(0, out)
            if cmd[:3] == ["gh", "pr", "edit"]:
                return _FakeCompletedProcess(edit_rc, "", edit_stderr)
            raise AssertionError(f"unexpected gh call: {cmd}")
        gp.subprocess.run = _run

    def test_matching_base_is_ok_no_repair_attempted(self) -> None:
        self._stub(["feat/local-longpolling\n"])
        ok, note = gp._verify_and_repair_pr_base(
            Path("/repo"), "https://github.com/o/r/pull/4", "feat/local-longpolling")
        self.assertTrue(ok)
        self.assertIn("matches", note)

    def test_mismatch_is_repaired_and_reverified(self) -> None:
        # first view = wrong base (dev-fix, the PR #4 bug); after `gh pr edit`
        # the second view confirms the repair took.
        self._stub(["dev-fix\n", "feat/local-longpolling\n"])
        ok, note = gp._verify_and_repair_pr_base(
            Path("/repo"), "https://github.com/o/r/pull/4", "feat/local-longpolling")
        self.assertTrue(ok)
        self.assertIn("repaired", note)
        self.assertIn("dev-fix", note)

    def test_mismatch_survives_when_gh_edit_fails(self) -> None:
        self._stub(["dev-fix\n"], edit_rc=1, edit_stderr="permission denied")
        ok, note = gp._verify_and_repair_pr_base(
            Path("/repo"), "https://github.com/o/r/pull/4", "feat/local-longpolling")
        self.assertFalse(ok)
        self.assertIn("permission denied", note)

    def test_mismatch_survives_when_edit_succeeds_but_base_still_wrong(self) -> None:
        # gh pr edit reports success (rc=0) but the re-verify still shows the
        # old base — e.g. a race, or a branch-protection rule silently
        # rejecting the retarget. Must fail CLOSED, not declare victory.
        self._stub(["dev-fix\n", "dev-fix\n"])
        ok, note = gp._verify_and_repair_pr_base(
            Path("/repo"), "https://github.com/o/r/pull/4", "feat/local-longpolling")
        self.assertFalse(ok)
        self.assertIn("NOT repaired", note)

    def test_gh_view_failure_fails_open(self) -> None:
        gp.subprocess.run = lambda *a, **k: _FakeCompletedProcess(1, "", "boom")
        ok, note = gp._verify_and_repair_pr_base(
            Path("/repo"), "https://github.com/o/r/pull/4", "feat/local-longpolling")
        self.assertTrue(ok)          # fail-open — never stop-the-line on gh flakiness
        self.assertIn("skipped", note)

    def test_missing_pr_url_or_base_is_skipped(self) -> None:
        ok, note = gp._verify_and_repair_pr_base(Path("/repo"), "", "main")
        self.assertTrue(ok)
        self.assertIn("skipped", note)
        ok, note = gp._verify_and_repair_pr_base(
            Path("/repo"), "https://github.com/o/r/pull/4", "")
        self.assertTrue(ok)
        self.assertIn("skipped", note)


class TryOpenDraftPrAlreadyPassesBaseTests(unittest.TestCase):
    """_try_open_draft_pr (the OTHER PR-creation path) was suspected too —
    confirm it already builds a `gh pr create ... --base <resolved>` argv, so
    the fix does not need to touch it."""

    def setUp(self) -> None:
        self._orig_run = gp.subprocess.run
        self._orig_base_branch = gp._base_branch
        self._orig_env = gp.os.environ.get("HANDOFF_DRAFT_PR_ENABLED")
        gp.os.environ["HANDOFF_DRAFT_PR_ENABLED"] = "1"

    def tearDown(self) -> None:
        gp.subprocess.run = self._orig_run
        gp._base_branch = self._orig_base_branch
        if self._orig_env is None:
            gp.os.environ.pop("HANDOFF_DRAFT_PR_ENABLED", None)
        else:
            gp.os.environ["HANDOFF_DRAFT_PR_ENABLED"] = self._orig_env

    def test_gh_pr_create_argv_includes_base(self) -> None:
        calls = []

        def _run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:2] == ["gh", "pr"]:
                return _FakeCompletedProcess(
                    0, "https://github.com/o/r/pull/9\n")
            return _FakeCompletedProcess(0, "")
        gp.subprocess.run = _run
        gp._base_branch = lambda target_repo: "feat/local-longpolling"
        gp._try_open_draft_pr(Path("/repo"), "feat/x", "task-1")
        pr_create_calls = [c for c in calls if c[:3] == ["gh", "pr", "create"]]
        self.assertEqual(len(pr_create_calls), 1)
        argv = pr_create_calls[0]
        self.assertIn("--base", argv)
        self.assertEqual(argv[argv.index("--base") + 1], "feat/local-longpolling")


if __name__ == "__main__":
    unittest.main()
