"""Child-env allowlist for spawned `claude` subprocesses (ai-delivery-private#13).

Before this, every stage child / triage call / meta-agent inherited the FULL
parent environment, so an agent running with --dangerously-skip-permissions
could read the Telegram bot token, owner ids, Windmill / LangSmith keys with a
single `env` in its Bash tool. These tests pin the inversion: the child gets
base system vars + the claude-harness vars + the ROUTED backend's model/auth
family + CHILD_ENV_EXTRA, and nothing else.

Fixture secret VALUES are assembled at runtime (never a contiguous literal in
source) so this file itself can never be a gitleaks finding — same
self-poisoning guard as tests/test_log_redaction.py.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

from child_env import BACKEND_VARS, build_child_env  # noqa: E402


def _fake_value(seed: int, n: int = 28) -> str:
    """A random-looking, sufficiently long fake secret VALUE (not a literal)."""
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(alphabet[(i * 7 + seed) % len(alphabet)] for i in range(n))


# Names the child must NEVER see: operator credentials and ids that no pipeline
# stage needs to do its job.
SECRET_NAMES = (
    "TELEGRAM_BOT_TOKEN",
    "OWNER_TELEGRAM_ID",
    "OWNER_NAME",
    "TAVILY_API_KEY",
    "LANGSMITH_API_KEY",
    "CC_LANGSMITH_API_KEY",
    "MEMO_QDRANT_URL",
    "WINDMILL_TOKEN",
    "WINDMILL_BASE_URL",
    "LITELLM_MASTER_KEY",
    "FOLDLOCK_PASSWORD",
    "AWS_SECRET_ACCESS_KEY",
)


def _fixture_env() -> dict[str, str]:
    """A parent environment shaped like the reference host's."""
    env = {
        # base system
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/home/operator",
        "SHELL": "/bin/bash",
        "USER": "operator",
        "LOGNAME": "operator",
        "TERM": "xterm-256color",
        "TMPDIR": "/tmp",
        "TZ": "Europe/Kyiv",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "LC_CTYPE": "en_US.UTF-8",
        # claude harness / tooling
        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
        "SSH_AUTH_SOCK": "/run/user/1000/keyring/ssh",
        "XDG_CONFIG_HOME": "/home/operator/.config",
        "JAVA_HOME": "/home/operator/.sdkman/candidates/java/current",
        "MAVEN_HOME": "/home/operator/.sdkman/candidates/maven/current",
        # backend families
        "DEEPSEEK_API_KEY": _fake_value(1),
        "DEEPSEEK_MODEL_PRIMARY": "deepseek-v4-pro",
        "GLM_API_KEY": _fake_value(2),
        "GLM_MODEL_PRIMARY": "glm-4.6",
        "ANTHROPIC_API_KEY": _fake_value(3),
        # parent-only pipeline knobs
        "STAGE_TIMEOUT_SEC": "1800",
        "TRIAGE_MODE": "s-only",
        "PIPELINE_ANTHROPIC_MODEL": "",
    }
    for i, name in enumerate(SECRET_NAMES):
        env[name] = _fake_value(11 + i)
    return env


class BaseAllowlistTests(unittest.TestCase):
    def test_base_system_vars_kept(self) -> None:
        env = build_child_env("anthropic", source=_fixture_env())
        for name in ("PATH", "HOME", "SHELL", "USER", "LOGNAME", "TERM",
                     "TMPDIR", "TZ", "LANG"):
            self.assertIn(name, env, name)

    def test_locale_family_kept_by_prefix(self) -> None:
        env = build_child_env("anthropic", source=_fixture_env())
        self.assertIn("LC_ALL", env)
        self.assertIn("LC_CTYPE", env)

    def test_harness_vars_kept(self) -> None:
        env = build_child_env("anthropic", source=_fixture_env())
        # Agent teams flag is load-bearing: stage prompts spawn teams.
        self.assertEqual(env.get("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"), "1")
        # git push over ssh + gh/CLI config discovery keep working.
        self.assertIn("SSH_AUTH_SOCK", env)
        self.assertIn("XDG_CONFIG_HOME", env)

    def test_build_toolchain_vars_kept(self) -> None:
        # A developer/tester stage builds the target repo; on a multi-JDK host
        # `mvn` resolves its JDK through JAVA_HOME.
        env = build_child_env("deepseek", source=_fixture_env())
        self.assertIn("JAVA_HOME", env)
        self.assertIn("MAVEN_HOME", env)

    def test_missing_vars_are_not_invented(self) -> None:
        env = build_child_env("anthropic", source={"PATH": "/bin"})
        self.assertEqual(env, {"PATH": "/bin"})

    def test_parent_only_knobs_dropped(self) -> None:
        env = build_child_env("deepseek", source=_fixture_env())
        for name in ("STAGE_TIMEOUT_SEC", "TRIAGE_MODE", "PIPELINE_ANTHROPIC_MODEL"):
            self.assertNotIn(name, env, name)


class SecretDroppingTests(unittest.TestCase):
    def test_operator_secrets_never_reach_the_child(self) -> None:
        for backend in ("anthropic", "deepseek", "glm", None, "unknown"):
            env = build_child_env(backend, source=_fixture_env())
            for name in SECRET_NAMES:
                self.assertNotIn(name, env, f"{name} leaked for backend={backend}")

    def test_secret_values_absent_too(self) -> None:
        source = _fixture_env()
        env = build_child_env("deepseek", source=source)
        leaked = {source[name] for name in SECRET_NAMES} & set(env.values())
        self.assertEqual(leaked, set())


