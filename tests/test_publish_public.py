"""Integration tests for scripts/publish-public.sh.

All tests run the script as a subprocess against a throwaway fixture repo.
PUBLISH_REPO_ROOT points at the fixture, PUBLISH_REMOTE points at a local
bare repo, so no network access or credentials are required (ADR-010).

Fixture secrets assembled at runtime — never literal in source (E9).
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "publish-public.sh"

# Remote name used inside each fixture repo
FIXTURE_REMOTE = "test-mirror"

# Secret prefix stored as a module-level variable so that any concatenation
# with a suffix literal is evaluated at runtime, not folded into a single
# constant in the pyc bytecode (E9 self-poisoning guard).
_GH_PAT_PREFIX = "ghp_"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def _make_fixture() -> tuple[Path, Path, Path]:
    """Return (src, mirror, tmproot).

    src    — private source repo (dev branch, representative files, one tag)
    mirror — local bare repo acting as the public mirror (main branch seeded)
    tmproot — parent temp dir (caller should shutil.rmtree on teardown)
    """
    tmproot = Path(tempfile.mkdtemp())
    src = tmproot / "src"
    mirror = tmproot / "mirror.git"
    seed_dir = tmproot / "seed"

    # ---- source repo ----
    subprocess.run(["git", "init", "-b", "dev", str(src)],
                   capture_output=True, check=True)
    _git(src, "config", "user.email", "fixture@test.io")
    _git(src, "config", "user.name", "fixture")

    # Public-facing files
    (src / "README.md").write_text("# Public readme\n")
    (src / "CHANGELOG.md").write_text(
        "## [Unreleased]\n\n"
        "- **Test bullet.** Integration test run.\n\n"
        "## v1.0.0\n\n- Initial release\n"
    )

    # Private directories that must be excluded
    (src / "STATE").mkdir()
    (src / "STATE" / "secret.md").write_text("private state\n")
    (src / "research").mkdir()
    (src / "research" / "data.md").write_text("research data\n")
    (src / "briefs").mkdir()
    (src / "briefs" / "brief.md").write_text("brief content\n")
    (src / "memory-bank").mkdir()
    (src / "memory-bank" / "README.md").write_text("memory-bank readme\n")
    # .claude/ — partial keep (2026-08-15): agents + commands are the product,
    # templates/ (vendored, upstream-licensed) and settings.json are not.
    (src / ".claude").mkdir()
    (src / ".claude" / "settings.json").write_text("{}\n")           # DROP
    (src / ".claude" / "agents").mkdir()
    (src / ".claude" / "agents" / "blind-hunter.md").write_text("lens\n")  # KEEP
    (src / ".claude" / "commands").mkdir()
    (src / ".claude" / "commands" / "team-review.md").write_text("cmd\n")  # KEEP
    (src / ".claude" / "templates").mkdir()
    (src / ".claude" / "templates" / "vendored.md").write_text("bmad\n")   # DROP

    # Single excluded file
    (src / "bot").mkdir()
    (src / "bot" / "projects.json").write_text("{}\n")

    # Env files (excluded) and example (kept)
    (src / "bot" / ".env").write_text("SECRET=abc\n")
    (src / "bot" / ".env.example").write_text("SECRET=placeholder\n")

    # tasks/ — partial keep
    (src / "tasks").mkdir()
    (src / "tasks" / "README.md").write_text("Tasks readme\n")  # KEEP
    (src / "tasks" / "_TEMPLATE").mkdir()
    (src / "tasks" / "_TEMPLATE" / "task.md").write_text("Template\n")  # KEEP
    (src / "tasks" / "active").mkdir()
    (src / "tasks" / "active" / ".gitkeep").write_text("")  # KEEP
    (src / "tasks" / "done").mkdir()
    (src / "tasks" / "done" / ".gitkeep").write_text("")  # KEEP

    # ops dir with blocklist
    (src / "ops").mkdir()
    (src / "ops" / "publish-blocklist.local").write_text("# empty test blocklist\n")

    # docs/ — an allowlisted top-level directory. Gate tests plant their leak
    # files here rather than at the repo root: since 2026-08-20 the top-level
    # filter is fail-closed (PUBLIC_TOPLEVEL), so a root-level leak.txt would
    # be dropped from the export before either scan could see it and the gate
    # would never fire. Planting inside an allowlisted directory is what the
    # real leak shape looks like anyway.
    (src / "docs").mkdir()
    (src / "docs" / "notes.md").write_text("public notes\n")

    _git(src, "add", "-A")
    _git(src, "commit", "-m", "init")
    _git(src, "tag", "v1.0.0")

    # ---- bare mirror repo ----
    subprocess.run(["git", "init", "--bare", "-b", "main", str(mirror)],
                   capture_output=True, check=True)

    # Seed mirror with a different tree so the first publish has changes
    seed_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(seed_dir)],
                   capture_output=True, check=True)
    _git(seed_dir, "config", "user.email", "seed@test.io")
    _git(seed_dir, "config", "user.name", "seed")
    (seed_dir / "README.md").write_text("# Public mirror v0.8\n")
    _git(seed_dir, "add", "-A")
    _git(seed_dir, "commit", "-m", "v0.8: initial public snapshot")
    subprocess.run(["git", "-C", str(seed_dir), "remote", "add", "origin", str(mirror)],
                   capture_output=True, check=True)
    subprocess.run(["git", "-C", str(seed_dir), "push", "origin", "main"],
                   capture_output=True, check=True)

    # Add mirror as remote in src
    _git(src, "remote", "add", FIXTURE_REMOTE, str(mirror))

    return src, mirror, tmproot


def _add_source_origin(src: Path, tmproot: Path) -> Path:
    """Give the fixture an `origin` remote holding the same dev tip.

    _make_fixture deliberately leaves `src` with only the mirror remote, so the
    freshness check (T23) reports "nothing to be stale against" and every older
    test keeps passing untouched. The freshness tests need the opposite, so they
    opt in by calling this.
    """
    origin = tmproot / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "dev", str(origin)],
                   capture_output=True, check=True)
    _git(src, "remote", "add", "origin", str(origin))
    _git(src, "push", "origin", "dev")
    return origin


def _advance_origin(src: Path, message: str) -> None:
    """Push one commit to origin/dev without moving the local dev branch."""
    _git(src, "checkout", "-q", "-b", "_ahead")
    (src / "README.md").write_text((src / "README.md").read_text() + message + "\n")
    _git(src, "commit", "-qam", message)
    _git(src, "push", "-q", "origin", "_ahead:dev")
    _git(src, "checkout", "-q", "dev")
    _git(src, "branch", "-qD", "_ahead")


def _commit_locally(src: Path, message: str) -> None:
    """Add one commit to local dev that origin does not have."""
    (src / "README.md").write_text((src / "README.md").read_text() + message + "\n")
    _git(src, "commit", "-qam", message)


def _run(src: Path, mirror: Path, *extra_args: str,
         env_override: dict | None = None) -> subprocess.CompletedProcess:
    """Run the publish script against the fixture."""
    env = os.environ.copy()
    env["PUBLISH_REPO_ROOT"] = str(src)
    env["PUBLISH_REMOTE"] = FIXTURE_REMOTE
    if env_override:
        env.update(env_override)
    return subprocess.run(
        ["bash", str(SCRIPT), *extra_args],
        env=env,
        capture_output=True,
        text=True,
    )


def _mirror_tip(mirror: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "main"],
        cwd=str(mirror), capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _mirror_commits(mirror: Path) -> list[str]:
    result = subprocess.run(
        ["git", "log", "--format=%H", "main"],
        cwd=str(mirror), capture_output=True, text=True, check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestExport(unittest.TestCase):
    """AC-01: exported tree contains no excluded paths."""

    def setUp(self) -> None:
        self.src, self.mirror, self.tmp = _make_fixture()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ac01_exported_tree_excludes_private_paths(self) -> None:
        result = _run(self.src, self.mirror, "--keep-tmp")
        # Extract TMPROOT from stderr
        tmproot = None
        for line in result.stderr.splitlines():
            if "TMPROOT kept at:" in line:
                tmproot = line.split("TMPROOT kept at:")[-1].strip()
                break
        self.assertIsNotNone(tmproot, f"TMPROOT not found in stderr:\n{result.stderr}")
        export_dir = Path(tmproot) / "export"
        self.assertTrue(export_dir.exists(), f"export dir missing at {export_dir}")

        # Collect all exported paths relative to export root
        exported = set()
        for p in export_dir.rglob("*"):
            if p.is_file() or p.is_symlink():
                exported.add(str(p.relative_to(export_dir)))

        # None of these should be in the export
        excluded_prefixes = ("STATE/", "research/", "briefs/", "memory-bank/",
                             ".claude/templates/", ".claude/settings.json",
                             "bot/projects.json")
        for path in exported:
            for prefix in excluded_prefixes:
                self.assertFalse(
                    path == prefix.rstrip("/") or path.startswith(prefix),
                    f"Excluded path found in export: {path}",
                )

        # …but the agent personas and commands MUST ship: stage_prompts.py
        # dispatches subagents by the names defined under .claude/agents/, so a
        # mirror without them ships a pipeline whose stages cannot run.
        for path in (".claude/agents/blind-hunter.md",
                     ".claude/commands/team-review.md"):
            self.assertIn(path, exported, f"Required path missing from export: {path}")

        # No *.env* files except *.example
        for path in exported:
            basename = path.rsplit("/", 1)[-1]
            if ".env" in basename:
                self.assertTrue(
                    basename.endswith(".example"),
                    f"Non-example env file in export: {path}",
                )

        # tasks/ checks
        for path in exported:
            if path.startswith("tasks/"):
                is_keep = (
                    path == "tasks/README.md"
                    or path.startswith("tasks/_TEMPLATE/")
                    or path.endswith("/.gitkeep")
                )
                self.assertTrue(is_keep, f"Unexpected tasks/ path in export: {path}")

        # things that SHOULD be in the export
        self.assertIn("README.md", exported)
        self.assertIn("tasks/README.md", exported)
        self.assertIn("tasks/_TEMPLATE/task.md", exported)
        self.assertIn("tasks/active/.gitkeep", exported)
        self.assertIn("bot/.env.example", exported)
        self.assertIn("docs/notes.md", exported)
        # Clean up the kept tmproot
        shutil.rmtree(tmproot, ignore_errors=True)

    def test_dotgithub_is_exported(self) -> None:
        """`.github/` ships (added to PUBLIC_TOPLEVEL 2026-08-31).

        The counterpart to the test below: the fail-closed filter is only worth
        having if adding a directory to the allowlist is a real, verifiable
        decision rather than an edit nobody checked. The workflow file is what
        shows a reader how this project is actually tested.
        """
        (self.src / ".github" / "workflows").mkdir(parents=True)
        (self.src / ".github" / "workflows" / "ci.yml").write_text(
            "name: CI\non: [push]\n"
        )
        _git(self.src, "add", ".github/workflows/ci.yml")
        _git(self.src, "commit", "-m", "add workflow")

        result = _run(self.src, self.mirror, "--keep-tmp")
        self.assertEqual(result.returncode, 0,
                         f"Expected exit 0.\nstdout:{result.stdout}\nstderr:{result.stderr}")

        tmproot = None
        for line in (result.stdout + result.stderr).splitlines():
            if "TMPROOT kept at:" in line:
                tmproot = Path(line.split("TMPROOT kept at:")[1].strip())
                break
        self.assertIsNotNone(tmproot, "could not find TMPROOT in output")

        export = tmproot / "export"
        self.assertTrue(
            (export / ".github" / "workflows" / "ci.yml").is_file(),
            ".github/workflows/ci.yml must be part of the export",
        )
        self.assertNotIn(
            "NEW TOP-LEVEL PATH", result.stdout + result.stderr,
            ".github is on the allowlist; it must not be reported as a new path",
        )
        shutil.rmtree(tmproot, ignore_errors=True)

    def test_ac01_unlisted_toplevel_directory_is_not_exported(self) -> None:
        """A top-level directory absent from PUBLIC_TOPLEVEL never ships.

        The top-level filter used to be a blacklist (EXCLUDE_DIRS), so a
        directory nobody thought to list — notes/, clients/, invoices/ — was
        published by default and neither scan objected: gitleaks and the
        blocklist catch secrets, not internal prose. This asserts the inverted
        rule, and that the drop is announced rather than silent.
        """
        (self.src / "clients").mkdir()
        (self.src / "clients" / "acme-notes.md").write_text(
            "Acme retainer, renewal Q3\n"
        )
        _git(self.src, "add", "clients/acme-notes.md")
        _git(self.src, "commit", "-m", "add client notes")

        result = _run(self.src, self.mirror, "--keep-tmp")
        self.assertEqual(result.returncode, 0,
                         f"Expected exit 0.\nstdout:{result.stdout}\nstderr:{result.stderr}")

        tmproot = None
        for line in result.stderr.splitlines():
            if "TMPROOT kept at:" in line:
                tmproot = line.split("TMPROOT kept at:")[-1].strip()
                break
        self.assertIsNotNone(tmproot, f"TMPROOT not found in stderr:\n{result.stderr}")
        export_dir = Path(tmproot)
        try:
            exported = {
                str(p.relative_to(export_dir / "export"))
                for p in (export_dir / "export").rglob("*")
                if p.is_file() or p.is_symlink()
            }
            self.assertNotIn("clients/acme-notes.md", exported,
                             "Unlisted top-level directory reached the export")
            self.assertFalse(
                any(path.startswith("clients/") for path in exported),
                "Unlisted top-level directory reached the export",
            )
            # The drop must be loud, and must name the path.
            combined = result.stdout + result.stderr
            self.assertIn("NEW TOP-LEVEL PATH", combined,
                          "Unlisted top-level path dropped silently")
            self.assertIn("clients/acme-notes.md", combined,
                          "Dropped path not named in the report")
        finally:
            shutil.rmtree(tmproot, ignore_errors=True)


class TestPreflight(unittest.TestCase):
    """AC-02, AC-05: preflight failures abort before any export."""

    def setUp(self) -> None:
        self.src, self.mirror, self.tmp = _make_fixture()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ac02_invalid_ref_exits_2(self) -> None:
        bad_ref = "nonexistent-branch-xyz-9999"
        result = _run(self.src, self.mirror, "--ref", bad_ref)
        self.assertEqual(result.returncode, 2,
                         f"Expected exit 2 for invalid ref.\nstdout:{result.stdout}\nstderr:{result.stderr}")
        combined = result.stdout + result.stderr
        self.assertIn(bad_ref, combined, "Invalid ref name not mentioned in output")

    def test_ac02_no_tmp_created_on_invalid_ref(self) -> None:
        # List /tmp before and after; no new entry should linger
        before = set(os.listdir("/tmp"))
        result = _run(self.src, self.mirror, "--ref", "bad-ref-xyz")
        self.assertEqual(result.returncode, 2)
        after = set(os.listdir("/tmp"))
        new_entries = after - before
        # Any new /tmp entries should be from the OS, not from our script
        # (script creates TMPROOT only after preflight passes)
        for entry in new_entries:
            self.assertFalse(
                entry.startswith("tmp."),
                f"Script left orphaned tmp dir after preflight failure: {entry}",
            )

    def test_ac05_missing_blocklist_exits_2_and_mentions_example(self) -> None:
        (self.src / "ops" / "publish-blocklist.local").unlink()
        result = _run(self.src, self.mirror)
        self.assertEqual(result.returncode, 2,
                         f"Expected exit 2 for missing blocklist.\nstdout:{result.stdout}\nstderr:{result.stderr}")
        combined = result.stdout + result.stderr
        self.assertIn(".example", combined, "Example file not mentioned in missing-blocklist error")


class TestGate(unittest.TestCase):
    """AC-03, AC-04: gate findings block publish with exit 3."""

    def setUp(self) -> None:
        self.src, self.mirror, self.tmp = _make_fixture()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ac03_gitleaks_detectable_secret_exits_3(self) -> None:
        # Assemble at runtime — never a single contiguous literal in source (E9).
        # _GH_PAT_PREFIX is a module-level variable (not a literal here), so
        # CPython does not constant-fold the concatenation into the pyc bytecode.
        # GitHub PAT format: ghp_ + 36 alphanumeric chars (rule: github-pat).
        secret = _GH_PAT_PREFIX + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        (self.src / "docs" / "leak-token.txt").write_text(f"token = {secret}\n")
        _git(self.src, "add", "docs/leak-token.txt")
        _git(self.src, "commit", "-m", "add token file")
        # Empty blocklist so only gitleaks fires
        (self.src / "ops" / "publish-blocklist.local").write_text("# no patterns\n")

        result = _run(self.src, self.mirror)
        self.assertEqual(result.returncode, 3,
                         f"Expected exit 3 for gitleaks finding.\nstdout:{result.stdout}\nstderr:{result.stderr}")
        combined = result.stdout + result.stderr
        self.assertIn("finding", combined.lower())

    def test_ac04_blocklist_only_pattern_exits_3_with_pattern_and_file(self) -> None:
        # Write a marker that gitleaks would NOT catch on its own
        marker = "INTERNAL-CORP-HOST-marker-9f3a"
        (self.src / "docs" / "internal.md").write_text(f"See {marker} for details\n")
        _git(self.src, "add", "docs/internal.md")
        _git(self.src, "commit", "-m", "add internal ref")
        # Blocklist pattern to detect the marker (escaped for POSIX ERE)
        (self.src / "ops" / "publish-blocklist.local").write_text(
            f"# project blocklist\n{re.escape(marker)}\n"
        )

        result = _run(self.src, self.mirror)
        self.assertEqual(result.returncode, 3,
                         f"Expected exit 3 for blocklist finding.\nstdout:{result.stdout}\nstderr:{result.stderr}")
        combined = result.stdout + result.stderr
        # Both the pattern and the file should be reported
        self.assertIn(re.escape(marker), combined)
        self.assertIn("internal.md", combined)


class TestSourceIntegrity(unittest.TestCase):
    """AC-06: caller's working tree and index are unchanged after any run."""

    def setUp(self) -> None:
        self.src, self.mirror, self.tmp = _make_fixture()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ac06_git_status_identical_before_and_after(self) -> None:
        before_status = _git(self.src, "status", "--porcelain").stdout
        before_head = _git(self.src, "rev-parse", "HEAD").stdout.strip()
        before_branch = _git(self.src, "branch", "--show-current").stdout.strip()

        _run(self.src, self.mirror)  # dry run

        after_status = _git(self.src, "status", "--porcelain").stdout
        after_head = _git(self.src, "rev-parse", "HEAD").stdout.strip()
        after_branch = _git(self.src, "branch", "--show-current").stdout.strip()

        self.assertEqual(before_status, after_status, "git status changed after run")
        self.assertEqual(before_head, after_head, "HEAD changed after run")
        self.assertEqual(before_branch, after_branch, "branch changed after run")


