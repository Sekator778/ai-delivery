"""Operator-facing review surfacing — batch from the 2026-06-02 M-validation.

Three small fixes that all surfaced while validating the convergence work:
  #6a — the clarify prompt promised an «Использовать defaults» button the bot
        never renders; the footer must instead tell the operator to reply.
  #7  — when the pipeline parks at awaiting-input with unresolved criticals, the
        findings must be surfaced (a PR comment) instead of stranded in the task
        dir; _build_unresolved_findings_comment assembles that body.
  hint — the reviewer's 'what NOT to flag as Critical' calibration existed only
        for S; extend a (gentler) form to M so it stops elevating test-DRY to
        Critical and looping (the [2,2] non-convergence we observed live).
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import stage_runner_agent as sra  # noqa: E402
import clarify as c  # noqa: E402


class ClarifyFooterTests(unittest.TestCase):
    def test_footer_drops_fake_button_and_tells_operator_to_reply(self) -> None:
        out = c.format_question_prompt("tg-x", ["which format, CSV or JSON?",
                                                "include the @ prefix?"])
        self.assertNotIn("Использовать defaults", out)   # the button that never existed
        self.assertNotIn("Отмена", out)
        self.assertIn("Кнопок нет", out)
        self.assertIn("reply", out.lower())
        # questions still rendered
        self.assertIn("1. which format, CSV or JSON?", out)


class ReviewerTriageHintTests(unittest.TestCase):
    def setUp(self) -> None:
        self._acting = sra._triage_acting

    def tearDown(self) -> None:
        sra._triage_acting = self._acting

    def test_m_hint_when_acting(self) -> None:
        sra._triage_acting = lambda *a, **k: True
        out = sra._reviewer_triage_hint({"triage": {"tier": "M"}})
        self.assertIn("tier M", out)
        self.assertIn("NEVER Critical", out)
        self.assertIn("re-flag", out)            # the anti-perpetual-criticals clause

    def test_s_hint_still_returned(self) -> None:
        sra._triage_acting = lambda *a, **k: True
        self.assertIn("TRIVIAL", sra._reviewer_triage_hint({"triage": {"tier": "S"}}))

    def test_l_tier_and_non_acting_are_empty(self) -> None:
        sra._triage_acting = lambda *a, **k: True
        self.assertEqual(sra._reviewer_triage_hint({"triage": {"tier": "L"}}), "")
        sra._triage_acting = lambda *a, **k: False
        self.assertEqual(sra._reviewer_triage_hint({"triage": {"tier": "M"}}), "")


class UnresolvedFindingsCommentTests(unittest.TestCase):
    def _task(self, tmp: str, *, with_md: bool) -> Path:
        d = Path(tmp)
        (d / "06-review-agent.json").write_text(json.dumps({
            "verdict": {"verdict": "request_changes", "critical": 2,
                        "warning": 5, "suggestion": 7}}))
        if with_md:
            (d / "06-review.md").write_text(
                "# Review\n\n## Critical Issues\n\n"
                "### CRITICAL-1: RealDictRow construction is wrong\n\nbody...\n")
        return d

    def test_body_has_counts_header_and_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = self._task(tmp, with_md=True)
            body = sra._build_unresolved_findings_comment(d, "tg-x")
            self.assertIn("parked for operator decision", body)
            self.assertIn("2 Critical", body)
            self.assertIn("5 warning", body)
            self.assertIn("CRITICAL-1", body)      # the findings section is included

    def test_body_falls_back_without_review_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = self._task(tmp, with_md=False)
            body = sra._build_unresolved_findings_comment(d, "tg-x")
            self.assertIn("2 Critical", body)
            self.assertIn("06-review.md", body)    # fallback pointer


if __name__ == "__main__":
    unittest.main()
