"""Runner crash-recovery tests.

Pin the fixes for the pattern-detector crash loop:
- A stage that raises an unhandled exception must NOT crash the runner — it
  becomes rc=1 so the caller marks the task `failed` (terminal), instead of
  leaving a non-terminal stage for the watcher to respawn forever.
- A respawned runner must SKIP stages that already produced a valid artifact
  (resume), instead of re-running discovery+ba on every recovery.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import stage_runner_agent as sra  # noqa: E402
import invest_validator as iv  # noqa: E402


class ExceptionSafetyTests(unittest.TestCase):
    def test_stage_crash_becomes_rc1(self) -> None:
        orig = sra._execute_single_stage_inner

        def boom(*_a, **_k):
            raise KeyError("simulated stage crash")

        sra._execute_single_stage_inner = boom
        try:
            rc = sra._execute_single_stage(Path("/tmp"), Path("/tmp"), "ba", {})
        finally:
            sra._execute_single_stage_inner = orig
        self.assertEqual(rc, 1)


class RunnerLockTests(unittest.TestCase):
    """Single-runner flock: the dispatcher, watcher, and manual ops can each
    decide to spawn a runner for the same task (the .runner.pid file is a
    TOCTOU race, not a lock). The runner's flock is the authoritative guard —
    only the lock holder proceeds; a second runner exits cleanly without doing
    billable work. Closes the 2026-05-31 double-spawn that doubled a run's cost."""

    def setUp(self) -> None:
        self._saved = sra._RUNNER_LOCK_FH
        sra._RUNNER_LOCK_FH = None

    def tearDown(self) -> None:
        fh = sra._RUNNER_LOCK_FH
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
        sra._RUNNER_LOCK_FH = self._saved

    def test_second_runner_denied_while_first_holds(self) -> None:
        import fcntl
        d = Path(tempfile.mkdtemp())
        # Simulate "another runner" holding the lock via an independent fd
        # (flock treats separate open descriptions as competitors, even in-proc).
        holder = (d / ".runner.lock").open("a+")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            self.assertFalse(sra._acquire_runner_lock(d))   # must back off
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            holder.close()
        # Lock now free → we can take it.
        self.assertTrue(sra._acquire_runner_lock(d))

    def test_acquire_writes_pid_and_holds_fd(self) -> None:
        d = Path(tempfile.mkdtemp())
        self.assertTrue(sra._acquire_runner_lock(d))
        self.assertIsNotNone(sra._RUNNER_LOCK_FH)           # fd retained, lock held
        self.assertEqual((d / ".runner.lock").read_text().strip(), str(os.getpid()))

    def test_release_on_exit_allows_reacquire(self) -> None:
        d = Path(tempfile.mkdtemp())
        self.assertTrue(sra._acquire_runner_lock(d))
        sra._RUNNER_LOCK_FH.close()                          # simulate process exit
        sra._RUNNER_LOCK_FH = None
        self.assertTrue(sra._acquire_runner_lock(d))         # free again → reacquire


class ResumeTests(unittest.TestCase):
    def test_artifact_ready_detection(self) -> None:
        d = Path(tempfile.mkdtemp())
        self.assertFalse(sra._stage_artifact_ready(d, "ba"))   # nothing produced yet

        (d / "01-ba.md").write_text("x" * 100)                 # canonical name
        self.assertTrue(sra._stage_artifact_ready(d, "ba"))

        d2 = Path(tempfile.mkdtemp())
        (d2 / "01-ba-agent.md").write_text("y" * 100)          # -agent name
        self.assertTrue(sra._stage_artifact_ready(d2, "ba"))

        d3 = Path(tempfile.mkdtemp())
        (d3 / "01-ba.md").write_text("tiny")                   # < 50 bytes → not ready
        self.assertFalse(sra._stage_artifact_ready(d3, "ba"))


class DevArtifactParseTests(unittest.TestCase):
    """Bug-8: DeepSeek writes PR/branch as Markdown, which the old strict
    DEV_COMPLETE/BRANCH:/PR_URL: regex missed → pr_url unpersisted → hotfix
    failed. The parsers must anchor on URL shape + a flexible branch marker."""

    DEEPSEEK_ARTIFACT = (
        "DEV_COMPLETE: added subtract(a, b)\n\n"
        "**Branch:** `phase-b4-poc-20260528-1818`  \n"
        "**PR:** https://github.com/Sekator778/ai-delivery-sandbox/pull/4  \n"
    )

    def test_pr_url_from_markdown(self) -> None:
        self.assertEqual(
            sra._extract_pr_url(self.DEEPSEEK_ARTIFACT),
            "https://github.com/Sekator778/ai-delivery-sandbox/pull/4",
        )

    def test_branch_from_markdown(self) -> None:
        self.assertEqual(
            sra._extract_branch(self.DEEPSEEK_ARTIFACT),
            "phase-b4-poc-20260528-1818",
        )

    def test_plain_marker_forms_still_work(self) -> None:
        plain = "BRANCH: phase-b4-poc-x\nPR_URL: https://github.com/o/r/pull/9\n"
        self.assertEqual(sra._extract_pr_url(plain), "https://github.com/o/r/pull/9")
        self.assertEqual(sra._extract_branch(plain), "phase-b4-poc-x")

    def test_no_pr_returns_none(self) -> None:
        self.assertIsNone(sra._extract_pr_url("no pull request was opened"))


