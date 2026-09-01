"""Adaptive complexity triage — policy + routing tests.

Pins the committee RFC's decision rules
(STATE/ARCH-REVIEW-2026-05-29-adaptive-triage-COMMITTEE-RFC.md) at $0, no
`claude` calls:

- the deterministic pre-scan extracts paths + detects path-risk;
- the Q3 policy table assigns the right tier for each branch (risk dominates;
  size L / underspecified / low-confidence → L; clear-tiny → S; else M);
- routing never drops the review/test/security/developer core;
- the runner is backward-compatible: TRIAGE_MODE off (or the kill switch) is
  byte-identical to the pre-triage pipeline;
- the upgrade ladder steps S→M→L and only raises caps.
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

import triage as t  # noqa: E402
import stage_runner_agent as sra  # noqa: E402


# A neutral thresholds object so tests don't depend on ambient env.
THR = t.Thresholds()


class PreScanTests(unittest.TestCase):
    def test_extracts_paths_and_low_risk(self) -> None:
        pre = t.prescan("add a function power(base, exp) to app/calc.py with 4 tests", None)
        self.assertIn("app/calc.py", pre.files)
        self.assertEqual(pre.risk, "low")
        self.assertTrue(pre.small_scope)
        self.assertFalse(pre.large_scope)

    def test_risk_flag_is_deterministic_and_dominates(self) -> None:
        for text, flag in [
            ("add a login check to auth/session.py", "auth"),
            ("encrypt the token before storing", "crypto"),
            ("write a DB migration to alter table users", "migration"),
            # Was "update the billing/checkout charge flow" — which _mask_identifiers
            # ate as a path ("billing/checkout"), so the flag actually came from the
            # bare `charge`. T14 narrowed that word (it is how "the CLI charges every
            # session" forced tier=L), so the fixture now uses payment vocabulary
            # that survives masking.
            ("fix the Stripe checkout flow", "payment"),
            ("tweak .github/workflows/ci.yml", "ci_cd"),
        ]:
            pre = t.prescan(text, None)
            self.assertEqual(pre.risk, "high", text)
            self.assertIn(flag, pre.risk_flags, text)

    def test_large_scope_verb_detected(self) -> None:
        pre = t.prescan("refactor the entire reporting subsystem", None)
        self.assertTrue(pre.large_scope)

    def test_existing_file_loc_counted(self) -> None:
        d = Path(tempfile.mkdtemp())
        (d / "calc.py").write_text("a\nb\nc\n")
        pre = t.prescan("fix calc.py", d)
        self.assertIn("calc.py", pre.existing)
        self.assertEqual(pre.loc_existing, 3)


class IdentifierMaskingTests(unittest.TestCase):
    """GH issue #5, item 1: risk keywords must not fire inside identifiers.
    ``_mask_identifiers`` is pure — test each masked category in isolation,
    then the integrated effect through ``_detect_risk``/``prescan``."""

    def test_backtick_span_masked(self) -> None:
        out = t._mask_identifiers("the `auth_token_helper` needs a rename")
        self.assertNotIn("auth_token_helper", out)
        self.assertIn("the", out)
        self.assertIn("needs a rename", out)

    def test_path_like_token_masked(self) -> None:
        out = t._mask_identifiers("see auth/session.py for details")
        self.assertNotIn("auth/session.py", out)
        self.assertIn("see", out)
        self.assertIn("for details", out)

    def test_branch_like_kebab_token_masked(self) -> None:
        out = t._mask_identifiers("branch 'mac-migration' was merged")
        self.assertNotIn("mac-migration", out)
        self.assertIn("branch", out)
        self.assertIn("was merged", out)

    def test_snake_case_token_masked(self) -> None:
        out = t._mask_identifiers("the migrate_users_table flag is unrelated")
        self.assertNotIn("migrate_users_table", out)

    def test_url_masked(self) -> None:
        out = t._mask_identifiers("see https://github.com/foo/auth-migrate-tool for ref")
        self.assertNotIn("https://github.com/foo/auth-migrate-tool", out)
        self.assertIn("see", out)
        self.assertIn("for ref", out)

    def test_masking_never_fuses_adjacent_words(self) -> None:
        # Masked spans become a single space, not empty string, so the words
        # on either side stay distinct tokens (no accidental new word).
        out = t._mask_identifiers("update `foo` bar")
        self.assertNotIn("updatebar", out)

    def test_prose_risk_word_survives_masking(self) -> None:
        # Masking must not eat legitimate standalone prose risk words.
        out = t._mask_identifiers("write a DB migration to alter table users")
        self.assertIn("migration", out)

    def test_mac_migration_branch_no_longer_flags_risk(self) -> None:
        # The concrete false positive from GH issue #5: a prompt quoting the
        # branch name 'mac-migration' must NOT trigger the soft-risk
        # 'migration' keyword.
        pre = t.prescan(
            "On branch 'mac-migration', add 3 lines to .gitignore "
            "(single file change) to exclude local build artifacts on main.",
            None,
        )
        self.assertEqual(pre.risk, "low")
        self.assertNotIn("migration", pre.risk_flags)

    def test_real_migration_work_still_flags_risk(self) -> None:
        # Genuine migration work (not just a branch-name mention) must still
        # be caught — masking must not blind the detector to real risk.
        pre = t.prescan("migrate the DB schema to v2, adding new tables via alembic", None)
        self.assertEqual(pre.risk, "high")
        self.assertIn("migration", pre.risk_flags)

    def test_real_target_path_risk_survives_masking(self) -> None:
        # A genuinely risky NAMED TARGET FILE (extracted as a real candidate
        # path) must still force risk=high — masking only strips prose noise,
        # not the deterministic file-path risk signal.
        pre = t.prescan("add a login check to auth/session.py", None)
        self.assertEqual(pre.risk, "high")
        self.assertIn("auth", pre.risk_flags)


class RiskWordNarrowingTests(unittest.TestCase):
    """The recurring failure mode: a bare risk word that is ordinary vocabulary
    HERE forces tier=L, because auth is a HARD flag one match dominates.

    Five words have been narrowed for this reason — `secret` (2026-06-07),
    `session` (T09), the payment words (T14), `token` (T18) and `permission`
    (T19). Only the last arrived with tests; the first four were narrowed in
    the regex with explanatory comments and pinned by nothing, so re-widening
    any of them would have gone unnoticed. Both directions are asserted here:
    the project's own vocabulary must stay clean, and real security work must
    still be caught. Under-matching a rare phrase is acceptable; a false L is
    every stage on Anthropic and L-sized caps.
    """

    def _flags(self, text: str) -> list[str]:
        return t.prescan(text, None).risk_flags

    # -- T19: permission ----------------------------------------------------
    HARNESS_PERMISSION = [
        "run claude with --dangerously-skip-permissions",
        "permissions: contents: read in the workflow",   # our own ci.yml
        "reduce permission prompts in the session",
        "check file permissions on the worktree",
        "the permission classifier blocked the delete",
        "add a permission prompt for destructive commands",
        "fix directory permissions after checkout",
        "the harness asks permission before each edit",
        "chmod the script, its permissions are wrong",
    ]
    AUTH_PERMISSION = [
        "privilege escalation in the admin panel",
        "grant admin permission to the service account",
        "permission bypass in the API layer",
        "add RBAC roles and permissions",
        "ACL check on the bucket",
        "user permissions are not revoked on logout",
        "role permission model needs a rewrite",
        "elevated permissions for the installer",
        "oauth scope permissions are too wide",
    ]

    def test_harness_permission_vocabulary_is_not_auth(self) -> None:
        for text in self.HARNESS_PERMISSION:
            self.assertNotIn("auth", self._flags(text),
                             f"{text!r} — harness vocabulary forced an auth flag")

    def test_real_permission_work_is_still_auth(self) -> None:
        for text in self.AUTH_PERMISSION:
            self.assertIn("auth", self._flags(text),
                          f"{text!r} — real auth work lost its flag")

    # -- T18: token ---------------------------------------------------------
    def test_budget_token_vocabulary_is_not_auth(self) -> None:
        for text in [
            "tokens, not dollars, are the budget unit on a subscription",
            "raise the token cap for the developer stage",
            "the stage burned 500k tokens before parking",
            "count input and output tokens per stage",
        ]:
            self.assertNotIn("auth", self._flags(text), text)

    def test_credential_tokens_are_still_auth(self) -> None:
        for text in [
            "rotate the api token in the vault",
            "the access token leaks into the log",
            "add refresh token revocation",
            "validate the JWT before trusting it",
        ]:
            self.assertIn("auth", self._flags(text), text)

    # -- T09: session -------------------------------------------------------
    def test_ordinary_session_vocabulary_is_not_auth(self) -> None:
        for text in [
            "give each child its own session per stage",
            "the 5-hour session limit was reached",
            "resume the tmux session after reboot",
        ]:
            self.assertNotIn("auth", self._flags(text), text)

    def test_real_session_attacks_are_still_auth(self) -> None:
        for text in [
            "fix session fixation on login",
            "session hijacking via the cookie",
        ]:
            self.assertIn("auth", self._flags(text), text)

    # -- T14: payment / 2026-06-07: secret ----------------------------------
    def test_our_own_vocabulary_is_not_payment_or_auth(self) -> None:
        for text, flag in [
            ("the CLI charges every session at Anthropic rates", "payment"),
            ("CI on GitHub uses no repository secrets", "auth"),
        ]:
            self.assertNotIn(flag, self._flags(text), text)

    def test_real_payment_work_is_still_payment(self) -> None:
        for text in [
            "fix the Stripe checkout flow",
            "handle the chargeback webhook",
        ]:
            self.assertIn("payment", self._flags(text), text)


class ClassifyTableTests(unittest.TestCase):
    """The Q3 policy table — one assertion per branch."""

    def test_risk_high_forces_L(self) -> None:
        pre = t.prescan("add a small helper to auth/login.py", None)  # tiny but risky
        tri = t.classify(pre, None, THR)
        self.assertEqual(tri.tier, "L")
        self.assertEqual(tri.dimensions["risk"], "high")

    def test_clear_tiny_is_S_without_llm(self) -> None:
        pre = t.prescan("add greet() to util.py", None)
        tri = t.classify(pre, None, THR)
        self.assertEqual(tri.tier, "S")
        self.assertEqual(tri.source, "deterministic")
        self.assertEqual(tri.caps["iteration_cap"], 1)
        self.assertEqual(tri.caps["token_cap"], 300_000)  # tokens, not $ (subscription)
        self.assertNotIn("cost_cap_usd", tri.caps)

    def test_llm_size_L_forces_L(self) -> None:
        pre = t.prescan("add greet() to util.py", None)
        v = t.Verdict(size="L", clarity="clear", confidence=0.9, ok=True)
        self.assertEqual(t.classify(pre, v, THR).tier, "L")

    def test_underspecified_forces_L(self) -> None:
        pre = t.prescan("add greet() to util.py", None)
        v = t.Verdict(size="S", clarity="underspecified", confidence=0.9, ok=True)
        self.assertEqual(t.classify(pre, v, THR).tier, "L")

    def test_low_confidence_forces_L(self) -> None:
        pre = t.prescan("add greet() to util.py", None)
        v = t.Verdict(size="S", clarity="clear", confidence=0.3, ok=True)  # < 0.7
        self.assertEqual(t.classify(pre, v, THR).tier, "L")

    def test_clear_small_confident_is_S(self) -> None:
        pre = t.prescan("add greet() to util.py", None)
        v = t.Verdict(size="S", clarity="clear", confidence=0.9, ok=True)
        self.assertEqual(t.classify(pre, v, THR).tier, "S")

    def test_medium_default_is_M(self) -> None:
        # several files, small-scope verb, confident, clear, low risk → not S, not L
        pre = t.prescan(
            "update a.py, b.py, c.py, d.py and e.py to add a shared header", None,
        )
        v = t.Verdict(size="M", clarity="clear", confidence=0.9, ok=True)
        tri = t.classify(pre, v, THR)
        self.assertEqual(tri.tier, "M")
        self.assertEqual(tri.caps, {"iteration_cap": 2, "token_cap": 550_000})

    def test_overestimate_bias_takes_larger_size(self) -> None:
        # deterministic scan says S (single small file), LLM says M → take M
        pre = t.prescan("add greet() to util.py", None)
        v = t.Verdict(size="M", clarity="clear", confidence=0.9, ok=True)
        self.assertEqual(t.classify(pre, v, THR).dimensions["size"], "M")

    def test_non_dev_task_routed_to_M_not_S(self) -> None:
        pre = t.prescan("what does this repo do?", None)
        v = t.Verdict(is_dev_task=False, size="S", clarity="clear",
                      confidence=0.9, ok=True)
        tri = t.classify(pre, v, THR)
        self.assertEqual(tri.tier, "M")
        self.assertEqual(tri.verdict, "non-dev")


class DisagreementReachesMTests(unittest.TestCase):
    """2026-06-01 calibration fix: a scan↔LLM SIZE disagreement must be routed by
    the (overestimate-biased) size, not force-failed to L. The old inline 0.6 cap
    sat below the 0.7 fail-safe floor, so ANY disagreement → L and M was
    structurally near-unreachable (0/16 runs). Raising the cap to the floor lets a
    genuine S↔M disagreement land at M — WITHOUT weakening risk-dominance or the
    size=='L' gate, which both fire before the confidence check."""

    def test_S_vs_M_disagreement_now_reaches_M(self) -> None:
        # det scan says S (single small file), LLM says M → _max_size→M.
        # This is the d752 shape: previously force-routed L on conf 0.6<0.7.
        pre = t.prescan("add greet() to util.py", None)
        v = t.Verdict(size="M", clarity="clear", confidence=0.9, ok=True)
        tri = t.classify(pre, v, THR)
        self.assertEqual(tri.dimensions["size"], "M")     # overestimate bias intact
        self.assertEqual(tri.tier, "M")                   # was "L" before the fix
        self.assertEqual(tri.caps, {"iteration_cap": 2, "token_cap": 550_000})

    def test_disagreement_with_size_L_still_L(self) -> None:
        # LLM says L vs det S → _max_size→L; size=='L' gate fires before conf.
        pre = t.prescan("add greet() to util.py", None)
        v = t.Verdict(size="L", clarity="clear", confidence=0.9, ok=True)
        self.assertEqual(t.classify(pre, v, THR).tier, "L")

    def test_disagreement_on_risky_task_still_L(self) -> None:
        # risk=high dominates everything — a disagreement cannot let it escape L.
        pre = t.prescan("add a login check to auth/session.py", None)
        self.assertEqual(pre.risk, "high")
        v = t.Verdict(size="M", clarity="clear", confidence=0.9, ok=True)
        self.assertEqual(t.classify(pre, v, THR).tier, "L")

    def test_disagreement_but_low_llm_confidence_still_L(self) -> None:
        # The cap is a CEILING (min), not a floor: a genuinely unsure LLM
        # (conf 0.4) stays below threshold and still fails safe to L.
        pre = t.prescan("add greet() to util.py", None)
        v = t.Verdict(size="M", clarity="clear", confidence=0.4, ok=True)
        self.assertEqual(t.classify(pre, v, THR).tier, "L")

    def test_disagree_cap_tracks_conf_threshold_from_env(self) -> None:
        import os
        os.environ.pop("TRIAGE_DISAGREE_CONF_CAP", None)
        os.environ.pop("TRIAGE_CONF_THRESHOLD", None)
        self.assertEqual(t.Thresholds.from_env().disagree_conf_cap, 0.7)
        os.environ["TRIAGE_CONF_THRESHOLD"] = "0.85"
        try:
            thr = t.Thresholds.from_env()
            # cap follows the threshold so a higher bar doesn't re-arm the squeeze
            self.assertEqual(thr.disagree_conf_cap, 0.85)
            self.assertEqual(thr.conf_threshold, 0.85)
        finally:
            os.environ.pop("TRIAGE_CONF_THRESHOLD", None)

    def test_explicit_cap_override_can_restore_force_L(self) -> None:
        # Setting the cap below the threshold restores the old "disagreement→L".
        thr = t.Thresholds(disagree_conf_cap=0.6)
        pre = t.prescan("add greet() to util.py", None)
        v = t.Verdict(size="M", clarity="clear", confidence=0.9, ok=True)
        self.assertEqual(t.classify(pre, v, thr).tier, "L")


class ExplicitSmallScopeFastPathTests(unittest.TestCase):
    """GH issue #5, item 2: an explicit, author-asserted tiny-bounded-scope
    statement ('one file', '3 lines', 'only .gitignore'...) on a chore/docs +
    clear change routes deterministically to S, even over a soft-risk hit or
    an LLM size verdict of M. Hard-risk still dominates."""

    def test_prescan_detects_explicit_small_scope_phrases(self) -> None:
        for text in [
            "change one file to fix the typo",
            "this is a single file change",
            "add 3 lines to the config",
            "a 12-line fix",
            "only .gitignore needs updating",
            "touches only config.yml",
            "exactly these files are touched",
            "only these files change",
        ]:
            self.assertTrue(t.prescan(text, None).explicit_small_scope, text)

    def test_prescan_does_not_flag_ordinary_prose_as_explicit_small_scope(self) -> None:
        for text in [
            "add greet() to util.py",
            "refactor the entire reporting subsystem",
            "update a.py, b.py, c.py, d.py and e.py to add a shared header",
        ]:
            self.assertFalse(t.prescan(text, None).explicit_small_scope, text)

    def test_mac_migration_false_positive_reproduced_now_S(self) -> None:
        # The exact incident from GH issue #5: a prompt quoting the branch
        # name 'mac-migration', a one-line .gitignore chore, and an LLM
        # verdict of {type: chore, size: M, clarity: clear, confidence: 0.70}
        # — mirroring the real run's reported reason
        # "soft-risk (migration) + size=M". Before the fix this landed on M
        # (soft-risk kept it at M even though masking alone would only get it
        # to the LLM's own M size call). After both fixes it is S.
        prompt = ("On branch 'mac-migration', add 3 lines to .gitignore "
                  "(single file change) to exclude local build artifacts on main.")
        pre = t.prescan(prompt, None)
        self.assertEqual(pre.risk, "low")               # fix #1: branch name masked
        self.assertTrue(pre.explicit_small_scope)        # fix #2 precondition
        v = t.Verdict(type="chore", size="M", clarity="clear", confidence=0.70, ok=True)
        tri = t.classify(pre, v, THR)
        self.assertEqual(tri.tier, "S")

    def test_fast_path_requires_chore_or_docs_type(self) -> None:
        prompt = ("On branch 'mac-migration', add 3 lines to .gitignore "
                  "(single file change).")
        pre = t.prescan(prompt, None)
        v = t.Verdict(type="feature", size="M", clarity="clear", confidence=0.9, ok=True)
        tri = t.classify(pre, v, THR)
        self.assertNotEqual(tri.tier, "S")

    def test_fast_path_requires_clear_clarity(self) -> None:
        pre = t.prescan("only one file needs a tweak", None)
        v = t.Verdict(type="chore", size="M", clarity="underspecified",
                       confidence=0.9, ok=True)
        self.assertEqual(t.classify(pre, v, THR).tier, "L")

    def test_fast_path_does_not_override_size_L(self) -> None:
        pre = t.prescan("only one file needs a tweak", None)
        v = t.Verdict(type="chore", size="L", clarity="clear", confidence=0.9, ok=True)
        self.assertEqual(t.classify(pre, v, THR).tier, "L")

    def test_hard_risk_still_dominates_explicit_small_scope(self) -> None:
        prompt = ("On branch 'mac-migration', add one line to auth/login.py "
                  "(single file change).")
        pre = t.prescan(prompt, None)
        self.assertTrue(pre.explicit_small_scope)
        self.assertEqual(pre.risk, "high")               # real target path auth/login.py
        v = t.Verdict(type="chore", size="S", clarity="clear", confidence=0.9, ok=True)
        tri = t.classify(pre, v, THR)
        self.assertEqual(tri.tier, "L")

    def test_fast_path_inert_without_explicit_scope_phrase(self) -> None:
        # A chore/clear/M verdict WITHOUT an explicit small-scope phrase in
        # the prompt must not be fast-pathed to S.
        pre = t.prescan("update the changelog", None)
        self.assertFalse(pre.explicit_small_scope)
        v = t.Verdict(type="chore", size="M", clarity="clear", confidence=0.9, ok=True)
        self.assertEqual(t.classify(pre, v, THR).tier, "M")


class VerdictParseTests(unittest.TestCase):
    def test_fenced_json_parsed(self) -> None:
        raw = ('```json\n{"is_dev_task": true, "type": "feature", '
               '"size": "S", "clarity": "clear", "confidence": 0.88}\n```')
        v = t.parse_verdict(raw)
        self.assertTrue(v.ok)
        self.assertEqual(v.size, "S")
        self.assertAlmostEqual(v.confidence, 0.88)

    def test_garbage_is_not_ok(self) -> None:
        self.assertFalse(t.parse_verdict("the model rambled with no json").ok)
        self.assertFalse(t.parse_verdict("").ok)

    def test_out_of_range_values_clamped(self) -> None:
        v = t.parse_verdict('{"size": "XL", "clarity": "meh", "confidence": 5}')
        self.assertEqual(v.size, "M")          # unknown size → M
        self.assertEqual(v.clarity, "clear")   # unknown clarity → clear
        self.assertEqual(v.confidence, 1.0)    # clamped to [0,1]

    def test_llm_verdict_never_raises_on_bad_runner(self) -> None:
        def boom(_p):
            raise RuntimeError("network down")
        v = t.llm_verdict("x", t.prescan("x", None), boom)
        self.assertFalse(v.ok)


class RoutingTests(unittest.TestCase):
    FULL = ["discovery", "ba", "pattern-detector", "architect", "tasks",
            "analyze", "edge-cases", "developer", "tester", "security", "reviewer"]

    def test_S_keeps_only_core(self) -> None:
        self.assertEqual(
            t.stages_for_tier("S", self.FULL),
            ["developer", "tester", "security", "reviewer"],
        )

    def test_M_keeps_ba_plus_core(self) -> None:
        self.assertEqual(
            t.stages_for_tier("M", self.FULL),
            ["ba", "developer", "tester", "security", "reviewer"],
        )

    def test_L_is_full(self) -> None:
        self.assertEqual(t.stages_for_tier("L", self.FULL), self.FULL)

    def test_core_never_drops_at_any_tier(self) -> None:
        core = {"developer", "tester", "security", "reviewer"}
        for tier in ("S", "M", "L"):
            kept = set(t.stages_for_tier(tier, self.FULL))
            self.assertTrue(core <= kept, f"{tier} dropped part of the core")

    def test_upgrade_ladder_steps(self) -> None:
        self.assertEqual(t.next_tier("S"), "M")
        self.assertEqual(t.next_tier("M"), "L")
        self.assertIsNone(t.next_tier("L"))


class RunnerBackwardCompatTests(unittest.TestCase):
    """The runner integration must be a no-op until explicitly enabled."""

    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in
                       ("TRIAGE_MODE", "TRIAGE_DISABLED", "PATTERN_DETECTION_ENABLED")}
        os.environ.pop("TRIAGE_MODE", None)
        os.environ.pop("TRIAGE_DISABLED", None)
        os.environ["PATTERN_DETECTION_ENABLED"] = "1"

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_mode_off_ignores_triage_state(self) -> None:
        os.environ["TRIAGE_MODE"] = "off"                   # explicit opt-out
        base = sra._pipeline_stages_for_agent(None)
        with_s = sra._pipeline_stages_for_agent({"triage": {"tier": "S"}})
        self.assertEqual(base, with_s)  # off → triage state ignored

    def test_default_mode_is_shadow_and_runs_full_pipeline(self) -> None:
        # TRIAGE_MODE unset (ai-delivery-private#2: default flipped off → shadow) —
        # still byte-identical stage list, but _triage_mode() now reports "shadow".
        self.assertEqual(sra._triage_mode(), "shadow")
        base = sra._pipeline_stages_for_agent(None)
        with_s = sra._pipeline_stages_for_agent({"triage": {"tier": "S"}})
        self.assertEqual(base, with_s)

    def test_shadow_runs_full_pipeline(self) -> None:
        base = sra._pipeline_stages_for_agent(None)
        os.environ["TRIAGE_MODE"] = "shadow"
        self.assertEqual(sra._pipeline_stages_for_agent({"triage": {"tier": "S"}}), base)

    def test_s_only_acts_on_S_not_M(self) -> None:
        base = sra._pipeline_stages_for_agent(None)  # mode still shadow (unset) here
        os.environ["TRIAGE_MODE"] = "s-only"
        self.assertEqual(
            sra._pipeline_stages_for_agent({"triage": {"tier": "S"}}),
            ["developer", "tester", "security", "reviewer"],
        )
        self.assertEqual(
            sra._pipeline_stages_for_agent({"triage": {"tier": "M"}}), base,
        )

    def test_full_acts_on_M(self) -> None:
        os.environ["TRIAGE_MODE"] = "full"
        stages = sra._pipeline_stages_for_agent({"triage": {"tier": "M"}})
        self.assertEqual(stages, ["ba", "developer", "tester", "security", "reviewer"])

    def test_kill_switch_restores_full(self) -> None:
        base_off = sra._pipeline_stages_for_agent(None)  # mode shadow (unset)
        os.environ["TRIAGE_MODE"] = "s-only"
        os.environ["TRIAGE_DISABLED"] = "1"              # kill switch wins
        self.assertEqual(
            sra._pipeline_stages_for_agent({"triage": {"tier": "S"}}), base_off,
        )
        self.assertEqual(sra._triage_mode(), "off")      # kill switch forces off

    def test_invalid_classification_falls_back_to_full(self) -> None:
        base = sra._pipeline_stages_for_agent(None)  # mode shadow (unset)
        os.environ["TRIAGE_MODE"] = "s-only"
        self.assertEqual(sra._pipeline_stages_for_agent({"triage": {}}), base)
        self.assertEqual(sra._pipeline_stages_for_agent({}), base)

    def test_invalid_mode_value_falls_back_to_shadow(self) -> None:
        # An unrecognised TRIAGE_MODE string is fail-safe: it now degrades to
        # `shadow` (observe-only), not `off` — ai-delivery-private#2.
        os.environ["TRIAGE_MODE"] = "bogus"
        self.assertEqual(sra._triage_mode(), "shadow")


class _SilenceSideEffects(unittest.TestCase):
    """Runner-level tests drive _maybe_run_triage / _maybe_upgrade_tier, which
    call _send_telegram (botctl-send-text subprocess) and _notify_bot (HTTP).
    Stub both so the test suite never reaches the live bot — running pytest must
    not spam the operator's Telegram."""

    def setUp(self) -> None:
        self._tg, self._nb = sra._send_telegram, sra._notify_bot
        sra._send_telegram = lambda *a, **k: None
        sra._notify_bot = lambda *a, **k: None

    def tearDown(self) -> None:
        sra._send_telegram, sra._notify_bot = self._tg, self._nb


