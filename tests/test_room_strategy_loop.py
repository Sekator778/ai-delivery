"""Tests for T30 — the room writes strategy memory and reads it back.

The live run of 2026-09-01 finished a room end-to-end and the store did not
move: T26 wired strategy memory to the PIPELINE's write-back, and the room's
finish is a different code path. These tests pin both halves of the loop —
extraction on every terminal outcome, recall into the conductor's first
prompt — and the two contracts that must survive them: either flag off means
byte-for-byte old behaviour, and a memory failure never touches delivery.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import room_conductor as room  # noqa: E402
import room_driver as driver  # noqa: E402
import strategy_memory as sm  # noqa: E402

OWNER_REQUEST = (
    "Хочу купить автомобиль: Берлин, до 50 км, 12-14 тысяч евро, гибрид, "
    "не маленький, 4 взрослых, акцент на расход"
)

EXTRACTION_OUTPUT = """\
# Memory Item 1
## Title
Listing sites block automation
## Description
Use when researching live marketplace listings; not for general model advice.
## Content
Expect the primary marketplace to refuse automated access and go straight to \
the secondary one; report which site the data actually came from.

# Memory Item 2
## Title
Name the criteria conflict early
## Description
Use when hard filters are mutually tight; skip when the market is deep.
## Content
State plainly which of the buyer's constraints cannot be met together and \
recommend a priority order instead of stretching the data.
"""


def _reply(action: str, **params) -> str:
    return json.dumps({"action": action, "reasoning": "test", "params": params})


def _with_cost(body: str, usd: float = 0.01) -> str:
    return body + "\n" + json.dumps({"type": "result", "total_cost_usd": usd})


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


def _run(harness: _Harness, *, budget: float = 2.0, fake_memory=None):
    with tempfile.TemporaryDirectory() as workdir:
        patcher = mock.patch.object(driver, "_memory", fake_memory) \
            if fake_memory is not None else mock.patch.object(
                driver, "_memory", driver._memory)
        with patcher:
            return asyncio.run(driver.run_room(
                OWNER_REQUEST, room_id="room-test", workdir=workdir,
                spawn=harness.spawn, notify=harness.notify,
                budget_usd=budget, max_turns=5,
            ))


def _fake_memory(recall_hits=None, write_error=None):
    """A stand-in for memory_inject: records write calls, serves recall."""
    calls: list[dict] = []

    def recall(query, scope):
        return list(recall_hits or [])

    def write_strategy_items(**kwargs):
        if write_error is not None:
            raise write_error
        calls.append(kwargs)
        return len(kwargs.get("items") or [])

    stub = types.SimpleNamespace(recall=recall,
                                 write_strategy_items=write_strategy_items)
    return stub, calls


class FlagOffTests(unittest.TestCase):
    """Either flag off → the room behaves byte-for-byte as before T30."""

    def test_no_extraction_child_and_no_memory_section(self) -> None:
        harness = _Harness([_with_cost(_reply("finish", summary="done"))])
        with mock.patch.dict("os.environ", {sm.FLAG_ENV: "0"}, clear=False):
            outcome = _run(harness)
        self.assertEqual(outcome.status, "finished")
        self.assertEqual(len(harness.spawned), 1)  # conductor only, no -mx
        self.assertNotIn("PAST STRATEGIES", harness.spawned[0]["prompt"])

    def test_prompt_without_memory_block_is_byte_identical(self) -> None:
        history = room.History()
        base = room.build_prompt(request="r", history=history, attempt=1,
                                 max_attempts=5, budget_left_usd=2.0)
        with_param = room.build_prompt(request="r", history=history, attempt=1,
                                       max_attempts=5, budget_left_usd=2.0,
                                       memory_block="")
        self.assertEqual(base, with_param)


class ExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict("os.environ", {sm.FLAG_ENV: "1"}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_finished_room_extracts_success_branch_and_writes(self) -> None:
        stub, calls = _fake_memory()
        harness = _Harness([
            _with_cost(_reply("finish", summary="shortlist delivered")),
            _with_cost(EXTRACTION_OUTPUT),
        ])
        outcome = _run(harness, fake_memory=stub)

        self.assertEqual(outcome.status, "finished")
        self.assertEqual(len(harness.spawned), 2)
        extraction = harness.spawned[1]
        self.assertTrue(extraction["task_id"].endswith("-mx"))
        self.assertIn("completed its task successfully", extraction["prompt"])
        self.assertIn("REQUEST: " + OWNER_REQUEST, extraction["prompt"])

        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call["scope"], driver.ROOM_MEMORY_SCOPE)
        self.assertEqual(call["source_query"], OWNER_REQUEST)
        self.assertEqual(call["verdict"].status, "success")
        self.assertEqual(call["verdict"].source, "gt")
        self.assertEqual([i.title for i in call["items"]],
                         ["Listing sites block automation",
                          "Name the criteria conflict early"])

    def test_budget_stop_extracts_failure_branch(self) -> None:
        stub, calls = _fake_memory()
        harness = _Harness([_with_cost(EXTRACTION_OUTPUT)])
        outcome = _run(harness, budget=0.0, fake_memory=stub)

        self.assertEqual(outcome.status, "budget")
        # The only child is the extraction one: budget died before turn 1.
        self.assertEqual(len(harness.spawned), 1)
        self.assertTrue(harness.spawned[0]["task_id"].endswith("-mx"))
        self.assertIn("attempted its task and failed",
                      harness.spawned[0]["prompt"])
        self.assertEqual(calls[0]["verdict"].status, "fail")
        # Delivery happened before memory: the budget message reached the chat.
        self.assertTrue(any("Бюджет" in m for m in harness.messages))

    def test_memory_failure_never_breaks_delivery(self) -> None:
        stub, _ = _fake_memory(write_error=RuntimeError("store on fire"))
        harness = _Harness([
            _with_cost(_reply("finish", summary="done")),
            _with_cost(EXTRACTION_OUTPUT),
        ])
        outcome = _run(harness, fake_memory=stub)
        self.assertEqual(outcome.status, "finished")
        self.assertTrue(any("done" in m for m in harness.messages))

    def test_no_items_parsed_writes_nothing(self) -> None:
        stub, calls = _fake_memory()
        harness = _Harness([
            _with_cost(_reply("finish", summary="done")),
            _with_cost("no items here"),
        ])
        _run(harness, fake_memory=stub)
        self.assertEqual(calls, [])


class RecallTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict("os.environ", {sm.FLAG_ENV: "1"}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_strategy_hits_reach_the_conductor_prompt(self) -> None:
        hits = [{"payload": {"title": "Listing sites block automation",
                             "description": "Use for live listings.",
                             "content": "Go straight to the secondary site."}}]
        stub, _ = _fake_memory(recall_hits=hits)
        harness = _Harness([
            _with_cost(_reply("finish", summary="done")),
            _with_cost(EXTRACTION_OUTPUT),
        ])
        _run(harness, fake_memory=stub)
        prompt = harness.spawned[0]["prompt"]
        self.assertIn("PAST STRATEGIES", prompt)
        self.assertIn("Listing sites block automation", prompt)
        self.assertIn(sm.INJECT_PREAMBLE.split(".")[0], prompt)

    def test_legacy_prose_hits_are_not_injected(self) -> None:
        hits = [{"payload": {"kind": "note", "text": "old mem0 prose"}}]
        stub, _ = _fake_memory(recall_hits=hits)
        harness = _Harness([
            _with_cost(_reply("finish", summary="done")),
            _with_cost(EXTRACTION_OUTPUT),
        ])
        _run(harness, fake_memory=stub)
        self.assertNotIn("PAST STRATEGIES", harness.spawned[0]["prompt"])

    def test_recall_error_degrades_to_no_block(self) -> None:
        def broken_recall(query, scope):
            raise RuntimeError("TEI down")

        stub, _ = _fake_memory()
        stub.recall = broken_recall
        harness = _Harness([
            _with_cost(_reply("finish", summary="done")),
            _with_cost(EXTRACTION_OUTPUT),
        ])
        outcome = _run(harness, fake_memory=stub)
        self.assertEqual(outcome.status, "finished")
        self.assertNotIn("PAST STRATEGIES", harness.spawned[0]["prompt"])


class WriteStrategyItemsTests(unittest.TestCase):
    """The store half, against a faked flat store — same pattern as T26 tests."""

    def setUp(self) -> None:
        import memory_inject as mi
        self.mi = mi
        patcher = mock.patch.dict("os.environ", {sm.FLAG_ENV: "1"}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _items(self):
        return [sm.StrategyItem("T", "When to use it.", "Do the thing.")]

    def test_writes_typed_point_into_flat_store(self) -> None:
        appended: list[dict] = []
        with mock.patch.object(self.mi, "_embed", lambda t: [1.0, 0.0]), \
             mock.patch.object(self.mi, "_search", lambda v, k, s=None: []), \
             mock.patch.object(self.mi._flat, "enabled", lambda: True), \
             mock.patch.object(self.mi._flat, "append",
                               lambda p: appended.append(p) or True):
            written = self.mi.write_strategy_items(
                task_id="room-x", scope="room", source_query="buy a car",
                items=self._items(), verdict=sm.Verdict("success", "gt"))
        self.assertEqual(written, 1)
        payload = appended[0]["payload"]
        self.assertEqual(payload["kind"], "strategy")
        self.assertEqual(payload["target_repo"], "room")
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["source_query"], "buy a car")
        self.assertEqual(payload["title"], "T")

    def test_skip_write_on_near_duplicate(self) -> None:
        appended: list[dict] = []
        near = [{"id": "x", "score": 0.95, "payload": {}}]
        with mock.patch.object(self.mi, "_embed", lambda t: [1.0, 0.0]), \
             mock.patch.object(self.mi, "_search", lambda v, k, s=None: near), \
             mock.patch.object(self.mi._flat, "enabled", lambda: True), \
             mock.patch.object(self.mi._flat, "append",
                               lambda p: appended.append(p) or True):
            written = self.mi.write_strategy_items(
                task_id="room-x", scope="room", source_query="buy a car",
                items=self._items(), verdict=sm.Verdict("success", "gt"))
        self.assertEqual(written, 0)
        self.assertEqual(appended, [])

    def test_flag_off_writes_nothing(self) -> None:
        with mock.patch.dict("os.environ", {sm.FLAG_ENV: "0"}, clear=False):
            written = self.mi.write_strategy_items(
                task_id="room-x", scope="room", source_query="q",
                items=self._items(), verdict=sm.Verdict("success", "gt"))
        self.assertEqual(written, 0)


if __name__ == "__main__":
    unittest.main()