class DevBranchHygieneTests(unittest.TestCase):
    """2026-06-01 stale-base fix: the Developer prompt must cut its branch FRESH
    from origin/<base>, never from the current local HEAD. On 2026-05-31 a new PR
    re-included a prior already-merged task's diff because the branch was cut from
    a stale local main + dirty tree (PR #8 carrying PR #7's --version diff)."""

    def _render_developer(self, target: str = "/home/x/projects/telegram-userbot-ai") -> str:
        d = Path(tempfile.mkdtemp()) / "TASK-DEV"
        d.mkdir()
        kw = sra._build_format_kwargs("developer", d, Path(target), {})
        return sra.STAGE_PROMPTS["developer"].format(**kw), kw

    def test_branch_cut_from_origin_not_local_head(self) -> None:
        rendered, kw = self._render_developer()
        self.assertEqual(kw["base_branch"], "main")
        # fresh-base idiom present
        self.assertIn("git fetch origin", rendered)
        self.assertIn(
            "git checkout -B <BRANCH_NAME_FROM_ORCHESTRATOR> origin/main", rendered)
        # the unsafe local-HEAD cut is gone
        self.assertNotIn(
            "git checkout -b <BRANCH_NAME_FROM_ORCHESTRATOR>\n", rendered)
        self.assertNotIn(
            "&& git checkout -b <BRANCH_NAME_FROM_ORCHESTRATOR>", rendered)

    def test_pr_base_matches_base_branch(self) -> None:
        rendered, _ = self._render_developer()
        self.assertIn("--base main", rendered)

    def test_no_blanket_git_add(self) -> None:
        # must PROHIBIT `git add -A`/`.` so unrelated untracked files (caches,
        # leftovers) don't enter the PR. Anchor on the contiguous phrase — a bare
        # assertIn("NEVER") would pass on any of the other 4 unrelated NEVER rules,
        # so a softened wording ("PREFER git add -A") would slip through.
        rendered, _ = self._render_developer()
        self.assertIn("NEVER `git add -A`", rendered)
        self.assertIn("`git add .`", rendered)

    def test_base_branch_overridable_via_env(self) -> None:
        os.environ["PIPELINE_BASE_BRANCH"] = "develop"
        try:
            rendered, kw = self._render_developer()
        finally:
            os.environ.pop("PIPELINE_BASE_BRANCH", None)
        self.assertEqual(kw["base_branch"], "develop")
        self.assertIn("origin/develop", rendered)
        self.assertIn("--base develop", rendered)


