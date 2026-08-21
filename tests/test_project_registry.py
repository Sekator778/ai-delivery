"""Project registry parsing + per-target base-branch resolution (#6).

The first self-targeted run was cut from the target repo's DEFAULT branch —
'master', a June snapshot — while development lived on 'dev'. The fix is a
per-target `base` in the registry plus an honest fallback chain. Both are pure
functions, so they are pinned here directly.
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

import project_registry as pr  # noqa: E402
import target_policy as tp  # noqa: E402


def _write_registry(payload: dict) -> Path:
    path = Path(tempfile.mkdtemp()) / "projects.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class ParseTests(unittest.TestCase):
    """Both entry shapes, side by side — an existing registry keeps working."""

    RAW = {
        "_default": "sandbox",
        "projects": {
            "sandbox": "/repos/ai-delivery-sandbox",          # legacy string
            "ai-delivery": {"path": "/repos/ai-delivery", "base": "dev"},
            "userbot": {"path": "/repos/telegram-userbot-ai"},  # dict, no base
        },
    }

    def test_legacy_string_entry(self) -> None:
        entries = pr.parse_projects(self.RAW)
        self.assertEqual(entries["sandbox"].path, "/repos/ai-delivery-sandbox")
        self.assertIsNone(entries["sandbox"].base)

    def test_extended_entry_carries_base(self) -> None:
        entries = pr.parse_projects(self.RAW)
        self.assertEqual(entries["ai-delivery"].path, "/repos/ai-delivery")
        self.assertEqual(entries["ai-delivery"].base, "dev")

    def test_extended_entry_without_base(self) -> None:
        self.assertIsNone(pr.parse_projects(self.RAW)["userbot"].base)

    def test_project_paths_is_the_legacy_shape(self) -> None:
        # bot.py consumed alias -> path strings before the extended form; both
        # entry shapes must still collapse to exactly that.
        self.assertEqual(pr.project_paths(self.RAW), {
            "sandbox": "/repos/ai-delivery-sandbox",
            "ai-delivery": "/repos/ai-delivery",
            "userbot": "/repos/telegram-userbot-ai",
        })

    def test_default_alias(self) -> None:
        self.assertEqual(pr.default_alias(self.RAW), "sandbox")
        self.assertIsNone(pr.default_alias({"projects": {}}))

    def test_malformed_entries_are_dropped_not_raised(self) -> None:
        # One bad line must never take the whole registry (and every target)
        # offline.
        entries = pr.parse_projects({"projects": {
            "ok": "/repos/ok", "empty": "", "nulled": None,
            "listy": ["/repos/x"], "nopath": {"base": "dev"},
        }})
        self.assertEqual(list(entries), ["ok"])

    def test_garbage_registry_is_empty(self) -> None:
        for raw in (None, [], "text", {"projects": "notadict"}):
            self.assertEqual(pr.parse_projects(raw), {})

    def test_load_registry_missing_or_broken_file(self) -> None:
        self.assertEqual(pr.load_registry(Path("/nope/projects.json")), {})
        broken = Path(tempfile.mkdtemp()) / "projects.json"
        broken.write_text("{not json", encoding="utf-8")
        self.assertEqual(pr.load_registry(broken), {})

    def test_base_for_path_normalizes_both_sides(self) -> None:
        raw = {"projects": {"a": {"path": "/repos/ai-delivery/", "base": "dev"}}}
        self.assertEqual(pr.base_for_path("/repos/ai-delivery", raw), "dev")
        self.assertIsNone(pr.base_for_path("/repos/other", raw))


class BaseBranchResolutionTests(unittest.TestCase):
    """Resolution order: per-target `base` -> PIPELINE_BASE_BRANCH -> the target
    repo's own origin default branch -> 'main'."""

    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k)
                       for k in ("PIPELINE_BASE_BRANCH", "PROJECTS_REGISTRY")}
        os.environ.pop("PIPELINE_BASE_BRANCH", None)
        os.environ.pop("PROJECTS_REGISTRY", None)

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _repo_with_origin_head(self, default_branch: str) -> Path:
        """A clone whose origin/HEAD points at `default_branch` — the shape the
        `git symbolic-ref` fallback reads."""
        root = Path(tempfile.mkdtemp())
        origin, work = root / "origin.git", root / "work"
        subprocess.run(["git", "init", "--bare", "-b", default_branch, str(origin)],
                       capture_output=True, check=True)
        subprocess.run(["git", "clone", str(origin), str(work)], capture_output=True)
        for args in (["config", "user.email", "t@t.io"], ["config", "user.name", "t"]):
            subprocess.run(["git", *args], cwd=str(work), capture_output=True)
        (work / "f.txt").write_text("x\n")
        subprocess.run(["git", "add", "-A"], cwd=str(work), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(work), capture_output=True)
        subprocess.run(["git", "push", "origin", default_branch],
                       cwd=str(work), capture_output=True)
        subprocess.run(["git", "remote", "set-head", "origin", "-a"],
                       cwd=str(work), capture_output=True)
        return work

    def test_per_target_base_wins_over_env_and_origin(self) -> None:
        repo = self._repo_with_origin_head("master")
        os.environ["PIPELINE_BASE_BRANCH"] = "release"
        os.environ["PROJECTS_REGISTRY"] = str(_write_registry(
            {"projects": {"t": {"path": str(repo), "base": "dev"}}}))
        self.assertEqual(tp._base_branch(repo), "dev")

    def test_env_wins_when_target_pins_no_base(self) -> None:
        repo = self._repo_with_origin_head("master")
        os.environ["PIPELINE_BASE_BRANCH"] = "release"
        os.environ["PROJECTS_REGISTRY"] = str(_write_registry(
            {"projects": {"t": str(repo)}}))
        self.assertEqual(tp._base_branch(repo), "release")

    def test_origin_default_branch_when_nothing_pinned(self) -> None:
        # The registry-less deployment must follow the REMOTE's default branch
        # instead of assuming 'main'.
        repo = self._repo_with_origin_head("master")
        os.environ["PROJECTS_REGISTRY"] = str(_write_registry({"projects": {}}))
        self.assertEqual(tp._origin_default_branch(repo), "master")
        self.assertEqual(tp._base_branch(repo), "master")

    def test_falls_back_to_main(self) -> None:
        os.environ["PROJECTS_REGISTRY"] = str(_write_registry({"projects": {}}))
        plain = Path(tempfile.mkdtemp())          # not a git repo at all
        self.assertIsNone(tp._origin_default_branch(plain))
        self.assertEqual(tp._base_branch(plain), "main")
        self.assertEqual(tp._base_branch(), "main")   # legacy no-arg call site

    def test_worktree_resolves_via_its_main_checkout(self) -> None:
        # Stages are handed the ephemeral worktree path; the registry entry
        # (and origin/HEAD) belong to the real checkout.
        repo = self._repo_with_origin_head("master")
        wt = Path(tempfile.mkdtemp()) / "wt"
        subprocess.run(["git", "worktree", "add", str(wt), "-b", "feat/x"],
                       cwd=str(repo), capture_output=True)
        os.environ["PROJECTS_REGISTRY"] = str(_write_registry(
            {"projects": {"t": {"path": str(repo), "base": "dev"}}}))
        self.assertEqual(tp._main_repo(wt).resolve(), repo.resolve())
        self.assertEqual(tp._base_branch(wt), "dev")


