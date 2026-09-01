"""Tests for strategy memory (T26).

Structured around the ReasoningBank code study rather than around the paper:
the parts pinned here are the ones the study found only by reading the code —
the retrieval insight, the judge's strictness rules, and the fragilities in
the Observations section that the brief said not to copy.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import strategy_memory as sm  # noqa: E402


ITEM_BLOCK = """\
# Memory Item 1
## Title
Read the migration chain first
## Description
Use when a schema change touches more than one table; not for additive columns.
## Content
Read the existing migration chain before writing a new one.
"""


class FlagTests(unittest.TestCase):
    def test_off_by_default(self) -> None:
        self.assertFalse(sm.strategy_enabled({}))

    def test_only_explicit_truthy_enables(self) -> None:
        for v in ("1", "true", "yes", "on"):
            self.assertTrue(sm.strategy_enabled({"MEMORY_STRATEGY_ENABLED": v}), v)
        for v in ("", "0", "no", "off", "later"):
            self.assertFalse(sm.strategy_enabled({"MEMORY_STRATEGY_ENABLED": v}), v)


class VerdictFromSignalTests(unittest.TestCase):
    """The gt path: when the pipeline already knows, do not ask a model."""

    def test_approve_is_success(self) -> None:
        v = sm.verdict_from_signal("approve")
        self.assertEqual((v.status, v.source), ("success", "gt"))

    def test_budget_stop_is_failure(self) -> None:
        v = sm.verdict_from_signal("budget_stop")
        self.assertEqual((v.status, v.source), ("fail", "gt"))

    def test_an_unrecognised_reason_defers_to_the_judge(self) -> None:
        self.assertIsNone(sm.verdict_from_signal("something new"))

    def test_a_merged_pr_with_no_contrary_reason_is_success(self) -> None:
        v = sm.verdict_from_signal("", {"pr_url": "https://example/pr/1"})
        self.assertEqual(v.status, "success")


class JudgeParsingTests(unittest.TestCase):
    """The reference's SWE judge used a substring test. That is the bug."""

    def test_success_is_parsed(self) -> None:
        v = sm.parse_judge_verdict("Thoughts: all three checks hold\nStatus: success")
        self.assertEqual((v.status, v.source), ("success", "judge"))
        self.assertIn("three checks", v.thoughts)

    def test_failure_is_parsed(self) -> None:
        self.assertEqual(sm.parse_judge_verdict("Thoughts: x\nStatus: failure").status, "fail")

    def test_a_negated_success_in_prose_is_not_a_success(self) -> None:
        """`if "success" in reply.lower()` would call this a success."""
        reply = "Thoughts: this was not a success, the agent guessed.\nStatus: failure"
        self.assertEqual(sm.parse_judge_verdict(reply).status, "fail")

    def test_prose_without_a_status_line_is_no_verdict(self) -> None:
        self.assertIsNone(sm.parse_judge_verdict("It seems successful to me."))

    def test_empty_reply_is_no_verdict(self) -> None:
        self.assertIsNone(sm.parse_judge_verdict(""))

    def test_the_judge_prompt_carries_the_strictness_rules(self) -> None:
        """Found in the code, not the paper — and the reason for the care."""
        prompt = sm.JUDGE_SYSTEM_PROMPT
        for rule in ("Completeness", "Grounding", "Right target"):
            self.assertIn(rule, prompt)
        self.assertIn("false success is more harmful", prompt)


class ExtractionBranchTests(unittest.TestCase):
    """Two branches. Learning from failures is the core of the delta."""

    def test_success_branch_asks_why_it_worked(self) -> None:
        prompt = sm.extraction_prompt(sm.Verdict("success", "gt"))
        self.assertIn("succeeded", prompt)
        self.assertNotIn("recovery", prompt)

    def test_failure_branch_asks_for_recovery_procedures(self) -> None:
        prompt = sm.extraction_prompt(sm.Verdict("fail", "gt"))
        self.assertIn("failed", prompt)
        self.assertIn("recovery procedures", prompt)

    def test_both_branches_cap_at_three_items(self) -> None:
        for verdict in (sm.Verdict("success", "gt"), sm.Verdict("fail", "gt")):
            self.assertIn("at most 3 memory items", sm.extraction_prompt(verdict))

    def test_both_branches_forbid_task_specifics(self) -> None:
        for verdict in (sm.Verdict("success", "gt"), sm.Verdict("fail", "gt")):
            self.assertIn("literal strings from this task",
                          sm.extraction_prompt(verdict))

    def test_judge_thoughts_are_appended_to_the_trajectory(self) -> None:
        verdict = sm.Verdict("fail", "judge", "it never checked the second page")
        text = sm.build_extraction_input("step 1\nstep 2", verdict)
        self.assertIn("step 2", text)
        self.assertIn("failed because: it never checked the second page", text)

    def test_a_gt_verdict_adds_no_invented_rationale(self) -> None:
        verdict = sm.Verdict("success", "gt")
        self.assertEqual(sm.build_extraction_input("step 1", verdict), "step 1")


