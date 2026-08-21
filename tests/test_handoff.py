"""Graceful terminal handoff + timeout≠crash (PR2, committee 2026-06-02).

Pins the work-preservation half of the compromise: a non-approve termination
(stage crash, wall-clock timeout, pre-stage gate) must never be a silent rc=1
FAILED that throws away pushed work and strands the task in active/. Instead it
routes through _handoff_terminal — recover the PR, write UNRESOLVED-FINDINGS.md,
pick the bucket, move it. And a wall-clock timeout is distinct from a crash: it
returns RC_STAGE_TIMEOUT and must NOT buy a second STAGE_TIMEOUT_SEC window via
the anthropic auto-fallback (≈ half the M-validation incident's $17.31).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import stage_runner_agent as sra  # noqa: E402


class _Patched(unittest.TestCase):
    """Save/restore the module globals these tests monkeypatch."""

    def setUp(self) -> None:
        self._saved = {
            name: getattr(sra, name) for name in (
                "_AWAITING_INPUT_DIR", "_FAILED_DIR", "_send_telegram",
                "_notify_bot", "_recover_pr_from_repo", "_handoff_terminal",
                "_execute_single_stage", "_resolve_stage_backend",
                "_canonicalize_stage_artifact", "_mirror_to_specs_folder",
                "_token_cap_exceeded",
            )
        }
        sra._send_telegram = lambda *a, **k: None
        sra._notify_bot = lambda *a, **k: None

    def tearDown(self) -> None:
        for name, val in self._saved.items():
            setattr(sra, name, val)


class StageTimeoutSentinelTests(_Patched):
    def test_inner_timeout_returns_sentinel_not_crash(self) -> None:
        sra._build_format_kwargs_saved = sra._build_format_kwargs
        sra._build_format_kwargs = lambda *a, **k: {}
        sra._subagent_env = getattr(sra, "_subagent_env")
        saved_env = sra._subagent_env
        sra._subagent_env = lambda *a, **k: {}
        sra._resolve_stage_backend = lambda *a, **k: ("anthropic", None)
        saved_prompt = sra.STAGE_PROMPTS.get("tester")
        sra.STAGE_PROMPTS["tester"] = "noop"
        saved_run = sra.subprocess.run

        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

        sra.subprocess.run = boom
        try:
            with tempfile.TemporaryDirectory() as tmp:
                d = Path(tmp)
                (d / "state.json").write_text('{"iteration": 1}')
                rc = sra._execute_single_stage_inner(d, d, "tester", {"iteration": 1})
                self.assertEqual(rc, sra.RC_STAGE_TIMEOUT)
                self.assertTrue((d / "tester.claude-error.log").exists())
        finally:
            sra.subprocess.run = saved_run
            sra._build_format_kwargs = sra._build_format_kwargs_saved
            sra._subagent_env = saved_env
            if saved_prompt is not None:
                sra.STAGE_PROMPTS["tester"] = saved_prompt


class AutoFallbackGuardTests(_Patched):
    def _task(self, tmp: str) -> Path:
        d = Path(tmp)
        (d / "state.json").write_text('{"iteration": 1, "stage": "tester"}')
        (d / "01-ba.md").write_text("# BRD\n\nenough content to pass the precheck.")
        return d

    def test_timeout_is_not_retried_on_anthropic(self) -> None:
        calls = []

        def stub(td, tr, stage, state, backend_override=None):
            calls.append(backend_override)
            return sra.RC_STAGE_TIMEOUT

        sra._execute_single_stage = stub
        with tempfile.TemporaryDirectory() as tmp:
            d = self._task(tmp)
            rc, _cost, _st = sra._run_pipeline_stage_with_breadcrumbs(
                d, d, "tester", {"iteration": 1}, "tid", 0.0, 20.0, d / "state.json")
            self.assertEqual(rc, sra.RC_STAGE_TIMEOUT)
            self.assertEqual(len(calls), 1)  # NO second 900s window
            self.assertEqual(json.loads((d / "state.json").read_text())["stage"], "failed")

    def test_nontimeout_failure_still_retries_on_anthropic(self) -> None:
        # proves the guard is specific to timeouts, not a blanket no-retry.
        seq = [1, 0]
        calls = []

        def stub(td, tr, stage, state, backend_override=None):
            calls.append(backend_override)
            return seq[len(calls) - 1]

        sra._execute_single_stage = stub
        sra._resolve_stage_backend = lambda *a, **k: ("deepseek", None)
        sra._canonicalize_stage_artifact = lambda *a, **k: None
        sra._mirror_to_specs_folder = lambda *a, **k: None
        sra._token_cap_exceeded = lambda *a, **k: False
        with tempfile.TemporaryDirectory() as tmp:
            d = self._task(tmp)
            rc, _cost, _st = sra._run_pipeline_stage_with_breadcrumbs(
                d, d, "tester", {"iteration": 1}, "tid", 0.0, 20.0, d / "state.json")
            self.assertEqual(rc, 0)
            self.assertEqual(calls, [None, "anthropic"])  # retried on anthropic


class TerminatePassthroughTests(_Patched):
    def test_costcap_and_clarify_pass_through_without_handoff(self) -> None:
        seen = []
        sra._handoff_terminal = lambda *a, **k: seen.append(k.get("rc")) or 99
        self.assertEqual(
            sra._terminate_pipeline(Path("/tmp"), Path("/tmp"), "t", 5, 0.0), 5)
        self.assertEqual(
            sra._terminate_pipeline(Path("/tmp"), Path("/tmp"), "t",
                                    sra._CLARIFY_PAUSE_RC, 0.0),
            sra._CLARIFY_PAUSE_RC)
        self.assertEqual(seen, [])  # handoff NOT invoked for caps / clarify

    def test_stage_failure_routes_through_handoff(self) -> None:
        seen = []
        sra._handoff_terminal = lambda *a, **k: seen.append(k.get("rc")) or sra._HANDOFF_RC
        rc = sra._terminate_pipeline(Path("/tmp"), Path("/tmp"), "t",
                                     sra.RC_STAGE_TIMEOUT, 0.0)
        self.assertEqual(rc, sra._HANDOFF_RC)
        self.assertEqual(seen, [sra.RC_STAGE_TIMEOUT])


class HandoffTerminalTests(_Patched):
    TID = "tg-handoff-0001"

    def test_with_pr_goes_to_awaiting_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sra._AWAITING_INPUT_DIR = root / "awaiting-input"
            sra._FAILED_DIR = root / "failed"
            d = root / "active" / self.TID
            d.mkdir(parents=True)
            (d / "state.json").write_text(json.dumps({
                "iteration": 2, "stage": "failed",
                "pr_url": "https://github.com/o/r/pull/10",
                "review_trend": [2, 2],
            }) + "\n")
            rc = sra._handoff_terminal(d, d, self.TID,
                                       rc=sra.RC_STAGE_TIMEOUT, cumulative_cost=1.23)
            self.assertEqual(rc, sra._HANDOFF_RC)
            moved = sra._AWAITING_INPUT_DIR / self.TID
            self.assertTrue(moved.is_dir())
            self.assertFalse(d.exists())  # left active/
            st = json.loads((moved / "state.json").read_text())
            self.assertEqual(st["stage"], "awaiting-input")
            doc = (moved / "UNRESOLVED-FINDINGS.md").read_text()
            self.assertIn("pull/10", doc)
            self.assertIn("[2, 2]", doc)

    def test_no_pr_goes_to_failed(self) -> None:
        sra._recover_pr_from_repo = lambda *a, **k: (None, None)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sra._AWAITING_INPUT_DIR = root / "awaiting-input"
            sra._FAILED_DIR = root / "failed"
            d = root / "active" / self.TID
            d.mkdir(parents=True)
            (d / "state.json").write_text('{"iteration": 1, "stage": "failed"}\n')
            rc = sra._handoff_terminal(d, d, self.TID, rc=2, cumulative_cost=0.0)
            self.assertEqual(rc, sra._HANDOFF_RC)
            moved = sra._FAILED_DIR / self.TID
            self.assertTrue(moved.is_dir())
            self.assertEqual(
                json.loads((moved / "state.json").read_text())["stage"], "failed")
            self.assertTrue((moved / "UNRESOLVED-FINDINGS.md").exists())


if __name__ == "__main__":
    unittest.main()
