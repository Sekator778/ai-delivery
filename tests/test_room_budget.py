"""Tests for T31 — the room's cap is a permission to hire, not only a stop.

Two live runs on 2026-09-01 shaped these: a $0.05 room hired a $0.92
specialist because nothing checked the price before the child ran; a $2.00
room's specialist wrote an excellent report, cost $2.26, and the budget
terminal reported "nothing collected" while the file sat on disk. And the
model the conductor paid for was not the model that ran.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import room_conductor as room  # noqa: E402
import room_driver as driver  # noqa: E402

REQUEST = "Find a used hybrid estate near Berlin for 12-14k EUR"


def _reply(action: str, **params) -> str:
    return json.dumps({"action": action, "reasoning": "test", "params": params})


def _delegate(model: str = "model_3", profile: str = "web-research") -> str:
    return _reply("delegate", task_instruction="Scan live listings and shortlist",
                  context="", tools_profile=profile, model=model)


def _finish(summary: str = "done") -> str:
    return _reply("finish", summary=summary)


def _with_cost(body: str, usd: float = 0.01) -> str:
    return body + "\n" + json.dumps({"type": "result", "total_cost_usd": usd})


def _result(text: str, usd: float = 0.01) -> str:
    """A child's stdout with the reply carried in the result event (T29 form)."""
    return json.dumps({"type": "result", "total_cost_usd": usd, "result": text})


class _Harness:
    def __init__(self, replies: "list[str]") -> None:
        self.replies = list(replies)
        self.spawned: list[dict] = []
        self.messages: list[str] = []
        self.files: list[str] = []

    async def spawn(self, *, task_id: str, cwd: str, prompt: str,
                    backend: str, model: "str | None" = None) -> driver.SpawnResult:
        self.spawned.append({"task_id": task_id, "backend": backend,
                             "prompt": prompt, "cwd": cwd, "model": model})
        index = len(self.spawned) - 1
        output = self.replies[index] if index < len(self.replies) else _with_cost("")
        return driver.SpawnResult(rc=0, output=output)

    async def notify(self, text: str, files: "list[str] | None" = None) -> None:
        self.messages.append(text)
        self.files.extend(files or [])


def _run(harness: _Harness, *, budget: float, workdir: "str | None" = None):
    if workdir is None:
        with tempfile.TemporaryDirectory() as tmp:
            return asyncio.run(driver.run_room(
                REQUEST, room_id="room-t31", workdir=tmp,
                spawn=harness.spawn, notify=harness.notify,
                budget_usd=budget, max_turns=5))
    return asyncio.run(driver.run_room(
        REQUEST, room_id="room-t31", workdir=workdir,
        spawn=harness.spawn, notify=harness.notify,
        budget_usd=budget, max_turns=5))


class FloorTests(unittest.TestCase):
    def test_floor_orders_with_price(self) -> None:
        floors = [room.min_spend_usd(o) for o in room.MODEL_CATALOGUE]
        self.assertEqual(floors, sorted(floors))
        self.assertGreater(floors[-1], 0.5)   # the anthropic option is not "cheap"

    def test_affordable_models_shrink_with_budget(self) -> None:
        self.assertEqual(room.affordable_models(2.0),
                         list(room.masked_models()))
        self.assertNotIn("model_3", room.affordable_models(0.05))
        self.assertEqual(room.affordable_models(0.0), [])

    def test_prompt_names_the_floor_only_when_something_is_out_of_reach(self) -> None:
        rich = room.build_prompt(REQUEST, room.History(), 1, 5, 2.0)
        poor = room.build_prompt(REQUEST, room.History(), 1, 5, 0.05)
        self.assertNotIn("Affordable now", rich)
        self.assertIn("Affordable now", poor)
        self.assertIn("model_3 needs at least", poor)


