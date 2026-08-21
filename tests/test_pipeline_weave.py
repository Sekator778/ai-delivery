"""Static weave-consistency tests for the agent-path pipeline.

Catches inter-stage artifact-name mismatches WITHOUT running `claude` — the
exact class of bug that aborted B.2 at "pattern-detector: 01-ba.md missing".

Each stage's orchestrator prompt hardcodes the canonical (unsuffixed) artifact
names of upstream stages (01-ba.md, 02-architecture.md, 03-dev.md, …) while the
agent path writes "-agent"-suffixed files. `_canonicalize_stage_artifact`
mirrors each stage's output to the canonical name after the stage completes.
These tests assert that every artifact a stage's prompt reads is actually
produced by an earlier stage (under either the canonical or the -agent name),
so the weave can be validated at $0 before any paid pipeline run.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "dispatcher"))

# Exercise the maximal pipeline (all opt-in stages on), matching bot/.env.
os.environ.setdefault("DISCOVERY_ENABLED", "1")
os.environ.setdefault("PATTERN_DETECTION_ENABLED", "1")
os.environ.setdefault("TASKS_STAGE_ENABLED", "1")
os.environ.setdefault("ANALYZE_STAGE_ENABLED", "1")
os.environ.setdefault("EDGE_CASES_STAGE_ENABLED", "1")

import stage_runner_agent as sra  # noqa: E402

# Matches pipeline artifacts like 00-discovery.md, 01-ba.md, 01b-patterns.md,
# 01-ba-agent.md, 02-architecture-agent.md, 03-dev.md … . Deliberately does
# NOT match sub-path inputs (requirements.md, spec.json, task.md,
# pr-diff-for-agent.md) — those always exist independent of a producing stage.
ARTIFACT_RE = re.compile(r"\b\d{2}[a-z]?-[a-z0-9-]+\.md\b")


def _produced_by(stage: str) -> set[str]:
    """Both names a stage yields once complete: the -agent output and the
    canonical mirror written by _canonicalize_stage_artifact."""
    out: set[str] = set()
    if stage in sra.STAGE_ARTIFACT_MAP:
        out.add(sra.STAGE_ARTIFACT_MAP[stage])
    if stage in sra.STAGE_CANONICAL_ARTIFACT:
        out.add(sra.STAGE_CANONICAL_ARTIFACT[stage])
    return out


class WeaveConsistencyTests(unittest.TestCase):
    def test_every_referenced_artifact_is_produced_upstream(self) -> None:
        order = sra._pipeline_stages_for_agent()
        self.assertIn("pattern-detector", order)  # flags applied
        self.assertEqual(order[0], "discovery")

        produced: set[str] = set()
        problems: list[str] = []
        for stage in order:
            refs = set(ARTIFACT_RE.findall(sra.STAGE_PROMPTS[stage]))
            own = _produced_by(stage)
            for ref in sorted(refs):
                if ref in own or ref in produced:
                    continue
                problems.append(
                    f"stage '{stage}' reads '{ref}' but no earlier stage "
                    f"produces it (available upstream: {sorted(produced)})"
                )
            produced |= own
        self.assertEqual(problems, [], "\n".join(problems))

    def test_brd_guard_is_satisfied_by_ba(self) -> None:
        # _run_pipeline_stage_with_breadcrumbs hard-requires 01-ba.md for every
        # stage after ba/discovery. The canonicalization must therefore yield
        # exactly that name from the ba stage.
        self.assertEqual(sra.STAGE_CANONICAL_ARTIFACT["ba"], "01-ba.md")

    def test_developer_hotfix_inputs_resolve(self) -> None:
        # The hotfix stage runs after a full first pass; its prompt reads prior
        # artifacts. All must be produced by the standard pipeline stages.
        prior: set[str] = set()
        for s in ("discovery", "ba", "pattern-detector", "architect",
                  "developer", "tester", "security", "reviewer"):
            prior |= _produced_by(s)
        own = _produced_by("developer-hotfix")
        missing = [
            r for r in sorted(ARTIFACT_RE.findall(sra.STAGE_PROMPTS["developer-hotfix"]))
            if r not in prior and r not in own
        ]
        self.assertEqual(missing, [], f"hotfix reads unproduced artifacts: {missing}")

    def test_every_stage_resolves_a_backend(self) -> None:
        # A stage missing from BACKEND makes _resolve_stage_backend raise
        # KeyError, which crashes the runner mid-stage (rc=1) and leaves the
        # task stuck for the watcher to respawn — the pattern-detector loop.
        stages = list(sra._pipeline_stages_for_agent()) + ["developer-hotfix"]
        problems: list[str] = []
        for stage in stages:
            try:
                backend, _ = sra._resolve_stage_backend(stage, 1, {})
            except Exception as exc:
                problems.append(f"{stage}: {type(exc).__name__}: {exc}")
                continue
            if backend not in sra.SUPPORTED_BACKENDS:
                problems.append(f"{stage}: unsupported backend {backend!r}")
        self.assertEqual(problems, [], "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