class BranchBaseCheckTests(unittest.TestCase):
    """Python-enforced base check (the backstop the branch-NAME gate lacks): the
    developer's branch must be cut from a FRESH origin/<base>, not a stale local
    HEAD. The 2026-05-31 regression cut a correctly-named feat/ branch from stale
    main and re-included an already-merged diff — the name gate could not catch
    it. _branch_base_ok must FAIL-CLOSED on a confirmed stale base and FAIL-OPEN
    on any git/infra error (never stop-the-line)."""

    def _git(self, *args, cwd):
        import subprocess
        return subprocess.run(["git", *args], cwd=str(cwd),
                              capture_output=True, text=True)

    def _remote_and_clone(self) -> Path:
        import subprocess
        root = Path(tempfile.mkdtemp())
        origin = root / "origin.git"
        work = root / "work"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                       capture_output=True)
        subprocess.run(["git", "clone", str(origin), str(work)], capture_output=True)
        self._git("config", "user.email", "t@t.io", cwd=work)
        self._git("config", "user.name", "t", cwd=work)
        (work / "base.txt").write_text("base\n")
        self._git("add", "-A", cwd=work)
        self._git("commit", "-m", "base", cwd=work)
        self._git("push", "origin", "main", cwd=work)
        return work

    def test_fresh_branch_passes(self) -> None:
        work = self._remote_and_clone()
        self._git("checkout", "-B", "feat/x", "origin/main", cwd=work)
        (work / "f.txt").write_text("x\n")
        self._git("add", "f.txt", cwd=work)
        self._git("commit", "-m", "feat", cwd=work)
        ok, reason = sra._branch_base_ok(work, "main")
        self.assertTrue(ok, reason)

    def test_stale_base_branch_fails_closed(self) -> None:
        work = self._remote_and_clone()
        stale = self._git("rev-parse", "HEAD", cwd=work).stdout.strip()
        # origin/main advances past the stale commit (a prior task got merged)
        (work / "merged.txt").write_text("merged\n")
        self._git("add", "merged.txt", cwd=work)
        self._git("commit", "-m", "prior task merged", cwd=work)
        self._git("push", "origin", "main", cwd=work)
        # developer cuts a (correctly-named) branch from the STALE local commit
        self._git("checkout", "-B", "feat/y", stale, cwd=work)
        (work / "g.txt").write_text("y\n")
        self._git("add", "g.txt", cwd=work)
        self._git("commit", "-m", "feat on stale base", cwd=work)
        ok, reason = sra._branch_base_ok(work, "main")
        self.assertFalse(ok, reason)            # origin/main not an ancestor → stale

    def test_missing_remote_fails_open(self) -> None:
        import subprocess
        d = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-b", "main", str(d)], capture_output=True)
        self._git("config", "user.email", "t@t.io", cwd=d)
        self._git("config", "user.name", "t", cwd=d)
        (d / "a.txt").write_text("a\n")
        self._git("add", "-A", cwd=d)
        self._git("commit", "-m", "a", cwd=d)
        ok, _ = sra._branch_base_ok(d, "main")  # no origin remote at all
        self.assertTrue(ok)                      # fail-open, never stop-the-line


class FallbackTests(unittest.TestCase):
    """Auto-fallback: a failed non-anthropic stage retries on anthropic. The
    retry forces it via model_routing[stage]='anthropic'; on failure the error
    is dumped to a log so it's diagnosable (Bug-9)."""

    def test_routing_override_forces_anthropic(self) -> None:
        # Since 2026-06-07 every stage defaults to anthropic, so pin a cheap
        # default explicitly — otherwise "the override forced anthropic" would
        # pass even if the override were ignored entirely.
        saved = dict(sra.BACKEND)
        self.addCleanup(lambda: (sra.BACKEND.clear(), sra.BACKEND.update(saved)))
        sra.BACKEND["developer"] = "deepseek"
        backend, _ = sra._resolve_stage_backend("developer", 1, {})
        self.assertEqual(backend, "deepseek")  # default routing
        backend2, _ = sra._resolve_stage_backend("developer", 1, {"developer": "anthropic"})
        self.assertEqual(backend2, "anthropic")  # explicit per-stage routing

    def test_backend_override_param_is_wired(self) -> None:
        # The fallback calls _execute_single_stage(..., backend_override="anthropic").
        # An unknown stage returns 4 before any backend logic — this proves the
        # param threads through the wrapper to the inner without a TypeError
        # (Bug-11: the state-dict override never reached _execute, so the retry
        # silently re-ran the same backend).
        rc = sra._execute_single_stage(
            Path("/tmp"), Path("/tmp"), "no-such-stage", {}, backend_override="anthropic",
        )
        self.assertEqual(rc, 4)

    def test_dump_stage_error_writes_log(self) -> None:
        import types
        d = Path(tempfile.mkdtemp())
        proc = types.SimpleNamespace(stdout="OUT-tail", stderr="BOOM-traceback")
        sra._dump_stage_error(d, "developer", proc, "rc=1")
        log = d / "developer.claude-error.log"
        self.assertTrue(log.is_file())
        txt = log.read_text()
        self.assertIn("BOOM-traceback", txt)
        self.assertIn("OUT-tail", txt)


class AnalyzeGateTests(unittest.TestCase):
    """WS-5: the analyze gate must read the TRAILING CRITICAL_COUNT line, never a
    prose/table mention earlier in the report — the false-block bug class shared
    with the PR-URL extractor (anchor on position/shape, not first match)."""

    def test_trailing_count_parsed(self) -> None:
        d = Path(tempfile.mkdtemp())
        self.assertIsNone(sra._analyze_critical_count(d))            # no report
        (d / "02c-analyze.md").write_text("## report\nstuff\nCRITICAL_COUNT: 2\n")
        self.assertEqual(sra._analyze_critical_count(d), 2)

    def test_prose_mention_does_not_false_block(self) -> None:
        d = Path(tempfile.mkdtemp())
        # a findings row quotes the token mid-line; the real trailing count is 0
        (d / "02c-analyze.md").write_text(
            "| I1 | Inconsistency | LOW | spec | mentions CRITICAL_COUNT: 5 | fix |\n"
            "\nCRITICAL_COUNT: 0\n"
        )
        self.assertEqual(sra._analyze_critical_count(d), 0)

    def test_agent_suffixed_fallback(self) -> None:
        d = Path(tempfile.mkdtemp())
        (d / "02c-analyze-agent.md").write_text("CRITICAL_COUNT: 3\n")
        self.assertEqual(sra._analyze_critical_count(d), 3)


