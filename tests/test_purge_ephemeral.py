"""Tests for `qdrant-memory.py purge-ephemeral` (backlog/T20).

The store accumulated `task_lesson` points written for `$TMPDIR` fixtures by
runner-level tests, before the T02 write-back guard existed. They dilute every
recall: the scoped half of `recall()` filters by `target_repo`, and these are
targets that no longer exist.

The load-bearing choice under test is what counts as garbage. Selection is by
`target_repo` ONLY. Widening it to "mentions a temp path anywhere" also catches
real session summaries that quote a path in passing — six of them in the
2026-08-28 export, including operator hand-off notes. Those are content.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "qdrant-memory.py"

MACOS_TMP = "/var/folders/nt/0tkgtyt96_v12yx82b79b5bw0000gn/T/tmpwcn_cr87/repo"


def _rec(rid: str, target: str, text: str, source: str = "pipeline_writeback") -> dict:
    return {"id": rid,
            "payload": {"kind": "task_lesson", "source": source,
                        "target_repo": target, "text": text}}


class PurgeEphemeralTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.store = self.tmp / "store.jsonl"
        self.records = [
            _rec("keep-1", "/Users/op/projects/ai-delivery", "real lesson, real repo"),
            _rec("drop-1", MACOS_TMP, f"Task task on {MACOS_TMP} finished"),
            _rec("drop-2", "/tmp/tmpabc/repo", "Task task on /tmp/tmpabc/repo finished"),
            # Mentions a temp path in its text but is NOT scoped to one: a real
            # session summary. Must survive.
            _rec("keep-2", "/Users/op/projects/ai-delivery",
                 f"worktree was at {MACOS_TMP} during the run; the fix is in git_pr.py",
                 source="session_stop"),
            # No target_repo at all.
            {"id": "keep-3", "payload": {"source": "session_stop", "text": "no target"}},
        ]
        self.store.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in self.records) + "\n")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "purge-ephemeral", str(self.store), *args],
            capture_output=True, text=True, cwd=str(REPO_ROOT))

    def _ids(self) -> list[str]:
        return [json.loads(l)["id"] for l in self.store.read_text().splitlines() if l.strip()]

    def test_dry_run_reports_but_writes_nothing(self) -> None:
        before = self.store.read_text()
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("2 ephemeral", r.stdout + r.stderr)
        self.assertIn("dry run", r.stdout + r.stderr)
        self.assertEqual(self.store.read_text(), before, "dry run modified the file")

    def test_yes_removes_only_ephemeral_targets(self) -> None:
        r = self._run("--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(self._ids()), ["keep-1", "keep-2", "keep-3"])

    def test_a_summary_that_merely_quotes_a_temp_path_survives(self) -> None:
        """The distinction the whole task turns on."""
        self._run("--yes")
        kept = {json.loads(l)["id"]: json.loads(l) for l in
                self.store.read_text().splitlines() if l.strip()}
        self.assertIn("keep-2", kept, "deleted a session summary for quoting a path")
        self.assertIn(MACOS_TMP, kept["keep-2"]["payload"]["text"])

    def test_second_run_is_a_no_op(self) -> None:
        self._run("--yes")
        r = self._run()
        self.assertIn("0 ephemeral", r.stdout + r.stderr)

    def test_missing_file_is_skipped_not_fatal(self) -> None:
        """The vectors file is gitignored — absent on any machine but the host."""
        missing = self.tmp / "absent.jsonl"
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "purge-ephemeral", str(missing)],
            capture_output=True, text=True, cwd=str(REPO_ROOT))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("not present", r.stdout + r.stderr)

    def test_uses_the_same_predicate_as_write_back(self) -> None:
        """Two definitions of 'ephemeral' drifting apart is what caused this."""
        sys.path.insert(0, str(REPO_ROOT / "dispatcher"))
        from memory_inject import _is_ephemeral_target
        self.assertTrue(_is_ephemeral_target(MACOS_TMP))
        self.assertFalse(_is_ephemeral_target("/Users/op/projects/ai-delivery"))


if __name__ == "__main__":
    unittest.main()