class RunnerActingTests(_SilenceSideEffects):
    """End-to-end-ish (no claude): _maybe_run_triage writes state + lite BRD."""

    def _seed(self, mode: str, prompt: str):
        d = Path(tempfile.mkdtemp())
        (d / "state.json").write_text('{"history": []}')
        spec = {"prompt": prompt, "target_repo": str(d)}
        os.environ["TRIAGE_MODE"] = mode
        os.environ["TRIAGE_LLM_ENABLED"] = "0"   # deterministic only, no claude
        return d, spec

    def tearDown(self) -> None:
        os.environ.pop("TRIAGE_MODE", None)
        os.environ.pop("TRIAGE_LLM_ENABLED", None)
        super().tearDown()

    def test_off_is_noop(self) -> None:
        d, spec = self._seed("off", "add greet() to util.py")
        tri = sra._maybe_run_triage(d, d, {}, spec, "T1")
        self.assertIsNone(tri)
        self.assertFalse((d / "00a-triage.md").exists())

    def test_s_tier_writes_state_report_and_lite_brd(self) -> None:
        d, spec = self._seed("s-only", "add greet() to util.py with a test")
        tri = sra._maybe_run_triage(d, d, {}, spec, "T2")
        self.assertIsNotNone(tri)
        self.assertEqual(tri.tier, "S")
        self.assertTrue((d / "00a-triage.md").exists())
        self.assertTrue((d / "01-ba.md").exists())          # lite BRD for skipped BA
        st = json.loads((d / "state.json").read_text())
        self.assertEqual(st["triage"]["tier"], "S")
        self.assertEqual(st["triage"]["mode"], "s-only")

    def test_shadow_writes_state_but_no_lite_brd(self) -> None:
        d, spec = self._seed("shadow", "add greet() to util.py")
        tri = sra._maybe_run_triage(d, d, {}, spec, "T3")
        self.assertIsNotNone(tri)
        self.assertTrue((d / "00a-triage.md").exists())
        self.assertFalse((d / "01-ba.md").exists())         # shadow never acts


