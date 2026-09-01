"""Honest provider pricing for non-anthropic backends (plan step 1).

The claude CLI prices every endpoint at Anthropic rates: a real DeepSeek
tester stage cost $0.05 while the CLI reported $1.12 (~22x), and that figure
fed cost_cap_usd. backend_routing.apply_backend_pricing recomputes the stage
cost from the token counts times the provider price table at the one point
where cost enters the artifact. These tests pin the contract: computation,
passthrough, fallback visibility, env override, and non-mutation.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

from backend_routing import apply_backend_pricing  # noqa: E402

# input=1000, output=2000, cache_read=1M, cache_write=0 at deepseek-v4-pro
# rates effective 2026-08-17 (1.32 / 3.96 / 0.044 per 1M):
# 0.00132 + 0.00792 + 0.044
_PRO_EXPECTED = 0.05324

_USAGE = {
    "total_cost_usd": 1.12,
    "input_tokens": 1000,
    "output_tokens": 2000,
    "cache_read_tokens": 1_000_000,
    "cache_creation_tokens": 0,
    "session_id": "s-1",
}


class DeepseekPricingTests(unittest.TestCase):
    def setUp(self) -> None:
        # Pin the model resolution regardless of the operator's env.
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for var in ("DEEPSEEK_MODEL_PRIMARY", "BACKEND_PRICES_JSON"):
            os.environ.pop(var, None)

    def test_deepseek_cost_is_computed_from_tokens(self) -> None:
        out = apply_backend_pricing("deepseek", _USAGE)
        self.assertAlmostEqual(out["total_cost_usd"], _PRO_EXPECTED, places=9)
        self.assertEqual(out["cost_source"], "computed:deepseek-v4-pro")
        # The CLI's inflated figure must survive for comparability.
        self.assertEqual(out["cli_reported_cost_usd"], 1.12)

    def test_primary_model_env_selects_the_price_row(self) -> None:
        os.environ["DEEPSEEK_MODEL_PRIMARY"] = "deepseek-v4-flash"
        out = apply_backend_pricing("deepseek", _USAGE)
        # 0.00044 + 0.00264 + 0.014 at flash rates (2026-08-17)
        self.assertAlmostEqual(out["total_cost_usd"], 0.01708, places=9)
        self.assertEqual(out["cost_source"], "computed:deepseek-v4-flash")

    def test_prices_json_override_wins(self) -> None:
        os.environ["BACKEND_PRICES_JSON"] = (
            '{"deepseek": {"deepseek-v4-pro": {"input": 1.0, "output": 1.0,'
            ' "cache_read": 1.0, "cache_write": 1.0}}}'
        )
        out = apply_backend_pricing("deepseek", _USAGE)
        # (1000 + 2000 + 1M) tokens at $1/M flat
        self.assertAlmostEqual(out["total_cost_usd"], 1.003, places=9)

    def test_broken_prices_json_falls_back_to_builtin(self) -> None:
        os.environ["BACKEND_PRICES_JSON"] = "{not json"
        out = apply_backend_pricing("deepseek", _USAGE)
        self.assertAlmostEqual(out["total_cost_usd"], _PRO_EXPECTED, places=9)

    def test_missing_token_fields_count_as_zero(self) -> None:
        out = apply_backend_pricing(
            "deepseek", {"total_cost_usd": 0.5, "output_tokens": 2000})
        # 2000 output tokens at 3.96/M, everything else absent = 0
        self.assertAlmostEqual(out["total_cost_usd"], 0.00792, places=9)

    def test_input_is_not_mutated(self) -> None:
        original = dict(_USAGE)
        apply_backend_pricing("deepseek", _USAGE)
        self.assertEqual(_USAGE, original)


class PassthroughTests(unittest.TestCase):
    def test_anthropic_keeps_the_cli_figure(self) -> None:
        out = apply_backend_pricing("anthropic", dict(_USAGE))
        self.assertEqual(out["total_cost_usd"], 1.12)
        self.assertEqual(out["cost_source"], "cli")
        self.assertNotIn("cli_reported_cost_usd", out)

    def test_backend_without_price_table_stays_visible(self) -> None:
        # glm has no table entry: keep the CLI figure but LABEL it, so the
        # inflation is a queryable fact in the ledger, not a silent lie.
        out = apply_backend_pricing("glm", dict(_USAGE))
        self.assertEqual(out["total_cost_usd"], 1.12)
        self.assertEqual(out["cost_source"], "cli-no-price-table:glm")

    def test_empty_cost_info_passes_through(self) -> None:
        self.assertEqual(apply_backend_pricing("deepseek", {}), {})


if __name__ == "__main__":
    unittest.main()
