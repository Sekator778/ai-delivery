"""Placeholder-coverage guard for STAGE_PROMPTS (dispatcher/stage_prompts.py).

Companion to the per-stage spot-checks already in test_triage.py / test_pr_base.py
/ test_worktree_isolation.py (`STAGE_PROMPTS[stage].format(**kw)` must not
KeyError) — this file generalizes the pattern to EVERY stage in one place, so
a future prompt edit that introduces a new `{placeholder}` without a matching
`_build_format_kwargs` entry fails fast here instead of at pipeline runtime
(where a KeyError mid-stage burns a paid subagent dispatch before anyone
notices).

Added alongside the BMAD steal-list BA/Architect fragments (issue #21):
the Architect ADR "Prevents" field and the BA Theater Check are both static
prose additions (no new placeholders), but this guard is the load-bearing
proof of that claim — and stays in place for whatever touches these prompts
next.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import stage_runner_agent as sra  # noqa: E402


class PlaceholderCoverageTests(unittest.TestCase):
    """Every STAGE_PROMPTS entry must render with the kwargs
    _build_format_kwargs produces for that stage — no unfilled placeholder,
    no KeyError. Uses a fake (non-existent) target repo path and empty state,
    matching the fake-path convention already used for the "developer" stage
    in test_triage.py (PoC-mode / base-branch resolution degrade gracefully
    on a nonexistent path; that's the behaviour under test here too, not a
    gap in this guard)."""

    def test_every_stage_prompt_formats_cleanly(self) -> None:
        task_dir = Path(tempfile.mkdtemp()) / "TASK-PLACEHOLDER"
        task_dir.mkdir()
        target_repo = Path("/repos/does-not-exist")
        problems: list[str] = []
        for stage, prompt in sra.STAGE_PROMPTS.items():
            try:
                kw = sra._build_format_kwargs(stage, task_dir, target_repo, {})
                prompt.format(**kw)
            except Exception as exc:  # noqa: BLE001 — aggregate, don't stop at first
                problems.append(f"{stage}: {type(exc).__name__}: {exc}")
        self.assertEqual(problems, [], "\n".join(problems))

    # Phrase-presence tests (ADR "Prevents" field, BA Theater Check, the
    # target-CLAUDE.md/AGENTS.md wording) were removed 2026-08-15: they pinned
    # exact prompt wording and had to be rewritten on every prompt edit. Their
    # invariant — whose instructions actually reach a stage — is proved by the
    # sandbox smoke run (SANDBOX_CANARY) that is part of the contract for
    # runner changes, and the dispatch topology is guarded by
    # tests/test_arch_map.py against docs/CALL-TREE.md.


class TargetInstructionsTests(unittest.TestCase):
    """What remains of the 2026-08-15 target-instructions guards: the one
    structural invariant a phrase edit cannot legitimately break. Stages run
    from the target repo (see dispatcher/agent_roster.py), so a relative
    template citation resolves against the wrong tree."""

    def test_template_citations_are_absolute(self) -> None:
        # Stages run from the target repo, so a bare `.claude/templates/...`
        # citation resolves against the wrong tree — and fails silently, since
        # the stage would simply find no file and proceed with the inlined copy.
        for stage, prompt in sra.STAGE_PROMPTS.items():
            self.assertNotIn("`.claude/templates/", prompt, stage)


class VoltagentPersonaTests(unittest.TestCase):
    """The vendored VoltAgent pipeline personas must stay free of upstream's
    dead machinery (2026-08-15, both cleanup passes).

    Upstream ships an inter-agent ecosystem this pipeline does not have:
    a "Query context manager" step (context-manager is the Discovery stage's
    own persona here, not a live service), requesting_agent JSON protocol
    blocks, canned delivery notifications with fabricated statistics (an
    invitation to invent results), and "Integration with other agents" lists
    naming agents absent from this roster. A re-vendor restores all of it —
    these negatives make that restoration fail the suite instead of shipping.
    """

    PERSONAS = ("backend-developer.md", "test-automator.md",
                "security-auditor.md")
    DEAD_MACHINERY = ("Query context manager", "requesting_agent",
                      "Integration with other agents", "Delivery notification")

    def _body(self, name: str) -> str:
        # Everything below the HTML provenance comment — the comment itself
        # quotes the removed upstream wording on purpose.
        text = (REPO_ROOT / ".claude" / "agents" / name).read_text()
        self.assertIn("ai-delivery override", text,
                      f"{name}: provenance comment missing")
        return text.split("-->", 1)[-1]

    def test_no_dead_upstream_machinery_in_any_body(self) -> None:
        for name in self.PERSONAS:
            body = self._body(name)
            for token in self.DEAD_MACHINERY:
                self.assertNotIn(token, body, f"{name}: {token!r} leaked back")

    def test_developer_has_no_default_language(self) -> None:
        # Upstream opened with "deep expertise in Node.js 18+, Python 3.11+,
        # and Go 1.21+" — a persona writing code for arbitrary target repos
        # cannot carry a language list; the language comes from the target's
        # own instructions.
        body = self._body("backend-developer.md")
        for token in ("Node.js 18+", "Python 3.11+", "Go 1.21+"):
            self.assertNotIn(token, body, f"language default leaked back: {token}")


if __name__ == "__main__":
    unittest.main()
