"""Limit-outage detection, parking and auto-requeue (issue #11).

Incident 2026-08-12: the Claude Max window was exhausted mid-stage, the claude
CLI silently retried the inner Agent call for 30 minutes, the stage died on
STAGE_TIMEOUT_SEC (rc=124) and the task was marked FAILED — $4.42, no artifact.

Covered here:
  * LimitStallWatch classification — api-error marker, silence+retries, and the
    negatives that keep a healthy (or merely slow) stage alive.
  * reset-time parsing (RFC3339 `*-reset`, `retry-after`, CLI `resets at <t>`)
    and the resume_at policy around it.
  * park / requeue decisions: the pure is_due gate, the park side effects, and
    the watcher sweep that moves a due task back to inbox/.
  * the live stream reader in stage_runner_agent — a real child process proves
    the detector fires WHILE the stage runs (the old subprocess.run only handed
    the stream over after the child exited, which is why nothing saw the storm).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import limit_stall as ls  # noqa: E402
import stage_runner_agent as sra  # noqa: E402
import watcher as w  # noqa: E402


def _assistant(text: str = "working") -> str:
    return json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    })


def _tool_result(text: str = "ok", is_error: bool = False) -> str:
    return json.dumps({
        "type": "user",
        "is_error": is_error,
        "message": {"content": [{"type": "tool_result", "content": text}]},
    })


def _retry(attempt: int) -> str:
    return json.dumps({
        "type": "tool_progress",
        "subagent_retry": {"attempt": attempt},
        "elapsed_time_seconds": 0,
    })


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


class DetectorTests(unittest.TestCase):
    def _watch(self, now: float = 1000.0) -> ls.LimitStallWatch:
        return ls.LimitStallWatch(window_sec=300, min_retries=2, now=now)

    def test_api_error_marker_classifies_immediately(self) -> None:
        watch = self._watch()
        watch.feed(_assistant(), now=1001.0)
        self.assertIsNone(watch.verdict(now=1002.0))
        watch.feed(
            '{"type":"system","subtype":"api_error","api_error_status":429}',
            now=1003.0)
        self.assertEqual(watch.verdict(now=1003.0), ls.VERDICT_API_ERROR)

    def test_marker_variants_all_match(self) -> None:
        for line in (
            '{"type":"system","api_error_status": 429}',
            '{"type":"result","error":{"type":"overloaded_error"}}',
            "API Error: 429 Too Many Requests",
            "Claude AI usage limit reached",
            "You've hit your session limit",
        ):
            watch = self._watch()
            watch.feed(line, now=1001.0)
            self.assertEqual(watch.verdict(now=1001.0), ls.VERDICT_API_ERROR,
                             f"missed marker in: {line}")

    def test_model_text_quoting_a_marker_does_not_trip(self) -> None:
        """The stream carries tool_result payloads — grepping our own sources
        for 'rate limit exceeded' must not kill a healthy stage."""
        watch = self._watch()
        watch.feed(_assistant("the docs mention rate limit exceeded handling"),
                   now=1001.0)
        watch.feed(_tool_result("dispatcher/x.py: # 429 too many requests"),
                   now=1002.0)
        self.assertIsNone(watch.verdict(now=1002.0))

    def test_tool_result_flagged_is_error_does_trip(self) -> None:
        watch = self._watch()
        watch.feed(_tool_result("API Error: 429 Too Many Requests", is_error=True),
                   now=1001.0)
        self.assertEqual(watch.verdict(now=1001.0), ls.VERDICT_API_ERROR)

    def test_retry_storm_plus_silence_classifies_as_stall(self) -> None:
        """The incident's exact shape: subagent_retry events accumulate, no
        assistant / tool-result event for the whole window."""
        watch = self._watch()
        watch.feed(_assistant(), now=1001.0)
        for i, ts in enumerate((1010.0, 1100.0, 1200.0), start=1):
            watch.feed(_retry(i), now=ts)
        self.assertIsNone(watch.verdict(now=1200.0))          # inside the window
        self.assertEqual(watch.verdict(now=1301.1), ls.VERDICT_STALL)
        self.assertEqual(watch.retries, 3)

    def test_silence_without_retries_is_not_a_stall(self) -> None:
        """A slow-but-working stage (one long tool call) must not be killed."""
        watch = self._watch()
        watch.feed(_assistant(), now=1001.0)
        self.assertIsNone(watch.verdict(now=9999.0))

    def test_retries_below_threshold_are_not_a_stall(self) -> None:
        watch = self._watch()
        watch.feed(_assistant(), now=1001.0)
        watch.feed(_retry(1), now=1002.0)
        self.assertIsNone(watch.verdict(now=9999.0))

    def test_healthy_stream_never_classifies(self) -> None:
        watch = self._watch()
        now = 1000.0
        for _ in range(20):
            now += 120.0                       # slower than the 300 s window…
            watch.feed(_assistant(), now=now)
            now += 60.0
            watch.feed(_tool_result(), now=now)  # …but always progressing
            self.assertIsNone(watch.verdict(now=now))
        watch.feed(json.dumps({"type": "result", "total_cost_usd": 1.5}), now=now)
        self.assertIsNone(watch.verdict(now=now))
        self.assertEqual(watch.retries, 0)

    def test_retry_event_does_not_count_as_progress(self) -> None:
        watch = self._watch()
        watch.feed(_assistant(), now=1000.0)
        watch.feed(_retry(1), now=1200.0)
        watch.feed(_retry(2), now=1250.0)
        self.assertEqual(watch.last_progress, 1000.0)
        self.assertEqual(watch.verdict(now=1301.0), ls.VERDICT_STALL)

    def test_blank_and_non_json_lines_are_harmless(self) -> None:
        watch = self._watch()
        watch.feed("", now=1001.0)
        watch.feed("   \n", now=1001.0)
        watch.feed("not json at all", now=1001.0)
        watch.feed('{"broken": ', now=1001.0)
        self.assertIsNone(watch.verdict(now=1002.0))
        self.assertEqual(watch.progress_events, 0)


# ---------------------------------------------------------------------------
# Reset-time parsing
# ---------------------------------------------------------------------------


class ResetParsingTests(unittest.TestCase):
    NOW = 1_700_000_000.0

    def test_rfc3339_ratelimit_reset_header(self) -> None:
        line = ('{"headers":{"anthropic-ratelimit-requests-reset":'
                '"2026-08-12T22:00:00Z"}}')
        expected = datetime(2026, 8, 12, 22, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(ls.parse_reset_at(line, self.NOW), expected)

    def test_rfc3339_with_offset(self) -> None:
        line = "anthropic-ratelimit-tokens-reset: 2026-08-12T22:00:00+02:00"
        expected = datetime(
            2026, 8, 12, 22, 0,
            tzinfo=timezone(timedelta(hours=2))).timestamp()
        self.assertEqual(ls.parse_reset_at(line, self.NOW), expected)

    def test_retry_after_seconds(self) -> None:
        self.assertEqual(ls.parse_reset_at("retry-after: 120", self.NOW),
                         self.NOW + 120)
        self.assertEqual(ls.parse_reset_at('{"retry_after": 45}', self.NOW),
                         self.NOW + 45)

    def test_cli_resets_at_clock(self) -> None:
        """The CLI prints local wall-clock; resolve to the NEXT occurrence."""
        now = datetime(2026, 8, 12, 17, 46).timestamp()
        got = ls.parse_reset_at("Claude AI usage limit reached — resets at 9pm", now)
        self.assertIsNotNone(got)
        self.assertEqual(datetime.fromtimestamp(got).hour, 21)
        self.assertGreater(got, now)

    def test_cli_resets_at_rolls_to_tomorrow_when_past(self) -> None:
        now = datetime(2026, 8, 12, 23, 30).timestamp()
        got = ls.parse_reset_at("resets at 9:15am", now)
        self.assertIsNotNone(got)
        self.assertGreater(got, now)
        self.assertLess(got - now, 24 * 3600)
        self.assertEqual(datetime.fromtimestamp(got).day, 13)

    def test_rfc3339_wins_over_retry_after(self) -> None:
        line = ('retry-after: 30; anthropic-ratelimit-requests-reset='
                '"2026-08-12T22:00:00Z"')
        expected = datetime(2026, 8, 12, 22, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(ls.parse_reset_at(line, self.NOW), expected)

    def test_no_reset_information(self) -> None:
        self.assertIsNone(ls.parse_reset_at("", self.NOW))
        self.assertIsNone(ls.parse_reset_at(_retry(3), self.NOW))
        self.assertIsNone(ls.parse_reset_at("resets at 99:99", self.NOW))

    def test_resume_at_falls_back_to_flat_backoff(self) -> None:
        self.assertEqual(ls.resume_at_for(_retry(2), self.NOW, backoff_min=30),
                         self.NOW + 1800)

    def test_resume_at_uses_parsed_reset(self) -> None:
        self.assertEqual(
            ls.resume_at_for("retry-after: 600", self.NOW, backoff_min=30),
            self.NOW + 600)

    def test_resume_at_ignores_a_reset_in_the_past(self) -> None:
        line = 'anthropic-ratelimit-requests-reset="2020-01-01T00:00:00Z"'
        self.assertEqual(ls.resume_at_for(line, self.NOW, backoff_min=30),
                         self.NOW + 1800)

    def test_resume_at_caps_an_absurd_reset(self) -> None:
        line = 'anthropic-ratelimit-requests-reset="2099-01-01T00:00:00Z"'
        self.assertEqual(ls.resume_at_for(line, self.NOW, backoff_min=30),
                         self.NOW + ls.LIMIT_PARK_MAX_HOURS * 3600)

    def test_to_epoch_accepts_both_persisted_shapes(self) -> None:
        iso = "2026-08-12T22:00:00+00:00"
        self.assertEqual(ls.to_epoch(iso),
                         datetime(2026, 8, 12, 22, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(ls.to_epoch(1700.5), 1700.5)
        self.assertIsNone(ls.to_epoch(None))
        self.assertIsNone(ls.to_epoch(""))
        self.assertIsNone(ls.to_epoch("not a time"))


# ---------------------------------------------------------------------------
# Requeue decision (pure)
# ---------------------------------------------------------------------------


class IsDueTests(unittest.TestCase):
    NOW = 1_700_000_000.0

    def test_only_parked_tasks_are_candidates(self) -> None:
        for stage in ("developer", "awaiting-input", "failed", "done", ""):
            self.assertFalse(
                ls.is_due({"stage": stage, "resume_at": self.NOW - 1}, self.NOW),
                f"stage={stage} must not be un-parked")

    def test_not_due_before_resume_at(self) -> None:
        state = {"stage": ls.PARKED_STAGE,
                 "resume_at": ls._iso(self.NOW + 600)}
        self.assertFalse(ls.is_due(state, self.NOW))

    def test_due_after_resume_at(self) -> None:
        state = {"stage": ls.PARKED_STAGE,
                 "resume_at": ls._iso(self.NOW - 1)}
        self.assertTrue(ls.is_due(state, self.NOW))

    def test_missing_resume_at_is_due(self) -> None:
        self.assertTrue(ls.is_due({"stage": ls.PARKED_STAGE}, self.NOW))

    def test_park_budget_exhausted_waits_for_the_operator(self) -> None:
        state = {"stage": ls.PARKED_STAGE,
                 "resume_at": ls._iso(self.NOW - 1),
                 "limit_parks": ls.LIMIT_PARK_LIMIT}
        self.assertFalse(ls.is_due(state, self.NOW))
        state["limit_parks"] = ls.LIMIT_PARK_LIMIT - 1
        self.assertTrue(ls.is_due(state, self.NOW))

    def test_none_state(self) -> None:
        self.assertFalse(ls.is_due(None, self.NOW))


# ---------------------------------------------------------------------------
# park() side effects
# ---------------------------------------------------------------------------


class ParkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self._saved = ls._AWAITING_INPUT_DIR
        ls._AWAITING_INPUT_DIR = self.root / "awaiting-input"
        self.active = self.root / "active"
        self.active.mkdir(parents=True)
        self.task_dir = self.active / "TASK-LS-1"
        self.task_dir.mkdir()
        (self.task_dir / "state.json").write_text(
            json.dumps({"stage": "architect", "cost_usd": 4.42}))
        self.notified: list = []
        self.telegram: list = []
        self._notify, self._send = ls._notify_bot, ls._send_telegram
        ls._notify_bot = lambda signal, task_id, **kw: self.notified.append(
            (signal, task_id, kw))
        ls._send_telegram = self.telegram.append

    def tearDown(self) -> None:
        ls._AWAITING_INPUT_DIR = self._saved
        ls._notify_bot, ls._send_telegram = self._notify, self._send
        shutil.rmtree(self.root, ignore_errors=True)

    def test_park_moves_to_awaiting_input_and_stamps_resume_at(self) -> None:
        resume_at = time.time() + 1800
        dst = ls.park(self.task_dir, "TASK-LS-1", stage="architect",
                      reason="limits exhausted", resume_at=resume_at,
                      hint="resets at 9pm", cost_usd=4.42)

        self.assertFalse(self.task_dir.exists())
        self.assertEqual(dst, ls._AWAITING_INPUT_DIR / "TASK-LS-1")
        state = json.loads((dst / "state.json").read_text())
        self.assertEqual(state["stage"], ls.PARKED_STAGE)
        self.assertAlmostEqual(ls.to_epoch(state["resume_at"]), resume_at, delta=1)
        self.assertEqual(state["limit_stall"]["stage"], "architect")
        self.assertIn("LIMIT-STALL", (dst / "worklog.md").read_text())

    def test_park_notifies_the_owner(self) -> None:
        ls.park(self.task_dir, "TASK-LS-1", stage="architect", reason="r",
                resume_at=time.time() + 60, cost_usd=1.0)
        self.assertEqual(len(self.notified), 1)
        signal, task_id, payload = self.notified[0]
        self.assertEqual((signal, task_id), ("limit_stall_parked", "TASK-LS-1"))
        self.assertEqual(payload["failed_stage"], "architect")
        self.assertIn("resume_at", payload)
        self.assertTrue(any("parked until" in m for m in self.telegram))

    def test_second_park_of_the_same_task_is_a_no_op(self) -> None:
        """tester+security run in parallel and can stall together."""
        dst = ls.park(self.task_dir, "TASK-LS-1", stage="tester", reason="r",
                      resume_at=time.time() + 60)
        again = ls.park(dst, "TASK-LS-1", stage="security", reason="r",
                        resume_at=time.time() + 60)
        self.assertEqual(again, dst)
        self.assertEqual(len(self.notified), 1)
        state = json.loads((dst / "state.json").read_text())
        self.assertEqual(state["limit_stall"]["stage"], "tester")


# ---------------------------------------------------------------------------
# Watcher resume sweep
# ---------------------------------------------------------------------------


class ResumeSweepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self._saved = {n: getattr(w, n) for n in ("AWAITING_INPUT_DIR", "INBOX_DIR")}
        w.AWAITING_INPUT_DIR = self.root / "awaiting-input"
        w.INBOX_DIR = self.root / "inbox"
        w.AWAITING_INPUT_DIR.mkdir(parents=True)

    def tearDown(self) -> None:
        for n, v in self._saved.items():
            setattr(w, n, v)
        shutil.rmtree(self.root, ignore_errors=True)

    def _mk(self, task_id: str, **state) -> Path:
        d = w.AWAITING_INPUT_DIR / task_id
        d.mkdir()
        (d / "state.json").write_text(json.dumps(state))
        return d

    def test_due_task_goes_back_to_inbox(self) -> None:
        d = self._mk("TASK-DUE", stage=ls.PARKED_STAGE,
                     resume_at=ls._iso(time.time() - 60),
                     limit_stall={"stage": "architect"})
        w.scan_limit_parked_for_resume()
        self.assertFalse(d.exists())
        moved = w.INBOX_DIR / "TASK-DUE"
        self.assertTrue(moved.is_dir())
        state = json.loads((moved / "state.json").read_text())
        self.assertEqual(state["stage"], "inbox")
        self.assertNotIn("resume_at", state)
        self.assertEqual(state["limit_parks"], 1)

    def test_task_still_waiting_is_left_alone(self) -> None:
        d = self._mk("TASK-WAIT", stage=ls.PARKED_STAGE,
                     resume_at=ls._iso(time.time() + 3600))
        w.scan_limit_parked_for_resume()
        self.assertTrue(d.exists())
        self.assertFalse((w.INBOX_DIR / "TASK-WAIT").exists())

    def test_other_parked_tasks_are_untouched(self) -> None:
        d = self._mk("TASK-OPERATOR", stage="awaiting-input")
        w.scan_limit_parked_for_resume()
        self.assertTrue(d.exists())

    def test_transient_sweep_does_not_jump_a_limit_park(self) -> None:
        """Both sweeps read awaiting-input/; only the limit sweep owns a park."""
        d = self._mk("TASK-BOTH", stage=ls.PARKED_STAGE,
                     resume_at=ls._iso(time.time() + 3600))
        (d / "architect.claude-error.log").write_text(
            "API Error: ConnectionRefused while streaming")
        old = time.time() - w.GRACE_PERIOD - 60
        os.utime(d / "state.json", (old, old))
        w.scan_awaiting_input_for_transient_retry()
        self.assertTrue(d.exists())
        self.assertFalse((w.INBOX_DIR / "TASK-BOTH").exists())

    def test_exhausted_park_budget_stays_for_the_operator(self) -> None:
        d = self._mk("TASK-CAP", stage=ls.PARKED_STAGE,
                     resume_at=ls._iso(time.time() - 60),
                     limit_parks=ls.LIMIT_PARK_LIMIT)
        w.scan_limit_parked_for_resume()
        self.assertTrue(d.exists())

    def test_inbox_collision_does_not_overwrite_a_queued_task(self) -> None:
        self._mk("TASK-DUP", stage=ls.PARKED_STAGE,
                 resume_at=ls._iso(time.time() - 60))
        (w.INBOX_DIR / "TASK-DUP").mkdir(parents=True)
        (w.INBOX_DIR / "TASK-DUP" / "spec.json").write_text("{}")
        w.scan_limit_parked_for_resume()
        self.assertTrue((w.AWAITING_INPUT_DIR / "TASK-DUP").exists())
        self.assertEqual((w.INBOX_DIR / "TASK-DUP" / "spec.json").read_text(), "{}")

    def test_parked_stage_is_terminal_for_the_respawn_logic(self) -> None:
        self.assertIn(ls.PARKED_STAGE, w.TERMINAL_STAGES)
        self.assertTrue(w._is_terminal_stage(ls.PARKED_STAGE))
        self.assertEqual(w._terminal_bucket_for(ls.PARKED_STAGE),
                         w.AWAITING_INPUT_DIR)


# ---------------------------------------------------------------------------
# Runner wiring
# ---------------------------------------------------------------------------


class RunnerContractTests(unittest.TestCase):
    def test_sentinel_is_distinct_and_documented(self) -> None:
        self.assertEqual(sra.RC_LIMIT_STALL, 126)
        self.assertNotIn(sra.RC_LIMIT_STALL,
                         (sra.RC_STAGE_TIMEOUT, sra.RC_RATE_LIMITED))
        self.assertIn(sra.RC_LIMIT_STALL, sra._HANDOFF_REASONS)
        self.assertIn("limit", sra._HANDOFF_REASONS[sra.RC_LIMIT_STALL].lower())

    def test_terminate_pipeline_passes_the_park_through(self) -> None:
        """park() already moved the task; a handoff would re-file it as failed."""
        self.assertEqual(
            sra._terminate_pipeline(Path("/nonexistent"), Path("/nonexistent"),
                                    "TASK-X", sra.RC_LIMIT_STALL, 1.0),
            sra.RC_LIMIT_STALL)


class LiveStreamReaderTests(unittest.TestCase):
    """The whole point of #11: the stall must be visible WHILE the child runs.

    Each case spawns a real child that writes stream-json to stdout and then
    hangs, and asserts the reader kills it in seconds instead of waiting out the
    30-minute stage timeout."""

    def setUp(self) -> None:
        self._saved = (ls.STALL_WINDOW_SEC, ls.STALL_MIN_RETRIES, sra._STALL_POLL_SEC)
        ls.STALL_WINDOW_SEC, ls.STALL_MIN_RETRIES = 1, 2
        sra._STALL_POLL_SEC = 0.2

    def tearDown(self) -> None:
        (ls.STALL_WINDOW_SEC, ls.STALL_MIN_RETRIES,
         sra._STALL_POLL_SEC) = self._saved

    @staticmethod
    def _child(body: str) -> list:
        return [sys.executable, "-c",
                "import sys, time\n" + body + "\nsys.stdout.flush()\ntime.sleep(120)\n"]

    def test_api_error_line_kills_the_child_early(self) -> None:
        argv = self._child(
            'sys.stdout.write(\'{"type":"system","subtype":"init"}\\n\')\n'
            'sys.stdout.write(\'{"type":"system","api_error_status":429,'
            '"message":"retry-after: 120"}\\n\')')
        t0 = time.time()
        proc, reason, hint = sra._run_claude_stage(
            argv, env=dict(os.environ), timeout=120, stage="architect")
        self.assertEqual(reason, ls.VERDICT_API_ERROR)
        self.assertLess(time.time() - t0, 30)
        self.assertNotEqual(proc.returncode, 0)          # terminated, not exited
        self.assertIn("api_error_status", proc.stdout)   # stream still captured
        self.assertIn("retry-after", hint)
        self.assertEqual(ls.resume_at_for(hint, 1000.0), 1120.0)

    def test_retry_storm_kills_the_child_early(self) -> None:
        argv = self._child(
            'sys.stdout.write(\'{"type":"assistant","message":{}}\\n\')\n'
            'sys.stdout.write(\'{"type":"tool_progress","subagent_retry":'
            '{"attempt":1},"elapsed_time_seconds":0}\\n\')\n'
            'sys.stdout.write(\'{"type":"tool_progress","subagent_retry":'
            '{"attempt":2},"elapsed_time_seconds":0}\\n\')')
        t0 = time.time()
        proc, reason, _hint = sra._run_claude_stage(
            argv, env=dict(os.environ), timeout=120, stage="architect")
        self.assertEqual(reason, ls.VERDICT_STALL)
        self.assertLess(time.time() - t0, 30)
        self.assertNotEqual(proc.returncode, 0)

    def test_healthy_child_is_not_disturbed(self) -> None:
        argv = [sys.executable, "-c",
                'import sys\n'
                'sys.stdout.write(\'{"type":"assistant","message":{}}\\n\')\n'
                'sys.stdout.write(\'{"type":"result","total_cost_usd":0.5}\\n\')\n']
        proc, reason, _hint = sra._run_claude_stage(
            argv, env=dict(os.environ), timeout=60, stage="architect")
        self.assertIsNone(reason)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(sra._parse_cost(proc.stdout).get("total_cost_usd"), 0.5)

    def test_wall_clock_timeout_still_raises(self) -> None:
        """RC_STAGE_TIMEOUT's path is unchanged: a silent, retry-free hang."""
        argv = self._child('sys.stdout.write(\'{"type":"assistant","message":{}}\\n\')')
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            sra._run_claude_stage(argv, env=dict(os.environ), timeout=1,
                                  stage="architect")
        self.assertIn("assistant", caught.exception.output or "")

    def test_detector_can_be_switched_off(self) -> None:
        argv = [sys.executable, "-c", 'print("{\\"type\\":\\"result\\"}")']
        os.environ["LIMIT_STALL_DETECT"] = "0"
        try:
            proc, reason, _hint = sra._run_claude_stage(
                argv, env=dict(os.environ), timeout=60, stage="architect")
        finally:
            os.environ.pop("LIMIT_STALL_DETECT", None)
        self.assertIsNone(reason)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("result", proc.stdout)


