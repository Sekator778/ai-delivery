"""Ephemeral worktree isolation for task execution (#6).

The runner used to hand the developer subagent the target repo's LIVE checkout
and tell it to `git checkout -B <branch>` there. On the first self-targeted run
that checkout WAS the running ai-delivery deployment: its files vanished from
disk mid-run and freshly spawned runners started executing June-era code.

The property under test: running a task NEVER changes the target checkout's
branch or working tree. Everything here drives real git — the flow is thin
enough not to need mocks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import stage_runner_agent as sra  # noqa: E402


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo),
                          capture_output=True, text=True)


def _make_repo() -> Path:
    """A clone with an origin, on branch 'main', one commit."""
    root = Path(tempfile.mkdtemp())
    origin, work = root / "origin.git", root / "work"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   capture_output=True, check=True)
    subprocess.run(["git", "clone", str(origin), str(work)], capture_output=True)
    _git(work, "config", "user.email", "t@t.io")
    _git(work, "config", "user.name", "t")
    (work / "live-file.txt").write_text("deployment file\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "push", "origin", "main")
    # resolve(): macOS /var -> /private/var; the allowlist normalizes its entries,
    # so an unresolved path would never match MERGEABLE_REPO_PATHS.
    return work.resolve()


class WorktreeLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _make_repo()
        self.task_dir = Path(tempfile.mkdtemp()) / "TASK-WT"
        self.task_dir.mkdir()
        (self.task_dir / "state.json").write_text("{}")
        self._root = sra._WORKTREE_ROOT
        sra._WORKTREE_ROOT = Path(tempfile.mkdtemp()) / "wt-root"
        self._env = {k: os.environ.get(k) for k in
                     ("WORKTREE_ISOLATION_ENABLED", "MERGEABLE_REPO_PATHS",
                      "PIPELINE_BASE_BRANCH", "PROJECTS_REGISTRY")}
        os.environ.pop("WORKTREE_ISOLATION_ENABLED", None)
        os.environ.pop("PIPELINE_BASE_BRANCH", None)
        os.environ["PROJECTS_REGISTRY"] = "/nonexistent/projects.json"
        # opt the target INTO real (mergeable) mode → feat/<task-id> branches
        os.environ["MERGEABLE_REPO_PATHS"] = str(self.repo)

    def tearDown(self) -> None:
        sra._WORKTREE_ROOT = self._root
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _state(self) -> dict:
        return json.loads((self.task_dir / "state.json").read_text())

    def test_worktree_created_on_expected_branch_without_touching_target(self) -> None:
        before_branch = _git(self.repo, "branch", "--show-current").stdout.strip()
        state: dict = {"base_branch": "main"}
        wt = sra._ensure_task_worktree(self.task_dir, self.repo, state, "TASK-WT")
        self.assertIsNotNone(wt)
        # the isolated checkout is on the task branch...
        self.assertEqual(_git(wt, "branch", "--show-current").stdout.strip(),
                         "feat/TASK-WT")
        # ...and it is a real, separate directory with the repo content
        self.assertTrue((wt / "live-file.txt").is_file())
        self.assertNotEqual(wt.resolve(), self.repo.resolve())
        # THE regression: the live checkout never moved off its branch and its
        # working tree is untouched.
        self.assertEqual(_git(self.repo, "branch", "--show-current").stdout.strip(),
                         before_branch)
        self.assertTrue((self.repo / "live-file.txt").is_file())
        self.assertEqual(_git(self.repo, "status", "--porcelain").stdout, "")
        # the record is persisted for resume + cleanup
        record = self._state()["worktree"]
        self.assertEqual(record["branch"], "feat/TASK-WT")
        self.assertEqual(record["target_repo"], str(self.repo))
        self.assertEqual(Path(record["path"]).resolve(), wt.resolve())

    def test_branch_is_cut_from_fresh_origin_base(self) -> None:
        # origin/main advances after this clone last fetched
        other = Path(tempfile.mkdtemp()) / "second"
        origin = _git(self.repo, "remote", "get-url", "origin").stdout.strip()
        subprocess.run(["git", "clone", origin, str(other)], capture_output=True)
        _git(other, "config", "user.email", "t@t.io")
        _git(other, "config", "user.name", "t")
        (other / "merged.txt").write_text("prior task merged\n")
        _git(other, "add", "-A")
        _git(other, "commit", "-m", "prior task")
        _git(other, "push", "origin", "main")

        wt = sra._ensure_task_worktree(self.task_dir, self.repo,
                                       {"base_branch": "main"}, "TASK-WT")
        # cut from the just-fetched origin/main, so the stale-base gate passes
        self.assertTrue((wt / "merged.txt").is_file())
        ok, reason = sra._branch_base_ok(wt, "main")
        self.assertTrue(ok, reason)

    def test_reuse_is_idempotent_then_resume_recreates_on_same_branch(self) -> None:
        state: dict = {"base_branch": "main"}
        first = sra._ensure_task_worktree(self.task_dir, self.repo, state, "TASK-WT")
        again = sra._ensure_task_worktree(self.task_dir, self.repo, state, "TASK-WT")
        self.assertEqual(first, again)          # no second worktree

        (first / "work.txt").write_text("committed work\n")
        _git(first, "add", "-A")
        _git(first, "commit", "-m", "feat: work")

        sra._remove_task_worktree(self.task_dir, state)
        self.assertFalse(first.exists())
        # the BRANCH survives removal — it carries the pushed PR
        self.assertTrue(sra._branch_exists(self.repo, "feat/TASK-WT"))

        # a respawn re-attaches to the SAME branch, keeping the commits
        again2 = sra._ensure_task_worktree(self.task_dir, self.repo, state, "TASK-WT")
        self.assertEqual(_git(again2, "branch", "--show-current").stdout.strip(),
                         "feat/TASK-WT")
        self.assertTrue((again2 / "work.txt").is_file())

    def test_parallel_tasks_never_share_a_branch(self) -> None:
        # a leftover branch of the same name must not be hijacked
        _git(self.repo, "branch", "feat/TASK-WT")
        wt = sra._ensure_task_worktree(self.task_dir, self.repo,
                                       {"base_branch": "main"}, "TASK-WT")
        self.assertEqual(_git(wt, "branch", "--show-current").stdout.strip(),
                         "feat/TASK-WT-1")

    def test_isolation_can_be_disabled(self) -> None:
        os.environ["WORKTREE_ISOLATION_ENABLED"] = "0"
        self.assertIsNone(sra._ensure_task_worktree(
            self.task_dir, self.repo, {}, "TASK-WT"))
        self.assertNotIn("worktree", self._state())

    def test_removal_of_a_never_created_worktree_is_a_noop(self) -> None:
        sra._remove_task_worktree(self.task_dir, {})   # must not raise
        self.assertNotIn("worktree", self._state())


class StageRepoRoutingTests(unittest.TestCase):
    """Which directory each stage runs in."""

    def setUp(self) -> None:
        self.repo = _make_repo()
        self.task_dir = Path(tempfile.mkdtemp()) / "TASK-ROUTE"
        self.task_dir.mkdir()
        (self.task_dir / "state.json").write_text("{}")
        self._root = sra._WORKTREE_ROOT
        sra._WORKTREE_ROOT = Path(tempfile.mkdtemp()) / "wt-root"
        self._env = {k: os.environ.get(k) for k in
                     ("WORKTREE_ISOLATION_ENABLED", "PROJECTS_REGISTRY")}
        os.environ.pop("WORKTREE_ISOLATION_ENABLED", None)
        os.environ["PROJECTS_REGISTRY"] = "/nonexistent/projects.json"

    def tearDown(self) -> None:
        sra._WORKTREE_ROOT = self._root
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_upstream_stages_read_the_target_checkout(self) -> None:
        for stage in ("discovery", "ba", "pattern-detector", "architect"):
            self.assertEqual(
                sra._stage_repo(self.task_dir, self.repo, stage, {}, "TASK-ROUTE"),
                self.repo, stage)

    def test_developer_creates_and_downstream_stages_reuse(self) -> None:
        state: dict = {"base_branch": "main"}
        dev = sra._stage_repo(self.task_dir, self.repo, "developer", state, "TASK-ROUTE")
        self.assertNotEqual(dev, self.repo)
        for stage in ("tester", "security", "reviewer", "developer-hotfix"):
            self.assertEqual(
                sra._stage_repo(self.task_dir, self.repo, stage, state, "TASK-ROUTE"),
                dev, stage)

    def test_developer_fails_closed_when_the_worktree_cannot_be_made(self) -> None:
        not_a_repo = Path(tempfile.mkdtemp())
        self.assertIsNone(sra._stage_repo(
            self.task_dir, not_a_repo, "developer", {}, "TASK-ROUTE"))

    def test_downstream_stages_fall_back_instead_of_creating(self) -> None:
        # no developer stage ran → nothing to reuse; a read-mostly stage must not
        # start creating worktrees of its own.
        self.assertEqual(
            sra._stage_repo(self.task_dir, self.repo, "tester", {}, "TASK-ROUTE"),
            self.repo)
        self.assertNotIn("worktree", json.loads(
            (self.task_dir / "state.json").read_text()))

    def test_legacy_mode_runs_in_place(self) -> None:
        os.environ["WORKTREE_ISOLATION_ENABLED"] = "0"
        self.assertEqual(
            sra._stage_repo(self.task_dir, self.repo, "developer", {}, "TASK-ROUTE"),
            self.repo)


class StageDispatchIntegrationTests(unittest.TestCase):
    """Drive the real _run_pipeline_stage_with_breadcrumbs (the single funnel
    every stage is dispatched through) with only the claude subprocess stubbed,
    so the wiring — not just the helpers — is pinned."""

    def setUp(self) -> None:
        self.repo = _make_repo()
        self.task_dir = Path(tempfile.mkdtemp()) / "TASK-DISPATCH"
        self.task_dir.mkdir()
        (self.task_dir / "state.json").write_text("{}")
        (self.task_dir / "01-ba.md").write_text("# BRD\n")
        self._root = sra._WORKTREE_ROOT
        sra._WORKTREE_ROOT = Path(tempfile.mkdtemp()) / "wt-root"
        self._env = {k: os.environ.get(k) for k in
                     ("MERGEABLE_REPO_PATHS", "PROJECTS_REGISTRY",
                      "WORKTREE_ISOLATION_ENABLED")}
        os.environ["MERGEABLE_REPO_PATHS"] = str(self.repo)
        os.environ["PROJECTS_REGISTRY"] = "/nonexistent/projects.json"
        os.environ.pop("WORKTREE_ISOLATION_ENABLED", None)
        self._saved = {n: getattr(sra, n) for n in (
            "_execute_single_stage", "_read_stage_cost_usd", "_send_telegram",
            "_notify_bot", "_canonicalize_stage_artifact",
            "_mirror_to_specs_folder", "_token_cap_exceeded")}
        sra._read_stage_cost_usd = lambda *a, **k: 0.0
        sra._send_telegram = lambda *a, **k: None
        sra._notify_bot = lambda *a, **k: None
        sra._canonicalize_stage_artifact = lambda *a, **k: None
        sra._mirror_to_specs_folder = lambda *a, **k: None
        sra._token_cap_exceeded = lambda *a, **k: False

    def tearDown(self) -> None:
        sra._WORKTREE_ROOT = self._root
        for n, v in self._saved.items():
            setattr(sra, n, v)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_developer_stage_runs_in_the_worktree_not_the_live_checkout(self) -> None:
        seen: list = []

        def fake_stage(task_dir, repo, stage, state, backend_override=None):
            seen.append(Path(repo))
            # act like a well-behaved subagent: commit on the branch it woke up on
            (Path(repo) / "feature.py").write_text("def f(): ...\n")
            _git(Path(repo), "add", "-A")
            _git(Path(repo), "commit", "-m", "feat: thing")
            return 0

        sra._execute_single_stage = fake_stage
        state: dict = {"base_branch": "main"}
        rc, _cost, _state = sra._run_pipeline_stage_with_breadcrumbs(
            self.task_dir, self.repo, "developer", state, "TASK-DISPATCH",
            0.0, 100.0, self.task_dir / "state.json")

        self.assertEqual(rc, 0)
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0].resolve(), self.repo.resolve())
        self.assertEqual(_git(seen[0], "branch", "--show-current").stdout.strip(),
                         "feat/TASK-DISPATCH")
        # the live checkout: same branch, clean tree, no stray file
        self.assertEqual(_git(self.repo, "branch", "--show-current").stdout.strip(),
                         "main")
        self.assertEqual(_git(self.repo, "status", "--porcelain").stdout, "")
        self.assertFalse((self.repo / "feature.py").exists())

    def test_stage_fails_closed_instead_of_using_the_live_checkout(self) -> None:
        called: list = []
        sra._execute_single_stage = lambda *a, **k: called.append(a) or 0
        not_a_repo = Path(tempfile.mkdtemp())
        rc, _cost, _state = sra._run_pipeline_stage_with_breadcrumbs(
            self.task_dir, not_a_repo, "developer", {}, "TASK-DISPATCH",
            0.0, 100.0, self.task_dir / "state.json")
        self.assertEqual(rc, 2)
        self.assertEqual(called, [])   # the stage never ran anywhere
        self.assertEqual(
            json.loads((self.task_dir / "state.json").read_text())["stage"],
            "failed")


class DeveloperPromptTests(unittest.TestCase):
    """The prompt must describe the world the subagent actually wakes up in."""

    def _render(self, state: dict, stage: str = "developer",
                target: str = "/repos/target") -> tuple[str, dict]:
        task_dir = Path(tempfile.mkdtemp()) / "TASK-PROMPT"
        task_dir.mkdir()
        kw = sra._build_format_kwargs(stage, task_dir, Path(target), state)
        return sra.STAGE_PROMPTS[stage].format(**kw), kw

    def test_worktree_mode_forbids_switching_branches(self) -> None:
        state = {"base_branch": "dev",
                 "worktree": {"path": "/tmp/wt/TASK-PROMPT-abc",
                              "branch": "feat/TASK-PROMPT"}}
        rendered, kw = self._render(state, target="/tmp/wt/TASK-PROMPT-abc")
        self.assertEqual(kw["branch_name"], "feat/TASK-PROMPT")
        self.assertIn("already on `feat/TASK-PROMPT`".lower(), rendered.lower())
        self.assertIn("isolated", rendered)
        self.assertIn("NEVER run `git checkout`", rendered)
        # the legacy in-place branch cut must be gone
        self.assertNotIn("git checkout -B <BRANCH_NAME_FROM_ORCHESTRATOR>", rendered)
        # base branch still drives the PR target
        self.assertIn("--base dev", rendered)

    def test_legacy_mode_keeps_the_fresh_base_checkout(self) -> None:
        rendered, kw = self._render({"base_branch": "main"})
        self.assertIn("git fetch origin", rendered)
        self.assertIn("git checkout -B <BRANCH_NAME_FROM_ORCHESTRATOR> origin/main",
                      rendered)
        self.assertEqual(kw["branch_name"], "<BRANCH_NAME_FROM_ORCHESTRATOR>")

    def test_hotfix_in_a_worktree_does_not_checkout(self) -> None:
        state = {"branch": "feat/TASK-PROMPT", "pr_url": "https://github.com/o/r/pull/1",
                 "iteration": 2, "iteration_cap": 3,
                 "worktree": {"path": "/tmp/wt/x", "branch": "feat/TASK-PROMPT"}}
        rendered, _ = self._render(state, stage="developer-hotfix")
        self.assertNotIn("`git checkout feat/TASK-PROMPT`", rendered)
        self.assertIn("Do NOT `git checkout`", rendered)

    def test_hotfix_without_a_worktree_still_checks_out(self) -> None:
        state = {"branch": "feat/TASK-PROMPT", "pr_url": "https://github.com/o/r/pull/1",
                 "iteration": 2, "iteration_cap": 3}
        rendered, _ = self._render(state, stage="developer-hotfix")
        self.assertIn("`git checkout feat/TASK-PROMPT`", rendered)


if __name__ == "__main__":
    unittest.main()
