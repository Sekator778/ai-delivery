"""Regression tests for the Telegram-bound formatting pipeline.

Background — the long-standing "literal \\n in APPROVE message" bug:

When the Developer stage emits ``PR_URL: <url>\nTESTS: ...`` inside a
stream-json event, the agent's newline gets JSON-encoded as the two literal
characters ``\\`` + ``n`` inside the JSON string. ``stage_runner._run_claude``
concatenates those event lines verbatim into ``full_output``, so any regex
that swallows non-whitespace will happily eat the backslash-n and trail into
the next token (typically ``TESTS:``). That corrupted ``pr_url`` then flows
through ``approve_summary`` into Telegram, where users see
``https://…/pull/1\\nTESTS:`` rendered literally.

The fix lives in ``_extract_pr_url`` — exclude backslash from the captured
character class. These tests pin that behavior and verify that the downstream
summary builder produces real newlines (1-byte ``\\n``) rather than the 2-char
escape sequence.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

from stage_runner_agent import _extract_pr_url  # noqa: E402
from auto_loop import (  # noqa: E402
    ReviewerVerdict,
    approve_summary,
)


class ExtractPrUrlTests(unittest.TestCase):
    """The regex in ``_extract_pr_url`` must stop at a literal backslash so
    JSON-escaped newlines from stream-json output cannot leak into the PR URL.
    """

    def test_clean_pr_url(self) -> None:
        out = "DEV_COMPLETE: done\nPR_URL: https://github.com/x/y/pull/1\nTESTS: 5/5\n"
        self.assertEqual(
            _extract_pr_url(out), "https://github.com/x/y/pull/1"
        )

    def test_strips_json_escaped_newline(self) -> None:
        # Reproduce the actual bug: stream-json output where the agent's
        # \n was JSON-encoded as the two literal chars '\\' + 'n'. This is
        # what stage_runner sees after concatenating event lines.
        json_event = (
            '{"type":"assistant","message":{"content":[{"type":"text",'
            '"text":"DEV_COMPLETE: feature shipped\\n'
            'PR_URL: https://github.com/Sekator778/ai-delivery-sandbox/pull/1\\n'
            'TESTS: 3/3"}]}}'
        )
        url = _extract_pr_url(json_event)
        self.assertEqual(
            url, "https://github.com/Sekator778/ai-delivery-sandbox/pull/1"
        )
        # The corrupted-extraction symptom: leaking '\\n' or 'TESTS:' into
        # the captured URL. Pin against both.
        self.assertNotIn("\\", url or "")
        self.assertNotIn("TESTS", url or "")
        self.assertNotIn("\\n", url or "")

    def test_no_pr_returns_none(self) -> None:
        self.assertIsNone(_extract_pr_url("DEV_COMPLETE: nothing to ship\n"))

    def test_handles_trailing_whitespace(self) -> None:
        out = "PR_URL: https://example.com/pr/9   \nother junk\n"
        self.assertEqual(_extract_pr_url(out), "https://example.com/pr/9")


class ApproveSummaryTests(unittest.TestCase):
    """``approve_summary`` must embed real Python-string newlines (1 byte
    each) — Telegram renders these as line breaks. A literal ``\\n`` (2-char
    escape) would surface to the user as visible junk text.
    """

    def test_contains_real_newlines(self) -> None:
        verdict = ReviewerVerdict("approve", critical=0, warning=0, suggestion=0)
        msg = approve_summary(
            "https://github.com/x/y/pull/1",
            iterations=1,
            verdict=verdict,
            elapsed_sec=720,
        )
        # Real newline (single byte): present.
        self.assertIn("\n", msg)
        # Literal 2-char escape: absent.
        self.assertNotIn("\\n", msg)

    def test_each_section_on_its_own_line(self) -> None:
        verdict = ReviewerVerdict("approve", critical=0, warning=2, suggestion=5)
        msg = approve_summary(
            "https://github.com/x/y/pull/42",
            iterations=2,
            verdict=verdict,
            elapsed_sec=900,
        )
        lines = msg.split("\n")
        # Expect 4 lines: header+PR, iterations, findings, time
        self.assertEqual(len(lines), 4, f"expected 4 lines, got {lines!r}")
        self.assertIn("PR: https://github.com/x/y/pull/42", lines[0])
        self.assertTrue(lines[1].startswith("Итераций:"))
        self.assertTrue(lines[2].startswith("Findings:"))
        self.assertTrue(lines[3].startswith("Время:"))

    def test_end_to_end_pipeline_no_literal_escape(self) -> None:
        """Simulate the full path: stream-json output → _extract_pr_url →
        approve_summary. The user-visible string must never contain ``\\n``."""
        stream_output = (
            'PR_URL: https://github.com/Sekator778/ai-delivery-sandbox/pull/1\\n'
            'TESTS: 3/3'
        )
        url = _extract_pr_url(stream_output)
        verdict = ReviewerVerdict("approve", critical=0, warning=0, suggestion=0)
        msg = approve_summary(url or "N/A", iterations=1, verdict=verdict, elapsed_sec=720)
        self.assertNotIn("\\n", msg)
        self.assertNotIn("TESTS:", msg)
        self.assertIn("pull/1", msg)


class ReportDigestTests(unittest.TestCase):
    """Notifications must inline report CONTENT, not a server-local path the
    remote operator cannot open from Telegram (observed 2026-06-05/06 — INVEST
    'see 01-ba-invest.md' and handoff 'see UNRESOLVED-FINDINGS.md' were dead ends)."""

    def test_short_body_passes_through(self) -> None:
        from stage_runner_agent import _report_digest
        self.assertEqual(_report_digest("4 violation(s): weak modal at line 135"),
                         "4 violation(s): weak modal at line 135")

    def test_long_body_truncated_and_flagged(self) -> None:
        from stage_runner_agent import _report_digest
        out = _report_digest("x" * 5000, cap=1600)
        self.assertLess(len(out), 1700)
        self.assertIn("truncated", out)

    def test_empty_and_none(self) -> None:
        from stage_runner_agent import _report_digest
        self.assertEqual(_report_digest(""), "")
        self.assertEqual(_report_digest(None), "")


if __name__ == "__main__":
    unittest.main()