class ItemParsingTests(unittest.TestCase):
    """The reference never parsed these fields. Here a malformed item is dropped."""

    def test_a_well_formed_item_parses_into_fields(self) -> None:
        items = sm.parse_memory_items(ITEM_BLOCK)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Read the migration chain first")
        self.assertIn("more than one table", items[0].description)
        self.assertIn("migration chain", items[0].content)

    def test_surrounding_prose_is_ignored(self) -> None:
        noisy = "Here are my thoughts.\n\n" + ITEM_BLOCK + "\nHope that helps!"
        self.assertEqual(len(sm.parse_memory_items(noisy)), 1)

    def test_an_item_missing_a_field_is_dropped_not_stored(self) -> None:
        broken = ITEM_BLOCK + "\n# Memory Item 2\n## Title\nNo content here\n## Description\nx\n"
        items = sm.parse_memory_items(broken)
        self.assertEqual(len(items), 1, "the incomplete item must not be stored")

    def test_more_than_three_items_are_capped(self) -> None:
        many = "".join(
            f"# Memory Item {i}\n## Title\nT{i}\n## Description\nD{i}\n## Content\nC{i}\n"
            for i in range(1, 7)
        )
        self.assertEqual(len(sm.parse_memory_items(many)), 3)

    def test_empty_input_is_no_items(self) -> None:
        self.assertEqual(sm.parse_memory_items(""), [])


class EmbeddingTextTests(unittest.TestCase):
    """The study's central find: embed the source query, not the lesson."""

    def setUp(self) -> None:
        self.item = sm.parse_memory_items(ITEM_BLOCK)[0]
        self.query = "add rate limiting to the upload endpoint"

    def test_embedding_text_is_query_plus_description(self) -> None:
        text = self.item.embedding_text(self.query)
        self.assertIn(self.query, text)
        self.assertIn(self.item.description, text)

    def test_embedding_text_excludes_the_lesson_content(self) -> None:
        """Embedding the content is the intuitive choice and the wrong one."""
        self.assertNotIn(self.item.content, self.item.embedding_text(self.query))

    def test_whitespace_in_the_query_is_normalised(self) -> None:
        text = self.item.embedding_text("  add   rate\n limiting  ")
        self.assertTrue(text.startswith("add rate limiting"))


class DuplicateGuardTests(unittest.TestCase):
    def test_an_identical_vector_is_a_duplicate(self) -> None:
        self.assertTrue(sm.is_duplicate([1.0, 0.0], [[1.0, 0.0]]))

    def test_an_orthogonal_vector_is_not(self) -> None:
        self.assertFalse(sm.is_duplicate([1.0, 0.0], [[0.0, 1.0]]))

    def test_just_below_the_threshold_is_kept(self) -> None:
        self.assertFalse(sm.is_duplicate([1.0, 0.0], [[0.85, 0.53]]))

    def test_an_empty_bank_never_duplicates(self) -> None:
        self.assertFalse(sm.is_duplicate([1.0, 0.0], []))

    def test_mismatched_dimensions_do_not_crash(self) -> None:
        self.assertFalse(sm.is_duplicate([1.0, 0.0], [[1.0, 0.0, 0.0]]))


class InjectionTests(unittest.TestCase):
    def test_the_preamble_permits_ignoring_and_forces_a_relevance_call(self) -> None:
        block = sm.format_strategy_block(sm.parse_memory_items(ITEM_BLOCK))
        self.assertIn("hints, not instructions", block)
        self.assertIn("whether you are using it", block)

    def test_each_item_renders_title_when_and_how(self) -> None:
        block = sm.format_strategy_block(sm.parse_memory_items(ITEM_BLOCK))
        self.assertIn("Read the migration chain first", block)
        self.assertIn("When:", block)
        self.assertIn("How:", block)

    def test_no_items_renders_nothing(self) -> None:
        self.assertEqual(sm.format_strategy_block([]), "")