class SpecsFolderMirrorTests(unittest.TestCase):
    """WS-4b alias-staging (Фаза 1): the additive specs/ folder mirror is
    opt-in (default OFF), maps ONLY the three documented Spec-Kit filenames
    (ba→spec.md, architect→plan.md, tasks→tasks.md), mirrors the .json sibling,
    and is idempotent. Purely additive — the flat canonical names stay primary."""

    def _seed_ba(self, d: Path) -> None:
        (d / "01-ba.md").write_text("# BRD\n" + "x" * 100)
        (d / "01-ba.json").write_text('{"cost": {}}')

    def test_off_by_default_no_folder(self) -> None:
        d = Path(tempfile.mkdtemp())
        self._seed_ba(d)
        os.environ.pop("SPECS_FOLDER_MIRROR_ENABLED", None)
        sra._mirror_to_specs_folder(d, "ba")
        self.assertFalse((d / "specs").exists())          # nothing created when OFF

    def test_on_mirrors_spec_and_json(self) -> None:
        d = Path(tempfile.mkdtemp())
        self._seed_ba(d)
        os.environ["SPECS_FOLDER_MIRROR_ENABLED"] = "1"
        try:
            sra._mirror_to_specs_folder(d, "ba")
        finally:
            os.environ.pop("SPECS_FOLDER_MIRROR_ENABLED", None)
        self.assertTrue((d / "specs" / "spec.md").is_file())   # spec-kit name
        self.assertTrue((d / "specs" / "spec.json").is_file()) # .json sibling
        self.assertTrue((d / "01-ba.md").is_file())            # flat stays primary

    def test_unmapped_stage_is_noop(self) -> None:
        d = Path(tempfile.mkdtemp())
        (d / "05-security.md").write_text("y" * 100)
        os.environ["SPECS_FOLDER_MIRROR_ENABLED"] = "1"
        try:
            sra._mirror_to_specs_folder(d, "security")     # not in SPECS_FOLDER_SEMANTIC
        finally:
            os.environ.pop("SPECS_FOLDER_MIRROR_ENABLED", None)
        self.assertFalse((d / "specs").exists())

    def test_idempotent_and_missing_src_noop(self) -> None:
        d = Path(tempfile.mkdtemp())
        os.environ["SPECS_FOLDER_MIRROR_ENABLED"] = "1"
        try:
            sra._mirror_to_specs_folder(d, "architect")    # no 02-architecture.md yet → noop
            self.assertFalse((d / "specs").exists())
            (d / "02-architecture.md").write_text("z" * 100)
            sra._mirror_to_specs_folder(d, "architect")
            sra._mirror_to_specs_folder(d, "architect")    # second run must not raise
        finally:
            os.environ.pop("SPECS_FOLDER_MIRROR_ENABLED", None)
        self.assertTrue((d / "specs" / "plan.md").is_file())


class InvestFooterTests(unittest.TestCase):
    """The INVEST report footer must state the gate's ACTUAL mode. A report
    that says 'warn-only' while the gate hard-blocks misleads the operator
    (regression observed 2026-05-29 on the sandbox power run)."""

    def _bad_report(self):
        return iv.Report(
            artifact="x",
            violations=[iv.Violation(line=1, kind="no_ac", snippet="FR=1 AC=0")],
            fr_count=1,
        )

    def test_blocking_footer_says_blocking(self) -> None:
        self.assertIn("BLOCKING", iv.format_report(self._bad_report(), blocking=True))

    def test_warn_only_footer_says_warn_only(self) -> None:
        self.assertIn("warn-only", iv.format_report(self._bad_report(), blocking=False))

    def test_footer_defaults_from_env(self) -> None:
        rep = self._bad_report()
        os.environ.pop("INVEST_BLOCKING", None)
        self.assertIn("BLOCKING", iv.format_report(rep))        # default = block
        os.environ["INVEST_BLOCKING"] = "0"
        try:
            self.assertIn("warn-only", iv.format_report(rep))
        finally:
            os.environ.pop("INVEST_BLOCKING", None)


if __name__ == "__main__":
    unittest.main()
