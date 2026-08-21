"""The .claude/agents/ roster must stay exactly the working set (2026-08-15).

The directory grew to 22 files by importing two upstream catalogues wholesale;
11 of them were never dispatched by anything, and 5 were referenced only in the
README's own tables. Meanwhile the personas that DID run were the imported
boilerplate ones — `backend-developer` announced "deep expertise in Node.js 18+"
while writing Python.

The rule now: a file lives here only if something dispatches it by name, or it
is documentation. This test is what makes that rule hold, because the failure
mode is silent — an unused persona costs nothing at runtime, so nothing else
ever notices it. It fails in both directions on purpose: an orphan file is a
failure, and so is a dispatched name with no file (which would fail at runtime
as a dead `subagent_type`, after a paid stage has already started).

Provenance and sync policy for each kept file: .claude/agents/UPSTREAM.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / ".claude" / "agents"
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import stage_prompts as sp  # noqa: E402

# Non-persona files that legitimately live in the directory.
NON_PERSONA = {"README.md", "UPSTREAM"}

# Dispatched by the .claude/commands/team-* slash commands rather than by the
# pipeline. Listed explicitly so "the pipeline does not use it" alone can never
# justify keeping a file — it has to have a named consumer.
SLASH_COMMAND_PERSONAS = {
    "team-lead", "team-reviewer", "team-debugger", "team-implementer",
}


def _pipeline_personas() -> set[str]:
    """Every persona the stage prompts dispatch by name."""
    names: set[str] = set()
    for prompt in sp.STAGE_PROMPTS.values():
        names.update(re.findall(r'subagent_type\s*=\s*"([a-z0-9-]+)"', prompt))
    return names


def _roster() -> set[str]:
    return {p.stem for p in AGENTS_DIR.glob("*.md") if p.name not in NON_PERSONA}


class RosterTests(unittest.TestCase):
    def test_every_file_has_a_named_consumer(self) -> None:
        used = _pipeline_personas() | SLASH_COMMAND_PERSONAS
        orphans = sorted(_roster() - used)
        self.assertEqual(
            orphans, [],
            "personas with no consumer — delete them or name the consumer: "
            f"{orphans}",
        )

    def test_every_dispatched_persona_has_a_file(self) -> None:
        missing = sorted(_pipeline_personas() - _roster())
        self.assertEqual(
            missing, [],
            f"stage prompts dispatch personas that do not exist: {missing}",
        )

    def test_slash_command_personas_are_really_referenced(self) -> None:
        # Guard the allowlist above against becoming its own dumping ground:
        # if the team-* commands go away, these four must go with them.
        commands = REPO_ROOT / ".claude" / "commands"
        text = "\n".join(p.read_text() for p in commands.glob("*.md"))
        for name in sorted(SLASH_COMMAND_PERSONAS):
            self.assertIn(name, text,
                          f"{name} is allowlisted but no slash command mentions it")

    def test_prompts_do_not_cite_a_deleted_persona_file(self) -> None:
        # A prompt may cite a persona as reading material without dispatching
        # it (that is how python-pro survived unused for months). Such a
        # citation must still resolve to a file that exists.
        cited: set[str] = set()
        for prompt in sp.STAGE_PROMPTS.values():
            cited.update(re.findall(r'\.claude/agents/([a-z0-9-]+)\.md', prompt))
        dangling = sorted(cited - _roster())
        self.assertEqual(dangling, [],
                         f"prompts cite persona files that do not exist: {dangling}")


class UpstreamStampTests(unittest.TestCase):
    """Every persona must be accounted for in the provenance file, so the drift
    check has a complete picture and nothing is silently un-syncable."""

    def test_upstream_file_exists(self) -> None:
        self.assertTrue((AGENTS_DIR / "UPSTREAM").is_file())

    def test_every_persona_appears_in_the_stamp(self) -> None:
        stamp = (AGENTS_DIR / "UPSTREAM").read_text()
        missing = sorted(n for n in _roster() if f"{n}.md" not in stamp)
        self.assertEqual(missing, [],
                         f"personas absent from .claude/agents/UPSTREAM: {missing}")

    # VENDORED class only — the ADAPTED ones (architect, code-reviewer,
    # business-analyst, context-manager, the three lenses) are deliberately not
    # diffed: their bodies were replaced, so an upstream diff is noise. The
    # first live drift run is what moved business-analyst and context-manager
    # out of this list (2026-08-15): 275 of business-analyst's differing lines
    # existed only upstream, i.e. it had been rewritten, not patched.
    VENDORED = ("backend-developer", "test-automator", "security-auditor",
                "team-lead", "team-reviewer", "team-debugger", "team-implementer")

    def test_vendored_personas_are_in_the_drift_check(self) -> None:
        script = (REPO_ROOT / "ops" / "refresh-vendored-templates.sh").read_text()
        for name in self.VENDORED:
            self.assertIn(f"{name}.md|", script,
                          f"{name} is VENDORED but not mapped in the drift check")

    def test_adapted_personas_are_excluded_from_the_diff(self) -> None:
        script = (REPO_ROOT / "ops" / "refresh-vendored-templates.sh").read_text()
        mapping_lines = [l for l in script.splitlines() if l.startswith("$AGENTS_ROOT/")]
        mapped = {l.split("|", 1)[0].rsplit("/", 1)[-1][:-3] for l in mapping_lines}
        for name in ("architect", "code-reviewer", "business-analyst",
                     "context-manager", "blind-hunter", "edge-case-hunter",
                     "verification-gap", "pattern-detector"):
            self.assertNotIn(
                name, mapped,
                f"{name} is ADAPTED/OURS — diffing it against upstream would "
                "invite bulk-applying a diff that undoes the adaptation")


if __name__ == "__main__":
    unittest.main()