class WriteBackFlagTests(unittest.TestCase):
    """Flag off: the record written is byte-for-byte the old one."""

    def _capture(self, env: dict) -> dict:
        import memory_inject as mi

        captured: dict = {}

        def fake_embed(text: str):
            captured["embedded"] = text
            return [0.1, 0.2, 0.3]

        def fake_append(point: dict) -> bool:
            captured["point"] = point
            return True

        with mock.patch.dict("os.environ", env, clear=False), \
             mock.patch.object(mi, "_embed", fake_embed), \
             mock.patch.object(mi._flat, "enabled", lambda: True), \
             mock.patch.object(mi._flat, "append", fake_append), \
             mock.patch.object(mi, "_retire_over_cap", lambda repo: None):
            mi.write_back(
                task_id="t-1",
                target_repo="/repo/x",
                spec_prompt="add rate limiting to the upload endpoint",
                state={"tier": "M", "iteration": 1, "cost_usd": 1.0, "pr_url": ""},
                stop_reason="approve",
            )
        return captured

    def test_flag_off_writes_the_legacy_payload_unchanged(self) -> None:
        captured = self._capture({"MEMORY_STRATEGY_ENABLED": "0",
                                  "MEMORY_WRITEBACK_ENABLED": "1"})
        payload = captured["point"]["payload"]
        for new_key in ("status", "verdict_source", "source_query"):
            self.assertNotIn(new_key, payload,
                             f"{new_key} must not appear with the flag off")
        self.assertEqual(captured["embedded"], payload["text"],
                         "with the flag off the embedded text is the summary, as before")

    def test_flag_on_adds_the_verdict_fields(self) -> None:
        captured = self._capture({"MEMORY_STRATEGY_ENABLED": "1",
                                  "MEMORY_WRITEBACK_ENABLED": "1"})
        payload = captured["point"]["payload"]
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["verdict_source"], "gt")
        self.assertIn("rate limiting", payload["source_query"])

    def test_flag_on_embeds_the_request_too(self) -> None:
        captured = self._capture({"MEMORY_STRATEGY_ENABLED": "1",
                                  "MEMORY_WRITEBACK_ENABLED": "1"})
        self.assertIn("rate limiting", captured["embedded"])

    def test_an_unsettled_verdict_leaves_the_record_legacy_shaped(self) -> None:
        """No signal and no judge: write the old record rather than guess."""
        import memory_inject as mi
        captured: dict = {}
        with mock.patch.dict("os.environ",
                             {"MEMORY_STRATEGY_ENABLED": "1",
                              "MEMORY_WRITEBACK_ENABLED": "1"}, clear=False), \
             mock.patch.object(mi, "_embed", lambda t: (captured.update(embedded=t), [0.1])[1]), \
             mock.patch.object(mi._flat, "enabled", lambda: True), \
             mock.patch.object(mi._flat, "append",
                               lambda p: (captured.update(point=p), True)[1]), \
             mock.patch.object(mi, "_retire_over_cap", lambda repo: None):
            mi.write_back(task_id="t-2", target_repo="/repo/x", spec_prompt="q",
                          state={"tier": "S"}, stop_reason="something unmapped")
        self.assertNotIn("status", captured["point"]["payload"])


class LegacyRecordTests(unittest.TestCase):
    """Old records have none of the new keys and must still be readable."""

    def test_payload_fields_are_additive(self) -> None:
        item = sm.parse_memory_items(ITEM_BLOCK)[0]
        fields = sm.payload_fields(item, verdict=sm.Verdict("fail", "judge"),
                                   source_query="q")
        self.assertEqual(
            set(fields),
            {"status", "verdict_source", "title", "description", "content", "source_query"},
        )

    def test_format_block_tolerates_a_record_without_the_new_fields(self) -> None:
        import memory_inject as mi
        legacy = [{"id": "1", "payload": {"kind": "task_lesson", "text": "old record"}}]
        self.assertIn("old record", mi.format_block(legacy))


if __name__ == "__main__":
    unittest.main()
