"""Tests for the room driver (T28).

T25 shipped the conductor's brain and no loop: on the live run of 2026-09-01
the conductor returned a valid delegation for $0.178 and then nothing happened
— no specialist, no turn two, nothing in the chat. These tests are mostly about
that: every terminal path must deliver something, because silence was the bug.

The conductor replies below are RECONSTRUCTED from the T28 brief, not copied
from a live log: logs/bot.log is gitignored and absent from a fresh clone
(`git check-ignore` confirms). The shape and the request are the brief's; the
exact wording is not the owner's.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import room_conductor as room  # noqa: E402
import room_driver as driver  # noqa: E402


# Reconstruction of the live turn-1 request (T28 brief, "Контекст первого прогона").
OWNER_REQUEST = (
    "Хочу купить автомобиль: Берлин, до 50 км, 12-14 тысяч евро, гибрид, "
    "не маленький, небольшой пробег, не скучный, чтобы влезали 4 взрослых, "
    "с акцентом на расход"
)


def _reply(action: str, **params) -> str:
    return json.dumps({"action": action, "reasoning": "reconstructed",
                       "params": params})


def _delegate(model: str = "model_3", profile: str = "web-research") -> str:
    return _reply(
        "delegate",
        task_instruction="Find hybrid estates in Berlin, 12-14k EUR, low mileage",
        context="",
        tools_profile=profile,
        model=model,
    )


def _finish(summary: str = "Three candidates with reasons") -> str:
    return _reply("finish", summary=summary)


def _with_cost(body: str, usd: float) -> str:
    """A child's stdout: its reply plus the stream-json result line."""
    return body + "\n" + json.dumps({"type": "result", "total_cost_usd": usd})


class _Harness:
    """Records what the driver spawned and what it delivered."""

    def __init__(self, replies: "list[str]") -> None:
        self.replies = list(replies)
        self.spawned: list[dict] = []
        self.messages: list[str] = []
        self.files: list[str] = []
        self.rc_for: dict[int, int] = {}

    async def spawn(self, *, task_id: str, cwd: str, prompt: str,
                    backend: str) -> driver.SpawnResult:
        self.spawned.append({"task_id": task_id, "backend": backend,
                             "prompt": prompt, "cwd": cwd})
        index = len(self.spawned) - 1
        output = self.replies[index] if index < len(self.replies) else _with_cost("", 0.0)
        return driver.SpawnResult(rc=self.rc_for.get(index, 0), output=output)

    async def notify(self, text: str, files: "list[str] | None" = None) -> None:
        self.messages.append(text)
        self.files.extend(files or [])

    @property
    def all_text(self) -> str:
        return "\n".join(self.messages)


def _run(harness: _Harness, workdir: str, **kw) -> driver.RoomOutcome:
    return asyncio.run(driver.run_room(
        OWNER_REQUEST, room_id="room-test", workdir=workdir,
        spawn=harness.spawn, notify=harness.notify, **kw))


