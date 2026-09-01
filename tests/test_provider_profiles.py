"""Named provider key profiles (backlog/T15).

A provider used to have exactly one key: `_subagent_env` read DEEPSEEK_API_KEY /
GLM_API_KEY straight from the environment, so two keys of the same provider
could not coexist and the cost ledger never recorded which key paid. These tests
pin the four properties that make profiles safe to add:

  * no registry ⇒ byte-identical behavior (the regression that matters most);
  * two profiles ⇒ two different keys actually reach the child env;
  * the profile's own variable NEVER reaches the child, and the provider's
    default key is dropped from a child running on a profile — the allowlist
    (#13) is not widened by this feature;
  * a profile that resolves to nothing degrades exactly like a missing global
    key: warn, fall back to anthropic.

Fixture key VALUES are assembled at runtime rather than written as literals, so
this file cannot itself become a gitleaks finding (same guard as
tests/test_child_env.py).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import provider_profiles as profiles  # noqa: E402
import backend_routing as br  # noqa: E402


def _fake_key(seed: int, n: int = 24) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(alphabet[(i * 5 + seed) % len(alphabet)] for i in range(n))


MAIN_KEY = _fake_key(3)
ALT_KEY = _fake_key(11)

_REGISTRY = {
    "profiles": {
        "main": {"backend": "deepseek", "api_key_env": "DEEPSEEK_API_KEY"},
        "alt": {"backend": "deepseek", "api_key_env": "DEEPSEEK_API_KEY_ALT"},
        "alt-url": {"backend": "deepseek", "api_key_env": "DEEPSEEK_API_KEY_ALT",
                    "base_url": "https://alt.example/anthropic"},
        "empty": {"backend": "deepseek", "api_key_env": "DEEPSEEK_KEY_NOT_SET"},
        "wrong-provider": {"backend": "glm", "api_key_env": "DEEPSEEK_API_KEY"},
    },
    "defaults": {"deepseek": "main"},
}

_BASE_ENV = {
    "PATH": "/usr/bin",
    "HOME": "/home/op",
    "DEEPSEEK_API_KEY": MAIN_KEY,
    "DEEPSEEK_API_KEY_ALT": ALT_KEY,
}


def _with_registry(registry: dict | None):
    """Env patch that points the resolver at a temp registry (or at none)."""
    env = dict(_BASE_ENV)
    if registry is None:
        env["PROVIDER_PROFILES_PATH"] = str(Path(tempfile.mkdtemp()) / "absent.json")
    else:
        path = Path(tempfile.mkdtemp()) / "providers.json"
        path.write_text(json.dumps(registry))
        env["PROVIDER_PROFILES_PATH"] = str(path)
    return mock.patch.dict(os.environ, env, clear=True)


class RegistryTests(unittest.TestCase):
    def test_absent_registry_resolves_to_nothing(self) -> None:
        with _with_registry(None):
            self.assertIsNone(profiles.resolve("deepseek", "alt"))

    def test_malformed_registry_degrades_to_nothing(self) -> None:
        path = Path(tempfile.mkdtemp()) / "providers.json"
        path.write_text("{not json")
        with mock.patch.dict(os.environ, {**_BASE_ENV,
                                          "PROVIDER_PROFILES_PATH": str(path)}, clear=True):
            self.assertEqual(profiles.load(), {})
            self.assertIsNone(profiles.resolve("deepseek"))

    def test_default_applies_when_no_profile_named(self) -> None:
        with _with_registry(_REGISTRY):
            resolved = profiles.resolve("deepseek")
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved.name, "main")
            self.assertEqual(resolved.api_key, MAIN_KEY)

    def test_profile_of_another_backend_is_refused(self) -> None:
        """A glm profile must never hand its key to deepseek."""
        with _with_registry(_REGISTRY):
            self.assertIsNone(profiles.resolve("deepseek", "wrong-provider"))

    def test_repr_never_shows_the_key(self) -> None:
        text = repr(profiles.Profile("n", "deepseek", MAIN_KEY))
        self.assertNotIn(MAIN_KEY, text)
        self.assertIn("<set>", text)

    def test_api_key_file_is_read(self) -> None:
        key_path = Path(tempfile.mkdtemp()) / "glm.key"
        key_path.write_text(ALT_KEY + "\n")
        registry = {"profiles": {"f": {"backend": "glm",
                                       "api_key_file": str(key_path)}}}
        with _with_registry(registry):
            self.assertEqual(profiles.resolve("glm", "f").api_key, ALT_KEY)


class PrecedenceTests(unittest.TestCase):
    def test_routing_label_wins_over_task_wide_selection(self) -> None:
        got = profiles.for_stage({"developer": "deepseek:alt"}, "developer",
                                 "deepseek", {"deepseek": "main"})
        self.assertEqual(got, "alt")

    def test_task_wide_selection_applies_without_a_label(self) -> None:
        got = profiles.for_stage({}, "developer", "deepseek", {"deepseek": "alt"})
        self.assertEqual(got, "alt")

    def test_profile_drops_when_the_stage_escalates(self) -> None:
        """Iteration-2 escalation / L-guard move the stage to anthropic; a
        deepseek profile means nothing there and must not follow it."""
        got = profiles.for_stage({"developer": "deepseek:alt"}, "developer",
                                 "anthropic", {"deepseek": "alt"})
        self.assertIsNone(got)

    def test_selection_parsing(self) -> None:
        backends = ("anthropic", "deepseek", "glm")
        with _with_registry(_REGISTRY):
            self.assertEqual(profiles.parse_selection("deepseek:alt", backends=backends),
                             ("deepseek:alt", None))
            self.assertEqual(profiles.parse_selection("off", backends=backends),
                             (None, None))
            self.assertIsNotNone(profiles.parse_selection("openai", backends=backends)[1])
            self.assertIsNotNone(
                profiles.parse_selection("deepseek:nope", backends=backends)[1])
            # a glm profile requested under deepseek is a config error, not a swap
            self.assertIsNotNone(
                profiles.parse_selection("deepseek:wrong-provider", backends=backends)[1])

    def test_spec_field_only_carries_a_profile(self) -> None:
        self.assertEqual(profiles.spec_field("deepseek:alt"), {"deepseek": "alt"})
        self.assertEqual(profiles.spec_field("deepseek"), {})
        self.assertEqual(profiles.spec_field(None), {})


class SubagentEnvTests(unittest.TestCase):
    """The property that decides whether this feature is safe: which key ends
    up in the child, and which variables do not."""

    def test_no_registry_keeps_todays_behavior(self) -> None:
        with _with_registry(None):
            env = br._subagent_env("deepseek", "developer")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], MAIN_KEY)
        # the backend's own variable still passes through, as it always has
        self.assertEqual(env.get("DEEPSEEK_API_KEY"), MAIN_KEY)

    def test_two_profiles_give_two_different_keys(self) -> None:
        with _with_registry(_REGISTRY):
            main = br._subagent_env("deepseek", "developer", profile="main")
            alt = br._subagent_env("deepseek", "developer", profile="alt")
        self.assertEqual(main["ANTHROPIC_AUTH_TOKEN"], MAIN_KEY)
        self.assertEqual(alt["ANTHROPIC_AUTH_TOKEN"], ALT_KEY)
        self.assertNotEqual(main["ANTHROPIC_AUTH_TOKEN"], alt["ANTHROPIC_AUTH_TOKEN"])

    def test_child_never_sees_the_profile_variable_or_the_other_key(self) -> None:
        with _with_registry(_REGISTRY):
            env = br._subagent_env("deepseek", "developer", profile="alt")
        self.assertNotIn("DEEPSEEK_API_KEY_ALT", env)   # allowlist not widened
        self.assertNotIn("DEEPSEEK_API_KEY", env)       # main key dropped
        self.assertNotIn(MAIN_KEY, env.values())
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], ALT_KEY)

    def test_profile_base_url_overrides_the_endpoint(self) -> None:
        with _with_registry(_REGISTRY):
            env = br._subagent_env("deepseek", "developer", profile="alt-url")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://alt.example/anthropic")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], ALT_KEY)

    def test_profile_without_a_key_falls_back_like_a_missing_global_key(self) -> None:
        with _with_registry(_REGISTRY):
            env = br._subagent_env("deepseek", "developer", profile="empty")
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)   # anthropic path
        self.assertNotIn("ANTHROPIC_BASE_URL", env)


class LedgerAttributionTests(unittest.TestCase):
    def _ledger(self, path: Path):
        with mock.patch.dict(os.environ, {"COST_LEDGER_PATH": str(path)}, clear=False):
            for name in ("cost_ledger",):
                sys.modules.pop(name, None)
            import cost_ledger  # noqa: PLC0415 — re-imported to pick up the path
            return cost_ledger

    def test_profile_is_recorded(self) -> None:
        db = Path(tempfile.mkdtemp()) / "cost.db"
        ledger = self._ledger(db)
        ledger.record(task_id="t", stage="developer", backend="deepseek",
                      profile="alt", cost_usd=0.11, source="computed:x")
        row = sqlite3.connect(db).execute(
            "SELECT backend, profile FROM cost_events").fetchone()
        self.assertEqual(row, ("deepseek", "alt"))

    def test_ledger_written_before_the_column_is_migrated(self) -> None:
        """A host that has been running since before profiles existed must not
        start failing every INSERT: CREATE TABLE IF NOT EXISTS does nothing to
        an existing table, so the column is added on connect."""
        db = Path(tempfile.mkdtemp()) / "old.db"
        con = sqlite3.connect(db)
        con.executescript(
            "CREATE TABLE cost_events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts TEXT NOT NULL, task_id TEXT NOT NULL, stage TEXT NOT NULL,"
            " backend TEXT NOT NULL, cost_usd REAL NOT NULL, input_tokens INTEGER,"
            " output_tokens INTEGER, cache_read_tokens INTEGER,"
            " cache_creation_tokens INTEGER, source TEXT NOT NULL,"
            " elapsed_sec REAL, session_id TEXT);")
        con.execute("INSERT INTO cost_events (ts, task_id, stage, backend, cost_usd,"
                    " source) VALUES ('2026-01-01T00:00:00+00:00','old','ba',"
                    "'anthropic',1.0,'cli')")
        con.commit()
        con.close()
        ledger = self._ledger(db)
        ledger.record(task_id="new", stage="developer", backend="deepseek",
                      profile="alt", cost_usd=0.5, source="computed:x")
        rows = sqlite3.connect(db).execute(
            "SELECT task_id, profile FROM cost_events ORDER BY id").fetchall()
        self.assertEqual(rows, [("old", None), ("new", "alt")])


if __name__ == "__main__":
    unittest.main()
