"""Three-lens reviewer stage (#21) — static contract tests.

The reviewer stage was upgraded from a single `code-reviewer` subagent pass to
three INDEPENDENT lenses (blind-hunter / edge-case-hunter / verification-gap)
dispatched by the stage orchestrator, which then performs severity triage itself
as the single authority. Source: `research/bmad-steal-list.md` §2 items 1-3
(Blind Hunter quota, verification-gap evidence discipline, severity-authority
separation) and §4 rows 1-4, adapted from BMAD-METHOD v6.11.0 (MIT).

Everything here runs at $0 — no `claude`, no network. It guards the three ways
this upgrade can silently break:

  1. **Placeholder coverage** — every stage prompt must render from
     `_build_format_kwargs` alone. A new `{placeholder}` without a matching
     kwarg is a KeyError that only shows up mid-run, after the pipeline has
     already paid for the upstream stages.
  2. **Lens agent files** — the prompt dispatches by `subagent_type`, so a
     missing/renamed file or a widened tool list is a silent regression (a lens
     with Write/Edit/Bash stops being a reviewer).
  3. **Verdict contract** — the runner regex-parses the four trailing lines and
     the hotfix loop is driven by CRITICAL; the rework must not touch that shape.
"""

from __future__ import annotations

import re
import string
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

import stage_runner_agent as sra  # noqa: E402

AGENTS_DIR = REPO_ROOT / ".claude" / "agents"
LENSES = ("blind-hunter", "edge-case-hunter", "verification-gap")

# Interactive-halt phrasing that must not survive the adaptation: BMAD's
# human-in-the-loop skills end turns waiting for a menu choice, which hangs
# forever under `claude -p` (steal-list §3).
INTERACTIVE_HALTS = (
    "stop and wait for input",
    "wait for user input",
    "wait for the user",
    "await user",
    "ask the user",
    "halt and ask",
    "obtain explicit user approval",
    "yes/no approval",
)