class BackendSelectionTests(unittest.TestCase):
    def test_deepseek_gets_only_its_own_family(self) -> None:
        env = build_child_env("deepseek", source=_fixture_env())
        self.assertIn("DEEPSEEK_API_KEY", env)
        self.assertIn("DEEPSEEK_MODEL_PRIMARY", env)
        self.assertNotIn("GLM_API_KEY", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_glm_gets_only_its_own_family(self) -> None:
        env = build_child_env("glm", source=_fixture_env())
        self.assertIn("GLM_API_KEY", env)
        self.assertNotIn("DEEPSEEK_API_KEY", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_anthropic_gets_only_its_own_family(self) -> None:
        env = build_child_env("anthropic", source=_fixture_env())
        self.assertIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("DEEPSEEK_API_KEY", env)
        self.assertNotIn("GLM_API_KEY", env)

    def test_unknown_or_missing_backend_gets_no_model_keys(self) -> None:
        for backend in (None, "", "openai"):
            env = build_child_env(backend, source=_fixture_env())
            for family in BACKEND_VARS.values():
                for name in family:
                    self.assertNotIn(name, env, f"{name} for backend={backend!r}")

    def test_backend_label_is_case_insensitive(self) -> None:
        env = build_child_env("DeepSeek", source=_fixture_env())
        self.assertIn("DEEPSEEK_API_KEY", env)


class ChildEnvExtraTests(unittest.TestCase):
    def test_declared_extras_pass_through(self) -> None:
        source = _fixture_env()
        source["CHILD_ENV_EXTRA"] = "WINDMILL_BASE_URL, MEMO_QDRANT_URL"
        env = build_child_env("anthropic", source=source)
        self.assertIn("WINDMILL_BASE_URL", env)
        self.assertIn("MEMO_QDRANT_URL", env)
        # everything NOT declared stays dropped
        self.assertNotIn("TELEGRAM_BOT_TOKEN", env)

    def test_extra_ignores_blanks_and_unknown_names(self) -> None:
        source = _fixture_env()
        source["CHILD_ENV_EXTRA"] = " , NOT_SET_ANYWHERE ,"
        env = build_child_env("anthropic", source=source)
        self.assertNotIn("NOT_SET_ANYWHERE", env)

    def test_extra_var_itself_not_forwarded(self) -> None:
        source = _fixture_env()
        source["CHILD_ENV_EXTRA"] = "WINDMILL_BASE_URL"
        self.assertNotIn("CHILD_ENV_EXTRA", build_child_env("anthropic", source=source))

    def test_caller_declared_extra_argument(self) -> None:
        env = build_child_env("anthropic", extra=["TRIAGE_MODE"], source=_fixture_env())
        self.assertIn("TRIAGE_MODE", env)


class SubagentEnvIntegrationTests(unittest.TestCase):
    """The dispatcher's _subagent_env must build on the allowlist while keeping
    the ANTHROPIC_* rewrite that routes the claude CLI at the chosen provider."""

    def _subagent_env(self, backend: str, stage: str = "developer") -> dict:
        import backend_routing  # noqa: E402 — dispatcher dir on sys.path above

        return backend_routing._subagent_env(backend, stage)

    def test_deepseek_stage_env_is_minimal_but_routed(self) -> None:
        with mock.patch.dict(os.environ, _fixture_env(), clear=True):
            env = self._subagent_env("deepseek")
        self.assertTrue(env["ANTHROPIC_BASE_URL"].endswith("deepseek.com/anthropic"))
        self.assertTrue(env["ANTHROPIC_AUTH_TOKEN"])
        for name in SECRET_NAMES:
            self.assertNotIn(name, env, name)
        self.assertNotIn("GLM_API_KEY", env)

    def test_anthropic_stage_env_has_no_alt_provider_keys(self) -> None:
        with mock.patch.dict(os.environ, _fixture_env(), clear=True):
            env = self._subagent_env("anthropic")
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertNotIn("DEEPSEEK_API_KEY", env)
        self.assertNotIn("GLM_API_KEY", env)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", env)
        self.assertIn("PATH", env)

    def test_missing_backend_key_falls_back_without_leaking(self) -> None:
        source = _fixture_env()
        source.pop("DEEPSEEK_API_KEY")
        with mock.patch.dict(os.environ, source, clear=True):
            env = self._subagent_env("deepseek")
        self.assertNotIn("ANTHROPIC_BASE_URL", env)  # fell back to anthropic
        self.assertNotIn("GLM_API_KEY", env)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", env)


class BotSubagentEnvTests(unittest.TestCase):
    """bot.py's own sub-Claude spawn (and the meta agent) use the same helper."""

    def _bot(self):
        sys.path.insert(0, str(REPO_ROOT / "bot"))
        import bot as bot_module  # noqa: E402 — needs the bot dir on sys.path

        return bot_module

    def test_bot_subagent_env_drops_the_bot_token(self) -> None:
        bot_module = self._bot()
        with mock.patch.dict(os.environ, _fixture_env(), clear=True):
            env = bot_module._subagent_env("deepseek")
        self.assertNotIn("TELEGRAM_BOT_TOKEN", env)
        self.assertNotIn("OWNER_TELEGRAM_ID", env)
        self.assertTrue(env["ANTHROPIC_AUTH_TOKEN"])

    def test_bot_anthropic_backend_minimal(self) -> None:
        bot_module = self._bot()
        with mock.patch.dict(os.environ, _fixture_env(), clear=True):
            env = bot_module._subagent_env("anthropic")
        for name in SECRET_NAMES:
            self.assertNotIn(name, env, name)
        self.assertIn("HOME", env)


if __name__ == "__main__":
    unittest.main()