class HappyPathTests(unittest.TestCase):
    """delegate -> delegate -> finish reaches a final message."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def test_two_delegations_then_finish(self) -> None:
        harness = _Harness([
            _with_cost(_delegate(), 0.18),          # conductor turn 1
            _with_cost("specialist output A", 0.30),  # specialist 1
            _with_cost(_delegate(), 0.10),          # conductor turn 2
            _with_cost("specialist output B", 0.20),  # specialist 2
            _with_cost(_finish(), 0.05),            # conductor turn 3
        ])
        outcome = _run(harness, self.tmp, budget_usd=5.0, max_turns=5)

        self.assertEqual(outcome.status, "finished")
        self.assertEqual(outcome.delegations, 2)
        self.assertIn("Three candidates", harness.all_text)

    def test_the_specialist_gets_the_brief_and_the_original_request(self) -> None:
        harness = _Harness([
            _with_cost(_delegate(), 0.1),
            _with_cost("done", 0.1),
            _with_cost(_finish(), 0.1),
        ])
        _run(harness, self.tmp, budget_usd=5.0, max_turns=5)

        specialist = next(s for s in harness.spawned if "-s1" in s["task_id"])
        self.assertIn("Find hybrid estates", specialist["prompt"])
        self.assertIn("Берлин", specialist["prompt"],
                      "the specialist must see the owner's own words, not only the brief")

    def test_a_web_research_specialist_runs_on_the_anthropic_backend(self) -> None:
        harness = _Harness([
            _with_cost(_delegate(model="model_3"), 0.1),
            _with_cost("done", 0.1),
            _with_cost(_finish(), 0.1),
        ])
        _run(harness, self.tmp, budget_usd=5.0, max_turns=5)
        specialist = next(s for s in harness.spawned if "-s1" in s["task_id"])
        self.assertEqual(specialist["backend"], "anthropic")


class DeliverableTests(unittest.TestCase):
    """"The report is ready" with no report is the same bug as silence."""

    def test_finish_sends_the_files_the_specialist_left_behind(self) -> None:
        tmp = tempfile.mkdtemp()
        (Path(tmp) / "shortlist.md").write_text("# Shortlist\n", encoding="utf-8")

        harness = _Harness([
            _with_cost(_delegate(profile="documents", model="model_1"), 0.1),
            _with_cost("wrote the shortlist", 0.1),
            _with_cost(_finish(), 0.1),
        ])
        outcome = _run(harness, tmp, budget_usd=5.0, max_turns=5)

        self.assertEqual(outcome.status, "finished")
        self.assertTrue(harness.files, "the deliverable must reach the chat")
        self.assertTrue(any(f.endswith("shortlist.md") for f in harness.files))
        self.assertIn("Three candidates", harness.all_text,
                      "text and files, not one or the other")

    def test_an_empty_scratch_delivers_text_without_crashing(self) -> None:
        harness = _Harness([_with_cost(_finish(), 0.1)])
        outcome = _run(harness, tempfile.mkdtemp(), budget_usd=5.0, max_turns=5)
        self.assertEqual(outcome.status, "finished")
        self.assertEqual(harness.files, [])

    def test_dotfiles_are_not_delivered(self) -> None:
        tmp = tempfile.mkdtemp()
        (Path(tmp) / ".hidden").write_text("noise", encoding="utf-8")
        harness = _Harness([_with_cost(_finish(), 0.1)])
        _run(harness, tmp, budget_usd=5.0, max_turns=5)
        self.assertEqual(harness.files, [])


class BudgetTests(unittest.TestCase):
    """The cap is enforced by the driver, not merely mentioned in the prompt."""

    def test_budget_exhausted_after_a_specialist_stops_honestly(self) -> None:
        harness = _Harness([
            _with_cost(_delegate(), 0.10),
            _with_cost("expensive specialist", 0.95),
        ])
        outcome = _run(harness, tempfile.mkdtemp(), budget_usd=1.0, max_turns=5)

        self.assertEqual(outcome.status, "budget")
        self.assertIn("Бюджет комнаты исчерпан", harness.all_text)
        self.assertIn("специалистов отработало 1", harness.all_text)

    def test_conductor_turns_are_debited_too(self) -> None:
        """Turn one of the live run cost $0.178 — 9% of the default cap."""
        harness = _Harness([
            _with_cost(_delegate(), 0.60),   # conductor alone blows most of it
            _with_cost("specialist", 0.60),
        ])
        outcome = _run(harness, tempfile.mkdtemp(), budget_usd=1.0, max_turns=5)

        self.assertEqual(outcome.status, "budget")
        self.assertGreater(outcome.spent_usd, 1.0,
                           "the conductor's own turn must be on the books")

    def test_spend_is_reported_in_the_stop_message(self) -> None:
        harness = _Harness([_with_cost(_delegate(), 2.0)])
        _run(harness, tempfile.mkdtemp(), budget_usd=1.0, max_turns=5)
        self.assertIn("$2.00", harness.all_text)

    def test_turn_limit_stops_with_a_message(self) -> None:
        harness = _Harness([
            _with_cost(_delegate(), 0.01), _with_cost("s", 0.01),
            _with_cost(_delegate(), 0.01), _with_cost("s", 0.01),
        ])
        outcome = _run(harness, tempfile.mkdtemp(), budget_usd=99.0, max_turns=2)
        self.assertEqual(outcome.status, "turns")
        self.assertIn("Лимит ходов исчерпан", harness.all_text)


class ParseFailureTests(unittest.TestCase):
    """One reprompt, then an honest refusal. Never silence."""

    def test_one_bad_reply_is_retried(self) -> None:
        harness = _Harness([
            _with_cost("I think we should look at hybrids.", 0.05),  # no JSON
            _with_cost(_finish(), 0.05),
        ])
        outcome = _run(harness, tempfile.mkdtemp(), budget_usd=5.0, max_turns=5)
        self.assertEqual(outcome.status, "finished")
        self.assertEqual(len(harness.spawned), 2, "the conductor was re-prompted")

    def test_two_bad_replies_stop_with_an_explanation(self) -> None:
        harness = _Harness([
            _with_cost("no json", 0.05),
            _with_cost("still no json", 0.05),
        ])
        outcome = _run(harness, tempfile.mkdtemp(), budget_usd=5.0, max_turns=5)
        self.assertEqual(outcome.status, "unparseable")
        self.assertIn("не разобрать", harness.all_text)
        self.assertNotEqual(harness.messages, [], "silence is never an outcome")

    def test_the_retry_prompt_carries_the_repair_hint(self) -> None:
        harness = _Harness([
            _with_cost("no json", 0.05),
            _with_cost(_finish(), 0.05),
        ])
        _run(harness, tempfile.mkdtemp(), budget_usd=5.0, max_turns=5)
        self.assertIn("ONE JSON object", harness.spawned[1]["prompt"])

    def test_an_illegal_model_for_the_profile_is_refused_with_the_reason(self) -> None:
        """The conductor must be able to re-pick knowingly."""
        harness = _Harness([
            _with_cost(_delegate(model="model_1", profile="web-research"), 0.05),
            _with_cost(_delegate(model="model_3"), 0.05),
            _with_cost("specialist", 0.05),
            _with_cost(_finish(), 0.05),
        ])
        outcome = _run(harness, tempfile.mkdtemp(), budget_usd=5.0, max_turns=6)

        self.assertEqual(outcome.status, "finished")
        retry_prompt = harness.spawned[1]["prompt"]
        self.assertIn("cannot run on model_1", retry_prompt)
        self.assertIn("model_3", retry_prompt,
                      "the refusal must name the legal choice")


class SpecialistFailureTests(unittest.TestCase):
    """A child that fell over is visible in the chat and in the history."""

    def test_a_failing_specialist_is_reported_not_swallowed(self) -> None:
        harness = _Harness([
            _with_cost(_delegate(), 0.05),
            _with_cost("boom", 0.05),
            _with_cost(_finish(), 0.05),
        ])
        harness.rc_for[1] = 1  # the specialist fails

        outcome = _run(harness, tempfile.mkdtemp(), budget_usd=5.0, max_turns=5)
        self.assertEqual(outcome.status, "finished")
        self.assertIn("упал", harness.all_text)

    def test_the_conductor_sees_the_failure_in_its_next_prompt(self) -> None:
        harness = _Harness([
            _with_cost(_delegate(), 0.05),
            _with_cost("traceback here", 0.05),
            _with_cost(_finish(), 0.05),
        ])
        harness.rc_for[1] = 1
        _run(harness, tempfile.mkdtemp(), budget_usd=5.0, max_turns=5)
        self.assertIn("failed (rc=1)", harness.spawned[2]["prompt"])


class CostParsingTests(unittest.TestCase):
    """Budget in the provider's currency, not the CLI's Anthropic prices."""

    def test_reads_the_stream_json_result_line(self) -> None:
        usd, _ = driver.parse_cost(
            '{"type":"result","total_cost_usd":0.178}', "anthropic")
        self.assertAlmostEqual(usd, 0.178)

    def test_a_cost_without_token_counts_is_not_silently_zero(self) -> None:
        """Repricing computes from tokens; no tokens would reprice to $0.00.

        A room that debits nothing spends without limit — the same shape as the
        "unknown model costs $0" that T26 refused to copy. Found by these tests
        before it reached a live run.
        """
        line = json.dumps({"type": "result", "total_cost_usd": 2.0})
        usd, source = driver.parse_cost(line, "deepseek")
        self.assertEqual(usd, 2.0)
        self.assertIn("fallback", source, "the fallback must be visible in the ledger")

    def test_missing_cost_is_zero_and_labelled_not_guessed(self) -> None:
        usd, source = driver.parse_cost("no result line", "deepseek")
        self.assertEqual(usd, 0.0)
        self.assertEqual(source, "no-cost-line")

    def test_empty_output_does_not_crash(self) -> None:
        self.assertEqual(driver.parse_cost("", "deepseek"), (0.0, "no-output"))

    def test_a_deepseek_child_is_repriced_off_the_cli_figure(self) -> None:
        """The CLI reports Anthropic prices whatever endpoint it was aimed at."""
        line = json.dumps({"type": "result", "total_cost_usd": 9.99,
                           "input_tokens": 1000, "output_tokens": 1000})
        usd, source = driver.parse_cost(line, "deepseek")
        self.assertNotEqual(source, "cli",
                            "a non-anthropic backend must not pass the CLI figure through")
        self.assertLess(usd, 9.99, "DeepSeek tokens are not priced at Anthropic rates")


if __name__ == "__main__":
    unittest.main()
