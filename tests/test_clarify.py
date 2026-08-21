"""Clarify round-trip helpers.

Pin the 2026-06-01 fix: the BA emits LABELED markers
``**[NEEDS CLARIFICATION: Q1]** <question text trails the marker>`` and the old
extractor captured only the bracket label ("Q1"), so the operator saw
"Q1 / Q2 / Q3" in Telegram instead of the actual questions. The extractor must
handle BOTH the inline format (question inside the brackets) and the labeled
format (question trailing the marker)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import clarify as c  # noqa: E402


class ExtractInlineFormatTests(unittest.TestCase):
    def test_question_inside_brackets(self) -> None:
        text = "- FR-001 [NEEDS CLARIFICATION: which retention policy?]\n"
        self.assertEqual(c.extract_pending_markers(text), ["which retention policy?"])

    def test_dedup_and_cap(self) -> None:
        # multi-word inline questions (bare "q1" would be read as a LABEL, not a
        # question — that ambiguity is by design; see _LABEL_RE).
        qs = ["retention policy?", "oauth provider", "rate limit?",
              "error format", "page size", "extra capped out"]
        order = [qs[0], qs[0], qs[1], qs[2], qs[3], qs[4], qs[5]]
        text = "\n".join(f"FR [NEEDS CLARIFICATION: {q}]" for q in order)
        out = c.extract_pending_markers(text)
        self.assertEqual(out, qs[:5])  # first dup collapsed, capped at MAX_QUESTIONS


class ExtractLabeledFormatTests(unittest.TestCase):
    """The shape the BA actually produced on 2026-06-01."""

    REAL = (
        "## Open Questions\n\n"
        "**[NEEDS CLARIFICATION: Q1]** Should `--dry-run` also suppress the Telethon\n"
        "session authorization step? The current assumption is that auth is required.\n\n"
        "**[NEEDS CLARIFICATION: Q2]** `joiner.py` resolves invite-link targets via\n"
        "`ImportChatInviteRequest`. Show the raw hash or resolve the name?\n\n"
        "**[NEEDS CLARIFICATION: Q3]** The scope covers Python scripts. Is the Java\n"
        "`TelegramClientFacade` in scope for a future phase, or excluded?\n"
    )

    def test_labels_are_not_surfaced(self) -> None:
        out = c.extract_pending_markers(self.REAL)
        self.assertEqual(len(out), 3)
        self.assertNotIn("Q1", out)
        self.assertNotIn("Q2", out)
        self.assertNotIn("Q3", out)

    def test_trailing_text_is_the_question(self) -> None:
        out = c.extract_pending_markers(self.REAL)
        self.assertTrue(out[0].startswith("Should `--dry-run` also suppress"))
        self.assertIn("Telethon", out[0])
        self.assertTrue(out[1].startswith("`joiner.py` resolves"))  # code span balanced
        self.assertIn("TelegramClientFacade", out[2])

    def test_multiline_question_collapsed_to_one_line(self) -> None:
        out = c.extract_pending_markers(self.REAL)
        self.assertNotIn("\n", out[0])

    def test_label_then_no_trailing_falls_back(self) -> None:
        # degenerate: a labeled marker with nothing after it → keep the label
        # rather than dropping the question entirely.
        out = c.extract_pending_markers("done [NEEDS CLARIFICATION: Q1]")
        self.assertEqual(out, ["Q1"])


class ExtractTitledFormatTests(unittest.TestCase):
    """The shape the BA produced on 2026-06-02 (deploy-smoke L run): a descriptive
    HEADING in the brackets with the real question + default trailing it. Earlier
    only the heading leaked to the operator (the title-vs-body bug)."""

    REAL = (
        "**[NEEDS CLARIFICATION: FR-008/FR-009 — Guard implementation shape.]** "
        "The BRD permits two guard shapes: (a) inline null-check, (b) a no-op bean "
        "conditional on telegram.client.enabled=false. Default to (b).\n\n"
        "**[NEEDS CLARIFICATION: Kafka image selection.]** Redpanda is an acceptable "
        "lightweight analog. Confirm Redpanda vs Apache Kafka.\n"
    )

    def test_heading_and_question_both_surfaced(self) -> None:
        out = c.extract_pending_markers(self.REAL)
        self.assertEqual(len(out), 2)
        self.assertIn("Guard implementation shape", out[0])  # the heading kept
        self.assertIn("no-op", out[0])                       # the question body
        self.assertIn("Default to (b)", out[0])              # the BA's default
        self.assertIn("Kafka image selection", out[1])
        self.assertIn("Redpanda", out[1])

    def test_inline_full_question_not_split(self) -> None:
        # a substantive bracket with nothing meaningful trailing stays as-is
        out = c.extract_pending_markers(
            "[NEEDS CLARIFICATION: Should silent channels appear with count=0?]"
        )
        self.assertEqual(out, ["Should silent channels appear with count=0?"])


class ReplyParsingTests(unittest.TestCase):
    def test_enumerated_reply(self) -> None:
        out = c.parse_reply_answers("1. offline\n2) raw hash\n3: excluded", 3)
        self.assertEqual(out, ["offline", "raw hash", "excluded"])

    def test_freeform_lines_padded(self) -> None:
        out = c.parse_reply_answers("offline\nraw hash", 3)
        self.assertEqual(out, ["offline", "raw hash", ""])


if __name__ == "__main__":
    unittest.main()