class UpgradeLadderTests(_SilenceSideEffects):
    def setUp(self) -> None:
        super().setUp()
        os.environ["TRIAGE_MODE"] = "s-only"

    def tearDown(self) -> None:
        os.environ.pop("TRIAGE_MODE", None)
        super().tearDown()

    def test_upgrade_raises_caps_and_bumps_tier(self) -> None:
        d = Path(tempfile.mkdtemp())
        (d / "state.json").write_text(json.dumps(
            {"history": [], "triage": {"tier": "S",
                                       "caps": {"iteration_cap": 1, "token_cap": 300_000}}}
        ))
        state = json.loads((d / "state.json").read_text())
        new_iter, upgraded = sra._maybe_upgrade_tier(d, state, 1, "test trigger")
        self.assertTrue(upgraded)
        self.assertEqual(state["triage"]["tier"], "M")          # S → M
        self.assertGreaterEqual(new_iter, 2)
        self.assertGreaterEqual(state["triage"]["caps"]["token_cap"], 550_000)  # caps rose

    def test_no_upgrade_when_not_acting(self) -> None:
        os.environ["TRIAGE_MODE"] = "shadow"   # observe only
        d = Path(tempfile.mkdtemp())
        (d / "state.json").write_text('{"history": []}')
        state = {"triage": {"tier": "S"}}
        i, up = sra._maybe_upgrade_tier(d, state, 1, "x")
        self.assertFalse(up)
        self.assertEqual(i, 1)