class MainRepoTests(unittest.TestCase):
    def test_plain_checkout_is_returned_unchanged(self) -> None:
        d = Path(tempfile.mkdtemp())
        self.assertEqual(tp._main_repo(d), d)
        (d / ".git").mkdir()
        self.assertEqual(tp._main_repo(d), d)

    def test_garbage_git_file_falls_back_to_the_path(self) -> None:
        d = Path(tempfile.mkdtemp())
        (d / ".git").write_text("not a gitdir pointer")
        self.assertEqual(tp._main_repo(d), d)


class BranchSafetyExpectedTests(unittest.TestCase):
    """With a pre-created worktree the runner knows the ONE allowed branch, so
    the shape check becomes an identity check."""

    def test_expected_branch_is_an_identity_check(self) -> None:
        self.assertTrue(tp._branch_safety_ok("feat/t-1", False, "feat/t-1"))
        self.assertTrue(tp._branch_safety_ok("origin/feat/t-1", False, "feat/t-1"))
        # correctly shaped, but NOT the branch the worktree was cut on
        self.assertFalse(tp._branch_safety_ok("feat/other", False, "feat/t-1"))
        self.assertFalse(tp._branch_safety_ok("main", False, "feat/t-1"))
        self.assertFalse(tp._branch_safety_ok("", False, "feat/t-1"))

    def test_prefix_rules_stand_without_an_expectation(self) -> None:
        self.assertTrue(tp._branch_safety_ok("feat/t-1", False))
        self.assertFalse(tp._branch_safety_ok("main", False))
        self.assertTrue(tp._branch_safety_ok("phase-b4-poc-20260812-0834", True))


if __name__ == "__main__":
    unittest.main()
