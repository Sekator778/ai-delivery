"""Cross-provider rate-limit fallback (2026-06-06).

Three parallel anthropic dev tasks all hit the Claude 5-hour session limit at the
same instant (2026-06-05) and dead-ended: the legacy auto-fallback only retried
cheap→anthropic, so a stage already on anthropic got NO retry, and the handoff
mislabelled the quota stall as "crash; anthropic fallback also failed".

Pinned here:
  1. _stage_hit_rate_limit detects a 429 / "five_hour" / session-limit log and
     does NOT fire on an ordinary crash log.
  2. _rate_limit_fallback_chain offers the OTHER providers (independent quota),
     skipping the rate-limited backend and any whose key is unset.
  3. A rate-limited stage retries across providers; if every provider is
     exhausted it returns RC_RATE_LIMITED (honest), not rc=1 (crash).
  4. A provider that recovers stops the chain (no needless further attempts).
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import stage_runner_agent as sra  # noqa: E402

_RL_LOG = (
    'event {"type":"result","is_error":true,"api_error_status":429,'
    '"result":"You\'ve hit your session limit",'
    '"rate_limit_info":{"rateLimitType":"five_hour"}}'
)
_CRASH_LOG = "stage=ba  reason=orchestrator exited rc=1\nTraceback: KeyError 'foo'\n"


class RateLimitDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.d = Path(tempfile.mkdtemp())

    def test_detects_429_session_limit(self) -> None:
        (self.d / "developer.claude-error.log").write_text(_RL_LOG)
        self.assertTrue(sra._stage_hit_rate_limit(self.d, "developer"))

    def test_ignores_plain_crash(self) -> None:
        (self.d / "ba.claude-error.log").write_text(_CRASH_LOG)
        self.assertFalse(sra._stage_hit_rate_limit(self.d, "ba"))

    def test_missing_log_is_not_rate_limited(self) -> None:
        self.assertFalse(sra._stage_hit_rate_limit(self.d, "tester"))


class FallbackChainTests(unittest.TestCase):
    def test_anthropic_falls_to_other_providers(self) -> None:
        with _env(DEEPSEEK_API_KEY="x", GLM_API_KEY="y"):
            self.assertEqual(
                sra._rate_limit_fallback_chain("anthropic"), ["deepseek", "glm"])

    def test_skips_self_and_keeps_order(self) -> None:
        with _env(DEEPSEEK_API_KEY="x", GLM_API_KEY="y"):
            self.assertEqual(
                sra._rate_limit_fallback_chain("deepseek"), ["anthropic", "glm"])

    def test_skips_backend_with_unset_key(self) -> None:
        with _env(DEEPSEEK_API_KEY="x", GLM_API_KEY=""):
            # GLM key unset → only deepseek remains as an alternate to anthropic
            self.assertEqual(
                sra._rate_limit_fallback_chain("anthropic"), ["deepseek"])

    def test_honest_handoff_reason_distinct_from_crash(self) -> None:
        self.assertEqual(sra.RC_RATE_LIMITED, 125)
        self.assertIn(sra.RC_RATE_LIMITED, sra._HANDOFF_REASONS)
        self.assertNotEqual(
            sra._HANDOFF_REASONS[sra.RC_RATE_LIMITED], sra._HANDOFF_REASONS[1])
        self.assertIn("rate limit", sra._HANDOFF_REASONS[sra.RC_RATE_LIMITED].lower())


class FallbackIntegrationTests(unittest.TestCase):
    """Drive the real _run_pipeline_stage_with_breadcrumbs with the stage
    executor stubbed, so the fallback wiring is exercised end-to-end."""

    def setUp(self) -> None:
        self.d = Path(tempfile.mkdtemp())
        self.target = Path(tempfile.mkdtemp())
        # stage 'tester' runs after BA → the BRD precheck needs 01-ba.md
        (self.d / "01-ba.md").write_text("# BRD\n")
        (self.d / "spec.json").write_text(json.dumps(
            {"task_id": "t-rl", "trigger": "windmill", "user": "op",
             "prompt": "x", "target_repo": str(self.target),
             "model_routing": {"tester": "anthropic"}}))
        (self.d / "state.json").write_text(json.dumps(
            {"id": "t-rl", "stage": "tester", "iteration": 1,
             "model_routing": {"tester": "anthropic"}, "cost_usd": 0.0}))
        self.state = {"iteration": 1, "model_routing": {"tester": "anthropic"},
                      "tier": None}
        # silence side-effects, keep _update_state real (writes state.json)
        self._orig = {n: getattr(sra, n) for n in (
            "_execute_single_stage", "_read_stage_cost_usd", "_send_telegram",
            "_notify_bot", "_canonicalize_stage_artifact", "_mirror_to_specs_folder",
            "_token_cap_exceeded")}
        sra._read_stage_cost_usd = lambda *a, **k: 0.0
        sra._send_telegram = lambda *a, **k: None
        sra._notify_bot = lambda *a, **k: None
        sra._canonicalize_stage_artifact = lambda *a, **k: None
        sra._mirror_to_specs_folder = lambda *a, **k: None
        sra._token_cap_exceeded = lambda *a, **k: False

    def tearDown(self) -> None:
        for n, v in self._orig.items():
            setattr(sra, n, v)

    def _run(self):
        return sra._run_pipeline_stage_with_breadcrumbs(
            self.d, self.target, "tester", self.state, "t-rl",
            0.0, 100.0, self.d / "state.json")

    def test_exhausted_returns_rate_limited(self) -> None:
        calls = []

        def stub(task_dir, target, stage, state, backend_override=None):
            calls.append(backend_override)
            (task_dir / f"{stage}.claude-error.log").write_text(_RL_LOG)
            return 1  # every provider rate-limited

        sra._execute_single_stage = stub
        with _env(DEEPSEEK_API_KEY="x", GLM_API_KEY="y",
                  RATE_LIMIT_CROSS_PROVIDER_FALLBACK="1"):
            rc, _cost, _state = self._run()

        self.assertEqual(rc, sra.RC_RATE_LIMITED)
        # initial anthropic attempt, then the two independent-quota providers
        self.assertEqual(calls, [None, "deepseek", "glm"])
        st = json.loads((self.d / "state.json").read_text())
        self.assertEqual(st["stage"], "failed")

    def test_recovers_on_first_alternate_provider(self) -> None:
        calls = []

        def stub(task_dir, target, stage, state, backend_override=None):
            calls.append(backend_override)
            if backend_override is None:
                (task_dir / f"{stage}.claude-error.log").write_text(_RL_LOG)
                return 1  # anthropic rate-limited
            return 0  # deepseek has independent quota → succeeds

        sra._execute_single_stage = stub
        with _env(DEEPSEEK_API_KEY="x", GLM_API_KEY="y",
                  RATE_LIMIT_CROSS_PROVIDER_FALLBACK="1"):
            rc, _cost, _state = self._run()

        self.assertEqual(rc, 0)
        # stopped at the first provider that recovered — glm never tried
        self.assertEqual(calls, [None, "deepseek"])

    def test_opt_out_disables_cross_provider(self) -> None:
        calls = []

        def stub(task_dir, target, stage, state, backend_override=None):
            calls.append(backend_override)
            (task_dir / f"{stage}.claude-error.log").write_text(_RL_LOG)
            return 1

        sra._execute_single_stage = stub
        with _env(DEEPSEEK_API_KEY="x", GLM_API_KEY="y",
                  RATE_LIMIT_CROSS_PROVIDER_FALLBACK="0"):
            rc, _cost, _state = self._run()

        # no cross-provider retry; still reported honestly as rate-limited
        self.assertEqual(calls, [None])
        self.assertEqual(rc, sra.RC_RATE_LIMITED)


class _env:
    """Context manager: set/restore os.environ keys for a test."""

    def __init__(self, **kv):
        self.kv = kv
        self._saved: dict = {}

    def __enter__(self):
        import os
        for k, v in self.kv.items():
            self._saved[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *exc):
        import os
        for k, old in self._saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


if __name__ == "__main__":
    unittest.main()