def _agent_text(name: str) -> str:
    return (AGENTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def _frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    out: dict = {}
    for line in text[3:end].splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


class PlaceholderCoverageTests(unittest.TestCase):
    """Every stage prompt renders from _build_format_kwargs output alone."""

    def _render(self, stage: str, state: dict | None = None) -> str:
        task_dir = Path(tempfile.mkdtemp()) / "tg-lens"
        task_dir.mkdir()
        kw = sra._build_format_kwargs(stage, task_dir, Path("/repos/target"),
                                      dict(state or {}))
        return sra.STAGE_PROMPTS[stage].format(**kw)   # must not KeyError

    def test_every_stage_prompt_renders(self) -> None:
        problems: list[str] = []
        for stage in sorted(sra.STAGE_PROMPTS):
            try:
                self._render(stage)
            except Exception as exc:  # noqa: BLE001 — the report is the point
                problems.append(f"{stage}: {type(exc).__name__}: {exc}")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_reviewer_placeholders_all_supplied(self) -> None:
        # Explicit set comparison, so a NEW reviewer placeholder fails here with
        # its own name rather than as an opaque KeyError.
        task_dir = Path(tempfile.mkdtemp())
        kw = sra._build_format_kwargs("reviewer", task_dir, Path("/repos/t"), {})
        needed = {f for _, f, _, _ in
                  string.Formatter().parse(sra.STAGE_PROMPTS["reviewer"]) if f}
        self.assertEqual(needed - set(kw), set(),
                         f"reviewer prompt placeholders missing from kwargs: "
                         f"{sorted(needed - set(kw))}")
        self.assertIn("lenses_artifact", kw)
        self.assertTrue(kw["lenses_artifact"].endswith("review-lenses.md"))

    def test_reviewer_renders_with_and_without_triage_hint(self) -> None:
        for tier in ("S", "M", "L", None):
            state = {"triage": {"tier": tier}} if tier else {}
            self.assertIn("REVIEW_COMPLETE", self._render("reviewer", state))


class LensAgentFileTests(unittest.TestCase):
    """The three lens definitions the reviewer prompt dispatches by name."""

    def test_files_exist_with_matching_name(self) -> None:
        for lens in LENSES:
            path = AGENTS_DIR / f"{lens}.md"
            self.assertTrue(path.is_file(), f"missing lens agent: {path}")
            self.assertEqual(_frontmatter(path.read_text())["name"], lens)

    def test_tool_restriction_header(self) -> None:
        # Read-only role class: a lens with Write/Edit/Bash could patch the code
        # it is reviewing. Enforced at the tooling level, not in prose.
        for lens in LENSES + ("code-reviewer",):
            tools = _frontmatter(_agent_text(lens)).get("tools", "")
            self.assertEqual([t.strip() for t in tools.split(",")],
                             ["Read", "Grep", "Glob"], f"{lens}: tools={tools!r}")

    def test_no_interactive_halts(self) -> None:
        for lens in LENSES:
            body = _agent_text(lens).lower()
            for phrase in INTERACTIVE_HALTS:
                self.assertNotIn(phrase, body, f"{lens}: interactive halt {phrase!r}")

    def test_structured_findings_block_and_terminal_line(self) -> None:
        for lens in LENSES:
            body = _agent_text(lens)
            self.assertIn(f"LENS_COMPLETE: {lens}", body)
            self.assertIn("**Lens:**", body)
            self.assertIn("**Proposed severity:**", body)

    def test_severity_is_a_proposal_not_a_verdict(self) -> None:
        for lens in LENSES:
            body = _agent_text(lens)
            self.assertIn("PROPOSAL", body)
            self.assertIn("orchestrator sets final severity", body)

    def test_blind_hunter_is_context_free_with_a_quota(self) -> None:
        body = _agent_text("blind-hunter")
        self.assertIn("at least ten", body)
        self.assertIn("01-ba.md", body)          # named as forbidden reading
        self.assertIn("context-free", body.lower())

    def test_edge_case_hunter_has_the_deletion_check(self) -> None:
        body = _agent_text("edge-case-hunter")
        self.assertIn("Deletion check", body)
        self.assertIn("neither re-established nor intentionally retired", body)
        self.assertIn("Kind: deletion", body.replace("`", ""))

    def test_verification_gap_proves_the_test_would_catch_it(self) -> None:
        body = _agent_text("verification-gap")
        self.assertIn("Demonstration", body)
        self.assertIn("smallest realistic regression", body)
        self.assertIn("Read a test before claiming what it covers", body)
        self.assertIn("search the whole repo by the symbol", body)
        for shape in ("Regression gap", "Missing-adoption gap",
                      "Broken-verification gap"):
            self.assertIn(shape, body)


class ReviewerPromptTests(unittest.TestCase):
    """The stage prompt itself: dispatch, independence, triage authority."""

    def setUp(self) -> None:
        self.prompt = sra.STAGE_PROMPTS["reviewer"]

    def test_dispatches_all_three_lenses_by_subagent_type(self) -> None:
        for lens in LENSES:
            self.assertIn(f'subagent_type = "{lens}"', self.prompt)

    def test_lenses_run_in_parallel_and_are_awaited(self) -> None:
        self.assertIn("ONE message", self.prompt)
        self.assertIn("synchronously", self.prompt)
        self.assertIn("Never background a lens", self.prompt)

    def test_lens_contexts_are_independent(self) -> None:
        self.assertIn("INDEPENDENT context", self.prompt)
        self.assertIn("any lens's output to", self.prompt)

    def test_orchestrator_is_the_single_severity_authority(self) -> None:
        # The steal-list §2 item 3 rule, verbatim in substance.
        self.assertIn("DISREGARD any severity a lens proposed", self.prompt)
        self.assertIn("information asymmetry", self.prompt)
        self.assertIn("Do NOT delegate it", self.prompt)

    def test_triage_confirms_findings_against_the_code(self) -> None:
        self.assertIn("CONFIRM OR REFUTE", self.prompt)
        self.assertIn("Dedupe", self.prompt)
        self.assertIn("DISMISSED", self.prompt)

    def test_never_exits_without_an_artifact(self) -> None:
        # Unattended-mode discipline: every failure branch still writes a verdict.
        self.assertIn("Never exit this stage without an artifact", self.prompt)
        self.assertIn("Lens failure handling", self.prompt)
        self.assertIn("if ALL THREE fail", self.prompt)
        self.assertIn("AGENT_ARTIFACT_MISSING", self.prompt)
        self.assertIn("AGENT_REVIEWER_DONE", self.prompt)

    def test_findings_sections_the_pr_comment_builder_reads(self) -> None:
        # git_pr._build_unresolved_findings_comment slices from "## Critical".
        for heading in ("## Summary", "## Lens coverage", "## Critical",
                        "## Warning", "## Suggestion", "## Dismissed"):
            self.assertIn(heading, self.prompt)


class VerdictContractTests(unittest.TestCase):
    """The runner-facing contract must be byte-identical to before the rework."""

    ARTIFACT = (
        "## Summary\nfine\n\n## Critical\nNone\n\n## Warning\nNone\n\n"
        "## Suggestion\nNone\n\n"
        "REVIEW_COMPLETE: approve\nCRITICAL: 0\nWARNING: 2\nSUGGESTION: 3\n"
    )

    def test_verdict_block_still_parses(self) -> None:
        m = sra.VERDICT_RE.search(self.ARTIFACT)
        self.assertIsNotNone(m)
        self.assertEqual((m.group(1).lower(), m.group(2), m.group(3), m.group(4)),
                         ("approve", "0", "2", "3"))

    def test_prompt_still_demands_the_four_verdict_lines(self) -> None:
        block = ("REVIEW_COMPLETE: <approve | request_changes>\n"
                 "CRITICAL: <count>\nWARNING: <count>\nSUGGESTION: <count>")
        self.assertIn(block, sra.STAGE_PROMPTS["reviewer"])

    def test_stage_wiring_unchanged(self) -> None:
        self.assertEqual(sra.STAGE_ARTIFACT_MAP["reviewer"], "06-review-agent.md")
        self.assertEqual(sra.STAGE_CANONICAL_ARTIFACT["reviewer"], "06-review.md")
        self.assertEqual(sra.STAGE_DONE_MARKERS["reviewer"], "AGENT_REVIEWER_DONE")

    def test_lenses_artifact_is_not_mistaken_for_a_stage_artifact(self) -> None:
        # test_pipeline_weave asserts every NN-*.md name a prompt reads is
        # produced upstream; the raw lens dump is reviewer-stage scratch, so its
        # name must stay outside that pattern (like pr-diff-for-agent.md).
        artifact_re = re.compile(r"\b\d{2}[a-z]?-[a-z0-9-]+\.md\b")
        kw = sra._build_format_kwargs("reviewer", Path(tempfile.mkdtemp()),
                                      Path("/repos/t"), {})
        self.assertIsNone(artifact_re.search(Path(kw["lenses_artifact"]).name))


if __name__ == "__main__":
    unittest.main()