class TokenGovernorTests(_SilenceSideEffects):
    """Tokens — not dollars — are the budget unit on a subscription. The
    per-stage governor accumulates state.tokens_used and enforces the tier
    token_cap only when triage is acting."""

    def _seed_stage_tokens(self, d: Path, stage: str, tot: int) -> None:
        name = sra.STAGE_ARTIFACT_MAP[stage].replace(".md", ".json")
        (d / name).write_text(json.dumps(
            {"cost": {"input_tokens": tot // 2, "output_tokens": tot - tot // 2}}
        ))

    def test_read_stage_tokens(self) -> None:
        d = Path(tempfile.mkdtemp())
        self.assertEqual(sra._read_stage_tokens(d, "developer"), 0)  # no file
        self._seed_stage_tokens(d, "developer", 13282)
        self.assertEqual(sra._read_stage_tokens(d, "developer"), 13282)

    def test_add_tokens_used_is_cumulative(self) -> None:
        d = Path(tempfile.mkdtemp())
        (d / "state.json").write_text('{"history": []}')
        self.assertEqual(sra._add_tokens_used(d, 1000), 1000)
        self.assertEqual(sra._add_tokens_used(d, 500), 1500)

    def test_enforces_cap_when_acting(self) -> None:
        """A cap stop is an operator gate, not a crash (T08): the stop parks the
        task through budget_gate instead of writing stage="failed"."""
        os.environ["TRIAGE_MODE"] = "s-only"
        parked: list[dict] = []
        real_park = sra._park_budget_stop
        sra._park_budget_stop = lambda *a, **kw: parked.append(kw)
        try:
            d = Path(tempfile.mkdtemp())
            (d / "state.json").write_text(json.dumps(
                {"history": [], "triage": {"tier": "S", "caps": {"token_cap": 10_000}}}
            ))
            self._seed_stage_tokens(d, "developer", 13282)  # over the 10k cap
            self.assertTrue(sra._token_cap_exceeded(d, {}, "developer", "T"))
            self.assertEqual(len(parked), 1)
            self.assertEqual(parked[0]["stop_reason"], "token_cap")
            self.assertNotEqual(
                json.loads((d / "state.json").read_text()).get("stage"), "failed")
        finally:
            sra._park_budget_stop = real_park
            os.environ.pop("TRIAGE_MODE", None)

    def test_tracks_but_does_not_enforce_in_shadow(self) -> None:
        os.environ["TRIAGE_MODE"] = "shadow"
        try:
            d = Path(tempfile.mkdtemp())
            (d / "state.json").write_text(json.dumps(
                {"history": [], "triage": {"tier": "S", "caps": {"token_cap": 10_000}}}
            ))
            self._seed_stage_tokens(d, "developer", 13282)
            self.assertFalse(sra._token_cap_exceeded(d, {}, "developer", "T"))  # shadow never aborts
            st = json.loads((d / "state.json").read_text())
            self.assertEqual(st["tokens_used"], 13282)   # but still tracked
            self.assertNotEqual(st.get("stage"), "failed")
        finally:
            os.environ.pop("TRIAGE_MODE", None)


class SafetyRoutingTests(unittest.TestCase):
    """Safety-critical direction: complex / risky / vague tasks must NEVER route
    to S (which skips BA+architect and caps iterations at 1). Tested on the
    deterministic-only path — the WEAKEST signal; with the LLM verdict it only
    biases further toward L. This guards the 'will complex tasks still work'
    guarantee against future regressions."""

    COMPLEX_OR_RISKY = [
        "refactor the entire billing module across services",
        "add a login/session check to auth/session.py",
        "write a DB migration to alter the users table",
        "rewrite the payment/checkout charge flow",
        "update .github/workflows/deploy.yml to add a stage",
        "implement a new caching subsystem with Redis across the API",
        "change the encryption / token signing in crypto/jwt.py",
        "integrate a new third-party SDK end-to-end",
        "migrate the ORM from SQLAlchemy 1.x to 2.x",
        "add a small helper but it touches auth and 12 files",
    ]
    VAGUE = ["fix something", "fix the bug", "make it better", "improve performance"]

    def test_complex_and_risky_never_S(self) -> None:
        for txt in self.COMPLEX_OR_RISKY:
            self.assertNotEqual(t.decide(txt, None).tier, "S", f"{txt!r} routed to S — unsafe")

    def test_vague_tasks_never_S(self) -> None:
        for txt in self.VAGUE:
            self.assertNotEqual(t.decide(txt, None).tier, "S",
                                f"{txt!r} (underspecified) routed to S")

    def test_risk_forces_L_even_when_tiny(self) -> None:
        tri = t.decide("add one line to auth/login.py", None)   # tiny but risky
        self.assertEqual(tri.tier, "L")
        self.assertEqual(tri.dimensions["risk"], "high")

    def test_clear_trivial_still_S(self) -> None:
        self.assertEqual(
            t.decide("Add a function greet(name) to util.py with a test", None).tier, "S")

    def test_zero_named_files_not_S(self) -> None:
        # no concrete file anchor → too vague to size as trivial deterministically
        self.assertNotEqual(t.decide("just add a quick fix", None).tier, "S")


class ReviewerNitpickGuardTests(unittest.TestCase):
    """RFC §Q3 'what NOT to flag' + the upgrade severity gate — the fix for the
    nitpick loop that would otherwise claw back S-tier savings (the $16.95
    pathology re-entering through the upgrade ladder)."""

    def tearDown(self) -> None:
        os.environ.pop("TRIAGE_MODE", None)

    def test_hint_fires_only_for_acting_S(self) -> None:
        os.environ["TRIAGE_MODE"] = "s-only"
        self.assertIn("what NOT to flag",
                      sra._reviewer_triage_hint({"triage": {"tier": "S"}}))
        # M/L acting → no hint (full rigor)
        self.assertEqual(sra._reviewer_triage_hint({"triage": {"tier": "M"}}), "")
        # shadow → no hint (pure observation, no behaviour change)
        os.environ["TRIAGE_MODE"] = "shadow"
        self.assertEqual(sra._reviewer_triage_hint({"triage": {"tier": "S"}}), "")
        # off / no triage → no hint
        os.environ["TRIAGE_MODE"] = "off"
        self.assertEqual(sra._reviewer_triage_hint({"triage": {"tier": "S"}}), "")
        self.assertEqual(sra._reviewer_triage_hint({}), "")

    def test_reviewer_prompt_renders_with_and_without_hint(self) -> None:
        # The {triage_hint} placeholder must format cleanly in both states.
        # Kwargs come from _build_format_kwargs (not a hand-written list) so a
        # new reviewer placeholder cannot pass here and KeyError in production.
        for hint in ("", sra._REVIEWER_S_HINT):
            kw = sra._build_format_kwargs("reviewer", Path("/d"), Path("/r"), {})
            kw["triage_hint"] = hint
            rendered = sra.STAGE_PROMPTS["reviewer"].format(**kw)
            self.assertIn("REVIEW_COMPLETE", rendered)

    def test_critical_count_reads_verdict(self) -> None:
        d = Path(tempfile.mkdtemp())
        self.assertEqual(sra._reviewer_critical_count(d), 0)        # no file
        (d / "06-review-agent.json").write_text(json.dumps(
            {"verdict": {"verdict": "request_changes", "critical": 2}}))
        self.assertEqual(sra._reviewer_critical_count(d), 2)
        (d / "06-review-agent.json").write_text(json.dumps(
            {"verdict": {"verdict": "request_changes", "critical": 0}}))
        self.assertEqual(sra._reviewer_critical_count(d), 0)        # nitpick = 0 critical

    def _review_task_dir(self, verdict: dict, iteration: int = 1) -> Path:
        d = Path(tempfile.mkdtemp())
        (d / "06-review-agent.json").write_text(json.dumps({"verdict": verdict}))
        (d / "state.json").write_text(json.dumps({"iteration": iteration}))
        return d

    def test_zero_critical_request_changes_does_not_loop(self) -> None:
        # THE fix (tg-20260530-193650-1979 pathology): a request_changes verdict
        # with 0 critical findings must route straight to awaiting-approval — NOT
        # open the developer-hotfix loop. Looping on pure nitpicks burned $15.98
        # and bloated PR #6 from 420→1284 lines without ever converging.
        d = self._review_task_dir(
            {"verdict": "request_changes", "critical": 0,
             "warning": 5, "suggestion": 7})
        stage, reason = sra._decide_post_pipeline_stage(
            d, 5.0, 20.0, {"iteration_cap": 3})
        self.assertEqual(stage, "awaiting-approval")
        self.assertEqual(reason, "approve_no_critical")

    def test_critical_request_changes_still_loops(self) -> None:
        # A genuine merge-blocker (critical>0) with room below the cap MUST still
        # open the hotfix loop — the guard only spares pure-nitpick reviews.
        d = self._review_task_dir(
            {"verdict": "request_changes", "critical": 1}, iteration=1)
        stage, reason = sra._decide_post_pipeline_stage(
            d, 5.0, 20.0, {"iteration_cap": 3})
        self.assertEqual(stage, "request-changes-pending")
        self.assertEqual(reason, "request_changes")

    def test_plain_approve_unaffected(self) -> None:
        d = self._review_task_dir({"verdict": "approve", "critical": 0})
        stage, reason = sra._decide_post_pipeline_stage(
            d, 5.0, 20.0, {"iteration_cap": 3})
        self.assertEqual(stage, "awaiting-approval")
        self.assertEqual(reason, "approve")


class PocModeTests(unittest.TestCase):
    """Per-target PoC mode (Step 2), FAIL-SAFE contract (hardened after the
    adversarial audit): PoC is ON for EVERY target unless its absolute path is
    explicitly allowlisted in MERGEABLE_REPO_PATHS. A sandbox can NEVER produce
    a mergeable PR; a real-mode branch check rejects any default branch under any
    casing/prefix, and bare SHAs."""

    SANDBOX = Path("/home/x/ai-delivery-sandbox")
    REAL = Path("/home/x/telegram-userbot-ai")

    def tearDown(self) -> None:
        for k in ("MERGEABLE_REPO_PATHS", "SANDBOX_REPO_PATHS", "POC_MODE"):
            os.environ.pop(k, None)

    def test_everything_is_poc_by_default(self) -> None:
        os.environ.pop("MERGEABLE_REPO_PATHS", None)
        self.assertTrue(sra._poc_mode_for_target(self.SANDBOX))   # sandbox → PoC
        self.assertTrue(sra._poc_mode_for_target(self.REAL))      # real → PoC too (fail-safe)
        # a scratch repo NOT named 'sandbox' must still be PoC (the old escape)
        self.assertTrue(sra._poc_mode_for_target(Path("/home/x/scratch-clone")))

    def test_mergeable_only_when_allowlisted(self) -> None:
        os.environ["MERGEABLE_REPO_PATHS"] = str(self.REAL)
        self.assertFalse(sra._poc_mode_for_target(self.REAL))     # opted in → mergeable
        self.assertTrue(sra._poc_mode_for_target(self.SANDBOX))   # not listed → still PoC

    def test_sandbox_cannot_be_forced_mergeable_by_name(self) -> None:
        # SANDBOX_REPO_PATHS is COST-only now; it must NOT affect the PoC seatbelt
        os.environ["SANDBOX_REPO_PATHS"] = "/home/x/my-playground"
        os.environ.pop("MERGEABLE_REPO_PATHS", None)
        self.assertTrue(sra._poc_mode_for_target(self.SANDBOX))   # still PoC

    def test_branch_safety_poc_mode(self) -> None:
        self.assertTrue(sra._branch_safety_ok("phase-b4-poc-20260530-1200", True))
        self.assertFalse(sra._branch_safety_ok("feat/tg-1", True))
        self.assertFalse(sra._branch_safety_ok("main", True))

    def test_branch_safety_real_mode_rejects_default_variants(self) -> None:
        self.assertTrue(sra._branch_safety_ok("feat/tg-1", False))
        for bad in ("main", "Main", "MASTER", "origin/main", "develop", "HEAD",
                    "trunk", "", None, "a1b2c3d", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"):
            self.assertFalse(sra._branch_safety_ok(bad, False), f"{bad!r} slipped through")

    def test_developer_prompt_renders_poc(self) -> None:
        os.environ.pop("MERGEABLE_REPO_PATHS", None)
        k = sra._build_format_kwargs("developer", Path("/t/tg-1"), self.SANDBOX, {})
        out = sra.STAGE_PROMPTS["developer"].format(**k)   # must not KeyError
        self.assertIn("[PoC, DO NOT MERGE]", out)
        self.assertIn("phase-b4-poc-", out)

    def test_developer_prompt_renders_real_when_opted_in(self) -> None:
        os.environ["MERGEABLE_REPO_PATHS"] = str(self.REAL)
        k = sra._build_format_kwargs("developer", Path("/t/tg-1"), self.REAL, {})
        out = sra.STAGE_PROMPTS["developer"].format(**k)   # must not KeyError
        self.assertNotIn("[PoC, DO NOT MERGE]", out)
        self.assertIn("feat/tg-1", out)


class TierModelRoutingTests(unittest.TestCase):
    """_apply_tier_model_routing (committee 2026-05-31): real targets run S/M on
    Sonnet and reserve Opus for L. An explicit global pin and sandbox targets are
    both left untouched (the global pin wins; sandbox is already on its cheap
    model). Inert while a global PIPELINE_ANTHROPIC_MODEL is set."""

    REAL = Path("/home/x/projects/telegram-userbot-ai")     # name has no 'sandbox'
    SANDBOX = Path("/home/x/projects/ai-delivery-sandbox")   # name has 'sandbox'

    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in
                       ("PIPELINE_ANTHROPIC_MODEL", "SANDBOX_ANTHROPIC_MODEL",
                        "LOW_TIER_ANTHROPIC_MODEL", "SANDBOX_REPO_PATHS")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_real_S_M_get_sonnet(self) -> None:
        for tier in ("S", "M"):
            os.environ.pop("PIPELINE_ANTHROPIC_MODEL", None)
            sra._apply_tier_model_routing(self.REAL, tier, "")
            self.assertEqual(os.environ.get("PIPELINE_ANTHROPIC_MODEL"),
                             "claude-sonnet-4-6", f"tier {tier} should pin Sonnet")

    def test_real_L_gets_opus_unset(self) -> None:
        os.environ["PIPELINE_ANTHROPIC_MODEL"] = "stale"
        sra._apply_tier_model_routing(self.REAL, "L", "")
        self.assertIsNone(os.environ.get("PIPELINE_ANTHROPIC_MODEL"),
                          "tier L must leave the pin unset → Opus default")

    def test_real_no_tier_gets_opus_unset(self) -> None:
        os.environ["PIPELINE_ANTHROPIC_MODEL"] = "stale"
        sra._apply_tier_model_routing(self.REAL, None, "")
        self.assertIsNone(os.environ.get("PIPELINE_ANTHROPIC_MODEL"))

    def test_global_override_wins(self) -> None:
        # operator pinned a model globally → untouched even for a real S target
        os.environ["PIPELINE_ANTHROPIC_MODEL"] = "claude-opus-4-8"
        sra._apply_tier_model_routing(self.REAL, "S", "claude-opus-4-8")
        self.assertEqual(os.environ.get("PIPELINE_ANTHROPIC_MODEL"), "claude-opus-4-8")

    def test_sandbox_untouched(self) -> None:
        # sandbox already pinned by the per-target step; tier routing must not touch it
        os.environ["PIPELINE_ANTHROPIC_MODEL"] = "claude-sonnet-4-6"
        sra._apply_tier_model_routing(self.SANDBOX, "L", "")
        self.assertEqual(os.environ.get("PIPELINE_ANTHROPIC_MODEL"), "claude-sonnet-4-6")

    def test_low_tier_model_env_override(self) -> None:
        os.environ["LOW_TIER_ANTHROPIC_MODEL"] = "claude-sonnet-custom"
        sra._apply_tier_model_routing(self.REAL, "S", "")
        self.assertEqual(os.environ.get("PIPELINE_ANTHROPIC_MODEL"), "claude-sonnet-custom")


if __name__ == "__main__":
    unittest.main()
