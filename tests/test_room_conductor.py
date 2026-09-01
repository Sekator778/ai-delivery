"""Tests for the room conductor (T25).

Two halves. Most of the file pins the three fragilities the AOrchestra code
study recorded in its Observations section — the reference's real failure
modes, which the brief said not to copy:

  1. `json.loads` with no retry: one malformed reply ends the task.
  2. An unknown action burns a turn and leaves nothing in the history, so the
     model cannot see itself repeating the mistake.
  3. SUBTASK HISTORY with no cap: it grows until it crowds out the task.

The rest pins the flag: with ROOM_ENABLED off the bot must behave exactly as it
did before this feature existed.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import room_conductor as room  # noqa: E402


def _delegation(**over) -> str:
    params = {
        "task_instruction": "Compare three used estate cars under 15k EUR",
        "context": "",
        "tools_profile": "web-research",
        "model": "model_1",
    }
    params.update(over)
    import json
    return json.dumps({"action": "delegate", "reasoning": "r", "params": params})


class FlagDefaultTests(unittest.TestCase):
    """Default off, and off means absent rather than present-but-refusing."""

    def test_disabled_by_default(self) -> None:
        self.assertFalse(room.room_enabled({}))

    def test_only_explicit_truthy_values_enable_it(self) -> None:
        for value in ("1", "true", "yes", "on", "TRUE"):
            self.assertTrue(room.room_enabled({"ROOM_ENABLED": value}), value)
        for value in ("", "0", "false", "no", "off", "maybe"):
            self.assertFalse(room.room_enabled({"ROOM_ENABLED": value}), value)

    def test_bot_registers_the_command_only_behind_the_flag(self) -> None:
        """The handler and the menu entry are both inside the same gate.

        Read as source rather than by importing bot.py: importing it pulls in
        telegram, aiohttp and a module-level config load. What matters here is
        structural — that neither registration exists outside the gate.
        """
        src = (REPO_ROOT / "bot" / "bot.py").read_text(encoding="utf-8")

        handler = re.search(
            r'if _room_enabled\(\):\s*\n\s*app\.add_handler\(\s*CommandHandler\("room"',
            src,
        )
        self.assertIsNotNone(
            handler, "the /room handler must be registered only under _room_enabled()")

        menu = re.search(
            r'if _room_enabled\(\):\s*\n\s*commands\.insert\([^)]*BotCommand\("room"',
            src,
        )
        self.assertIsNotNone(
            menu, "the /room menu entry must be advertised only under _room_enabled()")

        # And nowhere else: one registration each, both gated.
        self.assertEqual(src.count('CommandHandler("room"'), 1)
        self.assertEqual(src.count('BotCommand("room"'), 1)


class JsonRetryTests(unittest.TestCase):
    """Fragility 1: a malformed reply must be a turn, not the end of the task."""

    def test_plain_json_parses(self) -> None:
        decision = room.parse_decision(_delegation())
        self.assertEqual(decision["action"], "delegate")

    def test_json_inside_a_fenced_block_parses(self) -> None:
        decision = room.parse_decision("Sure:\n```json\n" + _delegation() + "\n```\n")
        self.assertEqual(decision["action"], "delegate")

    def test_json_wrapped_in_prose_parses(self) -> None:
        decision = room.parse_decision("I think: " + _delegation() + " — hope that helps")
        self.assertEqual(decision["action"], "delegate")

    def test_prose_only_raises_with_a_repair_hint(self) -> None:
        with self.assertRaises(room.DecisionParseError) as ctx:
            room.parse_decision("Let's delegate this to a researcher.")
        hint = ctx.exception.repair_hint()
        self.assertIn("ONE JSON object", hint)

    def test_an_unparseable_reply_does_not_end_the_run(self) -> None:
        history = room.History()
        turn = room.interpret("no json here at all", history)
        self.assertFalse(turn.ok)
        self.assertTrue(history.entries, "the failed turn must be recorded")

    def test_unknown_action_is_rejected(self) -> None:
        with self.assertRaises(room.DecisionParseError):
            room.parse_decision('{"action": "improvise", "params": {}}')


class FailureVisibilityTests(unittest.TestCase):
    """Fragility 2: a burnt turn must leave a trace the conductor can read."""

    def test_an_unparseable_reply_shows_up_in_the_rendered_history(self) -> None:
        history = room.History()
        room.interpret("nonsense", history)
        rendered = history.render()
        self.assertIn("unparseable", rendered)
        self.assertIn("ONE JSON object", rendered,
                      "the history must carry the hint for the next attempt")

    def test_a_refused_delegation_shows_up_and_names_the_field(self) -> None:
        history = room.History()
        turn = room.interpret(_delegation(model="model_99"), history)
        self.assertFalse(turn.ok)
        rendered = history.render()
        self.assertIn("refused", rendered)
        self.assertIn("model_99", rendered)

    def test_an_unknown_tools_profile_is_refused_not_defaulted(self) -> None:
        history = room.History()
        turn = room.interpret(_delegation(tools_profile="outbound-email"), history)
        self.assertFalse(turn.ok)
        self.assertIn("outbound-email", turn.error)

    def test_an_empty_instruction_is_refused(self) -> None:
        history = room.History()
        turn = room.interpret(_delegation(task_instruction="   "), history)
        self.assertFalse(turn.ok)
        self.assertIn("task_instruction", turn.error)

    def test_an_unknown_model_is_an_error_not_a_free_one(self) -> None:
        """The reference priced anything missing from its table at $0."""
        with self.assertRaises(room.DelegationError):
            room.Delegation.from_params({
                "task_instruction": "x", "tools_profile": "documents",
                "model": "gpt-whatever",
            })


class HistoryCapTests(unittest.TestCase):
    """Fragility 3: bounded, and the truncation is announced."""

    def test_history_is_capped(self) -> None:
        history = room.History(max_chars=200)
        for i in range(20):
            history.add(room.HistoryEntry("delegation", f"attempt {i}", "x" * 80))
        self.assertLessEqual(len(history.render()), 400)

    def test_truncation_is_visible_not_silent(self) -> None:
        history = room.History(max_chars=200)
        for i in range(20):
            history.add(room.HistoryEntry("delegation", f"attempt {i}", "x" * 80))
        self.assertIn("truncated", history.render())

    def test_the_most_recent_entries_survive(self) -> None:
        history = room.History(max_chars=200)
        for i in range(20):
            history.add(room.HistoryEntry("delegation", f"attempt {i}", ""))
        self.assertIn("attempt 19", history.render())

    def test_empty_history_says_so(self) -> None:
        self.assertIn("first attempt", room.History().render())


class ModelMaskingTests(unittest.TestCase):
    """Masked names + a price table: the choice is about cost, not brand."""

    def test_masks_are_ordered_cheapest_first(self) -> None:
        prices = [opt.price_per_mtok for opt in room.masked_models().values()]
        self.assertEqual(prices, sorted(prices))

    def test_the_prompt_carries_no_real_model_names(self) -> None:
        prompt = room.build_prompt("buy a car", room.History(), 1, 5, 2.0)
        for opt in room.MODEL_CATALOGUE:
            self.assertNotIn(opt.real_name, prompt,
                             f"{opt.real_name} leaked into the prompt")
            self.assertNotIn(opt.backend, prompt)

    def test_a_masked_choice_resolves_back_to_a_real_backend(self) -> None:
        delegation = room.Delegation.from_params({
            "task_instruction": "x", "tools_profile": "documents", "model": "model_1",
        })
        self.assertEqual(delegation.option.backend, "deepseek")


class PromptTests(unittest.TestCase):
    """The prompt must carry what the decision needs, on every scenario."""

    SCENARIOS = (
        "Что почитать про переход на электромобиль в Европе",
        "Подобрать подержанный универсал до 15000 евро",
        "Составь план поездки в Португалию на неделю",
    )

    def test_each_scenario_builds_a_usable_prompt(self) -> None:
        for request in self.SCENARIOS:
            prompt = room.build_prompt(request, room.History(), 1, 5, 2.0)
            with self.subTest(request=request):
                self.assertIn(request, prompt, "the original request must be present")
                for profile in room.TOOL_PROFILES:
                    self.assertIn(profile, prompt)
                self.assertIn("model_1", prompt)
                self.assertIn("READ", prompt)
                self.assertIn("VERIFY", prompt)

    def test_the_prompt_states_there_are_no_outbound_capabilities(self) -> None:
        prompt = room.build_prompt("book me a hotel", room.History(), 1, 5, 2.0)
        self.assertIn("no outbound capabilities", prompt.lower())

    def test_the_last_attempt_is_announced(self) -> None:
        prompt = room.build_prompt("x", room.History(), 5, 5, 2.0)
        self.assertIn("LAST attempt", prompt)

    def test_an_exhausted_budget_is_announced(self) -> None:
        prompt = room.build_prompt("x", room.History(), 2, 5, 0.0)
        self.assertIn("EXHAUSTED", prompt)


class BudgetTests(unittest.TestCase):
    """A hard cap, unlike the reference, which only ever counted."""

    def test_default_cap_is_two_dollars(self) -> None:
        self.assertEqual(room.budget_usd({}), 2.0)

    def test_cap_is_configurable(self) -> None:
        self.assertEqual(room.budget_usd({"ROOM_BUDGET_USD": "0.5"}), 0.5)

    def test_a_nonsense_cap_falls_back_rather_than_crashing(self) -> None:
        self.assertEqual(room.budget_usd({"ROOM_BUDGET_USD": "free"}), 2.0)
        self.assertEqual(room.budget_usd({"ROOM_BUDGET_USD": "-5"}), 2.0)

    def test_delegation_cap_defaults_to_five(self) -> None:
        self.assertEqual(room.max_delegations({}), 5)

    def test_the_room_has_its_own_ledger_source(self) -> None:
        self.assertEqual(room.COST_SOURCE, "room")


class ScratchTests(unittest.TestCase):
    def test_default_scratch_follows_the_existing_convention(self) -> None:
        root = room.scratch_root({})
        self.assertIn("_scratch", root)

    def test_scratch_is_configurable(self) -> None:
        self.assertEqual(room.scratch_root({"ROOM_SCRATCH_ROOT": "/tmp/rooms"}), "/tmp/rooms")


class FinishTests(unittest.TestCase):
    def test_finish_carries_the_summary(self) -> None:
        history = room.History()
        turn = room.interpret(
            '{"action":"finish","reasoning":"done","params":{"summary":"three cars, with reasons"}}',
            history,
        )
        self.assertTrue(turn.ok)
        self.assertEqual(turn.action, "finish")
        self.assertIn("three cars", turn.summary)


if __name__ == "__main__":
    unittest.main()
