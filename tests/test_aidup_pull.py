"""Tests for `aidstack.sh pull` — the update step inside starting the stand.

There is no scheduler and no background process: starting the stand is the
deploy trigger. `aidup` takes the newest commit of the branch it is on, and if
anything at all is in the way it starts what is already checked out.

That last part is the whole contract, so most of these tests assert the same
thing from different angles: **the step never fails**. A stand that refuses to
start because git had an opinion is worse than a stand running last week's
code — and unlike an unattended loop, a human is right there reading the
output.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "ops" / "atlas" / "aidstack.sh"


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=check)


class AidupPullTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        self.work = self.tmp / "work"

        subprocess.run(["git", "init", "--bare", "-b", "dev", str(self.origin)],
                       capture_output=True, check=True)
        subprocess.run(["git", "init", "-b", "dev", str(self.work)],
                       capture_output=True, check=True)
        _git(self.work, "config", "user.email", "fixture@test.io")
        _git(self.work, "config", "user.name", "fixture")
        # aidstack.sh resolves REPO_ROOT two levels up from itself.
        (self.work / "ops" / "atlas").mkdir(parents=True)
        shutil.copy(SCRIPT, self.work / "ops" / "atlas" / "aidstack.sh")
        (self.work / "README.md").write_text("v1\n")
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-m", "init")
        _git(self.work, "remote", "add", "origin", str(self.origin))
        _git(self.work, "push", "-u", "origin", "dev")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # NB: aidstack.sh's warn() prints to stdout, not stderr — that is the
    # file's own convention, so the assertions follow it rather than changing it.
    def _pull(self, **env_extra: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env.update(env_extra)
        return subprocess.run(
            ["bash", str(self.work / "ops" / "atlas" / "aidstack.sh"), "pull"],
            cwd=str(self.work), env=env, capture_output=True, text=True)

    def _advance_origin(self, text: str = "v2\n") -> str:
        other = self.tmp / "other"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(other)],
                       capture_output=True, check=True)
        _git(other, "config", "user.email", "other@test.io")
        _git(other, "config", "user.name", "other")
        (other / "README.md").write_text(text)
        _git(other, "add", "-A")
        _git(other, "commit", "-m", "advance")
        _git(other, "push", "origin", "dev")
        sha = _git(other, "rev-parse", "HEAD").stdout.strip()
        shutil.rmtree(other)
        return sha

    def _head(self) -> str:
        return _git(self.work, "rev-parse", "HEAD").stdout.strip()

    # -- the happy path ------------------------------------------------------
    def test_takes_the_new_version_when_there_is_one(self) -> None:
        sha = self._advance_origin()
        r = self._pull()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._head(), sha, "did not take the new commit")
        self.assertIn("new commit", r.stdout)

    def test_nothing_new_is_a_quiet_no_op(self) -> None:
        before = self._head()
        r = self._pull()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._head(), before)
        self.assertIn("nothing new", r.stdout)

    # -- every obstacle starts what is checked out, and none of them fail ----
    def test_dirty_tree_starts_what_is_checked_out(self) -> None:
        self._advance_origin()
        before = self._head()
        (self.work / "README.md").write_text("local edit\n")
        r = self._pull()
        self.assertEqual(r.returncode, 0, "an update problem must not fail the start")
        self.assertEqual(self._head(), before, "clobbered local changes")
        self.assertIn("uncommitted changes", r.stdout)

    def test_local_ahead_starts_what_is_checked_out(self) -> None:
        (self.work / "README.md").write_text("unpushed\n")
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-m", "local only")
        before = self._head()
        r = self._pull()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self._head(), before)
        self.assertIn("ahead of origin", r.stdout)

    def test_diverged_starts_what_is_checked_out(self) -> None:
        self._advance_origin("remote side\n")
        (self.work / "README.md").write_text("local side\n")
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-m", "local divergence")
        before = self._head()
        r = self._pull()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self._head(), before)
        self.assertIn("diverged", r.stdout)

    def test_detached_head_starts_what_is_checked_out(self) -> None:
        _git(self.work, "checkout", "-q", "--detach")
        before = self._head()
        r = self._pull()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self._head(), before)
        self.assertIn("detached HEAD", r.stdout)

    def test_unreachable_remote_starts_what_is_checked_out(self) -> None:
        """Offline is the common case for a laptop, and must be harmless."""
        _git(self.work, "remote", "set-url", "origin",
             str(self.tmp / "does-not-exist.git"))
        before = self._head()
        r = self._pull()
        self.assertEqual(r.returncode, 0, "offline must not fail the start")
        self.assertEqual(self._head(), before)
        self.assertIn("fetch failed", r.stdout)

    def test_follows_whatever_branch_is_checked_out(self) -> None:
        """No branch name configured anywhere: it follows the checkout."""
        _git(self.work, "checkout", "-q", "-b", "release")
        _git(self.work, "push", "-q", "-u", "origin", "release")
        other = self.tmp / "other2"
        subprocess.run(["git", "clone", "-q", "-b", "release", str(self.origin), str(other)],
                       capture_output=True, check=True)
        _git(other, "config", "user.email", "o@test.io")
        _git(other, "config", "user.name", "o")
        (other / "README.md").write_text("release moved\n")
        _git(other, "add", "-A")
        _git(other, "commit", "-m", "release advance")
        _git(other, "push", "origin", "release")
        sha = _git(other, "rev-parse", "HEAD").stdout.strip()
        r = self._pull()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._head(), sha, "did not follow the checked-out branch")

    def test_opt_out(self) -> None:
        self._advance_origin()
        before = self._head()
        r = self._pull(AIDUP_PULL="0")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self._head(), before)
        self.assertIn("skipped", r.stdout)


if __name__ == "__main__":
    unittest.main()