class TestCommitShape(unittest.TestCase):
    """AC-07, AC-08: commit parent and message format."""

    def setUp(self) -> None:
        self.src, self.mirror, self.tmp = _make_fixture()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _do_push(self) -> str:
        """Run --push and return the new mirror tip SHA."""
        before_tip = _mirror_tip(self.mirror)
        result = _run(self.src, self.mirror,
                      "--push", "--push-url", str(self.mirror))
        self.assertEqual(result.returncode, 0,
                         f"Push failed.\nstdout:{result.stdout}\nstderr:{result.stderr}")
        new_tip = _mirror_tip(self.mirror)
        self.assertNotEqual(new_tip, before_tip, "Mirror tip did not advance after push")
        return new_tip

    def test_ac07_commit_parent_is_fetched_public_tip(self) -> None:
        public_tip_before = _mirror_tip(self.mirror)
        new_tip = self._do_push()
        # The new commit's parent must equal what was the public tip before push
        parent = subprocess.run(
            ["git", "rev-parse", f"{new_tip}^"],
            cwd=str(self.mirror), capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(parent, public_tip_before,
                         "New commit's parent != pre-push mirror tip")

    def test_ac07_pre_existing_commits_unchanged(self) -> None:
        commits_before = _mirror_commits(self.mirror)
        self._do_push()
        commits_after = _mirror_commits(self.mirror)
        # All pre-existing commits must still be present with the same SHAs
        for sha in commits_before:
            self.assertIn(sha, commits_after, f"Pre-existing commit {sha} was removed")

    def test_ac08_commit_message_format(self) -> None:
        new_tip = self._do_push()
        msg = subprocess.run(
            ["git", "log", "-1", "--format=%s", new_tip],
            cwd=str(self.mirror), capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertRegex(msg, r"^Release v\d+\.\d+\.\d+: .+",
                         f"Commit message does not match 'Release <version>: <summary>': {msg!r}")

    def test_ac08_no_tag_aborts_before_commit(self) -> None:
        # Create fixture with no tag
        _, _, tmp2 = _make_fixture()
        src2 = tmp2 / "src"
        mirror2 = tmp2 / "mirror.git"
        _git(src2, "tag", "-d", "v1.0.0")  # delete the tag
        result = _run(src2, mirror2)
        shutil.rmtree(tmp2, ignore_errors=True)
        self.assertEqual(result.returncode, 2,
                         f"Expected exit 2 for no-tag case.\nstdout:{result.stdout}\nstderr:{result.stderr}")


class TestDryRun(unittest.TestCase):
    """AC-09: no-flag run prints diffstat + push command, mirror unchanged."""

    def setUp(self) -> None:
        self.src, self.mirror, self.tmp = _make_fixture()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ac09_dry_run_prints_diffstat_and_push_command(self) -> None:
        before_tip = _mirror_tip(self.mirror)
        result = _run(self.src, self.mirror)
        self.assertEqual(result.returncode, 0,
                         f"Dry run failed.\nstdout:{result.stdout}\nstderr:{result.stderr}")
        combined = result.stdout + result.stderr
        # Diffstat
        self.assertIn("DIFF STAT", combined)
        # Push command line
        self.assertIn("git push", combined)
        self.assertIn("refs/heads/main", combined)
        # Mirror is unchanged
        after_tip = _mirror_tip(self.mirror)
        self.assertEqual(before_tip, after_tip, "Mirror tip advanced during dry run")

    def test_ac09_no_network_mutation_in_dry_run(self) -> None:
        commits_before = _mirror_commits(self.mirror)
        _run(self.src, self.mirror)
        commits_after = _mirror_commits(self.mirror)
        self.assertEqual(commits_before, commits_after,
                         "Mirror commits changed during dry run")


class TestPushGating(unittest.TestCase):
    """AC-10: push authorization requirements."""

    def setUp(self) -> None:
        self.src, self.mirror, self.tmp = _make_fixture()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ac10_push_alone_exits_2(self) -> None:
        result = _run(self.src, self.mirror, "--push")
        self.assertEqual(result.returncode, 2,
                         f"Expected exit 2 for --push without URL.\nstdout:{result.stdout}\nstderr:{result.stderr}")

    def test_ac10_push_with_url_creates_exactly_one_commit(self) -> None:
        commits_before = _mirror_commits(self.mirror)
        result = _run(self.src, self.mirror, "--push", "--push-url", str(self.mirror))
        self.assertEqual(result.returncode, 0,
                         f"Push failed.\nstdout:{result.stdout}\nstderr:{result.stderr}")
        commits_after = _mirror_commits(self.mirror)
        self.assertEqual(len(commits_after), len(commits_before) + 1,
                         "Expected exactly one new commit after push")

    def test_ac10_push_with_allow_temp_pushurl(self) -> None:
        # Set the remote's fetch URL to the mirror (test-mirror has no pushurl sentinel)
        # --allow-temp-pushurl derives the push URL from the fetch URL
        result = _run(self.src, self.mirror, "--push", "--allow-temp-pushurl")
        self.assertEqual(result.returncode, 0,
                         f"Push with --allow-temp-pushurl failed.\nstdout:{result.stdout}\nstderr:{result.stderr}")
        # Verify mirror advanced
        commits_after = _mirror_commits(self.mirror)
        self.assertEqual(len(commits_after), 2)

    def test_ac10_no_pushurl_persisted_after_push(self) -> None:
        _run(self.src, self.mirror, "--push", "--push-url", str(self.mirror))
        # Check that no pushurl was written to the fixture's git config
        result = subprocess.run(
            ["git", "config", "--get", f"remote.{FIXTURE_REMOTE}.pushurl"],
            cwd=str(self.src), capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0,
                            "A pushurl was persisted in git config after --push-url run")


class TestIdempotency(unittest.TestCase):
    """AC-11: second run with identical export reports nothing-to-publish."""

    def setUp(self) -> None:
        self.src, self.mirror, self.tmp = _make_fixture()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ac11_second_run_is_nothing_to_publish(self) -> None:
        # First run: push
        r1 = _run(self.src, self.mirror, "--push", "--push-url", str(self.mirror))
        self.assertEqual(r1.returncode, 0, f"First push failed.\n{r1.stdout}\n{r1.stderr}")
        commits_after_first = _mirror_commits(self.mirror)

        # Second run: dry run (should be nothing to publish)
        r2 = _run(self.src, self.mirror)
        self.assertEqual(r2.returncode, 0, f"Second run failed.\n{r2.stdout}\n{r2.stderr}")
        combined = r2.stdout + r2.stderr
        self.assertIn("nothing to publish", combined.lower())

        # Mirror unchanged
        commits_after_second = _mirror_commits(self.mirror)
        self.assertEqual(commits_after_first, commits_after_second,
                         "Mirror changed during second (idempotent) run")

    def test_ac11_nothing_to_publish_with_push_flag(self) -> None:
        # Push once to sync
        _run(self.src, self.mirror, "--push", "--push-url", str(self.mirror))
        commits_before = _mirror_commits(self.mirror)

        # Second run with --push — still nothing to publish
        r2 = _run(self.src, self.mirror, "--push", "--push-url", str(self.mirror))
        self.assertEqual(r2.returncode, 0)
        combined = r2.stdout + r2.stderr
        self.assertIn("nothing to publish", combined.lower())
        self.assertEqual(commits_before, _mirror_commits(self.mirror))


class TestReporting(unittest.TestCase):
    """AC-12: every terminal path reports excluded paths + both verdicts + push status."""

    def setUp(self) -> None:
        self.src, self.mirror, self.tmp = _make_fixture()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _assert_required_output(self, result: subprocess.CompletedProcess,
                                 label: str) -> None:
        combined = result.stdout + result.stderr
        self.assertRegex(combined, r"excluded.*(dir|path)",
                         f"{label}: excluded paths not reported")
        self.assertRegex(combined, r"layer 1.*gitleaks",
                         f"{label}: layer 1 verdict not reported")
        self.assertRegex(combined, r"layer 2.*blocklist",
                         f"{label}: layer 2 verdict not reported")
        # push status — one of these phrases
        push_status_present = any(phrase in combined.lower() for phrase in (
            "push: skipped", "push: completed", "push: not required",
            "dry-run", "nothing to publish",
        ))
        self.assertTrue(push_status_present,
                        f"{label}: push status not reported\ncombined:{combined[:800]}")

    def test_ac12_dry_run_reports_all(self) -> None:
        result = _run(self.src, self.mirror)
        self._assert_required_output(result, "dry-run")

    def test_ac12_push_run_reports_all(self) -> None:
        result = _run(self.src, self.mirror, "--push", "--push-url", str(self.mirror))
        self._assert_required_output(result, "push")

    def test_ac12_gate_failure_reports_all(self) -> None:
        # Cause a gate failure (blocklist hit)
        marker = "CORP-INTERNAL-ID-z9q7"
        (self.src / "docs" / "leak.txt").write_text(f"host: {marker}\n")
        _git(self.src, "add", "docs/leak.txt")
        _git(self.src, "commit", "-m", "add leak")
        (self.src / "ops" / "publish-blocklist.local").write_text(
            f"{re.escape(marker)}\n"
        )
        result = _run(self.src, self.mirror)
        self.assertEqual(result.returncode, 3)
        # Even on failure, excluded paths + both verdicts + push status must appear
        combined = result.stdout + result.stderr
        self.assertRegex(combined, r"excluded.*(dir|path)")
        self.assertRegex(combined, r"layer 2.*blocklist")

    def test_ac12_nothing_to_publish_reports_all(self) -> None:
        # Sync first
        _run(self.src, self.mirror, "--push", "--push-url", str(self.mirror))
        # Second run — nothing to publish
        result = _run(self.src, self.mirror)
        self._assert_required_output(result, "nothing-to-publish")


class TestCrashSafety(unittest.TestCase):
    """AC-15: SIGINT mid-run leaves no orphaned state."""

    def setUp(self) -> None:
        self.src, self.mirror, self.tmp = _make_fixture()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ac15_sigint_leaves_no_orphan(self) -> None:
        env = os.environ.copy()
        env["PUBLISH_REPO_ROOT"] = str(self.src)
        env["PUBLISH_REMOTE"] = FIXTURE_REMOTE

        proc = subprocess.Popen(
            ["bash", str(SCRIPT)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(1.5)  # let it start and create TMPROOT
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=10)

        # No orphaned worktrees in fixture repo
        wt_result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(self.src), capture_output=True, text=True,
        )
        worktrees = [
            line for line in wt_result.stdout.splitlines()
            if line.startswith("worktree ")
        ]
        # Should have exactly one worktree (the main one)
        self.assertEqual(len(worktrees), 1,
                         f"Orphaned worktrees after SIGINT: {wt_result.stdout}")

        # No stray branches (only 'dev' should exist)
        branches_result = subprocess.run(
            ["git", "branch"],
            cwd=str(self.src), capture_output=True, text=True,
        )
        branches = [b.strip().lstrip("* ") for b in branches_result.stdout.splitlines()]
        self.assertEqual(branches, ["dev"],
                         f"Stray branches after SIGINT: {branches}")

        # No lock files in the .git dir
        lock_files = list(self.src.joinpath(".git").rglob("*.lock"))
        self.assertEqual(lock_files, [],
                         f"Lock files left after SIGINT: {lock_files}")

        # Subsequent normal run succeeds
        result = _run(self.src, self.mirror)
        self.assertEqual(result.returncode, 0,
                         f"Post-SIGINT run failed.\nstdout:{result.stdout}\nstderr:{result.stderr}")


class TestSelfCheck(unittest.TestCase):
    """FR-024: --self-check runs offline against a fixture."""

    def test_self_check_exits_0(self) -> None:
        result = subprocess.run(
            ["bash", str(SCRIPT), "--self-check"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0,
                         f"--self-check failed.\nstdout:{result.stdout}\nstderr:{result.stderr}")



class TestRefFreshness(unittest.TestCase):
    """T23: the source ref must be what origin says the branch is.

    The script already refused to trust local state for the destination — it
    re-fetches the mirror tip before building and verifies the tip after
    pushing. These pin the same discipline for the source, which is what public
    commit 19238a3 (a stale tree under an accurate-looking summary) cost us.
    """

    def setUp(self) -> None:
        self.src, self.mirror, self.tmp = _make_fixture()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- the cases that must refuse ------------------------------------------

    def test_behind_origin_exits_2(self) -> None:
        _add_source_origin(self.src, self.tmp)
        _advance_origin(self.src, "origin moved on")

        result = _run(self.src, self.mirror, "--ref", "dev")
        combined = result.stdout + result.stderr
        self.assertEqual(result.returncode, 2,
                         f"A ref behind origin must abort in preflight.\n{combined}")
        self.assertIn("BEHIND", combined)
        self.assertIn("--allow-stale-ref", combined,
                      "The refusal must name the override")

    def test_behind_origin_publishes_nothing(self) -> None:
        """The refusal must land before the export, not after it."""
        _add_source_origin(self.src, self.tmp)
        _advance_origin(self.src, "origin moved on")
        tip_before = _mirror_tip(self.mirror)

        _run(self.src, self.mirror, "--ref", "dev")

        self.assertEqual(_mirror_tip(self.mirror), tip_before,
                         "A stale ref must not reach the mirror")

    def test_ahead_of_origin_exits_2(self) -> None:
        """Unpushed local work must not reach a public repository."""
        _add_source_origin(self.src, self.tmp)
        _commit_locally(self.src, "unpushed local work")

        result = _run(self.src, self.mirror, "--ref", "dev")
        combined = result.stdout + result.stderr
        self.assertEqual(result.returncode, 2,
                         f"A ref ahead of origin must abort.\n{combined}")
        self.assertIn("AHEAD", combined)

    def test_diverged_from_origin_exits_2(self) -> None:
        _add_source_origin(self.src, self.tmp)
        _advance_origin(self.src, "origin moved on")
        _git(self.src, "reset", "-q", "--hard", "HEAD")
        _commit_locally(self.src, "divergent local work")

        result = _run(self.src, self.mirror, "--ref", "dev")
        combined = result.stdout + result.stderr
        self.assertEqual(result.returncode, 2,
                         f"A diverged ref must abort.\n{combined}")
        self.assertIn("DIVERGED", combined)

    def test_unreachable_origin_exits_2(self) -> None:
        """Unknown freshness is not confirmed freshness."""
        _git(self.src, "remote", "add", "origin",
             str(self.tmp / "no-such-repo-xyz.git"))

        result = _run(self.src, self.mirror, "--ref", "dev")
        combined = result.stdout + result.stderr
        self.assertEqual(result.returncode, 2,
                         f"An unreachable origin must abort, not be skipped.\n{combined}")
        self.assertIn("unknown", combined.lower())

    # -- the cases that must proceed -----------------------------------------

    def test_in_sync_reports_freshness_and_proceeds(self) -> None:
        _add_source_origin(self.src, self.tmp)

        result = _run(self.src, self.mirror, "--ref", "dev")
        combined = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0,
                         f"A ref in sync with origin must publish.\n{combined}")
        self.assertIn("freshness", combined,
                      "A passing check must still say it ran")

    def test_no_origin_remote_is_announced_not_silent(self) -> None:
        """No origin is not drift — but the log must not look like a pass."""
        result = _run(self.src, self.mirror, "--ref", "dev")
        combined = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, combined)
        self.assertIn("freshness", combined)
        self.assertIn("no 'origin' remote", combined)

    def test_ref_absent_on_origin_is_announced_not_silent(self) -> None:
        _add_source_origin(self.src, self.tmp)
        _git(self.src, "branch", "local-only", "dev")

        result = _run(self.src, self.mirror, "--ref", "local-only")
        combined = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, combined)
        self.assertIn("does not exist on origin", combined)

    # -- the override --------------------------------------------------------

    def test_allow_stale_ref_overrides_and_warns(self) -> None:
        _add_source_origin(self.src, self.tmp)
        _advance_origin(self.src, "origin moved on")

        result = _run(self.src, self.mirror, "--ref", "dev", "--allow-stale-ref")
        combined = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0,
                         f"--allow-stale-ref must permit the publish.\n{combined}")
        self.assertIn("--allow-stale-ref", combined,
                      "The override must announce itself, never apply silently")

    def test_source_remote_is_not_env_overridable(self) -> None:
        """A silent way to disable the check would defeat the flag's purpose."""
        text = SCRIPT.read_text()
        self.assertIn('readonly SOURCE_REMOTE="origin"', text)
        self.assertNotIn("SOURCE_REMOTE:-", text,
                         "SOURCE_REMOTE must not be env-overridable")


class TestDieMessage(unittest.TestCase):
    """die() printed its exit-code argument as part of the message."""

    def setUp(self) -> None:
        self.src, self.mirror, self.tmp = _make_fixture()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_exit_code_is_not_appended_to_the_message(self) -> None:
        result = _run(self.src, self.mirror, "--ref", "nonexistent-branch-xyz-9999")
        self.assertEqual(result.returncode, 2)
        fatal = [ln for ln in (result.stdout + result.stderr).splitlines()
                 if "FATAL" in ln]
        self.assertTrue(fatal, "expected a FATAL line")
        self.assertFalse(fatal[0].rstrip().endswith(" 2"),
                         f"exit code leaked into the message: {fatal[0]!r}")


if __name__ == "__main__":
    unittest.main()
