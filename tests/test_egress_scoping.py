"""Egress scoping for stage children (backlog/T12, opt-in).

A stage child runs `claude --dangerously-skip-permissions` over a repository
whose content is the attack surface, and its Bash tool can reach any host.
The env allowlist (#13) closed "leak the key"; it cannot close "leak through a
request". With EGRESS_SCOPING_ENABLED=1 the pipeline's own settings.json now
carries a `sandbox` block that confines stage commands to an allowlist.

These tests pin two things: that the flag OFF changes nothing at all, and that
the flag ON produces the four settings that make the guard a guard rather than
a suggestion. What they cannot cover is the sandbox actually blocking a
request — that needs the CLI and a real stage, i.e. the smoke run on atlas the
design note lists as a precondition for using this.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import pipeline_config as pc  # noqa: E402


class FlagOffTests(unittest.TestCase):
    def test_settings_are_untouched_without_the_flag(self) -> None:
        """The regression that matters: an install that does not opt in gets
        exactly the settings it got before this feature existed."""
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(pc._settings_payload(), {"hooks": {}})

    def test_a_value_other_than_1_is_off(self) -> None:
        for value in ("0", "", "true", "yes"):
            with mock.patch.dict(os.environ, {pc.EGRESS_FLAG: value}, clear=True):
                self.assertNotIn("sandbox", pc._settings_payload(), value)


class FlagOnTests(unittest.TestCase):
    def _payload(self, **env) -> dict:
        with mock.patch.dict(os.environ, {pc.EGRESS_FLAG: "1", **env}, clear=True):
            return pc._settings_payload()

    def test_guard_cannot_silently_disable_itself(self) -> None:
        """failIfUnavailable: the CLI otherwise warns and runs UNSANDBOXED when
        the sandbox will not start — the T01 failure shape (a dead guard that
        reports healthy)."""
        self.assertIs(self._payload()["sandbox"]["failIfUnavailable"], True)

    def test_agent_cannot_lift_the_guard(self) -> None:
        """allowUnsandboxedCommands: false — otherwise the agent may retry a
        blocked command with dangerouslyDisableSandbox."""
        self.assertIs(self._payload()["sandbox"]["allowUnsandboxedCommands"], False)

    def test_filesystem_isolation_is_off_on_purpose(self) -> None:
        """We scope the network; the stage must still write its worktree."""
        self.assertIs(self._payload()["sandbox"]["filesystem"]["disabled"], True)

    def test_target_repo_settings_cannot_widen_the_allowlist(self) -> None:
        """allowManagedDomainsOnly — the target's own .claude/settings.json is
        untrusted input by definition here."""
        network = self._payload()["sandbox"]["network"]
        self.assertIs(network["allowManagedDomainsOnly"], True)

    def test_allowlist_covers_what_a_stage_actually_needs(self) -> None:
        domains = self._payload()["sandbox"]["network"]["allowedDomains"]
        for host in ("api.anthropic.com", "api.deepseek.com", "github.com"):
            self.assertIn(host, domains)

    def test_operator_can_extend_the_allowlist(self) -> None:
        domains = self._payload(**{pc.EGRESS_EXTRA: "crates.io, github.com"})[
            "sandbox"]["network"]["allowedDomains"]
        self.assertIn("crates.io", domains)
        self.assertEqual(domains.count("github.com"), 1)  # deduped, not doubled


if __name__ == "__main__":
    unittest.main()
