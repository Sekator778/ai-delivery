"""BA must re-run on a clarify resume (5th finding, 2026-06-02).

The resume-artifact check skips a stage whose artifact already exists. After a
clarify round-trip the BA artifact (with unresolved markers) is still on disk, so
BA was skipped and the operator's answers in clarifications.md were silently
dropped — the BRD reached the developer unresolved. _ba_needs_clarify_rerun
forces BA to re-run in exactly that situation, and stops doing so once the
markers are resolved (no re-run loop).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import stage_runner_agent as sra  # noqa: E402

_BA_WITH_MARKER = (
    "# BRD\n\n- FR-001 the report exports data\n"
    "[NEEDS CLARIFICATION: which output format, CSV or JSON?]\n"
)
_BA_RESOLVED = "# BRD\n\n- FR-001 the report exports data as CSV and JSON\n"


class BaClarifyRerunTests(unittest.TestCase):
    def _task(self, *, ba: str | None, answered: bool) -> Path:
        d = Path(tempfile.mkdtemp())
        if ba is not None:
            (d / "01-ba-agent.md").write_text(ba)
        if answered:
            (d / "clarifications.md").write_text("# Clarifications\n\n**A1.** JSON\n")
        return d

    def test_rerun_when_answered_and_markers_remain(self) -> None:
        d = self._task(ba=_BA_WITH_MARKER, answered=True)
        self.assertTrue(sra._ba_needs_clarify_rerun(d, "ba"))

    def test_no_rerun_once_markers_resolved(self) -> None:
        # BA already folded the answers in → no markers → skip is correct again.
        d = self._task(ba=_BA_RESOLVED, answered=True)
        self.assertFalse(sra._ba_needs_clarify_rerun(d, "ba"))

    def test_no_rerun_without_answers(self) -> None:
        # markers present but operator hasn't answered → normal pause path, not a
        # resume re-run.
        d = self._task(ba=_BA_WITH_MARKER, answered=False)
        self.assertFalse(sra._ba_needs_clarify_rerun(d, "ba"))

    def test_only_applies_to_ba(self) -> None:
        d = self._task(ba=_BA_WITH_MARKER, answered=True)
        self.assertFalse(sra._ba_needs_clarify_rerun(d, "developer"))


if __name__ == "__main__":
    unittest.main()
