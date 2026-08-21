"""Sticky triage across re-ingest (committee follow-up 2026-06-02).

Pins the 4th finding from the live M-validation: the dispatcher writes a fresh
state.json on every (re-)ingest, so a clarify round-trip re-ran triage from
scratch — and on the second pass the best-effort LLM verdict flaked, leaving
deterministic-only conf 0.50 which trips the <0.70 fail-safe and silently
downgraded M→L. The task is unchanged across the round-trip, so the FIRST verdict
must stick: it is persisted to a durable triage.json and reused instead of
re-classified.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import stage_runner_agent as sra  # noqa: E402


class _FakeTri:
    def __init__(self, tier: str) -> None:
        self.tier = tier
        self.source = "llm+deterministic"
        self.confidence = 0.85
        self.dimensions = {"type": "feature", "size": tier, "risk": "low",
                           "clarity": "clear"}
        self.caps = {"iteration_cap": 2 if tier == "M" else 3,
                     "token_cap": 550000 if tier == "M" else 800000}
        self.reasons = ["fresh classification"]

    def to_state(self, mode: str) -> dict:
        return {"verdict": "dev", "estimate": self.tier, "tier": self.tier,
                "dimensions": self.dimensions, "confidence": self.confidence,
                "reasons": self.reasons, "caps": self.caps,
                "source": self.source, "mode": mode}


class PersistRoundTripTests(unittest.TestCase):
    def test_persist_then_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            v = {"tier": "M", "caps": {"iteration_cap": 2}, "confidence": 0.85}
            sra._persist_triage(d, v)
            self.assertTrue((d / "triage.json").exists())
            self.assertEqual(sra._load_persisted_triage(d), v)

    def test_load_none_when_missing_or_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self.assertIsNone(sra._load_persisted_triage(d))           # missing
            (d / "triage.json").write_text("{ not json")
            self.assertIsNone(sra._load_persisted_triage(d))           # malformed
            (d / "triage.json").write_text('{"confidence": 0.9}')      # no tier/caps
            self.assertIsNone(sra._load_persisted_triage(d))


class StickyReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {n: getattr(sra, n) for n in (
            "_triage_mode", "_triage_acting", "_write_triage_report",
            "_send_telegram")}
        self._decide = sra._triage.decide
        self._env = os.environ.get("TRIAGE_STICKY")
        sra._triage_mode = lambda: "full"
        sra._triage_acting = lambda *a, **k: False
        sra._write_triage_report = lambda *a, **k: None
        sra._send_telegram = lambda *a, **k: None
        self.calls: list = []

        def decide_stub(*a, **k):
            self.calls.append(1)
            return _FakeTri("L")          # a DIFFERENT tier — proves reuse vs reclassify

        sra._triage.decide = decide_stub

    def tearDown(self) -> None:
        for n, v in self._saved.items():
            setattr(sra, n, v)
        sra._triage.decide = self._decide
        if self._env is None:
            os.environ.pop("TRIAGE_STICKY", None)
        else:
            os.environ["TRIAGE_STICKY"] = self._env

    def _task(self, tmp: str, *, with_persisted_tier: str | None) -> Path:
        d = Path(tmp)
        (d / "state.json").write_text('{"stage": "received", "iteration": 0}')
        if with_persisted_tier:
            sra._persist_triage(d, _FakeTri(with_persisted_tier).to_state("full"))
        return d

    def test_reuses_persisted_verdict_without_reclassifying(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = self._task(tmp, with_persisted_tier="M")
            out = sra._maybe_run_triage(d, d, {}, {"prompt": "x"}, "tid")
            self.assertIsNotNone(out)                       # truthy → "triage acted"
            self.assertEqual(self.calls, [])                # decide NOT called
            tri = json.loads((d / "state.json").read_text())["triage"]
            self.assertEqual(tri["tier"], "M")              # M preserved, not L
            self.assertTrue(any("sticky" in r for r in tri["reasons"]))

    def test_first_run_classifies_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = self._task(tmp, with_persisted_tier=None)   # no triage.json yet
            sra._maybe_run_triage(d, d, {}, {"prompt": "x"}, "tid")
            self.assertEqual(self.calls, [1])               # classified fresh
            self.assertEqual(sra._load_persisted_triage(d)["tier"], "L")  # persisted
            tri = json.loads((d / "state.json").read_text())["triage"]
            self.assertEqual(tri["tier"], "L")

    def test_sticky_disabled_reclassifies(self) -> None:
        os.environ["TRIAGE_STICKY"] = "0"
        with tempfile.TemporaryDirectory() as tmp:
            d = self._task(tmp, with_persisted_tier="M")    # M on disk
            sra._maybe_run_triage(d, d, {}, {"prompt": "x"}, "tid")
            self.assertEqual(self.calls, [1])               # re-classified despite persisted
            tri = json.loads((d / "state.json").read_text())["triage"]
            self.assertEqual(tri["tier"], "L")              # overwritten with fresh verdict


if __name__ == "__main__":
    unittest.main()
