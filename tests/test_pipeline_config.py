"""Stages must not inherit the operator's personal ~/.claude (2026-08-15).

Moving each stage's cwd into the target project fixed PROJECT instructions
leaking across repositories. It did nothing about the USER level:
~/.claude/settings.json applies to every claude session on the machine
regardless of working directory. On the operator's machine that meant every
stage of every task inherited:

  Stop           *            afplay ~/.claude/sounds/cat-meow.mp3
  PreToolUse     Agent|Task   bash ~/.claude/hooks/agent-limit.sh
  StopFailure    rate_limit   osascript -e 'display notification ...'
  effortLevel    xhigh

The sound is absurd; agent-limit.sh is a correctness problem. It caps concurrent
subagents at 3 in "ONE GLOBAL bucket per machine (NOT per session_id)" — its own
words — and denies the fourth. The Reviewer dispatches three lenses in parallel,
tester and security run as a pair, and the operator's interactive session counts
into the same bucket, so a lens can be denied and the review returns clean with a
hole in it.

Verified by hand before the module was written: under the isolated config,
four parallel subagents all completed and the hook's global counter never moved.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import pipeline_config as pc  # noqa: E402


class _EnvSandbox(unittest.TestCase):
    """Each test runs against a throwaway config dir, never the real one."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self._saved = {k: os.environ.get(k) for k in
                       ("PIPELINE_CLAUDE_CONFIG_DIR", "PIPELINE_ISOLATED_CONFIG",
                        "CLAUDE_CONFIG_DIR")}
        os.environ["PIPELINE_CLAUDE_CONFIG_DIR"] = str(self.root / "pipeline")
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        os.environ.pop("PIPELINE_ISOLATED_CONFIG", None)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _fake_operator_dir(self, *, with_creds: bool = True) -> Path:
        """A stand-in ~/.claude carrying exactly what the real one carries."""
        op = self.root / "operator"
        op.mkdir(parents=True, exist_ok=True)
        (op / "settings.json").write_text(json.dumps({
            "hooks": {"Stop": [{"hooks": [{"type": "command",
                                           "command": "afplay meow.mp3"}]}]},
            "effortLevel": "xhigh",
        }))
        (op / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "someone@example.com"},
            "userID": "u" * 64,
            "hasCompletedOnboarding": True,
            "projects": {"/some/private/path": {"history": ["secret"]}},
            "cachedGrowthBookFeatures": {"a": 1},
        }))
        if with_creds:
            (op / ".credentials.json").write_text(json.dumps({
                "claudeAiOauth": {"accessToken": "tok",
                                  "expiresAt": 10**13},  # far future, ms
            }))
        return op


class SettingsIsolationTests(_EnvSandbox):
    def test_pipeline_settings_declare_no_hooks(self) -> None:
        target = pc.ensure()
        settings = json.loads((target / "settings.json").read_text())
        self.assertEqual(settings.get("hooks"), {},
                         "a stage must not run the operator's desktop hooks")

    def test_operator_settings_are_not_copied(self) -> None:
        op = self._fake_operator_dir()
        os.environ["CLAUDE_CONFIG_DIR"] = str(op)
        target = pc.ensure()
        settings = json.loads((target / "settings.json").read_text())
        self.assertNotIn("effortLevel", settings)
        self.assertEqual(settings["hooks"], {})

    def test_settings_are_rewritten_each_run(self) -> None:
        # The directory is managed, not seeded once — a hand-edit there must not
        # silently become policy for every future task.
        target = pc.ensure()
        (target / "settings.json").write_text(json.dumps({"hooks": {"Stop": ["x"]}}))
        target = pc.ensure()
        self.assertEqual(json.loads((target / "settings.json").read_text())["hooks"], {})


class AccountSeedTests(_EnvSandbox):
    def test_identity_is_seeded(self) -> None:
        op = self._fake_operator_dir()
        os.environ["CLAUDE_CONFIG_DIR"] = str(op)
        target = pc.ensure()
        acct = json.loads((target / ".claude.json").read_text())
        self.assertIn("oauthAccount", acct)
        self.assertIn("userID", acct)

    def test_operator_project_history_is_left_behind(self) -> None:
        op = self._fake_operator_dir()
        os.environ["CLAUDE_CONFIG_DIR"] = str(op)
        target = pc.ensure()
        acct = json.loads((target / ".claude.json").read_text())
        self.assertNotIn("projects", acct,
                         "per-repo history from the operator's config leaked in")
        self.assertNotIn("cachedGrowthBookFeatures", acct)

    def test_seeded_files_are_owner_only(self) -> None:
        op = self._fake_operator_dir()
        os.environ["CLAUDE_CONFIG_DIR"] = str(op)
        target = pc.ensure()
        for name in (".claude.json", ".credentials.json"):
            mode = (target / name).stat().st_mode & 0o777
            self.assertEqual(mode, 0o600, f"{name} is not owner-only")


class CredentialSeedTests(_EnvSandbox):
    def test_file_backed_credentials_are_copied(self) -> None:
        op = self._fake_operator_dir()
        os.environ["CLAUDE_CONFIG_DIR"] = str(op)
        target = pc.ensure()
        blob = json.loads((target / ".credentials.json").read_text())
        self.assertEqual(blob["claudeAiOauth"]["accessToken"], "tok")

    def test_fresh_token_is_not_reseeded(self) -> None:
        op = self._fake_operator_dir()
        os.environ["CLAUDE_CONFIG_DIR"] = str(op)
        target = pc.ensure()
        (target / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {"accessToken": "already-here", "expiresAt": 10**13}}))
        pc.ensure()
        blob = json.loads((target / ".credentials.json").read_text())
        self.assertEqual(blob["claudeAiOauth"]["accessToken"], "already-here")

    def test_expired_token_is_replaced(self) -> None:
        op = self._fake_operator_dir()
        os.environ["CLAUDE_CONFIG_DIR"] = str(op)
        target = pc.ensure()
        (target / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {"accessToken": "stale", "expiresAt": 1}}))
        pc.ensure()
        blob = json.loads((target / ".credentials.json").read_text())
        self.assertEqual(blob["claudeAiOauth"]["accessToken"], "tok")

    def test_unparseable_token_counts_as_stale(self) -> None:
        # Handing a stage a dead credential costs the whole stage; re-seeding a
        # good one costs nothing.
        target = pc.ensure()
        bad = target / ".credentials.json"
        bad.write_text("{ not json")
        self.assertFalse(pc._token_is_fresh(bad))


class EnvApplicationTests(_EnvSandbox):
    def test_env_points_at_the_pipeline_dir(self) -> None:
        env: dict = {}
        pc.apply_to_env(env)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(pc.config_dir()))

    def test_opt_out_leaves_env_untouched(self) -> None:
        os.environ["PIPELINE_ISOLATED_CONFIG"] = "0"
        env: dict = {}
        pc.apply_to_env(env)
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)

    def test_every_backend_gets_the_isolated_config(self) -> None:
        # The isolation must not be an anthropic-only property: a DeepSeek or
        # GLM stage runs the same CLI and would inherit the same hooks.
        sys.path.insert(0, str(REPO_ROOT / "dispatcher"))
        import backend_routing as br
        for backend in ("anthropic", "deepseek", "glm"):
            env = br._subagent_env(backend, "tester")
            self.assertIn("CLAUDE_CONFIG_DIR", env, backend)


if __name__ == "__main__":
    unittest.main()