class PreAuthorisationTests(unittest.TestCase):
    """The $0.05 room: the hire is refused before the child runs."""

    def test_unaffordable_hire_is_refused_and_conductor_re_decides(self) -> None:
        harness = _Harness([
            _with_cost(_delegate("model_3"), 0.01),   # c1: wants the dear option
            _with_cost(_finish("finished with what I had"), 0.01),  # c2
        ])
        outcome = _run(harness, budget=0.05)

        ids = [s["task_id"] for s in harness.spawned]
        self.assertEqual(ids, ["room-t31-c1", "room-t31-c2"])  # no -s1
        self.assertEqual(outcome.status, "finished")
        self.assertEqual(outcome.delegations, 0)
        self.assertTrue(any("Найм отклонён" in m for m in harness.messages))
        # The conductor sees the refusal and why on its next turn.
        self.assertIn("insufficient budget for model_3", harness.spawned[1]["prompt"])

    def test_affordable_hire_runs(self) -> None:
        harness = _Harness([
            _with_cost(_delegate("model_3"), 0.01),
            _with_cost("listings found", 0.5),
            _with_cost(_finish(), 0.01),
        ])
        outcome = _run(harness, budget=2.0)
        ids = [s["task_id"] for s in harness.spawned]
        self.assertIn("room-t31-s1", ids)
        self.assertEqual(outcome.delegations, 1)


class ModelPinTests(unittest.TestCase):
    def test_specialist_runs_the_model_the_conductor_bought(self) -> None:
        harness = _Harness([
            _with_cost(_delegate("model_3"), 0.01),
            _with_cost("ok", 0.1),
            _with_cost(_finish(), 0.01),
        ])
        _run(harness, budget=2.0)
        specialist = next(s for s in harness.spawned if s["task_id"].endswith("-s1"))
        self.assertEqual(specialist["backend"], "anthropic")
        self.assertEqual(specialist["model"],
                         room.masked_models()["model_3"].real_name)

    def test_cheap_option_pins_the_cheap_model(self) -> None:
        harness = _Harness([
            _with_cost(_delegate("model_1", "documents"), 0.01),
            _with_cost("ok", 0.01),
            _with_cost(_finish(), 0.01),
        ])
        _run(harness, budget=2.0)
        specialist = next(s for s in harness.spawned if s["task_id"].endswith("-s1"))
        self.assertEqual(specialist["model"], "deepseek-v4-flash")

    def test_conductor_turns_are_not_pinned(self) -> None:
        harness = _Harness([_with_cost(_finish(), 0.01)])
        _run(harness, budget=2.0)
        self.assertIsNone(harness.spawned[0]["model"])


class PartialDeliveryTests(unittest.TestCase):
    """The $2.00 room: paid-for work on disk is delivered, with an honest label."""

    def test_budget_terminal_delivers_files_and_last_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = _Harness([
                _with_cost(_delegate("model_3"), 0.02),
                _result("Report written to report.md — shortlist of three", 2.5),
                # c2 never runs: the specialist alone exceeded the cap.
            ])
            # The specialist "wrote" its deliverable while running.
            original_spawn = harness.spawn

            async def spawn_and_write(**kw):
                res = await original_spawn(**kw)
                if kw["task_id"].endswith("-s1"):
                    Path(tmp, "report.md").write_text("# Shortlist\n", encoding="utf-8")
                return res

            outcome = asyncio.run(driver.run_room(
                REQUEST, room_id="room-t31", workdir=tmp,
                spawn=spawn_and_write, notify=harness.notify,
                budget_usd=2.0, max_turns=5))

            self.assertEqual(outcome.status, "budget")
            self.assertEqual(len(harness.files), 1)
            self.assertTrue(harness.files[0].endswith("report.md"))
            self.assertEqual(outcome.files, harness.files)
            final = harness.messages[-1]
            self.assertIn("Бюджет комнаты исчерпан", final)
            self.assertIn("успели сделать", final)
            self.assertIn("shortlist of three", final)  # the specialist's own text
            self.assertNotIn("Итог не собран", final)

    def test_budget_terminal_without_work_stays_plain(self) -> None:
        harness = _Harness([])
        outcome = _run(harness, budget=0.0)
        self.assertEqual(outcome.status, "budget")
        self.assertEqual(harness.files, [])
        self.assertNotIn("успели сделать", harness.messages[-1])


if __name__ == "__main__":
    unittest.main()