if __name__ == "__main__":
    unittest.main()


class RateLimitEventStructuralTests(unittest.TestCase):
    """Regression for the 2026-08-13 false positive: the CLI's INFORMATIONAL
    rate_limit_event (status "allowed") matched the old five_hour word-marker
    and killed healthy stages within seconds."""

    INCIDENT_LINE = ('{"type":"rate_limit_event","rate_limit_info":{"status":"allowed",'
                     '"resetsAt":%d,"rateLimitType":"five_hour","overageStatus":"rejected",'
                     '"overageDisabledReason":"org_level_disabled","isUsingOverage":false},'
                     '"uuid":"x","session_id":"y"}')

    def test_allowed_event_is_not_a_limit_signal(self):
        now = 1_786_650_000.0
        watch = ls.LimitStallWatch(now=now)
        watch.feed(self.INCIDENT_LINE % int(now + 1800), now=now + 1)
        self.assertIsNone(watch.verdict(now=now + 2))
        self.assertEqual(watch.reset_at_hint, now + 1800)

    def test_non_allowed_status_is_a_limit_signal(self):
        now = 1_786_650_000.0
        line = self.INCIDENT_LINE.replace('"status":"allowed"', '"status":"rejected"')
        watch = ls.LimitStallWatch(now=now)
        watch.feed(line % int(now + 900), now=now + 1)
        self.assertEqual(watch.verdict(now=now + 2), ls.VERDICT_API_ERROR)
        self.assertEqual(watch.reset_at_hint, now + 900)

    # The real payload from the 2026-08-15 false positive, verbatim from
    # tasks/.../reviewer.claude-error.log. status "allowed_warning" means the
    # request WENT THROUGH and utilization merely crossed surpassedThreshold.
    WARNING_LINE = ('{"type":"rate_limit_event","rate_limit_info":'
                    '{"status":"allowed_warning","resetsAt":%d,'
                    '"rateLimitType":"five_hour","utilization":0.91,'
                    '"isUsingOverage":false,"surpassedThreshold":0.9},'
                    '"uuid":"7","session_id":"y"}')

    def test_allowed_warning_is_not_a_limit_signal(self):
        # The equality check `status != "allowed"` treated this as exhaustion:
        # it killed a reviewer stage 136s in with all three lenses mid-flight,
        # parked the task for 90 minutes and told the operator limits were
        # gone, with 9% of the five-hour window still available.
        now = 1_786_789_000.0
        watch = ls.LimitStallWatch(now=now)
        watch.feed(self.WARNING_LINE % int(now + 7400), now=now + 1)
        self.assertIsNone(watch.verdict(now=now + 2))

    def test_allowed_warning_still_yields_the_reset_hint(self):
        # Not a stall, but the window boundary is still the best resume clock
        # if a genuine limit arrives later in the same stage.
        now = 1_786_789_000.0
        watch = ls.LimitStallWatch(now=now)
        watch.feed(self.WARNING_LINE % int(now + 7400), now=now + 1)
        self.assertEqual(watch.reset_at_hint, now + 7400)

    def test_unknown_status_still_parks(self):
        # Fail-safe direction: the rule is "anything that does not say allowed
        # blocks", not an enumeration of known blocking statuses, so a status
        # upstream invents tomorrow parks the task rather than being ignored.
        now = 1_786_789_000.0
        line = self.WARNING_LINE.replace('"allowed_warning"', '"throttled_soft"')
        watch = ls.LimitStallWatch(now=now)
        watch.feed(line % int(now + 900), now=now + 1)
        self.assertEqual(watch.verdict(now=now + 2), ls.VERDICT_API_ERROR)

    def test_five_hour_string_alone_no_longer_matches(self):
        self.assertFalse(ls.has_limit_marker(
            'some log line mentioning "rateLimitType":"five_hour" in passing', None))

    def test_parse_reset_at_prefers_structured_epoch(self):
        now = 1_786_650_000.0
        text = 'blah "resetsAt":%d blah' % int(now + 1234)
        self.assertEqual(ls.parse_reset_at(text, now=now), now + 1234)

    def test_parse_reset_at_rejects_implausible_epoch(self):
        now = 1_786_650_000.0
        # ten-digit but far in the past — must not be taken as the reset time
        self.assertIsNone(ls.parse_reset_at('"resetsAt":1000000000', now=now))
